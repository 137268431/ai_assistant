from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

from ibkr_compute.api.ops.action_views import (
    build_backtest_batch_detail_response,
    build_backtest_batches_response,
    build_backtest_cancel_response,
    build_backtest_cleanup_response,
    build_backtest_execution_cost_fills_response,
    build_backtest_execution_cost_import_recent_fills_response,
    build_backtest_execution_cost_import_response,
    build_backtest_execution_cost_profile_response,
    build_backtest_replay_response,
    build_backtest_run_detail_response,
    build_backtest_run_response,
    build_backtest_runs_response,
    build_backtest_status_response,
)
from ibkr_compute.api.service_topology import (
    build_service_topology,
    get_api_internal_url,
    get_backtest_internal_url,
    get_console_base_url,
    get_runtime_mode,
    get_runtime_internal_url,
)
from ibkr_compute.api.shared.route_runtime import register_app_module_context
from ibkr_compute.api.support.symbols import normalize_symbol_csv, normalize_symbols
from ibkr_compute.backtest import BacktestService
from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_compute.core.config import Config
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.observability.prometheus import install_flask_metrics


SERVICE_NAME = "ibkr-backtest"
PORT = int(os.environ.get("PORT", "5105"))
PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://127.0.0.1:8090")
CONSOLE_BASE_URL = get_console_base_url()
PB_PUBLIC_URL = CONSOLE_BASE_URL
SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "backtest"]
DEFAULT_COMPUTE_ENVIRONMENTS = ["live"]

pb = PBClient(base_url=PB_BASE_URL)
cfg = Config(pb_client=pb)
_start_time = time.time()


def _register_canonical_module_alias():
    module = sys.modules.get(__name__)
    canonical_name = str(getattr(__spec__, "name", "") or "").strip()
    if module is not None and canonical_name:
        sys.modules.setdefault(canonical_name, module)
    return module


def _service_profile() -> str:
    return str(os.environ.get("IBKR_SERVICE_PROFILE") or "backtest").strip().lower() or "backtest"


def _normalize_runtime_environment_name(value, default: str = "live") -> str:
    normalized = resolve_data_environment(value or default)
    if normalized in SUPPORTED_COMPUTE_ENVIRONMENTS:
        return normalized
    fallback = resolve_data_environment(default or "live")
    return fallback if fallback in SUPPORTED_COMPUTE_ENVIRONMENTS else "live"


def get_ibkr_service():
    return None


def _maybe_restore_ibkr_service(*_args, **_kwargs):
    return None


def _ibkr_service_environment(_service=None) -> str:
    return configured_broker_mode()


def _backtest_account_snapshot_provider(environment: str = "") -> dict:
    runtime_environment = normalize_broker_mode(environment, configured_broker_mode())
    errors = []
    for base_url, path in (
        (get_runtime_internal_url(), "/ibkr/account"),
        (get_api_internal_url(), "/api/custom/ibkr/account_snapshot"),
    ):
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}{path}",
                params={"environment": runtime_environment},
                timeout=20,
            )
            payload = response.json() if response.content else {}
            if response.ok and isinstance(payload, dict):
                return payload
            errors.append(
                f"{path}:{response.status_code}:{str((payload or {}).get('error') or '')[:120]}"
            )
        except Exception as exc:
            errors.append(f"{path}:{str(exc)[:120]}")
    return {"ok": False, "error": "; ".join(errors) or "account_snapshot_unavailable", "summary": {}}


backtest_service = BacktestService(pb, account_snapshot_provider=_backtest_account_snapshot_provider)

app = Flask(__name__, static_folder=None)
install_flask_metrics(app, service_name=SERVICE_NAME)
register_app_module_context(app, _register_canonical_module_alias() or sys.modules[__name__])


