#!/usr/bin/env python3
"""Telegram group bridge service (Python standard library only).

- Polls Telegram via getUpdates (long polling) once a bot token is present
  in bot_token.txt; silently waits (re-checking every 60s) while it is absent.
- Stores group messages in a persistent pending queue (state.json).
- Serves a small token-guarded HTTP API:
    GET  /<bridge_token>/pending  -> fetch-and-claim: returns pending queue
                                 messages and atomically marks them processing
                                 (300s lease, then they return to pending)
    GET  /<bridge_token>/status   -> binding status (JSON object)
    POST /<bridge_token>/reply    -> {in_reply_to, text} send reply, mark replied
    POST /<bridge_token>/reply_media -> multipart {in_reply_to, file} send
                                 photo/video reply, mark replied
    POST /<bridge_token>/ack      -> {id} mark handled without replying
  Every other path returns 404.
- Duplicate-send gate on reply / reply_media / reply_sticker: an identical
  send (same chat + same text / file content / sticker file_id) is
  fingerprinted in state (recent_sends, kept 10 minutes) and a second
  identical send within 120 seconds is refused with 409 before Telegram
  is called. Sends are never retried automatically; when a response is
  lost the outcome is reported as unknown and the fingerprint is kept,
  so a blind resend stays blocked while the caller verifies.
- One-time owner binding: while config.json holds a pending_auth_code and the
  30-minute window (opened when polling first starts with a token) is open, a
  group message containing the code AND sent as a channel (sender_chat) binds
  that group + channel as owner; the code is then deleted from config.
"""

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.environ.get("BRIDGE_HOME", "/opt/telegram-bridge")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
BOT_TOKEN_PATH = os.path.join(BASE_DIR, "bot_token.txt")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Telegram bot API file download limit
MEDIA_RETENTION_SECONDS = 7 * 24 * 3600
CLAIM_LEASE_SECONDS = 900
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
# Main owner channel (帝): an authorized personal account sending the
# binding code binds the group to this channel.
MAIN_OWNER_CHANNEL_ID = OWNER_CHANNEL_ID
MAIN_OWNER_CHANNEL_TITLE = "帝"
# Telegram's shared @Channel_Bot anonymous identity: it is the from.id
# of ANY channel-posted message, never a personal account, and must
# never be added to authorized_ids.
CHANNEL_BOT_SHARED_ID = CHANNEL_BOT_SHARED_ID

_lock = threading.Lock()
_archive_lock = threading.Lock()
_tg_active = {"active": False}
AUTH_WINDOW_SECONDS = 1800


