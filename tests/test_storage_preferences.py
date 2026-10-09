import tempfile
import unittest
from pathlib import Path

from storage import Storage


class PreferenceAndMailStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "bot.db")
        self.storage = Storage(self.path)
        self.first = self.storage.create_session(7, "工作")
        self.second = self.storage.create_session(7, "闲聊")

    def tearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    def test_session_choices_override_inherit_and_survive_reopen(self):
        self.storage.set_user_model(7, "deepseek", "flash")
        persona = self.storage.create_persona({"name": "工作伙伴", "identity": "工作助手"})
        self.storage.set_session_model(7, self.first, "xiaomi", "mimo")
        self.storage.set_session_persona(7, self.first, persona)
        self.assertEqual(self.storage.get_effective_model_selection(7, self.first)["model_id"], "mimo")
        self.assertEqual(self.storage.get_effective_model_selection(7, self.second)["model_id"], "flash")
        self.assertEqual(self.storage.get_effective_persona(7, self.first)["id"], persona)
        self.storage.set_session_persona(7, self.second, "none")
        self.assertIsNone(self.storage.get_effective_persona(7, self.second))
        self.storage._conn.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage.get_session_preferences(7, self.first)["persona_id"], persona)
        self.storage.clear_session_preferences(7, self.first)
        self.assertEqual(self.storage.get_effective_model_selection(7, self.first)["model_id"], "flash")
        self.assertEqual(self.storage.get_effective_persona(7, self.first)["id"], "default")

    def test_disabled_persona_returns_to_inherit_without_touching_model(self):
        persona = self.storage.create_persona({"name": "测试角色", "identity": "测试身份"})
        self.storage.set_session_persona(7, self.first, persona)
        self.storage.set_session_model(7, self.first, "xiaomi", "mimo")
        self.storage.set_persona_enabled(persona, False)
        prefs = self.storage.get_session_preferences(7, self.first)
        self.assertIsNone(prefs["persona_id"])
        self.assertEqual(prefs["model_id"], "mimo")
        for setter, args in ((self.storage.set_session_model, ("deepseek", "flash")),
                             (self.storage.set_session_persona, ("none",)),
                             (self.storage.clear_session_preferences, ())):
            with self.assertRaises(ValueError):
                setter(8, self.first, *args)

    def test_empty_session_marker_survives_restart_and_is_cleared_before_prompt(self):
        self.storage.set_pi_session_file(self.first, "/sessions/new.jsonl", known_empty=True)
        self.storage._conn.close()
        self.storage = Storage(self.path)
        self.assertTrue(self.storage.get_session(self.first)["pi_session_empty"])
        self.storage.mark_session_nonempty(self.first)
        self.assertFalse(self.storage.get_session(self.first)["pi_session_empty"])
        self.storage.set_pi_session_file(self.first, "/sessions/replaced.jsonl")
        self.assertFalse(self.storage.get_session(self.first)["pi_session_empty"])

    def test_mail_history_and_recovery_distinguish_uncertain_from_rejected(self):
        first = self.storage.create_mail_draft(7, "test@example.com", "测试", "正文")
        second = self.storage.create_mail_draft(7, "test@example.com", "测试2", "正文")
        self.storage.claim_mail_draft(7, first)
        self.storage.claim_mail_draft(7, second)
        self.storage.finish_mail_draft(7, second, sent=False)
        self.storage._conn.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage.get_mail_draft(7, first)["state"], "sending")
        self.assertEqual(self.storage.recover_interrupted_mail_drafts(), 1)
        self.assertEqual(self.storage.recover_interrupted_mail_drafts(), 0)
        self.assertEqual(self.storage.get_mail_draft(7, first)["state"], "uncertain")
        self.assertEqual(len(self.storage.list_mail_drafts(7, states=["failed"])), 1)
        self.assertEqual(len(self.storage.list_mail_drafts(7, states=["uncertain"])), 1)
        self.assertEqual(self.storage.list_mail_drafts(8), [])
        self.assertFalse(self.storage.claim_mail_draft(7, first))


if __name__ == "__main__":
    unittest.main()
