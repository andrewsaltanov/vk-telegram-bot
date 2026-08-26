"""
Formatting and sending VK posts to Telegram (aiogram 3.7).
"""
import asyncio
import html
import logging
import re
from typing import List

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InputMediaPhoto

from keyboards import get_manual_post_keyboard

logger = logging.getLogger(__name__)

MAX_CAPTION = 1024
MAX_TEXT = 4096

def _html_len(s: str) -> int:
    """Rendered character count Telegram uses for its limit (strips tags, decodes entities)."""
    return len(html.unescape(re.sub(r'<[^>]+>', '', s)))


BRAND_LINK = "https://ugnest.com/links"
BRAND_TEXT = "Уютное гнездышко – поиск жилья, жильцов и соседей без посредников"
SEP = "\n\n"


# ── Retry on flood control ────────────────────────────────────────────────────

async def _retry(coro_fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except TelegramRetryAfter as e:
            if attempt == max_retries:
                raise
            wait = e.retry_after + 1
            logger.warning(f"Flood control: waiting {wait}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)


# ── Caption builder ───────────────────────────────────────────────────────────

def build_caption(content: dict, limit: int = MAX_CAPTION, is_suggested: bool = False) -> str:
    """
    Build a Telegram HTML caption for a VK post.

    For suggested posts: footer shows a link to the author's VK page instead of 📌 Оригинал в VK.
    If text fits:   text + attachments + footer + brand
    If truncated:   text… + 🔍 Полный пост... + attachments + brand
                    (footer link omitted — 🔍 already links to the post)
    """
    post_link = content.get("post_link", "")
    author_link = content.get("author_link", "")

    brand = f'<a href="{html.escape(BRAND_LINK)}">{html.escape(BRAND_TEXT)}</a>'

    # footer_full: for suggested posts — link to author's profile; otherwise — link to original post
    if is_suggested and author_link:
        footer_full = f'👤 <a href="{html.escape(author_link)}">Ссылка на автора поста в VK</a>\n\n{brand}'
    elif post_link:
        footer_full = f'📌 <a href="{html.escape(post_link)}">Оригинал в VK</a>\n\n{brand}'
    else:
        footer_full = brand

    # footer_short omits the link when 🔍 already links there (used when text is truncated)
    footer_short = brand

    # --- attachments ---
    att = []
    for url in content.get("links", []):
        att.append(f"🔗 {html.escape(url)}")
    for doc in content.get("docs", []):
        att.append(f'📎 <a href="{html.escape(doc["url"])}">{html.escape(doc["title"])}</a>')
    for vid in content.get("videos", []):
        att.append(f'🎥 <a href="{html.escape(vid["url"])}">{html.escape(vid["title"])}</a>')
    att_str = "\n".join(att)

    plain_text = content.get("text", "").strip()
    raw_text = html.escape(plain_text)
    see_more = (
        f'🔍 <a href="{html.escape(post_link)}">Полный пост смотрите на стене в VK</a>'
        if post_link
        else "🔍 Полный пост смотрите на стене в VK"
    )

    def _join(*parts) -> str:
        return SEP.join(p for p in parts if p)

    # Try full text first (no truncation).
    # Use rendered length (Telegram counts after HTML parsing, not raw bytes).
    full = _join(raw_text, att_str, footer_full)
    if _html_len(full) <= limit:
        return full

    # Text is truncated — use footer_short (no 📌, since 🔍 already has the link)
    overhead = _html_len(_join(see_more, att_str, footer_short))
    available = limit - overhead - len(SEP) if raw_text else 0

    if available > 30:
        # Truncate unescaped text so slicing doesn't break HTML entities
        truncated = html.escape(plain_text[:available - 1]) + "…"
        return _join(truncated, see_more, att_str, footer_short)

    # No room for text at all — just show see_more
    return _join(see_more, att_str, footer_short)


# ── Send to group topic (preview + schedule buttons) ─────────────────────────

async def send_vk_post_to_topic(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    content: dict,
    keyboard=None,
    is_suggested: bool = False,
    post_db_id: int = 0,
) -> List[int]:
    photos = content.get("photos", [])
    message_ids: List[int] = []

    # Detect if the full text doesn't fit in the photo caption limit (1024 chars).
    # If so, we send media as a separate album and the full text as a text message.
    is_oversized = False
    if photos:
        full_caption = build_caption(content, limit=999_999, is_suggested=is_suggested)
        is_oversized = len(full_caption) > MAX_CAPTION

    try:
        if is_oversized:
            # ── Oversized post: album + full text + manual-posting warning ──

            # 1. Collect all media: photos + video thumbnails
            media_urls = list(photos[:10])
            for vid in content.get("videos", []):
                if vid.get("thumb") and len(media_urls) < 10:
                    media_urls.append(vid["thumb"])

            album_anchor_id: int | None = None
            if media_urls:
                media = [InputMediaPhoto(media=url) for url in media_urls]
                msgs = await _retry(lambda: bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    message_thread_id=thread_id,
                ))
                message_ids.extend(m.message_id for m in msgs)
                album_anchor_id = msgs[0].message_id

            # 2. Full text as separate message (up to 4096 chars, not truncated in caption)
            text_caption = build_caption(content, limit=MAX_TEXT, is_suggested=is_suggested)
            text_kwargs = dict(
                chat_id=chat_id,
                text=text_caption,
                parse_mode="HTML",
                message_thread_id=thread_id,
                disable_web_page_preview=True,
            )
            if album_anchor_id:
                text_kwargs["reply_to_message_id"] = album_anchor_id
            text_msg = await _retry(lambda: bot.send_message(**text_kwargs))
            message_ids.append(text_msg.message_id)

            # 3. Keyboard with manual-posting warning
            # For oversized posts use a dedicated keyboard that includes "Размещён вручную" button
            oversized_keyboard = get_manual_post_keyboard(post_db_id) if post_db_id else keyboard
            if oversized_keyboard:
                btn_msg = await _retry(lambda: bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ <b>Пост слишком длинный для автоматической публикации.</b>\n"
                        "Разместите его в канале вручную.\n\n"
                        "📋 Действия с постом:"
                    ),
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                    reply_markup=oversized_keyboard,
                    reply_to_message_id=text_msg.message_id,
                ))
                message_ids.insert(0, btn_msg.message_id)

        elif photos:
            # ── Normal photo post ──
            caption = build_caption(content, limit=MAX_CAPTION, is_suggested=is_suggested)
            if len(photos) == 1:
                msg = await _retry(lambda: bot.send_photo(
                    chat_id=chat_id,
                    photo=photos[0],
                    caption=caption,
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                    reply_markup=keyboard,
                ))
                message_ids.append(msg.message_id)
            else:
                media = [InputMediaPhoto(media=url) for url in photos[:10]]
                media[0].caption = caption
                media[0].parse_mode = "HTML"
                msgs = await _retry(lambda: bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    message_thread_id=thread_id,
                ))
                message_ids.extend(m.message_id for m in msgs)
                if keyboard:
                    btn_msg = await _retry(lambda: bot.send_message(
                        chat_id=chat_id,
                        text="📋 Действия с постом:",
                        message_thread_id=thread_id,
                        reply_markup=keyboard,
                        reply_to_message_id=msgs[0].message_id,
                    ))
                    message_ids.insert(0, btn_msg.message_id)
        else:
            # ── Text-only post ──
            caption = build_caption(content, limit=MAX_TEXT, is_suggested=is_suggested)
            msg = await _retry(lambda: bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                message_thread_id=thread_id,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            ))
            message_ids.append(msg.message_id)
    except TelegramBadRequest as e:
        if "thread" in str(e).lower():
            raise  # caller handles missing topic
        logger.error(f"Error sending post to topic {thread_id}: {e}")
    except Exception as e:
        logger.error(f"Error sending post to topic {thread_id}: {e}")

    return message_ids


