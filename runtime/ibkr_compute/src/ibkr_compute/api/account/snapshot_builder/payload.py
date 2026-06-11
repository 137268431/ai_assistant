from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

from ibkr_compute.api.account.buying_power_guard import (
    build_buying_power_guard,
    enrich_buying_power_summary,
)
from ibkr_compute.api.account.live import (
    _extract_summary_number,
    _extract_summary_text,
    _normalize_live_order,
    _normalize_live_position,
    _summary_lookup,
)
from ibkr_compute.api.runtime.common import _coerce_float
from ibkr_compute.api.account.snapshot_builder.context import (
    build_snapshot_context,
    get_snapshot_refresh_lock,
    load_cached_snapshot,
    mark_cached_snapshot_refresh_error,
    store_cached_snapshot,
)
from ibkr_compute.api.account.snapshot_builder.fetch import fetch_snapshot_sources
from ibkr_compute.api.account.snapshot_builder.recovery import (
    filter_trusted_pb_fallback_order_rows,
    load_pb_fallback_order_rows,
    pb_order_rows_to_live_orders,
    recover_live_open_orders,
)
from ibkr_compute.order.buying_power_reservations import (
    apply_reservations_to_buying_power_summary,
    merge_reservation_snapshot_into_guard,
)


def _env_bool(name: str, default: bool = False) -> bool:
    text = str(os.environ.get(name, "") or "").strip().lower()
    if not text:
        return bool(default)
    return text in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _buying_power_guard_max_stale_seconds() -> float:
    return _env_float("IBKR_BUYING_POWER_GUARD_MAX_STALE_SEC", 300.0, minimum=0.0)


def _filter_orders_for_account(account_id: str, rows: list[dict]) -> list[dict]:
    normalized_account = str(account_id or "").strip()
    if not normalized_account:
        return list(rows or [])
    filtered = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        row_account = str(item.get("account") or "").strip()
        if row_account and row_account != normalized_account:
            continue
        filtered.append(item)
    return filtered


def _normalize_snapshot_rows(
    account_id: str,
    positions_raw: list[dict],
    orders_raw: list[dict],
    live_open_payload: dict,
    fallback_ids: list[str],
):
    positions = [_normalize_live_position(item) for item in (positions_raw or []) if isinstance(item, dict)]
    filtered_orders_raw = _filter_orders_for_account(account_id, orders_raw)
    filtered_live_open_raw = _filter_orders_for_account(account_id, live_open_payload.get("orders") or [])
    orders = [_normalize_live_order(item) for item in filtered_orders_raw if isinstance(item, dict)]
    live_open_orders = [_normalize_live_order(item) for item in filtered_live_open_raw if isinstance(item, dict)]
    live_open_payload["orders"] = filtered_live_open_raw
    if not live_open_orders:
        existing_coverage = live_open_payload.get("coverage") or {}
        live_open_orders = [item for item in orders if item.get("is_open")]
        live_open_payload["coverage"] = {
            "coverage_state": str(existing_coverage.get("coverage_state") or "complete"),
            "bulk_open_count": int(existing_coverage.get("bulk_open_count") or len(live_open_orders)),
            "recovered_from_status_count": int(existing_coverage.get("recovered_from_status_count") or 0),
            "tracker_seed_count": int(existing_coverage.get("tracker_seed_count") or 0),
            "pb_seed_count": int(existing_coverage.get("pb_seed_count") or len(fallback_ids)),
            "unresolved_seed_count": int(existing_coverage.get("unresolved_seed_count") or 0),
            "unresolved_order_ids": list(existing_coverage.get("unresolved_order_ids") or []),
        }
    return positions, orders, live_open_orders, live_open_payload


def _summary_number_optional(summary_map: dict, *keys: str) -> tuple[float | None, str, dict | None]:
    for key in keys:
        lookup_key = str(key).strip().lower()
        raw_value = summary_map.get(lookup_key)
        if raw_value is None:
            continue
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("amount", "value"):
                number = _coerce_float(lowered.get(field))
                if number is not None:
                    return float(number), key, raw_value
        else:
            number = _coerce_float(raw_value)
            if number is not None:
                return float(number), key, None
    return None, "", None


def _summary_currency(raw_value: dict | None, fallback: str) -> str:
    if isinstance(raw_value, dict):
        lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
        currency = str(lowered.get("currency") or "").strip().upper()
        if currency:
            return currency
    return fallback or "USD"


def _pnl_raw_number(pnl_raw: dict | None, key: str) -> float | None:
    if not isinstance(pnl_raw, dict):
        return None
    number = _coerce_float(pnl_raw.get(key))
    if number is None:
        return None
    if not math.isfinite(float(number)) or abs(float(number)) >= 1e100:
        return None
    return float(number)


def _build_account_today_pnl(summary_map: dict, currency: str, pnl_raw: dict | None = None) -> dict:
    stream_daily_pnl = _pnl_raw_number(pnl_raw, "daily_pnl")
    realized_pnl, _, _ = _summary_number_optional(summary_map, "RealizedPnL")
    unrealized_pnl, _, _ = _summary_number_optional(summary_map, "UnrealizedPnL")
    stream_realized_pnl = _pnl_raw_number(pnl_raw, "realized_pnl")
    stream_unrealized_pnl = _pnl_raw_number(pnl_raw, "unrealized_pnl")
    if stream_daily_pnl is not None:
        return {
            "ok": True,
            "net": stream_daily_pnl,
            "currency": currency or "USD",
            "source": "broker_req_pnl",
            "raw_field": "reqPnL.dailyPnL",
            "realized": stream_realized_pnl if stream_realized_pnl is not None else realized_pnl,
            "unrealized": stream_unrealized_pnl if stream_unrealized_pnl is not None else unrealized_pnl,
            "message": "",
        }

    daily_pnl, raw_field, raw_value = _summary_number_optional(summary_map, "DailyPnL", "DayPnL", "PnL")
    if daily_pnl is None:
        return {
            "ok": False,
            "net": None,
            "currency": currency or "USD",
            "source": "unavailable",
            "raw_field": "",
            "realized": realized_pnl,
            "unrealized": unrealized_pnl,
            "message": "IBKR did not provide DailyPnL/DayPnL/PnL",
        }
    return {
        "ok": True,
        "net": daily_pnl,
        "currency": _summary_currency(raw_value, currency),
        "source": "broker_daily_pnl",
        "raw_field": raw_field,
        "realized": realized_pnl,
        "unrealized": unrealized_pnl,
        "message": "",
    }


def _build_snapshot_summary(
    summary_raw: dict,
    account_id: str,
    positions: list[dict],
    pnl_raw: dict | None = None,
) -> dict:
    summary_map = _summary_lookup(summary_raw)
    total_unrealized = sum(float(item.get("unrealized_pnl", 0) or 0) for item in positions)
    total_market_value = sum(abs(float(item.get("market_value", 0) or 0)) for item in positions)
    currency = _extract_summary_text(summary_map, "currency", "basecurrency") or "USD"
    account_today_pnl = _build_account_today_pnl(summary_map, currency, pnl_raw)
    summary = {
        "account_code": _extract_summary_text(summary_map, "accountcode") or account_id,
        "account_type": _extract_summary_text(summary_map, "accounttype"),
        "net_liquidation": _extract_summary_number(summary_map, "netliquidation", "netliq"),
        "available_funds": _extract_summary_number(summary_map, "availablefunds"),
        "buying_power": _extract_summary_number(summary_map, "buyingpower"),
        "excess_liquidity": _extract_summary_number(summary_map, "excessliquidity"),
        "equity_with_loan": _extract_summary_number(summary_map, "equitywithloanvalue"),
        "gross_position_value": _extract_summary_number(summary_map, "grosspositionvalue", "stockmarketvalue") or total_market_value,
        "total_cash_value": _extract_summary_number(summary_map, "totalcashvalue", "cashbalance", "settledcash"),
        "initial_margin": _extract_summary_number(summary_map, "initmarginreq"),
        "maintenance_margin": _extract_summary_number(summary_map, "maintmarginreq"),
        "unrealized_pnl": _extract_summary_number(summary_map, "unrealizedpnl") or total_unrealized,
        "realized_pnl": _extract_summary_number(summary_map, "realizedpnl"),
        "daily_pnl": account_today_pnl["net"],
        "today_pnl": account_today_pnl["net"],
        "daily_pnl_available": bool(account_today_pnl["ok"]),
        "account_today_pnl": account_today_pnl,
        "currency": currency,
    }
    return enrich_buying_power_summary(summary)


