from __future__ import annotations

from typing import Any, Callable

from ibkr_api.two_factor.startup_sync import sync_startup_auth_progress
from ibkr_api.two_factor.state import apply_runtime_state


NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
RequestJsonRequest = Callable[..., dict[str, Any]]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]
InspectRuntimeEnvironment = Callable[[str], dict[str, Any]]
BuildMismatchPayload = Callable[[dict[str, Any], str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
MergeStartupSteps = Callable[[Any, Any, bool], dict[str, dict[str, Any]]]
DeliverStartupProgressCard = Callable[[dict[str, Any], str], dict[str, Any]]


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def request_payload(result: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    payload = as_dict(result.get("payload"))
    if "ok" not in payload:
        payload["ok"] = bool(result.get("ok"))
    if result.get("target_url") and not payload.get("upstream"):
        payload["upstream"] = str(result.get("target_url") or "")
    if result.get("error") and not payload.get("error"):
        payload["error"] = str(result.get("error") or "")
    payload["status_code"] = int(result.get("status_code") or 0)
    return payload


def with_runtime_context(
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


def runtime_authenticated(state: dict[str, Any]) -> bool:
    return bool(state.get("runtime_authenticated")) and bool(state.get("gateway_reachable")) and int(state.get("gateway_status_code") or 0) != 401


def sync_startup(
    pb: Any,
    environment: str,
    status: str,
    state_data: dict[str, Any],
    *,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> None:
    try:
        sync_startup_auth_progress(
            pb,
            environment,
            status,
            state_data,
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
    except Exception:
        pass


__all__ = [
    "AsDict",
    "BuildMismatchPayload",
    "ConfigValue",
    "DeliverStartupProgressCard",
    "EmitSystemEvent",
    "FetchRuntimeStatus",
    "InspectRuntimeEnvironment",
    "MergeStartupSteps",
    "NormalizeEnvironment",
    "RequestJsonRequest",
    "SendInteractive",
    "UpdateInteractive",
    "parse_bool",
    "request_payload",
    "runtime_authenticated",
    "sync_startup",
    "with_runtime_context",
]
