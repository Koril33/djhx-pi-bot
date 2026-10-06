import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from pi_bridge import PiBusyError, PiError, PiRpcClient


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, data: bytes) -> None:
        for raw in data.splitlines():
            if raw:
                self.proc.commands.append(json.loads(raw))
                self.proc.on_command(self.proc.commands[-1])

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeProc:
    def __init__(self, on_command=None):
        # 与 asyncio 子进程默认的 64 KiB reader limit 一致。
        self.stdout = asyncio.StreamReader(limit=64 * 1024)
        self.stderr = asyncio.StreamReader(limit=64 * 1024)
        self.stdin = FakeStdin(self)
        self.commands = []
        self.on_command = on_command or (lambda command: None)
        self.returncode = None
        self._exited = asyncio.Event()

    def emit(self, event: dict) -> None:
        self.stdout.feed_data(json.dumps(event, ensure_ascii=False).encode() + b"\n")

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode


class PiRpcClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.client = PiRpcClient("pi", self.tempdir.name)

    async def asyncTearDown(self) -> None:
        await self.client.stop()
        self.tempdir.cleanup()

    async def test_collects_each_model_call_and_compaction_usage(self) -> None:
        usage = {"input": 2, "cacheRead": 3, "cacheWrite": 0,
                 "output": 4, "reasoning": 1, "totalTokens": 9}

        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            for index in range(2):
                proc.emit({"type": "message_end", "message": {"role": "assistant",
                    "provider": "deepseek", "model": "deepseek-flash", "timestamp": 1790215200000,
                    "usage": usage, "stopReason": "toolUse" if index == 0 else "stop"}})
            proc.emit({"type": "compaction_end", "result": {"usage": usage}})
            proc.emit({"type": "agent_settled"})

        proc = FakeProc(on_command)
        received = []
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            result = "".join([part async for part in self.client.prompt("hello", on_usage=received.append)])
        self.assertEqual(result, "")
        self.assertEqual([item["kind"] for item in received], ["assistant", "assistant", "compaction"])
        self.assertEqual(received[0]["occurred_at"], 1790215200)
        self.assertEqual(received[0]["usage"]["cacheRead"], 3)

    async def test_tool_end_exposes_only_error_flag_to_callback(self) -> None:
        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            proc.emit({"type": "tool_execution_end", "toolName": "bash",
                       "isError": False, "result": "private output"})
            proc.emit({"type": "agent_settled"})

        proc = FakeProc(on_command)
        seen = []
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            result = "".join([part async for part in self.client.prompt(
                "hello", on_tool=lambda *args: seen.append(args),
            )])
        self.assertEqual(result, "")
        self.assertEqual(seen, [("bash", "end", {"is_error": False})])

    async def test_extension_dialog_is_auto_cancelled_but_widget_is_ignored(self) -> None:
        def on_command(command):
            if command.get("type") != "prompt":
                return
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            proc.emit({"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Authorize?"})
            proc.emit({"type": "extension_ui_request", "id": "ui-2", "method": "setWidget", "widgetKey": "subagent-async"})
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ok"}})
            proc.emit({"type": "agent_settled"})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            result = "".join([part async for part in self.client.prompt("hello")])
        self.assertEqual(result, "ok")
        responses = [cmd for cmd in proc.commands if cmd.get("type") == "extension_ui_response"]
        self.assertEqual(responses, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])

    async def test_reads_120kb_record_and_waits_for_agent_settled(self) -> None:
        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "A"}})
            proc.emit({"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": "x" * 120_000}]}})
            proc.emit({"type": "agent_end", "willRetry": True})
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "B"}})
            proc.emit({"type": "agent_settled"})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            result = "".join([part async for part in self.client.prompt("hello")])
        self.assertEqual(result, "AB")
        self.assertFalse(self.client.busy)
        self.assertIsNone(self.client._transport_error)

    async def test_concurrent_state_and_steer_responses_do_not_consume_prompt_events(self) -> None:
        first_delta = asyncio.Event()
        outstanding = {}

        def on_command(command):
            if command["type"] == "prompt":
                proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
                proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "A"}})
            elif command["type"] in ("get_state", "steer"):
                outstanding[command["type"]] = command
                if len(outstanding) == 2:
                    for name in ("steer", "get_state"):
                        cmd = outstanding[name]
                        proc.emit({"id": cmd["id"], "type": "response", "command": name, "success": True,
                                   "data": {"sessionId": "s1"} if name == "get_state" else {}})
                    proc.emit({"type": "agent_end", "willRetry": False})
                    proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "B"}})
                    proc.emit({"type": "agent_settled"})

        async def consume():
            parts = []
            async for part in self.client.prompt("hello"):
                parts.append(part)
                first_delta.set()
            return "".join(parts)

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            prompt_task = asyncio.create_task(consume())
            await asyncio.wait_for(first_delta.wait(), 1)
            state, _ = await asyncio.wait_for(
                asyncio.gather(
                    self.client.get_state(),
                    self.client.steer("change", images=[{"type": "image", "data": "YWJj", "mimeType": "image/png"}]),
                ), 1
            )
            result = await asyncio.wait_for(prompt_task, 1)
        self.assertEqual(state["sessionId"], "s1")
        self.assertEqual(result, "AB")
        self.assertEqual(outstanding["steer"]["images"], [
            {"type": "image", "data": "YWJj", "mimeType": "image/png"}
        ])

    async def test_stdout_eof_fails_request_immediately_and_next_command_restarts(self) -> None:
        def on_first(command):
            first.stdout.feed_eof()  # 进程仍“存活”，但协议 reader 已退出。

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": command["type"],
                         "success": True, "data": {"sessionId": "new"}})

        first = FakeProc(on_first)
        second = FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            with self.assertRaisesRegex(PiError, "stdout 已关闭"):
                await asyncio.wait_for(self.client.get_state(), 1)
            state = await asyncio.wait_for(self.client.get_state(), 1)
        self.assertEqual(state["sessionId"], "new")
        self.assertEqual(first.returncode, 0)

    async def test_response_timeout_has_readable_error(self) -> None:
        first = FakeProc()

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "get_state",
                         "success": True, "data": {"sessionId": "recovered"}})

        second = FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            await self.client.ensure_running()
            with self.assertRaisesRegex(PiError, "get_state 响应超时"):
                await self.client._request({"id": "test-timeout", "type": "get_state"}, timeout=0.01)
            state = await asyncio.wait_for(self.client.get_state(), 1)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(state["sessionId"], "recovered")

    async def test_turn_timeout_recycles_stuck_process(self) -> None:
        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "get_state",
                         "success": True, "data": {"sessionId": "recovered"}})

        first = FakeProc(on_first)
        second = FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            return next(processes)

        with (
            patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn),
            patch("pi_bridge.TURN_TIMEOUT", 0.01),
        ):
            with self.assertRaisesRegex(PiError, "处理超时"):
                _ = [part async for part in self.client.prompt("hang")]
            state = await asyncio.wait_for(self.client.get_state(), 1)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(state["sessionId"], "recovered")

    async def test_cancelled_session_change_is_error(self) -> None:
        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": command["type"],
                       "success": True, "data": {"cancelled": True}})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            with self.assertRaisesRegex(PiError, "取消"):
                await self.client.new_session("test")
            with self.assertRaisesRegex(PiError, "取消"):
                await self.client.switch_session("/tmp/session.jsonl")

    async def test_named_new_session_sends_name_field_without_argument_collision(self) -> None:
        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": command["type"],
                       "success": True, "data": {}})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            await self.client.new_session("回归测试")
        self.assertEqual([command["type"] for command in proc.commands], ["new_session", "set_session_name"])
        self.assertEqual(proc.commands[1]["name"], "回归测试")

    async def test_live_process_reconnects_when_requested_session_differs(self) -> None:
        first = FakeProc()
        second = FakeProc()
        starts = []

        async def spawn(*args, **kwargs):
            starts.append(args)
            return (first, second)[len(starts) - 1]

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            await self.client.ensure_running("/sessions/old.jsonl")
            self.client._session_file = None  # pi 已创建新会话，但 DB 仍指向旧会话。
            self.client.busy = True
            with self.assertRaises(PiBusyError):
                await self.client.ensure_running("/sessions/old.jsonl")
            self.assertIsNone(first.returncode)
            self.client.busy = False
            await self.client.ensure_running("/sessions/old.jsonl")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1][starts[1].index("--session") + 1], "/sessions/old.jsonl")

    async def test_session_name_failure_does_not_report_created_session_as_failed(self) -> None:
        def on_command(command):
            success = command["type"] != "set_session_name"
            proc.emit({"id": command["id"], "type": "response", "command": command["type"],
                       "success": success, "error": "naming unavailable" if not success else None,
                       "data": {"sessionFile": "/new.jsonl"} if success else {}})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            data = await self.client.new_session("name")
        self.assertEqual(data["sessionFile"], "/new.jsonl")

    async def test_provider_error_reported_after_settled(self) -> None:
        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            proc.emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "provider unavailable"}})
            proc.emit({"type": "agent_settled"})

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            with self.assertRaisesRegex(PiError, "provider unavailable"):
                _ = [part async for part in self.client.prompt("hello")]

    async def test_reconnect_uses_session_file_observed_from_state(self) -> None:
        starts = []

        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": "get_state", "success": True,
                        "data": {"sessionFile": "/sessions/current.jsonl"}})
            first.stdout.feed_eof()

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "get_state", "success": True,
                         "data": {"sessionFile": "/sessions/current.jsonl"}})

        first, second = FakeProc(on_first), FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            starts.append(args)
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            await self.client.ensure_running("/sessions/original.jsonl")
            await self.client.get_state()
            await self.client.get_state()
        self.assertEqual(starts[1][starts[1].index("--session") + 1], "/sessions/current.jsonl")

    async def test_switch_session_and_model_survive_reconnect(self) -> None:
        starts = []

        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": command["type"],
                        "success": True, "data": {}})
            if command["type"] == "set_model":
                first.stdout.feed_eof()

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "get_state", "success": True,
                         "data": {"sessionFile": "/sessions/switched.jsonl"}})

        first, second = FakeProc(on_first), FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            starts.append(args)
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            await self.client.ensure_running("/sessions/original.jsonl")
            await self.client.switch_session("/sessions/switched.jsonl")
            await self.client.set_model("other/model-id")
            await self.client.get_state()
        self.assertEqual(starts[1][starts[1].index("--session") + 1], "/sessions/switched.jsonl")
        self.assertEqual(starts[1][starts[1].index("--provider") + 1], "other")
        self.assertEqual(starts[1][starts[1].index("--model") + 1], "model-id")

    async def test_new_session_discards_old_path_before_reconnect(self) -> None:
        starts = []

        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": command["type"],
                        "success": True, "data": {"cancelled": False}})
            if command["type"] == "new_session":
                first.stdout.feed_eof()

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "get_state", "success": True,
                         "data": {"sessionFile": "/sessions/new.jsonl"}})

        first, second = FakeProc(on_first), FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            starts.append(args)
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            await self.client.ensure_running("/sessions/old.jsonl")
            await self.client.new_session()
            state = await self.client.get_state()
        self.assertNotIn("--session", starts[1])
        self.assertEqual(state["sessionFile"], "/sessions/new.jsonl")

    async def test_early_close_restarts_process_before_next_prompt(self) -> None:
        starts = []

        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            first.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "old"}})

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            second.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "new"}})
            second.emit({"type": "agent_settled"})

        first, second = FakeProc(on_first), FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            starts.append(args)
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            stream = self.client.prompt("first")
            self.assertEqual(await stream.__anext__(), "old")
            await stream.aclose()
            self.assertEqual("".join([part async for part in self.client.prompt("second")]), "new")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(starts), 2)

    async def test_cancelled_prompt_terminates_old_run(self) -> None:
        sent = asyncio.Event()

        def on_first(command):
            first.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            sent.set()

        def on_second(command):
            second.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            second.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "fresh"}})
            second.emit({"type": "agent_settled"})

        first, second = FakeProc(on_first), FakeProc(on_second)
        processes = iter((first, second))

        async def spawn(*args, **kwargs):
            return next(processes)

        with patch("pi_bridge.asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(collect(self.client.prompt("cancel")))
            await asyncio.wait_for(sent.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            output = await asyncio.wait_for(collect(self.client.prompt("next")), 1)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(output, "fresh")

    async def test_event_queue_filters_large_tool_results_and_applies_backpressure(self) -> None:
        thinking_started = asyncio.Event()
        release_thinking = asyncio.Event()

        def on_command(command):
            proc.emit({"id": command["id"], "type": "response", "command": "prompt", "success": True})
            proc.emit({"type": "message_end", "message": {"role": "toolResult", "content": "x" * 120_000}})
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "secret"}})
            for _ in range(500):
                proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}})
            proc.emit({"type": "agent_settled"})

        async def on_thinking():
            thinking_started.set()
            await release_thinking.wait()

        proc = FakeProc(on_command)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            task = asyncio.create_task(collect(self.client.prompt("long", on_thinking=on_thinking)))
            await asyncio.wait_for(thinking_started.wait(), 1)
            await asyncio.sleep(0.02)
            self.assertEqual(self.client._event_queue.qsize(), self.client._event_queue.maxsize)
            release_thinking.set()
            output = await asyncio.wait_for(task, 1)
        self.assertEqual(output, "x" * 500)


async def collect(stream):
    return "".join([part async for part in stream])


if __name__ == "__main__":
    unittest.main()
