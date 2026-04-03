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
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, redirect, request

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
)
from ibkr_compute.workflows.daily_scanner import DailyScanner

app = Flask(__name__)

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", PB_BASE_URL)
pb = PBClient(base_url=PB_BASE_URL)
cfg = Config(pb_client=pb)

engines = {}
signal_gens = {}
last_compute_time = 0.0
last_scan_time = 0.0
last_processed_ms = {}
last_interval_fetch_ms = {}
compute_count = 0
error_count = 0
rollup_bootstrap_checked = set()
symbol_metadata_cache = {}
daily_close_cache = {}
metadata_cache_updated_at = 0.0

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


def get_or_create_engine(environment: str, symbol: str, interval: str) -> IndicatorEngine:
    key = (environment, symbol, interval)
    if key not in engines:
        engines[key] = IndicatorEngine(symbol, interval)
        signal_gens[key] = SignalGenerator(symbol, interval)
    return engines[key]




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
    global daily_close_cache
    if daily_close_cache and not force and set(environments).issubset(set(daily_close_cache.keys())):
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
    return daily_close_cache


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

    cfg.refresh()
    payload = request.get_json(silent=True) or {}
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

    refresh_symbol_metadata()
    rollup_results = ensure_higher_timeframe_bars(enabled_environments, force=force_rollup)
    errors += sum(int(result.get("errors", 0) or 0) for result in rollup_results.values())
    refresh_daily_close_cache(enabled_environments)

    try:
        for environment in enabled_environments:
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
                        processed += 1

                        if not snapshot or not engine.is_ready():
                            continue

                        try:
                            pb.upsert_indicator(build_indicator_payload(environment, symbol, interval, bar, engine, snapshot))
                        except Exception:
                            errors += 1
                            traceback.print_exc()

                        if interval != "5m" or not signal_generator:
                            continue

                        signal = signal_generator.update(snapshot)
                        if signal and is_recent_signal_bar(bar_ms, interval):
                            try:
                                pb.upsert_signal(build_signal_payload(environment, symbol, interval, bar, engine, signal))
                                signals_found += 1
                            except Exception:
                                errors += 1
                                traceback.print_exc()
    except Exception:
        errors += 1
        error_count += 1
        traceback.print_exc()

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

    for engine in engines.values():
        engine.reset()
    for signal_generator in signal_gens.values():
        signal_generator.daily_reset()
    last_processed_ms.clear()
    last_interval_fetch_ms.clear()
    daily_close_cache = {}
    rollup_bootstrap_checked.clear()

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
        "compute_count": compute_count,
        "last_compute": datetime.fromtimestamp(last_compute_time).isoformat() if last_compute_time else None,
        "last_scan": datetime.fromtimestamp(last_scan_time).isoformat() if last_scan_time else None,
    })


_start_time = time.time()
_ibkr_service = None



def get_ibkr_service():
    global _ibkr_service
    if _ibkr_service is None:
        try:
            from ibkr_compute.ibkr_service import IBKRTradingService

            _ibkr_service = IBKRTradingService()
        except Exception as exc:
            print(f"[IBKR] Service init failed: {exc}")
    return _ibkr_service


@app.route("/ibkr/start", methods=["POST"])
def ibkr_start():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    try:
        import threading
        payload = request.get_json(silent=True) or {}
        trigger_login = payload.get("trigger_login", True)
        reason = str(payload.get("reason") or "manual_start")
        source = str(payload.get("source") or "api_start")

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

        thread = threading.Thread(
            target=service.start,
            kwargs={
                "trigger_login": bool(trigger_login),
                "reason": reason,
                "source": source,
            },
            daemon=True,
            name="ibkr-service",
        )
        thread.start()
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
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    service.stop()
    return jsonify({"ok": True, "message": "IBKR service stopped"})


@app.route("/ibkr/status", methods=["GET"])
def ibkr_status():
    service = get_ibkr_service()
    if not service:
        return jsonify({"ok": False, "error": "IBKR service not initialized"})
    return jsonify({"ok": True, **service.status()})


@app.route("/ibkr/dashboard", methods=["GET"])
def ibkr_dashboard():
    return redirect(f"{PB_PUBLIC_URL.rstrip('/')}/ibkr_runtime.html", code=302)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
