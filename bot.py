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
import re
import time
from html import escape

import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

MAX_HTML_LEN = 3900  # Telegram 消息上限 4096，转义/标签会膨胀，留余量


# ================= Markdown → Telegram HTML =================


# Telegram 语法高亮支持的语言（白名单，超出则退化为无高亮 <pre>）
_PRE_LANGS = {
    "python", "py", "javascript", "js", "typescript", "ts", "java", "c", "cpp", "c++",
    "csharp", "cs", "go", "rust", "rs", "ruby", "rb", "php", "swift", "kotlin", "kt",
    "scala", "r", "dart", "lua", "perl", "haskell", "hs", "sql", "json", "yaml", "yml",
    "toml", "xml", "html", "css", "scss", "bash", "sh", "shell", "zsh", "powershell",
    "ps1", "dockerfile", "docker", "makefile", "cmake", "ini", "diff", "markdown", "md",
    "latex", "tex", "graphql", "proto", "protobuf", "regex", "vim", "elixir", "erlang",
    "clojure", "groovy", "julia", "nim", "ocaml", "pascal", "fortran", "objectivec",
}


def md_to_html(text: str) -> str:
    """标准 Markdown → Telegram HTML parse_mode 子集。

    转换：粗体、斜体、删除线、行内代码、围栏代码块、表格（等宽块）、标题（转粗体）、
    链接、引用块、无序列表（含嵌套缩进）、分隔线。
    粗体/斜体/标题/链接等片段经 stash 暂存、最后统一恢复，避免与残余转义重复转义
    （修复双重转义：**a & b** 曾被转成 &amp;amp;）。
    流式半截文本可能产生非法 HTML，调用方需对 BadRequest 降级纯文本重试。
    """
    if not text:
        return text

    sentinel: list[str] = []  # 先暂存已渲染的 HTML 片段，最后恢复

    def stash(html: str) -> str:
        sentinel.append(html)
        return f"\x00S{len(sentinel) - 1}\x00"

    # 1) 围栏代码块 ```lang\n...``` → <pre language="lang">…</pre>（语言白名单，Telegram 语法高亮）
    def _fence(m: re.Match) -> str:
        body = m.group(2).rstrip("\n")
        lang = m.group(1).strip().lower()
        if lang in _PRE_LANGS:
            return stash(f'<pre language="{lang}">{escape(body, quote=False)}</pre>')
        return stash(f"<pre>{escape(body, quote=False)}</pre>")

    text = re.sub(r"```(\w*)[ \t]*\n(.*?)```", _fence, text, flags=re.S)

    # 2) 引用块 > 行 → <blockquote>…</blockquote>（内容递归渲染，支持嵌套）
    def _quote(m: re.Match) -> str:
        lines = m.group(0).rstrip("\n").splitlines()
        inner = "\n".join(re.sub(r"^[ \t]*>\s?", "", ln) for ln in lines)
        return stash(f"<blockquote>{md_to_html(inner)}</blockquote>")

    text = re.sub(r"(?m)(^[ \t]*>[^\n]*\n?)+", _quote, text)

    # 3) 行内代码 `x` → <code>x</code>
    def _inline(m: re.Match) -> str:
        return stash(f"<code>{escape(m.group(1), quote=False)}</code>")

    text = re.sub(r"`([^`\n]+)`", _inline, text)

    # 4) 粗体 **x** → <b>x</b>（先于斜体处理，避免 * 冲突）
    def _bold(m: re.Match) -> str:
        return stash(f"<b>{escape(m.group(1))}</b>")

    text = re.sub(r"\*\*([^*\n]+)\*\*", _bold, text)

    # 5) 斜体 *x* → <i>x</i>（排除 ** 残留；内容两端非空白，避免误判乘法）
    def _italic(m: re.Match) -> str:
        return stash(f"<i>{escape(m.group(1))}</i>")

    text = re.sub(r"(?<!\*)\*([^\s*](?:[^*\n]*[^\s*])?)\*(?!\*)", _italic, text)

    # 6) 删除线 ~~x~~ → <s>x</s>（GFM 双波浪，避免与 ~ 路径/home 误判）
    def _strike(m: re.Match) -> str:
        return stash(f"<s>{escape(m.group(1))}</s>")

    text = re.sub(r"~~([^~\n]+?)~~", _strike, text)

    # 7) 标题 # x → <b>x</b>（Telegram 无标题语义）
    def _heading(m: re.Match) -> str:
        return stash(f"<b>{escape(m.group(2))}</b>")

    text = re.sub(r"(?m)^\s{0,3}(#{1,6})\s+(.+?)\s*$", _heading, text)

    # 8) 表格 → <pre> 等宽块（去掉 | 分隔行，列对齐）
    def _table(m: re.Match) -> str:
        lines = m.group(0).rstrip("\n").splitlines()
        rows = [ln for ln in lines if not re.fullmatch(r"[\s|:\-]+", ln)]
        if not rows:
            return ""
        cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in rows]
        max_cols = max(len(r) for r in cells)
        widths = [max(len(r[i]) for r in cells if i < len(r)) for i in range(max_cols)]
        out = []
        for r in cells:
            line = "  ".join(
                (c.ljust(widths[i]) if i < len(widths) else c) for i, c in enumerate(r)
            )
            out.append(line.rstrip())
        return stash(f"<pre>{escape(chr(10).join(out))}</pre>")

    text = re.sub(r"(?m)(^\s*\|.*\|\s*$\n?){2,}", _table, text)

    # 9) 链接 [text](url) → <a href="url">text</a>（仅 http/https/tg 协议）
    def _link(m: re.Match) -> str:
        url, label = m.group(2), m.group(1)
        return stash(f'<a href="{escape(url, quote=False)}">{escape(label)}</a>')

    text = re.sub(r"\[([^\[\]\n]+)\]\((https?://[^)\s]+|tg://[^)\s]+)\)", _link, text)

    # 10) 分隔线 --- → 空行
    text = re.sub(r"(?m)^\s*---+\s*$", "", text)

    # 11) 无序列表：- / * → • bullet；复选框 → ✅/⬜；保留嵌套缩进（全角空格防折叠）
    def _indent(prefix: str) -> str:
        return "　" * (len(prefix.expandtabs(2)) // 2)

    text = re.sub(
        r"(?m)^([ \t]*)[-*][ \t]+\[[xX]\][ \t]+",
        lambda m: _indent(m.group(1)) + "• ✅ ",
        text,
    )
    text = re.sub(
        r"(?m)^([ \t]*)[-*][ \t]+\[ \][ \t]+",
        lambda m: _indent(m.group(1)) + "• ⬜ ",
        text,
    )
    text = re.sub(
        r"(?m)^([ \t]*)[-*][ \t]+",
        lambda m: _indent(m.group(1)) + "• ",
        text,
    )

    # 12) 残余纯文本转义：仅保留 Telegram 合法标签，其余 <...> 一律转义
    def _escape_residual(s: str) -> str:
        tag_re = re.compile(
            r"(</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|br|blockquote)(?:\s[^<>]*)?/?>)"
        )
        parts = tag_re.split(s)
        return "".join(
            p if tag_re.fullmatch(p) else escape(p, quote=False) for p in parts
        )

    text = _escape_residual(text)

    # 13) 恢复暂存的代码/表格/粗体等片段（可能嵌套，需迭代到无残留：
    #     表格/粗体可能包裹行内代码的占位符，单遍顺序替换会漏掉内层）
    while True:
        prev = text
        for i, html in enumerate(sentinel):
            text = text.replace(f"\x00S{i}\x00", html)
        if text == prev:
            break
    return text


# Telegram API 网络类异常（服务器→TG 偶发抖动，AGENTS.md 已记载）：重试而非中断
_TG_NET_EXC = (httpx.TransportError, asyncio.TimeoutError, TimeoutError)


def _split_markdown_blocks(text: str) -> list[str]:
    """把 markdown 切成原子块：普通单行，或完整围栏代码块（```…``` 不切开）。"""
    blocks: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            block = [lines[i]]
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < len(lines):  # 闭合 fence
                block.append(lines[i])
                i += 1
            blocks.append("\n".join(block))
        else:
            blocks.append(lines[i])
            i += 1
    return blocks


def split_markdown(text: str, max_html_len: int = MAX_HTML_LEN) -> list[str]:
    """按块切 markdown：每段 md_to_html 后 ≤ max_html_len，且不切断代码块。

    切段基于「实际 HTML 长度」而非原始字符数，避免转义膨胀（< → &lt; 等）导致超限降级。
    单个超长代码块（>max_html_len）无法再切，作为独立一段交由 _send 降级兜底。
    """
    if not text:
        return [text]
    blocks = _split_markdown_blocks(text)
    chunks: list[str] = []
    cur: list[str] = []
    for block in blocks:
        cur.append(block)
        if len(cur) > 1 and len(md_to_html("\n".join(cur))) > max_html_len:
            chunks.append("\n".join(cur[:-1]) + "\n")  # 补回段末换行
            cur = [block]
    if cur:
        chunks.append("\n".join(cur))
    # 去掉空白段
    chunks = [c for c in chunks if c.strip()]
    return chunks or [text]


def strip_markdown(text: str) -> str:
    """降级纯文本时剥离常见 markdown 标记，避免用户看到 ** 和 ` 等原始符号。"""
    if not text:
        return text
    # 围栏代码块标记
    text = re.sub(r"```\w*[ \t]*\n?", "", text)
    # 粗体 / 斜体 / 删除线
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^\s*](?:[^*\n]*[^\s*])?)\*(?!\*)", r"\1", text)
    text = re.sub(r"~~([^~\n]+?)~~", r"\1", text)
    # 行内代码
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # 链接 [text](url) → text
    text = re.sub(r"\[([^\[\]\n]+)\]\((?:https?://|tg://)[^)\s]+\)", r"\1", text)
    # 标题标记
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    # 引用标记
    text = re.sub(r"(?m)^[ \t]*>\s?", "", text)
    # 无序列表标记
    text = re.sub(r"(?m)^([ \t]*)[-*][ \t]+", r"\1• ", text)
    # 复选框
    text = re.sub(r"• \[[xX]\]", "• ✅", text)
    text = re.sub(r"• \[ \]", "• ⬜", text)
    # 表格分隔行与竖线
    text = re.sub(r"(?m)^[\s|:\-]+\n", "", text)
    text = text.replace("|", " ")
    return text


