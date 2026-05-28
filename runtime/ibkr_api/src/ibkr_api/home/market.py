from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.home.common import escape_filter, load_records, normalize_symbols, to_float, to_int, to_text
from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.broker_mode import normalize_broker_mode


ConfigValue = Callable[[str, str, str], str]
RequestJson = Callable[..., dict[str, Any]]
TimeStrings = Callable[[], dict[str, str]]


def _format_age_seconds(value: Any) -> str:
    seconds = to_float(value)
    if seconds is None:
        return "--"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{round(seconds)}s"


def _compute_pct_change(current_value: Any, reference_value: Any) -> float | None:
    current = to_float(current_value)
    reference = to_float(reference_value)
    if current is None or reference is None or reference <= 0:
        return None
    return round(((current - reference) / reference) * 100, 2)


def _record_date(row: dict[str, Any]) -> str:
    us_time = to_text(row.get("us_time"))
    if us_time:
        return us_time.split(" ")[0]
    return ""


def _build_daily_metrics(current_close: float | None, daily_rows: list[dict[str, Any]], market_date: str) -> dict[str, Any]:
    if current_close is None or current_close <= 0:
        return {}
    normalized: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for row in daily_rows or []:
        close = to_float(row.get("close"))
        date = _record_date(row)
        if close is None or close <= 0 or not date or date in seen_dates:
            continue
        seen_dates.add(date)
        normalized.append({"close": close, "date": date})
    history = [row for row in normalized if not market_date or row["date"] < market_date]
    if not history:
        return {}
    return {
        "day_change_pct": _compute_pct_change(current_close, history[0].get("close")),
        "change_7d": _compute_pct_change(current_close, history[4].get("close") if len(history) > 4 else None),
    }


def _load_monitor_symbols(
    pb: Any,
    *,
    data_environment: str,
    config_value: ConfigValue,
) -> tuple[list[str], list[dict[str, Any]]]:
    configured = normalize_symbols(config_value("ibkr_market_ws_symbols", "SPY,QQQ,VIX", data_environment))
    if data_environment == "live":
        scope_filter = '(environment = "live" || environment = "global" || environment = "")'
    else:
        env = escape_filter(data_environment)
        scope_filter = f'(environment = "{env}" || environment = "global")'
    watch_rows = load_records(
        pb,
        "watchlist",
        filter_expr=f'{scope_filter} && symbol_role = "market_monitor"',
        sort="-updated",
        per_page=100,
        max_pages=1,
    )

    priority = {"": 0, "global": 1, data_environment: 2}
    merged: dict[str, dict[str, Any]] = {}
    applied: dict[str, int] = {}
    for symbol in configured:
        merged[symbol] = {"symbol": symbol, "environment": "config", "symbol_role": "market_monitor"}
        applied[symbol] = -1
    for row in watch_rows:
        symbol = to_text(row.get("symbol")).upper()
        if not symbol:
            continue
        env = to_text(row.get("environment")).lower()
        rank = priority.get(env, -1)
        if rank < 0:
            continue
        if symbol in applied and applied[symbol] > rank:
            continue
        applied[symbol] = rank
        merged[symbol] = row

    remaining = set(merged)
    ordered = [symbol for symbol in configured if symbol in remaining]
    for symbol in ordered:
        remaining.discard(symbol)
    ordered.extend(sorted(remaining))
    return ordered, watch_rows


def _load_daily_rows(pb: Any, *, symbols: list[str], data_environment: str) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    symbol_filter = " || ".join(f'symbol = "{escape_filter(symbol)}"' for symbol in symbols)
    rows = load_records(
        pb,
        "ibkr_bars",
        filter_expr=f'environment = "{escape_filter(data_environment)}" && interval = "1d" && ({symbol_filter})',
        sort="-bar_time_ms",
        per_page=min(500, max(50, len(symbols) * 8)),
        max_pages=1,
    )
    grouped: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    seen_dates: dict[str, set[str]] = {symbol: set() for symbol in symbols}
    for row in rows:
        symbol = to_text(row.get("symbol")).upper()
        if symbol not in grouped:
            continue
        date = _record_date(row) or str(to_int(row.get("bar_time_ms"), 0))
        if date in seen_dates[symbol]:
            continue
        seen_dates[symbol].add(date)
        if len(grouped[symbol]) < 8:
            grouped[symbol].append(row)
    return grouped


