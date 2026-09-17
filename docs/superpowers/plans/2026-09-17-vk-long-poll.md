# VK Bots Long Poll for Published Posts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect new published VK wall posts via VK's Bots Long Poll API (`wall_post_new` events, delivered over a connection this bot opens outward using the existing community token) instead of frequent `wall.get` polling — freeing up the personal user-token's monthly API quota to poll the suggested-post queue more often, while a low-frequency safety-net poll keeps handling deletion detection.

**Architecture:** A new `src/long_poll.py` module runs one `run_long_poll(comm_cfg, poller)` asyncio task per community (3 total), each independently calling `groups.getLongPollServer` with that community's own token and then looping `act=a_check` requests against the returned session. New posts dispatch to `VKPoller.handle_new_published_post()`, which reuses the existing send/dedup pipeline. `VKPoller`'s own `wall.get` loop keeps a second, much longer cadence (`PUBLISHED_SAFETY_POLL_INTERVAL`) purely for deletion detection and catching anything a Long Poll reconnect gap missed — new-post delivery for "published" no longer depends on it at all.

**Tech Stack:** Python 3.11, aiohttp (already a dependency via aiogram/requirements.txt), aiosqlite, no test framework (project has none — verification below is via standalone scripts run manually, matching existing project convention).

**Spec:** `docs/superpowers/specs/2026-09-17-vk-long-poll-design.md`

## Global Constraints

- This repo has no test suite, linter, or formatter configured — do not add one. Verification steps below are standalone `python3` scripts run manually, not `pytest`.
- All commands run with `src/` as the working directory (project convention — imports use bare module names, no package prefix).
- **This phase is local-only.** Do not touch the production VPS (`144.31.102.35`), do not run `docker compose` there. The spec's one remaining manual step (toggling Long Poll on + checking "Записи на стене" in each community's VK admin panel) is a separate later phase, not part of this plan.
- **Never run `python src/main.py` against the real `.env`'s `BOT_TOKEN`.** The production bot on the VPS is already polling Telegram with that same token — a second local `getUpdates` consumer on the same token will conflict with it (Telegram allows only one long-poll consumer per bot token) and can visibly disrupt the live bot. Every verification step below that needs a running process either avoids `dp.start_polling` entirely or uses a fresh throwaway `Config`, never the checked-in `.env`.
- The personal user-token used for `wall.get` is currently quota-exhausted for the month — no verification step may depend on a live call to `api.vk.com` using it. `groups.getLongPollServer` uses the separate *community* token instead (confirmed live during design not to be quota-blocked), but verification of `src/long_poll.py` still uses a local mock HTTP server rather than any real VK call at all, so it stays deterministic and independent of VK's live state.
- The codebase is at commit `3b50d59`'s state for `src/` files — no code from the abandoned Callback-API plan (`docs/superpowers/plans/2026-09-17-vk-callback-api.md`) was ever applied.

---

## File Structure

