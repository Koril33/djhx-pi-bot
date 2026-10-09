import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from bot import AgentSession
from memory import MemoryService, memory_match, safe_turn_text
from storage import Storage
from scripts.backup_db import main as backup_main


def settings(folder: str, **overrides):
    values = dict(
        memory_enabled=True, memory_auto_extract=True,
        memory_batch_turns=2, memory_turn_retention=2,
        memory_summary_retry_seconds=3600,
        memory_turn_user_chars=100, memory_turn_assistant_chars=100,
        memory_summary_input_max_bytes=1000, memory_summary_response_max_bytes=1000,
        memory_active_limit=4, memory_item_max_chars=80,
        memory_inject_max_items=2, memory_inject_max_bytes=300,
        memory_summary_max_bytes=100, memory_items_max_bytes=150,
        memory_context_dir=str(Path(folder) / "context"),
        pi_cmd="pi", pi_session_dir=str(Path(folder) / "sessions"),
        pi_provider="deepseek", pi_model="deepseek-flash", pricing_file="pricing.json",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class MemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.settings = settings(self.folder.name)
        self.memory = MemoryService(self.settings, self.storage)
        self.storage.ensure_user(7, "tester")
        self.sid = self.storage.create_session(7, "topic")
        self.storage.set_active(self.sid)

    def tearDown(self):
        self.storage._conn.close()
        self.folder.cleanup()

    def test_used_memory_snapshot_only_includes_complete_context_items(self):
        self.settings.memory_items_max_bytes = 1000
        self.settings.memory_inject_max_bytes = 300
        for index in range(3):
            self.storage.add_memory(
                7, f"记忆内容 {index} " + "甲" * 30, limit=4,
            )
        used = []
        body = self.memory.build_context(7, self.sid, None, "记忆内容", used)
        self.assertEqual(len(used), 1)
        self.assertIn("- " + used[0]["text"], body)
        self.assertNotIn("记忆内容 1 " + "甲" * 30, body)
        self.assertTrue(all(set(item) == {"id", "text", "source", "reason"} for item in used))

    def test_auto_memory_pause_keeps_summary_but_blocks_new_items(self):
        first = self.storage.start_turn(7, self.sid, 101, "问题一", False)
        self.storage.finish_turn(first, "回答一", complete=True)
        self.storage.set_user_auto_memory(7, False)
        self.storage.apply_summary(
            7, self.sid, 0, first, "摘要一", ["应被拦截的记忆"],
            memory_limit=4, turn_retention=2,
        )
        self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "摘要一")
        self.assertEqual(self.storage.list_memories(7), [])
        self.storage.set_user_auto_memory(7, True)
        second = self.storage.start_turn(7, self.sid, 102, "问题二", False)
        self.storage.finish_turn(second, "回答二", complete=True)
        self.storage.apply_summary(
            7, self.sid, first, second, "摘要二", ["恢复后的记忆"],
            memory_limit=4, turn_retention=2,
        )
        self.assertEqual(self.storage.list_memories(7)[0]["text"], "恢复后的记忆")
        self.storage.set_user_auto_memory(7, None)
        self.assertIsNone(self.storage.get_user_auto_memory(7))

    def test_chinese_phrase_match_has_explainable_priority(self):
        specific = memory_match("偏好：请使用简体中文", "能不能用简体中文给我回答")
        generic = memory_match("我今天想喝茶", "能不能用简体中文给我回答")
        self.assertGreater(specific[0], generic[0])
        self.assertIn("简体中文", specific[1])
        self.assertEqual(memory_match("无关内容", "异步取消")[0], 0)

    def test_turn_memory_snapshot_is_scoped_and_survives_memory_edit(self):
        memory_id = self.storage.add_memory(7, "原始记忆", limit=4)
        turn_id = self.storage.start_turn(7, self.sid, 12345, "测试", False)
        self.storage.set_turn_memories(turn_id, [
            {"id": memory_id, "text": "原始记忆", "source": "manual"},
        ])
        self.storage.edit_memory(7, memory_id, "更新后的记忆")
        row = self.storage.get_turn_for_user(7, self.sid, turn_id)
        self.assertEqual(json.loads(row["injected_memory_json"])[0]["text"], "原始记忆")
        self.assertIsNone(self.storage.get_turn_for_user(8, self.sid, turn_id))
        self.assertEqual(self.storage.recent_turns_for_user(7, self.sid)[0]["id"], turn_id)

    def test_memory_is_bounded_and_delete_removes_next_context(self):
        item_id = self.storage.add_memory(7, "我喜欢用中文回答", limit=4)
        self.storage.add_memory(7, "长期偏好：代码保持简洁", limit=4)
        self.storage.add_memory(7, "第三条很长的记忆" * 10, limit=4)
        text = self.memory.build_context(7, self.sid, None, "请用中文回答")
        self.assertIn("我喜欢用中文回答", text)
        self.assertLessEqual(len(text.encode("utf-8")), self.settings.memory_inject_max_bytes)
        self.assertLessEqual(text.count("\n- "), self.settings.memory_inject_max_items)
        path = self.memory.context_path(7)
        self.memory.refresh_context(7, self.sid, None, "请用中文回答")
        self.assertIn("我喜欢用中文回答", json.loads(path.read_text(encoding="utf-8"))["text"])
        self.assertTrue(self.storage.delete_memory(7, item_id))
        self.memory.refresh_context(7, self.sid, None, "请用中文回答")
        self.assertNotIn("我喜欢用中文回答", json.loads(path.read_text(encoding="utf-8"))["text"])

    def test_unrelated_memories_are_not_injected_but_pins_remain(self):
        self.storage.add_memory(7, "我喜欢喝咖啡", limit=4)
        pinned = self.storage.add_memory(7, "回答使用中文", limit=4)
        self.storage.pin_memory(7, pinned, True)
        body = self.memory.build_context(7, self.sid, None, "排查异步取消")
        self.assertNotIn("咖啡", body)
        self.assertIn("回答使用中文", body)

    def test_saturated_old_summary_cannot_displace_latest_user_and_assistant_tail(self):
        self.settings.memory_summary_max_bytes = 1536
        self.storage.apply_summary(7, self.sid, 0, 0, "旧" * 512, [], memory_limit=4, turn_retention=2)
        turn = self.storage.start_turn(7, self.sid, 2345, "问" * 4000 + "最新用户决定_MARKER", False)
        self.storage.append_turn_steer(turn, "补充要求_MARKER")
        self.storage.finish_turn(turn, "答" * 8000 + "最新助手结论_MARKER", complete=True)
        body = self.memory.build_recovery(self.sid)
        self.assertLessEqual(len(body.encode("utf-8")), 1536)
        self.assertIn("旧摘要", body)
        self.assertIn("最新用户决定_MARKER", body)
        self.assertIn("补充要求_MARKER", body)
        self.assertIn("最新助手结论_MARKER", body)
        self.assertIn("中间省略", body)

    def test_summary_progress_and_recovery_without_jsonl(self):
        for update_id in (10, 11, 12):
            turn = self.storage.start_turn(7, self.sid, update_id, f"用户 {update_id}", False)
            self.storage.finish_turn(turn, f"助手 {update_id}", complete=True)
        pending = self.storage.pending_summary_turns(self.sid)
        self.storage.apply_summary(7, self.sid, 0, pending[1]["id"], "已讨论用户 10 和 11", ["喜欢简短回答"],
                                   memory_limit=4, turn_retention=2)
        self.assertEqual([row["id"] for row in self.storage.pending_summary_turns(self.sid)],
                         [pending[2]["id"]])
        recovery = self.memory.build_recovery(self.sid)
        self.assertIn("已讨论用户 10 和 11", recovery)
        self.assertIn("用户 12", recovery)
        self.storage.set_session_recovery(self.sid, "/new/session.jsonl", recovery)
        self.assertIn("已讨论用户 10 和 11", self.memory.build_context(7, self.sid,
                                                        "/new/session.jsonl", ""))

    def test_duplicate_update_and_memory_limit(self):
        first = self.storage.start_turn(7, self.sid, 50, "hello", False)
        second = self.storage.start_turn(7, self.sid, 50, "hello", False)
        self.assertEqual(first, second)
        for index in range(4):
            self.storage.add_memory(7, f"事实 {index}", limit=4)
        with self.assertRaises(ValueError):
            self.storage.add_memory(7, "事实 5", limit=4)

    def test_interrupted_turn_is_incomplete_and_notice_survives_restart(self):
        running = self.storage.start_turn(7, self.sid, 51, "中断前的消息", False)
        self.assertEqual(self.storage.fail_interrupted_turns(), 1)
        self.assertEqual(self.storage.fail_interrupted_turns(), 0)
        self.assertEqual(self.storage._query("SELECT status FROM conversation_turns WHERE id = ?", (running,))[0]["status"],
                         "incomplete")
        self.assertEqual(self.storage.pending_summary_turns(self.sid), [])
        self.storage._conn.close()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.assertEqual(self.storage.pending_interruption_notice_count(7), 1)
        self.storage.acknowledge_interruption_notice(7)
        self.assertEqual(self.storage.pending_interruption_notice_count(7), 0)

    def test_secret_is_redacted_before_turn_is_clipped(self):
        text = "前言 password:abcdefghijklmnop 后文"
        safe = safe_turn_text(text, 24)
        self.assertLessEqual(len(safe), 24)
        self.assertNotIn("abc", safe)
        self.assertIn("已省略敏感内容", safe)

    def test_single_file_backup_contains_memory(self):
        self.storage.add_memory(7, "一条可迁移记忆", limit=4)
        target = Path(self.folder.name) / "copy.db"
        with patch.object(sys, "argv", ["backup_db", str(target), "--source", str(Path(self.folder.name) / "bot.db")]):
            backup_main()
        with closing(sqlite3.connect(target)) as copied:
            self.assertEqual(copied.execute("SELECT text FROM memory_items").fetchone()[0], "一条可迁移记忆")
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_existing_usage_database_is_migrated(self):
        legacy_path = Path(self.folder.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as old:
            old.execute("CREATE TABLE usage_runs (id TEXT PRIMARY KEY, user_id INTEGER, "
                        "session_id TEXT, telegram_update_id INTEGER, started_at REAL, finished_at REAL, "
                        "status TEXT, stats_before TEXT, stats_after TEXT)")
        migrated = Storage(str(legacy_path))
        try:
            columns = {row[1] for row in migrated._conn.execute("PRAGMA table_info(usage_runs)")}
            self.assertIn("origin", columns)
            self.assertIn("memory_context_bytes", columns)
        finally:
            migrated._conn.close()

    def test_existing_turn_database_gets_interruption_notice_column(self):
        legacy_path = Path(self.folder.name) / "legacy_turns.db"
        with closing(sqlite3.connect(legacy_path)) as old:
            old.execute(
                "CREATE TABLE conversation_turns (id INTEGER PRIMARY KEY, user_id INTEGER, "
                "session_id TEXT, telegram_update_id INTEGER UNIQUE, user_text TEXT, "
                "steer_text TEXT DEFAULT '', assistant_text TEXT DEFAULT '', has_image INTEGER DEFAULT 0, "
                "status TEXT DEFAULT 'running', created_at REAL)"
            )
            old.execute(
                "INSERT INTO conversation_turns (id, user_id, session_id, user_text, status, created_at) "
                "VALUES (1, 7, 'old', '中断的消息', 'running', 1)"
            )
            old.commit()
        migrated = Storage(str(legacy_path))
        try:
            self.assertEqual(migrated.fail_interrupted_turns(), 1)
            self.assertEqual(migrated.pending_interruption_notice_count(7), 1)
        finally:
            migrated._conn.close()


class AutomaticSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.settings = settings(self.folder.name)
        self.memory = MemoryService(self.settings, self.storage)
        self.sid = self.storage.create_session(7, "topic")
        self.storage.set_active(self.sid)
        for index in range(2):
            turn = self.storage.start_turn(7, self.sid, index, f"用户问候 {index}", False)
            self.storage.finish_turn(turn, "好的", complete=True)

    async def asyncTearDown(self):
        await self.memory.shutdown()
        self.storage._conn.close()
        self.folder.cleanup()

    async def test_memory_auto_command_pauses_resumes_and_restores_default(self):
        manager = SimpleNamespace(memory=self.memory)
        context = SimpleNamespace(args=["auto", "off"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(update, context)
            self.assertFalse(self.storage.get_user_auto_memory(7))
            self.assertIn("摘要仍会更新", reply.await_args.args[1])
            context.args = ["auto", "on"]
            await bot.cmd_memory(update, context)
            self.assertTrue(self.storage.get_user_auto_memory(7))
            context.args = ["auto", "default"]
            await bot.cmd_memory(update, context)
            self.assertIsNone(self.storage.get_user_auto_memory(7))
            self.settings.memory_auto_extract = False
            context.args = ["auto", "on"]
            await bot.cmd_memory(update, context)
            self.assertIn("全局自动提取已关闭", reply.await_args.args[1])
            self.assertIsNone(self.storage.get_user_auto_memory(7))

    async def test_memory_search_shows_phrase_basis_and_user_scope(self):
        specific_id = self.storage.add_memory(7, "偏好：请使用简体中文", limit=4)
        self.storage.add_memory(7, "我今天想喝茶", limit=4)
        self.storage.add_memory(8, "他人的简体中文记忆", limit=4)
        manager = SimpleNamespace(memory=self.memory)
        context = SimpleNamespace(args=["search", "能不能用简体中文给我回答"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(update, context)
        result = reply.await_args.args[1]
        self.assertIn(specific_id, result)
        self.assertIn("命中：用简体中文", result)
        self.assertNotIn("他人的简体中文记忆", result)

    async def test_memory_used_command_reads_current_session_snapshot_only(self):
        memory_id = self.storage.add_memory(7, "回合当时的记忆", limit=4)
        turn_id = self.storage.start_turn(7, self.sid, 555, "本轮问题", False)
        self.storage.set_turn_memories(turn_id, [
            {"id": memory_id, "text": "回合当时的记忆", "source": "manual", "reason": "命中：记忆"},
        ])
        self.storage.finish_turn(turn_id, "回答", complete=True)
        manager = SimpleNamespace(memory=self.memory)
        context = SimpleNamespace(args=["used"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(update, context)
            self.assertIn("回合当时的记忆", reply.await_args.args[1])
            self.assertIn("依据：命中：记忆", reply.await_args.args[1])
            self.assertIn(f"#{turn_id}", reply.await_args.args[1])
            context.args = ["used", "list"]
            await bot.cmd_memory(update, context)
            self.assertIn(f"#{turn_id}", reply.await_args.args[1])
            context.args = ["used", str(turn_id)]
            update.effective_user.id = 8
            await bot.cmd_memory(update, context)
            self.assertIn("当前没有会话", reply.await_args.args[1])

    async def test_memory_list_buttons_show_source_and_update_time(self):
        self.settings.memory_active_limit = 20
        for index in range(9):
            self.storage.add_memory(
                7, f"记忆 {index}", limit=20,
                source="auto" if index == 8 else "manual",
            )
        self.storage.add_memory(8, "其他用户记忆", limit=20)
        manager = SimpleNamespace(memory=self.memory)
        context = SimpleNamespace(args=["list"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(update, context)
            first = reply.await_args
            self.assertIn("第 1/2 页", first.args[1])
            self.assertIn("来源：自动提取", first.args[1])
            self.assertIn("来源：手动添加", first.args[1])
            self.assertIn("更新：20", first.args[1])
            self.assertNotIn("其他用户记忆", first.args[1])
            button = first.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.callback_data, "memory_page:2")
            update.callback_query = SimpleNamespace(data=button.callback_data, answer=AsyncMock())
            await bot.on_callback(update, context)
            second = reply.await_args
            self.assertIn("第 2/2 页", second.args[1])
            self.assertEqual(
                second.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                "memory_page:1",
            )
            update.callback_query.answer.assert_awaited_once()

    async def test_auto_summary_uses_flash_and_separate_cost_run(self):
        calls = []

        class FakePi:
            def __init__(self, *args, **kwargs):
                calls.append(kwargs)

            async def prompt(self, text, on_usage=None):
                on_usage({"kind": "assistant", "provider": "deepseek", "model": "deepseek-flash",
                          "occurred_at": 1790215200, "usage": {"input": 50, "cacheRead": 0, "output": 10}})
                yield json.dumps({"summary": "用户已打招呼", "memories": ["用户喜欢中文交流"]}, ensure_ascii=False)

            async def stop(self):
                pass

        with patch("memory.PiRpcClient", FakePi):
            await self.memory._summarize(7, self.sid)
        self.assertEqual(calls[0]["provider"], "deepseek")
        self.assertEqual(calls[0]["model"], "deepseek-flash")
        self.assertNotIn("--append-system-prompt", calls[0]["extra_args"])
        self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "用户已打招呼")
        status = self.storage.get_summary_job_status(7, self.sid)
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNone(status["last_failure_at"])
        self.assertIsNone(self.storage.get_summary_job_status(8, self.sid))
        self.assertEqual(self.storage.list_memories(7)[0]["text"], "用户喜欢中文交流")
        run = self.storage.get_last_usage_run(7)
        self.assertEqual(run["origin"], "memory_maintenance")
        self.assertEqual(len(self.storage.get_usage_events(run["id"])), 1)

    async def test_long_chinese_turn_is_fully_read_before_covering_and_each_call_is_isolated(self):
        self.settings.memory_summary_input_max_bytes = 16000
        self.settings.memory_summary_max_bytes = 1536
        self.settings.memory_summary_response_max_bytes = 8192
        pending = self.storage.pending_summary_turns(self.sid)
        self.storage._exec("UPDATE conversation_turns SET user_text = ?, assistant_text = ? WHERE id = ?",
                           ("问" * 4000, "答" * 8000 + "末尾关键决定_MARKER", pending[0]["id"]))
        before = self.storage.pending_summary_turns(self.sid)
        instructions, clients = [], []

        class FakePi:
            def __init__(self, *args, **kwargs):
                self.stopped = False
                clients.append(self)

            async def prompt(self, instruction, **kwargs):
                instructions.append(instruction)
                yield json.dumps({"summary": "最新决定已保存" if "末尾关键决定_MARKER" in instruction else "中间摘要",
                                  "memories": []}, ensure_ascii=False)

            async def stop(self):
                self.stopped = True

        with patch("memory.PiRpcClient", FakePi):
            await self.memory._summarize(7, self.sid)
        self.assertGreater(len(instructions), 1)
        self.assertTrue(all(len(text.encode("utf-8")) <= 16000 for text in instructions))
        raw = "".join(re.split(r"\n片段\d+/\d+（按时间先后，尚未结束则保留待续事项）：\n", text)[1]
                      for text in instructions)
        self.assertIn("问" * 4000, raw)
        self.assertIn("答" * 8000 + "末尾关键决定_MARKER", raw)
        self.assertTrue(all(client.stopped for client in clients))
        self.assertEqual(len(clients), len(instructions))
        self.assertEqual(self.storage.get_summary(self.sid)["covered_turn_id"], before[-1]["id"])
        self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "最新决定已保存")

    async def test_failed_middle_fragment_leaves_entire_turn_pending_for_retry(self):
        pending = self.storage.pending_summary_turns(self.sid)
        self.storage._exec("UPDATE conversation_turns SET assistant_text = ? WHERE id = ?",
                           ("长" * 2000 + "最终决定", pending[0]["id"]))
        calls = []

        class FailingSecondPi:
            def __init__(self, *args, **kwargs):
                pass

            async def prompt(self, instruction, **kwargs):
                calls.append(instruction)
                if len(calls) == 2:
                    raise RuntimeError("模拟第二片失败")
                yield '{"summary":"中间结果","memories":["仅第一片候选"]}'

            async def stop(self):
                pass

        with patch("memory.PiRpcClient", FailingSecondPi):
            with self.assertLogs("memory", level="ERROR"):
                await self.memory._summarize(7, self.sid)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.storage.get_summary(self.sid))
        self.assertEqual(len(self.storage.pending_summary_turns(self.sid)), 2)
        self.assertEqual(self.storage.list_memories(7), [])
        self.assertEqual(self.storage.get_last_usage_run(7)["status"], "incomplete")

    async def test_summary_failure_is_saved_without_internal_error_text(self):
        class FailingPi:
            def __init__(self, *args, **kwargs):
                pass

            async def prompt(self, text, on_usage=None):
                if False:
                    yield ""
                raise RuntimeError("private diagnostic")

            async def stop(self):
                pass

        with patch("memory.PiRpcClient", FailingPi):
            await self.memory._summarize(7, self.sid)
        status = self.storage.get_summary_job_status(7, self.sid)
        self.assertIsNotNone(status["last_failure_at"])
        self.assertEqual(status["last_failure_kind"], "RuntimeError")
        self.assertNotIn("private", status["last_failure_kind"])

    async def test_summary_status_command_shows_pending_and_recent_results(self):
        self.storage.record_summary_job_result(
            7, self.sid, success=False, failure_kind="TimeoutError",
        )
        manager = SimpleNamespace(memory=self.memory)
        context = SimpleNamespace(args=["status"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_summary(update, context)
        body = reply.await_args.args[1]
        self.assertIn("待处理完整回合：2", body)
        self.assertIn("最近一次失败", body)
        self.assertIn("TimeoutError", body)

    async def test_interruption_notice_is_acknowledged_only_after_delivery(self):
        self.storage.start_turn(7, self.sid, 999, "上次未完成", False)
        self.storage.fail_interrupted_turns()
        update = SimpleNamespace()
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            reply.side_effect = RuntimeError("Telegram unavailable")
            with self.assertRaises(RuntimeError):
                await bot.notify_interrupted_turns(update, self.storage, 7)
            self.assertEqual(self.storage.pending_interruption_notice_count(7), 1)
            reply.side_effect = None
            await bot.notify_interrupted_turns(update, self.storage, 7)
            self.assertIn("不会纳入 SQLite 摘要", reply.await_args.args[1])
        self.assertEqual(self.storage.pending_interruption_notice_count(7), 0)

    async def test_missing_jsonl_rebinds_existing_app_session(self):
        active = self.storage.get_active_session(7)
        self.storage.set_pi_session_file(active["id"], "/missing/history.jsonl")
        self.storage.apply_summary(7, self.sid, 0,
                                   self.storage.pending_summary_turns(self.sid)[-1]["id"],
                                   "旧会话已讨论问候", [], memory_limit=4, turn_retention=2)
        with patch("bot.PiRpcClient") as client_type:
            client = client_type.return_value
            client.reset_session = AsyncMock(return_value={"sessionFile": "/new/history.jsonl", "messageCount": 0})
            client.ensure_running = AsyncMock()
            client.stop = AsyncMock()
            client.has_live_session.return_value = False
            client.busy = False
            agent = AgentSession(7, self.settings, self.storage, self.memory)
            with self.assertRaises(bot.SessionRecoveryRequired):
                await agent.ensure_client()
            client.reset_session.assert_not_awaited()
            preview = bot.session_recovery_preview(agent, self.storage, 7, self.sid)
            self.assertIn("部分恢复", preview["text"])
            self.assertIn("旧会话已讨论问候", preview["recovery"])
            with self.assertRaises(bot.SessionRecoveryRequired):
                await bot.switch_user_session(agent, self.storage, 7, self.sid)
            client.reset_session.assert_not_awaited()
            await bot.switch_user_session(agent, self.storage, 7, self.sid, preview["version"])
        self.assertEqual(self.storage.get_active_session(7)["id"], self.sid)
        self.assertEqual(self.storage.get_active_session(7)["pi_session_file"], "/new/history.jsonl")
        self.assertIn("旧会话已讨论问候", self.storage.get_session_recovery(self.sid,
                                                        "/new/history.jsonl"))

    async def test_recovery_preview_distinguishes_blank_and_rejects_stale_confirmation(self):
        blank = self.storage.create_session(7, "空白测试")
        self.storage.set_pi_session_file(blank, "/missing/blank.jsonl")
        with patch("bot.PiRpcClient") as client_type:
            client = client_type.return_value
            client.busy = False
            client.has_live_session.return_value = False
            client.reset_session = AsyncMock(return_value={"sessionFile": "/new/blank.jsonl"})
            agent = AgentSession(7, self.settings, self.storage, self.memory)
            preview = bot.session_recovery_preview(agent, self.storage, 7, blank)
            self.assertEqual(preview["mode"], "空白恢复")
            self.assertIn("可用完整回合：0 条", preview["text"])
            self.assertEqual(preview["recovery"], "")
            with self.assertRaises(ValueError):
                bot.session_recovery_preview(agent, self.storage, 8, blank)
            turn = self.storage.start_turn(7, blank, None, "后来补录", False)
            self.storage.finish_turn(turn, "可恢复内容", complete=True)
            with self.assertRaises(bot.SessionRecoveryRequired):
                await bot.switch_user_session(agent, self.storage, 7, blank, preview["version"])
            client.reset_session.assert_not_awaited()
            fresh = bot.session_recovery_preview(agent, self.storage, 7, blank)
            self.assertEqual(fresh["mode"], "部分恢复")
            self.assertNotEqual(fresh["version"], preview["version"])
            self.assertEqual(self.storage.get_active_session(7)["id"], self.sid)

    async def test_chat_persona_is_versioned_and_loaded_only_for_chat(self):
        with patch("bot.PiRpcClient") as client_type:
            AgentSession(7, self.settings, self.storage, self.memory)
        args = client_type.call_args.kwargs["extra_args"]
        index = args.index("--append-system-prompt")
        persona = Path(args[index + 1])
        self.assertTrue(persona.is_absolute())
        self.assertIn("小丁", persona.read_text(encoding="utf-8"))
        self.assertIn("--extension", args)

    async def test_memory_delete_is_user_scoped_and_stops_injection(self):
        memory_id = self.storage.add_memory(7, "喜欢中文回答", limit=4)
        agent = SimpleNamespace(control_lock=asyncio.Lock(), client=SimpleNamespace(busy=False))
        manager = SimpleNamespace(memory=self.memory, get=lambda _: agent)
        context = SimpleNamespace(args=["delete", memory_id], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        wrong_user = SimpleNamespace(effective_user=SimpleNamespace(id=8))
        right_user = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(wrong_user, context)
            self.assertIsNotNone(self.storage.get_memory(7, memory_id))
            await bot.cmd_memory(right_user, context)
            self.assertIsNotNone(self.storage.get_memory(7, memory_id))
            self.assertIn("确认删除", reply.await_args.args[1])
            callback = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            right_user.callback_query = SimpleNamespace(data=callback, answer=AsyncMock())
            await bot.on_callback(right_user, context)
            self.assertIsNone(self.storage.get_memory(7, memory_id))
            self.assertIn("已删除", reply.await_args.args[1])

    async def test_old_memory_confirmation_does_not_delete_edited_version(self):
        memory_id = self.storage.add_memory(7, "旧内容", limit=4)
        agent = SimpleNamespace(control_lock=asyncio.Lock(), client=SimpleNamespace(busy=False))
        manager = SimpleNamespace(memory=self.memory, get=lambda _: agent)
        context = SimpleNamespace(args=["delete", memory_id], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_memory(update, context)
            callback = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            self.storage.edit_memory(7, memory_id, "更新后的内容")
            update.callback_query = SimpleNamespace(data=callback, answer=AsyncMock())
            await bot.on_callback(update, context)
            self.assertIn("预览已过期", reply.await_args.args[1])
        self.assertEqual(self.storage.get_memory(7, memory_id)["text"], "更新后的内容")

    async def test_summary_clear_requires_current_version_confirmation(self):
        covered = self.storage.pending_summary_turns(self.sid)[-1]["id"]
        self.storage.apply_summary(7, self.sid, 0, covered, "旧摘要", [],
                                   memory_limit=4, turn_retention=2)
        agent = SimpleNamespace(control_lock=asyncio.Lock(), client=SimpleNamespace(busy=False))
        manager = SimpleNamespace(memory=self.memory, get=lambda _: agent)
        context = SimpleNamespace(args=["clear"], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": manager,
        })
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.cmd_summary(update, context)
            self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "旧摘要")
            callback = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            self.storage._exec(
                "UPDATE session_summaries SET summary_text = ?, updated_at = updated_at + 1 WHERE session_id = ?",
                ("新摘要", self.sid),
            )
            update.callback_query = SimpleNamespace(data=callback, answer=AsyncMock())
            await bot.on_callback(update, context)
            self.assertIn("预览已过期", reply.await_args.args[1])
            self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "新摘要")
            context.args = ["clear"]
            await bot.cmd_summary(update, context)
            callback = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            update.callback_query = SimpleNamespace(data=callback, answer=AsyncMock())
            await bot.on_callback(update, context)
        self.assertEqual(self.storage.get_summary(self.sid)["summary_text"], "")


if __name__ == "__main__":
    unittest.main()
