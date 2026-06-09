import os
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
    load_market_snapshots_from_quotes,
    matches_open_report_time_window,
)
from ibkr_api.system.jobs.market_calendar import (
    _clear_market_calendar_cache,
    build_market_calendar_response,
    build_market_calendar_snapshot,
)
from ibkr_api.system.jobs.market_session_text import market_calendar_source_label
from ibkr_api.app_core.route_cache import RouteSWRCache, canonical_cache_key, request_cache_bypass


class SystemScanSummaryTest(unittest.TestCase):
    def _run_heartbeat_for_test(
        self,
        *,
        monitor_payload,
        summary_payload=None,
        states=None,
        emitted=None,
        time_us="2026-05-19 15:41:18",
    ):
        states = states if states is not None else {}
        emitted = emitted if emitted is not None else []
        summary = summary_payload or {
            "status": "running",
            "today": {"ibkr_bars": 10, "ibkr_signals": 0, "main_orders": 0},
            "ibkr_compute": {"status": "running"},
            "ibkr_runtime": {"status": "running"},
            "daily_scan": {"status": "completed"},
        }

        payload, status_code = build_system_heartbeat_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": time_us, "cn": "2026-05-20 03:41:18", "date": "2026-05-19"},
            build_system_summary_payload=lambda environment, lite_mode=False: summary,
            build_system_monitor_payload=lambda environment: monitor_payload,
            emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"notified": True},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): dict(data)}) or data,
        )
        return payload, status_code, states, emitted

    def test_open_report_window_is_0930_to_before_0940(self):
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:29:59"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:30:00"))
        self.assertTrue(matches_open_report_time_window("2026-04-28 09:35:00"))
        self.assertFalse(matches_open_report_time_window("2026-04-28 09:40:00"))

    def test_market_calendar_source_label_distinguishes_skipped_refresh(self):
        self.assertEqual(
            market_calendar_source_label("local_nyse_fallback", "ibkr_calendar_refresh_omitted"),
            "本地 NYSE 兜底日历（IBKR 日历刷新已跳过）",
        )
        self.assertEqual(
            market_calendar_source_label("local_nyse_fallback", "gateway_unreachable"),
            "本地 NYSE 兜底日历（IBKR 拉取失败: gateway_unreachable）",
        )

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

    def test_quote_market_snapshot_uses_fresh_realtime_quote(self):
        calls = []

        def request_json_request(method, base_url, path, params=None, timeout=0, **kwargs):
            calls.append((method, base_url, path, params, timeout))
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "items": [
                        {
                            "symbol": "SPY",
                            "last_price": 757.74,
                            "day_change_pct": -0.2409,
                            "prev_close": 759.57,
                            "last_update": "2026-06-03T09:36:35.413000-04:00",
                            "quote_age_s": 3.3,
                        },
                        {
                            "symbol": "QQQ",
                            "last_price": 745.52,
                            "day_change_pct": -0.0858,
                            "prev_close": 746.16,
                            "last_update": "2026-06-03T09:36:35.413000-04:00",
                            "quote_age_s": 0.0,
                        },
                    ],
                },
            }

        snapshots = load_market_snapshots_from_quotes(
            request_json_request,
            "http://runtime.internal",
            "live",
            ["SPY", "QQQ"],
            "2026-06-03",
            0,
        )

        self.assertEqual(calls[0][2], "/ibkr/quotes")
        self.assertEqual(calls[0][3], {"symbols": "SPY,QQQ", "environment": "live"})
        self.assertEqual(snapshots[0]["symbol"], "SPY")
        self.assertEqual(snapshots[0]["price"], 757.74)
        self.assertEqual(snapshots[0]["change_pct"], -0.2409)
        self.assertEqual(snapshots[0]["prev_close"], 759.57)
        self.assertEqual(snapshots[0]["latest_us_time"], "2026-06-03 09:36:35")
        self.assertEqual(snapshots[0]["freshness_min"], 0)
        self.assertEqual(snapshots[0]["status"], "realtime_quote")
        self.assertEqual(snapshots[1]["status"], "realtime_quote")

    def test_quote_market_snapshot_calculates_change_from_quote_prev_close(self):
        def request_json_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": True,
                "payload": {
                    "items": [
                        {
                            "symbol": "SPY",
                            "last_price": 105,
                            "prev_close": 100,
                            "last_update": "2026-06-03 09:31:00",
                            "quote_age_s": 30,
                        }
                    ],
                },
            }

        snapshots = load_market_snapshots_from_quotes(
            request_json_request,
            "http://runtime.internal",
            "live",
            ["SPY"],
            "2026-06-03",
            0,
        )

        self.assertEqual(snapshots[0]["price"], 105)
        self.assertEqual(snapshots[0]["change_pct"], 5.0)
        self.assertEqual(snapshots[0]["status"], "realtime_quote")

    def test_quote_market_snapshot_marks_stale_realtime_quote(self):
        def request_json_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": True,
                "payload": {
                    "items": [
                        {
                            "symbol": "VIX",
                            "last_price": 16.28,
                            "day_change_pct": 3.234,
                            "prev_close": 15.77,
                            "last_update": "2026-06-03T09:20:00-04:00",
                            "quote_age_s": 900,
                        }
                    ],
                },
            }

        snapshots = load_market_snapshots_from_quotes(
            request_json_request,
            "http://runtime.internal",
            "live",
            ["VIX"],
            "2026-06-03",
            0,
        )

        self.assertEqual(snapshots[0]["price"], 16.28)
        self.assertEqual(snapshots[0]["freshness_min"], 15)
        self.assertEqual(snapshots[0]["status"], "stale_quote")

    def test_quote_market_snapshot_ignores_storage_fallback_and_never_reads_pb_bars(self):
        def request_json_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": True,
                "payload": {
                    "items": [
                        {
                            "symbol": "SPY",
                            "last_price": 743.77,
                            "day_change_pct": 0.2,
                            "quote_fallback": True,
                            "fallback_source": "canonical_5m",
                            "quote_age_s": None,
                        }
                    ],
                },
            }

        with mock.patch.object(
            open_report_mod,
            "load_market_snapshots_from_pb",
            side_effect=AssertionError("ibkr_bars fallback should not be used"),
        ):
            snapshots = load_market_snapshots_from_quotes(
                request_json_request,
                "http://runtime.internal",
                "live",
                ["SPY", "QQQ"],
                "2026-06-03",
                0,
            )

        self.assertEqual(snapshots[0]["status"], "missing_quote")
        self.assertEqual(snapshots[0]["price"], 0.0)
        self.assertIsNone(snapshots[0]["change_pct"])
        self.assertEqual(snapshots[1]["status"], "missing_quote")

    def test_quote_market_snapshot_endpoint_failure_returns_missing_without_pb_fallback(self):
        def request_json_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {"ok": False, "status_code": 503, "payload": {"error": "runtime_down"}}

        with mock.patch.object(
            open_report_mod,
            "load_market_snapshots_from_pb",
            side_effect=AssertionError("ibkr_bars fallback should not be used"),
        ):
            snapshots = load_market_snapshots_from_quotes(
                request_json_request,
                "http://runtime.internal",
                "live",
                ["SPY", "QQQ", "VIX"],
                "2026-06-03",
                0,
            )

        self.assertEqual([item["status"] for item in snapshots], ["missing_quote", "missing_quote", "missing_quote"])
        self.assertEqual([item["price"] for item in snapshots], [0.0, 0.0, 0.0])

    def test_scan_summary_sends_weekend_closed_notice_without_loading_targets(self):
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
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-closed-weekend"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            signal_chat_id=lambda environment: f"signal-chat-{environment}",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: sent.append(symbols) or [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["reason"], "market_closed")
        self.assertFalse(payload["trading_day"])
        self.assertEqual(payload["market_date"], "2026-05-09")
        self.assertEqual(payload["job_id"], "system_scan_summary")
        self.assertEqual(len(sent), 1)
        self.assertIn("IBKR 今日闭市", sent[0]["header"]["title"]["content"])
        self.assertEqual(events[0][0], "market_closed_notice")
        self.assertEqual(target_calls, [])
        state = states[("system_notify_daily", "live")]
        self.assertEqual(state["open_sent_at"], "2026-05-09 09:30:00")
        self.assertEqual(state["open_reason"], "market_closed")
        self.assertTrue(state["open_notified"])
        self.assertEqual(state["open_message_id"], "om-closed-weekend")
        self.assertEqual(state["market_calendar"]["closed_reason"], "weekend")

    def test_open_report_sends_nyse_holiday_closed_card(self):
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
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-holiday"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: sent.append(symbols) or [],
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["reason"], "market_closed")
        self.assertEqual(payload["market_date"], "2026-07-03")
        self.assertEqual(len(sent), 1)
        self.assertIn("下次开盘", sent[0]["elements"][0]["content"])
        self.assertEqual(events[0][0], "market_closed_notice")
        state = states[("system_notify_daily", "live")]
        self.assertEqual(state["open_sent_at"], "2026-07-03 09:30:00")
        self.assertEqual(state["open_daily_scan_status"], "market_closed")
        self.assertEqual(state["open_message_id"], "om-holiday")

    def test_market_calendar_uses_ibkr_schedule_when_available(self):
        calls = []

        snapshot = build_market_calendar_snapshot(
            market_date="2026-05-25",
            broker_mode="live",
            data_environment="live",
            payload={},
            request_json_request=lambda method, base_url, path, params=None, timeout=0, **kwargs: calls.append(
                (method, base_url, path, params)
            )
            or {
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "source": "ibkr_schedule",
                    "market_date": "2026-05-25",
                    "symbol": "SPY",
                    "is_trading_day": False,
                    "is_closed": True,
                    "closed_reason": "ibkr_closed",
                    "session": {},
                    "next_open_us": "2026-05-26 09:30:00",
                    "next_open_beijing": "2026-05-26 21:30:00",
                },
            },
            compute_base_url="http://compute.internal",
            config_value=lambda key, default, environment: default,
        )

        self.assertEqual(snapshot["source"], "ibkr_schedule")
        self.assertTrue(snapshot["is_closed"])
        self.assertEqual(snapshot["closed_reason"], "ibkr_closed")
        self.assertEqual(calls[0][2], "/ibkr/market/calendar")

    def test_market_calendar_response_caches_and_allows_cache_bust(self):
        _clear_market_calendar_cache()
        calls = []

        def request_calendar(method, base_url, path, params=None, timeout=0, **kwargs):
            calls.append((method, base_url, path, params))
            return {
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "source": "ibkr_schedule",
                    "market_date": "2026-05-26",
                    "symbol": "SPY",
                    "is_trading_day": True,
                    "is_closed": False,
                    "closed_reason": "",
                    "market_session": {"kind": "regular", "label_zh": "盘中"},
                    "session": {},
                },
            }

        def build_payload(**extra):
            return {
                "date": "2026-05-26",
                "broker_mode": "paper",
                "market_data_mode": "live",
                "data_environment": "live",
                **extra,
            }

        common = {
            "time_strings": lambda: {"date": "2026-05-26"},
            "request_json_request": request_calendar,
            "compute_base_url": "http://compute.internal",
            "config_value": lambda key, default, environment: default,
        }
        try:
            first, first_status = build_market_calendar_response(payload=build_payload(), **common)
            second, second_status = build_market_calendar_response(payload=build_payload(), **common)
            bypass, bypass_status = build_market_calendar_response(payload=build_payload(cache_bust="1"), **common)
        finally:
            _clear_market_calendar_cache()

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(bypass_status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(first["_cache"]["state"], "miss")
        self.assertEqual(second["_cache"]["state"], "hit")
        self.assertEqual(bypass["_cache"]["state"], "bypass")

    def test_route_swr_cache_hits_and_bypass_refreshes(self):
        cache = RouteSWRCache("test-route-cache")
        calls = []
        key = canonical_cache_key("demo", {"a": "1", "cache_bust": "1"})

        def builder():
            calls.append(len(calls) + 1)
            return {"ok": True, "value": calls[-1]}, 200

        first, first_status = cache.get(key, builder=builder, ttl_seconds=30, stale_seconds=30)
        second, second_status = cache.get(key, builder=builder, ttl_seconds=30, stale_seconds=30)
        bypass, bypass_status = cache.get(
            key,
            builder=builder,
            ttl_seconds=30,
            stale_seconds=30,
            force=request_cache_bypass({"cache_bust": "1"}),
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(bypass_status, 200)
        self.assertEqual(first["value"], 1)
        self.assertEqual(second["value"], 1)
        self.assertEqual(bypass["value"], 2)
        self.assertEqual(first["_cache"]["state"], "miss")
        self.assertEqual(second["_cache"]["state"], "hit")
        self.assertEqual(bypass["_cache"]["state"], "bypass")

    def test_route_cache_key_ignores_refresh_only_account_params(self):
        base_key = canonical_cache_key("demo", {"a": "1"})
        refresh_key = canonical_cache_key(
            "demo",
            {
                "a": "1",
                "force": "1",
                "refresh": "1",
                "_ts": "1780985200",
                "_verify": "1",
                "cache_bust": "1",
            },
        )

        self.assertEqual(base_key, refresh_key)

    def test_route_swr_cache_force_uses_stale_on_refresh_error(self):
        cache = RouteSWRCache("test-route-cache")
        key = canonical_cache_key("demo", {"a": "1", "cache_bust": "1"})

        first, first_status = cache.get(
            key,
            builder=lambda: ({"ok": True, "value": 1}, 200),
            ttl_seconds=30,
            stale_seconds=30,
        )
        stale, stale_status = cache.get(
            key,
            builder=lambda: ({"ok": False, "error": "runtime_timeout"}, 502),
            ttl_seconds=30,
            stale_seconds=30,
            force=request_cache_bypass({"cache_bust": "1"}),
        )

        self.assertEqual(200, first_status)
        self.assertEqual(200, stale_status)
        self.assertEqual(1, first["value"])
        self.assertEqual(1, stale["value"])
        self.assertEqual("bypass_stale_error", stale["_cache"]["state"])
        self.assertTrue(stale["_cache"]["stale"])
        self.assertIn("runtime_timeout", stale["_cache"]["error"])

    def test_route_swr_cache_force_can_return_stale_while_refreshing(self):
        cache = RouteSWRCache("test-route-cache")
        key = canonical_cache_key("demo", {"a": "1", "cache_bust": "1"})
        calls = []

        cache.get(
            key,
            builder=lambda: ({"ok": True, "value": 1}, 200),
            ttl_seconds=30,
            stale_seconds=30,
        )

        def slow_builder():
            calls.append("refresh")
            return {"ok": True, "value": 2}, 200

        stale, status = cache.get(
            key,
            builder=slow_builder,
            ttl_seconds=30,
            stale_seconds=30,
            force=request_cache_bypass({"cache_bust": "1"}),
            prefer_stale_on_force=True,
        )

        self.assertEqual(200, status)
        self.assertEqual(1, stale["value"])
        self.assertEqual("bypass_stale_refresh", stale["_cache"]["state"])

    def test_route_swr_cache_can_clear_matching_keys(self):
        cache = RouteSWRCache("test-route-cache")
        keep_key = canonical_cache_key("demo", {"orders_fast": "1"})
        drop_key = canonical_cache_key("demo", {"orders_fast": "0"})

        cache.get(keep_key, builder=lambda: ({"ok": True, "value": "keep"}, 200), ttl_seconds=30, stale_seconds=30)
        cache.get(drop_key, builder=lambda: ({"ok": True, "value": "drop"}, 200), ttl_seconds=30, stale_seconds=30)
        cache.clear_matching(lambda key: key == drop_key)

        keep, _ = cache.get(keep_key, builder=lambda: ({"ok": True, "value": "new-keep"}, 200), ttl_seconds=30, stale_seconds=30)
        drop, _ = cache.get(drop_key, builder=lambda: ({"ok": True, "value": "new-drop"}, 200), ttl_seconds=30, stale_seconds=30)

        self.assertEqual("keep", keep["value"])
        self.assertEqual("new-drop", drop["value"])

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
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("**市场时段**: 盘中 (regular)", card_text)
        self.assertIn("**日历来源**", card_text)
        self.assertEqual(target_payloads[0]["broker_mode"], "paper")
        self.assertEqual(target_payloads[0]["market_data_mode"], "live")
        self.assertEqual(target_payloads[0]["environment"], "live")
        self.assertEqual(summary_calls, ["paper"])
        self.assertEqual(snapshot_calls, ["live"])
        self.assertEqual(states[("system_notify_daily", "paper")]["open_message_id"], "om-paper")

    def test_open_report_waits_for_pending_open_capture_then_reloads_targets(self):
        _clear_market_calendar_cache()
        sent = []
        states = {}
        events = []
        target_payloads = []
        calls = []
        status_calls = []

        def build_today_targets_response(*, payload):
            target_payloads.append(payload)
            if len(target_payloads) == 1:
                return {
                    "market_date": payload["market_date"],
                    "computed_at_ms": 1777469400000,
                    "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                    "summary": {"total": 0, "active_count": 0, "candidate_count": 0},
                    "items": [],
                }, 200
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777469410000,
                "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                "summary": {"total": 1, "active_count": 1, "candidate_count": 0},
                "items": [
                    {
                        "symbol": "NVDA",
                        "status": "active",
                        "score": 33,
                        "direction_bias": "long",
                        "technical_state": "ready",
                        "price": 910,
                        "day_change_pct": 1.5,
                    }
                ],
            }, 200

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=0, **kwargs):
            calls.append({"method": method, "path": path, "params": params, "json_body": json_body})
            if path == "/ibkr/market/calendar":
                return {
                    "ok": True,
                    "payload": {
                        "ok": True,
                        "source": "ibkr_schedule",
                        "market_date": "2026-04-29",
                        "is_trading_day": True,
                        "is_closed": False,
                        "market_session": {"kind": "regular", "label_zh": "盘中"},
                    },
                }
            self.assertEqual(path, "/scan/status")
            status_calls.append(params)
            status = "pending" if len(status_calls) == 1 else "completed"
            return {
                "ok": True,
                "payload": {
                    "ok": True,
                    "status": status,
                    "run_id": "scan-live-2026-04-29-preopen",
                    "result": {"ok": True, "new_targets": [{"symbol": "NVDA"}]},
                },
            }

        payload, status_code = build_system_open_report_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-29 09:30:14", "cn": "2026-04-29 21:30:14", "date": "2026-04-29"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: {"status": "running"},
            build_system_monitor_payload=lambda environment: {"scheduler": {"status": "running"}, "service_monitor": {"status_counts": {"running": 3}}},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-open-capture"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: "SPY,QQQ,VIX" if key == "ibkr_market_ws_symbols" else default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: [],
            request_json_request=request_json_request,
            compute_base_url="http://compute.internal",
            sleep_fn=lambda seconds: (_ for _ in ()).throw(AssertionError("capture should complete before sleeping")),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["opening_capture"]["status"], "completed")
        self.assertEqual(len(target_payloads), 2)
        self.assertEqual([call["path"] for call in calls], ["/ibkr/market/calendar", "/scan/status", "/scan/status"])
        card_text = "\n".join(element.get("content", "") for element in sent[0]["elements"] if element.get("tag") == "markdown")
        self.assertIn("NVDA", card_text)
        self.assertIn("**开盘采集**: completed", card_text)
        self.assertEqual(states[("system_notify_daily", "paper")]["open_target_total"], 1)
        self.assertEqual(states[("system_notify_daily", "paper")]["open_target_capture_status"], "completed")
        self.assertIn("开盘采集", events[0][4])

    def test_open_report_submits_capture_when_no_topup_exists(self):
        _clear_market_calendar_cache()
        sent = []
        states = {}
        target_payloads = []
        calls = []

        def build_today_targets_response(*, payload):
            target_payloads.append(payload)
            empty = len(target_payloads) == 1
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777555800000,
                "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                "summary": {"total": 0 if empty else 1, "active_count": 0 if empty else 1, "candidate_count": 0},
                "items": [] if empty else [{"symbol": "AAPL", "status": "active", "score": 20, "price": 200}],
            }, 200

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=0, **kwargs):
            calls.append({"method": method, "path": path, "params": params, "json_body": json_body})
            if path == "/ibkr/market/calendar":
                return {"ok": True, "payload": {"ok": True, "market_date": "2026-04-30", "is_trading_day": True, "is_closed": False}}
            if path == "/scan/status" and method == "GET" and len([call for call in calls if call["path"] == "/scan/status"]) == 1:
                return {"ok": False, "status_code": 404, "payload": {"status": "not_found", "error": "scan_attempt_not_found"}}
            if path == "/scan":
                self.assertEqual(json_body["mode"], "topup")
                self.assertTrue(json_body["force"])
                self.assertTrue(json_body["async"])
                self.assertEqual(json_body["trigger_source"], "open_report_empty_target_capture")
                self.assertEqual(json_body["run_id"], "open-report-capture-live-2026-04-30")
                return {"ok": True, "payload": {"ok": True, "accepted": True, "async": True, "run_id": json_body["run_id"], "status": "accepted"}}
            self.assertEqual(path, "/scan/status")
            return {"ok": True, "payload": {"ok": True, "status": "completed", "run_id": "open-report-capture-live-2026-04-30"}}

        payload, status_code = build_system_open_report_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-30 09:30:05", "cn": "2026-04-30 21:30:05", "date": "2026-04-30"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: {"status": "running"},
            build_system_monitor_payload=lambda environment: {"scheduler": {"status": "running"}},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-submit-capture"},
            write_system_event_record=lambda *args, **kwargs: {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: [],
            request_json_request=request_json_request,
            compute_base_url="http://compute.internal",
            sleep_fn=lambda seconds: (_ for _ in ()).throw(AssertionError("capture should complete before sleeping")),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["opening_capture"]["submitted"])
        self.assertEqual(payload["opening_capture"]["status"], "completed")
        self.assertEqual([call["path"] for call in calls], ["/ibkr/market/calendar", "/scan/status", "/scan", "/scan/status"])
        self.assertEqual(len(target_payloads), 2)
        card_text = "\n".join(element.get("content", "") for element in sent[0]["elements"] if element.get("tag") == "markdown")
        self.assertIn("AAPL", card_text)

    def test_open_report_reports_open_capture_timeout_without_blocking_card(self):
        _clear_market_calendar_cache()
        sent = []
        states = {}

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload["market_date"],
                "computed_at_ms": 1777642200000,
                "daily_scan": {"status": "completed", "market_date": payload["market_date"]},
                "summary": {"total": 0, "active_count": 0, "candidate_count": 0},
                "items": [],
            }, 200

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=0, **kwargs):
            if path == "/ibkr/market/calendar":
                return {"ok": True, "payload": {"ok": True, "market_date": "2026-05-01", "is_trading_day": True, "is_closed": False}}
            self.assertEqual(path, "/scan/status")
            return {"ok": True, "payload": {"ok": True, "status": "pending", "run_id": "scan-live-2026-05-01-preopen"}}

        def config_value(key, default, environment):
            if key == "ibkr_open_report_target_wait_sec":
                return "0"
            return default

        payload, status_code = build_system_open_report_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-05-01 09:30:05", "cn": "2026-05-01 21:30:05", "date": "2026-05-01"},
            build_today_targets_response=build_today_targets_response,
            build_system_summary_payload=lambda environment, lite_mode=False: {"status": "running"},
            build_system_monitor_payload=lambda environment: {"scheduler": {"status": "running"}},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "om-timeout"},
            write_system_event_record=lambda *args, **kwargs: {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=config_value,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: [],
            request_json_request=request_json_request,
            compute_base_url="http://compute.internal",
            sleep_fn=lambda seconds: (_ for _ in ()).throw(AssertionError("zero wait budget should not sleep")),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["opening_capture"]["status"], "timeout")
        self.assertEqual(sent[0]["header"]["template"], "orange")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["elements"] if element.get("tag") == "markdown")
        self.assertIn("开盘采集等待超时", card_text)
        self.assertEqual(states[("system_notify_daily", "paper")]["open_target_capture_status"], "timeout")

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
                    "market_session": {
                        "kind": "regular",
                        "label_zh": "盘中",
                        "source": "ibkr_schedule",
                        "regular_open_us": "2026-04-28 09:30:00",
                        "regular_close_us": "2026-04-28 16:00:00",
                        "extended_open_us": "2026-04-28 04:00:00",
                        "extended_close_us": "2026-04-28 20:00:00",
                    },
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
                    "market_session": {
                        "kind": "regular",
                        "label_zh": "盘中",
                        "source": "ibkr_schedule",
                        "regular_open_us": "2026-04-28 09:30:00",
                        "regular_close_us": "2026-04-28 16:00:00",
                        "extended_open_us": "2026-04-28 04:00:00",
                        "extended_close_us": "2026-04-28 20:00:00",
                    },
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
        self.assertEqual(emitted[0]["detail"]["市场时段"], "盘中 (regular)")
        self.assertIn("常规 2026-04-28 09:30:00 - 2026-04-28 16:00:00", emitted[0]["detail"]["交易时间(美东)"])
        self.assertEqual(emitted[0]["detail"]["日历来源"], "IBKR 合约交易时间")

    def test_heartbeat_debounces_first_monitor_source_timeout(self):
        monitor_payload = {
            "ok": False,
            "status": "warning",
            "monitor_source_unavailable": True,
            "runtime": {},
            "compute": {},
            "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
            "service_monitor": {
                "status_counts": {"running": 4, "unknown": 3},
                "services": {
                    "ibkr-compute": {"status": "unknown"},
                    "ibkr-runtime": {"status": "unknown"},
                    "ibkr-gateway": {"status": "unknown"},
                },
            },
            "upstream_monitor": {
                "ok": False,
                "status_code": 0,
                "target_url": "http://compute.internal:5100/ibkr/monitor",
                "error": "Read timed out",
                "elapsed_ms": 20001.0,
                "timeout_s": 20.0,
                "source_unavailable": True,
            },
            "flags": [{"code": "monitor_builder_compute_monitor", "severity": "warning", "detail": "Read timed out"}],
        }

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_COUNT": "2",
                "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_SEC": "90",
            },
        ):
            payload, status_code, states, emitted = self._run_heartbeat_for_test(monitor_payload=monitor_payload)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["unhealthy"])
        self.assertTrue(payload["event_suppressed_by_debounce"])
        self.assertEqual(payload["event"], {})
        self.assertEqual(emitted, [])
        self.assertNotIn("gateway_offline", payload["issue_codes"])
        self.assertNotIn("session_unauthenticated", payload["issue_codes"])
        self.assertNotIn("websocket_not_ready", payload["issue_codes"])
        state = states[("system_notify_heartbeat", "paper")]
        self.assertEqual(state["pending_monitor_source_count"], 1)
        self.assertFalse(state.get("last_issue_hash"))

    def test_heartbeat_alerts_on_second_monitor_source_timeout(self):
        states = {}
        emitted = []
        monitor_payload = {
            "ok": False,
            "status": "warning",
            "monitor_source_unavailable": True,
            "runtime": {},
            "compute": {},
            "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
            "service_monitor": {"status_counts": {"running": 4, "unknown": 3}, "services": {}},
            "upstream_monitor": {
                "ok": False,
                "status_code": 0,
                "target_url": "http://compute.internal:5100/ibkr/monitor",
                "error": "Read timed out",
                "elapsed_ms": 20001.0,
                "timeout_s": 20.0,
                "source_unavailable": True,
            },
            "flags": [{"code": "monitor_builder_compute_monitor", "severity": "warning", "detail": "Read timed out"}],
        }

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_COUNT": "2",
                "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_SEC": "90",
            },
        ):
            self._run_heartbeat_for_test(monitor_payload=monitor_payload, states=states, emitted=emitted)
            payload, status_code, states, emitted = self._run_heartbeat_for_test(
                monitor_payload=monitor_payload,
                states=states,
                emitted=emitted,
                time_us="2026-05-19 15:42:18",
            )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["event_suppressed_by_debounce"])
        self.assertEqual(payload["monitor_debounce"]["count"], 2)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["title"], "IBKR 系统心跳异常")
        self.assertNotEqual(emitted[0]["title"], "IBKR 连接链路未就绪")
        self.assertIn("监控源", emitted[0]["detail"])
        self.assertNotIn("gateway_offline", payload["issue_codes"])
        self.assertNotIn("session_unauthenticated", payload["issue_codes"])
        self.assertTrue(states[("system_notify_heartbeat", "paper")]["last_issue_hash"])

    def test_heartbeat_alerts_immediately_for_explicit_connection_issue(self):
        emitted = []
        monitor_payload = {
            "ok": False,
            "status": "warning",
            "runtime": {
                "status": "degraded",
                "gateway": {"running": False, "reachable": False},
                "session": {"authenticated": False},
                "websocket": {"connected": False, "ready": False},
            },
            "compute": {"status": "running"},
            "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
            "service_monitor": {"status_counts": {"running": 4}, "services": {}},
            "flags": [],
        }

        payload, status_code, _states, emitted = self._run_heartbeat_for_test(
            monitor_payload=monitor_payload,
            emitted=emitted,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["event_suppressed_by_debounce"])
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["title"], "IBKR 连接链路未就绪")
        self.assertIn("gateway_offline", payload["issue_codes"])
        self.assertIn("session_unauthenticated", payload["issue_codes"])
        self.assertIn("websocket_not_ready", payload["issue_codes"])

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

        self.assertIn("candidate 2", detail["Execution关注"])
        self.assertIn("deferred 1", detail["Execution关注"])
        self.assertIn("trace_error 1", detail["Execution关注"])
        self.assertIn("NVDA(受阻,volume_filter)", detail["待复核标的"])
        self.assertIn("MSFT(待TV触发,trace_error:trace timed out)", detail["待复核标的"])
        self.assertNotIn("窗口统计", detail)

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
                    "market_session": {
                        "kind": "regular",
                        "label_zh": "盘中",
                        "source": "ibkr_schedule",
                        "regular_open_us": "2026-04-28 09:30:00",
                        "regular_close_us": "2026-04-28 16:00:00",
                    },
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
        self.assertEqual(detail["市场时段"], "盘中 (regular)")
        self.assertIn("常规 2026-04-28 09:30:00 - 2026-04-28 16:00:00", detail["交易时间(美东)"])
        self.assertIn("orders 1", detail["Orders"])
        self.assertNotIn("orders 3", detail["Orders"])
        self.assertIn("signals 2", detail["TV webhook"])
        self.assertIn("expired 1", detail["TV signals"])
        self.assertIn("execution_eligible 3", detail["Targets"])
        self.assertIn("protected 0", detail["Execution/Protection"])
        self.assertIn("AAPL(多,已过期)", detail["TV信号标的"])
        self.assertIn("INTC(空,待确认)", detail["TV信号标的"])
        self.assertIn("INTC(空,待确认)", detail["待处理标的"])
        self.assertIn("candidate 1", detail["Execution关注"])
        self.assertIn("deferred 0", detail["Execution关注"])
        self.assertIn("review 1", detail["Execution关注"])
        self.assertIn("INTC(需复核)", detail["待复核标的"])
        self.assertNotIn("窗口统计", detail)
        self.assertNotIn("bars", " ".join(str(value) for value in detail.values()))


if __name__ == "__main__":
    unittest.main()
