import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import flush as flush_mod


def _indicator(symbol="AAPL", bar_time_ms=1713797100000):
    return {
        "environment": "live",
        "symbol": symbol,
        "exchange": "NASDAQ",
        "interval": "5",
        "script_tag": "ibkr_compute",
        "us_time": "2024-04-22 09:25:00",
        "cn_time": "2024-04-22 21:25:00",
        "bar_time_ms": bar_time_ms,
        "bar_index": 10,
        "extra": {"close": 100.0},
    }


class _FakePB:
    def __init__(self):
        self.batches = []

    def upsert_indicators(self, batch):
        self.batches.append(list(batch))
        return {"ok": True, "success": len(batch), "errors": 0}


class ComputeFlushTest(unittest.TestCase):
    def test_indicator_flush_prefers_direct_sqlite(self):
        pb = _FakePB()
        app = SimpleNamespace(pb=pb, cfg=None, ENVIRONMENT="live")
        direct_batches = []

        def fake_upsert_indicators(_conn, batch):
            direct_batches.append(list(batch))
            return len(batch)

        with mock.patch.object(flush_mod, "_api_app", return_value=app), mock.patch.object(
            flush_mod,
            "open_pb_sqlite",
        ) as open_sqlite, mock.patch.object(flush_mod, "upsert_indicators", side_effect=fake_upsert_indicators):
            conn = mock.MagicMock()
            open_sqlite.return_value.__enter__.return_value = conn
            conn.__enter__.return_value = conn
            result = flush_mod.flush_indicator_batch([_indicator()])

        self.assertTrue(result["ok"])
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["write_path"], "direct_sqlite")
        self.assertEqual(len(direct_batches), 1)
        self.assertEqual(pb.batches, [])

    def test_indicator_flush_falls_back_to_api_when_sqlite_fails(self):
        pb = _FakePB()
        app = SimpleNamespace(pb=pb, cfg=None, ENVIRONMENT="live")

        with mock.patch.object(flush_mod, "_api_app", return_value=app), mock.patch.object(
            flush_mod,
            "open_pb_sqlite",
            side_effect=RuntimeError("sqlite busy"),
        ):
            result = flush_mod.flush_indicator_batch([_indicator()])

        self.assertTrue(result["ok"])
        self.assertEqual(result["written"], 1)
        self.assertEqual(len(pb.batches), 1)


if __name__ == "__main__":
    unittest.main()