def log(msg):
    print("[bridge] %s" % msg, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    return cfg


def save_config(cfg):
    """Persist config.json atomically, keeping it chmod 600."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def load_state():
    st = load_json(STATE_PATH, {})
    st.setdefault("offset", 0)
    st.setdefault("bot_username", None)
    st.setdefault("bot_id", None)
    st.setdefault("next_id", 1)
    st.setdefault("queue", [])
    st.setdefault("auth_window_start", None)
    st.setdefault("auth_window_end", None)
    st.setdefault("pending_auth_window_start", None)
    st.setdefault("pending_auth_window_end", None)
    st.setdefault("avatars", {})
    st.setdefault("bios", {})
    st.setdefault("stickers", [])
    st.setdefault("recent_sends", [])
    st.setdefault("dm_window_start", None)
    st.setdefault("dm_window_end", None)
    return st


def get_bindings(cfg):
    """All owner bindings. Falls back to synthesizing one entry from the
    legacy single-binding fields when the bindings array is absent."""
    bindings = cfg.get("bindings")
    if bindings:
        return bindings
    if cfg.get("group_chat_id") is not None:
        return [
            {
                "group_chat_id": cfg.get("group_chat_id"),
                "owner_channel_id": cfg.get("owner_channel_id"),
                "owner_channel_title": cfg.get("owner_channel_title"),
                "bound_at": None,
            }
        ]
    return []


def allowed_chat_ids(cfg):
    """Set of group chat ids the bridge accepts messages from, or None to
    accept all groups (nothing bound yet)."""
    bindings = get_bindings(cfg)
    if not bindings:
        return None
    return {b.get("group_chat_id") for b in bindings}


def save_state(st):
    save_json_atomic(STATE_PATH, st)


def read_bot_token():
    try:
        with open(BOT_TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def tg_call(token, method, params=None, timeout=45):
    url = TELEGRAM_API.format(token=token, method=method)
    data = None
    headers = {}
    if params is not None:
        data = json.dumps(params).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
MAX_UPLOAD_BYTES = 55 * 1024 * 1024


def media_kind(filename, content_type):
    """Return ('sendPhoto', 'photo') or ('sendVideo', 'video'), else None."""
    ext = os.path.splitext(filename or "")[1].lower()
    mime = (content_type or "").lower()
    if mime.startswith("image/") or ext in PHOTO_EXTS:
        return ("sendPhoto", "photo")
    if mime.startswith("video/") or ext in VIDEO_EXTS:
        return ("sendVideo", "video")
    return None


def tg_send_media(token, method, field_name, chat_id, reply_to_message_id,
                  filename, content_type, data):
    """POST a media file to Telegram using multipart/form-data."""
    boundary = "----bridge" + secrets.token_hex(12)
    chunks = []

    def add_field(name, value):
        chunks.append(("--" + boundary + "\r\n").encode())
        chunks.append(
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        )
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("reply_to_message_id", reply_to_message_id)
    chunks.append(("--" + boundary + "\r\n").encode())
    chunks.append(
        (
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (field_name, filename.replace('"', "_"))
        ).encode()
    )
    chunks.append(
        ("Content-Type: %s\r\n\r\n" % (content_type or "application/octet-stream")).encode()
    )
    chunks.append(data)
    chunks.append(b"\r\n")
    chunks.append(("--" + boundary + "--\r\n").encode())
    body = b"".join(chunks)
    url = TELEGRAM_API.format(token=token, method=method)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_multipart(body, content_type_header):
    """Minimal multipart/form-data parser (stdlib has no server-side parser).

    Returns (fields: dict[str, str], files: dict[str, dict]).
    """
    m = re.search(r"boundary=([^;]+)", content_type_header or "")
    if not m:
        raise ValueError("no boundary in Content-Type")
    boundary = m.group(1).strip().strip('"').encode()
    delimiter = b"--" + boundary
    fields = {}
    files = {}
    for segment in body.split(delimiter):
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        if not segment or segment == b"--":
            continue
        head, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_text = head.decode("latin-1")
        name_m = re.search(r'name="([^"]+)"', head_text)
        if not name_m:
            continue
        name = name_m.group(1)
        filename_m = re.search(r'filename="([^"]*)"', head_text)
        ctype_m = re.search(r"Content-Type:\s*([^\r\n]+)", head_text, re.IGNORECASE)
        if filename_m is not None:
            files[name] = {
                "filename": filename_m.group(1),
                "content_type": ctype_m.group(1).strip() if ctype_m else "",
                "data": content,
            }
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def extract_sticker(msg):
    """Return {file_id, emoji, set_name} for a sticker message, else None."""
    obj = msg.get("sticker")
    if not obj or not obj.get("file_id"):
        return None
    return {
        "file_id": obj["file_id"],
        "emoji": obj.get("emoji"),
        "set_name": obj.get("set_name"),
    }


def sticker_kind(msg):
    """Classify a sticker message: 'webp' (static), 'webm' (video) or
    'tgs' (animated Lottie)."""
    obj = msg.get("sticker") or {}
    if obj.get("is_animated"):
        return "tgs"
    if obj.get("is_video"):
        return "webm"
    return "webp"


def note_sticker_seen(st, sticker, kind, record_id):
    """Upsert a sticker into the state library, counting sightings.

    Returns True when a preview download is needed: the entry is viewable
    in principle (not TGS) but has no preview file yet, or its preview file
    was swept by the media retention cleanup. TGS stickers are marked
    viewable=False permanently - there is no viewable preview for them.
    """
    lib = st.setdefault("stickers", [])
    entry = None
    for cand in lib:
        if cand.get("file_id") == sticker["file_id"]:
            entry = cand
            break
    if entry is None:
        entry = {
            "file_id": sticker["file_id"],
            "emoji": sticker.get("emoji"),
            "set_name": sticker.get("set_name"),
            "seen_count": 0,
            "kind": kind,
            "viewable": False,
            "preview_file": None,
            "sample_record_id": record_id,
        }
        lib.append(entry)
    entry["seen_count"] = entry.get("seen_count", 0) + 1
    entry["sample_record_id"] = record_id
    if not entry.get("emoji"):
        entry["emoji"] = sticker.get("emoji")
    if not entry.get("set_name"):
        entry["set_name"] = sticker.get("set_name")
    entry.setdefault("kind", kind)
    entry.setdefault("viewable", False)
    entry.setdefault("preview_file", None)
    if kind == "tgs":
        entry["viewable"] = False
        return False
    preview = entry.get("preview_file")
    if preview and os.path.isfile(os.path.join(MEDIA_DIR, preview)):
        entry["viewable"] = True
        return False
    entry["preview_file"] = None
    entry["viewable"] = False
    return True


def extract_media(msg):
    """Return (media_type, file_id, file_size) for photo/video/animation/
    image-or-video document messages, else None."""
    photos = msg.get("photo")
    if photos:
        largest = photos[-1]
        return ("photo", largest.get("file_id"), largest.get("file_size"))
    video = msg.get("video")
    if video:
        return ("video", video.get("file_id"), video.get("file_size"))
    animation = msg.get("animation")
    if animation:
        return ("animation", animation.get("file_id"), animation.get("file_size"))
    document = msg.get("document")
    if document:
        mime = (document.get("mime_type") or "").lower()
        if mime.startswith("image/") or mime.startswith("video/"):
            return ("document", document.get("file_id"), document.get("file_size"))
    return None


def download_telegram_file(token, file_id, file_size_hint, make_filename):
    """Download one Telegram file into MEDIA_DIR. make_filename(ext) names it.

    Returns (filename, None) on success or (None, note) when skipped/failed.
    Files over the 20 MB Telegram bot download limit are not downloaded.
    """
    if file_size_hint and file_size_hint > TG_DOWNLOAD_LIMIT:
        return (None, "file too large: %d bytes exceeds 20 MB bot download limit" % file_size_hint)
    try:
        res = tg_call(token, "getFile", {"file_id": file_id}, timeout=30)
    except Exception as exc:
        return (None, "getFile failed: %r" % (exc,))
    if not res.get("ok"):
        return (None, "getFile failed: %s" % (res.get("description") or "unknown error"))
    result = res.get("result") or {}
    real_size = result.get("file_size") or file_size_hint
    if real_size and real_size > TG_DOWNLOAD_LIMIT:
        return (None, "file too large: %d bytes exceeds 20 MB bot download limit" % real_size)
    file_path = result.get("file_path")
    if not file_path:
        return (None, "getFile returned no file_path")
    ext = os.path.splitext(file_path)[1].lower() or ".bin"
    filename = make_filename(ext)
    dest = os.path.join(MEDIA_DIR, filename)
    url = "https://api.telegram.org/file/bot%s/%s" % (token, file_path)
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        total = 0
        with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > TG_DOWNLOAD_LIMIT:
                    out.close()
                    os.unlink(dest)
                    return (None, "file too large: exceeded 20 MB while downloading")
                out.write(chunk)
    except Exception as exc:
        try:
            if os.path.exists(dest):
                os.unlink(dest)
        except OSError:
            pass
        return (None, "download failed: %r" % (exc,))
    return (filename, None)


def fetch_media_file(token, record_id, file_id, file_size):
    """Download a message attachment as <record_id><ext>."""
    return download_telegram_file(
        token, file_id, file_size, lambda ext: "%d%s" % (record_id, ext)
    )


def fetch_avatar(token, sender_id):
    """Fetch a user's first profile photo as avatar_<sender_id><ext>.

    Returns (filename_or_None, cacheable). cacheable is True when the result
    is definitive (photo downloaded, or the user has no profile photo) and
    may be cached per sender; transient failures are not cached.
    """
    try:
        res = tg_call(
            token, "getUserProfilePhotos", {"user_id": sender_id, "limit": 1}, timeout=30
        )
    except Exception:
        return (None, False)
    if not res.get("ok"):
        return (None, False)
    photos = (res.get("result") or {}).get("photos") or []
    if not photos or not photos[0]:
        return (None, True)
    largest = photos[0][-1]
    filename, _note = download_telegram_file(
        token,
        largest.get("file_id"),
        largest.get("file_size"),
        lambda ext: "avatar_%d%s" % (sender_id, ext),
    )
    if filename:
        return (filename, True)
    return (None, False)


def http_error_description(exc):
    """Telegram's error description from an HTTPError body, else repr(exc).

    The Bot API returns the cause in the response JSON body (e.g.
    "Bad Request: message to be replied not found"); urllib's default
    exception repr only carries the HTTP status, so callers that need the
    reason must read the body here.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if body.get("description"):
                return body["description"]
        except Exception:
            pass
    return repr(exc)


def tg_call_outcome(token, method, params, timeout=30):
    """Classify a Telegram call for send-path duplicate protection.

    Returns ("ok", result) when Telegram confirmed success,
    ("rejected", description) when Telegram answered and refused - the
    operation definitively did NOT happen - and ("unknown", error) when
    the response was lost (timeout / network), in which case the
    operation may or may not have happened and must not be blindly
    retried. tg_call itself performs exactly one HTTP request; nothing
    in the send path retries automatically.
    """
    try:
        res = tg_call(token, method, params, timeout=timeout)
    except urllib.error.HTTPError as exc:
        desc = None
        try:
            body = json.loads(exc.read().decode("utf-8"))
            desc = body.get("description")
        except Exception:
            pass
        if desc:
            return ("rejected", desc)
        return ("unknown", str(exc))
    except Exception as exc:
        return ("unknown", repr(exc))
    if res.get("ok"):
        return ("ok", res.get("result"))
    return ("rejected", res.get("description") or "telegram error")


def tg_call_safe(token, method, params, timeout=30):
    """tg_call that never raises: returns (True, result) or
    (False, telegram_error_description)."""
    status, payload = tg_call_outcome(token, method, params, timeout=timeout)
    return (status == "ok", payload)


SEND_GATE_WINDOW = 120  # identical resend blocked within this many seconds
SEND_GATE_RETENTION = 600  # send fingerprints kept this long in state


def send_fingerprint(kind, chat_id, payload):
    """Fingerprint one outbound send: kind + chat + content."""
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\0")
    h.update(str(chat_id).encode("utf-8"))
    h.update(b"\0")
    h.update(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
    return h.hexdigest()


def gate_reserve(st, fp, chat_id, record_id):
    """Reserve a send fingerprint in state. Returns False when an
    identical send already happened (or is in flight) inside the gate
    window - the caller must NOT send again. Old fingerprints beyond
    the retention window are pruned here."""
    now = int(time.time())
    sends = [
        s
        for s in (st.get("recent_sends") or [])
        if now - s.get("at", 0) < SEND_GATE_RETENTION
    ]
    for s in sends:
        if s.get("fp") == fp and now - s.get("at", 0) < SEND_GATE_WINDOW:
            st["recent_sends"] = sends
            return False
    sends.append({"fp": fp, "chat_id": chat_id, "record_id": record_id, "at": now})
    st["recent_sends"] = sends
    return True


def archive_append(chat_id, entry):
    """Append one JSON line to the per-group archive
    (archive/<chat_id>.jsonl, append-only, no rotation)."""
    if chat_id is None:
        return
    try:
        with _archive_lock:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            path = os.path.join(ARCHIVE_DIR, "%s.jsonl" % chat_id)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log("archive write failed: chat=%s error=%r" % (chat_id, exc))


def archive_read_tail(chat_id, limit):
    """Read the last `limit` entries of a group's archive ([] if none)."""
    path = os.path.join(ARCHIVE_DIR, "%s.jsonl" % chat_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def gate_release(st, fp):
    """Drop a fingerprint after a definitively rejected send, so a
    corrected retry is not blocked. Never called for lost responses:
    an unknown-outcome send keeps its fingerprint for the full window."""
    st["recent_sends"] = [
        s for s in (st.get("recent_sends") or []) if s.get("fp") != fp
    ]


_cleanup_state = {"last": 0}


def cleanup_old_media():
    """Delete media files older than 7 days. Self-throttled to hourly;
    called from the main loops, no cron involved."""
    now = time.time()
    if now - _cleanup_state["last"] < 3600:
        return
    _cleanup_state["last"] = now
    try:
        names = os.listdir(MEDIA_DIR)
    except OSError:
        return
    removed = 0
    for name in names:
        path = os.path.join(MEDIA_DIR, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > MEDIA_RETENTION_SECONDS:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    if removed:
        log("media cleanup: removed %d expired file(s)" % removed)


def sender_name(user):
    if not user:
        return ""
    parts = [user.get("first_name", ""), user.get("last_name", "")]
    name = " ".join(p for p in parts if p).strip()
    if not name:
        name = user.get("username", "") or str(user.get("id", ""))
    return name


def attempt_dm_binding(msg, text):
    """Try to bind the owner's DM chat via the one-time DM code.

    Returns True when the message carried the code and was consumed as a
    control message. Binding succeeds only for a private chat, inside the
    armed window, and when the sender is in authorized_ids; on success the
    chat id is stored as dm_chat_id and the code is burned.
    """
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return False
    cfg = load_config()
    code = cfg.get("pending_dm_code")
    if not code or not text or code not in text:
        return False
    st = load_state()
    now = int(time.time())
    start = st.get("dm_window_start")
    end = st.get("dm_window_end")
    if not (start is not None and end is not None and start <= now <= end):
        log("dm code seen outside the binding window; ignored")
        return True
    from_id = (msg.get("from") or {}).get("id")
    if from_id is None or from_id not in (cfg.get("authorized_ids") or []):
        log("warning: dm code from unauthorized sender_id=%s; not binding" % (from_id,))
        return True
    cfg["dm_chat_id"] = chat.get("id")
    cfg.pop("pending_dm_code", None)
    save_config(cfg)
    log("dm bound: chat_id=%s" % (cfg["dm_chat_id"],))
    return True


def ensure_auth_window():
    """Open the 30-minute binding window the first time polling runs with a
    token. The window is persisted in state.json and never re-opens."""
    with _lock:
        st = load_state()
        if st.get("auth_window_start") is None:
            now = int(time.time())
            st["auth_window_start"] = now
            st["auth_window_end"] = now + AUTH_WINDOW_SECONDS
            save_state(st)
            log("binding window opened (30 minutes)")


def attempt_binding(msg, text):
    """Check one group message for the one-time auth code.

    Returns True if the message contained the pending code (it is a control
    message and must not be queued), False otherwise. The code itself is
    never logged.
    """
    cfg = load_config()
    code = cfg.get("pending_auth_code")
    if not code or not text or code not in text:
        return False
    st = load_state()
    now = int(time.time())
    # The armed window for the current code lives in the pending_auth_*
    # fields; fall back to the original first-binding window fields.
    start = st.get("pending_auth_window_start")
    end = st.get("pending_auth_window_end")
    if start is None:
        start = st.get("auth_window_start")
        end = st.get("auth_window_end")
    in_window = start is not None and end is not None and start <= now <= end
    if not in_window:
        log("auth code seen outside the binding window; ignored")
        return True
    sender_chat = msg.get("sender_chat")
    from_user = msg.get("from") or {}
    from_id = from_user.get("id")
    if sender_chat:
        # Sent as a channel: bind the group to that channel.
        channel_id = sender_chat.get("id")
        channel_title = sender_chat.get("title")
        via = "channel"
    elif from_id is not None and from_id in (cfg.get("authorized_ids") or []):
        # Sent personally by an authorized account: bind the group to
        # the main owner channel (帝).
        channel_id = MAIN_OWNER_CHANNEL_ID
        channel_title = MAIN_OWNER_CHANNEL_TITLE
        via = "authorized_personal"
    else:
        log(
            "warning: auth code received without sender_chat from unauthorized sender_id=%s; not binding"
            % from_id
        )
        return True
    chat = msg.get("chat", {})
    group_id = chat.get("id")
    bindings = get_bindings(cfg)
    already = any(
        b.get("group_chat_id") == group_id and b.get("owner_channel_id") == channel_id
        for b in bindings
    )
    if not already:
        bindings.append(
            {
                "group_chat_id": group_id,
                "owner_channel_id": channel_id,
                "owner_channel_title": channel_title,
                "bound_at": now,
            }
        )
    cfg["bindings"] = bindings
    # Legacy single-binding fields mirror the first binding.
    first = bindings[0]
    cfg["group_chat_id"] = first.get("group_chat_id")
    cfg["owner_channel_id"] = first.get("owner_channel_id")
    cfg["owner_channel_title"] = first.get("owner_channel_title")
    if from_id is not None and from_id != CHANNEL_BOT_SHARED_ID:
        ids = cfg.get("authorized_ids") or []
        if from_id not in ids:
            ids.append(from_id)
        cfg["authorized_ids"] = ids
    cfg.pop("pending_auth_code", None)
    save_config(cfg)
    if already:
        log(
            "binding already existed for group_chat_id=%s owner_channel_id=%s; code burned"
            % (group_id, channel_id)
        )
    else:
        log(
            "binding completed: group_chat_id=%s owner_channel_id=%s owner_channel_title=%s via=%s (total bindings: %d)"
            % (group_id, channel_id, channel_title, via, len(bindings))
        )
    return True


BOT_COMMANDS = [
    {"command": "help", "description": "露露會的東西都在這裡喵"},
    {"command": "draw", "description": "叫露露畫一張圖，後面接描述"},
    {"command": "video", "description": "叫露露拍一小段影片，後面接描述"},
    {"command": "id", "description": "查你在 Telegram 的數字 ID"},
    {"command": "status", "description": "看露露有沒有醒著"},
]


def register_commands(token):
    """Register the bot command menu via setMyCommands (default scope).
    Failure is logged, never fatal - polling continues regardless."""
    try:
        res = tg_call(token, "setMyCommands", {"commands": BOT_COMMANDS}, timeout=30)
        if res.get("ok"):
            log("bot commands registered (%d)" % len(BOT_COMMANDS))
        else:
            log("setMyCommands returned not-ok: %s" % (res.get("description") or res,))
    except Exception as exc:
        log("setMyCommands failed (continuing): %r" % (exc,))


def poll_loop():
    announced_waiting = False
    while True:
        cleanup_old_media()
        reclaim_claims_locked()
        token = read_bot_token()
        if not token:
            _tg_active["active"] = False
            if not announced_waiting:
                log("bot_token.txt not present; waiting (re-check every 60s)")
                announced_waiting = True
            time.sleep(60)
            continue
        announced_waiting = False
        try:
            run_polling(token)
        except Exception as exc:  # never crash the service on poll errors
            _tg_active["active"] = False
            log("polling error: %r; retrying in 10s" % (exc,))
            time.sleep(10)


def run_polling(token):
    ensure_auth_window()
    with _lock:
        st = load_state()
        need_me = not st.get("bot_username")
    if need_me:
        me = tg_call(token, "getMe", {}, timeout=30)
        if me.get("ok"):
            with _lock:
                st = load_state()
                st["bot_username"] = me["result"].get("username")
                st["bot_id"] = me["result"].get("id")
                save_state(st)
            log("bot identity: @%s (id %s)" % (st["bot_username"], st["bot_id"]))
    register_commands(token)
    _tg_active["active"] = True
    while True:
        reclaim_claims_locked()
        if read_bot_token() != token:
            _tg_active["active"] = False
            log("bot token changed/removed; returning to wait loop")
            return
        with _lock:
            offset = load_state().get("offset", 0)
        try:
            res = tg_call(
                token,
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=45,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 404):
                _tg_active["active"] = False
                log("getUpdates rejected (HTTP %s); token may be invalid, waiting 60s" % exc.code)
                time.sleep(60)
                return
            raise
        if not res.get("ok"):
            time.sleep(5)
            continue
        updates = res.get("result", [])
        if not updates:
            continue
        cfg = load_config()
        allowed_chats = allowed_chat_ids(cfg)
        media_jobs = []
        avatar_jobs = {}  # sender_id -> [record_id, ...]
        sticker_jobs = []  # [(record_id, file_id), ...] needing a preview
        archive_ids = []  # record ids to write into the group archive
        with _lock:
            st = load_state()
            bot_username = st.get("bot_username")
            bot_id = st.get("bot_id")
            avatars_cache = st.get("avatars") or {}
            for upd in updates:
                st["offset"] = max(st.get("offset", 0), upd.get("update_id", 0) + 1)
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                text = msg.get("text") or msg.get("caption") or ""
                if attempt_dm_binding(msg, text):
                    # DM control message consumed; private chats otherwise
                    # never enter the queue
                    continue
                if chat.get("type") not in ("group", "supergroup"):
                    continue
                if attempt_binding(msg, text):
                    # control message consumed; refresh the group filter in
                    # case this message just bound a group
                    allowed_chats = allowed_chat_ids(load_config())
                    continue
                if allowed_chats is not None and chat.get("id") not in allowed_chats:
                    continue
                mentioned = False
                if bot_username and ("@" + bot_username).lower() in text.lower():
                    mentioned = True
                reply_to = msg.get("reply_to_message") or {}
                reply_from = reply_to.get("from") or {}
                if bot_id is not None and reply_from.get("id") == bot_id:
                    mentioned = True
                if bot_username and reply_from.get("username") == bot_username:
                    mentioned = True
                media = extract_media(msg)
                sticker = extract_sticker(msg)
                from_id = (msg.get("from") or {}).get("id")
                entities = msg.get("entities") or msg.get("caption_entities") or []
                entities_summary = {}
                for ent in entities:
                    etype = ent.get("type", "unknown")
                    entities_summary[etype] = entities_summary.get(etype, 0) + 1
                reply_to_info = None
                if reply_to:
                    reply_to_info = {
                        "message_id": reply_to.get("message_id"),
                        "sender_id": reply_from.get("id"),
                        "sender_name": sender_name(reply_from) if reply_from else None,
                        "sender_chat_id": (reply_to.get("sender_chat") or {}).get("id"),
                        "text": (reply_to.get("text") or reply_to.get("caption") or "")[:200],
                    }
                record = {
                    "id": st["next_id"],
                    "chat_id": chat.get("id"),
                    "message_id": msg.get("message_id"),
                    "sender_id": from_id,
                    "sender_name": sender_name(msg.get("from")),
                    "sender_chat_id": (msg.get("sender_chat") or {}).get("id"),
                    "sender_chat_title": (msg.get("sender_chat") or {}).get("title"),
                    "sender_avatar": avatars_cache.get(str(from_id)) if from_id else None,
                    "is_forwarded": bool(
                        msg.get("forward_origin")
                        or msg.get("forward_from")
                        or msg.get("forward_from_chat")
                        or msg.get("forward_sender_name")
                    ),
                    "entities_summary": entities_summary,
                    "sticker": sticker,
                    "text": text,
                    "media_type": media[0] if media else None,
                    "media_file": None,
                    "media_note": None,
                    "mentioned_bot": mentioned,
                    "reply_to": reply_to_info,
                    "timestamp": msg.get("date"),
                    "status": "pending",
                }
                st["next_id"] += 1
                st["queue"].append(record)
                archive_ids.append(record["id"])
                if sticker:
                    needs_preview = note_sticker_seen(
                        st, sticker, sticker_kind(msg), record["id"]
                    )
                    if needs_preview and all(
                        job[1] != sticker["file_id"] for job in sticker_jobs
                    ):
                        sticker_jobs.append((record["id"], sticker["file_id"]))
                if media:
                    media_jobs.append((record["id"], media[1], media[2]))
                if from_id and str(from_id) not in avatars_cache:
                    avatar_jobs.setdefault(from_id, []).append(record["id"])
            save_state(st)
        # Avatar fetches also run outside the lock; results are patched
        # into the records and the per-sender cache afterwards.
        for sender_id, record_ids in avatar_jobs.items():
            filename, cacheable = fetch_avatar(token, sender_id)
            with _lock:
                st = load_state()
                if cacheable:
                    cache = st.get("avatars") or {}
                    cache[str(sender_id)] = filename
                    st["avatars"] = cache
                for rid in record_ids:
                    rec = find_record(st, rid)
                    if rec is not None:
                        rec["sender_avatar"] = filename
                save_state(st)
        # Sticker previews also download outside the lock. A successful
        # preview makes the library entry viewable and is attached to the
        # sighting record as its media file, so the sticker body can be
        # fetched through the usual GET /media/<record_id> path.
        for record_id, file_id in sticker_jobs:
            digest = hashlib.sha1(file_id.encode("utf-8")).hexdigest()[:16]
            filename, _note = download_telegram_file(
                token,
                file_id,
                None,
                lambda ext, d=digest: "stk_%s%s" % (d, ext),
            )
            if filename is not None and os.path.splitext(filename)[1].lower() not in (
                ".webp",
                ".webm",
            ):
                # Unexpected format (e.g. a .tgs body): not viewable.
                try:
                    os.unlink(os.path.join(MEDIA_DIR, filename))
                except OSError:
                    pass
                filename = None
            if filename is None:
                continue
            with _lock:
                st = load_state()
                for entry in st.get("stickers") or []:
                    if entry.get("file_id") == file_id:
                        entry["preview_file"] = filename
                        entry["viewable"] = True
                rec = find_record(st, record_id)
                if rec is not None:
                    rec["media_type"] = "sticker"
                    rec["media_file"] = filename
                save_state(st)
        # Downloads run outside the state lock so the HTTP API stays
        # responsive; results are patched into the records afterwards.
        for record_id, file_id, file_size in media_jobs:
            filename, note = fetch_media_file(token, record_id, file_id, file_size)
            with _lock:
                st = load_state()
                rec = find_record(st, record_id)
                if rec is not None:
                    rec["media_file"] = filename
                    rec["media_note"] = note
                    save_state(st)
            if filename:
                log("media downloaded for record %d: %s" % (record_id, filename))
            else:
                log("media not downloaded for record %d: %s" % (record_id, note))
        # Archive the batch's inbound records last, when media/sticker
        # patches have settled, so entries carry the final media refs.
        if archive_ids:
            with _lock:
                st = load_state()
                by_id = {r["id"]: r for r in st["queue"]}
            for rid in archive_ids:
                rec = by_id.get(rid)
                if rec is None:
                    continue
                media_ref = rec.get("media_file")
                media_type = rec.get("media_type")
                if rec.get("sticker"):
                    media_ref = rec["sticker"].get("file_id")
                    media_type = "sticker"
                archive_append(
                    rec["chat_id"],
                    {
                        "ts": rec.get("timestamp"),
                        "direction": "in",
                        "chat_id": rec["chat_id"],
                        "record_id": rec["id"],
                        "message_id": rec.get("message_id"),
                        "sender_id": rec.get("sender_id"),
                        "sender_name": rec.get("sender_name"),
                        "sender_chat_id": rec.get("sender_chat_id"),
                        "sender_chat_title": rec.get("sender_chat_title"),
                        "text": rec.get("text"),
                        "media": media_ref,
                        "media_type": media_type,
                    },
                )


def reclaim_expired_claims(st, now):
    """Return processing records whose 300s lease expired back to pending.
    Mutates the passed state dict; returns how many were reclaimed."""
    count = 0
    for rec in st["queue"]:
        if rec.get("status") == "processing":
            claimed_at = rec.get("claimed_at") or 0
            if now - claimed_at > CLAIM_LEASE_SECONDS:
                rec["status"] = "pending"
                rec.pop("claimed_at", None)
                count += 1
    return count


def reclaim_claims_locked():
    """Locked wrapper used by the polling loop to recycle expired claims."""
    now = int(time.time())
    with _lock:
        st = load_state()
        count = reclaim_expired_claims(st, now)
        if count:
            save_state(st)
            log("claim lease expired; returned %d record(s) to pending" % count)


def find_record(st, ref):
    for rec in st["queue"]:
        if rec.get("id") == ref:
            return rec
    # fallback: allow addressing by telegram message_id
    for rec in st["queue"]:
        if rec.get("message_id") == ref:
            return rec
    return None


KNOWN_ENDPOINTS = (
    "pending",
    "reply",
    "ack",
    "status",
    "reply_media",
    "moderate",
    "notify",
    "analysis",
    "reply_sticker",
    "stickers",
    "archive",
)


def _normalize_text(text):
    return " ".join((text or "").lower().split())


def _duplication_ratio(texts):
    """Fraction of texts that have a near-duplicate among the others.

    Near-duplicate = normalized texts equal, or one containing the other
    (containment only counts for texts of at least 4 characters, so short
    greetings do not match everything). Empty texts are excluded.
    """
    norms = [n for n in (_normalize_text(t) for t in texts) if n]
    if len(norms) < 2:
        return 0.0
    counts = Counter(norms)
    matched = {text for text, count in counts.items() if count > 1}
    candidates = [text for text in counts if len(text) >= 4]
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if a in matched and b in matched:
                continue
            if a in b or b in a:
                matched.add(a)
                matched.add(b)
    similar = sum(counts[text] for text in matched)
    return round(similar / len(norms), 3)


def build_analysis(st, chat_id):
    """Per-sender aggregates for one chat, computed from retained history."""
    avatars = st.get("avatars") or {}
    groups = {}
    order = []
    for rec in st.get("queue", []):
        if rec.get("chat_id") != chat_id:
            continue
        sid = rec.get("sender_id")
        scid = rec.get("sender_chat_id")
        key = ("user", sid) if sid is not None else ("chat", scid)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)
    summaries = []
    for key in order:
        recs = groups[key]
        total = len(recs)
        timestamps = [r.get("timestamp") for r in recs if r.get("timestamp") is not None]
        link_count = 0
        entity_count = 0
        forward_count = 0
        for r in recs:
            summary = r.get("entities_summary") or {}
            if summary:
                entity_count += 1
                if summary.get("url") or summary.get("text_link"):
                    link_count += 1
            if r.get("is_forwarded"):
                forward_count += 1
        avatar = None
        for r in reversed(recs):
            if r.get("sender_avatar"):
                avatar = r["sender_avatar"]
                break
        sid = recs[-1].get("sender_id")
        if avatar is None and sid is not None:
            avatar = avatars.get(str(sid))
        summaries.append(
            {
                "sender_id": sid,
                "sender_name": recs[-1].get("sender_name"),
                "sender_avatar": avatar,
                "sender_chat_id": recs[-1].get("sender_chat_id"),
                "sender_chat_title": recs[-1].get("sender_chat_title"),
                "total": total,
                "first_seen": min(timestamps) if timestamps else None,
                "last_seen": max(timestamps) if timestamps else None,
                "link_ratio": round(link_count / total, 3) if total else 0.0,
                "entities_ratio": round(entity_count / total, 3) if total else 0.0,
                "forward_ratio": round(forward_count / total, 3) if total else 0.0,
                "recent_samples": [(r.get("text") or "")[:80] for r in recs[-5:]],
                "duplication": _duplication_ratio([r.get("text") or "" for r in recs]),
            }
        )
    summaries.sort(key=lambda s: s["total"], reverse=True)
    return summaries


def attach_bios(st, summaries):
    """Fill each summary's bio via getChat(sender_id), cached per sender.

    Same caching rhythm as avatars: a successful getChat response is
    definitive (bio text, or None when the user has none) and is stored in
    state["bios"]; failures are not cached and retry on the next call.
    """
    bios = dict(st.get("bios") or {})
    token = read_bot_token()
    fetched = {}
    missing = []
    for s in summaries:
        sid = s.get("sender_id")
        if sid is None:
            s["bio"] = None
            continue
        key = str(sid)
        if key in bios:
            s["bio"] = bios[key]
        else:
            s["bio"] = None
            missing.append((s, sid))
    if missing and token:
        # Fetch uncached bios concurrently with a short per-call timeout,
        # so a slow getChat cannot stall the whole analysis response.
        from concurrent.futures import ThreadPoolExecutor

        def fetch_one(sid):
            ok, result = tg_call_safe(token, "getChat", {"chat_id": sid}, timeout=10)
            if ok and isinstance(result, dict):
                return (sid, True, result.get("bio"))
            return (sid, False, None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda pair: fetch_one(pair[1]), missing))
        by_sid = {sid: (ok, bio) for sid, ok, bio in results}
        for s, sid in missing:
            ok, bio = by_sid.get(sid, (False, None))
            if ok:
                fetched[str(sid)] = bio
                s["bio"] = bio
    if fetched:
        bios.update(fetched)
        with _lock:
            st2 = load_state()
            cache = st2.get("bios") or {}
            cache.update(fetched)
            st2["bios"] = cache
            save_state(st2)
    return summaries


class Handler(BaseHTTPRequestHandler):
    server_version = "TelegramBridge/1.0"
    protocol_version = "HTTP/1.1"

    def _path_shape(self):
        """Path with every segment redacted except known endpoint names, so
        neither the real bridge token nor a guessed one ever reaches a log."""
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p != ""]
        if not parts:
            return "/"
        shape = []
        for i, part in enumerate(parts):
            if i > 0 and part in KNOWN_ENDPOINTS + ("media",):
                shape.append(part)
            else:
                shape.append("<seg>")
        return "/" + "/".join(shape)

    def log_message(self, fmt, *args):
        code = args[1] if len(args) > 1 else "-"
        sys.stdout.write(
            "[http] %s - %s %s -> %s\n"
            % (self.address_string(), self.command or "-", self._path_shape(), code)
        )
        sys.stdout.flush()

    def _reject(self, head_only=False):
        """Uniform 404 for every auth/routing failure (no reason given), with
        one journal line recording only source IP, method and path shape."""
        log(
            "request rejected: ip=%s method=%s path=%s"
            % (self.address_string(), self.command or "-", self._path_shape())
        )
        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        self._reject()

    def _route(self):
        """Return (endpoint, arg) only if the path is exactly
        /<bridge_token>/<endpoint> - or /<bridge_token>/media/<record_id> -
        with a full-string, constant-time token match. This is the single
        auth checkpoint for every endpoint."""
        token = load_config().get("bridge_token", "")
        if not token:
            return (None, None)
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p != ""]
        if len(parts) not in (2, 3):
            return (None, None)
        if not secrets.compare_digest(parts[0], token):
            return (None, None)
        if len(parts) == 3:
            if parts[1] == "media" and parts[2].isdigit():
                return ("media", int(parts[2]))
            if parts[1] == "analysis":
                try:
                    return ("analysis", int(parts[2]))
                except ValueError:
                    return (None, None)
            if parts[1] == "archive":
                try:
                    return ("archive", int(parts[2]))
                except ValueError:
                    return (None, None)
            return (None, None)
        if parts[1] not in KNOWN_ENDPOINTS:
            return (None, None)
        return (parts[1], None)

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _serve_media(self, record_id):
        with _lock:
            st = load_state()
            rec = find_record(st, record_id)
        if rec is None or not rec.get("media_file"):
            self._reject()
            return
        name = os.path.basename(rec["media_file"])
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(path):
            self._reject()
            return
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        endpoint, arg = self._route()
        if endpoint == "media":
            self._serve_media(arg)
            return
        if endpoint == "status":
            cfg = load_config()
            bindings = get_bindings(cfg)
            first = bindings[0] if bindings else {}
            status = {
                "bound": len(bindings) > 0,
                "group_chat_id": first.get("group_chat_id", cfg.get("group_chat_id")),
                "owner_channel_id": first.get("owner_channel_id", cfg.get("owner_channel_id")),
                "owner_channel_title": first.get(
                    "owner_channel_title", cfg.get("owner_channel_title")
                ),
                "bindings": bindings,
                "authorized_ids": cfg.get("authorized_ids") or [],
                "telegram_active": bool(_tg_active.get("active")),
            }
            self._send_json(200, status)
            return
        if endpoint == "analysis":
            with _lock:
                st = load_state()
            summaries = build_analysis(st, arg) if arg is not None else []
            summaries = attach_bios(st, summaries)
            self._send_json(200, summaries)
            return
        if endpoint == "stickers":
            with _lock:
                st = load_state()
            lib = sorted(
                st.get("stickers") or [],
                key=lambda e: e.get("seen_count", 0),
                reverse=True,
            )
            self._send_json(200, lib)
            return
        if endpoint == "archive":
            if arg is None:
                self._send_json(200, [])
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tail = 200
            raw_tail = (query.get("tail") or [None])[0]
            if raw_tail is not None:
                try:
                    tail = int(raw_tail)
                except (TypeError, ValueError):
                    tail = 200
            tail = max(1, min(tail, 5000))
            self._send_json(200, archive_read_tail(arg, tail))
            return
        if endpoint != "pending":
            self._send_404()
            return
        # Fetch-and-claim: atomically mark everything returned as
        # processing (900s lease) so no later /pending hands it out twice.
        # Optional ?chat_id=<id> restricts the claim to one group (used by
        # per-group workers); absent = all groups, as before. A present
        # but unparseable chat_id claims nothing (fail-closed).
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        chat_filter = None
        filter_active = False
        raw_chat = (query.get("chat_id") or [None])[0]
        if raw_chat is not None:
            filter_active = True
            try:
                chat_filter = int(raw_chat)
            except (TypeError, ValueError):
                chat_filter = None
        idle_only = query.get("idle_only", ["0"])[0]
        if idle_only not in ("0", "1") or (idle_only == "1" and chat_filter is None):
            self._send_json(400, {"ok": False, "error": "idle_only requires a valid chat_id"})
            return
        limit = None
        if "limit" in query:
            try:
                limit = int(query["limit"][0])
                if not 1 <= limit <= 1000:
                    raise ValueError
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "limit must be between 1 and 1000"})
                return
        now = int(time.time())
        with _lock:
            st = load_state()
            reclaimed = reclaim_expired_claims(st, now)
            claimed = []
            busy = idle_only == "1" and any(
                rec.get("chat_id") == chat_filter and rec.get("status") == "processing"
                for rec in st["queue"]
            )
            if not busy:
                for rec in st["queue"]:
                    if rec.get("status") != "pending":
                        continue
                    if filter_active and rec.get("chat_id") != chat_filter:
                        continue
                    rec["status"] = "processing"
                    rec["claimed_at"] = now
                    claimed.append(rec)
                    if limit is not None and len(claimed) >= limit:
                        break
            if claimed or reclaimed:
                save_state(st)
        self._send_json(200, claimed)

    def _handle_reply_media(self):
        ctype = self.headers.get("Content-Type") or ""
        if not ctype.startswith("multipart/form-data"):
            self._send_json(400, {"ok": False, "error": "multipart/form-data required"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json(413, {"ok": False, "error": "file too large"})
            return
        raw = self.rfile.read(length)
        try:
            fields, files = parse_multipart(raw, ctype)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": "bad multipart: %s" % exc})
            return
        upload = files.get("file")
        ref_raw = fields.get("in_reply_to")
        if upload is None or ref_raw is None:
            self._send_json(400, {"ok": False, "error": "in_reply_to and file are required"})
            return
        kind = media_kind(upload["filename"], upload["content_type"])
        if kind is None:
            self._send_json(400, {"ok": False, "error": "unsupported media type"})
            return
        method, field_name = kind
        try:
            ref = int(ref_raw)
        except (TypeError, ValueError):
            ref = ref_raw
        with _lock:
            st = load_state()
            rec = find_record(st, ref)
        if rec is None:
            self._send_json(404, {"ok": False, "error": "message not found"})
            return
        token = read_bot_token()
        if not token:
            self._send_json(503, {"ok": False, "error": "bot token not configured yet"})
            return
        fp = send_fingerprint("media", rec["chat_id"], hashlib.sha256(upload["data"]).digest())
        with _lock:
            st = load_state()
            reserved = gate_reserve(st, fp, rec["chat_id"], rec["id"])
            save_state(st)
        if not reserved:
            log("duplicate send blocked: record=%s chat=%s kind=media" % (rec["id"], rec["chat_id"]))
            self._send_json(409, {"ok": False, "error": "duplicate send blocked"})
            return
        try:
            res = tg_send_media(
                token,
                method,
                field_name,
                rec["chat_id"],
                rec["message_id"],
                upload["filename"] or ("media." + field_name),
                upload["content_type"],
                upload["data"],
            )
        except urllib.error.HTTPError as exc:
            # Telegram answered and refused: definitively not sent.
            desc = http_error_description(exc)
            with _lock:
                st = load_state()
                gate_release(st, fp)
                save_state(st)
            log("reply_media failed: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], desc))
            self._send_json(502, {"ok": False, "error": desc})
            return
        except Exception as exc:
            # Response lost: the media may already be in the group.
            # Keep the fingerprint; do not retry here.
            desc = http_error_description(exc)
            log("reply_media outcome unknown: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], desc))
            self._send_json(502, {"ok": False, "error": desc, "note": "send outcome unknown; identical resend blocked for %ds" % SEND_GATE_WINDOW})
            return
        if not res.get("ok"):
            with _lock:
                st = load_state()
                gate_release(st, fp)
                save_state(st)
            self._send_json(502, {"ok": False, "error": "telegram send failed", "telegram": res})
            return
        bot_id = bot_name = None
        with _lock:
            st = load_state()
            bot_id, bot_name = st.get("bot_id"), st.get("bot_username")
            rec2 = find_record(st, rec["id"])
            if rec2 is not None:
                rec2["status"] = "replied"
                rec2["replied_at"] = int(time.time())
                rec2["reply_kind"] = field_name
                save_state(st)
        archive_append(
            rec["chat_id"],
            {
                "ts": int(time.time()),
                "direction": "out",
                "chat_id": rec["chat_id"],
                "record_id": rec["id"],
                "message_id": (res.get("result") or {}).get("message_id") if isinstance(res, dict) else None,
                "sender_id": bot_id,
                "sender_name": bot_name,
                "kind": field_name,
                "text": None,
                "media": upload["filename"],
                "media_type": field_name,
            },
        )
        self._send_json(200, {"ok": True, "id": rec["id"], "telegram": res.get("result")})

    def _handle_moderate(self, body):
        ref = body.get("id")
        mute_hours = body.get("mute_hours")
        if ref is None or mute_hours is None:
            self._send_json(400, {"ok": False, "error": "id and mute_hours are required"})
            return
        try:
            mute_hours = float(mute_hours)
            if mute_hours < 0:
                raise ValueError
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "mute_hours must be a non-negative number"})
            return
        with _lock:
            st = load_state()
            rec = find_record(st, ref)
        if rec is None:
            self._send_json(404, {"ok": False, "error": "message not found"})
            return
        if rec.get("status") == "moderated":
            self._send_json(200, {"ok": True, "deleted": True, "muted": True, "already_moderated": True})
            return
        # Hard guardrails: never moderate authorized users or any bound
        # owner channel, no matter who asks.
        cfg = load_config()
        authorized = cfg.get("authorized_ids") or []
        owner_channels = {b.get("owner_channel_id") for b in get_bindings(cfg)}
        if rec.get("sender_id") is not None and rec.get("sender_id") in authorized:
            log("moderate rejected (guardrail): record=%s reason=authorized sender" % rec["id"])
            self._send_json(403, {"ok": False, "error": "forbidden: sender is an authorized user"})
            return
        if rec.get("sender_chat_id") is not None and rec.get("sender_chat_id") in owner_channels:
            log("moderate rejected (guardrail): record=%s reason=owner channel" % rec["id"])
            self._send_json(403, {"ok": False, "error": "forbidden: sender_chat is a bound owner channel"})
            return
        if rec.get("sender_id") is None:
            log("moderate rejected: record=%s reason=no sender_id to mute" % rec["id"])
            self._send_json(400, {"ok": False, "error": "record has no sender_id to mute"})
            return
        token = read_bot_token()
        if not token:
            self._send_json(503, {"ok": False, "error": "bot token not configured yet"})
            return
        ok, result = tg_call_safe(
            token,
            "deleteMessage",
            {"chat_id": rec["chat_id"], "message_id": rec["message_id"]},
        )
        already_gone = False
        if not ok:
            if isinstance(result, str) and "message to delete not found" in result:
                # The message is already gone (deleted by someone else or
                # the sender): count it as deleted and still apply the mute.
                already_gone = True
                log(
                    "moderate: record=%s chat=%s message already gone; continuing to mute"
                    % (rec["id"], rec["chat_id"])
                )
            else:
                log(
                    "moderate failed: record=%s chat=%s action=delete error=%s"
                    % (rec["id"], rec["chat_id"], result)
                )
                self._send_json(200, {"ok": False, "deleted": False, "muted": False, "error": result})
                return
        until_date = 0
        if mute_hours > 0:
            until_date = int(time.time()) + int(mute_hours * 3600)
        permissions = {
            "can_send_messages": False,
            "can_send_audios": False,
            "can_send_documents": False,
            "can_send_photos": False,
            "can_send_videos": False,
            "can_send_video_notes": False,
            "can_send_voice_notes": False,
            "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": False,
            "can_manage_topics": False,
        }
        ok, result = tg_call_safe(
            token,
            "restrictChatMember",
            {
                "chat_id": rec["chat_id"],
                "user_id": rec["sender_id"],
                "permissions": permissions,
                "until_date": until_date,
            },
        )
        if not ok:
            log(
                "moderate partial: record=%s chat=%s action=delete ok, action=mute error=%s"
                % (rec["id"], rec["chat_id"], result)
            )
            partial = {"ok": False, "deleted": True, "muted": False, "error": result}
            if already_gone:
                partial["note"] = "already_gone"
            self._send_json(200, partial)
            return
        with _lock:
            st = load_state()
            rec2 = find_record(st, rec["id"])
            if rec2 is not None:
                rec2["status"] = "moderated"
                rec2["moderated_at"] = int(time.time())
                save_state(st)
        log(
            "moderate ok: record=%s chat=%s sender=%s mute_hours=%s%s"
            % (
                rec["id"],
                rec["chat_id"],
                rec["sender_id"],
                mute_hours,
                " note=already_gone" if already_gone else "",
            )
        )
        success = {"ok": True, "deleted": True, "muted": True}
        if already_gone:
            success["note"] = "already_gone"
        self._send_json(200, success)

    def _handle_reply_sticker(self, body):
        ref = body.get("in_reply_to")
        file_id = body.get("file_id")
        if ref is None or not isinstance(file_id, str) or not file_id:
            self._send_json(400, {"ok": False, "error": "in_reply_to and file_id are required"})
            return
        with _lock:
            st = load_state()
            rec = find_record(st, ref)
        if rec is None:
            self._send_json(404, {"ok": False, "error": "message not found"})
            return
        token = read_bot_token()
        if not token:
            self._send_json(503, {"ok": False, "error": "bot token not configured yet"})
            return
        fp = send_fingerprint("stk", rec["chat_id"], file_id)
        with _lock:
            st = load_state()
            reserved = gate_reserve(st, fp, rec["chat_id"], rec["id"])
            save_state(st)
        if not reserved:
            log("duplicate send blocked: record=%s chat=%s kind=sticker" % (rec["id"], rec["chat_id"]))
            self._send_json(409, {"ok": False, "error": "duplicate send blocked"})
            return
        status, result = tg_call_outcome(
            token,
            "sendSticker",
            {
                "chat_id": rec["chat_id"],
                "sticker": file_id,
                "reply_to_message_id": rec["message_id"],
            },
        )
        if status == "rejected":
            with _lock:
                st = load_state()
                gate_release(st, fp)
                save_state(st)
            log("reply_sticker failed: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], result))
            self._send_json(502, {"ok": False, "error": result})
            return
        if status == "unknown":
            log("reply_sticker outcome unknown: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], result))
            self._send_json(502, {"ok": False, "error": result, "note": "send outcome unknown; identical resend blocked for %ds" % SEND_GATE_WINDOW})
            return
        bot_id = bot_name = None
        with _lock:
            st = load_state()
            bot_id, bot_name = st.get("bot_id"), st.get("bot_username")
            rec2 = find_record(st, rec["id"])
            if rec2 is not None:
                rec2["status"] = "replied"
                rec2["replied_at"] = int(time.time())
                rec2["reply_kind"] = "sticker"
                save_state(st)
        archive_append(
            rec["chat_id"],
            {
                "ts": int(time.time()),
                "direction": "out",
                "chat_id": rec["chat_id"],
                "record_id": rec["id"],
                "message_id": result.get("message_id") if isinstance(result, dict) else None,
                "sender_id": bot_id,
                "sender_name": bot_name,
                "kind": "sticker",
                "text": None,
                "media": file_id,
                "media_type": "sticker",
            },
        )
        self._send_json(200, {"ok": True, "id": rec["id"], "telegram": result})

    def _handle_notify(self, body):
        text = body.get("text")
        if not isinstance(text, str) or not text:
            self._send_json(400, {"ok": False, "error": "text is required"})
            return
        cfg = load_config()
        dm_chat_id = cfg.get("dm_chat_id")
        if dm_chat_id is None:
            self._send_json(400, {"ok": False, "error": "dm chat is not bound yet"})
            return
        token = read_bot_token()
        if not token:
            self._send_json(503, {"ok": False, "error": "bot token not configured yet"})
            return
        ok, result = tg_call_safe(
            token, "sendMessage", {"chat_id": dm_chat_id, "text": text}
        )
        if not ok:
            log("notify failed: chat=%s error=%s" % (dm_chat_id, result))
            self._send_json(200, {"ok": False, "error": result})
            return
        log("notify sent: chat=%s" % (dm_chat_id,))
        message_id = result.get("message_id") if isinstance(result, dict) else None
        self._send_json(200, {"ok": True, "message_id": message_id})

    def do_POST(self):
        endpoint, _arg = self._route()
        if endpoint == "reply_media":
            self._handle_reply_media()
            return
        if endpoint not in ("reply", "ack", "moderate", "notify", "reply_sticker"):
            self._send_404()
            return
        try:
            body = self._read_body_json()
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        if endpoint == "moderate":
            self._handle_moderate(body)
            return
        if endpoint == "notify":
            self._handle_notify(body)
            return
        if endpoint == "reply_sticker":
            self._handle_reply_sticker(body)
            return
        if endpoint == "ack":
            ref = body.get("id")
            with _lock:
                st = load_state()
                rec = find_record(st, ref)
                if rec is None:
                    self._send_json(404, {"ok": False, "error": "message not found"})
                    return
                rec["status"] = "acked"
                rec["acked_at"] = int(time.time())
                save_state(st)
            self._send_json(200, {"ok": True, "id": rec["id"]})
            return
        # endpoint == "reply"
        ref = body.get("in_reply_to")
        text = body.get("text")
        if ref is None or not text:
            self._send_json(400, {"ok": False, "error": "in_reply_to and text are required"})
            return
        with _lock:
            st = load_state()
            rec = find_record(st, ref)
        if rec is None:
            self._send_json(404, {"ok": False, "error": "message not found"})
            return
        token = read_bot_token()
        if not token:
            self._send_json(503, {"ok": False, "error": "bot token not configured yet"})
            return
        fp = send_fingerprint("msg", rec["chat_id"], text)
        with _lock:
            st = load_state()
            reserved = gate_reserve(st, fp, rec["chat_id"], rec["id"])
            save_state(st)
        if not reserved:
            log("duplicate send blocked: record=%s chat=%s kind=msg" % (rec["id"], rec["chat_id"]))
            self._send_json(409, {"ok": False, "error": "duplicate send blocked"})
            return
        status, payload = tg_call_outcome(
            token,
            "sendMessage",
            {
                "chat_id": rec["chat_id"],
                "text": text,
                "reply_to_message_id": rec["message_id"],
            },
            timeout=30,
        )
        if status == "rejected":
            with _lock:
                st = load_state()
                gate_release(st, fp)
                save_state(st)
            log("reply failed: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], payload))
            self._send_json(502, {"ok": False, "error": payload})
            return
        if status == "unknown":
            # Response lost: the message may already be in the group.
            # Keep the fingerprint so an identical resend stays blocked;
            # do NOT mark the record replied and do NOT retry here.
            log("reply outcome unknown: record=%s chat=%s error=%s" % (rec["id"], rec["chat_id"], payload))
            self._send_json(502, {"ok": False, "error": payload, "note": "send outcome unknown; identical resend blocked for %ds" % SEND_GATE_WINDOW})
            return
        res = {"result": payload}
        bot_id = bot_name = None
        with _lock:
            st = load_state()
            bot_id, bot_name = st.get("bot_id"), st.get("bot_username")
            rec2 = find_record(st, rec["id"])
            if rec2 is not None:
                rec2["status"] = "replied"
                rec2["replied_at"] = int(time.time())
                rec2["reply_text"] = text
                save_state(st)
        archive_append(
            rec["chat_id"],
            {
                "ts": int(time.time()),
                "direction": "out",
                "chat_id": rec["chat_id"],
                "record_id": rec["id"],
                "message_id": payload.get("message_id") if isinstance(payload, dict) else None,
                "sender_id": bot_id,
                "sender_name": bot_name,
                "kind": "text",
                "text": text,
                "media": None,
                "media_type": None,
            },
        )
        self._send_json(200, {"ok": True, "id": rec["id"], "telegram": res.get("result")})


    # Every other HTTP method gets the same uniform 404 - no 501 exception
    # path that would reveal the server surface.
    def do_PUT(self):
        self._reject()

    def do_DELETE(self):
        self._reject()

    def do_PATCH(self):
        self._reject()

    def do_OPTIONS(self):
        self._reject()

    def do_HEAD(self):
        self._reject(head_only=True)


def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    if not os.path.exists(STATE_PATH):
        save_state(load_state())
    cfg = load_config()
    if not cfg.get("bridge_token"):
        log("config.json missing bridge_token; cannot start API")
        return 1
    port = int(cfg.get("api_port", 29301))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.bridge_config = cfg
    server.daemon_threads = True
    t = threading.Thread(target=poll_loop, daemon=True, name="tg-poll")
    t.start()
    log("HTTP API listening on 0.0.0.0:%d" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
