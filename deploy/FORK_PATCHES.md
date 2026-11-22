# Fork patches vs upstream 1743986520/muse-telegram (commit 214c8e9)
# Applied in this working copy so the fork can deploy on Altare.
# Nothing here touches the queue/claim/moderation logic.

## 1. server/bridge.py — placeholder names that crash on import (critical)
Upstream left two sanitized placeholders as bare names:
- `MAIN_OWNER_CHANNEL_ID = OWNER_CHANNEL_ID` → NameError at import, the
  bridge never starts (found by running it: `python3 bridge.py` dies
  immediately; the unit tests pass only because they patch these constants).
- `CHANNEL_BOT_SHARED_ID = CHANNEL_BOT_SHARED_ID` → same NameError.
Fix:
- `MAIN_OWNER_CHANNEL_ID` now reads `OWNER_CHANNEL_ID` env (int, or None
  when the owner has no channel). Only used when an authorized person posts
  the binding code personally.
- `CHANNEL_BOT_SHARED_ID = 136817688`, Telegram's well-known public
  @Channel_Bot user id. Only used to refuse adding that shared identity to
  authorized_ids.
- `MAIN_OWNER_CHANNEL_TITLE` changed from "帝" (previous owner's channel
  name) to "Anbei".

## 2. altare_start.sh / Procfile / requirements.txt (new files)
- `altare_start.sh`: writes config.json + bot_token.txt (mode 600) from
  Altare env vars at every start, then execs bridge.py. Port comes from
  Altare's `$PORT` (bridge already binds 0.0.0.0).
- IDs are written as JSON numbers, not strings: the bridge compares
  Telegram's numeric ids with `in` against authorized_ids / bindings, so
  string ids would silently never match and no message would ever be queued.
- `Procfile`: `web: bash altare_start.sh`.
- `requirements.txt`: stdlib only, present so Altare picks the Python runtime.

## 3. agent/muse_canon.md, hooks/prompts/muse_template.md (new files)
Muse-as-itself persona + per-group rulebook template, replacing the
repo's Lulu canon and the previous owner's named members/groups.
Both are drafts awaiting the user's confirmation.
