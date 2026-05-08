from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlencode

from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    TODAY_TARGET_STATUSES,
    build_bar_environment_filter,
    classify_session,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_datetime,
    indicator_snapshot,
    interval_to_chart_tf,
    load_records_for_symbols,
    normalize_signal_record,
    normalize_symbols,
    pick_latest_signal,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.active_window_admission import (
    build_active_window_trace_for_bars,
    build_active_window_admission_item,
)
from ibkr_compute.market.timeframe_utils import normalize_interval


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]

DEFAULT_SIGNAL_WINDOW_MAX_BARS = 12
DEFAULT_LIMIT = 80
TIMELINE_WARMUP_BARS = 300
TIMELINE_TODAY_MAX_PAGES = 4
TIMELINE_WARMUP_MAX_PAGES = 2
SUPPORTED_ENVIRONMENTS = {"live", "paper"}
SUPPORTED_STATUSES = {"active", "candidate", "all"}

def _build_chart_trace_url(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> str:
    query = urlencode(
        {
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": "true",
            "include_trace": "true",
        }
    )
    return f"/ibkr_chart.html?{query}"


def _build_chart_trace_request(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    return {
        "path": "/api/custom/ibkr/proxy",
        "method": "POST",
        "body": {
            "action": "chart/timeline",
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": True,
            "include_trace": True,
        },
    }


def _build_trace_for_symbol(
    *,
    environment: str,
    symbol: str,
    bars: list[dict[str, Any]],
    signal_window_max_bars: int,
) -> dict[str, Any]:
    del environment
    return build_active_window_trace_for_bars(
        symbol=symbol,
        interval="5m",
        bars=bars,
        signal_window_max_bars=signal_window_max_bars,
    )

__all__ = [name for name in globals() if not name.startswith("__")]
