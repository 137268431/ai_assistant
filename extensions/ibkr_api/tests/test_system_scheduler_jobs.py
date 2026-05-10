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
from ibkr_api.system.jobs.early_expansion_topup import build_early_expansion_topup_response
from ibkr_api.system.jobs.intraday_window_admission import build_intraday_window_admission_response
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

    def test_market_open_reminder_skips_non_trading_day(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_market_open_reminder_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-05-09 09:30:00", date="2026-05-09"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "non_trading_day")
        self.assertFalse(payload["trading_day"])
        self.assertEqual(payload["market_date"], "2026-05-09")
        self.assertEqual(len(sent), 0)
        state = pb.states[("system_notify_daily", "live", "2026-05-09")]["data"]
        self.assertEqual(state["open_sent_at"], "2026-05-09 09:30:00")
        self.assertEqual(state["open_reason"], "non_trading_day")
        self.assertFalse(state["open_notified"])

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
        self.assertEqual(payload["new_active"], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "startup-chat-live")
        self.assertEqual(events[0][0], "early_expansion_topup")

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

    def test_intraday_window_admission_adds_valid_window_target_and_reconciles(self):
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
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertEqual(target["extra"]["intraday_window_admission"]["window_status"], "upper_active")
        reconcile_calls = [call for call in request_calls if call["path"] == "/ibkr/universe/reconcile"]
        self.assertEqual(reconcile_calls[0]["json_body"]["prime_symbols"], ["MSFT"])
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.created))

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
        self.assertIn("no_window", payload["rejection_summary"])

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
        self.assertIn("今日结果", card_text)
        self.assertEqual(sent[0]["card"]["header"]["template"], "green")
        self.assertIn("bars 10", card_text)
        self.assertNotIn("数据链路可能未落库", card_text)

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

    def test_daily_report_highlights_zero_bars_at_live_close(self):
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
        self.assertEqual(sent[0]["card"]["header"]["template"], "orange")
        card_text = "\n".join(element.get("content", "") for element in sent[0]["card"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("bars 0", card_text)
        self.assertIn("数据链路可能未落库", card_text)
        self.assertIn("Scheduler ingest", card_text)

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
            },
        )

        self.assertEqual(payload["today_market_date"], "2026-05-01")
        self.assertEqual(payload["today"]["ibkr_bars"], 22134)
        self.assertEqual(payload["today"]["ibkr_indicators"], 8494)
        self.assertEqual(payload["today"]["main_orders"], 0)
        self.assertEqual(payload["today"]["order_groups"], 0)
        self.assertEqual(payload["today"]["events"], 98)
        self.assertEqual(payload["today"]["ibkr_targets"], 9)
        self.assertNotIn("today_errors", payload)

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
