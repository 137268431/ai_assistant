from __future__ import annotations

import traceback

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.market.screener import load_effective_watchlist


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
    return {
        str(row.get("symbol", "")).strip().upper()
        for row in rows
        if str(row.get("symbol", "")).strip()
    }


def get_signal_generator_params(environment: str) -> dict:
    market_monitor_symbols = sorted(get_market_monitor_symbols(environment))
    signal_enabled_symbols = sorted(get_active_trade_symbols(environment))
    return {
        "market_monitor_symbols": ",".join(market_monitor_symbols),
        "signal_enabled_symbols": ",".join(signal_enabled_symbols),
    }


__all__ = [
    "get_active_trade_symbols",
    "get_market_monitor_symbols",
    "get_signal_generator_params",
]
