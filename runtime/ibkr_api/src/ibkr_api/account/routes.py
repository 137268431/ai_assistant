from __future__ import annotations

import os
import time
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.account.snapshot import build_account_snapshot_response, enrich_account_snapshot
from ibkr_api.app_core.cache_snapshots import build_snapshot_cache_key, cached_snapshot_response, clear_cached_snapshots, request_force_refresh
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key
from ibkr_api.modes import request_broker_mode


_ACCOUNT_ROUTE_CACHE = RouteSWRCache("account")
_ACCOUNT_PB: Any | None = None
_ACCOUNT_SNAPSHOT_SCOPES = ("account-snapshot",)


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
        clear_cached_snapshots(_ACCOUNT_PB, scopes=_ACCOUNT_SNAPSHOT_SCOPES)
        return
    _ACCOUNT_ROUTE_CACHE.clear()
    clear_cached_snapshots(_ACCOUNT_PB, scopes=_ACCOUNT_SNAPSHOT_SCOPES)


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


def _buying_power_refresh_request(payload: dict[str, Any]) -> bool:
    scope = str((payload or {}).get("refresh_scope") or (payload or {}).get("scope") or "").strip().lower()
    if scope in {"buying_power", "buying-power", "account_summary", "summary"}:
        return True
    return _truthy_param((payload or {}).get("summary_refresh")) or _truthy_param((payload or {}).get("buying_power_refresh"))


def _pnl_refresh_request(payload: dict[str, Any]) -> bool:
    scope = str((payload or {}).get("refresh_scope") or (payload or {}).get("scope") or "").strip().lower()
    if scope in {"pnl", "account_pnl", "account-pnl", "today_pnl", "today-pnl"}:
        return True
    return (
        _truthy_param((payload or {}).get("pnl_refresh"))
        or _truthy_param((payload or {}).get("account_pnl_refresh"))
        or _truthy_param((payload or {}).get("today_pnl_refresh"))
    )


def _manual_account_refresh_request(payload: dict[str, Any]) -> bool:
    scope = str((payload or {}).get("refresh_scope") or (payload or {}).get("scope") or "").strip().lower()
    if scope not in {"account", "account_snapshot", "account-snapshot", "positions", "position_prices", "position-prices", "manual"}:
        return False
    return (
        _truthy_param((payload or {}).get("manual_refresh"))
        or request_force_refresh(payload)
    )


def _prefers_stale_orders_fast(payload: dict[str, Any]) -> bool:
    if not _orders_fast_request(payload):
        return False
    if _truthy_param((payload or {}).get("prefer_live_on_force")):
        return False
    return _truthy_param(
        (payload or {}).get("prefer_stale")
        or (payload or {}).get("prefer_stale_on_force")
        or (payload or {}).get("stale_first")
        or "1"
    )


def _prefers_stale_account_snapshot(payload: dict[str, Any]) -> bool:
    if (_buying_power_refresh_request(payload) or _pnl_refresh_request(payload)) and request_force_refresh(payload):
        return False
    if _truthy_param((payload or {}).get("prefer_live_on_force")):
        return False
    return True


def _diagnostic_broker_refresh_allowed(payload: dict[str, Any]) -> bool:
    if not _truthy_param((payload or {}).get("diagnostic_broker_refresh")):
        return False
    if not _truthy_param(os.environ.get("IBKR_ACCOUNT_SNAPSHOT_DIAGNOSTIC_BROKER_REFRESH_ENABLED")):
        return False
    remote_addr = str(getattr(request, "remote_addr", "") or "").strip()
    return remote_addr in {"127.0.0.1", "::1", "localhost"}


def _account_snapshot_payload_for_cache(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload or {})
    sanitized.pop("broker_force", None)
    if _diagnostic_broker_refresh_allowed(payload):
        sanitized["broker_force"] = "1"
        sanitized["diagnostic_broker_refresh"] = "1"
    else:
        sanitized.pop("diagnostic_broker_refresh", None)
    return sanitized


