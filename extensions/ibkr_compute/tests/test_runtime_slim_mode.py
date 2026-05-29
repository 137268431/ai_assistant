from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.config import Config
from ibkr_compute.orchestration import market_universe_targets
from ibkr_compute.orchestration import service_support
from ibkr_compute.orchestration import warmup
from ibkr_compute.orchestration.market_universe_targets import TradingServiceMarketUniverseTargetsMixin
from ibkr_compute.orchestration.service_support import TradingServiceSupportMixin
from ibkr_compute.orchestration.warmup import TradingServiceWarmupMixin


class ScopedConfig(Config):
    def __init__(self, values: dict[tuple[str, str], str] | None = None):
        super().__init__()
        self.values = values or {}

    def get_for_environment(self, key: str, environment: str, default: str = None) -> str:
        if (key, environment) in self.values:
            return self.values[(key, environment)]
        return super().get_for_environment(key, environment, default)


class Sink:
    def __init__(self):
        self.ticks = []

    def on_tick(self, tick):
        self.ticks.append(tick)

    def on_market_tick(self, tick):
        self.ticks.append(tick)


class _NoopLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class _Wakeup:
    def __init__(self):
        self.sets = 0

    def set(self):
        self.sets += 1


class _MappedSink(Sink):
    def __init__(self):
        super().__init__()
        self.symbol_maps = []
        self.removed_conids = []

    def set_symbol_map(self, symbol_map):
        self.symbol_maps.append(dict(symbol_map))

    def remove_conids(self, conids):
        self.removed_conids.extend(sorted(conids))


class _WsClient:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []

    def subscribe(self, conid):
        self.subscribed.append(conid)

    def unsubscribe(self, conid):
        self.unsubscribed.append(conid)


class _NoopBackfill:
    def __init__(self):
        self.calls = []

    def backfill_all(self, conid_map, **kwargs):
        self.calls.append((dict(conid_map), dict(kwargs)))
        return {}


class _NoopWriter:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class _Session:
    def __init__(self, authenticated: bool = True):
        self.is_authenticated = authenticated


class DummyRuntime(TradingServiceSupportMixin):
    def __init__(self, config):
        self.config = config
        self.realtime_quote_book = Sink()
        self.bar_aggregator = Sink()
        self.order_flow_manager = Sink()

    def _signal_loop(self):
        pass

    def _subscription_refresh_loop(self):
        pass

    def _active_repair_loop(self):
        pass

    def _watchlist_backfill_loop(self):
        pass

    def _compute_loop(self):
        pass

    def _official_5m_close_loop(self):
        pass

    def _runtime_direct_topup_loop(self):
        pass

    def _bar_close_loop(self):
        pass

    def _warmup_loop(self):
        pass


class SlimWarmupRuntime(
    TradingServiceMarketUniverseTargetsMixin,
    TradingServiceSupportMixin,
    TradingServiceWarmupMixin,
):
    def __init__(self, *, authenticated: bool = True, running: bool = True):
        self.config = ScopedConfig(
            {
                ("ibkr_signal_source", "paper"): "tradingview",
                ("ibkr_market_ws_enabled", "live"): "false",
                ("ibkr_market_ws_enabled", "paper"): "false",
            }
        )
        self.session_keeper = _Session(authenticated)
        self._running = running
        self._subscription_lock = threading.Lock()
        self._warmup_lock = threading.Lock()
        self._warmup_signature = ()
        self._warmup_state = self._initial_warmup_state()
        self._warmup_wakeup = _Wakeup()
        self._current_market_date = "2026-05-29"
        self._active_target_date = ""
        self._active_subscription_map = {}
        self._active_subscription_symbols = []
        self._active_trade_symbols = []
        self._watchlist_symbols = []
        self._watchlist_trade_symbols = []
        self._watchlist_monitor_symbols = []
        self._symbol_meta = {}
        self.bar_aggregator = _MappedSink()
        self.realtime_quote_book = _MappedSink()
        self.order_flow_manager = Sink()
        self.ws_client = _WsClient()
        self.data_backfill = _NoopBackfill()
        self.data_writer = _NoopWriter()
        self.repair_calls = []

    def _now_iso(self):
        return "2026-05-29T14:00:00Z"

    def _market_date(self):
        return "2026-05-29"

    def _repair_stale_realtime_quote_subscriptions(self, conid_map, **kwargs):
        self.repair_calls.append((dict(conid_map), dict(kwargs)))


