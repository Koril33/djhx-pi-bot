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
- 可发送文字、图片、受限文本/PDF 附件与短语音；视频、贴纸等未支持类型会收到明确提示。
- 可发送单张 Telegram 照片或图片文件，可附文字说明；支持 JPEG、PNG、GIF、WebP，单张最大 10 MiB、宽高各不超过 8192 像素。使用 `/model list` 查看带 🖼 标记的图片模型。Telegram 相册最多 4 张图片、总计不超过 20 MiB，按组一次处理。
- 可发送 UTF-8 文本、代码或日志附件（如 .txt、.md、.py、.json、.log 等），最大 64 KiB 且不超过 30000 字符；可在说明文字中提问。未加密的含文字 PDF 默认最大 5 MiB、30 页，提取最多 30000 字符，超过文字预算会明确标注截断；扫描件请发图片。Office 和其他二进制文件暂不读取。
- 语音和音频默认最多 60 秒、10 MiB，使用本地 tiny 模型转写。转写后先预览，确认才作为普通文字提交；也可取消或复制修改后发送。首次启用需要 `uv run python -m scripts.prepare_voice` 下载并校验固定模型，日常转写不调用外部语音服务。参数见 [.env.example](.env.example)。
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
| `/help [chat\|sessions\|memory\|other]` | 精简首屏及分类帮助 |
| `/env list` / `/env use <name>` | 环境（V1 仅 server） |
| `/session new [name]` / `list [页码]` / `search <关键词>` / `rename <id> <名称>` / `use <id>` | 会话管理；缺失历史先预览再确认恢复 |
| `/session config show` / `model <provider/id>` / `persona <id\|none>` / `inherit` | 当前会话的模型与人格组合；`inherit` 恢复个人默认选择 |
| `/model list` / `/model use <id>` | 模型切换；用户选择在服务重启后保持 |
| `/persona current` / `list` / `use <id\|none> [new]` | 个人默认人格；`none` 不追加人格，`new` 同时新建会话隔离旧历史 |
| `/stop` | 取消本用户待启动消息，并中止下载、初始化、解析或当前模型任务 |
| `/steer <内容>` | 明确将指令插入正在运行的任务；图片可在 caption 中使用 |
| `/queue` / `/queue clear` | 查看或取消尚未开始的排队消息；普通消息依次成为新回合 |
| `/resend [结果 ID]` | 补发 SQLite 已保存的完整或部分回复；不会重跑模型或工具 |
| `/mail` | 预览、确认或取消 pi 创建的邮件草稿；24 小时有效 |
| `/mail history` / `/mail status <草稿 ID>` | 查询已提交、明确失败、结果不确定及过期邮件 |
| `/answer <输入框 ID> <内容>` | 回答 Pi 扩展的文字输入框；confirm/select 使用 Telegram 按钮 |
| `/status` | 本地运行阶段、实际模型、任务耗时、队列与待补发结果；RPC 查询有时间限制 |
| `/memory list [页码]` / `add` / `edit` / `delete` / `pin` / `unpin` | 长期记忆管理 |
| `/memory search <关键词>` / `used [list\|回合 ID]` / `auto [status\|on\|off\|default]` | 搜索、查看实际注入与依据、暂停或恢复自动提取 |
| `/memory capacity` / `manage` / `merge <ID1> <ID2> [整理后的内容]` | 查看满额状态与整理候选；预览确认后合并，保留置顶 |
| `/summary show` / `status` / `clear` | 查看摘要、待处理轮次及维护状态；清空须确认 |
| `/cost` / `/cost today [compact]` / `/cost auto off\|compact\|full` | 最近一轮 / 今日用量费用与每用户自动报告设置 |

## 聊天人格

