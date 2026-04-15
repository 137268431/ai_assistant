from __future__ import annotations

import time

from ibkr_compute.market.timeframe_utils import HIGHER_INTERVALS, bucket_start_ms, format_us_time


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceIntegrityMixin:
    def _collect_bar_integrity_snapshot(
        self,
        symbol: str,
        min_bars: int,
        gap_lookback: int,
        rollup_repair_enabled: bool,
    ) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        snapshot = self.data_backfill.get_integrity_snapshot(
            normalized_symbol,
            "5m",
            min_bars=min_bars,
            gap_lookback=gap_lookback,
        )
        derived_sync = self._inspect_derived_interval_sync(normalized_symbol) if rollup_repair_enabled else {
            "symbol": normalized_symbol,
            "latest_5m_ms": int(snapshot.get("latest_stored_ms", 0) or 0),
            "missing_intervals": [],
            "stale_intervals": [],
            "latest_interval_ms": {},
            "expected_closed_ms": {},
        }

        reasons = []
        if int(snapshot.get("stored_bar_count", 0) or 0) < min_bars:
            reasons.append(f"bars<{min_bars}")
        if int(snapshot.get("gap_count", 0) or 0) > 0:
            reasons.append(f"gaps={int(snapshot.get('gap_count', 0) or 0)}")
        if int(snapshot.get("duplicate_count", 0) or 0) > 0:
            reasons.append(f"duplicates={int(snapshot.get('duplicate_count', 0) or 0)}")
        if int(snapshot.get("bad_ohlc_count", 0) or 0) > 0:
            reasons.append(f"bad_ohlc={int(snapshot.get('bad_ohlc_count', 0) or 0)}")
        if derived_sync["missing_intervals"]:
            reasons.append(f"rollup_missing={','.join(derived_sync['missing_intervals'])}")
        if derived_sync["stale_intervals"]:
            reasons.append(f"rollup_stale={','.join(derived_sync['stale_intervals'])}")

        needs_history_fetch = (
            int(snapshot.get("stored_bar_count", 0) or 0) < min_bars
            or int(snapshot.get("gap_count", 0) or 0) > 0
        )
        needs_manual_review = (
            int(snapshot.get("duplicate_count", 0) or 0) > 0
            or int(snapshot.get("bad_ohlc_count", 0) or 0) > 0
        )
        needs_pipeline_repair = bool(
            needs_history_fetch
            or derived_sync["missing_intervals"]
            or derived_sync["stale_intervals"]
        )
        integrity_status = "ok"
        if needs_manual_review:
            integrity_status = "error"
        elif needs_pipeline_repair:
            integrity_status = "warn"

        snapshot["derived_sync"] = derived_sync
        snapshot["needs_history_fetch"] = needs_history_fetch
        snapshot["needs_manual_review"] = needs_manual_review
        snapshot["needs_pipeline_repair"] = needs_pipeline_repair
        snapshot["needs_repair"] = needs_pipeline_repair
        snapshot["safe_repair"] = needs_pipeline_repair and not needs_manual_review
        snapshot["repair_reason"] = ",".join(reasons)
        snapshot["integrity_status"] = integrity_status
        return snapshot

    def _build_bar_integrity_row(self, snapshot: dict, scan_scope: str, repair_state: dict | None = None) -> dict:
        service_mod = _service_mod()
        symbol = str(snapshot.get("symbol") or "").strip().upper()
        derived_sync = snapshot.get("derived_sync") or {}
        missing_intervals = list(derived_sync.get("missing_intervals") or [])
        stale_intervals = list(derived_sync.get("stale_intervals") or [])
        repair_state = repair_state or {}
        repair_attempted = bool(repair_state.get("attempted"))

        status = str(snapshot.get("integrity_status") or "ok")
        if repair_attempted:
            if bool(snapshot.get("needs_pipeline_repair")):
                status = "repair_failed"
            elif status == "ok":
                status = "repaired"

        latest_stored_ms = int(snapshot.get("latest_stored_ms", 0) or 0)
        return {
            "environment": service_mod.ENVIRONMENT,
            "market_date": self._bar_integrity_market_date(),
            "symbol": symbol,
            "interval": "5m",
            "scan_scope": str(scan_scope or "manual"),
            "status": status,
            "needs_repair": bool(snapshot.get("needs_pipeline_repair")),
            "safe_repair": bool(snapshot.get("safe_repair")),
            "bar_count": int(snapshot.get("stored_bar_count", 0) or 0),
            "latest_bar_time_ms": latest_stored_ms,
            "latest_bar_us_time": str(format_us_time(latest_stored_ms) or "") if latest_stored_ms > 0 else "",
            "oldest_loaded_ms": int(snapshot.get("oldest_loaded_ms", 0) or 0),
            "gap_count": int(snapshot.get("gap_count", 0) or 0),
            "duplicate_count": int(snapshot.get("duplicate_count", 0) or 0),
            "bad_ohlc_count": int(snapshot.get("bad_ohlc_count", 0) or 0),
            "missing_intervals": missing_intervals,
            "stale_intervals": stale_intervals,
            "gap_examples": list(snapshot.get("gap_examples") or []),
            "duplicate_examples": list(snapshot.get("duplicate_examples") or []),
            "bad_ohlc_examples": list(snapshot.get("bad_ohlc_examples") or []),
            "repair_attempts": 1 if repair_attempted else 0,
            "increment_repair_attempts": repair_attempted,
            "last_scan_at": self._now_iso(),
            "last_repair_at": self._now_iso() if repair_attempted else "",
            "last_repair_result": {
                **(repair_state.get("result") or {}),
                "attempted": repair_attempted,
            } if repair_attempted else {},
            "extra": {
                "repair_reason": str(snapshot.get("repair_reason") or ""),
                "needs_history_fetch": bool(snapshot.get("needs_history_fetch")),
                "needs_manual_review": bool(snapshot.get("needs_manual_review")),
                "derived_sync": derived_sync,
                "scanned_row_count": int(snapshot.get("scanned_row_count", 0) or 0),
                "scan_scope": str(scan_scope or "manual"),
                "source": "ibkr_service",
            },
        }

    def _run_bar_integrity_repairs(
        self,
        snapshots: dict[str, dict],
        source: str,
        allow_defer: bool = True,
        run_pipeline_repair: bool = True,
        history_period_overrides: dict[str, dict] | None = None,
    ) -> dict:
        service_mod = _service_mod()
        repair_symbols = [
            symbol for symbol, snapshot in snapshots.items()
            if bool(snapshot.get("safe_repair"))
        ]
        history_symbols = [
            symbol for symbol in repair_symbols
            if bool((snapshots.get(symbol) or {}).get("needs_history_fetch"))
        ]
        per_symbol = {
            symbol: {
                "attempted": symbol in repair_symbols,
                "result": {
                    "source": source,
                    "history_needed": bool((snapshots.get(symbol) or {}).get("needs_history_fetch")),
                    "pipeline_needed": bool((snapshots.get(symbol) or {}).get("needs_pipeline_repair")),
                },
            }
            for symbol in snapshots.keys()
        }
        if not repair_symbols:
            return {
                "repair_symbols": [],
                "history_symbols": [],
                "per_symbol": per_symbol,
            }

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if allow_defer and defer_repairs:
            for symbol in repair_symbols:
                per_symbol[symbol]["result"]["deferred"] = True
                per_symbol[symbol]["result"]["defer_reason"] = str(
                    defer_snapshot.get("reason") or "realtime_priority_active"
                )
                per_symbol[symbol]["result"]["queue_size"] = int(
                    defer_snapshot.get("queue_size", 0) or 0
                )
            service_mod.logger.info(
                "Pipeline repair deferred (%s): symbols=%s reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                source,
                ",".join(sorted(repair_symbols)),
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )
            return {
                "repair_symbols": [],
                "history_symbols": [],
                "deferred_symbols": sorted(repair_symbols),
                "deferred": True,
                "reason": str(defer_snapshot.get("reason") or "realtime_priority_active"),
                "per_symbol": per_symbol,
            }

        backfill_result = {}
        unresolved_history = []
        conid_map = self.conid_resolver.resolve_bulk(history_symbols) if history_symbols else {}
        if history_symbols:
            unresolved_history = [symbol for symbol in history_symbols if symbol not in conid_map]
            for symbol in unresolved_history:
                per_symbol[symbol]["result"]["history_error"] = "conid_unresolved"
            if conid_map:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                effective_period_overrides = {
                    symbol: dict((history_period_overrides or {}).get(symbol) or {})
                    for symbol in conid_map.keys()
                    if (history_period_overrides or {}).get(symbol)
                }
                backfill_result = self.data_backfill.backfill_all(
                    conid_map,
                    symbol_meta=symbol_meta,
                    intervals=["5m"],
                    repair_symbols=list(conid_map.keys()),
                    period_overrides=effective_period_overrides,
                )
                self.data_writer.flush()
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(conid_map.keys())
                for symbol in conid_map.keys():
                    per_symbol[symbol]["result"]["history_written"] = int(
                        ((backfill_result.get(symbol) or {}).get("5m", 0) or 0)
                    )
                    history_period = str(
                        ((effective_period_overrides.get(symbol) or {}).get("5m") or "")
                    ).strip()
                    if history_period:
                        per_symbol[symbol]["result"]["history_period"] = history_period

        pipeline_result = {
            "ok": True,
            "symbols": sorted(repair_symbols),
            "compute": {},
            "rollup": {},
            "skipped": not run_pipeline_repair,
        }
        if run_pipeline_repair:
            pipeline_result = self._run_symbol_pipeline_repair(repair_symbols, source=source)
        pipeline_ok = bool(pipeline_result.get("ok", False))
        for symbol in repair_symbols:
            per_symbol[symbol]["result"]["pipeline_ok"] = pipeline_ok
            per_symbol[symbol]["result"]["pipeline"] = {
                "processed": int(((pipeline_result.get("compute") or {}).get("processed", 0) or 0)),
                "errors": int(((pipeline_result.get("compute") or {}).get("errors", 0) or 0)),
                "rollup_written": int(((pipeline_result.get("rollup") or {}).get("written", 0) or 0)),
                "skipped": bool(pipeline_result.get("skipped", False)),
            }

        return {
            "repair_symbols": sorted(repair_symbols),
            "history_symbols": sorted(history_symbols),
            "unresolved_history_symbols": sorted(unresolved_history),
            "per_symbol": per_symbol,
        }

    def scan_bar_integrity(
        self,
        symbols,
        scan_scope: str = "manual",
        persist: bool = True,
        repair: bool = False,
    ) -> dict:
        service_mod = _service_mod()
        normalized_symbols = sorted(
            {str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()}
        )
        if not normalized_symbols:
            return {"ok": True, "rows": [], "summary": {"symbols": [], "repair_candidate_symbols": []}}

        self.config.refresh()
        self._refresh_runtime_settings()
        min_bars = max(
            60,
            self.config.get_int_for_environment(
                "ibkr_history_repair_min_bars_5m",
                service_mod.ENVIRONMENT,
                260,
            ),
        )
        gap_lookback = max(
            20,
            self.config.get_int_for_environment(
                "ibkr_history_repair_gap_lookback",
                service_mod.ENVIRONMENT,
                80,
            ),
        )
        rollup_repair_enabled = self.config.get_bool_for_environment(
            "ibkr_history_repair_rollup_enabled",
            service_mod.ENVIRONMENT,
            True,
        )

        snapshots = {
            symbol: self._collect_bar_integrity_snapshot(
                symbol,
                min_bars=min_bars,
                gap_lookback=gap_lookback,
                rollup_repair_enabled=rollup_repair_enabled,
            )
            for symbol in normalized_symbols
        }
        initial_repair_symbols = sorted(
            symbol for symbol, snapshot in snapshots.items()
            if bool(snapshot.get("needs_pipeline_repair"))
        )
        initial_repair_reasons = {
            symbol: str((snapshot.get("repair_reason") or ""))
            for symbol, snapshot in snapshots.items()
            if str(snapshot.get("repair_reason") or "")
        }
        repair_summary = {"repair_symbols": [], "history_symbols": [], "per_symbol": {}}
        if repair:
            repair_summary = self._run_bar_integrity_repairs(
                snapshots,
                source=f"{scan_scope}_integrity",
            )
            for symbol in repair_summary.get("repair_symbols") or []:
                snapshots[symbol] = self._collect_bar_integrity_snapshot(
                    symbol,
                    min_bars=min_bars,
                    gap_lookback=gap_lookback,
                    rollup_repair_enabled=rollup_repair_enabled,
                )

        rows = [
            self._build_bar_integrity_row(
                snapshots[symbol],
                scan_scope=scan_scope,
                repair_state=(repair_summary.get("per_symbol") or {}).get(symbol),
            )
            for symbol in normalized_symbols
        ]
        if persist and rows:
            try:
                self.pb.upsert_bar_integrity_items(rows)
            except Exception as exc:
                service_mod.logger.warning("Failed to persist bar integrity rows: %s", exc)

        summary = {
            "symbols": normalized_symbols,
            "scan_scope": scan_scope,
            "attempted_repair_symbols": sorted(repair_summary.get("repair_symbols") or []),
            "repair_candidate_symbols": sorted(
                symbol for symbol, snapshot in snapshots.items()
                if bool(snapshot.get("needs_pipeline_repair"))
            ),
            "initial_repair_symbols": initial_repair_symbols,
            "initial_repair_reasons": initial_repair_reasons,
            "manual_review_symbols": sorted(
                symbol for symbol, snapshot in snapshots.items()
                if bool(snapshot.get("needs_manual_review"))
            ),
            "history_fetch_symbols": sorted(repair_summary.get("history_symbols") or []),
            "repair_reasons": {
                symbol: str((snapshots.get(symbol) or {}).get("repair_reason") or "")
                for symbol in normalized_symbols
                if str((snapshots.get(symbol) or {}).get("repair_reason") or "")
            },
            "status_counts": {
                "ok": sum(1 for row in rows if row.get("status") == "ok"),
                "warn": sum(1 for row in rows if row.get("status") == "warn"),
                "error": sum(1 for row in rows if row.get("status") == "error"),
                "repaired": sum(1 for row in rows if row.get("status") == "repaired"),
                "repair_failed": sum(1 for row in rows if row.get("status") == "repair_failed"),
            },
        }
        return {
            "ok": True,
            "rows": rows,
            "summary": summary,
        }

    def _build_history_repair_plan(self, symbols: list[str]) -> dict[str, dict]:
        service_mod = _service_mod()
        if not symbols:
            return {}
        if not self.config.get_bool_for_environment("ibkr_history_repair_enabled", service_mod.ENVIRONMENT, True):
            return {}

        min_bars = max(
            60,
            self.config.get_int_for_environment(
                "ibkr_history_repair_min_bars_5m",
                service_mod.ENVIRONMENT,
                260,
            ),
        )
        gap_lookback = max(
            20,
            self.config.get_int_for_environment(
                "ibkr_history_repair_gap_lookback",
                service_mod.ENVIRONMENT,
                80,
            ),
        )
        rollup_repair_enabled = self.config.get_bool_for_environment(
            "ibkr_history_repair_rollup_enabled",
            service_mod.ENVIRONMENT,
            True,
        )
        plan = {}

        for symbol in sorted({str(item or "").upper() for item in symbols if str(item or "").strip()}):
            snapshot = self._collect_bar_integrity_snapshot(
                symbol,
                min_bars=min_bars,
                gap_lookback=gap_lookback,
                rollup_repair_enabled=rollup_repair_enabled,
            )
            if bool(snapshot.get("needs_pipeline_repair")):
                plan[symbol] = snapshot
        return plan

    def _build_startup_history_repair_plan(self, symbols: list[str]) -> dict[str, dict]:
        service_mod = _service_mod()
        if not symbols:
            return {}
        if not self.config.get_bool_for_environment("ibkr_history_repair_enabled", service_mod.ENVIRONMENT, True):
            return {}

        plan = {}
        for symbol in sorted({str(item or "").upper() for item in symbols if str(item or "").strip()}):
            snapshot = self._collect_startup_history_repair_snapshot(symbol)
            if bool(snapshot.get("needs_pipeline_repair")):
                plan[symbol] = snapshot
        return plan

    def _startup_history_repair_period(self, snapshot: dict) -> str:
        service_mod = _service_mod()
        reasons = [
            str(item or "").strip()
            for item in str((snapshot or {}).get("repair_reason") or "").split(",")
            if str(item or "").strip()
        ]
        if not reasons:
            return ""
        warmup_period = f"{self._live_warmup_days()}d"
        overlap_period = f"{self._restart_overlap_days()}d"
        if any(
            reason.startswith("bars<") or reason.startswith("startup_snapshot_error:")
            for reason in reasons
        ):
            return warmup_period
        if all(reason.startswith("today_regular_") for reason in reasons):
            return overlap_period or service_mod.STARTUP_HISTORY_REPAIR_SHORT_PERIOD
        if any(reason.startswith("gaps=") for reason in reasons):
            return overlap_period
        return warmup_period

    def _build_startup_history_period_overrides(self, repair_plan: dict[str, dict]) -> dict[str, dict]:
        overrides = {}
        for symbol, snapshot in (repair_plan or {}).items():
            period = self._startup_history_repair_period(snapshot)
            if period:
                overrides[str(symbol).upper()] = {"5m": period}
        return overrides

    def _inspect_derived_interval_sync(self, symbol: str) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        result = {
            "symbol": normalized_symbol,
            "latest_5m_ms": 0,
            "missing_intervals": [],
            "stale_intervals": [],
            "latest_interval_ms": {},
            "expected_closed_ms": {},
        }
        if not normalized_symbol:
            return result

        base_row = self.pb.get_first_record(
            "ibkr_bars",
            filter=(
                f'symbol = "{normalized_symbol}" && '
                'interval = "5m" && '
                f'{self._build_bar_environment_filter()}'
            ),
            sort="-bar_time_ms",
        )
        latest_5m_ms = int((base_row or {}).get("bar_time_ms", 0) or 0)
        result["latest_5m_ms"] = latest_5m_ms
        if latest_5m_ms <= 0:
            return result

        for interval in HIGHER_INTERVALS:
            row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    f'interval = "{interval}" && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
            )
            latest_interval_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            result["latest_interval_ms"][interval] = latest_interval_ms

            current_bucket_ms = bucket_start_ms(latest_5m_ms, interval)
            previous_source_row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    'interval = "5m" && '
                    f'bar_time_ms < {int(current_bucket_ms)} && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
            )
            previous_source_ms = int((previous_source_row or {}).get("bar_time_ms", 0) or 0)
            expected_closed_ms = bucket_start_ms(previous_source_ms, interval) if previous_source_ms > 0 else 0
            result["expected_closed_ms"][interval] = expected_closed_ms

            if expected_closed_ms <= 0:
                continue
            if latest_interval_ms <= 0:
                result["missing_intervals"].append(interval)
            elif latest_interval_ms < expected_closed_ms:
                result["stale_intervals"].append(interval)

        return result

    def _build_bar_environment_filter(self) -> str:
        service_mod = _service_mod()
        runtime_environment = str(service_mod.ENVIRONMENT or "").strip().lower() or "live"
        clauses = [f'environment = "{runtime_environment}"']
        if runtime_environment == "live":
            clauses.append('environment = ""')
        return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]

    def _run_symbol_pipeline_repair(self, symbols: list[str], source: str) -> dict:
        service_mod = _service_mod()
        normalized_symbols = sorted(
            {str(symbol or "").upper() for symbol in symbols if str(symbol or "").strip()}
        )
        if not normalized_symbols:
            return {"ok": True, "symbols": []}
        try:
            from ibkr_compute.api import server as compute_server

            result = compute_server.repair_symbol_pipeline_from_storage(
                service_mod.ENVIRONMENT,
                normalized_symbols,
            )
            self._last_pipeline_repair_at = time.time()
            self._last_pipeline_repair_symbols = normalized_symbols
            service_mod.logger.info(
                "Pipeline repair finished (%s): symbols=%s processed=%s rollup_written=%s errors=%s",
                source,
                ",".join(normalized_symbols),
                ((result.get("compute") or {}).get("processed", 0)),
                ((result.get("rollup") or {}).get("written", 0)),
                ((result.get("compute") or {}).get("errors", 0)),
            )
            return result
        except Exception as exc:
            service_mod.logger.warning("Pipeline repair failed (%s): %s", source, exc)
            return {"ok": False, "symbols": normalized_symbols, "error": str(exc)}