def _fetch_quotes(
    *,
    symbols: list[str],
    data_environment: str,
    request_json_request: RequestJson,
    runtime_base_url: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not symbols or not callable(request_json_request) or not to_text(runtime_base_url):
        return {}, {"ok": False, "error": "runtime_quotes_unavailable"}
    result = request_json_request(
        "GET",
        runtime_base_url,
        "/ibkr/quotes",
        params={"symbols": ",".join(symbols), "environment": data_environment},
        timeout=8.0,
    )
    payload = result.get("payload") if isinstance(result, dict) else {}
    items = payload.get("items") if isinstance(payload, dict) else []
    quote_map = {
        to_text(item.get("symbol")).upper(): dict(item)
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and to_text(item.get("symbol"))
    }
    return quote_map, result if isinstance(result, dict) else {"ok": False}


def _is_fresh_realtime_quote(quote: dict[str, Any]) -> bool:
    price = to_float(quote.get("last_price"))
    age = to_float(quote.get("quote_age_s"))
    return price is not None and price > 0 and age is not None and age <= 600 and quote.get("quote_fallback") is not True


def _build_market_item(symbol: str, *, quote: dict[str, Any], daily_rows: list[dict[str, Any]], market_date: str) -> dict[str, Any]:
    quote_price = to_float(quote.get("last_price"))
    daily_close = to_float(daily_rows[0].get("close")) if daily_rows else None
    source_kind = "none"
    display_price: float | None = None
    update_text = "--"
    data_source_text = "NO DATA"
    freshness_text = "--"
    realtime = False

    if _is_fresh_realtime_quote(quote):
        realtime = True
        source_kind = "quote"
        display_price = quote_price
        update_text = to_text(quote.get("last_update")) or "--"
        age_text = _format_age_seconds(quote.get("quote_age_s"))
        data_source_text = f"RT {age_text}"
        freshness_text = f"quote {age_text} old" if age_text != "--" else "quote age --"
    elif quote_price is not None and quote_price > 0:
        source_kind = to_text(quote.get("fallback_source") or "storage") or "storage"
        display_price = quote_price
        update_text = to_text(quote.get("us_time") or quote.get("last_update")) or "--"
        data_source_text = "BAR 5m + 1D" if daily_rows else "BAR 5m"
        data_age = to_float(quote.get("data_age_s"))
        freshness_text = f"{round(data_age / 60)}m old" if data_age is not None else "--"
    elif daily_close is not None and daily_close > 0:
        source_kind = "daily"
        display_price = daily_close
        update_text = to_text(daily_rows[0].get("us_time")) or "--"
        data_source_text = "BAR 1D"
        bar_time_ms = to_int(daily_rows[0].get("bar_time_ms"), 0)
        freshness_text = f"{round(max(0, int(time.time() * 1000) - bar_time_ms) / 60000)}m old" if bar_time_ms > 0 else "--"

    fallback_metrics = _build_daily_metrics(display_price, daily_rows, market_date)
    change_pct = to_float(quote.get("day_change_pct"))
    if change_pct is None:
        change_pct = to_float(fallback_metrics.get("day_change_pct"))
    change_7d = to_float(fallback_metrics.get("change_7d"))

    return {
        "symbol": symbol,
        "display_price": round(display_price, 4) if display_price is not None else None,
        "change_pct": change_pct,
        "change_7d": change_7d,
        "source_kind": source_kind,
        "data_source_text": data_source_text,
        "update_text": update_text,
        "freshness_text": freshness_text,
        "realtime": realtime,
    }


def build_home_market_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    config_value: ConfigValue,
    request_json_request: RequestJson,
    runtime_base_url: str,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    fallback_date = to_text((time_strings() or {}).get("date")) or time.strftime("%Y-%m-%d")
    market_date = to_text(request_payload.get("market_date") or request_payload.get("date")) or fallback_date
    try:
        parse_market_date_bounds_ms(market_date)
    except Exception:
        market_date = fallback_date

    symbols, watch_rows = _load_monitor_symbols(pb, data_environment=data_environment, config_value=config_value)
    if not symbols:
        return {
            "ok": True,
            "source": "ibkr-api",
            "broker_mode": normalize_broker_mode(broker_mode, "paper"),
            "data_environment": data_environment,
            "market_data_mode": data_environment,
            "market_date": market_date,
            "symbols": [],
            "items": [],
            "has_data": False,
            "realtime_count": 0,
            "fallback_count": 0,
            "stale_symbols": [],
            "watchlist_count": len(watch_rows),
        }, 200

    quote_map, quote_result = _fetch_quotes(
        symbols=symbols,
        data_environment=data_environment,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
    )
    daily_by_symbol = _load_daily_rows(pb, symbols=symbols, data_environment=data_environment)
    items = [
        _build_market_item(symbol, quote=quote_map.get(symbol, {}), daily_rows=daily_by_symbol.get(symbol, []), market_date=market_date)
        for symbol in symbols
    ]
    realtime_count = len([item for item in items if item.get("realtime")])
    fallback_count = max(len(symbols) - realtime_count, 0)
    stale_symbols = [item["symbol"] for item in items if item.get("display_price") is not None and not item.get("realtime")]
    has_data = any(item.get("display_price") is not None for item in items)

    return {
        "ok": True,
        "source": "ibkr-api",
        "broker_mode": normalize_broker_mode(broker_mode, "paper"),
        "data_environment": data_environment,
        "market_data_mode": data_environment,
        "market_date": market_date,
        "symbols": symbols,
        "items": items,
        "has_data": has_data,
        "realtime_count": realtime_count,
        "fallback_count": fallback_count,
        "stale_symbols": stale_symbols,
        "watchlist_count": len(watch_rows),
        "quote_upstream": {
            "ok": bool(quote_result.get("ok")),
            "status_code": to_int(quote_result.get("status_code"), 0),
            "elapsed_ms": quote_result.get("elapsed_ms"),
            "error": to_text(quote_result.get("error")),
        },
        "computed_at_ms": int(time.time() * 1000),
    }, 200


__all__ = ["build_home_market_response"]
