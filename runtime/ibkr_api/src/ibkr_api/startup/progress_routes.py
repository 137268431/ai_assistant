from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request


StartupDeps = dict[str, Any]


def register_startup_progress_routes(app, *, deps: StartupDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    time_strings = deps["time_strings"]
    get_state_payload = deps["get_state_payload"]
    normalize_startup_state = deps["normalize_startup_state"]
    build_startup_cycle_id = deps["build_startup_cycle_id"]
    build_startup_label = deps["build_startup_label"]
    startup_chat_id = deps["startup_chat_id"]
    default_startup_steps = deps["default_startup_steps"]
    normalize_startup_fields = deps["normalize_startup_fields"]
    merge_startup_steps = deps["merge_startup_steps"]
    deliver_startup_progress_card = deps["deliver_startup_progress_card"]
    resolve_startup_step_label = deps["resolve_startup_step_label"]
    write_system_event_record = deps["write_system_event_record"]
    ibkr_startup_state_key = deps["ibkr_startup_state_key"]
    ibkr_startup_state_date = deps["ibkr_startup_state_date"]

    @app.route("/api/custom/ibkr/startup/progress", methods=["POST"])
    def custom_ibkr_startup_progress() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = normalize_environment(payload.get("environment"), "live")
        times = time_strings()
        action = str(payload.get("action") or "update").strip().lower() or "update"
        create_if_missing = bool(payload.get("create_if_missing") is True)
        current_payload = get_state_payload(ibkr_startup_state_key, environment, date=ibkr_startup_state_date)
        current = normalize_startup_state(current_payload.get("data"), environment)
        next_state = normalize_startup_state(current, environment)

        should_create_cycle = action == "begin" or (not bool(current.get("active")) and create_if_missing)
        if should_create_cycle:
            next_seq = max(0, int(current.get("startup_seq") or 0)) + 1
            next_state.update(
                {
                    "cycle_id": build_startup_cycle_id(environment),
                    "startup_seq": next_seq,
                    "startup_label": build_startup_label(environment, next_seq, times["us"]),
                    "active": True,
                    "status": "active",
                    "started_at": times["us"],
                    "finished_at": "",
                    "startup_chat_id": startup_chat_id(environment),
                    "message_id": "",
                    "last_delivery_mode": "",
                    "last_delivery_at": "",
                    "last_delivery_error": "",
                    "fields": {},
                    "steps": default_startup_steps(),
                }
            )
        elif not next_state.get("cycle_id"):
            next_seq = max(1, int(current.get("startup_seq") or 1))
            next_state["cycle_id"] = build_startup_cycle_id(environment)
            next_state["startup_seq"] = next_seq
            next_state["startup_label"] = current.get("startup_label") or build_startup_label(
                environment,
                next_seq,
                current.get("started_at") or times["us"],
            )

        next_state["startup_chat_id"] = startup_chat_id(environment)
        if "title" in payload:
            next_state["title"] = str(payload.get("title") or next_state.get("title") or "IBKR Runtime 启动中")
        if "summary" in payload:
            next_state["summary"] = str(payload.get("summary") or "")
        if "current_step" in payload:
            next_state["current_step"] = str(payload.get("current_step") or "")
        if "current_blocker" in payload:
            next_state["current_blocker"] = str(payload.get("current_blocker") or "")
        if "operator_action" in payload:
            next_state["operator_action"] = str(payload.get("operator_action") or "")
        if "reason" in payload:
            next_state["reason"] = str(payload.get("reason") or "")
        if "source" in payload:
            next_state["source"] = str(payload.get("source") or "")
        if "runtime_phase" in payload:
            next_state["runtime_phase"] = str(payload.get("runtime_phase") or "")
        if "runtime_url" in payload:
            next_state["runtime_url"] = str(payload.get("runtime_url") or "")
        if "trigger_login" in payload:
            next_state["trigger_login"] = bool(payload.get("trigger_login") is True)

        if isinstance(payload.get("fields"), dict):
            next_state["fields"] = {
                **normalize_startup_fields(next_state.get("fields")),
                **normalize_startup_fields(payload.get("fields")),
            }
        next_state["steps"] = merge_startup_steps(
            next_state.get("steps"),
            payload.get("steps"),
            bool(next_state.get("trigger_login")),
        )

        if action in {"begin", "update", "pause"}:
            next_state["active"] = True
            next_state["status"] = str(payload.get("status") or "active").strip().lower() or "active"
            if not next_state.get("started_at"):
                next_state["started_at"] = times["us"]
            next_state["finished_at"] = ""
        elif action == "complete":
            next_state["active"] = False
            next_state["status"] = "completed"
            next_state["finished_at"] = times["us"]
        elif action == "fail":
            next_state["active"] = False
            next_state["status"] = "failed"
            next_state["finished_at"] = times["us"]
        elif action in {"abort", "clear"}:
            next_state["active"] = False
            next_state["status"] = "aborted"
            next_state["finished_at"] = times["us"]

        if action == "complete":
            next_state["steps"] = merge_startup_steps(
                next_state.get("steps"),
                {
                    "runtime_resume": {"status": "done", "detail": "认证恢复后 Runtime 已回到可运行状态。"},
                    "health_check": {"status": "done", "detail": "启动后健康检查已通过。"},
                },
                bool(next_state.get("trigger_login")),
            )

        next_state["last_update_at"] = times["us"]

        try:
            saved_record = pb.upsert_state(ibkr_startup_state_key, environment, next_state, date=ibkr_startup_state_date)
        except Exception as exc:
            return jsonify({"ok": False, "environment": environment, "error": str(exc), "source": "ibkr-api"}), 500

        saved_state = normalize_startup_state(
            (saved_record or {}).get("data") if isinstance(saved_record, dict) else next_state,
            environment,
        )
        delivery = deliver_startup_progress_card(saved_state, environment)
        if delivery.get("message_id"):
            saved_state["message_id"] = str(delivery.get("message_id") or "")
        saved_state["last_delivery_mode"] = "update" if current.get("message_id") else "send"
        saved_state["last_delivery_at"] = times["us"]
        saved_state["last_delivery_error"] = "" if delivery.get("success") else str(delivery.get("error") or "")
        try:
            pb.upsert_state(ibkr_startup_state_key, environment, saved_state, date=ibkr_startup_state_date)
        except Exception:
            pass

        if bool(payload.get("record_event") is True):
            event_type = str(payload.get("event_type") or "status_change").strip() or "status_change"
            level = str(payload.get("level") or "info").strip().lower() or "info"
            event_source = str(payload.get("event_source") or payload.get("source") or "ibkr_compute").strip() or "ibkr_compute"
            event_detail = payload.get("event_detail") if isinstance(payload.get("event_detail"), dict) else {
                "cycle_id": saved_state.get("cycle_id") or "",
                "current_step": resolve_startup_step_label(saved_state),
                "current_blocker": saved_state.get("current_blocker") or saved_state.get("summary") or "",
                "operator_action": saved_state.get("operator_action") or "",
            }
            write_system_event_record(
                event_type,
                level,
                event_source,
                str(payload.get("event_title") or saved_state.get("title") or "IBKR Runtime 启动中"),
                event_detail,
                environment,
                bool(delivery.get("success")),
            )

        return jsonify(
            {
                "ok": True,
                "environment": environment,
                "date": ibkr_startup_state_date,
                "cycle_id": saved_state.get("cycle_id") or "",
                "startup_label": saved_state.get("startup_label") or "",
                "message_id": saved_state.get("message_id") or "",
                "state": saved_state,
                "delivery_ok": bool(delivery.get("success")),
                "delivery_error": str(delivery.get("error") or ""),
                "source": "ibkr-api",
            }
        )

    exports["custom_ibkr_startup_progress"] = custom_ibkr_startup_progress
    return exports


__all__ = ["register_startup_progress_routes"]