def _account_snapshot_upstream_timeout(payload: dict[str, Any]) -> float:
    if _orders_fast_request(payload):
        return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_ORDERS_FAST_UPSTREAM_TIMEOUT_SEC", 2.5)
    if _buying_power_refresh_request(payload):
        return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_BUYING_POWER_UPSTREAM_TIMEOUT_SEC", 20.0)
    if _pnl_refresh_request(payload):
        return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_PNL_UPSTREAM_TIMEOUT_SEC", 20.0)
    return cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_UPSTREAM_TIMEOUT_SEC", 20.0)


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def register_account_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    global _ACCOUNT_PB
    pb = deps["pb"]
    _ACCOUNT_PB = pb
    normalize_environment = deps["normalize_environment"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/account_snapshot", methods=["GET"])
    def custom_ibkr_account_snapshot() -> Response:
        query_payload = _account_snapshot_payload_for_cache(request.args.to_dict(flat=True))
        cache_key = canonical_cache_key("account_snapshot", query_payload)
        cached_orders_fast_payload = None
        if _orders_fast_request(query_payload):
            cached_entry = _ACCOUNT_ROUTE_CACHE.peek(cache_key, allow_stale=True)
            if cached_entry is not None:
                cached_orders_fast_payload = cached_entry[0]
        ttl_seconds = cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_TTL_SEC", 5.0)
        stale_seconds = cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_STALE_SEC", 120.0)
        snapshot_stale_seconds = cache_seconds("IBKR_PB_SNAPSHOT_ACCOUNT_STALE_SEC", 30.0)
        force_refresh = request_force_refresh(query_payload)

        def build_account_payload() -> tuple[dict[str, Any], int]:
            return build_account_snapshot_response(
                pb,
                payload=query_payload,
                normalize_environment=normalize_environment,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
                upstream_timeout=_account_snapshot_upstream_timeout(query_payload),
                orders_fast_fallback_payload=cached_orders_fast_payload,
            )

        def build_direct_runtime_payload(*, pnl_only: bool = False) -> tuple[dict[str, Any], int]:
            started = time.monotonic()
            environment = request_broker_mode(query_payload)
            params: list[tuple[str, str]] = [("broker_mode", environment), ("environment", environment)]
            for key in (
                "include_pnl",
                "refresh_scope",
                "scope",
                "pnl_refresh",
                "account_pnl_refresh",
                "today_pnl_refresh",
                "manual_refresh",
                "force",
                "refresh",
                "cache_bust",
            ):
                value = query_payload.get(key)
                if value not in (None, ""):
                    params.append((key, str(value)))
            result = request_json_request(
                "GET",
                runtime_base_url,
                "/ibkr/account",
                params=params,
                timeout=_account_snapshot_upstream_timeout(query_payload),
            )
            status_code = int(result.get("status_code") or 200)
            upstream_payload = _object(result.get("payload"))
            selected_upstream = str(result.get("target_url") or f"{runtime_base_url.rstrip('/')}/ibkr/account")
            if not upstream_payload or (status_code >= 400 and not upstream_payload.get("ok")):
                return {
                    "ok": False,
                    "status": "offline",
                    "environment": environment,
                    "error": result.get("error") or upstream_payload.get("error") or "account_snapshot_upstream_unavailable",
                    "proxy_source": "ibkr-api",
                    "proxy_route": "/api/custom/ibkr/account_snapshot",
                    "proxy_upstream": selected_upstream,
                    "source": "ibkr-api",
                }, 502 if status_code < 400 else status_code

            payload = dict(upstream_payload) if pnl_only else enrich_account_snapshot(pb, dict(upstream_payload), environment)
            diagnostics = _object(payload.get("diagnostics"))
            account_diag = _object(diagnostics.get("account_snapshot"))
            account_diag.update(
                {
                    "direct_runtime_refresh": True,
                    "pnl_only": bool(pnl_only),
                    "upstream_elapsed_ms": round(float(result.get("elapsed_ms") or 0.0), 1),
                    "upstream_timeout_s": float(result.get("timeout_s") or _account_snapshot_upstream_timeout(query_payload) or 0.0),
                    "total_elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                    "upstream_status_code": status_code,
                }
            )
            diagnostics["account_snapshot"] = account_diag
            payload["diagnostics"] = diagnostics
            payload["environment"] = environment
            payload["broker_mode"] = environment
            payload["proxy_source"] = "ibkr-api"
            payload["proxy_route"] = "/api/custom/ibkr/account_snapshot"
            payload["proxy_upstream"] = selected_upstream
            payload["source"] = "ibkr-api"
            return payload, status_code if status_code >= 400 else 200

        if _pnl_refresh_request(query_payload):
            payload, status_code = build_direct_runtime_payload(pnl_only=True)
            response = jsonify(payload)
            return response if status_code == 200 else (response, status_code)

        if _manual_account_refresh_request(query_payload):
            payload, status_code = build_direct_runtime_payload(pnl_only=False)
            response = jsonify(payload)
            return response if status_code == 200 else (response, status_code)

        def build_account_with_snapshot() -> tuple[dict[str, Any], int]:
            if _orders_fast_request(query_payload):
                return build_account_payload()
            return cached_snapshot_response(
                pb,
                scope="account-snapshot",
                cache_key=build_snapshot_cache_key("account-snapshot", query_payload),
                builder=build_account_payload,
                ttl_seconds=ttl_seconds,
                stale_seconds=min(stale_seconds, snapshot_stale_seconds),
                environment=request_broker_mode(query_payload),
                market_date="global",
                force=force_refresh,
                background_refresh=True,
            )

        payload, status_code = _ACCOUNT_ROUTE_CACHE.get(
            cache_key,
            builder=build_account_with_snapshot,
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            force=force_refresh,
            prefer_stale_on_force=_prefers_stale_account_snapshot(query_payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_account_snapshot"] = custom_ibkr_account_snapshot
    return exports


__all__ = ["register_account_routes", "_clear_account_route_cache"]