def _fake_service_mod():
    return types.SimpleNamespace(
        ENVIRONMENT="paper",
        DATA_ENVIRONMENT="live",
        DEFAULT_WARMUP_REQUIRED_INTERVAL="5m",
        DEFAULT_MARKET_WS_SYMBOLS=[],
        WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
        WATCHLIST_SYMBOL_ROLE_TRADE="trade",
        logger=_NoopLogger(),
    )


def _patch_service_modules(monkeypatch):
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    monkeypatch.setattr(warmup, "_service_mod", _fake_service_mod)
    monkeypatch.setattr(market_universe_targets, "_service_mod", _fake_service_mod)


def test_tv_primary_slim_defaults_disable_local_technical_pipeline() -> None:
    cfg = Config()

    assert Config.DEFAULTS["ibkr_tv_primary_runtime_slim_enabled"] == "true"
    assert cfg.get_for_environment("ibkr_tv_primary_runtime_slim_enabled", "paper") == "true"
    assert Config.DEFAULTS["ibkr_runtime_technical_pipeline_enabled"] == "false"
    assert Config.DEFAULTS["ibkr_runtime_feed_technical_ticks_enabled"] == "true"


def test_tv_primary_slim_keeps_core_threads_and_skips_local_technical_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    runtime = DummyRuntime(
        ScopedConfig(
            {
                ("ibkr_signal_source", "paper"): "tradingview",
            }
        )
    )

    assert runtime._runtime_slim_mode_enabled() is True
    assert runtime._runtime_tv_primary_mode() is True
    assert runtime._runtime_technical_pipeline_enabled() is False
    assert [name for _, name, _ in runtime._runtime_background_thread_specs()] == [
        "signal-loop",
        "target-refresh",
    ]


def test_tv_primary_defaults_to_slim_when_tv_primary_flag_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    runtime = DummyRuntime(ScopedConfig({("ibkr_signal_source", "paper"): "tradingview"}))

    assert runtime._runtime_tv_primary_mode() is True
    assert runtime._runtime_slim_mode_enabled() is True
    assert [name for _, name, _ in runtime._runtime_background_thread_specs()] == [
        "signal-loop",
        "target-refresh",
    ]


def test_disabled_technical_pipeline_uses_slim_gate_even_without_tv_source(monkeypatch) -> None:
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    runtime = DummyRuntime(
        ScopedConfig(
            {
                ("ibkr_signal_source", "paper"): "ibkr_compute",
                ("ibkr_runtime_technical_pipeline_enabled", "paper"): "false",
            }
        )
    )

    assert runtime._runtime_tv_primary_mode() is False
    assert runtime._runtime_slim_mode_enabled() is True
    assert [name for _, name, _ in runtime._runtime_background_thread_specs()] == [
        "signal-loop",
        "target-refresh",
    ]


def test_slim_trade_readiness_ignores_missing_current_compute_for_active_trades(monkeypatch) -> None:
    _patch_service_modules(monkeypatch)
    runtime = SlimWarmupRuntime(authenticated=True, running=True)
    runtime._watchlist_symbols = ["AAPL"]
    runtime._watchlist_trade_symbols = ["AAPL"]
    runtime._active_subscription_map = {"AAPL": 1001}
    runtime._active_subscription_symbols = ["AAPL"]
    runtime._active_trade_symbols = ["AAPL"]
    runtime._active_target_date = "2026-05-29"
    runtime._open_slim_runtime_gate()
    runtime._trade_readiness_from_current_compute = mock.Mock(
        return_value={
            "open": False,
            "reason": "missing_trade_symbols",
            "source": "runtime_multi_timeframe_readiness",
            "symbols": ["AAPL"],
        }
    )

    readiness = runtime._trade_readiness_snapshot()

    assert readiness["open"] is True
    assert readiness["reason"] == "runtime_slim_mode"
    assert readiness["source"] == "runtime_slim_mode"
    runtime._trade_readiness_from_current_compute.assert_not_called()


def test_slim_trade_readiness_without_active_trade_symbols_stays_no_trade(monkeypatch) -> None:
    _patch_service_modules(monkeypatch)
    runtime = SlimWarmupRuntime(authenticated=True, running=True)
    runtime._watchlist_symbols = ["AAPL"]
    runtime._watchlist_trade_symbols = ["AAPL"]
    runtime._open_slim_runtime_gate()
    runtime._trade_readiness_from_current_compute = mock.Mock(
        return_value={
            "open": False,
            "reason": "no_trade_symbols",
            "source": "current_readiness",
            "symbols": [],
        }
    )

    readiness = runtime._trade_readiness_snapshot()

    assert readiness["open"] is False
    assert readiness["reason"] == "no_trade_symbols"
    assert runtime._warmup_state["trading_gate_open"] is False
    assert runtime._warmup_state["trading_gate_reason"] == "no_trade_symbols"
    runtime._trade_readiness_from_current_compute.assert_not_called()


