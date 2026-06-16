from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any


MARKET_DATA_SESSION_CONFLICT_CODE = "market_data_session_conflict"
MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE = 10197
MARKET_DATA_SESSION_CONFLICT_STATE_KEY = "market_data_session_conflict"
MARKET_DATA_SESSION_CONFLICT_STATE_DATE = "global"
MARKET_DATA_SESSION_CONFLICT_PERSIST_INTERVAL_SEC = 300
MARKET_DATA_SESSION_CONFLICT_REMINDER_INTERVAL_SEC = 60 * 60
MARKET_DATA_SESSION_CONFLICT_PHRASES = (
    "connected from a different ip address",
    "no market data during competing live session",
    "competing live session",
)
DEFAULT_TRACE_CONFLICT_WINDOW_SEC = 30 * 60
DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC = 3 * 60
DEFAULT_RECOVERY_QUOTE_FRESH_SEC = 120


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


def _iso_from_ms(value: Any, *, fallback_ms: int = 0) -> str:
    timestamp_ms = _parse_timestamp_ms(value)
    if timestamp_ms <= 0:
        timestamp_ms = int(fallback_ms or 0)
    if timestamp_ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _recent_enough(event_ms: int, *, now_ms: int, trace_window_sec: int) -> bool:
    if event_ms <= 0:
        return True
    age_s = max(0.0, (now_ms - event_ms) / 1000.0)
    return age_s <= max(1, int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC))


def _active_enough(event_ms: int, *, now_ms: int, active_window_sec: int) -> bool:
    if event_ms <= 0:
        return True
    age_s = max(0.0, (now_ms - event_ms) / 1000.0)
    return age_s <= max(1, int(active_window_sec or DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC))


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
        "event_at_ms": finished_ms,
        "event_at": _iso_from_ms(finished_ms),
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
        item["event_at_ms"] = event_ms
        item["event_at"] = _iso_from_ms(event_ms)
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
    active_window_sec: int = DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC,
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

    active_evidence = [
        item
        for item in evidence
        if _active_enough(
            _safe_int(item.get("event_at_ms") or item.get("finished_at_ms")),
            now_ms=now,
            active_window_sec=active_window_sec,
        )
    ]
    first = active_evidence[0] if active_evidence else (evidence[0] if evidence else {})
    return {
        "active": bool(active_evidence),
        "code": MARKET_DATA_SESSION_CONFLICT_CODE if evidence else "",
        "message": str(first.get("message") or ""),
        "source": str(first.get("source") or ""),
        "event_at": str(first.get("event_at") or ""),
        "event_at_ms": _safe_int(first.get("event_at_ms") or first.get("finished_at_ms")),
        "trace_window_sec": int(trace_window_sec or DEFAULT_TRACE_CONFLICT_WINDOW_SEC),
        "active_window_sec": int(active_window_sec or DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC),
        "evidence": evidence,
        "active_evidence": active_evidence,
    }


def _fresh_quote_count(realtime_quotes: dict[str, Any], *, quote_fresh_sec: int) -> int:
    quotes = realtime_quotes.get("quotes") if isinstance(realtime_quotes.get("quotes"), dict) else {}
    count = 0
    for quote in quotes.values():
        if not isinstance(quote, dict):
            continue
        age = quote.get("quote_age_s")
        if age is not None and _safe_float(age, quote_fresh_sec + 1) <= max(1, int(quote_fresh_sec or 0)):
            count += 1
    return count


