"""
IBKR Compute — 指标计算 HTTP 服务
由 PB cron 触发, 常驻内存维护 IndicatorEngine 热缓存

端点:
  POST /compute     — 读最新 ibkr_bars, 更新引擎, 写 ibkr_indicators / ibkr_signals
  POST /scan        — 盘前扫描, 写 ibkr_targets
  POST /recompute   — 全量重算 (清空缓存, 从 ibkr_bars 历史重建)
  GET  /health      — 健康检查
  GET  /status      — 引擎状态
"""

import os
import time
import traceback
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, redirect, request

from ibkr_compute.backtest import BacktestService
from ibkr_compute.core.config import Config
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    HIGHER_INTERVALS,
    build_runtime_timestamps,
    build_signal_id,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
    normalize_interval,
)
from ibkr_compute.workflows.daily_scanner import DailyScanner

app = Flask(__name__)

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", PB_BASE_URL)
pb = PBClient(base_url=PB_BASE_URL)
cfg = Config(pb_client=pb)
backtest_service = BacktestService(pb)

engines = {}
signal_gens = {}
last_compute_time = 0.0
last_scan_time = 0.0
last_processed_ms = {}
last_interval_fetch_ms = {}
compute_count = 0
error_count = 0
rollup_bootstrap_checked = set()
engine_bootstrap_checked = set()
persistent_cursor_envs_loaded = set()
symbol_metadata_cache = {}
daily_close_cache = {}
daily_close_cache_date = ""
metadata_cache_updated_at = 0.0
compute_lock = threading.RLock()

INTERVALS = list(COMPUTE_INTERVALS)
SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
IBKR_SCRIPT_TAG = os.environ.get("IBKR_SCRIPT_TAG", "IBKR_SAC_v1_20260403")
BOOTSTRAP_LOOKBACK_BARS = {
    "5m": 192,
    "15m": 192,
    "30m": 192,
    "1h": 192,
    "4h": 192,
    "1d": 260,
}
LEGACY_COLLECTION_MAP = {
    "signals": "ibkr_signals",
    "indicators": "ibkr_indicators",
    "qc_signals": "ibkr_signals",
    "qc_indicators": "ibkr_indicators",
    "qc_bars": "ibkr_bars",
    "daily_targets": "ibkr_targets",
    "qc_state": "ibkr_state",
}
LEGACY_EMPTY_FALLBACKS = {"ibkr_positions", "ibkr_session"}
ROLLUP_BATCH_SIZE = 100
INDICATOR_BATCH_SIZE = max(1, int(os.environ.get("IBKR_INDICATOR_BATCH_SIZE", "60")))
SIGNAL_BATCH_SIZE = max(1, int(os.environ.get("IBKR_SIGNAL_BATCH_SIZE", "30")))
COMPUTE_CURSOR_STATE_KEY = "compute_cursors"
COMPUTE_CURSOR_STATE_DATE = "global"
IBKR_RUNTIME_CONTROL_STATE_KEY = "ibkr_runtime_control"
IBKR_RUNTIME_CONTROL_STATE_DATE = "global"


def current_market_date(now: datetime | None = None) -> str:
    et_now = now.astimezone(timezone(timedelta(hours=-4))) if now else datetime.now(timezone(timedelta(hours=-4)))
    return et_now.strftime("%Y-%m-%d")


