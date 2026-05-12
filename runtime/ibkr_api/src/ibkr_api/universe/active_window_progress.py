from __future__ import annotations

from ibkr_api.universe.active_window_progress_support import *  # noqa: F401,F403

def build_active_window_items_for_symbols(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    interval: str,
    market_start_ms: int,
    market_end_ms: int,
    market_date: str,
    computed_at_ms: int | None = None,
    signal_window_max_bars: int | None = None,
    target_by_symbol: dict[str, dict[str, Any]] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    ordered_symbols = normalize_symbols(symbols)
    if limit is not None:
        ordered_symbols = ordered_symbols[: max(0, int(limit or 0))]
    runtime_environment = to_text(environment).lower() or LIVE_ENVIRONMENT
    normalized_interval = normalize_interval(to_text(interval) or "5m")
    computed_ms = int(computed_at_ms or int(time.time() * 1000))
    window_max_bars = (
        int(signal_window_max_bars)
        if signal_window_max_bars is not None
        else _resolve_signal_window_max_bars(pb, runtime_environment)
    )
    targets = {
        to_text(symbol).upper(): dict(row or {})
        for symbol, row in (target_by_symbol or {}).items()
        if to_text(symbol).upper()
    }
    empty_summary = {
        "total": 0,
        "active_count": 0,
        "candidate_count": 0,
        "with_live_bar_count": 0,
        "window_active_count": 0,
        "window_valid_count": 0,
        "candidate_signal_count": 0,
        "blocked_count": 0,
        "near_expiry_count": 0,
        "confirmed_count": 0,
        "trace_error_count": 0,
    }
    if not ordered_symbols:
        return {"summary": empty_summary, "items": [], "returned_count": 0}

    bars_by_symbol = _load_timeline_bars_by_symbol(
        pb,
        environment=runtime_environment,
        symbols=ordered_symbols,
        interval=normalized_interval,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf(normalized_interval)}"',
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=6,
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
        max_pages=6,
    )

    latest_indicator_by_symbol = _pick_latest_by_symbol(indicator_records)
    latest_signal_by_symbol = _latest_signal_by_symbol(signal_records)

    items: list[dict[str, Any]] = []
    summary = dict(empty_summary)
    active_count = 0
    candidate_count = 0
    with_live_bar_count = 0
    window_active_count = 0
    window_valid_count = 0
    candidate_signal_count = 0
    blocked_count = 0
    near_expiry_count = 0
    confirmed_count = 0
    trace_error_count = 0

    for symbol in ordered_symbols:
        target = targets.get(symbol) or {}
        target_extra = parse_json_object(target.get("extra"))
        symbol_bars = bars_by_symbol.get(symbol, [])
        today_bars = [row for row in symbol_bars if market_start_ms <= to_int(row.get("bar_time_ms"), 0) < market_end_ms]
        latest_bar = today_bars[-1] if today_bars else (symbol_bars[-1] if symbol_bars else {})
        trace_result = _build_trace_for_symbol(
            environment=runtime_environment,
            symbol=symbol,
            bars=symbol_bars,
            signal_window_max_bars=window_max_bars,
        )
        trace = dict(trace_result.get("trace") or {})
        trace_error = to_text(trace_result.get("error"))
        latest_row = trace_result.get("latest_row") if isinstance(trace_result.get("latest_row"), dict) else {}
        admission_item = build_active_window_admission_item(trace, signal_window_max_bars=window_max_bars)
        window_flags = dict(admission_item.get("window_flags") or {})
        signal_state = dict(admission_item.get("signal_state") or {})
        component_groups = dict(admission_item.get("component_groups") or {})
        components = dict(admission_item.get("components") or {})
        indicator_extra = indicator_snapshot(latest_indicator_by_symbol.get(symbol))
        latest_signal = latest_signal_by_symbol.get(symbol) or {}
        latest_bar_time_ms = to_int(first_defined(latest_row.get("bar_time_ms"), latest_bar.get("bar_time_ms")), 0)
        price = to_float(first_defined(latest_row.get("close"), latest_bar.get("close"))) or 0.0
        freshness_min = max(0, int((computed_ms - latest_bar_time_ms) // 60000)) if latest_bar_time_ms > 0 else None
        target_status = effective_target_status(target)
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        blocked_reason = to_text(signal_state.get("filter_reason"))
        filter_reasons: list[str] = []
        for reason in [blocked_reason, *(trace.get("filters") or [])]:
            reason_text = to_text(reason)
            if reason_text and reason_text not in filter_reasons:
                filter_reasons.append(reason_text)
        candidate_signal = dict(signal_state.get("signal_payload") or {}) if signal_state.get("signal_payload") else None
        if not candidate_signal and latest_signal:
            candidate_signal = {
                "signal_id": to_text(latest_signal.get("signal_id")),
                "direction": to_text(latest_signal.get("direction")),
                "signal": to_text(latest_signal.get("signal")),
                "status": to_text(latest_signal.get("status")),
                "bar_time_ms": to_int(latest_signal.get("bar_time_ms"), 0),
                "us_time": to_text(latest_signal.get("us_time")),
            }
        candidate_signal_label = (
            to_text(signal_state.get("label"))
            if candidate_signal and to_text(signal_state.get("label")) != "无信号"
            else to_text(first_defined(
                (candidate_signal or {}).get("signal"),
                (candidate_signal or {}).get("direction"),
            ))
        )
        bars_remaining = to_int(admission_item.get("bars_remaining"), 0)
        status = to_text(admission_item.get("window_status")) or "no_window"
        chart_trace_url = _build_chart_trace_url(
            environment=runtime_environment,
            symbol=symbol,
            interval=normalized_interval,
            start_ms=market_start_ms,
            end_ms=latest_bar_time_ms or market_end_ms,
        )

        item = {
            "symbol": symbol,
            "status": status,
            "window_status": status,
            "target_status": target_status,
            "score": round(to_float(target.get("score")) or 0.0, 4),
            "target_score": round(to_float(target.get("score")) or 0.0, 4),
            "direction_bias": direction_bias,
            "exchange": to_text(first_defined(target.get("exchange"), latest_bar.get("exchange"))).upper(),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_us_time": to_text(first_defined(latest_row.get("us_time"), latest_bar.get("us_time"))),
            "latest_cn_time": format_cn_time(latest_bar_time_ms),
            "freshness_min": freshness_min,
            "price": round(price, 4) if price > 0 else 0.0,
            "atr_pct": round(to_float(first_defined(latest_row.get("atr_pct"), indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "window_state": to_text(admission_item.get("window_state")),
            "window_max_bars": window_max_bars,
            "window_flags": window_flags,
            "signal_state": signal_state,
            "sd_upper_active": bool(admission_item.get("sd_upper_active")),
            "sd_upper_valid": bool(admission_item.get("sd_upper_valid")),
            "sd_upper_used": bool(admission_item.get("sd_upper_used")),
            "sd_upper_age_bars": to_int(admission_item.get("sd_upper_age_bars"), 0),
            "sd_lower_active": bool(admission_item.get("sd_lower_active")),
            "sd_lower_valid": bool(admission_item.get("sd_lower_valid")),
            "sd_lower_used": bool(admission_item.get("sd_lower_used")),
            "sd_lower_age_bars": to_int(admission_item.get("sd_lower_age_bars"), 0),
            "upper_window": admission_item.get("upper_window") if isinstance(admission_item.get("upper_window"), dict) else {},
            "lower_window": admission_item.get("lower_window") if isinstance(admission_item.get("lower_window"), dict) else {},
            "bars_remaining": bars_remaining,
            "component_progress": admission_item.get("component_progress"),
            "component_detail": component_groups,
            "component_groups": component_groups,
            "components": components,
            "collected_components": admission_item.get("collected_components") or [],
            "missing_components": admission_item.get("missing_components") or [],
            "candidate_signal": candidate_signal,
            "candidate_signal_label": candidate_signal_label,
            "blocked_reason": blocked_reason,
            "filter_reasons": filter_reasons,
            "chart_trace_url": chart_trace_url,
            "trace_url": chart_trace_url,
            "chart_trace_request": _build_chart_trace_request(
                environment=runtime_environment,
                symbol=symbol,
                interval=normalized_interval,
                start_ms=market_start_ms,
                end_ms=latest_bar_time_ms or market_end_ms,
            ),
            "trace_stage": to_text(signal_state.get("stage")) or "none",
            "trace_error": trace_error,
        }
        items.append(item)

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if latest_bar_time_ms > 0:
            with_live_bar_count += 1
        if item["sd_upper_active"] or item["sd_lower_active"]:
            window_active_count += 1
        if item["sd_upper_valid"] or item["sd_lower_valid"]:
            window_valid_count += 1
        if candidate_signal:
            candidate_signal_count += 1
        if status == "blocked":
            blocked_count += 1
        if status == "near_expiry":
            near_expiry_count += 1
        if status == "confirmed":
            confirmed_count += 1
        if trace_error:
            trace_error_count += 1

    summary.update(
        {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "with_live_bar_count": with_live_bar_count,
            "window_active_count": window_active_count,
            "window_valid_count": window_valid_count,
            "candidate_signal_count": candidate_signal_count,
            "blocked_count": blocked_count,
            "near_expiry_count": near_expiry_count,
            "confirmed_count": confirmed_count,
            "trace_error_count": trace_error_count,
        }
    )
    return {"summary": summary, "items": items, "returned_count": len(items)}


def build_active_window_progress_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    runtime_environment = normalize_environment(payload.get("environment"), LIVE_ENVIRONMENT)
    if runtime_environment not in SUPPORTED_ENVIRONMENTS:
        return {"ok": False, "error": "unsupported_environment", "environment": runtime_environment}, 400

    interval = normalize_interval(to_text(payload.get("interval")) or "5m")
    if interval != "5m":
        return {"ok": False, "error": "unsupported_interval", "interval": interval, "supported_intervals": ["5m"]}, 400

    requested_status = to_text(payload.get("status")).lower() or "active"
    if requested_status not in SUPPORTED_STATUSES:
        return {"ok": False, "error": "unsupported_status", "status": requested_status}, 400

    limit = max(1, min(200, to_int(payload.get("limit"), DEFAULT_LIMIT)))
    current_date = current_market_date(time_strings)
    requested_market_date = (
        to_text(first_defined(payload.get("marketDate"), payload.get("market_date"), payload.get("date")))
        or current_date
    )
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(requested_market_date)
        market_date = requested_market_date
    except Exception:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(current_date)
        market_date = current_date

    computed_at_ms = int(time.time() * 1000)
    signal_window_max_bars = _resolve_signal_window_max_bars(pb, runtime_environment)

    target_rows = pb.get_records(
        "ibkr_targets",
        filter=(
            f'environment = "{escape_filter(runtime_environment)}" && '
            f'date = "{escape_filter(market_date)}" && '
            f"{_status_filter(requested_status)}"
        ),
        sort="-updated",
        per_page=200,
        page=1,
    )
    target_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_symbols: list[str] = []
    for row in target_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        status = effective_target_status(row)
        if not symbol or symbol in target_by_symbol or status not in TODAY_TARGET_STATUSES:
            continue
        if requested_status != "all" and status != requested_status:
            continue
        target_by_symbol[symbol] = dict(row)
        ordered_symbols.append(symbol)
        if len(ordered_symbols) >= limit:
            break

    empty_summary = {
        "total": 0,
        "active_count": 0,
        "candidate_count": 0,
        "with_live_bar_count": 0,
        "window_active_count": 0,
        "window_valid_count": 0,
        "candidate_signal_count": 0,
        "blocked_count": 0,
        "near_expiry_count": 0,
        "confirmed_count": 0,
        "trace_error_count": 0,
    }
    if not ordered_symbols:
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            "current_market_date": current_date,
            "status": requested_status,
            "interval": interval,
            "limit": limit,
            "signal_window_max_bars": signal_window_max_bars,
            "computed_at_ms": computed_at_ms,
            "computed_at_us": format_et_datetime(computed_at_ms),
            "computed_at_cn": format_cn_time(computed_at_ms),
            "summary": empty_summary,
            "items": [],
            "source": "ibkr-api",
        }, 200

    bars_by_symbol = _load_timeline_bars_by_symbol(
        pb,
        environment=runtime_environment,
        symbols=ordered_symbols,
        interval=interval,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf(interval)}"',
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=6,
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
        max_pages=6,
    )

    latest_indicator_by_symbol = _pick_latest_by_symbol(indicator_records)
    latest_signal_by_symbol = _latest_signal_by_symbol(signal_records)

    items: list[dict[str, Any]] = []
    summary = dict(empty_summary)
    active_count = 0
    candidate_count = 0
    with_live_bar_count = 0
    window_active_count = 0
    window_valid_count = 0
    candidate_signal_count = 0
    blocked_count = 0
    near_expiry_count = 0
    confirmed_count = 0
    trace_error_count = 0

    for symbol in ordered_symbols:
        target = target_by_symbol.get(symbol) or {}
        target_extra = parse_json_object(target.get("extra"))
        symbol_bars = bars_by_symbol.get(symbol, [])
        today_bars = [row for row in symbol_bars if market_start_ms <= to_int(row.get("bar_time_ms"), 0) < market_end_ms]
        latest_bar = today_bars[-1] if today_bars else (symbol_bars[-1] if symbol_bars else {})
        trace_result = _build_trace_for_symbol(
            environment=runtime_environment,
            symbol=symbol,
            bars=symbol_bars,
            signal_window_max_bars=signal_window_max_bars,
        )
        trace = dict(trace_result.get("trace") or {})
        trace_error = to_text(trace_result.get("error"))
        latest_row = trace_result.get("latest_row") if isinstance(trace_result.get("latest_row"), dict) else {}
        window_flags = dict(trace.get("window_flags") or {})
        component_flags = dict(trace.get("component_flags") or {})
        signal_state = dict(trace.get("signal_state") or {})
        component_groups = _component_groups(component_flags, window_flags)
        component_rollup = _component_rollup(component_groups, window_flags)
        indicator_extra = indicator_snapshot(latest_indicator_by_symbol.get(symbol))
        latest_signal = latest_signal_by_symbol.get(symbol) or {}
        latest_bar_time_ms = to_int(first_defined(latest_row.get("bar_time_ms"), latest_bar.get("bar_time_ms")), 0)
        price = to_float(first_defined(latest_row.get("close"), latest_bar.get("close"))) or 0.0
        freshness_min = max(0, int((computed_at_ms - latest_bar_time_ms) // 60000)) if latest_bar_time_ms > 0 else None
        target_status = effective_target_status(target)
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        blocked_reason = to_text(signal_state.get("filter_reason"))
        filter_reasons: list[str] = []
        for reason in [blocked_reason, *(trace.get("filters") or [])]:
            reason_text = to_text(reason)
            if reason_text and reason_text not in filter_reasons:
                filter_reasons.append(reason_text)
        candidate_signal = dict(signal_state.get("signal_payload") or {}) if signal_state.get("signal_payload") else None
        if not candidate_signal and latest_signal:
            candidate_signal = {
                "signal_id": to_text(latest_signal.get("signal_id")),
                "direction": to_text(latest_signal.get("direction")),
                "signal": to_text(latest_signal.get("signal")),
                "status": to_text(latest_signal.get("status")),
                "bar_time_ms": to_int(latest_signal.get("bar_time_ms"), 0),
                "us_time": to_text(latest_signal.get("us_time")),
            }
        candidate_signal_label = (
            to_text(signal_state.get("label"))
            if candidate_signal and to_text(signal_state.get("label")) != "无信号"
            else to_text(first_defined(
                (candidate_signal or {}).get("signal"),
                (candidate_signal or {}).get("direction"),
            ))
        )
        bars_remaining = _bars_remaining(window_flags, signal_window_max_bars)
        status = _window_status(
            signal_state=signal_state,
            window_flags=window_flags,
            bars_remaining=bars_remaining,
            blocked_reason=blocked_reason,
        )
        chart_trace_url = _build_chart_trace_url(
            environment=runtime_environment,
            symbol=symbol,
            interval=interval,
            start_ms=market_start_ms,
            end_ms=latest_bar_time_ms or market_end_ms,
        )

        item = {
            "symbol": symbol,
            "status": status,
            "window_status": status,
            "target_status": target_status,
            "score": round(to_float(target.get("score")) or 0.0, 4),
            "target_score": round(to_float(target.get("score")) or 0.0, 4),
            "direction_bias": direction_bias,
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_us_time": to_text(first_defined(latest_row.get("us_time"), latest_bar.get("us_time"))),
            "freshness_min": freshness_min,
            "price": round(price, 4) if price > 0 else 0.0,
            "atr_pct": round(to_float(first_defined(latest_row.get("atr_pct"), indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "window_state": _window_state(window_flags),
            "window_max_bars": signal_window_max_bars,
            "window_flags": window_flags,
            "signal_state": signal_state,
            "sd_upper_active": bool(window_flags.get("sd_upper_active")),
            "sd_upper_valid": bool(window_flags.get("sd_upper_valid")),
            "sd_upper_used": bool(window_flags.get("sd_upper_used")),
            "sd_upper_age_bars": to_int(window_flags.get("sd_upper_age_bars"), 0),
            "sd_lower_active": bool(window_flags.get("sd_lower_active")),
            "sd_lower_valid": bool(window_flags.get("sd_lower_valid")),
            "sd_lower_used": bool(window_flags.get("sd_lower_used")),
            "sd_lower_age_bars": to_int(window_flags.get("sd_lower_age_bars"), 0),
            "upper_window": _side_window("upper", window_flags, signal_window_max_bars),
            "lower_window": _side_window("lower", window_flags, signal_window_max_bars),
            "bars_remaining": bars_remaining,
            "component_progress": component_rollup["progress"],
            "component_detail": component_groups,
            "component_groups": component_groups,
            "components": {
                "collected": component_rollup["collected"],
                "missing": component_rollup["missing"],
                "best_group": component_rollup["best_group"],
                "ready_groups": component_rollup["ready_groups"],
            },
            "collected_components": component_rollup["collected"],
            "missing_components": component_rollup["missing"],
            "candidate_signal": candidate_signal,
            "candidate_signal_label": candidate_signal_label,
            "blocked_reason": blocked_reason,
            "filter_reasons": filter_reasons,
            "chart_trace_url": chart_trace_url,
            "trace_url": chart_trace_url,
            "chart_trace_request": _build_chart_trace_request(
                environment=runtime_environment,
                symbol=symbol,
                interval=interval,
                start_ms=market_start_ms,
                end_ms=latest_bar_time_ms or market_end_ms,
            ),
            "trace_stage": to_text(signal_state.get("stage")) or "none",
            "trace_error": trace_error,
        }
        items.append(item)

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if latest_bar_time_ms > 0:
            with_live_bar_count += 1
        if item["sd_upper_active"] or item["sd_lower_active"]:
            window_active_count += 1
        if item["sd_upper_valid"] or item["sd_lower_valid"]:
            window_valid_count += 1
        if candidate_signal:
            candidate_signal_count += 1
        if status == "blocked":
            blocked_count += 1
        if status == "near_expiry":
            near_expiry_count += 1
        if status == "confirmed":
            confirmed_count += 1
        if trace_error:
            trace_error_count += 1

    summary.update(
        {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "with_live_bar_count": with_live_bar_count,
            "window_active_count": window_active_count,
            "window_valid_count": window_valid_count,
            "candidate_signal_count": candidate_signal_count,
            "blocked_count": blocked_count,
            "near_expiry_count": near_expiry_count,
            "confirmed_count": confirmed_count,
            "trace_error_count": trace_error_count,
        }
    )

    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        "current_market_date": current_date,
        "status": requested_status,
        "interval": interval,
        "limit": limit,
        "signal_window_max_bars": signal_window_max_bars,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": format_et_datetime(computed_at_ms),
        "computed_at_cn": format_cn_time(computed_at_ms),
        "summary": summary,
        "returned_count": len(items),
        "items": items,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_active_window_items_for_symbols", "build_active_window_progress_response"]
