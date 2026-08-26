# UX Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add six UX improvements to the VK-to-Telegram bridge bot: prominent quick-publish button, reschedule button, automatic VK content refresh at publish time, `/autoqueue` bulk scheduling, enhanced `/status`, and per-community notification muting.

**Architecture:** All features extend the existing aiogram 3.7 / APScheduler / aiosqlite stack. New DB methods are added to `database.py`; new callbacks to `callbacks.py`; keyboard changes to `keyboards.py`; new handlers to `handlers.py`; scheduler gains VK API access. No new top-level modules required.

**Tech Stack:** Python 3.11+, aiogram 3.7, aiosqlite, APScheduler 3.x, aiohttp (VK API), zoneinfo

**Spec:** `docs/superpowers/specs/2026-08-26-ux-improvements-design.md`

## Global Constraints

- All Telegram text is in Russian
- Parse mode is HTML everywhere (set as bot default)
- Admin check via `_is_admin(user_id, config)` before all command handlers
- Timezone from `config.TIMEZONE` (ZoneInfo), default `Europe/Moscow`
- VK community IDs stored positive; negated with `-abs()` when calling VK API
- No test framework exists — verification is manual (SQLite queries + Telegram interaction)
- Working directory for `python src/main.py` must be `src/` or Python path must include it

---

## File Map

| File | Role in this plan |
|---|---|
| `src/callbacks.py` | Add `RescheduleCallback` |
| `src/keyboards.py` | Move «Сейчас» to top row; add reschedule button to scheduled badge |
| `src/handlers.py` | Add reschedule handler, extract `_schedule_post_core()`, add `/autoqueue`, update `/status`, add `/mute` `/unmute`, update `/help` |
| `src/scheduler.py` | Add VK content check before publish; add mute check before notification |
| `src/database.py` | Add 7 new methods + migration v3 |
| `src/vk_client.py` | Add `fetch_post_data()` method |

---

## Task 1: Feature 6 — Prominent Quick-Publish Button

**Files:**
- Modify: `src/keyboards.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `get_schedule_keyboard()` / `get_manual_post_keyboard()` — same signatures, new visual layout. First row is now a single full-width «⚡️ Опубликовать сейчас» button; remaining options in 4-per-row grid.

- [ ] **Step 1: Open `src/keyboards.py` and locate `SCHEDULE_OPTIONS` and `_schedule_rows()`**

Current `SCHEDULE_OPTIONS` starts with `("⚡️ Сейчас", 0)` followed by 10 timed options.

- [ ] **Step 2: Remove the `("⚡️ Сейчас", 0)` entry from `SCHEDULE_OPTIONS`**

```python
SCHEDULE_OPTIONS = [
    ("30 мин",  30),
    ("1 ч",     60),
    ("1.5 ч",   90),
    ("2 ч",     120),
    ("2.5 ч",   150),
    ("3 ч",     180),
    ("3.5 ч",   210),
    ("4 ч",     240),
    ("4.5 ч",   270),
    ("5 ч",     300),
]
```

- [ ] **Step 3: Update `_schedule_rows()` to prepend the «Сейчас» button as its own full-width row before the grid**

```python
def _schedule_rows(builder: InlineKeyboardBuilder, post_db_id: int) -> None:
    """Add schedule time option buttons and a custom-time row."""
    # Full-width «Сейчас» button first
    builder.row(
        InlineKeyboardButton(
            text="⚡️ Опубликовать сейчас",
            callback_data=ScheduleCallback(post_db_id=post_db_id, option="0").pack(),
        )
    )
    # Time-grid: 4 per row
    row: list[InlineKeyboardButton] = []
    for label, minutes in SCHEDULE_OPTIONS:
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=ScheduleCallback(
                    post_db_id=post_db_id, option=str(minutes)
                ).pack(),
            )
        )
        if len(row) == 4:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text="🕐 Своё время",
            callback_data=ScheduleCallback(
                post_db_id=post_db_id, option="custom"
            ).pack(),
        )
    )
```

- [ ] **Step 4: Verify layout manually**

Start the bot locally (`cd src && python main.py`), trigger a new VK post appearing in the topic, check the keyboard: first row should be a single wide «⚡️ Опубликовать сейчас» button, then rows of 4 timed options, then «🕐 Своё время».

- [ ] **Step 5: Commit**

```bash
git add src/keyboards.py
git commit -m "feat: move quick-publish button to prominent first row"
```

---

## Task 2: Feature 1 — Reschedule Button and Handler

**Files:**
- Modify: `src/callbacks.py`
- Modify: `src/keyboards.py`
- Modify: `src/handlers.py`

**Interfaces:**
- Consumes: `get_schedule_keyboard(post_db_id)` from `keyboards.py` (already exists)
- Produces:
  - `RescheduleCallback(post_db_id: int, record_id: int)` — new callback class in `callbacks.py`
  - `get_scheduled_badge(post_db_id, unix_ts, timezone, record_id)` — updated to include reschedule row
  - `handle_reschedule(callback, callback_data, db, bot, config)` — new handler

- [ ] **Step 1: Add `RescheduleCallback` to `src/callbacks.py`**

```python
class RescheduleCallback(CallbackData, prefix="reschedule"):
    post_db_id: int
    record_id: int