def build_bar_environment_filter(environment: str, include_legacy_empty: bool = False) -> str:
    runtime_environment = str(environment or "").strip().lower() or "live"
    clauses = [f'environment = "{runtime_environment}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def normalize_bar_environment(bar: dict, environment: str) -> dict:
    payload = dict(bar)
    payload["environment"] = str(environment or "live").strip().lower() or "live"
    return payload


def get_market_index_symbols(environment: str) -> set[str]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    try:
        raw_value = cfg.get_for_environment("market_index_symbols", runtime_environment, "SPY,QQQ,VIX")
    except Exception:
        raw_value = cfg.get("market_index_symbols", "SPY,QQQ,VIX")
    return {
        str(item or "").strip().upper()
        for item in str(raw_value or "SPY,QQQ,VIX").split(",")
        if str(item or "").strip()
    }


def get_or_create_engine(environment: str, symbol: str, interval: str) -> IndicatorEngine:
    key = (environment, symbol, interval)
    if key not in engines:
        engines[key] = IndicatorEngine(symbol, interval)
        signal_gens[key] = SignalGenerator(symbol, interval)
    return engines[key]


def build_compute_cursor_key(symbol: str, interval: str) -> str:
    return f"{str(symbol or '').upper()}|{normalize_interval(interval)}"


def parse_compute_cursor_key(raw_key: str):
    text = str(raw_key or "").strip()
    if "|" not in text:
        return "", ""
    symbol, interval = text.split("|", 1)
    return str(symbol or "").upper(), normalize_interval(interval)


def apply_cursor_map(environment: str, cursor_map: dict) -> int:
    applied = 0
    for raw_key, raw_value in (cursor_map or {}).items():
        symbol, interval = parse_compute_cursor_key(raw_key)
        bar_ms = int(raw_value or 0)
        if not symbol or not interval or bar_ms <= 0:
            continue
        key = (environment, symbol, interval)
        last_processed_ms[key] = max(int(last_processed_ms.get(key, 0) or 0), bar_ms)
        interval_key = (environment, interval)
        last_interval_fetch_ms[interval_key] = max(int(last_interval_fetch_ms.get(interval_key, 0) or 0), bar_ms)
        applied += 1
    return applied


def collect_environment_cursor_map(environment: str) -> dict:
    env_map = {}
    for (env, symbol, interval), bar_ms in last_processed_ms.items():
        if env != environment:
            continue
        bar_ms = int(bar_ms or 0)
        if bar_ms <= 0:
            continue
        env_map[build_compute_cursor_key(symbol, interval)] = bar_ms
    return env_map


def persist_compute_cursors(environment: str):
    payload = {
        "version": 1,
        "cursor_count": 0,
        "updated_at_ms": int(time.time() * 1000),
        **build_runtime_timestamps(),
        "cursors": {},
    }
    payload["cursors"] = collect_environment_cursor_map(environment)
    payload["cursor_count"] = len(payload["cursors"])
    try:
        pb.upsert_state(
            COMPUTE_CURSOR_STATE_KEY,
            environment,
            payload,
            date=COMPUTE_CURSOR_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()


def seed_compute_cursors_from_indicators(environment: str) -> int:
    rows = pb.get_all_records(
        "ibkr_indicators",
        filter=f'environment = "{environment}"',
        sort="-bar_time_ms",
        max_pages=60,
    )
    seed_map = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        interval = normalize_interval(row.get("interval", ""))
        bar_ms = int(row.get("bar_time_ms", 0) or 0)
        if not symbol or not interval or bar_ms <= 0:
            continue
        cursor_key = build_compute_cursor_key(symbol, interval)
        if cursor_key not in seed_map:
            seed_map[cursor_key] = bar_ms

    applied = apply_cursor_map(environment, seed_map)
    if applied:
        persist_compute_cursors(environment)
    return applied


def load_persisted_compute_cursors(environment: str):
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if runtime_environment in persistent_cursor_envs_loaded:
        return

    applied = 0
    try:
        state = pb.get_state(
            COMPUTE_CURSOR_STATE_KEY,
            runtime_environment,
            date=COMPUTE_CURSOR_STATE_DATE,
        )
        payload = state.get("data") if isinstance(state, dict) else {}
        applied = apply_cursor_map(runtime_environment, payload.get("cursors") if isinstance(payload, dict) else {})
    except Exception:
        traceback.print_exc()

    if applied == 0:
        applied = seed_compute_cursors_from_indicators(runtime_environment)

    persistent_cursor_envs_loaded.add(runtime_environment)
    return applied


def bootstrap_engine_state(environment: str, symbol: str, interval: str, before_bar_time_ms: int, inclusive: bool = True):
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    key = (runtime_environment, symbol, normalized_interval)
    engine = get_or_create_engine(runtime_environment, symbol, normalized_interval)
    signal_generator = signal_gens.get(key)
    target_ms = int(before_bar_time_ms or 0)

    if key in engine_bootstrap_checked and (target_ms <= 0 or engine.last_bar_time_ms >= target_ms):
        return 0

    lookback = int(BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 192) or 192)
    max_pages = max(1, (lookback + 199) // 200 + 1)
    comparison = "<=" if inclusive else "<"
    filter_parts = [
        f'symbol = "{symbol}"',
        f'interval = "{normalized_interval}"',
        build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
    ]
    if target_ms > 0:
        filter_parts.append(f"bar_time_ms {comparison} {target_ms}")

    rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="-bar_time_ms",
        max_pages=max_pages,
    )
    if lookback > 0:
        rows = rows[:lookback]
    rows = list(reversed(rows))

    engine.reset()
    if signal_generator:
        signal_generator.daily_reset()

    processed = 0
    for row in rows:
        normalized_row = normalize_bar_environment(row, runtime_environment)
        snapshot = engine.update({
            "open": float(normalized_row.get("open", 0) or 0),
            "high": float(normalized_row.get("high", 0) or 0),
            "low": float(normalized_row.get("low", 0) or 0),
            "close": float(normalized_row.get("close", 0) or 0),
            "volume": float(normalized_row.get("volume", 0) or 0),
            "bar_time_ms": int(normalized_row.get("bar_time_ms", 0) or 0),
            "us_time": normalized_row.get("us_time", ""),
            "cn_time": normalized_row.get("cn_time", ""),
            "session_type": normalized_row.get("session_type", "regular"),
        })
        if normalized_interval == "5m" and signal_generator and snapshot and engine.is_ready():
            signal_generator.update(snapshot)
        processed += 1

    if engine.last_bar_time_ms > 0:
        last_processed_ms[key] = int(engine.last_bar_time_ms)
        interval_key = (runtime_environment, normalized_interval)
        last_interval_fetch_ms[interval_key] = max(
            int(last_interval_fetch_ms.get(interval_key, 0) or 0),
            int(engine.last_bar_time_ms),
        )
    engine_bootstrap_checked.add(key)
    return processed




def refresh_symbol_metadata(force: bool = False):
    global symbol_metadata_cache, metadata_cache_updated_at
    now = time.time()
    if symbol_metadata_cache and not force and (now - metadata_cache_updated_at) < 300:
        return symbol_metadata_cache

    metadata = {}
    try:
        rows = pb.get_all_records("watchlist", max_pages=20)
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            metadata[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
            }
    except Exception:
        traceback.print_exc()

    symbol_metadata_cache = metadata
    metadata_cache_updated_at = now
    return symbol_metadata_cache


def refresh_daily_close_cache(environments, force: bool = False):
    global daily_close_cache, daily_close_cache_date
    market_date = current_market_date()
    if (
        daily_close_cache
        and not force
        and daily_close_cache_date == market_date
        and set(environments).issubset(set(daily_close_cache.keys()))
    ):
        return daily_close_cache

    cache = {}
    now_ms = int(time.time() * 1000)
    lookback_ms = interval_to_ms("1d") * 400

    for environment in environments:
        rows = pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'interval = "1d" && {build_bar_environment_filter(environment, include_legacy_empty=True)} '
                f"&& bar_time_ms >= {max(0, now_ms - lookback_ms)}"
            ),
            sort="bar_time_ms",
            max_pages=400,
        )
        env_cache = {}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            close = float(row.get("close", 0) or 0)
            if not symbol or bar_ms <= 0 or close <= 0:
                continue
            env_cache.setdefault(symbol, []).append({
                "bar_time_ms": bar_ms,
                "date": ms_to_et(bar_ms).strftime("%Y-%m-%d"),
                "close": close,
            })
        cache[environment] = env_cache

    daily_close_cache = cache
    daily_close_cache_date = market_date
    return daily_close_cache


def reset_daily_runtime_state(environments=None, reason: str = "new_day") -> dict:
    runtime_environments = []
    for environment in (environments or DEFAULT_COMPUTE_ENVIRONMENTS):
        normalized = str(environment or "").strip().lower()
        if normalized in SUPPORTED_COMPUTE_ENVIRONMENTS and normalized not in runtime_environments:
            runtime_environments.append(normalized)

    reset_count = 0
    with compute_lock:
        for (environment, _, _), signal_generator in signal_gens.items():
            if environment not in runtime_environments:
                continue
            signal_generator.daily_reset()
            reset_count += 1

        global daily_close_cache, daily_close_cache_date
        daily_close_cache = {}
        daily_close_cache_date = ""

    return {
        "ok": True,
        "reason": reason,
        "date": current_market_date(),
        "environments": runtime_environments,
        "signal_generators_reset": reset_count,
    }


