import re
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.universe import active_window_progress as progress
from ibkr_api.universe import active_window_progress_support as support
from ibkr_api.universe.active_window_progress import build_active_window_progress_response


def _bar(symbol, ms, environment="live"):
    return {
        "id": f"{symbol}-{ms}",
        "symbol": symbol,
        "exchange": "NASDAQ",
        "interval": "5m",
        "open": 1,
        "high": 2,
        "low": 1,
        "close": float(ms),
        "volume": 100,
        "session_type": "regular",
        "us_time": "",
        "cn_time": "",
        "bar_time_ms": ms,
        "environment": environment,
        "created": "2026-04-28 09:30:00",
        "updated": "2026-04-28 09:30:00",
    }


class PagingPocketBase:
    def __init__(self, symbols):
        self.rows = []
        self.calls = []
        for symbol in symbols:
            self.rows.extend(_bar(symbol, ms) for ms in range(650, 1000))
            self.rows.extend(_bar(symbol, ms) for ms in range(1000, 1250))

    def get_all_records(self, collection, **kwargs):
        self.calls.append((collection, kwargs))
        if collection != "ibkr_bars":
            return []
        filter_text = kwargs.get("filter") or ""
        symbols = re.findall(r'symbol = "([^"]+)"', filter_text)
        rows = [row for row in self.rows if row["symbol"] in symbols]
        if "bar_time_ms >= 1000" in filter_text:
            rows = [row for row in rows if 1000 <= row["bar_time_ms"] < 2000]
        elif "bar_time_ms < 1000" in filter_text:
            rows = [row for row in rows if row["bar_time_ms"] < 1000]
        reverse = str(kwargs.get("sort") or "").startswith("-")
        rows = sorted(rows, key=lambda row: row["bar_time_ms"], reverse=reverse)
        return rows[: max(1, int(kwargs.get("max_pages") or 1)) * 200]


class SummaryPocketBase:
    def get_runtime_config(self, **kwargs):
        return [{"key": "signal_window_max_bars", "value": "12", "environment": "live"}]

    def get_records(self, collection, **kwargs):
        if collection == "ibkr_targets":
            return [
                {
                    "symbol": "AAA",
                    "status": "active",
                    "score": 1,
                    "extra": {"source": "daily_scan", "active_gate_passed": True},
                },
                {"symbol": "BBB", "status": "candidate", "score": 0.5},
            ]
        return []

    def get_all_records(self, collection, **kwargs):
        if collection == "ibkr_signals":
            return [
                {
                    "symbol": "BBB",
                    "signal_id": "stored-1",
                    "direction": "long",
                    "signal": "stored_long",
                    "status": "new",
                    "bar_time_ms": 1100,
                    "us_time": "2026-04-28 09:35:00",
                    "updated": "2026-04-28 09:35:00",
                }
            ]
        return []


class FakePocketBase:
    def get_runtime_config(self, **kwargs):
        return [
            {"key": "signal_window_max_bars", "value": "12", "environment": "global"},
            {"key": "signal_window_max_bars", "value": "9", "environment": "live"},
        ]

    def get_records(self, collection, **kwargs):
        if collection == "ibkr_targets":
            return [
                {
                    "symbol": "AAPL",
                    "status": "active",
                    "score": 1,
                    "direction_bias": "long",
                    "extra": {"source": "daily_scan", "active_gate_passed": True},
                }
            ]
        return []

    def get_all_records(self, collection, **kwargs):
        return []


