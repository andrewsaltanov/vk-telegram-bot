# VK Bots Long Poll for published-post detection

Supersedes `2026-09-16-vk-callback-api-design.md` (never implemented).

## Problem

VK's Sept 2026 platform policy caps unverified business profiles at 10,000
API calls/month. A first pass (commit `3b50d59`) raised the poller's
`POLL_INTERVAL` to 3600s and capped the deletion-check backlog, cutting
steady-state usage from ~52,000/month to ~4,300/month — safe, but it also
slowed new-post detection to up to an hour, and the "suggested" queue (which
admins act on) shares that same slow cadence.

The previous design (`2026-09-16-vk-callback-api-design.md`) proposed VK's
Callback API — a webhook VK pushes `wall_post_new` events to — to solve
this. It required a public HTTPS endpoint: a new subdomain, a Let's Encrypt
certificate, an nginx server block, and a confirmation/secret exchange
repeated in each of the 3 communities' VK admin panels. That work was never
started (the implementation attempt was aborted before Task 1) once a
simpler option surfaced.

**VK's Bots Long Poll API delivers the identical event catalog** (VK's own
docs: "event structures in Bots Long Poll are identical to Callback API")
over a connection *we* open outward to VK, using the *community* access
token already sitting in `communities.json` — no public endpoint, no domain,
no certificate, no nginx, no per-community secret. Verified live during
design (2026-09-17): `groups.getLongPollServer` succeeded immediately with
the existing community token from an arbitrary IP (unlike the personal user
token, community tokens are not IP-pinned) and was not subject to the
monthly-quota block currently affecting the personal user token used for
`wall.get`.

## Goals

- Detect new **published** wall posts in near-real-time via VK Bots Long
  Poll, at effectively zero API-quota cost against the constrained monthly
  cap (Long Poll's session calls are a different, apparently unaffected,
  token/quota pool from the exhausted personal user token).
- Free up the personal-token quota to poll the **suggested** queue
  (predlozhka) — which has no VK event under either transport — more
  frequently than today (~15-20 min instead of 3600s).
- Keep a low-frequency `wall.get` safety net for published posts: VK has no
  `wall_post_delete` event under any transport, so deletion detection still
  needs periodic polling; the same safety net also catches anything a Long
  Poll session missed during a reconnect gap.
- No double-sends when a post arrives via both Long Poll and the safety-net
  poll.

## Non-goals

- No Callback-API webhook, no domain, no certificate, no nginx change — this
  design needs none of them.
- No Long-Poll-based solution for suggested posts — VK has no event for the
  moderation queue under any transport; `filter=suggests` remains poll-only.
- No change to how scheduled publication, deletion of *suggested* posts, or
  Telegram-side logic works.
