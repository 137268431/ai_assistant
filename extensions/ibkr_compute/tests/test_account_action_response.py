import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.action_builders import common


class AccountActionResponseTest(unittest.TestCase):
    def test_action_response_uses_fast_snapshot_for_order_actions(self):
        class _Tracker:
            def get_cached_live_orders(self, *, include_all=False):
                self.include_all = include_all
                return [
                    {"orderId": "101", "status": "Submitted"},
                    {"orderId": "102", "status": "Cancelled"},
                ]

        class _Service:
            environment = "paper"
            order_tracker = _Tracker()

        with mock.patch.object(common, "_build_ibkr_account_snapshot", return_value={"ok": True}) as full_snapshot:
            payload, status = common._build_snapshot_action_response(
                _Service(),
                "cancel_all_orders",
                {"ok": True},
            )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        full_snapshot.assert_not_called()
        self.assertEqual("account_action_orders_fast", payload["snapshot"]["source"])
        self.assertEqual(2, payload["snapshot"]["counts"]["orders"])
        self.assertEqual(1, payload["snapshot"]["counts"]["open_orders"])

    def test_action_response_can_skip_snapshot_for_stress_cleanup(self):
        service = object()
        with mock.patch.object(common, "_build_ibkr_account_snapshot", return_value={"ok": True}) as full_snapshot, mock.patch.object(
            common,
            "_build_fast_action_snapshot",
            return_value={"ok": True},
        ) as fast_snapshot:
            payload, status = common._build_snapshot_action_response(
                service,
                "cancel_order",
                {"ok": True},
                include_snapshot=False,
            )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        full_snapshot.assert_not_called()
        fast_snapshot.assert_not_called()
        self.assertEqual("account_action_snapshot_skipped", payload["snapshot"]["source"])
        self.assertTrue(payload["snapshot"]["snapshot_skipped"])

    def test_batch_cancel_uses_fast_snapshot(self):
        class _Tracker:
            def get_cached_live_orders(self, *, include_all=False):
                return [{"orderId": "201", "status": "PreSubmitted"}]

        class _Service:
            environment = "paper"
            order_tracker = _Tracker()

        with mock.patch.object(common, "_build_ibkr_account_snapshot", return_value={"ok": True}) as full_snapshot:
            payload, status = common._build_snapshot_action_response(
                _Service(),
                "cancel_order_ids",
                {"ok": True, "submitted": 3},
            )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        full_snapshot.assert_not_called()
        self.assertEqual("account_action_orders_fast", payload["snapshot"]["source"])


if __name__ == "__main__":
    unittest.main()
