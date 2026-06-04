import copy
import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.signals.expiry import build_signal_expiry_response


class _FakePB:
    def __init__(self, signal_rows=None, order_rows=None):
        self.signals = {str(row["id"]): copy.deepcopy(row) for row in (signal_rows or [])}
        self.orders = {str(row["id"]): copy.deepcopy(row) for row in (order_rows or [])}
        self.updated = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        filter_text = str(filter or "")
        environment = self._extract_string(filter_text, "environment")
        if collection == "ibkr_signals":
            statuses = re.findall(r'status\s*=\s*"([^"]*)"', filter_text)
            rows = []
            for row in self.signals.values():
                if environment and str(row.get("environment") or "") != environment:
                    continue
                if statuses and str(row.get("status") or "") not in statuses:
                    continue
                rows.append(copy.deepcopy(row))
            return rows[:per_page]
        if collection == "orders":
            signal_id = self._extract_string(filter_text, "signal_id")
            rows = []
            for row in self.orders.values():
                if environment and str(row.get("environment") or "") != environment:
                    continue
                if signal_id and str(row.get("signal_id") or "") != signal_id:
                    continue
                rows.append(copy.deepcopy(row))
            return rows[:per_page]
        return []

    def update_record(self, collection, record_id, patch):
        current = copy.deepcopy(self.signals[str(record_id)])
        current.update(copy.deepcopy(patch))
        self.signals[str(record_id)] = current
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    @staticmethod
    def _extract_string(filter_text, field_name):
        match = re.search(rf'{re.escape(field_name)}\s*=\s*"([^"]*)"', filter_text)
        return match.group(1) if match else ""


class SignalExpiryBuildersTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')
        self.signal_chat_id_fn = lambda environment: f"signal-chat-{environment}"

    def test_signal_expiry_marks_stale_signal_expired(self):
        updated_cards = []
        pb = _FakePB(
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "awaiting_confirm",
                    "bar_time_ms": 1713797700000,
                    "us_time": "2026-04-22 09:35:00",
                    "extra": {"feishu_signal_message_id": "msg-old"},
                }
            ]
        )

        payload, status_code = build_signal_expiry_response(
            pb,
            payload={"broker_mode": "live", "market_data_mode": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "30",
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-new"},
            update_interactive=lambda message_id, card, environment: updated_cards.append((message_id, card, environment)) or {"success": True, "message_id": "msg-old"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["expired_count"], 1)
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["extra"]["expired_by"], "signal_expiry_check")
        self.assertEqual(row["extra"]["expired_reason"], "signal_expired")
        self.assertEqual(row["extra"]["status_reason"], "signal_expired")
        self.assertEqual(row["extra"]["expiry_reference"], "bar_time_ms")
        self.assertEqual(row["extra"]["signal_validity_minutes"], 30)
        self.assertEqual(row["extra"]["feishu_signal_message_id"], "msg-old")
        self.assertEqual(row["extra"]["feishu_signal_notify_last_action"], "expired")
        self.assertIn("**信号有效期**: 30 分钟", updated_cards[0][1]["elements"][0]["content"])

    def test_signal_expiry_marks_paper_broker_expired_without_live_top_level_status(self):
        sent_cards = []
        pb = _FakePB(
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "note": "paper:history_repair_pending",
                    "bar_time_ms": 1713797700000,
                    "us_time": "2026-04-22 09:35:00",
                    "extra": {"broker_mode": "paper"},
                }
            ]
        )

        payload, status_code = build_signal_expiry_response(
            pb,
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "30",
            send_interactive=lambda card, *_args, **_kwargs: sent_cards.append(card) or {"success": True, "message_id": "msg-new"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-old"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["market_data_mode"], "live")
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["note"], "")
        self.assertEqual(row["extra"]["broker_mode"], "paper")
        self.assertEqual(row["extra"]["data_environment"], "live")
        self.assertEqual(row["extra"]["execution_by_mode"]["paper"]["status"], "expired")
        self.assertEqual(row["extra"]["execution_by_mode"]["paper"]["note"], "signal_expired")
        content = sent_cards[0]["elements"][0]["content"]
        self.assertIn("**状态**: 已过期", content)
        self.assertIn("**原因**: signal_expired", content)
        self.assertIn("**信号有效期**: 30 分钟", content)
        self.assertNotIn("paper:history_repair_pending", content)

    def test_signal_expiry_skips_broker_cancelled_signal(self):
        pb = _FakePB(
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "bar_time_ms": 1713797700000,
                    "extra": {"execution_by_mode": {"paper": {"status": "cancelled"}}},
                }
            ]
        )

        payload, status_code = build_signal_expiry_response(
            pb,
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "30",
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-new"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-old"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["expired_count"], 0)
        self.assertEqual(pb.updated, [])

    def test_signal_expiry_repairs_signal_to_submitted_when_orders_exist(self):
        pb = _FakePB(
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "bar_time_ms": 1713797700000,
                    "extra": {},
                }
            ],
            order_rows=[
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "role": "entry",
                    "status": "Submitted",
                }
            ],
        )

        payload, status_code = build_signal_expiry_response(
            pb,
            payload={"broker_mode": "live", "market_data_mode": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "30",
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["repaired_to_order_status_count"], 1)
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["extra"]["status_repaired_by"], "signal_expiry_check")
        self.assertEqual(row["extra"]["status_repair_reason"], "orders_detected_before_expiry")
        self.assertEqual(row["extra"]["linked_order_unique_ids"], ["sig-1_entry"])

    def test_signal_expiry_repairs_signal_to_protected_active_when_entry_filled_with_protection(self):
        pb = _FakePB(
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "bar_time_ms": 1713797700000,
                    "extra": {},
                }
            ],
            order_rows=[
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "role": "entry",
                    "status": "Filled",
                },
                {
                    "id": "order-2",
                    "unique_id": "sig-1_tp",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "role": "take_profit",
                    "status": "Submitted",
                },
                {
                    "id": "order-3",
                    "unique_id": "sig-1_sl",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "role": "stop_loss",
                    "status": "Submitted",
                },
            ],
        )

        payload, status_code = build_signal_expiry_response(
            pb,
            payload={"broker_mode": "live", "market_data_mode": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "30",
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["extra"]["status_repair_reason"], "entry_filled_and_protection_submitted")
        self.assertEqual(
            row["extra"]["linked_order_unique_ids"],
            ["sig-1_entry", "sig-1_tp", "sig-1_sl"],
        )


if __name__ == "__main__":
    unittest.main()
