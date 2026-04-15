from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceAuthRecoveryMixin:
    def _initial_auth_recovery_state(self) -> dict:
        return {
            "cycle_id": "",
            "recovery_phase": "idle",
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
            "lock_owner": "",
            "lock_expires_at": "",
            "updated_at": "",
        }

    def _copy_auth_recovery_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._auth_recovery_state
        return dict(payload or {})

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

    def _mark_auth_recovered(self, source: str, reason: str = ""):
        service_mod = _service_mod()
        stamp = self._now_iso()
        current = self._copy_auth_recovery_state()
        previous_phase = str(current.get("recovery_phase") or "")
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
                try:
                    while not self._auth_probe_stop.is_set():
                        current = self._copy_auth_recovery_state()
                        if str(current.get("cycle_id") or "") != cycle_id:
                            return
                        attempts += 1
                        authenticated = False
                        gateway_status_code = 0
                        try:
                            auth_payload = self.session_keeper.check_auth_status()
                            authenticated = bool(auth_payload.get("authenticated", False))
                        except Exception as exc:
                            service_mod.logger.debug("Auth probe auth check failed: %s", exc)
                        try:
                            gateway_status_code = int(self.gateway_manager.status().get("status_code") or 0)
                        except Exception:
                            gateway_status_code = 0
                        phase = "manual_takeover" if self._manual_takeover_active(current) else "silent_probe"
                        self._set_auth_recovery_state(
                            cycle_id=cycle_id,
                            recovery_phase=phase,
                            interruption_kind=interruption_kind,
                            recovery_reason=recovery_reason,
                            probe_started_at=current.get("probe_started_at") or self._now_iso(),
                            probe_last_checked_at=self._now_iso(),
                            probe_attempts=attempts,
                            probe_result="authenticated" if authenticated else "pending",
                            last_gateway_status_code=gateway_status_code,
                            last_recovery_source=source,
                            lock_owner="auth_probe",
                            lock_expires_at=self._future_iso(service_mod.AUTH_RECOVERY_LOCK_TTL_SECONDS),
                        )
                        if authenticated:
                            self._mark_auth_recovered(source="auth_probe", reason=recovery_reason or source)
                            return
                        if (time.time() - started_perf) >= service_mod.AUTH_PROBE_WINDOW_SECONDS:
                            self._set_auth_recovery_state(
                                cycle_id=cycle_id,
                                recovery_phase="requested",
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                probe_last_checked_at=self._now_iso(),
                                probe_attempts=attempts,
                                probe_result="timeout",
                                auto_restart_scheduled=False,
                                last_recovery_source=source,
                                lock_owner="",
                                lock_expires_at="",
                                manual_takeover_active=False,
                            )
                            self._request_manual_2fa(
                                recovery_reason or "auth_probe_timeout",
                                "会话未在探测窗口内自动恢复，请在飞书 2FA 卡片手动触发当前轮次。",
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

    def _schedule_auth_restart(self, reason: str, source: str, trigger_login: bool = False):
        service_mod = _service_mod()
        with self._auth_recovery_lock:
            thread = self._auth_restart_thread
            if thread and thread.is_alive():
                return False
            current = self._copy_auth_recovery_state()
            current_phase = str(current.get("recovery_phase") or "")
            current_lock_owner = str(current.get("lock_owner") or "")
            if self._manual_takeover_active(current):
                service_mod.logger.info("Skip auth recovery restart during manual takeover")
                return False
            if current_phase in {"panic_resetting", "starting_runtime"} or current_lock_owner in {"panic_reset", "runtime_start"}:
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

    def _start_auth_recovery(self, interruption_kind: str, recovery_reason: str, source: str = "runtime"):
        service_mod = _service_mod()
        current = self._copy_auth_recovery_state()
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        phase = "manual_takeover" if self._manual_takeover_active(current) else "silent_probe"
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
