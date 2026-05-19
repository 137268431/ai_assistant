from __future__ import annotations

import requests
from flask import jsonify, request

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
    get_query_arg_bool,
    get_query_arg_csv,
    get_json_payload,
    get_query_arg_int,
    get_query_arg_text,
)
from ibkr_compute.api.route_runtime import (
    get_app_module,
    get_requested_environment,
    require_ibkr_service,
)
from ibkr_compute.api.service_topology import (
    build_service_topology,
    get_runtime_internal_url,
    get_service_profile,
    get_runtime_mode,
)
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    normalize_broker_mode,
    resolve_data_environment,
    resolve_market_data_mode,
)
from ibkr_compute.backtest.execution_fills import (
    DEFAULT_PROFILE_STATE_KEY,
    build_calibrated_execution_cost_profile,
    fetch_execution_fills,
    normalize_execution_fills,
    parse_date_range_ms,
    parse_flex_xml_fills,
    summarize_execution_fills,
)
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.storage_cleanup import DEFAULT_PROFILE as STORAGE_CLEANUP_DEFAULT_PROFILE
from ibkr_compute.market.storage_cleanup import StorageCleanup


BACKTEST_RUN_LIST_COLUMNS = [
    "id",
    "name",
    "status",
    "source_environment",
    "environment",
    "date_from",
    "date_to",
    "symbols",
    "symbol_source",
    "session_mode",
    "created",
    "updated",
    "started_at",
    "finished_at",
    "duration_s",
    "initial_capital",
    "trade_count",
    "net_pnl",
    "total_return_pct",
    "sharpe",
    "max_drawdown_pct",
    "win_rate",
    "progress",
    "error",
    "benchmark_symbol",
    "commission_per_share",
    "slippage_bps",
]
BACKTEST_RUN_DETAIL_COLUMNS = [
    "benchmark_symbol",
    "commission_per_share",
    "created",
    "date_from",
    "date_to",
    "duration_s",
    "environment",
    "error",
    "extra",
    "finished_at",
    "id",
    "initial_capital",
    "max_drawdown_pct",
    "metrics",
    "name",
    "net_pnl",
    "params",
    "progress",
    "session_mode",
    "sharpe",
    "slippage_bps",
    "source_environment",
    "started_at",
    "status",
    "symbol_source",
    "symbols",
    "total_return_pct",
    "trade_count",
    "updated",
    "win_rate",
]
BACKTEST_BATCH_LIST_COLUMNS = [
    "id",
    "name",
    "status",
    "source_environment",
    "environment",
    "date_from",
    "date_to",
    "symbols",
    "symbol_source",
    "session_mode",
    "created",
    "updated",
    "started_at",
    "finished_at",
    "variant_count",
    "completed_count",
    "best_run_id",
    "best_variant_label",
    "best_total_return_pct",
    "best_sharpe",
    "benchmark_symbol",
    "error",
]
BACKTEST_BATCH_DETAIL_COLUMNS = [
    "benchmark_symbol",
    "best_run_id",
    "best_sharpe",
    "best_total_return_pct",
    "best_variant_label",
    "completed_count",
    "created",
    "date_from",
    "date_to",
    "environment",
    "error",
    "extra",
    "finished_at",
    "id",
    "leaderboard",
    "name",
    "params",
    "session_mode",
    "source_environment",
    "started_at",
    "status",
    "symbol_source",
    "symbols",
    "updated",
    "variant_count",
]


def _status_code_for_result(result: dict, *, error_status: int = 409, ok_status: int = 200) -> int:
    return ok_status if result.get("ok") else error_status


def _row_to_dict(row) -> dict:
    if not row:
        return {}
    return {key: row[key] for key in row.keys()}


def _select_columns(columns: list[str]) -> str:
    return ", ".join(columns)


