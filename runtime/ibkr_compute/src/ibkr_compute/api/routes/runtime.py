from __future__ import annotations

from flask import redirect

from ibkr_compute.api.route_response import build_json_pair_response, build_json_request_response
from ibkr_compute.api.route_runtime import get_app_module, get_requested_broker_mode
from ibkr_compute.api.runtime_proxy import register_runtime_proxy_route, should_proxy_runtime_requests
from ibkr_compute.api.runtime.views import (
    _build_ibkr_2fa_probe_response,
    _build_ibkr_2fa_takeover_response,
    _build_ibkr_gateway_restart_response,
    _build_ibkr_gateway_start_response,
    _build_ibkr_gateway_stop_response,
    _build_ibkr_monitor_response,
    _build_ibkr_panic_reset_response,
    _build_ibkr_signal_wakeup_response,
    _build_ibkr_start_response,
    _build_ibkr_status_response,
    _build_ibkr_stop_response,
    _build_ibkr_universe_reconcile_response,
)


def register_runtime_routes(app):
    if should_proxy_runtime_requests():
        register_runtime_proxy_route(app, "ibkr_start", "/ibkr/start", ["POST"])
        register_runtime_proxy_route(app, "ibkr_stop", "/ibkr/stop", ["POST"])
        register_runtime_proxy_route(app, "ibkr_gateway_start", "/ibkr/gateway/start", ["POST"])
        register_runtime_proxy_route(app, "ibkr_gateway_stop", "/ibkr/gateway/stop", ["POST"])
        register_runtime_proxy_route(app, "ibkr_gateway_restart", "/ibkr/gateway/restart", ["POST"])
        register_runtime_proxy_route(app, "ibkr_status", "/ibkr/status", ["GET"])
        register_runtime_proxy_route(app, "ibkr_monitor", "/ibkr/monitor", ["GET"])
        register_runtime_proxy_route(app, "ibkr_universe_reconcile", "/ibkr/universe/reconcile", ["POST"])
        register_runtime_proxy_route(app, "ibkr_2fa_takeover", "/ibkr/2fa/takeover", ["POST"])
        register_runtime_proxy_route(app, "ibkr_2fa_probe", "/ibkr/2fa/probe", ["POST"])
        register_runtime_proxy_route(app, "ibkr_panic_reset", "/ibkr/panic-reset", ["POST"])
        register_runtime_proxy_route(app, "ibkr_signal_wakeup", "/ibkr/signals/wakeup", ["POST"])

        @app.route("/ibkr/dashboard", methods=["GET"])
        def ibkr_dashboard():
            app_mod = get_app_module()
            console_base_url = getattr(app_mod, "CONSOLE_BASE_URL", "") or app_mod.PB_PUBLIC_URL
            return redirect(f"{console_base_url.rstrip('/')}/ibkr_runtime.html", code=302)

        return

    @app.route("/ibkr/start", methods=["POST"])
    def ibkr_start():
        return build_json_request_response(_build_ibkr_start_response)

    @app.route("/ibkr/stop", methods=["POST"])
    def ibkr_stop():
        return build_json_pair_response(_build_ibkr_stop_response)

    @app.route("/ibkr/gateway/start", methods=["POST"])
    def ibkr_gateway_start():
        return build_json_request_response(_build_ibkr_gateway_start_response)

    @app.route("/ibkr/gateway/stop", methods=["POST"])
    def ibkr_gateway_stop():
        return build_json_request_response(_build_ibkr_gateway_stop_response)

    @app.route("/ibkr/gateway/restart", methods=["POST"])
    def ibkr_gateway_restart():
        return build_json_request_response(_build_ibkr_gateway_restart_response)

    @app.route("/ibkr/status", methods=["GET"])
    def ibkr_status():
        return build_json_pair_response(_build_ibkr_status_response)

    @app.route("/ibkr/monitor", methods=["GET"])
    def ibkr_monitor():
        requested_environment = get_requested_broker_mode()
        return build_json_pair_response(_build_ibkr_monitor_response, requested_environment)

    @app.route("/ibkr/universe/reconcile", methods=["POST"])
    def ibkr_universe_reconcile():
        return build_json_request_response(_build_ibkr_universe_reconcile_response)

    @app.route("/ibkr/2fa/takeover", methods=["POST"])
    def ibkr_2fa_takeover():
        return build_json_request_response(_build_ibkr_2fa_takeover_response)

    @app.route("/ibkr/2fa/probe", methods=["POST"])
    def ibkr_2fa_probe():
        return build_json_request_response(_build_ibkr_2fa_probe_response)

    @app.route("/ibkr/panic-reset", methods=["POST"])
    def ibkr_panic_reset():
        return build_json_request_response(_build_ibkr_panic_reset_response)

    @app.route("/ibkr/signals/wakeup", methods=["POST"])
    def ibkr_signal_wakeup():
        return build_json_request_response(_build_ibkr_signal_wakeup_response)

    @app.route("/ibkr/dashboard", methods=["GET"])
    def ibkr_dashboard():
        app_mod = get_app_module()
        console_base_url = getattr(app_mod, "CONSOLE_BASE_URL", "") or app_mod.PB_PUBLIC_URL
        return redirect(f"{console_base_url.rstrip('/')}/ibkr_runtime.html", code=302)
