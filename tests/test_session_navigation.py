import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
from storage import Storage


class SessionNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "bot.db"))
        self.user_id = 7
        for number in range(18):
            self.storage.create_session(self.user_id, f"项目 {number:02d}")
        self.foreign_id = self.storage.create_session(8, "他人会话")
        agent = SimpleNamespace(
            client=SimpleNamespace(busy=False),
            prompt_pending=False,
            control_lock=asyncio.Lock(),
        )
        manager = Mock()
        manager.get.return_value = agent
        self.context = SimpleNamespace(
            args=[],
            bot_data={"settings": SimpleNamespace(), "storage": self.storage, "manager": manager},
        )
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=self.user_id))

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_auto_name_hides_sensitive_message_content(self):
        for text in (
            "请保存 password: dummy-value",
            "联系 admin@example.com 查询进度",
            "打开 https://example.com/reset?token=dummy",
            "我的手机是 13800138000",
        ):
            self.assertEqual(bot.automatic_session_name(text), "私密会话")
        self.assertEqual(bot.automatic_session_name("讨论归并排序的复杂度"), "讨论归并排序的复杂度")
        sid = self.storage.create_session(9, "默认会话")
        self.storage.set_active(sid)
        await bot._auto_name_session(self.storage, 9, "密码: dummy-value")
        self.assertEqual(self.storage.get_session(sid)["name"], "私密会话")

    async def test_list_paginates_with_navigation_buttons(self):
        self.context.args = ["list", "2"]
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(self.update, self.context)
        text = reply.await_args.args[1]
        self.assertIn("第 2/3 页", text)
        self.assertEqual(text.count(" · "), 9)
        buttons = reply.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(buttons), 9)
        nav = [button.callback_data for button in buttons[-1]]
        self.assertEqual(nav, ["session_page:1", "session_page:3"])

    async def test_search_callback_and_rename_are_user_scoped(self):
        self.context.args = ["search", "项目 0"]
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(self.update, self.context)
        self.assertIn("搜索结果", reply.await_args.args[1])
        self.assertIn("第 1/2 页", reply.await_args.args[1])
        next_button = reply.await_args.kwargs["reply_markup"].inline_keyboard[-1][0]
        self.assertEqual(next_button.callback_data, "session_find:2:项目 0")
        query = SimpleNamespace(data=next_button.callback_data, answer=AsyncMock())
        self.update.callback_query = query
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_callback(self.update, self.context)
        self.assertIn("第 2/2 页", reply.await_args.args[1])
        query.answer.assert_awaited_once()

        self.context.args = ["rename", self.foreign_id, "非法修改"]
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(self.update, self.context)
        self.assertIn("找不到属于你的会话", reply.await_args.args[1])
        self.assertEqual(self.storage.get_session(self.foreign_id)["name"], "他人会话")

        own_id = self.storage.list_sessions(self.user_id)[0]["id"]
        self.context.args = ["rename", own_id, "我的新名称"]
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_session(self.update, self.context)
        self.assertIn("已将会话", reply.await_args.args[1])
        self.assertEqual(self.storage.get_session(own_id)["name"], "我的新名称")


if __name__ == "__main__":
    unittest.main()