后台“人格管理”把身份、性格、沟通风格、行为规则、背景和偏好保存到 SQLite；可创建、编辑、停用、删除、预览生成的追加提示词，并使用独立的无工具 Pi 会话测试。`/persona use` 与 `/model use` 保存个人默认选择；`/session config` 可为当前会话保存独立模型、人格或显式无人格，未覆盖的项继续继承个人选择。切换会话会加载对应组合，设置跨重启保持；`inherit` 清除当前会话覆盖。提示词变化从下一条消息开始生效，原 JSONL 不变。初始默认人格从 [prompts/default-persona.json](prompts/default-persona.json) 首次种入 SQLite，之后不自动覆盖；该模板称呼“小丁”，第二位用户可选择专属人格或 `none`。详见 [人格系统设计](docs/persona-product.md)。

## 长期记忆与会话摘要

- 自动记忆默认启用：每个会话积累指定数量的完整回合后，用独立、无工具的 DeepSeek Flash 调用更新该会话摘要，并提取跨会话稳定偏好。项目细节只留在会话摘要。维护调用单独计入 `/cost today`。
- `/memory add <内容>` 可以立即手动保存。`/memory list` 显示记忆 ID，`/memory edit <id> <内容>`、`/memory delete <id>` 可以修改或删除。每条记忆和有效记忆数量均有可配置上限。
- `/memory capacity` 与 `/memory manage` 明确显示容量、最近自动新增/重复刷新/因满额跳过的数量。满额不自动删除旧条目；表达相近与可能冲突的候选仅供核对，`/memory merge` 或后台整理页经预览确认后合并，保留置顶并拒绝过期版本。精确重复只刷新日期，保留人工文本、来源与置顶。
- pi 扩展在模型请求前临时注入受预算限制的 SQLite 背景资料，不把同一份记忆逐轮写入 JSONL。删除后不再主动注入，但已存在于旧 JSONL、旧回复或摘要的内容不会自动消失。
- 置顶条目优先；无匹配的非置顶记忆不再为了凑数量注入。`/memory used` 可核对一轮实际注入的文本与匹配依据。
- 自动摘要固定使用 DeepSeek Flash，不受 `/model use` 影响。若服务意外中断，未完成回合会在启动时标为不完整；下一条普通消息会提醒用户，不会自动重放可能已经执行工具的请求。
- 长回合按字节分为多次隔离的摘要请求，每次严格受输入预算限制；所有片段成功后才推进整轮覆盖进度。中途失败不提交部分摘要与记忆，后续重试仍能读取完整待处理回合。摘要仍有损，长回合会增加维护调用次数。
- Webhook 更新在入队时记录用户与当前会话；处理失败时向已授权用户发送不含内部错误的结果不确定提示。通知失败会保留待提醒状态，下次普通消息补发；可能有外部副作用的更新不会自动重放。
- 每位用户可用 `/cost auto off|compact|full` 控制每轮费用报告；`/cost last compact` 与 `/cost today compact` 提供简洁查询。未设置时沿用全局 COST_AUTO_REPORT。
- `/stop` 会取消本用户尚未开始的排队消息，并中止当前运行任务；回复分别报告取消数量和中止状态。
- 会话 JSONL 缺失时，bot 先预览部分或空白恢复范围；用户确认后才为原会话 ID 新建 Pi 会话。恢复上下文给旧摘要与最新文字回合分别留预算，并保留问题、回复及插入指令的头尾，截断有明确标记。无法仅靠 SQLite 恢复旧图片、工具输出或逐字历史。
- `MEMORY_*` 环境变量控制自动提取周期、原始回合保留数量、单条记忆长度和注入字节预算；维护模型不提供可覆盖项，见 [.env.example](.env.example)。完整设计及限制见 [产品设计文档](docs/memory-product.md)。
- 单管理员后台位于 `/admin`，可查看运行概览、管理会话与记忆、查看用量费用、调整记忆预算。账号密码由 `.env` 的 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 配置；后台保存的参数持久化到同一个 SQLite，并优先于相应的环境变量。见 [后台产品设计](docs/admin-product.md) 与 [开发运维文档](docs/admin-development.md)。
- 后台“邮件设置”可配置 SMTP 服务器、465 SSL/TLS 或 587 STARTTLS、用户名、授权码及发件邮箱。授权码保存到项目 `.env` 的 `SMTP_PASSWORD`，SQLite 只存环境引用，页面与工具不回显密码；启动会迁移旧数据库明文并清理数据库页，旧备份仍按凭据保护。服务账号及其 Pi 文件/命令工具拥有相同文件权限。Pi 的 `send_email` 只创建持久化的纯文本草稿，Bot 完整预览后等待用户确认，再由 Python 连接 SMTP。草稿跨重启保留，24 小时后不能发送。`/mail history` 与 `status` 区分已提交、明确拒绝和结果不确定；重启遗留的 `sending` 标为不确定，不自动重发。SMTP 接受不保证最终入箱。

