import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway import _IBGatewayApp, _PendingRequest, _ib_timestamp_to_ms
from ibkr_compute.market.data_backfill import DataBackfill


class _FakeBroker:
    def __init__(self, bars):
        self._bars = list(bars or [])
        self.calls = 0

    def request_historical_bars(self, **_kwargs):
        self.calls += 1
        return list(self._bars)


class _FailingBroker:
    def __init__(self, error_text):
        self.error_text = str(error_text)
        self.calls = 0

    def request_historical_bars(self, **_kwargs):
        self.calls += 1
        raise RuntimeError(self.error_text)


class _SequenceBroker:
    def __init__(self, responses):
        self.responses = [list(item or []) for item in (responses or [])]
        self.calls = []

    def request_historical_bars(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.responses:
            return []
        return list(self.responses.pop(0))


def _et_ms(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


def _history_bar(bar_time_ms):
    return {"t": int(bar_time_ms / 1000), "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10}


class HistoricalRequestFormatDateTest(unittest.TestCase):
    def test_daily_history_date_parses_as_session_date(self):
        self.assertEqual(
            _ib_timestamp_to_ms("20240430"),
            _et_ms(2024, 4, 30, 0, 0),
        )

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
    def test_long_5m_history_requests_are_chunked(self):
        first_ms = _et_ms(2020, 1, 15, 9, 30)
        second_ms = _et_ms(2020, 1, 8, 9, 30)
        third_ms = _et_ms(2020, 1, 1, 9, 30)
        broker = _SequenceBroker(
            [
                [_history_bar(first_ms)],
                [_history_bar(second_ms)],
                [_history_bar(third_ms)],
            ]
        )
        backfill = DataBackfill(
            data_writer=None,
            config=None,
            environment="live",
            broker=broker,
        )

        with mock.patch("ibkr_compute.market.data_backfill.time.sleep"):
            rows = backfill.fetch_history(
                121665622,
                "ZTS",
                interval="5m",
                exchange="BATS",
                repair=True,
                request_period="9d",
            )

        self.assertEqual([call["duration"] for call in broker.calls], ["4 D", "4 D", "1 D"])
        self.assertEqual(broker.calls[0]["end_datetime"], "")
        self.assertEqual(broker.calls[1]["end_datetime"], "20200115 09:25:00 US/Eastern")
        self.assertEqual(broker.calls[2]["end_datetime"], "20200108 09:25:00 US/Eastern")
        self.assertEqual(
            [row["us_time"] for row in rows],
            [
                "2020-01-01 09:30:00",
                "2020-01-08 09:30:00",
                "2020-01-15 09:30:00",
            ],
        )
        self.assertEqual({row["extra"]["request_period"] for row in rows}, {"9d"})
        self.assertEqual(
            {row["extra"]["request_chunk_period"] for row in rows},
            {"1d", "4d"},
        )

    def test_long_4h_history_requests_are_chunked_directly_from_ibkr(self):
        first_ms = _et_ms(2020, 1, 15, 12, 0)
        second_ms = _et_ms(2020, 1, 1, 12, 0)
        broker = _SequenceBroker(
            [
                [_history_bar(first_ms)],
                [_history_bar(second_ms)],
            ]
        )
        backfill = DataBackfill(
            data_writer=None,
            config=None,
            environment="live",
            broker=broker,
        )

        with mock.patch("ibkr_compute.market.data_backfill.time.sleep"):
            rows = backfill.fetch_history(
                121665622,
                "ZTS",
                interval="4h",
                exchange="BATS",
                repair=True,
                request_period="240d",
            )

        self.assertEqual([call["duration"] for call in broker.calls], ["120 D", "120 D"])
        self.assertEqual(broker.calls[0]["bar_size"], "4 hours")
        self.assertEqual(broker.calls[0]["end_datetime"], "")
        self.assertEqual(broker.calls[1]["end_datetime"], "20200115 08:00:00 US/Eastern")
        self.assertEqual(
            [row["us_time"] for row in rows],
            [
                "2020-01-01 12:00:00",
                "2020-01-15 12:00:00",
            ],
        )

    def test_history_concurrency_default_and_cap_allow_gateway_parallelism(self):
        backfill = DataBackfill(
            data_writer=None,
            config=None,
            environment="live",
            broker=_FakeBroker([]),
        )

        self.assertEqual(backfill._max_concurrency(), 8)
        self.assertEqual(backfill._request_spacing(), 0.35)
        self.assertEqual(backfill._interval_delay(), 0.10)

        class HighConcurrencyConfig:
            def get_int_for_environment(self, key, environment, fallback):
                del environment
                if key == "ibkr_history_max_concurrency":
                    return 20
                return fallback

            def get_float_for_environment(self, key, environment, fallback):
                del key, environment
                return fallback

        capped = DataBackfill(
            data_writer=None,
            config=HighConcurrencyConfig(),
            environment="live",
            broker=_FakeBroker([]),
        )

        self.assertEqual(capped._max_concurrency(), 10)

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

    def test_terminal_history_errors_do_not_retry(self):
        terminal_errors = [
            "contract_not_found:ZTS",
            "No security definition has been found for the request",
            "Historical Market Data Service error message:HMDS query returned no data: ZTS@SMART Trades",
        ]

        for error_text in terminal_errors:
            with self.subTest(error_text=error_text):
                broker = _FailingBroker(error_text)
                backfill = DataBackfill(
                    data_writer=None,
                    config=None,
                    environment="live",
                    broker=broker,
                )

                with mock.patch("ibkr_compute.market.data_backfill.time.sleep") as sleep_mock:
                    rows = backfill.fetch_history(
                        121665622,
                        "ZTS",
                        interval="5m",
                        exchange="BATS",
                        repair=False,
                        request_period="1d",
                    )

                self.assertEqual(rows, [])
                self.assertEqual(broker.calls, 1)
                sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
