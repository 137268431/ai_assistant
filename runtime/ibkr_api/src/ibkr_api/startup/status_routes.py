from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request


StartupDeps = dict[str, Any]


def register_startup_status_routes(app, *, deps: StartupDeps, exports: dict[str, Any]) -> dict[str, Any]:
    normalize_environment = deps["normalize_environment"]
    get_state_payload = deps["get_state_payload"]
    ibkr_startup_state_key = deps["ibkr_startup_state_key"]
    ibkr_startup_state_date = deps["ibkr_startup_state_date"]
    as_dict = deps["as_dict"]

    @app.route("/api/custom/ibkr/startup/status", methods=["GET"])
    def custom_ibkr_startup_status() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        payload = get_state_payload(ibkr_startup_state_key, environment, date=ibkr_startup_state_date)
        raw_state = as_dict(payload.get("data"))
        state = {
            **raw_state,
            "active": bool(raw_state.get("active")),
            "status": str(raw_state.get("status") or "idle").strip().lower() or "idle",
            "startup_label": str(raw_state.get("startup_label") or ""),
            "current_step": str(raw_state.get("current_step") or ""),
            "current_blocker": str(raw_state.get("current_blocker") or ""),
            "operator_action": str(raw_state.get("operator_action") or ""),
            "summary": str(raw_state.get("summary") or ""),
            "reason": str(raw_state.get("reason") or ""),
            "runtime_phase": str(raw_state.get("runtime_phase") or ""),
            "steps": as_dict(raw_state.get("steps")),
            "fields": as_dict(raw_state.get("fields")),
        }
        return jsonify(
            {
                "ok": True,
                "environment": payload.get("environment") or environment,
                "date": payload.get("date") or ibkr_startup_state_date,
                "startup_label": state.get("startup_label") or "",
                "state": state,
                "source": "ibkr-api",
            }
        )

    exports["custom_ibkr_startup_status"] = custom_ibkr_startup_status
    return exports


__all__ = ["register_startup_status_routes"]
