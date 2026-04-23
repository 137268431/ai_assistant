from __future__ import annotations

import time
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.storage.helpers import (
    batch_upsert_records,
    build_ping_signal_row,
    merge_bar_integrity_row,
    prepare_bar_integrity_row,
    prepare_bar_row,
    prepare_bar_truth_row,
    prepare_indicator_row,
    prepare_scan_row,
)


def register_storage_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    config_value = deps["config_value"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/ping_write", methods=["GET"])
    def custom_ibkr_ping_write() -> Response:
        now_ms = int(time.time() * 1000)
        signal_id = f"PING_{now_ms}"
        row = build_ping_signal_row(signal_id=signal_id, now_ms=now_ms)
        existing = pb.get_first_record(
            "ibkr_signals",
            filter=f'signal_id = "{signal_id}" && environment = "live"',
        )
        if existing and existing.get("id"):
            record = pb.update_record("ibkr_signals", str(existing["id"]), row)
        else:
            record = pb.create_record("ibkr_signals", row)
        return jsonify({"ok": True, "signal_id": signal_id, "id": str(record.get("id") or "")})

    exports["custom_ibkr_ping_write"] = custom_ibkr_ping_write

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

    @app.route("/api/custom/ibkr/scan", methods=["POST"])
    def custom_ibkr_scan() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = normalize_environment(payload.get("environment"), "live")
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


__all__ = ["register_storage_routes"]
