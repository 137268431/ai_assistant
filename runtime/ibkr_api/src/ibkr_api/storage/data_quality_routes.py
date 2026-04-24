from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.storage.helpers import (
    batch_upsert_records,
    merge_bar_integrity_row,
    prepare_bar_integrity_row,
    prepare_bar_truth_row,
)


StorageDeps = dict[str, Any]


def register_storage_data_quality_routes(app, *, deps: StorageDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]

    @app.route("/api/custom/ibkr/data_quality/upsert", methods=["POST"])
    def custom_ibkr_data_quality_upsert() -> Response:
        payload = request.get_json(silent=True) or {}
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "Empty data quality items array"}), 400
        default_environment = normalize_environment(payload.get("environment"), "live")
        prepared_rows: list[dict[str, Any]] = []
        errors = 0
        for item in items:
            environment = normalize_environment((item or {}).get("environment"), default_environment)
            row, error = prepare_bar_integrity_row(item if isinstance(item, dict) else {}, environment)
            if row is None:
                errors += 1
                continue
            prepared_rows.append(row)
        if not prepared_rows:
            return jsonify({"ok": False, "error": "No valid data quality rows", "errors": errors}), 400
        result = batch_upsert_records(
            pb,
            "ibkr_bar_integrity",
            prepared_rows,
            ["environment", "market_date", "symbol", "interval"],
            timeout=30,
            update_transform=merge_bar_integrity_row,
        )
        return jsonify(
            {
                "ok": errors == 0,
                "received": len(items),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "skipped": int(result.get("skipped", 0)),
                "errors": errors,
            }
        )

    exports["custom_ibkr_data_quality_upsert"] = custom_ibkr_data_quality_upsert

    @app.route("/api/custom/ibkr/data_quality/truth_upsert", methods=["POST"])
    def custom_ibkr_data_quality_truth_upsert() -> Response:
        payload = request.get_json(silent=True) or {}
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "Empty truth audit items array"}), 400
        default_environment = normalize_environment(payload.get("environment"), "live")
        prepared_rows: list[dict[str, Any]] = []
        errors = 0
        for item in items:
            environment = normalize_environment((item or {}).get("environment"), default_environment)
            row, error = prepare_bar_truth_row(item if isinstance(item, dict) else {}, environment)
            if row is None:
                errors += 1
                continue
            prepared_rows.append(row)
        if not prepared_rows:
            return jsonify({"ok": False, "error": "No valid truth audit rows", "errors": errors}), 400
        result = batch_upsert_records(
            pb,
            "ibkr_bar_truth_audit",
            prepared_rows,
            ["environment", "market_date", "symbol", "interval"],
            timeout=30,
        )
        return jsonify(
            {
                "ok": errors == 0,
                "received": len(items),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "skipped": int(result.get("skipped", 0)),
                "errors": errors,
            }
        )

    exports["custom_ibkr_data_quality_truth_upsert"] = custom_ibkr_data_quality_truth_upsert
    return exports


__all__ = ["register_storage_data_quality_routes"]
