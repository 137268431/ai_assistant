from __future__ import annotations

from typing import Any, Callable

from ibkr_compute.market.calendar import build_local_nyse_calendar_snapshot


ConfigValue = Callable[[str, str, str], Any]

TV_PRIMARY_SIGNAL_SOURCES = {"tv", "tradingview", "webhook_tv"}
FALSE_TEXT = {"0", "false", "no", "off", "disabled", "disable"}
TRUE_TEXT = {"1", "true", "yes", "on", "enabled", "enable"}


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _config_text(config_value: ConfigValue | None, key: str, environment: str, default: str = "") -> str:
    if not callable(config_value):
        return str(default or "")
    try:
        return _to_text(config_value(key, default, environment))
    except Exception:
        return str(default or "")


def _truthy_text(value: Any) -> bool:
    return _to_text(value).lower() in TRUE_TEXT


def _false_text(value: Any) -> bool:
    return _to_text(value).lower() in FALSE_TEXT


def legacy_target_universe_suppressed(
    config_value: ConfigValue | None,
    environment: str,
    *,
    broker_mode: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether legacy daily-scan/window-admission jobs should stay quiet."""
    environments = []
    for candidate in (environment, broker_mode):
        normalized = _to_text(candidate).lower()
        if normalized and normalized not in environments:
            environments.append(normalized)
    if not environments:
        environments.append("live")

    for env in environments:
        signal_source = _config_text(config_value, "ibkr_signal_source", env, "").lower()
        tv_slim_raw = _config_text(config_value, "ibkr_tv_primary_runtime_slim_enabled", env, "")
        if _truthy_text(tv_slim_raw) and (not signal_source or signal_source in TV_PRIMARY_SIGNAL_SOURCES):
            return True, "tv_primary_slim_mode", {
                "environment": env,
                "signal_source": signal_source,
                "ibkr_tv_primary_runtime_slim_enabled": tv_slim_raw,
            }

        technical_raw = _config_text(config_value, "ibkr_runtime_technical_pipeline_enabled", env, "")
        if _false_text(technical_raw):
            return True, "technical_pipeline_disabled", {
                "environment": env,
                "ibkr_runtime_technical_pipeline_enabled": technical_raw,
            }

    return False, "", {}


def legacy_target_market_closed(market_date: str) -> tuple[bool, str, dict[str, Any]]:
    snapshot = build_local_nyse_calendar_snapshot(market_date)
    if bool(snapshot.get("is_closed")) or snapshot.get("is_trading_day") is False:
        return True, "market_closed", {
            "market_date": _to_text(snapshot.get("market_date") or market_date),
            "closed_reason": _to_text(snapshot.get("closed_reason")) or "closed",
            "next_open_us": _to_text(snapshot.get("next_open_us")),
            "next_open_beijing": _to_text(snapshot.get("next_open_beijing")),
            "calendar_source": _to_text(snapshot.get("source")),
        }
    return False, "", {
        "market_date": _to_text(snapshot.get("market_date") or market_date),
        "calendar_source": _to_text(snapshot.get("source")),
    }


__all__ = [
    "legacy_target_market_closed",
    "legacy_target_universe_suppressed",
]
