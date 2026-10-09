import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Bot, Update

import bot_interactions as interactions
from config import Settings
from interactions import PendingActions
from storage import Storage


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.storage.ensure_user(7, "tester")
        self.sid = self.storage.create_session(7, "测试")
        self.storage.set_active(self.sid)
        self.actions = PendingActions(self.storage.db_path)
        self.agent = SimpleNamespace(
            user_id=7, client=SimpleNamespace(busy=False, proc=SimpleNamespace(returncode=None)),
            prompt_pending=False, turn_tail=None, turn_task=None, cancel_requested=None,
            execution_phase=None, turn_started_at=None,
        )
        self.manager = SimpleNamespace(get=lambda uid: self.agent, actions=self.actions)
        self.context = SimpleNamespace(bot=Bot("123:test"), bot_data={
            "settings": Settings(_env_file=None), "storage": self.storage, "manager": self.manager,
        })
        interactions._voice_lock = asyncio.Lock()
        interactions._dialogs.clear()

    async def asyncTearDown(self):
        interactions._dialogs.clear()
        self.actions.close()
        self.storage._conn.close()
        self.folder.cleanup()

    def callback(self, data, uid=7, update_id=501):
        return SimpleNamespace(
            update_id=update_id, effective_user=SimpleNamespace(id=uid),
            callback_query=SimpleNamespace(data=data, answer=AsyncMock()),
        )

    def voice_update(self):
        message = {"message_id": 100, "date": 1,
                   "chat": {"id": 7, "type": "private"},
                   "from": {"id": 7, "is_bot": False, "first_name": "tester"},
                   "voice": {"file_id": "test", "file_unique_id": "test", "duration": 4}}
        audio_file = SimpleNamespace(file_size=15, download_as_bytearray=AsyncMock(return_value=b"voice"))
        voice = SimpleNamespace(duration=4, file_size=15, get_file=AsyncMock(return_value=audio_file))
        return SimpleNamespace(
            update_id=400, effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7),
            message=SimpleNamespace(voice=voice, audio=None, to_json=lambda: json.dumps(message)),
        )

    async def test_voice_previews_and_only_confirmed_owner_runs_once(self):
        update = self.voice_update()
        update.message.caption = "请用英文回答"
        with patch("bot.reply_text", new_callable=AsyncMock) as reply, \
             patch("bot_interactions.transcribe_voice", new_callable=AsyncMock) as transcribe, \
             patch("bot.process_confirmed_attachment", new_callable=AsyncMock, create=True) as on_text:
            transcribe.return_value = {"text": "请计算二加二", "seconds": 4}
            await interactions.handle_voice(update, self.context)
            on_text.assert_not_awaited()
            preview = reply.await_args_list[-1]
            action = preview.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            await interactions.handle_interaction_callback(self.callback(action, uid=8), self.context)
            on_text.assert_not_awaited()
            await interactions.handle_interaction_callback(self.callback(action), self.context)
            on_text.assert_awaited_once()
            forwarded = on_text.await_args.args[0]
            self.assertIsInstance(forwarded, Update)
            self.assertEqual(forwarded.message.text, "请用英文回答\n\n[语音转写]\n请计算二加二")
            self.assertIsNone(forwarded.message.voice)
            await interactions.handle_interaction_callback(self.callback(action, update_id=502), self.context)
            on_text.assert_awaited_once()

    async def test_voice_confirmation_rejects_switched_session_without_consuming(self):
        ident = self.actions.create(7, 7, "voice", {"session_id": self.sid, "text": "text", "message": {}})
        other = self.storage.create_session(7, "另一个")
        self.storage.set_active(other)
        with patch("bot.reply_text", new_callable=AsyncMock) as reply, \
             patch("bot.process_confirmed_attachment", new_callable=AsyncMock, create=True) as on_text:
            await interactions.handle_interaction_callback(self.callback("voice_confirm:" + ident), self.context)
        on_text.assert_not_awaited()
        self.assertIn("会话已切换", reply.await_args.args[1])
        self.assertIsNotNone(self.actions.get(7, ident))

    async def test_stop_cancels_voice_worker_and_creates_no_confirmation(self):
        started = asyncio.Event()
        async def transcribe(*args):
            started.set()
            await asyncio.Future()
        with patch("bot.reply_text", new_callable=AsyncMock) as reply, \
             patch("bot_interactions.transcribe_voice", side_effect=transcribe):
            task = asyncio.create_task(interactions.handle_voice(self.voice_update(), self.context))
            await started.wait()
            self.agent.cancel_requested.set()
            self.agent.turn_task.cancel()
            await task
        self.assertIsNone(self.agent.turn_task)
        self.assertIsNone(self.agent.turn_tail)
        self.assertEqual(reply.await_count, 1)
        self.assertEqual(self.actions._conn.execute("SELECT count(*) FROM pending_actions").fetchone()[0], 0)

    async def test_extension_confirm_owner_and_protocol_callback(self):
        self.agent.client.set_extension_ui_handler = Mock()
        self.agent.client.response_extension_ui = AsyncMock()
        sent = asyncio.Event()
        async def send(*args, **kwargs):
            sent.set()
        with patch("bot._send", side_effect=send), patch("bot.reply_text", new_callable=AsyncMock):
            interactions.install_extension_ui(self.agent, self.context)
            handler = self.agent.client.set_extension_ui_handler.call_args.args[0]
            task = asyncio.create_task(handler({"id": "rpc1", "method": "confirm", "title": "test"}))
            await sent.wait()
            ident = next(iter(interactions._dialogs))
            await interactions.handle_interaction_callback(self.callback(f"piui:{ident}:yes", uid=8), self.context)
            self.assertFalse(task.done())
            await interactions.handle_interaction_callback(self.callback(f"piui:{ident}:yes"), self.context)
            await task
        self.agent.client.response_extension_ui.assert_awaited_once_with("rpc1", value=True, cancelled=False)
        self.assertEqual(interactions._dialogs, {})

    async def test_extension_input_short_deadline_and_process_replacement(self):
        self.agent.client.set_extension_ui_handler = Mock()
        self.agent.client.response_extension_ui = AsyncMock()
        sent = asyncio.Event()
        async def send(*args, **kwargs):
            sent.set()
        with patch("bot._send", side_effect=send), patch("bot.reply_text", new_callable=AsyncMock):
            interactions.install_extension_ui(self.agent, self.context)
            handler = self.agent.client.set_extension_ui_handler.call_args.args[0]
            task = asyncio.create_task(handler({"id": "rpc2", "method": "input"}))
            await sent.wait()
            ident = next(iter(interactions._dialogs))
            update = SimpleNamespace(message=SimpleNamespace(text=f"/answer {ident} 我的输入"), effective_user=SimpleNamespace(id=7))
            await interactions.cmd_answer(update, self.context)
            await task
            self.agent.client.response_extension_ui.assert_awaited_once_with("rpc2", value="我的输入", cancelled=False)
            self.agent.client.response_extension_ui.reset_mock()
            sent.clear()
            task = asyncio.create_task(handler({"id": "rpc3", "method": "confirm", "timeout": 100}))
            await sent.wait()
            self.agent.client.proc = SimpleNamespace(returncode=None)
            await task
        self.agent.client.response_extension_ui.assert_not_awaited()
        self.assertEqual(interactions._dialogs, {})
