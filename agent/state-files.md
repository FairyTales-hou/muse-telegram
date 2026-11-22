# Agent state files (schemas)

The agent's durable memory is plain files, so chat windows can be rotated
(fresh context) without losing continuity. Real files contain live member
data and are **not** included in this repo; formats below use fake data.

## Shared (all groups)

- `lulu_canon.md` — persona canon: every self-fact the agent has stated
  publicly (name, origin, age, appearance, likes). Checked before any
  self-answer; new self-facts are appended in the same wake they are said;
  never contradicted.
- `daily_reports.md` — append-only log for routine reports that must not
  interrupt the owner (dated one-liners).
- `sticker_rules_pending.md` — which library stickers the agent has
  personally viewed (only viewed, viewable stickers may be sent).

## Per group: `groups_state/<chat_id>/`

- `sender_classification.md` — one section per group:

  ```
  ## <chat_id>
  chat_id | sender_id | sender_name | category | note
  GROUP_ID_TEST | 123456789 | Some Name | 熟面孔 | regular since 2026-11-20
  GROUP_ID_TEST | 987654321 | Ad Pattern Andy | 廣告模式 | flyer bio, recruitment posts
  ```

  Categories: `熟面孔` (familiar) / `陌生` (stranger — watch, not presumed
  malicious) / `廣告模式` (ad pattern). Moderation judgments rest on this
  file plus live evidence.

- `recent_matters.md` — substantive replies already given, with timestamps.
  Same sender + same matter within 30 min → ack only, never re-answer.
- `processed_ids.txt` — one bridge record id per line; the dedup list.
  Every handled record id is appended; listed ids are never replied twice.

## Per group: `groups/<chat_id>/knowledge.md`

Owner-maintained facts for that group only (service lines, prices, rules).
The agent answers group-affair questions from this file instead of general
knowledge, and never invents numbers that are not in it.
