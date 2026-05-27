import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.snapshot_builder.fetch import fetch_snapshot_sources


class _Lifecycle:
    def __init__(self):
        self.pnl_calls = 0
        self.positions_calls = 0

    def get_account_snapshot(self, _account_id):
        return {
            "summary": {"NetLiquidation": {"value": "1000000", "currency": "USD"}},
            "positions": [],
        }

    def get_account_pnl(self, _account_id):
        self.pnl_calls += 1
        return {"ok": True, "daily_pnl": 12.34, "source": "reqPnL"}

    def get_positions(self, _account_id):
        self.positions_calls += 1
        raise AssertionError("empty account snapshot positions should not fall back")


class _Service:
    def __init__(self):
        self.order_lifecycle = _Lifecycle()


class AccountSnapshotFetchTest(unittest.TestCase):
    def test_include_pnl_false_skips_pnl_fetcher(self):
        service = _Service()

        payload = fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual(0, service.order_lifecycle.pnl_calls)
        self.assertEqual("", payload["errors"]["pnl"])
        self.assertEqual({}, payload["pnl_raw"])

    def test_empty_snapshot_positions_do_not_trigger_fallback(self):
        service = _Service()

        payload = fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual([], payload["positions_raw"])
        self.assertEqual(0, service.order_lifecycle.positions_calls)
        self.assertEqual("", payload["errors"]["positions"])


if __name__ == "__main__":
    unittest.main()
