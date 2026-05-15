from __future__ import annotations

from typing import Any


DEFAULT_BUYING_POWER_GUARD = {
    "ibkr_buying_power_guard_enabled": True,
    "ibkr_buying_power_warn_usd": 25000.0,
    "ibkr_buying_power_warn_pct_net_liq": 20.0,
    "ibkr_buying_power_block_usd": 10000.0,
    "ibkr_buying_power_block_pct_net_liq": 10.0,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return float(number)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    return text in {"true", "1", "yes", "y", "on"}


def _config_value(config: Any, key: str, environment: str, default: Any) -> Any:
    if config is None:
        return default
    for method_name, args in (
        ("get_for_environment", (key, environment, default)),
        ("get", (key, default)),
    ):
        getter = getattr(config, method_name, None)
        if callable(getter):
            try:
                value = getter(*args)
            except Exception:
                continue
            return default if value in (None, "") else value
    values = getattr(config, "values", None)
    if isinstance(values, dict):
        value = values.get(key, default)
        return default if value in (None, "") else value
    return default


def _config_bool(config: Any, key: str, environment: str = "live", default: bool = False) -> bool:
    getter = getattr(config, "get_bool_for_environment", None)
    if callable(getter):
        try:
            return bool(getter(key, environment, default))
        except Exception:
            pass
    getter = getattr(config, "get_bool", None)
    if callable(getter):
        try:
            return bool(getter(key, default))
        except Exception:
            pass
    return _safe_bool(_config_value(config, key, environment, default), default)


def _config_float(config: Any, key: str, environment: str = "live", default: float = 0.0) -> float:
    getter = getattr(config, "get_float_for_environment", None)
    if callable(getter):
        try:
            return _safe_float(getter(key, environment, default), default)
        except Exception:
            pass
    getter = getattr(config, "get_float", None)
    if callable(getter):
        try:
            return _safe_float(getter(key, default), default)
        except Exception:
            pass
    return _safe_float(_config_value(config, key, environment, default), default)


def enrich_buying_power_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    remaining = _safe_float(summary.get("remaining_buying_power"), _safe_float(summary.get("buying_power"), 0.0))
    net_liq = _safe_float(summary.get("net_liquidation"), 0.0)
    summary["remaining_buying_power"] = remaining
    summary["remaining_buying_power_pct_net_liq"] = (remaining / net_liq * 100.0) if net_liq > 0 else 0.0
    return summary


def estimate_entry_exposure(
    quantity: Any,
    entry_price: Any = None,
    take_profit_price: Any = None,
    stop_loss_price: Any = None,
    direction: str = "",
    order_type: str = "LMT",
) -> float:
    qty = abs(_safe_float(quantity, 0.0))
    if qty <= 0:
        return 0.0
    normalized_type = str(order_type or "LMT").strip().upper()
    price = _safe_float(entry_price, 0.0)
    if normalized_type == "MKT":
        candidates = [
            _safe_float(take_profit_price, 0.0),
            _safe_float(stop_loss_price, 0.0),
            price,
        ]
        price = max([candidate for candidate in candidates if candidate > 0] or [0.0])
    if price <= 0:
        return 0.0
    return qty * price


def build_buying_power_guard(
    summary: dict[str, Any] | None,
    config: Any = None,
    environment: str = "live",
    requested_exposure: Any = 0,
) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    summary_obj = enrich_buying_power_summary(dict(summary or {}))
    enabled = _config_bool(
        config,
        "ibkr_buying_power_guard_enabled",
        runtime_environment,
        bool(DEFAULT_BUYING_POWER_GUARD["ibkr_buying_power_guard_enabled"]),
    )
    remaining = _safe_float(summary_obj.get("remaining_buying_power"), _safe_float(summary_obj.get("buying_power"), 0.0))
    net_liq = _safe_float(summary_obj.get("net_liquidation"), 0.0)
    requested = max(0.0, _safe_float(requested_exposure, 0.0))
    remaining_after = remaining - requested
    warn_usd = max(
        0.0,
        _config_float(
            config,
            "ibkr_buying_power_warn_usd",
            runtime_environment,
            float(DEFAULT_BUYING_POWER_GUARD["ibkr_buying_power_warn_usd"]),
        ),
    )
    warn_pct = max(
        0.0,
        _config_float(
            config,
            "ibkr_buying_power_warn_pct_net_liq",
            runtime_environment,
            float(DEFAULT_BUYING_POWER_GUARD["ibkr_buying_power_warn_pct_net_liq"]),
        ),
    )
    block_usd = max(
        0.0,
        _config_float(
            config,
            "ibkr_buying_power_block_usd",
            runtime_environment,
            float(DEFAULT_BUYING_POWER_GUARD["ibkr_buying_power_block_usd"]),
        ),
    )
    block_pct = max(
        0.0,
        _config_float(
            config,
            "ibkr_buying_power_block_pct_net_liq",
            runtime_environment,
            float(DEFAULT_BUYING_POWER_GUARD["ibkr_buying_power_block_pct_net_liq"]),
        ),
    )
    warn_floor = max(warn_usd, net_liq * warn_pct / 100.0 if net_liq > 0 else 0.0)
    block_floor = max(block_usd, net_liq * block_pct / 100.0 if net_liq > 0 else 0.0)

    state = "ok"
    reason = "ok"
    if not enabled:
        reason = "buying_power_guard_disabled"
    elif remaining_after < block_floor:
        state = "blocked"
        reason = "buying_power_below_block_threshold"
    elif remaining_after < warn_floor:
        state = "warning"
        reason = "buying_power_below_warning_threshold"

    return {
        "enabled": bool(enabled),
        "basis": "buying_power",
        "environment": runtime_environment,
        "remaining": remaining,
        "net_liquidation": net_liq,
        "remaining_after": remaining_after,
        "requested_exposure": requested,
        "remaining_pct_net_liq": (remaining / net_liq * 100.0) if net_liq > 0 else 0.0,
        "remaining_after_pct_net_liq": (remaining_after / net_liq * 100.0) if net_liq > 0 else 0.0,
        "warn_floor": warn_floor,
        "block_floor": block_floor,
        "warn_usd": warn_usd,
        "warn_pct_net_liq": warn_pct,
        "block_usd": block_usd,
        "block_pct_net_liq": block_pct,
        "state": state,
        "reason": reason,
    }


__all__ = [
    "DEFAULT_BUYING_POWER_GUARD",
    "build_buying_power_guard",
    "enrich_buying_power_summary",
    "estimate_entry_exposure",
    "_config_bool",
    "_config_float",
]
