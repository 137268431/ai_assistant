from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


AsDict = Callable[[Any], dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]
NormalizeSymbolList = Callable[[Any], list[str]]
RequestJson = Callable[..., dict[str, Any]]
TrimArray = Callable[[Any, int], list[Any]]
TrimObjectEntries = Callable[[Any, int], dict[str, Any]]
BuildServiceTopology = Callable[[], dict[str, Any]]


def fetch_compute_monitor(environment: str, *, request_json: RequestJson, compute_base_url: str) -> dict[str, Any]:
    return request_json(
        compute_base_url,
        "/ibkr/monitor",
        params=[("environment", environment)],
        timeout=10,
    )


def fetch_compute_status(environment: str, *, request_json: RequestJson, compute_base_url: str) -> dict[str, Any]:
    return request_json(
        compute_base_url,
        "/status",
        params=[("environment", environment)],
        timeout=10,
    )


def fetch_compute_health(environment: str, *, request_json: RequestJson, compute_base_url: str) -> dict[str, Any]:
    return request_json(
        compute_base_url,
        "/health",
        params=[("environment", environment)],
        timeout=10,
    )


def fetch_runtime_status(
    environment: str,
    *,
    request_json: RequestJson,
    compute_base_url: str,
    runtime_base_url: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    proxy_result = request_json(
        compute_base_url,
        "/ibkr/status",
        params=[("environment", environment)],
        timeout=10,
    )
    proxy_upstream = f"{compute_base_url}/ibkr/status"
    direct_upstream = f"{runtime_base_url}/ibkr/status"
    selected_result = proxy_result
    selected_upstream = proxy_upstream
    error = str(proxy_result.get("error") or "")

    if (not bool(proxy_result.get("ok"))) and runtime_base_url:
        direct_result = request_json(
            runtime_base_url,
            "/ibkr/status",
            params=[("environment", environment)],
            timeout=10,
        )
        if bool(direct_result.get("ok")):
            selected_result = direct_result
            selected_upstream = direct_upstream
            error = ""
        elif not error:
            error = str(direct_result.get("error") or "")

    return {
        "payload": as_dict(selected_result.get("payload")),
        "ok": bool(selected_result.get("ok")),
        "error": error,
        "selected_upstream": selected_upstream,
        "proxy_upstream": proxy_upstream,
        "direct_upstream": direct_upstream,
        "status_code": int(selected_result.get("status_code") or 0),
    }


def fetch_runtime_health(
    environment: str,
    *,
    request_json: RequestJson,
    runtime_base_url: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    result = request_json(
        runtime_base_url,
        "/health",
        params=[("environment", environment)],
        timeout=10,
    )
    return {
        "payload": as_dict(result.get("payload")),
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        "upstream": f"{runtime_base_url}/health",
        "status_code": int(result.get("status_code") or 0),
    }


def merge_service_topology(*payloads: Any, build_service_topology: BuildServiceTopology) -> dict[str, Any]:
    merged = build_service_topology()
    merged_services = dict(merged.get("services") if isinstance(merged.get("services"), dict) else {})
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        topology = payload if isinstance(payload.get("services"), dict) else payload.get("service_topology")
        if not isinstance(topology, dict):
            continue
        for key, value in topology.items():
            if key == "services":
                continue
            merged[key] = value
        if isinstance(topology.get("services"), dict):
            merged_services.update(topology.get("services") or {})
    merged["services"] = merged_services
    return merged


def split_symbol_list_by_monitor(
    values: Any,
    monitor_symbols: Any,
    *,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, list[str]]:
    monitor_set = set(normalize_symbol_list(monitor_symbols))
    blocking: list[str] = []
    monitor: list[str] = []
    for symbol in normalize_symbol_list(values):
        if symbol in monitor_set:
            monitor.append(symbol)
        else:
            blocking.append(symbol)
    return {"blocking": blocking, "monitor": monitor}


def split_reason_map_by_monitor(
    value: Any,
    monitor_symbols: Any,
    *,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, dict[str, Any]]:
    monitor_set = set(normalize_symbol_list(monitor_symbols))
    blocking: dict[str, Any] = {}
    monitor: dict[str, Any] = {}
    if not isinstance(value, dict):
        return {"blocking": blocking, "monitor": monitor}
    for symbol, reason in value.items():
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        if normalized_symbol in monitor_set:
            monitor[normalized_symbol] = reason
        else:
            blocking[normalized_symbol] = reason
    return {"blocking": blocking, "monitor": monitor}


def build_statusz_compute_payload(
    compute_payload: dict[str, Any],
    include_engines: bool,
    *,
    as_dict: AsDict,
) -> dict[str, Any]:
    payload = as_dict(compute_payload)
    engine_map = payload.get("engines") if isinstance(payload.get("engines"), dict) else {}
    total_engines = int(payload.get("total_engines") or len(engine_map))
    payload["total_engines"] = total_engines
    payload["ready_engines"] = int(payload.get("ready_engines") or 0)
    payload["engines_available"] = total_engines > 0
    payload["engines_included"] = bool(include_engines)
    payload["statusz_mode"] = "full" if include_engines else "lite"
    if include_engines:
        payload["engines"] = dict(engine_map)
    else:
        payload.pop("engines", None)
    return payload


def build_statusz_live_readiness(
    compute_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
    *,
    as_dict: AsDict,
    normalize_environment: NormalizeEnvironment,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, Any]:
    compute = as_dict(compute_payload)
    runtime = as_dict(runtime_payload)
    engine_map = compute.get("engines") if isinstance(compute.get("engines"), dict) else {}
    warmup = as_dict(runtime.get("warmup"))
    market_universe = as_dict(runtime.get("market_universe"))
    service_topology = as_dict(runtime.get("service_topology"))
    environment = normalize_environment(runtime.get("environment") or compute.get("environment"), "live")
    required_interval = str(warmup.get("required_interval") or "5m").strip() or "5m"
    trade_symbols = normalize_symbol_list(
        warmup.get("trade_symbols")
        if isinstance(warmup.get("trade_symbols"), list) and warmup.get("trade_symbols")
        else market_universe.get("active_trade_symbols")
    )
    monitor_symbols = normalize_symbol_list(
        warmup.get("monitor_symbols")
        if isinstance(warmup.get("monitor_symbols"), list) and warmup.get("monitor_symbols")
        else market_universe.get("market_ws_symbols")
    )
    symbols = normalize_symbol_list(
        warmup.get("symbols")
        if isinstance(warmup.get("symbols"), list) and warmup.get("symbols")
        else (
            market_universe.get("data_symbols")
            if isinstance(market_universe.get("data_symbols"), list)
            else trade_symbols + monitor_symbols
        )
    )
    if not symbols:
        derived_symbols: list[str] = []
        for key in engine_map:
            parts = str(key or "").split("/")
            if len(parts) != 3:
                continue
            if parts[0] == environment and parts[2] == required_interval:
                derived_symbols.append(parts[1])
        symbols = normalize_symbol_list(derived_symbols)

    symbol_set = set(symbols + trade_symbols + monitor_symbols)
    all_symbols = sorted(symbol_set)
    trade_set = set(trade_symbols)
    monitor_set = set(monitor_symbols)
    ready_symbols = 0
    ready_trade_symbols = 0
    ready_monitor_symbols = 0
    ready_set: set[str] = set()
    for symbol in all_symbols:
        engine = engine_map.get(f"{environment}/{symbol}/{required_interval}") if isinstance(engine_map, dict) else None
        if not isinstance(engine, dict) or not bool(engine.get("is_ready")):
            continue
        ready_set.add(symbol)
        ready_symbols += 1
        if symbol in trade_set:
            ready_trade_symbols += 1
        if symbol in monitor_set:
            ready_monitor_symbols += 1
    non_monitor_pending_total = len([symbol for symbol in all_symbols if symbol not in ready_set and symbol not in monitor_set])
    monitor_pending_total = len([symbol for symbol in all_symbols if symbol not in ready_set and symbol in monitor_set])
    gate_open = len(trade_symbols) > 0 and ready_trade_symbols >= len(trade_symbols)
    phase = "idle"
    if all_symbols:
        phase = "ready" if non_monitor_pending_total == 0 else "pending"

    snapshot_symbols_total = int(warmup.get("symbols_total") or 0)
    snapshot_ready_symbols = int(warmup.get("ready_symbols") or 0)
    snapshot_ready_trade_symbols = int(warmup.get("ready_trade_symbols") or 0)
    snapshot_ready_monitor_symbols = int(warmup.get("ready_monitor_symbols") or 0)
    snapshot_phase = str(warmup.get("phase") or "").strip().lower() or "idle"
    snapshot_finished_at = warmup.get("finished_at") or ""
    snapshot_trade_symbols_total = int(warmup.get("trade_symbols_total") or len(trade_symbols))
    snapshot_monitor_symbols_total = int(warmup.get("monitor_symbols_total") or len(monitor_symbols))
    snapshot_pending_symbols_total = (
        len(warmup.get("pending_symbols"))
        if isinstance(warmup.get("pending_symbols"), list)
        else int(warmup.get("pending_symbols_total") or 0)
    )
    snapshot_monitor_pending_total = int(warmup.get("monitor_pending_symbols_total") or 0) or max(
        0, snapshot_monitor_symbols_total - snapshot_ready_monitor_symbols
    )
    snapshot_blocking_pending_total = int(warmup.get("blocking_pending_symbols_total") or 0) or max(
        0, snapshot_pending_symbols_total - snapshot_monitor_pending_total
    )
    snapshot_gate_open = bool(warmup.get("trading_gate_open"))
    snapshot_gate_reason = str(warmup.get("trading_gate_reason") or "").strip().lower() or (
        "ready" if snapshot_trade_symbols_total > 0 and snapshot_gate_open else (
            "warmup_incomplete" if snapshot_trade_symbols_total > 0 else "no_trade_symbols"
        )
    )
    snapshot_present = bool(
        snapshot_symbols_total
        or snapshot_ready_symbols
        or snapshot_ready_trade_symbols
        or snapshot_ready_monitor_symbols
        or str(warmup.get("phase") or "").strip()
        or str(warmup.get("finished_at") or "").strip()
    )
    snapshot_differs = bool(
        snapshot_present
        and (
            (snapshot_symbols_total > 0 and snapshot_symbols_total != len(all_symbols))
            or snapshot_ready_symbols != ready_symbols
            or snapshot_ready_trade_symbols != ready_trade_symbols
            or snapshot_ready_monitor_symbols != ready_monitor_symbols
        )
    )
    runtime_mode = str(
        runtime.get("runtime_mode")
        or compute.get("runtime_mode")
        or service_topology.get("runtime_mode")
        or ""
    ).strip().lower()
    runtime_snapshot_available = snapshot_present and (
        snapshot_symbols_total > 0
        or snapshot_ready_symbols > 0
        or snapshot_trade_symbols_total > 0
        or snapshot_monitor_symbols_total > 0
        or snapshot_phase != "idle"
        or bool(snapshot_finished_at)
    )
    if (runtime_mode == "remote" or not engine_map) and runtime_snapshot_available:
        return {
            "available": True,
            "engine_snapshot_available": bool(engine_map),
            "source": "runtime_warmup_snapshot",
            "environment": environment,
            "required_interval": required_interval,
            "computed_at": datetime.now(tz=ET).isoformat(),
            "phase": snapshot_phase,
            "gate_open": snapshot_gate_open,
            "gate_reason": snapshot_gate_reason,
            "symbols_total": snapshot_symbols_total or len(all_symbols),
            "trade_symbols_total": snapshot_trade_symbols_total,
            "monitor_symbols_total": snapshot_monitor_symbols_total,
            "ready_symbols": snapshot_ready_symbols,
            "ready_trade_symbols": snapshot_ready_trade_symbols,
            "ready_monitor_symbols": snapshot_ready_monitor_symbols,
            "pending_symbols_total": snapshot_pending_symbols_total,
            "non_monitor_pending_symbols_total": snapshot_blocking_pending_total,
            "blocking_pending_symbols_total": snapshot_blocking_pending_total,
            "monitor_pending_symbols_total": snapshot_monitor_pending_total,
            "snapshot_differs": False,
            "snapshot_phase": snapshot_phase,
            "snapshot_finished_at": snapshot_finished_at,
        }

    return {
        "available": bool(all_symbols) and bool(engine_map),
        "engine_snapshot_available": bool(engine_map),
        "source": "compute_engines",
        "environment": environment,
        "required_interval": required_interval,
        "computed_at": datetime.now(tz=ET).isoformat(),
        "phase": phase,
        "gate_open": gate_open,
        "gate_reason": "ready" if gate_open else ("live_not_ready" if trade_symbols else "no_trade_symbols"),
        "symbols_total": len(all_symbols),
        "trade_symbols_total": len(trade_symbols),
        "monitor_symbols_total": len(monitor_symbols),
        "ready_symbols": ready_symbols,
        "ready_trade_symbols": ready_trade_symbols,
        "ready_monitor_symbols": ready_monitor_symbols,
        "pending_symbols_total": max(0, len(all_symbols) - ready_symbols),
        "non_monitor_pending_symbols_total": non_monitor_pending_total,
        "blocking_pending_symbols_total": non_monitor_pending_total,
        "monitor_pending_symbols_total": monitor_pending_total,
        "snapshot_differs": snapshot_differs,
        "snapshot_phase": snapshot_phase,
        "snapshot_finished_at": snapshot_finished_at,
    }


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
    gateway = as_dict(payload.get("gateway"))
    session = as_dict(payload.get("session"))
    auth_recovery = as_dict(payload.get("auth_recovery"))
    websocket = as_dict(payload.get("websocket"))
    realtime_quotes = as_dict(payload.get("realtime_quotes"))
    canonical_5m = as_dict(payload.get("canonical_5m"))
    data_backfill = as_dict(payload.get("data_backfill"))
    order_tracker = as_dict(payload.get("order_tracker"))
    warmup = as_dict(payload.get("warmup"))
    realtime_compute = as_dict(payload.get("realtime_compute"))
    realtime_result = as_dict(realtime_compute.get("last_result"))
    market_universe = as_dict(payload.get("market_universe"))
    daily_scan = as_dict(payload.get("daily_scan"))
    persisted_daily_scan = as_dict(fallback.get("daily_scan"))
    if not daily_scan and persisted_daily_scan:
        daily_scan = dict(persisted_daily_scan)
    if int(market_universe.get("active_target_count") or 0) <= 0:
        fallback_active_target_count = int(fallback.get("active_target_count") or 0)
        if fallback_active_target_count > 0:
            market_universe["active_target_count"] = fallback_active_target_count
    if not str(market_universe.get("active_target_date") or "").strip():
        fallback_market_date = str(fallback.get("active_target_date") or daily_scan.get("market_date") or "").strip()
        if fallback_market_date:
            market_universe["active_target_date"] = fallback_market_date

    auth_recovery_summary = {
        "cycle_id": str(auth_recovery.get("cycle_id") or ""),
        "recovery_phase": str(auth_recovery.get("recovery_phase") or ""),
        "recovery_class": str(auth_recovery.get("recovery_class") or ""),
        "recovery_reason": str(auth_recovery.get("recovery_reason") or ""),
        "interruption_kind": str(auth_recovery.get("interruption_kind") or ""),
        "last_runtime_authenticated_at": auth_recovery.get("last_runtime_authenticated_at") or "",
        "last_gateway_status_code": int(auth_recovery.get("last_gateway_status_code") or 0),
        "last_recovery_source": str(auth_recovery.get("last_recovery_source") or ""),
        "probe_result": str(auth_recovery.get("probe_result") or ""),
        "probe_last_checked_at": auth_recovery.get("probe_last_checked_at") or "",
        "probe_attempts": int(auth_recovery.get("probe_attempts") or 0),
        "auto_restart_scheduled": bool(auth_recovery.get("auto_restart_scheduled")),
        "manual_takeover_active": bool(auth_recovery.get("manual_takeover_active")),
        "lock_owner": str(auth_recovery.get("lock_owner") or ""),
    }

    monitor_symbols = normalize_symbol_list(warmup.get("monitor_symbols"))
    pending_symbols = normalize_symbol_list(warmup.get("pending_symbols"))
    integrity_pending_symbols_all = normalize_symbol_list(warmup.get("integrity_pending_symbols"))
    pending_split = split_symbol_list_by_monitor(
        pending_symbols,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )
    integrity_pending_split = split_symbol_list_by_monitor(
        integrity_pending_symbols_all,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )
    integrity_repair_reasons = as_dict(warmup.get("integrity_repair_reasons")) if include_warmup_details else {}
    reason_split = split_reason_map_by_monitor(
        integrity_repair_reasons,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )

    return {
        "ok": payload.get("ok") if payload else None,
        "starting": bool(payload.get("starting")),
        "startup_complete": bool(payload.get("startup_complete")),
        "runtime_phase": str(payload.get("runtime_phase") or ""),
        "environment": str(payload.get("environment") or ""),
        "service_profile": str(payload.get("service_profile") or ""),
        "runtime_mode": str(payload.get("runtime_mode") or ""),
        "service_topology": as_dict(payload.get("service_topology")),
        "market_session": as_dict(payload.get("market_session")),
        "warmup_details_included": bool(include_warmup_details),
        "live_readiness": as_dict(live_readiness),
        "gateway": {
            "running": bool(gateway.get("running")),
            "reachable": bool(gateway.get("reachable")),
            "managed_by": str(gateway.get("managed_by") or ""),
            "status_code": int(gateway.get("status_code") or 0),
            "pid": int(gateway.get("pid") or 0),
            "uptime_s": int(gateway.get("uptime_s") or 0),
        },
        "session": {
            "authenticated": bool(session.get("authenticated")),
            "running": bool(session.get("running")),
            "consecutive_failures": int(session.get("consecutive_failures") or 0),
            "last_check": session.get("last_check") or session.get("last_tickle") or "",
            "last_tickle": session.get("last_tickle") or session.get("last_check") or "",
        },
        "auth_recovery": auth_recovery_summary,
        "websocket": {
            "connected": bool(websocket.get("connected")),
            "ready": bool(websocket.get("ready")),
            "running": bool(websocket.get("running")),
            "last_message": websocket.get("last_message") or "",
            "message_count": int(websocket.get("message_count") or 0),
            "subscribed_count": (
                len(websocket.get("subscribed_conids"))
                if isinstance(websocket.get("subscribed_conids"), list)
                else int(websocket.get("subscribed_count") or 0)
            ),
            "pending_count": (
                len(websocket.get("pending_conids"))
                if isinstance(websocket.get("pending_conids"), list)
                else int(websocket.get("pending_count") or 0)
            ),
        },
        "realtime_quotes": {
            "total_quotes": int(realtime_quotes.get("total_quotes") or 0),
            "stale_quotes": int(realtime_quotes.get("stale_quotes") or 0),
            "tick_count": int(realtime_quotes.get("tick_count") or 0),
            "update_count": int(realtime_quotes.get("update_count") or 0),
        },
        "canonical_5m": {
            "enabled": bool(canonical_5m.get("enabled", True)),
            "driver": str(canonical_5m.get("driver") or ""),
            "close_delay_sec": int(canonical_5m.get("close_delay_sec") or 0),
            "request_period": str(canonical_5m.get("request_period") or ""),
            "last_run": canonical_5m.get("last_run") or "",
            "last_due_bucket_ms": int(canonical_5m.get("last_due_bucket_ms") or 0),
            "last_completed_bucket_ms": int(canonical_5m.get("last_completed_bucket_ms") or 0),
            "lag_s": int(canonical_5m.get("lag_s") or 0),
            "last_written_bars": int(canonical_5m.get("last_written_bars") or 0),
            "written_symbols": trim_array(canonical_5m.get("written_symbols"), 24),
            "written_symbols_total": (
                len(canonical_5m.get("written_symbols"))
                if isinstance(canonical_5m.get("written_symbols"), list)
                else int(canonical_5m.get("written_symbols_total") or 0)
            ),
            "pending_symbols": trim_array(canonical_5m.get("pending_symbols"), 24),
            "pending_symbols_total": (
                len(canonical_5m.get("pending_symbols"))
                if isinstance(canonical_5m.get("pending_symbols"), list)
                else int(canonical_5m.get("pending_symbols_total") or 0)
            ),
            "last_error": str(canonical_5m.get("last_error") or ""),
        },
        "data_backfill": {
            "total_backfilled": int(data_backfill.get("total_backfilled") or 0),
        },
        "order_tracker": {
            "running": bool(order_tracker.get("running")),
            "last_poll": order_tracker.get("last_poll") or "",
            "tracked_orders": int(order_tracker.get("tracked_orders") or 0),
        },
        "warmup": {
            "phase": str(warmup.get("phase") or ""),
            "trading_gate_open": bool(warmup.get("trading_gate_open")),
            "trading_gate_reason": str(warmup.get("trading_gate_reason") or ""),
            "required_interval": str(warmup.get("required_interval") or ""),
            "symbols_total": int(warmup.get("symbols_total") or 0),
            "trade_symbols_total": int(warmup.get("trade_symbols_total") or 0),
            "monitor_symbols_total": int(warmup.get("monitor_symbols_total") or 0),
            "ready_symbols": int(warmup.get("ready_symbols") or 0),
            "ready_trade_symbols": int(warmup.get("ready_trade_symbols") or 0),
            "ready_monitor_symbols": int(warmup.get("ready_monitor_symbols") or 0),
            "pending_symbols": (
                trim_array(pending_symbols, len(pending_symbols))
                if include_warmup_details
                else trim_array(pending_symbols, 12)
            ),
            "pending_symbols_total": (
                len(warmup.get("pending_symbols"))
                if isinstance(warmup.get("pending_symbols"), list)
                else int(warmup.get("pending_symbols_total") or len(pending_symbols))
            ),
            "blocking_pending_symbols": trim_array(
                pending_split.get("blocking"),
                len(pending_split.get("blocking", [])) if include_warmup_details else 12,
            ),
            "blocking_pending_symbols_total": len(pending_split.get("blocking") or []),
            "monitor_pending_symbols": trim_array(
                pending_split.get("monitor"),
                len(pending_split.get("monitor", [])) if include_warmup_details else 12,
            ),
            "monitor_pending_symbols_total": len(pending_split.get("monitor") or []),
            "requested_at": warmup.get("requested_at") or "",
            "started_at": warmup.get("started_at") or "",
            "finished_at": warmup.get("finished_at") or "",
            "last_success_at": warmup.get("last_success_at") or "",
            "last_error": str(warmup.get("last_error") or ""),
            "reason": str(warmup.get("reason") or ""),
            "target_date": str(warmup.get("target_date") or ""),
            "symbols": (
                trim_array(warmup.get("symbols"), len(warmup.get("symbols")))
                if include_warmup_details and isinstance(warmup.get("symbols"), list)
                else []
            ),
            "trade_symbols": (
                trim_array(warmup.get("trade_symbols"), len(warmup.get("trade_symbols")))
                if include_warmup_details and isinstance(warmup.get("trade_symbols"), list)
                else []
            ),
            "monitor_symbols": (
                trim_array(warmup.get("monitor_symbols"), len(warmup.get("monitor_symbols")))
                if include_warmup_details and isinstance(warmup.get("monitor_symbols"), list)
                else []
            ),
            "ready_symbols_list": (
                trim_array(warmup.get("ready_symbols_list"), len(warmup.get("ready_symbols_list")))
                if include_warmup_details and isinstance(warmup.get("ready_symbols_list"), list)
                else []
            ),
            "symbol_status": (
                trim_array(warmup.get("symbol_status"), len(warmup.get("symbol_status")))
                if include_warmup_details and isinstance(warmup.get("symbol_status"), list)
                else []
            ),
            "integrity_pending_symbols": (
                trim_array(integrity_pending_symbols_all, len(integrity_pending_symbols_all))
                if include_warmup_details
                else []
            ),
            "integrity_pending_symbols_total": (
                len(warmup.get("integrity_pending_symbols"))
                if isinstance(warmup.get("integrity_pending_symbols"), list)
                else len(integrity_pending_symbols_all)
            ),
            "blocking_integrity_pending_symbols": (
                trim_array(integrity_pending_split.get("blocking"), len(integrity_pending_split.get("blocking", [])))
                if include_warmup_details
                else []
            ),
            "blocking_integrity_pending_symbols_total": len(integrity_pending_split.get("blocking") or []),
            "monitor_integrity_pending_symbols": (
                trim_array(integrity_pending_split.get("monitor"), len(integrity_pending_split.get("monitor", [])))
                if include_warmup_details
                else []
            ),
            "monitor_integrity_pending_symbols_total": len(integrity_pending_split.get("monitor") or []),
            "integrity_repair_reasons": integrity_repair_reasons,
            "blocking_integrity_repair_reasons": reason_split.get("blocking") or {},
            "monitor_integrity_repair_reasons": reason_split.get("monitor") or {},
            "preflight_repair": as_dict(warmup.get("preflight_repair")) if include_warmup_details else {},
        },
        "realtime_compute": {
            "runs": int(realtime_compute.get("runs") or 0),
            "queue_size": int(realtime_compute.get("queue_size") or 0),
            "thread_alive": bool(realtime_compute.get("thread_alive")),
            "inflight": bool(realtime_compute.get("inflight")),
            "inflight_age_s": int(realtime_compute.get("inflight_age_s") or 0),
            "inflight_timeout_threshold_s": int(realtime_compute.get("inflight_timeout_threshold_s") or 0),
            "stalled": bool(realtime_compute.get("stalled")),
            "stall_reason": str(realtime_compute.get("stall_reason") or ""),
            "last_started": realtime_compute.get("last_started") or "",
            "last_run": realtime_compute.get("last_run") or "",
            "last_bar_close": realtime_compute.get("last_bar_close") or "",
            "last_elapsed_s": float(realtime_compute.get("last_elapsed_s") or realtime_result.get("elapsed_s") or 0),
            "last_processed": int(realtime_result.get("processed") or 0),
            "last_signals": int(realtime_result.get("signals") or 0),
            "last_errors": int(realtime_result.get("errors") or 0),
        },
        "daily_scan": {
            "market_date": str(daily_scan.get("market_date") or ""),
            "status": str(daily_scan.get("status") or ""),
            "reason": str(daily_scan.get("reason") or ""),
            "started_at": daily_scan.get("started_at") or "",
            "finished_at": daily_scan.get("finished_at") or "",
            "last_error": str(daily_scan.get("last_error") or ""),
            "result": as_dict(daily_scan.get("result")),
        },
        "market_universe": {
            "market_date": str(market_universe.get("market_date") or ""),
            "last_daily_reset": market_universe.get("last_daily_reset") or "",
            "watchlist_pool_count": int(market_universe.get("watchlist_pool_count") or 0),
            "active_target_date": str(market_universe.get("active_target_date") or ""),
            "active_target_count": int(market_universe.get("active_target_count") or 0),
            "active_trade_symbols": trim_array(
                market_universe.get("active_trade_symbols"),
                len(market_universe.get("active_trade_symbols"))
                if isinstance(market_universe.get("active_trade_symbols"), list)
                else 0,
            ),
            "active_trade_symbols_total": (
                len(market_universe.get("active_trade_symbols"))
                if isinstance(market_universe.get("active_trade_symbols"), list)
                else int(market_universe.get("active_trade_symbols_total") or 0)
            ),
            "last_target_refresh": market_universe.get("last_target_refresh") or "",
            "active_repair_interval_min": int(market_universe.get("active_repair_interval_min") or 0),
            "last_active_repair": market_universe.get("last_active_repair") or "",
            "last_active_repair_symbols": trim_array(market_universe.get("last_active_repair_symbols"), 12),
            "last_active_repair_symbols_total": (
                len(market_universe.get("last_active_repair_symbols"))
                if isinstance(market_universe.get("last_active_repair_symbols"), list)
                else int(market_universe.get("last_active_repair_symbols_total") or 0)
            ),
            "last_active_repair_reasons": trim_object_entries(market_universe.get("last_active_repair_reasons"), 12),
            "watchlist_backfill_interval_min": int(market_universe.get("watchlist_backfill_interval_min") or 0),
            "last_watchlist_backfill": market_universe.get("last_watchlist_backfill") or "",
        },
        "runtime_control": as_dict(payload.get("runtime_control")),
    }


__all__ = [
    "build_statusz_compute_payload",
    "build_statusz_live_readiness",
    "build_statusz_runtime_payload",
    "fetch_compute_health",
    "fetch_compute_monitor",
    "fetch_compute_status",
    "fetch_runtime_health",
    "fetch_runtime_status",
    "merge_service_topology",
    "split_reason_map_by_monitor",
    "split_symbol_list_by_monitor",
]
