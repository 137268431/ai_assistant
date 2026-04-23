from __future__ import annotations

from typing import Any, Callable

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
    environment = normalize_environment(payload.get("environment"), "live")
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
    environment = normalize_environment(payload.get("environment"), "live")
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
    environment = normalize_environment(payload.get("environment"), "live")
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
    environment = normalize_environment(payload.get("environment"), "live")
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
        route_path="/api/custom/ibkr/2fa/panic-reset",
        runtime_path="/ibkr/panic-reset",
        runtime_body={
            "environment": environment,
            "restart_gateway": _parse_bool(payload.get("restart_gateway"), True),
            "restart_runtime": _parse_bool(payload.get("restart_runtime"), True),
            "trigger_login": _parse_bool(payload.get("trigger_login"), True),
            "reason": str(payload.get("reason") or "panic_reset_2fa"),
            "source": str(payload.get("source") or "runtime_page"),
        },
        response_message="已全量清空旧 2FA / Session 状态，并重新拉起新的验证周期。",
        timeout=60,
    )


__all__ = [
    "build_two_factor_panic_reset_response",
    "build_two_factor_probe_response",
    "build_two_factor_takeover_response",
]
