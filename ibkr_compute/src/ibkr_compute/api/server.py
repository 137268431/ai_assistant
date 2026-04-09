"""
IBKR Compute — 指标计算 HTTP 服务
由 PB cron 触发, 常驻内存维护 IndicatorEngine 热缓存

端点:
  POST /compute     — 读最新 ibkr_bars, 更新引擎, 写 ibkr_indicators / ibkr_signals
  POST /scan        — 盘前扫描, 写 ibkr_targets
  POST /recompute   — 全量重算 (清空缓存, 从 ibkr_bars 历史重建)
  POST /chart/timeline — 基于 ibkr_bars 现算图表指标 / 信号时间线
  GET  /contracts/search — 通过 IBKR API 搜索可用合约候选
  GET  /screener    — 基于 watchlist/bars/indicators/targets 聚合盘前筛选数据
  GET  /health      — 健康检查
  GET  /status      — 引擎状态
"""

import json
import logging
import os
import sys
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

import requests
from flask import Flask, Response, jsonify, redirect, request

from ibkr_compute.backtest import BacktestService
from ibkr_compute.core.config import Config
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    HIGHER_INTERVALS,
    build_runtime_timestamps,
    classify_session,
    build_signal_id,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
    normalize_interval,
)
from ibkr_compute.workflows.daily_scanner import DailyScanner


def _register_canonical_module_alias():
    module = sys.modules.get(__name__)
    spec = globals().get("__spec__")
    canonical_name = str(getattr(spec, "name", "") or "").strip()
    if not module or not canonical_name:
        return
    existing = sys.modules.get(canonical_name)
    if existing is None or existing is module:
        sys.modules[canonical_name] = module


_register_canonical_module_alias()

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
ibkr_account_snapshot_cache = {}
ibkr_account_snapshot_cache_lock = threading.Lock()

INTERVALS = list(COMPUTE_INTERVALS)
SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
IBKR_SCRIPT_TAG = os.environ.get("IBKR_SCRIPT_TAG", "IBKR_SAC_v1_20260403")
BOOTSTRAP_LOOKBACK_BARS = {
    # Intraday engines need at least 200 bars to satisfy DTP readiness after restart.
    # Keep a small warmup margin so fresh indicators resume immediately instead of
    # waiting for several new bars after every deploy/restart.
    "5m": 260,
    "15m": 260,
    "30m": 260,
    "1h": 260,
    "4h": 260,
    "1d": 260,
}
MATERIALIZE_MAX_WORKERS = max(1, min(8, int(os.environ.get("IBKR_MATERIALIZE_MAX_WORKERS", "1"))))
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
CHART_TIMELINE_VISIBLE_LIMIT = max(200, int(os.environ.get("IBKR_CHART_TIMELINE_VISIBLE_LIMIT", "3000")))
IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS = max(
    1.0,
    float(os.environ.get("IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS", "3.0")),
)
COMPUTE_CURSOR_STATE_KEY = "compute_cursors"
COMPUTE_CURSOR_STATE_DATE = "global"
IBKR_RUNTIME_CONTROL_STATE_KEY = "ibkr_runtime_control"
IBKR_RUNTIME_CONTROL_STATE_DATE = "global"
DEFAULT_TRADE_WINDOW_START = (9, 35)
DEFAULT_TRADE_WINDOW_END = (15, 30)
DEFAULT_ORDER_WINDOW_END = (15, 0)


def current_market_date(now: datetime | None = None) -> str:
    et_now = now.astimezone(timezone(timedelta(hours=-4))) if now else datetime.now(timezone(timedelta(hours=-4)))
    return et_now.strftime("%Y-%m-%d")


def get_environment_time_window(environment: str, key: str, default: tuple[int, int]) -> tuple[int, int]:
    raw_value = str(
        cfg.get_for_environment(key, environment, f"{default[0]:02d}:{default[1]:02d}") or ""
    ).strip()
    try:
        hour_text, minute_text = raw_value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return default


def resolve_initial_signal_state(environment: str, bar_ms: int) -> tuple[str, str]:
    manual_confirm_enabled = cfg.get_bool_for_environment(
        "signal_manual_confirm_enabled",
        environment,
        True,
    )

    if bar_ms <= 0:
        return (
            ("awaiting_confirm", "manual_confirmation_required")
            if manual_confirm_enabled
            else ("pending", "")
        )

    signal_time = ms_to_et(bar_ms)
    current = (signal_time.hour, signal_time.minute)
    trade_start = get_environment_time_window(environment, "trade_window_start_time", DEFAULT_TRADE_WINDOW_START)
    trade_end = get_environment_time_window(environment, "trade_window_end_time", DEFAULT_TRADE_WINDOW_END)
    order_end = get_environment_time_window(environment, "order_window_end_time", DEFAULT_ORDER_WINDOW_END)

    if not (trade_start <= current <= trade_end):
        return "rejected", "outside_trade_window"
    if not (trade_start <= current <= order_end):
        return "rejected", "outside_order_window"
    if manual_confirm_enabled:
        return "awaiting_confirm", "manual_confirmation_required"
    return "pending", ""


