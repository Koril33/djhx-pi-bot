"""语音预览及 Pi 对话框的 Telegram 交互。"""

import asyncio
import json
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter

from logging_conf import get_logger
from media import transcribe_voice

logger = get_logger("bot_interactions")
_voice_lock = asyncio.Lock()
_dialogs: dict[str, dict] = {}


async def _download_with_retry(operation):
    """只重试无副作用的 Telegram 文件读取。"""
    for attempt in range(3):
        try:
            return await operation()
        except (NetworkError, RetryAfter) as exc:
            if attempt == 2:
                raise ValueError("语音下载失败，请稍后重发") from None
            delay = .5 * (attempt + 1)
            if isinstance(exc, RetryAfter):
                delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
            await asyncio.sleep(min(max(.5, delay), 30))


async def handle_voice(update: Update, context) -> None:
    # 延后引用避免 bot 与交互模块循环导入。
    from bot import reply_text, run_preparation

    settings = context.bot_data["settings"]
    storage = context.bot_data["storage"]
    manager = context.bot_data["manager"]
    user_id = update.effective_user.id
    agent = manager.get(user_id)
    media = update.message.voice or update.message.audio
    if not settings.voice_enabled:
        await reply_text(update, "语音转写尚未启用，请发送文字。")
        return
    duration = media.duration.total_seconds() if isinstance(media.duration, timedelta) else media.duration
    if duration > settings.voice_max_seconds:
        await reply_text(update, f"语音最多支持 {settings.voice_max_seconds:g} 秒，请拆分后重发。")
        return
    if media.file_size and media.file_size > settings.voice_max_bytes:
        await reply_text(update, "语音附件过大，请缩短或压缩后重发。")
        return
    async def prepare(agent, cancel_requested):
        if agent.client.busy or agent.prompt_pending:
            await reply_text(update, "当前任务仍在结束，请稍后发送语音。")
            return
        agent.execution_phase = "voice"
        active = storage.get_active_session(user_id)
        original_session = active["id"] if active else None
        await reply_text(update, "🎙 正在本机转写语音，完成后请核对文字；发送 /stop 可取消。")
        async with _voice_lock:
            file = await _download_with_retry(media.get_file)
            if file.file_size and file.file_size > settings.voice_max_bytes:
                raise ValueError("语音附件过大，请缩短后重发")
            data = await _download_with_retry(
                lambda: file.download_as_bytearray(read_timeout=60, connect_timeout=20))
            result = await transcribe_voice(bytes(data), settings)
        if cancel_requested.is_set():
            return
        caption = (getattr(update.message, "caption", None) or "").strip()
        submitted = f"{caption}\n\n[语音转写]\n{result['text']}" if caption else result["text"]
        ident = manager.actions.create(user_id, update.effective_chat.id, "voice", {
            "text": submitted, "session_id": original_session,
            "message": json.loads(update.message.to_json()),
        })
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("确认发送", callback_data=f"voice_confirm:{ident}"),
            InlineKeyboardButton("取消", callback_data=f"voice_cancel:{ident}"),
        ]])
        await reply_text(update, f"🎙 转写结果（{result['seconds']:g} 秒）：\n{submitted}\n\n"
                         "核对后点击确认；如需修改，可以复制文字修改后发送。", reply_markup=buttons)
    try:
        await run_preparation(update, context, prepare)
    except ValueError as exc:
        await reply_text(update, f"⚠️ {exc}")


async def handle_interaction_callback(update: Update, context) -> bool:
    from bot import process_confirmed_attachment, reply_text

    query = update.callback_query
    data = query.data or ""
    actions = context.bot_data["manager"].actions
    user_id = update.effective_user.id
    if data.startswith(("voice_confirm:", "voice_cancel:")):
        action, ident = data.split(":", 1)
        if action == "voice_cancel":
            actions.cancel(user_id, ident)
            await reply_text(update, "已取消这次语音转写请求。")
            return True
        saved = actions.get(user_id, ident, "voice")
        active = context.bot_data["storage"].get_active_session(user_id)
        if saved is None:
            await reply_text(update, "转写预览已过期、已处理或不属于当前用户，请重新发送。")
            return True
        if saved["payload"]["session_id"] != (active["id"] if active else None):
            await reply_text(update, "会话已切换，请返回原会话后确认，或复制文字发送。")
            return True
        saved = actions.claim(user_id, ident, "voice")
        if saved is None:
            return True
        message = saved["payload"]["message"]
        for key in ("voice", "audio", "caption", "caption_entities", "entities"):
            message.pop(key, None)
        message["text"] = saved["payload"]["text"]
        synthetic = Update.de_json({"update_id": update.update_id, "message": message}, context.bot)
        await process_confirmed_attachment(
            synthetic, context, expected_session_id=saved["payload"]["session_id"])
        return True
    if data.startswith("piui:"):
        _, ident, choice = data.split(":", 2)
        dialog = _dialogs.get(ident)
        saved = actions.get(user_id, ident, "pi_ui")
        if saved is None or dialog is None or dialog["user_id"] != user_id:
            await reply_text(update, "这个对话框已结束或过期。")
            return True
        value = None
        if choice == "cancel":
            cancelled = True
        elif choice in ("yes", "no") and dialog["method"] == "confirm":
            cancelled, value = False, choice == "yes"
        elif choice.isdecimal() and dialog["method"] == "select":
            index = int(choice)
            if index >= len(dialog["options"]):
                return True
            cancelled, value = False, dialog["options"][index]
        else:
            return True
        if actions.claim(user_id, ident, "pi_ui") and not dialog["future"].done():
            dialog["future"].set_result((cancelled, value))
        return True
    return False


