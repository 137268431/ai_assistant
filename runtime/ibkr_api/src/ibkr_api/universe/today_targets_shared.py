from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, first_defined, parse_boolean, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.market.timeframe_utils import ET, classify_session, format_cn_time, format_us_time, interval_to_chart_tf, ms_to_et


LIVE_ENVIRONMENT = "live"
WATCHLIST_ROLE_TRADE = "trade"
DEFAULT_TECHNICAL_STATE = "watch"
DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
DAILY_SCAN_STATE_DATE = "global"
DAILY_SCAN_SUMMARY_TIME_ET = "09:20"
MARKET_OPEN_CHECK_TIME_ET = "09:30"
INTRADAY_REFRESH_RULE = "5m close-driven"
TODAY_TARGET_STATUSES = {"active", "candidate"}

TimeStrings = Callable[[], dict[str, str]]


def truthy_target_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = to_text(value).lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def target_row_is_daily_scan_active(row: dict[str, Any] | None) -> bool:
    extra = parse_json_object((row or {}).get("extra"))
    source = to_text(extra.get("source")).lower()
    return source == "daily_scan" and truthy_target_value(extra.get("active_gate_passed"))


def effective_target_status(row: dict[str, Any] | None) -> str:
    status = to_text((row or {}).get("status")).lower()
    if status == "active" and not target_row_is_daily_scan_active(row):
        return "candidate"
    return status


def normalize_symbols(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        symbol = to_text(raw_item).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def escape_filter(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def format_et_date(ms: int) -> str:
    return ms_to_et(ms).strftime("%Y-%m-%d") if int(ms or 0) > 0 else ""


def format_et_datetime(ms: int) -> str:
    return format_us_time(int(ms or 0)) if int(ms or 0) > 0 else ""


def parse_et_datetime_ms(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def current_market_date(time_strings: TimeStrings) -> str:
    return to_text((time_strings() or {}).get("date"))


def get_priority_environment_rank(environment: Any, runtime_environment: str) -> int:
    normalized = to_text(environment).lower()
    if normalized == runtime_environment:
        return 2
    if normalized == "global":
        return 1
    if not normalized:
        return 0
    return -1


def build_symbol_filter(symbols: list[str]) -> str:
    normalized = normalize_symbols(symbols)
    if not normalized:
        return ""
    return "(" + " || ".join(f'symbol = "{escape_filter(symbol)}"' for symbol in normalized) + ")"


def build_bar_environment_filter(runtime_environment: str) -> str:
    data_environment = resolve_data_environment(runtime_environment)
    clauses = [f'environment = "{escape_filter(data_environment)}"']
    if data_environment == LIVE_ENVIRONMENT:
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})"


def normalize_watchlist_role(value: Any) -> str:
    return "market_monitor" if to_text(value).lower() == "market_monitor" else WATCHLIST_ROLE_TRADE


def load_watch_meta(pb: Any, environment: str, symbols: list[str]) -> dict[str, dict[str, str]]:
    normalized_symbols = set(normalize_symbols(symbols))
    if not normalized_symbols:
        return {}
    rows = pb.get_records(
        "watchlist",
        filter=f'environment = "{escape_filter(environment)}" || environment = "global" || environment = ""',
        sort="-updated",
        per_page=500,
        page=1,
    )
    meta: dict[str, dict[str, str]] = {}
    ranks: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        if symbol not in normalized_symbols:
            continue
        if normalize_watchlist_role(row.get("symbol_role")) != WATCHLIST_ROLE_TRADE:
            continue
        rank = get_priority_environment_rank(row.get("environment"), environment)
        if rank < 0:
            continue
        if symbol in ranks and ranks[symbol] > rank:
            continue
        ranks[symbol] = rank
        meta[symbol] = {
            "exchange": to_text(row.get("exchange")).upper(),
            "industry": to_text(row.get("industry")),
            "note": to_text(row.get("note")),
        }
    return meta


def load_daily_scan_state(pb: Any, environment: str) -> dict[str, Any]:
    try:
        record = pb.get_state(DAILY_SCAN_STATE_KEY, environment, date=DAILY_SCAN_STATE_DATE)
    except Exception:
        record = None
    if not isinstance(record, dict):
        return {}
    payload = ensure_object(record.get("data"))
    payload["result"] = parse_json_object(payload.get("result")) if not isinstance(payload.get("result"), dict) else dict(payload.get("result"))
    return payload


def load_records_for_symbols(
    pb: Any,
    collection: str,
    *,
    base_filter_parts: list[str],
    symbols: list[str],
    sort: str,
    max_pages: int,
    chunk_size: int = 24,
) -> list[dict[str, Any]]:
    normalized_symbols = normalize_symbols(symbols)
    if not normalized_symbols:
        return []
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(normalized_symbols), max(1, int(chunk_size or 1))):
        chunk = normalized_symbols[offset:offset + max(1, int(chunk_size or 1))]
        filter_parts = list(base_filter_parts)
        symbol_filter = build_symbol_filter(chunk)
        if symbol_filter:
            filter_parts.append(symbol_filter)
        rows.extend(
            dict(row)
            for row in (pb.get_all_records(collection, filter=" && ".join(filter_parts), sort=sort, max_pages=max_pages) or [])
            if isinstance(row, dict)
        )
    return rows


