import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from telegram.error import BadRequest, NetworkError

import bot


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_is_retried_then_raised(self) -> None:
        sender = AsyncMock(side_effect=NetworkError("connection lost"))
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(NetworkError):
                await bot._send(sender, "hello")
        self.assertEqual(sender.await_count, 3)

    async def test_bad_html_falls_back_to_plain_text(self) -> None:
        sender = AsyncMock(side_effect=[BadRequest("can't parse entities"), "message"])
        result = await bot._send(sender, "**hello**")
        self.assertEqual(result, "message")
        self.assertEqual(sender.await_args_list[1].args[0], "hello")

    async def test_plain_text_failure_is_not_reported_as_success(self) -> None:
        message = SimpleNamespace(edit_text=AsyncMock(side_effect=[
            BadRequest("can't parse entities"), BadRequest("message to edit not found")
        ]))
        with self.assertRaises(BadRequest):
            await bot._send(None, "**hello**", edit=True, msg=message)


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_handler_rejects_unauthorized_user_before_agent_lookup(self) -> None:
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={1})
        storage = Mock()
        manager = Mock()
        app = bot.build_application(settings, storage, manager)
        message = SimpleNamespace(reply_text=AsyncMock(), chat=SimpleNamespace(type="private"))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=2),
            effective_chat=SimpleNamespace(type="private"),
            message=message,
            callback_query=None,
        )
        context = SimpleNamespace()
        await app.handlers[0][-1].callback(update, context)
        message.reply_text.assert_awaited_once()
        manager.get.assert_not_called()

    def test_group_chat_is_not_authorized(self) -> None:
        settings = SimpleNamespace(allowed_ids={1})
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(type="supergroup"),
        )
        self.assertFalse(bot.authorized(update, settings))


class SessionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_using_active_session_does_not_repeat_missing_jsonl_recovery(self) -> None:
        storage = Mock()
        storage.get_session.return_value = {
            "id": "current", "user_id": 1, "pi_session_file": "/missing.jsonl",
        }
        storage.get_active_session.return_value = {"id": "current", "pi_session_file": "/restored.jsonl"}
        client = SimpleNamespace(
            busy=False, reset_session=AsyncMock(), switch_session=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, prompt_pending=False, memory=Mock(),
            ensure_client=AsyncMock(), recovery_notice="已从 SQLite 恢复",
        )
        notice = await bot.switch_user_session(agent, storage, 1, "current")
        self.assertEqual(notice, "已从 SQLite 恢复")
        self.assertIsNone(agent.recovery_notice)
        client.reset_session.assert_not_awaited()
        client.switch_session.assert_not_awaited()
        storage.set_active.assert_not_called()

    async def test_read_only_commands_wait_for_session_change_before_syncing_client(self) -> None:
        for command, args in ((bot.cmd_model, ["list"]), (bot.cmd_status, [])):
            with self.subTest(command=command.__name__):
                lock = asyncio.Lock()
                await lock.acquire()
                client = SimpleNamespace(
                    busy=False,
                    proc=SimpleNamespace(returncode=None),
                    get_available_models=AsyncMock(return_value=[]),
                    get_state=AsyncMock(return_value={"sessionFile": "/current.jsonl"}),
                )
                agent = SimpleNamespace(
                    client=client, control_lock=lock, ensure_client=AsyncMock(),
                    settings=SimpleNamespace(pi_model="deepseek-flash"),
                )
                manager = Mock()
                manager.get.return_value = agent
                storage = Mock()
                storage.get_active_session.return_value = {"name": "current"}
                context = SimpleNamespace(args=args, bot_data={"manager": manager, "storage": storage})
                update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
                with patch.object(bot, "reply_text", new_callable=AsyncMock):
                    task = asyncio.create_task(command(update, context))
                    await asyncio.sleep(0)
                    agent.ensure_client.assert_not_awaited()
                    lock.release()
                    await task
                agent.ensure_client.assert_awaited_once()

    async def test_ensure_client_restarts_with_active_session_file(self) -> None:
        storage = Mock()
        storage.db_path = "data/test.db"
        storage.get_active_session.return_value = {
            "id": "current", "pi_session_file": "/current.jsonl"
        }
        settings = SimpleNamespace(
            pi_cmd="pi", pi_session_dir="/sessions", pi_provider=None, pi_model=None,
        )
        agent = bot.AgentSession(1, settings, storage)
        agent.client = SimpleNamespace(ensure_running=AsyncMock(), get_state=AsyncMock())
        await agent.ensure_client()
        agent.client.ensure_running.assert_awaited_once_with("/current.jsonl")
        agent.client.get_state.assert_not_awaited()

    async def test_failed_new_session_keeps_previous_active_record(self) -> None:
        storage = Mock()
        storage.get_active_session.return_value = {"pi_session_file": "/old.jsonl"}
        client = SimpleNamespace(
            busy=False,
            new_session=AsyncMock(),
            get_state=AsyncMock(return_value={}),
            switch_session=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(
            args=["new", "next"],
            bot_data={"settings": Mock(), "storage": storage, "manager": manager},
        )
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(update, context)
        storage.create_session.assert_not_called()
        storage.set_active.assert_not_called()
        client.switch_session.assert_awaited_once_with("/old.jsonl")
        self.assertIn("新建会话失败", reply.await_args.args[1])

    async def test_new_session_error_before_return_restores_previous_pi_session(self) -> None:
        storage = Mock()
        storage.get_active_session.return_value = {"id": "old", "pi_session_file": "/old.jsonl"}
        client = SimpleNamespace(
            busy=False,
            new_session=AsyncMock(side_effect=RuntimeError("post-create failure")),
            get_state=AsyncMock(),
            switch_session=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        context = SimpleNamespace(
            args=["new", "next"],
            bot_data={"settings": Mock(), "storage": storage, "manager": manager},
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            await bot.cmd_session(update, context)
        client.switch_session.assert_awaited_once_with("/old.jsonl")
        storage.create_session_with_file_and_activate.assert_not_called()

    async def test_new_session_activates_only_after_pi_returns_file(self) -> None:
        storage = Mock()
        storage.get_active_session.return_value = {"id": "old", "pi_session_file": "/old.jsonl"}
        storage.create_session_with_file_and_activate.return_value = "new"
        client = SimpleNamespace(
            busy=False,
            new_session=AsyncMock(),
            get_state=AsyncMock(return_value={"sessionFile": "/new.jsonl"}),
            switch_session=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        context = SimpleNamespace(
            args=["new", "next"],
            bot_data={"settings": Mock(), "storage": storage, "manager": manager},
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(update, context)
        storage.create_session_with_file_and_activate.assert_called_once_with(1, "next", "/new.jsonl")
        client.switch_session.assert_not_awaited()
        self.assertIn("已新建会话", reply.await_args.args[1])

    async def test_session_use_restores_previous_pi_session_if_storage_update_fails(self) -> None:
        storage = Mock()
        storage.get_session.return_value = {
            "id": "new", "user_id": 1, "name": "next", "pi_session_file": "/new.jsonl",
        }
        storage.get_active_session.return_value = {"pi_session_file": "/old.jsonl"}
        storage.set_active.side_effect = RuntimeError("database unavailable")
        client = SimpleNamespace(busy=False, switch_session=AsyncMock(), stop=AsyncMock())
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        context = SimpleNamespace(
            args=["use", "new"],
            bot_data={"settings": Mock(), "storage": storage, "manager": manager},
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(update, context)
        self.assertEqual(client.switch_session.await_args_list, [call("/new.jsonl"), call("/old.jsonl")])
        client.stop.assert_not_awaited()
        self.assertIn("切换会话失败", reply.await_args.args[1])

    async def test_session_use_restores_previous_pi_session_if_switch_result_is_lost(self) -> None:
        storage = Mock()
        storage.get_session.return_value = {
            "id": "new", "user_id": 1, "name": "next", "pi_session_file": "/new.jsonl",
        }
        storage.get_active_session.return_value = {"pi_session_file": "/old.jsonl"}
        client = SimpleNamespace(
            busy=False,
            switch_session=AsyncMock(side_effect=[RuntimeError("response lost"), {}]),
            stop=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        context = SimpleNamespace(
            args=["use", "new"],
            bot_data={"settings": Mock(), "storage": storage, "manager": manager},
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            await bot.cmd_session(update, context)
        self.assertEqual(client.switch_session.await_args_list, [call("/new.jsonl"), call("/old.jsonl")])
        storage.set_active.assert_not_called()


if __name__ == "__main__":
    unittest.main()
