"""
Telegram bot handlers: schedule callbacks, FSM for custom time, admin commands.
Uses APScheduler (aiogram 3.7) — no Telegram-side scheduling.
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import (
    CancelSchedCallback,
    DelChannelCallback,
    ManualDoneCallback,
    ManualInfoCallback,
    RescheduleCallback,
    ScheduleCallback,
    SchedInfoCallback,
)
from config import Config
from database import Database
from keyboards import get_schedule_keyboard, get_scheduled_badge, get_manually_placed_badge
from schedule_board import refresh_schedule_board
from scheduler import execute_scheduled_post

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
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
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
        )
        await callback.answer()
        return

    minutes = int(option)
    tz = ZoneInfo(config.TIMEZONE)
    # minutes=0 means "now" → delay 30 seconds
    delay = timedelta(seconds=30) if minutes == 0 else timedelta(minutes=minutes)
    schedule_time = datetime.now(tz) + delay
    await _do_schedule(callback, db, scheduler, post_db_id, schedule_time, bot, config)


@router.message(ScheduleStates.waiting_custom_time)
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
    tz = ZoneInfo(config.TIMEZONE)
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

async def _notify(event: CallbackQuery | Message, text: str, alert: bool = False):
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=alert)
    else:
        await event.reply(text)


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
        tz = ZoneInfo(config.TIMEZONE)
        prev_time = datetime.fromtimestamp(prev["schedule_time"], tz).strftime("%d.%m.%Y %H:%M")
        prev_text = json.loads(prev["orig_content_json"]).get("text", "")
        curr_text = json.loads(post["content_json"]).get("text", "")
        warn_lines = [f"⚠️ Этот пост уже публиковался в канале {prev_time}."]
        if prev_text.strip() != curr_text.strip():
            warn_lines.append("📝 Текст изменился с момента публикации.")
        try:
            await bot.send_message(
                chat_id=config.GROUP_ID,
                text="\n".join(warn_lines),
                message_thread_id=post.get("tg_topic_id"),
                reply_to_message_id=post.get("tg_message_id"),
            )
        except Exception as warn_err:
            logger.warning(f"Could not send duplicate warning: {warn_err}")
        # Do NOT return — allow rescheduling

    is_suggested = post["post_type"] == "suggested"

    # Cancel any existing pending schedule for this post (prevents double-publish)
    existing_pending = await db.get_pending_scheduled_for_post(post_db_id)

    # Warn if another post for the same channel is already scheduled within ±30 min
    nearby = await db.get_pending_for_channel_near_time(channel_id, int(schedule_time.timestamp()))
    existing_ids = {ep["id"] for ep in existing_pending}
    conflicts = [c for c in nearby if c["id"] not in existing_ids]
    if conflicts:
        tz = ZoneInfo(config.TIMEZONE)
        times = ", ".join(
            datetime.fromtimestamp(c["schedule_time"], tz).strftime("%H:%M")
            for c in conflicts
        )
        try:
            await bot.send_message(
                chat_id=config.GROUP_ID,
                text=f"⚠️ Рядом уже запланировано в этом канале: {times} (±30 мин)",
                message_thread_id=post.get("tg_topic_id"),
            )
        except Exception as conflict_err:
            logger.warning(f"Could not send conflict warning: {conflict_err}")

    for ep in existing_pending:
        job_id = ep.get("job_id")
        if job_id:
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        await db.mark_scheduled_post_cancelled(ep["id"])

    try:
        # 1. Persist to DB so the job survives restarts
        record_id = await db.save_scheduled_post_record(
            post_id=post_db_id,
            channel_id=channel_id,
            content_json=post["content_json"],
            is_suggested=is_suggested,
            schedule_time=int(schedule_time.timestamp()),
        )

        # 2. Add job to in-memory APScheduler
        job_id = f"sched_{record_id}"
        scheduler.add_job(
            execute_scheduled_post,
            trigger="date",
            run_date=schedule_time,
            args=[record_id],
            id=job_id,
            replace_existing=True,
        )

        # 3. Save job_id back to DB (for cancellation)
        await db.update_scheduled_post_job_id(record_id, job_id)

        unix_ts = int(schedule_time.timestamp())
        time_str = schedule_time.strftime("%d.%m.%Y %H:%M")
        await _notify(event, f"✅ Пост запланирован на {time_str}", alert=True)
        logger.info(f"Scheduled post {post_db_id} → channel {channel_id} at {time_str}")

        # 4. Replace schedule keyboard with a "scheduled" badge
        tg_msg_id = post.get("tg_message_id")
        topic_id = post.get("tg_topic_id")
        if tg_msg_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=config.GROUP_ID,
                    message_id=tg_msg_id,
                    reply_markup=get_scheduled_badge(post_db_id, unix_ts, config.TIMEZONE, record_id=record_id),
                )
            except Exception as edit_err:
                logger.warning(f"Could not update keyboard for post {post_db_id}: {edit_err}")

        # 5. Send reply marker in the topic
        try:
            await bot.send_message(
                chat_id=config.GROUP_ID,
                text=f"✅ Запланировано на {time_str}",
                message_thread_id=topic_id,
                reply_to_message_id=tg_msg_id,
            )
        except Exception as reply_err:
            logger.warning(f"Could not send schedule marker for post {post_db_id}: {reply_err}")

        # 6. Update the schedule board in the topic
        if community:
            try:
                await refresh_schedule_board(community["vk_id"])
            except Exception as board_err:
                logger.warning(f"Could not refresh schedule board: {board_err}")

    except Exception as e:
        logger.error(f"Schedule error: {e}")
        await _notify(event, f"❌ Ошибка планирования: {e}", alert=True)


# ── Manual placement callbacks ────────────────────────────────────────────────

@router.callback_query(ManualDoneCallback.filter())
async def handle_manual_done(
    callback: CallbackQuery,
    callback_data: ManualDoneCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    post = await db.get_post_by_id(callback_data.post_db_id)
    if not post:
        await callback.answer("❌ Пост не найден", show_alert=True)
        return

    tz = ZoneInfo(config.TIMEZONE)
    unix_ts = int(datetime.now(tz).timestamp())
    time_str = datetime.fromtimestamp(unix_ts, tz=tz).strftime("%d.%m.%Y %H:%M")

    await callback.answer(f"✅ Отмечен как размещённый вручную {time_str}", show_alert=True)

    tg_msg_id = post.get("tg_message_id")
    topic_id = post.get("tg_topic_id")

    if tg_msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=config.GROUP_ID,
                message_id=tg_msg_id,
                reply_markup=get_manually_placed_badge(callback_data.post_db_id, unix_ts, config.TIMEZONE),
            )
        except Exception as e:
            logger.warning(f"Could not update keyboard for post {callback_data.post_db_id}: {e}")

    try:
        await bot.send_message(
            chat_id=config.GROUP_ID,
            text=f"✅ Размещён вручную {time_str}",
            message_thread_id=topic_id,
            reply_to_message_id=tg_msg_id,
        )
    except Exception as e:
        logger.warning(f"Could not send manual marker for post {callback_data.post_db_id}: {e}")


@router.callback_query(ManualInfoCallback.filter())
async def handle_manual_info(
    callback: CallbackQuery,
    callback_data: ManualInfoCallback,
    config: Config,
):
    tz = ZoneInfo(config.TIMEZONE)
    dt = datetime.fromtimestamp(callback_data.unix_ts, tz=tz)
    time_str = dt.strftime("%d.%m.%Y %H:%M")
    await callback.answer(
        f"Размещён вручную {time_str} ({config.TIMEZONE})",
        show_alert=True,
    )


# ── Scheduled badge callbacks ─────────────────────────────────────────────────

@router.callback_query(SchedInfoCallback.filter())
async def handle_scheduled_info(
    callback: CallbackQuery,
    callback_data: SchedInfoCallback,
    config: Config,
):
    tz = ZoneInfo(config.TIMEZONE)
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
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
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
        try:
            await refresh_schedule_board(post["community_id"])
        except Exception as board_err:
            logger.warning(f"Could not refresh schedule board after cancel: {board_err}")


# ── Reschedule ───────────────────────────────────────────────────────────────

@router.callback_query(RescheduleCallback.filter())
async def handle_reschedule(
    callback: CallbackQuery,
    callback_data: RescheduleCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
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


# ── Delete from channel ───────────────────────────────────────────────────────

@router.callback_query(DelChannelCallback.filter())
async def handle_del_channel(
    callback: CallbackQuery,
    callback_data: DelChannelCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    record = await db.get_scheduled_post_record(callback_data.record_id)
    if not record:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    channel_msg_id = record.get("channel_message_id")
    if channel_msg_id:
        try:
            await bot.delete_message(
                chat_id=record["channel_id"],
                message_id=channel_msg_id,
            )
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete channel message: {e}")
    await callback.message.edit_text("🗑 Удалено из канала", reply_markup=None)
    await callback.answer("Удалено")


# ── Admin commands ─────────────────────────────────────────────────────────────

def _is_admin(user_id: int, config: Config) -> bool:
    # Empty ADMIN_IDS = unrestricted (open bot). Set ADMIN_IDS in .env to restrict.
    return not config.ADMIN_IDS or user_id in config.ADMIN_IDS


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    communities = await db.get_communities()
    if not communities:
        await message.reply("Сообщества не настроены.")
        return
    lines = ["<b>Мониторинг сообществ:</b>"]
    for c in communities:
        lines.append(
            f"\n🔹 <b>{c['name']}</b>\n"
            f"   Публикации: топик #{c['published_topic_id']}, "
            f"последний пост {c['last_post_id']}\n"
            f"   Предложки: топик #{c['suggested_topic_id']}, "
            f"последний {c['last_suggest_id']}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("queue"))
async def cmd_queue(message: Message, db: Database, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    records = await db.get_all_pending_with_community()
    if not records:
        await message.reply("📅 Запланированных публикаций нет.")
        return

    tz = ZoneInfo(config.TIMEZONE)
    builder = InlineKeyboardBuilder()
    lines = ["📅 <b>Запланированные публикации:</b>\n"]
    for r in records:
        t = datetime.fromtimestamp(r["schedule_time"], tz).strftime("%d.%m %H:%M")
        try:
            text = json.loads(r["content_json"]).get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            text = ""
        preview = text[:50] + ("…" if len(text) > 50 else "")
        community = r["community_name"]
        lines.append(f"• <b>{community}</b> — {t}")
        if preview:
            lines.append(f"  {preview}")
        builder.row(
            InlineKeyboardButton(
                text=f"❌ Отменить {t}",
                callback_data=CancelSchedCallback(record_id=r["id"]).pack(),
            )
        )

    await message.reply(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    await message.reply(
        "<b>Команды бота:</b>\n\n"
        "/status — состояние мониторинга\n"
        "/queue — очередь запланированных публикаций\n"
        "/help — эта справка\n\n"
        "<b>Планирование поста:</b>\n"
        "Нажми кнопку под постом в топике — выбери время или введи своё.",
        parse_mode="HTML",
    )
