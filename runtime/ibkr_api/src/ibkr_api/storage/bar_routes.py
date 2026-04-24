from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.storage.helpers import prepare_bar_row


StorageDeps = dict[str, Any]


def register_storage_bar_routes(app, *, deps: StorageDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    config_value = deps["config_value"]

    @app.route("/api/custom/ibkr/bars", methods=["POST"])
    def custom_ibkr_bars() -> Response:
        payload = request.get_json(silent=True) or {}
        bars = payload.get("bars")
        if not isinstance(bars, list) or not bars:
            return jsonify({"ok": False, "error": "Empty bars array"}), 400

        default_environment = normalize_environment(payload.get("environment"), "live")
        enabled_value = config_value("ibkr_bar_publish_enabled", "true", default_environment)
        if not parse_boolean(enabled_value, True):
            return jsonify(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "ibkr_bar_publish_enabled=false",
                    "config_value": str(enabled_value or ""),
                }
            )

        prepared_rows: list[dict[str, Any]] = []
        errors = 0
        for item in bars:
            row, error = prepare_bar_row(item if isinstance(item, dict) else {}, default_environment)
            if row is None:
                errors += 1
                continue
            prepared_rows.append(row)

        if not prepared_rows:
            return jsonify({"ok": False, "error": "No valid bars in request", "errors": errors}), 400

        result = pb.upsert_bars(prepared_rows)
        response_payload = {
            "ok": bool(result.get("ok", True)) and errors == 0,
            "received": len(bars),
            "accepted": len(prepared_rows),
            "errors": errors,
            **result,
        }
        return jsonify(response_payload)

    exports["custom_ibkr_bars"] = custom_ibkr_bars
    return exports


__all__ = ["register_storage_bar_routes"]
