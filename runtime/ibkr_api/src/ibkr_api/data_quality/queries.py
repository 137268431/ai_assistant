from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, to_int, to_text

EscapeFilterString = Callable[[Any], str]


def serialize_integrity_record(record: dict[str, Any]) -> dict[str, Any]:
    row = dict(record or {})
    return {
        "id": to_text(row.get("id")),
        "environment": to_text(row.get("environment")),
        "market_date": to_text(row.get("market_date")),
        "symbol": to_text(row.get("symbol")).upper(),
        "interval": to_text(row.get("interval") or "5m") or "5m",
        "scan_scope": to_text(row.get("scan_scope")),
        "status": to_text(row.get("status")),
        "needs_repair": bool(row.get("needs_repair")),
        "safe_repair": bool(row.get("safe_repair")),
        "bar_count": int(row.get("bar_count") or 0),
        "latest_bar_time_ms": int(row.get("latest_bar_time_ms") or 0),
        "latest_bar_us_time": to_text(row.get("latest_bar_us_time")),
        "oldest_loaded_ms": int(row.get("oldest_loaded_ms") or 0),
        "gap_count": int(row.get("gap_count") or 0),
        "duplicate_count": int(row.get("duplicate_count") or 0),
        "bad_ohlc_count": int(row.get("bad_ohlc_count") or 0),
        "missing_intervals": list(row.get("missing_intervals") or []),
        "stale_intervals": list(row.get("stale_intervals") or []),
        "gap_examples": list(row.get("gap_examples") or []),
        "duplicate_examples": list(row.get("duplicate_examples") or []),
        "bad_ohlc_examples": list(row.get("bad_ohlc_examples") or []),
        "repair_attempts": int(row.get("repair_attempts") or 0),
        "last_scan_at": to_text(row.get("last_scan_at")),
        "last_repair_at": to_text(row.get("last_repair_at")),
        "last_repair_result": ensure_object(row.get("last_repair_result")),
        "extra": ensure_object(row.get("extra")),
        "created": to_text(row.get("created")),
        "updated": to_text(row.get("updated")),
    }


def serialize_truth_record(record: dict[str, Any]) -> dict[str, Any]:
    row = dict(record or {})
    return {
        "id": to_text(row.get("id")),
        "environment": to_text(row.get("environment")),
        "market_date": to_text(row.get("market_date")),
        "symbol": to_text(row.get("symbol")).upper(),
        "interval": to_text(row.get("interval") or "5m") or "5m",
        "window_start_ms": int(row.get("window_start_ms") or 0),
        "window_end_ms": int(row.get("window_end_ms") or 0),
        "sampled_bar_count": int(row.get("sampled_bar_count") or 0),
        "matched_bar_count": int(row.get("matched_bar_count") or 0),
        "missing_stored_bar_count": int(row.get("missing_stored_bar_count") or 0),
        "missing_ibkr_bar_count": int(row.get("missing_ibkr_bar_count") or 0),
        "bar_mismatch_count": int(row.get("bar_mismatch_count") or 0),
        "indicator_mismatch_count": int(row.get("indicator_mismatch_count") or 0),
        "signal_mismatch_count": int(row.get("signal_mismatch_count") or 0),
        "status": to_text(row.get("status")),
        "mismatch_examples": list(row.get("mismatch_examples") or []),
        "source_meta": ensure_object(row.get("source_meta")),
        "last_checked_at": to_text(row.get("last_checked_at")),
        "created": to_text(row.get("created")),
        "updated": to_text(row.get("updated")),
    }


def serialize_daily_coverage_record(record: dict[str, Any]) -> dict[str, Any]:
    row = dict(record or {})
    return {
        "id": to_text(row.get("id")),
        "environment": to_text(row.get("environment")),
        "market_date": to_text(row.get("market_date")),
        "symbol": to_text(row.get("symbol")).upper(),
        "interval": to_text(row.get("interval") or "5m") or "5m",
        "session_mode": to_text(row.get("session_mode") or "regular") or "regular",
        "status": to_text(row.get("status")),
        "hard_gate": bool(row.get("hard_gate")),
        "needs_repair": bool(row.get("needs_repair")),
        "expected_count": int(row.get("expected_count") or 0),
        "actual_count": int(row.get("actual_count") or 0),
        "missing_count": int(row.get("missing_count") or 0),
        "gap_count": int(row.get("gap_count") or 0),
        "duplicate_count": int(row.get("duplicate_count") or 0),
        "bad_ohlc_count": int(row.get("bad_ohlc_count") or 0),
        "expected_start_ms": int(row.get("expected_start_ms") or 0),
        "expected_end_ms": int(row.get("expected_end_ms") or 0),
        "first_bar_ms": int(row.get("first_bar_ms") or 0),
        "last_bar_ms": int(row.get("last_bar_ms") or 0),
        "last_checked_at": to_text(row.get("last_checked_at")),
        "last_repair_at": to_text(row.get("last_repair_at")),
        "missing_windows": list(row.get("missing_windows") or []),
        "missing_examples": list(row.get("missing_examples") or []),
        "repair_windows": list(row.get("repair_windows") or []),
        "expected_mask_hex": to_text(row.get("expected_mask_hex")),
        "actual_mask_hex": to_text(row.get("actual_mask_hex")),
        "missing_mask_hex": to_text(row.get("missing_mask_hex")),
        "source": to_text(row.get("source")),
        "extra": ensure_object(row.get("extra")),
        "created": to_text(row.get("created")),
        "updated": to_text(row.get("updated")),
        "row_updated_at": to_text(row.get("updated") or row.get("last_checked_at")),
    }


