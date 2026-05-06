from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")


def normalize_interval_value(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "5": "5m",
        "5m": "5m",
        "15": "15m",
        "15m": "15m",
        "30": "30m",
        "30m": "30m",
        "60": "1h",
        "1h": "1h",
        "240": "4h",
        "4h": "4h",
        "d": "1d",
        "1d": "1d",
    }
    return mapping.get(normalized, normalized or "5m")


def interval_to_ms(value: Any) -> int:
    mapping = {
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    return mapping.get(normalize_interval_value(value), mapping["5m"])


def format_zoned_datetime(ms: int, tz: ZoneInfo) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def build_bar_close_meta(bar_time_ms: int, interval_value: Any) -> dict[str, Any]:
    if not isinstance(bar_time_ms, int) or bar_time_ms <= 0:
        return {}
    close_ms = bar_time_ms + interval_to_ms(interval_value)
    return {
        "bar_time_semantics": "start",
        "bar_close_time_ms": close_ms,
        "bar_close_us_time": format_zoned_datetime(close_ms, ET),
        "bar_close_cn_time": format_zoned_datetime(close_ms, CN),
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def prepare_bar_row(payload: dict[str, Any], default_environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    interval = normalize_interval_value(payload.get("interval"))
    environment = str(payload.get("environment") or default_environment or "live").strip().lower() or "live"
    bar_time_ms = int(payload.get("bar_time_ms") or 0)
    if not symbol or not interval or bar_time_ms <= 0:
        return None, "missing_symbol_interval_or_bar_time_ms"

    extra = _as_dict(payload.get("extra"))
    extra.update(build_bar_close_meta(bar_time_ms, interval))
    return {
        "symbol": symbol,
        "environment": environment,
        "exchange": str(payload.get("exchange") or "").strip().upper(),
        "interval": interval,
        "open": float(payload.get("open") or 0),
        "high": float(payload.get("high") or 0),
        "low": float(payload.get("low") or 0),
        "close": float(payload.get("close") or 0),
        "volume": float(payload.get("volume") or 0),
        "session_type": str(payload.get("session_type") or "").strip(),
        "us_time": str(payload.get("us_time") or "").strip(),
        "cn_time": str(payload.get("cn_time") or "").strip(),
        "bar_time_ms": bar_time_ms,
        "extra": extra,
    }, ""


def prepare_indicator_row(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    interval = str(payload.get("interval") or "").strip()
    bar_time_ms = int(payload.get("bar_time_ms") or 0)
    if not symbol or not interval or bar_time_ms <= 0:
        return None, "missing_symbol_interval_or_bar_time_ms"
    extra = _as_dict(payload.get("extra"))
    extra.update(build_bar_close_meta(bar_time_ms, interval))
    extra.setdefault("environment", environment)
    extra.setdefault("source", "ibkr_compute")
    return {
        "symbol": symbol,
        "environment": environment,
        "exchange": str(payload.get("exchange") or "").strip().upper(),
        "interval": interval,
        "script_tag": str(payload.get("script_tag") or "").strip(),
        "us_time": str(payload.get("us_time") or "").strip(),
        "cn_time": str(payload.get("cn_time") or "").strip(),
        "bar_time_ms": bar_time_ms,
        "bar_index": int(payload.get("bar_index")) if payload.get("bar_index") is not None else None,
        "extra": extra,
    }, ""


def prepare_scan_row(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    market_date = str(payload.get("date") or "").strip()
    if not symbol or not market_date:
        return None, "missing_symbol_or_date"
    return {
        "symbol": symbol,
        "environment": environment,
        "exchange": str(payload.get("exchange") or "").strip().upper(),
        "date": market_date,
        "direction_bias": str(payload.get("direction_bias") or "neutral").strip() or "neutral",
        "score": float(payload.get("score") or 0),
        "scan_reason": str(payload.get("scan_reason") or ""),
        "status": str(payload.get("status") or "candidate"),
        "us_time": str(payload.get("us_time") or "").strip(),
        "cn_time": str(payload.get("cn_time") or "").strip(),
        "bar_time_ms": int(payload.get("bar_time_ms") or 0),
        "extra": _as_dict(payload.get("extra")),
    }, ""


def prepare_bar_integrity_row(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    market_date = str(payload.get("market_date") or "").strip()
    interval = str(payload.get("interval") or "5m").strip() or "5m"
    if not symbol or not market_date:
        return None, "missing_symbol_or_market_date"
    return {
        "environment": environment,
        "market_date": market_date,
        "symbol": symbol,
        "interval": interval,
        "scan_scope": str(payload.get("scan_scope") or "manual").strip() or "manual",
        "status": str(payload.get("status") or "ok").strip() or "ok",
        "needs_repair": bool(payload.get("needs_repair")),
        "safe_repair": bool(payload.get("safe_repair")),
        "bar_count": int(payload.get("bar_count") or 0),
        "latest_bar_time_ms": int(payload.get("latest_bar_time_ms") or 0),
        "latest_bar_us_time": str(payload.get("latest_bar_us_time") or "").strip(),
        "oldest_loaded_ms": int(payload.get("oldest_loaded_ms") or 0),
        "gap_count": int(payload.get("gap_count") or 0),
        "duplicate_count": int(payload.get("duplicate_count") or 0),
        "bad_ohlc_count": int(payload.get("bad_ohlc_count") or 0),
        "missing_intervals": list(payload.get("missing_intervals") or []),
        "stale_intervals": list(payload.get("stale_intervals") or []),
        "gap_examples": list(payload.get("gap_examples") or []),
        "duplicate_examples": list(payload.get("duplicate_examples") or []),
        "bad_ohlc_examples": list(payload.get("bad_ohlc_examples") or []),
        "repair_attempts": int(payload.get("repair_attempts") or 0),
        "last_scan_at": str(payload.get("last_scan_at") or "").strip(),
        "last_repair_at": str(payload.get("last_repair_at") or "").strip(),
        "last_repair_result": _as_dict(payload.get("last_repair_result")),
        "extra": _as_dict(payload.get("extra")),
        "__increment_repair_attempts": bool(payload.get("increment_repair_attempts")),
    }, ""


def prepare_bar_coverage_daily_row(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    market_date = str(payload.get("market_date") or "").strip()
    interval = normalize_interval_value(payload.get("interval") or "5m")
    session_mode = str(payload.get("session_mode") or "regular").strip().lower() or "regular"
    if not symbol or not market_date:
        return None, "missing_symbol_or_market_date"
    if session_mode not in {"regular", "extended"}:
        session_mode = "regular"
    return {
        "environment": environment,
        "market_date": market_date,
        "symbol": symbol,
        "interval": interval,
        "session_mode": session_mode,
        "status": str(payload.get("status") or "ok").strip() or "ok",
        "hard_gate": bool(payload.get("hard_gate")),
        "needs_repair": bool(payload.get("needs_repair")),
        "expected_count": int(payload.get("expected_count") or 0),
        "actual_count": int(payload.get("actual_count") or 0),
        "missing_count": int(payload.get("missing_count") or 0),
        "gap_count": int(payload.get("gap_count") or 0),
        "duplicate_count": int(payload.get("duplicate_count") or 0),
        "bad_ohlc_count": int(payload.get("bad_ohlc_count") or 0),
        "expected_start_ms": int(payload.get("expected_start_ms") or 0),
        "expected_end_ms": int(payload.get("expected_end_ms") or 0),
        "first_bar_ms": int(payload.get("first_bar_ms") or 0),
        "last_bar_ms": int(payload.get("last_bar_ms") or 0),
        "last_checked_at": str(payload.get("last_checked_at") or "").strip(),
        "last_repair_at": str(payload.get("last_repair_at") or "").strip(),
        "missing_windows": list(payload.get("missing_windows") or []),
        "missing_examples": list(payload.get("missing_examples") or []),
        "repair_windows": list(payload.get("repair_windows") or []),
        "expected_mask_hex": str(payload.get("expected_mask_hex") or "").strip(),
        "actual_mask_hex": str(payload.get("actual_mask_hex") or "").strip(),
        "missing_mask_hex": str(payload.get("missing_mask_hex") or "").strip(),
        "source": str(payload.get("source") or "manual_scan").strip() or "manual_scan",
        "extra": _as_dict(payload.get("extra")),
    }, ""


def prepare_bar_truth_row(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    market_date = str(payload.get("market_date") or "").strip()
    interval = str(payload.get("interval") or "5m").strip() or "5m"
    if not symbol or not market_date:
        return None, "missing_symbol_or_market_date"
    return {
        "environment": environment,
        "market_date": market_date,
        "symbol": symbol,
        "interval": interval,
        "window_start_ms": int(payload.get("window_start_ms") or 0),
        "window_end_ms": int(payload.get("window_end_ms") or 0),
        "sampled_bar_count": int(payload.get("sampled_bar_count") or 0),
        "matched_bar_count": int(payload.get("matched_bar_count") or 0),
        "missing_stored_bar_count": int(payload.get("missing_stored_bar_count") or 0),
        "missing_ibkr_bar_count": int(payload.get("missing_ibkr_bar_count") or 0),
        "bar_mismatch_count": int(payload.get("bar_mismatch_count") or 0),
        "indicator_mismatch_count": int(payload.get("indicator_mismatch_count") or 0),
        "signal_mismatch_count": int(payload.get("signal_mismatch_count") or 0),
        "status": str(payload.get("status") or "unavailable").strip() or "unavailable",
        "mismatch_examples": list(payload.get("mismatch_examples") or []),
        "source_meta": _as_dict(payload.get("source_meta")),
        "last_checked_at": str(payload.get("last_checked_at") or "").strip(),
    }, ""


def build_ping_signal_row(*, signal_id: str, now_ms: int) -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "environment": "live",
        "direction": "long",
        "signal": "ping_write",
        "limit_price": 0,
        "entry": 100,
        "stop_loss": 99,
        "take_profit": 101,
        "rr": "1.00",
        "shares": 1,
        "signal_id": signal_id,
        "exchange": "NASDAQ",
        "interval": "5",
        "reason": "ping_write",
        "us_time": "2026-04-02 11:24:00",
        "cn_time": "2026-04-02 23:24:00",
        "date": "2026-04-02",
        "bar_time_ms": now_ms,
        "bar_index": 1,
        "script_tag": "ping",
        "chart_tf": "5",
        "extra": {"source": "ping_write", "environment": "live"},
        "status": "pending",
        "note": "",
    }


def merge_bar_integrity_row(item: dict[str, Any], existing_row: dict[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in item.items() if not str(key).startswith("__")}
    existing_attempts = int((existing_row or {}).get("repair_attempts") or 0)
    if item.get("__increment_repair_attempts"):
        merged["repair_attempts"] = max(int(item.get("repair_attempts") or 0), existing_attempts + 1)
        return merged
    merged["repair_attempts"] = max(int(item.get("repair_attempts") or 0), existing_attempts)
    if not merged.get("last_repair_at"):
        merged["last_repair_at"] = str((existing_row or {}).get("last_repair_at") or "")
        merged["last_repair_result"] = _as_dict((existing_row or {}).get("last_repair_result"))
    return merged


def merge_bar_coverage_daily_row(item: dict[str, Any], existing_row: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(item or {})
    if not merged.get("last_repair_at"):
        merged["last_repair_at"] = str((existing_row or {}).get("last_repair_at") or "")
    return merged


def batch_upsert_records(
    pb: Any,
    collection: str,
    items: list[dict[str, Any]],
    unique_fields: list[str],
    *,
    timeout: int = 30,
    update_transform: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prepared = [dict(item or {}) for item in items if isinstance(item, dict)]
    if not prepared:
        return {"ok": True, "created": 0, "updated": 0, "skipped": 0, "total": 0}

    existing = pb._find_existing_records(collection, prepared, unique_fields)
    requests_payload: list[dict[str, Any]] = []
    created = 0
    updated = 0

    for item in prepared:
        unique_key = tuple(item.get(field) for field in unique_fields)
        existing_row = existing.get(unique_key)
        body = dict(item)
        if update_transform is not None:
            body = update_transform(body, existing_row)
        body = {key: value for key, value in body.items() if not str(key).startswith("__")}
        if existing_row and existing_row.get("id"):
            updated += 1
            requests_payload.append(
                {
                    "method": "PATCH",
                    "url": f"/api/collections/{collection}/records/{existing_row['id']}",
                    "body": body,
                }
            )
            continue
        created += 1
        requests_payload.append(
            {
                "method": "POST",
                "url": f"/api/collections/{collection}/records",
                "body": body,
            }
        )

    pb._execute_batch_requests(requests_payload, timeout=timeout)
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": 0,
        "total": len(prepared),
    }
