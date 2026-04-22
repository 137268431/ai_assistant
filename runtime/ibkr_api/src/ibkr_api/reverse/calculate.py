from __future__ import annotations

from typing import Any, Callable

from ibkr_api.reverse.common import (
    ALLOWED_REVERSE_ACTION_TYPES,
    DEFAULT_REVERSE_PRIORITY,
    build_indicator_analysis,
    build_order_context,
    load_latest_indicator_record,
    load_reverse_signal_threshold,
    find_latest_active_entry_order,
    map_strength,
    normalize_environment_value,
    normalize_reverse_record,
    parse_triggered_signals,
    record_value,
    resolve_indicator_action,
    to_float,
    to_text,
    upsert_reverse_record,
)


def _build_analysis_payload(
    *,
    symbol: str,
    direction: str,
    target_state: str,
    strength: str,
    score: float,
    action_type: str,
    triggered_signals: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": direction,
        "source": "indicator",
        "target_state": target_state,
        "strength": strength,
        "score": score,
        "action_type": action_type,
        "triggered_signals": list(triggered_signals),
    }


def _build_no_target_response(
    *,
    symbol: str,
    direction: str,
    forced_action_type: str,
) -> tuple[dict[str, Any], int]:
    return {
        "success": True,
        "created": False,
        "reason": "no_conflict_target",
        "signal": None,
        "analysis": _build_analysis_payload(
            symbol=symbol,
            direction=direction,
            target_state="",
            strength="weak",
            score=0,
            action_type=forced_action_type or "cancel",
            triggered_signals=[],
        ),
    }, 200


def _build_no_conditions_response(
    *,
    symbol: str,
    direction: str,
    order_context: dict[str, Any],
    strength: str,
    score: float,
    action_type: str,
    triggered_signals: list[str],
) -> tuple[dict[str, Any], int]:
    return {
        "success": True,
        "created": False,
        "reason": "no_reverse_conditions",
        "signal": None,
        "analysis": _build_analysis_payload(
            symbol=symbol,
            direction=direction,
            target_state=to_text(order_context.get("target_state")),
            strength=strength,
            score=score,
            action_type=action_type,
            triggered_signals=triggered_signals,
        ),
    }, 200


def _build_reverse_response(record: Any, created: bool) -> tuple[dict[str, Any], int]:
    return {
        "success": True,
        "created": bool(created),
        "duplicate": not bool(created),
        "signal": normalize_reverse_record(record),
    }, 200


def _resolve_score_override(value: Any) -> float | None:
    if value is None or value == "":
        return None
    parsed = to_float(value)
    return parsed


def _build_effective_triggered_signals(
    payload: dict[str, Any],
    analysis: dict[str, Any],
    forced_action_type: str,
) -> list[str]:
    explicit = parse_triggered_signals(payload.get("triggered_signals"))
    if explicit:
        return explicit
    derived = [to_text(item) for item in analysis.get("triggered_signals") or [] if to_text(item)]
    if derived:
        return derived
    return [forced_action_type] if forced_action_type else []


def _build_extra_data(
    *,
    payload: dict[str, Any],
    analysis: dict[str, Any],
    order_context: dict[str, Any],
    direction: str,
    forced_action_type: str,
) -> dict[str, Any]:
    return {
        **dict(analysis.get("indicator_extra") or {}),
        "current_direction": direction,
        "reverse_kind": "indicator_conflict",
        "target_state": order_context.get("target_state") or "",
        "order_status": order_context.get("order_status") or "",
        "relation_status": order_context.get("relation_status") or "",
        "position_side": order_context.get("position_side") or "",
        "origin_signal_id": to_text(payload.get("origin_signal_id") or payload.get("signal_id") or ""),
        "signal_id": order_context.get("signal_id") or "",
        "order_unique_id": order_context.get("order_unique_id") or "",
        "broker_order_id": order_context.get("broker_order_id") or "",
        "order_id": order_context.get("broker_order_id") or "",
        "trade_group_id": order_context.get("trade_group_id") or "",
        "entry_order_unique_id": order_context.get("entry_order_unique_id") or "",
        "entry_price": order_context.get("entry_price") or analysis.get("close") or 0,
        "quantity": order_context.get("quantity") or 0,
        "take_profit": order_context.get("take_profit") or 0,
        "stop_loss": order_context.get("stop_loss") or 0,
        "crsi": analysis.get("crsi"),
        "obv_rsi": analysis.get("obv_rsi"),
        "vwap_dist": analysis.get("vwap_dist"),
        "close": analysis.get("close"),
        "manual_override": bool(forced_action_type),
    }


def _notify_if_threshold_met(
    *,
    threshold: int,
    score: float,
    created: bool,
    record: Any,
    notify_reverse_signal: Callable[[Any, dict[str, Any]], Any] | None,
) -> None:
    if score < threshold or not callable(notify_reverse_signal):
        return
    notify_reverse_signal(
        record,
        {
            "message": "检测到指标反转信号，等待 IBKR 执行" if created else "检测到重复指标反转信号，已刷新现有记录",
        },
    )


