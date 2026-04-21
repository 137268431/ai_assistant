import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "runtime" / "ibkr_compute" / "src" / "ibkr_compute" / "api" / "monitor" / "samples.py"


def _normalize_symbol_list(values):
    normalized = []
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol:
            normalized.append(symbol)
    return normalized


def _load_samples_module():
    host_stub = types.ModuleType("ibkr_compute.api.monitor.host")
    host_stub._api_app = lambda: SimpleNamespace(
        WATCHLIST_SYMBOL_ROLE_TRADE="trade",
        WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
        _coerce_float=lambda value: None if value is None else float(value),
    )
    host_stub._copy_active_subscription_map = lambda service: dict(getattr(service, "_active_subscription_map", {}))
    host_stub._normalize_symbol_list = _normalize_symbol_list
    sys.modules["ibkr_compute.api.monitor.host"] = host_stub

    universe_stub = types.ModuleType("ibkr_compute.api.compute.runtime_state.universe")
    universe_stub.get_market_monitor_symbols = lambda environment: ["QQQ", "SPY", "VIX"]
    sys.modules["ibkr_compute.api.compute.runtime_state.universe"] = universe_stub

    spec = importlib.util.spec_from_file_location("test_monitor_samples_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MonitorSamplesVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = _load_samples_module()

    def test_canonical_5m_written_symbol_keeps_monitor_subscription_visible(self):
        service = SimpleNamespace(_active_subscription_map={"VIX": 13455763})
        runtime_status = {
            "warmup": {
                "monitor_symbols": ["VIX"],
                "symbol_status": [
                    {
                        "symbol": "VIX",
                        "ready": True,
                        "last_bar_time_ms": 1776791100000,
                    }
                ],
                "pending_symbols": [],
            },
            "market_universe": {
                "active_trade_symbols": [],
                "last_active_repair_reasons": {},
            },
            "bar_aggregator": {"active_bars": {}},
            "canonical_5m": {
                "last_completed_bucket_ms": 1776792300000,
                "written_symbols": ["VIX"],
            },
            "realtime_quotes": {"quotes": {}},
        }

        payload = self.samples._build_monitor_samples(service, runtime_status)
        self.assertEqual(payload["stale_symbols"], [])
        self.assertEqual(len(payload["active_subscriptions"]), 1)
        vix = payload["active_subscriptions"][0]
        self.assertEqual(vix["symbol"], "VIX")
        self.assertTrue(vix["visible"])
        self.assertFalse(vix["stale"])
        self.assertIn("canonical_5m", vix["visibility_sources"])


if __name__ == "__main__":
    unittest.main()
