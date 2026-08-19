# AGENTS.md

本文件为 AI 编码代理（如 pi）提供项目约定。

## 技术栈

- Python >= 3.12，包管理 uv（`uv sync` / `uv lock`）
- FastAPI + uvicorn（webhook 入口）
- python-telegram-bot v22（异步）
- SQLite（标准库 sqlite3，`storage.py`）
- 日志：python-json-logger（结构化 JSON，见 `logging_conf.py`）

## 结构

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 入口：webhook / healthz / deploy-webhook / 生命周期 |
| `bot.py` | Telegram 处理器 + Agent 会话编排（AgentManager / AgentSession） |
| `pi_bridge.py` | pi RPC 常驻子进程管理（JSONL 协议、流式、崩溃重启） |
| `storage.py` | SQLite 用户/会话映射 |
| `config.py` | pydantic-settings 配置（环境变量 + .env） |
| `logging_conf.py` | dictConfig + JSON 日志 |

## 约定

- 密钥只放 `.env`（不入库），仓库只提交 `.env.example`
- 日志统一走 `get_logger()`，用 `extra={...}` 携带上下文
- 普通消息走 `on_text` → `AgentSession.ensure_client()` → `client.prompt()` 流式回复
- pi RPC 子进程 stdin 必须保持打开，禁止 close
- 修改后本地 `uv lock` + `python -m py_compile *.py` 自检，`git push gitea main` 触发 Gitea webhook 自动部署
- 部署链路：Gitea Push webhook → `app.py /deploy-webhook` → `deploy/deploy.sh`（pull+sync+restart）

## ⚠️ 工作流警示（在服务器部署目录里改代码时必读）

服务器上的 pi（通过 Telegram 驱动）**直接在 `/home/pi/project/djhx-pi-bot` 这个已部署的 git 仓库里工作**，必须遵守：

1. **改完必须 `git add` + `git commit` + `git push origin main`**。仓库对服务器是可写的（deploy key 已授权），不 push 会留下未提交改动或未推送提交。
2. **不 push 的后果**：任何 webhook 自动部署执行 `git pull` 时都会因工作区不干净而中止（`deploy/deploy.sh` 有护栏，会明确报错并提示先 push）。此时线上服务保持旧代码，且后续部署全部卡住。
3. 历史事故：2026-08-19 曾因 pi 本地改 `app.py` 未提交，导致一次 webhook 部署 pull 失败、服务跑了半小时旧代码。**先 commit+push 再触发新部署**是铁律。
4. 需要重启服务、跑部署脚本等系统操作，通过 `deploy/deploy.sh`（或 webhook 自动触发）完成，pi 不要直接改系统级配置。

## 常见坑（本项目的调试经验）

| 坑 | 说明 |
|---|---|
| `pi --mode rpc` 事件字段 | `message_update.assistantMessageEvent` 的文本增量字段是 **`delta`**，不是 `text` |
| stdin 不能 EOF | RPC 子进程 stdin 一旦关闭，pi 会提前退出、掐断进行中的推理 |
| PTB 手动启动 | `initialize()+start()` 手动模式**不触发 `post_init`**，命令菜单等需显式调用 `register_command_menu(bot)` |
| 日志 extra 保留键 | LogRecord 保留字段（如 `args`）不能用作 `extra` 键，会抛 "Attempt to overwrite" |
| 服务器→Telegram API 不稳 | `tg_app.start()`/请求偶发 `httpx.ReadError`，启动已加重试（lifespan 内 5 次×3s），勿删 |
| 换行符 | 仓库有 `.gitattributes` 统一 LF，Windows 提交勿手动改行尾 |
