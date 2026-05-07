import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import market_sentiment
from ibkr_compute.api.compute import payloads


class MarketSentimentClassificationTest(unittest.TestCase):
    def test_risk_on_long_is_with_trend(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 17.5, "change_pct": -5.0, "stale": False},
                {"symbol": "SPY", "value": 500.0, "change_pct": 0.8, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": 1.2, "stale": False},
            ],
            "long",
        )

        self.assertEqual(result["market_sentiment"], "risk_on")
        self.assertEqual(result["market_relation"], "with_trend")

    def test_risk_on_short_is_against_trend(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 18.0, "change_pct": -2.0, "stale": False},
                {"symbol": "SPY", "value": 500.0, "change_pct": 0.3, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": 0.1, "stale": False},
            ],
            "short",
        )

        self.assertEqual(result["market_sentiment"], "risk_on")
        self.assertEqual(result["market_relation"], "against_trend")

    def test_risk_off_short_is_with_trend(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 26.0, "change_pct": 12.0, "stale": False},
                {"symbol": "SPY", "value": 500.0, "change_pct": -1.1, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": -1.8, "stale": False},
            ],
            "short",
        )

        self.assertEqual(result["market_sentiment"], "risk_off")
        self.assertEqual(result["market_relation"], "with_trend")

    def test_panic_long_is_against_trend_not_direct_bullish(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 31.0, "prev_value": 29.5, "value_delta": 1.5, "stale": False},
                {"symbol": "SPY", "value": 500.0, "change_pct": -2.1, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": -2.8, "stale": False},
            ],
            "long",
        )

        self.assertEqual(result["market_sentiment"], "panic")
        self.assertEqual(result["market_relation"], "against_trend")

    def test_panic_reversal_watch_is_neutral(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 28.0, "prev_value": 30.5, "value_delta": -2.5, "stale": False},
                {"symbol": "SPY", "value": 500.0, "change_pct": 0.2, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": -0.1, "stale": False},
            ],
            "long",
        )

        self.assertEqual(result["market_sentiment"], "panic_reversal_watch")
        self.assertEqual(result["market_relation"], "neutral")

    def test_missing_or_stale_vix_is_unknown_neutral(self):
        result = market_sentiment.classify_market_sentiment(
            [
                {"symbol": "VIX", "value": 18.0, "change_pct": -3.0, "stale": True},
                {"symbol": "SPY", "value": 500.0, "change_pct": 0.8, "stale": False},
                {"symbol": "QQQ", "value": 430.0, "change_pct": 1.1, "stale": False},
            ],
            "long",
        )

        self.assertEqual(result["market_sentiment"], "unknown")
        self.assertEqual(result["market_relation"], "neutral")


class MarketSentimentStorageTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE ibkr_indicators (
                symbol TEXT, interval TEXT, us_time TEXT, cn_time TEXT,
                bar_time_ms INTEGER, extra TEXT, environment TEXT, updated TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE ibkr_bars (
                symbol TEXT, interval TEXT, us_time TEXT, cn_time TEXT,
                bar_time_ms INTEGER, close REAL, extra TEXT, environment TEXT, updated TEXT
            )
            """
        )
        return conn

    def test_fetch_snapshots_never_uses_future_rows(self):
        conn = self._conn()
        rows = [
            ("VIX", "5", "2026-05-07 10:00:00", 1000, {"close": 18.0, "day_change_pct": -4.0}),
            ("VIX", "5", "2026-05-07 10:05:00", 3000, {"close": 31.0, "day_change_pct": 20.0}),
            ("SPY", "5", "2026-05-07 10:00:00", 1000, {"close": 500.0, "day_change_pct": 0.5}),
            ("QQQ", "5", "2026-05-07 10:00:00", 1000, {"close": 430.0, "day_change_pct": 0.7}),
        ]
        for symbol, interval, us_time, bar_ms, extra in rows:
            conn.execute(
                "INSERT INTO ibkr_indicators VALUES (?, ?, ?, '', ?, ?, 'live', '')",
                (symbol, interval, us_time, bar_ms, json.dumps(extra)),
            )
        conn.commit()

        app = SimpleNamespace(cfg=None)
        with mock.patch.object(market_sentiment, "open_pb_sqlite", return_value=conn):
            snapshots = market_sentiment.fetch_market_sentiment_snapshots(
                api_app=app,
                environment="live",
                signal_bar_time_ms=2000,
                symbols=("VIX", "SPY", "QQQ"),
            )

        vix = {item["symbol"]: item for item in snapshots}["VIX"]
        self.assertEqual(vix["value"], 18.0)
        self.assertEqual(vix["bar_time_ms"], 1000)

    def test_bar_snapshot_wins_when_indicator_is_older(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO ibkr_indicators VALUES ('VIX', '5', '2026-05-07 10:00:00', '', 1000, ?, 'live', '')",
            (json.dumps({"close": 19.0, "day_change_pct": -1.0}),),
        )
        conn.execute(
            "INSERT INTO ibkr_bars VALUES ('VIX', '5m', '2026-05-07 10:05:00', '', 2000, 18.5, ?, 'live', '')",
            (json.dumps({"day_change_pct": -3.0}),),
        )
        conn.commit()

        app = SimpleNamespace(cfg=None)
        with mock.patch.object(market_sentiment, "open_pb_sqlite", return_value=conn):
            snapshots = market_sentiment.fetch_market_sentiment_snapshots(
                api_app=app,
                environment="live",
                signal_bar_time_ms=2000,
                symbols=("VIX",),
            )

        self.assertEqual(snapshots[0]["source"], "bar")
        self.assertEqual(snapshots[0]["value"], 18.5)


class SignalPayloadMarketSentimentTest(unittest.TestCase):
    def test_signal_payload_merges_market_sentiment_extra(self):
        fake_app = SimpleNamespace(
            IBKR_SCRIPT_TAG="ibkr_compute",
            resolve_initial_signal_state=lambda environment, bar_ms: ("pending", "ready"),
        )
        engine = SimpleNamespace(bar_count=42)
        bar = {
            "symbol": "AMD",
            "bar_time_ms": 2000,
            "close": 100.0,
            "exchange": "NASDAQ",
            "us_time": "2026-05-07 10:00:00",
            "cn_time": "2026-05-07 22:00:00",
        }
        signal = {
            "direction": "long",
            "signal": "mr_sdLower",
            "entry": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "rr": 2,
            "shares": 10,
            "reason": "test",
            "extra": {"atr": 1.2},
        }
        market_extra = {
            "market_indexes": [{"symbol": "VIX", "value": 18.0, "change_pct": -2.0}],
            "market_sentiment": "risk_on",
            "market_sentiment_text": "VIX 平稳",
            "market_relation": "with_trend",
            "market_relation_text": "多头信号顺市场情绪",
        }

        with mock.patch.object(payloads, "_api_app", return_value=fake_app), mock.patch.object(
            payloads,
            "refresh_symbol_metadata",
            return_value={"AMD": {"industry": "Semiconductors", "exchange": "NASDAQ"}},
        ), mock.patch.object(payloads, "get_daily_change_fields", return_value={}), mock.patch.object(
            payloads,
            "build_market_sentiment_extra",
            return_value=market_extra,
        ):
            result = payloads.build_signal_payload("live", "AMD", "5m", bar, engine, signal)

        self.assertEqual(result["extra"]["market_sentiment"], "risk_on")
        self.assertEqual(result["extra"]["market_relation"], "with_trend")
        self.assertEqual(result["extra"]["market_indexes"][0]["symbol"], "VIX")
        self.assertEqual(result["direction"], "long")
        self.assertEqual(result["entry"], 100.0)


if __name__ == "__main__":
    unittest.main()
