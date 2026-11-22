# Architecture — Telegram Group Bridge ("Lulu")

A bridge that lets an LLM agent (a separate chat session, "Lulu") live inside
several Telegram groups: reading every message, replying in character,
generating images/video, and moderating ads — with hard authorization and
anti-extraction rules.

> This is a sanitized snapshot of a live system (2026-11-22). All tokens,
> chat/user IDs, and the server IP are replaced with placeholders
> (`__BRIDGE_TOKEN__`, `GROUP_ID_*`, `OWNER_USER_ID`, `YOUR_SERVER_IP`, …).

## Components

```
Telegram groups
      │  getUpdates (30s long poll)
      ▼
┌─────────────────────────────┐        HTTP API (token in path)
│ bridge.py (server, stdlib   │ ◄──────────────────────────────┐
│ only, systemd service)      │                                │
│  - queue in state.json      │                                │
│  - fetch-and-claim + lease  │                                │
│  - media download/cache     │                                │
│  - per-group JSONL archive  │                                │
└─────────────────────────────┘                                │
      ▲ polls every 10s                                        │
┌─────┴──────────────────────┐                                 │
│ hook scripts (bash)        │  wake with claimed batch        │
│ one per group              ├──────────────►  agent chat (one per group)
└────────────────────────────┘                  reads MD state files,
                                                judges, replies via API,
                                                writes state files
```

1. **server/bridge.py** — single-file Python stdlib HTTP server + Telegram
   poller. No third-party packages. Runs as a dedicated system user under
   systemd on the same host as other workloads; binds a high port; every
   endpoint lives under `/<bridge_token>/…` and passes one
   `secrets.compare_digest` checkpoint; everything else is a uniform 404.
