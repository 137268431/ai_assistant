from __future__ import annotations

import time
from typing import Any

from flask import Response, jsonify

from ibkr_api.storage.helpers import build_ping_signal_row


StorageDeps = dict[str, Any]


def register_storage_ping_routes(app, *, deps: StorageDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]

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
    return exports


__all__ = ["register_storage_ping_routes"]
