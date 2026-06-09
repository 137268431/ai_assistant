import unittest
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.action_builders import positions as positions_mod


class FakeApiApp:
    def _ibkr_service_uses_paper_account(self, service):
        return False


class FakeOrderPlacer:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "order_ids": ["9001"]}
        self.calls = []

    def place_market_close(self, **kwargs):
        self.calls.append(dict(kwargs))
        return dict(self.result)


class FakeOrderModifier:
    def __init__(self, failures=None):
        self.failures = set(failures or [])
        self.cancelled = []

    def cancel_order(self, order_id, acct_id=None):
        self.cancelled.append(str(order_id))
        if str(order_id) in self.failures:
            return {"ok": False, "error": "cancel_failed"}
        return {"ok": True, "order_id": str(order_id)}


class FakeOrderTracker:
    def __init__(self, orders):
        self.orders = list(orders or [])

    def get_live_orders(self):
        return list(self.orders)


class FakeConidResolver:
    def __init__(self, conid):
        self.conid = conid
        self.calls = []

    def resolve(self, symbol):
        self.calls.append(symbol)
        return self.conid


class FakeLifecycle:
    def __init__(self, positions=None, result=None):
        self.positions = list(positions or [])
        self.result = result

    def get_positions_result(self):
        if self.result is not None:
            return dict(self.result)
        return {"ok": True, "positions": list(self.positions)}


class StrictNotifyPBClient:
    def __init__(self):
        self.system_events = []

    def notify_system_event(
        self,
        title,
        detail=None,
        *,
        event_type="status_change",
        level="info",
        source="ibkr_compute",
        environment=None,
        message_id="",
    ):
        self.system_events.append(
            {
                "title": title,
                "detail": dict(detail or {}),
                "event_type": event_type,
                "level": level,
                "source": source,
                "environment": environment,
                "message_id": message_id,
            }
        )
        return {"ok": True}


class FakeService:
    def __init__(self, *, orders=None, conid=123, positions=None):
        self.order_placer = FakeOrderPlacer()
        self.order_modifier = FakeOrderModifier()
        self.order_tracker = FakeOrderTracker(orders or [])
        self.conid_resolver = FakeConidResolver(conid)
        self.order_lifecycle = FakeLifecycle(positions or [])
        self.environment = "paper"
        self.pb_client = None


