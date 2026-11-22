#!/usr/bin/env bash
# Altare launcher for the Telegram group bridge.
# Altare App Hosting runs this via Procfile (`web: bash altare_start.sh`).
# It writes config.json + bot_token.txt from environment variables at startup
# (secrets never live in git), then starts bridge.py.
#
# Required env vars (set in Altare: Deployments -> Settings -> Variables):
#   TELEGRAM_BOT_TOKEN   bot token from @BotFather
#   BRIDGE_TOKEN         any random 256-bit hex string (the bridge API password)
#   OWNER_USER_ID        owner's numeric Telegram user id, e.g. 5452632641
# Optional env vars:
#   PORT                 set by Altare; bridge listens on it (default 29301)
#   OWNER_CHANNEL_ID     owner channel chat id (may be empty)
#   GROUP_CHAT_ID        group chat id (may be empty; bind later via one-time code)
#   BRIDGE_HOME          where config/state live (default: current directory)
set -euo pipefail

BRIDGE_HOME="${BRIDGE_HOME:-$PWD}"
export BRIDGE_HOME
PORT="${PORT:-29301}"

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is not set}"
: "${BRIDGE_TOKEN:?BRIDGE_TOKEN is not set}"
: "${OWNER_USER_ID:?OWNER_USER_ID is not set}"

mkdir -p "$BRIDGE_HOME/media" "$BRIDGE_HOME/archive"

# bot token file (mode 600, single line)
printf '%s' "$TELEGRAM_BOT_TOKEN" > "$BRIDGE_HOME/bot_token.txt"
chmod 600 "$BRIDGE_HOME/bot_token.txt"

# config.json — generated at every start from env, so a redeploy or a disk
# reset never loses the binding: bindings are pre-filled when GROUP_CHAT_ID
# is known, otherwise the group binds later with the one-time code flow.
python3 - "$BRIDGE_HOME/config.json" <<'PY'
import json, os, sys

path = sys.argv[1]
cfg = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError:
        cfg = {}

cfg["bridge_token"] = os.environ["BRIDGE_TOKEN"]
cfg["api_port"] = int(os.environ.get("PORT", "29301"))
# Telegram ids arrive as JSON numbers; the bridge compares them with `in`
# against these lists, so they MUST be ints here, not strings.
cfg["authorized_ids"] = [int(os.environ["OWNER_USER_ID"])]
group_id = os.environ.get("GROUP_CHAT_ID", "")
owner_channel = os.environ.get("OWNER_CHANNEL_ID", "")
if group_id:
    group_id = int(group_id)
    owner_channel = int(owner_channel) if owner_channel else None
    bindings = [b for b in cfg.get("bindings", []) if b.get("group_chat_id") != group_id]
    bindings.append({"group_chat_id": group_id, "owner_channel_id": owner_channel})
    cfg["bindings"] = bindings
elif "bindings" not in cfg:
    cfg["bindings"] = []
cfg.setdefault("dm_chat_id", None)

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("config.json written (api_port=%s, bindings=%d)" % (cfg["api_port"], len(cfg.get("bindings", []))))
PY

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

exec python3 "$SCRIPT_DIR/server/bridge.py"
