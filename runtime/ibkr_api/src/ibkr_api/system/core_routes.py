from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.system.health_report import build_health_report_response
from ibkr_api.system.notify import build_notify_response


SystemDeps = dict[str, Any]



def register_system_core_routes(app, *, deps: SystemDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    config = deps["config"]
    normalize_environment = deps["normalize_environment"]
    emit_system_event = deps["emit_system_event"]
    label_title_with_environment = deps["label_title_with_environment"]
    add_environment_to_detail = deps["add_environment_to_detail"]
    write_system_event_record = deps["write_system_event_record"]
    deliver_system_event_notification = deps["deliver_system_event_notification"]

    @app.route("/api/custom/ibkr/health-report", methods=["POST"])
    def custom_ibkr_health_report() -> Response:
        payload, status_code = build_health_report_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            label_title_with_environment=label_title_with_environment,
            add_environment_to_detail=add_environment_to_detail,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_health_report"] = custom_ibkr_health_report

    @app.route("/api/custom/ibkr/notify", methods=["POST"])
    def custom_ibkr_notify() -> Response:
        payload, status_code = build_notify_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_notify"] = custom_ibkr_notify

    @app.route("/api/custom/system/event", methods=["POST"])
    def custom_system_event() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = normalize_environment(payload.get("environment"), "live")
        raw_title = str(payload.get("title") or "").strip()
        raw_detail = payload.get("detail") if payload.get("detail") is not None else {}
        event_type = str(payload.get("event_type") or "status_change").strip() or "status_change"
        level = str(payload.get("level") or "info").strip().lower() or "info"
        source = str(payload.get("source") or "ibkr-api").strip() or "ibkr-api"
        message_id = str(payload.get("message_id") or "").strip()
        if not raw_title:
            return jsonify({"ok": False, "environment": environment, "error": "title required", "source": "ibkr-api"}), 400
        config.refresh()
        delivery = deliver_system_event_notification(
            event_type,
            level,
            source,
            raw_title,
            raw_detail,
            environment,
            message_id=message_id,
        )
        notified = bool(delivery.get("success")) and not bool(delivery.get("suppressed"))
        persisted = bool(write_system_event_record(event_type, level, source, raw_title, raw_detail, environment, notified))
        return jsonify(
            {
                "ok": True,
                "environment": environment,
                "notified": notified,
                "persisted": persisted,
                "message_id": str(delivery.get("message_id") or message_id),
                "updated": bool(delivery.get("updated")),
                "skipped": bool(delivery.get("skipped")),
                "suppressed": bool(delivery.get("suppressed")),
                "error": str(delivery.get("error") or ""),
                "source": "ibkr-api",
            }
        )

    exports["custom_system_event"] = custom_system_event
    return exports


__all__ = ["register_system_core_routes"]
