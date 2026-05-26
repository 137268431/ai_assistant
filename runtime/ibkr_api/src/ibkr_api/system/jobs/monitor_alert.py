from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_market_data_mode


MONITOR_ALERT_STATE_KEY = "system_monitor_alert"
MONITOR_ALERT_COOLDOWN_MS = 15 * 60 * 1000
IB_CLIENT_SERVICE_LABELS = (
    ("ibkr-runtime", "Runtime"),
    ("ibkr-compute", "Compute"),
    ("ibkr-api", "API"),
    ("ibkr-scheduler", "Scheduler"),
    ("ibkr-backtest", "Backtest"),
)

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
BuildAdmissionPreview = Callable[[dict[str, Any]], Any]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _service_counts_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    counts = _as_dict(service_monitor.get("status_counts"))
    parts = []
    if services:
        parts.append(f"total {len(services)}")
    parts.extend(
        f"{_to_text(status).lower()}:{_to_int(count, 0)}"
        for status, count in sorted(counts.items())
        if _to_int(count, 0) > 0
    )
    return " | ".join(parts) or "n/a"


def _backtest_service_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    service = _as_dict(services.get("ibkr-backtest"))
    if not service:
        return "n/a"
    status = _to_text(service.get("status")) or "unknown"
    worker = _to_text(service.get("worker_status") or service.get("readiness_phase"))
    client_id = _to_int(service.get("ib_gateway_client_id"), 0)
    parts = [f"{status}"]
    if worker and worker.lower() != status.lower():
        parts.append(f"worker {worker}")
    if client_id:
        parts.append(f"client {client_id}")
    return " | ".join(parts)


def _service_client_id(service: dict[str, Any]) -> int:
    return _to_int(
        service.get("ib_gateway_client_id")
        or service.get("broker_client_id")
        or service.get("client_id"),
        0,
    )


def _ib_client_ids_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    parts: list[str] = []
    seen: set[str] = set()
    for service_key, label in IB_CLIENT_SERVICE_LABELS:
        client_id = _service_client_id(_as_dict(services.get(service_key)))
        if not client_id:
            continue
        marker = f"{label}:{client_id}"
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(f"{label} client {client_id}")
    return " | ".join(parts)


