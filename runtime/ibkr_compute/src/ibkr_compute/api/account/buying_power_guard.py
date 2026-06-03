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


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return float(number)


def _summary_lookup(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in summary.items()}


def _summary_number(summary: dict[str, Any], *keys: str) -> float | None:
    lookup = _summary_lookup(summary)
    for key in keys:
        raw_value = lookup.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("amount", "value"):
                number = _optional_float(lowered.get(field))
                if number is not None:
                    return number
        else:
            number = _optional_float(raw_value)
            if number is not None:
                return number
    return None


def _summary_has_any(summary: dict[str, Any], *keys: str) -> bool:
    lookup = _summary_lookup(summary)
    return any(str(key).strip().lower() in lookup for key in keys)


def _summary_has_substantive_snapshot(summary: dict[str, Any]) -> bool:
    lookup = _summary_lookup(summary)
    for key in (
        "account_type",
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "equity_with_loan",
        "gross_position_value",
        "total_cash_value",
        "initial_margin",
        "maintenance_margin",
    ):
        raw_value = lookup.get(key)
        if raw_value in (None, ""):
            continue
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            raw_value = lowered.get("amount", lowered.get("value"))
        number = _optional_float(raw_value)
        if number is None:
            return True
        if number != 0.0:
            return True
    return False


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
    remaining = _summary_number(summary, "remaining_buying_power", "buying_power")
    net_liq = _summary_number(summary, "net_liquidation") or 0.0
    summary["remaining_buying_power"] = remaining
    summary["remaining_buying_power_pct_net_liq"] = (
        (remaining / net_liq * 100.0) if remaining is not None and net_liq > 0 else None
    )
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
    remaining_value = _summary_number(summary_obj, "remaining_buying_power", "buying_power")
    remaining_available = remaining_value is not None
    remaining = float(remaining_value) if remaining_available else None
    net_liq = _summary_number(summary_obj, "net_liquidation") or 0.0
    snapshot_unavailable = not summary_obj or (
        remaining_available and remaining == 0.0 and not _summary_has_substantive_snapshot(summary_obj)
    )
    requested = max(0.0, _safe_float(requested_exposure, 0.0))
    remaining_after = remaining - requested if remaining is not None else None
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
    elif snapshot_unavailable:
        state = "unavailable"
        reason = "account_snapshot_unavailable"
        remaining_available = False
        remaining = None
        remaining_after = None
    elif not remaining_available:
        state = "unavailable"
        reason = (
            "account_snapshot_unavailable"
            if not _summary_has_any(
                summary_obj,
                "account_code",
                "account_type",
                "net_liquidation",
                "available_funds",
                "excess_liquidity",
                "equity_with_loan",
                "gross_position_value",
                "total_cash_value",
                "initial_margin",
                "maintenance_margin",
            )
            else "buying_power_unavailable"
        )
    elif remaining_after is not None and remaining_after < block_floor:
        state = "blocked"
        reason = "buying_power_below_block_threshold"
    elif remaining_after is not None and remaining_after < warn_floor:
        state = "warning"
        reason = "buying_power_below_warning_threshold"

    return {
        "enabled": bool(enabled),
        "available": bool(remaining_available),
        "basis": "buying_power",
        "environment": runtime_environment,
        "remaining": remaining,
        "net_liquidation": net_liq,
        "remaining_after": remaining_after,
        "requested_exposure": requested,
        "remaining_pct_net_liq": (remaining / net_liq * 100.0) if remaining is not None and net_liq > 0 else None,
        "remaining_after_pct_net_liq": (
            (remaining_after / net_liq * 100.0) if remaining_after is not None and net_liq > 0 else None
        ),
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
