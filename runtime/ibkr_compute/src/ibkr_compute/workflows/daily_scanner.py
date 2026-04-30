"""
每日标的扫描器

当前实现改为 09:20 ET 的日内波动型初筛：
  1. 读取 trade watchlist
  2. 复用 screener 聚合得到量能 / 波动指标
  3. 保留 multi-TF 技术投票方向
  4. 叠加 avg_10d_volume / premarket_volume / atr_pct / day_change_pct 的质量门
  5. 根据 WS 订阅预算把通过标的写成 active / candidate
"""

from __future__ import annotations

import json
import time
from typing import Any

from ibkr_compute.api.compute.runtime_state.universe import get_market_monitor_symbols
from ibkr_compute.api.market.screener.payload import build_screener_payload
from ibkr_compute.api.market.screener.runtime import get_api_app
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
DAILY_SCAN_SOURCE = "daily_scan"
DAILY_SCAN_STAGE = "daily_scan_0920"
DAILY_SCAN_PRIMARY_WEIGHT = 2
DAILY_SCAN_SECONDARY_WEIGHT = 1
DAILY_SCAN_READY_TIMEFRAME_BONUS = 1
DEFAULT_SCAN_TIME_ET = "09:20"
DEFAULT_MIN_AVG_10D_VOLUME = 100_000
DEFAULT_MIN_ATR_PCT = 0.15
DEFAULT_MIN_ABS_DAY_CHANGE_PCT = 1.0
DEFAULT_MIN_PREMARKET_VOLUME = 5_000
MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}

REJECTION_BUCKET_NO_SNAPSHOT = "no_snapshot"
REJECTION_BUCKET_VOTE_TIE = "vote_tie"
REJECTION_BUCKET_AVG_10D = "avg_10d_volume_below_threshold"
REJECTION_BUCKET_PREMARKET = "premarket_volume_below_threshold"
REJECTION_BUCKET_ATR = "atr_pct_below_threshold"
REJECTION_BUCKET_DAY_CHANGE = "day_change_below_threshold"
REJECTION_BUCKET_DATA_INCOMPLETE = "data_incomplete_repairing"

DAILY_SCAN_LONG_PRIMARY_RULES = (
    ("trend_dir", 1, "trend_dir=1"),
    ("dtp_dir", 1, "dtp_dir=1"),
    ("ema_bullish", True, "ema_bullish=true"),
)
DAILY_SCAN_SHORT_PRIMARY_RULES = (
    ("trend_dir", -1, "trend_dir=-1"),
    ("dtp_dir", -1, "dtp_dir=-1"),
    ("ema_bearish", True, "ema_bearish=true"),
)
DAILY_SCAN_LONG_SECONDARY_RULES = (
    ("fractal_bull", True, "fractal_bull=true"),
    ("crsi_os", True, "crsi_os=true"),
    ("sd_lower", True, "sd_lower=true"),
)
DAILY_SCAN_SHORT_SECONDARY_RULES = (
    ("fractal_bear", True, "fractal_bear=true"),
    ("crsi_ob", True, "crsi_ob=true"),
    ("sd_upper", True, "sd_upper=true"),
)
DAILY_SCAN_REASON_RULES = (
    ("fractal_bull", "fractal_bull"),
    ("fractal_bear", "fractal_bear"),
    ("ema_bullish", "ema_bullish"),
    ("ema_bearish", "ema_bearish"),
    ("sd_lower", "sd_lower"),
    ("sd_upper", "sd_upper"),
)


def _daily_scan_rule_matches(snapshot: dict, rule: tuple[str, object, str]) -> bool:
    key, expected, _ = rule
    value = snapshot.get(key)
    if isinstance(expected, bool):
        return bool(value) is expected
    return value == expected


