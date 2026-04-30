"""Freshness planning for authoritative IBKR bar storage."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    bucket_start_ms,
    format_us_time,
    interval_to_ms,
    normalize_interval,
)

EXTENDED_SESSION_OPEN_MINUTE = 4 * 60
EXTENDED_SESSION_CLOSE_MINUTE = 20 * 60
REGULAR_SESSION_CLOSE_MINUTE = 16 * 60
DEFAULT_CLOSE_DELAY_SECONDS = 8
DEFAULT_REQUIRED_BARS = 0


def _escape_pb_filter(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def build_bar_environment_filter(environment: str, *, include_legacy_empty: bool = True) -> str:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    clauses = [f'environment = "{_escape_pb_filter(runtime_environment)}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def extract_conid_from_bar_row(row: dict | None) -> int:
    payload = row or {}
    extra = payload.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}
    for value in (payload.get("conid"), payload.get("conidEx"), extra.get("conid"), extra.get("conidEx")):
        try:
            conid = int(float(value or 0))
        except (TypeError, ValueError):
            conid = 0
        if conid > 0:
            return conid
    return 0


def _previous_business_day(value: datetime) -> datetime:
    cursor = value - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _previous_session_last_5m(value: datetime) -> datetime:
    cursor = _previous_business_day(value)
    return cursor.replace(hour=19, minute=55, second=0, microsecond=0)


def latest_expected_extended_5m_ms(*, now_ms: int | None = None, delay_seconds: float = DEFAULT_CLOSE_DELAY_SECONDS) -> int:
    """Return the latest expected closed 5m bucket in US extended-hours time.

    This avoids treating post-20:00 ET or weekend clock time as missing bars.
    Holiday calendars are intentionally not embedded here; IBKR API repair remains
    the authority if a holiday creates a false-positive stale diagnosis.
    """

    if now_ms and int(now_ms) > 0:
        now = datetime.fromtimestamp(int(now_ms) / 1000, ET)
    else:
        now = datetime.now(ET)
    effective = now - timedelta(seconds=max(0.0, float(delay_seconds or 0.0)))
    if effective.weekday() >= 5:
        return int(_previous_session_last_5m(effective).timestamp() * 1000)

    minutes = effective.hour * 60 + effective.minute
    if minutes < EXTENDED_SESSION_OPEN_MINUTE:
        return int(_previous_session_last_5m(effective).timestamp() * 1000)
    if minutes >= EXTENDED_SESSION_CLOSE_MINUTE:
        end = effective.replace(hour=19, minute=55, second=0, microsecond=0)
        return int(end.timestamp() * 1000)

    closed_source = effective - timedelta(milliseconds=interval_to_ms("5m"))
    closed_minutes = closed_source.hour * 60 + closed_source.minute
    if closed_source.weekday() >= 5 or closed_minutes < EXTENDED_SESSION_OPEN_MINUTE:
        return int(_previous_session_last_5m(effective).timestamp() * 1000)
    bucket_minutes = closed_minutes - (closed_minutes % 5)
    bucket = closed_source.replace(
        hour=bucket_minutes // 60,
        minute=bucket_minutes % 60,
        second=0,
        microsecond=0,
    )
    if bucket.hour * 60 + bucket.minute < EXTENDED_SESSION_OPEN_MINUTE:
        return int(_previous_session_last_5m(effective).timestamp() * 1000)
    return int(bucket.timestamp() * 1000)


def _previous_bucket_start_ms(bucket_ms: int, interval: str) -> int:
    interval_ms = interval_to_ms(interval)
    if bucket_ms <= interval_ms:
        return 0
    return bucket_start_ms(bucket_ms - 1, interval)


def expected_closed_ms_from_latest_5m(latest_5m_ms: int, interval: str) -> int:
    normalized = normalize_interval(interval)
    latest_5m_ms = int(latest_5m_ms or 0)
    if latest_5m_ms <= 0:
        return 0
    if normalized == "5m":
        return latest_5m_ms

    latest_dt = datetime.fromtimestamp(latest_5m_ms / 1000, ET)
    if normalized == "1d":
        minutes = latest_dt.hour * 60 + latest_dt.minute
        if minutes >= REGULAR_SESSION_CLOSE_MINUTE:
            day = latest_dt
        else:
            day = _previous_business_day(latest_dt)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    interval_ms = interval_to_ms(normalized)
    current_bucket_ms = bucket_start_ms(latest_5m_ms, normalized)
    latest_5m_close_ms = latest_5m_ms + interval_to_ms("5m")
    if latest_5m_close_ms >= current_bucket_ms + interval_ms:
        return current_bucket_ms
    return _previous_bucket_start_ms(current_bucket_ms, normalized)


def _status_from_reason(*, latest_ms: int, expected_ms: int, stored_count: int, required_bars: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if latest_ms <= 0:
        reasons.append("missing_bars")
    if required_bars > 0 and stored_count < required_bars:
        reasons.append(f"bars<{required_bars}")
    if expected_ms > 0 and latest_ms > 0 and latest_ms < expected_ms:
        reasons.append("latest_stale")
    if not reasons:
        return "ready", []
    if "latest_stale" in reasons:
        return "stale", reasons
    return "missing", reasons


class BarFreshnessPlanner:
    """Builds per-symbol/interval repair plans from PocketBase `ibkr_bars`."""

    def __init__(self, pb_client, config=None, environment: str = "live"):
        self.pb = pb_client
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"

    def _close_delay_seconds(self, environment: str) -> int:
        cfg = self.config
        if cfg is not None and hasattr(cfg, "get_int_for_environment"):
            try:
                return max(0, int(cfg.get_int_for_environment("ibkr_official_5m_close_delay_sec", environment, DEFAULT_CLOSE_DELAY_SECONDS)))
            except Exception:
                return DEFAULT_CLOSE_DELAY_SECONDS
        return DEFAULT_CLOSE_DELAY_SECONDS

    def _get_latest_row(self, environment: str, symbol: str, interval: str) -> dict:
        if self.pb is None:
            return {}
        filter_text = (
            f'symbol = "{_escape_pb_filter(symbol)}" && '
            f'interval = "{_escape_pb_filter(interval)}" && '
            f'{build_bar_environment_filter(environment)}'
        )
        try:
            if hasattr(self.pb, "get_first_record"):
                return self.pb.get_first_record("ibkr_bars", filter=filter_text, sort="-bar_time_ms") or {}
            if hasattr(self.pb, "get_all_records"):
                rows = self.pb.get_all_records("ibkr_bars", filter=filter_text, sort="-bar_time_ms", max_pages=1) or []
                return dict(rows[0]) if rows else {}
        except Exception:
            return {}
        return {}

    def _count_rows(self, environment: str, symbol: str, interval: str, required_bars: int) -> int:
        if self.pb is None or not hasattr(self.pb, "get_all_records"):
            return 0
        pages = 1
        if required_bars > 0:
            pages = max(1, min(20, (int(required_bars) + 199) // 200))
        filter_text = (
            f'symbol = "{_escape_pb_filter(symbol)}" && '
            f'interval = "{_escape_pb_filter(interval)}" && '
            f'{build_bar_environment_filter(environment)}'
        )
        try:
            return len(self.pb.get_all_records("ibkr_bars", filter=filter_text, sort="-bar_time_ms", max_pages=pages) or [])
        except Exception:
            return 0

    def _expected_closed_ms(
        self,
        environment: str,
        interval: str,
        latest_5m_ms: int,
        *,
        now_ms: int | None = None,
        delay_seconds: int | None = None,
    ) -> int:
        normalized = normalize_interval(interval)
        delay = self._close_delay_seconds(environment) if delay_seconds is None else max(0, int(delay_seconds or 0))
        expected_5m_ms = latest_expected_extended_5m_ms(now_ms=now_ms, delay_seconds=delay)
        if normalized == "5m":
            return expected_5m_ms
        if latest_5m_ms > 0:
            return expected_closed_ms_from_latest_5m(latest_5m_ms, normalized)
        return expected_closed_ms_from_latest_5m(expected_5m_ms, normalized)

    def plan_symbol(
        self,
        symbol: str,
        intervals: Iterable[str] | None = None,
        *,
        environment: str | None = None,
        required_bars: int = DEFAULT_REQUIRED_BARS,
        now_ms: int | None = None,
        delay_seconds: int | None = None,
    ) -> dict:
        runtime_environment = str(environment or self.environment or "live").strip().lower() or "live"
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_intervals = [normalize_interval(item) for item in (intervals or COMPUTE_INTERVALS) if str(item or "").strip()]
        normalized_intervals = list(dict.fromkeys(normalized_intervals))
        if not normalized_symbol:
            return {"ok": False, "error": "missing_symbol", "environment": runtime_environment, "symbol": "", "intervals": {}}

        latest_5m_row = self._get_latest_row(runtime_environment, normalized_symbol, "5m")
        latest_5m_ms = int((latest_5m_row or {}).get("bar_time_ms", 0) or 0)
        interval_payloads: dict[str, dict] = {}
        needs_repair_intervals: list[str] = []
        status_counts: dict[str, int] = {}
        conid = extract_conid_from_bar_row(latest_5m_row)

        for interval in normalized_intervals:
            row = latest_5m_row if interval == "5m" else self._get_latest_row(runtime_environment, normalized_symbol, interval)
            latest_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if conid <= 0:
                conid = extract_conid_from_bar_row(row)
            if int(required_bars or 0) > 0:
                stored_count = self._count_rows(runtime_environment, normalized_symbol, interval, int(required_bars or 0))
            else:
                stored_count = 1 if latest_ms > 0 else 0
            expected_ms = self._expected_closed_ms(
                runtime_environment,
                interval,
                latest_5m_ms,
                now_ms=now_ms,
                delay_seconds=delay_seconds,
            )
            status, reasons = _status_from_reason(
                latest_ms=latest_ms,
                expected_ms=expected_ms,
                stored_count=stored_count,
                required_bars=max(0, int(required_bars or 0)),
            )
            needs_repair = status != "ready"
            if needs_repair:
                needs_repair_intervals.append(interval)
            status_counts[status] = int(status_counts.get(status, 0) or 0) + 1
            interval_payloads[interval] = {
                "environment": runtime_environment,
                "symbol": normalized_symbol,
                "interval": interval,
                "status": status,
                "ready": status == "ready",
                "needs_repair": needs_repair,
                "reason": ",".join(reasons),
                "reasons": reasons,
                "stored_count": stored_count,
                "required_bars": max(0, int(required_bars or 0)),
                "latest_stored_ms": latest_ms,
                "latest_stored_us": format_us_time(latest_ms) if latest_ms > 0 else "",
                "expected_closed_ms": expected_ms,
                "expected_closed_us": format_us_time(expected_ms) if expected_ms > 0 else "",
                "stale_by_ms": max(0, expected_ms - latest_ms) if expected_ms > 0 and latest_ms > 0 else 0,
                "authoritative_source": "ibkr_history_api",
            }

        aggregate_status = "ready" if not needs_repair_intervals else ("stale" if any(interval_payloads[i]["status"] == "stale" for i in needs_repair_intervals) else "missing")
        return {
            "ok": True,
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "status": aggregate_status,
            "ready": aggregate_status == "ready",
            "needs_repair": aggregate_status != "ready",
            "intervals": interval_payloads,
            "needs_repair_intervals": needs_repair_intervals,
            "status_counts": status_counts,
            "latest_5m_ms": latest_5m_ms,
            "latest_5m_us": format_us_time(latest_5m_ms) if latest_5m_ms > 0 else "",
            "conid": conid,
            "checked_at_ms": int(time.time() * 1000),
        }

    def plan_symbols(
        self,
        symbols: Iterable[str],
        intervals: Iterable[str] | None = None,
        *,
        environment: str | None = None,
        required_bars: int = DEFAULT_REQUIRED_BARS,
        now_ms: int | None = None,
        delay_seconds: int | None = None,
    ) -> dict:
        rows = []
        for symbol in sorted({str(item or "").strip().upper() for item in (symbols or []) if str(item or "").strip()}):
            rows.append(
                self.plan_symbol(
                    symbol,
                    intervals,
                    environment=environment,
                    required_bars=required_bars,
                    now_ms=now_ms,
                    delay_seconds=delay_seconds,
                )
            )
        incomplete = [row for row in rows if bool(row.get("needs_repair"))]
        return {
            "ok": True,
            "environment": str(environment or self.environment or "live").strip().lower() or "live",
            "total": len(rows),
            "ready": len(rows) - len(incomplete),
            "incomplete": len(incomplete),
            "items": rows,
            "incomplete_symbols": [str(row.get("symbol") or "") for row in incomplete],
        }
