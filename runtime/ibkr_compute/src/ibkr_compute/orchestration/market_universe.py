from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta

from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.market.timeframe_utils import bucket_start_ms, format_us_time, interval_to_ms


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}
DAILY_SCAN_RUNNING_STALE_SECONDS = 10 * 60
DAILY_SCAN_FINAL_STATUSES = {"completed", "failed", "cancelled"}


def _safe_extra(row: dict | None) -> dict:
    payload = (row or {}).get("extra")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _target_row_is_manual(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source.startswith("manual_"):
        return True
    return source in MANUAL_TARGET_SOURCES


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _daily_scan_all_snapshotless(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    scanned = _safe_int(result.get("scanned"), 0)
    rejection_summary = result.get("rejection_summary")
    if not isinstance(rejection_summary, dict):
        rejection_summary = {}
    no_snapshot = _safe_int(rejection_summary.get("no_snapshot"), 0)
    if scanned <= 0 and isinstance(result.get("environment_results"), list):
        env_results = [row for row in result.get("environment_results") if isinstance(row, dict)]
        scanned = sum(_safe_int(row.get("scanned"), 0) for row in env_results)
        no_snapshot = 0
        for row in env_results:
            summary = row.get("rejection_summary")
            if isinstance(summary, dict):
                no_snapshot += _safe_int(summary.get("no_snapshot"), 0)
    return scanned > 0 and no_snapshot >= scanned


def _parse_iso_datetime(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _daily_scan_running_age_seconds(state: dict | None, now_dt: datetime) -> float | None:
    if not isinstance(state, dict):
        return None
    started_at = _parse_iso_datetime(state.get("started_at"))
    if started_at is None:
        return None
    if started_at.tzinfo is None and now_dt.tzinfo is not None:
        started_at = started_at.replace(tzinfo=now_dt.tzinfo)
    elif started_at.tzinfo is not None and now_dt.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    if now_dt.tzinfo is not None and started_at.tzinfo is not None:
        started_at = started_at.astimezone(now_dt.tzinfo)
    return max(0.0, (now_dt - started_at).total_seconds())


def _compact_daily_scan_result_for_state(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return compact_json_payload(
        result,
        max_list_items=60,
        max_dict_items=160,
        max_string_length=1200,
        max_depth=8,
    )


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _compact_daily_scan_diagnostics(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    preload = payload.get("compute_startup_preload") or (payload.get("compute") or {}).get("compute_startup_preload")
    bar_repair = payload.get("bar_repair_queue") or (payload.get("compute") or {}).get("bar_repair_queue")
    diagnostics = {}
    if isinstance(preload, dict):
        diagnostics["compute_startup_preload"] = {
            "status": preload.get("status"),
            "running": bool(preload.get("running")),
            "symbol_completed": _safe_int(preload.get("symbol_completed"), 0),
            "symbol_total": _safe_int(preload.get("symbol_total"), 0),
            "ready_count": _safe_int(preload.get("ready_count"), 0),
            "elapsed_s": round(_safe_float(preload.get("elapsed_s"), 0.0), 3),
            "error": str(preload.get("error") or "")[:240],
        }
    if isinstance(bar_repair, dict):
        diagnostics["bar_repair_queue"] = {
            "pending": _safe_int(bar_repair.get("pending"), 0),
            "inflight": _safe_int(bar_repair.get("inflight"), 0),
            "failed": _safe_int(bar_repair.get("failed"), 0),
            "succeeded": _safe_int(bar_repair.get("succeeded"), 0),
            "max_concurrency": _safe_int(bar_repair.get("max_concurrency"), 0),
            "request_spacing_s": _safe_float(bar_repair.get("request_spacing_s"), 0.0),
        }
    if payload.get("error") or payload.get("error_code") or payload.get("status_code"):
        diagnostics["remote_error"] = {
            "error_code": str(payload.get("error_code") or "")[:120],
            "status_code": payload.get("status_code"),
            "error": str(payload.get("error") or "")[:360],
        }
    return diagnostics


def _extract_daily_scan_failure_evidence(result: dict | None, diagnostics: dict | None = None) -> dict:
    evidence = {}
    payload = result if isinstance(result, dict) else {}
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    for key in ("status_code", "error_code", "path", "timeout_s"):
        if payload.get(key) not in (None, ""):
            evidence[key] = payload.get(key)
    counts = {
        "scanned": _safe_int(payload.get("scanned"), 0),
        "active": _safe_int(payload.get("active"), 0),
        "candidates": _safe_int(payload.get("candidates"), 0),
        "errors": _safe_int(payload.get("errors"), 0),
    }
    if any(counts.values()):
        evidence["counts"] = counts
    if isinstance(payload.get("data_completeness"), dict):
        completeness = payload.get("data_completeness") or {}
        evidence["data_completeness"] = {
            "status": completeness.get("status"),
            "excluded_incomplete_count": _safe_int(completeness.get("excluded_incomplete_count"), 0),
            "repairing_count": _safe_int(completeness.get("repairing_count"), 0),
            "incomplete_symbols": list(completeness.get("incomplete_symbols") or [])[:8],
        }
    for key in ("compute_startup_preload", "bar_repair_queue", "remote_error"):
        if isinstance(diag.get(key), dict):
            evidence[key] = diag.get(key)
    return evidence


def _classify_daily_scan_failure(
    error_text: str = "",
    *,
    result: dict | None = None,
    diagnostics: dict | None = None,
    retryable_default: bool = True,
) -> dict:
    payload = result if isinstance(result, dict) else {}
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    text = " ".join(
        str(item or "")
        for item in (
            error_text,
            payload.get("error"),
            payload.get("error_code"),
            payload.get("last_error"),
        )
    ).strip()
    lowered = text.lower()

    preload = diag.get("compute_startup_preload") if isinstance(diag.get("compute_startup_preload"), dict) else {}
    if preload and bool(preload.get("running")):
        elapsed_s = _safe_float(preload.get("elapsed_s"), 0.0)
        code = "compute_preload_timeout" if elapsed_s >= 900 else "compute_preload_running"
        return {
            "code": code,
            "retryable": True,
            "message": "Compute startup preload is still running",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "等待预热完成；若持续超时，检查 IB 历史数据和 bar repair 队列。",
        }

    if ("client id" in lowered and "in use" in lowered) or "code=326" in lowered:
        return {
            "code": "ib_gateway_client_id_conflict",
            "retryable": True,
            "message": "IB Gateway client id is already in use",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "检查 runtime / compute / bar repair 的 IB client id 是否冲突。",
        }
    bar_repair = diag.get("bar_repair_queue") if isinstance(diag.get("bar_repair_queue"), dict) else {}
    if bar_repair and _safe_int(bar_repair.get("pending"), 0) >= 100:
        return {
            "code": "bar_repair_backlog_high",
            "retryable": True,
            "message": "Bar repair queue backlog is high",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "等待补齐队列下降；若持续堆积，检查 IB Gateway client id / 历史数据链路。",
        }
    if "validation_json_size_limit" in lowered or "maximum allowed json size" in lowered:
        return {
            "code": "state_persist_json_too_large",
            "retryable": True,
            "message": "Persisted scan state exceeded PocketBase JSON size limit",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "已裁剪状态后可重试；若仍失败，检查 scan state 写入内容。",
        }
    error_code = str(payload.get("error_code") or "").strip()
    if error_code in {"compute_scan_submit_timeout", "compute_request_timeout", "compute_status_timeout"} or "read timed out" in lowered or "timeout" in lowered:
        return {
            "code": "compute_scan_submit_timeout",
            "retryable": True,
            "message": "Compute scan request timed out",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "先按 run_id 查询是否已在执行；未执行再重试。",
        }
    if error_code == "compute_unreachable" or "connection refused" in lowered or "failed to establish" in lowered:
        return {
            "code": "compute_unreachable",
            "retryable": True,
            "message": "Compute endpoint is unreachable",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "检查 ibkr-compute 服务和 127.0.0.1:5100。",
        }
    if "all_scanned_symbols_missing_technical_snapshots" in lowered:
        return {
            "code": "all_scanned_symbols_missing_technical_snapshots",
            "retryable": True,
            "message": "All scanned symbols are missing technical snapshots",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "等待指标快照 ready 后重试。",
        }
    completeness = payload.get("data_completeness") if isinstance(payload.get("data_completeness"), dict) else {}
    if completeness and str(completeness.get("status") or "").lower() == "repairing":
        return {
            "code": "data_completeness_repairing",
            "retryable": True,
            "message": "Daily scan data completeness repair is still running",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "等待缺失周期补齐后重试。",
        }
    if "persist" in lowered and ("target" in lowered or "pb_request_failed" in lowered):
        return {
            "code": "target_persist_failed",
            "retryable": False,
            "message": "Target persistence failed",
            "evidence": _extract_daily_scan_failure_evidence(payload, diag),
            "operator_action": "检查 PocketBase targets 写入链路。",
        }
    return {
        "code": str(payload.get("error_code") or payload.get("error") or error_text or "daily_scan_failed")[:120],
        "retryable": bool(payload.get("retryable", retryable_default)),
        "message": str(error_text or payload.get("error") or "daily_scan_failed")[:360],
        "evidence": _extract_daily_scan_failure_evidence(payload, diag),
        "operator_action": "检查 compute / screener / targets 写入链路，并在修复后重跑 /scan。",
    }


class TradingServiceMarketUniverseMixin:
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
                service_mod.ENVIRONMENT,
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
                    service_mod.ENVIRONMENT,
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
                return bool(config.get_bool_for_environment(key, service_mod.ENVIRONMENT, default))
            except Exception:
                return bool(default)
        return bool(default)

    def _daily_scan_config_int(self, key: str, default: int = 0) -> int:
        service_mod = _service_mod()
        config = getattr(self, "config", None)
        if config is not None and hasattr(config, "get_int_for_environment"):
            try:
                return int(config.get_int_for_environment(key, service_mod.ENVIRONMENT, default))
            except Exception:
                return int(default)
        return int(default)

    def _daily_scan_config_text(self, key: str, default: str = "") -> str:
        service_mod = _service_mod()
        config = getattr(self, "config", None)
        if config is not None and hasattr(config, "get_for_environment"):
            try:
                return str(config.get_for_environment(key, service_mod.ENVIRONMENT, default) or default)
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
        raw_schedule = self._daily_scan_config_text("ibkr_scan_schedule", "09:20-10:00")
        end_text = "10:00"
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
                    "environment": service_mod.ENVIRONMENT,
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

    def _environment_watchlist_filter(self) -> str:
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        return f'environment = "{safe_env}" || environment = "global" || environment = ""'

    def _watchlist_record_role(self, row: dict) -> str:
        service_mod = _service_mod()
        return service_mod.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))

    def _refresh_watchlist_pool(self, force: bool = False):
        service_mod = _service_mod()
        refresh_minutes = max(1, self.config.get_int_for_environment("watchlist_interval_min", service_mod.ENVIRONMENT, 5))
        now = time.time()
        if (
            not force
            and self._watchlist_symbols
            and (now - self._last_watchlist_refresh_at) < (refresh_minutes * 60)
        ):
            return

        service_mod.logger.info("Refreshing watchlist pool for env=%s", service_mod.ENVIRONMENT)
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, str(service_mod.ENVIRONMENT or "live").strip().lower(): 2}

        try:
            rows = self.pb.get_all_records(
                "watchlist",
                filter=self._environment_watchlist_filter(),
                max_pages=20,
            )
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol:
                    continue
                row_env = str(row.get("environment", "") or "").strip().lower()
                rank = priority.get(row_env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = row
        except Exception as exc:
            service_mod.logger.error("Failed to refresh watchlist pool: %s", exc)
            return

        symbol_meta = {}
        trade_symbols = []
        monitor_symbols = []
        for symbol, row in merged.items():
            symbol_role = self._watchlist_record_role(row)
            symbol_meta[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
                "symbol_role": symbol_role,
            }
            if symbol_role == service_mod.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR:
                monitor_symbols.append(symbol)
            else:
                trade_symbols.append(symbol)

        self._watchlist_records = merged
        self._watchlist_symbols = sorted(merged.keys())
        self._watchlist_trade_symbols = sorted(trade_symbols)
        self._watchlist_monitor_symbols = sorted(monitor_symbols)
        self._symbol_meta = symbol_meta
        self._last_watchlist_refresh_at = now
        service_mod.logger.info(
            "Watchlist pool refreshed: %d symbols (%d trade / %d monitor)",
            len(self._watchlist_symbols),
            len(self._watchlist_trade_symbols),
            len(self._watchlist_monitor_symbols),
        )

    def _get_target_subscription_limit(self) -> int:
        service_mod = _service_mod()
        return max(0, self.config.get_int_for_environment("ibkr_target_subscription_limit", service_mod.ENVIRONMENT, 80))

    def _get_total_subscription_limit(self) -> int:
        service_mod = _service_mod()
        return max(0, self.config.get_int_for_environment("ibkr_total_subscription_limit", service_mod.ENVIRONMENT, 80))

    def _get_trade_subscription_budget(self) -> int | None:
        target_limit = self._get_target_subscription_limit()
        total_limit = self._get_total_subscription_limit()
        trade_budget = target_limit if target_limit > 0 else None
        if total_limit > 0:
            remaining_budget = max(0, total_limit - len(self._market_ws_symbols()))
            trade_budget = remaining_budget if trade_budget is None else min(trade_budget, remaining_budget)
        return trade_budget

    def _parse_hhmm(self, raw_value) -> tuple[int, int] | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            return None
        return None

    def _today_target_rows(self):
        service_mod = _service_mod()
        today = datetime.now(service_mod.ET).strftime("%Y-%m-%d")
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        rows = self.pb.get_all_records(
            "ibkr_targets",
            filter=(
                f'date = "{today}" && '
                f'environment = "{safe_env}" && '
                '(status = "candidate" || status = "active")'
            ),
            sort="-score,-updated",
            max_pages=10,
        )
        return today, rows

    def _build_target_subscription_plan(self):
        target_date, rows = self._today_target_rows()
        trade_budget = self._get_trade_subscription_budget()
        selected_symbols = []
        selected_meta = {}
        selected_rows = []
        seen = set()

        active_rows = [
            row
            for row in rows
            if str(row.get("status", "") or "").strip().lower() == "active"
        ]
        prioritized_rows = [
            row for row in active_rows if _target_row_is_manual(row)
        ] + [
            row for row in active_rows if not _target_row_is_manual(row)
        ]

        for row in prioritized_rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or symbol in seen:
                continue
            if trade_budget is not None and len(selected_rows) >= trade_budget:
                break

            watchlist_row = self._watchlist_records.get(symbol) or {}
            selected_symbols.append(symbol)
            selected_rows.append(row)
            selected_meta[symbol] = {
                "exchange": str(
                    row.get("exchange")
                    or watchlist_row.get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    watchlist_row.get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        for symbol in self._market_ws_symbols():
            if symbol in seen:
                continue
            selected_symbols.append(symbol)
            selected_meta[symbol] = {
                "exchange": str(
                    self._symbol_meta.get(symbol, {}).get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    self._symbol_meta.get(symbol, {}).get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        return target_date, selected_symbols, selected_meta, selected_rows

    def _mark_target_statuses(self, target_date: str, selected_rows):
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            existing = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{target_date}" && '
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=10,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load target rows for status sync: %s", exc)
            return

        selected_ids = {str(row.get("id") or "") for row in selected_rows}
        for row in existing:
            record_id = str(row.get("id") or "")
            if not record_id:
                continue
            if _target_row_is_manual(row):
                continue
            desired = "active" if record_id in selected_ids else "candidate"
            current = str(row.get("status", "") or "").strip().lower()
            if current == desired:
                continue
            try:
                self.pb.update_record("ibkr_targets", record_id, {"status": desired})
            except Exception as exc:
                service_mod.logger.warning("Failed to update target status %s -> %s: %s", record_id, desired, exc)

    def _scan_schedule_start(self) -> tuple[int, int]:
        service_mod = _service_mod()
        preferred = self._parse_hhmm(
            self.config.get_for_environment("ibkr_daily_scan_time_et", service_mod.ENVIRONMENT, "09:20")
        )
        if preferred:
            return preferred
        raw_schedule = str(
            self.config.get_for_environment("ibkr_scan_schedule", service_mod.ENVIRONMENT, "09:20-10:00") or ""
        ).strip()
        start_text = raw_schedule.split("-", 1)[0].strip() or "09:20"
        scheduled = self._parse_hhmm(start_text)
        if scheduled:
            return scheduled
        return 9, 20

    def _scan_window_open(self) -> bool:
        service_mod = _service_mod()
        hour, minute = self._scan_schedule_start()
        now_et = datetime.now(service_mod.ET)
        return (now_et.hour, now_et.minute) >= (hour, minute)

    def _run_daily_scan_if_due(self, reason: str = "poll") -> dict:
        service_mod = _service_mod()
        self._refresh_watchlist_pool()
        market_date = self._current_market_date or self._market_date()
        state = self._copy_daily_scan_state()
        if str(state.get("market_date") or "") != market_date:
            state = self._set_daily_scan_state(**self._initial_daily_scan_state(market_date))

        if not self._scan_window_open():
            return {"ok": True, "skipped": True, "reason": "scan_window_not_open", "state": state}
        state_status = str(state.get("status") or "").strip().lower()
        if state_status == "retry_wait":
            if self._daily_scan_retry_wait_pending(state):
                return {"ok": True, "skipped": True, "reason": "daily_scan_retry_wait", "state": state}
            state = self._set_daily_scan_state(
                market_date=market_date,
                status="idle",
                reason=reason,
                started_at="",
                finished_at="",
                last_error="",
                next_retry_at="",
            )
            state_status = "idle"

        if state_status in {"pending", "running"} and str(state.get("run_id") or "").strip():
            try:
                from ibkr_compute.api.service_topology import uses_remote_compute_service

                if uses_remote_compute_service():
                    return self._poll_daily_scan_attempt(market_date=market_date, reason=reason, state=state)
            except Exception:
                pass

        if state_status == "failed":
            return {"ok": False, "skipped": True, "reason": "daily_scan_failed", "state": state}

        if state_status == "running":
            et_zone = getattr(service_mod, "ET", None)
            running_age = _daily_scan_running_age_seconds(
                state,
                datetime.now(et_zone) if et_zone is not None else datetime.now(),
            )
            if running_age is None or running_age < DAILY_SCAN_RUNNING_STALE_SECONDS:
                return {"ok": True, "skipped": True, "reason": "scan_running", "state": state}
            service_mod.logger.warning(
                "Daily scan running state is stale; retrying: market_date=%s age=%.1fs",
                market_date,
                running_age,
            )
            state = self._set_daily_scan_state(
                market_date=market_date,
                status="failed",
                reason=str(state.get("reason") or reason or "poll"),
                finished_at=self._now_iso(),
                last_error="stale_running_timeout",
                result={
                    "ok": False,
                    "error": "stale_running_timeout",
                    "previous_started_at": str(state.get("started_at") or ""),
                    "age_seconds": round(running_age, 3),
                },
            )
        if str(state.get("status") or "").strip().lower() == "completed":
            return {"ok": True, "skipped": True, "reason": "scan_already_completed", "state": state}

        warmup_state = self._copy_warmup_state()
        symbols_total = int(warmup_state.get("symbols_total", 0) or 0)
        if symbols_total <= 0:
            return {"ok": True, "skipped": True, "reason": "no_data_symbols", "state": state}
        blocking_pending_symbols = self._non_monitor_pending_symbols(
            warmup_state.get("pending_symbols") or [],
            warmup_state.get("monitor_symbols") or [],
        )
        if blocking_pending_symbols:
            return {
                "ok": True,
                "skipped": True,
                "reason": "data_warmup_incomplete",
                "blocking_symbols": blocking_pending_symbols,
                "state": state,
            }

        if not self._watchlist_trade_symbols:
            completed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="completed",
                reason=reason,
                started_at=self._now_iso(),
                finished_at=self._now_iso(),
                last_error="",
                result={
                    "ok": True,
                    "date": market_date,
                    "scanned": 0,
                    "eligible": 0,
                    "active": 0,
                    "candidates": 0,
                    "removed": 0,
                    "errors": 0,
                    "rejection_summary": {},
                    "rejection_examples": [],
                    "environments": [service_mod.ENVIRONMENT],
                },
            )
            self._notify_daily_scan_recovered(completed_state)
            self._last_target_refresh_at = 0.0
            return {"ok": True, "ran": True, "state": completed_state}

        attempt_count = _safe_int(state.get("attempt_count"), 0) + 1
        run_id = f"daily-scan-{service_mod.ENVIRONMENT}-{market_date}-{uuid.uuid4().hex[:10]}"
        try:
            from ibkr_compute.api.service_topology import uses_remote_compute_service

            if uses_remote_compute_service():
                from ibkr_compute.api.compute_status_client import get_remote_compute_status, get_remote_scan_status, trigger_remote_scan

                diagnostics = {}
                compute_status = get_remote_compute_status(force_refresh=True)
                diagnostics = _compact_daily_scan_diagnostics(compute_status)
                preload = diagnostics.get("compute_startup_preload") if isinstance(diagnostics.get("compute_startup_preload"), dict) else {}
                if preload and bool(preload.get("running")):
                    failure = _classify_daily_scan_failure(
                        "compute_startup_preload_running",
                        result={"ok": False, "error": "compute_startup_preload_running"},
                        diagnostics=diagnostics,
                        retryable_default=True,
                    )
                    return self._schedule_or_fail_daily_scan_retry(
                        market_date=market_date,
                        reason=reason,
                        failure=failure,
                        result={"ok": False, "error": "compute_startup_preload_running"},
                        diagnostics=diagnostics,
                        run_id=run_id,
                    )

                pending_state = self._set_daily_scan_state(
                    market_date=market_date,
                    status="pending",
                    reason=reason,
                    run_id=run_id,
                    attempt_count=attempt_count,
                    started_at=self._now_iso(),
                    finished_at="",
                    last_error="",
                    failure={},
                    diagnostics=diagnostics,
                    result={},
                )
                scan_payload = {
                    "environment": service_mod.ENVIRONMENT,
                    "async": True,
                    "run_id": run_id,
                    "trigger_source": reason,
                }
                result = trigger_remote_scan(scan_payload) or {}
                if result.get("ok") and (result.get("accepted") or result.get("async")):
                    accepted_state = self._set_daily_scan_state(
                        market_date=market_date,
                        status="pending",
                        reason=reason,
                        run_id=str(result.get("run_id") or run_id),
                        attempt_count=attempt_count,
                        started_at=str(pending_state.get("started_at") or self._now_iso()),
                        finished_at="",
                        last_error="",
                        failure={},
                        diagnostics=diagnostics,
                        result=_compact_daily_scan_result_for_state(result),
                    )
                    return {
                        "ok": True,
                        "ran": True,
                        "pending": True,
                        "run_id": accepted_state.get("run_id"),
                        "state": accepted_state,
                        "result": result,
                    }

                if not result.get("ok"):
                    status_payload = get_remote_scan_status(
                        {
                            "environment": service_mod.ENVIRONMENT,
                            "date": market_date,
                            "run_id": run_id,
                        }
                    ) or {}
                    if status_payload.get("ok") and str(status_payload.get("status") or "").lower() not in {"not_found", ""}:
                        poll_state = self._set_daily_scan_state(
                            market_date=market_date,
                            status="pending",
                            reason=reason,
                            run_id=run_id,
                            attempt_count=attempt_count,
                            started_at=str(pending_state.get("started_at") or self._now_iso()),
                            finished_at="",
                            last_error="",
                            diagnostics={**diagnostics, **_compact_daily_scan_diagnostics(status_payload)},
                            result=_compact_daily_scan_result_for_state(status_payload),
                        )
                        return self._poll_daily_scan_attempt(market_date=market_date, reason=reason, state=poll_state)

                    diagnostics = {
                        **diagnostics,
                        **_compact_daily_scan_diagnostics(result),
                    }
                    failure = _classify_daily_scan_failure(
                        str(result.get("error") or result.get("error_code") or "daily_scan_submit_failed"),
                        result=result,
                        diagnostics=diagnostics,
                        retryable_default=bool(result.get("retryable", True)),
                    )
                    return self._schedule_or_fail_daily_scan_retry(
                        market_date=market_date,
                        reason=reason,
                        failure=failure,
                        result=result,
                        diagnostics=diagnostics,
                        run_id=run_id,
                    )

                return self._finalize_daily_scan_result(market_date=market_date, reason=reason, result=result, run_id=run_id)
            else:
                self._set_daily_scan_state(
                    market_date=market_date,
                    status="running",
                    reason=reason,
                    run_id=run_id,
                    attempt_count=attempt_count,
                    started_at=self._now_iso(),
                    finished_at="",
                    last_error="",
                    result={},
                )
                scan_payload = {"environment": service_mod.ENVIRONMENT}
                from ibkr_compute.api import server as compute_server

                result = compute_server._run_internal_scan(scan_payload) or {}
            return self._finalize_daily_scan_result(market_date=market_date, reason=reason, result=result, run_id=run_id)
        except Exception as exc:
            failure = _classify_daily_scan_failure(
                str(exc),
                result={"ok": False, "error": str(exc)},
                retryable_default=True,
            )
            service_mod.logger.error("Daily scan execution failed: %s", exc)
            return self._schedule_or_fail_daily_scan_retry(
                market_date=market_date,
                reason=reason,
                failure=failure,
                result={"ok": False, "error": str(exc)},
                diagnostics={},
                run_id=run_id,
            )

    def _apply_live_subscriptions(self, target_date: str, conid_map: dict, reason: str = "", trade_symbols: list[str] | None = None):
        service_mod = _service_mod()
        with self._subscription_lock:
            previous_map = dict(self._active_subscription_map)
            previous_target_date = self._active_target_date
            previous_trade_symbols = list(self._active_trade_symbols)
            previous_conids = set(previous_map.values())
            next_conids = set(conid_map.values())
            removed_conids = previous_conids - next_conids
            added_symbols = [
                symbol for symbol, conid in conid_map.items()
                if previous_map.get(symbol) != conid
            ]
            normalized_trade_symbols = sorted(
                symbol for symbol in (trade_symbols or [])
                if symbol in conid_map
            )

            if removed_conids:
                self.bar_aggregator.remove_conids(removed_conids)
                self.realtime_quote_book.remove_conids(removed_conids)
                for conid in sorted(removed_conids):
                    self.ws_client.unsubscribe(conid)

            reverse_map = {cid: sym for sym, cid in conid_map.items()}
            self.bar_aggregator.set_symbol_map(reverse_map)
            self.realtime_quote_book.set_symbol_map(reverse_map)

            for symbol in added_symbols:
                conid = conid_map.get(symbol)
                if conid:
                    self.ws_client.subscribe(conid)

            self._active_subscription_map = dict(conid_map)
            self._active_subscription_symbols = sorted(conid_map.keys())
            self._active_trade_symbols = normalized_trade_symbols
            self._active_target_date = target_date
            self._last_target_refresh_at = time.time()
            subscriptions_changed = (
                previous_target_date != target_date
                or sorted(previous_map.items()) != sorted(conid_map.items())
                or previous_trade_symbols != normalized_trade_symbols
            )

        if added_symbols and reason not in {"startup", "session_restored"}:
            added_map = {symbol: conid_map[symbol] for symbol in added_symbols if symbol in conid_map}
            service_mod.logger.info(
                "Backfilling newly subscribed target symbols: %s",
                ",".join(sorted(added_map.keys())),
            )
            self.data_backfill.backfill_all(added_map, symbol_meta=self._symbol_meta, intervals=["5m"])
            self.data_writer.flush()
        elif added_symbols:
            service_mod.logger.info(
                "Skipping inline backfill during %s; warmup will backfill %d symbols asynchronously",
                reason or "startup",
                len(added_symbols),
            )

        service_mod.logger.info(
            "Applied target subscriptions (%s): active=%d added=%d removed=%d",
            reason or "refresh",
            len(conid_map),
            len(added_symbols),
            len(removed_conids),
        )
        if subscriptions_changed or reason in {"startup", "session_restored"}:
            self._schedule_warmup(reason=reason or "subscriptions_changed", force=reason in {"startup", "session_restored"})

    def _refresh_target_subscriptions(self, force: bool = False, reason: str = "loop"):
        service_mod = _service_mod()
        self._reset_for_new_market_day(force=False)
        refresh_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", service_mod.ENVIRONMENT, 60))
        now = time.time()
        if not force and (now - self._last_target_refresh_at) < refresh_seconds:
            return

        self._refresh_watchlist_pool(force=force)
        try:
            target_date, symbols, target_meta, selected_rows = self._build_target_subscription_plan()
        except Exception as exc:
            service_mod.logger.error("Failed to build target subscription plan: %s", exc)
            return

        if not symbols:
            service_mod.logger.info("No target symbols selected for %s (%s)", target_date, reason)
            self._mark_target_statuses(target_date, [])
            self._apply_live_subscriptions(target_date, {}, reason=reason, trade_symbols=[])
            return

        for symbol, meta in target_meta.items():
            base_meta = self._symbol_meta.get(symbol, {})
            self._symbol_meta[symbol] = {
                "exchange": str(meta.get("exchange") or base_meta.get("exchange") or "").upper(),
                "industry": str(meta.get("industry") or base_meta.get("industry") or ""),
            }

        conid_map = self.conid_resolver.resolve_bulk(symbols)
        if not conid_map:
            service_mod.logger.warning("No conids resolved for target plan (%s)", reason)
            return

        trade_symbols = sorted(
            {
                str(row.get("symbol", "")).upper()
                for row in selected_rows
                if str(row.get("symbol", "")).upper() in conid_map
            }
        )
        self._mark_target_statuses(target_date, selected_rows)
        self._apply_live_subscriptions(target_date, conid_map, reason=reason, trade_symbols=trade_symbols)

    def _subscription_refresh_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Target subscription loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._sync_session_transition()
                self._reset_for_new_market_day(force=False)
                if self.session_keeper.is_authenticated:
                    self._run_daily_scan_if_due(reason="poll")
                    self._refresh_target_subscriptions(reason="poll")
                else:
                    service_mod.logger.info("Skip target refresh while session is unauthenticated")
            except Exception as exc:
                service_mod.logger.error("Target subscription loop error: %s", exc)

            sleep_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", service_mod.ENVIRONMENT, 60))
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _remove_stale_target_rows(self, active_date: str) -> int:
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=20,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load stale target rows: %s", exc)
            return 0

        removed = 0
        removed_at = datetime.now(service_mod.ET).isoformat()
        for row in rows:
            row_date = str(row.get("date", "") or "").strip()
            if not row_date or row_date == active_date:
                continue

            record_id = str(row.get("id") or "")
            if not record_id:
                continue

            payload = {"status": "removed"}
            extra = row.get("extra")
            if isinstance(extra, dict):
                next_extra = dict(extra)
                next_extra["removed_reason"] = "market_day_reset"
                next_extra["removed_at"] = removed_at
                next_extra["removed_market_date"] = active_date
                payload["extra"] = next_extra

            try:
                self.pb.update_record("ibkr_targets", record_id, payload)
                removed += 1
            except Exception as exc:
                service_mod.logger.warning(
                    "Failed to remove stale target row %s (%s %s): %s",
                    record_id,
                    row_date,
                    str(row.get("symbol", "")).upper(),
                    exc,
                )

        if removed > 0:
            service_mod.logger.info("Removed %d stale target rows before activating %s", removed, active_date)
        return removed

    def _reset_for_new_market_day(self, force: bool = False):
        service_mod = _service_mod()
        current_date = self._market_date()
        previous_date = self._current_market_date
        if not force and previous_date == current_date:
            return False

        service_mod.logger.info(
            "Market day reset: previous=%s current=%s force=%s",
            previous_date or "n/a",
            current_date,
            force,
        )
        self._current_market_date = current_date
        self._last_daily_reset_at = time.time()

        self.signal_router.daily_reset()
        self.signal_processor.daily_reset()
        self.reverse_handler.daily_reset()
        self.order_lifecycle.daily_reset()
        self.timeframe_builder.reset()
        self.bar_aggregator.reset()
        self.realtime_quote_book.reset()
        self._quote_prev_close_cache = {}
        self._quote_prev_close_cache_date = current_date
        self._signal_wakeup.clear()
        drained = self._drain_compute_queue()
        if drained > 0:
            service_mod.logger.info(
                "Cleared %d queued realtime compute tasks during market day reset",
                drained,
            )

        try:
            from ibkr_compute.api import server as compute_server

            reset_result = compute_server.reset_daily_runtime_state(
                [service_mod.ENVIRONMENT],
                reason="market_day_reset",
            )
            service_mod.logger.info("Compute daily reset result: %s", reset_result)
        except Exception as exc:
            service_mod.logger.warning("Compute daily reset failed: %s", exc)

        self._reset_warmup_state(reason="market_day_reset")
        if previous_date and previous_date != current_date:
            self._set_daily_scan_state(**self._initial_daily_scan_state(current_date))
        else:
            with self._scan_state_lock:
                self._daily_scan_state = self._load_daily_scan_state(current_date)
        self._reset_daily_scan_alert_state(current_date)
        self._remove_stale_target_rows(current_date)
        self._apply_live_subscriptions(current_date, {}, reason="market_day_reset")
        self._active_target_date = ""
        self._last_target_refresh_at = 0.0
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._watchlist_idle_topup_cursor = 0
        self._last_watchlist_deep_maintenance_at = 0.0
        with self._watchlist_idle_topup_lock:
            self._watchlist_idle_observations = {}
            self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()
        self._last_active_repair_at = 0.0
        self._last_active_repair_symbols = []
        self._last_active_repair_reasons = {}
        self._last_history_repair_at = 0.0
        self._last_history_repair_symbols = []
        self._last_pipeline_repair_at = 0.0
        self._last_pipeline_repair_symbols = []
        self._watchlist_integrity_cursor = 0
        self._last_watchlist_integrity_at = 0.0
        self._last_watchlist_integrity_symbols = []
        self._last_watchlist_integrity_repair_symbols = []
        self._official_5m_state = self._initial_official_5m_state()
        if previous_date and previous_date != current_date:
            self._persist_watchlist_integrity_cursor()
        return True

    def _bar_integrity_market_date(self) -> str:
        return str(self._current_market_date or self._market_date())

    def _bar_integrity_cursor_payload(self) -> dict:
        service_mod = _service_mod()
        return {
            "market_date": self._bar_integrity_market_date(),
            "cursor": int(self._watchlist_integrity_cursor or 0),
            "last_scan_at": (
                datetime.fromtimestamp(self._last_watchlist_integrity_at, service_mod.ET).isoformat()
                if self._last_watchlist_integrity_at else ""
            ),
            "last_symbols": list(self._last_watchlist_integrity_symbols),
            "watchlist_pool_count": len(self._watchlist_symbols),
        }

    def _persist_watchlist_integrity_cursor(self):
        service_mod = _service_mod()
        try:
            self.pb.upsert_state(
                service_mod.BAR_INTEGRITY_STATE_KEY,
                service_mod.ENVIRONMENT,
                self._bar_integrity_cursor_payload(),
                date=service_mod.BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to persist watchlist integrity cursor: %s", exc)

    def _restore_watchlist_integrity_cursor(self):
        service_mod = _service_mod()
        try:
            record = self.pb.get_state(
                service_mod.BAR_INTEGRITY_STATE_KEY,
                service_mod.ENVIRONMENT,
                date=service_mod.BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load watchlist integrity cursor: %s", exc)
            return

        payload = record.get("data") if isinstance(record, dict) else {}
        if not isinstance(payload, dict):
            return
        if str(payload.get("market_date") or "") != self._bar_integrity_market_date():
            self._watchlist_integrity_cursor = 0
            return

        try:
            self._watchlist_integrity_cursor = max(0, int(payload.get("cursor", 0) or 0))
        except Exception:
            self._watchlist_integrity_cursor = 0

    def _watchlist_integrity_candidates(self):
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_integrity_batch_size",
                service_mod.ENVIRONMENT,
                service_mod.DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE,
            ),
        )
        start = self._watchlist_integrity_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_integrity_cursor = (start + batch_size) % max(len(pool), 1)
        self._persist_watchlist_integrity_cursor()
        return ordered[:batch_size]

    def _watchlist_backfill_candidates(self):
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(1, self.config.get_int_for_environment("ibkr_watchlist_backfill_batch_size", service_mod.ENVIRONMENT, 12))
        start = self._watchlist_backfill_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_backfill_cursor = (start + batch_size) % max(len(pool), 1)
        return ordered[:batch_size]

    def _initial_watchlist_idle_topup_state(self) -> dict:
        return {
            "enabled": True,
            "running": False,
            "status": "idle",
            "skip_reason": "",
            "last_admission": {},
            "last_started_at": "",
            "last_finished_at": "",
            "last_duration_s": 0.0,
            "last_symbol": "",
            "last_symbols": [],
            "last_written_bars": 0,
            "total_written_bars": 0,
            "cycle_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "last_error": "",
            "request_period": "1d",
            "mode": "continuous_until_active_due",
            "batch_size": 8,
            "max_symbols_per_cycle": 0,
            "loop_interval_sec": 60,
            "last_batches": [],
            "last_attempted_symbols": [],
            "last_attempted_symbols_total": 0,
            "last_processed_symbols": [],
            "last_processed_symbols_total": 0,
            "last_loaded_bars": 0,
            "last_request_count": 0,
            "last_stop_reason": "",
            "estimated_next_batch_s": 20.0,
            "seconds_until_next_active_5m_due": None,
            "active_target_count": 0,
        }

    def _copy_watchlist_idle_topup_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._watchlist_idle_topup_state
        copied = {}
        for key, value in (payload or {}).items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _set_watchlist_idle_topup_state(self, **updates) -> dict:
        with self._watchlist_idle_topup_lock:
            next_state = self._copy_watchlist_idle_topup_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = dict(value)
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            self._watchlist_idle_topup_state = next_state
            return self._copy_watchlist_idle_topup_state(next_state)

    def _watchlist_idle_topup_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_watchlist_idle_topup_enabled",
            service_mod.ENVIRONMENT,
            True,
        )

    def _watchlist_idle_topup_loop_interval_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            5,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_loop_interval_sec",
                service_mod.ENVIRONMENT,
                60,
            ),
        )

    def _watchlist_idle_topup_max_symbols_per_cycle(self) -> int:
        service_mod = _service_mod()
        return max(
            0,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle",
                service_mod.ENVIRONMENT,
                0,
            ),
        )

    def _watchlist_idle_topup_batch_size(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            min(
                50,
                self.config.get_int_for_environment(
                    "ibkr_watchlist_idle_topup_batch_size",
                    service_mod.ENVIRONMENT,
                    8,
                ),
            ),
        )

    def _watchlist_idle_topup_mode(
        self,
        active_target_count: int,
        active_due_guard_required: bool = True,
    ) -> str:
        if int(active_target_count or 0) <= 0:
            return "full_load_no_active_targets"
        return (
            "continuous_until_active_due"
            if bool(active_due_guard_required)
            else "full_load_off_active_window"
        )

    def _watchlist_idle_topup_estimated_batch_seconds(self, batch_summaries: list[dict] | None = None) -> float:
        durations = [
            float((item or {}).get("duration_s", 0) or 0)
            for item in (batch_summaries or [])
            if float((item or {}).get("duration_s", 0) or 0) > 0
        ]
        if durations:
            return round(max(5.0, min(45.0, (sum(durations[-3:]) / len(durations[-3:])) * 1.25)), 1)
        with self._watchlist_idle_topup_lock:
            previous = _safe_int(self._watchlist_idle_topup_state.get("estimated_next_batch_s"), 20)
        return float(max(5, min(45, previous or 20)))

    def _watchlist_idle_topup_due_budget_allows_batch(self, admission: dict, estimated_batch_s: float) -> tuple[bool, str]:
        if not bool(admission.get("active_due_guard_required")):
            return True, ""
        active_target_count = _safe_int(admission.get("active_target_count"), 0)
        if active_target_count <= 0:
            return True, ""
        seconds_until_due = admission.get("seconds_until_next_active_5m_due")
        try:
            seconds_until_due = float(seconds_until_due)
        except Exception:
            seconds_until_due = 0.0
        guard_sec = _safe_int(admission.get("active_due_guard_sec"), self._watchlist_active_due_guard_sec())
        if seconds_until_due <= max(0.0, float(guard_sec or 0) + max(0.0, float(estimated_batch_s or 0.0))):
            return False, "active_5m_due_guard"
        return True, ""

    def _watchlist_idle_topup_request_period(self) -> str:
        service_mod = _service_mod()
        value = self.config.get_for_environment(
            "ibkr_watchlist_idle_topup_request_period",
            service_mod.ENVIRONMENT,
            "1d",
        )
        return str(value or "1d").strip() or "1d"

    def _watchlist_idle_topup_stale_ms(self) -> int:
        service_mod = _service_mod()
        stale_minutes = max(
            5,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_stale_min",
                service_mod.ENVIRONMENT,
                20,
            ),
        )
        return stale_minutes * 60 * 1000

    def _watchlist_active_due_guard_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            0,
            self.config.get_int_for_environment(
                "ibkr_watchlist_active_due_guard_sec",
                service_mod.ENVIRONMENT,
                180,
            ),
        )

    def _seconds_until_next_active_5m_due(self, now_ts: float | None = None) -> float:
        current_ts = float(now_ts or time.time())
        interval_ms = interval_to_ms("5m")
        now_ms = int(current_ts * 1000)
        try:
            current_bucket_ms = bucket_start_ms(now_ms, "5m")
        except Exception:
            return 0.0
        next_due_ms = current_bucket_ms + interval_ms + (self._official_5m_close_delay_sec() * 1000)
        while next_due_ms <= now_ms:
            next_due_ms += interval_ms
        return round(max(0.0, (next_due_ms - now_ms) / 1000.0), 1)

    def _watchlist_idle_topup_completion_snapshot(self, *, now_ms: int | None = None) -> dict:
        current_ms = int(now_ms or time.time() * 1000)
        stale_ms = self._watchlist_idle_topup_stale_ms()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)
            total = len([symbol for symbol in self._watchlist_symbols if symbol not in active_symbols])
        with self._watchlist_idle_topup_lock:
            observations = {
                symbol: item
                for symbol, item in dict(self._watchlist_idle_observations).items()
                if symbol not in active_symbols
            }

        fresh = 0
        stale = 0
        missing = 0
        oldest_ms = 0
        oldest_symbol = ""
        for symbol, item in observations.items():
            latest_ms = _safe_int((item or {}).get("latest_ms"), 0)
            if latest_ms <= 0:
                missing += 1
                continue
            if (current_ms - latest_ms) >= stale_ms:
                stale += 1
            else:
                fresh += 1
            if oldest_ms <= 0 or latest_ms < oldest_ms:
                oldest_ms = latest_ms
                oldest_symbol = symbol

        observed = len(observations)
        return {
            "total": total,
            "observed": observed,
            "fresh": fresh,
            "stale": stale,
            "missing": missing,
            "unobserved": max(0, total - observed),
            "progress_pct": round((fresh / total) * 100.0, 2) if total > 0 else 100.0,
            "observed_pct": round((observed / total) * 100.0, 2) if total > 0 else 100.0,
            "oldest_symbol": oldest_symbol,
            "oldest_latest_ms": oldest_ms,
            "oldest_latest_us": format_us_time(oldest_ms) if oldest_ms > 0 else "",
        }

    def _watchlist_idle_topup_status(self) -> dict:
        with self._watchlist_idle_topup_lock:
            state = self._copy_watchlist_idle_topup_state()
        state["completion"] = self._watchlist_idle_topup_completion_snapshot()
        return state

    def _watchlist_idle_topup_compute_inflight(self) -> bool:
        return bool(
            float(self._last_realtime_compute_started_at or 0.0)
            and float(self._last_realtime_compute_started_at or 0.0) > float(self._last_realtime_compute_at or 0.0)
        )

    def _watchlist_idle_topup_data_writer_busy(self) -> tuple[bool, dict]:
        try:
            status = self.data_writer.status()
        except Exception as exc:
            return True, {"error": str(exc), "pending_batch": 0, "inflight_batch": 0}
        pending = _safe_int(status.get("pending_batch"), 0)
        inflight = _safe_int(status.get("inflight_batch"), 0)
        return pending > 0 or inflight > 0, {"pending_batch": pending, "inflight_batch": inflight}

    def _watchlist_idle_topup_bar_repair_busy(self) -> tuple[bool, dict]:
        coordinator = getattr(self, "bar_repair_coordinator", None)
        if coordinator is None or not hasattr(coordinator, "status"):
            return False, {"pending": 0, "inflight": 0}
        try:
            status = coordinator.status()
        except Exception as exc:
            return True, {"error": str(exc), "pending": 0, "inflight": 0}
        pending = _safe_int(status.get("pending"), 0)
        inflight = _safe_int(status.get("inflight"), 0)
        return pending > 0 or inflight > 0, {"pending": pending, "inflight": inflight}

    def _watchlist_idle_topup_admission(self) -> tuple[bool, dict]:
        service_mod = _service_mod()
        blockers = []
        official_5m = self._copy_official_5m_state()
        pending_symbols = self._normalize_symbol_list(official_5m.get("pending_symbols") or [])
        queue_size = int(self._compute_queue.qsize())
        compute_inflight = self._watchlist_idle_topup_compute_inflight()
        writer_busy, writer_status = self._watchlist_idle_topup_data_writer_busy()
        bar_repair_busy, bar_repair_status = self._watchlist_idle_topup_bar_repair_busy()
        resource_governor = self._resource_governor_snapshot()
        resource_admission = (
            (resource_governor.get("admission") or {}).get("watchlist_idle_topup") or {}
        )
        seconds_until_due = self._seconds_until_next_active_5m_due()
        due_guard_sec = self._watchlist_active_due_guard_sec()
        websocket_status = {}
        try:
            websocket_status = self.ws_client.status()
        except Exception:
            websocket_status = {}
        authenticated = bool(getattr(self.session_keeper, "is_authenticated", False))
        websocket_ready = bool(websocket_status.get("connected") or websocket_status.get("ready"))
        active_subscription_count = len(self._active_subscription_symbols)
        active_target_count = len(self._active_trade_symbols)
        market_session = service_mod.build_market_session_snapshot()
        market_session_kind = str(market_session.get("kind") or "").strip().lower()
        active_due_guard_required = bool(
            market_session_kind in {"regular", "close_transition"}
            and active_target_count > 0
        )
        lag_s = 0.0
        due_bucket_ms = _safe_int(official_5m.get("last_due_bucket_ms"), 0)
        completed_bucket_ms = _safe_int(official_5m.get("last_completed_bucket_ms"), 0)
        if due_bucket_ms > completed_bucket_ms:
            lag_s = round(max(0.0, (due_bucket_ms - completed_bucket_ms) / 1000.0), 1)

        if not self._watchlist_idle_topup_enabled():
            blockers.append({"code": "disabled"})
        if self._is_warmup_active():
            blockers.append({"code": "warmup_active"})
        if not authenticated:
            blockers.append({"code": "session_unauthenticated"})
        if active_due_guard_required and active_subscription_count > 0 and not websocket_ready:
            blockers.append({"code": "websocket_not_ready"})
        if active_due_guard_required and pending_symbols:
            blockers.append({"code": "official_5m_pending", "pending_symbols": pending_symbols})
        if queue_size > 0:
            blockers.append({"code": "compute_queue_busy", "queue_size": queue_size})
        if compute_inflight:
            blockers.append({"code": "compute_inflight"})
        if writer_busy:
            blockers.append({"code": "data_writer_busy", **writer_status})
        if bar_repair_busy:
            blockers.append({"code": "bar_repair_busy", **bar_repair_status})
        if active_due_guard_required and due_guard_sec > 0 and seconds_until_due < due_guard_sec:
            blockers.append(
                {
                    "code": "active_5m_due_guard",
                    "seconds_until_due": seconds_until_due,
                    "guard_sec": due_guard_sec,
                }
            )
        if active_due_guard_required and (completed_bucket_ms <= 0 or lag_s > 90):
            blockers.append(
                {
                    "code": "active_5m_not_fresh",
                    "lag_s": lag_s,
                    "last_completed_bucket_ms": completed_bucket_ms,
                }
            )
        if not bool(resource_admission.get("admit")):
            blockers.append(
                {
                    "code": "resource_governor_denied",
                    "status": resource_governor.get("status"),
                    "blockers": list(resource_admission.get("blockers") or []),
                }
            )

        snapshot = {
            "admit": not blockers,
            "blockers": blockers,
            "queue_size": queue_size,
            "compute_inflight": compute_inflight,
            "data_writer": writer_status,
            "bar_repair_queue": bar_repair_status,
            "official_5m_pending_symbols": pending_symbols,
            "active_target_count": active_target_count,
            "active_subscription_count": active_subscription_count,
            "authenticated": authenticated,
            "websocket_ready": websocket_ready,
            "seconds_until_next_active_5m_due": seconds_until_due,
            "active_due_guard_sec": due_guard_sec,
            "active_due_guard_required": active_due_guard_required,
            "market_session": market_session_kind,
            "resource_governor": resource_governor,
        }
        return not blockers, snapshot

    def _observe_watchlist_idle_symbol(self, symbol: str, latest_ms: int):
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return
        with self._watchlist_idle_topup_lock:
            self._watchlist_idle_observations[normalized] = {
                "latest_ms": int(latest_ms or 0),
                "observed_at": self._now_iso(),
            }

    def _watchlist_idle_topup_candidates(
        self,
        *,
        exclude_symbols: set[str] | None = None,
        scan_all: bool = False,
    ) -> list[dict]:
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)
        excluded = {
            str(symbol or "").strip().upper()
            for symbol in (exclude_symbols or set())
            if str(symbol or "").strip()
        }

        pool = [
            symbol for symbol in self._watchlist_symbols
            if symbol not in active_symbols and symbol not in excluded
        ]
        if not pool:
            return []

        scan_size = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_candidate_scan_size",
                service_mod.ENVIRONMENT,
                24,
            ),
        )
        start = self._watchlist_idle_topup_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        scanned = ordered if scan_all else ordered[: min(scan_size, len(ordered))]
        self._watchlist_idle_topup_cursor = (start + len(scanned)) % max(len(pool), 1)
        stale_ms = self._watchlist_idle_topup_stale_ms()
        now_ms = int(time.time() * 1000)
        candidates = []
        for symbol in scanned:
            latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            self._observe_watchlist_idle_symbol(symbol, latest_ms)
            if latest_ms <= 0 or (now_ms - latest_ms) >= stale_ms:
                candidates.append(
                    {
                        "symbol": symbol,
                        "latest_ms": latest_ms,
                        "missing": latest_ms <= 0,
                        "stale_age_s": round(max(0, now_ms - max(0, latest_ms)) / 1000.0, 1)
                        if latest_ms > 0 else None,
                    }
                )

        candidates.sort(key=lambda item: (0 if item.get("missing") else 1, _safe_int(item.get("latest_ms"), 0)))
        return candidates

    def _run_watchlist_idle_topup_cycle(self) -> dict:
        service_mod = _service_mod()
        if not hasattr(self, "_watchlist_idle_topup_state"):
            self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()

        enabled = self._watchlist_idle_topup_enabled()
        loop_interval_sec = self._watchlist_idle_topup_loop_interval_sec()
        batch_size = self._watchlist_idle_topup_batch_size()
        max_symbols = self._watchlist_idle_topup_max_symbols_per_cycle()
        request_period = self._watchlist_idle_topup_request_period()
        self._set_watchlist_idle_topup_state(
            enabled=enabled,
            loop_interval_sec=loop_interval_sec,
            batch_size=batch_size,
            max_symbols_per_cycle=max_symbols,
            request_period=request_period,
        )
        if not enabled:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason="disabled",
                last_stop_reason="disabled",
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            return state

        admitted, admission = self._watchlist_idle_topup_admission()
        active_target_count = _safe_int(admission.get("active_target_count"), len(getattr(self, "_active_trade_symbols", []) or []))
        active_due_guard_required = bool(admission.get("active_due_guard_required"))
        mode = self._watchlist_idle_topup_mode(active_target_count, active_due_guard_required)
        seconds_until_due = admission.get("seconds_until_next_active_5m_due")
        estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds([])
        self._set_watchlist_idle_topup_state(
            mode=mode,
            active_target_count=active_target_count,
            seconds_until_next_active_5m_due=seconds_until_due,
            estimated_next_batch_s=estimated_next_batch_s,
            last_admission=admission,
        )
        if not admitted:
            reason = str(((admission.get("blockers") or [{}])[0] or {}).get("code") or "admission_blocked")
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason=reason,
                last_stop_reason=reason,
                last_admission=admission,
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            service_mod.logger.debug("Watchlist idle topup skipped: %s", reason)
            return state

        budget_ok, budget_reason = self._watchlist_idle_topup_due_budget_allows_batch(
            admission,
            estimated_next_batch_s,
        )
        if not budget_ok:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason=budget_reason,
                last_stop_reason=budget_reason,
                last_admission=admission,
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            service_mod.logger.debug("Watchlist idle topup paused before active due: %s", budget_reason)
            return state

        self._refresh_watchlist_pool()
        started = time.time()
        started_at = self._now_iso()
        written_total = 0
        processed_symbols: list[str] = []
        attempted_symbols: list[str] = []
        attempted_set: set[str] = set()
        batch_summaries: list[dict] = []
        request_count = 0
        stop_reason = ""
        last_error = ""
        full_load_candidates: list[dict] | None = None
        full_load_candidate_initialized = False

        self._set_watchlist_idle_topup_state(
            running=True,
            status="running",
            skip_reason="",
            last_stop_reason="",
            last_error="",
            last_admission=admission,
            last_started_at=started_at,
            last_batches=[],
            last_attempted_symbols=[],
            last_attempted_symbols_total=0,
            last_processed_symbols=[],
            last_processed_symbols_total=0,
            last_loaded_bars=0,
            last_request_count=0,
        )

        try:
            while True:
                if getattr(self, "_running", True) is False:
                    stop_reason = "service_stopping"
                    break

                admitted, admission = self._watchlist_idle_topup_admission()
                active_target_count = _safe_int(admission.get("active_target_count"), len(getattr(self, "_active_trade_symbols", []) or []))
                active_due_guard_required = bool(admission.get("active_due_guard_required"))
                mode = self._watchlist_idle_topup_mode(active_target_count, active_due_guard_required)
                seconds_until_due = admission.get("seconds_until_next_active_5m_due")
                estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries)
                if not admitted:
                    blocker = str(((admission.get("blockers") or [{}])[0] or {}).get("code") or "admission_blocked")
                    stop_reason = f"admission_blocked:{blocker}"
                    break

                budget_ok, budget_reason = self._watchlist_idle_topup_due_budget_allows_batch(
                    admission,
                    estimated_next_batch_s,
                )
                if not budget_ok:
                    stop_reason = budget_reason
                    break

                if max_symbols > 0 and len(attempted_set) >= max_symbols:
                    stop_reason = "max_symbols_per_cycle"
                    break

                scan_all = active_target_count <= 0 or not active_due_guard_required
                if scan_all:
                    if not full_load_candidate_initialized:
                        full_load_candidates = self._watchlist_idle_topup_candidates(
                            exclude_symbols=attempted_set,
                            scan_all=True,
                        )
                        full_load_candidate_initialized = True
                    candidates = [
                        item for item in (full_load_candidates or [])
                        if str((item or {}).get("symbol") or "").strip().upper() not in attempted_set
                    ]
                else:
                    candidates = self._watchlist_idle_topup_candidates(
                        exclude_symbols=attempted_set,
                        scan_all=False,
                    )
                if not candidates:
                    stop_reason = "no_stale_or_missing_symbols" if not processed_symbols else "no_candidates"
                    break

                remaining = max_symbols - len(attempted_set) if max_symbols > 0 else batch_size
                take_count = min(batch_size, max(1, remaining))
                batch_candidates = candidates[:take_count]
                batch_symbols = [
                    str((item or {}).get("symbol") or "").strip().upper()
                    for item in batch_candidates
                    if str((item or {}).get("symbol") or "").strip()
                ]
                batch_symbols = [symbol for symbol in batch_symbols if symbol not in attempted_set]
                if not batch_symbols:
                    stop_reason = "no_candidates"
                    break

                for symbol in batch_symbols:
                    attempted_set.add(symbol)
                    attempted_symbols.append(symbol)

                batch_started = time.perf_counter()
                raw_conid_map = self.conid_resolver.resolve_bulk(batch_symbols)
                conid_map = {}
                unresolved_symbols = []
                for symbol in batch_symbols:
                    conid = int((raw_conid_map or {}).get(symbol) or 0)
                    if conid > 0:
                        conid_map[symbol] = conid
                    else:
                        unresolved_symbols.append(symbol)
                if unresolved_symbols:
                    service_mod.logger.info(
                        "Watchlist idle topup unresolved conids: %s",
                        ",".join(unresolved_symbols),
                    )

                batch_written = 0
                flush_ok = True
                if conid_map:
                    symbol_meta = {
                        symbol: dict((getattr(self, "_symbol_meta", {}) or {}).get(symbol, {}) or {})
                        for symbol in conid_map.keys()
                    }
                    period_overrides = {
                        symbol: {"5m": request_period}
                        for symbol in conid_map.keys()
                    }
                    try:
                        results = self.data_backfill.backfill_all(
                            conid_map,
                            symbol_meta=symbol_meta,
                            intervals=["5m"],
                            repair_symbols=[],
                            period_overrides=period_overrides,
                            trace_source="watchlist_idle_topup",
                        )
                    except TypeError as exc:
                        if "trace_source" not in str(exc):
                            raise
                        results = self.data_backfill.backfill_all(
                            conid_map,
                            symbol_meta=symbol_meta,
                            intervals=["5m"],
                            repair_symbols=[],
                            period_overrides=period_overrides,
                        )
                    batch_written = sum(
                        int((per_symbol or {}).get("5m", 0) or 0)
                        for per_symbol in (results or {}).values()
                    )
                    request_count += len(conid_map)
                    if hasattr(self.data_writer, "flush"):
                        flush_ok = bool(self.data_writer.flush())
                    if not flush_ok:
                        last_error = "flush_failed"

                    for symbol in conid_map.keys():
                        latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
                        self._observe_watchlist_idle_symbol(symbol, latest_ms)
                        if symbol not in processed_symbols:
                            processed_symbols.append(symbol)
                    self._last_backfill_at = time.time()
                    self._last_backfill_symbols = list(conid_map.keys())

                    if (
                        batch_written
                        and self.config.get_bool_for_environment(
                            "ibkr_watchlist_idle_topup_materialize_5m",
                            service_mod.ENVIRONMENT,
                            False,
                        )
                    ):
                        admitted_after_write, _ = self._watchlist_idle_topup_admission()
                        if admitted_after_write:
                            self._trigger_realtime_compute(
                                source="watchlist_idle_topup",
                                symbols=list(conid_map.keys()),
                                persist_signals=False,
                                intervals=["5m"],
                                rollup_intervals=[],
                            )

                batch_duration_s = round(max(0.0, time.perf_counter() - batch_started), 3)
                written_total += int(batch_written or 0)
                batch_summary = {
                    "index": len(batch_summaries) + 1,
                    "symbols": list(batch_symbols),
                    "resolved_symbols": list(conid_map.keys()),
                    "unresolved_symbols": unresolved_symbols,
                    "written_bars": int(batch_written or 0),
                    "duration_s": batch_duration_s,
                    "flush_ok": flush_ok,
                }
                batch_summaries.append(batch_summary)
                estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries)
                self._set_watchlist_idle_topup_state(
                    mode=mode,
                    active_target_count=active_target_count,
                    seconds_until_next_active_5m_due=seconds_until_due,
                    estimated_next_batch_s=estimated_next_batch_s,
                    last_admission=admission,
                    last_batches=batch_summaries[-12:],
                    last_attempted_symbols=list(attempted_symbols),
                    last_attempted_symbols_total=len(attempted_symbols),
                    last_processed_symbols=list(processed_symbols),
                    last_processed_symbols_total=len(processed_symbols),
                    last_symbols=list(processed_symbols),
                    last_loaded_bars=written_total,
                    last_written_bars=written_total,
                    last_request_count=request_count,
                )

                if max_symbols > 0 and len(attempted_set) >= max_symbols:
                    stop_reason = "max_symbols_per_cycle"
                    break
        except Exception as exc:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="error",
                skip_reason="",
                last_stop_reason="error",
                last_error=str(exc),
                error_count=_safe_int(self._watchlist_idle_topup_state.get("error_count"), 0) + 1,
                last_finished_at=self._now_iso(),
                last_duration_s=round(max(0.0, time.time() - started), 3),
                last_batches=batch_summaries[-12:],
                last_attempted_symbols=list(attempted_symbols),
                last_attempted_symbols_total=len(attempted_symbols),
                last_processed_symbols=list(processed_symbols),
                last_processed_symbols_total=len(processed_symbols),
                last_loaded_bars=written_total,
                last_request_count=request_count,
            )
            service_mod.logger.warning("Watchlist idle topup failed: %s", exc)
            return state

        if not stop_reason:
            stop_reason = "completed"
        status = "completed" if processed_symbols else "skipped"
        skip_reason = "" if processed_symbols else stop_reason
        if not processed_symbols and attempted_symbols and stop_reason in {"no_candidates", "no_stale_or_missing_symbols"}:
            skip_reason = "no_resolved_symbols"
            stop_reason = "no_resolved_symbols"
        state = self._set_watchlist_idle_topup_state(
            running=False,
            status=status,
            skip_reason=skip_reason,
            last_stop_reason=stop_reason,
            last_admission=admission,
            last_finished_at=self._now_iso(),
            last_duration_s=round(max(0.0, time.time() - started), 3),
            last_symbol=processed_symbols[-1] if processed_symbols else "",
            last_symbols=list(processed_symbols),
            last_attempted_symbols=list(attempted_symbols),
            last_attempted_symbols_total=len(attempted_symbols),
            last_processed_symbols=list(processed_symbols),
            last_processed_symbols_total=len(processed_symbols),
            last_written_bars=written_total,
            last_loaded_bars=written_total,
            last_request_count=request_count,
            last_batches=batch_summaries[-12:],
            last_error=last_error,
            total_written_bars=_safe_int(self._watchlist_idle_topup_state.get("total_written_bars"), 0) + written_total,
            cycle_count=_safe_int(self._watchlist_idle_topup_state.get("cycle_count"), 0) + 1,
            skipped_count=(
                _safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0)
                + (0 if processed_symbols else 1)
            ),
            mode=mode,
            active_target_count=active_target_count,
            seconds_until_next_active_5m_due=seconds_until_due,
            estimated_next_batch_s=self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries),
        )
        service_mod.logger.info(
            "Watchlist idle topup cycle finished: mode=%s status=%s stop=%s attempted=%d processed=%d written=%d batches=%d next_due_s=%s estimate_s=%.1f",
            mode,
            status,
            stop_reason,
            len(attempted_symbols),
            len(processed_symbols),
            written_total,
            len(batch_summaries),
            seconds_until_due,
            float(state.get("estimated_next_batch_s", 0) or 0),
        )
        return state

    def _active_repair_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Active target repair loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._run_active_repair_cycle()
            except Exception as exc:
                service_mod.logger.error("Active target repair loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int_for_environment("ibkr_active_repair_interval_min", service_mod.ENVIRONMENT, 5) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_active_repair_cycle(self):
        service_mod = _service_mod()
        if self._is_warmup_active():
            service_mod.logger.info("Active target repair skipped while startup warmup is active")
            return
        if not self.session_keeper.is_authenticated:
            service_mod.logger.info("Active target repair skipped while session is unauthenticated")
            return

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            service_mod.logger.info(
                "Active target repair downgraded to scan-only: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )

        result = self.scan_bar_integrity(
            list(self._active_subscription_symbols),
            scan_scope="active_target",
            persist=True,
            repair=not defer_repairs,
        )
        summary = result.get("summary") or {}
        attempted_repair_symbols = list(summary.get("attempted_repair_symbols") or [])
        repair_symbols = list(attempted_repair_symbols or summary.get("repair_candidate_symbols") or [])
        if not repair_symbols:
            service_mod.logger.info("Active target repair skipped: no repair needed")
            return
        if defer_repairs and not attempted_repair_symbols:
            service_mod.logger.info("Active target repair deferred: pending=%s", ",".join(repair_symbols))
            return

        self._last_active_repair_at = time.time()
        self._last_active_repair_symbols = repair_symbols
        self._last_active_repair_reasons = {
            symbol: str((summary.get("initial_repair_reasons") or summary.get("repair_reasons") or {}).get(symbol) or "")
            for symbol in repair_symbols
        }
        history_symbols = list(summary.get("history_fetch_symbols") or [])
        if history_symbols:
            self._last_history_repair_at = self._last_active_repair_at
            self._last_history_repair_symbols = history_symbols

    def _watchlist_backfill_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Watchlist backfill loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._run_watchlist_backfill_cycle()
            except Exception as exc:
                service_mod.logger.error("Watchlist backfill loop error: %s", exc)

            sleep_seconds = self._watchlist_idle_topup_loop_interval_sec()
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_watchlist_backfill_cycle(self):
        service_mod = _service_mod()
        if self._is_warmup_active():
            service_mod.logger.info("Watchlist backfill skipped while startup warmup is active")
            self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason="warmup_active",
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            return

        self._run_watchlist_idle_topup_cycle()

        deep_interval_sec = max(
            300,
            self.config.get_int_for_environment(
                "ibkr_watchlist_backfill_interval_min",
                service_mod.ENVIRONMENT,
                30,
            ) * 60,
        )
        now = time.time()
        if self._last_watchlist_deep_maintenance_at and (
            now - float(self._last_watchlist_deep_maintenance_at or 0.0)
        ) < deep_interval_sec:
            return
        self._last_watchlist_deep_maintenance_at = now

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            service_mod.logger.info(
                "Watchlist maintenance deferred: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )
            return
        self._refresh_watchlist_pool()

        candidates = self._watchlist_backfill_candidates()
        if not candidates:
            service_mod.logger.info("Watchlist backfill skipped: no non-target symbols in pool")
            return

        stale_minutes = max(5, self.config.get_int_for_environment("ibkr_watchlist_backfill_stale_min", service_mod.ENVIRONMENT, 20))
        now_ms = int(time.time() * 1000)
        stale_ms = stale_minutes * 60 * 1000
        eligible = []
        for symbol in candidates:
            latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            if latest_ms <= 0 or (now_ms - latest_ms) >= stale_ms:
                eligible.append(symbol)

        if not eligible:
            service_mod.logger.info("Watchlist backfill skipped: batch is fresh enough")
        else:
            conid_map = self.conid_resolver.resolve_bulk(eligible)
            if not conid_map:
                service_mod.logger.warning("Watchlist backfill skipped: no conids resolved")
            else:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                service_mod.logger.info("Running incremental watchlist backfill for %d symbols", len(conid_map))
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())

        if not self.config.get_bool_for_environment("ibkr_watchlist_integrity_enabled", service_mod.ENVIRONMENT, True):
            return

        integrity_candidates = self._watchlist_integrity_candidates()
        if not integrity_candidates:
            service_mod.logger.info("Watchlist integrity scan skipped: empty candidate batch")
            return

        result = self.scan_bar_integrity(
            integrity_candidates,
            scan_scope="watchlist",
            persist=True,
            repair=True,
        )
        summary = result.get("summary") or {}
        self._last_watchlist_integrity_at = time.time()
        self._last_watchlist_integrity_symbols = list(summary.get("symbols") or integrity_candidates)
        self._last_watchlist_integrity_repair_symbols = list(
            summary.get("attempted_repair_symbols")
            or summary.get("initial_repair_symbols")
            or summary.get("repair_candidate_symbols")
            or []
        )
        self._persist_watchlist_integrity_cursor()

    def _normalize_runtime_symbols(self, symbols) -> list[str]:
        normalized = []
        seen = set()
        for item in symbols or []:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
        return normalized

    def _select_latest_valid_prime_signals(self, captured_signals, allowed_symbols=None) -> dict:
        allowed = set(self._normalize_runtime_symbols(allowed_symbols))
        grouped: dict[str, list[dict]] = {}
        evaluated: list[dict] = []
        selected: list[dict] = []

        for item in captured_signals or []:
            symbol = str((item or {}).get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if allowed and symbol not in allowed:
                continue
            grouped.setdefault(symbol, []).append(dict(item or {}))

        for symbol, rows in grouped.items():
            rows.sort(key=lambda row: int(row.get("bar_time_ms", 0) or 0), reverse=True)
            for row in rows:
                signal_payload = {
                    "signal_id": str(row.get("signal_id") or ""),
                    "symbol": symbol,
                    "direction": str(row.get("direction") or "").strip(),
                    "entry": float(row.get("entry", 0) or 0),
                    "stop_loss": float(row.get("stop_loss", 0) or 0),
                    "take_profit": float(row.get("take_profit", 0) or 0),
                    "shares": int(row.get("shares", 0) or 0),
                    "signal_time": row.get("us_time") or "",
                }
                valid, reason = self.signal_processor.validate_signal(signal_payload)
                evaluated.append(
                    {
                        "symbol": symbol,
                        "signal_id": str(row.get("signal_id") or ""),
                        "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                        "valid": bool(valid),
                        "reason": str(reason or ""),
                    }
                )
                if not valid:
                    continue

                extra = dict(row.get("extra") or {})
                extra.update(
                    {
                        "universe_prime": True,
                        "universe_prime_source": "manual_pool_reconcile",
                        "universe_prime_triggered_at": self._now_iso(),
                    }
                )
                row["extra"] = extra
                selected.append(row)
                break

        return {
            "symbols": sorted(grouped.keys()),
            "selected_signals": selected,
            "evaluated": evaluated,
        }

    def _persist_prime_signals(self, selected_signals) -> dict:
        service_mod = _service_mod()
        persisted = []
        errors = []
        signal_ids = []

        for item in selected_signals or []:
            payload = dict(item or {})
            signal_id = str(payload.get("signal_id") or "").strip()
            if not signal_id:
                continue
            payload["environment"] = service_mod.ENVIRONMENT
            try:
                result = self.pb.upsert_signal(payload)
                persisted.append(
                    {
                        "signal_id": signal_id,
                        "symbol": str(payload.get("symbol") or "").strip().upper(),
                        "action": str(result.get("action") or ""),
                        "status": str(result.get("status") or ""),
                    }
                )
                signal_ids.append(signal_id)
            except Exception as exc:
                errors.append({"signal_id": signal_id, "error": str(exc)})

        if signal_ids:
            self.signal_router.forget_processed(signal_ids)
            self._signal_wakeup.set()

        return {
            "persisted": persisted,
            "errors": errors,
            "signal_ids": signal_ids,
        }

    def _prime_universe_symbols(self, symbols, *, emit_signals: bool = False, source: str = "universe_prime") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        backfill_result: dict = {"ok": True, "symbols": [], "resolved": []}
        conid_map = {}
        try:
            conid_map = self.conid_resolver.resolve_bulk(normalized_symbols)
        except Exception as exc:
            backfill_result = {"ok": False, "symbols": normalized_symbols, "error": str(exc)}

        if conid_map:
            try:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())
                backfill_result = {
                    "ok": True,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "missing": sorted(set(normalized_symbols) - set(conid_map.keys())),
                }
            except Exception as exc:
                backfill_result = {
                    "ok": False,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "error": str(exc),
                }

        compute_result = {}
        selected_signal_result = {"symbols": [], "selected_signals": [], "evaluated": []}
        persisted_signal_result = {"persisted": [], "errors": [], "signal_ids": []}
        interval_prime_started = False
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_result = compute_server._run_internal_compute(
                    {
                        "source": "targeted_recompute",
                        "environments": [service_mod.ENVIRONMENT],
                        "symbols": normalized_symbols,
                        "force_rollup": True,
                        "persist_signals": False,
                        "capture_signals": bool(emit_signals),
                    }
                ) or {}
        except Exception as exc:
            compute_result = {"ok": False, "error": str(exc)}

        if emit_signals:
            selected_signal_result = self._select_latest_valid_prime_signals(
                (compute_result or {}).get("captured_signals") or [],
                allowed_symbols=normalized_symbols,
            )
            persisted_signal_result = self._persist_prime_signals(
                selected_signal_result.get("selected_signals") or []
            )

        try:
            interval_prime_started = self._schedule_interval_prime(
                normalized_symbols,
                source=str(source or "universe_prime"),
            )
        except Exception:
            interval_prime_started = False

        return {
            "ok": bool((compute_result or {}).get("ok", True)) and bool(backfill_result.get("ok", True)),
            "symbols": normalized_symbols,
            "emit_signals": bool(emit_signals),
            "backfill": backfill_result,
            "compute": compute_result,
            "signal_selection": selected_signal_result,
            "signal_persist": persisted_signal_result,
            "interval_prime_started": interval_prime_started,
        }

    def _cleanup_universe_symbols(self, symbols, *, source: str = "universe_cleanup") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        removed_conids = []
        with self._subscription_lock:
            removed_conids = sorted(
                {
                    int(conid)
                    for symbol, conid in self._active_subscription_map.items()
                    if symbol in normalized_symbols and int(conid or 0) > 0
                }
            )

        if removed_conids:
            self.bar_aggregator.remove_conids(removed_conids)
            self.realtime_quote_book.remove_conids(removed_conids)
            for conid in removed_conids:
                try:
                    self.ws_client.unsubscribe(conid)
                except Exception:
                    service_mod.logger.warning("Universe cleanup unsubscribe failed: conid=%s", conid, exc_info=True)

        for symbol in normalized_symbols:
            self._quote_prev_close_cache.pop(symbol, None)
        self._last_backfill_symbols = [symbol for symbol in self._last_backfill_symbols if symbol not in normalized_symbols]
        self._last_active_repair_symbols = [symbol for symbol in self._last_active_repair_symbols if symbol not in normalized_symbols]
        self._last_history_repair_symbols = [symbol for symbol in self._last_history_repair_symbols if symbol not in normalized_symbols]
        self._last_pipeline_repair_symbols = [symbol for symbol in self._last_pipeline_repair_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_symbols = [symbol for symbol in self._last_watchlist_integrity_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_repair_symbols = [
            symbol for symbol in self._last_watchlist_integrity_repair_symbols if symbol not in normalized_symbols
        ]

        compute_reset = {}
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_reset = compute_server.reset_compute_state_for_symbols(
                    service_mod.ENVIRONMENT,
                    normalized_symbols,
                )
                compute_server.persist_compute_cursors(service_mod.ENVIRONMENT)
        except Exception as exc:
            compute_reset = {"ok": False, "error": str(exc)}

        sqlite_result = {}
        try:
            from ibkr_compute.market.pocketbase_sqlite import delete_symbol_runtime_data, open_pb_sqlite

            with open_pb_sqlite(readonly=False) as conn:
                sqlite_result = delete_symbol_runtime_data(conn, service_mod.ENVIRONMENT, normalized_symbols)
                conn.commit()
        except Exception as exc:
            sqlite_result = {"ok": False, "error": str(exc), "symbols": normalized_symbols}

        deleted_signal_ids = list((sqlite_result or {}).get("deleted_signal_ids") or [])
        if deleted_signal_ids:
            self.signal_router.forget_processed(deleted_signal_ids)

        return {
            "ok": not bool((sqlite_result or {}).get("error")) and not bool((compute_reset or {}).get("error")),
            "symbols": normalized_symbols,
            "source": str(source or "universe_cleanup"),
            "removed_conids": removed_conids,
            "compute_reset": compute_reset,
            "sqlite": sqlite_result,
        }

    def reconcile_market_universe(
        self,
        *,
        prime_symbols=None,
        cleanup_symbols=None,
        emit_signals: bool = False,
        source: str = "runtime_api",
        reason: str = "manual_reconcile",
    ) -> dict:
        service_mod = _service_mod()
        prime_list = self._normalize_runtime_symbols(prime_symbols)
        cleanup_list = self._normalize_runtime_symbols(cleanup_symbols)

        self.config.refresh()
        self._refresh_runtime_settings()
        self._reset_for_new_market_day(force=False)
        self._refresh_watchlist_pool(force=True)

        target_refresh = {
            "ok": True,
            "skipped": not self.session_keeper.is_authenticated,
            "reason": "session_unauthenticated" if not self.session_keeper.is_authenticated else "",
        }
        if self.session_keeper.is_authenticated:
            try:
                self._refresh_target_subscriptions(force=True, reason=reason or "manual_reconcile")
                target_refresh = {
                    "ok": True,
                    "skipped": False,
                    "active_target_date": self._active_target_date,
                    "active_target_symbols": list(self._active_trade_symbols),
                    "active_subscription_symbols": list(self._active_subscription_symbols),
                }
            except Exception as exc:
                target_refresh = {"ok": False, "skipped": False, "error": str(exc)}

        prime_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if prime_list:
            prime_result = self._prime_universe_symbols(
                prime_list,
                emit_signals=emit_signals,
                source=source or "runtime_api",
            )

        cleanup_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if cleanup_list:
            cleanup_result = self._cleanup_universe_symbols(
                cleanup_list,
                source=source or "runtime_api",
            )

        return {
            "ok": bool(target_refresh.get("ok", True)) and bool(prime_result.get("ok", True)) and bool(cleanup_result.get("ok", True)),
            "environment": service_mod.ENVIRONMENT,
            "market_date": str(self._current_market_date or self._market_date()),
            "source": str(source or "runtime_api"),
            "reason": str(reason or "manual_reconcile"),
            "emit_signals": bool(emit_signals),
            "target_refresh": target_refresh,
            "prime": prime_result,
            "cleanup": cleanup_result,
        }