def get_daily_change_fields(environment: str, symbol: str, current_close: float, bar_time_ms: int):
    env_cache = daily_close_cache.get(environment, {})
    rows = env_cache.get(symbol.upper(), [])
    current_date = ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
    history = [row for row in rows if row["date"] < current_date]

    prev_close = history[-1]["close"] if len(history) >= 1 else 0.0
    prev_prev_close = history[-2]["close"] if len(history) >= 2 else 0.0
    close_5 = history[-5]["close"] if len(history) >= 5 else 0.0

    day_change_pct = ((current_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    prev_close_change_pct = (
        (prev_close - prev_prev_close) / prev_prev_close * 100.0
        if prev_prev_close > 0 else 0.0
    )
    change_7d = ((current_close - close_5) / close_5 * 100.0) if close_5 > 0 else 0.0

    return {
        "day_change_pct": round(day_change_pct, 2),
        "prev_close_change_pct": round(prev_close_change_pct, 2),
        "change_7d": round(change_7d, 2),
    }


def is_recent_signal_bar(bar_time_ms: int, interval: str) -> bool:
    now_ms = int(time.time() * 1000)
    return bar_time_ms >= now_ms - max(interval_to_ms(interval) * 3, 15 * 60 * 1000)


def get_fetch_since_ms(environment: str, interval: str) -> int:
    key = (environment, interval)
    last_fetch = int(last_interval_fetch_ms.get(key, 0) or 0)
    if last_fetch > 0:
        return max(0, last_fetch - interval_to_ms(interval) * 2)
    return 0


def has_interval_bars(environment: str, interval: str) -> bool:
    try:
        rows = pb.get_records(
            "ibkr_bars",
            filter=f'interval = "{interval}" && {build_bar_environment_filter(environment, include_legacy_empty=True)}',
            sort="-bar_time_ms",
            per_page=1,
            page=1,
        )
        return bool(rows)
    except Exception:
        traceback.print_exc()
        return False


def rebuild_higher_timeframe_bars(environment: str) -> dict:
    base_rows = pb.get_all_records(
        "ibkr_bars",
        filter=f'interval = "5m" && {build_bar_environment_filter(environment, include_legacy_empty=True)}',
        sort="bar_time_ms",
        max_pages=1000,
    )
    if not base_rows:
        return {"processed_5m": 0, "written": 0, "errors": 0}

    builder = TimeframeBarBuilder(target_intervals=HIGHER_INTERVALS)
    batch = []
    written = 0
    errors = 0

    rows = sorted(
        base_rows,
        key=lambda item: (int(item.get("bar_time_ms", 0) or 0), str(item.get("symbol", "")).upper()),
    )

    def flush_batch():
        nonlocal written, errors, batch
        if not batch:
            return
        try:
            result = pb.upsert_bars(batch)
            if result.get("ok", False):
                written += int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
            else:
                errors += len(batch)
        except Exception:
            errors += len(batch)
            traceback.print_exc()
        batch = []

    for row in rows:
        base_bar = normalize_bar_environment(row, environment)
        for derived_bar in builder.consume(base_bar):
            batch.append(normalize_bar_environment(derived_bar, environment))
            if len(batch) >= ROLLUP_BATCH_SIZE:
                flush_batch()

    flush_batch()
    return {"processed_5m": len(rows), "written": written, "errors": errors}


def ensure_higher_timeframe_bars(environments, force: bool = False):
    results = {}
    for environment in environments:
        if not force and environment in rollup_bootstrap_checked:
            results[environment] = {"skipped": True, "reason": "already_checked", "written": 0, "errors": 0}
            continue

        missing_intervals = [interval for interval in HIGHER_INTERVALS if force or not has_interval_bars(environment, interval)]
        if not missing_intervals:
            rollup_bootstrap_checked.add(environment)
            results[environment] = {"skipped": True, "reason": "already_present", "written": 0, "errors": 0}
            continue

        rollup_result = rebuild_higher_timeframe_bars(environment)
        rollup_result["missing_intervals"] = missing_intervals
        results[environment] = rollup_result
        rollup_bootstrap_checked.add(environment)
    return results


def fetch_interval_bars(environment: str, interval: str):
    rows = pb.get_all_records(
        "ibkr_bars",
        filter=(
            f'interval = "{interval}" && {build_bar_environment_filter(environment, include_legacy_empty=True)} '
            f"&& bar_time_ms >= {get_fetch_since_ms(environment, interval)}"
        ),
        sort="bar_time_ms",
        max_pages=500,
    )
    if rows:
        last_interval_fetch_ms[(environment, interval)] = max(
            int(row.get("bar_time_ms", 0) or 0) for row in rows
        )
    return rows


def build_indicator_payload(environment: str, symbol: str, interval: str, bar: dict, engine: IndicatorEngine, snapshot: dict):
    chart_tf = interval_to_chart_tf(interval)
    bar_ms = int(bar.get("bar_time_ms", 0) or 0)
    daily_fields = get_daily_change_fields(environment, symbol, float(snapshot.get("close", 0) or 0), bar_ms)
    extra = {
        **snapshot,
        **daily_fields,
        "symbol": symbol,
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "script_tag": IBKR_SCRIPT_TAG,
        "environment": environment,
        "session_type": bar.get("session_type", "regular"),
        "source": "ibkr_compute",
        **build_runtime_timestamps(),
    }

    return {
        "environment": environment,
        "symbol": symbol,
        "exchange": str(bar.get("exchange", "") or "").upper(),
        "interval": chart_tf,
        "script_tag": IBKR_SCRIPT_TAG,
        "us_time": bar.get("us_time", ""),
        "cn_time": bar.get("cn_time", ""),
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "extra": extra,
    }


def build_signal_payload(environment: str, symbol: str, interval: str, bar: dict, engine: IndicatorEngine, signal: dict):
    chart_tf = interval_to_chart_tf(interval)
    bar_ms = int(bar.get("bar_time_ms", 0) or 0)
    symbol_meta = refresh_symbol_metadata().get(symbol, {})
    signal_type = str(signal.get("signal", "") or "")
    signal_extra = dict(signal.get("extra") or {})
    signal_extra.update({
        "industry": symbol_meta.get("industry", ""),
        **get_daily_change_fields(environment, symbol, float(bar.get("close", 0) or 0), bar_ms),
        "script_tag": IBKR_SCRIPT_TAG,
        "chart_tf": chart_tf,
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "close": round(float(bar.get("close", 0) or 0), 2),
        "atr": signal_extra.get("atr_raw", signal_extra.get("atr", 0)),
        "environment": environment,
        "source": "ibkr_compute",
        **build_runtime_timestamps(),
    })

    rr_value = signal.get("rr", "")
    rr_text = f"{float(rr_value):.1f}:1" if isinstance(rr_value, (int, float)) else str(rr_value or "")

    return {
        "environment": environment,
        "symbol": symbol,
        "signal_id": build_signal_id(symbol, bar_ms, signal_type),
        "direction": signal.get("direction", ""),
        "signal": signal_type,
        "limit_price": round(float(bar.get("close", 0) or 0), 2),
        "entry": signal.get("entry", 0),
        "stop_loss": signal.get("stop_loss", 0),
        "take_profit": signal.get("take_profit", 0),
        "rr": rr_text,
        "shares": signal.get("shares", 0),
        "exchange": str(bar.get("exchange", "") or symbol_meta.get("exchange", "")).upper(),
        "interval": chart_tf,
        "reason": signal.get("reason", ""),
        "us_time": bar.get("us_time", ""),
        "cn_time": bar.get("cn_time", ""),
        "date": (bar.get("us_time", "") or "")[:10],
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "script_tag": IBKR_SCRIPT_TAG,
        "chart_tf": chart_tf,
        "status": "pending",
        "extra": signal_extra,
    }


def flush_indicator_batch(batch):
    if not batch:
        return {"ok": True, "written": 0, "errors": 0}

    try:
        result = pb.upsert_indicators(batch)
        written = int(result.get("success", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if not result.get("ok", False) and written == 0 and errors == 0:
            errors = len(batch)
        if result.get("ok", False) or (written > 0 and errors == 0):
            return {"ok": True, "written": written, "errors": errors}
    except Exception:
        traceback.print_exc()

    written = 0
    errors = 0
    for item in batch:
        try:
            result = pb.upsert_indicator(item)
            if str(result.get("action", "")).strip().lower() != "skipped":
                written += 1
        except Exception:
            errors += 1
            traceback.print_exc()
    return {"ok": errors == 0, "written": written, "errors": errors}


def flush_signal_batch(batch):
    if not batch:
        return {"ok": True, "written": 0, "errors": 0}

    try:
        result = pb.upsert_signals(batch)
        written = int(result.get("success", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if not result.get("ok", False) and written == 0 and errors == 0:
            errors = len(batch)
        if result.get("ok", False) or (written > 0 and errors == 0):
            return {"ok": True, "written": written, "errors": errors}
    except Exception:
        traceback.print_exc()

    written = 0
    errors = 0
    for item in batch:
        try:
            result = pb.upsert_signal(item)
            if str(result.get("action", "")).strip().lower() != "skipped":
                written += 1
        except Exception:
            errors += 1
            traceback.print_exc()
    return {"ok": errors == 0, "written": written, "errors": errors}

def get_requested_environments(defaults=None):
    payload = request.get_json(silent=True) or {}
    requested = payload.get("environments")
    if requested is None:
        requested = payload.get("environment")
    if isinstance(requested, str):
        requested = [requested]

    if isinstance(requested, list):
        environments = []
        for value in requested:
            environment = str(value or "").strip().lower()
            if environment in SUPPORTED_COMPUTE_ENVIRONMENTS and environment not in environments:
                environments.append(environment)
        if environments:
            return environments

    return list(defaults or DEFAULT_COMPUTE_ENVIRONMENTS)


@app.route("/api/collections/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def proxy_legacy_pb_collections(subpath):
    raw_path = str(subpath or "").lstrip("/")
    if not raw_path:
        return jsonify({"ok": False, "error": "missing collection path"}), 400

    parts = raw_path.split("/", 1)
    collection = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    mapped_collection = LEGACY_COLLECTION_MAP.get(collection, collection)
    target_path = mapped_collection if not rest else f"{mapped_collection}/{rest}"
    upstream = f"{PB_BASE_URL.rstrip('/')}/api/collections/{target_path}"

    headers = {}
    auth_header = request.headers.get("Authorization")
    content_type = request.headers.get("Content-Type")
    if auth_header:
        headers["Authorization"] = auth_header
    if content_type:
        headers["Content-Type"] = content_type

    try:
        upstream_resp = requests.request(
            method=request.method,
            url=upstream,
            params=request.args,
            data=request.get_data(),
            headers=headers,
            timeout=30,
        )
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"proxy to PocketBase failed: {exc}",
            "upstream": upstream,
        }), 502

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    if request.method == "GET" and upstream_resp.status_code in (400, 404) and mapped_collection in LEGACY_EMPTY_FALLBACKS:
        per_page = request.args.get("perPage", type=int) or 30
        page = request.args.get("page", type=int) or 1
        return jsonify({
            "page": page,
            "perPage": per_page,
            "totalItems": 0,
            "totalPages": 0,
            "items": [],
        })

    response_headers = [
        (key, value)
        for key, value in upstream_resp.headers.items()
        if key.lower() not in excluded
    ]
    return Response(upstream_resp.content, upstream_resp.status_code, response_headers)



def is_environment_compute_enabled(environment: str) -> bool:
    runtime_environment = str(environment or "").strip().lower()
    if runtime_environment == "backtest" and not cfg.has_environment_override("ibkr_compute_enabled", runtime_environment):
        return False
    default_enabled = runtime_environment in ("live", "paper")
    return cfg.get_bool_for_environment("ibkr_compute_enabled", runtime_environment, default_enabled)


@app.route("/compute", methods=["POST"])
def compute():
    global last_compute_time, compute_count, error_count

    with compute_lock:
        cfg.refresh()
        payload = request.get_json(silent=True) or {}
        skip_persisted_cursor = str(payload.get("source") or "").strip().lower() == "recompute"
        requested_environments = get_requested_environments()
        enabled_environments = [env for env in requested_environments if is_environment_compute_enabled(env)]
        if not enabled_environments:
            return jsonify({
                "ok": True,
                "skipped": True,
                "reason": "compute_disabled",
                "requested_environments": requested_environments,
                "environments": [],
            })

        start = time.time()
        processed = 0
        signals_found = 0
        errors = 0
        force_rollup = bool(payload.get("force_rollup")) or str(payload.get("source") or "") == "recompute"
        indicator_batch = []
        signal_batch = []
        dirty_cursor_environments = set()

        def flush_pending_indicators():
            nonlocal errors, indicator_batch
            if not indicator_batch:
                return
            result = flush_indicator_batch(indicator_batch)
            errors += int(result.get("errors", 0) or 0)
            indicator_batch = []

        def flush_pending_signals():
            nonlocal errors, signals_found, signal_batch
            if not signal_batch:
                return
            result = flush_signal_batch(signal_batch)
            errors += int(result.get("errors", 0) or 0)
            signals_found += int(result.get("written", 0) or 0)
            signal_batch = []

        refresh_symbol_metadata()
        rollup_results = ensure_higher_timeframe_bars(enabled_environments, force=force_rollup)
        errors += sum(int(result.get("errors", 0) or 0) for result in rollup_results.values())
        refresh_daily_close_cache(enabled_environments)

        try:
            for environment in enabled_environments:
                if not skip_persisted_cursor:
                    load_persisted_compute_cursors(environment)
                market_index_symbols = get_market_index_symbols(environment)
                for interval in INTERVALS:
                    interval_bars = fetch_interval_bars(environment, interval)
                    if not interval_bars:
                        continue

                    by_symbol = {}
                    for bar in interval_bars:
                        symbol = str(bar.get("symbol", "")).upper()
                        if symbol:
                            by_symbol.setdefault(symbol, []).append(bar)

                    for symbol, bars in by_symbol.items():
                        bars.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                        engine = get_or_create_engine(environment, symbol, interval)
                        signal_generator = signal_gens.get((environment, symbol, interval))
                        key = (environment, symbol, interval)
                        last_ms = int(last_processed_ms.get(key, 0) or 0)
                        bootstrap_target_ms = last_ms
                        bootstrap_inclusive = True
                        if bootstrap_target_ms <= 0 and bars:
                            bootstrap_target_ms = int(bars[0].get("bar_time_ms", 0) or 0)
                            bootstrap_inclusive = False
                        if bootstrap_target_ms > 0:
                            bootstrap_engine_state(
                                environment,
                                symbol,
                                interval,
                                bootstrap_target_ms,
                                inclusive=bootstrap_inclusive,
                            )
                            last_ms = int(last_processed_ms.get(key, 0) or 0)

                        for bar in bars:
                            bar_ms = int(bar.get("bar_time_ms", 0) or 0)
                            if bar_ms <= last_ms:
                                continue

                            snapshot = engine.update({
                                "open": float(bar.get("open", 0) or 0),
                                "high": float(bar.get("high", 0) or 0),
                                "low": float(bar.get("low", 0) or 0),
                                "close": float(bar.get("close", 0) or 0),
                                "volume": float(bar.get("volume", 0) or 0),
                                "bar_time_ms": bar_ms,
                                "us_time": bar.get("us_time", ""),
                                "cn_time": bar.get("cn_time", ""),
                                "session_type": bar.get("session_type", "regular"),
                            })
                            last_processed_ms[key] = bar_ms
                            last_ms = bar_ms
                            dirty_cursor_environments.add(environment)
                            processed += 1

                            if not snapshot or not engine.is_ready():
                                continue

                            indicator_batch.append(build_indicator_payload(environment, symbol, interval, bar, engine, snapshot))
                            if len(indicator_batch) >= INDICATOR_BATCH_SIZE:
                                flush_pending_indicators()

                            if interval != "5m" or not signal_generator or symbol in market_index_symbols:
                                continue

                            signal = signal_generator.update(snapshot)
                            if signal and is_recent_signal_bar(bar_ms, interval):
                                signal_batch.append(build_signal_payload(environment, symbol, interval, bar, engine, signal))
                                if len(signal_batch) >= SIGNAL_BATCH_SIZE:
                                    flush_pending_signals()
        except Exception:
            errors += 1
            error_count += 1
            traceback.print_exc()
        finally:
            flush_pending_indicators()
            flush_pending_signals()
            for environment in sorted(dirty_cursor_environments):
                persist_compute_cursors(environment)

        last_compute_time = time.time()
        compute_count += 1
        return jsonify({
            "ok": True,
            "requested_environments": requested_environments,
            "environments": sorted(enabled_environments),
            "processed": processed,
            "signals": signals_found,
            "errors": errors,
            "rollup": rollup_results,
            "engines": len(engines),
            "elapsed_s": round(time.time() - start, 3),
        })


@app.route("/scan", methods=["POST"])
def scan():
    global last_scan_time

    cfg.refresh()
    requested_environments = get_requested_environments()
    enabled_environments = [env for env in requested_environments if is_environment_compute_enabled(env)]
    if not enabled_environments:
        return jsonify({
            "ok": True,
            "skipped": True,
            "reason": "compute_disabled",
            "requested_environments": requested_environments,
            "environments": [],
        })

    date_str = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")
    scanner = DailyScanner(pb_client=pb, engines=engines)
    result = scanner.run_scan(date_str, environments=enabled_environments)
    last_scan_time = time.time()

    return jsonify({
        "ok": True,
        "date": date_str,
        "requested_environments": requested_environments,
        "environments": enabled_environments,
        **result,
    })


@app.route("/recompute", methods=["POST"])
def recompute():
    global last_processed_ms, last_interval_fetch_ms, daily_close_cache, rollup_bootstrap_checked
    global engine_bootstrap_checked, persistent_cursor_envs_loaded

    for engine in engines.values():
        engine.reset()
    for signal_generator in signal_gens.values():
        signal_generator.daily_reset()
    last_processed_ms.clear()
    last_interval_fetch_ms.clear()
    daily_close_cache = {}
    rollup_bootstrap_checked.clear()
    engine_bootstrap_checked.clear()
    persistent_cursor_envs_loaded.clear()

    compute_payload = {}
    with app.test_request_context("/compute", method="POST", json={"source": "recompute", "force_rollup": True}):
        response = compute()
        try:
            compute_payload = response.get_json() or {}
        except Exception:
            compute_payload = {}

    return jsonify({
        "ok": True,
        "action": "recompute",
        "engines_reset": len(engines),
        "compute": compute_payload,
    })


@app.route("/backtest/run", methods=["POST"])
def backtest_run():
    payload = request.get_json(silent=True) or {}
    result = backtest_service.start_run(payload)
    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


@app.route("/backtest/status", methods=["GET"])
def backtest_status():
    return jsonify(backtest_service.status())


@app.route("/backtest/cancel", methods=["POST"])
def backtest_cancel():
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "").strip()
    result = backtest_service.cancel(run_id)
    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


@app.route("/backtest/replay", methods=["GET"])
def backtest_replay():
    run_id = str(request.args.get("run_id") or "").strip()
    symbol = str(request.args.get("symbol") or "").strip().upper()
    center_bar_ms = int(request.args.get("center_bar_ms") or 0)
    window = int(request.args.get("window") or 80)
    result = backtest_service.replay(run_id, symbol, center_bar_ms=center_bar_ms, window=window)
    status_code = 200 if result.get("ok") else 404
    return jsonify(result), status_code


@app.route("/backtest/cleanup", methods=["POST"])
def backtest_cleanup():
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "").strip()
    batch_id = str(payload.get("batch_id") or "").strip()
    result = backtest_service.cleanup(run_id=run_id, batch_id=batch_id)
    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "status": "running",
        "engines": len(engines),
        "compute_count": compute_count,
        "error_count": error_count,
        "last_compute": datetime.fromtimestamp(last_compute_time).isoformat() if last_compute_time else None,
        "last_scan": datetime.fromtimestamp(last_scan_time).isoformat() if last_scan_time else None,
        "uptime_s": round(time.time() - _start_time, 1),
        "backtest": backtest_service.status(),
    })


@app.route("/status", methods=["GET"])
def status():
    engine_status = {}
    for (environment, symbol, interval), engine in engines.items():
        key = f"{environment}/{symbol}/{interval}"
        engine_status[key] = {
            "environment": environment,
            "bar_count": engine.bar_count,
            "is_ready": engine.is_ready(),
            "last_bar_time_ms": engine.last_bar_time_ms,
            "last_close": engine.get_snapshot().get("close"),
        }

    return jsonify({
        "ok": True,
        "compute_enabled": cfg.compute_enabled,
        "compute_enabled_by_environment": {
            environment: is_environment_compute_enabled(environment)
            for environment in SUPPORTED_COMPUTE_ENVIRONMENTS
        },
        "supported_environments": SUPPORTED_COMPUTE_ENVIRONMENTS,
        "default_environments": DEFAULT_COMPUTE_ENVIRONMENTS,
        "total_engines": len(engines),
        "ready_engines": sum(1 for engine in engines.values() if engine.is_ready()),
        "engines": engine_status,
        "persisted_cursor_envs_loaded": sorted(persistent_cursor_envs_loaded),
        "tracked_cursors": len(last_processed_ms),
        "compute_count": compute_count,
        "last_compute": datetime.fromtimestamp(last_compute_time).isoformat() if last_compute_time else None,
        "last_scan": datetime.fromtimestamp(last_scan_time).isoformat() if last_scan_time else None,
        "backtest": backtest_service.status(),
    })


_start_time = time.time()
_ibkr_service = None
_ibkr_restore_attempted = False


def _ibkr_runtime_control_default(environment: str) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    return {
        "environment": runtime_environment,
        "desired_running": False,
        "last_source": "",
        "last_reason": "",
        "last_start_request_at": "",
        "last_stop_request_at": "",
        "last_restore_attempt_at": "",
        "last_restore_trigger_login": False,
        "updated_at": "",
    }


def get_ibkr_runtime_control(environment: str) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    fallback = _ibkr_runtime_control_default(runtime_environment)
    try:
        state = pb.get_state(
            IBKR_RUNTIME_CONTROL_STATE_KEY,
            runtime_environment,
            date=IBKR_RUNTIME_CONTROL_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()
        return fallback

    payload = state.get("data") if isinstance(state, dict) else {}
    if not isinstance(payload, dict):
        return fallback
    return {
        **fallback,
        **payload,
        "environment": runtime_environment,
        "desired_running": bool(payload.get("desired_running", False)),
        "last_restore_trigger_login": bool(payload.get("last_restore_trigger_login", False)),
    }


def set_ibkr_runtime_control(
    environment: str,
    desired_running: bool,
    source: str,
    reason: str,
    extra: dict | None = None,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    timestamps = build_runtime_timestamps()
    current = get_ibkr_runtime_control(runtime_environment)
    patch = {
        **current,
        "environment": runtime_environment,
        "desired_running": bool(desired_running),
        "last_source": str(source or "").strip(),
        "last_reason": str(reason or "").strip(),
        "updated_at": timestamps.get("us", ""),
    }
    if desired_running:
        patch["last_start_request_at"] = timestamps.get("us", "")
    else:
        patch["last_stop_request_at"] = timestamps.get("us", "")
    if isinstance(extra, dict):
        patch.update(extra)
    try:
        return pb.upsert_state(
            IBKR_RUNTIME_CONTROL_STATE_KEY,
            runtime_environment,
            patch,
            date=IBKR_RUNTIME_CONTROL_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()
        return {"data": patch}


def _background_start_ibkr_service(service, trigger_login: bool, reason: str, source: str):
    thread = threading.Thread(
        target=service.start,
        kwargs={
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        },
        daemon=True,
        name=f"ibkr-service-{source}",
    )
    thread.start()
    return thread


def _maybe_restore_ibkr_service(service):
    global _ibkr_restore_attempted
    if not service or _ibkr_restore_attempted:
        return

    runtime_environment = _ibkr_service_environment(service)
    control = get_ibkr_runtime_control(runtime_environment)
    if not control.get("desired_running"):
        return
    if getattr(service, "is_busy", False):
        return

    _ibkr_restore_attempted = True
    timestamps = build_runtime_timestamps()
    set_ibkr_runtime_control(
        runtime_environment,
        True,
        source="server_boot",
        reason="auto_restore",
        extra={
            "last_restore_attempt_at": timestamps.get("us", ""),
            "last_restore_trigger_login": False,
        },
    )
    print(
        f"[IBKR] Auto-restore requested for env={runtime_environment}; "
        "starting runtime with trigger_login=false"
    )
    _background_start_ibkr_service(
        service,
        trigger_login=False,
        reason="auto_restore",
        source="server_boot",
    )



def get_ibkr_service():
    global _ibkr_service
    if _ibkr_service is None:
        try:
            from ibkr_compute.ibkr_service import IBKRTradingService

            _ibkr_service = IBKRTradingService()
        except Exception as exc:
            print(f"[IBKR] Service init failed: {exc}")
    return _ibkr_service


def _ibkr_service_environment(service) -> str:
    try:
        return str(service.status().get("environment") or "live").strip().lower() or "live"
    except Exception:
        return "live"


def _ibkr_service_uses_paper_account(service) -> bool:
    return _ibkr_service_environment(service) == "paper"


def _coerce_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _summary_lookup(summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in summary.items()}


def _extract_summary_number(summary_map: dict, *keys: str) -> float:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("amount", "value"):
                number = _coerce_float(lowered.get(field))
                if number is not None:
                    return float(number)
        else:
            number = _coerce_float(raw_value)
            if number is not None:
                return float(number)
    return 0.0


def _extract_summary_text(summary_map: dict, *keys: str) -> str:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("value", "displayvalue", "text"):
                value = lowered.get(field)
                if value not in (None, ""):
                    return str(value)
            amount = lowered.get("amount")
            if amount not in (None, ""):
                return str(amount)
        elif raw_value not in (None, ""):
            return str(raw_value)
    return ""


def _normalize_live_position(position: dict) -> dict:
    quantity = float(_coerce_float(position.get("position"), 0.0) or 0.0)
    market_price = float(_coerce_float(position.get("mktPrice"), 0.0) or 0.0)
    market_value = _coerce_float(position.get("mktValue"))
    if market_value is None:
        market_value = quantity * market_price

    return {
        "symbol": str(position.get("ticker") or position.get("contractDesc") or "").strip().upper(),
        "conid": int(_coerce_float(position.get("conid"), 0) or 0),
        "quantity": quantity,
        "direction": "long" if quantity > 0 else "short" if quantity < 0 else "flat",
        "avg_cost": float(_coerce_float(position.get("avgCost"), 0.0) or 0.0),
        "avg_price": float(_coerce_float(position.get("avgPrice"), 0.0) or 0.0),
        "market_price": market_price,
        "market_value": float(market_value or 0.0),
        "unrealized_pnl": float(_coerce_float(position.get("unrealizedPnl"), 0.0) or 0.0),
        "realized_pnl": float(_coerce_float(position.get("realizedPnl"), 0.0) or 0.0),
        "account": str(position.get("acctId") or position.get("account") or "").strip(),
        "currency": str(position.get("currency") or "USD").strip().upper(),
        "asset_class": str(position.get("assetClass") or "").strip().upper(),
        "raw": position,
    }


def _normalize_live_order(order: dict) -> dict:
    status = str(order.get("status") or "").strip()
    total_quantity = float(
        _coerce_float(
            order.get("totalSize")
            if order.get("totalSize") is not None
            else order.get("quantity"),
            0.0,
        ) or 0.0
    )
    filled_quantity = float(_coerce_float(order.get("filledQuantity"), 0.0) or 0.0)
    remaining_quantity = _coerce_float(order.get("remainingQuantity"))
    if remaining_quantity is None:
        remaining_quantity = _coerce_float(order.get("remainingSize"))
    if remaining_quantity is None:
        remaining_quantity = max(total_quantity - filled_quantity, 0.0)

    closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
    normalized_status = status.upper()

    return {
        "order_id": str(order.get("orderId") or order.get("id") or "").strip(),
        "parent_id": str(order.get("parentId") or "").strip(),
        "symbol": str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip().upper(),
        "side": str(order.get("side") or "").strip().upper(),
        "status": status,
        "order_type": str(order.get("orderType") or order.get("orderDesc") or "").strip().upper(),
        "price": float(_coerce_float(order.get("price"), 0.0) or 0.0),
        "avg_price": float(_coerce_float(order.get("avgPrice"), 0.0) or 0.0),
        "total_quantity": total_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": float(remaining_quantity or 0.0),
        "time_in_force": str(order.get("tif") or order.get("timeInForce") or "").strip().upper(),
        "account": str(order.get("acct") or order.get("acctId") or "").strip(),
        "can_cancel": bool(normalized_status and normalized_status not in closed_statuses),
        "can_modify": bool(normalized_status and normalized_status not in closed_statuses),
        "raw": order,
    }


def _build_ibkr_account_snapshot(service) -> dict:
    runtime_environment = _ibkr_service_environment(service)
    service_status = service.status() if hasattr(service, "status") else {}
    use_paper = _ibkr_service_uses_paper_account(service)
    account_id = ""
    if hasattr(service, "order_placer"):
        try:
            account_id = str(service.order_placer.get_active_account_id(use_paper=use_paper) or "").strip()
        except Exception:
            account_id = ""
    if not account_id and hasattr(service, "order_lifecycle"):
        account_id = str(getattr(service.order_lifecycle, "account_id", "") or "").strip()

    summary_raw = {}
    positions_raw = []
    orders_raw = []
    summary_error = ""
    positions_error = ""
    orders_error = ""

    try:
        summary_raw = service.order_lifecycle.get_account_summary(account_id)
    except Exception as exc:
        summary_error = str(exc)
    try:
        positions_raw = service.order_lifecycle.get_positions(account_id)
    except Exception as exc:
        positions_error = str(exc)
    try:
        orders_raw = service.order_tracker.get_live_orders()
    except Exception as exc:
        orders_error = str(exc)

    positions = [_normalize_live_position(item) for item in (positions_raw or []) if isinstance(item, dict)]
    orders = [_normalize_live_order(item) for item in (orders_raw or []) if isinstance(item, dict)]
    summary_map = _summary_lookup(summary_raw)

    total_unrealized = sum(float(item.get("unrealized_pnl", 0) or 0) for item in positions)
    total_market_value = sum(abs(float(item.get("market_value", 0) or 0)) for item in positions)
    open_orders_count = len([item for item in orders if item.get("can_cancel")])

    summary = {
        "account_code": _extract_summary_text(summary_map, "accountcode") or account_id,
        "account_type": _extract_summary_text(summary_map, "accounttype"),
        "net_liquidation": _extract_summary_number(summary_map, "netliquidation", "netliq"),
        "available_funds": _extract_summary_number(summary_map, "availablefunds"),
        "buying_power": _extract_summary_number(summary_map, "buyingpower"),
        "excess_liquidity": _extract_summary_number(summary_map, "excessliquidity"),
        "equity_with_loan": _extract_summary_number(summary_map, "equitywithloanvalue"),
        "gross_position_value": _extract_summary_number(summary_map, "grosspositionvalue", "stockmarketvalue") or total_market_value,
        "total_cash_value": _extract_summary_number(summary_map, "totalcashvalue", "cashbalance", "settledcash"),
        "initial_margin": _extract_summary_number(summary_map, "initmarginreq"),
        "maintenance_margin": _extract_summary_number(summary_map, "maintmarginreq"),
        "unrealized_pnl": _extract_summary_number(summary_map, "unrealizedpnl") or total_unrealized,
        "realized_pnl": _extract_summary_number(summary_map, "realizedpnl"),
        "currency": _extract_summary_text(summary_map, "currency", "basecurrency") or "USD",
    }

    return {
        "ok": True,
        "environment": runtime_environment,
        "account_id": account_id,
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "websocket_ready": bool((service_status.get("websocket") or {}).get("ready")),
        "summary": summary,
        "summary_raw": summary_raw if isinstance(summary_raw, dict) else {},
        "positions": positions,
        "orders": orders,
        "counts": {
            "positions": len(positions),
            "open_positions": len([item for item in positions if float(item.get("quantity", 0) or 0) != 0]),
            "orders": len(orders),
            "open_orders": open_orders_count,
        },
        "errors": {
            "summary": summary_error,
            "positions": positions_error,
            "orders": orders_error,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.route("/ibkr/start", methods=["POST"])
def ibkr_start():
    global _ibkr_restore_attempted
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    try:
        payload = request.get_json(silent=True) or {}
        trigger_login = payload.get("trigger_login", True)
        reason = str(payload.get("reason") or "manual_start")
        source = str(payload.get("source") or "api_start")
        runtime_environment = _ibkr_service_environment(service)

        _ibkr_restore_attempted = False
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source=source,
            reason=reason,
            extra={
                "last_restore_trigger_login": bool(trigger_login),
            },
        )

        if getattr(service, "is_busy", False):
            return jsonify({
                "ok": True,
                "message": "IBKR service already starting" if getattr(service, "is_starting", False) else "IBKR service already running",
                "trigger_login": bool(trigger_login),
                "reason": reason,
                "source": source,
                "starting": bool(getattr(service, "is_starting", False)),
                "running": bool(getattr(service, "is_running", False)),
            })

        _background_start_ibkr_service(
            service,
            trigger_login=bool(trigger_login),
            reason=reason,
            source=source,
        )
        return jsonify({
            "ok": True,
            "message": "IBKR service starting",
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/ibkr/stop", methods=["POST"])
def ibkr_stop():
    global _ibkr_restore_attempted
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    service.stop()
    _ibkr_restore_attempted = False
    runtime_environment = _ibkr_service_environment(service)
    set_ibkr_runtime_control(
        runtime_environment,
        False,
        source="api_stop",
        reason="manual_stop",
        extra={
            "last_restore_trigger_login": False,
        },
    )
    return jsonify({"ok": True, "message": "IBKR service stopped"})


@app.route("/ibkr/status", methods=["GET"])
def ibkr_status():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    _maybe_restore_ibkr_service(service)
    status_payload = service.status()
    status_payload["runtime_control"] = get_ibkr_runtime_control(_ibkr_service_environment(service))
    return jsonify({"ok": True, **status_payload})


@app.route("/ibkr/account", methods=["GET"])
def ibkr_account():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    try:
        return jsonify(_build_ibkr_account_snapshot(service))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/ibkr/positions", methods=["GET"])
def ibkr_positions():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": True,
            "environment": snapshot.get("environment"),
            "account_id": snapshot.get("account_id"),
            "positions": snapshot.get("positions", []),
            "count": snapshot.get("counts", {}).get("positions", 0),
            "fetched_at": snapshot.get("fetched_at"),
        }
    )


@app.route("/ibkr/orders/live", methods=["GET"])
def ibkr_live_orders():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": True,
            "environment": snapshot.get("environment"),
            "account_id": snapshot.get("account_id"),
            "orders": snapshot.get("orders", []),
            "count": snapshot.get("counts", {}).get("orders", 0),
            "open_count": snapshot.get("counts", {}).get("open_orders", 0),
            "fetched_at": snapshot.get("fetched_at"),
        }
    )


@app.route("/ibkr/orders/cancel", methods=["POST"])
def ibkr_cancel_order():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("id") or "").strip()
    acct_id = str(payload.get("account_id") or "").strip() or None
    if not order_id:
        return jsonify({"ok": False, "error": "Missing order_id"}), 400

    result = service.order_modifier.cancel_order(order_id, acct_id=acct_id)
    time.sleep(0.5)
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": bool(result.get("ok")),
            "action": "cancel_order",
            "order_id": order_id,
            "result": result,
            "snapshot": snapshot,
        }
    ), (200 if result.get("ok") else 500)


