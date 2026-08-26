import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import schedule_board
from config import load_config
from database import Database
from handlers import router
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
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    await reload_pending_jobs(scheduler, db, now)
    scheduler.start()
    logger.info("APScheduler started")

    # Create Telegram topics for VK communities (if not yet)
    await setup_communities(bot, db, config)

    # VK polling loop
    poller = VKPoller(bot=bot, db=db, config=config, scheduler=scheduler)
    poller_task = asyncio.create_task(poller.start())

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot, db=db, config=config, scheduler=scheduler)
    finally:
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
