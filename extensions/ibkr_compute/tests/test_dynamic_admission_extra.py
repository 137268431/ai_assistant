import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.universe.dynamic_admission import evaluate_dynamic_admission
from ibkr_compute.workflows.daily_scanner_run import DailyScannerRunMixin


class _PB:
    def __init__(self, records):
        self.records = list(records)

    def get_all_records(self, *args, **kwargs):
        return list(self.records)


class _Scanner(DailyScannerRunMixin):
    def __init__(self, records):
        self.pb_client = _PB(records)


class DynamicAdmissionExtraTest(unittest.TestCase):
    def test_high_price_mega_cap_can_qualify_by_dollar_volume(self):
        admission = evaluate_dynamic_admission(
            "AMZN",
            metrics={
                "price": 185,
                "avg_10d_volume": 60_000,
                "premarket_volume": 8_000,
                "today_volume": 100_000,
                "atr_pct": 0.5,
                "day_change_pct": 1.2,
                "market_cap": 2_000_000_000_000,
            },
        )

        thresholds = admission["dynamic_thresholds"]
        gate_buckets = {gate["bucket"] for gate in admission["failed_gates"]}
        self.assertTrue(admission["quality_gate_passed"])
        self.assertEqual("mega_core", thresholds["threshold_profile"])
        self.assertLess(thresholds["avg_10d_volume_gte"], 100_000)
        self.assertGreaterEqual(thresholds["avg_dollar_volume_gte"], 10_000_000)
        self.assertNotIn("avg_10d_volume_below_threshold", gate_buckets)
        self.assertIn("threshold_profile=mega_core", admission["reason_tags"])

    def test_active_growth_symbol_uses_dynamic_dollar_volume_and_activity(self):
        admission = evaluate_dynamic_admission(
            "APP",
            metrics={
                "price": 80,
                "avg_10d_volume": 80_000,
                "premarket_volume": 3_000,
                "today_volume": 50_000,
                "rvol_20": 2.0,
                "atr_pct": 2.5,
                "day_change_pct": 3.0,
                "market_cap": 30_000_000_000,
                "beta": 1.6,
            },
        )

        thresholds = admission["dynamic_thresholds"]
        self.assertTrue(admission["quality_gate_passed"])
        self.assertEqual("mid_active", thresholds["threshold_profile"])
        self.assertGreater(thresholds["avg_10d_volume_gte"], 80_000)
        self.assertLessEqual(thresholds["activity_any_of"]["premarket_volume_gte"], 3_000)
        self.assertNotIn("avg_10d_volume_below_threshold", {gate["bucket"] for gate in admission["failed_gates"]})

    def test_low_float_hot_symbol_is_allowed_but_gets_strict_policy(self):
        admission = evaluate_dynamic_admission(
            "LOWF",
            metrics={
                "price": 10,
                "avg_10d_volume": 45_000,
                "premarket_volume": 3_500,
                "today_volume": 50_000,
                "rvol_20": 3.0,
                "atr_pct": 4.0,
                "day_change_pct": 5.0,
                "market_cap": 600_000_000,
                "shares_float": 10_000_000,
            },
        )

        self.assertTrue(admission["quality_gate_passed"])
        self.assertEqual("low_float_hot", admission["dynamic_thresholds"]["threshold_profile"])
        self.assertGreater(admission["dynamic_thresholds"]["admission_score_gte"], 58)
        self.assertEqual("aggressive", admission["strategy_policy"]["risk_profile"])
        self.assertEqual("strict", admission["strategy_policy"]["signal_confirmation"])

    def test_inactive_thin_or_penny_symbol_stays_blocked(self):
        admission = evaluate_dynamic_admission(
            "BAD",
            metrics={
                "price": 0.75,
                "avg_10d_volume": 15_000,
                "premarket_volume": 100,
                "today_volume": 200,
                "atr_pct": 0.02,
                "day_change_pct": 0.05,
                "market_cap": 50_000_000,
            },
        )

        self.assertFalse(admission["quality_gate_passed"])
        self.assertEqual("thin_or_penny", admission["dynamic_thresholds"]["threshold_profile"])
        self.assertIn("admission_score_below_threshold", {gate["bucket"] for gate in admission["failed_gates"]})

    def test_zero_price_is_a_blocking_quality_failure(self):
        admission = evaluate_dynamic_admission(
            "NOPX",
            metrics={
                "price": 0,
                "avg_10d_volume": 2_000_000,
                "premarket_volume": 50_000,
                "today_volume": 200_000,
                "atr_pct": 1.5,
                "day_change_pct": 2.0,
            },
        )

        price_gates = [gate for gate in admission["failed_gates"] if gate["bucket"] == "price_unusable"]
        self.assertFalse(admission["quality_gate_passed"])
        self.assertEqual("blocking", price_gates[0]["severity"])

    def test_extra_is_used_only_after_metrics_and_top_level_fundamentals(self):
        admission = evaluate_dynamic_admission(
            "XYZ",
            metrics={
                "price": 10,
                "premarket_volume": 25_000,
                "today_volume": 125_000,
                "atr_pct": 2.5,
                "day_change_pct": 3.0,
                "shares_float": 30_000_000,
                "sector": "Metrics Sector",
            },
            fundamentals={
                "shares_float": 40_000_000,
                "sector": "Top Sector",
                "extra": {
                    "avg_volume_10d_provider": 1_200_000,
                    "shares_float": 5_000_000,
                    "sector": "Extra Sector",
                    "country": "us",
                    "short_float_pct": 0.12,
                    "beta": 1.4,
                },
            },
            settings={"dynamic_admission_min_score": 0},
        )
        profile = admission["symbol_profile"]
        self.assertEqual(1_200_000, profile["avg_10d_volume"])
        self.assertEqual(30_000_000, profile["shares_float"])
        self.assertEqual("Metrics Sector", profile["sector"])
        self.assertEqual("US", profile["country"])
        self.assertEqual(12.0, profile["short_float_pct"])
        self.assertEqual(1.4, profile["beta"])
        self.assertEqual("normal_float", profile["float_profile"])

    def test_daily_scanner_normalizes_extra_without_overwriting_top_level(self):
        records = [
            {"symbol": "BAD", "provider": "finnhub", "status": "failed", "extra": {"sector": "Ignored"}},
            {
                "symbol": "XYZ",
                "provider": "finnhub",
                "status": "fresh",
                "sector": "Top Sector",
                "exchange": "nasdaq",
                "extra": {
                    "sector": "Extra Sector",
                    "country": "us",
                    "shares_float": 55_000_000,
                    "short_float_pct": 8.5,
                    "beta": 1.2,
                    "avg_volume_10d_provider": 900_000,
                },
            },
        ]
        result = _Scanner(records)._load_fundamentals_by_symbol(["BAD", "XYZ"])
        self.assertNotIn("BAD", result)
        normalized = result["XYZ"]
        self.assertEqual("Top Sector", normalized["sector"])
        self.assertEqual("US", normalized["country"])
        self.assertEqual(55_000_000, normalized["shares_float"])
        self.assertEqual(8.5, normalized["short_float_pct"])
        self.assertEqual(1.2, normalized["beta"])
        self.assertEqual(900_000, normalized["avg_volume_10d_provider"])


if __name__ == "__main__":
    unittest.main()
