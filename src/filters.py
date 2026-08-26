"""Reusable aiogram filters."""
from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import Config


class IsAdmin(BaseFilter):
    """Allows the update only for configured admins.

    Empty ADMIN_IDS means the bot is unrestricted — everyone passes.
    """

    async def __call__(self, message: Message, config: Config) -> bool:
        return not config.ADMIN_IDS or message.from_user.id in config.ADMIN_IDS
