from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.data_quality.queries import (
    build_daily_coverage_summary,
    build_summary,
    build_truth_summary,
    load_daily_coverage_items,
    load_effective_watchlist_symbols,
    load_integrity_items,
    load_truth_items,
    merge_items,
    paginate,
)
from ibkr_api.orders.values import parse_boolean, parse_integer, to_text
from ibkr_compute.core.broker_mode import resolve_data_environment


def _request_data_environment(args: Any) -> str:
    return resolve_data_environment(args.get("market_data_mode") or args.get("data_environment") or args.get("environment"))


def register_data_quality_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/data_quality/summary", methods=["GET"])
    def custom_ibkr_data_quality_summary() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        scan_scope = to_text(request.args.get("scan_scope"))
        try:
            integrity_items = load_integrity_items(pb, environment, market_date=market_date, scan_scope=scan_scope)
            truth_items = load_truth_items(pb, environment, market_date=market_date)
            merged_items = merge_items(integrity_items, truth_items)
            expected_symbols = (
                load_effective_watchlist_symbols(pb, environment)
                if not scan_scope or scan_scope == "watchlist"
                else [to_text(item.get("symbol")).upper() for item in merged_items if to_text(item.get("symbol"))]
            )
            summary = build_summary(environment, market_date, scan_scope, merged_items, expected_symbols)
            return jsonify({"ok": True, "summary": summary, "source": "ibkr-api"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_summary"] = custom_ibkr_data_quality_summary

    @app.route("/api/custom/ibkr/data_quality/truth_summary", methods=["GET"])
    def custom_ibkr_data_quality_truth_summary() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        try:
            truth_items = load_truth_items(pb, environment, market_date=market_date)
            expected_symbols = load_effective_watchlist_symbols(pb, environment)
            summary = build_truth_summary(environment, market_date, truth_items, expected_symbols)
            return jsonify({"ok": True, "summary": summary, "source": "ibkr-api"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_truth_summary"] = custom_ibkr_data_quality_truth_summary

    @app.route("/api/custom/ibkr/data_quality/list", methods=["GET"])
    def custom_ibkr_data_quality_list() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        scan_scope = to_text(request.args.get("scan_scope"))
        status = to_text(request.args.get("status")).lower()
        symbol = to_text(request.args.get("symbol")).upper()
        needs_repair = None
        if request.args.get("needs_repair") not in {None, ""}:
            needs_repair = parse_boolean(request.args.get("needs_repair"), False)
        page = parse_integer(request.args.get("page"), 1, 1)
        per_page = parse_integer(request.args.get("per_page"), 50, 1, 200)
        try:
            integrity_items = load_integrity_items(pb, environment, market_date=market_date, scan_scope=scan_scope, symbol=symbol)
            truth_items = load_truth_items(pb, environment, market_date=market_date, symbol=symbol)
            items = merge_items(integrity_items, truth_items)
            if status:
                items = [item for item in items if to_text(item.get("status")).lower() == status]
            if needs_repair is not None:
                items = [item for item in items if bool(item.get("needs_repair")) is bool(needs_repair)]
            paged = paginate(items, page=page, per_page=per_page)
            return jsonify(
                {
                    "ok": True,
                    "environment": environment,
                    "market_date": market_date,
                    "page": page,
                    "per_page": per_page,
                    "total": paged["total"],
                    "items": paged["items"],
                    "source": "ibkr-api",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_list"] = custom_ibkr_data_quality_list

    @app.route("/api/custom/ibkr/data_quality/daily_summary", methods=["GET"])
    def custom_ibkr_data_quality_daily_summary() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        date_from = to_text(request.args.get("date_from"))
        date_to = to_text(request.args.get("date_to"))
        symbol = to_text(request.args.get("symbol")).upper()
        session_mode = to_text(request.args.get("session_mode")).lower()
        status = to_text(request.args.get("status")).lower()
        needs_repair = None
        if request.args.get("needs_repair") not in {None, ""}:
            needs_repair = parse_boolean(request.args.get("needs_repair"), False)
        try:
            items = load_daily_coverage_items(
                pb,
                environment,
                market_date=market_date,
                date_from=date_from,
                date_to=date_to,
                symbol=symbol,
                session_mode=session_mode,
                status=status,
                needs_repair=needs_repair,
            )
            summary = build_daily_coverage_summary(environment, items)
            return jsonify({"ok": True, "summary": summary, "source": "ibkr-api"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_daily_summary"] = custom_ibkr_data_quality_daily_summary

    @app.route("/api/custom/ibkr/data_quality/daily_list", methods=["GET"])
    def custom_ibkr_data_quality_daily_list() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        date_from = to_text(request.args.get("date_from"))
        date_to = to_text(request.args.get("date_to"))
        status = to_text(request.args.get("status")).lower()
        symbol = to_text(request.args.get("symbol")).upper()
        session_mode = to_text(request.args.get("session_mode")).lower()
        needs_repair = None
        if request.args.get("needs_repair") not in {None, ""}:
            needs_repair = parse_boolean(request.args.get("needs_repair"), False)
        page = parse_integer(request.args.get("page"), 1, 1)
        per_page = parse_integer(request.args.get("per_page"), 50, 1, 500)
        try:
            items = load_daily_coverage_items(
                pb,
                environment,
                market_date=market_date,
                date_from=date_from,
                date_to=date_to,
                symbol=symbol,
                session_mode=session_mode,
                status=status,
                needs_repair=needs_repair,
            )
            paged = paginate(items, page=page, per_page=per_page)
            return jsonify(
                {
                    "ok": True,
                    "environment": environment,
                    "market_date": market_date,
                    "date_from": date_from,
                    "date_to": date_to,
                    "page": page,
                    "per_page": per_page,
                    "total": paged["total"],
                    "items": paged["items"],
                    "source": "ibkr-api",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_daily_list"] = custom_ibkr_data_quality_daily_list

    @app.route("/api/custom/ibkr/data_quality/truth_list", methods=["GET"])
    def custom_ibkr_data_quality_truth_list() -> Response:
        environment = _request_data_environment(request.args)
        market_date = to_text(request.args.get("market_date"))
        status = to_text(request.args.get("status")).lower()
        symbol = to_text(request.args.get("symbol")).upper()
        page = parse_integer(request.args.get("page"), 1, 1)
        per_page = parse_integer(request.args.get("per_page"), 50, 1, 200)
        try:
            items = load_truth_items(pb, environment, market_date=market_date, symbol=symbol)
            if status:
                items = [item for item in items if to_text(item.get("status")).lower() == status]
            paged = paginate(items, page=page, per_page=per_page)
            return jsonify(
                {
                    "ok": True,
                    "environment": environment,
                    "market_date": market_date,
                    "page": page,
                    "per_page": per_page,
                    "total": paged["total"],
                    "items": paged["items"],
                    "source": "ibkr-api",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "source": "ibkr-api"}), 500

    exports["custom_ibkr_data_quality_truth_list"] = custom_ibkr_data_quality_truth_list

    return exports


__all__ = ["register_data_quality_routes"]
