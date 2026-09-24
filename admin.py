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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import Settings
from bot import create_user_session, switch_user_session
from costs import format_usd
from logging_conf import get_logger
from memory import looks_secret
from pi_bridge import PiBusyError, PiError
from storage import Storage

logger = get_logger("admin")
router = APIRouter(prefix="/admin", include_in_schema=False)
_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
# 后台是整页导航。把这份小样式与 HTML 一起送达，避免每次切页重取 CSS 时短暂裸显。
_STYLE = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
_STYLE_TAG = f"<style>{_STYLE}</style>"
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")
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
        if not hmac.compare_digest(signature, expected):
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
        "Content-Security-Policy": f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
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
    if not hmac.compare_digest(form.get("csrf", ""), csrf):
        raise HTTPException(status_code=403, detail="页面已过期，请刷新后重试")
    return csrf, form


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers={"Cache-Control": "no-store"})


def _login_page(csrf: str, error: str = "") -> str:
    message = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · Pi 管理</title>
{_STYLE_TAG}</head><body class="login-body">
<main class="login-card"><div class="brand-mark" aria-hidden="true">P<span>·</span></div>
<p class="eyebrow">DJHX / PI</p><h1>管理后台</h1>{message}
<form method="post" action="/admin/login" class="form-stack">
<input type="hidden" name="csrf" value="{_e(csrf)}">
<label>用户名<input name="username" autocomplete="username" required autofocus></label>
<label>密码<input name="password" type="password" autocomplete="current-password" required></label>
<button class="button primary" type="submit">登录</button></form></main></body></html>"""


def _layout(title: str, active: str, body: str, csrf: str, user_id: int | None, settings: Settings) -> str:
    suffix = f"?{urlencode({'user_id': user_id})}" if user_id is not None else ""
    def nav(path: str, label: str, key: str) -> str:
        cls = "nav-link active" if active == key else "nav-link"
        return f'<a class="{cls}" href="/admin/{path}{suffix if path != "settings" else ""}">{label}</a>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} · Pi 管理</title>
{_STYLE_TAG}</head><body>
<div class="app-shell"><aside class="sidebar"><a class="brand" href="/admin/overview">
<span class="brand-mark">P<span>·</span></span><span>PI<span class="brand-muted"> / ADMIN</span></span></a>
<nav aria-label="主导航">{nav('overview', '概览', 'overview')}{nav('sessions', '会话管理', 'sessions')}{nav('usage', '用量费用', 'usage')}{nav('memories', '长期记忆', 'memories')}{nav('summaries', '会话摘要', 'summaries')}{nav('settings', '记忆设置', 'settings')}</nav>
<div class="sidebar-bottom"><span class="admin-name">{_e(settings.admin_username)}</span>
<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{_e(csrf)}">
<button class="text-button" type="submit">退出</button></form></div></aside>
<main class="main"><header class="topline"><span>DJHX PI BOT</span><span>ADMIN</span></header>
<div class="page-content">{body}</div></main></div></body></html>"""


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
              "created": "会话已创建", "switched": "会话已切换", "recovered": "会话已从 SQLite 恢复"}
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
    service = "正常" if worker is not None and not worker.done() else ("未读取" if worker is None else "异常")
    agent = request.app.state.manager._agents.get(user_id) if user_id is not None else None
    proc = agent.client.proc if agent is not None else None
    pi_state = ("运行中" if proc is not None and proc.returncode is None else
                ("待重连" if proc is not None else "按需启动"))
    busy = bool(agent and (agent.client.busy or agent.prompt_pending))
    active = storage.get_active_session(user_id) if user_id is not None else None
    model = (agent.client.model if agent is not None else None) or settings.pi_model
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
        failed.append(f'<div class="list-row"><span class="mono">#{_e(row["update_id"])}</span>'
                      f'<span>{_e(reason)}</span><span class="muted">{_e(_time_label(row["processed_at"]))}</span></div>')
    failure_html = "".join(failed) or '<p class="empty small">没有失败更新</p>'
    body = (f'<div class="page-heading"><div><p class="eyebrow">OVERVIEW</p><h1>概览</h1></div>{picker}</div>'
            f'<div class="metric-grid">{cards_html}</div>'
            f'<section class="section-block"><div class="section-head"><h2>最近失败更新</h2>'
            f'<span class="muted">仅展示故障类型，不自动重放</span></div><div class="simple-list">{failure_html}</div></section>')
    return _html(_layout("概览", "overview", body, csrf, user_id, settings))


