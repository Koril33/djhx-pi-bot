import re
import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    def test_mail_settings_hide_secret_and_send_test(self):
        self.login()
        page = self.client.get("/admin/mail")
        csrf = self.csrf(page)
        values = {"csrf": csrf, "host": "mail.example.com", "port": "465",
                  "username": "bot@example.com", "from_address": "bot@example.com",
                  "password": "smtp-secret"}
        self.assertEqual(self.client.post("/admin/mail", data={**values, "csrf": "wrong"}).status_code, 403)
        saved = self.client.post("/admin/mail", data=values, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(self.storage.get_smtp_settings()["password"], "smtp-secret")
        page = self.client.get("/admin/mail")
        self.assertIn("mail.example.com", page.text)
        self.assertNotIn("smtp-secret", page.text)
        self.assertEqual(self.client.post("/admin/mail", data={**values, "password": ""},
                                          follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_smtp_settings()["password"], "smtp-secret")
        with patch("admin.send_email") as send:
            result = self.client.post("/admin/mail/test", data={"csrf": csrf, "to": "admin@example.com"},
                                      follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "admin@example.com")

    def test_overview_shows_refresh_time_and_refreshes_status(self):
        self.login()
        first = self.client.get("/admin/overview?user_id=7")
        self.assertEqual(first.status_code, 200)
        self.assertIn("数据读取于", first.text)
        self.assertRegex(first.text, r'<time datetime="[^"]+">\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} 北京时间</time>')
        self.assertIn('action="/admin/overview" class="overview-refresh"', first.text)
        self.assertIn('name="user_id" value="7"', first.text)
        self.storage.enqueue_update(991, '{"update_id":991}')
        refreshed = self.client.get("/admin/overview?user_id=7")
        self.assertIn("待处理更新</span><strong>1</strong>", refreshed.text)

    def test_failed_update_can_be_acknowledged_without_deleting_dedup_id(self):
        self.storage.enqueue_update(900, '{"update_id":900}')
        self.storage.fail_update(900, "network")
        self.login()
        page = self.client.get("/admin/overview?user_id=7")
        self.assertIn("#900", page.text)
        self.assertEqual(self.client.post("/admin/updates/900/acknowledge", data={"csrf": "wrong"}).status_code, 403)
        result = self.client.post("/admin/updates/900/acknowledge", data={"csrf": self.csrf(page)},
                                  follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.storage.update_stats()["failed"], 0)
        self.assertFalse(self.storage.enqueue_update(900, '{"update_id":900}'))
        self.assertNotIn("#900", self.client.get("/admin/overview").text)

    def test_persona_create_preview_test_switch_and_disable(self):
        self.login()
        page = self.client.get("/admin/personas?user_id=7")
        csrf = self.csrf(page)
        self.assertIn("小丁的朋友", page.text)
        self.assertIn("无人格 · Pi 默认", page.text)
        self.assertEqual(self.client.post("/admin/personas/none/use", data={
            "csrf": csrf, "user_id": "7",
        }, follow_redirects=False).status_code, 303)
        self.assertIsNone(self.storage.get_active_persona(7))
        values = {"csrf": csrf, "user_id": "7", "name": "理性朋友",
                  "identity": "你是一位理性的朋友。", "communication_style": "先给结论。"}
        preview = self.client.post("/admin/personas", data={**values, "action": "preview"})
        self.assertEqual(preview.status_code, 200)
        self.assertIn("## 身份", preview.text)
        self.assertEqual(len(self.storage.list_personas()), 1)
        self.assertEqual(self.client.post("/admin/personas", data={**values, "csrf": "bad", "action": "save"}).status_code, 403)
        created = self.client.post("/admin/personas", data={**values, "action": "save"},
                                   follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        row = next(item for item in self.storage.list_personas() if item["name"] == "理性朋友")
        persona_id = row["id"]
        detail = self.client.get(f"/admin/personas/{persona_id}?user_id=7")
        self.assertIn("你是一位理性的朋友。", detail.text)
        changed = self.client.post(f"/admin/personas/{persona_id}", data={
            **values, "identity": "你是一位可靠的朋友。", "action": "save",
        }, follow_redirects=False)
        self.assertEqual(changed.status_code, 303)
        self.assertIn("你是一位可靠的朋友。", self.client.get(f"/admin/personas/{persona_id}").text)
        with patch("admin._run_persona_test", new_callable=AsyncMock, return_value="测试回复") as run:
            result = self.client.post(f"/admin/personas/{persona_id}/test", data={
                "csrf": csrf, "user_id": "7", "question": "你好",
            })
        self.assertIn("测试回复", result.text)
        run.assert_awaited_once()
        self.assertEqual(self.client.post(f"/admin/personas/{persona_id}/use", data={
            "csrf": csrf, "user_id": "7",
        }, follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_active_persona(7)["id"], persona_id)
        self.assertEqual(self.client.post(f"/admin/personas/{persona_id}/toggle", data={
            "csrf": csrf, "enabled": "0",
        }, follow_redirects=False).status_code, 303)
        self.assertEqual(self.storage.get_active_persona(7)["id"], "default")
        self.assertEqual(self.client.post(f"/admin/personas/{persona_id}/delete", data={
            "csrf": csrf,
        }, follow_redirects=False).status_code, 303)
        self.assertIsNone(self.storage.get_persona(persona_id))

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

    def test_usage_filters_session_date_model_and_lists_unpriced_calls(self):
        cst = timezone(timedelta(hours=8))
        today = datetime.now(cst).date().isoformat()
        one = self.storage.create_session(7, "项目甲")
        two = self.storage.create_session(7, "项目乙")
        other_user = self.storage.create_session(8, "其他用户")
        def record(sid, model, status, price, days_ago=0):
            run = self.storage.start_usage_run(7, sid, None, None)
            self.storage.record_usage_event(run, 1, {
                "kind": "assistant", "occurred_at": 1, "provider": "deepseek", "model": model,
                "usage": {"input": 10, "output": 5},
            }, PriceResult(status, usd=price))
            self.storage.finish_usage_run(run, "complete", None)
            if days_ago:
                stamp = (datetime.now(cst) - timedelta(days=days_ago)).timestamp()
                with self.storage._lock, self.storage._conn:
                    self.storage._conn.execute("UPDATE usage_runs SET started_at = ? WHERE id = ?", (stamp, run))
        record(one, "flash", "priced", "0.001")
        record(one, "unpriced", "missing_rule", None)
        record(two, "flash", "priced", "0.002")
        record(one, "flash", "priced", "0.004", days_ago=3)
        self.login()
        url = f"/admin/usage?user_id=7&range=30d&session_id={one}&date_from={today}&date_to={today}"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("未计价调用", page.text)
        self.assertIn("缺少价格规则", page.text)
        self.assertIn("项目甲", page.text)
        self.assertIn("$0.001", page.text)
        self.assertNotIn("$0.002", page.text)
        self.assertNotIn("$0.004", page.text)
        filtered = self.client.get(url + "&model=deepseek%2Fflash")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("$0.001", filtered.text)
        self.assertNotIn("缺少价格规则", filtered.text)
        self.assertIn("当前筛选范围内没有未计价调用", filtered.text)
        self.assertEqual(self.client.get(f"/admin/usage?user_id=7&session_id={other_user}").status_code, 400)
        self.assertEqual(self.client.get("/admin/usage?user_id=7&model=unknown%2Fmodel").status_code, 400)
        self.assertEqual(self.client.get("/admin/usage?user_id=7&date_from=bad").status_code, 400)

    def test_missing_session_requires_preview_confirmation(self):
        from bot import session_recovery_preview
        from memory import MemoryService

        active = self.storage.create_session(7, "当前")
        active_file = Path(self.folder.name) / "active.jsonl"
        active_file.write_text("{}", encoding="utf-8")
        self.storage.set_pi_session_file(active, str(active_file))
        self.storage.set_active(active)
        missing = self.storage.create_session(7, "需恢复")
        self.storage.set_pi_session_file(missing, str(Path(self.folder.name) / "missing.jsonl"))
        memory = MemoryService(self.settings, self.storage)
        client = SimpleNamespace(
            busy=False, has_live_session=Mock(return_value=False),
            reset_session=AsyncMock(return_value={"sessionFile": str(Path(self.folder.name) / "new.jsonl")}),
            switch_session=AsyncMock(), stop=AsyncMock(),
        )
        agent = SimpleNamespace(client=client, prompt_pending=False, memory=memory,
                                ensure_client=AsyncMock(), refresh_persona=AsyncMock(),
                                control_lock=asyncio.Lock(), recovery_notice=None)
        self.manager._agents[7] = agent
        self.manager.get = lambda user_id: agent
        self.login()
        page = self.client.get("/admin/sessions?user_id=7")
        self.assertIn("预览恢复", page.text)
        self.assertIn("空白恢复", page.text)
        csrf = self.csrf(page)
        no_confirmation = self.client.post(f"/admin/sessions/{missing}/use", data={
            "csrf": csrf, "user_id": "7",
        })
        self.assertEqual(no_confirmation.status_code, 400)
        client.reset_session.assert_not_awaited()
        self.assertEqual(self.storage.get_active_session(7)["id"], active)
        version = session_recovery_preview(agent, self.storage, 7, missing)["version"]
        confirmed = self.client.post(f"/admin/sessions/{missing}/use", data={
            "csrf": csrf, "user_id": "7", "recovery_version": version,
        }, follow_redirects=False)
        self.assertEqual(confirmed.status_code, 303)
        client.reset_session.assert_awaited_once()
        self.assertEqual(self.storage.get_active_session(7)["id"], missing)

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
        client.has_live_session.return_value = True
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
