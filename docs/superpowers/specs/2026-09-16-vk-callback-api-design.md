# VK Callback API for published-post detection

## Problem

VK's Sept 2026 platform policy caps unverified business profiles at 10,000 API
calls/month. Before this change, the poller burned ~52,000 calls/month just on
`wall.get` (2 calls × 3 communities × 288 cycles/day at the old 300s interval),
before counting deletion-check bursts. A first pass (commit `3b50d59`) raised
`POLL_INTERVAL` to 3600s and capped the deletion-check backlog, cutting steady
-state usage to ~4,300/month — safe, but it also slowed new-post detection to
up to an hour, and the "suggested" queue (which admins act on) shares that same
slow cadence.

VK's Callback API can push `wall_post_new` events to an HTTPS endpoint the
moment a post appears on a community's public wall, at effectively zero
API-quota cost. Moving published-post detection onto that push mechanism frees
up quota headroom to poll the "suggested" queue (predlozhka) — which has no
Callback event and must stay on `wall.get` — much more frequently than the
current 3600s.

## Goals

- Detect new **published** wall posts in near-real-time via VK Callback API,
  at near-zero API quota cost.
- Free up quota to poll **suggested** posts more frequently than today (target
  ~15-20 min instead of 3600s).
- Keep a low-frequency `wall.get` safety net for published posts, because (a)
  VK has no `wall_post_delete` Callback event, so deletion detection still
  needs periodic polling, and (b) it catches any webhook delivery VK failed to
  send (network blip, server restart during delivery).
- No double-sends when a post arrives via both the webhook and the safety-net
  poll.

## Non-goals

- No Callback-based solution for suggested posts — VK has no event for the
  moderation queue; `filter=suggests` remains poll-only.
- No change to how scheduled publication, deletion of *suggested* posts, or
  Telegram-side logic works.
- Not touching the unrelated `vk-to-telegram-bridge` container (VK Long Poll,
  DM forwarding) or the pre-existing Callback API servers/keys already
  configured for this community in VK's admin panel (per the "Ключи доступа"
  screen — 22 keys tied to other apps like "Callback API App", "Postmypost").
  We add a **new**, separate Callback server per community.

## Architecture

```
VK (per community)  --HTTPS POST-->  nginx (ugnestbot.aszhuloong.ru)
                                          --> 127.0.0.1:<PORT> (new)
                                          --> aiohttp app (callback_server.py)
                                                --> VKPoller.handle_new_published_post()
                                                       --> existing _send_post() pipeline
                                                       --> existing DB dedup (save_post_get_id)
```

The webhook server runs as a third concurrent asyncio task inside the
existing `vk_tg_bot` process, alongside aiogram's Telegram polling and the
existing `VKPoller.start()` loop — same pattern `main.py` already uses.

One callback URL is shared across all 3 communities (VK includes `group_id`
in every event body), so no per-community path or subdomain is needed —
dispatch happens inside the handler by looking up `group_id` in
`config.COMMUNITIES`.

## Components

### 1. `src/callback_server.py` (new)

An `aiohttp.web.Application` with two routes:

- `POST /vk-callback` — main handler.
- `GET /health` — trivial 200 OK, matching the pattern already used by the
  sibling `vk-to-telegram-bridge` container, useful for a Docker healthcheck
  later if wanted (not required now).

Handler logic for `POST /vk-callback`:

