"""
VK Bots Long Poll listener — one instance per community, detects new
published wall posts (wall_post_new) in near-real-time.

Runs as a standalone asyncio task alongside the bot's Telegram polling and
VK poller (wired up in main.py). See:
docs/superpowers/specs/2026-09-17-vk-long-poll-design.md
"""
import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

LONG_POLL_WAIT_SECONDS = 25
RECONNECT_DELAY_SECONDS = 5


async def _get_long_poll_session(
    session: aiohttp.ClientSession, token: str, group_id: int
) -> dict | None:
    params = {"group_id": abs(group_id), "v": "5.199", "access_token": token}
    try:
        async with session.get(
            "https://api.vk.com/method/groups.getLongPollServer",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
        if "error" in data:
            logger.error(f"groups.getLongPollServer failed for group {group_id}: {data['error']}")
            return None
        return data["response"]
    except Exception as e:
        logger.warning(f"groups.getLongPollServer network error for group {group_id}: {e}")
        return None


async def run_long_poll(comm_cfg, poller) -> None:
    group_id = comm_cfg.group_id
    async with aiohttp.ClientSession() as session:
        lp_session = None
        while True:
            if lp_session is None:
                lp_session = await _get_long_poll_session(session, comm_cfg.token, group_id)
                if lp_session is None:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue

            try:
                async with session.get(
                    lp_session["server"],
                    params={
                        "act": "a_check",
                        "key": lp_session["key"],
                        "ts": lp_session["ts"],
                        "wait": LONG_POLL_WAIT_SECONDS,
                    },
                    timeout=aiohttp.ClientTimeout(total=LONG_POLL_WAIT_SECONDS + 10),
                ) as resp:
                    data = await resp.json()
            except Exception as e:
                logger.warning(f"Long Poll network error for group {group_id}: {e}")
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue

            try:
                failed = data.get("failed")
                if failed == 1:
                    # History gap — VK supplies a fresh ts to resume from, same session.
                    lp_session["ts"] = data["ts"]
                    continue
                if failed in (2, 3):
                    # Key expired (2) or session data lost (3) — both need a fresh session.
                    logger.info(
                        f"Long Poll session for group {group_id} expired (failed={failed}), reconnecting"
                    )
                    lp_session = None
                    continue
                if failed:
                    logger.error(f"Long Poll unexpected failure for group {group_id}: {data}")
                    lp_session = None
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue

                lp_session["ts"] = data["ts"]
                updates = data.get("updates", [])
            except Exception as e:
                # Valid JSON but the wrong shape (e.g. not a dict, or missing
                # expected keys) — treat like a network/protocol error.
                logger.warning(f"Long Poll malformed response for group {group_id}: {e}")
                lp_session = None
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue

            for update in updates:
                if update.get("type") == "wall_post_new":
                    await poller.handle_new_published_post(group_id, update.get("object", {}))
