from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ibkr_compute.api.ops.bar_truth_compare import build_bar_truth_compare_payload
from ibkr_compute.api.ops.data_quality_truth import (
    TRUTH_AUDIT_INTERVAL,
    build_truth_audit_row,
    build_truth_audit_summary,
    resolve_truth_audit_window,
)
from ibkr_compute.market.pocketbase_sqlite import (
    delete_rows,
    open_pb_sqlite,
    pb_json_dumps,
    pb_now_text,
    pb_record_id,
    table_exists,
    upsert_bars,
)
from ibkr_compute.market.timeframe_utils import normalize_interval


TRUTH_REPAIR_EVENT_TABLE = "ibkr_bar_truth_repair_events"
TRUTH_REPAIR_ACTION_UPSERT = "upsert_ibkr_bar"
TRUTH_REPAIR_ACTION_REPLACE = "replace_with_ibkr_bar"
TRUTH_REPAIR_ACTION_DELETE = "delete_extra_stored_bar"
TRUTH_REPAIR_ACTIONS = {
    TRUTH_REPAIR_ACTION_UPSERT,
    TRUTH_REPAIR_ACTION_REPLACE,
    TRUTH_REPAIR_ACTION_DELETE,
}
BAR_ERROR_FIELDS = (
    "missing_stored_bar_count",
    "missing_ibkr_bar_count",
    "bar_mismatch_count",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def truth_summary_bar_error_total(summary: dict | None) -> int:
    data = summary if isinstance(summary, dict) else {}
    return sum(int_value(data.get(field), 0) for field in BAR_ERROR_FIELDS)


def truth_summary_ok(summary: dict | None) -> bool:
    data = summary if isinstance(summary, dict) else {}
    return truth_summary_bar_error_total(data) == 0 and int_value(data.get("matched_bar_count"), 0) > 0


def _copy_bar_for_write(
    bar: dict | None,
    *,
    environment: str,
    symbol: str,
    interval: str,
    operation_id: str,
    action: str,
) -> dict:
    payload = dict(bar or {})
    payload["environment"] = str(environment or "live").strip().lower() or "live"
    payload["symbol"] = str(payload.get("symbol") or symbol or "").strip().upper()
    payload["interval"] = normalize_interval(payload.get("interval") or interval or TRUTH_AUDIT_INTERVAL)
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    payload["extra"] = {
        **extra,
        "truth_repair": {
            "operation_id": operation_id,
            "action": action,
            "source": "ibkr_truth_repair",
        },
    }
    return payload


def build_truth_repair_plan(
    compare_payload: dict,
    *,
    operation_id: str,
    environment: str,
    market_date: str,
    symbol: str,
    interval: str = TRUTH_AUDIT_INTERVAL,
) -> dict:
    comparison = compare_payload.get("comparison") if isinstance(compare_payload, dict) else {}
    timeline = comparison.get("timeline") if isinstance(comparison, dict) else []
    items = []
    for row in timeline or []:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or {}).get("bar") if isinstance(row.get("status"), dict) else ""
        status = str(status or "").strip().lower()
        if status == "match":
            continue
        stored_bar = ((row.get("stored") or {}).get("bar") if isinstance(row.get("stored"), dict) else None)
        ibkr_bar = ((row.get("ibkr") or {}).get("bar") if isinstance(row.get("ibkr"), dict) else None)
        bar_diff = ((row.get("diff") or {}).get("bar") if isinstance(row.get("diff"), dict) else {})
        bar_time_ms = int_value(row.get("bar_time_ms"), 0)
        if bar_time_ms <= 0:
            continue
        action = ""
        if status == "missing_stored":
            action = TRUTH_REPAIR_ACTION_UPSERT
        elif status == "mismatch":
            action = TRUTH_REPAIR_ACTION_REPLACE
        elif status == "missing_ibkr":
            action = TRUTH_REPAIR_ACTION_DELETE
        if action not in TRUTH_REPAIR_ACTIONS:
            continue
        items.append(
            {
                "operation_id": operation_id,
                "environment": str(environment or "live").strip().lower() or "live",
                "market_date": str(market_date or "").strip(),
                "symbol": str(symbol or "").strip().upper(),
                "interval": normalize_interval(interval or TRUTH_AUDIT_INTERVAL),
                "bar_time_ms": bar_time_ms,
                "us_time": str(row.get("us_time") or ""),
                "action": action,
                "reason": status,
                "fields": list((bar_diff or {}).get("fields") or []),
                "old_bar": dict(stored_bar or {}) if isinstance(stored_bar, dict) else {},
                "new_bar": dict(ibkr_bar or {}) if isinstance(ibkr_bar, dict) else {},
                "applied": False,
                "verified": False,
                "error": "",
            }
        )

    action_counts: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "")
        action_counts[action] = action_counts.get(action, 0) + 1
    return {
        "operation_id": operation_id,
        "environment": str(environment or "live").strip().lower() or "live",
        "market_date": str(market_date or "").strip(),
        "symbol": str(symbol or "").strip().upper(),
        "interval": normalize_interval(interval or TRUTH_AUDIT_INTERVAL),
        "items": items,
        "summary": {
            "total": len(items),
            "action_counts": action_counts,
            "requires_delete_extra_bars": any(item.get("action") == TRUTH_REPAIR_ACTION_DELETE for item in items),
        },
    }


