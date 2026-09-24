# djhx-pi-bot

通过 Telegram Bot 与多个环境（Windows / Debian / 公网服务器）中的 [pi](https://pi.dev) 编码代理交互的桥接服务。

## 架构（V1）

```
Telegram ←→ nginx(TLS) ←→ FastAPI webhook ←→ python-telegram-bot
                                              └→ 常驻 pi --mode rpc 子进程 (JSONL over stdio)
                                              └→ SQLite (会话映射、更新队列、用量账本、长期记忆与摘要)
```

- V1 单环境（服务器本机）：bot 进程直接管理常驻 pi RPC 子进程
- 常驻 RPC 消除每次对话的冷启动成本，支持流式输出
- 可发送单张 Telegram 照片或图片文件，可附文字说明；支持 JPEG、PNG、GIF、WebP，单张最大 10 MiB、宽高各不超过 8192 像素。使用 `/model list` 查看带 🖼 标记的图片模型。Telegram 相册请拆成单张发送。
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
| `/memory list [页码]` / `add` / `edit` / `delete` / `pin` / `unpin` | 长期记忆管理 |
| `/summary show` / `/summary clear` | 当前会话摘要 |
| `/cost` / `/cost today` | 最近一轮 / 今日模型 token 与估算费用 |

## 长期记忆与会话摘要

- 自动记忆默认启用：每个会话积累指定数量的完整回合后，用独立、无工具的 DeepSeek Flash 调用更新该会话摘要，并提取跨会话稳定偏好。项目细节只留在会话摘要。维护调用单独计入 `/cost today`。
- `/memory add <内容>` 可以立即手动保存。`/memory list` 显示记忆 ID，`/memory edit <id> <内容>`、`/memory delete <id>` 可以修改或删除。每条记忆和有效记忆数量均有可配置上限。
- pi 扩展在模型请求前临时注入受预算限制的 SQLite 背景资料，不把同一份记忆逐轮写入 JSONL。删除后不再主动注入，但已存在于旧 JSONL、旧回复或摘要的内容不会自动消失。
- 自动摘要固定使用 DeepSeek Flash，不受 `/model use` 影响。若服务意外中断，未完成回合会在启动时标为不完整；下一条普通消息会提醒用户，不会自动重放可能已经执行工具的请求。
- 会话 JSONL 没有迁移时，bot 会为原会话 ID 新建 pi 会话，并从 SQLite 摘要和近期文字回合恢复主要背景。无法恢复旧图片、工具输出或逐字历史；没有摘要时会明确提示为空白历史。
- `MEMORY_*` 环境变量控制自动提取周期、原始回合保留数量、单条记忆长度和注入字节预算；维护模型不提供可覆盖项，见 [.env.example](.env.example)。完整设计及限制见 [产品设计文档](docs/memory-product.md)。
- 单管理员后台位于 `/admin`，可管理记忆、会话摘要和记忆预算。账号密码由 `.env` 的 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 配置；后台保存的参数持久化到同一个 SQLite，并优先于相应的环境变量。见 [后台产品设计](docs/admin-product.md) 与 [开发运维文档](docs/admin-development.md)。
- 使用 `uv run python -m scripts.backup_db backup.db` 可生成一致的单文件 SQLite 备份。迁移记忆时拷贝这个数据库；新服务器仍需另行配置 bot 和模型凭证。

## 用量和费用

- 每次 pi 模型回复及会话压缩结束后，保存 pi 报告的输入未命中、缓存命中、缓存写入、输出和推理 token。推理 token 属于输出 token，不重复计费。一次 Telegram 回复如果包含多次模型调用，账本会逐次记录。
- `/cost` 查询最近一轮，`/cost today` 按北京时间查询当天。默认每轮结束自动发送用量摘要；设置 `COST_AUTO_REPORT=false` 可关闭自动消息，保留账本和查询。`COST_TRACKING_ENABLED=false` 可完全关闭记录。
- `/cost` 中“各回合准备的记忆上下文”是每轮一次的大小快照，不是多次模型调用的累计注入量，也不能单独换算记忆 token 或费用；整轮 token 与费用仍来自逐次用量事件。
- 金额用 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/) 的公开费率估算，不代表官方实扣。pi 的会话统计仅用于核对漏记，不能代替逐次调用用量；如果缺少模型价格、费率已过复核日或账本与 pi 统计不符，查询会明确提示。
- 费率和峰谷时段维护在 [pricing.json](pricing.json)。官方变价后更新该文件的 `revision`、`checked_at`、`review_after`、生效区间、模型费率及中国节假日日历，再推送部署；服务每次计算都重新读取，无须修改 Python 代码。每次记录同时保存费率快照，后续更新价格不会改写历史账单。超过已配置的生效区间或节假日日历时停止估价，保留 token 数。可通过 `PRICING_FILE` 指定其他价格文件。
- 目前仅为 DeepSeek 模型估价；其他提供商照样记录 token，但显示未计价。要迁移用量账本，拷贝配置的 SQLite 数据库文件即可；它不是跨会话记忆功能。

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
- SQLite 中仍处于 `running` 的旧回合会在启动时变为 `incomplete`，并在用户下一条普通消息时提醒一次；这不会恢复中断期间丢失的回复。
- 推送到 Gitea `main` 后，查看部署日志中的目标提交和 `systemctl restart`，再核对 `/healthz`。服务器上直接修改代码时，先提交并推送，保持部署工作区干净。
