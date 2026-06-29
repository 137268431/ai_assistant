"""
Timeframe and timestamp helpers shared by the IBKR market-data pipeline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict

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

EXTENDED_OPEN_MINUTE = 4 * 60
REGULAR_OPEN_MINUTE = 9 * 60 + 30
REGULAR_CLOSE_MINUTE = 16 * 60
EARLY_CLOSE_MINUTE = 13 * 60
EXTENDED_CLOSE_MINUTE = 20 * 60
EARLY_EXTENDED_CLOSE_MINUTE = 17 * 60

SYMBOL_EXTENDED_CLOSE_MINUTES = {
    "VIX": EARLY_EXTENDED_CLOSE_MINUTE,
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
    "sd_squeeze_breakout_long": "_sqbrk_L",
    "sd_squeeze_breakout_short": "_sqbrk_S",
    "vwap_trend_pullback_long": "_vwappb_L",
    "vwap_trend_pullback_short": "_vwappb_S",
    "sd_trend_continuation_long": "_trend_U",
    "sd_mr_reversal_long": "_mr_L",
    "sd_mr_reversal_short": "_mr_U",
    "sd_trend_continuation_short": "_trend_L",
}

MARKET_SESSION_LABELS = {
    "closed": "closed",
    "premarket": "premarket",
    "regular": "regular",
    "close_transition": "close_transition",
    "afterhours": "afterhours",
    "overnight": "overnight",
}


def _normalize_market_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.astimezone(ET).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return ms_to_et(int(value)).date()
    text = str(value or "").strip()
    if text:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    return datetime.now(ET).date()


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def _easter_date(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    holidays: set[date] = set()
    for observed in (
        _observed_fixed_holiday(year, 1, 1),
        _observed_fixed_holiday(year + 1, 1, 1),
    ):
        if observed.year == year:
            holidays.add(observed)
    holidays.update(
        {
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            _easter_date(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed_fixed_holiday(year, 7, 4),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed_fixed_holiday(year, 12, 25),
        }
    )
    if year >= 2022:
        observed_juneteenth = _observed_fixed_holiday(year, 6, 19)
        if observed_juneteenth.year == year:
            holidays.add(observed_juneteenth)
    return holidays


def is_nyse_trading_day(value: Any) -> bool:
    day = _normalize_market_date(value)
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def is_nyse_early_close_day(value: Any) -> bool:
    day = _normalize_market_date(value)
    if not is_nyse_trading_day(day):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    if day.month == 12 and day.day == 24:
        return True
    if day.month == 7 and day.day == 3:
        return True
    return False


def regular_close_minute_for_date(value: Any) -> int:
    return EARLY_CLOSE_MINUTE if is_nyse_early_close_day(value) else REGULAR_CLOSE_MINUTE


def extended_close_minute_for_date(value: Any) -> int:
    return EARLY_EXTENDED_CLOSE_MINUTE if is_nyse_early_close_day(value) else EXTENDED_CLOSE_MINUTE


def extended_close_minute_for_symbol(symbol: Any, value: Any) -> int:
    normal_close = extended_close_minute_for_date(value)
    override = SYMBOL_EXTENDED_CLOSE_MINUTES.get(str(symbol or "").strip().upper())
    if override is None:
        return normal_close
    return min(int(override), int(normal_close))


def previous_trading_day(value: Any) -> date:
    cursor = _normalize_market_date(value) - timedelta(days=1)
    while not is_nyse_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _market_dt_for_minute(day: date, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=ET) + timedelta(minutes=int(minute))


def market_date_start_ms(value: Any) -> int:
    day = _normalize_market_date(value)
    return int(datetime(day.year, day.month, day.day, tzinfo=ET).timestamp() * 1000)


def regular_session_close_ms_for_date(value: Any) -> int:
    day = _normalize_market_date(value)
    return int(_market_dt_for_minute(day, regular_close_minute_for_date(day)).timestamp() * 1000)


def extended_session_open_ms_for_date(value: Any) -> int:
    day = _normalize_market_date(value)
    return int(_market_dt_for_minute(day, EXTENDED_OPEN_MINUTE).timestamp() * 1000)


def extended_session_close_ms_for_date(value: Any, symbol: Any = "") -> int:
    day = _normalize_market_date(value)
    close_minute = extended_close_minute_for_symbol(symbol, day) if str(symbol or "").strip() else extended_close_minute_for_date(day)
    return int(_market_dt_for_minute(day, close_minute).timestamp() * 1000)


def extended_session_close_ms_for_start(bar_time_ms: int, symbol: Any = "") -> int:
    return extended_session_close_ms_for_date(ms_to_et(int(bar_time_ms or 0)), symbol=symbol)


def latest_closed_daily_bucket_start_ms(now_ms: int) -> int:
    if int(now_ms or 0) <= 0:
        return 0
    current = ms_to_et(int(now_ms))
    day = current.date()
    if is_nyse_trading_day(day) and int(now_ms) >= extended_session_close_ms_for_date(day):
        return market_date_start_ms(day)
    return market_date_start_ms(previous_trading_day(day))


def previous_intraday_bucket_start_ms(bucket_ms: int, interval: str) -> int:
    normalized = normalize_interval(interval)
    if normalized == "1d":
        return market_date_start_ms(previous_trading_day(ms_to_et(int(bucket_ms or 0))))

    interval_minutes = interval_to_minutes(normalized)
    bucket_dt = ms_to_et(int(bucket_ms or 0))
    bucket_minute = bucket_dt.hour * 60 + bucket_dt.minute
    if bucket_minute <= EXTENDED_OPEN_MINUTE:
        previous_day = previous_trading_day(bucket_dt)
        previous_close = extended_close_minute_for_date(previous_day)
        last_start_minute = EXTENDED_OPEN_MINUTE + (
            max(0, previous_close - EXTENDED_OPEN_MINUTE - 1) // interval_minutes
        ) * interval_minutes
        return int(_market_dt_for_minute(previous_day, last_start_minute).timestamp() * 1000)
    return bucket_start_ms(int(bucket_ms) - 1, normalized)


def session_boundaries_for_date(value: Any) -> Dict[str, object]:
    day = _normalize_market_date(value)
    early_close = is_nyse_early_close_day(day)
    regular_close = regular_close_minute_for_date(day)
    extended_close = extended_close_minute_for_date(day)
    return {
        "market_date": day.isoformat(),
        "trading_day": is_nyse_trading_day(day),
        "early_close": early_close,
        "extended_open_minute": EXTENDED_OPEN_MINUTE,
        "regular_open_minute": REGULAR_OPEN_MINUTE,
        "regular_close_minute": regular_close,
        "extended_close_minute": extended_close,
        "extended_open_ms": extended_session_open_ms_for_date(day),
        "regular_close_ms": regular_session_close_ms_for_date(day),
        "extended_close_ms": extended_session_close_ms_for_date(day),
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


def bar_close_ms(bar_time_ms: int, interval: str, symbol: Any = "") -> int:
    normalized = normalize_interval(interval)
    if normalized == "1d":
        return extended_session_close_ms_for_start(int(bar_time_ms), symbol=symbol)

    close_ms = int(bar_time_ms) + interval_to_ms(normalized)
    dt = ms_to_et(int(bar_time_ms))
    minute = dt.hour * 60 + dt.minute
    session_close_minute = extended_close_minute_for_symbol(symbol, dt) if str(symbol or "").strip() else extended_close_minute_for_date(dt)
    if EXTENDED_OPEN_MINUTE <= minute < session_close_minute:
        return min(close_ms, extended_session_close_ms_for_date(dt, symbol=symbol))
    return close_ms


def build_bar_close_timestamps(bar_time_ms: int, interval: str, symbol: Any = "") -> Dict[str, object]:
    normalized = normalize_interval(interval)
    close_ms = bar_close_ms(bar_time_ms, normalized, symbol=symbol)
    payload: Dict[str, object] = {
        "bar_time_semantics": "start",
        "bar_close_time_ms": close_ms,
        "bar_close_us_time": format_us_time(close_ms),
        "bar_close_cn_time": format_cn_time(close_ms),
    }
    if normalized == "1d":
        regular_close_ms = regular_session_close_ms_for_date(ms_to_et(int(bar_time_ms)))
        payload.update(
            {
                "session_scope": "extended",
                "session_start_us_time": format_us_time(extended_session_open_ms_for_date(ms_to_et(int(bar_time_ms)))),
                "regular_close_time_ms": regular_close_ms,
                "regular_close_us_time": format_us_time(regular_close_ms),
                "extended_close_time_ms": close_ms,
                "extended_close_us_time": format_us_time(close_ms),
            }
        )
        if str(symbol or "").strip().upper() in SYMBOL_EXTENDED_CLOSE_MINUTES:
            payload["symbol_close_override"] = str(symbol or "").strip().upper()
    return payload


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
    if not is_nyse_trading_day(dt):
        return "closed"
    regular_close = regular_close_minute_for_date(dt)
    extended_close = extended_close_minute_for_date(dt)
    if minutes < EXTENDED_OPEN_MINUTE or minutes >= extended_close:
        return "closed"
    if minutes < REGULAR_OPEN_MINUTE:
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
    minutes = dt.hour * 60 + dt.minute
    if not is_nyse_trading_day(dt):
        if dt.weekday() == 6 and minutes >= EXTENDED_CLOSE_MINUTE:
            return "overnight"
        return "closed"

    regular_close = regular_close_minute_for_date(dt)
    extended_close = extended_close_minute_for_date(dt)
    if minutes < EXTENDED_OPEN_MINUTE:
        return "overnight"
    if minutes < REGULAR_OPEN_MINUTE:
        return "premarket"
    if minutes < regular_close:
        return "regular"
    if minutes < min(regular_close + 10, extended_close):
        return "close_transition"
    if minutes < extended_close:
        return "afterhours"
    return "overnight"


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
        "is_open": kind in {"premarket", "regular", "close_transition", "afterhours", "overnight"},
        "is_late_session": kind in {"close_transition", "afterhours", "overnight"},
        "requires_live_5m": kind in {"premarket", "regular", "close_transition", "afterhours"},
    }


def bucket_start_ms(bar_time_ms: int, interval: str) -> int:
    normalized = normalize_interval(interval)
    dt = ms_to_et(bar_time_ms)

    if normalized == "1d":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    minutes = interval_to_minutes(normalized)
    total_minutes = dt.hour * 60 + dt.minute
    bucket_minutes = EXTENDED_OPEN_MINUTE + (
        (total_minutes - EXTENDED_OPEN_MINUTE) // minutes
    ) * minutes
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
    if normalized == "1d":
        return latest_closed_daily_bucket_start_ms(effective_ms)
    if effective_ms <= interval_to_ms(normalized):
        return 0
    return bucket_start_ms(effective_ms - interval_to_ms(normalized), normalized)


def build_signal_id(symbol: str, bar_time_ms: int, signal_type: str) -> str:
    dt = ms_to_et(bar_time_ms)
    prefix = f"{str(symbol or '').upper()}_{dt.strftime('%Y%m%d_%H%M')}"
    return prefix + SIGNAL_SUFFIX_MAP.get(signal_type, "")
