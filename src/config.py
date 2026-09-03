import json
import os
from dataclasses import dataclass
from functools import cached_property
from typing import List
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass
class CommunityConfig:
    group_id: int
    name: str
    token: str        # community/group token (for posting on behalf of group)
    channel_id: int
    user_token: str = ""  # admin user token (required for wall.get + suggests)


@dataclass
class Config:
    BOT_TOKEN: str
    GROUP_ID: int
    ADMIN_IDS: List[int]
    COMMUNITIES: List[CommunityConfig]
    POLL_INTERVAL: int = 300
    INITIAL_POSTS_COUNT: int = 10
    DB_PATH: str = "/app/data/bot.db"
    TIMEZONE: str = "Europe/Moscow"
    COMMUNITIES_FILE: str = "/app/communities.json"

    def get_community_config(self, group_id: int) -> CommunityConfig | None:
        for c in self.COMMUNITIES:
            if c.group_id == group_id:
                return c
        return None

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)


def load_config() -> Config:
    admin_ids = [
        int(x.strip())
        for x in os.environ.get("ADMIN_IDS", "").split(",")
        if x.strip()
    ]

    communities_file = os.environ.get("COMMUNITIES_FILE", "/app/communities.json")
    communities: List[CommunityConfig] = []
    if os.path.exists(communities_file):
        with open(communities_file, encoding="utf-8") as f:
            raw = json.load(f)
        # Global user token fallback (if not set per-community)
        global_user_token = os.environ.get("VK_USER_TOKEN", "")
        for item in raw:
            communities.append(
                CommunityConfig(
                    group_id=int(item["group_id"]),
                    name=item.get("name", f"Community {item['group_id']}"),
                    token=item["token"],
                    channel_id=int(item["channel_id"]),
                    user_token=item.get("user_token", global_user_token),
                )
            )
    else:
        raise FileNotFoundError(
            f"communities.json not found at {communities_file}. "
            "Set COMMUNITIES_FILE env var or mount the file."
        )

    return Config(
        BOT_TOKEN=os.environ["BOT_TOKEN"],
        GROUP_ID=int(os.environ["GROUP_ID"]),
        ADMIN_IDS=admin_ids,
        COMMUNITIES=communities,
        POLL_INTERVAL=int(os.environ.get("POLL_INTERVAL", "300")),
        INITIAL_POSTS_COUNT=int(os.environ.get("INITIAL_POSTS_COUNT", "10")),
        DB_PATH=os.environ.get("DB_PATH", "/app/data/bot.db"),
        TIMEZONE=os.environ.get("TIMEZONE", "Europe/Moscow"),
        COMMUNITIES_FILE=communities_file,
    )
