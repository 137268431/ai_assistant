from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_symbols(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


_REASON_MESSAGES = {
    "ready": "Live readiness passed; trading data gate is open.",
    "background_repair": "Live readiness is usable while background repair continues.",
    "non_trade_readiness_pending": "Only monitor/non-trade readiness is pending; trade symbols are not blocked.",
    "session_unauthenticated": "IBKR session is not authenticated.",
    "runtime_stopped": "Runtime is stopped.",
    "remote_compute_status_unavailable": "Compute readiness is unavailable, so trade readiness cannot be confirmed.",
    "remote_compute_status_missing": "Compute readiness is missing for required trade symbols.",
    "missing_trade_symbols": "Required trade symbols are missing readiness.",
    "no_trade_symbols": "No trade symbols are active, so the trading gate is closed by design.",
    "live_not_ready": "Live readiness is not ready yet.",
    "warmup_incomplete": "Warmup is incomplete for active trade symbols.",
    "history_repair_pending": "Startup history repair is still marked pending.",
    "unavailable": "Trading gate status is unavailable.",
}


def _message_for(reason: str) -> str:
    return _REASON_MESSAGES.get(reason, reason.replace("_", " ") if reason else _REASON_MESSAGES["unavailable"])


def _blocker(code: str, detail: str = "", *, symbols: list[str] | None = None, trade_blocking: bool = True) -> dict[str, Any]:
    sample = _normalize_symbols(symbols or [])
    return {
        "code": code,
        "detail": detail or _message_for(code),
        "symbols_sample": sample[:20],
        "symbols_total": len(sample),
        "trade_blocking": bool(trade_blocking),
    }


def _raw_signal_gate(signal: dict[str, Any]) -> dict[str, Any]:
    reason = str(signal.get("trading_gate_reason") or "").strip() or "unknown"
    return {
        "open": bool(signal.get("trading_gate_open")),
        "reason": reason,
    }


def _raw_startup_snapshot(warmup: dict[str, Any]) -> dict[str, Any]:
    reason = str(warmup.get("trading_gate_reason") or "").strip() or "unknown"
    return {
        "open": bool(warmup.get("trading_gate_open")),
        "reason": reason,
        "phase": str(warmup.get("phase") or "").strip() or "unknown",
        "ready_trade_symbols": _to_int(warmup.get("ready_trade_symbols")),
        "trade_symbols_total": _to_int(warmup.get("trade_symbols_total")),
    }


def _base_gate(
    *,
    open: bool,
    source: str,
    reason: str,
    warmup: dict[str, Any],
    signal: dict[str, Any],
    blockers: list[dict[str, Any]] | None = None,
    snapshot_differs: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "open": bool(open),
        "trade_allowed": bool(open),
        "source": str(source or "unavailable"),
        "reason": str(reason or ("ready" if open else "unavailable")),
        "message": _message_for(str(reason or ("ready" if open else "unavailable"))),
        "blockers": list(blockers or []),
        "snapshot_differs": bool(snapshot_differs),
        "raw_signal_gate": _raw_signal_gate(signal),
        "raw_startup_snapshot": _raw_startup_snapshot(warmup),
    }
    if extra:
        payload.update(extra)
    return payload


def _snapshot_differs_from_open_live(warmup: dict[str, Any], open_gate: bool) -> bool:
    return bool(open_gate) and not bool(warmup.get("trading_gate_open"))


def _gate_from_live_readiness(
    live: dict[str, Any],
    *,
    warmup: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any] | None:
    if not live or not bool(live.get("available", True)):
        return None
    source = str(live.get("source") or "live_readiness").strip() or "live_readiness"
    gate_open = bool(live.get("gate_open") or live.get("trade_allowed"))
    reason = str(live.get("gate_reason") or "").strip() or ("ready" if gate_open else "live_not_ready")
    snapshot_differs = bool(live.get("snapshot_differs")) or _snapshot_differs_from_open_live(warmup, gate_open)
    blockers: list[dict[str, Any]] = []
    if not gate_open:
        pending_total = _to_int(live.get("blocking_pending_symbols_total") or live.get("non_monitor_pending_symbols_total"))
        blockers.append(
            _blocker(
                reason,
                symbols=_normalize_symbols(live.get("blocking_pending_symbols") or live.get("pending_symbols") or []),
                trade_blocking=reason != "no_trade_symbols",
                detail=_message_for(reason) if pending_total <= 0 else f"{_message_for(reason)} pending={pending_total}",
            )
        )
    return _base_gate(
        open=gate_open,
        source=source,
        reason=reason,
        warmup=warmup,
        signal=signal,
        blockers=blockers,
        snapshot_differs=snapshot_differs,
        extra={
            "ready_trade_symbols": _to_int(live.get("ready_trade_symbols")),
            "trade_symbols_total": _to_int(live.get("trade_symbols_total")),
            "ready_monitor_symbols": _to_int(live.get("ready_monitor_symbols")),
            "monitor_symbols_total": _to_int(live.get("monitor_symbols_total")),
        },
    )


def _hard_interval_payload(readiness: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    intervals = _as_dict(readiness.get("intervals"))
    hard_interval = str(readiness.get("hard_gate_interval") or "5m").strip() or "5m"
    interval = _as_dict(intervals.get(hard_interval))
    if interval:
        return hard_interval, interval
    for name, item in intervals.items():
        candidate = _as_dict(item)
        if bool(candidate.get("hard_gate")):
            return str(name), candidate
    return hard_interval, {}


def _gate_from_multitimeframe(
    runtime: dict[str, Any],
    *,
    warmup: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any] | None:
    readiness = _as_dict(runtime.get("multi_timeframe_readiness"))
    if not readiness:
        return None
    hard_interval, interval = _hard_interval_payload(readiness)
    trade_symbols = _normalize_symbols(
        warmup.get("trade_symbols")
        or _as_dict(runtime.get("market_universe")).get("active_trade_symbols")
        or []
    )
    trade_total = _to_int(warmup.get("trade_symbols_total"), len(trade_symbols)) or len(trade_symbols)
    if trade_total <= 0 and not trade_symbols:
        return _base_gate(
            open=False,
            source="runtime_multi_timeframe_readiness",
            reason="no_trade_symbols",
            warmup=warmup,
            signal=signal,
            blockers=[_blocker("no_trade_symbols", trade_blocking=False)],
            extra={"hard_gate_interval": hard_interval, "trade_symbols_total": 0},
        )
    if not interval:
        return _base_gate(
            open=False,
            source="runtime_multi_timeframe_readiness",
            reason="remote_compute_status_unavailable",
            warmup=warmup,
            signal=signal,
            blockers=[_blocker("remote_compute_status_unavailable")],
            extra={"hard_gate_interval": hard_interval, "trade_symbols_total": trade_total},
        )

    status = str(interval.get("status") or "").strip().lower()
    missing_ready = _normalize_symbols(interval.get("missing_ready_symbols") or [])
    missing_ready_total = _to_int(interval.get("missing_ready_symbols_total"), len(missing_ready))
    missing_trade = sorted(set(missing_ready).intersection(trade_symbols)) if trade_symbols else []
    ready_all = status == "ready" and missing_ready_total == 0
    if ready_all or (missing_ready_total > 0 and missing_ready and not missing_trade):
        reason = "ready" if ready_all else "non_trade_readiness_pending"
        return _base_gate(
            open=True,
            source="runtime_multi_timeframe_readiness",
            reason=reason,
            warmup=warmup,
            signal=signal,
            snapshot_differs=_snapshot_differs_from_open_live(warmup, True),
            extra={
                "hard_gate_interval": hard_interval,
                "ready_trade_symbols": trade_total,
                "trade_symbols_total": trade_total,
                "missing_ready_symbols_total": missing_ready_total,
            },
        )

    reason = "missing_trade_symbols" if missing_trade else "remote_compute_status_missing"
    detail_symbols = missing_trade or missing_ready
    return _base_gate(
        open=False,
        source="runtime_multi_timeframe_readiness",
        reason=reason,
        warmup=warmup,
        signal=signal,
        blockers=[_blocker(reason, symbols=detail_symbols)],
        extra={
            "hard_gate_interval": hard_interval,
            "trade_symbols_total": trade_total,
            "missing_ready_symbols_total": missing_ready_total,
        },
    )


def build_effective_trading_gate(
    runtime_payload: dict[str, Any] | None,
    *,
    live_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = _as_dict(runtime_payload)
    warmup = _as_dict(runtime.get("warmup"))
    signal = _as_dict(runtime.get("signal_processor"))
    live = _as_dict(live_readiness) or _as_dict(runtime.get("live_readiness"))

    live_gate = _gate_from_live_readiness(live, warmup=warmup, signal=signal)
    if live_gate is not None and bool(live_gate.get("open")):
        return live_gate
    if bool(signal.get("trading_gate_open")):
        reason = str(signal.get("trading_gate_reason") or "ready").strip() or "ready"
        return _base_gate(
            open=True,
            source="signal_processor",
            reason=reason,
            warmup=warmup,
            signal=signal,
            snapshot_differs=_snapshot_differs_from_open_live(warmup, True),
        )

    readiness_gate = _gate_from_multitimeframe(runtime, warmup=warmup, signal=signal)
    if readiness_gate is not None:
        return readiness_gate
    if live_gate is not None:
        return live_gate

    if signal:
        gate_open = bool(signal.get("trading_gate_open"))
        reason = str(signal.get("trading_gate_reason") or "").strip() or ("ready" if gate_open else "unavailable")
        return _base_gate(
            open=gate_open,
            source="signal_processor",
            reason=reason,
            warmup=warmup,
            signal=signal,
            blockers=[] if gate_open else [_blocker(reason)],
            snapshot_differs=_snapshot_differs_from_open_live(warmup, gate_open),
        )

    snapshot_open = bool(warmup.get("trading_gate_open"))
    snapshot_reason = str(warmup.get("trading_gate_reason") or "").strip() or ("ready" if snapshot_open else "unavailable")
    return _base_gate(
        open=snapshot_open,
        source="startup_snapshot",
        reason=snapshot_reason,
        warmup=warmup,
        signal=signal,
        blockers=[] if snapshot_open else [_blocker(snapshot_reason)],
    )


__all__ = ["build_effective_trading_gate"]
