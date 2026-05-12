from __future__ import annotations

import json
import traceback

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.market.screener import load_effective_watchlist
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS


MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}


def _safe_extra(row: dict | None) -> dict:
    payload = (row or {}).get("extra")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _target_row_is_manual(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source.startswith("manual_"):
        return True
    return source in MANUAL_TARGET_SOURCES


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _target_row_is_daily_scan_active(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    return source == "daily_scan" and _truthy(extra.get("active_gate_passed"))


def _get_trade_subscription_budget(api_app, environment: str) -> int | None:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    target_limit = max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 80) or 0),
    )
    total_limit = max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_total_subscription_limit", runtime_environment, 80) or 0),
    )
    trade_budget: int | None = target_limit if target_limit > 0 else None
    if total_limit > 0:
        remaining_budget = max(0, total_limit - len(get_market_monitor_symbols(runtime_environment)))
        trade_budget = remaining_budget if trade_budget is None else min(trade_budget, remaining_budget)
    return trade_budget


def _load_selected_active_trade_target_rows(environment: str, market_date: str | None = None) -> list[dict]:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    target_date = str(market_date or api_app.current_market_date()).strip() or api_app.current_market_date()
    market_monitor_symbols = get_market_monitor_symbols(runtime_environment)
    try:
        rows = api_app.pb.get_all_records(
            "ibkr_targets",
            filter=(
                f'date = "{target_date}" && '
                f'environment = "{runtime_environment}" && '
                'status = "active"'
            ),
            sort="-score,-updated",
            max_pages=10,
        )
    except Exception:
        traceback.print_exc()
        return []
    trade_budget = _get_trade_subscription_budget(api_app, runtime_environment)
    active_rows = [
        row
        for row in rows
        if str(row.get("symbol", "")).strip().upper() not in market_monitor_symbols
        and _target_row_is_daily_scan_active(row)
    ]
    prioritized_rows = list(active_rows)

    selected_rows: list[dict] = []
    seen = set()
    for row in prioritized_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        if trade_budget is not None and len(selected_rows) >= trade_budget:
            break
        selected_rows.append(row)
        seen.add(symbol)
    return selected_rows


def get_market_monitor_symbols(environment: str) -> set[str]:
    api_app = _api_app()
    watchlist_map = load_effective_watchlist(environment)
    watchlist_symbols = {
        symbol
        for symbol, row in watchlist_map.items()
        if api_app.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))
        == api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    }
    if not api_app.cfg.get_bool_for_environment("ibkr_market_ws_enabled", environment, True):
        return watchlist_symbols
    configured_symbols = set(
        api_app.normalize_symbol_csv(
            api_app.cfg.get_for_environment("ibkr_market_ws_symbols", environment, "SPY,QQQ,VIX")
        )
    )
    return watchlist_symbols.union(configured_symbols)


def get_active_trade_symbols(environment: str, market_date: str | None = None) -> set[str]:
    return {
        str(row.get("symbol", "")).strip().upper()
        for row in _load_selected_active_trade_target_rows(environment, market_date)
        if str(row.get("symbol", "")).strip()
    }


def get_active_target_direction_biases(environment: str, market_date: str | None = None) -> dict[str, str]:
    biases: dict[str, str] = {}
    for row in _load_selected_active_trade_target_rows(environment, market_date):
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        direction = str(row.get("direction_bias", "") or "").strip().lower()
        biases[symbol] = direction
    return biases


def get_signal_generator_params(environment: str) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    market_monitor_symbols = sorted(get_market_monitor_symbols(environment))
    selected_target_rows = _load_selected_active_trade_target_rows(runtime_environment)
    signal_enabled_symbols = sorted(
        {
            str(row.get("symbol", "")).strip().upper()
            for row in selected_target_rows
            if str(row.get("symbol", "")).strip()
        }
    )
    target_direction_bias_by_symbol: dict[str, str] = {}
    target_strategy_policy_by_symbol: dict[str, dict] = {}
    target_symbol_profile_by_symbol: dict[str, dict] = {}
    for row in selected_target_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        target_direction_bias_by_symbol[symbol] = str(row.get("direction_bias", "") or "").strip().lower()
        extra = _safe_extra(row)
        strategy_policy = extra.get("strategy_policy") if isinstance(extra.get("strategy_policy"), dict) else {}
        symbol_profile = extra.get("symbol_profile") if isinstance(extra.get("symbol_profile"), dict) else {}
        if strategy_policy:
            target_strategy_policy_by_symbol[symbol] = dict(strategy_policy)
        if symbol_profile:
            target_symbol_profile_by_symbol[symbol] = dict(symbol_profile)
    params = {
        "market_monitor_symbols": ",".join(market_monitor_symbols),
        "signal_enabled_symbols": ",".join(signal_enabled_symbols),
        "target_strategy_policy_enabled": bool(
            api_app.cfg.get_bool_for_environment("ibkr_target_strategy_policy_enabled", runtime_environment, False)
        ),
        "target_direction_bias_by_symbol": json.dumps(target_direction_bias_by_symbol, ensure_ascii=False),
        "target_strategy_policy_by_symbol": json.dumps(target_strategy_policy_by_symbol, ensure_ascii=False),
        "target_symbol_profile_by_symbol": json.dumps(target_symbol_profile_by_symbol, ensure_ascii=False),
    }
    for key, default in DEFAULT_PARAMS.items():
        if isinstance(default, bool):
            getter = getattr(api_app.cfg, "get_bool_for_environment", None)
            value = getter(key, runtime_environment, default) if getter else default
        elif isinstance(default, int):
            getter = getattr(api_app.cfg, "get_int_for_environment", None)
            value = getter(key, runtime_environment, default) if getter else default
        elif isinstance(default, float):
            getter = getattr(api_app.cfg, "get_float_for_environment", None)
            value = getter(key, runtime_environment, default) if getter else default
        else:
            getter = getattr(api_app.cfg, "get_for_environment", None)
            value = getter(key, runtime_environment, str(default)) if getter else default
        params[key] = value
    return params


__all__ = [
    "get_active_target_direction_biases",
    "get_active_trade_symbols",
    "get_market_monitor_symbols",
    "get_signal_generator_params",
]
