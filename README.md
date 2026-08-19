# djhx-pi-bot

通过 Telegram Bot 与多个环境（Windows / Debian / 公网服务器）中的 [pi](https://pi.dev) 编码代理交互的桥接服务。

## 架构（V1）

```
Telegram ←→ nginx(TLS) ←→ FastAPI webhook ←→ python-telegram-bot
                                              └→ 常驻 pi --mode rpc 子进程 (JSONL over stdio)
                                              └→ SQLite (用户/会话映射)
```

- V1 单环境（服务器本机）：bot 进程直接管理常驻 pi RPC 子进程
- 常驻 RPC 消除每次对话的冷启动成本，支持流式输出
- 多环境（Windows/Debian worker）规划在后续版本引入

## 快速开始

```bash
uv sync
cp .env.example .env   # 填入 BOT_TOKEN / ALLOWED_USER_IDS / WEBHOOK_SECRET
uv run uvicorn app:app --host 127.0.0.1 --port 8128
```

设置 webhook：

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<域名>/webhook/<WEBHOOK_SECRET>"
```

## 命令

| 命令 | 说明 |
|---|---|
| `/help` | 帮助 |
| `/env list` / `/env use <name>` | 环境（V1 仅 server） |
| `/session new [name]` / `list` / `use <id>` | 会话管理 |
| `/model list` / `/model use <id>` | 模型切换 |
| `/stop` | 中止当前任务 |
| `/status` | 运行状态 |

## 部署

见 `deploy/` 下的 systemd 与 nginx 配置（部署脚本 + 服务器侧 post-receive hook）。
