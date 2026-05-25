from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ibkr_compute.broker.ib_gateway import BrokerAdapter, _IBGatewayApp  # noqa: E402
from ibkr_compute.market.ws_client import IBKRWebSocketClient  # noqa: E402


class FakeIBClient:
    def __init__(self):
        self._state_lock = threading.RLock()
        self._ticker_seq = 8000
        self._tick_by_tick_meta = {}
        self._tick_by_tick_by_conid = {}
        self._tick_by_tick_last_request_at = {}
        self.req_tick_calls = []
        self.cancel_tick_calls = []

    def next_ticker_ids(self, count: int):
        start = self._ticker_seq
        self._ticker_seq += int(count)
        return list(range(start, start + int(count)))

    def reqTickByTickData(self, req_id, contract, tick_type, number_of_ticks, ignore_size):  # noqa: N802
        self.req_tick_calls.append(
            {
                "req_id": req_id,
                "contract": contract,
                "tick_type": tick_type,
                "number_of_ticks": number_of_ticks,
                "ignore_size": ignore_size,
            }
        )

    def cancelTickByTickData(self, req_id):  # noqa: N802
        self.cancel_tick_calls.append(req_id)


class TestableBroker(BrokerAdapter):
    def __init__(self):
        self.client = FakeIBClient()

    def connect(self) -> bool:
        return True

    def resolve_contract(self, symbol: str = "", conid: int = 0, exchange: str = "", sec_type: str = ""):
        return {
            "conid": int(conid),
            "symbol": str(symbol or "TST").upper(),
            "sec_type": str(sec_type or "STK").upper(),
            "exchange": str(exchange or "SMART"),
            "currency": "USD",
        }


class FakeMarketBroker:
    def __init__(self):
        self.tick_subscribe_calls = []
        self.tick_unsubscribe_calls = []
        self.market_listeners = []
        self.order_listeners = []

    def connect(self):
        return True

    def add_market_data_listener(self, callback):
        self.market_listeners.append(callback)

    def remove_market_data_listener(self, callback):
        if callback in self.market_listeners:
            self.market_listeners.remove(callback)

    def add_order_update_listener(self, callback):
        self.order_listeners.append(callback)

    def remove_order_update_listener(self, callback):
        if callback in self.order_listeners:
            self.order_listeners.remove(callback)

    def unsubscribe_market_data(self, conid):
        pass

    def subscribe_tick_by_tick(self, conid: int, symbol: str = "", tick_type: str = "Last"):
        self.tick_subscribe_calls.append({"conid": conid, "symbol": symbol, "tick_type": tick_type})
        return 9000 + int(conid)

    def unsubscribe_tick_by_tick(self, conid: int):
        self.tick_unsubscribe_calls.append(int(conid))


class TickByTickBrokerTests(unittest.TestCase):
    def test_subscribe_tracks_active_request_and_cancels(self):
        broker = TestableBroker()

        req_id = broker.subscribe_tick_by_tick(123, symbol="ABC")

        self.assertEqual(req_id, 8000)
        self.assertEqual(len(broker.client.req_tick_calls), 1)
        call = broker.client.req_tick_calls[0]
        self.assertEqual(call["tick_type"], "Last")
        self.assertEqual(call["number_of_ticks"], 0)
        self.assertFalse(call["ignore_size"])
        self.assertEqual(call["contract"].conId, 123)
        self.assertEqual(broker.list_tick_by_tick_subscriptions()[0]["conid"], 123)

        duplicate_req_id = broker.subscribe_tick_by_tick(123, symbol="ABC")
        self.assertEqual(duplicate_req_id, req_id)
        self.assertEqual(len(broker.client.req_tick_calls), 1)

        broker.unsubscribe_tick_by_tick(123)
        self.assertEqual(broker.client.cancel_tick_calls, [req_id])
        self.assertEqual(broker.list_tick_by_tick_subscriptions(), [])
        with self.assertRaisesRegex(RuntimeError, "tick_by_tick_duplicate_cooldown"):
            broker.subscribe_tick_by_tick(123, symbol="ABC")

    def test_tick_callbacks_emit_listener_payload_without_changing_req_mkt_data_source(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 1)
        payloads = []
        app.add_market_data_listener(payloads.append)

        app._ticker_meta[11] = {"tickerId": 11, "conid": 456, "symbol": "MKT"}
        app._ticker_payloads[11] = dict(app._ticker_meta[11])
        app._emit_tick(11)
        self.assertNotIn("source", payloads[-1])

        app._tick_by_tick_meta[22] = {
            "reqId": 22,
            "conid": 789,
            "conidEx": 789,
            "symbol": "TBT",
            "tick_type": "Last",
        }
        app.tickByTickAllLast(22, 1, 1_700_000_000, 101.25, 50, object(), "NYSE", "")
        tick_payload = payloads[-1]
        self.assertEqual(tick_payload["source"], "tick_by_tick")
        self.assertEqual(tick_payload["tick_type"], "Last")
        self.assertEqual(tick_payload["conid"], 789)
        self.assertEqual(tick_payload["31"], 101.25)
        self.assertEqual(tick_payload["7059"], 50)
        self.assertEqual(tick_payload["timestamp_ms"], 1_700_000_000_000)


class TickByTickMarketClientTests(unittest.TestCase):
    def test_ws_client_tracks_tick_by_tick_pending_active_and_unsubscribe(self):
        broker = FakeMarketBroker()
        client = IBKRWebSocketClient(broker=broker)

        client.subscribe_tick_by_tick(321)
        self.assertEqual(client.status()["tick_by_tick_pending_count"], 1)
        self.assertEqual(broker.tick_subscribe_calls, [])

        client.start()
        status = client.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["tick_by_tick_pending_count"], 0)
        self.assertEqual(status["tick_by_tick_subscribed_count"], 1)
        self.assertEqual(broker.tick_subscribe_calls, [{"conid": 321, "symbol": "", "tick_type": "Last"}])

        client.subscribe_tick_by_tick(321)
        self.assertEqual(len(broker.tick_subscribe_calls), 1)

        client.unsubscribe_tick_by_tick(321)
        status = client.status()
        self.assertEqual(status["tick_by_tick_subscribed_count"], 0)
        self.assertEqual(broker.tick_unsubscribe_calls, [321])


if __name__ == "__main__":
    unittest.main()
