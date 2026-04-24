from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.storage.helpers import batch_upsert_records, prepare_indicator_row


StorageDeps = dict[str, Any]


def register_storage_indicator_routes(app, *, deps: StorageDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]

    @app.route("/api/custom/ibkr/indicator", methods=["POST"])
    def custom_ibkr_indicator() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = normalize_environment(payload.get("environment"), "live")
        row, error = prepare_indicator_row(payload, environment)
        if row is None:
            return jsonify({"ok": False, "error": error or "invalid_indicator_payload"}), 400
        result = batch_upsert_records(
            pb,
            "ibkr_indicators",
            [row],
            ["symbol", "interval", "bar_time_ms", "environment"],
            timeout=30,
        )
        action = "created" if result.get("created") else "updated"
        return jsonify(
            {
                "ok": True,
                "symbol": row["symbol"],
                "interval": row["interval"],
                "collection": "ibkr_indicators",
                "action": action,
            }
        )

    exports["custom_ibkr_indicator"] = custom_ibkr_indicator

    @app.route("/api/custom/ibkr/indicators", methods=["POST"])
    def custom_ibkr_indicators() -> Response:
        payload = request.get_json(silent=True) or {}
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "Empty indicators array"}), 400
        default_environment = normalize_environment(payload.get("environment"), "live")
        prepared_rows: list[dict[str, Any]] = []
        errors = 0
        for item in items:
            environment = normalize_environment((item or {}).get("environment"), default_environment)
            row, error = prepare_indicator_row(item if isinstance(item, dict) else {}, environment)
            if row is None:
                errors += 1
                continue
            prepared_rows.append(row)
        if not prepared_rows:
            return jsonify({"ok": False, "error": "No valid indicators in request", "errors": errors}), 400
        result = batch_upsert_records(
            pb,
            "ibkr_indicators",
            prepared_rows,
            ["symbol", "interval", "bar_time_ms", "environment"],
            timeout=30,
        )
        return jsonify(
            {
                "ok": errors == 0,
                "received": len(items),
                "success": int(result.get("created", 0)) + int(result.get("updated", 0)),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "skipped": int(result.get("skipped", 0)),
                "errors": errors,
                "collection": "ibkr_indicators",
            }
        )

    exports["custom_ibkr_indicators"] = custom_ibkr_indicators
    return exports


__all__ = ["register_storage_indicator_routes"]
