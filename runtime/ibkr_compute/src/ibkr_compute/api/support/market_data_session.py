from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any


MARKET_DATA_SESSION_CONFLICT_CODE = "market_data_session_conflict"
MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE = 10197
MARKET_DATA_SESSION_CONFLICT_PHRASES = (
    "connected from a different ip address",
    "no market data during competing live session",
    "competing live session",
)
DEFAULT_TRACE_CONFLICT_WINDOW_SEC = 30 * 60


def is_market_data_session_conflict_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and any(phrase in text for phrase in MARKET_DATA_SESSION_CONFLICT_PHRASES))


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_timestamp_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0
        return int(number * 1000) if number < 10_000_000_000 else int(number)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = float(text)
    except (TypeError, ValueError):
        number = 0.0
    if number > 0:
        return int(number * 1000) if number < 10_000_000_000 else int(number)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _recent_enough(event_ms: int, *, now_ms: int, trace_window_sec: int) -> bool:
    if event_ms <= 0:
        return True
    age_s = max(0.0, (now_ms - event_ms) / 1000.0)
    return age_s <= max(1, int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC))


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
    error_code = _safe_int(trace.get("ib_error_code") or trace.get("error_code") or trace.get("last_error_code"))
    if error_code != MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE and not is_market_data_session_conflict_text(error):
        return None
    finished_ms = _trace_finished_ms(trace)
    if finished_ms <= 0:
        return None
    age_s = max(0.0, (now_ms - finished_ms) / 1000.0)
    if age_s > max(1, int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC)):
        return None
    return {
        "source": source,
        "message": error or f"IB error {error_code}",
        "trace_id": str(trace.get("trace_id") or ""),
        "finished_at_ms": finished_ms,
        "age_s": round(age_s, 1),
        **({"ib_error_code": error_code} if error_code else {}),
    }


def _append_conflict_evidence(
    evidence: list[dict[str, Any]],
    *,
    source: str,
    message: Any = "",
    ib_error_code: Any = 0,
    event_at: Any = None,
    now_ms: int,
    trace_window_sec: int,
) -> None:
    text = str(message or "").strip()
    error_code = _safe_int(ib_error_code)
    if error_code != MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE and not is_market_data_session_conflict_text(text):
        return
    event_ms = _parse_timestamp_ms(event_at)
    if not _recent_enough(event_ms, now_ms=now_ms, trace_window_sec=trace_window_sec):
        return
    item = {"source": source, "message": text or f"IB error {error_code}"}
    if error_code:
        item["ib_error_code"] = error_code
    if event_ms > 0:
        item["age_s"] = round(max(0.0, (now_ms - event_ms) / 1000.0), 1)
    evidence.append(item)


def _append_payload_evidence(
    evidence: list[dict[str, Any]],
    *,
    source: str,
    payload: dict[str, Any],
    now_ms: int,
    trace_window_sec: int,
) -> None:
    _append_conflict_evidence(
        evidence,
        source=source,
        message=payload.get("message") or payload.get("last_error") or payload.get("error"),
        ib_error_code=payload.get("code") or payload.get("last_error_code") or payload.get("ib_error_code"),
        event_at=payload.get("at") or payload.get("last_error_at") or payload.get("ts"),
        now_ms=now_ms,
        trace_window_sec=trace_window_sec,
    )


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
    _append_conflict_evidence(
        evidence,
        source="gateway.broker.last_error",
        message=broker.get("last_error"),
        ib_error_code=broker.get("last_error_code"),
        event_at=broker.get("last_error_at"),
        now_ms=now,
        trace_window_sec=trace_window_sec,
    )
    for index, item in enumerate(broker.get("recent_errors") or []):
        _append_payload_evidence(
            evidence,
            source=f"gateway.broker.recent_errors[{index}]",
            payload=_safe_dict(item),
            now_ms=now,
            trace_window_sec=trace_window_sec,
        )
    _append_conflict_evidence(
        evidence,
        source="gateway.last_error",
        message=gateway.get("last_error"),
        ib_error_code=gateway.get("last_error_code"),
        event_at=gateway.get("last_error_at"),
        now_ms=now,
        trace_window_sec=trace_window_sec,
    )

    canonical_5m = _safe_dict(status.get("canonical_5m"))
    _append_conflict_evidence(
        evidence,
        source="canonical_5m.last_error",
        message=canonical_5m.get("last_error"),
        ib_error_code=canonical_5m.get("last_error_code"),
        event_at=canonical_5m.get("last_error_at"),
        now_ms=now,
        trace_window_sec=trace_window_sec,
    )

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
    "MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE",
    "detect_market_data_session_conflict",
    "is_market_data_session_conflict_text",
]
