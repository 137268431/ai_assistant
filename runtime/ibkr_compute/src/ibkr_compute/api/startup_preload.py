from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from ibkr_compute.api.service_topology import get_runtime_mode, get_service_profile
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import normalize_interval


LOGGER = logging.getLogger("ibkr_compute.api")
_STARTUP_PRELOAD_LOCK = threading.Lock()
_STARTUP_PRELOAD_THREAD: threading.Thread | None = None
DIRECT_BACKFILL_DEFAULT_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
DIRECT_BACKFILL_DEFAULT_PERIODS = {
    "5m": "4d",
    "15m": "10d",
    "30m": "20d",
    "1h": "40d",
    "4h": "120d",
    "1d": "2y",
}
DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS = 260
DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
DIRECT_BACKFILL_DEFAULT_CONID_SCAN_PAGES = 3


def _api_app():
    from . import app as api_app

    return api_app


def _new_startup_preload_state() -> dict:
    return {
        "enabled": False,
        "should_schedule": False,
        "scheduled": False,
        "status": "idle",
        "running": False,
        "service_profile": "",
        "runtime_mode": "",
        "environments": [],
        "intervals": [],
        "env_total": 0,
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
        "direct_backfill": {
            "enabled": False,
            "status": "idle",
            "running": False,
            "intervals": [],
            "required_bars": DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS,
            "symbol_total": 0,
            "symbol_completed": 0,
            "planned_total": 0,
            "written": 0,
            "request_count": 0,
            "ready_count": 0,
            "started_at": 0.0,
            "finished_at": 0.0,
            "reason": "",
            "error": "",
            "results": {},
        },
    }


def _clone_state(payload: dict | None) -> dict:
    return copy.deepcopy(payload) if isinstance(payload, dict) else {}


def _ensure_startup_preload_state(api_app) -> tuple[threading.Lock, dict]:
    lock = getattr(api_app, "_compute_startup_preload_state_lock", None)
    if lock is None:
        lock = threading.Lock()
        setattr(api_app, "_compute_startup_preload_state_lock", lock)

    state = getattr(api_app, "_compute_startup_preload_state", None)
    if not isinstance(state, dict):
        state = _new_startup_preload_state()
        setattr(api_app, "_compute_startup_preload_state", state)
    return lock, state


def _publish_startup_preload_state(api_app, payload: dict) -> dict:
    lock, state = _ensure_startup_preload_state(api_app)
    snapshot = _clone_state(payload)
    with lock:
        state.clear()
        state.update(snapshot)
    return snapshot


def _read_startup_preload_state(api_app) -> dict:
    lock, state = _ensure_startup_preload_state(api_app)
    with lock:
        return _clone_state(state)


def _to_iso8601(timestamp_value) -> str | None:
    numeric = float(timestamp_value or 0.0)
    if numeric <= 0.0:
        return None
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()


def _env_flag(name: str, default: bool) -> bool:
    raw_value = str(os.environ.get(name, "") or "").strip().lower()
    if not raw_value:
        return bool(default)
    return raw_value not in {"0", "false", "no", "off"}


def _get_config_value(api_app, key: str, environment: str, fallback):
    raw_value = os.environ.get(str(key or "").strip().upper())
    if raw_value is not None and str(raw_value).strip() != "":
        return raw_value

    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_for_environment"):
        try:
            return cfg.get_for_environment(key, environment, fallback)
        except Exception:
            LOGGER.debug("Startup preload setting read failed: %s", key, exc_info=True)
    return fallback


def _get_config_bool(api_app, key: str, environment: str, fallback: bool) -> bool:
    raw_value = os.environ.get(str(key or "").strip().upper())
    if raw_value is not None and str(raw_value).strip() != "":
        return str(raw_value).strip().lower() not in {"0", "false", "no", "off"}

    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_bool_for_environment"):
        try:
            return bool(cfg.get_bool_for_environment(key, environment, fallback))
        except Exception:
            LOGGER.debug("Startup preload bool setting read failed: %s", key, exc_info=True)
    return bool(fallback)


def _get_config_int(api_app, key: str, environment: str, fallback: int) -> int:
    raw_value = os.environ.get(str(key or "").strip().upper())
    if raw_value is not None and str(raw_value).strip() != "":
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return int(fallback)

    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_int_for_environment"):
        try:
            return int(cfg.get_int_for_environment(key, environment, fallback))
        except Exception:
            LOGGER.debug("Startup preload int setting read failed: %s", key, exc_info=True)
    return int(fallback)


def is_compute_startup_preload_enabled() -> bool:
    # Warmup owns the readiness gate. Startup preload is opt-in so deploys and
    # off-hours restarts cannot be held by slow PocketBase storage hydration.
    return _env_flag("IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED", False)


def should_schedule_compute_startup_preload() -> bool:
    if not is_compute_startup_preload_enabled():
        return False
    if get_service_profile() != "compute":
        return False
    return get_runtime_mode() == "remote"


def resolve_compute_startup_preload_environments(api_app=None) -> list[str]:
    api_app = api_app or _api_app()
    supported_order = [
        str(environment or "").strip().lower()
        for environment in (api_app.SUPPORTED_COMPUTE_ENVIRONMENTS or [])
        if str(environment or "").strip()
    ]
    supported = set(supported_order)
    configured = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_ENVS", "") or "").strip()
    if configured:
        candidates = api_app.normalize_symbol_csv(configured)
        if any(str(environment or "").strip().lower() in {"*", "all"} for environment in candidates):
            candidates = supported_order
    elif "live" in supported:
        candidates = ["live"]
    else:
        candidates = [str(environment or "").strip().lower() for environment in (api_app.DEFAULT_COMPUTE_ENVIRONMENTS or [])]

    environments = []
    for environment in candidates:
        normalized = str(environment or "").strip().lower()
        if not normalized or normalized not in supported or normalized in environments:
            continue
        environments.append(normalized)
    return environments


def _parse_preload_interval_csv(raw_value: str) -> list[str]:
    return [
        str(item or "").strip().lower()
        for item in str(raw_value or "").replace(";", ",").split(",")
        if str(item or "").strip()
    ]


def resolve_compute_startup_preload_intervals(api_app=None) -> list[str]:
    api_app = api_app or _api_app()
    supported_source = getattr(api_app, "INTERVALS", None) or ["5m"]
    supported = [str(interval or "").strip().lower() for interval in supported_source if str(interval or "").strip()]
    configured = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS", "") or "").strip()
    # Startup warmup should clear the hard data-readiness gate quickly. Higher
    # timeframes can be requested explicitly or repaired by the background prime.
    candidates = _parse_preload_interval_csv(configured) if configured else ["5m"]
    if any(item in {"*", "all"} for item in candidates):
        candidates = supported

    intervals = []
    supported_set = set(supported)
    for interval in candidates:
        normalized = str(interval or "").strip().lower()
        if not normalized or normalized not in supported_set or normalized in intervals:
            continue
        intervals.append(normalized)
    return intervals


