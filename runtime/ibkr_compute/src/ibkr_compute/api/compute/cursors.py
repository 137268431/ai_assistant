from __future__ import annotations

import time
import traceback

from ibkr_compute.market.timeframe_utils import build_runtime_timestamps, normalize_interval

from .common import _api_app


def build_compute_cursor_key(symbol: str, interval: str) -> str:
    return f"{str(symbol or '').upper()}|{normalize_interval(interval)}"


def parse_compute_cursor_key(raw_key: str):
    text = str(raw_key or "").strip()
    if "|" not in text:
        return "", ""
    symbol, interval = text.split("|", 1)
    return str(symbol or "").upper(), normalize_interval(interval)


def apply_cursor_map(environment: str, cursor_map: dict) -> int:
    api_app = _api_app()
    applied = 0
    for raw_key, raw_value in (cursor_map or {}).items():
        symbol, interval = parse_compute_cursor_key(raw_key)
        bar_ms = int(raw_value or 0)
        if not symbol or not interval or bar_ms <= 0:
            continue
        key = (environment, symbol, interval)
        api_app.last_processed_ms[key] = max(int(api_app.last_processed_ms.get(key, 0) or 0), bar_ms)
        interval_key = (environment, interval)
        api_app.last_interval_fetch_ms[interval_key] = max(
            int(api_app.last_interval_fetch_ms.get(interval_key, 0) or 0),
            bar_ms,
        )
        applied += 1
    return applied


def collect_environment_cursor_map(environment: str) -> dict:
    api_app = _api_app()
    env_map = {}
    for (env, symbol, interval), bar_ms in api_app.last_processed_ms.items():
        if env != environment:
            continue
        bar_ms = int(bar_ms or 0)
        if bar_ms <= 0:
            continue
        env_map[build_compute_cursor_key(symbol, interval)] = bar_ms
    return env_map


def persist_compute_cursors(environment: str):
    api_app = _api_app()
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
        api_app.pb.upsert_state(
            api_app.COMPUTE_CURSOR_STATE_KEY,
            environment,
            payload,
            date=api_app.COMPUTE_CURSOR_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()


def seed_compute_cursors_from_indicators(environment: str) -> int:
    api_app = _api_app()
    rows = api_app.pb.get_all_records(
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
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if runtime_environment in api_app.persistent_cursor_envs_loaded:
        return

    applied = 0
    try:
        state = api_app.pb.get_state(
            api_app.COMPUTE_CURSOR_STATE_KEY,
            runtime_environment,
            date=api_app.COMPUTE_CURSOR_STATE_DATE,
        )
        payload = state.get("data") if isinstance(state, dict) else {}
        applied = apply_cursor_map(
            runtime_environment,
            payload.get("cursors") if isinstance(payload, dict) else {},
        )
    except Exception:
        traceback.print_exc()

    if applied == 0:
        applied = seed_compute_cursors_from_indicators(runtime_environment)

    api_app.persistent_cursor_envs_loaded.add(runtime_environment)
    return applied
