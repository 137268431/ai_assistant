from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, parse_boolean, to_float, to_int, to_text
from ibkr_api.universe.maintenance import (
    call_universe_reconcile,
    ensure_target_watchlist_record,
    find_effective_watchlist_record,
    find_record_by_id_or_filter,
    get_runtime_market_date,
    has_effective_watchlist_member,
    is_default_market_context_symbol,
    list_active_today_targets,
    normalize_direction_bias,
    normalize_target_status,
    normalize_watchlist_role,
    parse_json_object,
    remove_auto_watchlist_record_if_eligible,
    upsert_record,
    WATCHLIST_ROLE_MARKET_MONITOR,
)

RequestJsonRequest = Callable[..., dict[str, Any]]
TimeStrings = Callable[[], dict[str, str]]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]


def _normalize_extra(value: Any) -> dict[str, Any]:
    return parse_json_object(value)


def _pick_score(item: dict[str, Any], existing: dict[str, Any] | None = None) -> float:
    candidates = [
        to_float(item.get("target_score")),
        to_float(item.get("score")),
        to_float(item.get("tradability_score")),
        to_float((existing or {}).get("score")),
    ]
    for candidate in candidates:
        if candidate is not None and candidate > 0:
            return float(candidate)
    return 0.0


def build_target_upsert_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    symbol = to_text(payload.get("symbol")).upper()
    times = time_strings() or {}
    target_date = to_text(payload.get("date") or times.get("date"))
    source = to_text(payload.get("source") or "manual_page_add").lower() or "manual_page_add"
    current_market_date = get_runtime_market_date(
        environment,
        request_json_request=request_json_request,
        compute_base_url=compute_base_url,
        time_strings=time_strings,
    )

    if not symbol or not target_date:
        return {"ok": False, "error": "Missing symbol or date", "source": "ibkr-api"}, 400
    if source == "manual_page_add" and target_date != current_market_date:
        return (
            {
                "ok": False,
                "error": "manual_target_only_current_market_date",
                "current_market_date": current_market_date,
                "date": target_date,
                "source": "ibkr-api",
            },
            400,
        )

    exchange = to_text(payload.get("exchange")).upper()
    direction_bias = normalize_direction_bias(payload.get("direction_bias"), default="neutral")
    score = float(to_float(payload.get("score")) or 0.0)
    scan_reason = to_text(payload.get("scan_reason"))
    status = normalize_target_status(payload.get("status"), default="candidate")
    extra = _normalize_extra(payload.get("extra"))
    effective_watchlist_record = find_effective_watchlist_record(
        pb,
        symbol,
        environment,
        escape_filter_string=escape_filter_string,
    )
    effective_role = normalize_watchlist_role((effective_watchlist_record or {}).get("symbol_role"))
    is_market_context = is_default_market_context_symbol(symbol) or effective_role == WATCHLIST_ROLE_MARKET_MONITOR
    force_market_context_target = parse_boolean(payload.get("force_monitor_target") or payload.get("force_market_context_target"), False)
    if status in {"candidate", "active"} and is_market_context and not (force_market_context_target and environment != "live"):
        return (
            {
                "ok": False,
                "error": "market_context_symbol_not_trade_target",
                "symbol": symbol,
                "symbol_role": WATCHLIST_ROLE_MARKET_MONITOR,
                "requested_status": status,
                "environment": environment,
                "current_market_date": current_market_date,
                "message": "QQQ/SPY/VIX and market_monitor symbols are market context only and cannot be active trade targets.",
                "source": "ibkr-api",
            },
            400,
        )
    requested_bar_time_ms = to_int(payload.get("bar_time_ms"), 0)
    bar_time_ms = requested_bar_time_ms if requested_bar_time_ms > 0 else int(time.time() * 1000)
    us_time = to_text(payload.get("us_time") or times.get("us"))
    cn_time = to_text(payload.get("cn_time") or times.get("cn"))

    filter_expr = (
        f'symbol = "{escape_filter_string(symbol)}" && '
        f'date = "{escape_filter_string(target_date)}" && '
        f'environment = "{escape_filter_string(environment)}"'
    )
    compare_data = {
        "symbol": symbol,
        "environment": environment,
        "exchange": exchange,
        "date": target_date,
        "direction_bias": direction_bias,
        "score": score,
        "scan_reason": scan_reason,
        "status": status,
        "extra": extra,
    }
    try:
        result = upsert_record(
            pb,
            "ibkr_targets",
            filter_expr=filter_expr,
            data={
                **compare_data,
                "us_time": us_time,
                "cn_time": cn_time,
                "bar_time_ms": bar_time_ms,
            },
            compare_fields=list(compare_data.keys()),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "source": "ibkr-api"}, 500

    watchlist_sync: dict[str, Any] | None = None
    runtime_reconcile: dict[str, Any] | None = None
    if target_date == current_market_date and status in {"candidate", "active"}:
        watchlist_sync = ensure_target_watchlist_record(
            pb,
            symbol=symbol,
            environment=environment,
            exchange=exchange,
            industry=to_text(extra.get("industry") or extra.get("asset_class") or extra.get("description")),
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
        )
        try:
            reconcile = call_universe_reconcile(
                environment,
                {
                    "source": source,
                    "reason": "target_upsert",
                    "prime_symbols": [symbol],
                    "emit_signals": True,
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
    elif target_date == current_market_date and status == "removed":
        watchlist_sync = remove_auto_watchlist_record_if_eligible(
            pb,
            symbol,
            environment,
            current_market_date,
            escape_filter_string=escape_filter_string,
        )
        if not has_effective_watchlist_member(
            pb,
            symbol,
            environment,
            escape_filter_string=escape_filter_string,
        ) and not list_active_today_targets(
            pb,
            symbol,
            environment,
            current_market_date,
            escape_filter_string=escape_filter_string,
        ):
            try:
                reconcile = call_universe_reconcile(
                    environment,
                    {
                        "source": source or "manual_page_edit",
                        "reason": "target_mark_removed",
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
            "action": result.get("action") or "updated",
            "id": to_text((result.get("record") or {}).get("id")),
            "symbol": symbol,
            "date": target_date,
            "environment": environment,
            "current_market_date": current_market_date,
            "watchlist_sync": watchlist_sync,
            "runtime_reconcile": runtime_reconcile,
            "source": "ibkr-api",
        },
        200,
    )


def build_target_remove_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    fallback_environment = normalize_environment(payload.get("environment"), "live")
    record_id = to_text(payload.get("record_id") or payload.get("id"))
    symbol_hint = to_text(payload.get("symbol")).upper()
    filter_expr = ""
    if symbol_hint:
        filter_expr = (
            f'symbol = "{escape_filter_string(symbol_hint)}" && '
            f'environment = "{escape_filter_string(fallback_environment)}"'
        )
    record = find_record_by_id_or_filter(
        pb,
        "ibkr_targets",
        record_id=record_id,
        filter_expr=filter_expr,
        escape_filter_string=escape_filter_string,
    )
    if not record:
        return {"ok": False, "error": "target_record_not_found", "source": "ibkr-api"}, 404

    environment = normalize_environment(record.get("environment") or fallback_environment, fallback_environment)
    symbol = to_text(record.get("symbol") or symbol_hint).upper()
    current_market_date = get_runtime_market_date(
        environment,
        request_json_request=request_json_request,
        compute_base_url=compute_base_url,
        time_strings=time_strings,
    )
    actual_record_id = to_text(record.get("id"))
    pb.delete_record("ibkr_targets", actual_record_id)

    auto_watchlist = remove_auto_watchlist_record_if_eligible(
        pb,
        symbol,
        environment,
        current_market_date,
        escape_filter_string=escape_filter_string,
    )
    keep_watchlist = has_effective_watchlist_member(
        pb,
        symbol,
        environment,
        escape_filter_string=escape_filter_string,
    )
    keep_targets = bool(
        list_active_today_targets(
            pb,
            symbol,
            environment,
            current_market_date,
            escape_filter_string=escape_filter_string,
        )
    )

    runtime_reconcile: dict[str, Any] | None = None
    if symbol and not keep_watchlist and not keep_targets:
        try:
            reconcile = call_universe_reconcile(
                environment,
                {
                    "source": to_text(payload.get("source") or "manual_page_remove").lower() or "manual_page_remove",
                    "reason": "target_remove",
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
            "environment": environment,
            "current_market_date": current_market_date,
            "watchlist_sync": auto_watchlist,
            "runtime_reconcile": runtime_reconcile,
            "source": "ibkr-api",
        },
        200,
    )


def build_screener_targets_upsert_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, Any], int]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    environment = normalize_environment(payload.get("environment"), "live")
    market_date = to_text(payload.get("market_date") or payload.get("date"))
    if not market_date:
        return {"ok": False, "error": "Missing market_date", "source": "ibkr-api"}, 400
    if not items:
        return {"ok": False, "error": "Empty screener items array", "source": "ibkr-api"}, 400

    created = 0
    updated = 0
    skipped = 0
    errors = 0
    symbols: list[str] = []

    for raw_item in items:
        item = dict(raw_item or {})
        symbol = to_text(item.get("symbol")).upper()
        if not symbol:
            errors += 1
            continue

        filter_expr = (
            f'symbol = "{escape_filter_string(symbol)}" && '
            f'date = "{escape_filter_string(market_date)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        )
        existing = None
        try:
            existing = pb.get_first_record("ibkr_targets", filter=filter_expr)
        except Exception:
            existing = None
        existing_row = dict(existing or {})
        existing_extra = _normalize_extra(existing_row.get("extra"))
        item_extra = _normalize_extra(item.get("extra"))
        score = _pick_score(item, existing_row)
        merged_extra = {
            **existing_extra,
            **item_extra,
            "source": "ibkr_screener",
            "screener_snapshot": {
                "symbol": symbol,
                "price": float(to_float(item.get("price")) or 0.0),
                "atr_pct": float(to_float(item.get("atr_pct")) or 0.0),
                "avg_10d_volume": float(to_float(item.get("avg_10d_volume")) or 0.0),
                "premarket_volume": float(to_float(item.get("premarket_volume")) or 0.0),
                "today_volume": float(to_float(item.get("today_volume")) or 0.0),
                "tradability_score": float(to_float(item.get("tradability_score")) or 0.0),
                "operable_reasons": list(item.get("operable_reasons") or []),
                "freshness_min": int(to_int(item.get("freshness_min"), 0)),
                "is_operable": bool(item.get("is_operable")),
            },
            "pushed_from": "ibkr_screener_page",
            "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "score_source": (
                "target_score"
                if (to_float(item.get("target_score")) or 0) > 0
                else ("score" if (to_float(item.get("score")) or 0) > 0 else "tradability_score")
            ),
            "market_date": market_date,
            "environment": environment,
        }
        row_payload = {
            "symbol": symbol,
            "environment": environment,
            "exchange": to_text(item.get("exchange") or existing_row.get("exchange")).upper(),
            "date": market_date,
            "direction_bias": normalize_direction_bias(item.get("direction_bias") or existing_row.get("direction_bias"), default="neutral"),
            "score": score,
            "scan_reason": to_text(item.get("scan_reason") or existing_row.get("scan_reason") or "manual_screener_selection"),
            "status": normalize_target_status(item.get("target_status") or item.get("status") or existing_row.get("status"), default="candidate"),
            "us_time": to_text(item.get("latest_us_time") or existing_row.get("us_time")),
            "cn_time": to_text(item.get("latest_cn_time") or existing_row.get("cn_time")),
            "bar_time_ms": int(
                to_int(
                    item.get("latest_intraday_bar_time_ms")
                    or item.get("latest_bar_time_ms")
                    or existing_row.get("bar_time_ms"),
                    0,
                )
            ),
            "extra": merged_extra,
        }
        try:
            result = upsert_record(pb, "ibkr_targets", filter_expr=filter_expr, data=row_payload)
            symbols.append(symbol)
            action = to_text(result.get("action"))
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception:
            errors += 1

    return (
        {
            "ok": errors == 0,
            "market_date": market_date,
            "environment": environment,
            "received": len(items),
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "symbols": symbols,
            "source": "ibkr-api",
        },
        200,
    )


__all__ = [
    "build_screener_targets_upsert_response",
    "build_target_remove_response",
    "build_target_upsert_response",
]
