from __future__ import annotations

import os

from ibkr_compute.api.service_topology import get_runtime_mode, get_service_profile
from ibkr_compute.market.timeframe_utils import normalize_interval

from .startup_preload_state_store import (
    DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS,
    DIRECT_BACKFILL_DEFAULT_CONID_SCAN_PAGES,
    DIRECT_BACKFILL_DEFAULT_INTERVALS,
    DIRECT_BACKFILL_DEFAULT_PERIODS,
    DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS,
    LOGGER,
    _api_app,
)


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


__all__ = [name for name in globals() if not name.startswith("__")]
