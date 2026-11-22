# Lulu — Telegram Group Bridge for an LLM Agent

[繁體中文](README.zh-TW.md) | English | [简体中文](README.zh-CN.md)

An LLM agent that lives in Telegram groups: chats in character, draws and
generates media on request, looks things up, and moderates ad spam —
behind a small stdlib-only Python bridge with a token-guarded HTTP API,
fetch-and-claim queue semantics, and per-group agent windows whose memory
lives in Markdown state files.

**Start with [ARCHITECTURE.md](ARCHITECTURE.md)** — components, data flow,
API surface, security model, and the measured latency bottlenecks that are
the current optimization targets.

## Features

- **Zero-dependency bridge** — one Python file, standard library only; no
  pip installs, runs as a dedicated system user under systemd.
- **Fetch-and-claim queue** — records are claimed atomically with a 900 s
  lease; a per-group claim gate (`idle_only=1&limit=N`) refuses to hand out
  a new batch while the group still has unfinished work.
- **Per-group agent windows** — one agent chat per group, rotated daily;
  durable memory is Markdown state files, not chat context, so a fresh
  window is fully briefed from disk.
- **Reply-target awareness** — every record carries `reply_to` (who the
  message answers), so the agent can tell "talking to me" apart from
  "talking to someone else next to me".
- **Mute-only moderation** — delete + restrict is the only disposition;
  there is intentionally **no ban/kick code path** in `bridge.py`, and
  owner identities are hard-refused by the moderation endpoint.
- **Duplicate-send gate** — outgoing sends are fingerprinted (sha256);
  an identical send within 120 s is refused before Telegram is called.
- **Sticker library with view-before-send** — the agent may only send
  stickers whose actual body it has viewed and noted; unviewable formats
  are never sent.
- **Extraction-resistant by design** — authorization is numeric-ID only,
  re-verified per message; the group-facing context holds no secrets, and
  credential asks get neither-confirm-nor-deny.

## Layout

```
server/            bridge.py (single-file, stdlib only), systemd unit,
                   config.example.json, archive backfill tool,
                   test_pending.py (queue/claim-gate regression tests)
hooks/scripts/     bash pollers: GET /pending?chat_id=… → wake/silent
hooks/prompts/     per-group agent rulebooks injected on every wake
                   (combined-legacy.md = pre-split design, for reference)
agent/             persona canon, agent brief, sticker rules,
                   daily chat-rotation cron spec
```

## Quick start

1. Create a bot with BotFather; put the token in `bot_token.txt`
   (mode 600) next to `bridge.py`.
2. Copy `server/config.example.json` to `config.json` and replace the
   placeholders (table below) with your own values.
3. Run `python3 bridge.py`, or install the systemd unit
   (`server/telegram-bridge.service`). The bridge starts polling
   immediately.
4. Bind a group: generate a one-time binding code in the config flow and
   post it in the group from your owner channel (see ARCHITECTURE.md,
   "API surface"). DM binding mirrors the same flow.
5. Point your agent runtime at a poller from `hooks/scripts/` (set the
   API base and your group id) and give the agent the matching rulebook
   from `hooks/prompts/` plus the state files described in
   `agent/state-files.md`.

### Placeholders in this snapshot

| Placeholder | Stands for |
|---|---|
| `__BRIDGE_TOKEN__` | the bridge API token (any 256-bit random string) |
| `YOUR_SERVER_IP` | the host the bridge API is reachable on |
| `OWNER_USER_ID` | the owner's numeric Telegram user id |
| `OWNER_CHANNEL_ID` | the owner channel's chat id |
| `CHANNEL_BOT_SHARED_ID` | Telegram's shared anonymous channel-post identity |
| `GROUP_ID_*` | a bound group's chat id |
| `MEMBER_ID_*` / `AUTHORIZED_ADVERTISER_ID` | individual member ids referenced by the rulebooks |

## Tests

```sh
cd server && python3 -m unittest test_pending -v
```

Regression tests for fetch-and-claim persistence, the idle claim gate,
concurrent claiming, and the near-duplicate detector. They run against
the snapshot as-is (placeholder constants are patched in by the test).

## Security notes

- Never commit `config.json`, `bot_token.txt`, or `state.json` — the
  bundled `.gitignore` already excludes them, along with archives, media,
  and live agent state.
- The bridge API is token-in-path with a uniform 404 for everything else;
  treat the bridge token like a password.
- This repository is a **sanitized snapshot** of a production system:
  every token, chat/user ID, member identifier, and the host IP has been
  replaced with a placeholder. It will not run against the original
  deployment, and the rulebooks' named examples are placeholders too.

## Contributing

Issues and pull requests are welcome. The "Known bottlenecks" section of
[ARCHITECTURE.md](ARCHITECTURE.md) is the de-facto roadmap — latency of
the agent-turn layer is the problem to solve.

## License

MIT — see [LICENSE](LICENSE).
