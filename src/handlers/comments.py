"""Listens for Telegram auto-forwarding a channel post into its linked
discussion group, and hands it to channel_comments to complete any pending
continuation for a long suggested post."""
from aiogram import F, Router
from aiogram.types import Message

from channel_comments import handle_forwarded_channel_post
from database import Database

router = Router()


@router.message(F.is_automatic_forward)
async def on_channel_post_mirrored(message: Message, db: Database):
    await handle_forwarded_channel_post(message, db)