async def cmd_answer(update: Update, context) -> None:
    from bot import reply_text

    text = (update.message.text or "").split(maxsplit=2)
    if len(text) < 3:
        await reply_text(update, "用法：/answer <对话框 ID> <输入内容>")
        return
    ident, value = text[1], text[2]
    user_id = update.effective_user.id
    dialog = _dialogs.get(ident)
    if (dialog is None or dialog["user_id"] != user_id
            or dialog["method"] not in ("input", "editor")):
        await reply_text(update, "这个输入框已结束或过期。")
        return
    if len(value) > 12000:
        await reply_text(update, "输入超过 12000 字符，请缩短。")
        return
    actions = context.bot_data["manager"].actions
    if not actions.claim(user_id, ident, "pi_ui") or dialog["future"].done():
        await reply_text(update, "这个输入框已处理。")
        return
    dialog["future"].set_result((False, value))
    await reply_text(update, "已把输入交给当前任务。")


def install_extension_ui(agent, context, chat_id: int | None = None) -> None:
    from bot import _send

    client = agent.client
    if not hasattr(client, "set_extension_ui_handler"):
        return
    user_id = agent.user_id
    chat_id = chat_id if chat_id is not None else user_id
    actions = context.bot_data["manager"].actions

    async def handle(event: dict) -> None:
        proc = client.proc
        request_id = event.get("id")
        method = event.get("method")
        timeout_ms = event.get("timeout")
        ttl = min(120.0, max(.1, timeout_ms / 1000)) if isinstance(timeout_ms, (int, float)) and timeout_ms > 0 else 120.0
        deadline = asyncio.get_running_loop().time() + ttl
        options = event.get("options") or []
        if method not in ("confirm", "select", "input", "editor"):
            return
        if (not isinstance(options, list) or len(options) > 20
                or any(not isinstance(value, str) or len(value) > 512 for value in options)):
            await client.response_extension_ui(request_id, cancelled=True)
            return
        future = asyncio.get_running_loop().create_future()
        ident = actions.create(user_id, chat_id, "pi_ui", {"request_id": request_id}, ttl=ttl)
        _dialogs[ident] = {"future": future, "user_id": user_id, "method": method, "options": options}
        title = str(event.get("title") or "Pi 需要你的输入")[:300]
        body = str(event.get("message") or "")[:2000]
        if method == "confirm":
            buttons = [[InlineKeyboardButton("确认", callback_data=f"piui:{ident}:yes"),
                        InlineKeyboardButton("拒绝", callback_data=f"piui:{ident}:no")]]
        elif method == "select":
            buttons = [[InlineKeyboardButton(option[:60], callback_data=f"piui:{ident}:{index}")]
                       for index, option in enumerate(options)]
        else:
            buttons = []
            body += f"\n请发送 /answer {ident} 输入内容"
            if method == "editor" and event.get("prefill"):
                body += "\n原内容：\n" + str(event["prefill"])[:1500]
        buttons.append([InlineKeyboardButton("取消", callback_data=f"piui:{ident}:cancel")])
        try:
            await _send(lambda text, **kwargs: context.bot.send_message(chat_id, text, **kwargs),
                        f"{title}\n{body}\n\n有效期 {ttl:g} 秒，超时会取消。",
                        reply_markup=InlineKeyboardMarkup(buttons))
            cancelled, value = await asyncio.wait_for(future, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except (TimeoutError, asyncio.CancelledError):
            cancelled, value = True, None
        except Exception as exc:
            logger.warning("扩展输入框暂未送达", extra={"err_type": type(exc).__name__})
            cancelled, value = True, None
        finally:
            _dialogs.pop(ident, None)
            actions.cancel(user_id, ident)
        if proc is not None and client.proc is proc and proc.returncode is None:
            await client.response_extension_ui(request_id, value=value, cancelled=cancelled)

    client.set_extension_ui_handler(handle)
