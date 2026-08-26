"""Manual-placement badges (for oversized posts) and channel-message deletion."""
import logging
from datetime import datetime

from aiogram import Router, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from callbacks import DelChannelCallback, ManualDoneCallback, ManualInfoCallback
from config import Config
from database import Database
from keyboards import get_manually_placed_badge
from tg_utils import safe_call

from .common import _require_admin

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(ManualDoneCallback.filter())
async def handle_manual_done(
    callback: CallbackQuery,
    callback_data: ManualDoneCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not await _require_admin(callback, config):
        return

    post = await db.get_post_by_id(callback_data.post_db_id)
    if not post:
        await callback.answer("❌ Пост не найден", show_alert=True)
        return

    tz = config.tz
    unix_ts = int(datetime.now(tz).timestamp())
    time_str = datetime.fromtimestamp(unix_ts, tz=tz).strftime("%d.%m.%Y %H:%M")

    await callback.answer(f"✅ Отмечен как размещённый вручную {time_str}", show_alert=True)

    tg_msg_id = post.get("tg_message_id")
    topic_id = post.get("tg_topic_id")

    if tg_msg_id:
        await safe_call(
            bot.edit_message_reply_markup(
                chat_id=config.GROUP_ID,
                message_id=tg_msg_id,
                reply_markup=get_manually_placed_badge(callback_data.post_db_id, unix_ts, config.TIMEZONE),
            ),
            logger,
            f"Could not update keyboard for post {callback_data.post_db_id}",
        )

    await safe_call(
        bot.send_message(
            chat_id=config.GROUP_ID,
            text=f"✅ Размещён вручную {time_str}",
            message_thread_id=topic_id,
            reply_to_message_id=tg_msg_id,
        ),
        logger,
        f"Could not send manual marker for post {callback_data.post_db_id}",
    )


@router.callback_query(ManualInfoCallback.filter())
async def handle_manual_info(
    callback: CallbackQuery,
    callback_data: ManualInfoCallback,
    config: Config,
):
    tz = config.tz
    dt = datetime.fromtimestamp(callback_data.unix_ts, tz=tz)
    time_str = dt.strftime("%d.%m.%Y %H:%M")
    await callback.answer(
        f"Размещён вручную {time_str} ({config.TIMEZONE})",
        show_alert=True,
    )


# ── Delete from channel ───────────────────────────────────────────────────────

@router.callback_query(DelChannelCallback.filter())
async def handle_del_channel(
    callback: CallbackQuery,
    callback_data: DelChannelCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not await _require_admin(callback, config):
        return

    record = await db.get_scheduled_post_record(callback_data.record_id)
    if not record:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    channel_msg_id = record.get("channel_message_id")
    if channel_msg_id:
        try:
            await bot.delete_message(
                chat_id=record["channel_id"],
                message_id=channel_msg_id,
            )
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete channel message: {e}")
    await callback.message.edit_text("🗑 Удалено из канала", reply_markup=None)
    await callback.answer("Удалено")