def _session_page(request: Request, csrf: str, user_id: int | None, error: str = "") -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "sessions")
    if user_id is None:
        content = '<p class="empty">暂无用户</p>'
    else:
        add = (f'<form method="post" action="/admin/sessions/new" class="compact-form">'
               f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}">'
               '<label class="sr-only" for="session-name">会话名称</label>'
               '<input id="session-name" name="name" maxlength="60" placeholder="新会话名称" required>'
               '<button class="button primary" type="submit">新建会话</button></form>')
        agent = request.app.state.manager._agents.get(user_id)
        cards = []
        for row in storage.list_sessions(user_id):
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
            switch = ('<span class="badge">当前</span>' if active else
                      f'<form method="post" action="/admin/sessions/{sid}/use">'
                      f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                      f'<input type="hidden" name="user_id" value="{user_id}">'
                      '<button class="text-button" type="submit">切换</button></form>')
            rename = (f'<details><summary>重命名</summary><form method="post" action="/admin/sessions/{sid}/rename" class="inline-edit">'
                      f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}">'
                      f'<input name="name" value="{_e(row["name"])}" maxlength="60" required>'
                      '<button class="button primary" type="submit">保存</button></form></details>')
            cards.append(f'<article class="summary-card"><div class="summary-head"><div>'
                         f'<span class="session-state">{_e(file_state)}</span><h2>{_e(row["name"])}</h2></div>'
                         f'<span class="mono">{sid}</span></div><div class="session-foot">'
                         f'<span class="muted">{_e(_time_label(row["updated_at"]))}</span>'
                         f'<div class="card-actions">{switch}{rename}</div></div></article>')
        content = add + ('<div class="card-list">' + "".join(cards) + '</div>' if cards else '<p class="empty">暂无会话</p>')
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'<div class="page-heading"><div><p class="eyebrow">SESSIONS</p><h1>会话管理</h1></div>{picker}</div>{alert}{content}'
    return _html(_layout("会话管理", "sessions", body, csrf, user_id, settings), status_code=400 if error else 200)


