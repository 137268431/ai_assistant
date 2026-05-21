"""Large-operation alerting helpers shared by IBKR services."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

try:
    from ibkr_compute.core.broker_mode import configured_broker_mode
except Exception:  # pragma: no cover - import fallback for isolated tests
    configured_broker_mode = lambda: "paper"  # type: ignore


ALERT_STATE_PREFIX = "large_operation_notify:"
HIGH_RISK_INTERVALS = {"4h", "1d"}
DEFAULT_THRESHOLDS = {
    "min_symbols": 25,
    "min_tasks": 50,
    "min_period_days": 120,
    "min_duration_s": 120,
    "min_written": 10_000,
    "min_requests": 100,
    "min_retry": 10,
    "min_throttle": 50,
    "min_time_budget_s": 120,
    "progress_cooldown_s": 300,
}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default or 0)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default or 0.0)


def _config_int(config: Any, key: str, environment: str, default: int) -> int:
    getter = getattr(config, "get_int_for_environment", None)
    if callable(getter):
        try:
            return int(getter(key, environment, default))
        except Exception:
            return int(default)
    return int(default)


def _config_bool(config: Any, key: str, environment: str, default: bool) -> bool:
    getter = getattr(config, "get_bool_for_environment", None)
    if callable(getter):
        try:
            return bool(getter(key, environment, default))
        except Exception:
            return bool(default)
    return bool(default)


def _normalize_interval(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "5min": "5m",
        "5mins": "5m",
        "15min": "15m",
        "15mins": "15m",
        "30min": "30m",
        "30mins": "30m",
        "1hour": "1h",
        "4hour": "4h",
        "1day": "1d",
    }
    return aliases.get(text, text)


def _parse_csv(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    parsed: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text:
            parsed.append(text)
    return parsed


def _period_days(value: Any) -> int:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    suffix = "".join(ch for ch in text if ch.isalpha())
    if not digits:
        return 0
    amount = max(1, int(digits))
    if suffix == "d":
        return amount
    if suffix == "w":
        return amount * 7
    if suffix == "m":
        return amount * 30
    if suffix == "y":
        return amount * 365
    return amount


def _periods_from_operation(operation: Mapping[str, Any]) -> list[str]:
    periods: list[str] = []
    for key in ("period", "request_period", "time_budget_period"):
        value = operation.get(key)
        if value:
            periods.extend(_parse_csv(value) or [str(value)])
    raw_periods = operation.get("periods")
    if isinstance(raw_periods, Mapping):
        for value in raw_periods.values():
            if isinstance(value, Mapping):
                periods.extend(str(item) for item in value.values() if str(item or "").strip())
            elif str(value or "").strip():
                periods.append(str(value))
    else:
        periods.extend(_parse_csv(raw_periods))
    return periods


def _symbols_total(operation: Mapping[str, Any]) -> int:
    explicit = _to_int(operation.get("symbols_total"), 0)
    if explicit > 0:
        return explicit
    symbols = operation.get("symbols")
    if isinstance(symbols, (list, tuple, set)):
        return len([item for item in symbols if str(item or "").strip()])
    return 0


def _intervals(operation: Mapping[str, Any]) -> list[str]:
    explicit = [_normalize_interval(item) for item in _parse_csv(operation.get("intervals"))]
    return [item for item in dict.fromkeys(explicit) if item]


def large_operation_reasons(
    operation: Mapping[str, Any],
    *,
    config: Any = None,
    environment: str = "live",
) -> list[str]:
    """Return threshold reasons that make an operation large enough to alert."""

    env = str(environment or operation.get("data_environment") or "live").strip().lower() or "live"
    symbols_total = _symbols_total(operation)
    intervals = _intervals(operation)
    interval_count = max(1, len(intervals))
    task_count = _to_int(operation.get("task_count"), symbols_total * interval_count)
    max_period_days = max((_period_days(item) for item in _periods_from_operation(operation)), default=0)
    duration_s = _to_float(operation.get("duration_s"), 0.0)
    request_count = _to_int(operation.get("request_count"), 0)
    retry_count = _to_int(operation.get("retry_count"), 0)
    throttle_count = _to_int(operation.get("throttle_count"), 0)
    written = _to_int(operation.get("written"), 0)
    time_budget_s = _to_int(operation.get("time_budget_s"), 0)

    thresholds = {
        "min_symbols": _config_int(config, "ibkr_large_operation_alert_min_symbols", env, DEFAULT_THRESHOLDS["min_symbols"]),
        "min_tasks": _config_int(config, "ibkr_large_operation_alert_min_tasks", env, DEFAULT_THRESHOLDS["min_tasks"]),
        "min_period_days": _config_int(config, "ibkr_large_operation_alert_min_period_days", env, DEFAULT_THRESHOLDS["min_period_days"]),
        "min_duration_s": _config_int(config, "ibkr_large_operation_alert_min_duration_s", env, DEFAULT_THRESHOLDS["min_duration_s"]),
        "min_written": _config_int(config, "ibkr_large_operation_alert_min_written", env, DEFAULT_THRESHOLDS["min_written"]),
        "min_requests": _config_int(config, "ibkr_large_operation_alert_min_requests", env, DEFAULT_THRESHOLDS["min_requests"]),
        "min_retry": _config_int(config, "ibkr_large_operation_alert_min_retry", env, DEFAULT_THRESHOLDS["min_retry"]),
        "min_throttle": _config_int(config, "ibkr_large_operation_alert_min_throttle", env, DEFAULT_THRESHOLDS["min_throttle"]),
        "min_time_budget_s": _config_int(config, "ibkr_large_operation_alert_min_time_budget_s", env, DEFAULT_THRESHOLDS["min_time_budget_s"]),
    }

    reasons: list[str] = []
    if symbols_total >= thresholds["min_symbols"]:
        reasons.append(f"symbols>={thresholds['min_symbols']}:{symbols_total}")
    if task_count >= thresholds["min_tasks"]:
        reasons.append(f"tasks>={thresholds['min_tasks']}:{task_count}")
    high_intervals = sorted(set(intervals) & HIGH_RISK_INTERVALS)
    if high_intervals:
        reasons.append(f"high_interval:{','.join(high_intervals)}")
    if max_period_days >= thresholds["min_period_days"]:
        reasons.append(f"period_days>={thresholds['min_period_days']}:{max_period_days}")
    if time_budget_s >= thresholds["min_time_budget_s"]:
        reasons.append(f"time_budget_s>={thresholds['min_time_budget_s']}:{time_budget_s}")
    if duration_s >= thresholds["min_duration_s"]:
        reasons.append(f"duration_s>={thresholds['min_duration_s']}:{duration_s:.1f}")
    if request_count >= thresholds["min_requests"]:
        reasons.append(f"requests>={thresholds['min_requests']}:{request_count}")
    if retry_count >= thresholds["min_retry"]:
        reasons.append(f"retry>={thresholds['min_retry']}:{retry_count}")
    if throttle_count >= thresholds["min_throttle"]:
        reasons.append(f"throttle>={thresholds['min_throttle']}:{throttle_count}")
    if written >= thresholds["min_written"]:
        reasons.append(f"written>={thresholds['min_written']}:{written}")
    if bool(operation.get("force_large_operation")):
        planned_reason = str(operation.get("planned_large_reason") or "forced_large_operation").strip()
        reasons.append(planned_reason or "forced_large_operation")
    for item in _parse_csv(operation.get("large_reasons")):
        if item not in reasons:
            reasons.append(item)
    return reasons


def _operation_id(operation: Mapping[str, Any]) -> str:
    explicit = str(operation.get("operation_id") or operation.get("trace_id") or "").strip()
    if explicit:
        return explicit
    seed = "|".join(
        [
            str(operation.get("operation_type") or "operation"),
            str(operation.get("job_id") or operation.get("source") or ""),
            ",".join(str(item) for item in operation.get("symbols") or []),
            ",".join(_intervals(operation)),
            str(operation.get("started_at_ms") or ""),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _load_state(pb: Any, state_key: str, environment: str) -> dict[str, Any]:
    getter = getattr(pb, "get_state", None)
    if not callable(getter):
        return {}
    try:
        record = getter(state_key, environment, "global")
    except TypeError:
        try:
            record = getter(state_key, environment)
        except Exception:
            return {}
    except Exception:
        return {}
    data = (record or {}).get("data") if isinstance(record, Mapping) else {}
    return dict(data or {}) if isinstance(data, Mapping) else {}


def _save_state(pb: Any, state_key: str, environment: str, state: Mapping[str, Any]) -> None:
    upsert = getattr(pb, "upsert_state", None)
    if not callable(upsert):
        return
    try:
        upsert(state_key, environment, dict(state or {}), date="global")
    except TypeError:
        try:
            upsert(state_key, environment, dict(state or {}))
        except Exception:
            return
    except Exception:
        return


def _notify_system_event(
    pb: Any,
    *,
    title: str,
    detail: dict[str, Any],
    level: str,
    environment: str,
    message_id: str = "",
) -> dict[str, Any]:
    notifier = getattr(pb, "notify_system_event", None)
    if callable(notifier):
        try:
            return notifier(
                title,
                detail,
                event_type="alert",
                level=level,
                source="ibkr_large_operation",
                environment=environment,
                message_id=message_id,
            ) or {}
        except TypeError:
            try:
                return notifier(title, detail, "alert", environment) or {}
            except Exception:
                return {}
        except Exception:
            return {}
    creator = getattr(pb, "create_record", None)
    if callable(creator):
        try:
            return creator(
                "system_events",
                {
                    "event_type": "alert",
                    "level": level,
                    "source": "ibkr_large_operation",
                    "environment": environment,
                    "title": title,
                    "detail": detail,
                    "notified": False,
                },
            ) or {}
        except Exception:
            return {}
    return {}


def _stage_label(stage: str) -> str:
    return {
        "start": "开始",
        "progress": "进行中",
        "completed": "完成",
        "failed": "失败",
        "deferred": "已延期",
    }.get(stage, stage or "状态")


def _canonical_stage(stage: str, operation: Mapping[str, Any]) -> str:
    normalized = str(stage or "").strip().lower()
    if normalized in {"finish", "finished", "complete", "done", "success"}:
        normalized = "completed"
    if normalized in {"error", "failure"}:
        normalized = "failed"
    if operation.get("error") and normalized in {"completed", "done", "finish", "finished"}:
        normalized = "failed"
    if normalized not in {"start", "progress", "completed", "failed", "deferred"}:
        normalized = "progress"
    return normalized


def emit_large_operation_alert(
    pb: Any,
    operation: Mapping[str, Any],
    *,
    config: Any = None,
    stage: str = "start",
    environment: str = "",
    broker_mode: str = "",
) -> dict[str, Any]:
    """Emit a deduplicated alert when an operation crosses large-operation thresholds."""

    if pb is None:
        return {"ok": False, "skipped": True, "reason": "pb_unavailable"}

    data_environment = str(
        operation.get("data_environment") or operation.get("market_data_mode") or environment or "live"
    ).strip().lower() or "live"
    alert_environment = str(
        broker_mode or operation.get("broker_mode") or configured_broker_mode() or "paper"
    ).strip().lower() or "paper"
    if not _config_bool(config, "ibkr_large_operation_alert_enabled", data_environment, True):
        return {"ok": False, "skipped": True, "reason": "large_operation_alert_disabled"}

    normalized_stage = _canonical_stage(stage, operation)
    operation_id = _operation_id(operation)
    state_key = f"{ALERT_STATE_PREFIX}{operation_id}"
    state = _load_state(pb, state_key, alert_environment)
    reasons = large_operation_reasons(operation, config=config, environment=data_environment)
    previously_large = bool(state.get("large_operation"))
    if not reasons and not previously_large:
        return {"ok": True, "skipped": True, "reason": "below_large_operation_threshold", "operation_id": operation_id}

    now_ms = int(time.time() * 1000)
    cooldown_s = _config_int(
        config,
        "ibkr_large_operation_progress_cooldown_s",
        data_environment,
        DEFAULT_THRESHOLDS["progress_cooldown_s"],
    )
    if normalized_stage == "start" and bool(state.get("started_notified")):
        return {"ok": True, "skipped": True, "reason": "start_already_notified", "operation_id": operation_id}
    if normalized_stage == "progress":
        last_progress_ms = _to_int(state.get("last_progress_ms"), 0)
        if last_progress_ms > 0 and now_ms - last_progress_ms < max(1, cooldown_s) * 1000:
            return {"ok": True, "skipped": True, "reason": "progress_cooldown", "operation_id": operation_id}
    if normalized_stage in {"completed", "failed", "deferred"} and bool(state.get("completed_notified")):
        return {"ok": True, "skipped": True, "reason": "terminal_already_notified", "operation_id": operation_id}

    symbols = operation.get("symbols") if isinstance(operation.get("symbols"), (list, tuple, set)) else []
    symbol_sample = [str(item).upper() for item in list(symbols)[:12]]
    detail = {
        **dict(operation),
        "operation_id": operation_id,
        "alert_stage": normalized_stage,
        "large_operation": True,
        "large_reasons": reasons or list(state.get("large_reasons") or []),
        "data_environment": data_environment,
        "broker_mode": alert_environment,
        "symbols_total": _symbols_total(operation),
        "symbol_sample": symbol_sample,
        "intervals": _intervals(operation),
        "alerted_at_ms": now_ms,
    }
    operation_type = str(operation.get("operation_type") or operation.get("source") or "operation").strip()
    job_id = str(operation.get("job_id") or operation.get("source") or operation_type).strip()
    title = f"[Broker {alert_environment.upper()}] 大规模操作{_stage_label(normalized_stage)}: {job_id}"
    level = "error" if normalized_stage == "failed" else "warning"
    message_id = str(state.get("message_id") or "") if normalized_stage == "progress" else ""
    notify_result = _notify_system_event(
        pb,
        title=title,
        detail=detail,
        level=level,
        environment=alert_environment,
        message_id=message_id,
    )
    next_state = {
        **state,
        "large_operation": True,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "job_id": job_id,
        "data_environment": data_environment,
        "broker_mode": alert_environment,
        "large_reasons": reasons or list(state.get("large_reasons") or []),
        "updated_at_ms": now_ms,
    }
    returned_message_id = str((notify_result or {}).get("message_id") or message_id or state.get("message_id") or "")
    if returned_message_id:
        next_state["message_id"] = returned_message_id
    if normalized_stage == "start":
        next_state["started_notified"] = True
        next_state["started_at_ms"] = now_ms
    elif normalized_stage == "progress":
        next_state["last_progress_ms"] = now_ms
    else:
        next_state["completed_notified"] = True
        next_state["completed_stage"] = normalized_stage
        next_state["completed_at_ms"] = now_ms
    _save_state(pb, state_key, alert_environment, next_state)
    return {
        "ok": True,
        "operation_id": operation_id,
        "stage": normalized_stage,
        "reasons": reasons or list(state.get("large_reasons") or []),
        "notified": bool(notify_result),
        "event": notify_result,
        "state_key": state_key,
    }


__all__ = [
    "ALERT_STATE_PREFIX",
    "DEFAULT_THRESHOLDS",
    "emit_large_operation_alert",
    "large_operation_reasons",
]