def _daily_scan_matches_any(snapshot: dict, rules) -> bool:
    return any(_daily_scan_rule_matches(snapshot, rule) for rule in rules)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_extra(row: dict | None) -> dict:
    payload = (row or {}).get("extra")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _target_row_is_manual(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source.startswith("manual_"):
        return True
    return source in MANUAL_TARGET_SOURCES


def _format_threshold(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _format_metric_value(value: float | int) -> str:
    return _format_threshold(value)


def _new_rejection_trackers() -> tuple[dict[str, int], dict[str, list[dict[str, str]]]]:
    return {}, {}


def _record_rejection(
    summary: dict[str, int],
    examples: dict[str, list[dict[str, str]]],
    *,
    bucket: str,
    symbol: str,
    actual: str = "",
    threshold: str = "",
    note: str = "",
    limit: int = 3,
) -> None:
    normalized_bucket = str(bucket or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_bucket or not normalized_symbol:
        return
    summary[normalized_bucket] = int(summary.get(normalized_bucket, 0) or 0) + 1
    bucket_examples = examples.setdefault(normalized_bucket, [])
    if len(bucket_examples) >= max(1, int(limit or 0)):
        return
    bucket_examples.append(
        {
            "bucket": normalized_bucket,
            "symbol": normalized_symbol,
            "actual": str(actual or "").strip(),
            "threshold": str(threshold or "").strip(),
            "note": str(note or "").strip(),
        }
    )


def _flatten_rejection_examples(examples: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    rows = []
    for bucket in sorted(examples.keys()):
        rows.extend(examples.get(bucket) or [])
    return rows


def _metric_rank_bonus(row: dict) -> int:
    avg_10d_volume = _safe_float(row.get("avg_10d_volume"))
    premarket_volume = _safe_float(row.get("premarket_volume"))
    atr_pct = abs(_safe_float(row.get("atr_pct")))
    day_change_pct = abs(_safe_float(row.get("day_change_pct")))
    bonus = 0

    if avg_10d_volume >= 1_000_000:
        bonus += 5
    elif avg_10d_volume >= 500_000:
        bonus += 3
    elif avg_10d_volume >= DEFAULT_MIN_AVG_10D_VOLUME:
        bonus += 1

    if premarket_volume >= 30_000:
        bonus += 5
    elif premarket_volume >= 10_000:
        bonus += 3
    elif premarket_volume >= DEFAULT_MIN_PREMARKET_VOLUME:
        bonus += 1

    if atr_pct >= 0.30:
        bonus += 5
    elif atr_pct >= 0.20:
        bonus += 3
    elif atr_pct >= DEFAULT_MIN_ATR_PCT:
        bonus += 1

    if day_change_pct >= 4.0:
        bonus += 5
    elif day_change_pct >= 2.0:
        bonus += 3
    elif day_change_pct >= DEFAULT_MIN_ABS_DAY_CHANGE_PCT:
        bonus += 1

    return bonus


def _load_scan_settings(environment: str) -> dict:
    api_app = get_api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    api_app.cfg.refresh()
    monitor_count = len(get_market_monitor_symbols(runtime_environment))
    target_limit_raw = api_app.cfg.get_int_for_environment(
        "ibkr_target_subscription_limit",
        runtime_environment,
        80,
    )
    total_limit_raw = api_app.cfg.get_int_for_environment(
        "ibkr_total_subscription_limit",
        runtime_environment,
        80,
    )
    target_limit = max(0, int(target_limit_raw or 0))
    total_limit = max(0, int(total_limit_raw or 0))
    trade_budget: int | None = target_limit if target_limit > 0 else None
    if total_limit > 0:
        total_budget = max(0, total_limit - monitor_count)
        trade_budget = total_budget if trade_budget is None else min(trade_budget, total_budget)

    return {
        "scan_time_et": str(
            api_app.cfg.get_for_environment(
                "ibkr_daily_scan_time_et",
                runtime_environment,
                DEFAULT_SCAN_TIME_ET,
            )
            or DEFAULT_SCAN_TIME_ET
        ).strip()
        or DEFAULT_SCAN_TIME_ET,
        "min_avg_10d_volume": max(
            0,
            _safe_int(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_avg_10d_volume",
                    runtime_environment,
                    str(DEFAULT_MIN_AVG_10D_VOLUME),
                ),
                DEFAULT_MIN_AVG_10D_VOLUME,
            ),
        ),
        "min_atr_pct": max(
            0.0,
            _safe_float(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_atr_pct",
                    runtime_environment,
                    str(DEFAULT_MIN_ATR_PCT),
                ),
                DEFAULT_MIN_ATR_PCT,
            ),
        ),
        "min_abs_day_change_pct": max(
            0.0,
            _safe_float(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_abs_day_change_pct",
                    runtime_environment,
                    str(DEFAULT_MIN_ABS_DAY_CHANGE_PCT),
                ),
                DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
            ),
        ),
        "min_premarket_volume": max(
            0,
            _safe_int(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_premarket_volume",
                    runtime_environment,
                    str(DEFAULT_MIN_PREMARKET_VOLUME),
                ),
                DEFAULT_MIN_PREMARKET_VOLUME,
            ),
        ),
        "monitor_count": monitor_count,
        "target_subscription_limit": target_limit,
        "total_subscription_limit": total_limit,
        "trade_subscription_budget": trade_budget,
    }


