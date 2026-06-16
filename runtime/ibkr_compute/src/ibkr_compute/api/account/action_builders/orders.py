from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from ibkr_compute.api.account.action_builders.common import _build_snapshot_action_response
from ibkr_compute.api.account.buying_power_guard import (
    _config_bool,
    build_buying_power_guard,
    estimate_entry_exposure,
)
from ibkr_compute.api.account.live import _api_app, _app_coerce_float
from ibkr_compute.api.account.snapshot import (
    _build_ibkr_account_buying_power_snapshot,
    _build_ibkr_account_snapshot,
)
from ibkr_compute.api.account.snapshot_builder.context import build_fast_snapshot_status
from ibkr_compute.core.time_utils import ET
from ibkr_compute.order.buying_power_reservations import (
    LEDGER_SAFE_GUARD_META_KEYS,
    apply_reservations_to_buying_power_summary,
    merge_reservation_snapshot_into_guard,
)


BUYING_POWER_SNAPSHOT_META_KEYS = (
    "source",
    "snapshot_error",
    "configured_buying_power",
    "risk_model",
    "risk_model_default_entry_exposure",
    "risk_model_remaining_slots",
    "risk_model_position_exposure",
    "risk_model_open_order_exposure",
    "risk_model_used_exposure",
    "risk_model_strategy_position_count",
    "risk_model_strategy_entry_order_count",
    "risk_model_strategy_position_symbols",
    "risk_model_strategy_entry_order_symbols",
    *LEDGER_SAFE_GUARD_META_KEYS,
)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return number


def _payload_bool(value, default: bool = False) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    if value in (None, ""):
        return bool(default)
    return bool(value)


def _payload_modify_family(payload: dict) -> str:
    for key in ("order_family_type", "family", "role", "order_role"):
        text = str((payload or {}).get(key) or "").strip().lower()
        if text:
            return text
    source = str((payload or {}).get("source") or "").strip().lower()
    if any(token in source for token in ("stop_loss", "stop-loss", "adjust_stop", "move_sl")):
        return "stop_loss"
    if any(token in source for token in ("take_profit", "take-profit", "adjust_tp", "move_tp")):
        return "take_profit"
    return ""


def _payload_text(payload: dict, *keys: str) -> str:
    for key in keys:
        value = (payload or {}).get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _payload_order_ids(payload: dict) -> list[str]:
    raw: list[object] = []
    for key in ("order_ids", "cancel_order_ids", "ids"):
        value = (payload or {}).get(key)
        if isinstance(value, list):
            raw.extend(value)
        elif isinstance(value, str) and "," in value:
            raw.extend(value.split(","))
        elif value not in (None, ""):
            raw.append(value)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        order_id = str(item or "").strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        normalized.append(order_id)
    return normalized


def _result_order_ids(result: dict) -> list[str]:
    return [
        str(item or "").strip()
        for item in ((result or {}).get("order_ids") or [])
        if str(item or "").strip()
    ]


def _strict_bracket_protection_confirmed(result: dict) -> bool:
    order_ids = _result_order_ids(result)
    return bool((result or {}).get("protection_complete")) and len(order_ids) >= 3


def _mark_place_order_protection_incomplete(result: dict, *, reason: str) -> dict:
    marked = dict(result or {})
    order_ids = _result_order_ids(marked)
    original_error = str(marked.get("error") or "").strip()
    if original_error:
        marked["broker_error"] = original_error
    marked["ok"] = False
    marked["error"] = "protection_incomplete"
    marked["message"] = (
        "Entry order may have been submitted, but TP/SL protection was not fully confirmed."
    )
    marked["entry_submitted"] = bool(
        marked.get("entry_coid")
        or marked.get("entry_order_id")
        or (order_ids and str(order_ids[0] or "").strip())
        or marked.get("entry_submitted")
    )
    marked["protection_incomplete"] = True
    marked["protection_incomplete_reason"] = str(reason or "protection_not_confirmed")
    marked.setdefault("recommended_action", "refresh_account_and_review_or_cancel_unprotected_entry")
    marked.setdefault("safe_action", "diagnostic_only_no_broker_call")
    return marked


def _merge_snapshot_guard_metadata(guard: dict, snapshot_guard: dict | None) -> dict:
    if not isinstance(snapshot_guard, dict):
        return guard
    for key in BUYING_POWER_SNAPSHOT_META_KEYS:
        if snapshot_guard.get(key) not in (None, ""):
            guard[key] = snapshot_guard.get(key)
    if guard.get("state") == "unavailable" and snapshot_guard.get("reason"):
        guard["reason"] = snapshot_guard.get("reason")
    default_entry_exposure = _safe_float(guard.get("risk_model_default_entry_exposure"), 0.0)
    if default_entry_exposure > 0:
        remaining_after = guard.get("remaining_after")
        if remaining_after in (None, ""):
            remaining_after = guard.get("remaining")
        guard["risk_model_remaining_slots"] = int(
            math.floor(
                max(0.0, _safe_float(remaining_after, 0.0) - _safe_float(guard.get("block_floor"), 0.0))
                / default_entry_exposure
            )
        )
    return guard


