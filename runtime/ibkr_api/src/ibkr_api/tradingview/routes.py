from __future__ import annotations

import time
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_market_data_mode


WEBHOOK_INGEST_CONFIG_KEY = "tv_webhook_ingest_enabled"


def register_tradingview_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    process_tv_primary_event = deps["process_tv_primary_event"]
    config_value = deps["config_value"]
    parse_boolean = deps["parse_boolean"]
    exports: dict[str, Any] = {}

    @app.route("/webhook/tv", methods=["POST"])
    def webhook_tv() -> Response:
        api_received_at_ms = int(time.time() * 1000)
        payload = request.get_json(silent=True) or {}
        environment = request_market_data_mode(payload)
        enabled_value = config_value(WEBHOOK_INGEST_CONFIG_KEY, "TRUE", environment)
        if not parse_boolean(enabled_value, True):
            return jsonify(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": f"{WEBHOOK_INGEST_CONFIG_KEY}=false",
                    "config_value": str(enabled_value or ""),
                }
            )

        result, status_code = process_tv_primary_event(payload, api_received_at_ms=api_received_at_ms)
        response = jsonify(result)
        return response if status_code == 200 else (response, status_code)
    exports["webhook_tv"] = webhook_tv

    return exports


__all__ = [
    "WEBHOOK_INGEST_CONFIG_KEY",
    "register_tradingview_routes",
]
