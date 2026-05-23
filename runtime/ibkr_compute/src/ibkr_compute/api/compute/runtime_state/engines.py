from __future__ import annotations

import json

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.compute.runtime_state.universe import (
    get_signal_generator_params,
    signal_generator_params_for_interval,
)
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.market.timeframe_utils import normalize_interval


def _parse_object(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _float_or_zero(raw) -> float:
    try:
        return float(raw)
    except Exception:
        return 0.0


def _bool_value(raw, default: bool = False) -> bool:
    if raw is None:
        return bool(default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw or "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _signal_params_for_symbol(signal_params: dict, symbol: str) -> dict:
    normalized_symbol = str(symbol or "").strip().upper()
    params = dict(signal_params or {})
    direction_by_symbol = _parse_object(params.get("target_direction_bias_by_symbol"))
    direction_bias = str(direction_by_symbol.get(normalized_symbol) or "").strip().lower()
    if direction_bias:
        params["target_direction_bias"] = direction_bias

    if not _bool_value(params.get("target_strategy_policy_enabled"), False):
        return params

    policy_by_symbol = _parse_object(params.get("target_strategy_policy_by_symbol"))
    profile_by_symbol = _parse_object(params.get("target_symbol_profile_by_symbol"))
    strategy_policy = policy_by_symbol.get(normalized_symbol) if isinstance(policy_by_symbol.get(normalized_symbol), dict) else {}
    symbol_profile = profile_by_symbol.get(normalized_symbol) if isinstance(profile_by_symbol.get(normalized_symbol), dict) else {}
    exit_policy = strategy_policy.get("recommended_exit_policy") if isinstance(strategy_policy.get("recommended_exit_policy"), dict) else {}

    profile = str(exit_policy.get("exit_policy_profile") or exit_policy.get("profile") or "").strip()
    if profile:
        params["exit_policy_profile"] = profile
    sl_mult = _float_or_zero(exit_policy.get("sl_atr_mult"))
    if sl_mult > 0:
        params["sl_atr_mult"] = sl_mult
    tp_rr = _float_or_zero(exit_policy.get("tp_rr") or exit_policy.get("rr_ratio"))
    if tp_rr > 0:
        params["rr_ratio"] = tp_rr
    signal_profile = str(strategy_policy.get("recommended_signal_profile") or "").strip()
    if signal_profile:
        params["signal_strategy_profile"] = signal_profile
        params["ibkr_signal_strategy_profile"] = signal_profile
    if strategy_policy:
        params["target_strategy_policy"] = dict(strategy_policy)
    if symbol_profile:
        params["target_symbol_profile"] = dict(symbol_profile)
    return params


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
    interval_signal_params = signal_generator_params_for_interval(signal_params, normalized_interval)
    effective_signal_params = _signal_params_for_symbol(interval_signal_params, normalized_symbol)
    with api_app.compute_lock:
        if key not in api_app.engines:
            api_app.engines[key] = IndicatorEngine(normalized_symbol, normalized_interval, params=effective_signal_params)
            api_app.signal_gens[key] = SignalGenerator(
                normalized_symbol,
                normalized_interval,
                params=effective_signal_params,
            )
        else:
            signal_generator = api_app.signal_gens.get(key)
            if signal_generator:
                signal_generator.set_params(effective_signal_params)
        return api_app.engines[key]


__all__ = ["get_or_create_engine"]
