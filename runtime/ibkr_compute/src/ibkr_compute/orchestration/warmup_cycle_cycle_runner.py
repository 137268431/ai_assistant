from __future__ import annotations

import time
from datetime import datetime

from .market_universe_targets import _bar_pipeline_skip_reason
from .warmup_cycle_support import _service_mod

class WarmupCycleRunnerMixin:
    def _run_warmup_cycle(self):
        service_mod = _service_mod()
        snapshot = self._warmup_snapshot_from_subscriptions()
        previous_warmup_state = self._copy_warmup_state()
        if snapshot["symbols_total"] == 0:
            self._set_warmup_state(
                phase="idle",
                reason="no_active_symbols",
                trading_gate_open=False,
                trading_gate_reason="no_active_symbols",
            )
            return
        if not self.session_keeper.is_authenticated:
            self._close_warmup_gate("session_unauthenticated")
            return

        started_at = self._now_iso()
        warmup_started_perf = time.perf_counter()
        warmup_timings = {}
        integrity_reference_et = datetime.now(service_mod.ET)
        refresh_state = self._build_transient_warmup_retry_state(
            snapshot,
            previous_warmup_state,
            reason="refresh_running",
            requested_at=started_at,
            started_at=started_at,
        )
        preserve_gate = bool(refresh_state.get("trading_gate_open"))
        self._set_warmup_state(
            phase=str(refresh_state.get("phase") or "running") if preserve_gate else "running",
            reason="refresh_running" if preserve_gate else str(self._warmup_state.get("reason") or "warmup"),
            started_at=refresh_state.get("started_at") if preserve_gate else started_at,
            finished_at=refresh_state.get("finished_at") if preserve_gate else None,
            last_error="",
            trading_gate_open=preserve_gate,
            trading_gate_reason=(
                str(refresh_state.get("trading_gate_reason") or "ready")
                if preserve_gate
                else "warmup_running"
            ),
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            scan_symbols_total=snapshot["scan_symbols_total"],
            subscription_symbols_total=snapshot["subscription_symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            symbols=snapshot["symbols"],
            scan_symbols=snapshot["scan_symbols"],
            subscription_symbols=snapshot["subscription_symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            ready_symbols_list=refresh_state.get("ready_symbols_list") if preserve_gate else [],
            pending_symbols=refresh_state.get("pending_symbols") if preserve_gate else snapshot["symbols"],
            symbol_status=refresh_state.get("symbol_status") if preserve_gate else [],
            integrity_pending_symbols=refresh_state.get("integrity_pending_symbols") if preserve_gate else [],
            integrity_repair_reasons=refresh_state.get("integrity_repair_reasons") if preserve_gate else {},
            preflight_repair=previous_warmup_state.get("preflight_repair") if preserve_gate else {},
            backfill_written=int(previous_warmup_state.get("backfill_written", 0) or 0) if preserve_gate else 0,
            backfill_result=previous_warmup_state.get("backfill_result") if preserve_gate else {},
            compute_result=previous_warmup_state.get("compute_result") if preserve_gate else {},
            timings=previous_warmup_state.get("timings") if preserve_gate else {},
            last_duration_s=(
                float(previous_warmup_state.get("last_duration_s", 0.0) or 0.0)
                if preserve_gate
                else 0.0
            ),
        )

        service_mod.logger.info(
            "Warmup started: symbols=%d trade=%d monitor=%d target_date=%s",
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
            snapshot["target_date"] or "n/a",
        )
        self._sync_startup_progress(
            action="update",
            title="IBKR Runtime 启动中",
            summary="核心线程已启动，Warmup / 预检修复 / 历史回补已开始。",
            current_step="warmup",
            current_blocker="等待 warmup 基线统计与首轮预检修复结果",
            operator_action="等待系统推进预检修复、历史回补与交易门判断",
            steps={
                "warmup": {
                    "status": "running",
                    "detail": (
                        f"symbols={snapshot['symbols_total']} "
                        f"trade={snapshot['trade_symbols_total']} "
                        f"monitor={snapshot['monitor_symbols_total']}"
                    ),
                },
            },
            fields=self._build_startup_progress_fields(
                str(self._warmup_state.get("reason") or "warmup"),
                self._startup_source or "api_start",
                bool(self._startup_trigger_login),
            ),
            reason=self._startup_reason or "warmup",
            source=self._startup_source or "api_start",
            trigger_login=bool(self._startup_trigger_login),
        )

        backfill_result = {}
        backfill_written = 0
        compute_result = {}
        last_error = ""
        startup_gate_open_once = False

        try:
            step_started = time.perf_counter()
            self._load_warmup_compute_cursors()
            warmup_timings["cursor_load_s"] = round(time.perf_counter() - step_started, 3)

            step_started = time.perf_counter()
            preflight_result = self._run_warmup_preflight_repairs(
                snapshot,
                integrity_reference_et=integrity_reference_et,
            )
            warmup_timings["preflight_repair_s"] = round(time.perf_counter() - step_started, 3)
            compute_result["preflight_repair"] = preflight_result
            preflight_blockers = {
                symbol: {
                    "repair_reason": (
                        preflight_result.get("repair_reasons") or {}
                    ).get(symbol, "history_repair_pending")
                }
                for symbol in (preflight_result.get("remaining_repair_symbols") or [])
            }
            if preflight_result.get("history_written_total"):
                backfill_written += int(preflight_result.get("history_written_total", 0) or 0)
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(
                    preflight_result.get("attempted_repair_symbols") or []
                )

            step_started = time.perf_counter()
            storage_bootstrap = self._materialize_warmup_compute_symbols(
                snapshot["symbols"],
                hydrate_signal_state=True,
            )
            warmup_timings["storage_bootstrap_s"] = round(time.perf_counter() - step_started, 3)
            if storage_bootstrap:
                compute_result["storage_bootstrap"] = storage_bootstrap

            step_started = time.perf_counter()
            indicator_backfill = self._run_warmup_indicator_backfill(snapshot)
            warmup_timings["indicator_backfill_s"] = round(time.perf_counter() - step_started, 3)
            if indicator_backfill.get("plan"):
                compute_result["indicator_backfill"] = indicator_backfill
                backfill_written += int(indicator_backfill.get("written_total", 0) or 0)
            elif indicator_backfill.get("skipped"):
                compute_result["indicator_backfill"] = {
                    "skipped": True,
                    "skip_reason": indicator_backfill.get("skip_reason") or "no_bar_backfill_needed",
                }

            readiness = self._collect_warmup_readiness(snapshot)
            readiness = self._apply_integrity_readiness(readiness, snapshot, preflight_blockers)
            readiness = self._preserve_previous_gate_for_transient_remote_readiness(
                snapshot,
                readiness,
                previous_warmup_state,
            )
            if readiness["pending_symbols"]:
                step_started = time.perf_counter()
                if self._warmup_uses_remote_compute_service():
                    pending_storage_bootstrap = self._trigger_remote_warmup_prime(
                        readiness["pending_symbols"],
                        source="warmup_pending",
                    )
                else:
                    pending_storage_bootstrap = self._materialize_warmup_compute_symbols(
                        readiness["pending_symbols"],
                        hydrate_signal_state=True,
                    )
                warmup_timings["pending_storage_bootstrap_s"] = round(
                    time.perf_counter() - step_started,
                    3,
                )
                if pending_storage_bootstrap:
                    compute_result["pending_storage_bootstrap"] = pending_storage_bootstrap
                    if bool(pending_storage_bootstrap.get("ok", True)):
                        readiness = self._collect_warmup_readiness(snapshot)
                        readiness = self._apply_integrity_readiness(
                            readiness,
                            snapshot,
                            preflight_blockers,
                        )
                        readiness = self._preserve_previous_gate_for_transient_remote_readiness(
                            snapshot,
                            readiness,
                            previous_warmup_state,
                        )
            compute_result["warmup_timings"] = dict(warmup_timings)
            self._set_warmup_state(
                phase="running",
                required_interval=readiness["required_interval"],
                started_at=started_at,
                target_date=snapshot["target_date"],
                symbols_total=snapshot["symbols_total"],
                trade_symbols_total=snapshot["trade_symbols_total"],
                monitor_symbols_total=snapshot["monitor_symbols_total"],
                ready_symbols=readiness["ready_symbols"],
                ready_trade_symbols=readiness["ready_trade_symbols"],
                ready_monitor_symbols=readiness["ready_monitor_symbols"],
                symbols=snapshot["symbols"],
                trade_symbols=snapshot["trade_symbols"],
                monitor_symbols=snapshot["monitor_symbols"],
                ready_symbols_list=readiness["ready_symbols_list"],
                pending_symbols=readiness["pending_symbols"],
                symbol_status=readiness["symbol_status"],
                integrity_pending_symbols=readiness["integrity_pending_symbols"],
                integrity_repair_reasons=readiness["integrity_repair_reasons"],
                preflight_repair=preflight_result,
                trading_gate_open=readiness["trading_gate_open"],
                trading_gate_reason=readiness["trading_gate_reason"],
                compute_result=compute_result,
                timings=warmup_timings,
                last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
            )
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 已进入预热判断阶段，正在核对 readiness 与完整性阻塞。",
                current_step="warmup",
                current_blocker=(
                    "待完成标的: " + self._format_symbol_list(readiness["pending_symbols"])
                    if readiness["pending_symbols"]
                    else "等待交易门判断"
                ),
                operator_action="等待预检修复、历史回补与交易门开放",
                steps={
                    "warmup": {
                        "status": "running",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"integrity={self._format_symbol_list(readiness['integrity_pending_symbols'])}"
                        ),
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "预检修复标的": self._format_symbol_list(
                            preflight_result.get("attempted_repair_symbols") or []
                        ),
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )

            if readiness["trading_gate_open"] and not readiness["pending_symbols"]:
                finished_at = self._now_iso()
                phase = readiness["phase"]
                warmup_timings["total_elapsed_s"] = round(
                    time.perf_counter() - warmup_started_perf,
                    3,
                )
                compute_result["warmup_timings"] = dict(warmup_timings)
                self._set_warmup_state(
                    phase=phase,
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    finished_at=finished_at,
                    last_success_at=finished_at,
                    last_error=last_error,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    backfill_written=backfill_written,
                    backfill_result=backfill_result,
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=warmup_timings["total_elapsed_s"],
                )
                service_mod.logger.info(
                    "Warmup finished early after bootstrap: phase=%s gate=open ready=%d/%d trade_ready=%d/%d",
                    phase,
                    readiness["ready_symbols"],
                    snapshot["symbols_total"],
                    readiness["ready_trade_symbols"],
                    snapshot["trade_symbols_total"],
                )
                self._complete_startup_success(
                    "IBKR Runtime 启动完成",
                    {
                        "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                        "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                        "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                        "预检修复标的": self._format_symbol_list(
                            preflight_result.get("attempted_repair_symbols") or []
                        ),
                        "回补写入Bars": backfill_written,
                        "预热开始": started_at,
                        "预热完成": finished_at,
                        "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                        "交易门": "open",
                    },
                )
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
                self._signal_wakeup.set()
                return
            if readiness["trading_gate_open"] and readiness["pending_symbols"]:
                startup_gate_open_once = True
                warmup_timings["total_elapsed_s"] = round(
                    time.perf_counter() - warmup_started_perf,
                    3,
                )
                self._set_warmup_state(
                    phase="running",
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
                )
                service_mod.logger.info(
                    "Warmup trade gate open after bootstrap; continuing repair for pending symbols: %s",
                    ",".join(readiness["pending_symbols"]),
                )
                self._release_startup_after_trade_gate(
                    snapshot,
                    readiness,
                    preflight_result,
                    backfill_written,
                    started_at,
                    warmup_timings,
                )
                self._signal_wakeup.set()

            pending_status_by_symbol = {
                str((item or {}).get("symbol") or "").strip().upper(): dict(item or {})
                for item in (readiness.get("symbol_status") or [])
                if str((item or {}).get("symbol") or "").strip()
            }
            pending_map = {
                symbol: snapshot["conid_map"][symbol]
                for symbol in readiness["pending_symbols"]
                if symbol in snapshot["conid_map"]
                and str(
                    (pending_status_by_symbol.get(symbol) or {}).get("source") or ""
                ) not in {
                    "remote_compute_status_missing",
                    "remote_compute_status_unavailable",
                }
            }
            if pending_map:
                bar_skip_reason = _bar_pipeline_skip_reason(self, service_mod)
                if bar_skip_reason:
                    service_mod.logger.info(
                        "Warmup pending-symbol backfill skipped: symbols=%d reason=%s",
                        len(pending_map),
                        bar_skip_reason,
                    )
                    warmup_timings["pending_backfill_s"] = 0.0
                    backfill_result = {
                        "ok": True,
                        "skipped": True,
                        "reason": bar_skip_reason,
                        "symbols": sorted(pending_map.keys()),
                    }
                    compute_result["after_backfill"] = {
                        "skipped": True,
                        "skip_reason": bar_skip_reason,
                    }
                else:
                    service_mod.logger.info(
                        "Warmup backfilling pending symbols: %d of %d",
                        len(pending_map),
                        snapshot["symbols_total"],
                    )
                    step_started = time.perf_counter()
                    backfill_result = self.data_backfill.backfill_all(
                        pending_map,
                        symbol_meta=snapshot["symbol_meta"],
                        intervals=[service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL],
                        repair_symbols=list(pending_map.keys()),
                        period_overrides=self._multi_timeframe_5m_period_overrides(pending_map.keys()),
                    )
                    warmup_timings["pending_backfill_s"] = round(
                        time.perf_counter() - step_started,
                        3,
                    )
                    backfill_written += sum(
                        int(count or 0)
                        for per_symbol in backfill_result.values()
                        for count in per_symbol.values()
                    )
                    self.data_writer.flush()
                    if readiness["trading_gate_open"]:
                        self._last_history_repair_at = time.time()
                        self._last_history_repair_symbols = sorted(pending_map.keys())
                    step_started = time.perf_counter()
                    after_backfill_result = self._materialize_warmup_compute_symbols(
                        list(pending_map.keys()),
                        hydrate_signal_state=True,
                    )
                    warmup_timings["after_backfill_bootstrap_s"] = round(
                        time.perf_counter() - step_started,
                        3,
                    )
                    compute_result["after_backfill"] = after_backfill_result
        except Exception as exc:
            last_error = str(exc)
            service_mod.logger.error("Warmup cycle failed: %s", exc)
            if self._is_local_pb_unavailable_error(exc):
                retry_delay_s = 30
                service_mod.logger.warning(
                    "Warmup pending retry because PocketBase is temporarily unavailable: retry_in=%ss error=%s",
                    retry_delay_s,
                    exc,
                )
                retry_state = self._build_transient_warmup_retry_state(
                    snapshot,
                    previous_warmup_state,
                    reason="pb_unavailable_retry",
                    requested_at=self._now_iso(),
                    started_at=started_at,
                )
                if str(retry_state.get("phase") or "").strip().lower() == "ready":
                    retry_state.update(
                        preflight_repair=dict(previous_warmup_state.get("preflight_repair") or {}),
                        backfill_written=int(previous_warmup_state.get("backfill_written", 0) or 0),
                        backfill_result=dict(previous_warmup_state.get("backfill_result") or {}),
                        compute_result=dict(previous_warmup_state.get("compute_result") or {}),
                        timings=dict(previous_warmup_state.get("timings") or {}),
                        last_duration_s=float(previous_warmup_state.get("last_duration_s", 0.0) or 0.0),
                    )
                else:
                    retry_state.update(
                        preflight_repair={},
                        backfill_written=backfill_written,
                        backfill_result=backfill_result,
                        compute_result=compute_result,
                        timings=warmup_timings,
                        last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
                    )
                self._set_warmup_state(**retry_state)
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="PocketBase 本地服务暂时不可达，Warmup 已进入自动重试等待。",
                    current_step="warmup",
                    current_blocker="等待 PocketBase 恢复可用后自动重试 Warmup",
                    operator_action="无需人工干预，系统将在短暂延迟后自动重试；如持续失败再检查 PB hooks 发布流程",
                    steps={
                        "warmup": {
                            "status": "running",
                            "detail": f"pb_retry_in={retry_delay_s}s error={last_error}",
                        },
                    },
                    fields=self._build_startup_progress_fields(
                        self._startup_reason or "warmup",
                        self._startup_source or "api_start",
                        bool(self._startup_trigger_login),
                    ),
                    reason=self._startup_reason or "warmup",
                    source=self._startup_source or "api_start",
                    trigger_login=bool(self._startup_trigger_login),
                )
                if self._running:
                    time.sleep(retry_delay_s)
                    if self._running:
                        self._warmup_wakeup.set()
                return

        readiness = self._collect_warmup_readiness(snapshot)
        final_preflight = dict(compute_result.get("preflight_repair") or {})
        if final_preflight.get("initial_repair_symbols") or backfill_result:
            final_recheck_symbols = self._normalize_symbol_list(
                list(final_preflight.get("checked_symbols") or [])
                + list((backfill_result or {}).keys())
            )
            final_remaining_plan = self._build_startup_history_repair_plan(
                final_recheck_symbols,
                et_now=integrity_reference_et,
            )
            final_preflight["checked_symbols"] = final_recheck_symbols
            final_preflight["remaining_repair_symbols"] = sorted(final_remaining_plan.keys())
            final_preflight["repair_reasons"] = {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in final_remaining_plan.items()
            }
        final_blockers = {
            symbol: {
                "repair_reason": (
                    final_preflight.get("repair_reasons") or {}
                ).get(symbol, "history_repair_pending")
            }
            for symbol in (final_preflight.get("remaining_repair_symbols") or [])
        }
        readiness = self._apply_integrity_readiness(readiness, snapshot, final_blockers)
        readiness = self._preserve_previous_gate_for_transient_remote_readiness(
            snapshot,
            readiness,
            previous_warmup_state,
        )
        if startup_gate_open_once and not readiness["trading_gate_open"]:
            previous_ready_state = self._copy_warmup_state()
            if int(previous_ready_state.get("ready_symbols", 0) or 0) > int(readiness.get("ready_symbols", 0) or 0):
                for key in (
                    "required_interval",
                    "ready_symbols",
                    "ready_scan_symbols",
                    "ready_subscription_symbols",
                    "ready_trade_symbols",
                    "ready_monitor_symbols",
                    "ready_symbols_list",
                    "pending_symbols",
                    "symbol_status",
                    "integrity_pending_symbols",
                    "integrity_repair_reasons",
                ):
                    if key in previous_ready_state:
                        readiness[key] = previous_ready_state.get(key)
                readiness["phase"] = "ready" if not (readiness.get("pending_symbols") or []) else "degraded"
                readiness["data_ready"] = readiness["phase"] == "ready"
            readiness["trading_gate_open"] = True
            readiness["trade_allowed"] = True
            if str(readiness.get("trading_gate_reason") or "").strip() != "ready":
                readiness["trading_gate_reason"] = "background_repair"
        finished_at = self._now_iso()
        phase = "failed" if last_error and readiness["ready_symbols"] == 0 else readiness["phase"]
        warmup_timings["total_elapsed_s"] = round(time.perf_counter() - warmup_started_perf, 3)
        compute_result["warmup_timings"] = dict(warmup_timings)
        self._set_warmup_state(
            phase=phase,
            required_interval=readiness["required_interval"],
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=(
                finished_at
                if readiness["phase"] == "ready"
                else self._warmup_state.get("last_success_at")
            ),
            last_error=last_error,
            trading_gate_open=readiness["trading_gate_open"],
            trading_gate_reason=readiness["trading_gate_reason"],
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            ready_symbols=readiness["ready_symbols"],
            ready_trade_symbols=readiness["ready_trade_symbols"],
            ready_monitor_symbols=readiness["ready_monitor_symbols"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            ready_symbols_list=readiness["ready_symbols_list"],
            pending_symbols=readiness["pending_symbols"],
            symbol_status=readiness["symbol_status"],
            integrity_pending_symbols=readiness["integrity_pending_symbols"],
            integrity_repair_reasons=readiness["integrity_repair_reasons"],
            preflight_repair=final_preflight,
            backfill_written=backfill_written,
            backfill_result=backfill_result,
            compute_result=compute_result,
            timings=warmup_timings,
            last_duration_s=warmup_timings["total_elapsed_s"],
        )

        service_mod.logger.info(
            "Warmup finished: phase=%s gate=%s ready=%d/%d trade_ready=%d/%d backfill_written=%d compute_processed=%s total_elapsed_s=%.3f",
            phase,
            "open" if readiness["trading_gate_open"] else "closed",
            readiness["ready_symbols"],
            snapshot["symbols_total"],
            readiness["ready_trade_symbols"],
            snapshot["trade_symbols_total"],
            backfill_written,
            compute_result.get("processed", 0) if isinstance(compute_result, dict) else 0,
            warmup_timings["total_elapsed_s"],
        )
        startup_ready = bool(readiness["trading_gate_open"])
        if snapshot["trade_symbols_total"] <= 0:
            startup_ready = True

        if startup_ready:
            startup_title = "IBKR Runtime 启动完成"
            startup_detail = {
                "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                "预检修复标的": self._format_symbol_list(
                    final_preflight.get("attempted_repair_symbols") or []
                ),
                "回补写入Bars": backfill_written,
                "预热开始": started_at,
                "预热完成": finished_at,
                "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                "交易门": "open" if readiness["trading_gate_open"] else "closed",
            }
            if snapshot["trade_symbols_total"] <= 0:
                startup_title = "IBKR Runtime 启动完成（监控模式）"
                startup_detail["后续动作"] = (
                    "当前无 trade symbols，runtime 将继续执行 canonical 5m close、指标和 scan 链路。"
                )
            if phase != "ready":
                startup_title = "IBKR Runtime 启动完成（后台继续预热）"
                if snapshot["trade_symbols_total"] <= 0:
                    startup_detail["后续动作"] = (
                        "当前无 trade symbols，runtime 将在后台继续 monitor/integrity repair，并保持 canonical 5m close 链路运行。"
                    )
                else:
                    startup_detail["后续动作"] = "交易链路已开放，剩余 monitor/integrity repair 在后台继续。"
                startup_detail["待完成标的"] = self._format_symbol_list(
                    readiness.get("pending_symbols") or []
                )
                startup_detail["完整性阻塞"] = self._format_symbol_list(
                    readiness.get("integrity_pending_symbols") or []
                )
            if self._complete_startup_success(startup_title, startup_detail):
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
            self._signal_wakeup.set()
            if phase != "ready":
                self._schedule_remote_warmup_retry_if_needed(readiness)
        else:
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 尚未满足交易门开放条件，启动流程停在预热阶段。",
                current_step="warmup",
                current_blocker="交易门尚未开放，仍有待完成标的或完整性阻塞",
                operator_action="等待后续行情 / 回补推进，必要时人工检查阻塞标的",
                steps={
                    "warmup": {
                        "status": "failed" if last_error else "waiting",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"pending={self._format_symbol_list(readiness['pending_symbols'])}"
                        ),
                    },
                    "trading_gate": {
                        "status": "pending",
                        "detail": "等待交易门开放。",
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "完整性阻塞": self._format_symbol_list(
                            readiness["integrity_pending_symbols"]
                        ),
                        "最近异常": last_error or "",
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )
            if self._warmup_uses_remote_compute_service():
                pending_sources = {
                    str((item or {}).get("source") or "").strip()
                    for item in (readiness.get("symbol_status") or [])
                    if not bool((item or {}).get("ready"))
                }
                retryable_sources = {
                    "remote_compute_status_missing",
                    "remote_compute_status_unavailable",
                }
                if pending_sources and pending_sources.issubset(retryable_sources):
                    retry_delay_s = max(
                        1,
                        self.config.get_int_for_environment(
                            "ibkr_warmup_remote_compute_retry_sec",
                            service_mod.ENVIRONMENT,
                            5,
                        ),
                    )
                    service_mod.logger.info(
                        "Warmup waiting for remote compute readiness: sources=%s retry_in=%ss",
                        ",".join(sorted(pending_sources)),
                        retry_delay_s,
                    )
                    if self._running:
                        time.sleep(retry_delay_s)
                        if self._running:
                            self._warmup_wakeup.set()
