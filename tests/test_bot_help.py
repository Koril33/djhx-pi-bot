import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_home_is_short_and_has_category_buttons(self):
        update = SimpleNamespace()
        context = SimpleNamespace(args=[])
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_help(update, context)
        content = reply.await_args.args[1]
        self.assertLess(len(content), 250)
        self.assertIn("/stop", content)
        buttons = reply.await_args.kwargs["reply_markup"].inline_keyboard
        callbacks = {button.callback_data for row in buttons for button in row}
        self.assertEqual(
            {"help:chat", "help:sessions", "help:memory", "help:other", "cmd:status", "cmd:stop"},
            callbacks,
        )

    async def test_single_environment_does_not_claim_a_switch(self):
        update = SimpleNamespace()
        context = SimpleNamespace(args=["use", "server"])
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_env(update, context)
        content = reply.await_args.args[1]
        self.assertIn("已经在此环境", content)
        self.assertNotIn("已切换", content)

    async def test_help_category_and_callback_route(self):
        query = SimpleNamespace(data="help:memory", answer=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(args=[])
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_callback(update, context)
        query.answer.assert_awaited_once()
        self.assertIn("/memory", reply.await_args.args[1])
        self.assertIn("/summary", reply.await_args.args[1])
        self.assertNotIn(chr(92) + "n", reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
