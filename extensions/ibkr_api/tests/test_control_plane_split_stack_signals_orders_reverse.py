from control_plane_split_stack_helpers import *
from ibkr_api.orders.notifications import (
    build_order_callback_ledger_card,
    build_order_status_card,
    sync_order_callback_ledger_notification,
    sync_order_status_notification,
)
from ibkr_api.orders.upsert import build_order_record_payload


class _RequestArgs(dict):
    def to_dict(self, flat=True):
        return dict(self)


class ControlPlaneSplitStackSignalsOrdersReverseTest(unittest.TestCase):
    def test_signals_pending_route_reads_native_pb_records_and_enriches_indicator(self):
        signal_rows = [
            {
                "id": "sig-row-1",
                "signal_id": "sig-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "signal": "buy",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "limit_price": 180.1,
                "shares": 10,
                "rr": 2.0,
                "reason": "breakout",
                "exchange": "NASDAQ",
                "interval": "5m",
                "chart_tf": "5m",
                "date": "2026-04-22",
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "bar_time_ms": 1713797700000,
                "extra": {"note": "from-test"},
                "created": "2026-04-22 09:35:01",
            }
        ]
        indicator_row = {
            "id": "ind-1",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "interval": "5m",
            "script_tag": "main",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "bar_index": 321,
            "created": "2026-04-22 09:35:01",
            "updated": "2026-04-22 09:35:02",
            "extra": {"close": 181.25, "crsi": 72.1},
        }

        def fake_get_records(collection, **_kwargs):
            if collection == "ibkr_signals":
                return signal_rows
            if collection == "ibkr_cache_snapshots":
                return []
            return []

        with mock.patch.object(api_app_mod.request, "args", _RequestArgs({"environment": "live", "date": "2026-04-22"})):
            with mock.patch.object(api_app_mod.pb, "get_records", side_effect=fake_get_records) as records_mock:
                with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=indicator_row) as first_mock:
                    payload = api_app_mod.custom_ibkr_signals_pending()

        signal_record_calls = [call for call in records_mock.call_args_list if call.args and call.args[0] == "ibkr_signals"]
        self.assertEqual(1, len(signal_record_calls), records_mock.call_args_list)
        first_mock.assert_called_once()
        self.assertEqual(len(payload["ibkr_signals"]), 1)
        signal = payload["ibkr_signals"][0]
        self.assertEqual(signal["signal_id"], "sig-1")
        self.assertEqual(signal["latest_indicator"]["id"], "ind-1")
        self.assertEqual(signal["extra"]["latest_indicator_id"], "ind-1")
        self.assertEqual(signal["extra"]["close"], 181.25)
        self.assertEqual(signal["extra"]["crsi"], 72.1)

    def test_signal_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api", "action": "created"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "signal_id": "sig-1", "symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_proxy_custom_to_pb", side_effect=AssertionError("unexpected pb fallback")) as proxy_mock:
                with mock.patch.object(api_app_mod, "build_signal_ingest_response", return_value=(sentinel, 200)) as builder_mock:
                    payload = api_app_mod.custom_ibkr_signal()
        self.assertEqual(payload["action"], "created")
        proxy_mock.assert_not_called()
        builder_mock.assert_called_once()

    def test_signals_batch_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api", "created": 2}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "items": [{"signal_id": "sig-1", "symbol": "AAPL"}]}):
            with mock.patch.object(api_app_mod, "_proxy_custom_to_pb", side_effect=AssertionError("unexpected pb fallback")) as proxy_mock:
                with mock.patch.object(api_app_mod, "build_signals_ingest_response", return_value=(sentinel, 200)) as builder_mock:
                    payload = api_app_mod.custom_ibkr_signals()
        self.assertEqual(payload["created"], 2)
        proxy_mock.assert_not_called()
        builder_mock.assert_called_once()

    def test_orders_cancel_group_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_cancel_group_response", return_value=(sentinel, 200)) as builder_mock:
                with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                    payload = api_app_mod.custom_ibkr_orders_cancel_group()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_orders_close_group_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_close_group_response", return_value=(sentinel, 200)) as builder_mock:
                with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                    payload = api_app_mod.custom_ibkr_orders_close_group()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_reverse_calculate_route_uses_native_builder(self):
        sentinel = {"success": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"symbol": "AAPL", "direction": "long"}):
            with mock.patch.object(api_app_mod, "build_reverse_calculate_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_calculate()
        self.assertTrue(payload["success"])
        builder_mock.assert_called_once()

    def test_orders_upsert_route_writes_order_and_detail_natively(self):
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "extra": {"reason": "entry submitted"},
        }
        created = []
        send_calls = []

        def fake_create_record(collection, data):
            row = dict(data)
            row["id"] = f"{collection}-1"
            created.append((collection, row))
            return row

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=None):
                with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "orders-1"}):
                            with mock.patch.object(api_app_mod, "_order_chat_id", return_value="order-chat-test"):
                                with mock.patch.object(
                                    api_app_mod,
                                    "_feishu_send_interactive",
                                    side_effect=lambda card, chat_id, environment: send_calls.append(
                                        {"card": card, "chat_id": chat_id, "environment": environment}
                                    )
                                    or {"success": True, "message_id": "order-msg-1"},
                                ):
                                    payload = api_app_mod.custom_ibkr_orders_upsert()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["order"]["unique_id"], "sig-1_entry")
        self.assertEqual(payload["order"]["status"], "Submitted")
        self.assertEqual(payload["notification_mode"], "order_group_card")
        self.assertEqual(payload["notification"]["message_id"], "order-msg-1")
        self.assertEqual(send_calls[0]["chat_id"], "order-chat-test")
        self.assertEqual(created[0][0], "orders")
        self.assertEqual(created[1][0], "ibkr_order_details")
        self.assertEqual(created[1][1]["order_id"], "sig-1_entry")
        self.assertEqual(created[1][1]["extra"]["sequence"], 1)
        self.assertEqual(created[1][1]["extra"]["source"], "orders/upsert")

    def test_orders_reconcile_route_repairs_missing_detail_natively(self):
        request_payload = {
            "environment": "live",
            "trade_group_id": "sig-1_entry",
        }
        order_rows = [
            {
                "id": "order-1",
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "order_id": "",
                "broker_order_id": "",
                "symbol": "AAPL",
                "environment": "live",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
                "status": "Submitted",
                "filled_qty": 0,
                "fill_price": 0,
                "signal_id": "sig-1",
                "trade_group_id": "sig-1_entry",
                "entry_order_unique_id": "sig-1_entry",
                "parent_order_unique_id": "",
                "sibling_order_unique_id": "",
                "role": "entry",
                "relation_status": "active",
                "position_side": "long",
                "order_time": "2026-04-22 09:35:00",
                "fill_time": "",
                "bar_time_ms": 1713797700000,
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "extra": {"reason": "entry submitted"},
            }
        ]
        created = []

        def fake_get_records(collection, filter=None, sort=None, per_page=200, page=1):
            if collection == "orders":
                return order_rows
            if collection == "ibkr_order_details":
                return []
            return []

        def fake_create_record(collection, data):
            row = dict(data)
            row["id"] = f"{collection}-1"
            created.append((collection, row))
            return row

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_records", side_effect=fake_get_records):
                with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                    payload = api_app_mod.custom_ibkr_orders_reconcile()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["repaired"], 1)
        self.assertEqual(created[0][0], "ibkr_order_details")
        self.assertEqual(created[0][1]["order_id"], "sig-1_entry")
        self.assertEqual(created[0][1]["extra"]["source"], "orders/reconcile")
        self.assertEqual(created[0][1]["extra"]["repair_mode"], "missing_ibkr_order_details")

    def test_signals_ack_route_updates_signal_and_uses_native_order_upserts(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
        }
        request_payload = {
            "environment": "live",
            "signal_id": "sig-1",
            "status": "submitted",
            "note": "broker_ack",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "status": "Submitted",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
                "extra": {"protection_complete": True},
            },
        }
        upsert_result = (
            {
                "success": True,
                "order": {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "status": "Submitted",
                },
            },
            200,
        )
        call_order = []

        def fake_update_record(*args, **kwargs):
            call_order.append("signal_patch")
            return {"id": "sig-row-1"}

        def fake_order_upsert(*args, **kwargs):
            call_order.append("order_upsert")
            return upsert_result

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", side_effect=fake_update_record) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response", side_effect=fake_order_upsert) as req_mock:
                        payload = api_app_mod.custom_ibkr_signals_ack()

        self.assertEqual(call_order, ["order_upsert", "order_upsert", "order_upsert", "signal_patch"])
        update_mock.assert_called_once()
        self.assertEqual(update_mock.call_args.args[:2], ("ibkr_signals", "sig-row-1"))
        patch = update_mock.call_args.args[2]
        self.assertEqual(patch["status"], "submitted")
        self.assertEqual(patch["note"], "broker_ack")
        self.assertEqual(patch["extra"]["last_ack_status"], "submitted")
        self.assertEqual(patch["extra"]["last_ack_note"], "broker_ack")
        self.assertEqual(patch["extra"]["last_ack_source"], "ibkr-api")
        self.assertEqual(patch["extra"]["last_ack_broker_mode"], "live")
        self.assertEqual(patch["extra"]["last_ack_data_environment"], "live")
        self.assertTrue(patch["extra"]["protection_complete"])
        self.assertEqual(patch["extra"]["execution_by_mode"]["live"]["status"], "submitted")
        self.assertEqual(patch["extra"]["execution_by_mode"]["live"]["note"], "broker_ack")
        self.assertEqual(patch["extra"]["execution_by_mode"]["live"]["data_environment"], "live")
        self.assertEqual(req_mock.call_count, 3)
        self.assertTrue(all(call.kwargs.get("notify_order_status") for call in req_mock.call_args_list))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["signal_id"], "sig-1")
        self.assertEqual(payload["status"], "Submitted")
        self.assertEqual(payload["signal_status"], "submitted")
        self.assertEqual(payload["order_results"][0]["unique_id"], "sig-1_entry")

    def test_signals_ack_route_requires_signal_id(self):
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            payload, status_code = api_app_mod.custom_ibkr_signals_ack()
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"], "Missing signal_id")

    def test_signals_ack_route_defaults_to_submitted_not_executed(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "extra": {},
        }
        request_payload = {
            "environment": "live",
            "signal_id": "sig-1",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "status": "Submitted",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
            },
        }
        upsert_result = (
            {"success": True, "order": {"unique_id": "sig-1_entry", "status": "Submitted"}},
            200,
        )

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "sig-row-1"}) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response", return_value=upsert_result):
                        payload = api_app_mod.custom_ibkr_signals_ack()

        update_mock.assert_called_once()
        patch = update_mock.call_args.args[2]
        self.assertEqual(patch["status"], "submitted")
        self.assertEqual(patch["extra"]["last_ack_status"], "submitted")
        self.assertEqual(patch["extra"]["last_ack_broker_mode"], "live")
        self.assertEqual(patch["extra"]["execution_by_mode"]["live"]["status"], "submitted")
        self.assertTrue(payload["success"])

    def test_signals_ack_route_records_paper_execution_with_shared_live_data(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "environment": "live",
            "status": "pending",
            "extra": {"broker_mode": "live", "data_environment": "live"},
        }
        request_payload = {
            "broker_mode": "paper",
            "market_data_mode": "live",
            "data_environment": "live",
            "signal_id": "sig-1",
            "status": "submitted",
            "note": "broker_ack",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "status": "Submitted",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
            },
        }
        upsert_result = (
            {"success": True, "order": {"unique_id": "sig-1_entry", "status": "Submitted"}},
            200,
        )

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "sig-row-1"}) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response", return_value=upsert_result) as upsert_mock:
                        payload = api_app_mod.custom_ibkr_signals_ack()

        patch = update_mock.call_args.args[2]
        self.assertNotIn("status", patch)
        self.assertEqual(patch["extra"]["last_ack_broker_mode"], "paper")
        self.assertEqual(patch["extra"]["last_ack_data_environment"], "live")
        self.assertEqual(patch["extra"]["execution_by_mode"]["paper"]["status"], "submitted")
        self.assertEqual(patch["extra"]["execution_by_mode"]["paper"]["data_environment"], "live")
        self.assertEqual(upsert_mock.call_args.kwargs["payload"]["environment"], "paper")
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertTrue(payload["success"])

    def test_signals_ack_route_allows_forced_lifecycle_update_after_submitted_waiting_fill(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 100.15,
            "stop_loss": 98.4,
            "take_profit": 102.4,
            "environment": "live",
            "status": "submitted_waiting_fill",
            "extra": {
                "execution_by_mode": {
                    "live": {
                        "status": "submitted_waiting_fill",
                        "note": "submitted_waiting_fill",
                    }
                },
                "tv_direct_entry": True,
                "reference_entry": 100.0,
            },
        }
        request_payload = {
            "environment": "live",
            "signal_id": "sig-1",
            "status": "filled_position",
            "note": "entry_filled_final_protection_repriced",
            "order": {
                "executed_price": 100.06,
                "stop_loss": 98.46,
                "take_profit": 102.46,
                "extra": {
                    "signal_lifecycle_update": True,
                    "actual_fill_price": 100.06,
                    "final_stop_loss": 98.46,
                    "final_take_profit": 102.46,
                    "slippage_bps": 6.0,
                    "slippage_r": 0.0375,
                },
            },
        }

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "sig-row-1"}) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response") as upsert_mock:
                        payload = api_app_mod.custom_ibkr_signals_ack()

        upsert_mock.assert_not_called()
        update_mock.assert_called_once()
        patch = update_mock.call_args.args[2]
        self.assertEqual("filled_position", patch["status"])
        self.assertEqual("entry_filled_final_protection_repriced", patch["note"])
        self.assertEqual(100.06, patch["executed_price"])
        self.assertEqual(98.46, patch["stop_loss"])
        self.assertEqual(102.46, patch["take_profit"])
        self.assertEqual("filled_position", patch["extra"]["execution_by_mode"]["live"]["status"])
        self.assertEqual(100.06, patch["extra"]["actual_fill_price"])
        self.assertEqual(98.46, patch["extra"]["final_stop_loss"])
        self.assertEqual(6.0, patch["extra"]["slippage_bps"])
        self.assertTrue(payload["success"])
        self.assertEqual("filled_position", payload["signal_status"])

    def test_signals_ack_route_records_partial_when_order_upsert_fails(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "extra": {},
        }
        request_payload = {
            "environment": "live",
            "signal_id": "sig-1",
            "status": "protected_active",
            "note": "broker_ack",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "status": "Submitted",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
                "extra": {"protection_complete": True},
            },
        }
        upsert_results = [
            ({"success": True, "order": {"unique_id": "sig-1_entry", "status": "Submitted"}}, 200),
            ({"error": "pb write failed"}, 500),
        ]

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "sig-row-1"}) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response", side_effect=upsert_results) as req_mock:
                        payload, status_code = api_app_mod.custom_ibkr_signals_ack()

        self.assertEqual(status_code, 500)
        self.assertEqual(req_mock.call_count, 2)
        update_mock.assert_called_once()
        patch = update_mock.call_args.args[2]
        self.assertEqual(patch["status"], "protection_incomplete")
        self.assertEqual(patch["note"], "order_upsert_failed")
        self.assertTrue(patch["extra"]["ack_partial"])
        self.assertEqual(patch["extra"]["ack_partial_status"], "ack_partial")
        self.assertEqual(patch["extra"]["last_ack_broker_mode"], "live")
        self.assertEqual(patch["extra"]["execution_by_mode"]["live"]["status"], "protection_incomplete")
        self.assertTrue(patch["extra"]["order_upsert_failed"])
        self.assertEqual(patch["extra"]["order_upsert_error"], "pb write failed")
        self.assertEqual(payload["status"], "protection_incomplete")
        self.assertEqual(payload["ack_status"], "ack_partial")
        self.assertEqual(payload["signal_status"], "protection_incomplete")
        self.assertEqual(payload["diagnostic"], "order_upsert_failed")
        self.assertEqual(payload["order_results"][1]["ok"], False)

    def test_reverse_dispatch_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"reverse_id": "rev-1", "action": "execute"}):
            with mock.patch.object(api_app_mod, "build_reverse_dispatch_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_dispatch()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_reverse_list_route_uses_native_builder(self):
        sentinel = {"ibkr_signals": [{"id": "rev-1"}]}
        with mock.patch.object(api_app_mod.request, "args", _RequestArgs({"environment": "live", "date": "2026-04-22", "symbol": "AAPL", "status": "pending", "limit": "20"})):
            with mock.patch.object(api_app_mod, "build_reverse_list_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_list()
        self.assertEqual(payload["ibkr_signals"][0]["id"], "rev-1")
        builder_mock.assert_called_once()

    def test_reverse_pending_route_uses_native_builder(self):
        sentinel = {"ibkr_signals": [{"id": "rev-1"}]}
        with mock.patch.object(api_app_mod.request, "args", _RequestArgs({"environment": "live", "limit": "20"})):
            with mock.patch.object(api_app_mod, "build_reverse_pending_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_pending()
        self.assertEqual(payload["ibkr_signals"][0]["id"], "rev-1")
        builder_mock.assert_called_once()

    def test_reverse_ack_route_uses_native_builder(self):
        sentinel = {"success": True, "signal": {"id": "rev-1"}}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"reverse_id": "rev-1", "status": "confirmed"}):
            with mock.patch.object(api_app_mod, "build_reverse_ack_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_ack()
        self.assertTrue(payload["success"])
        builder_mock.assert_called_once()

    def test_build_signal_ack_orders_creates_entry_and_children(self):
        signal_row = {
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
        }
        payload = {
            "signal_id": "sig-1",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "direction": "long",
                "status": "Submitted",
                "limit_price": 180.1,
            },
        }
        orders = build_signal_ack_orders(signal_row, payload, "live")
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders[0]["role"], "entry")
        self.assertEqual(orders[1]["order_type"], "TakeProfit")
        self.assertEqual(orders[2]["order_type"], "StopLoss")
        self.assertEqual(orders[1]["parent_order_unique_id"], "sig-1_entry")
        self.assertEqual(orders[2]["sibling_order_unique_id"], "sig-1_take_profit")

    def test_build_order_upsert_response_idempotent_skips_detail_append(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "",
            "broker_order_id": "",
            "symbol": "AAPL",
            "environment": "live",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "sig-1",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "entry",
            "relation_status": "active",
            "position_side": "long",
            "order_time": "2026-04-22 09:35:00",
            "fill_time": "",
            "bar_time_ms": 1713797700000,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "extra": {
                "environment": "live",
                "order_id": "",
                "broker_order_id": "",
                "order_time": "2026-04-22 09:35:00",
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "bar_time_ms": 1713797700000,
                "trade_group_id": "sig-1_entry",
                "entry_order_unique_id": "sig-1_entry",
                "parent_order_unique_id": "",
                "sibling_order_unique_id": "",
                "role": "entry",
                "relation_status": "active",
                "position_side": "long",
                "previous_status": "",
                "current_status": "Submitted",
                "status_transition_text": "待成交",
                "status_updated_us_time": "2026-04-22 09:35:00",
                "status_updated_cn_time": "2026-04-22 21:35:00",
                "status_updated_bar_time_ms": 1713797700000,
                "last_status_source": "orders/upsert",
                "last_status_reason": "entry submitted",
                "created_us_time": "2026-04-22 09:35:00",
                "created_cn_time": "2026-04-22 21:35:00",
                "created_bar_time_ms": 1713797700000,
                "reason": "entry submitted",
            },
        }
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "extra": {"reason": "entry submitted"},
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=existing_row)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock()
        fake_pb.create_record = mock.Mock()
        notify_order_status = mock.Mock(return_value={"success": True, "message_id": "unused"})

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
            notify_order_status=notify_order_status,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["idempotent"])
        fake_pb.update_record.assert_not_called()
        fake_pb.create_record.assert_not_called()
        notify_order_status.assert_not_called()

    def test_build_order_upsert_response_recovers_unique_create_race(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "close_MSTR_20260609_015511",
            "order_type": "LMT",
            "order_id": "11291",
            "broker_order_id": "11291",
            "symbol": "MSTR",
            "environment": "paper",
            "direction": "short",
            "quantity": 39,
            "limit_price": 127.46,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "",
            "trade_group_id": "grp-mstr",
            "entry_order_unique_id": "entry-mstr",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "close",
            "relation_status": "active",
            "position_side": "short",
            "order_time": "2026-06-09 01:55:11",
            "fill_time": "",
            "bar_time_ms": 1780984511000,
            "us_time": "2026-06-09 01:55:11",
            "cn_time": "2026-06-09 13:55:11",
            "extra": {"environment": "paper", "role": "close"},
        }
        request_payload = {
            "environment": "paper",
            "unique_id": "close_MSTR_20260609_015511",
            "order_type": "LMT",
            "order_id": "11291",
            "broker_order_id": "11291",
            "symbol": "MSTR",
            "direction": "short",
            "quantity": 39,
            "limit_price": 127.46,
            "status": "Filled",
            "filled_qty": 39,
            "fill_price": 127.46,
            "trade_group_id": "grp-mstr",
            "entry_order_unique_id": "entry-mstr",
            "role": "close",
            "us_time": "2026-06-09 02:05:00",
            "cn_time": "2026-06-09 14:05:00",
            "bar_time_ms": 1780985100000,
        }

        def create_record(collection, data):
            if collection == "orders":
                raise RuntimeError(
                    "pb_request_failed:POST:/api/collections/orders/records:status=400:"
                    "body={\"data\":{\"environment\":{\"code\":\"validation_not_unique\"},"
                    "\"unique_id\":{\"code\":\"validation_not_unique\"}}}"
                )
            return {**data, "id": f"{collection}-1"}

        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(side_effect=[None, existing_row])
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock(side_effect=lambda collection, record_id, data: {**data, "id": record_id})
        fake_pb.create_record = mock.Mock(side_effect=create_record)

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual("Submitted", payload["previous_status"])
        self.assertEqual("Filled", payload["order"]["status"])
        self.assertEqual("order-1", payload["order"]["id"])
        fake_pb.update_record.assert_called_once()

    def test_build_order_upsert_response_treats_callback_heartbeat_as_idempotent(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "101",
            "broker_order_id": "101",
            "symbol": "AAPL",
            "environment": "live",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "sig-1",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "entry",
            "relation_status": "active",
            "position_side": "long",
            "order_time": "2026-04-22 09:35:00",
            "fill_time": "",
            "extra": {
                "environment": "live",
                "order_id": "101",
                "broker_order_id": "101",
                "order_time": "2026-04-22 09:35:00",
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "bar_time_ms": 1713797700000,
                "trade_group_id": "sig-1_entry",
                "entry_order_unique_id": "sig-1_entry",
                "parent_order_unique_id": "",
                "sibling_order_unique_id": "",
                "role": "entry",
                "relation_status": "active",
                "position_side": "long",
                "current_status": "Submitted",
                "ib_callback_type": "openOrder",
                "broker_realtime_callback": True,
                "broker_callback_received_at": "2026-04-22 09:35:00",
                "broker_callback_received_at_ms": 1713797700000,
            },
        }
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "101",
            "broker_order_id": "101",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:05",
            "cn_time": "2026-04-22 21:35:05",
            "bar_time_ms": 1713797705000,
            "extra": {
                "broker_realtime_callback": False,
                "ib_callback_type": "orderStatus",
                "broker_callback_received_at": "2026-04-22 09:35:05",
                "broker_callback_received_at_ms": 1713797705000,
            },
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=existing_row)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock()
        fake_pb.create_record = mock.Mock()
        notify_order_status = mock.Mock(return_value={"success": True})

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
            notify_order_status=notify_order_status,
            notify_order_callback_ledger=mock.Mock(return_value={"success": True}),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["idempotent"])
        fake_pb.update_record.assert_not_called()
        fake_pb.create_record.assert_not_called()
        notify_order_status.assert_not_called()

    def test_order_record_payload_preserves_existing_fill_time_on_filled_heartbeat(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "zts-entry",
            "order_type": "Entry",
            "order_id": "95",
            "broker_order_id": "95",
            "symbol": "ZTS",
            "environment": "paper",
            "direction": "short",
            "quantity": 64,
            "limit_price": 77.21,
            "status": "Filled",
            "filled_qty": 64,
            "fill_price": 77.21,
            "signal_id": "sig-zts",
            "trade_group_id": "zts-group",
            "entry_order_unique_id": "zts-entry",
            "role": "entry",
            "relation_status": "closed",
            "position_side": "short",
            "order_time": "2026-06-01 11:08:00",
            "fill_time": "",
            "bar_time_ms": 1780326662000,
            "us_time": "2026-06-01 11:11:02",
            "cn_time": "2026-06-01 23:11:02",
            "extra": {
                "filled_us_time": "2026-06-01 11:11:02",
                "filled_cn_time": "2026-06-01 23:11:02",
                "filled_bar_time_ms": 1780326662000,
            },
        }
        request_payload = {
            "environment": "paper",
            "unique_id": "zts-entry",
            "order_type": "Entry",
            "order_id": "95",
            "broker_order_id": "95",
            "symbol": "ZTS",
            "direction": "short",
            "quantity": 64,
            "limit_price": 77.21,
            "status": "Filled",
            "filled_qty": 64,
            "fill_price": 77.21,
            "signal_id": "sig-zts",
            "trade_group_id": "zts-group",
            "entry_order_unique_id": "zts-entry",
            "role": "entry",
            "us_time": "2026-06-02 00:24:53",
            "cn_time": "2026-06-02 12:24:53",
            "bar_time_ms": 1780374293000,
            "extra": {"broker_update_source": "poll"},
        }

        record = build_order_record_payload(request_payload, existing_row, "paper")

        self.assertEqual("2026-06-01 11:11:02", record["us_time"])
        self.assertEqual("2026-06-01 23:11:02", record["cn_time"])
        self.assertEqual(1780326662000, record["bar_time_ms"])
        self.assertEqual("2026-06-01 11:11:02", record["fill_time"])
        self.assertEqual("2026-06-01 11:11:02", record["extra"]["filled_us_time"])
        self.assertEqual(1780326662000, record["extra"]["filled_bar_time_ms"])

    def test_order_record_payload_recovers_entry_limit_from_submitted_extra_when_broker_price_zero(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "entry_BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "order_type": "LMT",
            "order_id": "207",
            "broker_order_id": "207",
            "symbol": "RVTY",
            "environment": "paper",
            "direction": "short",
            "quantity": 50,
            "limit_price": 0,
            "status": "Filled",
            "filled_qty": 50,
            "fill_price": 99.79,
            "signal_id": "BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "trade_group_id": "BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "entry_order_unique_id": "entry_BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "role": "entry",
            "relation_status": "active",
            "position_side": "short",
            "bar_time_ms": 1780667302000,
            "us_time": "2026-06-05 09:48:22",
            "cn_time": "2026-06-05 21:48:22",
            "extra": {
                "submitted_entry_limit_price": 99.64,
                "submitted_limit_cap_price": 99.64,
                "original_entry": 99.79,
                "reference_entry": 99.79,
            },
        }
        request_payload = {
            "environment": "paper",
            "unique_id": "entry_BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "order_type": "LMT",
            "order_id": "207",
            "broker_order_id": "207",
            "symbol": "RVTY",
            "direction": "short",
            "quantity": 50,
            "limit_price": 0,
            "status": "Filled",
            "filled_qty": 50,
            "fill_price": 99.79,
            "signal_id": "BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "trade_group_id": "BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "entry_order_unique_id": "entry_BATS_RVTY_short_20260605_0946_2_mr_sdUpper",
            "role": "entry",
            "us_time": "2026-06-05 12:33:58",
            "cn_time": "2026-06-06 00:33:58",
            "bar_time_ms": 1780677238265,
            "extra": {"broker_update_source": "request_snapshot"},
        }

        record = build_order_record_payload(request_payload, existing_row, "paper")

        self.assertEqual(99.64, record["limit_price"])
        self.assertEqual(99.79, record["fill_price"])

    def test_order_record_payload_accepts_explicit_fill_time_correction(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "zts-entry",
            "order_type": "Entry",
            "order_id": "95",
            "broker_order_id": "95",
            "symbol": "ZTS",
            "environment": "paper",
            "direction": "short",
            "quantity": 64,
            "limit_price": 77.21,
            "status": "Filled",
            "filled_qty": 64,
            "fill_price": 77.21,
            "signal_id": "sig-zts",
            "trade_group_id": "zts-group",
            "entry_order_unique_id": "zts-entry",
            "role": "entry",
            "relation_status": "closed",
            "position_side": "short",
            "bar_time_ms": 1780374293000,
            "us_time": "2026-06-02 00:24:53",
            "cn_time": "2026-06-02 12:24:53",
            "extra": {
                "filled_us_time": "2026-06-02 00:24:53",
                "filled_cn_time": "2026-06-02 12:24:53",
                "filled_bar_time_ms": 1780374293000,
            },
        }
        request_payload = {
            "environment": "paper",
            "unique_id": "zts-entry",
            "order_type": "Entry",
            "symbol": "ZTS",
            "status": "Filled",
            "fill_us_time": "2026-06-01 11:11:02",
            "fill_cn_time": "2026-06-01 23:11:02",
            "fill_bar_time_ms": 1780326662000,
        }

        record = build_order_record_payload(request_payload, existing_row, "paper")

        self.assertEqual("2026-06-01 11:11:02", record["us_time"])
        self.assertEqual("2026-06-01 23:11:02", record["cn_time"])
        self.assertEqual(1780326662000, record["bar_time_ms"])
        self.assertEqual("2026-06-01 11:11:02", record["fill_time"])
        self.assertEqual("2026-06-01 11:11:02", record["extra"]["filled_us_time"])

    def test_build_order_upsert_response_notifies_on_non_idempotent_submit(self):
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "extra": {"reason": "entry submitted"},
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=None)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.create_record = mock.Mock(side_effect=lambda collection, data: {**data, "id": f"{collection}-1"})
        fake_pb.update_record = mock.Mock()
        notify_calls = []
        ledger_calls = []

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
            notify_order_status=lambda status, order_row, options: notify_calls.append((status, order_row, options))
            or {"success": True, "message_id": "order-msg-1"},
            notify_order_callback_ledger=lambda status, order_row, options: ledger_calls.append((status, order_row, options))
            or {"success": True, "message_id": "ledger-msg-unexpected"},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["idempotent"])
        self.assertEqual(payload["notification_mode"], "order_group_card")
        self.assertEqual(payload["notification"]["message_id"], "order-msg-1")
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(notify_calls[0][0], "Submitted")
        self.assertEqual(notify_calls[0][1]["id"], "orders-1")
        self.assertEqual(notify_calls[0][2]["message"], "订单已提交")
        self.assertEqual(ledger_calls, [])
        self.assertEqual(payload["trade_ledger_notification"], {})
        fake_pb.update_record.assert_not_called()

    def test_build_order_upsert_response_routes_realtime_callback_to_trade_ledger(self):
        existing_row = {
            "id": "order-entry",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "",
            "broker_order_id": "",
            "symbol": "AAPL",
            "environment": "live",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "sig-1",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "entry",
            "relation_status": "active",
            "position_side": "long",
            "extra": {"environment": "live", "current_status": "Submitted"},
        }
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "101",
            "broker_order_id": "101",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:01",
            "cn_time": "2026-04-22 21:35:01",
            "bar_time_ms": 1713797701000,
            "extra": {
                "broker_realtime_callback": True,
                "ib_callback_type": "openOrder",
                "broker_callback_received_at": "2026-04-22 09:35:01",
                "broker_callback_received_at_ms": 1713797701000,
            },
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=existing_row)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock(side_effect=lambda collection, record_id, data: {**data, "id": record_id})
        fake_pb.create_record = mock.Mock(side_effect=lambda collection, data: {**data, "id": f"{collection}-detail"})
        ledger_calls = []

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
            notify_order_callback_ledger=lambda status, order_row, options: ledger_calls.append((status, order_row, options))
            or {"success": True, "message_id": "ledger-msg-1"},
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["idempotent"])
        self.assertEqual(payload["trade_ledger_notification"]["message_id"], "ledger-msg-1")
        self.assertEqual(len(ledger_calls), 1)
        self.assertEqual(ledger_calls[0][0], "Submitted")
        self.assertEqual(ledger_calls[0][1]["broker_order_id"], "101")
        self.assertEqual(ledger_calls[0][2]["previous_order"]["id"], "order-entry")

    def test_sync_order_callback_ledger_notification_sends_fill_delta(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-entry",
                    "unique_id": "sig-1_entry",
                    "order_type": "Entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "broker_order_id": "101",
                    "order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 5,
                    "fill_price": 180.2,
                    "extra": {
                        "environment": "live",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "orderStatus",
                        "broker_callback_received_at": "2026-04-22 09:36:00",
                    },
                }
                self.updated = []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "filled_qty": 0, "extra": {"environment": "live"}}
        send_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-1"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(result["message_id"], "ledger-msg-1")
        self.assertEqual(result["event_type"], "fill")
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][1], "ledger-chat-test")
        card_text = send_calls[0][0]["elements"][0]["content"]
        self.assertIn("真实回调", send_calls[0][0]["header"]["title"]["content"])
        self.assertIn("成交增量", card_text)
        self.assertIn("101", card_text)
        self.assertNotIn("实际盈亏", card_text)
        self.assertEqual(pb.updated[0][2]["extra"]["feishu_trade_ledger_message_id"], "ledger-msg-1")
        self.assertIn(
            pb.updated[0][2]["extra"]["feishu_trade_ledger_notify_key"],
            pb.updated[0][2]["extra"]["feishu_trade_ledger_notified_keys"],
        )

    def test_sync_order_callback_ledger_defers_recent_close_fill_without_reason(self):
        class _LedgerPB:
            def __init__(self):
                self.updated = []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                return dict(patch)

        now_s = 1000.0
        order = {
            "id": "order-close",
            "unique_id": "close_SGI_20260629_160512",
            "order_type": "LMT",
            "symbol": "SGI",
            "environment": "paper",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "11577",
            "order_id": "11577",
            "trade_group_id": "close_SGI_20260629_160512",
            "entry_order_unique_id": "close_SGI_20260629_160512",
            "direction": "short",
            "quantity": 64,
            "filled_qty": 64,
            "fill_price": 80.10,
            "extra": {
                "environment": "paper",
                "broker_realtime_callback": True,
                "ib_callback_type": "execDetails",
                "ib_exec_id": "exec-sgi",
                "broker_callback_received_at_ms": int(now_s * 1000) - 5000,
            },
        }
        previous = {**order, "filled_qty": 0, "extra": {"environment": "paper"}}
        send_calls = []

        with mock.patch("ibkr_api.orders.notifications.time.time", return_value=now_s):
            result = sync_order_callback_ledger_notification(
                _LedgerPB(),
                order,
                previous_order=previous,
                send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
                or {"success": True, "message_id": "should-not-send"},
                trade_ledger_chat_id="ledger-chat-test",
            )

        self.assertTrue(result["skipped"])
        self.assertEqual("exit_reason_pending", result["reason"])
        self.assertEqual([], send_calls)

    def test_sync_order_callback_ledger_updates_missing_reason_card_when_eod_reason_arrives(self):
        class _LedgerPB:
            def __init__(self, order):
                self.order = dict(order)
                self.updated = []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        notify_key = "trade_ledger_callback_v2:paper:11577:exec-sgi:fill"
        order = {
            "id": "order-close",
            "unique_id": "close_SGI_20260629_160512",
            "order_type": "LMT",
            "symbol": "SGI",
            "environment": "paper",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "11577",
            "order_id": "11577",
            "trade_group_id": "BATS_SGI_long_20260629_0946_2_mr_sdLower",
            "entry_order_unique_id": "entry_BATS_SGI_long_20260629_0946_2_mr_sdLower",
            "signal_id": "BATS_SGI_long_20260629_0946_2_mr_sdLower",
            "direction": "short",
            "quantity": 64,
            "filled_qty": 64,
            "fill_price": 80.10,
            "extra": {
                "environment": "paper",
                "broker_realtime_callback": True,
                "ib_callback_type": "commissionReport",
                "ib_exec_id": "exec-sgi",
                "close_reason": "force_flat_eod",
                "reason": "force_flat_eod",
                "feishu_trade_ledger_last_result": "success",
                "feishu_trade_ledger_notify_key": notify_key,
                "feishu_trade_ledger_notified_keys": [notify_key],
                "feishu_trade_ledger_last_exec_id": "exec-sgi",
                "feishu_trade_ledger_message_id": "ledger-msg-old",
                "feishu_trade_ledger_exit_reason_missing": True,
            },
        }
        previous = {**order, "status": "Submitted", "filled_qty": 0, "extra": {"environment": "paper"}}
        pb = _LedgerPB(order)
        update_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: {"success": True, "message_id": "should-not-send"},
            update_interactive=lambda message_id, card, environment: update_calls.append((message_id, card, environment))
            or {"success": True, "message_id": message_id},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertEqual("exit_reason_corrected", result["reason"])
        self.assertTrue(result["updated"])
        self.assertEqual(1, len(update_calls))
        self.assertEqual("ledger-msg-old", update_calls[0][0])
        content = update_calls[0][1]["elements"][0]["content"]
        self.assertIn("**平仓原因**: EOD 平仓（force_flat_eod）", content)
        patch_extra = pb.updated[0][2]["extra"]
        self.assertFalse(patch_extra["feishu_trade_ledger_exit_reason_missing"])
        self.assertEqual("force_flat_eod", patch_extra["feishu_trade_ledger_exit_reason_code"])

    def test_sync_order_callback_ledger_skips_replayed_fill_with_same_exec_id(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-entry",
                    "unique_id": "entry_BATS_BE_long_20260610_0946_2_mr_sdLower",
                    "order_type": "LMT",
                    "symbol": "BE",
                    "environment": "paper",
                    "status": "Submitted",
                    "role": "entry",
                    "broker_order_id": "11304",
                    "order_id": "11304",
                    "trade_group_id": "BATS_BE_long_20260610_0946_2_mr_sdLower",
                    "entry_order_unique_id": "entry_BATS_BE_long_20260610_0946_2_mr_sdLower",
                    "signal_id": "BATS_BE_long_20260610_0946_2_mr_sdLower",
                    "direction": "long",
                    "quantity": 19,
                    "filled_qty": 19,
                    "fill_price": 258.23,
                    "last_fill_price": 258.23,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "ib_exec_id": "0000e0d5.6a29f7a8.01.01",
                        "broker_callback_received_at": "2026-06-10 09:48:30",
                    },
                }
                self.updated = []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "filled_qty": 0, "extra": {"environment": "paper"}}
        send_calls = []

        first = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-first"},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertEqual("ledger-msg-first", first["message_id"])
        self.assertEqual(1, len(send_calls))
        self.assertTrue(first["notify_key"].startswith("trade_ledger_callback_v2:paper:11304:0000e0d5.6a29f7a8.01.01:fill"))

        replayed_order = {
            **pb.order,
            "status": "Filled",
            "filled_qty": 19,
            "extra": {
                **pb.order["extra"],
                "broker_realtime_callback": True,
                "ib_callback_type": "commissionReport",
                "ib_exec_id": "0000e0d5.6a29f7a8.01.01",
                "broker_callback_received_at": "2026-06-10 09:51:56",
            },
        }
        stale_previous = {**replayed_order, "status": "Submitted", "filled_qty": 0, "extra": {"environment": "paper"}}
        second = sync_order_callback_ledger_notification(
            pb,
            replayed_order,
            previous_order=stale_previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-duplicate"},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertTrue(second["skipped"])
        self.assertEqual("already_notified", second["reason"])
        self.assertEqual(1, len(send_calls))

    def test_sync_order_callback_ledger_allows_different_exec_id_fills(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-entry",
                    "unique_id": "sig-1_entry",
                    "order_type": "LMT",
                    "symbol": "AAPL",
                    "environment": "paper",
                    "status": "Submitted",
                    "role": "entry",
                    "broker_order_id": "101",
                    "order_id": "101",
                    "trade_group_id": "sig-1",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 20,
                    "filled_qty": 10,
                    "fill_price": 180.0,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "ib_exec_id": "exec-1",
                    },
                }
                self.updated = []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        send_calls = []
        sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order={**pb.order, "filled_qty": 0, "extra": {"environment": "paper"}},
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-1"},
            trade_ledger_chat_id="ledger-chat-test",
        )

        second_fill = {
            **pb.order,
            "filled_qty": 20,
            "extra": {
                **pb.order["extra"],
                "broker_realtime_callback": True,
                "ib_callback_type": "execDetails",
                "ib_exec_id": "exec-2",
            },
        }
        result = sync_order_callback_ledger_notification(
            pb,
            second_fill,
            previous_order={**second_fill, "filled_qty": 10, "extra": {"environment": "paper"}},
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-2"},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertEqual("ledger-msg-2", result["message_id"])
        self.assertEqual(2, len(send_calls))

    def test_sync_order_callback_ledger_labels_first_exec_details_with_no_new_delta(self):
        class _LedgerPB:
            def __init__(self):
                self.entry = {
                    "id": "order-entry",
                    "unique_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper_entry",
                    "order_type": "Entry",
                    "symbol": "NXPI",
                    "environment": "paper",
                    "status": "Closed",
                    "role": "entry",
                    "broker_order_id": "11290",
                    "order_id": "11290",
                    "trade_group_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "entry_order_unique_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "signal_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "direction": "short",
                    "quantity": 16,
                    "filled_qty": 16,
                    "fill_price": 304.0,
                }
                self.order = {
                    "id": "order-tp",
                    "unique_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper_tp",
                    "order_type": "LMT",
                    "symbol": "NXPI",
                    "environment": "paper",
                    "status": "Submitted",
                    "role": "take_profit",
                    "broker_order_id": "11298",
                    "order_id": "11298",
                    "trade_group_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "entry_order_unique_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "signal_id": "BATS_NXPI_short_20260609_0946_2_mr_sdUpper",
                    "direction": "short",
                    "quantity": 16,
                    "filled_qty": 16,
                    "fill_price": 302.05,
                    "last_fill_price": 302.05,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "exec_id": "00025b45.6a2c6f4c.01.01",
                        "broker_callback_received_at": "2026-06-09 10:31:48",
                    },
                }
                self.updated = []

            def get_records(self, collection, **kwargs):
                return [self.entry, self.order] if collection == "orders" else []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "extra": {"environment": "paper"}}
        send_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-first-exec"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertEqual("fill", result["event_type"])
        self.assertEqual("first_exec_details_callback", result["reason"])
        self.assertEqual(1, len(send_calls))
        card = send_calls[0][0]
        content = card["elements"][0]["content"]
        self.assertIn("**回调判定**: 成交明细首次入流水", content)
        self.assertIn("**判定依据**: 上一条记录无 broker 实时回调标记；当前是 IB execDetails 成交明细，已成交 16/16，成交增量 0。", content)
        self.assertIn("**状态**: 已成交", content)
        self.assertIn("**成交数量**: 16", content)
        self.assertIn("盈利 +$31.20", card["header"]["title"]["content"])
        self.assertNotIn("首次真实回调", content)

    def test_sync_order_callback_ledger_marks_incomplete_close_cancel_red(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-close",
                    "unique_id": "close_AAPL_20260609_100000",
                    "order_type": "LMT",
                    "symbol": "AAPL",
                    "environment": "paper",
                    "status": "Canceled",
                    "role": "close",
                    "broker_order_id": "301",
                    "order_id": "301",
                    "direction": "sell",
                    "quantity": 10,
                    "filled_qty": 4,
                    "fill_price": 180.0,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "orderStatus",
                        "broker_callback_received_at": "2026-06-09 10:01:00",
                    },
                }
                self.updated = []

            def get_records(self, collection, **kwargs):
                return []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "status": "Submitted", "extra": {"environment": "paper"}}
        send_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-close-cancel"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertEqual("terminal_status", result["event_type"])
        self.assertEqual("close_order_cancelled_incomplete", result["reason"])
        self.assertEqual(1, len(send_calls))
        card = send_calls[0][0]
        self.assertEqual("red", card["header"]["template"])
        self.assertIn("平仓未完成", card["header"]["title"]["content"])
        self.assertIn("平仓单已取消", card["header"]["title"]["content"])
        self.assertIn("剩余 6", card["elements"][0]["content"])
        self.assertEqual("close_order_cancelled_incomplete", pb.updated[0][2]["extra"]["feishu_trade_ledger_last_reason"])

    def test_order_callback_ledger_card_hides_ib_unset_double_prices(self):
        card = build_order_callback_ledger_card(
            {
                "symbol": "SPOT",
                "environment": "paper",
                "status": "Canceled",
                "role": "take_profit",
                "order_type": "LMT",
                "position_side": "long",
                "quantity": 20,
                "filled_qty": 0,
                "broker_order_id": "4000",
                "avgFillPrice": "1.7976931348623157e+308",
                "avg_price": "1.7976931348623157e+308",
                "extra": {"ib_callback_type": "orderStatus", "lastFillPrice": "1.7976931348623157e+308"},
            },
            {
                "status": "Canceled",
                "event_label": "已取消",
                "event_type": "status_change",
                "callback_type": "orderStatus",
                "filled_qty": 0,
                "fill_delta": 0,
            },
        )

        content = card["elements"][0]["content"]
        self.assertIn("**均价 / 最新成交价**: - / -", content)
        self.assertNotIn("179769313486", content)

    def test_sync_order_callback_ledger_notification_includes_exit_loss_from_group_prices(self):
        class _LedgerPB:
            def __init__(self):
                self.entry = {
                    "id": "order-entry",
                    "unique_id": "sig-1_entry",
                    "order_type": "Entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Closed",
                    "role": "entry",
                    "broker_order_id": "101",
                    "order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_price": 100.0,
                    "extra": {
                        "reference_entry": 99.5,
                        "submitted_entry_limit_price": 99.8,
                    },
                }
                self.order = {
                    "id": "order-close",
                    "unique_id": "sig-1_close",
                    "order_type": "MKT",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "close",
                    "broker_order_id": "102",
                    "order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "sell",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_price": 99.0,
                    "extra": {
                        "environment": "live",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "broker_callback_received_at": "2026-04-22 09:40:00",
                    },
                }
                self.updated = []

            def get_records(self, collection, **kwargs):
                return [self.entry, self.order] if collection == "orders" else []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "filled_qty": 0, "extra": {"environment": "live"}}
        send_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-loss"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(result["event_type"], "fill")
        self.assertEqual(len(send_calls), 1)
        card = send_calls[0][0]
        content = card["elements"][0]["content"]
        title = card["header"]["title"]["content"]
        self.assertIn("亏损 -$10.00", title)
        self.assertEqual("red", card["header"]["template"])
        self.assertIn("**实际盈亏**: 亏损 -$10.00", content)
        self.assertIn("平仓（原因未记录） @99.00", content)
        self.assertIn("入场 @100.00", content)
        self.assertIn("参考 99.50", content)
        self.assertIn("提交 99.80", content)
        self.assertIn("成本/滑点 +50.25bps", content)
        self.assertIn("+$0.50/股", content)
        self.assertIn("10股", content)
        self.assertIn("未计手续费", content)

    def test_sync_order_callback_ledger_notification_includes_short_exit_profit(self):
        class _LedgerPB:
            def __init__(self):
                self.entry = {
                    "id": "order-entry",
                    "unique_id": "sig-short_entry",
                    "order_type": "Entry",
                    "symbol": "TSLA",
                    "environment": "paper",
                    "status": "Closed",
                    "role": "entry",
                    "broker_order_id": "201",
                    "order_id": "201",
                    "trade_group_id": "sig-short_entry",
                    "entry_order_unique_id": "sig-short_entry",
                    "signal_id": "sig-short",
                    "direction": "short",
                    "quantity": 4,
                    "filled_qty": 4,
                    "fill_price": 100.0,
                }
                self.order = {
                    "id": "order-close",
                    "unique_id": "sig-short_close",
                    "order_type": "MKT",
                    "symbol": "TSLA",
                    "environment": "paper",
                    "status": "Filled",
                    "role": "close",
                    "broker_order_id": "202",
                    "order_id": "202",
                    "trade_group_id": "sig-short_entry",
                    "entry_order_unique_id": "sig-short_entry",
                    "signal_id": "sig-short",
                    "direction": "buy",
                    "quantity": 4,
                    "filled_qty": 4,
                    "fill_price": 95.0,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "broker_callback_received_at": "2026-04-22 09:45:00",
                    },
                }
                self.updated = []

            def get_records(self, collection, **kwargs):
                return [self.entry, self.order] if collection == "orders" else []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "filled_qty": 0, "extra": {"environment": "paper"}}
        send_calls = []

        sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-profit"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        card = send_calls[0][0]
        content = card["elements"][0]["content"]
        self.assertIn("盈利 +$20.00", card["header"]["title"]["content"])
        self.assertEqual("green", card["header"]["template"])
        self.assertIn("**实际盈亏**: 盈利 +$20.00", content)
        self.assertIn("平仓（原因未记录） @95.00", content)
        self.assertIn("**入场价格**: 100.00", content)
        self.assertIn("**出场价格**: 95.00", content)
        self.assertIn("**本次成交价**: 95.00", content)
        self.assertIn("**PnL计算数量**: 4", content)

    def test_order_callback_ledger_caps_reused_order_id_polluted_quantity(self):
        entry = {
            "id": "order-entry",
            "unique_id": "entry_BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "order_type": "LMT",
            "symbol": "HOOD",
            "environment": "paper",
            "status": "Filled",
            "role": "entry",
            "broker_order_id": "11383",
            "trade_group_id": "BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "signal_id": "BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "direction": "short",
            "quantity": 50,
            "filled_qty": 121,
            "fill_price": 81.715785,
            "commission": 2.227643,
            "extra": {"execution_price": 99.19, "last_fill_price": 99.19},
        }
        stop = {
            "id": "order-sl",
            "unique_id": "sl_BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "order_type": "STP",
            "symbol": "HOOD",
            "environment": "paper",
            "status": "Filled",
            "role": "stop_loss",
            "broker_order_id": "11385",
            "trade_group_id": "BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "signal_id": "BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            "direction": "buy",
            "quantity": 50,
            "filled_qty": 121,
            "fill_price": 82.238512,
            "commission": 2.000363,
            "extra": {
                "environment": "paper",
                "ib_callback_type": "execDetails",
                "execution_price": 98.97,
                "last_fill_price": 98.97,
            },
        }

        card = build_order_callback_ledger_card(
            stop,
            {"event_type": "fill", "status": "Filled", "filled_qty": 121, "fill_delta": 121},
            related_rows=[entry],
        )
        content = card["elements"][0]["content"]

        self.assertIn("盈利 +$6.77", card["header"]["title"]["content"])
        self.assertIn("**实际盈亏**: 盈利 +$6.77", content)
        self.assertIn("止损 @98.97", content)
        self.assertIn("入场 @99.19", content)
        self.assertIn("50股", content)
        self.assertIn("**入场价格**: 99.19", content)
        self.assertIn("**出场价格**: 98.97", content)
        self.assertIn("**本次成交价**: 98.97", content)
        self.assertIn("**PnL计算数量**: 50", content)

    def test_sync_order_callback_ledger_notification_uses_position_cost_for_unlinked_close(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-close",
                    "unique_id": "close_DELL_20260602_155536",
                    "order_type": "MKT",
                    "symbol": "DELL",
                    "environment": "paper",
                    "status": "Submitted",
                    "role": "close",
                    "broker_order_id": "166",
                    "order_id": "166",
                    "trade_group_id": "close_DELL_20260602_155536",
                    "entry_order_unique_id": "close_DELL_20260602_155536",
                    "signal_id": "",
                    "direction": "short",
                    "quantity": 11,
                    "filled_qty": 11,
                    "fill_price": 435.66,
                    "extra": {
                        "environment": "paper",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "execDetails",
                        "broker_callback_received_at": "2026-06-02T15:55:36.973664-04:00",
                        "entry_price_for_pnl": 437.0,
                        "position_avg_cost": 437.0,
                    },
                }
                self.updated = []

            def get_records(self, collection, **kwargs):
                return []

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                self.order = {**self.order, **patch}
                return dict(self.order)

        pb = _LedgerPB()
        previous = {**pb.order, "filled_qty": 0, "extra": {"environment": "paper"}}
        send_calls = []

        sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "ledger-msg-unlinked"},
            trade_ledger_chat_id="ledger-chat-test",
            console_base_url="https://console.example.com",
        )

        card = send_calls[0][0]
        content = card["elements"][0]["content"]
        self.assertIn("盈利 +$14.74", card["header"]["title"]["content"])
        self.assertIn("**状态**: 已成交", content)
        self.assertIn("**实际盈亏**: 盈利 +$14.74", content)
        self.assertIn("平仓（原因未记录） @435.66", content)

    def test_order_callback_ledger_labels_runner_stop_close_reason(self):
        order = {
            "id": "order-close",
            "unique_id": "close_TSLA_runner",
            "order_type": "MKT",
            "symbol": "TSLA",
            "environment": "paper",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "216",
            "trade_group_id": "BATS_TSLA_short_20260605_0946_2_mr_sdUpper",
            "entry_order_unique_id": "BATS_TSLA_short_20260605_0946_2_mr_sdUpper",
            "signal_id": "BATS_TSLA_short_20260605_0946_2_mr_sdUpper",
            "direction": "short",
            "quantity": 12,
            "filled_qty": 12,
            "fill_price": 403.38,
            "extra": {
                "environment": "paper",
                "close_reason": "runner_stop",
                "ib_callback_type": "execDetails",
            },
        }

        card = build_order_callback_ledger_card(order, {"event_type": "fill", "status": "Filled", "filled_qty": 12, "fill_delta": 12})
        content = card["elements"][0]["content"]
        title = card["header"]["title"]["content"]

        self.assertIn("Runner 止损", title)
        self.assertIn("做空 / SHORT", title)
        self.assertIn("**交易方向**: 做空 / SHORT", content)
        self.assertIn("**角色 / 类型**: Runner 止损 / MKT", content)
        self.assertNotIn("**角色 / 类型 / 方向**", content)
        self.assertIn("**平仓原因**: Runner 止损（runner_stop）", content)

    def test_order_callback_ledger_card_surfaces_long_direction(self):
        order = {
            "id": "order-entry",
            "unique_id": "sig-long-entry",
            "order_type": "LMT",
            "symbol": "AAPL",
            "environment": "paper",
            "status": "Filled",
            "role": "entry",
            "broker_order_id": "401",
            "trade_group_id": "sig-long-entry",
            "signal_id": "sig-long",
            "direction": "long",
            "quantity": 5,
            "filled_qty": 5,
            "fill_price": 190.0,
            "extra": {"environment": "paper", "ib_callback_type": "execDetails"},
        }

        card = build_order_callback_ledger_card(order, {"event_type": "fill", "status": "Filled", "filled_qty": 5, "fill_delta": 5})
        content = card["elements"][0]["content"]
        title = card["header"]["title"]["content"]

        self.assertIn("做多 / LONG", title)
        self.assertIn("**交易方向**: 做多 / LONG", content)
        self.assertIn("**角色 / 类型**: 入场 / LMT", content)

    def test_order_status_card_surfaces_unknown_direction(self):
        order = {
            "id": "order-status",
            "unique_id": "sig-unknown-entry",
            "order_type": "LMT",
            "symbol": "MSFT",
            "environment": "paper",
            "status": "Submitted",
            "role": "entry",
            "broker_order_id": "402",
            "trade_group_id": "sig-unknown-entry",
            "signal_id": "sig-unknown",
            "quantity": 2,
            "filled_qty": 0,
            "extra": {"environment": "paper"},
        }

        card = build_order_status_card(order, status="Submitted")
        content = card["elements"][0]["content"]
        title = card["header"]["title"]["content"]

        self.assertIn("方向未知", title)
        self.assertIn("**交易方向**: 方向未知", content)
        self.assertIn("**角色 / 类型**: entry / LMT", content)

    def test_order_callback_ledger_labels_eod_close_from_source(self):
        order = {
            "id": "order-close",
            "unique_id": "eod_force_close_AAPL_20260605_155500",
            "order_type": "MKT",
            "symbol": "AAPL",
            "environment": "paper",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "301",
            "direction": "long",
            "quantity": 3,
            "filled_qty": 3,
            "fill_price": 190.0,
            "extra": {
                "environment": "paper",
                "source": "eod_force_close",
                "ib_callback_type": "execDetails",
            },
        }

        card = build_order_callback_ledger_card(order, {"event_type": "fill", "status": "Filled", "filled_qty": 3, "fill_delta": 3})
        content = card["elements"][0]["content"]

        self.assertIn("EOD 平仓", card["header"]["title"]["content"])
        self.assertIn("**平仓原因**: EOD 平仓（force_flat_eod）", content)

    def test_order_callback_ledger_labels_eod_residual_close_as_eod(self):
        order = {
            "id": "order-close",
            "unique_id": "close_AAPL_20260605_155900",
            "order_type": "LMT",
            "symbol": "AAPL",
            "environment": "paper",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "302",
            "direction": "long",
            "quantity": 3,
            "filled_qty": 3,
            "fill_price": 190.0,
            "extra": {
                "environment": "paper",
                "close_reason": "force_flat_eod_residual",
                "ib_callback_type": "execDetails",
            },
        }

        card = build_order_callback_ledger_card(order, {"event_type": "fill", "status": "Filled", "filled_qty": 3, "fill_delta": 3})
        content = card["elements"][0]["content"]

        self.assertIn("EOD 平仓", card["header"]["title"]["content"])
        self.assertIn("**平仓原因**: EOD 平仓（force_flat_eod_residual）", content)

    def test_order_callback_ledger_uses_related_order_flow_reason_for_close(self):
        entry = {
            "id": "order-entry",
            "unique_id": "sig-flow_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "environment": "live",
            "status": "Closed",
            "role": "entry",
            "trade_group_id": "sig-flow_entry",
            "entry_order_unique_id": "sig-flow_entry",
            "direction": "long",
            "quantity": 10,
            "filled_qty": 10,
            "fill_price": 100.0,
            "extra": {
                "reason": "order_flow_adverse_delta_exit",
                "order_flow_decision": {"reason": "order_flow_adverse_delta_exit"},
            },
        }
        close = {
            "id": "order-close",
            "unique_id": "sig-flow_close",
            "order_type": "MKT",
            "symbol": "AAPL",
            "environment": "live",
            "status": "Filled",
            "role": "close",
            "broker_order_id": "302",
            "trade_group_id": "sig-flow_entry",
            "entry_order_unique_id": "sig-flow_entry",
            "direction": "sell",
            "quantity": 10,
            "filled_qty": 10,
            "fill_price": 99.0,
            "extra": {"environment": "live", "ib_callback_type": "execDetails"},
        }

        card = build_order_callback_ledger_card(
            close,
            {"event_type": "fill", "status": "Filled", "filled_qty": 10, "fill_delta": 10},
            related_rows=[entry],
        )
        content = card["elements"][0]["content"]

        self.assertIn("订单流提前平仓", card["header"]["title"]["content"])
        self.assertIn("**平仓原因**: 订单流提前平仓（order_flow_adverse_delta_exit）", content)
        self.assertIn("订单流提前平仓 @99.00", content)

    def test_sync_order_callback_ledger_notification_skips_submitted_open_order_noise(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-entry",
                    "unique_id": "sig-1_entry",
                    "order_type": "Entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "broker_order_id": "101",
                    "order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "fill_price": 0,
                    "extra": {
                        "environment": "live",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "openOrder",
                        "broker_callback_received_at": "2026-04-22 09:35:01",
                    },
                }

            def update_record(self, collection, record_id, patch):
                raise AssertionError("noise callback should not update notification state")

        pb = _LedgerPB()
        send_calls = []

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order={**pb.order, "broker_order_id": "", "order_id": "", "extra": {"environment": "live"}},
            send_interactive=lambda *args, **kwargs: send_calls.append((args, kwargs)) or {"success": True},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "non_terminal_callback_noise")
        self.assertEqual(send_calls, [])

    def test_sync_order_callback_ledger_notification_skips_full_fill_duplicate_status(self):
        class _LedgerPB:
            def __init__(self):
                self.order = {
                    "id": "order-entry",
                    "unique_id": "sig-1_entry",
                    "order_type": "Entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Filled",
                    "role": "entry",
                    "broker_order_id": "101",
                    "order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_price": 180.2,
                    "extra": {
                        "environment": "live",
                        "broker_realtime_callback": True,
                        "ib_callback_type": "orderStatus",
                        "broker_callback_received_at": "2026-04-22 09:36:30",
                        "feishu_trade_ledger_last_result": "success",
                        "feishu_trade_ledger_notified_keys": ["trade_ledger_callback_v1:live:101:filled"],
                    },
                }

            def update_record(self, collection, record_id, patch):
                raise AssertionError("duplicate full-fill status should not update notification state")

        pb = _LedgerPB()
        send_calls = []
        previous = {
            **pb.order,
            "status": "Submitted",
            "filled_qty": 10,
            "extra": {"environment": "live", "broker_realtime_callback": False},
        }

        result = sync_order_callback_ledger_notification(
            pb,
            pb.order,
            previous_order=previous,
            send_interactive=lambda *args, **kwargs: send_calls.append((args, kwargs)) or {"success": True},
            trade_ledger_chat_id="ledger-chat-test",
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "full_fill_already_seen")
        self.assertEqual(send_calls, [])

    def test_sync_order_status_notification_sends_then_updates_group_card(self):
        class _OrderNotifyPB:
            def __init__(self):
                self.orders = {
                    "order-entry": {
                        "id": "order-entry",
                        "unique_id": "sig-1_entry",
                        "order_type": "Entry",
                        "symbol": "AAPL",
                        "environment": "live",
                        "status": "Submitted",
                        "role": "entry",
                        "order_id": "101",
                        "broker_order_id": "101",
                        "trade_group_id": "sig-1_entry",
                        "entry_order_unique_id": "sig-1_entry",
                        "signal_id": "sig-1",
                        "direction": "long",
                        "quantity": 10,
                        "filled_qty": 0,
                        "limit_price": 180.1,
                        "us_time": "2026-04-22 09:35:00",
                        "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                    },
                    "order-tp": {
                        "id": "order-tp",
                        "unique_id": "sig-1_tp",
                        "order_type": "TakeProfit",
                        "symbol": "AAPL",
                        "environment": "live",
                        "status": "Submitted",
                        "role": "take_profit",
                        "order_id": "102",
                        "broker_order_id": "102",
                        "trade_group_id": "sig-1_entry",
                        "entry_order_unique_id": "sig-1_entry",
                        "parent_order_unique_id": "sig-1_entry",
                        "signal_id": "sig-1",
                        "direction": "long",
                        "quantity": 10,
                        "filled_qty": 0,
                        "limit_price": 184.0,
                        "us_time": "2026-04-22 09:35:00",
                        "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                    },
                }
                self.updated = []

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                assert collection == "orders"
                return [dict(row) for row in self.orders.values()]

            def update_record(self, collection, record_id, patch):
                assert collection == "orders"
                current = {**self.orders[record_id], **patch}
                self.orders[record_id] = current
                self.updated.append((collection, record_id, patch))
                return dict(current)

        pb = _OrderNotifyPB()
        send_calls = []
        update_calls = []

        first = sync_order_status_notification(
            pb,
            pb.orders["order-entry"],
            action="Submitted",
            message="订单已提交",
            send_interactive=lambda card, chat_id, environment: send_calls.append((card, chat_id, environment))
            or {"success": True, "message_id": "order-msg-1"},
            update_interactive=lambda *args, **kwargs: update_calls.append((args, kwargs)) or {"success": True},
            order_chat_id="order-chat-test",
            console_base_url="https://console.example.com",
        )
        pb.orders["order-tp"]["status"] = "Filled"
        pb.orders["order-tp"]["filled_qty"] = 10
        pb.orders["order-tp"]["fill_price"] = 184.0
        pb.orders["order-tp"]["extra"] = {**pb.orders["order-tp"]["extra"], "current_status": "Filled"}

        second = sync_order_status_notification(
            pb,
            pb.orders["order-tp"],
            action="Filled",
            message="止盈成交，交易组已关闭",
            send_interactive=lambda *args, **kwargs: send_calls.append((args, kwargs)) or {"success": True},
            update_interactive=lambda message_id, card, environment: update_calls.append((message_id, card, environment))
            or {"success": True, "message_id": message_id},
            order_chat_id="order-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertEqual(first["message_id"], "order-msg-1")
        self.assertEqual(second["message_id"], "order-msg-1")
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][1], "order-chat-test")
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0], "order-msg-1")
        self.assertEqual(second["group_status"], "Closed")
        self.assertEqual(pb.orders["order-entry"]["extra"]["feishu_order_message_id"], "order-msg-1")
        self.assertEqual(pb.orders["order-entry"]["extra"]["feishu_order_notify_last_status"], "Closed")

    def test_sync_order_status_notification_serializes_first_send_race(self):
        import threading
        import time

        class _OrderNotifyPB:
            def __init__(self):
                self.lock = threading.Lock()
                self.orders = {
                    "order-entry": {
                        "id": "order-entry",
                        "unique_id": "sig-1_entry",
                        "order_type": "Entry",
                        "symbol": "AAPL",
                        "environment": "live",
                        "status": "Submitted",
                        "role": "entry",
                        "order_id": "101",
                        "broker_order_id": "101",
                        "trade_group_id": "sig-1_entry",
                        "entry_order_unique_id": "sig-1_entry",
                        "signal_id": "sig-1",
                        "direction": "long",
                        "quantity": 10,
                        "filled_qty": 0,
                        "limit_price": 180.1,
                        "us_time": "2026-04-22 09:35:00",
                        "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                    },
                    "order-tp": {
                        "id": "order-tp",
                        "unique_id": "sig-1_tp",
                        "order_type": "TakeProfit",
                        "symbol": "AAPL",
                        "environment": "live",
                        "status": "Submitted",
                        "role": "take_profit",
                        "order_id": "102",
                        "broker_order_id": "102",
                        "trade_group_id": "sig-1_entry",
                        "entry_order_unique_id": "sig-1_entry",
                        "parent_order_unique_id": "sig-1_entry",
                        "signal_id": "sig-1",
                        "direction": "long",
                        "quantity": 10,
                        "filled_qty": 0,
                        "limit_price": 184.0,
                        "us_time": "2026-04-22 09:35:00",
                        "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                    },
                }

            def _copy_row(self, row):
                return {**row, "extra": dict(row.get("extra") or {})}

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                assert collection == "orders"
                with self.lock:
                    return [self._copy_row(row) for row in self.orders.values()]

            def update_record(self, collection, record_id, patch):
                assert collection == "orders"
                with self.lock:
                    current = {**self.orders[record_id], **patch}
                    if "extra" in patch:
                        current["extra"] = dict(patch["extra"] or {})
                    self.orders[record_id] = current
                    return self._copy_row(current)

            def mark_target_filled(self):
                with self.lock:
                    row = self.orders["order-tp"]
                    row["status"] = "Filled"
                    row["filled_qty"] = 10
                    row["fill_price"] = 184.0
                    row["extra"] = {**row["extra"], "current_status": "Filled"}

        pb = _OrderNotifyPB()
        send_started = threading.Event()
        send_calls = []
        update_calls = []
        results = {}
        errors = []

        def send_interactive(card, chat_id, environment):
            send_calls.append((card, chat_id, environment))
            send_started.set()
            time.sleep(0.05)
            return {"success": True, "message_id": "order-msg-race"}

        def update_interactive(message_id, card, environment):
            update_calls.append((message_id, card, environment))
            return {"success": True, "message_id": message_id}

        def run_entry():
            try:
                results["entry"] = sync_order_status_notification(
                    pb,
                    pb.orders["order-entry"],
                    action="Submitted",
                    message="订单已提交",
                    send_interactive=send_interactive,
                    update_interactive=update_interactive,
                    order_chat_id="order-chat-test",
                    console_base_url="https://console.example.com",
                )
            except Exception as exc:
                errors.append(exc)

        def run_target():
            try:
                results["target"] = sync_order_status_notification(
                    pb,
                    pb.orders["order-tp"],
                    action="Filled",
                    message="止盈成交，交易组已关闭",
                    send_interactive=send_interactive,
                    update_interactive=update_interactive,
                    order_chat_id="order-chat-test",
                    console_base_url="https://console.example.com",
                )
            except Exception as exc:
                errors.append(exc)

        entry_thread = threading.Thread(target=run_entry)
        entry_thread.start()
        self.assertTrue(send_started.wait(2))
        pb.mark_target_filled()
        target_thread = threading.Thread(target=run_target)
        target_thread.start()
        entry_thread.join(2)
        target_thread.join(2)

        self.assertEqual([], errors)
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(results["entry"]["message_id"], "order-msg-race")
        self.assertEqual(results["target"]["message_id"], "order-msg-race")
        self.assertEqual(results["target"]["group_status"], "Closed")
        self.assertEqual(update_calls[0][0], "order-msg-race")
        self.assertEqual(pb.orders["order-entry"]["extra"]["feishu_order_message_id"], "order-msg-race")
        self.assertEqual(pb.orders["order-entry"]["extra"]["feishu_order_notify_last_status"], "Closed")

    def test_sync_order_status_notification_waits_for_primary_before_child_first_send(self):
        class _ChildOnlyPB:
            def __init__(self):
                self.order = {
                    "id": "order-tp",
                    "unique_id": "sig-1_tp",
                    "order_type": "TakeProfit",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_id": "102",
                    "broker_order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "limit_price": 184.0,
                    "us_time": "2026-04-22 09:35:00",
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                }
                self.updated = []

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                assert collection == "orders"
                return [dict(self.order)]

            def update_record(self, collection, record_id, patch):
                self.updated.append((collection, record_id, patch))
                return {**self.order, **patch}

        pb = _ChildOnlyPB()
        send_calls = []
        update_calls = []

        result = sync_order_status_notification(
            pb,
            pb.order,
            action="Submitted",
            message="订单已提交",
            send_interactive=lambda *args, **kwargs: send_calls.append((args, kwargs)) or {"success": True, "message_id": "unexpected"},
            update_interactive=lambda *args, **kwargs: update_calls.append((args, kwargs)) or {"success": True},
            order_chat_id="order-chat-test",
            console_base_url="https://console.example.com",
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "waiting_for_primary_order")
        self.assertEqual(result["message_id"], "")
        self.assertEqual(send_calls, [])
        self.assertEqual(update_calls, [])
        self.assertEqual(pb.updated, [])

    def test_build_order_upsert_keeps_existing_stop_price_when_live_stp_price_is_zero(self):
        existing_row = {
            "id": "order-sl-1",
            "unique_id": "sig-1_sl",
            "order_type": "STP",
            "order_id": "27",
            "broker_order_id": "27",
            "symbol": "AAPL",
            "environment": "live",
            "direction": "long",
            "quantity": 10,
            "limit_price": 178.0,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "sig-1",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "sig-1_entry",
            "sibling_order_unique_id": "sig-1_tp",
            "role": "stop_loss",
            "relation_status": "active",
            "position_side": "long",
            "order_time": "2026-04-22 09:35:00",
            "fill_time": "",
            "bar_time_ms": 1713797700000,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "extra": {"role": "stop_loss", "limit_price": 178.0},
        }
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_sl",
            "order_type": "STP",
            "order_id": "27",
            "broker_order_id": "27",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 0,
            "status": "Filled",
            "filled_qty": 10,
            "fill_price": 178.2,
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "sig-1_entry",
            "sibling_order_unique_id": "sig-1_tp",
            "role": "stop_loss",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:36:00",
            "cn_time": "2026-04-22 21:36:00",
            "bar_time_ms": 1713797760000,
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=existing_row)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock(side_effect=lambda collection, record_id, data: {**data, "id": record_id})
        fake_pb.create_record = mock.Mock(side_effect=lambda collection, data: {**data, "id": f"{collection}-1"})

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        saved_order = fake_pb.update_record.call_args.args[2]
        self.assertEqual(178.0, saved_order["limit_price"])
        self.assertEqual("Filled", saved_order["status"])
