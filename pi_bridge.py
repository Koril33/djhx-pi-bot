"""pi RPC 桥接层。

管理一个常驻的 `pi --mode rpc` 子进程（JSONL over stdio），按命令 ID
匹配回复，并将不带命令 ID 的会话事件交给唯一的 prompt 消费者。

关键约束：stdin 必须保持打开（EOF 会让 pi 提前退出、掐断推理）。
"""
import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from logging_conf import get_logger

logger = get_logger("pi_bridge")


async def _invoke(cb: Callable[..., Any] | None, *args: Any) -> None:
    """调用可能为同步或异步的回调；cb 为 None 时静默跳过。"""
    if cb is None:
        return
    result = cb(*args)
    if asyncio.iscoroutine(result):
        await result

# 单轮最长等待时间（秒）：DeepSeek 深度思考 + 工具循环可能较长
TURN_TIMEOUT = 600
# 子进程连续崩溃保护
MAX_RESTARTS = 5
RESTART_WINDOW = 300
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_QUEUED_EVENTS = 256
MAX_DELTA_CHARS = 8192


class PiError(Exception):
    pass


class PiBusyError(PiError):
    pass


class PiRpcClient:
    def __init__(
        self,
        pi_cmd: str,
        session_dir: str,
        provider: str | None = None,
        model: str | None = None,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        self.pi_cmd = pi_cmd
        self.session_dir = session_dir
        self.provider = provider
        self.model = model
        self.extra_args = extra_args or []
        self.extra_env = extra_env or {}
        self._session_file: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[dict | PiError] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._transport_error: PiError | None = None
        self._thinking_queued = False
        self.busy = False
        self._restart_times: list[float] = []
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    def has_live_session(self, session_file: str | None) -> bool:
        """新会话的 JSONL 可能直到首轮 prompt 才落盘。"""
        return bool(
            session_file and self._session_file == session_file and self.proc
            and self.proc.returncode is None and self._transport_error is None
            and self._reader_task and not self._reader_task.done()
        )

    # ---------- 进程生命周期 ----------
    async def start(self, session_file: str | None = None) -> None:
        Path(self.session_dir).mkdir(parents=True, exist_ok=True)
        args = [self.pi_cmd, "--mode", "rpc", "--session-dir", self.session_dir]
        if session_file:
            args += ["--session", session_file]
        if self.provider:
            args += ["--provider", self.provider]
        if self.model:
            args += ["--model", self.model]
        args += self.extra_args

        logger.info("spawn pi rpc", extra={"pi_args": args})
        env = os.environ.copy()
        env.update(self.extra_env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise PiError(f"无法启动 pi 子进程: {exc}") from exc
        self.proc = proc
        self._transport_error = None
        self._event_queue = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._reader_task = asyncio.create_task(self._read_stdout(proc))
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc))

    def _fail_transport(self, proc: asyncio.subprocess.Process, error: PiError, *, log: bool = True) -> None:
        """唤醒所有等待者；下一条命令会回收进程并重新连接。"""
        if proc is not self.proc or self._transport_error is not None:
            return
        self._transport_error = error
        if log:
            logger.error("pi RPC 连接失效", extra={"reason": str(error), "returncode": proc.returncode})
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        if self.busy:
            # 连接已失效时丢弃积压事件，使 prompt 尽快读到错误。
            while not self._event_queue.empty():
                self._event_queue.get_nowait()
            self._event_queue.put_nowait(error)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        proc = self.proc
        if proc is not None:
            self._fail_transport(proc, PiError("pi RPC 连接已关闭"), log=False)
        self.proc = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (OSError, ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (OSError, ProcessLookupError, asyncio.TimeoutError):
                    logger.exception("无法结束 pi 子进程")
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    async def ensure_running(self, session_file: str | None = None) -> None:
        async with self._lock:
            healthy = (
                self.proc
                and self.proc.returncode is None
                and self._transport_error is None
                and self._reader_task
                and not self._reader_task.done()
            )
            if healthy and (not session_file or session_file == self._session_file):
                return
            if healthy and session_file and (self.busy or self._pending):
                raise PiBusyError("pi 正在处理请求，暂不能切换会话文件")
            if self.proc:
                await self._stop_unlocked()
            if session_file:
                self._session_file = session_file
            now = time.monotonic()
            self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW]
            if len(self._restart_times) >= MAX_RESTARTS:
                raise PiError("pi 子进程频繁崩溃，已熔断，请稍后再试")
            self._restart_times.append(now)
            await self.start(self._session_file)

    # ---------- 内部 IO ----------
    async def _route_event(self, proc: asyncio.subprocess.Process, event: dict) -> None:
        """仅保留界面所需的轻量事件，满队列时对 stdout 施加背压。"""
        if proc is not self.proc or not self.busy or self._transport_error:
            return
        etype = event.get("type")
        routed: dict | None = None
        if etype == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if not isinstance(update, dict):
                return
            subtype = update.get("type")
            if subtype == "text_delta" and isinstance(update.get("delta"), str):
                delta = update["delta"]
                for offset in range(0, len(delta), MAX_DELTA_CHARS):
                    await self._event_queue.put({"type": "message_update", "assistantMessageEvent":
                                                 {"type": "text_delta", "delta": delta[offset:offset + MAX_DELTA_CHARS]}})
                return
            if subtype == "thinking_delta" and not self._thinking_queued:
                self._thinking_queued = True
                routed = {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}}
            elif subtype == "error":
                routed = {"type": "message_update", "assistantMessageEvent":
                          {"type": "error", "error": str(update.get("error") or update.get("reason") or "")[:500]}}
        elif etype == "tool_execution_start":
            # 工具参数可能包含命令、文件路径和凭据；界面只需要工具类别。
            routed = {"type": etype, "toolName": str(event.get("toolName") or "")[:80]}
        elif etype == "tool_execution_end":
            routed = {"type": etype, "toolName": str(event.get("toolName") or "")[:80]}
        elif etype == "message_end":
            message = event.get("message") or {}
            if isinstance(message, dict) and message.get("role") == "assistant":
                routed = {"type": etype, "message": {"role": "assistant", "stopReason": message.get("stopReason"),
                                                       "errorMessage": str(message.get("errorMessage") or "")[:500]}}
                if isinstance(message.get("usage"), dict):
                    routed["message"]["usage"] = {
                        key: message["usage"][key]
                        for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")
                        if key in message["usage"]
                    }
                for key in ("provider", "model", "timestamp"):
                    if key in message:
                        routed["message"][key] = message[key]
        elif etype in ("auto_retry_end", "compaction_end", "error", "agent_settled"):
            routed = {"type": etype}
            for key in ("success", "finalError", "errorMessage", "error", "message"):
                if key in event:
                    value = event[key]
                    routed[key] = value if isinstance(value, bool) else str(value)[:500]
            if etype == "compaction_end" and isinstance(event.get("result"), dict):
                usage = event["result"].get("usage")
                if isinstance(usage, dict):
                    routed["usage"] = {
                        key: usage[key]
                        for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")
                        if key in usage
                    }
        if routed is not None:
            await self._event_queue.put(routed)

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        pending = bytearray()
        try:
            # StreamReader.readline() 默认限制 64 KiB；pi 的 JSONL 可以远大于此。
            while chunk := await proc.stdout.read(64 * 1024):
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    if end > MAX_RECORD_BYTES:
                        raise PiError("pi RPC 输出记录过长")
                    raw = bytes(pending[:end]).removesuffix(b"\r")
                    del pending[: end + 1]
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise PiError("pi RPC stdout 包含无效 JSON 记录") from exc
                    if not isinstance(event, dict):
                        raise PiError("pi RPC stdout 记录不是 JSON 对象")
                    if event.get("type") == "response":
                        cmd_id = event.get("id")
                        future = self._pending.get(cmd_id) if isinstance(cmd_id, str) else None
                        if future and not future.done():
                            future.set_result(event)
                        else:
                            logger.warning("pi RPC 未匹配的回复", extra={"cmd_id": cmd_id, "command": event.get("command")})
                    else:
                        await self._route_event(proc, event)
                if len(pending) > MAX_RECORD_BYTES:
                    raise PiError("pi RPC 输出记录过长")
            if pending:
                raise PiError("pi RPC stdout 在 JSON 记录中途结束")
            raise PiError(f"pi RPC stdout 已关闭，退出码: {proc.returncode}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, PiError) else PiError(f"读取 pi RPC stdout 失败: {type(exc).__name__}: {exc}")
            if proc is self.proc:
                logger.exception("pi RPC stdout 读取失败")
                self._fail_transport(proc, error)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        pending = bytearray()
        try:
            while chunk := await proc.stderr.read(8192):
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    raw = bytes(pending[:end]).removesuffix(b"\r")
                    del pending[: end + 1]
                    if raw:
                        logger.warning("pi stderr", extra={"line": raw.decode("utf-8", errors="replace")[:500]})
                if len(pending) > 8192:
                    logger.warning("pi stderr", extra={"line": pending[:500].decode("utf-8", errors="replace")})
                    pending.clear()
            if pending:
                logger.warning("pi stderr", extra={"line": pending[:500].decode("utf-8", errors="replace")})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if proc is self.proc:
                logger.exception("pi stderr 读取失败")
                self._fail_transport(proc, PiError(f"读取 pi stderr 失败: {type(exc).__name__}: {exc}"))

    async def _send(self, cmd: dict) -> None:
        async with self._write_lock:
            proc = self.proc
            if not proc or proc.returncode is not None or self._transport_error:
                raise self._transport_error or PiError("pi 子进程未运行")
            assert proc.stdin
            try:
                proc.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            except (OSError, ConnectionError) as exc:
                error = PiError(f"写入 pi RPC 失败: {type(exc).__name__}: {exc}")
                self._fail_transport(proc, error)
                raise error from exc

    async def _request(self, body: dict, timeout: float = 30) -> dict:
        cmd_id = body["id"]
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future  # 先订阅，再发送，防止极快的回复丢失
        try:
            await self._send(body)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                logger.error("等待 pi RPC 回复超时", extra={"cmd_id": cmd_id, "command": body["type"], "timeout_s": timeout})
                if self.proc and (body["type"] == "prompt" or not self.busy):
                    self._fail_transport(self.proc, PiError(f"pi {body['type']} 长时间没有响应"))
                raise PiError(f"等待 pi {body['type']} 响应超时（{timeout:g} 秒）") from exc
        except BaseException:
            if future.done() and not future.cancelled():
                future.exception()  # _send 失败时取走同步设置的异常，避免未读取警告。
            raise
        finally:
            self._pending.pop(cmd_id, None)
            if not future.done():
                future.cancel()

    async def _command(self, command_type: str, timeout: float = 60, **fields: Any) -> dict:
        await self.ensure_running()
        response = await self._request({"id": uuid.uuid4().hex, "type": command_type, **fields}, timeout)
        if not response.get("success"):
            raise PiError(f"pi {command_type} 失败: {response.get('error') or '未知错误'}")
        return response.get("data") or {}

    # ---------- 命令 ----------
    async def prompt(
        self,
        text: str,
        images: list[dict] | None = None,
        on_thinking: Callable[[], Any] | None = None,
        on_tool: Callable[[str, str, dict | None], Any] | None = None,
        on_usage: Callable[[dict], Any] | None = None,
    ) -> AsyncIterator[str]:
        """发送用户消息并流式产出文本增量。

        on_thinking：首次出现思考（thinking_delta）时回调一次（无参数），
                     用于 bot 层发「思考中」占位；不展示思考内容本身。
        on_tool(tool_name, phase, args)：工具执行开始/结束时回调，
                     phase 为 "start"/"end"，end 时 args 为 None。
                     回调须轻量（仅更新状态），避免阻塞流式循环。
        """
        await self.ensure_running()
        if self.busy:
            raise PiBusyError("pi 正在处理上一条消息")
        self.busy = True
        self._thinking_queued = False
        event_queue = self._event_queue  # 发送前订阅，避免极快的事件丢失
        proc = self.proc
        cmd_id = f"p{uuid.uuid4().hex}"
        body: dict = {"id": cmd_id, "type": "prompt", "message": text}
        if images:
            body["images"] = images
        t0 = time.monotonic()
        thinking_fired = False
        last_error: str | None = None
        settled = False
        may_be_running = False

        async def report_usage(kind: str, usage: dict, provider: str | None, model: str | None,
                               timestamp: int | float | None = None) -> None:
            if not any(usage.get(key) for key in ("input", "cacheRead", "cacheWrite", "output")):
                return
            occurred_at = (timestamp / 1000 if isinstance(timestamp, (int, float)) and timestamp > 100_000_000_000
                           else time.time())
            try:
                await _invoke(on_usage, {
                    "kind": kind, "usage": usage, "provider": provider or self.provider or "",
                    "model": model or self.model or "", "occurred_at": occurred_at,
                })
            except Exception:
                logger.exception("记录 pi 用量回调失败", extra={"cmd_id": cmd_id})

        try:
            may_be_running = True
            resp = await self._request(body)
            if not resp.get("success"):
                may_be_running = False
                raise PiError(f"pi 拒绝 prompt: {resp.get('error') or '未知错误'}")
            # agent_end 后还可能重试、压缩或处理 steer；必须等 agent_settled。
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=TURN_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    logger.error("pi 回合超时，重启 RPC 子进程", extra={"cmd_id": cmd_id, "timeout_s": TURN_TIMEOUT})
                    await self.stop()
                    raise PiError(f"pi 处理超时（{TURN_TIMEOUT:g} 秒），已重启连接") from exc
                if isinstance(event, PiError):
                    raise event
                etype = event.get("type")
                if etype == "message_update":
                    ev = event.get("assistantMessageEvent") or {}
                    evtype = ev.get("type")
                    if evtype == "thinking_delta" and not thinking_fired:
                        thinking_fired = True
                        await _invoke(on_thinking)
                    elif evtype == "text_delta" and ev.get("delta"):
                        yield ev["delta"]
                    elif evtype == "error":
                        last_error = str(ev.get("error") or ev.get("reason") or "模型响应失败")
                elif etype == "tool_execution_start":
                    await _invoke(on_tool, event.get("toolName", ""), "start", event.get("args") or {})
                elif etype == "tool_execution_end":
                    await _invoke(on_tool, event.get("toolName", ""), "end", None)
                elif etype == "message_end":
                    message = event.get("message") or {}
                    if message.get("role") == "assistant":
                        if isinstance(message.get("usage"), dict):
                            await report_usage("assistant", message["usage"], message.get("provider"),
                                               message.get("model"), message.get("timestamp"))
                        reason = message.get("stopReason")
                        if reason in ("error", "aborted"):
                            last_error = str(message.get("errorMessage") or ("pi 已中止" if reason == "aborted" else "模型响应失败"))
                        elif reason and reason != "pending":
                            last_error = None
                elif etype == "auto_retry_end" and event.get("success") is False:
                    last_error = str(event.get("finalError") or "模型重试失败")
                elif etype == "compaction_end" and event.get("errorMessage"):
                    last_error = str(event["errorMessage"])
                elif etype == "compaction_end" and isinstance(event.get("usage"), dict):
                    await report_usage("compaction", event["usage"], self.provider, self.model)
                elif etype == "error":
                    last_error = str(event.get("error") or event.get("message") or "pi 处理失败")
                elif etype == "agent_settled":
                    settled = True
                    if last_error:
                        raise PiError(last_error[:500])
                    break
        finally:
            try:
                if may_be_running and not settled and proc is not None:
                    # 消费者取消/提前关闭后，旧 run 仍会继续发送事件。先标记失效，
                    # 再在锁内终止旧进程，禁止下一条 prompt 误读旧的 agent_settled。
                    if self.proc is proc:
                        self._fail_transport(proc, PiError("pi 流式会话提前结束"), log=False)
                    async with self._lock:
                        if self.proc is proc:
                            await self._stop_unlocked()
            finally:
                self.busy = False
                logger.info(
                    "pi turn 完成",
                    extra={"cmd_id": cmd_id, "latency_ms": int((time.monotonic() - t0) * 1000), "text_len": len(text), "settled": settled},
                )

    async def new_session(self, name: str | None = None) -> dict:
        data = await self._command("new_session")
        if data.get("cancelled"):
            raise PiError("新建会话被 pi 扩展取消")
        # 新会话的文件名须由下一次 get_state 确认；不能继续挂载旧文件。
        self._session_file = None
        if name:
            try:
                await self._command("set_session_name", name=name)
            except Exception:
                # 会话已经创建；名称设置失败不能让上层误以为切换未发生。
                logger.exception("pi 会话创建成功，但设置名称失败", extra={"session_name": name})
        return data

    async def reset_session(self) -> dict:
        """丢失旧 JSONL 时从新的本地会话文件开始。"""
        if self.busy:
            raise PiBusyError("pi 正在处理请求，不能重建会话")
        async with self._lock:
            if self.busy:
                raise PiBusyError("pi 正在处理请求，不能重建会话")
            await self._stop_unlocked()
            self._session_file = None
            await self.start()
        return await self.get_state()

    async def get_state(self) -> dict:
        data = await self._command("get_state")
        if "sessionFile" in data:
            path = data["sessionFile"]
            self._session_file = path if isinstance(path, str) and path else None
        return data

    async def get_session_stats(self) -> dict:
        return await self._command("get_session_stats", timeout=5)

    async def switch_session(self, session_path: str) -> dict:
        """进程内切换到指定会话文件（无需重启进程）。"""
        data = await self._command("switch_session", sessionPath=session_path)
        if data.get("cancelled"):
            raise PiError("切换会话被 pi 扩展取消")
        self._session_file = session_path
        return data

    async def get_available_models(self) -> list[dict]:
        return (await self._command("get_available_models")).get("models", [])

    async def set_model(self, model_id: str) -> dict:
        """切换模型。pi RPC 的 set_model 需要 provider+modelId 同时匹配。

        支持两种输入："provider/modelId" 或纯 "modelId"；
        纯 id 时优先按 id 从可用模型列表反查 provider（多 provider 场景下比
        直接使用默认 provider 更可靠），查不到再退回默认 provider。
        """
        provider, separator, bare_id = model_id.partition("/")
        if not separator:
            bare_id, provider = provider, None
            for model in await self.get_available_models():
                if model.get("id") == bare_id:
                    provider = model.get("provider")
                    break
            provider = provider or self.provider
        if not provider or not bare_id:
            raise PiError(f"无法确定模型的 provider: {model_id}")
        data = await self._command("set_model", provider=provider, modelId=bare_id)
        self.provider, self.model = provider, bare_id
        return data

    async def abort(self) -> None:
        await self._command("abort")

    async def steer(self, text: str, images: list[dict] | None = None) -> None:
        """流式过程中插入新指令（可附图片），并确认 pi 已接受。"""
        fields: dict[str, Any] = {"message": text}
        if images:
            fields["images"] = images
        await self._command("steer", **fields)
