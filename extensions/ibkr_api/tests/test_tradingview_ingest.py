import copy
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.tradingview.ingest import normalize_risk_reward_value, upsert_tv_indicator, upsert_tv_signal


class _FakePB:
    def __init__(self, existing=None):
        self.existing = copy.deepcopy(existing)
        self.created = []
        self.lookup_filters = []

    def get_first_record(self, collection, filter=None, sort=None):
        self.lookup_filters.append((collection, str(filter or "")))
        return copy.deepcopy(self.existing)

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = f"{collection}-{len(self.created) + 1}"
        self.created.append((collection, row))
        return copy.deepcopy(row)


class TradingViewIngestTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')
        self.time_strings = lambda: {"date": "2026-04-23"}

    def test_normalize_risk_reward_value_falls_back_to_price_ratio(self):
        value = normalize_risk_reward_value("", 100, 95, 110)

        self.assertEqual(value, "2.00")

    def test_upsert_tv_indicator_maps_aliases_and_creates_record(self):
        pb = _FakePB()

        response = upsert_tv_indicator(
            {
                "symbol": "aapl",
                "interval": "5m",
                "bar_time_ms": "1713859200000",
                "environment": "paper",
                "extra": '{"dayChangePct":"1.5","bar_index":"7","exchange":"nasdaq"}',
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["ok"], True)
        self.assertEqual(len(pb.created), 1)
        collection, row = pb.created[0]
        self.assertEqual(collection, "tv_indicators")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["environment"], "paper")
        self.assertEqual(row["bar_index"], 7)
        self.assertEqual(row["extra"]["dayChangePct"], 1.5)
        self.assertEqual(row["extra"]["day_change_pct"], 1.5)
        self.assertEqual(row["extra"]["source"], "tradingview")

    def test_upsert_tv_indicator_skips_duplicates(self):
        pb = _FakePB(existing={"id": "tv-ind-1"})

        response = upsert_tv_indicator(
            {
                "symbol": "AAPL",
                "interval": "5m",
                "bar_time_ms": 1713859200000,
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["msg"], "duplicate indicator, skipped")
        self.assertEqual(pb.created, [])

    def test_upsert_tv_signal_creates_record_with_rr_and_date(self):
        pb = _FakePB()

        response = upsert_tv_signal(
            {
                "symbol": "AAPL",
                "direction": "Long",
                "entry": 100,
                "stop_loss": 95,
                "take_profit": 110,
                "signal_id": "sig-1",
                "us_time": "2026-04-23 09:35:00",
                "extra": {"reason": "breakout", "bar_time_ms": 1713859200000, "chart_tf": "5m"},
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
            time_strings=self.time_strings,
        )

        self.assertEqual(response["created_environments"], ["live"])
        self.assertEqual(len(pb.created), 1)
        collection, row = pb.created[0]
        self.assertEqual(collection, "tv_signals")
        self.assertEqual(row["rr"], "2.00")
        self.assertEqual(row["date"], "2026-04-23")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["extra"]["environment"], "live")
        self.assertEqual(row["extra"]["source"], "tradingview")

    def test_upsert_tv_signal_rejects_invalid_direction(self):
        pb = _FakePB()

        response, status_code = upsert_tv_signal(
            {
                "symbol": "AAPL",
                "direction": "sideways",
                "entry": 100,
                "stop_loss": 95,
                "take_profit": 110,
                "signal_id": "sig-1",
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
            time_strings=self.time_strings,
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(response["error"], "Invalid direction: must be 'long' or 'short'")


if __name__ == "__main__":
    unittest.main()
