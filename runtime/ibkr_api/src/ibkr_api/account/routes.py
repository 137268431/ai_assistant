from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.account.snapshot import build_account_snapshot_response
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key, request_cache_bypass


_ACCOUNT_ROUTE_CACHE = RouteSWRCache("account")


def _account_cache_key_is_orders_fast(key: tuple[Any, ...]) -> bool:
    try:
        params = dict(key[1])
    except Exception:
        params = {}
    profile = str(params.get("snapshot_profile") or params.get("profile") or "").strip().lower()
    if profile in {"orders_fast", "fast_orders", "live_orders_fast"}:
        return True
    for field in ("orders_fast", "fast_orders"):
        value = str(params.get(field) or "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _clear_account_route_cache(*, preserve_orders_fast: bool = False) -> None:
    if preserve_orders_fast:
        _ACCOUNT_ROUTE_CACHE.clear_matching(lambda key: not _account_cache_key_is_orders_fast(key))
        return
    _ACCOUNT_ROUTE_CACHE.clear()


def _truthy_param(value: Any) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    try:
        return float(text) != 0.0
    except (TypeError, ValueError):
        return False


def _orders_fast_request(payload: dict[str, Any]) -> bool:
    profile = str((payload or {}).get("snapshot_profile") or (payload or {}).get("profile") or "").strip().lower()
    if profile in {"orders_fast", "fast_orders", "live_orders_fast"}:
        return True
    for key in ("orders_fast", "fast_orders"):
        if _truthy_param((payload or {}).get(key)):
            return True
    return False


def _prefers_stale_orders_fast(payload: dict[str, Any]) -> bool:
    if not _orders_fast_request(payload):
        return False
    # Default to a bounded live refresh; stale-first is explicit so pending
    # order monitors do not hide a just-submitted bracket behind an old cache.
    return _truthy_param(
        (payload or {}).get("prefer_stale")
        or (payload or {}).get("prefer_stale_on_force")
        or (payload or {}).get("stale_first")
    )


def _account_snapshot_upstream_timeout(payload: dict[str, Any]) -> float:
    if _orders_fast_request(payload):
        return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_ORDERS_FAST_UPSTREAM_TIMEOUT_SEC", 2.5)
    return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_UPSTREAM_TIMEOUT_SEC", 20.0)


def register_account_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/account_snapshot", methods=["GET"])
    def custom_ibkr_account_snapshot() -> Response:
        query_payload = request.args.to_dict(flat=True)
        cache_key = canonical_cache_key("account_snapshot", query_payload)
        cached_orders_fast_payload = None
        if _orders_fast_request(query_payload):
            cached_entry = _ACCOUNT_ROUTE_CACHE.peek(cache_key, allow_stale=True)
            if cached_entry is not None:
                cached_orders_fast_payload = cached_entry[0]
        payload, status_code = _ACCOUNT_ROUTE_CACHE.get(
            cache_key,
            builder=lambda: build_account_snapshot_response(
                pb,
                payload=query_payload,
                normalize_environment=normalize_environment,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
                upstream_timeout=_account_snapshot_upstream_timeout(query_payload),
                orders_fast_fallback_payload=cached_orders_fast_payload,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_TTL_SEC", 5.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_STALE_SEC", 120.0),
            force=request_cache_bypass(query_payload),
            prefer_stale_on_force=_prefers_stale_orders_fast(query_payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_account_snapshot"] = custom_ibkr_account_snapshot
    return exports


__all__ = ["register_account_routes", "_clear_account_route_cache"]