1. Parse JSON body. On parse failure, log and return `200 "ok"` (VK doesn't
   care about our internal errors and will retry on non-200/timeout — for a
   malformed body there's nothing useful a retry would fix, so ack it away).
2. Look up the community by `body["group_id"]` in `config.COMMUNITIES`
   (positive group_id — VK sends it unsigned in callback bodies). Unknown
   `group_id` → log a warning, return `"ok"`.
3. Verify `body.get("secret") == community.callback_secret`. Mismatch → log a
   warning (possible spoofing attempt) and return `"ok"` **without
   processing** — never trust an unverified payload enough to post it to
   Telegram.
4. `type == "confirmation"` → return the community's stored
   `callback_confirmation` string as plain text (this is VK's one-time
   ownership check when you click "Подтвердить" in the community's Callback
   API settings).
5. `type == "wall_post_new"` → extract `body["object"]` (the post, same shape
   as a `wall.get` item) and call `VKPoller.handle_new_published_post(community,
   post)` (new method, see below). Return `"ok"`.
6. Any other `type` (VK sends many event types once a server is confirmed,
   e.g. `group_join`) → ignore, return `"ok"`.

VK requires a response within a few seconds or it retries; step 6's "ignore
unknown types" keeps the handler fast and forward-compatible with event types
we never asked VK to send but may receive anyway (VK's dashboard has
per-server checkboxes, but stray deliveries during misconfiguration are cheap
to shrug off).

### 2. `poller.py` changes

- **Refactor `_send_post`**: drop the `vk: VKClient` parameter. Its only uses
  of `vk` are the static helpers `VKClient.extract_post_content(post)`,
  `VKClient.get_author_link(post, community_id)`, `VKClient.get_post_link(...)`
  — none need a live token or API call. Calling them as `VKClient.<method>`
  directly (they're already `@staticmethod`) means both the polling loop and
  the new webhook handler can call the same send pipeline without needing a
  `VKClient` instance in the webhook path. All existing call sites in
  `_poll_wall` drop the `vk` argument.
- **New method `handle_new_published_post(self, community: dict, post: dict)`**:
  thin wrapper around `_send_post(community, post, community["published_topic_id"],
  "published")`, plus bumping `last_post_id` in the DB if `post["id"]` is
  greater than the stored value (mirrors what `_poll_wall` does, so the
  safety-net poll's `id > last_known_id` diff doesn't re-send it).
- **Dual cadence**: `POLL_INTERVAL` (already lowered to 3600s) becomes the
  cadence for the **suggested**-queue check only, and gets lowered again
  (config default → 1200s, i.e. 20 min — see Quota budget below). A new env
  var `PUBLISHED_SAFETY_POLL_INTERVAL` (default 14400s, 4h) controls how often
  the **published**-wall safety-net poll (new-post catch-up + deletion check)
  runs. `VKPoller.start()`'s loop tick stays at `POLL_INTERVAL`; a tick
  counter (`self._ticks_since_published_check`, incremented each loop,
  compared against `PUBLISHED_SAFETY_POLL_INTERVAL // POLL_INTERVAL`) decides
  whether this tick also runs `_poll_wall(..., "published")`. `"suggested"`
  runs every tick as before.

### 3. `config.py` changes

`CommunityConfig` gains two fields, populated from `communities.json`:

```python
callback_secret: str = ""         # VK Callback API "Секретный ключ" for this community
callback_confirmation: str = ""   # VK's one-time confirmation string for this community
```

`Config` gains `PUBLISHED_SAFETY_POLL_INTERVAL: int = 14400` and
`CALLBACK_SERVER_PORT: int = 8090` (the sibling `vk-to-telegram-bridge`
container already uses `8080` internally / `18080` on the host — `8090`
avoids collision on both).

### 4. `main.py` wiring

Start the aiohttp runner as a third task, same lifecycle pattern as
`poller_task`:

```python
callback_runner = await start_callback_server(config, poller)  # binds 127.0.0.1:PORT
...
finally:
    await callback_runner.cleanup()
```

`start_callback_server` needs a reference to the already-constructed
`VKPoller` instance (to call `handle_new_published_post`), so `VKPoller`
construction moves slightly earlier in `main()` — before the `POLL_ENABLED`
branch — so both the poller loop (if enabled) and the callback server (always
started, independent of `VK_POLLING_ENABLED`) can use the same instance.

## Quota budget (approximate)

| Source | Interval | Calls/day | Calls/month |
|---|---|---|---|
| Suggested (`wall.get`) | 1200s (20 min) | 3 × 72 = 216 | ~6,480 |
| Published safety-net (`wall.get`) | 14400s (4h) | 3 × 6 = 18 | ~540 |
| Deletion checks (`wall.getById`, capped at 20/cycle) | piggybacks on safety-net poll | worst case 3×20×6=360 | ~10,800 (only if backlog is saturated *every* cycle, which self-drains) |
| **Steady-state total** | | | **~7,000-8,000** |

This leaves headroom under the 10,000/month cap for scheduled-post execution
refreshes and admin actions. If actual usage runs tight, the first lever to
pull is the suggested-poll interval (raise it), not re-adding published
polling.

## Deployment / infra steps (manual, outside this repo's code)

1. DNS: `ugnestbot.aszhuloong.ru` → `A` record → `144.31.102.35`.
2. `certbot --nginx -d ugnestbot.aszhuloong.ru` on the server (existing
   certbot install, separate cert from the VPN project's domains).
3. New nginx server block proxying `https://ugnestbot.aszhuloong.ru/` →
   `127.0.0.1:<CALLBACK_SERVER_PORT>`.
4. `docker-compose.yml`: no public port mapping needed (nginx and the bot
   container must share a network path — either both on the host network via
   `127.0.0.1:<port>:<port>` like the sibling bridge does, or an internal
   Docker network nginx can reach; match whatever the sibling project already
   does since nginx already reaches its `18080`).
5. In VK's community admin panel (**for each of the 3 communities**):
   "Работа с API" → "Callback API" → add a **new** server (do not touch
   "Сервер 3" or whatever else already exists) → paste the callback URL → VK
   shows a confirmation string → copy it into that community's
   `callback_confirmation` in `communities.json` → deploy → click
   "Подтвердить" in VK's panel → set a "Секретный ключ" → copy it into
   `callback_secret` → deploy again → in "Типы событий" enable only
   `wall_post_new` (leave everything else unchecked) → save.

Step 5 has a chicken-and-egg shape (VK needs the confirmation string to exist
in our config *before* we can click Confirm, but VK only shows the string
after we save the URL): deploy code with placeholder/empty
`callback_confirmation` first, add the URL in VK's panel, copy the string VK
shows back into `communities.json`, redeploy, then click Confirm.

## Error handling & security

- **Spoofed events**: rejected by secret mismatch (see step 3 above) —
  logged, not processed, still ack'd with `200 "ok"` so VK doesn't retry-storm
  us over an attacker's junk.
- **Duplicate delivery** (VK retries if it doesn't get `"ok"` fast enough, or
  the same post could arrive via both webhook and the safety-net poll):
  already handled for free by `save_post_get_id`'s existing "already exists →
  return None" dedup — no new dedup logic needed.
- **Unknown community / bad payload**: logged and ack'd, never raises into
  VK's retry path.
- **Callback server down** (deploy, restart, crash): VK gives up after a few
  retries; the safety-net poll (`PUBLISHED_SAFETY_POLL_INTERVAL`) catches
  anything missed within its window — worst case, a published post is up to 4
  hours late instead of instant, same as today's behavior before this change.

## Testing plan

This repo has no test suite or CI (per `CLAUDE.md`). Verification is manual:

1. Local: run the aiohttp app standalone, `curl -X POST localhost:<port>/vk-callback`
   with a synthetic `wall_post_new` JSON body (real shape, fake secret) → expect
   the request to be logged as rejected, not sent to Telegram.
2. Same, with the correct secret for a test community → expect a message to
   land in that community's Telegram topic.
3. Send the identical payload twice → expect only one Telegram message
   (dedup via `save_post_get_id`).
4. After deploying to the VPS and completing the VK panel setup for one
   community: publish a real test post on that community's wall → confirm it
   appears in the Telegram topic within a few seconds, not waiting for the
   next poll cycle.
5. Confirm the VK panel shows the new Callback server as "подтверждён"
   (green) after step 5's flow above.
6. Watch `docker logs vk_tg_bot` for a day to confirm no `Flood control` /
   quota errors reappear and the suggested queue still updates within its new
   ~20 min cadence.
