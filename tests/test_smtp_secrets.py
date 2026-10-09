import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values, set_key

from storage import Storage


class SmtpSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "bot.db"
        self.secret = self.root / "test.env"
        self.store = Storage(str(self.db), secret_file=str(self.secret))
        self.config = {"host": "smtp.example.com", "port": 465, "username": "bot@example.com",
                       "password": "test-smtp-secret", "from_address": "bot@example.com"}

    def tearDown(self):
        self.store._conn.close()
        self.temp.cleanup()

    def test_password_stays_in_private_env_preserving_other_keys_and_comments(self):
        previous = '# retain this comment\nBOT_TOKEN="synthetic-token"\nMULTILINE="first\nsecond"\n'
        self.secret.write_text(previous, encoding="utf-8")
        password = "a'quoted\\secret${UNCHANGED}"
        self.store.save_smtp_settings(**{**self.config, "password": password})
        self.assertTrue(self.secret.read_text(encoding="utf-8").startswith(previous))
        self.assertEqual(dotenv_values(self.secret, interpolate=False)["SMTP_PASSWORD"], password)
        self.assertEqual(self.store.get_smtp_settings()["password"], password)
        row = self.store._query("SELECT password FROM smtp_settings")[0]
        self.assertEqual(row["password"], "env:SMTP_PASSWORD")
        self.assertNotIn(password.encode(), self.db.read_bytes())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)

    def test_latest_file_value_is_used_after_reopen_and_blank_updates_keep_secret(self):
        self.store.save_smtp_settings(**self.config)
        self.store._conn.close()
        self.store = Storage(str(self.db), secret_file=str(self.secret))
        set_key(self.secret, "SMTP_PASSWORD", "latest-file-password")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "stale-process-password"}):
            self.assertEqual(self.store.get_smtp_settings()["password"], "latest-file-password")
            self.store.save_smtp_settings(**{**self.config, "password": "", "host": "updated.example.com"})
        self.assertEqual(self.store.get_smtp_settings()["password"], "latest-file-password")
        self.assertEqual(self.store.get_smtp_settings()["host"], "updated.example.com")
        self.assertNotIn(b"latest-file-password", self.db.read_bytes())

    def test_legacy_password_migrates_before_database_is_scrubbed(self):
        self.store._conn.close()
        legacy = Storage(str(self.db))
        legacy.save_smtp_settings(**self.config)
        legacy._conn.close()
        self.secret.write_text("# existing config\nBOT_TOKEN=synthetic\n", encoding="utf-8")
        self.store = Storage(str(self.db), secret_file=str(self.secret))
        self.assertTrue(self.store.migrate_smtp_secret())
        self.assertFalse(self.store.migrate_smtp_secret())
        self.assertEqual(self.store.get_smtp_settings()["password"], self.config["password"])
        self.assertEqual(self.store._query("SELECT password FROM smtp_settings")[0]["password"], "env:SMTP_PASSWORD")
        self.assertNotIn(self.config["password"].encode(), self.db.read_bytes())
        self.assertIn("# existing config\nBOT_TOKEN=synthetic\n", self.secret.read_text(encoding="utf-8"))

    def test_no_smtp_does_not_create_secret_file(self):
        self.assertIsNone(self.store.get_smtp_settings())
        self.assertFalse(self.store.migrate_smtp_secret())
        self.assertFalse(self.secret.exists())

    def test_env_write_failure_preserves_existing_db_settings(self):
        self.store.save_smtp_settings(**self.config)
        with patch("storage.set_key", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.store.save_smtp_settings(**{**self.config, "password": "replacement", "host": "new.example.com"})
        self.assertEqual(self.store.get_smtp_settings()["host"], self.config["host"])
        self.assertEqual(self.store.get_smtp_settings()["password"], self.config["password"])

    def test_failed_legacy_migration_leaves_password_available_for_retry(self):
        self.store._conn.close()
        legacy = Storage(str(self.db))
        legacy.save_smtp_settings(**self.config)
        legacy._conn.close()
        self.store = Storage(str(self.db), secret_file=str(self.secret))
        with patch("storage.set_key", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.store.migrate_smtp_secret()
        self.assertEqual(self.store._query("SELECT password FROM smtp_settings")[0]["password"], self.config["password"])


if __name__ == "__main__":
    unittest.main()
