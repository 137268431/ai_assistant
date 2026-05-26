from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ibkr_compute.core.time_utils import CN, ET
from ibkr_compute.market.timeframe_utils import (
    EXTENDED_OPEN_MINUTE,
    REGULAR_CLOSE_MINUTE,
    REGULAR_OPEN_MINUTE,
    extended_close_minute_for_date,
    is_nyse_trading_day,
    regular_close_minute_for_date,
)

IBKR_SCHEDULE_SOURCE = "ibkr_schedule"
LOCAL_NYSE_FALLBACK_SOURCE = "local_nyse_fallback"
DEFAULT_CALENDAR_SYMBOL = "SPY"
DEFAULT_CALENDAR_EXCHANGE = "SMART"
DEFAULT_CALENDAR_SEC_TYPE = "STK"
MARKET_SESSION_LABELS_ZH = {
    "closed": "闭市",
    "premarket": "盘前",
    "regular": "盘中",
    "close_transition": "盘后过渡",
    "afterhours": "盘后",
}


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


def _format_clock(value: datetime | None) -> str:
    return value.astimezone(ET).strftime("%H:%M") if value else ""


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
        "open_beijing": _format_cn(start_dt),
        "close_beijing": _format_cn(end_dt),
        "open_ts": start_dt.isoformat(),
        "close_ts": end_dt.isoformat(),
    }


def _datetime_from_value(value: Any, *, fallback_tz: ZoneInfo = ET) -> datetime | None:
    text = _to_text(value)
    if not text:
        return None
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=fallback_tz)
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=fallback_tz)
    except Exception:
        return None


def _interval_open_dt(interval: dict[str, Any]) -> datetime | None:
    return _datetime_from_value(interval.get("open_ts")) or _datetime_from_value(interval.get("open_us"))


def _interval_close_dt(interval: dict[str, Any]) -> datetime | None:
    return _datetime_from_value(interval.get("close_ts")) or _datetime_from_value(interval.get("close_us"))


def _day_open_dt(snapshot: dict[str, Any]) -> datetime | None:
    intervals = [item for item in snapshot.get("intervals") or [] if isinstance(item, dict)]
    if intervals:
        return _interval_open_dt(intervals[0])
    return _datetime_from_value(snapshot.get("open_us"))


def _day_close_dt(snapshot: dict[str, Any]) -> datetime | None:
    intervals = [item for item in snapshot.get("intervals") or [] if isinstance(item, dict)]
    if intervals:
        return _interval_close_dt(intervals[-1])
    return _datetime_from_value(snapshot.get("close_us"))


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


def _session_window_fields(
    *,
    regular_day: dict[str, Any],
    extended_day: dict[str, Any],
) -> dict[str, Any]:
    regular_open = _day_open_dt(regular_day)
    regular_close = _day_close_dt(regular_day)
    extended_open = _day_open_dt(extended_day) or regular_open
    extended_close = _day_close_dt(extended_day) or regular_close
    return {
        "open_us": _format_us(regular_open),
        "close_us": _format_us(regular_close),
        "open_beijing": _format_cn(regular_open),
        "close_beijing": _format_cn(regular_close),
        "regular_open_us": _format_us(regular_open),
        "regular_close_us": _format_us(regular_close),
        "regular_open_beijing": _format_cn(regular_open),
        "regular_close_beijing": _format_cn(regular_close),
        "extended_open_us": _format_us(extended_open),
        "extended_close_us": _format_us(extended_close),
        "extended_open_beijing": _format_cn(extended_open),
        "extended_close_beijing": _format_cn(extended_close),
        "regular_intervals": list(regular_day.get("intervals") or []),
        "extended_intervals": list(extended_day.get("intervals") or []),
        "intervals": list(regular_day.get("intervals") or []),
    }


def _market_session_kind_from_windows(
    *,
    now: datetime,
    market_date: str,
    is_closed: bool,
    regular_open: datetime | None,
    regular_close: datetime | None,
    extended_open: datetime | None,
    extended_close: datetime | None,
) -> str:
    if is_closed:
        return "closed"
    et_now = now.astimezone(ET)
    if market_date and et_now.date().isoformat() != market_date:
        return "closed"
    if regular_open is None or regular_close is None:
        return "closed"
    extended_open = extended_open or regular_open
    extended_close = extended_close or regular_close
    if et_now < extended_open or et_now >= extended_close:
        return "closed"
    if et_now < regular_open:
        return "premarket"
    if et_now < regular_close:
        return "regular"
    transition_end = min(regular_close + timedelta(minutes=10), extended_close)
    if et_now < transition_end:
        return "close_transition"
    return "afterhours"