class ActiveWindowProgressApiTest(unittest.TestCase):
    def test_progress_response_builds_active_window_summary_without_bars(self):
        payload, status_code = build_active_window_progress_response(
            FakePocketBase(),
            payload={"environment": "live", "status": "active", "date": "2026-04-28"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower(),
            time_strings=lambda: {
                "date": "2026-04-28",
                "us": "2026-04-28 10:00:00",
                "cn": "2026-04-28 22:00:00",
            },
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["signal_window_max_bars"], 9)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["items"][0]["window_status"], "no_window")
        self.assertEqual(payload["items"][0]["window_max_bars"], 9)
        self.assertEqual(payload["items"][0]["trace_error"], "no_bars")

    def test_pb_fallback_loads_full_today_and_warmup_per_symbol(self):
        symbols = [f"S{i:02d}" for i in range(5)]
        pb = PagingPocketBase(symbols)

        with patch.object(support, "_load_timeline_bars_by_symbol_sqlite", return_value=None):
            rows_by_symbol = support._load_timeline_bars_by_symbol(
                pb,
                environment="live",
                symbols=symbols,
                interval="5m",
                market_start_ms=1000,
                market_end_ms=2000,
            )

        for symbol in symbols:
            rows = rows_by_symbol[symbol]
            self.assertEqual(len(rows), support.TIMELINE_WARMUP_BARS + 250)
            self.assertEqual(rows[0]["bar_time_ms"], 700)
            self.assertEqual(rows[support.TIMELINE_WARMUP_BARS - 1]["bar_time_ms"], 999)
            self.assertEqual(rows[-1]["bar_time_ms"], 1249)

        bar_calls = [kwargs for collection, kwargs in pb.calls if collection == "ibkr_bars"]
        self.assertEqual(len(bar_calls), len(symbols) * 2)
        for kwargs in bar_calls:
            self.assertEqual(len(re.findall(r'symbol = "([^"]+)"', kwargs.get("filter") or "")), 1)

    def test_summary_keeps_target_counts_and_adds_clear_meta(self):
        def fake_bars(_pb, *, symbols, market_start_ms, **_kwargs):
            return {symbol: [_bar(symbol, market_start_ms + 300000)] for symbol in symbols}

        def fake_trace(*, symbol, bars, **_kwargs):
            if symbol == "AAA":
                return {
                    "trace": {
                        "signal_state": {
                            "stage": "confirmed",
                            "label": "trace_long",
                            "signal_payload": {"signal": "trace_long", "direction": "long"},
                        },
                        "window_flags": {"sd_upper_valid": True},
                        "component_flags": {},
                    },
                    "latest_row": bars[-1],
                    "error": "",
                }
            return {
                "trace": {"signal_state": {"stage": "none", "label": ""}, "window_flags": {}, "component_flags": {}},
                "latest_row": bars[-1],
                "error": "",
            }

        with patch.object(progress, "_load_timeline_bars_by_symbol", side_effect=fake_bars), patch.object(
            progress, "_build_trace_for_symbol", side_effect=fake_trace
        ):
            payload, status_code = build_active_window_progress_response(
                SummaryPocketBase(),
                payload={"environment": "live", "status": "all", "date": "2026-04-28"},
                normalize_environment=lambda value, default="live": str(value or default).strip().lower(),
                time_strings=lambda: {"date": "2026-04-28"},
            )

        self.assertEqual(status_code, 200)
        summary = payload["summary"]
        self.assertEqual(summary["active_count"], 1)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(summary["target_active_count"], 1)
        self.assertEqual(summary["target_candidate_count"], 1)
        self.assertEqual(summary["trace_stage_counts"], {"confirmed": 1, "none": 1})
        self.assertEqual(summary["window_status_counts"], {"confirmed": 1, "no_window": 1})
        self.assertEqual(summary["current_candidate_signal_count"], 0)
        self.assertEqual(summary["timeline_data"]["symbols_requested"], 2)
        self.assertEqual(summary["timeline_data"]["symbols_with_today_bars_count"], 2)
        sources = {item["symbol"]: item.get("candidate_signal_source") for item in payload["items"]}
        self.assertEqual(sources["AAA"], "trace")
        self.assertEqual(sources["BBB"], "stored_signal")


if __name__ == "__main__":
    unittest.main()
