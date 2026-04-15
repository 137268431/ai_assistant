from __future__ import annotations

import time

from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.market.timeframe_utils import (
    build_runtime_timestamps,
    build_signal_id,
    interval_to_chart_tf,
    interval_to_ms,
    normalize_interval,
)


def _api_app():
    from . import app as api_app

    return api_app


def load_chart_timeline_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    visible_pages = max(1, (api_app.CHART_TIMELINE_VISIBLE_LIMIT + 199) // 200 + 1)
    warmup_pages = max(1, (warmup_bars + 199) // 200 + 1)

    base_filter_parts = [
        f'symbol = "{normalized_symbol}"',
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
    ]
    if end_ms > 0:
        base_filter_parts.append(f"bar_time_ms <= {int(end_ms)}")

    visible_filter_parts = list(base_filter_parts)
    if start_ms > 0:
        visible_filter_parts.append(f"bar_time_ms >= {int(start_ms)}")

    visible_rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(visible_filter_parts),
        sort="bar_time_ms",
        max_pages=visible_pages,
    )
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_rows = []
    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_filter_parts = list(base_filter_parts)
        warmup_filter_parts.append(f"bar_time_ms < {warmup_anchor_ms}")
        warmup_rows = api_app.pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(warmup_filter_parts),
            sort="-bar_time_ms",
            max_pages=warmup_pages,
        )
        warmup_rows = list(reversed(warmup_rows[:warmup_bars]))

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


