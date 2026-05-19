from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.route_request import coerce_request_bool, coerce_request_int, get_json_payload
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval


def load_chart_request_payload() -> dict:
    payload = get_json_payload()
    preview_bar = payload.get("preview_bar")
    data_environment = resolve_data_environment(
        payload.get("market_data_mode") or payload.get("data_environment") or payload.get("environment")
    )
    return {
        "environment": data_environment,
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "interval": normalize_interval(payload.get("interval") or "5m"),
        "start_ms": coerce_request_int(payload.get("start_ms"), 0, minimum=0),
        "end_ms": coerce_request_int(payload.get("end_ms"), 0, minimum=0),
        "include_signals": coerce_request_bool(payload.get("include_signals"), True),
        "include_trace": coerce_request_bool(payload.get("include_trace"), False),
        "preview_bar": preview_bar if isinstance(preview_bar, dict) else None,
        "backtest_run_id": str(payload.get("backtest_run_id") or "").strip(),
    }


def validate_chart_request(app_mod, params: dict, *, require_bounded_start: bool = False):
    if params["environment"] not in app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS:
        return jsonify({"ok": False, "error": "invalid_environment", "environment": params["environment"]}), 400
    if not params["symbol"]:
        return jsonify({"ok": False, "error": "missing_symbol"}), 400
    if params["interval"] not in COMPUTE_INTERVALS:
        return jsonify({"ok": False, "error": "invalid_interval", "interval": params["interval"]}), 400
    if require_bounded_start and params["start_ms"] <= 0:
        return jsonify({"ok": False, "error": "compare_requires_bounded_range"}), 400
    if params["start_ms"] > 0 and params["end_ms"] > 0 and params["start_ms"] > params["end_ms"]:
        return jsonify(
            {
                "ok": False,
                "error": "invalid_range",
                "start_ms": params["start_ms"],
                "end_ms": params["end_ms"],
            }
        ), 400
    return None
