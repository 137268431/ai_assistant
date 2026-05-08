import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.ws_client import IBKRWebSocketClient


class FakeBroker:
    def __init__(self):
        self.market_data_listeners = []
        self.order_update_listeners = []
        self.subscribed_conids = []
        self.unsubscribed_conids = []

    def add_market_data_listener(self, callback):
        self.market_data_listeners.append(callback)

    def remove_market_data_listener(self, callback):
        if callback in self.market_data_listeners:
            self.market_data_listeners.remove(callback)

    def add_order_update_listener(self, callback):
        self.order_update_listeners.append(callback)

    def remove_order_update_listener(self, callback):
        if callback in self.order_update_listeners:
            self.order_update_listeners.remove(callback)

    def connect(self):
        return True

    def subscribe_market_data(self, conid: int, symbol: str = "", exchange: str = "SMART"):
        self.subscribed_conids.append(int(conid))
        return int(conid)

    def unsubscribe_market_data(self, conid: int):
        self.unsubscribed_conids.append(int(conid))


class IBKRWebSocketClientTest(unittest.TestCase):
    def test_start_flush_clears_pending_subscriptions_after_success(self):
        broker = FakeBroker()
        client = IBKRWebSocketClient(broker=broker)
        client.subscribe(265598)

        self.assertEqual(1, client.status()["pending_count"])

        client.start()
        status = client.status()

        self.assertEqual(0, status["pending_count"])
        self.assertEqual([], status["pending_conids"])
        self.assertEqual([265598], status["subscribed_conids"])
        self.assertEqual([265598], broker.subscribed_conids)

        client.stop()

    def test_send_subscription_clears_pending_state_when_already_subscribed(self):
        broker = FakeBroker()
        client = IBKRWebSocketClient(broker=broker)
        client._pending_subscriptions.add(272093)
        client._subscribed_conids.add(272093)

        client._send_subscription(272093)

        status = client.status()
        self.assertEqual(0, status["pending_count"])
        self.assertEqual([], status["pending_conids"])
        self.assertEqual([], broker.subscribed_conids)

    def test_resubscribe_refreshes_already_subscribed_conid(self):
        broker = FakeBroker()
        client = IBKRWebSocketClient(broker=broker)
        client.start()
        client.subscribe(756733)
        broker.subscribed_conids.clear()

        client.resubscribe(756733)

        status = client.status()
        self.assertEqual([756733], broker.unsubscribed_conids)
        self.assertEqual([756733], broker.subscribed_conids)
        self.assertEqual([756733], status["subscribed_conids"])
        self.assertEqual(0, status["pending_count"])


if __name__ == "__main__":
    unittest.main()
