import re
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import _FIELDS, apply_runtime_settings, router
from config import Settings
from costs import PriceResult
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
        self.manager = SimpleNamespace(memory=None, _agents={})
        app.state.manager = self.manager
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
        self.assertIn("<style>", response.text)
        self.assertNotIn('href="/admin/style.css"', response.text)
        self.assertIn("style-src 'sha256-", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_overview_hides_failed_payload_and_usage_uses_ledger(self):
        self.storage.enqueue_update(99, '{"update_id":99,"message":"private"}')
        self.storage.claim_updates(1)
        self.storage.fail_update(99, "RuntimeError: private diagnostic")
        self.storage.enqueue_update(100, '{"update_id":100}')
        self.storage.claim_updates(1)
        self.storage.fail_update(100, "服务重启时更新执行状态不确定；未自动重放")
        run = self.storage.start_usage_run(7, None, None, None)
        self.storage.record_usage_event(run, 1, {
            "kind": "assistant", "occurred_at": 1, "provider": "deepseek", "model": "deepseek-flash",
            "usage": {"input": 20, "cacheRead": 80, "output": 10},
        }, PriceResult("priced", usd="0.0002"))
        self.storage.finish_usage_run(run, "complete", None)
        self.login()
        overview = self.client.get("/admin/overview?user_id=7")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("RuntimeError", overview.text)
        self.assertIn("重启时状态不确定", overview.text)
        self.assertNotIn("private diagnostic", overview.text)
        self.assertNotIn("private\"", overview.text)
        usage = self.client.get("/admin/usage?user_id=7&range=today")
        self.assertEqual(usage.status_code, 200)
        self.assertIn("deepseek/deepseek-flash", usage.text)
        self.assertIn("80%", usage.text)
        self.assertIn("$0.0002", usage.text)
        self.assertEqual(self.client.get("/admin/usage?user_id=8").text.count("$0.0002"), 0)

    def test_session_manage_uses_shared_flow_and_checks_ownership(self):
        old = self.storage.create_session(7, "旧会话")
        self.storage.set_pi_session_file(old, "/old.jsonl")
        self.storage.set_active(old)
        other = self.storage.create_session(8, "别人的会话")
        client = SimpleNamespace(
            busy=False, proc=None, model="deepseek-flash", has_live_session=Mock(return_value=False),
            new_session=AsyncMock(), get_state=AsyncMock(return_value={"sessionFile": "/new.jsonl"}),
            switch_session=AsyncMock(), stop=AsyncMock(),
        )
        agent = SimpleNamespace(
            client=client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(), memory=None, recovery_notice=None,
        )
        self.manager._agents[7] = agent
        self.manager.get = lambda user_id: agent if user_id == 7 else None
        self.login()
        page = self.client.get("/admin/sessions?user_id=7")
        self.assertIn("JSONL 缺失", page.text)
        self.storage.apply_summary(7, old, 0, 0, "旧讨论摘要", [], memory_limit=10, turn_retention=10)
        self.assertIn("SQLite 有摘要", self.client.get("/admin/sessions?user_id=7").text)
        csrf = self.csrf(page)
        self.assertEqual(self.client.post(f"/admin/sessions/{other}/use", data={
            "csrf": csrf, "user_id": "7",
        }).status_code, 404)
        created = self.client.post("/admin/sessions/new", data={
            "csrf": csrf, "user_id": "7", "name": "新会话",
        }, follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        new = self.storage.get_active_session(7)
        self.assertEqual(new["name"], "新会话")
        self.assertEqual(new["pi_session_file"], "/new.jsonl")
        self.assertEqual(self.client.post(f"/admin/sessions/{old}/rename", data={
            "csrf": csrf, "user_id": "7", "name": "改名",
        }, follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_session(old)["name"], "改名")
        switched = self.client.post(f"/admin/sessions/{old}/use", data={
            "csrf": csrf, "user_id": "7",
        }, follow_redirects=False)
        self.assertEqual(switched.status_code, 303)
        self.assertEqual(self.storage.get_active_session(7)["id"], old)
        client.switch_session.assert_awaited_with("/old.jsonl")
        agent.client.busy = True
        rejected = self.client.post("/admin/sessions/new", data={
            "csrf": csrf, "user_id": "7", "name": "不应创建",
        })
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("不应创建", [row["name"] for row in self.storage.list_sessions(7)])


if __name__ == "__main__":
    unittest.main()