def _confirmation_status_map(compare_payload: dict | None) -> dict[int, str]:
    comparison = compare_payload.get("comparison") if isinstance(compare_payload, dict) else {}
    timeline = comparison.get("timeline") if isinstance(comparison, dict) else []
    output: dict[int, str] = {}
    for row in timeline or []:
        if not isinstance(row, dict):
            continue
        bar_time_ms = int_value(row.get("bar_time_ms"), 0)
        status = (row.get("status") or {}).get("bar") if isinstance(row.get("status"), dict) else ""
        if bar_time_ms > 0:
            output[bar_time_ms] = str(status or "").strip().lower()
    return output


def confirm_delete_actions(plan: dict, confirm_compare_payload: dict | None) -> dict:
    confirmed = _confirmation_status_map(confirm_compare_payload)
    output = {**plan, "items": [dict(item) for item in plan.get("items") or []]}
    for item in output["items"]:
        if item.get("action") != TRUTH_REPAIR_ACTION_DELETE:
            continue
        bar_time_ms = int_value(item.get("bar_time_ms"), 0)
        if confirmed.get(bar_time_ms) == "missing_ibkr":
            item["delete_confirmed"] = True
        else:
            item["delete_confirmed"] = False
            item["error"] = "delete_not_confirmed_by_refetch"
    return output


def _event_payload(item: dict, *, applied: bool, verified: bool, error: str = "") -> tuple:
    now_text = pb_now_text()
    item_applied = bool(item.get("applied", applied))
    item_verified = bool(item.get("verified", verified))
    return (
        pb_record_id(),
        str(item.get("operation_id") or ""),
        str(item.get("environment") or "live").strip().lower() or "live",
        str(item.get("market_date") or ""),
        str(item.get("symbol") or "").strip().upper(),
        normalize_interval(item.get("interval") or TRUTH_AUDIT_INTERVAL),
        int_value(item.get("bar_time_ms"), 0),
        str(item.get("us_time") or ""),
        str(item.get("action") or ""),
        str(item.get("reason") or ""),
        pb_json_dumps(item.get("fields") if isinstance(item.get("fields"), list) else []),
        pb_json_dumps(item.get("old_bar") if isinstance(item.get("old_bar"), dict) else {}),
        pb_json_dumps(item.get("new_bar") if isinstance(item.get("new_bar"), dict) else {}),
        item_applied,
        item_verified,
        str(error or item.get("error") or ""),
        now_text,
        now_text,
    )


