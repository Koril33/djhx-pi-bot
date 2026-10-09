import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ops import create_backup, verify_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE check_data (value TEXT)")
            db.execute("INSERT INTO check_data VALUES ('kept')")
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        (self.sessions / "one.jsonl").write_text('{"type":"session"}\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_restores_consistent_db_and_full_jsonl(self):
        status = create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        restored = self.root / "restored"
        manifest = verify_backup(status["archive"], str(restored))
        self.assertEqual(manifest["session_files"], 1)
        with closing(sqlite3.connect(restored / "bot.db")) as db:
            self.assertEqual(db.execute("SELECT value FROM check_data").fetchone()[0], "kept")
        self.assertEqual((restored / "pi-sessions/one.jsonl").read_text(), '{"type":"session"}\n')
        with self.assertRaisesRegex(ValueError, "新目录"):
            verify_backup(status["archive"], str(restored))

    def test_backup_keeps_seven_daily_and_one_weekly_snapshot(self):
        for _ in range(9):
            create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        self.assertEqual(len(list((self.root / "backups/daily").glob("*.tar.gz"))), 7)
        self.assertEqual(len(list((self.root / "backups/weekly").glob("*.tar.gz"))), 1)

    def test_restore_rebinds_only_archived_session_paths_into_new_directory(self):
        original = str(self.sessions / "one.jsonl")
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE sessions (id TEXT, pi_session_file TEXT)")
            db.execute("CREATE TABLE session_recovery (session_id TEXT, pi_session_file TEXT)")
            db.execute("INSERT INTO sessions VALUES ('included', ?)", (original,))
            db.execute("INSERT INTO sessions VALUES ('missing', ?)", (str(self.sessions / "missing.jsonl"),))
            db.execute("INSERT INTO session_recovery VALUES ('included', ?)", (original,))
        status = create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        destination = self.root / "moved"
        manifest = verify_backup(status["archive"], str(destination))
        with closing(sqlite3.connect(destination / "bot.db")) as db:
            included = db.execute("SELECT pi_session_file FROM sessions WHERE id='included'").fetchone()[0]
            recovery = db.execute("SELECT pi_session_file FROM session_recovery").fetchone()[0]
            missing = db.execute("SELECT pi_session_file FROM sessions WHERE id='missing'").fetchone()[0]
        self.assertEqual(included, str(destination / "pi-sessions/one.jsonl"))
        self.assertEqual(recovery, included)
        self.assertTrue(Path(included).is_file())
        self.assertEqual(missing, str(self.sessions / "missing.jsonl"))
        self.assertEqual(manifest["restore"]["rebound_paths"], 2)
        self.assertTrue(manifest["restore"]["manual_rebind"])
        metadata = json.loads((destination / "restore-metadata.json").read_text())
        self.assertNotEqual(metadata["source_database_sha256"], metadata["restored_database_sha256"])

    def test_old_manifest_reports_manual_rebinding_without_guessing(self):
        status = create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        old = self.root / "old.tar.gz"
        with tarfile.open(status["archive"], "r:gz") as source, tarfile.open(old, "w:gz") as target:
            for member in source.getmembers():
                data = source.extractfile(member).read()
                if member.name == "manifest.json":
                    manifest = json.loads(data)
                    del manifest["source_session_dir"]
                    data = json.dumps(manifest).encode()
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        self.assertTrue(verify_backup(str(old))["restore"]["manual_rebind"])

    def test_restore_rejects_unlisted_traversal_and_symlinks_before_publication(self):
        status = create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        bad = self.root / "bad.tar.gz"
        with tarfile.open(status["archive"], "r:gz") as source, tarfile.open(bad, "w:gz") as target:
            for member in source.getmembers():
                target.addfile(member, source.extractfile(member))
            member = tarfile.TarInfo("../escape")
            member.size = 4
            target.addfile(member, io.BytesIO(b"nope"))
        destination = self.root / "new"
        with self.assertRaises(ValueError):
            verify_backup(str(bad), str(destination))
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_restore_rejects_hash_mismatch(self):
        status = create_backup(str(self.source), str(self.sessions), str(self.root / "backups"))
        bad = self.root / "bad-hash.tar.gz"
        with tarfile.open(status["archive"], "r:gz") as source, tarfile.open(bad, "w:gz") as target:
            for member in source.getmembers():
                data = source.extractfile(member).read()
                if member.name == "manifest.json":
                    manifest = json.loads(data)
                    manifest["files"][0]["sha256"] = "0" * 64
                    data = json.dumps(manifest).encode()
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "校验失败"):
            verify_backup(str(bad))
