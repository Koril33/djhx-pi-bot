import tempfile
import unittest
from pathlib import Path

from storage import Storage


class MemoryOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.sid = self.storage.create_session(7, "测试")

    def tearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    def test_full_capacity_refreshes_exact_repeat_but_never_evicts(self):
        first = self.storage.add_memory(7, "我喜欢简洁的回答", limit=2)
        self.storage.pin_memory(7, first, True)
        self.storage.add_memory(7, "其他稳定偏好", limit=2)
        self.storage._exec("UPDATE memory_items SET updated_at = 1 WHERE id = ?", (first,))
        result = self.storage.apply_summary(7, self.sid, 0, 1, "摘要",
                                           ["我喜欢简洁的回答", "新的偏好"],
                                           memory_limit=2, turn_retention=20)
        self.assertEqual(result, {"added": 0, "refreshed": 1, "skipped_full": 1})
        row = self.storage.get_memory(7, first)
        self.assertTrue(row["pinned"])
        self.assertEqual(row["source"], "manual")
        self.assertGreater(row["updated_at"], 1)
        state = self.storage.memory_capacity_status(7, 2)
        self.assertTrue(state["full"])
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["last_auto"]["skipped_full"], 1)

    def test_candidates_are_scoped_and_only_confirmed_merge_reduces_count(self):
        first = self.storage.add_memory(7, "我喜欢简洁的回答", limit=4)
        second = self.storage.add_memory(7, "我偏好简短的回复", limit=4)
        self.storage.add_memory(8, "我偏好简短的回复", limit=4)
        self.storage.pin_memory(7, first, True)
        candidates = self.storage.memory_organization_candidates(7)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["kind"], "near_duplicate")
        preview = self.storage.preview_memory_merge(7, [first, second], "偏好简洁回答")
        self.assertEqual(len(self.storage.list_memories(7)), 2)
        kept = self.storage.merge_memories(7, preview["ids"], preview["text"], preview["version"])
        self.assertEqual(kept, first)
        self.assertEqual(len(self.storage.list_memories(7)), 1)
        self.assertTrue(self.storage.get_memory(7, kept)["pinned"])
        self.assertEqual(len(self.storage.list_memories(8)), 1)

    def test_changed_pin_or_text_invalidates_preview_and_other_users_cannot_merge(self):
        first = self.storage.add_memory(7, "第一条记忆", limit=4)
        second = self.storage.add_memory(7, "第二条记忆", limit=4)
        preview = self.storage.preview_memory_merge(7, [first, second])
        self.storage.pin_memory(7, first, True)
        with self.assertRaisesRegex(ValueError, "过期"):
            self.storage.merge_memories(7, preview["ids"], preview["text"], preview["version"])
        with self.assertRaises(ValueError):
            self.storage.preview_memory_merge(8, [first, second])
        self.assertEqual(len(self.storage.list_memories(7)), 2)

    def test_conflict_candidates_are_not_automatically_changed(self):
        self.storage.add_memory(7, "我喜欢喝咖啡", limit=4)
        self.storage.add_memory(7, "我不喜欢喝咖啡", limit=4)
        candidates = self.storage.memory_organization_candidates(7)
        self.assertEqual(candidates[0]["kind"], "possible_conflict")
        self.assertEqual(len(self.storage.list_memories(7)), 2)

    def test_duplicate_outside_selection_does_not_delete_selected_memories(self):
        ids = [self.storage.add_memory(7, text, limit=4) for text in ("第一条", "第二条", "目标内容")]
        preview = self.storage.preview_memory_merge(7, ids[:2], "目标内容")
        with self.assertRaisesRegex(ValueError, "另一条"):
            self.storage.merge_memories(7, ids[:2], preview["text"], preview["version"])
        self.assertEqual(len(self.storage.list_memories(7)), 3)


if __name__ == "__main__":
    unittest.main()
