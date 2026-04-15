from __future__ import annotations

import time
import traceback

from flask import jsonify

from ibkr_compute.api.route_request import get_json_payload, get_query_arg_csv, get_query_arg_int, get_query_arg_text
from ibkr_compute.api.route_runtime import (
    build_runtime_environment_payload,
    get_requested_environment,
    get_service_status,
    require_ibkr_service,
)
from ibkr_compute.market.timeframe_utils import bucket_start_ms, format_us_time


def build_ibkr_quotes_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    requested_environment = get_requested_environment("live")
    runtime_status = get_service_status(service)
    symbols = app_mod._normalize_symbol_list(get_query_arg_csv("symbols"))
    items = service.realtime_quote_book.get_quotes(symbols=symbols)
    return jsonify(
        {
            "ok": True,
            **build_runtime_environment_payload(app_mod, service, requested_environment),
            "count": len(items),
            "symbols": symbols,
            "items": items,
            "summary": (runtime_status.get("realtime_quotes") or {}),
        }
    )


def build_ibkr_forming_bar_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    requested_environment = get_requested_environment("live")
    symbol = get_query_arg_text("symbol", upper=True)
    if not symbol:
        return jsonify({"ok": False, "error": "missing_symbol"}), 400

    preview = service.bar_aggregator.get_preview_bar(symbol)
    current_bucket_ms = bucket_start_ms(int(time.time() * 1000), "5m")
    if preview and int(preview.get("bar_time_ms", 0) or 0) != current_bucket_ms:
        preview = None
    quote = service.realtime_quote_book.get_quote(symbol)
    return jsonify(
        {
            "ok": True,
            **build_runtime_environment_payload(app_mod, service, requested_environment),
            "symbol": symbol,
            "interval": "5m",
            "preview": bool(preview),
            "current_bucket_ms": current_bucket_ms,
            "current_bucket_us": format_us_time(current_bucket_ms),
            "bar": preview,
            "quote": quote,
        }
    )


def build_ibkr_ingest_close_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    payload = get_json_payload()
    symbols = app_mod.get_requested_symbols(payload)
    service._run_official_5m_close_cycle(symbols_override=symbols or None)
    runtime_status = get_service_status(service)
    canonical = runtime_status.get("canonical_5m") or {}
    compute_result = runtime_status.get("realtime_compute") or {}
    return jsonify(
        {
            "ok": True,
            **build_runtime_environment_payload(app_mod, service),
            "symbols": symbols,
            "due_bucket_ms": int(canonical.get("last_due_bucket_ms", 0) or 0),
            "written_symbols": canonical.get("written_symbols") or [],
            "pending_symbols": canonical.get("pending_symbols") or [],
            "written_bars": int(canonical.get("last_written_bars", 0) or 0),
            "compute_triggered": int(canonical.get("last_written_bars", 0) or 0) > 0,
            "compute_result": compute_result.get("last_result") or {},
            "canonical_5m": canonical,
        }
    )


def _build_contract_search_runtime_payload(app_mod, service, service_status: dict) -> dict:
    return {
        **build_runtime_environment_payload(app_mod, service),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
    }


def build_contracts_search_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    query = get_query_arg_text("q")
    limit = get_query_arg_int("limit", 12, minimum=1, maximum=24)
    if not query:
        return jsonify({"ok": False, "error": "missing_query"}), 400
    if not hasattr(service, "conid_resolver") or service.conid_resolver is None:
        return jsonify({"ok": False, "error": "IBKR contract resolver unavailable"}), 503

    service_status = get_service_status(service)
    try:
        items = service.conid_resolver.search_contracts(query, limit=limit)
        return jsonify(
            {
                "ok": True,
                "query": query,
                "limit": limit,
                "count": len(items),
                "items": items,
                **_build_contract_search_runtime_payload(app_mod, service, service_status),
            }
        ), 200
    except Exception as exc:
        traceback.print_exc()
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "query": query,
                "limit": limit,
                **_build_contract_search_runtime_payload(app_mod, service, service_status),
            }
        ), 500
