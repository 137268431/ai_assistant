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


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _target_row_is_daily_scan_active(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    return source == "daily_scan" and _truthy(extra.get("active_gate_passed"))


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
