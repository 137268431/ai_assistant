from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_api.orders.upsert import build_order_upsert_response
from ibkr_api.orders.values import ensure_object
from ibkr_api.signals.ack import build_signal_ack_orders
from ibkr_api.signals.notifications import sync_signal_status_notification


_BROKER_SIGNAL_FINAL_STATUSES = {
    "submitted",
    "rejected",
    "expired",
    "blocked",
    "duplicate_existing_broker_order",
    "validation_rejected",
    "submit_failed",
    "protection_incomplete",
}


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


def _signal_consumed_for_broker(
    record: dict[str, Any],
    extra: dict[str, Any],
    broker_mode: str,
    data_environment: str,
) -> bool:
    broker_status = _broker_execution_status(extra, broker_mode)
    if broker_status in _BROKER_SIGNAL_FINAL_STATUSES:
        return True
    legacy_status = str((record or {}).get("status") or "").strip().lower()
    return broker_mode == data_environment and bool(legacy_status and legacy_status != "pending")


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
    date_str: str,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    as_dict: Callable[[Any], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    broker_mode = normalize_environment(environment, "live")
    data_environment = resolve_data_environment(broker_mode)
    normalized_date = str(date_str or "").strip()
    if not normalized_date:
        return {"error": "缺少 date 参数"}, 400

    try:
        records = pb.get_records(
            "ibkr_signals",
            filter=(
                f'status = "pending" && date = "{escape_filter_string(normalized_date)}" && '
                f'environment = "{escape_filter_string(data_environment)}"'
            ),
            sort="-bar_time_ms",
            per_page=100,
            page=1,
        )
        ibkr_signals = []
        for record in records or []:
            record_extra = as_dict(record.get("extra"))
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
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    broker_mode = environment
    data_environment = resolve_data_environment(broker_mode)
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
        if _signal_consumed_for_broker(signal_record, existing_extra, broker_mode, data_environment):
            broker_status = _broker_execution_status(existing_extra, broker_mode)
            return {
                "success": True,
                "signal_id": signal_id,
                "status": broker_status,
                "signal_status": broker_status,
                "idempotent": True,
                "broker_mode": broker_mode,
                "data_environment": data_environment,
                "source": "ibkr-api",
            }, 200
        signal_extra = {
            **existing_extra,
            "last_ack_status": status,
            "last_ack_note": note,
            "last_ack_source": "ibkr-api",
            "last_ack_broker_mode": broker_mode,
            "last_ack_data_environment": data_environment,
        }
        order_input = ensure_object(payload.get("order"))
        order_extra = ensure_object(order_input.get("extra"))
        if "protection_complete" in order_extra:
            signal_extra["protection_complete"] = bool(order_extra.get("protection_complete"))
        if order_extra.get("missing_order_ids") is not None:
            signal_extra["missing_order_ids"] = order_extra.get("missing_order_ids")

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
                "note": f"{broker_mode}:order_upsert_failed",
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
        update_payload = {
            "extra": signal_extra,
            "note": f"{broker_mode}:{status}",
        }
        if broker_mode == data_environment == "live":
            update_payload.update({"status": status, "note": note})
        updated_signal = pb.update_record(
            "ibkr_signals",
            str(signal_record.get("id")),
            update_payload,
        )
        notification_result: dict[str, Any] = {}
        if callable(update_interactive) or callable(send_interactive):
            signal_chat_id = signal_chat_id_fn(broker_mode) if callable(signal_chat_id_fn) else ""
            notification_signal = (
                dict(updated_signal)
                if isinstance(updated_signal, dict)
                else {**signal_record, "extra": signal_extra}
            )
            notification_signal.update(
                {
                    "status": status,
                    "note": note,
                    "broker_mode": broker_mode,
                    "data_environment": data_environment,
                    "extra": signal_extra,
                }
            )
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
                updated_signal = pb.update_record(
                    "ibkr_signals",
                    str(signal_record.get("id")),
                    {"extra": signal_extra},
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
