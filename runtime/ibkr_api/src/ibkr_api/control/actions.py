from __future__ import annotations

from typing import Any, Callable

from ibkr_api.control.config_records import upsert_config_value


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
RequestJsonRequest = Callable[..., dict[str, Any]]
InspectRuntimeEnvironment = Callable[[str], dict[str, Any]]
BuildMismatchPayload = Callable[[dict[str, Any], str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]


_EMERGENCY_CONFIG_ACTIONS = {
    "compute": [
        ("ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"),
    ],
    "trading": [
        ("ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"),
    ],
    "scheduler": [
        ("pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"),
    ],
    "publish": [
        ("ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"),
    ],
    "all": [
        ("ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"),
        ("ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"),
        ("pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"),
        ("ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"),
    ],
    "runtime": [],
}

_RECOVER_CONFIG_ACTIONS = {
    "compute": [
        ("ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"),
    ],
    "trading": [
        ("ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"),
    ],
    "scheduler": [
        ("pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"),
    ],
    "publish": [
        ("ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"),
    ],
    "all": [
        ("ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"),
        ("ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"),
        ("pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"),
        ("ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"),
    ],
}


def _normalize_action(value: Any, default: str = "all") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _upstream_payload(result: dict[str, Any], *, as_dict: AsDict, default_ok: bool = False) -> dict[str, Any]:
    payload = as_dict(result.get("payload"))
    normalized = dict(payload)
    if "ok" not in normalized:
        normalized["ok"] = bool(result.get("ok", default_ok))
    normalized["status_code"] = int(result.get("status_code") or 0)
    if result.get("error") and not normalized.get("error"):
        normalized["error"] = str(result.get("error") or "")
    if result.get("target_url") and not normalized.get("upstream"):
        normalized["upstream"] = str(result.get("target_url") or "")
    return normalized


def _apply_config_action(
    pb: Any,
    action_rows: list[tuple[str, str, str, str]],
    environment: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for key, value, display_name, description in action_rows:
        record = upsert_config_value(
            pb,
            key,
            value,
            environment,
            escape_filter_string=escape_filter_string,
            display_name=display_name,
            description=description,
            group_name="PB / IBKR 服务",
        )
        updated.append(
            {
                "key": str(key),
                "value": str(value),
                "id": str(record.get("id") or "") if isinstance(record, dict) else "",
            }
        )
    return updated


def build_emergency_stop_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
    emit_system_event: EmitSystemEvent | None,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    action = _normalize_action(payload.get("action"), "all")
    selected = _EMERGENCY_CONFIG_ACTIONS.get(action)
    if selected is None:
        return {"ok": False, "environment": environment, "error": "Unsupported emergency action", "action": action, "source": "ibkr-api"}, 400

    if action in {"runtime", "compute", "all"}:
        environment_info = inspect_runtime_environment(environment)
        if bool(environment_info.get("runtime_environment_mismatch")):
            return build_runtime_environment_mismatch_payload(environment_info, "/api/custom/ibkr/emergency-stop"), 409

    updated = _apply_config_action(
        pb,
        selected,
        environment,
        escape_filter_string=escape_filter_string,
    )

    runtime_stop = {"ok": True, "skipped": True, "status_code": 200}
    if action in {"runtime", "compute", "all"}:
        runtime_stop = _upstream_payload(
            request_json_request(
                "POST",
                runtime_base_url,
                "/ibkr/stop",
                json_body={"environment": environment},
                timeout=20,
            ),
            as_dict=as_dict,
            default_ok=True,
        )

    if callable(emit_system_event):
        try:
            emit_system_event(
                event_type="status_change",
                level="warning",
                source="manual",
                title="触发紧急停止",
                detail={
                    "action": action,
                    "updated_keys": ",".join(item["key"] for item in updated),
                    "runtime_stop": "requested" if runtime_stop.get("ok") is not False else "failed",
                },
                environment=environment,
            )
        except Exception:
            pass

    return {
        "ok": runtime_stop.get("ok") is not False,
        "environment": environment,
        "action": action,
        "updated": updated,
        "runtime_stop": runtime_stop,
        "source": "ibkr-api",
    }, 200


def build_recover_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    emit_system_event: EmitSystemEvent | None,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    action = _normalize_action(payload.get("action"), "all")
    selected = _RECOVER_CONFIG_ACTIONS.get(action)
    if selected is None:
        return {"ok": False, "environment": environment, "error": "Unsupported recover action", "action": action, "source": "ibkr-api"}, 400

    updated = _apply_config_action(
        pb,
        selected,
        environment,
        escape_filter_string=escape_filter_string,
    )

    if callable(emit_system_event):
        try:
            emit_system_event(
                event_type="status_change",
                level="info",
                source="manual",
                title="恢复运行开关",
                detail={
                    "action": action,
                    "updated_keys": ",".join(item["key"] for item in updated),
                },
                environment=environment,
            )
        except Exception:
            pass

    return {
        "ok": True,
        "environment": environment,
        "action": action,
        "updated": updated,
        "source": "ibkr-api",
    }, 200


def build_reauth_response(
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    environment_info = inspect_runtime_environment(environment)
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, "/api/custom/ibkr/reauth"), 409

    stop_result = request_json_request(
        "POST",
        runtime_base_url,
        "/ibkr/stop",
        json_body={"environment": environment},
        timeout=30,
    )
    start_result = request_json_request(
        "POST",
        runtime_base_url,
        "/ibkr/start",
        json_body={"environment": environment},
        timeout=30,
    )

    start_payload = _upstream_payload(start_result, as_dict=as_dict, default_ok=False)
    response = {
        **start_payload,
        "environment": environment,
        "source": "ibkr-api",
        "reauth": True,
        "stop_before_start": _upstream_payload(stop_result, as_dict=as_dict, default_ok=True),
    }
    status_code = int(start_result.get("status_code") or (200 if start_payload.get("ok") else 502) or 200)
    return response, status_code


__all__ = [
    "build_emergency_stop_response",
    "build_reauth_response",
    "build_recover_response",
]
