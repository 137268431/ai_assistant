from __future__ import annotations

import time
from typing import Any


MARKET_DATA_SESSION_CONFLICT_CODE = "market_data_session_conflict"
MARKET_DATA_SESSION_CONFLICT_PHRASE = "connected from a different ip address"
DEFAULT_TRACE_CONFLICT_WINDOW_SEC = 30 * 60


def is_market_data_session_conflict_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and MARKET_DATA_SESSION_CONFLICT_PHRASE in text)


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _trace_finished_ms(trace: dict[str, Any]) -> int:
    for key in ("finished_at_ms", "updated_at_ms", "last_error_at_ms"):
        value = trace.get(key)
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def _trace_recent_evidence(
    *,
    trace: dict[str, Any],
    source: str,
    now_ms: int,
    trace_window_sec: int,
) -> dict[str, Any] | None:
    error = str(trace.get("error") or trace.get("last_error") or "").strip()
    if not is_market_data_session_conflict_text(error):
        return None
    finished_ms = _trace_finished_ms(trace)
    if finished_ms <= 0:
        return None
    age_s = max(0.0, (now_ms - finished_ms) / 1000.0)
    if age_s > max(1, int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC)):
        return None
    return {
        "source": source,
        "message": error,
        "trace_id": str(trace.get("trace_id") or ""),
        "finished_at_ms": finished_ms,
        "age_s": round(age_s, 1),
    }


def _append_text_evidence(evidence: list[dict[str, Any]], *, source: str, message: Any) -> None:
    text = str(message or "").strip()
    if not is_market_data_session_conflict_text(text):
        return
    evidence.append({"source": source, "message": text})


def detect_market_data_session_conflict(
    runtime_status: dict[str, Any] | None,
    *,
    now_ms: int | None = None,
    trace_window_sec: int = DEFAULT_TRACE_CONFLICT_WINDOW_SEC,
) -> dict[str, Any]:
    status = _safe_dict(runtime_status)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    evidence: list[dict[str, Any]] = []

    data_backfill = _safe_dict(status.get("data_backfill"))
    last_trace = _safe_dict(data_backfill.get("last_trace"))
    item = _trace_recent_evidence(
        trace=last_trace,
        source="data_backfill.last_trace.error",
        now_ms=now,
        trace_window_sec=trace_window_sec,
    )
    if item:
        evidence.append(item)

    for index, trace in enumerate(data_backfill.get("recent_traces") or []):
        item = _trace_recent_evidence(
            trace=_safe_dict(trace),
            source=f"data_backfill.recent_traces[{index}].error",
            now_ms=now,
            trace_window_sec=trace_window_sec,
        )
        if item:
            evidence.append(item)

    gateway = _safe_dict(status.get("gateway"))
    broker = _safe_dict(gateway.get("broker"))
    _append_text_evidence(evidence, source="gateway.broker.last_error", message=broker.get("last_error"))
    _append_text_evidence(evidence, source="gateway.last_error", message=gateway.get("last_error"))

    canonical_5m = _safe_dict(status.get("canonical_5m"))
    _append_text_evidence(evidence, source="canonical_5m.last_error", message=canonical_5m.get("last_error"))

    first = evidence[0] if evidence else {}
    return {
        "active": bool(evidence),
        "code": MARKET_DATA_SESSION_CONFLICT_CODE if evidence else "",
        "message": str(first.get("message") or ""),
        "source": str(first.get("source") or ""),
        "trace_window_sec": int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC),
        "evidence": evidence,
    }


__all__ = [
    "DEFAULT_TRACE_CONFLICT_WINDOW_SEC",
    "MARKET_DATA_SESSION_CONFLICT_CODE",
    "detect_market_data_session_conflict",
    "is_market_data_session_conflict_text",
]
