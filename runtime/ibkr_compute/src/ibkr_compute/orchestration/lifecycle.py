from __future__ import annotations

import json
import time
from datetime import datetime, timedelta


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceLifecycleMixin:
    def _build_2fa_detail(self, reason: str) -> dict:
        service_mod = _service_mod()
        return {
            "环境": service_mod.ENVIRONMENT,
            "原因": reason,
        }

    def _request_manual_2fa(self, reason: str, message: str) -> bool:
        service_mod = _service_mod()
        self._auth_required_reason = reason
        requested = self.auth_handler.request_2fa_approval(
            reason=reason,
            source="ibkr_service",
            detail=self._build_2fa_detail(reason),
            message=message,
            force_reset=True,
            report_pending=False,
        )
        if requested:
            service_mod.logger.info("Manual 2FA request sent: %s", reason)
        else:
            service_mod.logger.warning("Manual 2FA request failed to send: %s", reason)
        return requested

    def _should_force_fresh_manual_auth_cycle(self, trigger_login: bool, source: str) -> bool:
        service_mod = _service_mod()
        if not service_mod.FORCE_FRESH_MANUAL_AUTH_CYCLE or not trigger_login:
            return False
        return str(source or "").strip().lower() in service_mod.FRESH_MANUAL_AUTH_SOURCES

    def _is_server_boot_resume_attempt(self, reason: str, source: str, trigger_login: bool) -> bool:
        if bool(trigger_login) or not bool(self._server_boot_resume_only):
            return False
        source_key = str(source or "").strip().lower()
        reason_key = str(reason or "").strip().lower()
        return source_key == "server_boot" or reason_key == "auto_restore"

    def _should_restart_gateway_before_start(self, reason: str, source: str, trigger_login: bool) -> bool:
        if bool(trigger_login):
            return False
        source_key = str(source or "").strip().lower()
        reason_key = str(reason or "").strip().lower()
        if reason_key == "manual_start":
            return bool(self._manual_start_restart_gateway)
        if reason_key == "weekly_reauth":
            return bool(self._weekly_reauth_restart_gateway)
        if self._is_server_boot_resume_attempt(reason, source, False):
            return False
        if reason_key == "auto_restore" or source_key == "server_boot":
            return not bool(self._server_boot_resume_only)
        return False

    def _restart_gateway_with_clean_session(self, reason: str, source: str) -> tuple[bool, dict]:
        service_mod = _service_mod()
        source_key = str(source or "").strip().lower()
        detail = {
            "检查时间": self._now_et(),
            "启动来源": source_key or "unknown",
            "启动原因": reason or "manual_start",
        }

        service_mod.logger.info(
            "Restarting Gateway with clean session: source=%s reason=%s",
            source_key or "-",
            reason or "-",
        )
        self.auth_handler.cancel()
        self.auth_handler.reset_cancel()

        cookie_result = service_mod.clear_cookies()
        detail["Cookie已清空"] = "yes" if cookie_result.get("removed") or not cookie_result.get("existed") else "no"
        if cookie_result.get("error"):
            detail["Cookie清理错误"] = str(cookie_result.get("error"))[:180]

        gateway_restarted = bool(self.gateway_manager.restart())
        detail["Gateway已重启"] = "yes" if gateway_restarted else "no"
        detail["GatewayPID"] = str(self.gateway_manager.pid or "")

        if not gateway_restarted:
            service_mod.logger.error("Failed to restart Gateway for clean session restart")
            detail["执行结果"] = "gateway_restart_failed"
            return False, detail

        time.sleep(1)
        self.session_keeper.check_auth_status()
        time.sleep(1)
        detail["Session已认证"] = "yes" if self.session_keeper.is_authenticated else "no"
        detail["执行结果"] = "ok"
        return True, detail

    def _prepare_fresh_manual_auth_cycle(self, reason: str, source: str) -> tuple[bool, dict]:
        service_mod = _service_mod()
        source_key = str(source or "").strip().lower()
        detail = {
            "检查时间": self._now_et(),
            "启动来源": source_key or "unknown",
            "启动原因": reason or "manual_start",
        }
        if not self._should_force_fresh_manual_auth_cycle(True, source_key):
            detail["执行结果"] = "skipped"
            return True, detail

        ok, restart_detail = self._restart_gateway_with_clean_session(reason, source_key)
        detail.update(restart_detail)
        if not ok:
            service_mod.logger.error("Failed to restart Gateway for fresh manual auth cycle")
            return False, detail
        service_mod.logger.info(
            "Fresh manual auth cycle ready: gateway_pid=%s authenticated=%s",
            self.gateway_manager.pid or "-",
            self.session_keeper.is_authenticated,
        )
        return True, detail

    def startup_strategy(self) -> dict:
        manual_start_mode = "fresh_cycle" if self._manual_start_restart_gateway else "resume_only"
        weekly_reauth_mode = "fresh_cycle" if self._weekly_reauth_restart_gateway else "resume_only"
        server_boot_mode = "resume_only" if self._server_boot_resume_only else "fresh_cycle"
        manual_gateway_restart_mode = "fresh_cycle"
        server_boot_card = bool(self._server_boot_publish_startup_card)
        startup_card_scope = "all_startups" if server_boot_card else "fresh_cycles_only"
        summary = (
            "手动启动 / 每周重验 / Gateway 重启走 fresh cycle；"
            "server_boot 默认只做 resume，不主动新开 2FA。"
        )
        if server_boot_mode == "fresh_cycle":
            summary = "手动启动、每周重验、Gateway 重启与 server_boot 都会走 fresh cycle。"
        elif server_boot_card:
            summary = (
                "手动启动 / 每周重验 / Gateway 重启走 fresh cycle；"
                "server_boot 默认只做 resume，但会同步发送启动卡片。"
            )
        return {
            "manual_start_mode": manual_start_mode,
            "weekly_reauth_mode": weekly_reauth_mode,
            "manual_gateway_restart_mode": manual_gateway_restart_mode,
            "server_boot_mode": server_boot_mode,
            "server_boot_publish_startup_card": server_boot_card,
            "fresh_cycle_requires_manual_2fa": True,
            "startup_card_scope": startup_card_scope,
            "summary": summary,
        }

    def _now_iso(self) -> str:
        service_mod = _service_mod()
        return datetime.now(service_mod.ET).isoformat()

    def _now_et(self) -> str:
        return self._now_iso()

    def _runtime_page_url(self) -> str:
        service_mod = _service_mod()
        base_url = service_mod.PB_PUBLIC_URL or ""
        if not base_url:
            return ""
        return f"{base_url}/ibkr_runtime.html?environment={service_mod.ENVIRONMENT}"

    def _runtime_phase_label(self) -> str:
        if self._starting:
            return "starting"
        if self._running:
            return "running"
        return "stopped"

    def _load_startup_progress_state(self) -> dict:
        service_mod = _service_mod()
        if not self.pb:
            return {}
        try:
            record = self.pb.get_state(
                service_mod.STARTUP_PROGRESS_STATE_KEY,
                service_mod.ENVIRONMENT,
                date=service_mod.STARTUP_PROGRESS_STATE_DATE,
            ) or {}
            data = record.get("data") if isinstance(record, dict) else {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            service_mod.logger.debug("Failed to load startup progress state: %s", exc)
            return {}

    def startup_progress_snapshot(self) -> dict:
        data = self._load_startup_progress_state()
        return {
            "active": bool(data.get("active")),
            "status": str(data.get("status") or ""),
            "cycle_id": str(data.get("cycle_id") or ""),
            "startup_label": str(data.get("startup_label") or ""),
            "startup_chat_id": str(data.get("startup_chat_id") or ""),
            "message_id": str(data.get("message_id") or ""),
            "reason": str(data.get("reason") or ""),
            "source": str(data.get("source") or ""),
            "current_step": str(data.get("current_step") or ""),
            "current_blocker": str(data.get("current_blocker") or ""),
            "operator_action": str(data.get("operator_action") or ""),
        }

    def _has_active_startup_cycle(self) -> bool:
        return bool(self.startup_progress_snapshot().get("active"))

    def auto_restore_guard(self) -> dict:
        startup = self.startup_progress_snapshot()
        auth = self._copy_auth_recovery_state()
        phase = str(auth.get("recovery_phase") or "").strip().lower()
        lock_owner = str(auth.get("lock_owner") or "").strip().lower()
        blocked_reasons = []

        if bool(startup.get("active")):
            blocked_reasons.append("startup_cycle_active")
        if phase in {"panic_resetting", "starting_runtime", "silent_probe", "requested", "manual_takeover"}:
            blocked_reasons.append(f"auth_recovery_phase:{phase}")
        if lock_owner in {"panic_reset", "runtime_start", "auth_probe", "manual_takeover"}:
            blocked_reasons.append(f"auth_recovery_lock:{lock_owner}")
        if self._auth_restart_thread and self._auth_restart_thread.is_alive():
            blocked_reasons.append("auth_restart_thread_alive")

        return {
            "allowed": not blocked_reasons,
            "blocked": bool(blocked_reasons),
            "reasons": blocked_reasons,
            "startup_active": bool(startup.get("active")),
            "startup_status": str(startup.get("status") or ""),
            "startup_label": str(startup.get("startup_label") or ""),
            "auth_recovery_phase": phase,
            "auth_recovery_lock_owner": lock_owner,
        }

    def _build_startup_pending_detail(self, reason: str, source: str, trigger_login: bool) -> dict:
        detail = {
            "状态结论": "IBKR Runtime 正在启动，交易链路暂未开放。",
            "系统简介": "负责 Gateway 会话、实时行情、订单链路、信号处理与启动预热。",
            "启动成功条件": (
                "1. Gateway 可用\n"
                "2. Session / 2FA 认证完成\n"
                "3. Targets / Subscriptions 装载完成\n"
                "4. WebSocket、订单链路与后台线程已启动\n"
                "5. Warmup / 预检修复 / 历史回补达到启动要求\n"
                "6. Trading Gate 打开或进入监控模式"
            ),
            "检查时间": self._now_et(),
            "Runtime阶段": "starting",
            "启动原因": reason or "manual_start",
            "启动来源": source or "api_start",
            "触发登录": "yes" if trigger_login else "no",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        return detail

    def _record_startup_progress_context(self, result: dict | None):
        if not isinstance(result, dict):
            return
        cycle_id = str(result.get("cycle_id", "") or "").strip()
        message_id = str(result.get("message_id", "") or "").strip()
        with self._state_lock:
            if not self._starting:
                return
            if cycle_id:
                self._startup_cycle_id = cycle_id
            if message_id:
                self._startup_status_message_id = message_id

    def _build_startup_progress_fields(
        self,
        reason: str,
        source: str,
        trigger_login: bool,
        extra: dict | None = None,
    ) -> dict:
        fields = {
            "启动原因": reason or "manual_start",
            "启动来源": source or "api_start",
            "触发登录": "yes" if trigger_login else "no",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            fields["运行页"] = runtime_url
        if extra:
            fields.update(extra)
        return fields

    def _should_publish_startup_progress(self, reason: str, source: str, trigger_login: bool) -> bool:
        source_key = str(source or "").strip().lower()
        reason_key = str(reason or "").strip().lower()
        if bool(trigger_login):
            return True
        if self._is_server_boot_resume_attempt(reason, source, False):
            return bool(self._server_boot_publish_startup_card or not self._server_boot_resume_only)
        if reason_key == "auto_restore" or source_key == "server_boot":
            return bool(self._server_boot_publish_startup_card or not self._server_boot_resume_only)
        if source_key in {"api_start", "runtime_page", "feishu_callback", "feishu_2fa", "codex_validation"}:
            return True
        if reason_key in {"manual_start", "manual_reauth", "weekly_reauth", "panic_reset_2fa", "manual_gateway_restart"}:
            return True
        return False

    def _should_promote_auth_wait_to_startup_cycle(self, reason: str, source: str, trigger_login: bool) -> bool:
        if bool(trigger_login) or self._startup_progress_enabled:
            return False
        if self._is_server_boot_resume_attempt(reason, source, trigger_login):
            return False
        source_key = str(source or "").strip().lower()
        reason_key = str(reason or "").strip().lower()
        return source_key == "server_boot" or reason_key == "auto_restore"

    def _sync_startup_progress(
        self,
        *,
        action: str = "update",
        status: str = "",
        title: str = "",
        summary: str = "",
        current_step: str = "",
        current_blocker: str = "",
        operator_action: str = "",
        steps: dict | None = None,
        fields: dict | None = None,
        reason: str = "",
        source: str = "",
        trigger_login: bool | None = None,
        record_event: bool = False,
        event_type: str = "status_change",
        event_title: str = "",
        event_detail: dict | None = None,
        level: str = "info",
        create_if_missing: bool = False,
        allow_when_disabled: bool = False,
    ) -> dict:
        service_mod = _service_mod()
        if not self.pb:
            return {}
        if not self._startup_progress_enabled and not allow_when_disabled:
            return {}
        try:
            result = self.pb.sync_startup_progress(
                action=action,
                environment=service_mod.ENVIRONMENT,
                status=status,
                title=title,
                summary=summary,
                current_step=current_step,
                current_blocker=current_blocker,
                operator_action=operator_action,
                reason=reason,
                source=source,
                runtime_phase=self._runtime_phase_label(),
                runtime_url=self._runtime_page_url(),
                trigger_login=trigger_login,
                steps=steps or {},
                fields=fields or {},
                create_if_missing=create_if_missing,
                record_event=record_event,
                event_type=event_type,
                event_title=event_title,
                event_detail=event_detail or {},
                level=level,
                event_source="ibkr_compute",
            )
            self._record_startup_progress_context(result)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            service_mod.logger.warning(
                "Startup progress sync failed (%s/%s): %s",
                action,
                title or current_step or "-",
                exc,
            )
            return {}

    def _announce_startup_pending(self, reason: str, source: str, trigger_login: bool):
        detail = self._build_startup_pending_detail(reason, source, trigger_login)
        self._sync_startup_progress(
            action="begin",
            title="IBKR Runtime 启动中",
            summary="IBKR Runtime 正在启动，交易链路暂未开放。",
            current_step="gateway",
            current_blocker="等待 Gateway 可用并完成首轮 Session 检查",
            operator_action="等待系统依次完成 Gateway、认证、订阅、线程与预热",
            fields=detail,
            reason=reason,
            source=source,
            trigger_login=trigger_login,
            record_event=True,
            event_type="status_change",
            event_title="IBKR Runtime 启动中",
            event_detail=detail,
            level="info",
        )

    def _format_symbol_list(self, symbols: list[str], limit: int = 12) -> str:
        items = [str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        if not items:
            return "-"
        if len(items) <= limit:
            return ",".join(items)
        return f"{','.join(items[:limit])} (+{len(items) - limit})"

    def _format_elapsed_seconds(self, elapsed_seconds: float) -> str:
        total_seconds = max(0, int(round(float(elapsed_seconds or 0))))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0 or hours > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    def _format_elapsed_between(self, started_at: str, finished_at: str) -> str:
        started_text = str(started_at or "").strip()
        finished_text = str(finished_at or "").strip()
        if not started_text or not finished_text:
            return "-"
        try:
            started_dt = datetime.fromisoformat(started_text)
            finished_dt = datetime.fromisoformat(finished_text)
        except Exception:
            return "-"
        elapsed_seconds = (finished_dt - started_dt).total_seconds()
        if elapsed_seconds < 0:
            return "-"
        return self._format_elapsed_seconds(elapsed_seconds)

    def clear_stale_startup_cycle(self, reason: str = "stale_cycle", source: str = "runtime_status") -> bool:
        startup = self.startup_progress_snapshot()
        if not startup.get("active"):
            return False
        if self._starting or self._running:
            return False
        fields = self._build_startup_progress_fields(
            startup.get("reason") or reason or "startup",
            startup.get("source") or source or "runtime_status",
            bool(startup.get("trigger_login")),
            {
                "状态结论": "旧的启动轮次已失效，系统已自动清理该轮次状态。",
                "清理原因": reason or "stale_cycle",
                "检查时间": self._now_et(),
            },
        )
        self._sync_startup_progress(
            action="abort",
            title="IBKR Runtime 启动轮次已清理",
            summary="旧启动轮次已失效，已自动清理，等待 Runtime 重新恢复。",
            current_step="runtime_resume",
            current_blocker="旧启动轮次已结束",
            operator_action="等待系统重新恢复 Runtime；如仍未恢复，再手动触发启动",
            steps={
                "runtime_resume": {
                    "status": "done",
                    "detail": "旧启动轮次已清理，不再阻塞当前运行态恢复。",
                },
            },
            fields=fields,
            reason=startup.get("reason") or reason or "startup",
            source=startup.get("source") or source or "runtime_status",
            trigger_login=bool(startup.get("trigger_login")),
            record_event=True,
            event_type="status_change",
            event_title="IBKR Runtime 清理陈旧启动轮次",
            event_detail=fields,
            level="warning",
            create_if_missing=False,
            allow_when_disabled=True,
        )
        return True

    def _get_time_window(self, key: str, default: tuple[int, int]) -> tuple[int, int]:
        service_mod = _service_mod()
        raw_value = str(
            self.config.get_for_environment(
                key,
                service_mod.ENVIRONMENT,
                f"{default[0]:02d}:{default[1]:02d}",
            )
            or ""
        ).strip()
        try:
            hour_text, minute_text = raw_value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return default

    def _trade_window_start(self) -> tuple[int, int]:
        service_mod = _service_mod()
        return self._get_time_window("trade_window_start_time", service_mod.DEFAULT_TRADE_WINDOW_START)

    def _trade_window_end(self) -> tuple[int, int]:
        service_mod = _service_mod()
        return self._get_time_window("trade_window_end_time", service_mod.DEFAULT_TRADE_WINDOW_END)

    def _expected_startup_today_regular_ms(self, et_now: datetime | None = None) -> int:
        service_mod = _service_mod()
        current_et = et_now or datetime.now(service_mod.ET)
        effective_et = current_et - timedelta(seconds=max(0, int(self._official_5m_close_delay_sec() or 0)))
        expected_ms = service_mod.interval_to_ms("5m")
        start_hour, start_minute = self._trade_window_start()
        end_hour, end_minute = self._trade_window_end()
        session_start = current_et.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        session_end = current_et.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        first_close_ready_at = session_start + timedelta(milliseconds=expected_ms)
        if session_end <= session_start or effective_et < first_close_ready_at:
            return 0
        capped_et = min(effective_et, session_end)
        capped_ms = int(capped_et.timestamp() * 1000)
        if capped_ms <= int(session_start.timestamp() * 1000):
            return 0
        return service_mod.bucket_start_ms(capped_ms - expected_ms, "5m")

    def _pop_startup_state(self) -> dict | None:
        with self._state_lock:
            if not self._starting:
                return None
            context = {
                "cycle_id": self._startup_cycle_id,
                "reason": self._startup_reason,
                "source": self._startup_source,
                "progress_enabled": self._startup_progress_enabled,
                "trigger_login": self._startup_trigger_login,
                "message_id": self._startup_status_message_id,
            }
            self._starting = False
            self._startup_progress_enabled = False
            self._startup_cycle_id = ""
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
            self._startup_status_message_id = ""
        return context

    def _release_startup_gate(
        self,
        reason: str,
        title: str,
        detail: dict | None = None,
        level: str = "warning",
    ) -> bool:
        startup_context = self._pop_startup_state()
        if not startup_context:
            return False

        normalized_reason = str(reason or "startup_released").strip()
        waiting_on_auth = normalized_reason == "session_unauthenticated"
        step_key = "auth" if waiting_on_auth else "gateway"
        step_detail = "等待手动触发 2FA 并恢复认证" if waiting_on_auth else "Gateway 当前不可用，等待恢复"
        current_blocker = "启动阶段已解除，当前进入恢复等待态。"
        operator_action = "去 2FA 卡片手动触发当前轮次" if waiting_on_auth else "检查 Gateway 状态并在恢复后重新启动"
        fields = self._build_startup_progress_fields(
            startup_context.get("reason") or reason or "startup",
            startup_context.get("source") or "api_start",
            bool(startup_context.get("trigger_login")),
            {
                "降级原因": normalized_reason,
                "状态结论": "IBKR Runtime 已离开启动态，但当前处于降级等待恢复状态。",
                **(detail or {}),
            },
        )
        self._sync_startup_progress(
            action="update",
            title="IBKR Runtime 启动中",
            summary="启动阶段已解除，但当前运行态处于等待恢复状态。",
            current_step=step_key,
            current_blocker=current_blocker,
            operator_action=operator_action,
            steps={
                step_key: {
                    "status": "failed" if not waiting_on_auth else "waiting",
                    "detail": step_detail,
                },
            },
            fields=fields,
            reason=startup_context.get("reason") or reason or "startup",
            source=startup_context.get("source") or "api_start",
            trigger_login=bool(startup_context.get("trigger_login")),
            record_event=True,
            event_type="status_change",
            event_title=title or "IBKR Runtime 启动态已解除",
            event_detail=fields,
            level=level,
            create_if_missing=False,
            allow_when_disabled=True,
        )
        return True

    def _collect_startup_history_repair_snapshot(
        self,
        symbol: str,
        et_now: datetime | None = None,
    ) -> dict:
        service_mod = _service_mod()
        normalized_symbol = str(symbol or "").strip().upper()
        snapshot = {
            "symbol": normalized_symbol,
            "interval": "5m",
            "stored_bar_count": 0,
            "latest_stored_ms": 0,
            "today_regular_count": 0,
            "today_regular_latest_ms": 0,
            "expected_today_regular_ms": 0,
            "today_gap_count": 0,
            "today_gap_examples": [],
            "needs_history_fetch": False,
            "needs_manual_review": False,
            "needs_pipeline_repair": False,
            "needs_repair": False,
            "safe_repair": False,
            "repair_reason": "",
            "integrity_status": "ok",
        }
        if not normalized_symbol:
            return snapshot

        min_bars = max(60, service_mod.indicator_ready_bar_count())
        expected_ms = service_mod.interval_to_ms("5m")
        bars_needed = max(400, min_bars + 20)
        max_pages = max(2, min(8, (bars_needed + 199) // 200))
        current_et = et_now or datetime.now(service_mod.ET)
        market_date = current_et.strftime("%Y-%m-%d")
        expected_today_regular_ms = self._expected_startup_today_regular_ms(current_et)
        require_today_regular = expected_today_regular_ms > 0
        freshness_tolerance_ms = max(expected_ms * 3, 15 * 60 * 1000)
        snapshot["expected_today_regular_ms"] = expected_today_regular_ms
        end_hour, end_minute = self._trade_window_end()
        session_end = current_et.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

        try:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    'interval = "5m" && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
                max_pages=max_pages,
            )
        except Exception as exc:
            snapshot["needs_history_fetch"] = True
            snapshot["needs_pipeline_repair"] = True
            snapshot["needs_repair"] = True
            snapshot["safe_repair"] = True
            snapshot["repair_reason"] = f"startup_snapshot_error:{exc}"
            snapshot["integrity_status"] = "warn"
            return snapshot

        if not rows:
            snapshot["needs_history_fetch"] = True
            snapshot["needs_pipeline_repair"] = True
            snapshot["needs_repair"] = True
            snapshot["safe_repair"] = True
            snapshot["repair_reason"] = f"bars<{min_bars}"
            snapshot["integrity_status"] = "warn"
            return snapshot

        rows = rows[:bars_needed]
        snapshot["stored_bar_count"] = len(rows)
        snapshot["latest_stored_ms"] = int((rows[0] or {}).get("bar_time_ms", 0) or 0)

        today_regular_rows = []
        for row in reversed(rows):
            bar_time_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_time_ms <= 0:
                continue
            row_dt = datetime.fromtimestamp(bar_time_ms / 1000, service_mod.ET)
            if row_dt.strftime("%Y-%m-%d") != market_date:
                continue
            session_type = str((row or {}).get("session_type", "") or "").strip().lower()
            if session_type != "regular":
                continue
            today_regular_rows.append(row)

        snapshot["today_regular_count"] = len(today_regular_rows)
        if today_regular_rows:
            snapshot["today_regular_latest_ms"] = int((today_regular_rows[-1] or {}).get("bar_time_ms", 0) or 0)

        today_gap_summary = service_mod._regular_session_gap_summary(
            today_regular_rows,
            "5m",
            same_day_only=False,
            example_limit=4,
        )
        snapshot["today_gap_count"] = int(today_gap_summary.get("gap_count", 0) or 0)
        snapshot["today_gap_examples"] = list(today_gap_summary.get("gap_examples") or [])

        reasons = []
        if snapshot["stored_bar_count"] < min_bars:
            reasons.append(f"bars<{min_bars}")
        if require_today_regular:
            latest_today_regular_ms = int(snapshot.get("today_regular_latest_ms", 0) or 0)
            if latest_today_regular_ms <= 0:
                reasons.append("today_regular_missing")
            elif latest_today_regular_ms < expected_today_regular_ms:
                missing_ms = max(0, expected_today_regular_ms - latest_today_regular_ms)
                missing_bars = max(1, int((missing_ms + expected_ms - 1) // expected_ms))
                stale_minutes = max(0, int(missing_ms // 60000))
                latest_bar_grace = (
                    missing_bars == 1
                    and int(snapshot.get("today_gap_count", 0) or 0) == 0
                    and current_et < session_end
                )
                if not latest_bar_grace:
                    reasons.append(f"today_regular_incomplete={missing_bars}")
                if (
                    not latest_bar_grace
                    and stale_minutes > 0
                    and stale_minutes > int(freshness_tolerance_ms // 60000)
                ):
                    reasons.append(f"today_regular_stale={stale_minutes}m")
        if int(snapshot.get("today_gap_count", 0) or 0) > 0:
            reasons.append(f"today_regular_gaps={int(snapshot.get('today_gap_count', 0) or 0)}")

        needs_repair = bool(reasons)
        snapshot["needs_history_fetch"] = needs_repair
        snapshot["needs_pipeline_repair"] = needs_repair
        snapshot["needs_repair"] = needs_repair
        snapshot["safe_repair"] = needs_repair
        snapshot["repair_reason"] = ",".join(reasons)
        snapshot["integrity_status"] = "warn" if needs_repair else "ok"
        return snapshot

    def _complete_startup_success(self, title: str, detail: dict | None = None) -> bool:
        startup_context = self._pop_startup_state()
        if not startup_context:
            return False

        payload = {
            "状态结论": "IBKR Runtime 已完成启动前回补与预热，当前服务可用。",
            "检查时间": self._now_et(),
            "Runtime阶段": self._runtime_phase_label(),
            "启动原因": startup_context.get("reason") or str((self._warmup_state or {}).get("reason") or "startup"),
            "启动来源": startup_context.get("source") or "api_start",
            "触发登录": "yes" if startup_context.get("trigger_login") else "no",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            payload["运行页"] = runtime_url
        if title and title != "IBKR Runtime 启动完成":
            payload["启动结果"] = title
        if detail:
            payload.update(detail)
        started_at = str(payload.get("预热开始") or "").strip()
        finished_at = str(payload.get("预热完成") or "").strip()
        if started_at and finished_at and "预热耗时" not in payload:
            payload["预热耗时"] = self._format_elapsed_between(started_at, finished_at)
        warmup_detail = "启动前 Warmup、预检修复与历史回补已完成。"
        if title and "后台继续预热" in title:
            warmup_detail = "交易门已开放，剩余 monitor / integrity repair 在后台继续。"
        elif title and "监控模式" in title:
            warmup_detail = "当前无 trade symbols，runtime 以监控模式继续运行。"
        self._sync_startup_progress(
            action="complete",
            title="IBKR Runtime 启动完成",
            summary=payload.get("状态结论", "IBKR Runtime 已完成启动前回补与预热，当前服务可用。"),
            current_step="trading_gate",
            current_blocker="全部阻塞步骤已完成",
            operator_action=str(payload.get("后续动作") or "可进入 Runtime 页面观察后续运行状态"),
            steps={
                "warmup": {
                    "status": "done",
                    "detail": warmup_detail,
                },
                "trading_gate": {
                    "status": "done",
                    "detail": f"交易门: {payload.get('交易门', 'open')}",
                },
            },
            fields=payload,
            reason=startup_context.get("reason") or str((self._warmup_state or {}).get("reason") or "startup"),
            source=startup_context.get("source") or "api_start",
            trigger_login=bool(startup_context.get("trigger_login")),
            record_event=True,
            event_type="status_change",
            event_title="IBKR Runtime 启动完成",
            event_detail=payload,
            level="info",
            create_if_missing=False,
            allow_when_disabled=True,
        )
        return True
