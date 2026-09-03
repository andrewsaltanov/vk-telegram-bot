"""
VK polling loop: fetch new posts, detect deletions, sync to Telegram topics.
Each community uses its own VK token and posts to its own Telegram channel.
"""
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import channel_comments
from config import Config
from database import Database
from keyboards import get_schedule_keyboard
from post_sender import send_vk_post_to_topic
from tg_utils import safe_call
from vk_client import VKClient

logger = logging.getLogger(__name__)

# Delays between VK API calls to stay under VK's rate limit (error 6/29 = "too many requests").
BETWEEN_COMMUNITIES_DELAY = 1
BETWEEN_WALL_TYPES_DELAY = 0.5
BETWEEN_SENT_POSTS_DELAY = 2
BETWEEN_DELETION_CHECKS_DELAY = 1
BETWEEN_JOB_CANCELS_DELAY = 0.1

# Cap on post_exists() calls per _check_deletions() invocation. A community with
# a large deletion backlog (e.g. after fixing a detection bug) can otherwise stall
# an entire poll cycle for tens of minutes, delaying new-post detection for every
# community. Excess candidates are simply picked up again on the next poll cycle.
# BETWEEN_DELETION_CHECKS_DELAY already paces the actual VK API call rate, so
# raising this cap only affects how much of a large backlog drains per cycle,
# not how fast individual calls fire.
MAX_DELETION_CHECKS_PER_CYCLE = 100

# Consecutive poll failures (VK API/network errors, not "no new posts") for a
# single community before we alert the admins — avoids alerting on one-off blips.
POLL_FAIL_ALERT_THRESHOLD = 3


