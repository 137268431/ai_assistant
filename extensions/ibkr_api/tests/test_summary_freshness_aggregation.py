from __future__ import annotations

import json
import re
from datetime import datetime
from unittest import mock

from control_plane_split_stack_helpers import *
from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.freshness_evaluator import build_data_freshness_summary


def _ms(us_time: str) -> int:
    return int(datetime.strptime(us_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp() * 1000)


def _bar(symbol: str, interval: str, start_us: str, close_us: str) -> dict:
    close_ms = _ms(close_us)
    return {
        "id": f"{symbol}-{interval}-{start_us}",
        "symbol": symbol,
        "interval": interval,
        "environment": "live",
        "bar_time_ms": _ms(start_us),
        "us_time": start_us,
        "extra": json.dumps({"bar_close_time_ms": close_ms, "bar_close_us_time": close_us}),
    }


class _FreshnessConfig:
    def get_int_for_environment(self, key, environment, default=0):
        if key == "ibkr_official_5m_close_delay_sec":
            return 0
        return default

    def get_bool_for_environment(self, key, environment, default=False):
        if key == "ibkr_summary_freshness_direct_sqlite_enabled":
            return True
        return default

    def get_float_for_environment(self, key, environment, default=0.0):
        return default


class _FreshnessPB:
    def __init__(self, *, bars=None, targets=None):
        self.bars = list(bars or [])
        self.targets = list(targets or [])
        self.filters = []

    def get_runtime_config(self):
        return []

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        self.filters.append((collection, filter or ""))
        if collection == "ibkr_targets":
            date_match = re.search(r'date = "([^"]+)"', filter or "")
            date_value = date_match.group(1) if date_match else ""
            return [row for row in self.targets if row.get("date") == date_value]
        if collection != "ibkr_bars":
            return []
        rows = self._bar_rows(filter or "")
        if sort == "-bar_time_ms":
            rows = sorted(rows, key=lambda row: int(row.get("bar_time_ms") or 0), reverse=True)
        return rows

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_all_records(collection, filter=filter, sort=sort, max_pages=1)
        return rows[0] if rows else None

    def _bar_rows(self, filter_text: str):
        symbol_match = re.search(r'symbol = "([^"]+)"', filter_text)
        interval_match = re.search(r'interval = "([^"]+)"', filter_text)
        symbol = symbol_match.group(1) if symbol_match else ""
        interval = interval_match.group(1) if interval_match else ""
        rows = self.bars
        if symbol:
            rows = [row for row in rows if row.get("symbol") == symbol]
        if interval:
            rows = [row for row in rows if row.get("interval") == interval]
        return list(rows)


class SummaryFreshnessAggregationTest(unittest.TestCase):
    def test_summaryz_aggregates_by_close_time_and_interval_due(self):
        bars = [
            _bar("AAPL", "5m", "2026-05-19 10:10:00", "2026-05-19 10:15:00"),
            _bar("AAPL", "15m", "2026-05-19 10:00:00", "2026-05-19 10:15:00"),
            _bar("AAPL", "30m", "2026-05-19 09:30:00", "2026-05-19 10:00:00"),
            _bar("AAPL", "1h", "2026-05-19 09:00:00", "2026-05-19 10:00:00"),
            _bar("AAPL", "4h", "2026-05-19 04:00:00", "2026-05-19 08:00:00"),
            _bar("AAPL", "1d", "2026-05-18 00:00:00", "2026-05-18 16:00:00"),
            _bar("MSFT", "5m", "2026-05-19 10:10:00", "2026-05-19 10:15:00"),
            _bar("MSFT", "15m", "2026-05-19 09:45:00", "2026-05-19 10:00:00"),
            _bar("MSFT", "30m", "2026-05-19 09:30:00", "2026-05-19 10:00:00"),
            _bar("MSFT", "1h", "2026-05-19 09:00:00", "2026-05-19 10:00:00"),
            _bar("MSFT", "4h", "2026-05-19 04:00:00", "2026-05-19 08:00:00"),
            _bar("MSFT", "1d", "2026-05-18 00:00:00", "2026-05-18 16:00:00"),
        ]
        pb = _FreshnessPB(bars=bars)
        runtime_payload = _sample_runtime_status_payload(authenticated=True)
        runtime_payload["market_universe"]["active_trade_symbols"] = ["AAPL", "MSFT"]

        with mock.patch("ibkr_compute.market.freshness_evaluator.open_pb_sqlite", side_effect=FileNotFoundError("no db")):
            with mock.patch.object(api_app_mod, "pb", pb):
                with mock.patch.object(api_app_mod, "config", _FreshnessConfig()):
                    with mock.patch.object(api_app_mod, "_fetch_compute_health", return_value={"ok": True, "payload": {"status": "running"}}):
                        with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value={"ok": True, "payload": _sample_compute_status_payload()}):
                            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value={"ok": True, "payload": runtime_payload, "error": ""}):
                                with mock.patch.object(api_app_mod, "_load_effective_config_map", return_value={"ibkr_compute_enabled": "TRUE"}):
                                    with mock.patch.object(api_app_mod, "_load_today_counts", return_value={}):
                                        with mock.patch.object(api_app_mod, "_load_recent_system_events", return_value=[]):
                                            with mock.patch.object(api_app_mod, "_collect_storage_health", return_value={"ok": True, "status": "ok"}):
                                                with mock.patch.object(
                                                    api_app_mod,
                                                    "_time_strings",
                                                    return_value={
                                                        "us": "2026-05-19 10:17:00",
                                                        "cn": "2026-05-19 22:17:00",
                                                        "date": "2026-05-19",
                                                    },
                                                ):
                                                    with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                                                        payload = api_app_mod.custom_system_summaryz()

        freshness = payload["data_freshness"]
        intervals = {item["interval"]: item for item in freshness["intervals"]}
        self.assertEqual(freshness["universe_source"], "runtime_market_universe")
        self.assertEqual(freshness["total_symbols"], 2)
        self.assertEqual(intervals["15m"]["expected_close_us"], "2026-05-19 10:15:00")
        self.assertEqual(intervals["15m"]["ready"], 1)
        self.assertEqual(intervals["15m"]["overdue"], 1)
        self.assertEqual(intervals["15m"]["status"], "overdue")
        self.assertEqual(intervals["1d"]["expected_close_us"], "2026-05-18 16:00:00")
        self.assertEqual(intervals["1d"]["not_due"], 2)

    def test_high_interval_waits_for_missing_5m_boundary(self):
        bars = [
            _bar("AAPL", "5m", "2026-05-19 10:05:00", "2026-05-19 10:10:00"),
            _bar("AAPL", "15m", "2026-05-19 09:45:00", "2026-05-19 10:00:00"),
        ]
        runtime_payload = _sample_runtime_status_payload(authenticated=True)
        runtime_payload["market_universe"]["active_trade_symbols"] = ["AAPL"]

        with mock.patch("ibkr_compute.market.freshness_evaluator.open_pb_sqlite", side_effect=FileNotFoundError("no db")):
            payload = build_data_freshness_summary(
                pb_client=_FreshnessPB(bars=bars),
                runtime_payload=runtime_payload,
                environment="live",
                market_date="2026-05-19",
                config=_FreshnessConfig(),
                now_us="2026-05-19 10:17:00",
                intervals=["5m", "15m"],
            )

        intervals = {item["interval"]: item for item in payload["intervals"]}
        self.assertEqual(intervals["5m"]["overdue"], 1)
        self.assertEqual(intervals["15m"]["waiting_5m"], 1)
        self.assertEqual(intervals["15m"]["overdue"], 0)
        self.assertEqual(intervals["15m"]["status"], "waiting_5m")

    def test_closed_afterhours_without_extended_evidence_is_quiet_not_overdue(self):
        bars = [
            _bar("AAPL", "5m", "2026-05-19 15:55:00", "2026-05-19 16:00:00"),
            _bar("AAPL", "15m", "2026-05-19 15:45:00", "2026-05-19 16:00:00"),
            _bar("AAPL", "1d", "2026-05-19 00:00:00", "2026-05-19 16:00:00"),
        ]
        runtime_payload = _sample_runtime_status_payload(authenticated=True)
        runtime_payload["market_universe"]["active_trade_symbols"] = ["AAPL"]

        with mock.patch("ibkr_compute.market.freshness_evaluator.open_pb_sqlite", side_effect=FileNotFoundError("no db")):
            payload = build_data_freshness_summary(
                pb_client=_FreshnessPB(bars=bars),
                runtime_payload=runtime_payload,
                environment="live",
                market_date="2026-05-20",
                config=_FreshnessConfig(),
                now_us="2026-05-20 00:30:00",
                intervals=["5m", "15m", "1d"],
            )

        intervals = {item["interval"]: item for item in payload["intervals"]}
        self.assertEqual(intervals["5m"]["quiet_extended"], 1)
        self.assertEqual(intervals["5m"]["overdue"], 0)
        self.assertEqual(intervals["15m"]["quiet_extended"], 1)
        self.assertEqual(intervals["15m"]["overdue"], 0)
        self.assertEqual(intervals["1d"]["ready"], 1)
        self.assertEqual(payload["overall"]["overdue"], 0)

    def test_empty_runtime_universe_falls_back_to_today_targets_only(self):
        bars = [
            _bar("AAPL", "5m", "2026-05-19 10:10:00", "2026-05-19 10:15:00"),
            _bar("OLD", "5m", "2026-05-19 10:10:00", "2026-05-19 10:15:00"),
        ]
        targets = [
            {"symbol": "AAPL", "environment": "live", "date": "2026-05-19"},
            {"symbol": "OLD", "environment": "live", "date": "2026-05-18"},
        ]
        runtime_payload = _sample_runtime_status_payload(authenticated=True)
        runtime_payload["market_universe"]["active_trade_symbols"] = []
        runtime_payload["market_universe"]["market_ws_symbols"] = []
        runtime_payload["market_universe"]["market_ws_subscribed_symbols"] = []

        with mock.patch("ibkr_compute.market.freshness_evaluator.open_pb_sqlite", side_effect=FileNotFoundError("no db")):
            payload = build_data_freshness_summary(
                pb_client=_FreshnessPB(bars=bars, targets=targets),
                runtime_payload=runtime_payload,
                environment="live",
                market_date="2026-05-19",
                config=_FreshnessConfig(),
                now_us="2026-05-19 10:17:00",
                intervals=["5m"],
            )

        self.assertEqual(payload["universe_source"], "today_targets")
        self.assertEqual(payload["symbols"], ["AAPL"])
        self.assertEqual(payload["total_symbols"], 1)
        self.assertEqual(payload["intervals"][0]["ready"], 1)