def _latest_by_key(records: list[dict[str, Any]], serializer: Callable[[dict[str, Any]], dict[str, Any]], key_builder: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records or []:
        item = serializer(row)
        key = to_text(key_builder(item))
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def load_effective_watchlist_symbols(pb: Any, environment: str) -> list[str]:
    runtime_environment = to_text(environment).lower() or "live"
    rows = pb.get_records(
        "watchlist",
        filter=(
            f'environment = "{runtime_environment}" || environment = "global" || environment = ""'
        ),
        sort="-updated",
        per_page=500,
        page=1,
    )
    priority = {"": 0, "global": 1, runtime_environment: 2}
    best_by_symbol: dict[str, int] = {}
    for row in rows or []:
        symbol = to_text((row or {}).get("symbol")).upper()
        if not symbol:
            continue
        row_environment = to_text((row or {}).get("environment")).lower()
        rank = priority.get(row_environment)
        if rank is None:
            continue
        if symbol not in best_by_symbol or rank > best_by_symbol[symbol]:
            best_by_symbol[symbol] = rank
    return sorted(best_by_symbol.keys())


def load_integrity_items(pb: Any, environment: str, *, market_date: str = "", scan_scope: str = "", symbol: str = "") -> list[dict[str, Any]]:
    filter_parts = [f'environment = "{to_text(environment).lower() or "live"}"']
    if market_date:
        filter_parts.append(f'market_date = "{to_text(market_date)}"')
    if scan_scope:
        filter_parts.append(f'scan_scope = "{to_text(scan_scope)}"')
    if symbol:
        filter_parts.append(f'symbol = "{to_text(symbol).upper()}"')
    rows = pb.get_all_records(
        "ibkr_bar_integrity",
        filter=" && ".join(filter_parts),
        sort="-updated",
        max_pages=20,
    )
    return _latest_by_key(
        [dict(row) for row in rows or []],
        serialize_integrity_record,
        lambda item: f'{to_text(item.get("symbol")).upper()}::{to_text(item.get("interval") or "5m")}',
    )


def load_truth_items(pb: Any, environment: str, *, market_date: str = "", symbol: str = "") -> list[dict[str, Any]]:
    filter_parts = [f'environment = "{to_text(environment).lower() or "live"}"']
    if market_date:
        filter_parts.append(f'market_date = "{to_text(market_date)}"')
    if symbol:
        filter_parts.append(f'symbol = "{to_text(symbol).upper()}"')
    try:
        rows = pb.get_all_records(
            "ibkr_bar_truth_audit",
            filter=" && ".join(filter_parts),
            sort="-updated",
            max_pages=20,
        )
    except Exception:
        rows = []
    return _latest_by_key(
        [dict(row) for row in rows or []],
        serialize_truth_record,
        lambda item: f'{to_text(item.get("symbol")).upper()}::{to_text(item.get("interval") or "5m")}',
    )


def load_daily_coverage_items(
    pb: Any,
    environment: str,
    *,
    market_date: str = "",
    date_from: str = "",
    date_to: str = "",
    symbol: str = "",
    session_mode: str = "",
    status: str = "",
    needs_repair: bool | None = None,
) -> list[dict[str, Any]]:
    filter_parts = [f'environment = "{to_text(environment).lower() or "live"}"']
    if market_date:
        filter_parts.append(f'market_date = "{to_text(market_date)}"')
    if date_from:
        filter_parts.append(f'market_date >= "{to_text(date_from)}"')
    if date_to:
        filter_parts.append(f'market_date <= "{to_text(date_to)}"')
    if symbol:
        filter_parts.append(f'symbol = "{to_text(symbol).upper()}"')
    if session_mode:
        filter_parts.append(f'session_mode = "{to_text(session_mode).lower()}"')
    if status:
        filter_parts.append(f'status = "{to_text(status).lower()}"')
    if needs_repair is not None:
        filter_parts.append(f'needs_repair = {str(bool(needs_repair)).lower()}')
    try:
        rows = pb.get_all_records(
            "ibkr_bar_coverage_daily",
            filter=" && ".join(filter_parts),
            sort="-market_date,-updated",
            max_pages=60,
        )
    except Exception:
        rows = []
    return [serialize_daily_coverage_record(dict(row)) for row in rows or []]


def resolve_proof(item: dict[str, Any]) -> dict[str, str]:
    integrity_status = to_text(item.get("status")).lower()
    truth_status = to_text(item.get("truth_status")).lower()
    duplicate_count = int(item.get("duplicate_count") or 0)
    bad_ohlc_count = int(item.get("bad_ohlc_count") or 0)
    if duplicate_count > 0 or bad_ohlc_count > 0 or integrity_status in {"error", "repair_failed"} or truth_status == "error":
        return {
            "status": "red",
            "title": "证明失败",
            "copy": "IBKR 真值比对发现缺失或字段不一致。" if truth_status == "error" else "内部一致性存在人工复核或修复失败问题。",
        }
    if integrity_status in {"ok", "repaired"} and truth_status == "ok":
        return {
            "status": "green",
            "title": "证明通过",
            "copy": "内部一致性正常，且已通过 IBKR 真值比对。",
        }
    copy = "当前仍缺少完整证明。"
    if not integrity_status:
        copy = "尚未完成内部一致性扫描。"
    elif integrity_status == "warn" or bool(item.get("needs_repair")):
        copy = "内部一致性仍有待补齐问题。"
    elif not truth_status:
        copy = "尚未运行 IBKR 真值审计。"
    elif truth_status == "unavailable":
        copy = "真值审计本轮不可用，请检查网关或会话。"
    return {"status": "yellow", "title": "证明未完成", "copy": copy}


def merge_items(integrity_items: list[dict[str, Any]], truth_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    def build_key(item: dict[str, Any]) -> str:
        return f'{to_text(item.get("symbol")).upper()}::{to_text(item.get("interval") or "5m")}'

    for item in integrity_items or []:
        key = build_key(item)
        merged[key] = {
            **item,
            "truth_audit": None,
            "truth_status": "",
            "truth_checked_at": "",
            "proof": {"status": "yellow", "title": "证明未完成", "copy": "尚未运行 IBKR 真值审计。"},
            "row_updated_at": to_text(item.get("updated") or item.get("last_scan_at")),
        }
    for truth in truth_items or []:
        key = build_key(truth)
        base = merged.get(key) or {
            "id": "",
            "environment": to_text(truth.get("environment")),
            "market_date": to_text(truth.get("market_date")),
            "symbol": to_text(truth.get("symbol")).upper(),
            "interval": to_text(truth.get("interval") or "5m") or "5m",
            "scan_scope": "",
            "status": "",
            "needs_repair": False,
            "safe_repair": False,
            "bar_count": 0,
            "latest_bar_time_ms": 0,
            "latest_bar_us_time": "",
            "oldest_loaded_ms": 0,
            "gap_count": 0,
            "duplicate_count": 0,
            "bad_ohlc_count": 0,
            "missing_intervals": [],
            "stale_intervals": [],
            "gap_examples": [],
            "duplicate_examples": [],
            "bad_ohlc_examples": [],
            "repair_attempts": 0,
            "last_scan_at": "",
            "last_repair_at": "",
            "last_repair_result": {},
            "extra": {},
            "created": "",
            "updated": "",
            "row_updated_at": "",
        }
        base["truth_audit"] = truth
        base["truth_status"] = to_text(truth.get("status"))
        base["truth_checked_at"] = to_text(truth.get("last_checked_at"))
        base["row_updated_at"] = to_text(
            truth.get("updated")
            or truth.get("last_checked_at")
            or base.get("row_updated_at")
            or base.get("updated")
            or base.get("last_scan_at")
        )
        base["proof"] = resolve_proof(base)
        merged[key] = base
    items = list(merged.values())
    for item in items:
        item["proof"] = resolve_proof(item)
    items.sort(key=lambda item: to_text(item.get("row_updated_at")), reverse=True)
    return items


def build_summary(environment: str, market_date: str, scan_scope: str, merged_items: list[dict[str, Any]], expected_symbols: list[str]) -> dict[str, Any]:
    items = list(merged_items or [])
    expected = list(expected_symbols or [])
    integrity_symbols: dict[str, bool] = {}
    truth_symbols: dict[str, bool] = {}
    summary = {
        "environment": environment,
        "market_date": market_date,
        "total": len(items),
        "needs_repair": 0,
        "manual_review": 0,
        "latest_scan_at": "",
        "latest_repair_at": "",
        "latest_truth_checked_at": "",
        "status_counts": {"ok": 0, "warn": 0, "error": 0, "repaired": 0, "repair_failed": 0, "repairing": 0, "missing": 0},
        "truth_status_counts": {"ok": 0, "error": 0, "unavailable": 0, "missing": 0},
        "proof_status_counts": {"green": 0, "yellow": 0, "red": 0},
        "scan_scope_counts": {"active_target": 0, "watchlist": 0, "manual": 0},
        "symbols": [],
        "expected_symbols_total": len(expected),
        "scanned_symbols_total": 0,
        "truth_audited_symbols_total": 0,
        "coverage_complete": False,
        "truth_coverage_complete": False,
        "unscanned_symbols": [],
        "unaudited_symbols": [],
        "database_correctness_status": "yellow",
        "database_correctness_copy": "当前还不能证明整库 bar 正确。",
    }
    symbol_set: dict[str, bool] = {}
    for item in items:
        symbol = to_text(item.get("symbol")).upper()
        if not symbol:
            continue
        symbol_set[symbol] = True
        status = to_text(item.get("status") or "missing").lower() or "missing"
        truth_status = to_text(item.get("truth_status") or "missing").lower() or "missing"
        proof_status = to_text(ensure_object(item.get("proof")).get("status") or "yellow").lower() or "yellow"
        scope = to_text(item.get("scan_scope"))
        summary["status_counts"].setdefault(status, 0)
        summary["truth_status_counts"].setdefault(truth_status, 0)
        summary["proof_status_counts"].setdefault(proof_status, 0)
        summary["scan_scope_counts"].setdefault(scope, 0)
        summary["status_counts"][status] += 1
        summary["truth_status_counts"][truth_status] += 1
        summary["proof_status_counts"][proof_status] += 1
        if scope:
            summary["scan_scope_counts"][scope] += 1
        if status != "missing":
            integrity_symbols[symbol] = True
        if truth_status != "missing":
            truth_symbols[symbol] = True
        if bool(item.get("needs_repair")):
            summary["needs_repair"] += 1
        if int(item.get("duplicate_count") or 0) > 0 or int(item.get("bad_ohlc_count") or 0) > 0:
            summary["manual_review"] += 1
        last_scan_at = to_text(item.get("last_scan_at"))
        last_repair_at = to_text(item.get("last_repair_at"))
        truth_checked_at = to_text(item.get("truth_checked_at"))
        if last_scan_at and (not summary["latest_scan_at"] or last_scan_at > summary["latest_scan_at"]):
            summary["latest_scan_at"] = last_scan_at
        if last_repair_at and (not summary["latest_repair_at"] or last_repair_at > summary["latest_repair_at"]):
            summary["latest_repair_at"] = last_repair_at
        if truth_checked_at and (not summary["latest_truth_checked_at"] or truth_checked_at > summary["latest_truth_checked_at"]):
            summary["latest_truth_checked_at"] = truth_checked_at

    summary["symbols"] = sorted(symbol_set.keys())
    summary["scanned_symbols_total"] = len(integrity_symbols)
    summary["truth_audited_symbols_total"] = len(truth_symbols)
    if expected:
        summary["unscanned_symbols"] = [symbol for symbol in expected if symbol not in integrity_symbols]
        summary["unaudited_symbols"] = [symbol for symbol in expected if symbol not in truth_symbols]
        summary["coverage_complete"] = len(summary["unscanned_symbols"]) == 0
        summary["truth_coverage_complete"] = len(summary["unaudited_symbols"]) == 0
    if not expected:
        summary["database_correctness_status"] = "yellow"
        summary["database_correctness_copy"] = "当前范围没有可证明的观察池标的。"
    elif summary["proof_status_counts"].get("red", 0) > 0:
        summary["database_correctness_status"] = "red"
        summary["database_correctness_copy"] = "至少有一个标的内部一致性失败或 IBKR 真值比对失败。"
    elif summary["coverage_complete"] and summary["truth_coverage_complete"] and summary["proof_status_counts"].get("green", 0) == len(expected):
        summary["database_correctness_status"] = "green"
        summary["database_correctness_copy"] = "全观察池已完成内部扫描与 IBKR 真值比对，当前库内 bar 可证明为正确。"
    else:
        summary["database_correctness_status"] = "yellow"
        summary["database_correctness_copy"] = "当前仍缺少完整覆盖或真值审计，整库还不能被证明为正确。"
    return summary


def build_truth_summary(environment: str, market_date: str, truth_items: list[dict[str, Any]], expected_symbols: list[str]) -> dict[str, Any]:
    items = list(truth_items or [])
    expected = list(expected_symbols or [])
    seen_symbols: dict[str, bool] = {}
    summary = {
        "environment": environment,
        "market_date": market_date,
        "total": len(items),
        "latest_checked_at": "",
        "status_counts": {"ok": 0, "error": 0, "unavailable": 0},
        "expected_symbols_total": len(expected),
        "audited_symbols_total": 0,
        "coverage_complete": False,
        "unscanned_symbols": [],
        "symbols": [],
    }
    for item in items:
        symbol = to_text(item.get("symbol")).upper()
        if symbol:
            seen_symbols[symbol] = True
        status = to_text(item.get("status") or "unavailable").lower() or "unavailable"
        summary["status_counts"].setdefault(status, 0)
        summary["status_counts"][status] += 1
        checked_at = to_text(item.get("last_checked_at"))
        if checked_at and (not summary["latest_checked_at"] or checked_at > summary["latest_checked_at"]):
            summary["latest_checked_at"] = checked_at
    summary["symbols"] = sorted(seen_symbols.keys())
    summary["audited_symbols_total"] = len(summary["symbols"])
    if expected:
        summary["unscanned_symbols"] = [symbol for symbol in expected if symbol not in seen_symbols]
        summary["coverage_complete"] = len(summary["unscanned_symbols"]) == 0
    return summary


def build_daily_coverage_summary(environment: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    rows = list(items or [])
    status_counts: dict[str, int] = {}
    session_counts: dict[str, int] = {}
    symbols: dict[str, bool] = {}
    dates: dict[str, bool] = {}
    needs_repair = 0
    hard_gate = 0
    missing_count = 0
    gap_count = 0
    latest_checked_at = ""
    latest_repair_at = ""
    for row in rows:
        status = to_text(row.get("status") or "missing").lower() or "missing"
        session_mode = to_text(row.get("session_mode") or "regular").lower() or "regular"
        status_counts[status] = status_counts.get(status, 0) + 1
        session_counts[session_mode] = session_counts.get(session_mode, 0) + 1
        symbol = to_text(row.get("symbol")).upper()
        market_date = to_text(row.get("market_date"))
        if symbol:
            symbols[symbol] = True
        if market_date:
            dates[market_date] = True
        if bool(row.get("needs_repair")):
            needs_repair += 1
        if bool(row.get("hard_gate")):
            hard_gate += 1
        missing_count += int(row.get("missing_count") or 0)
        gap_count += int(row.get("gap_count") or 0)
        checked_at = to_text(row.get("last_checked_at"))
        repaired_at = to_text(row.get("last_repair_at"))
        if checked_at and (not latest_checked_at or checked_at > latest_checked_at):
            latest_checked_at = checked_at
        if repaired_at and (not latest_repair_at or repaired_at > latest_repair_at):
            latest_repair_at = repaired_at
    return {
        "environment": environment,
        "total": len(rows),
        "symbols_total": len(symbols),
        "dates_total": len(dates),
        "symbols": sorted(symbols.keys()),
        "date_from": min(dates.keys()) if dates else "",
        "date_to": max(dates.keys()) if dates else "",
        "status_counts": status_counts,
        "session_mode_counts": session_counts,
        "needs_repair": needs_repair,
        "hard_gate": hard_gate,
        "missing_count": missing_count,
        "gap_count": gap_count,
        "latest_checked_at": latest_checked_at,
        "latest_repair_at": latest_repair_at,
        "database_clean": needs_repair == 0 and hard_gate == 0 and status_counts.get("hard_gap", 0) == 0,
    }


def paginate(items: list[dict[str, Any]], *, page: int, per_page: int) -> dict[str, Any]:
    offset = max(0, (int(page) - 1) * int(per_page))
    visible = list(items or [])[offset : offset + int(per_page)]
    return {"total": len(items or []), "items": visible}


__all__ = [
    "build_daily_coverage_summary",
    "build_summary",
    "build_truth_summary",
    "load_daily_coverage_items",
    "load_effective_watchlist_symbols",
    "load_integrity_items",
    "load_truth_items",
    "merge_items",
    "paginate",
    "resolve_proof",
    "serialize_daily_coverage_record",
    "serialize_integrity_record",
    "serialize_truth_record",
]
