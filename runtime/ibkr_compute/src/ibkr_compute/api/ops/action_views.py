from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.ops.bar_truth_compare import build_bar_truth_compare_payload
from ibkr_compute.api.ops.common import _resolve_data_quality_symbols
from ibkr_compute.api.ops.data_quality_truth import (
    TRUTH_AUDIT_INTERVAL,
    build_truth_audit_row,
    build_truth_audit_summary,
    resolve_truth_audit_window,
)
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
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
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


def build_bar_repair_status_response():
    app_mod = get_app_module()
    coordinator = getattr(app_mod, "bar_repair_coordinator", None)
    if coordinator is None or not hasattr(coordinator, "status"):
        return jsonify({"ok": True, "available": False, "pending": 0, "inflight": 0, "failed": 0})
    try:
        payload = coordinator.status(include_jobs=coerce_request_bool(get_query_arg_text("full", ""), False))
        payload["available"] = True
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "available": True, "error": str(exc)}), 500


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


def _build_data_quality_coverage(symbols: list[str], rows: list[dict]) -> dict:
    expected_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    scanned_symbols = sorted(
        {
            str((row or {}).get("symbol") or "").strip().upper()
            for row in (rows or [])
            if str((row or {}).get("symbol") or "").strip()
        }
    )
    scanned_set = set(scanned_symbols)
    expected_set = set(expected_symbols)
    return {
        "expected_symbols_total": len(expected_symbols),
        "scanned_symbols_total": len(scanned_symbols),
        "coverage_complete": bool(expected_symbols) and expected_set.issubset(scanned_set),
        "expected_symbols": expected_symbols,
        "scanned_symbols": scanned_symbols,
        "unscanned_symbols": sorted(expected_set - scanned_set) if expected_symbols else [],
    }


def _coerce_session_modes(value) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = ["regular", "extended"]
    modes = []
    for item in raw_items:
        mode = str(item or "").strip().lower()
        if mode in {"regular", "extended"} and mode not in modes:
            modes.append(mode)
    return modes or ["regular"]


def _build_data_quality_response(*, force_repair: bool | None = None):
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return unavailable

    payload = get_json_payload()
    symbols = _resolve_data_quality_symbols(service, payload)
    requested_scan_scope = str(payload.get("scan_scope") or "manual").strip().lower() or "manual"
    effective_scan_scope = "watchlist" if requested_scan_scope == "watchlist_full" else requested_scan_scope
    persist = coerce_request_bool(payload.get("persist"), True)
    repair = coerce_request_bool(payload.get("repair"), False) if force_repair is None else bool(force_repair)
    allow_repair_defer = coerce_request_bool(
        payload.get("allow_repair_defer"),
        False if repair else True,
    )
    result = service.scan_bar_integrity(
        symbols,
        scan_scope=effective_scan_scope,
        persist=persist,
        repair=repair,
        allow_repair_defer=allow_repair_defer,
    )
    coverage = _build_data_quality_coverage(symbols, result.get("rows") or [])
    result_summary = dict(result.get("summary") or {})
    result_summary.update(coverage)
    result["summary"] = result_summary
    return jsonify(
        {
            "ok": True,
            "symbols": symbols,
            "scan_scope": requested_scan_scope,
            "effective_scan_scope": effective_scan_scope,
            **result,
        }
    )


def build_ibkr_data_quality_scan_response():
    return _build_data_quality_response(force_repair=None)


def build_ibkr_data_quality_repair_response():
    return _build_data_quality_response(force_repair=True)