def build_bar_environment_filter(environment: str, include_legacy_empty: bool = False) -> str:
    runtime_environment = str(environment or "").strip().lower() or "live"
    clauses = [f'environment = "{runtime_environment}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR = "market_monitor"
VALID_WATCHLIST_SYMBOL_ROLES = {
    WATCHLIST_SYMBOL_ROLE_TRADE,
    WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR,
}


def normalize_watchlist_symbol_role(value, default: str = WATCHLIST_SYMBOL_ROLE_TRADE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_WATCHLIST_SYMBOL_ROLES:
        return normalized
    return default


def normalize_symbols(symbols) -> list[str]:
    normalized = []
    seen = set()
    if isinstance(symbols, str):
        symbols = [symbols]
    for raw_symbol in (symbols or []):
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def build_symbol_filter(symbols) -> str:
    normalized = normalize_symbols(symbols)
    if not normalized:
        return ""
    return "(" + " || ".join(f'symbol = "{symbol}"' for symbol in normalized) + ")"


def normalize_bar_environment(bar: dict, environment: str) -> dict:
    payload = dict(bar)
    payload["environment"] = str(environment or "live").strip().lower() or "live"
    return payload


def get_market_monitor_symbols(environment: str) -> set[str]:
    watchlist_map = load_effective_watchlist(environment)
    return {
        symbol
        for symbol, row in watchlist_map.items()
        if normalize_watchlist_symbol_role((row or {}).get("symbol_role")) == WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    }


def get_signal_generator_params(environment: str) -> dict:
    market_monitor_symbols = sorted(get_market_monitor_symbols(environment))
    return {
        "market_monitor_symbols": ",".join(market_monitor_symbols),
    }


def get_or_create_engine(environment: str, symbol: str, interval: str) -> IndicatorEngine:
    key = (environment, symbol, interval)
    signal_params = get_signal_generator_params(environment)
    with compute_lock:
        if key not in engines:
            engines[key] = IndicatorEngine(symbol, interval)
            signal_gens[key] = SignalGenerator(symbol, interval, params=signal_params)
        else:
            signal_generator = signal_gens.get(key)
            if signal_generator:
                signal_generator.set_params(signal_params)
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


def bootstrap_engine_state(
    environment: str,
    symbol: str,
    interval: str,
    before_bar_time_ms: int,
    inclusive: bool = True,
    force_rebuild: bool = False,
):
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    key = (runtime_environment, symbol, normalized_interval)
    engine = get_or_create_engine(runtime_environment, symbol, normalized_interval)
    signal_generator = signal_gens.get(key)
    target_ms = int(before_bar_time_ms or 0)

    if force_rebuild:
        engine_bootstrap_checked.discard(key)

    if not force_rebuild and key in engine_bootstrap_checked and (target_ms <= 0 or engine.last_bar_time_ms >= target_ms):
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


def materialize_engines_from_storage(environment: str, symbols, interval: str = "5m") -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    normalized_symbols = normalize_symbols(symbols)
    if not normalized_symbols:
        return {}
    started_at = time.perf_counter()

    symbol_filters = " || ".join(f'symbol = "{symbol}"' for symbol in normalized_symbols)
    rows = pb.get_all_records(
        "ibkr_bars",
        filter=(
            f'interval = "{normalized_interval}" && '
            f'{build_bar_environment_filter(runtime_environment, include_legacy_empty=True)} && '
            f'({symbol_filters})'
        ),
        sort="-bar_time_ms",
        max_pages=max(4, len(normalized_symbols)),
    )

    latest_by_symbol = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        bar_ms = int(row.get("bar_time_ms", 0) or 0)
        if symbol and bar_ms > 0 and symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = bar_ms
        if len(latest_by_symbol) >= len(normalized_symbols):
            break

    def _materialize_symbol(symbol: str) -> dict:
        target_ms = int(latest_by_symbol.get(symbol, 0) or 0)
        if target_ms <= 0:
            return {
                "processed": 0,
                "bar_count": 0,
                "last_bar_time_ms": 0,
                "is_ready": False,
                "reason": "no_stored_bars",
            }

        existing_engine = engines.get((runtime_environment, symbol, normalized_interval))
        force_rebuild = bool(existing_engine and not existing_engine.is_ready())
        processed = bootstrap_engine_state(
            runtime_environment,
            symbol,
            normalized_interval,
            target_ms,
            inclusive=True,
            force_rebuild=force_rebuild,
        )
        engine = engines.get((runtime_environment, symbol, normalized_interval))
        return {
            "processed": processed,
            "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
            "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
            "is_ready": bool(engine and engine.is_ready()),
            "reason": (
                "rebootstrapped"
                if force_rebuild and processed > 0
                else "bootstrapped"
                if processed > 0
                else "already_materialized"
            ),
        }

    results = {}
    worker_count = min(MATERIALIZE_MAX_WORKERS, len(normalized_symbols))
    if worker_count <= 1:
        for symbol in normalized_symbols:
            results[symbol] = _materialize_symbol(symbol)
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="materialize-engine") as executor:
            future_map = {
                executor.submit(_materialize_symbol, symbol): symbol
                for symbol in normalized_symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    traceback.print_exc()
                    results[symbol] = {
                        "processed": 0,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "is_ready": False,
                        "reason": "materialize_failed",
                    }

    ready_count = sum(1 for item in results.values() if bool((item or {}).get("is_ready")))
    logger.info(
        "Materialized engines from storage: env=%s interval=%s symbols=%d ready=%d workers=%d elapsed_s=%.3f",
        runtime_environment,
        normalized_interval,
        len(normalized_symbols),
        ready_count,
        worker_count,
        time.perf_counter() - started_at,
    )
    return results


def reset_compute_state_for_symbols(environment: str, symbols, intervals=None) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = set(normalize_symbols(symbols))
    interval_filter = {normalize_interval(interval) for interval in (intervals or INTERVALS)}
    removed = {"engines": 0, "signal_gens": 0, "cursors": 0, "bootstraps": 0}

    if not normalized_symbols:
        return removed

    for key in list(engines.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            engines[key].reset()
            removed["engines"] += 1

    for key in list(signal_gens.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            signal_gens[key].daily_reset()
            removed["signal_gens"] += 1

    for key in list(last_processed_ms.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            last_processed_ms.pop(key, None)
            removed["cursors"] += 1

    for key in list(engine_bootstrap_checked):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            engine_bootstrap_checked.discard(key)
            removed["bootstraps"] += 1

    return removed




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


def coerce_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return number


def coerce_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def load_effective_watchlist(environment: str) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    rows = pb.get_all_records(
        "watchlist",
        filter=(
            f'environment = "{runtime_environment}" '
            '|| environment = "global" '
            '|| environment = ""'
        ),
        sort="-updated",
        max_pages=30,
    )
    merged = {}
    applied = {}
    priority = {"": 0, "global": 1, runtime_environment: 2}
    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        row_environment = str(row.get("environment", "") or "").strip().lower()
        rank = priority.get(row_environment, -1)
        if rank < 0:
            continue
        if symbol in applied and applied[symbol] > rank:
            continue
        applied[symbol] = rank
        normalized_row = dict(row)
        normalized_row["symbol_role"] = normalize_watchlist_symbol_role((row or {}).get("symbol_role"))
        merged[symbol] = normalized_row
    return merged


def parse_market_date_bounds_ms(market_date: str) -> tuple[int, int]:
    et = timezone(timedelta(hours=-4))
    start_dt = datetime.strptime(str(market_date or "").strip(), "%Y-%m-%d").replace(
        tzinfo=et,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def build_tradability_assessment(row: dict) -> tuple[int, list[str]]:
    score = 0
    notes = []

    price = coerce_float(row.get("price"))
    avg_10d_volume = coerce_float(row.get("avg_10d_volume"))
    premarket_volume = coerce_float(row.get("premarket_volume"))
    today_volume = coerce_float(row.get("today_volume"))
    atr_pct = abs(coerce_float(row.get("atr_pct")))
    day_change_pct = abs(coerce_float(row.get("day_change_pct")))
    freshness_min = row.get("freshness_min")
    target_score = coerce_float(row.get("target_score"))

    if 2 <= price <= 80:
        score += 12
        notes.append("价位适中")
    elif 1 <= price <= 150:
        score += 6

    if avg_10d_volume >= 5_000_000:
        score += 18
        notes.append("10日均量>500万")
    elif avg_10d_volume >= 1_000_000:
        score += 12
        notes.append("10日均量>100万")
    elif avg_10d_volume >= 500_000:
        score += 6

    if premarket_volume >= 500_000:
        score += 18
        notes.append("盘前量能>50万")
    elif premarket_volume >= 100_000:
        score += 12
        notes.append("盘前量能>10万")
    elif today_volume >= 300_000:
        score += 8
        notes.append("当日成交活跃")

    if 2 <= atr_pct <= 12:
        score += 16
        notes.append("ATR波动充足")
    elif 1 <= atr_pct <= 20:
        score += 8

    if day_change_pct >= 2:
        score += 12
        notes.append("日内波动>2%")
    elif day_change_pct >= 0.8:
        score += 6

    if isinstance(freshness_min, int):
        if freshness_min <= 20:
            score += 14
            notes.append("bars新鲜")
        elif freshness_min <= 60:
            score += 8
        elif freshness_min <= 180:
            score += 3

    if target_score >= 10:
        score += 10
        notes.append("已入目标池")
    elif target_score >= 5:
        score += 6

    if str(row.get("direction_bias") or "").strip().lower() in {"long", "short"}:
        score += 4

    return min(100, score), notes


def build_screener_payload(
    environment: str,
    market_date: str | None = None,
    symbols=None,
    limit: int = 0,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    selected_symbols = normalize_symbols(symbols)
    selected_set = set(selected_symbols)
    market_date = str(market_date or current_market_date()).strip() or current_market_date()
    market_start_ms, market_end_ms = parse_market_date_bounds_ms(market_date)
    now_ms = int(time.time() * 1000)

    refresh_daily_close_cache([runtime_environment])
    watchlist_map_all = load_effective_watchlist(runtime_environment)
    watchlist_map = {
        symbol: row
        for symbol, row in watchlist_map_all.items()
        if normalize_watchlist_symbol_role((row or {}).get("symbol_role")) == WATCHLIST_SYMBOL_ROLE_TRADE
    }
    market_monitor_symbols = {
        symbol
        for symbol, row in watchlist_map_all.items()
        if normalize_watchlist_symbol_role((row or {}).get("symbol_role")) == WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    }
    metadata_map = refresh_symbol_metadata()

    universe_symbols = selected_symbols or sorted(watchlist_map.keys())
    if selected_set and not universe_symbols:
        universe_symbols = sorted(selected_set)
    universe_set = set(universe_symbols)

    target_rows = pb.get_all_records(
        "ibkr_targets",
        filter=f'date = "{market_date}" && environment = "{runtime_environment}"',
        sort="-updated",
        max_pages=20,
    )
    target_by_symbol = {}
    for row in target_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol in market_monitor_symbols:
            continue
        if symbol and symbol not in target_by_symbol:
            target_by_symbol[symbol] = row
            if symbol not in universe_set and not selected_set:
                universe_symbols.append(symbol)
                universe_set.add(symbol)

    if not universe_symbols:
        timestamps = build_runtime_timestamps()
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            **timestamps,
            "summary": {
                "total": 0,
                "with_live_bars": 0,
                "operable": 0,
                "candidate_targets": 0,
                "active_targets": 0,
            },
            "filters": {
                "exchanges": [],
                "industries": [],
                "target_statuses": [],
                "direction_biases": [],
            },
            "items": [],
        }

    symbol_filter = build_symbol_filter(universe_symbols)
    lookback_daily_ms = max(0, market_start_ms - interval_to_ms("1d") * 20)
    daily_filter_parts = [
        'interval = "1d"',
        build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
        f"bar_time_ms >= {lookback_daily_ms}",
        f"bar_time_ms < {market_end_ms}",
    ]
    if symbol_filter:
        daily_filter_parts.append(symbol_filter)
    daily_rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(daily_filter_parts),
        sort="bar_time_ms",
        max_pages=100,
    )

    intraday_filter_parts = [
        'interval = "5m"',
        build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
        f"bar_time_ms >= {market_start_ms}",
        f"bar_time_ms < {market_end_ms}",
    ]
    if symbol_filter:
        intraday_filter_parts.append(symbol_filter)
    intraday_rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(intraday_filter_parts),
        sort="bar_time_ms",
        max_pages=400,
    )

    indicator_filter_parts = [
        f'interval = "{interval_to_chart_tf("5m")}"',
        f'environment = "{runtime_environment}"',
        f"bar_time_ms >= {max(0, market_start_ms - interval_to_ms('1d') * 5)}",
    ]
    if symbol_filter:
        indicator_filter_parts.append(symbol_filter)
    indicator_rows = pb.get_all_records(
        "ibkr_indicators",
        filter=" && ".join(indicator_filter_parts),
        sort="-bar_time_ms",
        max_pages=120,
    )

    daily_bars_by_symbol = {}
    fallback_daily_by_symbol = {}
    for row in daily_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set:
            continue
        bar_ms = coerce_int(row.get("bar_time_ms"))
        close = coerce_float(row.get("close"))
        if bar_ms <= 0 or close <= 0:
            continue
        daily_bars_by_symbol.setdefault(symbol, []).append(row)
        row_date = ms_to_et(bar_ms).strftime("%Y-%m-%d")
        if row_date < market_date:
            fallback_daily_by_symbol[symbol] = row

    latest_intraday_by_symbol = {}
    volume_stats_by_symbol = {}
    for row in intraday_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set:
            continue
        bar_ms = coerce_int(row.get("bar_time_ms"))
        if bar_ms <= 0:
            continue
        volume = coerce_float(row.get("volume"))
        session_type = str(row.get("session_type", "") or "").strip().lower() or classify_session(
            bar_time_ms=bar_ms
        )
        latest_intraday_by_symbol[symbol] = row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "today": 0.0})
        stats["today"] += volume
        if session_type == "premarket":
            stats["premarket"] += volume

    latest_indicator_by_symbol = {}
    for row in indicator_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set or symbol in latest_indicator_by_symbol:
            continue
        latest_indicator_by_symbol[symbol] = row

    items = []
    exchange_values = set()
    industry_values = set()
    target_status_values = set()
    direction_bias_values = set()
    candidate_targets = 0
    active_targets = 0

    for symbol in universe_symbols:
        base_meta = watchlist_map.get(symbol) or metadata_map.get(symbol) or {}
        latest_intraday = latest_intraday_by_symbol.get(symbol)
        latest_daily = fallback_daily_by_symbol.get(symbol)
        latest_target = target_by_symbol.get(symbol)
        latest_indicator = latest_indicator_by_symbol.get(symbol)
        indicator_extra = parse_json_object((latest_indicator or {}).get("extra"))

        intraday_bar_ms = coerce_int((latest_intraday or {}).get("bar_time_ms"))
        daily_bar_ms = coerce_int((latest_daily or {}).get("bar_time_ms"))
        price = coerce_float((latest_intraday or {}).get("close"))
        price_source = "5m"
        if price <= 0:
            price = coerce_float((latest_daily or {}).get("close"))
            price_source = "1d_close" if price > 0 else ""

        compare_bar_ms = intraday_bar_ms or market_start_ms
        latest_bar_time_ms = intraday_bar_ms or daily_bar_ms
        latest_us_time = str((latest_intraday or {}).get("us_time") or (latest_daily or {}).get("us_time") or "")
        latest_session_type = str((latest_intraday or {}).get("session_type") or "").strip().lower()
        if intraday_bar_ms > 0 and not latest_session_type:
            latest_session_type = classify_session(bar_time_ms=intraday_bar_ms)

        daily_history = [
            row
            for row in daily_bars_by_symbol.get(symbol, [])
            if ms_to_et(coerce_int(row.get("bar_time_ms"))).strftime("%Y-%m-%d") < market_date
        ]
        last_10_daily = daily_history[-10:]
        avg_10d_volume = (
            round(
                sum(coerce_float(row.get("volume")) for row in last_10_daily) / len(last_10_daily),
                2,
            )
            if last_10_daily else 0.0
        )

        daily_fields = get_daily_change_fields(runtime_environment, symbol, price, compare_bar_ms) if price > 0 else {
            "day_change_pct": 0.0,
            "prev_close_change_pct": 0.0,
            "change_7d": 0.0,
        }

        target_status = str((latest_target or {}).get("status") or "").strip().lower()
        direction_bias = str((latest_target or {}).get("direction_bias") or "neutral").strip().lower() or "neutral"
        target_score = round(coerce_float((latest_target or {}).get("score")), 2)
        scan_reason = str((latest_target or {}).get("scan_reason") or "").strip()
        if target_status == "candidate":
            candidate_targets += 1
        elif target_status == "active":
            active_targets += 1

        volume_stats = volume_stats_by_symbol.get(symbol, {})
        freshness_min = None
        if intraday_bar_ms > 0:
            freshness_min = max(0, int((now_ms - intraday_bar_ms) // 60000))

        exchange = (
            str((base_meta or {}).get("exchange") or (latest_intraday or {}).get("exchange") or (latest_target or {}).get("exchange") or "")
            .strip()
            .upper()
        )
        industry = str((base_meta or {}).get("industry") or "").strip()
        note = str((base_meta or {}).get("note") or "").strip()
        atr_pct = round(coerce_float(indicator_extra.get("atr_pct", (latest_indicator or {}).get("atr_pct"))), 2)

        row = {
            "symbol": symbol,
            "exchange": exchange,
            "industry": industry,
            "note": note,
            "price": round(price, 4) if price > 0 else 0.0,
            "price_source": price_source,
            "atr_pct": atr_pct,
            "avg_10d_volume": avg_10d_volume,
            "premarket_volume": round(coerce_float(volume_stats.get("premarket")), 2),
            "today_volume": round(coerce_float(volume_stats.get("today")), 2),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_intraday_bar_time_ms": intraday_bar_ms,
            "latest_us_time": latest_us_time,
            "latest_session_type": latest_session_type,
            "freshness_min": freshness_min,
            "has_live_bar": intraday_bar_ms > 0,
            "target_status": target_status,
            "target_score": target_score,
            "direction_bias": direction_bias,
            "scan_reason": scan_reason,
            **daily_fields,
        }
        tradability_score, operable_reasons = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = operable_reasons
        row["is_operable"] = bool(
            row["has_live_bar"]
            and row["price"] > 0
            and row["avg_10d_volume"] >= 500_000
            and row["tradability_score"] >= 60
            and isinstance(row["freshness_min"], int)
            and row["freshness_min"] <= 90
        )
        items.append(row)

        if exchange:
            exchange_values.add(exchange)
        if industry:
            industry_values.add(industry)
        if target_status:
            target_status_values.add(target_status)
        if direction_bias:
            direction_bias_values.add(direction_bias)

    items.sort(
        key=lambda item: (
            0 if item.get("is_operable") else 1,
            -coerce_float(item.get("tradability_score")),
            -coerce_float(item.get("target_score")),
            -coerce_float(item.get("premarket_volume")),
            -coerce_float(item.get("avg_10d_volume")),
            item.get("symbol", ""),
        )
    )
    if limit > 0:
        items = items[:limit]

    timestamps = build_runtime_timestamps()
    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        **timestamps,
        "summary": {
            "total": len(items),
            "with_live_bars": sum(1 for item in items if item.get("has_live_bar")),
            "operable": sum(1 for item in items if item.get("is_operable")),
            "candidate_targets": candidate_targets,
            "active_targets": active_targets,
            "avg_premarket_volume": round(
                sum(coerce_float(item.get("premarket_volume")) for item in items) / len(items),
                2,
            ) if items else 0.0,
        },
        "filters": {
            "exchanges": sorted(exchange_values),
            "industries": sorted(industry_values),
            "target_statuses": sorted(target_status_values),
            "direction_biases": sorted(direction_bias_values),
        },
        "items": items,
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


def has_interval_bars(environment: str, interval: str, symbols=None) -> bool:
    symbol_filter = build_symbol_filter(symbols)
    filter_parts = [
        f'interval = "{interval}"',
        build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    try:
        rows = pb.get_records(
            "ibkr_bars",
            filter=" && ".join(filter_parts),
            sort="-bar_time_ms",
            per_page=1,
            page=1,
        )
        return bool(rows)
    except Exception:
        traceback.print_exc()
        return False


def rebuild_higher_timeframe_bars(environment: str, symbols=None, intervals=None) -> dict:
    normalized_symbols = normalize_symbols(symbols)
    target_intervals = [normalize_interval(interval) for interval in (intervals or HIGHER_INTERVALS)]
    target_intervals = [interval for interval in target_intervals if interval in HIGHER_INTERVALS]
    symbol_filter = build_symbol_filter(normalized_symbols)
    filter_parts = [
        'interval = "5m"',
        build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    base_rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="bar_time_ms",
        max_pages=1000,
    )
    if not base_rows:
        return {
            "processed_5m": 0,
            "written": 0,
            "errors": 0,
            "symbols": normalized_symbols,
            "intervals": target_intervals,
        }

    builder = TimeframeBarBuilder(target_intervals=target_intervals)
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
    return {
        "processed_5m": len(rows),
        "written": written,
        "errors": errors,
        "symbols": normalized_symbols,
        "intervals": target_intervals,
    }


def ensure_higher_timeframe_bars(environments, force: bool = False, symbols=None):
    normalized_symbols = normalize_symbols(symbols)
    results = {}
    for environment in environments:
        if normalized_symbols:
            rollup_result = rebuild_higher_timeframe_bars(
                environment,
                symbols=normalized_symbols,
                intervals=HIGHER_INTERVALS,
            )
            rollup_result["targeted"] = True
            results[environment] = rollup_result
            continue

        if not force and environment in rollup_bootstrap_checked:
            results[environment] = {"skipped": True, "reason": "already_checked", "written": 0, "errors": 0}
            continue

        missing_intervals = [
            interval for interval in HIGHER_INTERVALS
            if force or not has_interval_bars(environment, interval)
        ]
        if not missing_intervals:
            rollup_bootstrap_checked.add(environment)
            results[environment] = {"skipped": True, "reason": "already_present", "written": 0, "errors": 0}
            continue

        rollup_result = rebuild_higher_timeframe_bars(environment)
        rollup_result["missing_intervals"] = missing_intervals
        results[environment] = rollup_result
        rollup_bootstrap_checked.add(environment)
    return results


def fetch_interval_bars(environment: str, interval: str, symbols=None, full_scan: bool = False):
    symbol_filter = build_symbol_filter(symbols)
    filter_parts = [
        f'interval = "{interval}"',
        build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    if not full_scan:
        filter_parts.append(f"bar_time_ms >= {get_fetch_since_ms(environment, interval)}")
    rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="bar_time_ms",
        max_pages=500,
    )
    if rows:
        last_interval_fetch_ms[(environment, interval)] = max(
            int(row.get("bar_time_ms", 0) or 0) for row in rows
        )
    return rows


def repair_symbol_pipeline_from_storage(environment: str, symbols) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = normalize_symbols(symbols)
    if not normalized_symbols:
        return {"ok": True, "symbols": [], "rollup": {}, "compute": {}, "reset": {}}

    rollup_result = rebuild_higher_timeframe_bars(
        runtime_environment,
        symbols=normalized_symbols,
        intervals=HIGHER_INTERVALS,
    )
    reset_result = reset_compute_state_for_symbols(
        runtime_environment,
        normalized_symbols,
        intervals=INTERVALS,
    )
    compute_payload = {}
    with app.test_request_context(
        "/compute",
        method="POST",
        json={
            "source": "history_repair",
            "environments": [runtime_environment],
            "symbols": normalized_symbols,
            "force_rollup": True,
        },
    ):
        response = compute()
        try:
            compute_payload = response.get_json() or {}
        except Exception:
            compute_payload = {}

    return {
        "ok": bool(compute_payload.get("ok", True)),
        "symbols": normalized_symbols,
        "rollup": rollup_result,
        "reset": reset_result,
        "compute": compute_payload,
    }


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
    initial_status, initial_status_reason = resolve_initial_signal_state(environment, bar_ms)
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
        "signal_source": "ibkr_compute_realtime",
        "signal_source_label": "IBKR 实时计算",
        "signal_source_detail": "来自 IBKR 实盘 bars 收盘计算",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
        "status_reason": initial_status_reason,
        "initial_status": initial_status,
        "initial_status_reason": initial_status_reason,
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
        "status": initial_status,
        "note": initial_status_reason,
        "extra": signal_extra,
    }


def load_chart_timeline_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    visible_pages = max(1, (CHART_TIMELINE_VISIBLE_LIMIT + 199) // 200 + 1)
    warmup_pages = max(1, (warmup_bars + 199) // 200 + 1)

    base_filter_parts = [
        f'symbol = "{normalized_symbol}"',
        f'interval = "{normalized_interval}"',
        build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
    ]
    if end_ms > 0:
        base_filter_parts.append(f"bar_time_ms <= {int(end_ms)}")

    visible_filter_parts = list(base_filter_parts)
    if start_ms > 0:
        visible_filter_parts.append(f"bar_time_ms >= {int(start_ms)}")

    visible_rows = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(visible_filter_parts),
        sort="bar_time_ms",
        max_pages=visible_pages,
    )
    if len(visible_rows) > CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_rows = []
    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_filter_parts = list(base_filter_parts)
        warmup_filter_parts.append(f"bar_time_ms < {warmup_anchor_ms}")
        warmup_rows = pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(warmup_filter_parts),
            sort="-bar_time_ms",
            max_pages=warmup_pages,
        )
        warmup_rows = list(reversed(warmup_rows[:warmup_bars]))

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


def build_chart_indicator_row(environment: str, symbol: str, interval: str, row: dict) -> dict:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    daily_fields = get_daily_change_fields(environment, normalized_symbol, float(row.get("close", 0) or 0), bar_ms)
    timestamps = build_runtime_timestamps()
    indicator = {
        **{key: value for key, value in (row or {}).items() if key != "signal"},
        **daily_fields,
        **timestamps,
        "environment": environment,
        "symbol": normalized_symbol,
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
    }
    return indicator


def build_chart_signal_row(environment: str, symbol: str, interval: str, row: dict) -> dict | None:
    raw_signal = row.get("signal")
    if not raw_signal:
        return None

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    bar_index = int(row.get("bar_index", row.get("bar_count", 0)) or 0)
    daily_fields = get_daily_change_fields(environment, normalized_symbol, float(row.get("close", 0) or 0), bar_ms)
    signal_type = str(raw_signal.get("signal", "") or "")
    signal_extra = dict(raw_signal.get("extra") or {})
    signal_extra.update(
        {
            **daily_fields,
            **build_runtime_timestamps(),
            "chart_tf": chart_tf,
            "bar_time_ms": bar_ms,
            "bar_index": bar_index,
            "close": round(float(row.get("close", 0) or 0), 2),
            "atr": signal_extra.get("atr_raw", signal_extra.get("atr", row.get("atr", 0))),
            "atr_pct": row.get("atr_pct", signal_extra.get("atr_pct", 0)),
            "environment": environment,
            "source": "ibkr_compute_timeline",
            "signal_source": "ibkr_compute_timeline",
            "signal_source_label": "IBKR 图表回放",
            "signal_source_detail": "来自缓存 bars 时间线重算",
            "computed_from": "ibkr_bars",
        }
    )

    rr_value = raw_signal.get("rr", "")
    rr_text = f"{float(rr_value):.1f}:1" if isinstance(rr_value, (int, float)) else str(rr_value or "")

    return {
        "environment": environment,
        "symbol": normalized_symbol,
        "signal_id": build_signal_id(normalized_symbol, bar_ms, signal_type),
        "direction": raw_signal.get("direction", ""),
        "signal": signal_type,
        "limit_price": round(float(row.get("close", 0) or 0), 2),
        "entry": raw_signal.get("entry", 0),
        "stop_loss": raw_signal.get("stop_loss", 0),
        "take_profit": raw_signal.get("take_profit", 0),
        "rr": rr_text,
        "shares": raw_signal.get("shares", 0),
        "exchange": str(row.get("exchange", "") or "").upper(),
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "reason": raw_signal.get("reason", ""),
        "us_time": row.get("us_time", ""),
        "cn_time": row.get("cn_time", ""),
        "date": str(row.get("us_time", "") or "")[:10],
        "bar_time_ms": bar_ms,
        "bar_index": bar_index,
        "status": "computed",
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
        "extra": signal_extra,
    }


def build_chart_timeline_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)

    refresh_daily_close_cache([runtime_environment])
    source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    source_rows = source.get("source_rows") or []
    visible_rows = source.get("visible_rows") or []

    if not visible_rows:
        return {
            "ok": True,
            "bars": [],
            "indicator_timeline": [],
            "latest_indicator": None,
            "signals": [],
            "meta": {
                "environment": runtime_environment,
                "symbol": normalized_symbol,
                "interval": chart_tf,
                "start_ms": int(start_ms or 0),
                "end_ms": int(end_ms or 0),
                "visible_bar_count": 0,
                "source_bar_count": len(source_rows),
                "warmup_bars": int(source.get("warmup_limit", 0) or 0),
                "warmup_used": int(source.get("warmup_used", 0) or 0),
                "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
                "reason": "no_visible_bars",
            },
        }

    timeline = build_runtime_timeline(
        normalized_symbol,
        normalized_interval,
        source_rows,
        params=get_signal_generator_params(runtime_environment),
        include_signals=include_signals,
        visible_start_ms=int(start_ms or 0),
        visible_end_ms=int(end_ms or 0),
    )
    timeline_rows = timeline.get("rows") or []
    bars = [
        {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "exchange": str(row.get("exchange", "") or "").upper(),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": row.get("us_time", ""),
            "cn_time": row.get("cn_time", ""),
            "session_type": row.get("session_type", "regular"),
            "open": round(float(row.get("open", 0) or 0), 4),
            "high": round(float(row.get("high", 0) or 0), 4),
            "low": round(float(row.get("low", 0) or 0), 4),
            "close": round(float(row.get("close", 0) or 0), 4),
            "volume": round(float(row.get("volume", 0) or 0), 4),
        }
        for row in timeline_rows
    ]
    indicators = [
        build_chart_indicator_row(runtime_environment, normalized_symbol, normalized_interval, row)
        for row in timeline_rows
    ]
    signals = []
    if include_signals and normalized_interval == "5m":
        signals = [
            signal
            for signal in (
                build_chart_signal_row(runtime_environment, normalized_symbol, normalized_interval, row)
                for row in timeline_rows
            )
            if signal
        ]

    return {
        "ok": True,
        "bars": bars,
        "indicator_timeline": indicators,
        "latest_indicator": indicators[-1] if indicators else None,
        "signals": signals,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "visible_bar_count": len(bars),
            "source_bar_count": len(source_rows),
            "warmup_bars": int(source.get("warmup_limit", 0) or 0),
            "warmup_used": int(source.get("warmup_used", 0) or 0),
            "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
        },
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


def get_requested_symbols(payload=None):
    payload = payload if isinstance(payload, dict) else (request.get_json(silent=True) or {})
    requested = payload.get("symbols")
    if requested is None:
        requested = payload.get("symbol")
    return normalize_symbols(requested)


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
        source = str(payload.get("source") or "").strip().lower()
        requested_symbols = get_requested_symbols(payload)
        targeted_rebuild = bool(requested_symbols) and source in {"history_repair", "recompute", "targeted_recompute"}
        skip_persisted_cursor = source in {"recompute", "history_repair", "targeted_recompute"}
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
        force_rollup = bool(payload.get("force_rollup")) or source in {"recompute", "history_repair", "targeted_recompute"}
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
        rollup_results = ensure_higher_timeframe_bars(
            enabled_environments,
            force=force_rollup,
            symbols=requested_symbols if targeted_rebuild else None,
        )
        errors += sum(int(result.get("errors", 0) or 0) for result in rollup_results.values())
        refresh_daily_close_cache(enabled_environments)

        try:
            for environment in enabled_environments:
                if targeted_rebuild:
                    reset_compute_state_for_symbols(environment, requested_symbols, intervals=INTERVALS)
                if not skip_persisted_cursor:
                    load_persisted_compute_cursors(environment)
                market_monitor_symbols = get_market_monitor_symbols(environment)
                for interval in INTERVALS:
                    interval_bars = fetch_interval_bars(
                        environment,
                        interval,
                        symbols=requested_symbols if requested_symbols else None,
                        full_scan=targeted_rebuild,
                    )
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

                            if interval != "5m" or not signal_generator or symbol in market_monitor_symbols:
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
            "symbols": requested_symbols,
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


@app.route("/chart/timeline", methods=["POST"])
def chart_timeline():
    payload = request.get_json(silent=True) or {}
    environment = str(payload.get("environment") or "live").strip().lower() or "live"
    symbol = str(payload.get("symbol") or "").strip().upper()
    interval = normalize_interval(payload.get("interval") or "5m")
    start_ms = int(payload.get("start_ms") or 0)
    end_ms = int(payload.get("end_ms") or 0)
    include_signals = bool(payload.get("include_signals", True))

    if environment not in SUPPORTED_COMPUTE_ENVIRONMENTS:
        return jsonify({"ok": False, "error": "invalid_environment", "environment": environment}), 400
    if not symbol:
        return jsonify({"ok": False, "error": "missing_symbol"}), 400
    if interval not in COMPUTE_INTERVALS:
        return jsonify({"ok": False, "error": "invalid_interval", "interval": interval}), 400
    if start_ms > 0 and end_ms > 0 and start_ms > end_ms:
        return jsonify({"ok": False, "error": "invalid_range", "start_ms": start_ms, "end_ms": end_ms}), 400

    result = build_chart_timeline_payload(
        environment,
        symbol,
        interval,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )
    return jsonify(result), 200


@app.route("/contracts/search", methods=["GET"])
def contracts_search():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    query = str(request.args.get("q") or "").strip()
    limit = min(24, max(1, coerce_int(request.args.get("limit"), 12)))
    if not query:
        return jsonify({"ok": False, "error": "missing_query"}), 400
    if not hasattr(service, "conid_resolver") or service.conid_resolver is None:
        return jsonify({"ok": False, "error": "IBKR contract resolver unavailable"}), 503

    _maybe_restore_ibkr_service(service)
    service_status = service.status() if hasattr(service, "status") else {}
    try:
        items = service.conid_resolver.search_contracts(query, limit=limit)
        return jsonify(
            {
                "ok": True,
                "query": query,
                "limit": limit,
                "count": len(items),
                "items": items,
                "environment": _ibkr_service_environment(service),
                "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
                "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
            }
        ), 200
    except Exception as exc:
        traceback.print_exc()
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "query": query,
                "limit": limit,
                "environment": _ibkr_service_environment(service),
                "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
            }
        ), 500


@app.route("/screener", methods=["GET"])
def screener():
    environment = str(request.args.get("environment") or "live").strip().lower() or "live"
    market_date = str(request.args.get("market_date") or current_market_date()).strip() or current_market_date()
    symbols = normalize_symbols(str(request.args.get("symbols") or "").split(","))
    limit = coerce_int(request.args.get("limit"), 0)

    if environment not in SUPPORTED_COMPUTE_ENVIRONMENTS:
        return jsonify({"ok": False, "error": "invalid_environment", "environment": environment}), 400
    try:
        params = {
            "environment": environment,
            "market_date": market_date,
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if limit > 0:
            params["limit"] = str(limit)
        response = requests.get(
            f"{PB_BASE_URL.rstrip('/')}/api/custom/ibkr/screener",
            params=params,
            timeout=30,
        )
        return Response(
            response.text,
            status=response.status_code,
            mimetype="application/json",
        )
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_market_date", "market_date": market_date}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc), "environment": environment, "market_date": market_date}), 500


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


def _resolve_data_quality_symbols(service, payload: dict) -> list[str]:
    requested_symbols = normalize_symbols(payload.get("symbols"))
    if requested_symbols:
        return requested_symbols

    scan_scope = str(payload.get("scan_scope") or "manual").strip().lower()
    batch_size = max(1, int(payload.get("batch_size") or 0) or 8)
    if scan_scope == "active_target":
        try:
            status_payload = service.status()
            return normalize_symbols(((status_payload.get("market_universe") or {}).get("active_target_symbols") or []))
        except Exception:
            return []
    if scan_scope == "watchlist":
        try:
            service.config.refresh()
            service._refresh_watchlist_pool(force=True)
            return normalize_symbols((service._watchlist_symbols or [])[:batch_size])
        except Exception:
            return []
    return []


@app.route("/ibkr/data-quality/scan", methods=["POST"])
def ibkr_data_quality_scan():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    symbols = _resolve_data_quality_symbols(service, payload)
    scan_scope = str(payload.get("scan_scope") or "manual").strip().lower() or "manual"
    persist = bool(payload.get("persist", True))
    repair = bool(payload.get("repair", False))
    result = service.scan_bar_integrity(
        symbols,
        scan_scope=scan_scope,
        persist=persist,
        repair=repair,
    )
    return jsonify({
        "ok": True,
        "symbols": symbols,
        "scan_scope": scan_scope,
        **result,
    })


@app.route("/ibkr/data-quality/repair", methods=["POST"])
def ibkr_data_quality_repair():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    payload = request.get_json(silent=True) or {}
    symbols = _resolve_data_quality_symbols(service, payload)
    scan_scope = str(payload.get("scan_scope") or "manual").strip().lower() or "manual"
    result = service.scan_bar_integrity(
        symbols,
        scan_scope=scan_scope,
        persist=bool(payload.get("persist", True)),
        repair=True,
    )
    return jsonify({
        "ok": True,
        "symbols": symbols,
        "scan_scope": scan_scope,
        **result,
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


def _extract_live_order_text(order: dict, *keys: str) -> str:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _coerce_live_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


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
    # Support both bulk /iserver/account/orders format and individual /iserver/account/order/status/{id} format
    status = str(order.get("status") or order.get("order_status") or order.get("orderStatus") or "").strip()
    parent_id = str(order.get("parentId") or order.get("parent_order_id") or "").strip()
    order_type = str(order.get("orderType") or order.get("order_type") or order.get("orderDesc") or "").strip().upper()
    total_quantity = float(
        _coerce_float(
            order.get("totalSize") if order.get("totalSize") is not None
            else order.get("total_size") if order.get("total_size") is not None
            else order.get("quantity"),
            0.0,
        ) or 0.0
    )
    filled_quantity = float(_coerce_float(order.get("filledQuantity") or order.get("cum_fill"), 0.0) or 0.0)
    remaining_quantity = _coerce_float(order.get("remainingQuantity") or order.get("remainingSize"))
    if remaining_quantity is None:
        remaining_quantity = max(total_quantity - filled_quantity, 0.0)

    closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
    normalized_status = status.upper()
    if not parent_id:
        role = "entry"
    elif "STP" in order_type or "STOP" in order_type:
        role = "stop_loss"
    elif "LMT" in order_type or "LIMIT" in order_type:
        role = "take_profit"
    else:
        role = "child"

    # price: bulk uses "price", individual status uses "limit_price" / "stop_price"
    price = float(_coerce_float(order.get("price") or order.get("limit_price"), 0.0) or 0.0)
    trigger_price = float(_coerce_float(order.get("auxPrice") or order.get("stop_price"), 0.0) or 0.0)

    return {
        "order_id": str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip(),
        "parent_id": parent_id,
        "symbol": str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or order.get("contract_description_1") or "").strip().upper(),
        "conid": int(_coerce_float(order.get("conid") or order.get("conidex"), 0) or 0),
        "side": str(order.get("side") or "").strip().upper(),
        "status": status,
        "role": role,
        "order_type": order_type,
        "order_description": _extract_live_order_text(order, "orderDesc", "order_description", "order_description_with_contract", "description"),
        "price": price,
        "trigger_price": trigger_price,
        "avg_price": float(_coerce_float(order.get("avgPrice") or order.get("average_price"), 0.0) or 0.0),
        "total_quantity": total_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": float(remaining_quantity or 0.0),
        "time_in_force": str(order.get("tif") or order.get("timeInForce") or "").strip().upper(),
        "account": str(order.get("acct") or order.get("acctId") or order.get("account") or "").strip(),
        "currency": str(order.get("currency") or "USD").strip().upper(),
        "asset_class": _extract_live_order_text(order, "secType", "sec_type", "assetClass").upper(),
        "listing_exchange": _extract_live_order_text(order, "listingExchange", "listing_exchange", "exchange"),
        "submitted_time": _extract_live_order_text(order, "submittedTime", "submitTime", "order_time", "createdTime", "createTime"),
        "last_execution_time": _extract_live_order_text(order, "lastExecutionTime", "lastFillTime", "lastExecutionTime_r"),
        "good_till_date": _extract_live_order_text(order, "goodTillDate"),
        "outside_rth": _coerce_live_bool(order.get("outsideRth") or order.get("outside_rth"), False),
        "can_cancel": bool(normalized_status and normalized_status not in closed_statuses and not _coerce_live_bool(order.get("cannot_cancel_order"), False)),
        "can_modify": bool(normalized_status and normalized_status not in closed_statuses and not _coerce_live_bool(order.get("order_not_editable"), False)),
        "raw": order,
    }


def _canonical_order_status(value) -> str:
    text = str(value or "").strip().upper()
    if text in {"PENDING", "PRESUBMITTED", "SUBMITTED", "PENDINGSUBMIT", "INPROGRESS"}:
        return "SUBMITTED"
    if text in {"FILLED", "EXECUTED"}:
        return "FILLED"
    if text in {"CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}:
        return "CANCELED"
    return text or "UNKNOWN"


def _display_order_status(value) -> str:
    canonical = _canonical_order_status(value)
    return {
        "SUBMITTED": "Submitted",
        "FILLED": "Filled",
        "CANCELED": "Canceled",
        "UNKNOWN": "Unknown",
    }.get(canonical, str(value or canonical or "Unknown").strip() or "Unknown")


def _direction_from_side(value) -> str:
    side = str(value or "").strip().upper()
    if side == "BUY":
        return "long"
    if side == "SELL":
        return "short"
    return ""


def _extract_market_date_text(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    normalized = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except Exception:
        return ""
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d")
    return parsed.astimezone(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def _order_history_time_value(record: dict) -> str:
    for key in ("order_time", "us_time", "updated", "created", "fill_time"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _normalize_broker_history_order(order: dict) -> dict:
    live_order = _normalize_live_order(order)
    order_id = str(live_order.get("order_id") or "").strip()
    parent_id = str(live_order.get("parent_id") or "").strip()
    coid = _extract_live_order_text(order, "cOID", "coid", "order_ref", "orderRef")
    unique_id = coid or order_id
    entry_unique_id = parent_id or coid or order_id
    if not unique_id:
        unique_id = order_id
    if not entry_unique_id:
        entry_unique_id = unique_id
    status = _display_order_status(live_order.get("status"))
    canonical_status = _canonical_order_status(status)
    closed_statuses = {"FILLED", "CANCELED"}
    submitted_time = str(live_order.get("submitted_time") or "").strip()
    fill_time = str(live_order.get("last_execution_time") or "").strip()
    updated_time = fill_time or submitted_time or datetime.utcnow().isoformat()
    direction = _direction_from_side(live_order.get("side"))

    return {
        "source": "ibkr_direct",
        "source_kind": "broker_order",
        "source_label": "IBKR Direct",
        "unique_id": unique_id,
        "order_id": order_id,
        "broker_order_id": order_id,
        "order_type": live_order.get("order_type") or "",
        "symbol": live_order.get("symbol") or "",
        "direction": direction,
        "position_side": direction,
        "trade_group_id": entry_unique_id or unique_id,
        "entry_order_unique_id": entry_unique_id,
        "parent_order_unique_id": parent_id,
        "role": live_order.get("role") or "",
        "relation_status": "closed" if canonical_status in closed_statuses else "active",
        "quantity": live_order.get("total_quantity") or 0,
        "limit_price": live_order.get("price") or 0,
        "status": status,
        "filled_qty": live_order.get("filled_quantity") or 0,
        "fill_price": live_order.get("avg_price") or 0,
        "order_time": submitted_time,
        "fill_time": fill_time,
        "us_time": submitted_time,
        "updated": updated_time,
        "diagnostic_state": "",
        "diagnostic_note": "",
        "raw": live_order.get("raw") or {},
    }


def _normalize_pb_history_order(record: dict) -> dict:
    return {
        "record_id": str(record.get("id") or "").strip(),
        "order_id": str(record.get("order_id") or "").strip(),
        "broker_order_id": str(record.get("broker_order_id") or record.get("order_id") or "").strip(),
        "symbol": str(record.get("symbol") or "").strip().upper(),
        "status": _display_order_status(record.get("status")),
        "quantity": float(_coerce_float(record.get("quantity"), 0.0) or 0.0),
        "filled_qty": float(_coerce_float(record.get("filled_qty"), 0.0) or 0.0),
        "time_value": _order_history_time_value(record),
        "raw": record,
    }


def _build_broker_order_reconciliation(service, environment: str, broker_orders: list[dict]) -> dict:
    pb_error = ""
    market_date = current_market_date()
    pb_today_rows = []
    matched_count = 0
    broker_only_ids = []
    pb_only_ids = []
    status_mismatches = []
    filled_qty_mismatches = []
    quantity_mismatches = []

    try:
        pb_rows = []
        if getattr(service, "pb", None):
            safe_environment = str(environment or "live").replace("\\", "\\\\").replace('"', '\\"')
            pb_rows = service.pb.get_records(
                "orders",
                filter=f'environment = "{safe_environment}"',
                sort="-updated",
                per_page=200,
                page=1,
            )
        for row in pb_rows or []:
            if not isinstance(row, dict):
                continue
            normalized = _normalize_pb_history_order(row)
            if _extract_market_date_text(normalized.get("time_value")) != market_date:
                continue
            pb_today_rows.append(normalized)
    except Exception as exc:
        pb_error = str(exc)
        pb_today_rows = []

    pb_by_order_id = {}
    for row in pb_today_rows:
        key = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        if key and key not in pb_by_order_id:
            pb_by_order_id[key] = row

    for order in broker_orders:
        order_id = str(order.get("broker_order_id") or order.get("order_id") or "").strip()
        if not order_id:
            order["diagnostic_state"] = "missing_broker_order_id"
            order["diagnostic_note"] = "IBKR 未返回 broker order id，无法和 PB 订单表对账"
            continue

        pb_match = pb_by_order_id.pop(order_id, None)
        if not pb_match:
            broker_only_ids.append(order_id)
            order["diagnostic_state"] = "missing_in_pb"
            order["diagnostic_note"] = "IBKR 有该订单，但 PB 今日订单表未找到对应 broker_order_id"
            continue

        matched_count += 1
        mismatch_fields = []
        if _canonical_order_status(order.get("status")) != _canonical_order_status(pb_match.get("status")):
            mismatch_fields.append("status")
            status_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_status": order.get("status") or "",
                    "pb_status": pb_match.get("status") or "",
                }
            )

        broker_quantity = float(_coerce_float(order.get("quantity"), 0.0) or 0.0)
        pb_quantity = float(_coerce_float(pb_match.get("quantity"), 0.0) or 0.0)
        if abs(broker_quantity - pb_quantity) > 1e-9:
            mismatch_fields.append("quantity")
            quantity_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_quantity": broker_quantity,
                    "pb_quantity": pb_quantity,
                }
            )

        broker_filled = float(_coerce_float(order.get("filled_qty"), 0.0) or 0.0)
        pb_filled = float(_coerce_float(pb_match.get("filled_qty"), 0.0) or 0.0)
        if abs(broker_filled - pb_filled) > 1e-9:
            mismatch_fields.append("filled_qty")
            filled_qty_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_filled_qty": broker_filled,
                    "pb_filled_qty": pb_filled,
                }
            )

        if mismatch_fields:
            order["diagnostic_state"] = "field_mismatch"
            order["diagnostic_note"] = f'PB 对账字段不一致: {", ".join(mismatch_fields)}'
        else:
            order["diagnostic_state"] = "matched"
            order["diagnostic_note"] = "IBKR 与 PB 今日订单记录一致"

    pb_only_ids = sorted(pb_by_order_id.keys())

    return {
        "market_date": market_date,
        "pb_error": pb_error,
        "pb_today_count": len(pb_today_rows),
        "broker_today_count": len(broker_orders),
        "matched_count": matched_count,
        "broker_only_count": len(broker_only_ids),
        "pb_only_count": len(pb_only_ids),
        "status_mismatch_count": len(status_mismatches),
        "filled_qty_mismatch_count": len(filled_qty_mismatches),
        "quantity_mismatch_count": len(quantity_mismatches),
        "broker_only_ids": broker_only_ids[:20],
        "pb_only_ids": pb_only_ids[:20],
        "status_mismatches": status_mismatches[:20],
        "filled_qty_mismatches": filled_qty_mismatches[:20],
        "quantity_mismatches": quantity_mismatches[:20],
    }


def _build_ibkr_order_history(service, requested_days: int = 1) -> dict:
    runtime_environment = _ibkr_service_environment(service)
    service_status = service.status() if hasattr(service, "status") else {}
    use_paper = _ibkr_service_uses_paper_account(service)
    account_id = ""
    if hasattr(service, "order_placer"):
        try:
            account_id = str(service.order_placer.get_active_account_id(use_paper=use_paper) or "").strip()
        except Exception:
            account_id = ""

    broker_payload = {}
    broker_error = ""
    try:
        broker_payload = service.order_tracker.get_broker_order_history(days=requested_days, force=True)
    except Exception as exc:
        broker_error = str(exc)
        broker_payload = {}

    if not broker_error:
        broker_error = str(broker_payload.get("error") or "").strip()

    raw_orders = broker_payload.get("orders") or []
    broker_orders = [
        _normalize_broker_history_order(item)
        for item in raw_orders
        if isinstance(item, dict)
    ]
    reconciliation = _build_broker_order_reconciliation(service, runtime_environment, broker_orders)

    canonical_statuses = [_canonical_order_status(item.get("status")) for item in broker_orders]
    fetched_at = datetime.utcnow().isoformat()
    return {
        "ok": not broker_error,
        "error": broker_error,
        "environment": runtime_environment,
        "account_id": account_id,
        "source": "ibkr_direct_order_history",
        "service_running": bool(getattr(service, "is_running", False)),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "requested_days": max(1, int(requested_days or 1)),
        "effective_days": int(broker_payload.get("effective_days") or 1),
        "current_day_only": bool(broker_payload.get("current_day_only", True)),
        "items": broker_orders,
        "counts": {
            "total": len(broker_orders),
            "open": len([item for item in canonical_statuses if item == "SUBMITTED"]),
            "filled": len([item for item in canonical_statuses if item == "FILLED"]),
            "canceled": len([item for item in canonical_statuses if item == "CANCELED"]),
        },
        "reconciliation": reconciliation,
        "limitations": broker_payload.get("limitations") or [
            "IBKR Client Portal /iserver/account/orders 仅返回当前美东交易日订单。",
            "如果需要跨日历史订单，请补充 Flex / Statement 链路。",
        ],
        "errors": {
            "broker": broker_error,
            "pb": reconciliation.get("pb_error") or "",
        },
        "fetched_at": fetched_at,
        "raw": broker_payload.get("raw") if isinstance(broker_payload.get("raw"), dict) else {},
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

    cache_key = (runtime_environment, account_id)
    now = time.time()
    with ibkr_account_snapshot_cache_lock:
        cached_entry = ibkr_account_snapshot_cache.get(cache_key)
        if cached_entry and float(cached_entry.get("expires_at", 0) or 0) > now:
            return dict(cached_entry.get("payload") or {})
        if cached_entry:
            ibkr_account_snapshot_cache.pop(cache_key, None)

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

    # When the Gateway bulk orders endpoint returns empty (common after session restart),
    # fall back to individually verifying PB-tracked active orders by broker_order_id.
    if not orders_raw:
        try:
            pb_active = pb.get_records(
                "orders",
                filter=f'environment="{_ibkr_service_environment(service)}" && broker_order_id!="" && (status="Submitted" || status="Init" || status="PreSubmitted")',
                sort="-updated",
                per_page=50,
            )
            fallback_ids = [str(r.get("broker_order_id") or "").strip() for r in (pb_active or []) if r.get("broker_order_id")]
            if fallback_ids:
                orders_raw = service.order_tracker.get_orders_by_ids(fallback_ids)
                if orders_raw:
                    logger.info("Live orders fallback: bulk returned empty, recovered %d orders via individual status fetch", len(orders_raw))
        except Exception as exc:
            logger.debug("Live orders fallback failed: %s", exc)

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

    payload = {
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
    with ibkr_account_snapshot_cache_lock:
        ibkr_account_snapshot_cache[cache_key] = {
            "expires_at": now + IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS,
            "payload": payload,
        }
    return payload


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


@app.route("/ibkr/2fa/takeover", methods=["POST"])
def ibkr_2fa_takeover():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", True))
    ttl_seconds = int(payload.get("ttl_sec") or 0) or 600
    reason = str(payload.get("reason") or "manual_takeover").strip() or "manual_takeover"
    source = str(payload.get("source") or "api_takeover").strip() or "api_takeover"
    state = service.set_manual_takeover(
        enabled=enabled,
        ttl_seconds=ttl_seconds,
        reason=reason,
        source=source,
    )
    return jsonify({
        "ok": True,
        "environment": _ibkr_service_environment(service),
        "enabled": bool(enabled),
        "state": state,
    })


@app.route("/ibkr/2fa/probe", methods=["POST"])
def ibkr_2fa_probe():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "manual_probe").strip() or "manual_probe"
    source = str(payload.get("source") or "api_probe").strip() or "api_probe"
    state = service.trigger_auth_probe(reason=reason, source=source)
    return jsonify({
        "ok": True,
        "environment": _ibkr_service_environment(service),
        "state": state,
    })


@app.route("/ibkr/panic-reset", methods=["POST"])
def ibkr_panic_reset():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503
    payload = request.get_json(silent=True) or {}
    restart_gateway = bool(payload.get("restart_gateway", True))
    restart_runtime = bool(payload.get("restart_runtime", True))
    trigger_login = bool(payload.get("trigger_login", True))
    reason = str(payload.get("reason") or "panic_reset_2fa").strip() or "panic_reset_2fa"
    source = str(payload.get("source") or "api_panic_reset").strip() or "api_panic_reset"
    result = service.panic_reset_auth(
        restart_gateway=restart_gateway,
        restart_runtime=restart_runtime,
        trigger_login=trigger_login,
        reason=reason,
        source=source,
    )
    return jsonify({
        "ok": True,
        "environment": _ibkr_service_environment(service),
        **result,
    })


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


@app.route("/ibkr/orders/history", methods=["GET"])
def ibkr_order_history():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"}), 503

    requested_days = max(1, int(_coerce_float(request.args.get("days"), 1) or 1))
    payload = _build_ibkr_order_history(service, requested_days=requested_days)
    if payload.get("ok"):
        return jsonify(payload)
    error_text = str(payload.get("error") or (payload.get("errors") or {}).get("broker") or "").strip().lower()
    status_code = 409 if "not authenticated" in error_text else 502
    return jsonify(payload), status_code


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

    duplicate_order = None
    if hasattr(service, "order_tracker"):
        try:
            duplicate_order = service.order_tracker.find_duplicate_open_entry(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=float(entry_price or 0.0),
                entry_order_type=order_type,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Failed to inspect live orders before placement: {exc}"}), 500

    if duplicate_order:
        try:
            service.order_tracker.sync_live_orders_snapshot([duplicate_order])
        except Exception:
            pass
        snapshot = _build_ibkr_account_snapshot(service)
        broker_order_id = str(duplicate_order.get("orderId") or duplicate_order.get("id") or "").strip()
        return jsonify(
            {
                "ok": False,
                "error": "Duplicate open broker order already exists",
                "action": "place_order",
                "environment": runtime_environment,
                "symbol": symbol,
                "direction": direction,
                "quantity": quantity,
                "order_type": order_type,
                "entry_price": float(entry_price or 0.0),
                "take_profit_price": float(take_profit_price),
                "stop_loss_price": float(stop_loss_price),
                "duplicate_order": {
                    "order_id": broker_order_id,
                    "status": str(duplicate_order.get("status") or "").strip(),
                    "symbol": str(duplicate_order.get("ticker") or duplicate_order.get("symbol") or "").strip().upper(),
                    "side": str(duplicate_order.get("side") or "").strip().upper(),
                    "price": _coerce_float(duplicate_order.get("price"), 0.0) or 0.0,
                    "quantity": _coerce_float(
                        duplicate_order.get("totalSize")
                        if duplicate_order.get("totalSize") is not None
                        else duplicate_order.get("quantity"),
                        0.0,
                    ) or 0.0,
                },
                "snapshot": snapshot,
            }
        ), 409

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
