import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway import _IBGatewayApp, _PendingRequest
from ibkr_compute.market.data_backfill import DataBackfill


class _FakeBroker:
    def __init__(self, bars):
        self._bars = list(bars or [])

    def request_historical_bars(self, **_kwargs):
        return list(self._bars)


class HistoricalRequestFormatDateTest(unittest.TestCase):
    def test_request_historical_bars_uses_epoch_format(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app.connect_and_start = mock.Mock(return_value=True)
        app.request_contract_details = mock.Mock(
            return_value=[
                {
                    "conid": 121665622,
                    "symbol": "ZTS",
                    "sec_type": "STK",
                    "exchange": "SMART",
                    "currency": "USD",
                }
            ]
        )
        app._next_request = mock.Mock(return_value=(1001, _PendingRequest(kind="historical")))
        app._await = mock.Mock(return_value=[])
        app.reqHistoricalData = mock.Mock()

        app.request_historical_bars(
            conid=121665622,
            symbol="ZTS",
            duration="1 D",
            bar_size="5 mins",
            end_datetime="",
            use_rth=False,
            timeout=30,
        )

        self.assertTrue(app.reqHistoricalData.called)
        self.assertEqual(app.reqHistoricalData.call_args.args[7], 2)


class BackfillFutureGuardTest(unittest.TestCase):
    def test_fetch_history_drops_unsafe_future_5m_bars(self):
        broker = _FakeBroker(
            [
                {"t": 1776360900, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10},
                {"t": 1776361200, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2, "v": 12},
                {"t": 1776361500, "o": 1.2, "h": 1.4, "l": 1.1, "c": 1.3, "v": 15},
                {"t": 1776375900, "o": 1.3, "h": 1.5, "l": 1.2, "c": 1.4, "v": 18},
            ]
        )
        backfill = DataBackfill(
            data_writer=None,
            config=None,
            environment="live",
            broker=broker,
        )

        with mock.patch("ibkr_compute.market.data_backfill.time.time", return_value=1776361717.325):
            rows = backfill.fetch_history(
                121665622,
                "ZTS",
                interval="5m",
                exchange="BATS",
                repair=True,
                request_period="1d",
            )

        self.assertEqual(
            [row["us_time"] for row in rows],
            [
                "2026-04-16 13:35:00",
                "2026-04-16 13:40:00",
            ],
        )


if __name__ == "__main__":
    unittest.main()
