from __future__ import annotations

import inspect
import logging
import os


logger = logging.getLogger(__name__)


def _as_dict(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _safe_component_status(service, attr_name: str, errors: list[dict]) -> dict:
    component = getattr(service, attr_name, None)
    status_fn = getattr(component, "status", None)
    if not callable(status_fn):
        return {}
    try:
        return _as_dict(status_fn())
    except Exception as exc:
        logger.warning("IBKR service component status failed: %s: %s", attr_name, exc, exc_info=True)
        errors.append({"section": attr_name, "error": str(exc)})
        return {}


def _safe_method_payload(service, method_name: str, errors: list[dict]) -> dict:
    method = getattr(service, method_name, None)
    if not callable(method):
        return {}
    try:
        return _as_dict(method())
    except Exception as exc:
        logger.warning("IBKR service status helper failed: %s: %s", method_name, exc, exc_info=True)
        errors.append({"section": method_name, "error": str(exc)})
        return {}


def _safe_method_text(service, method_name: str, errors: list[dict]) -> str:
    method = getattr(service, method_name, None)
    if not callable(method):
        return ""
    try:
        return str(method() or "")
    except Exception as exc:
        logger.warning("IBKR service status text helper failed: %s: %s", method_name, exc, exc_info=True)
        errors.append({"section": method_name, "error": str(exc)})
        return ""


def _safe_list_attr(service, attr_name: str) -> list:
    value = getattr(service, attr_name, [])
    try:
        return list(value or [])
    except Exception:
        return []


def _build_minimal_runtime_status(service, error: Exception | None = None) -> dict:
    errors: list[dict] = []
    starting = bool(getattr(service, "_starting", False))
    running = bool(getattr(service, "_running", False))
    market_ws_symbols = []
    market_ws_method = getattr(service, "_market_ws_symbols", None)
    if callable(market_ws_method):
        try:
            market_ws_symbols = list(market_ws_method() or [])
        except Exception as exc:
            logger.warning("IBKR service market ws symbol status failed: %s", exc, exc_info=True)
            errors.append({"section": "_market_ws_symbols", "error": str(exc)})
    websocket_status = _safe_component_status(service, "ws_client", errors)
    active_subscription_symbols = _safe_list_attr(service, "_active_subscription_symbols")
    active_subscription_set = set(active_subscription_symbols)
    market_ws_symbols_ready = len([symbol for symbol in market_ws_symbols if symbol in active_subscription_set])
    status = {
        "gateway_control_available": True,
        "starting": starting,
        "startup_complete": bool(running and not starting),
        "runtime_phase": _safe_method_text(service, "_runtime_phase_label", errors),
        "environment": str(os.environ.get("IBKR_ENVIRONMENT") or "live"),
        "gateway": _safe_component_status(service, "gateway_manager", errors),
        "session": _safe_component_status(service, "session_keeper", errors),
        "websocket": websocket_status,
        "bar_aggregator": _safe_component_status(service, "bar_aggregator", errors),
        "realtime_quotes": _safe_component_status(service, "realtime_quote_book", errors),
        "canonical_5m": _safe_method_payload(service, "_copy_official_5m_state", errors),
        "warmup": _safe_method_payload(service, "_copy_warmup_state", errors),
        "daily_scan": _safe_method_payload(service, "_copy_daily_scan_state", errors),
        "data_writer": _safe_component_status(service, "data_writer", errors),
        "data_backfill": _safe_component_status(service, "data_backfill", errors),
        "order_tracker": _safe_component_status(service, "order_tracker", errors),
        "signal_router": _safe_component_status(service, "signal_router", errors),
        "signal_processor": _safe_component_status(service, "signal_processor", errors),
        "market_universe": {
            "market_date": str(getattr(service, "_current_market_date", "") or ""),
            "active_target_date": str(getattr(service, "_active_target_date", "") or ""),
            "active_target_count": len(_safe_list_attr(service, "_active_trade_symbols")),
            "active_subscription_count": len(active_subscription_symbols),
            "active_target_symbols": _safe_list_attr(service, "_active_trade_symbols"),
            "active_subscription_symbols": active_subscription_symbols,
            "active_trade_symbols": _safe_list_attr(service, "_active_trade_symbols"),
            "market_ws_symbols": market_ws_symbols,
            "market_ws_symbols_total": len(market_ws_symbols),
            "market_ws_symbols_ready": market_ws_symbols_ready,
            "market_ws_ready": bool(
                (websocket_status.get("connected") or websocket_status.get("ready"))
                and len(market_ws_symbols) > 0
                and market_ws_symbols_ready >= len(market_ws_symbols)
            ),
        },
    }
    if error is not None:
        status["status_error"] = str(error)
        errors.insert(0, {"section": "service.status", "error": str(error)})
    if errors:
        status["status_errors"] = errors
    return status


def get_service_status_snapshot(
    service,
    default: dict | None = None,
    *,
    refresh_auth: bool = False,
) -> dict:
    fallback = dict(default) if isinstance(default, dict) else {}
    if not service:
        return fallback

    status_fn = getattr(service, "status", None)
    if not callable(status_fn):
        return fallback

    try:
        signature = inspect.signature(status_fn)
    except (TypeError, ValueError):
        signature = None

    try:
        if signature and "refresh_auth" in signature.parameters:
            payload = status_fn(refresh_auth=refresh_auth)
        else:
            payload = status_fn()
    except Exception as exc:
        logger.warning("IBKR service status snapshot failed: %s", exc, exc_info=True)
        minimal = _build_minimal_runtime_status(service, exc)
        return {**fallback, **minimal}

    return dict(payload) if isinstance(payload, dict) else fallback


__all__ = ["get_service_status_snapshot"]
