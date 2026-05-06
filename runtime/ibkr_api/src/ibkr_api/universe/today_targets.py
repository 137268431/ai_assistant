from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    TODAY_TARGET_STATUSES,
    build_bar_environment_filter,
    build_daily_change_fields,
    classify_session,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_date,
    format_et_datetime,
    indicator_snapshot,
    interval_to_chart_tf,
    load_daily_scan_state,
    load_records_for_symbols,
    load_watch_meta,
    normalize_signal_record,
    parse_et_datetime_ms,
    pick_latest_signal,
    pick_reason_list,
)
from ibkr_api.universe.today_targets_workflow import (
    build_aligned_technical_flags,
    build_filtered_summary,
    build_ready_explanation,
    build_technical_flags,
    build_workflow_guide,
    ensure_row_details,
    matches_filters,
    normalize_filters,
    resolve_attention_state,
    resolve_technical_state,
    sort_rows,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
    build_tradability_assessment,
)


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]


def build_today_targets_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    runtime_environment = normalize_environment(payload.get("environment"), LIVE_ENVIRONMENT)
    current_date = current_market_date(time_strings)
    requested_market_date = to_text(first_defined(payload.get("marketDate"), payload.get("market_date"), payload.get("date"))) or current_date
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(requested_market_date)
        market_date = requested_market_date
    except Exception:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(current_date)
        market_date = current_date
    filters = normalize_filters(payload)
    workflow_guide = build_workflow_guide(runtime_environment, market_date)
    paginate = bool(payload.get("paginate"))
    requested_per_page = max(1, min(200, to_int(first_defined(payload.get("per_page"), payload.get("perPage")), 10)))
    requested_page = max(1, to_int(payload.get("page"), 1)) if paginate else 1
    computed_at_ms = int(time.time() * 1000)
    daily_scan = load_daily_scan_state(pb, runtime_environment)

    target_rows = pb.get_records(
        "ibkr_targets",
        filter=(
            f'environment = "{escape_filter(runtime_environment)}" && '
            f'date = "{escape_filter(market_date)}" && '
            '(status = "candidate" || status = "active")'
        ),
        sort="-updated",
        per_page=500,
        page=1,
    )
    target_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_symbols: list[str] = []
    for row in target_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in target_by_symbol:
            continue
        target_by_symbol[symbol] = dict(row)
        ordered_symbols.append(symbol)

    if not ordered_symbols:
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            "current_market_date": current_date,
            "computed_at_ms": computed_at_ms,
            "computed_at_us": format_et_datetime(computed_at_ms),
            "computed_at_cn": format_cn_time(computed_at_ms),
            "workflow": workflow_guide,
            "daily_scan": daily_scan,
            "summary": {
                "total": 0,
                "active_count": 0,
                "candidate_count": 0,
                "operable_count": 0,
                "technical_ready_count": 0,
                "signaled_count": 0,
                "awaiting_confirm_count": 0,
                "pending_count": 0,
                "executed_count": 0,
                "stale_count": 0,
            },
            "filters": filters,
            "filtered_summary": {"total": 0, "ready_count": 0, "signaled_count": 0, "needs_action_count": 0},
            "filtered_total": 0,
            "pagination_enabled": paginate,
            "page": 1,
            "per_page": requested_per_page if paginate else 0,
            "total_pages": 1,
            "has_prev_page": False,
            "has_next_page": False,
            "returned_count": 0,
            "items": [],
            "source": "ibkr-api",
        }, 200

    watch_meta = load_watch_meta(pb, runtime_environment, ordered_symbols)
    lookback_daily_ms = market_start_ms - 20 * 24 * 60 * 60 * 1000
    indicator_lookback_ms = market_start_ms

    daily_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "1d"',
            build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {lookback_daily_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=12,
    )
    intraday_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=30,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf("5m")}"',
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {indicator_lookback_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )
    signal_records = load_records_for_symbols(
        pb,
        "ibkr_signals",
        base_filter_parts=[
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )

    daily_history_by_symbol: dict[str, list[dict[str, Any]]] = {}
    fallback_daily_by_symbol: dict[str, dict[str, Any]] = {}
    for row in daily_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        close = to_float(row.get("close")) or 0.0
        if not symbol or bar_time_ms <= 0 or close <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": close,
            "volume": to_float(row.get("volume")) or 0.0,
            "us_time": to_text(row.get("us_time")),
            "date": format_et_date(bar_time_ms),
        }
        daily_history_by_symbol.setdefault(symbol, []).append(payload_row)
        if payload_row["date"] < market_date:
            fallback_daily_by_symbol[symbol] = payload_row

    latest_intraday_by_symbol: dict[str, dict[str, Any]] = {}
    volume_stats_by_symbol: dict[str, dict[str, float]] = {}
    for row in intraday_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": to_float(row.get("close")) or 0.0,
            "exchange": to_text(row.get("exchange")).upper(),
            "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
            "us_time": to_text(row.get("us_time")),
            "volume": to_float(row.get("volume")) or 0.0,
        }
        current_latest = latest_intraday_by_symbol.get(symbol)
        if current_latest is None or payload_row["bar_time_ms"] >= current_latest["bar_time_ms"]:
            latest_intraday_by_symbol[symbol] = payload_row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "today": 0.0})
        stats["today"] += payload_row["volume"]
        if payload_row["session_type"] == "premarket":
            stats["premarket"] += payload_row["volume"]

    latest_indicator_by_symbol: dict[str, dict[str, Any]] = {}
    for row in indicator_records:
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in latest_indicator_by_symbol:
            continue
        latest_indicator_by_symbol[symbol] = row

    signal_agg_by_symbol: dict[str, dict[str, Any]] = {}
    for row in signal_records:
        normalized_signal = normalize_signal_record(row)
        symbol = normalized_signal.get("symbol")
        if not symbol:
            continue
        bucket = signal_agg_by_symbol.setdefault(symbol, {"count": 0, "latest": None})
        bucket["count"] += 1
        bucket["latest"] = pick_latest_signal(bucket.get("latest"), normalized_signal)

    items: list[dict[str, Any]] = []
    active_count = 0
    candidate_count = 0
    operable_count = 0
    technical_ready_count = 0
    signaled_count = 0
    awaiting_confirm_count = 0
    pending_count = 0
    executed_count = 0
    stale_count = 0

    for symbol in ordered_symbols:
        target = target_by_symbol.get(symbol)
        if not target:
            continue
        target_extra = parse_json_object(target.get("extra"))
        screener_snapshot = parse_json_object(target_extra.get("screener_snapshot"))
        meta = watch_meta.get(symbol, {})
        intraday = latest_intraday_by_symbol.get(symbol)
        fallback_daily = fallback_daily_by_symbol.get(symbol)
        indicator_record = latest_indicator_by_symbol.get(symbol)
        indicator_extra = indicator_snapshot(indicator_record)
        signal_agg = signal_agg_by_symbol.get(symbol, {"count": 0, "latest": None})
        latest_signal = signal_agg.get("latest")
        history = [row for row in daily_history_by_symbol.get(symbol, []) if row.get("date") < market_date]
        last_10 = history[-10:]
        avg_10d_volume = round(sum(to_float(row.get("volume")) or 0.0 for row in last_10) / len(last_10), 2) if last_10 else 0.0
        price = to_float((intraday or {}).get("close")) or 0.0
        price_source = "5m"
        if price <= 0:
            price = to_float((fallback_daily or {}).get("close")) or 0.0
            price_source = "1d_close" if price > 0 else ""
        compare_history = build_daily_change_fields(history, price) if price > 0 else {
            "day_change_pct": 0.0,
            "prev_close_change_pct": 0.0,
            "change_7d": 0.0,
        }
        target_status = to_text(target.get("status")).lower()
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        score = round(to_float(target.get("score")) or 0.0, 2)
        scan_reason = to_text(target.get("scan_reason"))
        intraday_bar_time_ms = to_int((intraday or {}).get("bar_time_ms"), 0)
        latest_bar_time_ms = intraday_bar_time_ms or to_int((fallback_daily or {}).get("bar_time_ms"), 0)
        freshness_min = max(0, int((computed_at_ms - intraday_bar_time_ms) // 60000)) if intraday_bar_time_ms > 0 else None
        volume_stats = volume_stats_by_symbol.get(symbol, {"premarket": 0.0, "today": 0.0})
        row = {
            "symbol": symbol,
            "record_id": to_text(target.get("id")),
            "status": target_status,
            "target_status": target_status,
            "direction_bias": direction_bias,
            "score": score,
            "target_score": score,
            "scan_reason": scan_reason,
            "exchange": to_text(first_defined(meta.get("exchange"), (intraday or {}).get("exchange"), target.get("exchange"))).upper(),
            "industry": to_text(meta.get("industry")),
            "note": to_text(meta.get("note")),
            "price": round(price, 4) if price > 0 else 0.0,
            "price_source": price_source,
            "atr_pct": round(to_float(first_defined(indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "avg_10d_volume": avg_10d_volume,
            "premarket_volume": round(to_float(first_defined(volume_stats.get("premarket"), screener_snapshot.get("premarket_volume"))) or 0.0, 2),
            "today_volume": round(to_float(first_defined(volume_stats.get("today"), screener_snapshot.get("today_volume"))) or 0.0, 2),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_intraday_bar_time_ms": intraday_bar_time_ms,
            "latest_us_time": to_text((intraday or {}).get("us_time")) or to_text(target.get("us_time")) or to_text((fallback_daily or {}).get("us_time")),
            "freshness_min": freshness_min,
            "has_live_bar": intraday_bar_time_ms > 0,
            "day_change_pct": compare_history["day_change_pct"],
            "prev_close_change_pct": compare_history["prev_close_change_pct"],
            "change_7d": compare_history["change_7d"],
            "extra": target_extra,
            "updated": to_text(target.get("updated")),
        }
        tradability_score, assessment_notes = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = pick_reason_list(assessment_notes, screener_snapshot.get("operable_reasons") if isinstance(screener_snapshot.get("operable_reasons"), list) else [])
        row["is_operable"] = bool(
            row["has_live_bar"]
            and (to_float(row.get("price")) or 0.0) > 0
            and (to_float(row.get("avg_10d_volume")) or 0.0) >= TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME
            and (to_float(row.get("tradability_score")) or 0.0) >= TRADABILITY_OPERABLE_MIN_SCORE
            and isinstance(row.get("freshness_min"), int)
            and row["freshness_min"] <= TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN
        )
        row["technical_flags"] = build_technical_flags(indicator_extra)
        row["technical_aligned_flags"] = build_aligned_technical_flags(indicator_extra, direction_bias)
        row["technical_state"] = resolve_technical_state(row, row["technical_aligned_flags"])
        row["ready_explanation"] = build_ready_explanation(row)
        row["has_signal_today"] = bool(signal_agg.get("count"))
        row["signal_count_today"] = int(signal_agg.get("count") or 0)
        row["latest_signal_id"] = to_text((latest_signal or {}).get("signal_id"))
        row["latest_signal_status"] = to_text((latest_signal or {}).get("status"))
        row["latest_signal_direction"] = to_text((latest_signal or {}).get("direction"))
        row["latest_signal_time"] = to_text((latest_signal or {}).get("us_time"))
        row["latest_signal_time_ms"] = to_int((latest_signal or {}).get("sort_ms"), 0)
        row["latest_signal_note"] = to_text((latest_signal or {}).get("note"))
        attention_state, attention_rank = resolve_attention_state(row)
        row["attention_state"] = attention_state
        row["attention_rank"] = attention_rank

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if row["is_operable"]:
            operable_count += 1
        if row["technical_state"] == "ready":
            technical_ready_count += 1
        if row["technical_state"] == "stale":
            stale_count += 1
        if row["has_signal_today"]:
            signaled_count += 1
        if row["latest_signal_status"] == "awaiting_confirm":
            awaiting_confirm_count += 1
        if row["latest_signal_status"] == "pending":
            pending_count += 1
        if row["latest_signal_status"] == "executed":
            executed_count += 1
        items.append(row)

    filtered_items = [row for row in sort_rows(items, to_text(filters.get("sort_by"))) if matches_filters(row, filters)]
    filtered_summary = build_filtered_summary(filtered_items)
    total_pages = max(1, (len(filtered_items) + requested_per_page - 1) // requested_per_page) if paginate else 1
    page = min(requested_page, total_pages) if paginate else 1
    offset = (page - 1) * requested_per_page if paginate else 0
    paged_items = filtered_items[offset:offset + requested_per_page] if paginate else filtered_items
    for row in paged_items:
        ensure_row_details(row)

    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        "current_market_date": current_date,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": format_et_datetime(computed_at_ms),
        "computed_at_cn": format_cn_time(computed_at_ms),
        "workflow": workflow_guide,
        "daily_scan": daily_scan,
        "summary": {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "operable_count": operable_count,
            "technical_ready_count": technical_ready_count,
            "signaled_count": signaled_count,
            "awaiting_confirm_count": awaiting_confirm_count,
            "pending_count": pending_count,
            "executed_count": executed_count,
            "stale_count": stale_count,
        },
        "filters": filters,
        "filtered_summary": filtered_summary,
        "filtered_total": len(filtered_items),
        "pagination_enabled": paginate,
        "page": page,
        "per_page": requested_per_page if paginate else len(filtered_items),
        "total_pages": total_pages,
        "has_prev_page": page > 1 if paginate else False,
        "has_next_page": page < total_pages if paginate else False,
        "returned_count": len(paged_items),
        "items": paged_items,
        "source": "ibkr-api",
    }, 200


__all__ = ["TODAY_TARGET_STATUSES", "build_today_targets_response"]
