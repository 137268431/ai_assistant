import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.system.jobs.scan_summary import build_system_scan_summary_response
from ibkr_api.system.jobs.status_heartbeat import (
    _active_window_summary,
    build_system_heartbeat_response,
    build_system_status_reminder_response,
)
from ibkr_api.system.jobs import open_report as open_report_mod
from ibkr_api.system.jobs.open_report import (
    build_system_open_report_response,
    load_market_snapshots_from_pb,
    matches_open_report_time_window,
)


class SystemScanSummaryTest(unittest.TestCase):
    def test_open_report_window_is_0930_to_before_0940(self):
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:29:59"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:30:00"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:35:00"))
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:40:00"))

    def test_market_snapshot_uses_previous_regular_close_when_daily_is_stale(self):
        def fake_load_records(pb, collection, *, base_filter_parts, symbols, sort, max_pages, chunk_size=24):
            joined = " ".join(base_filter_parts)
            if collection != "ibkr_bars":
                return []
            if 'interval = "5m"' in joined and 'bar_time_ms >= 1778731200000' in joined:
                return [
                    {
                        "symbol": "SPY",
                        "bar_time_ms": 1778765100000,
                        "close": 743.77,
                        "us_time": "2026-05-14 09:25:00",
                    }
                ]
            if 'interval = "1d"' in joined:
                return [
                    {
                        "symbol": "SPY",
                        "bar_time_ms": 1777953600000,
                        "close": 726.46,
                        "us_time": "2026-05-05 00:00:00",
                    }
                ]
            if 'interval = "5m"' in joined and 'session_type = "regular"' in joined:
                return [
                    {
                        "symbol": "SPY",
                        "bar_time_ms": 1778716500000,
                        "close": 742.30,
                        "us_time": "2026-05-13 15:55:00",
                        "extra": '{"bar_close_us_time":"2026-05-13 16:00:00"}',
                    }
                ]
            return []

        with mock.patch.object(open_report_mod, "_load_records_for_symbols", side_effect=fake_load_records):
            snapshots = load_market_snapshots_from_pb(
                object(),
                "live",
                ["SPY"],
                "2026-05-14",
                1778765400000,
            )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["prev_close"], 742.3)
        self.assertEqual(snapshots[0]["prev_close_source"], "regular_5m")
        self.assertEqual(snapshots[0]["prev_close_time"], "2026-05-13 16:00:00")
        self.assertEqual(snapshots[0]["change_pct"], 0.2)
        self.assertEqual(snapshots[0]["freshness_min"], 5)

    def test_scan_summary_skips_weekend_without_sending_open_report(self):
        sent = []
        states = {}
        events = []
        target_calls = []

        def build_today_targets_response(*, payload):
            target_calls.append(payload)
            raise AssertionError("targets should not load on non-trading days")

        payload, status_code = build_system_scan_summary_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-05-09 09:30:00", "cn": "2026-05-09 21:30:00", "date": "2026-05-09"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: (_ for _ in ()).throw(AssertionError("summary should not load")),
            build_system_monitor_payload=lambda environment: (_ for _ in ()).throw(AssertionError("monitor should not load")),
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: (_ for _ in ()).throw(AssertionError("config should not load")),
            console_base_url=lambda: "https://quant.lzw-glory.top",
            signal_chat_id=lambda environment: f"signal-chat-{environment}",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: sent.append(symbols) or [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "non_trading_day")
        self.assertFalse(payload["trading_day"])
        self.assertEqual(payload["market_date"], "2026-05-09")
        self.assertEqual(payload["job_id"], "system_scan_summary")
        self.assertEqual(sent, [])
        self.assertEqual(events, [])
        self.assertEqual(target_calls, [])
        state = states[("system_notify_daily", "live")]
        self.assertEqual(state["open_sent_at"], "2026-05-09 09:30:00")
        self.assertEqual(state["open_reason"], "non_trading_day")
        self.assertFalse(state["open_notified"])

    def test_open_report_skips_nyse_holiday_without_sending_card(self):
        sent = []
        states = {}
        events = []

        payload, status_code = build_system_open_report_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-07-03 09:30:00", "cn": "2026-07-03 21:30:00", "date": "2026-07-03"},
            build_today_targets_response=lambda *, payload: (_ for _ in ()).throw(AssertionError("targets should not load")),
            build_system_summary_payload=lambda environment, lite_mode=False: (_ for _ in ()).throw(AssertionError("summary should not load")),
            build_system_monitor_payload=lambda environment: (_ for _ in ()).throw(AssertionError("monitor should not load")),
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: (_ for _ in ()).throw(AssertionError("config should not load")),
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: sent.append(symbols) or [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "non_trading_day")
        self.assertEqual(payload["market_date"], "2026-07-03")
        self.assertEqual(sent, [])
        self.assertEqual(events, [])
        state = states[("system_notify_daily", "live")]
        self.assertEqual(state["open_sent_at"], "2026-07-03 09:30:00")
        self.assertEqual(state["open_daily_scan_status"], "non_trading_day")

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

    def test_open_report_labels_paper_broker_with_shared_live_data(self):
        sent = []
        states = {}
        summary_calls = []
        target_payloads = []
        snapshot_calls = []

        def build_today_targets_response(*, payload):
            target_payloads.append(payload)
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777383000000,
                "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                "summary": {"total": 0},
                "items": [],
            }, 200

        payload, status_code = build_system_open_report_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 09:30:17", "cn": "2026-04-28 21:30:17", "date": "2026-04-28"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: summary_calls.append(environment) or {"status": "running"},
            build_system_monitor_payload=lambda environment: {"runtime": {}, "scheduler": {}, "service_monitor": {"status_counts": {"running": 1}}},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": "om-paper"},
            write_system_event_record=lambda *args, **kwargs: {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: snapshot_calls.append(environment) or [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(sent[0]["chat_id"], "startup-chat-paper")
        self.assertEqual(sent[0]["environment"], "paper")
        self.assertIn("Broker PAPER", sent[0]["card"]["header"]["title"]["content"])
        self.assertEqual(target_payloads[0]["broker_mode"], "paper")
        self.assertEqual(target_payloads[0]["market_data_mode"], "live")
        self.assertEqual(target_payloads[0]["environment"], "live")
        self.assertEqual(summary_calls, ["paper"])
        self.assertEqual(snapshot_calls, ["live"])
        self.assertEqual(states[("system_notify_daily", "paper")]["open_message_id"], "om-paper")

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

    def test_status_reminder_labels_paper_broker_with_shared_live_data(self):
        emitted = []
        summary_calls = []
        monitor_calls = []
        target_payloads = []
        window_payloads = []

        payload, status_code = build_system_status_reminder_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-28 10:00:01", "cn": "2026-04-28 22:00:01", "date": "2026-04-28"},
            build_system_summary_payload=lambda environment, lite_mode=False: summary_calls.append((environment, lite_mode)) or {
                "status": "running",
                "today": {"ibkr_bars": 10, "ibkr_signals": 0, "main_orders": 0},
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "daily_scan": {"status": "completed"},
            },
            build_system_monitor_payload=lambda environment: monitor_calls.append(environment) or {
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
            build_today_targets_response=lambda *, payload: target_payloads.append(payload) or ({"summary": {}, "items": []}, 200),
            build_active_window_progress_response=lambda *, payload: window_payloads.append(payload) or ({"summary": {}, "items": []}, 200),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["market_data_mode"], "live")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(summary_calls, [("paper", True)])
        self.assertEqual(monitor_calls, ["paper"])
        self.assertEqual(emitted[0]["environment"], "paper")
        self.assertEqual(target_payloads[0]["environment"], "live")
        self.assertEqual(target_payloads[0]["broker_mode"], "paper")
        self.assertEqual(target_payloads[0]["market_data_mode"], "live")
        self.assertEqual(window_payloads[0]["environment"], "live")
        self.assertEqual(window_payloads[0]["broker_mode"], "paper")
        self.assertEqual(window_payloads[0]["limit"], 50)

    def test_heartbeat_labels_paper_broker_with_shared_live_data(self):
        emitted = []
        state_writes = []

        payload, status_code = build_system_heartbeat_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "emit_nominal_ok": True},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-28 10:00:01", "cn": "2026-04-28 22:00:01", "date": "2026-04-28"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "today": {"ibkr_bars": 10, "ibkr_signals": 0, "main_orders": 0},
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
            get_state_payload=lambda state_key, environment: {"data": {}},
            upsert_state=lambda key, environment, data, date: state_writes.append(
                {"key": key, "environment": environment, "data": data, "date": date}
            ) or data,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["market_data_mode"], "live")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(emitted[0]["environment"], "paper")
        self.assertEqual(state_writes[0]["environment"], "paper")

    def test_active_window_summary_hides_normal_no_window_items(self):
        detail = _active_window_summary(
            {
                "summary": {
                    "window_active_count": 0,
                    "window_valid_count": 0,
                    "candidate_signal_count": 3,
                    "blocked_count": 0,
                    "near_expiry_count": 0,
                    "trace_error_count": 0,
                },
                "items": [
                    {"symbol": "AAPL", "window_status": "no_window"},
                    {"symbol": "TSLA", "window_status": "used"},
                ],
            },
            {
                "items": [
                    {"symbol": "AAPL", "is_operable": False, "has_signal_today": False},
                    {"symbol": "TSLA", "is_operable": False, "has_signal_today": True},
                ]
            },
        )

        self.assertEqual(detail, {})

    def test_active_window_summary_reports_actionable_window_anomalies(self):
        detail = _active_window_summary(
            {
                "summary": {
                    "window_active_count": 0,
                    "window_valid_count": 0,
                    "candidate_signal_count": 2,
                    "blocked_count": 1,
                    "near_expiry_count": 0,
                    "trace_error_count": 1,
                },
                "items": [
                    {"symbol": "NVDA", "window_status": "blocked", "blocked_reason": "volume_filter"},
                    {"symbol": "MSFT", "window_status": "no_window", "trace_error": "trace timed out"},
                    {"symbol": "AMD", "window_status": "no_window"},
                ],
            },
            {
                "items": [
                    {"symbol": "NVDA", "is_operable": True, "has_signal_today": False},
                    {"symbol": "MSFT", "is_operable": False, "has_signal_today": False},
                    {"symbol": "AMD", "is_operable": True, "has_signal_today": False},
                ]
            },
        )

        self.assertIn("candidate 2", detail["窗口统计"])
        self.assertIn("blocked 1", detail["窗口统计"])
        self.assertIn("trace_error 1", detail["窗口统计"])
        self.assertIn("NVDA(受阻,可操作待信号,volume_filter)", detail["窗口异常"])
        self.assertIn("MSFT(无窗口,trace错误:trace timed out)", detail["窗口异常"])
        self.assertIn("AMD(无窗口,可操作待信号)", detail["窗口异常"])
        self.assertNotIn("窗口未激活", detail)

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
                        "is_operable": True,
                        "technical_state": "ready",
                        "has_signal_today": True,
                        "latest_signal_status": "expired",
                    },
                    {
                        "symbol": "INTC",
                        "direction_bias": "short",
                        "is_operable": True,
                        "technical_state": "ready",
                        "has_signal_today": True,
                        "latest_signal_status": "awaiting_confirm",
                    },
                    {
                        "symbol": "NVDA",
                        "direction_bias": "long",
                        "is_operable": True,
                        "technical_state": "watch",
                        "has_signal_today": False,
                        "latest_signal_status": "",
                    },
                    {
                        "symbol": "TSLA",
                        "direction_bias": "short",
                        "is_operable": False,
                        "technical_state": "stale",
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
        self.assertIn("可操作待信号 1", detail["标的链路"])
        self.assertIn("NVDA(多,watch)", detail["标的链路"])
        self.assertIn("技术就绪待信号 0", detail["标的链路"])
        self.assertIn("已触发信号 2", detail["标的链路"])
        self.assertIn("AAPL(多,已过期)", detail["标的链路"])
        self.assertIn("INTC(空,待确认)", detail["标的链路"])
        self.assertIn("valid 2", detail["窗口统计"])
        self.assertIn("blocked 0", detail["窗口统计"])
        self.assertIn("trace_error 0", detail["窗口统计"])
        self.assertIn("AAPL(上窗口,4 bars)", detail["窗口已激活"])
        self.assertIn("INTC(下窗口,1 bars)", detail["窗口已激活"])
        self.assertIn("INTC(下窗口,1 bars)", detail["窗口异常"])
        self.assertNotIn("窗口未激活", detail)


if __name__ == "__main__":
    unittest.main()
