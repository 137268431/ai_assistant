from __future__ import annotations

from .market_universe_support import *
from .market_universe_support import (
    _classify_daily_scan_failure,
    _compact_daily_scan_diagnostics,
    _compact_daily_scan_result_for_state,
    _daily_scan_all_snapshotless,
    _daily_scan_running_age_seconds,
    _extract_daily_scan_failure_evidence,
    _parse_iso_datetime,
    _safe_extra,
    _safe_float,
    _safe_int,
    _service_mod,
    _target_row_is_manual,
)

from . import market_universe_support as _market_universe_support


def _service_mod():
    # Keep tests and legacy callers that patch market_universe._service_mod effective.
    import sys

    facade = sys.modules.get(f"{__package__}.market_universe")
    patched = getattr(facade, "_service_mod", None) if facade is not None else None
    if patched is not None:
        return patched()
    return _market_universe_support._service_mod()


class TradingServiceMarketUniverseDailyScanMixin:
    def _initial_daily_scan_state(self, market_date: str = "") -> dict:
        return {
            "market_date": str(market_date or self._market_date()),
            "status": "idle",
            "reason": "",
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "result": {},
            "run_id": "",
            "attempt_count": 0,
            "retry_count": 0,
            "next_retry_at": "",
            "retry_cutoff_at": "",
            "retry_block_reason": "",
            "failure": {},
            "diagnostics": {},
        }

    def _load_daily_scan_state(self, market_date: str) -> dict:
        service_mod = _service_mod()
        target_date = str(market_date or self._market_date())
        try:
            state = self.pb.get_state(
                service_mod.DAILY_SCAN_STATE_KEY,
                service_mod.DATA_ENVIRONMENT,
                date=service_mod.DAILY_SCAN_STATE_DATE,
            )
        except Exception:
            state = None
        payload = state.get("data") if isinstance(state, dict) else {}
        if not isinstance(payload, dict):
            return self._initial_daily_scan_state(target_date)
        loaded = {
            **self._initial_daily_scan_state(target_date),
            **payload,
        }
        if str(loaded.get("market_date") or "") != target_date:
            return self._initial_daily_scan_state(target_date)
        return loaded

    def _copy_daily_scan_state(self) -> dict:
        with self._scan_state_lock:
            return dict(self._daily_scan_state or {})

    def _set_daily_scan_state(self, **updates) -> dict:
        service_mod = _service_mod()
        with self._scan_state_lock:
            next_state = dict(self._daily_scan_state or self._initial_daily_scan_state())
            next_state.update(updates)
            next_state["market_date"] = str(next_state.get("market_date") or self._market_date())
            self._daily_scan_state = next_state
            try:
                self.pb.upsert_state(
                    service_mod.DAILY_SCAN_STATE_KEY,
                    service_mod.DATA_ENVIRONMENT,
                    next_state,
                    date=service_mod.DAILY_SCAN_STATE_DATE,
                )
            except Exception:
                service_mod.logger.warning("Persist daily scan state failed", exc_info=True)
            return dict(next_state)

    def _daily_scan_config_bool(self, key: str, default: bool = False) -> bool:
        service_mod = _service_mod()
        config = getattr(self, "config", None)
        if config is not None and hasattr(config, "get_bool_for_environment"):
            try:
                return bool(config.get_bool_for_environment(key, service_mod.DATA_ENVIRONMENT, default))
            except Exception:
                return bool(default)
        return bool(default)

    def _daily_scan_config_int(self, key: str, default: int = 0) -> int:
        service_mod = _service_mod()
        config = getattr(self, "config", None)
        if config is not None and hasattr(config, "get_int_for_environment"):
            try:
                return int(config.get_int_for_environment(key, service_mod.DATA_ENVIRONMENT, default))
            except Exception:
                return int(default)
        return int(default)

    def _daily_scan_config_text(self, key: str, default: str = "") -> str:
        service_mod = _service_mod()
        config = getattr(self, "config", None)
        if config is not None and hasattr(config, "get_for_environment"):
            try:
                return str(config.get_for_environment(key, service_mod.DATA_ENVIRONMENT, default) or default)
            except Exception:
                return str(default or "")
        return str(default or "")

    def _daily_scan_retry_delays(self) -> list[int]:
        raw = self._daily_scan_config_text("ibkr_daily_scan_retry_delays_sec", "60,120,240")
        delays: list[int] = []
        for item in raw.replace(";", ",").split(","):
            try:
                value = int(float(str(item or "").strip()))
            except Exception:
                continue
            if value >= 0:
                delays.append(value)
        return delays or [60, 120, 240]

    def _daily_scan_retry_cutoff(self, now_dt: datetime | None = None) -> datetime:
        service_mod = _service_mod()
        et_zone = getattr(service_mod, "ET", None)
        current = now_dt or (datetime.now(et_zone) if et_zone is not None else datetime.now())
        raw_schedule = self._daily_scan_config_text("ibkr_scan_schedule", "08:20-09:20")
        end_text = "09:20"
        if "-" in raw_schedule:
            end_text = raw_schedule.split("-", 1)[1].strip() or end_text
        parsed = self._parse_hhmm(end_text)
        if not parsed:
            parsed = (10, 0)
        cutoff = current.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
        if cutoff < current and current.hour < 4:
            cutoff = cutoff + timedelta(days=1)
        return cutoff

    def _daily_scan_next_retry_at(self, retry_count: int, now_dt: datetime | None = None) -> datetime:
        service_mod = _service_mod()
        et_zone = getattr(service_mod, "ET", None)
        current = now_dt or (datetime.now(et_zone) if et_zone is not None else datetime.now())
        delays = self._daily_scan_retry_delays()
        index = min(max(0, int(retry_count or 0)), len(delays) - 1)
        return current + timedelta(seconds=max(0, delays[index]))

    def _daily_scan_should_retry(self, state: dict, failure: dict, now_dt: datetime | None = None) -> tuple[bool, str, datetime | None, datetime]:
        service_mod = _service_mod()
        et_zone = getattr(service_mod, "ET", None)
        current = now_dt or (datetime.now(et_zone) if et_zone is not None else datetime.now())
        cutoff = self._daily_scan_retry_cutoff(current)
        if not self._daily_scan_config_bool("ibkr_daily_scan_auto_retry_enabled", True):
            return False, "auto_retry_disabled", None, cutoff
        if not bool((failure or {}).get("retryable")):
            return False, "failure_not_retryable", None, cutoff
        retry_count = _safe_int((state or {}).get("retry_count"), 0)
        max_retries = len(self._daily_scan_retry_delays())
        if retry_count >= max_retries:
            return False, "max_retries_reached", None, cutoff
        next_retry = self._daily_scan_next_retry_at(retry_count, current)
        if current >= cutoff or next_retry > cutoff:
            return False, "retry_cutoff_reached", None, cutoff
        return True, "", next_retry, cutoff

    def _notify_daily_scan_retrying(self, state: dict | None = None):
        service_mod = _service_mod()
        failure = (state or {}).get("failure") if isinstance((state or {}).get("failure"), dict) else {}
        detail = {
            "状态结论": "今日目标池自动筛选暂未完成，系统会按保守幂等规则自动重试。",
            "检查时间": self._now_et(),
            "交易日": str((state or {}).get("market_date") or self._current_market_date or self._market_date()),
            "Runtime阶段": self._runtime_phase_label(),
            "失败分类": str(failure.get("code") or (state or {}).get("last_error") or "daily_scan_retry"),
            "错误信息": str((state or {}).get("last_error") or failure.get("message") or "daily_scan_retry"),
            "重试次数": str((state or {}).get("retry_count") or 0),
            "下一次重试": str((state or {}).get("next_retry_at") or ""),
            "重试截止": str((state or {}).get("retry_cutoff_at") or ""),
            "处理建议": str(failure.get("operator_action") or "等待自动重试；若超过截止仍失败，再人工检查。"),
        }
        evidence = failure.get("evidence") if isinstance(failure.get("evidence"), dict) else {}
        if evidence:
            detail["原因证据"] = compact_json_payload(evidence, max_list_items=8, max_dict_items=40, max_string_length=360, max_depth=4)
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        self._emit_system_event("alert", "warning", "IBKR 盘前日筛重试中", detail)

    def _schedule_or_fail_daily_scan_retry(
        self,
        *,
        market_date: str,
        reason: str,
        failure: dict,
        result: dict | None = None,
        diagnostics: dict | None = None,
        run_id: str = "",
    ) -> dict:
        state = self._copy_daily_scan_state()
        can_retry, block_reason, next_retry, cutoff = self._daily_scan_should_retry(state, failure)
        base_updates = {
            "market_date": market_date,
            "reason": reason,
            "run_id": str(run_id or state.get("run_id") or ""),
            "last_error": str((failure or {}).get("message") or (failure or {}).get("code") or "daily_scan_failed"),
            "failure": dict(failure or {}),
            "diagnostics": compact_json_payload(diagnostics or {}, max_list_items=10, max_dict_items=80, max_string_length=480, max_depth=5),
            "result": _compact_daily_scan_result_for_state(result or {}),
        }
        if can_retry and next_retry is not None:
            retry_count = _safe_int(state.get("retry_count"), 0) + 1
            retry_state = self._set_daily_scan_state(
                **base_updates,
                status="retry_wait",
                finished_at=self._now_iso(),
                retry_count=retry_count,
                next_retry_at=next_retry.isoformat(),
                retry_cutoff_at=cutoff.isoformat(),
                retry_block_reason="",
            )
            self._notify_daily_scan_retrying(retry_state)
            return {"ok": False, "ran": True, "retry_scheduled": True, "state": retry_state, "failure": failure}

        final_failure = {
            **dict(failure or {}),
            "retryable": False,
            "retry_block_reason": block_reason,
        }
        failed_state = self._set_daily_scan_state(
            **base_updates,
            status="failed",
            finished_at=self._now_iso(),
            failure=final_failure,
            retry_block_reason=block_reason,
            next_retry_at="",
            retry_cutoff_at=cutoff.isoformat(),
        )
        self._notify_daily_scan_failed(failed_state)
        return {"ok": False, "ran": True, "retry_scheduled": False, "state": failed_state, "failure": final_failure}

    def _daily_scan_retry_wait_pending(self, state: dict) -> bool:
        next_retry_at = _parse_iso_datetime((state or {}).get("next_retry_at"))
        if next_retry_at is None:
            return False
        service_mod = _service_mod()
        et_zone = getattr(service_mod, "ET", None)
        now_dt = datetime.now(et_zone) if et_zone is not None else datetime.now()
        if next_retry_at.tzinfo is None and now_dt.tzinfo is not None:
            next_retry_at = next_retry_at.replace(tzinfo=now_dt.tzinfo)
        elif next_retry_at.tzinfo is not None and now_dt.tzinfo is not None:
            next_retry_at = next_retry_at.astimezone(now_dt.tzinfo)
        return now_dt < next_retry_at

    def _finalize_daily_scan_result(self, *, market_date: str, reason: str, result: dict, run_id: str = "") -> dict:
        scan_ok = bool(result.get("ok", True))
        last_error = "" if scan_ok else str(result.get("error") or result.get("last_error") or "daily_scan_failed")
        if scan_ok and _daily_scan_all_snapshotless(result):
            last_error = "all_scanned_symbols_missing_technical_snapshots"
            result = {**result, "ok": False, "error": last_error}
            scan_ok = False
        if scan_ok:
            completed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="completed",
                reason=reason,
                run_id=run_id,
                finished_at=self._now_iso(),
                last_error="",
                failure={},
                diagnostics={},
                next_retry_at="",
                result=_compact_daily_scan_result_for_state(result),
            )
            self._notify_daily_scan_recovered(completed_state)
            self._last_target_refresh_at = 0.0
            return {"ok": True, "ran": True, "state": completed_state, "result": result}

        failure = _classify_daily_scan_failure(last_error, result=result, retryable_default=True)
        return self._schedule_or_fail_daily_scan_retry(
            market_date=market_date,
            reason=reason,
            failure=failure,
            result=result,
            diagnostics=failure.get("evidence") if isinstance(failure.get("evidence"), dict) else {},
            run_id=run_id,
        )

    def _poll_daily_scan_attempt(self, *, market_date: str, reason: str, state: dict) -> dict:
        service_mod = _service_mod()
        run_id = str((state or {}).get("run_id") or "").strip()
        if not run_id:
            return {"ok": True, "skipped": True, "reason": "scan_running", "state": state}
        try:
            from ibkr_compute.api.compute_status_client import get_remote_scan_status

            payload = get_remote_scan_status(
                {
                    "environment": service_mod.DATA_ENVIRONMENT,
                    "broker_mode": service_mod.ENVIRONMENT,
                    "data_environment": service_mod.DATA_ENVIRONMENT,
                    "date": market_date,
                    "run_id": run_id,
                }
            ) or {}
        except Exception as exc:
            payload = {"ok": False, "error": str(exc), "error_code": "compute_status_failed", "retryable": True}

        if payload.get("ok") and str(payload.get("status") or "").strip().lower() in DAILY_SCAN_FINAL_STATUSES:
            result = payload.get("result") if isinstance(payload.get("result"), dict) else dict(payload)
            if "ok" not in result:
                result["ok"] = str(payload.get("status") or "").strip().lower() == "completed"
            if payload.get("counts") and isinstance(payload.get("counts"), dict):
                result.update({key: value for key, value in payload.get("counts").items() if key not in result})
            if payload.get("last_error") and not result.get("error"):
                result["error"] = payload.get("last_error")
            return self._finalize_daily_scan_result(market_date=market_date, reason=reason, result=result, run_id=run_id)

        et_zone = getattr(service_mod, "ET", None)
        now_dt = datetime.now(et_zone) if et_zone is not None else datetime.now()
        running_age = _daily_scan_running_age_seconds(state, now_dt)
        stall_timeout = self._daily_scan_config_int("ibkr_daily_scan_retry_stall_timeout_sec", 480)
        if running_age is not None and running_age >= stall_timeout:
            diagnostics = _compact_daily_scan_diagnostics(payload)
            failure = _classify_daily_scan_failure(
                "compute_scan_stalled",
                result={"ok": False, "error": "compute_scan_stalled", **payload},
                diagnostics=diagnostics,
                retryable_default=True,
            )
            if failure.get("code") == "compute_scan_submit_timeout":
                failure["code"] = "compute_scan_stalled"
            return self._schedule_or_fail_daily_scan_retry(
                market_date=market_date,
                reason=reason,
                failure=failure,
                result={"ok": False, "error": "compute_scan_stalled", **payload},
                diagnostics=diagnostics,
                run_id=run_id,
            )

        return {
            "ok": True,
            "skipped": True,
            "reason": "scan_pending",
            "run_id": run_id,
            "state": self._set_daily_scan_state(
                market_date=market_date,
                status="pending",
                reason=reason,
                run_id=run_id,
                diagnostics=_compact_daily_scan_diagnostics(payload),
            ),
        }

    def _reset_daily_scan_alert_state(self, market_date: str = ""):
        self._daily_scan_alert_market_date = str(market_date or "")
        self._daily_scan_alert_error = ""
        self._daily_scan_alert_title = ""
        self._daily_scan_alert_at = 0.0
        self._daily_scan_alert_active = False
        self._daily_scan_failure_count = 0

    def _notify_daily_scan_failed(self, state: dict | None = None):
        service_mod = _service_mod()
        market_date = str((state or {}).get("market_date") or self._current_market_date or self._market_date())
        error_text = str((state or {}).get("last_error") or "daily_scan_failed").strip() or "daily_scan_failed"
        reason = str((state or {}).get("reason") or "").strip() or "poll"
        result = (state or {}).get("result")
        if not isinstance(result, dict):
            result = {}
        failure = (state or {}).get("failure")
        if not isinstance(failure, dict):
            failure = {}
        if self._daily_scan_alert_market_date != market_date:
            self._reset_daily_scan_alert_state(market_date)
        self._daily_scan_failure_count += 1

        now = time.time()
        should_send = (
            not self._daily_scan_alert_active
            or error_text != self._daily_scan_alert_error
            or self._daily_scan_alert_at <= 0
            or (now - self._daily_scan_alert_at) >= service_mod.DAILY_SCAN_EVENT_ALERT_COOLDOWN_SECONDS
        )
        self._daily_scan_alert_market_date = market_date
        self._daily_scan_alert_error = error_text
        self._daily_scan_alert_title = "IBKR 盘前日筛失败"
        if not should_send:
            return

        detail = {
            "状态结论": "今日目标池自动筛选失败，盘中 active / candidate targets 不会按预期刷新。",
            "检查时间": self._now_et(),
            "交易日": market_date,
            "Runtime阶段": self._runtime_phase_label(),
            "触发原因": reason,
            "错误信息": error_text,
            "失败分类": str(failure.get("code") or "daily_scan_failed"),
            "失败次数": str(self._daily_scan_failure_count),
            "处理建议": str(failure.get("operator_action") or "检查 compute / screener / targets 写入链路，并在修复后手动重跑 /scan。"),
        }
        if (state or {}).get("retry_block_reason"):
            detail["重试停止原因"] = str((state or {}).get("retry_block_reason") or "")
        evidence = failure.get("evidence") if isinstance(failure.get("evidence"), dict) else {}
        if evidence:
            detail["原因证据"] = compact_json_payload(evidence, max_list_items=8, max_dict_items=40, max_string_length=360, max_depth=4)
        scanned = int(result.get("scanned", 0) or 0)
        active = int(result.get("active", 0) or 0)
        candidates = int(result.get("candidates", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if scanned > 0 or active > 0 or candidates > 0 or errors > 0:
            detail["扫描结果"] = (
                f"scanned={scanned} active={active} "
                f"candidate={candidates} errors={errors}"
            )
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url

        self._emit_system_event("alert", "error", self._daily_scan_alert_title, detail)
        self._daily_scan_alert_active = True
        self._daily_scan_alert_at = now

    def _notify_daily_scan_recovered(self, state: dict | None = None):
        if not self._daily_scan_alert_active:
            return

        market_date = str((state or {}).get("market_date") or self._daily_scan_alert_market_date or self._market_date())
        reason = str((state or {}).get("reason") or "").strip() or "poll"
        result = (state or {}).get("result")
        if not isinstance(result, dict):
            result = {}

        detail = {
            "状态结论": "今日目标池自动筛选已恢复成功，盘中 trade targets 已重新生成。",
            "检查时间": self._now_et(),
            "交易日": market_date,
            "Runtime阶段": self._runtime_phase_label(),
            "恢复来源": reason,
            "上一条错误": self._daily_scan_alert_error or "-",
        }
        scanned = int(result.get("scanned", 0) or 0)
        active = int(result.get("active", 0) or 0)
        candidates = int(result.get("candidates", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        detail["恢复结果"] = (
            f"scanned={scanned} active={active} "
            f"candidate={candidates} errors={errors}"
        )
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url

        self._emit_system_event("alert", "info", "IBKR 盘前日筛已恢复", detail)
        self._reset_daily_scan_alert_state(market_date)
