import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot


class MessageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_messages_wait_for_prior_turn_and_acknowledge_queue(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        processed = []

        async def process(update, _context):
            processed.append(update.update_id)
            if update.update_id == 1:
                entered.set()
                await release.wait()

        def make_update(update_id):
            message = SimpleNamespace(
                photo=None, document=None, media_group_id=None, caption=None,
                reply_text=AsyncMock(),
            )
            return SimpleNamespace(
                update_id=update_id, message=message, callback_query=None,
                effective_user=SimpleNamespace(id=7),
            )

        agent = SimpleNamespace(
            turn_tail=None, control_lock=asyncio.Lock(),
            prompt_pending=False, client=SimpleNamespace(busy=False),
        )
        manager = Mock()
        manager.get.return_value = agent
        storage = Mock()
        storage.count_user_queue.return_value = 1
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        first, second = make_update(1), make_update(2)
        with patch.object(bot, "_process_text", side_effect=process):
            first_task = asyncio.create_task(bot.on_text(first, context))
            await asyncio.wait_for(entered.wait(), 1)
            second_task = asyncio.create_task(bot.on_text(second, context))
            await asyncio.sleep(0.02)
            self.assertEqual(processed, [1])
            self.assertTrue(second.message.reply_text.await_count)
            self.assertIn("已排队", second.message.reply_text.await_args.args[0])
            release.set()
            await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)
        self.assertEqual(processed, [1, 2])

    async def test_slow_queue_receipt_does_not_reorder_later_message(self):
        first_release = asyncio.Event()
        ack_started = asyncio.Event()
        ack_release = asyncio.Event()
        processed = []

        async def process(update, _context):
            processed.append(update.update_id)
            if update.update_id == 1:
                await first_release.wait()

        async def slow_ack(*_args, **_kwargs):
            ack_started.set()
            await ack_release.wait()

        def make_update(update_id):
            return SimpleNamespace(
                update_id=update_id, callback_query=None,
                effective_user=SimpleNamespace(id=7),
                message=SimpleNamespace(
                    photo=None, document=None, media_group_id=None, caption=None,
                    reply_text=AsyncMock(),
                ),
            )

        agent = SimpleNamespace(
            turn_tail=None, control_lock=asyncio.Lock(), prompt_pending=False,
            client=SimpleNamespace(busy=False),
        )
        manager = Mock()
        manager.get.return_value = agent
        storage = Mock()
        storage.count_user_queue.return_value = 2
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        first, second, third = (make_update(n) for n in (1, 2, 3))
        second.message.reply_text.side_effect = slow_ack
        with patch.object(bot, "_process_text", side_effect=process):
            tasks = [asyncio.create_task(bot.on_text(first, context))]
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(bot.on_text(second, context)))
            await asyncio.wait_for(ack_started.wait(), 1)
            tasks.append(asyncio.create_task(bot.on_text(third, context)))
            first_release.set()
            await asyncio.sleep(0.02)
            self.assertEqual(processed, [1])
            ack_release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
        self.assertEqual(processed, [1, 2, 3])

    async def test_steer_waits_for_prompt_start(self):
        client = SimpleNamespace(busy=False, steer=AsyncMock())
        agent = SimpleNamespace(
            client=client, control_lock=asyncio.Lock(), prompt_pending=True,
            active_turn_id=None,
        )
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            message=SimpleNamespace(photo=None, document=None),
        )
        context = SimpleNamespace(
            args=["调整方向"],
            bot_data={"manager": manager, "storage": Mock()},
        )

        async def start_prompt(_seconds):
            client.busy = True

        with patch.object(bot.asyncio, "sleep", side_effect=start_prompt):
            with patch.object(bot, "reply_text", new_callable=AsyncMock):
                await bot.cmd_steer(update, context)
        client.steer.assert_awaited_once_with("调整方向", images=None)

    async def test_stop_cancels_waiting_messages_without_running_task(self):
        storage = Mock()
        storage.cancel_user_queue.return_value = 2
        client = SimpleNamespace(busy=False, abort=AsyncMock())
        agent = SimpleNamespace(prompt_pending=False, client=client)
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_stop(update, context)
        storage.cancel_user_queue.assert_called_once_with(7)
        client.abort.assert_not_awaited()
        self.assertIn("已取消 2 条", reply.await_args.args[1])
        self.assertIn("没有正在运行", reply.await_args.args[1])

    async def test_stop_cancels_waiting_messages_and_aborts_running_task(self):
        storage = Mock()
        storage.cancel_user_queue.return_value = 1
        client = SimpleNamespace(busy=True, abort=AsyncMock())
        agent = SimpleNamespace(prompt_pending=False, client=client)
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(bot_data={"manager": manager, "storage": storage})
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_stop(update, context)
        storage.cancel_user_queue.assert_called_once_with(7)
        client.abort.assert_awaited_once()
        self.assertIn("已取消 1 条", reply.await_args.args[1])
        self.assertIn("已确认中止", reply.await_args.args[1])

    async def test_queue_clear_only_requests_cancellation_of_unstarted_messages(self):
        storage = Mock()
        storage.cancel_user_queue.return_value = 2
        manager = Mock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        context = SimpleNamespace(args=["clear"], bot_data={"storage": storage, "manager": manager})
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_queue(update, context)
        storage.cancel_user_queue.assert_called_once_with(7)
        self.assertIn("2 条", reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
