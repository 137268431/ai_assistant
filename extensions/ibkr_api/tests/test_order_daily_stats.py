import sys
import unittest
from pathlib import Path


SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.orders.daily_stats import build_daily_order_stats


class OrderDailyStatsTest(unittest.TestCase):
    def test_counts_filled_take_profit_and_uses_stored_pnl_minus_commission(self):
        rows = [
            {
                "id": "entry-1",
                "unique_id": "entry-1",
                "trade_group_id": "tg-1",
                "signal_id": "sig-1",
                "role": "entry",
                "order_type": "Entry",
                "status": "Filled",
                "position_side": "long",
                "fill_price": 100,
                "filled_qty": 10,
                "commission": 1.0,
            },
            {
                "id": "tp-1",
                "unique_id": "tp-1",
                "trade_group_id": "tg-1",
                "entry_order_unique_id": "entry-1",
                "signal_id": "sig-1",
                "role": "take_profit",
                "order_type": "TakeProfit",
                "status": "Filled",
                "fill_price": 105,
                "filled_qty": 10,
                "pnl": 50,
                "commission": 0.5,
            },
            {
                "id": "sl-1",
                "unique_id": "sl-1",
                "trade_group_id": "tg-1",
                "entry_order_unique_id": "entry-1",
                "role": "stop_loss",
                "order_type": "StopLoss",
                "status": "Canceled",
            },
        ]

        stats = build_daily_order_stats(rows)

        self.assertEqual(stats["take_profit_filled"], 1)
        self.assertEqual(stats["stop_loss_filled"], 0)
        self.assertEqual(stats["winning_trades"], 1)
        self.assertEqual(stats["realized_gross_pnl"], 50.0)
        self.assertEqual(stats["realized_net_pnl"], 48.5)
        self.assertEqual(stats["profit_amount"], 48.5)
        self.assertEqual(stats["commission"], 1.5)
        self.assertEqual(stats["pnl_missing_count"], 0)

    def test_computes_stop_loss_pnl_when_stored_pnl_is_missing(self):
        rows = [
            {
                "id": "entry-1",
                "unique_id": "entry-1",
                "trade_group_id": "tg-1",
                "role": "entry",
                "order_type": "Entry",
                "status": "Filled",
                "position_side": "short",
                "fill_price": 100,
                "filled_qty": 5,
            },
            {
                "id": "sl-1",
                "unique_id": "sl-1",
                "trade_group_id": "tg-1",
                "entry_order_unique_id": "entry-1",
                "role": "stop_loss",
                "order_type": "StopLoss",
                "status": "Filled",
                "fill_price": 104,
                "filled_qty": 5,
                "commission": 1,
            },
        ]

        stats = build_daily_order_stats(rows)

        self.assertEqual(stats["take_profit_filled"], 0)
        self.assertEqual(stats["stop_loss_filled"], 1)
        self.assertEqual(stats["losing_trades"], 1)
        self.assertEqual(stats["realized_gross_pnl"], -20.0)
        self.assertEqual(stats["realized_net_pnl"], -21.0)
        self.assertEqual(stats["loss_amount"], -21.0)
        self.assertEqual(stats["pnl_missing_count"], 0)

    def test_tracks_missing_pnl_for_uncomputable_filled_exit(self):
        rows = [
            {
                "id": "tp-1",
                "unique_id": "tp-1",
                "role": "take_profit",
                "order_type": "TakeProfit",
                "status": "Filled",
            },
            {
                "id": "sl-1",
                "unique_id": "sl-1",
                "role": "stop_loss",
                "order_type": "StopLoss",
                "status": "Submitted",
            },
        ]

        stats = build_daily_order_stats(rows)

        self.assertEqual(stats["take_profit_filled"], 1)
        self.assertEqual(stats["stop_loss_filled"], 0)
        self.assertEqual(stats["pnl_missing_count"], 1)
        self.assertEqual(stats["winning_trades"], 0)
        self.assertEqual(stats["losing_trades"], 0)
        self.assertEqual(stats["realized_net_pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
