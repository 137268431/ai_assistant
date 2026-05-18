from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode
from ibkr_api.orders.reconcile_support import (
    append_reconciled_detail,
    build_repair_context,
    load_detail_records,
    load_order_records,
)
from ibkr_api.orders.values import parse_boolean, parse_integer, to_text


def build_orders_reconcile_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    only_missing = parse_boolean(payload.get("only_missing"), True)
    dry_run = parse_boolean(payload.get("dry_run"), False)
    suppress_notification = parse_boolean(payload.get("suppress_notification"), True)
    source = to_text(payload.get("source")) or "orders/reconcile"
    repair_source = to_text(payload.get("repair_source")) or source
    detail_limit = parse_integer(payload.get("detail_limit"), 50, 1, 200)

    try:
        order_rows = load_order_records(
            pb,
            payload=payload,
            environment=environment,
            escape_filter_string=escape_filter_string,
        )
        repaired = 0
        skipped = 0
        failed = 0
        results: list[dict[str, Any]] = []

        for order_row in order_rows:
            unique_id = to_text(order_row.get("unique_id"))
            symbol = to_text(order_row.get("symbol"))
            if not unique_id or not symbol:
                skipped += 1
                results.append(
                    {
                        "signal_id": to_text(order_row.get("signal_id")),
                        "unique_id": unique_id,
                        "symbol": symbol,
                        "status": "skipped_invalid_source",
                    }
                )
                continue

            detail_rows = load_detail_records(
                pb,
                unique_id=unique_id,
                environment=environment,
                detail_limit=detail_limit,
                escape_filter_string=escape_filter_string,
            )
            context = build_repair_context(
                order_row,
                detail_rows,
                environment=environment,
                suppress_notification=suppress_notification,
                only_missing=only_missing,
            )
            latest_detail_matches = context["latest_detail_status"] == context["status"]

            if only_missing and latest_detail_matches:
                skipped += 1
                results.append(
                    {
                        "signal_id": context["signal_id"],
                        "unique_id": context["unique_id"],
                        "symbol": context["symbol"],
                        "status": "skipped_current_status_detail_exists",
                        "detail_record_id": context["latest_detail_id"],
                        "latest_detail_status": context["latest_detail_status"],
                    }
                )
                continue

            if dry_run:
                repaired += 1
                results.append(
                    {
                        "signal_id": context["signal_id"],
                        "unique_id": context["unique_id"],
                        "symbol": context["symbol"],
                        "status": "dry_run_ready",
                        "payload": context,
                    }
                )
                continue

            try:
                detail_row = append_reconciled_detail(
                    pb,
                    order_row=order_row,
                    context=context,
                    source=source,
                    repair_source=repair_source,
                )
                repaired += 1
                results.append(
                    {
                        "signal_id": context["signal_id"],
                        "unique_id": context["unique_id"],
                        "symbol": context["symbol"],
                        "status": "repaired_detail",
                        "detail_record_id": to_text(detail_row.get("id")),
                        "latest_detail_status": context["latest_detail_status"],
                    }
                )
            except Exception as exc:
                failed += 1
                results.append(
                    {
                        "signal_id": context["signal_id"],
                        "unique_id": context["unique_id"],
                        "symbol": context["symbol"],
                        "status": "failed_exception",
                        "latest_detail_status": context["latest_detail_status"],
                        "error": str(exc),
                    }
                )

        return (
            {
                "success": True,
                "environment": environment,
                "dry_run": dry_run,
                "only_missing": only_missing,
                "suppress_notification": suppress_notification,
                "summary": {
                    "scanned": len(order_rows),
                    "repaired": repaired,
                    "skipped": skipped,
                    "failed": failed,
                },
                "results": results,
                "source": "ibkr-api",
            },
            200,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc), "source": "ibkr-api"}, 500
