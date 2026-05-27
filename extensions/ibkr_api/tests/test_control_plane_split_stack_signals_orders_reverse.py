from control_plane_split_stack_helpers import *
from ibkr_api.orders.notifications import sync_order_callback_ledger_notification, sync_order_status_notification


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

        with mock.patch.object(api_app_mod.request, "args", _RequestArgs({"environment": "live", "date": "2026-04-22"})):
            with mock.patch.object(api_app_mod.pb, "get_records", return_value=signal_rows) as records_mock:
                with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=indicator_row) as first_mock:
                    payload = api_app_mod.custom_ibkr_signals_pending()

        records_mock.assert_called_once()
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
        self.assertEqual(pb.updated[0][2]["extra"]["feishu_trade_ledger_message_id"], "ledger-msg-1")
        self.assertIn(
            pb.updated[0][2]["extra"]["feishu_trade_ledger_notify_key"],
            pb.updated[0][2]["extra"]["feishu_trade_ledger_notified_keys"],
        )

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
