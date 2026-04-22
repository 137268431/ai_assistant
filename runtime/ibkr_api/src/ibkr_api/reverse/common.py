from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import escape_filter_string, parse_boolean, parse_integer, to_float, to_int, to_text


LIVE_ENVIRONMENT = "live"
REVERSE_SIGNALS_COLLECTION = "ibkr_reverse_signals"
INDICATORS_COLLECTION = "ibkr_indicators"
ORDERS_COLLECTION = "orders"
DEFAULT_REVERSE_LIST_LIMIT = 200
MAX_REVERSE_LIST_LIMIT = 500
DEFAULT_REVERSE_PRIORITY = 5
DEFAULT_REVERSE_THRESHOLD = 6
ACTIVE_ENTRY_ORDER_LIMIT = 20
REVERSE_DEDUPE_LOOKBACK_LIMIT = 50
ALLOWED_REVERSE_ACTION_TYPES = ("cancel", "close", "adjust_sl", "adjust_tp")


def normalize_environment_value(value: Any, default: str = LIVE_ENVIRONMENT) -> str:
    normalized = to_text(value).lower()
    return normalized or default


def ensure_object(value: Any) -> dict[str, Any]:
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


def record_value(record_or_data: Any, field_name: str, default: Any = None) -> Any:
    if record_or_data is None:
        return default
    if isinstance(record_or_data, dict):
        return record_or_data.get(field_name, default)
    getter = getattr(record_or_data, "get", None)
    if callable(getter):
        value = getter(field_name)
        return default if value is None else value
    return getattr(record_or_data, field_name, default)


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def get_reverse_extra(record_or_data: Any) -> dict[str, Any]:
    return ensure_object(record_value(record_or_data, "extra"))


def merge_reverse_extra(record_or_data: Any, patch: dict[str, Any] | None) -> dict[str, Any]:
    return {
        **get_reverse_extra(record_or_data),
        **ensure_object(patch),
    }


