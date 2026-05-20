from __future__ import annotations

from datetime import datetime, timezone

from ibkr_compute.api.monitor.runtime.actions import _build_gateway_action_payload
from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_panic_reset_auth,
    _ibkr_service_environment,
    get_ibkr_service,
    set_ibkr_runtime_control,
)
from ibkr_compute.api.support.market_data_session import (
    MARKET_DATA_SESSION_CONFLICT_CODE,
    detect_market_data_session_conflict,
)


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _runtime_status_for_restart_guard(service) -> dict:
    if not hasattr(service, "status"):
        return {}
    try:
        payload = service.status()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _market_data_session_conflict_message() -> str:
    return (
        "检测到 IBKR 行情/历史数据会话被另一个 IP 占用。"
        "请先退出其他电脑/服务器上的 TWS、IB Gateway 或 IBKR Desktop，"
        "再重新触发 Gateway 重启；如确认要强制抢回会话，请使用 force_restart=true。"
    )


def _build_ibkr_gateway_start_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    reason = str(payload.get("reason") or "manual_gateway_start").strip() or "manual_gateway_start"
    source = str(payload.get("source") or "api_gateway_start").strip() or "api_gateway_start"

    ok = bool(service.gateway_manager.start())
    try:
        service.session_keeper.check_auth_status()
    except Exception:
        pass

    return (
        _build_gateway_action_payload(
            service,
            "start",
            ok=ok,
            message="Gateway 已启动。" if ok else "Gateway 启动失败。",
            reason=reason,
            source=source,
            extra={
                "gateway_started": ok,
                "startup_cycle_planned": False,
            },
        ),
        200,
    )


def _build_ibkr_gateway_stop_response(payload: dict | None = None) -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    reason = str(payload.get("reason") or "manual_gateway_stop").strip() or "manual_gateway_stop"
    source = str(payload.get("source") or "api_gateway_stop").strip() or "api_gateway_stop"
    runtime_environment = _ibkr_service_environment(service)
    startup_state = service.startup_progress_snapshot() if hasattr(service, "startup_progress_snapshot") else {}
    runtime_active = bool(getattr(service, "is_running", False) or getattr(service, "is_starting", False))
    startup_active = bool(startup_state.get("active"))

    if runtime_active:
        service.stop()
    gateway_stopped = bool(service.gateway_manager.stop())
    api_app._ibkr_restore_attempted = False

    if runtime_active or startup_active:
        set_ibkr_runtime_control(
            runtime_environment,
            False,
            source=source,
            reason=reason,
            extra={
                "last_restore_trigger_login": False,
            },
        )

    if startup_active:
        event_detail = {
            "状态结论": "Gateway 已被手动停止，当前启动轮次已终止。",
            "检查时间": datetime.now(timezone.utc).isoformat(),
            "执行动作": "manual_gateway_stop",
        }
        service._sync_startup_progress(
            action="abort",
            title="IBKR Runtime 启动已中断",
            summary="Gateway 已手动停止，当前启动轮次终止。",
            current_step="gateway",
            current_blocker="Gateway 已手动停止",
            operator_action="需要时重新启动 Runtime，系统会创建新的启动卡片。",
            steps={
                "gateway": {
                    "status": "failed",
                    "detail": "Gateway 已手动停止，当前启动轮次终止。",
                },
            },
            fields=service._build_startup_progress_fields(
                str(startup_state.get("reason") or reason or "manual_gateway_stop"),
                source,
                False,
                event_detail,
            ),
            reason=str(startup_state.get("reason") or reason or "manual_gateway_stop"),
            source=source,
            trigger_login=False,
            record_event=True,
            event_type="status_change",
            event_title="IBKR Runtime 启动已中断",
            event_detail=event_detail,
            level="warning",
            allow_when_disabled=True,
        )

    return (
        _build_gateway_action_payload(
            service,
            "stop",
            ok=gateway_stopped,
            message="Gateway 已停止。" if gateway_stopped else "Gateway 停止失败。",
            reason=reason,
            source=source,
            extra={
                "gateway_stopped": gateway_stopped,
                "runtime_stopped": runtime_active,
                "startup_cycle_aborted": startup_active,
            },
        ),
        200,
    )


def _build_ibkr_gateway_restart_response(payload: dict | None = None) -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    reason = str(payload.get("reason") or "manual_gateway_restart").strip() or "manual_gateway_restart"
    source = str(payload.get("source") or "api_gateway_restart").strip() or "api_gateway_restart"
    force_restart = _coerce_bool(payload.get("force_restart"), False)
    runtime_environment = _ibkr_service_environment(service)
    startup_state = service.startup_progress_snapshot() if hasattr(service, "startup_progress_snapshot") else {}
    requires_fresh_cycle = bool(
        getattr(service, "is_running", False)
        or getattr(service, "is_starting", False)
        or startup_state.get("active")
    )
    conflict = detect_market_data_session_conflict(_runtime_status_for_restart_guard(service))
    if bool(conflict.get("active")) and not force_restart:
        return (
            _build_gateway_action_payload(
                service,
                "restart",
                ok=False,
                message=_market_data_session_conflict_message(),
                reason=reason,
                source=source,
                extra={
                    "restart_blocked": True,
                    "blocker_code": MARKET_DATA_SESSION_CONFLICT_CODE,
                    "blocker": conflict,
                    "operation": "gateway_restart_blocked",
                    "gateway_restarted": False,
                    "runtime_restart_requested": False,
                    "startup_cycle_planned": False,
                    "forced": False,
                },
            ),
            409,
        )

    api_app._ibkr_restore_attempted = False

    if requires_fresh_cycle:
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source=source,
            reason=reason,
            extra={
                "last_restore_trigger_login": False,
            },
        )
        _background_panic_reset_auth(
            service,
            restart_gateway=True,
            restart_runtime=True,
            trigger_login=False,
            reason=reason,
            source=source,
        )
        return (
            _build_gateway_action_payload(
                service,
                "restart",
                ok=True,
                message="Gateway 重启已受理，正在创建新的 2FA 启动轮次。",
                reason=reason,
                source=source,
                extra={
                    "accepted": True,
                    "operation": "gateway_restart_fresh_cycle",
                    "gateway_restarted": False,
                    "runtime_restart_requested": True,
                    "startup_cycle_planned": True,
                    "background": True,
                    "forced": force_restart,
                },
            ),
            202,
        )

    ok = bool(service.gateway_manager.restart())
    try:
        service.session_keeper.check_auth_status()
    except Exception:
        pass

    return (
        _build_gateway_action_payload(
            service,
            "restart",
            ok=ok,
            message="Gateway 已重启。" if ok else "Gateway 重启失败。",
            reason=reason,
            source=source,
            extra={
                "gateway_restarted": ok,
                "startup_cycle_planned": False,
                "forced": force_restart,
            },
        ),
        200,
    )
