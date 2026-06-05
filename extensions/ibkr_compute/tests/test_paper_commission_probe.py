import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.validate import run_paper_commission_probe as probe  # noqa: E402


class PaperCommissionProbeHelpersTest(unittest.TestCase):
    def test_build_bracket_prices_keeps_protection_far_from_entry(self):
        self.assertEqual((120.0, 80.0), probe.build_bracket_prices(100, "long"))
        self.assertEqual((80.0, 120.0), probe.build_bracket_prices(100, "short"))

    def test_classifies_fixed_like_commissions(self):
        fills = [
            {"side": "sell", "shares": 1, "price": 400, "trade_value": 400, "commission": 1.008438},
            {"side": "buy", "shares": 1, "price": 399, "trade_value": 399, "commission": 1.000003},
        ]

        result = probe.classify_fees(fills)

        self.assertEqual("fixed_like", result.classification)
        self.assertGreater(result.fixed_like_estimate, 2.0)
        self.assertLess(result.tiered_min_estimate, 1.0)

    def test_classifies_tiered_like_commissions(self):
        fills = [
            {"side": "sell", "shares": 1, "price": 400, "trade_value": 400},
            {"side": "buy", "shares": 1, "price": 399, "trade_value": 399},
        ]
        tiered_total = sum(probe.tiered_min_commission(fill) for fill in fills)
        fills[0]["commission"] = tiered_total / 2
        fills[1]["commission"] = tiered_total / 2

        result = probe.classify_fees(fills)

        self.assertEqual("tiered_like", result.classification)

    def test_flat_detection_ignores_flat_rows_and_terminal_orders(self):
        snapshot = {
            "positions": [{"symbol": "TSLA", "quantity": 0}],
            "live_open_orders": [{"symbol": "TSLA", "order_id": "1", "status": "Filled"}],
            "orders": [{"symbol": "AAPL", "order_id": "2", "status": "Submitted"}],
        }

        self.assertTrue(probe.is_flat_for_symbol(snapshot, "TSLA"))

    def test_preflight_rejects_non_paper_snapshot(self):
        with self.assertRaisesRegex(probe.ProbeError, "refusing_non_paper_environment"):
            probe.check_clean_preflight({"ok": True, "environment": "live", "broker_mode": "live"}, "TSLA")

    def test_preflight_rejects_existing_position(self):
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [{"symbol": "TSLA", "quantity": 1}],
            "live_open_orders": [],
        }

        with self.assertRaisesRegex(probe.ProbeError, "pre_existing_position"):
            probe.check_clean_preflight(snapshot, "TSLA")

    def test_preflight_rejects_existing_open_order(self):
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [{"symbol": "TSLA", "quantity": 0}],
            "live_open_orders": [{"symbol": "TSLA", "order_id": "10", "status": "Submitted"}],
        }

        with self.assertRaisesRegex(probe.ProbeError, "pre_existing_open_order"):
            probe.check_clean_preflight(snapshot, "TSLA")

    def test_close_payload_uses_absolute_position_and_order_linkage(self):
        snapshot = {
            "positions": [
                {
                    "symbol": "TSLA",
                    "quantity": -1,
                    "conid": 76792991,
                    "avg_price": 400.0,
                }
            ]
        }
        place_response = {
            "signal_id": "probe-signal",
            "result": {
                "trade_group_id": "probe-group",
                "entry_coid": "entry_probe-group",
            },
        }

        payload = probe.close_payload_from_snapshot(
            snapshot,
            symbol="TSLA",
            direction="short",
            quantity=1,
            place_result=place_response,
        )

        self.assertEqual(1, payload["quantity"])
        self.assertEqual(76792991, payload["conid"])
        self.assertEqual("probe-group", payload["trade_group_id"])
        self.assertEqual("entry_probe-group", payload["entry_order_unique_id"])
        self.assertEqual("probe-signal", payload["signal_id"])


if __name__ == "__main__":
    unittest.main()
