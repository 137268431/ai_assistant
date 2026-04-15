from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.shared.route_request import get_json_payload


def json_response(payload: dict, status_code: int = 200):
    return jsonify(payload), status_code


def build_json_pair_response(builder, *args, **kwargs):
    response_payload, status_code = builder(*args, **kwargs)
    return json_response(response_payload, status_code)


def build_json_request_response(builder, *args, **kwargs):
    return build_json_pair_response(builder, get_json_payload(), *args, **kwargs)
