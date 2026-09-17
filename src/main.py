import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import schedule_board
from config import load_config
from database import Database
from handlers import router
from long_poll import run_long_poll
from poller import VKPoller
from scheduler import create_scheduler, init as init_scheduler, reload_pending_jobs
from setup import setup_communities

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    config = load_config()

    if not config.ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty — the bot is unrestricted, every Telegram user can "
            "use admin commands and buttons. Set ADMIN_IDS in .env to restrict access."
        )

    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Database
    db = Database(config.DB_PATH)
    await db.connect()

    # Bot & Dispatcher
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # APScheduler — persistent via DB, in-memory at runtime
    scheduler = create_scheduler(config.TIMEZONE)
    schedule_board.init(bot, db, config)
    init_scheduler(bot, db, config, refresh_board_fn=schedule_board.refresh_schedule_board)
    now = datetime.now(config.tz)
    await reload_pending_jobs(scheduler, db, now)
    scheduler.start()
    logger.info("APScheduler started")

    # Create Telegram topics for VK communities (if not yet)
    await setup_communities(bot, db, config)

    # VK Bots Long Poll uses each community's own group token (not the shared
    # VK_USER_TOKEN wall.get relies on), so it's independent of VK_POLLING_ENABLED
    # and keeps running even while the wall.get-based poller is paused (e.g. the
    # user token got flood-controlled/blocked account-wide) — the listeners only
    # need handle_new_published_post() on the poller instance, not poller.start().
    poller = VKPoller(bot=bot, db=db, config=config, scheduler=scheduler)
    poller_task = None
    if config.POLL_ENABLED:
        poller_task = asyncio.create_task(poller.start())
    else:
        logger.warning(
            "VK wall.get polling disabled (VK_POLLING_ENABLED=false) — the "
            "suggested-queue poll and published safety-net poll (deletion "
            "detection) are off, but VK Bots Long Poll still delivers new "
            "published posts, and the bot still handles Telegram commands and "
            "fires already-scheduled publications."
        )
    long_poll_tasks = [
        asyncio.create_task(run_long_poll(comm_cfg, poller))
        for comm_cfg in config.COMMUNITIES
    ]

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot, db=db, config=config, scheduler=scheduler)
    finally:
        for t in long_poll_tasks:
            t.cancel()
        for t in long_poll_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if poller_task:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