def _recovery_evidence(
    runtime_status: dict[str, Any],
    *,
    quote_fresh_sec: int = DEFAULT_RECOVERY_QUOTE_FRESH_SEC,
) -> dict[str, Any]:
    gateway = _safe_dict(runtime_status.get("gateway"))
    broker = _safe_dict(gateway.get("broker"))
    session = _safe_dict(runtime_status.get("session"))
    websocket = _safe_dict(runtime_status.get("websocket"))
    realtime_quotes = _safe_dict(runtime_status.get("realtime_quotes"))

    gateway_ready = bool(gateway.get("running") or gateway.get("reachable"))
    broker_ready = bool(broker.get("ready") or broker.get("connected") or not broker)
    session_authenticated = bool(session.get("authenticated"))
    websocket_ready = bool(websocket.get("connected") or websocket.get("ready"))
    subscribed_count = _safe_int(websocket.get("subscribed_count"), 0)
    total_quotes = _safe_int(realtime_quotes.get("total_quotes"), 0)
    stale_quotes = _safe_int(realtime_quotes.get("stale_quotes"), 0)
    fresh_quotes = _fresh_quote_count(realtime_quotes, quote_fresh_sec=quote_fresh_sec)

    blockers: list[str] = []
    if not gateway_ready:
        blockers.append("gateway_not_ready")
    if not broker_ready:
        blockers.append("broker_not_ready")
    if not session_authenticated:
        blockers.append("session_not_authenticated")
    if not websocket_ready:
        blockers.append("websocket_not_ready")
    if subscribed_count > 0:
        if total_quotes <= 0:
            blockers.append("realtime_quotes_missing")
        elif fresh_quotes <= 0 and stale_quotes >= total_quotes:
            blockers.append("realtime_quotes_stale")

    return {
        "ok": not blockers,
        "gateway_ready": gateway_ready,
        "broker_ready": broker_ready,
        "session_authenticated": session_authenticated,
        "websocket_ready": websocket_ready,
        "subscribed_count": subscribed_count,
        "total_quotes": total_quotes,
        "stale_quotes": stale_quotes,
        "fresh_quotes": fresh_quotes,
        "quote_fresh_sec": int(quote_fresh_sec or DEFAULT_RECOVERY_QUOTE_FRESH_SEC),
        "blockers": blockers,
    }


