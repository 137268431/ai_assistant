import os
import logging

from flask import Flask

from ibkr_compute.api.app_entrypoints import (
    build_compute_entrypoint_response,
    build_compute_prime_entrypoint_response,
    build_proxy_legacy_pb_collections_response,
    build_recompute_entrypoint_response,
    build_scan_entrypoint_response,
    build_scan_status_entrypoint_response,
)
from ibkr_compute.api.app_exports import apply_app_exports
from ibkr_compute.api.app_bootstrap import (
    build_constant_bundle,
    build_runtime_state_bundle,
    build_service_bundle,
    register_canonical_module_alias,
)
from ibkr_compute.api.app_logging import configure_api_logging
from ibkr_compute.api.app_routes import register_api_routes


configure_api_logging()
logger = logging.getLogger("ibkr_compute.api")
register_canonical_module_alias(__name__, globals())
apply_app_exports(globals())

app = Flask(__name__)

globals().update(
    build_service_bundle(
        compute_runner=lambda payload: _run_internal_compute(payload),
        scan_runner=lambda payload: _run_internal_scan(payload),
        current_market_date_resolver=lambda: current_market_date(),
        runtime_status_resolver=lambda environment: _get_runtime_status_snapshot(environment),
    )
)
globals().update(build_runtime_state_bundle())
globals().update(build_constant_bundle())


def proxy_legacy_pb_collections(subpath):
    return build_proxy_legacy_pb_collections_response(subpath)


def compute():
    return build_compute_entrypoint_response()


def compute_prime():
    return build_compute_prime_entrypoint_response()


def scan():
    return build_scan_entrypoint_response()


def scan_status():
    return build_scan_status_entrypoint_response()


def recompute():
    return build_recompute_entrypoint_response()

# Domain route registration keeps the Flask entrypoint stable while moving
# transport shells out of the compute composition root.
register_api_routes(app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
