from __future__ import annotations

from ibkr_compute.api.chart.timeline.runtime import _api_app


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


__all__ = [
    "CHART_COMPARE_BAR_FIELDS",
    "CHART_COMPARE_INDICATOR_FIELDS",
    "CHART_COMPARE_SIGNAL_FIELDS",
    "_build_chart_compare_group",
    "_build_chart_compare_summary",
    "_normalize_chart_compare_number",
    "_normalize_chart_compare_values",
]
