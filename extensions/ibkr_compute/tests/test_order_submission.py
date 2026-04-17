import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker import ib_gateway
from ibkr_compute.signal.signal_router import SignalRouter


class FakeOrder:
    def __init__(self):
        self.eTradeOnly = True
        self.firmQuoteOnly = True


class FakeContract:
    pass


class FakeClient:
    def __init__(self, ack_result):
        self.ack_result = ack_result
        self.placed_orders = []
        self.cleared_order_errors = []

    def next_order_ids(self, count):
        base = [101, 102, 103]
        return base[:count]

    def clear_order_error(self, order_id):
        self.cleared_order_errors.append(str(order_id))

    def place_order(self, contract, order):
        self.placed_orders.append((contract, order))

    def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
        return dict(self.ack_result)


class FakePBClient:
    def __init__(self, rows):
        self.rows = list(rows)

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        return list(self.rows)


class FakeConfig:
    def get_for_environment(self, key, environment, default=None):
        return default


class BrokerAdapterOrderSubmissionTest(unittest.TestCase):
    def test_place_bracket_order_clears_legacy_flags_and_surfaces_entry_rejection(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            {
                "ok": False,
                "error": "The 'EtradeOnly' order attribute is not supported.",
                "details": {"code": 10268},
            }
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 346218218,
            "symbol": "DELL",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_bracket_order(
                adapter,
                conid=346218218,
                symbol="DELL",
                direction="long",
                quantity=53,
                entry_price=191.23,
                take_profit_price=195.48,
                stop_loss_price=188.4,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertFalse(result["ok"])
        self.assertIn("EtradeOnly", result["error"])
        self.assertEqual(["101", "102", "103"], result["order_ids"])
        self.assertEqual(["101", "102", "103"], adapter.client.cleared_order_errors)
        self.assertEqual(3, len(adapter.client.placed_orders))
        for _, placed_order in adapter.client.placed_orders:
            self.assertFalse(placed_order.eTradeOnly)
            self.assertFalse(placed_order.firmQuoteOnly)


class SignalRouterDedupeTest(unittest.TestCase):
    def test_fetch_pending_signals_skips_duplicate_inflight_and_processed_ids(self):
        rows = [
            {
                "signal_id": "dup_signal",
                "symbol": "DELL",
                "direction": "long",
                "entry": 191.23,
                "stop_loss": 188.4,
                "take_profit": 195.48,
                "shares": 53,
                "rr": "1:2",
                "us_time": "2026-04-17 09:45:00",
                "extra": {"source": "ibkr_compute"},
            },
            {
                "signal_id": "dup_signal",
                "symbol": "DELL",
                "direction": "long",
                "entry": 191.23,
                "stop_loss": 188.4,
                "take_profit": 195.48,
                "shares": 53,
                "rr": "1:2",
                "us_time": "2026-04-17 09:45:00",
                "extra": {"source": "ibkr_compute"},
            },
            {
                "signal_id": "other_signal",
                "symbol": "AAPL",
                "direction": "short",
                "entry": 200.0,
                "stop_loss": 203.0,
                "take_profit": 194.0,
                "shares": 10,
                "rr": "1:2",
                "us_time": "2026-04-17 09:50:00",
                "extra": {"source": "tradingview"},
            },
        ]
        router = SignalRouter(FakePBClient(rows), FakeConfig(), environment="live")

        first_fetch = router.fetch_pending_signals()
        self.assertEqual(["dup_signal", "other_signal"], [item["signal_id"] for item in first_fetch])

        self.assertTrue(router.claim_signal("dup_signal"))
        self.assertFalse(router.claim_signal("dup_signal"))

        second_fetch = router.fetch_pending_signals()
        self.assertEqual(["other_signal"], [item["signal_id"] for item in second_fetch])

        router.release_signal("dup_signal")
        third_fetch = router.fetch_pending_signals()
        self.assertEqual(["dup_signal", "other_signal"], [item["signal_id"] for item in third_fetch])

        router.mark_processed("dup_signal")
        final_fetch = router.fetch_pending_signals()
        self.assertEqual(["other_signal"], [item["signal_id"] for item in final_fetch])


if __name__ == "__main__":
    unittest.main()
