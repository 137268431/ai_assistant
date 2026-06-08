from __future__ import annotations

import os

from ibkr_compute.api.runtime_status_client import (
    get_remote_runtime_status,
    get_runtime_internal_url as get_remote_runtime_internal_url,
    is_runtime_status_payload,
)
from ibkr_compute.api.shared.service_status import get_service_status_snapshot

DEFAULT_COMPUTE_INTERNAL_URL = "http://127.0.0.1:5100"
DEFAULT_BACKTEST_INTERNAL_URL = "http://127.0.0.1:5105"
DEFAULT_RUNTIME_INTERNAL_URL = "http://127.0.0.1:5101"
DEFAULT_API_INTERNAL_URL = "http://127.0.0.1:5102"
DEFAULT_SCHEDULER_INTERNAL_URL = "http://127.0.0.1:5103"
DEFAULT_CONSOLE_BASE_URL = "http://127.0.0.1:5104"
DEFAULT_PB_BASE_URL = "http://127.0.0.1:8090"
IBGW_CLIENT_ID_OFFSETS = {
    "runtime": 0,
    "compute": 20,
    "api": 30,
    "scheduler": 40,
    "backtest": 50,
}


def _normalize_base_url(value: str | None, default: str) -> str:
    text = str(value or "").strip() or default
    return text.rstrip("/")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except Exception:
        return int(default)


def _ib_gateway_client_id(profile: str) -> int:
    normalized = str(profile or "").strip().lower()
    base_client_id = _env_int("IBGW_CLIENT_ID", 31)
    default = base_client_id + IBGW_CLIENT_ID_OFFSETS.get(normalized, 0)
    env_key = f"IBGW_{normalized.upper()}_CLIENT_ID"
    return _env_int(env_key, default)


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


def uses_remote_compute_service() -> bool:
    return get_runtime_mode() == "remote" and get_service_profile() == "runtime"


def get_compute_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("IBKR_COMPUTE_INTERNAL_URL"),
        default or DEFAULT_COMPUTE_INTERNAL_URL,
    )


def get_backtest_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("IBKR_BACKTEST_INTERNAL_URL"),
        default or DEFAULT_BACKTEST_INTERNAL_URL,
    )


def get_runtime_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        get_remote_runtime_internal_url(default),
        default or DEFAULT_RUNTIME_INTERNAL_URL,
    )


def get_api_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("IBKR_API_INTERNAL_URL"),
        default or DEFAULT_API_INTERNAL_URL,
    )


def get_scheduler_internal_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("IBKR_SCHEDULER_INTERNAL_URL"),
        default or DEFAULT_SCHEDULER_INTERNAL_URL,
    )


def get_console_base_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("CONSOLE_BASE_URL")
        or os.environ.get("QUANT_BASE_URL")
        or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
        or default
        or "https://quant.lzw-glory.top",
        default or DEFAULT_CONSOLE_BASE_URL,
    )


def get_pocketbase_base_url(default: str | None = None) -> str:
    return _normalize_base_url(
        os.environ.get("PB_BASE_URL"),
        default or DEFAULT_PB_BASE_URL,
    )


def _derive_runtime_service_status(runtime_mode: str, service_profile: str, service_status: dict) -> str:
    readiness_degraded = _runtime_readiness_degraded(service_status)
    if service_profile == "runtime":
        if readiness_degraded:
            return "degraded"
        return "running" if service_status.get("ok", True) else "degraded"
    if runtime_mode == "remote":
        if is_runtime_status_payload(service_status):
            if not bool(service_status.get("ok", True)):
                return "degraded"
            return "degraded" if readiness_degraded else "running"
        return "expected_remote"
    return "embedded"


