# UX Improvements Design — VK-to-Telegram Bridge Bot

**Date:** 2026-08-26  
**Status:** Approved

## Context

The bot polls VK communities for wall posts, forwards them to Telegram supergroup forum topics, and lets admins schedule channel publications via inline keyboards. Six UX improvements were designed and approved to reduce friction in the editorial workflow.

---

## Feature 1: Reschedule Button («Перенести»)

### Problem
After scheduling a post, changing the time requires two steps: cancel → reschedule. This is unnecessary friction.

### Design
Add a `📅 Перенести` button to the scheduled badge alongside the existing cancel button.

**New badge layout:**
```
[✅ Запланировано на DD.MM HH:MM]
[❌ Отменить публикацию]
[📅 Перенести]
```

**Flow:** Pressing «Перенести» edits the message under the original post, replacing it with the full schedule time picker keyboard (same 11 options + custom time). When the admin picks a new time, `_do_schedule()` runs as normal — it already cancels existing pending records and creates a new one.

### Components
- **`callbacks.py`**: new `RescheduleCallback(post_db_id: int, record_id: int)`
- **`keyboards.py`**: update `get_scheduled_badge()` to include reschedule button (3rd row)
- **`handlers.py`**: new `handle_reschedule()` handler — finds the post's original keyboard message, edits it to show `get_schedule_keyboard(post_db_id)`, answers the callback
- No new DB methods needed

---

## Feature 2: Auto VK Content Check at Publish Time

### Problem
A VK post can be edited between scheduling and publication. The bot publishes the stale snapshot saved at scheduling time.

### Design
In `execute_scheduled_post()` (`scheduler.py`), **before** calling `send_post_to_channel()`:

1. Fetch current VK content via `wall.getById` using a `VKClient` instance created from `_config`
2. Extract content using the existing `vk_client.extract_post_content()`
3. Compare `text` field with the saved `content_json` (photo comparison skipped — URL stability not guaranteed by VK API)
4. **If changed:** update `content_json` in both `posts` and `scheduled_channel_posts` tables; use new content for publication; set a `content_updated` flag
5. **If VK post no longer exists:** cancel publication, send alert to group topic, return
6. **If VK API error:** log warning, proceed with saved content (fail-open)

**Notification augmentation:** The existing «📢 Опубликовано» message gains an extra line when content changed:
```
📢 Опубликовано в канале 14.08 15:00
📝 Текст обновился на VK — опубликована актуальная версия
[Ссылка на пост]  [🗑 Удалить из канала]
```

When post deleted on VK before firing:
```
🚫 Публикация отменена — пост удалён на VK
```

### Components
- **`scheduler.py`**: add VK content check block in `execute_scheduled_post()` before send; requires `_config` (already available); creates `VKClient` inline (context manager)
- **`database.py`**: new `update_post_content_json(post_id, content_json)` + `update_scheduled_post_content_json(record_id, content_json)` methods
- No new callbacks or keyboards

### Note on VK client instantiation
`scheduler.py` does not currently use VK. It gets `_config` from `init()` which includes `CommunityConfig` per community. Use `_config.get_community_config(community_id)` → create `async with VKClient(comm_cfg.token, user_token=comm_cfg.user_token) as vk`.

---

## Feature 3: Bulk Scheduling (`/autoqueue`)

### Problem
When many posts arrive overnight, scheduling each individually is tedious.

### Design
Command: `/autoqueue HH:MM interval_minutes` (e.g., `/autoqueue 09:00 60`)

**Behavior:**
1. Detect current topic from `message.message_thread_id`
2. Find the community mapped to this topic (published or suggested)
3. Query all posts in this topic **without** an active (`pending`) scheduled record
4. Sort by `vk_post_id` ascending (oldest first = earliest VK post first)
5. If any pending scheduled posts exist for this channel — start the queue after the last one
6. Assign times: `start_time`, `start_time + interval`, `start_time + 2×interval`, …
7. Call `_do_schedule()` logic for each (persist DB record + APScheduler job)
8. Reply with summary:
   ```
   ✅ Запланировано 7 постов:
   • 09:00 — «Квартира на Ленина…»
   • 10:00 — «Комната у метро…»
   …
   ```
9. If no unscheduled posts found: `ℹ️ Нет постов без расписания в этом топике.`
10. If topic not recognized: `❌ Команда должна использоваться внутри топика сообщества.`

**Time parsing:** accepts `HH:MM` (today or tomorrow if time is past) same logic as existing FSM.

### Components
- **`handlers.py`**: new `cmd_autoqueue()` with `@router.message(Command("autoqueue"))` + `_is_admin()` check
- **`database.py`**: new `get_unscheduled_posts_for_topic(topic_id)` — returns posts with no pending scheduled record
- **`keyboards.py`**: no changes (uses existing `get_scheduled_badge`)
- Update `/help` text

---

