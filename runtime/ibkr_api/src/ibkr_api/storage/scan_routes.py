from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_market_data_mode
from ibkr_api.storage.helpers import batch_upsert_records, prepare_scan_row


StorageDeps = dict[str, Any]


def register_storage_scan_routes(app, *, deps: StorageDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    @app.route("/api/custom/ibkr/scan", methods=["POST"])
    def custom_ibkr_scan() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = request_market_data_mode(payload)
        row, error = prepare_scan_row(payload, environment)
        if row is None:
            return jsonify({"ok": False, "error": error or "missing_symbol_or_date"}), 400
        result = batch_upsert_records(
            pb,
            "ibkr_targets",
            [row],
            ["symbol", "date", "environment"],
            timeout=30,
        )
        action = "created" if result.get("created") else "updated"
        return jsonify({"ok": True, "symbol": row["symbol"], "date": row["date"], "action": action})

    exports["custom_ibkr_scan"] = custom_ibkr_scan
    return exports


__all__ = ["register_storage_scan_routes"]
