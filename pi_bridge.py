"""pi RPC 桥接层。

管理一个常驻的 `pi --mode rpc` 子进程（JSONL over stdio）：
- 常驻进程消除每次对话的冷启动成本（此前 shell 桥每条消息 ~1.3s）
- 支持流式输出（message_update 事件）
- 支持 new_session / set_model / abort 等 Agent 操作
- 子进程崩溃自动重启

关键约束：stdin 必须保持打开（EOF 会让 pi 提前退出、掐断推理）。
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger("pi_bridge")

# 单轮最长等待时间（秒）：DeepSeek 深度思考 + 工具循环可能较长
TURN_TIMEOUT = 600
# 子进程连续崩溃保护
MAX_RESTARTS = 5
RESTART_WINDOW = 300


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
    ):
        self.pi_cmd = pi_cmd
        self.session_dir = session_dir
        self.provider = provider
        self.model = model
        self.proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 0
        self.busy = False
        self._restart_times: list[float] = []
        self._lock = asyncio.Lock()

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

        logger.info("spawn pi rpc", extra={"args": args})
        env = os.environ.copy()
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        self.proc = None

    async def ensure_running(self, session_file: str | None = None) -> None:
        async with self._lock:
            if self.proc and self.proc.returncode is None:
                return
            now = time.monotonic()
            self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW]
            if len(self._restart_times) >= MAX_RESTARTS:
                raise PiError("pi 子进程频繁崩溃，已熔断，请稍后再试")
            self._restart_times.append(now)
            await self.start(session_file)

    # ---------- 内部 IO ----------
    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("pi stdout 非 JSON 行", extra={"line": line[:200]})
                continue
            await self._queue.put(event)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                logger.warning("pi stderr", extra={"line": line[:500]})

    async def _send(self, cmd: dict) -> None:
        if not self.proc or self.proc.returncode is not None:
            raise PiError("pi 子进程未运行")
        payload = (json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8")
        assert self.proc.stdin
        self.proc.stdin.write(payload)
        await self.proc.stdin.drain()

    async def _wait_response(self, cmd_id: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = await asyncio.wait_for(self._queue.get(), timeout=max(0.1, deadline - time.monotonic()))
            if event.get("type") == "response" and event.get("id") == cmd_id:
                return event
            # 其他事件（非本轮 prompt 的）先忽略；正常时序下不会出现
        raise PiError("等待 pi 响应超时")

    # ---------- 命令 ----------
    async def prompt(self, text: str, images: list[dict] | None = None) -> AsyncIterator[str]:
        await self.ensure_running()
        if self.busy:
            raise PiBusyError("pi 正在处理上一条消息")
        self.busy = True
        cmd_id = f"p{uuid.uuid4().hex[:8]}"
        body: dict = {"id": cmd_id, "type": "prompt", "message": text}
        if images:
            body["images"] = images
        t0 = time.monotonic()
        try:
            await self._send(body)
            resp = await self._wait_response(cmd_id)
            if not resp.get("success"):
                raise PiError(f"pi 拒绝 prompt: {resp.get('error') or resp}")
            # 流式消费直到本轮回合结束
            while True:
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=TURN_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.error("pi 回合超时，发送 abort")
                    await self._safe_abort()
                    raise PiError("pi 处理超时")
                etype = event.get("type")
                if etype == "message_update":
                    ev = event.get("assistantMessageEvent") or {}
                    if ev.get("type") == "text_delta" and ev.get("text"):
                        yield ev["text"]
                elif etype in ("error",):
                    raise PiError(str(event)[:300])
                elif etype in ("agent_end", "agent_settled"):
                    usage = None
                    break
        finally:
            self.busy = False
            logger.info(
                "pi turn 完成",
                extra={"cmd_id": cmd_id, "latency_ms": int((time.monotonic() - t0) * 1000), "text_len": len(text)},
            )

    async def _safe_abort(self) -> None:
        try:
            await self._send({"type": "abort"})
        except Exception:
            pass

    async def new_session(self, name: str | None = None) -> dict:
        await self.ensure_running()
        cmd_id = f"s{uuid.uuid4().hex[:8]}"
        body: dict = {"id": cmd_id, "type": "new_session"}
        if name:
            body["sessionName"] = name
        await self._send(body)
        resp = await self._wait_response(cmd_id, timeout=60)
        if not resp.get("success"):
            raise PiError(f"new_session 失败: {resp}")
        return resp.get("data") or {}

    async def get_session_info(self) -> dict:
        await self.ensure_running()
        cmd_id = f"i{uuid.uuid4().hex[:8]}"
        await self._send({"id": cmd_id, "type": "get_session_info"})
        resp = await self._wait_response(cmd_id, timeout=60)
        if not resp.get("success"):
            raise PiError(f"get_session_info 失败: {resp}")
        return resp.get("data") or {}

    async def get_available_models(self) -> list[dict]:
        await self.ensure_running()
        cmd_id = f"m{uuid.uuid4().hex[:8]}"
        await self._send({"id": cmd_id, "type": "get_available_models"})
        resp = await self._wait_response(cmd_id, timeout=60)
        if not resp.get("success"):
            raise PiError(f"get_available_models 失败: {resp}")
        return (resp.get("data") or {}).get("models", [])

    async def set_model(self, model_id: str) -> dict:
        await self.ensure_running()
        cmd_id = f"x{uuid.uuid4().hex[:8]}"
        await self._send({"id": cmd_id, "type": "set_model", "modelId": model_id})
        resp = await self._wait_response(cmd_id, timeout=60)
        if not resp.get("success"):
            raise PiError(f"set_model 失败: {resp}")
        return resp.get("data") or {}

    async def abort(self) -> None:
        await self._safe_abort()
