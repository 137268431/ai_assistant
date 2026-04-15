from __future__ import annotations

import threading
import time


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceStartupMixin:
    def _emit_system_event(
        self,
        event_type: str,
        level: str,
        title: str,
        detail: dict,
        *,
        message_id: str = "",
    ):
        service_mod = _service_mod()
        if not self.pb:
            return {}
        try:
            return self.pb.notify_system_event(
                title=title,
                detail=detail,
                event_type=event_type,
                level=level,
                source="ibkr_compute",
                environment=service_mod.ENVIRONMENT,
                message_id=message_id,
            )
        except Exception as exc:
            service_mod.logger.warning(
                "System event emit failed (%s/%s): %s",
                event_type,
                title,
                exc,
            )
            return {}

    def _notify_session_issue(
        self,
        kind: str,
        title: str,
        summary: str,
        recommendation: str,
        extra_detail: dict | None = None,
    ):
        service_mod = _service_mod()
        now = time.time()
        should_send = (
            self._last_session_issue_kind != kind
            or self._last_session_issue_at <= 0
            or (now - self._last_session_issue_at) >= service_mod.SESSION_EVENT_ALERT_COOLDOWN_SECONDS
        )
        self._last_session_issue_kind = kind
        self._last_session_issue_title = title
        if not should_send:
            return

        detail = {
            "异常结论": summary,
            "检查时间": self._now_et(),
            "Session认证": "no",
            "Runtime阶段": self._runtime_phase_label(),
            "处理建议": recommendation,
        }
        if self._auth_required_reason:
            detail["触发原因"] = self._auth_required_reason
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        if extra_detail:
            detail.update(extra_detail)

        self._emit_system_event("alert", "warning", title, detail)
        self._last_session_issue_at = now

    def _notify_session_recovered(self, previous_kind: str):
        if not previous_kind:
            return
        detail = {
            "状态结论": "IBKR Session 已恢复认证，当前运行态重新正确。",
            "检查时间": self._now_et(),
            "Session认证": "yes",
            "Runtime阶段": self._runtime_phase_label(),
            "恢复来源": previous_kind,
            "后续动作": "系统将继续 warmup、订阅刷新和信号处理。",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        self._emit_system_event("alert", "info", "IBKR Session 已恢复认证", detail)
        self._last_session_issue_kind = ""
        self._last_session_issue_title = ""
        self._last_session_issue_at = 0.0

    def start(self, trigger_login: bool = False, reason: str = "manual_start", source: str = "api_start"):
        service_mod = _service_mod()
        with self._state_lock:
            if self._running:
                service_mod.logger.info("IBKR Trading Service already running")
                return
            if self._starting:
                service_mod.logger.info("IBKR Trading Service already starting")
                return
            self._starting = True
            self._startup_progress_enabled = self._should_publish_startup_progress(reason, source, trigger_login)
            self._startup_cycle_id = ""
            self._startup_reason = reason
            self._startup_source = source
            self._startup_trigger_login = bool(trigger_login)
            self._startup_status_message_id = ""

        startup_ok = False
        startup_exit_notice = None
        reset_startup_progress = False

        try:
            service_mod.logger.info("=" * 60)
            service_mod.logger.info(
                "IBKR Trading Service starting (env=%s reason=%s source=%s trigger_login=%s)",
                service_mod.ENVIRONMENT,
                reason or "-",
                source or "-",
                bool(trigger_login),
            )
            service_mod.logger.info("=" * 60)
            self._set_auth_recovery_state(
                recovery_phase="starting_runtime",
                recovery_reason=reason,
                last_recovery_source=source,
                lock_owner="runtime_start",
                lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
            )

            self.auth_handler.reset_cancel()
            self.config.refresh()
            self._refresh_runtime_settings()
            self._announce_startup_pending(reason, source, trigger_login)

            if self._should_restart_gateway_before_start(reason, source, trigger_login):
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="手动启动默认先重启 Gateway，确保本轮启动与 2FA 使用全新会话。",
                    current_step="gateway",
                    current_blocker="正在重启 Gateway 并清空旧 Session",
                    operator_action="等待 Gateway 重启完成后进入当前轮次",
                    steps={
                        "gateway": {
                            "status": "running",
                            "detail": "手动启动 / 每周重验会先重启 Gateway，再进入人工 2FA。",
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, trigger_login),
                    reason=reason,
                    source=source,
                    trigger_login=trigger_login,
                )
                prepared, gateway_detail = self._restart_gateway_with_clean_session(reason, source)
                if not prepared:
                    service_mod.logger.error("Gateway preflight restart failed, exiting")
                    startup_exit_notice = {
                        "level": "error",
                        "title": "IBKR Runtime 启动失败",
                        "detail": {
                            "异常结论": "Gateway 重启失败，Runtime 未能进入新的启动轮次。",
                            "检查时间": self._now_et(),
                            "失败阶段": "gateway",
                            "启动原因": reason or "manual_start",
                            "启动来源": source or "api_start",
                            **gateway_detail,
                        },
                    }
                    return

            if not self._ensure_gateway():
                service_mod.logger.error("Gateway setup failed, exiting")
                startup_exit_notice = {
                    "level": "error",
                    "title": "IBKR Runtime 启动失败",
                    "detail": {
                        "异常结论": "Gateway 启动失败，Runtime 未能进入运行态。",
                        "检查时间": self._now_et(),
                        "失败阶段": "gateway",
                        "启动原因": reason or "manual_start",
                        "启动来源": source or "api_start",
                    },
                }
                return
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Gateway 已可用，正在检查 Session / 2FA 状态。",
                current_step="auth",
                current_blocker="等待确认当前 Gateway Session 是否已认证",
                operator_action="等待系统完成认证检查；如未认证则转入手动 2FA",
                steps={
                    "gateway": {
                        "status": "done",
                        "detail": "Gateway 已启动并可访问。",
                    },
                    "auth": {
                        "status": "running",
                        "detail": "正在检查 Session / 2FA 认证状态。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            # Do a one-shot auth check WITHOUT starting the keeper loop.
            # Starting session_keeper here would cause it to periodically
            # tickle + check_auth during the 2FA wait, creating a second/third
            # HTTP client hitting the Gateway and interfering with the SSO flow.
            self.session_keeper.check_auth_status()
            time.sleep(1)

            if not self.session_keeper.is_authenticated:
                if not trigger_login:
                    if self._should_promote_auth_wait_to_startup_cycle(reason, source, trigger_login):
                        service_mod.logger.info(
                            "Promoting hidden auth wait into visible startup cycle (reason=%s source=%s)",
                            reason or "-",
                            source or "-",
                        )
                        self._startup_progress_enabled = True
                        self._announce_startup_pending(reason, source, False)
                    service_mod.logger.warning(
                        "Not authenticated and trigger_login disabled; requesting manual 2FA"
                    )
                    self._request_manual_2fa(
                        reason,
                        "检测到会话未认证，请在准备好时点击按钮触发 2FA。",
                    )
                    self._set_auth_recovery_state(
                        recovery_phase="requested",
                        recovery_reason=reason,
                        probe_result="manual_trigger_required",
                        last_recovery_source=source,
                        lock_owner="",
                        lock_expires_at="",
                    )
                    waiting_detail = {
                        "状态结论": "检测到 Gateway 当前未认证，Runtime 尚未完成启动。",
                        "检查时间": self._now_et(),
                        "当前动作": "等待当前启动卡片下方的人工按钮触发当前轮次。",
                    }
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="Gateway 尚未认证，启动流程等待人工触发 2FA。",
                        current_step="auth",
                        current_blocker="等待手动触发 2FA",
                        operator_action="点击当前启动卡片下方“开始 2FA 验证”",
                        steps={
                            "auth": {
                                "status": "waiting",
                                "detail": "当前不会自动发送新的 Push，需人工点击 2FA 卡片触发。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, False, waiting_detail),
                        reason=reason,
                        source=source,
                        trigger_login=False,
                        record_event=True,
                        event_type="status_change",
                        event_title="IBKR Runtime 等待手动 2FA",
                        event_detail=waiting_detail,
                        level="warning",
                    )
                    return
                if self._should_force_fresh_manual_auth_cycle(trigger_login, source):
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="正在清空旧 Session 并重启 Gateway，确保本轮 2FA 使用全新会话。",
                        current_step="auth",
                        current_blocker="等待旧 Session 清理完成并生成新的 2FA 会话",
                        operator_action="等待系统完成 Gateway 重置后自动进入当前轮次",
                        steps={
                            "auth": {
                                "status": "running",
                                "detail": "当前轮次会先重置旧 Session，再发起新的 2FA。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, True),
                        reason=reason,
                        source=source,
                        trigger_login=True,
                    )
                    prepared, fresh_detail = self._prepare_fresh_manual_auth_cycle(reason, source)
                    if not prepared:
                        service_mod.logger.error("Fresh manual auth preparation failed, exiting")
                        self._set_auth_recovery_state(
                            recovery_phase="failed",
                            recovery_reason=reason,
                            probe_result="fresh_auth_prepare_failed",
                            last_recovery_source=source,
                            lock_owner="",
                            lock_expires_at="",
                        )
                        self._sync_startup_progress(
                            action="update",
                            title="IBKR Runtime 启动中",
                            summary="旧 Session 清理失败，当前轮次未启动。",
                            current_step="auth",
                            current_blocker="Gateway 重置失败",
                            operator_action="稍后重新点击“开始 2FA 验证”",
                            steps={
                                "auth": {
                                    "status": "failed",
                                    "detail": "本轮在清理旧 Session / 重启 Gateway 时失败。",
                                },
                            },
                            fields=self._build_startup_progress_fields(reason, source, True, fresh_detail),
                            reason=reason,
                            source=source,
                            trigger_login=True,
                            record_event=True,
                            event_type="status_change",
                            event_title="IBKR Runtime 重置旧 Session 失败",
                            event_detail=fresh_detail,
                            level="error",
                        )
                        return
                service_mod.logger.info("Not authenticated, attempting login (session_keeper paused)...")
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="已显式触发 2FA，等待当前轮次完成认证。",
                    current_step="auth",
                    current_blocker="等待手机确认 2FA Push",
                    operator_action="查看手机通知；如切到 Challenge/Response，则去 Runtime 页面提交 Response Code",
                    steps={
                        "auth": {
                            "status": "running",
                            "detail": "当前轮次已启动，不会自动补发新的 Push。",
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, True),
                    reason=reason,
                    source=source,
                    trigger_login=True,
                )
                if not self.auth_handler.login(
                    reason=reason,
                    source=source,
                    detail=self._build_2fa_detail(reason),
                ):
                    service_mod.logger.error("Login failed, exiting")
                    self._auth_required_reason = reason
                    self._set_auth_recovery_state(
                        recovery_phase="failed",
                        recovery_reason=reason,
                        probe_result="login_failed",
                        last_recovery_source=source,
                        lock_owner="",
                        lock_expires_at="",
                    )
                    retry_detail = {
                        "状态结论": "当前 2FA 轮次未成功建立可用 Session，启动流程暂停。",
                        "检查时间": self._now_et(),
                        "当前动作": "请重新点击当前启动卡片下方按钮，手动触发下一轮。",
                    }
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="当前 2FA 轮次未完成认证，启动流程等待人工重新触发。",
                        current_step="auth",
                        current_blocker="2FA 未完成，需手动重新触发",
                        operator_action="回到当前启动卡片，重新点击下方“开始 2FA 验证”",
                        steps={
                            "auth": {
                                "status": "failed",
                                "detail": "本轮不会自动重试新的 Push，请人工重新触发。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, True, retry_detail),
                        reason=reason,
                        source=source,
                        trigger_login=True,
                        record_event=True,
                        event_type="status_change",
                        event_title="IBKR Runtime 等待重新触发 2FA",
                        event_detail=retry_detail,
                        level="warning",
                    )
                    return

                # Login succeeded — re-check auth once with the shared cookie store
                # before enabling the periodic keeper loop.
                self.session_keeper.check_auth_status()
                time.sleep(1)
            else:
                self._auth_required_reason = ""
                if reason != "manual_start" or source in ("feishu_callback", "runtime_page"):
                    self.auth_handler._report_2fa_status(
                        status="success",
                        reason=reason,
                        source=source,
                        detail=self._build_2fa_detail(reason),
                        message="Gateway 已处于认证状态，无需再次确认。",
                        last_result="复用现有认证会话。",
                    )
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Session / 2FA 已认证，正在装载订阅与核心线程。",
                current_step="subscriptions",
                current_blocker="等待 Targets / Subscriptions 装载完成",
                operator_action="等待系统继续装载订阅、线程与预热",
                steps={
                    "auth": {
                        "status": "done",
                        "detail": "Session / 2FA 已通过认证。",
                    },
                    "subscriptions": {
                        "status": "running",
                        "detail": "正在装载活动标的与订阅。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self._last_session_authenticated = bool(self.session_keeper.is_authenticated)
            if self._last_session_authenticated:
                self._mark_auth_recovered(source=source, reason=reason or "startup_authenticated")
            self.conid_resolver.load_cache_from_pb()
            self._reset_for_new_market_day(force=True)
            self._refresh_watchlist_pool(force=True)
            self._restore_watchlist_integrity_cursor()
            self.ws_client.start()
            time.sleep(2)
            self._refresh_target_subscriptions(force=True, reason="startup")
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Targets / Subscriptions 已装载，正在启动 WebSocket、订单与后台线程。",
                current_step="core_threads",
                current_blocker="等待 WebSocket、订单链路与后台线程全部启动",
                operator_action="等待系统拉起核心线程与 warmup 线程",
                steps={
                    "subscriptions": {
                        "status": "done",
                        "detail": f"活动订阅标的: {len(self._active_subscription_symbols)}",
                    },
                    "core_threads": {
                        "status": "running",
                        "detail": "正在启动 WebSocket、订单与后台线程。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self.order_tracker.start()
            self.order_lifecycle.start()

            self._running = True
            startup_ok = True
            self.session_keeper.start()
            self._signal_thread = threading.Thread(
                target=self._signal_loop,
                daemon=True,
                name="signal-loop",
            )
            self._signal_thread.start()
            self._subscription_thread = threading.Thread(
                target=self._subscription_refresh_loop,
                daemon=True,
                name="target-refresh",
            )
            self._subscription_thread.start()
            self._active_repair_thread = threading.Thread(
                target=self._active_repair_loop,
                daemon=True,
                name="active-repair",
            )
            self._active_repair_thread.start()
            self._watchlist_backfill_thread = threading.Thread(
                target=self._watchlist_backfill_loop,
                daemon=True,
                name="watchlist-backfill",
            )
            self._watchlist_backfill_thread.start()
            self._compute_thread = threading.Thread(
                target=self._compute_loop,
                daemon=True,
                name="close-compute",
            )
            self._compute_thread.start()
            self._official_close_thread = threading.Thread(
                target=self._official_5m_close_loop,
                daemon=True,
                name="official-5m-close",
            )
            self._official_close_thread.start()
            self._bar_close_thread = threading.Thread(
                target=self._bar_close_loop,
                daemon=True,
                name="bar-close-guard",
            )
            self._bar_close_thread.start()
            self._warmup_thread = threading.Thread(
                target=self._warmup_loop,
                daemon=True,
                name="runtime-warmup",
            )
            self._warmup_thread.start()
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="核心线程已启动，正在进入 Warmup / 预检修复 / 历史回补。",
                current_step="warmup",
                current_blocker="等待 Warmup、预检修复与历史回补完成",
                operator_action="等待 warmup 线程推进预检修复与交易门开放",
                steps={
                    "core_threads": {
                        "status": "done",
                        "detail": "WebSocket、订单链路与后台线程已启动。",
                    },
                    "warmup": {
                        "status": "running",
                        "detail": "正在执行 Warmup、预检修复与历史回补。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self._schedule_retention()

            if not self._active_subscription_symbols:
                self._complete_startup_success(
                    "IBKR Runtime 启动完成（无活动标的）",
                    {
                        "Warmup结果": "0/0 ready",
                        "交易标的": "0/0 ready",
                        "监控标的": "0/0 ready",
                        "预检修复标的": "-",
                        "回补写入Bars": 0,
                        "交易门": "closed",
                    },
                )
            service_mod.logger.info(
                "IBKR Trading Service core components started; waiting for warmup readiness"
            )
        except Exception as exc:
            startup_exit_notice = {
                "event_type": "alert",
                "level": "error",
                "title": "IBKR Runtime 启动失败",
                "detail": {
                    "异常结论": "启动过程中发生未处理异常，Runtime 未能完成启动。",
                    "检查时间": self._now_et(),
                    "失败阶段": "startup_exception",
                    "启动原因": reason or "manual_start",
                    "启动来源": source or "api_start",
                    "异常": str(exc),
                },
            }
            raise
        finally:
            exit_notice = None
            with self._state_lock:
                if not startup_ok and not self._running:
                    self._starting = False
                    self._startup_cycle_id = ""
                    self._startup_reason = ""
                    self._startup_source = ""
                    self._startup_trigger_login = False
                    self._startup_status_message_id = ""
                    if startup_exit_notice:
                        exit_notice = dict(startup_exit_notice)
                    reset_startup_progress = True
            if exit_notice:
                exit_detail = dict(exit_notice.get("detail", {}) or {})
                failure_stage = str(exit_detail.get("失败阶段") or "").strip().lower()
                failed_step = "core_threads"
                if failure_stage == "gateway":
                    failed_step = "gateway"
                elif failure_stage == "session_login":
                    failed_step = "auth"
                elif failure_stage == "startup_exception":
                    failed_step = "core_threads"
                operator_action = "检查失败阶段并在处理后重新启动 Runtime"
                if failed_step == "gateway":
                    operator_action = "检查 Gateway 进程与服务状态后重新启动 Runtime"
                elif failed_step == "auth":
                    operator_action = "检查 2FA 当前轮次并手动重新触发"
                self._sync_startup_progress(
                    action="fail",
                    title=exit_notice.get("title", "IBKR Runtime 启动失败"),
                    summary=str(exit_detail.get("异常结论") or "启动流程未能完成。"),
                    current_step=failed_step,
                    current_blocker=str(exit_detail.get("异常结论") or "启动流程未能完成。"),
                    operator_action=operator_action,
                    steps={
                        failed_step: {
                            "status": "failed",
                            "detail": str(exit_detail.get("异常") or exit_detail.get("失败阶段") or "启动失败"),
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, trigger_login, exit_detail),
                    reason=reason,
                    source=source,
                    trigger_login=trigger_login,
                    record_event=True,
                    event_type="alert",
                    event_title=exit_notice.get("title", "IBKR Runtime 启动失败"),
                    event_detail=exit_detail,
                    level=exit_notice.get("level", "error"),
                )
            if reset_startup_progress:
                with self._state_lock:
                    self._startup_progress_enabled = False

    def _ensure_gateway(self) -> bool:
        service_mod = _service_mod()
        if not self.gateway_manager.is_running:
            service_mod.logger.info("Starting IB Gateway...")
            if not self.gateway_manager.start():
                service_mod.logger.error("Failed to start IB Gateway")
                return False
            time.sleep(8)
        return True

    def _on_session_expired(self):
        service_mod = _service_mod()
        if self._starting and not self._running:
            service_mod.logger.info("Ignoring session_expired callback during runtime startup")
            return
        self._last_session_authenticated = False
        self._close_warmup_gate("session_unauthenticated")
        service_mod.logger.warning("Session expired; starting auth recovery probe")
        self._start_auth_recovery(
            interruption_kind="session_expired",
            recovery_reason="session_expired",
            source="session_keeper",
        )
        self._notify_session_issue(
            "session_expired",
            "IBKR Session 已失效，需重新触发 2FA",
            "检测到 IBKR Session 已失效，运行态已降为未认证，实时行情和交易链路可能不可用。",
            "系统会先尝试静默探测恢复；若仍未恢复，会自动重开一轮 2FA。你也可以在 Runtime 页面开启人工接管或手动全量重置。",
            {
                "恢复动作": "已进入静默探测窗口",
            },
        )

    def _on_gateway_down(self):
        service_mod = _service_mod()
        if self._starting and not self._running:
            service_mod.logger.info("Ignoring gateway_down callback during runtime startup")
            return
        self._last_session_authenticated = False
        self._close_warmup_gate("gateway_down")
        service_mod.logger.error("Gateway down, restarting gateway and starting auth recovery probe...")
        self.gateway_manager.restart()
        time.sleep(10)
        self.session_keeper.check_auth_status()
        self._start_auth_recovery(
            interruption_kind="gateway_down",
            recovery_reason="gateway_down",
            source="gateway_down",
        )
        self._notify_session_issue(
            "gateway_down",
            "IBKR Gateway 不可达，已触发重启",
            "检测到 Gateway 一度不可达，已执行自动重启；当前运行态不可用，通常需要重新完成 2FA。",
            "系统会先尝试静默探测恢复；若仍未恢复，会自动重开一轮 2FA。必要时可在 Runtime 页面执行全量清空后重试。",
            {
                "Gateway动作": "已自动重启",
                "恢复动作": "已进入静默探测窗口",
            },
        )