def _position_counts(positions: list[dict]) -> dict:
    long_positions = 0
    short_positions = 0
    flat_positions = 0
    for item in positions or []:
        quantity = float(item.get("quantity", 0) or 0)
        if quantity > 0:
            long_positions += 1
        elif quantity < 0:
            short_positions += 1
        else:
            flat_positions += 1
    return {
        "positions": len(positions),
        "position_rows": len(positions),
        "open_positions": long_positions + short_positions,
        "long_positions": long_positions,
        "short_positions": short_positions,
        "flat_positions": flat_positions,
    }


def _build_snapshot_counts(positions: list[dict], orders: list[dict], live_open_orders: list[dict]) -> dict:
    position_counts = _position_counts(positions)
    return {
        **position_counts,
        "orders": len(orders),
        "open_orders": len(live_open_orders),
        "cancelable_orders": len([item for item in live_open_orders if item.get("can_cancel")]),
        "editable_orders": len([item for item in live_open_orders if item.get("can_modify")]),
        "outside_rth_orders": len([item for item in live_open_orders if item.get("outside_rth")]),
        "recovered_open_orders": len([
            item for item in live_open_orders
            if str(item.get("recovery_source") or "").strip() == "status_recovered"
        ]),
    }


def _merge_position_counts(base_counts: dict, cached_counts: dict, *, available: bool) -> dict:
    merged = dict(base_counts or {})
    if not available:
        return merged
    for key in ("positions", "position_rows", "open_positions", "long_positions", "short_positions", "flat_positions"):
        if key in (cached_counts or {}):
            merged[key] = cached_counts.get(key)
    if "position_rows" not in merged and "positions" in merged:
        merged["position_rows"] = merged.get("positions")
    return merged


def _buying_power_unavailable_reason(service_status: dict, summary_error: str, summary: dict) -> str:
    status = service_status if isinstance(service_status, dict) else {}
    gateway = status.get("gateway") if isinstance(status.get("gateway"), dict) else {}
    session = status.get("session") if isinstance(status.get("session"), dict) else {}
    if gateway and not bool(gateway.get("running") or gateway.get("reachable")):
        return "gateway_unavailable"
    if session and session.get("authenticated") is False:
        return "session_unauthenticated"
    if summary_error:
        return "account_snapshot_unavailable"
    if not isinstance(summary, dict) or not summary:
        return "account_snapshot_unavailable"
    return "buying_power_unavailable"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return float(number)


def _account_data_circuit_status(service_status: dict) -> dict:
    status = service_status if isinstance(service_status, dict) else {}
    circuit = status.get("account_data_circuit") if isinstance(status.get("account_data_circuit"), dict) else {}
    if circuit:
        return dict(circuit)
    gateway = status.get("gateway") if isinstance(status.get("gateway"), dict) else {}
    broker = gateway.get("broker") if isinstance(gateway.get("broker"), dict) else {}
    circuit = broker.get("account_data_circuit") if isinstance(broker.get("account_data_circuit"), dict) else {}
    return dict(circuit) if circuit else {}


def _account_data_pacing_status(service_status: dict) -> dict:
    status = service_status if isinstance(service_status, dict) else {}
    pacing = status.get("account_data_pacing") if isinstance(status.get("account_data_pacing"), dict) else {}
    if pacing:
        return dict(pacing)
    gateway = status.get("gateway") if isinstance(status.get("gateway"), dict) else {}
    pacing = gateway.get("account_data_pacing") if isinstance(gateway.get("account_data_pacing"), dict) else {}
    if pacing:
        return dict(pacing)
    broker = gateway.get("broker") if isinstance(gateway.get("broker"), dict) else {}
    pacing = broker.get("account_data_pacing") if isinstance(broker.get("account_data_pacing"), dict) else {}
    return dict(pacing) if pacing else {}


def _summary_snapshot_available(summary: dict) -> bool:
    if not isinstance(summary, dict) or not summary:
        return False
    for key in (
        "remaining_buying_power",
        "buying_power",
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "equity_with_loan",
    ):
        raw = summary.get(key)
        if raw in (None, ""):
            continue
        number = _safe_float(raw, 0.0)
        if number != 0.0:
            return True
    return bool(str(summary.get("account_type") or "").strip())


def _numeric_summary_keys() -> tuple[str, ...]:
    return (
        "remaining_buying_power",
        "buying_power",
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "equity_with_loan",
        "gross_position_value",
        "total_cash_value",
        "initial_margin",
        "maintenance_margin",
    )


def _mask_unavailable_summary_values(summary: dict) -> dict:
    masked = dict(summary or {})
    if not masked or _summary_snapshot_available(masked):
        return masked
    for key in _numeric_summary_keys():
        if key in masked:
            masked[key] = None
    return masked


def _reservation_snapshot(service) -> dict:
    store = getattr(service, "buying_power_reservations", None)
    snapshotter = getattr(store, "snapshot", None)
    if not callable(snapshotter):
        return {}
    try:
        snapshot = snapshotter()
    except Exception:
        return {}
    return dict(snapshot) if isinstance(snapshot, dict) else {}


def _update_buying_power_baseline_from_payload(service, payload: dict) -> None:
    if not isinstance(payload, dict) or not payload:
        return
    reservations = getattr(service, "buying_power_reservations", None)
    updater = getattr(reservations, "update_baseline_from_snapshot", None)
    if not callable(updater):
        return
    try:
        updater(payload)
    except Exception:
        return


def _reservation_overlay_base_summary(summary: dict) -> dict:
    base = dict(summary or {})
    existing_reserved = max(0.0, _safe_float(base.get("local_reserved_exposure"), 0.0))
    if existing_reserved > 0:
        for key in ("remaining_buying_power", "buying_power"):
            current = base.get(key)
            if current not in (None, ""):
                base[key] = _safe_float(current, 0.0) + existing_reserved
    for key in ("local_reserved_exposure", "local_reserved_count", "local_reserved_order_ids"):
        base.pop(key, None)
    return base


def _apply_reservation_overlay_to_account_payload(service, payload: dict) -> dict:
    result = dict(payload or {})
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if not _summary_snapshot_available(summary):
        result["summary"] = _mask_unavailable_summary_values(summary)
        return _decorate_account_snapshot_health(result)
    reservation_snapshot = _reservation_snapshot(service)
    adjusted_summary = apply_reservations_to_buying_power_summary(
        _reservation_overlay_base_summary(summary),
        reservation_snapshot,
    )
    result["summary"] = adjusted_summary
    guard = build_buying_power_guard(
        adjusted_summary,
        config=getattr(service, "config", None),
        environment=result.get("environment") or "",
    )
    previous_guard = result.get("buying_power_guard") if isinstance(result.get("buying_power_guard"), dict) else {}
    for key in ("source", "snapshot_error"):
        if previous_guard.get(key) not in (None, ""):
            guard[key] = previous_guard.get(key)
    merge_reservation_snapshot_into_guard(guard, reservation_snapshot)
    result["buying_power_guard"] = guard
    return _decorate_account_snapshot_health(result)


