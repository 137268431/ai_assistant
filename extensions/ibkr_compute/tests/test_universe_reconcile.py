import sqlite3
import sys
import threading
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.pocketbase_sqlite import delete_symbol_runtime_data
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin


class DummySignalProcessor:
    def __init__(self, invalid_signal_ids=None):
        self.invalid_signal_ids = set(invalid_signal_ids or [])

    def validate_signal(self, signal: dict):
        signal_id = str(signal.get("signal_id") or "").strip()
        if signal_id in self.invalid_signal_ids:
            return False, "signal_expired"
        return True, "ok"


class DummyUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self, invalid_signal_ids=None):
        self.signal_processor = DummySignalProcessor(invalid_signal_ids=invalid_signal_ids)

    def _now_iso(self) -> str:
        return "2026-04-16T10:05:00-04:00"


class DummyConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_int_for_environment(self, key, _environment, fallback):
        return int(self.values.get(key, fallback))


class DummyQuoteBook:
    def __init__(self, stale_quotes):
        self.stale_quotes = list(stale_quotes)
        self.calls = []

    def get_stale_quotes(self, symbols=None, max_age_s=600):
        symbol_set = {str(symbol or "").strip().upper() for symbol in (symbols or [])}
        self.calls.append({"symbols": sorted(symbol_set), "max_age_s": max_age_s})
        return [
            dict(item)
            for item in self.stale_quotes
            if not symbol_set or str(item.get("symbol") or "").strip().upper() in symbol_set
        ]


class DummyWsClient:
    def __init__(self):
        self.resubscribed = []

    def resubscribe(self, conid):
        self.resubscribed.append(int(conid))


class DummyResubscribeUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self, stale_quotes=None):
        self.config = DummyConfig(
            {
                "ibkr_realtime_quote_stale_resubscribe_sec": 600,
                "ibkr_realtime_quote_resubscribe_cooldown_sec": 300,
                "ibkr_ws_resubscribe_batch_size": 8,
                "ibkr_ws_resubscribe_gap_ms": 0,
            }
        )
        self.realtime_quote_book = DummyQuoteBook(stale_quotes or [])
        self.ws_client = DummyWsClient()
        self._quote_resubscribe_at = {}
        self._subscription_lock = threading.Lock()
        self._active_subscription_map = {
            "SPY": 756733,
            "AAPL": 265598,
        }

    def _market_ws_symbols(self):
        return ["SPY", "QQQ", "VIX"]

    def _normalize_symbol_list(self, values):
        normalized = []
        seen = set()
        for value in values or []:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
        return normalized


class UniversePrimeSignalSelectionTest(unittest.TestCase):
    def test_selects_latest_valid_signal_per_symbol(self):
        universe = DummyUniverse(invalid_signal_ids={"AAPL_INVALID"})
        captured_signals = [
            {
                "signal_id": "AAPL_INVALID",
                "symbol": "AAPL",
                "direction": "long",
                "entry": 101.0,
                "stop_loss": 99.0,
                "take_profit": 106.0,
                "shares": 10,
                "bar_time_ms": 300,
                "us_time": "2026-04-16 10:00:00",
                "extra": {},
            },
            {
                "signal_id": "AAPL_VALID",
                "symbol": "AAPL",
                "direction": "long",
                "entry": 100.0,
                "stop_loss": 98.0,
                "take_profit": 105.0,
                "shares": 10,
                "bar_time_ms": 200,
                "us_time": "2026-04-16 09:55:00",
                "extra": {},
            },
            {
                "signal_id": "MSFT_VALID",
                "symbol": "MSFT",
                "direction": "short",
                "entry": 300.0,
                "stop_loss": 305.0,
                "take_profit": 290.0,
                "shares": 8,
                "bar_time_ms": 250,
                "us_time": "2026-04-16 09:58:00",
                "extra": {},
            },
        ]

        result = universe._select_latest_valid_prime_signals(captured_signals, allowed_symbols=["AAPL", "MSFT"])

        selected_ids = [item["signal_id"] for item in result["selected_signals"]]
        self.assertEqual(selected_ids, ["AAPL_VALID", "MSFT_VALID"])
        self.assertEqual(result["evaluated"][0]["signal_id"], "AAPL_INVALID")
        self.assertFalse(result["evaluated"][0]["valid"])
        self.assertTrue(result["selected_signals"][0]["extra"]["universe_prime"])