def _build_backtest_topology() -> dict:
    service_profile = _service_profile()
    topology = build_service_topology()
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    backtest = dict(services.get(SERVICE_NAME) if isinstance(services.get(SERVICE_NAME), dict) else {})
    services[SERVICE_NAME] = {
        **backtest,
        "service_name": SERVICE_NAME,
        "kind": "backtest_plane",
        "fault_domain": "backtest_plane",
        "owner": SERVICE_NAME,
        "status": "running",
        "internal_url": get_backtest_internal_url(f"http://127.0.0.1:{PORT}"),
        "upstream": PB_BASE_URL.rstrip("/"),
        "responsibility": "backtest jobs + replay",
        "restart_independent": True,
    }
    topology["service_profile"] = service_profile
    topology["split_stack"] = True
    topology["restart_independent"] = True
    topology["services"] = services
    return topology


def build_health_response():
    try:
        backtest_status = backtest_service.status()
    except Exception as exc:
        backtest_status = {"ok": False, "error": str(exc)}

    ok = bool(backtest_status.get("ok", True))
    status = "running" if ok else "degraded"
    payload = {
        "ok": ok,
        "status": status,
        "service": SERVICE_NAME,
        "service_profile": _service_profile(),
        "runtime_mode": get_runtime_mode(),
        "port": PORT,
        "pb_base_url": PB_BASE_URL,
        "backtest": backtest_status,
        "uptime_s": round(time.time() - _start_time, 1),
        "checked_at": (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ),
        "service_topology": _build_backtest_topology(),
    }
    return jsonify(payload), 200 if ok else 503


def register_backtest_routes(flask_app: Flask) -> None:
    flask_app.add_url_rule(
        "/backtest/run",
        endpoint="backtest_run",
        view_func=build_backtest_run_response,
        methods=["POST"],
    )
    flask_app.add_url_rule(
        "/backtest/status",
        endpoint="backtest_status",
        view_func=build_backtest_status_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/runs",
        endpoint="backtest_runs",
        view_func=build_backtest_runs_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/run",
        endpoint="backtest_run_detail",
        view_func=build_backtest_run_detail_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/batches",
        endpoint="backtest_batches",
        view_func=build_backtest_batches_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/batch",
        endpoint="backtest_batch_detail",
        view_func=build_backtest_batch_detail_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/cancel",
        endpoint="backtest_cancel",
        view_func=build_backtest_cancel_response,
        methods=["POST"],
    )
    flask_app.add_url_rule(
        "/backtest/replay",
        endpoint="backtest_replay",
        view_func=build_backtest_replay_response,
        methods=["GET"],
    )
    flask_app.add_url_rule(
        "/backtest/cleanup",
        endpoint="backtest_cleanup",
        view_func=build_backtest_cleanup_response,
        methods=["POST"],
    )
    flask_app.add_url_rule(
        "/backtest/execution-cost/import",
        endpoint="backtest_execution_cost_import",
        view_func=build_backtest_execution_cost_import_response,
        methods=["POST"],
    )
    flask_app.add_url_rule(
        "/backtest/execution-cost/import-recent-fills",
        endpoint="backtest_execution_cost_import_recent_fills",
        view_func=build_backtest_execution_cost_import_recent_fills_response,
        methods=["POST"],
    )
    flask_app.add_url_rule(
        "/backtest/execution-cost/fills",
        endpoint="backtest_execution_cost_fills",
        view_func=build_backtest_execution_cost_fills_response,
        methods=["GET", "POST"],
    )
    flask_app.add_url_rule(
        "/backtest/execution-cost/profile",
        endpoint="backtest_execution_cost_profile",
        view_func=build_backtest_execution_cost_profile_response,
        methods=["GET", "POST"],
    )
    flask_app.add_url_rule(
        "/health",
        endpoint="health",
        view_func=build_health_response,
        methods=["GET"],
    )


register_backtest_routes(app)


if __name__ == "__main__":
    print(f"[IBKR Backtest] Starting on port {PORT}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