def _decorate_account_snapshot_health(payload: dict, *, reason: str = "") -> dict:
    result = payload if isinstance(payload, dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    guard = result.get("buying_power_guard") if isinstance(result.get("buying_power_guard"), dict) else {}
    pacing = result.get("account_data_pacing") if isinstance(result.get("account_data_pacing"), dict) else {}
    summary_available = _summary_snapshot_available(summary)
    guard_state = str(guard.get("state") or "").strip().lower()
    health = "ok"
    health_reason = str(reason or "").strip() or "ok"
    if result.get("stale") or str(result.get("cache_state") or "").strip() in {"stale_after_error", "empty_error"}:
        health = "stale" if summary_available else "unavailable"
        health_reason = str(result.get("refresh_error") or reason or "account_snapshot_stale")
    elif result.get("ok") is False or guard_state == "unavailable" or not summary_available:
        health = "unavailable"
        health_reason = str(guard.get("reason") or reason or "account_snapshot_unavailable")
    result["summary_available"] = bool(summary_available)
    result["account_snapshot_health"] = {
        "state": health,
        "reason": health_reason,
        "summary_available": bool(summary_available),
        "buying_power_guard_state": guard_state or "",
        "source": str(result.get("source") or ""),
        "cache_state": str(result.get("cache_state") or ""),
        "cache_age_s": result.get("cache_age_s"),
        "retry_after_s": result.get("retry_after_s"),
    }
    result["account_data_policy"] = {
        "profile": str(result.get("snapshot_profile") or result.get("source") or "account_snapshot"),
        "served_from": str(result.get("source") or result.get("cache_state") or ""),
        "cache_age_s": result.get("cache_age_s"),
        "stale": bool(result.get("stale")),
        "broker_refresh_allowed": False,
        "pacing_blocked": bool(pacing.get("blocked") or result.get("pacing_blocked")),
        "retry_after_s": result.get("retry_after_s") or pacing.get("retry_after_s"),
        "health_state": health,
        "health_reason": health_reason,
    }
    return result


def _buying_power_full_snapshot_usable(payload: dict, *, allow_stale: bool = False) -> bool:
    health = payload.get("account_snapshot_health") if isinstance(payload.get("account_snapshot_health"), dict) else {}
    state = str(health.get("state") or "").strip().lower()
    if state == "ok":
        return True
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    guard_state = str(guard.get("state") or "").strip().lower()
    return bool(
        allow_stale
        and state == "stale"
        and guard.get("available") is True
        and guard_state in {"ok", "warning", "blocked"}
    )


def _fresh_full_snapshot_for_buying_power(api_app, context: dict, *, allow_stale: bool = False) -> dict:
    candidate_keys = [
        context["cache_key"],
        (context["runtime_environment"], context["account_id"], True),
    ]
    cached = {}
    for cache_key in candidate_keys:
        cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
        if isinstance(cached, dict):
            break
    if not isinstance(cached, dict) or not cached:
        return {}
    payload = _decorate_account_snapshot_health(dict(cached))
    if not _buying_power_full_snapshot_usable(payload, allow_stale=allow_stale):
        return {}
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    guard = dict(guard)
    guard.setdefault("source", "account_snapshot")
    payload["buying_power_guard"] = guard
    payload["source"] = "account_snapshot"
    return payload


def _build_buying_power_payload_from_full_snapshot(full_snapshot: dict, context: dict, *, allow_stale: bool = False) -> dict:
    if not isinstance(full_snapshot, dict) or not full_snapshot:
        return {}
    payload = _decorate_account_snapshot_health(dict(full_snapshot))
    if not _buying_power_full_snapshot_usable(payload, allow_stale=allow_stale):
        return {}
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    guard = dict(guard)
    guard.setdefault("source", "account_snapshot")
    cache_age_s = payload.get("cache_age_s")
    stale = bool(payload.get("stale"))
    if stale and _safe_float(cache_age_s, 0.0) > _buying_power_guard_max_stale_seconds():
        guard["available"] = False
        guard["state"] = "unavailable"
        guard["reason"] = "account_snapshot_stale"
        guard["snapshot_error"] = "account_snapshot_stale"
        guard["stale_blocked"] = True
    return _decorate_account_snapshot_health(
        {
            "ok": bool(payload.get("ok", True)),
            "environment": payload.get("environment") or context["runtime_environment"],
            "account_id": payload.get("account_id") or context["account_id"],
            "service_running": bool(payload.get("service_running")),
            "service_starting": bool(payload.get("service_starting")),
            "session_authenticated": bool(payload.get("session_authenticated")),
            "gateway_running": bool(payload.get("gateway_running")),
            "summary": dict(payload.get("summary") or {}),
            "buying_power_guard": guard,
            "summary_raw": dict(payload.get("summary_raw") or {}),
            "errors": dict(payload.get("errors") or {}),
            "fetched_at": payload.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
            "source": "account_snapshot",
            "snapshot_source": payload.get("source") or "account_snapshot",
            "stale": stale,
            "cache_state": payload.get("cache_state"),
            "cache_age_s": cache_age_s,
            "refresh_error": payload.get("refresh_error"),
            "account_data_pacing": dict(payload.get("account_data_pacing") or _account_data_pacing_status(context["service_status"])),
        }
    )


def _build_buying_power_payload_from_summary_raw(service, context: dict, summary_raw: dict, summary_error: str = "") -> dict:
    if not isinstance(summary_raw, dict):
        summary_raw = {}
    summary = _build_snapshot_summary(summary_raw, context["account_id"], [], {})
    if not _summary_snapshot_available(summary):
        summary = _mask_unavailable_summary_values(summary)
    guard = build_buying_power_guard(
        summary,
        config=getattr(service, "config", None),
        environment=context["runtime_environment"],
    )
    guard["source"] = "account_summary"
    if guard.get("state") == "unavailable":
        guard["reason"] = _buying_power_unavailable_reason(
            context["service_status"],
            summary_error,
            summary,
        )
        guard["snapshot_error"] = summary_error or guard["reason"]

    payload = {
        "ok": bool(guard.get("available")),
        "environment": context["runtime_environment"],
        "account_id": context["account_id"],
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((context["service_status"].get("session") or {}).get("authenticated")),
        "gateway_running": bool((context["service_status"].get("gateway") or {}).get("running")),
        "summary": summary,
        "buying_power_guard": guard,
        "summary_raw": summary_raw,
        "errors": {"summary": summary_error},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "account_summary",
        "account_data_pacing": _account_data_pacing_status(context["service_status"]),
    }
    return _decorate_account_snapshot_health(payload)


def _fetch_account_summary_raw_for_buying_power(service, context: dict) -> tuple[dict, str]:
    lifecycle = getattr(service, "order_lifecycle", None)
    getter = getattr(lifecycle, "get_account_summary", None)
    if not callable(getter):
        return {}, "account_summary_unavailable"
    try:
        value = getter(context["account_id"])
        summary_raw = value if isinstance(value, dict) else {}
    except Exception as exc:
        return {}, str(exc)
    if not summary_raw:
        return {}, "account_summary_unavailable"
    return summary_raw, ""


def _build_account_data_circuit_buying_power_snapshot(service, context: dict, circuit: dict) -> dict:
    retry_after_s = _safe_float((circuit or {}).get("remaining_s"), 0.0)
    reason = "account_data_circuit_open"
    guard = build_buying_power_guard({}, config=getattr(service, "config", None), environment=context["runtime_environment"])
    guard.update(
        {
            "available": False,
            "state": "unavailable",
            "reason": reason,
            "source": "account_data_circuit",
            "snapshot_error": reason,
            "retry_after_s": retry_after_s,
        }
    )
    payload = {
        "ok": False,
        "environment": context["runtime_environment"],
        "account_id": context["account_id"],
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((context["service_status"].get("session") or {}).get("authenticated")),
        "gateway_running": bool((context["service_status"].get("gateway") or {}).get("running")),
        "summary": {},
        "buying_power_guard": guard,
        "summary_raw": {},
        "errors": {"summary": reason},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "account_data_circuit",
        "account_data_circuit": dict(circuit or {}),
        "account_data_pacing": _account_data_pacing_status(context["service_status"]),
        "retry_after_s": retry_after_s,
    }
    return _decorate_account_snapshot_health(payload, reason=reason)


def _is_account_data_circuit_buying_power_snapshot(payload: dict) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    reason = str(guard.get("reason") or payload.get("source") or payload.get("refresh_error") or "").strip().lower()
    source = str(guard.get("source") or payload.get("source") or "").strip().lower()
    return "account_data_circuit" in reason or source == "account_data_circuit"


def _stale_buying_power_snapshot_after_error(api_app, cache_key: tuple, error: str) -> dict:
    cached = load_cached_snapshot(api_app, cache_key, allow_stale=True)
    if not isinstance(cached, dict) or not cached:
        return {}
    guard = cached.get("buying_power_guard") if isinstance(cached.get("buying_power_guard"), dict) else {}
    if not bool(guard.get("available")):
        return {}
    payload = dict(cached)
    payload["ok"] = True
    payload["stale"] = True
    payload["cache_state"] = "stale_after_error"
    payload["refresh_error"] = str(error or "").strip() or "buying_power_refresh_unavailable"
    errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    payload["errors"] = {**errors, "refresh": payload["refresh_error"]}
    return _decorate_account_snapshot_health(payload, reason=payload["refresh_error"])


def _build_ibkr_account_buying_power_snapshot(service) -> dict:
    context = build_snapshot_context(service, include_pnl=False, fast_status=True)
    api_app = context["api_app"]
    cache_key = (context["runtime_environment"], f"{context['account_id']}::buying_power", False)

    def with_baseline(payload: dict) -> dict:
        _update_buying_power_baseline_from_payload(service, payload)
        return payload

    cached = load_cached_snapshot(api_app, cache_key)
    if cached and not _is_account_data_circuit_buying_power_snapshot(cached):
        return with_baseline(_decorate_account_snapshot_health(cached))

    refresh_lock = get_snapshot_refresh_lock(api_app, cache_key)
    with refresh_lock:
        cached = load_cached_snapshot(api_app, cache_key)
        if cached and not _is_account_data_circuit_buying_power_snapshot(cached):
            return with_baseline(_decorate_account_snapshot_health(cached))

        full_cached = _fresh_full_snapshot_for_buying_power(api_app, context)
        payload = _build_buying_power_payload_from_full_snapshot(full_cached, context)
        if payload:
            store_cached_snapshot(api_app, cache_key, payload)
            return with_baseline(load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload)

        circuit = _account_data_circuit_status(context.get("service_status") or {})
        if bool(circuit.get("active")):
            stale_full_cached = _fresh_full_snapshot_for_buying_power(api_app, context, allow_stale=True)
            stale_full_payload = _build_buying_power_payload_from_full_snapshot(
                stale_full_cached,
                context,
                allow_stale=True,
            )
            if stale_full_payload:
                stale_full_payload["cache_state"] = "stale_after_account_data_circuit"
                stale_full_payload["stale"] = True
                stale_full_payload["refresh_error"] = "account_data_circuit_open"
                stale_full_payload["account_data_circuit"] = dict(circuit or {})
                return with_baseline(stale_full_payload)
            stale_payload = _stale_buying_power_snapshot_after_error(api_app, cache_key, "account_data_circuit_open")
            if stale_payload:
                return with_baseline(stale_payload)
            return with_baseline(_build_account_data_circuit_buying_power_snapshot(service, context, circuit))

        full_refreshed = _build_ibkr_account_snapshot(
            service,
            include_pnl=False,
            force_refresh=False,
            allow_stale=True,
            fast_status=True,
            apply_reservation_overlay=False,
        )
        full_payload = _build_buying_power_payload_from_full_snapshot(
            full_refreshed,
            context,
            allow_stale=bool((full_refreshed or {}).get("stale")),
        )
        if full_payload:
            if bool((full_refreshed or {}).get("stale")):
                full_payload["stale"] = True
                full_payload.setdefault("cache_state", "stale")
                if not full_payload.get("refresh_error"):
                    full_payload["refresh_error"] = str((full_refreshed or {}).get("refresh_error") or "stale_full_snapshot")
                return with_baseline(_decorate_account_snapshot_health(full_payload))
            store_cached_snapshot(api_app, cache_key, full_payload)
            return with_baseline(load_cached_snapshot(api_app, cache_key, allow_stale=False) or full_payload)

        if not _env_bool("IBKR_ACCOUNT_SUMMARY_FALLBACK_ENABLED", False):
            stale_payload = _stale_buying_power_snapshot_after_error(
                api_app,
                cache_key,
                "account_summary_fallback_disabled",
            )
            if stale_payload:
                return with_baseline(stale_payload)
            payload = _build_buying_power_payload_from_summary_raw(
                service,
                context,
                {},
                "account_summary_fallback_disabled",
            )
            store_cached_snapshot(api_app, cache_key, payload)
            return with_baseline(load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload)

        summary_raw, summary_error = _fetch_account_summary_raw_for_buying_power(service, context)
        payload = _build_buying_power_payload_from_summary_raw(service, context, summary_raw, summary_error)
        if (payload.get("buying_power_guard") or {}).get("state") == "unavailable":
            stale_payload = _stale_buying_power_snapshot_after_error(
                api_app,
                cache_key,
                (payload.get("buying_power_guard") or {}).get("snapshot_error") or summary_error,
            )
            if stale_payload:
                return with_baseline(stale_payload)
            full_refreshed = _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=False,
                allow_stale=False,
                fast_status=True,
                apply_reservation_overlay=False,
            )
            full_payload = _build_buying_power_payload_from_full_snapshot(full_refreshed, context)
            if full_payload:
                store_cached_snapshot(api_app, cache_key, full_payload)
                return with_baseline(load_cached_snapshot(api_app, cache_key, allow_stale=False) or full_payload)
        store_cached_snapshot(api_app, cache_key, payload)
        return with_baseline(load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload)


