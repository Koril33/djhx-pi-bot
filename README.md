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

## 部署（Gitea 驱动）

```
本地 push ──► Gitea (git@git.djhx.site:koril/djhx-pi-bot.git)
                  │ Push Webhook
                  ▼
        https://pi.djhx.site/deploy-webhook/<DEPLOY_SECRET>
                  │ 后台执行
                  ▼
        deploy/deploy.sh: git pull → uv sync → systemctl restart
```

- 单一事实源：Gitea；服务器通过 webhook 自动部署
- `deploy/deploy.sh` 与 `deploy/djhx-pi-bot.service` 随仓库版本化
- Gitea 仓库 → 设置 → Web 钩子 → 添加：URL 填 `https://pi.djhx.site/deploy-webhook/<secret>`（Gitea 类型，Push 事件，Secret 填 DEPLOY_SECRET）
- 部署日志：`logs/deploy.log`；服务日志：`journalctl -u djhx-pi-bot -f`
- 回滚：`git push gitea <上一commit>:main`
