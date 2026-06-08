from __future__ import annotations

import time
import traceback

from flask import jsonify

from ibkr_compute.api.route_request import get_json_payload, get_query_arg_csv, get_query_arg_int, get_query_arg_text
from ibkr_compute.api.route_request import get_query_arg_bool
from ibkr_compute.api.route_runtime import (
    build_runtime_environment_payload,
    get_requested_environment,
    get_service_status,
    require_ibkr_service,
)
from ibkr_compute.api.market.storage_quotes import fetch_storage_quote_snapshots, merge_quote_with_storage
from ibkr_compute.market.calendar import (
    DEFAULT_CALENDAR_EXCHANGE,
    DEFAULT_CALENDAR_SEC_TYPE,
    DEFAULT_CALENDAR_SYMBOL,
    build_ibkr_calendar_snapshot,
)
from ibkr_compute.market.timeframe_utils import bucket_start_ms, format_us_time


def build_ibkr_quotes_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    requested_environment = get_requested_environment("live")
    runtime_status = get_service_status(service)
    symbols = app_mod._normalize_symbol_list(get_query_arg_csv("symbols"))
    force_snapshot = get_query_arg_bool("snapshot", False) or get_query_arg_bool("force_snapshot", False)
    snapshot_timeout = max(0.2, min(float(get_query_arg_text("snapshot_timeout", "3") or 3.0), 10.0))
    items = service.realtime_quote_book.get_quotes(symbols=symbols)
    snapshot_results: dict[str, dict] = {}
    if symbols:
        by_symbol = {str(item.get("symbol") or "").strip().upper(): dict(item) for item in items if isinstance(item, dict)}
        if force_snapshot:
            requester = getattr(getattr(service, "ws_client", None), "request_market_data_snapshot", None)
            if not callable(requester):
                requester = getattr(getattr(service, "broker", None), "request_market_data_snapshot", None)
            for symbol in symbols:
                current = by_symbol.get(symbol) or {}
                conid = int(current.get("conid") or current.get("conidEx") or 0)
                if conid <= 0 and getattr(service, "conid_resolver", None) is not None:
                    try:
                        conid = int(service.conid_resolver.resolve(symbol) or 0)
                    except Exception:
                        conid = 0
                if not callable(requester) or conid <= 0:
                    snapshot_results[symbol] = {
                        "ok": False,
                        "error": "market_data_snapshot_unavailable" if not callable(requester) else "conid_unavailable",
                    }
                    continue
                try:
                    result = dict(requester(conid=conid, symbol=symbol, timeout=snapshot_timeout) or {})
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                snapshot_results[symbol] = result
                quote = result.get("quote") if isinstance(result.get("quote"), dict) else {}
                if not quote and isinstance(result.get("payload"), dict):
                    quote = result.get("payload") or {}
                if quote:
                    merged = {**current, **dict(quote)}
                    merged["symbol"] = symbol
                    merged["conid"] = int(merged.get("conid") or conid or 0)
                    merged["quote_fallback"] = False
                    merged["snapshot_requested"] = True
                    merged["snapshot_ok"] = result.get("ok") is not False
                    merged["snapshot_source"] = result.get("source") or "ibkr_market_data_snapshot"
                    merged["snapshot_error"] = str(result.get("error") or "")
                    if merged.get("quote_age_s") is None:
                        merged["quote_age_s"] = 0.0
                    by_symbol[symbol] = merged
        fallback_symbols = []
        for symbol in symbols:
            item = by_symbol.get(symbol) or {}
            if item.get("last_price") is None or item.get("day_change_pct") is None:
                fallback_symbols.append(symbol)
        if fallback_symbols:
            canonical = runtime_status.get("canonical_5m") or {}
            try:
                fallback_map = fetch_storage_quote_snapshots(
                    api_app=app_mod,
                    environment=requested_environment,
                    symbols=fallback_symbols,
                    safe_upper_ms=int(canonical.get("last_completed_bucket_ms", 0) or 0),
                    interval="5m",
                )
            except Exception:
                fallback_map = {}
            for symbol, snapshot in (fallback_map or {}).items():
                by_symbol[str(symbol or "").strip().upper()] = merge_quote_with_storage(by_symbol.get(symbol), snapshot)
            if fallback_map:
                items = [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]
        else:
            items = [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]
    return jsonify(
        {
            "ok": True,
            **build_runtime_environment_payload(app_mod, service, requested_environment),
            "count": len(items),
            "symbols": symbols,
            "items": items,
            "summary": (runtime_status.get("realtime_quotes") or {}),
            "snapshot_requested": bool(force_snapshot),
            "snapshot_results": snapshot_results if force_snapshot else {},
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


def build_ibkr_market_calendar_response():
    app_mod, service, unavailable = require_ibkr_service(restore=True)
    if unavailable:
        return unavailable

    market_date = get_query_arg_text("date") or get_query_arg_text("market_date")
    if not market_date:
        current_market_date = getattr(app_mod, "current_market_date", None)
        market_date = current_market_date() if callable(current_market_date) else ""
    symbol = get_query_arg_text("symbol", DEFAULT_CALENDAR_SYMBOL, upper=True) or DEFAULT_CALENDAR_SYMBOL
    exchange = get_query_arg_text("exchange", DEFAULT_CALENDAR_EXCHANGE, upper=True) or DEFAULT_CALENDAR_EXCHANGE
    sec_type = get_query_arg_text("sec_type", DEFAULT_CALENDAR_SEC_TYPE, upper=True) or DEFAULT_CALENDAR_SEC_TYPE
    conid = get_query_arg_int("conid", 0, minimum=0)

    broker = getattr(service, "broker", None)
    if broker is None or not hasattr(broker, "resolve_contract"):
        return jsonify({"ok": False, "error": "IBKR broker unavailable", "source": "ibkr_schedule"}), 503

    service_status = get_service_status(service)
    try:
        contract = broker.resolve_contract(
            symbol=symbol,
            conid=conid,
            exchange=exchange,
            sec_type=sec_type,
        )
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "source": "ibkr_schedule",
                **_build_contract_search_runtime_payload(app_mod, service, service_status),
            }
        ), 502
    if not contract:
        return jsonify(
            {
                "ok": False,
                "error": "contract_not_found",
                "source": "ibkr_schedule",
                "symbol": symbol,
                "conid": conid,
                "exchange": exchange,
                "sec_type": sec_type,
                **_build_contract_search_runtime_payload(app_mod, service, service_status),
            }
        ), 404

    payload = build_ibkr_calendar_snapshot(
        contract,
        market_date=market_date,
        symbol=symbol,
        exchange=exchange,
        sec_type=sec_type,
    )
    status_code = 200 if payload.get("ok") else 502
    return jsonify(
        {
            **payload,
            **_build_contract_search_runtime_payload(app_mod, service, service_status),
        }
    ), status_code
