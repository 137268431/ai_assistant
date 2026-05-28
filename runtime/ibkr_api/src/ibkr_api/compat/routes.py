from __future__ import annotations

import os
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.system.service_state import canonicalize_topology


NATIVE_CUSTOM_ROUTES = [
    "ibkr/bars",
    "ibkr/data_quality/daily_list",
    "ibkr/data_quality/daily_summary",
    "ibkr/data_quality/daily_upsert",
    "ibkr/data_quality/list",
    "ibkr/data_quality/summary",
    "ibkr/data_quality/truth_list",
    "ibkr/data_quality/truth_summary",
    "ibkr/data_quality/truth_upsert",
    "ibkr/data_quality/upsert",
    "ibkr/2fa/request",
    "ibkr/2fa/respond",
    "ibkr/2fa/result",
    "ibkr/2fa/status",
    "ibkr/2fa/takeover",
    "ibkr/2fa/probe",
    "ibkr/2fa/panic-reset",
    "ibkr/indicator",
    "ibkr/indicators",
    "ibkr/lifecycle-flow",
    "ibkr/emergency-stop",
    "ibkr/fundamentals",
    "ibkr/fundamentals/list",
    "ibkr/fundamentals/refresh",
    "ibkr/health-report",
    "ibkr/healthz",
    "ibkr/home-dashboard",
    "ibkr/home-market",
    "ibkr/account_snapshot",
    "ibkr/analytics/daily-signals",
    "ibkr/analytics/daily-trade-review",
    "ibkr/notify",
    "ibkr/orders/cancel_group",
    "ibkr/orders/cancel_sync",
    "ibkr/orders/close_group",
    "ibkr/orders/reconcile",
    "ibkr/orders/upsert",
    "ibkr/ping_write",
    "ibkr/recover",
    "ibkr/reauth",
    "ibkr/state/orders",
    "ibkr/state/signals",
    "ibkr/reverse/ack",
    "ibkr/reverse/calculate",
    "ibkr/reverse/dispatch",
    "ibkr/reverse/list",
    "ibkr/reverse/pending",
    "ibkr/runtime/config",
    "ibkr/scan",
    "ibkr/screener",
    "ibkr/screener/targets",
    "ibkr/signal",
    "ibkr/signals",
    "ibkr/signals/ack",
    "ibkr/signals/pending",
    "ibkr/services/action",
    "ibkr/startup/progress",
    "ibkr/startup/status",
    "ibkr/statusz",
    "ibkr/today-targets",
    "ibkr/watchlist/eligibility",
    "ibkr/targets/remove",
    "ibkr/targets/upsert",
    "ibkr/watchlist/remove",
    "ibkr/watchlist/upsert",
    "system/cronz",
    "system/event",
    "system/healthz",
    "system/jobs/2fa_hourly_check",
    "system/jobs/active_window_progress_status",
    "system/jobs/auth_edge_guard",
    "system/jobs/auth_pending_guard",
    "system/jobs/daily_report",
    "system/jobs/data_gap_guard",
    "system/jobs/fundamentals_refresh",
    "system/jobs/heartbeat",
    "system/jobs/intraday_window_admission",
    "system/jobs/market_open_reminder",
    "system/jobs/monitor_alert_guard",
    "system/jobs/order_detail_integrity",
    "system/jobs/order_expiry",
    "system/jobs/scan_summary",
    "system/jobs/signal_expiry",
    "system/jobs/status_reminder",
    "system/jobs/weekly_reauth_followup",
    "system/jobs/weekly_reauth_reminder",
    "system/monitorz",
    "system/scheduler/jobs/run",
    "system/schedulerz",
    "system/summaryz",
]

NATIVE_WEBHOOK_ROUTES = [
    "feishu/callback",
    "order/cancel",
    "order/close",
    "signal/cancel",
    "signal/confirm",
    "tv",
]


def _build_unmatched_route_response(*, route_family: str, subpath: str):
    return (
        jsonify(
            {
                "ok": False,
                "error": f"unsupported_{route_family}_route",
                "route_family": route_family,
                "subpath": subpath,
                "source": "ibkr-api",
            }
        ),
        404,
    )


