import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.workflows import daily_scanner as daily_scanner_mod  # noqa: E402
from ibkr_compute.workflows.daily_scanner import DailyScanner  # noqa: E402
from ibkr_compute.core.active_window_admission import signal_pressure_from_item  # noqa: E402
from ibkr_compute.core.indicators.technical import TechnicalIndicators  # noqa: E402


class FakeEngine:
    def __init__(self, snapshot: dict, ready: bool = True):
        self._snapshot = dict(snapshot)
        self._ready = bool(ready)

    def is_ready(self) -> bool:
        return self._ready

    def get_snapshot(self) -> dict:
        return dict(self._snapshot)


class DummyPBClient:
    def __init__(self, watchlist: list[dict], existing_targets: list[dict] | None = None):
        self.watchlist = list(watchlist)
        self.existing_targets = list(existing_targets or [])
        self.upserts: list[dict] = []
        self.updated: list[tuple[str, str, dict]] = []

    def get_records(self, collection: str, **kwargs):
        if collection == "watchlist":
            return list(self.watchlist)
        if collection == "ibkr_indicators":
            return []
        raise AssertionError(f"unexpected collection: {collection}")

    def get_all_records(self, collection: str, **kwargs):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected collection: {collection}")
        return list(self.existing_targets)

    def upsert_scan(self, payload: dict):
        self.upserts.append(dict(payload))

    def update_record(self, collection: str, record_id: str, payload: dict):
        self.updated.append((collection, record_id, dict(payload)))


def quality_metrics(**overrides) -> dict:
    row = {
        "avg_10d_volume": 3_500_000,
        "premarket_volume": 25_000,
        "atr_pct": 0.8,
        "day_change_pct": 2.5,
        "exchange": "SMART",
    }
    row.update(overrides)
    return row


def watchlist(symbols: list[str]) -> list[dict]:
    return [
        {"symbol": symbol, "environment": "live", "symbol_role": "trade", "exchange": "SMART"}
        for symbol in symbols
    ]


def engines_for(symbol: str, snapshots: dict[str, dict]) -> dict:
    return {
        ("live", symbol, timeframe): FakeEngine(snapshot)
        for timeframe, snapshot in snapshots.items()
    }


def sd_crsi_snapshots(side: str = "long") -> dict[str, dict]:
    if side == "short":
        return {
            "5m": {
                "us_time": "10:00",
                "ema_bearish": True,
                "trend_dir": -1,
                "sd_upper": True,
                "crsi_ob": True,
                "crsi": 82.0,
                "crsi_ub": 70.0,
            }
        }
    return {
        "5m": {
            "us_time": "10:00",
            "ema_bullish": True,
            "trend_dir": 1,
            "sd_lower": True,
            "crsi_os": True,
            "crsi": 18.0,
            "crsi_db": 30.0,
        }
    }