class UniverseRealtimeQuoteResubscribeTest(unittest.TestCase):
    def test_repairs_only_stale_market_monitor_quotes_and_respects_cooldown(self):
        universe = DummyResubscribeUniverse(
            stale_quotes=[
                {"symbol": "SPY", "quote_age_s": 701.0},
                {"symbol": "AAPL", "quote_age_s": 900.0},
            ]
        )

        repaired = universe._repair_stale_realtime_quote_subscriptions(
            {
                "SPY": 756733,
                "AAPL": 265598,
            },
            monitor_symbols=universe._market_ws_symbols(),
            reason="test",
        )
        repaired_again = universe._repair_stale_realtime_quote_subscriptions(
            {
                "SPY": 756733,
                "AAPL": 265598,
            },
            monitor_symbols=universe._market_ws_symbols(),
            reason="test",
        )

        self.assertEqual(["SPY"], repaired)
        self.assertEqual([], repaired_again)
        self.assertEqual([756733], universe.ws_client.resubscribed)
        self.assertEqual(["QQQ", "SPY", "VIX"], universe.realtime_quote_book.calls[0]["symbols"])

    def test_session_restore_force_resubscribes_active_market_data(self):
        universe = DummyResubscribeUniverse()

        repaired = universe._force_resubscribe_active_market_data(reason="session_restored")

        self.assertEqual(["SPY", "AAPL"], repaired)
        self.assertEqual([756733, 265598], universe.ws_client.resubscribed)


class DeleteSymbolRuntimeDataTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE ibkr_bars (symbol TEXT, environment TEXT);
            CREATE TABLE ibkr_indicators (symbol TEXT, environment TEXT);
            CREATE TABLE ibkr_reverse_signals (symbol TEXT, environment TEXT);
            CREATE TABLE ibkr_bar_integrity (symbol TEXT, environment TEXT);
            CREATE TABLE ibkr_bar_truth_audit (symbol TEXT, environment TEXT);
            CREATE TABLE ibkr_signals (symbol TEXT, environment TEXT, status TEXT, signal_id TEXT);
            CREATE TABLE orders (symbol TEXT, environment TEXT, signal_id TEXT);
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_deletes_non_executed_unlinked_symbol_rows_and_keeps_order_linked_signals(self):
        self.conn.executemany(
            "INSERT INTO ibkr_bars(symbol, environment) VALUES(?, ?)",
            [("AAPL", "live"), ("AAPL", ""), ("MSFT", "live")],
        )
        self.conn.executemany(
            "INSERT INTO ibkr_indicators(symbol, environment) VALUES(?, ?)",
            [("AAPL", "live"), ("AAPL", ""), ("MSFT", "live")],
        )
        self.conn.executemany(
            "INSERT INTO ibkr_reverse_signals(symbol, environment) VALUES(?, ?)",
            [("AAPL", "live"), ("AAPL", ""), ("MSFT", "live")],
        )
        self.conn.executemany(
            "INSERT INTO ibkr_bar_integrity(symbol, environment) VALUES(?, ?)",
            [("AAPL", "live"), ("AAPL", ""), ("MSFT", "live")],
        )
        self.conn.executemany(
            "INSERT INTO ibkr_bar_truth_audit(symbol, environment) VALUES(?, ?)",
            [("AAPL", "live"), ("AAPL", ""), ("MSFT", "live")],
        )
        self.conn.executemany(
            "INSERT INTO ibkr_signals(symbol, environment, status, signal_id) VALUES(?, ?, ?, ?)",
            [
                ("AAPL", "live", "pending", "SIG_DELETE"),
                ("AAPL", "", "executed", "SIG_EXECUTED"),
                ("AAPL", "live", "pending", "SIG_LINKED"),
                ("MSFT", "live", "pending", "SIG_OTHER"),
            ],
        )
        self.conn.execute(
            "INSERT INTO orders(symbol, environment, signal_id) VALUES(?, ?, ?)",
            ("AAPL", "live", "SIG_LINKED"),
        )

        result = delete_symbol_runtime_data(self.conn, "live", ["aapl"])

        self.assertEqual(result["deleted"]["ibkr_bars"], 2)
        self.assertEqual(result["deleted"]["ibkr_indicators"], 2)
        self.assertEqual(result["deleted"]["ibkr_reverse_signals"], 2)
        self.assertEqual(result["deleted"]["ibkr_bar_integrity"], 2)
        self.assertEqual(result["deleted"]["ibkr_bar_truth_audit"], 2)
        self.assertEqual(result["deleted"]["ibkr_signals"], 1)
        self.assertEqual(result["deleted_signal_ids"], ["SIG_DELETE"])
        self.assertEqual(result["preserved_signal_ids"], ["SIG_LINKED"])

        remaining_signals = {
            tuple(row)
            for row in self.conn.execute(
                "SELECT symbol, environment, status, signal_id FROM ibkr_signals ORDER BY signal_id"
            ).fetchall()
        }
        self.assertEqual(
            remaining_signals,
            {
                ("AAPL", "", "executed", "SIG_EXECUTED"),
                ("AAPL", "live", "pending", "SIG_LINKED"),
                ("MSFT", "live", "pending", "SIG_OTHER"),
            },
        )


if __name__ == "__main__":
    unittest.main()
