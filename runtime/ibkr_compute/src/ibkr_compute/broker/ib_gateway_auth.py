from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from ibkr_compute.broker.ib_gateway_support import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_ENVIRONMENT,
    DEFAULT_LOGIN_POLL_INTERVAL_SECONDS,
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    _safe_int,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ibkr_compute.broker.ib_gateway import BrokerAdapter
    from ibkr_compute.broker.ib_gateway_service import GatewayServiceManager
    from ibkr_compute.broker.ib_gateway_session import SocketSessionKeeper


class AuthController:
    def __init__(
        self,
        pb_client=None,
        gateway_manager: Optional[GatewayServiceManager] = None,
        broker: Optional[BrokerAdapter] = None,
        session_keeper: Optional[SocketSessionKeeper] = None,
        environment: str = DEFAULT_ENVIRONMENT,
        login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
        login_poll_interval_seconds: int = DEFAULT_LOGIN_POLL_INTERVAL_SECONDS,
    ):
        self.pb_client = pb_client
        self.gateway_manager = gateway_manager
        self.broker = broker
        self.session_keeper = session_keeper
        self.environment = str(environment or DEFAULT_ENVIRONMENT)
        self.login_timeout_seconds = max(30, int(login_timeout_seconds or DEFAULT_LOGIN_TIMEOUT_SECONDS))
        self.login_poll_interval_seconds = max(1, int(login_poll_interval_seconds or DEFAULT_LOGIN_POLL_INTERVAL_SECONDS))
        self._cancelled = False

    def request_2fa_approval(
        self,
        *,
        reason: str,
        source: str,
        detail: Optional[dict] = None,
        message: str = "",
        force_reset: bool = True,
        report_pending: bool = True,
    ) -> bool:
        if not self.pb_client:
            return False
        try:
            self.pb_client.request_ibkr_2fa(
                reason=reason,
                detail=detail or {},
                source=source,
                environment=self.environment,
                message=message,
                force_reset=force_reset,
            )
            if report_pending:
                self.pb_client.report_ibkr_2fa_result(
                    "pending",
                    detail=detail or {},
                    source=source,
                    environment=self.environment,
                    message=message,
                    last_result="waiting_manual_approval",
                )
            return True
        except Exception as exc:
            logger.warning("2FA approval request failed: %s", exc)
            return False

    def _report_2fa_status(
        self,
        status: str,
        detail: Optional[dict] = None,
        *,
        reason: str = "",
        source: str = "ibkr_compute",
        message: str = "",
        last_result: str = "",
        error: str = "",
        state_patch: Optional[dict] = None,
    ):
        if not self.pb_client:
            return {}
        try:
            return self.pb_client.report_ibkr_2fa_result(
                status,
                detail=detail or {},
                source=source,
                environment=self.environment,
                message=message,
                last_result=last_result,
                error=error,
                state_patch=state_patch or ({
                    "reason": reason,
                } if reason else {}),
            )
        except Exception:
            logger.exception("2FA status reporting failed")
            return {}

    @staticmethod
    def _is_authenticated_payload(payload: Optional[dict]) -> bool:
        data = payload if isinstance(payload, dict) else {}
        return bool(data.get("authenticated") or data.get("ready"))

    @staticmethod
    def _gateway_status_code(payload: Optional[dict]) -> int:
        data = payload if isinstance(payload, dict) else {}
        return _safe_int(data.get("status_code"), 0)

    @staticmethod
    def _gateway_running(payload: Optional[dict]) -> bool:
        data = payload if isinstance(payload, dict) else {}
        return bool(data.get("running") or data.get("gateway_running"))

    def _gateway_reachable_for_2fa(self, payload: Optional[dict]) -> bool:
        data = payload if isinstance(payload, dict) else {}
        status_code = self._gateway_status_code(data)
        if status_code in {0, 502, 503}:
            return False
        return bool(data.get("reachable") or self._gateway_running(data))

    def _gateway_health_state_patch(self, payload: Optional[dict]) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        status_code = self._gateway_status_code(data)
        running = self._gateway_running(data)
        return {
            "gateway_status_code": status_code,
            "gateway_running": running,
            "gateway_reachable": self._gateway_reachable_for_2fa(data),
            "gateway_2fa_not_reached": not self._gateway_reachable_for_2fa(data),
            "push_confirmed": False,
            "runtime_authenticated": bool(data.get("authenticated") or data.get("ready")),
        }

    def _current_auth_health(self) -> dict[str, Any]:
        if self.session_keeper:
            return self.session_keeper.check_auth_status()
        if self.broker:
            return self.broker.health()
        return {}

    def _direct_broker_auth_health(self) -> dict[str, Any]:
        if not self.broker:
            return {}
        try:
            return self.broker.health()
        except Exception as exc:
            logger.debug("Direct broker auth refresh failed: %s", exc)
            return {}

    def _mark_login_success(
        self,
        *,
        detail: dict,
        reason: str,
        source: str,
        message: str,
        last_result: str,
    ) -> bool:
        self._report_2fa_status(
            "success",
            detail,
            reason=reason,
            source=source,
            message=message,
            last_result=last_result,
        )
        return True

    def _attempt_post_approval_self_heal(
        self,
        *,
        detail: dict,
        reason: str,
        source: str,
    ) -> bool:
        if not self.broker:
            return False

        try:
            health = self._current_auth_health()
        except Exception as exc:
            logger.debug("Final auth refresh before self-heal failed: %s", exc)
            health = {}
        if self._is_authenticated_payload(health):
            return self._mark_login_success(
                detail=detail,
                reason=reason,
                source=source,
                message="IB Gateway 已完成认证。",
                last_result="authenticated",
            )

        reconnect_reason = f"{reason or 'manual_auth'}_timeout"
        try:
            reconnect_payload = self.broker.force_reconnect(reason=reconnect_reason)
        except Exception as exc:
            logger.warning("Post-approval broker reconnect failed: %s", exc)
            reconnect_payload = {}
        if self._is_authenticated_payload(reconnect_payload):
            return self._mark_login_success(
                detail=detail,
                reason=reason,
                source=source,
                message="IB Gateway 已完成认证，运行态连接已自动刷新。",
                last_result="authenticated_after_broker_reconnect",
            )

        fresh_probe_payload: dict[str, Any] = {}
        try:
            fresh_probe_payload = self.broker.fresh_health_probe(reason=reconnect_reason)
        except Exception as exc:
            logger.warning("Fresh broker health probe after manual 2FA timeout failed: %s", exc)
            fresh_probe_payload = {}
        if not self._is_authenticated_payload(fresh_probe_payload):
            return False

        logger.warning(
            "Fresh broker probe authenticated after manual 2FA timeout; attempting in-process self-heal: "
            "reason=%s source=%s probe_client_id=%s",
            reason or "-",
            source or "-",
            int(fresh_probe_payload.get("probe_client_id", 0) or 0),
        )
        broker_connect_timeout = float(getattr(self.broker, "connect_timeout", DEFAULT_CONNECT_TIMEOUT_SECONDS) or DEFAULT_CONNECT_TIMEOUT_SECONDS)
        for settle_seconds in (
            0.5,
            1.0,
            float(self.login_poll_interval_seconds),
            broker_connect_timeout,
        ):
            try:
                reconnect_payload = self.broker.force_reconnect(
                    reason=f"{reconnect_reason}_fresh_probe_authenticated",
                    settle_seconds=settle_seconds,
                )
            except Exception as exc:
                logger.warning("In-process broker self-heal reconnect failed: %s", exc)
                reconnect_payload = {}
            if self._is_authenticated_payload(reconnect_payload):
                return self._mark_login_success(
                    detail=detail,
                    reason=reason,
                    source=source,
                    message="IB Gateway 已完成认证，系统已自动修复运行态连接。",
                    last_result="fresh_probe_authenticated_self_healed",
                )
            try:
                health = self._current_auth_health()
            except Exception as exc:
                logger.debug("Post self-heal auth refresh failed: %s", exc)
                health = {}
            if self._is_authenticated_payload(health):
                return self._mark_login_success(
                    detail=detail,
                    reason=reason,
                    source=source,
                    message="IB Gateway 已完成认证，系统已自动修复运行态连接。",
                    last_result="fresh_probe_authenticated_self_healed",
                )

        broker_health = self._direct_broker_auth_health()
        if self._is_authenticated_payload(broker_health):
            logger.warning(
                "Fresh broker probe authenticated after manual 2FA timeout; "
                "direct broker health recovered after self-heal retries: reason=%s source=%s",
                reason or "-",
                source or "-",
            )
            return self._mark_login_success(
                detail=detail,
                reason=reason,
                source=source,
                message="IB Gateway 已完成认证，主连接已在超时后自动恢复。",
                last_result="fresh_probe_authenticated_late_broker_recovered",
            )

        logger.warning(
            "Fresh broker probe authenticated after manual 2FA timeout, but primary broker reconnect is still settling; "
            "accepting login success to avoid a false manual re-trigger: reason=%s source=%s",
            reason or "-",
            source or "-",
        )
        return self._mark_login_success(
            detail=detail,
            reason=reason,
            source=source,
            message="IB Gateway 已完成认证，主连接正在延迟恢复。",
            last_result="fresh_probe_authenticated_pending_reconnect",
        )

    def login(
        self,
        *,
        reason: str,
        source: str,
        detail: Optional[dict] = None,
    ) -> bool:
        detail = detail or {}
        self._cancelled = False

        if self.gateway_manager and not self.gateway_manager.is_running:
            if not self.gateway_manager.start():
                self._report_2fa_status(
                    "failed",
                    detail,
                    reason=reason,
                    source=source,
                    message="IB Gateway 未能启动，当前 2FA 轮次无法开始。",
                    last_result="gateway_start_failed",
                    error="gateway_start_failed",
                )
                return False

        approval_requested = self.request_2fa_approval(
            reason=reason,
            source=source,
            detail=detail,
            message="IB Gateway 登录流程已触发，正在等待 Gateway 进入 2FA；手机 Push 尚未确认发出。",
            force_reset=False,
            report_pending=False,
        )
        reported_gateway_not_ready = False
        reported_waiting_confirm = False

        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self._cancelled:
                self._report_2fa_status(
                    "failed",
                    detail,
                    reason=reason,
                    source=source,
                    message="登录轮次已取消。",
                    last_result="cancelled",
                    error="cancelled",
                )
                return False

            health: dict[str, Any] = {}
            try:
                health = self._current_auth_health()
                if self._is_authenticated_payload(health):
                    return self._mark_login_success(
                        detail=detail,
                        reason=reason,
                        source=source,
                        message="IB Gateway 已完成认证。",
                        last_result="authenticated",
                    )
                if self._gateway_reachable_for_2fa(health):
                    if not reported_waiting_confirm:
                        self._report_2fa_status(
                            "waiting_confirm",
                            detail,
                            reason=reason,
                            source=source,
                            message="IB Gateway 已进入认证等待；如果手机收到 IBKR Push，请只确认当前这一轮。",
                            last_result="waiting_mobile_approval",
                            state_patch={
                                **self._gateway_health_state_patch(health),
                                "gateway_2fa_not_reached": False,
                                "push_confirmed": True,
                            },
                        )
                        reported_waiting_confirm = True
                elif not reported_gateway_not_ready:
                    self._report_2fa_status(
                        "triggered",
                        detail,
                        reason=reason,
                        source=source,
                        message="IB Gateway API 尚不可用，暂未确认 IBKR 已向手机发送 Push。",
                        last_result="gateway_not_ready_push_not_confirmed",
                        state_patch=self._gateway_health_state_patch(health),
                    )
                    reported_gateway_not_ready = True
            except Exception as exc:
                logger.debug("IB Gateway auth poll failed: %s", exc)
                if not reported_gateway_not_ready:
                    self._report_2fa_status(
                        "triggered",
                        detail,
                        reason=reason,
                        source=source,
                        message="IB Gateway 认证探测失败，暂未确认 IBKR 已向手机发送 Push。",
                        last_result="gateway_probe_failed_push_not_confirmed",
                        state_patch={
                            "gateway_status_code": 503,
                            "gateway_running": bool(self.gateway_manager and self.gateway_manager.is_running),
                            "gateway_reachable": False,
                            "gateway_2fa_not_reached": True,
                            "push_confirmed": False,
                        },
                    )
                    reported_gateway_not_ready = True

            time.sleep(self.login_poll_interval_seconds)

        if self._attempt_post_approval_self_heal(
            detail=detail,
            reason=reason,
            source=source,
        ):
            return True

        self._report_2fa_status(
            "failed",
            detail,
            reason=reason,
            source=source,
            message=(
                "等待手机确认 2FA 超时，请重新触发当前轮次。"
                if approval_requested and reported_waiting_confirm else
                "Gateway 未真正进入 2FA 手机确认阶段；请重启 IB Gateway 后重新触发。"
                if approval_requested else
                "2FA 请求未成功送达且等待认证超时，请检查 Gateway / IBC 配置后重试。"
            ),
            last_result="login_timeout" if reported_waiting_confirm else "gateway_not_ready_timeout",
            error="login_timeout",
            state_patch={
                "gateway_2fa_not_reached": not reported_waiting_confirm,
                "push_confirmed": bool(reported_waiting_confirm),
            },
        )
        return False

    def cancel(self):
        self._cancelled = True

    def reset_cancel(self):
        self._cancelled = False

    def status(self) -> dict:
        return {
            "cancelled": bool(self._cancelled),
            "environment": self.environment,
            "gateway_service": self.gateway_manager.service_name if self.gateway_manager else "",
        }
