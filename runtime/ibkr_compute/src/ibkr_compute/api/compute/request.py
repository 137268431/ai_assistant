from __future__ import annotations

from ibkr_compute.api.route_request import get_json_payload


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


def should_persist_compute_signals(payload: dict) -> bool:
    api_app = _api_app()
    if "persist_signals" in payload:
        return _coerce_payload_bool(payload.get("persist_signals"), False)
    source = str(payload.get("source") or "").strip().lower()
    return source not in api_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES


def get_requested_environments(payload=None, defaults=None):
    api_app = _api_app()
    payload = payload if isinstance(payload, dict) else get_json_payload()
    requested = payload.get("environments")
    if requested is None:
        requested = payload.get("environment")
    if isinstance(requested, str):
        requested = [requested]

    if isinstance(requested, list):
        environments = []
        for value in requested:
            environment = str(value or "").strip().lower()
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


def is_environment_compute_enabled(environment: str) -> bool:
    api_app = _api_app()
    runtime_environment = str(environment or "").strip().lower()
    if runtime_environment == "backtest" and not api_app.cfg.has_environment_override("ibkr_compute_enabled", runtime_environment):
        return False
    default_enabled = runtime_environment in ("live", "paper")
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
    requested_environments = get_requested_environments(payload)
    enabled_environments = [env for env in requested_environments if is_environment_compute_enabled(env)]
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
    }
    force_rollup = _coerce_payload_bool(payload.get("force_rollup"), False) or source in {
        "recompute",
        "history_repair",
        "targeted_recompute",
        "canonical_close",
    }
    return {
        "payload": payload,
        "source": source,
        "persist_signals": should_persist_compute_signals(payload),
        "requested_symbols": requested_symbols,
        "requested_environments": requested_environments,
        "enabled_environments": enabled_environments,
        "targeted_rebuild": targeted_rebuild,
        "targeted_rollup": targeted_rollup,
        "incremental_rollup": bool(requested_symbols) and source == "canonical_close",
        "skip_persisted_cursor": source in {"recompute", "history_repair", "history_rebuild", "targeted_recompute"},
        "force_rollup": force_rollup,
        "intervals": api_app.INTERVALS,
    }
