import sys
import unittest
from pathlib import Path

SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.system.jobs.scan_summary import build_system_scan_summary_response
from ibkr_api.system.jobs.status_heartbeat import build_system_status_reminder_response
from ibkr_api.system.jobs.open_report import matches_open_report_time_window


class SystemScanSummaryTest(unittest.TestCase):
    def test_open_report_window_is_0930_to_before_0940(self):
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:29:59"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:30:00"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:35:00"))
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:40:00"))

    def test_scan_summary_delivers_open_report_to_status_chat(self):
        sent = []
        states = {}
        events = []

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777383000000,
                "computed_at_us": "2026-04-28 09:30:17",
                "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                "summary": {
                    "total": 1,
                    "active_count": 1,
                    "candidate_count": 0,
                    "operable_count": 1,
                    "technical_ready_count": 1,
                    "signaled_count": 0,
                },
                "items": [
                    {
                        "symbol": "NVDA",
                        "status": "active",
                        "score": 31,
                        "direction_bias": "long",
                        "technical_state": "ready",
                        "price": 900,
                        "day_change_pct": 1.2,
                        "scan_reason": "15m:ema_bullish",
                    }
                ],
            }, 200

        payload, status_code = build_system_scan_summary_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 09:30:17", "cn": "2026-04-28 21:30:17", "date": "2026-04-28"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
            },
            build_system_monitor_payload=lambda environment: {
                "runtime": {
                    "gateway": {"running": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True},
                },
                "scheduler": {"status": "running"},
                "service_monitor": {"status_counts": {"running": 6}},
            },
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": "om-scan"},
            write_system_event_record=lambda *args, **kwargs: events.append((args, kwargs)) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: "TRUE",
            console_base_url=lambda: "https://quant.lzw-glory.top",
            signal_chat_id=lambda environment: f"signal-chat-{environment}",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: [
                {"symbol": "SPY", "price": 500, "change_pct": 0.5, "latest_us_time": "2026-04-28 09:30:00", "freshness_min": 0, "status": "live"}
            ],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["notified"])
        self.assertEqual(sent[0]["chat_id"], "startup-chat-live")
        self.assertEqual(sent[0]["environment"], "live")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("**结论**", card_text)
        self.assertIn("**需要处理**", card_text)
        self.assertIn("今日标的", card_text)
        self.assertEqual(states[("system_notify_daily", "live")]["open_message_id"], "om-scan")
        self.assertEqual(states[("system_notify_daily", "live")]["open_sent_at"], "2026-04-28 09:30:17")
        self.assertEqual(events[0][0][0], "open_report")

    def test_scan_summary_reports_daily_scan_failure(self):
        sent = []
        states = {}

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777383000000,
                "daily_scan": {
                    "status": "failed",
                    "last_error": "scan timed out",
                    "market_date": payload["market_date"],
                },
                "summary": {"total": 0},
                "items": [],
            }, 200

        payload, status_code = build_system_scan_summary_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 09:35:00", "cn": "2026-04-28 21:35:00", "date": "2026-04-28"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: {"status": "running"},
            build_system_monitor_payload=lambda environment: {},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-failed"},
            write_system_event_record=lambda *args, **kwargs: {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: "SPY,QQQ,VIX" if key == "ibkr_market_ws_symbols" else "TRUE",
            console_base_url=lambda: "https://quant.lzw-glory.top",
            signal_chat_id=lambda environment: f"signal-chat-{environment}",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sent[0]["header"]["template"], "orange")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["elements"] if element.get("tag") == "markdown")
        self.assertIn("日筛失败: scan timed out", card_text)

    def test_status_reminder_skips_open_report_window(self):
        emitted = []

        payload, status_code = build_system_status_reminder_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 09:30:01", "cn": "2026-04-28 21:30:01", "date": "2026-04-28"},
            build_system_summary_payload=lambda environment, lite_mode=False: {"status": "running"},
            build_system_monitor_payload=lambda environment: {},
            emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "open_report_window")
        self.assertEqual(emitted, [])

    def test_status_reminder_includes_targets_and_active_windows(self):
        emitted = []

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload["market_date"],
                "summary": {
                    "total": 4,
                    "active_count": 4,
                    "candidate_count": 0,
                    "operable_count": 3,
                    "technical_ready_count": 2,
                    "signaled_count": 2,
                    "awaiting_confirm_count": 1,
                    "pending_count": 0,
                },
                "items": [
                    {
                        "symbol": "AAPL",
                        "direction_bias": "long",
                        "has_signal_today": True,
                        "latest_signal_status": "expired",
                    },
                    {
                        "symbol": "INTC",
                        "direction_bias": "short",
                        "has_signal_today": True,
                        "latest_signal_status": "awaiting_confirm",
                    },
                    {
                        "symbol": "NVDA",
                        "direction_bias": "long",
                        "has_signal_today": False,
                        "latest_signal_status": "",
                    },
                    {
                        "symbol": "TSLA",
                        "direction_bias": "short",
                        "has_signal_today": False,
                        "latest_signal_status": "",
                    },
                ],
            }, 200

        def build_active_window_progress_response(*, payload):
            return {
                "summary": {
                    "window_active_count": 3,
                    "window_valid_count": 2,
                    "candidate_signal_count": 1,
                    "near_expiry_count": 1,
                },
                "items": [
                    {
                        "symbol": "AAPL",
                        "window_status": "upper_active",
                        "sd_upper_valid": True,
                        "sd_lower_valid": False,
                        "bars_remaining": 4,
                    },
                    {
                        "symbol": "INTC",
                        "window_status": "near_expiry",
                        "sd_upper_valid": False,
                        "sd_lower_valid": True,
                        "bars_remaining": 1,
                    },
                    {
                        "symbol": "NVDA",
                        "window_status": "no_window",
                        "sd_upper_valid": False,
                        "sd_lower_valid": False,
                        "bars_remaining": 0,
                    },
                    {
                        "symbol": "TSLA",
                        "window_status": "used",
                        "sd_upper_valid": False,
                        "sd_lower_valid": False,
                        "bars_remaining": 0,
                    },
                ],
            }, 200

        payload, status_code = build_system_status_reminder_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 10:00:01", "cn": "2026-04-28 22:00:01", "date": "2026-04-28"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "today": {"ibkr_bars": 10, "ibkr_signals": 2, "orders": 3, "main_orders": 1, "order_groups": 1},
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "daily_scan": {"status": "completed"},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "gateway": {"running": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True},
                },
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0},
                "service_monitor": {"status_counts": {"running": 6}},
            },
            emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"notified": True},
            build_today_targets_response=build_today_targets_response,
            build_active_window_progress_response=build_active_window_progress_response,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        detail = emitted[0]["detail"]
        self.assertIn("orders 1", detail["数据"])
        self.assertNotIn("orders 3", detail["数据"])
        self.assertIn("expired 1", detail["今日标的"])
        self.assertIn("no_signal 2", detail["今日标的"])
        self.assertIn("AAPL(多,已过期)", detail["今日交易标的"])
        self.assertIn("INTC(空,待确认)", detail["今日交易标的"])
        self.assertIn("AAPL(多,已过期)", detail["已过期标的"])
        self.assertIn("NVDA(多)", detail["未出信号标的"])
        self.assertIn("valid 2", detail["窗口统计"])
        self.assertIn("AAPL(上窗口,4 bars)", detail["窗口已激活"])
        self.assertIn("INTC(下窗口,1 bars)", detail["窗口已激活"])
        self.assertIn("NVDA(无窗口)", detail["窗口未激活"])
        self.assertIn("TSLA(已使用)", detail["窗口未激活"])


if __name__ == "__main__":
    unittest.main()
