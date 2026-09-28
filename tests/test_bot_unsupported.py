import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update

import bot


class UnsupportedMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_update_reaches_fallback_and_gets_clear_reply(self):
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={7})
        app = bot.build_application(settings, Mock(), Mock())
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1, "date": 1,
                "chat": {"id": 7, "type": "private", "first_name": "User"},
                "from": {"id": 7, "is_bot": False, "first_name": "User"},
                "voice": {"file_id": "voice-id", "file_unique_id": "voice-unique", "duration": 2},
            },
        }
        update = Update.de_json(payload, app.bot)
        matching = next(h for h in app.handlers[0] if h.check_update(update))
        self.assertIs(matching, app.handlers[0][-1])
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await matching.callback(update, SimpleNamespace())
        self.assertIn("语音或音频", reply.await_args.args[1])

    async def test_known_text_still_reaches_text_handler_first(self):
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={7})
        app = bot.build_application(settings, Mock(), Mock())
        payload = {
            "update_id": 2,
            "message": {
                "message_id": 2, "date": 1,
                "chat": {"id": 7, "type": "private", "first_name": "User"},
                "from": {"id": 7, "is_bot": False, "first_name": "User"},
                "text": "hello",
            },
        }
        update = Update.de_json(payload, app.bot)
        matching = next(h for h in app.handlers[0] if h.check_update(update))
        self.assertIsNot(matching, app.handlers[0][-1])


if __name__ == "__main__":
    unittest.main()