def build_market_session_from_calendar(snapshot: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if now is None:
        current = datetime.now(ET)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=ET)
    else:
        current = now.astimezone(ET)
    session = dict(snapshot.get("session") or {})
    regular_open = _datetime_from_value(session.get("regular_open_us") or session.get("open_us"))
    regular_close = _datetime_from_value(session.get("regular_close_us") or session.get("close_us"))
    extended_open = _datetime_from_value(session.get("extended_open_us")) or regular_open
    extended_close = _datetime_from_value(session.get("extended_close_us")) or regular_close
    kind = _market_session_kind_from_windows(
        now=current,
        market_date=_to_text(snapshot.get("market_date")),
        is_closed=bool(snapshot.get("is_closed")),
        regular_open=regular_open,
        regular_close=regular_close,
        extended_open=extended_open,
        extended_close=extended_close,
    )
    return {
        "kind": kind,
        "label": kind,
        "label_zh": MARKET_SESSION_LABELS_ZH.get(kind, kind or "闭市"),
        "display_label": MARKET_SESSION_LABELS_ZH.get(kind, kind or "闭市"),
        "source": _to_text(snapshot.get("source")),
        "source_error": _to_text(snapshot.get("source_error")),
        "market_date": _to_text(snapshot.get("market_date")),
        "us_time": current.strftime("%Y-%m-%d %H:%M:%S"),
        "cn_time": current.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": current.weekday(),
        "minutes": current.hour * 60 + current.minute,
        "is_open": kind in {"premarket", "regular", "close_transition", "afterhours"},
        "is_late_session": kind in {"close_transition", "afterhours"},
        "requires_live_5m": kind in {"premarket", "regular", "close_transition", "afterhours"},
        "regular_open_us": _format_us(regular_open),
        "regular_close_us": _format_us(regular_close),
        "regular_open_beijing": _format_cn(regular_open),
        "regular_close_beijing": _format_cn(regular_close),
        "extended_open_us": _format_us(extended_open),
        "extended_close_us": _format_us(extended_close),
        "extended_open_beijing": _format_cn(extended_open),
        "extended_close_beijing": _format_cn(extended_close),
        "regular_window_us": (
            f"{_format_clock(regular_open)}-{_format_clock(regular_close)} ET"
            if regular_open and regular_close else ""
        ),
        "extended_window_us": (
            f"{_format_clock(extended_open)}-{_format_clock(extended_close)} ET"
            if extended_open and extended_close else ""
        ),
        "next_open_us": _to_text(snapshot.get("next_open_us")),
        "next_open_beijing": _to_text(snapshot.get("next_open_beijing")),
    }


