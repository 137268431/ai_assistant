from __future__ import annotations

import math
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
    load_pb_fallback_order_ids,
    recover_live_open_orders,
)


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


def _build_snapshot_counts(positions: list[dict], orders: list[dict], live_open_orders: list[dict]) -> dict:
    return {
        "positions": len(positions),
        "open_positions": len([item for item in positions if float(item.get("quantity", 0) or 0) != 0]),
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


def _decorate_account_snapshot_health(payload: dict, *, reason: str = "") -> dict:
    result = payload if isinstance(payload, dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    guard = result.get("buying_power_guard") if isinstance(result.get("buying_power_guard"), dict) else {}
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
    return result


def _fresh_full_snapshot_for_buying_power(api_app, context: dict) -> dict:
    candidate_keys = [
        context["cache_key"],
        (context["runtime_environment"], context["account_id"], True),
    ]
    cached = {}
    for cache_key in candidate_keys:
        cached = load_cached_snapshot(api_app, cache_key, allow_stale=False)
        if isinstance(cached, dict):
            break
    if not isinstance(cached, dict) or not cached:
        return {}
    payload = _decorate_account_snapshot_health(dict(cached))
    health = payload.get("account_snapshot_health") if isinstance(payload.get("account_snapshot_health"), dict) else {}
    if str(health.get("state") or "").strip().lower() != "ok":
        return {}
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    guard = dict(guard)
    guard.setdefault("source", "account_snapshot")
    payload["buying_power_guard"] = guard
    payload["source"] = "account_snapshot"
    return payload


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
        "retry_after_s": retry_after_s,
    }
    return _decorate_account_snapshot_health(payload, reason=reason)


def _build_ibkr_account_buying_power_snapshot(service) -> dict:
    context = build_snapshot_context(service, include_pnl=False)
    api_app = context["api_app"]
    cache_key = (context["runtime_environment"], f"{context['account_id']}::buying_power", False)

    cached = load_cached_snapshot(api_app, cache_key)
    if cached:
        return _decorate_account_snapshot_health(cached)

    refresh_lock = get_snapshot_refresh_lock(api_app, cache_key)
    with refresh_lock:
        cached = load_cached_snapshot(api_app, cache_key)
        if cached:
            return _decorate_account_snapshot_health(cached)

        circuit = _account_data_circuit_status(context.get("service_status") or {})
        if bool(circuit.get("active")):
            payload = _build_account_data_circuit_buying_power_snapshot(service, context, circuit)
            store_cached_snapshot(api_app, cache_key, payload)
            return load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload

        full_cached = _fresh_full_snapshot_for_buying_power(api_app, context)
        if full_cached:
            payload = {
                "ok": bool(full_cached.get("ok", True)),
                "environment": full_cached.get("environment") or context["runtime_environment"],
                "account_id": full_cached.get("account_id") or context["account_id"],
                "service_running": bool(full_cached.get("service_running")),
                "service_starting": bool(full_cached.get("service_starting")),
                "session_authenticated": bool(full_cached.get("session_authenticated")),
                "gateway_running": bool(full_cached.get("gateway_running")),
                "summary": dict(full_cached.get("summary") or {}),
                "buying_power_guard": dict(full_cached.get("buying_power_guard") or {}),
                "summary_raw": dict(full_cached.get("summary_raw") or {}),
                "errors": dict(full_cached.get("errors") or {}),
                "fetched_at": full_cached.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
                "source": "account_snapshot",
                "snapshot_source": full_cached.get("source") or "account_snapshot",
            }
            payload = _decorate_account_snapshot_health(payload)
            store_cached_snapshot(api_app, cache_key, payload)
            return load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload

        summary_raw = {}
        summary_error = ""
        lifecycle = getattr(service, "order_lifecycle", None)
        getter = getattr(lifecycle, "get_account_summary", None)
        if callable(getter):
            try:
                value = getter(context["account_id"])
                summary_raw = value if isinstance(value, dict) else {}
            except Exception as exc:
                summary_error = str(exc)
        else:
            summary_error = "account_summary_unavailable"
        if not summary_raw and not summary_error:
            summary_error = "account_summary_unavailable"

        summary = _build_snapshot_summary(summary_raw, context["account_id"], [], {})
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
        }
        payload = _decorate_account_snapshot_health(payload)
        store_cached_snapshot(api_app, cache_key, payload)
        return load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload


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
    fallback_ids = load_pb_fallback_order_ids(api_app, service)
    merged_orders_raw, live_open_payload = recover_live_open_orders(
        api_app,
        service,
        snapshot_sources["orders_raw"],
        fallback_ids,
    )
    positions, orders, live_open_orders, live_open_payload = _normalize_snapshot_rows(
        context["account_id"],
        snapshot_sources["positions_raw"],
        merged_orders_raw,
        live_open_payload,
        fallback_ids,
    )

    summary = _build_snapshot_summary(
        snapshot_sources["summary_raw"],
        context["account_id"],
        positions,
        snapshot_sources.get("pnl_raw") if isinstance(snapshot_sources.get("pnl_raw"), dict) else {},
    )
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
        "counts": _build_snapshot_counts(positions, orders, live_open_orders),
        "errors": snapshot_sources["errors"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "account_snapshot",
    }
    return _decorate_account_snapshot_health(payload)


def refresh_account_snapshot_cache(service, *, include_pnl: bool = False) -> dict:
    return _build_ibkr_account_snapshot(service, include_pnl=include_pnl, force_refresh=True, allow_stale=True)


def _build_ibkr_account_snapshot(
    service,
    *,
    include_pnl: bool = True,
    force_refresh: bool = False,
    allow_stale: bool = True,
) -> dict:
    context = build_snapshot_context(service, include_pnl=include_pnl)
    api_app = context["api_app"]
    cache_key = context["cache_key"]
    if not force_refresh:
        cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
        if cached:
            return cached

    refresh_lock = get_snapshot_refresh_lock(api_app, cache_key)
    with refresh_lock:
        if not force_refresh:
            cached = load_cached_snapshot(api_app, cache_key, allow_stale=allow_stale)
            if cached:
                return cached
        try:
            payload = _build_fresh_ibkr_account_snapshot_payload(service, context)
        except Exception as exc:
            error = str(exc) or "account_snapshot_refresh_failed"
            mark_cached_snapshot_refresh_error(api_app, cache_key, error)
            cached = load_cached_snapshot(api_app, cache_key, allow_stale=True)
            if cached:
                return _stale_snapshot_after_error(cached, error)
            return _account_snapshot_error_payload(context, error)

        store_cached_snapshot(api_app, cache_key, payload)
        return load_cached_snapshot(api_app, cache_key, allow_stale=False) or payload


__all__ = [
    "_build_ibkr_account_buying_power_snapshot",
    "_build_ibkr_account_snapshot",
    "refresh_account_snapshot_cache",
]
