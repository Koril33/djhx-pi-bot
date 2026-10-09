import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Bot, Update

import bot
from config import Settings
from storage import Storage


class SessionPresetFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.settings = Settings(_env_file=None, bot_token="test", webhook_secret="test",
            allowed_user_ids="7", memory_enabled=False, pi_provider="base", pi_model="default",
            pi_session_dir=self.folder.name)
        self.storage.set_user_model(7, "personal", "main")
        self.first = self.storage.create_session(7, "工作")
        self.second = self.storage.create_session(7, "生活")
        for sid in (self.first, self.second):
            self.storage.set_pi_session_file(sid, str(Path(self.folder.name) / f"{sid}.jsonl"), known_empty=True)
        self.storage.set_active(self.first)
        self.clients = []

        def client_factory(**kwargs):
            client = SimpleNamespace(provider=kwargs["provider"], model=kwargs["model"],
                busy=False, proc=None, extra_args=kwargs["extra_args"], path=None)
            async def stop():
                client.proc = None
            async def ensure_running(path):
                client.path = path
                client.proc = SimpleNamespace(returncode=None)
            async def set_model(value):
                client.provider, client.model = value.split("/", 1)
            client.stop = AsyncMock(side_effect=stop)
            client.ensure_running = AsyncMock(side_effect=ensure_running)
            client.set_model = AsyncMock(side_effect=set_model)
            client.get_state = AsyncMock(side_effect=lambda: {"sessionFile": client.path, "messageCount": 0})
            client.has_live_session = lambda path: client.proc is not None and client.path == path
            self.clients.append(client)
            return client

        self.client_patch = patch.object(bot, "PiRpcClient", side_effect=client_factory)
        self.client_patch.start()
        self.reply_patch = patch.object(bot, "reply_text", new_callable=AsyncMock)
        self.reply = self.reply_patch.start()
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=7))

    async def asyncTearDown(self):
        self.reply_patch.stop()
        self.client_patch.stop()
        self.storage._conn.close()
        self.folder.cleanup()

    def context(self, agent, args):
        return SimpleNamespace(args=args, bot_data={"manager": SimpleNamespace(get=lambda uid: agent),
            "settings": self.settings, "storage": self.storage})

    async def test_switch_roundtrip_restores_each_model_and_persona_without_changing_defaults(self):
        persona = self.storage.create_persona({"name": "写作助手", "identity": "每次给出三种措辞。"})
        self.storage.set_session_model(7, self.first, "writing", "alpha")
        self.storage.set_session_persona(7, self.first, persona)
        self.storage.set_session_model(7, self.second, "chat", "beta")
        self.storage.set_session_persona(7, self.second, "none")
        agent = bot.AgentSession(7, self.settings, self.storage)
        await agent.ensure_client()
        self.assertIn("三种措辞", agent.persona_file.read_text(encoding="utf-8"))
        await bot.switch_user_session(agent, self.storage, 7, self.second)
        self.assertEqual((agent.client.provider, agent.client.model), ("chat", "beta"))
        self.assertNotIn("--append-system-prompt", agent.client.extra_args)
        await bot.switch_user_session(agent, self.storage, 7, self.first)
        self.assertEqual((agent.client.provider, agent.client.model), ("writing", "alpha"))
        self.assertIn("--append-system-prompt", agent.client.extra_args)
        self.assertEqual(self.storage.get_user_model(7)["provider"], "personal")
        self.assertEqual(self.storage.get_active_persona(7)["id"], "default")
        self.assertEqual(len(self.storage.list_sessions(7)), 2)

    async def test_session_config_changes_only_current_preset_and_inherit_reapplies_defaults(self):
        agent = bot.AgentSession(7, self.settings, self.storage)
        await bot.cmd_session(self.update, self.context(agent, ["config", "model", "special/fast"]))
        self.assertEqual(self.storage.get_effective_model_selection(7, self.first)["provider"], "special")
        self.assertEqual(self.storage.get_user_model(7)["provider"], "personal")
        await bot.cmd_session(self.update, self.context(agent, ["config", "persona", "none"]))
        self.assertIsNone(self.storage.get_effective_persona(7, self.first))
        await bot.cmd_session(self.update, self.context(agent, ["config", "inherit"]))
        self.assertEqual((agent.client.provider, agent.client.model), ("personal", "main"))
        self.assertEqual(self.storage.get_effective_persona(7, self.first)["id"], "default")

    async def test_busy_session_cannot_change_preferences(self):
        agent = bot.AgentSession(7, self.settings, self.storage)
        agent.client.busy = True
        await bot.cmd_session(self.update, self.context(agent, ["config", "model", "other/model"]))
        self.assertIsNone(self.storage.get_session_preferences(7, self.first)["provider"])
        agent.client.set_model.assert_not_awaited()

    async def test_personal_defaults_do_not_override_current_session_or_report_a_false_switch(self):
        self.storage.set_session_model(7, self.first, "session", "fixed")
        self.storage.set_session_persona(7, self.first, "none")
        agent = bot.AgentSession(7, self.settings, self.storage)
        await bot.cmd_model(self.update, self.context(agent, ["use", "personal/new-default"]))
        self.assertEqual(self.storage.get_user_model(7)["model_id"], "new-default")
        self.assertEqual((agent.client.provider, agent.client.model), ("session", "fixed"))
        self.assertIn("仍沿用独立模型", self.reply.await_args.args[1])
        await bot.cmd_persona(self.update, self.context(agent, ["use", "default"]))
        self.assertIsNone(self.storage.get_effective_persona(7, self.first))
        self.assertIn("仍沿用独立人格", self.reply.await_args.args[1])

    async def test_empty_path_persists_across_agent_restart_but_prompt_clears_empty_flag(self):
        agent = bot.AgentSession(7, self.settings, self.storage)
        await agent.ensure_client()
        await agent.client.stop()
        restarted = bot.AgentSession(7, self.settings, self.storage)
        await restarted.ensure_client()
        self.assertEqual(restarted.client.path, self.storage.get_session(self.first)["pi_session_file"])
        observed = []
        async def prompt(*args, **kwargs):
            observed.append(self.storage.get_session(self.first)["pi_session_empty"])
            yield "完成。"
        restarted.client.prompt = prompt
        tg = Mock(spec=Bot)
        tg.send_chat_action = AsyncMock()
        update = Update.de_json({"update_id": 1, "message": {"message_id": 1, "date": 1700000000,
            "text": "你好", "from": {"id": 7, "is_bot": False, "first_name": "User"},
            "chat": {"id": 7, "type": "private"}}}, tg)
        context = self.context(restarted, [])
        context.bot_data["manager"].memory = None
        async def consume(update, stream, *args, **kwargs):
            async for delta in stream:
                pass
        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(update, context)
        self.assertEqual(observed, [0])
        await restarted.client.stop()
        with self.assertRaises(bot.SessionRecoveryRequired):
            await restarted.ensure_client()

    async def test_model_failure_does_not_commit_new_session_preference(self):
        agent = bot.AgentSession(7, self.settings, self.storage)
        agent.client.set_model.side_effect = bot.PiError("不可用模型")
        await bot.cmd_session(self.update, self.context(agent, ["config", "model", "other/missing"]))
        self.assertIsNone(self.storage.get_session_preferences(7, self.first)["provider"])
        self.assertEqual(self.storage.get_user_model(7)["provider"], "personal")