def insert_truth_repair_events(conn, items: list[dict], *, applied: bool, verified: bool = False) -> int:
    if not items:
        return 0
    if not table_exists(conn, TRUTH_REPAIR_EVENT_TABLE):
        return 0
    conn.executemany(
        f"""
        INSERT INTO {TRUTH_REPAIR_EVENT_TABLE} (
            id, operation_id, environment, market_date, symbol, interval, bar_time_ms,
            us_time, action, reason, fields, old_bar, new_bar, applied, verified,
            error, created, updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [_event_payload(item, applied=applied, verified=verified) for item in items],
    )
    return len(items)


def mark_truth_repair_events_verified(conn, *, operation_id: str, symbol: str, market_date: str, verified: bool, error: str = "") -> int:
    if not table_exists(conn, TRUTH_REPAIR_EVENT_TABLE):
        return 0
    cursor = conn.execute(
        f"""
        UPDATE {TRUTH_REPAIR_EVENT_TABLE}
        SET verified = ?, error = ?, updated = ?
        WHERE operation_id = ? AND symbol = ? AND market_date = ? AND applied = 1
        """,
        (
            bool(verified),
            str(error or ""),
            pb_now_text(),
            str(operation_id or ""),
            str(symbol or "").strip().upper(),
            str(market_date or ""),
        ),
    )
    return int(cursor.rowcount or 0)


def _delete_extra_bar(conn, item: dict) -> int:
    runtime_environment = str(item.get("environment") or "live").strip().lower() or "live"
    old_bar = item.get("old_bar") if isinstance(item.get("old_bar"), dict) else {}
    if isinstance(old_bar, dict) and "environment" in old_bar:
        raw_environment = old_bar.get("environment")
        environment_values = [str(raw_environment if raw_environment is not None else "").strip().lower()]
    else:
        environment_values = [runtime_environment]
        if runtime_environment == "live":
            environment_values.append("")
    placeholders = ", ".join("?" for _ in environment_values)
    return delete_rows(
        conn,
        "ibkr_bars",
        where=f"environment IN ({placeholders}) AND symbol = ? AND interval = ? AND bar_time_ms = ?",
        params=(
            *environment_values,
            str(item.get("symbol") or "").strip().upper(),
            normalize_interval(item.get("interval") or TRUTH_AUDIT_INTERVAL),
            int_value(item.get("bar_time_ms"), 0),
        ),
    )


def _delete_dependent_rows(conn, *, environment: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> dict:
    if start_ms <= 0 or end_ms <= 0:
        return {"ibkr_indicators": 0, "ibkr_signals": 0}
    env = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval or TRUTH_AUDIT_INTERVAL)
    deleted = {
        "ibkr_indicators": delete_rows(
            conn,
            "ibkr_indicators",
            where="environment = ? AND symbol = ? AND interval = ? AND bar_time_ms BETWEEN ? AND ?",
            params=(env, normalized_symbol, normalized_interval, start_ms, end_ms),
        )
    }

    linked_signal_ids = [
        str(row["signal_id"] or "").strip()
        for row in conn.execute(
            """
            SELECT DISTINCT signal_id
            FROM orders
            WHERE environment = ? AND symbol = ? AND COALESCE(signal_id, '') != ''
            """,
            (env, normalized_symbol),
        ).fetchall()
        if str(row["signal_id"] or "").strip()
    ]
    signal_where = (
        "environment = ? AND symbol = ? AND interval = ? AND bar_time_ms BETWEEN ? AND ? "
        "AND LOWER(COALESCE(status, '')) != 'executed'"
    )
    signal_params: list[Any] = [env, normalized_symbol, normalized_interval, start_ms, end_ms]
    if linked_signal_ids:
        placeholders = ", ".join("?" for _ in linked_signal_ids)
        signal_where += f" AND (COALESCE(signal_id, '') = '' OR signal_id NOT IN ({placeholders}))"
        signal_params.extend(linked_signal_ids)
    deleted["ibkr_signals"] = delete_rows(conn, "ibkr_signals", where=signal_where, params=signal_params)
    return deleted


def apply_truth_repair_plan(
    plan: dict,
    *,
    environment: str,
    window_end_ms: int,
    apply_changes: bool,
    delete_extra_bars: bool,
) -> dict:
    items = [dict(item) for item in plan.get("items") or [] if isinstance(item, dict)]
    if not apply_changes:
        return {
            "applied": False,
            "planned_items": len(items),
            "applied_items": 0,
            "upserted_bars": 0,
            "deleted_bars": 0,
            "events_written": 0,
            "dependent_deleted": {},
            "errors": [],
        }

    writable_items: list[dict] = []
    delete_items: list[dict] = []
    errors: list[dict] = []
    for item in items:
        action = str(item.get("action") or "")
        bar_time_ms = int_value(item.get("bar_time_ms"), 0)
        item["applied"] = False
        if bar_time_ms <= 0:
            item["error"] = "invalid_bar_time_ms"
            errors.append({"symbol": item.get("symbol"), "bar_time_ms": item.get("bar_time_ms"), "error": item["error"]})
            continue
        if action in {TRUTH_REPAIR_ACTION_UPSERT, TRUTH_REPAIR_ACTION_REPLACE}:
            new_bar = item.get("new_bar") if isinstance(item.get("new_bar"), dict) else {}
            if int_value(new_bar.get("bar_time_ms"), 0) <= 0:
                new_bar = {**new_bar, "bar_time_ms": bar_time_ms}
                item["new_bar"] = new_bar
            item["applied"] = True
            writable_items.append(item)
        elif action == TRUTH_REPAIR_ACTION_DELETE:
            if not delete_extra_bars:
                item["error"] = "delete_extra_bars_disabled"
                errors.append({"symbol": item.get("symbol"), "bar_time_ms": item.get("bar_time_ms"), "error": item["error"]})
            elif item.get("delete_confirmed") is not True:
                item["error"] = str(item.get("error") or "delete_not_confirmed_by_refetch")
                errors.append({"symbol": item.get("symbol"), "bar_time_ms": item.get("bar_time_ms"), "error": item["error"]})
            else:
                item["applied"] = True
                delete_items.append(item)
        else:
            item["error"] = "unsupported_repair_action"
            errors.append({"symbol": item.get("symbol"), "bar_time_ms": item.get("bar_time_ms"), "error": item["error"]})

    upsert_rows = [
        _copy_bar_for_write(
            item.get("new_bar") if isinstance(item.get("new_bar"), dict) else {},
            environment=environment,
            symbol=str(item.get("symbol") or ""),
            interval=str(item.get("interval") or TRUTH_AUDIT_INTERVAL),
            operation_id=str(item.get("operation_id") or ""),
            action=str(item.get("action") or ""),
        )
        for item in writable_items
    ]
    affected_by_symbol: dict[tuple[str, str], list[int]] = {}
    for item in [*writable_items, *delete_items]:
        bar_time_ms = int_value(item.get("bar_time_ms"), 0)
        if bar_time_ms <= 0:
            continue
        key = (
            str(item.get("symbol") or "").strip().upper(),
            normalize_interval(item.get("interval") or TRUTH_AUDIT_INTERVAL),
        )
        affected_by_symbol.setdefault(key, []).append(bar_time_ms)

    with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
        with conn:
            event_table_available = table_exists(conn, TRUTH_REPAIR_EVENT_TABLE)
            events_written = insert_truth_repair_events(conn, items, applied=True, verified=False)
            upserted_bars = upsert_bars(conn, upsert_rows) if upsert_rows else 0
            deleted_bars = sum(_delete_extra_bar(conn, item) for item in delete_items)
            dependent_deleted: dict[str, dict] = {}
            for (symbol, interval), bar_times in affected_by_symbol.items():
                valid_times = [ms for ms in bar_times if ms > 0]
                if not valid_times:
                    continue
                min_ms = min(valid_times)
                deleted = _delete_dependent_rows(
                    conn,
                    environment=environment,
                    symbol=symbol,
                    interval=interval,
                    start_ms=min_ms,
                    end_ms=window_end_ms,
                )
                dependent_deleted[f"{symbol}/{interval}"] = deleted
    return {
        "applied": True,
        "planned_items": len(items),
        "applied_items": len(writable_items) + len(delete_items),
        "upserted_bars": upserted_bars,
        "deleted_bars": deleted_bars,
        "events_written": events_written,
        "event_table_available": event_table_available,
        "dependent_deleted": dependent_deleted,
        "errors": errors,
    }


def _build_truth_row_from_compare(*, environment: str, market_date: str, symbol: str, interval: str, window: dict, compare_payload: dict) -> dict:
    comparison = compare_payload.get("comparison") if isinstance(compare_payload, dict) else {}
    return build_truth_audit_row(
        environment=environment,
        market_date=market_date,
        symbol=symbol,
        interval=interval,
        window_start_ms=int_value(window.get("start_ms"), 0),
        window_end_ms=int_value(window.get("end_ms"), 0),
        comparison_summary=comparison.get("summary") or {},
        mismatch_examples=comparison.get("mismatch_examples") or [],
        source_meta={
            "audit_mode": "bar_only",
            "repair_mode": "truth_repair",
            "stored": compare_payload.get("meta", {}).get("stored") if isinstance(compare_payload.get("meta"), dict) else {},
            "ibkr": compare_payload.get("meta", {}).get("ibkr") if isinstance(compare_payload.get("meta"), dict) else {},
        },
        last_checked_at=utc_now_iso(),
    )


def build_truth_repair_payload(
    *,
    service,
    symbols: list[str],
    environment: str,
    market_date: str,
    operation_id: str,
    apply_changes: bool = False,
    delete_extra_bars: bool = True,
    confirm_refetch: bool = True,
    persist_truth: bool = True,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()]
    normalized_interval = TRUTH_AUDIT_INTERVAL
    window = resolve_truth_audit_window(market_date, normalized_interval)
    operation = str(operation_id or "").strip() or f"truth_repair:{runtime_environment}:{market_date}:{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    initial_rows: list[dict] = []
    final_rows: list[dict] = []
    plans: list[dict] = []
    apply_results: list[dict] = []
    errors: list[dict] = []
    for symbol in normalized_symbols:
        try:
            initial_compare = build_bar_truth_compare_payload(
                runtime_environment,
                symbol,
                normalized_interval,
                start_ms=int_value(window["start_ms"], 0),
                end_ms=int_value(window["end_ms"], 0),
            )
            initial_rows.append(
                _build_truth_row_from_compare(
                    environment=runtime_environment,
                    market_date=market_date,
                    symbol=symbol,
                    interval=normalized_interval,
                    window=window,
                    compare_payload=initial_compare,
                )
            )
            plan = build_truth_repair_plan(
                initial_compare,
                operation_id=operation,
                environment=runtime_environment,
                market_date=market_date,
                symbol=symbol,
                interval=normalized_interval,
            )
            if apply_changes and confirm_refetch and plan["summary"].get("requires_delete_extra_bars"):
                confirm_compare = build_bar_truth_compare_payload(
                    runtime_environment,
                    symbol,
                    normalized_interval,
                    start_ms=int_value(window["start_ms"], 0),
                    end_ms=int_value(window["end_ms"], 0),
                )
                plan = confirm_delete_actions(plan, confirm_compare)
            plans.append(plan)
            apply_result = apply_truth_repair_plan(
                plan,
                environment=runtime_environment,
                window_end_ms=int_value(window["end_ms"], 0),
                apply_changes=apply_changes,
                delete_extra_bars=delete_extra_bars,
            )
            apply_result["symbol"] = symbol
            apply_results.append(apply_result)
            for apply_error in apply_result.get("errors") or []:
                errors.append({"symbol": symbol, **(apply_error if isinstance(apply_error, dict) else {"error": str(apply_error)})})
            if apply_changes and apply_result.get("applied_items"):
                try:
                    service._trigger_realtime_compute(
                        source="truth_repair",
                        symbols=[symbol],
                        persist_signals=False,
                        intervals=[normalized_interval],
                    )
                except Exception as exc:
                    errors.append({"symbol": symbol, "error": f"compute_repair_failed:{exc}"})
            final_compare = (
                build_bar_truth_compare_payload(
                    runtime_environment,
                    symbol,
                    normalized_interval,
                    start_ms=int_value(window["start_ms"], 0),
                    end_ms=int_value(window["end_ms"], 0),
                )
                if apply_changes
                else initial_compare
            )
            final_row = _build_truth_row_from_compare(
                environment=runtime_environment,
                market_date=market_date,
                symbol=symbol,
                interval=normalized_interval,
                window=window,
                compare_payload=final_compare,
            )
            final_rows.append(final_row)
            if apply_changes and apply_result.get("applied_items"):
                final_verified = str(final_row.get("status") or "").strip().lower() == "ok" and truth_summary_ok(final_row)
                try:
                    with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
                        with conn:
                            mark_truth_repair_events_verified(
                                conn,
                                operation_id=operation,
                                symbol=symbol,
                                market_date=market_date,
                                verified=final_verified,
                                error="" if final_verified else "final_truth_audit_failed",
                            )
                except Exception as exc:
                    errors.append({"symbol": symbol, "error": f"mark_verified_failed:{exc}"})
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            error_row = build_truth_audit_row(
                environment=runtime_environment,
                market_date=market_date,
                symbol=symbol,
                interval=normalized_interval,
                window_start_ms=int_value(window["start_ms"], 0),
                window_end_ms=int_value(window["end_ms"], 0),
                comparison_summary={},
                mismatch_examples=[],
                source_meta={"audit_mode": "bar_only", "repair_mode": "truth_repair"},
                error=str(exc),
                last_checked_at=utc_now_iso(),
            )
            initial_rows.append(error_row)
            final_rows.append(dict(error_row))

    if persist_truth and final_rows:
        try:
            service.pb.upsert_bar_truth_audit_items(final_rows)
        except Exception as exc:
            errors.append({"symbol": "", "error": f"persist_truth_failed:{exc}"})

    initial_summary = build_truth_audit_summary(initial_rows, expected_symbols=normalized_symbols)
    final_summary = build_truth_audit_summary(final_rows, expected_symbols=normalized_symbols)
    final_summary["market_date"] = market_date
    final_summary["window_start_ms"] = int_value(window["start_ms"], 0)
    final_summary["window_end_ms"] = int_value(window["end_ms"], 0)
    failed_symbols = sorted(
        str(row.get("symbol") or "").strip().upper()
        for row in final_rows
        if str(row.get("status") or "").strip().lower() != "ok" or not truth_summary_ok(row)
    )
    proof_status = (
        "green"
        if (
            not errors
            and normalized_symbols
            and bool(final_summary.get("coverage_complete"))
            and len(final_rows) == len(normalized_symbols)
            and not failed_symbols
        )
        else "red"
    )
    if any(str((err or {}).get("error") or "").lower().find("gateway") >= 0 for err in errors):
        proof_status = "unavailable"

    plan_summary = {
        "total": sum(int_value((plan.get("summary") or {}).get("total"), 0) for plan in plans),
        "action_counts": {},
    }
    for plan in plans:
        for action, count in ((plan.get("summary") or {}).get("action_counts") or {}).items():
            plan_summary["action_counts"][action] = int_value(plan_summary["action_counts"].get(action), 0) + int_value(count, 0)

    return {
        "ok": proof_status == "green",
        "operation_id": operation,
        "environment": runtime_environment,
        "market_date": market_date,
        "interval": normalized_interval,
        "symbols": normalized_symbols,
        "apply": bool(apply_changes),
        "delete_extra_bars": bool(delete_extra_bars),
        "confirm_refetch": bool(confirm_refetch),
        "proof_status": proof_status,
        "blocked_symbols": failed_symbols,
        "initial_truth_summary": initial_summary,
        "repair_plan": {"summary": plan_summary, "symbols": plans},
        "applied_summary": {
            "applied": bool(apply_changes),
            "symbols": apply_results,
            "applied_items": sum(int_value(item.get("applied_items"), 0) for item in apply_results),
            "upserted_bars": sum(int_value(item.get("upserted_bars"), 0) for item in apply_results),
            "deleted_bars": sum(int_value(item.get("deleted_bars"), 0) for item in apply_results),
            "events_written": sum(int_value(item.get("events_written"), 0) for item in apply_results),
        },
        "final_truth_summary": final_summary,
        "errors": errors,
        "generated_at": utc_now_iso(),
    }


__all__ = [
    "TRUTH_REPAIR_ACTION_DELETE",
    "TRUTH_REPAIR_ACTION_REPLACE",
    "TRUTH_REPAIR_ACTION_UPSERT",
    "apply_truth_repair_plan",
    "build_truth_repair_payload",
    "build_truth_repair_plan",
    "confirm_delete_actions",
    "truth_summary_ok",
]
