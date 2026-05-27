from __future__ import annotations

import math
from datetime import datetime, timezone

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
    load_cached_snapshot,
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


def _build_ibkr_account_snapshot(service, *, include_pnl: bool = True) -> dict:
    context = build_snapshot_context(service, include_pnl=include_pnl)
    api_app = context["api_app"]
    cached = load_cached_snapshot(api_app, context["cache_key"])
    if cached:
        return cached

    snapshot_sources = fetch_snapshot_sources(
        service,
        context["account_id"],
        include_pnl=context["include_pnl"],
    )
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
        "buying_power_guard": build_buying_power_guard(
            summary,
            config=getattr(service, "config", None),
            environment=context["runtime_environment"],
        ),
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
    }
    store_cached_snapshot(api_app, context["cache_key"], payload)
    return payload


__all__ = ["_build_ibkr_account_snapshot"]