def _resolve_compute_startup_preload_chunk_size(api_app=None, environment: str = "live") -> int:
    api_app = api_app or _api_app()
    cfg = getattr(api_app, "cfg", None)
    value = 32
    if cfg is not None:
        try:
            value = int(
                cfg.get_int_for_environment(
                    "ibkr_compute_startup_preload_chunk_size",
                    environment,
                    32,
                )
            )
        except Exception:
            value = 32
    raw_env = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_CHUNK_SIZE", "") or "").strip()
    if raw_env:
        try:
            value = int(raw_env)
        except ValueError:
            value = 32
    return max(1, min(128, value))


def _resolve_compute_startup_preload_use_materialize(api_app=None, environment: str = "live") -> bool:
    api_app = api_app or _api_app()
    enabled = _get_config_bool(
        api_app,
        "ibkr_compute_startup_preload_use_materialize",
        environment,
        False,
    )
    raw_env = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_USE_MATERIALIZE", "") or "").strip().lower()
    if raw_env:
        enabled = raw_env in {"1", "true", "yes", "on"}
    return enabled


def _resolve_compute_startup_preload_refresh_enabled(api_app=None, environment: str = "live", *, kind: str) -> bool:
    api_app = api_app or _api_app()
    key = f"ibkr_compute_startup_preload_refresh_{kind}"
    enabled = _get_config_bool(api_app, key, environment, False)
    raw_env = str(os.environ.get(f"IBKR_COMPUTE_STARTUP_PRELOAD_REFRESH_{kind.upper()}", "") or "").strip().lower()
    if raw_env:
        enabled = raw_env in {"1", "true", "yes", "on"}
    return enabled


def resolve_startup_direct_backfill_enabled(api_app=None, environment: str = "live") -> bool:
    api_app = api_app or _api_app()
    # Direct IBKR backfill is useful for explicit repair, but it must not block
    # deploy/startup readiness by default; storage warmup and runtime topups keep
    # the full universe current without monopolizing the gateway.
    return _get_config_bool(api_app, "ibkr_startup_direct_backfill_enabled", environment, False)


def resolve_startup_direct_backfill_intervals(api_app=None, environment: str = "live") -> list[str]:
    api_app = api_app or _api_app()
    supported_source = getattr(api_app, "INTERVALS", None) or DIRECT_BACKFILL_DEFAULT_INTERVALS
    supported = [
        normalize_interval(interval)
        for interval in supported_source
        if str(interval or "").strip()
    ]
    configured = str(
        _get_config_value(
            api_app,
            "ibkr_startup_direct_backfill_intervals",
            environment,
            ",".join(DIRECT_BACKFILL_DEFAULT_INTERVALS),
        )
        or ""
    ).strip()
    candidates = _parse_preload_interval_csv(configured) if configured else list(DIRECT_BACKFILL_DEFAULT_INTERVALS)
    if any(item in {"*", "all"} for item in candidates):
        candidates = list(supported)

    intervals = []
    supported_set = set(supported)
    for interval in candidates:
        normalized = normalize_interval(interval)
        if normalized and normalized in supported_set and normalized not in intervals:
            intervals.append(normalized)
    return intervals


def resolve_startup_direct_backfill_required_bars(api_app=None, environment: str = "live") -> int:
    api_app = api_app or _api_app()
    return max(
        1,
        _get_config_int(
            api_app,
            "ibkr_startup_direct_backfill_required_bars",
            environment,
            DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS,
        ),
    )


def _resolve_startup_direct_backfill_period(api_app, environment: str, interval: str) -> str:
    normalized = normalize_interval(interval)
    fallback = DIRECT_BACKFILL_DEFAULT_PERIODS.get(normalized, "4d")
    return str(
        _get_config_value(
            api_app,
            f"ibkr_startup_direct_backfill_period_{normalized}",
            environment,
            fallback,
        )
        or fallback
    ).strip() or fallback


def _resolve_startup_direct_backfill_client_id(api_app, environment: str) -> int:
    return max(
        1,
        _get_config_int(
            api_app,
            "ibkr_startup_direct_backfill_client_id",
            environment,
            9131,
        ),
    )


def _startup_direct_backfill_resolve_missing_conids_enabled(api_app, environment: str) -> bool:
    return _get_config_bool(
        api_app,
        "ibkr_startup_direct_backfill_resolve_missing_conids_enabled",
        environment,
        False,
    )


def _startup_direct_backfill_live_conid_resolution_enabled(api_app, environment: str) -> bool:
    # Startup must not contend with the runtime/gateway client unless explicitly allowed.
    return _get_config_bool(
        api_app,
        "ibkr_startup_direct_backfill_allow_live_conid_resolution",
        environment,
        False,
    )


def _resolve_startup_direct_backfill_conid_scan_intervals(api_app, environment: str) -> list[str]:
    raw_value = str(
        _get_config_value(
            api_app,
            "ibkr_startup_direct_backfill_conid_scan_intervals",
            environment,
            ",".join(DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS),
        )
        or ""
    ).strip()
    candidates = _parse_preload_interval_csv(raw_value) if raw_value else list(DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS)
    intervals = []
    for interval in candidates:
        try:
            normalized = normalize_interval(interval)
        except Exception:
            continue
        if normalized not in intervals:
            intervals.append(normalized)
    return intervals or list(DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS)


def _resolve_startup_direct_backfill_conid_scan_pages(api_app, environment: str) -> int:
    return max(
        1,
        min(
            50,
            _get_config_int(
                api_app,
                "ibkr_startup_direct_backfill_conid_scan_pages",
                environment,
                DIRECT_BACKFILL_DEFAULT_CONID_SCAN_PAGES,
            ),
        ),
    )


def _new_direct_backfill_state(api_app=None, environment: str = "live") -> dict:
    enabled = resolve_startup_direct_backfill_enabled(api_app, environment) if api_app is not None else False
    intervals = resolve_startup_direct_backfill_intervals(api_app, environment) if api_app is not None else []
    return {
        "enabled": enabled,
        "status": "pending" if enabled else "disabled",
        "running": False,
        "intervals": intervals,
        "required_bars": resolve_startup_direct_backfill_required_bars(api_app, environment) if api_app is not None else DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS,
        "symbol_total": 0,
        "symbol_completed": 0,
        "planned_total": 0,
        "conid_status": "idle",
        "resolved_conids": 0,
        "missing_conid_total": 0,
        "written": 0,
        "request_count": 0,
        "ready_count": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "reason": "" if enabled else "disabled",
        "error": "",
        "results": {},
    }


def _collect_preload_interval_targets(api_app, environment: str, intervals: list[str] | tuple[str, ...] | set[str] | None = None) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    interval_filter = {str(interval or "").strip().lower() for interval in (intervals or []) if str(interval or "").strip()}
    cursor_map = api_app.collect_environment_cursor_map(environment) or {}
    for raw_key, raw_value in cursor_map.items():
        symbol, interval = api_app.parse_compute_cursor_key(raw_key)
        interval = str(interval or "").strip().lower()
        target_ms = int(raw_value or 0)
        if not symbol or not interval or target_ms <= 0:
            continue
        if interval_filter and interval not in interval_filter:
            continue
        grouped.setdefault(interval, {})[symbol] = target_ms

    ordered_groups = {}
    for interval in list(getattr(api_app, "INTERVALS", None) or []):
        targets = grouped.pop(interval, {})
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    for interval in sorted(grouped):
        targets = grouped[interval]
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    return ordered_groups


