import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.snapshot_builder.payload import _filter_orders_for_account


class AccountSnapshotOrderFilteringTest(unittest.TestCase):
    def test_filter_orders_for_account_drops_other_account_orders(self):
        filtered = _filter_orders_for_account(
            "U13281777",
            [
                {"orderId": "1", "status": "Submitted", "account": "U13281777", "ticker": "XOM"},
                {"orderId": "9", "status": "Submitted", "account": "U18316222", "ticker": "AAPL"},
                {"orderId": "5", "status": "Submitted", "account": "", "ticker": "MSFT"},
            ],
        )

        self.assertEqual(["1", "5"], [str(item.get("orderId") or "") for item in filtered])


if __name__ == "__main__":
    unittest.main()
