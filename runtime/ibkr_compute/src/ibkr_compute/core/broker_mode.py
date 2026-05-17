from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


SUPPORTED_RUNTIME_ENVIRONMENTS = {"live", "paper", "backtest"}
BROKER_MODES = {"live", "paper"}
DEFAULT_DATA_ENVIRONMENT = "live"


def normalize_runtime_environment(value: Any, default: str = "live") -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "prod": "live",
        "production": "live",
        "sim": "paper",
        "simulated": "paper",
        "simulation": "paper",
        "test": "backtest",
    }
    normalized = aliases.get(text, text)
    fallback = str(default or "live").strip().lower() or "live"
    return normalized if normalized in SUPPORTED_RUNTIME_ENVIRONMENTS else fallback


def normalize_broker_mode(value: Any, default: str = "live") -> str:
    normalized = normalize_runtime_environment(value, default)
    if normalized in BROKER_MODES:
        return normalized
    fallback = normalize_runtime_environment(default, "live")
    return fallback if fallback in BROKER_MODES else "live"


def _env_value(env: Mapping[str, Any] | None, key: str) -> str:
    source = env if env is not None else os.environ
    try:
        return str(source.get(key) or "").strip()
    except Exception:
        return ""


def startup_broker_mode(env: Mapping[str, Any] | None = None) -> str:
    return normalize_broker_mode(_env_value(env, "IBKR_ENVIRONMENT"), "live")


def startup_gateway_mode(env: Mapping[str, Any] | None = None) -> str:
    broker_mode = startup_broker_mode(env)
    return normalize_broker_mode(_env_value(env, "IBKR_GATEWAY_TRADING_MODE") or broker_mode, broker_mode)


def configured_data_environment(env: Mapping[str, Any] | None = None) -> str:
    configured = _env_value(env, "IBKR_DATA_ENVIRONMENT") or DEFAULT_DATA_ENVIRONMENT
    return normalize_runtime_environment(configured, DEFAULT_DATA_ENVIRONMENT)


def resolve_data_environment(requested_environment: Any = None, env: Mapping[str, Any] | None = None) -> str:
    requested = normalize_runtime_environment(requested_environment, startup_broker_mode(env))
    if requested == "backtest":
        return "backtest"
    # LIVE and PAPER share the same canonical market-data stream unless explicitly overridden.
    data_environment = configured_data_environment(env)
    return "live" if data_environment == "paper" else data_environment


def broker_mode_payload(
    requested_environment: Any = None,
    env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    broker_mode = startup_broker_mode(env)
    gateway_mode = startup_gateway_mode(env)
    data_environment = resolve_data_environment(requested_environment or broker_mode, env)
    return {
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "gateway_mode": gateway_mode,
        "data_environment": data_environment,
        "market_data_environment": data_environment,
        "shared_data_environment": data_environment,
        "shared_market_data": data_environment == "live",
        "mode_mismatch": broker_mode != gateway_mode,
        "broker_mode_source": "IBKR_ENVIRONMENT",
        "gateway_mode_source": "IBKR_GATEWAY_TRADING_MODE",
        "data_environment_source": "IBKR_DATA_ENVIRONMENT" if _env_value(env, "IBKR_DATA_ENVIRONMENT") else "default_shared_live",
    }


__all__ = [
    "BROKER_MODES",
    "DEFAULT_DATA_ENVIRONMENT",
    "SUPPORTED_RUNTIME_ENVIRONMENTS",
    "broker_mode_payload",
    "configured_data_environment",
    "normalize_broker_mode",
    "normalize_runtime_environment",
    "resolve_data_environment",
    "startup_broker_mode",
    "startup_gateway_mode",
]