- **Not touching the unrelated `vk-to-telegram-bridge` container** (a
  separate repo/deployment on the same VPS that forwards VK community DMs to
  a different Telegram bot). It already holds its own Bots Long Poll session
  against the *same* community tokens for `message_new` events. Verified
  live (2026-09-17): opening a second, independent Long Poll session against
  the same group_id/token while that session was active caused no
  disruption — it kept forwarding messages normally throughout. VK's Bots
  Long Poll is a broadcast: each `groups.getLongPollServer` call is an
  independent session with its own `key`/`ts`, not a single-consumer queue
  (unlike, e.g., Telegram's `getUpdates`). This project opens its own
  session; it does not touch the sibling project's code, connection, or
  container.

## Architecture

```
groups.getLongPollServer(community_token, group_id)  →  {key, server, ts}
                                                              │
                    (one independent long-poll loop per community, 3 total)
                                                              │
        GET {server}?act=a_check&key=&ts=&wait=25  ──(VK, up to 25s)──▶
                                                              │
                              {ts, updates: [{type, object, group_id}, …]}
                                                              │
                       type == "wall_post_new"
                                                              │
                         VKPoller.handle_new_published_post(group_id, object)
                                                              │
                              existing _send_post() pipeline + DB dedup
```

Three independent asyncio tasks — one per community in
`config.COMMUNITIES` — run alongside aiogram's Telegram polling and the
existing `VKPoller.start()` loop, the same concurrent-task pattern
`main.py` already uses. Each task owns its own `key`/`ts` session state and
reconnects independently; one community's connection trouble never affects
another's.

## Components

### 1. `src/long_poll.py` (new)

One function, `run_long_poll(comm_cfg: CommunityConfig, poller: VKPoller) ->
None`, run as its own `asyncio.create_task(...)` per community. Loop body:

1. If no active session (`key`/`server`/`ts` unset, e.g. on first run or
   after an expiry below): call `groups.getLongPollServer` with
   `comm_cfg.token` (the *community* token — Long Poll needs `manage`
   rights, which this token already has, per the live test) and
   `comm_cfg.group_id`. On failure (network error, VK error), log, sleep 5s,
   retry.
2. `GET {server}?act=a_check&key={key}&ts={ts}&wait=25` with a client
   timeout a little longer than `wait` (VK recommends 25s; use 90s ceiling
   headroom in the HTTP timeout so a slow-but-alive long poll isn't killed
   early).
3. Response handling:
   - `failed` absent → normal response: update `ts` from the response, then
     for each item in `updates` where `type == "wall_post_new"`, call
     `poller.handle_new_published_post(group_id, item["object"])`. Ignore
     every other `type` (forward-compatible with event types VK may deliver
     that this project never asked to act on).
   - `failed == 1` → history gap; VK supplies a fresh `ts` in the same
     response — adopt it and continue, same session.
   - `failed == 2` or `failed == 3` → session invalid (key expired / data
     lost) — discard the session state and go back to step 1 for a fresh
     `groups.getLongPollServer` call.
   - Any other shape (network exception, malformed JSON, unexpected `failed`
     value) → log, discard session state, sleep 5s, go back to step 1.
4. Loop forever. Shutdown is cooperative cancellation: `main.py` cancels the
   task and awaits it catching `asyncio.CancelledError`, the same pattern
   already used for `poller_task` — no separate stop flag needed.

### 2. `poller.py` — unchanged from the Callback-API design

`VKPoller.handle_new_published_post(self, vk_id: int, post: dict) -> None`
is transport-agnostic: it already takes a bare `(group_id, post-dict)` pair
and doesn't care whether the caller is a webhook handler or a Long Poll
loop. The `_send_post` refactor (drop the unused `vk: VKClient` parameter,
call its static content-extraction helpers directly on the class) and the
dual-cadence safety-net poll (`PUBLISHED_SAFETY_POLL_INTERVAL`, ticked
against `POLL_INTERVAL`) both stand exactly as designed before — neither
depends on how `wall_post_new` events arrive.

### 3. `config.py` — smaller than the Callback-API design

Needed: `Config.PUBLISHED_SAFETY_POLL_INTERVAL: int = 14400` (unchanged from
before — still just a deletion-detection/catch-up cadence).

**Not needed** (removed from the Callback-API design along with the webhook
itself): `Config.CALLBACK_SERVER_PORT`, `CommunityConfig.callback_secret`,
`CommunityConfig.callback_confirmation`. Long Poll needs no new
per-community secret and no new config field beyond what already exists —
`CommunityConfig.token` (the community token) and `CommunityConfig.group_id`
are already there.

### 4. `main.py` wiring

Replace "start one callback server" with "start one Long Poll task per
community":

```python
long_poll_tasks = [
    asyncio.create_task(run_long_poll(comm_cfg, poller))
    for comm_cfg in config.COMMUNITIES
]
...
finally:
    for t in long_poll_tasks:
        t.cancel()
    for t in long_poll_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    if poller_task:
        ...
```

As in the Callback-API design, `VKPoller` is constructed unconditionally
(not only inside the `POLL_ENABLED` branch), since `handle_new_published_post`
must exist for the Long Poll tasks to call regardless of whether the
`wall.get`-based polling loop itself is enabled.

## Quota budget (approximate)

| Source | Interval | Calls/month (personal user-token quota) |
|---|---|---|
| Suggested (`wall.get`) | 1200s (20 min) | ~6,480 |
| Published safety-net (`wall.get`) | 14400s (4h) | ~540 |
| **Total against the 10,000/month cap** | | **~7,000** |
| Published new-post detection (Long Poll) | continuous | **0** against this cap — separate community-token quota pool, confirmed unaffected during design |

Same steady-state budget as the Callback-API design for the parts that still
use `wall.get`; the difference is that published-post detection no longer
needs to be squeezed into that budget at all.

## Deployment / infra steps (manual, outside this repo's code)

Dramatically smaller than the Callback-API design — no DNS, no certificate,
no nginx, no per-community secret exchange:

1. In VK's community admin panel (**for each of the 3 communities**):
   Управление → Дополнительно → Работа с API → **Long Poll API** tab →
   set to **Включено**.
2. In the same community's **Типы событий** tab, check **"Записи на
   стене"** (wall posts — delivers `wall_post_new`; leave every other event
   type unchecked, matching the "ask for exactly what we use" principle
   from the superseded design).
