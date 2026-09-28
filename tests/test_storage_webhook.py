import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram import Bot, Update
from telegram.error import NetworkError

import app as bot_app
from config import Settings
from storage import Storage, UpdateQueueFull


class StorageSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "bot.db"))

    def tearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    def test_set_active_changes_only_own_user_and_requires_existing_session(self):
        first = self.storage.create_session(1, "first")
        second = self.storage.create_session(1, "second")
        other = self.storage.create_session(2, "other")
        self.storage.set_active(first)
        self.storage.set_active(other)
        self.storage.set_active(second)

        self.assertEqual(self.storage.get_active_session(1)["id"], second)
        self.assertEqual(self.storage.get_active_session(2)["id"], other)
        with self.assertRaises(ValueError):
            self.storage.set_active("missing")
        self.assertEqual(self.storage.get_active_session(1)["id"], second)

    def test_inbox_is_durable_ordered_deduplicated_and_bounded(self):
        self.assertTrue(self.storage.enqueue_update(20, '{"update_id":20}', capacity=2))
        self.assertTrue(self.storage.enqueue_update(10, '{"update_id":10}', capacity=2))
        self.assertFalse(self.storage.enqueue_update(10, '{"update_id":10}', capacity=2))
        with self.assertRaises(UpdateQueueFull):
            self.storage.enqueue_update(30, '{"update_id":30}', capacity=2)
        self.assertEqual(self.storage.next_update()["update_id"], 10)

        self.storage.complete_update(10)
        self.assertEqual(self.storage.next_update()["update_id"], 20)
        self.assertEqual(self.storage.update_stats()["pending"], 1)
        self.assertFalse(self.storage.enqueue_update(10, '{"update_id":10}'))

        # 重新打开同一文件模拟服务重启，待处理记录仍在。
        reopened = Storage(str(Path(self.temp.name) / "bot.db"))
        try:
            self.assertEqual(reopened.next_update()["update_id"], 20)
        finally:
            reopened._conn.close()

    def test_create_and_activate_rolls_back_on_sql_error(self):
        first = self.storage.create_session(1, "existing")
        self.storage.set_active(first)
        self.storage._conn.execute(
            "CREATE TRIGGER reject_bad_session BEFORE UPDATE OF status ON sessions "
            "WHEN NEW.name = 'bad' AND NEW.status = 'active' "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
        with self.assertRaises(Exception):
            self.storage.create_session_with_file_and_activate(1, "bad", "new.jsonl")
        self.assertEqual(self.storage.get_active_session(1)["id"], first)
        self.assertEqual(len(self.storage.list_sessions(1)), 1)

    def test_terminal_failure_is_visible(self):
        self.storage.enqueue_update(1, '{"update_id":1}')
        self.storage.fail_update(1, "network")
        self.assertIsNone(self.storage.next_update())
        self.assertEqual(self.storage.update_stats()["failed"], 1)
        row = self.storage._query("SELECT payload FROM telegram_updates WHERE update_id = 1")[0]
        self.assertIsNone(row["payload"])

    def test_interrupted_update_is_not_replayed(self):
        self.storage.enqueue_update(1, '{"update_id":1}')
        self.storage.claim_updates(1)
        self.assertEqual(self.storage.fail_interrupted_updates(), 1)
        self.assertIsNone(self.storage.next_update())
        self.assertEqual(self.storage.update_stats()["failed"], 1)

    def test_waiting_messages_survive_restart_and_can_be_cancelled(self):
        for update_id, user_id in ((1, 7), (2, 7), (3, 8), (4, 7)):
            self.storage.enqueue_update(
                update_id, f'{{"update_id":{update_id}}}',
                user_id=user_id, queueable=True,
            )
        self.storage.claim_updates(3)
        self.assertTrue(self.storage.begin_update(1))  # 已开始，不能取消或重放
        self.assertEqual(self.storage.count_user_queue(7), 2)
        self.assertEqual(self.storage.cancel_user_queue(7), 2)  # 等待中的 2 和待分发的 4
        self.assertFalse(self.storage.begin_update(2))
        self.assertEqual(self.storage.fail_interrupted_updates(), 1)
        self.assertEqual(self.storage.next_update()["update_id"], 3)  # 仍未开始，安全重放
        self.assertEqual(self.storage.update_stats()["failed"], 1)
        self.assertEqual(self.storage.count_user_queue(7), 0)

    def test_album_updates_start_atomically(self):
        for update_id in (10, 11):
            self.storage.enqueue_update(
                update_id, f'{{"update_id":{update_id}}}', user_id=7, queueable=True,
            )
        self.storage.claim_updates(2)
        self.assertTrue(self.storage.begin_album_updates([10, 11]))
        rows = self.storage._query(
            "SELECT safe_to_retry FROM telegram_updates WHERE update_id IN (10, 11)"
        )
        self.assertEqual([row["safe_to_retry"] for row in rows], [0, 0])
        self.storage.complete_update(10)
        self.assertFalse(self.storage.begin_album_updates([10, 11]))

    def test_failed_update_keeps_user_session_and_pending_safe_notice(self):
        session_id = self.storage.create_session(7, "项目会话")
        self.storage.set_active(session_id)
        self.storage.enqueue_update(
            99, '{"update_id":99}', user_id=7, session_id=session_id,
        )
        self.storage.claim_updates(1)
        self.storage.fail_update(99, "RuntimeError: internal-secret")
        notice = self.storage.get_failure_notice(99)
        self.assertEqual(notice["user_id"], 7)
        self.assertEqual(notice["session_id"], session_id)
        self.assertEqual(notice["session_name"], "项目会话")
        self.assertEqual(len(self.storage.get_pending_failure_notices(7)), 1)
        self.storage.mark_failure_notice_sent(99)
        self.assertEqual(self.storage.get_pending_failure_notices(7), [])

    def test_acknowledge_keeps_dedup_record_without_health_failure(self):
        self.storage.enqueue_update(1, '{"update_id":1}')
        self.storage.claim_updates(1)
        self.storage.fail_interrupted_updates()
        self.assertTrue(self.storage.acknowledge_failed_update(1))
        self.assertFalse(self.storage.acknowledge_failed_update(1))
        self.assertEqual(self.storage.update_stats()["failed"], 0)
        self.assertEqual(self.storage.get_recent_failed_updates(), [])
        self.assertFalse(self.storage.enqueue_update(1, '{"update_id":1}'))
        row = self.storage._query("SELECT state, payload, acknowledged_at FROM telegram_updates WHERE update_id = 1")[0]
        self.assertEqual(row["state"], "acknowledged")
        self.assertIsNone(row["payload"])
        self.assertIsNotNone(row["acknowledged_at"])


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "bot.db"))
        self.settings_patch = patch.object(
            bot_app,
            "settings",
            Settings(
                bot_token="123:abc",
                allowed_user_ids="1",
                webhook_secret="telegram-secret",
                deploy_secret="deploy-secret",
                deploy_repo_full_name="koril/djhx-pi-bot",
                webhook_queue_capacity=1,
            ),
        )
        self.settings_patch.start()
        self.processed = []

        class TelegramApp:
            bot = Bot("123:abc")
            error_handler = None

            def add_error_handler(inner_self, callback, block=True):
                self.assertTrue(block)
                inner_self.error_handler = callback

            async def process_update(inner_self, update):
                self.processed.append(update.update_id)

        self.tg_app = TelegramApp()
        self.update_errors = {}
        bot_app.register_update_error_handler(self.tg_app, self.update_errors)
        bot_app.app.state.tg_app = self.tg_app
        bot_app.app.state.storage = self.storage
        bot_app.app.state.update_wake = asyncio.Event()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bot_app.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.settings_patch.stop()
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_webhook_ack_after_enqueue_and_duplicate(self):
        payload = {"update_id": 42}
        result = await self.client.post("/webhook/telegram-secret", json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(bot_app.app.state.update_wake.is_set())
        self.assertEqual(self.storage.update_stats()["pending"], 1)
        duplicate = await self.client.post("/webhook/telegram-secret", json=payload)
        self.assertEqual(duplicate.status_code, 200)
        full = await self.client.post("/webhook/telegram-secret", json={"update_id": 43})
        self.assertEqual(full.status_code, 503)

        stopping = asyncio.Event()
        worker = asyncio.create_task(
            bot_app.process_queued_updates(
                self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors
            )
        )
        for _ in range(20):
            if self.storage.update_stats()["pending"] == 0:
                break
            await asyncio.sleep(0.05)
        stopping.set()
        await asyncio.wait_for(worker, timeout=2)
        self.assertEqual(self.processed, [42])
        self.assertEqual(self.storage.update_stats()["pending"], 0)

    async def test_shutdown_leaves_undispatched_updates_pending(self):
        self.storage.enqueue_update(88, '{"update_id":88}')
        stopping = asyncio.Event()
        stopping.set()
        await bot_app.process_queued_updates(
            self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors
        )
        self.assertEqual(self.storage.next_update()["update_id"], 88)
        self.assertEqual(self.storage.update_stats()["failed"], 0)

    async def test_deploy_accepts_only_authenticated_main_push(self):
        payload = {"ref": "refs/heads/main", "repository": {"full_name": "koril/djhx-pi-bot"}}
        body = json.dumps(payload).encode()
        headers = {"X-Gitea-Event": "push"}
        with patch.object(bot_app.subprocess, "Popen") as spawn:
            wrong_branch = await self.client.post(
                "/deploy-webhook/deploy-secret",
                content=json.dumps({**payload, "ref": "refs/heads/dev"}),
                headers=headers,
            )
            self.assertEqual(wrong_branch.status_code, 204)
            invalid_signature = await self.client.post(
                "/deploy-webhook/deploy-secret",
                content=body,
                headers={**headers, "X-Gitea-Signature": "bad"},
            )
            self.assertEqual(invalid_signature.status_code, 403)
            signature = hmac.new(b"deploy-secret", body, hashlib.sha256).hexdigest()
            valid = await self.client.post(
                "/deploy-webhook/deploy-secret",
                content=body,
                headers={**headers, "X-Gitea-Signature": signature},
            )
            self.assertEqual(valid.status_code, 202)
            spawn.assert_called_once()

    async def test_stop_dispatches_while_prompt_is_running(self):
        prompt_started = asyncio.Event()
        release_prompt = asyncio.Event()
        stop_processed = asyncio.Event()
        stop_saw_prompt = []

        async def process(update):
            if update.update_id == 1:
                prompt_started.set()
                await release_prompt.wait()
            else:
                stop_saw_prompt.append(prompt_started.is_set())
                stop_processed.set()

        self.tg_app.process_update = process
        self.storage.enqueue_update(1, '{"update_id":1}', priority=0)
        self.storage.enqueue_update(2, '{"update_id":2}', priority=2)
        stopping = asyncio.Event()
        task = asyncio.create_task(
            bot_app.process_queued_updates(
                self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors
            )
        )
        try:
            await asyncio.wait_for(prompt_started.wait(), timeout=2)
            await asyncio.wait_for(stop_processed.wait(), timeout=2)
        finally:
            stopping.set()
            release_prompt.set()
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(self.storage.update_stats()["pending"], 0)
        self.assertEqual(stop_saw_prompt, [True])

    async def test_ptb_swallowed_handler_error_marks_only_its_update_failed(self):
        async def process(update):
            if update.update_id == 1:
                try:
                    raise NetworkError("Telegram send failed")
                except NetworkError as exc:
                    await self.tg_app.error_handler(update, SimpleNamespace(error=exc))
            else:
                self.processed.append(update.update_id)

        self.tg_app.process_update = process
        self.storage.enqueue_update(1, '{"update_id":1}')
        self.storage.enqueue_update(2, '{"update_id":2}')
        stopping = asyncio.Event()
        worker = asyncio.create_task(
            bot_app.process_queued_updates(
                self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors
            )
        )
        try:
            for _ in range(20):
                if self.storage.update_stats()["pending"] == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            stopping.set()
            await asyncio.wait_for(worker, timeout=2)
        rows = self.storage._query(
            "SELECT update_id, state, payload, last_error FROM telegram_updates ORDER BY update_id"
        )
        self.assertEqual([(row["update_id"], row["state"]) for row in rows], [(1, "failed"), (2, "done")])
        self.assertIsNone(rows[0]["payload"])
        self.assertIn("NetworkError", rows[0]["last_error"])
        self.assertEqual(self.processed, [2])
        self.assertEqual(self.update_errors, {})

    async def test_failed_update_notifies_authorized_user_without_internal_error(self):
        session_id = self.storage.create_session(1, "工作会话")
        self.storage.set_active(session_id)
        self.storage.enqueue_update(
            91, '{"update_id":91}', user_id=1, session_id=session_id,
        )
        self.tg_app.bot = SimpleNamespace(send_message=AsyncMock())

        async def fail(_update):
            raise RuntimeError("internal-secret")

        self.tg_app.process_update = fail
        stopping = asyncio.Event()
        worker = asyncio.create_task(
            bot_app.process_queued_updates(
                self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors,
            )
        )
        try:
            for _ in range(30):
                if self.storage.update_stats()["pending"] == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            stopping.set()
            await asyncio.wait_for(worker, timeout=2)
        call = self.tg_app.bot.send_message.await_args
        self.assertEqual(call.kwargs["chat_id"], 1)
        self.assertIn("工作会话", call.kwargs["text"])
        self.assertNotIn("internal-secret", call.kwargs["text"])
        self.assertEqual(self.storage.get_pending_failure_notices(1), [])

    async def test_failed_update_keeps_notice_when_telegram_send_fails(self):
        self.storage.enqueue_update(92, '{"update_id":92}', user_id=1)
        self.tg_app.bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("network")))

        async def fail(_update):
            raise RuntimeError("internal-secret")

        self.tg_app.process_update = fail
        stopping = asyncio.Event()
        worker = asyncio.create_task(
            bot_app.process_queued_updates(
                self.storage, self.tg_app, asyncio.Event(), stopping, self.update_errors,
            )
        )
        try:
            for _ in range(30):
                if self.storage.update_stats()["pending"] == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            stopping.set()
            await asyncio.wait_for(worker, timeout=2)
        self.assertEqual(len(self.storage.get_pending_failure_notices(1)), 1)

    async def test_plain_text_keeps_order_and_explicit_steer_has_control_capacity(self):
        data = {
            "update_id": 7,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "User"},
                "text": "继续按这个方向做",
            },
        }
        update = Update.de_json(data, self.tg_app.bot)
        manager = SimpleNamespace(
            _agents={1: SimpleNamespace(client=SimpleNamespace(busy=True), prompt_pending=False)}
        )
        self.assertEqual(bot_app._update_priority(update, manager), 0)
        data["message"]["text"] = "/steer 调整当前任务"
        self.assertEqual(bot_app._update_priority(Update.de_json(data, self.tg_app.bot), manager), 1)
        data["message"]["text"] = "/stop"
        self.assertEqual(bot_app._update_priority(Update.de_json(data, self.tg_app.bot), manager), 2)

    async def test_plain_photo_keeps_order_and_caption_steer_has_control_capacity(self):
        data = {
            "update_id": 8,
            "message": {
                "message_id": 2,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "User"},
                "photo": [{"file_id": "photo-id", "file_unique_id": "unique", "width": 32, "height": 32}],
                "caption": "看看这张图片",
            },
        }
        update = Update.de_json(data, self.tg_app.bot)
        manager = SimpleNamespace(
            _agents={1: SimpleNamespace(client=SimpleNamespace(busy=True), prompt_pending=False)}
        )
        self.assertEqual(bot_app._update_priority(update, manager), 0)
        data["message"]["caption"] = "/steer 看看这张图片"
        self.assertEqual(bot_app._update_priority(Update.de_json(data, self.tg_app.bot), manager), 1)


if __name__ == "__main__":
    unittest.main()
