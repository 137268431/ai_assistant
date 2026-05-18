from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_market_data_mode


def register_tradingview_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    upsert_tv_indicator = deps["upsert_tv_indicator"]
    upsert_tv_signal = deps["upsert_tv_signal"]
    config_value = deps["config_value"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    exports: dict[str, Any] = {}

    @app.route("/webhook/tv", methods=["POST"])
    def webhook_tv() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = request_market_data_mode(payload)
        enabled_value = config_value("tv_webhook_ingest_enabled", "TRUE", environment)
        if not parse_boolean(enabled_value, True):
            return jsonify(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "tv_webhook_ingest_enabled=false",
                    "config_value": str(enabled_value or ""),
                }
            )

        data_type = str(payload.get("type") or "signal").strip().lower() or "signal"
        if data_type == "indicator":
            return upsert_tv_indicator(payload)
        return upsert_tv_signal(payload)
    exports["webhook_tv"] = webhook_tv

    return exports


__all__ = ["register_tradingview_routes"]
