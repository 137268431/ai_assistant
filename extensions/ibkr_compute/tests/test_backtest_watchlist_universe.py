import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import constants, request_utils
from ibkr_compute.backtest.runtime_service import BacktestService


class FakePB:
    def __init__(self, rows):
        self.rows = list(rows)

    def get_all_records(self, *_args, **_kwargs):
        return list(self.rows)


class BacktestWatchlistUniverseTests(unittest.TestCase):
    def test_normalize_request_excludes_market_monitor_symbols_by_default(self):
        request = request_utils.normalize_request(
            {
                "symbol_source": "manual",
                "symbols": "AAPL,QQQ,SPY,VIX,NVDA",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
                "max_symbols": 99,
            }
        )

        self.assertEqual(request["symbols"], ["AAPL", "NVDA"])
        self.assertEqual(request["max_symbols"], 99)
        self.assertTrue(request["exclude_market_monitors"])
        self.assertTrue({"QQQ", "SPY", "VIX"}.issubset(set(request["exclude_symbols"])))

    def test_watchlist_default_max_symbols_uses_full_trade_pool_limit(self):
        request = request_utils.normalize_request(
            {
                "symbol_source": "watchlist",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
            }
        )

        self.assertEqual(request["max_symbols"], constants.DEFAULT_WATCHLIST_MAX_SYMBOLS)

    def test_watchlist_resolution_excludes_market_monitor_and_wrong_environment(self):
        service = BacktestService(None)
        service.pb = FakePB(
            [
                {"symbol": "AAPL", "exchange": "SMART", "environment": "global", "symbol_role": "trade"},
                {"symbol": "AAPL", "exchange": "NASDAQ", "environment": "live", "symbol_role": "trade"},
                {"symbol": "TSLA", "exchange": "SMART", "environment": "global", "symbol_role": ""},
                {"symbol": "MSFT", "exchange": "SMART", "environment": "paper", "symbol_role": "trade"},
                {"symbol": "QQQ", "exchange": "NASDAQ", "environment": "global", "symbol_role": "market_monitor"},
                {"symbol": "SPY", "exchange": "ARCA", "environment": "global", "symbol_role": "market_monitor"},
            ]
        )
        request = request_utils.normalize_request(
            {
                "symbol_source": "watchlist",
                "source_environment": "live",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
            }
        )

        symbols = service._resolve_symbols(request)

        self.assertEqual(symbols, ["AAPL", "TSLA"])

    def test_daily_scan_universe_excludes_market_monitor_symbols(self):
        service = BacktestService(None)
        service.pb = FakePB(
            [
                {"symbol": "AMD", "exchange": "SMART", "environment": "global", "symbol_role": "trade"},
                {"symbol": "QQQ", "exchange": "NASDAQ", "environment": "global", "symbol_role": "market_monitor"},
                {"symbol": "SPY", "exchange": "ARCA", "environment": "global", "symbol_role": "market_monitor"},
            ]
        )
        request = request_utils.normalize_request(
            {
                "symbol_source": "daily_scan_replay",
                "source_environment": "live",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
            }
        )

        rows = service._load_scan_universe(request)

        self.assertEqual([row["symbol"] for row in rows], ["AMD"])


if __name__ == "__main__":
    unittest.main()
