from __future__ import annotations

import inspect
import logging

from ibkr_compute.core.broker_mode import broker_mode_payload


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


def _with_resource_governor_snapshot(service, payload: dict, errors: list[dict]) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    if isinstance(payload.get("resource_governor"), dict):
        return payload
    resource_governor = _safe_method_payload(service, "_resource_governor_snapshot", errors)
    if resource_governor:
        payload = dict(payload)
        payload["resource_governor"] = resource_governor
    return payload


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


def _safe_method_int(service, method_name: str, default: int, errors: list[dict]) -> int:
    method = getattr(service, method_name, None)
    if not callable(method):
        return max(0, int(default or 0))
    try:
        return max(0, int(method() or 0))
    except Exception as exc:
        logger.warning("IBKR service status int helper failed: %s: %s", method_name, exc, exc_info=True)
        errors.append({"section": method_name, "error": str(exc)})
        return max(0, int(default or 0))


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
    watchlist_trade_symbols = _safe_list_attr(service, "_watchlist_trade_symbols")
    active_trade_symbols = _safe_list_attr(service, "_active_trade_symbols")
    active_trade_symbol_set = set(active_trade_symbols)
    candidate_target_symbols: list[str] = []
    target_rows_method = getattr(service, "_today_target_rows", None)
    normalize_symbols_method = getattr(service, "_normalize_symbol_list", None)
    if callable(target_rows_method) and callable(normalize_symbols_method):
        try:
            _target_date, target_rows = target_rows_method()
            candidate_target_symbols = list(
                normalize_symbols_method(
                    [
                        row.get("symbol")
                        for row in (target_rows or [])
                        if isinstance(row, dict)
                        and str(row.get("status") or "").strip().lower() == "candidate"
                    ]
                )
            )
        except Exception as exc:
            logger.warning("IBKR service candidate target status failed: %s", exc, exc_info=True)
            errors.append({"section": "_today_target_rows", "error": str(exc)})
    execution_eligible_symbols = list(active_trade_symbols)
    direction_method = getattr(service, "_active_target_direction_biases", None)
    if callable(direction_method):
        try:
            execution_eligible_symbols = [
                str(symbol or "").strip().upper()
                for symbol in (direction_method() or {}).keys()
                if str(symbol or "").strip()
            ]
        except Exception as exc:
            logger.warning("IBKR service execution target status failed: %s", exc, exc_info=True)
            errors.append({"section": "_active_target_direction_biases", "error": str(exc)})
    execution_eligible_symbol_set = set(execution_eligible_symbols)
    observe_target_symbols = sorted(active_trade_symbol_set - execution_eligible_symbol_set)
    inactive_trade_symbols = [
        symbol for symbol in watchlist_trade_symbols
        if symbol not in active_trade_symbol_set
    ]
    no_active_targets = bool(watchlist_trade_symbols) and not bool(active_trade_symbol_set) and not bool(candidate_target_symbols)
    no_execution_eligible_targets = bool(active_trade_symbol_set) and not bool(execution_eligible_symbol_set)
    if active_trade_symbol_set:
        trade_universe_status = "ready"
    elif candidate_target_symbols:
        trade_universe_status = "observing_candidates"
    elif watchlist_trade_symbols:
        trade_universe_status = "no_active_targets"
    else:
        trade_universe_status = "no_trade_symbols"
    active_subscription_set = set(active_subscription_symbols)
    market_ws_symbols_ready = len([symbol for symbol in market_ws_symbols if symbol in active_subscription_set])
    target_subscription_limit = _safe_method_int(service, "_get_target_subscription_limit", 80, errors)
    total_subscription_limit = _safe_method_int(service, "_get_total_subscription_limit", target_subscription_limit, errors)
    if total_subscription_limit <= 0:
        total_subscription_limit = target_subscription_limit
    entry_temp_subscription_reserve = _safe_method_int(service, "_get_entry_temp_subscription_reserve", 8, errors)
    trade_budget_method = getattr(service, "_get_trade_subscription_budget", None)
    try:
        trade_subscription_limit = trade_budget_method() if callable(trade_budget_method) else target_subscription_limit
    except Exception as exc:
        logger.warning("IBKR service trade subscription budget failed: %s", exc, exc_info=True)
        errors.append({"section": "_get_trade_subscription_budget", "error": str(exc)})
        trade_subscription_limit = target_subscription_limit
    if trade_subscription_limit is None:
        trade_subscription_limit = target_subscription_limit or total_subscription_limit
    trade_subscription_limit = max(0, int(trade_subscription_limit or 0))
    mode_payload = broker_mode_payload()
    status = {
        "gateway_control_available": True,
        "starting": starting,
        "startup_complete": bool(running and not starting),
        "runtime_phase": _safe_method_text(service, "_runtime_phase_label", errors),
        **mode_payload,
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
        "host_resources": _safe_method_payload(service, "_host_resources_snapshot", errors),
        "resource_governor": _safe_method_payload(service, "_resource_governor_snapshot", errors),
        "watchlist_idle_topup": _safe_method_payload(service, "_watchlist_idle_topup_status", errors),
        "order_tracker": _safe_component_status(service, "order_tracker", errors),
        "signal_router": _safe_component_status(service, "signal_router", errors),
        "signal_processor": _safe_component_status(service, "signal_processor", errors),
        "market_universe": {
            "market_date": str(getattr(service, "_current_market_date", "") or ""),
            "active_target_date": str(getattr(service, "_active_target_date", "") or ""),
            "watchlist_trade_count": len(watchlist_trade_symbols),
            "trade_universe_ready": bool(active_trade_symbol_set),
            "trade_universe_status": trade_universe_status,
            "trade_universe_reason": trade_universe_status,
            "no_active_targets": no_active_targets,
            "inactive_trade_symbols_total": len(inactive_trade_symbols),
            "inactive_trade_symbols_sample": inactive_trade_symbols[:25],
            "active_target_count": len(active_trade_symbols),
            "candidate_target_count": len(candidate_target_symbols),
            "candidate_target_symbols": candidate_target_symbols[:25],
            "execution_eligible_target_count": len(execution_eligible_symbols),
            "execution_eligible_symbols": execution_eligible_symbols,
            "observe_target_count": len(observe_target_symbols),
            "observe_target_symbols": observe_target_symbols[:25],
            "no_execution_eligible_targets": no_execution_eligible_targets,
            "active_subscription_count": len(active_subscription_symbols),
            "active_trade_symbol_count": len(active_trade_symbols),
            "active_monitor_symbol_count": market_ws_symbols_ready,
            "target_subscription_limit": target_subscription_limit,
            "total_subscription_limit": total_subscription_limit,
            "trade_subscription_limit": trade_subscription_limit,
            "trade_subscription_budget": trade_subscription_limit,
            "market_monitor_subscription_limit": len(market_ws_symbols),
            "websocket_subscription_limit": total_subscription_limit or target_subscription_limit,
            "entry_temp_subscription_reserve": entry_temp_subscription_reserve,
            "active_target_symbols": active_trade_symbols,
            "active_subscription_symbols": active_subscription_symbols,
            "active_trade_symbols": active_trade_symbols,
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
    refresh_calendar: bool = False,
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
        if signature:
            kwargs = {}
            if "refresh_auth" in signature.parameters:
                kwargs["refresh_auth"] = refresh_auth
            if "refresh_calendar" in signature.parameters:
                kwargs["refresh_calendar"] = refresh_calendar
            payload = status_fn(**kwargs) if kwargs else status_fn()
        else:
            payload = status_fn()
    except Exception as exc:
        logger.warning("IBKR service status snapshot failed: %s", exc, exc_info=True)
        minimal = _build_minimal_runtime_status(service, exc)
        return {**fallback, **minimal}

    if not isinstance(payload, dict):
        return fallback
    errors: list[dict] = []
    status = _with_resource_governor_snapshot(service, dict(payload), errors)
    if errors:
        existing_errors = status.get("status_errors")
        status_errors = list(existing_errors) if isinstance(existing_errors, list) else []
        status_errors.extend(errors)
        status["status_errors"] = status_errors
    return status


__all__ = ["get_service_status_snapshot"]