def build_reverse_calculate_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    escape_filter: Callable[[Any], str] | None = None,
    active_order_loader: Callable[[Any, str, str, str], Any] = find_latest_active_entry_order,
    order_context_builder: Callable[[Any], dict[str, Any] | None] = build_order_context,
    indicator_loader: Callable[[Any, str, str], Any] = load_latest_indicator_record,
    indicator_analyzer: Callable[[str, Any], dict[str, Any]] = build_indicator_analysis,
    reverse_upsert_builder: Callable[[Any, dict[str, Any]], dict[str, Any]] = upsert_reverse_record,
    threshold_loader: Callable[[Any, str], int] = load_reverse_signal_threshold,
    notify_reverse_signal: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    data = dict(payload or {})
    environment = normalize_environment_value(data.get("environment"))
    symbol = to_text(data.get("symbol")).upper()
    direction = to_text(data.get("direction")).lower()
    forced_action_type = to_text(data.get("force_action_type") or data.get("action_type")).lower()

    if not symbol or not direction:
        return {"error": "Missing symbol or direction"}, 400
    if direction not in {"long", "short"}:
        return {"error": "Invalid direction"}, 400
    if forced_action_type and forced_action_type not in ALLOWED_REVERSE_ACTION_TYPES:
        return {"error": f"Invalid force_action_type: {forced_action_type}"}, 400

    try:
        if escape_filter is None:
            active_order = active_order_loader(pb, symbol, direction, environment)
        else:
            active_order = active_order_loader(pb, symbol, direction, environment, escape_filter=escape_filter)
        order_context = order_context_builder(active_order)

        if not order_context or (order_context.get("direction") and order_context.get("direction") != direction):
            return _build_no_target_response(
                symbol=symbol,
                direction=direction,
                forced_action_type=forced_action_type,
            )

        if forced_action_type in {"adjust_sl", "adjust_tp", "close"} and order_context.get("target_state") != "filled_position":
            return {"error": f"Action {forced_action_type} requires filled_position"}, 400
        if forced_action_type == "cancel" and order_context.get("target_state") != "pending_entry":
            return {"error": "Action cancel requires pending_entry"}, 400

        if escape_filter is None:
            indicator_record = indicator_loader(pb, symbol, environment)
        else:
            indicator_record = indicator_loader(pb, symbol, environment, escape_filter=escape_filter)
        if not indicator_record:
            return {"error": "No ibkr_indicators found for symbol"}, 404

        analysis = indicator_analyzer(direction, indicator_record)
        score_override = _resolve_score_override(data.get("score_override"))
        score = analysis.get("score") if score_override is None else score_override
        score = float(score or 0)
        effective_triggered_signals = _build_effective_triggered_signals(data, analysis, forced_action_type)
        strength = map_strength(score)
        action_type = resolve_indicator_action(score, order_context.get("target_state"), forced_action_type)

        if (not forced_action_type and score <= 0) or not effective_triggered_signals:
            return _build_no_conditions_response(
                symbol=symbol,
                direction=direction,
                order_context=order_context,
                strength=strength,
                score=score,
                action_type=action_type,
                triggered_signals=effective_triggered_signals,
            )

        extra_data = _build_extra_data(
            payload=data,
            analysis=analysis,
            order_context=order_context,
            direction=direction,
            forced_action_type=forced_action_type,
        )
        upsert_payload = {
            "environment": environment,
            "symbol": symbol,
            "direction": direction,
            "source": "indicator",
            "priority": data.get("priority") if data.get("priority") is not None else DEFAULT_REVERSE_PRIORITY,
            "strength": strength,
            "score": score,
            "triggered_signals": effective_triggered_signals,
            "action_type": action_type,
            "status": "pending",
            "extra": extra_data,
            "bar_time_ms": record_value(indicator_record, "bar_time_ms"),
            "us_time": record_value(indicator_record, "us_time") or "",
            "cn_time": record_value(indicator_record, "cn_time") or "",
        }

        if escape_filter is None:
            upsert_result = reverse_upsert_builder(pb, upsert_payload)
        else:
            upsert_result = reverse_upsert_builder(pb, upsert_payload, escape_filter=escape_filter)
        threshold = int(threshold_loader(pb, environment))
        _notify_if_threshold_met(
            threshold=threshold,
            score=score,
            created=bool(upsert_result.get("created")),
            record=upsert_result.get("record"),
            notify_reverse_signal=notify_reverse_signal,
        )
        return _build_reverse_response(upsert_result.get("record"), bool(upsert_result.get("created")))
    except Exception as exc:
        return {"error": str(exc)}, 500


__all__ = ["build_reverse_calculate_response"]
