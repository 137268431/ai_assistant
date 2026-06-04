from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable


TV_WEBHOOK_EVENTS_COLLECTION = "tv_webhook_events"

LATENCY_STAGES = [
    {
        "key": "bar_close_to_pine_eval_ms",
        "label": "Bar收盘→Pine执行",
        "start_fields": ("bar_close_ms", "time_close_ms", "time_close"),
        "end_fields": ("pine_eval_ms", "timenow"),
    },
    {
        "key": "pine_eval_to_api_received_ms",
        "label": "Pine执行→API收到",
        "start_fields": ("pine_eval_ms", "timenow"),
        "end_fields": ("api_received_at_ms",),
    },
    {
        "key": "api_received_to_pb_created_ms",
        "label": "API收到→PB入库",
        "start_fields": ("api_received_at_ms",),
        "end_fields": ("pb_created_at_ms", "created"),
    },
    {
        "key": "pb_created_to_route_finished_ms",
        "label": "PB入库→路由完成",
        "start_fields": ("pb_created_at_ms", "created"),
        "end_fields": ("route_finished_at_ms", "updated"),
    },
]


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _extra(row: dict[str, Any] | None) -> dict[str, Any]:
    return _as_object((row or {}).get("extra"))


def _latency_trace(row: dict[str, Any] | None) -> dict[str, Any]:
    source = row or {}
    trace = source.get("latency_trace") or _extra(source).get("latency_trace")
    return _as_object(trace)


def _to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _timestamp_ms(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return 0
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            return int(numeric)
        if numeric > 1_000_000_000:
            return int(numeric * 1000)
        return 0
    text = _text(value)
    if not text:
        return 0
    try:
        numeric = float(text)
        if numeric > 1_000_000_000_000:
            return int(numeric)
        if numeric > 1_000_000_000:
            return int(numeric * 1000)
    except Exception:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def _first_timestamp(row: dict[str, Any], trace: dict[str, Any], fields: tuple[str, ...]) -> int:
    extra = _extra(row)
    for field in fields:
        for source in (trace, row, extra):
            parsed = _timestamp_ms(source.get(field)) if isinstance(source, dict) else 0
            if parsed > 0:
                return parsed
    return 0


def _duration_ms(start_ms: int, end_ms: int) -> int | None:
    if start_ms <= 0 or end_ms <= 0 or end_ms < start_ms:
        return None
    value = end_ms - start_ms
    return value if value > 0 else None


def _stage_duration(row: dict[str, Any], stage: dict[str, Any]) -> int | None:
    trace = _latency_trace(row)
    direct_value = _to_int(trace.get(stage["key"]), 0)
    if direct_value > 0:
        return direct_value
    start_ms = _first_timestamp(row, trace, tuple(stage["start_fields"]))
    end_ms = _first_timestamp(row, trace, tuple(stage["end_fields"]))
    return _duration_ms(start_ms, end_ms)


def _date_token(value: Any, fallback: str) -> str:
    text = _text(value) or fallback
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _next_date_token(date_token: str) -> str:
    return (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _resolve_date_range(request_params: dict[str, Any], fallback_date: str) -> tuple[str, str, str | None]:
    has_range = bool(_text(request_params.get("start_date")) or _text(request_params.get("end_date")))
    if has_range:
        start_date = _date_token(
            request_params.get("start_date") or request_params.get("date") or request_params.get("market_date"),
            fallback_date,
        )
        end_date = _date_token(
            request_params.get("end_date")
            or request_params.get("start_date")
            or request_params.get("date")
            or request_params.get("market_date")
            or start_date,
            start_date,
        )
    else:
        start_date = _date_token(request_params.get("date") or request_params.get("market_date"), fallback_date)
        end_date = start_date
    if end_date < start_date:
        return start_date, end_date, "invalid_date_range"
    return start_date, end_date, None


def _load_records(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    sort: str = "-created",
    per_page: int = 200,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    safe_per_page = max(1, min(int(per_page or 200), 200))
    for page in range(1, max(1, int(max_pages or 1)) + 1):
        batch = pb.get_records(collection, filter=filter_expr, sort=sort, per_page=safe_per_page, page=page) or []
        page_rows = [dict(row) for row in batch if isinstance(row, dict)]
        rows.extend(page_rows)
        if len(page_rows) < safe_per_page:
            break
    return rows


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    if percentile == 50:
        return round(float(median(values)))
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((percentile / 100.0) * len(ordered)) - 1))
    return int(ordered[index])


def _stage_summary(stage: dict[str, Any], values: list[int], total_events: int) -> dict[str, Any]:
    return {
        "key": stage["key"],
        "label": stage["label"],
        "count": len(values),
        "missing": max(0, total_events - len(values)),
        "avg_ms": _avg(values),
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "max_ms": max(values) if values else None,
    }


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: (_timestamp_ms(row.get("created")), _text(row.get("created"))))


