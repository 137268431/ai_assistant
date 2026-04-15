from __future__ import annotations

from ibkr_compute.api.compute.pipeline_views import build_compute_response
from ibkr_compute.api.compute.runtime_ops import build_recompute_response, build_scan_response
from ibkr_compute.api.legacy.views import build_legacy_pb_proxy_response
from ibkr_compute.api.shared.route_request import get_json_payload


def build_proxy_legacy_pb_collections_response(subpath):
    return build_legacy_pb_proxy_response(subpath)


def build_compute_entrypoint_response():
    return build_compute_response(get_json_payload())


def build_scan_entrypoint_response():
    return build_scan_response(get_json_payload())


def build_recompute_entrypoint_response():
    return build_recompute_response()
