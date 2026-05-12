"""Target admission reason and trigger timeline helpers."""

from __future__ import annotations

from typing import Any

from ibkr_compute.market.timeframe_utils import format_cn_time, format_us_time

from .daily_scanner_constants import DAILY_SCAN_REASON_RULES


_INDICATOR_LABELS = {
    "fractal_bull": "多头分形",
    "fractal_bear": "空头分形",
    "ema_bullish": "EMA多头",
    "ema_bearish": "EMA空头",
    "sd_lower": "SD下轨触碰",
    "sd_upper": "SD上轨触碰",
}

_TIMELINE_PRIORITY = {
    "day_gain": 10,
    "sd_admission": 20,
    "sd_upper": 30,
    "sd_lower": 31,
    "ema_bullish": 40,
    "ema_bearish": 41,
    "fractal_bull": 50,
    "fractal_bear": 51,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _format_number(value: float, digits: int = 2) -> str:
    rounded = round(float(value or 0.0), digits)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _format_pct(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{_format_number(value, 2)}%"


def _event_time_fields(triggered_at_ms: int, us_time: str = "", cn_time: str = "") -> dict[str, Any]:
    bar_ms = _safe_int(triggered_at_ms)
    return {
        "triggered_at_ms": bar_ms,
        "us_time": str(us_time or (format_us_time(bar_ms) if bar_ms > 0 else "")).strip(),
        "cn_time": str(cn_time or (format_cn_time(bar_ms) if bar_ms > 0 else "")).strip(),
    }


def _normalize_event(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    key = str(event.get("key") or "").strip()
    if not key:
        return None
    timeframe = str(event.get("timeframe") or "").strip()
    triggered_at_ms = _safe_int(event.get("triggered_at_ms") or event.get("bar_time_ms"))
    normalized = {
        "key": key,
        "label": str(event.get("label") or _INDICATOR_LABELS.get(key) or key).strip(),
        "timeframe": timeframe,
        "source": str(event.get("source") or "").strip(),
        "precision": str(event.get("precision") or "").strip(),
        **_event_time_fields(
            triggered_at_ms,
            str(event.get("us_time") or ""),
            str(event.get("cn_time") or ""),
        ),
    }
    for field in ("value", "value_label", "threshold", "threshold_label", "details"):
        if field in event and event.get(field) not in (None, ""):
            normalized[field] = event.get(field)
    return normalized


def _dedupe_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_event in events:
        event = _normalize_event(raw_event)
        if not event:
            continue
        key = (str(event.get("key") or ""), str(event.get("timeframe") or ""))
        existing = selected.get(key)
        existing_ms = _safe_int((existing or {}).get("triggered_at_ms"))
        event_ms = _safe_int(event.get("triggered_at_ms"))
        if existing is None or (event_ms > 0 and (existing_ms <= 0 or event_ms < existing_ms)):
            selected[key] = event
    return sorted(
        selected.values(),
        key=lambda item: (
            _TIMELINE_PRIORITY.get(str(item.get("key") or ""), 500),
            _safe_int(item.get("triggered_at_ms")) or 9_999_999_999_999,
            str(item.get("timeframe") or ""),
        ),
    )


def build_indicator_trigger_events_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    timeframe: str,
    bar_time_ms: int = 0,
    us_time: str = "",
    cn_time: str = "",
    source: str = "indicator_snapshot",
    precision: str = "latest_snapshot",
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    event_ms = _safe_int(bar_time_ms or snapshot.get("bar_time_ms"))
    event_us = str(us_time or snapshot.get("us_time") or "").strip()
    event_cn = str(cn_time or snapshot.get("cn_time") or "").strip()
    events: list[dict[str, Any]] = []
    for rule_key, _rule_label in DAILY_SCAN_REASON_RULES:
        if not _truthy(snapshot.get(rule_key)):
            continue
        events.append(
            {
                "key": rule_key,
                "label": _INDICATOR_LABELS.get(rule_key, rule_key),
                "timeframe": str(timeframe or "").strip(),
                "source": source,
                "precision": precision,
                **_event_time_fields(event_ms, event_us, event_cn),
            }
        )
    return events


def build_daily_scan_trigger_timeline(
    *,
    metric_row: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    day_gain_triggered: bool,
    day_gain_trigger_pct: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in metric_row.get("indicator_trigger_events") or []:
        if isinstance(item, dict):
            events.append(dict(item))

    if day_gain_triggered:
        day_gain_ms = _safe_int(metric_row.get("day_gain_triggered_at_ms") or metric_row.get("latest_bar_time_ms"))
        day_gain_pct = _safe_float(metric_row.get("day_gain_triggered_pct"), _safe_float(metric_row.get("day_change_pct")))
        day_gain_price = _safe_float(metric_row.get("day_gain_triggered_price"))
        event = {
            "key": "day_gain",
            "label": f"涨幅>={_format_pct(day_gain_trigger_pct)}",
            "source": str(metric_row.get("day_gain_trigger_source") or "5m_close").strip(),
            "precision": "first_cross" if _safe_int(metric_row.get("day_gain_triggered_at_ms")) > 0 else "latest_bar",
            "value": round(day_gain_pct, 4),
            "value_label": _format_pct(day_gain_pct, signed=True),
            "threshold": round(float(day_gain_trigger_pct or 0), 4),
            "threshold_label": _format_pct(day_gain_trigger_pct),
            **_event_time_fields(
                day_gain_ms,
                str(metric_row.get("day_gain_triggered_us_time") or ""),
                str(metric_row.get("day_gain_triggered_cn_time") or ""),
            ),
        }
        if day_gain_price > 0:
            event["details"] = {"price": round(day_gain_price, 4)}
        events.append(event)

    for timeframe, snapshot in (snapshots or {}).items():
        events.extend(
            build_indicator_trigger_events_from_snapshot(
                snapshot,
                timeframe=str(timeframe or ""),
                source="indicator_snapshot",
                precision="latest_snapshot",
            )
        )

    return _dedupe_timeline(events)


def add_sd_admission_event(extra: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = [dict(item) for item in extra.get("trigger_timeline") or [] if isinstance(item, dict)]
    admitted_ms = _safe_int(extra.get("sd_admitted_at_ms"))
    if admitted_ms > 0:
        label = "SD窗口通过"
        if _truthy(extra.get("sd_upper_valid")) and not _truthy(extra.get("sd_lower_valid")):
            label = "SD上轨窗口通过"
        elif _truthy(extra.get("sd_lower_valid")) and not _truthy(extra.get("sd_upper_valid")):
            label = "SD下轨窗口通过"
        timeline.append(
            {
                "key": "sd_admission",
                "label": label,
                "timeframe": "5m",
                "source": "active_window_admission",
                "precision": "first_admitted",
                **_event_time_fields(
                    admitted_ms,
                    str(extra.get("sd_admitted_us_time") or ""),
                    str(extra.get("sd_admitted_cn_time") or ""),
                ),
            }
        )
    return _dedupe_timeline(timeline)


def _event_short_text(event: dict[str, Any]) -> str:
    label = str(event.get("label") or event.get("key") or "").strip()
    us_time = str(event.get("us_time") or "").strip()
    date_text = us_time[:10] if len(us_time) >= 10 and us_time[4:5] == "-" and us_time[7:8] == "-" else ""
    time_text = us_time[11:16] if len(us_time) >= 16 else ""
    value_label = str(event.get("value_label") or "").strip()
    parts = [label]
    if time_text:
        parts.append(f"{date_text} {time_text} ET" if date_text else f"{time_text} ET")
    if value_label:
        parts.append(f"({value_label})")
    return " ".join(part for part in parts if part)


def build_active_reason_payload(
    *,
    symbol: str,
    status: str,
    direction_bias: str,
    score: float,
    active_min_score: float = 0.0,
    rank: int = 0,
    subscription_rank: int = 0,
    scan_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_extra = dict(extra or {})
    timeline = add_sd_admission_event(payload_extra)
    admission_score = _safe_float(payload_extra.get("admission_score"))
    thresholds = payload_extra.get("dynamic_thresholds") if isinstance(payload_extra.get("dynamic_thresholds"), dict) else {}
    admission_threshold = _safe_float((thresholds or {}).get("admission_score_gte"))
    long_votes = _safe_int(payload_extra.get("long_votes"))
    short_votes = _safe_int(payload_extra.get("short_votes"))

    summary_parts = [_event_short_text(item) for item in timeline[:3]]
    if active_min_score > 0:
        score_relation = ">=" if _safe_float(score) >= active_min_score else "<"
        summary_parts.append(f"score {_format_number(score, 1)}{score_relation}{_format_number(active_min_score, 1)}")
    if admission_score > 0:
        if admission_threshold > 0:
            summary_parts.append(f"admission {_format_number(admission_score, 1)}>={_format_number(admission_threshold, 1)}")
        else:
            summary_parts.append(f"admission {_format_number(admission_score, 1)}")
    if long_votes or short_votes:
        summary_parts.append(f"votes L{long_votes}:S{short_votes}")

    normalized_status = str(status or "").strip().lower()
    reason_count = len(timeline)
    summary = {
        "symbol": str(symbol or "").strip().upper(),
        "status": normalized_status,
        "active": normalized_status == "active",
        "direction_bias": str(direction_bias or "").strip().lower(),
        "score": round(_safe_float(score), 4),
        "active_min_score": round(_safe_float(active_min_score), 4),
        "rank": _safe_int(rank),
        "subscription_rank": _safe_int(subscription_rank),
        "reason_count": reason_count,
        "reason_keys": [str(item.get("key") or "") for item in timeline if str(item.get("key") or "")],
        "scan_reason": str(scan_reason or "").strip(),
        "primary_text": (
            f"入选 {normalized_status}: " + " · ".join(part for part in summary_parts if part)
            if summary_parts
            else str(scan_reason or "").strip()
        ),
    }
    if admission_score > 0:
        summary["admission_score"] = round(admission_score, 4)
    if admission_threshold > 0:
        summary["admission_score_threshold"] = round(admission_threshold, 4)
    if long_votes or short_votes:
        summary["long_votes"] = long_votes
        summary["short_votes"] = short_votes
    return {
        "trigger_timeline": timeline,
        "active_reason_summary": summary,
    }
