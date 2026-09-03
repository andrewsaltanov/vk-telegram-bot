"""
Posts the untruncated remainder of a long suggested-post caption as a
channel comment (a reply inside the channel's linked discussion group)
instead of the dead VK suggestion-queue link — see
post_sender.build_channel_caption().

Telegram automatically mirrors every channel post into its linked
discussion group; we wait for that mirror to arrive (matched via
handle_forwarded_channel_post, wired up in handlers/comments.py) and reply
to it. If the mirror doesn't show up within PENDING_TIMEOUT_SECONDS —
comments disabled after the fact, bot removed from the group —
flush_stale_continuations() posts the continuation standalone instead of
losing it.
"""
import logging
import time
from typing import Optional

from aiogram import Bot
from aiogram.types import Message, MessageOriginChannel

from config import Config
from database import Database
from post_sender import build_continuation_messages
from tg_utils import safe_call

logger = logging.getLogger(__name__)

PENDING_TIMEOUT_SECONDS = 15 * 60

# channel_id -> linked discussion group chat_id
_linked_chat_cache: dict[int, int] = {}


async def _get_linked_chat_id(bot: Bot, channel_id: int) -> Optional[int]:
    if channel_id in _linked_chat_cache:
        return _linked_chat_cache[channel_id]
    chat = await safe_call(
        bot.get_chat(channel_id), logger, f"Could not fetch chat {channel_id}"
    )
    linked_id = getattr(chat, "linked_chat_id", None) if chat else None
    if linked_id:
        _linked_chat_cache[channel_id] = linked_id
    return linked_id


async def queue_continuation(
    db: Database, channel_id: int, channel_msg_id: int, continuation_text: str
):
    """Call right after a long suggested post is published to the channel."""
    await db.create_pending_continuation(channel_id, channel_msg_id, continuation_text)


def _forward_origin(message: Message) -> Optional[tuple[int, int]]:
    """(channel_id, original_message_id) for a channel post auto-forwarded
    into its discussion group, or None for anything else."""
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, origin.message_id
    if message.forward_from_chat and message.forward_from_message_id:
        return message.forward_from_chat.id, message.forward_from_message_id
    return None


async def handle_forwarded_channel_post(message: Message, db: Database):
    """No-op unless this forward matches a continuation we're waiting on."""
    if not message.is_automatic_forward:
        return
    origin = _forward_origin(message)
    if origin is None:
        return
    channel_id, channel_msg_id = origin

    pending = await db.get_pending_continuation(channel_id, channel_msg_id)
    if not pending:
        return

    await _send_continuation(message.bot, message.chat.id, message.message_id, pending, db)


async def _send_continuation(
    bot: Bot, chat_id: int, reply_to_message_id: Optional[int], pending: dict, db: Database
):
    chunks = build_continuation_messages(pending["continuation_text"])
    for chunk in chunks:
        msg = await safe_call(
            bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
            ),
            logger,
            f"Could not send continuation comment to {chat_id}",
        )
        if msg is None:
            logger.warning(
                f"Continuation {pending['id']} failed mid-send, will retry via timeout flush"
            )
            return
        reply_to_message_id = None  # only the first chunk replies to the post

    await db.mark_continuation_sent(pending["id"])


async def flush_stale_continuations(bot: Bot, db: Database, config: Config):
    """Called once per VK poll cycle: fallback-post any continuation whose
    discussion-group mirror never showed up in time."""
    cutoff = int(time.time()) - PENDING_TIMEOUT_SECONDS
    for row in await db.get_stale_pending_continuations(cutoff):
        linked_chat_id = await _get_linked_chat_id(bot, row["channel_id"])
        if not linked_chat_id:
            logger.warning(
                f"Continuation {row['id']}: no linked discussion group for channel "
                f"{row['channel_id']}, marking failed"
            )
            await db.mark_continuation_failed(row["id"])
            await _notify_continuation_failed(bot, db, config, row["channel_id"])
            continue
        await _send_continuation(bot, linked_chat_id, None, row, db)


async def _notify_continuation_failed(bot: Bot, db: Database, config: Config, channel_id: int):
    community = await db.get_community_by_channel_id(channel_id)
    if not community or not community.get("published_topic_id"):
        return
    await safe_call(
        bot.send_message(
            chat_id=config.GROUP_ID,
            text=(
                "⚠️ Не удалось опубликовать продолжение комментария к посту — "
                f"проверьте обсуждения канала «{community.get('name', channel_id)}»"
            ),
            message_thread_id=community["published_topic_id"],
        ),
        logger,
        f"Could not send continuation-failure notice for channel {channel_id}",
    )
