import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.workflows import daily_scanner as daily_scanner_mod
from ibkr_compute.workflows.daily_scanner import DailyScanner, REJECTION_BUCKET_DATA_INCOMPLETE


ET = ZoneInfo("America/New_York")


def _ms(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=ET).timestamp() * 1000)


class _FakePB:
    def __init__(self, rows=None, watchlist=None, targets=None):
        self.rows = list(rows or [])
        self.watchlist = list(watchlist or [])
        self.targets = list(targets or [])
        self.upserts = []

    def get_records(self, collection, **kwargs):
        if collection == "watchlist":
            return list(self.watchlist)
        if collection == "ibkr_bars":
            return self.get_all_records(collection, **kwargs)[: kwargs.get("per_page", 200)]
        return []

    def get_all_records(self, collection, **kwargs):
        if collection == "ibkr_targets":
            return list(self.targets)
        if collection != "ibkr_bars":
            return []
        text = str(kwargs.get("filter") or "")
        result = list(self.rows)
        if 'symbol = "SPY"' in text:
            result = [row for row in result if row.get("symbol") == "SPY"]
        if 'interval = "5m"' in text:
            result = [row for row in result if row.get("interval") == "5m"]
        elif 'interval = "15m"' in text:
            result = [row for row in result if row.get("interval") == "15m"]
        return sorted(result, key=lambda row: int(row.get("bar_time_ms", 0)), reverse=str(kwargs.get("sort")) == "-bar_time_ms")

    def upsert_scan(self, payload):
        self.upserts.append(dict(payload))


class _FakeCfgWithSqliteRead:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def get_bool_for_environment(self, key, _environment, default=False):
        if key == "ibkr_bar_direct_sqlite_read_enabled":
            return self.enabled
        return default

    def get_float_for_environment(self, _key, _environment, default=0.0):
        return default

    def get_int_for_environment(self, key, _environment, default=0):
        if key == "ibkr_official_5m_close_delay_sec":
            return default
        return default


class _FakeEngine:
    def is_ready(self):
        return True

    def get_snapshot(self):
        return {"ema_bullish": True}


class _FakeCfg:
    def __init__(self, *, blocking=False):
        self.blocking = blocking

    def get_bool_for_environment(self, key, environment, default=False):
        if key == "ibkr_daily_scan_data_completeness_blocking_enabled":
            return self.blocking
        return key == "ibkr_daily_scan_data_completeness_enabled"

    def get_float_for_environment(self, key, environment, default=0.0):
        return default

    def get_int_for_environment(self, key, environment, default=0):
        if key == "ibkr_daily_scan_runtime_topup_wait_sec":
            return 0
        return default

    def get_for_environment(self, key, environment, default=None):
        if key == "ibkr_daily_scan_data_completeness_intervals":
            return "5m,15m"
        return default


class _FakePlanner:
    def __init__(self, *, stale_once=False):
        self.stale_once = stale_once
        self.calls = 0

    def plan_symbol(self, symbol, intervals, environment="live", required_bars=0):
        self.calls += 1
        if symbol == "NVDA" and (not self.stale_once or self.calls <= 2):
            return {
                "ok": True,
                "environment": environment,
                "symbol": symbol,
                "status": "stale",
                "needs_repair": True,
                "needs_repair_intervals": ["15m"],
                "intervals": {"15m": {"status": "stale", "expected_closed_ms": _ms(2026, 4, 29, 19, 45), "reason": "latest_stale"}},
            }
        return {
            "ok": True,
            "environment": environment,
            "symbol": symbol,
            "status": "ready",
            "needs_repair": False,
            "needs_repair_intervals": [],
            "intervals": {},
        }


class _FakeRepair:
    def __init__(self):
        self.calls = []

    def enqueue_from_freshness(self, freshness, priority="manual", trigger="manual"):
        self.calls.append((freshness["symbol"], priority, trigger))
        return [{"queued": True, "job": {"symbol": freshness["symbol"], "priority": priority, "trigger": trigger}}]


class _FakeWaitCfg(_FakeCfg):
    def get_int_for_environment(self, key, environment, default=0):
        if key == "ibkr_daily_scan_runtime_topup_wait_sec":
            return 20
        return default


