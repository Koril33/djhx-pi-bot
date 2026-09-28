import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
from storage import Storage
from pi_bridge import PiError, PiRpcClient


class ModelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "bot.db")
        self.storage = Storage(self.db_path)

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_catalog_shows_current_capabilities_prices_and_full_ids(self):
        models = [
            {"provider": "one", "id": "shared", "name": "Alpha", "input": ["text", "image"],
             "reasoning": True, "cost": {"input": 0.3, "output": 1.2}},
            {"provider": "two", "id": "shared", "name": "Beta", "input": ["text"],
             "cost": {"input": 1.0, "output": 2.0}},
        ]
        text, markup = bot.model_catalog_view(models, "two", "shared")
        self.assertIn("○ Alpha", text)
        self.assertIn("● Beta", text)
        self.assertIn("图片", text)
        self.assertIn("推理", text)
        self.assertIn("$0.3", text)
        self.assertIn("one/shared", text)
        self.assertIn("two/shared", text)
        self.assertNotIn(chr(92) + "n", text)
        self.assertEqual(
            [row[0].callback_data for row in markup.inline_keyboard],
            ["model_use:one/shared", "model_use:two/shared"],
        )

    async def test_bare_model_id_rejects_ambiguous_provider(self):
        client = PiRpcClient("pi", self.temp.name, provider="one")
        client.get_available_models = AsyncMock(return_value=[
            {"provider": "one", "id": "shared"},
            {"provider": "two", "id": "shared"},
        ])
        client._command = AsyncMock()
        with self.assertRaisesRegex(PiError, "多个提供商"):
            await client.set_model("shared")
        client._command.assert_not_awaited()

    async def test_saved_user_model_is_used_after_storage_reopen(self):
        self.storage.set_user_model(7, "other", "model-id")
        self.storage._conn.close()
        self.storage = Storage(self.db_path)
        settings = SimpleNamespace(
            pi_cmd="pi", pi_session_dir=self.temp.name,
            pi_provider="deepseek", pi_model="deepseek-flash",
            memory_enabled=False,
        )
        agent = bot.AgentSession(7, settings, self.storage)
        self.assertEqual((agent.client.provider, agent.client.model), ("other", "model-id"))

    async def test_model_use_persists_successful_rpc_choice(self):
        client = SimpleNamespace(
            provider="deepseek", model="deepseek-flash", busy=False,
        )

        async def set_model(value):
            client.provider, client.model = value.split("/", 1)

        client.set_model = AsyncMock(side_effect=set_model)
        agent = SimpleNamespace(
            client=client, prompt_pending=False,
            ensure_client=AsyncMock(), control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(
            args=["use", "other/model-id"],
            bot_data={"manager": manager, "storage": self.storage},
        )
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_model(update, context)
        self.assertEqual(tuple(self.storage.get_user_model(7)), ("other", "model-id"))
        self.assertIn("重启后保持", reply.await_args.args[1])

    async def test_model_switch_rolls_back_if_save_fails(self):
        client = SimpleNamespace(provider="deepseek", model="deepseek-flash", busy=False)

        async def set_model(value):
            client.provider, client.model = value.split("/", 1)

        client.set_model = AsyncMock(side_effect=set_model)
        agent = SimpleNamespace(
            client=client, prompt_pending=False,
            ensure_client=AsyncMock(), control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(
            args=["use", "other/model-id"],
            bot_data={"manager": manager, "storage": self.storage},
        )
        with patch.object(self.storage, "set_user_model", side_effect=OSError("disk error")):
            with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
                await bot.cmd_model(update, context)
        self.assertEqual((client.provider, client.model), ("deepseek", "deepseek-flash"))
        self.assertEqual(client.set_model.await_count, 2)
        self.assertIn("切换模型失败", reply.await_args.args[1])



if __name__ == "__main__":
    unittest.main()
