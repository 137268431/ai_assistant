import unittest

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


class FakeService:
    def __init__(self, *, orders=None, conid=123):
        self.order_placer = FakeOrderPlacer()
        self.order_modifier = FakeOrderModifier()
        self.order_tracker = FakeOrderTracker(orders or [])
        self.conid_resolver = FakeConidResolver(conid)


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


if __name__ == "__main__":
    unittest.main()
