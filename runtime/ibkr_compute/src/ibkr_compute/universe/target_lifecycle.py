from __future__ import annotations

from typing import Any, Iterable

from ibkr_compute.universe.target_execution import OBSERVE_TARGET_LAYER, parse_target_extra, target_extra_has_entry_activation


ACTIVE_SIGNAL_STATUSES = {
    "awaiting_confirm",
    "pending",
    "submitted",
    "submitted_waiting_fill",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "protection_incomplete",
    "protection_reprice_failed",
    "executed",
}
MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}


def _escape_filter_value(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _normalized_dates(values: Iterable[Any]) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        dates.append(text)
        seen.add(text)
    return dates


def _get_records(pb: Any, collection: str, *, filter: str = "", sort: str = "", max_pages: int = 5) -> list[dict[str, Any]]:
    if pb is None:
        return []
    get_all = getattr(pb, "get_all_records", None)
    if callable(get_all):
        rows = get_all(collection, filter=filter or None, sort=sort or None, max_pages=max_pages)
        return [dict(row) for row in (rows or []) if isinstance(row, dict)]
    getter = getattr(pb, "get_records", None)
    if not callable(getter):
        return []
    rows: list[dict[str, Any]] = []
    for page in range(1, max(1, max_pages) + 1):
        page_rows = getter(collection, filter=filter or None, sort=sort or None, per_page=200, page=page) or []
        typed_rows = [dict(row) for row in page_rows if isinstance(row, dict)]
        rows.extend(typed_rows)
        if len(typed_rows) < 200:
            break
    return rows


def _target_source(extra: dict[str, Any]) -> str:
    return _text(extra.get("source")).lower()


def _target_is_manual(extra: dict[str, Any]) -> bool:
    source = _target_source(extra)
    return source.startswith("manual_") or source in MANUAL_TARGET_SOURCES


def _has_other_open_signal(pb: Any, *, symbol: str, environment: str, closed_signal_id: str) -> bool:
    safe_symbol = _escape_filter_value(symbol)
    safe_environment = _escape_filter_value(environment)
    rows = _get_records(
        pb,
        "ibkr_signals",
        filter=f'symbol = "{safe_symbol}" && environment = "{safe_environment}"',
        sort="-updated",
        max_pages=5,
    )
    normalized_closed_id = _text(closed_signal_id)
    for row in rows:
        signal_id = _text(row.get("signal_id"))
        if normalized_closed_id and signal_id == normalized_closed_id:
            continue
        status = _text(row.get("status")).lower()
        extra = parse_target_extra(row.get("extra"))
        status_reason = _text(extra.get("status_reason") or row.get("note")).lower()
        if status in ACTIVE_SIGNAL_STATUSES and not (
            status_reason.startswith("closed_by_") or status_reason.startswith("superseded_by_")
        ):
            return True
    return False


def _target_matches_closed_signal(extra: dict[str, Any], signal_id: str) -> bool:
    if not target_extra_has_entry_activation(extra):
        return True
    entry_signal_id = _text(extra.get("entry_signal_id"))
    if entry_signal_id and signal_id:
        return entry_signal_id == signal_id
    return True


def demote_entry_activated_targets_after_close(
    pb: Any,
    *,
    symbol: str,
    environment: str,
    signal_id: str,
    dates: Iterable[Any],
    reason: str = "signal_closed",
    now_iso: str = "",
    logger: Any = None,
) -> dict[str, Any]:
    normalized_symbol = _text(symbol).upper()
    normalized_environment = _text(environment).lower() or "live"
    normalized_signal_id = _text(signal_id)
    target_dates = _normalized_dates(dates)
    result: dict[str, Any] = {
        "ok": True,
        "symbol": normalized_symbol,
        "environment": normalized_environment,
        "signal_id": normalized_signal_id,
        "dates": target_dates,
        "demoted": 0,
        "demoted_ids": [],
        "skipped": {},
    }
    if not pb or not normalized_symbol or not normalized_environment or not target_dates:
        result.update({"ok": False, "reason": "missing_context"})
        return result
    updater = getattr(pb, "update_record", None)
    if not callable(updater):
        result.update({"ok": False, "reason": "pb_update_unavailable"})
        return result

    if _has_other_open_signal(
        pb,
        symbol=normalized_symbol,
        environment=normalized_environment,
        closed_signal_id=normalized_signal_id,
    ):
        result["reason"] = "other_open_signal_exists"
        return result

    def skip(reason_key: str) -> None:
        skipped = result.setdefault("skipped", {})
        skipped[reason_key] = int(skipped.get(reason_key) or 0) + 1

    safe_symbol = _escape_filter_value(normalized_symbol)
    safe_environment = _escape_filter_value(normalized_environment)
    for target_date in target_dates:
        safe_date = _escape_filter_value(target_date)
        rows = _get_records(
            pb,
            "ibkr_targets",
            filter=(
                f'symbol = "{safe_symbol}" && '
                f'date = "{safe_date}" && '
                f'environment = "{safe_environment}" && '
                'status = "active"'
            ),
            sort="-updated",
            max_pages=2,
        )
        for row in rows:
            record_id = _text(row.get("id"))
            if not record_id:
                skip("missing_record_id")
                continue
            extra = parse_target_extra(row.get("extra"))
            if _target_is_manual(extra):
                skip("manual_target")
                continue
            if not _target_matches_closed_signal(extra, normalized_signal_id):
                skip("entry_activation_mismatch")
                continue
            blockers = ["target_not_active", "deactivated_after_close"]
            for blocker in extra.get("execution_blockers") or []:
                blocker_text = _text(blocker)
                if blocker_text and blocker_text not in blockers:
                    blockers.append(blocker_text)
            patch_extra = {
                **extra,
                "deactivated_after_close": True,
                "deactivated_signal_id": normalized_signal_id,
                "deactivated_reason": _text(reason) or "signal_closed",
                "deactivated_at": now_iso,
                "active_gate_passed": False,
                "context_active": False,
                "context_gate_passed": False,
                "execution_eligible": False,
                "execution_blockers": blockers,
                "target_layer": OBSERVE_TARGET_LAYER,
                "within_subscription_budget": False,
                "subscription_selected": False,
                "subscription_rank": 0,
            }
            try:
                updater("ibkr_targets", record_id, {"status": "candidate", "extra": patch_extra})
            except Exception as exc:
                skip("update_failed")
                if logger is not None:
                    try:
                        logger.warning("Failed to demote closed-signal target %s: %s", record_id, exc)
                    except Exception:
                        pass
                continue
            result["demoted"] = int(result.get("demoted") or 0) + 1
            result.setdefault("demoted_ids", []).append(record_id)
    return result


__all__ = ["ACTIVE_SIGNAL_STATUSES", "demote_entry_activated_targets_after_close"]