class VKPoller:
    def __init__(self, bot: Bot, db: Database, config: Config, scheduler: AsyncIOScheduler):
        self.bot = bot
        self.db = db
        self.config = config
        self.scheduler = scheduler
        self.running = False
        # (community_id, post_type) -> consecutive VK API/network failures.
        # In-memory only (resets on restart) — this only paces admin alerts,
        # nothing depends on it surviving a restart.
        self._poll_fail_counts: dict[tuple[int, str], int] = {}

    async def start(self):
        self.running = True
        logger.info(
            f"VK Poller started (interval={self.config.POLL_INTERVAL}s, "
            f"communities={[c.group_id for c in self.config.COMMUNITIES]})"
        )
        while self.running:
            try:
                await self._poll_all()
            except Exception as e:
                logger.error(f"Polling cycle error: {e}", exc_info=True)
            await asyncio.sleep(self.config.POLL_INTERVAL)

    def stop(self):
        self.running = False

    # ── Main cycle ────────────────────────────────────────────────────────────

    async def _poll_all(self):
        communities = await self.db.get_communities()
        for community in communities:
            # Get per-community VK token from config
            comm_cfg = self.config.get_community_config(community["vk_id"])
            if not comm_cfg:
                logger.warning(f"No config for community {community['vk_id']}, skipping.")
                continue
            try:
                async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
                    await self._poll_community(vk, community)
            except Exception as e:
                logger.error(
                    f"Error polling community {community['vk_id']}: {e}", exc_info=True
                )
            await asyncio.sleep(BETWEEN_COMMUNITIES_DELAY)

        try:
            await channel_comments.flush_stale_continuations(self.bot, self.db, self.config)
        except Exception as e:
            logger.error(f"Error flushing stale comment continuations: {e}", exc_info=True)

    async def _poll_community(self, vk: VKClient, community: dict):
        if community.get("published_topic_id"):
            await self._poll_wall(vk, community, "published")
            await asyncio.sleep(BETWEEN_WALL_TYPES_DELAY)
        if community.get("suggested_topic_id"):
            await self._poll_wall(vk, community, "suggested")

    # ── Wall polling ──────────────────────────────────────────────────────────

    async def _poll_wall(self, vk: VKClient, community: dict, post_type: str):
        community_id = community["vk_id"]
        topic_id = (
            community["published_topic_id"]
            if post_type == "published"
            else community["suggested_topic_id"]
        )
        last_id_key = "last_post_id" if post_type == "published" else "last_suggest_id"
        last_known_id = community.get(last_id_key, 0)

        if post_type == "published":
            vk_posts = await vk.get_wall_posts(community_id, count=50)
        else:
            vk_posts = await vk.get_suggested_posts(community_id, count=50)

        fail_key = (community_id, post_type)
        if vk_posts is None:
            await self._note_poll_failure(community, post_type, fail_key)
            return
        await self._note_poll_recovery(community, post_type, fail_key)

        # Sort oldest → newest by publication date
        vk_posts.sort(key=lambda p: p.get("date", p["id"]))

        is_first_run = last_known_id == 0

        if is_first_run:
            n = self.config.INITIAL_POSTS_COUNT
            posts_to_send = vk_posts[-n:] if n > 0 else []
        else:
            posts_to_send = [p for p in vk_posts if p["id"] > last_known_id]

        # Update last_post_id BEFORE sending so next poll sees new posts
        # even if bot restarts or flood control delays the current batch
        new_max_id = max((p["id"] for p in vk_posts), default=0)
        if new_max_id > last_known_id:
            await self.db.update_community_last_id(community_id, post_type, new_max_id)
            community[last_id_key] = new_max_id

        # Send new posts — small delay, flood control handled by retry in post_sender
        for post in posts_to_send:
            try:
                await self._send_post(vk, community, post, topic_id, post_type)
            except TelegramBadRequest as e:
                if "thread" in str(e).lower():
                    logger.warning(
                        f"Topic {topic_id} for community {community_id} ({post_type}) not found — "
                        "clearing from DB. Will be recreated on next restart."
                    )
                    await self.db.clear_community_topic(community_id, post_type)
                    return  # skip remaining posts for this topic
                raise
            await asyncio.sleep(BETWEEN_SENT_POSTS_DELAY)

        # Detect deleted posts
        current_vk_ids = {p["id"] for p in vk_posts}
        await self._check_deletions(vk, community, post_type, current_vk_ids)

    # ── Poll failure tracking / admin alerts ─────────────────────────────────────

    async def _note_poll_failure(self, community: dict, post_type: str, fail_key: tuple):
        count = self._poll_fail_counts.get(fail_key, 0) + 1
        self._poll_fail_counts[fail_key] = count
        if count == POLL_FAIL_ALERT_THRESHOLD:
            label = "публикаций" if post_type == "published" else "предложки"
            await safe_call(
                self.bot.send_message(
                    chat_id=self.config.GROUP_ID,
                    text=(
                        f"⚠️ Сообщество «{community.get('name', community['vk_id'])}» "
                        f"({label}): ошибка опроса VK {count} циклов подряд — "
                        "проверьте токен/доступ."
                    ),
                ),
                logger,
                "Could not send poll-failure alert",
            )

    async def _note_poll_recovery(self, community: dict, post_type: str, fail_key: tuple):
        count = self._poll_fail_counts.pop(fail_key, 0)
        if count >= POLL_FAIL_ALERT_THRESHOLD:
            label = "публикаций" if post_type == "published" else "предложки"
            await safe_call(
                self.bot.send_message(
                    chat_id=self.config.GROUP_ID,
                    text=(
                        f"✅ Опрос VK для «{community.get('name', community['vk_id'])}» "
                        f"({label}) восстановлен."
                    ),
                ),
                logger,
                "Could not send poll-recovery notice",
            )

    # ── Deletion check ────────────────────────────────────────────────────────

    async def _check_deletions(
        self,
        vk: VKClient,
        community: dict,
        post_type: str,
        current_vk_ids: set,
    ):
        if not current_vk_ids:
            return
        if post_type == "suggested":
            # A suggested post vanishes from wall.get(filter=suggests) both when it's
            # withdrawn/deleted AND when it's approved onto the public wall (often
            # under a different post id) — post_exists() can't tell those apart, so
            # we never auto-clean-up suggested posts here. Confirmed in production:
            # a suggested post got marked "deleted" and its message/DB row removed
            # just 11 minutes after being posted, almost certainly because it was
            # approved, not withdrawn. Published posts don't have this ambiguity.
            return
        # Only check posts that fall within the ID range VK returned.
        # Posts older than the batch's oldest ID can't appear in a 50-post fetch,
        # so calling post_exists() for them every cycle burns API quota needlessly.
        min_vk_id = min(current_vk_ids)
        stored_posts = await self.db.get_posts_by_community(
            community["vk_id"], post_type
        )
        candidates = [
            s for s in stored_posts
            if s["vk_post_id"] >= min_vk_id and s["vk_post_id"] not in current_vk_ids
        ]
        candidates.sort(key=lambda s: s["vk_post_id"])  # oldest first — drain backlog in order

        to_check = candidates[:MAX_DELETION_CHECKS_PER_CYCLE]
        deferred = len(candidates) - len(to_check)
        if deferred:
            logger.info(
                f"Deletion check for community {community['vk_id']} ({post_type}): "
                f"{deferred} candidate(s) deferred to next cycle"
            )

        for stored in to_check:
            exists = await vk.post_exists(community["vk_id"], stored["vk_post_id"])
            if not exists:
                await self._handle_deleted(stored)
            await asyncio.sleep(BETWEEN_DELETION_CHECKS_DELAY)

    # ── Send single post ──────────────────────────────────────────────────────

    async def _send_post(
        self,
        vk: VKClient,
        community: dict,
        post: dict,
        topic_id: int,
        post_type: str,
    ):
        community_id = community["vk_id"]

        content = vk.extract_post_content(post)
        content["author_link"] = vk.get_author_link(post, community_id)
        content["post_link"] = vk.get_post_link(community_id, post["id"])
        content["community_name"] = community.get("name", "")

        content_json = json.dumps(content, ensure_ascii=False)

        post_db_id = await self.db.save_post_get_id(
            vk_post_id=post["id"],
            community_id=community_id,
            post_type=post_type,
            tg_topic_id=topic_id,
            content_json=content_json,
        )

        if post_db_id is None:
            return  # Already exists

        keyboard = get_schedule_keyboard(post_db_id)

        msg_ids = await send_vk_post_to_topic(
            bot=self.bot,
            chat_id=self.config.GROUP_ID,
            thread_id=topic_id,
            content=content,
            keyboard=keyboard,
            is_suggested=(post_type == "suggested"),
            post_db_id=post_db_id,
        )

        if msg_ids:
            await self.db.update_post_message_id(post_db_id, msg_ids[0])
            logger.info(
                f"Sent {post_type} post vk_id={post['id']} "
                f"community={community_id} → topic={topic_id}"
            )
        else:
            # post_sender.py already falls back to a text-only send on photo failures,
            # so this means everything failed (e.g. Telegram outage) — the DB row now
            # exists (post_db_id), so this vk_post_id won't be retried; log loudly.
            logger.error(
                f"Failed to send {post_type} post vk_id={post['id']} "
                f"community={community_id} → topic={topic_id} — no message was created"
            )

    # ── Handle deletion ───────────────────────────────────────────────────────

    async def _handle_deleted(self, stored_post: dict):
        post_id = stored_post["id"]
        vk_post_id = stored_post["vk_post_id"]
        community_id = stored_post["community_id"]

        logger.info(
            f"VK post {vk_post_id} (community {community_id}) deleted — cleaning up."
        )

        # 1. Delete from group topic
        tg_msg_id = stored_post.get("tg_message_id")
        if tg_msg_id:
            try:
                await self.bot.delete_message(
                    chat_id=self.config.GROUP_ID,
                    message_id=tg_msg_id,
                )
            except TelegramBadRequest as e:
                logger.warning(f"Could not delete group message {tg_msg_id}: {e}")

        # 2. Cancel pending APScheduler jobs for this post
        scheduled = await self.db.get_pending_scheduled_for_post(post_id)
        for sched in scheduled:
            job_id = sched.get("job_id")
            if job_id:
                try:
                    self.scheduler.remove_job(job_id)
                    logger.info(f"Cancelled APScheduler job {job_id} for deleted VK post")
                except JobLookupError:
                    pass  # Already fired or never added
            await self.db.mark_scheduled_post_cancelled(sched["id"])
            await asyncio.sleep(BETWEEN_JOB_CANCELS_DELAY)

        # 3. Remove from DB (cascade deletes scheduled_channel_posts)
        await self.db.delete_post_by_id(post_id)
