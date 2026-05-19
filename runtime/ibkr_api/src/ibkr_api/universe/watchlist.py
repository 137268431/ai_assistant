from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.orders.values import parse_boolean, to_int, to_text
from ibkr_api.modes import request_broker_mode
from ibkr_api.storage.helpers import normalize_exchange_value
from ibkr_api.universe.maintenance import (
    WATCHLIST_ROLE_MARKET_MONITOR,
    WATCHLIST_ROLE_TRADE,
    call_universe_reconcile,
    find_record_by_id_or_filter,
    get_runtime_market_date,
    has_effective_watchlist_member,
    list_active_today_targets,
    normalize_record_environment,
    normalize_watchlist_role,
    remove_auto_watchlist_record_if_eligible,
    upsert_record,
)
from ibkr_api.universe.watchlist_eligibility import build_watchlist_eligibility_response
from ibkr_compute.core.broker_mode import resolve_data_environment

RequestJsonRequest = Callable[..., dict[str, Any]]
TimeStrings = Callable[[], dict[str, str]]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]


def _payload_data_environment(payload: dict[str, Any]) -> str:
    return resolve_data_environment(
        payload.get("market_data_mode") or payload.get("data_environment") or payload.get("environment")
    )


def build_watchlist_upsert_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    runtime_environment = request_broker_mode(payload)
    data_environment = _payload_data_environment(payload)
    record_environment = normalize_record_environment(
        payload.get("scope") or payload.get("target_environment") or payload.get("data_environment") or data_environment,
        runtime_environment=data_environment,
    )
    symbol = to_text(payload.get("symbol")).upper()
    if not symbol:
        return {"ok": False, "error": "Missing symbol", "source": "ibkr-api"}, 400

    times = time_strings() or {}
    filter_expr = (
        f'symbol = "{escape_filter_string(symbol)}" && '
        f'environment = "{escape_filter_string(record_environment)}"'
    )
    existing = None
    try:
        existing = pb.get_first_record("watchlist", filter=filter_expr)
    except Exception:
        existing = None
    existing_row = dict(existing or {})

    note = to_text(payload.get("note"))
    exchange = normalize_exchange_value(payload.get("exchange"))
    industry = to_text(
        payload.get("industry")
        or payload.get("asset_class")
        or "/".join(payload.get("sec_types") or [])
        or payload.get("description")
    )
    requested_bar_time_ms = to_int(payload.get("bar_time_ms"), 0)
    bar_time_ms = requested_bar_time_ms if requested_bar_time_ms > 0 else int(time.time() * 1000)
    us_time = to_text(payload.get("us_time") or times.get("us"))
    cn_time = to_text(payload.get("cn_time") or times.get("cn"))
    source = to_text(payload.get("source") or "manual_page").lower() or "manual_page"
    symbol_role = normalize_watchlist_role(payload.get("symbol_role") or payload.get("role") or existing_row.get("symbol_role"))
    manual_member = parse_boolean(payload.get("manual_member"), existing_row.get("manual_member") if existing_row else True)
    force_trade_add = parse_boolean(payload.get("force_trade_add"), False)
    should_check_by_default = symbol_role == WATCHLIST_ROLE_TRADE and source in {"manual_page", "manual_page_add", "manual_page_edit"}
    check_trade_eligibility = parse_boolean(payload.get("check_trade_eligibility"), should_check_by_default)
    eligibility_payload: dict[str, Any] = {}
    eligibility_warning = to_text(payload.get("eligibility_warning"))

    if symbol_role == WATCHLIST_ROLE_TRADE and check_trade_eligibility and not force_trade_add:
        try:
            eligibility_result, _ = build_watchlist_eligibility_response(
                pb,
                payload={
                    "data_environment": data_environment,
                    "symbols": [symbol],
                    "window_trading_days": payload.get("window_trading_days"),
                },
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
            )
            eligibility_items = eligibility_result.get("items") or []
            eligibility_payload = dict(eligibility_items[0]) if eligibility_items else {}
            if eligibility_payload and eligibility_payload.get("status") != "pass":
                eligibility_warning = to_text(eligibility_payload.get("message"))
        except Exception as exc:
            eligibility_payload = {"status": "check_failed", "error": str(exc)}
            eligibility_warning = f"{symbol} 日内交易适配性校验失败，请谨慎加入 trade。"

    compare_data = {
        "symbol": symbol,
        "environment": record_environment,
        "exchange": exchange,
        "industry": industry,
        "note": note,
        "symbol_role": symbol_role,
        "manual_member": manual_member,
    }
    data = {
        **compare_data,
        "created_us": to_text(existing_row.get("created_us") or payload.get("created_us") or us_time),
        "created_cn": to_text(existing_row.get("created_cn") or payload.get("created_cn") or cn_time),
        "updated_us": to_text(payload.get("updated_us") or us_time),
        "updated_cn": to_text(payload.get("updated_cn") or cn_time),
        "us_time": us_time,
        "cn_time": cn_time,
        "bar_time_ms": bar_time_ms,
    }

    try:
        result = upsert_record(
            pb,
            "watchlist",
            filter_expr=filter_expr,
            data=data,
            compare_fields=list(compare_data.keys()),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "source": "ibkr-api"}, 500

    runtime_reconcile: dict[str, Any] | None
    try:
        reconcile = call_universe_reconcile(
            data_environment,
            {
                "source": source,
                "reason": "watchlist_upsert",
                "broker_mode": runtime_environment,
                "prime_symbols": [symbol],
                "emit_signals": False,
            },
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        runtime_reconcile = {
            **dict(reconcile.get("payload") or {}),
            "proxy_upstream": reconcile.get("upstream") or "",
            "status_code": int(reconcile.get("statusCode") or 200),
        }
    except Exception as exc:
        runtime_reconcile = {"ok": False, "error": str(exc)}

    return (
        {
            "ok": True,
            "action": result.get("action") or "updated",
            "id": to_text((result.get("record") or {}).get("id")),
            "symbol": symbol,
            "environment": record_environment,
            "broker_mode": runtime_environment,
            "data_environment": data_environment,
            "symbol_role": symbol_role,
            "manual_member": manual_member,
            "force_trade_add": force_trade_add,
            "eligibility": eligibility_payload,
            "warning": eligibility_warning,
            "runtime_reconcile": runtime_reconcile,
            "source": "ibkr-api",
        },
        200,
    )


def build_watchlist_remove_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    runtime_environment = request_broker_mode(payload)
    data_environment = _payload_data_environment(payload)
    record_environment = normalize_record_environment(
        payload.get("scope") or payload.get("target_environment") or payload.get("data_environment") or data_environment,
        runtime_environment=data_environment,
    )
    record_id = to_text(payload.get("record_id") or payload.get("id"))
    symbol_hint = to_text(payload.get("symbol")).upper()
    remove_current_day_targets = parse_boolean(payload.get("remove_current_day_targets"), True)
    market_date = to_text(payload.get("market_date")) or get_runtime_market_date(
        data_environment,
        request_json_request=request_json_request,
        compute_base_url=compute_base_url,
        time_strings=time_strings,
    )
    filter_expr = ""
    if symbol_hint:
        filter_expr = (
            f'symbol = "{escape_filter_string(symbol_hint)}" && '
            f'environment = "{escape_filter_string(record_environment)}"'
        )
    record = find_record_by_id_or_filter(
        pb,
        "watchlist",
        record_id=record_id,
        filter_expr=filter_expr,
        escape_filter_string=escape_filter_string,
    )
    if not record:
        return {"ok": False, "error": "watchlist_record_not_found", "source": "ibkr-api"}, 404

    symbol = to_text(record.get("symbol") or symbol_hint).upper()
    symbol_role = normalize_watchlist_role(record.get("symbol_role"))
    actual_record_id = to_text(record.get("id"))
    pb.delete_record("watchlist", actual_record_id)

    removed_target_count = 0
    if remove_current_day_targets and symbol and symbol_role != WATCHLIST_ROLE_MARKET_MONITOR:
        target_rows = list_active_today_targets(
            pb,
            symbol,
            record_environment,
            market_date,
            escape_filter_string=escape_filter_string,
        )
        for row in target_rows:
            row_id = to_text((row or {}).get("id"))
            if not row_id:
                continue
            try:
                pb.delete_record("ibkr_targets", row_id)
                removed_target_count += 1
            except Exception:
                pass

    keep_watchlist = has_effective_watchlist_member(
        pb,
        symbol,
        record_environment,
        escape_filter_string=escape_filter_string,
    )
    keep_targets = bool(
        list_active_today_targets(
            pb,
            symbol,
            record_environment,
            market_date,
            escape_filter_string=escape_filter_string,
        )
    )

    runtime_reconcile: dict[str, Any] | None = None
    if symbol and not keep_watchlist and not keep_targets:
        try:
            reconcile = call_universe_reconcile(
                data_environment,
                {
                    "source": to_text(payload.get("source") or "manual_page_remove").lower() or "manual_page_remove",
                    "reason": "watchlist_remove",
                    "broker_mode": runtime_environment,
                    "cleanup_symbols": [symbol],
                },
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
            )
            runtime_reconcile = {
                **dict(reconcile.get("payload") or {}),
                "proxy_upstream": reconcile.get("upstream") or "",
                "status_code": int(reconcile.get("statusCode") or 200),
            }
        except Exception as exc:
            runtime_reconcile = {"ok": False, "error": str(exc)}

    return (
        {
            "ok": True,
            "action": "deleted",
            "id": actual_record_id,
            "symbol": symbol,
            "environment": record_environment,
            "broker_mode": runtime_environment,
            "data_environment": data_environment,
            "removed_target_count": removed_target_count,
            "runtime_reconcile": runtime_reconcile,
            "source": "ibkr-api",
        },
        200,
    )


__all__ = [
    "build_watchlist_eligibility_response",
    "build_watchlist_remove_response",
    "build_watchlist_upsert_response",
]
