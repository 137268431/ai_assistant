from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.ops.common import _resolve_data_quality_symbols
from ibkr_compute.api.route_request import (
    coerce_request_bool,
    coerce_request_int,
    get_json_payload,
    get_query_arg_int,
    get_query_arg_text,
)
from ibkr_compute.api.route_runtime import (
    get_app_module,
    get_requested_environment,
    require_ibkr_service,
)
from ibkr_compute.market.data_retention import DataRetention


def _status_code_for_result(result: dict, *, error_status: int = 409, ok_status: int = 200) -> int:
    return ok_status if result.get("ok") else error_status


def build_history_rebuild_start_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    result = app_mod.history_rebuild_manager.start(payload)
    return jsonify(result), _status_code_for_result(result)


def build_history_rebuild_status_response():
    app_mod = get_app_module()
    environment = get_requested_environment("live")
    return jsonify(app_mod.history_rebuild_manager.status(environment))


def build_retention_cleanup_response():
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    payload = get_json_payload()
    requested_environments = app_mod.get_requested_environments(defaults=app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS)
    retention_days = coerce_request_int(payload.get("retention_days"), 0, minimum=0)
    source = str(payload.get("source") or "").strip().lower() or "api"
    force = coerce_request_bool(payload.get("force"), False)
    retention = DataRetention(
        pb_client=app_mod.pb,
        config=app_mod.cfg,
        default_environments=requested_environments,
    )
    result = retention.cleanup(
        environments=requested_environments,
        retention_days=retention_days if retention_days > 0 else None,
        source=source,
        force=force,
    )
    return jsonify(
        {
            "ok": bool(result.get("ok", True)),
            "action": "retention_cleanup",
            "requested_environments": requested_environments,
            **result,
        }
    )


def build_backtest_run_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    result = app_mod.backtest_service.start_run(payload)
    return jsonify(result), _status_code_for_result(result)


def build_backtest_status_response():
    app_mod = get_app_module()
    return jsonify(app_mod.backtest_service.status())


def build_backtest_cancel_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    run_id = str(payload.get("run_id") or "").strip()
    result = app_mod.backtest_service.cancel(run_id)
    return jsonify(result), _status_code_for_result(result)


def build_backtest_replay_response():
    app_mod = get_app_module()
    run_id = get_query_arg_text("run_id")
    symbol = get_query_arg_text("symbol", upper=True)
    center_bar_ms = get_query_arg_int("center_bar_ms", 0, minimum=0)
    window = get_query_arg_int("window", 80, minimum=1)
    result = app_mod.backtest_service.replay(run_id, symbol, center_bar_ms=center_bar_ms, window=window)
    return jsonify(result), _status_code_for_result(result, error_status=404)


def build_backtest_cleanup_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    run_id = str(payload.get("run_id") or "").strip()
    batch_id = str(payload.get("batch_id") or "").strip()
    result = app_mod.backtest_service.cleanup(run_id=run_id, batch_id=batch_id)
    return jsonify(result), _status_code_for_result(result)


def _build_data_quality_response(*, force_repair: bool | None = None):
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return unavailable

    payload = get_json_payload()
    symbols = _resolve_data_quality_symbols(service, payload)
    scan_scope = str(payload.get("scan_scope") or "manual").strip().lower() or "manual"
    persist = coerce_request_bool(payload.get("persist"), True)
    repair = coerce_request_bool(payload.get("repair"), False) if force_repair is None else bool(force_repair)
    result = service.scan_bar_integrity(
        symbols,
        scan_scope=scan_scope,
        persist=persist,
        repair=repair,
    )
    return jsonify(
        {
            "ok": True,
            "symbols": symbols,
            "scan_scope": scan_scope,
            **result,
        }
    )


def build_ibkr_data_quality_scan_response():
    return _build_data_quality_response(force_repair=None)


def build_ibkr_data_quality_repair_response():
    return _build_data_quality_response(force_repair=True)
