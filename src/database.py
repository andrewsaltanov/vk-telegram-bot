import aiosqlite
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# Default "nearby schedule" conflict window for get_pending_for_channel_near_time (±30 min).
DEFAULT_CONFLICT_WINDOW_SECONDS = 1800


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ── Schema migrations ─────────────────────────────────────────────────────
    # Add new SQL strings here (append only — never edit existing entries).
    _MIGRATIONS: list = [
        # v1 — add channel_message_id to scheduled_channel_posts
        "ALTER TABLE scheduled_channel_posts ADD COLUMN channel_message_id INTEGER",
        # v2 — add schedule_board_msg_id to communities (for per-topic queue board)
        "ALTER TABLE communities ADD COLUMN schedule_board_msg_id INTEGER",
        # v3 — add notifications_muted to communities (for mute feature)
        "ALTER TABLE communities ADD COLUMN notifications_muted INTEGER DEFAULT 0",
        # v4 — add suggested_board_msg_id to communities (queue board mirrored into the suggested topic)
        "ALTER TABLE communities ADD COLUMN suggested_board_msg_id INTEGER",
    ]

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._run_migrations()
        logger.info(f"Database connected: {self.db_path}")

    async def _run_migrations(self):
        """Apply any pending schema migrations, tracked by _schema_version table."""
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_version (
                id      INTEGER PRIMARY KEY CHECK(id = 1),
                version INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self._conn.execute(
            "INSERT OR IGNORE INTO _schema_version (id, version) VALUES (1, 0)"
        )
        await self._conn.commit()

        async with self._conn.execute(
            "SELECT version FROM _schema_version WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        current = row["version"] if row else 0

        for idx, sql in enumerate(self._MIGRATIONS, start=1):
            if idx <= current:
                continue
            logger.info(f"Applying DB migration {idx}/{len(self._MIGRATIONS)} …")
            try:
                await self._conn.execute(sql)
            except aiosqlite.OperationalError as e:
                # Column/index already exists from a manual migration — safe to skip
                logger.warning(f"Migration {idx} skipped (already applied): {e}")
            await self._conn.execute(
                "UPDATE _schema_version SET version = ? WHERE id = 1", (idx,)
            )
            await self._conn.commit()
            logger.info(f"Migration {idx} applied.")

        if current < len(self._MIGRATIONS):
            logger.info(f"DB schema updated to version {len(self._MIGRATIONS)}.")

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

    # ── Schedule board ─────────────────────────────────────────────────────────

    async def get_pending_for_channel(self, channel_id: int) -> List[dict]:
        """All pending scheduled posts for a channel, sorted by time (for board rendering)."""
        async with self._conn.execute(
            """
            SELECT scp.id, scp.schedule_time, scp.content_json
            FROM scheduled_channel_posts scp
            WHERE scp.channel_id = ? AND scp.status = 'pending'
            ORDER BY scp.schedule_time ASC
            """,
            (channel_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def set_schedule_board_msg_id(self, vk_id: int, msg_id: Optional[int]):
        await self._conn.execute(
            "UPDATE communities SET schedule_board_msg_id = ? WHERE vk_id = ?",
            (msg_id, vk_id),
        )
        await self._conn.commit()

    async def set_suggested_board_msg_id(self, vk_id: int, msg_id: Optional[int]):
        await self._conn.execute(
            "UPDATE communities SET suggested_board_msg_id = ? WHERE vk_id = ?",
            (msg_id, vk_id),
        )
        await self._conn.commit()

    async def get_all_pending_with_community(self) -> List[dict]:
        """All pending scheduled posts with community name — for /queue command."""
        async with self._conn.execute(
            """
            SELECT scp.id, scp.schedule_time, scp.content_json,
                   c.name AS community_name, p.community_id AS community_id
            FROM scheduled_channel_posts scp
            JOIN posts p ON scp.post_id = p.id
            JOIN communities c ON p.community_id = c.vk_id
            WHERE scp.status = 'pending'
            ORDER BY scp.schedule_time ASC
            """
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_pending_for_channel_near_time(
        self, channel_id: int, schedule_ts: int, window: int = DEFAULT_CONFLICT_WINDOW_SECONDS
    ) -> List[dict]:
        """Pending posts for a channel within `window` seconds of schedule_ts."""
        async with self._conn.execute(
            """
            SELECT id, schedule_time
            FROM scheduled_channel_posts
            WHERE channel_id = ? AND status = 'pending'
              AND ABS(schedule_time - ?) < ?
            """,
            (channel_id, schedule_ts, window),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Content and analytics updates ──────────────────────────────────────────

    async def update_post_content_json(self, post_id: int, content_json: str):
        """Update a post's content_json."""
        await self._conn.execute(
            "UPDATE posts SET content_json = ? WHERE id = ?", (content_json, post_id)
        )
        await self._conn.commit()

    async def update_scheduled_post_content_json(self, record_id: int, content_json: str):
        """Update a scheduled post record's content_json."""
        await self._conn.execute(
            "UPDATE scheduled_channel_posts SET content_json = ? WHERE id = ?",
            (content_json, record_id),
        )
        await self._conn.commit()

    async def get_community_by_topic_id(self, topic_id: int) -> Optional[dict]:
        """Find community by either published_topic_id or suggested_topic_id."""
        async with self._conn.execute(
            "SELECT * FROM communities WHERE published_topic_id = ? OR suggested_topic_id = ?",
            (topic_id, topic_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_unscheduled_posts_for_topic(self, topic_id: int) -> List[dict]:
        """Posts in a topic that have no pending scheduled channel publication."""
        async with self._conn.execute(
            """
            SELECT p.*
            FROM posts p
            WHERE p.tg_topic_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM scheduled_channel_posts scp
                  WHERE scp.post_id = p.id AND scp.status IN ('pending', 'sent')
              )
            ORDER BY p.vk_post_id ASC
            """,
            (topic_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_pending_suggested_count(self, community_id: int) -> int:
        """Count suggested posts for a community that have not yet been published."""
        async with self._conn.execute(
            """
            SELECT COUNT(*) FROM posts p
            WHERE p.community_id = ? AND p.post_type = 'suggested'
              AND NOT EXISTS (
                  SELECT 1 FROM scheduled_channel_posts scp
                  WHERE scp.post_id = p.id AND scp.status = 'sent'
              )
            """,
            (community_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_published_count_since(self, community_id: int, since_ts: int) -> int:
        """Count channel posts published (status='sent') since a unix timestamp."""
        async with self._conn.execute(
            """
            SELECT COUNT(*) FROM scheduled_channel_posts scp
            JOIN posts p ON scp.post_id = p.id
            WHERE p.community_id = ? AND scp.status = 'sent' AND scp.schedule_time >= ?
            """,
            (community_id, since_ts),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def set_notifications_muted(self, vk_id: int, muted: bool):
        """Set the notifications_muted flag for a community."""
        await self._conn.execute(
            "UPDATE communities SET notifications_muted = ? WHERE vk_id = ?",
            (int(muted), vk_id),
        )
        await self._conn.commit()
