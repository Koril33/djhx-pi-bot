import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import InvalidToken, NetworkError

import app as bot_app
from config import Settings
from storage import Storage


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "bot.db"))
        self.settings = Settings(_env_file=None, bot_token="123:synthetic", webhook_secret="synthetic",
                                 allowed_user_ids="7", backup_root=str(Path(self.temp.name) / "backups"),
                                 update_worker_retry_seconds=0.001)

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_initialization_and_menu_network_failures_both_retry(self):
        tg = SimpleNamespace(initialize=AsyncMock(side_effect=[NetworkError("offline"), None]),
                             start=AsyncMock(), bot=SimpleNamespace(
                                 set_my_commands=AsyncMock(side_effect=[NetworkError("offline"), None])))
        with patch("app.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await bot_app.start_telegram(tg, self.settings)
        self.assertEqual(tg.initialize.await_count, 2)
        self.assertEqual(tg.start.await_count, 1)
        self.assertEqual(tg.bot.set_my_commands.await_count, 2)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3, 6])

    async def test_invalid_token_fails_fast(self):
        tg = SimpleNamespace(initialize=AsyncMock(side_effect=InvalidToken("bad token")))
        with patch("app.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(InvalidToken):
                await bot_app.start_telegram(tg, self.settings)
        sleep.assert_not_awaited()

    async def test_worker_failure_restarts_consumer(self):
        state = SimpleNamespace(storage=self.storage, tg_app=None, update_wake=asyncio.Event(),
                                update_stopping=asyncio.Event(), backup_paused=asyncio.Event(),
                                update_idle=asyncio.Event(), worker_restarts=0)
        calls = 0
        async def process(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary storage failure")
            state.update_stopping.set()
        with patch.object(bot_app, "settings", self.settings), patch.object(bot_app, "process_queued_updates", side_effect=process):
            await bot_app.supervise_update_worker(SimpleNamespace(state=state), {})
        self.assertEqual(calls, 2)
        self.assertEqual(state.worker_restarts, 1)

    async def test_historical_incident_does_not_change_readiness(self):
        self.storage.enqueue_update(1, '{}', user_id=7)
        self.storage.fail_update(1, "historic failure")
        state = SimpleNamespace(storage=self.storage, update_worker=SimpleNamespace(done=lambda: False),
                                worker_ready=True, manager=SimpleNamespace(_agents={}),
                                telegram_activity={"reachable": True})
        with patch.object(bot_app, "settings", self.settings):
            response = await bot_app.healthz(SimpleNamespace(app=SimpleNamespace(state=state)))
        value = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(value["ok"])
        self.assertFalse(value["degraded"])
        self.assertEqual(value["incidents"]["unacknowledged"], 1)

    async def test_paused_backup_still_allows_stop_control_slot(self):
        self.storage.enqueue_update(1, '{"update_id":1}', priority=0)
        self.storage.enqueue_update(2, '{"update_id":2}', priority=2)
        pause = asyncio.Event()
        pause.set()
        stopping = asyncio.Event()
        processed = []
        async def process(update):
            processed.append(update.update_id)
            stopping.set()
        tg = SimpleNamespace(bot=None, process_update=process)
        with patch.object(bot_app, "settings", self.settings):
            await bot_app.process_queued_updates(self.storage, tg, asyncio.Event(), stopping, {}, pause, asyncio.Event())
        self.assertEqual(processed, [2])
        self.assertEqual(self.storage.next_update()["update_id"], 1)

    async def test_probe_keeps_retrying_and_prunes_expired_runtime_records(self):
        state = SimpleNamespace(
            manager=SimpleNamespace(actions=SimpleNamespace(prune=Mock())),
            storage=SimpleNamespace(prune_updates=Mock()),
            telegram_activity={"reachable": True},
            tg_app=SimpleNamespace(bot=SimpleNamespace(get_me=AsyncMock(side_effect=[RuntimeError("probe failed"), None]))),
        )
        with patch.object(bot_app, "settings", self.settings), patch("app.time.monotonic", return_value=0), \
                patch("app.asyncio.sleep", new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await bot_app.monitor_telegram(SimpleNamespace(state=state))
        self.assertEqual(state.tg_app.bot.get_me.await_count, 2)
        state.manager.actions.prune.assert_called_once()
        state.storage.prune_updates.assert_called_once()
        self.assertTrue(state.telegram_activity["reachable"])
        self.assertIsNone(state.telegram_activity["error"])