2. **hooks/scripts/*.sh** — tiny bash pollers. Each calls
   `GET /pending?chat_id=<group>`; if the array is non-empty it emits a
   `wake` with the whole claimed batch as payload, else `silent`.
3. **hooks/prompts/*.md** — the per-group rulebook injected into the agent's
   wake turn (authorization, participation, moderation scope, dedup,
   reporting threshold, persona pointers). `combined-legacy.md` is the
   pre-split single-window rulebook, kept for reference.
4. **Agent chats** — one long-lived chat session per group ("window").
   The agent's durable memory is *files*, not the chat context:
   `lulu_canon.md` (persona facts), per-group `sender_classification.md`,
   `recent_matters.md`, `processed_ids.txt`, per-group `knowledge.md`.
   Chats are rotated daily by a cron (fresh chat + brief; hook delivery
   repointed; old chat archived) — see `agent/chat-rotation-cron.md`.

## Data flow (one message)

1. bridge polls `getUpdates`, appends a record to the queue in `state.json`
   and to `archive/<chat_id>.jsonl`. Records are enriched: sender avatar
   (cached), `entities_summary`, `is_forwarded`, `reply_to` (the message
   this one answers — sender + text excerpt — so the agent can tell who is
   being addressed; added 2026-11-22 after two misattributions), sticker
   metadata + downloadable preview, media download on demand (≤20 MB,
   7-day sweep).
2. The group's hook polls `/pending?chat_id=…` every 10 s. The endpoint is
   **fetch-and-claim**: returned records are atomically marked
   `status=processing` with `claimed_at`. A **900 s lease** returns
   un-acked/un-replied processing records to pending (checked in the poll
   loop and lazily at the start of `/pending`).
   The per-group hooks use `idle_only=1&limit=15`: within the same state
   lock, the bridge first reclaims expired leases, then claims at most 15
   pending records only if the group has no unexpired processing records.
   Messages not claimed remain pending in FIFO order. Other groups are
   independent. Existing `/pending` callers without these parameters retain
   their original behavior; `idle_only=1` requires a numeric `chat_id`.
3. The agent wakes with the batch, reads its MD files, judges each record
   (reply / ack / moderate / generate media), sends via
   `POST /reply | /reply_media | /reply_sticker | /moderate | /ack`,
   then updates its state files.
4. Sends are fingerprinted (sha256 of kind+chat+payload) into
   `recent_sends`; an identical send within 120 s is refused `409`
   *before* Telegram is called (duplicate-send gate). A lost response
   returns `502 send outcome unknown` and keeps the fingerprint.

**Limits of the gate:** processing records indicate unfinished bridge work,
not whether Muse's turn has actually completed. A turn may continue after its
last ack/reply, so one or more wake messages can still queue. Conversely, an
unresolved record holds the group until it is handled or its lease expires.
The 900-second lease is not a heartbeat: a slow live turn can overlap a later
claim, and processed IDs / the send gate do not guarantee exactly-once work.
Existing agent prompts also re-fetch `/pending` without a limit, so the hook's
15-record limit caps the initial wake only, not every record handled in a turn.
Prompts and chat participation rules are intentionally unchanged.

Update the bridge before updating the hooks: older servers silently ignore
these optional query parameters and would not apply the gate or limit. This
snapshot contains placeholders and must be adapted to the deployment before
use; no production deployment is performed by this repository change.

## API surface (all under `/<token>/`)

| Endpoint | Purpose |
|---|---|
| `GET /pending?chat_id=` | fetch-and-claim queue (lease 900 s) |
| `POST /reply` `{in_reply_to, text}` | text reply, marks record replied |
| `POST /reply_media` (multipart) | photo/video reply |
| `POST /reply_sticker` `{in_reply_to, file_id}` | sticker reply (viewable library only) |
| `POST /ack` `{id}` | mark handled, no reply |
| `POST /moderate` `{id, mute_hours}` | deleteMessage + restrictChatMember (mute-only; **no ban/kick exists in the code**); hard-refuses owner ids (403, zero Telegram calls); `mute_hours=0` = permanent; "message to delete not found" counts as deleted |
| `GET /media/<record_id>` | fetch an inbound media body for the agent to view |
| `GET /stickers` | sticker library (kind, viewable flag, seen_count) |
| `GET /analysis/<chat_id>` | per-sender aggregation (counts, link/forward ratios, duplication, bio via getChat, cached) |
| `GET /archive/<chat_id>?tail=N` | append-only per-group history (in + out) |
| `GET /status` | bindings, DM binding state |
| `POST /notify` `{text}` | DM the owner (requires DM binding) |
| binding flow | one-time code posted in a group binds it to the owner channel; DM binding mirrors it |

Record terminal states: `replied`, `acked`, `moderated` (never re-issued).

## Security model

- Authorization is numeric-ID only: `sender_id == OWNER_USER_ID` or
  `sender_chat_id == OWNER_CHANNEL_ID`, re-verified per message from
  transport fields. `CHANNEL_BOT_SHARED_ID` (the shared anonymous
  channel-post identity) never authorizes.
- Non-authorized members get chat + a small *safe operations* set
  (image/video generation, public web lookup, read-only inspection of
  files they themselves supplied). Everything else is authorized-only.
- Extraction defense: the group-facing context holds no secrets; credential
  asks get neither-confirm-nor-deny (even from the owner — credentials are
  only ever handled in the owner's private chat). Since 2026-11-20,
  extraction attempts get an in-character scolding reply instead of silence.
- Moderation is mute-only by construction; owner identities are
  un-moderatable by hard guard.

## Known bottlenecks — the optimization targets

Measured live on 2026-11-20 in the busiest group (~150 inbound msgs / 33 min):

1. **End-to-end reply latency is minutes, not seconds.** Budget: ≤10 s poll
   + claim (fast; fresh messages are claimed ~5 s after posting) + **one
   serial agent turn per batch (the dominant cost)**. A self-introduction
   requested at 17:04 was delivered at 17:20 during peak traffic.
2. **Serial turns per window.** Batches queue inside the group's chat;
   batch N+1 waits for batch N. Arrival rate > processing rate ⇒ lag
   compounds until traffic cools. Splitting windows per group (done
   2026-11-20) removed *cross-group* blocking only.
3. **Per-wake fixed overhead.** Every wake re-reads 4 MD files, re-fetches
   `/pending` before sending, then writes 3 state files — before/around
   the actual judging. (Mitigation in force: "speak first, accounting
   after" ordering rule — replies go out before bookkeeping.)
4. **Zombie recycling.** When a wake is cancelled or a turn dies mid-batch,
   its records sit in `processing` until the 900 s lease expires, then get
   re-claimed and re-processed. Observed: **147 stuck records** for one
   group accumulated across a window switch; flushed manually on
   2026-11-20. Needs a real fix (heartbeat/ack-on-claim-next, dead-letter
   handling, or lease keyed to wake liveness).
5. **Whole-batch payloads.** The wake payload embeds full records
   (avatars, entities, sticker blobs); big batches are heavy context for
   the agent before it can act on anything.
6. **Bookkeeping is in the agent's turn.** Classification/recent-matters/
   processed-ids writes are LLM tool calls. Moving mechanical bookkeeping
   (dedup marking, ack of pure chatter) into the bridge or the hook would
   shorten every turn.

Ideas welcome on: priority lanes (actionable messages before chatter),
smaller/claims-capped batches, push instead of poll, server-side
pre-filtering, incremental state sync, and turn-time profiling.

## Tests

`server/test_pending.py` — regression tests for fetch-and-claim
persistence (empty polls don't write, claims persist, expired leases are
reclaimed exactly once), the idle claim gate (busy group claims nothing,
invalid options are 400s), concurrent claiming (two racing hooks split
one batch), and the near-duplicate detector used by `/analysis`.
Run: `cd server && python3 -m unittest test_pending -v` (10 tests).

## Deployment

- Host: any Linux box with Python 3.13 (stdlib only — no pip installs).
- Layout: `/opt/telegram-bridge/` owned by a dedicated user; `config.json`
  and `bot_token.txt` mode 600; systemd unit `server/telegram-bridge.service`.
- Removal: disable unit, delete unit + directory, delete user. No cron,
  no timers, no system packages.