def _stale_snapshot_after_error(payload: dict, error: str) -> dict:
    result = dict(payload or {})
    result["ok"] = bool(result.get("ok", True))
    result["stale"] = True
    result["cache_state"] = "stale_after_error"
    result["refresh_error"] = str(error or "").strip() or "account_snapshot_refresh_failed"
    errors = result.get("errors") if isinstance(result.get("errors"), dict) else {}
    result["errors"] = {**errors, "refresh": result["refresh_error"]}
    return _decorate_account_snapshot_health(result, reason=result["refresh_error"])


def _account_snapshot_error_payload(context: dict, error: str) -> dict:
    message = str(error or "").strip() or "account_snapshot_unavailable"
    service_status = context["service_status"] if isinstance(context.get("service_status"), dict) else {}
    return _decorate_account_snapshot_health({
        "ok": False,
        "environment": context["runtime_environment"],
        "account_id": context["account_id"],
        "service_running": False,
        "service_starting": False,
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "websocket_ready": bool((service_status.get("websocket") or {}).get("ready")),
        "summary": {},
        "buying_power_guard": {"available": False, "state": "unavailable", "reason": "account_snapshot_unavailable"},
        "summary_raw": {},
        "pnl_raw": {},
        "include_pnl": bool(context.get("include_pnl")),
        "positions": None,
        "orders": None,
        "live_open_orders": None,
        "live_order_coverage": {},
        "recovery_diagnostics": {},
        "counts": {},
        "errors": {"summary": message, "positions": message, "orders": message, "refresh": message},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cache_state": "empty_error",
        "stale": False,
        "refresh_error": message,
        "account_data_pacing": _account_data_pacing_status(service_status),
    }, reason=message)


