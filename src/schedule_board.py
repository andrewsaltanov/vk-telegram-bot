"""
Schedule board: a single silently-editable message per community topic that
shows the pending publication queue for that community's channel.

Injected via init() from main.py to avoid circular imports.
"""
import html
import json
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from tg_utils import safe_call

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
        needs_recreate = False
        try:
            await _bot.edit_message_text(
                chat_id=_config.GROUP_ID,
                message_id=board_msg_id,
                text=board_text,
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message_id_invalid" in err:
                logger.info(f"Board message {board_msg_id} was deleted — will recreate.")
                await _db.set_schedule_board_msg_id(community_vk_id, None)
                needs_recreate = True
            elif "message is not modified" in err:
                pass  # content already up to date — still (re-)pin below
            else:
                logger.warning(f"Could not edit schedule board for community {community_vk_id}: {e}")
                return
        except Exception as e:
            logger.warning(f"Could not edit schedule board for community {community_vk_id}: {e}")
            return

        if not needs_recreate:
            await _pin_board_message(board_msg_id)
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
        await _pin_board_message(msg.message_id)
    except Exception as e:
        logger.warning(f"Could not send schedule board for community {community_vk_id}: {e}")


async def _pin_board_message(message_id: int) -> None:
    """Pin the board message in its topic — re-pinning an already-pinned message is a no-op,
    so this is safe to call on every refresh (covers admins accidentally unpinning it)."""
    await safe_call(
        _bot.pin_chat_message(
            chat_id=_config.GROUP_ID,
            message_id=message_id,
            disable_notification=True,
        ),
        logger,
        f"Could not pin schedule board message {message_id}",
    )


def _build_board_text(pending: list) -> str:
    if not pending:
        return "📅 Запланированных публикаций нет."

    tz = _config.tz
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
