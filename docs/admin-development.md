# 记忆管理后台：开发与运维

## 实现

`admin.py` 作为 FastAPI 子路由挂在现有 `app.py` 的 `/admin`。页面由 Python 渲染 HTML，样式源文件位于 `admin_static/style.css`；没有前端构建步骤。后台通过 `Storage` 操作记忆、会话、摘要、费用与配置，显示容量、整理候选和备份状态。记忆合并先预览，再校验条目版本与归属，保留置顶，确认后只更改选中的条目。

人格管理使用 `personas` 与 `user_persona_selection`，会话覆盖使用 `session_preferences`。`persona.py` 校验六个字段并生成准确的追加提示词，`prompts/default-persona.json` 只在首次建表时播种默认人格。`AgentSession` 把有效人格提示词原子写入数据库同目录下的 `persona-prompts/<user_id>.md`，文件权限为 `0600`，目录为 `0700`；提示词变化时重启空闲 Pi RPC 并沿用原会话文件。后台测试用临时目录与 `--no-tools --no-extensions --no-context-files` 独立调用 Pi，账本来源为 `persona_test`。完整产品边界见 [人格系统设计](persona-product.md)。

邮件设置页使用相同的管理员会话与 CSRF 校验。`smtp_settings` 只有 `id=1` 一行，表单不回填密码。`mail_service.py` 负责参数校验和 SMTP 发送，后台测试发送使用 `asyncio.to_thread`，避免同步 SMTP 阻塞 webhook 事件循环。`bot.py` 为聊天 Pi 加载 `pi_extensions/mail-tool.ts`；扩展将收件人、主题、正文写入 `mail_cli.py` 的标准输入，由短生命周期 Python 进程创建 SQLite `mail_drafts`，返回草稿 ID，Bot 再展示预览。用户确认后由服务执行 SMTP，草稿跨重启保留，24 小时后不能发送。工具参数和结果不接收、打印或返回授权码；服务账号及其 Pi 文件/命令工具拥有相同文件权限，不能把工具边界当成操作系统级隔离。

生产入口使用 `Storage(db_path, secret_file=".env")`。后台保存密码时保留 `.env` 的其他键与注释，原子更新 `SMTP_PASSWORD`，POSIX 权限为 `0600`，数据库只存 `env:SMTP_PASSWORD` 引用。启动 `migrate_smtp_secret()` 先写文件，再替换旧数据库密码并 `VACUUM` 清理旧页；没有邮件配置则不创建文件。引用解析每次读取文件，关闭变量插值，文件缺失返回空密码。`mail_cli.py` 指向项目同目录的 `.env`，只创建待确认草稿，不迁移密码、不发送 SMTP。默认 `Storage(secret_file=None)` 只保留测试与旧接口兼容，生产路径使用环境引用。

端口 465 通过 `SMTP_SSL` 在连接开始时完成 TLS；587 通过 `SMTP` 连接、`EHLO`、`STARTTLS`、再次 `EHLO` 后才认证。使用系统信任链验证证书；不允许明文回退。只接受单个邮箱、纯文本正文，限制主题和正文长度。SMTP 服务端接受后的退信不在本项目追踪范围内。明确拒绝标为 `failed`，发送超时或断线等未确认结果标为 `uncertain`；启动时把遗留的 `sending` 转成 `uncertain`，保留历史，不自动重发。检查 `/mail history` 或 `/mail status <ID>` 后再决定下一步，不重放旧 Telegram 更新。

概览使用 `update_stats()` 和最近失败更新的元数据，不读取原始 Telegram payload；Pi 状态只检查已有的 `AgentSession`，不因为打开页面启动新进程。管理员核对失败更新后可提交带 CSRF 的“标记已处理”：`telegram_updates.state` 变为 `acknowledged` 并记录 `acknowledged_at`，`update_id` 保留用于去重，原消息不会重放。`/healthz` 的当前就绪状态检查 worker、Telegram 探测与队列阻塞，未就绪返回 503；历史未处理事故单列显示。30 天清理包含失败与已处理记录。会话新建与切换调用 `bot.py` 中供 Telegram 和后台共用的 `create_user_session()` / `switch_user_session()`，并在同一用户的 `control_lock` 下执行。后台重命名仅更改 SQLite 显示名，会话模型与人格覆盖在 `session_preferences` 中持久化。用量页按北京时间计算日界，通过 `usage_runs` / `usage_events` 汇总；金额使用 Decimal，记忆维护按 `origin` 单列。

`delivery.py` 保存模型结果及 Telegram 分段送达状态，`/resend` 只补发已保存文字。`interactions.py` 为语音、图片与 Pi 输入框保存按用户隔离的一次性确认；`media.py` 在受限子进程中解析 PDF 和本地语音。`ops.py` 负责完整 SQLite+JSONL 备份、校验和离线恢复，见 [运维说明](operations.md)。

