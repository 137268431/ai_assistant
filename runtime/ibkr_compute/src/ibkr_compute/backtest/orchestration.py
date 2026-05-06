from __future__ import annotations

from .runtime_support import *


class BacktestOrchestrationMixin:
    def _execute_run(self, run_id: str, request: dict, progress_context: dict | None = None) -> dict:
        started_at = time.time()
        self._set_progress_context("running", "bootstrap", "resolving symbols", 2, progress_context)
        self._update_run(
            run_id,
            {
                "status": "running",
                "progress": 2,
                "started_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                "error": "",
            },
        )

        backtest_target_rows = []
        backtest_target_capture = self._empty_capture_summary(BACKTEST_TARGET_COLLECTION, run_id, "disabled")
        historical_targeting = {}
        allowed_trade_days_by_symbol = {}
        if request.get("symbol_source") == "daily_scan_replay":
            scan_replay = self._build_daily_scan_replay_plan(request, progress_context=progress_context)
            symbols = list(scan_replay.get("symbols") or [])
            backtest_target_rows = list(scan_replay.get("target_rows") or [])
            historical_targeting = dict(scan_replay.get("summary") or {})
            allowed_trade_days_by_symbol = self._invert_selection_plan(scan_replay.get("selection_plan") or {})
            self._set_progress_context("running", "persist_targets", "saving historical target replay", 14, progress_context)
            backtest_target_capture = self._persist_backtest_targets(run_id, backtest_target_rows)
            historical_targeting["capture"] = backtest_target_capture
        else:
            symbols = self._resolve_symbols(request)
        if not symbols:
            raise ValueError("No symbols resolved for backtest")
        request["symbols"] = symbols
        request["symbols_text"] = ",".join(symbols)
        preflight_backfill = self._preflight_backfill_symbols(symbols, request, progress_context=progress_context)
        self._update_run(
            run_id,
            {
                "symbols": request["symbols_text"],
                "extra": self._build_run_extra(
                    request,
                    symbols,
                    backtest_target_capture=backtest_target_capture,
                    historical_targeting=historical_targeting,
                    preflight_backfill=preflight_backfill,
                ),
            },
        )

        all_trades = []
        all_indicator_rows = []
        backtest_indicator_generated_count = 0
        all_signal_rows = []
        all_reverse_rows = []
        daily_equity_points = []
        skipped_symbols = []
        data_quality = []
        tv_symbol_reports = []
        realized_pnl = 0.0
        initial_capital = float(request["initial_capital"])
        portfolio_metrics = {}
        if str(request.get("execution_model") or "symbol_independent") == "portfolio_stream":
            portfolio_result = self._run_portfolio_stream_backtest(
                symbols,
                request,
                allowed_trade_days_by_symbol=allowed_trade_days_by_symbol,
                target_rows=backtest_target_rows,
                progress_context=progress_context,
            )
            all_trades = list(portfolio_result.get("trades") or [])
            all_indicator_rows = list(portfolio_result.get("indicator_rows") or [])
            backtest_indicator_generated_count = int(portfolio_result.get("indicator_count", len(all_indicator_rows)) or 0)
            all_signal_rows = list(portfolio_result.get("signal_rows") or [])
            all_reverse_rows = list(portfolio_result.get("reverse_rows") or [])
            skipped_symbols = list(portfolio_result.get("skipped_symbols") or [])
            data_quality = list(portfolio_result.get("data_quality") or [])
            tv_symbol_reports = list(portfolio_result.get("tv_symbol_reports") or [])
            portfolio_metrics = dict(portfolio_result.get("portfolio_metrics") or {})
        else:
            total_symbols = max(1, len(symbols))
            completed_symbols = 0

            for symbol in symbols:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                progress_value = 16 + int((completed_symbols / total_symbols) * 59)
                self._set_progress_context("running", "loading", f"loading {symbol}", progress_value, progress_context)
                bars = self._load_symbol_bars(
                    symbol,
                    request["source_environment"],
                    request["date_from"],
                    request["date_to"],
                    request["session_mode"],
                    allow_backfill=False,
                )
                if len(bars) < 40:
                    skipped_symbols.append(symbol)
                    data_quality.append(
                        {
                            "symbol": symbol,
                            "bar_count": len(bars),
                            "gap_count": 0,
                            "status": "insufficient_data",
                        }
                    )
                    completed_symbols += 1
                    bars.clear()
                    continue

                trades, quality, tv_symbol_report, indicator_rows, signal_rows, reverse_rows, indicator_count = self._run_symbol_backtest(
                    symbol,
                    bars,
                    request,
                    allowed_trade_days=allowed_trade_days_by_symbol.get(symbol),
                )
                all_trades.extend(trades)
                all_indicator_rows.extend(indicator_rows)
                backtest_indicator_generated_count += int(indicator_count or len(indicator_rows))
                all_signal_rows.extend(signal_rows)
                all_reverse_rows.extend(reverse_rows)
                data_quality.append(quality)
                if tv_symbol_report:
                    tv_symbol_reports.append(tv_symbol_report)
                completed_symbols += 1
                indicator_rows.clear()
                bars.clear()

        if self._cancel_event.is_set():
            raise BacktestCancelled()

        all_trades.sort(key=lambda item: (int(item.get("exit_bar_ms", 0) or 0), item.get("symbol", "")))
        all_reverse_rows.sort(key=lambda item: (int(item.get("bar_time_ms", 0) or 0), item.get("symbol", ""), item.get("action_type", "")))
        for trade in all_trades:
            realized_pnl += float(trade.get("pnl", 0) or 0)
            daily_equity_points.append(
                {
                    "date": str(trade.get("exit_us_time") or "")[:10],
                    "equity": round(initial_capital + realized_pnl, 2),
                }
            )

        daily_equity = self._collapse_daily_equity(daily_equity_points, initial_capital)
        benchmark_points = self._build_benchmark_curve(
            request["benchmark_symbol"],
            request["source_environment"],
            request["date_from"],
            request["date_to"],
            request["session_mode"],
            initial_capital,
            allow_backfill=False,
        )
        metrics = self._compute_metrics(initial_capital, all_trades, daily_equity)
        metrics.update(portfolio_metrics)
        metrics["benchmark"] = benchmark_points
        metrics["skipped_symbols"] = skipped_symbols
        metrics["data_quality"] = data_quality
        tv_parity = self._finalize_tv_parity_report(request, tv_symbol_reports)
        metrics["tv_parity"] = tv_parity.get("summary") or {}
        metrics["backtest_indicator_count"] = backtest_indicator_generated_count
        metrics["backtest_signal_count"] = len(all_signal_rows)
        metrics["backtest_target_count"] = len(backtest_target_rows)
        metrics["backtest_reverse_signal_count"] = len(all_reverse_rows)
        metrics["signal_count"] = len(all_signal_rows)
        metrics["executed_signal_count"] = len([row for row in all_signal_rows if str(row.get("status") or "") == "executed"])
        metrics["backtest_signal_status_breakdown"] = self._count_values(all_signal_rows, "status")
        metrics["backtest_signal_reason_breakdown"] = self._count_signal_status_reasons(all_signal_rows)
        metrics["backtest_signal_rejection_breakdown"] = self._count_signal_status_reasons(
            [row for row in all_signal_rows if str(row.get("status") or "") in {"skipped", "dropped"}]
        )
        metrics["daily_scan_match_diagnostics"] = self._build_daily_scan_match_diagnostics(
            backtest_target_rows,
            all_signal_rows,
        )
        metrics.update(
            self._build_backtest_funnel_metrics(
                request,
                backtest_target_rows,
                all_signal_rows,
                all_trades,
            )
        )
        metrics["signal_fill_rate"] = (
            round((metrics["executed_signal_count"] / metrics["signal_count"]) * 100.0, 4)
            if metrics["signal_count"] else 0.0
        )
        metrics["backtest_reverse_action_breakdown"] = self._count_values(all_reverse_rows, "action_type")
        metrics["backtest_reverse_strength_breakdown"] = self._count_values(all_reverse_rows, "strength")
        metrics["backtest_reverse_source_breakdown"] = self._count_values(all_reverse_rows, "source")
        metrics["backtest_reverse_status_breakdown"] = self._count_values(all_reverse_rows, "status")
        metrics["backtest_reverse_samples"] = self._build_backtest_reverse_samples(all_reverse_rows)
        metrics["historical_targeting"] = historical_targeting
        avg_loss = float(metrics.get("avg_loss", 0) or 0)
        metrics["win_loss_ratio"] = round((float(metrics.get("avg_win", 0) or 0) / abs(avg_loss)), 4) if avg_loss < 0 else 0.0
        analysis_report = self._build_run_analysis_report(
            run_id,
            request,
            symbols,
            metrics,
            tv_parity,
            historical_targeting=historical_targeting,
        )
        metrics["analysis_summary"] = analysis_report.get("summary") or {}

        persist_backtest_indicators = self._should_persist_backtest_indicators(request)
        self._set_progress_context(
            "running",
            "persist",
            "saving backtest indicators" if persist_backtest_indicators else "skipping backtest indicator persistence",
            92,
            progress_context,
        )
        try:
            if persist_backtest_indicators:
                backtest_indicator_capture = self._persist_backtest_indicators(run_id, all_indicator_rows)
            else:
                backtest_indicator_capture = self._empty_capture_summary(BACKTEST_INDICATOR_COLLECTION, run_id, "disabled")
                backtest_indicator_capture["attempted_count"] = backtest_indicator_generated_count
        finally:
            all_indicator_rows.clear()
        metrics["backtest_indicator_capture"] = backtest_indicator_capture
        self._set_progress_context("running", "persist", "saving trades and metrics", 94, progress_context)
        backtest_signal_capture = self._persist_backtest_signals(run_id, all_signal_rows)
        metrics["backtest_signal_capture"] = backtest_signal_capture
        metrics["backtest_target_capture"] = backtest_target_capture
        backtest_reverse_capture = self._persist_backtest_reverse_signals(run_id, all_reverse_rows)
        metrics["backtest_reverse_capture"] = backtest_reverse_capture
        self._persist_trades(run_id, all_trades)
        finished_at = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        duration_s = round(time.time() - started_at, 3)
        self._update_run(
            run_id,
            {
                "status": "completed",
                "progress": 100,
                "trade_count": int(metrics["trade_count"]),
                "net_pnl": float(metrics["net_pnl"]),
                "total_return_pct": float(metrics["total_return_pct"]),
                "sharpe": float(metrics["sharpe"]),
                "max_drawdown_pct": float(metrics["max_drawdown_pct"]),
                "win_rate": float(metrics["win_rate"]),
                "duration_s": duration_s,
                "finished_at": finished_at,
                "metrics": metrics,
                "extra": self._build_run_extra(
                    request,
                    symbols,
                    metrics=metrics,
                    daily_equity=daily_equity,
                    benchmark_points=benchmark_points,
                    tv_parity=tv_parity,
                    backtest_indicator_capture=backtest_indicator_capture,
                    backtest_signal_capture=backtest_signal_capture,
                    backtest_target_capture=backtest_target_capture,
                    backtest_reverse_capture=backtest_reverse_capture,
                    historical_targeting=historical_targeting,
                    analysis_report=analysis_report,
                    preflight_backfill=preflight_backfill,
                ),
                "error": "",
            },
        )
        if analysis_report.get("notification", {}).get("should_notify") and not request.get("batch_id"):
            baseline = analysis_report.get("baseline_comparison") or {}
            self._notify_backtest_improvement(
                "Backtest 发现更优同周期结果",
                {
                    "run_id": run_id,
                    "name": request.get("name") or "",
                    "date_from": request.get("date_from") or "",
                    "date_to": request.get("date_to") or "",
                    "symbol_source": request.get("symbol_source") or "",
                    "source_environment": request.get("source_environment") or "",
                    "symbols": symbols,
                    "headline": analysis_report.get("summary", {}).get("headline") or "",
                    "delta_return_pct": baseline.get("delta_return_pct", 0),
                    "delta_sharpe": baseline.get("delta_sharpe", 0),
                    "baseline_run_id": baseline.get("baseline_run_id") or "",
                    "baseline_name": baseline.get("baseline_name") or "",
                    "suggestions": (analysis_report.get("recommendation") or {}).get("suggestions") or [],
                },
            )
        self._clear_backtest_row_buffers(all_trades, all_signal_rows, all_reverse_rows, daily_equity_points, backtest_target_rows)
        return {
            "ok": True,
            "status": "completed",
            "metrics": metrics,
            "symbols": symbols,
            "daily_equity": daily_equity,
            "benchmark_points": benchmark_points,
            "tv_parity": tv_parity,
            "backtest_indicator_capture": backtest_indicator_capture,
            "backtest_signal_capture": backtest_signal_capture,
            "backtest_target_capture": backtest_target_capture,
            "backtest_reverse_capture": backtest_reverse_capture,
            "analysis_report": analysis_report,
            "duration_s": duration_s,
            "finished_at": finished_at,
            "error": "",
        }

    def _run_job(self, run_id: str, request: dict):
        started_at = time.time()
        try:
            self._execute_run(run_id, request)
            self._set_progress_context("completed", "done", "backtest completed", 100)
        except BacktestCancelled:
            duration_s = round(time.time() - started_at, 3)
            self._update_run(
                run_id,
                {
                    "status": "cancelled",
                    "progress": int(self._progress.get("progress", 0) or 0),
                    "finished_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_s": duration_s,
                    "error": "cancelled by user",
                },
            )
            self._set_progress_context("cancelled", "cancelled", "backtest cancelled", self._progress.get("progress", 0))
        except Exception as exc:
            traceback.print_exc()
            duration_s = round(time.time() - started_at, 3)
            self._update_run(
                run_id,
                {
                    "status": "failed",
                    "finished_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_s": duration_s,
                    "error": str(exc)[:1000],
                },
            )
            self._set_progress_context("failed", "failed", str(exc), self._progress.get("progress", 0))
        finally:
            try:
                self._apply_retention_limits(
                    retention_limit=int(request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT) or DEFAULT_BACKTEST_RETENTION_LIMIT),
                    exclude_run_ids={run_id},
                )
            except Exception:
                traceback.print_exc()
            with self._lock:
                self._cancel_event.clear()
                self._thread = None
                self._active_payload = {}
                self._active_run_id = ""
                self._active_batch_id = ""

    def _build_history_broker(self) -> BrokerAdapter:
        base_client_id = self._get_env_int("IBGW_CLIENT_ID", 31)
        client_id = self._get_env_int("IBGW_BACKTEST_CLIENT_ID", base_client_id + 20)
        return BrokerAdapter(client_id=client_id)

    @staticmethod
    def _get_env_int(name: str, default: int) -> int:
        try:
            return int(str(os.environ.get(name, "") or default).strip())
        except Exception:
            return int(default)

    def _update_batch_summary(self, batch_id: str, batch: dict, summaries: list[dict], extra_patch: dict | None = None, status: str = ""):
        sorted_items = sorted(
            summaries,
            key=lambda item: (
                0 if item.get("status") == "completed" else 1,
                -float(item.get("total_return_pct", 0) or 0),
                -float(item.get("sharpe", 0) or 0),
            ),
        )
        best = sorted_items[0] if sorted_items else {}
        completed_count = len([item for item in summaries if item.get("status") in {"completed", "failed", "cancelled"}])
        base_extra = extra_patch if extra_patch is not None else self._parse_object(batch.get("extra"))
        merged_extra = dict(base_extra or {})
        experiment_analysis = self._build_batch_experiment_analysis(batch_id, batch, summaries)
        merged_extra["experiment_analysis"] = experiment_analysis
        patch = {
            "completed_count": completed_count,
            "best_run_id": best.get("run_id") or "",
            "best_variant_label": best.get("variant_label") or "",
            "best_total_return_pct": float(best.get("total_return_pct", 0) or 0),
            "best_sharpe": float(best.get("sharpe", 0) or 0),
            "leaderboard": sorted_items,
            "extra": merged_extra,
        }
        if status:
            patch["status"] = status
        self._update_batch(batch_id, patch)

    def _run_batch_job(self, batch_id: str, request: dict, planned_runs: list[dict]):
        batch_started_at = time.time()
        summaries = []
        current_run_id = ""
        try:
            self._update_batch(
                batch_id,
                {
                    "status": "running",
                    "started_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "error": "",
                },
            )
            total_runs = max(1, len(planned_runs))
            for index, planned_request in enumerate(planned_runs, start=1):
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                run_id = str(planned_request.get("run_id") or "")
                current_run_id = run_id
                self._active_run_id = run_id
                self._active_batch_id = batch_id
                planned_request["batch_id"] = batch_id
                planned_request["batch_name"] = request["name"]
                planned_request["variant_count"] = total_runs
                progress_context = {
                    "start": int(((index - 1) / total_runs) * 100),
                    "end": int((index / total_runs) * 100),
                    "prefix": f'variant {index}/{total_runs} · {planned_request.get("variant_label") or ""} · ',
                }
                try:
                    outcome = self._execute_run(run_id, planned_request, progress_context=progress_context)
                    summaries.append(self._summarize_run_for_batch(run_id, planned_request, outcome))
                    batch = self._get_batch(batch_id) or {"extra": {}}
                    self._update_batch_summary(batch_id, batch, summaries)
                except BacktestCancelled:
                    raise
                except Exception as exc:
                    traceback.print_exc()
                    duration_s = round(time.time() - batch_started_at, 3)
                    self._update_run(
                        run_id,
                        {
                            "status": "failed",
                            "finished_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                            "duration_s": duration_s,
                            "error": str(exc)[:1000],
                        },
                    )
                    summaries.append(
                        self._summarize_run_for_batch(
                            run_id,
                            planned_request,
                            {"status": "failed", "metrics": {}, "error": str(exc)[:1000]},
                        )
                    )
                    batch = self._get_batch(batch_id) or {"extra": {}}
                    self._update_batch_summary(batch_id, batch, summaries)

            finished_at = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
            batch = self._get_batch(batch_id) or {"extra": {}}
            self._update_batch_summary(batch_id, batch, summaries, status="completed")
            refreshed_batch = self._get_batch(batch_id) or {"extra": {}}
            refreshed_extra = self._parse_object(refreshed_batch.get("extra"))
            experiment_analysis = self._parse_object(refreshed_extra.get("experiment_analysis"))
            self._update_batch(
                batch_id,
                {
                    "status": "completed",
                    "finished_at": finished_at,
                    "error": "",
                    "extra": {
                        **refreshed_extra,
                        "duration_s": round(time.time() - batch_started_at, 3),
                        **build_runtime_timestamps(),
                    },
                },
            )
            if experiment_analysis.get("notification", {}).get("should_notify"):
                comparison = experiment_analysis.get("comparison") or {}
                self._notify_backtest_improvement(
                    "Backtest experiment 找到更优变体",
                    {
                        "batch_id": batch_id,
                        "name": request.get("name") or "",
                        "date_from": request.get("date_from") or "",
                        "date_to": request.get("date_to") or "",
                        "symbol_source": request.get("symbol_source") or "",
                        "source_environment": request.get("source_environment") or "",
                        "best_run_id": comparison.get("best_run_id") or "",
                        "best_variant_label": comparison.get("best_variant_label") or "",
                        "delta_vs_historical_return_pct": comparison.get("delta_vs_historical_return_pct", 0),
                        "delta_vs_historical_sharpe": comparison.get("delta_vs_historical_sharpe", 0),
                        "historical_baseline_run_id": comparison.get("historical_baseline_run_id") or "",
                        "suggestions": experiment_analysis.get("suggestions") or [],
                    },
                )
            self._set_progress_context("completed", "done", "parameter batch completed", 100)
        except BacktestCancelled:
            if current_run_id:
                self._update_run(
                    current_run_id,
                    {
                        "status": "cancelled",
                        "progress": int(self._progress.get("progress", 0) or 0),
                        "finished_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                        "duration_s": round(time.time() - batch_started_at, 3),
                        "error": "cancelled by user",
                    },
                )
            self._update_batch(
                batch_id,
                {
                    "status": "cancelled",
                    "finished_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "error": "cancelled by user",
                },
            )
            self._set_progress_context("cancelled", "cancelled", "parameter batch cancelled", self._progress.get("progress", 0))
        finally:
            try:
                self._apply_retention_limits(
                    retention_limit=int(request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT) or DEFAULT_BACKTEST_RETENTION_LIMIT),
                    exclude_run_ids={str(item.get("run_id") or "") for item in planned_runs if str(item.get("run_id") or "")},
                    exclude_batch_ids={batch_id},
                )
            except Exception:
                traceback.print_exc()
            with self._lock:
                self._cancel_event.clear()
                self._thread = None
                self._active_payload = {}
                self._active_run_id = ""
                self._active_batch_id = ""
