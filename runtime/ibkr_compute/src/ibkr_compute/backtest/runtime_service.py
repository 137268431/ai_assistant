from __future__ import annotations

import gc
import json
import math
import os
import sqlite3
import statistics
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ibkr_compute.backtest import request_utils
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS, IndicatorEngine
from ibkr_compute.core.risk_management import compute_atr_tightened_stop
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.bar_coverage_daily import (
    DAILY_COVERAGE_OK_STATUSES,
    build_range_daily_coverage,
    load_daily_coverage_rows,
    market_date_from_ms,
    summarize_symbol_daily_coverage,
    trading_date_strings_from_ms,
)
from ibkr_compute.market.data_backfill import DataBackfill
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite, upsert_bar_coverage_daily, upsert_bars
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    ET,
    HIGHER_INTERVALS,
    build_runtime_timestamps,
    build_signal_id,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
    normalize_interval,
)
from ibkr_compute.order.order_lifecycle import DEFAULT_POSITION_LIMIT_MAX as LIVE_DEFAULT_POSITION_LIMIT_MAX
from ibkr_compute.signal.signal_processor import (
    DEFAULT_ORDER_WINDOW_END as LIVE_DEFAULT_ORDER_WINDOW_END,
    DEFAULT_SIGNAL_EXPIRY_MINUTES as LIVE_DEFAULT_SIGNAL_EXPIRY_MINUTES,
    DEFAULT_TRADE_WINDOW_END as LIVE_DEFAULT_TRADE_WINDOW_END,
    DEFAULT_TRADE_WINDOW_START as LIVE_DEFAULT_TRADE_WINDOW_START,
)
from ibkr_compute.workflows.daily_scanner import DailyScanner, _load_scan_settings

BACKTEST_ENVIRONMENT = "backtest"
RUN_COLLECTION = "ibkr_backtest_runs"
TRADE_COLLECTION = "ibkr_backtest_trades"
BATCH_COLLECTION = "ibkr_backtest_batches"
BACKTEST_INDICATOR_COLLECTION = "ibkr_backtest_indicators"
BACKTEST_SIGNAL_COLLECTION = "ibkr_backtest_signals"
BACKTEST_TARGET_COLLECTION = "ibkr_backtest_targets"
BACKTEST_REVERSE_SIGNAL_COLLECTION = "ibkr_backtest_reverse_signals"
TV_INDICATOR_COLLECTION = "tv_indicators"
TV_SIGNAL_COLLECTION = "tv_signals"
RUN_STATUS_VALUES = {"queued", "running", "completed", "failed", "cancelled"}
SESSION_MODE_VALUES = {"extended", "regular"}
SYMBOL_SOURCE_VALUES = {"manual", "targets", "watchlist", "daily_scan_replay"}
EXECUTION_MODEL_VALUES = {"symbol_independent", "portfolio_stream"}
BORROW_LIMIT_MODE_VALUES = {"none", "fixed", "account_buying_power"}
SIGNAL_PRIORITY_VALUES = {"daily_target_rank", "signal_quality", "liquidity"}
MANUAL_CONFIRM_MODE_VALUES = {"auto", "delayed", "strict"}
DEFAULT_MAX_SYMBOLS = 20
DEFAULT_MAX_PAGES = 1200
MAX_REPLAY_ROWS = 240
MAX_BATCH_VARIANTS = 16
TV_COMPARE_SAMPLE_LIMIT = 8
BACKTEST_WARMUP_BARS = 320
DEFAULT_SCAN_CUTOFF_TIME = "09:20"
DEFAULT_BACKTEST_RETENTION_LIMIT = 30
MAX_BACKTEST_RETENTION_LIMIT = 200
MAX_BACKTEST_WARMUP_BARS = 2000
DEFAULT_BACKTEST_BACKFILL_CONCURRENCY = 4
MAX_BACKTEST_BACKFILL_CONCURRENCY = 5
BACKTEST_COVERAGE_EDGE_GRACE_DAYS = 5
DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS = 600
MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS = 3600
DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES = 120
MAX_BACKTEST_BACKFILL_MAX_BATCHES = 240
DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS = 20
MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS = 60
DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES = 1
MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES = 5
BACKTEST_PREFLIGHT_HEARTBEAT_SECONDS = 15
SCAN_INTERVALS = tuple(COMPUTE_INTERVALS)
BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD = 0.05
BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD = 0.1
BACKTEST_SQLITE_PATH = os.environ.get("PB_SQLITE_PATH", "/opt/pocketbase/pb_data/data.db")


def _hhmm_from_tuple(value: tuple[int, int]) -> str:
    return f"{int(value[0]):02d}:{int(value[1]):02d}"


DEFAULT_PORTFOLIO_TRADE_WINDOW_START = _hhmm_from_tuple(LIVE_DEFAULT_TRADE_WINDOW_START)
DEFAULT_PORTFOLIO_TRADE_WINDOW_END = _hhmm_from_tuple(LIVE_DEFAULT_TRADE_WINDOW_END)
DEFAULT_PORTFOLIO_ORDER_WINDOW_END = _hhmm_from_tuple(LIVE_DEFAULT_ORDER_WINDOW_END)
DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES = LIVE_DEFAULT_SIGNAL_EXPIRY_MINUTES
DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX = LIVE_DEFAULT_POSITION_LIMIT_MAX

TV_INDICATOR_EXTRA_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema_fast",
    "ema_slow",
    "ema_trend",
    "ema_longest",
    "slope_slow",
    "slope_trend",
    "slope_longest",
    "ema_bull_touch",
    "ema_bear_touch",
    "ema_bullish",
    "ema_bearish",
    "fractal_bull",
    "fractal_bear",
    "sd_reg",
    "sd_std_dev",
    "sd_upper",
    "sd_lower",
    "sd_zone",
    "sd_trend",
    "dtp_avg",
    "dtp_atr",
    "dtp_dir",
    "dtp_phase",
    "dtp_phase_bars",
    "atr",
    "atr_raw",
    "atr_pct",
    "crsi",
    "crsi_db",
    "crsi_ub",
    "crsi_ob",
    "crsi_os",
    "crsi_bull_div",
    "crsi_bear_div",
    "crsi_hid_bull",
    "crsi_hid_bear",
    "obv_rsi",
    "obv_bull_div",
    "obv_bear_div",
    "obv_hid_bull",
    "obv_hid_bear",
    "vwap",
    "vwap_upper1",
    "vwap_lower1",
    "vwap_upper2",
    "vwap_lower2",
    "vwap_dist",
    "vwap_bullish",
    "trend_dir",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
)

TV_SIGNAL_CORE_FIELDS = (
    "signal_id",
    "signal",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "rr",
    "shares",
    "interval",
    "bar_time_ms",
    "us_time",
    "cn_time",
    "reason",
)

TV_SIGNAL_EXTRA_FIELDS = (
    "close",
    "atr",
    "atr_raw",
    "atr_pct",
    "sd_zone",
    "sd_trend",
    "dtp_dir",
    "dtp_phase",
    "crsi_state",
    "sl_dist_pct",
    "sl_atr_ratio",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
    "bar_time_ms",
    "chart_tf",
)


class BacktestCancelled(Exception):
    pass


WATCHLIST_SYMBOL_ROLE_TRADE = "trade"