def build_ibkr_data_quality_truth_audit_response():
    app_mod = get_app_module()
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return unavailable

    payload = get_json_payload()
    requested_scan_scope = str(payload.get("scan_scope") or "manual").strip().lower() or "manual"
    symbols = _resolve_data_quality_symbols(service, payload)
    persist = coerce_request_bool(payload.get("persist"), True)
    chunk_size = coerce_request_int(payload.get("chunk_size"), 20, minimum=1)
    runtime_environment = app_mod._ibkr_service_environment(service)
    market_date = str(payload.get("market_date") or getattr(service, "_bar_integrity_market_date", lambda: app_mod.current_market_date())()).strip()
    if not market_date:
        market_date = app_mod.current_market_date()

    try:
        window = resolve_truth_audit_window(market_date, TRUTH_AUDIT_INTERVAL)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "market_date": market_date}), 400

    status_payload = get_service_status_snapshot(service)
    gateway_running = bool((status_payload.get("gateway") or {}).get("running"))
    session_authenticated = bool((status_payload.get("session") or {}).get("authenticated"))
    if not gateway_running:
        return jsonify({"ok": False, "error": "IBKR gateway not running", "market_date": market_date}), 409
    if not session_authenticated:
        return jsonify({"ok": False, "error": "IBKR session not authenticated", "market_date": market_date}), 409

    rows: list[dict] = []
    errors: list[dict] = []
    for start in range(0, len(symbols), max(1, chunk_size)):
        batch = symbols[start:start + max(1, chunk_size)]
        for symbol in batch:
            try:
                compare_payload = build_bar_truth_compare_payload(
                    runtime_environment,
                    symbol,
                    TRUTH_AUDIT_INTERVAL,
                    start_ms=int(window["start_ms"]),
                    end_ms=int(window["end_ms"]),
                )
                comparison = compare_payload.get("comparison") or {}
                row = build_truth_audit_row(
                    environment=runtime_environment,
                    market_date=market_date,
                    symbol=symbol,
                    interval=TRUTH_AUDIT_INTERVAL,
                    window_start_ms=int(window["start_ms"]),
                    window_end_ms=int(window["end_ms"]),
                    comparison_summary=comparison.get("summary") or {},
                    mismatch_examples=comparison.get("mismatch_examples") or [],
                    source_meta={
                        "requested_scan_scope": requested_scan_scope,
                        "audit_mode": "bar_only",
                        "stored": compare_payload.get("meta", {}).get("stored") or {},
                        "ibkr": compare_payload.get("meta", {}).get("ibkr") or {},
                    },
                    last_checked_at=service._now_iso() if hasattr(service, "_now_iso") else "",
                )
            except Exception as exc:
                error_text = str(exc)
                row = build_truth_audit_row(
                    environment=runtime_environment,
                    market_date=market_date,
                    symbol=symbol,
                    interval=TRUTH_AUDIT_INTERVAL,
                    window_start_ms=int(window["start_ms"]),
                    window_end_ms=int(window["end_ms"]),
                    comparison_summary={},
                    mismatch_examples=[],
                    source_meta={
                        "requested_scan_scope": requested_scan_scope,
                        "audit_mode": "bar_only",
                    },
                    error=error_text,
                    last_checked_at=service._now_iso() if hasattr(service, "_now_iso") else "",
                )
                errors.append({"symbol": symbol, "error": error_text})
            rows.append(row)

    if persist and rows:
        try:
            service.pb.upsert_bar_truth_audit_items(rows)
        except Exception as exc:
            errors.append({"symbol": "", "error": f"persist_failed:{exc}"})

    summary = build_truth_audit_summary(rows, expected_symbols=symbols)
    summary["market_date"] = market_date
    summary["scan_scope"] = requested_scan_scope
    summary["audit_mode"] = "bar_only"
    summary["window_start_ms"] = int(window["start_ms"])
    summary["window_end_ms"] = int(window["end_ms"])
    summary["error_count"] = len(errors)
    return jsonify(
        {
            "ok": True,
            "symbols": symbols,
            "scan_scope": requested_scan_scope,
            "audit_mode": "bar_only",
            "market_date": market_date,
            "rows": rows,
            "summary": summary,
            "errors": errors,
        }
    ), 200


def build_ibkr_data_quality_daily_rescan_response():
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return unavailable

    payload = get_json_payload()
    symbols = _resolve_data_quality_symbols(service, payload)
    market_date = str(payload.get("market_date") or "").strip()
    date_from = str(payload.get("date_from") or market_date or "").strip()
    date_to = str(payload.get("date_to") or market_date or date_from).strip()
    if not date_from:
        date_from = service._bar_integrity_market_date() if hasattr(service, "_bar_integrity_market_date") else ""
    if not date_to:
        date_to = date_from
    persist = coerce_request_bool(payload.get("persist"), True)
    result = service.scan_bar_coverage_daily(
        symbols,
        market_date=market_date,
        date_from=date_from,
        date_to=date_to,
        interval=str(payload.get("interval") or "5m").strip() or "5m",
        session_modes=_coerce_session_modes(payload.get("session_modes")),
        source=str(payload.get("source") or "manual_scan").strip() or "manual_scan",
        persist=persist,
    )
    return jsonify({"ok": bool(result.get("ok", False)), "symbols": symbols, **result}), _status_code_for_result(result, error_status=500)