def normalize_status_filters(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    normalized: list[str] = []
    for item in items:
        status = to_text(item)
        if status:
            normalized.append(status)
    return normalized


def parse_triggered_signals(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [to_text(item) for item in parsed if to_text(item)]
        return [to_text(item) for item in text.split(",") if to_text(item)]
    return []


def clamp_reverse_limit(value: Any, default: int = DEFAULT_REVERSE_LIST_LIMIT) -> int:
    return parse_integer(value, default=default, minimum=1, maximum=MAX_REVERSE_LIST_LIMIT)


def build_date_range(date_text: Any) -> dict[str, int] | None:
    text = to_text(date_text)
    if not text:
        return None
    try:
        start = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = start.replace(hour=23, minute=59, second=59, microsecond=999000)
    return {
        "start_ms": int(start.timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
    }


def resolve_timestamp_text(clock: Callable[[], Any] | None = None) -> str:
    if callable(clock):
        value = clock()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return to_text(value)
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_reverse_record(
    pb: Any,
    reverse_id: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> Any:
    return pb.get_first_record(
        REVERSE_SIGNALS_COLLECTION,
        filter=f'id = "{escape_filter(reverse_id)}"',
    )


def merge_record_patch(record_or_data: Any, patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record_or_data) if isinstance(record_or_data, dict) else {}
    if not merged and record_or_data is not None:
        for field in (
            "id",
            "symbol",
            "direction",
            "strength",
            "score",
            "triggered_signals",
            "action_type",
            "status",
            "reason",
            "extra",
            "processed_time",
            "bar_time_ms",
            "us_time",
            "cn_time",
            "source",
            "priority",
            "environment",
            "created",
            "updated",
        ):
            value = record_value(record_or_data, field)
            if value is not None:
                merged[field] = value
    merged.update(patch)
    return merged


def _to_number(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return fallback if parsed is None else parsed


def _is_truthy(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return parse_boolean(value, False)


def _infer_reverse_kind(source: str, extra: dict[str, Any]) -> str:
    return to_text(extra.get("reverse_kind")) or ("signal_conflict" if source == "signal" else "indicator_conflict")


def _infer_target_state(extra: dict[str, Any], order_status: str, action_type: str) -> str:
    explicit = to_text(extra.get("target_state"))
    if explicit:
        return explicit
    if order_status == "Filled":
        return "filled_position"
    if order_status == "Submitted":
        return "pending_entry"
    if action_type in {"close", "adjust_sl", "adjust_tp"}:
        return "filled_position"
    if action_type == "cancel":
        return "pending_entry"
    return ""


def _normalize_triggered_signals(value: Any, extra: dict[str, Any]) -> list[str]:
    candidate = value if value is not None else extra.get("triggered_signals")
    return parse_triggered_signals(candidate)


def map_strength(score: Any) -> str:
    numeric_score = _to_number(score, 0)
    if numeric_score >= 6:
        return "strong"
    if numeric_score >= 3:
        return "medium"
    return "weak"


def resolve_indicator_action(score: Any, target_state: Any, forced_action_type: Any = "") -> str:
    forced = to_text(forced_action_type)
    if forced:
        return forced
    normalized_target_state = to_text(target_state)
    numeric_score = _to_number(score, 0)
    if normalized_target_state == "pending_entry":
        return "cancel"
    if numeric_score >= 6:
        return "close"
    if numeric_score >= 3:
        return "adjust_sl"
    return "cancel"


def load_latest_indicator_record(
    pb: Any,
    symbol: str,
    environment: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> Any:
    normalized_symbol = to_text(symbol).upper()
    runtime_environment = normalize_environment_value(environment)
    if not normalized_symbol:
        return None
    return pb.get_first_record(
        INDICATORS_COLLECTION,
        filter=(
            f'(environment = "{escape_filter(runtime_environment)}" || environment = "") && '
            f'symbol = "{escape_filter(normalized_symbol)}"'
        ),
        sort="-bar_time_ms",
    )


def build_indicator_analysis(direction: Any, indicator_record: Any) -> dict[str, Any]:
    extra = ensure_object(record_value(indicator_record, "extra"))
    normalized_direction = to_text(direction).lower()
    crsi = _to_number(extra.get("crsi"), 0)
    obv_rsi = _to_number(extra.get("obv_rsi"), 0)
    vwap_dist = to_float(extra.get("vwap_dist"))
    close = _to_number(extra.get("close"), 0)

    score = 0
    triggered_signals: list[str] = []

    if normalized_direction == "long" and crsi > 70:
        score += 2
        triggered_signals.append("cRSI超买")
    elif normalized_direction == "short" and crsi < 30:
        score += 2
        triggered_signals.append("cRSI超卖")

    is_bear_signal = normalized_direction == "long"
    crsi_reverse_div = extra.get("crsi_bear_div") if is_bear_signal else extra.get("crsi_bull_div")
    obv_reverse_div = extra.get("obv_bear_div") if is_bear_signal else extra.get("obv_bull_div")
    fractal_reverse = extra.get("fractal_bear") if is_bear_signal else extra.get("fractal_bull")
    sd_channel_reverse = extra.get("sd_upper") if is_bear_signal else extra.get("sd_lower")
    ema_touch_reverse = extra.get("ema_bear_touch") if is_bear_signal else extra.get("ema_bull_touch")

    if _is_truthy(crsi_reverse_div) or _is_truthy(obv_reverse_div):
        score += 3
        triggered_signals.append("背离")

    if _is_truthy(fractal_reverse) or _is_truthy(sd_channel_reverse):
        score += 2
        triggered_signals.append("分形/SD通道")

    if (vwap_dist is not None and abs(vwap_dist) > 2) or _is_truthy(ema_touch_reverse):
        score += 1
        triggered_signals.append("VWAP偏离/EMA触碰")

    return {
        "score": score,
        "triggered_signals": triggered_signals,
        "indicator_extra": extra,
        "crsi": crsi,
        "obv_rsi": obv_rsi,
        "vwap_dist": vwap_dist if vwap_dist is not None else 0,
        "close": close,
    }


def build_order_context(order_record: Any, *, default_environment: str = LIVE_ENVIRONMENT) -> dict[str, Any] | None:
    if not order_record:
        return None
    order_extra = ensure_object(record_value(order_record, "extra"))
    order_status = to_text(record_value(order_record, "status") or order_extra.get("status"))
    direction = to_text(first_non_empty(record_value(order_record, "direction"), order_extra.get("direction"), ""))
    trade_group_id = to_text(
        first_non_empty(
            record_value(order_record, "trade_group_id"),
            order_extra.get("trade_group_id"),
            record_value(order_record, "entry_order_unique_id"),
            record_value(order_record, "unique_id"),
        )
    )
    entry_order_unique_id = to_text(
        first_non_empty(
            record_value(order_record, "entry_order_unique_id"),
            order_extra.get("entry_order_unique_id"),
            record_value(order_record, "unique_id"),
        )
    )
    unique_id = to_text(
        first_non_empty(
            record_value(order_record, "unique_id"),
            order_extra.get("order_unique_id"),
            entry_order_unique_id,
            trade_group_id,
            "",
        )
    )
    broker_order_id = to_text(
        first_non_empty(
            record_value(order_record, "broker_order_id"),
            record_value(order_record, "order_id"),
            order_extra.get("broker_order_id"),
            order_extra.get("order_id"),
            "",
        )
    )

    return {
        "environment": normalize_environment_value(
            record_value(order_record, "environment") or order_extra.get("environment") or default_environment,
            default_environment,
        ),
        "symbol": to_text(record_value(order_record, "symbol") or order_extra.get("symbol")),
        "direction": direction,
        "target_state": "filled_position" if order_status == "Filled" else "pending_entry",
        "order_status": order_status,
        "relation_status": to_text(first_non_empty(record_value(order_record, "relation_status"), order_extra.get("relation_status"), "")),
        "position_side": to_text(first_non_empty(record_value(order_record, "position_side"), order_extra.get("position_side"), direction, "")),
        "signal_id": to_text(first_non_empty(record_value(order_record, "signal_id"), order_extra.get("signal_id"), "")),
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "order_unique_id": unique_id,
        "broker_order_id": broker_order_id,
        "entry_price": _to_number(
            first_non_empty(
                record_value(order_record, "fill_price"),
                record_value(order_record, "limit_price"),
                order_extra.get("fill_price"),
                order_extra.get("limit_price"),
                0,
            ),
            0,
        ),
        "quantity": _to_number(
            first_non_empty(
                record_value(order_record, "filled_qty"),
                record_value(order_record, "quantity"),
                order_extra.get("filled_qty"),
                order_extra.get("quantity"),
                0,
            ),
            0,
        ),
        "take_profit": _to_number(first_non_empty(record_value(order_record, "tp_price"), order_extra.get("tp_price"), 0), 0),
        "stop_loss": _to_number(first_non_empty(record_value(order_record, "sl_price"), order_extra.get("sl_price"), 0), 0),
        "order_record": order_record,
    }


def find_latest_active_entry_order(
    pb: Any,
    symbol: str,
    direction: str,
    environment: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    per_page: int = ACTIVE_ENTRY_ORDER_LIMIT,
) -> Any:
    normalized_symbol = to_text(symbol).upper()
    normalized_direction = to_text(direction).lower()
    runtime_environment = normalize_environment_value(environment)
    records = list(
        pb.get_records(
            ORDERS_COLLECTION,
            filter=(
                f'symbol = "{escape_filter(normalized_symbol)}" && '
                f'environment = "{escape_filter(runtime_environment)}" && '
                'order_type = "Entry" && '
                '(status = "Submitted" || status = "Filled")'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )
    if not records:
        return None
    if not normalized_direction:
        return records[0]
    for record in records:
        if to_text(record_value(record, "direction")).lower() == normalized_direction:
            return record
    return records[0]


def build_reverse_duplicate_criteria(payload: dict[str, Any] | None) -> dict[str, str]:
    data = payload or {}
    extra = ensure_object(data.get("extra"))
    source = to_text(data.get("source") or extra.get("source") or "indicator")
    return {
        "environment": normalize_environment_value(data.get("environment") or extra.get("environment"), LIVE_ENVIRONMENT),
        "symbol": to_text(data.get("symbol")).upper(),
        "direction": to_text(data.get("direction")).lower(),
        "reverse_kind": to_text(first_non_empty(extra.get("reverse_kind"), "signal_conflict" if source == "signal" else "indicator_conflict")),
        "target_state": to_text(first_non_empty(extra.get("target_state"), "")),
        "action_type": to_text(data.get("action_type") or extra.get("action_type")),
        "trade_group_id": to_text(first_non_empty(extra.get("trade_group_id"), "")),
        "origin_signal_id": to_text(first_non_empty(extra.get("origin_signal_id"), extra.get("signal_id_orig"), "")),
        "new_direction": to_text(first_non_empty(extra.get("new_direction"), "")),
    }


def find_pending_reverse_duplicate(
    pb: Any,
    criteria: dict[str, Any] | None,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    per_page: int = REVERSE_DEDUPE_LOOKBACK_LIMIT,
) -> Any:
    lookup = dict(criteria or {})
    symbol = to_text(lookup.get("symbol")).upper()
    if not symbol:
        return None
    environment = normalize_environment_value(lookup.get("environment"), LIVE_ENVIRONMENT)
    records = list(
        pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=(
                f'symbol = "{escape_filter(symbol)}" && '
                f'environment = "{escape_filter(environment)}" && '
                'status = "pending"'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )
    for record in records:
        normalized = normalize_reverse_record(record, default_environment=environment)
        if lookup.get("direction") and normalized.get("direction") != lookup.get("direction"):
            continue
        if to_text(lookup.get("reverse_kind")) != to_text(normalized.get("reverse_kind")):
            continue
        if to_text(lookup.get("target_state")) != to_text(normalized.get("target_state")):
            continue
        if to_text(lookup.get("action_type")) != to_text(normalized.get("action_type")):
            continue
        if to_text(lookup.get("trade_group_id")) != to_text(normalized.get("trade_group_id")):
            continue
        if to_text(lookup.get("origin_signal_id")) != to_text(normalized.get("origin_signal_id")):
            continue
        if to_text(lookup.get("new_direction")) != to_text(normalized.get("new_direction")):
            continue
        return record
    return None


def upsert_reverse_record(
    pb: Any,
    payload: dict[str, Any],
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> dict[str, Any]:
    data = dict(payload or {})
    extra = ensure_object(data.get("extra"))
    criteria = build_reverse_duplicate_criteria(data)
    existing = None if data.get("dedupe") is False else find_pending_reverse_duplicate(pb, criteria, escape_filter=escape_filter)
    existing_extra = get_reverse_extra(existing)
    triggered_signals = parse_triggered_signals(
        data.get("triggered_signals") if data.get("triggered_signals") is not None else extra.get("triggered_signals")
    )
    record_payload = {
        "symbol": criteria["symbol"],
        "environment": criteria["environment"],
        "direction": criteria["direction"],
        "source": to_text(data.get("source") or "indicator") or "indicator",
        "priority": parse_integer(data.get("priority"), default=DEFAULT_REVERSE_PRIORITY, minimum=0),
        "strength": to_text(data.get("strength") or "weak") or "weak",
        "score": _to_number(data.get("score"), 0),
        "triggered_signals": triggered_signals,
        "action_type": criteria["action_type"] or "cancel",
        "status": to_text(data.get("status") or "pending") or "pending",
        "reason": to_text(data.get("reason")),
        "bar_time_ms": to_int(data.get("bar_time_ms"), 0),
        "us_time": to_text(data.get("us_time")),
        "cn_time": to_text(data.get("cn_time")),
        "extra": {
            **existing_extra,
            **extra,
            "environment": criteria["environment"],
            "reverse_kind": criteria["reverse_kind"],
            "target_state": criteria["target_state"],
            "triggered_signals": triggered_signals,
        },
    }

    if existing:
        record_id = to_text(record_value(existing, "id"))
        updated = pb.update_record(REVERSE_SIGNALS_COLLECTION, record_id, record_payload)
        saved = updated if updated is not None else merge_record_patch(existing, record_payload)
        return {"record": saved, "created": False}

    created = pb.create_record(REVERSE_SIGNALS_COLLECTION, record_payload)
    return {"record": created, "created": True}


def load_reverse_signal_threshold(pb: Any, environment: str, default: int = DEFAULT_REVERSE_THRESHOLD) -> int:
    runtime_environment = normalize_environment_value(environment)
    getter = getattr(pb, "get_runtime_config", None)
    if callable(getter):
        try:
            rows = getter(scope="all", environment=runtime_environment)
        except TypeError:
            rows = getter(environment=runtime_environment)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if to_text(row.get("key")) != "reverse_signal_threshold":
                    continue
                return parse_integer(row.get("value"), default=default, minimum=1)
    fallback_value = getattr(pb, "reverse_signal_threshold", None)
    return parse_integer(fallback_value, default=default, minimum=1)


def normalize_reverse_record(record_or_data: Any, *, default_environment: str = LIVE_ENVIRONMENT) -> dict[str, Any]:
    extra = get_reverse_extra(record_or_data)
    source = to_text(record_value(record_or_data, "source") or extra.get("source"))
    action_type = to_text(record_value(record_or_data, "action_type") or extra.get("action_type"))
    order_status = to_text(first_non_empty(extra.get("order_status"), extra.get("target_order_status"), ""))
    trade_group_id = to_text(first_non_empty(extra.get("trade_group_id"), record_value(record_or_data, "trade_group_id"), ""))
    entry_order_unique_id = to_text(
        first_non_empty(extra.get("entry_order_unique_id"), record_value(record_or_data, "entry_order_unique_id"), "")
    )
    order_unique_id = to_text(first_non_empty(extra.get("order_unique_id"), entry_order_unique_id, trade_group_id, ""))
    broker_order_id = to_text(first_non_empty(extra.get("broker_order_id"), extra.get("order_id"), ""))
    signal_id = to_text(first_non_empty(extra.get("signal_id"), ""))
    origin_signal_id = to_text(first_non_empty(extra.get("origin_signal_id"), extra.get("signal_id_orig"), ""))
    environment = to_text(record_value(record_or_data, "environment") or extra.get("environment") or default_environment)
    return {
        "id": to_text(record_value(record_or_data, "id")),
        "environment": environment or default_environment,
        "symbol": to_text(record_value(record_or_data, "symbol") or extra.get("symbol")),
        "direction": to_text(record_value(record_or_data, "direction") or extra.get("direction")),
        "source": source,
        "reverse_kind": _infer_reverse_kind(source, extra),
        "target_state": _infer_target_state(extra, order_status, action_type),
        "target_order_status": order_status,
        "priority": to_int(first_non_empty(record_value(record_or_data, "priority"), extra.get("priority"), 0), 0),
        "strength": to_text(record_value(record_or_data, "strength") or extra.get("strength")),
        "score": _to_number(first_non_empty(record_value(record_or_data, "score"), extra.get("score"), 0), 0),
        "action_type": action_type,
        "status": to_text(record_value(record_or_data, "status") or extra.get("status") or "pending"),
        "reason": to_text(record_value(record_or_data, "reason") or extra.get("reason")),
        "processed_time": to_text(record_value(record_or_data, "processed_time") or extra.get("processed_time")),
        "triggered_signals": _normalize_triggered_signals(record_value(record_or_data, "triggered_signals"), extra),
        "signal_id": signal_id,
        "origin_signal_id": origin_signal_id,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "order_unique_id": order_unique_id,
        "broker_order_id": broker_order_id,
        "order_id": broker_order_id,
        "relation_status": to_text(first_non_empty(extra.get("relation_status"), "")),
        "position_side": to_text(first_non_empty(extra.get("position_side"), "")),
        "current_direction": to_text(
            first_non_empty(extra.get("current_direction"), record_value(record_or_data, "direction"), extra.get("direction"), "")
        ),
        "new_direction": to_text(first_non_empty(extra.get("new_direction"), "")),
        "entry_price": _to_number(first_non_empty(extra.get("entry_price"), extra.get("limit_price"), extra.get("fill_price"), 0), 0),
        "quantity": _to_number(first_non_empty(extra.get("quantity"), 0), 0),
        "take_profit": _to_number(first_non_empty(extra.get("take_profit"), extra.get("tp_price"), 0), 0),
        "stop_loss": _to_number(first_non_empty(extra.get("stop_loss"), extra.get("sl_price"), 0), 0),
        "old_sl": _to_number(first_non_empty(extra.get("old_sl"), 0), 0),
        "new_sl": _to_number(first_non_empty(extra.get("new_sl"), 0), 0),
        "old_tp": _to_number(first_non_empty(extra.get("old_tp"), 0), 0),
        "new_tp": _to_number(first_non_empty(extra.get("new_tp"), 0), 0),
        "executed_action": to_text(first_non_empty(extra.get("executed_action"), "")),
        "result_status": to_text(first_non_empty(extra.get("result_status"), "")),
        "manual_requested": bool(extra.get("manual_requested")),
        "manual_requested_at": to_text(first_non_empty(extra.get("manual_requested_at"), "")),
        "bar_time_ms": to_int(first_non_empty(record_value(record_or_data, "bar_time_ms"), extra.get("bar_time_ms"), 0), 0),
        "us_time": to_text(record_value(record_or_data, "us_time") or extra.get("us_time")),
        "cn_time": to_text(record_value(record_or_data, "cn_time") or extra.get("cn_time")),
        "created": to_text(record_value(record_or_data, "created") or extra.get("created")),
        "updated": to_text(record_value(record_or_data, "updated") or extra.get("updated")),
        "extra": extra,
    }


__all__ = [
    "ACTIVE_ENTRY_ORDER_LIMIT",
    "ALLOWED_REVERSE_ACTION_TYPES",
    "DEFAULT_REVERSE_LIST_LIMIT",
    "DEFAULT_REVERSE_PRIORITY",
    "DEFAULT_REVERSE_THRESHOLD",
    "INDICATORS_COLLECTION",
    "LIVE_ENVIRONMENT",
    "MAX_REVERSE_LIST_LIMIT",
    "ORDERS_COLLECTION",
    "REVERSE_DEDUPE_LOOKBACK_LIMIT",
    "REVERSE_SIGNALS_COLLECTION",
    "build_date_range",
    "build_indicator_analysis",
    "build_order_context",
    "build_reverse_duplicate_criteria",
    "clamp_reverse_limit",
    "ensure_object",
    "escape_filter_string",
    "fetch_reverse_record",
    "find_latest_active_entry_order",
    "find_pending_reverse_duplicate",
    "first_non_empty",
    "get_reverse_extra",
    "load_latest_indicator_record",
    "load_reverse_signal_threshold",
    "map_strength",
    "merge_record_patch",
    "merge_reverse_extra",
    "normalize_environment_value",
    "normalize_reverse_record",
    "normalize_status_filters",
    "parse_triggered_signals",
    "record_value",
    "resolve_indicator_action",
    "resolve_timestamp_text",
    "upsert_reverse_record",
]
