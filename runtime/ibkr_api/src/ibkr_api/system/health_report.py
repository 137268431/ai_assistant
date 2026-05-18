from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode


NormalizeEnvironment = Callable[[Any, str], str]
LabelTitleWithEnvironment = Callable[[Any, str], str]
AddEnvironmentToDetail = Callable[[Any, str], dict[str, Any]]


def build_health_report_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    label_title_with_environment: LabelTitleWithEnvironment,
    add_environment_to_detail: AddEnvironmentToDetail,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    persisted = False
    error = ""
    record_payload = {
        "event_type": "heartbeat",
        "level": "info",
        "source": "ibkr_compute",
        "environment": environment,
        "title": label_title_with_environment("IBKR 健康上报", environment),
        "detail": add_environment_to_detail(payload, environment),
        "us_time": str(payload.get("et_time") or ""),
        "cn_time": str(payload.get("bj_time") or ""),
        "notified": False,
    }

    try:
        pb.create_record("system_events", record_payload)
        persisted = True
    except Exception as exc:
        error = str(exc)

    response = {
        "ok": True,
        "environment": environment,
        "persisted": persisted,
        "source": "ibkr-api",
    }
    if error:
        response["error"] = error
    return response, 200