# ================= 工具活动 / 错误友好化 =================

_TOOL_ICONS = {
    "read": "🔍 读取",
    "bash": "⚙️ 执行",
    "edit": "✏️ 编辑",
    "write": "📝 写入",
    "grep": "🔎 搜索",
    "find": "🗂 查找",
    "ls": "📂 列出",
}


def _tool_activity(tool_name: str, phase: str, args: dict | None) -> str | None:
    """工具执行事件 → 友好活动描述；phase == "end" 返回 None（表示活动结束）。"""
    if phase == "end":
        return None
    args = args or {}
    target = ""
    for key in ("path", "file", "filePath", "command", "pattern", "query"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            target = v.strip()
            break
    if not target:
        for v in args.values():
            if isinstance(v, str) and v.strip():
                target = v.strip()
                break
    label = _TOOL_ICONS.get(tool_name, f"🔧 {tool_name or '工具'}")
    if target:
        return f"{label}: {target[:60]}"
    return label


def _friendly_error(e: Exception) -> str:
    """把内部异常映射成用户可读的友好文案。"""
    msg = str(e)
    if "超时" in msg or "timeout" in msg.lower():
        return "⏱️ 处理超时，已中止当前任务，可重新发送"
    if "熔断" in msg or "频繁崩溃" in msg:
        return "⚠️ 推理服务暂时不可用（频繁崩溃已熔断），请稍后重试"
    if "未运行" in msg or "未就绪" in msg or "未启动" in msg:
        return "⚠️ 推理服务未就绪，请稍后重试"
    return f"⚠️ {msg}"


def _render(text: str, activity_holder: dict | None) -> str:
    """正文 + 当前工具活动行（活动行附在末尾）。"""
    if activity_holder and activity_holder.get("current"):
        return f"{text}\n\n{activity_holder['current']}"
    return text


async def _auto_name_session(storage: Storage, user_id: int, text: str) -> None:
    """用第一条消息给「默认会话」自动命名，便于 /session list 辨认。"""
    active = storage.get_active_session(user_id)
    if active and active["name"] == "默认会话" and text.strip():
        short = text.strip().replace("\n", " ")[:20]
        if short:
            storage.rename_session(active["id"], short)


async def _send(
    send_fn,
    text: str,
    *,
    edit: bool = False,
    msg=None,
    reply_markup=None,
):
    """发送/编辑消息：优先 HTML 渲染；网络异常重试 3 次；BadRequest 降级纯文本。

    返回：编辑成功 → True；编辑失败 → False；发送成功 → Message；发送失败 → None。
    """
    html = md_to_html(text)
    for attempt in range(3):
        try:
            if edit:
                await msg.edit_text(html, parse_mode=ParseMode.HTML)
                return True
            return await send_fn(html, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                # 内容未变化（流式中途停顿），无需更新，视为成功
                return True if edit else None
            logger.warning("HTML 渲染失败，降级纯文本", extra={"err": str(e)[:200]})
            try:
                if edit:
                    await msg.edit_text(strip_markdown(text))
                    return True
                return await send_fn(strip_markdown(text), reply_markup=reply_markup)
            except Exception:
                return True if edit else None
        except _TG_NET_EXC as e:
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("Telegram API 网络异常，重试耗尽", extra={"err": str(e)[:200]})
            return True if edit else None
        except Exception:
            return True if edit else None


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


async def reply_text(update: Update, text: str, reply_markup=None) -> None:
    """按 Telegram 上限分段回复（基于 HTML 长度、不切断代码块）；reply_markup 仅首段。"""
    if not text:
        text = "(无内容)"
    chunks = split_markdown(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == 0 else None
        if update.callback_query is not None:
            await _send(update.callback_query.message.reply_text, chunk, reply_markup=markup)
        elif update.message is not None:
            await _send(update.message.reply_text, chunk, reply_markup=markup)
        else:
            await _send(update.effective_chat.send_message, chunk, reply_markup=markup)


async def stream_reply(
    update: Update,
    text_iter,
    interval: float,
    placeholder: dict | None = None,
    activity_holder: dict | None = None,
    initial_delay: float = 0.25,
    initial_chars: int = 16,
) -> None:
    """流式回复：合并增量后定时编辑，按 HTML 长度分段，不切断代码块。

    placeholder：{"msg": Message|None}，思考占位消息 holder（流式期间异步产生，动态读取）。
    activity_holder：当前工具活动状态（{"current": 描述}），渲染时附加到正文末尾。

    delta 生产者只追加文本并标记 dirty；渲染循环在到达刷新截止时间后才执行
    Markdown 解析和 Telegram API 编辑。首批文本等待短暂窗口或达到字符阈值，
    避免先显示一两个字再大段跳变。
    """
    target = update.message or update.callback_query.message
    msg = None
    buf = ""
    dirty = False
    done = False
    producer_error: Exception | None = None
    first_delta_at: float | None = None
    first_text_rendered = False
    last_edit = 0.0

    events: asyncio.Queue[tuple[str, object | None]] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for delta in text_iter:
                events.put_nowait(("delta", delta))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            events.put_nowait(("error", e))
        finally:
            events.put_nowait(("done", None))

    producer = asyncio.create_task(produce())

    def consume_event(kind: str, value: object | None) -> None:
        nonlocal buf, dirty, done, producer_error, first_delta_at
        if kind == "delta":
            if first_delta_at is None:
                first_delta_at = time.monotonic()
            buf += str(value or "")
            dirty = True
        elif kind == "error" and isinstance(value, Exception):
            producer_error = value
        elif kind == "done":
            done = True

    async def flush(*, force: bool = False) -> None:
        """把当前 dirty 快照刷新到 Telegram；仅此处执行 Markdown 分段。"""
        nonlocal msg, buf, dirty, first_text_rendered, last_edit
        if (not dirty and not force) or not buf:
            return

        # 思考占位若已发出，直接编辑它，避免多发一条消息。
        if msg is None and placeholder is not None and placeholder.get("msg") is not None:
            msg = placeholder["msg"]

        chunks = split_markdown(buf)
        if len(chunks) > 1:
            # 已闭合的段落地后从 buf 移除，后续只重新解析未完成的尾段。
            for chunk in chunks[:-1]:
                rendered = _render(chunk, activity_holder)
                if msg is None:
                    msg = await _send(target.reply_text, rendered)
                else:
                    await _send(None, rendered, edit=True, msg=msg)
                msg = await _send(target.reply_text, "…")
            buf = chunks[-1]

        rendered = _render(buf, activity_holder)
        if msg is None:
            msg = await _send(target.reply_text, rendered)
        else:
            await _send(None, rendered, edit=True, msg=msg)

        dirty = False
        first_text_rendered = True
        last_edit = time.monotonic()

    try:
        while not done:
            now = time.monotonic()
            timeout: float | None = None

            if dirty:
                if not first_text_rendered:
                    initial_ready = len(buf) >= max(1, initial_chars) or "\n" in buf
                    if initial_ready:
                        await flush()
                        continue
                    deadline = (first_delta_at or now) + max(0.0, initial_delay)
                else:
                    deadline = last_edit + max(0.0, interval)

                timeout = max(0.0, deadline - now)
                if timeout == 0.0:
                    await flush()
                    continue

            try:
                if timeout is None:
                    kind, value = await events.get()
                else:
                    kind, value = await asyncio.wait_for(events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await flush()
                continue

            consume_event(kind, value)
            # 合并已到达的 delta，避免事件循环自身成为每 token 开销。
            while not events.empty():
                queued_kind, queued_value = events.get_nowait()
                consume_event(queued_kind, queued_value)

        # 回合结束时立即刷新 dirty 内容，不等待下一个定时周期。
        await flush()

        if producer_error is not None:
            logger.error("流式回复异常", extra={"err": str(producer_error)[:200]})
            interrupted = f"{buf}\n\n[输出中断: {producer_error}]" if buf else f"[输出中断: {producer_error}]"
            if msg is not None:
                await _send(None, interrupted, edit=True, msg=msg)
            else:
                await _send(target.reply_text, interrupted)
        elif not buf:
            # 工具型回合可能没有文本；若已发思考占位，不要将它永久留在聊天中。
            if msg is None and placeholder is not None:
                msg = placeholder.get("msg")
            if msg is not None:
                await _send(None, "(无输出)", edit=True, msg=msg)
            else:
                await _send(target.reply_text, "(无输出)")
    except Exception as e:
        logger.error("流式回复异常", extra={"err": str(e)[:200]})
        try:
            if msg is not None:
                await _send(None, f"{buf}\n\n[输出中断: {e}]", edit=True, msg=msg)
            elif buf:
                await _send(target.reply_text, f"{buf}\n\n[输出中断: {e}]")
        except Exception:
            pass
    finally:
        if not producer.done():
            producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass


# ================= 快捷菜单 =================

def quick_menu() -> InlineKeyboardMarkup:
    """/start 与 /help 附带的内联快捷键盘。"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📁 会话列表", callback_data="cmd:session_list"),
                InlineKeyboardButton("🧠 模型列表", callback_data="cmd:model_list"),
            ],
            [
                InlineKeyboardButton("📊 状态", callback_data="cmd:status"),
                InlineKeyboardButton("⏹ 中止任务", callback_data="cmd:stop"),
            ],
        ]
    )


# ================= 命令处理器 =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 djhx-pi-bot — 通过 Telegram 控制 pi 编码代理\n\n"
        "直接发消息即可与 pi 对话，或点击下方快捷按钮。\n\n"
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
    await reply_text(update, text, reply_markup=quick_menu())


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
        buttons = []
        for r in rows:
            marker = "▶ " if r["status"] == "active" else ""
            buttons.append([InlineKeyboardButton(f"{marker}{r['name']}", callback_data=f"session_use:{r['id']}")])
        await reply_text(update, "📁 会话列表（点击切换）：", reply_markup=InlineKeyboardMarkup(buttons))

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
            buttons = []
            for m in models:
                images = "🖼 " if "image" in (m.get("input") or []) else ""
                buttons.append([InlineKeyboardButton(f"{images}{m['id']}", callback_data=f"model_use:{m['id']}")])
            await reply_text(update, "🧠 可用模型（点击切换）：", reply_markup=InlineKeyboardMarkup(buttons))
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
    # 追加 pi 会话详情（消息数 / 压缩状态），拿不到不阻塞主状态展示
    try:
        state = await agent.client.get_state()
        mc = state.get("messageCount")
        if mc is not None:
            pending = state.get("pendingMessageCount") or 0
            lines.append(f"  消息数: {mc}" + (f" | 待处理: {pending}" if pending else ""))
        if state.get("isCompacting"):
            lines.append("  ⚠️ 上下文压缩中…")
        sid = state.get("sessionId")
        if sid:
            lines.append(f"  session: {sid[:12]}")
    except Exception:
        pass
    await reply_text(update, "\n".join(lines))


# ================= 快捷按钮回调 =================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """内联键盘按钮点击：映射到对应命令处理器。"""
    query = update.callback_query
    await query.answer()
    actions = {
        "cmd:session_list": (cmd_session, ["list"]),
        "cmd:model_list": (cmd_model, ["list"]),
        "cmd:status": (cmd_status, []),
        "cmd:stop": (cmd_stop, []),
    }
    data = query.data or ""
    # 列表按钮直接触发切换操作
    if data.startswith("session_use:"):
        context.args = ["use", data.split(":", 1)[1]]
        await cmd_session(update, context)
        return
    if data.startswith("model_use:"):
        context.args = ["use", data.split(":", 1)[1]]
        await cmd_model(update, context)
        return
    entry = actions.get(data)
    if entry is None:
        await query.answer("未知操作", show_alert=False)
        return
    handler, args = entry
    context.args = args
    await handler(update, context)


# ================= 普通消息 =================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    text = update.message.text or ""
    chat = update.effective_chat

    storage.ensure_user(user.id, user.username)
    logger.info("收到消息", extra={"user_id": user.id, "chat_id": chat.id, "text_len": len(text)})

    # typing 持续刷新：Telegram 的 typing 约 5 秒过期，深度思考期长时保持反馈
    stop_typing = asyncio.Event()

    async def typing_keepalive() -> None:
        while not stop_typing.is_set():
            try:
                await chat.send_chat_action(ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_typing.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    keepalive = asyncio.create_task(typing_keepalive())

    try:
        try:
            await agent.ensure_client()
        except Exception as e:
            logger.error("pi 进程启动失败", extra={"err": str(e)[:300]})
            await reply_text(update, f"⚠️ pi 进程启动失败: {_friendly_error(e)}")
            return

        # 会话自动命名：默认会话的第一条消息作为会话名
        await _auto_name_session(storage, user.id, text)

        # 流式中途介入：pi 忙时插入 steer 而非拒绝
        if agent.client.busy:
            try:
                await agent.client.steer(text)
                await reply_text(update, "↩️ 已插入新指令，当前任务完成后将按新方向继续")
            except Exception as e:
                await reply_text(update, f"⚠️ 插入指令失败: {_friendly_error(e)}")
            return

        # 思考占位 + 工具活动状态
        placeholder = {"msg": None}
        activity = {"current": None}

        async def on_thinking() -> None:
            if placeholder["msg"] is None:
                placeholder["msg"] = await _send(update.message.reply_text, "🤔 深度思考中…")

        def on_tool(tool_name: str, phase: str, args: dict | None) -> None:
            activity["current"] = _tool_activity(tool_name, phase, args)

        async def gen():
            async for delta in agent.client.prompt(
                text,
                on_thinking=on_thinking,
                on_tool=on_tool,
            ):
                yield delta

        await stream_reply(
            update, gen(), settings.stream_edit_interval,
            placeholder=placeholder, activity_holder=activity,
            initial_delay=settings.stream_initial_delay,
            initial_chars=settings.stream_initial_chars,
        )
    except PiBusyError:
        await reply_text(update, "⏳ 我正在处理上一条消息，请稍等…")
    except PiError as e:
        await reply_text(update, _friendly_error(e))
    except Exception as e:
        logger.error("处理消息异常", extra={"user_id": user.id, "err": str(e)[:500]})
        await reply_text(update, f"⚠️ 处理出错: {_friendly_error(e)}")
    finally:
        stop_typing.set()
        keepalive.cancel()
        try:
            await keepalive
        except (asyncio.CancelledError, Exception):
            pass
    try:
        storage.touch(agent.storage.get_active_session(user.id)["id"])
    except Exception:
        pass


# ================= Application 组装 =================

COMMAND_MENU = [
    ("start", "开始使用"),
    ("help", "帮助"),
    ("env", "环境管理"),
    ("session", "会话管理"),
    ("model", "模型管理"),
    ("status", "运行状态"),
    ("stop", "中止任务"),
]


async def register_command_menu(bot) -> None:
    """把命令注册到 Telegram：用户输入 / 即弹出补全菜单（幂等，可重复调用）。"""
    try:
        await bot.set_my_commands(COMMAND_MENU)
        logger.info("命令菜单已注册")
    except Exception as e:
        logger.warning("命令菜单注册失败", extra={"err": str(e)[:300]})


def build_application(settings: Settings, storage: Storage, manager: AgentManager) -> Application:
    async def post_init(application: Application) -> None:
        """run_polling / run_webhook 模式下的启动钩子（当前 app.py 手动启动，另行调用）。"""
        await register_command_menu(application.bot)

    # 放宽网络超时：服务器到 api.telegram.org 偶发慢连接，默认 5s 太短
    app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .post_init(post_init)
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
            if update.callback_query is not None:
                await update.callback_query.answer("你没有权限", show_alert=True)
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
        CallbackQueryHandler(on_callback),
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
