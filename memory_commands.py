"""Telegram记忆容量和整理流程；Bot只需绑定命令及mm_回调。"""

import inspect
import re
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from logging_conf import get_logger


logger = get_logger("memory_commands")
_ID = re.compile(r"[0-9a-f]{12}")
_PREVIEWS_KEY = "memory_merge_previews"


def capacity_text(state: dict) -> str:
    lines = [f"🧠 记忆容量：{state['count']}/{state['limit']} · 置顶 {state['pinned']} 条"]
    if state["full"]:
        lines.append("容量已满：新自动记忆暂不新增，已有记忆仍可使用，精确重复会刷新。可用 /memory manage 核对整理候选。")
    else:
        lines.append(f"还可新增 {state['remaining']} 条；整理仅在预览确认后执行。")
    recent = state.get("last_auto")
    if recent:
        lines.append(f"最近自动整理：新增 {recent['added']} · 重复刷新 {recent['refreshed']}"
                     f" · 因满额未新增 {recent['skipped_full']}")
    return "\n".join(lines)


async def _preview(update, context, reply_fn, memory_ids, text=None):
    storage = context.bot_data["storage"]
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    preview = storage.preview_memory_merge(user_id, memory_ids, text,
                                           max_chars=settings.memory_item_max_chars)
    previews = context.user_data.setdefault(_PREVIEWS_KEY, {})
    now = time.time()
    for key in list(previews):
        if previews[key]["expires"] <= now:
            previews.pop(key)
    while len(previews) >= 10:
        previews.pop(next(iter(previews)))
    token = uuid.uuid4().hex[:12]
    previews[token] = {**preview, "user_id": user_id, "expires": now + 600}
    lines = ["整理预览（尚未修改）："]
    for row in preview["rows"]:
        lines.append(f"{'📌 ' if row['pinned'] else ''}{row['id']}：{row['text']}")
    lines += ["确认后保留一条，内容为：", preview["text"],
              "置顶状态将保留；已注入旧会话的文本不会自动消失。"]
    await reply_fn(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("确认整理", callback_data=f"mm_confirm:{token}"),
        InlineKeyboardButton("取消", callback_data=f"mm_cancel:{token}"),
    ]]))


async def handle_memory_management(update, context, reply_fn, refresh_fn=None):
    """reply_fn同bot.reply_text；可选refresh_fn(user_id)既可同步也可异步。"""
    storage, settings = context.bot_data["storage"], context.bot_data["settings"]
    user_id = update.effective_user.id
    args = context.args or ["manage"]
    sub = args[0]
    if sub == "merge":
        if len(args) < 3 or not all(_ID.fullmatch(value) for value in args[1:3]):
            await reply_fn(update, "用法：/memory merge <ID1> <ID2> [整理后的内容]；先预览，再点击确认。")
            return
        try:
            await _preview(update, context, reply_fn, args[1:3], " ".join(args[3:]) if len(args) > 3 else None)
        except ValueError as exc:
            await reply_fn(update, str(exc))
        return
    if len(args) != 1:
        await reply_fn(update, "用法：/memory capacity | /memory manage | /memory merge <ID1> <ID2> [整理后的内容]")
        return
    state = storage.memory_capacity_status(user_id, settings.memory_active_limit)
    lines, buttons = [capacity_text(state)], []
    if sub != "capacity":
        candidates = storage.memory_organization_candidates(user_id, limit=3)
        if not candidates:
            lines.append("暂未发现明显近重复或新旧冲突；仍可通过 /memory edit 或指定两个ID手动整理。")
        else:
            lines.append("以下只是待核对候选，不会自动删除或覆盖：")
            for index, candidate in enumerate(candidates, 1):
                label = "可能冲突" if candidate["kind"] == "possible_conflict" else "近重复"
                lines.append(f"{index}. {label}：{candidate['reason']}")
                for row in candidate["rows"]:
                    lines.append(f"{'📌 ' if row['pinned'] else ''}{row['id']}：{row['text']}")
                buttons.append([InlineKeyboardButton(
                    f"预览第 {index} 组", callback_data="mm_preview:" + ":".join(candidate["ids"]),
                )])
    await reply_fn(update, "\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def _refresh(context, user_id, refresh_fn):
    if refresh_fn:
        result = refresh_fn(user_id)
        if inspect.isawaitable(result):
            await result
        return
    manager = context.bot_data.get("manager")
    if manager is None or manager.memory is None:
        return
    agent = manager.get(user_id)
    async with agent.control_lock:
        if agent.client.busy or getattr(agent, "prompt_pending", False):
            return
        active = context.bot_data["storage"].get_active_session(user_id)
        if active:
            manager.memory.refresh_context(user_id, active["id"], active["pi_session_file"])


async def handle_memory_merge_callback(update, context, reply_fn, refresh_fn=None):
    """处理mm_preview/mm_confirm/mm_cancel；query.answer由Bot总路由处理。"""
    data = update.callback_query.data or ""
    user_id = update.effective_user.id
    if data.startswith("mm_preview:"):
        ids = data.split(":")[1:]
        if len(ids) != 2 or not all(_ID.fullmatch(value) for value in ids):
            await reply_fn(update, "整理入口无效，请重新使用 /memory manage")
            return
        try:
            await _preview(update, context, reply_fn, ids)
        except ValueError as exc:
            await reply_fn(update, str(exc))
        return
    action, _, token = data.partition(":")
    if action not in {"mm_confirm", "mm_cancel"}:
        return
    preview = context.user_data.get(_PREVIEWS_KEY, {}).pop(token, None)
    if preview is None or preview["user_id"] != user_id or preview["expires"] <= time.time():
        await reply_fn(update, "整理预览已过期，请重新使用 /memory manage 或 /memory merge")
        return
    if action == "mm_cancel":
        await reply_fn(update, "已取消整理，记忆未修改")
        return
    try:
        kept = context.bot_data["storage"].merge_memories(
            user_id, preview["ids"], preview["text"], preview["version"],
            max_chars=context.bot_data["settings"].memory_item_max_chars,
        )
    except ValueError as exc:
        await reply_fn(update, str(exc))
        return
    try:
        await _refresh(context, user_id, refresh_fn)
    except Exception:
        logger.warning("整理后刷新上下文失败", extra={"user_id": user_id, "memory_id": kept})
    state = context.bot_data["storage"].memory_capacity_status(
        user_id, context.bot_data["settings"].memory_active_limit,
    )
    await reply_fn(update, f"✅ 已整理为 {kept}，置顶状态保留；下一轮按新记忆生成上下文。\n" + capacity_text(state))
