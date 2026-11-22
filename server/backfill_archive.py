#!/usr/bin/env python3
"""One-time backfill of per-group archives from state.json's queue.

Writes archive/<chat_id>.jsonl for every chat present in the queue:
one "in" entry per record plus one "out" entry per replied record
(reply_text / reply_kind). Refuses to touch a non-empty existing file.
Run once, with the bridge service stopped, as root; files are handed
to the tgbridge user so the service can keep appending.
"""
import json
import os
import pwd
import sys

BASE = "/opt/telegram-bridge"
STATE = os.path.join(BASE, "state.json")
ADIR = os.path.join(BASE, "archive")


def main():
    st = json.load(open(STATE, encoding="utf-8"))
    tg = pwd.getpwnam("tgbridge")
    os.makedirs(ADIR, exist_ok=True)
    groups = {}
    for rec in sorted(st.get("queue") or [], key=lambda r: r.get("id", 0)):
        chat = rec.get("chat_id")
        if chat is None:
            continue
        entries = groups.setdefault(chat, [])
        media_ref = rec.get("media_file")
        media_type = rec.get("media_type")
        if rec.get("sticker"):
            media_ref = rec["sticker"].get("file_id")
            media_type = "sticker"
        entries.append(
            {
                "ts": rec.get("timestamp"),
                "direction": "in",
                "chat_id": chat,
                "record_id": rec.get("id"),
                "message_id": rec.get("message_id"),
                "sender_id": rec.get("sender_id"),
                "sender_name": rec.get("sender_name"),
                "sender_chat_id": rec.get("sender_chat_id"),
                "sender_chat_title": rec.get("sender_chat_title"),
                "text": rec.get("text"),
                "media": media_ref,
                "media_type": media_type,
            }
        )
        if rec.get("reply_text") or rec.get("reply_kind"):
            kind = "text" if rec.get("reply_text") else rec.get("reply_kind")
            entries.append(
                {
                    "ts": rec.get("replied_at") or rec.get("timestamp"),
                    "direction": "out",
                    "chat_id": chat,
                    "record_id": rec.get("id"),
                    "message_id": None,
                    "sender_id": st.get("bot_id"),
                    "sender_name": st.get("bot_username"),
                    "kind": kind,
                    "text": rec.get("reply_text"),
                    "media": None,
                    "media_type": kind if kind in ("photo", "video", "sticker") else None,
                }
            )
    for chat, entries in sorted(groups.items()):
        path = os.path.join(ADIR, "%s.jsonl" % chat)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print("SKIP (exists, non-empty): chat=%s" % chat)
            continue
        with open(path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.chown(path, tg.pw_uid, tg.pw_gid)
        os.chmod(path, 0o644)
        ins = sum(1 for e in entries if e["direction"] == "in")
        outs = sum(1 for e in entries if e["direction"] == "out")
        print("backfilled chat=%s: %d in, %d out" % (chat, ins, outs))
    os.chown(ADIR, tg.pw_uid, tg.pw_gid)
    os.chmod(ADIR, 0o755)


if __name__ == "__main__":
    sys.exit(main())
