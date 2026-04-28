import os
import sys
import unittest
from pathlib import Path

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
from ibkr_api.system.jobs.order_expiry import build_order_expiry_response
from ibkr_api.system.jobs.reminders import (
    build_system_daily_report_response,
    build_system_market_open_reminder_response,
)


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
            ],
            "ibkr_order_details": [],
        }
        self.created = []
        self.updated = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            if 'role = "entry"' in str(filter):
                return [dict(self.records["orders"][0])]
            if 'trade_group_id = "tg-1"' in str(filter):
                return [dict(row) for row in self.records["orders"]]
        return [dict(row) for row in self.records.get(collection, [])]

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


class _ReminderPB:
    def __init__(self):
        self.states = {}

    def upsert_state(self, state_key, environment, data, date="global"):
        self.states[(state_key, environment, date)] = {"data": dict(data)}
        return self.states[(state_key, environment, date)]


class SystemSchedulerJobsTest(unittest.TestCase):
    def _reminder_deps(self, pb, sent, now_us="2026-04-23 09:20:00", date="2026-04-23"):
        def emit_system_event(**kwargs):
            sent.append(kwargs)
            return {"notified": True, "persisted": True}

        def get_state_payload(state_key, environment, state_date=date):
            return {
                "data": dict(pb.states.get((state_key, environment, state_date), {}).get("data") or {}),
                "environment": environment,
                "date": state_date,
            }

        def build_system_summary_payload(environment, lite_mode=False):
            return {
                "status": "running",
                "today": {"ibkr_bars": 10, "ibkr_signals": 2, "orders": 1, "events": 0},
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

        return {
            "normalize_environment": lambda value, default: str(value or default).strip().lower() or default,
            "time_strings": lambda: {"us": now_us, "cn": "2026-04-23 21:20:00", "date": date},
            "build_system_summary_payload": build_system_summary_payload,
            "build_system_monitor_payload": build_system_monitor_payload,
            "emit_system_event": emit_system_event,
            "get_state_payload": get_state_payload,
            "upsert_state": lambda key, environment, data, state_date: pb.upsert_state(key, environment, data, date=state_date),
        }

    def test_order_expiry_marks_group_canceled(self):
        pb = _OrderExpiryPB()

        def cancel_broker_order(environment, order_id, payload):
            self.assertEqual(environment, "live")
            self.assertEqual(order_id, "12345")
            self.assertEqual(payload["trade_group_id"], "tg-1")
            return {"ok": True, "status_code": 200, "payload": {"ok": True}}

        payload, status_code = build_order_expiry_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            config_value=lambda key, default, environment: "30",
            cancel_broker_order=cancel_broker_order,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed_count"], 2)
        self.assertEqual(payload["expired_group_count"], 1)
        self.assertEqual(len(payload["detail_record_ids"]), 2)
        statuses = {row["id"]: row["status"] for row in pb.records["orders"]}
        self.assertEqual(statuses["entry-1"], "Canceled")
        self.assertEqual(statuses["tp-1"], "Canceled")

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
        self.assertEqual(payload["target_time_et"], "09:20")
        self.assertEqual(len(sent), 0)
        self.assertEqual(pb.states, {})

    def test_daily_report_skips_outside_target_window(self):
        pb = _ReminderPB()
        sent = []

        payload, status_code = build_system_daily_report_response(
            payload={"environment": "live"},
            **self._reminder_deps(pb, sent, now_us="2026-04-28 00:00:45", date="2026-04-28"),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "outside_time_window")
        self.assertEqual(payload["target_time_et"], "16:05")
        self.assertEqual(len(sent), 0)
        self.assertEqual(pb.states, {})

    def test_daily_report_is_idempotent_per_day_at_close_time(self):
        pb = _ReminderPB()
        sent = []

        for _ in range(2):
            payload, status_code = build_system_daily_report_response(
                payload={"environment": "live"},
                **self._reminder_deps(pb, sent, now_us="2026-04-23 16:05:00"),
            )
            self.assertEqual(status_code, 200)
            self.assertTrue(payload["ok"])

        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
