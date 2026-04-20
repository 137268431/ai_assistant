from __future__ import annotations

from ibkr_compute.api.market.support import coerce_float, coerce_int, parse_json_object
from ibkr_compute.api.legacy.views import build_legacy_pb_proxy_response
from ibkr_compute.api.support.market_time import current_market_date, get_environment_time_window, resolve_initial_signal_state
from ibkr_compute.api.support.symbols import (
    VALID_WATCHLIST_SYMBOL_ROLES,
    WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR,
    WATCHLIST_SYMBOL_ROLE_TRADE,
    build_bar_environment_filter,
    build_symbol_filter,
    normalize_bar_environment,
    normalize_symbol_csv,
    normalize_symbols,
    normalize_watchlist_symbol_role,
)
from ibkr_compute.market.timeframe_utils import (
    HIGHER_INTERVALS,
    build_runtime_timestamps,
    classify_session,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
)


SUPPORT_EXPORTS = {
    "VALID_WATCHLIST_SYMBOL_ROLES": VALID_WATCHLIST_SYMBOL_ROLES,
    "WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR": WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR,
    "WATCHLIST_SYMBOL_ROLE_TRADE": WATCHLIST_SYMBOL_ROLE_TRADE,
    "HIGHER_INTERVALS": HIGHER_INTERVALS,
    "build_bar_environment_filter": build_bar_environment_filter,
    "build_legacy_pb_proxy_response": build_legacy_pb_proxy_response,
    "build_runtime_timestamps": build_runtime_timestamps,
    "build_symbol_filter": build_symbol_filter,
    "classify_session": classify_session,
    "coerce_float": coerce_float,
    "coerce_int": coerce_int,
    "current_market_date": current_market_date,
    "get_environment_time_window": get_environment_time_window,
    "interval_to_chart_tf": interval_to_chart_tf,
    "interval_to_ms": interval_to_ms,
    "ms_to_et": ms_to_et,
    "normalize_bar_environment": normalize_bar_environment,
    "normalize_symbol_csv": normalize_symbol_csv,
    "normalize_symbols": normalize_symbols,
    "normalize_watchlist_symbol_role": normalize_watchlist_symbol_role,
    "parse_json_object": parse_json_object,
    "resolve_initial_signal_state": resolve_initial_signal_state,
}
