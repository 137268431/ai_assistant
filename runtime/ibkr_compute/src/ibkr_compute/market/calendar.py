from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ibkr_compute.core.time_utils import CN, ET
from ibkr_compute.market.timeframe_utils import (
    REGULAR_CLOSE_MINUTE,
    REGULAR_OPEN_MINUTE,
    is_nyse_trading_day,
    regular_close_minute_for_date,
)

IBKR_SCHEDULE_SOURCE = "ibkr_schedule"
LOCAL_NYSE_FALLBACK_SOURCE = "local_nyse_fallback"
DEFAULT_CALENDAR_SYMBOL = "SPY"
DEFAULT_CALENDAR_EXCHANGE = "SMART"
DEFAULT_CALENDAR_SEC_TYPE = "STK"


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(ET).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = _to_text(value)
    if not text:
        return None
    for candidate in (text[:10], text[:8]):
        try:
            if len(candidate) == 10:
                return datetime.strptime(candidate, "%Y-%m-%d").date()
            if len(candidate) == 8 and candidate.isdigit():
                return datetime.strptime(candidate, "%Y%m%d").date()
        except Exception:
            continue
    return None


def _date_key(value: Any) -> str:
    day = _parse_date(value)
    return day.isoformat() if day else ""


def _format_us(value: datetime | None) -> str:
    return value.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _format_cn(value: datetime | None) -> str:
    return value.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _timezone(value: Any) -> ZoneInfo:
    text = _to_text(value) or "America/New_York"
    aliases = {
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "US/EASTERN": "America/New_York",
        "AMERICA/NEW_YORK": "America/New_York",
    }
    target = aliases.get(text.upper(), text)
    try:
        return ZoneInfo(target)
    except Exception:
        return ET


def _parse_endpoint(value: str, default_day: date, tz: ZoneInfo) -> datetime | None:
    text = _to_text(value)
    if not text:
        return None
    day = default_day
    time_text = text
    if ":" in text:
        prefix, suffix = text.rsplit(":", 1)
        parsed_day = _parse_date(prefix)
        if parsed_day:
            day = parsed_day
            time_text = suffix
    elif len(text) >= 12 and text[:8].isdigit() and text[8:12].isdigit():
        parsed_day = _parse_date(text[:8])
        if parsed_day:
            day = parsed_day
            time_text = text[8:12]
    time_text = time_text.replace(":", "")
    if len(time_text) < 4 or not time_text[:4].isdigit():
        return None
    hour = int(time_text[:2])
    minute = int(time_text[2:4])
    if hour > 23 or minute > 59:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _parse_interval(segment: str, default_day: date, tz: ZoneInfo) -> dict[str, str] | None:
    text = _to_text(segment)
    if not text or text.upper() == "CLOSED" or "-" not in text:
        return None
    start_text, end_text = text.split("-", 1)
    start_dt = _parse_endpoint(start_text, default_day, tz)
    end_dt = _parse_endpoint(end_text, default_day, tz)
    if start_dt is None or end_dt is None:
        return None
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return {
        "open_us": _format_us(start_dt),
        "close_us": _format_us(end_dt),
        "open_cn": _format_cn(start_dt),
        "close_cn": _format_cn(end_dt),
        "open_ts": start_dt.isoformat(),
        "close_ts": end_dt.isoformat(),
    }


def parse_ibkr_trading_hours(hours_text: Any, *, time_zone_id: Any = "America/New_York") -> dict[str, dict[str, Any]]:
    """Parse IBKR ContractDetails tradingHours/liquidHours into day snapshots."""

    text = _to_text(hours_text)
    if not text:
        return {}
    tz = _timezone(time_zone_id)
    days: dict[str, dict[str, Any]] = {}
    for raw_entry in text.split(";"):
        entry = _to_text(raw_entry)
        if not entry or ":" not in entry:
            continue
        raw_day, raw_schedule = entry.split(":", 1)
        day = _parse_date(raw_day)
        if day is None:
            continue
        key = day.isoformat()
        schedule = _to_text(raw_schedule)
        closed = not schedule or schedule.upper() == "CLOSED"
        intervals = (
            []
            if closed
            else [
                interval
                for interval in (_parse_interval(part, day, tz) for part in schedule.split(","))
                if interval is not None
            ]
        )
        if not intervals:
            closed = True
        first_open = intervals[0] if intervals else {}
        last_close = intervals[-1] if intervals else {}
        days[key] = {
            "date": key,
            "is_trading_day": not closed,
            "is_closed": closed,
            "closed_reason": "ibkr_closed" if closed else "",
            "open_us": _to_text(first_open.get("open_us")),
            "close_us": _to_text(last_close.get("close_us")),
            "open_cn": _to_text(first_open.get("open_cn")),
            "close_cn": _to_text(last_close.get("close_cn")),
            "intervals": intervals,
        }
    return days


def _next_open_from_days(days: dict[str, dict[str, Any]], market_date: str) -> dict[str, Any]:
    for key in sorted(days):
        item = dict(days.get(key) or {})
        if key > market_date and item.get("is_trading_day") and _to_text(item.get("open_us")):
            return item
    return {}


