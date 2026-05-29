from __future__ import annotations

import sys
import types
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.config import Config
from ibkr_compute.orchestration import service_support
from ibkr_compute.orchestration.service_support import TradingServiceSupportMixin


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


def _fake_service_mod():
    return types.SimpleNamespace(ENVIRONMENT="paper", DATA_ENVIRONMENT="live")


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
