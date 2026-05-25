import unittest
from pathlib import Path
from unittest import mock

import sys
import types

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    class _FakeRequestArgs:
        def get(self, name, default=None):
            return default

        def getlist(self, name):
            return []

    flask_stub = types.ModuleType("flask")
    flask_stub.request = types.SimpleNamespace(args=_FakeRequestArgs(), get_json=lambda silent=True: {})
    flask_stub.jsonify = lambda payload: payload
    flask_stub.current_app = None
    flask_stub.has_app_context = lambda: False
    sys.modules["flask"] = flask_stub

from ibkr_compute.api.market import rules_views


class FakeConfig:
    DEFAULTS = {
        "ibkr_timeframe_param_profiles_json": '{"5m":{"signal_strategy_profile":"intraday_sd_v1"}}',
    }

    def __init__(self):
        self._records_by_key = {}

    def refresh(self):
        return None

    def get_for_environment(self, key, environment, default=""):
        return self.DEFAULTS.get(key, default)

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.get_for_environment(key, environment, str(default)).lower()
        return value in {"1", "true", "yes", "on"}

    def get_int_for_environment(self, key, environment, default=0):
        try:
            return int(self.get_for_environment(key, environment, str(default)))
        except ValueError:
            return int(default)


class FakeAppModule:
    SUPPORTED_COMPUTE_ENVIRONMENTS = ("live", "backtest")
    WATCHLIST_SYMBOL_ROLE_TRADE = "trade"

    def __init__(self):
        self.cfg = FakeConfig()

    def build_runtime_timestamps(self):
        return {"computed_at_us": "2026-05-22 09:35:00"}

    def normalize_watchlist_symbol_role(self, value):
        return "trade" if str(value or "trade") == "trade" else "market_monitor"


class FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self):
        return self._payload


DAILY_SCAN_SUMMARY = {
    "primary_weight": 2,
    "secondary_weight": 1,
    "ready_timeframe_bonus": 1,
    "long_primary": ["trend_dir=1", "dtp_dir=1", "ema_bullish=true"],
    "short_primary": ["trend_dir=-1", "dtp_dir=-1", "ema_bearish=true"],
    "long_secondary": ["fractal_bull=true", "crsi_os=true", "sd_lower=true"],
    "short_secondary": ["fractal_bear=true", "crsi_ob=true", "sd_upper=true"],
    "tie_behavior": "long_votes == short_votes => direction_bias=neutral, score=0",
    "final_bonus": "direction_bias 非 neutral 时额外加上 ready_timeframes_count",
    "scan_time_et": "09:20",
    "quality_gates": {
        "avg_10d_volume_gte": 100000,
        "premarket_volume_gte": 5000,
        "atr_pct_gte": 0.15,
        "abs_day_change_pct_gte": 1.0,
    },
    "subscription_budget": {
        "trade_budget": "77",
        "total_limit": 80,
        "monitor_count": 3,
    },
}


class SystemLogicRulesPayloadTest(unittest.TestCase):
    def test_rules_response_exposes_system_logic_sections(self):
        fake_app = FakeAppModule()
        with mock.patch.object(rules_views, "get_app_module", return_value=fake_app), \
            mock.patch.object(rules_views, "get_requested_environment", return_value="live"), \
            mock.patch.object(rules_views, "load_effective_watchlist", return_value={"AAPL": {"symbol_role": "trade"}}), \
            mock.patch.object(rules_views, "get_active_trade_symbols", return_value={"AAPL"}), \
            mock.patch.object(rules_views, "build_daily_scan_rule_summary", return_value=DAILY_SCAN_SUMMARY), \
            mock.patch.object(rules_views, "jsonify", side_effect=lambda payload: FakeJsonResponse(payload)):
            response = rules_views.build_rules_response()

        payload = response.get_json()
        self.assertEqual("system_logic_v2", payload["schema_version"])
        for key in (
            "system_flow",
            "selection",
            "indicators",
            "signals",
            "execution",
            "order_flow",
            "orders",
            "broker_mode_switch",
            "quality",
            "backtest_validation",
            "source_refs",
            "logic_coverage",
        ):
            self.assertIn(key, payload)

        coverage_ids = {item["id"] for item in payload["logic_coverage"]}
        self.assertIn("target_selection", coverage_ids)
        self.assertIn("data_indicators", coverage_ids)
        self.assertIn("scheduler", coverage_ids)
        self.assertIn("order_flow", coverage_ids)
        self.assertIn("broker_mode_switch", coverage_ids)
        self.assertTrue(all(item["status"] == "covered" for item in payload["logic_coverage"]))

    def test_indicator_and_execution_sections_include_expected_defaults(self):
        fake_app = FakeAppModule()
        with mock.patch.object(rules_views, "get_app_module", return_value=fake_app), \
            mock.patch.object(rules_views, "get_active_trade_symbols", return_value={"AAPL"}):
            indicators = rules_views._indicator_panel("live")
            execution = rules_views._execution_panel("live")
            signals = rules_views._signal_panel("live")
            order_flow = rules_views._order_flow_panel("live")
            broker_switch = rules_views._broker_mode_switch_panel("live")

        indicator_text = "\n".join(
            line
            for section in indicators["sections"]
            for line in section.get("lines", [])
        )
        self.assertIn("ema_touch_type", str(indicators["chips"]))
        self.assertIn("sd_length", indicator_text)
        self.assertIn("DTP Early", str(indicators["chips"]))
        self.assertIn("intraday_signal_validity_minutes", str(signals))

        execution_text = "\n".join(
            line
            for section in execution["sections"]
            for line in section.get("lines", [])
        )
        self.assertIn("trade window", execution_text)
        self.assertIn("order_window_end_time", str(execution))
        self.assertIn("signal_validity_minutes", str(execution))
        self.assertIn("position_amount", execution_text)
        self.assertIn("ibkr_order_flow_mode", str(order_flow))
        self.assertIn("Execution Pool", str(order_flow))
        self.assertIn("never_widen_stop_by_order_flow", str(order_flow))
        self.assertIn("broker-mode/switch/preview", str(broker_switch))
        self.assertIn("2FA", str(broker_switch))


if __name__ == "__main__":
    unittest.main()
