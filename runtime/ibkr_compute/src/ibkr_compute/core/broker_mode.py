from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


SUPPORTED_RUNTIME_ENVIRONMENTS = {"live", "paper", "backtest"}
BROKER_MODES = {"live", "paper"}
MARKET_DATA_MODES = {"live", "backtest"}

DEFAULT_BROKER_MODE = "paper"
DEFAULT_MARKET_DATA_MODE = "live"
DEFAULT_GATEWAY_MODE = "broker"
DEFAULT_DATA_ENVIRONMENT = DEFAULT_MARKET_DATA_MODE

BROKER_MODE_ENV_KEYS = ("IBKR_BROKER_MODE", "BROKER_MODE")
MARKET_DATA_MODE_ENV_KEYS = ("IBKR_MARKET_DATA_MODE", "MARKET_DATA_MODE")
GATEWAY_MODE_ENV_KEYS = ("IBKR_GATEWAY_MODE", "GATEWAY_MODE")

_RUNTIME_ALIASES = {
    "prod": "live",
    "production": "live",
    "sim": "paper",
    "simulated": "paper",
    "simulation": "paper",
    "test": "backtest",
}
_MARKET_DATA_ALIASES = {
    **_RUNTIME_ALIASES,
    # Paper broker mode shares the canonical live market-data stream.
    "paper": "live",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_runtime_environment(value: Any, default: str = "live") -> str:
    text = _normalize_text(value)
    normalized = _RUNTIME_ALIASES.get(text, text)
    fallback = _RUNTIME_ALIASES.get(_normalize_text(default), _normalize_text(default)) or "live"
    if fallback not in SUPPORTED_RUNTIME_ENVIRONMENTS:
        fallback = "live"
    return normalized if normalized in SUPPORTED_RUNTIME_ENVIRONMENTS else fallback


def normalize_broker_mode(value: Any, default: str = DEFAULT_BROKER_MODE) -> str:
    normalized = normalize_runtime_environment(value, default)
    if normalized in BROKER_MODES:
        return normalized
    fallback = normalize_runtime_environment(default, DEFAULT_BROKER_MODE)
    return fallback if fallback in BROKER_MODES else DEFAULT_BROKER_MODE


def normalize_market_data_mode(value: Any, default: str = DEFAULT_MARKET_DATA_MODE) -> str:
    text = _normalize_text(value)
    normalized = _MARKET_DATA_ALIASES.get(text, text)
    fallback_text = _normalize_text(default) or DEFAULT_MARKET_DATA_MODE
    fallback = _MARKET_DATA_ALIASES.get(fallback_text, fallback_text)
    if fallback not in MARKET_DATA_MODES:
        fallback = DEFAULT_MARKET_DATA_MODE
    return normalized if normalized in MARKET_DATA_MODES else fallback


def _env_value(env: Mapping[str, Any] | None, key: str) -> str:
    source = env if env is not None else os.environ
    try:
        return str(source.get(key) or "").strip()
    except Exception:
        return ""


def _first_env_value(env: Mapping[str, Any] | None, keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        value = _env_value(env, key)
        if value:
            return value, key
    return "", ""


def configured_broker_mode(env: Mapping[str, Any] | None = None) -> str:
    value, _source = _first_env_value(env, BROKER_MODE_ENV_KEYS)
    return normalize_broker_mode(value, DEFAULT_BROKER_MODE)


def configured_market_data_mode(env: Mapping[str, Any] | None = None) -> str:
    value, _source = _first_env_value(env, MARKET_DATA_MODE_ENV_KEYS)
    return normalize_market_data_mode(value, DEFAULT_MARKET_DATA_MODE)


def _normalize_gateway_mode(value: Any, broker_mode: str) -> str:
    text = _normalize_text(value) or DEFAULT_GATEWAY_MODE
    if text == "broker":
        return broker_mode
    return normalize_broker_mode(text, broker_mode)


def configured_gateway_mode(env: Mapping[str, Any] | None = None) -> str:
    broker_mode = configured_broker_mode(env)
    value, _source = _first_env_value(env, GATEWAY_MODE_ENV_KEYS)
    return _normalize_gateway_mode(value, broker_mode)


def resolve_market_data_mode(
    requested_market_data_mode: Any = None,
    env: Mapping[str, Any] | None = None,
) -> str:
    if requested_market_data_mode is None or str(requested_market_data_mode).strip() == "":
        return configured_market_data_mode(env)
    return normalize_market_data_mode(requested_market_data_mode, configured_market_data_mode(env))


def startup_broker_mode(env: Mapping[str, Any] | None = None) -> str:
    return configured_broker_mode(env)


def startup_gateway_mode(env: Mapping[str, Any] | None = None) -> str:
    return configured_gateway_mode(env)


def configured_data_environment(env: Mapping[str, Any] | None = None) -> str:
    return configured_market_data_mode(env)


def resolve_data_environment(requested_environment: Any = None, env: Mapping[str, Any] | None = None) -> str:
    requested = normalize_runtime_environment(requested_environment, configured_broker_mode(env))
    if requested == "backtest":
        return "backtest"
    return resolve_market_data_mode(None, env)


def _mode_source(
    *,
    requested_value: Any,
    env: Mapping[str, Any] | None,
    env_keys: tuple[str, ...],
    default_source: str,
    request_source: str,
) -> str:
    if requested_value is not None and str(requested_value).strip() != "":
        return request_source
    _value, source = _first_env_value(env, env_keys)
    return source or default_source


def mode_context(
    requested_broker_mode: Any = None,
    requested_market_data_mode: Any = None,
    requested_gateway_mode: Any = None,
    env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    broker_mode = normalize_broker_mode(requested_broker_mode, configured_broker_mode(env))
    market_data_mode = resolve_market_data_mode(requested_market_data_mode, env)
    if requested_gateway_mode is not None and str(requested_gateway_mode).strip() != "":
        gateway_mode = _normalize_gateway_mode(requested_gateway_mode, broker_mode)
    else:
        gateway_value, _source = _first_env_value(env, GATEWAY_MODE_ENV_KEYS)
        gateway_mode = _normalize_gateway_mode(gateway_value, broker_mode)

    broker_source = _mode_source(
        requested_value=requested_broker_mode,
        env=env,
        env_keys=BROKER_MODE_ENV_KEYS,
        default_source=f"default_{DEFAULT_BROKER_MODE}",
        request_source="request.broker_mode",
    )
    market_data_source = _mode_source(
        requested_value=requested_market_data_mode,
        env=env,
        env_keys=MARKET_DATA_MODE_ENV_KEYS,
        default_source=f"default_{DEFAULT_MARKET_DATA_MODE}",
        request_source="request.market_data_mode",
    )
    gateway_source = _mode_source(
        requested_value=requested_gateway_mode,
        env=env,
        env_keys=GATEWAY_MODE_ENV_KEYS,
        default_source="default_broker_mode",
        request_source="request.gateway_mode",
    )

    return {
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": market_data_mode,
        "gateway_mode": gateway_mode,
        "data_environment": market_data_mode,
        "market_data_environment": market_data_mode,
        "shared_data_environment": market_data_mode,
        "shared_market_data": market_data_mode == "live",
        "mode_mismatch": broker_mode != gateway_mode,
        "broker_mode_source": broker_source,
        "market_data_mode_source": market_data_source,
        "gateway_mode_source": gateway_source,
        "data_environment_source": market_data_source,
    }


def broker_mode_payload(
    requested_environment: Any = None,
    env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return mode_context(requested_broker_mode=requested_environment, env=env)


__all__ = [
    "BROKER_MODES",
    "BROKER_MODE_ENV_KEYS",
    "DEFAULT_BROKER_MODE",
    "DEFAULT_DATA_ENVIRONMENT",
    "DEFAULT_GATEWAY_MODE",
    "DEFAULT_MARKET_DATA_MODE",
    "GATEWAY_MODE_ENV_KEYS",
    "MARKET_DATA_MODES",
    "MARKET_DATA_MODE_ENV_KEYS",
    "SUPPORTED_RUNTIME_ENVIRONMENTS",
    "broker_mode_payload",
    "configured_broker_mode",
    "configured_data_environment",
    "configured_gateway_mode",
    "configured_market_data_mode",
    "mode_context",
    "normalize_broker_mode",
    "normalize_market_data_mode",
    "normalize_runtime_environment",
    "resolve_data_environment",
    "resolve_market_data_mode",
    "startup_broker_mode",
    "startup_gateway_mode",
]
