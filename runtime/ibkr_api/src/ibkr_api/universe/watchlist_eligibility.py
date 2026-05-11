from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.orders.values import ensure_object, to_int, to_text
from ibkr_api.universe.dynamic_admission import (
    build_admission_profile,
    build_dynamic_thresholds,
    evaluate_dynamic_admission_gates,
)
from ibkr_api.universe.fundamentals import load_fundamentals_cache_by_symbol


RequestJsonRequest = Callable[..., dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]

ET = ZoneInfo("America/New_York")

DEFAULT_WINDOW_TRADING_DAYS = 10
MIN_EVALUATED_DAYS = 3
MIN_PASS_DAYS = 2
MIN_PASS_RATE = 0.6
MAX_WINDOW_TRADING_DAYS = 20


def _normalize_similarity_symbol(value: Any) -> str:
    symbol = to_text(value).upper().replace("/", ".")
    if symbol in {"BRK-A", "BRK-B", "BF-A", "BF-B"}:
        symbol = symbol.replace("-", ".")
    return symbol


def _similar_group_key(group: tuple[str, ...]) -> str:
    return ",".join(sorted(_normalize_similarity_symbol(symbol) for symbol in group))


def _symbol_lookup_variants(symbol: str) -> set[str]:
    normalized = _normalize_similarity_symbol(symbol)
    variants = {normalized}
    if "." in normalized:
        variants.add(normalized.replace(".", "-"))
    return variants


