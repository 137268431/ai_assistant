from __future__ import annotations

from datetime import datetime
from typing import Any

from .status_types import AsDict, ET, NormalizeEnvironment, NormalizeSymbolList


def _derive_engine_symbols(
    engine_map: dict[str, Any],
    environment: str,
    required_interval: str,
) -> list[str]:
    derived_symbols: list[str] = []
    for key in engine_map:
        parts = str(key or "").split("/")
        if len(parts) != 3:
            continue
        if parts[0] == environment and parts[2] == required_interval:
            derived_symbols.append(parts[1])
    return derived_symbols


def _resolve_live_symbols(
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
        symbols = normalize_symbol_list(_derive_engine_symbols(engine_map, environment, required_interval))

    all_symbols = sorted(set(symbols + trade_symbols + monitor_symbols))
    return {
        "compute": compute,
        "runtime": runtime,
        "warmup": warmup,
        "market_universe": market_universe,
        "engine_map": engine_map,
        "environment": environment,
        "required_interval": required_interval,
        "trade_symbols": trade_symbols,
        "monitor_symbols": monitor_symbols,
        "all_symbols": all_symbols,
    }


def _count_ready_symbols(
    engine_map: dict[str, Any],
    environment: str,
    required_interval: str,
    all_symbols: list[str],
    trade_symbols: list[str],
    monitor_symbols: list[str],
) -> dict[str, Any]:
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

    non_monitor_pending_total = len(
        [symbol for symbol in all_symbols if symbol not in ready_set and symbol not in monitor_set]
    )
    monitor_pending_total = len(
        [symbol for symbol in all_symbols if symbol not in ready_set and symbol in monitor_set]
    )
    return {
        "ready_symbols": ready_symbols,
        "ready_trade_symbols": ready_trade_symbols,
        "ready_monitor_symbols": ready_monitor_symbols,
        "ready_set": ready_set,
        "non_monitor_pending_total": non_monitor_pending_total,
        "monitor_pending_total": monitor_pending_total,
        "gate_open": len(trade_symbols) > 0 and ready_trade_symbols >= len(trade_symbols),
        "phase": "ready" if all_symbols and ready_symbols >= len(all_symbols) else ("pending" if all_symbols else "idle"),
    }


def _as_readiness_summary_payload(
    source: str,
    payload: dict[str, Any],
    *,
    expected_symbols_total: int,
    required_interval: str,
) -> dict[str, Any]:
    readiness = payload.get("multi_timeframe_readiness") if isinstance(payload, dict) else {}
    if not isinstance(readiness, dict) or not readiness:
        return {}
    intervals = readiness.get("intervals") if isinstance(readiness.get("intervals"), dict) else {}
    interval_payload = intervals.get(required_interval)
    if not isinstance(interval_payload, dict):
        return {}
    symbols_total = int(readiness.get("symbols_total") or interval_payload.get("symbols_total") or 0)
    if expected_symbols_total > 0 and symbols_total < expected_symbols_total:
        return {}
    return {
        "source": source,
        "readiness": readiness,
        "interval": interval_payload,
        "symbols_total": symbols_total,
    }


def _build_live_readiness_from_summary(
    summary: dict[str, Any],
    *,
    environment: str,
    required_interval: str,
    all_symbols: list[str],
    trade_symbols: list[str],
    monitor_symbols: list[str],
    warmup: dict[str, Any],
    market_universe: dict[str, Any],
) -> dict[str, Any]:
    interval_payload = summary.get("interval") if isinstance(summary.get("interval"), dict) else {}
    symbols_total = int(summary.get("symbols_total") or len(all_symbols) or 0)
    missing_total = int(interval_payload.get("missing_ready_symbols_total") or 0)
    ready_symbols = max(0, symbols_total - missing_total)
    missing_symbols = {
        str(symbol or "").strip().upper()
        for symbol in (interval_payload.get("missing_ready_symbols") or [])
        if str(symbol or "").strip()
    }
    missing_list_is_complete = len(missing_symbols) >= missing_total
    if missing_total == 0:
        ready_trade_symbols = len(trade_symbols)
        ready_monitor_symbols = len(monitor_symbols)
    elif missing_list_is_complete:
        ready_trade_symbols = len([symbol for symbol in trade_symbols if symbol not in missing_symbols])
        ready_monitor_symbols = len([symbol for symbol in monitor_symbols if symbol not in missing_symbols])
    else:
        ready_trade_symbols = int(warmup.get("ready_trade_symbols") or 0)
        ready_monitor_symbols = int(warmup.get("ready_monitor_symbols") or 0)

    interval_status = str(interval_payload.get("status") or "").strip().lower()
    data_ready = bool(all_symbols) and interval_status == "ready" and missing_total == 0
    gate_open = bool(trade_symbols) and ready_trade_symbols >= len(trade_symbols)
    pending_symbols_total = max(0, symbols_total - ready_symbols)
    monitor_pending_total = max(0, len(monitor_symbols) - ready_monitor_symbols)
    non_monitor_pending_total = max(0, pending_symbols_total - monitor_pending_total)
    snapshot_phase = str(warmup.get("phase") or "").strip().lower() or "idle"
    snapshot_finished_at = warmup.get("finished_at") or ""
    snapshot_ready_symbols = int(warmup.get("ready_symbols") or 0)
    snapshot_differs = bool(
        warmup
        and (
            int(warmup.get("symbols_total") or 0) != symbols_total
            or snapshot_ready_symbols != ready_symbols
            or int(warmup.get("ready_trade_symbols") or 0) != ready_trade_symbols
            or int(warmup.get("ready_monitor_symbols") or 0) != ready_monitor_symbols
        )
    )
    return {
        "available": bool(all_symbols),
        "engine_snapshot_available": False,
        "source": str(summary.get("source") or "multi_timeframe_readiness"),
        "environment": environment,
        "required_interval": required_interval,
        "computed_at": datetime.now(tz=ET).isoformat(),
        "phase": "ready" if data_ready else ("degraded" if all_symbols else "idle"),
        "data_ready": data_ready,
        "trade_allowed": gate_open,
        "market_ws_ready": bool(market_universe.get("market_ws_ready")),
        "market_ws_symbols_ready": int(market_universe.get("market_ws_symbols_ready") or 0),
        "market_ws_symbols_total": int(market_universe.get("market_ws_symbols_total") or len(monitor_symbols) or 0),
        "gate_open": gate_open,
        "gate_reason": "ready" if gate_open else ("no_trade_symbols" if not trade_symbols else "live_not_ready"),
        "symbols_total": symbols_total,
        "trade_symbols_total": len(trade_symbols),
        "monitor_symbols_total": len(monitor_symbols),
        "ready_symbols": ready_symbols,
        "ready_trade_symbols": ready_trade_symbols,
        "ready_monitor_symbols": ready_monitor_symbols,
        "pending_symbols_total": pending_symbols_total,
        "non_monitor_pending_symbols_total": non_monitor_pending_total,
        "blocking_pending_symbols_total": pending_symbols_total,
        "monitor_pending_symbols_total": monitor_pending_total,
        "snapshot_differs": snapshot_differs,
        "snapshot_phase": snapshot_phase,
        "snapshot_finished_at": snapshot_finished_at,
    }


def build_statusz_live_readiness(
    compute_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
    *,
    as_dict: AsDict,
    normalize_environment: NormalizeEnvironment,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, Any]:
    resolved = _resolve_live_symbols(
        compute_payload,
        runtime_payload,
        as_dict=as_dict,
        normalize_environment=normalize_environment,
        normalize_symbol_list=normalize_symbol_list,
    )
    compute = resolved["compute"]
    runtime = resolved["runtime"]
    warmup = resolved["warmup"]
    engine_map = resolved["engine_map"]
    environment = resolved["environment"]
    required_interval = resolved["required_interval"]
    trade_symbols = resolved["trade_symbols"]
    monitor_symbols = resolved["monitor_symbols"]
    all_symbols = resolved["all_symbols"]
    market_universe = resolved["market_universe"]

    readiness_counts = _count_ready_symbols(
        engine_map,
        environment,
        required_interval,
        all_symbols,
        trade_symbols,
        monitor_symbols,
    )
    ready_symbols = readiness_counts["ready_symbols"]
    ready_trade_symbols = readiness_counts["ready_trade_symbols"]
    ready_monitor_symbols = readiness_counts["ready_monitor_symbols"]
    non_monitor_pending_total = readiness_counts["non_monitor_pending_total"]
    monitor_pending_total = readiness_counts["monitor_pending_total"]
    gate_open = readiness_counts["gate_open"]
    phase = readiness_counts["phase"]

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
    snapshot_non_monitor_pending_total = int(warmup.get("blocking_pending_symbols_total") or 0) or max(
        0, snapshot_pending_symbols_total - snapshot_monitor_pending_total
    )
    snapshot_gate_open = bool(warmup.get("trading_gate_open"))
    snapshot_gate_reason = str(warmup.get("trading_gate_reason") or "").strip().lower() or (
        "ready"
        if snapshot_trade_symbols_total > 0 and snapshot_gate_open
        else ("warmup_incomplete" if snapshot_trade_symbols_total > 0 else "no_trade_symbols")
    )
    snapshot_present = bool(
        snapshot_symbols_total
        or snapshot_ready_symbols
        or snapshot_ready_trade_symbols
        or snapshot_ready_monitor_symbols
        or str(warmup.get("phase") or "").strip()
        or str(warmup.get("finished_at") or "").strip()
    )
    prefer_runtime_snapshot = bool(
        snapshot_present
        and snapshot_phase == "ready"
        and snapshot_pending_symbols_total == 0
        and snapshot_ready_trade_symbols >= snapshot_trade_symbols_total
    )

    for summary in (
        _as_readiness_summary_payload(
            "runtime_multi_timeframe_readiness",
            runtime,
            expected_symbols_total=len(all_symbols),
            required_interval=required_interval,
        ),
        _as_readiness_summary_payload(
            "compute_multi_timeframe_readiness",
            compute,
            expected_symbols_total=len(all_symbols),
            required_interval=required_interval,
        ),
    ):
        if summary and not prefer_runtime_snapshot:
            return _build_live_readiness_from_summary(
                summary,
                environment=environment,
                required_interval=required_interval,
                all_symbols=all_symbols,
                trade_symbols=trade_symbols,
                monitor_symbols=monitor_symbols,
                warmup=warmup,
                market_universe=market_universe,
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
        or as_dict(runtime.get("service_topology")).get("runtime_mode")
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
            "data_ready": snapshot_phase == "ready" and snapshot_pending_symbols_total == 0,
            "trade_allowed": snapshot_gate_open,
            "market_ws_ready": bool(market_universe.get("market_ws_ready")),
            "market_ws_symbols_ready": int(market_universe.get("market_ws_symbols_ready") or 0),
            "market_ws_symbols_total": int(market_universe.get("market_ws_symbols_total") or snapshot_monitor_symbols_total or 0),
            "gate_open": snapshot_gate_open,
            "gate_reason": snapshot_gate_reason,
            "symbols_total": snapshot_symbols_total or len(all_symbols),
            "trade_symbols_total": snapshot_trade_symbols_total,
            "monitor_symbols_total": snapshot_monitor_symbols_total,
            "ready_symbols": snapshot_ready_symbols,
            "ready_trade_symbols": snapshot_ready_trade_symbols,
            "ready_monitor_symbols": snapshot_ready_monitor_symbols,
            "pending_symbols_total": snapshot_pending_symbols_total,
            "non_monitor_pending_symbols_total": snapshot_non_monitor_pending_total,
            "blocking_pending_symbols_total": snapshot_pending_symbols_total,
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
        "data_ready": phase == "ready" and ready_symbols >= len(all_symbols),
        "trade_allowed": gate_open,
        "market_ws_ready": bool(market_universe.get("market_ws_ready")),
        "market_ws_symbols_ready": int(market_universe.get("market_ws_symbols_ready") or 0),
        "market_ws_symbols_total": int(market_universe.get("market_ws_symbols_total") or len(monitor_symbols) or 0),
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
        "blocking_pending_symbols_total": max(0, len(all_symbols) - ready_symbols),
        "monitor_pending_symbols_total": monitor_pending_total,
        "snapshot_differs": snapshot_differs,
        "snapshot_phase": snapshot_phase,
        "snapshot_finished_at": snapshot_finished_at,
    }


__all__ = ["build_statusz_live_readiness"]