```

- [ ] **Step 2: Import `RescheduleCallback` in `src/keyboards.py`**

Add `RescheduleCallback` to the import from `callbacks`.

- [ ] **Step 3: Update `get_scheduled_badge()` to include reschedule button**

Current signature: `get_scheduled_badge(post_db_id, unix_ts, timezone, record_id=None) -> InlineKeyboardMarkup`

New layout (3 rows when `record_id` is provided):
```
Row 1: ✅ Запланировано на DD.MM HH:MM  (info, SchedInfoCallback)
Row 2: ❌ Отменить публикацию            (CancelSchedCallback)
Row 3: 📅 Перенести                      (RescheduleCallback)
```

```python
def get_scheduled_badge(
    post_db_id: int,
    unix_ts: int,
    timezone: str = "Europe/Moscow",
    record_id: int | None = None,
) -> InlineKeyboardMarkup:
    dt = datetime.fromtimestamp(unix_ts, tz=ZoneInfo(timezone))
    time_str = dt.strftime("%d.%m %H:%M")
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Запланировано на {time_str}",
        callback_data=SchedInfoCallback(post_db_id=post_db_id, unix_ts=unix_ts).pack(),
    )
    if record_id is not None:
        builder.button(
            text="❌ Отменить публикацию",
            callback_data=CancelSchedCallback(record_id=record_id).pack(),
        )
        builder.button(
            text="📅 Перенести",
            callback_data=RescheduleCallback(
                post_db_id=post_db_id, record_id=record_id
            ).pack(),
        )
        builder.adjust(1)  # one button per row
    return builder.as_markup()
```

- [ ] **Step 4: Import `RescheduleCallback` in `src/handlers.py`**

Add to the `from callbacks import (...)` block.

- [ ] **Step 5: Add `handle_reschedule()` handler in `src/handlers.py`**

Place it after `handle_cancel_sched`. This handler edits the post's keyboard message back to the full schedule picker:

```python
@router.callback_query(RescheduleCallback.filter())
async def handle_reschedule(
    callback: CallbackQuery,
    callback_data: RescheduleCallback,
    db: Database,
    bot: Bot,
    config: Config,
):
    if not _is_admin(callback.from_user.id, config):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    post = await db.get_post_by_id(callback_data.post_db_id)
    if not post:
        await callback.answer("❌ Пост не найден", show_alert=True)
        return

    tg_msg_id = post.get("tg_message_id")
    if not tg_msg_id:
        await callback.answer("❌ Сообщение поста не найдено", show_alert=True)
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=config.GROUP_ID,
            message_id=tg_msg_id,
            reply_markup=get_schedule_keyboard(callback_data.post_db_id),
        )
        await callback.answer("Выберите новое время")
    except Exception as e:
        logger.warning(f"Could not show reschedule picker: {e}")
        await callback.answer("❌ Не удалось открыть пикер времени", show_alert=True)
```

Also add `get_schedule_keyboard` to the keyboards import if not already present.

- [ ] **Step 6: Verify manually**

Schedule a post → see the badge with «📅 Перенести» → press it → verify the full schedule picker replaces the badge → pick a new time → verify badge shows new time, board updates, and old APScheduler job is replaced.

- [ ] **Step 7: Commit**

```bash
git add src/callbacks.py src/keyboards.py src/handlers.py
git commit -m "feat: add reschedule button to scheduled post badge"
```

---

## Task 3: DB — New Query Methods (prereq for Tasks 5–8)

**Files:**
- Modify: `src/database.py`

**Interfaces:**
- Produces (all `async` methods on `Database`):
  - `update_post_content_json(post_id: int, content_json: str) -> None`
  - `update_scheduled_post_content_json(record_id: int, content_json: str) -> None`
  - `get_community_by_topic_id(topic_id: int) -> Optional[dict]`
  - `get_unscheduled_posts_for_topic(topic_id: int) -> List[dict]`
  - `get_pending_suggested_count(community_id: int) -> int`
  - `get_published_count_since(community_id: int, since_ts: int) -> int`
  - `set_notifications_muted(vk_id: int, muted: bool) -> None`

- [ ] **Step 1: Add `update_post_content_json` and `update_scheduled_post_content_json`**

Append to `src/database.py` after the existing scheduled posts section:

```python
async def update_post_content_json(self, post_id: int, content_json: str):
    await self._conn.execute(
        "UPDATE posts SET content_json = ? WHERE id = ?", (content_json, post_id)
    )
    await self._conn.commit()

