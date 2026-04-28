"""
Timeframe and timestamp helpers shared by the IBKR market-data pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from ibkr_compute.core.time_utils import CN, ET

COMPUTE_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
HIGHER_INTERVALS = ("15m", "30m", "1h", "4h", "1d")

INTERVAL_MINUTES = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

CHART_TF_MAP = {
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

SIGNAL_SUFFIX_MAP = {
    "trend_sdUpper": "_trend_U",
    "mr_sdLower": "_mr_L",
    "mr_sdUpper": "_mr_U",
    "trend_sdLower": "_trend_L",
}

MARKET_SESSION_LABELS = {
    "closed": "closed",
    "regular": "regular",
    "close_transition": "close_transition",
    "afterhours": "afterhours",
}


def normalize_interval(value: str) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
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
    return mapping.get(lowered, lowered or "5m")


def interval_to_minutes(interval: str) -> int:
    normalized = normalize_interval(interval)
    return INTERVAL_MINUTES[normalized]


def interval_to_ms(interval: str) -> int:
    return interval_to_minutes(interval) * 60 * 1000


def interval_to_chart_tf(interval: str) -> str:
    normalized = normalize_interval(interval)
    return CHART_TF_MAP[normalized]


def ms_to_et(bar_time_ms: int) -> datetime:
    return datetime.fromtimestamp(int(bar_time_ms) / 1000, ET)


def format_us_time(bar_time_ms: int) -> str:
    return ms_to_et(bar_time_ms).strftime("%Y-%m-%d %H:%M:%S")


def format_cn_time(bar_time_ms: int) -> str:
    return ms_to_et(bar_time_ms).astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")


def bar_close_ms(bar_time_ms: int, interval: str) -> int:
    return int(bar_time_ms) + interval_to_ms(interval)


def build_bar_close_timestamps(bar_time_ms: int, interval: str) -> Dict[str, object]:
    normalized = normalize_interval(interval)
    close_ms = bar_close_ms(bar_time_ms, normalized)
    return {
        "bar_time_semantics": "start",
        "bar_close_time_ms": close_ms,
        "bar_close_us_time": format_us_time(close_ms),
        "bar_close_cn_time": format_cn_time(close_ms),
    }


def build_runtime_timestamps(now: datetime | None = None) -> Dict[str, object]:
    current = now.astimezone(ET) if now else datetime.now(ET)
    current_ms = int(current.timestamp() * 1000)
    return {
        "computed_at_ms": current_ms,
        "computed_at_us": current.strftime("%Y-%m-%d %H:%M:%S"),
        "computed_at_cn": current.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
    }


def classify_session(us_time: str = "", bar_time_ms: int | None = None) -> str:
    if us_time:
        dt = datetime.strptime(us_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    elif bar_time_ms is not None:
        dt = ms_to_et(bar_time_ms)
    else:
        dt = datetime.now(ET)

    minutes = dt.hour * 60 + dt.minute
    regular_open = 9 * 60 + 30
    regular_close = 16 * 60
    if minutes < regular_open:
        return "premarket"
    if minutes >= regular_close:
        return "afterhours"
    return "regular"


def _coerce_et_datetime(
    now: datetime | None = None,
    *,
    us_time: str = "",
    bar_time_ms: int | None = None,
) -> datetime:
    if us_time:
        return datetime.strptime(us_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    if bar_time_ms is not None:
        return ms_to_et(bar_time_ms)
    return now.astimezone(ET) if now else datetime.now(ET)


def classify_market_session_kind(
    now: datetime | None = None,
    *,
    us_time: str = "",
    bar_time_ms: int | None = None,
) -> str:
    dt = _coerce_et_datetime(now, us_time=us_time, bar_time_ms=bar_time_ms)
    if dt.weekday() >= 5:
        return "closed"

    minutes = dt.hour * 60 + dt.minute
    if minutes < (9 * 60 + 40):
        return "closed"
    if minutes < (16 * 60):
        return "regular"
    if minutes < (16 * 60 + 10):
        return "close_transition"
    if minutes < (20 * 60):
        return "afterhours"
    return "closed"


def build_market_session_snapshot(
    now: datetime | None = None,
    *,
    us_time: str = "",
    bar_time_ms: int | None = None,
) -> Dict[str, object]:
    dt = _coerce_et_datetime(now, us_time=us_time, bar_time_ms=bar_time_ms)
    kind = classify_market_session_kind(dt)
    return {
        "kind": kind,
        "label": MARKET_SESSION_LABELS.get(kind, kind or "closed"),
        "us_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "cn_time": dt.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": dt.weekday(),
        "minutes": dt.hour * 60 + dt.minute,
        "is_open": kind in {"regular", "close_transition", "afterhours"},
        "is_late_session": kind in {"close_transition", "afterhours"},
        "requires_live_5m": kind in {"regular", "close_transition", "afterhours"},
    }


def bucket_start_ms(bar_time_ms: int, interval: str) -> int:
    normalized = normalize_interval(interval)
    dt = ms_to_et(bar_time_ms)

    if normalized == "1d":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    minutes = interval_to_minutes(normalized)
    total_minutes = dt.hour * 60 + dt.minute
    bucket_minutes = total_minutes - (total_minutes % minutes)
    start = dt.replace(
        hour=bucket_minutes // 60,
        minute=bucket_minutes % 60,
        second=0,
        microsecond=0,
    )
    return int(start.timestamp() * 1000)


def latest_safe_closed_bucket_ms(
    interval: str,
    *,
    delay_seconds: float = 0.0,
    now_ms: int | None = None,
    now: datetime | None = None,
) -> int:
    normalized = normalize_interval(interval)
    effective_ms = int(now_ms or 0)
    if effective_ms <= 0:
        current = now.astimezone(ET) if now else datetime.now(ET)
        effective_ms = int(current.timestamp() * 1000)
    effective_ms -= max(0, int(float(delay_seconds or 0.0) * 1000))
    if effective_ms <= interval_to_ms(normalized):
        return 0
    return bucket_start_ms(effective_ms - interval_to_ms(normalized), normalized)


def build_signal_id(symbol: str, bar_time_ms: int, signal_type: str) -> str:
    dt = ms_to_et(bar_time_ms)
    prefix = f"{str(symbol or '').upper()}_{dt.strftime('%Y%m%d_%H%M')}"
    return prefix + SIGNAL_SUFFIX_MAP.get(signal_type, "")
