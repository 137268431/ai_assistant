import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.snapshot_builder.payload import _build_snapshot_summary, _filter_orders_for_account


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

    def test_snapshot_summary_uses_broker_daily_pnl(self):
        summary = _build_snapshot_summary(
            {
                "AccountCode": {"value": "U13281777"},
                "Currency": {"value": "USD"},
                "DailyPnL": {"value": "-12.34", "currency": "USD"},
                "RealizedPnL": {"value": "-5.00", "currency": "USD"},
                "UnrealizedPnL": {"value": "-7.34", "currency": "USD"},
            },
            "U13281777",
            [],
        )

        self.assertTrue(summary["daily_pnl_available"])
        self.assertEqual(-12.34, summary["daily_pnl"])
        self.assertEqual(-12.34, summary["today_pnl"])
        self.assertEqual(
            {
                "ok": True,
                "net": -12.34,
                "currency": "USD",
                "source": "broker_daily_pnl",
                "raw_field": "DailyPnL",
                "realized": -5.0,
                "unrealized": -7.34,
                "message": "",
            },
            summary["account_today_pnl"],
        )

    def test_snapshot_summary_falls_through_day_pnl_then_pnl(self):
        day_summary = _build_snapshot_summary(
            {"DayPnL": {"value": "4.25", "currency": "USD"}},
            "U13281777",
            [],
        )
        pnl_summary = _build_snapshot_summary(
            {"PnL": {"value": "8.5", "currency": "USD"}},
            "U13281777",
            [],
        )

        self.assertEqual(4.25, day_summary["account_today_pnl"]["net"])
        self.assertEqual("DayPnL", day_summary["account_today_pnl"]["raw_field"])
        self.assertEqual(8.5, pnl_summary["account_today_pnl"]["net"])
        self.assertEqual("PnL", pnl_summary["account_today_pnl"]["raw_field"])

    def test_snapshot_summary_does_not_infer_missing_daily_pnl(self):
        summary = _build_snapshot_summary(
            {
                "EquityWithLoanValue": {"value": "999422.72", "currency": "USD"},
                "PreviousDayEquityWithLoanValue": {"value": "1000000.00", "currency": "USD"},
                "RealizedPnL": {"value": "-556.81", "currency": "USD"},
                "UnrealizedPnL": {"value": "-20.47", "currency": "USD"},
            },
            "U13281777",
            [],
        )

        self.assertFalse(summary["daily_pnl_available"])
        self.assertIsNone(summary["daily_pnl"])
        self.assertIsNone(summary["today_pnl"])
        self.assertEqual("unavailable", summary["account_today_pnl"]["source"])
        self.assertIsNone(summary["account_today_pnl"]["net"])
        self.assertEqual(-556.81, summary["account_today_pnl"]["realized"])
        self.assertEqual(-20.47, summary["account_today_pnl"]["unrealized"])


if __name__ == "__main__":
    unittest.main()
