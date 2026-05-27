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
from ibkr_api.orders.notifications import build_order_group_status_card, build_order_status_card


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
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["note"], "")
        self.assertEqual(row["extra"]["signal_confirmation_required"], True)
        self.assertEqual(row["extra"]["signal_confirmation_mode"], "manual")
        self.assertEqual(row["extra"]["status_reason"], "manual_confirmation_required")
        self.assertEqual(row["extra"]["execution_by_mode"]["paper"]["status"], "awaiting_confirm")
        self.assertEqual(row["extra"]["execution_by_mode"]["paper"]["note"], "manual_confirmation_required")
        self.assertEqual(row["extra"]["signal_source"], "tradingview_webhook")
        self.assertEqual(row["extra"]["source"], "tv")
        self.assertEqual(row["extra"]["broker_mode"], "paper")
        self.assertEqual(row["extra"]["data_environment"], "live")
        self.assertEqual(row["extra"]["feishu_signal_message_id"], "signal-chat-paper:paper")
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
                {
                    "action": "confirm",
                    "signal_id": "sig-url",
                    "broker_mode": "paper",
                    "market_data_mode": "live",
                    "data_environment": "live",
                },
                {
                    "action": "reject",
                    "signal_id": "sig-url",
                    "broker_mode": "paper",
                    "market_data_mode": "live",
                    "data_environment": "live",
                },
            ],
        )
        card_text = "\n".join(element.get("content", "") for element in card["elements"] if element.get("tag") == "markdown")
        self.assertIn("Broker PAPER", card["header"]["title"]["content"])
        self.assertIn("**Broker**: Broker PAPER", card_text)
        self.assertIn("**数据**: Shared Data", card_text)
        action_urls = [action.get("multi_url", {}).get("url", "") for action in actions]
        self.assertFalse(any("/webhook/signal/confirm" in url for url in action_urls))
        self.assertFalse(any("/webhook/signal/cancel" in url for url in action_urls))
        self.assertTrue(any("/ibkr_signals.html" in url and "signal_id=sig-url" in url for url in action_urls))
        self.assertTrue(any("/ibkr_lifecycle_flow.html" in url and "signal_id=sig-url" in url for url in action_urls))
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

    def test_signal_cards_include_market_context_metrics(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-metrics",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "pending",
            "entry": 100.1,
            "stop_loss": 98.1,
            "take_profit": 104.1,
            "shares": 10,
            "extra": {
                "day_change_pct": "2.34%",
                "atr": 0.8472,
                "atr_pct": 0.57,
                "sl_atr_ratio": 1.61,
            },
        }

        notification_card = build_signal_notification_card(record, console_base_url="https://console.example.com")
        status_card = build_signal_status_card(record, message="信号已确认，等待执行", console_base_url="https://console.example.com")

        for card in (notification_card, status_card):
            content = card["elements"][0]["content"]
            self.assertIn("**当前涨幅**: +2.34%", content)
            self.assertIn("**ATR值 / ATR波动率**: 0.85 / 0.57% (低波动)", content)
            self.assertIn("**止损ATR倍数**: 1.61x", content)

    def test_signal_cards_include_expected_profit_and_loss(self):
        cases = [
            (
                {
                    "id": "sig-row-long",
                    "signal_id": "sig-pl-long",
                    "symbol": "AAPL",
                    "direction": "long",
                    "environment": "live",
                    "status": "pending",
                    "entry": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 104.0,
                    "shares": 10,
                },
                "**预计盈利 / 预计亏损**: +$40.00 / -$20.00",
            ),
            (
                {
                    "id": "sig-row-short",
                    "signal_id": "sig-pl-short",
                    "symbol": "PLTR",
                    "direction": "short",
                    "environment": "paper",
                    "status": "pending",
                    "limit_price": 135.13,
                    "entry": 134.81,
                    "stop_loss": 137.13,
                    "take_profit": 127.13,
                    "shares": 75,
                },
                "**预计盈利 / 预计亏损**: +$600.00 / -$150.00",
            ),
        ]

        for record, expected_line in cases:
            notification_card = build_signal_notification_card(record, console_base_url="https://console.example.com")
            status_card = build_signal_status_card(record, message="信号已确认，等待执行", console_base_url="https://console.example.com")
            for card in (notification_card, status_card):
                self.assertIn(expected_line, card["elements"][0]["content"])

    def test_signal_cards_skip_expected_profit_and_loss_when_plan_incomplete(self):
        record = {
            "id": "sig-row-missing",
            "signal_id": "sig-pl-missing",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "pending",
            "entry": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "shares": 0,
        }

        notification_card = build_signal_notification_card(record, console_base_url="https://console.example.com")
        status_card = build_signal_status_card(record, message="信号已确认，等待执行", console_base_url="https://console.example.com")

        for card in (notification_card, status_card):
            self.assertNotIn("预计盈利 / 预计亏损", card["elements"][0]["content"])

    def test_signal_status_card_includes_lifecycle_button(self):
        card = build_signal_status_card(
            {
                "id": "sig-row-1",
                "signal_id": "sig-life",
                "symbol": "AAPL",
                "direction": "long",
                "environment": "live",
                "status": "submitted",
                "us_time": "2026-05-15 09:35:00",
                "extra": {"trade_group_id": "tg-1"},
            },
            message="订单已提交",
            console_base_url="https://console.example.com",
        )
        actions = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]
        urls = [action.get("multi_url", {}).get("url", "") for action in actions]

        self.assertTrue(any("/ibkr_signals.html" in url and "signal_id=sig-life" in url for url in urls))
        self.assertTrue(
            any(
                "/ibkr_lifecycle_flow.html" in url
                and "signal_id=sig-life" in url
                and "trade_group_id=tg-1" in url
                and "date=2026-05-15" in url
                for url in urls
            )
        )

    def test_order_status_card_includes_lifecycle_button(self):
        card = build_order_status_card(
            {
                "id": "order-row-1",
                "unique_id": "entry-1",
                "order_id": "12345",
                "symbol": "AAPL",
                "environment": "paper",
                "signal_id": "sig-order",
                "trade_group_id": "tg-order",
                "status": "Submitted",
                "role": "entry",
                "order_type": "LMT",
                "quantity": 20,
                "filled_qty": 5,
                "us_time": "2026-05-15 09:40:00",
            },
            message="部分成交，等待保护单同步",
            console_base_url="https://console.example.com",
        )
        actions = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]
        urls = [action.get("multi_url", {}).get("url", "") for action in actions]

        self.assertTrue(any("/orders.html" in url and "order_id=12345" in url for url in urls))
        self.assertTrue(
            any(
                "/ibkr_lifecycle_flow.html" in url
                and "broker_mode=paper" in url
                and "signal_id=sig-order" in url
                and "trade_group_id=tg-order" in url
                and "order_id=12345" in url
                for url in urls
            )
        )

    def test_order_group_status_card_includes_realized_loss_for_closed_long(self):
        rows = [
            {
                "id": "order-entry",
                "unique_id": "ONON_20260526_1020_mr_L",
                "order_type": "Entry",
                "symbol": "ONON",
                "environment": "paper",
                "status": "Closed",
                "role": "entry",
                "order_id": "76",
                "broker_order_id": "76",
                "trade_group_id": "ONON_long_20260526_102612_harvest",
                "entry_order_unique_id": "ONON_long_20260526_102612_harvest",
                "signal_id": "ONON_20260526_1020_mr_L",
                "direction": "long",
                "quantity": 251,
                "filled_qty": 251,
                "fill_price": 39.91,
                "us_time": "2026-05-26 10:29:12",
            },
            {
                "id": "order-close",
                "unique_id": "close_ONON_long_20260526_102612_harvest",
                "order_type": "MKT",
                "symbol": "ONON",
                "environment": "paper",
                "status": "Filled",
                "role": "close",
                "order_id": "79",
                "broker_order_id": "79",
                "trade_group_id": "ONON_long_20260526_102612_harvest",
                "entry_order_unique_id": "ONON_long_20260526_102612_harvest",
                "signal_id": "ONON_20260526_1020_mr_L",
                "direction": "sell",
                "quantity": 251,
                "filled_qty": 251,
                "fill_price": 39.88,
                "us_time": "2026-05-26 10:29:12",
            },
            {
                "id": "order-tp",
                "unique_id": "tp_ONON_long_20260526_102612_harvest",
                "order_type": "TakeProfit",
                "symbol": "ONON",
                "environment": "paper",
                "status": "Canceled",
                "role": "take_profit",
                "order_id": "77",
                "broker_order_id": "77",
                "trade_group_id": "ONON_long_20260526_102612_harvest",
                "entry_order_unique_id": "ONON_long_20260526_102612_harvest",
                "signal_id": "ONON_20260526_1020_mr_L",
                "quantity": 251,
                "filled_qty": 0,
                "limit_price": 40.64,
            },
            {
                "id": "order-sl",
                "unique_id": "sl_ONON_long_20260526_102612_harvest",
                "order_type": "StopLoss",
                "symbol": "ONON",
                "environment": "paper",
                "status": "Canceled",
                "role": "stop_loss",
                "order_id": "78",
                "broker_order_id": "78",
                "trade_group_id": "ONON_long_20260526_102612_harvest",
                "entry_order_unique_id": "ONON_long_20260526_102612_harvest",
                "signal_id": "ONON_20260526_1020_mr_L",
                "quantity": 251,
                "filled_qty": 0,
                "limit_price": 39.36,
            },
        ]

        card = build_order_group_status_card(rows, status="Closed", message="已成交 -> 已平仓", console_base_url="https://console.example.com")
        content = card["elements"][0]["content"]
        title = card["header"]["title"]["content"]

        self.assertIn("亏损 -$7.53", title)
        self.assertEqual("red", card["header"]["template"])
        self.assertIn("**实际盈亏**: 亏损 -$7.53", content)
        self.assertIn("平仓 @39.88", content)
        self.assertIn("251股", content)
        self.assertIn("未计手续费", content)
        self.assertNotIn("保护状态", content)

    def test_order_group_status_card_computes_short_net_profit_with_commission(self):
        rows = [
            {
                "id": "order-entry",
                "unique_id": "short-entry",
                "order_type": "Entry",
                "symbol": "TSLA",
                "environment": "paper",
                "status": "Closed",
                "role": "entry",
                "order_id": "201",
                "broker_order_id": "201",
                "trade_group_id": "short-entry",
                "entry_order_unique_id": "short-entry",
                "signal_id": "sig-short",
                "direction": "short",
                "quantity": 4,
                "filled_qty": 4,
                "fill_price": 50.0,
                "commission": 0.5,
            },
            {
                "id": "order-close",
                "unique_id": "short-close",
                "order_type": "MKT",
                "symbol": "TSLA",
                "environment": "paper",
                "status": "Filled",
                "role": "close",
                "order_id": "202",
                "broker_order_id": "202",
                "trade_group_id": "short-entry",
                "entry_order_unique_id": "short-entry",
                "signal_id": "sig-short",
                "direction": "buy",
                "quantity": 4,
                "filled_qty": 4,
                "fill_price": 45.0,
                "commission": 0.5,
            },
        ]

        card = build_order_group_status_card(rows, status="Closed", message="平仓完成", console_base_url="https://console.example.com")
        content = card["elements"][0]["content"]

        self.assertIn("盈利 +$19.00", card["header"]["title"]["content"])
        self.assertEqual("green", card["header"]["template"])
        self.assertIn("**实际盈亏**: 盈利 +$19.00", content)
        self.assertIn("含手续费 $1.00", content)

    def test_order_group_status_card_flags_filled_entry_with_canceled_protection(self):
        rows = [
            {
                "id": "order-entry",
                "unique_id": "entry_NFLX_short_20260527_105036_harvest",
                "order_type": "LMT",
                "symbol": "NFLX",
                "environment": "paper",
                "status": "Filled",
                "role": "entry",
                "order_id": "84",
                "broker_order_id": "84",
                "trade_group_id": "NFLX_short_20260527_105036_harvest",
                "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                "signal_id": "NFLX_20260527_1045_mr_U",
                "direction": "short",
                "quantity": 114,
                "filled_qty": 114,
                "fill_price": 88.01877,
                "extra": {"reason": "order_submitted_by_ibkr_compute"},
            },
            {
                "id": "order-tp",
                "unique_id": "tp_NFLX_short_20260527_105036_harvest",
                "order_type": "LMT",
                "symbol": "NFLX",
                "environment": "paper",
                "status": "Canceled",
                "role": "take_profit",
                "order_id": "85",
                "broker_order_id": "85",
                "trade_group_id": "NFLX_short_20260527_105036_harvest",
                "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                "quantity": 114,
                "filled_qty": 0,
                "limit_price": 86.69,
                "extra": {
                    "broker_last_error": {"code": 201, "message": "Order rejected - reason:Invalid Price"},
                    "status_updated_bar_time_ms": 200,
                },
            },
            {
                "id": "order-sl",
                "unique_id": "sl_NFLX_short_20260527_105036_harvest",
                "order_type": "STP",
                "symbol": "NFLX",
                "environment": "paper",
                "status": "Canceled",
                "role": "stop_loss",
                "order_id": "86",
                "broker_order_id": "86",
                "trade_group_id": "NFLX_short_20260527_105036_harvest",
                "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                "quantity": 114,
                "filled_qty": 0,
                "limit_price": 88.90,
                "extra": {"reason": "Order Canceled", "status_updated_bar_time_ms": 100},
            },
        ]

        card = build_order_group_status_card(rows, status="Filled", message="已成交", console_base_url="https://console.example.com")
        content = card["elements"][0]["content"]

        self.assertIn("保护单不完整", card["header"]["title"]["content"])
        self.assertEqual("orange", card["header"]["template"])
        self.assertIn("**保护状态**: 异常 - 缺少有效止盈/止损", content)
        self.assertIn("止盈已取消", content)
        self.assertIn("止损已取消", content)
        self.assertIn("Invalid Price", content)
        self.assertNotIn("order_submitted_by_ibkr_compute", content)

    def test_order_group_status_card_explains_order_flow_exit_and_ignores_default_zero_pnl(self):
        rows = [
            {
                "id": "order-entry",
                "unique_id": "entry_BABA_long_20260526_112518_harvest",
                "order_type": "LMT",
                "symbol": "BABA",
                "environment": "paper",
                "status": "Closed",
                "role": "entry",
                "order_id": "80",
                "broker_order_id": "80",
                "trade_group_id": "BABA_long_20260526_112518_harvest",
                "entry_order_unique_id": "entry_BABA_long_20260526_112518_harvest",
                "signal_id": "BABA_20260526_1120_mr_L",
                "direction": "long",
                "quantity": 78,
                "filled_qty": 78,
                "limit_price": 129.44,
                "fill_price": 129.34,
                "pnl": 0,
                "commission": 0,
                "us_time": "2026-05-26 11:29:35",
                "extra": {
                    "last_status_reason": "order_flow_adverse_delta_exit",
                    "order_flow_full_exit": {
                        "closed_quantity": 78,
                        "decision": {
                            "action": "full_exit",
                            "reason": "order_flow_adverse_delta_exit",
                            "direction": "long",
                            "pnl_r": 0.0326,
                            "limit_price": 129.27,
                            "confirmation": {
                                "interval_sec": 60,
                                "delta": -4144,
                                "cvd": -213777,
                                "delta_ratio": -0.723211,
                                "min_delta_ratio": 0.12,
                            },
                        },
                        "market_close_result": {
                            "limit_price": 129.27,
                            "fill": {
                                "filled_quantity": 78,
                                "order": {"avgFillPrice": 129.35},
                            },
                        },
                    },
                },
            },
            {
                "id": "order-close",
                "unique_id": "close_BABA_20260526_112934",
                "order_type": "LMT",
                "symbol": "BABA",
                "environment": "paper",
                "status": "Filled",
                "role": "close",
                "order_id": "83",
                "broker_order_id": "83",
                "trade_group_id": "BABA_long_20260526_112518_harvest",
                "entry_order_unique_id": "entry_BABA_long_20260526_112518_harvest",
                "direction": "sell",
                "quantity": 78,
                "filled_qty": 78,
                "limit_price": 129.27,
                "fill_price": 129.35,
                "pnl": 0,
                "commission": 0,
            },
            {
                "id": "order-tp",
                "unique_id": "tp_BABA_long_20260526_112518_harvest",
                "order_type": "LMT",
                "symbol": "BABA",
                "environment": "paper",
                "status": "Canceled",
                "role": "take_profit",
                "order_id": "81",
                "broker_order_id": "81",
                "trade_group_id": "BABA_long_20260526_112518_harvest",
                "entry_order_unique_id": "entry_BABA_long_20260526_112518_harvest",
                "quantity": 78,
                "filled_qty": 0,
                "limit_price": 130.72,
                "pnl": 0,
                "commission": 0,
            },
            {
                "id": "order-sl",
                "unique_id": "sl_BABA_long_20260526_112518_harvest",
                "order_type": "STP",
                "symbol": "BABA",
                "environment": "paper",
                "status": "Canceled",
                "role": "stop_loss",
                "order_id": "82",
                "broker_order_id": "82",
                "trade_group_id": "BABA_long_20260526_112518_harvest",
                "entry_order_unique_id": "entry_BABA_long_20260526_112518_harvest",
                "quantity": 78,
                "filled_qty": 0,
                "limit_price": 128.42,
                "pnl": 0,
                "commission": 0,
            },
        ]

        card = build_order_group_status_card(rows, status="Closed", message="已成交 -> 已平仓", console_base_url="https://console.example.com")
        content = card["elements"][0]["content"]

        self.assertIn("盈利 +$0.78", card["header"]["title"]["content"])
        self.assertNotIn("持平 $0.00", card["header"]["title"]["content"])
        self.assertIn("**原因**: 订单流反向 Delta 过强，且利润未达到保护阈值，触发提前平仓（order_flow_adverse_delta_exit）", content)
        self.assertIn("delta_ratio -0.723211", content)
        self.assertIn("平仓阈值 0.18", content)
        self.assertIn("pnl_r 0.0326 ≤ 0.15", content)
        self.assertIn("**处理**: 已取消止盈/止损保护单，用平仓单卖出 78 股，限价 129.27，均价 129.35", content)
        self.assertIn("**实际盈亏**: 盈利 +$0.78", content)
        self.assertIn("未计手续费", content)

    def test_order_group_status_card_keeps_unknown_reason_code_readable(self):
        rows = [
            {
                "id": "order-entry",
                "unique_id": "entry-custom",
                "order_type": "LMT",
                "symbol": "AAPL",
                "environment": "paper",
                "status": "Closed",
                "role": "entry",
                "trade_group_id": "entry-custom",
                "direction": "long",
                "quantity": 2,
                "filled_qty": 2,
                "fill_price": 100.0,
                "extra": {"last_status_reason": "custom_exit_rule"},
            },
            {
                "id": "order-close",
                "unique_id": "close-custom",
                "order_type": "MKT",
                "symbol": "AAPL",
                "environment": "paper",
                "status": "Filled",
                "role": "close",
                "trade_group_id": "entry-custom",
                "direction": "sell",
                "quantity": 2,
                "filled_qty": 2,
                "fill_price": 101.0,
            },
        ]

        card = build_order_group_status_card(rows, status="Closed", message="平仓完成", console_base_url="https://console.example.com")

        self.assertIn("**原因**: 系统记录原因 custom_exit_rule", card["elements"][0]["content"])

    def test_order_cards_skip_or_use_only_available_pnl_data(self):
        entry_only_card = build_order_group_status_card(
            [
                {
                    "id": "order-entry",
                    "unique_id": "sig-entry",
                    "order_type": "Entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Filled",
                    "role": "entry",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_price": 100.0,
                }
            ],
            status="Filled",
            message="入场成交",
            console_base_url="https://console.example.com",
        )
        self.assertNotIn("实际盈亏", entry_only_card["elements"][0]["content"])

        single_order_card = build_order_status_card(
            {
                "id": "order-close",
                "unique_id": "sig-close",
                "order_type": "MKT",
                "symbol": "ONON",
                "environment": "paper",
                "status": "Filled",
                "role": "close",
                "quantity": 251,
                "filled_qty": 251,
                "fill_price": 39.88,
                "realized_net_pnl": -7.53,
            },
            message="平仓成交",
            console_base_url="https://console.example.com",
        )
        self.assertIn("**实际盈亏**: 亏损 -$7.53", single_order_card["elements"][0]["content"])

    def test_signal_status_card_includes_buying_power_guard(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-bp",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "submitted",
            "entry": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "shares": 10,
            "extra": {
                "buying_power_guard": {
                    "state": "warning",
                    "reason": "buying_power_below_warning_threshold",
                    "remaining": 30000,
                    "remaining_pct_net_liq": 30,
                    "requested_exposure": 10000,
                    "remaining_after": 20000,
                    "remaining_after_pct_net_liq": 20,
                }
            },
        }

        status_card = build_signal_status_card(record, message="订单已提交", console_base_url="https://console.example.com")
        content = status_card["elements"][0]["content"]

        self.assertIn("**当前剩余购买力**: $30,000.00 (30.0% NetLiq)", content)
        self.assertIn("**本次预估占用 / 下单后**: $10,000.00 / $20,000.00 (20.0% NetLiq)", content)
        self.assertIn("**购买力状态**: WARNING", content)

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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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
                "broker_mode": "live",
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

    def test_signal_status_card_uses_effective_broker_expired_and_filters_internal_note(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-paper-expired",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "pending",
            "note": "paper:history_repair_pending",
            "extra": {
                "broker_mode": "paper",
                "data_environment": "live",
                "execution_by_mode": {
                    "paper": {
                        "status": "expired",
                        "note": "signal_expired",
                        "status_reason": "signal_expired",
                    }
                },
            },
        }

        card = build_signal_status_card(
            record,
            message="信号超时自动失效",
            console_base_url="https://console.example.com",
        )
        content = card["elements"][0]["content"]

        self.assertIn("已过期", card["header"]["title"]["content"])
        self.assertIn("**状态**: 已过期", content)
        self.assertIn("**原因**: signal_expired", content)
        self.assertNotIn("paper:history_repair_pending", content)

    def test_signal_status_card_prefers_actual_ack_broker_over_stale_extra_mode(self):
        record = {
            "id": "sig-row-1",
            "signal_id": "sig-paper-submitted",
            "symbol": "AAPL",
            "direction": "long",
            "environment": "live",
            "status": "pending",
            "extra": {
                "broker_mode": "live",
                "data_environment": "live",
                "last_ack_broker_mode": "paper",
                "last_ack_data_environment": "live",
                "execution_by_mode": {
                    "paper": {
                        "status": "submitted",
                        "note": "broker_ack",
                        "data_environment": "live",
                    }
                },
            },
        }

        card = build_signal_status_card(
            record,
            message="订单已提交",
            console_base_url="https://console.example.com",
        )
        content = card["elements"][0]["content"]
        actions = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]

        self.assertIn("Broker PAPER", card["header"]["title"]["content"])
        self.assertIn("**Broker**: Broker PAPER", content)
        self.assertIn("**数据**: Shared Data", content)
        self.assertTrue(
            any("broker_mode=paper" in action.get("multi_url", {}).get("url", "") for action in actions)
        )

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
