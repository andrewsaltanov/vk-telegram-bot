"""
APScheduler setup.

Jobs survive bot restarts: every scheduled post is saved to DB (status='pending').
On startup, reload_pending_jobs() re-adds all pending DB records into the in-memory
scheduler. If the run_date is already past, the post is sent immediately.
"""
import asyncio
import json
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

import channel_comments
from keyboards import get_published_notification_keyboard
from post_sender import send_post_to_channel
from tg_utils import safe_call
from vk_client import VKClient

logger = logging.getLogger(__name__)

# Module-level references injected by main.py via init()
_bot = None
_db = None
_config = None
_refresh_board_fn = None


def init(bot, db, config, refresh_board_fn=None):
    global _bot, _db, _config, _refresh_board_fn
    _bot, _db, _config = bot, db, config
    _refresh_board_fn = refresh_board_fn


def create_scheduler(timezone: str) -> AsyncIOScheduler:
    return AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()},
        job_defaults={"misfire_grace_time": 60},
        timezone=timezone,
    )


# ── VK content pre-flight check ───────────────────────────────────────────────

async def _check_vk_content(
    record_id: int,
    post_id: int,
    community_id: int,
    vk_post_id: int,
    saved_content: dict,
    is_suggested: bool,
) -> tuple:
    """
    Fetches current VK content and compares with saved.
    Returns: (content_to_use, content_was_updated, post_was_deleted)
    Fail-open: on any error, returns saved_content unchanged.

    A suggested post can vanish from VK not because it was withdrawn, but
    because it was approved and moved onto the public wall under a different
    post id — querying its original suggested-post id then looks identical to
    a deletion. Since we can't tell the two apart from this API alone, a
    missing/deleted suggested post is never cancelled — we just publish the
    content we already captured.
    """
    if _config is None:
        return saved_content, False, False

    comm_cfg = _config.get_community_config(community_id)
    if not comm_cfg:
        return saved_content, False, False

    try:
        async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
            post_data = await vk.fetch_post_data(community_id, vk_post_id)

        if post_data is None:
            # API error — fail-open, use saved content
            logger.warning(
                f"VK API error fetching post {vk_post_id} for record {record_id} — using saved content"
            )
            return saved_content, False, False

        if post_data == {}:
            if is_suggested:
                # Likely approved onto the wall under a different id, not deleted.
                return saved_content, False, False
            # Post was deleted from VK
            return saved_content, False, True

        # Post exists — extract fresh content and compare text
        fresh_content = VKClient.extract_post_content(post_data)
        # Preserve metadata fields added at send time
        for key in ("post_link", "author_link", "community_name"):
            fresh_content[key] = saved_content.get(key, "")

        if fresh_content.get("text", "") != saved_content.get("text", ""):
            fresh_json = json.dumps(fresh_content, ensure_ascii=False)
            await _db.update_post_content_json(post_id, fresh_json)
            await _db.update_scheduled_post_content_json(record_id, fresh_json)
            logger.info(f"VK post {vk_post_id} text changed — updated record {record_id}")
            return fresh_content, True, False

        return saved_content, False, False

    except Exception as e:
        logger.warning(f"Error checking VK content for record {record_id}: {e}")
        return saved_content, False, False


# ── Job function ──────────────────────────────────────────────────────────────

