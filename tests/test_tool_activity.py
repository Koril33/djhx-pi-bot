import asyncio
import tempfile
import unittest

import bot
from pi_bridge import PiRpcClient


class ToolActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_activity_never_contains_tool_arguments_or_custom_name(self):
        secret = "SECRET_TOKEN_123"
        for tool_name, expected in (
            ("bash", "⚙️ 执行"),
            ("read", "🔍 读取"),
            ("custom_" + secret, "🔧 正在调用工具"),
        ):
            label = bot._tool_activity(
                tool_name, "start",
                {"command": "echo " + secret, "path": "/private/" + secret},
            )
            self.assertEqual(label, expected)
            self.assertNotIn(secret, label)
        self.assertIsNone(bot._tool_activity("bash", "end", {"command": secret}))

    async def test_rpc_event_discards_tool_arguments_before_queueing(self):
        with tempfile.TemporaryDirectory() as directory:
            client = PiRpcClient("pi", directory)
            proc = object()
            client.proc = proc
            client.busy = True
            await client._route_event(proc, {
                "type": "tool_execution_start", "toolName": "bash",
                "args": {"command": "echo SECRET_TOKEN_123"},
            })
            event = client._event_queue.get_nowait()
            self.assertEqual(event, {"type": "tool_execution_start", "toolName": "bash"})
            await client._route_event(proc, {
                "type": "tool_execution_end", "toolName": "bash",
                "isError": False, "result": "SECRET_TOKEN_123",
            })
            ended = client._event_queue.get_nowait()
            self.assertEqual(ended, {
                "type": "tool_execution_end", "toolName": "bash", "isError": False,
            })


if __name__ == "__main__":
    unittest.main()