def build_ibkr_data_quality_daily_repair_response():
    app_mod = get_app_module()
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return unavailable

    payload = get_json_payload()
    symbols = _resolve_data_quality_symbols(service, payload)
    market_date = str(payload.get("market_date") or "").strip()
    date_from = str(payload.get("date_from") or market_date or "").strip()
    date_to = str(payload.get("date_to") or market_date or date_from).strip()
    if not date_from:
        date_from = service._bar_integrity_market_date() if hasattr(service, "_bar_integrity_market_date") else ""
    if not date_to:
        date_to = date_from
    interval = str(payload.get("interval") or "5m").strip() or "5m"
    source_environment = app_mod._ibkr_service_environment(service)
    initial = service.scan_bar_coverage_daily(
        symbols,
        market_date=market_date,
        date_from=date_from,
        date_to=date_to,
        interval=interval,
        session_modes=_coerce_session_modes(payload.get("session_modes") or ["regular"]),
        source=str(payload.get("source") or "manual_repair_scan").strip() or "manual_repair_scan",
        persist=True,
    )
    if not initial.get("ok"):
        return jsonify({"ok": False, "symbols": symbols, "initial": initial}), 500

    backtest_service = getattr(app_mod, "backtest_service", None)
    if backtest_service is None or getattr(backtest_service, "data_backfill", None) is None:
        return jsonify({"ok": False, "error": "history_repair_unavailable", "symbols": symbols, "initial": initial}), 409
    if hasattr(backtest_service, "is_running") and backtest_service.is_running():
        return jsonify({"ok": False, "error": "backtest_service_busy", "symbols": symbols, "initial": initial}), 409

    max_symbols = coerce_request_int(payload.get("repair_max_symbols"), len(symbols), minimum=1)
    max_windows = coerce_request_int(payload.get("repair_max_windows"), 200, minimum=1)
    rows = list(initial.get("rows") or [])
    windows_by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        if not bool(row.get("needs_repair")):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        for window in row.get("repair_windows") or []:
            if len(windows_by_symbol.setdefault(symbol, [])) >= max_windows:
                break
            windows_by_symbol[symbol].append(dict(window or {}))
    selected_symbols = sorted(windows_by_symbol.keys())[:max_symbols]
    results: dict[str, dict] = {}
    for symbol in selected_symbols:
        fetched_rows = []
        repair_results = []
        for window in windows_by_symbol.get(symbol, [])[:max_windows]:
            repair = backtest_service._backfill_symbol_history(
                symbol,
                source_environment,
                int(window.get("start_ms", 0) or 0),
                int(window.get("end_ms", 0) or 0),
                interval=interval,
                max_elapsed_s=coerce_request_int(payload.get("repair_symbol_timeout_s"), 600, minimum=30),
                max_batches=coerce_request_int(payload.get("repair_max_batches"), 120, minimum=1),
                history_timeout_s=coerce_request_int(payload.get("history_timeout_s"), 20, minimum=5),
                history_max_retries=coerce_request_int(payload.get("history_max_retries"), 1, minimum=0),
            )
            repair_result = {key: value for key, value in (repair or {}).items() if key != "rows"}
            repair_result["window"] = window
            repair_results.append(repair_result)
            fetched_rows.extend(list((repair or {}).get("rows") or []))
        deduped = backtest_service._dedupe_backfill_rows(fetched_rows)
        persisted = backtest_service._persist_backfill_rows(deduped) if deduped else 0
        rolled = backtest_service._rollup_symbol_history(symbol, source_environment) if persisted > 0 else 0
        results[symbol] = {
            "ok": bool(persisted > 0 or not windows_by_symbol.get(symbol)),
            "fetched_rows": len(deduped),
            "persisted_rows": persisted,
            "rolled_rows": rolled,
            "repair_window_count": len(windows_by_symbol.get(symbol) or []),
            "repair_results": repair_results[:20],
        }

    final = service.scan_bar_coverage_daily(
        symbols,
        market_date=market_date,
        date_from=date_from,
        date_to=date_to,
        interval=interval,
        session_modes=_coerce_session_modes(payload.get("session_modes") or ["regular"]),
        source=str(payload.get("source") or "manual_repair_verify").strip() or "manual_repair_verify",
        persist=True,
    )
    return jsonify(
        {
            "ok": True,
            "symbols": symbols,
            "source_environment": source_environment,
            "initial": initial,
            "results": results,
            "final": final,
        }
    ), 200