def build_chart_source_window_from_rows(
    rows,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    deduped = {}
    for row in rows or []:
        bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        if end_ms > 0 and bar_ms > int(end_ms):
            continue
        deduped[bar_ms] = dict(row)

    ordered_rows = [deduped[bar_ms] for bar_ms in sorted(deduped)]
    visible_rows = ordered_rows
    if start_ms > 0:
        visible_rows = [
            row for row in visible_rows
            if int(row.get("bar_time_ms", 0) or 0) >= int(start_ms)
        ]
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    warmup_rows = []
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_rows = [
            row for row in ordered_rows
            if int(row.get("bar_time_ms", 0) or 0) < warmup_anchor_ms
        ][-warmup_bars:]

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


def build_chart_indicator_row(environment: str, symbol: str, interval: str, row: dict) -> dict:
    api_app = _api_app()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    daily_fields = api_app.get_daily_change_fields(
        environment,
        normalized_symbol,
        float(row.get("close", 0) or 0),
        bar_ms,
    )
    timestamps = build_runtime_timestamps()
    indicator = {
        **{key: value for key, value in (row or {}).items() if key != "signal"},
        **daily_fields,
        **timestamps,
        "environment": environment,
        "symbol": normalized_symbol,
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
    }
    return indicator


def build_chart_signal_row(environment: str, symbol: str, interval: str, row: dict) -> dict | None:
    api_app = _api_app()
    raw_signal = row.get("signal")
    if not raw_signal:
        return None

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    bar_index = int(row.get("bar_index", row.get("bar_count", 0)) or 0)
    daily_fields = api_app.get_daily_change_fields(
        environment,
        normalized_symbol,
        float(row.get("close", 0) or 0),
        bar_ms,
    )
    signal_type = str(raw_signal.get("signal", "") or "")
    signal_extra = dict(raw_signal.get("extra") or {})
    signal_extra.update(
        {
            **daily_fields,
            **build_runtime_timestamps(),
            "chart_tf": chart_tf,
            "bar_time_ms": bar_ms,
            "bar_index": bar_index,
            "close": round(float(row.get("close", 0) or 0), 2),
            "atr": signal_extra.get("atr_raw", signal_extra.get("atr", row.get("atr", 0))),
            "atr_pct": row.get("atr_pct", signal_extra.get("atr_pct", 0)),
            "environment": environment,
            "source": "ibkr_compute_timeline",
            "signal_source": "ibkr_compute_timeline",
            "signal_source_label": "IBKR 图表回放",
            "signal_source_detail": "来自缓存 bars 时间线重算",
            "computed_from": "ibkr_bars",
        }
    )

    rr_value = raw_signal.get("rr", "")
    rr_text = f"{float(rr_value):.1f}:1" if isinstance(rr_value, (int, float)) else str(rr_value or "")

    return {
        "environment": environment,
        "symbol": normalized_symbol,
        "signal_id": build_signal_id(normalized_symbol, bar_ms, signal_type),
        "direction": raw_signal.get("direction", ""),
        "signal": signal_type,
        "limit_price": round(float(row.get("close", 0) or 0), 2),
        "entry": raw_signal.get("entry", 0),
        "stop_loss": raw_signal.get("stop_loss", 0),
        "take_profit": raw_signal.get("take_profit", 0),
        "rr": rr_text,
        "shares": raw_signal.get("shares", 0),
        "exchange": str(row.get("exchange", "") or "").upper(),
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "reason": raw_signal.get("reason", ""),
        "us_time": row.get("us_time", ""),
        "cn_time": row.get("cn_time", ""),
        "date": str(row.get("us_time", "") or "")[:10],
        "bar_time_ms": bar_ms,
        "bar_index": bar_index,
        "status": "computed",
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
        "extra": signal_extra,
    }


def build_chart_timeline_payload_from_source(
    environment: str,
    symbol: str,
    interval: str,
    source: dict,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    source_rows = source.get("source_rows") or []
    visible_rows = source.get("visible_rows") or []
    source_meta = source.get("meta") or {}

    if not visible_rows:
        return {
            "ok": True,
            "bars": [],
            "indicator_timeline": [],
            "latest_indicator": None,
            "signals": [],
            "meta": {
                "environment": runtime_environment,
                "symbol": normalized_symbol,
                "interval": chart_tf,
                "start_ms": int(start_ms or 0),
                "end_ms": int(end_ms or 0),
                "visible_bar_count": 0,
                "source_bar_count": len(source_rows),
                "warmup_bars": int(source.get("warmup_limit", 0) or 0),
                "warmup_used": int(source.get("warmup_used", 0) or 0),
                "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
                "reason": "no_visible_bars",
                **source_meta,
            },
        }

    timeline = build_runtime_timeline(
        normalized_symbol,
        normalized_interval,
        source_rows,
        params=api_app.get_signal_generator_params(runtime_environment),
        include_signals=include_signals,
        visible_start_ms=int(start_ms or 0),
        visible_end_ms=int(end_ms or 0),
    )
    timeline_rows = timeline.get("rows") or []
    bars = [
        {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "exchange": str(row.get("exchange", "") or "").upper(),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": row.get("us_time", ""),
            "cn_time": row.get("cn_time", ""),
            "session_type": row.get("session_type", "regular"),
            "open": round(float(row.get("open", 0) or 0), 4),
            "high": round(float(row.get("high", 0) or 0), 4),
            "low": round(float(row.get("low", 0) or 0), 4),
            "close": round(float(row.get("close", 0) or 0), 4),
            "volume": round(float(row.get("volume", 0) or 0), 4),
        }
        for row in timeline_rows
    ]
    indicators = [
        build_chart_indicator_row(runtime_environment, normalized_symbol, normalized_interval, row)
        for row in timeline_rows
    ]
    signals = []
    if include_signals and normalized_interval == "5m":
        signals = [
            signal
            for signal in (
                build_chart_signal_row(runtime_environment, normalized_symbol, normalized_interval, row)
                for row in timeline_rows
            )
            if signal
        ]

    return {
        "ok": True,
        "bars": bars,
        "indicator_timeline": indicators,
        "latest_indicator": indicators[-1] if indicators else None,
        "signals": signals,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "visible_bar_count": len(bars),
            "source_bar_count": len(source_rows),
            "warmup_bars": int(source.get("warmup_limit", 0) or 0),
            "warmup_used": int(source.get("warmup_used", 0) or 0),
            "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
            **source_meta,
        },
    }


def build_chart_timeline_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)

    api_app.refresh_daily_close_cache([runtime_environment])
    source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    return build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )


