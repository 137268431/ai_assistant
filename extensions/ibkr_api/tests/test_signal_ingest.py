import copy
import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.signals.ingest import build_signal_ingest_response, build_signals_ingest_response
from ibkr_api.signals.notifications import build_signal_notification_card, build_signal_status_card


class _FakePB:
    def __init__(self, signal_rows=None, order_rows=None):
        self.signals = {
            str(row["id"]): copy.deepcopy(row)
            for row in (signal_rows or [])
        }
        self.orders = {
            str(row["id"]): copy.deepcopy(row)
            for row in (order_rows or [])
        }
        self.reverse_signals = {}
        self.created = []
        self.updated = []

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return copy.deepcopy(rows[0]) if rows else None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "ibkr_reverse_signals":
            return list(copy.deepcopy(row) for row in self.reverse_signals.values())[:per_page]
        if collection == "orders":
            return list(copy.deepcopy(row) for row in self._filter_orders(filter))[:per_page]
        if collection != "ibkr_signals":
            return []
        filter_text = str(filter or "")
        environment = self._extract_string(filter_text, "environment")
        signal_id = self._extract_string(filter_text, "signal_id")
        excluded_signal_id = self._extract_string(filter_text, "signal_id", operator="!=")
        symbol = self._extract_string(filter_text, "symbol")
        direction = self._extract_string(filter_text, "direction")
        interval = self._extract_string(filter_text, "interval")
        chart_tf = self._extract_string(filter_text, "chart_tf")
        script_tag = self._extract_string(filter_text, "script_tag")
        bar_time_ms = self._extract_number(filter_text, "bar_time_ms")

        rows = []
        for row in self.signals.values():
            if environment and str(row.get("environment") or "") != environment:
                continue
            if signal_id and str(row.get("signal_id") or "") != signal_id:
                continue
            if excluded_signal_id and str(row.get("signal_id") or "") == excluded_signal_id:
                continue
            if symbol and str(row.get("symbol") or "") != symbol:
                continue
            if direction and str(row.get("direction") or "") != direction:
                continue
            if interval and str(row.get("interval") or "") != interval:
                continue
            if chart_tf and str(row.get("chart_tf") or "") != chart_tf:
                continue
            if script_tag and str(row.get("script_tag") or "") != script_tag:
                continue
            if bar_time_ms is not None and int(row.get("bar_time_ms") or 0) != bar_time_ms:
                continue
            rows.append(copy.deepcopy(row))
        return rows[:per_page]

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = f"{collection}-{len(self.created) + 1}"
        self.created.append((collection, copy.deepcopy(row)))
        if collection == "ibkr_signals":
            self.signals[str(row["id"])] = copy.deepcopy(row)
        if collection == "ibkr_reverse_signals":
            self.reverse_signals[str(row["id"])] = copy.deepcopy(row)
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
        if collection == "ibkr_reverse_signals":
            current = copy.deepcopy(self.reverse_signals[str(record_id)])
            current.update(copy.deepcopy(patch))
            self.reverse_signals[str(record_id)] = current
            self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
            return copy.deepcopy(current)
        current = copy.deepcopy(self.signals[str(record_id)])
        current.update(copy.deepcopy(patch))
        self.signals[str(record_id)] = current
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    @staticmethod
    def _extract_string(filter_text, field_name, operator="="):
        match = re.search(rf'{re.escape(field_name)}\s*{re.escape(operator)}\s*"([^"]*)"', filter_text)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_number(filter_text, field_name):
        match = re.search(rf"{re.escape(field_name)}\s*=\s*(\d+)", filter_text)
        return int(match.group(1)) if match else None

    def _filter_orders(self, filter_value):
        filter_text = str(filter_value or "")
        environment = self._extract_string(filter_text, "environment")
        signal_id = self._extract_string(filter_text, "signal_id")
        rows = []
        for row in self.orders.values():
            if environment and str(row.get("environment") or "") != environment:
                continue
            if signal_id and str(row.get("signal_id") or "") != signal_id:
                continue
            rows.append(row)
        return rows


class SignalIngressBuildersTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')
        self.config_value = lambda key, default, environment: "true" if key == "signal_manual_confirm_enabled" else default
        self.signal_chat_id_fn = lambda environment: f"signal-chat-{environment}"

    def test_signal_ingest_creates_signal_with_manual_confirm_and_notification_metadata(self):
        pb = _FakePB()

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "aapl",
                "signal_id": "sig-1",
                "direction": "Long",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "bar_time_ms": 1713797700000,
                "interval": "5m",
                "chart_tf": "5m",
                "script_tag": "main",
                "extra": {"source": "tv"},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda card, chat_id, environment: {
                "success": True,
                "message_id": f"{chat_id}:{environment}",
                "card": card,
            },
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "created")
        self.assertEqual(payload["status"], "awaiting_confirm")
        row = pb.signals["ibkr_signals-1"]
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["status"], "awaiting_confirm")
        self.assertEqual(row["note"], "manual_confirmation_required")
        self.assertEqual(row["extra"]["signal_confirmation_required"], True)
        self.assertEqual(row["extra"]["signal_confirmation_mode"], "manual")
        self.assertEqual(row["extra"]["status_reason"], "manual_confirmation_required")
        self.assertEqual(row["extra"]["signal_source"], "tradingview_webhook")
        self.assertEqual(row["extra"]["source"], "tv")
        self.assertEqual(row["extra"]["feishu_signal_message_id"], "signal-chat-live:live")
        self.assertEqual(row["extra"]["feishu_signal_card_version"], 1)

    def test_signal_notification_failure_records_feishu_error_detail(self):
        pb = _FakePB()

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "aapl",
                "signal_id": "sig-failed",
                "direction": "long",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "bar_time_ms": 1713797700000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {
                "success": False,
                "error": "http_400",
                "http_status": 400,
                "api_code": 99991663,
                "api_message": "invalid card payload",
                "response_body": '{"code":99991663,"msg":"invalid card payload"}',
            },
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        extra = pb.signals["ibkr_signals-1"]["extra"]
        self.assertEqual(extra["feishu_signal_notify_last_result"], "failed")
        self.assertEqual(extra["feishu_signal_notify_error"], "http_400")
        self.assertEqual(extra["feishu_signal_notify_http_status"], 400)
        self.assertEqual(extra["feishu_signal_notify_api_code"], 99991663)
        self.assertEqual(extra["feishu_signal_notify_api_message"], "invalid card payload")
        self.assertIn("invalid card payload", extra["feishu_signal_notify_response_body"])

    def test_signal_notification_card_uses_callback_request_actions(self):
        sent = []
        pb = _FakePB()

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "aapl",
                "signal_id": "sig-url",
                "direction": "long",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "bar_time_ms": 1713797700000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda card, chat_id, environment: sent.append(card) or {"success": True, "message_id": "msg-url"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        card = sent[0]
        self.assertIn("elements", card)
        self.assertNotIn("schema", card)
        self.assertNotIn("body", card)
        actions = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]
        request_actions = [action for action in actions if action.get("action_type") == "request"]
        self.assertEqual([action["text"]["content"] for action in request_actions], ["确认", "拒绝"])
        self.assertTrue(all(action.get("url") == "https://console.example.com/webhook/feishu/callback" for action in request_actions))
        self.assertEqual(
            [action.get("value") for action in request_actions],
            [
                {"action": "confirm", "signal_id": "sig-url", "environment": "live"},
                {"action": "reject", "signal_id": "sig-url", "environment": "live"},
            ],
        )
        action_urls = [action.get("multi_url", {}).get("url", "") for action in actions]
        self.assertFalse(any("/webhook/signal/confirm" in url for url in action_urls))
        self.assertFalse(any("/webhook/signal/cancel" in url for url in action_urls))
        self.assertTrue(any("/ibkr_signals.html" in url and "signal_id=sig-url" in url for url in action_urls))
        self.assertEqual([], [action for action in actions if "behaviors" in action])

    def test_signal_notification_card_separates_reference_limit_and_actual_fill_prices(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-price",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "protected_active",
            "entry": 100.1,
            "stop_loss": 98.1,
            "take_profit": 104.1,
            "shares": 10,
            "extra": {
                "pre_submit_reference_price": 100.2,
                "pre_submit_reference_source": "last_price",
                "entry_fill_price": 100.08,
            },
        }

        notification_card = build_signal_notification_card(record, console_base_url="https://console.example.com")
        status_card = build_signal_status_card(record, message="entry filled", console_base_url="https://console.example.com")

        for card in (notification_card, status_card):
            content = card["elements"][0]["content"]
            self.assertIn("**参考价**: 100.20 (last_price)", content)
            self.assertIn("**入场限价 / 止盈 / 止损**: 100.10 / 104.10 / 98.10", content)
            self.assertIn("**实际成交价**: 100.08", content)
            self.assertNotIn("**入场 / 止盈 / 止损**", content)

    def test_signal_ingest_skips_duplicate_bar_signal_and_annotates_existing_row(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-existing",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "interval": "5m",
                    "chart_tf": "5m",
                    "script_tag": "main",
                    "bar_time_ms": 1713797700000,
                    "status": "awaiting_confirm",
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-new",
                "direction": "long",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "bar_time_ms": 1713797700000,
                "interval": "5m",
                "chart_tf": "5m",
                "script_tag": "main",
                "extra": {"signal_source": "timeline"},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "skipped_duplicate_bar_signal")
        self.assertEqual(payload["signal_id"], "sig-existing")
        self.assertEqual(payload["duplicate_signal_id"], "sig-new")
        self.assertEqual(len(pb.created), 0)
        extra = pb.signals["sig-row-1"]["extra"]
        self.assertEqual(extra["duplicate_signal_count"], 1)
        self.assertEqual(extra["last_duplicate_signal_id"], "sig-new")
        self.assertEqual(extra["last_duplicate_signal_source"], "timeline")
        self.assertEqual(extra["duplicate_bar_dedupe_key"], "live|AAPL|long|1713797700000|5m|5m|main")

    def test_signal_ingest_preserves_terminal_status_on_replay(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "executed",
                    "note": "broker_filled",
                    "extra": {"status_reason": "broker_filled"},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-1",
                "direction": "long",
                "status": "pending",
                "note": "manual_confirmation_required",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "executed")
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "executed")
        self.assertEqual(row["note"], "broker_filled")
        self.assertEqual(row["extra"]["status_reason"], "broker_filled")

    def test_signal_ingest_preserves_submitted_status_on_replay(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "submitted",
                    "note": "order_submitted_by_ibkr_compute",
                    "extra": {"status_reason": "order_submitted_by_ibkr_compute"},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-1",
                "direction": "long",
                "status": "pending",
                "note": "manual_confirmation_required",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "msg-1"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "submitted")
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["note"], "order_submitted_by_ibkr_compute")
        self.assertEqual(row["extra"]["status_reason"], "order_submitted_by_ibkr_compute")

    def test_signal_ingest_refreshes_same_direction_unsubmitted_active_signal(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "awaiting_confirm",
                    "entry": 180.0,
                    "stop_loss": 178.0,
                    "take_profit": 184.0,
                    "bar_time_ms": 1713797700000,
                    "extra": {"feishu_signal_message_id": "msg-old"},
                }
            ]
        )
        updates = []

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-new",
                "direction": "long",
                "signal": "long_setup",
                "entry": 181.0,
                "stop_loss": 179.0,
                "take_profit": 186.0,
                "bar_time_ms": 1713798000000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda message_id, card, environment: updates.append((message_id, card, environment)) or {
                "success": True,
                "message_id": message_id,
            },
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "refreshed_active_signal")
        self.assertEqual(payload["signal_id"], "sig-old")
        self.assertEqual(len(pb.created), 0)
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["entry"], 181.0)
        self.assertEqual(row["take_profit"], 186.0)
        self.assertEqual(row["signal_id"], "sig-old")
        self.assertEqual(row["extra"]["merged_signal_ids"], ["sig-new"])
        self.assertEqual(updates[0][0], "msg-old")

    def test_signal_ingest_suppresses_same_direction_after_broker_submission(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "submitted",
                    "entry": 180.0,
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-followup",
                "direction": "long",
                "signal": "long_setup",
                "entry": 181.0,
                "stop_loss": 179.0,
                "take_profit": 186.0,
                "bar_time_ms": 1713798000000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "suppressed_same_direction_followup")
        self.assertEqual(len(pb.created), 0)
        extra = pb.signals["sig-row-1"]["extra"]
        self.assertEqual(extra["followup_signal_ids"], ["sig-followup"])
        self.assertEqual(extra["suppressed_reason"], "same_direction_broker_order_active")

    def test_signal_ingest_reconfirms_confirmed_signal_when_followup_changes_execution_params(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "pending",
                    "entry": 180.0,
                    "limit_price": 0,
                    "stop_loss": 178.0,
                    "take_profit": 184.0,
                    "shares": 20,
                    "extra": {
                        "feishu_signal_message_id": "msg-old",
                        "confirmed_by": "manual",
                        "confirmed_at": "2026-05-13T13:35:00Z",
                    },
                }
            ]
        )
        updates = []

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-followup",
                "direction": "long",
                "signal": "long_setup",
                "entry": 181.0,
                "stop_loss": 179.0,
                "take_profit": 186.0,
                "shares": 20,
                "bar_time_ms": 1713798000000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda message_id, card, environment: updates.append((message_id, card, environment)) or {
                "success": True,
                "message_id": message_id,
            },
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "reconfirm_same_direction_followup")
        self.assertEqual(payload["status"], "awaiting_confirm")
        self.assertEqual(payload["changed_fields"], ["entry", "take_profit", "stop_loss"])
        self.assertEqual(len(pb.created), 0)
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "awaiting_confirm")
        self.assertEqual(row["note"], "followup_requires_reconfirm")
        self.assertEqual(row["entry"], 181.0)
        self.assertEqual(row["take_profit"], 186.0)
        self.assertEqual(row["signal_id"], "sig-old")
        self.assertEqual(row["extra"]["followup_requires_reconfirm"], True)
        self.assertEqual(row["extra"]["confirmation_stale"], True)
        self.assertEqual(row["extra"]["merged_signal_ids"], ["sig-followup"])
        self.assertEqual(row["extra"]["followup_signal_ids"], ["sig-followup"])
        self.assertEqual(row["extra"]["previous_confirmed_snapshot"]["entry"], 180.0)
        self.assertEqual(row["extra"]["previous_confirmed_snapshot"]["confirmed_by"], "manual")
        self.assertEqual(updates[0][0], "msg-old")
        self.assertIn("信号已更新，需重新确认", updates[0][1]["header"]["title"]["content"])
        action_blocks = [el for el in updates[0][1]["elements"] if el.get("tag") == "action"]
        self.assertTrue(any(action.get("value", {}).get("action") == "confirm" for block in action_blocks for action in block.get("actions", [])))

    def test_signal_ingest_records_same_direction_pending_followup_without_execution_change(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "pending",
                    "entry": 180.0,
                    "limit_price": 0,
                    "stop_loss": 178.0,
                    "take_profit": 184.0,
                    "shares": 20,
                    "extra": {"feishu_signal_message_id": "msg-old"},
                }
            ]
        )
        updates = []

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-followup",
                "direction": "long",
                "signal": "long_setup",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "shares": 20,
                "bar_time_ms": 1713798000000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda *args, **kwargs: updates.append((args, kwargs)) or {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "suppressed_same_direction_followup")
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["entry"], 180.0)
        self.assertEqual(row["extra"]["followup_signal_ids"], ["sig-followup"])
        self.assertEqual(row["extra"]["suppressed_reason"], "same_direction_followup_no_execution_change")
        self.assertEqual(updates, [])

    def test_signal_ingest_does_not_reconfirm_pending_signal_with_order_trace(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "pending",
                    "entry": 180.0,
                    "limit_price": 0,
                    "stop_loss": 178.0,
                    "take_profit": 184.0,
                    "shares": 20,
                    "extra": {"feishu_signal_message_id": "msg-old"},
                }
            ],
            order_rows=[{"id": "order-1", "environment": "live", "signal_id": "sig-old"}],
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-followup",
                "direction": "long",
                "signal": "long_setup",
                "entry": 181.0,
                "stop_loss": 179.0,
                "take_profit": 186.0,
                "shares": 20,
                "bar_time_ms": 1713798000000,
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "suppressed_same_direction_followup")
        row = pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["entry"], 180.0)
        self.assertEqual(row["extra"]["suppressed_reason"], "same_direction_order_trace_active")

    def test_signal_ingest_suppresses_weak_reverse_against_broker_order(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "submitted",
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-weak-short",
                "direction": "short",
                "signal": "",
                "entry": 179.0,
                "bar_time_ms": 1713798000000,
                "extra": {"signal_strength_score": 3},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "suppressed_weak_reverse_signal")
        self.assertEqual(len(pb.created), 0)
        extra = pb.signals["sig-row-1"]["extra"]
        self.assertEqual(extra["latest_suppressed_reverse_signal_id"], "sig-weak-short")
        self.assertEqual(extra["latest_suppressed_reverse_reason"], "reverse_signal_below_strong_threshold")

    def test_signal_ingest_queues_full_auto_reverse_for_strong_opposite_broker_signal(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "submitted",
                    "extra": {"trade_group_id": "group-1"},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-strong-short",
                "direction": "short",
                "signal": "short_setup",
                "entry": 179.0,
                "stop_loss": 181.0,
                "take_profit": 174.0,
                "bar_time_ms": 1713798000000,
                "extra": {"signal_strength_score": 7},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "queued_full_auto_reverse")
        self.assertEqual(payload["target"], "ibkr_reverse_signals")
        reverse_rows = [row for collection, row in pb.created if collection == "ibkr_reverse_signals"]
        self.assertEqual(len(reverse_rows), 1)
        reverse = reverse_rows[0]
        self.assertEqual(reverse["action_type"], "cancel")
        self.assertEqual(reverse["source"], "signal")
        self.assertEqual(reverse["extra"]["reverse_stage"], "cancel_old_order")
        self.assertEqual(reverse["extra"]["new_direction"], "short")
        self.assertEqual(reverse["extra"]["reentry_signal_payload"]["signal_id"], "sig-strong-short")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["reverse_policy"], "full_auto_reverse")

    def test_signal_ingest_skips_reverse_when_broker_signal_has_no_order_trace(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "protected_active",
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-strong-short",
                "direction": "short",
                "signal": "short_setup",
                "entry": 179.0,
                "stop_loss": 181.0,
                "take_profit": 174.0,
                "bar_time_ms": 1713798000000,
                "extra": {"signal_strength_score": 7},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["target"], "ibkr_signals")
        self.assertEqual(payload["action"], "created")
        self.assertEqual([], [row for collection, row in pb.created if collection == "ibkr_reverse_signals"])
        self.assertEqual(pb.signals["sig-row-1"]["status"], "closed")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["closed_reason"], "stale_active_signal_without_order_trace")
        created_signals = [row for collection, row in pb.created if collection == "ibkr_signals"]
        self.assertEqual(len(created_signals), 1)
        self.assertEqual(created_signals[0]["extra"]["stale_active_signal_id"], "sig-old")

    def test_signal_ingest_blocks_reverse_when_protection_incomplete(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-old",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "protection_incomplete",
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signal_ingest_response(
            pb,
            payload={
                "environment": "live",
                "symbol": "AAPL",
                "signal_id": "sig-strong-short",
                "direction": "short",
                "signal": "short_setup",
                "entry": 179.0,
                "bar_time_ms": 1713798000000,
                "extra": {"signal_strength_score": 7},
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=self.config_value,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "unused"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["action"], "blocked_reverse_protection_incomplete")
        self.assertEqual([], [row for collection, row in pb.created if collection == "ibkr_reverse_signals"])
        extra = pb.signals["sig-row-1"]["extra"]
        self.assertEqual(extra["latest_suppressed_reverse_reason"], "protection_incomplete_blocks_auto_reverse")

    def test_signal_status_card_labels_protected_active(self):
        card = build_signal_status_card(
            {
                "id": "sig-row-1",
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "direction": "long",
                "environment": "live",
                "status": "protected_active",
                "extra": {"status_reason": "entry_filled_and_protection_submitted"},
            },
            message="entry filled",
            console_base_url="https://console.example.com",
        )

        self.assertIn("保护单已生效", card["header"]["title"]["content"])
        self.assertIn("保护单已生效", card["elements"][0]["content"])

    def test_reconfirm_cards_are_not_labeled_as_new_independent_signal(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-old",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "awaiting_confirm",
            "entry": 181.0,
            "stop_loss": 179.0,
            "take_profit": 186.0,
            "extra": {
                "followup_requires_reconfirm": True,
                "latest_followup_signal_id": "sig-followup",
                "reconfirm_changed_fields": ["entry", "stop_loss", "take_profit"],
                "previous_confirmed_snapshot": {
                    "entry": 180.0,
                    "stop_loss": 178.0,
                    "take_profit": 184.0,
                },
            },
        }

        notification_card = build_signal_notification_card(record, console_base_url="https://console.example.com")
        status_card = build_signal_status_card(record, message="需要重确认", console_base_url="https://console.example.com")

        self.assertIn("信号已更新，需重新确认", notification_card["header"]["title"]["content"])
        self.assertNotIn("新交易信号", notification_card["header"]["title"]["content"])
        self.assertIn("需重确认字段", notification_card["elements"][0]["content"])
        self.assertIn("信号已更新，需重新确认", status_card["header"]["title"]["content"])
        action_blocks = [el for el in status_card["elements"] if el.get("tag") == "action"]
        self.assertTrue(any(action.get("value", {}).get("action") == "confirm" for block in action_blocks for action in block.get("actions", [])))

    def test_signals_batch_aggregates_created_duplicate_and_error_counts(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-existing",
                    "environment": "live",
                    "symbol": "AAPL",
                    "direction": "long",
                    "interval": "5m",
                    "chart_tf": "5m",
                    "script_tag": "main",
                    "bar_time_ms": 1713797700000,
                    "status": "awaiting_confirm",
                    "extra": {},
                }
            ]
        )

        payload, status_code = build_signals_ingest_response(
            pb,
            payload={
                "environment": "live",
                "items": [
                    {
                        "symbol": "MSFT",
                        "signal_id": "sig-2",
                        "direction": "long",
                        "entry": 300.0,
                        "stop_loss": 295.0,
                        "take_profit": 310.0,
                        "bar_time_ms": 1713798000000,
                    },
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig-dup",
                        "direction": "long",
                        "entry": 180.0,
                        "stop_loss": 178.0,
                        "take_profit": 184.0,
                        "bar_time_ms": 1713797700000,
                        "interval": "5m",
                        "chart_tf": "5m",
                        "script_tag": "main",
                    },
                    {
                        "symbol": "NVDA",
                        "direction": "long",
                    },
                ],
            },
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            config_value=lambda key, default, environment: "false" if key == "signal_manual_confirm_enabled" else default,
            send_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "batch-msg"},
            update_interactive=lambda *_args, **_kwargs: {"success": True, "message_id": "batch-msg"},
            signal_chat_id_fn=self.signal_chat_id_fn,
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["received"], 3)
        self.assertEqual(payload["created"], 1)
        self.assertEqual(payload["duplicates"], 1)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["success"], 1)
        created_row = pb.signals["ibkr_signals-1"]
        self.assertEqual(created_row["status"], "pending")
        self.assertEqual(created_row["extra"]["signal_confirmation_mode"], "auto")


if __name__ == "__main__":
    unittest.main()
