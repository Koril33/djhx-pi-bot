"""Telegram 业务层：python-telegram-bot 处理器 + Agent 会话编排。

V1 拓扑：单个环境（服务器本机），每用户一个常驻 pi RPC 进程。
命令面：
  /help /start /status
  /env list /env use <name>          （V1 仅 server 本机）
  /session new [name] /session list /session use <id>
  /model list /model use <model_id>
  /stop
普通文本：转发给 pi（常驻 RPC，流式回复）。
"""
import asyncio
import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from logging_conf import get_logger
from pi_bridge import PiBusyError, PiError, PiRpcClient
from storage import Storage

logger = get_logger("bot")

MAX_MSG_LEN = 4000  # Telegram 单条消息上限（留余量）


class AgentSession:
    """单个用户在单个环境下的 pi 会话。"""

    def __init__(self, user_id: int, settings: Settings, storage: Storage):
        self.user_id = user_id
        self.settings = settings
        self.storage = storage
        self.client = PiRpcClient(
            pi_cmd=settings.pi_cmd,
            session_dir=settings.pi_session_dir,
            provider=settings.pi_provider,
            model=settings.pi_model,
        )

    async def ensure_default_session(self) -> None:
        """确保用户至少有一个 active 会话记录。"""
        if self.storage.get_active_session(self.user_id) is None:
            sid = self.storage.create_session(self.user_id, "默认会话")
            self.storage.set_active(sid)

    async def ensure_client(self) -> None:
        """确保 pi 进程在跑，且挂载正确的会话文件。"""
        await self.ensure_default_session()
        active = self.storage.get_active_session(self.user_id)
        session_file = active["pi_session_file"] if active else None
        if self.client.proc is None or self.client.proc.returncode is not None:
            await self.client.ensure_running(session_file)
        # 首次同步 pi 侧会话文件路径
        if active and not active["pi_session_file"]:
            try:
                state = await self.client.get_state()
                if state.get("sessionFile"):
                    self.storage.set_pi_session_file(active["id"], state["sessionFile"])
            except Exception as e:
                logger.warning("同步会话文件失败", extra={"err": str(e)})


class AgentManager:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self._agents: dict[int, AgentSession] = {}

    def get(self, user_id: int) -> AgentSession:
        agent = self._agents.get(user_id)
        if agent is None:
            agent = AgentSession(user_id, self.settings, self.storage)
            self._agents[user_id] = agent
        return agent

    async def shutdown(self) -> None:
        for agent in self._agents.values():
            try:
                await agent.client.stop()
            except Exception:
                pass


# ================= 工具函数 =================

def authorized(update: Update, settings: Settings) -> bool:
    if not settings.allowed_ids:
        return False
    return update.effective_user is not None and update.effective_user.id in settings.allowed_ids


async def reply_text(update: Update, text: str) -> None:
    """按 4096 上限分段回复。"""
    if not text:
        text = "(无内容)"
    for i in range(0, len(text), MAX_MSG_LEN):
        chunk = text[i : i + MAX_MSG_LEN]
        if update.callback_query is not None:
            await update.callback_query.message.reply_text(chunk)
        elif update.message is not None:
            await update.message.reply_text(chunk)
        else:
            await update.effective_chat.send_message(chunk)


async def stream_reply(update: Update, text_iter, interval: float) -> None:
    """流式回复：增量编辑消息（节流），超过上限分段新开消息。"""
    target = update.message or update.callback_query.message
    first = True
    msg = None
    buf = ""
    last_edit = 0.0
    try:
        async for delta in text_iter:
            buf += delta
            now = time.monotonic()
            if len(buf) >= MAX_MSG_LEN:
                if msg is not None:
                    try:
                        await msg.edit_text(buf[:MAX_MSG_LEN])
                    except Exception:
                        pass
                elif first:
                    msg = await target.reply_text(buf[:MAX_MSG_LEN])
                    first = False
                buf = buf[MAX_MSG_LEN:]
                msg = await target.reply_text("…")
                last_edit = now
            elif first:
                msg = await target.reply_text(buf)
                first = False
                last_edit = now
            elif now - last_edit >= interval and 0 < len(buf) <= MAX_MSG_LEN:
                try:
                    await msg.edit_text(buf)
                    last_edit = now
                except Exception:
                    pass
        if msg is None and not buf:
            msg = await target.reply_text("(无输出)")
        elif msg is not None:
            try:
                await msg.edit_text(buf or "(无输出)")
            except Exception:
                pass
    except Exception as e:
        logger.error("流式回复异常", extra={"err": str(e)[:200]})
        try:
            if msg is not None:
                await msg.edit_text(f"{buf}\n\n[输出中断: {e}]")
        except Exception:
            pass