@app.route("/ibkr/orders/cancel_all", methods=["POST"])
def ibkr_cancel_all_orders():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    acct_id = str(payload.get("account_id") or "").strip() or None
    result = service.order_modifier.cancel_all_orders(acct_id=acct_id)
    time.sleep(0.5)
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": bool(result.get("ok")),
            "action": "cancel_all_orders",
            "result": result,
            "snapshot": snapshot,
        }
    ), (200 if result.get("ok") else 500)


@app.route("/ibkr/orders/modify", methods=["POST"])
def ibkr_modify_order():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("id") or "").strip()
    acct_id = str(payload.get("account_id") or "").strip() or None
    updates = {}

    if not order_id:
        return jsonify({"ok": False, "error": "Missing order_id"}), 400

    price = _coerce_float(payload.get("price"))
    quantity = _coerce_float(payload.get("quantity"))
    tif = str(payload.get("tif") or "").strip().upper()

    if price is not None:
        updates["price"] = price
    if quantity is not None:
        updates["quantity"] = quantity
    if tif:
        updates["tif"] = tif
    if not updates:
        return jsonify({"ok": False, "error": "No valid modify fields supplied"}), 400

    result = service.order_modifier.modify_order(order_id, updates, acct_id=acct_id)
    time.sleep(0.5)
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": bool(result.get("ok")),
            "action": "modify_order",
            "order_id": order_id,
            "updates": updates,
            "result": result,
            "snapshot": snapshot,
        }
    ), (200 if result.get("ok") else 500)


