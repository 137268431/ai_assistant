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
        return set()
    trade_budget = _get_trade_subscription_budget(api_app, runtime_environment)
    active_rows = [
        row
        for row in rows
        if str(row.get("symbol", "")).strip().upper() not in market_monitor_symbols
    ]
    prioritized_rows = [
        row for row in active_rows if _target_row_is_manual(row)
    ] + [
        row for row in active_rows if not _target_row_is_manual(row)
    ]

    selected_symbols = []
    seen = set()
    for row in prioritized_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        if trade_budget is not None and len(selected_symbols) >= trade_budget:
            break
        selected_symbols.append(symbol)
        seen.add(symbol)
    return set(selected_symbols)


def get_signal_generator_params(environment: str) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    market_monitor_symbols = sorted(get_market_monitor_symbols(environment))
    signal_enabled_symbols = sorted(get_active_trade_symbols(environment))
    params = {
        "market_monitor_symbols": ",".join(market_monitor_symbols),
        "signal_enabled_symbols": ",".join(signal_enabled_symbols),
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
    "get_active_trade_symbols",
    "get_market_monitor_symbols",
    "get_signal_generator_params",
]
