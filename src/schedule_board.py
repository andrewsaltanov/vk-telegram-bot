"""
Schedule board: a silently-editable, pinned message showing the pending
publication queue for a community's channel. Mirrored into both the
published and suggested topics, since admins schedule posts from either.

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


# (topic id field, stored board-message-id field, DB setter method name) — same
# queue text is mirrored into whichever of these topics the community has.
_BOARD_TOPICS = (
    ("published_topic_id", "schedule_board_msg_id", "set_schedule_board_msg_id"),
    ("suggested_topic_id", "suggested_board_msg_id", "set_suggested_board_msg_id"),
)


async def refresh_schedule_board(community_vk_id: int) -> None:
    """Rebuild and pin the pending-publications board in every topic the community has."""
    if _bot is None or _db is None or _config is None:
        return

    community = await _db.get_community(community_vk_id)
    if not community:
        return

    pending = await _db.get_pending_for_channel(community["channel_id"])
    board_text = _build_board_text(pending)

    for topic_key, msg_id_key, setter_name in _BOARD_TOPICS:
        topic_id = community.get(topic_key)
        if not topic_id:
            continue
        await _refresh_board_message(
            community_vk_id=community_vk_id,
            topic_id=topic_id,
            board_msg_id=community.get(msg_id_key),
            board_text=board_text,
            save_msg_id=getattr(_db, setter_name),
        )


async def _refresh_board_message(
    community_vk_id: int,
    topic_id: int,
    board_msg_id: int | None,
    board_text: str,
    save_msg_id,
) -> None:
    """Edit (or create) and pin one board message. `save_msg_id` persists the
    message id under whichever DB column this topic's board uses."""
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
                await save_msg_id(community_vk_id, None)
                needs_recreate = True
            elif "message is not modified" in err:
                pass  # content already up to date — still (re-)pin below
            else:
                logger.warning(
                    f"Could not edit schedule board {board_msg_id} for community {community_vk_id}: {e}"
                )
                return
        except Exception as e:
            logger.warning(
                f"Could not edit schedule board {board_msg_id} for community {community_vk_id}: {e}"
            )
            return

        if not needs_recreate:
            await _pin_board_message(board_msg_id)
            return

    try:
        msg = await _bot.send_message(
            chat_id=_config.GROUP_ID,
            message_thread_id=topic_id,
            text=board_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        await save_msg_id(community_vk_id, msg.message_id)
        await _pin_board_message(msg.message_id)
    except Exception as e:
        logger.warning(
            f"Could not send schedule board to topic {topic_id} for community {community_vk_id}: {e}"
        )


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