def _latest_event_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    stage_values = {stage["key"]: _stage_duration(row, stage) for stage in LATENCY_STAGES}
    known_values = [value for value in stage_values.values() if isinstance(value, int) and value > 0]
    return {
        "id": row.get("id"),
        "event_id": row.get("event_id"),
        "symbol": row.get("symbol"),
        "date": row.get("date"),
        "status": row.get("status"),
        "created": row.get("created"),
        "updated": row.get("updated"),
        "complete": len(known_values) == len(LATENCY_STAGES),
        "total_ms": sum(known_values) if len(known_values) == len(LATENCY_STAGES) else None,
        "stages": stage_values,
    }


def build_signal_latency_analytics_response(
    pb: Any,
    *,
    params: dict[str, Any] | None = None,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    time_strings: Callable[[], dict[str, str]],
) -> tuple[dict[str, Any], int]:
    request_params = params if isinstance(params, dict) else {}
    fallback_date = _text((time_strings() or {}).get("date")) or datetime.utcnow().strftime("%Y-%m-%d")
    start_date, end_date, date_error = _resolve_date_range(request_params, fallback_date)
    if date_error:
        return {
            "ok": False,
            "source": "ibkr-api",
            "error": date_error,
            "message": "end_date must be on or after start_date",
            "start_date": start_date,
            "end_date": end_date,
        }, 400

    single_day = start_date == end_date
    data_environment = normalize_environment(
        request_params.get("data_environment") or request_params.get("market_data_mode") or request_params.get("environment"),
        "live",
    )
    data_escaped = escape_filter_string(data_environment)
    start_date_escaped = escape_filter_string(start_date)
    end_date_escaped = escape_filter_string(end_date)
    if single_day:
        event_filter = (
            f'environment = "{data_escaped}" && event_type = "entry" && date = "{start_date_escaped}"'
        )
    else:
        event_filter = (
            f'environment = "{data_escaped}" && event_type = "entry" && '
            f'date >= "{start_date_escaped}" && date <= "{end_date_escaped}"'
        )

    rows = _load_records(pb, TV_WEBHOOK_EVENTS_COLLECTION, filter_expr=event_filter, sort="-created", max_pages=120)
    total_events = len(rows)
    values_by_stage = {stage["key"]: [] for stage in LATENCY_STAGES}
    complete_events = 0
    missing_trace_count = 0

    for row in rows:
        if not _latency_trace(row):
            missing_trace_count += 1
        row_complete = True
        for stage in LATENCY_STAGES:
            value = _stage_duration(row, stage)
            if isinstance(value, int) and value > 0:
                values_by_stage[stage["key"]].append(value)
            else:
                row_complete = False
        if row_complete:
            complete_events += 1

    stage_rows = [_stage_summary(stage, values_by_stage[stage["key"]], total_events) for stage in LATENCY_STAGES]
    slowest_stage = max(
        (row for row in stage_rows if row["p95_ms"] is not None),
        key=lambda row: int(row["p95_ms"] or 0),
        default=None,
    )
    latest_event = _latest_event_payload(_latest_row(rows))
    payload = {
        "ok": True,
        "source": "ibkr-api",
        "date": start_date,
        "start_date": start_date,
        "end_date": end_date,
        "range_label": start_date if single_day else f"{start_date} ~ {end_date}",
        "data_environment": data_environment,
        "event_filter": event_filter,
        "total_events": total_events,
        "complete_events": complete_events,
        "missing_trace_count": missing_trace_count,
        "stage_rows": stage_rows,
        "slowest_stage": slowest_stage,
        "latest_event": latest_event,
        "summary": {
            "total_events": total_events,
            "complete_events": complete_events,
            "missing_trace_count": missing_trace_count,
            "slowest_stage": slowest_stage,
        },
    }
    return payload, 200


__all__ = ["build_signal_latency_analytics_response"]