# ================= 命令处理器 =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 djhx-pi-bot — 通过 Telegram 控制 pi 编码代理\n\n"
        "直接发消息即可与 pi 对话。\n\n"
        "命令：\n"
        "/env list — 查看可用环境\n"
        "/env use <name> — 切换环境（V1 仅 server）\n"
        "/session new [名称] — 新建会话\n"
        "/session list — 列出我的会话\n"
        "/session use <id> — 切换到指定会话\n"
        "/model list — 列出可用模型\n"
        "/model use <model_id> — 切换模型\n"
        "/stop — 中止当前任务\n"
        "/status — 查看运行状态\n"
        "/help — 帮助"
    )
    await reply_text(update, text)


async def cmd_env(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0] == "list":
        await reply_text(update, "环境列表：\n  server  （本机，当前环境）")
        return
    if args[0] == "use":
        if len(args) < 2:
            await reply_text(update, "用法：/env use <name>")
            return
        if args[1] == "server":
            await reply_text(update, "已切换到环境 server（当前唯一环境）")
        else:
            await reply_text(update, f"未知环境：{args[1]}")
        return
    await reply_text(update, "用法：/env list | /env use <name>")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    args = context.args
    sub = args[0] if args else ""

    if sub == "new":
        name = " ".join(args[1:]).strip() or f"会话-{len(storage.list_sessions(user.id)) + 1}"
        try:
            sid = storage.create_session(user.id, name)
            storage.set_active(sid)
            await agent.ensure_client()  # 确保进程在跑
            await agent.client.new_session(name)
            state = await agent.client.get_state()
            if state.get("sessionFile"):
                storage.set_pi_session_file(sid, state["sessionFile"])
            await reply_text(update, f"✅ 已新建会话「{name}」\nID: {sid}")
        except Exception as e:
            logger.error("新建会话失败", extra={"user_id": user.id, "err": str(e)[:300]})
            await reply_text(update, f"新建会话失败: {e}")

    elif sub == "list":
        rows = storage.list_sessions(user.id)
        if not rows:
            await reply_text(update, "还没有会话，用 /session new 创建")
            return
        lines = ["📁 会话列表："]
        for r in rows:
            marker = "▶ " if r["status"] == "active" else "  "
            lines.append(f"{marker}`{r['id']}` {r['name']}  ({r['updated_at']})")
        await reply_text(update, "\n".join(lines))

    elif sub == "use":
        if len(args) < 2:
            await reply_text(update, "用法：/session use <id>")
            return
        sid = args[1]
        row = storage.get_session(sid)
        if row is None or row["user_id"] != user.id:
            await reply_text(update, f"找不到会话 {sid}")
            return
        if not row["pi_session_file"]:
            await reply_text(update, "该会话没有关联的 pi 会话文件，无法切换（请用 /session new 创建）")
            return
        try:
            # 进程内切换会话，无需重启（避免冷启动）
            await agent.ensure_client()
            await agent.client.switch_session(row["pi_session_file"])
            storage.set_active(sid)
            await reply_text(update, f"✅ 已切换到会话「{row['name']}」({sid})")
        except Exception as e:
            logger.error("切换会话失败", extra={"err": str(e)[:300]})
            await reply_text(update, f"切换会话失败: {e}")

    else:
        await reply_text(update, "用法：/session new [名称] | /session list | /session use <id>")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    args = context.args
    sub = args[0] if args else ""

    if sub == "list":
        try:
            models = await agent.client.get_available_models()
            lines = ["🧠 可用模型："]
            for m in models:
                images = "🖼" if "image" in (m.get("input") or []) else "  "
                lines.append(f"{images} {m['id']}  (context={m.get('contextWindow','?')})")
            await reply_text(update, "\n".join(lines))
        except Exception as e:
            logger.error("模型列表失败", extra={"err": str(e)[:300]})
            await reply_text(update, f"获取模型列表失败: {e}")

    elif sub == "use":
        if len(args) < 2:
            await reply_text(update, "用法：/model use <model_id>")
            return
        try:
            await agent.client.set_model(args[1])
            await reply_text(update, f"✅ 已切换模型: {args[1]}")
        except Exception as e:
            logger.error("切换模型失败", extra={"err": str(e)[:300]})
            await reply_text(update, f"切换模型失败: {e}")

    else:
        await reply_text(update, "用法：/model list | /model use <model_id>")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    await agent.client.abort()
    await reply_text(update, "⏹ 已发送中止指令")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    storage = context.bot_data["storage"]
    agent = manager.get(user.id)
    proc = agent.client.proc
    proc_state = "运行中" if (proc and proc.returncode is None) else "未运行"
    active = storage.get_active_session(user.id)
    active_name = active["name"] if active else "-"
    lines = [
        "📊 状态：",
        f"  环境: server（本机）",
        f"  pi 进程: {proc_state}",
        f"  忙碌: {'是' if agent.client.busy else '否'}",
        f"  当前会话: {active_name}",
        f"  默认模型: {agent.settings.pi_model}",
    ]
    await reply_text(update, "\n".join(lines))


