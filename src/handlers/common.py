"""Shared helpers used across handler modules."""
from aiogram.types import CallbackQuery, Message

from config import Config


def _is_admin(user_id: int, config: Config) -> bool:
    # Empty ADMIN_IDS = unrestricted (open bot). Set ADMIN_IDS in .env to restrict.
    return not config.ADMIN_IDS or user_id in config.ADMIN_IDS


async def _require_admin(callback: CallbackQuery, config: Config) -> bool:
    """Check admin access for a callback query; alert and return False if denied."""
    if _is_admin(callback.from_user.id, config):
        return True
    await callback.answer("⛔️ Нет доступа", show_alert=True)
    return False


async def _notify(event: CallbackQuery | Message, text: str, alert: bool = False):
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=alert)
    else:
        await event.reply(text)