def _usage_page(request: Request, csrf: str, user_id: int | None, period: str) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "usage")
    periods = {"today": ("今天", 0), "7d": ("近 7 天", 6), "30d": ("近 30 天", 29)}
    label, days_back = periods[period]
    midnight = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (midnight - timedelta(days=days_back)).timestamp()
    runs = storage.get_usage_runs_since(user_id, since) if user_id is not None else []
    events = storage.get_usage_events_since(user_id, since) if user_id is not None else []
    counts = {key: sum(row[key] for row in events) for key in
              ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}
    total = sum((Decimal(row["estimated_usd"]) for row in events if row["estimated_usd"] is not None), Decimal(0))
    maintenance = sum((Decimal(row["estimated_usd"]) for row in events
                       if row["origin"] == "memory_maintenance" and row["estimated_usd"] is not None), Decimal(0))
    cache_base = counts["input_tokens"] + counts["cache_read_tokens"]
    cache_rate = f'{counts["cache_read_tokens"] / cache_base:.0%}' if cache_base else "—"
    range_links = "".join(
        f'<a class="range-link{" active" if key == period else ""}" href="/admin/usage?{urlencode({"range": key, **({"user_id": user_id} if user_id is not None else {})})}">{title}</a>'
        for key, (title, _) in periods.items()
    )
    metrics = (("估算费用", format_usd(total)), ("聊天轮次", str(sum(r["origin"] == "chat" for r in runs))),
               ("缓存命中", cache_rate), ("记忆维护", format_usd(maintenance)))
    metric_html = "".join(f'<div class="metric-card"><span>{_e(name)}</span><strong>{_e(value)}</strong></div>'
                          for name, value in metrics)
    by_model: dict[str, dict[str, object]] = {}
    for row in events:
        key = f'{row["provider"]}/{row["model"]}'
        item = by_model.setdefault(key, {"calls": 0, "tokens": 0, "cost": Decimal(0)})
        item["calls"] += 1
        item["tokens"] += row["total_tokens"]
        if row["estimated_usd"] is not None:
            item["cost"] += Decimal(row["estimated_usd"])
    model_rows = "".join(
        f'<div class="list-row"><span>{_e(model)}</span><span>{entry["calls"]} 次 · {entry["tokens"]:,} tokens</span>'
        f'<strong>{_e(format_usd(entry["cost"]))}</strong></div>'
        for model, entry in sorted(by_model.items())
    ) or '<p class="empty small">暂无用量事件</p>'
    issues = []
    unpriced = sum(row["estimated_usd"] is None for row in events)
    if unpriced:
        issues.append(f'{unpriced} 次调用未计价')
    if any(row["price_status"] == "stale_price" for row in events):
        issues.append("价格待复核")
    if any(row["status"] != "complete" for row in runs):
        issues.append("存在未完成回合")
    issue_html = f'<p class="form-error">{_e("；".join(issues))}</p>' if issues else ""
    token_line = (f'未命中 {counts["input_tokens"]:,} · 缓存读取 {counts["cache_read_tokens"]:,} · '
                  f'缓存写入 {counts["cache_write_tokens"]:,} · 输出 {counts["output_tokens"]:,}')
    recent = []
    for run in storage.get_recent_usage_runs(user_id) if user_id is not None else []:
        amount = sum((Decimal(event["estimated_usd"]) for event in storage.get_usage_events(run["id"])
                      if event["estimated_usd"] is not None), Decimal(0))
        origin = "记忆维护" if run["origin"] == "memory_maintenance" else "聊天"
        recent.append(f'<div class="list-row"><span>{_e(_time_label(run["started_at"]))}</span>'
                      f'<span>{origin} · {_e(run["status"])}</span><strong>{_e(format_usd(amount))}</strong></div>')
    body = (f'<div class="page-heading"><div><p class="eyebrow">USAGE</p><h1>用量费用</h1></div>{picker}</div>'
            f'<div class="range-links">{range_links}</div>{issue_html}<div class="metric-grid">{metric_html}</div>'
            f'<p class="usage-caption">{_e(label)} · {token_line}</p>'
            f'<section class="section-block"><div class="section-head"><h2>模型</h2><span class="muted">按公开费率估算</span></div>'
            f'<div class="simple-list">{model_rows}</div></section>'
            f'<section class="section-block"><div class="section-head"><h2>最近记录</h2></div>'
            f'<div class="simple-list">{"".join(recent) or "<p class=\"empty small\">暂无记录</p>"}</div></section>')
    return _html(_layout("用量费用", "usage", body, csrf, user_id, settings))