## 网络恢复与结果补发

Telegram 的初始化、`getMe`、启动和命令注册遇到网络错误时按配置退避重试；错误 token 立即失败。运行中定期探活，更新消费者意外退出会恢复。`/stop` 有独立的控制槽，下载、初始化与解析也受取消控制。

模型输出与 Telegram 送达分别记账。流式编辑短暂失败后继续接收 Pi 输出，生成的文字先持久化，最终发送失败仍能用 `/resend` 取回；服务重启后保留已生成的完整或部分结果。补发只发送保存的分段，不继续任务、不重跑模型或工具。有 Telegram 消息 ID 时优先编辑；发送超时而没有 ID 时先提示可能重复，用户确认才补发。未完成任务的缓存明确标为部分结果。

Pi 扩展的确认、选择与文字输入通过 Telegram 按钮或 `/answer` 回答，默认两分钟内有效，超时取消。图片模型不兼容时可保留受限待确认请求，切换到支持图片的模型后继续处理；确认入口过期或会话变化时会明确提示。

## 备份与恢复

完整流程与故障处理见 [运维说明](docs/operations.md)。

服务默认每个成功备份周期（24 小时）生成一致 SQLite 加完整 `PI_SESSION_DIR` JSONL 归档。备份前暂停新普通更新的处理，等待已开始任务和摘要维护空闲；忙碌或失败时延后。保留最近 7 份 daily 快照及最近 4 个 ISO 周的首份 weekly 快照，后台概览和 `/healthz` 显示最近成功时间与状态。部署前也生成完整备份。

```bash
uv run python -m scripts.backup_bundle --reason manual
uv run python -m scripts.restore_backup <archive.tar.gz> --verify-only
uv run python -m scripts.restore_backup <archive.tar.gz> --destination <新的离线目录>
```

手动备份前先确认没有正在写入的任务。恢复命令先校验 manifest、SHA256、SQLite 与 JSONL，只允许新的目标目录，不覆盖线上文件。新格式归档保存原会话根目录，可将确实归档的会话路径重绑定到新目录；旧格式或未归档/外部路径会返回 `restore.manual_rebind=true`，须逐项核对，不能把这种恢复当作完整迁移。恢复后的数据库和会话目录应保持在指定目标路径；启用前再配置 `DB_PATH`、`PI_SESSION_DIR` 并按部署流程重启。

归档不包含 `.env`、Pi 的模型凭据、项目工作文件或未保存到 JSONL 的外部附件。新服务器需另行保护并配置这些内容，包括 SMTP 授权码。SQLite 包含用户消息、记忆、草稿与送达记录；旧版本备份可能含 SMTP 明文密码。备份目录/文件在 POSIX 上分别限制为 0700/0600。现有 `scripts.backup_db` 仍可生成只含 SQLite 的单文件备份，但它不能逐字恢复完整 Pi 会话。异机副本需要另行安排，服务内置的本机备份不能应对服务器整盘丢失。

## 用量和费用

