"""
Scheduling flow: pick a time (preset or custom), persist an APScheduler job,
reschedule, cancel, and bulk-autoqueue posts from a topic.
"""
import json
import logging
from datetime import datetime, timedelta

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from callbacks import (
    CancelCustomTimeCallback,
    CancelSchedCallback,
    RescheduleCallback,
    RetryPublishCallback,
    ScheduleCallback,
    SchedInfoCallback,
)
from config import Config
from database import Database
from filters import IsAdmin
from keyboards import get_custom_time_cancel_keyboard, get_schedule_keyboard, get_scheduled_badge
from schedule_board import refresh_schedule_board
from scheduler import execute_scheduled_post
from tg_utils import safe_call

from .common import _notify, _require_admin

logger = logging.getLogger(__name__)
router = Router()


class ScheduleStates(StatesGroup):
    waiting_custom_time = State()


# ── Schedule callback ──────────────────────────────────────────────────────────

@router.callback_query(ScheduleCallback.filter())
async def handle_schedule_callback(
    callback: CallbackQuery,
    callback_data: ScheduleCallback,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    scheduler: AsyncIOScheduler,
):
    if not await _require_admin(callback, config):
        return

    post_db_id = callback_data.post_db_id
    option = callback_data.option

    if option == "custom":
        await state.set_state(ScheduleStates.waiting_custom_time)
        await state.update_data(post_db_id=post_db_id)
        await callback.message.reply(
            "⏰ Введите время публикации:\n"
            "<code>ЧЧ:ММ</code> — сегодня\n"
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code> — конкретная дата\n\n"
            f"Часовой пояс: <b>{config.TIMEZONE}</b>",
            parse_mode="HTML",
            reply_markup=get_custom_time_cancel_keyboard(post_db_id),
        )
        await callback.answer()
        return

    minutes = int(option)
    tz = config.tz
    # minutes=0 means "now" → delay 30 seconds
    delay = timedelta(seconds=30) if minutes == 0 else timedelta(minutes=minutes)
    schedule_time = datetime.now(tz) + delay
    await _do_schedule(callback, db, scheduler, post_db_id, schedule_time, bot, config)


@router.callback_query(CancelCustomTimeCallback.filter())
async def handle_cancel_custom_time(
    callback: CallbackQuery,
    callback_data: CancelCustomTimeCallback,
    state: FSMContext,
    config: Config,
):
    if not await _require_admin(callback, config):
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


@router.message(Command("cancel"), IsAdmin())
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.reply("ℹ️ Нечего отменять.")
        return
    await state.clear()
    await message.reply("❌ Ввод времени отменён.")


def _not_a_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


# Commands (e.g. /status) must not be swallowed as an invalid time string
# while this FSM state is active — only plain text is treated as a time entry.
@router.message(ScheduleStates.waiting_custom_time, _not_a_command)
async def handle_custom_time_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    scheduler: AsyncIOScheduler,
):
    text = (message.text or "").strip()
    data = await state.get_data()
    post_db_id = data.get("post_db_id")
    tz = config.tz
    now = datetime.now(tz)

    schedule_time: datetime | None = None
    try:
        if len(text) <= 5:
            t = datetime.strptime(text, "%H:%M")
            schedule_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if schedule_time <= now:
                schedule_time += timedelta(days=1)
        else:
            naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
            schedule_time = naive.replace(tzinfo=tz)
    except ValueError:
        await message.reply(
            "❌ Неверный формат. Используйте:\n"
            "<code>ЧЧ:ММ</code> или <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
            parse_mode="HTML",
        )
        return

    if schedule_time <= now:
        await message.reply("❌ Время должно быть в будущем!")
        return

    await state.clear()
    await _do_schedule(message, db, scheduler, post_db_id, schedule_time, bot, config)


# ── Core scheduling logic ──────────────────────────────────────────────────────

