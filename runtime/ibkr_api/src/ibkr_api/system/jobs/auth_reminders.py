from __future__ import annotations

from datetime import datetime
from typing import Any

from ibkr_api.modes import request_broker_mode

from .auth_shared import (
    ET,
    FetchRuntimeStatus,
    GetStatePayload,
    NormalizeEnvironment,
    NormalizeTwoFactorStateWithRuntime,
    RequestTwoFactorApproval,
    _as_dict,
    _to_int,
    _to_text,
    load_auth_attention_summary,
)


def _needs_auth_attention(auth: dict[str, Any]) -> bool:
    return bool(auth.get("gateway_reachable")) and (
        not bool(auth.get("runtime_authenticated"))
        or _to_int(auth.get("gateway_status_code"), 0) == 401
        or not bool(auth.get("runtime_started"))
    )


def _build_weekly_deadline_text(state: dict[str, Any]) -> str:
    if _to_text(state.get("business_deadline_cn")):
        return f"{_to_text(state.get('business_deadline_cn'))} 北京时间 / {_to_text(state.get('business_deadline_at'))} 美东"
    return "美股周一盘前前"


def build_two_factor_hourly_check_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = request_broker_mode(request_payload)
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    now_ms = int(datetime.now(tz=ET).timestamp() * 1000)
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
        now_ms=now_ms,
    )
    if not _needs_auth_attention(auth):
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "auth_not_required",
            "source": "ibkr-api",
            "job_id": "ibkr_2fa_hourly_check",
        }, 200

    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    status = _to_text(state.get("status")).lower()
    reason = _to_text(state.get("reason")).lower()
    last_push_ms = _to_int(state.get("last_request_push_ms"), 0)
    reminder_eligible = status == "requested" and reason in {"manual_start", "startup", "weekly_reauth", "manual_gateway_restart"}
    if not reminder_eligible:
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "state_not_eligible",
            "source": "ibkr-api",
            "job_id": "ibkr_2fa_hourly_check",
        }, 200
    if last_push_ms > 0 and (now_ms - last_push_ms) < 55 * 60 * 1000:
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "recent_push_exists",
            "source": "ibkr-api",
            "job_id": "ibkr_2fa_hourly_check",
        }, 200

    result = request_two_factor_approval(
        environment=environment,
        reason=_to_text(state.get("reason")) or "scheduled_2fa_check",
        source="ibkr_scheduler",
        message=(
            "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
            if reason == "weekly_reauth" and bool(state.get("business_deadline_overdue"))
            else (
                "本周重登仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。"
                if reason == "weekly_reauth"
                else (
                    "网关重启后的新轮次仍停在待手动触发阶段，请只去当前启动卡片点击开始验证。"
                    if reason == "manual_gateway_restart"
                    else "启动验证仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证。"
                )
            )
        ),
        detail={
            "当前状态": status or "requested",
            "周验证截止": (
                f"{_to_text(state.get('business_deadline_cn'))} 北京时间 / {_to_text(state.get('business_deadline_at'))} 美东"
                if _to_text(state.get("business_deadline_cn"))
                else "n/a"
            ),
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
            "最近结果": _to_text(state.get("last_result")),
            "最近错误": _to_text(state.get("last_error")),
        },
        force_reset=False,
        force_new=False,
    )
    return {
        "ok": True,
        "environment": environment,
        "requested": True,
        "result": result,
        "source": "ibkr-api",
        "job_id": "ibkr_2fa_hourly_check",
    }, 200


def build_weekly_reauth_followup_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = request_broker_mode(request_payload)
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
    )
    if not _needs_auth_attention(auth):
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "auth_not_required",
            "source": "ibkr-api",
            "job_id": "ibkr_weekly_reauth_followup",
        }, 200

    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    if _to_text(state.get("status")).lower() != "requested" or _to_text(state.get("reason")).lower() != "weekly_reauth":
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "weekly_reauth_not_pending",
            "source": "ibkr-api",
            "job_id": "ibkr_weekly_reauth_followup",
        }, 200

    result = request_two_factor_approval(
        environment=environment,
        reason="weekly_reauth",
        source="ibkr_scheduler",
        message=(
            "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
            if bool(state.get("business_deadline_overdue"))
            else "美国周一已进入盘前准备窗口。本周重登仍待手动开始，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。"
        ),
        detail={
            "提醒类型": "weekly_reauth_followup",
            "最晚完成": _build_weekly_deadline_text(state),
            "点击后时限": "180 秒",
            "当前状态": _to_text(state.get("status")) or "requested",
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
            "最近结果": _to_text(state.get("last_result")),
            "最近错误": _to_text(state.get("last_error")),
        },
        force_reset=False,
        force_new=False,
    )
    return {
        "ok": True,
        "environment": environment,
        "requested": True,
        "result": result,
        "source": "ibkr-api",
        "job_id": "ibkr_weekly_reauth_followup",
    }, 200


def build_weekly_reauth_reminder_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = request_broker_mode(request_payload)
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
    )
    if auth.get("runtime_authenticated") and auth.get("gateway_reachable") and _to_int(auth.get("gateway_status_code"), 0) != 401:
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "already_authenticated",
            "source": "ibkr-api",
            "job_id": "ibkr_weekly_reauth_reminder",
        }, 200

    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    result = request_two_factor_approval(
        environment=environment,
        reason="weekly_reauth",
        source="ibkr_scheduler",
        message="美国周一已开始，本周重登提醒已发出。你有空时再去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。点击开始后需在 180 秒内完成当前 2FA。",
        detail={
            "提醒类型": "weekly_reauth",
            "最晚完成": _build_weekly_deadline_text(state),
            "点击后时限": "180 秒",
            "当前状态": _to_text(state.get("status")) or "requested",
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
        },
        force_reset=False,
        force_new=False,
    )
    return {
        "ok": True,
        "environment": environment,
        "requested": True,
        "result": result,
        "source": "ibkr-api",
        "job_id": "ibkr_weekly_reauth_reminder",
    }, 200


__all__ = [
    "build_two_factor_hourly_check_response",
    "build_weekly_reauth_followup_response",
    "build_weekly_reauth_reminder_response",
]