def _filter_preload_interval_targets(
    interval_targets: dict[str, dict[str, int]],
    symbols: list[str] | tuple[str, ...] | set[str],
) -> dict[str, dict[str, int]]:
    allowed = {
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    }
    if not allowed:
        return {}
    filtered: dict[str, dict[str, int]] = {}
    for interval, targets in (interval_targets or {}).items():
        kept = {
            str(symbol or "").strip().upper(): int(target_ms or 0)
            for symbol, target_ms in (targets or {}).items()
            if str(symbol or "").strip().upper() in allowed and int(target_ms or 0) > 0
        }
        if kept:
            filtered[interval] = {symbol: kept[symbol] for symbol in sorted(kept)}
    return filtered


def _resolve_preload_watchlist_symbols(api_app, environment: str) -> list[str]:
    try:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        rows = api_app.pb.get_all_records(
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
            symbol = str((row or {}).get("symbol", "")).strip().upper()
            if not symbol:
                continue
            row_environment = str((row or {}).get("environment", "") or "").strip().lower()
            rank = priority.get(row_environment, -1)
            if rank < 0:
                continue
            if symbol in applied and applied[symbol] > rank:
                continue
            applied[symbol] = rank
            merged[symbol] = True
        if merged:
            return sorted(merged.keys())
    except Exception:
        LOGGER.exception("Compute startup preload watchlist resolve failed: env=%s", environment)

    metadata = getattr(api_app, "symbol_metadata_cache", {}) or {}
    return sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in metadata.keys()
            if str(symbol or "").strip()
        }
    )


def _resolve_startup_direct_core_symbols(api_app, environment: str) -> list[str]:
    """Resolve the full data universe that must stay warm all day.

    Trading windows decide whether orders may be placed, but compute readiness
    should not degrade outside regular hours just because there are no active
    trade targets yet.
    """

    runtime_environment = str(environment or "live").strip().lower() or "live"
    symbols: set[str] = set()
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        try:
            market_date = api_app.current_market_date() if hasattr(api_app, "current_market_date") else ""
            if market_date:
                rows = pb.get_all_records(
                    "ibkr_targets",
                    filter=(
                        f'date = "{_pb_filter_escape(market_date)}" && '
                        f'environment = "{_pb_filter_escape(runtime_environment)}" && '
                        '(status = "active" || status = "candidate")'
                    ),
                    max_pages=10,
                )
                symbols.update(
                    str((row or {}).get("symbol") or "").strip().upper()
                    for row in rows or []
                    if str((row or {}).get("symbol") or "").strip()
                )
        except Exception:
            LOGGER.debug("Startup direct target symbols unavailable", exc_info=True)

        try:
            rows = pb.get_all_records(
                "watchlist",
                filter=(
                    f'(environment = "{_pb_filter_escape(runtime_environment)}" '
                    '|| environment = "global" '
                    '|| environment = "") && '
                    'symbol_role = "market_monitor"'
                ),
                sort="-updated",
                max_pages=10,
            )
            symbols.update(
                str((row or {}).get("symbol") or "").strip().upper()
                for row in rows or []
                if str((row or {}).get("symbol") or "").strip()
            )
        except Exception:
            LOGGER.debug("Startup direct monitor symbols unavailable", exc_info=True)

    symbols.update(_resolve_preload_watchlist_symbols(api_app, runtime_environment))

    if not symbols:
        try:
            raw = str(api_app.cfg.get_for_environment("ibkr_market_ws_symbols", runtime_environment, "SPY,QQQ,VIX") or "")
        except Exception:
            raw = "SPY,QQQ,VIX"
        symbols.update(str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip())
    return sorted(symbols)


def _startup_direct_backfill_full_watchlist_enabled(api_app, environment: str) -> bool:
    return _get_config_bool(
        api_app,
        "ibkr_startup_direct_backfill_full_watchlist_enabled",
        environment,
        False,
    )


def _resolve_startup_direct_backfill_symbols(api_app, environment: str) -> list[str]:
    """Resolve symbols allowed to use direct IBKR history requests at startup.

    The full data universe is still warmed from storage, but direct IBKR
    requests are intentionally narrower by default so deploy/restart warmup
    cannot overload PocketBase or the gateway with the entire watchlist.
    """

    runtime_environment = str(environment or "live").strip().lower() or "live"
    symbols: set[str] = set()
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        try:
            market_date = api_app.current_market_date() if hasattr(api_app, "current_market_date") else ""
            if market_date:
                rows = pb.get_all_records(
                    "ibkr_targets",
                    filter=(
                        f'date = "{_pb_filter_escape(market_date)}" && '
                        f'environment = "{_pb_filter_escape(runtime_environment)}" && '
                        '(status = "active" || status = "candidate")'
                    ),
                    max_pages=10,
                )
                symbols.update(
                    str((row or {}).get("symbol") or "").strip().upper()
                    for row in rows or []
                    if str((row or {}).get("symbol") or "").strip()
                )
        except Exception:
            LOGGER.debug("Startup direct target symbols unavailable", exc_info=True)

        try:
            rows = pb.get_all_records(
                "watchlist",
                filter=(
                    f'(environment = "{_pb_filter_escape(runtime_environment)}" '
                    '|| environment = "global" '
                    '|| environment = "") && '
                    'symbol_role = "market_monitor"'
                ),
                sort="-updated",
                max_pages=10,
            )
            symbols.update(
                str((row or {}).get("symbol") or "").strip().upper()
                for row in rows or []
                if str((row or {}).get("symbol") or "").strip()
            )
        except Exception:
            LOGGER.debug("Startup direct monitor symbols unavailable", exc_info=True)

    if _startup_direct_backfill_full_watchlist_enabled(api_app, runtime_environment):
        symbols.update(_resolve_preload_watchlist_symbols(api_app, runtime_environment))

    if not symbols:
        try:
            raw = str(api_app.cfg.get_for_environment("ibkr_market_ws_symbols", runtime_environment, "SPY,QQQ,VIX") or "")
        except Exception:
            raw = "SPY,QQQ,VIX"
        symbols.update(str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip())
    return sorted(symbols)


def _pb_filter_escape(value: str) -> str:
    return str(value or "").replace('"', '\\"')


def _count_stored_bars_for_startup_direct_backfill(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    required_bars: int,
    conid_map: dict[str, int] | None = None,
) -> int:
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "get_all_records"):
        return 0

    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    max_pages = max(1, min(20, (max(1, int(required_bars or 0)) + 199) // 200))
    try:
        rows = pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{_pb_filter_escape(normalized_symbol)}" && '
                f'interval = "{_pb_filter_escape(normalized_interval)}" && '
                f'environment = "{_pb_filter_escape(normalized_environment)}"'
            ),
            sort="-bar_time_ms",
            max_pages=max_pages,
        )
        if conid_map is not None and normalized_symbol not in conid_map:
            for row in rows or []:
                conid = _extract_conid_from_bar_row(row)
                if conid > 0:
                    conid_map[normalized_symbol] = conid
                    break
        return len(rows or [])
    except Exception as exc:
        LOGGER.warning(
            "Startup direct backfill count failed: env=%s interval=%s symbol=%s error=%s",
            normalized_environment,
            normalized_interval,
            normalized_symbol,
            exc,
        )
        return 0


