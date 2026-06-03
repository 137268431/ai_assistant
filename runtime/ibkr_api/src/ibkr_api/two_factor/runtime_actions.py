from __future__ import annotations

import subprocess
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode

from ibkr_api.two_factor.state import apply_runtime_state, load_two_factor_state


NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
RequestJsonRequest = Callable[..., dict[str, Any]]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]
InspectRuntimeEnvironment = Callable[[str], dict[str, Any]]
BuildMismatchPayload = Callable[[dict[str, Any], str], dict[str, Any]]


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _request_payload(result: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    payload = as_dict(result.get("payload"))
    if "ok" not in payload:
        payload["ok"] = bool(result.get("ok"))
    if result.get("target_url") and not payload.get("upstream"):
        payload["upstream"] = str(result.get("target_url") or "")
    if result.get("error") and not payload.get("error"):
        payload["error"] = str(result.get("error") or "")
    payload["status_code"] = int(result.get("status_code") or 0)
    return payload


def _with_runtime_context(
    state_data: dict[str, Any],
    *,
    runtime_payload: dict[str, Any],
    runtime_status_error: str,
    environment: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    state = apply_runtime_state(state_data, runtime_payload, as_dict=as_dict)
    actual_runtime_environment = str((runtime_payload or {}).get("environment") or environment).strip().lower() or environment
    state["requested_environment"] = environment
    state["actual_runtime_environment"] = actual_runtime_environment
    state["runtime_environment_mismatch"] = actual_runtime_environment != environment
    if runtime_status_error:
        state["runtime_status_error"] = runtime_status_error
    return state


def _build_runtime_action_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
    route_path: str,
    runtime_path: str,
    runtime_body: dict[str, Any],
    response_message: str,
    extra_fields: dict[str, Any] | None = None,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    environment_info = inspect_runtime_environment(environment)
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, route_path), 409

    action_result = request_json_request(
        "POST",
        runtime_base_url,
        runtime_path,
        json_body=runtime_body,
        timeout=timeout,
    )
    action_payload = _request_payload(action_result, as_dict=as_dict)

    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = as_dict(runtime_result.get("payload"))
    runtime_status_error = str(runtime_result.get("error") or "")
    state = _with_runtime_context(
        current.get("data") or {},
        runtime_payload=runtime_payload,
        runtime_status_error=runtime_status_error,
        environment=environment,
        as_dict=as_dict,
    )

    status_code = int(action_payload.get("status_code") or 0)
    if status_code <= 0:
        status_code = 200 if bool(action_payload.get("ok")) else 502
    response_payload = {
        "ok": bool(action_payload.get("ok")),
        "environment": environment,
        "state": state,
        "payload": action_payload,
        "message": response_message,
        "source": "ibkr-api",
    }
    if isinstance(extra_fields, dict):
        response_payload.update(extra_fields)
    return response_payload, status_code


def _safe_inspect_runtime_environment(
    environment: str,
    *,
    inspect_runtime_environment: InspectRuntimeEnvironment,
) -> tuple[dict[str, Any], str]:
    try:
        environment_info = inspect_runtime_environment(environment)
    except Exception as exc:
        return {
            "requested_environment": environment,
            "actual_runtime_environment": environment,
            "runtime_environment_mismatch": False,
            "runtime_status_error": str(exc),
        }, str(exc)
    if not isinstance(environment_info, dict):
        return {
            "requested_environment": environment,
            "actual_runtime_environment": environment,
            "runtime_environment_mismatch": False,
            "runtime_status_error": "runtime_environment_inspection_unavailable",
        }, "runtime_environment_inspection_unavailable"
    return environment_info, str(environment_info.get("runtime_status_error") or "")


def _safe_runtime_action_request(
    *,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    runtime_path: str,
    runtime_body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    try:
        result = request_json_request(
            "POST",
            runtime_base_url,
            runtime_path,
            json_body=runtime_body,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "target_url": f"{runtime_base_url.rstrip('/')}/{runtime_path.lstrip('/')}",
            "error": str(exc),
        }
    return result if isinstance(result, dict) else {"ok": False, "status_code": 0, "payload": {}, "error": "runtime_action_unavailable"}


_PANIC_RESET_CONFIRM_TEXT = "重开2FA"


def _panic_reset_fallback_operations() -> list[dict[str, str]]:
    return [
        {"service": "ibkr-runtime", "action": "stop"},
        {"service": "ibkr-gateway", "action": "restart"},
        {"service": "ibkr-runtime", "action": "start"},
    ]


def _run_panic_reset_systemctl_fallback() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    timeouts = {
        ("ibkr-runtime", "stop"): 30.0,
        ("ibkr-gateway", "restart"): 90.0,
        ("ibkr-runtime", "start"): 45.0,
    }
    for operation in _panic_reset_fallback_operations():
        service = operation["service"]
        action = operation["action"]
        try:
            proc = subprocess.run(
                ["systemctl", action, service],
                capture_output=True,
                text=True,
                timeout=timeouts.get((service, action), 45.0),
            )
            results.append(
                {
                    **operation,
                    "ok": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "stdout": (proc.stdout or "").strip(),
                    "stderr": (proc.stderr or "").strip(),
                }
            )
        except Exception as exc:
            results.append(
                {
                    **operation,
                    "ok": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(exc),
                }
            )
    return results


def _panic_reset_fallback_plan(
    environment: str,
    *,
    reason: str,
    source: str,
    dry_run: bool,
    operation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "accepted": True,
        "fallback": True,
        "route_context": "panic-reset",
        "dry_run": bool(dry_run),
        "reason": reason,
        "source": source,
        "operations": operation_results if operation_results is not None else _panic_reset_fallback_operations(),
        "requires_confirm_text": "" if not dry_run else _PANIC_RESET_CONFIRM_TEXT,
        "environment": environment,
    }


def _build_panic_reset_fallback_response(
    pb: Any,
    *,
    environment: str,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    action_payload: dict[str, Any],
    inspect_error: str,
    reason: str,
    source: str,
    execute_fallback: bool,
) -> tuple[dict[str, Any], int]:
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    state = dict(current.get("data") or {})
    runtime_status_error = str(action_payload.get("error") or inspect_error or "runtime_unavailable")
    state.update(
        {
            "requested_environment": environment,
            "actual_runtime_environment": environment,
            "runtime_environment_mismatch": False,
            "runtime_status_error": runtime_status_error,
            "panic_reset_fallback": True,
            "operator_action": "panic_reset",
            "reset_recommended": True,
            "reset_reason": "runtime_unavailable",
        }
    )
    operation_results = _run_panic_reset_systemctl_fallback() if execute_fallback else None
    fallback = _panic_reset_fallback_plan(
        environment,
        reason=reason,
        source=source,
        dry_run=not execute_fallback,
        operation_results=operation_results,
    )
    fallback_ok = all(bool(item.get("ok")) for item in (operation_results or [])) if execute_fallback else True
    if execute_fallback:
        state["panic_reset_fallback_executed"] = True
        state["panic_reset_fallback_ok"] = fallback_ok
    status_code = 202 if fallback_ok else 502
    return {
        "ok": fallback_ok,
        "accepted": True,
        "fallback": True,
        "fallback_executed": bool(execute_fallback),
        "environment": environment,
        "state": state,
        "payload": {
            "ok": fallback_ok,
            "accepted": True,
            "fallback": True,
            "fallback_executed": bool(execute_fallback),
            "status_code": status_code,
            "runtime_action": action_payload,
            "recovery_plan": fallback,
        },
        "recovery_plan": fallback,
        "message": (
            "Runtime panic-reset 请求不可用；API 已执行受保护的 Gateway/Runtime 恢复链。"
            if execute_fallback
            else "Runtime panic-reset 请求不可用；API 已接受受保护的恢复计划，请补充确认字段或在运维侧执行。"
        ),
        "source": "ibkr-api",
    }, status_code


def build_two_factor_takeover_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    enabled = _parse_bool(payload.get("enabled"), True)
    return _build_runtime_action_response(
        pb,
        payload=payload,
        normalize_environment=normalize_environment,
        as_dict=as_dict,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
        fetch_runtime_status=fetch_runtime_status,
        inspect_runtime_environment=inspect_runtime_environment,
        build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
        route_path="/api/custom/ibkr/2fa/takeover",
        runtime_path="/ibkr/2fa/takeover",
        runtime_body={
            "broker_mode": environment,
            "environment": environment,
            "enabled": enabled,
            "ttl_sec": int(payload.get("ttl_sec") or 0) or 600,
            "reason": str(payload.get("reason") or "manual_takeover"),
            "source": str(payload.get("source") or "runtime_page"),
        },
        response_message=(
            "已开启人工接管；系统会继续静默探测，但不会把当前轮次误判为已锁死。"
            if enabled
            else "已结束人工接管，并立即恢复静默探测。"
        ),
        extra_fields={"enabled": enabled},
        timeout=20,
    )


def build_two_factor_probe_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    return _build_runtime_action_response(
        pb,
        payload=payload,
        normalize_environment=normalize_environment,
        as_dict=as_dict,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
        fetch_runtime_status=fetch_runtime_status,
        inspect_runtime_environment=inspect_runtime_environment,
        build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
        route_path="/api/custom/ibkr/2fa/probe",
        runtime_path="/ibkr/2fa/probe",
        runtime_body={
            "broker_mode": environment,
            "environment": environment,
            "reason": str(payload.get("reason") or "manual_probe"),
            "source": str(payload.get("source") or "runtime_page"),
        },
        response_message="已触发静默探测；若当前会话其实已在真实账户侧恢复，系统会自动转为 success。",
        timeout=20,
    )


def build_two_factor_panic_reset_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    route_path = "/api/custom/ibkr/2fa/panic-reset"
    reason = str(payload.get("reason") or "panic_reset_2fa")
    source = str(payload.get("source") or "runtime_page")
    runtime_body = {
        "broker_mode": environment,
        "environment": environment,
        "restart_gateway": _parse_bool(payload.get("restart_gateway"), True),
        "restart_runtime": _parse_bool(payload.get("restart_runtime"), True),
        "trigger_login": _parse_bool(payload.get("trigger_login"), True),
        "reason": reason,
        "source": source,
    }

    environment_info, inspect_error = _safe_inspect_runtime_environment(
        environment,
        inspect_runtime_environment=inspect_runtime_environment,
    )
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, route_path), 409

    action_result = _safe_runtime_action_request(
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
        runtime_path="/ibkr/panic-reset",
        runtime_body=runtime_body,
        timeout=60,
    )
    action_payload = _request_payload(action_result, as_dict=as_dict)
    action_ok = bool(action_payload.get("ok"))
    status_code = int(action_payload.get("status_code") or 0)
    if (not action_ok) or status_code <= 0:
        if inspect_error and not action_payload.get("runtime_status_error"):
            action_payload["runtime_status_error"] = inspect_error
        execute_fallback = str(payload.get("confirm_text") or payload.get("confirmation") or "").strip() == _PANIC_RESET_CONFIRM_TEXT
        return _build_panic_reset_fallback_response(
            pb,
            environment=environment,
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            action_payload=action_payload,
            inspect_error=inspect_error,
            reason=reason,
            source=source,
            execute_fallback=execute_fallback,
        )

    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    try:
        runtime_result = fetch_runtime_status(environment)
    except Exception as exc:
        runtime_result = {"payload": {}, "error": str(exc), "ok": False, "status_code": 0}
    runtime_payload = as_dict(runtime_result.get("payload") if isinstance(runtime_result, dict) else {})
    runtime_status_error = str((runtime_result.get("error") if isinstance(runtime_result, dict) else "") or inspect_error or "")
    state = _with_runtime_context(
        current.get("data") or {},
        runtime_payload=runtime_payload,
        runtime_status_error=runtime_status_error,
        environment=environment,
        as_dict=as_dict,
    )
    if runtime_status_error:
        state["panic_reset_status_fallback"] = True

    return {
        "ok": True,
        "environment": environment,
        "state": state,
        "payload": action_payload,
        "message": "已全量清空旧 2FA / Session 状态，并重新拉起新的验证周期。",
        "source": "ibkr-api",
    }, status_code or 200


__all__ = [
    "build_two_factor_panic_reset_response",
    "build_two_factor_probe_response",
    "build_two_factor_takeover_response",
]
