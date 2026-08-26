"""
Schedule board: a single silently-editable message per community topic that
shows the pending publication queue for that community's channel.

Injected via init() from main.py to avoid circular imports.
"""
import html
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_db = None
_config = None


def init(bot, db, config):
    global _bot, _db, _config
    _bot, _db, _config = bot, db, config


async def refresh_schedule_board(community_vk_id: int) -> None:
    """Rebuild and edit (or create) the schedule board message in the community's published topic."""
    if _bot is None or _db is None or _config is None:
        return

    community = await _db.get_community(community_vk_id)
    if not community or not community.get("published_topic_id"):
        return

    pending = await _db.get_pending_for_channel(community["channel_id"])
    board_text = _build_board_text(pending)

    board_msg_id = community.get("schedule_board_msg_id")
    if board_msg_id:
        try:
            await _bot.edit_message_text(
                chat_id=_config.GROUP_ID,
                message_id=board_msg_id,
                text=board_text,
                parse_mode="HTML",
            )
            return
        except TelegramBadRequest as e:
            if "message to edit not found" in str(e).lower() or "message_id_invalid" in str(e).lower():
                logger.info(f"Board message {board_msg_id} was deleted — will recreate.")
                await _db.set_schedule_board_msg_id(community_vk_id, None)
            else:
                logger.warning(f"Could not edit schedule board for community {community_vk_id}: {e}")
                return
        except Exception as e:
            logger.warning(f"Could not edit schedule board for community {community_vk_id}: {e}")
            return

    try:
        msg = await _bot.send_message(
            chat_id=_config.GROUP_ID,
            message_thread_id=community["published_topic_id"],
            text=board_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        await _db.set_schedule_board_msg_id(community_vk_id, msg.message_id)
    except Exception as e:
        logger.warning(f"Could not send schedule board for community {community_vk_id}: {e}")


def _build_board_text(pending: list) -> str:
    if not pending:
        return "📅 Запланированных публикаций нет."

    tz = ZoneInfo(_config.TIMEZONE)
    lines = ["📅 <b>Очередь публикаций в канале:</b>"]
    for r in pending:
        t = datetime.fromtimestamp(r["schedule_time"], tz).strftime("%d.%m %H:%M")
        try:
            text = json.loads(r["content_json"]).get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            text = ""
        preview = html.escape(text[:50]) + "…" if text else ""
        lines.append(f"• {t}" + (f" — {preview}" if preview else ""))
    return "\n".join(lines)