def _startup_direct_backfill_freshness_plan(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    required_bars: int,
) -> dict:
    pb = getattr(api_app, "pb", None)
    if pb is None:
        return {"status": "missing", "needs_repair": True, "stored_count": 0, "reason": "pb_unavailable"}
    try:
        planner = BarFreshnessPlanner(pb, getattr(api_app, "cfg", None), environment=environment)
        payload = planner.plan_symbol(
            symbol,
            [interval],
            environment=environment,
            required_bars=required_bars,
        )
        interval_payload = (payload.get("intervals") or {}).get(normalize_interval(interval)) or {}
        return {
            **interval_payload,
            "conid": int(payload.get("conid", 0) or 0),
            "aggregate_status": str(payload.get("status") or ""),
        }
    except Exception as exc:
        LOGGER.warning(
            "Startup direct backfill freshness planning failed: env=%s interval=%s symbol=%s error=%s",
            environment,
            interval,
            symbol,
            exc,
        )
        stored_count = _count_stored_bars_for_startup_direct_backfill(
            api_app,
            environment,
            symbol,
            interval,
            required_bars,
        )
        return {
            "status": "ready" if stored_count >= required_bars else "missing",
            "needs_repair": stored_count < required_bars,
            "stored_count": stored_count,
            "reason": "fallback_count",
        }


def _extract_conid_from_bar_row(row: dict | None) -> int:
    payload = row or {}
    extra = payload.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}

    for value in (
        payload.get("conid"),
        payload.get("conidEx"),
        extra.get("conid"),
        extra.get("conidEx"),
    ):
        try:
            conid = int(float(value or 0))
        except (TypeError, ValueError):
            conid = 0
        if conid > 0:
            return conid
    return 0


def _merge_conids_from_bar_rows(
    result: dict[str, int],
    rows: list[dict] | None,
    wanted: set[str],
) -> None:
    for row in rows or []:
        symbol = str((row or {}).get("symbol") or "").strip().upper()
        if symbol not in wanted or symbol in result:
            continue
        conid = _extract_conid_from_bar_row(row)
        if conid > 0:
            result[symbol] = conid


def _resolve_startup_direct_backfill_conids(api_app, symbols: list[str], environment: str = "live") -> dict[str, int]:
    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols:
        return {}

    result: dict[str, int] = {}
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        wanted = set(normalized_symbols)
        safe_environment = _pb_filter_escape(str(environment or "live").strip().lower() or "live")
        scan_pages = _resolve_startup_direct_backfill_conid_scan_pages(api_app, environment)
        for interval in _resolve_startup_direct_backfill_conid_scan_intervals(api_app, environment):
            if len(result) >= len(wanted):
                return result
            try:
                safe_interval = _pb_filter_escape(normalize_interval(interval))
                rows = pb.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'environment = "{safe_environment}" && '
                        f'interval = "{safe_interval}"'
                    ),
                    sort="-bar_time_ms",
                    max_pages=scan_pages,
                )
                _merge_conids_from_bar_rows(result, rows, wanted)
            except Exception as exc:
                LOGGER.warning(
                    "Startup direct backfill bar conid read failed: env=%s interval=%s error=%s",
                    environment,
                    interval,
                    exc,
                )

        try:
            rows = pb.get_all_records("ibkr_conid_cache", max_pages=20)
            for row in rows or []:
                symbol = str((row or {}).get("symbol") or "").strip().upper()
                conid = int((row or {}).get("conid") or 0)
                if symbol in wanted and conid > 0:
                    result[symbol] = conid
            if len(result) >= len(wanted):
                return result
        except Exception as exc:
            text = str(exc or "")
            if "Missing collection context" in text or "status=404" in text:
                LOGGER.debug("Startup direct backfill PB conid cache unavailable: %s", exc)
            else:
                LOGGER.warning("Startup direct backfill PB conid cache read failed: %s", exc)

    missing_symbols = [symbol for symbol in normalized_symbols if symbol not in result]
    if not missing_symbols:
        return result
    if not (
        _startup_direct_backfill_resolve_missing_conids_enabled(api_app, environment)
        and _startup_direct_backfill_live_conid_resolution_enabled(api_app, environment)
    ):
        return result

    resolver = getattr(api_app, "conid_resolver", None)
    if resolver is not None and hasattr(resolver, "resolve_bulk"):
        try:
            result.update({
                str(symbol or "").strip().upper(): int(conid)
                for symbol, conid in (resolver.resolve_bulk(missing_symbols) or {}).items()
                if str(symbol or "").strip() and int(conid or 0) > 0
            })
            missing_symbols = [symbol for symbol in normalized_symbols if symbol not in result]
            if not missing_symbols:
                return result
        except Exception as exc:
            LOGGER.warning("Startup direct backfill existing conid resolver failed: %s", exc)

    try:
        from ibkr_compute.broker import BrokerAdapter
        from ibkr_compute.market.conid_resolver import ConidResolver

        resolver = ConidResolver(
            pb_client=getattr(api_app, "pb", None),
            broker=BrokerAdapter(
                client_id=_resolve_startup_direct_backfill_client_id(api_app, environment),
            ),
        )
        try:
            resolver.load_cache_from_pb()
        except Exception:
            LOGGER.debug("Startup direct backfill conid cache load failed", exc_info=True)
        result.update({
            str(symbol or "").strip().upper(): int(conid)
            for symbol, conid in (resolver.resolve_bulk(missing_symbols) or {}).items()
            if str(symbol or "").strip() and int(conid or 0) > 0
        })
        return result
    except Exception as exc:
        LOGGER.warning("Startup direct backfill conid resolve failed: %s", exc)
        return result


def _startup_direct_backfill_symbol_meta(api_app, symbols: list[str]) -> dict[str, dict]:
    metadata = getattr(api_app, "symbol_metadata_cache", {}) or {}
    result = {}
    for symbol in symbols or []:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        result[normalized_symbol] = dict(metadata.get(normalized_symbol) or {})
    return result


def _materialize_startup_direct_interval(api_app, environment: str, symbols: list[str], interval: str) -> tuple[int, int]:
    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols or not hasattr(api_app, "materialize_engines_from_storage"):
        return 0, 0

    results = api_app.materialize_engines_from_storage(
        environment,
        normalized_symbols,
        normalize_interval(interval),
        hydrate_signal_state=True,
        persist_latest_indicator=False,
    )
    ready_count = sum(1 for payload in (results or {}).values() if bool((payload or {}).get("is_ready")))
    seeded_count = sum(1 for payload in (results or {}).values() if bool((payload or {}).get("indicator_seeded")))
    return ready_count, seeded_count