def _memory_page(request: Request, csrf: str, user_id: int | None, error: str = "") -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "memories")
    if user_id is None:
        contents = '<p class="empty">暂无用户</p>'
    else:
        query = request.query_params.get("q", "").strip()[:80]
        memories = storage.list_memories(user_id)
        shown = [row for row in memories if query.casefold() in row["text"].casefold()]
        search = f'<form method="get" action="/admin/memories" class="search-form"><input type="hidden" name="user_id" value="{user_id}"><input name="q" value="{_e(query)}" placeholder="搜索记忆" aria-label="搜索记忆"><button class="button subtle">搜索</button></form>'
        add = f'''<form method="post" action="/admin/memories/add" class="add-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><label class="sr-only" for="new-memory">新增记忆</label><textarea id="new-memory" name="text" maxlength="{settings.memory_item_max_chars}" rows="2" placeholder="写入一条长期记忆" required></textarea><button class="button primary" type="submit">添加记忆</button></form>'''
        cards = []
        for row in shown:
            ident = _e(row["id"])
            badge = '<span class="badge">置顶</span>' if row["pinned"] else ""
            source = "手动" if row["source"] == "manual" else "自动"
            cards.append(f'''<article class="memory-card"><div class="card-meta"><span>{source}</span>{badge}<span class="mono">{ident}</span></div><p class="memory-text">{_e(row["text"])}</p><div class="card-actions"><form method="post" action="/admin/memories/{ident}/pin"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><input type="hidden" name="pinned" value="{0 if row["pinned"] else 1}"><button class="text-button" type="submit">{'取消置顶' if row['pinned'] else '置顶'}</button></form><details><summary>编辑</summary><form method="post" action="/admin/memories/{ident}/edit" class="inline-edit"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><textarea name="text" maxlength="{settings.memory_item_max_chars}" rows="3" required>{_e(row["text"])}</textarea><button class="button primary" type="submit">保存</button></form></details><details class="danger-details"><summary>删除</summary><form method="post" action="/admin/memories/{ident}/delete"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><button class="button danger" type="submit">确认删除</button></form></details></div></article>''')
        cards_html = "".join(cards) if cards else '<p class="empty">暂无记忆</p>'
        contents = f'<div class="toolbar"><span class="count">{len(memories)} / {settings.memory_active_limit}</span>{search}</div>{add}<div class="card-list">{cards_html}</div>'
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'<div class="page-heading"><div><p class="eyebrow">MEMORY</p><h1>长期记忆</h1></div>{picker}</div>{alert}{contents}'
    return _html(_layout("长期记忆", "memories", body, csrf, user_id, settings), status_code=400 if error else 200)


def _summary_page(request: Request, csrf: str, user_id: int | None) -> HTMLResponse:
    storage, settings = _storage(request), _settings(request)
    picker = _user_picker(storage, settings, user_id, "summaries")
    cards = []
    for row in storage.list_admin_summaries(user_id) if user_id is not None else []:
        summary = row["summary_text"] or ""
        ident = _e(row["id"])
        if summary:
            clear = f'''<details class="danger-details"><summary>清空摘要</summary><form method="post" action="/admin/summaries/{ident}/clear"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="user_id" value="{user_id}"><button class="button danger" type="submit">确认清空</button></form></details>'''
            content = f'<p class="summary-text">{_e(summary)}</p>'
        else:
            clear, content = "", '<p class="empty small">暂无摘要</p>'
        cards.append(f'<article class="summary-card"><div class="summary-head"><div><span class="session-state">{_e(row["status"])}</span><h2>{_e(row["name"])}</h2></div><span class="mono">{ident}</span></div>{content}<div class="card-actions">{clear}</div></article>')
    content = "".join(cards) if cards else '<p class="empty">暂无会话</p>'
    body = f'<div class="page-heading"><div><p class="eyebrow">SESSIONS</p><h1>会话摘要</h1></div>{picker}</div>{_notice(request)}<div class="card-list">{content}</div>'
    return _html(_layout("会话摘要", "summaries", body, csrf, user_id, settings))


