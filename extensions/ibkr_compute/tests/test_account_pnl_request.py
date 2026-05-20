import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway import _IBGatewayApp


class AccountPnlRequestTest(unittest.TestCase):
    def test_request_account_pnl_returns_first_callback_and_cancels(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={})
        app._managed_accounts = "U13281777"
        app.cancelPnL = mock.Mock()

        def _emit_pnl(req_id, account, model_code):
            self.assertEqual("U13281777", account)
            self.assertEqual("", model_code)
            app.pnl(req_id, -12.34, -7.34, -5.0)

        app.reqPnL = mock.Mock(side_effect=_emit_pnl)

        payload = app.request_account_pnl(timeout=1)

        self.assertEqual("U13281777", payload["account"])
        self.assertEqual(-12.34, payload["daily_pnl"])
        self.assertEqual(-7.34, payload["unrealized_pnl"])
        self.assertEqual(-5.0, payload["realized_pnl"])
        self.assertEqual("reqPnL", payload["source"])
        app.cancelPnL.assert_called_once()

    def test_request_account_pnl_filters_ibkr_unset_double(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={})
        app.cancelPnL = mock.Mock()

        def _emit_pnl(req_id, _account, _model_code):
            app.pnl(req_id, 1.7976931348623157e308, 1.7976931348623157e308, 0.0)

        app.reqPnL = mock.Mock(side_effect=_emit_pnl)

        payload = app.request_account_pnl(account="U13281777", timeout=1)

        self.assertIsNone(payload["daily_pnl"])
        self.assertIsNone(payload["unrealized_pnl"])
        self.assertEqual(0.0, payload["realized_pnl"])
        app.cancelPnL.assert_called_once()


if __name__ == "__main__":
    unittest.main()
