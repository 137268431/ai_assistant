from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


BAR_INTERVAL_MS = 5 * 60 * 1000
BAR_LAG_ALERT_MS = 10 * 60 * 1000
INDICATOR_LAG_ALERT_MS = 10 * 60 * 1000
GAP_ALERT_COOLDOWN_MS = 30 * 60 * 1000
GAP_MONITOR_STATE_KEY = "system_gap_monitor"
GAP_ALERT_EXCLUDED_SYMBOLS = {"VIX"}

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
EmitSystemEvent = Callable[..., dict[str, Any]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _unique_sorted(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values or []:
        item = _to_text(value).upper()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    output.sort()
    return output


def _alertable_symbols(symbols: list[Any]) -> list[str]:
    return [symbol for symbol in _unique_sorted(symbols) if symbol not in GAP_ALERT_EXCLUDED_SYMBOLS]


def _is_regular_session(row: dict[str, Any]) -> bool:
    return _to_text(row.get("session_type")).lower() == "regular"


def _get_state_data(pb: Any, state_key: str, environment: str, date_token: str) -> dict[str, Any]:
    try:
        record = pb.get_state(state_key, environment, date=date_token)
    except Exception:
        record = None
    return _as_dict((record or {}).get("data") if isinstance(record, dict) else {})


def _load_watchlist_symbols(pb: Any, environment: str) -> list[str]:
    filter_expr = f'(environment = "{environment}" || environment = "global" || environment = "") && symbol != ""'
    rows = pb.get_records("watchlist", filter=filter_expr, sort="symbol", per_page=500, page=1)
    return _unique_sorted([row.get("symbol") for row in rows or [] if isinstance(row, dict)])


def _load_target_symbols(pb: Any, environment: str, date_token: str) -> list[str]:
    filter_expr = (
        f'date = "{date_token}" && environment = "{environment}" && '
        '(status = "candidate" || status = "active")'
    )
    rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", per_page=500, page=1)
    return _unique_sorted([row.get("symbol") for row in rows or [] if isinstance(row, dict)])


def _load_recent_rows_by_symbol(pb: Any, collection: str, environment: str, interval: str, today_start: str, limit: int) -> dict[str, Any]:
    rows = pb.get_records(
        collection,
        filter=(
            f'environment = "{environment}" && '
            f'interval = "{interval}" && '
            f'us_time >= "{today_start}"'
        ),
        sort="-bar_time_ms",
        per_page=max(1, limit),
        page=1,
    )
    latest_by_symbol: dict[str, dict[str, Any]] = {}
    series_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = _to_text(row.get("symbol")).upper()
        if not symbol:
            continue
        if symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = {
                "bar_time_ms": _to_int(row.get("bar_time_ms"), 0),
                "us_time": _to_text(row.get("us_time")),
                "session_type": _to_text(row.get("session_type")).lower(),
            }
        series = series_by_symbol.setdefault(symbol, [])
        if len(series) < 12:
            series.append(
                {
                    "bar_time_ms": _to_int(row.get("bar_time_ms"), 0),
                    "us_time": _to_text(row.get("us_time")),
                    "session_type": _to_text(row.get("session_type")).lower(),
                }
            )
    return {"latest_by_symbol": latest_by_symbol, "series_by_symbol": series_by_symbol}


def _build_gap_fingerprint(summary: dict[str, Any]) -> str:
    return str(
        {
            "latest_bar_time_ms": summary.get("latest_bar_time_ms") or 0,
            "bar_lag_symbols": list(summary.get("bar_lag_symbols") or [])[:12],
            "indicator_lag_symbols": list(summary.get("indicator_lag_symbols") or [])[:12],
            "sequence_gap_examples": list(summary.get("sequence_gap_examples") or [])[:6],
        }
    )


def load_data_gap_summary(pb: Any, *, environment: str, date_token: str, today_start: str) -> dict[str, Any]:
    watchlist_symbols = _load_watchlist_symbols(pb, environment)
    target_symbols = _load_target_symbols(pb, environment, date_token)
    bars = _load_recent_rows_by_symbol(pb, "ibkr_bars", environment, "5m", today_start, 1200)
    indicators = _load_recent_rows_by_symbol(pb, "ibkr_indicators", environment, "5", today_start, 1200)
    latest_bar_by_symbol = _as_dict(bars.get("latest_by_symbol"))
    latest_indicator_by_symbol = _as_dict(indicators.get("latest_by_symbol"))
    monitored_symbols = target_symbols or list(latest_bar_by_symbol.keys())
    symbols = _alertable_symbols(list(monitored_symbols) + list(latest_bar_by_symbol.keys()))
    excluded_symbols = _unique_sorted(
        [
            symbol
            for symbol in list(monitored_symbols) + list(latest_bar_by_symbol.keys())
            if _to_text(symbol).upper() in GAP_ALERT_EXCLUDED_SYMBOLS
        ]
    )

    latest_bar_time_ms = 0
    latest_bar_symbol = ""
    ignored_non_regular_symbols: list[str] = []
    for symbol in symbols:
        bucket = _as_dict(latest_bar_by_symbol.get(symbol))
        bar_ms = _to_int(_as_dict(bucket).get("bar_time_ms"), 0)
        if bar_ms > 0 and not _is_regular_session(bucket):
            ignored_non_regular_symbols.append(symbol)
            continue
        if bar_ms > latest_bar_time_ms:
            latest_bar_time_ms = bar_ms
            latest_bar_symbol = symbol

    bar_lag_symbols: list[str] = []
    indicator_lag_symbols: list[str] = []
    sequence_gap_examples: list[dict[str, Any]] = []
    max_bar_lag_ms = 0
    max_indicator_lag_ms = 0
    for symbol in symbols:
        latest_bar = _as_dict(latest_bar_by_symbol.get(symbol))
        bar_ms = _to_int(latest_bar.get("bar_time_ms"), 0)
        if bar_ms <= 0 or not _is_regular_session(latest_bar):
            continue
        if latest_bar_time_ms > 0 and bar_ms > 0:
            lag_ms = latest_bar_time_ms - bar_ms
            if lag_ms >= BAR_LAG_ALERT_MS:
                bar_lag_symbols.append(symbol)
                max_bar_lag_ms = max(max_bar_lag_ms, lag_ms)
        latest_indicator = _as_dict(latest_indicator_by_symbol.get(symbol))
        indicator_ms = _to_int(latest_indicator.get("bar_time_ms"), 0)
        if bar_ms > 0 and (bar_ms - indicator_ms) >= INDICATOR_LAG_ALERT_MS:
            indicator_lag_symbols.append(symbol)
            max_indicator_lag_ms = max(max_indicator_lag_ms, bar_ms - indicator_ms)
        if len(sequence_gap_examples) >= 6:
            continue
        series = list(_as_dict(bars.get("series_by_symbol")).get(symbol) or [])
        if len(series) < 3:
            continue
        series.sort(key=lambda item: _to_int(_as_dict(item).get("bar_time_ms"), 0))
        for index in range(1, len(series)):
            prev = _as_dict(series[index - 1])
            curr = _as_dict(series[index])
            if _to_text(prev.get("session_type")).lower() != "regular" or _to_text(curr.get("session_type")).lower() != "regular":
                continue
            delta_ms = _to_int(curr.get("bar_time_ms"), 0) - _to_int(prev.get("bar_time_ms"), 0)
            if BAR_INTERVAL_MS < delta_ms <= 6 * BAR_INTERVAL_MS:
                sequence_gap_examples.append(
                    {
                        "symbol": symbol,
                        "prev_us_time": _to_text(prev.get("us_time")),
                        "next_us_time": _to_text(curr.get("us_time")),
                        "missing_points": max(round(delta_ms / BAR_INTERVAL_MS) - 1, 1),
                    }
                )
                break

    summary = {
        "watchlist_count": len(watchlist_symbols),
        "target_count": len(target_symbols),
        "monitored_symbol_count": len(monitored_symbols),
        "alertable_symbol_count": len(symbols),
        "excluded_gap_symbols": excluded_symbols,
        "ignored_non_regular_symbols": _unique_sorted(ignored_non_regular_symbols),
        "today_bar_symbol_count": len(latest_bar_by_symbol),
        "latest_bar_time_ms": latest_bar_time_ms,
        "latest_bar_symbol": latest_bar_symbol,
        "latest_bar_us_time": _to_text(_as_dict(latest_bar_by_symbol.get(latest_bar_symbol)).get("us_time")),
        "bar_lag_symbols": bar_lag_symbols,
        "indicator_lag_symbols": indicator_lag_symbols,
        "sequence_gap_examples": sequence_gap_examples,
        "bar_lag_count": len(bar_lag_symbols),
        "indicator_lag_count": len(indicator_lag_symbols),
        "sequence_gap_count": len(sequence_gap_examples),
        "max_bar_lag_min": round(max_bar_lag_ms / 60000) if max_bar_lag_ms else 0,
        "max_indicator_lag_min": round(max_indicator_lag_ms / 60000) if max_indicator_lag_ms else 0,
        "market_activity_detected": latest_bar_time_ms > 0,
    }
    summary["has_issue"] = bool(summary["market_activity_detected"] and (summary["bar_lag_count"] or summary["indicator_lag_count"] or summary["sequence_gap_count"]))
    summary["fingerprint"] = _build_gap_fingerprint(summary)
    return summary


def build_data_gap_guard_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    emit_system_event: EmitSystemEvent,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    today_start = f"{times['date']} 00:00:00"
    gaps = load_data_gap_summary(pb, environment=environment, date_token=times["date"], today_start=today_start)
    next_state = {
        "last_gap_scan_at": times["us"],
        "last_gap_fingerprint": gaps.get("fingerprint") or "",
    }
    current_state = _get_state_data(pb, GAP_MONITOR_STATE_KEY, environment, times["date"])
    event_result: dict[str, Any] = {}

    if not gaps.get("market_activity_detected") or not gaps.get("has_issue"):
        next_state["last_gap_issue_at"] = ""
        pb.upsert_state(GAP_MONITOR_STATE_KEY, environment, next_state, date=times["date"])
        return {
            "ok": True,
            "environment": environment,
            "summary": gaps,
            "state": next_state,
            "source": "ibkr-api",
            "job_id": "system_data_gap_guard",
        }, 200

    last_alert_hash = _to_text(current_state.get("last_gap_alert_hash"))
    last_alert_ms = _to_int(current_state.get("last_gap_alert_ms"), 0)
    should_notify = gaps.get("fingerprint") != last_alert_hash or last_alert_ms <= 0 or (now_ms - last_alert_ms) >= GAP_ALERT_COOLDOWN_MS
    if should_notify:
        detail = {
            "检查时间": times["us"],
            "最新bar时间": gaps.get("latest_bar_us_time") or "unknown",
            "bars缺口数": str(gaps.get("bar_lag_count") or 0),
            "指标滞后数": str(gaps.get("indicator_lag_count") or 0),
            "序列缺口数": str(gaps.get("sequence_gap_count") or 0),
        }
        bar_lag_symbols = list(gaps.get("bar_lag_symbols") or [])
        indicator_lag_symbols = list(gaps.get("indicator_lag_symbols") or [])
        sequence_gap_examples = list(gaps.get("sequence_gap_examples") or [])
        if bar_lag_symbols:
            detail["bars异常样本"] = ", ".join(bar_lag_symbols[:10])
        if indicator_lag_symbols:
            detail["指标异常样本"] = ", ".join(indicator_lag_symbols[:10])
        if sequence_gap_examples:
            first = _as_dict(sequence_gap_examples[0])
            detail["序列缺口样本"] = f"{_to_text(first.get('symbol'))}: {_to_text(first.get('prev_us_time'))} -> {_to_text(first.get('next_us_time'))} ({_to_int(first.get('missing_points'), 0)})"
        event_result = emit_system_event(
            event_type="alert",
            level="warning",
            source="ibkr_compute",
            title="IBKR 数据缺口告警",
            detail=detail,
            environment=environment,
        )
        next_state.update(
            {
                "last_gap_issue_at": times["us"],
                "last_gap_alert_ms": now_ms,
                "last_gap_alert_hash": gaps.get("fingerprint") or "",
            }
        )
    pb.upsert_state(GAP_MONITOR_STATE_KEY, environment, next_state, date=times["date"])
    return {
        "ok": True,
        "environment": environment,
        "summary": gaps,
        "state": next_state,
        "notified": bool(event_result.get("notified")),
        "source": "ibkr-api",
        "job_id": "system_data_gap_guard",
    }, 200
