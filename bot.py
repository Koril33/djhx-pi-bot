"""Telegram 业务层：python-telegram-bot 处理器 + Agent 会话编排。

V1 拓扑：单个环境（服务器本机），每用户一个常驻 pi RPC 进程。
命令面：
  /help /start /status
  /env list /env use <name>          （V1 仅 server 本机）
  /session new [name] /session list /session use <id>
  /model list /model use <model_id>
  /stop
普通文本与单张图片：转发给 pi（常驻 RPC，流式回复）。
"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from costs import PricingCatalog, format_usd
from memory import MemoryService, looks_secret, safe_turn_text
from persona import persona_prompt_path, render_persona, write_prompt
from logging_conf import get_logger
from pi_bridge import PiBusyError, PiError, PiRpcClient
from storage import Storage

logger = get_logger("bot")

MAX_HTML_LEN = 3900  # Telegram 消息上限 4096，转义/标签会膨胀，留余量
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192  # DeepSeek 单张图片的长/宽上限


class _ImageTooLarge(ValueError):
    """仅用于区分本地校验错误与下载库抛出的异常。"""


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

    转换：粗体、斜体、删除线、行内代码、围栏代码块、表格（逐项列表）、标题（转粗体）、
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

    # 表格在行内 Markdown 处理前转成窄屏友好的逐项列表；围栏代码已被暂存，不会误改。
    def _table(m: re.Match) -> str:
        lines = m.group(0).rstrip("\n").splitlines()
        rows = [ln for ln in lines if not re.fullmatch(r"[\s|:\-]+", ln)]
        if len(rows) < 2:
            return m.group(0)
        cells = [[cell.strip() for cell in ln.strip().strip("|").split("|")] for ln in rows]
        headers = cells[0]
        indexed = headers[0].casefold() in {"#", "序号", "编号", "no", "no.", "id"}
        first_field = 1 if indexed else 0
        blocks = []
        for ordinal, row in enumerate(cells[1:], 1):
            if not any(row):
                continue
            number = row[0] if indexed and row[0] else str(ordinal)
            title = row[first_field] if first_field < len(row) else "条目"
            title = re.sub(r"[`*_~]", "", title)[:36]
            if first_field < len(row) and len(row[first_field]) > 36:
                title += "…"
            fields = [f"**{number}. {title}**"]
            for index in range(first_field, len(row)):
                if row[index]:
                    label = headers[index] if index < len(headers) and headers[index] else f"第 {index + 1} 列"
                    fields.append(f"- {label}：{row[index]}")
            blocks.append("\n".join(fields))
        return "\n\n".join(blocks) + "\n" if blocks else m.group(0)

    text = re.sub(r"(?m)(^[ \t]*\|.*\|[ \t]*\n?){2,}", _table, text)

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

    # 8) 链接 [text](url) → <a href="url">text</a>（仅 http/https/tg 协议）
    def _link(m: re.Match) -> str:
        url, label = m.group(2), m.group(1)
        return stash(f'<a href="{escape(url, quote=False)}">{escape(label)}</a>')

    text = re.sub(r"\[([^\[\]\n]+)\]\((https?://[^)\s]+|tg://[^)\s]+)\)", _link, text)

    # 9) 分隔线 --- → 空行
    text = re.sub(r"(?m)^\s*---+\s*$", "", text)

    # 10) 无序列表：- / * → • bullet；复选框 → ✅/⬜；保留嵌套缩进（全角空格防折叠）
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

    # 11) 残余纯文本转义：仅保留 Telegram 合法标签，其余 <...> 一律转义
    def _escape_residual(s: str) -> str:
        tag_re = re.compile(
            r"(</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|br|blockquote)(?:\s[^<>]*)?/?>)"
        )
        parts = tag_re.split(s)
        return "".join(
            p if tag_re.fullmatch(p) else escape(p, quote=False) for p in parts
        )

    text = _escape_residual(text)

    # 12) 恢复暂存的代码/粗体等片段（可能嵌套，需迭代到无残留）
    while True:
        prev = text
        for i, html in enumerate(sentinel):
            text = text.replace(f"\x00S{i}\x00", html)
        if text == prev:
            break
    return text


# Telegram API 网络类异常（服务器→TG 偶发抖动，AGENTS.md 已记载）：重试而非中断
_TG_NET_EXC = (NetworkError, httpx.TransportError, asyncio.TimeoutError, TimeoutError)


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
    """按渲染后的 HTML 长度分段；超长单行和代码块也必须拆开。"""
    if not text:
        return [text]

    def fits(part: str) -> bool:
        return (
            len(md_to_html(part)) <= max_html_len
            and len(part.encode("utf-16-le")) // 2 <= 4096
        )

    def split_long_line(line: str, prefix: str = "", suffix: str = "") -> list[str]:
        """将单行拆成可发送片段，代码块可通过 prefix/suffix 逐段闭合。"""
        parts = []
        while line:
            low, high = 1, len(line)
            best = 0
            while low <= high:
                mid = (low + high) // 2
                if fits(prefix + line[:mid] + suffix):
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            if not best:
                raise ValueError("Telegram 消息长度上限不足以容纳一个字符")
            parts.append(prefix + line[:best] + suffix)
            line = line[best:]
        return parts

    blocks: list[str] = []
    for block in _split_markdown_blocks(text):
        if fits(block):
            blocks.append(block)
            continue
        if block.lstrip().startswith("```") and "\n" in block:
            opening, _, rest = block.partition("\n")
            body = rest.rsplit("```", 1)[0] if rest.rstrip().endswith("```") else rest
            # 重新闭合每一段，确保 Telegram HTML 的 <pre> 标签合法。
            prefix, suffix = opening + "\n", "\n```"
            if fits(prefix + "x" + suffix):
                pending: str | None = None
                for line in body.split("\n"):
                    candidate = pending + "\n" + line if pending is not None else line
                    if fits(prefix + candidate + suffix):
                        pending = candidate
                        continue
                    if pending is not None:
                        blocks.append(prefix + pending + suffix)
                        pending = None
                    if fits(prefix + line + suffix):
                        pending = line
                    else:
                        blocks.extend(split_long_line(line, prefix, suffix))
                if pending is not None:
                    blocks.append(prefix + pending + suffix)
                continue
        for line in block.split("\n"):
            blocks.extend(split_long_line(line) if line else [""])

    chunks: list[str] = []
    cur: list[str] = []
    for block in blocks:
        cur.append(block)
        if len(cur) > 1 and not fits("\n".join(cur)):
            chunks.append("\n".join(cur[:-1]))
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
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)) or "超时" in msg or "timeout" in msg.lower():
        return "⏱️ 处理超时，请重新发送；如持续出现请联系管理员"
    if "熔断" in msg or "频繁崩溃" in msg:
        return "⚠️ 推理服务暂时不可用（频繁崩溃已熔断），请稍后重试"
    if "未运行" in msg or "未就绪" in msg or "未启动" in msg:
        return "⚠️ 推理服务未就绪，请稍后重试"
    return f"⚠️ {msg}" if msg else "⚠️ 处理失败，请稍后重试"


def _render(text: str, activity_holder: dict | None) -> str:
    """正文 + 当前工具活动行（活动行附在末尾）。"""
    if activity_holder and activity_holder.get("current"):
        return f"{text}\n\n{activity_holder['current']}" if text else activity_holder["current"]
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
    """发送/编辑消息；格式错误降级纯文本，网络错误重试，失败向调用者抛出。"""
    html = md_to_html(text)
    plain = strip_markdown(text)
    use_html = True
    for attempt in range(3):
        try:
            if edit:
                if use_html:
                    await msg.edit_text(html, parse_mode=ParseMode.HTML)
                else:
                    await msg.edit_text(plain)
                return True
            if use_html:
                return await send_fn(html, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            return await send_fn(plain, reply_markup=reply_markup)
        except BadRequest as e:
            if edit and "Message is not modified" in str(e):
                # 内容未变化（流式中途停顿），无需更新，视为成功
                return True
            if not use_html:
                logger.error("Telegram 纯文本发送失败", extra={"err": str(e)[:300], "edit": edit})
                raise
            logger.warning("HTML 渲染失败，降级纯文本", extra={"err": str(e)[:200]})
            use_html = False
        except RetryAfter as e:
            if attempt == 2:
                logger.error("Telegram 限流重试耗尽", extra={"err": str(e)[:300], "edit": edit})
                raise
            delay = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
            await asyncio.sleep(max(0.5, delay))
        except _TG_NET_EXC as e:
            if attempt == 2:
                logger.error("Telegram API 网络异常，重试耗尽", extra={"err": str(e)[:300], "edit": edit})
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception:
            logger.exception("Telegram 消息发送失败", extra={"edit": edit})
            raise
    raise RuntimeError("Telegram 消息发送失败：重试次数耗尽")


class AgentSession:
    """单个用户在单个环境下的 pi 会话。"""

    def __init__(self, user_id: int, settings: Settings, storage: Storage,
                 memory: MemoryService | None = None):
        self.user_id = user_id
        self.settings = settings
        self.storage = storage
        self.prompt_pending = False
        self.active_turn_id: int | None = None
        self.recovery_notice: str | None = None
        self.control_lock = asyncio.Lock()
        self.memory = memory
        memory_enabled = getattr(settings, "memory_enabled", False) and memory is not None
        if memory_enabled:
            memory.clear_context(user_id)
        self.persona_file = persona_prompt_path(storage.db_path, user_id)
        self._persona_digest = write_prompt(
            self.persona_file, render_persona(storage.get_active_persona(user_id))
        )
        extra_args = ["--append-system-prompt", str(self.persona_file)]
        if memory_enabled:
            extra_args += ["--extension", str(Path(__file__).resolve().parent / "pi_extensions" / "memory-context.ts")]
        project_dir = Path(__file__).resolve().parent
        extra_args += ["--extension", str(project_dir / "pi_extensions" / "mail-tool.ts")]
        extra_env = {
            "DJHX_MAIL_DB_PATH": str(Path(storage.db_path).resolve()),
            "DJHX_MAIL_PYTHON": sys.executable,
            "DJHX_MAIL_WORKDIR": str(project_dir),
        }
        if memory_enabled:
            extra_env["DJHX_MEMORY_CONTEXT_FILE"] = str(memory.context_path(user_id))
        self.client = PiRpcClient(
            pi_cmd=settings.pi_cmd,
            session_dir=settings.pi_session_dir,
            provider=settings.pi_provider,
            model=settings.pi_model,
            extra_args=extra_args,
            extra_env=extra_env,
        )

    async def ensure_default_session(self) -> None:
        """确保用户至少有一个 active 会话记录。"""
        if self.storage.get_active_session(self.user_id) is None:
            sid = self.storage.create_session(self.user_id, "默认会话")
            self.storage.set_active(sid)

    async def ensure_client(self) -> None:
        """确保 pi 进程在跑，且挂载正确的会话文件。"""
        await self.refresh_persona()
        await self.ensure_default_session()
        active = self.storage.get_active_session(self.user_id)
        session_file = active["pi_session_file"] if active else None
        if (self.memory is not None and active and session_file
                and not Path(session_file).is_file()
                and not self.client.has_live_session(session_file)):
            recovery = self.memory.build_recovery(active["id"])
            state = await self.client.reset_session()
            new_file = state.get("sessionFile")
            if not new_file:
                raise PiError("pi 未返回新会话文件，无法恢复缺失的历史")
            self.storage.set_session_recovery(active["id"], new_file, recovery)
            self.recovery_notice = ("旧会话文件不存在，已从 SQLite 摘要恢复主要内容；原始对话无法逐字恢复"
                                    if recovery else "旧会话文件不存在，已创建空白 pi 会话；SQLite 没有可用摘要")
            return
        # 进程仍存活但 stdout reader 已失效时，也必须按当前会话文件重启。
        await self.client.ensure_running(session_file)
        # 首次同步 pi 侧会话文件路径
        if active and not active["pi_session_file"]:
            try:
                state = await self.client.get_state()
                if state.get("sessionFile"):
                    self.storage.set_pi_session_file(active["id"], state["sessionFile"])
            except Exception as e:
                logger.warning("同步会话文件失败", extra={"err": str(e)})

    async def refresh_persona(self) -> None:
        """SQLite 人格变更后重启空闲 RPC，使下一轮读取新的追加提示词。"""
        text = render_persona(self.storage.get_active_persona(self.user_id))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == self._persona_digest:
            return
        if self.client.busy or self.prompt_pending:
            raise PiBusyError("当前任务仍在运行，暂不能切换人格")
        await self.client.stop()
        self._persona_digest = write_prompt(self.persona_file, text)


class AgentManager:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.memory = MemoryService(settings, storage) if getattr(settings, "memory_enabled", False) else None
        self._agents: dict[int, AgentSession] = {}

    def get(self, user_id: int) -> AgentSession:
        agent = self._agents.get(user_id)
        if agent is None:
            agent = AgentSession(user_id, self.settings, self.storage, self.memory)
            self._agents[user_id] = agent
        return agent

    async def shutdown(self) -> None:
        if self.memory is not None:
            await self.memory.shutdown()
        for agent in self._agents.values():
            try:
                await agent.client.stop()
            except Exception:
                pass


# ================= 工具函数 =================

def authorized(update: Update, settings: Settings) -> bool:
    if not settings.allowed_ids:
        return False
    return (
        update.effective_user is not None
        and update.effective_user.id in settings.allowed_ids
        and update.effective_chat is not None
        and update.effective_chat.type == "private"
    )


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


async def notify_interrupted_turns(update: Update, storage: Storage, user_id: int) -> None:
    """下一条普通消息到来时告知上次中断；发送成功后才清除通知。"""
    count = storage.pending_interruption_notice_count(user_id)
    if not count:
        return
    await reply_text(
        update,
        f"⚠️ 上次服务中断留下 {count} 个未完成回合，已标记为不完整，"
        "不会纳入 SQLite 摘要，也不会自动重放。旧 pi 会话可能仍保留部分内容；"
        "如需继续，请先确认工具执行结果，再重新发送原消息。",
    )
    storage.acknowledge_interruption_notice(user_id)


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
    if activity_holder is not None:
        activity_holder["notify"] = lambda: events.put_nowait(("activity", None))

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
        elif kind == "activity":
            if first_delta_at is None:
                first_delta_at = time.monotonic()
            dirty = True
        elif kind == "error" and isinstance(value, Exception):
            producer_error = value
        elif kind == "done":
            done = True

    async def flush(*, force: bool = False) -> None:
        """把当前 dirty 快照刷新到 Telegram；仅此处执行 Markdown 分段。"""
        nonlocal msg, buf, dirty, first_delta_at, first_text_rendered, last_edit
        if not dirty and not force:
            return
        if not buf and (not activity_holder or not activity_holder.get("current")) and not first_text_rendered:
            # 工具可能在首次刷新前已开始并结束。清除空刷新标记，
            # 否则截止时间一过会在 while 循环中无 await 地占满事件循环。
            dirty = False
            first_delta_at = None
            return

        # 思考占位若已发出，直接编辑它，避免多发一条消息。
        if msg is None and placeholder is not None and placeholder.get("msg") is not None:
            msg = placeholder["msg"]

        chunks = split_markdown(buf) if buf else []
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

        rendered = _render(buf, activity_holder) or "🤔 正在处理…"
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
                    initial_ready = (
                        len(buf) >= max(1, initial_chars)
                        or "\n" in buf
                        or bool(activity_holder and activity_holder.get("current"))
                    )
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
        if activity_holder is not None and activity_holder.get("current"):
            activity_holder["current"] = None
            dirty = True
        await flush()

        if producer_error is not None:
            logger.error(
                "流式回复异常",
                exc_info=(type(producer_error), producer_error, producer_error.__traceback__),
                extra={"err_type": type(producer_error).__name__, "err": str(producer_error)[:200]},
            )
            reason = _friendly_error(producer_error)
            interrupted = f"[输出中断：{reason}]"
            if msg is None and placeholder is not None:
                msg = placeholder.get("msg")
            if msg is not None and not buf:
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
        logger.exception("流式回复发送异常", extra={"err_type": type(e).__name__, "err": str(e)[:200]})
        if isinstance(e, (*_TG_NET_EXC, RetryAfter)):
            raise
        reason = _friendly_error(e)
        interrupted = f"[输出中断：{reason}]"
        try:
            if msg is not None and not buf:
                await _send(None, interrupted, edit=True, msg=msg)
            else:
                await _send(target.reply_text, interrupted)
        except Exception:
            logger.exception("流式错误提示发送失败")
            raise
    finally:
        if activity_holder is not None:
            activity_holder.pop("notify", None)
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
        "直接发文字或单张图片即可与 pi 对话；图片可附说明文字。\n"
        "图片支持 JPEG、PNG、GIF、WebP，最大 10 MiB；当前模型须支持图片输入。\n\n"
        "命令：\n"
        "/env list — 查看可用环境\n"
        "/env use <name> — 切换环境（V1 仅 server）\n"
        "/session new [名称] — 新建会话\n"
        "/session list — 列出我的会话\n"
        "/session use <id> — 切换到指定会话\n"
        "/model list — 列出可用模型\n"
        "/model use <model_id> — 切换模型\n"
        "/persona list — 列出人格\n"
        "/persona use <id> — 切换人格\n"
        "/cost [last|today] — 查看模型用量与估算费用\n"
        "/memory — 查看和管理长期记忆\n"
        "/summary — 查看或清空当前会话摘要\n"
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


async def create_user_session(agent: AgentSession, storage: Storage, user_id: int, name: str) -> str:
    """供 Telegram 和后台共用的建会话流程；失败时恢复旧 Pi 会话。"""
    if agent.client.busy or agent.prompt_pending:
        raise PiBusyError("当前任务仍在运行")
    await agent.ensure_client()
    previous = storage.get_active_session(user_id)
    previous_file = previous["pi_session_file"] if previous else None
    if not previous_file:
        previous_file = (await agent.client.get_state()).get("sessionFile")
        if previous and previous_file:
            storage.set_pi_session_file(previous["id"], previous_file)
    if not previous_file:
        raise PiError("无法确认当前 pi 会话文件，请稍后重试")
    try:
        await agent.client.new_session(name)
        state = await agent.client.get_state()
        session_file = state.get("sessionFile")
        if not session_file:
            raise PiError("pi 未返回新会话文件")
        return storage.create_session_with_file_and_activate(user_id, name, session_file)
    except Exception:
        try:
            await agent.client.switch_session(previous_file)
        except Exception:
            logger.exception("恢复旧 pi 会话失败", extra={"user_id": user_id})
            await agent.client.stop()
        raise


async def switch_user_session(agent: AgentSession, storage: Storage, user_id: int, sid: str) -> str | None:
    """供 Telegram 和后台共用的切换流程，含 JSONL 缺失恢复和失败回滚。"""
    if agent.client.busy or agent.prompt_pending:
        raise PiBusyError("当前任务仍在运行")
    row = storage.get_session(sid)
    if row is None or row["user_id"] != user_id:
        raise ValueError("会话不存在")
    await agent.ensure_client()
    # ensure_client 已处理当前会话的 JSONL 缺失；再次 reset 会把刚恢复的文件覆盖掉。
    previous = storage.get_active_session(user_id)
    if previous is not None and "id" in previous and previous["id"] == sid:
        notice = getattr(agent, "recovery_notice", None)
        agent.recovery_notice = None
        return notice
    row = storage.get_session(sid)
    memory_service = getattr(agent, "memory", None)
    missing_file = memory_service is not None and (
        not row["pi_session_file"] or (
            not Path(row["pi_session_file"]).is_file()
            and not agent.client.has_live_session(row["pi_session_file"])
        )
    )
    if not row["pi_session_file"] and memory_service is None:
        raise PiError("该会话没有可用的 pi 会话文件")
    previous_file = previous["pi_session_file"] if previous else None
    switch_attempted = False
    try:
        switch_attempted = True
        if missing_file:
            recovery = memory_service.build_recovery(sid)
            state = await agent.client.reset_session()
            new_file = state.get("sessionFile")
            if not new_file:
                raise PiError("pi 未返回新会话文件")
            storage.set_session_recovery(sid, new_file, recovery)
            notice = ("已从 SQLite 摘要恢复主要内容；旧 JSONL 未迁移"
                      if recovery else "旧 JSONL 不存在，SQLite 也没有可用摘要；已创建空白会话")
        else:
            await agent.client.switch_session(row["pi_session_file"])
            notice = None
        storage.set_active(sid)
    except Exception:
        if switch_attempted:
            try:
                if not previous_file:
                    raise PiError("无法确认旧会话文件")
                await agent.client.switch_session(previous_file)
            except Exception:
                logger.exception("恢复旧 pi 会话失败", extra={"user_id": user_id})
                await agent.client.stop()
        raise
    agent.recovery_notice = None
    return notice


async def _cmd_session_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    args = context.args
    sub = args[0] if args else ""

    if sub in {"new", "use"} and (agent.client.busy or agent.prompt_pending):
        await reply_text(update, "⏳ 当前任务仍在运行，请完成或 /stop 后再切换会话")
        return

    if sub == "new":
        name = " ".join(args[1:]).strip() or f"会话-{len(storage.list_sessions(user.id)) + 1}"
        try:
            sid = await create_user_session(agent, storage, user.id, name)
        except Exception as e:
            logger.exception("新建会话失败", extra={"user_id": user.id, "err": str(e)[:300]})
            await reply_text(update, f"新建会话失败：{_friendly_error(e)}")
        else:
            await reply_text(update, f"✅ 已新建会话「{name}」\nID: {sid}")

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
        try:
            notice = await switch_user_session(agent, storage, user.id, sid)
        except Exception as e:
            logger.exception("切换会话失败", extra={"user_id": user.id, "err": str(e)[:300]})
            await reply_text(update, f"切换会话失败: {_friendly_error(e)}")
        else:
            await reply_text(update, f"✅ 已切换到会话「{row['name']}」({sid})"
                             + (f"\n{notice}" if notice else ""))

    else:
        await reply_text(update, "用法：/session new [名称] | /session list | /session use <id>")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = context.args[0] if context.args else ""
    if sub not in {"new", "use"}:
        await _cmd_session_unlocked(update, context)
        return
    agent = context.bot_data["manager"].get(update.effective_user.id)
    async with agent.control_lock:
        await _cmd_session_unlocked(update, context)


async def _cmd_model_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    args = context.args
    sub = args[0] if args else ""

    if sub == "use" and (agent.client.busy or agent.prompt_pending):
        await reply_text(update, "⏳ 当前任务仍在运行，请完成或 /stop 后再切换模型")
        return

    if sub == "list":
        try:
            await agent.ensure_client()
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
            await agent.ensure_client()
            await agent.client.set_model(args[1])
            await reply_text(update, f"✅ 已切换模型: {args[1]}")
        except Exception as e:
            logger.error("切换模型失败", extra={"err": str(e)[:300]})
            await reply_text(update, f"切换模型失败: {e}")

    else:
        await reply_text(update, "用法：/model list | /model use <model_id>")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = context.args[0] if context.args else ""
    if sub not in {"use", "list"}:
        await _cmd_model_unlocked(update, context)
        return
    agent = context.bot_data["manager"].get(update.effective_user.id)
    async with agent.control_lock:
        await _cmd_model_unlocked(update, context)


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    storage = context.bot_data["storage"]
    args = context.args or []
    sub = args[0] if args else "current"
    if sub in {"current", "list"}:
        active = storage.get_active_persona(user.id)
        if sub == "current":
            await reply_text(update, f"当前人格：{active['name']} ({active['id']})\n用法：/persona list | /persona use <id>")
            return
        rows = [row for row in storage.list_personas() if row["enabled"]]
        lines = ["可用人格："] + [
            f"{'●' if row['id'] == active['id'] else '○'} {row['name']} ({row['id']})"
            for row in rows
        ]
        lines.append("切换：/persona use <id>")
        await reply_text(update, "\n".join(lines))
        return
    if sub != "use" or len(args) != 2:
        await reply_text(update, "用法：/persona current | /persona list | /persona use <id>")
        return
    agent = context.bot_data["manager"].get(user.id)
    async with agent.control_lock:
        if agent.client.busy or agent.prompt_pending:
            await reply_text(update, "⏳ 当前任务仍在运行，请完成或 /stop 后再切换人格")
            return
        try:
            storage.set_active_persona(user.id, args[1])
        except ValueError as exc:
            await reply_text(update, str(exc))
            return
        selected = storage.get_active_persona(user.id)
        await reply_text(update, f"已切换人格：{selected['name']}。下一条消息起生效。")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    agent = manager.get(user.id)
    if agent.prompt_pending and not agent.client.busy:
        for _ in range(100):
            if agent.client.busy or not agent.prompt_pending:
                break
            await asyncio.sleep(0.05)
    if not agent.client.busy:
        await reply_text(update, "当前没有正在运行的任务")
        return
    try:
        await agent.client.abort()
    except Exception as e:
        logger.exception("中止任务失败", extra={"user_id": user.id})
        await reply_text(update, f"中止任务失败：{_friendly_error(e)}")
        return
    await reply_text(update, "⏹ pi 已确认中止指令")


async def _cmd_status_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    manager = context.bot_data["manager"]
    storage = context.bot_data["storage"]
    agent = manager.get(user.id)
    proc = agent.client.proc
    proc_state = "运行中" if (proc and proc.returncode is None) else "未运行"
    active = storage.get_active_session(user.id)
    active_name = active["name"] if active else "-"
    persona_name = storage.get_active_persona(user.id)["name"]
    lines = [
        "📊 状态：",
        f"  环境: server（本机）",
        f"  pi 进程: {proc_state}",
        f"  忙碌: {'是' if agent.client.busy else '否'}",
        f"  当前会话: {active_name}",
        f"  当前人格: {persona_name}",
        f"  默认模型: {agent.settings.pi_model}",
    ]
    # 追加 pi 会话详情（消息数 / 压缩状态），拿不到不阻塞主状态展示
    try:
        if not proc or proc.returncode is not None:
            raise PiError("pi 进程未运行")
        await agent.ensure_client()
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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent = context.bot_data["manager"].get(update.effective_user.id)
    async with agent.control_lock:
        await _cmd_status_unlocked(update, context)


def _cost_report(storage: Storage, runs: list, *, title: str) -> str:
    counts = {key: 0 for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}
    total_usd = Decimal(0)
    maintenance_usd = Decimal(0)
    persona_test_usd = Decimal(0)
    prepared_context_bytes = 0
    unpriced = 0
    stale = False
    incomplete_usage = False
    has_events = False
    tiers: set[str] = set()
    for run in runs:
        prepared_context_bytes += run["memory_context_bytes"]
        run_counts = {key: 0 for key in counts}
        for event in storage.get_usage_events(run["id"]):
            has_events = True
            for key in counts:
                counts[key] += event[key]
                run_counts[key] += event[key]
            if event["estimated_usd"] is None:
                unpriced += 1
            else:
                total_usd += Decimal(event["estimated_usd"])
                if run["origin"] == "memory_maintenance":
                    maintenance_usd += Decimal(event["estimated_usd"])
                if run["origin"] == "persona_test":
                    persona_test_usd += Decimal(event["estimated_usd"])
            if event["price_tier"]:
                tiers.add(event["price_tier"])
            stale |= event["price_status"] == "stale_price"
        if run["stats_before"] and run["stats_after"]:
            try:
                before = json.loads(run["stats_before"])["tokens"]
                after = json.loads(run["stats_after"])["tokens"]
                for field, ledger_field in (("input", "input_tokens"), ("cacheRead", "cache_read_tokens"),
                                            ("cacheWrite", "cache_write_tokens"), ("output", "output_tokens")):
                    if after[field] - before[field] > run_counts[ledger_field]:
                        incomplete_usage = True
            except (KeyError, TypeError, ValueError):
                pass
    if not runs:
        return f"{title}：暂无用量记录"
    tier_name = {"peak": "峰时", "off_peak": "谷时"}
    tier_label = "、".join(tier_name.get(tier, tier) for tier in sorted(tiers))
    lines = [
        title,
        f"输入：未命中 {counts['input_tokens']:,} · 缓存命中 {counts['cache_read_tokens']:,}"
        + (f" · 缓存写入 {counts['cache_write_tokens']:,}" if counts["cache_write_tokens"] else ""),
        f"输出：{counts['output_tokens']:,} tokens",
        f"按公开价格估算：{format_usd(total_usd)}" + (f"（{tier_label}）" if tier_label else ""),
    ]
    if unpriced:
        lines.append(f"⚠️ {unpriced} 次模型调用未计价；上方金额不是完整总额")
    if any(run["origin"] == "memory_maintenance" for run in runs):
        lines.append(f"其中记忆维护：{format_usd(maintenance_usd)}")
    if any(run["origin"] == "persona_test" for run in runs):
        lines.append(f"其中人格测试：{format_usd(persona_test_usd)}")
    if prepared_context_bytes:
        lines.append(
            f"各回合准备的记忆上下文合计：{prepared_context_bytes:,} 字节"
            "（每回合记一次；多次模型调用可能重复注入，不能据此计算记忆费用）"
        )
    if stale:
        lines.append("⚠️ 费率文件已到复核日期，请核对 DeepSeek 官方价格")
    if incomplete_usage:
        lines.append("⚠️ pi 会话统计高于已记录事件；可能包含工具或摘要调用，金额可能偏低")
    if any(run["status"] != "complete" for run in runs):
        lines.append("⚠️ 存在未完成回合，用量可能不完整")
    if not has_events:
        lines.append("尚未收到模型用量；请勿将 $0 视为官方实扣金额")
    return "\n".join(lines)


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    if not getattr(settings, "cost_tracking_enabled", False):
        await reply_text(update, "用量记录未启用")
        return
    storage = context.bot_data["storage"]
    user_id = update.effective_user.id
    mode = context.args[0] if context.args else "last"
    if mode == "last":
        last = storage.get_last_usage_run(user_id, origin="chat")
        runs = [last] if last else []
        title = "📊 最近一轮"
    elif mode == "today":
        local_now = datetime.now(timezone(timedelta(hours=8)))
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        runs = storage.get_usage_runs_since(user_id, midnight.timestamp())
        chat_count = sum(run["origin"] == "chat" for run in runs)
        maintenance_count = sum(run["origin"] == "memory_maintenance" for run in runs)
        title = f"📊 今日用量（北京时间，聊天 {chat_count} 轮，记忆维护 {maintenance_count} 次）"
    else:
        await reply_text(update, "用法：/cost [last|today]")
        return
    await reply_text(update, _cost_report(storage, runs, title=title))


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    manager = context.bot_data["manager"]
    memory = getattr(manager, "memory", None)
    if memory is None:
        await reply_text(update, "长期记忆未启用")
        return
    storage = context.bot_data["storage"]
    user_id = update.effective_user.id
    args = context.args or []
    sub = args[0] if args else "list"
    if sub == "list":
        try:
            page = max(1, int(args[1])) if len(args) > 1 else 1
        except ValueError:
            await reply_text(update, "用法：/memory list [页码]")
            return
        rows = storage.list_memories(user_id)
        page_size = 8
        shown = rows[(page - 1) * page_size:page * page_size]
        lines = [f"🧠 长期记忆（{len(rows)}/{settings.memory_active_limit}，第 {page} 页）"]
        lines += [f"{'📌 ' if row['pinned'] else ''}{row['id']}：{row['text']}" for row in shown]
        if not shown:
            lines.append("暂无此页记忆")
        lines.append("/memory add <内容> | edit <id> <内容> | delete <id> | pin <id> | unpin <id>")
        await reply_text(update, "\n".join(lines))
        return
    if sub not in {"add", "edit", "delete", "pin", "unpin"}:
        await reply_text(update, "用法：/memory list [页码] | add <内容> | edit <id> <内容> | delete <id> | pin <id> | unpin <id>")
        return
    agent = manager.get(user_id)
    async with agent.control_lock:
        if sub == "add" or sub == "edit":
            item = " ".join(args[1:] if sub == "add" else args[2:]).strip()
            if not item or (sub == "edit" and len(args) < 3):
                await reply_text(update, f"用法：/memory {sub} " + ("<内容>" if sub == "add" else "<id> <内容>"))
                return
            if len(item) > settings.memory_item_max_chars:
                await reply_text(update, f"记忆过长，最多 {settings.memory_item_max_chars} 字符")
                return
            if looks_secret(item):
                await reply_text(update, "请不要把密码、密钥或令牌存为长期记忆")
                return
            try:
                if sub == "add":
                    memory_id = storage.add_memory(user_id, item, limit=settings.memory_active_limit)
                else:
                    memory_id = args[1]
                    if not storage.edit_memory(user_id, memory_id, item):
                        await reply_text(update, "找不到这条记忆")
                        return
            except Exception as exc:
                await reply_text(update, f"保存记忆失败：{str(exc)[:120]}")
                return
            result = f"✅ 已保存记忆 {memory_id}"
        else:
            if len(args) < 2:
                await reply_text(update, f"用法：/memory {sub} <id>")
                return
            memory_id = args[1]
            if sub == "delete":
                changed = storage.delete_memory(user_id, memory_id)
                result = f"已删除记忆 {memory_id}；旧会话历史中相同信息仍可能存在"
            else:
                changed = storage.pin_memory(user_id, memory_id, sub == "pin")
                result = f"已{'置顶' if sub == 'pin' else '取消置顶'}记忆 {memory_id}"
            if not changed:
                await reply_text(update, "找不到这条记忆")
                return
        active = storage.get_active_session(user_id)
        if active and not agent.client.busy:
            try:
                memory.refresh_context(user_id, active["id"], active["pi_session_file"])
            except Exception:
                memory.clear_context(user_id)
                logger.exception("记忆变更后刷新上下文失败", extra={"user_id": user_id})
                result += "；临时上下文刷新失败，下一轮将重新生成"
        await reply_text(update, result + ("；当前正在进行的请求可能已读取旧内容" if agent.client.busy else ""))


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = context.bot_data["manager"]
    if getattr(manager, "memory", None) is None:
        await reply_text(update, "会话摘要未启用")
        return
    storage = context.bot_data["storage"]
    user_id = update.effective_user.id
    active = storage.get_active_session(user_id)
    if active is None:
        await reply_text(update, "当前没有会话")
        return
    sub = context.args[0] if context.args else "show"
    if sub == "show":
        row = storage.get_summary(active["id"])
        await reply_text(update, "📝 当前会话摘要\n" + (row["summary_text"] if row and row["summary_text"] else "暂无摘要"))
    elif sub == "clear":
        agent = manager.get(user_id)
        async with agent.control_lock:
            storage.clear_summary(active["id"])
            if not agent.client.busy:
                try:
                    manager.memory.refresh_context(user_id, active["id"], active["pi_session_file"])
                except Exception:
                    manager.memory.clear_context(user_id)
                    logger.exception("摘要清空后刷新上下文失败", extra={"user_id": user_id})
        await reply_text(update, "已清空 SQLite 会话摘要；已压缩的旧内容无法仅靠数据库重建，旧 pi JSONL 不会自动删除")
    else:
        await reply_text(update, "用法：/summary [show|clear]")


async def _read_usage_stats(client: PiRpcClient) -> dict | None:
    try:
        stats = await client.get_session_stats()
        return {"tokens": stats.get("tokens"), "cost": stats.get("cost")}
    except Exception as exc:
        logger.warning("读取 pi 会话用量失败", extra={"err": str(exc)[:200]})
        return None


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

def _image_mime_type(data: bytes | bytearray) -> str | None:
    """只根据实际文件头判断 pi 支持的图片格式，不信任 Telegram 元数据。"""
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return "image/png"
    if len(data) >= 4 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 13 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_dimensions(data: bytes | bytearray, mime_type: str) -> tuple[int, int] | None:
    """只读图片头中的宽高，不解码用户提供的像素数据。"""
    if mime_type == "image/png" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime_type == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime_type == "image/webp":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3fff, int.from_bytes(data[28:30], "little") & 0x3fff
        if data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2f:
            return (
                1 + data[21] + ((data[22] & 0x3f) << 8),
                1 + (data[22] >> 6) + (data[23] << 2) + ((data[24] & 0x0f) << 10),
            )
    if mime_type == "image/jpeg":
        pos = 2
        sof_markers = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
        while pos + 1 < len(data):
            if data[pos] != 0xff:
                return None
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                return None
            marker = data[pos]
            pos += 1
            if marker == 0xda or marker == 0xd9:
                return None
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            if pos + 2 > len(data):
                return None
            length = int.from_bytes(data[pos:pos + 2], "big")
            if length < 2 or pos + length > len(data):
                return None
            if marker in sof_markers:
                if length < 7:
                    return None
                return int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 3:pos + 5], "big")
            pos += length
    return None


async def _download_image(message, *, user_id: int | None = None) -> dict[str, str]:
    """下载一张 Telegram 图片，验证字节数及文件头后组装 pi RPC 请求。"""
    media = message.photo[-1] if message.photo else message.document
    if media is None:
        raise ValueError("消息中没有图片")
    if media.file_size is not None and media.file_size > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 10 MiB 限制，请压缩后重发")
    for attempt in range(3):
        try:
            file = await media.get_file()
            if file.file_size is not None and file.file_size > MAX_IMAGE_BYTES:
                raise _ImageTooLarge("图片超过 10 MiB 限制，请压缩后重发")
            data = await file.download_as_bytearray(read_timeout=60, connect_timeout=20)
            break
        except _ImageTooLarge:
            raise
        except (*_TG_NET_EXC, RetryAfter) as exc:
            if attempt == 2:
                logger.warning("Telegram 图片下载重试耗尽", extra={"user_id": user_id, "err_type": type(exc).__name__})
                raise ValueError("图片下载失败，请稍后重发") from None
            if isinstance(exc, RetryAfter):
                delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
                await asyncio.sleep(max(0.5, delay))
            else:
                await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as exc:
            logger.warning("Telegram 图片下载失败", extra={"user_id": user_id, "err_type": type(exc).__name__})
            raise ValueError("图片下载失败，请稍后重发") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 10 MiB 限制，请重发")
    mime_type = _image_mime_type(data)
    if mime_type is None:
        raise ValueError("图片格式无效；仅支持 JPEG、PNG、GIF、WebP")
    dimensions = _image_dimensions(data, mime_type)
    if dimensions is None or min(dimensions) < 1:
        raise ValueError("无法读取图片尺寸，文件可能已损坏")
    if max(dimensions) > MAX_IMAGE_DIMENSION:
        raise ValueError("图片宽或高超过 8192 像素，请缩小后重发")
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}


async def _model_accepts_images(client: PiRpcClient) -> bool:
    """从 pi 当前模型的能力元数据确认图片支持，避免图片被静默丢弃。"""
    state = await client.get_state()
    model = state.get("model")
    if isinstance(model, dict) and isinstance(model.get("input"), list):
        return "image" in model["input"]
    raise ValueError("无法确认当前模型的图片能力，请用 /model list 选择带 🖼 标记的模型后重发")


async def _prepare_images(client: PiRpcClient, message, *, user_id: int | None = None) -> list[dict[str, str]]:
    if not await _model_accepts_images(client):
        raise ValueError("当前模型不支持图片输入，请用 /model list 选择带 🖼 标记的模型后重发")
    return [await _download_image(message, user_id=user_id)]

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    message = update.message
    has_image = bool(message.photo or message.document)
    text = message.text or message.caption or ""
    if has_image and not text.strip():
        text = "请分析这张图片。"
    chat = update.effective_chat
    owns_prompt = False
    usage_run_id: str | None = None
    usage_seq = 0
    turn_settled = False
    memory_turn_id: int | None = None
    memory_session_id: str | None = None
    assistant_text = ""
    injected_bytes = 0

    # 相册由多条独立 Update 组成；逐条提交会丢失图片顺序或让后续图片撞上忙碌状态。
    # 当前明确要求单张发送，并在同一相册内只提示一次。
    if has_image and message.media_group_id:
        notices = context.bot_data.setdefault("image_album_notices", {})
        now = time.monotonic()
        for key, seen_at in list(notices.items()):
            if now - seen_at > 300:
                del notices[key]
        key = (user.id, message.media_group_id)
        if key not in notices:
            notices[key] = now
            try:
                await reply_text(update, "⚠️ 暂不支持相册合并处理，请将图片逐张发送")
            except Exception:
                notices.pop(key, None)
                raise
        return

    agent = manager.get(user.id)
    storage.ensure_user(user.id, user.username)
    logger.info("收到消息", extra={"user_id": user.id, "chat_id": chat.id, "text_len": len(text), "has_image": has_image})

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
        if agent.control_lock.locked():
            await reply_text(update, "⏳ 正在准备上一条消息或切换会话/模型，请稍后再发")
            return

        # 与 /model、/session 共用锁，保证图片能力检查和下载期间模型、会话不切换。
        # 在锁内依据最新 busy 状态选择 steer 或保留下一次 prompt。
        async with agent.control_lock:
            try:
                await agent.ensure_client()
            except Exception as e:
                logger.error("pi 进程启动失败", extra={"err": str(e)[:300]})
                await reply_text(update, f"⚠️ pi 进程启动失败: {_friendly_error(e)}")
                return
            await notify_interrupted_turns(update, storage, user.id)
            if getattr(agent, "recovery_notice", None):
                await reply_text(update, agent.recovery_notice)
                agent.recovery_notice = None

            # 图片下载可能耗时，先完成校验，再依据最新的 busy 状态选择 steer 或 prompt。
            # 否则当前回合可能在下载期间结束，图片被提交到空闲 steer 队列。
            images = None
            if has_image:
                try:
                    images = await _prepare_images(agent.client, message, user_id=user.id)
                except ValueError as exc:
                    await reply_text(update, f"⚠️ {exc}")
                    return

            # 会话自动命名：默认会话的第一条消息作为会话名
            await _auto_name_session(storage, user.id, text)

            # 流式中途介入：pi 忙时插入 steer 而非拒绝
            if agent.prompt_pending and not agent.client.busy:
                for _ in range(100):
                    if agent.client.busy or not agent.prompt_pending:
                        break
                    await asyncio.sleep(0.05)
            if agent.client.busy:
                try:
                    if has_image:
                        await agent.client.steer(text, images=images)
                    else:
                        await agent.client.steer(text)
                    if getattr(agent, "active_turn_id", None) is not None:
                        storage.append_turn_steer(
                            agent.active_turn_id,
                            safe_turn_text(text, getattr(settings, "memory_turn_user_chars", 4000)),
                        )
                    await reply_text(update, "↩️ pi 已收到新指令，将在当前任务中处理")
                except Exception as e:
                    logger.exception("插入指令失败", extra={"user_id": user.id})
                    await reply_text(update, f"⚠️ 插入指令失败: {_friendly_error(e)}")
                return
            if agent.prompt_pending:
                await reply_text(update, "⏳ 当前任务正在启动，请稍后再发")
                return
            agent.prompt_pending = True
            owns_prompt = True
            memory = getattr(manager, "memory", None)
            if memory is not None:
                try:
                    active = storage.get_active_session(user.id)
                    if active:
                        memory_session_id = active["id"]
                        memory_turn_id = storage.start_turn(
                            user.id, memory_session_id, getattr(update, "update_id", None),
                            safe_turn_text(text, settings.memory_turn_user_chars), has_image,
                        )
                        agent.active_turn_id = memory_turn_id
                        injected_bytes = memory.refresh_context(
                            user.id, memory_session_id, active["pi_session_file"], text,
                        )
                        logger.info("记忆上下文已准备", extra={"user_id": user.id,
                                   "injected_bytes": injected_bytes})
                except Exception:
                    logger.exception("准备记忆上下文失败", extra={"user_id": user.id})
                    memory.clear_context(user.id)

        if getattr(settings, "cost_tracking_enabled", False):
            stats_before = await _read_usage_stats(agent.client)
            try:
                active = storage.get_active_session(user.id)
                usage_run_id = storage.start_usage_run(
                    user.id, active["id"] if active else None,
                    getattr(update, "update_id", None), stats_before,
                    memory_context_bytes=injected_bytes,
                )
            except Exception:
                logger.exception("创建用量记录失败", extra={"user_id": user.id})

        pricing = PricingCatalog(getattr(settings, "pricing_file", "pricing.json"))

        def on_usage(event: dict) -> None:
            nonlocal usage_seq
            if usage_run_id is None:
                return
            usage_seq += 1
            price = pricing.price(event["provider"], event["model"], event["usage"], event["occurred_at"])
            try:
                storage.record_usage_event(usage_run_id, usage_seq, event, price)
            except Exception:
                logger.exception("保存模型用量失败", extra={"user_id": user.id, "run_id": usage_run_id})

        # 思考占位 + 工具活动状态
        placeholder = {"msg": None}
        activity = {"current": None}

        async def on_thinking() -> None:
            if placeholder["msg"] is None:
                placeholder["msg"] = await _send(update.message.reply_text, "🤔 深度思考中…")

        def on_tool(tool_name: str, phase: str, args: dict | None) -> None:
            activity["current"] = _tool_activity(tool_name, phase, args)
            notify = activity.get("notify")
            if notify is not None:
                notify()

        async def gen():
            nonlocal turn_settled, assistant_text
            async for delta in agent.client.prompt(
                text,
                images=images,
                on_thinking=on_thinking,
                on_tool=on_tool,
                on_usage=on_usage,
            ):
                if memory_turn_id is not None:
                    assistant_text = (assistant_text + delta)[:settings.memory_turn_assistant_chars]
                yield delta
            turn_settled = True

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
        if owns_prompt:
            if memory_turn_id is not None:
                try:
                    storage.finish_turn(
                        memory_turn_id,
                        safe_turn_text(assistant_text, settings.memory_turn_assistant_chars),
                        complete=turn_settled,
                    )
                    if turn_settled and memory_session_id is not None:
                        manager.memory.schedule_summary(user.id, memory_session_id)
                except Exception:
                    logger.exception("保存会话回合失败", extra={"user_id": user.id})
            if hasattr(agent, "active_turn_id"):
                agent.active_turn_id = None
            if usage_run_id is not None:
                stats_after = await _read_usage_stats(agent.client)
                try:
                    storage.finish_usage_run(usage_run_id, "complete" if turn_settled else "incomplete", stats_after)
                    if getattr(settings, "cost_auto_report", False):
                        run = storage.get_last_usage_run(user.id, origin="chat")
                        if run is not None and run["id"] == usage_run_id:
                            try:
                                await reply_text(update, _cost_report(storage, [run], title="📊 本轮用量"))
                            except Exception:
                                logger.exception("发送用量摘要失败", extra={"user_id": user.id})
                except Exception:
                    logger.exception("完成用量记录失败", extra={"user_id": user.id, "run_id": usage_run_id})
            agent.prompt_pending = False
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
    ("persona", "人格切换"),
    ("status", "运行状态"),
    ("cost", "模型用量与费用"),
    ("memory", "长期记忆"),
    ("summary", "当前会话摘要"),
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
        CommandHandler("persona", cmd_persona),
        CommandHandler("stop", cmd_stop),
        CommandHandler("status", cmd_status),
        CommandHandler("cost", cmd_cost),
        CommandHandler("memory", cmd_memory),
        CommandHandler("summary", cmd_summary),
        CallbackQueryHandler(on_callback),
    ]
    for h in handlers:
        inner = h.callback
        h.callback = lambda u, c, inner=inner: auth_guard(u, c, inner)
        app.add_handler(h)

    # 未知命令与普通文本走同一鉴权入口。
    async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply_text(update, "未知命令，/help 查看帮助")

    app.add_handler(MessageHandler(filters.COMMAND, lambda u, c: auth_guard(u, c, unknown_command)))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL,
        lambda u, c: auth_guard(u, c, on_text),
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: auth_guard(u, c, on_text)))

    return app
