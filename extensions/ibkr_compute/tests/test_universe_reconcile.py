import sqlite3
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.pocketbase_sqlite import delete_symbol_runtime_data
from ibkr_compute.orchestration import market_universe as market_universe_mod
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


class DummyPreloadCoordinator:
    def __init__(self):
        self.calls = []

    def enqueue(self, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), **kwargs})
        return {
            "ok": True,
            "available": True,
            "enabled": True,
            "symbols": list(symbols),
            "queued": [{"symbol": symbol} for symbol in symbols],
            "deduped": [],
        }


class DummyConidResolver:
    def resolve_bulk(self, symbols):
        return {str(symbol or "").strip().upper(): index + 1 for index, symbol in enumerate(symbols or [])}


class DummyDataBackfill:
    def __init__(self):
        self.calls = []

    def backfill_all(self, conid_map, symbol_meta=None, intervals=None):
        self.calls.append({"conid_map": dict(conid_map), "symbol_meta": dict(symbol_meta or {}), "intervals": list(intervals or [])})
        return {symbol: {"5m": 1} for symbol in conid_map}


class DummyDataWriter:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1


class FailDataBackfill:
    def backfill_all(self, *_args, **_kwargs):
        raise AssertionError("backfill_all should not be called")


class FailFlushWriter:
    def flush(self):
        raise AssertionError("flush should not be called")


class DummySignalRouter:
    def forget_processed(self, _signal_ids):
        return None


class DummyTargetPlanPB:
    def __init__(self, rows, signals=None):
        self.rows = [dict(row) for row in rows]
        self.signals = [dict(row) for row in (signals or [])]
        self.updated = []

    def get_all_records(self, collection, **_kwargs):
        if collection == "ibkr_targets":
            return [dict(row) for row in self.rows]
        if collection == "ibkr_signals":
            return [dict(row) for row in self.signals]
        return []

    def update_record(self, collection, record_id, data):
        self.updated.append((collection, record_id, dict(data)))
        for row in self.rows:
            if row.get("id") == record_id:
                row.update(dict(data))
                return dict(row)
        return {"id": record_id, **dict(data)}


class DummyTargetPlanUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self, rows, signals=None):
        self.config = DummyConfig(
            {
                "ibkr_target_subscription_limit": 10,
                "ibkr_total_subscription_limit": 10,
                "entry_pre_submit_temp_subscription_limit": 0,
            }
        )
        self.pb = DummyTargetPlanPB(rows, signals=signals)
        self._watchlist_records = {
            "AAPL": {"symbol": "AAPL", "exchange": "NASDAQ", "industry": "Technology", "symbol_role": "trade"},
            "SPY": {"symbol": "SPY", "exchange": "ARCA", "industry": "ETF", "symbol_role": "market_monitor"},
        }
        self._symbol_meta = {
            "AAPL": {"exchange": "NASDAQ", "industry": "Technology"},
            "SPY": {"exchange": "ARCA", "industry": "ETF"},
        }

    def _market_ws_symbols(self):
        return ["SPY", "QQQ", "VIX"]


class DummyPrimeUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self):
        self.conid_resolver = DummyConidResolver()
        self.data_backfill = DummyDataBackfill()
        self.data_writer = DummyDataWriter()
        self.backtest_preload_coordinator = DummyPreloadCoordinator()
        self.signal_processor = DummySignalProcessor()
        self.signal_router = DummySignalRouter()
        self._signal_wakeup = threading.Event()
        self._symbol_meta = {"AAPL": {"exchange": "SMART"}}
        self._last_backfill_at = 0
        self._last_backfill_symbols = []

    def _now_iso(self) -> str:
        return "2026-04-16T10:05:00-04:00"

    def _schedule_interval_prime(self, symbols, source="universe_prime"):
        self.interval_prime = {"symbols": list(symbols), "source": source}
        return True


class DummyMarketDataSink:
    def __init__(self):
        self.symbol_maps = []
        self.removed = []

    def set_symbol_map(self, symbol_map):
        self.symbol_maps.append(dict(symbol_map or {}))

    def remove_conids(self, conids):
        self.removed.append(sorted(conids))