async def _schedule_post_core(
    db: Database,
    scheduler: AsyncIOScheduler,
    post_db_id: int,
    channel_id: int,
    content_json: str,
    is_suggested: bool,
    schedule_time: datetime,
) -> tuple[int, int]:
    """Persist a scheduled post record and add APScheduler job. Returns (record_id, unix_ts)."""
    existing_pending = await db.get_pending_scheduled_for_post(post_db_id)
    for ep in existing_pending:
        job_id = ep.get("job_id")
        if job_id:
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        await db.mark_scheduled_post_cancelled(ep["id"])

    record_id = await db.save_scheduled_post_record(
        post_id=post_db_id,
        channel_id=channel_id,
        content_json=content_json,
        is_suggested=is_suggested,
        schedule_time=int(schedule_time.timestamp()),
    )
    job_id = f"sched_{record_id}"
    scheduler.add_job(
        execute_scheduled_post,
        trigger="date",
        run_date=schedule_time,
        args=[record_id],
        id=job_id,
        replace_existing=True,
    )
    await db.update_scheduled_post_job_id(record_id, job_id)
    return record_id, int(schedule_time.timestamp())


async def _do_schedule(
    event: CallbackQuery | Message,
    db: Database,
    scheduler: AsyncIOScheduler,
    post_db_id: int,
    schedule_time: datetime,
    bot: Bot,
    config: Config,
):
    post = await db.get_post_by_id(post_db_id)
    if not post:
        await _notify(event, "❌ Пост не найден", alert=True)
        return

    community = await db.get_community(post["community_id"])
    channel_id = community["channel_id"] if community else 0
    if not channel_id:
        await _notify(event, "❌ Канал для этого сообщества не настроен", alert=True)
        return

    # Fix B: warn if this vk_post was already published to the channel
    prev = await db.get_sent_channel_post_for_vk_id(
        post["vk_post_id"], post["community_id"]
    )
    if prev:
        tz = config.tz
        prev_time = datetime.fromtimestamp(prev["schedule_time"], tz).strftime("%d.%m.%Y %H:%M")
        prev_text = json.loads(prev["orig_content_json"]).get("text", "")
        curr_text = json.loads(post["content_json"]).get("text", "")
        warn_lines = [f"⚠️ Этот пост уже публиковался в канале {prev_time}."]
        if prev_text.strip() != curr_text.strip():
            warn_lines.append("📝 Текст изменился с момента публикации.")
        await safe_call(
            bot.send_message(
                chat_id=config.GROUP_ID,
                text="\n".join(warn_lines),
                message_thread_id=post.get("tg_topic_id"),
                reply_to_message_id=post.get("tg_message_id"),
            ),
            logger,
            "Could not send duplicate warning",
        )
        # Do NOT return — allow rescheduling

    is_suggested = post["post_type"] == "suggested"

    # Cancel any existing pending schedule for this post (prevents double-publish)
    existing_pending = await db.get_pending_scheduled_for_post(post_db_id)

    # Warn if another post for the same channel is already scheduled within ±30 min
    nearby = await db.get_pending_for_channel_near_time(channel_id, int(schedule_time.timestamp()))
    existing_ids = {ep["id"] for ep in existing_pending}
    conflicts = [c for c in nearby if c["id"] not in existing_ids]
    if conflicts:
        tz = config.tz
        times = ", ".join(
            datetime.fromtimestamp(c["schedule_time"], tz).strftime("%H:%M")
            for c in conflicts
        )
        await safe_call(
            bot.send_message(
                chat_id=config.GROUP_ID,
                text=f"⚠️ Рядом уже запланировано в этом канале: {times} (±30 мин)",
                message_thread_id=post.get("tg_topic_id"),
            ),
            logger,
            "Could not send conflict warning",
        )

    try:
        record_id, unix_ts = await _schedule_post_core(
            db=db,
            scheduler=scheduler,
            post_db_id=post_db_id,
            channel_id=channel_id,
            content_json=post["content_json"],
            is_suggested=is_suggested,
            schedule_time=schedule_time,
        )
        time_str = schedule_time.strftime("%d.%m.%Y %H:%M")
        await _notify(event, f"✅ Пост запланирован на {time_str}", alert=True)
        logger.info(f"Scheduled post {post_db_id} → channel {channel_id} at {time_str}")

        # 4. Replace schedule keyboard with a "scheduled" badge
        tg_msg_id = post.get("tg_message_id")
        topic_id = post.get("tg_topic_id")
        if tg_msg_id:
            await safe_call(
                bot.edit_message_reply_markup(
                    chat_id=config.GROUP_ID,
                    message_id=tg_msg_id,
                    reply_markup=get_scheduled_badge(post_db_id, unix_ts, config.TIMEZONE, record_id=record_id),
                ),
                logger,
                f"Could not update keyboard for post {post_db_id}",
            )

        # 5. Send reply marker in the topic
        await safe_call(
            bot.send_message(
                chat_id=config.GROUP_ID,
                text=f"✅ Запланировано на {time_str}",
                message_thread_id=topic_id,
                reply_to_message_id=tg_msg_id,
            ),
            logger,
            f"Could not send schedule marker for post {post_db_id}",
        )

        # 6. Update the schedule board in the topic
        if community:
            await safe_call(
                refresh_schedule_board(community["vk_id"]),
                logger,
                "Could not refresh schedule board",
            )

    except Exception as e:
        logger.error(f"Schedule error: {e}")
        await _notify(event, f"❌ Ошибка планирования: {e}", alert=True)