def build_ibkr_calendar_snapshot(
    contract: dict[str, Any],
    *,
    market_date: Any,
    symbol: str = DEFAULT_CALENDAR_SYMBOL,
    exchange: str = DEFAULT_CALENDAR_EXCHANGE,
    sec_type: str = DEFAULT_CALENDAR_SEC_TYPE,
    now: datetime | None = None,
) -> dict[str, Any]:
    target_date = _date_key(market_date)
    if not target_date:
        return {"ok": False, "error": "invalid_market_date", "source": IBKR_SCHEDULE_SOURCE}
    details = dict(contract or {})
    liquid_hours_text = _to_text(details.get("liquid_hours") or details.get("liquidHours"))
    trading_hours_text = _to_text(details.get("trading_hours") or details.get("tradingHours"))
    time_zone_id = details.get("time_zone_id") or details.get("timeZoneId")
    regular_days = parse_ibkr_trading_hours(liquid_hours_text, time_zone_id=time_zone_id)
    extended_days = parse_ibkr_trading_hours(trading_hours_text, time_zone_id=time_zone_id)
    days = regular_days or extended_days
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
    regular_day = dict((regular_days or extended_days).get(target_date) or {})
    extended_day = dict((extended_days or regular_days).get(target_date) or {})
    day_snapshot = regular_day or extended_day
    if not day_snapshot:
        return {
            "ok": False,
            "error": "ibkr_schedule_date_unavailable",
            "source": IBKR_SCHEDULE_SOURCE,
            "available_dates": sorted(set(regular_days) | set(extended_days)),
            "market_date": target_date,
            "symbol": _to_text(details.get("symbol") or symbol).upper(),
            "exchange": _to_text(details.get("exchange") or exchange).upper(),
            "sec_type": _to_text(details.get("sec_type") or sec_type).upper(),
        }
    session = _session_window_fields(regular_day=regular_day, extended_day=extended_day)
    schedule_parts = []
    if liquid_hours_text:
        schedule_parts.append("liquid_hours")
    if trading_hours_text:
        schedule_parts.append("trading_hours")
    next_open = day_snapshot if day_snapshot.get("is_trading_day") else _next_open_from_days(regular_days or days, target_date)
    payload = {
        "ok": True,
        "source": IBKR_SCHEDULE_SOURCE,
        "market_date": target_date,
        "symbol": _to_text(details.get("symbol") or symbol).upper(),
        "conid": int(details.get("conid") or 0),
        "exchange": _to_text(details.get("exchange") or exchange).upper(),
        "primary_exchange": _to_text(details.get("primary_exchange")),
        "sec_type": _to_text(details.get("sec_type") or sec_type).upper(),
        "schedule_kind": "+".join(schedule_parts) or "unknown",
        "time_zone_id": _to_text(time_zone_id or "America/New_York"),
        "is_trading_day": bool(day_snapshot.get("is_trading_day")),
        "is_closed": bool(day_snapshot.get("is_closed")),
        "closed_reason": _to_text(day_snapshot.get("closed_reason")) if day_snapshot.get("is_closed") else "",
        "session": session,
        "next_open_us": _to_text(next_open.get("open_us")),
        "next_open_beijing": _to_text(next_open.get("open_cn")),
        "available_dates": sorted(set(regular_days) | set(extended_days)),
    }
    payload["market_session"] = build_market_session_from_calendar(payload, now=now)
    return payload


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
    now: datetime | None = None,
) -> dict[str, Any]:
    day = _parse_date(market_date)
    if day is None:
        return {"ok": False, "error": "invalid_market_date", "source": LOCAL_NYSE_FALLBACK_SOURCE}
    trading_day = bool(is_nyse_trading_day(day))
    close_minute = regular_close_minute_for_date(day) if trading_day else REGULAR_CLOSE_MINUTE
    extended_close_minute = extended_close_minute_for_date(day)
    open_dt = _session_datetime(day, REGULAR_OPEN_MINUTE) if trading_day else None
    close_dt = _session_datetime(day, close_minute) if trading_day else None
    extended_open_dt = _session_datetime(day, EXTENDED_OPEN_MINUTE) if trading_day else None
    extended_close_dt = _session_datetime(day, extended_close_minute) if trading_day else None
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
            "regular_open_us": _format_us(open_dt),
            "regular_close_us": _format_us(close_dt),
            "regular_open_beijing": _format_cn(open_dt),
            "regular_close_beijing": _format_cn(close_dt),
            "extended_open_us": _format_us(extended_open_dt),
            "extended_close_us": _format_us(extended_close_dt),
            "extended_open_beijing": _format_cn(extended_open_dt),
            "extended_close_beijing": _format_cn(extended_close_dt),
            "regular_intervals": [],
            "extended_intervals": [],
            "intervals": [],
        },
        "next_open_us": _format_us(next_open_dt),
        "next_open_beijing": _format_cn(next_open_dt),
    }
    if source_error:
        payload["source_error"] = _to_text(source_error)
    payload["market_session"] = build_market_session_from_calendar(payload, now=now)
    return payload


__all__ = [
    "DEFAULT_CALENDAR_EXCHANGE",
    "DEFAULT_CALENDAR_SEC_TYPE",
    "DEFAULT_CALENDAR_SYMBOL",
    "IBKR_SCHEDULE_SOURCE",
    "LOCAL_NYSE_FALLBACK_SOURCE",
    "build_ibkr_calendar_snapshot",
    "build_local_nyse_calendar_snapshot",
    "build_market_session_from_calendar",
    "parse_ibkr_trading_hours",
]