class DummySubscriptionWs:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []

    def subscribe(self, conid):
        self.subscribed.append(int(conid))

    def unsubscribe(self, conid):
        self.unsubscribed.append(int(conid))


class DummyLiveSubscriptionUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self):
        self.config = DummyConfig()
        self._subscription_lock = threading.RLock()
        self._active_subscription_map = {}
        self._active_subscription_symbols = []
        self._active_trade_symbols = []
        self._active_target_date = ""
        self._last_target_refresh_at = 0.0
        self._symbol_meta = {"AAPL": {"exchange": "SMART"}}
        self.bar_aggregator = DummyMarketDataSink()
        self.realtime_quote_book = DummyMarketDataSink()
        self.ws_client = DummySubscriptionWs()
        self.data_backfill = FailDataBackfill()
        self.data_writer = FailFlushWriter()
        self.scheduled_warmups = []
        self.repair_calls = []

    def _runtime_slim_mode_enabled(self):
        return True

    def _market_ws_symbols(self):
        return []

    def _schedule_warmup(self, reason="subscriptions_changed", force=False):
        self.scheduled_warmups.append({"reason": reason, "force": force})
        return True

    def _repair_stale_realtime_quote_subscriptions(self, conid_map, monitor_symbols=None, reason=""):
        self.repair_calls.append(
            {
                "conid_map": dict(conid_map or {}),
                "monitor_symbols": list(monitor_symbols or []),
                "reason": reason,
            }
        )
        return False


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


class UniversePrimeBacktestPreloadTest(unittest.TestCase):
    def test_prime_enqueues_default_backtest_preload_without_blocking_compute(self):
        universe = DummyPrimeUniverse()
        fake_server = types.SimpleNamespace(
            compute_lock=threading.RLock(),
            _run_internal_compute=lambda payload: {"ok": True, "payload": payload, "captured_signals": []},
        )

        import ibkr_compute.api as compute_api_pkg

        with mock.patch.dict(sys.modules, {"ibkr_compute.api.server": fake_server}), mock.patch.object(
            compute_api_pkg,
            "server",
            fake_server,
            create=True,
        ):
            result = universe._prime_universe_symbols(["aapl"], source="target_upsert")

        self.assertTrue(result["ok"])
        self.assertEqual(["AAPL"], result["backtest_preload"]["symbols"])
        self.assertEqual(1, len(result["backtest_preload"]["queued"]))
        preload_call = universe.backtest_preload_coordinator.calls[0]
        self.assertEqual(["AAPL"], preload_call["symbols"])
        self.assertEqual("live", preload_call["environment"])
        self.assertEqual("target_upsert", preload_call["trigger"])
        self.assertEqual("new_universe_symbol_default_backtest_preload", preload_call["reason"])
        self.assertTrue(result["interval_prime_started"])

    def test_prime_skips_runtime_bar_backfill_in_slim_mode(self):
        universe = DummyPrimeUniverse()
        universe._runtime_slim_mode_enabled = lambda: True
        fake_server = types.SimpleNamespace(
            compute_lock=threading.RLock(),
            _run_internal_compute=lambda payload: {"ok": True, "payload": payload, "captured_signals": []},
        )

        import ibkr_compute.api as compute_api_pkg

        with mock.patch.dict(sys.modules, {"ibkr_compute.api.server": fake_server}), mock.patch.object(
            compute_api_pkg,
            "server",
            fake_server,
            create=True,
        ):
            result = universe._prime_universe_symbols(["aapl"], source="target_upsert")

        self.assertTrue(result["ok"])
        self.assertEqual([], universe.data_backfill.calls)
        self.assertEqual(0, universe.data_writer.flushed)
        self.assertTrue(result["backfill"]["skipped"])
        self.assertEqual("tv_primary_no_bar_writes", result["backfill"]["reason"])
        self.assertEqual(["AAPL"], result["backtest_preload"]["symbols"])
        self.assertTrue(result["interval_prime_started"])


