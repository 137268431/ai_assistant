from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_api.reverse.normalize import normalize_reverse_record
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_datetime,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]

SUPPORTED_LIVE_ENVIRONMENTS = {"live", "paper"}
SUPPORTED_MODES = {"auto", "live", "paper", "backtest"}
ET = ZoneInfo("America/New_York")

LANE_DEFINITIONS = [
    {"key": "selection", "label": "选股入选", "copy": "为什么进入候选池 / 今日标的"},
    {"key": "signal", "label": "信号生成", "copy": "策略条件、过滤和信号参数"},
    {"key": "confirmation", "label": "确认/有效期", "copy": "确认、拒绝、过期、阻塞"},
    {"key": "execution", "label": "实际成交", "copy": "IBKR 执行回报/成交数量/成交价"},
    {"key": "protection", "label": "保护单", "copy": "TP/SL 创建、修改、修复与数量对齐"},
    {"key": "position", "label": "持仓变化", "copy": "剩余仓位、部分成交、部分退出"},
    {"key": "risk_adjustment", "label": "风控调整", "copy": "提前止盈/止损、移动止损、保护价格修改"},
    {"key": "scale", "label": "加减仓/买回", "copy": "部分止盈后买回、追加仓位"},
    {"key": "exit", "label": "平仓", "copy": "TP/SL/反向/手动/EOD 平仓"},
    {"key": "interrupt", "label": "中断/异常", "copy": "缺成交、保护不完整、系统中断"},
]
LANE_INDEX = {lane["key"]: index for index, lane in enumerate(LANE_DEFINITIONS)}
GRAPH_EXCLUDED_EVENT_TYPES = {"order_detail_snapshot"}
ENDPOINT_EVENT_TYPE = "lifecycle_endpoint"
TERMINAL_EVENT_TYPES = {
    "exit_take_profit",
    "exit_stop_loss",
    "exit_reverse",
    "exit_eod",
    "manual_close",
    "trade_closed",
    "entry_canceled",
    "entry_missed",
    "signal_rejected",
    "signal_expired",
}
ENTRY_TERMINAL_STATUSES = {"cancelled", "canceled", "expired", "rejected", "inactive"}
SUBMITTED_SIGNAL_STATUSES = {"submitted", "submitted_waiting_fill"}
PROTECTED_SIGNAL_STATUSES = {"protected_active", "filled_repricing_protection", "filled_position"}
PROTECTION_INCOMPLETE_SIGNAL_STATUSES = {"protection_incomplete", "protection_reprice_failed"}
ENTRY_MISSED_SIGNAL_STATUSES = {"entry_missed_limit_cap"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(value: Any) -> float:
    return to_float(value) or 0.0


def _is_present(value: Any) -> bool:
    return value is not None and value != ""


def _safe_text(value: Any) -> str:
    return to_text(value).strip()


def _upper(value: Any) -> str:
    return _safe_text(value).upper()


def _lower(value: Any) -> str:
    return _safe_text(value).lower()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    parsed = parse_json_object(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        parsed = parse_json_object(value)
        if isinstance(parsed, list):
            return list(parsed)
    return []


def _pb_get_all(pb: Any, collection: str, *, filter: str = "", sort: str = "", max_pages: int = 3, per_page: int = 200) -> tuple[list[dict[str, Any]], str]:
    try:
        get_all_records = getattr(pb, "get_all_records", None)
        if callable(get_all_records):
            rows = get_all_records(collection, filter=filter or None, sort=sort or None, max_pages=max_pages) or []
        else:
            rows = []
            get_records = getattr(pb, "get_records", None)
            if not callable(get_records):
                return [], "pb_missing_get_records"
            for page in range(1, max_pages + 1):
                page_rows = get_records(collection, filter=filter or None, sort=sort or None, per_page=per_page, page=page) or []
                rows.extend(page_rows)
                if len(page_rows) < per_page:
                    break
        return [dict(row) for row in rows if isinstance(row, dict)], ""
    except Exception as exc:  # pragma: no cover - defensive around remote PB schemas
        return [], f"{collection}:{exc}"


def _or_equals(field: str, values: list[str], *, limit: int = 20) -> str:
    cleaned = []
    for value in values or []:
        text = _safe_text(value)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f'{field} = "{escape_filter(cleaned[0])}"'
    return "(" + " || ".join(f'{field} = "{escape_filter(item)}"' for item in cleaned) + ")"


def _contains_filter(field: str, value: str) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    return f'{field} ~ "{escape_filter(text)}"'


def _combine_filter(parts: list[str]) -> str:
    cleaned = [part for part in parts if _safe_text(part)]
    return " && ".join(cleaned)


def _date_bounds(date_text: str) -> tuple[int, int]:
    try:
        return parse_market_date_bounds_ms(date_text)
    except Exception:
        return 0, 0


def _text_matches(value: str, candidates: list[Any]) -> bool:
    expected = _safe_text(value).casefold()
    if not expected:
        return False
    for candidate in candidates:
        if _safe_text(candidate).casefold() == expected:
            return True
    return False


def _reverse_row_matches_context(
    row: dict[str, Any],
    *,
    environment: str,
    signal_id: str,
    trade_group_id: str,
    order_id: str,
) -> bool:
    if not (signal_id or trade_group_id or order_id):
        return True

    normalized = normalize_reverse_record(row, default_environment=environment)
    extra = _json_object(normalized.get("extra"))
    triggered_signals = normalized.get("triggered_signals")
    if not isinstance(triggered_signals, list):
        triggered_signals = _json_list(triggered_signals)

    if signal_id and _text_matches(
        signal_id,
        [
            normalized.get("signal_id"),
            normalized.get("origin_signal_id"),
            extra.get("signal_id"),
            extra.get("origin_signal_id"),
            extra.get("signal_id_orig"),
            extra.get("source_signal_id"),
            *triggered_signals,
        ],
    ):
        return True

    if trade_group_id and _text_matches(
        trade_group_id,
        [
            normalized.get("trade_group_id"),
            normalized.get("entry_order_unique_id"),
            normalized.get("order_unique_id"),
            extra.get("trade_group_id"),
            extra.get("entry_order_unique_id"),
            extra.get("order_unique_id"),
            extra.get("bracket_group"),
        ],
    ):
        return True

    if order_id and _text_matches(
        order_id,
        [
            normalized.get("order_id"),
            normalized.get("broker_order_id"),
            normalized.get("entry_order_unique_id"),
            normalized.get("order_unique_id"),
            extra.get("order_id"),
            extra.get("broker_order_id"),
            extra.get("ib_order_id"),
            extra.get("entry_order_unique_id"),
            extra.get("order_unique_id"),
        ],
    ):
        return True

    return False


def _filter_reverse_rows_by_context(
    rows: list[dict[str, Any]],
    *,
    environment: str,
    signal_id: str,
    trade_group_id: str,
    order_id: str,
) -> list[dict[str, Any]]:
    if not (signal_id or trade_group_id or order_id):
        return rows
    return [
        row
        for row in rows
        if _reverse_row_matches_context(
            row,
            environment=environment,
            signal_id=signal_id,
            trade_group_id=trade_group_id,
            order_id=order_id,
        )
    ]


def _parse_time_text_ms(text: Any) -> int:
    value = _safe_text(text)
    if not value:
        return 0
    compact = " ".join(value.split())
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
        try:
            return int(datetime.strptime(compact, fmt).replace(tzinfo=ET).timestamp() * 1000)
        except ValueError:
            pass
    try:
        normalized = value.replace("Z", "+00:00")
        parsed_dt = datetime.fromisoformat(normalized)
        if parsed_dt.tzinfo is None:
            # PB created/updated fields are UTC ISO timestamps; keep naive text off local TZ.
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return int(parsed_dt.timestamp() * 1000)
    except Exception:
        try:
            parsed = datetime.strptime(value.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
            return int(parsed.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            return 0


def _event_time_ms(row: dict[str, Any], extra: dict[str, Any] | None = None) -> int:
    source = {**(extra or {}), **(row or {})}
    for key in (
        "bar_time_ms",
        "trade_time_ms",
        "fill_time_ms",
        "entry_bar_ms",
        "exit_bar_ms",
        "created_at_ms",
        "updated_at_ms",
        "timestamp_ms",
    ):
        value = to_int(source.get(key), 0)
        if value > 0:
            return value
    for key in ("created", "updated", "trade_time", "us_time"):
        value = _parse_time_text_ms(source.get(key))
        if value > 0:
            return value
    return 0


def _row_id(row: dict[str, Any], prefix: str) -> str:
    for key in ("id", "signal_id", "order_id", "broker_order_id", "unique_id", "exec_id"):
        text = _safe_text(row.get(key))
        if text:
            return f"{prefix}_{text}"
    return f"{prefix}_{abs(hash(str(sorted(row.items()))))}"


def _warning(code: str, message: str, *, severity: str = "warning", **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "details": {key: value for key, value in details.items() if value not in (None, "", [])},
    }


def _base_event(
    event_type: str,
    *,
    stage: str,
    state: str = "done",
    label: str = "",
    reason: str = "",
    ts_ms: int = 0,
    symbol: str = "",
    signal_id: str = "",
    trade_group_id: str = "",
    order_id: str = "",
    role: str = "",
    fill_source: str = "",
    qty: float | None = None,
    price: float | None = None,
    price_kind: str = "",
    price_label: str = "",
    source: str = "",
    details: dict[str, Any] | None = None,
    changes: list[dict[str, Any]] | None = None,
    change_summary: str = "",
    changed_at_ms: int = 0,
) -> dict[str, Any]:
    normalized_price_kind = _safe_text(price_kind) or (
        "actual_fill"
        if fill_source in {"actual_ibkr", "paper_ibkr"}
        else ("simulated_fill" if fill_source == "backtest_simulated" else "not_a_fill")
    )
    event = {
        "id": "",
        "event_type": event_type,
        "stage": stage if stage in LANE_INDEX else "interrupt",
        "state": state,
        "label": label or event_type.replace("_", " "),
        "reason": reason,
        "ts_ms": ts_ms or 0,
        "us_time": format_et_datetime(ts_ms) if ts_ms else "",
        "cn_time": format_cn_time(ts_ms) if ts_ms else "",
        "symbol": symbol,
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "order_id": order_id,
        "role": role,
        "fill_source": fill_source,
        "qty": qty,
        "price": price,
        "price_kind": normalized_price_kind,
        "price_label": price_label,
        "source": source,
        "details": details or {},
    }
    if changes:
        event_changes = [dict(item) for item in changes if isinstance(item, dict)]
        event["changes"] = event_changes
        event["change_summary"] = _safe_text(change_summary) or _format_change_summary(event_changes)
        change_ts_ms = changed_at_ms or ts_ms
        if change_ts_ms:
            event["changed_at_ms"] = change_ts_ms
            event["changed_at_us"] = format_et_datetime(change_ts_ms)
            event["changed_at_cn"] = format_cn_time(change_ts_ms)
    elif _safe_text(change_summary):
        event["change_summary"] = _safe_text(change_summary)
    return {key: value for key, value in event.items() if value is not None}


def _format_change_value(value: Any) -> str:
    if value is None or value == "":
        return "--"
    if isinstance(value, bool):
        return "true" if value else "false"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return _safe_text(value) or "--"
    if not (numeric == numeric and abs(numeric) != float("inf")):
        return _safe_text(value) or "--"
    formatted = f"{numeric:.4f}".rstrip("0").rstrip(".")
    return formatted or "0"


def _build_change(field: str, label: str, before: Any, after: Any) -> dict[str, Any]:
    if not _is_present(before) or not _is_present(after):
        return {}
    if _safe_text(before) == _safe_text(after):
        return {}
    return {
        "field": field,
        "label": label,
        "before": "" if before is None else before,
        "after": "" if after is None else after,
        "before_text": _format_change_value(before),
        "after_text": _format_change_value(after),
    }


def _format_change_summary(changes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for change in changes or []:
        if not isinstance(change, dict):
            continue
        label = _safe_text(change.get("label") or change.get("field"))
        if not label:
            continue
        before = change.get("before_text") or _format_change_value(change.get("before"))
        after = change.get("after_text") or _format_change_value(change.get("after"))
        parts.append(f"{label} {before} -> {after}")
        if len(parts) >= 3:
            break
    return " / ".join(parts)


def _first_present_from_sources(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and _is_present(source.get(key)):
                return source.get(key)
    return None


def _price_changes_from_sources(
    sources: list[dict[str, Any]],
    specs: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for field, label, before_keys, after_keys in specs:
        change = _build_change(
            field,
            label,
            _first_present_from_sources(sources, before_keys),
            _first_present_from_sources(sources, after_keys),
        )
        if change:
            changes.append(change)
    return changes


def _status_changes_from_sources(row: dict[str, Any], extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    sources = [extra or {}, row or {}]
    previous_status = _first_present_from_sources(sources, ("previous_status", "old_status", "status_before"))
    if not _is_present(previous_status):
        return []
    current_status = _first_present_from_sources(sources, ("current_status", "new_status", "status_after", "status", "order_status"))
    change = _build_change("status", "状态", previous_status, current_status)
    return [change] if change else []


def _order_price_changes(row: dict[str, Any], extra: dict[str, Any], role: str, limit_price: float) -> list[dict[str, Any]]:
    sources = [
        extra or {},
        row or {},
        {"limit_price": limit_price} if limit_price > 0 else {},
    ]
    if role in {"take_profit", "repair_tp"}:
        return _price_changes_from_sources(
            sources,
            [
                (
                    "take_profit",
                    "TP",
                    ("old_tp", "old_take_profit", "previous_tp", "previous_take_profit", "before_tp"),
                    ("new_tp", "take_profit", "tp_price", "limit_price"),
                )
            ],
        )
    if role in {"stop_loss", "repair_sl"}:
        return _price_changes_from_sources(
            sources,
            [
                (
                    "stop_loss",
                    "SL",
                    ("old_sl", "old_stop_loss", "previous_sl", "previous_stop_loss", "before_sl"),
                    ("new_sl", "stop_loss", "sl_price", "auxPrice", "aux_price", "limit_price"),
                )
            ],
        )
    return []


def _protective_change_specs() -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    return [
        (
            "stop_loss",
            "SL",
            ("old_sl", "old_stop_loss", "previous_sl", "previous_stop_loss", "before_sl"),
            ("new_sl", "stop_loss", "sl_price", "stop_price"),
        ),
        (
            "take_profit",
            "TP",
            ("old_tp", "old_take_profit", "previous_tp", "previous_take_profit", "before_tp"),
            ("new_tp", "take_profit", "tp_price", "target_price"),
        ),
    ]


def _change_values_equal(left: Any, right: Any) -> bool:
    if _safe_text(left) == _safe_text(right):
        return True
    left_number = to_float(left)
    right_number = to_float(right)
    if left_number is not None and right_number is not None:
        return abs(left_number - right_number) < 1e-9
    return False


def _stable_protective_values_from_sources(
    sources: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    changed_fields = {_safe_text(change.get("field")) for change in changes or [] if isinstance(change, dict)}
    stable_values: list[dict[str, Any]] = []
    for field, label, before_keys, after_keys in _protective_change_specs():
        if field in changed_fields:
            continue
        before = _first_present_from_sources(sources, before_keys)
        after = _first_present_from_sources(sources, after_keys)
        if not _is_present(before):
            continue
        if _is_present(after) and not _change_values_equal(before, after):
            continue
        value = after if _is_present(after) else before
        stable_values.append(
            {
                "field": field,
                "label": label,
                "value": value,
                "value_text": _format_change_value(value),
            }
        )
    return stable_values


def _format_protective_change_summary(
    sources: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> str:
    parts = []
    if changes:
        parts.extend(_format_change_summary(changes).split(" / "))
    for item in _stable_protective_values_from_sources(sources, changes):
        label = _safe_text(item.get("label") or item.get("field"))
        value = _safe_text(item.get("value_text"))
        if label and value:
            parts.append(f"{label} {value}")
        if len(parts) >= 3:
            break
    return " / ".join(part for part in parts if part)


def _event_sort_key(event: dict[str, Any]) -> tuple[int, int, str]:
    stage = _safe_text(event.get("stage"))
    return (to_int(event.get("ts_ms"), 0) or 9_999_999_999_999, LANE_INDEX.get(stage, 99), _safe_text(event.get("event_type")))


def _sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=_event_sort_key)
    for index, event in enumerate(ordered, start=1):
        event["seq"] = index
        event["id"] = event.get("id") or f"evt_{index:03d}_{_safe_text(event.get('event_type')) or 'event'}"
    return ordered


def _events_to_graph(events: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for event in events:
        if _safe_text(event.get("event_type")) in GRAPH_EXCLUDED_EVENT_TYPES:
            continue
        node_id = _safe_text(event.get("id"))
        nodes.append(
            {
                "id": node_id,
                "event_id": node_id,
                "type": _safe_text(event.get("event_type")),
                "stage": _safe_text(event.get("stage")),
                "lane": _safe_text(event.get("stage")),
                "state": _safe_text(event.get("state")) or "done",
                "label": _safe_text(event.get("label")) or _safe_text(event.get("event_type")),
                "summary": _safe_text(event.get("reason")) or _safe_text(event.get("source")),
                "reason": _safe_text(event.get("reason")),
                "ts_ms": to_int(event.get("ts_ms"), 0),
                "symbol": _safe_text(event.get("symbol")),
                "signal_id": _safe_text(event.get("signal_id")),
                "trade_group_id": _safe_text(event.get("trade_group_id")),
                "order_id": _safe_text(event.get("order_id")),
                "role": _safe_text(event.get("role")),
                "fill_source": _safe_text(event.get("fill_source")),
                "price_kind": _safe_text(event.get("price_kind")),
                "price_label": _safe_text(event.get("price_label")),
                "price": event.get("price"),
                "changes": event.get("changes") or [],
                "change_summary": _safe_text(event.get("change_summary")),
                "data": event,
            }
        )

    def context_key(node: dict[str, Any]) -> str:
        for key in ("trade_group_id", "signal_id", "symbol"):
            value = _safe_text(node.get(key))
            if value:
                return f"{key}:{value}"
        return ""

    def add_edge(left: dict[str, Any], right: dict[str, Any], edge_type: str = "next") -> None:
        source_id = _safe_text(left.get("id"))
        target_id = _safe_text(right.get("id"))
        if not source_id or not target_id or source_id == target_id:
            return
        edge_id = f"edge_{source_id}_{target_id}_{edge_type}"
        if any(edge.get("id") == edge_id for edge in edges):
            return
        edges.append({"id": edge_id, "source": source_id, "target": target_id, "type": edge_type})

    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        key = context_key(node)
        if key:
            grouped.setdefault(key, []).append(node)
    for group_nodes in grouped.values():
        for left, right in zip(group_nodes, group_nodes[1:]):
            add_edge(left, right)

    signal_heads: dict[str, dict[str, Any]] = {}
    for node in nodes:
        signal_id = _safe_text(node.get("signal_id"))
        if signal_id and not _safe_text(node.get("trade_group_id")):
            signal_heads[signal_id] = node
    seen_trade_groups: set[str] = set()
    for node in nodes:
        signal_id = _safe_text(node.get("signal_id"))
        trade_group_id = _safe_text(node.get("trade_group_id"))
        if not signal_id or not trade_group_id or trade_group_id in seen_trade_groups:
            continue
        seen_trade_groups.add(trade_group_id)
        head = signal_heads.get(signal_id)
        if head:
            add_edge(head, node, "signal_to_group")

    if warnings:
        warning_node_id = "warnings_current"
        nodes.append(
            {
                "id": warning_node_id,
                "type": "warning_rollup",
                "stage": "interrupt",
                "lane": "interrupt",
                "state": "blocked" if any(item.get("severity") in {"error", "critical"} for item in warnings) else "warning",
                "label": f"Warnings x{len(warnings)}",
                "summary": " / ".join(_safe_text(item.get("code")) for item in warnings[:3]),
                "reason": warnings[0].get("message") if warnings else "",
                "ts_ms": to_int(events[-1].get("ts_ms"), 0) if events else _now_ms(),
                "data": {"warnings": warnings},
            }
        )
    return nodes, edges


def _endpoint_context_groups(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    candidates = [
        event
        for event in events or []
        if _safe_text(event.get("event_type")) not in GRAPH_EXCLUDED_EVENT_TYPES | {ENDPOINT_EVENT_TYPE}
    ]
    if not candidates:
        return {}

    def group_by(field: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in candidates:
            value = _safe_text(event.get(field))
            if value:
                grouped.setdefault(f"{field}:{value}", []).append(event)
        return grouped

    for field in ("trade_group_id", "signal_id", "symbol"):
        grouped = group_by(field)
        if grouped:
            return grouped
    return {"global:lifecycle": candidates}


def _event_status(event: dict[str, Any]) -> str:
    details = _json_object(event.get("details"))
    return _lower(first_defined(details.get("status"), event.get("state"), event.get("status")))


def _terminal_event_for_group(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(events or [], key=_event_sort_key)
    for event in reversed(ordered):
        if _safe_text(event.get("event_type")) in TERMINAL_EVENT_TYPES:
            return event

    has_entry_fill = any(
        _safe_text(event.get("event_type")) in {"entry_filled", "entry_partially_filled"}
        or (_safe_text(event.get("event_type")) == "fill_execution" and _safe_text(event.get("role")) == "entry")
        for event in ordered
    )
    if has_entry_fill:
        return None
    for event in reversed(ordered):
        if _safe_text(event.get("role")) == "entry" and _event_status(event) in ENTRY_TERMINAL_STATUSES:
            return event
    return None


def _append_lifecycle_endpoints(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return events
    if any(_safe_text(event.get("event_type")) == ENDPOINT_EVENT_TYPE for event in events):
        return events

    result = list(events)
    now_ms = _now_ms()
    for group_events in _endpoint_context_groups(events).values():
        ordered = sorted(group_events, key=_event_sort_key)
        if not ordered:
            continue
        last_event = ordered[-1]
        terminal_event = _terminal_event_for_group(ordered)
        is_terminal = terminal_event is not None
        anchor = terminal_event or last_event
        anchor_ts = to_int(anchor.get("ts_ms"), 0)
        last_ts = to_int(last_event.get("ts_ms"), 0)
        endpoint_ts = (max(anchor_ts, last_ts) + 1) if is_terminal and max(anchor_ts, last_ts) > 0 else now_ms
        result.append(
            _base_event(
                ENDPOINT_EVENT_TYPE,
                stage="exit" if is_terminal else (_safe_text(last_event.get("stage")) or "interrupt"),
                state="terminal" if is_terminal else "active",
                label="生命周期结束" if is_terminal else "当前仍进行中",
                reason=(
                    f"ended_by_{_safe_text(anchor.get('event_type'))}"
                    if is_terminal
                    else "no_terminal_event_detected"
                ),
                ts_ms=endpoint_ts,
                symbol=_safe_text(first_defined(last_event.get("symbol"), anchor.get("symbol"))),
                signal_id=_safe_text(first_defined(last_event.get("signal_id"), anchor.get("signal_id"))),
                trade_group_id=_safe_text(first_defined(last_event.get("trade_group_id"), anchor.get("trade_group_id"))),
                order_id=_safe_text(anchor.get("order_id")),
                source="lifecycle_flow.endpoint",
                details={
                    "terminal": is_terminal,
                    "last_event_id": _safe_text(last_event.get("id")),
                    "last_event_type": _safe_text(last_event.get("event_type")),
                    "anchor_event_id": _safe_text(anchor.get("id")),
                    "anchor_event_type": _safe_text(anchor.get("event_type")),
                },
            )
        )
    return result


def _current_step(nodes: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    if warnings:
        severe = next((item for item in warnings if item.get("severity") in {"critical", "error"}), None) or warnings[0]
        return {
            "stage": "interrupt",
            "state": "blocked" if severe.get("severity") in {"critical", "error"} else "warning",
            "label": severe.get("code") or "warning",
            "reason": severe.get("message") or "需要复核",
        }
    if not nodes:
        return {"stage": "selection", "state": "empty", "label": "暂无生命周期事件", "reason": "没有匹配到数据"}
    node = nodes[-1]
    return {
        "stage": node.get("stage"),
        "state": node.get("state"),
        "label": node.get("label"),
        "reason": node.get("summary") or node.get("reason"),
        "event_id": node.get("event_id") or node.get("id"),
    }


def _extract_signal_reason(row: dict[str, Any], extra: dict[str, Any]) -> str:
    return _safe_text(first_defined(
        row.get("reason"),
        row.get("note"),
        extra.get("signal_status_reason"),
        extra.get("reason"),
        extra.get("setup_reason"),
        row.get("setup_label"),
        row.get("setup"),
    ))


def _signal_execution_for_mode(extra: dict[str, Any], broker_mode: str) -> dict[str, Any]:
    execution_by_mode = _json_object(extra.get("execution_by_mode"))
    for key in (broker_mode, _lower(broker_mode), _upper(broker_mode)):
        execution = execution_by_mode.get(key)
        if isinstance(execution, dict):
            return dict(execution)
    return {}


def _signal_effective_status(row: dict[str, Any], extra: dict[str, Any], broker_mode: str) -> str:
    top_level_status = _lower(first_defined(row.get("status"), extra.get("status")))
    if top_level_status in {"closed", "expired", "rejected", "cancelled", "canceled"} | ENTRY_MISSED_SIGNAL_STATUSES:
        return top_level_status
    broker_status = _lower(_signal_execution_for_mode(extra, broker_mode).get("status"))
    return broker_status or top_level_status


def _signal_status_event_type(status: str) -> tuple[str, str, str]:
    normalized = _lower(status)
    if normalized in {"awaiting_confirm", "confirm_pending"}:
        return "signal_pending_confirmation", "confirmation", "active"
    if normalized in {"pending", "confirmed", "executed"} | SUBMITTED_SIGNAL_STATUSES | PROTECTED_SIGNAL_STATUSES:
        return "signal_confirmed", "confirmation", "done"
    if normalized in {"rejected", "cancelled", "canceled"}:
        return "signal_rejected", "confirmation", "terminal"
    if normalized in {"expired"}:
        return "signal_expired", "confirmation", "terminal"
    if normalized in ENTRY_MISSED_SIGNAL_STATUSES:
        return "entry_missed", "execution", "terminal"
    if normalized in {"blocked", "dropped", "skipped"} | PROTECTION_INCOMPLETE_SIGNAL_STATUSES:
        return "signal_blocked", "confirmation", "blocked"
    return "signal_status", "confirmation", "done"


def _first_time_from_sources(sources: list[dict[str, Any]], *, ms_keys: tuple[str, ...], text_keys: tuple[str, ...]) -> int:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ms_keys:
            value = to_int(source.get(key), 0)
            if value > 0:
                return value
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in text_keys:
            value = _parse_time_text_ms(source.get(key))
            if value > 0:
                return value
    return 0


def _signal_status_time_ms(status: str, row: dict[str, Any], extra: dict[str, Any], fallback_ts_ms: int, *, allow_bar_time: bool = False) -> int:
    normalized = _lower(status)
    sources = [extra or {}, row or {}]
    ms_keys = [
        "status_updated_bar_time_ms",
        "status_changed_bar_time_ms",
        "status_time_ms",
        "timestamp_ms",
        "updated_at_ms",
    ]
    text_keys = [
        "status_updated_at",
        "status_changed_at",
        "status_time",
        "updated",
    ]
    if normalized == "expired":
        ms_keys[:0] = ["expired_at_ms", "expired_bar_time_ms"]
        text_keys[:0] = ["expired_at"]
    elif normalized in {"rejected", "cancelled", "canceled"}:
        ms_keys[:0] = ["rejected_at_ms", "cancelled_at_ms", "canceled_at_ms"]
        text_keys[:0] = ["rejected_at", "cancelled_at", "canceled_at"]
    elif normalized in {"confirmed", "pending", "executed"} | SUBMITTED_SIGNAL_STATUSES | PROTECTED_SIGNAL_STATUSES:
        ms_keys[:0] = ["confirmed_at_ms", "reconfirmed_at_ms", "submitted_at_ms", "executed_at_ms", "protected_at_ms"]
        text_keys[:0] = ["confirmed_at", "reconfirmed_at", "submitted_at", "executed_at", "protected_at"]
    elif normalized in {"blocked", "dropped", "skipped"} | PROTECTION_INCOMPLETE_SIGNAL_STATUSES:
        ms_keys[:0] = ["blocked_at_ms", "dropped_at_ms", "skipped_at_ms"]
        text_keys[:0] = ["blocked_at", "dropped_at", "skipped_at", "status_repaired_at"]
    elif normalized in ENTRY_MISSED_SIGNAL_STATUSES:
        ms_keys[:0] = ["entry_missed_at_ms", "missed_at_ms", "expired_at_ms", "cancelled_at_ms", "canceled_at_ms"]
        text_keys[:0] = ["entry_missed_at", "missed_at", "expired_at", "cancelled_at", "canceled_at"]
    elif normalized in {"closed", "completed"}:
        ms_keys[:0] = ["closed_at_ms", "closed_bar_time_ms", "exit_fill_bar_time_ms"]
        text_keys[:0] = ["closed_at", "completed_at", "exit_fill_time"]
    if allow_bar_time:
        ms_keys.append("bar_time_ms")
    value = _first_time_from_sources(sources, ms_keys=tuple(ms_keys), text_keys=tuple(text_keys))
    return value or fallback_ts_ms


def _role(row: dict[str, Any]) -> str:
    extra = _json_object(row.get("extra"))
    role = _lower(first_defined(row.get("role"), extra.get("role")))
    order_type = _lower(first_defined(row.get("order_type"), extra.get("order_type")))
    unique = _lower(first_defined(row.get("unique_id"), extra.get("unique_id"), row.get("order_ref"), row.get("cOID")))
    if role:
        return role
    if "take" in order_type or "tp" in unique:
        return "take_profit"
    if "stop" in order_type or "sl" in unique:
        return "stop_loss"
    if "close" in order_type or "close" in unique:
        return "close"
    return "entry"


def _order_time_ms(row: dict[str, Any]) -> int:
    return _event_time_ms(row, _json_object(row.get("extra")))


def _order_fill_time_ms(row: dict[str, Any], extra: dict[str, Any] | None = None, fallback_ts_ms: int = 0) -> int:
    sources = [extra or {}, row or {}]
    value = _first_time_from_sources(
        sources,
        ms_keys=(
            "filled_bar_time_ms",
            "fill_bar_time_ms",
            "trade_time_ms",
            "last_execution_time_ms",
            "lastExecutionTimeMs",
        ),
        text_keys=(
            "last_execution_time",
            "lastExecutionTime",
            "fill_time",
            "filled_us_time",
            "trade_time",
        ),
    )
    return value or fallback_ts_ms


def _order_submission_time_ms(row: dict[str, Any], extra: dict[str, Any] | None = None, fallback_ts_ms: int = 0) -> int:
    sources = [extra or {}, row or {}]
    created_ms = _first_time_from_sources(
        sources,
        ms_keys=(
            "created_bar_time_ms",
            "submitted_bar_time_ms",
            "submit_bar_time_ms",
            "order_created_bar_time_ms",
            "order_submitted_bar_time_ms",
            "placed_bar_time_ms",
            "created_at_ms",
        ),
        text_keys=(
            "created_at",
            "submitted_at",
            "submit_time",
            "order_created_at",
            "order_submitted_at",
            "created",
        ),
    )
    return created_ms or fallback_ts_ms or _order_time_ms(row)


def _order_id(row: dict[str, Any]) -> str:
    return _safe_text(first_defined(row.get("order_id"), row.get("broker_order_id"), row.get("ib_order_id"), row.get("unique_id")))


def _order_match_ids(row: dict[str, Any]) -> list[str]:
    extra = _json_object(row.get("extra"))
    ids: list[str] = []
    for value in (
        row.get("order_id"),
        row.get("broker_order_id"),
        row.get("ib_order_id"),
        row.get("unique_id"),
        row.get("order_ref"),
        row.get("cOID"),
        extra.get("order_id"),
        extra.get("broker_order_id"),
        extra.get("ib_order_id"),
        extra.get("unique_id"),
        extra.get("coid"),
        extra.get("cOID"),
    ):
        text = _safe_text(value)
        if text and text not in ids:
            ids.append(text)
    return ids


def _order_qty(row: dict[str, Any]) -> float:
    extra = _json_object(row.get("extra"))
    return _safe_float(first_defined(row.get("quantity"), row.get("total_qty"), row.get("shares"), extra.get("quantity"), extra.get("shares")))


def _order_filled_qty(row: dict[str, Any]) -> float:
    extra = _json_object(row.get("extra"))
    return _safe_float(first_defined(row.get("filled_qty"), row.get("filledQuantity"), row.get("filled_quantity"), extra.get("filled_qty"), extra.get("filledQuantity")))


def _order_fill_price(row: dict[str, Any]) -> float:
    extra = _json_object(row.get("extra"))
    return _safe_float(first_defined(row.get("fill_price"), row.get("avgPrice"), row.get("avg_price"), row.get("average_price"), extra.get("fill_price"), extra.get("avgPrice")))


def _order_limit_price(row: dict[str, Any]) -> float:
    extra = _json_object(row.get("extra"))
    return _safe_float(first_defined(row.get("limit_price"), row.get("price"), row.get("tp_price"), row.get("sl_price"), extra.get("limit_price"), extra.get("price"), extra.get("tp_price"), extra.get("sl_price")))


def _order_reference_price(row: dict[str, Any], role: str) -> tuple[float | None, str, str]:
    fill_price = _order_fill_price(row)
    limit_price = _order_limit_price(row)
    if fill_price > 0:
        return fill_price, "order_detail_unverified_fill", "订单明细价"
    if limit_price <= 0:
        return None, "not_a_fill", ""
    if role in {"take_profit", "repair_tp"}:
        return limit_price, "take_profit_price", "止盈价"
    if role in {"stop_loss", "repair_sl"}:
        return limit_price, "stop_loss_price", "止损价"
    if role == "entry":
        return limit_price, "entry_limit", "开仓限价"
    if role == "close":
        return limit_price, "close_limit", "平仓限价"
    return limit_price, "order_reference_price", "订单参考价"


def _order_status(row: dict[str, Any]) -> str:
    status = _lower(first_defined(row.get("status"), row.get("order_status"), _json_object(row.get("extra")).get("status")))
    return status.replace("-", "_").replace(" ", "_")


def _normalized_order_status(value: Any) -> str:
    return _lower(value).replace("-", "_").replace(" ", "_")


def _order_initial_status(row: dict[str, Any], extra: dict[str, Any] | None, current_status: str) -> str:
    previous_status = _first_present_from_sources([extra or {}, row or {}], ("previous_status", "old_status", "status_before"))
    return _normalized_order_status(previous_status) or current_status


def _order_submission_state(initial_status: str, current_status: str) -> str:
    if _is_open_status(initial_status) or _is_open_status(current_status):
        return "active"
    return "done"


def _is_filled_status(status: str) -> bool:
    normalized = _lower(status).replace("-", "_").replace(" ", "_")
    return normalized in {"filled", "executed", "closed", "complete", "completed", "partiallyfilled", "partially_filled"}


def _is_canceled_status(status: str) -> bool:
    normalized = _lower(status).replace("-", "_").replace(" ", "_")
    return normalized in {"cancelled", "canceled", "inactive", "api_cancelled", "api_canceled"}


def _is_open_status(status: str) -> bool:
    normalized = _lower(status).replace("-", "_").replace(" ", "_")
    return normalized in {
        "init",
        "submitted",
        "presubmitted",
        "pre_submitted",
        "pending",
        "pendingsubmit",
        "pending_submit",
        "apipending",
        "api_pending",
        "partiallyfilled",
        "partially_filled",
        "active",
        "working",
        "held",
    }


def _order_cancel_event_meta(role: str) -> tuple[str, str, str, str]:
    normalized = _lower(role)
    if normalized in {"take_profit", "repair_tp"}:
        return "take_profit_canceled", "exit", "done", "止盈单取消"
    if normalized in {"stop_loss", "repair_sl"}:
        return "stop_loss_canceled", "exit", "done", "止损单取消"
    if normalized == "entry":
        return "entry_canceled", "execution", "terminal", "开仓订单取消"
    return "close_canceled", "exit", "done", "平仓订单取消"


def _order_transition_reason(row: dict[str, Any], extra: dict[str, Any], fallback: str) -> str:
    return _safe_text(
        first_defined(
            extra.get("status_transition_text"),
            row.get("status_transition_text"),
            extra.get("status_reason"),
            row.get("status_reason"),
            extra.get("reason"),
            row.get("reason"),
            fallback,
        )
    )


def _fill_source_for_environment(environment: str) -> str:
    return "paper_ibkr" if environment == "paper" else "actual_ibkr"


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows or []:
        key = _safe_text(first_defined(row.get("id"), row.get("unique_id"), row.get("order_id"), row.get("broker_order_id")))
        if not key:
            key = str(sorted((row or {}).items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _order_detail_event_limit() -> int:
    try:
        return max(20, int(float(os.environ.get("IBKR_LIFECYCLE_ORDER_DETAIL_EVENT_LIMIT", "300") or 300)))
    except Exception:
        return 300


def _order_detail_compression_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    extra = _json_object(row.get("extra"))
    relation_status = _lower(first_defined(row.get("relation_status"), extra.get("relation_status")))
    order_key = _order_id(row) or _safe_text(first_defined(row.get("id"), row.get("unique_id")))
    if not order_key:
        order_key = _order_detail_row_identity(row)
    return (
        order_key,
        _role(row),
        _order_status(row),
        relation_status,
    )


def _order_detail_row_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            _safe_text(row.get("id")),
            _safe_text(first_defined(row.get("unique_id"), row.get("order_ref"), row.get("cOID"))),
            _order_id(row),
            _role(row),
            _order_status(row),
            str(_order_time_ms(row) or 0),
            _safe_text(first_defined(row.get("created"), row.get("updated"))),
        ]
    )


def _compress_order_detail_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_rows = sorted(
        [row for row in rows or [] if isinstance(row, dict)],
        key=lambda item: (_order_time_ms(item) or 9_999_999_999_999, _role(item), _order_id(item), _safe_text(item.get("created"))),
    )
    grouped: dict[tuple[str, str, str, str], dict[str, dict[str, Any]]] = {}
    for row in ordered_rows:
        key = _order_detail_compression_key(row)
        bucket = grouped.setdefault(key, {"first": row, "latest": row})
        bucket["latest"] = row

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in grouped.values():
        for row in (bucket.get("first"), bucket.get("latest")):
            if not isinstance(row, dict):
                continue
            identity = _order_detail_row_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            kept.append(row)

    kept.sort(key=lambda item: (_order_time_ms(item) or 9_999_999_999_999, _role(item), _order_id(item), _safe_text(item.get("created"))))
    limit = _order_detail_event_limit()
    if len(kept) > limit:
        return kept[-limit:]
    return kept


def _fill_order_id(row: dict[str, Any]) -> str:
    return _safe_text(first_defined(row.get("order_id"), row.get("broker_order_id"), row.get("ib_order_id")))


def _build_fill_index(fills: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_order: dict[str, list[dict[str, Any]]] = {}
    for fill in fills or []:
        order_id = _fill_order_id(fill)
        if not order_id:
            continue
        by_order.setdefault(order_id, []).append(fill)
    for rows in by_order.values():
        rows.sort(key=lambda row: _event_time_ms(row) or 0)
    return by_order


def _fill_qty(fill: dict[str, Any]) -> float:
    return abs(_safe_float(first_defined(fill.get("shares"), fill.get("qty"), fill.get("quantity"))))


def _fill_price(fill: dict[str, Any]) -> float:
    return _safe_float(first_defined(fill.get("price"), fill.get("fill_price"), fill.get("avg_price")))


def _fill_side(fill: dict[str, Any]) -> str:
    return _lower(first_defined(fill.get("side"), fill.get("action")))


def _signed_position_delta(role: str, side: str, qty: float, direction: str) -> float:
    normalized_role = _lower(role)
    normalized_side = _lower(side)
    normalized_direction = _lower(direction)
    if normalized_role == "entry":
        if normalized_side == "sell" and normalized_direction != "short":
            return -qty
        if normalized_side == "buy" and normalized_direction == "short":
            return -qty
        return qty
    if normalized_role in {"take_profit", "stop_loss", "close", "repair_tp", "repair_sl"}:
        return -qty
    return 0.0


def _load_live_sources(
    pb: Any,
    *,
    environment: str,
    data_environment: str,
    payload: dict[str, Any],
    market_date: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    source_errors: list[dict[str, Any]] = []
    symbol = _upper(payload.get("symbol"))
    signal_id = _safe_text(payload.get("signal_id"))
    trade_group_id = _safe_text(payload.get("trade_group_id"))
    order_id = _safe_text(payload.get("order_id"))
    start_ms, end_ms = _date_bounds(market_date)

    def load(collection: str, parts: list[str], sort: str = "-created", max_pages: int = 3) -> list[dict[str, Any]]:
        rows, error = _pb_get_all(pb, collection, filter=_combine_filter(parts), sort=sort, max_pages=max_pages)
        if error:
            source_errors.append(_warning("source_read_failed", f"读取 {collection} 失败: {error}", collection=collection))
        return rows

    broker_env_part = f'environment = "{escape_filter(environment)}"'
    data_env_part = f'environment = "{escape_filter(data_environment)}"'
    signal_parts = [data_env_part]
    target_parts = [data_env_part]
    order_parts = [broker_env_part]
    reverse_query_parts = [broker_env_part]
    system_parts: list[str] = []

    if symbol:
        for parts in (signal_parts, target_parts, order_parts, reverse_query_parts):
            parts.append(f'symbol = "{escape_filter(symbol)}"')
    if market_date:
        target_parts.append(f'date = "{escape_filter(market_date)}"')
        signal_parts.append(f'date = "{escape_filter(market_date)}"')
    if signal_id:
        signal_parts.append(f'signal_id = "{escape_filter(signal_id)}"')
        order_parts.append(f'signal_id = "{escape_filter(signal_id)}"')
        system_parts.append(f'(title ~ "{escape_filter(signal_id)}" || detail ~ "{escape_filter(signal_id)}")')
    if trade_group_id:
        order_parts.append(f'trade_group_id = "{escape_filter(trade_group_id)}"')
        system_parts.append(f'(title ~ "{escape_filter(trade_group_id)}" || detail ~ "{escape_filter(trade_group_id)}")')
    if order_id:
        order_parts.append(f'(order_id = "{escape_filter(order_id)}" || broker_order_id = "{escape_filter(order_id)}")')
        system_parts.append(f'(title ~ "{escape_filter(order_id)}" || detail ~ "{escape_filter(order_id)}")')
    if start_ms > 0 and end_ms > 0:
        reverse_query_parts.append(f"bar_time_ms >= {start_ms}")
        reverse_query_parts.append(f"bar_time_ms < {end_ms}")
        if not (signal_id or trade_group_id or order_id):
            order_parts.append(f"bar_time_ms >= {start_ms}")
            order_parts.append(f"bar_time_ms < {end_ms}")

    targets = load("ibkr_targets", target_parts, sort="-updated", max_pages=2)
    signals = load("ibkr_signals", signal_parts, sort="bar_time_ms", max_pages=4)
    orders = _dedupe_rows(load("orders", order_parts, sort="bar_time_ms,created", max_pages=4))
    order_details = _dedupe_rows(load("ibkr_order_details", order_parts, sort="bar_time_ms,created", max_pages=4))
    reverse_rows = _filter_reverse_rows_by_context(
        load("ibkr_reverse_signals", reverse_query_parts, sort="bar_time_ms", max_pages=3),
        environment=environment,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        order_id=order_id,
    )

    system_filter_parts = []
    if system_parts:
        system_filter_parts.append("(" + " || ".join(system_parts) + ")")
        if start_ms > 0 and end_ms > 0:
            system_filter_parts.extend([f'created >= "{escape_filter(market_date)} 00:00:00"', f'created <= "{escape_filter(market_date)} 23:59:59"'])
        system_filter_parts.append(f'(environment = "{escape_filter(environment)}" || environment = "global")')
    system_events = load("system_events", system_filter_parts, sort="-created", max_pages=1) if system_parts else []

    order_ids = []
    for row in orders:
        for match_id in _order_match_ids(row):
            if match_id not in order_ids:
                order_ids.append(match_id)
    fill_parts = [broker_env_part]
    order_filter = _or_equals("order_id", order_ids, limit=30)
    if order_filter:
        fill_parts.append(order_filter)
    else:
        if symbol:
            fill_parts.append(f'symbol = "{escape_filter(symbol)}"')
        if start_ms > 0 and end_ms > 0:
            fill_parts.extend([f"trade_time_ms >= {start_ms}", f"trade_time_ms < {end_ms}"])
    fills = load("ibkr_execution_fills", fill_parts, sort="trade_time_ms,created", max_pages=4)

    return {
        "targets": targets,
        "signals": signals,
        "orders": orders,
        "order_details": order_details,
        "reverse_rows": reverse_rows,
        "system_events": system_events,
        "fills": fills,
    }, source_errors, {
        "symbol": symbol,
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "market_date": market_date,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "broker_mode": environment,
        "data_environment": data_environment,
    }


def _build_live_events(
    pb: Any,
    *,
    environment: str,
    data_environment: str,
    payload: dict[str, Any],
    market_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    data_environment = resolve_data_environment(data_environment)
    sources, warnings, context = _load_live_sources(
        pb,
        environment=environment,
        data_environment=data_environment,
        payload=payload,
        market_date=market_date,
    )
    symbol = context.get("symbol") or ""
    signal_id = context.get("signal_id") or ""
    trade_group_id = context.get("trade_group_id") or ""
    fill_source = _fill_source_for_environment(environment)
    raw_order_details = list(sources.get("order_details") or [])
    compressed_order_details = _compress_order_detail_rows(raw_order_details)
    sources["order_details"] = compressed_order_details
    order_detail_compression = {
        "raw": len(raw_order_details),
        "events": len(compressed_order_details),
        "compressed": len(compressed_order_details) < len(raw_order_details),
        "limit": _order_detail_event_limit(),
    }
    events: list[dict[str, Any]] = []

    for row in sources["targets"]:
        extra = _json_object(row.get("extra"))
        event_symbol = _upper(row.get("symbol")) or symbol
        events.append(
            _base_event(
                "target_selected",
                stage="selection",
                label="选股入选",
                reason=_safe_text(first_defined(row.get("scan_reason"), row.get("note"), extra.get("scan_reason"), extra.get("reason"), "target_selected")),
                ts_ms=_event_time_ms(row, extra),
                symbol=event_symbol,
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                source="ibkr_targets",
                details={
                    "score": _safe_float(row.get("score")),
                    "direction_bias": _safe_text(row.get("direction_bias")),
                    "status": _safe_text(row.get("status")),
                    "rank": to_int(first_defined(row.get("rank"), row.get("subscription_rank")), 0),
                },
            )
        )

    for row in sources["signals"]:
        extra = _json_object(row.get("extra"))
        event_symbol = _upper(row.get("symbol")) or symbol
        row_signal_id = _safe_text(row.get("signal_id")) or signal_id
        ts_ms = _event_time_ms(row, extra)
        reason = _extract_signal_reason(row, extra)
        events.append(
            _base_event(
                "signal_generated",
                stage="signal",
                label="信号生成",
                reason=reason or "signal_generated",
                ts_ms=ts_ms,
                symbol=event_symbol,
                signal_id=row_signal_id,
                trade_group_id=trade_group_id,
                source="ibkr_signals",
                details={
                    "signal": _safe_text(row.get("signal")),
                    "direction": _safe_text(row.get("direction")),
                    "entry_plan_price": _safe_float(row.get("entry")),
                    "planned_shares": _safe_float(row.get("shares")),
                    "take_profit_plan": _safe_float(row.get("take_profit")),
                    "stop_loss_plan": _safe_float(row.get("stop_loss")),
                    "price_policy": "planned_only_not_actual_fill",
                },
            )
        )
        broker_execution = _signal_execution_for_mode(extra, environment)
        status = _signal_effective_status(row, extra, environment)
        if status and status not in {"generated", "new"}:
            event_type, stage, state = _signal_status_event_type(status)
            status_ts_ms = _signal_status_time_ms(status, row, extra, ts_ms)
            broker_status_reason = (
                first_defined(broker_execution.get("status_reason"), broker_execution.get("note"))
                if _lower(broker_execution.get("status")) == status
                else None
            )
            events.append(
                _base_event(
                    event_type,
                    stage=stage,
                    state=state,
                    label=status.replace("_", " "),
                    reason=_safe_text(
                        first_defined(broker_status_reason, extra.get("status_reason"), row.get("note"), reason, status)
                    ),
                    ts_ms=status_ts_ms,
                    symbol=event_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=trade_group_id,
                    source="ibkr_signals.status",
                    details={
                        "status": status,
                        "top_level_status": _lower(row.get("status")),
                        "signal_bar_time_ms": ts_ms,
                    },
                )
            )
        for item in _json_list(extra.get("status_history")):
            if not isinstance(item, dict):
                continue
            item_status = _lower(item.get("status"))
            if not item_status:
                continue
            event_type, stage, state = _signal_status_event_type(item_status)
            item_ts_ms = _signal_status_time_ms(item_status, item, {}, ts_ms, allow_bar_time=True)
            events.append(
                _base_event(
                    event_type,
                    stage=stage,
                    state=state,
                    label=item_status.replace("_", " "),
                    reason=_safe_text(first_defined(item.get("reason"), item.get("note"), item_status)),
                    ts_ms=item_ts_ms,
                    symbol=event_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=trade_group_id,
                    source="ibkr_signals.status_history",
                    details=item,
                )
            )

    fill_index = _build_fill_index(sources["fills"])
    entry_actual_qty = 0.0
    exit_actual_qty = 0.0
    entry_order_count = 0
    seen_exit_before_entry = False
    protection_open_qty_by_role = {"take_profit": 0.0, "stop_loss": 0.0}
    protection_roles_seen: set[str] = set()

    orders_sorted = sorted(sources["orders"], key=lambda row: (_order_time_ms(row) or 9_999_999_999_999, _role(row), _order_id(row)))
    for row in orders_sorted:
        extra = _json_object(row.get("extra"))
        role = _role(row)
        status = _order_status(row)
        order_id = _order_id(row)
        row_symbol = _upper(row.get("symbol")) or symbol
        row_signal_id = _safe_text(first_defined(row.get("signal_id"), extra.get("signal_id"), signal_id))
        row_trade_group = _safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"), trade_group_id))
        quantity = _order_qty(row)
        filled_qty = _order_filled_qty(row)
        fill_price = _order_fill_price(row)
        limit_price = _order_limit_price(row)
        ts_ms = _order_time_ms(row)
        direction = _lower(first_defined(row.get("direction"), row.get("position_side"), extra.get("direction"), extra.get("position_side")))
        status_changes = _status_changes_from_sources(row, extra)
        initial_status = _order_initial_status(row, extra, status)
        submission_ts_ms = _order_submission_time_ms(row, extra, ts_ms)
        submission_state = _order_submission_state(initial_status, status)

        if role == "entry":
            entry_order_count += 1
            events.append(
                _base_event(
                    "entry_submitted",
                    stage="execution",
                    state=submission_state,
                    label="开仓订单提交",
                    reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), initial_status, "entry_submitted")),
                    ts_ms=submission_ts_ms,
                    symbol=row_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=row_trade_group,
                    order_id=order_id,
                    role=role,
                    qty=quantity,
                    price=limit_price if limit_price > 0 else None,
                    price_kind="entry_limit" if limit_price > 0 else "not_a_fill",
                    price_label="开仓限价" if limit_price > 0 else "",
                    source="orders",
                    details={"status": initial_status, "current_status": status, "quantity": quantity, "limit_price": limit_price, "price_policy": "limit_or_plan_not_actual_fill"},
                )
            )
        elif role in {"take_profit", "repair_tp"}:
            protection_roles_seen.add("take_profit")
            protection_open_qty_by_role["take_profit"] += quantity if _is_open_status(status) else 0.0
            event_type = "take_profit_modified" if role == "repair_tp" or extra.get("old_tp") or extra.get("new_tp") else "take_profit_created"
            price_changes = _order_price_changes(row, extra, role, limit_price) if event_type.endswith("modified") else []
            price_change_sources = [
                extra,
                row,
                {"tp_price": limit_price, "take_profit": limit_price} if limit_price > 0 else {},
            ]
            price_change_summary = _format_protective_change_summary(price_change_sources, price_changes) if event_type.endswith("modified") else ""
            event_ts_ms = ts_ms if event_type.endswith("modified") else submission_ts_ms
            event_state = ("active" if _is_open_status(status) else "done") if event_type.endswith("modified") else submission_state
            events.append(
                _base_event(
                    event_type,
                    stage="protection" if event_type.endswith("created") else "risk_adjustment",
                    state=event_state,
                    label="止盈单" + ("修改" if event_type.endswith("modified") else "创建"),
                    reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), initial_status, event_type)),
                    ts_ms=event_ts_ms,
                    symbol=row_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=row_trade_group,
                    order_id=order_id,
                    role=role,
                    qty=quantity,
                    price=limit_price if limit_price > 0 else None,
                    price_kind="take_profit_price" if limit_price > 0 else "not_a_fill",
                    price_label="止盈价" if limit_price > 0 else "",
                    source="orders",
                    details={"status": initial_status, "current_status": status, "quantity": quantity, "tp_price": limit_price, **({"old_tp": extra.get("old_tp"), "new_tp": extra.get("new_tp")} if extra else {})},
                    changes=price_changes,
                    change_summary=price_change_summary,
                )
            )
        elif role in {"stop_loss", "repair_sl"}:
            protection_roles_seen.add("stop_loss")
            protection_open_qty_by_role["stop_loss"] += quantity if _is_open_status(status) else 0.0
            event_type = "stop_loss_modified" if role == "repair_sl" or extra.get("old_sl") or extra.get("new_sl") else "stop_loss_created"
            price_changes = _order_price_changes(row, extra, role, limit_price) if event_type.endswith("modified") else []
            price_change_sources = [
                extra,
                row,
                {"sl_price": limit_price, "stop_loss": limit_price, "stop_price": limit_price} if limit_price > 0 else {},
            ]
            price_change_summary = _format_protective_change_summary(price_change_sources, price_changes) if event_type.endswith("modified") else ""
            event_ts_ms = ts_ms if event_type.endswith("modified") else submission_ts_ms
            event_state = ("active" if _is_open_status(status) else "done") if event_type.endswith("modified") else submission_state
            events.append(
                _base_event(
                    event_type,
                    stage="protection" if event_type.endswith("created") else "risk_adjustment",
                    state=event_state,
                    label="止损单" + ("修改" if event_type.endswith("modified") else "创建"),
                    reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), initial_status, event_type)),
                    ts_ms=event_ts_ms,
                    symbol=row_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=row_trade_group,
                    order_id=order_id,
                    role=role,
                    qty=quantity,
                    price=limit_price if limit_price > 0 else None,
                    price_kind="stop_loss_price" if limit_price > 0 else "not_a_fill",
                    price_label="止损价" if limit_price > 0 else "",
                    source="orders",
                    details={"status": initial_status, "current_status": status, "quantity": quantity, "sl_price": limit_price, **({"old_sl": extra.get("old_sl"), "new_sl": extra.get("new_sl")} if extra else {})},
                    changes=price_changes,
                    change_summary=price_change_summary,
                )
            )
        else:
            events.append(
                _base_event(
                    "close_submitted",
                    stage="exit",
                    state=submission_state,
                    label="平仓订单提交",
                    reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), initial_status, "close_submitted")),
                    ts_ms=submission_ts_ms,
                    symbol=row_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=row_trade_group,
                    order_id=order_id,
                    role=role,
                    qty=quantity,
                    price=limit_price if limit_price > 0 else None,
                    price_kind="close_limit" if limit_price > 0 else "not_a_fill",
                    price_label="平仓限价" if limit_price > 0 else "",
                    source="orders",
                    details={"status": initial_status, "current_status": status, "quantity": quantity, "limit_price": limit_price},
                )
            )

        matched_by_exec_id: dict[str, dict[str, Any]] = {}
        for match_id in _order_match_ids(row):
            for fill in fill_index.get(match_id, []):
                fill_key = _safe_text(first_defined(fill.get("exec_id"), fill.get("id"), f"{match_id}:{_event_time_ms(fill)}:{_fill_qty(fill)}:{_fill_price(fill)}"))
                matched_by_exec_id.setdefault(fill_key, fill)
        matched_fills = sorted(matched_by_exec_id.values(), key=lambda fill: _event_time_ms(fill) or 0)
        if matched_fills:
            fill_qty_sum = sum(_fill_qty(fill) for fill in matched_fills)
            fill_value_sum = sum(_fill_qty(fill) * _fill_price(fill) for fill in matched_fills)
            fill_avg_price = fill_value_sum / fill_qty_sum if fill_qty_sum > 0 else 0.0
            if filled_qty > 0 and abs(fill_qty_sum - filled_qty) > max(0.01, filled_qty * 0.001):
                warnings.append(
                    _warning(
                        "order_filled_qty_mismatch_execution_sum",
                        "订单 filled_qty 与 ibkr_execution_fills 汇总不一致，生命周期以逐笔 execution fills 为准。",
                        order_id=order_id,
                        order_filled_qty=filled_qty,
                        execution_fill_qty=fill_qty_sum,
                    )
                )
            aggregate_qty = fill_qty_sum
            aggregate_price = fill_avg_price
            for fill in matched_fills:
                fill_qty = _fill_qty(fill)
                fill_px = _fill_price(fill)
                fill_ts = _event_time_ms(fill) or ts_ms
                events.append(
                    _base_event(
                        "fill_execution",
                        stage="execution" if role == "entry" else "exit",
                        label="IBKR 实际成交",
                        reason=_safe_text(fill.get("exec_id")) or "ibkr_execution_fill",
                        ts_ms=fill_ts,
                        symbol=_upper(fill.get("symbol")) or row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source=fill_source,
                        qty=fill_qty,
                        price=fill_px,
                        source="ibkr_execution_fills",
                        details={
                            "exec_id": _safe_text(fill.get("exec_id")),
                            "side": _fill_side(fill),
                            "commission": _safe_float(fill.get("commission")),
                            "source_priority": "execution_fill_wins",
                        },
                    )
                )
                delta = _signed_position_delta(role, _fill_side(fill), fill_qty, direction)
                if role == "entry":
                    if seen_exit_before_entry or entry_order_count > 1:
                        events.append(
                            _base_event(
                                "scale_in_rebuy_filled" if seen_exit_before_entry else "entry_incremental_fill",
                                stage="scale" if seen_exit_before_entry else "execution",
                                label="买回/加仓成交" if seen_exit_before_entry else "追加开仓成交",
                                reason="actual_ibkr_fill_after_exit" if seen_exit_before_entry else "incremental_entry_fill",
                                ts_ms=fill_ts,
                                symbol=row_symbol,
                                signal_id=row_signal_id,
                                trade_group_id=row_trade_group,
                                order_id=order_id,
                                role=role,
                                fill_source=fill_source,
                                qty=fill_qty,
                                price=fill_px,
                                source="ibkr_execution_fills",
                                details={"position_delta": delta},
                            )
                        )
                    entry_actual_qty += max(0.0, delta)
                elif role in {"take_profit", "repair_tp", "stop_loss", "repair_sl", "close"}:
                    exit_actual_qty += fill_qty
                    seen_exit_before_entry = True
        else:
            aggregate_qty = filled_qty
            aggregate_price = fill_price
            if _is_filled_status(status) and (filled_qty <= 0 or fill_price <= 0):
                warnings.append(
                    _warning(
                        "filled_status_without_fill_qty",
                        "订单显示已成交，但没有实际成交股数/价格；不会用计划价补齐。",
                        severity="error",
                        order_id=order_id,
                        role=role,
                        status=status,
                    )
                )
                events.append(
                    _base_event(
                        "fill_missing_actual",
                        stage="interrupt",
                        state="error",
                        label="缺少实际成交",
                        reason="filled_status_without_actual_qty_or_price",
                        ts_ms=ts_ms,
                        symbol=row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source="unknown",
                        source="orders",
                        details={"status": status, "quantity": quantity, "filled_qty": filled_qty, "fill_price": fill_price},
                    )
                )
            elif filled_qty > 0 and fill_price > 0:
                warnings.append(
                    _warning(
                        "execution_fill_unmatched",
                        "未找到逐笔 ibkr_execution_fills，暂使用订单同步的实际成交字段；请核对 broker execution 回补。",
                        order_id=order_id,
                        role=role,
                    )
                )

        if aggregate_qty > 0 and aggregate_price > 0:
            aggregate_ts_ms = _order_fill_time_ms(row, extra, ts_ms)
            is_partial = quantity > 0 and aggregate_qty < quantity - 0.0001
            if role == "entry":
                event_type = "entry_partially_filled" if is_partial else "entry_filled"
                if not matched_fills:
                    entry_actual_qty += aggregate_qty
                events.append(
                    _base_event(
                        event_type,
                        stage="execution" if not is_partial else "position",
                        state="partially_filled" if is_partial else "done",
                        label="开仓部分成交" if is_partial else "开仓已成交",
                        reason="actual_order_fill" if not matched_fills else "actual_execution_fills_aggregated",
                        ts_ms=aggregate_ts_ms,
                        symbol=row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source=fill_source,
                        qty=aggregate_qty,
                        price=aggregate_price,
                        source="ibkr_execution_fills" if matched_fills else "orders.actual_fill_fields",
                        details={"requested_qty": quantity, "filled_qty": aggregate_qty, "remaining_qty": max(0.0, quantity - aggregate_qty)},
                    )
                )
            elif role in {"take_profit", "repair_tp"}:
                if not matched_fills:
                    exit_actual_qty += aggregate_qty
                    seen_exit_before_entry = True
                event_type = "partial_take_profit_filled" if is_partial else "exit_take_profit"
                events.append(
                    _base_event(
                        event_type,
                        stage="position" if is_partial else "exit",
                        state="partially_filled" if is_partial else "terminal",
                        label="部分止盈成交" if is_partial else "止盈平仓",
                        reason="actual_take_profit_fill",
                        ts_ms=aggregate_ts_ms,
                        symbol=row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source=fill_source,
                        qty=aggregate_qty,
                        price=aggregate_price,
                        source="ibkr_execution_fills" if matched_fills else "orders.actual_fill_fields",
                        details={"requested_qty": quantity, "filled_qty": aggregate_qty},
                    )
                )
            elif role in {"stop_loss", "repair_sl"}:
                if not matched_fills:
                    exit_actual_qty += aggregate_qty
                    seen_exit_before_entry = True
                event_type = "partial_stop_loss_filled" if is_partial else "exit_stop_loss"
                events.append(
                    _base_event(
                        event_type,
                        stage="position" if is_partial else "exit",
                        state="partially_filled" if is_partial else "terminal",
                        label="部分止损成交" if is_partial else "止损平仓",
                        reason="actual_stop_loss_fill",
                        ts_ms=aggregate_ts_ms,
                        symbol=row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source=fill_source,
                        qty=aggregate_qty,
                        price=aggregate_price,
                        source="ibkr_execution_fills" if matched_fills else "orders.actual_fill_fields",
                        details={"requested_qty": quantity, "filled_qty": aggregate_qty},
                    )
                )
            elif role == "close":
                if not matched_fills:
                    exit_actual_qty += aggregate_qty
                    seen_exit_before_entry = True
                close_reason = _lower(first_defined(row.get("reason"), extra.get("reason"), extra.get("close_reason")))
                if "reverse" in close_reason:
                    event_type = "exit_reverse"
                    label = "TV 退出平仓"
                elif "eod" in close_reason or "end" in close_reason:
                    event_type = "exit_eod"
                    label = "收盘/EOD 平仓"
                else:
                    event_type = "manual_close"
                    label = "手动/普通平仓"
                events.append(
                    _base_event(
                        event_type,
                        stage="exit",
                        state="terminal",
                        label=label,
                        reason=close_reason or "actual_close_fill",
                        ts_ms=aggregate_ts_ms,
                        symbol=row_symbol,
                        signal_id=row_signal_id,
                        trade_group_id=row_trade_group,
                        order_id=order_id,
                        role=role,
                        fill_source=fill_source,
                        qty=aggregate_qty,
                        price=aggregate_price,
                        source="ibkr_execution_fills" if matched_fills else "orders.actual_fill_fields",
                        details={"requested_qty": quantity, "filled_qty": aggregate_qty},
                    )
                )

        if _is_canceled_status(status):
            cancel_event_type, cancel_stage, cancel_state, cancel_label = _order_cancel_event_meta(role)
            reference_price, reference_price_kind, reference_price_label = _order_reference_price(row, role)
            events.append(
                _base_event(
                    cancel_event_type,
                    stage=cancel_stage,
                    state=cancel_state,
                    label=cancel_label,
                    reason=_order_transition_reason(row, extra, "order_canceled"),
                    ts_ms=ts_ms,
                    symbol=row_symbol,
                    signal_id=row_signal_id,
                    trade_group_id=row_trade_group,
                    order_id=order_id,
                    role=role,
                    qty=quantity,
                    price=reference_price,
                    price_kind=reference_price_kind,
                    price_label=reference_price_label,
                    source="orders.status",
                    details={
                        "status": status,
                        "initial_status": initial_status,
                        "quantity": quantity,
                        "filled_qty": filled_qty,
                        "fill_price": fill_price,
                        "transition_policy": "terminal_status_split_from_submission_event",
                    },
                    changes=status_changes,
                )
            )

    remaining_position_qty = max(0.0, entry_actual_qty - exit_actual_qty)
    excessive_protection_roles = {
        role: qty
        for role, qty in protection_open_qty_by_role.items()
        if qty > remaining_position_qty + 0.0001
    }
    if entry_actual_qty > 0 and excessive_protection_roles:
        largest_open_protection_qty = max(excessive_protection_roles.values())
        warnings.append(
            _warning(
                "protection_qty_exceeds_remaining_position",
                "保护单数量超过实际剩余仓位，部分成交/部分平仓后应按 IBKR 实际剩余股数修正 TP/SL。",
                severity="error",
                actual_entry_qty=entry_actual_qty,
                actual_exit_qty=exit_actual_qty,
                remaining_position_qty=remaining_position_qty,
                open_protection_qty_by_role=protection_open_qty_by_role,
                excessive_roles=excessive_protection_roles,
            )
        )
        events.append(
            _base_event(
                "protection_qty_mismatch",
                stage="interrupt",
                state="blocked",
                label="保护单数量不匹配",
                reason="protection_qty_exceeds_remaining_position",
                ts_ms=_now_ms(),
                symbol=symbol,
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                source="lifecycle_flow.protection_check",
                details={
                    "actual_entry_qty": entry_actual_qty,
                    "actual_exit_qty": exit_actual_qty,
                    "remaining_position_qty": remaining_position_qty,
                    "open_protection_qty": largest_open_protection_qty,
                    "open_protection_qty_by_role": protection_open_qty_by_role,
                    "excessive_roles": excessive_protection_roles,
                    "recommended_action": "modify_or_replace_tp_sl_to_actual_remaining_qty",
                },
            )
        )
    if entry_actual_qty > 0 and remaining_position_qty > 0 and "stop_loss" not in protection_roles_seen:
        warnings.append(
            _warning(
                "stop_loss_missing_for_partial_fill",
                "已有实际成交仓位，但未看到止损保护单；部分成交也必须按实际成交股数创建/修复 SL。",
                severity="error",
                remaining_position_qty=remaining_position_qty,
            )
        )

    for row in sorted(sources.get("order_details") or [], key=lambda item: (_order_time_ms(item) or 9_999_999_999_999, _role(item), _order_id(item))):
        extra = _json_object(row.get("extra"))
        detail_role = _role(row)
        detail_status = _order_status(row)
        detail_stage = "execution" if detail_role == "entry" else "protection" if detail_role in {"take_profit", "stop_loss", "repair_tp", "repair_sl"} else "exit"
        detail_price, detail_price_kind, detail_price_label = _order_reference_price(row, detail_role)
        detail_changes = _status_changes_from_sources(row, extra)
        events.append(
            _base_event(
                "order_detail_snapshot",
                stage=detail_stage,
                state="active" if _is_open_status(detail_status) else "done",
                label="订单明细快照",
                reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), detail_status, "order_detail_snapshot")),
                ts_ms=_order_time_ms(row),
                symbol=_upper(row.get("symbol")) or symbol,
                signal_id=_safe_text(first_defined(row.get("signal_id"), extra.get("signal_id"), signal_id)),
                trade_group_id=_safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"), trade_group_id)),
                order_id=_order_id(row),
                role=detail_role,
                qty=_order_filled_qty(row) or _order_qty(row) or None,
                price=detail_price,
                price_kind=detail_price_kind,
                price_label=detail_price_label,
                source="ibkr_order_details",
                details={
                    "status": detail_status,
                    "quantity": _order_qty(row),
                    "filled_qty": _order_filled_qty(row),
                    "fill_price": _order_fill_price(row),
                    "limit_price": _order_limit_price(row),
                    "calculation_policy": "timeline_only_not_position_calculation",
                },
                changes=detail_changes,
            )
        )

    for row in sources["reverse_rows"]:
        extra = _json_object(row.get("extra"))
        action = _lower(first_defined(row.get("action_type"), row.get("status"), extra.get("action_type")))
        event_type = "reverse_action"
        stage = "interrupt"
        if action in {"adjust_sl", "adjust_stop", "move_sl"}:
            event_type = "stop_loss_modified"
            stage = "risk_adjustment"
        elif action in {"adjust_tp", "move_tp"}:
            event_type = "take_profit_modified"
            stage = "risk_adjustment"
        elif action in {"close", "cancel", "reverse_close"}:
            event_type = "exit_reverse"
            stage = "exit"
        change_sources = [extra, row]
        changes = _price_changes_from_sources(change_sources, _protective_change_specs())
        events.append(
            _base_event(
                event_type,
                stage=stage,
                state="done" if stage != "interrupt" else "warning",
                label=action.replace("_", " ") if action else "reverse action",
                reason=_safe_text(first_defined(row.get("reason"), extra.get("reason"), action)),
                ts_ms=_event_time_ms(row, extra),
                symbol=_upper(row.get("symbol")) or symbol,
                signal_id=_safe_text(first_defined(row.get("signal_id"), extra.get("signal_id"), signal_id)),
                trade_group_id=_safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"), trade_group_id)),
                source="ibkr_reverse_signals",
                details={"action_type": action, "old_sl": extra.get("old_sl"), "new_sl": extra.get("new_sl"), "old_tp": extra.get("old_tp"), "new_tp": extra.get("new_tp")},
                changes=changes,
                change_summary=_format_protective_change_summary(change_sources, changes),
            )
        )

    for row in sources["system_events"]:
        extra = _json_object(first_defined(row.get("detail"), row.get("extra")))
        severity = _lower(first_defined(row.get("severity"), row.get("level"), extra.get("severity"))) or "warning"
        events.append(
            _base_event(
                "system_interrupt",
                stage="interrupt",
                state="error" if severity in {"error", "critical"} else "warning",
                label=_safe_text(first_defined(row.get("event_type"), row.get("type"), "system event")),
                reason=_safe_text(first_defined(row.get("title"), row.get("message"), row.get("reason"), extra.get("reason"))),
                ts_ms=_event_time_ms(row, extra),
                symbol=symbol,
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                source="system_events",
                details={"severity": severity, **extra},
            )
        )

    unmatched_fills = []
    order_ids = {match_id for row in orders_sorted for match_id in _order_match_ids(row)}
    for fill in sources["fills"]:
        fill_order_id = _fill_order_id(fill)
        if fill_order_id and fill_order_id not in order_ids:
            unmatched_fills.append(fill)
    for fill in unmatched_fills[:20]:
        warnings.append(
            _warning(
                "execution_fill_unmatched",
                "找到 execution fill，但没有匹配到订单生命周期记录。",
                order_id=_fill_order_id(fill),
                exec_id=_safe_text(fill.get("exec_id")),
            )
        )

    source_summary = {
        "counts": {name: len(rows) for name, rows in sources.items()},
        "raw_counts": {
            **{name: len(rows) for name, rows in sources.items()},
            "order_details": len(raw_order_details),
        },
        "order_detail_compression": order_detail_compression,
        "fill_policy": "live/paper prices and quantities come from ibkr_execution_fills first, then verified order actual fill fields; planned signal prices are never treated as fills.",
        "actual_entry_qty": entry_actual_qty,
        "actual_exit_qty": exit_actual_qty,
        "remaining_position_qty": remaining_position_qty,
        "open_protection_qty_by_role": protection_open_qty_by_role,
        "environment": environment,
        "broker_mode": environment,
        "data_environment": data_environment,
    }
    return events, warnings, context, source_summary


def _load_backtest_run(pb: Any, run_id: str) -> tuple[dict[str, Any], str]:
    if not run_id:
        return {}, "missing_run_id"
    rows, error = _pb_get_all(pb, "ibkr_backtest_runs", filter=f'id = "{escape_filter(run_id)}"', sort="-created", max_pages=1)
    if error:
        return {}, error
    return (rows[0] if rows else {}), "" if rows else "run_not_found"


def _backtest_stage(stage: str, event_type: str) -> str:
    normalized_stage = _lower(stage)
    event = _lower(event_type)
    if normalized_stage == "target":
        return "selection"
    if normalized_stage == "signal":
        return "signal"
    if normalized_stage == "execution":
        return "execution"
    if normalized_stage == "risk":
        return "risk_adjustment"
    if normalized_stage == "special":
        return "interrupt" if "reverse" in event else "scale"
    if normalized_stage in LANE_INDEX:
        return normalized_stage
    if event.startswith("target"):
        return "selection"
    if event.startswith("signal"):
        return "signal"
    if "fill" in event or "opened" in event:
        return "execution"
    if "adjust" in event or event.endswith("modified"):
        return "risk_adjustment"
    if "close" in event or "exit" in event or "closed" in event:
        return "exit"
    return "interrupt"


def _normalize_backtest_event(row: dict[str, Any], *, run_id: str, symbol_filter: str = "", date_filter: str = "") -> dict[str, Any] | None:
    symbol = _upper(row.get("symbol"))
    event_date = _safe_text(first_defined(row.get("date"), row.get("trade_date")))
    if date_filter and event_date and event_date != date_filter:
        return None
    if symbol_filter and symbol and symbol != symbol_filter:
        return None
    event_type = _safe_text(row.get("event_type")) or _safe_text(row.get("type")) or "backtest_event"
    event_status = _lower(first_defined(row.get("status"), row.get("action_type")))
    stage = _backtest_stage(_safe_text(row.get("stage")), event_type)
    if _lower(event_type) in {"reverse_action", "reverse_signal", "reverse_adjust_sl"}:
        if event_status in {"adjust_sl", "adjust_tp", "adjust_stop", "move_sl", "move_tp", "adjusted"}:
            stage = "risk_adjustment"
        elif event_status in {"close", "closed", "reverse_close", "manual_close"}:
            stage = "exit"
        elif event_status in {"cancel", "cancelled", "canceled"}:
            stage = "interrupt"
    fill_event = any(token in _lower(event_type) for token in ("fill", "opened", "closed", "trade"))
    row_details = _json_object(row.get("details")) if not isinstance(row.get("details"), dict) else dict(row.get("details") or {})
    change_sources = [row, row_details]
    changes = _price_changes_from_sources(change_sources, _protective_change_specs())
    return _base_event(
        event_type,
        stage=stage,
        state=_safe_text(row.get("status")) or "done",
        label=_safe_text(row.get("label")) or event_type.replace("_", " "),
        reason=_safe_text(row.get("reason")),
        ts_ms=to_int(first_defined(row.get("bar_time_ms"), row.get("entry_bar_ms"), row.get("exit_bar_ms")), 0),
        symbol=symbol,
        signal_id=_safe_text(row.get("signal_id")),
        trade_group_id=_safe_text(row.get("trade_group_id")),
        fill_source="backtest_simulated" if fill_event else "",
        qty=_safe_float(first_defined(row.get("shares"), row.get("qty"), row.get("quantity"))) if fill_event else None,
        price=_safe_float(first_defined(row.get("entry_price"), row.get("exit_price"), row.get("price"))) if fill_event else None,
        source="ibkr_backtest_runs.metrics.backtest_audit",
        details={"run_id": run_id, "backtest_simulated": fill_event, **row_details},
        changes=changes,
        change_summary=_format_protective_change_summary(change_sources, changes),
    )


def _build_backtest_fallback_events(pb: Any, *, run_id: str, symbol: str = "", date_filter: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def load(collection: str, sort: str) -> list[dict[str, Any]]:
        rows, error = _pb_get_all(pb, collection, filter=f'run_id = "{escape_filter(run_id)}"', sort=sort, max_pages=6)
        if error:
            warnings.append(_warning("source_read_failed", f"读取 {collection} 失败: {error}", collection=collection))
        return rows

    for row in load("ibkr_backtest_targets", "bar_time_ms"):
        if symbol and _upper(row.get("symbol")) != symbol:
            continue
        if date_filter and _safe_text(row.get("date")) != date_filter:
            continue
        extra = _json_object(row.get("extra"))
        events.append(
            _base_event(
                "target_selected",
                stage="selection",
                label="回测选股入选",
                reason=_safe_text(first_defined(row.get("scan_reason"), extra.get("scan_reason"), "backtest_target")),
                ts_ms=_event_time_ms(row, extra),
                symbol=_upper(row.get("symbol")),
                source="ibkr_backtest_targets",
                details={"run_id": run_id, "score": _safe_float(row.get("score")), "fill_source_note": "selection_not_fill"},
            )
        )
    for row in load("ibkr_backtest_signals", "bar_time_ms"):
        if symbol and _upper(row.get("symbol")) != symbol:
            continue
        if date_filter and _safe_text(row.get("date")) != date_filter:
            continue
        extra = _json_object(row.get("extra"))
        events.append(
            _base_event(
                "signal_generated",
                stage="signal",
                state=_lower(row.get("status")) or "generated",
                label="回测信号生成",
                reason=_safe_text(first_defined(row.get("reason"), extra.get("signal_status_reason"), "backtest_signal")),
                ts_ms=_event_time_ms(row, extra),
                symbol=_upper(row.get("symbol")),
                signal_id=_safe_text(row.get("signal_id")),
                source="ibkr_backtest_signals",
                details={"run_id": run_id, "entry_plan_price": _safe_float(row.get("entry")), "planned_shares": _safe_float(row.get("shares")), "fill_source_note": "signal_not_actual_fill"},
            )
        )
        if _lower(row.get("status")) in {"executed", "filled"}:
            events.append(
                _base_event(
                    "entry_filled",
                    stage="execution",
                    label="回测模拟开仓",
                    reason="backtest_simulated_entry",
                    ts_ms=_event_time_ms(row, extra),
                    symbol=_upper(row.get("symbol")),
                    signal_id=_safe_text(row.get("signal_id")),
                    fill_source="backtest_simulated",
                    qty=_safe_float(row.get("shares")),
                    price=_safe_float(row.get("entry")),
                    source="ibkr_backtest_signals",
                    details={"run_id": run_id},
                )
            )
    for row in load("ibkr_backtest_trades", "entry_bar_ms"):
        if symbol and _upper(row.get("symbol")) != symbol:
            continue
        trade_date = _safe_text(first_defined(row.get("date"), row.get("trade_date"), row.get("entry_date")))
        if date_filter and trade_date and trade_date != date_filter:
            continue
        extra = _json_object(row.get("extra"))
        events.append(
            _base_event(
                "trade_opened",
                stage="execution",
                label="回测开仓成交",
                reason=_safe_text(first_defined(row.get("reason"), "backtest_trade_opened")),
                ts_ms=to_int(row.get("entry_bar_ms"), 0),
                symbol=_upper(row.get("symbol")),
                signal_id=_safe_text(row.get("signal_id")),
                trade_group_id=_safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"))),
                fill_source="backtest_simulated",
                qty=_safe_float(row.get("shares")),
                price=_safe_float(row.get("entry_price")),
                source="ibkr_backtest_trades",
                details={"run_id": run_id, "entry_slippage_bps": extra.get("entry_slippage_bps")},
            )
        )
        exit_reason = _lower(row.get("exit_reason")) or "closed"
        if to_int(row.get("exit_bar_ms"), 0) > 0:
            events.append(
                _base_event(
                    "trade_closed",
                    stage="exit",
                    state=exit_reason,
                    label="回测平仓",
                    reason=exit_reason,
                    ts_ms=to_int(row.get("exit_bar_ms"), 0),
                    symbol=_upper(row.get("symbol")),
                    signal_id=_safe_text(row.get("signal_id")),
                    trade_group_id=_safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"))),
                    fill_source="backtest_simulated",
                    qty=_safe_float(row.get("shares")),
                    price=_safe_float(row.get("exit_price")),
                    source="ibkr_backtest_trades",
                    details={"run_id": run_id, "pnl": _safe_float(row.get("pnl")), "pnl_pct": _safe_float(row.get("pnl_pct"))},
                )
            )
    for row in load("ibkr_backtest_reverse_signals", "bar_time_ms"):
        if symbol and _upper(row.get("symbol")) != symbol:
            continue
        if date_filter and _safe_text(row.get("date")) != date_filter:
            continue
        extra = _json_object(row.get("extra"))
        action = _lower(first_defined(row.get("action_type"), row.get("status")))
        stage = "risk_adjustment" if action.startswith("adjust") else "exit" if action in {"close", "cancel"} else "interrupt"
        change_sources = [extra, row]
        changes = _price_changes_from_sources(change_sources, _protective_change_specs())
        events.append(
            _base_event(
                "reverse_action",
                stage=stage,
                state=action or "generated",
                label="回测反向/调整",
                reason=_safe_text(first_defined(row.get("reason"), action)),
                ts_ms=_event_time_ms(row, extra),
                symbol=_upper(row.get("symbol")),
                signal_id=_safe_text(first_defined(row.get("signal_id"), extra.get("signal_id"))),
                trade_group_id=_safe_text(first_defined(row.get("trade_group_id"), extra.get("trade_group_id"))),
                source="ibkr_backtest_reverse_signals",
                details={"run_id": run_id, "action_type": action, **extra},
                changes=changes,
                change_summary=_format_protective_change_summary(change_sources, changes),
            )
        )
    return events, warnings


def _build_backtest_events(pb: Any, *, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    run_id = _safe_text(first_defined(payload.get("run_id"), payload.get("id")))
    symbol = _upper(payload.get("symbol"))
    date_filter = _safe_text(first_defined(payload.get("backtest_date"), payload.get("date"), payload.get("market_date")))
    warnings: list[dict[str, Any]] = []
    context = {"run_id": run_id, "symbol": symbol, "backtest_date": date_filter}
    run, error = _load_backtest_run(pb, run_id)
    if error:
        warnings.append(_warning("backtest_run_load_failed", f"无法读取回测 run: {error}", severity="error", run_id=run_id))
        return [], warnings, context, {"counts": {}, "fill_policy": "backtest unavailable"}

    metrics = _json_object(run.get("metrics"))
    extra = _json_object(run.get("extra"))
    audit = metrics.get("backtest_audit") if isinstance(metrics.get("backtest_audit"), dict) else {}
    if not audit:
        audit = extra.get("backtest_audit_summary") if isinstance(extra.get("backtest_audit_summary"), dict) else {}
    timeline = []
    if isinstance(audit, dict):
        timeline.extend(item for item in audit.get("timeline", []) if isinstance(item, dict))
        focus_day = audit.get("focus_day") if isinstance(audit.get("focus_day"), dict) else {}
        timeline.extend(item for item in focus_day.get("timeline", []) if isinstance(item, dict))
        for flow in audit.get("symbol_day_flows", []) if isinstance(audit.get("symbol_day_flows"), list) else []:
            if not isinstance(flow, dict):
                continue
            if symbol and _upper(flow.get("symbol")) != symbol:
                continue
            if date_filter and _safe_text(flow.get("date")) != date_filter:
                continue
            timeline.extend(item for item in flow.get("events", []) if isinstance(item, dict))

    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in timeline:
        event = _normalize_backtest_event(item, run_id=run_id, symbol_filter=symbol, date_filter=date_filter)
        if not event:
            continue
        key = f"{event.get('event_type')}|{event.get('symbol')}|{event.get('signal_id')}|{event.get('ts_ms')}|{event.get('reason')}"
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    fallback_events: list[dict[str, Any]] = []
    fallback_warnings: list[dict[str, Any]] = []
    if not events:
        fallback_events, fallback_warnings = _build_backtest_fallback_events(pb, run_id=run_id, symbol=symbol, date_filter=date_filter)
        events.extend(fallback_events)
        warnings.extend(fallback_warnings)

    source_summary = {
        "counts": {
            "ibkr_backtest_runs": 1,
            "native_audit_events": len(events) if timeline else 0,
            "fallback_events": len(fallback_events),
        },
        "audit_mode": "native" if timeline else "derived",
        "fill_policy": "Backtest lifecycle labels only simulated fill events as backtest_simulated; selection, signal, and adjustment events are not IBKR fills.",
        "run": {
            "id": run_id,
            "name": _safe_text(run.get("name")),
            "status": _safe_text(run.get("status")),
            "date_from": _safe_text(run.get("date_from")),
            "date_to": _safe_text(run.get("date_to")),
            "source_environment": _safe_text(run.get("source_environment")),
        },
    }
    return events, warnings, context, source_summary


def build_lifecycle_flow_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    requested_mode = _lower(payload.get("mode")) or "auto"
    if requested_mode not in SUPPORTED_MODES:
        return {"ok": False, "error": "unsupported_mode", "mode": requested_mode, "supported_modes": sorted(SUPPORTED_MODES)}, 400

    requested_environment = _lower(payload.get("environment"))
    has_backtest_context = bool(_safe_text(payload.get("run_id")))
    if requested_mode == "backtest" or requested_environment == "backtest" or has_backtest_context:
        requested_mode = "backtest"
        environment = "backtest"
        data_environment = "backtest"
        events, warnings, context, source_summary = _build_backtest_events(pb, payload=payload)
    else:
        mode_payload = {
            **payload,
            "broker_mode": payload.get("broker_mode") or (requested_mode if requested_mode in {"live", "paper"} else payload.get("environment")),
            "market_data_mode": payload.get("market_data_mode") or payload.get("data_environment") or payload.get("environment"),
            "data_environment": payload.get("data_environment"),
        }
        environment = request_broker_mode(mode_payload)
        data_environment = request_market_data_mode(mode_payload)
        if environment not in SUPPORTED_LIVE_ENVIRONMENTS:
            return {"ok": False, "error": "unsupported_environment", "environment": environment, "supported_environments": sorted(SUPPORTED_LIVE_ENVIRONMENTS)}, 400
        requested_market_date = _safe_text(first_defined(payload.get("market_date"), payload.get("date"))) or current_market_date(time_strings)
        events, warnings, context, source_summary = _build_live_events(
            pb,
            environment=environment,
            data_environment=data_environment,
            payload=payload,
            market_date=requested_market_date,
        )
        requested_mode = environment

    ordered_events = _sort_events(_append_lifecycle_endpoints(events))
    nodes, edges = _events_to_graph(ordered_events, warnings)
    response = {
        "ok": True,
        "mode": requested_mode,
        "environment": environment,
        "broker_mode": environment,
        "data_environment": data_environment,
        "market_data_environment": data_environment,
        "shared_market_data": data_environment == "live",
        "context": context,
        "current_step": _current_step(nodes, warnings),
        "lanes": LANE_DEFINITIONS,
        "nodes": nodes,
        "edges": edges,
        "events": ordered_events,
        "warnings": warnings,
        "source_summary": source_summary,
        "computed_at_ms": _now_ms(),
        "computed_at_us": format_et_datetime(_now_ms()),
        "computed_at_cn": format_cn_time(_now_ms()),
        "source": "ibkr-api",
    }
    return response, 200


__all__ = ["build_lifecycle_flow_response"]