def test_slim_trade_readiness_keeps_auth_and_runtime_blocks(monkeypatch) -> None:
    _patch_service_modules(monkeypatch)
    runtime = SlimWarmupRuntime(authenticated=False, running=True)
    runtime._watchlist_symbols = ["AAPL"]
    runtime._watchlist_trade_symbols = ["AAPL"]
    runtime._active_subscription_map = {"AAPL": 1001}
    runtime._active_subscription_symbols = ["AAPL"]
    runtime._active_trade_symbols = ["AAPL"]
    runtime._active_target_date = "2026-05-29"
    runtime._open_slim_runtime_gate()

    readiness = runtime._trade_readiness_snapshot()
    assert readiness == {"open": False, "reason": "session_unauthenticated"}

    runtime.session_keeper.is_authenticated = True
    runtime._running = False

    readiness = runtime._trade_readiness_snapshot()
    assert readiness == {"open": False, "reason": "runtime_stopped"}


def test_slim_target_refresh_keeps_gate_open_instead_of_warmup_pending(monkeypatch) -> None:
    _patch_service_modules(monkeypatch)
    runtime = SlimWarmupRuntime(authenticated=True, running=True)
    runtime._watchlist_symbols = ["AAPL"]
    runtime._watchlist_trade_symbols = ["AAPL"]
    runtime._apply_live_subscriptions(
        "2026-05-29",
        {"AAPL": 1001},
        reason="startup",
        trade_symbols=["AAPL"],
    )
    runtime._open_slim_runtime_gate()

    runtime._watchlist_symbols = ["AAPL", "MSFT"]
    runtime._watchlist_trade_symbols = ["AAPL", "MSFT"]
    runtime._trade_readiness_from_current_compute = mock.Mock(
        return_value={
            "open": False,
            "reason": "missing_trade_symbols",
            "source": "runtime_multi_timeframe_readiness",
            "symbols": ["MSFT"],
        }
    )

    runtime._apply_live_subscriptions(
        "2026-05-29",
        {"AAPL": 1001, "MSFT": 1002},
        reason="poll",
        trade_symbols=["AAPL", "MSFT"],
    )
    readiness = runtime._trade_readiness_snapshot()

    assert runtime._warmup_state["phase"] == "ready"
    assert runtime._warmup_state["trading_gate_open"] is True
    assert runtime._warmup_state["trading_gate_reason"] == "runtime_slim_mode"
    assert runtime._warmup_state["pending_symbols"] == []
    assert runtime._warmup_state["trade_symbols"] == ["AAPL", "MSFT"]
    assert runtime._warmup_state["ready_trade_symbols"] == 2
    assert readiness["open"] is True
    assert readiness["reason"] == "runtime_slim_mode"
    assert readiness["source"] == "runtime_slim_mode"
    runtime._trade_readiness_from_current_compute.assert_not_called()


def test_full_runtime_feeds_ticks_to_quote_book_bar_aggregator_and_order_flow(monkeypatch) -> None:
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    runtime = DummyRuntime(
        ScopedConfig(
            {
                ("ibkr_signal_source", "paper"): "ibkr_compute",
                ("ibkr_tv_primary_runtime_slim_enabled", "paper"): "false",
                ("ibkr_runtime_technical_pipeline_enabled", "paper"): "true",
            }
        )
    )

    tick = {"symbol": "SPY", "price": 500}
    runtime._on_ws_market_tick(tick)

    assert runtime.realtime_quote_book.ticks == [tick]
    assert runtime.bar_aggregator.ticks == [tick]
    assert runtime.order_flow_manager.ticks == [tick]


def test_slim_runtime_ticks_only_update_quote_book(monkeypatch) -> None:
    monkeypatch.setattr(service_support, "_service_mod", _fake_service_mod)
    runtime = DummyRuntime(ScopedConfig({("ibkr_signal_source", "paper"): "tradingview"}))

    tick = {"symbol": "SPY", "price": 500}
    runtime._on_ws_market_tick(tick)

    assert runtime.realtime_quote_book.ticks == [tick]
    assert runtime.bar_aggregator.ticks == []
    assert runtime.order_flow_manager.ticks == []
