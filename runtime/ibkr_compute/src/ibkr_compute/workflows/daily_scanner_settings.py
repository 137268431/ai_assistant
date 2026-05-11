"""Settings and rule-summary helpers for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.api.compute.runtime_state.universe import get_market_monitor_symbols
from ibkr_compute.api.market.screener.runtime import get_api_app
from ibkr_compute.universe.dynamic_admission import (
    DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE,
    normalize_admission_bool,
)

from .daily_scanner_constants import (
    DAILY_SCAN_LONG_PRIMARY_RULES,
    DAILY_SCAN_LONG_SECONDARY_RULES,
    DAILY_SCAN_PRIMARY_WEIGHT,
    DAILY_SCAN_READY_TIMEFRAME_BONUS,
    DAILY_SCAN_REASON_RULES,
    DAILY_SCAN_SECONDARY_WEIGHT,
    DAILY_SCAN_SHORT_PRIMARY_RULES,
    DAILY_SCAN_SHORT_SECONDARY_RULES,
    DEFAULT_DAY_GAIN_TRIGGER_PCT,
    DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
    DEFAULT_MIN_ATR_PCT,
    DEFAULT_MIN_AVG_10D_VOLUME,
    DEFAULT_MIN_PREMARKET_VOLUME,
    DEFAULT_SCAN_TIME_ET,
)
from .daily_scanner_support import _safe_float, _safe_int


def _cfg_bool(api_app, key: str, environment: str, default: bool) -> bool:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_bool_for_environment"):
        try:
            return bool(cfg.get_bool_for_environment(key, environment, default))
        except Exception:
            return default
    if cfg is not None and hasattr(cfg, "get_for_environment"):
        try:
            return normalize_admission_bool(cfg.get_for_environment(key, environment, default), default)
        except Exception:
            return default
    return default


def _cfg_float(api_app, key: str, environment: str, default: float) -> float:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_float_for_environment"):
        try:
            return float(cfg.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    if cfg is not None and hasattr(cfg, "get_for_environment"):
        try:
            return _safe_float(cfg.get_for_environment(key, environment, str(default)), default)
        except Exception:
            return float(default)
    return float(default)


def _load_scan_settings(
    environment: str,
    *,
    api_app_getter=None,
    monitor_symbols_getter=None,
) -> dict:
    api_app = (api_app_getter or get_api_app)()
    load_monitor_symbols = monitor_symbols_getter or get_market_monitor_symbols
    runtime_environment = str(environment or "live").strip().lower() or "live"
    api_app.cfg.refresh()
    monitor_count = len(load_monitor_symbols(runtime_environment))
    target_limit_raw = api_app.cfg.get_int_for_environment(
        "ibkr_target_subscription_limit",
        runtime_environment,
        80,
    )
    total_limit_raw = api_app.cfg.get_int_for_environment(
        "ibkr_total_subscription_limit",
        runtime_environment,
        80,
    )
    target_limit = max(0, int(target_limit_raw or 0))
    total_limit = max(0, int(total_limit_raw or 0))
    trade_budget: int | None = target_limit if target_limit > 0 else None
    if total_limit > 0:
        total_budget = max(0, total_limit - monitor_count)
        trade_budget = total_budget if trade_budget is None else min(trade_budget, total_budget)

    return {
        "scan_time_et": str(
            api_app.cfg.get_for_environment(
                "ibkr_daily_scan_time_et",
                runtime_environment,
                DEFAULT_SCAN_TIME_ET,
            )
            or DEFAULT_SCAN_TIME_ET
        ).strip()
        or DEFAULT_SCAN_TIME_ET,
        "min_avg_10d_volume": max(
            0,
            _safe_int(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_avg_10d_volume",
                    runtime_environment,
                    str(DEFAULT_MIN_AVG_10D_VOLUME),
                ),
                DEFAULT_MIN_AVG_10D_VOLUME,
            ),
        ),
        "min_atr_pct": max(
            0.0,
            _safe_float(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_atr_pct",
                    runtime_environment,
                    str(DEFAULT_MIN_ATR_PCT),
                ),
                DEFAULT_MIN_ATR_PCT,
            ),
        ),
        "min_abs_day_change_pct": max(
            0.0,
            _safe_float(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_abs_day_change_pct",
                    runtime_environment,
                    str(DEFAULT_MIN_ABS_DAY_CHANGE_PCT),
                ),
                DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
            ),
        ),
        "min_premarket_volume": max(
            0,
            _safe_int(
                api_app.cfg.get_for_environment(
                    "ibkr_daily_scan_min_premarket_volume",
                    runtime_environment,
                    str(DEFAULT_MIN_PREMARKET_VOLUME),
                ),
                DEFAULT_MIN_PREMARKET_VOLUME,
            ),
        ),
        "monitor_count": monitor_count,
        "target_subscription_limit": target_limit,
        "total_subscription_limit": total_limit,
        "trade_subscription_budget": trade_budget,
        "dynamic_admission_enabled": _cfg_bool(
            api_app,
            "ibkr_dynamic_admission_enabled",
            runtime_environment,
            True,
        ),
        "dynamic_admission_min_score": max(
            0.0,
            _cfg_float(
                api_app,
                "ibkr_dynamic_admission_min_score",
                runtime_environment,
                DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE,
            ),
        ),
        "day_gain_trigger_enabled": _cfg_bool(
            api_app,
            "ibkr_daily_scan_day_gain_trigger_enabled",
            runtime_environment,
            True,
        ),
        "day_gain_trigger_pct": max(
            0.0,
            _cfg_float(
                api_app,
                "ibkr_daily_scan_day_gain_trigger_pct",
                runtime_environment,
                DEFAULT_DAY_GAIN_TRIGGER_PCT,
            ),
        ),
    }


def build_daily_scan_rule_summary(
    environment: str | None = None,
    *,
    api_app_getter=None,
    monitor_symbols_getter=None,
    settings_loader=None,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if settings_loader is None:
        settings = _load_scan_settings(
            runtime_environment,
            api_app_getter=api_app_getter,
            monitor_symbols_getter=monitor_symbols_getter,
        )
    else:
        settings = settings_loader(runtime_environment)
    trade_budget = settings["trade_subscription_budget"]
    budget_label = "unlimited" if trade_budget is None else str(int(trade_budget))
    cfg = getattr((api_app_getter or get_api_app)(), "cfg", None)
    try:
        completeness_blocking = bool(
            cfg is not None
            and hasattr(cfg, "get_bool_for_environment")
            and cfg.get_bool_for_environment(
                "ibkr_daily_scan_data_completeness_blocking_enabled",
                runtime_environment,
                True,
            )
        )
    except Exception:
        completeness_blocking = True
    return {
        "primary_weight": DAILY_SCAN_PRIMARY_WEIGHT,
        "secondary_weight": DAILY_SCAN_SECONDARY_WEIGHT,
        "ready_timeframe_bonus": DAILY_SCAN_READY_TIMEFRAME_BONUS,
        "long_primary": [label for _, _, label in DAILY_SCAN_LONG_PRIMARY_RULES],
        "short_primary": [label for _, _, label in DAILY_SCAN_SHORT_PRIMARY_RULES],
        "long_secondary": [label for _, _, label in DAILY_SCAN_LONG_SECONDARY_RULES],
        "short_secondary": [label for _, _, label in DAILY_SCAN_SHORT_SECONDARY_RULES],
        "metric_secondary": [
            f"day_change_pct >= {settings.get('day_gain_trigger_pct', DEFAULT_DAY_GAIN_TRIGGER_PCT)}"
        ] if bool(settings.get("day_gain_trigger_enabled", True)) else [],
        "tie_behavior": "long_votes == short_votes => direction_bias=neutral, score=0",
        "final_bonus": "direction_bias 非 neutral 时额外加上 ready_timeframes_count",
        "reason_fields": [label for _, label in DAILY_SCAN_REASON_RULES],
        "scan_time_et": settings["scan_time_et"],
        "quality_gates": {
            "dynamic_admission_enabled": bool(settings.get("dynamic_admission_enabled", False)),
            "dynamic_admission_score_gte": settings.get(
                "dynamic_admission_min_score",
                DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE,
            ),
            "avg_10d_volume_gte": settings["min_avg_10d_volume"],
            "atr_pct_gte": settings["min_atr_pct"],
            "abs_day_change_pct_gte": settings["min_abs_day_change_pct"],
            "premarket_volume_gte": settings["min_premarket_volume"],
            "data_completeness_blocking": completeness_blocking,
        },
        "subscription_budget": {
            "trade_budget": budget_label,
            "total_limit": int(settings["total_subscription_limit"] or 0),
            "monitor_count": int(settings["monitor_count"] or 0),
        },
    }
