"""用 pi 提供商用量和可更新的价格文件估算费用。"""

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PriceResult:
    status: str
    usd: str | None = None
    tier: str | None = None
    snapshot: str | None = None


def _utc_time(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        seconds = value / 1000 if value > 100_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("无效时间")
    return hour * 60 + minute


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("无效 token 数")
    return value


def normalize_usage(value: dict) -> dict[str, int]:
    """pi 的 input 已扣除 cacheRead；reasoning 是 output 的子集。"""
    if not isinstance(value, dict):
        raise ValueError("缺少用量")
    counts = {
        "input": _count(value.get("input")),
        "cacheRead": _count(value.get("cacheRead", 0)),
        "cacheWrite": _count(value.get("cacheWrite", 0)),
        "output": _count(value.get("output")),
        "reasoning": _count(value.get("reasoning", 0)),
    }
    counts["totalTokens"] = sum(counts[key] for key in ("input", "cacheRead", "cacheWrite", "output"))
    if counts["reasoning"] > counts["output"]:
        raise ValueError("推理 token 超过输出 token")
    return counts


class PricingCatalog:
    def __init__(self, path: str = "pricing.json"):
        candidate = Path(path)
        self.path = candidate if candidate.is_absolute() else Path(__file__).resolve().parent / candidate

    def price(self, provider: str, model: str, usage: dict, at: Any = None) -> PriceResult:
        try:
            counts = normalize_usage(usage)
        except ValueError:
            return PriceResult("invalid_usage")
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return PriceResult("missing_config")
        try:
            if config["currency"] != "USD" or config["provider"] != provider:
                return PriceResult("missing_rule")
            model_id = config.get("aliases", {}).get(model, model)
            rule = config["models"].get(model_id)
            if rule is None:
                return PriceResult("missing_rule")
            moment = _utc_time(at)
            if not (_parse_time(config["effective_from"]) <= moment < _parse_time(config["effective_until"])):
                return PriceResult("outside_effective_period")
            schedule = config["peak_schedule"]
            china_day = (moment + timedelta(minutes=int(schedule["china_offset_minutes"]))).date()
            if china_day.isoformat() > schedule["holiday_calendar_through"]:
                return PriceResult("calendar_unknown")
            now_minute = moment.hour * 60 + moment.minute
            peak = (
                moment.weekday() in schedule["weekdays_utc"]
                and china_day.isoformat() not in schedule["china_holidays"]
                and any(_minutes(start) <= now_minute < _minutes(end) for start, end in schedule["windows_utc"])
            )
            tier = "peak" if peak else "off_peak"
            rates = rule[tier]
            if counts["cacheWrite"] and "cache_write" not in rates:
                return PriceResult("missing_rule")
            components = {
                "input": counts["input"] * Decimal(rates["cache_miss"]),
                "cacheRead": counts["cacheRead"] * Decimal(rates["cache_hit"]),
                "output": counts["output"] * Decimal(rates["output"]),
                "cacheWrite": counts["cacheWrite"] * Decimal(rates.get("cache_write", "0")),
            }
            if any(Decimal(rate) < 0 for rate in rates.values()):
                raise ValueError("负价格")
            denominator = Decimal(config["per_tokens"])
            if not denominator.is_finite() or denominator <= 0 or any(
                not Decimal(rate).is_finite() for rate in rates.values()
            ):
                raise ValueError("无效价格或计价单位")
            amount = sum(components.values()) / denominator
            stale = moment >= _parse_time(config["review_after"])
            snapshot = json.dumps({
                "revision": config["revision"], "source_url": config["source_url"],
                "checked_at": config["checked_at"], "model": model_id, "tier": tier,
                "rates_per_million": rates, "per_tokens": config["per_tokens"],
            }, ensure_ascii=False, sort_keys=True)
            return PriceResult("stale_price" if stale else "priced", str(amount), tier, snapshot)
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, ArithmeticError):
            return PriceResult("invalid_config")


def format_usd(value: str | Decimal) -> str:
    amount = Decimal(value)
    return f"${amount:.12f}".rstrip("0").rstrip(".")
