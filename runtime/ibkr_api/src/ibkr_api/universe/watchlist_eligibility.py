from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.orders.values import ensure_object, to_int, to_text


RequestJsonRequest = Callable[..., dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]

ET = ZoneInfo("America/New_York")

DEFAULT_WINDOW_TRADING_DAYS = 10
MIN_EVALUATED_DAYS = 3
MIN_PASS_DAYS = 2
MIN_PASS_RATE = 0.6
MAX_WINDOW_TRADING_DAYS = 20

MIN_AVG_10D_VOLUME = 100_000
MIN_PREMARKET_VOLUME = 5_000
MIN_ATR_PCT = 0.15
MIN_ABS_DAY_CHANGE_PCT = 1.0

SIMILAR_SYMBOL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("GOOG", "GOOGL"),
)
CANONICAL_SYMBOL_BY_GROUP = {
    "GOOG,GOOGL": "GOOGL",
}


def _normalize_symbols(value: Any) -> list[str]:
    raw_items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        symbol = to_text(raw_item).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _metric_value(row: dict[str, Any], key: str) -> float:
    if key == "atr_pct":
        return abs(_safe_float(row.get(key)))
    if key == "day_change_pct":
        return abs(_safe_float(row.get(key)))
    return _safe_float(row.get(key))


