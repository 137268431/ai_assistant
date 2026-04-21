from __future__ import annotations

import os

from ibkr_compute.api.runtime_status_client import (
    get_remote_runtime_status,
    get_runtime_internal_url as get_remote_runtime_internal_url,
    is_runtime_status_payload,
)
from ibkr_compute.api.shared.service_status import get_service_status_snapshot

DEFAULT_COMPUTE_INTERNAL_URL = "http://127.0.0.1:5100"


def _normalize_base_url(value: str | None, default: str) -> str:
    text = str(value or "").strip() or default
    return text.rstrip("/")


def get_service_profile() -> str:
    profile = str(os.environ.get("IBKR_SERVICE_PROFILE", "compute") or "").strip().lower()
    return profile or "compute"


def get_runtime_mode() -> str:
    mode = str(os.environ.get("IBKR_RUNTIME_MODE", "embedded") or "").strip().lower()
    return "remote" if mode == "remote" else "embedded"


def is_runtime_service_profile() -> bool:
    return get_service_profile() == "runtime"


def is_runtime_remote_mode() -> bool:
    return get_runtime_mode() == "remote" and not is_runtime_service_profile()


def get_compute_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("IBKR_COMPUTE_INTERNAL_URL"),
        default or DEFAULT_COMPUTE_INTERNAL_URL,
    )


def get_runtime_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        get_remote_runtime_internal_url(default),
        default or "http://127.0.0.1:5101",
    )


def _derive_runtime_service_status(runtime_mode: str, service_profile: str, service_status: dict) -> str:
    if service_profile == "runtime":
        return "running" if service_status.get("ok", True) else "degraded"
    if runtime_mode == "remote":
        if is_runtime_status_payload(service_status):
            if bool(service_status.get("ok", True)):
                return "running"
            return "degraded"
        return "expected_remote"
    return "embedded"


def _derive_gateway_status(runtime_mode: str, service_profile: str, service_status: dict) -> str:
    gateway = (service_status or {}).get("gateway") or {}
    if bool(gateway.get("running")) or bool(gateway.get("reachable")):
        return "running"
    if service_profile == "runtime" or runtime_mode == "remote":
        return "offline"
    return "embedded"


def _select_topology_status_payload(runtime_mode: str, service_profile: str, payload: dict) -> dict:
    if service_profile == "runtime" or runtime_mode != "remote":
        return payload
    if is_runtime_status_payload(payload):
        return payload
    remote_payload = get_remote_runtime_status()
    if is_runtime_status_payload(remote_payload):
        return remote_payload
    return payload


def build_service_topology(service=None, service_status: dict | None = None) -> dict:
    runtime_mode = get_runtime_mode()
    service_profile = get_service_profile()
    compute_internal_url = get_compute_internal_url()
    runtime_internal_url = get_runtime_internal_url()
    runtime_owner = "ibkr-runtime" if runtime_mode == "remote" else "ibkr-compute"

    payload = service_status if isinstance(service_status, dict) else {}
    if not payload and service is not None and hasattr(service, "status"):
        payload = get_service_status_snapshot(service)

    topology_payload = _select_topology_status_payload(runtime_mode, service_profile, payload)
    gateway = topology_payload.get("gateway") or {}
    session = topology_payload.get("session") or {}

    return {
        "service_profile": service_profile,
        "runtime_mode": runtime_mode,
        "restart_independent": runtime_mode == "remote",
        "services": {
            "pocketbase": {
                "service_name": "pocketbase",
                "kind": "storage_ui",
                "owner": "pocketbase",
                "status": "external",
                "restart_independent": True,
            },
            "ibkr-compute": {
                "service_name": "ibkr-compute",
                "kind": "control_plane",
                "owner": "ibkr-compute",
                "status": "running" if service_profile == "compute" else "peer",
                "internal_url": compute_internal_url,
                "upstream": runtime_internal_url if runtime_mode == "remote" else "",
                "runtime_mode": runtime_mode,
                "runtime_owner": runtime_owner,
                "restart_independent": runtime_mode == "remote",
            },
            "ibkr-runtime": {
                "service_name": "ibkr-runtime",
                "kind": "data_plane",
                "owner": runtime_owner,
                "status": _derive_runtime_service_status(runtime_mode, service_profile, topology_payload),
                "internal_url": runtime_internal_url,
                "upstream": runtime_internal_url if runtime_mode == "remote" else "",
                "runtime_mode": runtime_mode,
                "session_authenticated": bool(session.get("authenticated")),
                "restart_independent": runtime_mode == "remote",
            },
            "ibkr-gateway": {
                "service_name": "ibkr-gateway",
                "kind": "broker_gateway",
                "owner": runtime_owner,
                "status": _derive_gateway_status(runtime_mode, service_profile, topology_payload),
                "managed_by": str(gateway.get("managed_by") or runtime_owner),
                "pid": int(gateway.get("pid") or 0),
                "restart_independent": runtime_mode == "remote",
            },
        },
    }