class ClosePositionActionTest(unittest.TestCase):
    def setUp(self):
        self._orig_api_app = positions_mod._api_app
        self._orig_coerce_float = positions_mod._app_coerce_float
        self._orig_snapshot = positions_mod._build_snapshot_action_response
        positions_mod._api_app = lambda: FakeApiApp()
        positions_mod._app_coerce_float = lambda value, default=None: default if value in (None, "") else float(value)
        positions_mod._build_snapshot_action_response = (
            lambda service, action, result, **kwargs: (
                {"ok": bool(result.get("ok")), "action": action, "result": result, **(kwargs.get("extra") or {})},
                200 if result.get("ok") else 500,
            )
        )

    def tearDown(self):
        positions_mod._api_app = self._orig_api_app
        positions_mod._app_coerce_float = self._orig_coerce_float
        positions_mod._build_snapshot_action_response = self._orig_snapshot

    def test_market_close_cancels_matching_bracket_children(self):
        service = FakeService(
            orders=[
                {
                    "orderId": "101",
                    "ticker": "NVDA",
                    "side": "BUY",
                    "status": "Filled",
                    "cOID": "entry_NVDA_long_20260514_094637",
                },
                {
                    "orderId": "102",
                    "ticker": "NVDA",
                    "side": "SELL",
                    "status": "Submitted",
                    "parentId": "101",
                    "orderType": "LMT",
                    "cOID": "tp_NVDA_long_20260514_094637",
                    "ocaGroup": "NVDA_long_20260514_094637",
                },
                {
                    "orderId": "103",
                    "ticker": "NVDA",
                    "side": "SELL",
                    "status": "Submitted",
                    "parentId": "101",
                    "orderType": "STP",
                    "cOID": "sl_NVDA_long_20260514_094637",
                    "ocaGroup": "NVDA_long_20260514_094637",
                },
                {
                    "orderId": "201",
                    "ticker": "MSFT",
                    "side": "SELL",
                    "status": "Submitted",
                    "parentId": "200",
                    "orderType": "STP",
                    "cOID": "sl_MSFT_long_20260514_094637",
                },
            ]
        )

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 0,
                "quantity": 43,
                "direction": "long",
                "avg_cost": 500.25,
                "market_price": 505.0,
                "unrealized_pnl": 204.25,
                "trade_group_id": "NVDA_long_20260514_094637",
                "entry_order_unique_id": "entry_NVDA_long_20260514_094637",
            },
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(123, service.order_placer.calls[0]["conid"])
        self.assertEqual(500.25, service.order_placer.calls[0]["position_snapshot"]["avg_cost"])
        self.assertEqual(505.0, service.order_placer.calls[0]["position_snapshot"]["market_price"])
        self.assertEqual(204.25, service.order_placer.calls[0]["position_snapshot"]["unrealized_pnl"])
        self.assertEqual(["102", "103"], service.order_modifier.cancelled)
        self.assertEqual(
            ["102", "103"],
            payload["result"]["protection_cancel"]["cancelled_order_ids"],
        )

    def test_close_position_passes_marketable_limit_controls(self):
        service = FakeService(orders=[])

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 123,
                "quantity": 3,
                "direction": "long",
                "order_type": "marketable_limit",
                "limit_price": 500.12,
                "wait_for_fill": True,
                "fill_timeout": 17,
                "outside_rth": True,
                "tif": "DAY",
            },
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        call = service.order_placer.calls[0]
        self.assertEqual("marketable_limit", call["order_type"])
        self.assertEqual(500.12, call["limit_price"])
        self.assertTrue(call["wait_for_fill"])
        self.assertEqual(17.0, call["fill_timeout"])
        self.assertTrue(call["outside_rth"])
        self.assertEqual("DAY", call["tif"])

    def test_close_position_defaults_to_auto_session_limit(self):
        service = FakeService(orders=[])

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 123,
                "quantity": 3,
                "direction": "short",
                "market_price": 500.12,
            },
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        call = service.order_placer.calls[0]
        self.assertEqual("LMT", call["order_type"])
        self.assertEqual("auto_session_limit", call["execution_profile"])
        self.assertEqual(500.12, call["position_snapshot"]["market_price"])
        self.assertIsNone(call["outside_rth"])

    def test_close_position_cancels_existing_close_order_before_submit(self):
        service = FakeService(
            orders=[
                {
                    "orderId": "301",
                    "ticker": "NVDA",
                    "side": "SELL",
                    "status": "Submitted",
                    "orderType": "LMT",
                    "cOID": "close_NVDA_20260608_100000",
                }
            ]
        )

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 123,
                "quantity": 3,
                "direction": "long",
                "market_price": 500.12,
            },
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(["301"], service.order_modifier.cancelled)
        self.assertEqual(["301"], payload["result"]["existing_close_cancel"]["cancelled_order_ids"])

    def test_existing_close_cancel_failure_notifies_with_supported_signature(self):
        service = FakeService(
            orders=[
                {
                    "orderId": "301",
                    "ticker": "NVDA",
                    "side": "SELL",
                    "status": "Submitted",
                    "orderType": "LMT",
                    "cOID": "close_NVDA_20260608_100000",
                }
            ]
        )
        service.order_modifier = FakeOrderModifier(failures={"301"})
        service.pb_client = StrictNotifyPBClient()

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 123,
                "quantity": 3,
                "direction": "long",
                "market_price": 500.12,
            },
        )

        self.assertEqual(409, status_code)
        self.assertFalse(payload["ok"])
        self.assertEqual(1, len(service.pb_client.system_events))
        event = service.pb_client.system_events[0]
        self.assertEqual("平仓未执行：旧平仓挂单取消失败", event["title"])
        self.assertEqual("ibkr_close_execution", event["event_type"])
        self.assertEqual("ibkr_account_action", event["source"])
        self.assertNotIn("category", event)

    def test_market_close_uses_explicit_protection_order_ids(self):
        service = FakeService(orders=[])

        payload, status_code = positions_mod._build_ibkr_close_position_response(
            service,
            {
                "symbol": "NVDA",
                "conid": 123,
                "quantity": 43,
                "direction": "long",
                "cancel_order_ids": ["102", "103"],
            },
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(["102", "103"], service.order_modifier.cancelled)

    def test_close_all_positions_submits_limit_closes_for_each_position(self):
        service = FakeService(
            positions=[
                {"ticker": "MSTR", "conid": 272110, "position": -39, "mktPrice": 127.5},
                {"ticker": "NVDA", "conid": 4815747, "position": 23, "mktPrice": 145.2},
                {"ticker": "BOXX", "conid": 1, "position": 2, "mktPrice": 108.0},
            ]
        )

        payload, status_code = positions_mod._build_ibkr_close_all_positions_response(
            service,
            {"keep_symbols": "BOXX"},
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["closed"])
        self.assertEqual(2, len(service.order_placer.calls))
        self.assertEqual(["MSTR", "NVDA"], [call["symbol"] for call in service.order_placer.calls])
        self.assertEqual(["LMT", "LMT"], [call["order_type"] for call in service.order_placer.calls])


if __name__ == "__main__":
    unittest.main()
