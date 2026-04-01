"""
QC Compute — 指标计算 HTTP 服务
由 PB cron 触发, 常驻内存维护 IndicatorEngine 热缓存

端点:
  POST /compute     — 读最新qc_bars, 更新引擎, 写指标/信号
  POST /scan        — 盘前扫描, 写daily_targets
  POST /recompute   — 全量重算 (清空缓存, 从历史重建)
  GET  /health      — 健康检查
  GET  /status      — 引擎状态
"""

import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify

from qc_compute.integrations.pb_client import PBClient
from qc_compute.core.config import Config
from qc_compute.core.indicator_engine import IndicatorEngine
from qc_compute.core.signal_generator import SignalGenerator
from qc_compute.workflows.daily_scanner import DailyScanner

app = Flask(__name__)

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
pb = PBClient(base_url=PB_BASE_URL)
cfg = Config(pb_client=pb)

engines = {}           # (environment, symbol, interval) -> IndicatorEngine
signal_gens = {}       # (environment, symbol, interval) -> SignalGenerator
last_compute_time = 0
last_scan_time = 0
last_processed_ms = {} # (environment, symbol, interval) -> last bar_time_ms
compute_count = 0
error_count = 0

INTERVALS = ["5m", "15m", "30m", "1H", "4H", "1D", "1W"]
SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]


def get_or_create_engine(environment: str, symbol: str, interval: str) -> IndicatorEngine:
    key = (environment, symbol, interval)
    if key not in engines:
        engines[key] = IndicatorEngine(symbol, interval)
        signal_gens[key] = SignalGenerator(symbol, interval)
    return engines[key]


def get_requested_environments(defaults=None):
    payload = request.get_json(silent=True) or {}
    requested = payload.get("environments")
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


def is_environment_compute_enabled(environment: str) -> bool:
    runtime_environment = str(environment or "").strip().lower()
    if runtime_environment == "backtest" and not cfg.has_environment_override("qc_compute_enabled", runtime_environment):
        return False
    default_enabled = runtime_environment in ("live", "paper")
    return cfg.get_bool_for_environment("qc_compute_enabled", runtime_environment, default_enabled)


def get_us_time_now() -> str:
    et = datetime.now(timezone(timedelta(hours=-4)))
    return et.strftime("%Y-%m-%d %H:%M:%S")


