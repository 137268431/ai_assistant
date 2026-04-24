from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, to_int, to_text
from ibkr_api.account.snapshot_live_orders import build_managed_order_context, normalize_live_order
from ibkr_api.account.snapshot_relations import build_relation_context
from ibkr_api.account.snapshot_shared import (
    _clone_string_list,
    _is_closed_order_status,
    canonical_order_status,
    normalize_order_record,
)


NormalizeEnvironment = Callable[[Any, str], str]
RequestJsonRequest = Callable[..., dict[str, Any]]


def enrich_account_snapshot(pb: Any, payload: dict[str, Any], environment: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    positions = list(payload.get("positions") or [])
    broker_orders = list(payload.get("orders") or [])
    live_open_orders = list(payload.get("live_open_orders") or []) or [item for item in broker_orders if not _is_closed_order_status((item or {}).get("status_key") or (item or {}).get("status"))]
    managed_context = build_managed_order_context(pb, environment, live_open_orders)
    symbols = [to_text((item or {}).get("symbol")).upper() for item in positions]
    symbols.extend(to_text((group or {}).get("symbol")).upper() for group in managed_context.get("active_groups", []))
    symbols.extend(to_text((group or {}).get("symbol")).upper() for group in managed_context.get("live_order_groups", []))
    symbols = [symbol for symbol in symbols if symbol]
    relation_context = build_relation_context(pb, environment, symbols, managed_context.get("order_records", []))
    system_managed_count = 0
    external_count = 0
    flat_count = 0
    next_positions = []
    for position in positions:
        item = ensure_object(position)
        symbol = to_text(item.get("symbol")).upper()
        quantity = float(item.get("quantity") or 0.0)
        active_group = ensure_object(relation_context["activeGroupBySymbol"].get(symbol))
        related_signal = ensure_object(relation_context["signalMap"].get(to_text(active_group.get("signal_id")))) if active_group.get("signal_id") else {}
        if quantity == 0:
            flat_count += 1
            relation = {
                "status": "flat_legacy",
                "reason": "gateway_flat_position_record",
                "signal_id": to_text(active_group.get("signal_id")),
                "signal_status": to_text(related_signal.get("status")),
                "trade_group_id": to_text(active_group.get("trade_group_id")),
                "entry_order_unique_id": to_text(active_group.get("entry_order_unique_id")),
                "last_order_status": to_text(active_group.get("latest_order_status")),
                "order_updated": to_text(active_group.get("latest_updated")),
            }
        elif active_group and active_group.get("has_open_exposure"):
            system_managed_count += 1
            relation = {
                "status": "system_managed",
                "reason": "matched_open_trade_group",
                "signal_id": to_text(active_group.get("signal_id")),
                "signal_status": to_text(related_signal.get("status")),
                "signal_note": to_text(related_signal.get("note")),
                "trade_group_id": to_text(active_group.get("trade_group_id")),
                "entry_order_unique_id": to_text(active_group.get("entry_order_unique_id")),
                "last_order_status": to_text(active_group.get("latest_order_status")),
                "order_updated": to_text(active_group.get("latest_updated")),
                "order_count": len(active_group.get("orders") or []),
            }
        else:
            external_count += 1
            relation = {
                "status": "external_position",
                "reason": "no_system_order_link",
                "signal_id": "",
                "signal_status": "",
                "trade_group_id": "",
                "entry_order_unique_id": "",
                "last_order_status": "",
                "order_updated": "",
            }
        next_positions.append({**item, "relation": relation})
    payload["positions"] = next_positions
    coverage = ensure_object(payload.get("live_order_coverage"))
    recovered_open_orders = to_int(ensure_object(payload.get("counts")).get("recovered_open_orders"), 0) or len([item for item in managed_context.get("live_orders", []) if to_text(item.get("recovery_source")) == "status_recovered"])
    payload["live_open_orders"] = managed_context.get("live_orders", [])
    payload["live_order_groups"] = managed_context.get("live_order_groups", [])
    payload["matched_order_groups"] = managed_context.get("matched_order_groups", [])
    payload["broker_only_order_groups"] = managed_context.get("broker_only_order_groups", [])
    payload["managed_order_groups"] = managed_context.get("active_groups", [])
    payload["pb_only_order_groups"] = managed_context.get("pb_only_active_groups", [])
    counts = ensure_object(payload.get("counts"))
    payload["counts"] = {
        **counts,
        "open_orders": len(payload.get("live_open_orders") or []),
        "cancelable_orders": to_int(managed_context.get("cancelable_order_count"), 0),
        "editable_orders": to_int(managed_context.get("editable_order_count"), 0),
        "outside_rth_orders": to_int(managed_context.get("outside_rth_order_count"), 0),
        "recovered_open_orders": recovered_open_orders,
        "broker_matched_orders": to_int(managed_context.get("matched_live_order_count"), 0),
        "broker_only_open_orders": to_int(managed_context.get("broker_only_live_order_count"), 0),
        "system_managed_positions": system_managed_count,
        "external_positions": external_count,
        "flat_positions": flat_count,
        "pb_active_order_groups": len(managed_context.get("active_groups", [])),
        "pb_active_orders": to_int(managed_context.get("active_order_count"), 0),
        "pb_only_active_order_groups": len(managed_context.get("pb_only_active_groups", [])),
        "pb_shadow_groups": len(managed_context.get("pb_only_active_groups", [])),
        "missing_client_order_id_orders": to_int(managed_context.get("missing_client_order_id_count"), 0),
        "status_mismatch_orders": to_int(managed_context.get("status_mismatch_count"), 0),
        "quantity_mismatch_orders": to_int(managed_context.get("quantity_mismatch_count"), 0),
        "filled_qty_mismatch_orders": to_int(managed_context.get("filled_qty_mismatch_count"), 0),
    }
    payload["order_reconciliation"] = {
        "broker_total_orders": len(broker_orders),
        "broker_open_orders": len(payload.get("live_open_orders") or []),
        "broker_matched_orders": to_int(managed_context.get("matched_live_order_count"), 0),
        "broker_only_open_orders": to_int(managed_context.get("broker_only_live_order_count"), 0),
        "broker_matched_groups": len(managed_context.get("matched_order_groups", [])),
        "broker_only_groups": len(managed_context.get("broker_only_order_groups", [])),
        "pb_active_order_groups": len(managed_context.get("active_groups", [])),
        "pb_active_orders": to_int(managed_context.get("active_order_count"), 0),
        "pb_only_active_order_groups": len(managed_context.get("pb_only_active_groups", [])),
        "pb_shadow_groups": len(managed_context.get("pb_only_active_groups", [])),
        "broker_only_orders": managed_context.get("broker_only_orders", []),
        "coverage_state": to_text(coverage.get("coverage_state")) or "complete",
        "bulk_open_count": to_int(coverage.get("bulk_open_count"), 0),
        "recovered_open_orders": recovered_open_orders,
        "unresolved_seed_count": to_int(coverage.get("unresolved_seed_count"), 0),
        "unresolved_order_ids": _clone_string_list(coverage.get("unresolved_order_ids")),
        "cancelable_orders": to_int(managed_context.get("cancelable_order_count"), 0),
        "editable_orders": to_int(managed_context.get("editable_order_count"), 0),
        "outside_rth_orders": to_int(managed_context.get("outside_rth_order_count"), 0),
        "missing_client_order_id_orders": to_int(managed_context.get("missing_client_order_id_count"), 0),
        "status_mismatch_orders": to_int(managed_context.get("status_mismatch_count"), 0),
        "quantity_mismatch_orders": to_int(managed_context.get("quantity_mismatch_count"), 0),
        "filled_qty_mismatch_orders": to_int(managed_context.get("filled_qty_mismatch_count"), 0),
    }
    return payload


def build_account_snapshot_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    result = request_json_request(
        "GET",
        runtime_base_url,
        "/ibkr/account",
        params=[("environment", environment)],
        timeout=20.0,
    )
    status_code = int(result.get("status_code") or 200)
    upstream_payload = ensure_object(result.get("payload"))
    selected_upstream = to_text(result.get("target_url")) or f"{runtime_base_url.rstrip('/')}/ibkr/account"
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
    enriched = enrich_account_snapshot(pb, dict(upstream_payload), environment)
    enriched["proxy_source"] = "ibkr-api"
    enriched["proxy_route"] = "/api/custom/ibkr/account_snapshot"
    enriched["proxy_upstream"] = selected_upstream
    enriched["source"] = "ibkr-api"
    return enriched, status_code if status_code >= 400 else 200


__all__ = [
    "build_account_snapshot_response",
    "build_managed_order_context",
    "build_relation_context",
    "canonical_order_status",
    "enrich_account_snapshot",
    "normalize_live_order",
    "normalize_order_record",
]