class UniverseTargetSubscriptionRuntimeSlimTest(unittest.TestCase):
    def test_apply_live_subscriptions_keeps_subscribe_flow_but_skips_inline_backfill(self):
        universe = DummyLiveSubscriptionUniverse()
        service_mod = mock.Mock()
        service_mod.ENVIRONMENT = "paper"
        service_mod.DATA_ENVIRONMENT = "live"
        service_mod.logger = mock.Mock()

        with mock.patch.object(market_universe_mod, "_service_mod", return_value=service_mod):
            universe._apply_live_subscriptions(
                "2026-05-29",
                {"AAPL": 265598},
                reason="refresh",
                trade_symbols=["AAPL"],
            )

        self.assertEqual([265598], universe.ws_client.subscribed)
        self.assertEqual({"AAPL": 265598}, universe._active_subscription_map)
        self.assertEqual(["AAPL"], universe._active_trade_symbols)
        self.assertEqual([{"reason": "refresh", "force": False}], universe.scheduled_warmups)
        self.assertEqual(1, len(universe.repair_calls))


class UniverseTargetSubscriptionPlanTest(unittest.TestCase):
    def test_old_daily_scan_active_gate_target_is_selected_as_trade_row(self):
        universe = DummyTargetPlanUniverse(
            [
                {"id": "target-spy", "symbol": "SPY", "status": "active", "direction_bias": "long", "score": 99, "extra": {"source": "manual_page_add"}},
                {
                    "id": "target-aapl",
                    "symbol": "AAPL",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 80,
                    "extra": {"source": "daily_scan", "active_gate_passed": True},
                },
            ]
        )

        target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()
        universe._mark_target_statuses(target_date, selected_rows)

        self.assertIn("AAPL", symbols)
        self.assertIn("SPY", symbols)  # still subscribed as market context data
        self.assertEqual(["AAPL"], [row["symbol"] for row in selected_rows])
        spy_updates = [data for collection, record_id, data in universe.pb.updated if record_id == "target-spy"]
        self.assertTrue(spy_updates)
        self.assertEqual("candidate", spy_updates[-1]["status"])
        self.assertTrue(spy_updates[-1]["extra"]["blocked_from_trading"])

    def test_new_context_active_daily_scan_target_is_selected_as_trade_row(self):
        universe = DummyTargetPlanUniverse(
            [
                {
                    "id": "target-aapl",
                    "symbol": "AAPL",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 80,
                    "extra": {"source": "daily_scan", "context_active": True},
                },
                {
                    "id": "target-msft",
                    "symbol": "MSFT",
                    "status": "active",
                    "direction_bias": "short",
                    "score": 70,
                    "extra": {"source": "intraday_window_admission", "context_gate_passed": "passed"},
                },
            ]
        )

        _target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()

        self.assertIn("AAPL", symbols)
        self.assertNotIn("MSFT", symbols)
        self.assertEqual(["AAPL"], [row["symbol"] for row in selected_rows])

    def test_status_sync_keeps_unselected_active_target_active(self):
        universe = DummyTargetPlanUniverse(
            [
                {
                    "id": "target-aapl",
                    "symbol": "AAPL",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 90,
                    "extra": {"source": "daily_scan", "context_active": True},
                },
                {
                    "id": "target-msft",
                    "symbol": "MSFT",
                    "status": "active",
                    "direction_bias": "short",
                    "score": 80,
                    "extra": {"source": "daily_scan", "context_active": True},
                },
            ]
        )
        universe.config.values["ibkr_target_subscription_limit"] = 1

        target_date, _symbols, _meta, selected_rows = universe._build_target_subscription_plan()
        universe._mark_target_statuses(target_date, selected_rows)

        self.assertEqual(["AAPL"], [row["symbol"] for row in selected_rows])
        rows_by_id = {row["id"]: row for row in universe.pb.rows}
        self.assertEqual("active", rows_by_id["target-aapl"]["status"])
        self.assertTrue(rows_by_id["target-aapl"]["extra"]["within_subscription_budget"])
        self.assertEqual(1, rows_by_id["target-aapl"]["extra"]["subscription_rank"])
        self.assertEqual("active", rows_by_id["target-msft"]["status"])
        self.assertFalse(rows_by_id["target-msft"]["extra"]["within_subscription_budget"])
        self.assertEqual(0, rows_by_id["target-msft"]["extra"]["subscription_rank"])

    def test_terminal_latest_signal_demotes_target_and_excludes_subscription(self):
        universe = DummyTargetPlanUniverse(
            [
                {
                    "id": "target-aapl",
                    "symbol": "AAPL",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 90,
                    "extra": {"source": "daily_scan", "context_active": True},
                },
            ],
            signals=[
                {
                    "id": "sig-row",
                    "signal_id": "SIG_CLOSED",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "closed",
                    "bar_time_ms": 1000,
                },
            ],
        )

        target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()
        universe._mark_target_statuses(target_date, selected_rows)

        self.assertNotIn("AAPL", symbols)
        self.assertEqual([], selected_rows)
        row = universe.pb.rows[0]
        self.assertEqual("candidate", row["status"])
        self.assertTrue(row["extra"]["deactivated_after_close"])
        self.assertFalse(row["extra"]["subscription_selected"])
        self.assertFalse(row["extra"]["within_subscription_budget"])
        self.assertIn("deactivated_after_close", row["extra"]["execution_blockers"])

    def test_non_context_active_candidate_is_not_selected_as_trade_row(self):
        universe = DummyTargetPlanUniverse(
            [
                {
                    "id": "target-aapl",
                    "symbol": "AAPL",
                    "status": "active",
                    "score": 99,
                    "extra": {"source": "external_import", "context_active": True},
                },
                {
                    "id": "target-msft",
                    "symbol": "MSFT",
                    "status": "candidate",
                    "score": 98,
                    "extra": {"source": "daily_scan", "context_active": True},
                },
                {
                    "id": "target-tsla",
                    "symbol": "TSLA",
                    "status": "active",
                    "score": 97,
                    "extra": {"source": "daily_scan"},
                },
            ]
        )

        _target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()

        self.assertNotIn("AAPL", symbols)
        self.assertNotIn("MSFT", symbols)
        self.assertNotIn("TSLA", symbols)
        self.assertEqual([], selected_rows)

    def test_manual_active_target_is_selected_as_trade_row(self):
        universe = DummyTargetPlanUniverse(
            [
                {"id": "target-aapl", "symbol": "AAPL", "status": "active", "direction_bias": "long", "score": 99, "extra": {"source": "manual_page_add"}},
            ]
        )

        _target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()

        self.assertIn("AAPL", symbols)
        self.assertEqual(["AAPL"], [row["symbol"] for row in selected_rows])

    def test_tradingview_pre_alert_active_is_demoted_to_candidate(self):
        universe = DummyTargetPlanUniverse(
            [
                {
                    "id": "target-wpm",
                    "symbol": "WPM",
                    "status": "active",
                    "score": 90,
                    "extra": {"source": "tradingview", "event_type": "pre_alert", "activity_rank": 1},
                },
            ]
        )

        target_date, symbols, _meta, selected_rows = universe._build_target_subscription_plan()
        universe._mark_target_statuses(target_date, selected_rows)

        self.assertNotIn("WPM", symbols)
        self.assertEqual([], selected_rows)
        rows_by_id = {row["id"]: row for row in universe.pb.rows}
        self.assertEqual("candidate", rows_by_id["target-wpm"]["status"])
        self.assertFalse(rows_by_id["target-wpm"]["extra"]["within_subscription_budget"])

    def test_trade_budget_reserves_entry_quote_slots(self):
        rows = [
            {
                "id": f"target-{index}",
                "symbol": f"SYM{index}",
                "status": "active",
                "direction_bias": "long",
                "score": 100 - index,
                "extra": {"source": "daily_scan", "active_gate_passed": True},
            }
            for index in range(6)
        ]
        universe = DummyTargetPlanUniverse(rows)
        universe.config.values["entry_pre_submit_temp_subscription_limit"] = 2

        _target_date, _symbols, _meta, selected_rows = universe._build_target_subscription_plan()

        self.assertEqual(5, len(selected_rows))
        self.assertEqual(["SYM0", "SYM1", "SYM2", "SYM3", "SYM4"], [row["symbol"] for row in selected_rows])


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
