import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_shows_live_model_duration_and_queue(self):
        client = SimpleNamespace(
            busy=True, proc=SimpleNamespace(returncode=None),
            provider="old", model="old-id",
            get_state=AsyncMock(return_value={
                "model": {"provider": "new", "id": "actual-id"},
                "messageCount": 5,
            }),
        )
        agent = SimpleNamespace(
            client=client, prompt_pending=False, turn_started_at=100.0,
            control_lock=asyncio.Lock(), ensure_client=AsyncMock(),
            settings=SimpleNamespace(pi_provider="default", pi_model="default-id"),
        )
        manager = Mock()
        manager.get.return_value = agent
        storage = Mock()
        storage.get_active_session.return_value = {"name": "当前会话"}
        storage.get_active_persona.return_value = None
        storage.count_user_queue.return_value = 3
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        message = SimpleNamespace(edit_text=AsyncMock())
        with patch.object(bot.time, "monotonic", return_value=125.0):
            with patch.object(bot, "reply_text", new_callable=AsyncMock, return_value=message) as reply:
                await bot.cmd_status(update, context)
        self.assertIn("未实时确认", reply.await_args.args[1])
        text = message.edit_text.await_args.args[0]
        self.assertIn("当前模型: new/actual-id", text)
        self.assertIn("当前任务耗时: 0 分 25 秒", text)
        self.assertIn("排队: 3 条", text)

    async def test_status_labels_model_unverified_when_process_stopped(self):
        client = SimpleNamespace(
            busy=False, proc=None, provider="saved", model="saved-id",
        )
        agent = SimpleNamespace(
            client=client, prompt_pending=False, turn_started_at=None,
            control_lock=asyncio.Lock(),
            settings=SimpleNamespace(pi_provider="default", pi_model="default-id"),
        )
        manager = Mock()
        manager.get.return_value = agent
        storage = Mock()
        storage.get_active_session.return_value = None
        storage.get_active_persona.return_value = None
        storage.count_user_queue.return_value = 0
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_status(update, context)
        self.assertIn("saved/saved-id（下次启动使用）", reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
