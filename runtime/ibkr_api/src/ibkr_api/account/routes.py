from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.account.snapshot import build_account_snapshot_response


def register_account_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/account_snapshot", methods=["GET"])
    def custom_ibkr_account_snapshot() -> Response:
        payload, status_code = build_account_snapshot_response(
            pb,
            payload=request.args.to_dict(flat=True),
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_account_snapshot"] = custom_ibkr_account_snapshot
    return exports


__all__ = ["register_account_routes"]
