"""单管理员后台：服务端页面、签名会话和 SQLite 记忆管理。"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import Settings
from bot import create_user_session, session_recovery_preview, switch_user_session
from costs import PricingCatalog, format_usd
from logging_conf import get_logger
from mail_service import MailSendError, email_address, send_email, smtp_settings as validate_smtp_settings
from memory import looks_secret
from ops import backup_status
from persona import FIELDS as PERSONA_FIELDS, render_persona, validate_persona, write_prompt
from pi_bridge import PiBusyError, PiError, PiRpcClient
from storage import Storage

logger = get_logger("admin")
router = APIRouter(prefix="/admin", include_in_schema=False)
_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
# 后台是整页导航。把这份小样式与 HTML 一起送达，避免每次切页重取 CSS 时短暂裸显。
_STYLE = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
_STYLE_TAG = f"<style>{_STYLE}</style>"
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")
_SCRIPT = (_STATIC_DIR / "form_state.js").read_text(encoding="utf-8")
_SCRIPT_TAG = f"<script>{_SCRIPT}</script>"
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")
_SESSION_COOKIE = "dj_admin_session"
_PRELOGIN_COOKIE = "dj_admin_prelogin"
_SESSION_SECONDS = 12 * 3600
_PRELOGIN_SECONDS = 10 * 60
_LOGIN_FAILURES: dict[str, list[float]] = {}

# 仅这些记忆预算允许从后台覆盖；模型和密钥仍归 .env 管理。
_FIELDS: tuple[tuple[str, str, str, int, int], ...] = (
    ("memory_batch_turns", "摘要间隔", "轮", 1, 100),
    ("memory_summary_retry_seconds", "失败重试", "秒", 60, 86400),
    ("memory_turn_retention", "近期回合保留", "轮", 1, 1000),
    ("memory_turn_user_chars", "用户文本入库", "字符", 100, 20000),
    ("memory_turn_assistant_chars", "回复文本入库", "字符", 100, 40000),
    ("memory_summary_input_max_bytes", "摘要输入", "字节", 1024, 131072),
    ("memory_summary_response_max_bytes", "摘要回复", "字节", 512, 32768),
    ("memory_active_limit", "记忆总数", "条", 1, 1000),
    ("memory_item_max_chars", "单条记忆", "字符", 16, 2000),
    ("memory_inject_max_items", "每轮选取", "条", 1, 100),
    ("memory_inject_max_bytes", "总注入预算", "字节", 512, 32768),
    ("memory_summary_max_bytes", "恢复摘要预算", "字节", 128, 16384),
    ("memory_items_max_bytes", "记忆条目预算", "字节", 128, 16384),
)
_FIELD_KEYS = {row[0] for row in _FIELDS}


def _e(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _same_secret(value: str, expected: str) -> bool:
    """compare_digest 的 str 仅支持 ASCII；表单和配置允许 Unicode。"""
    return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def _memory_values(data: dict[str, object], *, from_form: bool) -> dict[str, int | bool]:
    values: dict[str, int | bool] = {}
    if from_form:
        values["memory_auto_extract"] = data.get("memory_auto_extract") == "on"
    elif "memory_auto_extract" in data:
        value = data["memory_auto_extract"]
        if type(value) is not bool:
            raise ValueError("自动提取配置无效")
        values["memory_auto_extract"] = value
    for key, label, _unit, minimum, maximum in _FIELDS:
        if key not in data:
            if from_form:
                raise ValueError(f"缺少参数：{label}")
            continue
        value = data[key]
        if from_form:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
                raise ValueError(f"{label}必须是整数")
            parsed = int(value)
        else:
            if type(value) is not int:
                raise ValueError(f"{label}配置无效")
            parsed = value
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{label}范围：{minimum}–{maximum}")
        values[key] = parsed
    return values


def _validated_candidate(settings: Settings, values: dict[str, int | bool]) -> Settings:
    candidate = Settings(**{**settings.model_dump(), **values})
    if candidate.memory_summary_max_bytes + candidate.memory_items_max_bytes > candidate.memory_inject_max_bytes:
        raise ValueError("恢复摘要与记忆条目预算之和不能超过总注入预算")
    candidate.validate_runtime()
    return candidate


def apply_runtime_settings(settings: Settings, storage: Storage) -> None:
    """启动时加载 SQLite 覆盖值；表单保存时用同一套约束校验。"""
    raw = storage.get_runtime_settings()
    unknown = set(raw) - _FIELD_KEYS - {"memory_auto_extract"}
    if unknown:
        logger.warning("忽略未知的后台参数", extra={"keys": sorted(unknown)})
    values = _memory_values(raw, from_form=False)
    _validated_candidate(settings, values)
    for key, value in values.items():
        setattr(settings, key, value)


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(
        f"djhxpibot-admin-v1\0{settings.admin_username}\0{settings.admin_password}\0{settings.webhook_secret}".encode()
    ).digest()


def _seal(settings: Settings, kind: str, csrf: str) -> str:
    payload = {"kind": kind, "user": settings.admin_username, "iat": int(time.time()), "csrf": csrf}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(_key(settings), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _unseal(settings: Settings, token: str | None, kind: str) -> str | None:
    if not token or not settings.admin_username or not settings.admin_password:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(_key(settings), body.encode(), hashlib.sha256).hexdigest()
        if not _same_secret(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        age = time.time() - payload["iat"]
        ttl = _SESSION_SECONDS if kind == "auth" else _PRELOGIN_SECONDS
        if payload["kind"] != kind or payload["user"] != settings.admin_username or not 0 <= age <= ttl:
            return None
        csrf = payload["csrf"]
        return csrf if isinstance(csrf, str) and len(csrf) >= 24 else None
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
        return None


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _storage(request: Request) -> Storage:
    return request.app.state.storage


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; script-src 'sha256-{_SCRIPT_HASH}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _html(body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=_headers())


def _cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/admin", secure=True,
                        httponly=True, samesite="strict")


def _session(request: Request) -> str | None:
    return _unseal(_settings(request), request.cookies.get(_SESSION_COOKIE), "auth")


def _require_session(request: Request) -> str:
    csrf = _session(request)
    if not csrf:
        raise HTTPException(status_code=401, detail="请先登录")
    return csrf


async def _form(request: Request) -> dict[str, str]:
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(status_code=415, detail="仅接受表单提交")
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="表单长度无效") from exc
    if content_length > 16384:
        raise HTTPException(status_code=413, detail="表单过大")
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(status_code=413, detail="表单过大")
    try:
        data = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=40)
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="表单格式无效") from exc
    return {key: values[-1] for key, values in data.items()}


async def _authorized_form(request: Request) -> tuple[str, dict[str, str]]:
    csrf = _require_session(request)
    form = await _form(request)
    if not _same_secret(form.get("csrf", ""), csrf):
        raise HTTPException(status_code=403, detail="页面已过期，请刷新后重试")
    return csrf, form


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers={"Cache-Control": "no-store"})


def _login_page(csrf: str, error: str = "", username: str = "") -> str:
    message = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · Pi 管理</title>
{_STYLE_TAG}</head><body class="login-body">
<main class="login-card"><div class="brand-mark" aria-hidden="true">P<span>·</span></div>
<p class="eyebrow">DJHX / PI</p><h1>管理后台</h1>{message}
<form method="post" action="/admin/login" class="form-stack">
<input type="hidden" name="csrf" value="{_e(csrf)}">
<label>用户名<input name="username" value="{_e(username)}" autocomplete="username" required autofocus></label>
<label>密码<input name="password" type="password" autocomplete="current-password" required></label>
<button class="button primary" type="submit">登录</button></form></main>{_SCRIPT_TAG}</body></html>"""