def _runtime_readiness_degraded(service_status: dict) -> bool:
    gateway = (service_status or {}).get("gateway") or {}
    session = (service_status or {}).get("session") or {}
    websocket = (service_status or {}).get("websocket") or {}
    broker = (gateway.get("broker") or {}) if isinstance(gateway, dict) else {}
    if isinstance(gateway, dict):
        if "reachable" in gateway and not bool(gateway.get("reachable")):
            return True
        try:
            if int(gateway.get("status_code") or 0) >= 400:
                return True
        except Exception:
            pass
    if isinstance(broker, dict):
        if "ready" in broker and not bool(broker.get("ready")):
            return True
        if "connected" in broker and not bool(broker.get("connected")):
            return True
        try:
            if int(broker.get("status_code") or 0) >= 400:
                return True
        except Exception:
            pass
    if isinstance(session, dict) and "authenticated" in session and not bool(session.get("authenticated")):
        return True
    if isinstance(websocket, dict) and ("ready" in websocket or "connected" in websocket):
        return not bool(websocket.get("ready") or websocket.get("connected"))
    return False


def _derive_gateway_status(runtime_mode: str, service_profile: str, service_status: dict) -> str:
    gateway = (service_status or {}).get("gateway") or {}
    if bool(gateway.get("reachable")):
        return "running"
    if bool(gateway.get("running")) and gateway.get("reachable") is False:
        return "degraded"
    if bool(gateway.get("running")):
        return "running"
    if service_profile == "runtime":
        return "offline"
    if runtime_mode == "remote":
        return "expected_remote"
    return "embedded"


def _select_topology_status_payload(
    runtime_mode: str,
    service_profile: str,
    payload: dict,
    *,
    fetch_runtime_status: bool | None = None,
) -> dict:
    if service_profile == "runtime" or runtime_mode != "remote":
        return payload
    if is_runtime_status_payload(payload):
        return payload
    should_fetch = (
        str(os.environ.get("IBKR_TOPOLOGY_FETCH_RUNTIME_STATUS") or "").strip().lower()
        in {"1", "true", "yes", "on"}
        if fetch_runtime_status is None
        else bool(fetch_runtime_status)
    )
    if not should_fetch:
        return payload
    remote_payload = get_remote_runtime_status()
    if is_runtime_status_payload(remote_payload):
        return remote_payload
    return payload


def _owned_service_status(service_profile: str, owner_profile: str) -> str:
    return "running" if service_profile == owner_profile else "peer"