## Feature 4: Enhanced `/status`

### Design
Extend `cmd_status()` to include per-community analytics:

```
🔹 Сообщество А
   Топик публикаций: #123 | Последний пост: 456789
   Топик предложек: #124 | Ожидает: 3 предложки
   Запланировано: 2 поста (ближайший: 15.08 14:00)
   Опубликовано за 7 дней: 12 постов
```

### Components
- **`database.py`**: new `get_pending_suggested_count(community_id)` + `get_published_count_since(community_id, since_ts)` + reuse existing `get_all_pending_with_community()` for next scheduled
- **`handlers.py`**: update `cmd_status()` to call new methods and format extended output

---

## Feature 5: Mute/Unmute Publish Notifications (`/mute`, `/unmute`)

### Problem
«📢 Опубликовано в канале» notifications sent to the topic after every publication create noise when publication volume is high.

### Design
- Commands `/mute` and `/unmute` toggle a per-community mute flag
- When used **inside a community topic**: affects that community only; bot resolves community via `published_topic_id` or `suggested_topic_id` match
- When used **in general chat or unrecognised topic**: reply with list of current mute states per community

**Storage:** new `notifications_muted INTEGER DEFAULT 0` column in `communities` table → **migration v3**.

**Behavior in `execute_scheduled_post()`:** check `community["notifications_muted"]`; if true, skip the «📢 Опубликовано» message (still send the content-changed alert if relevant).

### Components
- **`database.py`**: migration v3 + `set_notifications_muted(vk_id, muted: bool)` + `get_community_by_topic_id(topic_id)` (to resolve community from thread context)
- **`handlers.py`**: new `cmd_mute()` + `cmd_unmute()` handlers
- **`scheduler.py`**: add mute check before sending notification

---

## Feature 6: Prominent Quick-Publish Button

### Problem
«⚡️ Сейчас» is buried among 11 schedule buttons. Urgent posts require scanning the keyboard.

### Design
Move «⚡️ Сейчас» to its own full-width first row, separated from the time-grid:

```
[        ⚡️ Опубликовать сейчас        ]
[ 30 мин ][ 1 ч ][ 1.5 ч ][ 2 ч ]
[ 2.5 ч  ][ 3 ч ][ 3.5 ч ][ 4 ч ]
[ 4.5 ч  ][ 5 ч ]
[      🕐 Своё время      ]
```

Label change: «⚡️ Опубликовать сейчас» (clearer intent). Callback unchanged (`option="0"`).

### Components
- **`keyboards.py`**: update `SCHEDULE_OPTIONS` (remove `0` entry) + `_schedule_rows()` (prepend the «Сейчас» button as its own row before the grid)

---

## Data Flow Summary

```
Admin presses button / types command
        │
        ├── Reschedule → edit message to show picker → _do_schedule()
        ├── /autoqueue → find unscheduled posts → _do_schedule() × N
        ├── /mute|unmute → toggle DB flag
        └── /status → aggregate DB queries → formatted reply

APScheduler fires execute_scheduled_post()
        │
        ├── [NEW] VK wall.getById → compare content
        │       ├── changed → update DB, set flag
        │       └── deleted → cancel, notify, return
        ├── send_post_to_channel()
        ├── refresh_schedule_board()
        └── [CONDITIONAL] notify group topic
                └── check notifications_muted flag
```

---

## DB Migrations

| Version | SQL |
|---|---|
| v3 | `ALTER TABLE communities ADD COLUMN notifications_muted INTEGER DEFAULT 0` |

(v1 and v2 already applied)

New methods summary:
- `update_post_content_json(post_id, json)`
- `update_scheduled_post_content_json(record_id, json)`
- `get_unscheduled_posts_for_topic(topic_id)`
- `get_pending_suggested_count(community_id)`
- `get_published_count_since(community_id, since_ts)`
- `get_community_by_topic_id(topic_id)`
- `set_notifications_muted(vk_id, muted)`

---

## Verification

1. **Reschedule:** Schedule a post → press «Перенести» → verify picker appears → pick new time → verify badge shows new time and board updates
2. **Auto VK check:** Schedule a post, manually update `content_json` in DB to old text, let it fire → verify published content is fresh and notification mentions update
3. **VK post deleted:** Schedule a post, delete it on VK, let scheduled time fire → verify cancellation notification in topic, no channel post
4. **`/autoqueue`:** In a topic with 5 unscheduled posts, type `/autoqueue 10:00 30` → verify 5 DB records at 10:00, 10:30, 11:00, 11:30, 12:00 and board shows them
5. **`/status`:** Verify suggestion count and 7-day publication count are accurate
6. **`/mute` + `/unmute`:** Schedule two posts in sequence; after `/mute` the «📢» notification should not appear; after `/unmute` it returns
7. **Quick publish:** Verify «⚡️» is the first full-width button and triggers ~30s publish
