import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import bot
from bot import _compact_cost_report, _cost_report
from costs import PricingCatalog, PriceResult, normalize_usage
from storage import Storage


def utc(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class PricingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = PricingCatalog()
        self.usage = {"input": 236, "cacheRead": 2304, "cacheWrite": 0,
                      "output": 124, "reasoning": 10}

    def test_peak_uses_cache_miss_hit_and_output_without_double_counting_reasoning(self) -> None:
        result = self.catalog.price("deepseek", "deepseek-v4-flash", self.usage,
                                    utc("2026-09-24T02:00:00"))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.tier, "peak")
        self.assertEqual(Decimal(result.usd), Decimal("0.000233424"))
        self.assertEqual(json.loads(result.snapshot)["model"], "deepseek-flash")

    def test_weekend_and_chinese_holiday_use_offpeak_rate(self) -> None:
        for date in ("2026-09-26T02:00:00", "2026-10-01T02:00:00"):
            result = self.catalog.price("deepseek", "deepseek-flash", self.usage, utc(date))
            self.assertEqual(result.tier, "off_peak")
            self.assertEqual(Decimal(result.usd), Decimal("0.000116712"))

    def test_unknown_model_and_expired_calendar_are_not_silently_priced(self) -> None:
        self.assertEqual(self.catalog.price("other", "deepseek-flash", self.usage,
                                             utc("2026-09-24T02:00:00")).status, "missing_rule")
        self.assertEqual(self.catalog.price("deepseek", "future-model", self.usage,
                                             utc("2026-09-24T02:00:00")).status, "missing_rule")
        self.assertEqual(self.catalog.price("deepseek", "deepseek-flash", self.usage,
                                             utc("2027-01-01T02:00:00")).status, "outside_effective_period")

    def test_flat_provider_prices_any_time_and_free_cache_write(self) -> None:
        usage = {"input": 236, "cacheRead": 2304, "cacheWrite": 5,
                 "output": 124, "reasoning": 10}
        # mimo-v2.6-flash 无峰谷，缓存写入限时免费：任意时刻、含 cacheWrite 都能计价。
        for date in ("2026-10-07T02:00:00", "2026-10-10T22:00:00"):
            result = self.catalog.price("xiaomi", "mimo-v2.6-flash", usage, utc(date))
            self.assertEqual(result.status, "priced")
            self.assertIsNone(result.tier)
            self.assertEqual(Decimal(result.usd), Decimal("0.0000742112"))
        snapshot = json.loads(result.snapshot)
        self.assertEqual(snapshot["model"], "mimo-v2.6-flash")
        self.assertIsNone(snapshot["tier"])

    def test_updated_price_file_is_read_without_code_change(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads(Path(self.catalog.path).read_text(encoding="utf-8"))
            path = Path(folder) / "pricing.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            catalog = PricingCatalog(str(path))
            first = catalog.price("deepseek", "deepseek-flash", self.usage, utc("2026-09-24T02:00:00"))
            config["providers"]["deepseek"]["revision"] = "new-revision"
            config["providers"]["deepseek"]["models"]["deepseek-flash"]["peak"]["output"] = "2.40"
            path.write_text(json.dumps(config), encoding="utf-8")
            second = catalog.price("deepseek", "deepseek-flash", self.usage, utc("2026-09-24T02:00:00"))
            self.assertGreater(Decimal(second.usd), Decimal(first.usd))
            self.assertEqual(json.loads(second.snapshot)["revision"], "new-revision")
            config["per_tokens"] = 0
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(catalog.price("deepseek", "deepseek-flash", self.usage,
                                           utc("2026-09-24T02:00:00")).status, "invalid_config")


class CostReportModelLineTests(unittest.TestCase):
    """报告中的模型行：单模型、跨轮多模型拼接、无用量时跳过、过期文案指向实际 provider。"""

    def _run(self, storage: Storage, events, price: PriceResult | None = None):
        run_id = storage.start_usage_run(42, None, 123, None)
        for seq, (provider, model) in enumerate(events, 1):
            storage.record_usage_event(run_id, seq, {
                "kind": "assistant", "occurred_at": utc("2026-09-24T02:00:00"),
                "provider": provider, "model": model,
                "usage": {"input": 2, "cacheRead": 3, "output": 4, "reasoning": 1},
            }, price or PriceResult("priced", "0.0005", "peak"))
        storage.finish_usage_run(run_id, "complete", None)
        return storage.get_last_usage_run(42)

    def test_single_model_line_sits_between_title_and_input(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                run = self._run(storage, [("deepseek", "deepseek-flash")])
                lines = _cost_report(storage, [run], title="📊 本轮用量").splitlines()
                self.assertEqual(lines[0], "📊 本轮用量")
                self.assertEqual(lines[1], "模型：deepseek/deepseek-flash")
                self.assertTrue(lines[2].startswith("输入："))

    def test_same_run_duplicates_are_deduped_in_first_seen_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                run = self._run(storage, [
                    ("xiaomi", "mimo-v2.6-flash"),
                    ("deepseek", "deepseek-flash"),
                    ("xiaomi", "mimo-v2.6-flash"),
                ])
                report = _cost_report(storage, [run], title="test")
                self.assertIn("模型：xiaomi/mimo-v2.6-flash、deepseek/deepseek-flash", report)

    def test_several_runs_are_joined_for_date_range_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                first = self._run(storage, [("deepseek", "deepseek-flash")])
                second = self._run(storage, [("xiaomi", "mimo-v2.6-flash")])
                report = _cost_report(storage, [first, second], title="test")
                self.assertIn("模型：deepseek/deepseek-flash、xiaomi/mimo-v2.6-flash", report)

    def test_model_line_is_skipped_when_no_usage_event_arrived(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                run_id = storage.start_usage_run(42, None, None, None)
                storage.finish_usage_run(run_id, "complete", None)
                report = _cost_report(storage, [storage.get_last_usage_run(42)], title="test")
                self.assertNotIn("模型：", report)
                self.assertIn("尚未收到模型用量", report)

    def test_stale_warning_names_real_providers_instead_of_deepseek(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                run = self._run(
                    storage, [("xiaomi", "mimo-v2.6-flash")],
                    price=PriceResult("stale_price", "0.001", "peak", '{"revision":"r"}'),
                )
                report = _cost_report(storage, [run], title="test")
                self.assertIn("费率文件已到复核日期", report)
                self.assertIn("请核对 xiaomi 官方价格", report)
                self.assertNotIn("DeepSeek", report)


class UsageLedgerTests(unittest.TestCase):
    def test_persists_price_snapshot_and_flags_unaccounted_stats(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                self._check_persisted_snapshot(storage)

    def _check_persisted_snapshot(self, storage: Storage) -> None:
        run_id = storage.start_usage_run(42, None, 123, {"tokens": {"input": 0, "output": 0,
                "cacheRead": 0, "cacheWrite": 0}})
        event = {"kind": "assistant", "occurred_at": utc("2026-09-24T02:00:00"),
                 "provider": "deepseek", "model": "deepseek-flash", "usage": {
                     "input": 2, "cacheRead": 3, "output": 4, "reasoning": 1}}
        price = PricingCatalog().price(event["provider"], event["model"],
                                       event["usage"], event["occurred_at"])
        storage.record_usage_event(run_id, 1, event, price)
        storage.finish_usage_run(run_id, "complete", {"tokens": {"input": 3, "output": 4,
            "cacheRead": 3, "cacheWrite": 0}})
        row = storage.get_usage_events(run_id)[0]
        self.assertEqual(row["total_tokens"], 9)
        self.assertEqual(row["price_snapshot"], price.snapshot)
        report = _cost_report(storage, [storage.get_last_usage_run(42)], title="test")
        self.assertIn("pi 会话统计高于已记录事件", report)
        self.assertIn("缓存命中 3", report)

    def test_user_report_mode_persists_and_compact_report_is_clear(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "bot.db")
            storage = Storage(path)
            storage.set_user_cost_report_mode(42, "compact")
            run_id = storage.start_usage_run(42, None, 123, None)
            storage.record_usage_event(
                run_id, 1,
                {"kind": "assistant", "occurred_at": 1, "provider": "deepseek",
                 "model": "deepseek-flash", "usage": {"input": 2, "cacheRead": 3, "output": 4}},
                PriceResult("priced", "0.0005", "peak"),
            )
            storage.finish_usage_run(run_id, "complete", None)
            report = _compact_cost_report(storage, [storage.get_last_usage_run(42)], title="test")
            self.assertIn("输入 5", report)
            self.assertIn("输出 4", report)
            self.assertIn("$0.0005", report)
            self.assertLess(len(report), 100)
            storage._conn.close()
            reopened = Storage(path)
            self.assertEqual(reopened.get_user_cost_report_mode(42), "compact")
            with self.assertRaises(ValueError):
                reopened.set_user_cost_report_mode(42, "invalid")
            reopened._conn.close()

    def test_unknown_price_is_visible_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            with closing(storage._conn):
                run_id = storage.start_usage_run(42, None, None, None)
                storage.record_usage_event(run_id, 1, {"kind": "assistant", "occurred_at": 1,
                    "provider": "other", "model": "model", "usage": {"input": 1, "output": 1}},
                    PriceResult("missing_rule"))
                storage.finish_usage_run(run_id, "complete", None)
                report = _cost_report(storage, [storage.get_last_usage_run(42)], title="test")
                self.assertIn("1 次模型调用未计价", report)




class CostCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_mode_is_per_user_and_can_be_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "bot.db"))
            update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
            context = SimpleNamespace(
                args=["auto"],
                bot_data={"settings": SimpleNamespace(cost_tracking_enabled=True, cost_auto_report=True),
                          "storage": storage},
            )
            with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
                await bot.cmd_cost(update, context)
                self.assertIn("full（全局默认）", reply.await_args.args[1])
                context.args = ["auto", "compact"]
                await bot.cmd_cost(update, context)
                self.assertEqual(storage.get_user_cost_report_mode(7), "compact")
                context.args = ["auto"]
                await bot.cmd_cost(update, context)
                self.assertIn("compact（个人设置）", reply.await_args.args[1])
            storage._conn.close()


if __name__ == "__main__":
    unittest.main()