def _alert_flags(monitor_payload: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for item in monitor_payload.get("flags") or []:
        if not isinstance(item, dict):
            continue
        code = _to_text(item.get("code"))
        severity = _to_text(item.get("severity")).lower()
        if not code or severity not in {"warning", "error"}:
            continue
        flags.append(dict(item))
    return flags


def _fingerprint(monitor_payload: dict[str, Any], flags: list[dict[str, Any]]) -> str:
    return str(
        {
            "status": _to_text(monitor_payload.get("status")).lower(),
            "flag_codes": sorted(_to_text(item.get("code")) for item in flags if _to_text(item.get("code"))),
        }
    )


def _admission_preview_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and result:
        payload = result[0]
    else:
        payload = result
    return dict(payload) if isinstance(payload, dict) else {}


def _admission_preview_detail(admission_preview: dict[str, Any]) -> dict[str, str]:
    if not admission_preview:
        return {}
    if admission_preview.get("error") and not admission_preview.get("ok", False):
        return {"入池预览": f"unavailable:{_to_text(admission_preview.get('error'))}"}
    admitted_items = [
        dict(item)
        for item in (admission_preview.get("admitted_items") or [])
        if isinstance(item, dict) and _to_text(item.get("symbol"))
    ]
    summary = (
        f"would_admit {_to_int(admission_preview.get('would_admit'), len(admitted_items))} | "
        f"eligible {_to_int(admission_preview.get('eligible'), 0)} | "
        f"scanned {_to_int(admission_preview.get('scanned'), 0)}"
    )
    detail = {"入池预览": summary}
    if admitted_items:
        parts = []
        for item in admitted_items[:8]:
            symbol = _to_text(item.get("symbol")).upper()
            direction = _to_text(item.get("direction_bias"))
            score = item.get("score")
            window = _to_text(item.get("window_status"))
            bars = _to_int(item.get("bars_remaining"), 0)
            score_text = f"score {score}" if score not in (None, "") else "score n/a"
            bars_text = f", {bars} bars" if bars > 0 else ""
            parts.append(f"{symbol}({direction or 'n/a'}, {score_text}, {window or 'window'}{bars_text})")
        detail["可能加入"] = " | ".join(parts)
    else:
        rejection_summary = admission_preview.get("rejection_summary")
        if isinstance(rejection_summary, dict) and rejection_summary:
            parts = [
                f"{_to_text(reason)} {_to_int(count, 0)}"
                for reason, count in list(rejection_summary.items())[:5]
            ]
            detail["未入池原因"] = " | ".join(parts)
    return detail


def _detail(
    monitor_payload: dict[str, Any],
    flags: list[dict[str, Any]],
    *,
    timestamp_us: str,
    admission_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service_monitor = _as_dict(monitor_payload.get("service_monitor"))
    counts = _as_dict(service_monitor.get("status_counts"))
    runtime = _as_dict(monitor_payload.get("runtime"))
    scheduler = _as_dict(monitor_payload.get("scheduler"))
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    pb_disk = _as_dict(_as_dict(monitor_payload.get("pocketbase")).get("disk"))
    filesystem = _as_dict(pb_disk.get("filesystem"))
    detail = {
        "检查时间": timestamp_us,
        "监控状态": _to_text(monitor_payload.get("status")).upper() or "UNKNOWN",
        "触发项": " | ".join(
            f"{_to_text(item.get('title') or item.get('code'))}: {_to_text(item.get('detail'))}"
            for item in flags[:4]
        ) or "none",
        "服务统计": _service_counts_line(service_monitor),
        "Backtest": _backtest_service_line(service_monitor),
        "IB ClientID": _ib_client_ids_line(service_monitor) or "n/a",
        "Session": "authenticated" if session.get("authenticated") else "pending",
        "WebSocket": "connected" if websocket.get("connected") or websocket.get("ready") else "offline",
        "DispatchLag": (
            f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m"
            if scheduler.get("latest_ingested_bar_time_ms")
            else "awaiting bars"
        ),
        "PB磁盘": (
            f"{filesystem.get('used_pct')}%"
            if filesystem.get("used_pct") is not None
            else "unknown"
        ),
    }
    detail.update(_admission_preview_detail(admission_preview or {}))
    return detail


def _should_load_admission_preview(flags: list[dict[str, Any]]) -> bool:
    preview_codes = {"no_active_targets", "no_execution_eligible_targets"}
    return any(_to_text(item.get("code")) in preview_codes for item in flags)


def _load_admission_preview(
    builder: BuildAdmissionPreview | None,
    *,
    environment: str,
    market_date: str,
) -> dict[str, Any]:
    if not callable(builder):
        return {}
    try:
        result = builder(
            {
                "environment": environment,
                "market_data_mode": environment,
                "data_environment": environment,
                "market_date": market_date,
                "dry_run": True,
                "force": True,
                "max_admit": 8,
                "max_scan_symbols": 200,
                "trigger_source": "monitor_alert_preview",
            }
        )
        return _admission_preview_payload(result)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_system_monitor_alert_guard_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    emit_system_event: EmitSystemEvent,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    build_admission_preview: BuildAdmissionPreview | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = request_market_data_mode(request_payload)
    times = time_strings()
    monitor_payload = _as_dict(build_system_monitor_payload(environment))
    flags = _alert_flags(monitor_payload)
    state = _as_dict(get_state_payload(MONITOR_ALERT_STATE_KEY, environment).get("data"))
    current_ms = _to_int(datetime.now(timezone.utc).timestamp() * 1000, 0)
    next_state = {
        **state,
        "last_monitor_check_at": times["us"],
    }

    if not flags:
        next_state.update(
            {
                "last_monitor_issue_at": "",
                "last_monitor_alert_hash": "",
                "last_monitor_alert_ms": 0,
            }
        )
        upsert_state(MONITOR_ALERT_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_monitor_alert_guard",
            "triggered": False,
            "state": next_state,
            "source": "ibkr-api",
        }, 200

    fingerprint = _fingerprint(monitor_payload, flags)
    last_hash = _to_text(state.get("last_monitor_alert_hash"))
    last_ms = _to_int(state.get("last_monitor_alert_ms"), 0)
    should_notify = fingerprint != last_hash or last_ms <= 0 or (current_ms - last_ms) >= MONITOR_ALERT_COOLDOWN_MS
    level = "error" if any(_to_text(item.get("severity")).lower() == "error" for item in flags) else "warning"
    title = f"IBKR Monitor {'严重告警' if level == 'error' else '告警'}（{len(flags)}项）"
    event: dict[str, Any] = {}
    admission_preview: dict[str, Any] = {}
    if should_notify:
        if _should_load_admission_preview(flags):
            admission_preview = _load_admission_preview(
                build_admission_preview,
                environment=environment,
                market_date=_to_text((times or {}).get("date")),
            )
        event = emit_system_event(
            event_type="alert",
            level=level,
            source="ibkr-api",
            title=title,
            detail=_detail(
                monitor_payload,
                flags,
                timestamp_us=times["us"],
                admission_preview=admission_preview,
            ),
            environment=environment,
        )
        next_state.update(
            {
                "last_monitor_issue_at": times["us"],
                "last_monitor_alert_hash": fingerprint,
                "last_monitor_alert_ms": current_ms,
            }
        )
    upsert_state(MONITOR_ALERT_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_monitor_alert_guard",
        "triggered": bool(should_notify),
        "flag_codes": [_to_text(item.get("code")) for item in flags],
        "admission_preview": admission_preview,
        "event": event,
        "state": next_state,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_system_monitor_alert_guard_response"]
