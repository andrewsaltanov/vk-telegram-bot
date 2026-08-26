"""
Shared helper for Telegram API calls that are allowed to fail silently
(edit a keyboard, send a marker message, refresh a board) — log a warning
and move on instead of repeating try/except at every call site.
"""
import logging
from typing import Awaitable, TypeVar

T = TypeVar("T")


async def safe_call(coro: Awaitable[T], logger: logging.Logger, message: str) -> T | None:
    """Await `coro`; on any exception, log `message` as a warning and return None."""
    try:
        return await coro
    except Exception as e:
        logger.warning(f"{message}: {e}")
        return None
