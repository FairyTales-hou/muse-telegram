# 露露（Lulu）— 给 LLM 智能体用的 Telegram 群组桥接

[繁體中文](README.zh-TW.md) | [English](README.md) | 简体中文

一个住在 Telegram 群组里的 LLM 智能体：以角色身份聊天、应要求画图与
生成媒体、查询公开信息、审核广告垃圾信息 — 背后是一座只用 Python
标准库写成的桥接服务，具备 token 守卫的 HTTP API、领取式队列
（fetch-and-claim）语义，以及「每群一个智能体窗口、记忆存放在
Markdown 状态文件」的设计。

**先看 [ARCHITECTURE.md](ARCHITECTURE.md)** — 组件、数据流、API 接口、
安全模型，以及当前优化目标：实测过的延迟瓶颈。

## 功能

- **零依赖桥接** — 单个 Python 文件、只用标准库，不用 pip 安装
  任何东西；以专用系统用户身份在 systemd 下运行。
- **领取式队列** — 记录以 900 秒租约原子领取；每群有 claim gate
  （`idle_only=1&limit=N`）：该群还有未完成的工作时，拒绝再发新批次。
- **每群一个智能体窗口** — 每群一个专属聊天窗口、每日轮换；长期记忆
  是 Markdown 状态文件而不是聊天上下文，新窗口读文件即完整接手。
- **回复对象感知** — 每条记录带 `reply_to`（这条消息在回复谁），智能体
  能分辨「在跟我说」和「在跟我旁边的人说」。
- **只禁言的审核** — 删帖＋禁言是唯一处置；`bridge.py` 里刻意**不存在
  封锁／踢人（ban/kick）的代码路径**，审核端点对主人身份硬性拒绝。
- **重复发送闸门** — 外发消息以 sha256 指纹记录；120 秒内完全相同的
  发送会在调用 Telegram 之前就被拒绝。
- **先看过才能发的贴纸库** — 智能体只能发它亲眼看过本体并记下内容的
  贴纸；无法查看的格式永远不发。
- **天生抗萃取** — 授权只认数字 ID、每条消息重新验证；面向群组的
  上下文里没有任何机密，索取凭据一律不确认也不否认。

## 仓库结构

```
server/            bridge.py（单个文件、标准库）、systemd unit、
                   config.example.json、存档回填工具、
                   test_pending.py（队列／claim gate 回归测试）
hooks/scripts/     bash 轮询器：GET /pending?chat_id=… → wake/silent
hooks/prompts/     每群一份的智能体规则书，每次唤醒时注入
                   （combined-legacy.md 是分群前的旧设计，留作参考）
agent/             人设 canon、智能体简报、贴纸规则、
                   每日聊天室轮换 cron 规格
```

## 快速上手

1. 用 BotFather 创建机器人，把 token 放进 `bridge.py` 旁边的
   `bot_token.txt`（权限 600）。
2. 把 `server/config.example.json` 复制成 `config.json`，将占位符
   （见下表）换成你自己的值。
3. 运行 `python3 bridge.py`，或安装 systemd unit
   （`server/telegram-bridge.service`）。桥接启动后立即开始轮询。
4. 绑定群组：在配置流程生成一次性绑定码，用你的主人频道在群组里
   发出该码（见 ARCHITECTURE.md 的「API surface」）。私信（DM）绑定
   走同一套流程。
5. 把你的智能体运行环境指向 `hooks/scripts/` 里的轮询器（设置 API
   base 与群组 id），并给智能体 `hooks/prompts/` 里对应的规则书，
   以及 `agent/state-files.md` 描述的状态文件。

### 快照中的占位符

| 占位符 | 代表 |
|---|---|
| `__BRIDGE_TOKEN__` | 桥接 API token（任意 256-bit 随机字符串） |
| `YOUR_SERVER_IP` | 桥接 API 所在主机 |
| `OWNER_USER_ID` | 主人的 Telegram 数字用户 ID |
| `OWNER_CHANNEL_ID` | 主人频道的 chat id |
| `CHANNEL_BOT_SHARED_ID` | Telegram 官方的匿名频道发帖共用身份 |
| `GROUP_ID_*` | 已绑定群组的 chat id |
| `MEMBER_ID_*` / `AUTHORIZED_ADVERTISER_ID` | 规则书里提到的个别成员 ID |

## 测试

```sh
cd server && python3 -m unittest test_pending -v
```

涵盖领取式队列的持久化、idle claim gate、并发领取，以及近重复检测的
回归测试。测试可直接对快照运行（占位常数由测试自行 patch）。

## 安全须知

- 绝不要提交 `config.json`、`bot_token.txt`、`state.json` — 内附的
  `.gitignore` 已排除它们，以及存档、媒体与智能体的实时状态。
- 桥接 API 以路径中的 token 守卫，其他路径一律返回 404；请把桥接
  token 当密码看待。
- 本仓库是生产系统的**去敏快照**：所有 token、chat／用户 ID、成员
  标识与主机 IP 都已换成占位符。它无法对原始部署运行，规则书里的
  具名示例也同样是占位符。

## 贡献

欢迎 issue 与 pull request。ARCHITECTURE.md 的「Known bottlenecks」
一节就是实质的路线图 — 智能体回合层的延迟是最值得解的问题。

## 授权

MIT — 见 [LICENSE](LICENSE)。