# ================= 普通消息 =================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    text = update.message.text or ""

    storage.ensure_user(user.id, user.username)
    logger.info("收到消息", extra={"user_id": user.id, "chat_id": update.effective_chat.id, "text_len": len(text)})

    try:
        await update.effective_chat.send_chat_action(ChatAction.TYPING)
    except Exception:
        pass  # typing 状态失败不阻断主流程

    try:
        await agent.ensure_client()
    except Exception as e:
        logger.error("pi 进程启动失败", extra={"err": str(e)[:300]})
        await reply_text(update, f"⚠️ pi 进程启动失败: {e}")
        return

    try:
        async def gen():
            async for delta in agent.client.prompt(text):
                yield delta

        await stream_reply(update, gen(), settings.stream_edit_interval)
    except PiBusyError:
        await reply_text(update, "⏳ 我正在处理上一条消息，请稍等…")
    except PiError as e:
        await reply_text(update, f"⚠️ {e}")
    except Exception as e:
        logger.error("处理消息异常", extra={"user_id": user.id, "err": str(e)[:500]})
        await reply_text(update, f"⚠️ 处理出错: {e}")
    try:
        storage.touch(agent.storage.get_active_session(user.id)["id"])
    except Exception:
        pass


# ================= Application 组装 =================

def build_application(settings: Settings, storage: Storage, manager: AgentManager) -> Application:
    # 放宽网络超时：服务器到 api.telegram.org 偶发慢连接，默认 5s 太短
    app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["storage"] = storage
    app.bot_data["manager"] = manager

    # 统一鉴权包装
    async def auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE, inner) -> None:
        if not authorized(update, settings):
            logger.warning(
                "拒绝未授权用户",
                extra={"user_id": update.effective_user.id if update.effective_user else None},
            )
            if update.message and update.message.chat.type == "private":
                await update.message.reply_text("你没有使用该 bot 的权限。")
            return
        await inner(update, context)

    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("env", cmd_env),
        CommandHandler("session", cmd_session),
        CommandHandler("model", cmd_model),
        CommandHandler("stop", cmd_stop),
        CommandHandler("status", cmd_status),
    ]
    for h in handlers:
        inner = h.callback
        h.callback = lambda u, c, inner=inner: auth_guard(u, c, inner)
        app.add_handler(h)

    # 未知命令提示
    app.add_handler(MessageHandler(filters.COMMAND, lambda u, c: reply_text(u, "未知命令，/help 查看帮助")))
    # 普通文本
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