async def update_scheduled_post_content_json(self, record_id: int, content_json: str):
    await self._conn.execute(
        "UPDATE scheduled_channel_posts SET content_json = ? WHERE id = ?",
        (content_json, record_id),
    )
    await self._conn.commit()
```

- [ ] **Step 2: Add `get_community_by_topic_id`**

```python
async def get_community_by_topic_id(self, topic_id: int) -> Optional[dict]:
    """Find community by either published_topic_id or suggested_topic_id."""
    async with self._conn.execute(
        "SELECT * FROM communities WHERE published_topic_id = ? OR suggested_topic_id = ?",
        (topic_id, topic_id),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None
```

- [ ] **Step 3: Add `get_unscheduled_posts_for_topic`**

```python
async def get_unscheduled_posts_for_topic(self, topic_id: int) -> List[dict]:
    """Posts in a topic that have no pending scheduled channel publication."""
    async with self._conn.execute(
        """
        SELECT p.*
        FROM posts p
        WHERE p.tg_topic_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_channel_posts scp
              WHERE scp.post_id = p.id AND scp.status = 'pending'
          )
        ORDER BY p.vk_post_id ASC
        """,
        (topic_id,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Add analytics methods for `/status`**

```python
async def get_pending_suggested_count(self, community_id: int) -> int:
    async with self._conn.execute(
        "SELECT COUNT(*) FROM posts WHERE community_id = ? AND post_type = 'suggested'",
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
```

- [ ] **Step 5: Add `set_notifications_muted`**

```python
async def set_notifications_muted(self, vk_id: int, muted: bool):
    await self._conn.execute(
        "UPDATE communities SET notifications_muted = ? WHERE vk_id = ?",
        (int(muted), vk_id),
    )
    await self._conn.commit()
```

- [ ] **Step 6: Verify methods exist and are callable**

Run `python -c "import sys; sys.path.insert(0, 'src'); import database; print('OK')"` from project root — should print `OK` with no import errors.

- [ ] **Step 7: Commit**

```bash
git add src/database.py
git commit -m "feat: add DB methods for content refresh, autoqueue, status analytics, mute"
```

---

## Task 4: VK — `fetch_post_data` Method

**Files:**
- Modify: `src/vk_client.py`

**Interfaces:**
- Produces: `async fetch_post_data(community_id: int, post_id: int) -> Optional[dict]`
  - Returns `dict` (post data) → post exists
  - Returns `{}` (empty dict) → post was deleted from VK
  - Returns `None` → VK API error (unknown state)

- [ ] **Step 1: Add `fetch_post_data` to `VKClient` in `src/vk_client.py`**

Add after `post_exists()`:

```python
async def fetch_post_data(self, community_id: int, post_id: int) -> Optional[dict]:
    """
    Fetch full post data for content comparison.
    Returns: dict with post data, {} if deleted, None if API error.
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
```

- [ ] **Step 2: Verify import path**

`scheduler.py` will do `from vk_client import VKClient`. Confirm `vk_client.py` is importable from `src/`:

```bash
cd "/Users/andrewsaltanov/Desktop/Learning/VK post /src" && python -c "from vk_client import VKClient; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/vk_client.py
git commit -m "feat: add VKClient.fetch_post_data for content change detection"
```

---

## Task 5: Feature 2 — Auto VK Content Check at Publish Time

**Files:**
- Modify: `src/scheduler.py`

**Interfaces:**
- Consumes:
  - `VKClient.fetch_post_data(community_id, post_id) -> Optional[dict]` (Task 4)
  - `VKClient.extract_post_content(post) -> dict` (existing)
  - `db.update_post_content_json(post_id, content_json)` (Task 3)
  - `db.update_scheduled_post_content_json(record_id, content_json)` (Task 3)
  - `_config.get_community_config(community_id) -> CommunityConfig | None` (existing)

- [ ] **Step 1: Add `VKClient` import to `src/scheduler.py`**

```python
from vk_client import VKClient
```

- [ ] **Step 2: Add `_check_vk_content()` helper function in `scheduler.py`**

Place before `execute_scheduled_post`:

```python
async def _check_vk_content(
    record_id: int,
    post_id: int,
    community_id: int,
    vk_post_id: int,
    saved_content: dict,
) -> tuple[dict, bool, bool]:
    """
    Fetches current VK content and compares with saved.
    Returns: (content_to_use, content_was_updated, post_was_deleted)
    """
    if _config is None:
        return saved_content, False, False

    comm_cfg = _config.get_community_config(community_id)
    if not comm_cfg:
        return saved_content, False, False

    try:
        async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk:
            post_data = await vk.fetch_post_data(community_id, vk_post_id)

        if post_data is None:
            # API error — fail-open, use saved content
            logger.warning(
                f"VK API error fetching post {vk_post_id} for record {record_id} — using saved content"
            )
            return saved_content, False, False

        if post_data == {}:
            # Post was deleted from VK
            return saved_content, False, True

        # Post exists — check if text changed
        import json as _json
        from vk_client import VKClient as _VKC  # already imported
        fresh_content = VKClient(comm_cfg.token).extract_post_content(post_data)
        # Preserve metadata fields added at send time
        for key in ("post_link", "author_link", "community_name"):
            fresh_content[key] = saved_content.get(key, "")

        if fresh_content.get("text", "") != saved_content.get("text", ""):
            fresh_json = _json.dumps(fresh_content, ensure_ascii=False)
            await _db.update_post_content_json(post_id, fresh_json)
            await _db.update_scheduled_post_content_json(record_id, fresh_json)
            logger.info(f"VK post {vk_post_id} text changed — updated record {record_id}")
            return fresh_content, True, False

        return saved_content, False, False

    except Exception as e:
        logger.warning(f"Error checking VK content for record {record_id}: {e}")
        return saved_content, False, False
```

- [ ] **Step 3: Update `execute_scheduled_post()` to call `_check_vk_content()` and handle results**

Replace the current try-block that starts with `msg_ids = await send_post_to_channel(...)`. The full updated body of `execute_scheduled_post` after the initial guard:

```python
async def execute_scheduled_post(record_id: int):
    """Called by APScheduler at the scheduled time."""
    if _bot is None or _db is None:
        logger.error("Scheduler not initialised — bot/db missing")
        return

    record = await _db.get_scheduled_post_record(record_id)
    if not record or record["status"] != "pending":
        return

    try:
        content = json.loads(record["content_json"])
    except (json.JSONDecodeError, TypeError) as parse_err:
        logger.error(f"Corrupt content_json for record {record_id}: {parse_err}")
        await _db.mark_scheduled_post_sent(record_id)
        return

    # Fetch original post to get vk_post_id and community_id
    orig_post = await _db.get_post_by_id(record["post_id"])

    # Check VK for content changes before publishing
    content_was_updated = False
    if orig_post:
        content, content_was_updated, post_deleted = await _check_vk_content(
            record_id=record_id,
            post_id=record["post_id"],
            community_id=orig_post["community_id"],
            vk_post_id=orig_post["vk_post_id"],
            saved_content=content,
        )
        if post_deleted:
            await _db.mark_scheduled_post_cancelled(record_id)
            logger.info(f"Record {record_id}: VK post deleted — cancelling publication")
            if orig_post and _config:
                try:
                    await _bot.send_message(
                        chat_id=_config.GROUP_ID,
                        text="🚫 Публикация отменена — пост удалён на VK",
                        message_thread_id=orig_post.get("tg_topic_id"),
                        reply_to_message_id=orig_post.get("tg_message_id"),
                    )
                except Exception as e:
                    logger.warning(f"Could not send deletion notice: {e}")
            if orig_post and _refresh_board_fn:
                try:
                    await _refresh_board_fn(orig_post["community_id"])
                except Exception:
                    pass
            return

    try:
        msg_ids = await send_post_to_channel(
            bot=_bot,
            channel_id=record["channel_id"],
            content=content,
            is_suggested=bool(record["is_suggested"]),
        )
        channel_msg_id = msg_ids[0] if msg_ids else None

        if channel_msg_id:
            await _db.update_scheduled_post_channel_msg_id(record_id, channel_msg_id)

        await _db.mark_scheduled_post_sent(record_id)
        logger.info(
            f"Scheduled post record_id={record_id} sent to channel {record['channel_id']}"
        )

        # Refresh schedule board
        if orig_post and _refresh_board_fn:
            try:
                await _refresh_board_fn(orig_post["community_id"])
            except Exception as board_err:
                logger.warning(f"Could not refresh schedule board: {board_err}")

        # Send notification to GROUP topic
        community = await _db.get_community(orig_post["community_id"]) if orig_post else None
        muted = community.get("notifications_muted", 0) if community else 0

        if channel_msg_id and _config and orig_post and not muted:
            channel_id = record["channel_id"]
            ch_id_str = str(abs(channel_id))[3:] if channel_id < 0 else str(channel_id)
            link = f"https://t.me/c/{ch_id_str}/{channel_msg_id}"
            tz = ZoneInfo(_config.TIMEZONE)
            time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M")
            update_line = "\n📝 Текст обновился на VK — опубликована актуальная версия" if content_was_updated else ""
            try:
                await _bot.send_message(
                    chat_id=_config.GROUP_ID,
                    text=f'📢 Опубликовано в канале {time_str}{update_line}\n<a href="{link}">Ссылка на пост</a>',
                    parse_mode="HTML",
                    message_thread_id=orig_post.get("tg_topic_id"),
                    reply_to_message_id=orig_post.get("tg_message_id"),
                    reply_markup=get_published_notification_keyboard(record_id),
                    disable_web_page_preview=True,
                )
            except Exception as notify_err:
                logger.warning(f"Could not send publish notification: {notify_err}")

    except Exception as e:
        logger.error(f"Failed to send scheduled post {record_id}: {e}")
```

Note: The `VKClient` used in `_check_vk_content` for `extract_post_content` is a sync method — instantiate without context manager just for extraction: `VKClient("").extract_post_content(post_data)` since `extract_post_content` doesn't use `_session`.

- [ ] **Step 4: Fix `_check_vk_content` — use `extract_post_content` correctly**

`extract_post_content` is a regular (sync) method. Replace the VKClient instantiation for extraction:

```python
# Inside _check_vk_content, replace the extraction line:
fresh_content = VKClient("").extract_post_content(post_data)
```

No network call needed here — just parsing.

- [ ] **Step 5: Manual verification**

Start the bot. Schedule a post for 2 minutes from now. In SQLite:
```sql
UPDATE scheduled_channel_posts SET content_json = replace(content_json, '"text": "', '"text": "СТАРЫЙ ТЕКСТ — ')
WHERE id = <record_id>;
UPDATE posts SET content_json = replace(content_json, '"text": "', '"text": "СТАРЫЙ ТЕКСТ — ')
WHERE id = <post_id>;
```
Wait for the scheduled time → verify the channel post has the ORIGINAL VK text (fresh), and the notification in the topic says «📝 Текст обновился на VK».

- [ ] **Step 6: Commit**

```bash
git add src/scheduler.py src/vk_client.py
git commit -m "feat: auto-refresh VK content at publish time, handle deleted posts"
```

---

## Task 6: Feature 3 — `/autoqueue` Bulk Scheduling

**Files:**
- Modify: `src/handlers.py`

**Interfaces:**
- Consumes:
  - `db.get_community_by_topic_id(topic_id)` (Task 3)
  - `db.get_unscheduled_posts_for_topic(topic_id)` (Task 3)
  - `db.get_pending_for_channel(channel_id)` (existing — added in previous session)
  - `db.save_scheduled_post_record(...)` (existing)
  - `db.update_scheduled_post_job_id(...)` (existing)
  - `db.mark_scheduled_post_cancelled(...)` (existing)
  - `db.get_pending_scheduled_for_post(post_db_id)` (existing)
  - `execute_scheduled_post` (existing import from `scheduler`)
  - `get_scheduled_badge(post_db_id, unix_ts, timezone, record_id)` (existing)
  - `refresh_schedule_board(community_vk_id)` (existing import)

- [ ] **Step 1: Extract `_schedule_post_core()` helper from `_do_schedule()` in `handlers.py`**

This extracts the DB+APScheduler part so `/autoqueue` can reuse it without individual Telegram notifications:

```python
async def _schedule_post_core(
    db: Database,
    scheduler: AsyncIOScheduler,
    post_db_id: int,
    channel_id: int,
    content_json: str,
    is_suggested: bool,
    schedule_time: datetime,
) -> tuple[int, int]:
    """
    Persist a scheduled post record and add APScheduler job.
    Cancels any existing pending records for this post first.
    Returns (record_id, unix_ts).
    """
    existing_pending = await db.get_pending_scheduled_for_post(post_db_id)
    for ep in existing_pending:
        job_id = ep.get("job_id")
        if job_id:
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        await db.mark_scheduled_post_cancelled(ep["id"])

    record_id = await db.save_scheduled_post_record(
        post_id=post_db_id,
        channel_id=channel_id,
        content_json=content_json,
        is_suggested=is_suggested,
        schedule_time=int(schedule_time.timestamp()),
    )
    job_id = f"sched_{record_id}"
    scheduler.add_job(
        execute_scheduled_post,
        trigger="date",
        run_date=schedule_time,
        args=[record_id],
        id=job_id,
        replace_existing=True,
    )
    await db.update_scheduled_post_job_id(record_id, job_id)
    return record_id, int(schedule_time.timestamp())
```

- [ ] **Step 2: Refactor `_do_schedule()` to use `_schedule_post_core()`**

Replace the block in `_do_schedule()` that starts with `# 1. Persist to DB...` through `# 3. Save job_id back to DB`:

```python
    try:
        record_id, unix_ts = await _schedule_post_core(
            db=db,
            scheduler=scheduler,
            post_db_id=post_db_id,
            channel_id=channel_id,
            content_json=post["content_json"],
            is_suggested=is_suggested,
            schedule_time=schedule_time,
        )

        time_str = schedule_time.strftime("%d.%m.%Y %H:%M")
        await _notify(event, f"✅ Пост запланирован на {time_str}", alert=True)
        logger.info(f"Scheduled post {post_db_id} → channel {channel_id} at {time_str}")

        # 4. Replace schedule keyboard with a "scheduled" badge
        tg_msg_id = post.get("tg_message_id")
        topic_id = post.get("tg_topic_id")
        if tg_msg_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=config.GROUP_ID,
                    message_id=tg_msg_id,
                    reply_markup=get_scheduled_badge(
                        post_db_id, unix_ts, config.TIMEZONE, record_id=record_id
                    ),
                )
            except Exception as edit_err:
                logger.warning(f"Could not update keyboard for post {post_db_id}: {edit_err}")

        # 5. Send reply marker in the topic
        try:
            await bot.send_message(
                chat_id=config.GROUP_ID,
                text=f"✅ Запланировано на {time_str}",
                message_thread_id=topic_id,
                reply_to_message_id=tg_msg_id,
            )
        except Exception as reply_err:
            logger.warning(f"Could not send schedule marker for post {post_db_id}: {reply_err}")

        # 6. Update the schedule board in the topic
        if community:
            try:
                await refresh_schedule_board(community["vk_id"])
            except Exception as board_err:
                logger.warning(f"Could not refresh schedule board: {board_err}")

    except Exception as e:
        logger.error(f"Schedule error: {e}")
        await _notify(event, f"❌ Ошибка планирования: {e}", alert=True)
```

- [ ] **Step 3: Add `cmd_autoqueue()` handler in `handlers.py`**

```python
@router.message(Command("autoqueue"))
async def cmd_autoqueue(
    message: Message,
    db: Database,
    config: Config,
    scheduler: AsyncIOScheduler,
    bot: Bot,
):
    if not _is_admin(message.from_user.id, config):
        return

    topic_id = message.message_thread_id
    if not topic_id:
        await message.reply("❌ Команда должна использоваться внутри топика сообщества.")
        return

    # Parse args: /autoqueue HH:MM interval_minutes
    args = (message.text or "").split()[1:]
    if len(args) < 2:
        await message.reply(
            "❌ Использование: <code>/autoqueue ЧЧ:ММ интервал_мин</code>\n"
            "Пример: <code>/autoqueue 09:00 60</code>",
            parse_mode="HTML",
        )
        return

    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    try:
        t = datetime.strptime(args[0], "%H:%M")
        start_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if start_time <= now:
            start_time += timedelta(days=1)
        interval_min = int(args[1])
        if interval_min <= 0:
            raise ValueError("interval must be positive")
    except (ValueError, IndexError):
        await message.reply(
            "❌ Неверный формат. Используйте: <code>/autoqueue ЧЧ:ММ интервал_мин</code>",
            parse_mode="HTML",
        )
        return

    community = await db.get_community_by_topic_id(topic_id)
    if not community:
        await message.reply("❌ Этот топик не привязан ни к одному сообществу.")
        return

    channel_id = community.get("channel_id", 0)
    if not channel_id:
        await message.reply("❌ Канал для этого сообщества не настроен.")
        return

    posts = await db.get_unscheduled_posts_for_topic(topic_id)
    if not posts:
        await message.reply("ℹ️ Нет постов без расписания в этом топике.")
        return

    # If there are already pending posts for this channel, start after the last one
    existing_pending = await db.get_pending_for_channel(channel_id)
    if existing_pending:
        last_ts = max(r["schedule_time"] for r in existing_pending)
        last_dt = datetime.fromtimestamp(last_ts, tz)
        candidate = last_dt + timedelta(minutes=interval_min)
        if candidate > start_time:
            start_time = candidate

    scheduled_lines = []
    current_time = start_time

    for post in posts:
        is_suggested = post["post_type"] == "suggested"
        try:
            record_id, unix_ts = await _schedule_post_core(
                db=db,
                scheduler=scheduler,
                post_db_id=post["id"],
                channel_id=channel_id,
                content_json=post["content_json"],
                is_suggested=is_suggested,
                schedule_time=current_time,
            )
            # Update the post's keyboard to show scheduled badge
            if post.get("tg_message_id"):
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=config.GROUP_ID,
                        message_id=post["tg_message_id"],
                        reply_markup=get_scheduled_badge(
                            post["id"], unix_ts, config.TIMEZONE, record_id=record_id
                        ),
                    )
                except Exception:
                    pass

            import json as _json
            text_preview = _json.loads(post["content_json"]).get("text", "")[:40]
            time_str = current_time.strftime("%H:%M")
            scheduled_lines.append(f"• {time_str} — {text_preview}…" if text_preview else f"• {time_str}")
            current_time += timedelta(minutes=interval_min)

        except Exception as e:
            logger.error(f"autoqueue: failed to schedule post {post['id']}: {e}")
            scheduled_lines.append(f"• ❌ Ошибка: {post['id']}")

    # Refresh board once after bulk scheduling
    try:
        await refresh_schedule_board(community["vk_id"])
    except Exception:
        pass

    count = len(posts)
    summary = f"✅ Запланировано {count} постов:\n" + "\n".join(scheduled_lines)
    await message.reply(summary)
    logger.info(f"autoqueue: scheduled {count} posts for community {community['vk_id']}")
```

Note: add `import json as _json` at the top of the function or use the existing `json` import.

- [ ] **Step 4: Add `/autoqueue` to `/help` text**

```python
"/autoqueue ЧЧ:ММ мин — массовое планирование постов из топика\n"
```

- [ ] **Step 5: Verify manually**

In a topic with ≥3 unscheduled posts, type `/autoqueue 23:59 30`. Verify:
- Bot replies with summary listing N posts at 23:59, 00:29, 00:59 (next day)
- Each post's keyboard is updated to the scheduled badge
- Schedule board in topic shows all queued posts
- SQLite: `SELECT id, schedule_time, status FROM scheduled_channel_posts ORDER BY schedule_time;` — shows correct timestamps

- [ ] **Step 6: Commit**

```bash
git add src/handlers.py
git commit -m "feat: add /autoqueue bulk scheduling and extract _schedule_post_core helper"
```

---

## Task 7: Feature 4 — Enhanced `/status`

**Files:**
- Modify: `src/handlers.py`

**Interfaces:**
- Consumes:
  - `db.get_pending_suggested_count(community_id)` (Task 3)
  - `db.get_published_count_since(community_id, since_ts)` (Task 3)
  - `db.get_all_pending_with_community()` (existing — from previous session)

- [ ] **Step 1: Update `cmd_status()` in `handlers.py`**

Replace the existing function body:

```python
@router.message(Command("status"))
async def cmd_status(message: Message, db: Database, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    communities = await db.get_communities()
    if not communities:
        await message.reply("Сообщества не настроены.")
        return

    tz = ZoneInfo(config.TIMEZONE)
    week_ago = int((datetime.now(tz) - timedelta(days=7)).timestamp())

    # Build a map of community_id → next pending publication
    all_pending = await db.get_all_pending_with_community()
    next_pub: dict[int, str] = {}
    for r in all_pending:
        # all_pending is sorted by schedule_time ASC — first hit per community is earliest
        # We need community_id, which isn't directly in all_pending; match by name is fragile
        # Instead query per community below
        pass

    lines = ["<b>Мониторинг сообществ:</b>"]
    for c in communities:
        community_id = c["vk_id"]
        suggested_count = await db.get_pending_suggested_count(community_id)
        published_week = await db.get_published_count_since(community_id, week_ago)

        # Find next pending pub for this community from all_pending
        # all_pending rows have community_name but not community_id — use channel_id match
        community_pending = [
            r for r in all_pending
            if r["community_name"] == c["name"]
        ]
        if community_pending:
            next_ts = community_pending[0]["schedule_time"]
            next_dt = datetime.fromtimestamp(next_ts, tz).strftime("%d.%m %H:%M")
            sched_str = f"{len(community_pending)} пост(ов), ближайший: {next_dt}"
        else:
            sched_str = "нет"

        lines.append(
            f"\n🔹 <b>{c['name']}</b>\n"
            f"   Публикации: топик #{c['published_topic_id']} | последний пост {c['last_post_id']}\n"
            f"   Предложки: топик #{c['suggested_topic_id']} | ожидает: {suggested_count}\n"
            f"   Запланировано: {sched_str}\n"
            f"   Опубликовано за 7 дней: {published_week}"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")
```

- [ ] **Step 2: Verify output**

Type `/status` in the supergroup — verify each community block shows all 4 data points. Check that suggestion count and 7-day count match what's in the DB:

```sql
SELECT COUNT(*) FROM posts WHERE community_id = <id> AND post_type = 'suggested';
SELECT COUNT(*) FROM scheduled_channel_posts scp
  JOIN posts p ON scp.post_id = p.id
  WHERE p.community_id = <id> AND scp.status = 'sent'
    AND scp.schedule_time >= strftime('%s','now','-7 days');
```

- [ ] **Step 3: Commit**

```bash
git add src/handlers.py
git commit -m "feat: enhance /status with suggestion count, weekly stats, next scheduled"
```

---

## Task 8: Feature 5 — `/mute` and `/unmute` Notification Commands

**Files:**
- Modify: `src/database.py` (migration v3)
- Modify: `src/handlers.py`
- Modify: `src/scheduler.py` (mute check already added in Task 5 — verify it reads the flag)

**Interfaces:**
- Consumes:
  - `db.get_community_by_topic_id(topic_id)` (Task 3)
  - `db.set_notifications_muted(vk_id, muted)` (Task 3)
  - `community["notifications_muted"]` read in `execute_scheduled_post()` (Task 5)

- [ ] **Step 1: Add migration v3 to `_MIGRATIONS` in `database.py`**

```python
_MIGRATIONS: list = [
    # v1 — add channel_message_id to scheduled_channel_posts
    "ALTER TABLE scheduled_channel_posts ADD COLUMN channel_message_id INTEGER",
    # v2 — add schedule_board_msg_id to communities
    "ALTER TABLE communities ADD COLUMN schedule_board_msg_id INTEGER",
    # v3 — add notifications_muted flag to communities
    "ALTER TABLE communities ADD COLUMN notifications_muted INTEGER DEFAULT 0",
]
```

- [ ] **Step 2: Add `/mute` and `/unmute` handlers in `handlers.py`**

```python
@router.message(Command("mute"))
async def cmd_mute(message: Message, db: Database, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    await _set_mute(message, db, config, muted=True)


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, db: Database, config: Config):
    if not _is_admin(message.from_user.id, config):
        return
    await _set_mute(message, db, config, muted=False)


async def _set_mute(message: Message, db: Database, config: Config, muted: bool):
    topic_id = message.message_thread_id
    action = "отключены" if muted else "включены"

    if topic_id:
        community = await db.get_community_by_topic_id(topic_id)
        if community:
            await db.set_notifications_muted(community["vk_id"], muted)
            await message.reply(
                f"{'🔕' if muted else '🔔'} Уведомления о публикации "
                f"<b>{action}</b> для «{community['name']}».",
                parse_mode="HTML",
            )
            return
        await message.reply("❌ Этот топик не привязан к сообществу.")
        return

    # General chat — show current states
    communities = await db.get_communities()
    lines = [f"{'🔕' if muted else '🔔'} Не в топике сообщества. Текущие настройки:\n"]
    for c in communities:
        state = "🔕 выкл" if c.get("notifications_muted") else "🔔 вкл"
        lines.append(f"• {c['name']}: уведомления {state}")
    await message.reply("\n".join(lines))
```

- [ ] **Step 3: Update `/help` text**

Add:
```python
"/mute — отключить уведомления о публикации (в топике)\n"
"/unmute — включить уведомления о публикации (в топике)\n"
```

- [ ] **Step 4: Verify migration v3 runs on restart**

Stop and restart the bot. Check logs for `Applying DB migration 3/3` then `Migration 3 applied.`

Verify column exists:
```sql
PRAGMA table_info(communities);
-- should show notifications_muted column
```

- [ ] **Step 5: Verify mute behavior**

Type `/mute` inside a community's published topic. Schedule a post. Wait for it to fire. Verify no «📢 Опубликовано» message appears in the topic. Type `/unmute`. Schedule another post. Verify notification returns.

- [ ] **Step 6: Commit**

```bash
git add src/database.py src/handlers.py
git commit -m "feat: add /mute /unmute per-community notification controls"
```

---

## Self-Review

**Spec coverage check:**
- F1 Reschedule ✅ Task 2
- F2 Auto VK content check ✅ Task 5
- F3 /autoqueue ✅ Task 6
- F4 Enhanced /status ✅ Task 7
- F5 /mute /unmute ✅ Task 8
- F6 Quick-publish button ✅ Task 1
- DB migration v3 ✅ Task 8 Step 1
- VK deleted post handling ✅ Task 5 Step 3
- Mute check in scheduler ✅ Task 5 Step 3 (included in `execute_scheduled_post` rewrite)

**Type consistency:**
- `_schedule_post_core` returns `tuple[int, int]` (record_id, unix_ts) — used identically in `_do_schedule` (Task 6 Step 2) and `cmd_autoqueue` (Task 6 Step 3) ✅
- `fetch_post_data` returns `Optional[dict]`, `{}` for deleted, `None` for error — consumed correctly in `_check_vk_content` ✅
- `get_community_by_topic_id` returns `Optional[dict]` — null-checked at every call site ✅
- `RescheduleCallback(post_db_id, record_id)` — produced in Task 2, consumed in Task 2's handler ✅