def get_chart_compare_request_period(interval: str, start_ms: int = 0, end_ms: int = 0) -> str:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    interval_ms = max(1, int(interval_to_ms(normalized_interval) or 0))
    effective_end_ms = int(end_ms or 0) or int(time.time() * 1000)
    fallback_start_ms = max(0, effective_end_ms - (api_app.CHART_TIMELINE_VISIBLE_LIMIT * interval_ms))
    effective_start_ms = int(start_ms or 0) if int(start_ms or 0) > 0 else fallback_start_ms
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    day_ms = 24 * 60 * 60 * 1000
    visible_span_ms = max(interval_ms, effective_end_ms - effective_start_ms)
    warmup_span_ms = max(3 * day_ms, warmup_bars * interval_ms + (2 * day_ms))
    request_days = max(1, min(730, (visible_span_ms + warmup_span_ms + day_ms - 1) // day_ms))
    return f"{int(request_days)}d"


def load_chart_compare_ibkr_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    service = api_app.get_ibkr_service()
    if not service:
        raise RuntimeError("IBKR service not initialized")
    if not hasattr(service, "conid_resolver") or service.conid_resolver is None:
        raise RuntimeError("IBKR contract resolver unavailable")
    if not hasattr(service, "data_backfill") or service.data_backfill is None:
        raise RuntimeError("IBKR history fetch unavailable")

    api_app._maybe_restore_ibkr_service(service)
    service_status = service.status() if hasattr(service, "status") else {}
    gateway_running = bool((service_status.get("gateway") or {}).get("running"))
    session_authenticated = bool((service_status.get("session") or {}).get("authenticated"))
    if not gateway_running:
        raise RuntimeError("IBKR gateway not running")
    if not session_authenticated:
        raise RuntimeError("IBKR session not authenticated")

    conid = int(service.conid_resolver.resolve(normalized_symbol) or 0)
    if conid <= 0:
        raise RuntimeError(f"Cannot resolve conid for {normalized_symbol}")

    symbol_meta = api_app.refresh_symbol_metadata().get(normalized_symbol, {})
    request_period = get_chart_compare_request_period(normalized_interval, start_ms=start_ms, end_ms=end_ms)
    fetched_rows = service.data_backfill.fetch_history(
        conid,
        normalized_symbol,
        interval=normalized_interval,
        exchange=str(symbol_meta.get("exchange") or ""),
        repair=True,
        request_period=request_period,
    )

    normalized_rows = []
    for row in fetched_rows:
        payload = api_app.normalize_bar_environment(row, runtime_environment)
        payload["source"] = "ibkr_chart_compare"
        extra = api_app.parse_json_object(payload.get("extra"))
        extra["compare_chain"] = "ibkr_api"
        extra["request_period"] = request_period
        payload["extra"] = extra
        normalized_rows.append(payload)

    source = build_chart_source_window_from_rows(
        normalized_rows,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    source["meta"] = {
        "chain": "ibkr_api",
        "conid": conid,
        "gateway_running": gateway_running,
        "session_authenticated": session_authenticated,
        "request_period": request_period,
        "fetched_bar_count": len(normalized_rows),
    }
    return source


CHART_COMPARE_BAR_FIELDS = {
    "open": 2,
    "high": 2,
    "low": 2,
    "close": 2,
    "volume": 0,
}
CHART_COMPARE_INDICATOR_FIELDS = {
    "ema_fast": 2,
    "ema_slow": 2,
    "ema_trend": 2,
    "vwap": 2,
    "crsi": 2,
    "obv_rsi": 2,
    "atr_pct": 2,
}
CHART_COMPARE_SIGNAL_FIELDS = {
    "entry": 2,
    "stop_loss": 2,
    "take_profit": 2,
}


def _normalize_chart_compare_number(value, digits: int = 2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, digits)


def _normalize_chart_compare_values(raw: dict | None, numeric_fields: dict, text_fields=(), int_fields=()) -> dict | None:
    api_app = _api_app()
    if not raw or not isinstance(raw, dict):
        return None
    normalized = {}
    for field, digits in numeric_fields.items():
        normalized[field] = _normalize_chart_compare_number(raw.get(field), digits)
    for field in text_fields:
        normalized[field] = str(raw.get(field, "") or "")
    for field in int_fields:
        normalized[field] = api_app.coerce_int(raw.get(field), 0)
    return normalized


def _build_chart_compare_group(stored_item: dict | None, ibkr_item: dict | None, numeric_fields: dict, text_fields=(), int_fields=()) -> dict:
    if stored_item is None and ibkr_item is None:
        return {"status": "absent", "count": 0, "fields": [], "values": {}}
    if stored_item is None:
        return {"status": "missing_stored", "count": 0, "fields": [], "values": {}}
    if ibkr_item is None:
        return {"status": "missing_ibkr", "count": 0, "fields": [], "values": {}}

    stored_values = _normalize_chart_compare_values(stored_item, numeric_fields, text_fields=text_fields, int_fields=int_fields) or {}
    ibkr_values = _normalize_chart_compare_values(ibkr_item, numeric_fields, text_fields=text_fields, int_fields=int_fields) or {}
    diff_values = {}
    for field in sorted(set(stored_values.keys()) | set(ibkr_values.keys())):
        stored_value = stored_values.get(field)
        ibkr_value = ibkr_values.get(field)
        if stored_value == ibkr_value:
            continue
        diff_entry = {"stored": stored_value, "ibkr": ibkr_value}
        if isinstance(stored_value, (int, float)) and isinstance(ibkr_value, (int, float)):
            diff_entry["delta"] = round(ibkr_value - stored_value, 4)
        diff_values[field] = diff_entry

    fields = list(diff_values.keys())
    return {
        "status": "mismatch" if fields else "match",
        "count": len(fields),
        "fields": fields,
        "values": diff_values,
    }


def _build_chart_compare_summary(stored_timeline: dict, ibkr_timeline: dict) -> dict:
    stored_bars = stored_timeline.get("bars") or []
    ibkr_bars = ibkr_timeline.get("bars") or []
    stored_indicators = stored_timeline.get("indicator_timeline") or []
    ibkr_indicators = ibkr_timeline.get("indicator_timeline") or []
    stored_signals = stored_timeline.get("signals") or []
    ibkr_signals = ibkr_timeline.get("signals") or []

    stored_bar_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in stored_bars
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }
    ibkr_bar_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in ibkr_bars
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }
    stored_indicator_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in stored_indicators
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }
    ibkr_indicator_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in ibkr_indicators
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }
    stored_signal_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in stored_signals
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }
    ibkr_signal_map = {
        int(item.get("bar_time_ms", 0) or 0): item
        for item in ibkr_signals
        if int(item.get("bar_time_ms", 0) or 0) > 0
    }

    timeline = []
    mismatch_examples = []
    summary = {
        "stored_visible_bars": len(stored_bars),
        "ibkr_visible_bars": len(ibkr_bars),
        "matched_bar_count": 0,
        "missing_stored_bar_count": 0,
        "missing_ibkr_bar_count": 0,
        "bar_mismatch_count": 0,
        "indicator_mismatch_count": 0,
        "signal_mismatch_count": 0,
    }

    all_bar_times = sorted(set(stored_bar_map.keys()) | set(ibkr_bar_map.keys()))
    for bar_time_ms in all_bar_times:
        stored_bar = stored_bar_map.get(bar_time_ms)
        ibkr_bar = ibkr_bar_map.get(bar_time_ms)
        stored_indicator = stored_indicator_map.get(bar_time_ms)
        ibkr_indicator = ibkr_indicator_map.get(bar_time_ms)
        stored_signal = stored_signal_map.get(bar_time_ms)
        ibkr_signal = ibkr_signal_map.get(bar_time_ms)

        bar_diff = _build_chart_compare_group(
            stored_bar,
            ibkr_bar,
            CHART_COMPARE_BAR_FIELDS,
            text_fields=("session_type",),
        )
        indicator_diff = _build_chart_compare_group(
            stored_indicator,
            ibkr_indicator,
            CHART_COMPARE_INDICATOR_FIELDS,
            int_fields=("trend_dir",),
        )
        signal_diff = _build_chart_compare_group(
            stored_signal,
            ibkr_signal,
            CHART_COMPARE_SIGNAL_FIELDS,
            text_fields=("signal", "direction", "status"),
        )

        if bar_diff["status"] == "match":
            summary["matched_bar_count"] += 1
        elif bar_diff["status"] == "missing_stored":
            summary["missing_stored_bar_count"] += 1
        elif bar_diff["status"] == "missing_ibkr":
            summary["missing_ibkr_bar_count"] += 1
        elif bar_diff["status"] == "mismatch":
            summary["bar_mismatch_count"] += 1

        if indicator_diff["status"] == "mismatch":
            summary["indicator_mismatch_count"] += 1
        if signal_diff["status"] == "mismatch":
            summary["signal_mismatch_count"] += 1

        compare_row = {
            "bar_time_ms": bar_time_ms,
            "us_time": (
                str((stored_bar or {}).get("us_time") or "")
                or str((ibkr_bar or {}).get("us_time") or "")
                or str((stored_indicator or {}).get("us_time") or "")
                or str((ibkr_indicator or {}).get("us_time") or "")
            ),
            "status": {
                "bar": bar_diff["status"],
                "indicator": indicator_diff["status"],
                "signal": signal_diff["status"],
            },
            "stored": {
                "bar": stored_bar,
                "indicator": stored_indicator,
                "signal": stored_signal,
            },
            "ibkr": {
                "bar": ibkr_bar,
                "indicator": ibkr_indicator,
                "signal": ibkr_signal,
            },
            "diff": {
                "bar": bar_diff,
                "indicator": indicator_diff,
                "signal": signal_diff,
            },
        }
        timeline.append(compare_row)

        severity = 0
        if bar_diff["status"] in {"missing_stored", "missing_ibkr"}:
            severity += 40
        elif bar_diff["status"] == "mismatch":
            severity += 30
        if indicator_diff["status"] in {"missing_stored", "missing_ibkr"}:
            severity += 20
        elif indicator_diff["status"] == "mismatch":
            severity += 15
        if signal_diff["status"] in {"missing_stored", "missing_ibkr"}:
            severity += 10
        elif signal_diff["status"] == "mismatch":
            severity += 8

        if severity > 0:
            mismatch_examples.append(
                {
                    "bar_time_ms": bar_time_ms,
                    "us_time": compare_row["us_time"],
                    "status": compare_row["status"],
                    "bar_fields": bar_diff["fields"],
                    "indicator_fields": indicator_diff["fields"],
                    "signal_fields": signal_diff["fields"],
                    "severity": severity,
                }
            )

    api_app = _api_app()
    mismatch_examples.sort(
        key=lambda item: (
            -int(item.get("severity", 0) or 0),
            -int(item.get("bar_time_ms", 0) or 0),
        )
    )
    return {
        "summary": summary,
        "timeline": timeline,
        "mismatch_examples": mismatch_examples[:api_app.CHART_COMPARE_MAX_MISMATCH_EXAMPLES],
    }


def build_chart_compare_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)

    api_app.refresh_daily_close_cache([runtime_environment])
    stored_source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    stored_source["meta"] = {"chain": "stored_bars"}
    stored_timeline = build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        stored_source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )

    ibkr_source = load_chart_compare_ibkr_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    ibkr_timeline = build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        ibkr_source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )

    comparison = _build_chart_compare_summary(stored_timeline, ibkr_timeline)
    return {
        "ok": True,
        "stored_timeline": stored_timeline,
        "ibkr_timeline": ibkr_timeline,
        "comparison": comparison,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": interval_to_chart_tf(normalized_interval),
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "include_signals": bool(include_signals and normalized_interval == "5m"),
            "stored": stored_timeline.get("meta") or {},
            "ibkr": ibkr_timeline.get("meta") or {},
        },
    }
