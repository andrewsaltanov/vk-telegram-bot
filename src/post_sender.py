"""
Formatting and sending VK posts to Telegram (aiogram 3.7).
"""
import asyncio
import html
import logging
import re
from typing import List, Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto

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


# ── Retry on flood control / transient media-fetch failures ─────────────────

# Telegram fetches media URLs itself; a burst of several photo fetches in one
# sendMediaGroup call can trip VK CDN's own rate-limiting even though every
# URL is independently reachable — a short backoff before retrying the exact
# same request often succeeds once that burst has passed.
WEBPAGE_FETCH_RETRY_DELAYS = (5, 10, 20)


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
        except TelegramBadRequest as e:
            if "webpage_curl_failed" not in str(e).lower() or attempt >= len(WEBPAGE_FETCH_RETRY_DELAYS):
                raise
            wait = WEBPAGE_FETCH_RETRY_DELAYS[attempt]
            logger.warning(
                f"WEBPAGE_CURL_FAILED, retrying in {wait}s (attempt {attempt + 1}/{len(WEBPAGE_FETCH_RETRY_DELAYS)})"
            )
            await asyncio.sleep(wait)


# ── Photo download (upload bytes ourselves instead of letting Telegram fetch) ─

PHOTO_DOWNLOAD_TIMEOUT = 20


async def _download_photos(urls: List[str]) -> List[BufferedInputFile]:
    """
    Download each photo ourselves and hand Telegram the bytes, instead of
    passing the URL for Telegram's server to fetch. VK's CDN intermittently
    refuses Telegram's own fetcher for a given edge node (WEBPAGE_CURL_FAILED)
    even though the same URL is reliably reachable from here — downloading
    ourselves sidesteps that entirely. Photos that fail to download are
    skipped rather than failing the whole post.
    """
    async def fetch(session: aiohttp.ClientSession, index: int, url: str) -> Optional[BufferedInputFile]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Photo download got HTTP {resp.status}: {url}")
                    return None
                data = await resp.read()
                return BufferedInputFile(data, filename=f"photo{index}.jpg")
        except Exception as e:
            logger.warning(f"Could not download photo, skipping: {url} ({e})")
            return None

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=PHOTO_DOWNLOAD_TIMEOUT)) as session:
        files = await asyncio.gather(*(fetch(session, i, url) for i, url in enumerate(urls)))
    return [f for f in files if f is not None]


# ── Caption builder ───────────────────────────────────────────────────────────

COMMENTS_NOTE = "💬 Продолжение описания — в комментариях"


def _build_attachments(content: dict) -> str:
    att = []
    for url in content.get("links", []):
        att.append(f"🔗 {html.escape(url)}")
    for doc in content.get("docs", []):
        att.append(f'📎 <a href="{html.escape(doc["url"])}">{html.escape(doc["title"])}</a>')
    for vid in content.get("videos", []):
        att.append(f'🎥 <a href="{html.escape(vid["url"])}">{html.escape(vid["title"])}</a>')
    return "\n".join(att)


def _join(*parts) -> str:
    return SEP.join(p for p in parts if p)