def _parse_snapshot_timestamp(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
        if number > 0:
            return number / 1000.0 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        text = str(value or "").strip()
        if not text:
            return 0.0
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def _snapshot_age_s(payload: dict | None) -> float | None:
    data = payload if isinstance(payload, dict) else {}
    guard = data.get("buying_power_guard") if isinstance(data.get("buying_power_guard"), dict) else {}
    for value in (
        data.get("fetched_at"),
        data.get("summary_fetched_at"),
        data.get("snapshot_fetched_at"),
        data.get("baseline_fetched_at"),
        guard.get("snapshot_fetched_at"),
        guard.get("baseline_fetched_at"),
    ):
        epoch = _parse_snapshot_timestamp(value)
        if epoch > 0:
            return max(0.0, time.time() - epoch)
    return None


def _snapshot_et_date(*payloads: dict | None) -> str:
    for payload in payloads:
        data = payload if isinstance(payload, dict) else {}
        guard = data.get("buying_power_guard") if isinstance(data.get("buying_power_guard"), dict) else {}
        for value in (
            data.get("fetched_at"),
            data.get("summary_fetched_at"),
            data.get("snapshot_fetched_at"),
            data.get("baseline_fetched_at"),
            guard.get("snapshot_fetched_at"),
            guard.get("baseline_fetched_at"),
        ):
            epoch = _parse_snapshot_timestamp(value)
            if epoch > 0:
                return datetime.fromtimestamp(epoch, timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
    return ""


def _snapshot_max_age_sec(service, environment: str) -> float:
    config = getattr(service, "config", None)
    getter = getattr(config, "get_float_for_environment", None)
    if callable(getter):
        try:
            return max(1.0, float(getter("ibkr_buying_power_max_snapshot_age_sec", environment, 180.0)))
        except Exception:
            pass
    getter = getattr(config, "get_float", None)
    if callable(getter):
        try:
            return max(1.0, float(getter("ibkr_buying_power_max_snapshot_age_sec", 180.0)))
        except Exception:
            pass
    return 180.0


def _baseline_max_age_sec(service, environment: str) -> float:
    config = getattr(service, "config", None)
    default = 1800.0
    getter = getattr(config, "get_float_for_environment", None)
    if callable(getter):
        try:
            return max(_snapshot_max_age_sec(service, environment), float(getter("ibkr_buying_power_stale_baseline_max_age_sec", environment, default)))
        except Exception:
            pass
    getter = getattr(config, "get_float", None)
    if callable(getter):
        try:
            return max(_snapshot_max_age_sec(service, environment), float(getter("ibkr_buying_power_stale_baseline_max_age_sec", default)))
        except Exception:
            pass
    return default


def _ledger_next_refresh_at(force_refresh_details: dict) -> str:
    retry_after_s = 0.0
    for key in ("snapshot_force_refresh_retry_after_s", "retry_after_s"):
        try:
            retry_after_s = max(retry_after_s, float((force_refresh_details or {}).get(key) or 0.0))
        except (TypeError, ValueError):
            pass
    return (datetime.now(timezone.utc) + timedelta(seconds=max(30.0, retry_after_s))).isoformat()


def _buying_power_snapshot_summary(payload: dict | None) -> dict:
    summary = (payload or {}).get("summary") if isinstance(payload, dict) else {}
    return dict(summary or {}) if isinstance(summary, dict) else {}


def _buying_power_guard_available(payload: dict | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    guard = data.get("buying_power_guard") if isinstance(data.get("buying_power_guard"), dict) else {}
    state = str(guard.get("state") or "").strip().lower()
    return bool(_buying_power_snapshot_summary(data) and guard.get("available") is not False and state != "unavailable")


def _ledger_safe_can_allow_manual(
    guard: dict,
    *,
    reservation_snapshot: dict,
    account_summary_source: str,
    stale_age_s: float | None,
    freshness_block_reason: str,
    baseline: dict,
    snapshot: dict,
    force_refresh_details: dict,
) -> tuple[bool, dict]:
    details = {
        "ledger_safe_enabled": True,
        "ledger_safe_freshness_reason": str(freshness_block_reason or ""),
        "ledger_snapshot_source": str(account_summary_source or ""),
        "ledger_snapshot_age_s": round(float(stale_age_s), 1) if stale_age_s is not None else None,
        "ledger_next_refresh_at": _ledger_next_refresh_at(force_refresh_details),
    }
    snapshot_date = _snapshot_et_date(
        snapshot if account_summary_source == "snapshot" else {},
        baseline if account_summary_source == "baseline" else {},
        snapshot,
        baseline,
    )
    today_et = datetime.now(ET).strftime("%Y-%m-%d")
    details["ledger_snapshot_et_date"] = snapshot_date
    details["ledger_today_et_date"] = today_et
    if snapshot_date != today_et:
        details["ledger_safe_block_reason"] = "snapshot_not_today"
        return False, details
    if not bool((reservation_snapshot or {}).get("today_entry_exposure_available")):
        details["ledger_safe_block_reason"] = "today_entry_ledger_unavailable"
        details["ledger_today_entry_exposure_error"] = str((reservation_snapshot or {}).get("today_entry_exposure_error") or "")
        return False, details

    requested = _safe_float((guard or {}).get("requested_exposure"), 0.0)
    account_remaining = _safe_float(
        (guard or {}).get("account_remaining_buying_power"),
        _safe_float((guard or {}).get("remaining"), 0.0)
        + _safe_float((guard or {}).get("local_reserved_exposure"), 0.0),
    )
    today_open_exposure = max(0.0, _safe_float((reservation_snapshot or {}).get("today_entry_exposure"), 0.0))
    pending_reserved = max(0.0, _safe_float((reservation_snapshot or {}).get("pending_reservation_exposure"), 0.0))
    ledger_remaining_before = max(0.0, account_remaining - today_open_exposure - pending_reserved)
    ledger_remaining_after = max(0.0, ledger_remaining_before - requested)
    block_floor = _safe_float((guard or {}).get("block_floor"), 0.0)
    warn_floor = _safe_float((guard or {}).get("warn_floor"), 0.0)
    net_liq = _safe_float((guard or {}).get("net_liquidation"), 0.0)
    details.update(
        {
            "ledger_safe_used": True,
            "ledger_snapshot_fetched_at": str(
                (snapshot or {}).get("fetched_at")
                or (baseline or {}).get("fetched_at")
                or (guard or {}).get("snapshot_fetched_at")
                or ""
            ),
            "ledger_account_remaining_buying_power": round(account_remaining, 2),
            "ledger_today_open_exposure": round(today_open_exposure, 2),
            "ledger_pending_reserved_exposure": round(pending_reserved, 2),
            "ledger_requested_exposure": round(requested, 2),
            "ledger_remaining_before_request": round(ledger_remaining_before, 2),
            "ledger_remaining_after": round(ledger_remaining_after, 2),
            "ledger_today_entry_count": int((reservation_snapshot or {}).get("today_entry_count") or 0),
            "ledger_pending_reserved_count": int((reservation_snapshot or {}).get("pending_reservation_count") or 0),
            "ledger_remaining_after_pct_net_liq": ledger_remaining_after / net_liq * 100.0 if net_liq > 0 else None,
            "ledger_remaining_pct_net_liq": ledger_remaining_before / net_liq * 100.0 if net_liq > 0 else None,
            "refresh_block_reason": str(
                (force_refresh_details or {}).get("snapshot_force_refresh_reason")
                or (force_refresh_details or {}).get("snapshot_force_refresh_result")
                or ""
            ),
        }
    )
    if ledger_remaining_after < block_floor:
        details["ledger_safe_block_reason"] = "remaining_after_below_block_floor"
        return False, details
    details["ledger_safe_block_reason"] = ""
    details["ledger_safe_state"] = "warning" if warn_floor > 0 and ledger_remaining_after < warn_floor else "ledger_safe"
    return True, details


def _apply_ledger_safe_decision(
    guard: dict,
    *,
    ledger_allowed: bool,
    ledger_details: dict,
    freshness_block_reason: str,
) -> dict:
    guard.update(ledger_details)
    if ledger_allowed:
        guard["available"] = True
        guard["state"] = str(ledger_details.get("ledger_safe_state") or "ledger_safe")
        guard["reason"] = "buying_power_ledger_safe_after_refresh_blocked"
        guard["snapshot_error"] = freshness_block_reason
        guard["snapshot_stale_allowed"] = True
        guard["snapshot_stale_allowed_reason"] = "today_ledger_safe"
        guard["original_freshness_block_reason"] = freshness_block_reason
        guard["source"] = "today_ledger_safe"
        guard["remaining"] = ledger_details.get("ledger_remaining_before_request")
        guard["remaining_after"] = ledger_details.get("ledger_remaining_after")
        guard["remaining_pct_net_liq"] = ledger_details.get("ledger_remaining_pct_net_liq")
        guard["remaining_after_pct_net_liq"] = ledger_details.get("ledger_remaining_after_pct_net_liq")
        return guard
    if ledger_details.get("ledger_safe_block_reason") == "remaining_after_below_block_floor":
        guard["available"] = True
        guard["state"] = "blocked"
        guard["reason"] = "buying_power_ledger_below_block_threshold"
        guard["snapshot_error"] = freshness_block_reason
        guard["original_freshness_block_reason"] = freshness_block_reason
        guard["source"] = "today_ledger_safe"
        guard["remaining"] = ledger_details.get("ledger_remaining_before_request")
        guard["remaining_after"] = ledger_details.get("ledger_remaining_after")
        guard["remaining_pct_net_liq"] = ledger_details.get("ledger_remaining_pct_net_liq")
        guard["remaining_after_pct_net_liq"] = ledger_details.get("ledger_remaining_after_pct_net_liq")
        return guard
    guard["available"] = False
    guard["state"] = "unavailable"
    guard["reason"] = freshness_block_reason
    guard["snapshot_error"] = freshness_block_reason
    return guard


def _notify_manual_buying_power_event(
    service,
    *,
    title: str,
    level: str,
    environment: str,
    symbol: str,
    direction: str,
    quantity: int,
    guard: dict,
) -> None:
    if not _config_bool(getattr(service, "config", None), "ibkr_buying_power_notify_enabled", environment, True):
        return
    pb = getattr(service, "pb", None)
    notifier = getattr(pb, "notify_system_event", None)
    if not callable(notifier):
        return
    def guard_number(key: str):
        raw = (guard or {}).get(key)
        if raw in (None, ""):
            return "不可用"
        try:
            return round(float(raw), 2)
        except (TypeError, ValueError):
            return "不可用"
    detail = {
        "标的": symbol,
        "方向": direction,
        "数量": quantity,
        "风控来源": str((guard or {}).get("source") or "account_summary"),
        "当前剩余购买力": guard_number("remaining"),
        "本次预估占用": guard_number("requested_exposure"),
        "下单后剩余购买力": guard_number("remaining_after"),
        "预警阈值": guard_number("warn_floor"),
        "禁止阈值": guard_number("block_floor"),
        "状态": str((guard or {}).get("state") or "ok"),
        "原因": str((guard or {}).get("reason") or ""),
    }
    if (guard or {}).get("configured_buying_power") not in (None, ""):
        detail["配置购买力"] = guard_number("configured_buying_power")
    if (guard or {}).get("risk_model_used_exposure") not in (None, ""):
        detail["策略已占用"] = guard_number("risk_model_used_exposure")
    if (guard or {}).get("risk_model_remaining_slots") not in (None, ""):
        detail["估算剩余可开仓数"] = guard.get("risk_model_remaining_slots")
    if (guard or {}).get("ledger_safe_used"):
        detail["今日账本放行"] = "是"
        detail["今日已开仓占用"] = guard_number("ledger_today_open_exposure")
        detail["今日待提交预占"] = guard_number("ledger_pending_reserved_exposure")
        detail["账本校验后剩余"] = guard_number("ledger_remaining_after")
    if (guard or {}).get("ledger_next_refresh_at"):
        detail["下次购买力刷新时间"] = str(guard.get("ledger_next_refresh_at") or "")
    if (guard or {}).get("refresh_block_reason"):
        detail["当前刷新受阻原因"] = str(guard.get("refresh_block_reason") or "")
    try:
        notifier(
            title,
            detail,
            event_type="account_order",
            level=level,
            source="ibkr_compute",
            environment=environment,
        )
    except Exception:
        pass


def _build_ibkr_cancel_order_response(service, payload: dict) -> tuple[dict, int]:
    action_started_at = time.perf_counter()
    order_id = str((payload or {}).get("order_id") or (payload or {}).get("id") or "").strip()
    order_ids = _payload_order_ids(payload)
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    include_snapshot = _payload_bool((payload or {}).get("include_snapshot"), True)
    if order_ids:
        source = str((payload or {}).get("source") or "cancel_order_ids").strip() or "cancel_order_ids"
        symbol = str((payload or {}).get("symbol") or "").strip().upper()
        operation_started_at = time.perf_counter()
        result = service.order_modifier.cancel_order_ids(order_ids, acct_id=acct_id, source=source, symbol=symbol)
        operation_elapsed_s = time.perf_counter() - operation_started_at
        return _build_snapshot_action_response(
            service,
            "cancel_order_ids",
            result,
            delay_seconds=0.0,
            extra={"order_ids": order_ids},
            include_snapshot=include_snapshot,
            action_started_at=action_started_at,
            operation_elapsed_s=operation_elapsed_s,
        )
    if not order_id:
        return {"ok": False, "error": "Missing order_id"}, 400

    operation_started_at = time.perf_counter()
    result = service.order_modifier.cancel_order(order_id, acct_id=acct_id)
    operation_elapsed_s = time.perf_counter() - operation_started_at
    return _build_snapshot_action_response(
        service,
        "cancel_order",
        result,
        delay_seconds=0.0,
        extra={"order_id": order_id},
        include_snapshot=include_snapshot,
        action_started_at=action_started_at,
        operation_elapsed_s=operation_elapsed_s,
    )


def _build_ibkr_cancel_all_orders_response(service, payload: dict) -> tuple[dict, int]:
    action_started_at = time.perf_counter()
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    include_snapshot = _payload_bool((payload or {}).get("include_snapshot"), True)
    operation_started_at = time.perf_counter()
    result = service.order_modifier.cancel_all_orders(acct_id=acct_id)
    operation_elapsed_s = time.perf_counter() - operation_started_at
    return _build_snapshot_action_response(
        service,
        "cancel_all_orders",
        result,
        delay_seconds=0.0,
        include_snapshot=include_snapshot,
        action_started_at=action_started_at,
        operation_elapsed_s=operation_elapsed_s,
    )


def _build_ibkr_modify_order_response(service, payload: dict) -> tuple[dict, int]:
    action_started_at = time.perf_counter()
    order_id = str((payload or {}).get("order_id") or (payload or {}).get("id") or "").strip()
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    include_snapshot = _payload_bool((payload or {}).get("include_snapshot"), True)
    family = _payload_modify_family(payload)
    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    updates = {}

    if not order_id:
        return {"ok": False, "error": "Missing order_id"}, 400

    price = _app_coerce_float((payload or {}).get("price"))
    quantity = _app_coerce_float((payload or {}).get("quantity"))
    tif = str((payload or {}).get("tif") or "").strip().upper()

    if price is not None:
        if family in {"stop_loss", "stop", "sl"}:
            updates["auxPrice"] = price
        else:
            updates["price"] = price
    if quantity is not None:
        updates["quantity"] = quantity
    if tif:
        updates["tif"] = tif
    if not updates:
        return {"ok": False, "error": "No valid modify fields supplied"}, 400

    operation_started_at = time.perf_counter()
    result = service.order_modifier.modify_order(
        order_id,
        updates,
        acct_id=acct_id,
        operation="adjust_stop_loss" if family in {"stop_loss", "stop", "sl"} else "modify_manual",
        order_family_type=family,
        symbol=symbol,
    )
    operation_elapsed_s = time.perf_counter() - operation_started_at
    return _build_snapshot_action_response(
        service,
        "modify_order",
        result,
        delay_seconds=0.0,
        extra={
            "order_id": order_id,
            "updates": updates,
        },
        include_snapshot=include_snapshot,
        action_started_at=action_started_at,
        operation_elapsed_s=operation_elapsed_s,
    )


def _build_ibkr_place_order_response(service, payload: dict) -> tuple[dict, int]:
    action_started_at = time.perf_counter()
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    if runtime_environment == "backtest":
        return {"ok": False, "error": "Backtest environment does not support live order placement"}, 400
    trading_enabled = _config_bool(getattr(service, "config", None), "ibkr_trading_enabled", runtime_environment, True)
    live_trading_enabled = _config_bool(
        getattr(service, "config", None),
        "ibkr_live_trading_enabled",
        runtime_environment,
        True,
    )
    if not trading_enabled or (runtime_environment == "live" and not live_trading_enabled):
        return {
            "ok": False,
            "error": "trading_disabled",
            "environment": runtime_environment,
            "broker_mode": runtime_environment,
            "ibkr_trading_enabled": bool(trading_enabled),
            "ibkr_live_trading_enabled": bool(live_trading_enabled),
        }, 403

    service_status = build_fast_snapshot_status(service)
    session_authenticated = bool((service_status.get("session") or {}).get("authenticated"))
    service_running = bool(getattr(service, "is_running", False) or getattr(service, "is_starting", False))
    if not service_running:
        return {"ok": False, "error": "IBKR service is not running"}, 409
    if not session_authenticated:
        return {"ok": False, "error": "IBKR session is not authenticated"}, 409
    if not hasattr(service, "order_placer") or not hasattr(service, "conid_resolver"):
        return {"ok": False, "error": "IBKR order components are unavailable"}, 503

    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    direction = str((payload or {}).get("direction") or "").strip().lower()
    order_type = str(
        (payload or {}).get("order_type") or (payload or {}).get("entry_order_type") or "LMT"
    ).strip().upper()
    quantity_value = _app_coerce_float((payload or {}).get("quantity"))
    conid = int(_app_coerce_float((payload or {}).get("conid"), 0) or 0)
    entry_price = _app_coerce_float((payload or {}).get("entry_price"))
    take_profit_price = _app_coerce_float((payload or {}).get("take_profit_price"))
    stop_loss_price = _app_coerce_float((payload or {}).get("stop_loss_price"))

    if not symbol:
        return {"ok": False, "error": "Missing symbol"}, 400
    if direction not in {"long", "short"}:
        return {"ok": False, "error": "direction must be long or short"}, 400
    if order_type not in {"LMT", "MKT"}:
        return {"ok": False, "error": "order_type must be LMT or MKT"}, 400
    if quantity_value is None or quantity_value <= 0 or abs(quantity_value - round(quantity_value)) > 1e-9:
        return {"ok": False, "error": "quantity must be a positive integer"}, 400
    if take_profit_price is None or take_profit_price <= 0 or stop_loss_price is None or stop_loss_price <= 0:
        return {"ok": False, "error": "take_profit_price and stop_loss_price are required"}, 400
    if order_type == "LMT" and (entry_price is None or entry_price <= 0):
        return {"ok": False, "error": "entry_price is required for limit orders"}, 400

    quantity = int(round(quantity_value))
    if direction == "long" and take_profit_price <= stop_loss_price:
        return {"ok": False, "error": "For long orders, take profit must be above stop loss"}, 400
    if direction == "short" and take_profit_price >= stop_loss_price:
        return {"ok": False, "error": "For short orders, take profit must be below stop loss"}, 400
    if order_type == "LMT" and entry_price is not None:
        if direction == "long" and not (stop_loss_price < entry_price < take_profit_price):
            return {"ok": False, "error": "For long limit orders, stop < entry < take profit is required"}, 400
        if direction == "short" and not (take_profit_price < entry_price < stop_loss_price):
            return {"ok": False, "error": "For short limit orders, take profit < entry < stop is required"}, 400

    requested_exposure = estimate_entry_exposure(
        quantity,
        entry_price,
        take_profit_price,
        stop_loss_price,
        direction,
        order_type,
    )
    if requested_exposure <= 0:
        return {
            "ok": False,
            "error": "buying_power_price_unavailable",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
        }, 400
    reservation_store = getattr(service, "buying_power_reservations", None)
    reservation_snapshot = {}
    snapshotter = getattr(reservation_store, "snapshot", None)
    if callable(snapshotter):
        try:
            reservation_snapshot = snapshotter()
        except Exception:
            reservation_snapshot = {}
    baseline = {}
    baseline_getter = getattr(reservation_store, "baseline_snapshot", None)
    if callable(baseline_getter):
        try:
            candidate = baseline_getter()
            baseline = candidate if isinstance(candidate, dict) else {}
        except Exception:
            baseline = {}

    max_snapshot_age_s = _snapshot_max_age_sec(service, runtime_environment)
    baseline_max_age_s = _baseline_max_age_sec(service, runtime_environment)
    pre_submit_snapshot = _build_ibkr_account_buying_power_snapshot(service)

    def evaluate_snapshot(payload: dict) -> tuple[dict, float | None, dict, bool, bool]:
        payload = payload if isinstance(payload, dict) else {}
        guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
        age_s = _snapshot_age_s(payload)
        summary = _buying_power_snapshot_summary(payload)
        guard_available = bool(summary and guard.get("available") is not False and str(guard.get("state") or "").strip().lower() != "unavailable")
        fresh = bool(guard_available and age_s is not None and age_s <= max_snapshot_age_s)
        return guard, age_s, summary, guard_available, fresh

    snapshot_guard, snapshot_age_s, snapshot_summary, snapshot_guard_available, snapshot_fresh = evaluate_snapshot(pre_submit_snapshot)
    force_refresh_details = {}
    if not snapshot_fresh:
        forced_snapshot = _build_ibkr_account_buying_power_snapshot(service, force_refresh=True)
        forced_guard, forced_age_s, forced_summary, forced_guard_available, forced_fresh = evaluate_snapshot(forced_snapshot)
        force_refresh_details = {
            "snapshot_force_refresh_attempted": True,
            "snapshot_force_refresh_result": "fresh" if forced_fresh else "stale" if forced_summary else "unavailable",
            "snapshot_force_refresh_reason": str(
                forced_guard.get("reason")
                or (forced_snapshot or {}).get("refresh_error")
                or (forced_snapshot or {}).get("last_refresh_error")
                or ""
            ),
            "snapshot_force_refresh_age_s": round(float(forced_age_s), 1) if forced_age_s is not None else None,
        }
        if isinstance(forced_snapshot, dict) and forced_snapshot.get("hard_blocked") is not None:
            force_refresh_details["snapshot_force_refresh_hard_blocked"] = bool(forced_snapshot.get("hard_blocked"))
        if (forced_snapshot or {}).get("retry_after_s") not in (None, ""):
            force_refresh_details["snapshot_force_refresh_retry_after_s"] = (forced_snapshot or {}).get("retry_after_s")
        if forced_fresh or (forced_summary and not snapshot_summary):
            pre_submit_snapshot = forced_snapshot
            snapshot_guard = forced_guard
            snapshot_age_s = forced_age_s
            snapshot_summary = forced_summary
            snapshot_guard_available = forced_guard_available
            snapshot_fresh = forced_fresh

    baseline_available = bool((baseline or {}).get("available"))
    baseline_age_s = _snapshot_age_s(baseline) if baseline_available else None
    baseline_fresh = bool(baseline_available and baseline_age_s is not None and baseline_age_s <= baseline_max_age_s)
    if not snapshot_fresh and baseline_fresh:
        account_summary_source = "baseline"
        account_summary = dict((baseline or {}).get("summary") or {})
    else:
        account_summary_source = "snapshot" if snapshot_summary else "baseline" if baseline_available else ""
        account_summary = snapshot_summary or dict((baseline or {}).get("summary") or {})
    freshness_block_reason = ""
    if not snapshot_fresh and not baseline_fresh and account_summary and (baseline_available or snapshot_guard_available):
        stale_age_s = snapshot_age_s if account_summary_source == "snapshot" else baseline_age_s
        freshness_block_reason = (
            "buying_power_snapshot_missing_timestamp"
            if stale_age_s is None
            else "buying_power_snapshot_stale"
        )

    adjusted_summary = apply_reservations_to_buying_power_summary(
        account_summary,
        reservation_snapshot,
    )
    buying_power_guard = build_buying_power_guard(
        adjusted_summary,
        config=getattr(service, "config", None),
        environment=runtime_environment,
        requested_exposure=requested_exposure,
    )
    account_remaining_raw = account_summary.get("remaining_buying_power")
    if account_remaining_raw in (None, ""):
        account_remaining_raw = account_summary.get("buying_power")
    if account_remaining_raw not in (None, ""):
        buying_power_guard["account_remaining_buying_power"] = _safe_float(account_remaining_raw, 0.0)
    merge_reservation_snapshot_into_guard(buying_power_guard, reservation_snapshot)
    _merge_snapshot_guard_metadata(buying_power_guard, snapshot_guard if isinstance(snapshot_guard, dict) else None)
    if force_refresh_details:
        buying_power_guard.update(force_refresh_details)
    if baseline_available:
        buying_power_guard["baseline_available"] = True
        buying_power_guard["baseline_source"] = str((baseline or {}).get("source") or "")
        buying_power_guard["baseline_fetched_at"] = str((baseline or {}).get("fetched_at") or "")
        buying_power_guard["baseline_stored_at"] = str((baseline or {}).get("stored_at") or "")
        buying_power_guard["baseline_cache_state"] = str((baseline or {}).get("cache_state") or "")
        if baseline_age_s is not None:
            buying_power_guard["baseline_age_s"] = round(float(baseline_age_s), 1)
        buying_power_guard["baseline_max_age_s"] = round(float(baseline_max_age_s), 1)
        buying_power_guard["baseline_fresh"] = bool(baseline_fresh)
    buying_power_guard["snapshot_fetched_at"] = (pre_submit_snapshot or {}).get("fetched_at") or (baseline or {}).get("fetched_at") or ""
    if snapshot_age_s is not None:
        buying_power_guard["snapshot_age_s"] = round(float(snapshot_age_s), 1)
    buying_power_guard["snapshot_max_age_s"] = round(float(max_snapshot_age_s), 1)
    buying_power_guard["snapshot_fresh"] = bool(snapshot_fresh or baseline_fresh)
    if isinstance((pre_submit_snapshot or {}).get("errors"), dict):
        buying_power_guard["snapshot_errors"] = dict((pre_submit_snapshot or {}).get("errors") or {})
    if freshness_block_reason and buying_power_guard.get("enabled"):
        stale_age_s = snapshot_age_s if account_summary_source == "snapshot" else baseline_age_s
        ledger_allowed, ledger_details = _ledger_safe_can_allow_manual(
            buying_power_guard,
            reservation_snapshot=reservation_snapshot,
            account_summary_source=account_summary_source,
            stale_age_s=stale_age_s,
            freshness_block_reason=freshness_block_reason,
            baseline=baseline,
            snapshot=pre_submit_snapshot,
            force_refresh_details=force_refresh_details,
        )
        _apply_ledger_safe_decision(
            buying_power_guard,
            ledger_allowed=ledger_allowed,
            ledger_details=ledger_details,
            freshness_block_reason=freshness_block_reason,
        )
    guard_state = str(buying_power_guard.get("state") or "").strip().lower()
    if guard_state == "unavailable":
        _notify_manual_buying_power_event(
            service,
            title="手动开仓暂停：购买力风控不可用",
            level="error",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )
        return {
            "ok": False,
            "error": "buying_power_unavailable",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "buying_power_guard": buying_power_guard,
            "snapshot": pre_submit_snapshot,
        }, 503
    if guard_state == "blocked":
        _notify_manual_buying_power_event(
            service,
            title="手动开仓已被动态购买力上限拦截",
            level="error",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )
        return {
            "ok": False,
            "error": "buying_power_blocked",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "buying_power_guard": buying_power_guard,
            "snapshot": pre_submit_snapshot,
        }, 409
    if guard_state == "ledger_safe" or (guard_state == "warning" and buying_power_guard.get("ledger_safe_used")):
        _notify_manual_buying_power_event(
            service,
            title="手动开仓按今日账本安全放行：账户刷新待重试",
            level="warning",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )
    elif guard_state == "warning":
        _notify_manual_buying_power_event(
            service,
            title="手动开仓购买力预警",
            level="warning",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )

    if conid <= 0:
        try:
            conid = int(service.conid_resolver.resolve(symbol) or 0)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to resolve contract for {symbol}: {exc}"}, 500
    if conid <= 0:
        return {"ok": False, "error": f"Cannot resolve conid for {symbol}"}, 404

    duplicate_order = None
    if hasattr(service, "order_tracker"):
        try:
            duplicate_order = service.order_tracker.find_duplicate_open_entry(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=float(entry_price or 0.0),
                entry_order_type=order_type,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Failed to inspect live orders before placement: {exc}"}, 500

    if duplicate_order:
        try:
            service.order_tracker.sync_live_orders_snapshot([duplicate_order])
        except Exception:
            pass
        snapshot = _build_ibkr_account_snapshot(service)
        broker_order_id = str((duplicate_order or {}).get("orderId") or (duplicate_order or {}).get("id") or "").strip()
        return {
            "ok": False,
            "error": "Duplicate open broker order already exists",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "duplicate_order": {
                "order_id": broker_order_id,
                "status": str((duplicate_order or {}).get("status") or "").strip(),
                "symbol": str(
                    (duplicate_order or {}).get("ticker") or (duplicate_order or {}).get("symbol") or ""
                ).strip().upper(),
                "side": str((duplicate_order or {}).get("side") or "").strip().upper(),
                "price": _app_coerce_float((duplicate_order or {}).get("price"), 0.0) or 0.0,
                "quantity": _app_coerce_float(
                    (duplicate_order or {}).get("totalSize")
                    if (duplicate_order or {}).get("totalSize") is not None
                    else (duplicate_order or {}).get("quantity"),
                    0.0,
                ) or 0.0,
            },
            "snapshot": snapshot,
        }, 409

    signal_id = _payload_text(payload, "signal_id", "client_signal_id") or f"MANUAL_{runtime_environment.upper()}_{symbol}_{int(time.time())}"
    trade_group_id = _payload_text(
        payload,
        "trade_group_id",
        "bracket_group",
        "client_order_id",
        "order_ref",
        "orderRef",
        "cOID",
        "coid",
    )
    order_extra = (payload or {}).get("order_extra")
    if not isinstance(order_extra, dict):
        order_extra = (payload or {}).get("extra") if isinstance((payload or {}).get("extra"), dict) else {}
    order_extra = dict(order_extra or {})
    if trade_group_id:
        order_extra.setdefault("trade_group_id", trade_group_id)
        order_extra.setdefault("client_order_id", trade_group_id)
    order_extra.setdefault("manual_order_api", True)
    tif = _payload_text(payload, "tif", "entry_tif").upper() or "DAY"
    outside_rth = _payload_bool(
        (payload or {}).get("outside_rth")
        if (payload or {}).get("outside_rth") not in (None, "")
        else (payload or {}).get("outsideRth")
        if (payload or {}).get("outsideRth") not in (None, "")
        else (payload or {}).get("extended_hours")
        if (payload or {}).get("extended_hours") not in (None, "")
        else (payload or {}).get("outside_regular_trading_hours"),
        False,
    )
    config = getattr(service, "config", None)
    config_has_value = getattr(config, "has_value_for_environment", None)
    has_fast_accept_config = False
    if callable(config_has_value):
        try:
            has_fast_accept_config = bool(config_has_value("ibkr_order_place_fast_accept_enabled", runtime_environment))
        except Exception:
            has_fast_accept_config = False
    fast_accept_enabled = runtime_environment == "paper" if not has_fast_accept_config else _config_bool(
        config,
        "ibkr_order_place_fast_accept_enabled",
        runtime_environment,
        runtime_environment == "paper",
    )
    if "wait_for_confirmation" in (payload or {}):
        fast_accept_enabled = not _payload_bool((payload or {}).get("wait_for_confirmation"), True)
    if "fast_ack" in (payload or {}):
        fast_accept_enabled = _payload_bool((payload or {}).get("fast_ack"), fast_accept_enabled)
    if "async_confirm" in (payload or {}):
        fast_accept_enabled = _payload_bool((payload or {}).get("async_confirm"), fast_accept_enabled)
    confirmation_mode = "background" if fast_accept_enabled else "sync"

    operation_started_at = time.perf_counter()
    result = service.order_placer.place_bracket_order(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=float(entry_price or 0.0),
        take_profit_price=float(take_profit_price),
        stop_loss_price=float(stop_loss_price),
        use_paper=api_app._ibkr_service_uses_paper_account(service),
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        entry_order_type=order_type,
        tif=tif,
        order_extra=order_extra,
        outside_rth=outside_rth,
        buying_power_guard=buying_power_guard,
        confirmation_mode=confirmation_mode,
    )
    operation_elapsed_s = time.perf_counter() - operation_started_at
    strict_confirmation_requested = _payload_bool((payload or {}).get("wait_for_confirmation"), False)
    if bool(result.get("ok")) and bool(result.get("protection_incomplete")):
        result = _mark_place_order_protection_incomplete(result, reason="protection_incomplete")
    elif bool(result.get("ok")) and strict_confirmation_requested and not _strict_bracket_protection_confirmed(result):
        result = _mark_place_order_protection_incomplete(result, reason="strict_confirmation_missing_protection")
    submitted_buying_power_guard = (
        dict(result.get("buying_power_guard"))
        if isinstance(result.get("buying_power_guard"), dict)
        else dict(buying_power_guard)
    )
    if result.get("ok"):
        _notify_manual_buying_power_event(
            service,
            title="手动开仓已提交",
            level="info",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=submitted_buying_power_guard,
        )
    return _build_snapshot_action_response(
        service,
        "place_order",
        result,
        delay_seconds=0.0,
        extra={
            "environment": runtime_environment,
            "symbol": symbol,
            "conid": conid,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "signal_id": signal_id,
            "trade_group_id": trade_group_id,
            "buying_power_guard": submitted_buying_power_guard,
            "pre_submit_buying_power_guard": buying_power_guard,
            "confirmation_mode": confirmation_mode,
            "fast_ack": bool(fast_accept_enabled),
            "wait_for_confirmation": bool(strict_confirmation_requested),
            "tif": tif,
            "outside_rth": outside_rth,
        },
        include_snapshot=_payload_bool((payload or {}).get("include_snapshot"), True),
        action_started_at=action_started_at,
        operation_elapsed_s=operation_elapsed_s,
    )


__all__ = [
    "_build_ibkr_cancel_all_orders_response",
    "_build_ibkr_cancel_order_response",
    "_build_ibkr_modify_order_response",
    "_build_ibkr_place_order_response",
]