导航使用普通链接，切页会加载完整 HTML。最初页面从 `/admin/style.css` 读取样式且响应为 `no-store`，每次切页都重新请求，浏览器短暂显示未着色内容。现在服务启动时读取约 8 KB 的样式，将其直接放入每页 `<head>`；CSP 使用对应的 SHA-256 哈希放行这份固定样式。页面仍保持 `no-store`，且切页不再依赖第二个样式请求。

新增的 `runtime_settings(key, value_json, updated_at)` 保存可在线修改的记忆参数。启动顺序是建立 SQLite 连接、读取和校验覆盖值、应用到共享 `Settings` 对象，然后创建 `AgentManager`。表单保存时先整体校验，再写入 SQLite，最后更新进程中的 `Settings`；写入失败不会更新运行值。数据库迁移由 `CREATE TABLE IF NOT EXISTS` 完成。

| 配置来源 | 优先级 | 内容 |
|---|---:|---|
| `.env` | 基础 | 管理员身份、密钥、模型以及全部默认记忆参数 |
| `runtime_settings` | 高 | 后台允许编辑的记忆参数，逐项覆盖 `.env` |

后台提供“恢复环境默认值”操作：校验当前 `.env` 默认值，删除记忆参数的 `runtime_settings` 覆盖，再同步运行值。操作经过管理员认证与 CSRF 校验，不需要手工修改数据库。

## 登录与请求保护

- `ADMIN_USERNAME` 为 3–32 位字母、数字、下划线或连字符；`ADMIN_PASSWORD` 为 12–128 个字符。生产环境使用随机长密码，仅放在服务器 `.env`，不得提交仓库。
- 登录前和登录后使用不同的 HMAC 签名 cookie；签名密钥由管理员凭据和 webhook 密钥派生。cookie 设置 `Secure`、`HttpOnly`、`SameSite=Strict`，路径限定 `/admin`。登录有效期 12 小时，改密码会使旧 cookie 失效。
- 所有写操作只接受 URL 编码表单，要求隐藏 CSRF token 与签名 cookie 一致。登录失败按客户端地址限制为 15 分钟内最多 8 次；单次表单最多 16 KiB。
- 后台 HTML 禁用缓存，并设置 CSP、拒绝 iframe、禁止引用来源等响应头。管理操作只记录 ID 和参数键，不记录记忆正文与密码。
- SMTP 授权码与旧版本数据库、备份按凭据保护。POSIX 上数据库文件由 `Storage` 限制为 `0600`，完整备份目录与归档分别为 `0700` / `0600`，不得放入 Git。凭据存储的最终行为见 [邮件设置说明](admin-product.md)。
- 反向代理必须提供 HTTPS。若使用代理转发真实客户端地址，只在请求的直接来源为本机时读取 `X-Real-IP`。

## 配置与部署

在服务器项目目录的 `.env` 中设置：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<随机生成的 12–128 字符密码>
```

然后按项目既有链路部署：推送 Gitea `main` 后由 webhook 执行 `deploy/deploy.sh`；仅修改 `.env` 时也需通过部署脚本重启服务。使用 `uv run python -m scripts.backup_bundle --reason manual` 生成数据库与完整会话归档。迁移需要校验并恢复到新目录，核对会话路径重绑定，并另外配置 `.env`、Pi 私有模型凭据与项目文件；详见 [备份恢复](operations.md)。

验证：

```bash
uv lock
uv run python -m unittest discover -s tests -v
uv run python -m py_compile *.py
```

线上检查 `/admin/login` 返回登录页、未登录的 `/admin/memories` 与 `/admin/mail` 跳转登录页，登录后检查记忆、设置和邮件页能读取。参数保存和删除操作有实际副作用，线上验证应使用专门的测试条目并在结束后删除。邮件测试只向明确授权的地址发送；检查 SMTP 返回成功与收件端投递结果。不要在命令行参数、日志或提交中放入授权码。

## 文件与后续扩展

`config.py` 定义身份和默认值；`storage.py` 管理配置表与查询；`admin.py` 实现校验、认证、页面和写操作；`bot.py` 提供共用会话操作；`admin_static/style.css` 实现响应式视觉样式；`mail_service.py`、`mail_cli.py` 与 `pi_extensions/mail-tool.ts` 构成发信链路；`tests/test_admin.py` 和 `tests/test_mail.py` 验证登录、CSRF、秘密不回显、配置持久化及 TLS 模式。

若未来需要多管理员、精确审计或复杂检索，可在这些边界内增加管理员表、操作日志表和 FTS5。首版无需额外服务。