def _query_backtest_collection(
    *,
    table: str,
    columns: list[str],
    environment: str,
    limit: int,
    order_column: str,
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    page_limit = max(1, min(100, int(limit or 40)))
    order = "updated" if order_column == "updated" else "created"
    with open_pb_sqlite(readonly=True, timeout=5.0) as conn:
        rows = conn.execute(
            f"""
            SELECT {_select_columns(columns)}
            FROM {table}
            WHERE source_environment = ?
            ORDER BY {order} DESC
            LIMIT ?
            """,
            (normalized_environment, page_limit),
        ).fetchall()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM {table} WHERE source_environment = ?",
            (normalized_environment,),
        ).fetchone()
    items = [_row_to_dict(row) for row in rows]
    return {
        "ok": True,
        "source": "sqlite",
        "environment": normalized_environment,
        "items": items,
        "page": 1,
        "perPage": page_limit,
        "totalItems": int((total_row or {}).get("total", 0) if hasattr(total_row, "get") else total_row["total"] if total_row else 0),
        "totalPages": 1,
    }


def _query_backtest_record(*, table: str, columns: list[str], record_id: str, id_label: str) -> tuple[dict, int]:
    normalized_id = str(record_id or "").strip()
    if not normalized_id:
        return {"ok": False, "error": f"missing_{id_label}"}, 400
    with open_pb_sqlite(readonly=True, timeout=5.0) as conn:
        row = conn.execute(
            f"""
            SELECT {_select_columns(columns)}
            FROM {table}
            WHERE id = ?
            LIMIT 1
            """,
            (normalized_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": f"{id_label}_not_found", id_label: normalized_id}, 404
    return {"ok": True, "source": "sqlite", "item": _row_to_dict(row)}, 200


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


def build_backtest_preload_status_response():
    app_mod = get_app_module()
    coordinator = getattr(app_mod, "backtest_preload_coordinator", None)
    if coordinator is None or not hasattr(coordinator, "status"):
        return jsonify({"ok": True, "available": False, "pending": 0, "inflight": 0, "failed": 0})
    try:
        payload = get_json_payload()
        symbols = payload.get("symbols") or payload.get("symbols_text") or payload.get("symbol") or ""
        if request.method == "POST" and symbols:
            environment = resolve_data_environment(
                payload.get("market_data_mode")
                or payload.get("data_environment")
                or payload.get("environment")
                or get_requested_environment("live")
            )
            result = coordinator.enqueue(
                symbols,
                environment=environment,
                trigger=str(payload.get("trigger") or "api_status_post").strip() or "api_status_post",
                reason=str(payload.get("reason") or "manual_preload").strip() or "manual_preload",
                date_from=str(payload.get("date_from") or "").strip() or None,
                date_to=str(payload.get("date_to") or "").strip() or None,
                lookback_days=coerce_request_int(payload.get("lookback_days"), 0, minimum=0) or None,
                warmup_bars=coerce_request_int(payload.get("warmup_bars"), 0, minimum=0) or None,
                source_payload=payload,
            )
            return jsonify(result)
        status = coordinator.status(include_jobs=coerce_request_bool(get_query_arg_text("full", ""), False))
        status["available"] = True
        return jsonify(status)
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


def build_storage_cleanup_response():
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    payload = get_json_payload()
    requested_environments = app_mod.get_requested_environments(defaults=app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS)
    dry_run = coerce_request_bool(payload.get("dry_run"), False)
    force = coerce_request_bool(payload.get("force"), False)
    source = str(payload.get("source") or "").strip().lower() or "api"
    profile = str(payload.get("profile") or STORAGE_CLEANUP_DEFAULT_PROFILE).strip() or STORAGE_CLEANUP_DEFAULT_PROFILE
    cleanup = StorageCleanup(
        pb_client=app_mod.pb,
        config=app_mod.cfg,
        default_environments=requested_environments,
    )
    result = cleanup.cleanup(
        environments=requested_environments,
        dry_run=dry_run,
        force=force,
        source=source,
        profile=profile,
    )
    return jsonify(
        {
            "ok": bool(result.get("ok", True)),
            "action": "storage_cleanup",
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
    status = app_mod.backtest_service.status()
    payload = dict(status) if isinstance(status, dict) else {"status": "unknown"}
    payload.setdefault("ok", bool(payload.get("status") != "error"))
    payload.setdefault("service", "ibkr-backtest")
    payload.setdefault("service_profile", get_service_profile())
    payload.setdefault("runtime_mode", get_runtime_mode())
    payload.setdefault("service_topology", build_service_topology())
    return jsonify(payload)


def build_backtest_runs_response():
    environment = get_requested_environment("live")
    limit = get_query_arg_int("limit", 40, minimum=1, maximum=100)
    order = get_query_arg_text("order", "created", lower=True)
    return jsonify(
        _query_backtest_collection(
            table="ibkr_backtest_runs",
            columns=BACKTEST_RUN_LIST_COLUMNS,
            environment=environment,
            limit=limit,
            order_column=order,
        )
    )


def build_backtest_run_detail_response():
    payload, status_code = _query_backtest_record(
        table="ibkr_backtest_runs",
        columns=BACKTEST_RUN_DETAIL_COLUMNS,
        record_id=get_query_arg_text("run_id") or get_query_arg_text("id"),
        id_label="run_id",
    )
    return jsonify(payload), status_code


def build_backtest_batches_response():
    environment = get_requested_environment("live")
    limit = get_query_arg_int("limit", 30, minimum=1, maximum=100)
    order = get_query_arg_text("order", "created", lower=True)
    return jsonify(
        _query_backtest_collection(
            table="ibkr_backtest_batches",
            columns=BACKTEST_BATCH_LIST_COLUMNS,
            environment=environment,
            limit=limit,
            order_column=order,
        )
    )


def build_backtest_batch_detail_response():
    payload, status_code = _query_backtest_record(
        table="ibkr_backtest_batches",
        columns=BACKTEST_BATCH_DETAIL_COLUMNS,
        record_id=get_query_arg_text("batch_id") or get_query_arg_text("id"),
        id_label="batch_id",
    )
    return jsonify(payload), status_code


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


def _execution_payload_environment(payload: dict) -> str:
    return normalize_broker_mode(
        (payload or {}).get("broker_mode")
        or get_query_arg_text("broker_mode")
        or (payload or {}).get("environment")
        or get_query_arg_text("environment"),
        configured_broker_mode(),
    )


def _execution_payload_symbols(payload: dict) -> list[str]:
    raw = (payload or {}).get("symbols") or (payload or {}).get("symbols_text")
    if raw is None:
        return [str(item or "").strip().upper() for item in get_query_arg_csv("symbols") if str(item or "").strip()]
    values = raw if isinstance(raw, list) else str(raw or "").replace("\n", ",").split(",")
    symbols = []
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _extract_uploaded_xml(payload: dict) -> str:
    for key in ("flex_xml", "xml", "content"):
        text = str((payload or {}).get(key) or "").strip()
        if text:
            return text
    files = getattr(request, "files", None)
    if not files:
        return ""
    for key in ("flex_xml_file", "file", "xml"):
        uploaded = files.get(key) if hasattr(files, "get") else None
        if not uploaded:
            continue
        data = uploaded.read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")
    return ""


def _extract_recent_fill_items(payload: dict) -> list[dict]:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    candidates = raw.get("orders") if isinstance(raw.get("orders"), list) else None
    if candidates is None:
        candidates = payload.get("orders") if isinstance(payload.get("orders"), list) else None
    if candidates is None:
        candidates = payload.get("items") if isinstance(payload.get("items"), list) else []
    items = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        raw_item = item.get("raw") if isinstance(item.get("raw"), dict) else None
        if raw_item:
            merged = dict(raw_item)
            for key in ("commission", "symbol", "ticker", "filled_qty", "fill_price", "fill_time", "order_id"):
                if key in item and key not in merged:
                    merged[key] = item.get(key)
            items.append(merged)
        else:
            items.append(dict(item))
    return items


def _upsert_execution_fills(app_mod, fills: list[dict], *, dry_run: bool) -> dict:
    if dry_run:
        return {"ok": True, "created": 0, "updated": 0, "skipped": 0, "total": len(fills), "dry_run": True}
    pb = getattr(app_mod, "pb", None)
    if pb is None:
        raise RuntimeError("pocketbase_client_unavailable")
    if hasattr(pb, "upsert_execution_fills"):
        return pb.upsert_execution_fills(fills)
    return pb._batch_upsert_records(
        "ibkr_execution_fills",
        fills,
        ["environment", "account", "exec_id"],
        timeout=30,
    )


def build_backtest_execution_cost_import_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    environment = _execution_payload_environment(payload)
    account = str(payload.get("account") or get_query_arg_text("account") or "").strip()
    source = str(payload.get("source") or get_query_arg_text("source") or "").strip().lower()
    dry_run = coerce_request_bool(payload.get("dry_run"), get_query_arg_bool("dry_run", False))
    xml_text = _extract_uploaded_xml(payload)
    try:
        if xml_text:
            fills = parse_flex_xml_fills(
                xml_text,
                environment=environment,
                account=account,
                source=source or "flex",
            )
            import_source = source or "flex"
        else:
            raw_items = payload.get("fills") if isinstance(payload.get("fills"), list) else payload.get("items")
            if not isinstance(raw_items, list):
                return jsonify({"ok": False, "error": "missing_fills_or_flex_xml"}), 400
            import_source = source or "manual"
            fills = normalize_execution_fills(
                raw_items,
                environment=environment,
                account=account,
                source=import_source,
            )
        result = _upsert_execution_fills(app_mod, fills, dry_run=dry_run)
        return jsonify(
            {
                "ok": True,
                "source": import_source,
                "environment": environment,
                "account": account,
                "dry_run": dry_run,
                "imported": len(fills),
                "summary": summarize_execution_fills(fills),
                "upsert": result,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "environment": environment, "account": account}), 500


def _fetch_recent_fills_from_runtime(environment: str, days: int) -> dict:
    response = requests.get(
        f"{get_runtime_internal_url().rstrip('/')}/ibkr/orders/history",
        params={"broker_mode": environment, "environment": environment, "days": max(1, int(days or 1))},
        timeout=30,
    )
    payload = response.json() if response.content else {}
    if not response.ok:
        return {
            "ok": False,
            "error": str((payload or {}).get("error") or f"runtime_history_status_{response.status_code}"),
            "payload": payload if isinstance(payload, dict) else {},
        }
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_runtime_history_payload"}


def build_backtest_execution_cost_import_recent_fills_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    environment = _execution_payload_environment(payload)
    account = str(payload.get("account") or get_query_arg_text("account") or "").strip()
    days = coerce_request_int(payload.get("days"), get_query_arg_int("days", 1, minimum=1), minimum=1, maximum=30)
    dry_run = coerce_request_bool(payload.get("dry_run"), get_query_arg_bool("dry_run", False))
    broker_payload = {}
    broker_source = "runtime_order_history"
    try:
        service_getter = getattr(app_mod, "get_ibkr_service", None)
        service = service_getter() if callable(service_getter) else None
        tracker = getattr(service, "order_tracker", None) if service is not None else None
        if tracker is not None and hasattr(tracker, "get_broker_order_history"):
            broker_payload = tracker.get_broker_order_history(days=days, force=True)
            broker_source = "local_order_tracker"
        else:
            broker_payload = _fetch_recent_fills_from_runtime(environment, days)
        if not broker_payload.get("ok", True):
            return jsonify(
                {
                    "ok": False,
                    "error": broker_payload.get("error") or "recent_fills_unavailable",
                    "source": broker_source,
                    "environment": environment,
                }
            ), 502
        raw_items = _extract_recent_fill_items(broker_payload)
        fills = normalize_execution_fills(
            raw_items,
            environment=environment,
            account=account,
            source="recent_fills",
        )
        result = _upsert_execution_fills(app_mod, fills, dry_run=dry_run)
        return jsonify(
            {
                "ok": True,
                "source": broker_source,
                "environment": environment,
                "account": account,
                "days": days,
                "dry_run": dry_run,
                "raw_count": len(raw_items),
                "imported": len(fills),
                "summary": summarize_execution_fills(fills),
                "upsert": result,
                "limitations": broker_payload.get("limitations") or [],
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "source": broker_source, "environment": environment}), 500


def _execution_query_window(payload: dict) -> tuple[int, int]:
    start_ms = coerce_request_int(payload.get("start_ms"), get_query_arg_int("start_ms", 0, minimum=0), minimum=0)
    end_ms = coerce_request_int(payload.get("end_ms"), get_query_arg_int("end_ms", 0, minimum=0), minimum=0)
    if start_ms or end_ms:
        return start_ms, end_ms
    return parse_date_range_ms(
        payload.get("date_from") or get_query_arg_text("date_from"),
        payload.get("date_to") or get_query_arg_text("date_to"),
    )


def build_backtest_execution_cost_fills_response():
    payload = get_json_payload()
    environment = _execution_payload_environment(payload)
    account = str(payload.get("account") or get_query_arg_text("account") or "").strip()
    source = str(payload.get("source") or get_query_arg_text("source") or "").strip().lower()
    start_ms, end_ms = _execution_query_window(payload)
    limit = coerce_request_int(payload.get("limit"), get_query_arg_int("limit", 200, minimum=1, maximum=20000), minimum=1, maximum=20000)
    result = fetch_execution_fills(
        environment=environment,
        symbols=_execution_payload_symbols(payload),
        account=account,
        source=source,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=limit,
    )
    result.update({"environment": environment, "account": account, "source_filter": source, "limit": limit})
    return jsonify(result)


def build_backtest_execution_cost_profile_response():
    app_mod = get_app_module()
    payload = get_json_payload()
    environment = _execution_payload_environment(payload)
    account = str(payload.get("account") or get_query_arg_text("account") or "").strip()
    source = str(payload.get("source") or get_query_arg_text("source") or "").strip().lower()
    start_ms, end_ms = _execution_query_window(payload)
    limit = coerce_request_int(payload.get("limit"), get_query_arg_int("limit", 5000, minimum=1, maximum=20000), minimum=1, maximum=20000)
    persist_state = coerce_request_bool(payload.get("persist_state"), get_query_arg_bool("persist_state", False))
    base_request = payload.get("base_request") if isinstance(payload.get("base_request"), dict) else payload

    try:
        xml_text = _extract_uploaded_xml(payload)
        if xml_text:
            fills = parse_flex_xml_fills(xml_text, environment=environment, account=account, source=source or "flex")
            fill_source = source or "flex_payload"
            query_payload = {"ok": True, "available": True, "summary": summarize_execution_fills(fills)}
        elif isinstance(payload.get("fills"), list) or isinstance(payload.get("items"), list):
            raw_items = payload.get("fills") if isinstance(payload.get("fills"), list) else payload.get("items")
            fills = normalize_execution_fills(raw_items, environment=environment, account=account, source=source or "manual")
            fill_source = source or "request_payload"
            query_payload = {"ok": True, "available": True, "summary": summarize_execution_fills(fills)}
        else:
            query_payload = fetch_execution_fills(
                environment=environment,
                symbols=_execution_payload_symbols(payload),
                account=account,
                source=source,
                start_ms=start_ms,
                end_ms=end_ms,
                limit=limit,
            )
            fills = query_payload.get("items") or []
            fill_source = "ibkr_execution_fills"
        profile_payload = build_calibrated_execution_cost_profile(
            fills,
            base_request=base_request,
            environment=environment,
            account=account,
            source=fill_source,
        )
        state_result = {}
        if persist_state and profile_payload.get("profile_available"):
            state_date = str(payload.get("state_date") or account or "global").strip() or "global"
            state_key = str(payload.get("state_key") or DEFAULT_PROFILE_STATE_KEY).strip() or DEFAULT_PROFILE_STATE_KEY
            state_result = app_mod.pb.upsert_state(
                state_key,
                environment,
                {
                    "profile": profile_payload.get("profile") or {},
                    "execution_cost_profile": profile_payload.get("execution_cost_profile") or {},
                    "backtest_payload_patch": profile_payload.get("backtest_payload_patch") or {},
                    "sample": profile_payload.get("sample") or {},
                    "query": {
                        "account": account,
                        "source": source,
                        "symbols": _execution_payload_symbols(payload),
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "limit": limit,
                    },
                },
                date=state_date,
            )
        return jsonify(
            {
                **profile_payload,
                "environment": environment,
                "account": account,
                "source_filter": source,
                "fill_source": fill_source,
                "fill_query": {
                    "available": query_payload.get("available", True),
                    "summary": query_payload.get("summary") or summarize_execution_fills(fills),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "limit": limit,
                },
                "persist_state": persist_state,
                "state": {
                    "saved": bool(state_result),
                    "id": str((state_result or {}).get("id") or ""),
                    "state_key": str((state_result or {}).get("state_key") or DEFAULT_PROFILE_STATE_KEY if state_result else ""),
                },
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "environment": environment, "account": account}), 500


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
    runtime_environment = resolve_market_data_mode(
        payload.get("market_data_mode") or payload.get("data_environment")
    )
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