@app.route("/compute", methods=["POST"])
def compute():
    global last_compute_time, compute_count, error_count

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

    start = time.time()
    processed = 0
    signals_found = 0
    errors = 0
    enabled_environment_set = set(enabled_environments)

    try:
        for interval in INTERVALS:
            # 读取最新未处理的bars
            since_ms = 0
            filter_parts = [f'interval = "{interval}"']

            new_bars = pb.get_records(
                "qc_bars",
                filter=" && ".join(filter_parts),
                sort="-bar_time_ms",
                per_page=200,
            )

            if not new_bars:
                continue

            # 按 environment + symbol 分组，避免 live/paper 共用同一个 engine
            by_symbol = {}
            for bar in new_bars:
                environment = str(bar.get("environment", "live") or "live").strip().lower()
                sym = bar.get("symbol", "").upper()
                if sym and environment in enabled_environment_set:
                    by_symbol.setdefault((environment, sym), []).append(bar)

            for (environment, symbol), bars in by_symbol.items():
                bars.sort(key=lambda b: b.get("bar_time_ms", 0))
                engine = get_or_create_engine(environment, symbol, interval)
                key = (environment, symbol, interval)
                last_ms = last_processed_ms.get(key, 0)

                for bar in bars:
                    bar_ms = bar.get("bar_time_ms", 0)
                    if bar_ms <= last_ms:
                        continue

                    bar_data = {
                        "open": float(bar.get("open", 0)),
                        "high": float(bar.get("high", 0)),
                        "low": float(bar.get("low", 0)),
                        "close": float(bar.get("close", 0)),
                        "volume": float(bar.get("volume", 0)),
                        "bar_time_ms": bar_ms,
                        "us_time": bar.get("us_time", ""),
                        "cn_time": bar.get("cn_time", ""),
                        "session_type": bar.get("session_type", "regular"),
                    }

                    snapshot = engine.update(bar_data)
                    last_processed_ms[key] = bar_ms
                    processed += 1

                    # 发布指标
                    if snapshot and engine.is_ready():
                        try:
                            pb.upsert_indicator({
                                "environment": environment,
                                "symbol": symbol,
                                "exchange": bar.get("exchange", ""),
                                "interval": interval,
                                "script_tag": "qc_compute",
                                "us_time": bar.get("us_time", ""),
                                "cn_time": bar.get("cn_time", ""),
                                "bar_time_ms": bar_ms,
                                "bar_index": engine.bar_count,
                                "extra": {
                                    **snapshot,
                                    "environment": environment,
                                    "session_type": bar.get("session_type", "regular"),
                                    "source": "qc",
                                },
                            })
                        except Exception as e:
                            errors += 1

                    # 检测信号
                    sig_gen = signal_gens.get(key)
                    if sig_gen and engine.is_ready():
                        signal = sig_gen.update(snapshot)
                        if signal:
                            signals_found += 1
                            try:
                                pb.upsert_signal({
                                    "environment": environment,
                                    "symbol": symbol,
                                    "signal_id": signal.get("signal_id", f"qc_{symbol}_{bar_ms}"),
                                    "direction": signal.get("direction", ""),
                                    "signal": signal.get("signal", ""),
                                    "entry": signal.get("entry", 0),
                                    "stop_loss": signal.get("stop_loss", 0),
                                    "take_profit": signal.get("take_profit", 0),
                                    "rr": signal.get("rr", ""),
                                    "shares": signal.get("shares", 0),
                                    "interval": interval,
                                    "us_time": bar.get("us_time", ""),
                                    "cn_time": bar.get("cn_time", ""),
                                    "date": bar.get("us_time", "")[:10] if bar.get("us_time") else "",
                                    "bar_time_ms": bar_ms,
                                    "reason": signal.get("reason", ""),
                                    "status": "pending",
                                    "session_type": bar.get("session_type", "regular"),
                                    "extra": {
                                        "environment": environment,
                                        "source": "qc",
                                        "session_type": bar.get("session_type", "regular"),
                                    },
                                })
                            except Exception as e:
                                errors += 1

    except Exception as e:
        errors += 1
        error_count += 1
        traceback.print_exc()

    elapsed = round(time.time() - start, 3)
    last_compute_time = time.time()
    compute_count += 1

    return jsonify({
        "ok": True,
        "requested_environments": requested_environments,
        "environments": sorted(enabled_environments),
        "processed": processed,
        "signals": signals_found,
        "errors": errors,
        "engines": len(engines),
        "elapsed_s": elapsed,
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

    et = datetime.now(timezone(timedelta(hours=-4)))
    date_str = et.strftime("%Y-%m-%d")

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
    """全量重算: 清空引擎, 从qc_bars历史重建"""
    global last_processed_ms

    for engine in engines.values():
        engine.reset()
    for sg in signal_gens.values():
        sg.daily_reset()
    last_processed_ms.clear()

    # 触发一次compute
    with app.test_request_context("/compute", method="POST", json={"source": "recompute"}):
        result = compute()

    return jsonify({
        "ok": True,
        "action": "recompute",
        "engines_reset": len(engines),
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
        "write_mode": cfg.write_mode,
        "compute_enabled": cfg.compute_enabled,
        "compute_enabled_by_environment": {
            environment: is_environment_compute_enabled(environment)
            for environment in SUPPORTED_COMPUTE_ENVIRONMENTS
        },
        "supported_environments": SUPPORTED_COMPUTE_ENVIRONMENTS,
        "default_environments": DEFAULT_COMPUTE_ENVIRONMENTS,
        "total_engines": len(engines),
        "ready_engines": sum(1 for e in engines.values() if e.is_ready()),
        "engines": engine_status,
        "compute_count": compute_count,
        "last_compute": datetime.fromtimestamp(last_compute_time).isoformat() if last_compute_time else None,
        "last_scan": datetime.fromtimestamp(last_scan_time).isoformat() if last_scan_time else None,
    })


_start_time = time.time()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"[QC Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