# ── Send to channel (called by APScheduler job) ───────────────────────────────

async def send_post_to_channel(
    bot: Bot,
    channel_id: int,
    content: dict,
    is_suggested: bool = False,
) -> List[int]:
    photos = content.get("photos", [])
    lim = MAX_CAPTION if photos else MAX_TEXT
    caption = build_caption(content, limit=lim, is_suggested=is_suggested)
    message_ids: List[int] = []

    try:
        if photos:
            if len(photos) == 1:
                msg = await _retry(lambda: bot.send_photo(
                    chat_id=channel_id,
                    photo=photos[0],
                    caption=caption,
                    parse_mode="HTML",
                ))
                message_ids.append(msg.message_id)
            else:
                # Album — caption on first photo
                media = [InputMediaPhoto(media=url) for url in photos[:10]]
                media[0].caption = caption
                media[0].parse_mode = "HTML"
                msgs = await _retry(lambda: bot.send_media_group(
                    chat_id=channel_id,
                    media=media,
                ))
                message_ids.extend(m.message_id for m in msgs)
        else:
            msg = await _retry(lambda: bot.send_message(
                chat_id=channel_id,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
            ))
            message_ids.append(msg.message_id)
    except Exception as e:
        logger.error(f"Error sending post to channel {channel_id}: {e}")
        raise

    return message_ids
