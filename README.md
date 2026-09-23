# djhx-pi-bot

通过 Telegram Bot 与多个环境（Windows / Debian / 公网服务器）中的 [pi](https://pi.dev) 编码代理交互的桥接服务。

## 架构（V1）

```
Telegram ←→ nginx(TLS) ←→ FastAPI webhook ←→ python-telegram-bot
                                              └→ 常驻 pi --mode rpc 子进程 (JSONL over stdio)
                                              └→ SQLite (用户/会话映射、Telegram 更新队列)
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

## 故障排查

- `curl http://127.0.0.1:8128/healthz` 查看更新队列的待处理、处理中和失败数量。`degraded: true` 表示有更新需要检查。
- `journalctl -u djhx-pi-bot --since '1 hour ago' --no-pager` 查看 bot、pi RPC 和 Telegram 发送错误；`logs/deploy.log` 记录部署过程。
- 已开始处理的 Telegram 更新若因服务重启或异常而中断，会记录为失败，不会自动重放可能已执行工具的请求。确认结果后由用户重新发送。
- 推送到 Gitea `main` 后，查看部署日志中的目标提交和 `systemctl restart`，再核对 `/healthz`。服务器上直接修改代码时，先提交并推送，保持部署工作区干净。