def build_caption(content: dict, limit: int = MAX_CAPTION, is_suggested: bool = False) -> str:
    """
    Build a Telegram HTML caption for a VK post.

    For suggested posts: footer shows a link to the author's VK page instead of 📌 Оригинал в VK.
    If text fits:   text + attachments + footer + brand
    If truncated:   text… + 🔍 Полный пост... + attachments + brand
                    (footer link omitted — 🔍 already links to the post)

    The 🔍 "see more" link for suggested posts points at the author's profile
    rather than post_link: post_link is the post's URL in VK's suggestion
    queue, which stops resolving as soon as the post leaves that queue
    (approved or withdrawn) — often within minutes.
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

    att_str = _build_attachments(content)

    plain_text = content.get("text", "").strip()
    raw_text = html.escape(plain_text)
    see_more_link = author_link if (is_suggested and author_link) else post_link
    see_more = (
        f'🔍 <a href="{html.escape(see_more_link)}">Полный пост смотрите на стене в VK</a>'
        if see_more_link
        else "🔍 Полный пост смотрите на стене в VK"
    )

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


def build_channel_caption(
    content: dict, limit: int, is_suggested: bool
) -> tuple[str, Optional[str]]:
    """
    Caption for the public channel post. For suggested posts whose text
    doesn't fit `limit`, truncates without a VK link at all — post_link
    (the suggestion-queue URL) is dead by the time this is published, and
    unlike the admin-topic preview there's no author profile worth sending
    people to on the public channel — and returns the untruncated remainder
    so the caller can post it as a channel comment instead. Everything else
    behaves exactly like build_caption().
    """
    if not is_suggested:
        return build_caption(content, limit=limit, is_suggested=is_suggested), None

    brand = f'<a href="{html.escape(BRAND_LINK)}">{html.escape(BRAND_TEXT)}</a>'
    author_link = content.get("author_link", "")
    footer_full = (
        f'👤 <a href="{html.escape(author_link)}">Ссылка на автора поста в VK</a>\n\n{brand}'
        if author_link
        else brand
    )
    att_str = _build_attachments(content)

    plain_text = content.get("text", "").strip()
    raw_text = html.escape(plain_text)

    full = _join(raw_text, att_str, footer_full)
    if _html_len(full) <= limit:
        return full, None

    overhead = _html_len(_join(COMMENTS_NOTE, att_str, brand))
    available = limit - overhead - len(SEP) if raw_text else 0

    if available > 30:
        truncated = html.escape(plain_text[:available - 1]) + "…"
        caption = _join(truncated, COMMENTS_NOTE, att_str, brand)
        remainder = plain_text[available - 1:].strip()
    else:
        caption = _join(COMMENTS_NOTE, att_str, brand)
        remainder = plain_text

    return caption, (remainder or None)


def build_continuation_messages(remainder_text: str, limit: int = MAX_TEXT) -> List[str]:
    """
    Split the untruncated remainder of a post into <=`limit`-char HTML
    messages for posting as channel comments. The brand link is appended to
    the last chunk (its own message if it doesn't fit alongside the text).
    """
    text = remainder_text.strip()
    brand = f'<a href="{html.escape(BRAND_LINK)}">{html.escape(BRAND_TEXT)}</a>'
    if not text:
        return [brand]

    raw_chunks: List[str] = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        raw_chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    raw_chunks.append(text)

    messages = [html.escape(c) for c in raw_chunks]
    if _html_len(messages[-1]) + _html_len(SEP + brand) <= limit:
        messages[-1] = _join(messages[-1], brand)
    else:
        messages.append(brand)
    return messages


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
                files = await _download_photos(media_urls)
                if files:
                    try:
                        media = [InputMediaPhoto(media=f) for f in files]
                        msgs = await _retry(lambda: bot.send_media_group(
                            chat_id=chat_id,
                            media=media,
                            message_thread_id=thread_id,
                        ))
                        message_ids.extend(m.message_id for m in msgs)
                        album_anchor_id = msgs[0].message_id
                    except Exception as album_err:
                        # Don't lose the post over a failed album — still send the text below.
                        logger.warning(f"Could not send album for topic {thread_id}, skipping photos: {album_err}")
                else:
                    logger.warning(f"Could not download any photos for topic {thread_id}, skipping photos")

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
            photos_sent = False
            files = await _download_photos(photos[:10])
            if files:
                try:
                    if len(files) == 1:
                        msg = await _retry(lambda: bot.send_photo(
                            chat_id=chat_id,
                            photo=files[0],
                            caption=caption,
                            parse_mode="HTML",
                            message_thread_id=thread_id,
                            reply_markup=keyboard,
                        ))
                        message_ids.append(msg.message_id)
                    else:
                        media = [InputMediaPhoto(media=f) for f in files]
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
                    photos_sent = True
                except Exception as photo_err:
                    # Fall back to text-only so the post (and a link back to VK for the
                    # photos) isn't lost entirely.
                    logger.warning(
                        f"Could not send photo(s) for topic {thread_id}, falling back to text-only: {photo_err}"
                    )
                    message_ids.clear()
            else:
                logger.warning(f"Could not download any photos for topic {thread_id}, falling back to text-only")

            if not photos_sent:
                text_caption = build_caption(content, limit=MAX_TEXT, is_suggested=is_suggested)
                text_msg = await _retry(lambda: bot.send_message(
                    chat_id=chat_id,
                    text=text_caption,
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                    disable_web_page_preview=True,
                ))
                message_ids.append(text_msg.message_id)

                manual_keyboard = get_manual_post_keyboard(post_db_id) if post_db_id else keyboard
                if manual_keyboard:
                    btn_msg = await _retry(lambda: bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ <b>Не удалось загрузить фото поста.</b>\n"
                            "Проверьте оригинал в VK и разместите вручную при необходимости.\n\n"
                            "📋 Действия с постом:"
                        ),
                        parse_mode="HTML",
                        message_thread_id=thread_id,
                        reply_markup=manual_keyboard,
                        reply_to_message_id=text_msg.message_id,
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
) -> tuple[List[int], Optional[str]]:
    """
    Returns (message_ids, continuation_text). continuation_text is set when a
    suggested post's caption had to be truncated — the caller is expected to
    post it as a channel comment (see channel_comments.py).
    """
    photos = content.get("photos", [])
    message_ids: List[int] = []
    continuation: Optional[str] = None

    try:
        photos_sent = False
        if photos:
            caption, continuation = build_channel_caption(
                content, limit=MAX_CAPTION, is_suggested=is_suggested
            )
            files = await _download_photos(photos[:10])
            if files:
                try:
                    if len(files) == 1:
                        msg = await _retry(lambda: bot.send_photo(
                            chat_id=channel_id,
                            photo=files[0],
                            caption=caption,
                            parse_mode="HTML",
                        ))
                        message_ids.append(msg.message_id)
                    else:
                        # Album — caption on first photo
                        media = [InputMediaPhoto(media=f) for f in files]
                        media[0].caption = caption
                        media[0].parse_mode = "HTML"
                        msgs = await _retry(lambda: bot.send_media_group(
                            chat_id=channel_id,
                            media=media,
                        ))
                        message_ids.extend(m.message_id for m in msgs)
                    photos_sent = True
                except Exception as photo_err:
                    # Fall back to text-only rather than silently losing a paid
                    # channel publication.
                    logger.warning(
                        f"Could not send photo(s) to channel {channel_id}, falling back to text-only: {photo_err}"
                    )
                    message_ids.clear()
                    continuation = None  # this caption/continuation pair never got sent
            else:
                logger.warning(f"Could not download any photos for channel {channel_id}, falling back to text-only")
                continuation = None

        if not photos or not photos_sent:
            text_caption, continuation = build_channel_caption(
                content, limit=MAX_TEXT, is_suggested=is_suggested
            )
            msg = await _retry(lambda: bot.send_message(
                chat_id=channel_id,
                text=text_caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
            ))
            message_ids.append(msg.message_id)
    except Exception as e:
        logger.error(f"Error sending post to channel {channel_id}: {e}")
        raise

    return message_ids, continuation
