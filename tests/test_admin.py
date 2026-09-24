import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import _FIELDS, apply_runtime_settings, router
from config import Settings
from storage import Storage


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.storage.ensure_user(7, "alice")
        self.storage.ensure_user(8, "bob")
        self.settings = Settings(
            _env_file=None, bot_token="test", webhook_secret="webhook-secret",
            allowed_user_ids="7,8", admin_username="admin",
            admin_password="test-password-12345",
        )
        app = FastAPI()
        app.include_router(router)
        app.state.settings = self.settings
        app.state.storage = self.storage
        app.state.manager = SimpleNamespace(memory=None, _agents={})
        self.client = TestClient(app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.storage._conn.close()
        self.folder.cleanup()

    @staticmethod
    def csrf(page):
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def login(self):
        login = self.client.get("/admin/login")
        self.assertEqual(login.status_code, 200)
        response = self.client.post("/admin/login", data={
            "csrf": self.csrf(login), "username": "admin", "password": "test-password-12345",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_auth_csrf_and_memory_user_scope(self):
        self.assertEqual(self.client.get("/admin/memories", follow_redirects=False).status_code, 303)
        login = self.client.get("/admin/login")
        self.assertEqual(self.client.post("/admin/login", data={
            "csrf": self.csrf(login), "username": "admin", "password": "wrong",
        }).status_code, 401)
        self.login()
        page = self.client.get("/admin/memories?user_id=7")
        csrf = self.csrf(page)
        self.assertEqual(self.client.post("/admin/memories/add", data={
            "csrf": "wrong", "user_id": "7", "text": "用中文回答",
        }).status_code, 403)
        self.assertEqual(self.client.post("/admin/memories/add", data={
            "csrf": csrf, "text": "用中文回答",
        }).status_code, 400)
        response = self.client.post("/admin/memories/add", data={
            "csrf": csrf, "user_id": "7", "text": "用中文回答",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        item = self.storage.list_memories(7)[0]
        self.assertEqual(self.client.get("/admin/memories?user_id=8").text.count("用中文回答"), 0)
        self.assertEqual(self.client.post(f"/admin/memories/{item['id']}/delete", data={
            "csrf": csrf, "user_id": "8",
        }).status_code, 404)
        self.assertEqual(self.client.post(f"/admin/memories/{item['id']}/pin", data={
            "csrf": csrf, "user_id": "7", "pinned": "1",
        }, follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_memory(7, item["id"])["pinned"], 1)
        self.assertEqual(self.client.post(f"/admin/memories/{item['id']}/edit", data={
            "csrf": csrf, "user_id": "7", "text": "偏好简洁回复",
        }, follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_memory(7, item["id"])["text"], "偏好简洁回复")
        self.assertEqual(self.client.post(f"/admin/memories/{item['id']}/delete", data={
            "csrf": csrf, "user_id": "7",
        }, follow_redirects=False).status_code, 303)
        self.assertFalse(self.storage.list_memories(7))

    def test_summary_and_settings_survive_restart(self):
        self.login()
        csrf = self.csrf(self.client.get("/admin/summaries?user_id=7"))
        sid = self.storage.create_session(7, "项目讨论")
        self.storage.apply_summary(7, sid, 0, 0, "旧摘要", [], memory_limit=10, turn_retention=10)
        self.assertIn("旧摘要", self.client.get("/admin/summaries?user_id=7").text)
        self.assertEqual(self.client.post(f"/admin/summaries/{sid}/clear", data={
            "csrf": csrf, "user_id": "8",
        }).status_code, 404)
        self.assertEqual(self.client.post(f"/admin/summaries/{sid}/clear", data={
            "csrf": csrf, "user_id": "7",
        }, follow_redirects=False).status_code, 303)
        self.assertNotIn("旧摘要", self.client.get("/admin/summaries?user_id=7").text)
        data = {key: str(getattr(self.settings, key)) for key, *_ in _FIELDS}
        data.update(csrf=csrf, memory_batch_turns="9")
        self.assertEqual(self.client.post("/admin/settings", data=data, follow_redirects=False).status_code, 303)
        self.assertEqual(self.settings.memory_batch_turns, 9)
        self.assertFalse(self.settings.memory_auto_extract)
        loaded = Settings(
            _env_file=None, bot_token="test", webhook_secret="webhook-secret",
            allowed_user_ids="7,8", admin_username="admin", admin_password="test-password-12345",
        )
        apply_runtime_settings(loaded, self.storage)
        self.assertEqual(loaded.memory_batch_turns, 9)
        self.assertFalse(loaded.memory_auto_extract)
        data["memory_summary_max_bytes"] = "8000"
        self.assertEqual(self.client.post("/admin/settings", data=data).status_code, 400)
        self.assertEqual(self.storage.get_runtime_settings()["memory_summary_max_bytes"], 1536)

    def test_memory_text_is_escaped_and_login_requires_signed_cookie(self):
        self.storage.add_memory(7, '<img src=x onerror=alert(1)>', limit=100)
        page = self.client.get("/admin/login")
        self.assertEqual(self.client.post("/admin/login", data={
            "csrf": self.csrf(page), "username": "admin", "password": "test-password-12345",
        }, cookies={"dj_admin_prelogin": "tampered"}).status_code, 403)
        self.login()
        response = self.client.get("/admin/memories?user_id=7")
        self.assertIn("&lt;img", response.text)
        self.assertNotIn("<img src=x", response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
