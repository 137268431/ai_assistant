import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

from ibkr_api.system.jobs.auth import build_auth_immediate_issue, is_operational_2fa_issue
from ibkr_api.system.jobs.daily_event_ledger import build_daily_event_reconcile_response
from ibkr_api.system.jobs.early_expansion_topup import (
    build_early_expansion_topup_response,
    notify_new_targets_from_scan,
)
from ibkr_api.system.jobs.intraday_window_admission import build_intraday_window_admission_response
from ibkr_api.system.jobs.monitor_alert import build_system_monitor_alert_guard_response
from ibkr_api.system.jobs.order_expiry import build_order_expiry_response
from ibkr_api.system.jobs.reminders import (
    build_system_daily_report_response,
    build_system_market_open_reminder_response,
)
from ibkr_api.system.summary_support import build_system_summary_payload
from ibkr_compute.market.timeframe_utils import ET


class _OrderExpiryPB:
    def __init__(self):
        self.records = {
            "orders": [
                {
                    "id": "entry-1",
                    "unique_id": "entry-1",
                    "entry_order_unique_id": "entry-1",
                    "trade_group_id": "tg-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "signal_id": "sig-1",
                    "status": "Submitted",
                    "role": "entry",
                    "broker_order_id": "12345",
                    "order_id": "12345",
                    "quantity": 10,
                    "direction": "BUY",
                    "order_type": "LMT",
                    "bar_time_ms": 1,
                    "us_time": "2026-04-23 09:30:00",
                    "cn_time": "2026-04-23 21:30:00",
                    "extra": {},
                },
                {
                    "id": "tp-1",
                    "unique_id": "tp-1",
                    "entry_order_unique_id": "entry-1",
                    "trade_group_id": "tg-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "signal_id": "sig-1",
                    "status": "Init",
                    "role": "tp",
                    "broker_order_id": "12346",
                    "order_id": "12346",
                    "quantity": 10,
                    "direction": "SELL",
                    "order_type": "LMT",
                    "bar_time_ms": 1,
                    "us_time": "2026-04-23 09:30:00",
                    "cn_time": "2026-04-23 21:30:00",
                    "extra": {},
                },
                {
                    "id": "sl-1",
                    "unique_id": "sl-1",
                    "entry_order_unique_id": "entry-1",
                    "trade_group_id": "tg-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "signal_id": "sig-1",
                    "status": "Init",
                    "role": "sl",
                    "broker_order_id": "12347",
                    "order_id": "12347",
                    "quantity": 10,
                    "direction": "SELL",
                    "order_type": "STP",
                    "bar_time_ms": 1,
                    "us_time": "2026-04-23 09:30:00",
                    "cn_time": "2026-04-23 21:30:00",
                    "extra": {},
                },
            ],
            "ibkr_order_details": [],
            "ibkr_signals": [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "submitted",
                    "extra": {"feishu_signal_message_id": "sig-msg-1"},
                }
            ],
        }
        self.created = []
        self.updated = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            return [dict(row) for row in self._filter_orders(filter)]
        if collection == "ibkr_signals":
            return [dict(row) for row in self.records["ibkr_signals"]]
        return [dict(row) for row in self.records.get(collection, [])]

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return dict(rows[0]) if rows else None

    def update_record(self, collection, record_id, data):
        for row in self.records[collection]:
            if row["id"] == record_id:
                row.update(data)
                self.updated.append((collection, record_id, data))
                return dict(row)
        raise KeyError(record_id)

    def create_record(self, collection, data):
        payload = dict(data)
        payload.setdefault("id", f"{collection}-{len(self.created) + 1}")
        self.created.append((collection, payload))
        self.records.setdefault(collection, []).append(payload)
        return payload

    def _filter_orders(self, filter_value):
        text = str(filter_value or "")
        rows = []
        environment = self._filter_environment(text)
        fields = self._filter_fields(text)
        values = set(self._filter_values(text))
        max_bar_time_ms = self._filter_max_bar_time_ms(text)
        wants_entry = 'role = "entry"' in text
        wants_open_expiry_status = 'status = "Init"' in text and 'status = "Submitted"' in text
        for row in self.records["orders"]:
            if environment and str(row.get("environment") or "") != environment:
                continue
            if max_bar_time_ms is not None and int(row.get("bar_time_ms") or 0) > max_bar_time_ms:
                continue
            if wants_entry and str(row.get("role") or "") not in {"entry", ""}:
                continue
            if wants_open_expiry_status and str(row.get("status") or "") not in {"Init", "Submitted"}:
                continue
            if fields and values:
                extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
                if not any(str(row.get(field) or extra.get(field) or "") in values for field in fields):
                    continue
            rows.append(row)
        return rows

    @staticmethod
    def _filter_values(filter_value):
        matches = re.findall(r'=\s*"([^"]*)"', str(filter_value or ""))
        environment = _OrderExpiryPB._filter_environment(filter_value)
        if environment and matches and matches[-1] == environment:
            return matches[:-1]
        return matches

    @staticmethod
    def _filter_environment(filter_value):
        match = re.search(r'environment\s*=\s*"([^"]*)"', str(filter_value or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _filter_fields(filter_value):
        return [
            field
            for field in re.findall(r'([a-zA-Z_]+)\s*=\s*"[^"]*"', str(filter_value or ""))
            if field not in {"environment", "status", "role"}
        ]

    @staticmethod
    def _filter_max_bar_time_ms(filter_value):
        match = re.search(r"bar_time_ms\s*<=\s*(\d+)", str(filter_value or ""))
        return int(match.group(1)) if match else None


class _ReminderPB:
    def __init__(self):
        self.states = {}

    def upsert_state(self, state_key, environment, data, date="global"):
        self.states[(state_key, environment, date)] = {"data": dict(data)}
        return self.states[(state_key, environment, date)]


class _IntradayAdmissionPB:
    def __init__(self):
        self.records = {
            "watchlist": [],
            "ibkr_targets": [],
            "ibkr_bars": [],
            "ibkr_indicators": [],
            "system_events": [],
        }
        self.created = []
        self.updated = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        return [dict(row) for row in self.records.get(collection, [])]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        return [dict(row) for row in self.records.get(collection, [])]

    def get_first_record(self, collection, filter=None, sort=None):
        filter_text = str(filter or "")
        rows = self.records.get(collection, [])
        for row in rows:
            if self._matches(row, filter_text):
                return dict(row)
        return None

    def create_record(self, collection, data):
        payload = dict(data)
        payload.setdefault("id", f"{collection}-{len(self.records.get(collection, [])) + 1}")
        self.records.setdefault(collection, []).append(payload)
        self.created.append((collection, payload))
        return dict(payload)

    def update_record(self, collection, record_id, data):
        for row in self.records.setdefault(collection, []):
            if row.get("id") == record_id:
                row.update(dict(data))
                self.updated.append((collection, record_id, dict(data)))
                return dict(row)
        raise KeyError(record_id)

    @staticmethod
    def _matches(row, filter_text):
        text = str(filter_text or "")
        for field in ("symbol", "environment", "date"):
            marker = f'{field} = "'
            if marker not in text:
                continue
            value = text.split(marker, 1)[1].split('"', 1)[0]
            if str(row.get(field, "") or "").strip() != value:
                return False
        return True


class SystemSchedulerJobsTest(unittest.TestCase):
    def _reminder_deps(
        self,
        pb,
        sent,
        now_us="2026-04-23 09:30:00",
        date="2026-04-23",
        event_result=None,
        config_overrides=None,
        today=None,
        daily=False,
    ):
        def emit_system_event(**kwargs):
            sent.append(kwargs)
            return dict(event_result or {"notified": True, "persisted": True, "message_id": "msg-1"})

        def feishu_send_interactive(card, chat_id, environment):
            sent.append({"card": card, "chat_id": chat_id, "environment": environment})
            return dict(event_result or {"success": True, "message_id": "msg-1"})

        def write_system_event_record(*args, **kwargs):
            return {"id": "event-1", "args": args, "kwargs": kwargs}

        def config_value(key, default, environment):
            overrides = config_overrides or {}
            return overrides.get(key, default)

        def get_state_payload(state_key, environment, state_date=date):
            return {
                "data": dict(pb.states.get((state_key, environment, state_date), {}).get("data") or {}),
                "environment": environment,
                "date": state_date,
            }

        def build_system_summary_payload(environment, lite_mode=False):
            return {
                "status": "running",
                "today": dict(today or {"ibkr_bars": 10, "ibkr_signals": 2, "orders": 1, "events": 0}),
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "daily_scan": {"status": "completed", "market_date": date},
            }

        def build_system_monitor_payload(environment):
            return {
                "runtime": {
                    "gateway": {"running": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True},
                    "daily_scan": {"status": "completed", "market_date": date},
                },
                "scheduler": {"status": "running", "job_count": 3},
                "service_monitor": {"status_counts": {"running": 6}},
            }

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload.get("market_date") or date,
                "daily_scan": {"status": "completed", "market_date": payload.get("market_date") or date},
                "summary": {
                    "total": 2,
                    "active_count": 1,
                    "candidate_count": 1,
                    "operable_count": 2,
                    "technical_ready_count": 1,
                    "signaled_count": 0,
                },
                "items": [],
            }, 200

        deps = {
            "normalize_environment": lambda value, default: str(value or default).strip().lower() or default,
            "time_strings": lambda: {"us": now_us, "cn": "2026-04-23 21:30:00", "date": date},
            "build_system_summary_payload": build_system_summary_payload,
            "build_system_monitor_payload": build_system_monitor_payload,
            "emit_system_event": emit_system_event,
            "get_state_payload": get_state_payload,
            "upsert_state": lambda key, environment, data, state_date: pb.upsert_state(key, environment, data, date=state_date),
        }
        if daily:
            deps.pop("emit_system_event", None)
            deps.update(
                {
                    "feishu_send_interactive": feishu_send_interactive,
                    "write_system_event_record": write_system_event_record,
                    "config_value": config_value,
                    "console_base_url": lambda: "https://quant.lzw-glory.top",
                    "startup_chat_id": lambda environment: f"startup-chat-{environment}",
                    "build_today_targets_response": build_today_targets_response,
                }
            )
        return deps

    def _admission_bar(self, symbol, minutes, **overrides):
        base_dt = datetime(2026, 4, 23, 9, 35, tzinfo=ET) + timedelta(minutes=minutes)
        close = float(overrides.pop("close", 10.0))
        return {
            "symbol": symbol,
            "environment": "live",
            "interval": "5m",
            "bar_time_ms": int(base_dt.timestamp() * 1000),
            "us_time": base_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "cn_time": "",
            "session_type": "regular",
            "exchange": "NASDAQ",
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": float(overrides.pop("volume", 200000)),
            **overrides,
        }

    def _admission_daily_bar(self, symbol, days_ago, volume=800000, close=9.0):
        dt = datetime(2026, 4, 23, 0, 0, tzinfo=ET) - timedelta(days=days_ago)
        return {
            "symbol": symbol,
            "environment": "live",
            "interval": "1d",
            "bar_time_ms": int(dt.timestamp() * 1000),
            "us_time": dt.strftime("%Y-%m-%d 00:00:00"),
            "close": close,
            "volume": volume,
        }

    def _run_admission(self, pb, *, payload=None, request_calls=None, config_overrides=None):
        request_calls = request_calls if request_calls is not None else []
        config_overrides = config_overrides or {}

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            request_calls.append({"method": method, "path": path, "json_body": json_body})
            if method == "GET" and path == "/ibkr/status":
                return {"status_code": 200, "payload": {"market_date": "2026-04-23"}}
            if method == "POST" and path == "/ibkr/universe/reconcile":
                return {
                    "status_code": 200,
                    "payload": {"ok": True, "primed": list((json_body or {}).get("prime_symbols") or [])},
                    "target_url": "http://compute/ibkr/universe/reconcile",
                }
            return {"status_code": 200, "payload": {"ok": True}}

        return build_intraday_window_admission_response(
            pb,
            payload={
                "environment": "live",
                "market_date": "2026-04-23",
                "force": True,
                **(payload or {}),
            },
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"'),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            config_value=lambda key, default, environment: config_overrides.get(key, default),
            write_system_event_record=lambda *args, **kwargs: pb.create_record(
                "system_events",
                {"event_type": args[0], "title": args[3], "detail": args[4]},
            ),
        )

    def _admission_window_payload(self, symbol="MSFT", *, status="upper_active"):
        valid = status in {"upper_active", "both_active", "near_expiry"}
        return {
            "summary": {"total": 1, "window_valid_count": 1 if valid else 0},
            "items": [
                {
                    "symbol": symbol,
                    "window_status": status,
                    "trace_stage": "none",
                    "sd_upper_valid": valid,
                    "sd_lower_valid": False,
                    "sd_upper_active": valid,
                    "sd_lower_active": False,
                    "bars_remaining": 10 if valid else 0,
                    "component_progress": 0.5,
                    "window_flags": {
                        "sd_upper_valid": valid,
                        "sd_lower_valid": False,
                        "sd_upper_active": valid,
                        "sd_lower_active": False,
                        "sd_upper_used": False,
                        "sd_lower_used": False,
                        "sd_upper_age_bars": 2 if valid else 0,
                        "sd_lower_age_bars": 0,
                    },
                    "signal_state": {"stage": "none", "direction": ""},
                    "components": {"ready_groups": [], "best_group": ""},
                    "component_detail": {},
                    "latest_bar_time_ms": self._admission_bar(symbol, 5)["bar_time_ms"],
                    "latest_us_time": "2026-04-23 09:40:00",
                    "price": 10.2,
                    "atr_pct": 2.5,
                    "target_score": 0,
                }
            ],
        }

    def test_order_expiry_marks_group_canceled(self):
        pb = _OrderExpiryPB()
        updated_cards = []
        cancel_calls = []

        def cancel_broker_order(environment, order_id, payload):
            self.assertEqual(environment, "live")
            self.assertEqual(payload["trade_group_id"], "tg-1")
            cancel_calls.append(order_id)
            return {"ok": True, "status_code": 200, "payload": {"ok": True}}

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=cancel_broker_order,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda message_id, card, environment: updated_cards.append((message_id, card, environment)) or {"success": True, "message_id": message_id},
            signal_chat_id_fn=lambda environment: f"signal-chat-{environment}",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed_count"], 3)
        self.assertEqual(payload["expired_group_count"], 1)
        self.assertEqual(payload["cancelled_order_ids"], ["12345", "12346", "12347"])
        self.assertEqual(cancel_calls, ["12345", "12346", "12347"])
        self.assertEqual(len(payload["detail_record_ids"]), 3)
        statuses = {row["id"]: row["status"] for row in pb.records["orders"]}
        self.assertEqual(statuses["entry-1"], "Canceled")
        self.assertEqual(statuses["tp-1"], "Canceled")
        self.assertEqual(statuses["sl-1"], "Canceled")
        signal_row = pb.records["ibkr_signals"][0]
        self.assertEqual(signal_row["status"], "expired")
        self.assertEqual(signal_row["note"], "order_expired")
        self.assertEqual(signal_row["extra"]["expired_by"], "order_expiry_check")
        self.assertEqual(signal_row["extra"]["status_reason"], "order_expired")
        self.assertEqual(signal_row["extra"]["feishu_signal_notify_last_action"], "expired")
        self.assertEqual(payload["signal_expired_count"], 1)
        self.assertEqual(payload["signal_results"][0]["signal_id"], "sig-1")
        self.assertEqual(updated_cards[0][0], "sig-msg-1")
        self.assertIn("挂单超时自动取消", updated_cards[0][1]["elements"][0]["content"])
        self.assertIn("**订单有效期**: 30 分钟", updated_cards[0][1]["elements"][0]["content"])

    def test_order_expiry_matches_split_group_aliases_and_cleans_up_closed_broker_rows(self):
        pb = _OrderExpiryPB()
        pb.records["orders"] = [
            {
                "id": "nflx-entry",
                "unique_id": "NFLX_short_20260423",
                "entry_order_unique_id": "NFLX_short_20260423",
                "trade_group_id": "NFLX_short_20260423",
                "environment": "live",
                "symbol": "NFLX",
                "signal_id": "sig-nflx",
                "status": "Submitted",
                "role": "entry",
                "broker_order_id": "301",
                "order_id": "301",
                "quantity": 5,
                "bar_time_ms": 1,
                "extra": {},
            },
            {
                "id": "nflx-tp",
                "unique_id": "NFLX_short_20260423_tp",
                "entry_order_unique_id": "entry_NFLX_short_20260423",
                "trade_group_id": "entry_NFLX_short_20260423",
                "environment": "live",
                "symbol": "NFLX",
                "signal_id": "sig-nflx",
                "status": "Submitted",
                "role": "tp",
                "broker_order_id": "302",
                "order_id": "302",
                "quantity": 5,
                "bar_time_ms": 1,
                "extra": {},
            },
            {
                "id": "nflx-sl",
                "unique_id": "NFLX_short_20260423_sl",
                "entry_order_unique_id": "entry_NFLX_short_20260423",
                "trade_group_id": "entry_NFLX_short_20260423",
                "environment": "live",
                "symbol": "NFLX",
                "signal_id": "sig-nflx",
                "status": "Submitted",
                "role": "sl",
                "broker_order_id": "303",
                "order_id": "303",
                "quantity": 5,
                "bar_time_ms": 1,
                "extra": {},
            },
        ]
        pb.records["ibkr_signals"] = [
            {
                "id": "sig-row-nflx",
                "signal_id": "sig-nflx",
                "environment": "live",
                "symbol": "NFLX",
                "status": "submitted",
                "extra": {},
            }
        ]
        cancel_calls = []

        def cancel_broker_order(environment, order_id, payload):
            cancel_calls.append(order_id)
            if order_id == "302":
                return {"ok": False, "status_code": 404, "payload": {"error": "Order not found"}}
            if order_id == "303":
                return {"ok": False, "status_code": 400, "message": "Already cancelled"}
            return {"ok": True, "status_code": 200}

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=cancel_broker_order,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True},
            signal_chat_id_fn=lambda environment: f"signal-chat-{environment}",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(cancel_calls, ["301", "302", "303"])
        self.assertEqual(payload["cancelled_order_ids"], ["301", "302", "303"])
        self.assertEqual({row["status"] for row in pb.records["orders"]}, {"Canceled"})
        self.assertTrue(all(row["relation_status"] == "closed" for row in pb.records["orders"]))
        self.assertEqual(payload["processed_count"], 3)
        self.assertEqual(len(payload["detail_record_ids"]), 3)

    def test_order_expiry_repairs_stale_children_after_entry_canceled(self):
        pb = _OrderExpiryPB()
        for row in pb.records["orders"]:
            if row["role"] == "entry":
                row["status"] = "Canceled"
                row["relation_status"] = "closed"
            else:
                row["status"] = "Submitted"
                row["relation_status"] = "active"
        cancel_calls = []

        def cancel_broker_order(environment, order_id, payload):
            cancel_calls.append((environment, order_id, payload["trade_group_id"]))
            return {"ok": False, "message": "order not found"}

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=cancel_broker_order,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True},
            signal_chat_id_fn=lambda environment: f"signal-chat-{environment}",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed_count"], 2)
        self.assertEqual(payload["expired_group_count"], 1)
        self.assertEqual(payload["cancelled_order_ids"], ["12346", "12347"])
        self.assertEqual(cancel_calls, [("live", "12346", "tg-1"), ("live", "12347", "tg-1")])
        statuses = {row["id"]: row["status"] for row in pb.records["orders"]}
        self.assertEqual(statuses["entry-1"], "Canceled")
        self.assertEqual(statuses["tp-1"], "Canceled")
        self.assertEqual(statuses["sl-1"], "Canceled")
        created_details = [row for row in pb.records["ibkr_order_details"] if row.get("extra", {}).get("stale_pb_repair")]
        self.assertEqual(len(created_details), 2)
        self.assertTrue(all(row["extra"]["source"] == "order_reconcile_stale_pb" for row in created_details))

    def test_order_expiry_repairs_stale_children_when_broker_has_no_live_orders(self):
        pb = _OrderExpiryPB()
        for row in pb.records["orders"]:
            if row["role"] == "entry":
                row["status"] = "Filled"
                row["relation_status"] = "closed"
                row["filled_qty"] = 10
            else:
                row["status"] = "Submitted"
                row["relation_status"] = "active"
        cancel_calls = []

        def cancel_broker_order(environment, order_id, payload):
            cancel_calls.append((environment, order_id, payload["trade_group_id"]))
            return {"ok": True}

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=cancel_broker_order,
            list_live_broker_order_ids=lambda environment: [],
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True},
            signal_chat_id_fn=lambda environment: f"signal-chat-{environment}",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed_count"], 2)
        self.assertEqual(payload["expired_group_count"], 1)
        self.assertEqual(payload["cancelled_order_ids"], [])
        self.assertEqual(cancel_calls, [])
        statuses = {row["id"]: row["status"] for row in pb.records["orders"]}
        self.assertEqual(statuses["entry-1"], "Filled")
        self.assertEqual(statuses["tp-1"], "Canceled")
        self.assertEqual(statuses["sl-1"], "Canceled")
        self.assertEqual(pb.records["ibkr_signals"][0]["status"], "submitted")
        created_details = [row for row in pb.records["ibkr_order_details"] if row.get("extra", {}).get("stale_pb_repair")]
        self.assertEqual(len(created_details), 2)
        self.assertTrue(all(row["extra"]["live_order_checked"] for row in created_details))
        self.assertTrue(all(row["extra"]["cancel_skip_reason"] == "child_orders_not_live_at_broker" for row in created_details))

    def test_order_expiry_does_not_repair_filled_entry_children_still_live_at_broker(self):
        pb = _OrderExpiryPB()
        for row in pb.records["orders"]:
            if row["role"] == "entry":
                row["status"] = "Filled"
                row["relation_status"] = "closed"
                row["filled_qty"] = 10
            else:
                row["status"] = "Submitted"
                row["relation_status"] = "active"
        cancel_calls = []

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=lambda environment, order_id, payload: cancel_calls.append(order_id) or {"ok": True},
            list_live_broker_order_ids=lambda environment: ["12346"],
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True},
            signal_chat_id_fn=lambda environment: f"signal-chat-{environment}",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed_count"], 0)
        self.assertEqual(payload["expired_group_count"], 0)
        self.assertEqual(cancel_calls, [])
        statuses = {row["id"]: row["status"] for row in pb.records["orders"]}
        self.assertEqual(statuses["tp-1"], "Submitted")
        self.assertEqual(statuses["sl-1"], "Submitted")

    def test_auth_issue_treats_stale_broker_as_operational_recovery(self):
        issue = build_auth_immediate_issue(
            {
                "status": "recovering",
                "has_request": False,
                "active": False,
                "gateway_reachable": True,
                "gateway_status_code": 401,
                "runtime_authenticated": False,
                "runtime_started": False,
                "recovery_phase": "silent_probe",
                "recovery_class": "stale_broker",
                "probe_result": "stale_broker_restart_scheduled",
                "auto_restart_scheduled": True,
            }
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue["kind"], "stale_broker_recovering")
        self.assertTrue(is_operational_2fa_issue(issue))

    def test_auth_issue_plain_401_escalates(self):
        issue = build_auth_immediate_issue(
            {
                "status": "success",
                "has_request": False,
                "active": False,
                "gateway_reachable": True,
                "gateway_status_code": 401,
                "runtime_authenticated": False,
                "runtime_started": True,
                "recovery_phase": "idle",
                "recovery_class": "",
                "probe_result": "",
                "auto_restart_scheduled": False,
            }
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue["kind"], "session_expired")
        self.assertFalse(is_operational_2fa_issue(issue))

    def test_market_open_reminder_is_idempotent_per_day(self):
        pb = _ReminderPB()
        sent = []

        for _ in range(2):
            payload, status_code = build_system_market_open_reminder_response(
                payload={"environment": "live"},
                **self._reminder_deps(pb, sent),
            )
            self.assertEqual(status_code, 200)
            self.assertTrue(payload["ok"])

        self.assertEqual(len(sent), 1)

    def test_market_open_reminder_skips_outside_target_window(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_market_open_reminder_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-04-28 00:00:42", date="2026-04-28"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "outside_time_window")
        self.assertEqual(payload["target_time_et"], "09:30")
        self.assertEqual(len(sent), 0)
        self.assertEqual(pb.states, {})

    def test_market_open_reminder_sends_closed_notice_on_non_trading_day(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_market_open_reminder_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-05-09 09:30:00", date="2026-05-09"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["reason"], "market_closed")
        self.assertFalse(payload["trading_day"])
        self.assertEqual(payload["market_date"], "2026-05-09")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "IBKR 今日闭市提醒")
        state = pb.states[("system_notify_daily", "live", "2026-05-09")]["data"]
        self.assertEqual(state["open_sent_at"], "2026-05-09 09:30:00")
        self.assertEqual(state["open_reason"], "market_closed")
        self.assertTrue(state["open_notified"])
        self.assertEqual(state["market_calendar"]["closed_reason"], "weekend")

    def test_market_open_reminder_retries_when_delivery_fails(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_market_open_reminder_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, event_result={"success": False, "error": "send_failed"}),
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["ok"])
        state = pb.states[("system_notify_daily", "live", "2026-04-23")]["data"]
        self.assertNotIn("open_sent_at", state)
        self.assertEqual(state["open_error"], "send_failed")

    def test_early_expansion_topup_notifies_only_when_new_targets_exist(self):
        sent = []
        events = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/scan")
            self.assertEqual(json_body["mode"], "topup")
            self.assertTrue(json_body["force"])
            self.assertFalse(json_body["async"])
            self.assertTrue(json_body["open_target_reconcile"])
            self.assertEqual(json_body["trigger_source"], "open_target_pool_reconcile")
            self.assertLessEqual(float(timeout), 30.0)
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "scanned": 2,
                    "eligible": 1,
                    "new_active": 1,
                    "new_candidates": 0,
                    "new_targets": [
                        {
                            "symbol": "NVDA",
                            "status": "active",
                            "direction_bias": "long",
                            "score": 18,
                            "scan_reason": "5m:ema_bullish",
                        }
                    ],
                },
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:40:00", "cn": "2026-04-23 21:40:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append({"card": card, "chat_id": chat_id}) or {"success": True, "message_id": "msg-topup"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["notified"])
        self.assertEqual(payload["message_id"], "msg-topup")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["detail"]["status"], "success")
        self.assertEqual(payload["new_active"], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "startup-chat-live")
        self.assertEqual(events[0][0], "early_expansion_topup")

    def test_early_expansion_topup_labels_paper_broker_with_shared_live_data(self):
        sent = []
        events = []
        scan_payloads = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/scan")
            scan_payloads.append(dict(json_body or {}))
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "scanned": 1,
                    "eligible": 1,
                    "new_active": 1,
                    "new_candidates": 0,
                    "new_targets": [{"symbol": "NVDA", "status": "active", "direction_bias": "long", "score": 18}],
                },
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:40:00", "cn": "2026-04-23 21:40:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": "msg-topup"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(scan_payloads[0]["environment"], "live")
        self.assertEqual(scan_payloads[0]["broker_mode"], "paper")
        self.assertEqual(sent[0]["chat_id"], "startup-chat-paper")
        self.assertEqual(sent[0]["environment"], "paper")
        self.assertIn("Broker PAPER", sent[0]["card"]["header"]["title"]["content"])
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("**数据**: Shared Data", card_text)
        self.assertIn("environment=live", sent[0]["card"]["elements"][-1]["actions"][0]["multi_url"]["url"])
        self.assertEqual(events[0][5], "paper")
        self.assertEqual(events[0][4]["broker_mode"], "paper")
        self.assertEqual(events[0][4]["data_environment"], "live")

    def test_early_expansion_topup_skips_notification_without_new_targets(self):
        sent = []

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            request_json_request=lambda method, base_url, path, params=None, json_body=None, timeout=5.0: {
                "ok": True,
                "status_code": 200,
                "payload": {"ok": True, "new_targets": [], "new_active": 0, "new_candidates": 0},
            },
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
            write_system_event_record=lambda *args, **kwargs: {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "no_new_targets")
        self.assertEqual(sent, [])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["detail"]["status"], "success")

    def test_early_expansion_topup_async_submit_returns_submitted(self):
        sent = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/scan")
            self.assertTrue(json_body["async"])
            self.assertLessEqual(float(timeout), 30.0)
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "run_id": "scan-live-2026-04-23-abc123",
                    "status": "accepted",
                    "mode": "topup",
                },
                "target_url": "http://compute/scan",
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
            write_system_event_record=lambda *args, **kwargs: {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "submitted")
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["run_id"], "scan-live-2026-04-23-abc123")
        self.assertEqual(payload["notified"], False)
        self.assertEqual(sent, [])
        self.assertEqual(payload["detail"]["status"], "submitted")
        self.assertEqual(payload["detail"]["upstream_status"], "accepted")

    def test_early_expansion_topup_notifies_completed_async_scan_before_submitting_next(self):
        sent = []
        events = []
        states = {}
        calls = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"method": method, "path": path, "params": params, "json_body": json_body})
            if path == "/scan/status":
                self.assertEqual(method, "GET")
                self.assertIn(("mode", "topup"), params)
                return {
                    "ok": True,
                    "status_code": 200,
                    "payload": {
                        "ok": True,
                        "status": "completed",
                        "run_id": "scan-live-2026-04-23-old",
                        "environment": "live",
                        "date": "2026-04-23",
                        "mode": "topup",
                        "result": {
                            "ok": True,
                            "scanned": 3,
                            "eligible": 1,
                            "new_active": 1,
                            "new_candidates": 0,
                            "new_targets": [
                                {
                                    "symbol": "NVDA",
                                    "status": "active",
                                    "direction_bias": "long",
                                    "score": 18,
                                    "scan_reason": "5m:ema_bullish",
                                }
                            ],
                        },
                    },
                }
            self.assertEqual(path, "/scan")
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "run_id": "scan-live-2026-04-23-new",
                    "status": "accepted",
                    "mode": "topup",
                },
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append({"card": card, "chat_id": chat_id, "environment": environment}) or {"success": True, "message_id": "msg-topup"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "submitted")
        self.assertTrue(payload["notified"])
        self.assertEqual(payload["message_id"], "msg-topup")
        self.assertEqual(payload["completed_notification"]["notify_key"], "scan-live-2026-04-23-old")
        self.assertEqual([call["path"] for call in calls], ["/scan/status", "/scan"])
        self.assertEqual(sent[0]["chat_id"], "startup-chat-live")
        self.assertEqual(events[0][0], "early_expansion_topup")
        state = states[("ibkr_early_expansion_topup_notify", "live")]
        self.assertIn("scan-live-2026-04-23-old", state["notified_keys"])

    def test_early_expansion_topup_open_reconcile_overrides_empty_pending_scan(self):
        sent = []
        events = []
        calls = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"method": method, "path": path, "params": params, "json_body": dict(json_body or {})})
            if path == "/scan/status":
                return {
                    "ok": True,
                    "status_code": 200,
                    "payload": {
                        "ok": True,
                        "status": "running",
                        "run_id": "scan-live-2026-04-23-pending",
                        "counts": {"active": 0},
                    },
                }
            self.assertEqual(path, "/scan")
            self.assertFalse(json_body["async"])
            self.assertTrue(json_body["open_target_reconcile"])
            self.assertEqual(json_body["trigger_source"], "open_target_pool_reconcile")
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "scanned": 1,
                    "eligible": 1,
                    "new_active": 1,
                    "new_candidates": 0,
                    "new_targets": [{"symbol": "NVDA", "status": "active", "direction_bias": "long", "score": 18}],
                },
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 09:35:00", "cn": "2026-04-23 21:35:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append({"card": card, "chat_id": chat_id}) or {"success": True, "message_id": "msg-topup"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            get_state_payload=lambda state_key, environment: {"data": {}},
            upsert_state=lambda key, environment, data, date: data,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual([call["path"] for call in calls], ["/scan/status", "/scan"])
        self.assertEqual(payload["completed_notification"]["reason"], "open_reconcile_overrides_pending_scan")
        self.assertEqual(payload["detail"]["target_reconcile_window"]["current_time_et"], "09:35")
        self.assertEqual(len(sent), 1)

    def test_early_expansion_topup_does_not_duplicate_completed_async_notification(self):
        sent = []
        states = {
            ("ibkr_early_expansion_topup_notify", "live"): {
                "notified_keys": ["scan-live-2026-04-23-old"],
            }
        }

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            if path == "/scan/status":
                return {
                    "ok": True,
                    "status_code": 200,
                    "payload": {
                        "ok": True,
                        "status": "completed",
                        "run_id": "scan-live-2026-04-23-old",
                        "result": {
                            "new_active": 1,
                            "new_candidates": 0,
                            "new_targets": [{"symbol": "NVDA", "status": "active"}],
                        },
                    },
                }
            return {
                "ok": True,
                "status_code": 200,
                "payload": {"ok": True, "accepted": True, "async": True, "run_id": "scan-live-2026-04-23-new", "status": "accepted"},
            }

        payload, status_code = build_early_expansion_topup_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            time_strings=lambda: {"us": "2026-04-23 10:00:00", "cn": "2026-04-23 22:00:00", "date": "2026-04-23"},
            request_json_request=request_json_request,
            compute_base_url="http://compute",
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
            write_system_event_record=lambda *args, **kwargs: {"id": "event-1"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "submitted")
        self.assertFalse(payload["notified"])
        self.assertEqual(payload["completed_notification"]["reason"], "already_notified")
        self.assertEqual(sent, [])

    def test_notify_new_targets_from_seed_scan_records_dedup_state(self):
        sent = []
        events = []
        states = {}

        delivered = notify_new_targets_from_scan(
            status_payload={"run_id": "seed-run-1", "status": "completed"},
            result={
                "run_id": "seed-run-1",
                "new_active": 1,
                "new_candidates": 0,
                "new_targets": [{"symbol": "BX", "status": "active", "score": 22}],
            },
            broker_mode="paper",
            data_environment="live",
            market_date="2026-05-22",
            times={"us": "2026-05-22 09:31:00", "cn": "2026-05-22 21:31:00", "date": "2026-05-22"},
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": "msg-seed"},
            write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-seed"},
            config_value=lambda key, default, environment: default,
            console_base_url=lambda: "https://quant.lzw-glory.top",
            startup_chat_id=lambda environment: f"startup-chat-{environment}",
            get_state_payload=lambda state_key, environment, date=None: {
                "data": states.get((state_key, environment, date or "2026-05-22"), {})
            },
            upsert_state=lambda key, environment, data, date: states.update({(key, environment, date): dict(data)}) or data,
            source="seed",
        )

        self.assertTrue(delivered["finalized"])
        self.assertEqual(delivered["notify_key"], "seed-run-1")
        self.assertEqual(sent[0]["environment"], "paper")
        self.assertIn("信号窗口入池", sent[0]["card"]["header"]["title"]["content"])
        self.assertEqual(events[0][0], "early_expansion_topup")
        notify_state = states[("ibkr_early_expansion_topup_notify", "paper", "2026-05-22")]
        self.assertIn("seed-run-1", notify_state["notified_keys"])
        self.assertEqual(notify_state["last_source"], "seed")

    def _daily_event_reconcile_deps(self, states, sent, events, *, now_us, request_json_request=None):
        market_date = now_us[:10]

        def get_state_payload(state_key, environment, date=None):
            state_date = date or market_date
            return {"data": dict(states.get((state_key, environment, state_date), {}).get("data") or {})}

        def upsert_state(state_key, environment, data, date):
            record = {"data": dict(data)}
            states[(state_key, environment, date)] = record
            return record

        return {
            "normalize_environment": lambda value, default: str(value or default).strip().lower() or default,
            "time_strings": lambda: {"us": now_us, "cn": now_us.replace("09:", "21:").replace("10:", "22:"), "date": market_date},
            "build_today_targets_response": lambda payload: (
                {
                    "market_date": payload.get("market_date") or market_date,
                    "daily_scan": {"status": "completed", "market_date": payload.get("market_date") or market_date},
                    "summary": {
                        "total": 1,
                        "active_count": 1,
                        "candidate_count": 0,
                        "operable_count": 1,
                        "technical_ready_count": 1,
                        "signaled_count": 0,
                    },
                    "items": [{"symbol": "BX", "status": "active"}],
                },
                200,
            ),
            "build_system_summary_payload": lambda environment, lite_mode=False: {
                "status": "running",
                "today": {"ibkr_bars": 10, "ibkr_signals": 0, "orders": 0, "events": 0},
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
            },
            "build_system_monitor_payload": lambda environment: {
                "runtime": {
                    "gateway": {"running": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True},
                    "daily_scan": {"status": "completed", "market_date": market_date},
                },
                "scheduler": {"status": "running"},
                "service_monitor": {"status_counts": {"running": 6}},
            },
            "feishu_send_interactive": lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": f"msg-{len(sent)}"},
            "write_system_event_record": lambda *args, **kwargs: events.append(args) or {"id": f"event-{len(events)}"},
            "get_state_payload": get_state_payload,
            "upsert_state": upsert_state,
            "config_value": lambda key, default, environment: default,
            "console_base_url": lambda: "https://quant.lzw-glory.top",
            "startup_chat_id": lambda environment: f"startup-chat-{environment}",
            "load_market_snapshots": lambda environment, symbols, market_date, computed_at_ms: [],
            "request_json_request": request_json_request,
            "compute_base_url": "http://compute",
        }

    def test_daily_event_reconcile_skips_legacy_target_events_in_tv_primary_slim(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "result": {"new_targets": [{"symbol": "BX", "status": "active"}]},
                }
            }
        }
        deps = self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 09:40:00")
        deps["config_value"] = lambda key, default, environment: {
            "ibkr_signal_source": "tradingview",
            "ibkr_tv_primary_runtime_slim_enabled": "TRUE",
        }.get(key, default)

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "dry_run": True},
            **deps,
        )

        self.assertEqual(status_code, 200)
        ledger_events = payload["ledger"]["events"]
        self.assertEqual(ledger_events["daily_scan_seed"]["status"], "skipped")
        self.assertEqual(ledger_events["target_pool_quality"]["status"], "skipped")
        self.assertFalse(any(event_id.startswith("new_targets:seed") for event_id in ledger_events))
        self.assertEqual(payload["actions"], [])
        self.assertEqual(sent, [])
        self.assertEqual(events, [])

    def test_daily_event_reconcile_sends_seed_new_targets_once(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "seed-run-2",
                    "finished_at": "2026-05-22T13:28:18Z",
                    "result": {
                        "active": 5,
                        "candidates": 0,
                        "new_active": 5,
                        "new_candidates": 0,
                        "new_targets": [{"symbol": symbol, "status": "active"} for symbol in ["BX", "AVGO", "BAC", "BABA", "COHR"]],
                    },
                }
            }
        }

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "force_event_id": "new_targets:seed"},
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 09:40:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(payload["actions"][0]["action"], "notify_new_targets")
        event = payload["ledger"]["events"]["new_targets:seed:seed-run-2"]
        self.assertEqual(event["status"], "completed_late")
        self.assertEqual(event["detail"]["new_targets"], 5)
        notify_state = states[("ibkr_early_expansion_topup_notify", "paper", "2026-05-22")]["data"]
        self.assertIn("seed-run-2", notify_state["notified_keys"])

    def test_daily_event_reconcile_open_report_late_before_cutoff(self):
        sent = []
        events = []
        states = {}

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "force_event_id": "open_report"},
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 09:40:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(payload["actions"][0]["action"], "send_open_report")
        self.assertEqual(payload["ledger"]["events"]["open_report"]["status"], "completed_late")
        state = states[("system_notify_daily", "paper", "2026-05-22")]["data"]
        self.assertEqual(state["open_sent_at"], "2026-05-22 09:40:00")

    def test_daily_event_reconcile_open_report_missed_after_cutoff(self):
        sent = []
        events = []
        states = {}

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "force_event_id": "open_report"},
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 10:31:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sent, [])
        self.assertEqual(payload["actions"], [])
        self.assertEqual(payload["ledger"]["events"]["open_report"]["status"], "missed")
        self.assertNotIn(("system_notify_daily", "paper", "2026-05-22"), states)

    def test_daily_event_reconcile_does_not_resend_existing_seed_notice(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "seed-run-3",
                    "result": {
                        "new_active": 1,
                        "new_candidates": 0,
                        "new_targets": [{"symbol": "BX", "status": "active"}],
                    },
                }
            },
            ("ibkr_early_expansion_topup_notify", "paper", "2026-05-22"): {
                "data": {"notified_keys": ["seed-run-3"]}
            },
        }

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "force_event_id": "new_targets:seed"},
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 09:40:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sent, [])
        self.assertEqual(payload["actions"][0]["result"]["reason"], "already_notified")
        self.assertEqual(payload["ledger"]["events"]["new_targets:seed:seed-run-3"]["status"], "completed_late")

    def test_daily_event_reconcile_can_force_seed_notice_after_cutoff(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "seed-run-4",
                    "result": {
                        "new_active": 1,
                        "new_candidates": 0,
                        "new_targets": [{"symbol": "BX", "status": "active"}],
                    },
                }
            }
        }

        payload, status_code = build_daily_event_reconcile_response(
            payload={
                "broker_mode": "paper",
                "market_data_mode": "live",
                "force_event_id": "new_targets:seed",
                "allow_after_cutoff": True,
            },
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 10:31:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["allow_after_cutoff"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(payload["ledger"]["events"]["new_targets:seed:seed-run-4"]["status"], "completed_late")

    def test_daily_event_reconcile_resubmits_seed_scan_when_pool_quality_bad_and_data_ready(self):
        sent = []
        events = []
        calls = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "seed-partial",
                    "result": {
                        "scanned": 124,
                        "active": 5,
                        "candidates": 0,
                        "new_targets": [{"symbol": "BX", "status": "active"}],
                        "data_completeness": {
                            "blocking_enabled": True,
                            "status": "repairing",
                            "excluded_incomplete_count": 112,
                        },
                        "rejection_summary": {"data_incomplete_repairing": 112},
                    },
                }
            }
        }

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"method": method, "path": path, "params": params, "json_body": json_body})
            if path == "/ibkr/status":
                return {
                    "ok": True,
                    "payload": {
                        "watchlist_idle_topup": {
                            "completion": {
                                "total": 119,
                                "fresh": 119,
                                "stale": 0,
                                "missing": 0,
                                "unobserved": 0,
                                "expected_latest_5m_ms": 1000,
                                "oldest_latest_ms": 1000,
                            }
                        }
                    },
                }
            self.assertEqual(path, "/scan")
            self.assertEqual(method, "POST")
            self.assertTrue(json_body["async"])
            self.assertTrue(json_body["force"])
            self.assertEqual(json_body["mode"], "seed")
            self.assertEqual(json_body["trigger_source"], "daily_event_target_pool_quality_repair")
            return {
                "ok": True,
                "payload": {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "run_id": "target-pool-quality-live-2026-05-22",
                    "status": "accepted",
                },
            }

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **self._daily_event_reconcile_deps(
                states,
                sent,
                events,
                now_us="2026-05-22 11:36:00",
                request_json_request=request_json_request,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual([call["path"] for call in calls], ["/ibkr/market/calendar", "/ibkr/status", "/scan"])
        action = payload["actions"][0]
        self.assertEqual(action["event_id"], "target_pool_quality")
        self.assertEqual(action["action"], "submit_seed_rescan")
        event = payload["ledger"]["events"]["target_pool_quality"]
        self.assertEqual(event["status"], "repairing")
        self.assertEqual(event["detail"]["reason"], "high_incomplete_data")
        self.assertEqual(event["detail"]["repair_run_id"], "target-pool-quality-live-2026-05-22")
        self.assertEqual(sent, [])

    def test_daily_event_reconcile_waits_for_existing_target_pool_repair(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "seed-partial",
                    "result": {
                        "scanned": 124,
                        "active": 5,
                        "data_completeness": {
                            "blocking_enabled": True,
                            "status": "repairing",
                            "excluded_incomplete_count": 112,
                        },
                    },
                }
            },
            ("ibkr_daily_event_ledger", "paper", "2026-05-22"): {
                "data": {
                    "events": {
                        "target_pool_quality": {
                            "status": "repairing",
                            "detail": {"repair_run_id": "target-pool-quality-live-2026-05-22"},
                        }
                    }
                }
            },
        }

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            self.assertEqual(path, "/scan/status")
            return {
                "ok": True,
                "payload": {
                    "ok": True,
                    "status": "running",
                    "run_id": "target-pool-quality-live-2026-05-22",
                },
            }

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **self._daily_event_reconcile_deps(
                states,
                sent,
                events,
                now_us="2026-05-22 11:37:00",
                request_json_request=request_json_request,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["actions"], [])
        self.assertEqual(payload["ledger"]["events"]["target_pool_quality"]["status"], "repairing")

    def test_daily_event_reconcile_sends_late_seed_notice_after_target_pool_repair(self):
        sent = []
        events = []
        states = {
            ("ibkr_daily_scan_state", "live", "global"): {
                "data": {
                    "status": "completed",
                    "market_date": "2026-05-22",
                    "run_id": "target-pool-quality-live-2026-05-22",
                    "finished_at": "2026-05-22T15:55:33Z",
                    "result": {
                        "scanned": 124,
                        "active": 86,
                        "new_active": 86,
                        "data_completeness": {"blocking_enabled": True, "excluded_incomplete_count": 0},
                        "new_targets": [{"symbol": symbol, "status": "active"} for symbol in ["AAPL", "NVDA"]],
                    },
                }
            },
            ("ibkr_daily_event_ledger", "paper", "2026-05-22"): {
                "data": {
                    "events": {
                        "target_pool_quality": {
                            "status": "repairing",
                            "detail": {"repair_run_id": "target-pool-quality-live-2026-05-22"},
                        }
                    }
                }
            },
        }

        payload, status_code = build_daily_event_reconcile_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **self._daily_event_reconcile_deps(states, sent, events, now_us="2026-05-22 11:58:00"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(sent), 1)
        event = payload["ledger"]["events"]["new_targets:seed:target-pool-quality-live-2026-05-22"]
        self.assertEqual(event["status"], "completed_late")
        self.assertEqual(event["detail"]["late_reason"], "target_pool_quality_repair")
        self.assertEqual(event["detail"]["new_targets"], 2)

    def test_early_expansion_topup_read_timeout_stays_pending(self):
        sent = []
        events = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/scan")
            self.assertTrue(json_body["async"])
            self.assertLessEqual(float(timeout), 30.0)
            return {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "HTTPConnectionPool(host='compute'): Read timed out. (read timeout=15)",
                "target_url": "http://compute/scan",
            }

        with mock.patch("ibkr_api.system.jobs.early_expansion_topup.time.monotonic", side_effect=[100.0, 115.2]):
            payload, status_code = build_early_expansion_topup_response(
                payload={"environment": "live"},
                normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
                time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
                request_json_request=request_json_request,
                compute_base_url="http://compute",
                feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True},
                write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
                config_value=lambda key, default, environment: default,
                console_base_url=lambda: "https://quant.lzw-glory.top",
                startup_chat_id=lambda environment: f"startup-chat-{environment}",
            )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "timeout_waiting")
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["notified"], False)
        self.assertEqual(sent, [])
        self.assertEqual(events, [])
        self.assertNotIn("error", payload)
        self.assertEqual(payload["elapsed_s"], 15.2)
        self.assertEqual(payload["detail"]["status"], "timeout_waiting")
        self.assertEqual(payload["detail"]["elapsed_s"], 15.2)
        self.assertIn("Read timed out", payload["detail"]["upstream_error"])

    def test_early_expansion_topup_real_failure_still_fails(self):
        sent = []
        events = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            return {
                "ok": True,
                "status_code": 200,
                "payload": {"ok": False, "error": "scanner exploded", "status": "failed"},
                "target_url": "http://compute/scan",
            }

        with mock.patch("ibkr_api.system.jobs.early_expansion_topup.time.monotonic", side_effect=[50.0, 53.456]):
            payload, status_code = build_early_expansion_topup_response(
                payload={"environment": "live"},
                normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
                time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
                request_json_request=request_json_request,
                compute_base_url="http://compute",
                feishu_send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "msg-fail"},
                write_system_event_record=lambda *args, **kwargs: events.append(args) or {"id": "event-1"},
                config_value=lambda key, default, environment: default,
                console_base_url=lambda: "https://quant.lzw-glory.top",
                startup_chat_id=lambda environment: f"startup-chat-{environment}",
            )

        self.assertEqual(status_code, 502)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "scanner exploded")
        self.assertEqual(payload["elapsed_s"], 3.456)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["error"], "scanner exploded")
        self.assertEqual(payload["detail"]["elapsed_s"], 3.456)
        self.assertEqual(payload["message_id"], "msg-fail")
        self.assertEqual(len(sent), 1)
        self.assertEqual(events[0][1], "error")
        self.assertEqual(events[0][4]["error"], "scanner exploded")
        self.assertEqual(events[0][4]["elapsed_s"], 3.456)

    def test_intraday_window_admission_adds_valid_window_candidate_and_reconciles_without_signals(self):
        pb = _IntradayAdmissionPB()
        pb.records["watchlist"] = [
            {"id": "wl-1", "symbol": "MSFT", "environment": "live", "symbol_role": "trade", "exchange": "NASDAQ"}
        ]
        pb.records["ibkr_bars"] = [
            *[self._admission_daily_bar("MSFT", days_ago, volume=900000, close=9.0 + days_ago * 0.01) for days_ago in range(1, 12)],
            self._admission_bar("MSFT", 0, sd_upper=True, close=10.0, volume=250000),
            self._admission_bar("MSFT", 5, close=10.2, volume=250000),
        ]
        pb.records["ibkr_indicators"] = [
            {
                "symbol": "MSFT",
                "environment": "live",
                "interval": "5",
                "bar_time_ms": self._admission_bar("MSFT", 5)["bar_time_ms"],
                "atr_pct": 2.5,
                "trend_dir": 1,
            }
        ]
        request_calls = []

        with mock.patch("ibkr_api.system.jobs.intraday_window_admission.time.time", return_value=datetime(2026, 4, 23, 9, 50, tzinfo=ET).timestamp()):
            with mock.patch(
                "ibkr_api.system.jobs.intraday_window_admission.build_active_window_items_for_symbols",
                return_value=self._admission_window_payload("MSFT"),
            ):
                payload, status_code = self._run_admission(pb, request_calls=request_calls)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["admitted_symbols"], ["MSFT"])
        target = next(row for row in pb.records["ibkr_targets"] if row["symbol"] == "MSFT")
        self.assertEqual(target["status"], "candidate")
        self.assertEqual(target["direction_bias"], "long")
        self.assertEqual(target["extra"]["intraday_window_admission"]["window_status"], "upper_active")
        self.assertFalse(target["extra"]["execution_eligible"])
        self.assertEqual(target["extra"]["target_layer"], "observe")
        reconcile_calls = [call for call in request_calls if call["path"] == "/ibkr/universe/reconcile"]
        self.assertEqual(reconcile_calls[0]["json_body"]["prime_symbols"], ["MSFT"])
        self.assertFalse(reconcile_calls[0]["json_body"]["emit_signals"])
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.created))

    def test_intraday_window_admission_skips_tv_primary_slim(self):
        pb = _IntradayAdmissionPB()
        request_calls = []

        with mock.patch(
            "ibkr_api.system.jobs.intraday_window_admission.build_active_window_items_for_symbols"
        ) as window_mock:
            payload, status_code = self._run_admission(
                pb,
                request_calls=request_calls,
                config_overrides={
                    "ibkr_signal_source": "tradingview",
                    "ibkr_tv_primary_runtime_slim_enabled": "TRUE",
                },
            )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "tv_primary_slim_mode")
        self.assertEqual(payload["admitted_symbols"], [])
        self.assertEqual(pb.records["ibkr_targets"], [])
        self.assertEqual(request_calls, [])
        window_mock.assert_not_called()

    def test_intraday_window_admission_dry_run_does_not_write_or_reconcile(self):
        pb = _IntradayAdmissionPB()
        pb.records["watchlist"] = [
            {"id": "wl-1", "symbol": "MSFT", "environment": "live", "symbol_role": "trade", "exchange": "NASDAQ"}
        ]
        pb.records["ibkr_bars"] = [
            *[self._admission_daily_bar("MSFT", days_ago, volume=900000) for days_ago in range(1, 12)],
            self._admission_bar("MSFT", 0, sd_upper=True, close=10.0),
            self._admission_bar("MSFT", 5, close=10.2),
        ]
        pb.records["ibkr_indicators"] = [
            {"symbol": "MSFT", "environment": "live", "interval": "5", "bar_time_ms": self._admission_bar("MSFT", 5)["bar_time_ms"], "atr_pct": 2.5}
        ]
        request_calls = []

        with mock.patch("ibkr_api.system.jobs.intraday_window_admission.time.time", return_value=datetime(2026, 4, 23, 9, 50, tzinfo=ET).timestamp()):
            with mock.patch(
                "ibkr_api.system.jobs.intraday_window_admission.build_active_window_items_for_symbols",
                return_value=self._admission_window_payload("MSFT"),
            ):
                payload, status_code = self._run_admission(pb, payload={"dry_run": True}, request_calls=request_calls)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["would_admit"], 1)
        self.assertEqual(payload["admitted"], 0)
        self.assertEqual(pb.records["ibkr_targets"], [])
        self.assertFalse(any(call["path"] == "/ibkr/universe/reconcile" for call in request_calls))

    def test_intraday_window_admission_rejects_without_current_window(self):
        pb = _IntradayAdmissionPB()
        pb.records["watchlist"] = [
            {"id": "wl-1", "symbol": "MSFT", "environment": "live", "symbol_role": "trade", "exchange": "NASDAQ"}
        ]
        pb.records["ibkr_bars"] = [
            *[self._admission_daily_bar("MSFT", days_ago, volume=900000) for days_ago in range(1, 12)],
            self._admission_bar("MSFT", 0, close=10.0),
            self._admission_bar("MSFT", 5, close=10.2),
        ]
        pb.records["ibkr_indicators"] = [
            {"symbol": "MSFT", "environment": "live", "interval": "5", "bar_time_ms": self._admission_bar("MSFT", 5)["bar_time_ms"], "atr_pct": 2.5}
        ]

        with mock.patch("ibkr_api.system.jobs.intraday_window_admission.time.time", return_value=datetime(2026, 4, 23, 9, 50, tzinfo=ET).timestamp()):
            payload, status_code = self._run_admission(pb)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["admitted"], 0)
        self.assertEqual(payload["reason"], "no_eligible_active_windows")
        self.assertIn("signal_pressure_not_ready", payload["rejection_summary"])

    def test_intraday_window_admission_respects_full_budget(self):
        pb = _IntradayAdmissionPB()
        pb.records["watchlist"] = [
            {"id": "wl-1", "symbol": "MSFT", "environment": "live", "symbol_role": "trade", "exchange": "NASDAQ"}
        ]
        pb.records["ibkr_targets"] = [
            {"id": "target-1", "symbol": "AAPL", "environment": "live", "date": "2026-04-23", "status": "active"}
        ]
        pb.records["ibkr_bars"] = [
            *[self._admission_daily_bar("MSFT", days_ago, volume=900000) for days_ago in range(1, 12)],
            self._admission_bar("MSFT", 0, sd_upper=True, close=10.0),
            self._admission_bar("MSFT", 5, close=10.2),
        ]
        pb.records["ibkr_indicators"] = [
            {"symbol": "MSFT", "environment": "live", "interval": "5", "bar_time_ms": self._admission_bar("MSFT", 5)["bar_time_ms"], "atr_pct": 2.5}
        ]

        with mock.patch("ibkr_api.system.jobs.intraday_window_admission.time.time", return_value=datetime(2026, 4, 23, 9, 50, tzinfo=ET).timestamp()):
            payload, status_code = self._run_admission(
                pb,
                config_overrides={
                    "ibkr_target_subscription_limit": "1",
                    "ibkr_total_subscription_limit": "0",
                },
            )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "subscription_budget_full")
        self.assertEqual(payload["admitted"], 0)

    def _run_monitor_alert_guard(
        self,
        states=None,
        events=None,
        *,
        flags=None,
        status="warning",
        runtime=None,
        service_monitor=None,
        config_overrides=None,
        now_us="2026-04-23 12:05:00",
        date="2026-04-23",
        build_admission_preview=None,
    ):
        states = states if states is not None else {}
        events = events if events is not None else []
        flags = list(flags or [])
        config_overrides = config_overrides or {}

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, state_date):
            states[(state_key, environment)] = dict(data)
            return data

        def build_system_monitor_payload(environment):
            return {
                "status": status,
                "flags": flags,
                "runtime": runtime or {"session": {"authenticated": True}, "websocket": {"connected": True}},
                "scheduler": {"latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": service_monitor or {"status_counts": {"running": 8}, "services": {}},
                "pocketbase": {"disk": {"filesystem": {"used_pct": 10}}},
            }

        payload, status_code = build_system_monitor_alert_guard_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": now_us, "cn": "2026-04-24 00:05:00", "date": date},
            build_system_monitor_payload=build_system_monitor_payload,
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
            build_admission_preview=build_admission_preview,
            config_value=lambda key, default, environment: config_overrides.get(key, default),
        )
        return payload, status_code, states, events

    def test_system_monitor_alert_suppresses_first_account_snapshot_warning(self):
        flag = {
            "code": "account_snapshot_degraded",
            "severity": "warning",
            "title": "Account snapshot unavailable",
            "detail": "read timeout",
        }

        payload, status_code, states, events = self._run_monitor_alert_guard(flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertEqual([], events)
        self.assertEqual(["account_snapshot_degraded"], payload["flag_codes"])
        self.assertEqual([], payload["alert_flag_codes"])
        self.assertEqual(["account_snapshot_degraded"], payload["suppressed_flag_codes"])
        self.assertEqual(1, payload["account_snapshot_warning_streak"])
        self.assertEqual(1, states[("system_monitor_alert", "live")]["account_snapshot_warning_streak"])

    def test_system_monitor_alert_emits_on_second_account_snapshot_warning(self):
        flag = {
            "code": "account_snapshot_degraded",
            "severity": "warning",
            "title": "Account snapshot unavailable",
            "detail": "read timeout",
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        payload, status_code, _, events = self._run_monitor_alert_guard(states=states, events=events, flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["account_snapshot_degraded"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(2, payload["account_snapshot_warning_streak"])
        self.assertEqual(1, len(events))
        self.assertEqual("IBKR Monitor 告警（1项）", events[0]["title"])

    def test_system_monitor_alert_suppresses_first_monitor_source_warning(self):
        flags = [
            {
                "code": "account_snapshot_degraded",
                "severity": "warning",
                "title": "Account snapshot unavailable",
                "detail": "read timeout",
            },
            {
                "code": "monitor_builder_compute_monitor",
                "severity": "warning",
                "title": "Monitor aggregation degraded (compute_monitor)",
                "detail": "read timeout",
            },
        ]

        payload, status_code, states, events = self._run_monitor_alert_guard(flags=flags)

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertEqual([], events)
        self.assertEqual(
            ["account_snapshot_degraded", "monitor_builder_compute_monitor"],
            payload["flag_codes"],
        )
        self.assertEqual([], payload["alert_flag_codes"])
        self.assertEqual(
            ["account_snapshot_degraded", "monitor_builder_compute_monitor"],
            payload["suppressed_flag_codes"],
        )
        self.assertEqual(1, payload["account_snapshot_warning_streak"])
        self.assertEqual(1, payload["monitor_source_warning_streak"])
        state = states[("system_monitor_alert", "live")]
        self.assertEqual(1, state["monitor_source_warning_streak"])
        self.assertFalse(state.get("last_monitor_alert_hash"))

    def test_system_monitor_alert_emits_on_third_monitor_source_warning(self):
        flag = {
            "code": "monitor_builder_compute_monitor",
            "severity": "warning",
            "title": "Monitor aggregation degraded (compute_monitor)",
            "detail": "read timeout",
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        second_payload, _, _, events = self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        payload, status_code, _, events = self._run_monitor_alert_guard(states=states, events=events, flags=[flag])

        self.assertFalse(second_payload["triggered"])
        self.assertEqual(["monitor_builder_compute_monitor"], second_payload["suppressed_flag_codes"])
        self.assertEqual(2, second_payload["monitor_source_warning_streak"])
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["monitor_builder_compute_monitor"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(3, payload["monitor_source_warning_streak"])
        self.assertEqual(1, len(events))
        self.assertEqual("IBKR Monitor 告警（1项）", events[0]["title"])

    def test_system_monitor_alert_suppressed_monitor_source_warning_does_not_recover(self):
        flag = {
            "code": "monitor_builder_compute_monitor",
            "severity": "warning",
            "title": "Monitor aggregation degraded (compute_monitor)",
            "detail": "read timeout",
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        payload, status_code, _, events = self._run_monitor_alert_guard(
            states=states,
            events=events,
            flags=[],
            status="ok",
            now_us="2026-04-23 12:10:00",
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertFalse(payload["recovered"])
        self.assertEqual([], events)
        self.assertEqual(0, states[("system_monitor_alert", "live")]["monitor_source_warning_streak"])

    def test_system_monitor_alert_warning_uses_longer_cooldown(self):
        flag = {
            "code": "account_snapshot_degraded",
            "severity": "warning",
            "title": "Account snapshot unavailable",
            "detail": "read timeout",
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        state = states[("system_monitor_alert", "live")]
        state["last_monitor_alert_ms"] = int(state["last_monitor_alert_ms"]) - 30 * 60 * 1000
        payload, status_code, _, events = self._run_monitor_alert_guard(states=states, events=events, flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertEqual(1, len(events))
        self.assertEqual(3, payload["account_snapshot_warning_streak"])

    def test_system_monitor_alert_non_account_warning_still_emits_immediately(self):
        flag = {"code": "websocket_not_ready", "severity": "warning", "title": "WS", "detail": "offline"}

        payload, status_code, _, events = self._run_monitor_alert_guard(flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["websocket_not_ready"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(1, len(events))

    def test_system_monitor_alert_suppresses_first_ws_silence_warning(self):
        flag = {
            "code": "market_data_silent",
            "severity": "warning",
            "title": "Market data slowed",
            "detail": "recent WebSocket message age 91s",
        }

        payload, status_code, states, events = self._run_monitor_alert_guard(flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertEqual([], events)
        self.assertEqual(["market_data_silent"], payload["flag_codes"])
        self.assertEqual([], payload["alert_flag_codes"])
        self.assertEqual(["market_data_silent"], payload["suppressed_flag_codes"])
        self.assertEqual(1, payload["ws_silence_warning_streak"])
        self.assertEqual(1, states[("system_monitor_alert", "live")]["ws_silence_warning_streak"])

    def test_system_monitor_alert_emits_on_second_ws_silence_warning(self):
        flag = {
            "code": "market_data_silent",
            "severity": "warning",
            "title": "Market data slowed",
            "detail": "recent WebSocket message age 91s",
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        payload, status_code, _, events = self._run_monitor_alert_guard(states=states, events=events, flags=[flag])

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["market_data_silent"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(2, payload["ws_silence_warning_streak"])
        self.assertEqual(1, len(events))
        self.assertEqual("IBKR Monitor 告警（1项）", events[0]["title"])
        self.assertEqual("2/2", events[0]["detail"]["WS静默连续"])

    def test_system_monitor_alert_emits_ws_silence_critical_immediately(self):
        flag = {
            "code": "market_data_silent_critical",
            "severity": "error",
            "title": "Market data silent",
            "detail": "recent WebSocket message age 181s",
        }

        payload, status_code, _, events = self._run_monitor_alert_guard(flags=[flag], status="error")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["market_data_silent_critical"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(0, payload["ws_silence_warning_streak"])
        self.assertEqual(1, len(events))
        self.assertEqual("IBKR Monitor 严重告警（1项）", events[0]["title"])

    def test_system_monitor_alert_emits_recovery_when_flags_clear(self):
        flag = {"code": "websocket_not_ready", "severity": "warning", "title": "WS", "detail": "offline"}
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag])
        payload, status_code, _, events = self._run_monitor_alert_guard(
            states=states,
            events=events,
            flags=[],
            status="ok",
            now_us="2026-04-23 12:10:00",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertTrue(payload["recovered"])
        self.assertEqual(["websocket_not_ready"], payload["recovered_flag_codes"])
        self.assertEqual(2, len(events))
        self.assertEqual("IBKR Monitor 已恢复", events[1]["title"])
        self.assertEqual("info", events[1]["level"])
        self.assertEqual("IBKR Monitor 告警已恢复。", events[1]["detail"]["结论"])
        state = states[("system_monitor_alert", "live")]
        self.assertEqual("", state["last_monitor_alert_hash"])
        self.assertEqual(["websocket_not_ready"], state["last_monitor_recovery_codes"])

    def test_system_monitor_alert_emits_market_data_conflict_recovery_detail(self):
        flag = {
            "code": "market_data_session_conflict",
            "severity": "warning",
            "title": "Market data session conflict",
            "detail": "No market data during competing live session",
        }
        active_runtime = {
            "session": {"authenticated": True},
            "websocket": {"connected": True, "subscribed_count": 2},
            "gateway": {"running": True, "broker": {"connected": True, "ready": True}},
            "market_data_session_conflict": {
                "active": True,
                "first_seen_at": "2026-06-16T12:20:24+00:00",
                "last_seen_at": "2026-06-16T12:30:53+00:00",
                "last_error_at": "2026-06-16T12:20:24+00:00",
                "count": 15,
            },
        }
        recovered_runtime = {
            "session": {"authenticated": True},
            "websocket": {"connected": True, "ready": True, "subscribed_count": 2},
            "gateway": {"running": True, "broker": {"connected": True, "ready": True}},
            "market_data_session_conflict": {
                "active": False,
                "first_seen_at": "2026-06-16T12:20:24+00:00",
                "last_error_at": "2026-06-16T12:20:24+00:00",
                "resolved_at": "2026-06-16T12:33:08+00:00",
                "recovery_action": "resubscribed",
                "resubscribed_count": 2,
                "recovery_evidence": {
                    "session_authenticated": True,
                    "websocket_ready": True,
                    "fresh_quotes": 2,
                },
            },
        }
        states = {}
        events = []

        self._run_monitor_alert_guard(states=states, events=events, flags=[flag], runtime=active_runtime)
        payload, status_code, _, events = self._run_monitor_alert_guard(
            states=states,
            events=events,
            flags=[],
            status="ok",
            runtime=recovered_runtime,
            now_us="2026-06-16 08:35:00",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["recovered"])
        self.assertEqual(["market_data_session_conflict"], payload["recovered_flag_codes"])
        self.assertEqual(2, len(events))
        self.assertEqual("IBKR 行情会话冲突已恢复", events[1]["title"])
        self.assertEqual("已恢复", events[1]["detail"]["行情冲突"])
        self.assertEqual("2026-06-16T12:33:08+00:00", events[1]["detail"]["恢复时间"])
        self.assertEqual("resubscribed", events[1]["detail"]["恢复动作"])
        self.assertEqual("2", events[1]["detail"]["重订阅数量"])
        self.assertIn("fresh_quotes=2", events[1]["detail"]["恢复依据"])

    def test_system_monitor_alert_error_bypasses_account_warning_streak_gate(self):
        flag = {
            "code": "account_runtime_unavailable",
            "severity": "error",
            "title": "Account runtime unavailable",
            "detail": "session_authenticated=False",
        }

        payload, status_code, _, events = self._run_monitor_alert_guard(flags=[flag], status="error")

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(["account_runtime_unavailable"], payload["alert_flag_codes"])
        self.assertEqual([], payload["suppressed_flag_codes"])
        self.assertEqual(1, len(events))
        self.assertEqual("error", events[0]["level"])

    def test_system_monitor_alert_suppresses_legacy_target_gap_in_tv_primary_slim(self):
        flag = {
            "code": "no_active_targets",
            "severity": "warning",
            "title": "No active trade targets",
            "detail": "watchlist 中存在交易标的，但当前 active target 数为 0",
        }

        payload, status_code, _states, events = self._run_monitor_alert_guard(
            flags=[flag],
            config_overrides={
                "ibkr_signal_source": "tradingview",
                "ibkr_tv_primary_runtime_slim_enabled": "TRUE",
            },
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["triggered"])
        self.assertEqual(payload["flag_codes"], [])
        self.assertEqual(payload["suppressed_flag_codes"], ["no_active_targets"])
        self.assertEqual(events, [])

    def test_system_monitor_alert_includes_admission_preview_for_target_gap(self):
        states = {}
        events = []
        preview_requests = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def build_admission_preview(payload):
            preview_requests.append(dict(payload))
            return {
                "ok": True,
                "scanned": 12,
                "eligible": 2,
                "would_admit": 2,
                "admitted_items": [
                    {
                        "symbol": "APP",
                        "direction_bias": "short",
                        "score": 88.5,
                        "window_status": "upper_active",
                        "bars_remaining": 3,
                    },
                    {
                        "symbol": "TOST",
                        "direction_bias": "long",
                        "score": 77,
                        "window_status": "lower_active",
                    },
                ],
            }

        payload, status_code = build_system_monitor_alert_guard_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:05:00", "cn": "2026-04-24 00:05:00", "date": "2026-04-23"},
            build_system_monitor_payload=lambda environment: {
                "status": "warning",
                "flags": [
                    {
                        "code": "no_execution_eligible_targets",
                        "severity": "warning",
                        "title": "No executable trade targets",
                        "detail": "active targets are observe only",
                    }
                ],
                "runtime": {"session": {"authenticated": True}, "websocket": {"connected": True}},
                "scheduler": {"latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {"status_counts": {"running": 8}, "services": {}},
                "pocketbase": {"disk": {"filesystem": {"used_pct": 10}}},
            },
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
            build_admission_preview=build_admission_preview,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(1, len(preview_requests))
        self.assertTrue(preview_requests[0]["dry_run"])
        self.assertTrue(preview_requests[0]["force"])
        self.assertEqual("monitor_alert_preview", preview_requests[0]["trigger_source"])
        self.assertEqual(2, payload["admission_preview"]["would_admit"])
        self.assertIn("would_admit 2", events[0]["detail"]["入池预览"])
        self.assertIn("APP(short", events[0]["detail"]["可能加入"])

    def test_daily_report_skips_outside_target_window(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-04-28 00:00:45", date="2026-04-28", daily=True),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "outside_time_window")
        self.assertEqual(payload["target_time_et"], "16:05")
        self.assertEqual(payload["window_minutes"], 30)
        self.assertEqual(len(sent), 0)
        self.assertEqual(pb.states, {})

    def test_daily_report_allows_delayed_scheduler_within_window(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-04-23 16:20:00", daily=True),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["skipped"])
        self.assertTrue(payload["notified"])
        self.assertEqual(len(sent), 1)
        state = pb.states[("system_notify_daily", "live", "2026-04-23")]["data"]
        self.assertEqual(state["close_sent_at"], "2026-04-23 16:20:00")
        self.assertEqual(state["close_message_id"], "msg-1")

    def test_daily_report_skips_after_expanded_window_boundary(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-04-23 16:35:00", daily=True),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "outside_time_window")
        self.assertEqual(payload["target_time_et"], "16:05")
        self.assertEqual(payload["window_minutes"], 30)
        self.assertEqual(len(sent), 0)
        self.assertEqual(pb.states, {})

    def test_daily_report_skips_non_trading_day_without_feishu(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-05-09 16:05:00", date="2026-05-09", daily=True),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "non_trading_day")
        self.assertFalse(payload["trading_day"])
        self.assertEqual(payload["market_date"], "2026-05-09")
        self.assertEqual(len(sent), 0)
        state = pb.states[("system_notify_daily", "live", "2026-05-09")]["data"]
        self.assertEqual(state["close_sent_at"], "2026-05-09 16:05:00")
        self.assertEqual(state["close_reason"], "non_trading_day")
        self.assertEqual(state["close_daily_scan_status"], "non_trading_day")
        self.assertFalse(state["close_notified"])

    def test_daily_report_is_idempotent_per_day_at_close_time(self):
        pb = _ReminderPB()
        sent = []

        for _ in range(2):
            payload, status_code = build_system_daily_report_response(
                payload={"environment": "live"},
                **self._reminder_deps(pb, sent, now_us="2026-04-23 16:05:00", daily=True),
            )
            self.assertEqual(status_code, 200)
            self.assertTrue(payload["ok"])

        self.assertEqual(len(sent), 1)
        state = pb.states[("system_notify_daily", "live", "2026-04-23")]["data"]
        self.assertEqual(state["close_sent_at"], "2026-04-23 16:05:00")
        self.assertTrue(state["close_notified"])
        self.assertTrue(state["close_persisted"])
        self.assertEqual(state["close_message_id"], "msg-1")
        self.assertEqual(sent[0]["chat_id"], "startup-chat-live")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("**结论**", card_text)
        self.assertIn("**需要处理**", card_text)
        self.assertIn("TV webhook", card_text)
        self.assertIn("Targets", card_text)
        self.assertIn("Orders", card_text)
        self.assertIn("Execution/Protection", card_text)
        self.assertIn("今日止盈/止损", card_text)
        self.assertIn("今日盈亏", card_text)
        self.assertEqual(sent[0]["card"]["header"]["template"], "green")
        self.assertNotIn("bars", card_text)
        self.assertNotIn("日筛", card_text)
        self.assertNotIn("数据链路可能未落库", card_text)

    def test_daily_report_includes_protective_exit_and_pnl_stats(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(
                pb,
                sent,
                now_us="2026-04-23 16:05:00",
                today={
                    "ibkr_bars": 10,
                    "ibkr_signals": 2,
                    "orders": 4,
                    "events": 0,
                    "take_profit_filled": 2,
                    "stop_loss_filled": 1,
                    "protective_take_profit_filled": 1,
                    "protective_stop_loss_filled": 1,
                    "close_take_profit_filled": 1,
                    "close_stop_loss_filled": 0,
                    "close_flat_filled": 1,
                    "close_unclassified_filled": 1,
                    "winning_trades": 2,
                    "losing_trades": 1,
                    "realized_net_pnl": 123.45,
                    "profit_amount": 200,
                    "loss_amount": -76.55,
                    "commission": 7.0,
                    "pnl_missing_count": 1,
                },
                daily=True,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("**今日止盈/止损**: 止盈成交 2（保护 1 + 平仓 1） | 止损成交 1（保护 1 + 平仓 0） | 平本 1 | 未判定 1", card_text)
        self.assertIn("**今日盈亏**: 净 +$123.45 | 盈利 2/+$200.00 | 亏损 1/-$76.55 | 手续费 $7.00 | PnL缺失 1", card_text)

    def test_daily_report_labels_paper_broker_with_shared_live_data(self):
        pb = _ReminderPB()
        sent = []
        summary_calls = []
        target_payloads = []
        deps = self._reminder_deps(pb, sent, now_us="2026-04-23 16:05:00", daily=True)
        base_summary = deps["build_system_summary_payload"]
        base_targets = deps["build_today_targets_response"]

        def build_system_summary_payload(environment, lite_mode=False):
            summary_calls.append(environment)
            return base_summary(environment, lite_mode=lite_mode)

        def build_today_targets_response(*, payload):
            target_payloads.append(payload)
            return base_targets(payload=payload)

        deps["build_system_summary_payload"] = build_system_summary_payload
        deps["build_today_targets_response"] = build_today_targets_response

        payload, status_code = build_system_daily_report_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **deps,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(sent[0]["chat_id"], "startup-chat-paper")
        self.assertEqual(sent[0]["environment"], "paper")
        self.assertIn("Broker PAPER", sent[0]["card"]["header"]["title"]["content"])
        self.assertEqual(summary_calls, ["paper"])
        self.assertEqual(target_payloads[0]["broker_mode"], "paper")
        self.assertEqual(target_payloads[0]["market_data_mode"], "live")
        self.assertEqual(target_payloads[0]["environment"], "live")
        state = pb.states[("system_notify_daily", "paper", "2026-04-23")]["data"]
        self.assertEqual(state["close_message_id"], "msg-1")

    def test_daily_report_does_not_mark_sent_when_notification_fails(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(
                pb,
                sent,
                now_us="2026-04-23 16:05:00",
                event_result={"notified": False, "persisted": True, "error": "missing_token"},
                daily=True,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "missing_token")
        state = pb.states[("system_notify_daily", "live", "2026-04-23")]["data"]
        self.assertNotIn("close_sent_at", state)
        self.assertEqual(state["close_last_attempt_at"], "2026-04-23 16:05:00")
        self.assertEqual(state["close_error"], "missing_token")
        self.assertFalse(state["close_notified"])
        self.assertEqual(len(sent), 1)

    def test_daily_report_marks_sent_when_notifications_are_disabled(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(
                pb,
                sent,
                now_us="2026-04-23 16:05:00",
                config_overrides={"daily_summary_notify_enabled": "FALSE"},
                daily=True,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        state = pb.states[("system_notify_daily", "live", "2026-04-23")]["data"]
        self.assertEqual(state["close_sent_at"], "2026-04-23 16:05:00")
        self.assertEqual(state["close_reason"], "daily_summary_notify_disabled")
        self.assertEqual(state["close_error"], "")
        self.assertEqual(len(sent), 0)

    def test_daily_report_ignores_zero_bars_in_tv_primary_notice(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(
                pb,
                sent,
                now_us="2026-04-23 16:05:00",
                today={"ibkr_bars": 0, "ibkr_signals": 0, "orders": 0, "events": 0},
                daily=True,
            ),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sent[0]["card"]["header"]["template"], "green")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertNotIn("bars 0", card_text)
        self.assertNotIn("数据链路可能未落库", card_text)
        self.assertNotIn("Scheduler ingest", card_text)
        self.assertIn("TV webhook", card_text)

    def test_system_summary_payload_uses_loaded_today_counts(self):
        payload = build_system_summary_payload(
            "live",
            lite_mode=True,
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            load_effective_config_map=lambda environment, selected_keys=None: {},
            is_enabled_text=lambda value: True,
            fetch_compute_health=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_compute_status=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_runtime_status=lambda environment: {"ok": True, "payload": {"status": "running", "environment": environment}},
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            merge_service_topology=lambda *payloads: {"services": {}},
            load_recent_system_events=lambda environment, limit: [],
            time_strings=lambda now_ts=None: {"us": "2026-05-01 16:05:00", "cn": "2026-05-02 04:05:00", "date": "2026-05-01"},
            load_today_counts=lambda environment, market_date: {
                "ibkr_bars": 22134,
                "ibkr_indicators": 8494,
                "ibkr_signals": 0,
                "orders": 0,
                "main_orders": 0,
                "order_groups": 0,
                "events": 98,
                "ibkr_targets": 9,
                "take_profit_filled": 2,
                "stop_loss_filled": 1,
                "protective_take_profit_filled": 1,
                "protective_stop_loss_filled": 1,
                "close_take_profit_filled": 1,
                "close_stop_loss_filled": 0,
                "close_flat_filled": 1,
                "close_unclassified_filled": 1,
                "close_filled": 3,
                "manual_close_filled": 3,
                "winning_trades": 2,
                "losing_trades": 1,
                "flat_trades": 0,
                "pnl_missing_count": 1,
                "realized_gross_pnl": 130.45,
                "realized_net_pnl": 123.45,
                "profit_amount": 200.0,
                "loss_amount": -76.55,
                "commission": 7.0,
            },
        )

        self.assertEqual(payload["today_market_date"], "2026-05-01")
        self.assertEqual(payload["today"]["ibkr_bars"], 22134)
        self.assertEqual(payload["today"]["ibkr_indicators"], 8494)
        self.assertEqual(payload["today"]["main_orders"], 0)
        self.assertEqual(payload["today"]["order_groups"], 0)
        self.assertEqual(payload["today"]["events"], 98)
        self.assertEqual(payload["today"]["ibkr_targets"], 9)
        self.assertEqual(payload["today"]["take_profit_filled"], 2)
        self.assertEqual(payload["today"]["stop_loss_filled"], 1)
        self.assertEqual(payload["today"]["protective_take_profit_filled"], 1)
        self.assertEqual(payload["today"]["protective_stop_loss_filled"], 1)
        self.assertEqual(payload["today"]["close_take_profit_filled"], 1)
        self.assertEqual(payload["today"]["close_stop_loss_filled"], 0)
        self.assertEqual(payload["today"]["close_flat_filled"], 1)
        self.assertEqual(payload["today"]["close_unclassified_filled"], 1)
        self.assertEqual(payload["today"]["close_filled"], 3)
        self.assertEqual(payload["today"]["manual_close_filled"], 3)
        self.assertEqual(payload["today"]["winning_trades"], 2)
        self.assertEqual(payload["today"]["losing_trades"], 1)
        self.assertEqual(payload["today"]["pnl_missing_count"], 1)
        self.assertEqual(payload["today"]["realized_gross_pnl"], 130.45)
        self.assertEqual(payload["today"]["realized_net_pnl"], 123.45)
        self.assertEqual(payload["today"]["profit_amount"], 200.0)
        self.assertEqual(payload["today"]["loss_amount"], -76.55)
        self.assertEqual(payload["today"]["commission"], 7.0)
        self.assertNotIn("today_errors", payload)

    def test_system_summary_payload_loads_today_counts_by_broker_mode(self):
        calls = []

        def load_today_counts(environment, market_date):
            calls.append((environment, market_date))
            return {
                "ibkr_bars": 22134,
                "ibkr_indicators": 8494,
                "ibkr_signals": 31,
                "orders": 27,
                "main_orders": 8,
                "order_groups": 8,
                "events": 9,
                "ibkr_targets": 46,
                "take_profit_filled": 0,
                "stop_loss_filled": 4,
            }

        payload = build_system_summary_payload(
            "paper",
            lite_mode=True,
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            load_effective_config_map=lambda environment, selected_keys=None: {},
            is_enabled_text=lambda value: True,
            fetch_compute_health=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_compute_status=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_runtime_status=lambda environment: {"ok": True, "payload": {"status": "running", "environment": environment}},
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            merge_service_topology=lambda *payloads: {"services": {}},
            load_recent_system_events=lambda environment, limit: [],
            time_strings=lambda now_ts=None: {"us": "2026-05-20 16:05:00", "cn": "2026-05-21 04:05:00", "date": "2026-05-20"},
            load_today_counts=load_today_counts,
        )

        self.assertEqual(calls, [("paper", "2026-05-20")])
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(payload["today"]["orders"], 27)
        self.assertEqual(payload["today"]["main_orders"], 8)
        self.assertEqual(payload["today"]["stop_loss_filled"], 4)

    def test_system_summary_payload_preserves_response_when_today_counts_fail(self):
        payload = build_system_summary_payload(
            "live",
            lite_mode=True,
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            load_effective_config_map=lambda environment, selected_keys=None: {},
            is_enabled_text=lambda value: True,
            fetch_compute_health=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_compute_status=lambda environment: {"ok": True, "payload": {"status": "running"}},
            fetch_runtime_status=lambda environment: {"ok": True, "payload": {"status": "running", "environment": environment}},
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            merge_service_topology=lambda *payloads: {"services": {}},
            load_recent_system_events=lambda environment, limit: [],
            time_strings=lambda now_ts=None: {"us": "2026-05-01 16:05:00", "cn": "2026-05-02 04:05:00", "date": "2026-05-01"},
            load_today_counts=lambda environment, market_date: (_ for _ in ()).throw(RuntimeError("pb offline")),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["today"]["ibkr_bars"], 0)
        self.assertIn("_summary", payload["today_errors"])


if __name__ == "__main__":
    unittest.main()
