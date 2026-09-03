"""Admin-only commands: monitoring status, publication queue, mute controls, help."""
import json
import logging
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import CancelSchedCallback
from config import Config
from database import Database
from filters import IsAdmin

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("status"), IsAdmin())
async def cmd_status(message: Message, db: Database, config: Config):
    communities = await db.get_communities()
    if not communities:
        await message.reply("Сообщества не настроены.")
        return

    tz = config.tz
    week_ago = int((datetime.now(tz) - timedelta(days=7)).timestamp())

    # Build a map of community_id → next pending publication
    all_pending = await db.get_all_pending_with_community()

    lines = ["<b>Мониторинг сообществ:</b>"]
    for c in communities:
        community_id = c["vk_id"]
        suggested_count = await db.get_pending_suggested_count(community_id)
        published_week = await db.get_published_count_since(community_id, week_ago)

        # Find next pending pub for this community from all_pending
        community_pending = [
            r for r in all_pending
            if r["community_id"] == c["vk_id"]
        ]
        if community_pending:
            next_ts = community_pending[0]["schedule_time"]
            next_dt = datetime.fromtimestamp(next_ts, tz).strftime("%d.%m %H:%M")
            sched_str = f"{len(community_pending)} пост(ов), ближайший: {next_dt}"
        else:
            sched_str = "нет"

        lines.append(
            f"\n🔹 <b>{c['name']}</b>\n"
            f"   Публикации: топик #{c['published_topic_id']} | последний пост {c['last_post_id']}\n"
            f"   Предложки: топик #{c['suggested_topic_id']} | ожидает: {suggested_count}\n"
            f"   Запланировано: {sched_str}\n"
            f"   Опубликовано за 7 дней: {published_week}"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("queue"), IsAdmin())
async def cmd_queue(message: Message, db: Database, config: Config):
    records = await db.get_all_pending_with_community()
    if not records:
        await message.reply("📅 Запланированных публикаций нет.")
        return

    tz = config.tz
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


@router.message(Command("mute"), IsAdmin())
async def cmd_mute(message: Message, db: Database):
    await _set_mute(message, db, muted=True)


@router.message(Command("unmute"), IsAdmin())
async def cmd_unmute(message: Message, db: Database):
    await _set_mute(message, db, muted=False)


async def _set_mute(message: Message, db: Database, muted: bool):
    topic_id = message.message_thread_id
    action = "отключены" if muted else "включены"

    if topic_id:
        community = await db.get_community_by_topic_id(topic_id)
        if community:
            await db.set_notifications_muted(community["vk_id"], muted)
            await message.reply(
                f"{'🔕' if muted else '🔔'} Уведомления о публикации "
                f"<b>{action}</b> для «{community['name']}».",
                parse_mode="HTML",
            )
            return
        await message.reply("❌ Этот топик не привязан к сообществу.")
        return

    # General chat — show current states
    communities = await db.get_communities()
    lines = ["ℹ️ Не в топике сообщества. Текущие настройки:\n"]
    for c in communities:
        state = "🔕 выкл" if c.get("notifications_muted") else "🔔 вкл"
        lines.append(f"• {c['name']}: уведомления {state}")
    await message.reply("\n".join(lines))


@router.message(Command("help"), IsAdmin())
async def cmd_help(message: Message):
    await message.reply(
        "<b>Команды бота:</b>\n\n"
        "/status — состояние мониторинга\n"
        "/queue — очередь запланированных публикаций\n"
        "/autoqueue ЧЧ:ММ мин — массовое планирование постов из топика\n"
        "/mute — отключить уведомления о публикации (в топике)\n"
        "/unmute — включить уведомления о публикации (в топике)\n"
        "/cancel — отменить ожидание ввода кастомного времени\n"
        "/help — эта справка\n\n"
        "<b>Планирование поста:</b>\n"
        "Нажми кнопку под постом в топике — выбери время или введи своё.",
        parse_mode="HTML",
    )
