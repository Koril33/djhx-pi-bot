import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from storage import Storage


class FailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_update_is_reported_once_on_next_message(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            session_id = storage.create_session(7, "安全会话")
            storage.set_active(session_id)
            storage.enqueue_update(
                42, '{"update_id":42}', user_id=7, session_id=session_id,
            )
            storage.claim_updates(1)
            self.assertEqual(storage.fail_interrupted_updates(), 1)
            update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
            with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
                await bot.notify_failed_updates(update, storage, 7)
                await bot.notify_failed_updates(update, storage, 7)
            reply.assert_awaited_once()
            text = reply.await_args.args[1]
            self.assertIn("安全会话", text)
            self.assertIn("不会自动重发", text)
            self.assertEqual(storage.get_pending_failure_notices(7), [])
            storage._conn.close()


if __name__ == "__main__":
    unittest.main()
