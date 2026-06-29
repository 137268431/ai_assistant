from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceAuthRecoveryMixin:
    POST_LOGIN_2FA_INTERRUPTION_KINDS = {"post_login_2fa", "manual_gateway_restart"}

    def _initial_auth_recovery_state(self) -> dict:
        return {
            "cycle_id": "",
            "recovery_phase": "idle",
            "recovery_class": "",
            "recovery_reason": "",
            "interruption_kind": "",
            "manual_takeover_active": False,
            "manual_takeover_started_at": "",
            "manual_takeover_until": "",
            "probe_started_at": "",
            "probe_last_checked_at": "",
            "probe_attempts": 0,
            "probe_result": "",
            "auto_restart_scheduled": False,
            "last_runtime_authenticated_at": "",
            "last_gateway_status_code": 0,
            "last_recovery_source": "",
            "disconnect_reason_code": "",
            "disconnect_reason_label": "",
            "disconnect_reason_confidence": "",
            "disconnect_reason_evidence": {},
            "lock_owner": "",
            "lock_expires_at": "",
            "auto_repair_attempts": 0,
            "auto_repair_last_at": "",
            "auto_repair_last_reason": "",
            "auto_repair_cooldown_until": "",
            "auto_repair_blocked_reason": "",
            "updated_at": "",
        }

    def _copy_auth_recovery_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._auth_recovery_state
        return dict(payload or {})

    @staticmethod
    def _normalize_recovery_value(value: str) -> str:
        return str(value or "").strip().lower()

    def _derive_auth_recovery_class(self, state: dict | None = None) -> str:
        payload = self._copy_auth_recovery_state(state)
        phase = self._normalize_recovery_value(payload.get("recovery_phase"))
        reason = self._normalize_recovery_value(payload.get("recovery_reason"))
        interruption_kind = self._normalize_recovery_value(payload.get("interruption_kind"))
        probe_result = self._normalize_recovery_value(payload.get("probe_result"))
        source = self._normalize_recovery_value(payload.get("last_recovery_source"))
        auto_restart_scheduled = bool(payload.get("auto_restart_scheduled"))

        if probe_result == "gateway_socket_unreachable":
            return "local_socket_unreachable"
        if (
            probe_result in {
                "fresh_probe_authenticated",
                "stale_broker_detected",
                "stale_broker_restart_failed",
                "stale_broker_restart_scheduled",
            }
            or reason == "stale_inprocess_broker"
        ):
            return "stale_broker"
        if phase == "requested" or probe_result in {
            "manual_trigger_required",
            "resume_probe_timeout",
            "timeout_after_self_heal",
        }:
            return "manual_auth_required"
        if interruption_kind in {"session_expired", "gateway_down"}:
            return "scheduled_restart"
        if reason in {"session_expired", "gateway_down"} and source in {"session_keeper", "gateway_down"}:
            return "scheduled_restart"
        if phase in {"idle", "recovered", "runtime_stopped"} and not auto_restart_scheduled:
            return ""
        return ""

    def _parse_iso_timestamp(self, value: str) -> datetime | None:
        service_mod = _service_mod()
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=service_mod.ET)
            return parsed.astimezone(service_mod.ET)
        except Exception:
            return None

    def _future_iso(self, offset_seconds: int) -> str:
        service_mod = _service_mod()
        return (datetime.now(service_mod.ET) + timedelta(seconds=max(0, int(offset_seconds or 0)))).isoformat()

    def _next_auth_cycle_id(self) -> str:
        with self._auth_recovery_lock:
            self._auth_cycle_seq += 1
            return f"{int(time.time() * 1000)}-{self._auth_cycle_seq}"

    def _auth_recovery_pb_patch(self, snapshot: dict | None = None) -> dict:
        service_mod = _service_mod()
        state = self._copy_auth_recovery_state(snapshot)
        patch = {}
        for key in service_mod.AUTH_RECOVERY_PB_FIELDS:
            patch[key] = state.get(key)
        return patch

    def _load_global_auth_state(self) -> dict:
        service_mod = _service_mod()
        if not self.pb:
            return {}
        try:
            record = self.pb.get_state("ibkr_2fa", service_mod.ENVIRONMENT, date="global") or {}
            data = record.get("data") if isinstance(record, dict) else {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            service_mod.logger.debug("Failed to load global ibkr_2fa state: %s", exc)
            return {}

    def _sync_auth_recovery_state_to_pb(self, snapshot: dict | None = None):
        service_mod = _service_mod()
        if not self.pb:
            return
        try:
            current = self._load_global_auth_state()
            payload = {
                **current,
                **self._auth_recovery_pb_patch(snapshot),
            }
            if not payload.get("status"):
                payload["status"] = "requested"
            self.pb.upsert_state("ibkr_2fa", service_mod.ENVIRONMENT, payload, date="global")
        except Exception as exc:
            service_mod.logger.debug("Failed to sync auth recovery state to PB: %s", exc)

    def _set_auth_recovery_state(self, sync_pb: bool = True, **updates) -> dict:
        with self._auth_recovery_lock:
            next_state = self._copy_auth_recovery_state()
            next_state.update(updates)
            if "recovery_class" not in updates or updates.get("recovery_class") is None:
                next_state["recovery_class"] = self._derive_auth_recovery_class(next_state)
            next_state["updated_at"] = self._now_iso()
            if not next_state.get("manual_takeover_active"):
                next_state["manual_takeover_started_at"] = ""
                next_state["manual_takeover_until"] = ""
            self._auth_recovery_state = next_state
            snapshot = self._copy_auth_recovery_state(next_state)
        if sync_pb:
            self._sync_auth_recovery_state_to_pb(snapshot)
        return snapshot

    def _manual_takeover_active(self, snapshot: dict | None = None) -> bool:
        service_mod = _service_mod()
        state = self._copy_auth_recovery_state(snapshot)
        if not bool(state.get("manual_takeover_active")):
            return False
        until_dt = self._parse_iso_timestamp(state.get("manual_takeover_until", ""))
        if until_dt and until_dt <= datetime.now(service_mod.ET):
            self._set_auth_recovery_state(
                manual_takeover_active=False,
                manual_takeover_started_at="",
                manual_takeover_until="",
            )
            return False
        return True

    def _is_server_boot_resume_recovery(
        self,
        interruption_kind: str = "",
        recovery_reason: str = "",
        source: str = "",
        recovery_phase: str = "",
    ) -> bool:
        kind_key = str(interruption_kind or "").strip().lower()
        reason_key = str(recovery_reason or "").strip().lower()
        source_key = str(source or "").strip().lower()
        phase_key = str(recovery_phase or "").strip().lower()
        if phase_key == "resume_waiting_manual":
            return True
        if kind_key == "server_boot_resume":
            return True
        return reason_key == "auto_restore" and source_key == "server_boot"

    def _auth_probe_window_seconds(self, interruption_kind: str) -> int:
        service_mod = _service_mod()
        default_window = max(service_mod.AUTH_PROBE_INTERVAL_SECONDS, int(service_mod.AUTH_PROBE_WINDOW_SECONDS or 0))
        if str(interruption_kind or "").strip().lower() in self.POST_LOGIN_2FA_INTERRUPTION_KINDS:
            return max(default_window, int(getattr(service_mod, "AUTH_POST_LOGIN_PROBE_WINDOW_SECONDS", 240) or 240))
        if self._auth_probe_in_late_session():
            default_window = max(
                default_window,
                int(service_mod.AUTH_PROBE_LATE_SESSION_WINDOW_SECONDS or 0),
            )
        if self._is_server_boot_resume_recovery(interruption_kind=interruption_kind):
            return max(default_window, 300)
        return default_window

    def _auth_probe_self_heal_grace_seconds(self, interruption_kind: str) -> int:
        service_mod = _service_mod()
        grace_seconds = max(
            service_mod.AUTH_PROBE_INTERVAL_SECONDS,
            int(service_mod.AUTH_PROBE_SELF_HEAL_GRACE_SECONDS or 0),
        )
        if str(interruption_kind or "").strip().lower() in self.POST_LOGIN_2FA_INTERRUPTION_KINDS:
            return max(grace_seconds, int(getattr(service_mod, "AUTH_POST_LOGIN_PROBE_GRACE_SECONDS", 180) or 180))
        if self._auth_probe_in_late_session():
            grace_seconds = max(
                grace_seconds,
                int(service_mod.AUTH_PROBE_LATE_SESSION_SELF_HEAL_GRACE_SECONDS or 0),
            )
        if self._is_server_boot_resume_recovery(interruption_kind=interruption_kind):
            grace_seconds = max(grace_seconds, 300)
        return grace_seconds

    def _auth_probe_market_session_kind(self) -> str:
        service_mod = _service_mod()
        try:
            return str(service_mod.classify_market_session_kind()).strip().lower()
        except Exception:
            return ""

    def _auth_probe_in_late_session(self) -> bool:
        return self._auth_probe_market_session_kind() in {"close_transition", "afterhours", "overnight"}

    @staticmethod
    def _gateway_socket_unreachable(status: dict | None, status_code: int | None = None) -> bool:
        payload = status if isinstance(status, dict) else {}
        code = int(status_code if status_code is not None else payload.get("status_code") or 0)
        return code in {502, 504} or payload.get("api_socket_listening") is False

    def _auto_repair_cooldown_active(self, state: dict | None = None) -> bool:
        service_mod = _service_mod()
        until_dt = self._parse_iso_timestamp((state or self._auth_recovery_state).get("auto_repair_cooldown_until", ""))
        return bool(until_dt and until_dt > datetime.now(service_mod.ET))

    def _gateway_socket_auto_repair_candidate(self, gateway_status: dict | None, session_status: dict | None) -> tuple[bool, str]:
        service_mod = _service_mod()
        gateway = gateway_status if isinstance(gateway_status, dict) else {}
        session = session_status if isinstance(session_status, dict) else {}
        if bool(session.get("authenticated")):
            return False, "session_authenticated"
        running = bool(gateway.get("running") or gateway.get("gateway_running"))
        if not running:
            return False, "gateway_not_running"
        code = int(gateway.get("status_code") or 0)
        api_socket_listening = gateway.get("api_socket_listening")
        unreachable = self._gateway_socket_unreachable(gateway, code)
        if not unreachable:
            return False, "gateway_socket_reachable"
        uptime_s = int(gateway.get("uptime_s") or 0)
        min_uptime_s = int(getattr(service_mod, "AUTH_2FA_AUTO_REPAIR_SOCKET_GRACE_SECONDS", 240) or 240)
        if uptime_s and uptime_s < min_uptime_s:
            return False, "gateway_socket_grace"
        if api_socket_listening is False:
            return True, "gateway_socket_not_listening"
        if code in {502, 504}:
            return True, "gateway_socket_unreachable"
        return False, "gateway_socket_not_confirmed"

    def maybe_auto_repair_gateway_socket(
        self,
        *,
        gateway_status: dict | None = None,
        session_status: dict | None = None,
        market_data_session_conflict: dict | None = None,
        source: str = "status_watchdog",
    ) -> dict:
        service_mod = _service_mod()
        current = self._copy_auth_recovery_state()
        if not bool(getattr(service_mod, "AUTH_2FA_AUTO_REPAIR_ENABLED", True)):
            return current
        if bool((market_data_session_conflict or {}).get("active")):
            return self._set_auth_recovery_state(auto_repair_blocked_reason="market_data_session_conflict")
        candidate, reason = self._gateway_socket_auto_repair_candidate(gateway_status, session_status)
        if not candidate:
            if reason not in {"gateway_socket_reachable", "session_authenticated"}:
                return self._set_auth_recovery_state(auto_repair_blocked_reason=reason)
            return current
        if self._manual_takeover_active(current):
            return self._set_auth_recovery_state(auto_repair_blocked_reason="manual_takeover")
        phase = self._normalize_recovery_value(current.get("recovery_phase"))
        lock_owner = self._normalize_recovery_value(current.get("lock_owner"))
        if phase in {"panic_resetting", "starting_runtime"} or lock_owner in {"panic_reset", "runtime_start"}:
            return self._set_auth_recovery_state(auto_repair_blocked_reason="repair_already_running")
        if self._auth_restart_thread and self._auth_restart_thread.is_alive():
            return self._set_auth_recovery_state(auto_repair_blocked_reason="restart_already_running")
        if self._auto_repair_cooldown_active(current):
            return self._set_auth_recovery_state(auto_repair_blocked_reason="cooldown")
        max_attempts = int(getattr(service_mod, "AUTH_2FA_AUTO_REPAIR_MAX_ATTEMPTS", 2) or 0)
        attempts = int(current.get("auto_repair_attempts") or 0)
        if max_attempts <= 0 or attempts >= max_attempts:
            return self._set_auth_recovery_state(auto_repair_blocked_reason="attempt_limit")

        next_attempts = attempts + 1
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="panic_resetting",
            recovery_reason="gateway_socket_unreachable_auto_repair",
            interruption_kind="gateway_socket_unreachable",
            manual_takeover_active=False,
            probe_last_checked_at=self._now_iso(),
            probe_result="auto_repair_scheduled",
            auto_restart_scheduled=True,
            last_gateway_status_code=int((gateway_status or {}).get("status_code") or 0),
            last_recovery_source=source or "status_watchdog",
            disconnect_reason_code="local_socket_unreachable",
            disconnect_reason_label="本地 Gateway Socket 不可达",
            disconnect_reason_confidence="high",
            lock_owner="panic_reset",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
            auto_repair_attempts=next_attempts,
            auto_repair_last_at=self._now_iso(),
            auto_repair_last_reason=reason,
            auto_repair_cooldown_until=self._future_iso(service_mod.AUTH_2FA_AUTO_REPAIR_COOLDOWN_SECONDS),
            auto_repair_blocked_reason="",
        )

        def worker():
            try:
                service_mod.logger.warning(
                    "Auto repairing IBKR Gateway socket by panic reset: reason=%s attempt=%s/%s",
                    reason,
                    next_attempts,
                    max_attempts,
                )
                self.panic_reset_auth(
                    restart_gateway=True,
                    restart_runtime=True,
                    trigger_login=True,
                    reason="gateway_socket_unreachable_auto_repair",
                    source=source or "status_watchdog",
                )
            except Exception as exc:
                service_mod.logger.error("Gateway socket auto repair failed: %s", exc)
                self._set_auth_recovery_state(
                    recovery_phase="failed",
                    recovery_reason="gateway_socket_unreachable_auto_repair",
                    probe_result="auto_repair_failed",
                    auto_restart_scheduled=False,
                    last_recovery_source=source or "status_watchdog",
                    lock_owner="",
                    lock_expires_at="",
                    auto_repair_blocked_reason=str(exc),
                )

        threading.Thread(target=worker, daemon=True, name="gateway-socket-auto-repair").start()
        return snapshot

    def _wait_for_server_boot_resume_auth(
        self,
        cycle_id: str,
        recovery_reason: str,
        source: str,
        attempts: int,
    ) -> tuple[bool, int]:
        service_mod = _service_mod()
        passive_interval = max(30, service_mod.AUTH_PROBE_INTERVAL_SECONDS * 6)
        while not self._auth_probe_stop.is_set():
            current = self._copy_auth_recovery_state()
            if str(current.get("cycle_id") or "") != cycle_id:
                return False, attempts
            if self._starting or self._running:
                service_mod.logger.info(
                    "Server boot resume passive probe exiting because runtime already moved forward"
                )
                return False, attempts
            attempts += 1
            authenticated = False
            gateway_status = {}
            gateway_status_code = 0
            try:
                auth_payload = self.session_keeper.check_auth_status()
                authenticated = bool(auth_payload.get("authenticated", False))
            except Exception as exc:
                service_mod.logger.debug("Server boot passive auth probe failed: %s", exc)
            try:
                gateway_status = self.gateway_manager.status()
                gateway_status_code = int(gateway_status.get("status_code") or 0)
            except Exception:
                gateway_status = {}
                gateway_status_code = 0
            self._set_auth_recovery_state(
                cycle_id=cycle_id,
                recovery_phase="resume_waiting_manual",
                recovery_class="local_socket_unreachable" if self._gateway_socket_unreachable(gateway_status, gateway_status_code) else None,
                interruption_kind="server_boot_resume",
                recovery_reason=recovery_reason,
                probe_started_at=current.get("probe_started_at") or self._now_iso(),
                probe_last_checked_at=self._now_iso(),
                probe_attempts=attempts,
                probe_result=(
                    "authenticated"
                    if authenticated
                    else (
                        "gateway_socket_unreachable"
                        if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                        else "resume_probe_timeout"
                    )
                ),
                auto_restart_scheduled=False,
                last_gateway_status_code=gateway_status_code,
                last_recovery_source=source,
                disconnect_reason_code=(
                    "local_socket_unreachable"
                    if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                    else current.get("disconnect_reason_code", "")
                ),
                disconnect_reason_label=(
                    "本地 Gateway Socket 不可达"
                    if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                    else current.get("disconnect_reason_label", "")
                ),
                disconnect_reason_confidence=(
                    "high"
                    if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                    else current.get("disconnect_reason_confidence", "")
                ),
                lock_owner="",
                lock_expires_at="",
                manual_takeover_active=False,
            )
            if authenticated:
                self._mark_auth_recovered(
                    source="server_boot_resume_probe",
                    reason=recovery_reason or source,
                )
                return True, attempts
            if self._auth_probe_stop.wait(timeout=passive_interval):
                return False, attempts
        return False, attempts

    def _mark_auth_recovered(self, source: str, reason: str = ""):
        service_mod = _service_mod()
        stamp = self._now_iso()
        current = self._copy_auth_recovery_state()
        previous_phase = str(current.get("recovery_phase") or "")
        restart_reason = str(current.get("recovery_reason") or reason or "auto_restore")
        restart_source = str(current.get("last_recovery_source") or source or "server_boot")
        resume_recovery = self._is_server_boot_resume_recovery(
            interruption_kind=str(current.get("interruption_kind") or ""),
            recovery_reason=restart_reason,
            source=restart_source,
            recovery_phase=previous_phase,
        )
        snapshot = self._set_auth_recovery_state(
            cycle_id=current.get("cycle_id") or self._next_auth_cycle_id(),
            recovery_phase="recovered",
            recovery_reason=reason or current.get("recovery_reason") or source,
            interruption_kind="",
            manual_takeover_active=False,
            probe_last_checked_at=stamp,
            probe_result="authenticated",
            auto_restart_scheduled=False,
            last_runtime_authenticated_at=stamp,
            last_recovery_source=source,
            disconnect_reason_code="",
            disconnect_reason_label="",
            disconnect_reason_confidence="",
            disconnect_reason_evidence={},
            lock_owner="",
            lock_expires_at="",
        )
        self._auth_required_reason = ""
        if previous_phase and previous_phase not in {"idle", "recovered"}:
            try:
                self.auth_handler._report_2fa_status(
                    status="success",
                    reason=reason or "auth_recovered",
                    source=source,
                    detail=self._build_2fa_detail(reason or source),
                    message="IBKR 会话已恢复认证。",
                    last_result="会话恢复成功。",
                    state_patch=self._auth_recovery_pb_patch(snapshot),
                )
            except Exception as exc:
                service_mod.logger.debug("Failed to report auth recovery success: %s", exc)
        if resume_recovery and self._starting:
            service_mod.logger.info(
                "Skip auth recovery restart because runtime startup is already in progress"
            )
        elif resume_recovery and not self._running:
            scheduled = self._schedule_auth_restart(
                reason=restart_reason or "auto_restore",
                source=restart_source or "server_boot",
                trigger_login=False,
            )
            if scheduled:
                self._set_auth_recovery_state(
                    cycle_id=snapshot.get("cycle_id") or current.get("cycle_id") or self._next_auth_cycle_id(),
                    recovery_phase="recovered",
                    recovery_reason=restart_reason or "auto_restore",
                    probe_result="authenticated_resume_restart_scheduled",
                    auto_restart_scheduled=True,
                    last_recovery_source=restart_source or "server_boot",
                )
        elif self._should_restart_runtime_after_auth_recovered(
            previous_phase=previous_phase,
            recovery_reason=restart_reason or reason,
            recovery_source=restart_source or source,
        ):
            scheduled = self._schedule_auth_restart(
                reason=restart_reason or "auth_recovered",
                source=restart_source or source or "auth_probe",
                trigger_login=False,
            )
            if scheduled:
                self._set_auth_recovery_state(
                    cycle_id=snapshot.get("cycle_id") or current.get("cycle_id") or self._next_auth_cycle_id(),
                    recovery_phase="recovered",
                    recovery_reason=restart_reason or "auth_recovered",
                    probe_result="authenticated_runtime_restart_scheduled",
                    auto_restart_scheduled=True,
                    last_recovery_source=restart_source or source or "auth_probe",
                )

    def _fresh_broker_auth_probe(self, reason: str) -> dict:
        service_mod = _service_mod()
        try:
            payload = self.broker.fresh_health_probe(reason=reason)
            return dict(payload or {})
        except Exception as exc:
            service_mod.logger.warning("Fresh broker auth probe failed: %s", exc)
            return {}

    def _handle_stale_broker_detected(
        self,
        *,
        cycle_id: str,
        interruption_kind: str,
        recovery_reason: str,
        current: dict,
        gateway_status_code: int,
        attempts: int,
        fresh_probe_payload: dict | None,
    ) -> tuple[bool, int]:
        service_mod = _service_mod()
        payload = dict(fresh_probe_payload or {})
        probe_client_id = int(payload.get("probe_client_id", 0) or 0)
        gateway_status_code = int(payload.get("status_code", 0) or gateway_status_code or 0)
        service_mod.logger.warning(
            "Fresh broker probe authenticated while runtime broker remained stale; scheduling runtime restart: "
            "cycle=%s probe_client_id=%s reason=%s",
            cycle_id,
            probe_client_id or 0,
            recovery_reason or "-",
        )
        scheduled = False
        try:
            scheduled = self._schedule_auth_restart(
                reason="stale_inprocess_broker",
                source="auth_probe_fresh_broker",
                trigger_login=False,
            )
        except Exception as exc:
            service_mod.logger.error("Failed to schedule stale broker runtime restart: %s", exc)
        self._auth_required_reason = ""
        self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="silent_probe" if scheduled else "failed",
            recovery_class="stale_broker",
            interruption_kind=interruption_kind,
            recovery_reason="stale_inprocess_broker",
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_attempts=attempts,
            probe_result="stale_broker_restart_scheduled" if scheduled else "stale_broker_restart_failed",
            auto_restart_scheduled=bool(scheduled),
            last_gateway_status_code=gateway_status_code,
            last_recovery_source="auth_probe_fresh_broker",
            lock_owner="auth_probe" if scheduled else "",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS) if scheduled else "",
            manual_takeover_active=False,
        )
        if not scheduled:
            service_mod.logger.error(
                "Fresh broker probe authenticated but runtime restart could not be scheduled; manual 2FA suppressed"
            )
        return True, attempts

    def _attempt_auth_probe_self_heal(
        self,
        cycle_id: str,
        interruption_kind: str,
        recovery_reason: str,
        source: str,
        attempts: int,
    ) -> tuple[bool, int]:
        service_mod = _service_mod()
        attempts = max(0, int(attempts or 0))
        current = self._copy_auth_recovery_state()
        if str(current.get("cycle_id") or "") != cycle_id or self._auth_probe_stop.is_set():
            return False, attempts
        if not self.gateway_manager.is_running:
            service_mod.logger.warning(
                "Auth probe timeout with gateway service down; skipping self-heal and escalating: cycle=%s",
                cycle_id,
            )
            return False, attempts

        service_mod.logger.warning(
            "Auth probe timeout; attempting forced broker reconnect before manual 2FA: cycle=%s reason=%s source=%s",
            cycle_id,
            recovery_reason or "-",
            source or "-",
        )
        gateway_status_code = 0
        try:
            gateway_status_code = int(self.gateway_manager.status().get("status_code") or 0)
        except Exception:
            gateway_status_code = 0
        self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="manual_takeover" if self._manual_takeover_active(current) else "silent_probe",
            interruption_kind=interruption_kind,
            recovery_reason=recovery_reason,
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_attempts=attempts,
            probe_result="self_heal",
            last_gateway_status_code=gateway_status_code,
            last_recovery_source=source,
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )

        authenticated = False
        try:
            reconnect_payload = self.broker.force_reconnect(reason="auth_probe_timeout")
            authenticated = bool(reconnect_payload.get("authenticated") or reconnect_payload.get("ready"))
            gateway_status_code = int(reconnect_payload.get("status_code") or gateway_status_code or 0)
        except Exception as exc:
            service_mod.logger.warning("Forced broker reconnect during auth probe failed: %s", exc)

        attempts += 1
        self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="manual_takeover" if self._manual_takeover_active() else "silent_probe",
            interruption_kind=interruption_kind,
            recovery_reason=recovery_reason,
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_attempts=attempts,
            probe_result="authenticated" if authenticated else "self_heal_pending",
            last_gateway_status_code=gateway_status_code,
            last_recovery_source=source,
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        if authenticated:
            try:
                self.session_keeper.check_auth_status()
            except Exception:
                service_mod.logger.debug("Post-reconnect auth refresh failed", exc_info=True)
            self._mark_auth_recovered(source="auth_probe_self_heal", reason=recovery_reason or source)
            return True, attempts

        fresh_probe_payload = self._fresh_broker_auth_probe("auth_probe_timeout")
        if bool(fresh_probe_payload.get("authenticated") or fresh_probe_payload.get("ready")):
            attempts += 1
            return self._handle_stale_broker_detected(
                cycle_id=cycle_id,
                interruption_kind=interruption_kind,
                recovery_reason=recovery_reason,
                current=current,
                gateway_status_code=gateway_status_code,
                attempts=attempts,
                fresh_probe_payload=fresh_probe_payload,
            )

        grace_deadline = time.time() + self._auth_probe_self_heal_grace_seconds(interruption_kind)
        while not self._auth_probe_stop.is_set() and time.time() < grace_deadline:
            current = self._copy_auth_recovery_state()
            if str(current.get("cycle_id") or "") != cycle_id:
                return False, attempts
            attempts += 1
            authenticated = False
            try:
                auth_payload = self.session_keeper.check_auth_status()
                authenticated = bool(auth_payload.get("authenticated", False))
            except Exception as exc:
                service_mod.logger.debug("Auth self-heal probe failed: %s", exc)
            try:
                gateway_status_code = int(self.gateway_manager.status().get("status_code") or 0)
            except Exception:
                gateway_status_code = 0
            self._set_auth_recovery_state(
                cycle_id=cycle_id,
                recovery_phase="manual_takeover" if self._manual_takeover_active(current) else "silent_probe",
                interruption_kind=interruption_kind,
                recovery_reason=recovery_reason,
                probe_started_at=current.get("probe_started_at") or self._now_iso(),
                probe_last_checked_at=self._now_iso(),
                probe_attempts=attempts,
                probe_result="authenticated" if authenticated else "self_heal_pending",
                last_gateway_status_code=gateway_status_code,
                last_recovery_source=source,
                lock_owner="auth_probe",
                lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
            )
            if authenticated:
                self._mark_auth_recovered(source="auth_probe_self_heal", reason=recovery_reason or source)
                return True, attempts
            remaining = max(0.0, grace_deadline - time.time())
            if remaining <= 0:
                break
            if self._auth_probe_stop.wait(timeout=min(service_mod.AUTH_PROBE_INTERVAL_SECONDS, remaining)):
                return False, attempts
        fresh_probe_payload = self._fresh_broker_auth_probe("auth_probe_grace_exhausted")
        if bool(fresh_probe_payload.get("authenticated") or fresh_probe_payload.get("ready")):
            attempts += 1
            current = self._copy_auth_recovery_state()
            return self._handle_stale_broker_detected(
                cycle_id=cycle_id,
                interruption_kind=interruption_kind,
                recovery_reason=recovery_reason,
                current=current,
                gateway_status_code=gateway_status_code,
                attempts=attempts,
                fresh_probe_payload=fresh_probe_payload,
            )
        return False, attempts

    def _should_restart_runtime_after_auth_recovered(
        self,
        *,
        previous_phase: str,
        recovery_reason: str,
        recovery_source: str,
    ) -> bool:
        if self._running or self._starting:
            return False
        phase_key = self._normalize_recovery_value(previous_phase)
        reason_key = self._normalize_recovery_value(recovery_reason)
        source_key = self._normalize_recovery_value(recovery_source)
        startup_active = False
        try:
            startup_active = bool(self.startup_progress_snapshot().get("active"))
        except Exception:
            startup_active = False
        if startup_active and phase_key in {"requested", "silent_probe", "manual_takeover"}:
            return True
        manual_reasons = {
            "manual_start",
            "manual_reauth",
            "weekly_reauth",
            "manual_gateway_restart",
            "panic_reset_2fa",
        }
        manual_sources = {"api_start", "runtime_page", "feishu_callback", "feishu_2fa"}
        return phase_key in {"requested", "silent_probe", "manual_takeover"} and (
            reason_key in manual_reasons or source_key in manual_sources
        )

    def _ensure_auth_probe(self, cycle_id: str, interruption_kind: str, recovery_reason: str, source: str):
        service_mod = _service_mod()
        with self._auth_recovery_lock:
            thread = self._auth_probe_thread
            if thread and thread.is_alive():
                return False
            self._auth_probe_stop.clear()

            def worker():
                started_perf = time.time()
                attempts = 0
                probe_window_seconds = self._auth_probe_window_seconds(interruption_kind)
                try:
                    while not self._auth_probe_stop.is_set():
                        current = self._copy_auth_recovery_state()
                        if str(current.get("cycle_id") or "") != cycle_id:
                            return
                        attempts += 1
                        authenticated = False
                        gateway_status = {}
                        gateway_status_code = 0
                        try:
                            auth_payload = self.session_keeper.check_auth_status()
                            authenticated = bool(auth_payload.get("authenticated", False))
                        except Exception as exc:
                            service_mod.logger.debug("Auth probe auth check failed: %s", exc)
                        try:
                            gateway_status = self.gateway_manager.status()
                            gateway_status_code = int(gateway_status.get("status_code") or 0)
                        except Exception:
                            gateway_status = {}
                            gateway_status_code = 0
                        phase = "manual_takeover" if self._manual_takeover_active(current) else (
                            "resume_waiting_manual"
                            if self._is_server_boot_resume_recovery(
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                source=source,
                                recovery_phase=str(current.get("recovery_phase") or ""),
                            ) and str(current.get("recovery_phase") or "").strip().lower() == "resume_waiting_manual"
                            else "silent_probe"
                        )
                        self._set_auth_recovery_state(
                            cycle_id=cycle_id,
                            recovery_phase=phase,
                            interruption_kind=interruption_kind,
                            recovery_reason=recovery_reason,
                            probe_started_at=current.get("probe_started_at") or self._now_iso(),
                            probe_last_checked_at=self._now_iso(),
                            probe_attempts=attempts,
                            probe_result="authenticated" if authenticated else (
                                "gateway_socket_unreachable"
                                if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                else ("resume_probe_timeout" if phase == "resume_waiting_manual" else "pending")
                            ),
                            last_gateway_status_code=gateway_status_code,
                            last_recovery_source=source,
                            disconnect_reason_code=(
                                "local_socket_unreachable"
                                if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                else current.get("disconnect_reason_code", "")
                            ),
                            disconnect_reason_label=(
                                "本地 Gateway Socket 不可达"
                                if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                else current.get("disconnect_reason_label", "")
                            ),
                            disconnect_reason_confidence=(
                                "high"
                                if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                else current.get("disconnect_reason_confidence", "")
                            ),
                            lock_owner="" if phase == "resume_waiting_manual" else "auth_probe",
                            lock_expires_at="" if phase == "resume_waiting_manual" else self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
                        )
                        if authenticated:
                            self._mark_auth_recovered(source="auth_probe", reason=recovery_reason or source)
                            return
                        if (time.time() - started_perf) >= probe_window_seconds:
                            if self._manual_takeover_active(current):
                                self._set_auth_recovery_state(
                                    cycle_id=cycle_id,
                                    recovery_phase="manual_takeover",
                                    interruption_kind=interruption_kind,
                                    recovery_reason=recovery_reason,
                                    probe_last_checked_at=self._now_iso(),
                                    probe_attempts=attempts,
                                    probe_result="manual_takeover_waiting",
                                    auto_restart_scheduled=False,
                                    last_recovery_source=source,
                                    lock_owner="manual_takeover",
                                    lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
                                )
                                if self._auth_probe_stop.wait(timeout=service_mod.AUTH_PROBE_INTERVAL_SECONDS):
                                    return
                                continue
                            recovered, attempts = self._attempt_auth_probe_self_heal(
                                cycle_id=cycle_id,
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                source=source,
                                attempts=attempts,
                            )
                            current = self._copy_auth_recovery_state()
                            if recovered:
                                return
                            if self._auth_probe_stop.is_set() or str(current.get("cycle_id") or "") != cycle_id:
                                return
                            if self._manual_takeover_active(current):
                                continue
                            if self._is_server_boot_resume_recovery(
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                source=source,
                            ):
                                service_mod.logger.warning(
                                    "Server boot resume auth probe timed out; keeping runtime in silent-resume wait without auto 2FA"
                                )
                                self._set_auth_recovery_state(
                                    cycle_id=cycle_id,
                                    recovery_phase="resume_waiting_manual",
                                    interruption_kind=interruption_kind,
                                    recovery_reason=recovery_reason,
                                    probe_last_checked_at=self._now_iso(),
                                    probe_attempts=attempts,
                                    probe_result=(
                                        "gateway_socket_unreachable"
                                        if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                        else "resume_probe_timeout"
                                    ),
                                    auto_restart_scheduled=False,
                                    last_gateway_status_code=gateway_status_code,
                                    last_recovery_source=source,
                                    disconnect_reason_code=(
                                        "local_socket_unreachable"
                                        if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                        else current.get("disconnect_reason_code", "")
                                    ),
                                    disconnect_reason_label=(
                                        "本地 Gateway Socket 不可达"
                                        if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                        else current.get("disconnect_reason_label", "")
                                    ),
                                    disconnect_reason_confidence=(
                                        "high"
                                        if self._gateway_socket_unreachable(gateway_status, gateway_status_code)
                                        else current.get("disconnect_reason_confidence", "")
                                    ),
                                    lock_owner="",
                                    lock_expires_at="",
                                    manual_takeover_active=False,
                                )
                                self._wait_for_server_boot_resume_auth(
                                    cycle_id=cycle_id,
                                    recovery_reason=recovery_reason,
                                    source=source,
                                    attempts=attempts,
                                )
                                return
                            self._set_auth_recovery_state(
                                cycle_id=cycle_id,
                                recovery_phase="requested",
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                probe_last_checked_at=self._now_iso(),
                                probe_attempts=attempts,
                                probe_result="timeout_after_self_heal",
                                auto_restart_scheduled=False,
                                last_recovery_source=source,
                                lock_owner="",
                                lock_expires_at="",
                                manual_takeover_active=False,
                            )
                            self._request_manual_2fa(
                                recovery_reason or "auth_probe_timeout",
                                "会话未在静默探测与本地重连窗口内自动恢复，请在飞书 2FA 卡片手动触发当前轮次。",
                            )
                            return
                        if self._auth_probe_stop.wait(timeout=service_mod.AUTH_PROBE_INTERVAL_SECONDS):
                            return
                finally:
                    with self._auth_recovery_lock:
                        if threading.current_thread() is self._auth_probe_thread:
                            self._auth_probe_thread = None

            self._auth_probe_thread = threading.Thread(
                target=worker,
                daemon=True,
                name="auth-probe",
            )
            self._auth_probe_thread.start()
            return True

    def _schedule_auth_restart(
        self,
        reason: str,
        source: str,
        trigger_login: bool = False,
        *,
        allow_panic_reset: bool = False,
    ):
        service_mod = _service_mod()
        with self._auth_recovery_lock:
            thread = self._auth_restart_thread
            if thread and thread.is_alive():
                return False
            if self._starting:
                service_mod.logger.info("Skip auth recovery restart while runtime startup is in progress")
                return False
            current = self._copy_auth_recovery_state()
            current_phase = str(current.get("recovery_phase") or "")
            current_lock_owner = str(current.get("lock_owner") or "")
            if self._manual_takeover_active(current):
                service_mod.logger.info("Skip auth recovery restart during manual takeover")
                return False
            blocked_phases = {"starting_runtime"}
            blocked_lock_owners = {"runtime_start"}
            if not allow_panic_reset:
                blocked_phases.add("panic_resetting")
                blocked_lock_owners.add("panic_reset")
            if current_phase in blocked_phases or current_lock_owner in blocked_lock_owners:
                service_mod.logger.info(
                    "Skip auth recovery restart while phase=%s lock_owner=%s",
                    current_phase or "-",
                    current_lock_owner or "-",
                )
                return False

            def worker():
                try:
                    service_mod.logger.warning("Auth recovery restart scheduled: reason=%s source=%s", reason, source)
                    self.stop()
                    time.sleep(2)
                    self.start(
                        trigger_login=bool(trigger_login),
                        reason=reason or "auth_recovery_auto_restart",
                        source=source or "auth_recovery",
                    )
                except Exception as exc:
                    service_mod.logger.error("Auth recovery restart failed: %s", exc)
                    self._set_auth_recovery_state(
                        recovery_phase="failed",
                        recovery_reason=reason or "auth_recovery_auto_restart",
                        probe_result="restart_failed",
                        auto_restart_scheduled=False,
                        last_recovery_source=source or "auth_recovery",
                        lock_owner="",
                        lock_expires_at="",
                    )
                finally:
                    with self._auth_recovery_lock:
                        if threading.current_thread() is self._auth_restart_thread:
                            self._auth_restart_thread = None

            self._auth_restart_thread = threading.Thread(
                target=worker,
                daemon=True,
                name="auth-restart",
            )
            self._auth_restart_thread.start()
            return True

    def _start_auth_recovery(
        self,
        interruption_kind: str,
        recovery_reason: str,
        source: str = "runtime",
        disconnect_classification: dict | None = None,
    ):
        service_mod = _service_mod()
        current = self._copy_auth_recovery_state()
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        phase = "manual_takeover" if self._manual_takeover_active(current) else "silent_probe"
        classification = disconnect_classification if isinstance(disconnect_classification, dict) else {}
        classification_evidence = classification.get("evidence")
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase=phase,
            recovery_reason=recovery_reason,
            interruption_kind=interruption_kind,
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_attempts=int(current.get("probe_attempts") or 0),
            probe_result="pending",
            auto_restart_scheduled=False,
            last_gateway_status_code=int(self.gateway_manager.status().get("status_code") or 0),
            last_recovery_source=source,
            disconnect_reason_code=str(classification.get("reason_code") or ""),
            disconnect_reason_label=str(classification.get("reason_label") or ""),
            disconnect_reason_confidence=str(classification.get("confidence") or ""),
            disconnect_reason_evidence=classification_evidence if isinstance(classification_evidence, dict) else {},
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        self._ensure_auth_probe(cycle_id, interruption_kind, recovery_reason, source)
        return snapshot

    def set_manual_takeover(
        self,
        enabled: bool,
        ttl_seconds: int | None = None,
        reason: str = "",
        source: str = "runtime_page",
    ) -> dict:
        service_mod = _service_mod()
        effective_ttl = ttl_seconds if ttl_seconds is not None else service_mod.AUTH_MANUAL_TAKEOVER_TTL_SECONDS
        cycle_id = self._copy_auth_recovery_state().get("cycle_id") or self._next_auth_cycle_id()
        if enabled:
            snapshot = self._set_auth_recovery_state(
                cycle_id=cycle_id,
                recovery_phase="manual_takeover",
                recovery_reason=reason or "manual_takeover",
                interruption_kind=self._copy_auth_recovery_state().get("interruption_kind") or "manual_takeover",
                manual_takeover_active=True,
                manual_takeover_started_at=self._now_iso(),
                manual_takeover_until=self._future_iso(effective_ttl or service_mod.AUTH_MANUAL_TAKEOVER_TTL_SECONDS),
                last_recovery_source=source,
                lock_owner="manual_takeover",
                lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
            )
            if not self.session_keeper.is_authenticated:
                self._ensure_auth_probe(
                    cycle_id,
                    str(snapshot.get("interruption_kind") or "manual_takeover"),
                    str(snapshot.get("recovery_reason") or "manual_takeover"),
                    source,
                )
            return snapshot
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            manual_takeover_active=False,
            manual_takeover_started_at="",
            manual_takeover_until="",
            recovery_phase="silent_probe" if not self.session_keeper.is_authenticated else "recovered",
            last_recovery_source=source,
            lock_owner="",
            lock_expires_at="",
        )
        if not self.session_keeper.is_authenticated:
            self._ensure_auth_probe(
                cycle_id,
                str(snapshot.get("interruption_kind") or "manual_takeover"),
                reason or "manual_takeover_released",
                source,
            )
        return snapshot

    def trigger_auth_probe(self, reason: str = "", source: str = "runtime_page") -> dict:
        service_mod = _service_mod()
        current = self._copy_auth_recovery_state()
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="manual_takeover" if self._manual_takeover_active(current) else "silent_probe",
            recovery_reason=reason or current.get("recovery_reason") or "manual_probe",
            interruption_kind=current.get("interruption_kind") or "manual_probe",
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_result="pending",
            auto_restart_scheduled=False,
            last_recovery_source=source,
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        self._ensure_auth_probe(
            cycle_id,
            str(snapshot.get("interruption_kind") or "manual_probe"),
            str(snapshot.get("recovery_reason") or "manual_probe"),
            source,
        )
        return snapshot

    def panic_reset_auth(
        self,
        restart_gateway: bool = True,
        restart_runtime: bool = True,
        trigger_login: bool = True,
        reason: str = "",
        source: str = "runtime_page",
    ) -> dict:
        service_mod = _service_mod()
        cycle_id = self._next_auth_cycle_id()
        self._auth_probe_stop.set()
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="panic_resetting",
            recovery_reason=reason or "panic_reset_2fa",
            interruption_kind="panic_reset",
            manual_takeover_active=False,
            probe_started_at="",
            probe_last_checked_at="",
            probe_attempts=0,
            probe_result="resetting",
            auto_restart_scheduled=bool(restart_runtime and trigger_login),
            last_recovery_source=source,
            lock_owner="panic_reset",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        cookie_result = {"path": "", "existed": False, "removed": False, "error": ""}
        gateway_restarted = False
        runtime_started = False
        self.stop()
        cookie_result = service_mod.clear_cookies()
        if self.pb:
            try:
                monitor_date = datetime.now(service_mod.ET).strftime("%Y-%m-%d")
                cleared_auth_state = {
                    **self._load_global_auth_state(),
                    "status": "requested",
                    "message": "已全量清空旧 2FA / Session 状态，准备开启新一轮验证。",
                    "last_result": "旧 2FA / Session 状态已清空。",
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "response_rejected_at": "",
                    "challenge_feedback": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                    "browser_authenticated": False,
                    "gateway_authenticated": False,
                    "backend_authenticated": False,
                    "runtime_authenticated": False,
                    "runtime_started": False,
                    "message_id": "",
                    "last_delivered_ms": 0,
                    "last_delivered_at": "",
                    "last_delivered_hash": "",
                    "last_delivered_status": "",
                    "last_request_push_ms": 0,
                    "last_request_push_at": "",
                    "requested_at": self._now_iso(),
                    "triggered_at": "",
                    "result_at": "",
                    "next_retry_at": "",
                    **self._auth_recovery_pb_patch(snapshot),
                }
                self.pb.upsert_state("ibkr_2fa", service_mod.ENVIRONMENT, cleared_auth_state, date="global")
                self.pb.upsert_state("system_auth_edge_monitor", service_mod.ENVIRONMENT, {}, date=monitor_date)
                self.pb.upsert_state("system_auth_monitor", service_mod.ENVIRONMENT, {}, date=monitor_date)
            except Exception as exc:
                service_mod.logger.warning("Failed to clear PB auth state during panic reset: %s", exc)
        self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="panic_resetting",
            recovery_reason=reason or "panic_reset_2fa",
            interruption_kind="panic_reset",
            manual_takeover_active=False,
            manual_takeover_started_at="",
            manual_takeover_until="",
            probe_started_at="",
            probe_last_checked_at=self._now_iso(),
            probe_attempts=0,
            probe_result="cookies_cleared" if cookie_result.get("removed") or not cookie_result.get("existed") else "cookie_clear_failed",
            auto_restart_scheduled=bool(restart_runtime and trigger_login),
            last_recovery_source=source,
            lock_owner="panic_reset",
            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        if restart_gateway:
            gateway_restarted = bool(self.gateway_manager.restart())
        if restart_runtime:
            runtime_started = self._schedule_auth_restart(
                reason=reason or "panic_reset_2fa",
                source=source or "panic_reset",
                trigger_login=bool(trigger_login),
                allow_panic_reset=True,
            )
        return {
            "cycle_id": cycle_id,
            "state": self._copy_auth_recovery_state(),
            "cookie_cleared": cookie_result,
            "gateway_restarted": gateway_restarted,
            "runtime_started": runtime_started,
            "restart_gateway": bool(restart_gateway),
            "restart_runtime": bool(restart_runtime),
            "trigger_login": bool(trigger_login),
            "reason": reason or "panic_reset_2fa",
            "source": source,
        }