def indicator_snapshot(record: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(record or {})
    extra = parse_json_object(row.get("extra"))
    snapshot = dict(extra)
    for field in (
        "atr_pct",
        "ema_bullish",
        "ema_bearish",
        "crsi_bull_div",
        "crsi_bear_div",
        "obv_bull_div",
        "obv_bear_div",
        "fractal_bull",
        "fractal_bear",
        "ema_bull_touch",
        "ema_bear_touch",
        "trend_dir",
        "vwap_bullish",
    ):
        value = row.get(field)
        if value not in (None, ""):
            snapshot[field] = value
    return snapshot


def build_daily_change_fields(history: list[dict[str, Any]], current_close: float) -> dict[str, float]:
    prev_close = to_float(history[-1].get("close")) if len(history) >= 1 else 0.0
    prev_prev_close = to_float(history[-2].get("close")) if len(history) >= 2 else 0.0
    close_5 = to_float(history[-5].get("close")) if len(history) >= 5 else 0.0
    day_change_pct = ((current_close - prev_close) / prev_close) * 100 if prev_close and prev_close > 0 else 0.0
    prev_close_change_pct = ((prev_close - prev_prev_close) / prev_prev_close) * 100 if prev_prev_close and prev_prev_close > 0 else 0.0
    change_7d = ((current_close - close_5) / close_5) * 100 if close_5 and close_5 > 0 else 0.0
    return {
        "day_change_pct": round(day_change_pct, 2),
        "prev_close_change_pct": round(prev_close_change_pct, 2),
        "change_7d": round(change_7d, 2),
    }


def push_unique_text(items: list[str], value: Any) -> None:
    text = to_text(value)
    if text and text not in items:
        items.append(text)


def pick_reason_list(current_reasons: list[str] | None, fallback_reasons: list[str] | None) -> list[str]:
    merged: list[str] = []
    for source in (current_reasons or [], fallback_reasons or []):
        for item in source:
            push_unique_text(merged, item)
    return merged


def normalize_signal_status(value: Any) -> str:
    return to_text(value).lower()


def normalize_signal_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = parse_json_object(record.get("extra"))
    bar_time_ms = to_int(first_defined(record.get("bar_time_ms"), extra.get("bar_time_ms")), 0)
    created = to_text(record.get("created"))
    updated = to_text(record.get("updated")) or created
    created_ms = parse_et_datetime_ms(created)
    updated_ms = parse_et_datetime_ms(updated) or created_ms
    return {
        "symbol": to_text(record.get("symbol")).upper(),
        "signal_id": to_text(first_defined(record.get("signal_id"), extra.get("signal_id"))),
        "direction": to_text(first_defined(record.get("direction"), extra.get("direction"))).lower(),
        "signal": to_text(first_defined(record.get("signal"), extra.get("signal"))),
        "status": normalize_signal_status(first_defined(record.get("status"), extra.get("status"))),
        "bar_time_ms": bar_time_ms,
        "created": created,
        "updated": updated,
        "created_ms": created_ms,
        "updated_ms": updated_ms,
        "sort_ms": bar_time_ms or updated_ms or created_ms,
        "us_time": to_text(record.get("us_time")) or (format_et_datetime(bar_time_ms) if bar_time_ms > 0 else (updated or created)),
        "note": to_text(record.get("note")) or to_text(first_defined(extra.get("note"), extra.get("status_reason"))),
    }


def pick_latest_signal(current_signal: dict[str, Any] | None, next_signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not next_signal:
        return current_signal
    if not current_signal:
        return next_signal
    if to_int(next_signal.get("sort_ms"), 0) != to_int(current_signal.get("sort_ms"), 0):
        return next_signal if to_int(next_signal.get("sort_ms"), 0) > to_int(current_signal.get("sort_ms"), 0) else current_signal
    if to_int(next_signal.get("updated_ms"), 0) != to_int(current_signal.get("updated_ms"), 0):
        return next_signal if to_int(next_signal.get("updated_ms"), 0) > to_int(current_signal.get("updated_ms"), 0) else current_signal
    return next_signal


__all__ = [
    "DAILY_SCAN_SUMMARY_TIME_ET",
    "DEFAULT_TECHNICAL_STATE",
    "INTRADAY_REFRESH_RULE",
    "LIVE_ENVIRONMENT",
    "MARKET_OPEN_CHECK_TIME_ET",
    "TODAY_TARGET_STATUSES",
    "WATCHLIST_ROLE_TRADE",
    "build_bar_environment_filter",
    "build_daily_change_fields",
    "build_symbol_filter",
    "classify_session",
    "current_market_date",
    "escape_filter",
    "format_cn_time",
    "format_et_date",
    "format_et_datetime",
    "get_priority_environment_rank",
    "indicator_snapshot",
    "interval_to_chart_tf",
    "load_daily_scan_state",
    "load_records_for_symbols",
    "load_watch_meta",
    "normalize_signal_record",
    "normalize_signal_status",
    "normalize_symbols",
    "parse_et_datetime_ms",
    "pick_latest_signal",
    "pick_reason_list",
    "push_unique_text",
]
