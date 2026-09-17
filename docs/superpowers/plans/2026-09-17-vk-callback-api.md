> **Superseded 2026-09-17 — never executed.** The design it implements
> (`docs/superpowers/specs/2026-09-16-vk-callback-api-design.md`) was
> replaced by a VK Long Poll design before Task 1 was dispatched — the
> execution worktree was discarded with zero commits. See
> `docs/superpowers/plans/2026-09-17-vk-long-poll.md` for the current plan.
> Kept for history only.

# VK Callback API for Published Posts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect new published VK wall posts via VK's Callback API (`wall_post_new` push events) instead of frequent polling, freeing up API quota to poll the suggested-post queue more often — while keeping a low-frequency safety-net poll for deletion detection and missed-webhook recovery.

**Architecture:** A new `src/callback_server.py` aiohttp app runs as a third concurrent asyncio task inside the existing `vk_tg_bot` process (alongside aiogram's Telegram polling and `VKPoller.start()`). It verifies each VK event's `secret` against the per-community value in `communities.json`, then calls a new `VKPoller.handle_new_published_post()` method that reuses the existing send/dedup pipeline. `VKPoller`'s own loop gains a second, much longer cadence (`PUBLISHED_SAFETY_POLL_INTERVAL`) for a periodic "published" wall check that now exists only for deletion detection and catch-up — new-post delivery for "published" no longer depends on it.

**Tech Stack:** Python 3.11, aiohttp (already a dependency via aiogram/requirements.txt), aiosqlite, no test framework (project has none — verification below is via standalone scripts run manually, matching existing project convention).

**Spec:** `docs/superpowers/specs/2026-09-16-vk-callback-api-design.md`

## Global Constraints

- This repo has no test suite, linter, or formatter configured — do not add one. Verification steps below are standalone `python3` scripts run manually, not `pytest`.
- All commands run with `src/` as the working directory (project convention — imports use bare module names, no package prefix).
- **This phase is local-only.** Do not touch the production VPS (`144.31.102.35`), do not run `docker compose` there, and do not perform any of the spec's "Deployment / infra steps" (DNS, certbot, nginx, VK admin panel). That is a separate later phase.
- **Never run `python src/main.py` against the real `.env`'s `BOT_TOKEN`.** The production bot on the VPS is already polling Telegram with that same token — a second local `getUpdates` consumer on the same token will conflict with it (Telegram allows only one long-poll consumer per bot token) and can visibly disrupt the live bot. Every verification step below that needs a running process either avoids `dp.start_polling` entirely or uses a fresh throwaway `Config`/DB, never the checked-in `.env`.
- VK's account used by this project is currently quota-exhausted for the month (see spec's Problem section) — no verification step may depend on a live call to `api.vk.com`. All VK interactions are simulated with synthetic post/event dicts.

---

## File Structure