class SignalWindowV1AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100_000,
            "min_premarket_volume": 5_000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
            "active_target_limit": 24,
            "active_min_score": 0,
            "day_gain_trigger_enabled": True,
            "day_gain_trigger_pct": 4.0,
        }
        self.settings_patch = mock.patch.object(
            daily_scanner_mod,
            "_load_scan_settings",
            return_value=dict(self.settings),
        )
        self.api_app_patch = mock.patch.object(daily_scanner_mod, "get_api_app", return_value=object())
        self.settings_patch.start()
        self.api_app_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.api_app_patch.stop()

    def build_scanner(self, symbols: list[str], snapshots_by_symbol: dict[str, dict[str, dict]]):
        engines = {}
        for symbol, snapshots in snapshots_by_symbol.items():
            engines.update(engines_for(symbol, snapshots))
        pb_client = DummyPBClient(watchlist=symbols and watchlist(symbols) or [])
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._attach_live_trigger_inputs = lambda *args, **kwargs: None
        scanner._build_metric_rows = lambda date, environment, rows: {
            symbol: quality_metrics() for symbol in rows
        }
        return scanner, pb_client

    def test_day_gain_rvol_orb_vwap_only_does_not_active_admit(self):
        symbol = "HOT"
        hot_only_snapshots = {
            "1d": {"ema_bullish": True, "trend_dir": 1},
            "4h": {"ema_bullish": True, "trend_dir": 1},
            "1h": {"ema_bullish": True, "trend_dir": 1},
            "30m": {"ema_bullish": True, "trend_dir": 1},
            "15m": {
                "us_time": "10:00",
                "ema_bullish": True,
                "trend_dir": 1,
                "orb_breakout_up": True,
                "vwap_alignment": "above",
                "rvol_20": 3.5,
            },
            "5m": {
                "us_time": "10:00",
                "ema_bullish": True,
                "trend_dir": 1,
                "orb_breakout_up": True,
                "vwap_alignment": "above",
                "rvol_20": 3.5,
            },
        }
        scanner, pb_client = self.build_scanner([symbol], {symbol: hot_only_snapshots})
        metrics = quality_metrics(
            day_change_pct=4.8,
            rvol_20=3.5,
            orb_breakout_up=True,
            vwap_alignment="above",
        )
        scanner._build_metric_rows = lambda date, environment, rows: {symbol: dict(metrics)}

        evaluated = scanner.evaluate_symbol(
            symbol,
            "2026-04-21",
            "live",
            metrics=metrics,
        )
        self.assertTrue(evaluated["quality_gate_passed"])
        self.assertEqual(evaluated["extra"]["admission_gate_version"], "signal_window_v1")
        self.assertFalse(evaluated["extra"]["signal_pressure_passed"])
        self.assertFalse(evaluated["context_gate_passed"])

        result = scanner.run_scan("2026-04-21", environments=["live"])

        self.assertEqual([row for row in pb_client.upserts if row["status"] == "active"], [])
        self.assertEqual(result["new_active"], 0)
        self.assertEqual(result["active"], 0)
        self.assertEqual(result["rejection_summary"].get("context_gate_not_passed"), 1)

    def test_sd_crsi_pressure_inside_window_active_admits(self):
        symbol = "SDOK"
        scanner, pb_client = self.build_scanner([symbol], {symbol: sd_crsi_snapshots("long")})

        result = scanner.run_scan("2026-04-21", environments=["live"])

        self.assertEqual(result["new_active"], 1)
        self.assertEqual(result["active"], 1)
        self.assertEqual(len(pb_client.upserts), 1)
        payload = pb_client.upserts[0]
        extra = payload["extra"]
        self.assertEqual(payload["status"], "active")
        self.assertEqual(extra["admission_gate_version"], "signal_window_v1")
        self.assertTrue(extra["signal_window_gate_passed"])
        self.assertTrue(extra["signal_pressure_passed"])
        self.assertIn("5m:sd_lower", extra["signal_pressure_keys"])
        self.assertIn("5m:crsi_oversold", extra["signal_pressure_keys"])

    def test_seed_active_target_limit_caps_active_rows(self):
        self.settings["active_target_limit"] = 1
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        symbols = ["AAPL", "MSFT", "NVDA"]
        snapshots = {symbol: sd_crsi_snapshots("long") for symbol in symbols}
        scanner, pb_client = self.build_scanner(symbols, snapshots)

        result = scanner.run_scan("2026-04-21", environments=["live"])

        active_rows = [row for row in pb_client.upserts if row["status"] == "active"]
        self.assertEqual(len(active_rows), 1)
        self.assertEqual(result["new_active"], 1)
        self.assertEqual(result["active"], 1)
        self.assertLessEqual(len(active_rows), self.settings["active_target_limit"])

    def test_seed_active_target_limit_zero_blocks_active_rows(self):
        self.settings["active_target_limit"] = "0"
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        symbols = ["AAPL", "MSFT"]
        snapshots = {symbol: sd_crsi_snapshots("long") for symbol in symbols}
        scanner, pb_client = self.build_scanner(symbols, snapshots)

        result = scanner.run_scan("2026-04-21", environments=["live"])

        self.assertEqual([row for row in pb_client.upserts if row["status"] == "active"], [])
        self.assertEqual(result["new_active"], 0)
        self.assertEqual(result["active"], 0)
        self.assertEqual(result["rejection_summary"].get("active_target_limit_full"), 2)

    def test_directionless_candidate_signal_does_not_pass_pressure_gate(self):
        pressure = signal_pressure_from_item(
            {
                "us_time": "10:00",
                "trace_stage": "candidate",
                "candidate_signal": {"signal": "sd_mr_reversal_long"},
            }
        )

        self.assertFalse(pressure["signal_pressure_passed"])
        self.assertEqual(pressure["signal_pressure_sides"], [])
        self.assertNotIn("candidate_signal", pressure["signal_pressure_keys"])

    def test_flat_stochrsi_rsi_stays_neutral(self):
        indicator = TechnicalIndicators(
            {
                "stochrsi_rsi_length": 3,
                "stochrsi_length": 3,
                "stochrsi_k_smoothing": 1,
                "stochrsi_d_smoothing": 1,
            }
        )
        output = {}
        for _ in range(12):
            output = indicator.update(
                {"open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000}
            )

        self.assertEqual(output["stochrsi_rsi"], 50.0)


if __name__ == "__main__":
    unittest.main()
