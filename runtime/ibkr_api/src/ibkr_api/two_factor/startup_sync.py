from __future__ import annotations

from typing import Any, Callable

from ibkr_api.runtime.two_factor import normalize_two_factor_status
from ibkr_api.two_factor.state import time_strings


STARTUP_STATE_KEY = "ibkr_runtime_startup"
STARTUP_STATE_DATE = "global"

NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
MergeStartupSteps = Callable[[Any, Any, bool], dict[str, dict[str, Any]]]
DeliverStartupProgressCard = Callable[[dict[str, Any], str], dict[str, Any]]


def _startup_step_status(startup_state: dict[str, Any], key: str) -> str:
    steps = startup_state.get("steps") if isinstance(startup_state, dict) else {}
    step = steps.get(key) if isinstance(steps, dict) else {}
    return str((step or {}).get("status") or "").strip().lower()


def _manual_2fa_flow_started(startup_state: dict[str, Any]) -> bool:
    return (
        bool((startup_state or {}).get("trigger_login"))
        or _startup_step_status(startup_state, "manual_trigger") == "done"
        or _startup_step_status(startup_state, "manual_confirm") in {"waiting", "running", "done", "failed"}
    )


def _step_patch(status: str, state_data: dict[str, Any], *, manual_flow_started: bool = False) -> tuple[str, str, str, dict[str, dict[str, Any]]]:
    challenge_code = str(state_data.get("challenge_code") or "").strip()
    if status == "requested":
        return (
            "manual_trigger",
            "等待手动触发 2FA",
            "点击当前启动卡片下方“开始 2FA 验证”",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "done", "detail": "已复用或刷新当前 2FA 卡片。"},
                "manual_trigger": {"status": "waiting", "detail": "点击当前启动卡片下方“开始 2FA 验证”。"},
            },
        )
    if status == "triggered":
        return (
            "manual_confirm",
            "等待 Gateway 进入 2FA",
            "先确认 Gateway 已真正进入 Second Factor；手机 Push 尚未确认发出",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "done", "detail": "当前 2FA 卡片已准备完成。"},
                "manual_trigger": {"status": "done", "detail": "飞书按钮已点下，只代表控制请求已发送。"},
                "manual_confirm": {"status": "waiting", "detail": "等待 Gateway 进入手机 Push 或 Challenge/Response；未进入前不要盲等手机。"},
            },
        )
    if status == "waiting_confirm":
        return (
            "manual_confirm",
            "等待手机确认 2FA Push",
            "查看手机通知完成确认",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "done", "detail": "当前 2FA 卡片已准备完成。"},
                "manual_trigger": {"status": "done", "detail": "飞书触发步骤已完成。"},
                "manual_confirm": {"status": "waiting", "detail": "现在只需要点手机通知确认，不要重复触发。"},
            },
        )
    if status == "waiting_response":
        return (
            "manual_confirm",
            "等待提交 Response Code",
            "去 Runtime 页面提交当前 Challenge 对应的 Response Code",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "done", "detail": "当前 2FA 卡片已准备完成。"},
                "manual_trigger": {"status": "done", "detail": "飞书触发步骤已完成。"},
                "manual_confirm": {
                    "status": "waiting",
                    "detail": f"等待提交当前 Challenge 的 Response Code: {challenge_code}" if challenge_code else "等待提交当前轮次的 Response Code。",
                },
            },
        )
    if status == "success":
        if not manual_flow_started:
            return (
                "runtime_resume",
                "Session 已认证，未新开 2FA",
                "等待系统继续装载订阅、线程和预热",
                {
                    "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前恢复流程。"},
                    "card_ready": {"status": "skipped", "detail": "已复用现有认证会话，本轮无需准备新的 2FA 卡片。"},
                    "manual_trigger": {"status": "skipped", "detail": "Session 已认证，本轮无需在飞书手动触发 2FA。"},
                    "manual_confirm": {"status": "skipped", "detail": "Session 已认证，本轮无需完成新的 2FA 验证。"},
                    "runtime_resume": {"status": "running", "detail": "认证已恢复，正在继续恢复 Runtime。"},
                },
            )
        return (
            "runtime_resume",
            "2FA 已完成，等待 Runtime 继续启动",
            "等待系统继续装载订阅、线程和预热",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "done", "detail": "当前 2FA 卡片阶段已完成。"},
                "manual_trigger": {"status": "done", "detail": "飞书手动触发已完成。"},
                "manual_confirm": {"status": "done", "detail": "当前 2FA 验证已完成。"},
                "runtime_resume": {"status": "running", "detail": "认证已恢复，正在继续恢复 Runtime。"},
            },
        )
    if status in {"resume_pending", "recovering"}:
        return (
            "runtime_resume",
            "等待静默恢复 Gateway Session",
            "等待系统继续静默探测；当前不会自动触发新的 2FA",
            {
                "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
                "card_ready": {"status": "skipped", "detail": "当前不会自动新开 2FA 卡片。"},
                "manual_trigger": {"status": "skipped", "detail": "静默恢复期间不会在飞书手动触发 2FA。"},
                "manual_confirm": {"status": "skipped", "detail": "静默恢复期间无需完成新的 2FA 验证。"},
                "runtime_resume": {"status": "running", "detail": str(state_data.get("last_result") or state_data.get("message") or "系统正在静默恢复当前会话。")},
            },
        )
    return (
        "manual_trigger",
        "2FA 未完成，需手动重新触发",
        "回到当前启动卡片，重新点击下方“开始 2FA 验证”",
        {
            "service_boot": {"status": "done", "detail": "Gateway 已启动并进入当前验证流程。"},
            "card_ready": {"status": "done", "detail": "当前 2FA 卡片仍可复用。"},
            "manual_trigger": {"status": "failed", "detail": str(state_data.get("last_error") or state_data.get("last_result") or state_data.get("message") or "请重新手动触发当前轮次。")},
        },
    )


def sync_startup_auth_progress(
    pb: Any,
    environment: Any,
    status: Any,
    state_data: dict[str, Any] | None,
    *,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
    startup_state_key: str = STARTUP_STATE_KEY,
    startup_state_date: str = STARTUP_STATE_DATE,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    state = as_dict(state_data)
    try:
        current_record = pb.get_state(startup_state_key, runtime_environment, date=startup_state_date)
    except Exception:
        current_record = None
    current_data = as_dict((current_record or {}).get("data") if isinstance(current_record, dict) else {})
    if not bool(current_data.get("active")):
        return {"ok": True, "skipped": True, "reason": "no_active_startup_cycle"}

    normalized_status = normalize_two_factor_status(status)
    current_step, current_blocker, operator_action, patch_steps = _step_patch(
        normalized_status,
        state,
        manual_flow_started=_manual_2fa_flow_started(current_data),
    )
    next_state = {
        **current_data,
        "summary": str(state.get("message") or state.get("last_result") or current_data.get("summary") or ""),
        "current_step": current_step,
        "current_blocker": current_blocker,
        "operator_action": operator_action,
        "last_update_at": str(state.get("updated_at") or time_strings()["us"]),
        "steps": merge_startup_steps(current_data.get("steps"), patch_steps, bool(current_data.get("trigger_login"))),
    }
    try:
        pb.upsert_state(startup_state_key, runtime_environment, next_state, date=startup_state_date)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        delivery = deliver_startup_progress_card(next_state, runtime_environment)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "delivery": delivery, "state": next_state}


__all__ = ["sync_startup_auth_progress"]
