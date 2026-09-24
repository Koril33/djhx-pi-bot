# 记忆管理后台：开发与运维

## 实现

`admin.py` 作为 FastAPI 子路由挂在现有 `app.py` 的 `/admin`。页面由 Python 渲染 HTML，样式源文件位于 `admin_static/style.css`；没有前端构建步骤。后台通过现有 `Storage` 操作 `memory_items`、`sessions` 和 `session_summaries`。

导航使用普通链接，切页会加载完整 HTML。最初页面从 `/admin/style.css` 读取样式且响应为 `no-store`，每次切页都重新请求，浏览器短暂显示未着色内容。现在服务启动时读取约 8 KB 的样式，将其直接放入每页 `<head>`；CSP 使用对应的 SHA-256 哈希放行这份固定样式。页面仍保持 `no-store`，且切页不再依赖第二个样式请求。

新增的 `runtime_settings(key, value_json, updated_at)` 保存可在线修改的记忆参数。启动顺序是建立 SQLite 连接、读取和校验覆盖值、应用到共享 `Settings` 对象，然后创建 `AgentManager`。表单保存时先整体校验，再写入 SQLite，最后更新进程中的 `Settings`；写入失败不会更新运行值。数据库迁移由 `CREATE TABLE IF NOT EXISTS` 完成。

| 配置来源 | 优先级 | 内容 |
|---|---:|---|
| `.env` | 基础 | 管理员身份、密钥、模型以及全部默认记忆参数 |
| `runtime_settings` | 高 | 后台允许编辑的记忆参数，逐项覆盖 `.env` |

当前后台不提供“恢复环境变量默认值”按钮。需要恢复时，备份数据库后删除 `runtime_settings` 中相应的键并重启服务。

## 登录与请求保护

- `ADMIN_USERNAME` 为 3–32 位字母、数字、下划线或连字符；`ADMIN_PASSWORD` 为 12–128 个字符。生产环境使用随机长密码，仅放在服务器 `.env`，不得提交仓库。
- 登录前和登录后使用不同的 HMAC 签名 cookie；签名密钥由管理员凭据和 webhook 密钥派生。cookie 设置 `Secure`、`HttpOnly`、`SameSite=Strict`，路径限定 `/admin`。登录有效期 12 小时，改密码会使旧 cookie 失效。
- 所有写操作只接受 URL 编码表单，要求隐藏 CSRF token 与签名 cookie 一致。登录失败按客户端地址限制为 15 分钟内最多 8 次；单次表单最多 16 KiB。
- 后台 HTML 禁用缓存，并设置 CSP、拒绝 iframe、禁止引用来源等响应头。管理操作只记录 ID 和参数键，不记录记忆正文与密码。
- 反向代理必须提供 HTTPS。若使用代理转发真实客户端地址，只在请求的直接来源为本机时读取 `X-Real-IP`。

## 配置与部署

在服务器项目目录的 `.env` 中设置：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<随机生成的 12–128 字符密码>
```

然后按项目既有链路部署：推送 Gitea `main` 后由 webhook 执行 `deploy/deploy.sh`；仅修改 `.env` 时也需通过部署脚本重启服务。可使用 `uv run python -m scripts.backup_db <备份文件>` 生成 SQLite 一致性备份。迁移到新服务器时复制数据库并重新配置 `.env` 管理员凭据和其他服务密钥。

验证：

```bash
uv lock
uv run python -m unittest discover -s tests -v
uv run python -m py_compile *.py
```

线上检查 `/admin/login` 返回登录页、未登录的 `/admin/memories` 跳转登录页，登录后检查记忆与设置能读取。参数保存和删除操作有实际副作用，线上验证应使用专门的测试条目并在结束后删除。

## 文件与后续扩展

`config.py` 定义身份和默认值；`storage.py` 管理配置表与查询；`admin.py` 实现校验、认证、页面和写操作；`admin_static/style.css` 实现响应式视觉样式；`tests/test_admin.py` 验证登录、CSRF、用户隔离、记忆 CRUD、摘要清空和设置持久化。

若未来需要多管理员、精确审计或复杂检索，可在这些边界内增加管理员表、操作日志表和 FTS5。首版无需额外服务。
