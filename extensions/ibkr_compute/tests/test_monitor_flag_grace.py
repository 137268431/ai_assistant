from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.monitor.flags import _build_monitor_flags


class MonitorFlagGraceTest(unittest.TestCase):
    def _build_market_data_flags(self, session_kind: str, last_message_age_s: float, extra_api_utilization: dict | None = None):
        api_utilization = {
            "subscription_limit": 70,
            "active_subscription_count": 2,
            "pending_subscription_count": 0,
            "last_message_age_s": last_message_age_s,
        }
        if extra_api_utilization:
            api_utilization.update(extra_api_utilization)
        return _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "market_session": {"kind": session_kind},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            api_utilization,
            {},
            {},
        )

    def test_session_unauthenticated_suppressed_during_restart_grace(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": datetime.now(timezone.utc).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("session_unauthenticated", flag_codes)

    def test_session_unauthenticated_returns_after_restart_grace_expires(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertIn("session_unauthenticated", flag_codes)

    def test_session_unauthenticated_stays_suppressed_longer_after_close(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "market_session": {"kind": "afterhours"},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("session_unauthenticated", flag_codes)

    def test_session_unauthenticated_returns_after_late_session_grace_expires(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "market_session": {"kind": "afterhours"},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": (datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertIn("session_unauthenticated", flag_codes)

    def test_market_data_silent_warns_in_regular_session(self):
        flags = self._build_market_data_flags("regular", 61.0)

        warning = next(item for item in flags if item["code"] == "market_data_silent")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("warning=60s", warning["detail"])
        self.assertIn("critical=180s", warning["detail"])

    def test_market_data_silent_critical_in_regular_session(self):
        flags = self._build_market_data_flags("regular", 181.0)

        critical = next(item for item in flags if item["code"] == "market_data_silent_critical")
        self.assertEqual(critical["severity"], "error")
        self.assertIn("session=regular", critical["detail"])

    def test_market_data_session_conflict_suppresses_ws_silence_for_10197(self):
        flags = _build_monitor_flags(
            {
                "gateway": {
                    "running": True,
                    "reachable": True,
                    "broker": {
                        "connected": True,
                        "ready": True,
                        "last_error_code": 10197,
                        "last_error": "No market data during competing live session",
                        "last_error_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                "market_session": {"kind": "regular"},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 2,
                "pending_subscription_count": 0,
                "last_message_age_s": 347.7,
            },
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertIn("market_data_session_conflict", flag_codes)
        self.assertNotIn("market_data_silent", flag_codes)
        self.assertNotIn("market_data_silent_critical", flag_codes)

    def test_market_data_session_conflict_detail_includes_persisted_state(self):
        flags = _build_monitor_flags(
            {
                "gateway": {
                    "running": True,
                    "reachable": True,
                    "broker": {
                        "connected": True,
                        "ready": True,
                        "last_error_code": 10197,
                        "last_error": "No market data during competing live session",
                        "last_error_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
                "order_flow": {"enabled": False},
                "signal_router": {"signal_source": "tradingview"},
                "market_data_session_conflict": {
                    "active": True,
                    "first_seen_at": "2026-06-15T14:17:46+00:00",
                    "last_seen_at": "2026-06-15T15:56:45+00:00",
                    "last_error_at": "2026-06-15T15:56:45+00:00",
                    "count": 8,
                },
            },
            {"active_subscription_count": 2, "pending_subscription_count": 0},
            {},
            {},
        )

        conflict = next(item for item in flags if item["code"] == "market_data_session_conflict")
        self.assertEqual("Market data session conflict (orders still available)", conflict["title"])
        self.assertIn("首次记录", conflict["detail"])
        self.assertIn("累计次数：8", conflict["detail"])
        self.assertIn("下单影响：当前不阻断下单", conflict["detail"])
        self.assertIn("信号来源=tradingview", conflict["detail"])
        self.assertIn("OrderFlow=disabled", conflict["detail"])
        self.assertIn("1-3 分钟", conflict["detail"])

    def test_market_data_session_conflict_warns_when_order_flow_uses_quotes(self):
        flags = _build_monitor_flags(
            {
                "gateway": {
                    "running": True,
                    "reachable": True,
                    "broker": {
                        "connected": True,
                        "ready": True,
                        "last_error_code": 10197,
                        "last_error": "No market data during competing live session",
                        "last_error_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True, "subscribed_count": 3, "pending_count": 0},
                "order_flow": {"enabled": True},
                "signal_router": {"signal_source": "tradingview"},
            },
            {"active_subscription_count": 3, "pending_subscription_count": 0},
            {},
            {},
        )

        conflict = next(item for item in flags if item["code"] == "market_data_session_conflict")
        self.assertIn("orders still available", conflict["title"])
        self.assertIn("下单影响：可能影响依赖实时报价的入场/出场", conflict["detail"])
        self.assertIn("OrderFlow=enabled", conflict["detail"])

    def test_market_data_session_conflict_does_not_claim_orders_ok_when_broker_not_ready(self):
        flags = _build_monitor_flags(
            {
                "gateway": {
                    "running": True,
                    "reachable": True,
                    "broker": {
                        "connected": False,
                        "ready": False,
                        "last_error_code": 10197,
                        "last_error": "No market data during competing live session",
                        "last_error_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True, "subscribed_count": 3, "pending_count": 0},
                "order_flow": {"enabled": False},
                "signal_router": {"signal_source": "tradingview"},
            },
            {"active_subscription_count": 3, "pending_subscription_count": 0},
            {},
            {},
        )

        conflict = next(item for item in flags if item["code"] == "market_data_session_conflict")
        self.assertEqual("Market data session conflict", conflict["title"])
        self.assertIn("下单影响：订单通道可能受影响", conflict["detail"])

    def test_market_data_silent_is_suppressed_during_close_transition_with_default_late_thresholds(self):
        flags = self._build_market_data_flags("close_transition", 111.6)

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("market_data_silent", flag_codes)
        self.assertNotIn("market_data_silent_critical", flag_codes)

    def test_market_data_silent_warns_after_late_session_warn_threshold(self):
        flags = self._build_market_data_flags("close_transition", 601.0)

        warning = next(item for item in flags if item["code"] == "market_data_silent")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("warning=600s", warning["detail"])
        self.assertIn("critical=1200s", warning["detail"])

    def test_market_data_silent_critical_after_late_session_critical_threshold(self):
        flags = self._build_market_data_flags("afterhours", 1201.0)

        critical = next(item for item in flags if item["code"] == "market_data_silent_critical")
        self.assertEqual(critical["severity"], "error")
        self.assertIn("session=afterhours", critical["detail"])

    def test_market_data_silent_is_disabled_when_market_is_closed(self):
        flags = self._build_market_data_flags("closed", 1200.0)

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("market_data_silent", flag_codes)
        self.assertNotIn("market_data_silent_critical", flag_codes)

    def test_stale_market_monitor_symbol_does_not_raise_control_warning(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {"active_subscription_count": 1, "pending_subscription_count": 0},
            {},
            {
                "stale_symbols": ["VIX"],
                "active_subscriptions": [
                    {"symbol": "VIX", "role": "market_monitor", "stale": True},
                ],
            },
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("stale_active_symbols", flag_codes)

    def test_stale_trade_symbol_still_raises_control_warning(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {"active_subscription_count": 1, "pending_subscription_count": 0},
            {},
            {
                "stale_symbols": ["AAPL"],
                "active_subscriptions": [
                    {"symbol": "AAPL", "role": "trade", "stale": True},
                ],
            },
        )

        warning = next(item for item in flags if item["code"] == "stale_active_symbols")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("当前有 1 个", warning["detail"])

    def test_stale_warning_counts_only_non_monitor_symbols(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {"active_subscription_count": 2, "pending_subscription_count": 0},
            {},
            {
                "stale_symbols": ["AAPL", "VIX"],
                "active_subscriptions": [
                    {"symbol": "AAPL", "role": "trade", "stale": True},
                    {"symbol": "VIX", "role": "market_monitor", "stale": True},
                ],
            },
        )

        warning = next(item for item in flags if item["code"] == "stale_active_symbols")
        self.assertIn("当前有 1 个", warning["detail"])
        self.assertIn("另有 1 个 market monitor", warning["detail"])


if __name__ == "__main__":
    unittest.main()
