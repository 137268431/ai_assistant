from __future__ import annotations

from typing import Any

from flask import Response, request


def register_tradingview_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    upsert_tv_indicator = deps["upsert_tv_indicator"]
    upsert_tv_signal = deps["upsert_tv_signal"]
    exports: dict[str, Any] = {}

    @app.route("/webhook/tv", methods=["POST"])
    def webhook_tv() -> Response:
        payload = request.get_json(silent=True) or {}
        data_type = str(payload.get("type") or "signal").strip().lower() or "signal"
        if data_type == "indicator":
            return upsert_tv_indicator(payload)
        return upsert_tv_signal(payload)
    exports["webhook_tv"] = webhook_tv

    return exports


__all__ = ["register_tradingview_routes"]
