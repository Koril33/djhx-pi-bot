import tempfile
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import AgentSession, cmd_persona, on_callback
from config import Settings
from admin import _run_persona_test
from persona import render_persona, validate_persona
from storage import Storage


class PersonaStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))

    def tearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    def test_default_seed_and_selection_survive_reopen(self):
        default = self.storage.get_active_persona(7)
        self.assertEqual(default["id"], "default")
        self.assertIn("小丁", render_persona(default))
        values = validate_persona({"name": "简洁助手", "identity": "你是简洁助手。",
                                   "communication_style": "每次只给两句话。"})
        persona_id = self.storage.create_persona(values)
        self.storage.set_active_persona(7, persona_id)
        self.storage._conn.close()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.assertEqual(self.storage.get_active_persona(7)["id"], persona_id)
        self.assertEqual(self.storage.get_active_persona(8)["id"], "default")
        self.assertEqual(self.storage.get_active_persona(7)["communication_style"], "每次只给两句话。")

    def test_disable_resets_selection_and_default_cannot_be_deleted(self):
        persona_id = self.storage.create_persona({"name": "测试", "identity": "测试身份"})
        self.storage.set_active_persona(7, persona_id)
        self.assertTrue(self.storage.set_persona_enabled(persona_id, False))
        self.assertEqual(self.storage.get_active_persona(7)["id"], "default")
        with self.assertRaises(ValueError):
            self.storage.set_active_persona(7, persona_id)
        with self.assertRaises(ValueError):
            self.storage.delete_persona("default")
        self.assertTrue(self.storage.delete_persona(persona_id))

    def test_invalid_persona_is_rejected(self):
        with self.assertRaises(ValueError):
            self.storage.create_persona({"name": "空身份", "identity": ""})
        with self.assertRaises(ValueError):
            self.storage.create_persona({"name": "超长", "identity": "x" * 4001})

    def test_explicit_none_survives_reopen_and_can_switch_back(self):
        self.storage.set_active_persona(7, "none")
        self.assertIsNone(self.storage.get_active_persona(7))
        self.assertEqual(self.storage.get_active_persona(8)["id"], "default")
        self.storage._conn.close()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.assertIsNone(self.storage.get_active_persona(7))
        self.storage.set_active_persona(7, "default")
        self.assertEqual(self.storage.get_active_persona(7)["id"], "default")


class PersonaAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.settings = Settings(_env_file=None, bot_token="test", webhook_secret="test",
                                 allowed_user_ids="7", memory_enabled=False,
                                 pi_session_dir=str(Path(self.folder.name) / "sessions"))

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    async def test_selection_reloads_prompt_without_changing_session(self):
        session_id = self.storage.create_session(7, "现有会话")
        self.storage.set_active(session_id)
        self.storage.set_pi_session_file(session_id, "/existing/history.jsonl")
        with patch("bot.PiRpcClient") as client_type:
            client = client_type.return_value
            client.busy = False
            client.stop = AsyncMock()
            client.ensure_running = AsyncMock()
            agent = AgentSession(7, self.settings, self.storage)
            client.extra_args = client_type.call_args.kwargs["extra_args"]
            self.assertIn("小丁", agent.persona_file.read_text(encoding="utf-8"))
            new_id = self.storage.create_persona({"name": "新朋友", "identity": "你是一位理性的朋友。"})
            self.storage.set_active_persona(7, new_id)
            await agent.ensure_client()
        client.stop.assert_awaited_once()
        client.ensure_running.assert_awaited_once_with("/existing/history.jsonl")
        self.assertIn("你是一位理性的朋友。", agent.persona_file.read_text(encoding="utf-8"))
        self.assertEqual(self.storage.get_active_session(7)["id"], session_id)
        args = client_type.call_args.kwargs["extra_args"]
        self.assertEqual(Path(args[args.index("--append-system-prompt") + 1]), agent.persona_file)

    async def test_none_omits_prompt_and_switch_preserves_session(self):
        session_id = self.storage.create_session(7, "现有会话")
        self.storage.set_active(session_id)
        self.storage.set_pi_session_file(session_id, "/existing/history.jsonl")
        self.storage.set_active_persona(7, "none")
        with patch("bot.PiRpcClient") as client_type:
            client = client_type.return_value
            client.busy = False
            client.stop = AsyncMock()
            client.ensure_running = AsyncMock()
            agent = AgentSession(7, self.settings, self.storage)
            client.extra_args = client_type.call_args.kwargs["extra_args"]
            self.assertNotIn("--append-system-prompt", client.extra_args)
            self.storage.set_active_persona(7, "default")
            await agent.ensure_client()
            self.assertIn("--append-system-prompt", client.extra_args)
            self.storage.set_active_persona(7, "none")
            await agent.ensure_client()
            self.assertNotIn("--append-system-prompt", client.extra_args)
        self.assertEqual(client.stop.await_count, 2)
        self.assertEqual(client.ensure_running.await_count, 2)
        self.assertEqual(client.ensure_running.call_args.args[0], "/existing/history.jsonl")
        self.assertEqual(self.storage.get_active_session(7)["id"], session_id)
        self.assertFalse(agent.persona_file.exists())

    async def test_persona_list_buttons_switch_through_callback(self):
        new_id = self.storage.create_persona({"name": "工作伙伴", "identity": "你是工作伙伴。"})
        agent = SimpleNamespace(control_lock=asyncio.Lock(), client=SimpleNamespace(busy=False),
                                prompt_pending=False)
        context = SimpleNamespace(args=["list"], bot_data={
            "storage": self.storage, "manager": SimpleNamespace(get=lambda user_id: agent),
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch("bot.reply_text", new_callable=AsyncMock) as reply:
            await cmd_persona(update, context)
        buttons = reply.await_args.kwargs["reply_markup"].inline_keyboard
        callbacks = [row[0].callback_data for row in buttons]
        self.assertIn("persona_use:none", callbacks)
        self.assertIn(f"persona_use:{new_id}", callbacks)
        query = SimpleNamespace(data=f"persona_use:{new_id}", answer=AsyncMock())
        update.callback_query = query
        with patch("bot.reply_text", new_callable=AsyncMock):
            await on_callback(update, context)
        query.answer.assert_awaited_once()
        self.assertEqual(self.storage.get_active_persona(7)["id"], new_id)

    async def test_telegram_command_selects_persona(self):
        new_id = self.storage.create_persona({"name": "工作伙伴", "identity": "你是工作伙伴。"})
        agent = SimpleNamespace(control_lock=asyncio.Lock(), client=SimpleNamespace(busy=False),
                                prompt_pending=False)
        context = SimpleNamespace(args=["use", new_id], bot_data={
            "storage": self.storage, "manager": SimpleNamespace(get=lambda user_id: agent),
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch("bot.reply_text", new_callable=AsyncMock) as reply:
            await cmd_persona(update, context)
        self.assertEqual(self.storage.get_active_persona(7)["id"], new_id)
        self.assertIn("下一条消息起生效", reply.call_args.args[1])
        context.args = ["use", "none"]
        with patch("bot.reply_text", new_callable=AsyncMock) as reply:
            await cmd_persona(update, context)
        self.assertIsNone(self.storage.get_active_persona(7))
        self.assertIn("无人格", reply.call_args.args[1])

    async def test_admin_test_uses_isolated_tool_free_pi(self):
        async def answer(_question, *, on_usage):
            yield "你好，小丁。"

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=self.settings, storage=self.storage,
        )))
        with patch("admin.PiRpcClient") as client_type:
            client = client_type.return_value
            client.prompt = answer
            client.stop = AsyncMock()
            result = await _run_persona_test(request, self.storage.get_persona("default"), 7, "你好")
        args = client_type.call_args.kwargs["extra_args"]
        self.assertIn("--no-tools", args)
        self.assertIn("--no-extensions", args)
        self.assertIn("--append-system-prompt", args)
        self.assertIn("你好，小丁。", result)
        self.assertEqual(self.storage.get_last_usage_run(7, origin="persona_test")["status"], "complete")


if __name__ == "__main__":
    unittest.main()
