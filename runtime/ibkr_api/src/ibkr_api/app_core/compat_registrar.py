from __future__ import annotations

from typing import Any

from ibkr_api.compat.routes import register_compat_routes


def register_compat_proxy_routes(app, deps: dict[str, Any]) -> dict[str, Any]:
    return register_compat_routes(
        app,
        deps={
            "pb_base_url": deps["pb_base_url"],
            "compute_base_url": deps["compute_base_url"],
            "runtime_base_url": deps["runtime_base_url"],
            "scheduler_base_url": deps["scheduler_base_url"],
            "direct_proxy_map": deps["direct_proxy_map"],
            "action_proxy_map": deps["action_proxy_map"],
            "delegated_pocketbase_custom_routes": deps["delegated_pocketbase_custom_routes"],
            "delegated_pocketbase_webhook_routes": deps["delegated_pocketbase_webhook_routes"],
            "build_service_topology": deps["build_service_topology"],
            "config": deps["config"],
            "normalize_environment": deps["normalize_environment"],
            "scheduler_status": deps["scheduler_status"],
            "scheduler_job_states": deps["scheduler_job_states"],
            "build_cron_payload": deps["build_cron_payload"],
            "build_scheduler_summary": deps["build_scheduler_summary"],
            "augment_scheduler_summary": deps["augment_scheduler_summary"],
            "forward_request": deps["forward_request"],
            "proxy_custom_to_pb": deps["proxy_custom_to_pb"],
            "proxy_webhook_to_pb": deps["proxy_webhook_to_pb"],
        },
    )