@app.route("/ibkr/orders/place", methods=["POST"])
def ibkr_place_order():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    runtime_environment = _ibkr_service_environment(service)
    if runtime_environment == "backtest":
        return jsonify({"ok": False, "error": "Backtest environment does not support live order placement"}), 400

    service_status = service.status() if hasattr(service, "status") else {}
    session_authenticated = bool((service_status.get("session") or {}).get("authenticated"))
    service_running = bool(getattr(service, "is_running", False) or getattr(service, "is_starting", False))
    if not service_running:
        return jsonify({"ok": False, "error": "IBKR service is not running"}), 409
    if not session_authenticated:
        return jsonify({"ok": False, "error": "IBKR session is not authenticated"}), 409
    if not hasattr(service, "order_placer") or not hasattr(service, "conid_resolver"):
        return jsonify({"ok": False, "error": "IBKR order components are unavailable"}), 503

    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol") or "").strip().upper()
    direction = str(payload.get("direction") or "").strip().lower()
    order_type = str(payload.get("order_type") or payload.get("entry_order_type") or "LMT").strip().upper()
    quantity_value = _coerce_float(payload.get("quantity"))
    conid = int(_coerce_float(payload.get("conid"), 0) or 0)
    entry_price = _coerce_float(payload.get("entry_price"))
    take_profit_price = _coerce_float(payload.get("take_profit_price"))
    stop_loss_price = _coerce_float(payload.get("stop_loss_price"))

    if not symbol:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    if direction not in {"long", "short"}:
        return jsonify({"ok": False, "error": "direction must be long or short"}), 400
    if order_type not in {"LMT", "MKT"}:
        return jsonify({"ok": False, "error": "order_type must be LMT or MKT"}), 400
    if quantity_value is None or quantity_value <= 0 or abs(quantity_value - round(quantity_value)) > 1e-9:
        return jsonify({"ok": False, "error": "quantity must be a positive integer"}), 400
    if take_profit_price is None or take_profit_price <= 0 or stop_loss_price is None or stop_loss_price <= 0:
        return jsonify({"ok": False, "error": "take_profit_price and stop_loss_price are required"}), 400
    if order_type == "LMT" and (entry_price is None or entry_price <= 0):
        return jsonify({"ok": False, "error": "entry_price is required for limit orders"}), 400

    quantity = int(round(quantity_value))
    if direction == "long" and take_profit_price <= stop_loss_price:
        return jsonify({"ok": False, "error": "For long orders, take profit must be above stop loss"}), 400
    if direction == "short" and take_profit_price >= stop_loss_price:
        return jsonify({"ok": False, "error": "For short orders, take profit must be below stop loss"}), 400
    if order_type == "LMT" and entry_price is not None:
        if direction == "long" and not (stop_loss_price < entry_price < take_profit_price):
            return jsonify({"ok": False, "error": "For long limit orders, stop < entry < take profit is required"}), 400
        if direction == "short" and not (take_profit_price < entry_price < stop_loss_price):
            return jsonify({"ok": False, "error": "For short limit orders, take profit < entry < stop is required"}), 400

    if conid <= 0:
        try:
            conid = int(service.conid_resolver.resolve(symbol) or 0)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Failed to resolve contract for {symbol}: {exc}"}), 500
    if conid <= 0:
        return jsonify({"ok": False, "error": f"Cannot resolve conid for {symbol}"}), 404

    signal_id = f"MANUAL_{runtime_environment.upper()}_{symbol}_{int(time.time())}"
    result = service.order_placer.place_bracket_order(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=float(entry_price or 0.0),
        take_profit_price=float(take_profit_price),
        stop_loss_price=float(stop_loss_price),
        use_paper=_ibkr_service_uses_paper_account(service),
        signal_id=signal_id,
        entry_order_type=order_type,
    )

    time.sleep(0.75)
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": bool(result.get("ok")),
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "conid": conid,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "signal_id": signal_id,
            "result": result,
            "snapshot": snapshot,
        }
    ), (200 if result.get("ok") else 500)


