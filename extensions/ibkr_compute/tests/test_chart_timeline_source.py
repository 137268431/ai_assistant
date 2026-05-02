import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.chart.timeline import source


class _FakeConfig:
    def get_bool_for_environment(self, key, environment, default=False):
        del key, environment
        return default

    def get_float_for_environment(self, key, environment, default=0.0):
        del key, environment
        return default


def _fake_app(pb):
    return SimpleNamespace(
        BOOTSTRAP_LOOKBACK_BARS={"5m": 2},
        CHART_TIMELINE_VISIBLE_LIMIT=3,
        build_bar_environment_filter=lambda environment, include_legacy_empty=True: (
            f'(environment = "{environment}" || environment = "")'
            if include_legacy_empty and environment == "live"
            else f'environment = "{environment}"'
        ),
        cfg=_FakeConfig(),
        pb=pb,
    )


def _row(bar_time_ms, environment="live"):
    return {
        "id": f"bar-{bar_time_ms}",
        "symbol": "AAPL",
        "exchange": "SMART",
        "interval": "5m",
        "open": float(bar_time_ms),
        "high": float(bar_time_ms) + 1,
        "low": float(bar_time_ms) - 1,
        "close": float(bar_time_ms) + 0.5,
        "volume": float(bar_time_ms) * 10,
        "session_type": "regular",
        "us_time": f"us-{bar_time_ms}",
        "cn_time": f"cn-{bar_time_ms}",
        "bar_time_ms": bar_time_ms,
        "extra": {"source": "test"},
        "environment": environment,
        "created": "",
        "updated": "",
    }


def _create_bar_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE ibkr_bars (
            id TEXT,
            symbol TEXT,
            exchange TEXT,
            interval TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            session_type TEXT,
            us_time TEXT,
            cn_time TEXT,
            bar_time_ms INTEGER,
            extra TEXT,
            environment TEXT,
            created TEXT,
            updated TEXT
        )
        """
    )
    for bar_time_ms, environment in (
        (1000, "live"),
        (2000, ""),
        (3000, "live"),
        (4000, "live"),
        (5000, "live"),
        (6000, "live"),
        (6000, "paper"),
    ):
        conn.execute(
            """
            INSERT INTO ibkr_bars (
                id, symbol, exchange, interval, open, high, low, close, volume,
                session_type, us_time, cn_time, bar_time_ms, extra, environment,
                created, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"bar-{bar_time_ms}-{environment}",
                "AAPL",
                "SMART",
                "5m",
                float(bar_time_ms),
                float(bar_time_ms) + 1,
                float(bar_time_ms) - 1,
                float(bar_time_ms) + 0.5,
                float(bar_time_ms) * 10,
                "regular",
                f"us-{bar_time_ms}",
                f"cn-{bar_time_ms}",
                bar_time_ms,
                '{"source":"sqlite"}',
                environment,
                "",
                "",
            ),
        )
    return conn


class ChartTimelineSourceTest(unittest.TestCase):
    def test_load_source_bars_prefers_sqlite_readonly(self):
        conn = _create_bar_db()
        pb = SimpleNamespace(
            get_all_records=mock.Mock(side_effect=AssertionError("PB API should not be called")),
        )
        fake_app = _fake_app(pb)
        open_sqlite = mock.Mock(return_value=conn)

        try:
            with mock.patch.object(source, "_api_app", return_value=fake_app):
                with mock.patch.object(source, "open_pb_sqlite", open_sqlite):
                    result = source.load_chart_timeline_source_bars(
                        "live",
                        "aapl",
                        "5",
                        start_ms=3000,
                        end_ms=6000,
                    )
        finally:
            conn.close()

        self.assertEqual([row["bar_time_ms"] for row in result["visible_rows"]], [4000, 5000, 6000])
        self.assertEqual([row["bar_time_ms"] for row in result["source_rows"]], [2000, 3000, 4000, 5000, 6000])
        self.assertEqual(result["warmup_limit"], 2)
        self.assertEqual(result["warmup_used"], 2)
        self.assertEqual(result["source_rows"][0]["environment"], "")
        self.assertEqual(result["visible_rows"][0]["extra"], {"source": "sqlite"})
        open_sqlite.assert_called_once_with(readonly=True, timeout=30.0)
        pb.get_all_records.assert_not_called()

    def test_load_source_bars_falls_back_to_pb_api_when_sqlite_fails(self):
        pb = SimpleNamespace(
            get_all_records=mock.Mock(
                side_effect=[
                    [_row(3000), _row(4000), _row(5000), _row(6000)],
                    [_row(3000), _row(2000), _row(1000)],
                ]
            )
        )
        fake_app = _fake_app(pb)

        with mock.patch.object(source, "_api_app", return_value=fake_app):
            with mock.patch.object(source, "open_pb_sqlite", side_effect=RuntimeError("sqlite unavailable")):
                result = source.load_chart_timeline_source_bars(
                    "live",
                    "AAPL",
                    "5m",
                    start_ms=3000,
                    end_ms=6000,
                )

        self.assertEqual([row["bar_time_ms"] for row in result["visible_rows"]], [4000, 5000, 6000])
        self.assertEqual([row["bar_time_ms"] for row in result["source_rows"]], [2000, 3000, 4000, 5000, 6000])
        self.assertEqual(result["warmup_limit"], 2)
        self.assertEqual(result["warmup_used"], 2)
        self.assertEqual(pb.get_all_records.call_count, 2)
        first_call = pb.get_all_records.call_args_list[0]
        second_call = pb.get_all_records.call_args_list[1]
        self.assertEqual(first_call.args[0], "ibkr_bars")
        self.assertEqual(first_call.kwargs["sort"], "bar_time_ms")
        self.assertIn('symbol = "AAPL"', first_call.kwargs["filter"])
        self.assertIn("bar_time_ms >= 3000", first_call.kwargs["filter"])
        self.assertEqual(second_call.args[0], "ibkr_bars")
        self.assertEqual(second_call.kwargs["sort"], "-bar_time_ms")
        self.assertIn("bar_time_ms < 4000", second_call.kwargs["filter"])


if __name__ == "__main__":
    unittest.main()