def build_ibkr_calendar_snapshot(
    contract: dict[str, Any],
    *,
    market_date: Any,
    symbol: str = DEFAULT_CALENDAR_SYMBOL,
    exchange: str = DEFAULT_CALENDAR_EXCHANGE,
    sec_type: str = DEFAULT_CALENDAR_SEC_TYPE,
) -> dict[str, Any]:
    target_date = _date_key(market_date)
    if not target_date:
        return {"ok": False, "error": "invalid_market_date", "source": IBKR_SCHEDULE_SOURCE}
    details = dict(contract or {})
    hours_text = _to_text(
        details.get("liquid_hours")
        or details.get("liquidHours")
        or details.get("trading_hours")
        or details.get("tradingHours")
    )
    hours_kind = (
        "liquid_hours"
        if _to_text(details.get("liquid_hours") or details.get("liquidHours"))
        else "trading_hours"
    )
    days = parse_ibkr_trading_hours(hours_text, time_zone_id=details.get("time_zone_id") or details.get("timeZoneId"))
    if not days:
        return {
            "ok": False,
            "error": "ibkr_schedule_unavailable",
            "source": IBKR_SCHEDULE_SOURCE,
            "market_date": target_date,
            "symbol": _to_text(details.get("symbol") or symbol).upper(),
            "exchange": _to_text(details.get("exchange") or exchange).upper(),
            "sec_type": _to_text(details.get("sec_type") or sec_type).upper(),
        }
    day_snapshot = dict(days.get(target_date) or {})
    if not day_snapshot:
        return {
            "ok": False,
            "error": "ibkr_schedule_date_unavailable",
            "source": IBKR_SCHEDULE_SOURCE,
            "available_dates": sorted(days),
            "market_date": target_date,
            "symbol": _to_text(details.get("symbol") or symbol).upper(),
            "exchange": _to_text(details.get("exchange") or exchange).upper(),
            "sec_type": _to_text(details.get("sec_type") or sec_type).upper(),
        }
    next_open = day_snapshot if day_snapshot.get("is_trading_day") else _next_open_from_days(days, target_date)
    return {
        "ok": True,
        "source": IBKR_SCHEDULE_SOURCE,
        "market_date": target_date,
        "symbol": _to_text(details.get("symbol") or symbol).upper(),
        "conid": int(details.get("conid") or 0),
        "exchange": _to_text(details.get("exchange") or exchange).upper(),
        "primary_exchange": _to_text(details.get("primary_exchange")),
        "sec_type": _to_text(details.get("sec_type") or sec_type).upper(),
        "schedule_kind": hours_kind,
        "time_zone_id": _to_text(details.get("time_zone_id") or details.get("timeZoneId") or "America/New_York"),
        "is_trading_day": bool(day_snapshot.get("is_trading_day")),
        "is_closed": bool(day_snapshot.get("is_closed")),
        "closed_reason": _to_text(day_snapshot.get("closed_reason")) if day_snapshot.get("is_closed") else "",
        "session": {
            "open_us": _to_text(day_snapshot.get("open_us")),
            "close_us": _to_text(day_snapshot.get("close_us")),
            "open_beijing": _to_text(day_snapshot.get("open_cn")),
            "close_beijing": _to_text(day_snapshot.get("close_cn")),
            "intervals": list(day_snapshot.get("intervals") or []),
        },
        "next_open_us": _to_text(next_open.get("open_us")),
        "next_open_beijing": _to_text(next_open.get("open_cn")),
        "available_dates": sorted(days),
    }


def _session_datetime(day: date, minute_of_day: int) -> datetime:
    hour, minute = divmod(int(minute_of_day), 60)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def _next_nyse_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(31):
        if is_nyse_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def build_local_nyse_calendar_snapshot(
    market_date: Any,
    *,
    symbol: str = DEFAULT_CALENDAR_SYMBOL,
    exchange: str = DEFAULT_CALENDAR_EXCHANGE,
    sec_type: str = DEFAULT_CALENDAR_SEC_TYPE,
    source_error: str = "",
) -> dict[str, Any]:
    day = _parse_date(market_date)
    if day is None:
        return {"ok": False, "error": "invalid_market_date", "source": LOCAL_NYSE_FALLBACK_SOURCE}
    trading_day = bool(is_nyse_trading_day(day))
    close_minute = regular_close_minute_for_date(day) if trading_day else REGULAR_CLOSE_MINUTE
    open_dt = _session_datetime(day, REGULAR_OPEN_MINUTE) if trading_day else None
    close_dt = _session_datetime(day, close_minute) if trading_day else None
    next_day = day if trading_day else _next_nyse_trading_day(day)
    next_open_dt = _session_datetime(next_day, REGULAR_OPEN_MINUTE)
    closed_reason = "" if trading_day else ("weekend" if day.weekday() >= 5 else "nyse_holiday")
    payload = {
        "ok": True,
        "source": LOCAL_NYSE_FALLBACK_SOURCE,
        "market_date": day.isoformat(),
        "symbol": _to_text(symbol).upper() or DEFAULT_CALENDAR_SYMBOL,
        "exchange": _to_text(exchange).upper() or DEFAULT_CALENDAR_EXCHANGE,
        "sec_type": _to_text(sec_type).upper() or DEFAULT_CALENDAR_SEC_TYPE,
        "time_zone_id": "America/New_York",
        "is_trading_day": trading_day,
        "is_closed": not trading_day,
        "closed_reason": closed_reason,
        "session": {
            "open_us": _format_us(open_dt),
            "close_us": _format_us(close_dt),
            "open_beijing": _format_cn(open_dt),
            "close_beijing": _format_cn(close_dt),
            "intervals": [],
        },
        "next_open_us": _format_us(next_open_dt),
        "next_open_beijing": _format_cn(next_open_dt),
    }
    if source_error:
        payload["source_error"] = _to_text(source_error)
    return payload


__all__ = [
    "DEFAULT_CALENDAR_EXCHANGE",
    "DEFAULT_CALENDAR_SEC_TYPE",
    "DEFAULT_CALENDAR_SYMBOL",
    "IBKR_SCHEDULE_SOURCE",
    "LOCAL_NYSE_FALLBACK_SOURCE",
    "build_ibkr_calendar_snapshot",
    "build_local_nyse_calendar_snapshot",
    "parse_ibkr_trading_hours",
]
