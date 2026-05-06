"""Shared constants and helpers for historical IBKR backfill."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Dict, List, Optional, Sequence

from ibkr_compute.core.time_utils import ET

from .timeframe_utils import (
    classify_session,
    format_us_time,
    interval_to_ms,
    normalize_interval,
)

ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")

PERIOD_MAP = {
    "5m": ("4d", "5min"),
    "15m": ("10d", "15min"),
    "30m": ("20d", "30min"),
    "1h": ("40d", "1h"),
    "4h": ("120d", "4h"),
    "1d": ("2y", "1d"),
}


def _parse_intervals(value: str, fallback: Sequence[str]) -> List[str]:
    parsed = []
    for raw in str(value or "").split(","):
        normalized = normalize_interval(raw)
        if normalized and normalized not in parsed:
            parsed.append(normalized)
    return parsed or list(fallback)


DEFAULT_BACKFILL_INTERVALS = _parse_intervals(
    os.environ.get("IBKR_BACKFILL_INTERVALS", "5m"),
    fallback=("5m",),
)
REQUEST_SPACING_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_REQUEST_SPACING", "0.15")))
INTERVAL_DELAY_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_INTERVAL_DELAY", "0.10")))
MAX_CONCURRENT_REQUESTS = max(
    1,
    min(10, int(os.environ.get("IBKR_HISTORY_MAX_CONCURRENCY", "10"))),
)
MAX_RETRIES = max(0, int(os.environ.get("IBKR_HISTORY_MAX_RETRIES", "4")))
RETRY_BASE_DELAY_SECONDS = max(0.5, float(os.environ.get("IBKR_HISTORY_RETRY_BASE_DELAY", "2.0")))
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
DEFAULT_HISTORY_CLOSE_DELAY_SECONDS = max(
    1,
    int(os.environ.get("IBKR_OFFICIAL_5M_CLOSE_DELAY_SEC", "8")),
)
TRACE_RECENT_LIMIT = max(1, int(os.environ.get("IBKR_HISTORY_TRACE_RECENT_LIMIT", "20")))
TRACE_SLOW_SECONDS = max(0.1, float(os.environ.get("IBKR_HISTORY_TRACE_SLOW_SEC", "2.0")))

IB_DURATION_SUFFIX = {
    "s": "S",
    "d": "D",
    "w": "W",
    "m": "M",
    "y": "Y",
}
IB_BAR_SIZE_MAP = {
    "5min": "5 mins",
    "15min": "15 mins",
    "30min": "30 mins",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}
DEFAULT_CHUNK_DAYS = {
    "5m": 4,
    "15m": 14,
    "30m": 30,
    "1h": 60,
    "4h": 120,
}


def _to_ib_duration(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "1 D"
    if " " in text:
        return text.upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    suffix = "".join(ch for ch in text if ch.isalpha())
    if digits and suffix in IB_DURATION_SUFFIX:
        return f"{int(digits)} {IB_DURATION_SUFFIX[suffix]}"
    return text.upper()


def _parse_period_days(value: str) -> Optional[int]:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    suffix = "".join(ch for ch in text if ch.isalpha())
    if not digits:
        return None
    amount = max(1, int(digits))
    if suffix == "d":
        return amount
    if suffix == "w":
        return amount * 7
    if suffix == "m":
        return amount * 30
    if suffix == "y":
        return amount * 365
    return None


def _period_from_days(days: int) -> str:
    return f"{max(1, int(days or 0))}d"


def _format_ib_end_datetime(bar_time_ms: int) -> str:
    if int(bar_time_ms or 0) <= 0:
        return ""
    return datetime.fromtimestamp(int(bar_time_ms) / 1000, ET).strftime("%Y%m%d %H:%M:%S US/Eastern")


def _to_ib_bar_size(value: str) -> str:
    text = str(value or "").strip().lower()
    return IB_BAR_SIZE_MAP.get(text, value)


def _regular_session_gap_summary(
    rows: Sequence[Dict],
    interval: str,
    *,
    same_day_only: bool = False,
    example_limit: int = 4,
) -> Dict[str, object]:
    expected_ms = interval_to_ms(interval)
    gap_count = 0
    gap_examples: List[Dict[str, object]] = []

    def market_date(row: Dict) -> str:
        bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
        if bar_time_ms > 0:
            try:
                return datetime.fromtimestamp(bar_time_ms / 1000, ET).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass
        us_time = str(row.get("us_time", "") or "").strip()
        return us_time.split(" ", 1)[0] if us_time else ""

    for index in range(1, len(rows)):
        prev = rows[index - 1]
        curr = rows[index]
        if str(prev.get("session_type", "") or "").strip().lower() != "regular":
            continue
        if str(curr.get("session_type", "") or "").strip().lower() != "regular":
            continue

        prev_ms = int(prev.get("bar_time_ms", 0) or 0)
        curr_ms = int(curr.get("bar_time_ms", 0) or 0)
        if prev_ms <= 0 or curr_ms <= 0:
            continue
        if same_day_only and market_date(prev) != market_date(curr):
            continue

        delta_ms = curr_ms - prev_ms
        if delta_ms <= expected_ms:
            continue

        gap_count += 1
        if len(gap_examples) < example_limit:
            gap_examples.append({
                "prev_us_time": str(prev.get("us_time", "") or ""),
                "next_us_time": str(curr.get("us_time", "") or ""),
                "missing_points": max(int(round(delta_ms / expected_ms)) - 1, 1),
            })

    return {
        "gap_count": gap_count,
        "gap_examples": gap_examples,
    }


def _regular_session_expected_bar_times(
    start_ms: int,
    end_ms: int,
    interval: str,
) -> List[int]:
    expected_ms = interval_to_ms(interval)
    if expected_ms <= 0 or start_ms <= 0 or end_ms <= 0 or start_ms > end_ms:
        return []

    expected_times: List[int] = []
    current_ms = start_ms
    while current_ms <= end_ms:
        if classify_session(format_us_time(current_ms), current_ms).strip().lower() == "regular":
            expected_times.append(current_ms)
        current_ms += expected_ms
    return expected_times
