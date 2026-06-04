from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ibkr_api.analytics.daily_signals import build_daily_signal_analytics_response


TARGET_DECISIONS_COLLECTION = "ibkr_target_decisions"
SIGNALS_COLLECTION = "ibkr_signals"
ORDERS_COLLECTION = "orders"

ENTRY_ROLES = {"entry"}
PROTECTION_ROLES = {"take_profit", "repair_tp", "tp", "stop_loss", "repair_sl", "sl"}
EXIT_ROLES = {"take_profit", "repair_tp", "tp", "stop_loss", "repair_sl", "sl", "close", "manual_close", "market_close", "close_order", "reverse_close"}
FILLED_STATUSES = {
    "filled",
    "executed",
    "closed",
    "complete",
    "completed",
    "partiallyfilled",
    "partially_filled",
}
POSITIVE_DECISIONS = {"selected", "active", "accepted", "candidate"}
NEGATIVE_DECISIONS = {"rejected", "not_selected", "deferred", "error", "blocked"}

NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
TimeStrings = Callable[[], dict[str, str]]


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _lower(value) in {"1", "true", "yes", "y", "on", "include", "included"}


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        if isinstance(parsed, list):
            return list(parsed)
        if isinstance(parsed, dict):
            return [dict(parsed)]
    return []