def build_service_topology(
    service=None,
    service_status: dict | None = None,
    *,
    fetch_runtime_status: bool | None = None,
) -> dict:
    runtime_mode = get_runtime_mode()
    service_profile = get_service_profile()
    compute_internal_url = get_compute_internal_url()
    backtest_internal_url = get_backtest_internal_url()
    runtime_internal_url = get_runtime_internal_url()
    api_internal_url = get_api_internal_url()
    scheduler_internal_url = get_scheduler_internal_url()
    console_base_url = get_console_base_url()
    pocketbase_base_url = get_pocketbase_base_url()
    runtime_owner = "ibkr-runtime" if runtime_mode == "remote" else "ibkr-compute"

    payload = service_status if isinstance(service_status, dict) else {}
    if not payload and service is not None and hasattr(service, "status"):
        payload = get_service_status_snapshot(service)

    topology_payload = _select_topology_status_payload(
        runtime_mode,
        service_profile,
        payload,
        fetch_runtime_status=fetch_runtime_status,
    )
    gateway = topology_payload.get("gateway") or {}
    session = topology_payload.get("session") or {}
    runtime_client_id = _ib_gateway_client_id("runtime")
    try:
        runtime_client_id = int(
            str(
                topology_payload.get("ib_gateway_client_id")
                or topology_payload.get("broker_client_id")
                or runtime_client_id
            ).strip()
        )
    except Exception:
        runtime_client_id = _ib_gateway_client_id("runtime")

    return {
        "service_profile": service_profile,
        "runtime_mode": runtime_mode,
        "split_stack": True,
        "restart_independent": runtime_mode == "remote",
        "services": {
            "pocketbase": {
                "service_name": "pocketbase",
                "kind": "storage_auth",
                "fault_domain": "storage_auth",
                "owner": "pocketbase",
                "status": "external",
                "public_url": pocketbase_base_url,
                "responsibility": "auth + collections",
                "restart_independent": True,
            },
            "ibkr-console": {
                "service_name": "ibkr-console",
                "kind": "console",
                "fault_domain": "console",
                "owner": "ibkr-console",
                "status": _owned_service_status(service_profile, "console"),
                "public_url": console_base_url,
                "upstream": api_internal_url,
                "responsibility": "static frontend",
                "restart_independent": True,
            },
            "ibkr-api": {
                "service_name": "ibkr-api",
                "kind": "control_plane",
                "fault_domain": "control_plane",
                "owner": "ibkr-api",
                "status": _owned_service_status(service_profile, "api"),
                "ib_gateway_client_id": _ib_gateway_client_id("api"),
                "internal_url": api_internal_url,
                "upstream": pocketbase_base_url,
                "responsibility": "compatibility routes + control APIs",
                "restart_independent": True,
            },
            "ibkr-scheduler": {
                "service_name": "ibkr-scheduler",
                "kind": "scheduler",
                "fault_domain": "scheduler",
                "owner": "ibkr-scheduler",
                "status": _owned_service_status(service_profile, "scheduler"),
                "ib_gateway_client_id": _ib_gateway_client_id("scheduler"),
                "internal_url": scheduler_internal_url,
                "upstream": pocketbase_base_url,
                "responsibility": "job registry + persisted cursor dispatch",
                "restart_independent": True,
            },
            "ibkr-compute": {
                "service_name": "ibkr-compute",
                "kind": "compute_plane",
                "fault_domain": "compute_plane",
                "owner": "ibkr-compute",
                "status": _owned_service_status(service_profile, "compute"),
                "ib_gateway_client_id": _ib_gateway_client_id("compute"),
                "internal_url": compute_internal_url,
                "upstream": runtime_internal_url if runtime_mode == "remote" else "",
                "runtime_mode": runtime_mode,
                "runtime_owner": runtime_owner,
                "responsibility": "indicators + signals + scans + history/data quality",
                "restart_independent": runtime_mode == "remote",
            },
            "ibkr-backtest": {
                "service_name": "ibkr-backtest",
                "kind": "backtest_plane",
                "fault_domain": "backtest_plane",
                "owner": "ibkr-backtest",
                "status": _owned_service_status(service_profile, "backtest"),
                "ib_gateway_client_id": _ib_gateway_client_id("backtest"),
                "internal_url": backtest_internal_url,
                "upstream": pocketbase_base_url,
                "responsibility": "backtest runs + replay + backtest cleanup",
                "restart_independent": True,
            },
            "ibkr-runtime": {
                "service_name": "ibkr-runtime",
                "kind": "data_plane",
                "fault_domain": "data_plane",
                "owner": runtime_owner,
                "status": _derive_runtime_service_status(runtime_mode, service_profile, topology_payload),
                "internal_url": runtime_internal_url,
                "upstream": runtime_internal_url if runtime_mode == "remote" else "",
                "runtime_mode": runtime_mode,
                "ib_gateway_client_id": runtime_client_id,
                "session_authenticated": bool(session.get("authenticated")),
                "responsibility": "gateway + bars + live trading state",
                "restart_independent": runtime_mode == "remote",
            },
            "ibkr-gateway": {
                "service_name": "ibkr-gateway",
                "kind": "broker_gateway",
                "fault_domain": "broker_gateway",
                "owner": runtime_owner,
                "status": _derive_gateway_status(runtime_mode, service_profile, topology_payload),
                "managed_by": str(gateway.get("managed_by") or runtime_owner),
                "pid": int(gateway.get("pid") or 0),
                "responsibility": "IBKR client gateway",
                "restart_independent": runtime_mode == "remote",
            },
        },
    }
