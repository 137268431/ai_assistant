import sys
import unittest
from pathlib import Path
from unittest import mock


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway import _IBGatewayApp  # noqa: E402


class CancelOrderCompatTest(unittest.TestCase):
    def test_cancel_open_order_uses_order_cancel_for_newer_ibapi(self):
        class FakeOrderCancel:
            pass

        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.cancelOrder = mock.Mock()

        with mock.patch("ibkr_compute.broker.ib_gateway.OrderCancel", FakeOrderCancel):
            app.cancel_open_order("218")

        app.cancelOrder.assert_called_once()
        order_id, cancel_payload = app.cancelOrder.call_args.args
        self.assertEqual(218, order_id)
        self.assertIsInstance(cancel_payload, FakeOrderCancel)

    def test_cancel_open_order_falls_back_to_legacy_signature(self):
        class FakeOrderCancel:
            pass

        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        calls = []

        def cancel_order(*args):
            calls.append(args)
            if len(args) == 2:
                raise TypeError("cancelOrder() takes 2 positional arguments but 3 were given")

        app.cancelOrder = cancel_order

        with mock.patch("ibkr_compute.broker.ib_gateway.OrderCancel", FakeOrderCancel):
            app.cancel_open_order("219")

        self.assertEqual(2, len(calls))
        self.assertEqual(219, calls[0][0])
        self.assertIsInstance(calls[0][1], FakeOrderCancel)
        self.assertEqual((219,), calls[1])


if __name__ == "__main__":
    unittest.main()
