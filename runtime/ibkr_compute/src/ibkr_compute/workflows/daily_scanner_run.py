"""Scan orchestration for the daily IBKR scanner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.timeframe_utils import format_cn_time, format_us_time, interval_to_chart_tf
from ibkr_compute.universe.target_execution import apply_target_execution_metadata
from ibkr_compute.universe.dynamic_admission import normalize_admission_bool

from .daily_scanner_constants import (
    DAILY_SCAN_MODE_SEED,
    DAILY_SCAN_MODE_TOPUP,
    DAILY_SCAN_SOURCE,
    DAILY_SCAN_STAGE,
    DAILY_SCAN_TOPUP_STAGE,
    DEFAULT_ACTIVE_MIN_SCORE,
    DEFAULT_ACTIVE_TARGET_LIMIT,
    DEFAULT_DAY_GAIN_TRIGGER_PCT,
    MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT,
    REJECTION_BUCKET_ACTIVE_BUDGET,
    REJECTION_BUCKET_DATA_INCOMPLETE,
    REJECTION_BUCKET_CONTEXT_GATE,
    REJECTION_BUCKET_TOPUP_ACTIVE_BUDGET,
    REJECTION_BUCKET_TOPUP_ACTIVE_SCORE,
)
from .daily_scanner_support import (
    _flatten_rejection_examples,
    _new_rejection_trackers,
    _normalize_scan_mode,
    _record_rejection,
    _safe_float,
    _target_row_is_daily_scan_active,
)
from .daily_scanner_target_reasons import (
    build_active_reason_payload,
    build_indicator_trigger_events_from_snapshot,
)


class DailyScannerRunMixin:
    def run_scan(self, date: str, environments=None, *, mode: str = DAILY_SCAN_MODE_SEED) -> dict:
        """
        执行每日自动筛选。

        返回聚合统计，订阅预算只写入 metadata，不再把预算溢出目标降为 candidate。
        """
        scanned = 0
        candidates = 0
        active = 0
        removed = 0
        errors = 0
        ok = True
        retryable = False
        error_text = ""
        environment_results = []
        eligible = 0
        rejection_summary: dict[str, int] = {}
        rejection_examples: list[dict[str, str]] = []
        data_completeness = {
            "enabled": False,
            "status": "unknown",
            "excluded_incomplete_count": 0,
            "repairing_count": 0,
            "incomplete_symbols": [],
            "soft_incomplete_count": 0,
            "soft_incomplete_symbols": [],
            "blocking_incomplete_symbols": [],
            "repair_strategy": "",
            "runtime_topup_waited": False,
            "repair_jobs": [],
        }

        scan_mode = _normalize_scan_mode(mode)
        runtime_environments = list(dict.fromkeys(resolve_data_environment(item) for item in (environments or ["live"])))

        for environment in runtime_environments:
            result = self._run_environment_scan(date, environment, mode=scan_mode)
            environment_results.append(result)
            scanned += int(result.get("scanned", 0) or 0)
            eligible += int(result.get("eligible", 0) or 0)
            candidates += int(result.get("candidates", 0) or 0)
            active += int(result.get("active", 0) or 0)
            removed += int(result.get("removed", 0) or 0)
            errors += int(result.get("errors", 0) or 0)
            if result.get("ok") is False:
                ok = False
                retryable = retryable or bool(result.get("retryable"))
                if not error_text:
                    error_text = str(result.get("error") or result.get("last_error") or "daily_scan_failed")
            for bucket, count in (result.get("rejection_summary") or {}).items():
                normalized_bucket = str(bucket or "").strip()
                if not normalized_bucket:
                    continue
                rejection_summary[normalized_bucket] = (
                    int(rejection_summary.get(normalized_bucket, 0) or 0)
                    + int(count or 0)
                )
            for example in result.get("rejection_examples") or []:
                if len(rejection_examples) >= 12:
                    break
                normalized = {
                    "bucket": str((example or {}).get("bucket") or "").strip(),
                    "symbol": str((example or {}).get("symbol") or "").strip().upper(),
                    "actual": str((example or {}).get("actual") or "").strip(),
                    "threshold": str((example or {}).get("threshold") or "").strip(),
                    "note": str((example or {}).get("note") or "").strip(),
                }
                if not normalized["bucket"] or not normalized["symbol"]:
                    continue
                if environment and len(runtime_environments) > 1:
                    normalized["environment"] = str(environment).strip().lower()
                rejection_examples.append(normalized)
            completeness = result.get("data_completeness") or {}
            if completeness:
                data_completeness["enabled"] = bool(data_completeness.get("enabled")) or bool(completeness.get("enabled"))
                data_completeness["blocking_enabled"] = (
                    bool(data_completeness.get("blocking_enabled"))
                    or bool(completeness.get("blocking_enabled"))
                )
                data_completeness["excluded_incomplete_count"] += int(completeness.get("excluded_incomplete_count", 0) or 0)
                data_completeness["repairing_count"] += int(completeness.get("repairing_count", 0) or 0)
                data_completeness["incomplete_symbols"].extend(completeness.get("incomplete_symbols") or [])
                data_completeness["soft_incomplete_count"] += int(completeness.get("soft_incomplete_count", 0) or 0)
                data_completeness["soft_incomplete_symbols"].extend(completeness.get("soft_incomplete_symbols") or [])
                data_completeness["blocking_incomplete_symbols"].extend(completeness.get("blocking_incomplete_symbols") or [])
                repair_strategy = str(completeness.get("repair_strategy") or "").strip()
                if repair_strategy and not str(data_completeness.get("repair_strategy") or "").strip():
                    data_completeness["repair_strategy"] = repair_strategy
                data_completeness["runtime_topup_waited"] = (
                    bool(data_completeness.get("runtime_topup_waited"))
                    or bool(completeness.get("runtime_topup_waited"))
                )
                data_completeness["repair_job_count"] = (
                    int(data_completeness.get("repair_job_count", 0) or 0)
                    + int(completeness.get("repair_job_count", 0) or 0)
                )
                remaining_slots = MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT - len(data_completeness["repair_jobs"])
                if remaining_slots > 0:
                    data_completeness["repair_jobs"].extend((completeness.get("repair_jobs") or [])[:remaining_slots])

        data_completeness["incomplete_symbols"] = sorted(set(data_completeness.get("incomplete_symbols") or []))
        data_completeness["incomplete_symbol_count"] = len(data_completeness["incomplete_symbols"])
        data_completeness["soft_incomplete_symbols"] = sorted(set(data_completeness.get("soft_incomplete_symbols") or []))
        data_completeness["soft_incomplete_count"] = len(data_completeness["soft_incomplete_symbols"])
        data_completeness["blocking_incomplete_symbols"] = sorted(set(data_completeness.get("blocking_incomplete_symbols") or []))
        data_completeness["status"] = "repairing" if data_completeness["incomplete_symbols"] else "ready"
        data_completeness = compact_json_payload(data_completeness, max_list_items=80)

        payload = {
            "ok": ok,
            "mode": scan_mode,
            "scan_stage": DAILY_SCAN_TOPUP_STAGE if scan_mode == DAILY_SCAN_MODE_TOPUP else DAILY_SCAN_STAGE,
            "scanned": scanned,
            "eligible": eligible,
            "candidates": candidates,
            "active": active,
            "removed": removed,
            "errors": errors,
            "rejection_summary": rejection_summary,
            "rejection_examples": rejection_examples,
            "data_completeness": data_completeness,
            "excluded_incomplete_count": int(data_completeness.get("excluded_incomplete_count", 0) or 0),
            "environment_results": environment_results,
            "new_targets": [
                row
                for result in environment_results
                for row in (result.get("new_targets") or [])
            ],
            "new_active": sum(int(result.get("new_active", 0) or 0) for result in environment_results),
            "new_candidates": sum(int(result.get("new_candidates", 0) or 0) for result in environment_results),
        }
        if not ok:
            payload["error"] = error_text or "daily_scan_failed"
            payload["retryable"] = retryable
        return payload

    def _run_environment_scan(self, date: str, environment: str, *, mode: str = DAILY_SCAN_MODE_SEED) -> dict:
        runtime_environment = resolve_data_environment(environment)
        scan_mode = _normalize_scan_mode(mode)
        scan_stage = DAILY_SCAN_TOPUP_STAGE if scan_mode == DAILY_SCAN_MODE_TOPUP else DAILY_SCAN_STAGE
        settings = self._load_scan_settings(runtime_environment)
        watchlist = self._get_watchlist(runtime_environment)
        watchlist_symbols = sorted(
            {
                str(item.get("symbol", "")).strip().upper()
                for item in watchlist
                if str(item.get("symbol", "")).strip()
            }
        )
        completeness_gate = self._build_data_completeness_gate(runtime_environment, watchlist_symbols)
        incomplete_symbols = set(completeness_gate.get("incomplete_symbols") or [])
        blocking_incomplete_symbols = (
            set(completeness_gate.get("blocking_incomplete_symbols") or [])
            if bool(completeness_gate.get("blocking_enabled"))
            else set()
        )
        quality_gate = self._data_completeness_scan_quality_gate(
            runtime_environment,
            symbols_total=len(watchlist_symbols),
            blocking_incomplete_count=len(blocking_incomplete_symbols),
        )
        repair_strategy = str(completeness_gate.get("repair_strategy") or "").strip()
        incomplete_note = (
            "5m 基础数据不完整，blocking 开启，等待 Runtime watchlist 回补后重扫"
            if repair_strategy == "runtime_watchlist_idle_topup"
            else "5m 基础数据不完整，blocking 开启，已排除本轮筛选并进入异步 API 补偿"
        )
        if scan_mode == DAILY_SCAN_MODE_SEED and bool(quality_gate.get("defer")):
            rejection_summary, rejection_examples_by_bucket = _new_rejection_trackers()
            for symbol in sorted(blocking_incomplete_symbols)[:12]:
                freshness = (completeness_gate.get("items") or {}).get(symbol) or {}
                stale_intervals = list(freshness.get("needs_repair_intervals") or [])
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_DATA_INCOMPLETE,
                    symbol=symbol,
                    actual=",".join(stale_intervals) or str(freshness.get("status") or "incomplete"),
                    threshold="blocking intervals ready",
                    note=incomplete_note,
                )
            rejection_summary[REJECTION_BUCKET_DATA_INCOMPLETE] = len(blocking_incomplete_symbols)
            data_completeness = {
                "enabled": bool(completeness_gate.get("enabled")),
                "blocking_enabled": bool(completeness_gate.get("blocking_enabled")),
                "status": str(completeness_gate.get("status") or "repairing"),
                "intervals": list(completeness_gate.get("intervals") or []),
                "blocking_intervals": list(completeness_gate.get("blocking_intervals") or []),
                "excluded_incomplete_count": len(blocking_incomplete_symbols),
                "repairing_count": len(incomplete_symbols),
                "incomplete_symbol_count": len(incomplete_symbols),
                "incomplete_symbols": sorted(incomplete_symbols),
                "soft_incomplete_count": len(completeness_gate.get("soft_incomplete_symbols") or []),
                "soft_incomplete_symbols": list(completeness_gate.get("soft_incomplete_symbols") or []),
                "blocking_incomplete_symbols": sorted(blocking_incomplete_symbols),
                "repair_strategy": repair_strategy,
                "runtime_topup_waited": bool(completeness_gate.get("runtime_topup_waited")),
                "repair_job_count": int(completeness_gate.get("repair_job_count", 0) or 0),
                "repair_jobs": list(completeness_gate.get("repair_jobs") or []),
                "quality_gate": quality_gate,
            }
            return {
                "ok": False,
                "retryable": True,
                "deferred": True,
                "error": "daily_scan_data_incomplete_repairing",
                "last_error": "daily_scan_data_incomplete_repairing",
                "environment": runtime_environment,
                "mode": scan_mode,
                "scan_stage": scan_stage,
                "scanned": len(watchlist_symbols),
                "eligible": 0,
                "candidates": 0,
                "active": 0,
                "removed": 0,
                "errors": 0,
                "new_targets": [],
                "new_active": 0,
                "new_candidates": 0,
                "trade_subscription_budget": settings["trade_subscription_budget"],
                "active_target_limit": max(
                    0,
                    int(settings.get("active_target_limit", DEFAULT_ACTIVE_TARGET_LIMIT) or DEFAULT_ACTIVE_TARGET_LIMIT),
                ),
                "active_min_score": max(
                    0.0,
                    _safe_float(settings.get("active_min_score"), DEFAULT_ACTIVE_MIN_SCORE),
                ),
                "manual_active_count": 0,
                "manual_retained_count": 0,
                "timeframe_rollup": {},
                "engine_materialize": {},
                "data_completeness": data_completeness,
                "excluded_incomplete_count": len(blocking_incomplete_symbols),
                "rejection_summary": rejection_summary,
                "rejection_examples": _flatten_rejection_examples(rejection_examples_by_bucket),
            }
        timeframe_rollup = self._rollup_scan_timeframes(runtime_environment, watchlist_symbols)
        engine_materialize = self._materialize_scan_engines(runtime_environment, watchlist_symbols)
        stored_indicator_snapshots = self._load_stored_indicator_snapshots(runtime_environment, watchlist_symbols)
        metric_rows = self._build_metric_rows(date, runtime_environment, watchlist_symbols)
        self._attach_live_trigger_inputs(date, runtime_environment, metric_rows, settings)
        existing_rows = self._load_today_target_rows(date, runtime_environment)
        eligible = []
        errors = 0
        rejection_summary, rejection_examples_by_bucket = _new_rejection_trackers()
        for item in watchlist:
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if symbol in blocking_incomplete_symbols:
                freshness = (completeness_gate.get("items") or {}).get(symbol) or {}
                stale_intervals = list(freshness.get("needs_repair_intervals") or [])
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_DATA_INCOMPLETE,
                    symbol=symbol,
                    actual=",".join(stale_intervals) or str(freshness.get("status") or "incomplete"),
                    threshold="blocking intervals ready",
                    note=incomplete_note,
                )
                continue
            try:
                result = self.evaluate_symbol(
                    symbol,
                    date,
                    runtime_environment,
                    metrics=metric_rows.get(symbol) or {},
                    settings=settings,
                    stored_snapshots=stored_indicator_snapshots.get(symbol) or {},
                )
                if result and bool(result.get("quality_gate_passed")):
                    result["exchange"] = str(item.get("exchange", "") or result.get("exchange", "")).strip().upper()
                    if symbol in incomplete_symbols:
                        freshness = (completeness_gate.get("items") or {}).get(symbol) or {}
                        result_extra = result.setdefault("extra", {})
                        result_extra["data_quality"] = {
                            "needs_repair": True,
                            "scan_blocking": False,
                            "needs_repair_intervals": list(freshness.get("needs_repair_intervals") or []),
                            "status": str(freshness.get("status") or "repairing"),
                            "severity": "warning",
                        }
                    eligible.append(result)
                else:
                    for example in (result or {}).get("rejection_examples") or []:
                        _record_rejection(
                            rejection_summary,
                            rejection_examples_by_bucket,
                            bucket=str((example or {}).get("bucket") or ""),
                            symbol=str((example or {}).get("symbol") or symbol),
                            actual=str((example or {}).get("actual") or ""),
                            threshold=str((example or {}).get("threshold") or ""),
                            note=str((example or {}).get("note") or ""),
                        )
            except Exception as exc:
                errors += 1
                print(f"[Scanner] {runtime_environment}/{symbol} error: {exc}")

        eligible.sort(
            key=lambda item: (
                -_safe_float(item.get("context_score")),
                -_safe_float(item.get("score")),
                -_safe_float(item.get("admission_score")),
                -_safe_float(item.get("technical_score")),
                -abs(_safe_float(item.get("day_change_pct"))),
                -_safe_float(item.get("premarket_volume")),
                -_safe_float(item.get("avg_10d_volume")),
                -_safe_float(item.get("atr_pct")),
                str(item.get("symbol", "")).strip().upper(),
            )
        )

        trade_budget = settings["trade_subscription_budget"]
        raw_active_target_limit = settings.get("active_target_limit", DEFAULT_ACTIVE_TARGET_LIMIT)
        try:
            active_target_limit = max(
                0,
                int(DEFAULT_ACTIVE_TARGET_LIMIT if raw_active_target_limit in (None, "") else raw_active_target_limit),
            )
        except (TypeError, ValueError):
            active_target_limit = DEFAULT_ACTIVE_TARGET_LIMIT
        active_min_score = max(
            0.0,
            _safe_float(settings.get("active_min_score"), DEFAULT_ACTIVE_MIN_SCORE),
        )
        active_limit = active_target_limit
        if trade_budget is not None:
            trade_budget_limit = max(0, int(trade_budget))
            active_limit = min(active_limit, trade_budget_limit)

        existing_target_symbols = {
            str(row.get("symbol", "")).strip().upper()
            for row in existing_rows
            if str(row.get("symbol", "")).strip()
        }
        retained_symbols = set(existing_target_symbols if scan_mode == DAILY_SCAN_MODE_TOPUP else [])
        active_symbols = {
            str(row.get("symbol", "")).strip().upper()
            for row in existing_rows
            if str(row.get("status", "")).strip().lower() == "active"
            and _target_row_is_daily_scan_active(row)
            and str(row.get("symbol", "")).strip()
        }
        if scan_mode != DAILY_SCAN_MODE_TOPUP:
            active_symbols = set()
        active_count = 0
        candidate_count = 0
        new_targets: list[dict] = []

        auto_rank = 0
        for result in eligible:
            symbol = str(result.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if scan_mode == DAILY_SCAN_MODE_TOPUP and symbol in existing_target_symbols:
                continue

            auto_rank += 1
            legacy_score_gate_passed = _safe_float(result.get("score")) >= active_min_score
            context_gate_passed = bool(result.get("context_gate_passed"))
            qualifies_active = context_gate_passed
            if scan_mode != DAILY_SCAN_MODE_TOPUP and not context_gate_passed:
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_CONTEXT_GATE,
                    symbol=symbol,
                    actual=str(result.get("context_reason") or "context_gate_passed=false"),
                    threshold="context_gate_passed=true",
                    note="基础质量通过，但 multi-timeframe context 未形成，seed 不再写入低 context candidate",
                )
                continue
            if not qualifies_active:
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_TOPUP_ACTIVE_SCORE if scan_mode == DAILY_SCAN_MODE_TOPUP else REJECTION_BUCKET_CONTEXT_GATE,
                    symbol=symbol,
                    actual=str(result.get("context_reason") or f"context_score={_safe_float(result.get('context_score')):.3f}"),
                    threshold="context_gate_passed=true",
                    note="信号窗口入池只新增 trade-window SD/cRSI pressure active 标的；低 pressure 不写 candidate",
                )
                continue
            if active_limit is not None and len(active_symbols) >= active_limit:
                _record_rejection(
                    rejection_summary,
                    rejection_examples_by_bucket,
                    bucket=REJECTION_BUCKET_TOPUP_ACTIVE_BUDGET if scan_mode == DAILY_SCAN_MODE_TOPUP else REJECTION_BUCKET_ACTIVE_BUDGET,
                    symbol=symbol,
                    actual=str(len(active_symbols)),
                    threshold=str(active_limit),
                    note="active 目标上限已满，不再写入超限 active 标的",
                )
                continue
            status = "active"
            within_subscription_budget = qualifies_active and (
                active_limit is None or len(active_symbols) < active_limit
            )
            retained_symbols.add(symbol)
            if within_subscription_budget:
                active_symbols.add(symbol)
                subscription_rank = len(active_symbols)
            else:
                subscription_rank = 0
            active_count += 1
            context_active = context_gate_passed

            context_allowed_sides = list(result.get("allowed_sides") or [])
            extra = {
                **(result.get("extra") or {}),
                "environment": runtime_environment,
                "source": DAILY_SCAN_SOURCE,
                "scan_mode": scan_mode,
                "scan_stage": scan_stage,
                "topup_round_time_et": datetime.now(ET).strftime("%H:%M") if scan_mode == DAILY_SCAN_MODE_TOPUP else "",
                "technical_score": round(_safe_float(result.get("technical_score")), 3),
                "admission_score": round(_safe_float(result.get("admission_score")), 3),
                "avg_10d_volume": round(_safe_float(result.get("avg_10d_volume")), 2),
                "premarket_volume": round(_safe_float(result.get("premarket_volume")), 2),
                "atr_pct": round(_safe_float(result.get("atr_pct")), 4),
                "day_change_pct": round(_safe_float(result.get("day_change_pct")), 2),
                "selection_rank": auto_rank,
                "subscription_rank": subscription_rank,
                "within_subscription_budget": within_subscription_budget,
                "active_target_limit": active_target_limit,
                "active_limit_effective": active_limit if active_limit is not None else 0,
                "active_min_score": active_min_score,
                "active_gate_passed": context_gate_passed,
                "legacy_score_gate_passed": legacy_score_gate_passed,
                "context_active": context_active,
                "context_gate_passed": context_gate_passed,
                "setup_family": str(result.get("setup_family") or "none"),
                "context_allowed_sides": context_allowed_sides,
                "context_score": round(_safe_float(result.get("context_score")), 3),
                "context_reason": str(result.get("context_reason") or "").strip(),
            }
            dynamic_thresholds = result.get("dynamic_thresholds") or extra.get("dynamic_thresholds") or {}
            if isinstance(dynamic_thresholds, dict):
                extra["threshold_profile"] = str(dynamic_thresholds.get("threshold_profile") or "").strip()
                extra["threshold_profile_reasons"] = list(dynamic_thresholds.get("threshold_profile_reasons") or [])
            extra = apply_target_execution_metadata(
                extra,
                direction_bias=result.get("direction_bias", "neutral"),
                status=status,
                active_gate_passed=context_gate_passed,
            )
            extra.update(
                build_active_reason_payload(
                    symbol=symbol,
                    status=status,
                    direction_bias=str(result.get("direction_bias", "neutral") or "neutral"),
                    score=_safe_float(result.get("score")),
                    active_min_score=active_min_score,
                    rank=auto_rank,
                    subscription_rank=subscription_rank,
                    scan_reason=str(result.get("reason", "") or ""),
                    extra=extra,
                )
            )
            self.pb_client.upsert_scan(
                {
                    "environment": runtime_environment,
                    "symbol": symbol,
                    "exchange": str(result.get("exchange", "") or "").strip().upper(),
                    "date": date,
                    "direction_bias": result.get("direction_bias", "neutral"),
                    "score": round(_safe_float(result.get("score")), 3),
                    "scan_reason": result.get("reason", ""),
                    "status": status,
                    "extra": extra,
                }
            )
            new_targets.append(
                {
                    "environment": runtime_environment,
                    "symbol": symbol,
                    "exchange": str(result.get("exchange", "") or "").strip().upper(),
                    "date": date,
                    "status": status,
                    "direction_bias": result.get("direction_bias", "neutral"),
                    "score": round(_safe_float(result.get("score")), 3),
                    "admission_score": round(_safe_float(result.get("admission_score")), 3),
                    "threshold_profile": extra.get("threshold_profile", ""),
                    "execution_eligible": bool(extra.get("execution_eligible")),
                    "target_layer": str(extra.get("target_layer") or ""),
                    "execution_blockers": list(extra.get("execution_blockers") or []),
                    "scan_reason": result.get("reason", ""),
                    "scan_stage": scan_stage,
                    "active_reason_summary": extra.get("active_reason_summary") or {},
                }
            )

        removed = 0
        if scan_mode != DAILY_SCAN_MODE_TOPUP:
            removed = self._reconcile_removed_targets(
                date=date,
                environment=runtime_environment,
                retained_symbols=retained_symbols,
            )

        return {
            "environment": runtime_environment,
            "mode": scan_mode,
            "scan_stage": scan_stage,
            "scanned": len(watchlist_symbols),
            "eligible": len([
                item
                for item in eligible
                if str(item.get("symbol", "")).strip().upper()
                not in (existing_target_symbols if scan_mode == DAILY_SCAN_MODE_TOPUP else set())
            ]),
            "candidates": candidate_count + sum(
                1
                for row in (existing_rows if scan_mode == DAILY_SCAN_MODE_TOPUP else [])
                if str(row.get("status", "")).strip().lower() == "candidate"
            ),
            "active": active_count + sum(
                1
                for row in (existing_rows if scan_mode == DAILY_SCAN_MODE_TOPUP else [])
                if str(row.get("status", "")).strip().lower() == "active"
                and _target_row_is_daily_scan_active(row)
            ),
            "removed": removed,
            "errors": errors,
            "new_targets": new_targets,
            "new_active": sum(1 for row in new_targets if row.get("status") == "active"),
            "new_candidates": sum(1 for row in new_targets if row.get("status") == "candidate"),
            "trade_subscription_budget": trade_budget,
            "active_target_limit": active_target_limit,
            "active_limit_effective": active_limit if active_limit is not None else 0,
            "active_min_score": active_min_score,
            "manual_active_count": 0,
            "manual_retained_count": 0,
            "timeframe_rollup": timeframe_rollup,
            "engine_materialize": engine_materialize,
            "data_completeness": {
                "enabled": bool(completeness_gate.get("enabled")),
                "blocking_enabled": bool(completeness_gate.get("blocking_enabled")),
                "status": str(completeness_gate.get("status") or ("ready" if not incomplete_symbols else "repairing")),
                "intervals": list(completeness_gate.get("intervals") or []),
                "blocking_intervals": list(completeness_gate.get("blocking_intervals") or []),
                "excluded_incomplete_count": len(blocking_incomplete_symbols),
                "repairing_count": len(incomplete_symbols),
                "incomplete_symbol_count": len(incomplete_symbols),
                "incomplete_symbols": sorted(incomplete_symbols),
                "soft_incomplete_count": len(completeness_gate.get("soft_incomplete_symbols") or []),
                "soft_incomplete_symbols": list(completeness_gate.get("soft_incomplete_symbols") or []),
                "blocking_incomplete_symbols": sorted(blocking_incomplete_symbols),
                "repair_strategy": str(completeness_gate.get("repair_strategy") or "").strip(),
                "runtime_topup_waited": bool(completeness_gate.get("runtime_topup_waited")),
                "repair_job_count": int(completeness_gate.get("repair_job_count", 0) or 0),
                "repair_jobs": list(completeness_gate.get("repair_jobs") or []),
            },
            "rejection_summary": rejection_summary,
            "rejection_examples": _flatten_rejection_examples(rejection_examples_by_bucket),
        }

    def _build_metric_rows(self, date: str, environment: str, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        payload = self._build_screener_payload(
            environment=environment,
            market_date=date,
            symbols=symbols,
            limit=0,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        rows = {}
        for item in items:
            symbol = str((item or {}).get("symbol", "")).strip().upper()
            if not symbol:
                continue
            rows[symbol] = dict(item)
        fundamentals_by_symbol = self._load_fundamentals_by_symbol(symbols)
        for symbol, fundamentals in fundamentals_by_symbol.items():
            row = rows.get(symbol)
            if row is None:
                continue
            row["fundamentals"] = dict(fundamentals)
            row["symbol_fundamentals"] = dict(fundamentals)
        return rows

    def _attach_live_trigger_inputs(
        self,
        date: str,
        environment: str,
        metric_rows: dict[str, dict],
        settings: dict,
    ) -> None:
        if not metric_rows:
            return
        try:
            self._attach_live_day_gain_trigger_times(date, environment, metric_rows, settings)
        except Exception as exc:
            print(f"[Scanner] trigger day-gain timing skipped: {exc}")
        try:
            indicator_events = self._load_live_indicator_trigger_events(date, environment, sorted(metric_rows.keys()))
        except Exception as exc:
            print(f"[Scanner] indicator trigger timing skipped: {exc}")
            indicator_events = {}
        for symbol, events in (indicator_events or {}).items():
            if not events or symbol not in metric_rows:
                continue
            metric_rows[symbol]["indicator_trigger_events"] = events

    def _scan_date_bounds_ms(self, date: str) -> tuple[int, int]:
        start_dt = datetime.strptime(str(date or "").strip(), "%Y-%m-%d").replace(
            tzinfo=ET,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end_dt = start_dt + timedelta(days=1)
        return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    def _attach_live_day_gain_trigger_times(
        self,
        date: str,
        environment: str,
        metric_rows: dict[str, dict],
        settings: dict,
    ) -> None:
        if not normalize_admission_bool((settings or {}).get("day_gain_trigger_enabled"), True):
            return
        threshold_pct = max(
            0.0,
            _safe_float((settings or {}).get("day_gain_trigger_pct"), DEFAULT_DAY_GAIN_TRIGGER_PCT),
        )
        if threshold_pct <= 0:
            return
        symbols = [
            symbol
            for symbol, row in (metric_rows or {}).items()
            if _safe_float((row or {}).get("prev_close")) > 0
            and _safe_float((row or {}).get("day_change_pct")) >= threshold_pct
        ]
        if not symbols:
            return
        start_ms, end_ms = self._scan_date_bounds_ms(date)
        placeholders = ", ".join("?" for _ in symbols)
        runtime_environment = resolve_data_environment(environment)
        env_clause = "(environment = ? OR environment = '')" if runtime_environment == "live" else "environment = ?"
        params = [runtime_environment, start_ms, end_ms, *symbols]
        with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, close, us_time, cn_time, bar_time_ms
                FROM ibkr_bars
                WHERE interval = '5m'
                  AND {env_clause}
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                  AND symbol IN ({placeholders})
                ORDER BY symbol ASC, bar_time_ms ASC
                """,
                tuple(params),
            ).fetchall()
        seen: set[str] = set()
        for row in rows:
            symbol = str(row["symbol"] or "").strip().upper()
            if not symbol or symbol in seen or symbol not in metric_rows:
                continue
            metric_row = metric_rows[symbol]
            prev_close = _safe_float(metric_row.get("prev_close"))
            close = _safe_float(row["close"])
            if prev_close <= 0 or close <= 0:
                continue
            day_gain_pct = (close - prev_close) / prev_close * 100.0
            if day_gain_pct < threshold_pct:
                continue
            bar_ms = int(row["bar_time_ms"] or 0)
            metric_row.update(
                {
                    "day_gain_triggered_at_ms": bar_ms,
                    "day_gain_triggered_us_time": str(row["us_time"] or format_us_time(bar_ms)),
                    "day_gain_triggered_cn_time": str(row["cn_time"] or format_cn_time(bar_ms)),
                    "day_gain_triggered_price": round(close, 4),
                    "day_gain_triggered_pct": round(day_gain_pct, 4),
                    "day_gain_trigger_source": "ibkr_bars_5m_close",
                }
            )
            seen.add(symbol)

    def _load_live_indicator_trigger_events(
        self,
        date: str,
        environment: str,
        symbols: list[str],
    ) -> dict[str, list[dict]]:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols:
            return {}
        start_ms, end_ms = self._scan_date_bounds_ms(date)
        placeholders = ", ".join("?" for _ in normalized_symbols)
        chart_tf = interval_to_chart_tf("5m")
        interval_values = list(dict.fromkeys([chart_tf, "5m"]))
        interval_placeholders = ", ".join("?" for _ in interval_values)
        runtime_environment = resolve_data_environment(environment)
        params = [runtime_environment, *interval_values, start_ms, end_ms, *normalized_symbols]
        with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, interval, us_time, cn_time, bar_time_ms, extra
                FROM ibkr_indicators
                WHERE environment = ?
                  AND interval IN ({interval_placeholders})
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                  AND symbol IN ({placeholders})
                ORDER BY bar_time_ms ASC
                """,
                tuple(params),
            ).fetchall()
        events_by_symbol: dict[str, list[dict]] = {}
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            symbol = str(row["symbol"] or "").strip().upper()
            if not symbol:
                continue
            extra = row["extra"]
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            if not isinstance(extra, dict):
                extra = {}
            events = build_indicator_trigger_events_from_snapshot(
                extra,
                timeframe="5m",
                bar_time_ms=int(row["bar_time_ms"] or 0),
                us_time=str(row["us_time"] or ""),
                cn_time=str(row["cn_time"] or ""),
                source="indicator_history",
                precision="first_seen",
            )
            for event in events:
                key = (symbol, str(event.get("key") or ""), str(event.get("timeframe") or ""))
                if key in seen:
                    continue
                seen.add(key)
                events_by_symbol.setdefault(symbol, []).append(event)
        return events_by_symbol

    def _load_fundamentals_by_symbol(self, symbols: list[str]) -> dict[str, dict]:
        normalized_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            }
        )
        if not normalized_symbols:
            return {}
        pb = getattr(self, "pb_client", None)
        if pb is None:
            return {}

        def escape(value: str) -> str:
            return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

        symbol_filter = "(" + " || ".join(f'symbol = "{escape(symbol)}"' for symbol in normalized_symbols) + ")"
        filter_expr = f'{symbol_filter} && provider = "finnhub"'
        try:
            if hasattr(pb, "get_all_records"):
                records = pb.get_all_records("ibkr_fundamentals", filter=filter_expr, sort="-updated", max_pages=2)
            else:
                records = pb.get_records("ibkr_fundamentals", filter=filter_expr, sort="-updated", per_page=500, page=1)
        except Exception:
            return {}

        result: dict[str, dict] = {}
        for raw_record in records or []:
            record = dict(raw_record or {})
            symbol = str(record.get("symbol", "")).strip().upper()
            if not symbol or symbol in result:
                continue
            status = str(record.get("status") or "").strip().lower()
            if status == "failed":
                continue
            extra = _as_dict(record.get("extra"))
            market_cap_usd = _safe_float(_fundamental_value(record, extra, "market_cap_usd", "market_cap", "marketCap"))
            market_cap_millions = _safe_float(
                _fundamental_value(record, extra, "market_cap_millions", "marketCapitalization")
            )
            if market_cap_usd <= 0 and market_cap_millions > 0:
                market_cap_usd = market_cap_millions * 1_000_000.0
            share_outstanding_millions = _safe_float(
                _fundamental_value(record, extra, "share_outstanding_millions", "shares_outstanding_millions")
            )
            if share_outstanding_millions > 0:
                shares_outstanding = share_outstanding_millions * 1_000_000.0
            else:
                shares_outstanding = _safe_float(
                    _fundamental_value(record, extra, "shares_outstanding", "sharesOutstanding", "share_outstanding")
                )
            normalized = {
                **record,
                "market_cap": market_cap_usd,
                "market_cap_usd": market_cap_usd,
                "shares_outstanding": shares_outstanding,
                "shares_float": _fundamental_value(record, extra, "shares_float", "float_shares"),
                "float_shares": _fundamental_value(record, extra, "float_shares", "shares_float"),
                "short_float_pct": _fundamental_value(record, extra, "short_float_pct"),
                "sector": str(_fundamental_value(record, extra, "sector") or "").strip(),
                "country": str(_fundamental_value(record, extra, "country") or "").strip().upper(),
                "beta": _fundamental_value(record, extra, "beta"),
                "avg_volume_10d_provider": _fundamental_value(
                    record, extra, "avg_volume_10d_provider", "avg_10d_volume"
                ),
                "industry": str(_fundamental_value(record, extra, "industry") or "").strip(),
                "exchange": str(_fundamental_value(record, extra, "exchange") or "").strip().upper(),
                "provider": str(record.get("provider") or "finnhub").strip().lower(),
            }
            result[symbol] = normalized
        return result


def _as_dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _first_present(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _fundamental_value(row, extra, *keys):
    return _first_present(*(row.get(key) for key in keys), *(extra.get(key) for key in keys))
