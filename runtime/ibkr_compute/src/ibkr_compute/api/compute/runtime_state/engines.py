from __future__ import annotations

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.compute.runtime_state.universe import get_signal_generator_params
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.market.timeframe_utils import normalize_interval


def get_or_create_engine(
    environment: str,
    symbol: str,
    interval: str,
    signal_params: dict | None = None,
):
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    key = (runtime_environment, normalized_symbol, normalized_interval)
    signal_params = signal_params or get_signal_generator_params(runtime_environment)
    with api_app.compute_lock:
        if key not in api_app.engines:
            api_app.engines[key] = IndicatorEngine(normalized_symbol, normalized_interval)
            api_app.signal_gens[key] = SignalGenerator(
                normalized_symbol,
                normalized_interval,
                params=signal_params,
            )
        else:
            signal_generator = api_app.signal_gens.get(key)
            if signal_generator:
                signal_generator.set_params(signal_params)
        return api_app.engines[key]


__all__ = ["get_or_create_engine"]