- `src/config.py` — modified: `CommunityConfig` gains `callback_secret`/`callback_confirmation`; `Config` gains `PUBLISHED_SAFETY_POLL_INTERVAL`/`CALLBACK_SERVER_PORT`; `load_config()` wires both.
- `src/poller.py` — modified: `_send_post` drops its `vk` parameter (calls `VKClient`'s static helpers directly); new `handle_new_published_post()` method; polling loop gains a tick-based dual cadence (`_should_check_published`).
- `src/callback_server.py` — new: the aiohttp webhook app (`create_callback_app`, `start_callback_server`, route handlers).
- `src/main.py` — modified: constructs `VKPoller` unconditionally (was only inside the `POLL_ENABLED` branch), starts the callback server as a third task, cleans it up in `finally`.
- `communities.json`, `.env.example`, `README.md` — modified: document/add the two new per-community fields and two new env vars.

## Interfaces (for reference across tasks)

- `VKPoller.handle_new_published_post(self, vk_id: int, post: dict) -> None` — entry point the webhook calls.
- `VKPoller._send_post(self, community: dict, post: dict, topic_id: int, post_type: str) -> None` — unchanged behavior, `vk` parameter removed.
- `VKPoller._should_check_published(self, tick: int) -> bool` — pure function, no I/O.
- `create_callback_app(config: Config, poller: VKPoller) -> aiohttp.web.Application`
- `start_callback_server(config: Config, poller: VKPoller) -> aiohttp.web.AppRunner`
- `Config.get_community_config(self, group_id: int) -> CommunityConfig | None` — already exists, used by the webhook to find `callback_secret`/`callback_confirmation`.
- `Database.get_community(self, vk_id: int) -> Optional[dict]` — already exists, used by `handle_new_published_post` to get the DB-shape community row (topic ids, `last_post_id`).

---

### Task 1: Config schema for Callback API

**Files:**
- Modify: `src/config.py`
- Modify: `communities.json`
- Modify: `.env.example`
- Modify: `README.md:41-53,75-92`

**Interfaces:**
- Produces: `CommunityConfig.callback_secret: str`, `CommunityConfig.callback_confirmation: str`, `Config.PUBLISHED_SAFETY_POLL_INTERVAL: int`, `Config.CALLBACK_SERVER_PORT: int`.

- [ ] **Step 1: Add the two new `CommunityConfig` fields**

In `src/config.py`, change:

```python
@dataclass
class CommunityConfig:
    group_id: int
    name: str
    token: str        # community/group token (for posting on behalf of group)
    channel_id: int
    user_token: str = ""  # admin user token (required for wall.get + suggests)
```

to:

```python
@dataclass
class CommunityConfig:
    group_id: int
    name: str
    token: str        # community/group token (for posting on behalf of group)
    channel_id: int
    user_token: str = ""  # admin user token (required for wall.get + suggests)
    callback_secret: str = ""        # VK Callback API "Секретный ключ" for this community
    callback_confirmation: str = ""  # VK's one-time confirmation string for this community
```

- [ ] **Step 2: Add the two new `Config` fields and lower `POLL_INTERVAL`'s default**

Change:

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
    CALLBACK_SERVER_PORT: int = 8090
```

(The dataclass field default of `300` above is unreachable in practice — `load_config()` always passes an explicit value — but leave it as a fallback; Step 4 changes what `load_config()` actually passes.)

- [ ] **Step 3: Wire the two new `CommunityConfig` fields into `load_config()`**

Change:

```python
            communities.append(
                CommunityConfig(
                    group_id=int(item["group_id"]),
                    name=item.get("name", f"Community {item['group_id']}"),
                    token=item["token"],
                    channel_id=int(item["channel_id"]),
                    user_token=item.get("user_token", global_user_token),
                )
            )
```

to:

```python
            communities.append(
                CommunityConfig(
                    group_id=int(item["group_id"]),
                    name=item.get("name", f"Community {item['group_id']}"),
                    token=item["token"],
                    channel_id=int(item["channel_id"]),
                    user_token=item.get("user_token", global_user_token),
                    callback_secret=item.get("callback_secret", ""),
                    callback_confirmation=item.get("callback_confirmation", ""),
                )
            )
```

- [ ] **Step 4: Wire the two new `Config` fields into `load_config()`, lower `POLL_INTERVAL`'s effective default to 1200s**

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
        # POLL_INTERVAL now only paces the "suggested" queue (no Callback event
        # exists for it). Published posts arrive via VK Callback API webhook
        # instead; PUBLISHED_SAFETY_POLL_INTERVAL below is just a slow safety
        # net for deletion detection and catching any missed webhook delivery.
        POLL_INTERVAL=int(os.environ.get("POLL_INTERVAL", "1200")),
        PUBLISHED_SAFETY_POLL_INTERVAL=int(os.environ.get("PUBLISHED_SAFETY_POLL_INTERVAL", "14400")),
        CALLBACK_SERVER_PORT=int(os.environ.get("CALLBACK_SERVER_PORT", "8090")),
        POLL_ENABLED=os.environ.get("VK_POLLING_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
        INITIAL_POSTS_COUNT=int(os.environ.get("INITIAL_POSTS_COUNT", "10")),
        DB_PATH=os.environ.get("DB_PATH", "/app/data/bot.db"),
        TIMEZONE=os.environ.get("TIMEZONE", "Europe/Moscow"),
        COMMUNITIES_FILE=communities_file,
    )
```

- [ ] **Step 5: Add placeholder fields to the local `communities.json`**

Read `communities.json` and add `"callback_secret": ""` and `"callback_confirmation": ""` to each of the 3 existing entries (values stay empty — they get filled in during the later infra phase per the spec's chicken-and-egg deployment note; empty strings are valid defaults the code already handles).

- [ ] **Step 6: Document the new fields in `.env.example`**

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
# Интервал опроса ПРЕДЛОЖКИ (filter=suggests) в секундах — у VK нет Callback-
# события для неё, так что она остаётся на обычном опросе. По умолчанию 20 мин.
POLL_INTERVAL=1200

# Интервал редкого "safety net" опроса ОПУБЛИКОВАННОЙ стены — используется
# только для проверки удалений (у VK нет события wall_post_delete) и для
# подстраховки на случай, если вебхук от VK не долетел. Новые опубликованные
# посты обнаруживаются мгновенно через VK Callback API (wall_post_new),
# а не через этот интервал. По умолчанию 4 часа.
PUBLISHED_SAFETY_POLL_INTERVAL=14400

# Порт, на котором слушает локальный HTTP-сервер для VK Callback API
# (nginx на сервере проксирует внешний HTTPS-адрес на этот порт).
CALLBACK_SERVER_PORT=8090
```

- [ ] **Step 7: Update the README's `.env` and `communities.json` tables**

In `README.md`, change the `.env` table row:

```
| `POLL_INTERVAL` | Интервал опроса VK в секундах (по умолчанию `300`) |
```

to two rows:

```
| `POLL_INTERVAL` | Интервал опроса предложки VK в секундах (по умолчанию `1200`) |
| `PUBLISHED_SAFETY_POLL_INTERVAL` | Редкий safety-net опрос опубликованной стены — только для проверки удалений (по умолчанию `14400`) |
| `CALLBACK_SERVER_PORT` | Порт локального HTTP-сервера для VK Callback API (по умолчанию `8090`) |
```

And change the `communities.json` example block:

```json
[
  {
    "group_id": 123456789,
    "name": "Моё сообщество",
    "token": "vk_community_token",
    "channel_id": -1001234567890,
    "user_token": "vk_user_admin_token"
  }
]
```

to:

```json
[
  {
    "group_id": 123456789,
    "name": "Моё сообщество",
    "token": "vk_community_token",
    "channel_id": -1001234567890,
    "user_token": "vk_user_admin_token",
    "callback_secret": "vk_callback_secret_key",
    "callback_confirmation": "vk_callback_confirmation_string"
  }
]
```

and its field table:

```
| `user_token` | Токен пользователя-администратора VK (нужен для чтения стены и предложки). Получить: [vkhost.github.io](https://vkhost.github.io/) → `wall` + `groups` |
```

to add two rows after it:

```
| `user_token` | Токен пользователя-администратора VK (нужен для чтения стены и предложки). Получить: [vkhost.github.io](https://vkhost.github.io/) → `wall` + `groups` |
| `callback_secret` | "Секретный ключ" из настроек Callback API этого сообщества в VK — проверяется на каждом входящем вебхуке |
| `callback_confirmation` | Строка подтверждения, которую VK показывает при добавлении Callback-сервера — сервер отвечает ею на событие `confirmation` |
```

- [ ] **Step 8: Verify config loads correctly with the new fields**

Run (from the `src/` directory):

```bash
cd "/Users/andrewsaltanov/Desktop/Learning/VK post /src" && \
COMMUNITIES_FILE="../communities.json" python3 -c "
from config import load_config
c = load_config()
print('POLL_INTERVAL =', c.POLL_INTERVAL)
print('PUBLISHED_SAFETY_POLL_INTERVAL =', c.PUBLISHED_SAFETY_POLL_INTERVAL)
print('CALLBACK_SERVER_PORT =', c.CALLBACK_SERVER_PORT)
for comm in c.COMMUNITIES:
    print(comm.group_id, repr(comm.callback_secret), repr(comm.callback_confirmation))
"
```

Expected output: `POLL_INTERVAL = 1200`, `PUBLISHED_SAFETY_POLL_INTERVAL = 14400`, `CALLBACK_SERVER_PORT = 8090`, and 3 lines each showing `''` `''` for the two new fields (this reads the real `.env` in the project root for `BOT_TOKEN`/`GROUP_ID`/`ADMIN_IDS` via `load_dotenv()`, but only *reads* it — no network call happens).

- [ ] **Step 9: Commit**

```bash
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/config.py communities.json .env.example README.md && \
git commit -m "$(cat <<'EOF'
feat: add config fields for VK Callback API

CommunityConfig gains callback_secret/callback_confirmation (per-
community, used by the upcoming webhook receiver); Config gains
PUBLISHED_SAFETY_POLL_INTERVAL and CALLBACK_SERVER_PORT. POLL_INTERVAL's
effective default drops to 1200s now that it only paces the suggested-
post queue.

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
on the class means the upcoming webhook handler can reuse this send
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
    # ── Webhook entry point (VK Callback API) ────────────────────────────────

    async def handle_new_published_post(self, vk_id: int, post: dict) -> None:
        """Called by callback_server.py when VK pushes a wall_post_new event."""
        community = await self.db.get_community(vk_id)
        if not community:
            logger.warning(f"Webhook wall_post_new for unknown community {vk_id}, ignoring.")
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
        vk_id=777, name="Webhook Test", screen_name="wt",
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

        # Re-delivery of the same event (VK retry) — must not double-send
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
feat: add VKPoller.handle_new_published_post webhook entry point

Thin wrapper around the existing _send_post pipeline, called by the
upcoming Callback API server when VK pushes a wall_post_new event.
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
        # "published" now arrives in real time via VK Callback API (see
        # callback_server.py) — this periodic check only runs occasionally,
        # as a deletion-detection safety net and catch-up for missed webhooks.
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

POLL_INTERVAL now paces only the suggested-post check (no Callback event
exists for it). A tick counter compares against
PUBLISHED_SAFETY_POLL_INTERVAL to decide whether a given cycle also runs
the published-wall poll, which now exists purely for deletion detection
and catching any webhook VK failed to deliver.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 5: `src/callback_server.py`

**Files:**
- Create: `src/callback_server.py`

**Interfaces:**
- Consumes: `Config.get_community_config(group_id: int) -> CommunityConfig | None` (existing), `VKPoller.handle_new_published_post(vk_id: int, post: dict) -> None` (Task 3).
- Produces: `create_callback_app(config, poller) -> aiohttp.web.Application`, `start_callback_server(config, poller) -> aiohttp.web.AppRunner`.

- [ ] **Step 1: Write the module**

Create `src/callback_server.py`:

```python
"""
VK Callback API webhook receiver for published wall posts.

Runs as a standalone aiohttp app alongside the bot's Telegram polling and
VK poller (wired up in main.py). See:
docs/superpowers/specs/2026-09-16-vk-callback-api-design.md
"""
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


def create_callback_app(config, poller) -> web.Application:
    app = web.Application()
    app["config"] = config
    app["poller"] = poller
    app.router.add_post("/vk-callback", handle_callback)
    app.router.add_get("/health", handle_health)
    return app


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_callback(request: web.Request) -> web.Response:
    config = request.app["config"]
    poller = request.app["poller"]

    try:
        body = await request.json()
    except Exception:
        logger.warning("VK callback: could not parse JSON body")
        return web.Response(text="ok")

    group_id = body.get("group_id")
    community = config.get_community_config(group_id) if group_id else None
    if not community:
        logger.warning(f"VK callback: unknown group_id={group_id}")
        return web.Response(text="ok")

    event_type = body.get("type")

    # "confirmation" is VK's one-time ownership check when adding a Callback
    # server — it happens before a secret key is even set, so it's answered
    # unconditionally, without a secret check.
    if event_type == "confirmation":
        return web.Response(text=community.callback_confirmation)

    if body.get("secret") != community.callback_secret:
        logger.warning(
            f"VK callback: secret mismatch for group_id={group_id} — ignoring event"
        )
        return web.Response(text="ok")

    if event_type == "wall_post_new":
        post = body.get("object", {})
        await poller.handle_new_published_post(group_id, post)
        return web.Response(text="ok")

    # Any other confirmed event type (group_join, wall_reply_new, etc.) —
    # VK may deliver these even if we only asked for wall_post_new in the
    # dashboard; ignoring them keeps this handler forward-compatible.
    return web.Response(text="ok")


async def start_callback_server(config, poller) -> web.AppRunner:
    app = create_callback_app(config, poller)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.CALLBACK_SERVER_PORT)
    await site.start()
    logger.info(f"VK Callback server listening on 127.0.0.1:{config.CALLBACK_SERVER_PORT}")
    return runner
```

- [ ] **Step 2: Write a standalone verification script**

Create `/tmp/verify_task5.py`:

```python
import asyncio
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

from aiohttp.test_utils import TestClient, TestServer

from callback_server import create_callback_app
from config import Config, CommunityConfig


async def run():
    config = Config(
        BOT_TOKEN="test", GROUP_ID=1, ADMIN_IDS=[],
        COMMUNITIES=[
            CommunityConfig(
                group_id=111, name="Test", token="t", channel_id=-1,
                callback_secret="s3cr3t", callback_confirmation="conf-str",
            ),
        ],
    )
    poller = AsyncMock()
    app = create_callback_app(config, poller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 1. Malformed JSON — ack'd, nothing dispatched
        resp = await client.post("/vk-callback", data="not json")
        assert resp.status == 200 and await resp.text() == "ok"
        poller.handle_new_published_post.assert_not_called()

        # 2. Unknown group_id — ack'd, nothing dispatched
        resp = await client.post(
            "/vk-callback",
            json={"type": "wall_post_new", "group_id": 999, "secret": "x", "object": {}},
        )
        assert await resp.text() == "ok"
        poller.handle_new_published_post.assert_not_called()

        # 3. Confirmation — answered regardless of secret
        resp = await client.post("/vk-callback", json={"type": "confirmation", "group_id": 111})
        assert await resp.text() == "conf-str", await resp.text()

        # 4. Wrong secret on a real event type — ignored
        resp = await client.post(
            "/vk-callback",
            json={"type": "wall_post_new", "group_id": 111, "secret": "wrong", "object": {"id": 5}},
        )
        assert await resp.text() == "ok"
        poller.handle_new_published_post.assert_not_called()

        # 5. Correct secret + wall_post_new — dispatched
        resp = await client.post(
            "/vk-callback",
            json={"type": "wall_post_new", "group_id": 111, "secret": "s3cr3t", "object": {"id": 5}},
        )
        assert await resp.text() == "ok"
        poller.handle_new_published_post.assert_called_once_with(111, {"id": 5})

        # 6. Unrelated confirmed event type — ignored
        poller.handle_new_published_post.reset_mock()
        resp = await client.post(
            "/vk-callback",
            json={"type": "group_join", "group_id": 111, "secret": "s3cr3t"},
        )
        assert await resp.text() == "ok"
        poller.handle_new_published_post.assert_not_called()

        # 7. Health check
        resp = await client.get("/health")
        assert resp.status == 200 and (await resp.json())["status"] == "ok"

        print("TASK 5 VERIFICATION PASSED")
    finally:
        await client.close()


asyncio.run(run())
```

- [ ] **Step 3: Run it**

```bash
python3 /tmp/verify_task5.py
```

Expected output: `TASK 5 VERIFICATION PASSED`.

- [ ] **Step 4: Delete the scratch script and commit**

```bash
rm -f /tmp/verify_task5.py
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/callback_server.py && \
git commit -m "$(cat <<'EOF'
feat: add VK Callback API webhook receiver

New aiohttp app (create_callback_app/start_callback_server) that
verifies each event's secret against the per-community value, answers
VK's one-time confirmation check, and dispatches wall_post_new events
to VKPoller.handle_new_published_post. Not yet wired into main.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

### Task 6: Wire the callback server into `main.py`

**Files:**
- Modify: `src/main.py`

**Interfaces:**
- Consumes: `start_callback_server(config, poller) -> aiohttp.web.AppRunner` (Task 5).

- [ ] **Step 1: Import `start_callback_server`**

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
from callback_server import start_callback_server
from config import load_config
from database import Database
from handlers import router
from poller import VKPoller
from scheduler import create_scheduler, init as init_scheduler, reload_pending_jobs
from setup import setup_communities
```

- [ ] **Step 2: Construct `VKPoller` unconditionally and start the callback server**

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
    # VK polling loop + Callback API webhook receiver share one VKPoller
    # instance — the webhook needs handle_new_published_post() even when
    # VK_POLLING_ENABLED=false, so the poller is always constructed.
    poller = VKPoller(bot=bot, db=db, config=config, scheduler=scheduler)
    poller_task = None
    if config.POLL_ENABLED:
        poller_task = asyncio.create_task(poller.start())
    else:
        logger.warning(
            "VK polling disabled (VK_POLLING_ENABLED=false) — the bot will still "
            "handle Telegram commands and fire already-scheduled publications, "
            "but won't fetch new VK posts. The Callback API webhook still runs."
        )

    callback_runner = await start_callback_server(config, poller)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot, db=db, config=config, scheduler=scheduler)
    finally:
        await callback_runner.cleanup()
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

- [ ] **Step 3: Verify the wiring without touching Telegram**

This step deliberately does **not** run `python src/main.py` or call `dp.start_polling` — see the Global Constraints note about not creating a second `getUpdates` consumer on the production bot token. Instead, verify just the new lines (`start_callback_server` actually binds and serves) in isolation.

Create `/tmp/verify_task6.py`:

```python
import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, "/Users/andrewsaltanov/Desktop/Learning/VK post /src")

import aiohttp

from callback_server import start_callback_server
from config import Config


async def run():
    config = Config(
        BOT_TOKEN="test", GROUP_ID=1, ADMIN_IDS=[], COMMUNITIES=[],
        CALLBACK_SERVER_PORT=8099,
    )
    fake_poller = MagicMock()
    runner = await start_callback_server(config, fake_poller)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:8099/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data == {"status": "ok"}, data
        print("TASK 6 VERIFICATION PASSED")
    finally:
        await runner.cleanup()


asyncio.run(run())
```

- [ ] **Step 4: Run it**

```bash
python3 /tmp/verify_task6.py
```

Expected output: `TASK 6 VERIFICATION PASSED`. This confirms `start_callback_server` binds a real TCP port and serves real HTTP responses — the same call `main.py` now makes — without involving `Bot`, `Dispatcher`, or any Telegram network call.

- [ ] **Step 5: Read through `main.py`'s diff once more by eye**

Confirm: `poller` is now constructed before the `if config.POLL_ENABLED` branch (not inside it), `callback_runner` is awaited before the `try`, and `await callback_runner.cleanup()` is the first line in `finally` (so it releases port `CALLBACK_SERVER_PORT` even if something below it raises). No automated check substitutes for this — it's a 6-line diff and the risk (holding the port open, or constructing `VKPoller` twice) is easy to eyeball but awkward to assert from outside.

- [ ] **Step 6: Delete the scratch script and commit**

```bash
rm -f /tmp/verify_task6.py
cd "/Users/andrewsaltanov/Desktop/Learning/VK post " && \
git add src/main.py && \
git commit -m "$(cat <<'EOF'
feat: start the VK Callback API webhook server alongside the bot

VKPoller is now always constructed (the webhook needs
handle_new_published_post() even with VK_POLLING_ENABLED=false).
start_callback_server() runs as a third concurrent task; its runner is
cleaned up first in the shutdown path.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SLHKwvTVhGX4idHHfZA9NU
EOF
)"
```

---

## What this plan does NOT cover (by design)

Per the spec's "Deployment / infra steps" section — explicitly out of scope until the user asks to move to that phase:

- DNS record for `ugnestbot.aszhuloong.ru`
- `certbot` / nginx server block on the VPS
- Adding a new Callback server + entering the confirmation string + secret key in each of the 3 communities' VK admin panels
- Deploying any of this to `144.31.102.35`

After Task 6, the code is complete and locally verified, but the callback server is not reachable from the internet and no VK community knows it exists yet — safe to sit as-is until the infra phase.
