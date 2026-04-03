import aiosqlite
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        # Migration: add channel_message_id column if it doesn't exist yet
        try:
            await self._conn.execute(
                "ALTER TABLE scheduled_channel_posts ADD COLUMN channel_message_id INTEGER"
            )
            await self._conn.commit()
        except aiosqlite.OperationalError:
            pass  # Column already exists — expected on every run after first migration
        logger.info(f"Database connected: {self.db_path}")

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS communities (
                vk_id              INTEGER PRIMARY KEY,
                name               TEXT NOT NULL,
                screen_name        TEXT DEFAULT '',
                channel_id         INTEGER DEFAULT 0,
                published_topic_id INTEGER,
                suggested_topic_id INTEGER,
                last_post_id       INTEGER DEFAULT 0,
                last_suggest_id    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS posts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_post_id    INTEGER NOT NULL,
                community_id  INTEGER NOT NULL,
                post_type     TEXT NOT NULL,
                tg_message_id INTEGER,
                tg_topic_id   INTEGER,
                content_json  TEXT,
                created_at    INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(vk_post_id, community_id, post_type)
            );

            CREATE TABLE IF NOT EXISTS scheduled_channel_posts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id      INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                channel_id   INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                is_suggested INTEGER DEFAULT 0,
                schedule_time INTEGER NOT NULL,
                job_id       TEXT,
                channel_message_id INTEGER,
                status       TEXT DEFAULT 'pending',
                created_at   INTEGER DEFAULT (strftime('%s','now'))
            );
        """)
        await self._conn.commit()

    # ── Communities ───────────────────────────────────────────────────────────

    async def get_communities(self) -> List[dict]:
        async with self._conn.execute("SELECT * FROM communities") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_community(self, vk_id: int) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM communities WHERE vk_id = ?", (vk_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def upsert_community(
        self,
        vk_id: int,
        name: str,
        screen_name: str,
        channel_id: int,
        published_topic_id: int,
        suggested_topic_id: int,
    ):
        await self._conn.execute(
            """
            INSERT INTO communities
                (vk_id, name, screen_name, channel_id, published_topic_id, suggested_topic_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(vk_id) DO UPDATE SET
                name               = excluded.name,
                screen_name        = excluded.screen_name,
                channel_id         = excluded.channel_id,
                published_topic_id = excluded.published_topic_id,
                suggested_topic_id = excluded.suggested_topic_id
            """,
            (vk_id, name, screen_name, channel_id, published_topic_id, suggested_topic_id),
        )
        await self._conn.commit()

    _LAST_ID_COL = {"published": "last_post_id", "suggested": "last_suggest_id"}
    _TOPIC_COL = {"published": "published_topic_id", "suggested": "suggested_topic_id"}

    async def update_community_last_id(self, vk_id: int, post_type: str, post_id: int):
        col = self._LAST_ID_COL[post_type]
        await self._conn.execute(
            f"UPDATE communities SET {col} = ? WHERE vk_id = ?", (post_id, vk_id)
        )
        await self._conn.commit()

    async def clear_community_topic(self, vk_id: int, post_type: str):
        """Set topic_id to NULL so setup recreates it on next restart."""
        col = self._TOPIC_COL[post_type]
        await self._conn.execute(
            f"UPDATE communities SET {col} = NULL WHERE vk_id = ?", (vk_id,)
        )
        await self._conn.commit()

    # ── Posts ─────────────────────────────────────────────────────────────────

    async def save_post_get_id(
        self,
        vk_post_id: int,
        community_id: int,
        post_type: str,
        tg_topic_id: int,
        content_json: str,
    ) -> Optional[int]:
        try:
            async with self._conn.execute(
                """
                INSERT INTO posts (vk_post_id, community_id, post_type, tg_topic_id, content_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (vk_post_id, community_id, post_type, tg_topic_id, content_json),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except aiosqlite.IntegrityError:
            return None

    async def update_post_message_id(self, post_id: int, tg_message_id: int):
        await self._conn.execute(
            "UPDATE posts SET tg_message_id = ? WHERE id = ?", (tg_message_id, post_id)
        )
        await self._conn.commit()

    async def get_post(self, vk_post_id: int, community_id: int, post_type: str) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM posts WHERE vk_post_id=? AND community_id=? AND post_type=?",
            (vk_post_id, community_id, post_type),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_post_by_id(self, post_id: int) -> Optional[dict]:
        async with self._conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_posts_by_community(self, community_id: int, post_type: str) -> List[dict]:
        async with self._conn.execute(
            "SELECT * FROM posts WHERE community_id=? AND post_type=?",
            (community_id, post_type),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def delete_post_by_id(self, post_id: int):
        """Delete post; CASCADE removes its scheduled_channel_posts rows."""
        await self._conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await self._conn.commit()

    # ── Scheduled channel posts ────────────────────────────────────────────────

    async def save_scheduled_post_record(
        self,
        post_id: int,
        channel_id: int,
        content_json: str,
        is_suggested: bool,
        schedule_time: int,
    ) -> int:
        """Insert a scheduled-post record and return its id."""
        async with self._conn.execute(
            """
            INSERT INTO scheduled_channel_posts
                (post_id, channel_id, content_json, is_suggested, schedule_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, channel_id, content_json, int(is_suggested), schedule_time),
        ) as cur:
            row_id = cur.lastrowid
        await self._conn.commit()
        return row_id

    async def update_scheduled_post_job_id(self, record_id: int, job_id: str):
        await self._conn.execute(
            "UPDATE scheduled_channel_posts SET job_id = ? WHERE id = ?", (job_id, record_id)
        )
        await self._conn.commit()

    async def get_scheduled_post_record(self, record_id: int) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM scheduled_channel_posts WHERE id = ?", (record_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_pending_scheduled_posts(self) -> List[dict]:
        """All pending scheduled posts — used on startup to reload APScheduler."""
        async with self._conn.execute(
            "SELECT * FROM scheduled_channel_posts WHERE status = 'pending'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_pending_scheduled_for_post(self, post_id: int) -> List[dict]:
        async with self._conn.execute(
            "SELECT * FROM scheduled_channel_posts WHERE post_id = ? AND status = 'pending'",
            (post_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def mark_scheduled_post_sent(self, record_id: int):
        await self._conn.execute(
            "UPDATE scheduled_channel_posts SET status = 'sent' WHERE id = ?", (record_id,)
        )
        await self._conn.commit()

    async def mark_scheduled_post_cancelled(self, record_id: int):
        await self._conn.execute(
            "UPDATE scheduled_channel_posts SET status = 'cancelled' WHERE id = ?", (record_id,)
        )
        await self._conn.commit()

    async def update_scheduled_post_channel_msg_id(self, record_id: int, channel_msg_id: int):
        await self._conn.execute(
            "UPDATE scheduled_channel_posts SET channel_message_id = ? WHERE id = ?",
            (channel_msg_id, record_id),
        )
        await self._conn.commit()

    async def get_sent_channel_post_for_vk_id(
        self, vk_post_id: int, community_id: int
    ) -> Optional[dict]:
        """Find the most recent sent channel publication for this vk_post_id."""
        async with self._conn.execute(
            """
            SELECT scp.*, p.content_json AS orig_content_json
            FROM scheduled_channel_posts scp
            JOIN posts p ON scp.post_id = p.id
            WHERE p.vk_post_id = ? AND p.community_id = ?
              AND scp.status = 'sent'
            ORDER BY scp.schedule_time DESC LIMIT 1
            """,
            (vk_post_id, community_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
