import asyncio
import aiohttp
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.131"
VK_API_URL = "https://api.vk.com/method/"


class VKClient:
    def __init__(self, token: str, user_token: str = ""):
        self.token = token          # community/group token
        self.user_token = user_token  # user admin token (for wall reads)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _call(
        self,
        method: str,
        params: dict,
        use_token: bool = True,
        use_user_token: bool = False,
        _retries: int = 3,
    ) -> Optional[dict]:
        base = {"v": VK_API_VERSION}
        if use_user_token and self.user_token:
            base["access_token"] = self.user_token
        elif use_token and self.token:
            base["access_token"] = self.token
        params = {**base, **params}
        try:
            async with self._session.get(
                f"{VK_API_URL}{method}", params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error(f"VK HTTP error [{method}]: {e}")
            return None

        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg", "")
            if code == 6 or code == 29:
                # Rate limit — wait and retry with exponential backoff
                if _retries > 0:
                    wait = 2 ** (4 - _retries)  # 1s, 2s, 4s
                    logger.warning(
                        f"VK rate limit [{method}] (error {code}), retrying in {wait}s "
                        f"({_retries} retries left)"
                    )
                    await asyncio.sleep(wait)
                    return await self._call(
                        method, params, use_token, use_user_token, _retries=_retries - 1
                    )
                logger.error(f"VK rate limit [{method}] — giving up after retries")
                return None
            if code == 27:
                logger.error(
                    f"VK error 27 for [{method}]: group token cannot call this method. "
                    "For 'suggests' you need a USER token (admin of the community). "
                    "Get it at https://vkhost.github.io/ with 'wall' + 'groups' scopes."
                )
            else:
                logger.error(f"VK API error [{method}]: {code} — {msg}")
            return None
        return data.get("response")

    # ── Community info ────────────────────────────────────────────────────────

    async def get_group_info(self, group_id: int) -> Optional[dict]:
        result = await self._call(
            "groups.getById",
            {"group_id": abs(group_id), "fields": "name,screen_name"},
        )
        if not result:
            return None
        # API may return list or {"groups": [...]}
        if isinstance(result, list):
            return result[0] if result else None
        groups = result.get("groups", [])
        return groups[0] if groups else None

    # ── Wall posts ────────────────────────────────────────────────────────────

    async def get_wall_posts(
        self, community_id: int, count: int = 50, offset: int = 0
    ) -> List[dict]:
        # Requires user admin token — group tokens are not supported (VK error 27)
        if not self.user_token:
            logger.warning(
                f"No user_token for community {community_id}. "
                "wall.get requires a user admin token. Add 'user_token' to communities.json "
                "or set VK_USER_TOKEN in .env. Get it at https://vkhost.github.io/"
            )
            return []
        result = await self._call(
            "wall.get",
            {
                "owner_id": -abs(community_id),
                "count": count,
                "offset": offset,
                "filter": "owner",
            },
            use_user_token=True,
        )
        if not result:
            return []
        return result.get("items", [])

    async def get_suggested_posts(
        self, community_id: int, count: int = 50, offset: int = 0
    ) -> List[dict]:
        if not self.user_token:
            logger.warning(
                f"No user_token for community {community_id}. "
                "Suggested posts require a user admin token."
            )
            return []
        result = await self._call(
            "wall.get",
            {
                "owner_id": -abs(community_id),
                "count": count,
                "offset": offset,
                "filter": "suggests",
            },
            use_user_token=True,
        )
        if not result:
            return []
        return result.get("items", [])

    async def post_exists(self, community_id: int, post_id: int) -> bool:
        result = await self._call(
            "wall.getById",
            {"posts": f"-{abs(community_id)}_{post_id}"},
            use_user_token=True,
        )
        if result is None:
            return True  # API error — assume exists to avoid false deletions
        items = result if isinstance(result, list) else result.get("items", [])
        return bool(items)

    async def fetch_post_data(self, community_id: int, post_id: int) -> Optional[dict]:
        """
        Fetch full post data for content comparison.
        Returns: dict with post data if found, {} if post was deleted, None if API error.
        """
        result = await self._call(
            "wall.getById",
            {"posts": f"-{abs(community_id)}_{post_id}"},
            use_user_token=True,
        )
        if result is None:
            return None  # API error — caller should fail-open
        items = result if isinstance(result, list) else result.get("items", [])
        return items[0] if items else {}  # {} signals deleted

    # ── Content extraction ────────────────────────────────────────────────────

    def extract_post_content(self, post: dict) -> dict:
        """Extract all relevant content from a VK post."""
        text = post.get("text", "")
        photos: List[str] = []
        videos: List[dict] = []
        links: List[str] = []
        docs: List[dict] = []

        for att in post.get("attachments", []):
            att_type = att.get("type")

            if att_type == "photo":
                photo = att["photo"]
                sizes = sorted(
                    photo.get("sizes", []),
                    key=lambda s: s.get("width", 0),
                    reverse=True,
                )
                if sizes:
                    photos.append(sizes[0]["url"])

            elif att_type == "video":
                video = att["video"]
                vid_owner = video.get("owner_id", 0)
                vid_id = video.get("id", 0)
                thumb = ""
                if video.get("image"):
                    thumb = sorted(
                        video["image"], key=lambda i: i.get("width", 0), reverse=True
                    )[0].get("url", "")
                videos.append(
                    {
                        "url": f"https://vk.com/video{vid_owner}_{vid_id}",
                        "title": video.get("title", "Видео"),
                        "thumb": thumb,
                    }
                )

            elif att_type == "link":
                url = att["link"].get("url", "")
                if url:
                    links.append(url)

            elif att_type == "doc":
                doc = att["doc"]
                doc_url = doc.get("url", "")
                if doc_url:
                    docs.append({"title": doc.get("title", "Документ"), "url": doc_url})

        return {
            "text": text,
            "photos": photos,
            "videos": videos,
            "links": links,
            "docs": docs,
        }

    def get_author_link(self, post: dict, community_id: int) -> str:
        from_id = post.get("from_id") or post.get("signer_id")
        if from_id:
            if from_id > 0:
                return f"https://vk.com/id{from_id}"
            else:
                return f"https://vk.com/club{abs(from_id)}"
        return f"https://vk.com/club{abs(community_id)}"

    def get_post_link(self, community_id: int, post_id: int) -> str:
        return f"https://vk.com/wall-{abs(community_id)}_{post_id}"
