from __future__ import annotations

import os
import threading
import time
from typing import Any

from flask import jsonify

from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

from ibkr_compute.api.compute.pipeline_views import (
    COMPUTE_LOCK_TIMEOUT_SECONDS,
    _acquire_compute_lock,
)
from ibkr_compute.api.compute.request import (
    get_requested_environments,
    get_requested_symbols,
    is_environment_compute_enabled,
)


PRIME_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
READINESS_INTERVALS = ("5m", "15m", "30m", "1h")
READINESS_SOFT_INTERVALS = ("15m", "30m", "1h")
READINESS_MAX_MISSING_SYMBOLS = 20
READINESS_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.environ.get("IBKR_MULTI_TIMEFRAME_READINESS_CACHE_TTL_SEC", "15.0")),
)
_readiness_cache_lock = threading.Lock()
_readiness_cache: dict[tuple, tuple[float, dict]] = {}


def _api_app():
    from .. import app as api_app

    return api_app


def _normalize_interval_list(values, default_intervals=PRIME_INTERVALS, allowed_intervals=PRIME_INTERVALS) -> list[str]:
    source = values
    if source is None:
        source = list(default_intervals)
    if not isinstance(source, (list, tuple, set)):
        source = [source]

    allowed = {normalize_interval(interval) for interval in allowed_intervals}
    normalized = []
    for value in source:
        interval = normalize_interval(value)
        if interval in allowed and interval not in normalized:
            normalized.append(interval)
    return normalized


def _unsupported_interval_values(values, allowed_intervals=PRIME_INTERVALS) -> list[str]:
    if values is None:
        return []
    source = values if isinstance(values, (list, tuple, set)) else [values]
    allowed = {normalize_interval(interval) for interval in allowed_intervals}
    unsupported = []
    for value in source:
        raw_value = str(value or "").strip()
        if not raw_value:
            continue
        interval = normalize_interval(value)
        if interval not in allowed and raw_value not in unsupported:
            unsupported.append(raw_value)
    return unsupported


def _requested_interval_values(payload: dict) -> list:
    requested = payload.get("intervals")
    if requested is None:
        requested = payload.get("interval")
    if requested is None:
        requested = payload.get("prime_intervals")
    return requested


