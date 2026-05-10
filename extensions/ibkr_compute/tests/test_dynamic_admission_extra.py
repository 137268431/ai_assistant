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