async def execute_scheduled_post(record_id: int):
    """Called by APScheduler at the scheduled time."""
    if _bot is None or _db is None:
        logger.error("Scheduler not initialised — bot/db missing")
        return

    record = await _db.get_scheduled_post_record(record_id)
    if not record or record["status"] != "pending":
        return  # Already sent or cancelled

    try:
        content = json.loads(record["content_json"])
    except (json.JSONDecodeError, TypeError) as parse_err:
        logger.error(f"Corrupt content_json for record {record_id}: {parse_err}")
        await _db.mark_scheduled_post_sent(record_id)  # Prevent infinite retry
        return

    # Fetch original post BEFORE try block — needed for VK check and board refresh
    orig_post = await _db.get_post_by_id(record["post_id"])

    # Check VK for content changes / deletion before publishing
    content_was_updated = False
    if orig_post:
        content, content_was_updated, post_deleted = await _check_vk_content(
            record_id=record_id,
            post_id=record["post_id"],
            community_id=orig_post["community_id"],
            vk_post_id=orig_post["vk_post_id"],
            saved_content=content,
            is_suggested=bool(record["is_suggested"]),
        )
        if post_deleted:
            await _db.mark_scheduled_post_cancelled(record_id)
            logger.info(f"Record {record_id}: VK post deleted — cancelling publication")
            await safe_call(
                _bot.send_message(
                    chat_id=_config.GROUP_ID,
                    text="🚫 Публикация отменена — пост удалён на VK",
                    message_thread_id=orig_post.get("tg_topic_id"),
                    reply_to_message_id=orig_post.get("tg_message_id"),
                ),
                logger,
                "Could not send deletion notice",
            )
            if _refresh_board_fn:
                await safe_call(
                    _refresh_board_fn(orig_post["community_id"]),
                    logger,
                    "Could not refresh schedule board after deletion",
                )
            return

    try:
        msg_ids, continuation_text = await send_post_to_channel(
            bot=_bot,
            channel_id=record["channel_id"],
            content=content,
            is_suggested=bool(record["is_suggested"]),
        )
        channel_msg_id = msg_ids[0] if msg_ids else None

        if channel_msg_id:
            await _db.update_scheduled_post_channel_msg_id(record_id, channel_msg_id)
            if continuation_text:
                await channel_comments.queue_continuation(
                    _db, record["channel_id"], channel_msg_id, continuation_text
                )

        await _db.mark_scheduled_post_sent(record_id)
        logger.info(
            f"Scheduled post record_id={record_id} sent to channel {record['channel_id']}"
        )

        # Refresh schedule board (remove this entry from the queue display)
        if orig_post and _refresh_board_fn:
            await safe_call(
                _refresh_board_fn(orig_post["community_id"]),
                logger,
                "Could not refresh schedule board",
            )

        # Send notification to GROUP topic
        community = await _db.get_community(orig_post["community_id"]) if orig_post else None
        muted = community.get("notifications_muted", 0) if community else 0

        if channel_msg_id and _config and orig_post and not muted:
            channel_id = record["channel_id"]
            # Build t.me link: -1001234567890 → https://t.me/c/1234567890/msg_id
            ch_id_str = str(abs(channel_id))[3:] if channel_id < 0 else str(channel_id)
            link = f"https://t.me/c/{ch_id_str}/{channel_msg_id}"

            tz = _config.tz
            time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M")
            update_line = "\n📝 Текст обновился на VK — опубликована актуальная версия" if content_was_updated else ""

            await safe_call(
                _bot.send_message(
                    chat_id=_config.GROUP_ID,
                    text=f'📢 Опубликовано в канале {time_str}{update_line}\n<a href="{link}">Ссылка на пост</a>',
                    parse_mode="HTML",
                    message_thread_id=orig_post.get("tg_topic_id"),
                    reply_to_message_id=orig_post.get("tg_message_id"),
                    reply_markup=get_published_notification_keyboard(record_id),
                    disable_web_page_preview=True,
                ),
                logger,
                "Could not send publish notification",
            )

    except Exception as e:
        logger.error(f"Failed to send scheduled post {record_id}: {e}")


# ── Startup reload ─────────────────────────────────────────────────────────────

async def reload_pending_jobs(scheduler: AsyncIOScheduler, db, now: datetime):
    """Re-add all DB-pending jobs into APScheduler after a restart."""
    records = await db.get_pending_scheduled_posts()
    reloaded = 0
    for r in records:
        run_date = datetime.fromtimestamp(r["schedule_time"], tz=scheduler.timezone)
        job_id = f"sched_{r['id']}"
        if run_date <= now:
            # Overdue — fire immediately in background
            asyncio.create_task(execute_scheduled_post(r["id"]))
        else:
            scheduler.add_job(
                execute_scheduled_post,
                trigger="date",
                run_date=run_date,
                args=[r["id"]],
                id=job_id,
                replace_existing=True,
            )
            await db.update_scheduled_post_job_id(r["id"], job_id)
        reloaded += 1

    if reloaded:
        logger.info(f"Reloaded {reloaded} pending scheduled post(s) from DB")
