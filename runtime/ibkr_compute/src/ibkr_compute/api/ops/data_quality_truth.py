from __future__ import annotations

from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
TRUTH_AUDIT_INTERVAL = "5m"
TRUTH_AUDIT_WINDOW_START = (9, 30)
TRUTH_AUDIT_WINDOW_END = (15, 55)
TRUTH_AUDIT_ERROR_FIELDS = (
    "missing_stored_bar_count",
    "missing_ibkr_bar_count",
    "bar_mismatch_count",
)


def normalize_truth_symbols(values: Iterable[str] | None) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        output.append(symbol)
    return output


def resolve_truth_audit_window(
    market_date: str,
    interval: str = TRUTH_AUDIT_INTERVAL,
) -> dict[str, int | str]:
    normalized_date = str(market_date or "").strip()
    if not normalized_date:
        raise ValueError("market_date_required")
    if str(interval or TRUTH_AUDIT_INTERVAL).strip().lower() != TRUTH_AUDIT_INTERVAL:
        raise ValueError("truth_audit_interval_unsupported")

    year, month, day = (int(part) for part in normalized_date.split("-", 2))
    start_dt = datetime(
        year,
        month,
        day,
        TRUTH_AUDIT_WINDOW_START[0],
        TRUTH_AUDIT_WINDOW_START[1],
        tzinfo=ET,
    )
    end_dt = datetime(
        year,
        month,
        day,
        TRUTH_AUDIT_WINDOW_END[0],
        TRUTH_AUDIT_WINDOW_END[1],
        tzinfo=ET,
    )
    return {
        "market_date": normalized_date,
        "start_ms": int(start_dt.timestamp() * 1000),
        "end_ms": int(end_dt.timestamp() * 1000),
    }


def classify_truth_audit_status(
    comparison_summary: dict | None = None,
    *,
    error: str = "",
) -> str:
    if str(error or "").strip():
        return "unavailable"

    summary = comparison_summary if isinstance(comparison_summary, dict) else {}
    for key in TRUTH_AUDIT_ERROR_FIELDS:
        if int(summary.get(key, 0) or 0) > 0:
            return "error"
    if int(summary.get("matched_bar_count", 0) or 0) <= 0:
        return "unavailable"
    return "ok"


def build_truth_audit_row(
    *,
    environment: str,
    market_date: str,
    symbol: str,
    interval: str,
    window_start_ms: int,
    window_end_ms: int,
    comparison_summary: dict | None = None,
    mismatch_examples: list[dict] | None = None,
    source_meta: dict | None = None,
    error: str = "",
    last_checked_at: str = "",
) -> dict:
    summary = comparison_summary if isinstance(comparison_summary, dict) else {}
    mismatch_rows = list(mismatch_examples or [])
    status = classify_truth_audit_status(summary, error=error)
    return {
        "environment": str(environment or "").strip().lower() or "live",
        "market_date": str(market_date or "").strip(),
        "symbol": str(symbol or "").strip().upper(),
        "interval": str(interval or TRUTH_AUDIT_INTERVAL).strip() or TRUTH_AUDIT_INTERVAL,
        "window_start_ms": int(window_start_ms or 0),
        "window_end_ms": int(window_end_ms or 0),
        "sampled_bar_count": int(
            summary.get("stored_visible_bars")
            or summary.get("ibkr_visible_bars")
            or 0
        ),
        "matched_bar_count": int(summary.get("matched_bar_count", 0) or 0),
        "missing_stored_bar_count": int(summary.get("missing_stored_bar_count", 0) or 0),
        "missing_ibkr_bar_count": int(summary.get("missing_ibkr_bar_count", 0) or 0),
        "bar_mismatch_count": int(summary.get("bar_mismatch_count", 0) or 0),
        "indicator_mismatch_count": int(summary.get("indicator_mismatch_count", 0) or 0),
        "signal_mismatch_count": int(summary.get("signal_mismatch_count", 0) or 0),
        "status": status,
        "mismatch_examples": mismatch_rows,
        "source_meta": {
            **(source_meta or {}),
            "error": str(error or "").strip(),
        },
        "last_checked_at": str(last_checked_at or "").strip(),
    }


def build_truth_audit_summary(
    rows: Iterable[dict] | None,
    *,
    expected_symbols: Iterable[str] | None = None,
) -> dict:
    items = [dict(row) for row in (rows or []) if isinstance(row, dict)]
    expected = normalize_truth_symbols(expected_symbols)
    symbol_map: dict[str, dict] = {}
    status_counts = {"ok": 0, "error": 0, "unavailable": 0}
    latest_checked_at = ""

    for row in items:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        symbol_map[symbol] = row
        status = str(row.get("status") or "unavailable").strip().lower() or "unavailable"
        if status not in status_counts:
            status_counts[status] = 0
        status_counts[status] += 1
        checked_at = str(row.get("last_checked_at") or "").strip()
        if checked_at and checked_at > latest_checked_at:
            latest_checked_at = checked_at

    audited_symbols = sorted(symbol_map.keys())
    expected_set = set(expected)
    audited_set = set(audited_symbols)
    unscanned_symbols = sorted(expected_set - audited_set) if expected else []
    mismatch_symbols = sorted(
        symbol
        for symbol, row in symbol_map.items()
        if str(row.get("status") or "").strip().lower() == "error"
    )
    unavailable_symbols = sorted(
        symbol
        for symbol, row in symbol_map.items()
        if str(row.get("status") or "").strip().lower() == "unavailable"
    )
    coverage_complete = bool(expected) and not unscanned_symbols
    return {
        "total": len(audited_symbols),
        "expected_symbols_total": len(expected),
        "audited_symbols_total": len(audited_symbols),
        "coverage_complete": coverage_complete,
        "unscanned_symbols": unscanned_symbols,
        "status_counts": status_counts,
        "mismatch_symbols": mismatch_symbols,
        "unavailable_symbols": unavailable_symbols,
        "symbols": audited_symbols,
        "latest_checked_at": latest_checked_at,
    }


__all__ = [
    "ET",
    "TRUTH_AUDIT_ERROR_FIELDS",
    "TRUTH_AUDIT_INTERVAL",
    "TRUTH_AUDIT_WINDOW_END",
    "TRUTH_AUDIT_WINDOW_START",
    "build_truth_audit_row",
    "build_truth_audit_summary",
    "classify_truth_audit_status",
    "normalize_truth_symbols",
    "resolve_truth_audit_window",
]