def build_daily_scan_rule_summary(environment: str | None = None) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    settings = _load_scan_settings(runtime_environment)
    trade_budget = settings["trade_subscription_budget"]
    budget_label = "unlimited" if trade_budget is None else str(int(trade_budget))
    return {
        "primary_weight": DAILY_SCAN_PRIMARY_WEIGHT,
        "secondary_weight": DAILY_SCAN_SECONDARY_WEIGHT,
        "ready_timeframe_bonus": DAILY_SCAN_READY_TIMEFRAME_BONUS,
        "long_primary": [label for _, _, label in DAILY_SCAN_LONG_PRIMARY_RULES],
        "short_primary": [label for _, _, label in DAILY_SCAN_SHORT_PRIMARY_RULES],
        "long_secondary": [label for _, _, label in DAILY_SCAN_LONG_SECONDARY_RULES],
        "short_secondary": [label for _, _, label in DAILY_SCAN_SHORT_SECONDARY_RULES],
        "tie_behavior": "long_votes == short_votes => direction_bias=neutral, score=0",
        "final_bonus": "direction_bias 非 neutral 时额外加上 ready_timeframes_count",
        "reason_fields": [label for _, label in DAILY_SCAN_REASON_RULES],
        "scan_time_et": settings["scan_time_et"],
        "quality_gates": {
            "avg_10d_volume_gte": settings["min_avg_10d_volume"],
            "atr_pct_gte": settings["min_atr_pct"],
            "abs_day_change_pct_gte": settings["min_abs_day_change_pct"],
            "premarket_volume_gte": settings["min_premarket_volume"],
        },
        "subscription_budget": {
            "trade_budget": budget_label,
            "total_limit": int(settings["total_subscription_limit"] or 0),
            "monitor_count": int(settings["monitor_count"] or 0),
        },
    }


