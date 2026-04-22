import copy
import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.signals.ingest import build_signal_ingest_response, build_signals_ingest_response


class _FakePB:
    def __init__(self, signal_rows=None):
        self.signals = {
            str(row["id"]): copy.deepcopy(row)
            for row in (signal_rows or [])
        }
        self.created = []
        self.updated = []

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return copy.deepcopy(rows[0]) if rows else None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
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
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
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