def _date_token(value: Any, fallback: str) -> str:
    text = _text(value) or fallback
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _next_date_token(date_token: str) -> str:
    return (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _load_records(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    sort: str = "-created",
    per_page: int = 200,
    max_pages: int = 10,
) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    try:
        getter = getattr(pb, "get_records", None)
        if not callable(getter):
            return [], f"{collection}:missing_get_records"
        safe_per_page = max(1, min(int(per_page or 200), 200))
        for page in range(1, max(1, int(max_pages or 1)) + 1):
            batch = getter(collection, filter=filter_expr, sort=sort, per_page=safe_per_page, page=page) or []
            page_rows = [dict(row) for row in batch if isinstance(row, dict)]
            rows.extend(page_rows)
            if len(page_rows) < safe_per_page:
                break
        return rows, ""
    except Exception as exc:
        return [], f"{collection}:{exc}"


def _role(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    raw = _lower(row.get("role") or extra.get("role") or row.get("order_type") or extra.get("order_type"))
    raw = raw.replace("-", "_").replace(" ", "_")
    unique_id = _lower(row.get("unique_id") or extra.get("unique_id") or row.get("coid") or extra.get("coid"))
    if raw in {"entry", "entry_order"}:
        return "entry"
    if raw in {"takeprofit", "take_profit", "repair_tp", "tp"}:
        return "take_profit"
    if raw in {"stoploss", "stop_loss", "repair_sl", "sl", "stop"}:
        return "stop_loss"
    if raw in {"close", "manual_close", "market_close", "close_order", "reverse_close", "marketclose"}:
        return "close"
    if unique_id.startswith("entry_"):
        return "entry"
    if unique_id.startswith(("close_", "manual_close_", "market_close_")):
        return "close"
    return raw or "unknown"


def _is_filled(row: dict[str, Any]) -> bool:
    extra = _as_object(row.get("extra"))
    filled_qty = _to_float(row.get("filled_qty") if row.get("filled_qty") not in (None, "") else extra.get("filled_qty"))
    actual_filled_qty = _to_float(
        row.get("actual_filled_qty") if row.get("actual_filled_qty") not in (None, "") else extra.get("actual_filled_qty")
    )
    return (
        _lower(row.get("status") or extra.get("status")) in FILLED_STATUSES
        or (filled_qty is not None and filled_qty > 0)
        or (actual_filled_qty is not None and actual_filled_qty > 0)
    )


def _order_reason(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    for value in (
        row.get("reason"),
        row.get("status_reason"),
        row.get("note"),
        row.get("close_reason"),
        extra.get("close_reason"),
        extra.get("reason"),
        extra.get("status_reason"),
        extra.get("status_reason_human"),
        extra.get("close_reason_code"),
    ):
        text = _text(value)
        if text:
            return text
    return ""


def _infer_related_order_reason(order: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    own_reason = _order_reason(order)
    if own_reason:
        return own_reason
    order_ms = _event_time_ms(order)
    candidates: list[tuple[int, int, str]] = []
    for other in rows:
        if other is order:
            continue
        reason = _order_reason(other)
        if not reason:
            continue
        other_ms = _event_time_ms(other)
        distance = abs(order_ms - other_ms) if order_ms and other_ms else 0
        if distance and distance > 15 * 60 * 1000:
            continue
        role = _role(other)
        status = _lower(other.get("status") or _as_object(other.get("extra")).get("status"))
        priority = 3
        if role == "entry" and status == "closed":
            priority = 0
        elif role in EXIT_ROLES and _is_filled(other):
            priority = 1
        elif role in PROTECTION_ROLES and status in {"canceled", "cancelled"}:
            priority = 2
        candidates.append((priority, distance, reason))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else ""


def _event_time_ms(row: dict[str, Any]) -> int:
    for field in ("ts_ms", "bar_time_ms", "created_ms", "updated_ms", "trade_time_ms", "filled_at_ms"):
        value = _to_int(row.get(field), 0)
        if value > 0:
            return value
    return _parse_time_text_ms(_event_time_text(row))


def _event_time_text(row: dict[str, Any]) -> str:
    for field in ("us_time", "created", "updated", "trade_time", "fill_time"):
        text = _text(row.get(field))
        if text:
            return text
    return ""


def _parse_time_text_ms(value: Any) -> int:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    text = _text(value)
    if not text:
        return 0
    if text.isdigit():
        return _to_int(text, 0)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except Exception:
            continue
    return 0


def _row_or_extra(row: dict[str, Any], *fields: str) -> Any:
    extra = _as_object(row.get("extra"))
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            return value
    for field in fields:
        value = extra.get(field)
        if value not in (None, ""):
            return value
    return None


def _signal_status(signal: dict[str, Any]) -> str:
    extra = _as_object(signal.get("extra"))
    return _lower(signal.get("status") or extra.get("status") or "unknown") or "unknown"


def _signal_reason(signal: dict[str, Any]) -> str:
    extra = _as_object(signal.get("extra"))
    for value in (
        signal.get("status_reason"),
        signal.get("reason"),
        signal.get("note"),
        extra.get("status_reason_human"),
        extra.get("status_reason"),
        extra.get("rejection_reason_human"),
        extra.get("rejection_reason_code"),
        extra.get("reason"),
    ):
        text = _text(value)
        if text:
            return text
    return ""


def _signal_name(signal: dict[str, Any]) -> str:
    return _text(_row_or_extra(signal, "signal", "setup", "setup_name", "setup_label", "signal_type"))


def _signal_bar_time_ms(signal: dict[str, Any]) -> int:
    return _to_int(_row_or_extra(signal, "bar_time_ms", "signal_bar_time_ms", "ts_ms"), 0)


def _signal_expired_at(signal: dict[str, Any]) -> str:
    return _text(_row_or_extra(signal, "expired_at", "validation_expired_at"))


def _signal_expired_at_ms(signal: dict[str, Any]) -> int:
    value = _to_int(_row_or_extra(signal, "expired_at_ms", "expired_bar_time_ms", "validation_expired_at_ms"), 0)
    if value > 0:
        return value
    return _parse_time_text_ms(_signal_expired_at(signal))


def _signal_status_explanation(signal: dict[str, Any]) -> str:
    status = _signal_status(signal)
    reason = _signal_reason(signal)
    if reason:
        return reason
    expired_at = _signal_expired_at(signal)
    if status == "expired":
        return f"signal expired at {expired_at}" if expired_at else "signal expired before execution"
    if status in {"rejected", "cancelled", "canceled"}:
        return "signal rejected before order submission"
    if status in {"closed", "executed", "filled", "complete", "completed", "protected_active", "filled_position"}:
        return "signal completed execution lifecycle"
    if status == "filled_repricing_protection":
        return "entry filled; protection orders are repricing"
    if status == "protection_reprice_failed":
        return "entry filled; protection reprice failed and needs action"
    if status == "entry_missed_limit_cap":
        return "entry missed limit cap; signal is terminal"
    if status in {"pending", "confirmed", "submitted", "submitted_waiting_fill"}:
        return "signal awaiting execution lifecycle update"
    return status


def _build_signal_summary(signals: list[dict[str, Any]]) -> dict[str, Any]:
    latest_signal = signals[-1] if signals else {}
    status_reason = _signal_reason(latest_signal)
    return {
        "total": len(signals),
        "latest_signal_id": _text(latest_signal.get("signal_id")),
        "latest_status": _signal_status(latest_signal) if latest_signal else "",
        "latest_reason": status_reason,
        "latest_direction": _text(latest_signal.get("direction")),
        "latest_time": _event_time_text(latest_signal),
        "signal": _signal_name(latest_signal),
        "status_reason": status_reason,
        "bar_time_ms": _signal_bar_time_ms(latest_signal),
        "expired_at": _signal_expired_at(latest_signal),
        "expired_at_ms": _signal_expired_at_ms(latest_signal),
        "status_explanation": _signal_status_explanation(latest_signal) if latest_signal else "",
    }


def _decision_reason(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    for value in (row.get("reason_text"), row.get("reason_code"), row.get("decision"), extra.get("reason")):
        text = _text(value)
        if text:
            return text
    return ""


def _decision_reason_code(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    return _text(row.get("reason_code") or extra.get("reason_code"))


def _decision_source(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    return _text(row.get("source") or extra.get("source"))


def _decision_object(row: dict[str, Any], field: str) -> dict[str, Any]:
    direct = _as_object(row.get(field))
    if direct:
        return direct
    extra = _as_object(row.get("extra"))
    return _as_object(extra.get(field))


def _decision_rejection_examples(row: dict[str, Any]) -> list[Any]:
    extra = _as_object(row.get("extra"))
    for value in (
        row.get("rejection_examples"),
        extra.get("rejection_examples"),
        extra.get("failed_gates"),
        extra.get("rejections"),
    ):
        rows = _as_list(value)
        if rows:
            return rows
    return []


def _decision_created_ms(row: dict[str, Any]) -> int:
    value = _to_int(_row_or_extra(row, "created_ms"), 0)
    if value > 0:
        return value
    return _parse_time_text_ms(row.get("created"))


def _decision_bool(row: dict[str, Any], field: str) -> bool:
    return _truthy(_row_or_extra(row, field))


def _build_decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_key": _text(decision.get("decision_key")),
        "decision": _text(decision.get("decision")),
        "code": _decision_reason_code(decision),
        "text": _text(decision.get("reason_text")) or _decision_reason_code(decision),
        "source": _decision_source(decision),
        "created": _text(decision.get("created")),
        "created_ms": _decision_created_ms(decision),
        "rank": _to_int(decision.get("rank"), 0),
        "active_gate_passed": _decision_bool(decision, "active_gate_passed"),
        "context_gate_passed": _decision_bool(decision, "context_gate_passed"),
        "related_target_id": _text(decision.get("related_target_id")),
        "metrics": _decision_object(decision, "metrics"),
        "thresholds": _decision_object(decision, "thresholds"),
        "rejection_examples": _decision_rejection_examples(decision),
    }


def _build_not_selected_reasons(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _build_decision_summary(decision)
        for decision in decisions
        if _lower(decision.get("decision")) in NEGATIVE_DECISIONS
    ]


def _latest_decision_with_values(decisions: list[dict[str, Any]], values: set[str]) -> dict[str, Any]:
    for decision in reversed(decisions):
        if _lower(decision.get("decision")) in values:
            return decision
    return {}


def _positive_selection_decision(target: dict[str, Any] | None, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    positive = _latest_decision_with_values(decisions, POSITIVE_DECISIONS)
    if positive:
        return positive
    return decisions[-1] if target and decisions else {}


def _positive_selection_reason(target: dict[str, Any] | None, positive_decision: dict[str, Any]) -> str:
    target_extra = _as_object((target or {}).get("extra"))
    active_summary = _as_object(target_extra.get("active_reason_summary"))
    return (
        _text((target or {}).get("scan_reason"))
        or _text(active_summary.get("scan_reason"))
        or _decision_reason(positive_decision)
    )


def _build_selection_summary(
    *,
    target: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    selection_decision: str,
    selection_reason: str,
) -> dict[str, Any]:
    latest_decision = decisions[-1] if decisions else {}
    positive_decision = _positive_selection_decision(target, decisions)
    target_extra = _as_object((target or {}).get("extra"))
    decision_counts = Counter(_lower(row.get("decision")) or "unknown" for row in decisions)
    return {
        "selected": bool(target) or _lower(selection_decision) in POSITIVE_DECISIONS,
        "decision": selection_decision,
        "latest_decision": _text(latest_decision.get("decision")),
        "reason_code": _decision_reason_code(latest_decision),
        "reason_text": selection_reason,
        "selected_reason": selection_reason,
        "positive_decision": _text(positive_decision.get("decision")),
        "positive_reason_code": _decision_reason_code(positive_decision),
        "source": _decision_source(latest_decision) or _text(target_extra.get("source")),
        "rank": _to_int(latest_decision.get("rank"), 0),
        "created": _text(latest_decision.get("created")) or _text((target or {}).get("created")),
        "created_ms": _decision_created_ms(latest_decision) or _event_time_ms(target or {}),
        "decision_count": len(decisions),
        "selected_decision_count": int(decision_counts.get("selected", 0)),
        "not_selected_decision_count": sum(int(decision_counts.get(key, 0)) for key in ("rejected", "not_selected", "deferred", "error")),
        "active_gate_passed": _decision_bool(latest_decision, "active_gate_passed") or _truthy(target_extra.get("active_gate_passed")),
        "context_gate_passed": _decision_bool(latest_decision, "context_gate_passed") or _truthy(target_extra.get("context_gate_passed")),
        "related_target_id": _text(latest_decision.get("related_target_id")) or _text((target or {}).get("id")),
        "target_status": _text((target or {}).get("status")),
    }


def _build_selection_diagnostics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    latest_decision = decisions[-1] if decisions else {}
    decision_counts = Counter(_lower(row.get("decision")) or "unknown" for row in decisions)
    sources = Counter(_decision_source(row) or "unknown" for row in decisions)
    rejection_examples: list[Any] = []
    for decision in decisions:
        for example in _decision_rejection_examples(decision):
            if len(rejection_examples) >= 20:
                break
            rejection_examples.append(example)
        if len(rejection_examples) >= 20:
            break
    return {
        "decision_counts": dict(decision_counts),
        "sources": dict(sources),
        "latest_decision_key": _text(latest_decision.get("decision_key")),
        "metrics": _decision_object(latest_decision, "metrics"),
        "thresholds": _decision_object(latest_decision, "thresholds"),
        "rejection_examples": rejection_examples,
        "decisions": [_build_decision_summary(decision) for decision in decisions],
    }


def _legacy_rejection_coverage(today_targets: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    daily_scan = today_targets.get("daily_scan") if isinstance(today_targets.get("daily_scan"), dict) else {}
    scan_result = daily_scan.get("result") if isinstance(daily_scan.get("result"), dict) else {}
    rejection_summary = _as_object(scan_result.get("rejection_summary"))
    rejection_examples = _as_list(scan_result.get("rejection_examples"))
    coverage = {
        "source": "today_targets.daily_scan.result",
        "summary_exists": bool(rejection_summary),
        "examples_exists": bool(rejection_examples),
        "summary_count": sum(_to_int(value, 0) for value in rejection_summary.values()),
        "example_count": len(rejection_examples),
        "rejection_summary": rejection_summary,
        "rejection_examples": rejection_examples[:20],
    }
    return coverage, rejection_summary, rejection_examples


def _decision_key(symbol: str, source: str, stage: str) -> str:
    return "|".join(part for part in (source, stage, symbol) if part)


def _target_decision_group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = _upper(row.get("symbol"))
        if symbol:
            grouped[symbol].append(row)
    for symbol in grouped:
        grouped[symbol].sort(key=lambda item: (_event_time_ms(item), _text(item.get("created"))))
    return grouped


def _orders_by_symbol(orders: list[dict[str, Any]], signals_by_id: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        symbol = _upper(order.get("symbol"))
        if not symbol:
            signal = signals_by_id.get(_text(order.get("signal_id")))
            symbol = _upper((signal or {}).get("symbol"))
        if symbol:
            grouped[symbol].append(order)
    for symbol in grouped:
        grouped[symbol].sort(key=lambda item: (_event_time_ms(item), _event_time_text(item), _text(item.get("id"))))
    return grouped


def _summarize_orders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    role_counts = Counter(_role(row) for row in rows)
    status_counts = Counter(_lower(row.get("status")) or "unknown" for row in rows)
    entry_filled = sum(1 for row in rows if _role(row) in ENTRY_ROLES and _is_filled(row))
    exit_filled = sum(1 for row in rows if _role(row) in EXIT_ROLES and _role(row) not in ENTRY_ROLES and _is_filled(row))
    protection_orders = sum(1 for row in rows if _role(row) in PROTECTION_ROLES)
    latest = rows[-1] if rows else {}
    return {
        "total": len(rows),
        "entry_filled": entry_filled,
        "exit_filled": exit_filled,
        "protection_orders": protection_orders,
        "role_counts": dict(role_counts),
        "status_counts": dict(status_counts),
        "latest_status": _text(latest.get("status")),
        "latest_role": _role(latest) if latest else "",
    }


def _build_issue_flags(
    *,
    symbol: str,
    target: dict[str, Any] | None,
    signals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    broker_mode: str,
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    order_summary = _summarize_orders(orders)
    has_orders = bool(orders)
    rejected_signals = [signal for signal in signals if _signal_status(signal) in {"rejected", "expired", "cancelled", "canceled"}]
    if rejected_signals and has_orders:
        flags.append({"code": "rejected_signal_has_orders", "severity": "high", "message": "signal rejected/expired but orders exist"})
    if order_summary["entry_filled"] and not order_summary["protection_orders"] and not order_summary["exit_filled"]:
        flags.append({"code": "entry_filled_without_protection", "severity": "high", "message": "entry filled without TP/SL protection"})
    for order in orders:
        if _role(order) == "close" and _is_filled(order):
            if not _infer_related_order_reason(order, orders):
                flags.append({"code": "close_missing_reason", "severity": "medium", "message": "close order filled without close reason"})
                break
    for signal in signals:
        extra = _as_object(signal.get("extra"))
        effective_broker = _lower(signal.get("effective_broker_mode") or extra.get("effective_broker_mode"))
        if effective_broker and effective_broker != _lower(broker_mode):
            flags.append({"code": "broker_mode_mismatch", "severity": "medium", "message": f"signal broker mode {effective_broker} differs from review {broker_mode}"})
            break
    if target:
        extra = _as_object(target.get("extra"))
        source = _lower(extra.get("source"))
        stored_status = _lower(target.get("stored_target_status") or target.get("raw_status") or target.get("status"))
        status = _lower(target.get("target_status") or target.get("status"))
        if stored_status == "active" and status == "candidate" and source in {"intraday_window_admission", "daily_scan"}:
            flags.append({"code": "active_status_policy_mismatch", "severity": "high", "message": "active target was downgraded to candidate by status policy"})
    if not signals and order_summary["entry_filled"]:
        flags.append({"code": "entry_without_signal", "severity": "high", "message": "entry filled but no same-day signal was found"})
    return flags


def _target_item_by_symbol(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_upper(item.get("symbol")): dict(item) for item in items if isinstance(item, dict) and _upper(item.get("symbol"))}


def _event_sort_ms(event: dict[str, Any]) -> int:
    direct = _to_int(event.get("ts_ms"), 0)
    if direct > 0:
        return direct
    for field in ("time", "us_time", "created", "updated", "trade_time", "fill_time"):
        parsed = _parse_time_text_ms(event.get(field))
        if parsed > 0:
            return parsed
    return 0


def _build_events(
    *,
    decisions: list[dict[str, Any]],
    target: dict[str, Any] | None,
    signals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for decision in decisions:
        events.append(
            {
                "type": "target_decision",
                "lane": "selection",
                "status": _text(decision.get("decision")),
                "reason": _decision_reason(decision),
                "ts_ms": _event_time_ms(decision),
                "time": _event_time_text(decision),
                "source": _text(decision.get("source")),
            }
        )
    if target:
        target_extra = _as_object(target.get("extra"))
        active_summary = target_extra.get("active_reason_summary")
        active_summary = active_summary if isinstance(active_summary, dict) else {}
        events.append(
            {
                "type": "target_selected",
                "lane": "selection",
                "status": _text(target.get("status")),
                "reason": _text(target.get("scan_reason")) or _text(active_summary.get("scan_reason")),
                "ts_ms": _event_time_ms(target),
                "time": _event_time_text(target),
                "source": _text(target_extra.get("source")),
            }
        )
    for signal in signals:
        events.append(
            {
                "type": "signal",
                "lane": "signal",
                "status": _signal_status(signal),
                "reason": _signal_reason(signal),
                "ts_ms": _event_time_ms(signal),
                "time": _event_time_text(signal),
                "signal_id": _text(signal.get("signal_id")),
                "direction": _text(signal.get("direction")),
            }
        )
    for order in orders:
        role = _role(order)
        lane = "execution"
        if role in PROTECTION_ROLES:
            lane = "protection"
        elif role in EXIT_ROLES and role != "entry":
            lane = "exit"
        events.append(
            {
                "type": "order",
                "lane": lane,
                "status": _text(order.get("status")),
                "reason": _infer_related_order_reason(order, orders),
                "role": role,
                "ts_ms": _event_time_ms(order),
                "time": _event_time_text(order),
                "order_id": _text(order.get("order_id") or order.get("broker_order_id") or order.get("id")),
                "signal_id": _text(order.get("signal_id")),
            }
        )
    events.sort(key=lambda item: (_event_sort_ms(item), _text(item.get("time")), _text(item.get("type"))))
    return events


def _target_is_active(target: dict[str, Any] | None) -> bool:
    if not target:
        return False
    target_extra = _as_object(target.get("extra"))
    status = _lower(target.get("target_status") or target.get("status"))
    if status:
        return status == "active"
    return _truthy(target_extra.get("active_gate_passed")) or _truthy(target_extra.get("context_gate_passed"))


def _infer_lifecycle_status(*, target: dict[str, Any] | None, decisions: list[dict[str, Any]], signals: list[dict[str, Any]], orders: list[dict[str, Any]]) -> str:
    order_summary = _summarize_orders(orders)
    if order_summary["exit_filled"]:
        return "closed"
    if order_summary["entry_filled"]:
        return "open"
    if signals:
        return "signaled"
    if _target_is_active(target):
        return "active"
    if target:
        return "selected"
    decision_values = {_lower(row.get("decision")) for row in decisions}
    if decision_values & NEGATIVE_DECISIONS:
        return "not_selected"
    return "unknown"


def _build_tab_flags(lifecycle_status: str, issues: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    if lifecycle_status == "active":
        flags.extend(["active", "selected"])
    elif lifecycle_status in {"selected", "signaled"}:
        flags.append("selected")
    elif lifecycle_status in {"open", "closed"}:
        flags.append("traded")
    elif lifecycle_status == "not_selected":
        flags.append("not_selected")
    if issues:
        flags.append("problem")
    return flags


def _matches_status_filter(row: dict[str, Any], status_filter: str) -> bool:
    if not status_filter or status_filter == "all":
        return True
    status = _lower(row.get("lifecycle_status") or row.get("review_status"))
    flags = set(_as_list(row.get("tab_flags")))
    if status_filter == status or status_filter in flags:
        return True
    if status_filter == "traded":
        return bool((row.get("order_summary") or {}).get("entry_filled")) or status in {"open", "closed"}
    if status_filter == "selected":
        return "selected" in flags or bool(row.get("target")) or _lower(row.get("selection_decision")) in POSITIVE_DECISIONS
    if status_filter == "not_selected":
        return "not_selected" in flags or status == "not_selected"
    if status_filter == "problem":
        return bool(row.get("issue_flags"))
    return True


def build_daily_trade_review_response(
    pb: Any,
    *,
    params: dict[str, Any] | None = None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    request_params = params if isinstance(params, dict) else {}
    fallback_date = _text((time_strings() or {}).get("date")) or datetime.utcnow().strftime("%Y-%m-%d")
    market_date = _date_token(request_params.get("market_date") or request_params.get("date"), fallback_date)
    next_date = _next_date_token(market_date)
    broker_mode = normalize_environment(request_params.get("broker_mode") or request_params.get("environment"), "paper")
    data_environment = normalize_environment(
        request_params.get("data_environment") or request_params.get("market_data_mode") or request_params.get("environment"),
        "live",
    )
    symbol_filter = _upper(request_params.get("symbol"))
    status_filter = _lower(request_params.get("status") or "all")
    include_events = _truthy(request_params.get("include_events"))
    include_lifecycle = _truthy(request_params.get("include_lifecycle"))
    limit = max(1, min(_to_int(request_params.get("limit"), 300), 500))

    warnings: list[dict[str, Any]] = []
    date_escaped = escape_filter_string(market_date)
    next_date_escaped = escape_filter_string(next_date)
    data_escaped = escape_filter_string(data_environment)
    broker_escaped = escape_filter_string(broker_mode)

    signal_filter = (
        f'environment = "{data_escaped}" && '
        f'(date = "{date_escaped}" || (us_time >= "{date_escaped} 00:00:00" && us_time < "{next_date_escaped} 00:00:00"))'
    )
    if symbol_filter:
        signal_filter += f' && symbol = "{escape_filter_string(symbol_filter)}"'
    signals, error = _load_records(pb, SIGNALS_COLLECTION, filter_expr=signal_filter, sort="us_time", max_pages=30)
    if error:
        warnings.append({"code": "signals_read_failed", "message": error})
    signals_by_id = {_text(row.get("signal_id")): row for row in signals if _text(row.get("signal_id"))}
    signal_ids = list(signals_by_id.keys())

    orders: list[dict[str, Any]] = []
    if signal_ids:
        for index in range(0, len(signal_ids), 25):
            chunk = signal_ids[index : index + 25]
            id_filter = " || ".join(f'signal_id = "{escape_filter_string(signal_id)}"' for signal_id in chunk)
            order_filter = f'environment = "{broker_escaped}" && ({id_filter})'
            batch, order_error = _load_records(pb, ORDERS_COLLECTION, filter_expr=order_filter, sort="us_time", max_pages=20)
            orders.extend(batch)
            if order_error:
                warnings.append({"code": "orders_read_failed", "message": order_error})
    day_order_filter = (
        f'environment = "{broker_escaped}" && '
        f'((us_time >= "{date_escaped} 00:00:00" && us_time < "{next_date_escaped} 00:00:00") || '
        f'(created >= "{date_escaped} 00:00:00" && created < "{next_date_escaped} 00:00:00"))'
    )
    if symbol_filter:
        day_order_filter += f' && symbol = "{escape_filter_string(symbol_filter)}"'
    day_orders, day_order_error = _load_records(pb, ORDERS_COLLECTION, filter_expr=day_order_filter, sort="us_time", max_pages=20)
    if day_order_error:
        warnings.append({"code": "day_orders_read_failed", "message": day_order_error})
    seen_orders: set[str] = set()
    merged_orders: list[dict[str, Any]] = []
    for order in [*orders, *day_orders]:
        key = "|".join(
            [
                _text(order.get("id")),
                _text(order.get("order_id") or order.get("broker_order_id")),
                _text(order.get("unique_id")),
                _text(order.get("signal_id")),
                _text(order.get("role")),
                _text(order.get("us_time") or order.get("created")),
            ]
        )
        if key in seen_orders:
            continue
        seen_orders.add(key)
        merged_orders.append(order)
    orders = merged_orders

    decision_filter = f'environment = "{data_escaped}" && market_date = "{date_escaped}"'
    if symbol_filter:
        decision_filter += f' && symbol = "{escape_filter_string(symbol_filter)}"'
    decisions, decision_error = _load_records(pb, TARGET_DECISIONS_COLLECTION, filter_expr=decision_filter, sort="created", max_pages=30)
    if decision_error:
        warnings.append({"code": "target_decisions_unavailable", "message": decision_error})

    try:
        from ibkr_api.universe.today_targets import build_today_targets_response

        today_targets, today_status = build_today_targets_response(
            pb,
            payload={
                "market_date": market_date,
                "broker_mode": broker_mode,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
                "paginate": False,
            },
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )
        if today_status != 200 or not today_targets.get("ok"):
            warnings.append({"code": "today_targets_unavailable", "message": _text(today_targets.get("error") or today_status)})
            today_targets = {"items": [], "summary": {}}
    except Exception as exc:
        today_targets = {"items": [], "summary": {}}
        warnings.append({"code": "today_targets_unavailable", "message": str(exc)})

    daily_signals, stats_status = build_daily_signal_analytics_response(
        pb,
        params={"date": market_date, "broker_mode": broker_mode, "data_environment": data_environment},
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
        time_strings=time_strings,
    )
    if stats_status != 200 or not daily_signals.get("ok"):
        warnings.append({"code": "daily_signals_unavailable", "message": _text(daily_signals.get("error") or stats_status)})
        daily_signals = {"summary": {}, "setup_rows": [], "realized_trades": []}

    targets_by_symbol = _target_item_by_symbol(today_targets.get("items") or [])
    decisions_by_symbol = _target_decision_group(decisions)
    signals_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        symbol = _upper(signal.get("symbol"))
        if symbol:
            signals_by_symbol[symbol].append(signal)
    for symbol in signals_by_symbol:
        signals_by_symbol[symbol].sort(key=lambda item: (_event_time_ms(item), _event_time_text(item)))
    orders_grouped = _orders_by_symbol(orders, signals_by_id)

    realized_by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {"closed_trades": 0, "net_pnl": 0.0})
    for trade in daily_signals.get("realized_trades") or []:
        if not isinstance(trade, dict):
            continue
        symbol = _upper(trade.get("symbol"))
        if not symbol:
            continue
        bucket = realized_by_symbol[symbol]
        bucket["closed_trades"] += 1
        bucket["net_pnl"] = round(float(bucket.get("net_pnl") or 0.0) + _to_float(trade.get("net_pnl")), 6)

    symbol_set = set(targets_by_symbol) | set(decisions_by_symbol) | set(signals_by_symbol) | set(orders_grouped) | set(realized_by_symbol)
    if symbol_filter:
        symbol_set = {symbol for symbol in symbol_set if symbol == symbol_filter}

    items: list[dict[str, Any]] = []
    for symbol in sorted(symbol_set):
        target = targets_by_symbol.get(symbol)
        symbol_decisions = decisions_by_symbol.get(symbol, [])
        symbol_signals = signals_by_symbol.get(symbol, [])
        symbol_orders = orders_grouped.get(symbol, [])
        latest_decision = symbol_decisions[-1] if symbol_decisions else {}
        positive_decision = _positive_selection_decision(target, symbol_decisions)
        issues = _build_issue_flags(
            symbol=symbol,
            target=target,
            signals=symbol_signals,
            orders=symbol_orders,
            broker_mode=broker_mode,
        )
        order_summary = _summarize_orders(symbol_orders)
        if target:
            selection_decision = _text(positive_decision.get("decision")) or "selected"
        else:
            selection_decision = _text(latest_decision.get("decision"))
        selection_reason = _positive_selection_reason(target, positive_decision)
        lifecycle_status = _infer_lifecycle_status(
            target=target,
            decisions=symbol_decisions,
            signals=symbol_signals,
            orders=symbol_orders,
        )
        tab_flags = _build_tab_flags(lifecycle_status, issues)
        row = {
            "symbol": symbol,
            "review_status": lifecycle_status,
            "lifecycle_status": lifecycle_status,
            "tab_flags": tab_flags,
            "has_problem": bool(issues),
            "selection_decision": selection_decision,
            "selection_reason": selection_reason,
            "selected_reason": selection_reason,
            "selection_summary": _build_selection_summary(
                target=target,
                decisions=symbol_decisions,
                selection_decision=selection_decision,
                selection_reason=selection_reason,
            ),
            "selection_diagnostics": _build_selection_diagnostics(symbol_decisions),
            "not_selected_reasons": _build_not_selected_reasons(symbol_decisions),
            "target": target or None,
            "signal_summary": _build_signal_summary(symbol_signals),
            "order_summary": order_summary,
            "realized": dict(realized_by_symbol.get(symbol) or {"closed_trades": 0, "net_pnl": 0.0}),
            "issue_flags": issues,
            "lifecycle_url": f"/ibkr_lifecycle_flow.html?symbol={symbol}&date={market_date}&broker_mode={broker_mode}&market_data_mode={data_environment}",
            "decision_count": len(symbol_decisions),
        }
        if include_events:
            row["events"] = _build_events(decisions=symbol_decisions, target=target, signals=symbol_signals, orders=symbol_orders)
        if include_lifecycle and symbol:
            try:
                from ibkr_api.universe.lifecycle_flow import build_lifecycle_flow_response

                lifecycle, lifecycle_status = build_lifecycle_flow_response(
                    pb,
                    payload={
                        "symbol": symbol,
                        "market_date": market_date,
                        "broker_mode": broker_mode,
                        "market_data_mode": data_environment,
                        "data_environment": data_environment,
                    },
                    normalize_environment=normalize_environment,
                    time_strings=time_strings,
                )
                row["lifecycle"] = lifecycle if lifecycle_status == 200 else {"ok": False, "status": lifecycle_status}
            except Exception as exc:
                row["lifecycle"] = {"ok": False, "error": str(exc)}
        if _matches_status_filter(row, status_filter):
            items.append(row)

    items.sort(key=lambda item: (0 if item.get("issue_flags") else 1, item.get("review_status") != "not_selected", item.get("symbol")))
    matched_items = list(items)
    truncated = len(matched_items) > limit
    items = matched_items[:limit]

    status_counts = Counter(_text(row.get("lifecycle_status") or row.get("review_status")) or "unknown" for row in matched_items)
    tab_counts = Counter(flag for row in matched_items for flag in _as_list(row.get("tab_flags")))
    tab_counts["all"] = len(matched_items)
    issue_count = int(tab_counts.get("problem", 0))
    selected_count = int(tab_counts.get("selected", 0))
    not_selected_count = int(tab_counts.get("not_selected", 0))
    traded_count = int(tab_counts.get("traded", 0))

    legacy_rejection_coverage, legacy_rejection_summary, legacy_rejection_examples = _legacy_rejection_coverage(today_targets)
    coverage = {
        "target_decisions": {
            "collection": TARGET_DECISIONS_COLLECTION,
            "available": not bool(decision_error),
            "exists": bool(decisions),
            "present": bool(decisions),
            "rows": len(decisions),
            "error": decision_error,
        },
        "legacy_rejections": legacy_rejection_coverage,
        "using_legacy_rejections_only": not decisions and bool(legacy_rejection_summary or legacy_rejection_examples),
    }

    if not decisions:
        if legacy_rejection_summary or legacy_rejection_examples:
            warnings.append(
                {
                    "code": "legacy_rejection_summary_only",
                    "message": "target decision ledger is empty; showing legacy rejection summary/examples only for old scans",
                    "rejection_summary": legacy_rejection_summary,
                    "rejection_examples": legacy_rejection_examples,
                }
            )

    return {
        "ok": True,
        "source": "ibkr-api",
        "market_date": market_date,
        "date": market_date,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "filters": {
            "symbol": symbol_filter,
            "status": status_filter,
            "include_events": include_events,
            "include_lifecycle": include_lifecycle,
            "limit": limit,
        },
        "summary": {
            "symbols": len(symbol_set),
            "returned": len(items),
            "matched": len(matched_items),
            "truncated": truncated,
            "selected_count": selected_count,
            "not_selected_count": not_selected_count,
            "traded_count": traded_count,
            "issue_count": issue_count,
            "decision_rows": len(decisions),
            "signal_count": len(signals),
            "order_count": len(orders),
            "status_counts": dict(status_counts),
            "tab_counts": dict(tab_counts),
            "today_targets": today_targets.get("summary") or {},
            "daily_signals": daily_signals.get("summary") or {},
        },
        "coverage": coverage,
        "integrations": [
            {"id": "lifecycle_flow", "endpoint": "/api/custom/ibkr/lifecycle-flow", "role": "per-symbol lifecycle drilldown"},
            {"id": "daily_signals", "endpoint": "/api/custom/ibkr/analytics/daily-signals", "role": "rule/RR/PnL summary"},
            {"id": "today_targets", "endpoint": "/api/custom/ibkr/today-targets", "role": "selected target reasons"},
            {"id": "active_window_progress", "endpoint": "/api/custom/ibkr/active-window-progress", "role": "signal readiness and blockers"},
            {"id": "target_decisions", "collection": TARGET_DECISIONS_COLLECTION, "role": "full selected/not-selected ledger"},
        ],
        "setup_rows": daily_signals.get("setup_rows") or [],
        "realized_trades": daily_signals.get("realized_trades") or [],
        "warnings": warnings,
        "items": items,
    }, 200


__all__ = ["TARGET_DECISIONS_COLLECTION", "build_daily_trade_review_response"]