class BacktestService:
    def __init__(self, pb_client: PBClient, account_snapshot_provider: Callable[..., dict] | None = None):
        self.pb = pb_client
        self.account_snapshot_provider = account_snapshot_provider
        self._history_broker = self._build_history_broker() if pb_client else None
        self.data_backfill = DataBackfill(broker=self._history_broker) if pb_client else None
        self.conid_resolver = ConidResolver(pb_client=pb_client, broker=self._history_broker) if pb_client else None
        if self.conid_resolver is not None:
            try:
                self.conid_resolver.load_cache_from_pb()
            except Exception:
                pass
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._active_run_id = ""
        self._active_batch_id = ""
        self._active_payload: Dict[str, Any] = {}
        self._progress = {
            "status": "idle",
            "run_id": "",
            "batch_id": "",
            "mode": "single",
            "progress": 0,
            "stage": "idle",
            "message": "",
            "updated_at_ms": 0,
        }

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "running": self.is_running(),
                "run_id": self._active_run_id,
                "batch_id": self._active_batch_id,
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
        return {
            "execution_model": request.get("execution_model", "portfolio_stream"),
            "borrow_limit_mode": request.get("borrow_limit_mode", "none"),
            "max_borrow_amount": float(request.get("max_borrow_amount", 0) or 0),
            "position_limit_max": int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX),
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
            self._progress = {
                "status": status,
                "run_id": self._active_run_id,
                "batch_id": self._active_batch_id,
                "mode": "batch" if self._active_batch_id else "single",
                "progress": max(0, min(100, int(progress))),
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
            "strategy_tag": request["strategy_tag"],
            "compare_with_tv": bool(request.get("compare_with_tv", True)),
            "compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "persist_backtest_indicators": bool(request.get("persist_backtest_indicators", False)),
            "warmup_bars": int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            "scan_warmup_bars": int(request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)) or BACKTEST_WARMUP_BARS),
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
            extra["daily_scan_match_diagnostics"] = metrics.get("daily_scan_match_diagnostics") or {}
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

    def _escape_pb_filter_value(self, raw_value: Any) -> str:
        return str(raw_value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _normalize_symbol_tokens(self, raw_value: Any) -> list[str]:
        items = []
        if isinstance(raw_value, list):
            source = raw_value
        else:
            source = str(raw_value or "").replace("\n", ",").split(",")
        for item in source:
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in items:
                continue
            items.append(symbol)
        return items

    def _compute_symbol_overlap(self, left_symbols: list[str], right_symbols: list[str]) -> dict:
        left_set = {str(item or "").strip().upper() for item in left_symbols if str(item or "").strip()}
        right_set = {str(item or "").strip().upper() for item in right_symbols if str(item or "").strip()}
        if not left_set and not right_set:
            return {"exact_match": True, "overlap_ratio": 1.0, "shared_count": 0}
        if not left_set or not right_set:
            return {"exact_match": False, "overlap_ratio": 0.0, "shared_count": 0}
        shared = left_set & right_set
        union = left_set | right_set
        overlap_ratio = (len(shared) / len(union)) if union else 0.0
        return {
            "exact_match": left_set == right_set,
            "overlap_ratio": round(overlap_ratio, 4),
            "shared_count": len(shared),
        }

    def _coerce_run_for_analysis(self, run: dict) -> dict:
        metrics = self._parse_object(run.get("metrics"))
        extra = self._parse_object(run.get("extra"))
        params = self._parse_object(run.get("params"))
        return {
            "run_id": str(run.get("id") or ""),
            "name": str(run.get("name") or ""),
            "status": str(run.get("status") or ""),
            "symbols": self._normalize_symbol_tokens(
                run.get("symbols") or extra.get("resolved_symbols") or extra.get("requested_symbols") or []
            ),
            "date_from": str(run.get("date_from") or ""),
            "date_to": str(run.get("date_to") or ""),
            "source_environment": str(run.get("source_environment") or ""),
            "symbol_source": str(run.get("symbol_source") or ""),
            "session_mode": str(run.get("session_mode") or ""),
            "trade_count": int(run.get("trade_count", metrics.get("trade_count", 0)) or 0),
            "net_pnl": float(run.get("net_pnl", metrics.get("net_pnl", 0)) or 0),
            "total_return_pct": float(run.get("total_return_pct", metrics.get("total_return_pct", 0)) or 0),
            "sharpe": float(run.get("sharpe", metrics.get("sharpe", 0)) or 0),
            "max_drawdown_pct": float(run.get("max_drawdown_pct", metrics.get("max_drawdown_pct", 0)) or 0),
            "win_rate": float(run.get("win_rate", metrics.get("win_rate", 0)) or 0),
            "signal_fill_rate": float(metrics.get("signal_fill_rate", 0) or 0),
            "strategy_tag": str(extra.get("strategy_tag") or ""),
            "strategy_params": deepcopy(params.get("strategy_params") or {}),
            "batch_id": str(extra.get("batch_id") or ""),
            "created": str(run.get("created") or ""),
            "metrics": metrics,
            "extra": extra,
        }

    def _load_comparable_completed_runs(
        self,
        request: dict,
        exclude_run_ids: set[str] | None = None,
        exclude_batch_ids: set[str] | None = None,
        max_pages: int = 12,
    ) -> list[dict]:
        if not self.pb:
            return []
        exclude_ids = {str(item or "").strip() for item in (exclude_run_ids or set()) if str(item or "").strip()}
        exclude_batches = {str(item or "").strip() for item in (exclude_batch_ids or set()) if str(item or "").strip()}
        filter_text = (
            f'status = "completed" && '
            f'source_environment = "{self._escape_pb_filter_value(request.get("source_environment"))}" && '
            f'symbol_source = "{self._escape_pb_filter_value(request.get("symbol_source"))}" && '
            f'date_from = "{self._escape_pb_filter_value(request.get("date_from"))}" && '
            f'date_to = "{self._escape_pb_filter_value(request.get("date_to"))}" && '
            f'session_mode = "{self._escape_pb_filter_value(request.get("session_mode"))}"'
        )
        rows = self.pb.get_all_records(
            RUN_COLLECTION,
            filter=filter_text,
            sort="-created",
            max_pages=max_pages,
        )
        items = []
        for row in rows:
            candidate = self._coerce_run_for_analysis(row)
            candidate_run_id = candidate["run_id"]
            if not candidate_run_id or candidate_run_id in exclude_ids:
                continue
            if candidate["batch_id"] and candidate["batch_id"] in exclude_batches:
                continue
            items.append(candidate)
        return items

    def _pick_best_comparable_run(
        self,
        current_symbols: list[str],
        current_strategy_tag: str,
        candidates: list[dict],
    ) -> dict | None:
        if not candidates:
            return None
        scored = []
        safe_strategy_tag = str(current_strategy_tag or "").strip()
        for item in candidates:
            overlap = self._compute_symbol_overlap(current_symbols, item.get("symbols") or [])
            similarity_score = 0
            if overlap["exact_match"]:
                similarity_score += 3
            elif overlap["overlap_ratio"] >= 0.6:
                similarity_score += 2
            elif overlap["overlap_ratio"] > 0:
                similarity_score += 1
            if safe_strategy_tag and safe_strategy_tag == str(item.get("strategy_tag") or "").strip():
                similarity_score += 1
            scored.append(
                (
                    similarity_score,
                    float(overlap["overlap_ratio"]),
                    float(item.get("total_return_pct", 0) or 0),
                    float(item.get("sharpe", 0) or 0),
                    -float(item.get("max_drawdown_pct", 0) or 0),
                    item,
                    overlap,
                )
            )
        scored.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]), reverse=True)
        if not scored:
            return None
        best = dict(scored[0][5])
        best["scope_similarity"] = {
            "score": scored[0][0],
            **scored[0][6],
        }
        return best

    def _build_strategy_param_delta(self, current_params: dict, reference_params: dict, limit: int = 4) -> list[dict]:
        diffs = []
        current = current_params or {}
        reference = reference_params or {}
        for key in sorted(set(current.keys()) | set(reference.keys())):
            current_value = current.get(key)
            reference_value = reference.get(key)
            if current_value == reference_value:
                continue
            diffs.append(
                {
                    "key": key,
                    "current": current_value,
                    "reference": reference_value,
                }
            )
            if len(diffs) >= max(1, int(limit or 0)):
                break
        return diffs

    def _build_backtest_recommendations(self, request: dict, metrics: dict, tv_summary: dict, comparison: dict | None = None) -> dict:
        strengths = []
        risks = []
        suggestions = []

        total_return_pct = float(metrics.get("total_return_pct", 0) or 0)
        sharpe = float(metrics.get("sharpe", 0) or 0)
        win_rate = float(metrics.get("win_rate", 0) or 0)
        signal_fill_rate = float(metrics.get("signal_fill_rate", 0) or 0)
        max_drawdown_pct = float(metrics.get("max_drawdown_pct", 0) or 0)
        profit_factor = float(metrics.get("profit_factor", 0) or 0)
        win_loss_ratio = float(metrics.get("win_loss_ratio", 0) or 0)
        reverse_count = int(metrics.get("backtest_reverse_signal_count", 0) or 0)
        trade_count = int(metrics.get("trade_count", 0) or 0)
        target_count = int(metrics.get("backtest_target_count", 0) or 0)
        tv_status = str((tv_summary or {}).get("status") or "disabled")
        historical_targeting = metrics.get("historical_targeting") or {}
        reverse_actions = metrics.get("backtest_reverse_action_breakdown") or {}
        reverse_cancel = int(reverse_actions.get("cancel", 0) or 0)
        reverse_adjust = int(reverse_actions.get("adjust_sl", 0) or 0) + int(reverse_actions.get("adjust_tp", 0) or 0)

        score = 0
        if total_return_pct > 0:
            score += 2
            strengths.append("收益为正，当前参数组合具备继续迭代价值。")
        else:
            risks.append("总收益为负，本期参数/筛股组合没有跑出有效优势。")
        if sharpe >= 1:
            score += 2
            strengths.append("Sharpe 大于等于 1，风险调整后的回报相对稳定。")
        elif sharpe > 0:
            score += 1
        else:
            risks.append("Sharpe 偏弱，说明收益波动质量还不够稳。")
        if win_rate >= 55:
            score += 1
            strengths.append("胜率超过 55%，信号方向判断整体不差。")
        if signal_fill_rate >= 50:
            score += 1
            strengths.append("信号成交率较高，入场链路摩擦可控。")
        elif trade_count > 0:
            risks.append("信号成交率偏低，很多信号没有转成实际成交。")
        if max_drawdown_pct <= 2:
            score += 1
        elif max_drawdown_pct >= max(3.0, abs(total_return_pct) * 1.5):
            risks.append("回撤相对收益偏大，说明风控与筛股还需要继续收紧。")
        if tv_status in {"warn", "fail", "error"}:
            risks.append(f"TV 对齐状态为 {tv_status}，分析结论仍需先建立在 bars 正确性之上。")
            score -= 1
        if request.get("symbol_source") == "daily_scan_replay" and target_count > 0:
            strengths.append(f"历史盘前选股已成功回放，共覆盖 {historical_targeting.get('target_date_count', 0)} 个交易日。")
        if reverse_count > max(0, trade_count):
            risks.append("反转动作次数多于成交笔数，说明冲突阈值可能偏敏感。")

        if signal_fill_rate < 35:
            suggestions.append("优先提高信号成交率：复查入场条件、滑点假设、盘前 cutoff 和 session_mode。")
        daily_scan_match = metrics.get("daily_scan_match_diagnostics") or {}
        if request.get("symbol_source") == "daily_scan_replay" and int(daily_scan_match.get("generated_signal_count", 0) or 0) > 0:
            selected_day_signal_rate = float(daily_scan_match.get("selected_day_signal_rate_pct", 0) or 0)
            if selected_day_signal_rate < 25:
                risks.append("9:20 日筛入选标的与后续日内信号重合度偏低，开仓数量会被 selection plan 明显压缩。")
                suggestions.append("优先调 daily_scan_replay 命中率：扩大 max_symbols，或放宽盘前成交量/涨跌幅/ATR%门槛后做对照回测。")
        if total_return_pct <= 0 or max_drawdown_pct >= max(3.0, abs(total_return_pct) * 1.5):
            suggestions.append("优先收紧风险：减少弱分数标的、降低 max_symbols，或提高止损纪律。")
        if tv_status in {"warn", "fail", "error"}:
            suggestions.append("先消除 TV 对齐漂移，再解读策略优劣，避免把数据问题当作策略问题。")
        if reverse_cancel >= max(2, reverse_adjust + 1):
            suggestions.append("反转动作以 cancel 为主，建议复查 indicator_conflict 的弱冲突阈值，避免过早放弃仓位。")
        if request.get("symbol_source") == "daily_scan_replay" and target_count <= 1:
            suggestions.append("历史盘前标的过少，建议扩大回放底池，或放宽 premarket cutoff 前的准备窗口。")
        if win_rate >= 55 and win_loss_ratio < 1:
            suggestions.append("胜率尚可但盈亏比不足，建议复查 rr_ratio / 止盈参数，避免过早止盈。")
        if win_rate < 45 and profit_factor <= 1:
            suggestions.append("胜率和 profit factor 同时偏弱，建议优先缩减入场频率而不是放大仓位。")

        if comparison and comparison.get("improved_vs_baseline"):
            strengths.append("相对同周期历史基准已有明显提升，可作为当前优先候选配置。")
            param_delta = comparison.get("strategy_param_delta") or []
            if param_delta:
                changed_keys = ", ".join(str(item.get("key") or "") for item in param_delta if str(item.get("key") or ""))
                if changed_keys:
                    suggestions.append(f"可优先复核这些参数变化是否稳定有效：{changed_keys}。")
        elif comparison and comparison.get("baseline_exists"):
            risks.append("与同周期历史基准相比尚未形成显著优势，暂不建议直接替换当前候选方案。")

        verdict = "strong" if score >= 5 else ("mixed" if score >= 2 else "weak")
        if verdict == "strong":
            headline = "本期回测表现偏强，收益、风险和成交结构整体可接受。"
        elif verdict == "mixed":
            headline = "本期回测有可用信号，但还存在明确的优化空间。"
        else:
            headline = "本期回测暂未形成稳定优势，优先排查筛股、风险和对齐质量。"

        deduped_suggestions = []
        seen_suggestions = set()
        for item in suggestions:
            if item in seen_suggestions:
                continue
            seen_suggestions.add(item)
            deduped_suggestions.append(item)

        return {
            "verdict": verdict,
            "headline": headline,
            "strengths": strengths[:6],
            "risks": risks[:6],
            "suggestions": deduped_suggestions[:6],
        }

    def _build_run_analysis_report(
        self,
        run_id: str,
        request: dict,
        symbols: list[str],
        metrics: dict,
        tv_parity: dict,
        historical_targeting: dict | None = None,
    ) -> dict:
        current_strategy_tag = str(request.get("strategy_tag") or "").strip()
        current_params = deepcopy((request.get("params") or {}).get("strategy_params") or {})
        batch_id = str(request.get("batch_id") or "").strip()
        candidates = self._load_comparable_completed_runs(
            request,
            exclude_run_ids={run_id},
            exclude_batch_ids={batch_id} if batch_id else set(),
        )
        baseline = self._pick_best_comparable_run(symbols, current_strategy_tag, candidates)
        comparison = {
            "baseline_exists": bool(baseline),
            "improved_vs_baseline": False,
        }
        if baseline:
            delta_return = round(float(metrics.get("total_return_pct", 0) or 0) - float(baseline.get("total_return_pct", 0) or 0), 4)
            delta_sharpe = round(float(metrics.get("sharpe", 0) or 0) - float(baseline.get("sharpe", 0) or 0), 4)
            delta_win_rate = round(float(metrics.get("win_rate", 0) or 0) - float(baseline.get("win_rate", 0) or 0), 4)
            improved = (
                delta_return >= BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD
                or (
                    delta_return >= 0
                    and delta_sharpe >= BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD
                )
            )
            comparison.update(
                {
                    "baseline_run_id": baseline.get("run_id") or "",
                    "baseline_name": baseline.get("name") or "",
                    "baseline_total_return_pct": float(baseline.get("total_return_pct", 0) or 0),
                    "baseline_sharpe": float(baseline.get("sharpe", 0) or 0),
                    "baseline_win_rate": float(baseline.get("win_rate", 0) or 0),
                    "delta_return_pct": delta_return,
                    "delta_sharpe": delta_sharpe,
                    "delta_win_rate": delta_win_rate,
                    "scope_similarity": baseline.get("scope_similarity") or {},
                    "strategy_param_delta": self._build_strategy_param_delta(
                        current_params,
                        deepcopy(baseline.get("strategy_params") or {}),
                    ),
                    "improved_vs_baseline": bool(improved),
                }
            )
        recommendation = self._build_backtest_recommendations(
            request,
            metrics,
            (tv_parity or {}).get("summary") or {},
            comparison=comparison,
        )
        report = {
            "run_id": run_id,
            "baseline_comparison": comparison,
            "recommendation": recommendation,
            "historical_targeting": historical_targeting or {},
            "notification": {
                "should_notify": bool(comparison.get("improved_vs_baseline")),
                "reason": "better_same_period" if comparison.get("improved_vs_baseline") else "",
            },
        }
        report["summary"] = {
            "verdict": recommendation["verdict"],
            "headline": recommendation["headline"],
            "improved_vs_baseline": bool(comparison.get("improved_vs_baseline")),
            "baseline_run_id": comparison.get("baseline_run_id") or "",
            "delta_return_pct": float(comparison.get("delta_return_pct", 0) or 0),
            "delta_sharpe": float(comparison.get("delta_sharpe", 0) or 0),
        }
        return report

    def _notify_backtest_improvement(self, title: str, detail: dict) -> dict:
        if not self.pb:
            return {"ok": False, "notified": False, "error": "pb_client_unavailable"}
        try:
            result = self.pb.notify_ibkr_event(
                title=title,
                detail=detail,
                notify_type="status",
                environment=BACKTEST_ENVIRONMENT,
            )
            return {
                "ok": True,
                "notified": bool(result.get("ok", True)),
            }
        except Exception as exc:
            traceback.print_exc()
            return {
                "ok": False,
                "notified": False,
                "error": str(exc)[:300],
            }

    def _build_batch_experiment_analysis(
        self,
        batch_id: str,
        request: dict,
        summaries: list[dict],
    ) -> dict:
        completed = [item for item in summaries if item.get("status") == "completed"]
        sorted_completed = sorted(
            completed,
            key=lambda item: (
                -float(item.get("total_return_pct", 0) or 0),
                -float(item.get("sharpe", 0) or 0),
                float(item.get("max_drawdown_pct", 0) or 0),
            ),
        )
        best = sorted_completed[0] if sorted_completed else {}
        second = sorted_completed[1] if len(sorted_completed) > 1 else {}
        exclude_run_ids = {str(item.get("run_id") or "") for item in summaries if str(item.get("run_id") or "")}
        historical_candidates = self._load_comparable_completed_runs(
            request,
            exclude_run_ids=exclude_run_ids,
            exclude_batch_ids={batch_id} if batch_id else set(),
        )
        historical_best = self._pick_best_comparable_run(
            self._normalize_symbol_tokens(request.get("symbols") or []),
            str(best.get("strategy_tag") or request.get("strategy_tag") or ""),
            historical_candidates,
        )

        comparison = {
            "best_run_id": best.get("run_id") or "",
            "best_variant_label": best.get("variant_label") or "",
            "variant_count": len(completed),
            "historical_baseline_run_id": historical_best.get("run_id") if historical_best else "",
            "improved_vs_historical": False,
        }
        if best and second:
            comparison["delta_vs_second_best_return_pct"] = round(
                float(best.get("total_return_pct", 0) or 0) - float(second.get("total_return_pct", 0) or 0),
                4,
            )
            comparison["delta_vs_second_best_sharpe"] = round(
                float(best.get("sharpe", 0) or 0) - float(second.get("sharpe", 0) or 0),
                4,
            )
            comparison["strategy_param_delta_vs_second_best"] = self._build_strategy_param_delta(
                deepcopy(best.get("strategy_params") or {}),
                deepcopy(second.get("strategy_params") or {}),
            )
        if best and historical_best:
            delta_return = round(
                float(best.get("total_return_pct", 0) or 0) - float(historical_best.get("total_return_pct", 0) or 0),
                4,
            )
            delta_sharpe = round(
                float(best.get("sharpe", 0) or 0) - float(historical_best.get("sharpe", 0) or 0),
                4,
            )
            improved = (
                delta_return >= BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD
                or (delta_return >= 0 and delta_sharpe >= BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD)
            )
            comparison.update(
                {
                    "historical_baseline_name": historical_best.get("name") or "",
                    "historical_baseline_return_pct": float(historical_best.get("total_return_pct", 0) or 0),
                    "historical_baseline_sharpe": float(historical_best.get("sharpe", 0) or 0),
                    "delta_vs_historical_return_pct": delta_return,
                    "delta_vs_historical_sharpe": delta_sharpe,
                    "improved_vs_historical": bool(improved),
                    "strategy_param_delta_vs_historical": self._build_strategy_param_delta(
                        deepcopy(best.get("strategy_params") or {}),
                        deepcopy(historical_best.get("strategy_params") or {}),
                    ),
                    "scope_similarity": historical_best.get("scope_similarity") or {},
                }
            )

        suggestions = []
        if comparison.get("strategy_param_delta_vs_second_best"):
            keys = ", ".join(
                str(item.get("key") or "")
                for item in comparison["strategy_param_delta_vs_second_best"]
                if str(item.get("key") or "")
            )
            if keys:
                suggestions.append(f"优先复核最佳变体相对次优变体的参数差异：{keys}。")
        if comparison.get("improved_vs_historical"):
            suggestions.append("最佳变体已优于同周期历史基准，可考虑作为当前候选默认参数继续前推。")
        elif best:
            suggestions.append("本次 experiment 形成了内部最优参数，但相对历史同周期基准还未拉开明显优势。")
        if not best:
            suggestions.append("本次 experiment 没有完成的变体结果，先检查参数输入或数据范围。")

        headline = "本次参数扫描已形成最佳变体。" if best else "本次参数扫描尚未形成可用结果。"
        if comparison.get("improved_vs_historical"):
            headline = "本次参数扫描找到优于同周期历史基准的最佳变体。"

        return {
            "summary": {
                "headline": headline,
                "best_run_id": best.get("run_id") or "",
                "best_variant_label": best.get("variant_label") or "",
                "improved_vs_historical": bool(comparison.get("improved_vs_historical")),
                "delta_vs_historical_return_pct": float(comparison.get("delta_vs_historical_return_pct", 0) or 0),
                "delta_vs_second_best_return_pct": float(comparison.get("delta_vs_second_best_return_pct", 0) or 0),
            },
            "comparison": comparison,
            "suggestions": suggestions[:6],
            "notification": {
                "should_notify": bool(comparison.get("improved_vs_historical")),
                "reason": "better_same_period" if comparison.get("improved_vs_historical") else "",
            },
        }

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

    def _resolve_symbols(self, request: dict) -> list[str]:
        symbols = list(request.get("symbols") or [])
        if symbols:
            return symbols[: request["max_symbols"]]

        source = request["symbol_source"]
        if source == "targets":
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=f'environment = "{request["source_environment"]}" && status = "active"',
                sort="-date,-score",
                max_pages=20,
            )
            resolved = []
            seen = set()
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                resolved.append(symbol)
                if len(resolved) >= request["max_symbols"]:
                    break
            return resolved

        if source == "watchlist":
            rows = self.pb.get_all_records(
                "watchlist",
                filter=f'symbol_role = "{WATCHLIST_SYMBOL_ROLE_TRADE}" || symbol_role = ""',
                sort="symbol",
                max_pages=20,
            )
            resolved = []
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol or symbol in resolved:
                    continue
                resolved.append(symbol)
                if len(resolved) >= request["max_symbols"]:
                    break
            return resolved

        return []

    def _date_to_ms_range(self, date_from: str, date_to: str) -> tuple[int, int]:
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _session_edge_grace_ms(self, days: int = BACKTEST_COVERAGE_EDGE_GRACE_DAYS) -> int:
        return max(0, int(days or 0)) * 24 * 60 * 60 * 1000

    def _symbol_range_coverage_summary(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        *,
        warmup_bars: int = BACKTEST_WARMUP_BARS,
        refresh_daily_coverage: bool = False,
    ) -> dict:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        warmup_lookback_ms = interval_to_ms("5m") * max(0, int(warmup_bars or 0), BACKTEST_WARMUP_BARS)
        requested_start_ms = max(0, start_ms - warmup_lookback_ms)
        daily_summary = self._symbol_range_coverage_summary_from_daily_coverage(
            symbol,
            source_environment,
            start_ms,
            end_ms,
            refresh=refresh_daily_coverage,
        )
        if daily_summary is not None:
            return daily_summary
        sqlite_summary = self._symbol_range_coverage_summary_from_sqlite(
            symbol,
            source_environment,
            requested_start_ms,
            end_ms,
        )
        if sqlite_summary is not None:
            return sqlite_summary

        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            start_ms=requested_start_ms,
            end_ms=end_ms,
            descending=False,
        )
        first_bar_ms = int(rows[0].get("bar_time_ms", 0) or 0) if rows else 0
        last_bar_ms = int(rows[-1].get("bar_time_ms", 0) or 0) if rows else 0
        gap_count = self._count_internal_5m_gaps(rows) if rows else 0
        edge_grace_ms = self._session_edge_grace_ms()
        missing_start = not rows or first_bar_ms <= 0 or first_bar_ms > requested_start_ms + edge_grace_ms
        missing_end = not rows or last_bar_ms <= 0 or last_bar_ms < end_ms - edge_grace_ms
        needs_backfill = bool(not rows or missing_start or missing_end or gap_count > 0)
        repair_windows = self._build_backfill_repair_windows(rows, requested_start_ms, end_ms) if needs_backfill else []
        reasons = []
        if not rows:
            reasons.append("no_rows")
        if missing_start:
            reasons.append("missing_start")
        if missing_end:
            reasons.append("missing_end")
        if gap_count > 0:
            reasons.append("internal_gaps")
        return {
            "symbol": symbol,
            "needs_backfill": needs_backfill,
            "reasons": reasons,
            "row_count": len(rows),
            "gap_count": gap_count,
            "first_bar_ms": first_bar_ms,
            "last_bar_ms": last_bar_ms,
            "first_bar_us": format_us_time(first_bar_ms) if first_bar_ms > 0 else "",
            "last_bar_us": format_us_time(last_bar_ms) if last_bar_ms > 0 else "",
            "requested_start_ms": requested_start_ms,
            "requested_end_ms": end_ms,
            "requested_start_us": format_us_time(requested_start_ms) if requested_start_ms > 0 else "",
            "requested_end_us": format_us_time(end_ms) if end_ms > 0 else "",
            "repair_window_count": len(repair_windows),
            "repair_windows": repair_windows[:20],
            "repair_windows_truncated": max(0, len(repair_windows) - 20),
            "diagnostic_source": "python_rows",
        }

    def _symbol_range_coverage_summary_from_daily_coverage(
        self,
        symbol: str,
        source_environment: str,
        requested_start_ms: int,
        requested_end_ms: int,
        *,
        refresh: bool = False,
    ) -> dict | None:
        db_path = str(BACKTEST_SQLITE_PATH or "").strip()
        if not db_path or not os.path.exists(db_path):
            return None

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        date_from = market_date_from_ms(int(requested_start_ms))
        date_to = market_date_from_ms(int(requested_end_ms))
        expected_dates = trading_date_strings_from_ms(int(requested_start_ms), int(requested_end_ms))
        if not expected_dates:
            return None

        try:
            with sqlite3.connect(db_path, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                existing_rows = load_daily_coverage_rows(
                    conn,
                    symbols=[normalized_symbol],
                    environment=source_environment,
                    date_from=date_from,
                    date_to=date_to,
                    interval="5m",
                    session_mode="regular",
                )
                by_date = {str(row.get("market_date") or ""): dict(row) for row in existing_rows}
                dates_to_rebuild = []
                for market_date in expected_dates:
                    row = by_date.get(market_date)
                    status = str((row or {}).get("status") or "").strip().lower()
                    if refresh or row is None or status not in DAILY_COVERAGE_OK_STATUSES:
                        dates_to_rebuild.append(market_date)

                if dates_to_rebuild:
                    rebuilt_rows = build_range_daily_coverage(
                        conn,
                        symbols=[normalized_symbol],
                        environment=source_environment,
                        date_from=min(dates_to_rebuild),
                        date_to=max(dates_to_rebuild),
                        interval="5m",
                        session_modes=("regular",),
                        source="backtest_preflight",
                        date_filter=dates_to_rebuild,
                    )
                    if rebuilt_rows:
                        with conn:
                            upsert_bar_coverage_daily(conn, rebuilt_rows)
                        for row in rebuilt_rows:
                            by_date[str(row.get("market_date") or "")] = dict(row)

                rows = [by_date[market_date] for market_date in expected_dates if market_date in by_date]
                summary = summarize_symbol_daily_coverage(
                    symbol=normalized_symbol,
                    rows=rows,
                    requested_start_ms=int(requested_start_ms),
                    requested_end_ms=int(requested_end_ms),
                    interval="5m",
                )
                summary["daily_coverage"]["rebuilt_dates"] = dates_to_rebuild[:20]
                summary["daily_coverage"]["rebuilt_dates_truncated"] = max(0, len(dates_to_rebuild) - 20)
                return summary
        except sqlite3.OperationalError:
            return None
        except Exception:
            traceback.print_exc()
            return None

    def _symbol_range_coverage_summary_from_sqlite(
        self,
        symbol: str,
        source_environment: str,
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> dict | None:
        db_path = str(BACKTEST_SQLITE_PATH or "").strip()
        if not db_path or not os.path.exists(db_path):
            return None

        normalized_interval = "5m"
        interval_ms = interval_to_ms(normalized_interval)
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                summary = conn.execute(
                    """
                    SELECT COUNT(*) AS row_count,
                           MIN(bar_time_ms) AS first_bar_ms,
                           MAX(bar_time_ms) AS last_bar_ms,
                           SUM(CASE WHEN COALESCE(us_time, '') = '' THEN 1 ELSE 0 END) AS missing_us_time_count
                    FROM ibkr_bars
                    WHERE symbol = ?
                      AND interval = ?
                      AND environment = ?
                      AND bar_time_ms >= ?
                      AND bar_time_ms <= ?
                    """,
                    (
                        symbol,
                        normalized_interval,
                        source_environment,
                        int(requested_start_ms),
                        int(requested_end_ms),
                    ),
                ).fetchone()
                if summary is None:
                    return None

                row_count = int(summary["row_count"] or 0)
                missing_us_time_count = int(summary["missing_us_time_count"] or 0)
                if row_count > 0 and missing_us_time_count > 0:
                    return None

                first_bar_ms = int(summary["first_bar_ms"] or 0)
                last_bar_ms = int(summary["last_bar_ms"] or 0)
                raw_windows: list[dict] = []
                if row_count <= 0:
                    raw_windows.append(
                        {
                            "start_ms": int(requested_start_ms),
                            "end_ms": int(requested_end_ms),
                            "reason": "no_rows",
                        }
                    )
                    gap_count = 0
                else:
                    edge_grace_ms = self._session_edge_grace_ms()
                    if first_bar_ms > int(requested_start_ms) + edge_grace_ms:
                        raw_windows.append(
                            {
                                "start_ms": int(requested_start_ms),
                                "end_ms": max(0, first_bar_ms - interval_ms),
                                "reason": "missing_start",
                            }
                        )
                    if last_bar_ms < int(requested_end_ms) - edge_grace_ms:
                        raw_windows.append(
                            {
                                "start_ms": last_bar_ms + interval_ms,
                                "end_ms": int(requested_end_ms),
                                "reason": "missing_end",
                            }
                        )
                    gap_rows = conn.execute(
                        """
                        WITH ordered AS (
                            SELECT bar_time_ms,
                                   substr(us_time, 1, 10) AS us_day,
                                   LAG(bar_time_ms) OVER (
                                       PARTITION BY substr(us_time, 1, 10)
                                       ORDER BY bar_time_ms
                                   ) AS prev_ms
                            FROM ibkr_bars
                            WHERE symbol = ?
                              AND interval = ?
                              AND environment = ?
                              AND bar_time_ms >= ?
                              AND bar_time_ms <= ?
                        )
                        SELECT prev_ms + ? AS start_ms,
                               bar_time_ms - ? AS end_ms,
                               'internal_gap' AS reason
                        FROM ordered
                        WHERE prev_ms IS NOT NULL
                          AND us_day != ''
                          AND bar_time_ms - prev_ms > ?
                        ORDER BY start_ms
                        LIMIT 500
                        """,
                        (
                            symbol,
                            normalized_interval,
                            source_environment,
                            int(requested_start_ms),
                            int(requested_end_ms),
                            interval_ms,
                            interval_ms,
                            interval_ms * 3,
                        ),
                    ).fetchall()
                    gap_count = len(gap_rows)
                    raw_windows.extend(dict(row) for row in gap_rows)
        except Exception:
            traceback.print_exc()
            return None

        repair_windows = self._merge_backfill_repair_windows(raw_windows)
        missing_start = any("missing_start" in str(item.get("reason") or "") for item in repair_windows)
        missing_end = any("missing_end" in str(item.get("reason") or "") for item in repair_windows)
        needs_backfill = bool(row_count <= 0 or missing_start or missing_end or gap_count > 0)
        reasons = []
        if row_count <= 0:
            reasons.append("no_rows")
        if missing_start:
            reasons.append("missing_start")
        if missing_end:
            reasons.append("missing_end")
        if gap_count > 0:
            reasons.append("internal_gaps")
        return {
            "symbol": symbol,
            "needs_backfill": needs_backfill,
            "reasons": reasons,
            "row_count": row_count,
            "gap_count": gap_count,
            "first_bar_ms": first_bar_ms,
            "last_bar_ms": last_bar_ms,
            "first_bar_us": format_us_time(first_bar_ms) if first_bar_ms > 0 else "",
            "last_bar_us": format_us_time(last_bar_ms) if last_bar_ms > 0 else "",
            "requested_start_ms": int(requested_start_ms),
            "requested_end_ms": int(requested_end_ms),
            "requested_start_us": format_us_time(int(requested_start_ms)) if int(requested_start_ms) > 0 else "",
            "requested_end_us": format_us_time(int(requested_end_ms)) if int(requested_end_ms) > 0 else "",
            "repair_window_count": len(repair_windows),
            "repair_windows": repair_windows[:20],
            "repair_windows_truncated": max(0, len(repair_windows) - 20),
            "diagnostic_source": "sqlite_window",
        }

    def _preflight_backfill_symbols(
        self,
        symbols: list[str],
        request: dict,
        progress_context: dict | None = None,
    ) -> dict:
        if not bool(request.get("preflight_backfill", True)):
            return {
                "enabled": False,
                "symbols": list(symbols or []),
                "needed_symbols": [],
                "initial": [],
                "results": {},
                "final": [],
            }
        if not symbols:
            return {"enabled": True, "symbols": [], "needed_symbols": [], "initial": [], "results": {}, "final": []}

        self._set_progress_context("running", "preflight", "checking bar coverage", 4, progress_context)
        initial = [
            self._symbol_range_coverage_summary(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                warmup_bars=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            for symbol in symbols
        ]
        needed_symbols = [item["symbol"] for item in initial if item.get("needs_backfill")]
        if not needed_symbols:
            return {
                "enabled": True,
                "symbols": list(symbols),
                "needed_symbols": [],
                "initial": initial,
                "results": {},
                "final": initial,
            }

        start_ms, end_ms = self._date_to_ms_range(request["date_from"], request["date_to"])
        warmup_lookback_ms = interval_to_ms("5m") * (
            max(
                BACKTEST_WARMUP_BARS,
                int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            + 20
        )
        backfill_start_ms = max(0, start_ms - warmup_lookback_ms)
        max_workers = min(
            max(1, int(request.get("backfill_concurrency", DEFAULT_BACKTEST_BACKFILL_CONCURRENCY) or DEFAULT_BACKTEST_BACKFILL_CONCURRENCY)),
            MAX_BACKTEST_BACKFILL_CONCURRENCY,
            len(needed_symbols),
        )
        symbol_timeout_s = min(
            MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
            max(
                30,
                int(
                    request.get("backfill_symbol_timeout_s", DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)
                    or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS
                ),
            ),
        )
        max_batches = min(
            MAX_BACKTEST_BACKFILL_MAX_BATCHES,
            max(
                1,
                int(request.get("backfill_max_batches", DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES) or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES),
            ),
        )
        history_timeout_s = min(
            MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
            max(
                5,
                int(
                    request.get("backfill_history_timeout_s", DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)
                    or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS
                ),
            ),
        )
        history_max_retries = min(
            MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
            max(
                0,
                int(
                    request.get("backfill_history_max_retries")
                    if request.get("backfill_history_max_retries") is not None
                    else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
                ),
            ),
        )
        self._set_progress_context(
            "running",
            "preflight_backfill",
            f"backfilling {len(needed_symbols)} symbols with {max_workers} workers",
            5,
            progress_context,
        )

        results: dict[str, dict] = {}
        completed = 0
        repair_windows_by_symbol = {
            str(item.get("symbol") or "").upper(): list(item.get("repair_windows") or [])
            for item in initial
            if item.get("needs_backfill")
        }

        def run_symbol(symbol: str) -> dict:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            windows = repair_windows_by_symbol.get(symbol) or [
                {
                    "start_ms": backfill_start_ms,
                    "end_ms": end_ms,
                    "reason": "legacy_full_range",
                }
            ]
            repair_rows = []
            repair_results = []
            remaining_batches = max_batches
            window_started_at = time.monotonic()
            for window in windows:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                if remaining_batches <= 0:
                    repair_results.append(
                        {
                            "ok": bool(repair_rows),
                            "reason": "max_batches_reached",
                            "fetched_rows": 0,
                            "batches": 0,
                            "window": window,
                        }
                    )
                    break
                elapsed_s = time.monotonic() - window_started_at
                remaining_elapsed_s = max(30, int(symbol_timeout_s - elapsed_s))
                if elapsed_s >= symbol_timeout_s:
                    repair_results.append(
                        {
                            "ok": bool(repair_rows),
                            "reason": "symbol_timeout",
                            "fetched_rows": 0,
                            "batches": 0,
                            "window": window,
                        }
                    )
                    break
                repair = self._backfill_symbol_history(
                    symbol,
                    request["source_environment"],
                    int(window.get("start_ms", backfill_start_ms) or backfill_start_ms),
                    int(window.get("end_ms", end_ms) or end_ms),
                    interval="5m",
                    max_elapsed_s=remaining_elapsed_s,
                    max_batches=remaining_batches,
                    history_timeout_s=history_timeout_s,
                    history_max_retries=history_max_retries,
                    cancel_check=self._cancel_event.is_set,
                )
                repair_result = {
                    key: value
                    for key, value in (repair or {}).items()
                    if key not in {"rows"}
                }
                repair_result["window"] = window
                repair_results.append(repair_result)
                repair_rows.extend(list((repair or {}).get("rows") or []))
                remaining_batches -= int((repair or {}).get("batches", 0) or 0)
                if str((repair or {}).get("reason") or "") in {"symbol_timeout", "cancelled", "partial_cancelled", "max_batches_reached"}:
                    break

            repair_rows = self._dedupe_backfill_rows(repair_rows)
            persisted_rows = self._persist_backfill_rows(repair_rows) if repair_rows else 0
            rolled_rows = self._rollup_symbol_history(symbol, request["source_environment"]) if persisted_rows > 0 else 0
            ok = bool(repair_rows) and persisted_rows > 0
            if not repair_rows and all(str(item.get("reason") or "") == "no_rows_fetched" for item in repair_results):
                ok = False
            reason = "ok" if ok else str((repair_results[-1] if repair_results else {}).get("reason") or "no_rows_fetched")
            return {
                "symbol": symbol,
                "ok": ok,
                "reason": reason,
                "fetched_rows": len(repair_rows),
                "batches": int(sum(int(item.get("batches", 0) or 0) for item in repair_results)),
                "elapsed_s": round(time.monotonic() - window_started_at, 3),
                "persisted_rows": int(persisted_rows or 0),
                "rolled_rows": int(rolled_rows or 0),
                "repair_window_count": len(windows),
                "repair_windows": windows[:20],
                "repair_results": repair_results[:20],
            }

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ibkr-backtest-preflight") as executor:
            pending = {executor.submit(run_symbol, symbol): symbol for symbol in needed_symbols}
            while pending:
                if self._cancel_event.is_set():
                    for pending_future in pending:
                        pending_future.cancel()
                    raise BacktestCancelled()
                done, pending_futures = wait(
                    pending,
                    timeout=BACKTEST_PREFLIGHT_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    inflight_symbols = [pending[future] for future in pending]
                    progress_value = 5 + int((completed / max(1, len(needed_symbols))) * 10)
                    inflight_text = ",".join(inflight_symbols[:6]) or "--"
                    if len(inflight_symbols) > 6:
                        inflight_text += f"+{len(inflight_symbols) - 6}"
                    self._set_progress_context(
                        "running",
                        "preflight_backfill",
                        f"backfilled {completed}/{len(needed_symbols)} symbols; inflight {inflight_text}",
                        progress_value,
                        progress_context,
                    )
                    continue

                symbol_by_future = dict(pending)
                pending = {future: symbol_by_future[future] for future in pending_futures}
                for future in done:
                    symbol = symbol_by_future.get(future, "")
                    try:
                        results[symbol] = future.result()
                    except BacktestCancelled:
                        for pending_future in pending:
                            pending_future.cancel()
                        raise
                    except Exception as exc:
                        traceback.print_exc()
                        results[symbol] = {"symbol": symbol, "ok": False, "error": str(exc)[:500]}
                    completed += 1
                    progress_value = 5 + int((completed / max(1, len(needed_symbols))) * 10)
                    self._set_progress_context(
                        "running",
                        "preflight_backfill",
                        f"backfilled {completed}/{len(needed_symbols)} symbols",
                        progress_value,
                        progress_context,
                    )

        self._set_progress_context("running", "preflight", "rechecking bar coverage", 15, progress_context)
        final = [
            self._symbol_range_coverage_summary(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                warmup_bars=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            for symbol in symbols
        ]
        return {
            "enabled": True,
            "symbols": list(symbols),
            "needed_symbols": needed_symbols,
            "initial": initial,
            "results": results,
            "final": final,
            "backfill_start_us": format_us_time(backfill_start_ms) if backfill_start_ms > 0 else "",
            "backfill_end_us": format_us_time(end_ms) if end_ms > 0 else "",
            "concurrency": max_workers,
            "symbol_timeout_s": symbol_timeout_s,
            "max_batches": max_batches,
            "history_timeout_s": history_timeout_s,
            "history_max_retries": history_max_retries,
        }

    def _format_backfill_start_time(self, anchor_ms: int) -> str:
        return ms_to_et(anchor_ms).strftime("%Y%m%d-%H:%M:%S")

    def _backfill_symbol_history(
        self,
        symbol: str,
        source_environment: str,
        start_ms: int,
        end_ms: int,
        interval: str = "5m",
        *,
        max_elapsed_s: int | None = None,
        max_batches: int | None = None,
        history_timeout_s: int | None = None,
        history_max_retries: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        if (
            not self.pb
            or self.data_backfill is None
            or self.conid_resolver is None
            or start_ms <= 0
            or end_ms <= 0
            or end_ms < start_ms
        ):
            return {"ok": False, "reason": "backfill_unavailable"}

        normalized_interval = normalize_interval(interval)
        if normalized_interval != "5m":
            return {"ok": False, "reason": "unsupported_interval"}

        try:
            conid = int(self.conid_resolver.resolve(symbol) or 0)
        except Exception:
            conid = 0
        if conid <= 0:
            return {"ok": False, "reason": "conid_unresolved"}

        interval_ms = interval_to_ms(normalized_interval)
        period = "4d"
        bar_size = "5min"
        anchor_ms = int(end_ms)
        earliest_needed_ms = int(start_ms)
        batches = 0
        fetched_rows = []
        seen_bar_ms = set()
        started_at = time.monotonic()
        batch_limit = min(
            MAX_BACKTEST_BACKFILL_MAX_BATCHES,
            max(1, int(max_batches or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES)),
        )
        elapsed_limit_s = min(
            MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
            max(30, int(max_elapsed_s or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)),
        )
        request_timeout_s = min(
            MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
            max(5, int(history_timeout_s or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)),
        )
        request_max_retries = min(
            MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
            max(
                0,
                int(
                    history_max_retries
                    if history_max_retries is not None
                    else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
                ),
            ),
        )

        stop_reason = ""
        while anchor_ms >= earliest_needed_ms and batches < batch_limit:
            if cancel_check and cancel_check():
                fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                return {
                    "ok": bool(fetched_rows),
                    "reason": "cancelled" if not fetched_rows else "partial_cancelled",
                    "fetched_rows": len(fetched_rows),
                    "batches": batches,
                    "rows": fetched_rows,
                    "elapsed_s": round(time.monotonic() - started_at, 3),
                }
            if time.monotonic() - started_at >= elapsed_limit_s:
                stop_reason = "symbol_timeout"
                break
            start_time = self._format_backfill_start_time(anchor_ms)
            try:
                payload = self.data_backfill._request_history_json(
                    conid,
                    symbol,
                    normalized_interval,
                    period,
                    bar_size,
                    start_time=start_time,
                    timeout=request_timeout_s,
                    max_retries=request_max_retries,
                )
            except Exception as exc:
                fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                return {
                    "ok": bool(fetched_rows),
                    "reason": "partial_history_fetch_failed" if fetched_rows else "history_fetch_failed",
                    "error": str(exc)[:1000],
                    "fetched_rows": len(fetched_rows),
                    "batches": batches,
                    "rows": fetched_rows,
                    "elapsed_s": round(time.monotonic() - started_at, 3),
                }
            bars = list(payload.get("data") or [])
            if not bars:
                break

            batches += 1
            oldest_batch_ms = 0
            for bar in bars:
                raw_bar_time = int(bar.get("t", 0) or 0)
                bar_time_ms = raw_bar_time if raw_bar_time > 1_000_000_000_000 else raw_bar_time * 1000
                if bar_time_ms <= 0:
                    continue
                if oldest_batch_ms <= 0 or bar_time_ms < oldest_batch_ms:
                    oldest_batch_ms = bar_time_ms
                if bar_time_ms in seen_bar_ms:
                    continue
                seen_bar_ms.add(bar_time_ms)
                if bar_time_ms < earliest_needed_ms or bar_time_ms > end_ms:
                    continue
                fetched_rows.append({
                    "symbol": symbol,
                    "environment": source_environment,
                    "exchange": "",
                    "interval": normalized_interval,
                    "open": float(bar.get("o", 0) or 0),
                    "high": float(bar.get("h", 0) or 0),
                    "low": float(bar.get("l", 0) or 0),
                    "close": float(bar.get("c", 0) or 0),
                    "volume": float(bar.get("v", 0) or 0),
                    "bar_time_ms": bar_time_ms,
                    "us_time": format_us_time(bar_time_ms),
                    "cn_time": format_cn_time(bar_time_ms),
                    "session_type": classify_session(bar_time_ms=bar_time_ms),
                    "source": "backfill",
                    "extra": {
                        "source": "ibkr_history_backfill",
                        "canonical": True,
                        "conid": conid,
                        "interval": normalized_interval,
                        "outside_rth": True,
                        "request_period": period,
                        "request_bar": bar_size,
                        "request_start_time": start_time,
                        "backfill_scope": "backtest_range",
                        **build_runtime_timestamps(),
                    },
                })

            if oldest_batch_ms <= 0 or oldest_batch_ms <= earliest_needed_ms:
                break
            next_anchor_ms = oldest_batch_ms - interval_ms
            if next_anchor_ms >= anchor_ms:
                break
            anchor_ms = next_anchor_ms

        if not stop_reason and anchor_ms >= earliest_needed_ms and batches >= batch_limit:
            stop_reason = "max_batches_reached"

        fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
        return {
            "ok": bool(fetched_rows),
            "reason": stop_reason or ("ok" if fetched_rows else "no_rows_fetched"),
            "fetched_rows": len(fetched_rows),
            "batches": batches,
            "rows": fetched_rows,
            "elapsed_s": round(time.monotonic() - started_at, 3),
        }

    def _merge_backfill_rows(self, rows: list[dict], fetched_rows: list[dict]) -> list[dict]:
        merged = {}
        for row in rows or []:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms > 0:
                merged[bar_ms] = row
        for row in fetched_rows or []:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms > 0 and bar_ms not in merged:
                merged[bar_ms] = row
        return [merged[key] for key in sorted(merged)]

    def _dedupe_backfill_rows(self, rows: list[dict]) -> list[dict]:
        merged = {}
        for row in rows or []:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_ms > 0:
                merged[bar_ms] = row
        return [merged[key] for key in sorted(merged)]

    def _persist_backfill_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        try:
            with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
                with conn:
                    return upsert_bars(conn, rows)
        except Exception:
            traceback.print_exc()

        if not self.pb:
            return 0

        written = 0
        for index in range(0, len(rows), 80):
            chunk = rows[index : index + 80]
            try:
                result = self.pb.upsert_bars(chunk)
            except Exception:
                traceback.print_exc()
                continue
            if result.get("ok", False):
                written += int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
        return written

    def _count_internal_5m_gaps(self, rows: list[dict]) -> int:
        gap_count = 0
        previous_ms = 0
        previous_day = ""
        for row in rows or []:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_ms <= 0:
                continue
            current_day = str((row or {}).get("us_time", "") or "")[:10] or ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if previous_ms > 0 and previous_day == current_day:
                if bar_ms - previous_ms > interval_to_ms("5m") * 3:
                    gap_count += 1
            previous_ms = bar_ms
            previous_day = current_day
        return gap_count

    def _merge_backfill_repair_windows(self, raw_windows: list[dict]) -> list[dict]:
        interval_ms = interval_to_ms("5m")
        windows: list[dict] = []
        for window in sorted(raw_windows or [], key=lambda item: (int(item.get("start_ms", 0) or 0), int(item.get("end_ms", 0) or 0))):
            start_ms = max(0, int(window.get("start_ms", 0) or 0))
            end_ms = int(window.get("end_ms", 0) or 0)
            if start_ms <= 0 or end_ms <= 0 or end_ms < start_ms:
                continue
            if windows and start_ms <= int(windows[-1]["end_ms"]) + interval_ms:
                windows[-1]["end_ms"] = max(int(windows[-1]["end_ms"]), end_ms)
                reasons = set(str(windows[-1].get("reason") or "").split(","))
                reasons.add(str(window.get("reason") or ""))
                windows[-1]["reason"] = ",".join(sorted(item for item in reasons if item))
                continue
            windows.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason": str(window.get("reason") or "missing"),
                }
            )
        for window in windows:
            window["start_us"] = format_us_time(int(window["start_ms"]))
            window["end_us"] = format_us_time(int(window["end_ms"]))
        return windows

    def _build_backfill_repair_windows(
        self,
        rows: list[dict],
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> list[dict]:
        interval_ms = interval_to_ms("5m")
        edge_grace_ms = self._session_edge_grace_ms()
        raw_windows: list[dict] = []

        def add_window(start_ms: int, end_ms: int, reason: str):
            start_ms = max(0, int(start_ms or 0))
            end_ms = int(end_ms or 0)
            if start_ms <= 0 or end_ms <= 0 or end_ms < start_ms:
                return
            raw_windows.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason": reason,
                }
            )

        sorted_rows = sorted(
            [row for row in rows or [] if int((row or {}).get("bar_time_ms", 0) or 0) > 0],
            key=lambda item: int(item.get("bar_time_ms", 0) or 0),
        )
        if not sorted_rows:
            add_window(requested_start_ms, requested_end_ms, "no_rows")
            return self._merge_backfill_repair_windows(raw_windows)

        first_bar_ms = int(sorted_rows[0].get("bar_time_ms", 0) or 0)
        last_bar_ms = int(sorted_rows[-1].get("bar_time_ms", 0) or 0)
        if first_bar_ms > requested_start_ms + edge_grace_ms:
            add_window(requested_start_ms, first_bar_ms - interval_ms, "missing_start")
        if last_bar_ms < requested_end_ms - edge_grace_ms:
            add_window(last_bar_ms + interval_ms, requested_end_ms, "missing_end")

        previous_ms = 0
        previous_day = ""
        for row in sorted_rows:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            current_day = str((row or {}).get("us_time", "") or "")[:10] or ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if previous_ms > 0 and previous_day == current_day and bar_ms - previous_ms > interval_ms * 3:
                add_window(previous_ms + interval_ms, bar_ms - interval_ms, "internal_gap")
            previous_ms = bar_ms
            previous_day = current_day

        return self._merge_backfill_repair_windows(raw_windows)

    def _rollup_symbol_history(self, symbol: str, source_environment: str) -> int:
        try:
            with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
                rows = conn.execute(
                    """
                    SELECT symbol, exchange, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms
                    FROM ibkr_bars
                    WHERE symbol = ? AND interval = '5m' AND environment = ?
                    ORDER BY bar_time_ms ASC
                    """,
                    (symbol, source_environment),
                ).fetchall()
                if not rows:
                    return 0

                builder = TimeframeBarBuilder(target_intervals=HIGHER_INTERVALS)
                pending = []
                written = 0
                for row in rows:
                    base_bar = {
                        "symbol": str(row["symbol"] or "").upper(),
                        "exchange": str(row["exchange"] or "").upper(),
                        "environment": source_environment,
                        "interval": "5m",
                        "open": float(row["open"] or 0),
                        "high": float(row["high"] or 0),
                        "low": float(row["low"] or 0),
                        "close": float(row["close"] or 0),
                        "volume": float(row["volume"] or 0),
                        "session_type": str(row["session_type"] or ""),
                        "us_time": str(row["us_time"] or ""),
                        "cn_time": str(row["cn_time"] or ""),
                        "bar_time_ms": int(row["bar_time_ms"] or 0),
                    }
                    for derived in builder.consume(base_bar):
                        derived_extra = dict(derived.get("extra") or {})
                        derived_extra["canonical"] = True
                        derived["extra"] = derived_extra
                        pending.append(derived)
                        if len(pending) >= 400:
                            with conn:
                                written += upsert_bars(conn, pending)
                            pending = []
                if pending:
                    with conn:
                        written += upsert_bars(conn, pending)
                return written
        except Exception:
            traceback.print_exc()
            return 0

    def _ensure_scan_history_available(self, symbol: str, request: dict, cutoff_ms: int) -> dict:
        environment = request["source_environment"]
        lookback_limit = int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
        coverage_bars = max(BACKTEST_WARMUP_BARS + 20, lookback_limit + 20)
        coverage_start_ms = max(0, cutoff_ms - (interval_to_ms("5m") * coverage_bars))
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            environment,
            start_ms=coverage_start_ms,
            end_ms=cutoff_ms,
            descending=False,
        )
        need_backfill = (
            not rows
            or int(rows[0].get("bar_time_ms", 0) or 0) > coverage_start_ms
            or int(rows[-1].get("bar_time_ms", 0) or 0) < max(0, cutoff_ms - interval_to_ms("5m"))
            or self._count_internal_5m_gaps(rows) > 0
        )
        if not need_backfill:
            return {"ok": True, "needed": False, "persisted_rows": 0, "rolled_rows": 0}

        repair = self._backfill_symbol_history(
            symbol,
            environment,
            coverage_start_ms,
            cutoff_ms,
            interval="5m",
        )
        repair_rows = list((repair or {}).get("rows") or [])
        if not repair_rows:
            return {"ok": False, "needed": True, "persisted_rows": 0, "rolled_rows": 0}

        persisted_rows = self._persist_backfill_rows(repair_rows)
        rolled_rows = self._rollup_symbol_history(symbol, environment) if persisted_rows > 0 else 0
        return {
            "ok": persisted_rows > 0,
            "needed": True,
            "persisted_rows": persisted_rows,
            "rolled_rows": rolled_rows,
        }

    def _load_bar_rows_from_sqlite(
        self,
        symbol: str,
        source_environment: str,
        *,
        interval: str = "5m",
        start_ms: int | None = None,
        end_ms: int | None = None,
        before_bar_time_ms: int | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        db_path = str(BACKTEST_SQLITE_PATH or "").strip()
        if not db_path or not os.path.exists(db_path):
            return []

        conditions = [
            "symbol = ?",
            "interval = ?",
            "environment = ?",
        ]
        params: list[Any] = [symbol, normalize_interval(interval), source_environment]
        if start_ms is not None:
            conditions.append("bar_time_ms >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            conditions.append("bar_time_ms <= ?")
            params.append(int(end_ms))
        if before_bar_time_ms is not None:
            conditions.append("bar_time_ms < ?")
            params.append(int(before_bar_time_ms))

        sql = (
            "SELECT symbol, exchange, interval, open, high, low, close, volume, "
            "session_type, us_time, cn_time, bar_time_ms "
            "FROM ibkr_bars "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY bar_time_ms {'DESC' if descending else 'ASC'}"
        )
        if limit is not None and int(limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params).fetchall()
        except Exception:
            traceback.print_exc()
            return []

        normalized = []
        for row in rows or []:
            normalized.append(
                {
                    "symbol": str(row["symbol"] or ""),
                    "exchange": str(row["exchange"] or ""),
                    "interval": str(row["interval"] or "5m"),
                    "open": float(row["open"] or 0),
                    "high": float(row["high"] or 0),
                    "low": float(row["low"] or 0),
                    "close": float(row["close"] or 0),
                    "volume": float(row["volume"] or 0),
                    "session_type": str(row["session_type"] or ""),
                    "us_time": str(row["us_time"] or ""),
                    "cn_time": str(row["cn_time"] or ""),
                    "bar_time_ms": int(row["bar_time_ms"] or 0),
                }
            )
        return normalized

    def _load_symbol_bars(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        session_mode: str,
        *,
        allow_backfill: bool = True,
    ) -> list[dict]:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            start_ms=start_ms,
            end_ms=end_ms,
            descending=False,
        )
        if not rows and self.pb:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=DEFAULT_MAX_PAGES,
            )
        if rows:
            first_bar_ms = int(rows[0].get("bar_time_ms", 0) or 0)
            last_bar_ms = int(rows[-1].get("bar_time_ms", 0) or 0)
        else:
            first_bar_ms = 0
            last_bar_ms = 0
        if allow_backfill and (not rows or first_bar_ms > start_ms or last_bar_ms < end_ms - self._session_edge_grace_ms()):
            warmup_lookback_ms = interval_to_ms("5m") * (BACKTEST_WARMUP_BARS + 20)
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                max(0, start_ms - warmup_lookback_ms),
                end_ms,
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
        if allow_backfill and rows and self._count_internal_5m_gaps(rows) > 0:
            warmup_lookback_ms = interval_to_ms("5m") * (BACKTEST_WARMUP_BARS + 20)
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                max(0, start_ms - warmup_lookback_ms),
                end_ms,
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and session_type != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": "5m",
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized

    def _load_symbol_warmup_bars(
        self,
        symbol: str,
        source_environment: str,
        before_bar_time_ms: int,
        session_mode: str,
        limit: int = BACKTEST_WARMUP_BARS,
    ) -> list[dict]:
        if not self.pb or before_bar_time_ms <= 0 or limit <= 0:
            return []
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            before_bar_time_ms=before_bar_time_ms,
            descending=True,
            limit=max(limit + 20, BACKTEST_WARMUP_BARS + 20),
        )
        if not rows:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                    f"&& bar_time_ms < {before_bar_time_ms}"
                ),
                sort="-bar_time_ms",
                max_pages=max(4, min(20, math.ceil(limit / 200) + 2)),
            )
        if len(rows) < limit:
            warmup_start_ms = max(0, before_bar_time_ms - (interval_to_ms("5m") * max(limit + 20, BACKTEST_WARMUP_BARS + 20)))
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                warmup_start_ms,
                max(0, before_bar_time_ms - interval_to_ms("5m")),
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
            rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0), reverse=True)
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and session_type != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": "5m",
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
            if len(normalized) >= limit:
                break
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized

    def _parse_pb_datetime(self, raw_value: Any) -> Optional[datetime]:
        text = str(raw_value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ET)

    def _load_scan_universe(self, request: dict, as_of_date: str = "") -> list[dict]:
        requested_symbols = list(request.get("symbols") or [])
        if requested_symbols:
            return [{"symbol": symbol, "exchange": "", "environment": request.get("source_environment", "live")} for symbol in requested_symbols]

        source_environment = str(request.get("source_environment") or "live").strip().lower() or "live"
        rows = self.pb.get_all_records(
            "watchlist",
            filter=f'symbol_role = "{WATCHLIST_SYMBOL_ROLE_TRADE}" || symbol_role = ""',
            sort="symbol",
            max_pages=20,
        )
        merged = {}
        priority = {"": 0, "global": 1, source_environment: 2}
        applied = {}
        as_of_end = None
        if as_of_date:
            as_of_end = datetime.strptime(as_of_date, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1) - timedelta(milliseconds=1)
        for row in rows:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if not symbol:
                continue
            env = str(row.get("environment", "") or "").strip().lower()
            rank = priority.get(env, -1)
            if rank < 0:
                continue
            if as_of_end is not None:
                created_at = self._parse_pb_datetime(row.get("created")) or self._parse_pb_datetime(row.get("created_us"))
                if created_at and created_at > as_of_end:
                    continue
            if symbol in applied and applied[symbol] > rank:
                continue
            applied[symbol] = rank
            merged[symbol] = row
        return list(merged.values())

    def _load_trading_dates(self, request: dict) -> list[str]:
        start_ms, end_ms = self._date_to_ms_range(request["date_from"], request["date_to"])
        benchmark_symbol = str(request.get("benchmark_symbol") or "").strip().upper() or "SPY"
        dates = []
        seen = set()
        for interval, max_pages in (("1d", 24), ("5m", 240)):
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{benchmark_symbol}" && interval = "{interval}" && environment = "{request["source_environment"]}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=max_pages,
            )
            for row in rows:
                bar_ms = int(row.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                date_text = ms_to_et(bar_ms).strftime("%Y-%m-%d")
                if date_text in seen:
                    continue
                seen.add(date_text)
                dates.append(date_text)
        if dates:
            dates.sort()
            return dates

        start = datetime.strptime(request["date_from"], "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(request["date_to"], "%Y-%m-%d").replace(tzinfo=ET)
        fallback = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                fallback.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        return fallback

    def _build_scan_cutoff_ms(self, date_text: str, cutoff_time: str) -> int:
        hour_text, minute_text = str(cutoff_time or DEFAULT_SCAN_CUTOFF_TIME).split(":", 1)
        cutoff_dt = datetime.strptime(date_text, "%Y-%m-%d").replace(
            tzinfo=ET,
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        return int(cutoff_dt.timestamp() * 1000)

    def _load_interval_bars_before(
        self,
        symbol: str,
        source_environment: str,
        interval: str,
        end_bar_time_ms: int,
        session_mode: str,
        limit: int,
    ) -> list[dict]:
        normalized_interval = normalize_interval(interval)
        if end_bar_time_ms <= 0 or limit <= 0:
            return []
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval=normalized_interval,
            end_ms=end_bar_time_ms,
            descending=True,
            limit=max(limit + 20, limit),
        )
        if not rows and self.pb:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "{normalized_interval}" && environment = "{source_environment}" '
                    f"&& bar_time_ms <= {end_bar_time_ms}"
                ),
                sort="-bar_time_ms",
                max_pages=max(4, min(20, math.ceil(limit / 200) + 2)),
            )
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and normalized_interval != "1d" and session_type != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": normalized_interval,
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
            if len(normalized) >= limit:
                break
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized

    def _build_historical_scan_metric_row(
        self,
        symbol: str,
        trade_date: str,
        request: dict,
        cutoff_ms: int,
        engines: dict | None = None,
    ) -> dict:
        source_environment = str(request.get("source_environment") or "live").strip().lower() or "live"
        day_start_ms = int(datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=ET).timestamp() * 1000)
        intraday_rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval="5m",
            start_ms=day_start_ms,
            end_ms=cutoff_ms,
            descending=False,
        )
        if not intraday_rows and not os.path.exists(str(BACKTEST_SQLITE_PATH or "")):
            intraday_rows = [
                row
                for row in self._load_interval_bars_before(
                    symbol,
                    source_environment,
                    "5m",
                    cutoff_ms,
                    "extended",
                    240,
                )
                if ms_to_et(int(row.get("bar_time_ms", 0) or 0)).strftime("%Y-%m-%d") == trade_date
            ]

        premarket_volume = 0.0
        latest_close = 0.0
        exchange = ""
        for row in intraday_rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms > cutoff_ms:
                continue
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms)).strip().lower()
            if session_type == "premarket":
                premarket_volume += float(row.get("volume", 0) or 0)
            latest_close = float(row.get("close", 0) or latest_close or 0)
            if not exchange:
                exchange = str(row.get("exchange", "") or "").strip().upper()

        daily_rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval="1d",
            end_ms=max(0, day_start_ms - 1),
            descending=True,
            limit=16,
        )
        daily_rows = [
            row
            for row in daily_rows
            if ms_to_et(int(row.get("bar_time_ms", 0) or 0)).strftime("%Y-%m-%d") < trade_date
        ]
        daily_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
        if not daily_rows:
            lookback_start_ms = max(0, day_start_ms - interval_to_ms("1d") * 20)
            prior_5m = self._load_bar_rows_from_sqlite(
                symbol,
                source_environment,
                interval="5m",
                start_ms=lookback_start_ms,
                end_ms=max(0, day_start_ms - 1),
                descending=False,
            )
            grouped: dict[str, dict] = {}
            for row in prior_5m:
                bar_ms = int(row.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                date_key = ms_to_et(bar_ms).strftime("%Y-%m-%d")
                bucket = grouped.setdefault(date_key, {"bar_time_ms": bar_ms, "close": 0.0, "volume": 0.0})
                bucket["bar_time_ms"] = bar_ms
                bucket["close"] = float(row.get("close", 0) or bucket.get("close", 0) or 0)
                bucket["volume"] = float(bucket.get("volume", 0) or 0) + float(row.get("volume", 0) or 0)
            daily_rows = [grouped[key] for key in sorted(grouped.keys())][-16:]

        last_10_daily = daily_rows[-10:]
        avg_10d_volume = (
            sum(float(row.get("volume", 0) or 0) for row in last_10_daily) / len(last_10_daily)
            if last_10_daily
            else 0.0
        )
        prev_close = float(daily_rows[-1].get("close", 0) or 0) if daily_rows else 0.0
        if latest_close <= 0:
            latest_close = prev_close
        day_change_pct = ((latest_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

        atr_pct = 0.0
        engine = (engines or {}).get((source_environment, symbol, "5m"))
        if engine is not None and engine.is_ready():
            snapshot = engine.get_snapshot() or {}
            atr_pct = abs(float(snapshot.get("atr_pct", 0) or 0))
            if atr_pct <= 0 and latest_close > 0:
                atr_pct = abs(float(snapshot.get("atr", 0) or 0)) / latest_close * 100.0
        else:
            atr_pct = self._load_historical_scan_atr_pct(symbol, source_environment, cutoff_ms, latest_close)

        return {
            "symbol": symbol,
            "exchange": exchange,
            "price": round(float(latest_close or 0), 4),
            "avg_10d_volume": round(float(avg_10d_volume or 0), 2),
            "premarket_volume": round(float(premarket_volume or 0), 2),
            "atr_pct": round(float(atr_pct or 0), 4),
            "day_change_pct": round(float(day_change_pct or 0), 4),
            "latest_bar_time_ms": int(intraday_rows[-1].get("bar_time_ms", 0) or 0) if intraday_rows else 0,
        }

    def _load_historical_scan_atr_pct(
        self,
        symbol: str,
        source_environment: str,
        cutoff_ms: int,
        latest_close: float = 0.0,
    ) -> float:
        db_path = str(BACKTEST_SQLITE_PATH or "").strip()
        if not db_path or not os.path.exists(db_path) or cutoff_ms <= 0:
            return 0.0
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT extra
                    FROM ibkr_indicators
                    WHERE symbol = ?
                      AND interval = ?
                      AND environment = ?
                      AND bar_time_ms <= ?
                    ORDER BY bar_time_ms DESC
                    LIMIT 1
                    """,
                    (symbol, interval_to_chart_tf("5m"), source_environment, int(cutoff_ms)),
                ).fetchone()
        except Exception:
            return 0.0
        if not row:
            return 0.0
        extra = self._parse_object(row["extra"])
        atr_pct = abs(float(extra.get("atr_pct", 0) or 0))
        if atr_pct <= 0 and latest_close > 0:
            atr_pct = abs(float(extra.get("atr", 0) or 0)) / latest_close * 100.0
        return round(float(atr_pct or 0), 4)

    def _build_historical_scan_metric_rejections(
        self,
        symbol: str,
        metric_row: dict,
        settings: dict,
        *,
        allow_unknown_atr: bool = False,
    ) -> list[dict]:
        examples = []
        checks = (
            ("avg_10d_volume", "min_avg_10d_volume", "avg_10d_volume_below_threshold", "10 日均量不足"),
            ("premarket_volume", "min_premarket_volume", "premarket_volume_below_threshold", "盘前量不足"),
            ("day_change_pct", "min_abs_day_change_pct", "day_change_below_threshold", "日内涨跌幅不足"),
        )
        for metric_key, threshold_key, bucket, note in checks:
            actual = abs(float(metric_row.get(metric_key, 0) or 0)) if metric_key == "day_change_pct" else float(metric_row.get(metric_key, 0) or 0)
            threshold = float(settings.get(threshold_key, 0) or 0)
            if actual < threshold:
                examples.append(
                    {
                        "bucket": bucket,
                        "symbol": symbol,
                        "actual": round(actual, 4),
                        "threshold": round(threshold, 4),
                        "note": note,
                    }
                )
        atr_pct = abs(float(metric_row.get("atr_pct", 0) or 0))
        atr_threshold = float(settings.get("min_atr_pct", 0) or 0)
        if atr_pct < atr_threshold and not (allow_unknown_atr and atr_pct <= 0):
            examples.append(
                {
                    "bucket": "atr_pct_below_threshold",
                    "symbol": symbol,
                    "actual": round(atr_pct, 4),
                    "threshold": round(atr_threshold, 4),
                    "note": "ATR 不足",
                }
            )
        return examples

    def _load_historical_scan_settings(self, source_environment: str) -> dict:
        try:
            return _load_scan_settings(source_environment)
        except Exception:
            return {
                "scan_time_et": DEFAULT_SCAN_CUTOFF_TIME,
                "min_avg_10d_volume": 100000,
                "min_premarket_volume": 5000,
                "min_atr_pct": 0.15,
                "min_abs_day_change_pct": 1.0,
                "monitor_count": 0,
                "target_subscription_limit": 80,
                "total_subscription_limit": 80,
                "trade_subscription_budget": 80,
            }

    def _build_historical_scan_engines(self, symbol: str, request: dict, cutoff_ms: int) -> tuple[dict, dict]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        environment = request["source_environment"]
        session_mode = request.get("scan_session_mode") or "extended"
        lookback_limit = int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
        repair_summary = self._ensure_scan_history_available(symbol, request, cutoff_ms)
        engines = {}
        details = {
            "ready_timeframes": [],
            "bars_loaded": {},
            "last_bar_time_ms_by_interval": {},
            "repair_summary": repair_summary,
        }
        for interval in SCAN_INTERVALS:
            bars = self._load_interval_bars_before(
                symbol,
                environment,
                interval,
                cutoff_ms,
                session_mode,
                lookback_limit,
            )
            details["bars_loaded"][interval] = len(bars)
            if not bars:
                continue
            engine = IndicatorEngine(symbol, interval, params=params)
            last_snapshot = None
            for bar in bars:
                last_snapshot = engine.update(bar)
            if not last_snapshot or not engine.is_ready():
                continue
            engines[(environment, symbol, interval)] = engine
            details["ready_timeframes"].append(interval)
            details["last_bar_time_ms_by_interval"][interval] = int(bars[-1].get("bar_time_ms", 0) or 0)
        details["ready_timeframes"].sort(key=lambda item: interval_to_ms(item))
        return engines, details

    def _evaluate_historical_scan_symbol(
        self,
        symbol: str,
        trade_date: str,
        request: dict,
        settings: dict | None = None,
    ) -> dict | None:
        cutoff_ms = self._build_scan_cutoff_ms(trade_date, request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME)
        metric_row = self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, None)
        scan_settings = settings or self._load_historical_scan_settings(request["source_environment"])
        prefilter_rejections = self._build_historical_scan_metric_rejections(
            symbol,
            metric_row,
            scan_settings,
            allow_unknown_atr=True,
        )
        if prefilter_rejections:
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": "neutral",
                "reason": "historical_metric_prefilter",
                "quality_gate_passed": False,
                "rejection_examples": prefilter_rejections,
                "extra": {
                    "environment": request["source_environment"],
                    "timeframes_ready": [],
                    "long_votes": 0,
                    "short_votes": 0,
                    "scan_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                    "scan_cutoff_ms": cutoff_ms,
                    "metric_row": metric_row,
                    "prefiltered_before_indicators": True,
                },
            }
        engines, details = self._build_historical_scan_engines(symbol, request, cutoff_ms)
        if not engines:
            return None
        metric_row = self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, engines)
        scanner = DailyScanner(self.pb, engines)
        result = scanner.evaluate_symbol(
            symbol,
            trade_date,
            request["source_environment"],
            metrics=metric_row,
            settings=scan_settings,
        )
        if not result:
            return None
        enriched = dict(result)
        enriched_extra = dict(result.get("extra") or {})
        enriched_extra.update(
            {
                "scan_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                "scan_cutoff_ms": cutoff_ms,
                "scan_session_mode": request.get("scan_session_mode") or "extended",
                "scan_warmup_bars": int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
                "ready_timeframes": list(details.get("ready_timeframes") or []),
                "bars_loaded": details.get("bars_loaded") or {},
                "last_bar_time_ms_by_interval": details.get("last_bar_time_ms_by_interval") or {},
                "metric_row": metric_row,
            }
        )
        enriched["extra"] = enriched_extra
        return enriched

    def _build_daily_scan_replay_plan(self, request: dict, progress_context: dict | None = None) -> dict:
        trading_dates = self._load_trading_dates(request)
        selected_symbols = []
        selected_lookup = set()
        target_rows = []
        selection_plan = {}
        daily_summaries = []
        total_days = max(1, len(trading_dates))
        universe_mode = "manual_symbols" if request.get("symbols") else "watchlist_snapshot"

        for day_index, trade_date in enumerate(trading_dates, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            progress_value = 3 + int((day_index - 1) / total_days * 12)
            self._set_progress_context(
                "running",
                "scan_replay",
                f"rebuilding premarket targets for {trade_date}",
                progress_value,
                progress_context,
            )
            universe_rows = self._load_scan_universe(request, as_of_date=trade_date)
            universe_snapshot_fallback = False
            if not universe_rows and trade_date and not request.get("symbols"):
                universe_rows = self._load_scan_universe(request, as_of_date="")
                universe_snapshot_fallback = bool(universe_rows)
            day_candidates = []
            scanned_count = 0
            ready_count = 0
            quality_rejected_count = 0
            rejection_summary: dict[str, int] = {}
            scan_settings = self._load_historical_scan_settings(request["source_environment"])
            for item in universe_rows:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                symbol = str(item.get("symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                scanned_count += 1
                evaluated = self._evaluate_historical_scan_symbol(symbol, trade_date, request, settings=scan_settings)
                if not evaluated:
                    continue
                ready_count += 1
                if not bool(evaluated.get("quality_gate_passed")):
                    quality_rejected_count += 1
                    for example in evaluated.get("rejection_examples") or []:
                        bucket = str((example or {}).get("bucket") or "").strip()
                        if bucket:
                            rejection_summary[bucket] = int(rejection_summary.get(bucket, 0) or 0) + 1
                    continue
                score = float(evaluated.get("score", 0) or 0)
                direction_bias = str(evaluated.get("direction_bias", "neutral") or "neutral")
                if score <= 0 or direction_bias == "neutral":
                    continue
                day_candidates.append(
                    {
                        "symbol": symbol,
                        "exchange": str(item.get("exchange", "") or "").upper(),
                        "score": score,
                        "direction_bias": direction_bias,
                        "scan_reason": str(evaluated.get("reason", "") or ""),
                        "extra": dict(evaluated.get("extra") or {}),
                    }
                )

            day_candidates.sort(key=lambda item: (-float(item.get("score", 0) or 0), item.get("symbol", "")))
            selected_rows = day_candidates[: request["max_symbols"]]
            selection_plan[trade_date] = [item["symbol"] for item in selected_rows]
            for rank, candidate in enumerate(selected_rows, start=1):
                symbol = candidate["symbol"]
                if symbol not in selected_lookup:
                    selected_lookup.add(symbol)
                    selected_symbols.append(symbol)
                cutoff_ms = int((candidate.get("extra") or {}).get("scan_cutoff_ms", 0) or 0)
                target_rows.append(
                    {
                        "symbol": symbol,
                        "exchange": candidate.get("exchange", ""),
                        "date": trade_date,
                        "direction_bias": candidate.get("direction_bias", "neutral"),
                        "score": round(float(candidate.get("score", 0) or 0), 4),
                        "scan_reason": candidate.get("scan_reason", ""),
                        "status": "active",
                        "rank": rank,
                        "us_time": format_us_time(cutoff_ms) if cutoff_ms > 0 else f"{trade_date} {request.get('premarket_cutoff_time') or DEFAULT_SCAN_CUTOFF_TIME}",
                        "cn_time": format_cn_time(cutoff_ms) if cutoff_ms > 0 else "",
                        "bar_time_ms": cutoff_ms,
                        "environment": BACKTEST_ENVIRONMENT,
                        "extra": {
                            **(candidate.get("extra") or {}),
                            "source_environment": request["source_environment"],
                            "selection_rank": rank,
                            "universe_mode": universe_mode,
                            "universe_snapshot_fallback": universe_snapshot_fallback,
                            "universe_size": scanned_count,
                            **build_runtime_timestamps(),
                        },
                    }
                )
            daily_summaries.append(
                {
                    "date": trade_date,
                    "universe_size": scanned_count,
                    "universe_snapshot_fallback": universe_snapshot_fallback,
                    "ready_symbol_count": ready_count,
                    "quality_rejected_count": quality_rejected_count,
                    "rejection_summary": rejection_summary,
                    "candidate_count": len(day_candidates),
                    "selected_count": len(selected_rows),
                    "selected_symbols": [item["symbol"] for item in selected_rows],
                }
            )

        return {
            "symbols": selected_symbols,
            "selection_plan": selection_plan,
            "target_rows": target_rows,
            "summary": {
                "mode": "daily_scan_replay",
                "target_date_count": len(trading_dates),
                "selected_symbol_count": len(selected_symbols),
                "target_row_count": len(target_rows),
                "premarket_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                "scan_session_mode": request.get("scan_session_mode") or "extended",
                "scan_warmup_bars": int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
                "universe_mode": universe_mode,
                "daily": daily_summaries,
            },
        }

    def _invert_selection_plan(self, selection_plan: dict[str, list[str]]) -> dict[str, set[str]]:
        inverted = {}
        for trade_date, symbols in (selection_plan or {}).items():
            for symbol in list(symbols or []):
                symbol_text = str(symbol or "").strip().upper()
                if not symbol_text:
                    continue
                inverted.setdefault(symbol_text, set()).add(str(trade_date or ""))
        return inverted

    def _build_daily_target_lookup(self, target_rows: list[dict], symbols: list[str]) -> dict[tuple[str, str], dict]:
        lookup: dict[tuple[str, str], dict] = {}
        fallback_rank = {str(symbol or "").strip().upper(): index for index, symbol in enumerate(symbols or [], start=1)}
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or "")[:10]
            if not symbol or not date_text:
                continue
            extra = self._parse_object(row.get("extra"))
            rank = int(row.get("rank", extra.get("selection_rank", fallback_rank.get(symbol, 999999))) or fallback_rank.get(symbol, 999999))
            score = float(row.get("score", 0) or 0)
            lookup[(date_text, symbol)] = {
                "rank": rank,
                "score": score,
                "direction_bias": str(row.get("direction_bias", "") or ""),
            }
        for symbol, rank in fallback_rank.items():
            lookup.setdefault(("", symbol), {"rank": rank, "score": 0.0, "direction_bias": ""})
        return lookup

    def _parse_hhmm_tuple(self, raw_value: Any, default: str) -> tuple[int, int]:
        text = str(raw_value or default or "00:00").strip()
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        hour_text, minute_text = str(default or "00:00").split(":", 1)
        return int(hour_text), int(minute_text)

    def _portfolio_bar_time_tuple(self, bar_or_ms: dict | int) -> tuple[int, int]:
        if isinstance(bar_or_ms, dict):
            bar_ms = int(bar_or_ms.get("bar_time_ms", 0) or 0)
        else:
            bar_ms = int(bar_or_ms or 0)
        et_time = ms_to_et(bar_ms) if bar_ms > 0 else datetime.now(ET)
        return et_time.hour, et_time.minute

    def _portfolio_bar_in_trade_window(self, bar_or_ms: dict | int, request: dict) -> bool:
        current = self._portfolio_bar_time_tuple(bar_or_ms)
        start = self._parse_hhmm_tuple(request.get("trade_window_start_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_START)
        end = self._parse_hhmm_tuple(request.get("trade_window_end_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_END)
        return start <= current <= end

    def _portfolio_bar_in_order_window(self, bar_or_ms: dict | int, request: dict) -> bool:
        current = self._portfolio_bar_time_tuple(bar_or_ms)
        start = self._parse_hhmm_tuple(request.get("trade_window_start_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_START)
        end = self._parse_hhmm_tuple(request.get("order_window_end_time"), DEFAULT_PORTFOLIO_ORDER_WINDOW_END)
        return start <= current <= end

    def _portfolio_signal_expired(self, signal: dict, bar_or_ms: dict | int, request: dict) -> bool:
        if isinstance(bar_or_ms, dict):
            bar_ms = int(bar_or_ms.get("bar_time_ms", 0) or 0)
        else:
            bar_ms = int(bar_or_ms or 0)
        signal_ms = int(signal.get("signal_bar_ms", signal.get("bar_time_ms", 0)) or 0)
        if signal_ms <= 0 or bar_ms <= 0:
            return False
        validity_ms = int(request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) * 60 * 1000
        return bar_ms - signal_ms > validity_ms

    def _cooldown_bars_to_ms(self, bars: int) -> int:
        return max(0, int(bars or 0)) * interval_to_ms("5m")

    def _backtest_cooldown_active(self, state: dict, bar: dict) -> tuple[bool, str]:
        until_ms = int(state.get("cooldown_until_ms", 0) or 0)
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        if until_ms <= 0 or bar_ms >= until_ms:
            if until_ms > 0 and bar_ms >= until_ms:
                state["cooldown_until_ms"] = 0
                state["cooldown_reason"] = ""
            return False, ""
        return True, str(state.get("cooldown_reason") or "cooldown_active")

    def _start_backtest_cooldown(self, state: dict, bar: dict, bars: int, reason: str):
        duration_ms = self._cooldown_bars_to_ms(bars)
        if duration_ms <= 0:
            return
        state["cooldown_until_ms"] = int(bar.get("bar_time_ms", 0) or 0) + duration_ms
        state["cooldown_reason"] = str(reason or "cooldown_active").strip() or "cooldown_active"

    def _maybe_apply_backtest_atr_stop(
        self,
        position: dict | None,
        snapshot: dict,
        request: dict,
    ) -> dict | None:
        if not position or not self._normalize_bool(request.get("atr_dynamic_stop_enabled"), True):
            return position
        current_price = self._coerce_float_value(snapshot.get("close"), 0.0)
        current_atr = self._coerce_float_value(snapshot.get("atr"), 0.0)
        result = compute_atr_tightened_stop(
            position,
            current_price=current_price,
            current_atr=current_atr,
            sl_atr_mult=self._coerce_float_value((request.get("params") or {}).get("strategy_params", {}).get("sl_atr_mult"), DEFAULT_PARAMS["sl_atr_mult"]),
            min_profit_r=self._coerce_float_value(request.get("atr_stop_min_profit_r"), 0.3),
            deviation_threshold=self._coerce_float_value(request.get("atr_stop_deviation_threshold"), 0.30),
            min_change=self._coerce_float_value(request.get("atr_stop_min_change"), 0.01),
        )
        if not result.get("should_update"):
            return position
        position["stop_price"] = float(result["new_sl"])
        position["last_stop_atr"] = float(result.get("current_atr", current_atr) or current_atr)
        position["atr_stop_adjust_count"] = int(position.get("atr_stop_adjust_count", 0) or 0) + 1
        position["last_atr_stop_adjust"] = result
        return position

    def _coerce_float_value(self, value: Any, default: float = 0.0) -> float:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip().replace(",", "")
        if not text:
            return default
        try:
            return float(text)
        except Exception:
            return default

    def _resolve_account_buying_power_snapshot(self, environment: str = "live") -> dict:
        if not callable(self.account_snapshot_provider):
            return {
                "ok": False,
                "reason": "account_snapshot_provider_unavailable",
                "snapshot": {},
                "buying_power": 0.0,
            }
        try:
            try:
                snapshot = self.account_snapshot_provider(environment) or {}
            except TypeError:
                snapshot = self.account_snapshot_provider() or {}
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"account_snapshot_error: {exc}",
                "snapshot": {},
                "buying_power": 0.0,
            }
        summary = snapshot.get("summary") if isinstance(snapshot, dict) else {}
        summary = summary if isinstance(summary, dict) else {}
        buying_power = self._coerce_float_value(summary.get("buying_power"), 0.0)
        return {
            "ok": buying_power > 0,
            "reason": "" if buying_power > 0 else "buying_power_unavailable",
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
            "buying_power": buying_power,
        }

    def _resolve_portfolio_risk_limits(self, request: dict) -> dict:
        initial_capital = float(request.get("initial_capital", 0) or 0)
        mode = str(request.get("borrow_limit_mode") or "none").strip().lower()
        account_snapshot = {}
        account_summary = {}
        account_snapshot_ok = False
        account_snapshot_reason = ""
        if mode == "account_buying_power":
            resolved = self._resolve_account_buying_power_snapshot(request.get("source_environment") or "live")
            account_snapshot = resolved.get("snapshot") or {}
            account_summary = account_snapshot.get("summary") if isinstance(account_snapshot.get("summary"), dict) else {}
            account_snapshot_ok = bool(resolved.get("ok"))
            account_snapshot_reason = str(resolved.get("reason") or "")
            buying_power = float(resolved.get("buying_power", 0) or 0)
            if buying_power <= 0:
                raise ValueError(f"account_buying_power_unavailable: {account_snapshot_reason or 'missing buying_power'}")
            total_exposure_limit = buying_power
            max_borrow_amount = max(0.0, buying_power - initial_capital)
        elif mode == "fixed":
            max_borrow_amount = max(0.0, float(request.get("max_borrow_amount", 0) or 0))
            total_exposure_limit = initial_capital + max_borrow_amount
        else:
            mode = "none"
            max_borrow_amount = 0.0
            total_exposure_limit = initial_capital
        return {
            "initial_capital": round(initial_capital, 4),
            "borrow_limit_mode": mode,
            "max_borrow_amount": round(max_borrow_amount, 4),
            "total_exposure_limit": round(max(0.0, total_exposure_limit), 4),
            "account_snapshot_ok": account_snapshot_ok,
            "account_snapshot_reason": account_snapshot_reason,
            "account_summary": {
                "account_code": account_summary.get("account_code") or "",
                "account_type": account_summary.get("account_type") or "",
                "currency": account_summary.get("currency") or "",
                "buying_power": self._coerce_float_value(account_summary.get("buying_power"), 0.0),
                "available_funds": self._coerce_float_value(account_summary.get("available_funds"), 0.0),
                "net_liquidation": self._coerce_float_value(account_summary.get("net_liquidation"), 0.0),
                "gross_position_value": self._coerce_float_value(account_summary.get("gross_position_value"), 0.0),
            },
        }

    def _portfolio_signal_exposure(self, signal: dict) -> float:
        shares = max(0, int(signal.get("shares", 0) or 0))
        entry_price = float(signal.get("entry_price", signal.get("entry", 0)) or 0)
        return max(0.0, shares * entry_price)

    def _portfolio_position_exposure(self, position: dict) -> float:
        explicit = self._coerce_float_value(position.get("entry_exposure"), -1.0)
        if explicit >= 0:
            return explicit
        shares = max(0, int(position.get("shares", 0) or 0))
        entry_price = float(position.get("entry_price", 0) or 0)
        return max(0.0, shares * entry_price)

    def _portfolio_projected_borrow(self, ledger: dict, additional_exposure: float = 0.0) -> float:
        equity_cash = float(ledger.get("initial_capital", 0) or 0) + float(ledger.get("realized_pnl", 0) or 0)
        projected_exposure = (
            float(ledger.get("open_exposure", 0) or 0)
            + float(ledger.get("reserved_exposure", 0) or 0)
            + float(additional_exposure or 0)
        )
        return max(0.0, projected_exposure - equity_cash)

    def _portfolio_can_reserve_exposure(self, ledger: dict, exposure: float) -> tuple[bool, str, dict]:
        requested = max(0.0, float(exposure or 0))
        projected_exposure = (
            float(ledger.get("open_exposure", 0) or 0)
            + float(ledger.get("reserved_exposure", 0) or 0)
            + requested
        )
        total_limit = float(ledger.get("total_exposure_limit", 0) or 0)
        projected_borrow = self._portfolio_projected_borrow(ledger, requested)
        max_borrow = float(ledger.get("max_borrow_amount", 0) or 0)
        details = {
            "requested_exposure": round(requested, 4),
            "projected_exposure": round(projected_exposure, 4),
            "total_exposure_limit": round(total_limit, 4),
            "projected_borrowed_amount": round(projected_borrow, 4),
            "max_borrow_amount": round(max_borrow, 4),
        }
        if total_limit > 0 and projected_exposure - total_limit > 1e-6:
            return False, "buying_power_exceeded", details
        if projected_borrow - max_borrow > 1e-6:
            return False, "borrow_limit_exceeded", details
        return True, "ok", details

    def _portfolio_record_rejection(self, ledger: dict, reason: str):
        key = str(reason or "unknown").strip() or "unknown"
        rejection_counts = ledger.setdefault("rejection_counts", {})
        rejection_counts[key] = int(rejection_counts.get(key, 0) or 0) + 1

    def _portfolio_record_candidate_sample(self, ledger: dict, candidate: dict, status: str, reason: str, details: dict | None = None):
        samples = ledger.setdefault("candidate_samples", [])
        if len(samples) >= 200:
            return
        signal_payload = candidate.get("signal_payload") or {}
        bar = candidate.get("bar") or {}
        samples.append(
            {
                "bar_time_ms": int(bar.get("bar_time_ms", signal_payload.get("bar_time_ms", 0)) or 0),
                "us_time": str(bar.get("us_time", signal_payload.get("us_time", "")) or ""),
                "symbol": str(signal_payload.get("symbol", candidate.get("symbol", "")) or ""),
                "direction": str(signal_payload.get("direction", "") or ""),
                "status": str(status or ""),
                "reason": str(reason or ""),
                "target_rank": int(candidate.get("target_rank", 999999) or 999999),
                "target_score": round(float(candidate.get("target_score", 0) or 0), 4),
                "signal_quality": round(float(candidate.get("signal_quality", 0) or 0), 4),
                "requested_exposure": round(float(candidate.get("requested_exposure", 0) or 0), 4),
                **(details or {}),
            }
        )

    def _build_portfolio_candidate(
        self,
        state: dict,
        bar: dict,
        index: int,
        signal: dict,
        signal_payload: dict,
        signal_row: dict,
        current_day: str,
        target_lookup: dict[tuple[str, str], dict],
    ) -> dict:
        symbol = str(state.get("symbol", signal_payload.get("symbol", "")) or "").strip().upper()
        target_meta = target_lookup.get((current_day, symbol)) or target_lookup.get(("", symbol)) or {}
        extra = dict(signal_payload.get("extra") or {})
        rr = self._coerce_float_value(signal_payload.get("rr"), 0.0)
        signal_quality = max(
            rr,
            self._coerce_float_value(extra.get("signal_score"), 0.0),
            self._coerce_float_value(extra.get("score"), 0.0),
            self._coerce_float_value(extra.get("sl_atr_ratio"), 0.0),
        )
        requested_exposure = self._portfolio_signal_exposure(signal_payload)
        return {
            "state": state,
            "symbol": symbol,
            "bar": bar,
            "index": index,
            "signal": signal,
            "signal_payload": signal_payload,
            "signal_row": signal_row,
            "signal_id": str(signal_row.get("signal_id", "") or ""),
            "current_day": current_day,
            "target_rank": int(target_meta.get("rank", 999999) or 999999),
            "target_score": float(target_meta.get("score", 0) or 0),
            "signal_quality": signal_quality,
            "requested_exposure": requested_exposure,
        }

    def _sort_portfolio_candidates(self, candidates: list[dict], request: dict) -> list[dict]:
        priority = str(request.get("simultaneous_signal_priority") or "daily_target_rank").strip().lower()

        def sort_key(candidate: dict):
            symbol = str(candidate.get("symbol", "") or "")
            bar_ms = int((candidate.get("bar") or {}).get("bar_time_ms", 0) or 0)
            target_rank = int(candidate.get("target_rank", 999999) or 999999)
            target_score = float(candidate.get("target_score", 0) or 0)
            signal_quality = float(candidate.get("signal_quality", 0) or 0)
            requested_exposure = float(candidate.get("requested_exposure", 0) or 0)
            if priority == "signal_quality":
                return (bar_ms, -signal_quality, target_rank, -target_score, requested_exposure, symbol)
            if priority == "liquidity":
                bar = candidate.get("bar") or {}
                liquidity = float(bar.get("volume", 0) or 0) * float(bar.get("close", 0) or 0)
                return (bar_ms, -liquidity, target_rank, -signal_quality, requested_exposure, symbol)
            return (bar_ms, target_rank, -target_score, -signal_quality, requested_exposure, symbol)

        return sorted(candidates, key=sort_key)

    def _release_portfolio_pending(self, ledger: dict, pending_signal: dict | None):
        if not pending_signal:
            return
        ledger["reserved_exposure"] = max(
            0.0,
            float(ledger.get("reserved_exposure", 0) or 0) - self._portfolio_signal_exposure(pending_signal),
        )

    def _open_portfolio_position_from_pending(
        self,
        ledger: dict,
        symbol: str,
        bar: dict,
        pending_signal: dict,
        commission_per_share: float,
        slippage_bps: float,
    ) -> dict | None:
        position = self._check_pending_entry_fill(
            symbol,
            bar,
            pending_signal,
            commission_per_share,
            slippage_bps,
        )
        if not position:
            return None
        self._release_portfolio_pending(ledger, pending_signal)
        exposure = self._portfolio_position_exposure(position)
        position["entry_exposure"] = exposure
        position["portfolio_execution_model"] = "portfolio_stream"
        ledger["open_exposure"] = float(ledger.get("open_exposure", 0) or 0) + exposure
        ledger["max_gross_exposure"] = max(float(ledger.get("max_gross_exposure", 0) or 0), float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0))
        ledger["max_borrowed_amount"] = max(float(ledger.get("max_borrowed_amount", 0) or 0), self._portfolio_projected_borrow(ledger, 0))
        return position

    def _close_portfolio_position(self, ledger: dict, position: dict | None, trade: dict | None) -> dict | None:
        if not position or not trade:
            return trade
        exposure = self._portfolio_position_exposure(position)
        ledger["open_exposure"] = max(0.0, float(ledger.get("open_exposure", 0) or 0) - exposure)
        ledger["realized_pnl"] = float(ledger.get("realized_pnl", 0) or 0) + float(trade.get("pnl", 0) or 0)
        trade_extra = self._parse_object(trade.get("extra"))
        trade_extra.update(
            {
                "execution_model": "portfolio_stream",
                "entry_exposure": round(exposure, 4),
                "portfolio_open_exposure_after": round(float(ledger.get("open_exposure", 0) or 0), 4),
                "portfolio_reserved_exposure_after": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
                "portfolio_borrowed_after": round(self._portfolio_projected_borrow(ledger, 0), 4),
            }
        )
        trade["extra"] = trade_extra
        return trade

    def _accept_portfolio_candidate(self, ledger: dict, candidate: dict, signal_index: dict, request: dict) -> bool:
        if str(request.get("manual_confirm_mode") or "auto") == "strict":
            reason = "manual_confirmation_required"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        position_limit = int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX)
        if int(ledger.get("daily_position_count", 0) or 0) >= position_limit:
            reason = "position_limit_reached"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        confirm_delay_minutes = int(request.get("confirm_delay_minutes", 0) or 0)
        if str(request.get("manual_confirm_mode") or "auto") != "delayed":
            confirm_delay_minutes = 0
        validity_minutes = int(request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES)
        if confirm_delay_minutes > validity_minutes:
            reason = "signal_expired_before_confirm"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        exposure = float(candidate.get("requested_exposure", 0) or 0)
        ok, reason, details = self._portfolio_can_reserve_exposure(ledger, exposure)
        if not ok:
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
            return False

        state = candidate["state"]
        pending_signal = self._build_backtest_pending_signal(
            candidate["symbol"],
            candidate["bar"],
            candidate["signal"],
            candidate["signal_payload"],
        )
        confirm_ready_ms = int(candidate["bar"].get("bar_time_ms", 0) or 0) + confirm_delay_minutes * 60 * 1000
        pending_signal["reserved_exposure"] = exposure
        pending_signal["confirm_ready_bar_ms"] = confirm_ready_ms
        pending_signal["portfolio_target_rank"] = int(candidate.get("target_rank", 999999) or 999999)
        pending_signal["portfolio_target_score"] = float(candidate.get("target_score", 0) or 0)
        state["pending_signal"] = pending_signal
        ledger["reserved_exposure"] = float(ledger.get("reserved_exposure", 0) or 0) + exposure
        ledger["daily_position_count"] = int(ledger.get("daily_position_count", 0) or 0) + 1
        ledger["max_gross_exposure"] = max(float(ledger.get("max_gross_exposure", 0) or 0), float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0))
        ledger["max_borrowed_amount"] = max(float(ledger.get("max_borrowed_amount", 0) or 0), self._portfolio_projected_borrow(ledger, 0))
        self._mark_backtest_signal_status(
            signal_index,
            candidate.get("signal_id"),
            "pending",
            "accepted_pending_entry",
            {
                **details,
                "portfolio_target_rank": int(candidate.get("target_rank", 999999) or 999999),
                "portfolio_target_score": round(float(candidate.get("target_score", 0) or 0), 4),
                "confirm_delay_minutes": confirm_delay_minutes,
                "confirm_ready_bar_ms": confirm_ready_ms,
            },
        )
        self._portfolio_record_candidate_sample(ledger, candidate, "pending", "accepted_pending_entry", details)
        return True

    def _bootstrap_backtest_state(self, engine: IndicatorEngine, signal_gen: SignalGenerator, warmup_bars: list[dict]) -> str:
        previous_day = ""
        for bar in warmup_bars:
            current_day = str(bar.get("us_time", "") or "")[:10]
            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
            previous_day = current_day
            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue
            signal_gen.update(snapshot)
        return previous_day

    def _prepare_portfolio_symbol_state(
        self,
        symbol: str,
        bars: list[dict],
        request: dict,
        allowed_trade_days: set[str] | None,
    ) -> tuple[dict | None, dict | None]:
        if len(bars) < 40:
            return None, {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": 0,
                "status": "insufficient_data",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }
        precheck_gap_count = self._count_internal_5m_gaps(bars)
        if precheck_gap_count > 0:
            return None, {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": precheck_gap_count,
                "status": "invalid_gap",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }

        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        engine = IndicatorEngine(symbol, "5m", params=params)
        signal_gen = SignalGenerator(symbol, "5m", params=params)
        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0),
            request["session_mode"],
            limit=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
        )
        previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars) if warmup_bars else ""
        compare_with_tv = self._should_compare_with_tv(request)
        compare_tv_signals = self._should_compare_tv_signals(request)
        tv_reference = self._load_tv_reference(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
            include_signals=compare_tv_signals,
        ) if compare_with_tv else {}
        symbol_tv_parity = self._init_symbol_tv_parity(
            symbol,
            tv_reference,
            compare_tv_signals=compare_tv_signals,
        ) if compare_with_tv else None
        return {
            "symbol": symbol,
            "bars": bars,
            "engine": engine,
            "signal_gen": signal_gen,
            "daily_close_lookup": self._load_daily_close_lookup(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
            ),
            "symbol_tv_parity": symbol_tv_parity,
            "allowed_trade_days": allowed_trade_days,
            "previous_ms": 0,
            "previous_day": previous_day,
            "previous_bar": None,
            "pending_signal": None,
            "open_position": None,
            "cooldown_until_ms": 0,
            "cooldown_reason": "",
            "gap_count": 0,
            "market_bars": 0,
            "reverse_index": set(),
        }, None

    def _run_portfolio_stream_backtest(
        self,
        symbols: list[str],
        request: dict,
        allowed_trade_days_by_symbol: dict[str, set[str]] | None = None,
        target_rows: list[dict] | None = None,
        progress_context: dict | None = None,
    ) -> dict:
        risk_limits = self._resolve_portfolio_risk_limits(request)
        ledger = {
            **risk_limits,
            "realized_pnl": 0.0,
            "open_exposure": 0.0,
            "reserved_exposure": 0.0,
            "max_gross_exposure": 0.0,
            "max_borrowed_amount": 0.0,
            "daily_position_count": 0,
            "current_day": "",
            "rejection_counts": {},
            "candidate_samples": [],
        }
        states: dict[str, dict] = {}
        bars_by_time: dict[int, list[tuple[str, int, dict]]] = {}
        data_quality = []
        skipped_symbols = []
        all_trades = []
        all_indicator_rows = []
        indicator_count = 0
        all_signal_rows = []
        all_reverse_rows = []
        tv_symbol_reports = []
        signal_index = {}
        target_lookup = self._build_daily_target_lookup(target_rows or [], symbols)

        total_symbols = max(1, len(symbols))
        for completed_symbols, symbol in enumerate(symbols):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            progress_value = 16 + int((completed_symbols / total_symbols) * 8)
            self._set_progress_context("running", "loading", f"loading {symbol}", progress_value, progress_context)
            bars = self._load_symbol_bars(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                request["session_mode"],
                allow_backfill=False,
            )
            state, quality = self._prepare_portfolio_symbol_state(
                symbol,
                bars,
                request,
                (allowed_trade_days_by_symbol or {}).get(symbol),
            )
            if quality:
                data_quality.append(quality)
                skipped_symbols.append(symbol)
                bars.clear()
                continue
            if not state:
                skipped_symbols.append(symbol)
                bars.clear()
                continue
            states[symbol] = state
            for index, bar in enumerate(state["bars"]):
                bar_ms = int(bar.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                bars_by_time.setdefault(bar_ms, []).append((symbol, index, bar))

        if not states:
            result = {
                "trades": [],
                "indicator_rows": [],
                "indicator_count": 0,
                "signal_rows": [],
                "reverse_rows": [],
                "data_quality": data_quality,
                "skipped_symbols": skipped_symbols,
                "tv_symbol_reports": [],
                "portfolio_metrics": {
                    "execution_model": "portfolio_stream",
                    "portfolio_risk": risk_limits,
                    "portfolio_rejection_counts": {},
                    "portfolio_candidate_samples": [],
                },
            }
            self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
            return result

        bar_times = sorted(bars_by_time.keys())
        total_steps = max(1, len(bar_times))
        commission_per_share = float(request["commission_per_share"])
        slippage_bps = float(request["slippage_bps"])
        force_flat_eod = bool(request["force_flat_eod"])
        compare_tv_signals = self._should_compare_tv_signals(request)
        capture_indicator_rows = self._should_persist_backtest_indicators(request)

        def append_trade(position: dict, trade: dict | None):
            if not trade:
                return
            closed_trade = self._close_portfolio_position(ledger, position, trade)
            if closed_trade:
                all_trades.append(closed_trade)
                if str(closed_trade.get("exit_reason") or "") == "stop_loss":
                    state = states.get(str(closed_trade.get("symbol") or "").upper())
                    if state is not None:
                        self._start_backtest_cooldown(
                            state,
                            {"bar_time_ms": int(closed_trade.get("exit_bar_ms", 0) or 0)},
                            int(request.get("cooldown_bars_after_sl", 6) or 0),
                            "cooldown_after_stop_loss",
                        )

        for step_index, bar_ms in enumerate(bar_times):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            if step_index % 50 == 0 or step_index == total_steps - 1:
                progress_value = 25 + int((step_index / total_steps) * 60)
                self._set_progress_context("running", "streaming", f"streaming portfolio {step_index + 1}/{total_steps}", progress_value, progress_context)
            entries = sorted(bars_by_time.pop(bar_ms, []) or [], key=lambda item: item[0])
            current_day = ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if ledger.get("current_day") and ledger["current_day"] != current_day:
                ledger["daily_position_count"] = 0
            ledger["current_day"] = current_day

            candidates = []
            for symbol, index, bar in entries:
                state = states[symbol]
                state["market_bars"] = int(state.get("market_bars", 0) or 0) + 1
                current_symbol_day = str(bar.get("us_time", "") or "")[:10]

                previous_ms = int(state.get("previous_ms", 0) or 0)
                if previous_ms > 0 and int(bar["bar_time_ms"]) - previous_ms > interval_to_ms("5m") * 3:
                    state["gap_count"] = int(state.get("gap_count", 0) or 0) + 1
                state["previous_ms"] = int(bar["bar_time_ms"])

                if state.get("previous_day") and current_symbol_day != state.get("previous_day"):
                    state["signal_gen"].daily_reset()
                    state["cooldown_until_ms"] = 0
                    state["cooldown_reason"] = ""
                    if state.get("open_position") and force_flat_eod and state.get("previous_bar"):
                        trade = self._close_position(
                            state["open_position"],
                            state["previous_bar"],
                            commission_per_share,
                            slippage_bps,
                            "eod",
                        )
                        append_trade(state["open_position"], trade)
                        state["open_position"] = None
                    if state.get("pending_signal"):
                        self._mark_backtest_signal_status(signal_index, state["pending_signal"].get("signal_id"), "dropped", "new_day_reset")
                        self._release_portfolio_pending(ledger, state["pending_signal"])
                    state["pending_signal"] = None
                state["previous_day"] = current_symbol_day

                pending_signal = state.get("pending_signal")
                if pending_signal and state.get("open_position") is None:
                    if self._portfolio_signal_expired(pending_signal, bar, request):
                        self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "signal_expired")
                        self._portfolio_record_rejection(ledger, "signal_expired")
                        self._release_portfolio_pending(ledger, pending_signal)
                        state["pending_signal"] = None
                    elif int(bar.get("bar_time_ms", 0) or 0) >= int(pending_signal.get("confirm_ready_bar_ms", pending_signal.get("signal_bar_ms", 0)) or 0):
                        filled_position = self._open_portfolio_position_from_pending(
                            ledger,
                            symbol,
                            bar,
                            pending_signal,
                            commission_per_share,
                            slippage_bps,
                        )
                        if filled_position:
                            state["open_position"] = filled_position
                            self._mark_backtest_signal_status(
                                signal_index,
                                pending_signal.get("signal_id"),
                                "executed",
                                "entry_limit_filled",
                                {
                                    "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                                    "entry_us_time": str(bar.get("us_time", "") or ""),
                                    "entry_cn_time": str(bar.get("cn_time", "") or ""),
                                    "entry_price": round(float(filled_position.get("entry_price", 0) or 0), 4),
                                    "entry_limit_price": round(float(filled_position.get("entry_limit_price", 0) or 0), 4),
                                    "portfolio_open_exposure": round(float(ledger.get("open_exposure", 0) or 0), 4),
                                    "portfolio_reserved_exposure": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
                                },
                            )
                            state["pending_signal"] = None

                if state.get("open_position"):
                    closed = self._check_exit(state["open_position"], bar, commission_per_share, slippage_bps)
                    if closed:
                        append_trade(state["open_position"], closed)
                        state["open_position"] = None

                snapshot = state["engine"].update(bar)
                if not snapshot or not state["engine"].is_ready():
                    state["previous_bar"] = bar
                    continue
                indicator_count += 1

                daily_fields = self._get_daily_change_fields_from_lookup(
                    state["daily_close_lookup"],
                    float(snapshot.get("close", 0) or 0),
                    int(bar.get("bar_time_ms", 0) or 0),
                )
                indicator_payload = None
                indicator_audit = None
                if state.get("symbol_tv_parity") is not None or capture_indicator_rows:
                    indicator_payload = self._build_tv_indicator_compare_payload(
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        request["source_environment"],
                        daily_fields,
                    )
                    if state.get("symbol_tv_parity") is not None:
                        indicator_audit = self._compare_generated_indicator(state["symbol_tv_parity"], indicator_payload)
                    if capture_indicator_rows:
                        all_indicator_rows.append(
                            self._build_backtest_indicator_row(
                                request,
                                bar,
                                indicator_payload,
                                indicator_audit,
                            )
                        )

                signal = state["signal_gen"].update(snapshot)
                trading_day_enabled = state.get("allowed_trade_days") is None or current_symbol_day in state.get("allowed_trade_days")
                preexisting_pending_signal = state.get("pending_signal") if state.get("pending_signal") and state.get("open_position") is None else None
                preexisting_open_position = state.get("open_position")
                signal_conflict_emitted = False

                if signal and int(signal.get("shares", 0) or 0) > 0:
                    signal_payload = self._build_tv_signal_compare_payload(
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        signal,
                        request["source_environment"],
                        daily_fields,
                    )
                    if state.get("symbol_tv_parity") is not None and compare_tv_signals:
                        self._compare_generated_signal(state["symbol_tv_parity"], signal_payload)
                    signal_row = self._build_backtest_signal_row(request, bar, signal_payload)
                    signal_row["status"] = "generated"
                    all_signal_rows.append(signal_row)
                    signal_id = str(signal_row.get("signal_id", "") or "")
                    if signal_id:
                        signal_index[signal_id] = signal_row

                    if not trading_day_enabled:
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "symbol_not_selected_for_day")
                    elif not self._portfolio_bar_in_trade_window(bar, request):
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "outside_trade_window")
                        self._portfolio_record_rejection(ledger, "outside_trade_window")
                    elif not self._portfolio_bar_in_order_window(bar, request):
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "outside_order_window")
                        self._portfolio_record_rejection(ledger, "outside_order_window")
                    elif self._backtest_cooldown_active(state, bar)[0]:
                        _, cooldown_reason = self._backtest_cooldown_active(state, bar)
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", cooldown_reason)
                        self._portfolio_record_rejection(ledger, cooldown_reason)
                    else:
                        active_target = preexisting_pending_signal if preexisting_pending_signal else preexisting_open_position
                        active_state = "pending_entry" if preexisting_pending_signal else ("filled_position" if preexisting_open_position else "")
                        active_direction = str((active_target or {}).get("direction", "") or "").strip().lower()
                        new_direction = str(signal_payload.get("direction", "") or "").strip().lower()
                        if active_target or index >= len(state["bars"]) - 1:
                            if active_target and active_direction and new_direction and new_direction != active_direction:
                                reverse_row = self._build_backtest_reverse_signal_row(
                                    request,
                                    symbol,
                                    bar,
                                    state["engine"].bar_count,
                                    snapshot,
                                    daily_fields,
                                    active_target,
                                    target_state=active_state,
                                    reverse_kind="signal_conflict",
                                    source="signal",
                                    origin_signal_payload=signal_payload,
                                )
                                reverse_key = self._build_backtest_reverse_key(reverse_row)
                                if reverse_row and reverse_key not in state["reverse_index"]:
                                    all_reverse_rows.append(reverse_row)
                                    state["reverse_index"].add(reverse_key)
                                    signal_conflict_emitted = True
                                    if preexisting_pending_signal and active_state == "pending_entry":
                                        updated_pending = self._apply_backtest_pending_reverse_action(
                                            state["pending_signal"],
                                            reverse_row,
                                            signal_index,
                                        )
                                        if updated_pending is None:
                                            self._release_portfolio_pending(ledger, state["pending_signal"])
                                        state["pending_signal"] = updated_pending
                                    elif preexisting_open_position and active_state == "filled_position":
                                        original_position = state["open_position"]
                                        state["open_position"], reverse_trade = self._apply_backtest_position_reverse_action(
                                            state["open_position"],
                                            reverse_row,
                                            bar,
                                            commission_per_share,
                                            slippage_bps,
                                        )
                                        if reverse_trade:
                                            append_trade(original_position, reverse_trade)
                                            self._start_backtest_cooldown(
                                                state,
                                                bar,
                                                int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                                "cooldown_after_reverse_close",
                                            )
                            if active_target:
                                drop_reason = "active_target_exists"
                                if active_direction and new_direction and new_direction != active_direction:
                                    drop_reason = "signal_conflict_active_target"
                                self._mark_backtest_signal_status(signal_index, signal_id, "dropped", drop_reason)
                            elif index >= len(state["bars"]) - 1:
                                self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "last_bar_no_entry")
                        else:
                            if force_flat_eod and index < len(state["bars"]) - 1:
                                next_day = str(state["bars"][index + 1].get("us_time", "") or "")[:10]
                                if next_day != current_symbol_day:
                                    self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "force_flat_eod")
                                else:
                                    candidates.append(
                                        self._build_portfolio_candidate(
                                            state,
                                            bar,
                                            index,
                                            signal,
                                            signal_payload,
                                            signal_row,
                                            current_symbol_day,
                                            target_lookup,
                                        )
                                    )
                            else:
                                candidates.append(
                                    self._build_portfolio_candidate(
                                        state,
                                        bar,
                                        index,
                                        signal,
                                        signal_payload,
                                        signal_row,
                                        current_symbol_day,
                                        target_lookup,
                                    )
                                )

                if not signal_conflict_emitted and preexisting_pending_signal and state.get("pending_signal") and state.get("open_position") is None:
                    reverse_row = self._build_backtest_reverse_signal_row(
                        request,
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        daily_fields,
                        state["pending_signal"],
                        target_state="pending_entry",
                        reverse_kind="indicator_conflict",
                        source="indicator",
                    )
                    reverse_key = self._build_backtest_reverse_key(reverse_row)
                    if reverse_row and reverse_key not in state["reverse_index"]:
                        all_reverse_rows.append(reverse_row)
                        state["reverse_index"].add(reverse_key)
                        updated_pending = self._apply_backtest_pending_reverse_action(
                            state["pending_signal"],
                            reverse_row,
                            signal_index,
                        )
                        if updated_pending is None:
                            self._release_portfolio_pending(ledger, state["pending_signal"])
                        state["pending_signal"] = updated_pending

                if not signal_conflict_emitted and preexisting_open_position and state.get("open_position"):
                    reverse_row = self._build_backtest_reverse_signal_row(
                        request,
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        daily_fields,
                        state["open_position"],
                        target_state="filled_position",
                        reverse_kind="indicator_conflict",
                        source="indicator",
                    )
                    reverse_key = self._build_backtest_reverse_key(reverse_row)
                    if reverse_row and reverse_key not in state["reverse_index"]:
                        all_reverse_rows.append(reverse_row)
                        state["reverse_index"].add(reverse_key)
                        original_position = state["open_position"]
                        state["open_position"], reverse_trade = self._apply_backtest_position_reverse_action(
                            state["open_position"],
                            reverse_row,
                            bar,
                            commission_per_share,
                            slippage_bps,
                        )
                        if reverse_trade:
                            append_trade(original_position, reverse_trade)
                            self._start_backtest_cooldown(
                                state,
                                bar,
                                int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                "cooldown_after_reverse_close",
                            )

                if state.get("open_position"):
                    state["open_position"] = self._maybe_apply_backtest_atr_stop(
                        state["open_position"],
                        snapshot,
                        request,
                    )

                state["previous_bar"] = bar

            for candidate in self._sort_portfolio_candidates(candidates, request):
                self._accept_portfolio_candidate(ledger, candidate, signal_index, request)

        for symbol in sorted(states.keys()):
            state = states[symbol]
            if state.get("open_position"):
                last_bar = state["bars"][-1]
                trade = self._close_position(
                    state["open_position"],
                    last_bar,
                    commission_per_share,
                    slippage_bps,
                    "last_bar",
                )
                append_trade(state["open_position"], trade)
                state["open_position"] = None
            if state.get("pending_signal"):
                self._mark_backtest_signal_status(signal_index, state["pending_signal"].get("signal_id"), "dropped", "last_bar_no_entry")
                self._release_portfolio_pending(ledger, state["pending_signal"])
                state["pending_signal"] = None

            quality = {
                "symbol": symbol,
                "bar_count": len(state["bars"]),
                "gap_count": int(state.get("gap_count", 0) or 0),
                "status": "ok",
                "first_bar_us": state["bars"][0].get("us_time", "") if state["bars"] else "",
                "last_bar_us": state["bars"][-1].get("us_time", "") if state["bars"] else "",
                "market_bars": int(state.get("market_bars", 0) or 0),
                "selected_trade_day_count": len(state.get("allowed_trade_days") or []),
            }
            data_quality.append(quality)
            if state.get("symbol_tv_parity") is not None:
                self._finalize_symbol_tv_parity(state["symbol_tv_parity"])
                tv_symbol_reports.append(state["symbol_tv_parity"])

        all_trades.sort(key=lambda item: (int(item.get("exit_bar_ms", 0) or 0), item.get("symbol", "")))
        all_reverse_rows.sort(key=lambda item: (int(item.get("bar_time_ms", 0) or 0), item.get("symbol", ""), item.get("action_type", "")))
        gross_now = float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0)
        portfolio_metrics = {
            "execution_model": "portfolio_stream",
            "portfolio_risk": {
                **risk_limits,
                "position_limit_max": int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX),
                "signal_validity_minutes": int(request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES),
                "trade_window_start_time": str(request.get("trade_window_start_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_START),
                "trade_window_end_time": str(request.get("trade_window_end_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_END),
                "order_window_end_time": str(request.get("order_window_end_time") or DEFAULT_PORTFOLIO_ORDER_WINDOW_END),
                "simultaneous_signal_priority": str(request.get("simultaneous_signal_priority") or "daily_target_rank"),
                "manual_confirm_mode": str(request.get("manual_confirm_mode") or "auto"),
                "confirm_delay_minutes": int(request.get("confirm_delay_minutes", 0) or 0),
            },
            "portfolio_rejection_counts": dict(ledger.get("rejection_counts") or {}),
            "portfolio_candidate_samples": list(ledger.get("candidate_samples") or []),
            "portfolio_max_gross_exposure": round(float(ledger.get("max_gross_exposure", 0) or 0), 4),
            "portfolio_max_borrowed_amount": round(float(ledger.get("max_borrowed_amount", 0) or 0), 4),
            "portfolio_final_open_exposure": round(float(ledger.get("open_exposure", 0) or 0), 4),
            "portfolio_final_reserved_exposure": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
            "portfolio_final_gross_exposure": round(gross_now, 4),
            "portfolio_realized_pnl": round(float(ledger.get("realized_pnl", 0) or 0), 4),
        }
        result = {
            "trades": all_trades,
            "indicator_rows": all_indicator_rows,
            "indicator_count": indicator_count,
            "signal_rows": all_signal_rows,
            "reverse_rows": all_reverse_rows,
            "data_quality": data_quality,
            "skipped_symbols": skipped_symbols,
            "tv_symbol_reports": tv_symbol_reports,
            "portfolio_metrics": portfolio_metrics,
        }
        self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
        return result

    def _run_symbol_backtest(
        self,
        symbol: str,
        bars: list[dict],
        request: dict,
        allowed_trade_days: set[str] | None = None,
    ) -> tuple[list[dict], dict, dict | None, list[dict], list[dict], list[dict], int]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        slippage_bps = float(request["slippage_bps"])
        commission_per_share = float(request["commission_per_share"])
        force_flat_eod = bool(request["force_flat_eod"])
        engine = IndicatorEngine(symbol, "5m", params=params)
        signal_gen = SignalGenerator(symbol, "5m", params=params)
        compare_with_tv = self._should_compare_with_tv(request)
        compare_tv_signals = self._should_compare_tv_signals(request)
        capture_indicator_rows = self._should_persist_backtest_indicators(request)
        tv_reference = self._load_tv_reference(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
            include_signals=compare_tv_signals,
        ) if compare_with_tv else {}
        daily_close_lookup = self._load_daily_close_lookup(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
        )
        symbol_tv_parity = self._init_symbol_tv_parity(
            symbol,
            tv_reference,
            compare_tv_signals=compare_tv_signals,
        ) if compare_with_tv else None
        trades = []
        indicator_rows = []
        indicator_count = 0
        signal_rows = []
        reverse_rows = []
        signal_index = {}
        reverse_index = set()
        gap_count = 0
        market_bars = 0
        previous_ms = 0
        previous_day = ""
        pending_signal = None
        open_position = None
        cooldown_state = {"cooldown_until_ms": 0, "cooldown_reason": ""}

        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0) if bars else 0,
            request["session_mode"],
            limit=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
        )
        if warmup_bars:
            previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars)

        precheck_gap_count = self._count_internal_5m_gaps(bars)
        if precheck_gap_count > 0:
            quality = {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": precheck_gap_count,
                "status": "invalid_gap",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }
            return [], quality, symbol_tv_parity, [], [], [], 0

        for index, bar in enumerate(bars):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            market_bars += 1
            current_day = str(bar.get("us_time", "") or "")[:10]

            if previous_ms > 0 and int(bar["bar_time_ms"]) - previous_ms > interval_to_ms("5m") * 3:
                gap_count += 1
            previous_ms = int(bar["bar_time_ms"])

            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
                cooldown_state["cooldown_until_ms"] = 0
                cooldown_state["cooldown_reason"] = ""
                if open_position and force_flat_eod:
                    exit_trade = self._close_position(open_position, bars[index - 1], commission_per_share, slippage_bps, "eod")
                    trades.append(exit_trade)
                    open_position = None
                if pending_signal:
                    self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "new_day_reset")
                pending_signal = None
            previous_day = current_day

            if pending_signal and open_position is None:
                filled_position = self._check_pending_entry_fill(
                    symbol,
                    bar,
                    pending_signal,
                    commission_per_share,
                    slippage_bps,
                )
                if filled_position:
                    open_position = filled_position
                    self._mark_backtest_signal_status(
                        signal_index,
                        pending_signal.get("signal_id"),
                        "executed",
                        "entry_limit_filled",
                        {
                            "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                            "entry_us_time": str(bar.get("us_time", "") or ""),
                            "entry_cn_time": str(bar.get("cn_time", "") or ""),
                            "entry_price": round(float(open_position.get("entry_price", 0) or 0), 4),
                            "entry_limit_price": round(float(open_position.get("entry_limit_price", 0) or 0), 4),
                        },
                    )
                    pending_signal = None

            if open_position:
                closed = self._check_exit(open_position, bar, commission_per_share, slippage_bps)
                if closed:
                    trades.append(closed)
                    if str(closed.get("exit_reason") or "") == "stop_loss":
                        self._start_backtest_cooldown(
                            cooldown_state,
                            bar,
                            int(request.get("cooldown_bars_after_sl", 6) or 0),
                            "cooldown_after_stop_loss",
                        )
                    open_position = None

            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue
            indicator_count += 1

            daily_fields = self._get_daily_change_fields_from_lookup(
                daily_close_lookup,
                float(snapshot.get("close", 0) or 0),
                int(bar.get("bar_time_ms", 0) or 0),
            )
            indicator_payload = None
            indicator_audit = None
            if symbol_tv_parity is not None or capture_indicator_rows:
                indicator_payload = self._build_tv_indicator_compare_payload(
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    request["source_environment"],
                    daily_fields,
                )
                if symbol_tv_parity is not None:
                    indicator_audit = self._compare_generated_indicator(symbol_tv_parity, indicator_payload)
                if capture_indicator_rows:
                    indicator_rows.append(
                        self._build_backtest_indicator_row(
                            request,
                            bar,
                            indicator_payload,
                            indicator_audit,
                        )
                    )
            signal = signal_gen.update(snapshot)
            trading_day_enabled = allowed_trade_days is None or current_day in allowed_trade_days
            preexisting_pending_signal = pending_signal if pending_signal and open_position is None else None
            preexisting_open_position = open_position
            signal_conflict_emitted = False

            if signal and int(signal.get("shares", 0) or 0) > 0:
                signal_payload = self._build_tv_signal_compare_payload(
                    symbol,
                    bar,
                    engine.bar_count,
                    signal,
                    request["source_environment"],
                    daily_fields,
                )
                if symbol_tv_parity is not None and compare_tv_signals:
                    self._compare_generated_signal(symbol_tv_parity, signal_payload)
                signal_row = self._build_backtest_signal_row(request, bar, signal_payload)
                signal_row["status"] = "generated"
                signal_rows.append(signal_row)
                signal_id = str(signal_row.get("signal_id", "") or "")
                if signal_id:
                    signal_index[signal_id] = signal_row
                if not trading_day_enabled:
                    self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "symbol_not_selected_for_day")
                elif self._backtest_cooldown_active(cooldown_state, bar)[0]:
                    _, cooldown_reason = self._backtest_cooldown_active(cooldown_state, bar)
                    self._mark_backtest_signal_status(signal_index, signal_id, "skipped", cooldown_reason)
                else:
                    active_target = preexisting_pending_signal if preexisting_pending_signal else preexisting_open_position
                    active_state = "pending_entry" if preexisting_pending_signal else ("filled_position" if preexisting_open_position else "")
                    active_direction = str((active_target or {}).get("direction", "") or "").strip().lower()
                    new_direction = str(signal_payload.get("direction", "") or "").strip().lower()
                    if active_target or index >= len(bars) - 1:
                        if active_target and active_direction and new_direction and new_direction != active_direction:
                            reverse_row = self._build_backtest_reverse_signal_row(
                                request,
                                symbol,
                                bar,
                                engine.bar_count,
                                snapshot,
                                daily_fields,
                                active_target,
                                target_state=active_state,
                                reverse_kind="signal_conflict",
                                source="signal",
                                origin_signal_payload=signal_payload,
                            )
                            reverse_key = self._build_backtest_reverse_key(reverse_row)
                            if reverse_row and reverse_key not in reverse_index:
                                reverse_rows.append(reverse_row)
                                reverse_index.add(reverse_key)
                                signal_conflict_emitted = True
                                if preexisting_pending_signal and active_state == "pending_entry":
                                    pending_signal = self._apply_backtest_pending_reverse_action(
                                        pending_signal,
                                        reverse_row,
                                        signal_index,
                                    )
                                elif preexisting_open_position and active_state == "filled_position":
                                    open_position, reverse_trade = self._apply_backtest_position_reverse_action(
                                        open_position,
                                        reverse_row,
                                        bar,
                                        commission_per_share,
                                        slippage_bps,
                                    )
                                    if reverse_trade:
                                        trades.append(reverse_trade)
                                        self._start_backtest_cooldown(
                                            cooldown_state,
                                            bar,
                                            int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                            "cooldown_after_reverse_close",
                                        )
                        if active_target:
                            drop_reason = "active_target_exists"
                            if active_direction and new_direction and new_direction != active_direction:
                                drop_reason = "signal_conflict_active_target"
                            self._mark_backtest_signal_status(signal_index, signal_id, "dropped", drop_reason)
                        elif index >= len(bars) - 1:
                            self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "last_bar_no_entry")
                    else:
                        pending_signal = self._build_backtest_pending_signal(
                            symbol,
                            bar,
                            signal,
                            signal_payload,
                        )

                        if force_flat_eod and index < len(bars) - 1:
                            next_day = str(bars[index + 1].get("us_time", "") or "")[:10]
                            if next_day != current_day:
                                self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "force_flat_eod")
                                pending_signal = None

            if not signal_conflict_emitted and preexisting_pending_signal and pending_signal and open_position is None:
                reverse_row = self._build_backtest_reverse_signal_row(
                    request,
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    daily_fields,
                    pending_signal,
                    target_state="pending_entry",
                    reverse_kind="indicator_conflict",
                    source="indicator",
                )
                reverse_key = self._build_backtest_reverse_key(reverse_row)
                if reverse_row and reverse_key not in reverse_index:
                    reverse_rows.append(reverse_row)
                    reverse_index.add(reverse_key)
                    pending_signal = self._apply_backtest_pending_reverse_action(
                        pending_signal,
                        reverse_row,
                        signal_index,
                    )

            if not signal_conflict_emitted and preexisting_open_position and open_position:
                reverse_row = self._build_backtest_reverse_signal_row(
                    request,
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    daily_fields,
                    open_position,
                    target_state="filled_position",
                    reverse_kind="indicator_conflict",
                    source="indicator",
                )
                reverse_key = self._build_backtest_reverse_key(reverse_row)
                if reverse_row and reverse_key not in reverse_index:
                    reverse_rows.append(reverse_row)
                    reverse_index.add(reverse_key)
                    open_position, reverse_trade = self._apply_backtest_position_reverse_action(
                        open_position,
                        reverse_row,
                        bar,
                        commission_per_share,
                        slippage_bps,
                    )
                    if reverse_trade:
                        trades.append(reverse_trade)
                        self._start_backtest_cooldown(
                            cooldown_state,
                            bar,
                            int(request.get("cooldown_bars_after_reverse", 3) or 0),
                            "cooldown_after_reverse_close",
                        )

            if open_position:
                open_position = self._maybe_apply_backtest_atr_stop(open_position, snapshot, request)

        if open_position:
            trades.append(self._close_position(open_position, bars[-1], commission_per_share, slippage_bps, "last_bar"))
        if pending_signal:
            self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "last_bar_no_entry")

        quality = {
            "symbol": symbol,
            "bar_count": len(bars),
            "gap_count": gap_count,
            "status": "ok",
            "first_bar_us": bars[0].get("us_time", "") if bars else "",
            "last_bar_us": bars[-1].get("us_time", "") if bars else "",
            "market_bars": market_bars,
            "selected_trade_day_count": len(allowed_trade_days or []),
        }
        if symbol_tv_parity is not None:
            self._finalize_symbol_tv_parity(symbol_tv_parity)
        return trades, quality, symbol_tv_parity, indicator_rows, signal_rows, reverse_rows, indicator_count

    def _should_compare_with_tv(self, request: dict) -> bool:
        return bool(request.get("compare_with_tv", True)) and str(request.get("source_environment") or "").strip().lower() != BACKTEST_ENVIRONMENT

    def _should_compare_tv_signals(self, request: dict) -> bool:
        return self._should_compare_with_tv(request) and bool(request.get("compare_tv_signals", False))

    def _should_persist_backtest_indicators(self, request: dict) -> bool:
        return bool(request.get("persist_backtest_indicators", False))

    def _clear_backtest_row_buffers(self, *buffers: Any, collect: bool = True):
        for buffer in buffers:
            if hasattr(buffer, "clear"):
                try:
                    buffer.clear()
                except Exception:
                    continue
        if collect:
            gc.collect()

    def _clear_portfolio_working_sets(
        self,
        states: dict[str, dict],
        bars_by_time: dict[int, list[tuple[str, int, dict]]],
        signal_index: dict,
        target_lookup: dict,
    ):
        for state in list(states.values()):
            self._clear_backtest_row_buffers(
                state.get("bars"),
                state.get("daily_close_lookup"),
                state.get("reverse_index"),
                collect=False,
            )
            state.pop("previous_bar", None)
            state.pop("open_position", None)
            state.pop("pending_signal", None)
        self._clear_backtest_row_buffers(states, bars_by_time, signal_index, target_lookup)

    def _parse_object(self, value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    def _load_daily_close_lookup(self, symbol: str, source_environment: str, date_from: str, date_to: str) -> list[dict]:
        if not self.pb:
            return []
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        lookback_start_ms = max(0, start_ms - (interval_to_ms("1d") * 40))
        rows = self.pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{symbol}" && interval = "1d" && environment = "{source_environment}" '
                f"&& bar_time_ms >= {lookback_start_ms} && bar_time_ms <= {end_ms}"
            ),
            sort="bar_time_ms",
            max_pages=120,
        )
        lookup = []
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            close = float(row.get("close", 0) or 0)
            if bar_ms <= 0 or close <= 0:
                continue
            lookup.append(
                {
                    "bar_time_ms": bar_ms,
                    "date": ms_to_et(bar_ms).strftime("%Y-%m-%d"),
                    "close": close,
                }
            )
        return lookup

    def _get_daily_change_fields_from_lookup(self, lookup: list[dict], current_close: float, bar_time_ms: int) -> dict:
        current_date = ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
        history = [row for row in lookup if str(row.get("date") or "") < current_date]
        prev_close = float(history[-1]["close"]) if len(history) >= 1 else 0.0
        prev_prev_close = float(history[-2]["close"]) if len(history) >= 2 else 0.0
        close_5 = float(history[-5]["close"]) if len(history) >= 5 else 0.0

        day_change_pct = ((current_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
        prev_close_change_pct = ((prev_close - prev_prev_close) / prev_prev_close * 100.0) if prev_prev_close > 0 else 0.0
        change_7d = ((current_close - close_5) / close_5 * 100.0) if close_5 > 0 else 0.0

        return {
            "day_change_pct": round(day_change_pct, 2),
            "prev_close_change_pct": round(prev_close_change_pct, 2),
            "change_7d": round(change_7d, 2),
        }

    def _build_tv_indicator_compare_payload(
        self,
        symbol: str,
        bar: dict,
        bar_index: int,
        snapshot: dict,
        source_environment: str,
        daily_fields: dict,
    ) -> dict:
        chart_tf = interval_to_chart_tf("5m")
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        extra = {
            **snapshot,
            **(daily_fields or {}),
            "symbol": symbol,
            "interval": chart_tf,
            "chart_tf": chart_tf,
            "bar_time_ms": bar_ms,
            "bar_index": int(bar_index or 0),
            "environment": source_environment,
            "session_type": bar.get("session_type", "regular"),
            "source": "ibkr_compute_backtest",
        }
        return {
            "symbol": symbol,
            "interval": chart_tf,
            "bar_time_ms": bar_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "extra": extra,
        }

    def _build_tv_signal_compare_payload(
        self,
        symbol: str,
        bar: dict,
        bar_index: int,
        signal: dict,
        source_environment: str,
        daily_fields: dict,
    ) -> dict:
        chart_tf = interval_to_chart_tf("5m")
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        signal_extra = dict(signal.get("extra") or {})
        signal_extra.update(
            {
                **(daily_fields or {}),
                "chart_tf": chart_tf,
                "bar_time_ms": bar_ms,
                "bar_index": int(bar_index or 0),
                "close": round(float(bar.get("close", 0) or 0), 2),
                "environment": source_environment,
                "source": "ibkr_compute_backtest",
            }
        )
        return {
            "symbol": symbol,
            "signal_id": build_signal_id(symbol, bar_ms, str(signal.get("signal", "") or "")),
            "signal": str(signal.get("signal", "") or ""),
            "direction": str(signal.get("direction", "") or ""),
            "entry": float(signal.get("entry", 0) or 0),
            "stop_loss": float(signal.get("stop_loss", 0) or 0),
            "take_profit": float(signal.get("take_profit", 0) or 0),
            "rr": signal.get("rr"),
            "shares": int(signal.get("shares", 0) or 0),
            "interval": chart_tf,
            "bar_time_ms": bar_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "reason": str(signal.get("reason", "") or ""),
            "extra": signal_extra,
        }

    def _build_backtest_indicator_row(self, request: dict, bar: dict, indicator_payload: dict, indicator_audit: dict | None = None) -> dict:
        indicator_extra = dict(indicator_payload.get("extra") or {})
        audit = indicator_audit or {}
        indicator_extra.update(
            {
                "backtest_source_environment": request.get("source_environment") or "",
                "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
                "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
                "tv_parity_status": audit.get("status") or "unverified",
                "tv_parity_field_count": int(audit.get("field_count", 0) or 0),
                "script_tag": str(request.get("strategy_tag") or ""),
                **build_runtime_timestamps(),
            }
        )
        if audit.get("mismatches"):
            indicator_extra["tv_parity_fields"] = audit.get("mismatches")

        return {
            "symbol": indicator_payload.get("symbol", ""),
            "exchange": str(bar.get("exchange", "") or "").upper(),
            "interval": indicator_payload.get("interval", interval_to_chart_tf("5m")),
            "script_tag": str(request.get("strategy_tag") or ""),
            "us_time": indicator_payload.get("us_time", ""),
            "cn_time": indicator_payload.get("cn_time", ""),
            "bar_time_ms": int(indicator_payload.get("bar_time_ms", 0) or 0),
            "bar_index": int(indicator_extra.get("bar_index", 0) or 0),
            "environment": BACKTEST_ENVIRONMENT,
            "extra": indicator_extra,
        }

    def _build_backtest_signal_row(self, request: dict, bar: dict, signal_payload: dict) -> dict:
        signal_extra = dict(signal_payload.get("extra") or {})
        signal_extra.update(
            {
                "backtest_source_environment": request.get("source_environment") or "",
                "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
                "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
                "script_tag": str(request.get("strategy_tag") or ""),
                **build_runtime_timestamps(),
            }
        )
        return {
            "symbol": signal_payload.get("symbol", ""),
            "direction": signal_payload.get("direction", ""),
            "signal": signal_payload.get("signal", ""),
            "entry": float(signal_payload.get("entry", 0) or 0),
            "stop_loss": float(signal_payload.get("stop_loss", 0) or 0),
            "take_profit": float(signal_payload.get("take_profit", 0) or 0),
            "rr": "" if signal_payload.get("rr") in (None, "") else str(signal_payload.get("rr")),
            "shares": int(signal_payload.get("shares", 0) or 0),
            "signal_id": signal_payload.get("signal_id", ""),
            "exchange": str(bar.get("exchange", "") or "").upper(),
            "interval": signal_payload.get("interval", interval_to_chart_tf("5m")),
            "reason": signal_payload.get("reason", ""),
            "us_time": signal_payload.get("us_time", ""),
            "cn_time": signal_payload.get("cn_time", ""),
            "date": str(signal_payload.get("us_time", "") or "")[:10],
            "bar_time_ms": int(signal_payload.get("bar_time_ms", 0) or 0),
            "bar_index": int(signal_extra.get("bar_index", 0) or 0),
            "script_tag": str(request.get("strategy_tag") or ""),
            "status": "generated",
            "environment": BACKTEST_ENVIRONMENT,
            "extra": signal_extra,
        }

    def _mark_backtest_signal_status(
        self,
        signal_index: dict,
        signal_id: Any,
        status: str,
        reason: str,
        extra_patch: dict | None = None,
    ):
        safe_signal_id = str(signal_id or "").strip()
        if not safe_signal_id:
            return
        row = signal_index.get(safe_signal_id)
        if not row:
            return
        row["status"] = status
        extra = self._parse_object(row.get("extra"))
        extra["signal_status_reason"] = reason
        if extra_patch:
            extra.update(extra_patch)
        row["extra"] = extra

    def _map_reverse_strength(self, score: float) -> str:
        safe_score = float(score or 0)
        if safe_score >= 6:
            return "strong"
        if safe_score >= 3:
            return "medium"
        return "weak"

    def _resolve_reverse_indicator_action(
        self,
        score: float,
        target_state: str,
        progress_ratio: float = 0.0,
    ) -> str:
        if target_state == "pending_entry":
            return "cancel"
        if score >= 6:
            return "close"
        if score >= 3:
            if float(progress_ratio or 0) >= 0.7:
                return "adjust_tp"
            return "adjust_sl"
        if score > 0:
            return "cancel"
        return ""

    def _analyze_backtest_reverse_snapshot(self, snapshot: dict, direction: str) -> dict:
        if not snapshot or direction not in {"long", "short"}:
            return {"score": 0.0, "triggered_signals": []}

        score = 0.0
        triggered_signals = []
        crsi = float(snapshot.get("crsi", 0) or 0)
        vwap_dist = float(snapshot.get("vwap_dist", 0) or 0)
        is_bear_signal = direction == "long"
        crsi_reverse_div = bool(snapshot.get("crsi_bear_div" if is_bear_signal else "crsi_bull_div"))
        obv_reverse_div = bool(snapshot.get("obv_bear_div" if is_bear_signal else "obv_bull_div"))
        fractal_reverse = bool(snapshot.get("fractal_bear" if is_bear_signal else "fractal_bull"))
        sd_channel_reverse = bool(snapshot.get("sd_upper" if is_bear_signal else "sd_lower"))
        ema_touch_reverse = bool(snapshot.get("ema_bear_touch" if is_bear_signal else "ema_bull_touch"))

        if (direction == "long" and crsi > 70) or (direction == "short" and crsi < 30):
            score += 2
            triggered_signals.append("cRSI超买" if direction == "long" else "cRSI超卖")
        if crsi_reverse_div or obv_reverse_div:
            score += 3
            triggered_signals.append("背离")
        if fractal_reverse or sd_channel_reverse:
            score += 2
            triggered_signals.append("分形/SD通道")
        if abs(vwap_dist) > 2 or ema_touch_reverse:
            score += 1
            triggered_signals.append("VWAP偏离/EMA触碰")

        deduped = []
        seen = set()
        for item in triggered_signals:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return {
            "score": score,
            "triggered_signals": deduped,
            "crsi": crsi,
            "obv_rsi": float(snapshot.get("obv_rsi", 0) or 0),
            "vwap_dist": vwap_dist,
            "close": float(snapshot.get("close", 0) or 0),
        }

    def _build_backtest_reverse_key(self, reverse_row: dict | None) -> str:
        if not reverse_row:
            return ""
        signal_id = str(reverse_row.get("signal_id", "") or "").strip()
        trade_group_id = str(reverse_row.get("trade_group_id", "") or "").strip()
        target_state = str(reverse_row.get("target_state", "") or "").strip()
        action_type = str(reverse_row.get("action_type", "") or "").strip()
        direction = str(reverse_row.get("direction", "") or "").strip()
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip()
        origin_signal_id = str(reverse_row.get("origin_signal_id", "") or "").strip()
        new_direction = str(self._parse_object(reverse_row.get("extra")).get("new_direction", "") or "").strip()
        return "|".join(
            [
                signal_id or trade_group_id or str(reverse_row.get("symbol", "") or "").strip().upper(),
                direction,
                target_state,
                action_type,
                reverse_kind,
                origin_signal_id,
                new_direction,
            ]
        )

    def _build_backtest_pending_signal(
        self,
        symbol: str,
        bar: dict,
        signal: dict,
        signal_payload: dict,
    ) -> dict:
        signal_id = signal_payload.get("signal_id", build_signal_id(symbol, int(bar["bar_time_ms"]), str(signal.get("signal", ""))))
        return {
            **signal,
            "symbol": symbol,
            "signal_id": signal_id,
            "signal_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "signal_us_time": str(bar.get("us_time", "") or ""),
            "signal_cn_time": str(bar.get("cn_time", "") or ""),
            "signal_close": float(bar.get("close", 0) or 0),
            "reason": str(signal.get("reason", "") or ""),
            "chart_tf": interval_to_chart_tf("5m"),
            "entry_price": float(signal.get("entry", 0) or 0),
            "target_price": float(signal.get("take_profit", 0) or 0),
            "stop_price": float(signal.get("stop_loss", 0) or 0),
            "pending_since_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "pending_since_us_time": str(bar.get("us_time", "") or ""),
        }

    def _check_pending_entry_fill(
        self,
        symbol: str,
        bar: dict,
        pending_signal: dict,
        commission_per_share: float,
        slippage_bps: float,
    ) -> Optional[dict]:
        direction = str(pending_signal.get("direction", "") or "").strip().lower()
        entry_price = float(
            pending_signal.get("entry_price", pending_signal.get("entry", 0)) or 0
        )
        if direction not in {"long", "short"} or entry_price <= 0:
            return None

        bar_open = float(bar.get("open", 0) or 0)
        bar_high = float(bar.get("high", 0) or 0)
        bar_low = float(bar.get("low", 0) or 0)
        raw_fill_price = 0.0
        if direction == "long":
            if bar_open > 0 and bar_open <= entry_price:
                raw_fill_price = bar_open
            elif bar_low <= entry_price <= max(bar_high, bar_open):
                raw_fill_price = entry_price
        else:
            if bar_open > 0 and bar_open >= entry_price:
                raw_fill_price = bar_open
            elif min(bar_low, bar_open) <= entry_price <= bar_high:
                raw_fill_price = entry_price

        if raw_fill_price <= 0:
            return None
        return self._open_position(
            symbol,
            bar,
            pending_signal,
            commission_per_share,
            slippage_bps,
            raw_fill_price=raw_fill_price,
        )

    def _calculate_position_progress(self, target: dict, current_price: float) -> dict:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        target_price = float(target.get("target_price", target.get("take_profit", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or target_price <= 0:
            return {"favorable_move": 0.0, "target_move": 0.0, "progress_ratio": 0.0}

        if direction == "long":
            favorable_move = current_price - entry_price
            target_move = target_price - entry_price
        else:
            favorable_move = entry_price - current_price
            target_move = entry_price - target_price
        progress_ratio = (favorable_move / target_move) if abs(target_move) > 1e-9 else 0.0
        return {
            "favorable_move": round(float(favorable_move or 0), 4),
            "target_move": round(float(target_move or 0), 4),
            "progress_ratio": round(float(progress_ratio or 0), 4),
        }

    def _compute_adjusted_stop_price(self, target: dict, current_price: float) -> float:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        stop_price = float(target.get("stop_price", target.get("stop_loss", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or stop_price <= 0:
            return 0.0
        price_buffer = max(0.01, current_price * 0.001)
        if direction == "long":
            favorable_move = max(0.0, current_price - entry_price)
            tightened = entry_price + (favorable_move * 0.35)
            candidate = max(stop_price, tightened)
            candidate = min(candidate, current_price - price_buffer)
        else:
            favorable_move = max(0.0, entry_price - current_price)
            tightened = entry_price - (favorable_move * 0.35)
            candidate = min(stop_price, tightened)
            candidate = max(candidate, current_price + price_buffer)
        return round(float(candidate or 0), 4)

    def _compute_adjusted_take_profit_price(self, target: dict, current_price: float) -> float:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        take_profit = float(target.get("target_price", target.get("take_profit", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or take_profit <= 0:
            return 0.0
        price_buffer = max(0.01, current_price * 0.001)
        if direction == "long":
            if take_profit <= current_price:
                return round(take_profit, 4)
            gap = take_profit - current_price
            candidate = current_price + max(price_buffer, gap * 0.35)
            candidate = min(take_profit, max(entry_price + price_buffer, candidate))
        else:
            if take_profit >= current_price:
                return round(take_profit, 4)
            gap = current_price - take_profit
            candidate = current_price - max(price_buffer, gap * 0.35)
            candidate = max(take_profit, min(entry_price - price_buffer, candidate))
        return round(float(candidate or 0), 4)

    def _build_backtest_reverse_price_patch(self, target: dict, action_type: str, current_price: float) -> dict:
        if action_type == "adjust_sl":
            old_sl = float(target.get("stop_price", target.get("stop_loss", 0)) or 0)
            new_sl = self._compute_adjusted_stop_price(target, current_price)
            if old_sl > 0 and new_sl > 0 and abs(new_sl - old_sl) >= 0.0001:
                return {
                    "old_sl": round(old_sl, 4),
                    "new_sl": round(new_sl, 4),
                    "stop_loss": round(new_sl, 4),
                }
        if action_type == "adjust_tp":
            old_tp = float(target.get("target_price", target.get("take_profit", 0)) or 0)
            new_tp = self._compute_adjusted_take_profit_price(target, current_price)
            if old_tp > 0 and new_tp > 0 and abs(new_tp - old_tp) >= 0.0001:
                return {
                    "old_tp": round(old_tp, 4),
                    "new_tp": round(new_tp, 4),
                    "take_profit": round(new_tp, 4),
                }
        return {}

    def _resolve_reverse_signal_conflict_action(self, target_state: str, progress_ratio: float) -> str:
        if target_state == "pending_entry":
            return "cancel"
        return "close"

    def _build_backtest_signal_conflict_analysis(
        self,
        target: dict,
        target_state: str,
        origin_signal_payload: dict,
        current_price: float,
    ) -> dict:
        progress = self._calculate_position_progress(target, current_price)
        action_type = self._resolve_reverse_signal_conflict_action(
            target_state,
            progress.get("progress_ratio", 0),
        )
        score_map = {
            "cancel": 2.0,
            "adjust_sl": 4.0,
            "adjust_tp": 5.0,
            "close": 7.0,
        }
        triggered_signals = ["信号反转"]
        price_patch = self._build_backtest_reverse_price_patch(target, action_type, current_price)
        return {
            "score": score_map.get(action_type, 2.0),
            "action_type": action_type,
            "triggered_signals": triggered_signals,
            "progress": progress,
            "price_patch": price_patch,
            "current_price": round(float(current_price or 0), 4),
            "new_direction": str(origin_signal_payload.get("direction", "") or "").strip().lower(),
        }

    def _apply_backtest_pending_reverse_action(
        self,
        pending_signal: dict | None,
        reverse_row: dict | None,
        signal_index: dict,
    ) -> dict | None:
        if not pending_signal or not reverse_row:
            return pending_signal
        action_type = str(reverse_row.get("action_type", "") or "").strip().lower()
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip().lower()
        if action_type == "cancel":
            self._mark_backtest_signal_status(
                signal_index,
                pending_signal.get("signal_id"),
                "dropped",
                f"reverse_{reverse_kind}_{action_type}",
                {
                    "reverse_action_type": action_type,
                    "reverse_kind": reverse_kind,
                },
            )
            return None
        return pending_signal

    def _apply_backtest_position_reverse_action(
        self,
        position: dict | None,
        reverse_row: dict | None,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
    ) -> tuple[dict | None, dict | None]:
        if not position or not reverse_row:
            return position, None
        action_type = str(reverse_row.get("action_type", "") or "").strip().lower()
        extra = self._parse_object(reverse_row.get("extra"))
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip().lower()
        if action_type == "close":
            trade = self._close_position(position, bar, commission_per_share, slippage_bps, f"reverse_{reverse_kind}_close")
            trade_extra = self._parse_object(trade.get("extra"))
            trade_extra.update(
                {
                    "reverse_action_type": action_type,
                    "reverse_kind": reverse_kind,
                    "origin_signal_id": str(reverse_row.get("origin_signal_id", "") or ""),
                }
            )
            trade["extra"] = trade_extra
            return None, trade
        if action_type == "adjust_sl":
            new_sl = float(extra.get("new_sl", 0) or 0)
            if new_sl > 0:
                position["stop_price"] = new_sl
        elif action_type == "adjust_tp":
            new_tp = float(extra.get("new_tp", 0) or 0)
            if new_tp > 0:
                position["target_price"] = new_tp
        return position, None

    def _build_backtest_reverse_signal_row(
        self,
        request: dict,
        symbol: str,
        bar: dict,
        bar_index: int,
        snapshot: dict,
        daily_fields: dict,
        target: dict,
        target_state: str = "filled_position",
        reverse_kind: str = "indicator_conflict",
        source: str = "indicator",
        origin_signal_payload: dict | None = None,
    ) -> dict | None:
        direction = str(target.get("direction", "") or "").strip().lower()
        if direction not in {"long", "short"}:
            return None
        bar_time_ms = int(bar.get("bar_time_ms", 0) or 0)
        if reverse_kind == "signal_conflict":
            if not origin_signal_payload:
                return None
            new_direction = str(origin_signal_payload.get("direction", "") or "").strip().lower()
            if not new_direction or new_direction == direction:
                return None
            analysis = self._build_backtest_signal_conflict_analysis(
                target,
                target_state,
                origin_signal_payload,
                float(snapshot.get("close", bar.get("close", 0)) or 0),
            )
        else:
            analysis = self._analyze_backtest_reverse_snapshot(snapshot, direction)
            if float(analysis.get("score", 0) or 0) <= 0 or not list(analysis.get("triggered_signals") or []):
                return None
            if target_state == "pending_entry":
                pending_since_ms = int(target.get("pending_since_bar_ms", target.get("signal_bar_ms", 0)) or 0)
                pending_age_bars = 0
                pending_same_day = False
                if pending_since_ms > 0:
                    pending_age_bars = max(
                        0,
                        int((bar_time_ms - pending_since_ms) / max(1, interval_to_ms("5m"))),
                    )
                    pending_same_day = (
                        ms_to_et(pending_since_ms).strftime("%Y-%m-%d")
                        == ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
                    )
                reverse_score = float(analysis.get("score", 0) or 0)
                if pending_age_bars < 2:
                    return None
                pending_late_session = False
                if pending_same_day:
                    pending_bar_et = ms_to_et(bar_time_ms)
                    pending_late_session = pending_bar_et.hour > 16 or (
                        pending_bar_et.hour == 16 and pending_bar_et.minute >= 45
                    )
                # Same-day pending entries should usually wait for a true opposite signal.
                # Only let indicator conflicts cancel them before the close when the reverse is exceptionally strong.
                if pending_same_day:
                    if reverse_score < 6 and not pending_late_session:
                        return None
                elif reverse_score < 3:
                    return None
                analysis["pending_age_bars"] = pending_age_bars
                analysis["pending_same_day"] = pending_same_day
                analysis["pending_late_session"] = pending_late_session
            progress = {}
            if target_state == "filled_position":
                progress = self._calculate_position_progress(
                    target,
                    float(analysis.get("close", snapshot.get("close", 0)) or 0),
                )
                analysis["progress"] = progress
            analysis["action_type"] = self._resolve_reverse_indicator_action(
                float(analysis.get("score", 0) or 0),
                target_state,
                float(progress.get("progress_ratio", 0) or 0),
            )
            if not analysis.get("action_type"):
                return None
            analysis["price_patch"] = self._build_backtest_reverse_price_patch(
                target,
                str(analysis.get("action_type", "") or ""),
                float(analysis.get("close", snapshot.get("close", 0)) or 0),
            )
            analysis["new_direction"] = ""

        score = float(analysis.get("score", 0) or 0)
        triggered_signals = list(analysis.get("triggered_signals") or [])
        action_type = str(analysis.get("action_type", "") or "").strip().lower()
        if score <= 0 or not triggered_signals or not action_type:
            return None

        signal_id = str(target.get("signal_id", "") or "").strip()
        trade_group_id = signal_id or f"{symbol}_{int(target.get('entry_bar_ms', target.get('signal_bar_ms', 0)) or 0)}"
        strength = self._map_reverse_strength(score)
        close_value = float(analysis.get("current_price", analysis.get("close", snapshot.get("close", 0))) or 0)
        entry_price = round(float(target.get("entry_price", target.get("entry", 0)) or 0), 4)
        take_profit = round(float(target.get("target_price", target.get("take_profit", 0)) or 0), 4)
        stop_loss = round(float(target.get("stop_price", target.get("stop_loss", 0)) or 0), 4)
        order_status = "Submitted" if target_state == "pending_entry" else "Filled"
        relation_status = "backtest_entry_pending" if target_state == "pending_entry" else "backtest_position_open"
        reverse_source = source or ("signal" if reverse_kind == "signal_conflict" else "indicator")
        price_patch = dict(analysis.get("price_patch") or {})
        new_direction = str(analysis.get("new_direction", "") or "").strip().lower()
        origin_signal_id = signal_id
        if origin_signal_payload is not None:
            origin_signal_id = str(origin_signal_payload.get("signal_id", "") or "").strip() or signal_id
        extra = {
            "environment": BACKTEST_ENVIRONMENT,
            "source_environment": request.get("source_environment") or "",
            "reverse_kind": reverse_kind,
            "target_state": target_state,
            "order_status": order_status,
            "relation_status": relation_status,
            "position_side": direction,
            "current_direction": direction,
            "signal_id": signal_id,
            "origin_signal_id": origin_signal_id,
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": trade_group_id,
            "order_unique_id": trade_group_id,
            "broker_order_id": "",
            "order_id": "",
            "entry_price": entry_price,
            "quantity": int(target.get("shares", 0) or 0),
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "crsi": round(float(analysis.get("crsi", 0) or 0), 4),
            "obv_rsi": round(float(analysis.get("obv_rsi", 0) or 0), 4),
            "vwap_dist": round(float(analysis.get("vwap_dist", 0) or 0), 4),
            "close": round(close_value, 4),
            "triggered_signals": triggered_signals,
            "chart_tf": interval_to_chart_tf("5m"),
            "bar_index": int(bar_index or 0),
            "bar_time_ms": bar_time_ms,
            "signal_bar_ms": int(target.get("signal_bar_ms", 0) or 0),
            "signal_us_time": str(target.get("signal_us_time", "") or ""),
            "signal_close": round(float(target.get("signal_close", 0) or 0), 4),
            "simulated_only": True,
            "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
            "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "script_tag": str(request.get("strategy_tag") or ""),
            **(daily_fields or {}),
            **build_runtime_timestamps(),
        }
        if new_direction:
            extra["new_direction"] = new_direction
        if price_patch:
            extra.update(price_patch)
        if analysis.get("pending_age_bars") is not None:
            extra["pending_age_bars"] = int(analysis.get("pending_age_bars", 0) or 0)
        if analysis.get("pending_same_day") is not None:
            extra["pending_same_day"] = bool(analysis.get("pending_same_day"))
        if analysis.get("pending_late_session") is not None:
            extra["pending_late_session"] = bool(analysis.get("pending_late_session"))
        progress = analysis.get("progress") or {}
        if progress:
            extra["progress_ratio"] = round(float(progress.get("progress_ratio", 0) or 0), 4)
            extra["favorable_move"] = round(float(progress.get("favorable_move", 0) or 0), 4)
            extra["target_move"] = round(float(progress.get("target_move", 0) or 0), 4)
        if reverse_kind == "signal_conflict":
            reason = f"signal_conflict({target_state}) -> new_signal={str(origin_signal_payload.get('signal', '') or '')}"
        else:
            reason = f"indicator_conflict({target_state}) -> {', '.join(triggered_signals)}"
        return {
            "symbol": symbol,
            "direction": direction,
            "reverse_kind": reverse_kind,
            "source": reverse_source,
            "target_state": target_state,
            "target_order_status": order_status,
            "strength": strength,
            "score": round(score, 4),
            "triggered_signals": triggered_signals,
            "action_type": action_type,
            "status": "generated",
            "reason": reason,
            "priority": max(1, min(10, int(round(score)) or 1)),
            "signal_id": signal_id,
            "origin_signal_id": origin_signal_id,
            "trade_group_id": trade_group_id,
            "date": str(bar.get("us_time", "") or "")[:10],
            "bar_time_ms": bar_time_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "environment": BACKTEST_ENVIRONMENT,
            "extra": extra,
        }

    def _count_values(self, rows: list[dict], field_name: str) -> dict:
        counts = {}
        for row in rows:
            key = str(row.get(field_name, "") or "").strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _count_signal_status_reasons(self, rows: list[dict]) -> dict:
        counts = {}
        for row in rows:
            extra = self._parse_object(row.get("extra"))
            reason = str(extra.get("signal_status_reason", "") or "").strip()
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def _build_daily_scan_match_diagnostics(self, target_rows: list[dict], signal_rows: list[dict]) -> dict:
        selected_by_date: dict[str, set[str]] = {}
        selected_pairs = set()
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or "")[:10]
            if not symbol or not date_text:
                continue
            selected_by_date.setdefault(date_text, set()).add(symbol)
            selected_pairs.add((date_text, symbol))

        def pct(numerator: int, denominator: int) -> float:
            return round((float(numerator) / float(denominator)) * 100.0, 4) if denominator else 0.0

        def bump(counts: dict, key: str, amount: int = 1):
            if not key:
                return
            counts[key] = int(counts.get(key, 0) or 0) + amount

        def top_counts(counts: dict, limit: int = 12) -> list[dict]:
            return [
                {"key": key, "count": int(value)}
                for key, value in sorted(
                    counts.items(),
                    key=lambda item: (-int(item[1] or 0), str(item[0])),
                )[: max(0, int(limit or 0))]
            ]

        daily_stats = {
            date_text: {
                "date": date_text,
                "selected_count": len(symbols),
                "signal_count": 0,
                "selected_day_signal_count": 0,
                "off_plan_signal_count": 0,
                "not_selected_signal_count": 0,
                "executed_signal_count": 0,
            }
            for date_text, symbols in selected_by_date.items()
        }
        symbol_stats: dict[str, dict] = {}
        selected_pairs_with_signal = set()
        selected_pairs_with_executed = set()
        generated_signal_count = len(signal_rows or [])
        selected_day_signal_count = 0
        selected_day_executed_signal_count = 0
        off_plan_signal_count = 0
        not_selected_signal_count = 0
        executed_signal_count = 0
        not_selected_symbol_counts: dict[str, int] = {}
        not_selected_date_counts: dict[str, int] = {}
        off_plan_symbol_counts: dict[str, int] = {}
        matched_symbol_counts: dict[str, int] = {}
        skipped_not_selected_samples = []

        for row in signal_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or row.get("us_time", "") or "")[:10]
            status = str(row.get("status", "") or "").strip().lower()
            extra = self._parse_object(row.get("extra"))
            reason = str(extra.get("signal_status_reason", "") or "").strip()
            if not symbol:
                continue

            symbol_stat = symbol_stats.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "signal_count": 0,
                    "selected_day_signal_count": 0,
                    "off_plan_signal_count": 0,
                    "not_selected_signal_count": 0,
                    "executed_signal_count": 0,
                },
            )
            symbol_stat["signal_count"] += 1
            if date_text:
                day_stat = daily_stats.setdefault(
                    date_text,
                    {
                        "date": date_text,
                        "selected_count": len(selected_by_date.get(date_text, set())),
                        "signal_count": 0,
                        "selected_day_signal_count": 0,
                        "off_plan_signal_count": 0,
                        "not_selected_signal_count": 0,
                        "executed_signal_count": 0,
                    },
                )
                day_stat["signal_count"] += 1
            else:
                day_stat = None

            selected_pair = bool(date_text and (date_text, symbol) in selected_pairs)
            if selected_pair:
                selected_day_signal_count += 1
                selected_pairs_with_signal.add((date_text, symbol))
                symbol_stat["selected_day_signal_count"] += 1
                bump(matched_symbol_counts, symbol)
                if day_stat is not None:
                    day_stat["selected_day_signal_count"] += 1
            else:
                off_plan_signal_count += 1
                symbol_stat["off_plan_signal_count"] += 1
                bump(off_plan_symbol_counts, symbol)
                if day_stat is not None:
                    day_stat["off_plan_signal_count"] += 1

            if reason == "symbol_not_selected_for_day":
                not_selected_signal_count += 1
                symbol_stat["not_selected_signal_count"] += 1
                bump(not_selected_symbol_counts, symbol)
                bump(not_selected_date_counts, date_text or "unknown")
                if day_stat is not None:
                    day_stat["not_selected_signal_count"] += 1
                if len(skipped_not_selected_samples) < 10:
                    skipped_not_selected_samples.append(
                        {
                            "symbol": symbol,
                            "date": date_text,
                            "us_time": str(row.get("us_time", "") or ""),
                            "direction": str(row.get("direction", "") or ""),
                            "status": status,
                            "reason": reason,
                        }
                    )

            if status == "executed":
                executed_signal_count += 1
                symbol_stat["executed_signal_count"] += 1
                if day_stat is not None:
                    day_stat["executed_signal_count"] += 1
                if selected_pair:
                    selected_day_executed_signal_count += 1
                    selected_pairs_with_executed.add((date_text, symbol))

        daily_summary = []
        for item in sorted(daily_stats.values(), key=lambda value: str(value.get("date", ""))):
            signal_count = int(item.get("signal_count", 0) or 0)
            selected_day_signals = int(item.get("selected_day_signal_count", 0) or 0)
            item["selected_day_signal_rate_pct"] = pct(selected_day_signals, signal_count)
            daily_summary.append(item)

        symbol_summary = list(symbol_stats.values())
        symbol_summary.sort(key=lambda item: (-int(item.get("signal_count", 0) or 0), str(item.get("symbol", ""))))

        return {
            "enabled": bool(selected_pairs),
            "target_date_count": len(selected_by_date),
            "selected_pair_count": len(selected_pairs),
            "selected_symbol_count": len({symbol for _, symbol in selected_pairs}),
            "generated_signal_count": generated_signal_count,
            "selected_day_signal_count": selected_day_signal_count,
            "off_plan_signal_count": off_plan_signal_count,
            "not_selected_signal_count": not_selected_signal_count,
            "executed_signal_count": executed_signal_count,
            "selected_day_executed_signal_count": selected_day_executed_signal_count,
            "selected_pair_with_signal_count": len(selected_pairs_with_signal),
            "selected_pair_with_executed_count": len(selected_pairs_with_executed),
            "selected_day_signal_rate_pct": pct(selected_day_signal_count, generated_signal_count),
            "not_selected_signal_rate_pct": pct(not_selected_signal_count, generated_signal_count),
            "selected_day_execution_rate_pct": pct(selected_day_executed_signal_count, selected_day_signal_count),
            "selected_pair_hit_rate_pct": pct(len(selected_pairs_with_signal), len(selected_pairs)),
            "selected_pair_execution_hit_rate_pct": pct(len(selected_pairs_with_executed), len(selected_pairs)),
            "top_not_selected_symbols": top_counts(not_selected_symbol_counts),
            "top_not_selected_dates": top_counts(not_selected_date_counts),
            "top_off_plan_symbols": top_counts(off_plan_symbol_counts),
            "top_matched_symbols": top_counts(matched_symbol_counts),
            "skipped_not_selected_samples": skipped_not_selected_samples,
            "symbol_summary": symbol_summary[:20],
            "daily_summary": daily_summary,
        }

    def _build_backtest_funnel_metrics(
        self,
        request: dict,
        target_rows: list[dict],
        signal_rows: list[dict],
        trades: list[dict],
    ) -> dict:
        def pct(numerator: int, denominator: int) -> float:
            return round((float(numerator) / float(denominator)) * 100.0, 4) if denominator else 0.0

        def row_date(row: dict, *field_names: str) -> str:
            for field_name in field_names:
                value = row.get(field_name)
                if value not in (None, ""):
                    text = str(value)
                    if len(text) >= 10:
                        return text[:10]
            bar_ms = int(row.get("bar_time_ms", row.get("entry_bar_ms", 0)) or 0)
            return ms_to_et(bar_ms).strftime("%Y-%m-%d") if bar_ms > 0 else ""

        target_pairs_by_date: dict[str, set[str]] = {}
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = row_date(row, "date", "us_time")
            if not symbol or not date_text:
                continue
            target_pairs_by_date.setdefault(date_text, set()).add(symbol)

        target_enabled = bool(target_pairs_by_date) and str(request.get("symbol_source") or "") == "daily_scan_replay"
        signal_counts_by_date: dict[str, int] = {}
        executed_signal_counts_by_date: dict[str, int] = {}
        signal_pairs_by_date: dict[str, set[str]] = {}
        for row in signal_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = row_date(row, "date", "us_time")
            if not symbol or not date_text:
                continue
            if target_enabled and symbol not in target_pairs_by_date.get(date_text, set()):
                continue
            signal_pairs_by_date.setdefault(date_text, set()).add(symbol)
            signal_counts_by_date[date_text] = int(signal_counts_by_date.get(date_text, 0) or 0) + 1
            if str(row.get("status", "") or "").strip().lower() == "executed":
                executed_signal_counts_by_date[date_text] = int(executed_signal_counts_by_date.get(date_text, 0) or 0) + 1

        open_counts_by_date: dict[str, int] = {}
        opened_pairs_by_date: dict[str, set[str]] = {}
        for row in trades or []:
            date_text = row_date(row, "entry_us_time")
            if not date_text:
                continue
            open_counts_by_date[date_text] = int(open_counts_by_date.get(date_text, 0) or 0) + 1
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if symbol:
                opened_pairs_by_date.setdefault(date_text, set()).add(symbol)

        all_dates = sorted(
            set(target_pairs_by_date.keys())
            | set(signal_counts_by_date.keys())
            | set(executed_signal_counts_by_date.keys())
            | set(open_counts_by_date.keys())
        )
        daily_funnel = []
        for date_text in all_dates:
            target_count = len(target_pairs_by_date.get(date_text, set())) if target_enabled else 0
            signal_count = int(signal_counts_by_date.get(date_text, 0) or 0)
            target_signal_count = (
                len(target_pairs_by_date.get(date_text, set()) & signal_pairs_by_date.get(date_text, set()))
                if target_enabled
                else 0
            )
            executed_signal_count = int(executed_signal_counts_by_date.get(date_text, 0) or 0)
            open_count = int(open_counts_by_date.get(date_text, 0) or 0)
            target_entry_count = (
                len(target_pairs_by_date.get(date_text, set()) & opened_pairs_by_date.get(date_text, set()))
                if target_enabled
                else 0
            )
            daily_funnel.append(
                {
                    "date": date_text,
                    "target_enabled": target_enabled,
                    "target_count": target_count,
                    "signal_count": signal_count,
                    "target_signal_count": target_signal_count,
                    "executed_signal_count": executed_signal_count,
                    "target_entry_count": target_entry_count,
                    "open_count": open_count,
                    "target_to_signal_rate": pct(target_signal_count, target_count) if target_enabled else 0.0,
                    "target_to_entry_rate": pct(target_entry_count, target_count) if target_enabled else 0.0,
                    "signal_to_entry_rate": pct(executed_signal_count, signal_count),
                }
            )

        target_count = sum(len(symbols) for symbols in target_pairs_by_date.values()) if target_enabled else 0
        target_signal_count = (
            sum(
                len(target_pairs_by_date.get(date_text, set()) & signal_pairs_by_date.get(date_text, set()))
                for date_text in target_pairs_by_date.keys()
            )
            if target_enabled
            else 0
        )
        target_entry_count = (
            sum(
                len(target_pairs_by_date.get(date_text, set()) & opened_pairs_by_date.get(date_text, set()))
                for date_text in target_pairs_by_date.keys()
            )
            if target_enabled
            else 0
        )
        signal_count = sum(int(value or 0) for value in signal_counts_by_date.values())
        executed_signal_count = sum(int(value or 0) for value in executed_signal_counts_by_date.values())
        open_count = sum(int(value or 0) for value in open_counts_by_date.values())
        return {
            "daily_open_counts": [
                {"date": date_text, "open_count": int(open_counts_by_date.get(date_text, 0) or 0)}
                for date_text in sorted(open_counts_by_date.keys())
            ],
            "daily_funnel": daily_funnel,
            "target_funnel_enabled": target_enabled,
            "funnel_signal_count": signal_count,
            "funnel_executed_signal_count": executed_signal_count,
            "target_signal_count": target_signal_count,
            "target_to_signal_rate": pct(target_signal_count, target_count) if target_enabled else 0.0,
            "target_entry_count": target_entry_count,
            "open_count": open_count,
            "target_to_entry_rate": pct(target_entry_count, target_count) if target_enabled else 0.0,
            "signal_to_entry_rate": pct(executed_signal_count, signal_count),
        }

    def _build_backtest_reverse_samples(self, rows: list[dict], limit: int = 8) -> list[dict]:
        samples = []
        for row in rows[: max(0, int(limit or 0))]:
            samples.append(
                {
                    "symbol": str(row.get("symbol", "") or ""),
                    "direction": str(row.get("direction", "") or ""),
                    "action_type": str(row.get("action_type", "") or ""),
                    "score": round(float(row.get("score", 0) or 0), 4),
                    "strength": str(row.get("strength", "") or ""),
                    "status": str(row.get("status", "") or ""),
                    "target_state": str(row.get("target_state", "") or ""),
                    "us_time": str(row.get("us_time", "") or ""),
                    "triggered_signals": list(row.get("triggered_signals") or []),
                    "signal_id": str(row.get("signal_id", "") or ""),
                }
            )
        return samples

    def _normalize_tv_indicator_row(self, row: dict) -> dict:
        extra = self._parse_object(row.get("extra"))
        return {
            "symbol": str(row.get("symbol", "") or "").upper(),
            "interval": str(row.get("interval", "") or ""),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": str(row.get("us_time", "") or ""),
            "cn_time": str(row.get("cn_time", "") or ""),
            "extra": extra,
        }

    def _normalize_tv_signal_row(self, row: dict) -> dict:
        extra = self._parse_object(row.get("extra"))
        return {
            "symbol": str(row.get("symbol", "") or "").upper(),
            "signal_id": str(row.get("signal_id", "") or ""),
            "signal": str(row.get("signal", "") or ""),
            "direction": str(row.get("direction", "") or ""),
            "entry": float(row.get("entry", 0) or 0),
            "stop_loss": float(row.get("stop_loss", 0) or 0),
            "take_profit": float(row.get("take_profit", 0) or 0),
            "rr": self._parse_rr_value(
                row.get("rr"),
                entry=row.get("entry"),
                stop_loss=row.get("stop_loss"),
                take_profit=row.get("take_profit"),
            ),
            "shares": int(row.get("shares", 0) or 0),
            "interval": str(row.get("interval", "") or ""),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": str(row.get("us_time", "") or ""),
            "cn_time": str(row.get("cn_time", "") or ""),
            "reason": str(row.get("reason", "") or ""),
            "extra": extra,
        }

    def _load_tv_reference(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        include_signals: bool = True,
    ) -> dict:
        reference = {"indicator_map": {}, "signal_map": {}, "signal_bar_map": {}, "error": ""}
        if not self.pb:
            reference["error"] = "pb_client_unavailable"
            return reference
        try:
            start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
            chart_tf = interval_to_chart_tf("5m")
            indicator_rows = self.pb.get_all_records(
                TV_INDICATOR_COLLECTION,
                filter=(
                    f'symbol = "{symbol}" && interval = "{chart_tf}" && environment = "{source_environment}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=DEFAULT_MAX_PAGES,
            )
            for row in indicator_rows:
                normalized = self._normalize_tv_indicator_row(row)
                if normalized["bar_time_ms"] > 0:
                    reference["indicator_map"][normalized["bar_time_ms"]] = normalized
            if include_signals:
                signal_rows = self.pb.get_all_records(
                    TV_SIGNAL_COLLECTION,
                    filter=(
                        f'symbol = "{symbol}" && interval = "{chart_tf}" && environment = "{source_environment}" '
                        f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                    ),
                    sort="bar_time_ms",
                    max_pages=400,
                )
                for row in signal_rows:
                    normalized = self._normalize_tv_signal_row(row)
                    signal_id = normalized["signal_id"]
                    if signal_id:
                        reference["signal_map"][signal_id] = normalized
                    bar_ms = normalized["bar_time_ms"]
                    if bar_ms > 0:
                        reference["signal_bar_map"].setdefault(bar_ms, []).append(normalized)
        except Exception as exc:
            reference["error"] = str(exc)[:300]
        return reference

    def _init_symbol_tv_parity(self, symbol: str, reference: dict, compare_tv_signals: bool = True) -> dict:
        return {
            "symbol": symbol,
            "reference_error": str((reference or {}).get("error") or ""),
            "signal_compare_enabled": bool(compare_tv_signals),
            "indicators": {
                "generated_count": 0,
                "tv_count": len((reference or {}).get("indicator_map", {})),
                "matched_count": 0,
                "mismatch_count": 0,
                "missing_in_tv_count": 0,
                "missing_in_backtest_count": 0,
                "field_mismatch_count": 0,
                "mismatch_samples": [],
                "missing_in_tv_samples": [],
                "missing_in_backtest_samples": [],
                "_seen_keys": set(),
            },
            "signals": {
                "generated_count": 0,
                "tv_count": len((reference or {}).get("signal_map", {})) if compare_tv_signals else 0,
                "matched_count": 0,
                "mismatch_count": 0,
                "missing_in_tv_count": 0,
                "missing_in_backtest_count": 0,
                "field_mismatch_count": 0,
                "mismatch_samples": [],
                "missing_in_tv_samples": [],
                "missing_in_backtest_samples": [],
                "_seen_keys": set(),
            },
            "_tv_indicator_map": dict((reference or {}).get("indicator_map", {})),
            "_tv_signal_map": dict((reference or {}).get("signal_map", {})),
            "_tv_signal_bar_map": dict((reference or {}).get("signal_bar_map", {})),
        }

    def _append_tv_sample(self, bucket: list, payload: dict):
        if len(bucket) < TV_COMPARE_SAMPLE_LIMIT:
            bucket.append(payload)

    def _normalize_compare_time(self, value: Any) -> str:
        text = str(value or "").strip()
        return text[:16] if len(text) >= 16 else text

    def _coerce_numeric(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None

    def _parse_rr_value(self, raw_value: Any, entry: Any = 0, stop_loss: Any = 0, take_profit: Any = 0) -> float | None:
        numeric = self._coerce_numeric(raw_value)
        if numeric is not None:
            return numeric

        text = str(raw_value or "").strip().replace("：", ":")
        if text:
            parts = text.split(":")
            if len(parts) == 2:
                left = self._coerce_numeric(parts[0])
                right = self._coerce_numeric(parts[1])
                if left is not None and right not in (None, 0):
                    return left / right

        entry_price = self._coerce_numeric(entry)
        stop_price = self._coerce_numeric(stop_loss)
        take_price = self._coerce_numeric(take_profit)
        if entry_price is None or stop_price is None or take_price is None:
            return None
        risk = abs(entry_price - stop_price)
        reward = abs(take_price - entry_price)
        if risk <= 0:
            return None
        return reward / risk

    def _compare_values(self, field: str, left: Any, right: Any) -> tuple[bool, Any, Any]:
        if left in (None, "") and right in (None, ""):
            return True, left, right
        if field in {"us_time", "cn_time"}:
            left_norm = self._normalize_compare_time(left)
            right_norm = self._normalize_compare_time(right)
            return left_norm == right_norm, left_norm, right_norm
        if field == "rr":
            left_norm = self._parse_rr_value(left)
            right_norm = self._parse_rr_value(right)
            if left_norm is None and right_norm is None:
                return True, left_norm, right_norm
            return round(float(left_norm or 0), 4) == round(float(right_norm or 0), 4), left_norm, right_norm
        if isinstance(left, bool) or isinstance(right, bool):
            left_norm = bool(left)
            right_norm = bool(right)
            return left_norm == right_norm, left_norm, right_norm

        left_num = self._coerce_numeric(left)
        right_num = self._coerce_numeric(right)
        if left_num is not None and right_num is not None:
            if field in {"bar_time_ms", "shares", "volume"}:
                left_norm = int(round(left_num))
                right_norm = int(round(right_num))
            else:
                precision = 2 if field in {"entry", "stop_loss", "take_profit", "close", "atr_pct", "day_change_pct", "prev_close_change_pct", "change_7d", "sl_dist_pct"} else 4
                left_norm = round(left_num, precision)
                right_norm = round(right_num, precision)
            return left_norm == right_norm, left_norm, right_norm

        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        return left_text == right_text, left_text, right_text

    def _compare_payload_fields(self, generated: dict, reference: dict, core_fields: tuple[str, ...], extra_fields: tuple[str, ...]) -> list[dict]:
        mismatches = []
        generated_extra = self._parse_object(generated.get("extra"))
        reference_extra = self._parse_object(reference.get("extra"))

        for field in core_fields:
            matched, left_norm, right_norm = self._compare_values(field, generated.get(field), reference.get(field))
            if not matched:
                mismatches.append({"field": field, "generated": left_norm, "tv": right_norm})

        for field in extra_fields:
            matched, left_norm, right_norm = self._compare_values(field, generated_extra.get(field), reference_extra.get(field))
            if not matched:
                mismatches.append({"field": f"extra.{field}", "generated": left_norm, "tv": right_norm})
        return mismatches

    def _compare_generated_indicator(self, report: dict, generated: dict) -> dict:
        section = report["indicators"]
        key = int(generated.get("bar_time_ms", 0) or 0)
        section["generated_count"] += 1
        section["_seen_keys"].add(key)

        reference = report["_tv_indicator_map"].get(key)
        if not reference:
            section["missing_in_tv_count"] += 1
            self._append_tv_sample(
                section["missing_in_tv_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": generated.get("us_time", ""),
                },
            )
            return {"status": "missing_in_tv", "field_count": 0, "mismatches": []}

        mismatches = self._compare_payload_fields(
            generated,
            reference,
            ("bar_time_ms", "us_time", "cn_time", "interval"),
            TV_INDICATOR_EXTRA_FIELDS,
        )
        if mismatches:
            section["mismatch_count"] += 1
            section["field_mismatch_count"] += len(mismatches)
            self._append_tv_sample(
                section["mismatch_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": generated.get("us_time", ""),
                    "fields": mismatches[:10],
                },
            )
            return {"status": "mismatch", "field_count": len(mismatches), "mismatches": mismatches[:10]}

        section["matched_count"] += 1
        return {"status": "matched", "field_count": 0, "mismatches": []}

    def _compare_generated_signal(self, report: dict, generated: dict):
        if not bool(report.get("signal_compare_enabled", True)):
            return
        section = report["signals"]
        signal_id = str(generated.get("signal_id", "") or "")
        bar_time_ms = int(generated.get("bar_time_ms", 0) or 0)
        section["generated_count"] += 1
        if signal_id:
            section["_seen_keys"].add(signal_id)

        reference = report["_tv_signal_map"].get(signal_id)
        if not reference:
            section["missing_in_tv_count"] += 1
            self._append_tv_sample(
                section["missing_in_tv_samples"],
                {
                    "symbol": report["symbol"],
                    "signal_id": signal_id,
                    "bar_time_ms": bar_time_ms,
                    "us_time": generated.get("us_time", ""),
                    "tv_candidates_at_bar": [item.get("signal_id", "") for item in report["_tv_signal_bar_map"].get(bar_time_ms, [])][:5],
                },
            )
            return

        mismatches = self._compare_payload_fields(
            generated,
            reference,
            TV_SIGNAL_CORE_FIELDS,
            TV_SIGNAL_EXTRA_FIELDS,
        )
        if mismatches:
            section["mismatch_count"] += 1
            section["field_mismatch_count"] += len(mismatches)
            self._append_tv_sample(
                section["mismatch_samples"],
                {
                    "symbol": report["symbol"],
                    "signal_id": signal_id,
                    "bar_time_ms": bar_time_ms,
                    "fields": mismatches[:10],
                },
            )
            return

        section["matched_count"] += 1

    def _finalize_symbol_tv_parity(self, report: dict):
        for key, reference in report["_tv_indicator_map"].items():
            if key in report["indicators"]["_seen_keys"]:
                continue
            report["indicators"]["missing_in_backtest_count"] += 1
            self._append_tv_sample(
                report["indicators"]["missing_in_backtest_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": reference.get("us_time", ""),
                },
            )

        if bool(report.get("signal_compare_enabled", True)):
            for key, reference in report["_tv_signal_map"].items():
                if key in report["signals"]["_seen_keys"]:
                    continue
                report["signals"]["missing_in_backtest_count"] += 1
                self._append_tv_sample(
                    report["signals"]["missing_in_backtest_samples"],
                    {
                        "symbol": report["symbol"],
                        "signal_id": key,
                        "bar_time_ms": reference.get("bar_time_ms", 0),
                        "us_time": reference.get("us_time", ""),
                    },
                )

        for section_name in ("indicators", "signals"):
            section = report[section_name]
            denominator = max(section["generated_count"], section["tv_count"], 1)
            section["match_rate"] = round((section["matched_count"] / denominator) * 100.0, 2) if denominator else 100.0
            section.pop("_seen_keys", None)

        if not bool(report.get("signal_compare_enabled", True)):
            report["signals"].update(
                {
                    "generated_count": 0,
                    "tv_count": 0,
                    "matched_count": 0,
                    "mismatch_count": 0,
                    "missing_in_tv_count": 0,
                    "missing_in_backtest_count": 0,
                    "field_mismatch_count": 0,
                    "mismatch_samples": [],
                    "missing_in_tv_samples": [],
                    "missing_in_backtest_samples": [],
                    "match_rate": 0.0,
                    "status": "disabled",
                }
            )

        if report["reference_error"]:
            report["status"] = "error"
        elif report["indicators"]["tv_count"] == 0 and (not bool(report.get("signal_compare_enabled", True)) or report["signals"]["tv_count"] == 0):
            report["status"] = "no_reference"
        elif bool(report.get("signal_compare_enabled", True)) and (
            report["signals"]["mismatch_count"]
            or report["signals"]["missing_in_tv_count"]
            or report["signals"]["missing_in_backtest_count"]
        ):
            report["status"] = "fail"
        elif report["indicators"]["mismatch_count"] or report["indicators"]["missing_in_tv_count"] or report["indicators"]["missing_in_backtest_count"]:
            report["status"] = "warn"
        else:
            report["status"] = "pass"

        report.pop("_tv_indicator_map", None)
        report.pop("_tv_signal_map", None)
        report.pop("_tv_signal_bar_map", None)

    def _finalize_tv_parity_report(self, request: dict, symbol_reports: list[dict]) -> dict:
        if not self._should_compare_with_tv(request):
            return {
                "enabled": False,
                "status": "disabled",
                "summary": {"status": "disabled", "enabled": False},
                "symbols": [],
            }

        reports = [item for item in symbol_reports if item]
        reports.sort(key=lambda item: str(item.get("symbol", "")))

        indicator_totals = {
            "generated_count": 0,
            "tv_count": 0,
            "matched_count": 0,
            "mismatch_count": 0,
            "missing_in_tv_count": 0,
            "missing_in_backtest_count": 0,
            "field_mismatch_count": 0,
        }
        signal_totals = {
            "generated_count": 0,
            "tv_count": 0,
            "matched_count": 0,
            "mismatch_count": 0,
            "missing_in_tv_count": 0,
            "missing_in_backtest_count": 0,
            "field_mismatch_count": 0,
        }
        compare_tv_signals = self._should_compare_tv_signals(request)

        for report in reports:
            for key in indicator_totals:
                indicator_totals[key] += int(report.get("indicators", {}).get(key, 0) or 0)
                signal_totals[key] += int(report.get("signals", {}).get(key, 0) or 0)

        indicator_denominator = max(indicator_totals["generated_count"], indicator_totals["tv_count"], 1)
        signal_denominator = max(signal_totals["generated_count"], signal_totals["tv_count"], 1)
        indicator_match_rate = round((indicator_totals["matched_count"] / indicator_denominator) * 100.0, 2) if indicator_denominator else 100.0
        signal_match_rate = round((signal_totals["matched_count"] / signal_denominator) * 100.0, 2) if signal_denominator else 100.0
        symbols_with_reference = len([item for item in reports if item.get("status") not in {"no_reference", "error"}])

        if not reports or (indicator_totals["tv_count"] == 0 and (not compare_tv_signals or signal_totals["tv_count"] == 0)):
            status = "no_reference"
        elif any(item.get("status") == "error" for item in reports):
            status = "error"
        elif compare_tv_signals and (
            signal_totals["mismatch_count"]
            or signal_totals["missing_in_tv_count"]
            or signal_totals["missing_in_backtest_count"]
        ):
            status = "fail"
        elif indicator_totals["mismatch_count"] or indicator_totals["missing_in_tv_count"] or indicator_totals["missing_in_backtest_count"]:
            status = "warn"
        else:
            status = "pass"

        summary = {
            "enabled": True,
            "status": status,
            "symbol_count": len(reports),
            "symbols_with_reference": symbols_with_reference,
            "signal_compare_enabled": compare_tv_signals,
            "indicator_match_rate": indicator_match_rate,
            "signal_match_rate": signal_match_rate,
            "indicator_generated_count": indicator_totals["generated_count"],
            "indicator_tv_count": indicator_totals["tv_count"],
            "indicator_matched_count": indicator_totals["matched_count"],
            "indicator_mismatch_count": indicator_totals["mismatch_count"],
            "indicator_missing_in_tv_count": indicator_totals["missing_in_tv_count"],
            "indicator_missing_in_backtest_count": indicator_totals["missing_in_backtest_count"],
            "signal_generated_count": signal_totals["generated_count"],
            "signal_tv_count": signal_totals["tv_count"],
            "signal_matched_count": signal_totals["matched_count"],
            "signal_mismatch_count": signal_totals["mismatch_count"],
            "signal_missing_in_tv_count": signal_totals["missing_in_tv_count"],
            "signal_missing_in_backtest_count": signal_totals["missing_in_backtest_count"],
        }
        return {
            "enabled": True,
            "status": status,
            "signal_compare_enabled": compare_tv_signals,
            "source_environment": request.get("source_environment") or "",
            "session_mode": request.get("session_mode") or "",
            "interval": interval_to_chart_tf("5m"),
            "symbols": reports,
            "summary": summary,
        }

    def _empty_capture_summary(self, collection: str, run_id: str, status: str = "empty") -> dict:
        return {
            "collection": collection,
            "run_id": run_id,
            "attempted_count": 0,
            "saved_count": 0,
            "error_count": 0,
            "status": "disabled" if not self.pb else status,
            "errors": [],
        }

    def _append_capture_error(self, summary: dict, payload: dict, exc: Exception | str, fields: tuple[str, ...]):
        summary["error_count"] += 1
        summary["status"] = "partial"
        if len(summary["errors"]) >= TV_COMPARE_SAMPLE_LIMIT:
            return
        sample = {field: payload.get(field, "") for field in fields}
        if "bar_time_ms" in sample:
            sample["bar_time_ms"] = int(sample.get("bar_time_ms", 0) or 0)
        sample["error"] = str(exc)[:300]
        summary["errors"].append(sample)

    def _prepare_backtest_capture_payload(self, run_id: str, row: dict) -> dict:
        payload = dict(row or {})
        extra = self._parse_object(payload.get("extra"))
        extra["backtest_run_id"] = run_id
        payload["run_id"] = run_id
        payload["extra"] = extra
        return payload

    def _persist_backtest_collection_rows(
        self,
        collection: str,
        run_id: str,
        rows: list[dict],
        error_fields: tuple[str, ...],
    ) -> dict:
        summary = self._empty_capture_summary(collection, run_id)
        summary["attempted_count"] = len(rows)
        if not self.pb or not run_id:
            return summary
        if not rows:
            return summary

        summary["status"] = "ok"
        payloads = []
        for row in rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payloads.append(self._prepare_backtest_capture_payload(run_id, row))

        bulk_create = getattr(self.pb, "create_records", None)
        chunk_size = 50
        for index in range(0, len(payloads), chunk_size):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            chunk = payloads[index : index + chunk_size]
            if callable(bulk_create):
                try:
                    bulk_create(collection, chunk, timeout=60, batch_size=chunk_size)
                    summary["saved_count"] += len(chunk)
                    continue
                except Exception as exc:
                    for payload in chunk:
                        self._append_capture_error(summary, payload, exc, error_fields)
                    continue

            for payload in chunk:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                try:
                    self.pb.create_record(collection, payload)
                    summary["saved_count"] += 1
                except Exception as exc:
                    self._append_capture_error(summary, payload, exc, error_fields)

        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _persist_backtest_indicators(self, run_id: str, indicator_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_INDICATOR_COLLECTION,
            run_id,
            indicator_rows,
            ("bar_time_ms", "symbol"),
        )

    def _persist_backtest_signals(self, run_id: str, signal_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_SIGNAL_COLLECTION,
            run_id,
            signal_rows,
            ("bar_time_ms", "symbol", "signal_id"),
        )

    def _persist_backtest_targets(self, run_id: str, target_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_TARGET_COLLECTION,
            run_id,
            target_rows,
            ("date", "symbol"),
        )

    def _persist_backtest_reverse_signals(self, run_id: str, reverse_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_REVERSE_SIGNAL_COLLECTION,
            run_id,
            reverse_rows,
            ("bar_time_ms", "symbol", "action_type", "signal_id"),
        )

    def _create_records_or_raise(self, collection: str, payloads: list[dict], timeout: int = 60, batch_size: int = 50):
        if not payloads:
            return
        bulk_create = getattr(self.pb, "create_records", None)
        if callable(bulk_create):
            bulk_create(collection, payloads, timeout=timeout, batch_size=batch_size)
            return
        for payload in payloads:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            self.pb.create_record(collection, payload)

    def _open_position(
        self,
        symbol: str,
        bar: dict,
        signal: dict,
        commission_per_share: float,
        slippage_bps: float,
        raw_fill_price: float | None = None,
    ) -> dict:
        direction = str(signal.get("direction", "") or "")
        shares = max(0, int(signal.get("shares", 0) or 0))
        entry_reference = float(raw_fill_price if raw_fill_price is not None else bar.get("open", 0) or 0)
        fill_price = self._apply_slippage(entry_reference, direction, is_entry=True, bps=slippage_bps)
        return {
            "symbol": symbol,
            "direction": direction,
            "signal": str(signal.get("signal", "") or ""),
            "signal_id": str(signal.get("signal_id", "") or ""),
            "reason": str(signal.get("reason", "") or ""),
            "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "entry_us_time": str(bar.get("us_time", "") or ""),
            "entry_cn_time": str(bar.get("cn_time", "") or ""),
            "entry_price": round(fill_price, 4),
            "entry_limit_price": float(signal.get("entry_price", signal.get("entry", 0)) or 0),
            "target_price": float(signal.get("target_price", signal.get("take_profit", 0)) or 0),
            "stop_price": float(signal.get("stop_price", signal.get("stop_loss", 0)) or 0),
            "original_stop_loss": float(signal.get("stop_price", signal.get("stop_loss", 0)) or 0),
            "last_stop_atr": float((signal.get("extra") or {}).get("atr", 0) or 0),
            "atr_stop_adjust_count": 0,
            "mfe": 0.0,
            "mae": 0.0,
            "shares": shares,
            "bars_held": 0,
            "entry_commission": round(shares * commission_per_share, 4),
            "signal_bar_ms": int(signal.get("signal_bar_ms", 0) or 0),
            "signal_us_time": str(signal.get("signal_us_time", "") or ""),
            "signal_close": float(signal.get("signal_close", 0) or 0),
        }

    def _check_exit(self, position: dict, bar: dict, commission_per_share: float, slippage_bps: float) -> Optional[dict]:
        direction = str(position.get("direction", "") or "")
        stop_price = float(position.get("stop_price", 0) or 0)
        target_price = float(position.get("target_price", 0) or 0)
        high = float(bar.get("high", 0) or 0)
        low = float(bar.get("low", 0) or 0)
        close = float(bar.get("close", 0) or 0)
        shares = int(position.get("shares", 0) or 0)
        position["bars_held"] = int(position.get("bars_held", 0) or 0) + 1
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price > 0:
            if direction == "long":
                favorable = max(0.0, high - entry_price)
                adverse = max(0.0, entry_price - low)
            else:
                favorable = max(0.0, entry_price - low)
                adverse = max(0.0, high - entry_price)
            position["mfe"] = max(float(position.get("mfe", 0) or 0), favorable)
            position["mae"] = max(float(position.get("mae", 0) or 0), adverse)

        exit_reason = ""
        raw_exit_price = 0.0
        if direction == "long":
            if stop_price > 0 and low <= stop_price:
                raw_exit_price = stop_price
                exit_reason = "stop_loss"
            elif target_price > 0 and high >= target_price:
                raw_exit_price = target_price
                exit_reason = "take_profit"
        else:
            if stop_price > 0 and high >= stop_price:
                raw_exit_price = stop_price
                exit_reason = "stop_loss"
            elif target_price > 0 and low <= target_price:
                raw_exit_price = target_price
                exit_reason = "take_profit"

        if not exit_reason:
            return None

        exit_price = self._apply_slippage(raw_exit_price, direction, is_entry=False, bps=slippage_bps)
        exit_commission = round(shares * commission_per_share, 4)
        pnl = self._calc_pnl(direction, float(position["entry_price"]), exit_price, shares) - float(position["entry_commission"]) - exit_commission
        pnl_pct = 0.0
        position_cost = max(0.01, float(position["entry_price"]) * shares)
        pnl_pct = (pnl / position_cost) * 100.0
        return {
            "symbol": position["symbol"],
            "direction": direction,
            "signal": position.get("signal", ""),
            "signal_id": position.get("signal_id", ""),
            "reason": position.get("reason", ""),
            "entry_bar_ms": int(position["entry_bar_ms"]),
            "entry_us_time": position["entry_us_time"],
            "entry_cn_time": position["entry_cn_time"],
            "entry_price": round(float(position["entry_price"]), 4),
            "exit_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "exit_us_time": str(bar.get("us_time", "") or ""),
            "exit_cn_time": str(bar.get("cn_time", "") or ""),
            "exit_price": round(exit_price, 4),
            "shares": shares,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "bars_held": int(position.get("bars_held", 0) or 0),
            "exit_reason": exit_reason,
            "session_type": str(bar.get("session_type", "") or "regular"),
            "extra": {
                "stop_price": stop_price,
                "target_price": target_price,
                "mfe": round(float(position.get("mfe", 0) or 0), 4),
                "mae": round(float(position.get("mae", 0) or 0), 4),
                "atr_stop_adjust_count": int(position.get("atr_stop_adjust_count", 0) or 0),
                "last_atr_stop_adjust": position.get("last_atr_stop_adjust") or {},
                "signal_bar_ms": int(position.get("signal_bar_ms", 0) or 0),
                "signal_us_time": position.get("signal_us_time", ""),
                "signal_close": float(position.get("signal_close", 0) or 0),
                **build_runtime_timestamps(),
            },
        }

    def _close_position(self, position: dict, bar: dict, commission_per_share: float, slippage_bps: float, reason: str) -> dict:
        direction = str(position.get("direction", "") or "")
        shares = int(position.get("shares", 0) or 0)
        raw_exit_price = float(bar.get("close", 0) or 0)
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price > 0 and raw_exit_price > 0:
            favorable = max(0.0, raw_exit_price - entry_price) if direction == "long" else max(0.0, entry_price - raw_exit_price)
            adverse = max(0.0, entry_price - raw_exit_price) if direction == "long" else max(0.0, raw_exit_price - entry_price)
            position["mfe"] = max(float(position.get("mfe", 0) or 0), favorable)
            position["mae"] = max(float(position.get("mae", 0) or 0), adverse)
        exit_price = self._apply_slippage(raw_exit_price, direction, is_entry=False, bps=slippage_bps)
        exit_commission = round(shares * commission_per_share, 4)
        pnl = self._calc_pnl(direction, float(position["entry_price"]), exit_price, shares) - float(position["entry_commission"]) - exit_commission
        position_cost = max(0.01, float(position["entry_price"]) * shares)
        pnl_pct = (pnl / position_cost) * 100.0
        return {
            "symbol": position["symbol"],
            "direction": direction,
            "signal": position.get("signal", ""),
            "signal_id": position.get("signal_id", ""),
            "reason": position.get("reason", ""),
            "entry_bar_ms": int(position["entry_bar_ms"]),
            "entry_us_time": position["entry_us_time"],
            "entry_cn_time": position["entry_cn_time"],
            "entry_price": round(float(position["entry_price"]), 4),
            "exit_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "exit_us_time": str(bar.get("us_time", "") or ""),
            "exit_cn_time": str(bar.get("cn_time", "") or ""),
            "exit_price": round(exit_price, 4),
            "shares": shares,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "bars_held": int(position.get("bars_held", 0) or 0),
            "exit_reason": reason,
            "session_type": str(bar.get("session_type", "") or "regular"),
            "extra": {
                "stop_price": float(position.get("stop_price", 0) or 0),
                "target_price": float(position.get("target_price", 0) or 0),
                "mfe": round(float(position.get("mfe", 0) or 0), 4),
                "mae": round(float(position.get("mae", 0) or 0), 4),
                "atr_stop_adjust_count": int(position.get("atr_stop_adjust_count", 0) or 0),
                "last_atr_stop_adjust": position.get("last_atr_stop_adjust") or {},
                "signal_bar_ms": int(position.get("signal_bar_ms", 0) or 0),
                "signal_us_time": position.get("signal_us_time", ""),
                "signal_close": float(position.get("signal_close", 0) or 0),
                **build_runtime_timestamps(),
            },
        }

    def _apply_slippage(self, price: float, direction: str, is_entry: bool, bps: float) -> float:
        if price <= 0:
            return 0.0
        slip = max(0.0, float(bps)) / 10000.0
        if direction == "long":
            return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
        return price * (1.0 - slip) if is_entry else price * (1.0 + slip)

    def _calc_pnl(self, direction: str, entry_price: float, exit_price: float, shares: int) -> float:
        if direction == "long":
            return (exit_price - entry_price) * shares
        return (entry_price - exit_price) * shares

    def _collapse_daily_equity(self, points: list[dict], initial_capital: float) -> list[dict]:
        if not points:
            return []
        collapsed = []
        by_date = {}
        for point in points:
            by_date[str(point.get("date") or "")] = round(float(point.get("equity", initial_capital) or initial_capital), 2)
        for date_text in sorted(by_date.keys()):
            collapsed.append({"date": date_text, "equity": by_date[date_text]})
        return collapsed

    def _build_benchmark_curve(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        session_mode: str,
        initial_capital: float,
        *,
        allow_backfill: bool = True,
    ) -> list[dict]:
        benchmark_symbol = str(symbol or "").strip().upper()
        if not benchmark_symbol:
            return []
        bars = self._load_symbol_bars(
            benchmark_symbol,
            source_environment,
            date_from,
            date_to,
            session_mode,
            allow_backfill=allow_backfill,
        )
        if not bars:
            return []
        closes_by_day = {}
        for bar in bars:
            day = str(bar.get("us_time", "") or "")[:10]
            closes_by_day[day] = float(bar.get("close", 0) or 0)
        items = []
        first_close = 0.0
        for day in sorted(closes_by_day.keys()):
            close = closes_by_day[day]
            if close <= 0:
                continue
            if first_close <= 0:
                first_close = close
            equity = initial_capital * (close / first_close)
            items.append({"date": day, "equity": round(equity, 2)})
        return items

    def _compute_metrics(self, initial_capital: float, trades: list[dict], daily_equity: list[dict]) -> dict:
        trade_count = len(trades)
        net_pnl = round(sum(float(item.get("pnl", 0) or 0) for item in trades), 4)
        gross_profit = round(sum(max(0.0, float(item.get("pnl", 0) or 0)) for item in trades), 4)
        gross_loss = round(sum(min(0.0, float(item.get("pnl", 0) or 0)) for item in trades), 4)
        winners = [item for item in trades if float(item.get("pnl", 0) or 0) > 0]
        losers = [item for item in trades if float(item.get("pnl", 0) or 0) < 0]
        win_rate = (len(winners) / trade_count * 100.0) if trade_count else 0.0
        avg_win = (gross_profit / len(winners)) if winners else 0.0
        avg_loss = (gross_loss / len(losers)) if losers else 0.0
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else 0.0
        expectancy = (net_pnl / trade_count) if trade_count else 0.0
        total_return_pct = (net_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0
        exit_reason_breakdown = {}
        direction_breakdown = {}
        mfe_values = []
        mae_values = []
        for item in trades:
            exit_reason = str(item.get("exit_reason") or "unknown")
            direction = str(item.get("direction") or "unknown")
            exit_reason_breakdown[exit_reason] = int(exit_reason_breakdown.get(exit_reason, 0) or 0) + 1
            direction_breakdown[direction] = int(direction_breakdown.get(direction, 0) or 0) + 1
            extra = self._parse_object(item.get("extra"))
            mfe_values.append(float(extra.get("mfe", 0) or 0))
            mae_values.append(float(extra.get("mae", 0) or 0))

        daily_returns = []
        previous_equity = initial_capital
        for point in daily_equity:
            equity = float(point.get("equity", previous_equity) or previous_equity)
            if previous_equity > 0:
                daily_returns.append((equity - previous_equity) / previous_equity)
            previous_equity = equity

        sharpe = 0.0
        sortino = 0.0
        if len(daily_returns) >= 2:
            mean_return = statistics.fmean(daily_returns)
            std_return = statistics.pstdev(daily_returns)
            if std_return > 0:
                sharpe = mean_return / std_return * math.sqrt(252)
            downside = [min(0.0, value) for value in daily_returns]
            downside_std = statistics.pstdev(downside)
            if downside_std > 0:
                sortino = mean_return / downside_std * math.sqrt(252)

        max_drawdown_pct = 0.0
        equity_peak = initial_capital
        for point in daily_equity:
            equity = float(point.get("equity", initial_capital) or initial_capital)
            equity_peak = max(equity_peak, equity)
            if equity_peak > 0:
                drawdown = (equity - equity_peak) / equity_peak * 100.0
                max_drawdown_pct = min(max_drawdown_pct, drawdown)

        monthly_returns = []
        if daily_equity:
            month_start_equity = None
            month_key = ""
            last_equity = initial_capital
            for point in daily_equity:
                day = str(point.get("date") or "")
                current_key = day[:7]
                equity = float(point.get("equity", initial_capital) or initial_capital)
                if current_key != month_key:
                    if month_key and month_start_equity and month_start_equity > 0:
                        monthly_returns.append(
                            {
                                "month": month_key,
                                "return_pct": round((last_equity - month_start_equity) / month_start_equity * 100.0, 4),
                            }
                        )
                    month_key = current_key
                    month_start_equity = last_equity if last_equity > 0 else initial_capital
                last_equity = equity
            if month_key and month_start_equity and month_start_equity > 0:
                monthly_returns.append(
                    {
                        "month": month_key,
                        "return_pct": round((last_equity - month_start_equity) / month_start_equity * 100.0, 4),
                    }
                )

        return {
            "trade_count": trade_count,
            "net_pnl": round(net_pnl, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "total_return_pct": round(total_return_pct, 4),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 4),
            "expectancy": round(expectancy, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "max_drawdown_pct": round(abs(max_drawdown_pct), 4),
            "ending_equity": round(initial_capital + net_pnl, 4),
            "daily_equity_count": len(daily_equity),
            "monthly_returns": monthly_returns,
            "exit_reason_breakdown": exit_reason_breakdown,
            "direction_breakdown": direction_breakdown,
            "avg_mfe": round(statistics.fmean(mfe_values), 4) if mfe_values else 0.0,
            "avg_mae": round(statistics.fmean(mae_values), 4) if mae_values else 0.0,
        }

    def _persist_trades(self, run_id: str, trades: list[dict]):
        if not self.pb or not run_id or not trades:
            return
        payloads = []
        for index, trade in enumerate(trades, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payloads.append(
                {
                    "run_id": run_id,
                    "symbol": trade.get("symbol", ""),
                    "direction": trade.get("direction", ""),
                    "signal": trade.get("signal", ""),
                    "signal_id": trade.get("signal_id", ""),
                    "entry_us_time": trade.get("entry_us_time", ""),
                    "exit_us_time": trade.get("exit_us_time", ""),
                    "entry_bar_ms": int(trade.get("entry_bar_ms", 0) or 0),
                    "exit_bar_ms": int(trade.get("exit_bar_ms", 0) or 0),
                    "entry_price": float(trade.get("entry_price", 0) or 0),
                    "exit_price": float(trade.get("exit_price", 0) or 0),
                    "shares": int(trade.get("shares", 0) or 0),
                    "pnl": float(trade.get("pnl", 0) or 0),
                    "pnl_pct": float(trade.get("pnl_pct", 0) or 0),
                    "bars_held": int(trade.get("bars_held", 0) or 0),
                    "exit_reason": trade.get("exit_reason", ""),
                    "session_type": trade.get("session_type", "regular"),
                    "trade_index": index,
                    "extra": trade.get("extra", {}),
                }
            )
        self._create_records_or_raise(TRADE_COLLECTION, payloads)

    def _build_replay_timeline(self, symbol: str, bars: list[dict], params: dict) -> list[dict]:
        timeline = build_runtime_timeline(
            symbol,
            "5m",
            bars,
            params=params,
            include_signals=True,
        )
        return [
            {
                "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                "us_time": row.get("us_time", ""),
                "session_type": row.get("session_type", "regular"),
                "open": round(float(row.get("open", 0) or 0), 4),
                "high": round(float(row.get("high", 0) or 0), 4),
                "low": round(float(row.get("low", 0) or 0), 4),
                "close": round(float(row.get("close", 0) or 0), 4),
                "atr": round(float(row.get("atr", 0) or 0), 4),
                "sd_zone": row.get("sd_zone", ""),
                "sd_trend": row.get("sd_trend", 0),
                "dtp_phase": row.get("dtp_phase", ""),
                "signal": row.get("signal"),
            }
            for row in timeline.get("rows", [])
        ]
