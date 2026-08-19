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
| `app.py` | FastAPI 入口：webhook / healthz / 生命周期 |
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
- 修改后本地 `uv lock` + `python -m py_compile *.py` 自检，推送触发服务器 post-receive 自动部署