# ── Scheduled badge callback ──────────────────────────────────────────────────

@router.callback_query(SchedInfoCallback.filter())
async def handle_scheduled_info(
    callback: CallbackQuery,
    callback_data: SchedInfoCallback,
    config: Config,
):
    tz = config.tz
    dt = datetime.fromtimestamp(callback_data.unix_ts, tz=tz)
    time_str = dt.strftime("%d.%m.%Y %H:%M")
    await callback.answer(
        f"Запланировано на {time_str} ({config.TIMEZONE})",
        show_alert=True,
    )


# ── Cancel scheduled publication ─────────────────────────────────────────────

@router.callback_query(CancelSchedCallback.filter())
async def handle_cancel_sched(
    callback: CallbackQuery,
    callback_data: CancelSchedCallback,
    db: Database,
    config: Config,
    scheduler: AsyncIOScheduler,
):
    if not await _require_admin(callback, config):
        return

    record = await db.get_scheduled_post_record(callback_data.record_id)
    if not record or record["status"] != "pending":
        await callback.answer("Публикация уже не активна", show_alert=True)
        return
    job_id = record.get("job_id")
    if job_id:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
    await db.mark_scheduled_post_cancelled(callback_data.record_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("❌ Публикация отменена", show_alert=True)

    # Refresh board so cancelled post disappears from the queue display
    post = await db.get_post_by_id(record["post_id"])
    if post:
        await safe_call(
            refresh_schedule_board(post["community_id"]),
            logger,
            "Could not refresh schedule board after cancel",
        )


# ── Retry a failed publication ───────────────────────────────────────────────

@router.callback_query(RetryPublishCallback.filter())
async def handle_retry_publish(
    callback: CallbackQuery,
    callback_data: RetryPublishCallback,
    db: Database,
    config: Config,
):
    if not await _require_admin(callback, config):
        return

    record = await db.get_scheduled_post_record(callback_data.record_id)
    if not record or record["status"] != "failed":
        await callback.answer("Публикация уже не в статусе ошибки", show_alert=True)
        return

    await db.reset_scheduled_post_pending(callback_data.record_id)
    await callback.answer("🔁 Повторяем публикацию…")
    await execute_scheduled_post(callback_data.record_id)


# ── Reschedule ───────────────────────────────────────────────────────────────

@router.callback_query(RescheduleCallback.filter())
async def handle_reschedule(
    callback: CallbackQuery,
    callback_data: RescheduleCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not await _require_admin(callback, config):
        return

    post = await db.get_post_by_id(callback_data.post_db_id)
    if not post:
        await callback.answer("❌ Пост не найден", show_alert=True)
        return

    tg_msg_id = post.get("tg_message_id")
    if not tg_msg_id:
        await callback.answer("❌ Сообщение поста не найдено", show_alert=True)
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=config.GROUP_ID,
            message_id=tg_msg_id,
            reply_markup=get_schedule_keyboard(callback_data.post_db_id),
        )
        await callback.answer("Выберите новое время")
    except Exception as e:
        logger.warning(f"Could not show reschedule picker: {e}")
        await callback.answer("❌ Не удалось открыть пикер времени", show_alert=True)


# ── Autoqueue ─────────────────────────────────────────────────────────────────

@router.message(Command("autoqueue"), IsAdmin())
async def cmd_autoqueue(
    message: Message,
    db: Database,
    config: Config,
    scheduler: AsyncIOScheduler,
    bot: Bot,
):
    topic_id = message.message_thread_id
    if not topic_id:
        await message.reply("❌ Команда должна использоваться внутри топика сообщества.")
        return

    # Parse args: /autoqueue HH:MM interval_minutes
    args = (message.text or "").split()[1:]
    if len(args) < 2:
        await message.reply(
            "❌ Использование: <code>/autoqueue ЧЧ:ММ интервал_мин</code>\n"
            "Пример: <code>/autoqueue 09:00 60</code>",
            parse_mode="HTML",
        )
        return

    tz = config.tz
    now = datetime.now(tz)
    try:
        t = datetime.strptime(args[0], "%H:%M")
        start_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if start_time <= now:
            start_time += timedelta(days=1)
        interval_min = int(args[1])
        if interval_min <= 0:
            raise ValueError("interval must be positive")
    except (ValueError, IndexError):
        await message.reply(
            "❌ Неверный формат. Используйте: <code>/autoqueue ЧЧ:ММ интервал_мин</code>",
            parse_mode="HTML",
        )
        return

    community = await db.get_community_by_topic_id(topic_id)
    if not community:
        await message.reply("❌ Этот топик не привязан ни к одному сообществу.")
        return

    channel_id = community.get("channel_id", 0)
    if not channel_id:
        await message.reply("❌ Канал для этого сообщества не настроен.")
        return

    posts = await db.get_unscheduled_posts_for_topic(topic_id)
    if not posts:
        await message.reply("ℹ️ Нет постов без расписания в этом топике.")
        return

    # If there are already pending posts for this channel, start after the last one
    existing_pending = await db.get_pending_for_channel(channel_id)
    if existing_pending:
        last_ts = max(r["schedule_time"] for r in existing_pending)
        last_dt = datetime.fromtimestamp(last_ts, tz)
        candidate = last_dt + timedelta(minutes=interval_min)
        if candidate > start_time:
            start_time = candidate

    scheduled_lines = []
    current_time = start_time

    for post in posts:
        is_suggested = post["post_type"] == "suggested"
        try:
            record_id, unix_ts = await _schedule_post_core(
                db=db,
                scheduler=scheduler,
                post_db_id=post["id"],
                channel_id=channel_id,
                content_json=post["content_json"],
                is_suggested=is_suggested,
                schedule_time=current_time,
            )
            # Update the post's keyboard to show scheduled badge
            if post.get("tg_message_id"):
                await safe_call(
                    bot.edit_message_reply_markup(
                        chat_id=config.GROUP_ID,
                        message_id=post["tg_message_id"],
                        reply_markup=get_scheduled_badge(
                            post["id"], unix_ts, config.TIMEZONE, record_id=record_id
                        ),
                    ),
                    logger,
                    f"autoqueue: could not update keyboard for post {post['id']}",
                )

            text_preview = json.loads(post["content_json"]).get("text", "")[:40]
            time_str = current_time.strftime("%H:%M")
            scheduled_lines.append(f"• {time_str} — {text_preview}…" if text_preview else f"• {time_str}")
            current_time += timedelta(minutes=interval_min)

        except Exception as e:
            logger.error(f"autoqueue: failed to schedule post {post['id']}: {e}")
            scheduled_lines.append(f"• ❌ Ошибка: {post['id']}")

    # Refresh board once after bulk scheduling
    await safe_call(
        refresh_schedule_board(community["vk_id"]),
        logger,
        "autoqueue: could not refresh schedule board",
    )

    count = len(posts)
    summary = f"✅ Запланировано {count} постов:\n" + "\n".join(scheduled_lines)
    await message.reply(summary)
    logger.info(f"autoqueue: scheduled {count} posts for community {community['vk_id']}")
