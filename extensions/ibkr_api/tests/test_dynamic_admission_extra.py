import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_api.universe.dynamic_admission import build_admission_profile


class ApiDynamicAdmissionExtraTest(unittest.TestCase):
    def test_extra_fields_fallback_without_overwriting_top_level_profile(self):
        profile = build_admission_profile(
            symbol="XYZ",
            fundamentals={
                "provider": "finnhub",
                "market_cap_usd": 3_000_000_000,
                "sector": "Top Sector",
                "extra": {
                    "market_cap_usd": 100_000_000,
                    "sector": "Extra Sector",
                    "country": "us",
                    "shares_float": 45_000_000,
                    "short_float_pct": 0.08,
                    "beta": 1.1,
                    "avg_volume_10d_provider": 800_000,
                },
            },
        )
        self.assertEqual("mid_cap", profile["name"])
        self.assertEqual(3_000_000_000, profile["market_cap_usd"])
        self.assertEqual("Top Sector", profile["sector"])
        self.assertEqual("us", profile["country"])
        self.assertEqual(45_000_000, profile["shares_float"])
        self.assertEqual(0.08, profile["short_float_pct"])
        self.assertEqual(1.1, profile["beta"])
        self.assertEqual(800_000, profile["avg_volume_10d_provider"])

    def test_non_object_extra_is_ignored(self):
        profile = build_admission_profile(
            symbol="XYZ",
            fundamentals={"provider": "finnhub", "extra": "not-a-dict"},
            latest_row={"price": 10, "avg_10d_volume": 1_500_000},
        )
        self.assertEqual("standard_liquidity", profile["name"])
        self.assertIsNone(profile["shares_float"])


if __name__ == "__main__":
    unittest.main()
