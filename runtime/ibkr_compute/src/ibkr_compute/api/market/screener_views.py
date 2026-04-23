from __future__ import annotations

import traceback
from flask import Response, jsonify

from ibkr_compute.api.market.screener.payload import build_screener_payload
from ibkr_compute.api.route_request import get_query_arg_csv, get_query_arg_int, get_query_arg_text
from ibkr_compute.api.route_runtime import get_app_module


def build_screener_response():
    app_mod = get_app_module()
    environment = get_query_arg_text("environment", "live", lower=True)
    market_date = get_query_arg_text("market_date", app_mod.current_market_date())
    symbols = app_mod.normalize_symbols(get_query_arg_csv("symbols"))
    limit = get_query_arg_int("limit", 0, minimum=0)

    if environment not in app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS:
        return jsonify({"ok": False, "error": "invalid_environment", "environment": environment}), 400
    try:
        date.fromisoformat(market_date)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_market_date", "market_date": market_date}), 400

    try:
        payload = build_screener_payload(
            environment=environment,
            market_date=market_date,
            symbols=symbols,
            limit=limit,
        )
        return jsonify(payload)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc), "environment": environment, "market_date": market_date}), 500