def _payload_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _collect_latest_rows_by_symbol(api_app, collection: str, environment: str, interval: str, symbols: list[str]) -> dict:
    if not symbols:
        return {}

    symbol_set = set(symbols)
    chart_interval = interval_to_chart_tf(interval) if collection == "ibkr_indicators" else interval
    filter_parts = [
        f'interval = "{chart_interval}"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True)
        if collection == "ibkr_bars"
        else f'environment = "{environment}"',
    ]
    rows_by_symbol = {}
    max_pages = max(2, min(12, (len(symbols) + 199) // 200 + 4))
    for page in range(1, max_pages + 1):
        rows = api_app.pb.get_records(
            collection,
            filter=" && ".join(filter_parts),
            sort="-bar_time_ms",
            per_page=200,
            page=page,
        )
        if not rows:
            break
        for row in rows:
            symbol = str((row or {}).get("symbol", "")).strip().upper()
            if not symbol or symbol not in symbol_set or symbol in rows_by_symbol:
                continue
            rows_by_symbol[symbol] = dict(row or {})
        if len(rows_by_symbol) >= len(symbol_set) or len(rows) < 200:
            break
    return rows_by_symbol


def _engine_status_by_symbol(api_app, environment: str, interval: str, symbols: list[str]) -> dict[str, dict]:
    result = {}
    for symbol in symbols:
        engine = api_app.engines.get((environment, symbol, interval))
        result[symbol] = {
            "ready": bool(engine and engine.is_ready()),
            "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
            "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
        }
    return result


def _readiness_status(*, interval: str, missing_indicators: list[str], missing_ready: list[str]) -> str:
    del missing_indicators
    if interval == "5m":
        return "ready" if not missing_ready else "blocked"
    return "ready" if not missing_ready else "degraded"


def build_multi_timeframe_readiness(
    api_app=None,
    *,
    environment: str = "live",
    symbols: list[str] | None = None,
    intervals: list[str] | None = None,
    use_cache: bool = True,
    include_storage: bool = True,
) -> dict:
    api_app = api_app or _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    target_intervals = _normalize_interval_list(
        intervals,
        default_intervals=READINESS_INTERVALS,
        allowed_intervals=READINESS_INTERVALS,
    )

    target_symbols = api_app.normalize_symbols(symbols or [])
    if not target_symbols:
        target_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for key in getattr(api_app, "engines", {}).keys()
                if isinstance(key, tuple)
                and len(key) >= 3
                for env, symbol, interval in [key[:3]]
                if env == runtime_environment and interval == "5m" and str(symbol or "").strip()
            }
        )
    if not target_symbols:
        return {
            "environment": runtime_environment,
            "status": "unknown",
            "reason": "no_target_symbols",
            "hard_gate_interval": "5m",
            "soft_gate_intervals": list(READINESS_SOFT_INTERVALS),
            "symbols_total": 0,
            "intervals": {},
            "checked_at_ms": int(time.time() * 1000),
        }

    cache_key = (runtime_environment, tuple(target_symbols), tuple(target_intervals), bool(include_storage))
    now = time.time()
    if use_cache and READINESS_CACHE_TTL_SECONDS > 0:
        with _readiness_cache_lock:
            cached = _readiness_cache.get(cache_key)
            if cached and now < cached[0]:
                return dict(cached[1])

    by_interval: dict[str, Any] = {}
    overall_status = "ready"
    for interval in target_intervals:
        engines = _engine_status_by_symbol(api_app, runtime_environment, interval, target_symbols)
        ready_symbols = sorted(symbol for symbol, item in engines.items() if bool(item.get("ready")))
        if include_storage:
            bar_rows = _collect_latest_rows_by_symbol(api_app, "ibkr_bars", runtime_environment, interval, target_symbols)
            indicator_rows = _collect_latest_rows_by_symbol(
                api_app,
                "ibkr_indicators",
                runtime_environment,
                interval,
                target_symbols,
            )
            bar_symbols = sorted(bar_rows.keys())
            indicator_symbols = sorted(indicator_rows.keys())
            latest_bar_ms = max((int((row or {}).get("bar_time_ms", 0) or 0) for row in bar_rows.values()), default=0)
            latest_indicator_ms = max(
                (int((row or {}).get("bar_time_ms", 0) or 0) for row in indicator_rows.values()),
                default=0,
            )
            readiness_source = "storage"
        else:
            bar_symbols = sorted(
                symbol
                for symbol, item in engines.items()
                if int((item or {}).get("last_bar_time_ms", 0) or 0) > 0
                or int((item or {}).get("bar_count", 0) or 0) > 0
            )
            indicator_symbols = list(ready_symbols)
            latest_bar_ms = max(
                (int((item or {}).get("last_bar_time_ms", 0) or 0) for item in engines.values()),
                default=0,
            )
            latest_indicator_ms = max(
                (
                    int((item or {}).get("last_bar_time_ms", 0) or 0)
                    for item in engines.values()
                    if bool((item or {}).get("ready"))
                ),
                default=0,
            )
            readiness_source = "engine"
        missing_bar_symbols = sorted(set(target_symbols) - set(bar_symbols))
        missing_ready_symbols = sorted(set(target_symbols) - set(ready_symbols))
        missing_indicator_symbols = sorted(set(target_symbols) - set(indicator_symbols))

        interval_status = _readiness_status(
            interval=interval,
            missing_indicators=missing_indicator_symbols,
            missing_ready=missing_ready_symbols,
        )
        if interval_status == "blocked":
            overall_status = "blocked"
        elif interval_status == "degraded" and overall_status == "ready":
            overall_status = "degraded"

        by_interval[interval] = {
            "status": interval_status,
            "hard_gate": interval == "5m",
            "soft_gate": interval in READINESS_SOFT_INTERVALS,
            "storage_checked": bool(include_storage),
            "readiness_source": readiness_source,
            "symbols_total": len(target_symbols),
            "bar_symbols": len(bar_symbols),
            "ready_symbols": len(ready_symbols),
            "indicator_symbols": len(indicator_symbols),
            "latest_bar_time_ms": latest_bar_ms,
            "latest_indicator_time_ms": latest_indicator_ms,
            "missing_bar_symbols": missing_bar_symbols[:READINESS_MAX_MISSING_SYMBOLS],
            "missing_ready_symbols": missing_ready_symbols[:READINESS_MAX_MISSING_SYMBOLS],
            "missing_indicator_symbols": missing_indicator_symbols[:READINESS_MAX_MISSING_SYMBOLS],
            "missing_bar_symbols_total": len(missing_bar_symbols),
            "missing_ready_symbols_total": len(missing_ready_symbols),
            "missing_indicator_symbols_total": len(missing_indicator_symbols),
        }

    payload = {
        "environment": runtime_environment,
        "status": overall_status,
        "hard_gate_interval": "5m",
        "soft_gate_intervals": list(READINESS_SOFT_INTERVALS),
        "symbols_total": len(target_symbols),
        "intervals": by_interval,
        "checked_at_ms": int(time.time() * 1000),
    }
    if use_cache and READINESS_CACHE_TTL_SECONDS > 0:
        with _readiness_cache_lock:
            _readiness_cache[cache_key] = (time.time() + READINESS_CACHE_TTL_SECONDS, dict(payload))
    return payload