def _format_count(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(round(value, 2))


def _build_environment_filter(environment: str, *, include_legacy_empty: bool = True) -> str:
    safe_environment = to_text(environment).lower()
    clauses = [f'environment = "{safe_environment}"']
    if include_legacy_empty and safe_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def _parse_us_date(row: dict[str, Any]) -> str:
    us_time = to_text((row or {}).get("us_time"))
    if len(us_time) >= 10:
        return us_time[:10]
    return ""


def _load_recent_market_dates(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    window_trading_days: int,
    escape_filter_string: EscapeFilterString,
) -> list[str]:
    requested = max(1, min(MAX_WINDOW_TRADING_DAYS, int(window_trading_days or DEFAULT_WINDOW_TRADING_DAYS)))
    candidate_symbols = list(dict.fromkeys([*symbols, "AAPL", "SPY", "QQQ"]))
    current_et = datetime.now(ET)
    current_date = current_et.strftime("%Y-%m-%d")
    dates: list[str] = []
    seen: set[str] = set()

    for symbol in candidate_symbols:
        if len(dates) >= requested:
            break
        filter_expr = (
            f"{_build_environment_filter(environment)} && "
            'interval = "5m" && '
            f'symbol = "{escape_filter_string(symbol)}"'
        )
        for page in range(1, 21):
            try:
                rows = pb.get_records(
                    "ibkr_bars",
                    filter=filter_expr,
                    sort="-bar_time_ms",
                    per_page=200,
                    page=page,
                )
            except Exception:
                rows = []
            if not rows:
                break
            for row in rows:
                date_text = _parse_us_date(row)
                if not date_text or date_text in seen:
                    continue
                # During a live session the current date is not a complete
                # trading day yet. Weekend runs still keep the latest Friday.
                if date_text == current_date and current_et.hour < 20:
                    continue
                seen.add(date_text)
                dates.append(date_text)
                if len(dates) >= requested:
                    break
            if len(rows) < 200 or len(dates) >= requested:
                break
    return dates[:requested]


def _load_screener_rows(
    *,
    environment: str,
    symbols: list[str],
    market_dates: list[str],
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    rows_by_symbol: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
    errors: list[dict[str, Any]] = []
    symbol_csv = ",".join(symbols)
    for market_date in market_dates:
        result = request_json_request(
            "GET",
            compute_base_url,
            "/screener",
            params=[
                ("environment", environment),
                ("market_date", market_date),
                ("symbols", symbol_csv),
                ("limit", "0"),
            ],
            timeout=45.0,
        )
        status_code = int(result.get("status_code") or 0)
        payload = ensure_object(result.get("payload"))
        if status_code >= 400 or not payload:
            errors.append(
                {
                    "market_date": market_date,
                    "status_code": status_code,
                    "error": to_text(result.get("error") or payload.get("error") or "screener_unavailable"),
                }
            )
            continue
        for row in payload.get("items") or []:
            if not isinstance(row, dict):
                continue
            symbol = to_text(row.get("symbol")).upper()
            if symbol in rows_by_symbol:
                rows_by_symbol[symbol][market_date] = dict(row)
    return rows_by_symbol, errors


def _daily_failures(row: dict[str, Any]) -> list[dict[str, Any]]:
    checks = (
        ("avg_10d_volume", MIN_AVG_10D_VOLUME, "10 日均量不足"),
        ("premarket_volume", MIN_PREMARKET_VOLUME, "盘前量不足"),
        ("atr_pct", MIN_ATR_PCT, "ATR 不足"),
        ("day_change_pct", MIN_ABS_DAY_CHANGE_PCT, "日内涨跌幅不足"),
    )
    failures = []
    for key, threshold, note in checks:
        actual = _metric_value(row, key)
        if actual < threshold:
            failures.append(
                {
                    "bucket": f"{key}_below_threshold",
                    "metric": key,
                    "actual": round(actual, 4),
                    "threshold": threshold,
                    "note": note,
                }
            )
    return failures


def _row_has_usable_data(row: dict[str, Any]) -> bool:
    return bool(
        row.get("has_live_bar")
        or _safe_float(row.get("price")) > 0
        or _safe_float(row.get("avg_10d_volume")) > 0
        or _safe_float(row.get("premarket_volume")) > 0
        or _safe_float(row.get("atr_pct")) > 0
    )


def _find_similar_group(symbol: str) -> tuple[str, ...]:
    for group in SIMILAR_SYMBOL_GROUPS:
        if symbol in group:
            return group
    return ()


def _load_duplicate_context(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    escape_filter_string: EscapeFilterString,
) -> dict[str, dict[str, Any]]:
    group_symbols = sorted({member for symbol in symbols for member in _find_similar_group(symbol)})
    if not group_symbols:
        return {}
    symbol_filter = "(" + " || ".join(f'symbol = "{escape_filter_string(symbol)}"' for symbol in group_symbols) + ")"
    filter_expr = (
        f'({{env}} || environment = "global" || environment = "") && '
        f"{symbol_filter} && "
        '(symbol_role = "trade" || symbol_role = "")'
    ).replace("{env}", f'environment = "{escape_filter_string(environment)}"')
    try:
        rows = pb.get_records("watchlist", filter=filter_expr, sort="-updated", per_page=100, page=1)
    except Exception:
        rows = []
    return {to_text((row or {}).get("symbol")).upper(): dict(row or {}) for row in rows if to_text((row or {}).get("symbol"))}


def _build_duplicate_warning(symbol: str, existing_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    group = _find_similar_group(symbol)
    if not group:
        return {"duplicate": False}
    group_key = ",".join(group)
    canonical = CANONICAL_SYMBOL_BY_GROUP.get(group_key, group[0])
    existing = [member for member in group if member != symbol and member in existing_by_symbol]
    if not existing:
        return {
            "duplicate": False,
            "group": list(group),
            "canonical_symbol": canonical,
        }
    message = (
        f"{symbol} 与现有 trade 标的 {', '.join(existing)} 属于同一组；"
        f"建议只保留 {canonical}，避免重复暴露和重复回测。"
    )
    return {
        "duplicate": True,
        "group": list(group),
        "canonical_symbol": canonical,
        "existing_trade_symbols": existing,
        "message": message,
        "non_canonical": symbol != canonical,
    }


def _summarize_failures(daily_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for failure in daily_failures:
        bucket = to_text(failure.get("bucket"))
        if not bucket:
            continue
        item = buckets.setdefault(
            bucket,
            {
                "bucket": bucket,
                "metric": failure.get("metric"),
                "count": 0,
                "threshold": failure.get("threshold"),
                "note": failure.get("note"),
                "examples": [],
            },
        )
        item["count"] = int(item.get("count") or 0) + 1
        if len(item["examples"]) < 3:
            item["examples"].append(failure.get("actual"))
    return sorted(buckets.values(), key=lambda item: (-int(item.get("count") or 0), to_text(item.get("bucket"))))


def _build_symbol_result(
    *,
    symbol: str,
    market_dates: list[str],
    rows_by_date: dict[str, dict[str, Any]],
    duplicate_warning: dict[str, Any],
    window_trading_days: int,
) -> dict[str, Any]:
    evaluated_days = 0
    pass_days = 0
    all_failures: list[dict[str, Any]] = []
    daily_results: list[dict[str, Any]] = []
    latest_metrics: dict[str, Any] = {}

    for market_date in market_dates:
        row = rows_by_date.get(market_date) or {}
        if not row or not _row_has_usable_data(row):
            daily_results.append({"market_date": market_date, "status": "missing"})
            continue
        evaluated_days += 1
        failures = _daily_failures(row)
        passed = not failures
        if passed:
            pass_days += 1
        all_failures.extend(failures)
        if not latest_metrics:
            latest_metrics = {
                "market_date": market_date,
                "price": row.get("price"),
                "avg_10d_volume": row.get("avg_10d_volume"),
                "premarket_volume": row.get("premarket_volume"),
                "atr_pct": row.get("atr_pct"),
                "day_change_pct": row.get("day_change_pct"),
                "tradability_score": row.get("tradability_score"),
                "is_operable": row.get("is_operable"),
            }
        daily_results.append(
            {
                "market_date": market_date,
                "status": "pass" if passed else "fail",
                "failures": failures,
            }
        )

    required_pass_days = max(MIN_PASS_DAYS, int(ceil(evaluated_days * MIN_PASS_RATE))) if evaluated_days else MIN_PASS_DAYS
    if evaluated_days < MIN_EVALUATED_DAYS:
        status = "insufficient_data"
        recommendation = "market_monitor"
        message = f"{symbol} 近 {window_trading_days} 个交易日可评估数据不足，建议先加入观察池预热数据。"
    elif pass_days >= required_pass_days:
        status = "pass"
        recommendation = "trade"
        message = f"{symbol} 近 {window_trading_days} 个交易日有 {pass_days}/{evaluated_days} 天满足日内交易基础条件，可加入 trade。"
    else:
        status = "warn"
        recommendation = "market_monitor"
        message = (
            f"{symbol} 近 {window_trading_days} 个交易日仅 {pass_days}/{evaluated_days} 天满足基础条件，"
            f"低于 {required_pass_days} 天精品准入线，建议加入观察池，不建议进入每日交易池。"
        )

    duplicate = dict(duplicate_warning or {})
    if duplicate.get("duplicate"):
        status = "warn" if status == "pass" else status
        recommendation = "market_monitor" if duplicate.get("non_canonical") else recommendation
        message = f"{message} {duplicate.get('message')}"

    return {
        "symbol": symbol,
        "status": status,
        "recommendation": recommendation,
        "message": message,
        "window_trading_days": int(window_trading_days),
        "evaluated_days": evaluated_days,
        "pass_days": pass_days,
        "min_pass_days": required_pass_days,
        "min_pass_rate": MIN_PASS_RATE,
        "thresholds": {
            "avg_10d_volume_gte": MIN_AVG_10D_VOLUME,
            "premarket_volume_gte": MIN_PREMARKET_VOLUME,
            "atr_pct_gte": MIN_ATR_PCT,
            "abs_day_change_pct_gte": MIN_ABS_DAY_CHANGE_PCT,
        },
        "fail_reasons": _summarize_failures(all_failures),
        "latest_metrics": latest_metrics,
        "duplicate": duplicate,
        "daily_results": daily_results,
    }


def build_watchlist_eligibility_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    symbols = _normalize_symbols(payload.get("symbols") or payload.get("symbol"))
    window_trading_days = max(
        1,
        min(MAX_WINDOW_TRADING_DAYS, to_int(payload.get("window_trading_days"), DEFAULT_WINDOW_TRADING_DAYS)),
    )
    if not symbols:
        return {"ok": False, "error": "missing_symbols", "source": "ibkr-api"}, 400

    raw_market_dates = payload.get("market_dates")
    if isinstance(raw_market_dates, (list, tuple)):
        market_dates = [to_text(item)[:10] for item in raw_market_dates if len(to_text(item)) >= 10]
    else:
        market_dates = []
    market_dates = list(dict.fromkeys(market_dates))[:window_trading_days]
    if not market_dates:
        market_dates = _load_recent_market_dates(
            pb,
            environment=environment,
            symbols=symbols,
            window_trading_days=window_trading_days,
            escape_filter_string=escape_filter_string,
        )

    duplicate_context = _load_duplicate_context(
        pb,
        environment=environment,
        symbols=symbols,
        escape_filter_string=escape_filter_string,
    )

    rows_by_symbol, errors = ({symbol: {} for symbol in symbols}, [])
    if market_dates:
        rows_by_symbol, errors = _load_screener_rows(
            environment=environment,
            symbols=symbols,
            market_dates=market_dates,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )

    items = [
        _build_symbol_result(
            symbol=symbol,
            market_dates=market_dates,
            rows_by_date=rows_by_symbol.get(symbol) or {},
            duplicate_warning=_build_duplicate_warning(symbol, duplicate_context),
            window_trading_days=window_trading_days,
        )
        for symbol in symbols
    ]

    status_counts: dict[str, int] = {}
    for item in items:
        status = to_text(item.get("status") or "unknown")
        status_counts[status] = int(status_counts.get(status, 0) or 0) + 1

    return {
        "ok": True,
        "environment": environment,
        "window_trading_days": window_trading_days,
        "market_dates": market_dates,
        "items": items,
        "summary": {
            "total": len(items),
            "pass": int(status_counts.get("pass", 0) or 0),
            "warn": int(status_counts.get("warn", 0) or 0),
            "insufficient_data": int(status_counts.get("insufficient_data", 0) or 0),
        },
        "errors": errors,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_watchlist_eligibility_response"]