def register_compat_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb_base_url = deps["pb_base_url"]
    compute_base_url = deps["compute_base_url"]
    backtest_base_url = deps["backtest_base_url"]
    runtime_base_url = deps["runtime_base_url"]
    scheduler_base_url = deps["scheduler_base_url"]
    direct_proxy_map = deps["direct_proxy_map"]
    action_proxy_map = deps["action_proxy_map"]
    delegated_pocketbase_custom_routes = deps["delegated_pocketbase_custom_routes"]
    delegated_pocketbase_webhook_routes = deps["delegated_pocketbase_webhook_routes"]
    build_service_topology = deps["build_service_topology"]
    config = deps["config"]
    normalize_environment = deps["normalize_environment"]
    scheduler_status = deps["scheduler_status"]
    scheduler_job_states = deps["scheduler_job_states"]
    build_cron_payload = deps["build_cron_payload"]
    build_scheduler_summary = deps["build_scheduler_summary"]
    augment_scheduler_summary = deps["augment_scheduler_summary"]
    forward_request = deps["forward_request"]
    proxy_custom_to_pb = deps["proxy_custom_to_pb"]
    proxy_webhook_to_pb = deps["proxy_webhook_to_pb"]
    exports: dict[str, Any] = {}

    def scheduler_status_lite(environment: str) -> dict[str, Any]:
        try:
            return scheduler_status(environment, lite=True)
        except TypeError as exc:
            if "lite" not in str(exc):
                raise
            return scheduler_status(environment)

    @app.route("/health", methods=["GET"])
    def health() -> Response:
        service_topology, service_monitor = canonicalize_topology("live", build_service_topology())
        return jsonify(
            {
                "ok": True,
                "status": "running",
                "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
                "service_topology": service_topology,
                "service_monitor": service_monitor,
                "upstreams": {
                    "pocketbase": pb_base_url,
                    "compute": compute_base_url,
                    "backtest": backtest_base_url,
                    "runtime": runtime_base_url,
                    "scheduler": scheduler_base_url,
                },
                "scheduler_job_count": None,
                "scheduler_job_count_source": "omitted_fast_health",
            }
        )
    exports["health"] = health

    @app.route("/status", methods=["GET"])
    def status() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        lite = str(request.args.get("lite") or "").strip().lower() in {"1", "true", "yes", "on"}
        if lite:
            service_topology, service_monitor = canonicalize_topology(environment, build_service_topology())
            return jsonify(
                {
                    "ok": True,
                    "status": "running",
                    "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
                    "environment": environment,
                    "service_topology": service_topology,
                    "service_monitor": service_monitor,
                    "lite": True,
                }
            )
        scheduler_payload = scheduler_status_lite(environment)
        scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
        config.refresh()
        scheduler_items = build_cron_payload(config, environment, scheduler_jobs)
        service_topology, service_monitor = canonicalize_topology(environment, build_service_topology())
        return jsonify(
            {
                "ok": True,
                "status": "running",
                "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
                "service_topology": service_topology,
                "service_monitor": service_monitor,
                "compatibility": {
                    "direct_proxy_routes": sorted({path for (_, path) in direct_proxy_map}),
                    "proxy_action_routes": sorted(action_proxy_map),
                    "native_custom_routes": NATIVE_CUSTOM_ROUTES,
                    "native_webhook_routes": NATIVE_WEBHOOK_ROUTES,
                    "delegated_pocketbase_custom_routes": delegated_pocketbase_custom_routes,
                    "delegated_pocketbase_webhook_routes": delegated_pocketbase_webhook_routes,
                    "pocketbase_proxy_routes": {
                        "custom": "/api/custom/*",
                        "webhook": "/webhook/*",
                    },
                    "fallback_to_pocketbase_custom": False,
                    "fallback_to_pocketbase_webhooks": False,
                    "unmatched_custom_route_behavior": "404_from_ibkr_api",
                    "unmatched_webhook_route_behavior": "404_from_ibkr_api",
                },
                "scheduler_jobs": scheduler_jobs,
                "scheduler": augment_scheduler_summary(build_scheduler_summary(environment, scheduler_payload), scheduler_items),
            }
        )
    exports["status"] = status

    @app.route("/api/collections/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    def collections_proxy(subpath: str) -> Response:
        return forward_request(pb_base_url, f"/api/collections/{subpath}")
    exports["collections_proxy"] = collections_proxy

    @app.route("/api/custom/ibkr/proxy", methods=["POST"])
    def custom_ibkr_proxy() -> Response:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        target = action_proxy_map.get(action)
        if not target:
            return proxy_custom_to_pb("ibkr/proxy")

        base_url, target_path = target
        proxy_body = {key: value for key, value in payload.items() if key != "action"}
        if action == "recompute":
            proxy_body = None
        return forward_request(base_url, target_path, json_body=proxy_body)
    exports["custom_ibkr_proxy"] = custom_ibkr_proxy

    @app.route("/api/custom/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    def custom_proxy(subpath: str) -> Response:
        direct_target = direct_proxy_map.get((request.method.upper(), subpath))
        if direct_target:
            base_url, target_path = direct_target
            return forward_request(base_url, target_path)
        return _build_unmatched_route_response(route_family="custom", subpath=subpath)
    exports["custom_proxy"] = custom_proxy

    @app.route("/webhook/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    def webhook_proxy(subpath: str) -> Response:
        return _build_unmatched_route_response(route_family="webhook", subpath=subpath)
    exports["webhook_proxy"] = webhook_proxy

    return exports


__all__ = [
    "NATIVE_CUSTOM_ROUTES",
    "NATIVE_WEBHOOK_ROUTES",
    "register_compat_routes",
]