def _run_startup_direct_backfill_environment(
    api_app,
    summary: dict,
    environment: str,
    symbols: list[str],
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    direct_state = summary.setdefault(
        "direct_backfill",
        _new_direct_backfill_state(api_app, normalized_environment),
    )
    enabled = resolve_startup_direct_backfill_enabled(api_app, normalized_environment)
    intervals = resolve_startup_direct_backfill_intervals(api_app, normalized_environment)
    required_bars = resolve_startup_direct_backfill_required_bars(api_app, normalized_environment)
    direct_state.update(
        {
            "enabled": enabled,
            "intervals": intervals,
            "required_bars": required_bars,
        }
    )

    if not enabled:
        direct_state["status"] = "disabled"
        direct_state["running"] = False
        direct_state["reason"] = "disabled"
        return direct_state
    if not intervals:
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = "no_direct_backfill_intervals"
        return direct_state

    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols:
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = "no_direct_backfill_symbols"
        return direct_state

    if float(direct_state.get("started_at") or 0.0) <= 0.0:
        direct_state["started_at"] = time.time()
    direct_state["status"] = "running"
    direct_state["running"] = True
    direct_state["reason"] = ""

    env_state = {
        "status": "running",
        "intervals": {},
        "symbol_count": len(normalized_symbols),
        "symbol_completed": 0,
        "planned_total": 0,
        "written": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "missing_conid_symbols": [],
        "error": "",
    }
    direct_state.setdefault("results", {})[normalized_environment] = env_state
    direct_state["symbol_total"] += len(normalized_symbols) * len(intervals)
    _publish_startup_preload_state(api_app, summary)

    interval_targets: dict[str, list[str]] = {}
    planning_conid_map: dict[str, int] = {}
    for interval in intervals:
        normalized_interval = normalize_interval(interval)
        interval_state = {
            "status": "planning",
            "symbol_count": len(normalized_symbols),
            "symbol_completed": 0,
            "backfill_symbols_total": 0,
            "freshness_status_counts": {},
            "stale_symbols": [],
            "missing_symbols": [],
            "written": 0,
            "ready_count": 0,
            "indicator_seeded": 0,
            "missing_conid_symbols": [],
            "request_period": _resolve_startup_direct_backfill_period(
                api_app,
                normalized_environment,
                normalized_interval,
            ),
            "error": "",
        }
        env_state["intervals"][normalized_interval] = interval_state
        targets = []
        for symbol in normalized_symbols:
            freshness = _startup_direct_backfill_freshness_plan(
                api_app,
                normalized_environment,
                symbol,
                normalized_interval,
                required_bars,
            )
            conid = int(freshness.get("conid", 0) or 0)
            if conid > 0 and symbol not in planning_conid_map:
                planning_conid_map[symbol] = conid
            freshness_status = str(freshness.get("status") or "unknown")
            interval_state["freshness_status_counts"][freshness_status] = int(
                interval_state["freshness_status_counts"].get(freshness_status, 0) or 0
            ) + 1
            if freshness_status == "stale":
                interval_state["stale_symbols"].append(symbol)
            elif freshness_status in {"missing", "gap", "failed", "unknown"}:
                interval_state["missing_symbols"].append(symbol)
            if bool(freshness.get("needs_repair")):
                targets.append(symbol)
        interval_targets[normalized_interval] = targets
        interval_state["backfill_symbols_total"] = len(targets)
        env_state["planned_total"] += len(targets)
        direct_state["planned_total"] += len(targets)
        _publish_startup_preload_state(api_app, summary)

    all_backfill_symbols = sorted({symbol for targets in interval_targets.values() for symbol in targets})
    conid_map = {
        symbol: int(planning_conid_map.get(symbol) or 0)
        for symbol in all_backfill_symbols
        if int(planning_conid_map.get(symbol) or 0) > 0
    }
    unresolved_backfill_symbols = [
        symbol
        for symbol in all_backfill_symbols
        if int(conid_map.get(symbol) or 0) <= 0
    ]
    direct_state["conid_status"] = "resolving" if unresolved_backfill_symbols else ("completed" if all_backfill_symbols else "skipped")
    direct_state["resolved_conids"] = len(conid_map)
    for interval, targets in interval_targets.items():
        if targets:
            env_state["intervals"][interval]["status"] = "resolving_conids"
    _publish_startup_preload_state(api_app, summary)
    if unresolved_backfill_symbols:
        conid_map.update(
            _resolve_startup_direct_backfill_conids(
                api_app,
                unresolved_backfill_symbols,
                normalized_environment,
            )
        )
    missing_all_conids = [symbol for symbol in all_backfill_symbols if int(conid_map.get(symbol) or 0) <= 0]
    direct_state["conid_status"] = "completed"
    direct_state["resolved_conids"] = len(conid_map)
    direct_state["missing_conid_total"] = len(missing_all_conids)
    env_state["missing_conid_symbols"] = missing_all_conids
    _publish_startup_preload_state(api_app, summary)
    symbol_meta = _startup_direct_backfill_symbol_meta(api_app, normalized_symbols)
    writer = None
    backfill = None
    broker = None
    try:
        if all_backfill_symbols:
            from ibkr_compute.broker import BrokerAdapter
            from ibkr_compute.market.data_backfill import DataBackfill
            from ibkr_compute.market.data_writer import DataWriter

            writer = DataWriter(
                pb_client=getattr(api_app, "pb", None),
                config=getattr(api_app, "cfg", None),
                environment=normalized_environment,
            )
            broker = BrokerAdapter(
                client_id=_resolve_startup_direct_backfill_client_id(
                    api_app,
                    normalized_environment,
                ),
            )
            backfill = DataBackfill(
                data_writer=writer,
                config=getattr(api_app, "cfg", None),
                environment=normalized_environment,
                broker=broker,
            )

        ordered_intervals = [
            normalize_interval(interval)
            for interval in intervals
        ]
        ordered_intervals = sorted(
            ordered_intervals,
            key=lambda interval: 0 if interval_targets.get(interval) else 1,
        )

        for normalized_interval in ordered_intervals:
            interval_state = env_state["intervals"][normalized_interval]
            interval_state["status"] = "running"
            _publish_startup_preload_state(api_app, summary)
            target_symbols = list(interval_targets.get(normalized_interval) or [])
            missing_conid_symbols = [symbol for symbol in target_symbols if int(conid_map.get(symbol) or 0) <= 0]
            interval_state["missing_conid_symbols"] = missing_conid_symbols
            env_state["missing_conid_symbols"] = sorted(
                set(env_state.get("missing_conid_symbols") or []).union(missing_conid_symbols)
            )

            runnable_conids = {
                symbol: int(conid_map.get(symbol) or 0)
                for symbol in target_symbols
                if int(conid_map.get(symbol) or 0) > 0
            }
            written = 0
            if runnable_conids and backfill is not None:
                request_period = str(interval_state.get("request_period") or "").strip()
                period_overrides = {
                    symbol: {normalized_interval: request_period}
                    for symbol in runnable_conids
                }
                results = backfill.backfill_all(
                    runnable_conids,
                    symbol_meta=symbol_meta,
                    intervals=[normalized_interval],
                    repair_symbols=list(runnable_conids.keys()),
                    period_overrides=period_overrides,
                )
                if writer is not None:
                    writer.flush()
                written = sum(
                    int((payload or {}).get(normalized_interval, 0) or 0)
                    for payload in (results or {}).values()
                )
                interval_state["written"] = written
                env_state["written"] += written
                direct_state["written"] += written
                _publish_startup_preload_state(api_app, summary)

            if not target_symbols:
                interval_state["reason"] = "no_backfill_needed"
                interval_state["symbol_completed"] = len(normalized_symbols)
                interval_state["status"] = "completed"
                env_state["symbol_completed"] += len(normalized_symbols)
                direct_state["symbol_completed"] += len(normalized_symbols)
                _publish_startup_preload_state(api_app, summary)
                continue
            if written <= 0:
                interval_state["reason"] = "no_bars_written"
                interval_state["symbol_completed"] = len(normalized_symbols)
                interval_state["status"] = "completed"
                env_state["symbol_completed"] += len(normalized_symbols)
                direct_state["symbol_completed"] += len(normalized_symbols)
                _publish_startup_preload_state(api_app, summary)
                continue

            materialize_symbols = target_symbols
            interval_state["status"] = "materializing"
            already_ready_count = max(0, len(normalized_symbols) - len(materialize_symbols))
            interval_state["symbol_completed"] = already_ready_count
            env_state["symbol_completed"] += already_ready_count
            direct_state["symbol_completed"] += already_ready_count
            _publish_startup_preload_state(api_app, summary)
            ready_count = 0
            indicator_seeded = 0
            for symbol in materialize_symbols:
                symbol_ready, symbol_seeded = _materialize_startup_direct_interval(
                    api_app,
                    normalized_environment,
                    [symbol],
                    normalized_interval,
                )
                ready_count += symbol_ready
                indicator_seeded += symbol_seeded
                interval_state["ready_count"] = ready_count
                interval_state["indicator_seeded"] = indicator_seeded
                interval_state["symbol_completed"] += 1
                env_state["symbol_completed"] += 1
                env_state["ready_count"] += symbol_ready
                env_state["indicator_seeded"] += symbol_seeded
                direct_state["symbol_completed"] += 1
                direct_state["ready_count"] += symbol_ready
                _publish_startup_preload_state(api_app, summary)
            interval_state["status"] = "completed"
            _publish_startup_preload_state(api_app, summary)
    except Exception as exc:
        LOGGER.exception("Startup direct backfill failed: env=%s", normalized_environment)
        env_state["status"] = "failed"
        env_state["error"] = str(exc)
        direct_state["status"] = "failed"
        direct_state["error"] = str(exc)
        _publish_startup_preload_state(api_app, summary)
    finally:
        if backfill is not None:
            status = backfill.status()
            direct_state["request_count"] += int(status.get("request_count", 0) or 0)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                LOGGER.debug("Startup direct backfill writer close failed", exc_info=True)
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                LOGGER.debug("Startup direct backfill broker disconnect failed", exc_info=True)

    if env_state.get("status") != "failed":
        env_state["status"] = "completed"
    if direct_state.get("status") != "failed":
        direct_state["status"] = "completed"
        direct_state["error"] = ""
    direct_state["running"] = False
    direct_state["finished_at"] = time.time()
    _publish_startup_preload_state(api_app, summary)
    return direct_state


def _preload_symbol_indicator_state(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    target_ms: int,
    *,
    use_materialize: bool = True,
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = str(interval or "").strip().lower()

    if use_materialize and hasattr(api_app, "materialize_engines_from_storage"):
        try:
            results = api_app.materialize_engines_from_storage(
                normalized_environment,
                [normalized_symbol],
                normalized_interval,
                hydrate_signal_state=True,
                persist_latest_indicator=False,
            )
            result = dict((results or {}).get(normalized_symbol) or {})
            if result:
                result.setdefault("indicator_seeded", False)
                return result
        except Exception:
            LOGGER.exception(
                "Compute startup preload materialize failed: env=%s interval=%s symbol=%s",
                normalized_environment,
                normalized_interval,
                normalized_symbol,
            )

    processed = api_app.bootstrap_engine_state(
        normalized_environment,
        normalized_symbol,
        normalized_interval,
        int(target_ms or 0),
        inclusive=True,
    )
    engine = api_app.engines.get((normalized_environment, normalized_symbol, normalized_interval))
    return {
        "processed": processed,
        "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
        "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
        "is_ready": bool(engine and engine.is_ready()),
        "indicator_seeded": False,
        "reason": "bootstrapped" if processed > 0 else "already_materialized",
    }


def _preload_interval_indicator_state(
    api_app,
    environment: str,
    symbols: list[str],
    interval: str,
    target_ms_by_symbol: dict[str, int] | None = None,
) -> dict[str, dict]:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = str(interval or "").strip().lower()
    normalized_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    ]
    if not normalized_symbols:
        return {}

    targets = {
        str(symbol or "").strip().upper(): int(target_ms or 0)
        for symbol, target_ms in (target_ms_by_symbol or {}).items()
        if str(symbol or "").strip()
    }

    results: dict[str, dict] = {}
    if hasattr(api_app, "materialize_engines_from_storage"):
        try:
            materialized = api_app.materialize_engines_from_storage(
                normalized_environment,
                normalized_symbols,
                normalized_interval,
                hydrate_signal_state=True,
                persist_latest_indicator=False,
            )
            for symbol in normalized_symbols:
                payload = dict((materialized or {}).get(symbol) or {})
                if payload:
                    payload.setdefault("indicator_seeded", False)
                    results[symbol] = payload
            missing = [symbol for symbol in normalized_symbols if symbol not in results]
            if not missing:
                return results
            LOGGER.warning(
                "Compute startup preload materialize returned partial result: env=%s interval=%s missing=%s",
                normalized_environment,
                normalized_interval,
                ",".join(missing),
            )
        except Exception:
            LOGGER.exception(
                "Compute startup preload batch materialize failed: env=%s interval=%s symbols=%d",
                normalized_environment,
                normalized_interval,
                len(normalized_symbols),
            )

    for symbol in normalized_symbols:
        if symbol in results:
            continue
        results[symbol] = _preload_symbol_indicator_state(
            api_app,
            normalized_environment,
            symbol,
            normalized_interval,
            int(targets.get(symbol, 0) or 0),
            use_materialize=False,
        )
    return results


def get_compute_startup_preload_state(api_app=None) -> dict:
    api_app = api_app or _api_app()
    snapshot = _read_startup_preload_state(api_app)
    if not snapshot:
        snapshot = _new_startup_preload_state()

    snapshot["enabled"] = is_compute_startup_preload_enabled()
    snapshot["should_schedule"] = should_schedule_compute_startup_preload()
    snapshot["service_profile"] = get_service_profile()
    snapshot["runtime_mode"] = get_runtime_mode()
    if not snapshot.get("environments"):
        snapshot["environments"] = resolve_compute_startup_preload_environments(api_app)
    if not snapshot.get("intervals"):
        snapshot["intervals"] = resolve_compute_startup_preload_intervals(api_app)
    if not isinstance(snapshot.get("direct_backfill"), dict):
        first_environment = (snapshot.get("environments") or ["live"])[0]
        snapshot["direct_backfill"] = _new_direct_backfill_state(api_app, first_environment)
    snapshot["env_total"] = max(
        int(snapshot.get("env_total") or 0),
        len(snapshot.get("environments") or []),
    )

    started_at = float(snapshot.get("started_at") or 0.0)
    finished_at = float(snapshot.get("finished_at") or 0.0)
    elapsed_s = 0.0
    if started_at > 0.0:
        end_time = finished_at if finished_at > 0.0 else time.time()
        elapsed_s = round(max(0.0, end_time - started_at), 3)

    status = str(snapshot.get("status") or "").strip().lower()
    if (not snapshot["enabled"] or not snapshot["should_schedule"]) and status in {"", "idle"}:
        snapshot["status"] = "disabled"
    elif not status:
        if snapshot.get("running"):
            snapshot["status"] = "running"
        elif snapshot.get("error"):
            snapshot["status"] = "failed"
        elif finished_at > 0.0:
            snapshot["status"] = "completed"
        elif snapshot.get("scheduled"):
            snapshot["status"] = "scheduled"
        else:
            snapshot["status"] = "idle"
    else:
        snapshot["status"] = status

    if (
        (not snapshot.get("reason"))
        and snapshot["status"] == "disabled"
        and (not snapshot["enabled"] or not snapshot["should_schedule"])
    ):
        snapshot["reason"] = "schedule_guard_blocked"

    snapshot["started_at"] = _to_iso8601(started_at)
    snapshot["finished_at"] = _to_iso8601(finished_at)
    snapshot["elapsed_s"] = elapsed_s
    return snapshot


def run_compute_startup_preload(api_app=None) -> dict:
    api_app = api_app or _api_app()
    environments = resolve_compute_startup_preload_environments(api_app)
    preload_intervals = resolve_compute_startup_preload_intervals(api_app)
    direct_environment = environments[0] if environments else "live"
    previous_state = _read_startup_preload_state(api_app)
    summary = {
        "ok": True,
        "enabled": is_compute_startup_preload_enabled(),
        "should_schedule": should_schedule_compute_startup_preload(),
        "scheduled": bool(previous_state.get("scheduled")),
        "status": "running",
        "running": True,
        "service_profile": get_service_profile(),
        "runtime_mode": get_runtime_mode(),
        "environments": environments,
        "intervals": preload_intervals,
        "env_total": len(environments),
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "started_at": time.time(),
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
        "direct_backfill": _new_direct_backfill_state(api_app, direct_environment),
    }
    _publish_startup_preload_state(api_app, summary)

    if not environments or not preload_intervals:
        summary["status"] = "skipped"
        summary["running"] = False
        summary["finished_at"] = time.time()
        summary["reason"] = "no_preload_environments" if not environments else "no_preload_intervals"
        direct_state = summary.setdefault("direct_backfill", {})
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = summary["reason"]
        direct_state["finished_at"] = summary["finished_at"]
        _publish_startup_preload_state(api_app, summary)
        return summary

    LOGGER.info(
        "Compute startup preload starting: envs=%s intervals=%s service_profile=%s runtime_mode=%s",
        ",".join(environments),
        ",".join(preload_intervals),
        summary["service_profile"],
        summary["runtime_mode"],
    )

    try:
        api_app.cfg.refresh()
    except Exception as exc:
        LOGGER.warning("Compute startup preload config refresh failed: %s", exc)

    if _resolve_compute_startup_preload_refresh_enabled(api_app, environments[0] if environments else "live", kind="metadata"):
        try:
            api_app.refresh_symbol_metadata(force=True)
        except Exception as exc:
            LOGGER.warning("Compute startup preload symbol metadata refresh failed: %s", exc)

    if _resolve_compute_startup_preload_refresh_enabled(api_app, environments[0] if environments else "live", kind="daily_close"):
        try:
            api_app.refresh_daily_close_cache(environments, force=True)
        except Exception as exc:
            LOGGER.warning("Compute startup preload daily close refresh failed: %s", exc)

    try:
        for environment in environments:
            cursor_applied = int(api_app.load_persisted_compute_cursors(environment) or 0)
            all_interval_targets = _collect_preload_interval_targets(api_app, environment, preload_intervals)
            data_symbols = _resolve_startup_direct_core_symbols(api_app, environment)
            direct_symbols = _resolve_startup_direct_backfill_symbols(api_app, environment)
            interval_targets = _filter_preload_interval_targets(all_interval_targets, data_symbols)
            cursor_count = len(api_app.collect_environment_cursor_map(environment) or {})
            full_cursor_symbol_total = sum(len(targets or {}) for targets in all_interval_targets.values())
            env_result = {
                "status": "running",
                "preload_scope": "data_universe",
                "core_symbols": list(direct_symbols),
                "core_symbol_count": len(direct_symbols),
                "data_symbols": list(data_symbols),
                "data_symbol_count": len(data_symbols),
                "cursor_applied": cursor_applied,
                "cursor_count": cursor_count,
                "full_cursor_symbol_total": full_cursor_symbol_total,
                "full_cursor_interval_total": len(all_interval_targets),
                "interval_total": len(interval_targets),
                "interval_completed": 0,
                "symbol_total": sum(len(targets or {}) for targets in interval_targets.values()),
                "symbol_completed": 0,
                "ready_count": 0,
                "indicator_seeded": 0,
                "intervals": {
                    interval: {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    }
                    for interval, targets in interval_targets.items()
                },
            }
            summary["symbol_total"] += env_result["symbol_total"]
            summary["results"][environment] = env_result
            LOGGER.info(
                "Compute startup preload environment: env=%s cursor_applied=%d cursor_count=%d intervals=%d",
                environment,
                cursor_applied,
                cursor_count,
                len(interval_targets),
            )

            _run_startup_direct_backfill_environment(
                api_app,
                summary,
                environment,
                direct_symbols,
            )

            fallback_symbols = [
                symbol
                for symbol in data_symbols
                if symbol not in set((interval_targets.get("5m") or {}).keys())
            ] if "5m" in preload_intervals else []
            if fallback_symbols:
                # Fresh environments can have bars but no cursor state yet; warm 5m engines from storage first.
                interval_state = env_result["intervals"].setdefault(
                    "5m",
                    {
                        "status": "pending",
                        "symbol_count": 0,
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["symbol_count"] = int(interval_state.get("symbol_count", 0) or 0) + len(fallback_symbols)
                env_result["symbol_total"] += len(fallback_symbols)
                summary["symbol_total"] += len(fallback_symbols)
                env_result["storage_fallback_symbols"] = len(fallback_symbols)

            _publish_startup_preload_state(api_app, summary)

            for interval, targets in interval_targets.items():
                interval_state = env_result["intervals"].setdefault(
                    interval,
                    {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["status"] = "running" if targets else "completed"
                _publish_startup_preload_state(api_app, summary)
                use_materialize = _resolve_compute_startup_preload_use_materialize(api_app, environment)
                if use_materialize:
                    target_items = list((targets or {}).items())
                    chunk_size = _resolve_compute_startup_preload_chunk_size(api_app, environment)
                    for index in range(0, len(target_items), chunk_size):
                        chunk_targets = {
                            symbol: target_ms
                            for symbol, target_ms in target_items[index:index + chunk_size]
                        }
                        chunk_results = _preload_interval_indicator_state(
                            api_app,
                            environment,
                            list(chunk_targets.keys()),
                            interval,
                            chunk_targets,
                        )
                        for symbol in chunk_targets:
                            preload_result = dict((chunk_results or {}).get(symbol) or {})
                            if bool(preload_result.get("is_ready")):
                                interval_state["ready_count"] += 1
                                env_result["ready_count"] += 1
                                summary["ready_count"] += 1
                            if bool(preload_result.get("indicator_seeded")):
                                interval_state["indicator_seeded"] += 1
                                env_result["indicator_seeded"] += 1
                                summary["indicator_seeded"] += 1
                            interval_state["symbol_completed"] += 1
                            env_result["symbol_completed"] += 1
                            summary["symbol_completed"] += 1
                        _publish_startup_preload_state(api_app, summary)
                else:
                    for symbol, target_ms in (targets or {}).items():
                        preload_result = _preload_symbol_indicator_state(
                            api_app,
                            environment,
                            symbol,
                            interval,
                            target_ms,
                            use_materialize=False,
                        )
                        if bool(preload_result.get("is_ready")):
                            interval_state["ready_count"] += 1
                            env_result["ready_count"] += 1
                            summary["ready_count"] += 1
                        if bool(preload_result.get("indicator_seeded")):
                            interval_state["indicator_seeded"] += 1
                            env_result["indicator_seeded"] += 1
                            summary["indicator_seeded"] += 1
                        interval_state["symbol_completed"] += 1
                        env_result["symbol_completed"] += 1
                        summary["symbol_completed"] += 1
                        _publish_startup_preload_state(api_app, summary)
                interval_state["status"] = "completed"
                env_result["interval_completed"] += 1
                _publish_startup_preload_state(api_app, summary)

            if fallback_symbols:
                interval_state = env_result["intervals"].setdefault(
                    "5m",
                    {
                        "status": "pending",
                        "symbol_count": len(fallback_symbols),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["status"] = "running"
                _publish_startup_preload_state(api_app, summary)
                indicator_seeded = 0
                chunk_size = _resolve_compute_startup_preload_chunk_size(api_app, environment)
                for index in range(0, len(fallback_symbols), chunk_size):
                    chunk_symbols = fallback_symbols[index:index + chunk_size]
                    fallback_results = _preload_interval_indicator_state(
                        api_app,
                        environment,
                        chunk_symbols,
                        "5m",
                        {},
                    )
                    for symbol in chunk_symbols:
                        result = dict((fallback_results or {}).get(symbol) or {})
                        if bool(result.get("is_ready")):
                            interval_state["ready_count"] += 1
                            env_result["ready_count"] += 1
                            summary["ready_count"] += 1
                        if bool(result.get("indicator_seeded")):
                            indicator_seeded += 1
                            interval_state["indicator_seeded"] += 1
                            env_result["indicator_seeded"] += 1
                            summary["indicator_seeded"] += 1
                        interval_state["symbol_completed"] += 1
                        env_result["symbol_completed"] += 1
                        summary["symbol_completed"] += 1
                    _publish_startup_preload_state(api_app, summary)
                env_result["storage_fallback_indicator_seeded"] = indicator_seeded
                if not interval_targets.get("5m"):
                    env_result["interval_completed"] += 1
                interval_state["status"] = "completed"
                _publish_startup_preload_state(api_app, summary)

            env_result["status"] = "completed"
            summary["env_completed"] += 1
            _publish_startup_preload_state(api_app, summary)
    except Exception as exc:
        traceback.print_exc()
        summary["ok"] = False
        summary["status"] = "failed"
        summary["running"] = False
        summary["error"] = str(exc)
        summary["finished_at"] = time.time()
        _publish_startup_preload_state(api_app, summary)
        return summary

    summary["status"] = "completed"
    summary["running"] = False
    summary["finished_at"] = time.time()
    _publish_startup_preload_state(api_app, summary)
    return summary


def _run_compute_startup_preload_thread(api_app=None):
    result = run_compute_startup_preload(api_app)
    elapsed_s = round(max(0.0, float(result.get("finished_at", 0.0) or 0.0) - float(result.get("started_at", 0.0) or 0.0)), 3)
    if result.get("ok", False):
        LOGGER.info(
            "Compute startup preload finished: envs=%s elapsed_s=%s",
            ",".join(result.get("environments") or []),
            elapsed_s,
        )
        return
    LOGGER.warning(
        "Compute startup preload failed: envs=%s elapsed_s=%s error=%s",
        ",".join(result.get("environments") or []),
        elapsed_s,
        result.get("error", ""),
    )


def schedule_compute_startup_preload() -> bool:
    global _STARTUP_PRELOAD_THREAD

    if not should_schedule_compute_startup_preload():
        return False

    with _STARTUP_PRELOAD_LOCK:
        thread = _STARTUP_PRELOAD_THREAD
        if thread is not None and thread.is_alive():
            return False
        api_app = _api_app()
        environments = resolve_compute_startup_preload_environments(api_app)
        _publish_startup_preload_state(
            api_app,
            {
                "ok": True,
                "enabled": is_compute_startup_preload_enabled(),
                "should_schedule": True,
                "scheduled": True,
                "status": "scheduled",
                "running": False,
                "service_profile": get_service_profile(),
                "runtime_mode": get_runtime_mode(),
                "environments": environments,
                "intervals": resolve_compute_startup_preload_intervals(api_app),
                "env_total": len(environments),
                "env_completed": 0,
                "symbol_total": 0,
                "symbol_completed": 0,
                "ready_count": 0,
                "indicator_seeded": 0,
                "started_at": 0.0,
                "finished_at": 0.0,
                "reason": "",
                "error": "",
                "results": {},
                "direct_backfill": _new_direct_backfill_state(
                    api_app,
                    environments[0] if environments else "live",
                ),
            },
        )
        _STARTUP_PRELOAD_THREAD = threading.Thread(
            target=_run_compute_startup_preload_thread,
            args=(api_app,),
            daemon=True,
            name="compute-startup-preload",
        )
        _STARTUP_PRELOAD_THREAD.start()
        return True


__all__ = [
    "get_compute_startup_preload_state",
    "is_compute_startup_preload_enabled",
    "resolve_compute_startup_preload_environments",
    "resolve_compute_startup_preload_intervals",
    "resolve_startup_direct_backfill_enabled",
    "resolve_startup_direct_backfill_intervals",
    "resolve_startup_direct_backfill_required_bars",
    "run_compute_startup_preload",
    "schedule_compute_startup_preload",
    "should_schedule_compute_startup_preload",
]
