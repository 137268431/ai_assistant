from __future__ import annotations

import time

from ibkr_compute.market.timeframe_utils import format_us_time, latest_safe_closed_bucket_ms, normalize_interval

from .runtime_pipeline_support import _service_mod


class RuntimePipelineDirectTopupMixin:
    def _runtime_direct_topup_enabled(self) -> bool:
            service_mod = _service_mod()
            return self.config.get_bool_for_environment(
                "ibkr_runtime_direct_topup_enabled",
                service_mod.DATA_ENVIRONMENT,
                False,
            )

    def _runtime_direct_topup_intervals(self) -> list[str]:
            service_mod = _service_mod()
            raw_value = str(
                self.config.get_for_environment(
                    "ibkr_runtime_direct_topup_intervals",
                    service_mod.DATA_ENVIRONMENT,
                    ",".join(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS),
                )
                or ""
            )
            allowed = {normalize_interval(interval) for interval in service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS}
            intervals = []
            for item in raw_value.replace(";", ",").split(","):
                normalized = normalize_interval(item)
                if normalized in {"all", "*"}:
                    return list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS)
                if normalized in allowed and normalized not in intervals:
                    intervals.append(normalized)
            return intervals or list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS)

    def _runtime_direct_topup_close_delay_sec(self) -> int:
            service_mod = _service_mod()
            return max(
                1,
                self.config.get_int_for_environment(
                    "ibkr_runtime_direct_topup_close_delay_sec",
                    service_mod.DATA_ENVIRONMENT,
                    service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS,
                ),
            )

    def _runtime_direct_topup_loop_interval_sec(self) -> float:
            service_mod = _service_mod()
            return max(
                1.0,
                self.config.get_float_for_environment(
                    "ibkr_runtime_direct_topup_loop_interval_sec",
                    service_mod.DATA_ENVIRONMENT,
                    service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS,
                ),
            )

    def _runtime_direct_topup_parallel_enabled(self) -> bool:
            service_mod = _service_mod()
            return self.config.get_bool_for_environment(
                "ibkr_runtime_direct_topup_parallel_enabled",
                service_mod.DATA_ENVIRONMENT,
                bool(getattr(service_mod, "DEFAULT_RUNTIME_DIRECT_TOPUP_PARALLEL_ENABLED", False)),
            )

    def _runtime_direct_topup_wait_for_watchlist_5m_enabled(self) -> bool:
            service_mod = _service_mod()
            return self.config.get_bool_for_environment(
                "ibkr_runtime_direct_topup_wait_for_watchlist_5m_enabled",
                service_mod.DATA_ENVIRONMENT,
                False,
            )

    def _runtime_direct_topup_interval_priority(self) -> list[str]:
            service_mod = _service_mod()
            fallback = tuple(
                getattr(
                    service_mod,
                    "DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVAL_PRIORITY",
                    ("4h", "1h", "30m", "15m", "1d"),
                )
                or ("4h", "1h", "30m", "15m", "1d")
            )
            raw_value = str(
                self.config.get_for_environment(
                    "ibkr_runtime_direct_topup_interval_priority",
                    service_mod.DATA_ENVIRONMENT,
                    ",".join(fallback),
                )
                or ""
            )
            allowed = {normalize_interval(interval) for interval in service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS}
            priority = []
            for item in raw_value.replace(";", ",").split(","):
                normalized = normalize_interval(item)
                if normalized in allowed and normalized not in priority:
                    priority.append(normalized)
            if priority:
                return priority
            return [
                normalize_interval(interval)
                for interval in fallback
                if normalize_interval(interval) in allowed
            ] or list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS)

    def _runtime_direct_topup_order_intervals(self, intervals: list[str]) -> list[str]:
            normalized_intervals = []
            for interval in intervals:
                normalized = normalize_interval(interval)
                if normalized and normalized not in normalized_intervals:
                    normalized_intervals.append(normalized)
            priority = self._runtime_direct_topup_interval_priority()
            ordered = [interval for interval in priority if interval in normalized_intervals]
            ordered.extend(interval for interval in normalized_intervals if interval not in ordered)
            return ordered

    def _runtime_direct_topup_request_period(self, interval: str) -> str:
            service_mod = _service_mod()
            normalized = normalize_interval(interval)
            fallback = service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_PERIODS.get(normalized, "2d")
            return str(
                self.config.get_for_environment(
                    f"ibkr_runtime_direct_topup_period_{normalized}",
                    service_mod.DATA_ENVIRONMENT,
                    fallback,
                )
                or fallback
            ).strip() or fallback

    def _initial_direct_topup_state(self) -> dict:
            service_mod = _service_mod()
            return {
                "enabled": False,
                "driver": "ibkr_history_direct_topup",
                "intervals": list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS),
                "close_delay_sec": service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS,
                "loop_interval_s": service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS,
                "parallel_enabled": bool(getattr(service_mod, "DEFAULT_RUNTIME_DIRECT_TOPUP_PARALLEL_ENABLED", False)),
                "wait_for_watchlist_5m": False,
                "interval_priority": list(
                    getattr(
                        service_mod,
                        "DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVAL_PRIORITY",
                        ("4h", "1h", "30m", "15m", "1d"),
                    )
                    or ("4h", "1h", "30m", "15m", "1d")
                ),
                "last_run": "",
                "last_error": "",
                "total_written_bars": 0,
                "last_completed_interval": "",
                "intervals_state": {},
            }

    def _copy_direct_topup_state(self, source: dict | None = None) -> dict:
            payload = source if source is not None else self._direct_topup_state
            copied = {}
            for key, value in payload.items():
                if isinstance(value, dict):
                    copied[key] = {
                        nested_key: dict(nested_value) if isinstance(nested_value, dict) else list(nested_value) if isinstance(nested_value, list) else nested_value
                        for nested_key, nested_value in value.items()
                    }
                elif isinstance(value, list):
                    copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    copied[key] = value
            return copied

    def _set_direct_topup_state(self, **updates) -> dict:
            with self._direct_topup_lock:
                next_state = self._copy_direct_topup_state()
                for key, value in updates.items():
                    if isinstance(value, dict):
                        next_state[key] = {
                            nested_key: dict(nested_value) if isinstance(nested_value, dict) else list(nested_value) if isinstance(nested_value, list) else nested_value
                            for nested_key, nested_value in value.items()
                        }
                    elif isinstance(value, list):
                        next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                    else:
                        next_state[key] = value
                self._direct_topup_state = next_state
                return self._copy_direct_topup_state(next_state)

    def _runtime_direct_topup_latest_due_ms(self, interval: str) -> int:
            return latest_safe_closed_bucket_ms(
                normalize_interval(interval),
                delay_seconds=self._runtime_direct_topup_close_delay_sec(),
                now_ms=int(time.time() * 1000),
            )

    def _runtime_direct_topup_5m_is_current(self) -> bool:
            official_5m = self._copy_official_5m_state()
            due_bucket_ms = self._latest_safe_closed_5m_ms()
            completed_bucket_ms = int(official_5m.get("last_completed_bucket_ms", 0) or 0)
            if due_bucket_ms > 0 and completed_bucket_ms < due_bucket_ms:
                return False
            if int(official_5m.get("pending_symbols_total", 0) or 0) > 0:
                return False
            return True

    def _runtime_direct_topup_watchlist_5m_pending(self) -> bool:
            def _count(value) -> int:
                try:
                    return int(value or 0)
                except Exception:
                    return 0

            snapshot_getter = getattr(self, "_watchlist_idle_topup_completion_snapshot", None)
            if not callable(snapshot_getter):
                return False
            try:
                completion = snapshot_getter()
            except Exception:
                return False
            if not isinstance(completion, dict):
                return False
            pending_count = (
                _count(completion.get("stale"))
                + _count(completion.get("missing"))
                + _count(completion.get("unobserved"))
            )
            return pending_count > 0

    def _runtime_direct_topup_symbols(self) -> tuple[list[str], dict, dict]:
            snapshot = self._warmup_snapshot_from_subscriptions()
            snapshot_symbols = list(snapshot.get("symbols") or [])
            if "trade_symbols" in snapshot:
                trade_set = set(self._normalize_symbol_list(snapshot.get("trade_symbols") or []))
                symbols = [symbol for symbol in snapshot_symbols if symbol in trade_set]
            else:
                symbols = snapshot_symbols
            normalized_symbols = self._normalize_symbol_list(symbols)
            return (
                normalized_symbols,
                dict(snapshot.get("conid_map") or {}),
                dict(snapshot.get("symbol_meta") or {}),
            )

    def _run_runtime_direct_topup_interval(
            self,
            interval: str,
            symbols: list[str],
            conid_map: dict,
            symbol_meta: dict,
            state: dict,
        ) -> dict:
            service_mod = _service_mod()
            normalized_interval = normalize_interval(interval)
            due_bucket_ms = self._runtime_direct_topup_latest_due_ms(normalized_interval)
            interval_states = dict(state.get("intervals_state") or {})
            interval_state = dict(interval_states.get(normalized_interval) or {})
            last_completed_bucket_ms = int(interval_state.get("last_completed_bucket_ms", 0) or 0)
            request_period = self._runtime_direct_topup_request_period(normalized_interval)

            base_update = {
                **interval_state,
                "enabled": True,
                "request_period": request_period,
                "last_due_bucket_ms": due_bucket_ms,
                "last_due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
            }
            if due_bucket_ms <= 0:
                base_update["status"] = "waiting"
                base_update["reason"] = "no_safe_bucket"
                return base_update
            if due_bucket_ms <= last_completed_bucket_ms:
                base_update["status"] = "idle"
                base_update["reason"] = "already_completed"
                return base_update
            if not symbols:
                base_update.update(
                    {
                        "status": "skipped",
                        "reason": "no_trade_symbols",
                        "last_run": self._now_iso(),
                        "last_completed_bucket_ms": due_bucket_ms,
                        "last_completed_bucket_us": format_us_time(due_bucket_ms),
                        "last_written_bars": 0,
                        "written_symbols": [],
                        "written_symbols_total": 0,
                        "pending_symbols": [],
                        "pending_symbols_total": 0,
                        "last_error": "",
                    }
                )
                return base_update

            runnable_conids = {
                symbol: int(conid_map.get(symbol) or 0)
                for symbol in symbols
                if int(conid_map.get(symbol) or 0) > 0
            }
            missing_conid_symbols = [symbol for symbol in symbols if symbol not in runnable_conids]
            if not runnable_conids:
                base_update.update(
                    {
                        "status": "pending",
                        "reason": "missing_conids",
                        "last_run": self._now_iso(),
                        "last_written_bars": 0,
                        "written_symbols": [],
                        "written_symbols_total": 0,
                        "pending_symbols": missing_conid_symbols,
                        "pending_symbols_total": len(missing_conid_symbols),
                        "last_error": "missing_conids",
                    }
                )
                return base_update

            started_at = time.time()
            base_update.update(
                {
                    "status": "running",
                    "reason": "",
                    "last_run": self._now_iso(),
                    "symbol_count": len(symbols),
                    "missing_conid_symbols": missing_conid_symbols,
                    "last_error": "",
                }
            )
            interval_states[normalized_interval] = base_update
            self._set_direct_topup_state(
                enabled=self._runtime_direct_topup_enabled(),
                intervals=self._runtime_direct_topup_intervals(),
                close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
                loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
                last_run=self._now_iso(),
                intervals_state=interval_states,
            )

            written_bars = 0
            written_symbols = []
            last_error = ""
            try:
                period_overrides = {
                    symbol: {normalized_interval: request_period}
                    for symbol in runnable_conids
                }
                results = self.data_backfill.backfill_all(
                    runnable_conids,
                    symbol_meta=symbol_meta,
                    intervals=[normalized_interval],
                    repair_symbols=[],
                    period_overrides=period_overrides,
                    trace_source="runtime_direct_topup",
                )
                self.data_writer.flush()
                for symbol, payload in (results or {}).items():
                    count = int((payload or {}).get(normalized_interval, 0) or 0)
                    if count > 0:
                        written_symbols.append(symbol)
                        written_bars += count
                compute_result = {}
                if runnable_conids:
                    compute_result = self._trigger_realtime_compute(
                        source="direct_history_topup",
                        symbols=list(runnable_conids.keys()),
                        persist_signals=False,
                        intervals=[normalized_interval],
                        rollup_intervals=[],
                    )
                    if compute_result.get("ok") is False:
                        last_error = str(compute_result.get("error") or "compute_failed")
            except Exception as exc:
                service_mod.logger.warning(
                    "Runtime direct %s top-up failed: %s",
                    normalized_interval,
                    exc,
                )
                last_error = str(exc)

            verified_symbols = []
            pending_freshness_symbols = []
            freshness_by_symbol = {}
            if not last_error:
                planner = getattr(self, "bar_freshness_planner", None)
                if planner is None:
                    verified_symbols = list(runnable_conids.keys())
                    pending_freshness_symbols = []
                    freshness_by_symbol = {symbol: {"status": "not_checked", "reason": "planner_unavailable"} for symbol in runnable_conids.keys()}
                else:
                    for symbol in runnable_conids.keys():
                        try:
                            freshness = planner.plan_symbol(
                                symbol,
                                [normalized_interval],
                                environment=service_mod.DATA_ENVIRONMENT,
                                required_bars=0,
                            )
                            interval_payload = (freshness.get("intervals") or {}).get(normalized_interval) or {}
                            latest_ms = int(interval_payload.get("latest_stored_ms", 0) or 0)
                            freshness_by_symbol[symbol] = interval_payload
                            if latest_ms >= due_bucket_ms:
                                verified_symbols.append(symbol)
                            else:
                                pending_freshness_symbols.append(symbol)
                        except Exception as exc:
                            freshness_by_symbol[symbol] = {"status": "verify_failed", "error": str(exc)}
                            pending_freshness_symbols.append(symbol)
                if pending_freshness_symbols:
                    last_error = "freshness_pending_after_topup"

            completed = not last_error
            base_update.update(
                {
                    "status": "completed" if completed else ("pending" if pending_freshness_symbols else "failed"),
                    "reason": "" if completed else str(last_error or "topup_failed"),
                    "last_completed_bucket_ms": due_bucket_ms if completed else last_completed_bucket_ms,
                    "last_completed_bucket_us": format_us_time(due_bucket_ms) if completed else str(interval_state.get("last_completed_bucket_us") or ""),
                    "last_written_bars": written_bars,
                    "written_symbols": sorted(written_symbols),
                    "written_symbols_total": len(written_symbols),
                    "verified_symbols": sorted(verified_symbols),
                    "verified_symbols_total": len(verified_symbols),
                    "pending_symbols": [] if completed else sorted(pending_freshness_symbols or runnable_conids.keys()),
                    "pending_symbols_total": 0 if completed else len(pending_freshness_symbols or runnable_conids),
                    "freshness_by_symbol": freshness_by_symbol,
                    "duration_s": round(max(0.0, time.time() - started_at), 3),
                    "last_error": last_error,
                }
            )
            return base_update

    def _run_runtime_direct_topup_intervals_batch(
            self,
            intervals: list[str],
            symbols: list[str],
            conid_map: dict,
            symbol_meta: dict,
            state: dict,
        ) -> tuple[dict, list[str], int, str]:
            service_mod = _service_mod()
            interval_states = dict(state.get("intervals_state") or {})
            runnable_conids = {
                symbol: int(conid_map.get(symbol) or 0)
                for symbol in symbols
                if int(conid_map.get(symbol) or 0) > 0
            }
            missing_conid_symbols = [symbol for symbol in symbols if symbol not in runnable_conids]
            run_specs = []
            ran_intervals = []
            errors = []

            for interval in intervals:
                normalized_interval = normalize_interval(interval)
                due_bucket_ms = self._runtime_direct_topup_latest_due_ms(normalized_interval)
                interval_state = dict(interval_states.get(normalized_interval) or {})
                last_completed_bucket_ms = int(interval_state.get("last_completed_bucket_ms", 0) or 0)
                request_period = self._runtime_direct_topup_request_period(normalized_interval)
                base_update = {
                    **interval_state,
                    "enabled": True,
                    "request_period": request_period,
                    "last_due_bucket_ms": due_bucket_ms,
                    "last_due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
                }
                if due_bucket_ms <= 0:
                    base_update["status"] = "waiting"
                    base_update["reason"] = "no_safe_bucket"
                    interval_states[normalized_interval] = base_update
                    continue
                if due_bucket_ms <= last_completed_bucket_ms:
                    base_update["status"] = "idle"
                    base_update["reason"] = "already_completed"
                    interval_states[normalized_interval] = base_update
                    continue
                ran_intervals.append(normalized_interval)
                if not symbols:
                    base_update.update(
                        {
                            "status": "skipped",
                            "reason": "no_trade_symbols",
                            "last_run": self._now_iso(),
                            "last_completed_bucket_ms": due_bucket_ms,
                            "last_completed_bucket_us": format_us_time(due_bucket_ms),
                            "last_written_bars": 0,
                            "written_symbols": [],
                            "written_symbols_total": 0,
                            "pending_symbols": [],
                            "pending_symbols_total": 0,
                            "last_error": "",
                        }
                    )
                    interval_states[normalized_interval] = base_update
                    continue
                if not runnable_conids:
                    base_update.update(
                        {
                            "status": "pending",
                            "reason": "missing_conids",
                            "last_run": self._now_iso(),
                            "symbol_count": len(symbols),
                            "missing_conid_symbols": missing_conid_symbols,
                            "last_written_bars": 0,
                            "written_symbols": [],
                            "written_symbols_total": 0,
                            "pending_symbols": missing_conid_symbols,
                            "pending_symbols_total": len(missing_conid_symbols),
                            "last_error": "missing_conids",
                        }
                    )
                    interval_states[normalized_interval] = base_update
                    errors.append(f"{normalized_interval}:missing_conids")
                    continue

                base_update.update(
                    {
                        "status": "running",
                        "reason": "",
                        "last_run": self._now_iso(),
                        "symbol_count": len(symbols),
                        "missing_conid_symbols": missing_conid_symbols,
                        "last_error": "",
                    }
                )
                interval_states[normalized_interval] = base_update
                run_specs.append(
                    {
                        "interval": normalized_interval,
                        "due_bucket_ms": due_bucket_ms,
                        "last_completed_bucket_ms": last_completed_bucket_ms,
                        "last_completed_bucket_us": str(interval_state.get("last_completed_bucket_us") or ""),
                        "request_period": request_period,
                    }
                )

            if not run_specs:
                return interval_states, ran_intervals, 0, ";".join(errors[:5])

            self._set_direct_topup_state(
                enabled=self._runtime_direct_topup_enabled(),
                intervals=list(state.get("intervals") or intervals),
                close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
                loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
                parallel_enabled=self._runtime_direct_topup_parallel_enabled(),
                interval_priority=self._runtime_direct_topup_interval_priority(),
                last_run=self._now_iso(),
                intervals_state=interval_states,
            )

            started_at = time.time()
            interval_list = [spec["interval"] for spec in run_specs]
            period_by_interval = {spec["interval"]: spec["request_period"] for spec in run_specs}
            written_by_interval = {interval: 0 for interval in interval_list}
            written_symbols_by_interval = {interval: [] for interval in interval_list}
            batch_error = ""
            flushed = False
            try:
                period_overrides = {
                    symbol: dict(period_by_interval)
                    for symbol in runnable_conids
                }
                results = self.data_backfill.backfill_all(
                    runnable_conids,
                    symbol_meta=symbol_meta,
                    intervals=interval_list,
                    repair_symbols=[],
                    period_overrides=period_overrides,
                    trace_source="runtime_direct_topup_parallel",
                )
                self.data_writer.flush()
                flushed = True
                for symbol, payload in (results or {}).items():
                    for interval in interval_list:
                        count = int((payload or {}).get(interval, 0) or 0)
                        if count > 0:
                            written_symbols_by_interval[interval].append(symbol)
                            written_by_interval[interval] += count
                compute_result = self._trigger_realtime_compute(
                    source="direct_history_topup",
                    symbols=list(runnable_conids.keys()),
                    persist_signals=False,
                    intervals=interval_list,
                    rollup_intervals=[],
                )
                if compute_result.get("ok") is False:
                    batch_error = str(compute_result.get("error") or "compute_failed")
            except Exception as exc:
                service_mod.logger.warning(
                    "Runtime direct batch top-up failed for %s: %s",
                    ",".join(interval_list),
                    exc,
                )
                batch_error = str(exc)
            finally:
                if not flushed and getattr(self, "data_writer", None) is not None:
                    try:
                        self.data_writer.flush()
                    except Exception as exc:
                        if not batch_error:
                            batch_error = str(exc)

            verified_by_interval = {interval: [] for interval in interval_list}
            pending_by_interval = {interval: [] for interval in interval_list}
            freshness_by_interval = {interval: {} for interval in interval_list}
            if not batch_error:
                planner = getattr(self, "bar_freshness_planner", None)
                if planner is None:
                    for interval in interval_list:
                        verified_by_interval[interval] = list(runnable_conids.keys())
                        freshness_by_interval[interval] = {
                            symbol: {"status": "not_checked", "reason": "planner_unavailable"}
                            for symbol in runnable_conids.keys()
                        }
                else:
                    due_by_interval = {spec["interval"]: int(spec["due_bucket_ms"] or 0) for spec in run_specs}
                    for symbol in runnable_conids.keys():
                        try:
                            freshness = planner.plan_symbol(
                                symbol,
                                interval_list,
                                environment=service_mod.DATA_ENVIRONMENT,
                                required_bars=0,
                            )
                            intervals_payload = freshness.get("intervals") or {}
                        except Exception as exc:
                            intervals_payload = {
                                interval: {"status": "verify_failed", "error": str(exc)}
                                for interval in interval_list
                            }
                        for interval in interval_list:
                            interval_payload = dict(intervals_payload.get(interval) or {})
                            freshness_by_interval[interval][symbol] = interval_payload
                            latest_ms = int(interval_payload.get("latest_stored_ms", 0) or 0)
                            if latest_ms >= due_by_interval[interval]:
                                verified_by_interval[interval].append(symbol)
                            else:
                                pending_by_interval[interval].append(symbol)

            total_written = 0
            for spec in run_specs:
                normalized_interval = spec["interval"]
                interval_error = batch_error
                pending_freshness_symbols = pending_by_interval.get(normalized_interval) or []
                if not interval_error and pending_freshness_symbols:
                    interval_error = "freshness_pending_after_topup"
                completed = not interval_error
                pending_symbols = [] if completed else sorted(pending_freshness_symbols or runnable_conids.keys())
                base_update = dict(interval_states.get(normalized_interval) or {})
                written_bars = int(written_by_interval.get(normalized_interval, 0) or 0)
                total_written += written_bars
                base_update.update(
                    {
                        "status": "completed" if completed else ("pending" if pending_freshness_symbols else "failed"),
                        "reason": "" if completed else str(interval_error or "topup_failed"),
                        "last_completed_bucket_ms": spec["due_bucket_ms"] if completed else spec["last_completed_bucket_ms"],
                        "last_completed_bucket_us": format_us_time(spec["due_bucket_ms"]) if completed else spec["last_completed_bucket_us"],
                        "last_written_bars": written_bars,
                        "written_symbols": sorted(written_symbols_by_interval.get(normalized_interval) or []),
                        "written_symbols_total": len(written_symbols_by_interval.get(normalized_interval) or []),
                        "verified_symbols": sorted(verified_by_interval.get(normalized_interval) or []),
                        "verified_symbols_total": len(verified_by_interval.get(normalized_interval) or []),
                        "pending_symbols": pending_symbols,
                        "pending_symbols_total": len(pending_symbols),
                        "freshness_by_symbol": freshness_by_interval.get(normalized_interval) or {},
                        "duration_s": round(max(0.0, time.time() - started_at), 3),
                        "last_error": interval_error,
                    }
                )
                interval_states[normalized_interval] = base_update
                if interval_error:
                    errors.append(f"{normalized_interval}:{interval_error}")

            return interval_states, ran_intervals, total_written, ";".join(errors[:5])

    def _run_runtime_direct_topup_cycle(self, intervals_override: list[str] | None = None):
            service_mod = _service_mod()
            enabled = self._runtime_direct_topup_enabled()
            intervals = [
                normalize_interval(interval)
                for interval in (intervals_override or self._runtime_direct_topup_intervals())
                if str(interval or "").strip()
            ]
            state = self._copy_direct_topup_state()
            state.update(
                {
                    "enabled": enabled,
                    "intervals": self._runtime_direct_topup_order_intervals(intervals),
                    "close_delay_sec": self._runtime_direct_topup_close_delay_sec(),
                    "loop_interval_s": self._runtime_direct_topup_loop_interval_sec(),
                    "parallel_enabled": self._runtime_direct_topup_parallel_enabled(),
                    "wait_for_watchlist_5m": self._runtime_direct_topup_wait_for_watchlist_5m_enabled(),
                    "interval_priority": self._runtime_direct_topup_interval_priority(),
                }
            )
            intervals = list(state["intervals"])
            if not enabled:
                self._set_direct_topup_state(**state)
                return
            if not intervals:
                state["last_error"] = "no_direct_topup_intervals"
                self._set_direct_topup_state(**state)
                return
            if not self._runtime_direct_topup_5m_is_current():
                state["last_error"] = "canonical_5m_not_current"
                self._set_direct_topup_state(**state)
                return
            if self._runtime_direct_topup_wait_for_watchlist_5m_enabled() and self._runtime_direct_topup_watchlist_5m_pending():
                state["last_error"] = "watchlist_5m_pending"
                self._set_direct_topup_state(**state)
                return

            symbols, conid_map, symbol_meta = self._runtime_direct_topup_symbols()
            interval_states = dict(state.get("intervals_state") or {})
            ran_interval = ""
            total_written = int(state.get("total_written_bars", 0) or 0)
            last_error = ""

            if self._runtime_direct_topup_parallel_enabled():
                interval_states, ran_intervals, written_delta, last_error = self._run_runtime_direct_topup_intervals_batch(
                    intervals,
                    symbols,
                    conid_map,
                    symbol_meta,
                    state,
                )
                ran_interval = ",".join(ran_intervals)
                total_written += written_delta
            else:
                for interval in intervals:
                    normalized_interval = normalize_interval(interval)
                    interval_state = self._run_runtime_direct_topup_interval(
                        normalized_interval,
                        symbols,
                        conid_map,
                        symbol_meta,
                        state,
                    )
                    interval_states[normalized_interval] = interval_state
                    if str(interval_state.get("status") or "") in {"completed", "failed", "skipped", "pending"}:
                        previous_state = (state.get("intervals_state") or {}).get(normalized_interval) or {}
                        if int(interval_state.get("last_due_bucket_ms", 0) or 0) > int(previous_state.get("last_completed_bucket_ms", 0) or 0):
                            ran_interval = normalized_interval
                            total_written += int(interval_state.get("last_written_bars", 0) or 0)
                            last_error = str(interval_state.get("last_error") or "")
                            break

            self._set_direct_topup_state(
                enabled=enabled,
                intervals=intervals,
                close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
                loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
                parallel_enabled=self._runtime_direct_topup_parallel_enabled(),
                wait_for_watchlist_5m=self._runtime_direct_topup_wait_for_watchlist_5m_enabled(),
                interval_priority=self._runtime_direct_topup_interval_priority(),
                last_run=self._now_iso(),
                last_error=last_error,
                total_written_bars=total_written,
                last_completed_interval=ran_interval,
                intervals_state=interval_states,
            )

    def _runtime_direct_topup_loop(self):
            service_mod = _service_mod()
            service_mod.logger.info("Runtime direct higher-timeframe top-up loop started")
            while self._running:
                try:
                    self._run_runtime_direct_topup_cycle()
                except Exception as exc:
                    service_mod.logger.error("Runtime direct top-up loop error: %s", exc)
                    self._set_direct_topup_state(
                        enabled=self._runtime_direct_topup_enabled(),
                        intervals=self._runtime_direct_topup_intervals(),
                        close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
                        loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
                        parallel_enabled=self._runtime_direct_topup_parallel_enabled(),
                        wait_for_watchlist_5m=self._runtime_direct_topup_wait_for_watchlist_5m_enabled(),
                        interval_priority=self._runtime_direct_topup_interval_priority(),
                        last_run=self._now_iso(),
                        last_error=str(exc),
                    )
                time.sleep(self._runtime_direct_topup_loop_interval_sec())