3. No code deploy is required for this step to take effect — the community
   token already has the necessary `manage` right (verified live), so once
   Long Poll is toggled on and the event type is checked, `run_long_poll`
   starts receiving events on its very next poll cycle.

## Error handling & security

- **No spoofing surface at all**: unlike a webhook, nothing external can
  reach this code uninvited — Long Poll is a connection *we* initiate
  outward, authenticated by possession of the community token. There is no
  secret-verification step to get right or get wrong.
- **Session expiry / data loss** (`failed` 2/3): handled by discarding
  session state and calling `groups.getLongPollServer` again — a normal,
  expected occurrence (VK docs note the key expires; this is not an error
  condition worth alerting on).
- **Duplicate delivery** (a reconnect could theoretically redeliver an event
  near the boundary, or the same post could arrive via both Long Poll and
  the safety-net poll): handled for free by the existing
  `save_post_get_id`'s "already exists → return None" dedup — no new dedup
  logic needed, exactly as in the superseded design.
- **Long Poll down for one community** (VK-side outage, network partition):
  that community's task retries with a 5s backoff independently; the other
  two communities' tasks are unaffected (separate tasks, separate sessions).
  The safety-net poll (`PUBLISHED_SAFETY_POLL_INTERVAL`) catches anything
  missed within its window — worst case, a published post is up to 4 hours
  late instead of instant, same fallback behavior as the superseded design.
- **Sibling project's Long Poll session**: confirmed live (see Non-goals)
  that a second independent session does not disrupt it. If this project's
  Long Poll task and the sibling's ever showed interference in production
  despite the test, the fix is on this project's side (back off, add
  jitter) — the sibling project is not touched either way.

## Testing plan

This repo has no test suite or CI (per `CLAUDE.md`). Verification is manual,
without needing the exhausted personal user-token quota at all (Long Poll
and its safety-net interactions can be verified against synthetic data, and
the community-token calls have already been shown live not to be
quota-blocked):

1. Local: with a synthetic session dict (`{"key": "fake", "server":
   "http://127.0.0.1:<mock-port>", "ts": "1"}`) and a tiny local HTTP stub
   standing in for `{server}`, verify `run_long_poll`'s response handling
   for each case: a normal `updates` array with a `wall_post_new` item
   (dispatches to a mocked `handle_new_published_post`), an item of an
   unrelated `type` (ignored), `failed: 1` (adopts the new `ts`, loops),
   `failed: 2` / `failed: 3` (drops session state, calls
   `groups.getLongPollServer` again — verify via a mock).
2. Local: verify `handle_new_published_post` dedup exactly as in the
   superseded design's Task 3 (same method, unchanged).
3. After toggling Long Poll on for one community in VK's panel (the only
   manual infra step): publish a real test post on that community's wall →
   confirm it appears in the Telegram topic within a few seconds.
4. Watch `docker logs vk_tg_bot` for a day to confirm: no unexpected Long
   Poll reconnect storms, the suggested queue still updates within its
   ~20 min cadence, and — separately — `docker logs vk-to-telegram-bridge`
   keeps forwarding messages with no new errors (confirming the sibling
   project stays unaffected under sustained real traffic, not just the
   few-minute live test done during design).
