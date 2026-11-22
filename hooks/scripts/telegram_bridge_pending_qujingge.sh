#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

API_BASE='http://YOUR_SERVER_IP:29301/__BRIDGE_TOKEN__'

response="$(curl --fail --silent --show-error --max-time 10 "$API_BASE/pending?chat_id=GROUP_ID_QUJINGGE&idle_only=1&limit=15" 2>/dev/null)" || {
  log "pending_fetch_failed" '{}'
  silent "pending fetch failed" '{}'
  exit 0
}

count="$(printf '%s' "$response" | jq -r 'if type == "array" then length else 0 end' 2>/dev/null)" || {
  log "pending_parse_failed" '{}'
  silent "pending parse failed" '{}'
  exit 0
}

if [ "$count" -gt 0 ]; then
  payload="$(printf '%s' "$response" | jq -c '{count: length, messages: .}')"
  wake "telegram bridge has pending messages" "$payload"
else
  silent "telegram queue empty" '{}'
fi