@app.route("/ibkr/positions/close", methods=["POST"])
def ibkr_close_position():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    conid = int(_coerce_float(payload.get("conid"), 0) or 0)
    symbol = str(payload.get("symbol") or "").strip().upper()
    position_value = _coerce_float(payload.get("position"))
    quantity = _coerce_float(payload.get("quantity"))

    if quantity is None and position_value is not None:
        quantity = abs(float(position_value))
    elif quantity is not None:
        quantity = abs(float(quantity))

    direction = str(payload.get("direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        if position_value is not None:
            direction = "long" if float(position_value) > 0 else "short"
        else:
            direction = "long"

    if conid <= 0 or not symbol or not quantity or quantity <= 0:
        return jsonify({"ok": False, "error": "Missing conid/symbol/quantity"}), 400

    result = service.order_placer.place_market_close(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        use_paper=_ibkr_service_uses_paper_account(service),
    )
    time.sleep(0.75)
    snapshot = _build_ibkr_account_snapshot(service)
    return jsonify(
        {
            "ok": bool(result.get("ok")),
            "action": "close_position",
            "symbol": symbol,
            "conid": conid,
            "quantity": quantity,
            "direction": direction,
            "result": result,
            "snapshot": snapshot,
        }
    ), (200 if result.get("ok") else 500)


@app.route("/ibkr/dashboard", methods=["GET"])
def ibkr_dashboard():
    return redirect(f"{PB_PUBLIC_URL.rstrip('/')}/ibkr_runtime.html", code=302)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
