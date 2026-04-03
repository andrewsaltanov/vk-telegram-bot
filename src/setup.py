"""
Initial setup: create Telegram forum topics for each configured VK community.
"""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from config import Config
from database import Database
from vk_client import VKClient

logger = logging.getLogger(__name__)


async def setup_communities(bot: Bot, db: Database, config: Config):
    """Ensure every configured VK community has topics in the Telegram group."""
    if not config.COMMUNITIES:
        logger.warning("COMMUNITIES is empty — nothing to set up.")
        return

    for comm_cfg in config.COMMUNITIES:
        community_id = comm_cfg.group_id
        existing = await db.get_community(community_id)

        pub_topic_id = existing.get("published_topic_id") if existing else None
        sug_topic_id = existing.get("suggested_topic_id") if existing else None

        if pub_topic_id and sug_topic_id:
            # Update channel_id in case it changed in communities.json
            if existing.get("channel_id") != comm_cfg.channel_id:
                await db.upsert_community(
                    vk_id=community_id,
                    name=existing["name"],
                    screen_name=existing.get("screen_name", ""),
                    channel_id=comm_cfg.channel_id,
                    published_topic_id=pub_topic_id,
                    suggested_topic_id=sug_topic_id,
                )
                logger.info(
                    f"Updated channel_id for community {community_id} → {comm_cfg.channel_id}"
                )
            else:
                logger.info(
                    f"Community {community_id} ({existing.get('name')}) already configured."
                )
            continue

        # One or both topics missing — fetch VK info and create them
        async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
            info = await vk.get_group_info(community_id)

        name = comm_cfg.name
        screen_name = info.get("screen_name", str(community_id)) if info else str(community_id)

        try:
            if not pub_topic_id:
                pub_topic = await bot.create_forum_topic(
                    chat_id=config.GROUP_ID,
                    name=f"📢 {name}",
                )
                pub_topic_id = pub_topic.message_thread_id
                await _send_welcome(
                    bot, config.GROUP_ID, pub_topic_id,
                    f"📢 Здесь будут появляться новые <b>опубликованные посты</b> "
                    f'из <a href="https://vk.com/{screen_name}">{name}</a>.\n\n'
                    f"Канал публикации: {comm_cfg.channel_id}",
                )
                await asyncio.sleep(0.5)

            if not sug_topic_id:
                sug_topic = await bot.create_forum_topic(
                    chat_id=config.GROUP_ID,
                    name=f"💡 {name} — Предложки",
                )
                sug_topic_id = sug_topic.message_thread_id
                await _send_welcome(
                    bot, config.GROUP_ID, sug_topic_id,
                    f"💡 Здесь будут появляться <b>предложенные посты</b> "
                    f'из <a href="https://vk.com/{screen_name}">{name}</a>.\n\n'
                    f"Канал публикации: {comm_cfg.channel_id}",
                )

        except TelegramBadRequest as e:
            logger.error(
                f"Cannot create topics for community {community_id}: {e}\n"
                "Make sure the group is a supergroup with Topics enabled "
                "and the bot is an admin with 'Manage Topics' permission."
            )
            continue

        await db.upsert_community(
            vk_id=community_id,
            name=name,
            screen_name=screen_name,
            channel_id=comm_cfg.channel_id,
            published_topic_id=pub_topic_id,
            suggested_topic_id=sug_topic_id,
        )

        logger.info(
            f"✅ Set up community '{name}' ({community_id}): "
            f"pub_topic={pub_topic_id}, sug_topic={sug_topic_id}, "
            f"channel={comm_cfg.channel_id}"
        )
        await asyncio.sleep(1)


async def _send_welcome(bot: Bot, chat_id: int, thread_id: int, text: str):
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not send welcome message to topic {thread_id}: {e}")
