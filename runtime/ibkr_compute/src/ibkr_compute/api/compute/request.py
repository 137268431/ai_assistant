from __future__ import annotations

from ibkr_compute.api.route_request import get_json_payload
from ibkr_compute.core.broker_mode import resolve_market_data_mode


def _api_app():
    from .. import app as api_app

    return api_app


def _coerce_payload_bool(value, default: bool = False) -> bool:
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


def _coerce_payload_int(value, default: int = 0) -> int:
    if value is None:
        return int(default or 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default or 0)


def should_persist_compute_signals(payload: dict) -> bool:
    api_app = _api_app()
    if "persist_signals" in payload:
        return _coerce_payload_bool(payload.get("persist_signals"), False)
    source = str(payload.get("source") or "").strip().lower()
    return source not in api_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES


def get_rollup_intervals_for_source(source: str) -> list[str]:
    api_app = _api_app()
    configured = list(getattr(api_app, "HIGHER_INTERVALS", []) or [])
    if not configured:
        configured = [
            interval
            for interval in (getattr(api_app, "INTERVALS", []) or [])
            if str(interval or "").strip().lower() != "5m"
        ]

    normalized = []
    for value in configured:
        interval = str(value or "").strip().lower()
        if interval and interval not in normalized:
            normalized.append(interval)

    if source in {"canonical_close", "watchlist_idle_topup"}:
        # Daily rollup only closes when the trading day rolls over. Recomputing it
        # on every intraday 5m refresh turns each cycle into a multi-day rebuild.
        normalized = [interval for interval in normalized if interval != "1d"]

    return normalized


def _normalize_requested_intervals(values, allowed_intervals: list[str]) -> list[str]:
    if values is None:
        return []
    source = values if isinstance(values, list) else [values]
    allowed = {str(item or "").strip().lower() for item in allowed_intervals}
    normalized = []
    for value in source:
        interval = str(value or "").strip().lower()
        if interval and interval in allowed and interval not in normalized:
            normalized.append(interval)
    return normalized


def get_requested_environments(payload=None, defaults=None):
    api_app = _api_app()
    payload = payload if isinstance(payload, dict) else get_json_payload()
    requested = payload.get("environments")
    if requested is None:
        requested = payload.get("market_data_modes")
    if requested is None:
        requested = payload.get("market_data_mode")
    if requested is None:
        requested = payload.get("data_environment")
    if isinstance(requested, str):
        requested = [requested]

    if isinstance(requested, list):
        environments = []
        for value in requested:
            environment = resolve_market_data_mode(value)
            if environment in api_app.SUPPORTED_COMPUTE_ENVIRONMENTS and environment not in environments:
                environments.append(environment)
        if environments:
            return environments

    return list(defaults or api_app.DEFAULT_COMPUTE_ENVIRONMENTS)


def get_requested_symbols(payload=None):
    api_app = _api_app()
    payload = payload if isinstance(payload, dict) else get_json_payload()
    requested = payload.get("symbols")
    if requested is None:
        requested = payload.get("symbol")
    return api_app.normalize_symbols(requested)


def get_requested_persist_signal_symbols(payload=None):
    api_app = _api_app()
    payload = payload if isinstance(payload, dict) else get_json_payload()
    if "persist_signal_symbols" not in payload and "persist_signal_symbol" not in payload:
        return None
    requested = payload.get("persist_signal_symbols")
    if requested is None:
        requested = payload.get("persist_signal_symbol")
    if isinstance(requested, str):
        requested = requested.replace("\n", ",").split(",")
    elif isinstance(requested, (list, tuple, set)):
        items = []
        for value in requested:
            if isinstance(value, str):
                items.extend(value.replace("\n", ",").split(","))
            else:
                items.append(value)
        requested = items
    return api_app.normalize_symbols(requested)


def is_environment_compute_enabled(environment: str) -> bool:
    api_app = _api_app()
    runtime_environment = str(environment or "").strip().lower()
    if runtime_environment == "backtest" and not api_app.cfg.has_environment_override("ibkr_compute_enabled", runtime_environment):
        return False
    default_enabled = runtime_environment == "live"
    return api_app.cfg.get_bool_for_environment("ibkr_compute_enabled", runtime_environment, default_enabled)


def build_compute_disabled_payload(requested_environments) -> dict:
    return {
        "ok": True,
        "skipped": True,
        "reason": "compute_disabled",
        "requested_environments": requested_environments,
        "environments": [],
    }


def build_compute_execution_plan(payload=None) -> dict:
    api_app = _api_app()
    payload = payload if isinstance(payload, dict) else get_json_payload()
    source = str(payload.get("source") or "").strip().lower()
    requested_symbols = get_requested_symbols(payload)
    persist_signal_symbols = get_requested_persist_signal_symbols(payload)
    requested_environments = get_requested_environments(payload)
    enabled_environments = [env for env in requested_environments if is_environment_compute_enabled(env)]
    capture_signals = _coerce_payload_bool(payload.get("capture_signals"), False)
    targeted_rebuild = bool(requested_symbols) and source in {
        "history_repair",
        "history_rebuild",
        "recompute",
        "targeted_recompute",
    }
    targeted_rollup = bool(requested_symbols) and source in {
        "history_repair",
        "recompute",
        "targeted_recompute",
        "canonical_close",
        "watchlist_idle_topup",
    }
    force_rollup = _coerce_payload_bool(payload.get("force_rollup"), False) or source in {
        "recompute",
        "history_repair",
        "targeted_recompute",
        "canonical_close",
        "watchlist_idle_topup",
    }
    requested_intervals = _normalize_requested_intervals(payload.get("intervals"), api_app.INTERVALS)
    requested_rollup_intervals = _normalize_requested_intervals(payload.get("rollup_intervals"), api_app.HIGHER_INTERVALS)
    rollup_since_ms = max(0, _coerce_payload_int(payload.get("rollup_since_ms"), 0))
    default_intervals = ["5m"] if source == "ibkr_scheduler" and "intervals" not in payload else api_app.INTERVALS
    default_rollup_intervals = [] if source == "ibkr_scheduler" and "rollup_intervals" not in payload else get_rollup_intervals_for_source(source)
    return {
        "payload": payload,
        "source": source,
        "persist_signals": should_persist_compute_signals(payload),
        "persist_signal_symbols": persist_signal_symbols,
        "capture_signals": capture_signals,
        "requested_symbols": requested_symbols,
        "requested_environments": requested_environments,
        "enabled_environments": enabled_environments,
        "targeted_rebuild": targeted_rebuild,
        "targeted_rollup": targeted_rollup,
        "incremental_rollup": bool(requested_symbols) and source in {"canonical_close", "watchlist_idle_topup"},
        "skip_persisted_cursor": source in {"recompute", "history_repair", "history_rebuild", "targeted_recompute"},
        "force_rollup": force_rollup,
        "rollup_since_ms": rollup_since_ms,
        "rollup_intervals": requested_rollup_intervals if "rollup_intervals" in payload else default_rollup_intervals,
        "intervals": requested_intervals if "intervals" in payload else default_intervals,
    }