def _settings_page(request: Request, csrf: str, error: str = "") -> HTMLResponse:
    settings = _settings(request)
    saved = _storage(request).get_runtime_settings()
    groups = (
        ("自动整理", _FIELDS[:7]),
        ("记忆边界", _FIELDS[7:]),
    )
    sections = []
    for group_name, fields in groups:
        rows = "".join(
            f'<label class="setting-field"><span>{_e(label)}</span><span class="input-with-unit"><input type="number" name="{key}" min="{low}" max="{high}" value="{getattr(settings, key)}" required><span>{_e(unit)}</span></span></label>'
            for key, label, unit, low, high in fields
        )
        sections.append(f'<section class="settings-group"><h2>{group_name}</h2><div class="settings-grid">{rows}</div></section>')
    checked = " checked" if settings.memory_auto_extract else ""
    source = "SQLite" if saved else ".env"
    alert = f'<p class="form-error" role="alert">{_e(error)}</p>' if error else _notice(request)
    body = f'''<div class="page-heading"><div><p class="eyebrow">CONFIGURATION</p><h1>记忆设置</h1></div><span class="source-label">{source}</span></div>{alert}
<form method="post" action="/admin/settings" class="settings-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><label class="switch-row"><span>自动提取</span><input type="checkbox" name="memory_auto_extract"{checked}></label>{''.join(sections)}<div class="form-footer"><button class="button primary" type="submit">保存设置</button></div></form>'''
    return _html(_layout("记忆设置", "settings", body, csrf, None, settings), status_code=400 if error else 200)


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
    if not expected or not hmac.compare_digest(form.get("csrf", ""), expected):
        raise HTTPException(status_code=403, detail="页面已过期，请刷新后重试")
    ip = _client_ip(request)
    now = time.monotonic()
    failures = [value for value in _LOGIN_FAILURES.get(ip, []) if now - value < 900]
    if len(failures) >= 8:
        raise HTTPException(status_code=429, detail="请稍后再试")
    valid_user = hmac.compare_digest(form.get("username", ""), settings.admin_username)
    valid_password = hmac.compare_digest(form.get("password", ""), settings.admin_password)
    if not (valid_user and valid_password):
        _LOGIN_FAILURES[ip] = failures + [now]
        csrf = secrets.token_urlsafe(24)
        response = _html(_login_page(csrf, "用户名或密码错误"), status_code=401)
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
        return _session_page(request, csrf, user_id, str(exc))
    agent = request.app.state.manager.get(user_id)
    try:
        async with agent.control_lock:
            sid = await create_user_session(agent, storage, user_id, name)
    except (PiError, ValueError) as exc:
        return _session_page(request, csrf, user_id, str(exc))
    except Exception:
        logger.exception("后台新建会话失败", extra={"user_id": user_id})
        return _session_page(request, csrf, user_id, "新建失败，请查看服务日志")
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
            notice = await switch_user_session(agent, storage, user_id, session_id)
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
        return _session_page(request, csrf, user_id, str(exc))
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
    return _usage_page(request, csrf, user_id, period)


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
        return _memory_page(request, csrf, user_id, "记忆内容为空、过长或包含敏感凭据")
    try:
        storage.ensure_user(user_id, None)
        memory_id = storage.add_memory(user_id, item, limit=settings.memory_active_limit)
    except ValueError as exc:
        return _memory_page(request, csrf, user_id, str(exc))
    _refresh_memory(request, user_id)
    logger.info("后台添加记忆", extra={"user_id": user_id, "memory_id": memory_id})
    return _redirect(f"/admin/memories?{urlencode({'user_id': user_id, 'status': 'saved'})}")


@router.post("/memories/{memory_id}/edit")
async def edit_memory(memory_id: str, request: Request):
    csrf, form = await _authorized_form(request)
    storage, settings = _storage(request), _settings(request)
    user_id = _form_user(form, storage, settings)
    item = form.get("text", "").strip()
    if not item or len(item) > settings.memory_item_max_chars or looks_secret(item):
        return _memory_page(request, csrf, user_id, "记忆内容为空、过长或包含敏感凭据")
    try:
        changed = storage.edit_memory(user_id, memory_id, item)
    except sqlite3.IntegrityError:
        return _memory_page(request, csrf, user_id, "相同记忆已存在")
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
        return _settings_page(request, csrf, str(exc))
    storage.save_runtime_settings(values)
    for key, value in values.items():
        setattr(settings, key, value)
    logger.info("后台更新记忆参数", extra={"keys": sorted(values)})
    return _redirect("/admin/settings?status=saved")