def _summarize_prime_interval(materialize_result: dict) -> dict:
    ready_symbols = sorted(
        symbol
        for symbol, result in (materialize_result or {}).items()
        if bool((result or {}).get("is_ready"))
    )
    indicator_seeded_symbols = sorted(
        symbol
        for symbol, result in (materialize_result or {}).items()
        if bool((result or {}).get("indicator_seeded"))
    )
    return {
        "symbols": sorted((materialize_result or {}).keys()),
        "symbols_total": len(materialize_result or {}),
        "ready_symbols": ready_symbols,
        "ready_symbols_total": len(ready_symbols),
        "indicator_seeded_symbols": indicator_seeded_symbols,
        "indicator_seeded_symbols_total": len(indicator_seeded_symbols),
    }


def build_compute_prime_response(payload=None):
    api_app = _api_app()
    request_payload = payload if isinstance(payload, dict) else {}
    requested_environments = get_requested_environments(request_payload)
    enabled_environments = [
        environment for environment in requested_environments if is_environment_compute_enabled(environment)
    ]
    requested_symbols = get_requested_symbols(request_payload)
    requested_interval_values = _requested_interval_values(request_payload)
    intervals = _normalize_interval_list(requested_interval_values)
    unsupported_intervals = _unsupported_interval_values(requested_interval_values)
    persist_latest_indicator = _payload_bool(
        request_payload.get("persist_latest_indicator", request_payload.get("persist_indicators")),
        False,
    )

    if not requested_symbols:
        return jsonify(
            {
                "ok": False,
                "error": "symbols_required",
                "requested_environments": requested_environments,
                "environments": enabled_environments,
            }
        ), 400
    if unsupported_intervals or not intervals:
        return jsonify(
            {
                "ok": False,
                "error": "unsupported_prime_intervals",
                "allowed_intervals": list(PRIME_INTERVALS),
                "unsupported_intervals": unsupported_intervals,
                "requested_environments": requested_environments,
                "environments": enabled_environments,
                "symbols": requested_symbols,
            }
        ), 400
    if not enabled_environments:
        return jsonify(
            {
                "ok": True,
                "skipped": True,
                "reason": "compute_disabled",
                "requested_environments": requested_environments,
                "environments": [],
                "symbols": requested_symbols,
                "intervals": intervals,
            }
        )

    compute_lock = _acquire_compute_lock(api_app)
    if compute_lock is None:
        return jsonify(
            {
                "ok": False,
                "error": "compute_busy",
                "retryable": True,
                "lock_timeout_s": COMPUTE_LOCK_TIMEOUT_SECONDS,
                "requested_environments": requested_environments,
                "environments": enabled_environments,
                "symbols": requested_symbols,
                "intervals": intervals,
            }
        ), 503

    started = time.time()
    results = {}
    errors = 0
    with compute_lock:
        api_app.cfg.refresh()
        for environment in enabled_environments:
            env_result = {
                "intervals": {},
                "symbols": requested_symbols,
            }
            for interval in intervals:
                if interval == "5m":
                    rollup_result = {
                        environment: {
                            "skipped": True,
                            "reason": "base_interval_materialize_only",
                            "written": 0,
                            "errors": 0,
                            "intervals": ["5m"],
                        }
                    }
                else:
                    rollup_result = api_app.ensure_higher_timeframe_bars(
                        [environment],
                        force=True,
                        symbols=requested_symbols,
                        incremental=False,
                        intervals=[interval],
                    )
                materialize_result = api_app.materialize_engines_from_storage(
                    environment,
                    requested_symbols,
                    interval,
                    hydrate_signal_state=False,
                    persist_latest_indicator=persist_latest_indicator,
                )
                interval_errors = sum(
                    int((item or {}).get("errors", 0) or 0)
                    for item in (rollup_result or {}).values()
                )
                errors += interval_errors
                env_result["intervals"][interval] = {
                    "ok": interval_errors == 0,
                    "rollup": rollup_result.get(environment, {}) if isinstance(rollup_result, dict) else {},
                    "materialize": _summarize_prime_interval(materialize_result),
                }
            env_result["readiness"] = build_multi_timeframe_readiness(
                api_app,
                environment=environment,
                symbols=requested_symbols,
                intervals=["5m", *intervals],
                use_cache=False,
                include_storage=True,
            )
            results[environment] = env_result

    return jsonify(
        {
            "ok": errors == 0,
            "errors": errors,
            "requested_environments": requested_environments,
            "environments": enabled_environments,
            "symbols": requested_symbols,
            "intervals": intervals,
            "persist_latest_indicator": persist_latest_indicator,
            "results": results,
            "elapsed_s": round(time.time() - started, 3),
        }
    )
