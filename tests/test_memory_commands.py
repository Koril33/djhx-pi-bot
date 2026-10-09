import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from memory_commands import handle_memory_management, handle_memory_merge_callback
from storage import Storage


class MemoryManagementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.settings = SimpleNamespace(memory_active_limit=2, memory_item_max_chars=240)
        self.context = SimpleNamespace(args=[], user_data={}, bot_data={
            "storage": self.storage, "settings": self.settings,
        })
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=7), callback_query=None)
        self.reply = AsyncMock()
        self.first = self.storage.add_memory(7, "我喜欢简洁的回答", limit=2)
        self.second = self.storage.add_memory(7, "我偏好简短的回复", limit=2)

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    async def test_manage_full_capacity_is_explicit_and_does_not_modify_memory(self):
        self.context.args = ["manage"]
        await handle_memory_management(self.update, self.context, self.reply)
        call = self.reply.await_args
        self.assertIn("2/2", call.args[1])
        self.assertIn("容量已满", call.args[1])
        self.assertIn("不会自动删除", call.args[1])
        self.assertTrue(call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("mm_preview:"))
        self.assertEqual(len(self.storage.list_memories(7)), 2)

    async def test_custom_text_preview_requires_confirmation_and_retains_pinning(self):
        self.storage.pin_memory(7, self.first, True)
        self.context.args = ["merge", self.first, self.second, "已核对，偏好简洁回答"]
        await handle_memory_management(self.update, self.context, self.reply)
        self.assertEqual(len(self.storage.list_memories(7)), 2)
        callback = self.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.update.callback_query = SimpleNamespace(data=callback)
        refresh = AsyncMock()
        await handle_memory_merge_callback(self.update, self.context, self.reply, refresh)
        rows = self.storage.list_memories(7)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pinned"])
        self.assertEqual(rows[0]["text"], "已核对，偏好简洁回答")
        refresh.assert_awaited_once_with(7)
        await handle_memory_merge_callback(self.update, self.context, self.reply, refresh)
        self.assertIn("已过期", self.reply.await_args.args[1])
        self.assertEqual(len(self.storage.list_memories(7)), 1)

    async def test_edited_version_and_other_user_cannot_confirm(self):
        self.context.args = ["merge", self.first, self.second]
        await handle_memory_management(self.update, self.context, self.reply)
        callback = self.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.storage.edit_memory(7, self.first, "已经改变的偏好")
        self.update.callback_query = SimpleNamespace(data=callback)
        await handle_memory_merge_callback(self.update, self.context, self.reply)
        self.assertIn("已过期", self.reply.await_args.args[1])
        self.assertEqual(len(self.storage.list_memories(7)), 2)
        self.update.effective_user.id = 8
        self.context.args = ["merge", self.first, self.second]
        await handle_memory_management(self.update, self.context, self.reply)
        self.assertIn("不属于", self.reply.await_args.args[1])
        self.assertEqual(len(self.storage.list_memories(7)), 2)

    async def test_cancelled_preview_never_merges(self):
        self.context.args = ["merge", self.first, self.second]
        await handle_memory_management(self.update, self.context, self.reply)
        callback = self.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
        self.update.callback_query = SimpleNamespace(data=callback)
        await handle_memory_merge_callback(self.update, self.context, self.reply)
        self.assertIn("已取消", self.reply.await_args.args[1])
        self.assertEqual(len(self.storage.list_memories(7)), 2)


if __name__ == "__main__":
    unittest.main()
