from __future__ import annotations

from typing import Any

from .status_runtime_sections import (
    build_auth_recovery_summary,
    build_canonical_5m_payload,
    build_daily_scan_payload,
    build_data_backfill_payload,
    build_gateway_payload,
    build_market_universe_payload,
    build_order_tracker_payload,
    build_realtime_compute_payload,
    build_realtime_quotes_payload,
    build_session_payload,
    build_websocket_payload,
)
from .status_runtime_warmup import build_runtime_warmup_payload
from .status_types import AsDict, NormalizeSymbolList, TrimArray, TrimObjectEntries
from .effective_gate import build_effective_trading_gate


def _resolve_daily_scan(payload: dict[str, Any], fallback: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    daily_scan = as_dict(payload.get("daily_scan"))
    persisted_daily_scan = as_dict(fallback.get("daily_scan"))
    if not daily_scan and persisted_daily_scan:
        return dict(persisted_daily_scan)
    return daily_scan


def _resolve_market_universe(
    payload: dict[str, Any],
    fallback: dict[str, Any],
    daily_scan: dict[str, Any],
    *,
    as_dict: AsDict,
) -> dict[str, Any]:
    market_universe = as_dict(payload.get("market_universe"))
    if int(market_universe.get("active_target_count") or 0) <= 0:
        fallback_active_target_count = int(fallback.get("active_target_count") or 0)
        if fallback_active_target_count > 0:
            market_universe["active_target_count"] = fallback_active_target_count
    if int(market_universe.get("execution_eligible_target_count") or 0) <= 0:
        fallback_execution_count = int(fallback.get("execution_eligible_target_count") or 0)
        if fallback_execution_count > 0:
            market_universe["execution_eligible_target_count"] = fallback_execution_count
            market_universe["execution_eligible_symbols"] = list(fallback.get("execution_eligible_symbols") or [])
            market_universe["no_execution_eligible_targets"] = False
    if int(market_universe.get("observe_target_count") or 0) <= 0:
        fallback_observe_count = int(fallback.get("observe_target_count") or 0)
        if fallback_observe_count > 0:
            market_universe["observe_target_count"] = fallback_observe_count
            market_universe["observe_target_symbols"] = list(fallback.get("observe_target_symbols") or [])
    if not str(market_universe.get("active_target_date") or "").strip():
        fallback_market_date = str(fallback.get("active_target_date") or daily_scan.get("market_date") or "").strip()
        if fallback_market_date:
            market_universe["active_target_date"] = fallback_market_date
    return market_universe


def build_statusz_runtime_payload(
    runtime_payload: dict[str, Any],
    include_warmup_details: bool,
    *,
    live_readiness: dict[str, Any],
    fallback_state: dict[str, Any] | None,
    as_dict: AsDict,
    normalize_symbol_list: NormalizeSymbolList,
    trim_array: TrimArray,
    trim_object_entries: TrimObjectEntries,
) -> dict[str, Any]:
    payload = as_dict(runtime_payload)
    fallback = as_dict(fallback_state)
    daily_scan = _resolve_daily_scan(payload, fallback, as_dict=as_dict)
    market_universe = _resolve_market_universe(payload, fallback, daily_scan, as_dict=as_dict)
    live = as_dict(live_readiness)
    warmup = build_runtime_warmup_payload(
        payload.get("warmup"),
        include_warmup_details,
        as_dict=as_dict,
        normalize_symbol_list=normalize_symbol_list,
        trim_array=trim_array,
    )

    runtime_view = {
        "ok": payload.get("ok") if payload else None,
        "starting": bool(payload.get("starting")),
        "startup_complete": bool(payload.get("startup_complete")),
        "runtime_phase": str(payload.get("runtime_phase") or ""),
        "environment": str(payload.get("environment") or ""),
        "broker_mode": str(payload.get("broker_mode") or payload.get("environment") or ""),
        "gateway_mode": str(payload.get("gateway_mode") or ""),
        "data_environment": str(payload.get("data_environment") or payload.get("market_data_environment") or ""),
        "market_data_environment": str(payload.get("market_data_environment") or payload.get("data_environment") or ""),
        "shared_market_data": bool(payload.get("shared_market_data")),
        "mode_mismatch": bool(payload.get("mode_mismatch")),
        "service_profile": str(payload.get("service_profile") or ""),
        "runtime_mode": str(payload.get("runtime_mode") or ""),
        "service_topology": as_dict(payload.get("service_topology")),
        "market_session": as_dict(payload.get("market_session")),
        "warmup_details_included": bool(include_warmup_details),
        "live_readiness": live,
        "gateway": build_gateway_payload(payload.get("gateway"), as_dict=as_dict),
        "session": build_session_payload(payload.get("session"), as_dict=as_dict),
        "auth_recovery": build_auth_recovery_summary(payload.get("auth_recovery"), as_dict=as_dict),
        "websocket": build_websocket_payload(payload.get("websocket"), as_dict=as_dict),
        "realtime_quotes": build_realtime_quotes_payload(payload.get("realtime_quotes"), as_dict=as_dict),
        "canonical_5m": build_canonical_5m_payload(
            payload.get("canonical_5m"),
            as_dict=as_dict,
            trim_array=trim_array,
        ),
        "data_backfill": build_data_backfill_payload(payload.get("data_backfill"), as_dict=as_dict),
        "order_tracker": build_order_tracker_payload(payload.get("order_tracker"), as_dict=as_dict),
        "order_flow": as_dict(payload.get("order_flow")),
        "warmup": warmup,
        "realtime_compute": build_realtime_compute_payload(payload.get("realtime_compute"), as_dict=as_dict),
        "daily_scan": build_daily_scan_payload(daily_scan, as_dict=as_dict),
        "market_universe": build_market_universe_payload(
            market_universe,
            as_dict=as_dict,
            trim_array=trim_array,
            trim_object_entries=trim_object_entries,
        ),
        "runtime_control": as_dict(payload.get("runtime_control")),
    }
    runtime_view["signal_processor"] = as_dict(payload.get("signal_processor"))
    runtime_view["multi_timeframe_readiness"] = as_dict(payload.get("multi_timeframe_readiness"))
    runtime_view["effective_trading_gate"] = build_effective_trading_gate(runtime_view, live_readiness=live)
    return runtime_view


__all__ = ["build_statusz_runtime_payload"]