def _state_payload(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    data = record.get("data")
    if isinstance(data, dict):
        return dict(data)
    return dict(record)


def _load_conflict_state(owner: Any, environment: str) -> dict[str, Any]:
    cached = getattr(owner, "_market_data_session_conflict_state", None)
    loaded = bool(getattr(owner, "_market_data_session_conflict_state_loaded", False))
    if loaded and isinstance(cached, dict):
        return dict(cached)
    pb = getattr(owner, "pb", None) or getattr(owner, "pb_client", None)
    state: dict[str, Any] = {}
    getter = getattr(pb, "get_state", None)
    if callable(getter):
        try:
            state = _state_payload(
                getter(
                    MARKET_DATA_SESSION_CONFLICT_STATE_KEY,
                    environment,
                    MARKET_DATA_SESSION_CONFLICT_STATE_DATE,
                )
            )
        except Exception:
            state = {}
    try:
        setattr(owner, "_market_data_session_conflict_state", dict(state))
        setattr(owner, "_market_data_session_conflict_state_loaded", True)
    except Exception:
        pass
    return dict(state)


def _conflict_signature(conflict: dict[str, Any]) -> str:
    evidence = conflict.get("evidence") if isinstance(conflict.get("evidence"), list) else []
    first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    return "|".join(
        [
            str(conflict.get("source") or first.get("source") or ""),
            str(conflict.get("message") or first.get("message") or ""),
            str(conflict.get("event_at_ms") or first.get("event_at_ms") or first.get("finished_at_ms") or ""),
            str(first.get("ib_error_code") or ""),
        ]
    )


def _compact_evidence(conflict: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in (conflict.get("evidence") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                key: item.get(key)
                for key in (
                    "source",
                    "message",
                    "ib_error_code",
                    "event_at",
                    "event_at_ms",
                    "age_s",
                    "trace_id",
                )
                if item.get(key) not in (None, "")
            }
        )
    return compact


def _persist_conflict_state(owner: Any, environment: str, state: dict[str, Any]) -> None:
    pb = getattr(owner, "pb", None) or getattr(owner, "pb_client", None)
    upsert = getattr(pb, "upsert_state", None)
    if not callable(upsert):
        return
    try:
        upsert(
            MARKET_DATA_SESSION_CONFLICT_STATE_KEY,
            environment,
            state,
            date=MARKET_DATA_SESSION_CONFLICT_STATE_DATE,
        )
    except TypeError:
        try:
            upsert(MARKET_DATA_SESSION_CONFLICT_STATE_KEY, environment, state, MARKET_DATA_SESSION_CONFLICT_STATE_DATE)
        except Exception:
            pass
    except Exception:
        pass


def _record_conflict_system_event(
    owner: Any,
    *,
    environment: str,
    title: str,
    detail: dict[str, Any],
    level: str,
    now_ms: int,
) -> None:
    pb = getattr(owner, "pb", None) or getattr(owner, "pb_client", None)
    create = getattr(pb, "create_record", None)
    if not callable(create):
        return
    now_iso = _iso_from_ms(now_ms)
    payload = {
        "event_type": MARKET_DATA_SESSION_CONFLICT_STATE_KEY,
        "level": str(level or "warning"),
        "source": "ibkr_compute",
        "environment": environment,
        "title": title,
        "detail": detail,
        "us_time": now_iso,
        "cn_time": now_iso,
        "notified": False,
    }
    try:
        create("system_events", payload)
    except Exception:
        pass


def record_market_data_session_conflict_state(
    owner: Any,
    runtime_status: dict[str, Any] | None,
    *,
    environment: str,
    now_ms: int | None = None,
    persist_interval_sec: int = MARKET_DATA_SESSION_CONFLICT_PERSIST_INTERVAL_SEC,
    reminder_interval_sec: int = MARKET_DATA_SESSION_CONFLICT_REMINDER_INTERVAL_SEC,
    active_window_sec: int = DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC,
    recovery_quote_fresh_sec: int = DEFAULT_RECOVERY_QUOTE_FRESH_SEC,
) -> dict[str, Any]:
    status = _safe_dict(runtime_status)
    runtime_environment = (
        str(environment or status.get("data_environment") or status.get("environment") or "live").strip().lower()
        or "live"
    )
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    now_iso = _iso_from_ms(now)
    previous = _load_conflict_state(owner, runtime_environment)
    conflict = detect_market_data_session_conflict(
        status,
        now_ms=now,
        active_window_sec=active_window_sec,
    )
    previous_active = bool(previous.get("active"))
    previous_signature = str(previous.get("last_signature") or "")
    last_persist_ms = _parse_timestamp_ms(previous.get("last_persisted_at"))
    last_event_ms = _parse_timestamp_ms(previous.get("last_event_recorded_at"))

    if bool(conflict.get("active")):
        signature = _conflict_signature(conflict)
        is_new_signature = signature and signature != previous_signature
        count = _safe_int(previous.get("count"), 0)
        if not previous_active or is_new_signature or count <= 0:
            count += 1
        first_seen_at = str(previous.get("first_seen_at") or now_iso)
        event_at = str(conflict.get("event_at") or "") or now_iso
        state = {
            **previous,
            "active": True,
            "code": MARKET_DATA_SESSION_CONFLICT_CODE,
            "environment": runtime_environment,
            "first_seen_at": first_seen_at,
            "last_seen_at": now_iso,
            "last_error_at": event_at,
            "last_error_at_ms": _safe_int(conflict.get("event_at_ms"), 0),
            "resolved_at": "",
            "count": count,
            "message": str(conflict.get("message") or ""),
            "source": str(conflict.get("source") or ""),
            "evidence": _compact_evidence(conflict),
            "active_evidence": _compact_evidence({"evidence": conflict.get("active_evidence") or []}),
            "last_signature": signature,
            "trace_window_sec": _safe_int(conflict.get("trace_window_sec"), DEFAULT_TRACE_CONFLICT_WINDOW_SEC),
            "active_window_sec": _safe_int(conflict.get("active_window_sec"), DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC),
            "recovery_evidence": {},
            "recovery_action": "",
            "resubscribed_count": 0,
            "resubscribed_at": "",
            "resubscribe_resolved_at": "",
            "recommended_action": (
                "Exit other TWS, IB Gateway, IBKR Desktop, Client Portal, mobile, or third-party market-data clients; "
                "wait 1-3 minutes; restart Gateway only if the conflict remains after the external session is released."
            ),
        }
        should_persist = (
            not previous_active
            or is_new_signature
            or last_persist_ms <= 0
            or (now - last_persist_ms) >= max(1, int(persist_interval_sec or 0)) * 1000
        )
        should_record_event = (
            not previous_active
            or last_event_ms <= 0
            or (now - last_event_ms) >= max(1, int(reminder_interval_sec or 0)) * 1000
        )
        if should_persist and not should_record_event:
            state["last_persisted_at"] = now_iso
            _persist_conflict_state(owner, runtime_environment, state)
        if should_record_event:
            state["last_event_recorded_at"] = now_iso
            _record_conflict_system_event(
                owner,
                environment=runtime_environment,
                title="IBKR market data session conflict recorded",
                level="warning",
                now_ms=now,
                detail={
                    "code": MARKET_DATA_SESSION_CONFLICT_CODE,
                    "message": state["message"],
                    "source": state["source"],
                    "first_seen_at": state["first_seen_at"],
                    "last_error_at": state["last_error_at"],
                    "count": state["count"],
                    "recommended_action": state["recommended_action"],
                },
            )
            state["last_persisted_at"] = now_iso
            _persist_conflict_state(owner, runtime_environment, state)
        try:
            setattr(owner, "_market_data_session_conflict_state", dict(state))
            setattr(owner, "_market_data_session_conflict_state_loaded", True)
        except Exception:
            pass
        return dict(state)

    if previous_active:
        recovery_evidence = _recovery_evidence(status, quote_fresh_sec=recovery_quote_fresh_sec)
        if not bool(recovery_evidence.get("ok")):
            state = {
                **previous,
                "active": True,
                "environment": runtime_environment,
                "recovery_pending": True,
                "recovery_evidence": recovery_evidence,
                "active_window_sec": int(active_window_sec or DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC),
                "recommended_action": (
                    "No fresh 10197 is detected, but runtime evidence is not healthy enough yet; "
                    "wait for Gateway, Session, WebSocket, and fresh quotes to recover."
                ),
            }
            if last_persist_ms <= 0 or (now - last_persist_ms) >= max(1, int(persist_interval_sec or 0)) * 1000:
                state["last_persisted_at"] = now_iso
                _persist_conflict_state(owner, runtime_environment, state)
            try:
                setattr(owner, "_market_data_session_conflict_state", dict(state))
                setattr(owner, "_market_data_session_conflict_state_loaded", True)
            except Exception:
                pass
            return dict(state)

        state = {
            **previous,
            "active": False,
            "environment": runtime_environment,
            "last_seen_at": str(previous.get("last_seen_at") or ""),
            "resolved_at": now_iso,
            "last_persisted_at": now_iso,
            "recovery_pending": False,
            "recovery_evidence": recovery_evidence,
            "recovery_action": "pending_resubscribe",
            "resubscribed_count": _safe_int(previous.get("resubscribed_count"), 0),
            "resubscribed_at": str(previous.get("resubscribed_at") or ""),
            "resubscribe_resolved_at": str(previous.get("resubscribe_resolved_at") or ""),
            "active_window_sec": int(active_window_sec or DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC),
            "recommended_action": "Conflict is no longer detected; verify live market-data messages are fresh.",
        }
        _persist_conflict_state(owner, runtime_environment, state)
        _record_conflict_system_event(
            owner,
            environment=runtime_environment,
            title="IBKR market data session conflict resolved",
            level="info",
            now_ms=now,
            detail={
                "code": MARKET_DATA_SESSION_CONFLICT_CODE,
                "first_seen_at": state.get("first_seen_at", ""),
                "last_seen_at": state.get("last_seen_at", ""),
                "resolved_at": state.get("resolved_at", ""),
                "count": state.get("count", 0),
                "recovery_evidence": recovery_evidence,
            },
        )
        try:
            setattr(owner, "_market_data_session_conflict_state", dict(state))
            setattr(owner, "_market_data_session_conflict_state_loaded", True)
        except Exception:
            pass
        return dict(state)

    state = dict(previous)
    state.setdefault("active", False)
    state.setdefault("code", MARKET_DATA_SESSION_CONFLICT_CODE)
    state.setdefault("environment", runtime_environment)
    try:
        setattr(owner, "_market_data_session_conflict_state", dict(state))
        setattr(owner, "_market_data_session_conflict_state_loaded", True)
    except Exception:
        pass
    return state


__all__ = [
    "DEFAULT_ACTIVE_CONFLICT_WINDOW_SEC",
    "DEFAULT_RECOVERY_QUOTE_FRESH_SEC",
    "DEFAULT_TRACE_CONFLICT_WINDOW_SEC",
    "MARKET_DATA_SESSION_CONFLICT_CODE",
    "MARKET_DATA_SESSION_CONFLICT_IB_ERROR_CODE",
    "MARKET_DATA_SESSION_CONFLICT_STATE_DATE",
    "MARKET_DATA_SESSION_CONFLICT_STATE_KEY",
    "detect_market_data_session_conflict",
    "is_market_data_session_conflict_text",
    "record_market_data_session_conflict_state",
]
