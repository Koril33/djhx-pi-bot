import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import set_key

spec = importlib.util.spec_from_file_location(
    "service_entry", Path(__file__).resolve().parents[1] / "deploy/service_entry.py")
service_entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_entry)


class ServiceEntryTests(unittest.TestCase):
    def test_legacy_storage_marker_uses_latest_env_secret_during_code_rollback(self):
        class LegacyStorage:
            def get_smtp_settings(self):
                return {"host": "smtp.example.com", "password": "env:SMTP_PASSWORD"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / ".env"
            set_key(path, "SMTP_PASSWORD", "first-file-secret")
            service_entry.install_legacy_smtp_reader(LegacyStorage, root)
            legacy = LegacyStorage()
            with patch.dict(os.environ, {"SMTP_PASSWORD": "stale-process-secret"}):
                self.assertEqual(legacy.get_smtp_settings()["password"], "first-file-secret")
                set_key(path, "SMTP_PASSWORD", "latest-file-secret${UNCHANGED}")
                self.assertEqual(legacy.get_smtp_settings()["password"], "latest-file-secret${UNCHANGED}")
            self.assertEqual(legacy.get_smtp_settings()["host"], "smtp.example.com")

    def test_legacy_storage_without_marker_keeps_original_behavior(self):
        class LegacyStorage:
            def get_smtp_settings(self):
                return self.values
        service_entry.install_legacy_smtp_reader(LegacyStorage, Path("unused-fixture"))
        legacy = LegacyStorage()
        legacy.values = None
        self.assertIsNone(legacy.get_smtp_settings())
        legacy.values = {"password": "legacy-test-secret"}
        self.assertIs(legacy.get_smtp_settings(), legacy.values)
