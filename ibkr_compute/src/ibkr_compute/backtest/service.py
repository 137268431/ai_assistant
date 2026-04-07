from __future__ import annotations

import json
import math
import statistics
import threading
import time
import traceback
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS, IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    ET,
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
from ibkr_compute.workflows.daily_scanner import DailyScanner

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
DEFAULT_MAX_SYMBOLS = 20
DEFAULT_MAX_PAGES = 1200
MAX_REPLAY_ROWS = 240
MAX_BATCH_VARIANTS = 16
TV_COMPARE_SAMPLE_LIMIT = 8
BACKTEST_WARMUP_BARS = 320
DEFAULT_SCAN_CUTOFF_TIME = "09:25"
DEFAULT_BACKTEST_RETENTION_LIMIT = 30
MAX_BACKTEST_RETENTION_LIMIT = 200
MAX_BACKTEST_WARMUP_BARS = 2000
SCAN_INTERVALS = tuple(COMPUTE_INTERVALS)

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


class BacktestService:
    def __init__(self, pb_client: PBClient):
        self.pb = pb_client
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
        now = datetime.now(ET)
        name = str(payload.get("name") or "").strip() or f"Backtest {now.strftime('%Y-%m-%d %H:%M')}"
        source_environment = str(payload.get("source_environment") or "live").strip().lower() or "live"
        symbol_source = str(payload.get("symbol_source") or "manual").strip().lower() or "manual"
        if symbol_source not in SYMBOL_SOURCE_VALUES:
            symbol_source = "manual"

        symbols = self._parse_symbols(payload.get("symbols") or payload.get("symbols_text") or "")
        benchmark_symbol = str(payload.get("benchmark_symbol") or "SPY").strip().upper() or "SPY"
        date_from = str(payload.get("date_from") or now.strftime("%Y-%m-%d")).strip()
        date_to = str(payload.get("date_to") or date_from).strip()
        session_mode = str(payload.get("session_mode") or "extended").strip().lower() or "extended"
        if session_mode not in SESSION_MODE_VALUES:
            session_mode = "extended"

        initial_capital = max(1000.0, float(payload.get("initial_capital") or 100000))
        commission_per_share = max(0.0, float(payload.get("commission_per_share") or 0.005))
        slippage_bps = max(0.0, float(payload.get("slippage_bps") or 2.0))
        force_flat_eod = True
        max_symbols = max(1, min(DEFAULT_MAX_SYMBOLS, int(payload.get("max_symbols") or DEFAULT_MAX_SYMBOLS)))
        compare_with_tv = bool(payload.get("compare_with_tv", True))
        compare_tv_signals = bool(payload.get("compare_tv_signals", False))
        warmup_bars = self._normalize_positive_int(
            payload.get("warmup_bars") or payload.get("preheat_bars"),
            default=BACKTEST_WARMUP_BARS,
            minimum=0,
            maximum=MAX_BACKTEST_WARMUP_BARS,
        )
        scan_warmup_bars = self._normalize_positive_int(
            payload.get("scan_warmup_bars") or payload.get("selection_warmup_bars"),
            default=warmup_bars,
            minimum=0,
            maximum=MAX_BACKTEST_WARMUP_BARS,
        )
        premarket_cutoff_time = self._normalize_hhmm(payload.get("premarket_cutoff_time") or payload.get("scan_cutoff_time"))
        scan_session_mode = str(payload.get("scan_session_mode") or "extended").strip().lower() or "extended"
        if scan_session_mode not in SESSION_MODE_VALUES:
            scan_session_mode = "extended"
        retention_limit = self._normalize_positive_int(
            payload.get("retention_limit"),
            default=DEFAULT_BACKTEST_RETENTION_LIMIT,
            minimum=1,
            maximum=MAX_BACKTEST_RETENTION_LIMIT,
        )
        raw_params = payload.get("strategy_params") or payload.get("params") or {}
        strategy_params = self._normalize_strategy_params(raw_params)
        strategy_tag = str(payload.get("strategy_tag") or "IBKR_SAC_BACKTEST_V1").strip() or "IBKR_SAC_BACKTEST_V1"
        variants = self._normalize_variants(payload.get("variants") or [], strategy_params, strategy_tag)

        return {
            "name": name,
            "source_environment": source_environment,
            "symbol_source": symbol_source,
            "symbols": symbols,
            "symbols_text": ",".join(symbols),
            "benchmark_symbol": benchmark_symbol,
            "date_from": date_from,
            "date_to": date_to,
            "session_mode": session_mode,
            "scan_session_mode": scan_session_mode,
            "initial_capital": initial_capital,
            "commission_per_share": commission_per_share,
            "slippage_bps": slippage_bps,
            "force_flat_eod": force_flat_eod,
            "max_symbols": max_symbols,
            "compare_with_tv": compare_with_tv,
            "compare_tv_signals": compare_tv_signals,
            "warmup_bars": warmup_bars,
            "scan_warmup_bars": scan_warmup_bars,
            "premarket_cutoff_time": premarket_cutoff_time,
            "retention_limit": retention_limit,
            "params": {
                "strategy_params": strategy_params,
                "strategy_tag": strategy_tag,
                "source_environment": source_environment,
                "session_mode": session_mode,
                "warmup_bars": warmup_bars,
                "scan_warmup_bars": scan_warmup_bars,
                "premarket_cutoff_time": premarket_cutoff_time,
            },
            "variants": variants,
            "strategy_tag": strategy_tag,
        }

    def _normalize_strategy_params(self, raw_params: dict, base_params: dict | None = None) -> dict:
        params = dict(base_params or DEFAULT_PARAMS)
        allowed_keys = set(DEFAULT_PARAMS.keys())
        if isinstance(raw_params, dict):
            translated = dict(raw_params)

            # Accept the config aliases used elsewhere in the IBKR stack and
            # translate them into the core engine's rr_ratio convention.
            if "rr_ratio" not in translated and "risk_reward_ratio" in translated:
                translated["rr_ratio"] = translated.get("risk_reward_ratio")
            if "rr_ratio" not in translated and "tp_atr_mult" in translated:
                try:
                    sl_atr_mult = float(translated.get("sl_atr_mult", params.get("sl_atr_mult", DEFAULT_PARAMS["sl_atr_mult"])))
                    tp_atr_mult = float(translated.get("tp_atr_mult"))
                    if sl_atr_mult:
                        translated["rr_ratio"] = tp_atr_mult / sl_atr_mult
                except Exception:
                    pass

            for key, value in translated.items():
                if key not in allowed_keys:
                    continue
                default = DEFAULT_PARAMS[key]
                try:
                    if isinstance(default, bool):
                        params[key] = bool(value)
                    elif isinstance(default, int) and not isinstance(default, bool):
                        params[key] = int(value)
                    elif isinstance(default, float):
                        params[key] = float(value)
                    else:
                        params[key] = value
                except Exception:
                    params[key] = default
        return params

    def _normalize_variants(self, raw_variants: list, base_params: dict, default_strategy_tag: str) -> list[dict]:
        if not isinstance(raw_variants, list):
            return []

        variants = []
        for index, item in enumerate(raw_variants[:MAX_BATCH_VARIANTS], start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("name") or f"Variant {index}").strip() or f"Variant {index}"
            raw_params = item.get("strategy_params")
            if raw_params is None:
                raw_params = item.get("params")
            if raw_params is None:
                raw_params = {
                    key: value
                    for key, value in item.items()
                    if key not in {"label", "name", "strategy_params", "params", "strategy_tag"}
                }
            variants.append(
                {
                    "label": label,
                    "strategy_params": self._normalize_strategy_params(raw_params or {}, base_params=base_params),
                    "strategy_tag": str(item.get("strategy_tag") or default_strategy_tag).strip() or default_strategy_tag,
                }
            )
        return variants

    def _parse_symbols(self, raw_symbols: str) -> list[str]:
        items = []
        for chunk in str(raw_symbols or "").replace("\n", ",").split(","):
            symbol = str(chunk or "").strip().upper()
            if not symbol or symbol in items:
                continue
            items.append(symbol)
        return items

    def _normalize_positive_int(self, raw_value: Any, default: int, minimum: int = 0, maximum: int = MAX_BACKTEST_WARMUP_BARS) -> int:
        try:
            value = int(raw_value)
        except Exception:
            value = int(default)
        return max(minimum, min(maximum, value))

    def _normalize_hhmm(self, raw_value: Any) -> str:
        text = str(raw_value or "").strip()
        if ":" not in text:
            return DEFAULT_SCAN_CUTOFF_TIME
        hour_text, minute_text = text.split(":", 1)
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except Exception:
            return DEFAULT_SCAN_CUTOFF_TIME
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return DEFAULT_SCAN_CUTOFF_TIME
        return f"{hour:02d}:{minute:02d}"

    def _build_variant_request(self, base_request: dict, variant: dict, variant_index: int) -> dict:
        request = deepcopy(base_request)
        request["variant_index"] = variant_index
        request["variant_label"] = str(variant.get("label") or f"Variant {variant_index}").strip() or f"Variant {variant_index}"
        request["strategy_tag"] = str(variant.get("strategy_tag") or base_request["strategy_tag"]).strip() or base_request["strategy_tag"]
        request["params"] = {
            "strategy_params": deepcopy(variant.get("strategy_params") or base_request["params"]["strategy_params"]),
            "strategy_tag": request["strategy_tag"],
            "source_environment": base_request["source_environment"],
            "session_mode": base_request["session_mode"],
        }
        request["name"] = f'{base_request["name"]} · {request["variant_label"]}'
        return request

    def _create_run_record(self, request: dict, extra_patch: dict | None = None) -> dict:
        extra = {
            "force_flat_eod": request["force_flat_eod"],
            "requested_symbols": request["symbols"],
            "max_symbols": request["max_symbols"],
            "strategy_tag": request["strategy_tag"],
            "warmup_bars": request.get("warmup_bars", BACKTEST_WARMUP_BARS),
            "scan_warmup_bars": request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)),
            "premarket_cutoff_time": request.get("premarket_cutoff_time", DEFAULT_SCAN_CUTOFF_TIME),
            "scan_session_mode": request.get("scan_session_mode", "extended"),
            "retention_limit": request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT),
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
                    "warmup_bars": request.get("warmup_bars", BACKTEST_WARMUP_BARS),
                    "scan_warmup_bars": request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)),
                    "premarket_cutoff_time": request.get("premarket_cutoff_time", DEFAULT_SCAN_CUTOFF_TIME),
                    "scan_session_mode": request.get("scan_session_mode", "extended"),
                    "retention_limit": request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT),
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
            params = run.get("params") or {}
            run_extra = run.get("extra") or {}
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
                    "strategy_tag": run_extra.get("strategy_tag") or "",
                    "strategy_params": deepcopy(params.get("strategy_params") or {}),
                    "error": run.get("error") or "",
                }
            )
        summaries.sort(key=lambda item: (item["variant_index"], item["name"]))
        return summaries

    def _sync_batch_after_run_cleanup(self, run: dict, deleted_run_id: str):
        extra = run.get("extra") or {}
        batch_id = str(extra.get("batch_id") or "").strip()
        if not batch_id:
            return
        batch = self._get_batch(batch_id)
        if not batch:
            return
        batch_extra = batch.get("extra") or {}
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
        batch_extra = batch.get("extra") or {}
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
    ) -> dict:
        extra = {
            "force_flat_eod": request["force_flat_eod"],
            "requested_symbols": request["symbols"],
            "resolved_symbols": symbols,
            "max_symbols": request["max_symbols"],
            "strategy_tag": request["strategy_tag"],
            "compare_with_tv": bool(request.get("compare_with_tv", True)),
            "compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "warmup_bars": int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            "scan_warmup_bars": int(request.get("scan_warmup_bars", request.get("warmup_bars", BACKTEST_WARMUP_BARS)) or BACKTEST_WARMUP_BARS),
            "premarket_cutoff_time": str(request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME),
            "scan_session_mode": str(request.get("scan_session_mode") or "extended"),
            "retention_limit": int(request.get("retention_limit", DEFAULT_BACKTEST_RETENTION_LIMIT) or DEFAULT_BACKTEST_RETENTION_LIMIT),
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
        return extra

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
        self._update_run(
            run_id,
            {
                "symbols": request["symbols_text"],
                "extra": self._build_run_extra(
                    request,
                    symbols,
                    backtest_target_capture=backtest_target_capture,
                    historical_targeting=historical_targeting,
                ),
            },
        )

        all_trades = []
        all_indicator_rows = []
        all_signal_rows = []
        all_reverse_rows = []
        daily_equity_points = []
        skipped_symbols = []
        data_quality = []
        tv_symbol_reports = []
        realized_pnl = 0.0
        initial_capital = float(request["initial_capital"])
        total_symbols = max(1, len(symbols))
        completed_symbols = 0

        for symbol in symbols:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            progress_value = 5 + int((completed_symbols / total_symbols) * 70)
            self._set_progress_context("running", "loading", f"loading {symbol}", progress_value, progress_context)
            bars = self._load_symbol_bars(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                request["session_mode"],
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
                continue

            trades, quality, tv_symbol_report, indicator_rows, signal_rows, reverse_rows = self._run_symbol_backtest(
                symbol,
                bars,
                request,
                allowed_trade_days=allowed_trade_days_by_symbol.get(symbol),
            )
            all_trades.extend(trades)
            all_indicator_rows.extend(indicator_rows)
            all_signal_rows.extend(signal_rows)
            all_reverse_rows.extend(reverse_rows)
            data_quality.append(quality)
            if tv_symbol_report:
                tv_symbol_reports.append(tv_symbol_report)
            completed_symbols += 1

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
        )
        metrics = self._compute_metrics(initial_capital, all_trades, daily_equity)
        metrics["benchmark"] = benchmark_points
        metrics["skipped_symbols"] = skipped_symbols
        metrics["data_quality"] = data_quality
        tv_parity = self._finalize_tv_parity_report(request, tv_symbol_reports)
        metrics["tv_parity"] = tv_parity.get("summary") or {}
        metrics["backtest_indicator_count"] = len(all_indicator_rows)
        metrics["backtest_signal_count"] = len(all_signal_rows)
        metrics["backtest_target_count"] = len(backtest_target_rows)
        metrics["backtest_reverse_signal_count"] = len(all_reverse_rows)
        metrics["signal_count"] = len(all_signal_rows)
        metrics["executed_signal_count"] = len([row for row in all_signal_rows if str(row.get("status") or "") == "executed"])
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

        self._set_progress_context("running", "persist", "saving trades and metrics", 92, progress_context)
        backtest_indicator_capture = self._persist_backtest_indicators(run_id, all_indicator_rows)
        metrics["backtest_indicator_capture"] = backtest_indicator_capture
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
                ),
                "error": "",
            },
        )
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
        patch = {
            "completed_count": completed_count,
            "best_run_id": best.get("run_id") or "",
            "best_variant_label": best.get("variant_label") or "",
            "best_total_return_pct": float(best.get("total_return_pct", 0) or 0),
            "best_sharpe": float(best.get("sharpe", 0) or 0),
            "leaderboard": sorted_items,
            "extra": extra_patch if extra_patch is not None else (batch.get("extra") or {}),
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
            self._update_batch(
                batch_id,
                {
                    "status": "completed",
                    "finished_at": finished_at,
                    "error": "",
                    "extra": {
                        **(batch.get("extra") or {}),
                        "duration_s": round(time.time() - batch_started_at, 3),
                        **build_runtime_timestamps(),
                    },
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
            rows = self.pb.get_all_records("watchlist", sort="symbol", max_pages=20)
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

    def _load_symbol_bars(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        session_mode: str,
    ) -> list[dict]:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        rows = self.pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
            ),
            sort="bar_time_ms",
            max_pages=DEFAULT_MAX_PAGES,
        )
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
        rows = self.pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                f"&& bar_time_ms < {before_bar_time_ms}"
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
        rows = self.pb.get_all_records("watchlist", sort="symbol", max_pages=20)
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

    def _build_historical_scan_engines(self, symbol: str, request: dict, cutoff_ms: int) -> tuple[dict, dict]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        environment = request["source_environment"]
        session_mode = request.get("scan_session_mode") or "extended"
        lookback_limit = int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
        engines = {}
        details = {
            "ready_timeframes": [],
            "bars_loaded": {},
            "last_bar_time_ms_by_interval": {},
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

    def _evaluate_historical_scan_symbol(self, symbol: str, trade_date: str, request: dict) -> dict | None:
        cutoff_ms = self._build_scan_cutoff_ms(trade_date, request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME)
        engines, details = self._build_historical_scan_engines(symbol, request, cutoff_ms)
        if not engines:
            return None
        scanner = DailyScanner(self.pb, engines)
        result = scanner.evaluate_symbol(symbol, trade_date, request["source_environment"])
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
            day_candidates = []
            scanned_count = 0
            ready_count = 0
            for item in universe_rows:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                symbol = str(item.get("symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                scanned_count += 1
                evaluated = self._evaluate_historical_scan_symbol(symbol, trade_date, request)
                if not evaluated:
                    continue
                ready_count += 1
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
                            "universe_size": scanned_count,
                            **build_runtime_timestamps(),
                        },
                    }
                )
            daily_summaries.append(
                {
                    "date": trade_date,
                    "universe_size": scanned_count,
                    "ready_symbol_count": ready_count,
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

    def _run_symbol_backtest(
        self,
        symbol: str,
        bars: list[dict],
        request: dict,
        allowed_trade_days: set[str] | None = None,
    ) -> tuple[list[dict], dict, dict | None, list[dict], list[dict]]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        slippage_bps = float(request["slippage_bps"])
        commission_per_share = float(request["commission_per_share"])
        force_flat_eod = bool(request["force_flat_eod"])
        engine = IndicatorEngine(symbol, "5m", params=params)
        signal_gen = SignalGenerator(symbol, "5m", params=params)
        compare_with_tv = self._should_compare_with_tv(request)
        compare_tv_signals = self._should_compare_tv_signals(request)
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

        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0) if bars else 0,
            request["session_mode"],
            limit=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
        )
        if warmup_bars:
            previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars)

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
                if open_position and force_flat_eod:
                    exit_trade = self._close_position(open_position, bars[index - 1], commission_per_share, slippage_bps, "eod")
                    trades.append(exit_trade)
                    open_position = None
                if pending_signal:
                    self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "new_day_reset")
                pending_signal = None
            previous_day = current_day

            if pending_signal and open_position is None:
                open_position = self._open_position(symbol, bar, pending_signal, commission_per_share, slippage_bps)
                self._mark_backtest_signal_status(
                    signal_index,
                    pending_signal.get("signal_id"),
                    "executed",
                    "opened_next_bar",
                    {
                        "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                        "entry_us_time": str(bar.get("us_time", "") or ""),
                        "entry_cn_time": str(bar.get("cn_time", "") or ""),
                    },
                )
                pending_signal = None

            if open_position:
                closed = self._check_exit(open_position, bar, commission_per_share, slippage_bps)
                if closed:
                    trades.append(closed)
                    open_position = None

            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue

            daily_fields = self._get_daily_change_fields_from_lookup(
                daily_close_lookup,
                float(snapshot.get("close", 0) or 0),
                int(bar.get("bar_time_ms", 0) or 0),
            )
            indicator_payload = self._build_tv_indicator_compare_payload(
                symbol,
                bar,
                engine.bar_count,
                snapshot,
                request["source_environment"],
                daily_fields,
            )
            indicator_audit = None
            if symbol_tv_parity is not None:
                indicator_audit = self._compare_generated_indicator(symbol_tv_parity, indicator_payload)
            indicator_rows.append(
                self._build_backtest_indicator_row(
                    request,
                    bar,
                    indicator_payload,
                    indicator_audit,
                )
            )
            if open_position:
                reverse_row = self._build_backtest_reverse_signal_row(
                    request,
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    daily_fields,
                    open_position,
                )
                reverse_key = self._build_backtest_reverse_key(reverse_row)
                if reverse_row and reverse_key not in reverse_index:
                    reverse_rows.append(reverse_row)
                    reverse_index.add(reverse_key)

            signal = signal_gen.update(snapshot)
            trading_day_enabled = allowed_trade_days is None or current_day in allowed_trade_days
            if not signal or open_position is not None or index >= len(bars) - 1:
                continue
            if int(signal.get("shares", 0) or 0) <= 0:
                continue
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
                continue
            pending_signal = {
                **signal,
                "signal_id": signal_payload.get("signal_id", build_signal_id(symbol, int(bar["bar_time_ms"]), str(signal.get("signal", "")))),
                "signal_bar_ms": int(bar["bar_time_ms"]),
                "signal_us_time": bar.get("us_time", ""),
                "signal_cn_time": bar.get("cn_time", ""),
                "signal_close": float(bar.get("close", 0) or 0),
                "reason": str(signal.get("reason", "") or ""),
                "chart_tf": interval_to_chart_tf("5m"),
            }

            if force_flat_eod and index < len(bars) - 1:
                next_day = str(bars[index + 1].get("us_time", "") or "")[:10]
                if next_day != current_day:
                    self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "force_flat_eod")
                    pending_signal = None

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
        return trades, quality, symbol_tv_parity, indicator_rows, signal_rows, reverse_rows

    def _should_compare_with_tv(self, request: dict) -> bool:
        return bool(request.get("compare_with_tv", True)) and str(request.get("source_environment") or "").strip().lower() != BACKTEST_ENVIRONMENT

    def _should_compare_tv_signals(self, request: dict) -> bool:
        return self._should_compare_with_tv(request) and bool(request.get("compare_tv_signals", False))

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

    def _resolve_reverse_indicator_action(self, score: float, target_state: str) -> str:
        if target_state == "pending_entry":
            return "cancel"
        if score >= 6:
            return "close"
        if score >= 3:
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
        return "|".join(
            [
                signal_id or trade_group_id or str(reverse_row.get("symbol", "") or "").strip().upper(),
                direction,
                target_state,
                action_type,
            ]
        )

    def _build_backtest_reverse_signal_row(
        self,
        request: dict,
        symbol: str,
        bar: dict,
        bar_index: int,
        snapshot: dict,
        daily_fields: dict,
        position: dict,
    ) -> dict | None:
        direction = str(position.get("direction", "") or "").strip().lower()
        if direction not in {"long", "short"}:
            return None
        analysis = self._analyze_backtest_reverse_snapshot(snapshot, direction)
        score = float(analysis.get("score", 0) or 0)
        triggered_signals = list(analysis.get("triggered_signals") or [])
        if score <= 0 or not triggered_signals:
            return None

        target_state = "filled_position"
        action_type = self._resolve_reverse_indicator_action(score, target_state)
        if not action_type:
            return None

        signal_id = str(position.get("signal_id", "") or "").strip()
        trade_group_id = signal_id or f"{symbol}_{int(position.get('entry_bar_ms', 0) or 0)}"
        strength = self._map_reverse_strength(score)
        bar_time_ms = int(bar.get("bar_time_ms", 0) or 0)
        close_value = float(analysis.get("close", snapshot.get("close", 0)) or 0)
        extra = {
            "environment": BACKTEST_ENVIRONMENT,
            "source_environment": request.get("source_environment") or "",
            "reverse_kind": "indicator_conflict",
            "target_state": target_state,
            "order_status": "Filled",
            "relation_status": "backtest_position_open",
            "position_side": direction,
            "current_direction": direction,
            "signal_id": signal_id,
            "origin_signal_id": signal_id,
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": trade_group_id,
            "order_unique_id": trade_group_id,
            "broker_order_id": "",
            "order_id": "",
            "entry_price": round(float(position.get("entry_price", 0) or 0), 4),
            "quantity": int(position.get("shares", 0) or 0),
            "take_profit": round(float(position.get("target_price", 0) or 0), 4),
            "stop_loss": round(float(position.get("stop_price", 0) or 0), 4),
            "crsi": round(float(analysis.get("crsi", 0) or 0), 4),
            "obv_rsi": round(float(analysis.get("obv_rsi", 0) or 0), 4),
            "vwap_dist": round(float(analysis.get("vwap_dist", 0) or 0), 4),
            "close": round(close_value, 4),
            "triggered_signals": triggered_signals,
            "chart_tf": interval_to_chart_tf("5m"),
            "bar_index": int(bar_index or 0),
            "bar_time_ms": bar_time_ms,
            "signal_bar_ms": int(position.get("signal_bar_ms", 0) or 0),
            "signal_us_time": str(position.get("signal_us_time", "") or ""),
            "signal_close": round(float(position.get("signal_close", 0) or 0), 4),
            "simulated_only": True,
            "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
            "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "script_tag": str(request.get("strategy_tag") or ""),
            **(daily_fields or {}),
            **build_runtime_timestamps(),
        }
        reason = f"indicator_conflict({target_state}) -> {', '.join(triggered_signals)}"
        return {
            "symbol": symbol,
            "direction": direction,
            "reverse_kind": "indicator_conflict",
            "source": "indicator",
            "target_state": target_state,
            "target_order_status": "Filled",
            "strength": strength,
            "score": round(score, 4),
            "triggered_signals": triggered_signals,
            "action_type": action_type,
            "status": "generated",
            "reason": reason,
            "priority": max(1, min(10, int(round(score)) or 1)),
            "signal_id": signal_id,
            "origin_signal_id": signal_id,
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

    def _persist_backtest_indicators(self, run_id: str, indicator_rows: list[dict]) -> dict:
        summary = self._empty_capture_summary(BACKTEST_INDICATOR_COLLECTION, run_id)
        summary["attempted_count"] = len(indicator_rows)
        if not self.pb or not run_id:
            return summary
        if not indicator_rows:
            return summary

        summary["status"] = "ok"
        for row in indicator_rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payload = dict(row or {})
            extra = self._parse_object(payload.get("extra"))
            extra["backtest_run_id"] = run_id
            payload["run_id"] = run_id
            payload["extra"] = extra
            try:
                self.pb.create_record(BACKTEST_INDICATOR_COLLECTION, payload)
                summary["saved_count"] += 1
            except Exception as exc:
                summary["error_count"] += 1
                summary["status"] = "partial"
                if len(summary["errors"]) < TV_COMPARE_SAMPLE_LIMIT:
                    summary["errors"].append(
                        {
                            "bar_time_ms": int(payload.get("bar_time_ms", 0) or 0),
                            "symbol": str(payload.get("symbol", "") or ""),
                            "error": str(exc)[:300],
                        }
                    )
        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _persist_backtest_signals(self, run_id: str, signal_rows: list[dict]) -> dict:
        summary = self._empty_capture_summary(BACKTEST_SIGNAL_COLLECTION, run_id)
        summary["attempted_count"] = len(signal_rows)
        if not self.pb or not run_id:
            return summary
        if not signal_rows:
            return summary

        summary["status"] = "ok"
        for row in signal_rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payload = dict(row or {})
            extra = self._parse_object(payload.get("extra"))
            extra["backtest_run_id"] = run_id
            payload["run_id"] = run_id
            payload["extra"] = extra
            try:
                self.pb.create_record(BACKTEST_SIGNAL_COLLECTION, payload)
                summary["saved_count"] += 1
            except Exception as exc:
                summary["error_count"] += 1
                summary["status"] = "partial"
                if len(summary["errors"]) < TV_COMPARE_SAMPLE_LIMIT:
                    summary["errors"].append(
                        {
                            "bar_time_ms": int(payload.get("bar_time_ms", 0) or 0),
                            "symbol": str(payload.get("symbol", "") or ""),
                            "signal_id": str(payload.get("signal_id", "") or ""),
                            "error": str(exc)[:300],
                        }
                    )
        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _persist_backtest_targets(self, run_id: str, target_rows: list[dict]) -> dict:
        summary = self._empty_capture_summary(BACKTEST_TARGET_COLLECTION, run_id)
        summary["attempted_count"] = len(target_rows)
        if not self.pb or not run_id:
            return summary
        if not target_rows:
            return summary

        summary["status"] = "ok"
        for row in target_rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payload = dict(row or {})
            extra = self._parse_object(payload.get("extra"))
            extra["backtest_run_id"] = run_id
            payload["run_id"] = run_id
            payload["extra"] = extra
            try:
                self.pb.create_record(BACKTEST_TARGET_COLLECTION, payload)
                summary["saved_count"] += 1
            except Exception as exc:
                summary["error_count"] += 1
                summary["status"] = "partial"
                if len(summary["errors"]) < TV_COMPARE_SAMPLE_LIMIT:
                    summary["errors"].append(
                        {
                            "date": str(payload.get("date", "") or ""),
                            "symbol": str(payload.get("symbol", "") or ""),
                            "error": str(exc)[:300],
                        }
                    )
        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _persist_backtest_reverse_signals(self, run_id: str, reverse_rows: list[dict]) -> dict:
        summary = self._empty_capture_summary(BACKTEST_REVERSE_SIGNAL_COLLECTION, run_id)
        summary["attempted_count"] = len(reverse_rows)
        if not self.pb or not run_id:
            return summary
        if not reverse_rows:
            return summary

        summary["status"] = "ok"
        for row in reverse_rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payload = dict(row or {})
            extra = self._parse_object(payload.get("extra"))
            extra["backtest_run_id"] = run_id
            payload["run_id"] = run_id
            payload["extra"] = extra
            try:
                self.pb.create_record(BACKTEST_REVERSE_SIGNAL_COLLECTION, payload)
                summary["saved_count"] += 1
            except Exception as exc:
                summary["error_count"] += 1
                summary["status"] = "partial"
                if len(summary["errors"]) < TV_COMPARE_SAMPLE_LIMIT:
                    summary["errors"].append(
                        {
                            "bar_time_ms": int(payload.get("bar_time_ms", 0) or 0),
                            "symbol": str(payload.get("symbol", "") or ""),
                            "action_type": str(payload.get("action_type", "") or ""),
                            "signal_id": str(payload.get("signal_id", "") or ""),
                            "error": str(exc)[:300],
                        }
                    )
        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _open_position(self, symbol: str, bar: dict, signal: dict, commission_per_share: float, slippage_bps: float) -> dict:
        direction = str(signal.get("direction", "") or "")
        shares = max(0, int(signal.get("shares", 0) or 0))
        base_open = float(bar.get("open", 0) or 0)
        fill_price = self._apply_slippage(base_open, direction, is_entry=True, bps=slippage_bps)
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
            "target_price": float(signal.get("take_profit", 0) or 0),
            "stop_price": float(signal.get("stop_loss", 0) or 0),
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
    ) -> list[dict]:
        benchmark_symbol = str(symbol or "").strip().upper()
        if not benchmark_symbol:
            return []
        bars = self._load_symbol_bars(benchmark_symbol, source_environment, date_from, date_to, session_mode)
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
        }

    def _persist_trades(self, run_id: str, trades: list[dict]):
        for index, trade in enumerate(trades, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payload = {
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
            self.pb.create_record(TRADE_COLLECTION, payload)

    def _build_replay_timeline(self, symbol: str, bars: list[dict], params: dict) -> list[dict]:
        engine = IndicatorEngine(symbol, "5m", params=params)
        signal_gen = SignalGenerator(symbol, "5m", params=params)
        timeline = []
        previous_day = ""
        for bar in bars:
            current_day = str(bar.get("us_time", "") or "")[:10]
            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
            previous_day = current_day
            snapshot = engine.update(bar)
            signal = None
            if snapshot and engine.is_ready():
                signal = signal_gen.update(snapshot)
            timeline.append(
                {
                    "bar_time_ms": int(bar.get("bar_time_ms", 0) or 0),
                    "us_time": bar.get("us_time", ""),
                    "session_type": bar.get("session_type", "regular"),
                    "open": round(float(bar.get("open", 0) or 0), 4),
                    "high": round(float(bar.get("high", 0) or 0), 4),
                    "low": round(float(bar.get("low", 0) or 0), 4),
                    "close": round(float(bar.get("close", 0) or 0), 4),
                    "atr": round(float((snapshot or {}).get("atr", 0) or 0), 4),
                    "sd_zone": (snapshot or {}).get("sd_zone", ""),
                    "sd_trend": (snapshot or {}).get("sd_trend", 0),
                    "dtp_phase": (snapshot or {}).get("dtp_phase", ""),
                    "signal": signal,
                }
            )
        return timeline
