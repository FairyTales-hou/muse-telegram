# 露露（Lulu）— 給 LLM 智能體用的 Telegram 群組橋接

[English](README.md) | 繁體中文

一個住在 Telegram 群組裡的 LLM 智能體：以角色身分聊天、應要求畫圖與
生成媒體、查詢公開資訊、審核廣告垃圾訊息 — 背後是一座只用 Python
標準函式庫寫成的橋接服務，具備 token 守衛的 HTTP API、領取式佇列
（fetch-and-claim）語義，以及「每群一個智能體窗口、記憶存放在
Markdown 狀態檔」的設計。

**先看 [ARCHITECTURE.md](ARCHITECTURE.md)** — 元件、資料流、API 介面、
安全模型，以及目前優化目標：實測過的延遲瓶頸。

## 功能

- **零依賴橋接** — 單一 Python 檔案、只用標準函式庫，不用 pip 安裝
  任何東西；以專用系統使用者身分在 systemd 下運行。
- **領取式佇列** — 紀錄以 900 秒租約原子領取；每群有 claim gate
  （`idle_only=1&limit=N`）：該群還有未完成的工作時，拒絕再發新批次。
- **每群一個智能體窗口** — 每群一個專屬聊天窗口、每日輪替；長期記憶
  是 Markdown 狀態檔而不是聊天上下文，新窗口讀檔即完整接手。
- **回覆對象感知** — 每筆紀錄帶 `reply_to`（這則訊息在回覆誰），智能體
  能分辨「在跟我說話」和「在跟我旁邊的人說話」。
- **只禁言的審核** — 刪文＋禁言是唯一處置；`bridge.py` 裡刻意**不存在
  封鎖／踢人（ban/kick）的程式路徑**，審核端點對主人身分硬性拒絕。
- **重複發送閘門** — 外發訊息以 sha256 指紋記錄；120 秒內完全相同的
  發送會在呼叫 Telegram 之前就被拒絕。
- **先看過才能發的貼紙庫** — 智能體只能發它親眼看過本體並記下內容的
  貼紙；無法檢視的格式永遠不發。
- **天生抗萃取** — 授權只認數字 ID、每則訊息重新驗證；面向群組的
  上下文裡沒有任何機密，索取憑證一律不確認也不否認。

## 倉庫結構

```
server/            bridge.py（單一檔案、標準函式庫）、systemd unit、
                   config.example.json、存檔回填工具、
                   test_pending.py（佇列／claim gate 回歸測試）
hooks/scripts/     bash 輪詢器：GET /pending?chat_id=… → wake/silent
hooks/prompts/     每群一份的智能體規則書，每次喚醒時注入
                   （combined-legacy.md 是分群前的舊設計，留作參考）
agent/             人設 canon、智能體簡報、貼紙規則、
                   每日聊天室輪替 cron 規格
```

## 快速上手

1. 用 BotFather 建立機器人，把 token 放進 `bridge.py` 旁邊的
   `bot_token.txt`（權限 600）。
2. 把 `server/config.example.json` 複製成 `config.json`，將佔位符
   （見下表）換成你自己的值。
3. 執行 `python3 bridge.py`，或安裝 systemd unit
   （`server/telegram-bridge.service`）。橋接啟動後立即開始輪詢。
4. 綁定群組：在設定流程產生一次性綁定碼，用你的主人頻道在群組裡
   發出該碼（見 ARCHITECTURE.md 的「API surface」）。私訊（DM）綁定
   走同一套流程。
5. 把你的智能體運行環境指向 `hooks/scripts/` 裡的輪詢器（設定 API
   base 與群組 id），並給智能體 `hooks/prompts/` 裡對應的規則書，
   以及 `agent/state-files.md` 描述的狀態檔案。

### 快照中的佔位符

| 佔位符 | 代表 |
|---|---|
| `__BRIDGE_TOKEN__` | 橋接 API token（任意 256-bit 隨機字串） |
| `YOUR_SERVER_IP` | 橋接 API 所在主機 |
| `OWNER_USER_ID` | 主人的 Telegram 數字用戶 ID |
| `OWNER_CHANNEL_ID` | 主人頻道的 chat id |
| `CHANNEL_BOT_SHARED_ID` | Telegram 官方的匿名頻道發文共用身分 |
| `GROUP_ID_*` | 已綁定群組的 chat id |
| `MEMBER_ID_*` / `AUTHORIZED_ADVERTISER_ID` | 規則書裡提到的個別成員 ID |

## 測試

```sh
cd server && python3 -m unittest test_pending -v
```

涵蓋領取式佇列的持久化、idle claim gate、並發領取，以及近重複偵測的
回歸測試。測試可直接對快照運行（佔位常數由測試自行 patch）。

## 安全須知

- 絕不要提交 `config.json`、`bot_token.txt`、`state.json` — 內附的
  `.gitignore` 已排除它們，以及存檔、媒體與智能體的即時狀態。
- 橋接 API 以路徑中的 token 守衛，其他路徑一律回 404；請把橋接
  token 當密碼看待。
- 本倉庫是生產系統的**去敏快照**：所有 token、chat／用戶 ID、成員
  識別與主機 IP 都已換成佔位符。它無法對原始部署運行，規則書裡的
  具名範例也同樣是佔位符。

## 貢獻

歡迎 issue 與 pull request。ARCHITECTURE.md 的「Known bottlenecks」
一節就是實質的路線圖 — 智能體回合層的延遲是最值得解的問題。

## 授權

MIT — 見 [LICENSE](LICENSE)。
