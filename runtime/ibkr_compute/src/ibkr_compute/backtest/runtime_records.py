from __future__ import annotations

from .runtime_support import *


class BacktestRuntimeRecordsMixin:
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            client_id = int(getattr(self._history_broker, "client_id", 0) or 0)
            return {
                "ok": True,
                "running": self.is_running(),
                "run_id": self._active_run_id,
                "batch_id": self._active_batch_id,
                "ib_gateway_client_id": client_id,
                "broker_client_id": client_id,
                **self._progress,
            }

    def start_run(self, payload: dict | None) -> dict:
        request = self._normalize_request(payload or {})
        with self._lock:
            if self.is_running():
                return {
                    "ok": False,
                    "error": "backtest_already_running",
                    "active_run_id": self._active_run_id,
                    "active_batch_id": self._active_batch_id,
                    **self._progress,
                }
            if request["variants"]:
                return self._start_batch_run(request)
            return self._start_single_run(request)

    def _start_single_run(self, request: dict) -> dict:
        run_record = self._create_run_record(request)
        run_id = str(run_record.get("id") or "")
        self._active_run_id = run_id
        self._active_batch_id = ""
        self._active_payload = request
        self._cancel_event.clear()
        self._progress = {
            "status": "queued",
            "run_id": run_id,
            "batch_id": "",
            "mode": "single",
            "progress": 0,
            "stage": "queued",
            "message": "backtest queued",
            "updated_at_ms": int(time.time() * 1000),
        }
        self._thread = threading.Thread(
            target=self._run_job,
            args=(run_id, request),
            daemon=True,
            name=f"ibkr-backtest-{run_id[:8]}",
        )
        self._thread.start()
        return {
            "ok": True,
            "mode": "single",
            "run_id": run_id,
            "status": "queued",
            "name": request["name"],
        }

    def _start_batch_run(self, request: dict) -> dict:
        planned_runs = []
        for index, variant in enumerate(request["variants"], start=1):
            planned_request = self._build_variant_request(request, variant, index)
            planned_runs.append(planned_request)

        batch_record = self._create_batch_record(request, planned_runs)
        batch_id = str(batch_record.get("id") or "")

        run_ids = []
        for planned_request in planned_runs:
            extra_patch = {
                "batch_id": batch_id,
                "batch_name": request["name"],
                "variant_label": planned_request["variant_label"],
                "variant_index": planned_request["variant_index"],
                "variant_count": len(planned_runs),
            }
            run_record = self._create_run_record(planned_request, extra_patch=extra_patch)
            planned_request["run_id"] = str(run_record.get("id") or "")
            run_ids.append(planned_request["run_id"])

        self._update_batch(
            batch_id,
            {
                "variant_count": len(planned_runs),
                "completed_count": 0,
                "params": {
                    "base_strategy_params": request["params"]["strategy_params"],
                    "variants": [
                        {
                            "label": item["variant_label"],
                            "strategy_params": item["params"]["strategy_params"],
                            "strategy_tag": item["strategy_tag"],
                        }
                        for item in planned_runs
                    ],
                },
                "extra": {
                    "run_ids": run_ids,
                    "symbol_source": request["symbol_source"],
                    "requested_symbols": request["symbols"],
                    "max_symbols": request["max_symbols"],
                    **self._build_execution_extra(request),
                    **build_runtime_timestamps(),
                },
            },
        )

        self._active_run_id = run_ids[0] if run_ids else ""
        self._active_batch_id = batch_id
        self._active_payload = request
        self._cancel_event.clear()
        self._progress = {
            "status": "queued",
            "run_id": self._active_run_id,
            "batch_id": batch_id,
            "mode": "batch",
            "progress": 0,
            "stage": "queued",
            "message": f"parameter batch queued ({len(planned_runs)} variants)",
            "updated_at_ms": int(time.time() * 1000),
        }
        self._thread = threading.Thread(
            target=self._run_batch_job,
            args=(batch_id, request, planned_runs),
            daemon=True,
            name=f"ibkr-backtest-batch-{batch_id[:8]}",
        )
        self._thread.start()
        return {
            "ok": True,
            "mode": "batch",
            "batch_id": batch_id,
            "run_id": self._active_run_id,
            "run_ids": run_ids,
            "variant_count": len(planned_runs),
            "status": "queued",
            "name": request["name"],
        }

    def cancel(self, run_id: str = "") -> dict:
        with self._lock:
            if not self.is_running():
                return {"ok": False, "error": "no_active_backtest"}
            if run_id and run_id != self._active_run_id:
                return {
                    "ok": False,
                    "error": "run_id_mismatch",
                    "active_run_id": self._active_run_id,
                    "active_batch_id": self._active_batch_id,
                }
            self._cancel_event.set()
            self._progress.update(
                {
                    "status": "cancelling",
                    "stage": "cancelling",
                    "message": "cancel requested",
                    "updated_at_ms": int(time.time() * 1000),
                }
            )
            return {
                "ok": True,
                "run_id": self._active_run_id,
                "batch_id": self._active_batch_id,
                "status": "cancelling",
            }

    def replay(self, run_id: str, symbol: str, center_bar_ms: int = 0, window: int = 80) -> dict:
        run = self._get_run(run_id)
        if not run:
            return {"ok": False, "error": "run_not_found", "run_id": run_id}

        params = dict(DEFAULT_PARAMS)
        params.update((run.get("params") or {}).get("strategy_params") or {})
        extra = run.get("extra") or {}
        source_environment = str(run.get("source_environment") or "live").strip().lower() or "live"
        date_from = str(run.get("date_from") or "")
        date_to = str(run.get("date_to") or "")
        session_mode = str(run.get("session_mode") or "extended").strip().lower() or "extended"
        symbol_text = str(symbol or "").strip().upper()
        if not symbol_text:
            return {"ok": False, "error": "missing_symbol"}

        bars = self._load_symbol_bars(symbol_text, source_environment, date_from, date_to, session_mode)
        if not bars:
            return {"ok": False, "error": "no_bars_for_symbol", "symbol": symbol_text}

        timeline = self._build_replay_timeline(symbol_text, bars, params)
        if center_bar_ms > 0:
            center_index = 0
            for index, row in enumerate(timeline):
                if int(row.get("bar_time_ms", 0) or 0) >= center_bar_ms:
                    center_index = index
                    break
            half = max(10, min(int(window), MAX_REPLAY_ROWS) // 2)
            start = max(0, center_index - half)
            end = min(len(timeline), center_index + half)
            timeline = timeline[start:end]
        else:
            timeline = timeline[-max(20, min(int(window), MAX_REPLAY_ROWS)) :]

        return {
            "ok": True,
            "run_id": run_id,
            "symbol": symbol_text,
            "source_environment": source_environment,
            "session_mode": session_mode,
            "window": len(timeline),
            "rows": timeline,
            "strategy_tag": extra.get("strategy_tag") or "",
        }

    def cleanup(self, run_id: str = "", batch_id: str = "") -> dict:
        safe_batch_id = str(batch_id or "").strip()
        if safe_batch_id:
            return self._cleanup_batch(safe_batch_id)

        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return {"ok": False, "error": "missing_run_id"}
        with self._lock:
            if self.is_running() and safe_run_id == self._active_run_id:
                return {"ok": False, "error": "run_is_active", "run_id": safe_run_id}

        run = self._get_run(safe_run_id)
        if not run:
            return {"ok": False, "error": "run_not_found", "run_id": safe_run_id}

        deleted = self._delete_run_series(safe_run_id)
        self.pb.delete_record(RUN_COLLECTION, safe_run_id)
        self._sync_batch_after_run_cleanup(run, safe_run_id)
        return {
            "ok": True,
            "run_id": safe_run_id,
            **deleted,
            "deleted_run": True,
        }

    def _delete_collection_rows(self, collection: str, run_id: str, sort: str, max_pages: int) -> int:
        safe_run_id_filter = str(run_id or "").replace('"', '\\"')
        if not safe_run_id_filter:
            return 0
        deleted_count = 0
        rows = self.pb.get_all_records(
            collection,
            filter=f'run_id = "{safe_run_id_filter}"',
            sort=sort,
            max_pages=max_pages,
        )
        for row in rows:
            record_id = str(row.get("id") or "")
            if not record_id:
                continue
            self.pb.delete_record(collection, record_id)
            deleted_count += 1
        return deleted_count

    def _delete_run_series(self, run_id: str) -> dict:
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return {
                "deleted_trades": 0,
                "deleted_indicators": 0,
                "deleted_signals": 0,
                "deleted_targets": 0,
                "deleted_reverse_signals": 0,
            }
        return {
            "deleted_trades": self._delete_collection_rows(TRADE_COLLECTION, safe_run_id, "trade_index", 200),
            "deleted_indicators": self._delete_collection_rows(BACKTEST_INDICATOR_COLLECTION, safe_run_id, "bar_time_ms", DEFAULT_MAX_PAGES),
            "deleted_signals": self._delete_collection_rows(BACKTEST_SIGNAL_COLLECTION, safe_run_id, "bar_time_ms", 200),
            "deleted_targets": self._delete_collection_rows(BACKTEST_TARGET_COLLECTION, safe_run_id, "date,symbol", 100),
            "deleted_reverse_signals": self._delete_collection_rows(BACKTEST_REVERSE_SIGNAL_COLLECTION, safe_run_id, "bar_time_ms", 200),
        }

    def _normalize_request(self, payload: dict) -> dict:
        return request_utils.normalize_request(payload)

    def _normalize_strategy_params(self, raw_params: dict, base_params: dict | None = None) -> dict:
        return request_utils.normalize_strategy_params(raw_params, base_params=base_params)

    def _normalize_variants(self, raw_variants: list, base_params: dict, default_strategy_tag: str) -> list[dict]:
        return request_utils.normalize_variants(raw_variants, base_params, default_strategy_tag)

    def _parse_symbols(self, raw_symbols: str) -> list[str]:
        return request_utils.parse_symbols(raw_symbols)

    def _normalize_bool(self, raw_value: Any, default: bool = False) -> bool:
        return request_utils.normalize_bool(raw_value, default=default)

    def _normalize_positive_int(self, raw_value: Any, default: int, minimum: int = 0, maximum: int = MAX_BACKTEST_WARMUP_BARS) -> int:
        return request_utils.normalize_positive_int(raw_value, default=default, minimum=minimum, maximum=maximum)

    def _normalize_hhmm(self, raw_value: Any) -> str:
        return request_utils.normalize_hhmm(raw_value)

    def _build_variant_request(self, base_request: dict, variant: dict, variant_index: int) -> dict:
        return request_utils.build_variant_request(base_request, variant, variant_index)

    def _build_execution_extra(self, request: dict) -> dict:
        execution_profile = compact_execution_cost_profile(build_execution_cost_profile(request))
        return {
            "execution_model": request.get("execution_model", "portfolio_stream"),
            "account_model_mode": request.get("account_model_mode", DEFAULT_ACCOUNT_MODEL_MODE),
            "fee_model": request.get("fee_model", DEFAULT_FEE_MODEL),
            "slippage_model": request.get("slippage_model", DEFAULT_SLIPPAGE_MODEL),
            "execution_cost_profile": execution_profile,
            "account_model_summary": request.get("account_model_summary") or {},
            "account_model_status": request.get("account_model_status") or {},
            "borrow_limit_mode": request.get("borrow_limit_mode", "none"),
            "max_borrow_amount": float(request.get("max_borrow_amount", 0) or 0),
            "position_limit_max": int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or 0),
            "max_strategy_open_positions": int(request.get("max_strategy_open_positions", DEFAULT_MAX_STRATEGY_OPEN_POSITIONS)),
            "consecutive_stop_loss_limit": int(
                request.get("consecutive_stop_loss_limit", DEFAULT_PORTFOLIO_CONSECUTIVE_STOP_LOSS_LIMIT)
                or DEFAULT_PORTFOLIO_CONSECUTIVE_STOP_LOSS_LIMIT
            ),
            "signal_validity_minutes": int(request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES),
            "trade_window_start_time": str(request.get("trade_window_start_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_START),
            "trade_window_end_time": str(request.get("trade_window_end_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_END),
            "order_window_end_time": str(request.get("order_window_end_time") or DEFAULT_PORTFOLIO_ORDER_WINDOW_END),
            "simultaneous_signal_priority": str(request.get("simultaneous_signal_priority") or "daily_target_rank"),
            "manual_confirm_mode": str(request.get("manual_confirm_mode") or "auto"),
            "confirm_delay_minutes": int(request.get("confirm_delay_minutes", 0) or 0),
        }

    def _create_run_record(self, request: dict, extra_patch: dict | None = None) -> dict:
        extra = {
            "force_flat_eod": request["force_flat_eod"],
            "requested_symbols": request["symbols"],
            "max_symbols": request["max_symbols"],
            "strategy_tag": request["strategy_tag"],
            "requested_symbol_source": request.get("requested_symbol_source") or request["symbol_source"],
            "historical_targets_replay": bool(request.get("historical_targets_replay")),
            "warmup_bars": request.get("warmup_bars", BACKTEST_WARMUP_BARS),
            "scan_warmup_bars": request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)),
            "premarket_cutoff_time": request.get("premarket_cutoff_time", DEFAULT_SCAN_CUTOFF_TIME),
            "scan_session_mode": request.get("scan_session_mode", "extended"),
            "retention_limit": request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT),
            "preflight_backfill": bool(request.get("preflight_backfill", True)),
            "backfill_concurrency": int(request.get("backfill_concurrency", DEFAULT_BACKTEST_BACKFILL_CONCURRENCY) or DEFAULT_BACKTEST_BACKFILL_CONCURRENCY),
            "backfill_symbol_timeout_s": int(
                request.get("backfill_symbol_timeout_s", DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)
                or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS
            ),
            "backfill_max_batches": int(
                request.get("backfill_max_batches", DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES)
                or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES
            ),
            "backfill_history_timeout_s": int(
                request.get("backfill_history_timeout_s", DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)
                or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS
            ),
            "backfill_history_max_retries": int(
                request.get("backfill_history_max_retries")
                if request.get("backfill_history_max_retries") is not None
                else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
            ),
            **self._build_execution_extra(request),
            **build_runtime_timestamps(),
        }
        if extra_patch:
            extra.update(extra_patch)
        return self.pb.create_record(
            RUN_COLLECTION,
            {
                "name": request["name"],
                "status": "queued",
                "environment": BACKTEST_ENVIRONMENT,
                "source_environment": request["source_environment"],
                "symbol_source": request["symbol_source"],
                "symbols": request["symbols_text"],
                "benchmark_symbol": request["benchmark_symbol"],
                "date_from": request["date_from"],
                "date_to": request["date_to"],
                "session_mode": request["session_mode"],
                "initial_capital": request["initial_capital"],
                "commission_per_share": request["commission_per_share"],
                "slippage_bps": request["slippage_bps"],
                "progress": 0,
                "trade_count": 0,
                "net_pnl": 0,
                "total_return_pct": 0,
                "sharpe": 0,
                "max_drawdown_pct": 0,
                "win_rate": 0,
                "duration_s": 0,
                "params": request["params"],
                "metrics": {},
                "extra": extra,
                "error": "",
                "started_at": "",
                "finished_at": "",
            },
        )

    def _create_batch_record(self, request: dict, planned_runs: list[dict]) -> dict:
        return self.pb.create_record(
            BATCH_COLLECTION,
            {
                "name": request["name"],
                "status": "queued",
                "environment": BACKTEST_ENVIRONMENT,
                "source_environment": request["source_environment"],
                "symbol_source": request["symbol_source"],
                "symbols": request["symbols_text"],
                "benchmark_symbol": request["benchmark_symbol"],
                "date_from": request["date_from"],
                "date_to": request["date_to"],
                "session_mode": request["session_mode"],
                "variant_count": len(planned_runs),
                "completed_count": 0,
                "best_run_id": "",
                "best_variant_label": "",
                "best_total_return_pct": 0,
                "best_sharpe": 0,
                "leaderboard": [],
                "params": {},
                "extra": {
                    "requested_symbols": request["symbols"],
                    "max_symbols": request["max_symbols"],
                    "requested_symbol_source": request.get("requested_symbol_source") or request["symbol_source"],
                    "historical_targets_replay": bool(request.get("historical_targets_replay")),
                    "warmup_bars": request.get("warmup_bars", BACKTEST_WARMUP_BARS),
                    "scan_warmup_bars": request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)),
                    "premarket_cutoff_time": request.get("premarket_cutoff_time", DEFAULT_SCAN_CUTOFF_TIME),
                    "scan_session_mode": request.get("scan_session_mode", "extended"),
                    "retention_limit": request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT),
                    "preflight_backfill": bool(request.get("preflight_backfill", True)),
                    "backfill_concurrency": int(request.get("backfill_concurrency", DEFAULT_BACKTEST_BACKFILL_CONCURRENCY) or DEFAULT_BACKTEST_BACKFILL_CONCURRENCY),
                    "backfill_symbol_timeout_s": int(
                        request.get("backfill_symbol_timeout_s", DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)
                        or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS
                    ),
                    "backfill_max_batches": int(
                        request.get("backfill_max_batches", DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES)
                        or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES
                    ),
                    "backfill_history_timeout_s": int(
                        request.get("backfill_history_timeout_s", DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)
                        or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS
                    ),
                    "backfill_history_max_retries": int(
                        request.get("backfill_history_max_retries")
                        if request.get("backfill_history_max_retries") is not None
                        else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
                    ),
                    **self._build_execution_extra(request),
                    **build_runtime_timestamps(),
                },
                "error": "",
                "started_at": "",
                "finished_at": "",
            },
        )

    def _get_batch(self, batch_id: str) -> Optional[dict]:
        safe_id = str(batch_id or "").strip().replace('"', '\\"')
        if not safe_id:
            return None
        return self.pb.get_first_record(BATCH_COLLECTION, filter=f'id = "{safe_id}"')

    def _update_batch(self, batch_id: str, patch: dict):
        if not batch_id:
            return
        try:
            self.pb.update_record(BATCH_COLLECTION, batch_id, patch)
        except Exception:
            traceback.print_exc()

    def _summarize_run_for_batch(self, run_id: str, request: dict, outcome: dict) -> dict:
        metrics = outcome.get("metrics") or {}
        return {
            "run_id": run_id,
            "name": request.get("name") or "",
            "variant_label": request.get("variant_label") or "",
            "variant_index": int(request.get("variant_index", 0) or 0),
            "status": outcome.get("status") or "completed",
            "trade_count": int(metrics.get("trade_count", 0) or 0),
            "net_pnl": float(metrics.get("net_pnl", 0) or 0),
            "total_return_pct": float(metrics.get("total_return_pct", 0) or 0),
            "sharpe": float(metrics.get("sharpe", 0) or 0),
            "max_drawdown_pct": float(metrics.get("max_drawdown_pct", 0) or 0),
            "win_rate": float(metrics.get("win_rate", 0) or 0),
            "signal_fill_rate": float(metrics.get("signal_fill_rate", 0) or 0),
            "win_loss_ratio": float(metrics.get("win_loss_ratio", 0) or 0),
            "backtest_reverse_signal_count": int(metrics.get("backtest_reverse_signal_count", 0) or 0),
            "analysis_summary": deepcopy(metrics.get("analysis_summary") or {}),
            "strategy_tag": request.get("strategy_tag") or "",
            "strategy_params": deepcopy((request.get("params") or {}).get("strategy_params") or {}),
            "error": outcome.get("error") or "",
        }

    def _collect_batch_run_summaries(self, batch: dict) -> list[dict]:
        extra = batch.get("extra") or {}
        run_ids = list(extra.get("run_ids") or [])
        summaries = []
        for run_id in run_ids:
            run = self._get_run(str(run_id or ""))
            if not run:
                continue
            params = self._parse_object(run.get("params"))
            run_extra = self._parse_object(run.get("extra"))
            run_metrics = self._parse_object(run.get("metrics"))
            summaries.append(
                {
                    "run_id": run.get("id") or "",
                    "name": run.get("name") or "",
                    "variant_label": run_extra.get("variant_label") or "",
                    "variant_index": int(run_extra.get("variant_index", 0) or 0),
                    "status": run.get("status") or "",
                    "trade_count": int(run.get("trade_count", 0) or 0),
                    "net_pnl": float(run.get("net_pnl", 0) or 0),
                    "total_return_pct": float(run.get("total_return_pct", 0) or 0),
                    "sharpe": float(run.get("sharpe", 0) or 0),
                    "max_drawdown_pct": float(run.get("max_drawdown_pct", 0) or 0),
                    "win_rate": float(run.get("win_rate", 0) or 0),
                    "signal_fill_rate": float(run_metrics.get("signal_fill_rate", 0) or 0),
                    "win_loss_ratio": float(run_metrics.get("win_loss_ratio", 0) or 0),
                    "backtest_reverse_signal_count": int(run_metrics.get("backtest_reverse_signal_count", 0) or 0),
                    "analysis_summary": deepcopy(run_metrics.get("analysis_summary") or {}),
                    "strategy_tag": run_extra.get("strategy_tag") or "",
                    "strategy_params": deepcopy(params.get("strategy_params") or {}),
                    "error": run.get("error") or "",
                }
            )
        summaries.sort(key=lambda item: (item["variant_index"], item["name"]))
        return summaries

    def _sync_batch_after_run_cleanup(self, run: dict, deleted_run_id: str):
        extra = self._parse_object(run.get("extra"))
        batch_id = str(extra.get("batch_id") or "").strip()
        if not batch_id:
            return
        batch = self._get_batch(batch_id)
        if not batch:
            return
        batch_extra = self._parse_object(batch.get("extra"))
        run_ids = [item for item in list(batch_extra.get("run_ids") or []) if str(item or "") != deleted_run_id]
        if not run_ids:
            self.pb.delete_record(BATCH_COLLECTION, batch_id)
            return

        updated_extra = dict(batch_extra)
        updated_extra["run_ids"] = run_ids
        summaries = self._collect_batch_run_summaries({**batch, "extra": updated_extra})
        self._update_batch_summary(batch_id, batch, summaries, extra_patch=updated_extra)

    def _cleanup_batch(self, batch_id: str) -> dict:
        safe_batch_id = str(batch_id or "").strip()
        if not safe_batch_id:
            return {"ok": False, "error": "missing_batch_id"}
        with self._lock:
            if self.is_running() and safe_batch_id == self._active_batch_id:
                return {"ok": False, "error": "batch_is_active", "batch_id": safe_batch_id}

        batch = self._get_batch(safe_batch_id)
        if not batch:
            return {"ok": False, "error": "batch_not_found", "batch_id": safe_batch_id}

        deleted_runs = 0
        deleted_trades = 0
        deleted_indicators = 0
        deleted_signals = 0
        deleted_targets = 0
        batch_extra = self._parse_object(batch.get("extra"))
        run_ids = [str(item or "").strip() for item in list(batch_extra.get("run_ids") or []) if str(item or "").strip()]
        if not run_ids:
            run_ids = [str(item.get("run_id") or "").strip() for item in self._collect_batch_run_summaries(batch) if str(item.get("run_id") or "").strip()]
        for run_id in run_ids:
            deleted = self._delete_run_series(run_id)
            deleted_trades += int(deleted.get("deleted_trades", 0) or 0)
            deleted_indicators += int(deleted.get("deleted_indicators", 0) or 0)
            deleted_signals += int(deleted.get("deleted_signals", 0) or 0)
            deleted_targets += int(deleted.get("deleted_targets", 0) or 0)
            if run_id and self._get_run(run_id):
                self.pb.delete_record(RUN_COLLECTION, run_id)
                deleted_runs += 1

        self.pb.delete_record(BATCH_COLLECTION, safe_batch_id)
        return {
            "ok": True,
            "batch_id": safe_batch_id,
            "deleted_runs": deleted_runs,
            "deleted_trades": deleted_trades,
            "deleted_indicators": deleted_indicators,
            "deleted_signals": deleted_signals,
            "deleted_targets": deleted_targets,
            "deleted_batch": True,
        }

    def _apply_retention_limits(
        self,
        retention_limit: int = DEFAULT_BACKTEST_RETENTION_LIMIT,
        exclude_run_ids: set[str] | None = None,
        exclude_batch_ids: set[str] | None = None,
    ) -> dict:
        limit = self._normalize_positive_int(
            retention_limit,
            default=DEFAULT_BACKTEST_RETENTION_LIMIT,
            minimum=1,
            maximum=MAX_BACKTEST_RETENTION_LIMIT,
        )
        skip_runs = {str(item or "").strip() for item in (exclude_run_ids or set()) if str(item or "").strip()}
        skip_batches = {str(item or "").strip() for item in (exclude_batch_ids or set()) if str(item or "").strip()}
        deleted_batches = 0
        deleted_runs = 0

        try:
            batches = self.pb.get_all_records(BATCH_COLLECTION, sort="-created", max_pages=10)
            for index, batch in enumerate(batches):
                batch_id = str(batch.get("id") or "").strip()
                if not batch_id or index < limit or batch_id in skip_batches:
                    continue
                result = self._cleanup_batch(batch_id)
                if result.get("ok"):
                    deleted_batches += 1
        except Exception:
            traceback.print_exc()

        try:
            runs = self.pb.get_all_records(RUN_COLLECTION, sort="-created", max_pages=20)
            standalone_runs = []
            for run in runs:
                run_id = str(run.get("id") or "").strip()
                if not run_id:
                    continue
                extra = run.get("extra") or {}
                if isinstance(extra, str):
                    extra = self._parse_object(extra)
                if str((extra or {}).get("batch_id") or "").strip():
                    continue
                standalone_runs.append(run)
            for index, run in enumerate(standalone_runs):
                run_id = str(run.get("id") or "").strip()
                if not run_id or index < limit or run_id in skip_runs:
                    continue
                result = self.cleanup(run_id=run_id)
                if result.get("ok"):
                    deleted_runs += 1
        except Exception:
            traceback.print_exc()

        return {
            "ok": True,
            "retention_limit": limit,
            "deleted_batches": deleted_batches,
            "deleted_runs": deleted_runs,
        }

    def _get_run(self, run_id: str) -> Optional[dict]:
        safe_id = str(run_id or "").strip().replace('"', '\\"')
        if not safe_id:
            return None
        return self.pb.get_first_record(RUN_COLLECTION, filter=f'id = "{safe_id}"')

    def _update_run(self, run_id: str, patch: dict):
        if not run_id:
            return
        try:
            self.pb.update_record(RUN_COLLECTION, run_id, patch)
        except Exception:
            traceback.print_exc()

    def _set_progress(self, status: str, stage: str, message: str, progress: int):
        with self._lock:
            progress_value = max(0, min(100, int(progress)))
            previous = dict(self._progress or {})
            same_active = (
                str(previous.get("run_id") or "") == str(self._active_run_id or "")
                and str(previous.get("batch_id") or "") == str(self._active_batch_id or "")
            )
            if same_active and status in {"running", "cancelling"} and str(previous.get("status") or "") in {"queued", "running", "cancelling"}:
                progress_value = max(progress_value, int(previous.get("progress", 0) or 0))
            if same_active and str(previous.get("status") or "") == "cancelling" and status == "running":
                status = "cancelling"
            self._progress = {
                "status": status,
                "run_id": self._active_run_id,
                "batch_id": self._active_batch_id,
                "mode": "batch" if self._active_batch_id else "single",
                "progress": progress_value,
                "stage": stage,
                "message": message,
                "updated_at_ms": int(time.time() * 1000),
            }

    def _map_progress(self, progress: int, context: dict | None = None) -> int:
        base = max(0, min(100, int(progress)))
        if not context:
            return base
        start = max(0, min(100, int(context.get("start", 0) or 0)))
        end = max(start, min(100, int(context.get("end", 100) or 100)))
        return int(start + ((end - start) * base / 100.0))

    def _set_progress_context(self, status: str, stage: str, message: str, progress: int, context: dict | None = None):
        prefix = str((context or {}).get("prefix") or "")
        self._set_progress(status, stage, f"{prefix}{message}", self._map_progress(progress, context))

    def _build_run_extra(
        self,
        request: dict,
        symbols: list[str],
        metrics: dict | None = None,
        daily_equity: list | None = None,
        benchmark_points: list | None = None,
        tv_parity: dict | None = None,
        backtest_indicator_capture: dict | None = None,
        backtest_signal_capture: dict | None = None,
        backtest_target_capture: dict | None = None,
        backtest_reverse_capture: dict | None = None,
        historical_targeting: dict | None = None,
        analysis_report: dict | None = None,
        preflight_backfill: dict | None = None,
    ) -> dict:
        extra = {
            "force_flat_eod": request["force_flat_eod"],
            "requested_symbols": request["symbols"],
            "resolved_symbols": symbols,
            "max_symbols": request["max_symbols"],
            "machine_profile": str(request.get("machine_profile") or ""),
            "strategy_tag": request["strategy_tag"],
            "compare_with_tv": bool(request.get("compare_with_tv", True)),
            "compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "persist_backtest_indicators": bool(request.get("persist_backtest_indicators", False)),
            "warmup_bars": int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            "scan_warmup_bars": int(request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)) or BACKTEST_WARMUP_BARS),
            "daily_selected_only": bool(request.get("daily_selected_only", False)),
            "daily_selection_require_sd_trigger": bool(request.get("daily_selection_require_sd_trigger", False)),
            "daily_selection_reuse_live_admission": bool(request.get("daily_selection_reuse_live_admission", False)),
            "daily_selection_sd_mode": str(request.get("daily_selection_sd_mode") or "hard"),
            "daily_selection_candidate_limit": int(request.get("daily_selection_candidate_limit", 0) or 0),
            "daily_selection_cache_enabled": bool(request.get("daily_selection_cache_enabled", False)),
            "daily_selection_cache_mode": str(request.get("daily_selection_cache_mode") or "use_or_build"),
            "daily_selection_cache_force_rebuild": bool(request.get("daily_selection_cache_force_rebuild", False)),
            "daily_selection_cache_trust_existing": bool(request.get("daily_selection_cache_trust_existing", False)),
            "daily_selection_cache_revalidate_input_hash": bool(request.get("daily_selection_cache_revalidate_input_hash", False)),
            "daily_scan_min_avg_10d_volume": float(request.get("daily_scan_min_avg_10d_volume", 0) or 0),
            "daily_scan_min_premarket_volume": float(request.get("daily_scan_min_premarket_volume", 0) or 0),
            "daily_scan_min_atr_pct": float(request.get("daily_scan_min_atr_pct", 0) or 0),
            "daily_scan_min_abs_day_change_pct": float(request.get("daily_scan_min_abs_day_change_pct", 0) or 0),
            "premarket_cutoff_time": str(request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME),
            "scan_session_mode": str(request.get("scan_session_mode") or "extended"),
            "retention_limit": int(request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT) or DEFAULT_BACKTEST_RETENTION_LIMIT),
            "preflight_backfill": bool(request.get("preflight_backfill", True)),
            "backfill_concurrency": int(request.get("backfill_concurrency", DEFAULT_BACKTEST_BACKFILL_CONCURRENCY) or DEFAULT_BACKTEST_BACKFILL_CONCURRENCY),
            "backfill_symbol_timeout_s": int(
                request.get("backfill_symbol_timeout_s", DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)
                or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS
            ),
            "backfill_max_batches": int(
                request.get("backfill_max_batches", DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES)
                or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES
            ),
            "backfill_history_timeout_s": int(
                request.get("backfill_history_timeout_s", DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)
                or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS
            ),
            "backfill_history_max_retries": int(
                request.get("backfill_history_max_retries")
                if request.get("backfill_history_max_retries") is not None
                else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
            ),
            "resource_guard": {
                "enabled": bool(request.get("resource_guard_enabled", DEFAULT_BACKTEST_RESOURCE_GUARD_ENABLED)),
                "max_load": float(request.get("resource_guard_max_load", DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD) or 0),
                "min_available_mb": int(request.get("resource_guard_min_available_mb", DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB) or 0),
                "max_rss_mb": int(request.get("resource_guard_max_rss_mb", DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB) or 0),
                "sleep_s": float(request.get("resource_guard_sleep_s", DEFAULT_BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS) or 0),
                "check_steps": int(request.get("resource_guard_check_steps", DEFAULT_BACKTEST_RESOURCE_GUARD_CHECK_STEPS) or 0),
            },
            **self._build_execution_extra(request),
            **build_runtime_timestamps(),
        }
        if request.get("batch_id"):
            extra.update(
                {
                    "batch_id": request["batch_id"],
                    "batch_name": request.get("batch_name") or "",
                    "variant_label": request.get("variant_label") or "",
                    "variant_index": int(request.get("variant_index", 0) or 0),
                    "variant_count": int(request.get("variant_count", 0) or 0),
                }
            )
        if metrics is not None:
            extra["monthly_returns"] = metrics.get("monthly_returns") or []
            extra["portfolio_risk"] = metrics.get("portfolio_risk") or {}
            extra["portfolio_rejection_counts"] = metrics.get("portfolio_rejection_counts") or {}
            extra["execution_cost_summary"] = metrics.get("execution_cost_summary") or {}
            extra["daily_scan_match_diagnostics"] = metrics.get("daily_scan_match_diagnostics") or {}
            extra["daily_selection_cache"] = metrics.get("daily_selection_cache") or {}
            audit = metrics.get("backtest_audit") or {}
            if audit:
                extra["backtest_audit_summary"] = {
                    "focus_date": audit.get("focus_date") or "",
                    "focus_symbols": audit.get("focus_symbols") or [],
                    "event_count": audit.get("event_count") or 0,
                    "event_type_counts": audit.get("event_type_counts") or {},
                    "stage_counts": audit.get("stage_counts") or {},
                    "timeline_truncated": bool(audit.get("timeline_truncated")),
                    "daily_summary": audit.get("daily_summary") or [],
                }
        if daily_equity is not None:
            extra["equity_curve"] = daily_equity
        if benchmark_points is not None:
            extra["benchmark_curve"] = benchmark_points
        if tv_parity is not None:
            extra["tv_parity"] = tv_parity
        if backtest_indicator_capture is not None:
            extra["backtest_indicator_capture"] = backtest_indicator_capture
        if backtest_signal_capture is not None:
            extra["backtest_signal_capture"] = backtest_signal_capture
        if backtest_target_capture is not None:
            extra["backtest_target_capture"] = backtest_target_capture
        if backtest_reverse_capture is not None:
            extra["backtest_reverse_capture"] = backtest_reverse_capture
        if historical_targeting is not None:
            extra["historical_targeting"] = historical_targeting
        if analysis_report is not None:
            extra["analysis_report"] = analysis_report
        if preflight_backfill is not None:
            extra["preflight_backfill"] = preflight_backfill
        return extra