SIMILAR_SYMBOL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("GOOG", "GOOGL"),
    ("FOX", "FOXA"),
    ("BRK.A", "BRK.B"),
    ("BF.A", "BF.B"),
    ("SQ", "XYZ"),
)
CANONICAL_SYMBOL_BY_GROUP = {
    _similar_group_key(("GOOG", "GOOGL")): "GOOGL",
    _similar_group_key(("FOX", "FOXA")): "FOXA",
    _similar_group_key(("BRK.A", "BRK.B")): "BRK.B",
    _similar_group_key(("BF.A", "BF.B")): "BF.B",
    _similar_group_key(("SQ", "XYZ")): "XYZ",
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


def _daily_failures(row: dict[str, Any], thresholds: dict[str, Any], *, market_date: str) -> list[dict[str, Any]]:
    return evaluate_dynamic_admission_gates(row, thresholds, market_date=market_date)


def _row_has_usable_data(row: dict[str, Any]) -> bool:
    return bool(
        row.get("has_live_bar")
        or _safe_float(row.get("price")) > 0
        or _safe_float(row.get("avg_10d_volume")) > 0
        or _safe_float(row.get("premarket_volume")) > 0
        or _safe_float(row.get("atr_pct")) > 0
    )


def _find_similar_group(symbol: str) -> tuple[str, ...]:
    normalized_symbol = _normalize_similarity_symbol(symbol)
    for group in SIMILAR_SYMBOL_GROUPS:
        if normalized_symbol in {_normalize_similarity_symbol(member) for member in group}:
            return group
    return ()


def _load_duplicate_context(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    escape_filter_string: EscapeFilterString,
) -> dict[str, dict[str, Any]]:
    group_symbols = sorted(
        {
            variant
            for symbol in symbols
            for member in _find_similar_group(symbol)
            for variant in _symbol_lookup_variants(member)
        }
    )
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
    return {
        _normalize_similarity_symbol((row or {}).get("symbol")): dict(row or {})
        for row in rows
        if to_text((row or {}).get("symbol"))
    }


def _build_duplicate_warning(symbol: str, existing_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    group = _find_similar_group(symbol)
    if not group:
        return {"duplicate": False}
    normalized_symbol = _normalize_similarity_symbol(symbol)
    group_key = _similar_group_key(group)
    canonical = CANONICAL_SYMBOL_BY_GROUP.get(group_key, group[0])
    normalized_canonical = _normalize_similarity_symbol(canonical)
    existing = [
        to_text(existing_by_symbol.get(_normalize_similarity_symbol(member), {}).get("symbol") or member).upper()
        for member in group
        if _normalize_similarity_symbol(member) != normalized_symbol
        and _normalize_similarity_symbol(member) in existing_by_symbol
    ]
    non_canonical = normalized_symbol != normalized_canonical
    if not existing:
        result = {
            "duplicate": False,
            "group": list(group),
            "canonical_symbol": canonical,
            "non_canonical": non_canonical,
        }
        if non_canonical:
            result["message"] = f"{symbol} 属于相似标的组；建议使用 canonical {canonical}，避免后续重复暴露。"
        return result
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
        "non_canonical": non_canonical,
    }


def _summarize_failures(daily_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for failure in daily_failures:
        bucket = to_text(failure.get("bucket") or failure.get("gate"))
        if not bucket:
            continue
        item = buckets.setdefault(
            bucket,
            {
                "bucket": bucket,
                "gate": to_text(failure.get("gate") or bucket),
                "metric": failure.get("metric"),
                "count": 0,
                "days_failed": 0,
                "op": failure.get("op") or "gte",
                "threshold": failure.get("threshold"),
                "severity": failure.get("severity") or "hard",
                "note": failure.get("note"),
                "examples": [],
            },
        )
        item["count"] = int(item.get("count") or 0) + 1
        item["days_failed"] = int(item.get("days_failed") or 0) + 1
        if len(item["examples"]) < 3:
            example: dict[str, Any] = {"actual": failure.get("actual")}
            if failure.get("market_date"):
                example["market_date"] = failure.get("market_date")
            item["examples"].append(example)
    return sorted(buckets.values(), key=lambda item: (-int(item.get("count") or 0), to_text(item.get("bucket"))))


def _aggregate_failed_gates(
    *,
    evaluated_days: int,
    pass_days: int,
    required_pass_days: int,
    min_evaluated_days: int,
    min_pass_rate: float,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if evaluated_days < min_evaluated_days:
        failures.append(
            {
                "gate": "min_evaluated_days",
                "bucket": "min_evaluated_days",
                "metric": "evaluated_days",
                "op": "gte",
                "actual": evaluated_days,
                "threshold": min_evaluated_days,
                "severity": "hard",
                "note": "Not enough recent market days had usable screener data.",
                "count": 1,
                "days_failed": 1,
                "examples": [{"actual": evaluated_days}],
            }
        )
    if evaluated_days >= min_evaluated_days and pass_days < required_pass_days:
        failures.append(
            {
                "gate": "min_pass_days",
                "bucket": "min_pass_days",
                "metric": "pass_days",
                "op": "gte",
                "actual": pass_days,
                "threshold": required_pass_days,
                "severity": "hard",
                "note": "Recent pass-days are below the dynamic admission requirement.",
                "count": 1,
                "days_failed": max(0, required_pass_days - pass_days),
                "min_pass_rate": min_pass_rate,
                "examples": [{"actual": pass_days}],
            }
        )
    return failures


def _build_symbol_result(
    *,
    symbol: str,
    market_dates: list[str],
    rows_by_date: dict[str, dict[str, Any]],
    fundamentals: dict[str, Any],
    duplicate_warning: dict[str, Any],
    window_trading_days: int,
) -> dict[str, Any]:
    evaluated_days = 0
    pass_days = 0
    all_failures: list[dict[str, Any]] = []
    daily_results: list[dict[str, Any]] = []
    latest_metrics: dict[str, Any] = {}
    latest_row: dict[str, Any] = {}

    for market_date in market_dates:
        row = rows_by_date.get(market_date) or {}
        if row and _row_has_usable_data(row):
            latest_row = dict(row)
            break

    admission_profile = build_admission_profile(
        symbol=symbol,
        fundamentals=fundamentals,
        latest_row=latest_row,
    )
    thresholds = build_dynamic_thresholds(admission_profile)

    for market_date in market_dates:
        row = rows_by_date.get(market_date) or {}
        if not row or not _row_has_usable_data(row):
            daily_results.append({"market_date": market_date, "status": "missing"})
            continue
        evaluated_days += 1
        failures = _daily_failures(row, thresholds, market_date=market_date)
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

    failed_gates = [
        *_summarize_failures(all_failures),
        *_aggregate_failed_gates(
            evaluated_days=evaluated_days,
            pass_days=pass_days,
            required_pass_days=required_pass_days,
            min_evaluated_days=MIN_EVALUATED_DAYS,
            min_pass_rate=MIN_PASS_RATE,
        ),
    ]

    duplicate = dict(duplicate_warning or {})
    if duplicate.get("duplicate") or duplicate.get("non_canonical"):
        status = "warn" if status == "pass" else status
        recommendation = "market_monitor" if duplicate.get("non_canonical") else recommendation
        if duplicate.get("message"):
            message = f"{message} {duplicate.get('message')}"

    pass_rate = (pass_days / evaluated_days) if evaluated_days > 0 else 0.0
    admission_score = round(max(0.0, min(100.0, pass_rate * 100.0)), 3)
    if status == "insufficient_data" and latest_metrics:
        admission_score = max(admission_score, 25.0)
    if duplicate.get("non_canonical"):
        admission_score = min(admission_score, 55.0)
    needs_backfill = evaluated_days < MIN_EVALUATED_DAYS or not latest_metrics

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
        "profile": admission_profile,
        "symbol_profile": admission_profile,
        "fundamentals_profile": admission_profile,
        "thresholds": {
            **thresholds,
            "min_evaluated_days_gte": MIN_EVALUATED_DAYS,
            "pass_days_gte": required_pass_days,
            "pass_rate_gte": MIN_PASS_RATE,
        },
        "dynamic_thresholds": thresholds,
        "admission_score": admission_score,
        "score": admission_score,
        "pass_rate": round(pass_rate, 4),
        "needs_backfill": needs_backfill,
        "failed_gates": failed_gates,
        "fail_reasons": failed_gates,
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
    fundamentals_by_symbol = load_fundamentals_cache_by_symbol(
        pb,
        symbols,
        provider=payload.get("fundamentals_provider") or "finnhub",
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
            fundamentals=fundamentals_by_symbol.get(symbol) or {},
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
        "fundamentals_provider": to_text(payload.get("fundamentals_provider") or "finnhub").lower(),
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
