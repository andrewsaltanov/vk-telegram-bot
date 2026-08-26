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
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

from keyboards import get_published_notification_keyboard
from post_sender import send_post_to_channel

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

    try:
        msg_ids = await send_post_to_channel(
            bot=_bot,
            channel_id=record["channel_id"],
            content=content,
            is_suggested=bool(record["is_suggested"]),
        )
        channel_msg_id = msg_ids[0] if msg_ids else None

        if channel_msg_id:
            await _db.update_scheduled_post_channel_msg_id(record_id, channel_msg_id)

        await _db.mark_scheduled_post_sent(record_id)
        logger.info(
            f"Scheduled post record_id={record_id} sent to channel {record['channel_id']}"
        )

        # Refresh schedule board (remove this entry from the queue display)
        orig_post = await _db.get_post_by_id(record["post_id"])
        if orig_post and _refresh_board_fn:
            try:
                await _refresh_board_fn(orig_post["community_id"])
            except Exception as board_err:
                logger.warning(f"Could not refresh schedule board: {board_err}")

        # Send notification to GROUP topic
        if channel_msg_id and _config and orig_post:
            channel_id = record["channel_id"]
            # Build t.me link: -1001234567890 → https://t.me/c/1234567890/msg_id
            ch_id_str = str(abs(channel_id))[3:] if channel_id < 0 else str(channel_id)
            link = f"https://t.me/c/{ch_id_str}/{channel_msg_id}"

            tz = ZoneInfo(_config.TIMEZONE)
            time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M")

            try:
                await _bot.send_message(
                    chat_id=_config.GROUP_ID,
                    text=f'📢 Опубликовано в канале {time_str}\n<a href="{link}">Ссылка на пост</a>',
                    parse_mode="HTML",
                    message_thread_id=orig_post.get("tg_topic_id"),
                    reply_to_message_id=orig_post.get("tg_message_id"),
                    reply_markup=get_published_notification_keyboard(record_id),
                    disable_web_page_preview=True,
                )
            except Exception as notify_err:
                logger.warning(f"Could not send publish notification: {notify_err}")

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