class DailyScanner:
    def __init__(self, pb_client: PBClient, engines: dict):
        self.pb_client = pb_client
        self.engines = engines
        self.api_app = get_api_app()

    def _data_completeness_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return hasattr(self.api_app, "bar_freshness_planner")
        try:
            return bool(cfg.get_bool_for_environment("ibkr_daily_scan_data_completeness_enabled", environment, True))
        except Exception:
            return hasattr(self.api_app, "bar_freshness_planner")

    def _data_completeness_intervals(self, environment: str) -> list[str]:
        cfg = getattr(self.api_app, "cfg", None)
        raw = "5m,15m,30m,1h,4h,1d"
        if cfg is not None and hasattr(cfg, "get_for_environment"):
            try:
                raw = str(cfg.get_for_environment("ibkr_daily_scan_data_completeness_intervals", environment, raw) or raw)
            except Exception:
                raw = "5m,15m,30m,1h,4h,1d"
        parsed = [normalize_interval(item) for item in raw.split(",") if str(item or "").strip()]
        parsed = [item for item in dict.fromkeys(parsed) if item in COMPUTE_INTERVALS]
        return parsed or list(COMPUTE_INTERVALS)

    def _build_data_completeness_gate(self, environment: str, symbols: list[str]) -> dict:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols or not self._data_completeness_enabled(environment):
            return {"enabled": False, "items": {}, "incomplete_symbols": [], "repair_jobs": []}

        planner = getattr(self.api_app, "bar_freshness_planner", None)
        if planner is None:
            planner = BarFreshnessPlanner(self.pb_client, getattr(self.api_app, "cfg", None), environment=environment)
        intervals = self._data_completeness_intervals(environment)
        items = {}
        incomplete_symbols = []
        repair_jobs = []
        coordinator = getattr(self.api_app, "bar_repair_coordinator", None)
        for symbol in normalized_symbols:
            freshness = planner.plan_symbol(symbol, intervals, environment=environment, required_bars=0)
            items[symbol] = freshness
            if not bool(freshness.get("needs_repair")):
                continue
            incomplete_symbols.append(symbol)
            if coordinator is not None and hasattr(coordinator, "enqueue_from_freshness"):
                try:
                    repair_jobs.extend(
                        coordinator.enqueue_from_freshness(
                            freshness,
                            priority="daily_scan",
                            trigger="daily_scan_data_completeness",
                        )
                    )
                except Exception as exc:
                    repair_jobs.append({"queued": False, "symbol": symbol, "error": str(exc)})
        return {
            "enabled": True,
            "intervals": intervals,
            "items": items,
            "incomplete_symbols": incomplete_symbols,
            "repairing_symbols": incomplete_symbols,
            "repair_jobs": repair_jobs,
            "status": "ready" if not incomplete_symbols else "repairing",
        }

    def run_scan(self, date: str, environments=None) -> dict:
        """
        执行每日自动筛选。

        返回聚合统计，active / candidate 已经按订阅预算写入。
        """
        scanned = 0
        candidates = 0
        active = 0
        removed = 0
        errors = 0
        environment_results = []
        eligible = 0
        rejection_summary: dict[str, int] = {}
        rejection_examples: list[dict[str, str]] = []
        data_completeness = {
            "enabled": False,
            "status": "unknown",
            "excluded_incomplete_count": 0,
            "repairing_count": 0,
            "incomplete_symbols": [],
            "repair_jobs": [],
        }

        runtime_environments = environments or ["live", "paper"]

        for environment in runtime_environments:
            result = self._run_environment_scan(date, environment)
            environment_results.append(result)
            scanned += int(result.get("scanned", 0) or 0)
            eligible += int(result.get("eligible", 0) or 0)
            candidates += int(result.get("candidates", 0) or 0)
            active += int(result.get("active", 0) or 0)
            removed += int(result.get("removed", 0) or 0)
            errors += int(result.get("errors", 0) or 0)
            for bucket, count in (result.get("rejection_summary") or {}).items():
                normalized_bucket = str(bucket or "").strip()
                if not normalized_bucket:
                    continue
                rejection_summary[normalized_bucket] = (
                    int(rejection_summary.get(normalized_bucket, 0) or 0)
                    + int(count or 0)
                )
            for example in result.get("rejection_examples") or []:
                if len(rejection_examples) >= 12:
                    break
                normalized = {
                    "bucket": str((example or {}).get("bucket") or "").strip(),
                    "symbol": str((example or {}).get("symbol") or "").strip().upper(),
                    "actual": str((example or {}).get("actual") or "").strip(),
                    "threshold": str((example or {}).get("threshold") or "").strip(),
                    "note": str((example or {}).get("note") or "").strip(),
                }
                if not normalized["bucket"] or not normalized["symbol"]:
                    continue
                if environment and len(runtime_environments) > 1:
                    normalized["environment"] = str(environment).strip().lower()
                rejection_examples.append(normalized)
            completeness = result.get("data_completeness") or {}
            if completeness:
                data_completeness["enabled"] = bool(data_completeness.get("enabled")) or bool(completeness.get("enabled"))
                data_completeness["excluded_incomplete_count"] += int(completeness.get("excluded_incomplete_count", 0) or 0)
                data_completeness["repairing_count"] += int(completeness.get("repairing_count", 0) or 0)
                data_completeness["incomplete_symbols"].extend(completeness.get("incomplete_symbols") or [])
                data_completeness["repair_jobs"].extend(completeness.get("repair_jobs") or [])

        data_completeness["incomplete_symbols"] = sorted(set(data_completeness.get("incomplete_symbols") or []))
        data_completeness["status"] = "repairing" if data_completeness["incomplete_symbols"] else "ready"

        return {
            "scanned": scanned,
            "eligible": eligible,
            "candidates": candidates,
            "active": active,
            "removed": removed,
            "errors": errors,
            "rejection_summary": rejection_summary,
            "rejection_examples": rejection_examples,
            "data_completeness": data_completeness,
            "excluded_incomplete_count": int(data_completeness.get("excluded_incomplete_count", 0) or 0),
            "environment_results": environment_results,
        }

    def _run_environment_scan(self, date: str, environment: str) -> dict:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        settings = _load_scan_settings(runtime_environment)
        watchlist = self._get_watchlist(runtime_environment)
        watchlist_symbols = sorted(
            {
                str(item.get("symbol", "")).strip().upper()
                for item in watchlist
                if str(item.get("symbol", "")).strip()
            }
        )
        completeness_gate = self._build_data_completeness_gate(runtime_environment, watchlist_symbols)
        incomplete_symbols = set(completeness_gate.get("incomplete_symbols") or [])
        metric_rows = self._build_metric_rows(date, runtime_environment, watchlist_symbols)
        existing_rows = self._load_today_target_rows(date, runtime_environment)
        manual_rows = {
            str(row.get("symbol", "")).strip().upper(): row
            for row in existing_rows
            if _target_row_is_manual(row)
        }
        manual_retained_symbols = set(manual_rows.keys())
        manual_active_count = sum(
            1
            for row in manual_rows.values()
            if str(row.get("status", "")).strip().lower() == "active"
        )

        eligible = []
        errors = 0
        rejection_summary, rejection_examples_by_bucket = _new_rejection_trackers()
        for item in watchlist:
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if symbol in incomplete_symbols:
                freshness = (completeness_gate.get("items") or {}).get(symbol) or {}
                stale_intervals = list(freshness.get("needs_repair_intervals") or [])
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_DATA_INCOMPLETE,
                    symbol=symbol,
                    actual=",".join(stale_intervals) or str(freshness.get("status") or "incomplete"),
                    threshold="all required intervals ready",
                    note="数据不完整，已排除本轮筛选并进入异步 API 补偿",
                )
                continue
            try:
                result = self.evaluate_symbol(
                    symbol,
                    date,
                    runtime_environment,
                    metrics=metric_rows.get(symbol) or {},
                    settings=settings,
                )
                if result and bool(result.get("quality_gate_passed")):
                    result["exchange"] = str(item.get("exchange", "") or result.get("exchange", "")).strip().upper()
                    eligible.append(result)
                else:
                    for example in (result or {}).get("rejection_examples") or []:
                        _record_rejection(
                            rejection_summary,
                            rejection_examples_by_bucket,
                            bucket=str((example or {}).get("bucket") or ""),
                            symbol=str((example or {}).get("symbol") or symbol),
                            actual=str((example or {}).get("actual") or ""),
                            threshold=str((example or {}).get("threshold") or ""),
                            note=str((example or {}).get("note") or ""),
                        )
            except Exception as exc:
                errors += 1
                print(f"[Scanner] {runtime_environment}/{symbol} error: {exc}")

        eligible.sort(
            key=lambda item: (
                -_safe_float(item.get("technical_score")),
                -abs(_safe_float(item.get("day_change_pct"))),
                -_safe_float(item.get("premarket_volume")),
                -_safe_float(item.get("avg_10d_volume")),
                -_safe_float(item.get("atr_pct")),
                str(item.get("symbol", "")).strip().upper(),
            )
        )

        trade_budget = settings["trade_subscription_budget"]
        if trade_budget is None:
            auto_active_budget = len(eligible)
        else:
            auto_active_budget = max(0, int(trade_budget) - manual_active_count)

        retained_symbols = set(manual_retained_symbols)
        active_symbols = {
            symbol
            for symbol, row in manual_rows.items()
            if str(row.get("status", "")).strip().lower() == "active"
        }
        active_count = 0
        candidate_count = 0

        auto_rank = 0
        for result in eligible:
            symbol = str(result.get("symbol", "")).strip().upper()
            if not symbol or symbol in manual_retained_symbols:
                continue

            auto_rank += 1
            status = "active" if auto_rank <= auto_active_budget else "candidate"
            retained_symbols.add(symbol)
            if status == "active":
                active_symbols.add(symbol)
                active_count += 1
            else:
                candidate_count += 1

            extra = {
                **(result.get("extra") or {}),
                "environment": runtime_environment,
                "source": DAILY_SCAN_SOURCE,
                "scan_stage": DAILY_SCAN_STAGE,
                "technical_score": round(_safe_float(result.get("technical_score")), 3),
                "avg_10d_volume": round(_safe_float(result.get("avg_10d_volume")), 2),
                "premarket_volume": round(_safe_float(result.get("premarket_volume")), 2),
                "atr_pct": round(_safe_float(result.get("atr_pct")), 4),
                "day_change_pct": round(_safe_float(result.get("day_change_pct")), 2),
                "subscription_rank": len(active_symbols) if status == "active" else 0,
                "within_subscription_budget": status == "active",
            }
            self.pb_client.upsert_scan(
                {
                    "environment": runtime_environment,
                    "symbol": symbol,
                    "exchange": str(result.get("exchange", "") or "").strip().upper(),
                    "date": date,
                    "direction_bias": result.get("direction_bias", "neutral"),
                    "score": round(_safe_float(result.get("score")), 3),
                    "scan_reason": result.get("reason", ""),
                    "status": status,
                    "extra": extra,
                }
            )

        removed = self._reconcile_removed_targets(
            date=date,
            environment=runtime_environment,
            retained_symbols=retained_symbols,
        )

        return {
            "environment": runtime_environment,
            "scan_stage": DAILY_SCAN_STAGE,
            "scanned": len(watchlist_symbols),
            "eligible": len([item for item in eligible if str(item.get("symbol", "")).strip().upper() not in manual_retained_symbols]),
            "candidates": candidate_count + sum(
                1
                for row in manual_rows.values()
                if str(row.get("status", "")).strip().lower() == "candidate"
            ),
            "active": active_count + manual_active_count,
            "removed": removed,
            "errors": errors,
            "trade_subscription_budget": trade_budget,
            "manual_active_count": manual_active_count,
            "manual_retained_count": len(manual_retained_symbols),
            "data_completeness": {
                "enabled": bool(completeness_gate.get("enabled")),
                "status": str(completeness_gate.get("status") or ("ready" if not incomplete_symbols else "repairing")),
                "intervals": list(completeness_gate.get("intervals") or []),
                "excluded_incomplete_count": len(incomplete_symbols),
                "repairing_count": len(incomplete_symbols),
                "incomplete_symbols": sorted(incomplete_symbols),
                "repair_jobs": list(completeness_gate.get("repair_jobs") or []),
            },
            "rejection_summary": rejection_summary,
            "rejection_examples": _flatten_rejection_examples(rejection_examples_by_bucket),
        }

    def evaluate_symbol(
        self,
        symbol: str,
        date: str,
        environment: str,
        *,
        metrics: dict | None = None,
        settings: dict | None = None,
    ) -> dict | None:
        """
        单标的评分：
        先看 multi-TF 方向，再叠加日内波动型质量门。
        """
        del date
        runtime_environment = str(environment or "live").strip().lower() or "live"
        settings = settings or _load_scan_settings(runtime_environment)
        snapshots = {}
        for (engine_environment, sym, tf), engine in self.engines.items():
            if engine_environment == runtime_environment and sym == symbol and engine.is_ready():
                snapshots[tf] = engine.get_snapshot()

        if not snapshots:
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": "neutral",
                "reason": "no_snapshot",
                "quality_gate_passed": False,
                "rejection_examples": [
                    {
                        "bucket": REJECTION_BUCKET_NO_SNAPSHOT,
                        "symbol": symbol,
                        "actual": "ready_timeframes=0",
                        "threshold": ">=1 ready timeframe",
                        "note": "缺少可用技术快照",
                    }
                ],
                "extra": {
                    "environment": runtime_environment,
                    "timeframes_ready": [],
                    "long_votes": 0,
                    "short_votes": 0,
                },
            }

        technical_score = 0
        long_votes = 0
        short_votes = 0
        technical_reasons = []

        for tf, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_PRIMARY_RULES):
                long_votes += 1
                technical_score += DAILY_SCAN_PRIMARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_PRIMARY_RULES):
                short_votes += 1
                technical_score += DAILY_SCAN_PRIMARY_WEIGHT

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_SECONDARY_RULES):
                long_votes += 1
                technical_score += DAILY_SCAN_SECONDARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_SECONDARY_RULES):
                short_votes += 1
                technical_score += DAILY_SCAN_SECONDARY_WEIGHT

            for rule_key, rule_label in DAILY_SCAN_REASON_RULES:
                if snapshot.get(rule_key):
                    technical_reasons.append(f"{tf}:{rule_label}")

        if long_votes == short_votes:
            direction_bias = "neutral"
        elif long_votes > short_votes:
            direction_bias = "long"
        else:
            direction_bias = "short"

        if direction_bias == "neutral":
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": direction_bias,
                "reason": "vote_tie",
                "quality_gate_passed": False,
                "rejection_examples": [
                    {
                        "bucket": REJECTION_BUCKET_VOTE_TIE,
                        "symbol": symbol,
                        "actual": f"long_votes={long_votes}, short_votes={short_votes}",
                        "threshold": "long_votes != short_votes",
                        "note": "技术投票平票",
                    }
                ],
                "extra": {
                    "environment": runtime_environment,
                    "timeframes_ready": sorted(snapshots.keys()),
                    "long_votes": long_votes,
                    "short_votes": short_votes,
                },
            }

        technical_score += len(snapshots) * DAILY_SCAN_READY_TIMEFRAME_BONUS
        metric_row = dict(metrics or {})
        avg_10d_volume = _safe_float(metric_row.get("avg_10d_volume"))
        premarket_volume = _safe_float(metric_row.get("premarket_volume"))
        atr_pct = abs(_safe_float(metric_row.get("atr_pct")))
        day_change_pct = _safe_float(metric_row.get("day_change_pct"))
        quality_gate_passed = (
            avg_10d_volume >= _safe_float(settings.get("min_avg_10d_volume"), DEFAULT_MIN_AVG_10D_VOLUME)
            and premarket_volume >= _safe_float(settings.get("min_premarket_volume"), DEFAULT_MIN_PREMARKET_VOLUME)
            and atr_pct >= _safe_float(settings.get("min_atr_pct"), DEFAULT_MIN_ATR_PCT)
            and abs(day_change_pct) >= _safe_float(
                settings.get("min_abs_day_change_pct"),
                DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
            )
        )

        gate_reasons = [
            f"10d>={_format_threshold(settings['min_avg_10d_volume'])}",
            f"pre>={_format_threshold(settings['min_premarket_volume'])}",
            f"atr>={_format_threshold(settings['min_atr_pct'])}",
            f"|day|>={_format_threshold(settings['min_abs_day_change_pct'])}",
        ]
        rejection_examples = []
        if avg_10d_volume < _safe_float(settings.get("min_avg_10d_volume"), DEFAULT_MIN_AVG_10D_VOLUME):
            rejection_examples.append(
                {
                    "bucket": REJECTION_BUCKET_AVG_10D,
                    "symbol": symbol,
                    "actual": _format_metric_value(avg_10d_volume),
                    "threshold": _format_threshold(settings["min_avg_10d_volume"]),
                    "note": "10 日均量不足",
                }
            )
        if premarket_volume < _safe_float(settings.get("min_premarket_volume"), DEFAULT_MIN_PREMARKET_VOLUME):
            rejection_examples.append(
                {
                    "bucket": REJECTION_BUCKET_PREMARKET,
                    "symbol": symbol,
                    "actual": _format_metric_value(premarket_volume),
                    "threshold": _format_threshold(settings["min_premarket_volume"]),
                    "note": "盘前量不足",
                }
            )
        if atr_pct < _safe_float(settings.get("min_atr_pct"), DEFAULT_MIN_ATR_PCT):
            rejection_examples.append(
                {
                    "bucket": REJECTION_BUCKET_ATR,
                    "symbol": symbol,
                    "actual": _format_metric_value(atr_pct),
                    "threshold": _format_threshold(settings["min_atr_pct"]),
                    "note": "ATR 不足",
                }
            )
        if abs(day_change_pct) < _safe_float(
            settings.get("min_abs_day_change_pct"),
            DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
        ):
            rejection_examples.append(
                {
                    "bucket": REJECTION_BUCKET_DAY_CHANGE,
                    "symbol": symbol,
                    "actual": _format_metric_value(abs(day_change_pct)),
                    "threshold": _format_threshold(settings["min_abs_day_change_pct"]),
                    "note": "日内涨跌幅不足",
                }
            )
        final_score = technical_score + _metric_rank_bonus(metric_row)
        reason_items = list(dict.fromkeys(technical_reasons[:4] + gate_reasons))
        reason_text = ", ".join(reason_items[:8]).strip()
        if not reason_text:
            reason_text = (
                f"dir={direction_bias}, tech={technical_score}, pre={round(premarket_volume)}, "
                f"avg10d={round(avg_10d_volume)}, atr={round(atr_pct, 4)}, day={round(day_change_pct, 2)}"
            )

        return {
            "symbol": symbol,
            "score": round(final_score, 3),
            "technical_score": round(technical_score, 3),
            "direction_bias": direction_bias,
            "reason": reason_text,
            "quality_gate_passed": quality_gate_passed,
            "rejection_examples": rejection_examples,
            "exchange": str(metric_row.get("exchange", "") or "").strip().upper(),
            "avg_10d_volume": round(avg_10d_volume, 2),
            "premarket_volume": round(premarket_volume, 2),
            "atr_pct": round(atr_pct, 4),
            "day_change_pct": round(day_change_pct, 2),
            "extra": {
                "environment": runtime_environment,
                "timeframes_ready": sorted(snapshots.keys()),
                "long_votes": long_votes,
                "short_votes": short_votes,
            },
        }

    def _build_metric_rows(self, date: str, environment: str, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        payload = build_screener_payload(
            environment=environment,
            market_date=date,
            symbols=symbols,
            limit=0,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        rows = {}
        for item in items:
            symbol = str((item or {}).get("symbol", "")).strip().upper()
            if not symbol:
                continue
            rows[symbol] = dict(item)
        return rows

    def _load_today_target_rows(self, date: str, environment: str) -> list[dict]:
        try:
            return self.pb_client.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{date}" && '
                    f'environment = "{environment}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=20,
            )
        except Exception as exc:
            print(f"[Scanner] load targets error: {exc}")
            return []

    def _reconcile_removed_targets(self, *, date: str, environment: str, retained_symbols: set[str]) -> int:
        rows = self._load_today_target_rows(date, environment)
        removed = 0
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol or symbol in retained_symbols or _target_row_is_manual(row):
                continue
            record_id = str(row.get("id") or "").strip()
            if not record_id:
                continue
            extra = _safe_extra(row)
            next_extra = {
                **extra,
                "source": extra.get("source") or DAILY_SCAN_SOURCE,
                "scan_stage": DAILY_SCAN_STAGE,
                "removed_reason": "daily_scan_trim",
                "removed_at": int(time.time() * 1000),
            }
            try:
                self.pb_client.update_record(
                    "ibkr_targets",
                    record_id,
                    {
                        "status": "removed",
                        "extra": next_extra,
                    },
                )
                removed += 1
            except Exception as exc:
                print(f"[Scanner] mark removed error: {environment}/{symbol}: {exc}")
        return removed

    def _get_watchlist(self, environment: str) -> list:
        try:
            records = self.pb_client.get_records(
                "watchlist",
                filter=(
                    f'(environment = "{environment}" || environment = "global" || environment = "") '
                    f'&& (symbol_role = "{WATCHLIST_SYMBOL_ROLE_TRADE}" || symbol_role = "")'
                ),
                per_page=500,
            )
            merged = {}
            priority = {"": 0, "global": 1, environment: 2}
            applied = {}
            for item in records:
                symbol = str(item.get("symbol", "")).upper()
                if not symbol:
                    continue
                env = str(item.get("environment", "") or "").strip().lower()
                rank = priority.get(env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = item
            return list(merged.values())
        except Exception as exc:
            print(f"[Scanner] get watchlist error: {exc}")
            return []
