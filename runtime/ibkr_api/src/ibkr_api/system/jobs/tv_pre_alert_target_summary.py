from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.values import parse_boolean


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
TimeStrings = Callable[[], dict[str, str]]
EmitSystemEvent = Callable[..., dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]

ET = ZoneInfo("America/New_York")
SUMMARY_EVENT_TYPE = "tv_pre_alert_target_summary"
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_THRESHOLD_COUNT = 5
DEFAULT_TOP_SYMBOL_LIMIT = 12
EARLY_BUCKET_MINUTES = 5
PERIODIC_DUE_TOLERANCE_MS = 3 * 60 * 1000


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, float) and math.isnan(value):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if math.isnan(parsed):
            return float(default)
        return parsed
    except Exception:
        return float(default)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _escape(escape_filter_string: EscapeFilterString, value: Any) -> str:
    try:
        return _to_text(escape_filter_string(value))
    except Exception:
        return _to_text(value).replace("\\", "\\\\").replace('"', '\\"')


def _config_int(
    config_value: ConfigValue | None,
    key: str,
    default: int,
    environment: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        raw = config_value(key, str(default), environment) if callable(config_value) else default
    except Exception:
        raw = default
    value = max(int(minimum), _to_int(raw, default))
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _config_bool(config_value: ConfigValue | None, key: str, default: bool, environment: str) -> bool:
    try:
        raw = config_value(key, "TRUE" if default else "FALSE", environment) if callable(config_value) else default
    except Exception:
        raw = default
    return parse_boolean(raw, default)


def _parse_et_time_ms(value: Any) -> int:
    text = _to_text(value)
    if not text:
        return 0
    if text.isdigit():
        return _to_int(text, 0)
    normalized = text.replace("T", " ")
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return int(datetime.strptime(normalized[:length], fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def _now_from_times_ms(time_strings: TimeStrings) -> tuple[dict[str, str], int]:
    try:
        times = dict(time_strings() or {})
    except Exception:
        times = {}
    now_ms = _parse_et_time_ms(times.get("us"))
    if now_ms <= 0:
        now_ms = int(datetime.now(ET).timestamp() * 1000)
    return times, now_ms


def _floor_time_ms(epoch_ms: int, minutes: int) -> int:
    interval_ms = max(1, int(minutes or 1)) * 60 * 1000
    return int(epoch_ms // interval_ms * interval_ms)


def _format_time(epoch_ms: int) -> str:
    if epoch_ms <= 0:
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000.0, ET).strftime("%H:%M")


def _format_window(epoch_start_ms: int, epoch_end_ms: int) -> str:
    if epoch_start_ms <= 0 or epoch_end_ms <= 0:
        return "n/a"
    start_dt = datetime.fromtimestamp(epoch_start_ms / 1000.0, ET)
    end_dt = datetime.fromtimestamp(epoch_end_ms / 1000.0, ET)
    if start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%Y-%m-%d %H:%M')}-{end_dt.strftime('%H:%M')} ET"
    return f"{start_dt.strftime('%Y-%m-%d %H:%M')}-{end_dt.strftime('%Y-%m-%d %H:%M')} ET"


def _load_records(pb: Any, collection: str, *, filter: str, sort: str = "-updated", max_pages: int = 10) -> list[dict[str, Any]]:
    try:
        getter = getattr(pb, "get_all_records", None)
        if callable(getter):
            rows = getter(collection, filter=filter, sort=sort, max_pages=max_pages)
        else:
            rows = pb.get_records(collection, filter=filter, sort=sort, per_page=500, page=1)
    except Exception:
        rows = []
    return [dict(row) for row in (rows or []) if isinstance(row, dict)]


def _is_tv_pre_alert_target(row: dict[str, Any]) -> bool:
    extra = _as_dict(row.get("extra"))
    source = _to_text(extra.get("source")).lower()
    event_type = _to_text(extra.get("event_type")).lower()
    has_tv_activation = bool(_to_text(extra.get("first_tv_event_id")) or _to_text(extra.get("last_tv_event_id")))
    return source in {"tradingview", "tv"} and (event_type == "pre_alert" or has_tv_activation)


def _target_times(row: dict[str, Any]) -> tuple[int, int]:
    extra = _as_dict(row.get("extra"))
    row_bar_ms = _to_int(row.get("bar_time_ms"), 0)
    first_ms = _to_int(extra.get("first_bar_time_ms"), 0) or row_bar_ms
    last_ms = _to_int(extra.get("last_bar_time_ms"), 0) or row_bar_ms or first_ms
    return first_ms, last_ms


def _in_window(value_ms: int, start_ms: int, end_ms: int) -> bool:
    return value_ms > 0 and start_ms <= value_ms < end_ms


def _direction_value(row: dict[str, Any]) -> str:
    extra = _as_dict(row.get("extra"))
    direction = _to_text(row.get("direction_bias") or extra.get("direction_bias")).lower()
    return direction if direction in {"long", "short", "neutral"} else "unknown"


def _mtf_status_value(row: dict[str, Any]) -> str:
    extra = _as_dict(row.get("extra"))
    status = _to_text(extra.get("mtf_last_status") or extra.get("mtf_status")).lower()
    if status in {"pass", "warn", "block"}:
        return status
    return "unknown"


def _status_value(row: dict[str, Any]) -> str:
    status = _to_text(row.get("status")).lower()
    return status if status in {"active", "candidate"} else (status or "unknown")


def _summary_item(row: dict[str, Any], *, change_kind: str) -> dict[str, Any]:
    extra = _as_dict(row.get("extra"))
    rank = _to_int(extra.get("activity_rank"), 0)
    score = _to_float(row.get("score"), _to_float(extra.get("activity_score"), 0.0))
    first_ms, last_ms = _target_times(row)
    mtf_status = _mtf_status_value(row)
    return {
        "symbol": _to_text(row.get("symbol")).upper(),
        "change_kind": change_kind,
        "status": _status_value(row),
        "direction": _direction_value(row),
        "mtf_status": mtf_status,
        "mtf_score": _to_float(extra.get("mtf_last_score") or extra.get("mtf_score"), 0.0),
        "mtf_block_reason": _to_text(extra.get("mtf_last_block_reason") or extra.get("mtf_block_reason")),
        "activity_rank": rank,
        "score": score,
        "first_bar_time_ms": first_ms,
        "last_bar_time_ms": last_ms,
        "first_time": _format_time(first_ms),
        "last_time": _format_time(last_ms),
    }


def _rank_item_key(item: dict[str, Any]) -> tuple[int, int, float, str]:
    rank = _to_int(item.get("activity_rank"), 999999)
    if rank <= 0:
        rank = 999999
    new_first = 0 if _to_text(item.get("change_kind")) == "new" else 1
    return (new_first, rank, -_to_float(item.get("score"), 0.0), _to_text(item.get("symbol")))


def _label_item(item: dict[str, Any]) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    kind = "新增" if _to_text(item.get("change_kind")) == "new" else "更新"
    direction = {"long": "多", "short": "空", "neutral": "中性"}.get(_to_text(item.get("direction")), "方向?")
    status = _to_text(item.get("status")) or "unknown"
    mtf = _to_text(item.get("mtf_status")) or "unknown"
    rank = _to_int(item.get("activity_rank"), 0)
    rank_part = f",#{rank}" if rank > 0 else ""
    return f"{symbol}({kind},{direction},{status},MTF {mtf}{rank_part})"


def _count_by(items: list[dict[str, Any]], key: str, allowed: tuple[str, ...]) -> dict[str, int]:
    counts = {item: 0 for item in allowed}
    for item in items:
        value = _to_text(item.get(key)).lower() or "unknown"
        if value not in counts:
            counts[value] = 0
        counts[value] += 1
    return counts


def _counts_line(counts: dict[str, int], order: tuple[str, ...]) -> str:
    parts = []
    for key in order:
        parts.append(f"{key}:{_to_int(counts.get(key), 0)}")
    extras = sorted(key for key in counts if key not in set(order) and _to_int(counts.get(key), 0) > 0)
    parts.extend(f"{key}:{_to_int(counts.get(key), 0)}" for key in extras)
    return " | ".join(parts)


def _join_limited(values: list[str], *, limit: int = DEFAULT_TOP_SYMBOL_LIMIT) -> str:
    clean = [value for value in values if value]
    if not clean:
        return "none"
    shown = clean[: max(1, limit)]
    suffix = f" | +{len(clean) - len(shown)}" if len(clean) > len(shown) else ""
    return " | ".join(shown) + suffix


def _build_target_summary(rows: list[dict[str, Any]], *, start_ms: int, end_ms: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    scanned = 0
    for row in rows:
        if not _is_tv_pre_alert_target(row):
            continue
        scanned += 1
        first_ms, last_ms = _target_times(row)
        first_in_window = _in_window(first_ms, start_ms, end_ms)
        last_in_window = _in_window(last_ms, start_ms, end_ms)
        if first_in_window:
            items.append(_summary_item(row, change_kind="new"))
        elif last_in_window:
            items.append(_summary_item(row, change_kind="updated"))
    items = [item for item in items if _to_text(item.get("symbol"))]
    items.sort(key=_rank_item_key)
    new_count = sum(1 for item in items if _to_text(item.get("change_kind")) == "new")
    updated_count = sum(1 for item in items if _to_text(item.get("change_kind")) == "updated")
    status_counts = _count_by(items, "status", ("active", "candidate"))
    direction_counts = _count_by(items, "direction", ("long", "short", "neutral", "unknown"))
    mtf_counts = _count_by(items, "mtf_status", ("pass", "warn", "block", "unknown"))
    return {
        "scanned_tv_targets": scanned,
        "new_count": new_count,
        "updated_count": updated_count,
        "total_count": new_count + updated_count,
        "status_counts": status_counts,
        "direction_counts": direction_counts,
        "mtf_counts": mtf_counts,
        "items": items,
    }


def _summary_event_matches(
    row: dict[str, Any],
    *,
    data_environment: str,
    market_date: str,
) -> bool:
    detail = _as_dict(row.get("detail"))
    row_data_environment = _to_text(
        detail.get("data_environment") or detail.get("market_data_mode") or detail.get("target_environment")
    ).lower()
    row_market_date = _to_text(detail.get("market_date") or detail.get("日期"))
    if row_data_environment and row_data_environment != data_environment:
        return False
    if row_market_date and row_market_date != market_date:
        return False
    return True


def _load_summary_events(
    pb: Any,
    *,
    broker_mode: str,
    data_environment: str,
    market_date: str,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    filter_expr = (
        f'event_type = "{_escape(escape_filter_string, SUMMARY_EVENT_TYPE)}" && '
        f'environment = "{_escape(escape_filter_string, broker_mode)}"'
    )
    rows = _load_records(pb, "system_events", filter=filter_expr, sort="-created", max_pages=10)
    return [
        row
        for row in rows
        if _summary_event_matches(row, data_environment=data_environment, market_date=market_date)
    ]


def _latest_summary_end_ms(events: list[dict[str, Any]]) -> int:
    latest = 0
    for row in events:
        detail = _as_dict(row.get("detail"))
        latest = max(latest, _to_int(detail.get("window_end_ms"), 0))
    return latest


def _has_duplicate_window(events: list[dict[str, Any]], window_key: str) -> bool:
    for row in events:
        detail = _as_dict(row.get("detail"))
        if _to_text(detail.get("summary_window_key")) == window_key:
            return True
    return False


def _window_bounds(
    payload: dict[str, Any],
    *,
    now_ms: int,
    window_minutes: int,
    last_summary_end_ms: int,
) -> tuple[int, int, bool, bool]:
    explicit_start = _to_int(payload.get("window_start_ms") or payload.get("start_ms"), 0)
    explicit_end = _to_int(payload.get("window_end_ms") or payload.get("end_ms"), 0)
    explicit = bool(explicit_start or explicit_end)
    periodic_end_ms = _floor_time_ms(now_ms, window_minutes)
    periodic_due = explicit or (now_ms - periodic_end_ms <= PERIODIC_DUE_TOLERANCE_MS)
    if explicit_end > 0:
        end_ms = explicit_end
    elif periodic_due:
        end_ms = periodic_end_ms
    else:
        end_ms = _floor_time_ms(now_ms, EARLY_BUCKET_MINUTES)
    if end_ms <= 0:
        end_ms = now_ms
    default_start = end_ms - max(1, window_minutes) * 60 * 1000
    if explicit_start > 0:
        start_ms = explicit_start
    elif last_summary_end_ms > 0 and last_summary_end_ms < end_ms:
        start_ms = max(default_start, last_summary_end_ms)
    else:
        start_ms = default_start
    return start_ms, end_ms, periodic_due, explicit


def _event_detail(
    *,
    market_date: str,
    broker_mode: str,
    data_environment: str,
    start_ms: int,
    end_ms: int,
    window_key: str,
    summary: dict[str, Any],
    trigger_kind: str,
    threshold_count: int,
    top_symbol_limit: int,
) -> dict[str, Any]:
    items = [dict(item) for item in summary.get("items") or [] if isinstance(item, dict)]
    status_counts = _as_dict(summary.get("status_counts"))
    direction_counts = _as_dict(summary.get("direction_counts"))
    mtf_counts = _as_dict(summary.get("mtf_counts"))
    return {
        "窗口": _format_window(start_ms, end_ms),
        "新增入池": _to_int(summary.get("new_count"), 0),
        "更新": _to_int(summary.get("updated_count"), 0),
        "Active/Candidate": _counts_line(status_counts, ("active", "candidate")),
        "方向": _counts_line(direction_counts, ("long", "short", "neutral", "unknown")),
        "MTF": _counts_line(mtf_counts, ("pass", "warn", "block", "unknown")),
        "Top symbols": _join_limited([_label_item(item) for item in items], limit=top_symbol_limit),
        "触发方式": trigger_kind,
        "阈值": threshold_count,
        "Broker/Data": f"{broker_mode}/{data_environment}",
        "market_date": market_date,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "dedupe_scope": "symbol+date+market_data_mode; new uses first_bar_time_ms, update uses last_bar_time_ms",
        "summary_window_key": window_key,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
    }


def build_tv_pre_alert_target_summary_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    emit_system_event: EmitSystemEvent,
    config_value: ConfigValue | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = dict(payload or {}) if isinstance(payload, dict) else {}
    broker_mode = normalize_environment(request_broker_mode(request_payload), "paper")
    data_environment = request_market_data_mode(request_payload)
    times, now_ms = _now_from_times_ms(time_strings)
    market_date = _to_text(request_payload.get("market_date") or request_payload.get("date") or times.get("date"))
    if not market_date:
        market_date = datetime.fromtimestamp(now_ms / 1000.0, ET).strftime("%Y-%m-%d")

    window_minutes = _to_int(
        request_payload.get("window_minutes") or request_payload.get("window_min"),
        _config_int(
            config_value,
            "tv_pre_alert_target_summary_window_min",
            DEFAULT_WINDOW_MINUTES,
            data_environment,
            minimum=1,
            maximum=240,
        ),
    )
    window_minutes = max(1, min(240, window_minutes))
    threshold_count = _to_int(
        request_payload.get("threshold_count") or request_payload.get("min_count"),
        _config_int(
            config_value,
            "tv_pre_alert_target_summary_threshold_count",
            DEFAULT_THRESHOLD_COUNT,
            data_environment,
            minimum=0,
            maximum=500,
        ),
    )
    top_symbol_limit = _to_int(
        request_payload.get("top_symbol_limit"),
        _config_int(
            config_value,
            "tv_pre_alert_target_summary_top_symbol_limit",
            DEFAULT_TOP_SYMBOL_LIMIT,
            data_environment,
            minimum=1,
            maximum=50,
        ),
    )
    top_symbol_limit = max(1, min(50, top_symbol_limit))
    force = parse_boolean(request_payload.get("force"), False)
    dry_run = parse_boolean(request_payload.get("dry_run"), False)
    threshold_enabled = _config_bool(
        config_value,
        "tv_pre_alert_target_summary_threshold_enabled",
        True,
        data_environment,
    )

    previous_events = _load_summary_events(
        pb,
        broker_mode=broker_mode,
        data_environment=data_environment,
        market_date=market_date,
        escape_filter_string=escape_filter_string,
    )
    start_ms, end_ms, periodic_due, explicit_window = _window_bounds(
        request_payload,
        now_ms=now_ms,
        window_minutes=window_minutes,
        last_summary_end_ms=_latest_summary_end_ms(previous_events),
    )
    if start_ms >= end_ms:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_new_summary_window",
            "job_id": SUMMARY_EVENT_TYPE,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "source": "ibkr-api",
        }, 200

    window_key = f"{market_date}:{broker_mode}:{data_environment}:{start_ms}:{end_ms}"
    if not force and _has_duplicate_window(previous_events, window_key):
        return {
            "ok": True,
            "skipped": True,
            "reason": "duplicate_window",
            "job_id": SUMMARY_EVENT_TYPE,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "window": _format_window(start_ms, end_ms),
            "summary_window_key": window_key,
            "source": "ibkr-api",
        }, 200

    filter_expr = (
        f'date = "{_escape(escape_filter_string, market_date)}" && '
        f'environment = "{_escape(escape_filter_string, data_environment)}"'
    )
    rows = _load_records(pb, "ibkr_targets", filter=filter_expr, sort="-score,-updated", max_pages=20)
    summary = _build_target_summary(rows, start_ms=start_ms, end_ms=end_ms)
    total_count = _to_int(summary.get("total_count"), 0)

    if total_count <= 0 and not force:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_tv_pre_alert_targets",
            "job_id": SUMMARY_EVENT_TYPE,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "window": _format_window(start_ms, end_ms),
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "scanned_tv_targets": _to_int(summary.get("scanned_tv_targets"), 0),
            "source": "ibkr-api",
        }, 200

    threshold_hit = bool(threshold_enabled and threshold_count > 0 and total_count >= threshold_count)
    if not force and not explicit_window and not periodic_due and not threshold_hit:
        return {
            "ok": True,
            "skipped": True,
            "reason": "waiting_for_periodic_or_threshold",
            "job_id": SUMMARY_EVENT_TYPE,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "window": _format_window(start_ms, end_ms),
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "total_count": total_count,
            "threshold_count": threshold_count,
            "periodic_due": periodic_due,
            "source": "ibkr-api",
        }, 200

    trigger_kind = "forced" if force else ("periodic" if periodic_due or explicit_window else "threshold")
    detail = _event_detail(
        market_date=market_date,
        broker_mode=broker_mode,
        data_environment=data_environment,
        start_ms=start_ms,
        end_ms=end_ms,
        window_key=window_key,
        summary=summary,
        trigger_kind=trigger_kind,
        threshold_count=threshold_count,
        top_symbol_limit=top_symbol_limit,
    )
    title = f"TV pre_alert 入池汇总 {_format_time(start_ms)}-{_format_time(end_ms)} ET"
    event = {}
    if not dry_run:
        event = emit_system_event(
            event_type=SUMMARY_EVENT_TYPE,
            level="info",
            source="ibkr_api",
            title=title,
            detail=detail,
            environment=broker_mode,
        )

    return {
        "ok": True,
        "skipped": False,
        "dry_run": dry_run,
        "reason": trigger_kind,
        "job_id": SUMMARY_EVENT_TYPE,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "market_date": market_date,
        "window": _format_window(start_ms, end_ms),
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "summary_window_key": window_key,
        "periodic_due": periodic_due,
        "threshold_hit": threshold_hit,
        "threshold_count": threshold_count,
        "new_count": _to_int(summary.get("new_count"), 0),
        "updated_count": _to_int(summary.get("updated_count"), 0),
        "total_count": total_count,
        "status_counts": _as_dict(summary.get("status_counts")),
        "direction_counts": _as_dict(summary.get("direction_counts")),
        "mtf_counts": _as_dict(summary.get("mtf_counts")),
        "items": summary.get("items") or [],
        "event": event,
        "source": "ibkr-api",
    }, 200


__all__ = [
    "SUMMARY_EVENT_TYPE",
    "build_tv_pre_alert_target_summary_response",
]
