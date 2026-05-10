"""Scan orchestration for the daily IBKR scanner."""

from __future__ import annotations

from datetime import datetime

from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.core.time_utils import ET

from .daily_scanner_constants import (
    DAILY_SCAN_MODE_SEED,
    DAILY_SCAN_MODE_TOPUP,
    DAILY_SCAN_SOURCE,
    DAILY_SCAN_STAGE,
    DAILY_SCAN_TOPUP_STAGE,
    MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT,
    REJECTION_BUCKET_DATA_INCOMPLETE,
)
from .daily_scanner_support import (
    _flatten_rejection_examples,
    _new_rejection_trackers,
    _normalize_scan_mode,
    _record_rejection,
    _safe_float,
    _target_row_is_manual,
)


class DailyScannerRunMixin:
    def run_scan(self, date: str, environments=None, *, mode: str = DAILY_SCAN_MODE_SEED) -> dict:
        """
        执行每日自动筛选。

        返回聚合统计，active / candidate 已经按订阅预算写入。
        """
        scanned = 0
        candidates = 0
        active = 0
        removed = 0
        errors = 0
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
            "repair_jobs": [],
        }

        scan_mode = _normalize_scan_mode(mode)
        runtime_environments = environments or ["live", "paper"]

        for environment in runtime_environments:
            result = self._run_environment_scan(date, environment, mode=scan_mode)
            environment_results.append(result)
            scanned += int(result.get("scanned", 0) or 0)
            eligible += int(result.get("eligible", 0) or 0)
            candidates += int(result.get("candidates", 0) or 0)
            active += int(result.get("active", 0) or 0)
            removed += int(result.get("removed", 0) or 0)
            errors += int(result.get("errors", 0) or 0)
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
                data_completeness["repair_job_count"] = (
                    int(data_completeness.get("repair_job_count", 0) or 0)
                    + int(completeness.get("repair_job_count", 0) or 0)
                )
                remaining_slots = MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT - len(data_completeness["repair_jobs"])
                if remaining_slots > 0:
                    data_completeness["repair_jobs"].extend((completeness.get("repair_jobs") or [])[:remaining_slots])

        data_completeness["incomplete_symbols"] = sorted(set(data_completeness.get("incomplete_symbols") or []))
        data_completeness["incomplete_symbol_count"] = len(data_completeness["incomplete_symbols"])
        data_completeness["status"] = "repairing" if data_completeness["incomplete_symbols"] else "ready"
        data_completeness = compact_json_payload(data_completeness, max_list_items=80)

        return {
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

    def _run_environment_scan(self, date: str, environment: str, *, mode: str = DAILY_SCAN_MODE_SEED) -> dict:
        runtime_environment = str(environment or "live").strip().lower() or "live"
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
        blocking_incomplete_symbols = set(incomplete_symbols) if bool(completeness_gate.get("blocking_enabled")) else set()
        engine_materialize = self._materialize_scan_engines(runtime_environment, watchlist_symbols)
        stored_indicator_snapshots = self._load_stored_indicator_snapshots(runtime_environment, watchlist_symbols)
        metric_rows = self._build_metric_rows(date, runtime_environment, watchlist_symbols)
        existing_rows = self._load_today_target_rows(date, runtime_environment)
        manual_rows = {
            str(row.get("symbol", "")).strip().upper(): row
            for row in existing_rows
            if _target_row_is_manual(row)
        }
        manual_retained_symbols = set(manual_rows.keys())
        manual_active_count = sum(
            1
            for row in manual_rows.values()
            if str(row.get("status", "")).strip().lower() == "active"
        )

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
                    threshold="all required intervals ready",
                    note="数据不完整，blocking 开启，已排除本轮筛选并进入异步 API 补偿",
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
        if trade_budget is None:
            active_limit = len(existing_rows) + len(eligible)
        elif scan_mode == DAILY_SCAN_MODE_TOPUP:
            active_limit = max(0, int(trade_budget))
        else:
            active_limit = max(0, int(trade_budget) - manual_active_count)

        existing_target_symbols = {
            str(row.get("symbol", "")).strip().upper()
            for row in existing_rows
            if str(row.get("symbol", "")).strip()
        }
        retained_symbols = set(existing_target_symbols if scan_mode == DAILY_SCAN_MODE_TOPUP else manual_retained_symbols)
        active_symbols = {
            str(row.get("symbol", "")).strip().upper()
            for row in existing_rows
            if str(row.get("status", "")).strip().lower() == "active"
            and str(row.get("symbol", "")).strip()
        }
        if scan_mode != DAILY_SCAN_MODE_TOPUP:
            active_symbols = {
                symbol
                for symbol, row in manual_rows.items()
                if str(row.get("status", "")).strip().lower() == "active"
            }
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
            if scan_mode != DAILY_SCAN_MODE_TOPUP and symbol in manual_retained_symbols:
                continue

            auto_rank += 1
            status = "active" if len(active_symbols) < active_limit else "candidate"
            retained_symbols.add(symbol)
            if status == "active":
                active_symbols.add(symbol)
                active_count += 1
            else:
                candidate_count += 1

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
                "subscription_rank": len(active_symbols) if status == "active" else 0,
                "within_subscription_budget": status == "active",
            }
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
                    "scan_reason": result.get("reason", ""),
                    "scan_stage": scan_stage,
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
                not in (existing_target_symbols if scan_mode == DAILY_SCAN_MODE_TOPUP else manual_retained_symbols)
            ]),
            "candidates": candidate_count + sum(
                1
                for row in (existing_rows if scan_mode == DAILY_SCAN_MODE_TOPUP else manual_rows.values())
                if str(row.get("status", "")).strip().lower() == "candidate"
            ),
            "active": active_count + sum(
                1
                for row in (existing_rows if scan_mode == DAILY_SCAN_MODE_TOPUP else manual_rows.values())
                if str(row.get("status", "")).strip().lower() == "active"
            ),
            "removed": removed,
            "errors": errors,
            "new_targets": new_targets,
            "new_active": sum(1 for row in new_targets if row.get("status") == "active"),
            "new_candidates": sum(1 for row in new_targets if row.get("status") == "candidate"),
            "trade_subscription_budget": trade_budget,
            "manual_active_count": manual_active_count,
            "manual_retained_count": len(manual_retained_symbols),
            "engine_materialize": engine_materialize,
            "data_completeness": {
                "enabled": bool(completeness_gate.get("enabled")),
                "blocking_enabled": bool(completeness_gate.get("blocking_enabled")),
                "status": str(completeness_gate.get("status") or ("ready" if not incomplete_symbols else "repairing")),
                "intervals": list(completeness_gate.get("intervals") or []),
                "excluded_incomplete_count": len(blocking_incomplete_symbols),
                "repairing_count": len(incomplete_symbols),
                "incomplete_symbol_count": len(incomplete_symbols),
                "incomplete_symbols": sorted(incomplete_symbols),
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
