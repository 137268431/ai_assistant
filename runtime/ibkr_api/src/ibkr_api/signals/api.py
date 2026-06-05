from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.upsert import build_order_upsert_response
from ibkr_api.orders.values import ensure_object, first_defined, to_float
from ibkr_api.signals.ack import build_signal_ack_orders
from ibkr_api.signals.notifications import sync_signal_status_notification


_BROKER_SIGNAL_FINAL_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "cancelled",
    "canceled",
    "rejected",
    "expired",
    "blocked",
    "duplicate_existing_broker_order",
    "validation_rejected",
    "submit_failed",
    "protection_incomplete",
    "protection_reprice_failed",
    "entry_missed_limit_cap",
    "ignored_no_broker_position",
    "stale_signal",
    "signal_clock_skew",
    "stale_signal/signal_clock_skew",
    "executed",
    "closed",
}

_SIGNAL_ACK_DISPLAY_EXTRA_KEYS = (
    "tv_reference_entry",
    "tv_reference_entry_price",
    "tv_reference_stop_loss",
    "tv_reference_sl",
    "tv_reference_take_profit",
    "tv_reference_tp",
    "reference_entry",
    "reference_entry_price",
    "reference_stop_loss",
    "reference_sl",
    "reference_take_profit",
    "reference_tp",
    "original_entry",
    "original_stop_loss",
    "original_take_profit",
    "initial_entry",
    "initial_stop_loss",
    "initial_take_profit",
    "tv_reference",
    "tv_reference_prices",
    "tv_reference_plan",
    "pre_submit_reference_price",
    "pre_submit_reference_source",
    "submitted_entry_limit_price",
    "submitted_limit_cap_price",
    "submitted_limit_price",
    "submitted_price",
    "entry_limit_cap_price",
    "limit_cap_price",
    "bounded_limit_price",
    "submitted_limit_cap_bps",
    "entry_limit_cap_bps",
    "limit_cap_bps",
    "submitted_limit_cap_applied",
    "entry_limit_cap_applied",
    "limit_cap_applied",
    "entry_price_plan",
    "entry_limit_intent",
    "entry_repriced",
    "actual_fill_price",
    "entry_fill_price",
    "executed_price",
    "final_stop_loss",
    "final_sl",
    "final_take_profit",
    "final_tp",
    "protection_final_stop_loss",
    "protection_final_take_profit",
    "protection_rebase_result",
    "protection_reprice_result",
    "entry_slippage_bps",
    "actual_slippage_bps",
    "fill_slippage_bps",
    "slippage_bps",
    "entry_slippage_r",
    "actual_slippage_r",
    "fill_slippage_r",
    "slippage_r",
    "risk_r",
    "initial_risk_r",
    "adaptive_priority",
    "adaptivePriority",
    "ib_algo_adaptive_priority",
    "order_adaptive_priority",
    "adaptive_order_priority",
    "ibkr_adaptive_priority",
    "adaptive",
    "algo",
    "order_algo",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execution_by_mode(extra: dict[str, Any]) -> dict[str, Any]:
    value = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _broker_execution_status(extra: dict[str, Any], broker_mode: str) -> str:
    broker_map = _execution_by_mode(extra).get(broker_mode)
    if not isinstance(broker_map, dict):
        return ""
    return str(broker_map.get("status") or "").strip().lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _lifecycle_status_update_allowed(existing_status: str, incoming_status: str) -> bool:
    existing = str(existing_status or "").strip().lower()
    incoming = str(incoming_status or "").strip().lower()
    if not incoming or incoming == existing:
        return False
    allowed_existing = {
        "submitted",
        "submitted_waiting_fill",
        "filled_repricing_protection",
        "filled_position",
        "protected_active",
        "protection_incomplete",
    }
    allowed_incoming = {
        "submitted_waiting_fill",
        "filled_repricing_protection",
        "filled_position",
        "protected_active",
        "protection_incomplete",
        "protection_reprice_failed",
        "entry_missed_limit_cap",
        "executed",
        "closed",
    }
    return existing in allowed_existing and incoming in allowed_incoming


def _copy_present_fields(target: dict[str, Any], *sources: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target if isinstance(target, dict) else {})
    for key in _SIGNAL_ACK_DISPLAY_EXTRA_KEYS:
        for source in sources:
            if not isinstance(source, dict):
                continue
            value = source.get(key)
            if value not in (None, ""):
                merged[key] = value
                break
    return merged


def _with_signal_ack_display_extra(
    signal_extra: dict[str, Any],
    *,
    order_input: dict[str, Any],
    order_extra: dict[str, Any],
) -> dict[str, Any]:
    merged = _copy_present_fields(signal_extra, order_extra, order_input)
    submitted_limit = first_defined(
        order_extra.get("submitted_entry_limit_price"),
        order_extra.get("submitted_limit_cap_price"),
        order_extra.get("submitted_limit_price"),
        order_extra.get("submitted_price"),
        order_extra.get("entry_limit_cap_price"),
        order_extra.get("limit_cap_price"),
        order_extra.get("bounded_limit_price"),
        order_input.get("submitted_entry_limit_price"),
        order_input.get("submitted_limit_price"),
        order_input.get("limit_price"),
    )
    if submitted_limit not in (None, ""):
        merged["submitted_entry_limit_price"] = submitted_limit

    actual_fill = first_defined(
        order_extra.get("actual_fill_price"),
        order_extra.get("entry_fill_price"),
        order_extra.get("executed_price"),
        order_input.get("actual_fill_price"),
        order_input.get("entry_fill_price"),
        order_input.get("executed_price"),
        order_input.get("fill_price"),
        order_input.get("avg_fill_price"),
        order_input.get("avgPrice"),
    )
    if (to_float(actual_fill) or 0.0) > 0:
        merged["actual_fill_price"] = actual_fill
        merged["entry_fill_price"] = first_defined(merged.get("entry_fill_price"), actual_fill)
        merged["executed_price"] = first_defined(merged.get("executed_price"), actual_fill)
    return merged


def _signal_consumed_for_broker(
    record: dict[str, Any],
    extra: dict[str, Any],
    broker_mode: str,
    data_environment: str,
) -> bool:
    broker_status = _broker_execution_status(extra, broker_mode)
    if broker_status in _BROKER_SIGNAL_FINAL_STATUSES:
        return True
    top_level_status = str((record or {}).get("status") or "pending").strip().lower() or "pending"
    return broker_mode == data_environment == "live" and bool(
        top_level_status and top_level_status not in {"pending", "awaiting_confirm"}
    )


def _effective_pending_status(record: dict[str, Any], extra: dict[str, Any], broker_mode: str, data_environment: str) -> str:
    broker_status = _broker_execution_status(extra, broker_mode)
    if broker_status:
        return broker_status
    top_level_status = str((record or {}).get("status") or "pending").strip().lower() or "pending"
    if broker_mode == data_environment == "live":
        return top_level_status
    return top_level_status if top_level_status in {"pending", "awaiting_confirm"} else ""


def _with_broker_execution(
    extra: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
    order_results: list[dict[str, Any]] | None = None,
    primary_order_status: str = "",
    partial: bool = False,
) -> dict[str, Any]:
    merged = dict(extra if isinstance(extra, dict) else {})
    execution_by_mode = _execution_by_mode(merged)
    broker_payload = execution_by_mode.get(broker_mode)
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode[broker_mode] = {
        **broker_payload,
        "status": status,
        "note": note,
        "status_reason": str(merged.get("status_reason") or note or status).strip() or status,
        "data_environment": data_environment,
        "updated_at": _utc_now_iso(),
        "source": "ibkr-api",
        "ack_partial": bool(partial),
        "primary_order_status": primary_order_status,
        "order_results": list(order_results or []),
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["last_ack_broker_mode"] = broker_mode
    merged["last_ack_data_environment"] = data_environment
    return merged


def _clear_scoped_note_payload(record: dict[str, Any], broker_mode: str) -> dict[str, str]:
    note = str((record or {}).get("note") or "").strip().lower()
    if note.startswith(f"{broker_mode}:") or "history_repair_pending" in note:
        return {"note": ""}
    return {}


def _normalize_indicator_snapshot(
    record: dict[str, Any] | None,
    *,
    as_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(record, dict) or not record.get("id"):
        return None
    extra = as_dict(record.get("extra"))
    return {
        "id": record.get("id"),
        "symbol": record.get("symbol"),
        "exchange": record.get("exchange"),
        "interval": record.get("interval"),
        "script_tag": record.get("script_tag"),
        "us_time": record.get("us_time"),
        "cn_time": record.get("cn_time"),
        "bar_time_ms": record.get("bar_time_ms"),
        "bar_index": record.get("bar_index"),
        "created": record.get("created"),
        "updated": record.get("updated"),
        **extra,
    }


def _find_latest_indicator_snapshot(
    pb: Any,
    signal_record: dict[str, Any],
    environment: str,
    *,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    as_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    symbol = str(signal_record.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    preferred_interval = str(signal_record.get("chart_tf") or signal_record.get("interval") or "").strip()
    signal_bar_time_ms = int(signal_record.get("bar_time_ms") or 0)
    runtime_environment = normalize_environment(environment, "live")
    base_filter = (
        f'(environment = "{escape_filter_string(runtime_environment)}" || environment = "") && '
        f'symbol = "{escape_filter_string(symbol)}"'
    )
    filters: list[str] = []
    if preferred_interval and signal_bar_time_ms > 0:
        filters.append(
            f'{base_filter} && interval = "{escape_filter_string(preferred_interval)}" && bar_time_ms <= {signal_bar_time_ms}'
        )
    if preferred_interval:
        filters.append(f'{base_filter} && interval = "{escape_filter_string(preferred_interval)}"')
    if signal_bar_time_ms > 0:
        filters.append(f"{base_filter} && bar_time_ms <= {signal_bar_time_ms}")
    filters.append(base_filter)

    for filter_expr in filters:
        try:
            record = pb.get_first_record("ibkr_indicators", filter=filter_expr, sort="-bar_time_ms")
        except Exception:
            record = None
        snapshot = _normalize_indicator_snapshot(record if isinstance(record, dict) else None, as_dict=as_dict)
        if snapshot:
            return snapshot
    return None


def _enrich_signal_extra(signal_extra: dict[str, Any], indicator: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(signal_extra if isinstance(signal_extra, dict) else {})
    if not isinstance(indicator, dict):
        return merged
    indicator_keys = (
        "close",
        "day_change_pct",
        "prev_close_change_pct",
        "change_7d",
        "atr",
        "atr_pct",
        "sl_dist_pct",
        "sl_atr_ratio",
        "trend_dir",
        "ema_bullish",
        "ema_bearish",
        "ema_bull_touch",
        "ema_bear_touch",
        "fractal_bull",
        "fractal_bear",
        "crsi",
        "obv_rsi",
        "dtp_dir",
        "dtp_phase",
        "dtp_phase_bars",
        "sd_zone",
        "sd_trend",
        "vwap",
        "vwap_dist",
        "vwap_bullish",
    )
    for key in indicator_keys:
        if merged.get(key) in {None, ""} and indicator.get(key) is not None:
            merged[key] = indicator.get(key)
    merged["latest_indicator_id"] = indicator.get("id") or merged.get("latest_indicator_id") or ""
    return merged


def build_signals_pending_response(
    pb: Any,
    *,
    environment: str,
    data_environment: str | None = None,
    date_str: str,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    as_dict: Callable[[Any], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    broker_mode = request_broker_mode({"broker_mode": environment})
    data_environment = request_market_data_mode({"data_environment": data_environment})
    normalized_date = str(date_str or "").strip()
    if not normalized_date:
        return {"error": "缺少 date 参数"}, 400

    try:
        records = pb.get_records(
            "ibkr_signals",
            filter=(
                f'date = "{escape_filter_string(normalized_date)}" && '
                f'environment = "{escape_filter_string(data_environment)}"'
            ),
            sort="-bar_time_ms",
            per_page=100,
            page=1,
        )
        ibkr_signals = []
        for record in records or []:
            record_extra = as_dict(record.get("extra"))
            if _effective_pending_status(record, record_extra, broker_mode, data_environment) != "pending":
                continue
            if _signal_consumed_for_broker(record, record_extra, broker_mode, data_environment):
                continue
            latest_indicator = _find_latest_indicator_snapshot(
                pb,
                record,
                data_environment,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                as_dict=as_dict,
            )
            extra = _enrich_signal_extra(record_extra, latest_indicator)
            ibkr_signals.append(
                {
                    "id": record.get("id"),
                    "signal_id": record.get("signal_id"),
                    "environment": broker_mode,
                    "broker_mode": broker_mode,
                    "data_environment": record.get("environment") or data_environment,
                    "symbol": record.get("symbol"),
                    "direction": record.get("direction"),
                    "signal": record.get("signal"),
                    "entry": record.get("entry"),
                    "stop_loss": record.get("stop_loss"),
                    "take_profit": record.get("take_profit"),
                    "limit_price": record.get("limit_price"),
                    "shares": record.get("shares"),
                    "rr": record.get("rr"),
                    "reason": record.get("reason"),
                    "exchange": record.get("exchange"),
                    "interval": record.get("interval"),
                    "chart_tf": record.get("chart_tf"),
                    "date": record.get("date"),
                    "us_time": record.get("us_time"),
                    "cn_time": record.get("cn_time"),
                    "bar_time_ms": record.get("bar_time_ms"),
                    "latest_indicator": latest_indicator,
                    "extra": extra,
                    "created": record.get("created"),
                }
            )
        return {
            "ibkr_signals": ibkr_signals,
            "broker_mode": broker_mode,
            "data_environment": data_environment,
        }, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


def build_signals_ack_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    order_upsert_builder: Callable[..., tuple[dict[str, Any], int]] = build_order_upsert_response,
    send_interactive: Callable[..., dict[str, Any]] | None = None,
    update_interactive: Callable[..., dict[str, Any]] | None = None,
    signal_chat_id_fn: Callable[[str], str] | None = None,
    console_base_url: str = "",
    notify_order_status: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    notify_order_callback_ledger: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    broker_mode = request_broker_mode(payload)
    environment = broker_mode
    data_environment = request_market_data_mode(payload)
    signal_id = str(payload.get("signal_id") or "").strip()
    status = str(payload.get("status") or "submitted").strip() or "submitted"
    note = str(payload.get("note") or "")

    if not signal_id:
        return {"error": "Missing signal_id"}, 400

    try:
        signal_record = pb.get_first_record(
            "ibkr_signals",
            filter=(
                f'signal_id = "{escape_filter_string(signal_id)}" && '
                f'environment = "{escape_filter_string(data_environment)}"'
            ),
        )
        if not signal_record or not signal_record.get("id"):
            return {"error": "Signal not found"}, 404

        existing_extra = ensure_object(signal_record.get("extra"))
        order_input = ensure_object(payload.get("order"))
        order_extra = ensure_object(order_input.get("extra"))
        payload_extra = ensure_object(payload.get("extra"))
        if _signal_consumed_for_broker(signal_record, existing_extra, broker_mode, data_environment):
            broker_status = _broker_execution_status(existing_extra, broker_mode)
            top_level_status = str((signal_record or {}).get("status") or "").strip().lower()
            existing_status = broker_status or top_level_status
            force_lifecycle_update = (
                _truthy(payload.get("lifecycle_update"))
                or _truthy(order_input.get("lifecycle_update"))
                or _truthy(order_extra.get("signal_lifecycle_update"))
                or _truthy(order_extra.get("force_signal_status_notification"))
            )
            if force_lifecycle_update or _lifecycle_status_update_allowed(existing_status, status):
                pass
            else:
                return {
                    "success": True,
                    "signal_id": signal_id,
                    "status": existing_status,
                    "signal_status": existing_status,
                    "idempotent": True,
                    "broker_mode": broker_mode,
                    "data_environment": data_environment,
                    "source": "ibkr-api",
                }, 200
        signal_extra = {
            **existing_extra,
            **payload_extra,
            "last_ack_status": status,
            "last_ack_note": note,
            "last_ack_source": "ibkr-api",
            "last_ack_broker_mode": broker_mode,
            "last_ack_data_environment": data_environment,
        }
        if "protection_complete" in order_extra:
            signal_extra["protection_complete"] = bool(order_extra.get("protection_complete"))
        if order_extra.get("missing_order_ids") is not None:
            signal_extra["missing_order_ids"] = order_extra.get("missing_order_ids")
        signal_extra = _with_signal_ack_display_extra(
            signal_extra,
            order_input=order_input,
            order_extra=order_extra,
        )

        ack_orders = build_signal_ack_orders(signal_record, payload, broker_mode)
        primary_status = "Init"
        order_results: list[dict[str, Any]] = []

        def record_partial_ack(error: str) -> None:
            failure_status = "protection_incomplete"
            diagnostic_extra = _with_broker_execution(
                {
                    **signal_extra,
                    "ack_partial": True,
                    "ack_partial_status": "ack_partial",
                    "ack_error": "order_upsert_failed",
                    "order_upsert_failed": True,
                    "order_upsert_error": error,
                    "order_results": order_results,
                    "status_reason": "order_upsert_failed",
                    "protection_incomplete": True,
                    "protection_complete": False,
                },
                broker_mode=broker_mode,
                data_environment=data_environment,
                status=failure_status,
                note="order_upsert_failed",
                order_results=order_results,
                primary_order_status=primary_status or "Init",
                partial=True,
            )
            update_payload = {
                "extra": diagnostic_extra,
                **_clear_scoped_note_payload(signal_record, broker_mode),
            }
            if broker_mode == data_environment == "live":
                update_payload.update({"status": failure_status, "note": "order_upsert_failed"})
            try:
                pb.update_record(
                    "ibkr_signals",
                    str(signal_record.get("id")),
                    update_payload,
                )
            except Exception:
                try:
                    pb.update_record(
                        "ibkr_signals",
                        str(signal_record.get("id")),
                        {"extra": diagnostic_extra},
                    )
                except Exception:
                    pass

        for order_payload in ack_orders:
            try:
                response_payload, response_status_code = order_upsert_builder(
                    pb,
                    payload=order_payload,
                    normalize_environment=normalize_environment,
                    escape_filter_string=escape_filter_string,
                    notify_order_status=notify_order_status,
                    notify_order_callback_ledger=notify_order_callback_ledger,
                )
            except Exception as exc:
                response_payload = {"error": str(exc)}
                response_status_code = 500
            order_response = response_payload.get("order") if isinstance(response_payload.get("order"), dict) else {}
            order_results.append(
                {
                    "ok": int(response_status_code or 0) < 400,
                    "status_code": int(response_status_code or 0),
                    "unique_id": str(order_payload.get("unique_id") or ""),
                    "role": str(order_payload.get("role") or ""),
                    "status": str(order_response.get("status") or order_payload.get("status") or ""),
                    "payload": response_payload,
                }
            )
            if str(order_payload.get("role") or "").strip() == "entry" and order_results[-1]["status"]:
                primary_status = str(order_results[-1]["status"])
            elif not primary_status or primary_status == "Init":
                primary_status = str(order_results[-1]["status"] or primary_status)
            if not bool(order_results[-1].get("ok")):
                error = str(response_payload.get("error") or "orders_upsert_failed")
                record_partial_ack(error)
                return (
                    {
                        "error": error,
                        "diagnostic": "order_upsert_failed",
                        "ack_status": "ack_partial",
                        "signal_id": signal_id,
                        "status": "protection_incomplete",
                        "signal_status": "protection_incomplete",
                        "primary_order_status": primary_status or "Init",
                        "order_results": order_results,
                        "broker_mode": broker_mode,
                        "data_environment": data_environment,
                        "source": "ibkr-api",
                    },
                    int(response_status_code or 500 or 500),
                )

        signal_extra = _with_broker_execution(
            signal_extra,
            broker_mode=broker_mode,
            data_environment=data_environment,
            status=status,
            note=note,
            order_results=order_results,
            primary_order_status=primary_status or "Init",
        )
        notification_result: dict[str, Any] = {}
        existing_message_id = str(signal_extra.get("feishu_signal_message_id") or "").strip()
        if existing_message_id and callable(update_interactive):
            signal_chat_id = signal_chat_id_fn(broker_mode) if callable(signal_chat_id_fn) else ""
            notification_signal = {
                **dict(signal_record),
                "status": status,
                "note": note,
                "broker_mode": broker_mode,
                "data_environment": data_environment,
                "extra": signal_extra,
            }
            notification_result = sync_signal_status_notification(
                notification_signal,
                action=status,
                message="订单已提交，当前剩余购买力已更新" if status == "submitted" else note,
                send_interactive=send_interactive,
                signal_chat_id=signal_chat_id,
                update_interactive=update_interactive,
                console_base_url=console_base_url,
            )
            notification_extra = ensure_object(notification_result.get("extra_patch"))
            if notification_extra:
                signal_extra = notification_extra
        update_payload = {
            "extra": signal_extra,
            **_clear_scoped_note_payload(signal_record, broker_mode),
        }
        if status.lower() in {
            "filled_repricing_protection",
            "filled_position",
            "protected_active",
            "protection_reprice_failed",
            "protection_incomplete",
        }:
            actual_fill = first_defined(
                order_extra.get("actual_fill_price"),
                order_extra.get("entry_fill_price"),
                order_extra.get("executed_price"),
                order_input.get("actual_fill_price"),
                order_input.get("entry_fill_price"),
                order_input.get("executed_price"),
                order_input.get("fill_price"),
                order_input.get("avgPrice"),
            )
            final_sl = first_defined(
                order_extra.get("final_stop_loss"),
                order_extra.get("protection_final_stop_loss"),
                order_extra.get("stop_loss"),
                order_input.get("final_stop_loss"),
                order_input.get("stop_loss"),
            )
            final_tp = first_defined(
                order_extra.get("final_take_profit"),
                order_extra.get("protection_final_take_profit"),
                order_extra.get("take_profit"),
                order_input.get("final_take_profit"),
                order_input.get("take_profit"),
            )
            if (to_float(actual_fill) or 0.0) > 0:
                update_payload["executed_price"] = actual_fill
            if (to_float(final_sl) or 0.0) > 0:
                update_payload["stop_loss"] = final_sl
            if (to_float(final_tp) or 0.0) > 0:
                update_payload["take_profit"] = final_tp
        if broker_mode == data_environment == "live":
            update_payload.update({"status": status, "note": note})
        pb.update_record(
            "ibkr_signals",
            str(signal_record.get("id")),
            update_payload,
        )

        return (
            {
                "success": True,
                "signal_id": signal_id,
                "status": primary_status or "Init",
                "signal_status": status,
                "primary_order_status": primary_status or "Init",
                "order_results": order_results,
                "notification": notification_result,
                "broker_mode": broker_mode,
                "data_environment": data_environment,
                "source": "ibkr-api",
            },
            200,
        )
    except Exception as exc:
        return {"error": str(exc), "signal_id": signal_id, "source": "ibkr-api"}, 500