- 每次 pi 模型回复及会话压缩结束后，保存 pi 报告的输入未命中、缓存命中、缓存写入、输出和推理 token。推理 token 属于输出 token，不重复计费。一次 Telegram 回复如果包含多次模型调用，账本会逐次记录。
- `/cost` 查询最近一轮，`/cost today` 按北京时间查询当天。默认每轮结束自动发送用量摘要；设置 `COST_AUTO_REPORT=false` 可关闭自动消息，保留账本和查询。`COST_TRACKING_ENABLED=false` 可完全关闭记录。
- `/cost` 中“各回合准备的记忆上下文”是每轮一次的大小快照，不是多次模型调用的累计注入量，也不能单独换算记忆 token 或费用；整轮 token 与费用仍来自逐次用量事件。
- 金额用 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/) 和 [小米 MiMo 官方价格页](https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go) 的公开费率估算，不代表官方实扣。pi 的会话统计仅用于核对漏记，不能代替逐次调用用量；如果缺少模型价格、费率已过复核日或账本与 pi 统计不符，查询会明确提示。
- 费率和峰谷时段维护在 [pricing.json](pricing.json)。文件按 `providers.<provider>` 分块（目前含 `deepseek`、`xiaomi`），每块自带 `revision`、`checked_at`、`review_after`、生效区间、`aliases`、可选 `peak_schedule` 和 `models`，共享顶层 `currency`/`per_tokens`。DeepSeek 有峰谷分时，MiMo 全天单一费率（`tier` 为空）；官方变价后更新对应块的字段、模型费率及中国节假日日历，再推送部署；服务每次计算都重新读取，无须修改 Python 代码。每次记录同时保存费率快照，后续更新价格不会改写历史账单。超过已配置的生效区间或节假日日历时停止估价，保留 token 数。可通过 `PRICING_FILE` 指定其他价格文件。
- 目前为 DeepSeek 和小米 MiMo 模型估价；其他提供商照样记录 token，但显示未计价。要迁移用量账本，拷贝配置的 SQLite 数据库文件即可；它不是跨会话记忆功能。

## 部署（Gitea 驱动）

```
本地 push ──► Gitea (git@git.djhx.site:koril/djhx-pi-bot.git)
                  │ Push Webhook
                  ▼
        https://pi.djhx.site/deploy-webhook/<DEPLOY_SECRET>
                  │ 后台执行
                  ▼
        deploy/deploy.sh: 空闲护栏 → 同步与自检 → 完整备份 → 重启与就绪检查
```

- 单一事实源：Gitea；服务器通过 webhook 自动部署
- `deploy/deploy.sh` 与 `deploy/djhx-pi-bot.service` 随仓库版本化
- Gitea 仓库 → 设置 → Web 钩子 → 添加：URL 填 `https://pi.djhx.site/deploy-webhook/<secret>`（Gitea 类型，Push 事件，Secret 填 DEPLOY_SECRET）
- 部署日志：`logs/deploy.log`；服务日志：`journalctl -u djhx-pi-bot -f`
- 部署结果记录在 `data/deploy-state`；健康检查同时核对运行提交。重启由独立部署进程协调，启动补偿检查可处理重启期间遗漏的 push；失败目标有护栏，避免恢复旧版本后再次自动部署同一失败提交。
- 失败部署由脚本尝试恢复先前运行代码；人工回退则把需要恢复的代码提交为新的 `main` 提交，推送后核对部署结果。数据恢复单独按备份流程处理。

## 故障排查

- `curl http://127.0.0.1:8128/healthz` 分别查看 `readiness`（消费者、Telegram、队列）、`incidents.unacknowledged`（待核对旧事故）、`activity`、运行 `revision` 和 `backup`。`ok/degraded` 根据当前就绪状态，旧失败记录本身不再让正常服务持续降级；未就绪返回 HTTP 503。核对事故后可在后台“标记已处理”，保留去重记录且不重放。失败与已处理记录均按 30 天保留。
- `journalctl -u djhx-pi-bot --since '1 hour ago' --no-pager` 查看 bot、pi RPC 和 Telegram 发送错误；`logs/deploy.log` 记录部署过程。
- 已开始处理的 Telegram 更新若因服务重启或异常而中断，会记录为失败，不会自动重放可能已执行工具的请求。确认结果后由用户重新发送。
- SQLite 中仍处于 `running` 的旧回合会在启动时变为 `incomplete`，下一条普通消息提醒一次。已持久化的文字可用 `/resend` 查询；未生成或未写入的文字、尚未确定的工具副作用不能靠补发恢复。
- 推送到 Gitea `main` 后，查看部署日志中的目标提交和 `systemctl restart`，再核对 `/healthz`。服务器上直接修改代码时，先提交并推送，保持部署工作区干净。