- `src/config.py` — modified: `Config` gains `PUBLISHED_SAFETY_POLL_INTERVAL`; `load_config()`'s effective `POLL_INTERVAL` default drops to 1200s.
- `src/poller.py` — modified: `_send_post` drops its `vk` parameter (calls `VKClient`'s static helpers directly); new `handle_new_published_post()` method; polling loop gains a tick-based dual cadence (`_should_check_published`).
- `src/long_poll.py` — new: the Bots Long Poll listener (`run_long_poll`, `_get_long_poll_session`).
- `src/main.py` — modified: constructs `VKPoller` unconditionally (was only inside the `POLL_ENABLED` branch), starts one Long Poll task per community, cleans them up in `finally`.
- `.env.example`, `README.md` — modified: document the two changed/new env-facing values.

## Interfaces (for reference across tasks)

- `VKPoller.handle_new_published_post(self, vk_id: int, post: dict) -> None` — entry point the Long Poll listener calls.
- `VKPoller._send_post(self, community: dict, post: dict, topic_id: int, post_type: str) -> None` — unchanged behavior, `vk` parameter removed.
- `VKPoller._should_check_published(self, tick: int) -> bool` — pure function, no I/O.
- `run_long_poll(comm_cfg: CommunityConfig, poller: VKPoller) -> None` — runs forever until cancelled.
- `_get_long_poll_session(session: aiohttp.ClientSession, token: str, group_id: int) -> dict | None` — returns `{"key", "server", "ts"}` or `None` on failure.
- `Config.get_community_config(self, group_id: int) -> CommunityConfig | None` — already exists.
- `Database.get_community(self, vk_id: int) -> Optional[dict]` — already exists, used by `handle_new_published_post` to get the DB-shape community row (topic ids, `last_post_id`).
- `Database.update_community_last_id(self, vk_id: int, post_type: str, post_id: int) -> None` — already exists.

---

### Task 1: Config — `PUBLISHED_SAFETY_POLL_INTERVAL` and the lowered `POLL_INTERVAL` default

**Files:**
- Modify: `src/config.py`
- Modify: `.env.example`
- Modify: `README.md:75-82`

**Interfaces:**
- Produces: `Config.PUBLISHED_SAFETY_POLL_INTERVAL: int`.

- [ ] **Step 1: Add the new `Config` field**

In `src/config.py`, change:

```python
@dataclass
class Config:
    BOT_TOKEN: str
    GROUP_ID: int
    ADMIN_IDS: List[int]
    COMMUNITIES: List[CommunityConfig]
    POLL_INTERVAL: int = 300
    POLL_ENABLED: bool = True
    INITIAL_POSTS_COUNT: int = 10
    DB_PATH: str = "/app/data/bot.db"
    TIMEZONE: str = "Europe/Moscow"
    COMMUNITIES_FILE: str = "/app/communities.json"
```

to:

```python
@dataclass
class Config:
    BOT_TOKEN: str
    GROUP_ID: int
    ADMIN_IDS: List[int]
    COMMUNITIES: List[CommunityConfig]
    POLL_INTERVAL: int = 300
    POLL_ENABLED: bool = True
    INITIAL_POSTS_COUNT: int = 10
    DB_PATH: str = "/app/data/bot.db"
    TIMEZONE: str = "Europe/Moscow"
    COMMUNITIES_FILE: str = "/app/communities.json"
    PUBLISHED_SAFETY_POLL_INTERVAL: int = 14400
```

(The dataclass field default of `300` for `POLL_INTERVAL` above is unreachable in practice — `load_config()` always passes an explicit value — but leave it as a fallback; Step 2 changes what `load_config()` actually passes.)

- [ ] **Step 2: Wire the new field into `load_config()`, lower `POLL_INTERVAL`'s effective default to 1200s**

Change:

```python
        # VK's Sept 2026 policy caps unverified apps at 10,000 API calls/month —
        # each cycle burns 2 wall.get calls per community, so 300s was ~5x over budget.
        POLL_INTERVAL=int(os.environ.get("POLL_INTERVAL", "3600")),
        POLL_ENABLED=os.environ.get("VK_POLLING_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
        INITIAL_POSTS_COUNT=int(os.environ.get("INITIAL_POSTS_COUNT", "10")),
        DB_PATH=os.environ.get("DB_PATH", "/app/data/bot.db"),
        TIMEZONE=os.environ.get("TIMEZONE", "Europe/Moscow"),
        COMMUNITIES_FILE=communities_file,
    )
```

to:

```python
        # POLL_INTERVAL now only paces the "suggested" queue (no VK event
        # exists for it under any transport). Published posts arrive via VK
        # Bots Long Poll instead (see long_poll.py); PUBLISHED_SAFETY_POLL_INTERVAL
        # below is just a slow safety net for deletion detection and catching
        # anything a Long Poll reconnect gap missed.
        POLL_INTERVAL=int(os.environ.get("POLL_INTERVAL", "1200")),
        PUBLISHED_SAFETY_POLL_INTERVAL=int(os.environ.get("PUBLISHED_SAFETY_POLL_INTERVAL", "14400")),
        POLL_ENABLED=os.environ.get("VK_POLLING_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
        INITIAL_POSTS_COUNT=int(os.environ.get("INITIAL_POSTS_COUNT", "10")),
        DB_PATH=os.environ.get("DB_PATH", "/app/data/bot.db"),
        TIMEZONE=os.environ.get("TIMEZONE", "Europe/Moscow"),
        COMMUNITIES_FILE=communities_file,
    )
```

- [ ] **Step 3: Document the new env var in `.env.example`**

In `.env.example`, change:

```
# ===== НАСТРОЙКИ =====
# Интервал опроса VK в секундах (по умолчанию 1 час).
# ВАЖНО: с 7 сентября 2026 VK ограничивает неверифицированные бизнес-профили
# 10 000 вызовов API/мес. Каждый цикл опроса — 2 вызова wall.get на сообщество,
# так что при 300 сек (5 мин) и 3 сообществах лимит вылетает за ~6 дней.
# 3600 сек держит потребление в пределах ~40% месячной квоты.
POLL_INTERVAL=3600
```

to:

```
# ===== НАСТРОЙКИ =====
# Интервал опроса ПРЕДЛОЖКИ (filter=suggests) в секундах — у VK нет события
# ни в Callback API, ни в Long Poll для неё, так что она остаётся на обычном
# опросе. По умолчанию 20 мин.
POLL_INTERVAL=1200

# Интервал редкого "safety net" опроса ОПУБЛИКОВАННОЙ стены — используется
# только для проверки удалений (у VK нет события wall_post_delete ни в одном
# транспорте) и как подстраховка на случай пропуска события в Long Poll.
# Новые опубликованные посты обнаруживаются мгновенно через VK Bots Long
# Poll (см. long_poll.py), а не через этот интервал. По умолчанию 4 часа.
PUBLISHED_SAFETY_POLL_INTERVAL=14400
```

- [ ] **Step 4: Update the README's `.env` table**

In `README.md`, change:

```
| `POLL_INTERVAL` | Интервал опроса VK в секундах (по умолчанию `300`) |
```

to two rows:

```
| `POLL_INTERVAL` | Интервал опроса предложки VK в секундах (по умолчанию `1200`) |
| `PUBLISHED_SAFETY_POLL_INTERVAL` | Редкий safety-net опрос опубликованной стены — только для проверки удалений (по умолчанию `14400`) |
```

- [ ] **Step 5: Verify config loads correctly with the new field**

Run (from the `src/` directory):

```bash
cd "/Users/andrewsaltanov/Desktop/Learning/VK post /src" && \
COMMUNITIES_FILE="../communities.json" python3 -c "
from config import load_config
c = load_config()
print('POLL_INTERVAL =', c.POLL_INTERVAL)
print('PUBLISHED_SAFETY_POLL_INTERVAL =', c.PUBLISHED_SAFETY_POLL_INTERVAL)
"
```

Expected output: `POLL_INTERVAL = 1200`, `PUBLISHED_SAFETY_POLL_INTERVAL = 14400` (this reads the real `.env` in the project root for `BOT_TOKEN`/`GROUP_ID`/`ADMIN_IDS` via `load_dotenv()`, but only *reads* it — no network call happens).

- [ ] **Step 6: Commit**

```bash
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/config.py .env.example README.md && \
git commit -m "$(cat <<'EOF'
feat: add PUBLISHED_SAFETY_POLL_INTERVAL, lower POLL_INTERVAL's default

POLL_INTERVAL now only paces the suggested-post queue (no VK event
exists for it under any transport). PUBLISHED_SAFETY_POLL_INTERVAL
paces a separate, much slower published-wall poll that exists purely
for deletion detection and catching anything VK Bots Long Poll missed
(added in a later task) — new published posts no longer wait on this
interval at all.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 2: Refactor `_send_post` to drop the `vk` parameter

**Files:**
- Modify: `src/poller.py:122-178` (call site), `src/poller.py:268-321` (`_send_post`)

**Interfaces:**
- Consumes: `VKClient.extract_post_content(post: dict) -> dict`, `VKClient.get_author_link(post: dict, community_id: int) -> str`, `VKClient.get_post_link(community_id: int, post_id: int) -> str` (all already `@staticmethod` on `VKClient`, `src/vk_client.py:189,247,257`).
- Produces: `VKPoller._send_post(self, community: dict, post: dict, topic_id: int, post_type: str) -> None` (was `_send_post(self, vk, community, post, topic_id, post_type)`).

- [ ] **Step 1: Drop the `vk` parameter from `_send_post`'s signature and body**

In `src/poller.py`, change:

```python
    async def _send_post(
        self,
        vk: VKClient,
        community: dict,
        post: dict,
        topic_id: int,
        post_type: str,
    ):
        community_id = community["vk_id"]

        content = vk.extract_post_content(post)
        content["author_link"] = vk.get_author_link(post, community_id)
        content["post_link"] = vk.get_post_link(community_id, post["id"])
        content["community_name"] = community.get("name", "")
```

to:

```python
    async def _send_post(
        self,
        community: dict,
        post: dict,
        topic_id: int,
        post_type: str,
    ):
        community_id = community["vk_id"]

        content = VKClient.extract_post_content(post)
        content["author_link"] = VKClient.get_author_link(post, community_id)
        content["post_link"] = VKClient.get_post_link(community_id, post["id"])
        content["community_name"] = community.get("name", "")
```

- [ ] **Step 2: Update the one call site in `_poll_wall`**

Change:

```python
            try:
                await self._send_post(vk, community, post, topic_id, post_type)
```

to:

```python
            try:
                await self._send_post(community, post, topic_id, post_type)
```

- [ ] **Step 3: Write a standalone verification script**

Create a scratch file (not committed) at `/tmp/verify_task2.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

from config import Config
from database import Database
from poller import VKPoller


async def run():
    db = Database("/tmp/verify_task2.db")
    await db.connect()
    await db.upsert_community(
        vk_id=555, name="Test Community", screen_name="test",
        channel_id=-100, published_topic_id=10, suggested_topic_id=20,
    )
    community = await db.get_community(555)

    # _send_post reads self.config.GROUP_ID as a kwarg to send_vk_post_to_topic
    # even though that call is mocked below — kwargs are evaluated before the
    # (now-mocked) call happens, so config=None would raise AttributeError.
    config = Config(BOT_TOKEN="test", GROUP_ID=999, ADMIN_IDS=[], COMMUNITIES=[])
    poller = VKPoller(bot=None, db=db, config=config, scheduler=None)

    fake_post = {"id": 1, "text": "hello", "attachments": [], "from_id": 1, "date": 1}

    with patch("poller.send_vk_post_to_topic", new=AsyncMock(return_value=[42])) as mock_send:
        await poller._send_post(community, fake_post, topic_id=10, post_type="published")
        assert mock_send.call_count == 1, f"expected 1 call, got {mock_send.call_count}"

        # Same vk_post_id again — must be deduped (no second Telegram send)
        await poller._send_post(community, fake_post, topic_id=10, post_type="published")
        assert mock_send.call_count == 1, f"dedup failed, got {mock_send.call_count} calls"

    print("TASK 2 VERIFICATION PASSED")
    await db.close()


asyncio.run(run())
```

- [ ] **Step 4: Run it**

```bash
rm -f /tmp/verify_task2.db && python3 /tmp/verify_task2.py
```

Expected output: `TASK 2 VERIFICATION PASSED`. If it fails with `AttributeError` on `bot`/`scheduler` being `None`, that means `_send_post` reached code that needs them — double check the diff in Step 1 only touched the lines shown (the real `_send_post` never touches `self.bot`/`self.scheduler` directly, only passes them through as opaque arguments to the mocked `send_vk_post_to_topic`; `self.config.GROUP_ID` is read directly though, which is why the script above uses a real minimal `Config`, not `None`).

- [ ] **Step 5: Delete the scratch script and commit the real change**

```bash
rm -f /tmp/verify_task2.py /tmp/verify_task2.db
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/poller.py && \
git commit -m "$(cat <<'EOF'
refactor: drop vk parameter from VKPoller._send_post

extract_post_content/get_author_link/get_post_link are @staticmethod
on VKClient and need no live token or API call. Calling them directly
on the class means the upcoming Long Poll listener can reuse this send
pipeline without needing a VKClient instance.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 3: Add `handle_new_published_post` to `VKPoller`

**Files:**
- Modify: `src/poller.py` (add a new method after `_poll_community`)

**Interfaces:**
- Consumes: `Database.get_community(vk_id: int) -> Optional[dict]`, `Database.update_community_last_id(vk_id: int, post_type: str, post_id: int) -> None` (both existing, `src/database.py:134,169`), `VKPoller._send_post` (Task 2).
- Produces: `VKPoller.handle_new_published_post(self, vk_id: int, post: dict) -> None`.

- [ ] **Step 1: Add the method**

In `src/poller.py`, immediately after `_poll_community` (before the `# ── Wall polling` section header), add:

```python
    # ── Entry point for the Long Poll listener (long_poll.py) ────────────────

    async def handle_new_published_post(self, vk_id: int, post: dict) -> None:
        """Called by long_poll.py when VK delivers a wall_post_new event."""
        community = await self.db.get_community(vk_id)
        if not community:
            logger.warning(f"Long Poll wall_post_new for unknown community {vk_id}, ignoring.")
            return
        topic_id = community.get("published_topic_id")
        if not topic_id:
            return
        last_known_id = community.get("last_post_id", 0)
        if post["id"] > last_known_id:
            await self.db.update_community_last_id(vk_id, "published", post["id"])
        await self._send_post(community, post, topic_id, "published")
```

- [ ] **Step 2: Write a standalone verification script**

Create `/tmp/verify_task3.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

from config import Config
from database import Database
from poller import VKPoller


async def run():
    db = Database("/tmp/verify_task3.db")
    await db.connect()
    await db.upsert_community(
        vk_id=777, name="Long Poll Test", screen_name="lpt",
        channel_id=-200, published_topic_id=11, suggested_topic_id=21,
    )

    # See Task 2's script for why config must be a real Config, not None.
    config = Config(BOT_TOKEN="test", GROUP_ID=999, ADMIN_IDS=[], COMMUNITIES=[])
    poller = VKPoller(bot=None, db=db, config=config, scheduler=None)
    fake_post = {"id": 100, "text": "new post", "attachments": [], "from_id": 1, "date": 1}

    with patch("poller.send_vk_post_to_topic", new=AsyncMock(return_value=[1])) as mock_send:
        await poller.handle_new_published_post(777, fake_post)
        assert mock_send.call_count == 1

        community = await db.get_community(777)
        assert community["last_post_id"] == 100, community["last_post_id"]

        # Re-delivery of the same event (Long Poll reconnect near a boundary,
        # or the safety-net poll picking up the same post) — must not double-send
        await poller.handle_new_published_post(777, fake_post)
        assert mock_send.call_count == 1, f"expected dedup, got {mock_send.call_count} calls"

    # Unknown community — must not raise
    await poller.handle_new_published_post(999999, fake_post)

    print("TASK 3 VERIFICATION PASSED")
    await db.close()


asyncio.run(run())
```

- [ ] **Step 3: Run it**

```bash
rm -f /tmp/verify_task3.db && python3 /tmp/verify_task3.py
```

Expected output: `TASK 3 VERIFICATION PASSED`.

- [ ] **Step 4: Delete the scratch script and commit**

```bash
rm -f /tmp/verify_task3.py /tmp/verify_task3.db
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/poller.py && \
git commit -m "$(cat <<'EOF'
feat: add VKPoller.handle_new_published_post entry point

Thin wrapper around the existing _send_post pipeline, called by the
upcoming Long Poll listener when VK delivers a wall_post_new event.
Bumps last_post_id the same way the polling path does, so the safety-
net poll's id > last_known_id diff won't re-send it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 4: Dual polling cadence

**Files:**
- Modify: `src/poller.py:61-119` (`__init__`, `start`, `_poll_all`, `_poll_community`)

**Interfaces:**
- Consumes: `Config.POLL_INTERVAL: int`, `Config.PUBLISHED_SAFETY_POLL_INTERVAL: int` (Task 1).
- Produces: `VKPoller._published_check_every_n_ticks(self) -> int`, `VKPoller._should_check_published(self, tick: int) -> bool` (both pure, no I/O — used directly by verification).

- [ ] **Step 1: Add the two pure helper methods**

In `src/poller.py`, add these two methods right after `stop()`:

```python
    def _published_check_every_n_ticks(self) -> int:
        return max(1, self.config.PUBLISHED_SAFETY_POLL_INTERVAL // self.config.POLL_INTERVAL)

    def _should_check_published(self, tick: int) -> bool:
        return tick % self._published_check_every_n_ticks() == 0
```

- [ ] **Step 2: Thread a tick counter through `start()`**

Change:

```python
    async def start(self):
        self.running = True
        logger.info(
            f"VK Poller started (interval={self.config.POLL_INTERVAL}s, "
            f"communities={[c.group_id for c in self.config.COMMUNITIES]})"
        )
        while self.running:
            try:
                await self._poll_all()
            except Exception as e:
                logger.error(f"Polling cycle error: {e}", exc_info=True)
            await asyncio.sleep(self.config.POLL_INTERVAL)
```

to:

```python
    async def start(self):
        self.running = True
        logger.info(
            f"VK Poller started (suggested interval={self.config.POLL_INTERVAL}s, "
            f"published safety-net interval={self.config.PUBLISHED_SAFETY_POLL_INTERVAL}s, "
            f"communities={[c.group_id for c in self.config.COMMUNITIES]})"
        )
        tick = 0
        while self.running:
            try:
                await self._poll_all(check_published=self._should_check_published(tick))
            except Exception as e:
                logger.error(f"Polling cycle error: {e}", exc_info=True)
            tick += 1
            await asyncio.sleep(self.config.POLL_INTERVAL)
```

- [ ] **Step 3: Thread `check_published` through `_poll_all` and `_poll_community`**

Change:

```python
    async def _poll_all(self):
        communities = await self.db.get_communities()
        for community in communities:
            # Get per-community VK token from config
            comm_cfg = self.config.get_community_config(community["vk_id"])
            if not comm_cfg:
                logger.warning(f"No config for community {community['vk_id']}, skipping.")
                continue
            try:
                async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
                    await self._poll_community(vk, community)
            except Exception as e:
                logger.error(
                    f"Error polling community {community['vk_id']}: {e}", exc_info=True
                )
            await asyncio.sleep(BETWEEN_COMMUNITIES_DELAY)

        try:
            await channel_comments.flush_stale_continuations(self.bot, self.db, self.config)
        except Exception as e:
            logger.error(f"Error flushing stale comment continuations: {e}", exc_info=True)

    async def _poll_community(self, vk: VKClient, community: dict):
        if community.get("published_topic_id"):
            await self._poll_wall(vk, community, "published")
            await asyncio.sleep(BETWEEN_WALL_TYPES_DELAY)
        if community.get("suggested_topic_id"):
            await self._poll_wall(vk, community, "suggested")
```

to:

```python
    async def _poll_all(self, check_published: bool):
        communities = await self.db.get_communities()
        for community in communities:
            # Get per-community VK token from config
            comm_cfg = self.config.get_community_config(community["vk_id"])
            if not comm_cfg:
                logger.warning(f"No config for community {community['vk_id']}, skipping.")
                continue
            try:
                async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
                    await self._poll_community(vk, community, check_published)
            except Exception as e:
                logger.error(
                    f"Error polling community {community['vk_id']}: {e}", exc_info=True
                )
            await asyncio.sleep(BETWEEN_COMMUNITIES_DELAY)

        try:
            await channel_comments.flush_stale_continuations(self.bot, self.db, self.config)
        except Exception as e:
            logger.error(f"Error flushing stale comment continuations: {e}", exc_info=True)

    async def _poll_community(self, vk: VKClient, community: dict, check_published: bool):
        # "published" now arrives in real time via VK Bots Long Poll (see
        # long_poll.py) — this periodic check only runs occasionally, as a
        # deletion-detection safety net and catch-up for anything Long Poll missed.
        if check_published and community.get("published_topic_id"):
            await self._poll_wall(vk, community, "published")
            await asyncio.sleep(BETWEEN_WALL_TYPES_DELAY)
        if community.get("suggested_topic_id"):
            await self._poll_wall(vk, community, "suggested")
```

- [ ] **Step 4: Write a standalone verification script**

Create `/tmp/verify_task4.py`:

```python
import sys
sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

from config import Config
from poller import VKPoller

config = Config(
    BOT_TOKEN="test", GROUP_ID=1, ADMIN_IDS=[], COMMUNITIES=[],
    POLL_INTERVAL=1200, PUBLISHED_SAFETY_POLL_INTERVAL=14400,
)
poller = VKPoller(bot=None, db=None, config=config, scheduler=None)

every_n = poller._published_check_every_n_ticks()
assert every_n == 12, f"expected 12 (14400 // 1200), got {every_n}"

results = [poller._should_check_published(t) for t in range(24)]
expected = [t % 12 == 0 for t in range(24)]
assert results == expected, (results, expected)
assert results[0] is True   # first tick always checks published (startup)
assert results[12] is True
assert results[1] is False

# Edge case: PUBLISHED_SAFETY_POLL_INTERVAL smaller than POLL_INTERVAL must
# not divide-by-zero or skip every tick — floor at "every tick".
config2 = Config(
    BOT_TOKEN="test", GROUP_ID=1, ADMIN_IDS=[], COMMUNITIES=[],
    POLL_INTERVAL=1200, PUBLISHED_SAFETY_POLL_INTERVAL=100,
)
poller2 = VKPoller(bot=None, db=None, config=config2, scheduler=None)
assert poller2._published_check_every_n_ticks() == 1
assert all(poller2._should_check_published(t) for t in range(5))

print("TASK 4 VERIFICATION PASSED")
```

- [ ] **Step 5: Run it**

```bash
python3 /tmp/verify_task4.py
```

Expected output: `TASK 4 VERIFICATION PASSED`.

- [ ] **Step 6: Delete the scratch script and commit**

```bash
rm -f /tmp/verify_task4.py
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/poller.py && \
git commit -m "$(cat <<'EOF'
feat: decouple published-wall safety-net poll from the suggested-queue cadence

POLL_INTERVAL now paces only the suggested-post check (no VK event
exists for it under any transport). A tick counter compares against
PUBLISHED_SAFETY_POLL_INTERVAL to decide whether a given cycle also
runs the published-wall poll, which now exists purely for deletion
detection and catching anything the Long Poll listener missed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 5: `src/long_poll.py` — the Bots Long Poll listener

**Files:**
- Create: `src/long_poll.py`

**Interfaces:**
- Consumes: `VKPoller.handle_new_published_post(vk_id: int, post: dict) -> None` (Task 3), `CommunityConfig.token: str`, `CommunityConfig.group_id: int` (existing).
- Produces: `run_long_poll(comm_cfg, poller) -> None`, `_get_long_poll_session(session, token, group_id) -> dict | None`, module constants `LONG_POLL_WAIT_SECONDS = 25`, `RECONNECT_DELAY_SECONDS = 5`.

- [ ] **Step 1: Write the module**

Create `src/long_poll.py`:

```python
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
    except Exception as e:
        logger.warning(f"groups.getLongPollServer network error for group {group_id}: {e}")
        return None
    if "error" in data:
        logger.error(f"groups.getLongPollServer failed for group {group_id}: {data['error']}")
        return None
    return data["response"]


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
            for update in data.get("updates", []):
                if update.get("type") == "wall_post_new":
                    await poller.handle_new_published_post(group_id, update.get("object", {}))
```

- [ ] **Step 2: Write a verification script covering the normal/failed=1/2/3 cases against a local mock server**

Create `/tmp/verify_task5_main.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

from aiohttp import web

import long_poll
from config import CommunityConfig


async def run():
    call_count = {"n": 0}

    async def lp_handler(request):
        call_count["n"] += 1
        n = call_count["n"]
        if n == 1:
            return web.json_response({
                "ts": "2",
                "updates": [
                    {"type": "wall_post_new", "group_id": 555, "object": {"id": 10}},
                    {"type": "message_new", "group_id": 555, "object": {"id": 999}},
                ],
            })
        if n == 2:
            return web.json_response({"failed": 1, "ts": "50"})
        if n == 3:
            return web.json_response({"failed": 2})
        if n == 4:
            return web.json_response({"failed": 3})
        # From here on, hold briefly then return an empty batch, so the
        # test's cancellation below has something in-flight to interrupt.
        await asyncio.sleep(1)
        return web.json_response({"ts": "51", "updates": []})

    app = web.Application()
    app.router.add_get("/lp", lp_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8765)
    await site.start()

    # AsyncMock(return_value=fake_session) returns this SAME dict object on
    # every "reconnect" — run_long_poll mutates lp_session["ts"] in place, so
    # ts drifts across reconnects instead of resetting to "1" each time. That's
    # harmless here: the assertions below only check dispatch and call counts,
    # not exact ts values.
    fake_session = {"key": "k", "server": "http://127.0.0.1:8765/lp", "ts": "1"}
    comm_cfg = CommunityConfig(group_id=555, name="Test", token="tok", channel_id=-1)
    poller = AsyncMock()

    with patch("long_poll._get_long_poll_session", new=AsyncMock(return_value=fake_session)) as mock_get_session:
        task = asyncio.create_task(long_poll.run_long_poll(comm_cfg, poller))
        await asyncio.sleep(2)  # let it churn through calls 1-4 and into the 5th (blocking) call
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    poller.handle_new_published_post.assert_called_once_with(555, {"id": 10})
    assert mock_get_session.call_count >= 3, (
        f"expected re-fetch on failed=2 and failed=3 (plus the initial fetch), "
        f"got {mock_get_session.call_count} session fetches"
    )

    await runner.cleanup()
    print("TASK 5 MAIN-LOOP VERIFICATION PASSED")


asyncio.run(run())
```

- [ ] **Step 3: Run it**

```bash
python3 /tmp/verify_task5_main.py
```

Expected output: `TASK 5 MAIN-LOOP VERIFICATION PASSED`.

- [ ] **Step 4: Write a second verification script covering network-error resilience**

Create `/tmp/verify_task5_network.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

import long_poll
from config import CommunityConfig


async def run():
    # Port 1 has nothing listening on it — every poll GET raises a connection error.
    fake_session = {"key": "k", "server": "http://127.0.0.1:1/lp", "ts": "1"}
    comm_cfg = CommunityConfig(group_id=555, name="Test", token="tok", channel_id=-1)
    poller = AsyncMock()

    with patch("long_poll._get_long_poll_session", new=AsyncMock(return_value=fake_session)), \
         patch("long_poll.RECONNECT_DELAY_SECONDS", 0.05):
        task = asyncio.create_task(long_poll.run_long_poll(comm_cfg, poller))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    poller.handle_new_published_post.assert_not_called()
    print("TASK 5 NETWORK-ERROR VERIFICATION PASSED")


asyncio.run(run())
```

- [ ] **Step 5: Run it**

```bash
python3 /tmp/verify_task5_network.py
```

Expected output: `TASK 5 NETWORK-ERROR VERIFICATION PASSED`. This confirms the loop survives repeated connection failures (a `ConnectionRefusedError` inside `run_long_poll`'s `try`/`except`) without crashing and without ever dispatching a post.

- [ ] **Step 6: Delete the scratch scripts and commit**

```bash
rm -f /tmp/verify_task5_main.py /tmp/verify_task5_network.py
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/long_poll.py && \
git commit -m "$(cat <<'EOF'
feat: add VK Bots Long Poll listener for published wall posts

run_long_poll opens a groups.getLongPollServer session with the
community's own token (no new token or public endpoint needed) and
loops act=a_check requests, dispatching wall_post_new events to
VKPoller.handle_new_published_post. Handles failed=1 (adopt new ts),
failed=2/3 (reconnect with a fresh session), and network errors (retry
with backoff). Not yet wired into main.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 6: Wire the Long Poll listeners into `main.py`

**Files:**
- Modify: `src/main.py`

**Interfaces:**
- Consumes: `run_long_poll(comm_cfg, poller) -> None` (Task 5).

- [ ] **Step 1: Import `run_long_poll`**

In `src/main.py`, change:

```python
import schedule_board
from config import load_config
from database import Database
from handlers import router
from poller import VKPoller
from scheduler import create_scheduler, init as init_scheduler, reload_pending_jobs
from setup import setup_communities
```

to:

```python
import schedule_board
from config import load_config
from database import Database
from handlers import router
from long_poll import run_long_poll
from poller import VKPoller
from scheduler import create_scheduler, init as init_scheduler, reload_pending_jobs
from setup import setup_communities
```

- [ ] **Step 2: Construct `VKPoller` unconditionally and start one Long Poll task per community**

Change:

```python
    # VK polling loop
    poller_task = None
    if config.POLL_ENABLED:
        poller = VKPoller(bot=bot, db=db, config=config, scheduler=scheduler)
        poller_task = asyncio.create_task(poller.start())
    else:
        logger.warning(
            "VK polling disabled (VK_POLLING_ENABLED=false) — the bot will still "
            "handle Telegram commands and fire already-scheduled publications, "
            "but won't fetch new VK posts."
        )

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot, db=db, config=config, scheduler=scheduler)
    finally:
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
```

to:

```python
    # VK polling loop + Long Poll listeners share one VKPoller instance — the
    # listeners need handle_new_published_post() even when
    # VK_POLLING_ENABLED=false, so the poller is always constructed.
    poller = VKPoller(bot=bot, db=db, config=config, scheduler=scheduler)
    poller_task = None
    if config.POLL_ENABLED:
        poller_task = asyncio.create_task(poller.start())
    else:
        logger.warning(
            "VK polling disabled (VK_POLLING_ENABLED=false) — the bot will still "
            "handle Telegram commands and fire already-scheduled publications, "
            "but won't fetch new VK posts. VK Bots Long Poll still runs."
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
```

- [ ] **Step 3: Verify the wiring without touching Telegram or the real VK API**

This step deliberately does **not** run `python src/main.py` or call `dp.start_polling` — see the Global Constraints note about not creating a second `getUpdates` consumer on the production bot token. It also avoids any real call to `api.vk.com` by patching `long_poll._get_long_poll_session` (the same technique Task 5 used), so it exercises exactly the lines Step 2 added — building one task per community and cleanly cancelling all of them — without any external dependency.

Create `/tmp/verify_task6.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

import long_poll
from config import Config, CommunityConfig


async def run():
    config = Config(
        BOT_TOKEN="test", GROUP_ID=1, ADMIN_IDS=[],
        COMMUNITIES=[
            CommunityConfig(group_id=1, name="A", token="t1", channel_id=-1),
            CommunityConfig(group_id=2, name="B", token="t2", channel_id=-2),
        ],
    )
    fake_poller = MagicMock()

    with patch("long_poll._get_long_poll_session", new=AsyncMock(return_value=None)), \
         patch("long_poll.RECONNECT_DELAY_SECONDS", 0.05):
        long_poll_tasks = [
            asyncio.create_task(long_poll.run_long_poll(comm_cfg, fake_poller))
            for comm_cfg in config.COMMUNITIES
        ]
        await asyncio.sleep(0.3)  # let both tasks retry a few times independently

        for t in long_poll_tasks:
            t.cancel()
        for t in long_poll_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    print("TASK 6 VERIFICATION PASSED")


asyncio.run(run())
```

- [ ] **Step 4: Run it**

```bash
python3 /tmp/verify_task6.py
```

Expected output: `TASK 6 VERIFICATION PASSED`. This confirms the exact "one task per community, cancel-and-await-all-in-`finally`" pattern `main.py` now uses works cleanly for multiple concurrent communities, with no real network or Telegram dependency.

- [ ] **Step 5: Read through `main.py`'s diff once more by eye**

Confirm: `poller` is now constructed before the `if config.POLL_ENABLED` branch (not inside it), `long_poll_tasks` is built right after that branch, and its cancellation loop runs before `poller_task`'s cancellation in `finally` (order between the two doesn't matter functionally since they're independent, but cancelling before awaiting, for every task, before moving on, does — the diff above cancels all Long Poll tasks first, then awaits each, matching the existing single-task pattern already used for `poller_task`). No automated check substitutes for this — it's a small diff and the risk (a task leaking past shutdown, or `VKPoller` being constructed twice) is easy to eyeball but awkward to assert from outside.

- [ ] **Step 6: Delete the scratch script and commit**

```bash
rm -f /tmp/verify_task6.py
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/main.py && \
git commit -m "$(cat <<'EOF'
feat: start VK Bots Long Poll listeners alongside the bot

VKPoller is now always constructed (the Long Poll listeners need
handle_new_published_post() even with VK_POLLING_ENABLED=false). One
run_long_poll task starts per community; all are cancelled and awaited
in the shutdown path before the wall.get poller task.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

## What this plan does NOT cover (by design)

Per the spec's "Deployment / infra steps" section — explicitly out of scope until the user asks to move to that phase:

- Toggling Long Poll API to "Включено" in each of the 3 communities' VK admin panels
- Checking "Записи на стене" in each community's "Типы событий" tab
- Any changes on `144.31.102.35`

After Task 6, the code is complete and locally verified, but no VK community has Long Poll enabled yet, so `run_long_poll` will get real "Long Poll is disabled" or similar errors from VK until that manual step happens — safe to sit as-is until the infra phase.