def _build_fresh_ibkr_account_snapshot_payload(service, context: dict) -> dict:
    api_app = context["api_app"]
    snapshot_sources = fetch_snapshot_sources(
        service,
        context["account_id"],
        include_pnl=context["include_pnl"],
    )
    source_errors = snapshot_sources.get("errors") if isinstance(snapshot_sources.get("errors"), dict) else {}
    if source_errors.get("positions") and not bool(snapshot_sources.get("positions_loaded")):
        raise RuntimeError(str(source_errors.get("positions") or "account_positions_unavailable"))
    fallback_rows_raw = load_pb_fallback_order_rows(api_app, service)
    fallback_rows, fallback_trust = filter_trusted_pb_fallback_order_rows(
        service,
        fallback_rows_raw,
        broker_rows=snapshot_sources["orders_raw"] if isinstance(snapshot_sources.get("orders_raw"), list) else [],
    )
    fallback_ids = [
        str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        for row in fallback_rows
        if isinstance(row, dict) and str(row.get("broker_order_id") or row.get("order_id") or "").strip()
    ]
    merged_orders_raw, live_open_payload = recover_live_open_orders(
        api_app,
        service,
        snapshot_sources["orders_raw"],
        fallback_ids,
        fallback_rows=fallback_rows,
    )
    diagnostics = live_open_payload.get("diagnostics") if isinstance(live_open_payload.get("diagnostics"), dict) else {}
    diagnostics["pb_fallback_trust"] = fallback_trust
    live_open_payload["diagnostics"] = diagnostics
    positions, orders, live_open_orders, live_open_payload = _normalize_snapshot_rows(
        context["account_id"],
        snapshot_sources["positions_raw"],
        merged_orders_raw,
        live_open_payload,
        fallback_ids,
    )
    inferred_positions, positions_inference = _position_inference_payload(positions, orders, live_open_orders)

    summary = _build_snapshot_summary(
        snapshot_sources["summary_raw"],
        context["account_id"],
        positions,
        snapshot_sources.get("pnl_raw") if isinstance(snapshot_sources.get("pnl_raw"), dict) else {},
    )
    if not _summary_snapshot_available(summary):
        summary = _mask_unavailable_summary_values(summary)
        source_errors = dict(snapshot_sources["errors"])
        source_errors["summary"] = source_errors.get("summary") or "account_summary_unavailable"
        snapshot_sources["errors"] = source_errors
    guard = build_buying_power_guard(
        summary,
        config=getattr(service, "config", None),
        environment=context["runtime_environment"],
    )
    guard.setdefault("source", "account_snapshot")
    payload = {
        "ok": True,
        "environment": context["runtime_environment"],
        "account_id": context["account_id"],
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((context["service_status"].get("session") or {}).get("authenticated")),
        "gateway_running": bool((context["service_status"].get("gateway") or {}).get("running")),
        "websocket_ready": bool((context["service_status"].get("websocket") or {}).get("ready")),
        "summary": summary,
        "buying_power_guard": guard,
        "summary_raw": snapshot_sources["summary_raw"] if isinstance(snapshot_sources["summary_raw"], dict) else {},
        "pnl_raw": snapshot_sources.get("pnl_raw") if isinstance(snapshot_sources.get("pnl_raw"), dict) else {},
        "include_pnl": bool(context["include_pnl"]),
        "positions": positions,
        "orders": orders,
        "live_open_orders": live_open_orders,
        "live_order_coverage": live_open_payload.get("coverage") or {},
        "recovery_diagnostics": live_open_payload.get("diagnostics") or {},
        "counts": _merge_inferred_position_counts(
            _build_snapshot_counts(positions, orders, live_open_orders),
            inferred_positions,
        ),
        "positions_detail_available": True,
        "positions_source": "account_snapshot",
        "inferred_strategy_positions": inferred_positions,
        "positions_inference": positions_inference,
        "errors": snapshot_sources["errors"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "account_snapshot",
        "account_data_pacing": _account_data_pacing_status(context["service_status"]),
    }
    return _decorate_account_snapshot_health(payload)


def _load_cached_full_snapshot_for_orders_fast(api_app, context: dict) -> dict:
    seen: set[tuple] = set()
    candidate_keys = [
        context["cache_key"],
        (context["runtime_environment"], context["account_id"], False),
        (context["runtime_environment"], context["account_id"], True),
    ]
    for cache_key in candidate_keys:
        if cache_key in seen:
            continue
        seen.add(cache_key)
        cached = load_cached_snapshot(api_app, cache_key, allow_stale=True)
        if isinstance(cached, dict) and cached:
            decorated = _decorate_account_snapshot_health(dict(cached))
            health = decorated.get("account_snapshot_health") if isinstance(decorated.get("account_snapshot_health"), dict) else {}
            health_state = str(health.get("state") or "").strip().lower()
            if health_state in {"ok", "stale"} and bool(decorated.get("summary_available")):
                return decorated
    return {}


def _cached_live_order_rows(service, *, include_all: bool = True) -> tuple[list[dict], str]:
    tracker = getattr(service, "order_tracker", None)
    getter = getattr(tracker, "get_cached_live_orders", None)
    if not callable(getter):
        return [], "unavailable"
    try:
        rows = list(getter(include_all=include_all) or [])
    except TypeError:
        try:
            rows = list(getter() or [])
        except Exception:
            return [], "error"
    except Exception:
        return [], "error"
    return [dict(item) for item in rows if isinstance(item, dict)], "callback_cache"


def _order_identity(order: dict) -> str:
    if not isinstance(order, dict):
        return ""
    return str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip()


def _merge_cached_and_pb_order_rows(cached_rows: list[dict], fallback_rows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for item in cached_rows or []:
        order_id = _order_identity(item)
        if order_id:
            if order_id in seen:
                continue
            seen.add(order_id)
        merged.append(dict(item))
    for item in pb_order_rows_to_live_orders(fallback_rows):
        order_id = _order_identity(item)
        if order_id and order_id in seen:
            continue
        if order_id:
            seen.add(order_id)
        merged.append(item)
    return merged


def _build_orders_fast_live_open_payload(cached_rows: list[dict], fallback_rows: list[dict]) -> dict:
    pb_orders = pb_order_rows_to_live_orders(fallback_rows)
    cached_ids = [_order_identity(item) for item in cached_rows or [] if _order_identity(item)]
    pb_ids = [_order_identity(item) for item in pb_orders if _order_identity(item)]
    open_like_cached = [
        item for item in cached_rows or []
        if str(item.get("status") or item.get("order_status") or item.get("orderStatus") or "").strip().upper()
        not in {"", "FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}
    ]
    orders = _merge_cached_and_pb_order_rows(open_like_cached, fallback_rows)
    return {
        "orders": orders,
        "coverage": {
            "coverage_state": "cache_only",
            "bulk_open_count": len(open_like_cached),
            "recovered_from_status_count": 0,
            "tracker_seed_count": 0,
            "pb_seed_count": len(pb_ids),
            "unresolved_seed_count": 0,
            "unresolved_order_ids": [],
        },
        "diagnostics": {
            "seed_sources": {order_id: ["pb"] for order_id in pb_ids},
            "recovered_order_ids": [],
            "resolved_closed_order_ids": [],
            "bulk_order_ids": cached_ids,
            "cached_order_ids": cached_ids,
            "pb_seed_order_ids": pb_ids,
            "cache_only": True,
        },
    }


def _order_side_direction(order: dict) -> str:
    side = str((order or {}).get("side") or "").strip().upper()
    if side in {"BUY", "BOT"}:
        return "long"
    if side in {"SELL", "SLD", "SSHORT"}:
        return "short"
    text = " ".join(
        str(value or "").strip().lower()
        for value in (
            (order or {}).get("client_order_id"),
            ((order or {}).get("raw") or {}).get("cOID") if isinstance((order or {}).get("raw"), dict) else "",
            ((order or {}).get("raw") or {}).get("orderRef") if isinstance((order or {}).get("raw"), dict) else "",
        )
        if str(value or "").strip()
    )
    if "_short_" in text or text.endswith("_short") or " short " in text:
        return "short"
    if "_long_" in text or text.endswith("_long") or " long " in text:
        return "long"
    return ""


def _order_strategy_group(order: dict) -> str:
    raw = (order or {}).get("raw") if isinstance((order or {}).get("raw"), dict) else {}
    candidates = (
        (order or {}).get("trade_group_id"),
        (order or {}).get("bracket_group"),
        (order or {}).get("client_order_id"),
        raw.get("trade_group_id"),
        raw.get("bracket_group"),
        raw.get("cOID"),
        raw.get("coid"),
        raw.get("orderRef"),
        raw.get("order_ref"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        lower = text.lower()
        for prefix in ("entry_", "tp_", "sl_", "stop_", "take_profit_", "stop_loss_"):
            if lower.startswith(prefix):
                return text[len(prefix):]
        return text
    return ""


def _strategy_direction_from_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if "_short_" in text or text.endswith("_short") or " short " in text:
        return "short"
    if "_long_" in text or text.endswith("_long") or " long " in text:
        return "long"
    return ""


def _protection_implied_position_direction(order: dict) -> str:
    raw = (order or {}).get("raw") if isinstance((order or {}).get("raw"), dict) else {}
    for value in (
        (order or {}).get("trade_group_id"),
        (order or {}).get("bracket_group"),
        (order or {}).get("client_order_id"),
        raw.get("trade_group_id"),
        raw.get("bracket_group"),
        raw.get("cOID"),
        raw.get("coid"),
        raw.get("orderRef"),
        raw.get("order_ref"),
    ):
        direction = _strategy_direction_from_text(str(value or ""))
        if direction:
            return direction
    side = str((order or {}).get("side") or "").strip().upper()
    if side in {"BUY", "BOT"}:
        return "short"
    if side in {"SELL", "SLD", "SSHORT"}:
        return "long"
    return ""


def _protection_price(order: dict) -> float:
    role = str((order or {}).get("role") or "").strip().lower()
    if role == "stop_loss":
        return _safe_float((order or {}).get("trigger_price"), 0.0) or _safe_float((order or {}).get("price"), 0.0)
    return _safe_float((order or {}).get("price"), 0.0) or _safe_float((order or {}).get("trigger_price"), 0.0)


def _dedupe_orders_by_id(orders: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        order_id = str(order.get("order_id") or "").strip()
        dedupe_key = order_id or f"{order.get('role')}:{order.get('client_order_id')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(order)
    return deduped


def _inferred_position_from_entry(entry: dict, children: list[dict], *, direction: str, quantity: float, group: str) -> dict:
    tp_orders = [item for item in children if str(item.get("role") or "").strip().lower() == "take_profit"]
    sl_orders = [item for item in children if str(item.get("role") or "").strip().lower() == "stop_loss"]
    signed_quantity = quantity if direction == "long" else -quantity
    avg_price = _safe_float(entry.get("avg_price"), 0.0) or _safe_float(entry.get("price"), 0.0)
    market_value = abs(signed_quantity * avg_price) if avg_price > 0 else 0.0
    relation_reason = (
        "filled_entry_with_open_protection_orders"
        if str(entry.get("status_key") or "").strip().upper() == "FILLED"
        else "open_protection_orders_without_filled_entry"
    )
    relation = {
        "status": "inferred_strategy_position",
        "reason": relation_reason,
        "signal_id": str(((entry.get("raw") or {}) if isinstance(entry.get("raw"), dict) else {}).get("signal_id") or ""),
        "trade_group_id": group,
        "entry_order_unique_id": str(entry.get("client_order_id") or ""),
        "last_order_status": str(entry.get("status") or ""),
        "order_count": (1 if entry.get("order_id") else 0) + len(children),
        "entry_filled_qty": quantity if str(entry.get("status_key") or "").strip().upper() == "FILLED" else 0.0,
        "exit_filled_qty": 0.0,
        "commission": _safe_float(entry.get("commission"), 0.0),
        "commission_currency": str(entry.get("commission_currency") or entry.get("currency") or "USD").upper(),
        "commission_known": _safe_float(entry.get("commission"), 0.0) > 0,
        "commission_source": "broker_order",
        "commission_fill_count": 1 if _safe_float(entry.get("commission"), 0.0) > 0 else 0,
    }
    return {
        "symbol": str(entry.get("symbol") or "").strip().upper(),
        "conid": int(_safe_float(entry.get("conid"), 0.0) or 0),
        "quantity": signed_quantity,
        "direction": direction,
        "avg_cost": avg_price,
        "avg_price": avg_price,
        "market_price": 0.0,
        "market_value": market_value,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "account": str(entry.get("account") or ""),
        "currency": str(entry.get("currency") or "USD").upper(),
        "asset_class": str(entry.get("asset_class") or "STK").upper() or "STK",
        "source": "orders_fast_inferred",
        "inferred": True,
        "inference_confidence": "high" if tp_orders and sl_orders and relation_reason.startswith("filled_entry") else "medium",
        "entry_order_id": str(entry.get("order_id") or ""),
        "entry_client_order_id": str(entry.get("client_order_id") or ""),
        "trade_group_id": group,
        "protection_status": "complete" if tp_orders and sl_orders else "partial",
        "take_profit_order_id": str((tp_orders[0] or {}).get("order_id") or "") if tp_orders else "",
        "take_profit_price": _protection_price(tp_orders[0]) if tp_orders else 0.0,
        "stop_loss_order_id": str((sl_orders[0] or {}).get("order_id") or "") if sl_orders else "",
        "stop_loss_price": _protection_price(sl_orders[0]) if sl_orders else 0.0,
        "open_protection_order_ids": [
            str(item.get("order_id") or "").strip()
            for item in children
            if str(item.get("order_id") or "").strip()
        ],
        "relation": relation,
    }


def _infer_strategy_positions_from_orders(orders: list[dict], live_open_orders: list[dict]) -> list[dict]:
    all_orders = [dict(item) for item in (orders or []) if isinstance(item, dict)]
    if not all_orders:
        return []
    open_ids = {
        str(item.get("order_id") or "").strip()
        for item in (live_open_orders or [])
        if isinstance(item, dict) and str(item.get("order_id") or "").strip()
    }
    open_children = [
        item
        for item in all_orders
        if item.get("is_open") or (str(item.get("order_id") or "").strip() in open_ids)
    ]
    children_by_parent: dict[str, list[dict]] = {}
    children_by_group: dict[str, list[dict]] = {}
    open_parent_entry_ids = set()
    for child in open_children:
        role = str(child.get("role") or "").strip().lower()
        if role == "entry":
            order_id = str(child.get("order_id") or "").strip()
            if order_id:
                open_parent_entry_ids.add(order_id)
            continue
        if role not in {"take_profit", "stop_loss", "child"}:
            continue
        parent_id = str(child.get("parent_id") or "").strip()
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(child)
        group = _order_strategy_group(child)
        if group:
            children_by_group.setdefault(group, []).append(child)

    inferred = []
    seen_groups: set[str] = set()
    for entry in all_orders:
        if str(entry.get("role") or "").strip().lower() != "entry":
            continue
        if str(entry.get("status_key") or "").strip().upper() != "FILLED":
            continue
        direction = _order_side_direction(entry)
        if direction not in {"long", "short"}:
            continue
        quantity = _safe_float(entry.get("filled_quantity"), 0.0) or _safe_float(entry.get("total_quantity"), 0.0)
        if quantity <= 0:
            continue
        order_id = str(entry.get("order_id") or "").strip()
        group = _order_strategy_group(entry)
        if group and group in seen_groups:
            continue
        candidates = []
        if order_id:
            candidates.extend(children_by_parent.get(order_id, []))
        if group:
            candidates.extend(children_by_group.get(group, []))
        deduped_children = []
        seen_child_ids = set()
        for child in candidates:
            child_id = str(child.get("order_id") or "").strip()
            dedupe_key = child_id or f"{child.get('role')}:{child.get('client_order_id')}"
            if dedupe_key in seen_child_ids:
                continue
            seen_child_ids.add(dedupe_key)
            if str(child.get("symbol") or "").strip().upper() == str(entry.get("symbol") or "").strip().upper():
                deduped_children.append(child)
        if not deduped_children:
            continue
        tp_orders = [item for item in deduped_children if str(item.get("role") or "").strip().lower() == "take_profit"]
        sl_orders = [item for item in deduped_children if str(item.get("role") or "").strip().lower() == "stop_loss"]
        inferred.append(
            _inferred_position_from_entry(
                entry,
                deduped_children,
                direction=direction,
                quantity=quantity,
                group=group,
            )
        )
        if group:
            seen_groups.add(group)

    for group, children in sorted(children_by_group.items()):
        if not group or group in seen_groups:
            continue
        deduped_children = _dedupe_orders_by_id(children)
        tp_orders = [item for item in deduped_children if str(item.get("role") or "").strip().lower() == "take_profit"]
        sl_orders = [item for item in deduped_children if str(item.get("role") or "").strip().lower() == "stop_loss"]
        if not tp_orders or not sl_orders:
            continue
        parent_ids = {
            str(item.get("parent_id") or "").strip()
            for item in deduped_children
            if str(item.get("parent_id") or "").strip()
        }
        if parent_ids and any(parent_id in open_parent_entry_ids for parent_id in parent_ids):
            continue
        anchor = tp_orders[0] if tp_orders else deduped_children[0]
        direction = _protection_implied_position_direction(anchor)
        if direction not in {"long", "short"}:
            continue
        symbol = str(anchor.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quantities = [
            _safe_float(item.get("total_quantity"), 0.0) or _safe_float(item.get("remaining_quantity"), 0.0)
            for item in deduped_children
        ]
        quantity = max([value for value in quantities if value > 0], default=0.0)
        if quantity <= 0:
            continue
        pseudo_entry = {
            "order_id": sorted(parent_ids)[0] if parent_ids else "",
            "client_order_id": f"entry_{group}",
            "symbol": symbol,
            "conid": anchor.get("conid"),
            "avg_price": 0.0,
            "price": 0.0,
            "account": anchor.get("account"),
            "currency": anchor.get("currency") or "USD",
            "asset_class": anchor.get("asset_class") or "STK",
            "status": "Filled (inferred)",
            "status_key": "",
            "raw": anchor.get("raw") if isinstance(anchor.get("raw"), dict) else {},
        }
        inferred.append(
            _inferred_position_from_entry(
                pseudo_entry,
                deduped_children,
                direction=direction,
                quantity=quantity,
                group=group,
            )
        )
        seen_groups.add(group)
    return inferred


def _position_inference_payload(positions: list[dict], orders: list[dict], live_open_orders: list[dict]) -> tuple[list[dict], dict]:
    inferred = _infer_strategy_positions_from_orders(orders, live_open_orders)
    broker_open_count = _position_counts(positions).get("open_positions", 0)
    use_inferred_for_display = bool(inferred and broker_open_count == 0)
    return inferred, {
        "available": bool(inferred),
        "source": "orders_fast" if inferred else "",
        "method": "filled_entry_with_open_protection_orders" if inferred else "",
        "broker_open_positions": broker_open_count,
        "inferred_open_positions": len(inferred),
        "display_fallback": use_inferred_for_display,
        "reason": "broker_positions_empty" if use_inferred_for_display else "",
    }


def _merge_inferred_position_counts(counts: dict, inferred_positions: list[dict]) -> dict:
    merged = dict(counts or {})
    inferred_count = len(inferred_positions or [])
    merged["inferred_strategy_positions"] = inferred_count
    merged["inferred_open_positions"] = inferred_count
    merged["effective_open_positions"] = max(int(merged.get("open_positions") or 0), inferred_count)
    return merged


def _build_orders_fast_ibkr_account_snapshot_payload(
    service,
    context: dict,
    *,
    open_orders_only: bool = False,
    timing_started: float | None = None,
    context_elapsed_ms: float = 0.0,
) -> dict:
    started = float(timing_started or time.perf_counter())
    timings_ms: dict[str, float] = {"context_ms": round(float(context_elapsed_ms or 0.0), 1)}
    last_timing = time.perf_counter()

    def mark_timing(name: str) -> None:
        nonlocal last_timing
        now = time.perf_counter()
        timings_ms[name] = round((now - last_timing) * 1000.0, 1)
        last_timing = now

    api_app = context["api_app"]
    cached_full = _load_cached_full_snapshot_for_orders_fast(api_app, context)
    mark_timing("cached_full_snapshot_ms")
    cached_order_rows, cached_order_source = _cached_live_order_rows(service, include_all=not bool(open_orders_only))
    mark_timing("cached_live_orders_ms")
    fallback_rows_raw = load_pb_fallback_order_rows(api_app, service)
    fallback_rows, fallback_trust = filter_trusted_pb_fallback_order_rows(
        service,
        fallback_rows_raw,
        broker_rows=cached_order_rows,
    )
    mark_timing("pb_fallback_orders_ms")
    merged_orders_raw = _merge_cached_and_pb_order_rows(cached_order_rows, fallback_rows)
    live_open_payload = _build_orders_fast_live_open_payload(cached_order_rows, fallback_rows)
    cached_positions = (
        []
        if open_orders_only
        else cached_full.get("positions")
        if isinstance(cached_full.get("positions"), list)
        else []
    )
    positions_source = "omitted_open_orders_only" if open_orders_only else ("snapshot_cache" if cached_full else "unavailable")
    cached_counts = cached_full.get("counts") if isinstance(cached_full.get("counts"), dict) else {}
    cached_position_counts_available = bool(
        cached_counts
        and all(key in cached_counts for key in ("open_positions", "long_positions", "short_positions"))
    )
    positions, orders, live_open_orders, live_open_payload = _normalize_snapshot_rows(
        context["account_id"],
        cached_positions,
        merged_orders_raw,
        live_open_payload,
        [
            str(row.get("broker_order_id") or row.get("order_id") or "").strip()
            for row in fallback_rows
            if isinstance(row, dict) and str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        ],
    )
    mark_timing("normalize_rows_ms")
    inferred_positions, positions_inference = _position_inference_payload(positions, orders, live_open_orders)
    summary = dict(cached_full.get("summary") or {})
    summary_raw = dict(cached_full.get("summary_raw") or {})
    pnl_raw = dict(cached_full.get("pnl_raw") or {})
    if not summary:
        summary = _build_snapshot_summary(summary_raw, context["account_id"], positions, pnl_raw)
    guard = dict(cached_full.get("buying_power_guard") or {})
    if not guard:
        guard = build_buying_power_guard(
            summary,
            config=getattr(service, "config", None),
            environment=context["runtime_environment"],
        )
    guard.setdefault("source", "orders_fast_cached_summary" if cached_full else "orders_fast")
    errors = dict(cached_full.get("errors") or {})
    if not _summary_snapshot_available(summary):
        errors.setdefault("summary", "orders_fast_summary_cache_unavailable")
    counts = _merge_inferred_position_counts(
        _build_snapshot_counts(positions, orders, live_open_orders),
        inferred_positions,
    )
    if open_orders_only:
        counts = _merge_position_counts(counts, cached_counts, available=cached_position_counts_available)
        counts = _merge_inferred_position_counts(counts, inferred_positions)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    payload = {
        "ok": True,
        "environment": context["runtime_environment"],
        "account_id": context["account_id"],
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((context["service_status"].get("session") or {}).get("authenticated")),
        "gateway_running": bool((context["service_status"].get("gateway") or {}).get("running")),
        "websocket_ready": bool((context["service_status"].get("websocket") or {}).get("ready")),
        "summary": summary,
        "buying_power_guard": guard,
        "summary_raw": summary_raw,
        "pnl_raw": pnl_raw,
        "include_pnl": bool(context["include_pnl"]),
        "positions": positions,
        "orders": orders,
        "live_open_orders": live_open_orders,
        "live_order_coverage": live_open_payload.get("coverage") or {},
        "recovery_diagnostics": live_open_payload.get("diagnostics") or {},
        "counts": counts,
        "positions_detail_available": bool(((not open_orders_only) and cached_full) or inferred_positions),
        "positions_source": positions_source,
        "positions_count_available": bool(((not open_orders_only) and cached_full) or cached_position_counts_available or inferred_positions),
        "inferred_strategy_positions": inferred_positions,
        "positions_inference": positions_inference,
        "errors": errors,
        "diagnostics": {
            "account_snapshot": {
                "runtime_elapsed_ms": elapsed_ms,
                "total_elapsed_ms": elapsed_ms,
                "orders_fast": True,
                "open_orders_only": bool(open_orders_only),
            }
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "account_snapshot_orders_fast",
        "account_data_pacing": _account_data_pacing_status(context["service_status"]),
        "snapshot_profile": "orders_fast",
        "orders_fast": True,
        "orders_fast_open_orders_only": bool(open_orders_only),
        "orders_fast_diagnostics": {
            "order_source": cached_order_source,
            "cached_order_count": len(cached_order_rows),
            "pb_fallback_order_count": len(fallback_rows),
            "pb_fallback_raw_order_count": len(fallback_rows_raw),
            "pb_fallback_skipped": bool(fallback_rows_raw and not fallback_rows),
            "pb_fallback_trust": fallback_trust,
            "open_orders_only": bool(open_orders_only),
            "historical_orders_omitted": bool(open_orders_only),
            "positions_source": positions_source,
            "positions_omitted": bool(open_orders_only),
            "positions_count_available": bool(cached_position_counts_available),
            "summary_source": "snapshot_cache" if cached_full else "unavailable",
            "summary_cache_state": str(cached_full.get("cache_state") or "") if cached_full else "",
            "summary_cache_age_s": cached_full.get("cache_age_s") if cached_full else None,
            "status_source": "fast_runtime_state",
            "skipped_account_data_fetch": True,
            "elapsed_ms": elapsed_ms,
            "timings_ms": timings_ms,
        },
    }
    return _apply_reservation_overlay_to_account_payload(service, payload)


def refresh_account_snapshot_cache(service, *, include_pnl: bool = False) -> dict:
    return _build_ibkr_account_snapshot(service, include_pnl=include_pnl, force_refresh=True, allow_stale=True)


def _build_ibkr_account_snapshot(
    service,
    *,
    include_pnl: bool = True,
    force_refresh: bool = False,
    allow_stale: bool = True,
    orders_fast: bool = False,
    orders_fast_open_only: bool = False,
    fast_status: bool = False,
    apply_reservation_overlay: bool = True,
) -> dict:
    started = time.perf_counter()
    context = build_snapshot_context(service, include_pnl=include_pnl, fast_status=bool(orders_fast or fast_status))
    context_elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    api_app = context["api_app"]
    if orders_fast:
        return _build_orders_fast_ibkr_account_snapshot_payload(
            service,
            context,
            open_orders_only=bool(orders_fast_open_only),
            timing_started=started,
            context_elapsed_ms=context_elapsed_ms,
        )
    cache_key = context["cache_key"]
    if not force_refresh:
        cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
        if cached:
            return _apply_reservation_overlay_to_account_payload(service, cached) if apply_reservation_overlay else cached

    refresh_lock = get_snapshot_refresh_lock(api_app, cache_key)
    with refresh_lock:
        if not force_refresh:
            cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
            if cached:
                return _apply_reservation_overlay_to_account_payload(service, cached) if apply_reservation_overlay else cached
        try:
            payload = _build_fresh_ibkr_account_snapshot_payload(service, context)
        except Exception as exc:
            error = str(exc) or "account_snapshot_refresh_failed"
            mark_cached_snapshot_refresh_error(api_app, cache_key, error)
            cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
            if cached:
                stale = _stale_snapshot_after_error(cached, error)
                return _apply_reservation_overlay_to_account_payload(service, stale) if apply_reservation_overlay else stale
            error_payload = _account_snapshot_error_payload(context, error)
            return _apply_reservation_overlay_to_account_payload(service, error_payload) if apply_reservation_overlay else error_payload

        decorated = _decorate_account_snapshot_health(dict(payload))
        if not bool(decorated.get("summary_available")):
            summary_error = (
                (decorated.get("errors") or {}).get("summary")
                if isinstance(decorated.get("errors"), dict)
                else ""
            )
            cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
            if cached:
                stale = _stale_snapshot_after_error(
                    cached,
                    summary_error,
                )
                return _apply_reservation_overlay_to_account_payload(service, stale) if apply_reservation_overlay else stale
            if not allow_stale:
                error_payload = _account_snapshot_error_payload(
                    context,
                    summary_error or "account_snapshot_unavailable",
                )
                return _apply_reservation_overlay_to_account_payload(service, error_payload) if apply_reservation_overlay else error_payload
            return _apply_reservation_overlay_to_account_payload(service, decorated) if apply_reservation_overlay else decorated
        store_cached_snapshot(api_app, cache_key, decorated)
        fresh = load_cached_snapshot(api_app, cache_key, allow_stale=False) or decorated
        return _apply_reservation_overlay_to_account_payload(service, fresh) if apply_reservation_overlay else fresh


__all__ = [
    "_build_ibkr_account_buying_power_snapshot",
    "_build_ibkr_account_snapshot",
    "refresh_account_snapshot_cache",
]