class BarFreshnessAndScanRepairTest(unittest.TestCase):
    def test_planner_prefers_direct_sqlite_latest_and_count(self):
        pb = _FakePB(rows=[{"symbol": "SPY", "interval": "5m", "bar_time_ms": _ms(2026, 4, 29, 10, 0)}])
        planner = BarFreshnessPlanner(pb, config=_FakeCfgWithSqliteRead(), environment="live")

        with mock.patch("ibkr_compute.market.bar_freshness.open_pb_sqlite") as open_sqlite, mock.patch(
            "ibkr_compute.market.bar_freshness.fetch_latest_bar",
            return_value={"symbol": "SPY", "interval": "5m", "bar_time_ms": _ms(2026, 4, 29, 19, 55)},
        ), mock.patch(
            "ibkr_compute.market.bar_freshness.count_recent_bars",
            return_value=260,
        ):
            open_sqlite.return_value.__enter__.return_value = object()
            payload = planner.plan_symbol(
                "SPY",
                ["5m"],
                environment="live",
                required_bars=260,
                now_ms=_ms(2026, 4, 29, 20, 5),
            )

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["latest_5m_ms"], _ms(2026, 4, 29, 19, 55))
        self.assertEqual(pb.upserts, [])

    def test_high_interval_stale_even_when_source_5m_has_afterhours(self):
        rows = [
            {"symbol": "SPY", "interval": "5m", "environment": "live", "bar_time_ms": _ms(2026, 4, 29, 19, 55), "extra": {"conid": 756733}},
            {"symbol": "SPY", "interval": "15m", "environment": "live", "bar_time_ms": _ms(2026, 4, 29, 11, 30)},
        ]
        planner = BarFreshnessPlanner(_FakePB(rows=rows), environment="live")

        payload = planner.plan_symbol("SPY", ["15m"], environment="live", now_ms=_ms(2026, 4, 29, 20, 5))

        interval = payload["intervals"]["15m"]
        self.assertEqual(interval["status"], "stale")
        self.assertTrue(payload["needs_repair"])
        self.assertEqual(interval["expected_closed_ms"], _ms(2026, 4, 29, 19, 45))

    def test_daily_scan_repairs_but_does_not_exclude_incomplete_symbol_by_default(self):
        repair = _FakeRepair()
        api_app = SimpleNamespace(
            cfg=_FakeCfg(),
            bar_freshness_planner=_FakePlanner(),
            bar_repair_coordinator=repair,
        )
        pb = _FakePB(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "NVDA", "environment": "live", "symbol_role": "trade"},
            ]
        )
        engines = {
            ("live", "AAPL", "5m"): _FakeEngine(),
            ("live", "NVDA", "5m"): _FakeEngine(),
        }
        with mock.patch.object(daily_scanner_mod, "get_api_app", return_value=api_app):
            scanner = DailyScanner(pb_client=pb, engines=engines)
        scanner.api_app = api_app
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 500000,
                "premarket_volume": 25000,
                "atr_pct": 0.5,
                "day_change_pct": 2.0,
            }
            for symbol in symbols
        }

        with mock.patch.object(daily_scanner_mod, "_load_scan_settings", return_value={
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
        }):
            result = scanner.run_scan("2026-04-29", environments=["live"])

        self.assertEqual([row["symbol"] for row in pb.upserts], ["AAPL", "NVDA"])
        self.assertEqual(result["excluded_incomplete_count"], 0)
        self.assertEqual(result["data_completeness"]["repairing_count"], 1)
        self.assertEqual(result["data_completeness"]["incomplete_symbol_count"], 1)
        self.assertFalse(result["data_completeness"]["blocking_enabled"])
        self.assertNotIn(REJECTION_BUCKET_DATA_INCOMPLETE, result["rejection_summary"])
        self.assertEqual(repair.calls, [("NVDA", "daily_scan", "daily_scan_data_completeness")])

    def test_daily_scan_can_exclude_incomplete_symbol_when_blocking_enabled(self):
        repair = _FakeRepair()
        api_app = SimpleNamespace(
            cfg=_FakeCfg(blocking=True),
            bar_freshness_planner=_FakePlanner(),
            bar_repair_coordinator=repair,
        )
        pb = _FakePB(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "NVDA", "environment": "live", "symbol_role": "trade"},
            ]
        )
        engines = {
            ("live", "AAPL", "5m"): _FakeEngine(),
            ("live", "NVDA", "5m"): _FakeEngine(),
        }
        with mock.patch.object(daily_scanner_mod, "get_api_app", return_value=api_app):
            scanner = DailyScanner(pb_client=pb, engines=engines)
        scanner.api_app = api_app
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 500000,
                "premarket_volume": 25000,
                "atr_pct": 0.5,
                "day_change_pct": 2.0,
            }
            for symbol in symbols
        }

        with mock.patch.object(daily_scanner_mod, "_load_scan_settings", return_value={
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
        }):
            result = scanner.run_scan("2026-04-29", environments=["live"])

        self.assertEqual([row["symbol"] for row in pb.upserts], ["AAPL"])
        self.assertEqual(result["excluded_incomplete_count"], 1)
        self.assertTrue(result["data_completeness"]["blocking_enabled"])
        self.assertEqual(result["rejection_summary"][REJECTION_BUCKET_DATA_INCOMPLETE], 1)
        self.assertEqual(repair.calls, [("NVDA", "daily_scan", "daily_scan_data_completeness")])

    def test_daily_scan_remote_compute_waits_for_runtime_watchlist_topup_without_enqueueing_repair(self):
        api_app = SimpleNamespace(
            cfg=_FakeCfg(blocking=True),
            bar_freshness_planner=_FakePlanner(),
            bar_repair_coordinator=None,
        )
        pb = _FakePB(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "NVDA", "environment": "live", "symbol_role": "trade"},
            ]
        )
        engines = {
            ("live", "AAPL", "5m"): _FakeEngine(),
            ("live", "NVDA", "5m"): _FakeEngine(),
        }
        with mock.patch.object(daily_scanner_mod, "get_api_app", return_value=api_app):
            scanner = DailyScanner(pb_client=pb, engines=engines)
        scanner.api_app = api_app
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 500000,
                "premarket_volume": 25000,
                "atr_pct": 0.5,
                "day_change_pct": 2.0,
            }
            for symbol in symbols
        }

        with mock.patch.object(daily_scanner_mod, "_load_scan_settings", return_value={
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
        }):
            result = scanner.run_scan("2026-04-29", environments=["live"])

        self.assertEqual([row["symbol"] for row in pb.upserts], ["AAPL"])
        self.assertEqual(result["excluded_incomplete_count"], 1)
        self.assertEqual(result["data_completeness"]["repair_job_count"], 0)
        self.assertEqual(result["data_completeness"]["repair_strategy"], "runtime_watchlist_idle_topup")
        example = result["rejection_examples"][0]
        self.assertEqual(example["bucket"], REJECTION_BUCKET_DATA_INCOMPLETE)
        self.assertIn("Runtime watchlist 回补", example["note"])

    def test_daily_scan_rechecks_after_runtime_watchlist_topup_becomes_fresh(self):
        planner = _FakePlanner(stale_once=True)
        api_app = SimpleNamespace(
            cfg=_FakeWaitCfg(blocking=True),
            bar_freshness_planner=planner,
            bar_repair_coordinator=None,
        )
        pb = _FakePB(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "NVDA", "environment": "live", "symbol_role": "trade"},
            ]
        )
        engines = {
            ("live", "AAPL", "5m"): _FakeEngine(),
            ("live", "NVDA", "5m"): _FakeEngine(),
        }
        with mock.patch.object(daily_scanner_mod, "get_api_app", return_value=api_app):
            scanner = DailyScanner(pb_client=pb, engines=engines)
        scanner.api_app = api_app
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 500000,
                "premarket_volume": 25000,
                "atr_pct": 0.5,
                "day_change_pct": 2.0,
            }
            for symbol in symbols
        }

        runtime_payload = {
            "watchlist_idle_topup": {
                "completion": {
                    "total": 2,
                    "fresh": 2,
                    "stale": 0,
                    "missing": 0,
                    "unobserved": 0,
                    "expected_latest_5m_ms": _ms(2026, 4, 29, 10, 0),
                    "oldest_latest_ms": _ms(2026, 4, 29, 10, 0),
                }
            }
        }
        with mock.patch.object(daily_scanner_mod, "_load_scan_settings", return_value={
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
        }), mock.patch(
            "ibkr_compute.api.runtime_status_client.get_remote_runtime_status",
            return_value=runtime_payload,
        ):
            result = scanner.run_scan("2026-04-29", environments=["live"])

        self.assertEqual(result["data_completeness"]["status"], "ready")
        self.assertTrue(result["data_completeness"]["runtime_topup_waited"])
        self.assertEqual(result["excluded_incomplete_count"], 0)
        self.assertEqual([row["symbol"] for row in pb.upserts], ["AAPL", "NVDA"])


if __name__ == "__main__":
    unittest.main()