def _layout(title: str, active: str, body: str, csrf: str, user_id: int | None, settings: Settings) -> str:
    suffix = f"?{urlencode({'user_id': user_id})}" if user_id is not None else ""
    def nav(path: str, label: str, key: str) -> str:
        cls = "nav-link active" if active == key else "nav-link"
        return f'<a class="{cls}" href="/admin/{path}{suffix if path not in {"settings", "mail"} else ""}">{label}</a>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} · Pi 管理</title>
{_STYLE_TAG}</head><body>
<div class="app-shell"><aside class="sidebar"><a class="brand" href="/admin/overview">
<span class="brand-mark">P<span>·</span></span><span>PI<span class="brand-muted"> / ADMIN</span></span></a>
<nav aria-label="主导航">{nav('overview', '概览', 'overview')}{nav('sessions', '会话管理', 'sessions')}{nav('usage', '用量费用', 'usage')}{nav('memories', '长期记忆', 'memories')}{nav('summaries', '会话摘要', 'summaries')}{nav('personas', '人格管理', 'personas')}{nav('settings', '记忆设置', 'settings')}{nav('mail', '邮件设置', 'mail')}</nav>
<p class="nav-scroll-hint">← 左右滑动查看全部菜单 →</p>
<div class="sidebar-bottom"><span class="admin-name">{_e(settings.admin_username)}</span>
<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{_e(csrf)}">
<button class="text-button" type="submit">退出</button></form></div></aside>
<main class="main"><header class="topline"><span>DJHX PI BOT</span><span>ADMIN</span></header>
<div class="page-content">{body}</div></main></div>{_SCRIPT_TAG}</body></html>"""


def _users(storage: Storage, settings: Settings) -> list[tuple[int, str]]:
    known = {row["user_id"]: row["username"] or "" for row in storage.list_admin_users()}
    for user_id in settings.allowed_ids:
        known.setdefault(user_id, "")
    return [(user_id, known[user_id]) for user_id in sorted(known)]


def _selected_user(raw: str | None, storage: Storage, settings: Settings) -> int | None:
    users = _users(storage, settings)
    ids = {user_id for user_id, _ in users}
    if raw:
        try:
            user_id = int(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="用户无效") from exc
        if user_id not in ids:
            raise HTTPException(status_code=400, detail="用户无效")
        return user_id
    return users[0][0] if users else None


def _form_user(form: dict[str, str], storage: Storage, settings: Settings) -> int:
    if not form.get("user_id"):
        raise HTTPException(status_code=400, detail="用户无效")
    user_id = _selected_user(form["user_id"], storage, settings)
    if user_id is None:
        raise HTTPException(status_code=400, detail="用户无效")
    return user_id


def _user_picker(storage: Storage, settings: Settings, user_id: int | None, path: str) -> str:
    options = "".join(
        f'<option value="{uid}"{" selected" if uid == user_id else ""}>{_e(name or str(uid))}'
        f'{" · " + str(uid) if name else ""}</option>'
        for uid, name in _users(storage, settings)
    )
    return f'<form class="user-picker" method="get" action="/admin/{path}"><label class="sr-only" for="user-id">用户</label><select id="user-id" name="user_id">{options}</select><button class="button subtle" type="submit">切换</button></form>'


def _notice(request: Request) -> str:
    labels = {"saved": "已保存", "deleted": "已删除", "pinned": "已更新", "cleared": "已清空",
              "created": "会话已创建", "switched": "会话已切换", "recovered": "会话已从 SQLite 恢复",
              "sent": "测试邮件已提交给 SMTP 服务器", "restored": "已恢复 .env 默认值"}
    value = labels.get(request.query_params.get("status", ""))
    return f'<p class="notice" role="status">{value}</p>' if value else ""


_CST = timezone(timedelta(hours=8))


def _time_label(value: float | str | None) -> str:
    if value is None:
        return "—"
    try:
        moment = (datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float))
                  else datetime.fromisoformat(value).replace(tzinfo=timezone.utc))
        return moment.astimezone(_CST).strftime("%m-%d %H:%M")
    except (ValueError, OverflowError):
        return "—"


def _overview_page(request: Request, csrf: str, user_id: int | None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "overview")
    stats = storage.update_stats()
    worker = getattr(request.app.state, "update_worker", None)
    service = "正常" if worker is not None and not worker.done() and getattr(request.app.state, "worker_ready", True) else ("未读取" if worker is None else "异常")
    agent = request.app.state.manager._agents.get(user_id) if user_id is not None else None
    proc = agent.client.proc if agent is not None else None
    pi_state = ("运行中" if proc is not None and proc.returncode is None else
                ("待重连" if proc is not None else "按需启动"))
    busy = bool(agent and (agent.client.busy or agent.prompt_pending))
    active = storage.get_active_session(user_id) if user_id is not None else None
    selected_model = (storage.get_effective_model_selection(user_id, active["id"] if active else None)
                      if user_id is not None else None)
    model_id = ((agent.client.model if agent is not None else None)
                or (selected_model["model_id"] if selected_model else settings.pi_model))
    provider = ((getattr(agent.client, "provider", None) if agent is not None else None)
                or (selected_model["provider"] if selected_model else settings.pi_provider))
    model = f"{provider}/{model_id}"
    session_name = active["name"] if active else "暂无会话"
    cards = (
        ("BOT", service), ("PI", "处理中" if busy else pi_state),
        ("当前会话", session_name), ("当前模型", model),
        ("待处理更新", str(stats["pending"])), ("失败更新", str(stats["failed"])),
    )
    cards_html = "".join(f'<div class="metric-card"><span>{_e(label)}</span><strong>{_e(value)}</strong></div>'
                         for label, value in cards)
    failed = []
    for row in storage.get_recent_failed_updates():
        error = row["last_error"] or ""
        if error.startswith("服务重启时更新执行状态不确定"):
            reason = "重启时状态不确定"
        else:
            reason = error.split(":", 1)[0].strip()[:64]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", reason):
                reason = "处理失败"
        low_risk = bool(row["safe_to_retry"])
        risk = ("处理器尚未开始；原消息已清除，可请用户重新发送"
                if low_risk else
                "处理可能已产生副作用；请先核对工具、消息和外部结果，再决定是否重发")
        notice = ("通知待发送" if row["failure_notice_pending"] else
                  "已通知用户" if row["failure_notice_sent_at"] else "无通知记录")
        details = (f'用户 {_e(row["user_id"] if row["user_id"] is not None else "未知")} · '
                   f'会话 {_e(row["session_id"] or "未关联")} · 尝试 {row["attempts"]} 次 · '
                   f'{"普通消息" if row["queueable"] else "优先更新"} · {notice}')
        failed.append(
            f'<article class="failure-card"><div class="failure-head">'
            f'<strong class="mono">#{_e(row["update_id"])}</strong><span>{_e(reason)}</span>'
            f'<span class="badge {"risk-low" if low_risk else "risk-high"}">'
            f'{"未开始处理" if low_risk else "有副作用风险"}</span></div>'
            f'<p class="failure-meta">{details}</p>'
            f'<p class="failure-meta">接收 {_e(_time_label(row["received_at"]))} · '
            f'失败 {_e(_time_label(row["processed_at"]))}</p>'
            f'<p class="failure-risk">{_e(risk)}</p>'
            f'<form method="post" action="/admin/updates/{row["update_id"]}/acknowledge">'
            f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
            f'<button class="text-button" type="submit">标记已处理</button></form></article>'
        )
    failure_html = "".join(failed) or '<p class="empty small">没有失败更新</p>'
    backup = backup_status(settings)
    backup_time = _time_label(backup.get("timestamp"))
    backup_label = ("未启用" if not backup["enabled"] else
                    "尚无成功备份" if not backup.get("archive") else
                    "已超过备份周期" if backup.get("stale") else "正常")
    backup_html = (f'<section class="section-block"><div class="section-head"><h2>数据备份</h2>'
                   f'<span class="muted">{_e(backup_label)}</span></div><div class="simple-list">'
                   f'<div class="list-row"><span>最近成功备份</span><strong>{_e(backup_time)}</strong></div>'
                   f'<div class="list-row"><span>完整会话文件</span><strong>{_e(backup.get("session_files", "—"))}</strong></div>'
                   '<p class="muted">每日备份数据库与完整会话，保留最近 7 份及 4 周副本。</p></div></section>')
    refreshed_at = datetime.now(_CST)
    refresh = (f'<form method="get" action="/admin/overview" class="overview-refresh">'
               + (f'<input type="hidden" name="user_id" value="{user_id}">' if user_id is not None else "")
               + '<button class="button subtle" type="submit">刷新</button></form>')
    body = (f'<div class="page-heading"><div><p class="eyebrow">OVERVIEW</p><h1>概览</h1></div>'
            f'<div class="overview-actions">{picker}{refresh}</div></div>'
            f'<p class="overview-timestamp">数据读取于 <time datetime="{refreshed_at.isoformat()}">'
            f'{refreshed_at.strftime("%Y-%m-%d %H:%M:%S")} 北京时间</time></p>'
            f'<div class="metric-grid">{cards_html}</div>{backup_html}'
            f'<section class="section-block"><div class="section-head"><h2>最近失败更新</h2>'
            f'<span class="muted">仅展示故障类型，不自动重放</span></div><div class="simple-list">{failure_html}</div></section>')
    return _html(_layout("概览", "overview", body, csrf, user_id, settings))



_ADMIN_PAGE_SIZE = 10


def _admin_list_page(request: Request, rows: list, section: str, user_id: int | None,
                     matches) -> tuple[list, str, str, int]:
    """搜索与分页共用同一组 GET 参数；越界页码自动回到最后一页。"""
    query = request.query_params.get("q", "").strip()[:80]
    filtered = [row for row in rows if not query or matches(row, query.casefold())]
    raw = request.query_params.get("page", "1")
    requested = int(raw) if raw.isdecimal() and 0 < len(raw) <= 6 else 1
    pages = max(1, (len(filtered) + _ADMIN_PAGE_SIZE - 1) // _ADMIN_PAGE_SIZE)
    page = min(max(1, requested), pages)
    shown = filtered[(page - 1) * _ADMIN_PAGE_SIZE:page * _ADMIN_PAGE_SIZE]
    base = {"user_id": user_id} if user_id is not None else {}
    label = {"sessions": "会话", "memories": "记忆", "summaries": "摘要", "personas": "人格"}[section]
    search = (f'<form method="get" action="/admin/{section}" class="search-form">'
              + (f'<input type="hidden" name="user_id" value="{user_id}">' if user_id is not None else "")
              + f'<input name="q" value="{_e(query)}" maxlength="80" placeholder="搜索{label}" '
              f'aria-label="搜索{label}"><button class="button subtle">搜索</button></form>')
    links = []
    for target, title in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= pages:
            params = urlencode({**base, "q": query, "page": target})
            links.append(f'<a class="button subtle" href="/admin/{section}?{_e(params)}">{title}</a>')
        else:
            links.append(f'<span class="button subtle" aria-disabled="true">{title}</span>')
    pager = (f'<nav class="list-pagination" aria-label="分页">'
             f'<span>第 {page}/{pages} 页 · 共 {len(filtered)} 条匹配</span>'
             f'{links[0]}{links[1]}</nav>')
    return shown, search, pager, len(filtered)


def _session_page(request: Request, csrf: str, user_id: int | None, error: str = "",
                  draft_name: str = "", rename_values: dict[str, str] | None = None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "sessions")
    if user_id is None:
        content = '<p class="empty">暂无用户</p>'
    else:
        add = (f'<form method="post" action="/admin/sessions/new" class="compact-form">'
               f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}">'
               '<label class="sr-only" for="session-name">会话名称</label>'
               f'<input id="session-name" name="name" value="{_e(draft_name)}" maxlength="60" placeholder="新会话名称" required>'
               '<button class="button primary" type="submit">新建会话</button></form>')
        agent = request.app.state.manager.get(user_id)
        shown, search, pager, _ = _admin_list_page(
            request, storage.list_sessions(user_id), "sessions", user_id,
            lambda row, q: q in row["name"].casefold() or q in row["id"].casefold(),
        )
        cards = []
        for row in shown:
            sid = _e(row["id"])
            session_file = row["pi_session_file"]
            exists = bool(session_file and Path(session_file).is_file())
            live = bool(agent and session_file and agent.client.has_live_session(session_file))
            file_state = ("JSONL 正常" if exists else
                          ("会话运行中" if live else ("JSONL 缺失" if session_file else "JSONL 未关联")))
            summary = storage.get_summary(row["id"])
            has_summary = bool(summary and summary["summary_text"])
            if not exists and not live and has_summary:
                file_state += " · SQLite 有摘要"
            active = row["status"] == "active"
            try:
                preview = session_recovery_preview(agent, storage, user_id, row["id"])
                preview_error = ""
            except PiError as exc:
                preview, preview_error = None, str(exc)
            if preview:
                description = _e(preview["text"]).replace("\n", "<br>")
                switch = (f'<details><summary>预览恢复{("（当前）" if active else "")}</summary>'
                          f'<p class="small">{description}</p>'
                          f'<form method="post" action="/admin/sessions/{sid}/use">'
                          f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                          f'<input type="hidden" name="user_id" value="{user_id}">'
                          f'<input type="hidden" name="recovery_version" value="{preview["version"]}">'
                          '<button class="button primary" type="submit">确认恢复并切换</button></form></details>')
            elif preview_error:
                switch = f'<span class="muted">{_e(preview_error)}</span>'
            else:
                switch = ('<span class="badge">当前</span>' if active else
                          f'<form method="post" action="/admin/sessions/{sid}/use">'
                          f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                          f'<input type="hidden" name="user_id" value="{user_id}">'
                          '<button class="text-button" type="submit">切换</button></form>')
            rename = (f'<details><summary>重命名</summary><form method="post" action="/admin/sessions/{sid}/rename" class="inline-edit">'
                      f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}">'
                      f'<input name="name" value="{_e((rename_values or {}).get(row["id"], row["name"]))}" maxlength="60" required>'
                      '<button class="button primary" type="submit">保存</button></form></details>')
            cards.append(f'<article class="summary-card"><div class="summary-head"><div>'
                         f'<span class="session-state">{_e(file_state)}</span><h2>{_e(row["name"])}</h2></div>'
                         f'<span class="mono">{sid}</span></div><div class="session-foot">'
                         f'<span class="muted">{_e(_time_label(row["updated_at"]))}</span>'
                         f'<div class="card-actions">{switch}{rename}</div></div></article>')
        content = add + search + ('<div class="card-list">' + "".join(cards) + '</div>'
                                  if cards else '<p class="empty">没有匹配的会话</p>') + pager
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'<div class="page-heading"><div><p class="eyebrow">SESSIONS</p><h1>会话管理</h1></div>{picker}</div>{alert}{content}'
    return _html(_layout("会话管理", "sessions", body, csrf, user_id, settings), status_code=400 if error else 200)


def _usage_page(request: Request, csrf: str, user_id: int | None, period: str,
                start_date: date | None = None, end_date: date | None = None,
                session_id: str | None = None, model: str | None = None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "usage")
    periods = {"today": ("今天", 0), "7d": ("近 7 天", 6), "30d": ("近 30 天", 29)}
    if start_date is not None and end_date is not None:
        label = f"{start_date.isoformat()} 至 {end_date.isoformat()}"
        since = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_CST).timestamp()
        until = (datetime(end_date.year, end_date.month, end_date.day, tzinfo=_CST)
                 + timedelta(days=1)).timestamp()
    else:
        label, days_back = periods[period]
        since = (datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=days_back)).timestamp()
        until = (datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=1)).timestamp()
    runs = storage.get_usage_runs_since(user_id, since, until, session_id, model) if user_id is not None else []
    events = storage.get_usage_events_since(user_id, since, until, session_id, model) if user_id is not None else []
    sessions = storage.list_sessions(user_id) if user_id is not None else []
    session_names = {row["id"]: row["name"] for row in sessions}
    models = storage.get_usage_models(user_id) if user_id is not None else []
    filters = {"user_id": user_id, "session_id": session_id, "model": model}
    range_links = "".join(
        f'<a class="range-link{" active" if key == period and start_date is None else ""}" '
        f'href="/admin/usage?{urlencode({"range": key, **{k: v for k, v in filters.items() if v is not None}})}">{title}</a>'
        for key, (title, _) in periods.items()
    )
    session_options = '<option value="">全部会话</option>' + "".join(
        f'<option value="{_e(row["id"])}"{" selected" if session_id == row["id"] else ""}>'
        f'{_e(row["name"])} · {_e(row["id"])}</option>' for row in sessions
    )
    model_options = '<option value="">全部模型</option>' + "".join(
        f'<option value="{_e(name)}"{" selected" if model == name else ""}>{_e(name)}</option>'
        for name in models
    )
    filter_form = (
        '<form class="usage-filters" method="get" action="/admin/usage">'
        + (f'<input type="hidden" name="user_id" value="{user_id}">' if user_id is not None else "")
        + f'<input type="hidden" name="range" value="{_e(period)}">'
        + f'<label>会话<select name="session_id">{session_options}</select></label>'
        + f'<label>模型<select name="model">{model_options}</select></label>'
        + f'<label>开始日期<input type="date" name="date_from" value="{start_date.isoformat() if start_date else ""}"></label>'
        + f'<label>结束日期<input type="date" name="date_to" value="{end_date.isoformat() if end_date else ""}"></label>'
        + '<button class="button primary" type="submit">应用筛选</button></form>'
    )
    counts = {key: sum(row[key] for row in events) for key in
              ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}
    total = sum((Decimal(row["estimated_usd"]) for row in events if row["estimated_usd"] is not None), Decimal(0))
    maintenance = sum((Decimal(row["estimated_usd"]) for row in events
                       if row["origin"] == "memory_maintenance" and row["estimated_usd"] is not None), Decimal(0))
    persona_testing = sum((Decimal(row["estimated_usd"]) for row in events
                           if row["origin"] == "persona_test" and row["estimated_usd"] is not None), Decimal(0))
    unpriced = [row for row in events if row["estimated_usd"] is None]
    cache_base = counts["input_tokens"] + counts["cache_read_tokens"]
    cache_rate = f'{counts["cache_read_tokens"] / cache_base:.0%}' if cache_base else "—"
    metrics = (("估算费用", format_usd(total)), ("聊天轮次", str(sum(r["origin"] == "chat" for r in runs))),
               ("缓存命中", cache_rate), ("记忆维护", format_usd(maintenance)),
               ("人格测试", format_usd(persona_testing)), ("未计价调用", str(len(unpriced))))
    metric_html = "".join(f'<div class="metric-card"><span>{_e(name)}</span><strong>{_e(value)}</strong></div>'
                          for name, value in metrics)
    by_model: dict[str, dict[str, object]] = {}
    by_run: dict[str, list] = {}
    for row in events:
        key = f'{row["provider"]}/{row["model"]}'
        item = by_model.setdefault(key, {"calls": 0, "tokens": 0, "cost": Decimal(0), "unpriced": 0})
        item["calls"] += 1
        item["tokens"] += row["total_tokens"]
        by_run.setdefault(row["run_id"], []).append(row)
        if row["estimated_usd"] is not None:
            item["cost"] += Decimal(row["estimated_usd"])
        else:
            item["unpriced"] += 1
    model_rows = "".join(
        f'<div class="list-row"><span>{_e(name)}</span><span>{entry["calls"]} 次 · {entry["tokens"]:,} tokens'
        f'{" · " + str(entry["unpriced"]) + " 次未计价" if entry["unpriced"] else ""}</span>'
        f'<strong>{_e(format_usd(entry["cost"]))}</strong></div>'
        for name, entry in sorted(by_model.items())
    ) or '<p class="empty small">暂无用量事件</p>'
    issues = []
    if any(row["price_status"] == "stale_price" for row in events):
        issues.append("价格待复核")
    if any(row["status"] != "complete" for row in runs):
        issues.append("存在未完成回合")
    issue_html = f'<p class="form-error">{_e("；".join(issues))}</p>' if issues else ""
    token_line = (f'未命中 {counts["input_tokens"]:,} · 缓存读取 {counts["cache_read_tokens"]:,} · '
                  f'缓存写入 {counts["cache_write_tokens"]:,} · 输出 {counts["output_tokens"]:,}')
    reasons = {
        "missing_rule": "缺少价格规则", "missing_config": "无价格配置",
        "outside_effective_period": "超出价格生效期", "calendar_unknown": "无法判断计价时段",
        "invalid_usage": "用量数据无效", "invalid_config": "价格配置无效",
    }
    raw_unpriced_page = request.query_params.get("unpriced_page", "1")
    requested_page = (int(raw_unpriced_page)
                      if raw_unpriced_page.isdecimal() and 0 < len(raw_unpriced_page) <= 6 else 1)
    unpriced_pages = max(1, (len(unpriced) + 19) // 20)
    unpriced_page = min(max(1, requested_page), unpriced_pages)
    unpriced_rows = "".join(
        f'<div class="list-row"><span>{_e(_time_label(row["started_at"]))}</span>'
        f'<span>{_e(row["provider"] + "/" + row["model"])} · '
        f'{_e(session_names.get(row["session_id"], "已删除会话" if row["session_id"] else "无关联会话"))} · '
        f'{_e(reasons.get(row["price_status"], row["price_status"]))}</span>'
        f'<strong>{row["total_tokens"]:,} tokens</strong></div>'
        for row in unpriced[(unpriced_page - 1) * 20:unpriced_page * 20]
    ) or '<p class="empty small">当前筛选范围内没有未计价调用</p>'
    unpriced_nav = []
    for target, title in ((unpriced_page - 1, "上一页"), (unpriced_page + 1, "下一页")):
        if 1 <= target <= unpriced_pages:
            params = dict(request.query_params)
            params["unpriced_page"] = target
            unpriced_nav.append(
                f'<a class="button subtle" href="/admin/usage?{_e(urlencode(params))}">{title}</a>'
            )
        else:
            unpriced_nav.append(f'<span class="button subtle" aria-disabled="true">{title}</span>')
    unpriced_pager = (f'<nav class="list-pagination" aria-label="未计价调用分页">'
                      f'<span>第 {unpriced_page}/{unpriced_pages} 页 · 共 {len(unpriced)} 次</span>'
                      f'{unpriced_nav[0]}{unpriced_nav[1]}</nav>')
    recent = []
    for run in runs[:8]:
        run_events = by_run.get(run["id"], [])
        amount = sum((Decimal(event["estimated_usd"]) for event in run_events
                      if event["estimated_usd"] is not None), Decimal(0))
        missing_count = sum(event["estimated_usd"] is None for event in run_events)
        origin = {"memory_maintenance": "记忆维护", "persona_test": "人格测试"}.get(run["origin"], "聊天")
        recent.append(f'<div class="list-row"><span>{_e(_time_label(run["started_at"]))}</span>'
                      f'<span>{origin} · {_e(session_names.get(run["session_id"], "已删除会话" if run["session_id"] else "无关联会话"))} · {_e(run["status"])}'
                      f'{" · " + str(missing_count) + " 次未计价" if missing_count else ""}</span>'
                      f'<strong>{_e(format_usd(amount))}</strong></div>')
    caption = label + (f' · {session_names[session_id]}' if session_id else "") + (f' · {model}' if model else "")
    body = (f'<div class="page-heading"><div><p class="eyebrow">USAGE</p><h1>用量费用</h1></div>{picker}</div>'
            f'<div class="range-links">{range_links}</div>{filter_form}{issue_html}<div class="metric-grid">{metric_html}</div>'
            f'<p class="usage-caption">{_e(caption)} · {token_line}</p>'
            f'<section class="section-block"><div class="section-head"><h2>模型</h2><span class="muted">按公开费率估算</span></div>'
            f'<div class="simple-list">{model_rows}</div></section>'
            f'<section class="section-block"><div class="section-head"><h2>未计价调用</h2>'
            f'<span class="muted">不计入估算费用 · 共 {len(unpriced)} 次</span></div>'
            f'<div class="simple-list">{unpriced_rows}</div>{unpriced_pager}</section>'
            f'<section class="section-block"><div class="section-head"><h2>最近记录</h2></div>'
            f'<div class="simple-list">{"".join(recent) or "<p class=\"empty small\">暂无记录</p>"}</div></section>')
    return _html(_layout("用量费用", "usage", body, csrf, user_id, settings))


def _memory_page(request: Request, csrf: str, user_id: int | None, error: str = "",
                 draft_text: str = "", edit_values: dict[str, str] | None = None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "memories")
    if user_id is None:
        contents = '<p class="empty">暂无用户</p>'
    else:
        memories = storage.list_memories(user_id)
        capacity = storage.memory_capacity_status(user_id, settings.memory_active_limit)
        shown, search, pager, matched = _admin_list_page(
            request, memories, "memories", user_id,
            lambda row, q: q in row["text"].casefold() or q in row["source"].casefold(),
        )
        add = f'''<form method="post" action="/admin/memories/add" class="add-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><label class="sr-only" for="new-memory">新增记忆</label><textarea id="new-memory" name="text" maxlength="{settings.memory_item_max_chars}" rows="2" placeholder="写入一条长期记忆" required>{_e(draft_text)}</textarea><button class="button primary" type="submit">添加记忆</button></form>'''
        cards = []
        for row in shown:
            ident = _e(row["id"])
            badge = '<span class="badge">置顶</span>' if row["pinned"] else ""
            source = "手动" if row["source"] == "manual" else "自动"
            cards.append(f'''<article class="memory-card"><div class="card-meta"><span>{source}</span>{badge}<span class="mono">{ident}</span></div><p class="memory-text">{_e(row["text"])}</p><div class="card-actions"><form method="post" action="/admin/memories/{ident}/pin"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><input type="hidden" name="pinned" value="{0 if row["pinned"] else 1}"><button class="text-button" type="submit">{'取消置顶' if row['pinned'] else '置顶'}</button></form><details><summary>编辑</summary><form method="post" action="/admin/memories/{ident}/edit" class="inline-edit"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><textarea name="text" maxlength="{settings.memory_item_max_chars}" rows="3" required>{_e((edit_values or {}).get(row["id"], row["text"]))}</textarea><button class="button primary" type="submit">保存</button></form></details><details class="danger-details"><summary>删除</summary><form method="post" action="/admin/memories/{ident}/delete"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><button class="button danger" type="submit">确认删除</button></form></details></div></article>''')
        cards_html = "".join(cards) if cards else '<p class="empty">暂无记忆</p>'
        capacity_notice = ('<p class="memory-capacity" role="status">记忆容量已满，'
                           '新记忆会暂时跳过。请核对并合并重复或冲突的内容；置顶条目会保留。</p>'
                           if capacity["full"] else '')
        organize_link = f'/admin/memories?{urlencode({"user_id": user_id, "organize": "1"})}'
        organize = (f'<section class="section-block"><div class="section-head"><h2>记忆整理</h2>'
                    f'<a class="button subtle" href="{_e(organize_link)}">查找重复与冲突</a></div>'
                    '<p class="muted">建议仅供核对，确认合并前不会修改或删除任何记忆。</p>')
        if request.query_params.get("organize") == "1":
            candidates = storage.memory_organization_candidates(user_id)
            for candidate in candidates:
                before = "".join(f'<p class="memory-text">{_e(row["text"])}</p>' for row in candidate["rows"])
                label = "可能冲突" if candidate["kind"] == "possible_conflict" else "表达相近"
                organize += (f'<article class="memory-card"><span class="badge">{label}</span>'
                             f'{before}<p class="muted">{_e(candidate["reason"])}</p>'
                             f'<form method="post" action="/admin/memories/merge" class="inline-edit">'
                             f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                             f'<input type="hidden" name="user_id" value="{user_id}">'
                             f'<input type="hidden" name="ids" value="{_e(",".join(candidate["ids"]))}">'
                             f'<input type="hidden" name="version" value="{_e(candidate["version"])}">'
                             f'<label>核对后的记忆<textarea name="text" maxlength="{settings.memory_item_max_chars}" required>'
                             f'{_e(candidate["suggested_text"])}</textarea></label>'
                             '<button class="button primary" type="submit">确认合并以上条目</button></form></article>')
            if not candidates:
                organize += '<p class="empty small">没有找到明显重复或冲突；可在列表中手动编辑、删除。</p>'
        organize += '</section>'
        contents = (f'<div class="toolbar"><span class="count">{matched} 条匹配 · '
                    f'{len(memories)} / {settings.memory_active_limit}</span>{search}</div>'
                    f'{capacity_notice}{add}<div class="card-list">{cards_html}</div>{pager}{organize}')
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'<div class="page-heading"><div><p class="eyebrow">MEMORY</p><h1>长期记忆</h1></div>{picker}</div>{alert}{contents}'
    return _html(_layout("长期记忆", "memories", body, csrf, user_id, settings), status_code=400 if error else 200)


def _summary_page(request: Request, csrf: str, user_id: int | None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "summaries")
    summaries = storage.list_admin_summaries(user_id) if user_id is not None else []
    shown, search, pager, _ = _admin_list_page(
        request, summaries, "summaries", user_id,
        lambda row, q: q in row["name"].casefold() or q in row["id"].casefold()
        or q in (row["summary_text"] or "").casefold(),
    )
    cards = []
    for row in shown:
        summary = row["summary_text"] or ""
        ident = _e(row["id"])
        if summary:
            clear = f'''<details class="danger-details"><summary>清空摘要</summary><form method="post" action="/admin/summaries/{ident}/clear"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><button class="button danger" type="submit">确认清空</button></form></details>'''
            content = f'<p class="summary-text">{_e(summary)}</p>'
        else:
            clear, content = "", '<p class="empty small">暂无摘要</p>'
        cards.append(f'<article class="summary-card"><div class="summary-head"><div><span class="session-state">{_e(row["status"])}</span><h2>{_e(row["name"])}</h2></div><span class="mono">{ident}</span></div>{content}<div class="card-actions">{clear}</div></article>')
    content = "".join(cards) if cards else '<p class="empty">暂无会话</p>'
    body = (f'<div class="page-heading"><div><p class="eyebrow">SESSIONS</p><h1>会话摘要</h1></div>{picker}</div>'
            f'{_notice(request)}{search}<div class="card-list">{content}</div>{pager}')
    return _html(_layout("会话摘要", "summaries", body, csrf, user_id, settings))


def _personas_page(request: Request, csrf: str, user_id: int | None, error: str = "") -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "personas")
    active = storage.get_active_persona(user_id) if user_id is not None else None
    personas = storage.list_personas()
    all_rows = ([{"id": "none", "name": "无人格 · Pi 默认", "identity": ""}]
                if user_id is not None else []) + personas
    shown, search, pager, matched = _admin_list_page(
        request, all_rows, "personas", user_id,
        lambda row, q: q in row["name"].casefold() or q in row["id"].casefold(),
    )
    cards = []
    if any(row["id"] == "none" for row in shown):
        none_selected = active is None
        activate_none = (f'<form method="post" action="/admin/personas/none/use">'
                         f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                         f'<input type="hidden" name="user_id" value="{user_id}">'
                         f'<button class="text-button" type="submit">切换为当前人格</button></form>'
                         if not none_selected else "")
        cards.append(f'<article class="memory-card"><div class="card-meta">'
                     f'<span class="badge">{"当前" if none_selected else "可用"}</span>'
                     f'<span class="mono">none</span></div><h2>无人格 · Pi 默认</h2>'
                     f'<div class="card-actions">{activate_none}</div></article>')
    for row in shown:
        if row["id"] == "none":
            continue
        persona_id = row["id"]
        selected = bool(active and active["id"] == persona_id)
        badge = ("当前" if selected else "默认" if persona_id == "default" else
                 "可用" if row["enabled"] else "已停用")
        activate = (f'<form method="post" action="/admin/personas/{persona_id}/use">'
                    f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                    f'<input type="hidden" name="user_id" value="{user_id}">'
                    f'<button class="text-button" type="submit">切换为当前人格</button></form>'
                    if user_id is not None and row["enabled"] and not selected else "")
        toggle = (f'<form method="post" action="/admin/personas/{persona_id}/toggle">'
                  f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                  f'<input type="hidden" name="user_id" value="{user_id or ""}">'
                  f'<input type="hidden" name="enabled" value="{0 if row["enabled"] else 1}">'
                  f'<button class="text-button" type="submit">{"停用" if row["enabled"] else "启用"}</button></form>'
                  if persona_id != "default" else "")
        delete = (f'<details class="danger-details"><summary>删除</summary>'
                  f'<form method="post" action="/admin/personas/{persona_id}/delete">'
                  f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                  f'<input type="hidden" name="user_id" value="{user_id or ""}">'
                  f'<button class="button danger" type="submit">确认删除</button></form></details>'
                  if persona_id != "default" else "")
        cards.append(f'<article class="memory-card"><div class="card-meta"><span class="badge">{badge}</span>'
                     f'<span class="mono">{_e(persona_id)}</span></div><h2>{_e(row["name"])}</h2>'
                     f'<p class="persona-excerpt">{_e(row["identity"][:180])}</p>'
                     f'<div class="card-actions"><a href="/admin/personas/{persona_id}'
                     f'{"?" + urlencode({"user_id": user_id}) if user_id is not None else ""}">编辑 · 预览 · 测试</a>'
                     f'{activate}{toggle}{delete}</div></article>')
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = (f'<div class="page-heading"><div><p class="eyebrow">PERSONAS</p><h1>人格管理</h1></div>{picker}</div>'
            f'{alert}<div class="toolbar"><span class="count">{matched} 条匹配 · {len(personas)} 个人格</span>'
            f'{search}<a class="button primary" href="/admin/personas/new'
            f'{"?" + urlencode({"user_id": user_id}) if user_id is not None else ""}">创建人格</a></div>'
            f'<div class="card-list">{"".join(cards) or "<p class=\"empty\">没有匹配的人格</p>"}</div>{pager}')
    return _html(_layout("人格管理", "personas", body, csrf, user_id, settings),
                 status_code=400 if error else 200)


def _persona_form_page(request: Request, csrf: str, user_id: int | None, row=None,
                       values: dict[str, str] | None = None, error: str = "",
                       preview: str | None = None, test_result: str | None = None,
                       question: str = "") -> HTMLResponse:
    current = values if values is not None else (dict(row) if row is not None else {})
    persona_id = row["id"] if row is not None else None
    title = "编辑人格" if row is not None else "创建人格"
    action = f"/admin/personas/{persona_id}" if persona_id else "/admin/personas"
    fields = []
    for key, label in PERSONA_FIELDS:
        required = " required" if key == "identity" else ""
        fields.append(f'<label class="persona-field"><span>{_e(label)}</span>'
                      f'<textarea name="{key}" rows="4" maxlength="4000"{required}>'
                      f'{_e(current.get(key, ""))}</textarea></label>')
    actual = preview if preview is not None else (render_persona(row) if row is not None else "")
    prompt_html = (f'<section class="settings-group"><div class="section-head"><h2>生成的追加提示词</h2>'
                   f'<span class="muted">{len(actual.encode("utf-8")):,} 字节</span></div>'
                   f'<pre class="persona-prompt">{_e(actual)}</pre></section>' if actual else "")
    result_html = (f'<section class="settings-group"><h2>测试回复</h2>'
                   f'<p class="persona-result">{_e(test_result)}</p></section>' if test_result is not None else "")
    test_html = (f'<section class="settings-group"><h2>独立测试</h2>'
                 f'<form method="post" action="/admin/personas/{persona_id}/test" class="persona-form">'
                 f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                 f'<input type="hidden" name="user_id" value="{user_id or ""}">'
                 f'<label class="persona-field"><span>测试消息</span>'
                 f'<textarea name="question" rows="3" maxlength="1000" required>{_e(question)}</textarea></label>'
                 f'<div class="form-footer"><button class="button subtle" type="submit">运行测试</button></div>'
                 f'</form></section>' if persona_id else "")
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = (f'<div class="page-heading"><div><p class="eyebrow">PERSONAS</p><h1>{title}</h1></div>'
            f'<a class="button subtle" href="/admin/personas">返回列表</a></div>{alert}'
            f'<section class="settings-group"><form method="post" action="{action}" class="persona-form">'
            f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
            f'<input type="hidden" name="user_id" value="{user_id or ""}">'
            f'<label class="persona-field"><span>名称</span><input name="name" value="{_e(current.get("name", ""))}" maxlength="60" required></label>'
            f'{"".join(fields)}<div class="form-footer persona-actions">'
            f'<button class="button subtle" type="submit" name="action" value="preview">预览提示词</button>'
            f'<button class="button primary" type="submit" name="action" value="save">保存人格</button>'
            f'</div></form></section>{prompt_html}{test_html}{result_html}')
    return _html(_layout(title, "personas", body, csrf, user_id, _settings(request)),
                 status_code=400 if error else 200)


def _settings_page(request: Request, csrf: str, error: str = "",
                   values: dict[str, str] | None = None) -> HTMLResponse:
    settings = _settings(request)
    saved = _storage(request).get_runtime_settings()
    defaults = Settings()
    overridden = {key for key in saved if key in _FIELD_KEYS or key == "memory_auto_extract"}
    groups = (
        ("自动整理", _FIELDS[:7]),
        ("记忆边界", _FIELDS[7:]),
    )
    sections = []
    for group_name, fields in groups:
        rows = "".join(
            f'<label class="setting-field"><span>{_e(label)}</span><span class="input-with-unit"><input type="number" name="{key}" min="{low}" max="{high}" value="{_e(values.get(key, getattr(settings, key)) if values is not None else getattr(settings, key))}" required><span>{_e(unit)}</span></span>'
            f'{"<small>环境默认：" + str(getattr(defaults, key)) + "</small>" if key in overridden else ""}</label>'
            for key, label, unit, low, high in fields
        )
        sections.append(f'<section class="settings-group"><h2>{group_name}</h2><div class="settings-grid">{rows}</div></section>')
    checked = " checked" if ((values.get("memory_auto_extract") == "on") if values is not None
                             else settings.memory_auto_extract) else ""
    source = "SQLite 覆盖" if overridden else ".env"
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'''<div class="page-heading"><div><p class="eyebrow">CONFIGURATION</p><h1>记忆设置</h1></div><span class="source-label">{source}</span></div>{alert}
<form method="post" action="/admin/settings" class="settings-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><label class="switch-row"><span>自动提取</span><input type="checkbox" name="memory_auto_extract"{checked}></label>{''.join(sections)}<div class="form-footer"><button class="button primary" type="submit">保存设置</button></div></form>'''
    if overridden:
        body += (f'<details class="settings-restore"><summary>恢复 .env 默认值</summary>'
                 f'<p>将移除 {len(overridden)} 项 SQLite 覆盖，并立即采用 .env 中的记忆参数。</p>'
                 f'<form method="post" action="/admin/settings/restore">'
                 f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                 '<button class="button danger" type="submit">确认恢复默认值</button></form></details>')
    return _html(_layout("记忆设置", "settings", body, csrf, None, settings), status_code=400 if error else 200)


def _mail_page(request: Request, csrf: str, error: str = "",
               values: dict[str, str] | None = None, test_to: str = "") -> HTMLResponse:
    current = _storage(request).get_smtp_settings()
    values = values or {}
    host = _e(values.get("host", current["host"] if current else ""))
    port = str(values.get("port", current["port"] if current else 465))
    username = _e(values.get("username", current["username"] if current else ""))
    from_address = _e(values.get("from_address", current["from_address"] if current else ""))
    secret_hint = "已保存，留空保持原值" if current else "SMTP 密码或授权码"
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    test = (f'''<section class="settings-group"><h2>发送测试</h2>
<form method="post" action="/admin/mail/test" class="mail-test-form">
<input type="hidden" name="csrf" value="{_e(csrf)}">
<label class="mail-field"><span>收件邮箱</span><input type="email" name="to" value="{_e(test_to)}" autocomplete="off" maxlength="254" required></label>
<button class="button subtle" type="submit">发送测试邮件</button></form></section>''' if current else "")
    body = f'''<div class="page-heading"><div><p class="eyebrow">CONFIGURATION</p><h1>邮件设置</h1></div>
<span class="source-label">{'SQLite' if current else '未配置'}</span></div>{alert}
<section class="settings-group"><h2>SMTP</h2>
<form method="post" action="/admin/mail" class="mail-form">
<input type="hidden" name="csrf" value="{_e(csrf)}">
<label class="mail-field"><span>服务器</span><input name="host" value="{host}" maxlength="253" placeholder="mail.example.com" required></label>
<label class="mail-field"><span>端口与加密</span><select name="port"><option value="465"{' selected' if port == "465" else ''}>465 · SSL/TLS</option><option value="587"{' selected' if port == "587" else ''}>587 · STARTTLS</option></select></label>
<label class="mail-field"><span>用户名</span><input name="username" value="{username}" autocomplete="off" maxlength="254" required></label>
<label class="mail-field"><span>发件邮箱</span><input type="email" name="from_address" value="{from_address}" autocomplete="off" maxlength="254" required></label>
<label class="mail-field"><span>密码 / 授权码</span><input type="password" name="password" autocomplete="new-password" maxlength="1024" placeholder="{secret_hint}"{' required' if not current else ''}></label>
<div class="form-footer"><button class="button primary" type="submit">保存配置</button></div></form></section>{test}'''
    return _html(_layout("邮件设置", "mail", body, csrf, None, _settings(request)),
                 status_code=400 if error else 200)


def _client_ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    if direct in {"127.0.0.1", "::1"}:
        return request.headers.get("x-real-ip", direct)[:64]
    return direct[:64]


def _refresh_memory(request: Request, user_id: int) -> None:
    manager = request.app.state.manager
    memory = getattr(manager, "memory", None)
    agent = manager._agents.get(user_id)
    if memory is None or agent is None or agent.client.busy:
        return
    active = _storage(request).get_active_session(user_id)
    if active:
        try:
            memory.refresh_context(user_id, active["id"], active["pi_session_file"])
        except Exception:
            logger.exception("后台变更后刷新记忆上下文失败", extra={"user_id": user_id})


@router.get("")
@router.get("/")
async def home(request: Request):
    return _redirect("/admin/overview" if _session(request) else "/admin/login")


@router.get("/login")
async def login_page(request: Request):
    settings = _settings(request)
    if not settings.admin_username or not settings.admin_password:
        return _html('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>未配置</title><body>后台未配置</body></html>', status_code=503)
    if _session(request):
        return _redirect("/admin/overview")
    csrf = secrets.token_urlsafe(24)
    response = _html(_login_page(csrf))
    _cookie(response, _PRELOGIN_COOKIE, _seal(settings, "pre", csrf), _PRELOGIN_SECONDS)
    return response


@router.post("/login")
async def login(request: Request):
    settings = _settings(request)
    if not settings.admin_username or not settings.admin_password:
        raise HTTPException(status_code=503, detail="后台未配置")
    form = await _form(request)
    expected = _unseal(settings, request.cookies.get(_PRELOGIN_COOKIE), "pre")
    if not expected or not _same_secret(form.get("csrf", ""), expected):
        raise HTTPException(status_code=403, detail="页面已过期，请刷新后重试")
    ip = _client_ip(request)
    now = time.monotonic()
    failures = [value for value in _LOGIN_FAILURES.get(ip, []) if now - value < 900]
    if len(failures) >= 8:
        raise HTTPException(status_code=429, detail="请稍后再试")
    valid_user = _same_secret(form.get("username", ""), settings.admin_username)
    valid_password = _same_secret(form.get("password", ""), settings.admin_password)
    if not (valid_user and valid_password):
        _LOGIN_FAILURES[ip] = failures + [now]
        csrf = secrets.token_urlsafe(24)
        response = _html(_login_page(csrf, "用户名或密码错误", form.get("username", "")), status_code=401)
        _cookie(response, _PRELOGIN_COOKIE, _seal(settings, "pre", csrf), _PRELOGIN_SECONDS)
        logger.warning("后台登录失败", extra={"client_ip": ip})
        await asyncio.sleep(0.2)
        return response
    _LOGIN_FAILURES.pop(ip, None)
    csrf = secrets.token_urlsafe(24)
    response = _redirect("/admin/overview")
    _cookie(response, _SESSION_COOKIE, _seal(settings, "auth", csrf), _SESSION_SECONDS)
    response.delete_cookie(_PRELOGIN_COOKIE, path="/admin")
    logger.info("后台登录成功", extra={"client_ip": ip})
    return response


@router.post("/logout")
async def logout(request: Request):
    await _authorized_form(request)
    response = _redirect("/admin/login")
    response.delete_cookie(_SESSION_COOKIE, path="/admin")
    return response


@router.get("/overview")
async def overview(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _overview_page(request, csrf, user_id)


@router.get("/sessions")
async def sessions(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _session_page(request, csrf, user_id)


@router.post("/updates/{update_id}/acknowledge")
async def acknowledge_update(update_id: int, request: Request):
    await _authorized_form(request)
    if not _storage(request).acknowledge_failed_update(update_id):
        raise HTTPException(status_code=404, detail="失败更新不存在")
    logger.info("后台标记失败更新已处理", extra={"update_id": update_id})
    return _redirect("/admin/overview")


def _session_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not 1 <= len(name) <= 60 or any(ord(char) < 32 for char in name):
        raise ValueError("会话名称须为 1–60 个字符，且不能包含换行")
    return name


@router.post("/sessions/new")
async def new_session(request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    try:
        name = _session_name(form.get("name"))
    except ValueError as exc:
        return _session_page(request, csrf, user_id, str(exc), draft_name=form.get("name", ""))
    agent = request.app.state.manager.get(user_id)
    try:
        async with agent.control_lock:
            sid = await create_user_session(agent, storage, user_id, name)
    except (PiError, ValueError) as exc:
        return _session_page(request, csrf, user_id, str(exc), draft_name=form.get("name", ""))
    except Exception:
        logger.exception("后台新建会话失败", extra={"user_id": user_id})
        return _session_page(request, csrf, user_id, "新建失败，请查看服务日志", draft_name=form.get("name", ""))
    logger.info("后台新建会话", extra={"user_id": user_id, "session_id": sid})
    return _redirect(f"/admin/sessions?{urlencode({'user_id': user_id, 'status': 'created'})}")


@router.post("/sessions/{session_id}/use")
async def use_session(session_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    row = storage.get_session(session_id)
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    agent = request.app.state.manager.get(user_id)
    try:
        async with agent.control_lock:
            notice = await switch_user_session(agent, storage, user_id, session_id,
                                               str(form.get("recovery_version") or ""))
    except (PiError, ValueError) as exc:
        return _session_page(request, csrf, user_id, str(exc))
    except Exception:
        logger.exception("后台切换会话失败", extra={"user_id": user_id, "session_id": session_id})
        return _session_page(request, csrf, user_id, "切换失败，请查看服务日志")
    logger.info("后台切换会话", extra={"user_id": user_id, "session_id": session_id, "recovered": bool(notice)})
    status = "recovered" if notice else "switched"
    return _redirect(f"/admin/sessions?{urlencode({'user_id': user_id, 'status': status})}")


@router.post("/sessions/{session_id}/rename")
async def rename_session(session_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    row = storage.get_session(session_id)
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        name = _session_name(form.get("name"))
    except ValueError as exc:
        return _session_page(request, csrf, user_id, str(exc), rename_values={session_id: form.get("name", "")})
    storage.rename_session(session_id, name)
    logger.info("后台重命名会话", extra={"user_id": user_id, "session_id": session_id})
    return _redirect(f"/admin/sessions?{urlencode({'user_id': user_id, 'status': 'saved'})}")


@router.get("/usage")
async def usage(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    period = request.query_params.get("range", "today")
    if period not in {"today", "7d", "30d"}:
        raise HTTPException(status_code=400, detail="时间范围无效")
    storage = _storage(request)
    session_id = request.query_params.get("session_id") or None
    if session_id:
        row = storage.get_session(session_id)
        if row is None or row["user_id"] != user_id:
            raise HTTPException(status_code=400, detail="会话筛选无效")
    model = request.query_params.get("model") or None
    if model and (user_id is None or model not in storage.get_usage_models(user_id)):
        raise HTTPException(status_code=400, detail="模型筛选无效")
    raw_start = request.query_params.get("date_from") or None
    raw_end = request.query_params.get("date_to") or None
    try:
        start_date = date.fromisoformat(raw_start) if raw_start else None
        end_date = date.fromisoformat(raw_end) if raw_end else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式无效") from exc
    if start_date and not end_date:
        end_date = datetime.now(_CST).date()
    elif end_date and not start_date:
        start_date = end_date
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    return _usage_page(request, csrf, user_id, period, start_date, end_date, session_id, model)


@router.get("/memories")
async def memories(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _memory_page(request, csrf, user_id)


@router.post("/memories/add")
async def add_memory(request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    item = form.get("text", "").strip()
    if not item or len(item) > settings.memory_item_max_chars or looks_secret(item):
        return _memory_page(request, csrf, user_id, "记忆内容为空、过长或包含敏感凭据", draft_text="" if looks_secret(item) else form.get("text", ""))
    try:
        storage.ensure_user(user_id, None)
        memory_id = storage.add_memory(user_id, item, limit=settings.memory_active_limit)
    except ValueError as exc:
        return _memory_page(request, csrf, user_id, str(exc), draft_text="" if looks_secret(item) else form.get("text", ""))
    _refresh_memory(request, user_id)
    logger.info("后台添加记忆", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'status': 'saved'})}")


@router.post("/memories/merge")
async def merge_memories(request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    try:
        memory_id = storage.merge_memories(
            user_id, form.get("ids", "").split(","), form.get("text", ""),
            form.get("version", ""), max_chars=settings.memory_item_max_chars,
        )
    except ValueError as exc:
        return _memory_page(request, csrf, user_id, str(exc))
    _refresh_memory(request, user_id)
    logger.info("后台确认合并记忆", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'organize': '1', 'status': 'saved'})}")


@router.post("/memories/{memory_id}/edit")
async def edit_memory(memory_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    item = form.get("text", "").strip()
    if not item or len(item) > settings.memory_item_max_chars or looks_secret(item):
        return _memory_page(request, csrf, user_id, "记忆内容为空、过长或包含敏感凭据", edit_values={memory_id: "" if looks_secret(item) else form.get("text", "")})
    try:
        changed = storage.edit_memory(user_id, memory_id, item)
    except sqlite3.IntegrityError:
        return _memory_page(request, csrf, user_id, "相同记忆已存在", edit_values={memory_id: "" if looks_secret(item) else form.get("text", "")})
    if not changed:
        raise HTTPException(status_code=404, detail="记忆不存在")
    _refresh_memory(request, user_id)
    logger.info("后台编辑记忆", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'status': 'saved'})}")


@router.post("/memories/{memory_id}/pin")
async def pin_memory(memory_id: str, request: Request):
    _, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    if form.get("pinned") not in {"0", "1"}:
        raise HTTPException(status_code=400, detail="请求无效")
    if not storage.pin_memory(user_id, memory_id, form["pinned"] == "1"):
        raise HTTPException(status_code=404, detail="记忆不存在")
    _refresh_memory(request, user_id)
    logger.info("后台调整记忆置顶", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'status': 'pinned'})}")


@router.post("/memories/{memory_id}/delete")
async def delete_memory(memory_id: str, request: Request):
    _, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    if not storage.delete_memory(user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    _refresh_memory(request, user_id)
    logger.info("后台删除记忆", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'status': 'deleted'})}")


@router.get("/summaries")
async def summaries(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _summary_page(request, csrf, user_id)


@router.post("/summaries/{session_id}/clear")
async def clear_summary(session_id: str, request: Request):
    _, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    row = storage.get_session(session_id)
    if user_id is None or row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    storage.clear_summary(session_id)
    _refresh_memory(request, user_id)
    logger.info("后台清空会话摘要", extra={"user_id": user_id, "session_id": session_id})
    return _redirect(f"/admin/summaries?{urlencode({'user_id': user_id, 'status': 'cleared'})}")


@router.get("/settings")
async def settings_page(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    return _settings_page(request, csrf)


@router.post("/settings")
async def save_settings(request: Request):
    csrf, form = await _authorized_form(request)
    settings, storage = _settings(request), _storage(request)
    try:
        values = _memory_values(form, from_form=True)
        _validated_candidate(settings, values)
    except ValueError as exc:
        return _settings_page(request, csrf, str(exc), form)
    storage.save_runtime_settings(values)
    for key, value in values.items():
        setattr(settings, key, value)
    logger.info("后台更新记忆参数", extra={"keys": sorted(values)})
    return _redirect("/admin/settings?status=saved")


@router.post("/settings/restore")
async def restore_settings(request: Request):
    csrf, _ = await _authorized_form(request)
    settings, storage = _settings(request), _storage(request)
    defaults = Settings()
    values = {key: getattr(defaults, key) for key in _FIELD_KEYS | {"memory_auto_extract"}}
    try:
        _validated_candidate(settings, values)
    except ValueError as exc:
        return _settings_page(request, csrf, str(exc))
    storage.delete_runtime_settings(_FIELD_KEYS | {"memory_auto_extract"})
    for key, value in values.items():
        setattr(settings, key, value)
    logger.info("后台恢复 .env 记忆参数")
    return _redirect("/admin/settings?status=restored")


@router.get("/mail")
async def mail_page(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    return _mail_page(request, csrf)


@router.post("/mail")
async def save_mail(request: Request):
    csrf, form = await _authorized_form(request)
    storage = _storage(request)
    current = storage.get_smtp_settings()
    password = form.get("password", "") or (current["password"] if current else "")
    try:
        values = validate_smtp_settings(form.get("host", ""), form.get("port", ""),
                                        form.get("username", ""), password,
                                        form.get("from_address", ""))
    except ValueError as exc:
        return _mail_page(request, csrf, str(exc), form)
    storage.save_smtp_settings(**values)
    logger.info("后台保存 SMTP 配置", extra={"host": values["host"], "port": values["port"]})
    return _redirect("/admin/mail?status=saved")


@router.get("/personas")
async def personas_page(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _personas_page(request, csrf, user_id)


@router.get("/personas/new")
async def new_persona_page(request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _persona_form_page(request, csrf, user_id)


@router.post("/personas")
async def create_persona(request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _selected_user(form.get("user_id"), storage, settings)
    try:
        values = validate_persona(form)
        if form.get("action") == "preview":
            return _persona_form_page(request, csrf, user_id, values=values,
                                      preview=render_persona(values))
        if form.get("action") != "save":
            raise ValueError("操作无效")
        persona_id = storage.create_persona(values)
    except (ValueError, sqlite3.IntegrityError) as exc:
        message = "人格名称已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
        return _persona_form_page(request, csrf, user_id, values=form, error=message)
    logger.info("后台创建人格", extra={"persona_id": persona_id})
    return _redirect(f"/admin/personas/{persona_id}?{urlencode({'status': 'created', **({'user_id': user_id} if user_id is not None else {})})}")


@router.get("/personas/{persona_id}")
async def persona_detail(persona_id: str, request: Request):
    csrf = _session(request)
    if not csrf:
        return _redirect("/admin/login")
    row = _storage(request).get_persona(persona_id)
    if row is None:
        raise HTTPException(status_code=404, detail="人格不存在")
    user_id = _selected_user(request.query_params.get("user_id"), _storage(request), _settings(request))
    return _persona_form_page(request, csrf, user_id, row)


@router.post("/personas/{persona_id}")
async def edit_persona(persona_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    row = storage.get_persona(persona_id)
    if row is None:
        raise HTTPException(status_code=404, detail="人格不存在")
    user_id = _selected_user(form.get("user_id"), storage, settings)
    try:
        values = validate_persona(form)
        if form.get("action") == "preview":
            return _persona_form_page(request, csrf, user_id, row, values=values,
                                      preview=render_persona(values))
        if form.get("action") != "save":
            raise ValueError("操作无效")
        storage.update_persona(persona_id, values)
    except (ValueError, sqlite3.IntegrityError) as exc:
        message = "人格名称已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
        return _persona_form_page(request, csrf, user_id, row, values=form, error=message)
    logger.info("后台更新人格", extra={"persona_id": persona_id})
    return _redirect(f"/admin/personas/{persona_id}?{urlencode({'status': 'saved', **({'user_id': user_id} if user_id is not None else {})})}")


@router.post("/personas/{persona_id}/use")
async def use_persona(persona_id: str, request: Request):
    _, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    try:
        storage.set_active_persona(user_id, persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("后台切换人格", extra={"user_id": user_id, "persona_id": persona_id})
    return _redirect(f"/admin/personas?{urlencode({'user_id': user_id, 'status': 'switched'})}")


@router.post("/personas/{persona_id}/toggle")
async def toggle_persona(persona_id: str, request: Request):
    _, form = await _authorized_form(request)
    storage = _storage(request)
    if form.get("enabled") not in {"0", "1"}:
        raise HTTPException(status_code=400, detail="操作无效")
    try:
        changed = storage.set_persona_enabled(persona_id, form["enabled"] == "1")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="人格不存在")
    logger.info("后台调整人格状态", extra={"persona_id": persona_id, "enabled": form["enabled"] == "1"})
    return _redirect("/admin/personas?status=saved")


@router.post("/personas/{persona_id}/delete")
async def delete_persona(persona_id: str, request: Request):
    await _authorized_form(request)
    try:
        deleted = _storage(request).delete_persona(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="人格不存在")
    logger.info("后台删除人格", extra={"persona_id": persona_id})
    return _redirect("/admin/personas?status=deleted")


async def _run_persona_test(request: Request, row, user_id: int, question: str) -> str:
    settings, storage = _settings(request), _storage(request)
    manager = getattr(request.app.state, "manager", None)
    agent = getattr(manager, "_agents", {}).get(user_id) if manager is not None else None
    provider = (agent.client.provider if agent is not None else None) or settings.pi_provider
    model = (agent.client.model if agent is not None else None) or settings.pi_model
    with tempfile.TemporaryDirectory(prefix="dj-pi-persona-") as folder:
        path = Path(folder) / "persona.md"
        write_prompt(path, render_persona(row))
        client = PiRpcClient(
            settings.pi_cmd, str(Path(folder) / "sessions"),
            provider=provider, model=model,
            extra_args=["--no-session", "--no-tools", "--no-extensions", "--no-context-files",
                        "--no-skills", "--no-prompt-templates", "--append-system-prompt", str(path)],
        )
        run_id = storage.start_usage_run(user_id, None, None, None, origin="persona_test")
        seq = 0

        def on_usage(event: dict) -> None:
            nonlocal seq
            seq += 1
            price = PricingCatalog(settings.pricing_file).price(
                event["provider"], event["model"], event["usage"], event["occurred_at"]
            )
            storage.record_usage_event(run_id, seq, event, price)

        complete = False
        try:
            parts = []
            async with asyncio.timeout(120):
                async for part in client.prompt(question, on_usage=on_usage):
                    parts.append(part)
                    if sum(len(item) for item in parts) > 8000:
                        raise ValueError("测试回复超过 8000 字符")
            complete = True
            answer = "".join(parts).strip() or "（模型没有返回文字）"
            events = storage.get_usage_events(run_id)
            prices = [Decimal(event["estimated_usd"]) for event in events
                      if event["estimated_usd"] is not None]
            cost = ("用量未返回" if not events else "未完整计价" if len(prices) < len(events)
                    else format_usd(sum(prices, Decimal(0))))
            return f"{answer}\n\n模型：{provider}/{model} · 测试估算费用：{cost}"
        finally:
            await client.stop()
            storage.finish_usage_run(run_id, "complete" if complete else "failed", None)


@router.post("/personas/{persona_id}/test")
async def test_persona(persona_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    row = storage.get_persona(persona_id)
    if row is None:
        raise HTTPException(status_code=404, detail="人格不存在")
    user_id = _form_user(form, storage, settings)
    question = form.get("question", "").strip()
    if not 1 <= len(question) <= 1000:
        return _persona_form_page(request, csrf, user_id, row, error="测试消息须为 1–1000 字符", question=question)
    try:
        result = await _run_persona_test(request, row, user_id, question)
    except (PiError, ValueError, TimeoutError) as exc:
        return _persona_form_page(request, csrf, user_id, row,
                                  error=f"测试失败：{str(exc)[:160]}", question=question)
    except Exception:
        logger.exception("后台人格测试失败", extra={"persona_id": persona_id, "user_id": user_id})
        return _persona_form_page(request, csrf, user_id, row,
                                  error="测试失败，请查看服务日志", question=question)
    logger.info("后台人格测试完成", extra={"persona_id": persona_id, "user_id": user_id})
    return _persona_form_page(request, csrf, user_id, row, test_result=result, question=question)


@router.post("/mail/test")
async def test_mail(request: Request):
    csrf, form = await _authorized_form(request)
    config = _storage(request).get_smtp_settings()
    if config is None:
        return _mail_page(request, csrf, "请先保存 SMTP 配置", test_to=form.get("to", ""))
    try:
        recipient = email_address(form.get("to", ""))
        await asyncio.to_thread(send_email, config, recipient, "Pi 邮件功能测试", "这是一封来自 djhx-pi-bot 的 SMTP 测试邮件。")
    except (ValueError, MailSendError) as exc:
        return _mail_page(request, csrf, str(exc), test_to=form.get("to", ""))
    logger.info("后台发送 SMTP 测试邮件", extra={"recipient": recipient})
    return _redirect("/admin/mail?status=sent")
