import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telegram.error import NetworkError

import bot
from delivery import DeliveryStore
from pi_bridge import PiRpcClient
from tests import test_bot_images
from tests.test_pi_bridge import FakeProc


class ReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "bot.db")
        self.store = DeliveryStore(self.path)
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7), callback_query=None,
            message=SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=101))),
        )
        self.context = SimpleNamespace(
            args=[], bot=SimpleNamespace(edit_message_text=AsyncMock()),
            bot_data={"delivery": self.store, "manager": Mock()},
        )

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def cached(self, *, uncertain=False, user_id=7, message_id=None):
        reply_id = self.store.create(user_id, user_id, 1, "session")
        self.store.finish(reply_id, "保存的结果", complete=True)
        self.store.plan(reply_id, ["保存的结果"])
        if message_id is not None:
            self.store.stage(reply_id, 0, "保存的结果", message_id)
        if uncertain:
            self.store.failed(reply_id, 0, uncertain=True, error_kind="TimedOut")
        return reply_id

    async def test_reopen_recovers_unknown_sends_and_keeps_full_result(self):
        reply_id = self.store.create(7, 7, 1, "session")
        self.store.checkpoint(reply_id, "完整保存的文字" * 1000)
        self.store.plan(reply_id, ["完整保存的文字"])
        self.store.sending(reply_id, 0)
        self.store.close()
        self.store = DeliveryStore(self.path)
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.get(7, reply_id)["execution_state"], "interrupted")
        self.assertEqual(self.store.get(7, reply_id)["full_text"], "完整保存的文字" * 1000)
        self.assertEqual(self.store.chunks(reply_id)[0]["status"], "uncertain")

    async def test_resend_uses_known_message_id_without_new_send_or_model(self):
        self.cached(message_id=123)
        await bot.cmd_resend(self.update, self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        self.update.message.reply_text.assert_not_awaited()
        self.context.bot_data["manager"].get.assert_not_called()

    async def test_uncertain_send_requires_confirmation_and_survives_replanning(self):
        reply_id = self.cached(uncertain=True)
        self.store.plan(reply_id, ["保存的结果"])
        await bot.cmd_resend(self.update, self.context)
        self.assertIn("可能已经到达", self.update.message.reply_text.await_args.args[0])
        self.assertEqual(self.store.chunks(reply_id)[0]["status"], "uncertain")
        self.update.message.reply_text.reset_mock()
        self.context.args = [reply_id, "confirm"]
        await bot.cmd_resend(self.update, self.context)
        self.update.message.reply_text.assert_awaited_once()
        self.assertEqual(self.store.chunks(reply_id)[0]["message_id"], 101)
        self.assertEqual(self.store.chunks(reply_id)[0]["status"], "sent")

    async def test_cannot_resend_other_users_result(self):
        self.context.args = [self.cached(user_id=8)]
        await bot.cmd_resend(self.update, self.context)
        self.assertIn("属于你的", self.update.message.reply_text.await_args.args[0])
        self.context.bot.edit_message_text.assert_not_awaited()

    async def test_concurrent_resends_create_only_one_new_message(self):
        self.cached()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=101)
        self.update.message.reply_text.side_effect = send
        first = asyncio.create_task(bot.cmd_resend(self.update, self.context))
        await entered.wait()
        second = asyncio.create_task(bot.cmd_resend(self.update, self.context))
        await asyncio.sleep(0)
        self.assertEqual(self.update.message.reply_text.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.update.message.reply_text.await_count, 1)
        self.context.bot.edit_message_text.assert_awaited_once()

    async def test_telegram_failure_does_not_stop_rpc_and_caches_all_output(self):
        client = PiRpcClient("pi", self.temp.name)
        async def finish():
            await asyncio.sleep(0.03)
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "最终结果"}})
            proc.emit({"type": "agent_settled"})
        def command(cmd):
            proc.emit({"type": "response", "id": cmd["id"], "success": True})
            proc.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "前半部分"}})
            asyncio.create_task(finish())
        proc = FakeProc(command)
        reply_id = self.store.create(7, 7, 1, None)
        with patch("pi_bridge.asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(bot, "_send", side_effect=bot.DeliveryUncertainError("offline")):
                result = await bot.stream_reply(
                    self.update, client.prompt("do work"), interval=0, initial_chars=1,
                    delivery_store=self.store, delivery_id=reply_id,
                )
        self.assertTrue(result["delivery_failed"])
        self.assertEqual(result["chunks"], ["前半部分最终结果"])
        self.assertIsNone(proc.returncode)
        self.assertFalse(client.busy)
        self.assertEqual(self.store.chunks(reply_id)[0]["status"], "uncertain")
        await client.stop()

    async def test_final_send_failure_keeps_complete_reply_independent_of_memory(self):
        fixture = test_bot_images.ImageMessageTests(); fixture.setUp()
        fixture.manager.delivery = self.store
        fixture.message.photo = []
        fixture.message.text = "question"
        fixture.message.caption = None
        with patch.object(bot, "_send", side_effect=bot.DeliveryUncertainError("offline")):
            await bot._process_text(fixture.update, fixture.context)
        row = self.store.get(1)
        self.assertEqual(row["full_text"], "红色")
        self.assertEqual(row["execution_state"], "complete")
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(len(self.store.pending(1)), 1)


class StartupAndStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_download_prevents_prompt_start(self):
        fixture = test_bot_images.ImageMessageTests(); fixture.setUp()
        fixture.client.abort = AsyncMock()
        fixture.storage.cancel_user_queue.return_value = 0
        entered, release = asyncio.Event(), asyncio.Event()
        remote = fixture.media.get_file.return_value
        async def download():
            entered.set(); await release.wait(); return remote
        fixture.media.get_file.side_effect = download
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            task = asyncio.create_task(bot._process_text(fixture.update, fixture.context))
            await entered.wait()
            await bot.cmd_stop(fixture.update, fixture.context)
            await task
            release.set()
        self.assertEqual(fixture.calls, [])
        fixture.client.abort.assert_not_awaited()
        self.assertIsNone(fixture.agent.turn_task)

    async def test_status_is_immediate_while_control_lock_is_held(self):
        fixture = test_bot_images.ImageMessageTests(); fixture.setUp()
        fixture.agent.settings = SimpleNamespace(pi_provider="provider", pi_model="model")
        fixture.client.proc = SimpleNamespace(returncode=None)
        fixture.storage.get_active_persona.return_value = None
        await fixture.agent.control_lock.acquire()
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await asyncio.wait_for(bot.cmd_status(fixture.update, fixture.context), 0.1)
        self.assertIn("当前会话", reply.await_args.args[1])
        fixture.client.get_state.assert_not_awaited()
        fixture.agent.control_lock.release()

    async def test_idempotent_edit_retries_but_ambiguous_send_does_not(self):
        message = SimpleNamespace(edit_text=AsyncMock(side_effect=NetworkError("offline")))
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(NetworkError):
                await bot._send(None, "result", edit=True, msg=message)
        self.assertEqual(message.edit_text.await_count, 3)
        sender = AsyncMock(side_effect=NetworkError("response lost"))
        with self.assertRaises(bot.DeliveryUncertainError):
            await bot._send(sender, "result")
        self.assertEqual(sender.await_count, 1)

    async def test_connection_failure_before_send_can_be_retried(self):
        error = NetworkError("connect failed")
        error.__cause__ = httpx.ConnectError("connection refused")
        sender = AsyncMock(side_effect=[error, SimpleNamespace(message_id=1)])
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            await bot._send(sender, "result")
        self.assertEqual(sender.await_count, 2)


if __name__ == "__main__":
    unittest.main()
