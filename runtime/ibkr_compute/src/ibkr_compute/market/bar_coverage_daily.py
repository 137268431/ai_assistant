"""Daily bar coverage ledger helpers shared by live scans and backtests."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import json
import sqlite3
from typing import Any, Iterable, Sequence

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.timeframe_utils import format_us_time, interval_to_ms, ms_to_et, normalize_interval

REGULAR_OPEN_MINUTE = 9 * 60 + 30
REGULAR_CLOSE_MINUTE = 16 * 60
EARLY_CLOSE_MINUTE = 13 * 60
EXTENDED_OPEN_MINUTE = 4 * 60
EXTENDED_CLOSE_MINUTE = 20 * 60
DAILY_COVERAGE_OK_STATUSES = {"ok", "repaired"}


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_date(value: Any) -> str:
    return str(value or "").strip()[:10]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _parse_date(value: Any) -> date:
    return datetime.strptime(_normalize_date(value), "%Y-%m-%d").date()


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
            _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _easter_date(year) - timedelta(days=2),  # Good Friday
            _last_weekday(year, 5, 0),  # Memorial Day
            _observed_fixed_holiday(year, 7, 4),
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
            _observed_fixed_holiday(year, 12, 25),
        }
    )
    if year >= 2022:
        observed_juneteenth = _observed_fixed_holiday(year, 6, 19)
        if observed_juneteenth.year == year:
            holidays.add(observed_juneteenth)
    return holidays


def is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def is_nyse_early_close_day(day: date) -> bool:
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


def regular_close_minute_for_date(day: date) -> int:
    return EARLY_CLOSE_MINUTE if is_nyse_early_close_day(day) else REGULAR_CLOSE_MINUTE


def minute_of_day(bar_time_ms: int) -> int:
    dt = ms_to_et(int(bar_time_ms))
    return dt.hour * 60 + dt.minute


def market_date_from_ms(bar_time_ms: int) -> str:
    return ms_to_et(int(bar_time_ms)).strftime("%Y-%m-%d")


def date_ms_bounds(market_date: str) -> tuple[int, int]:
    start = datetime.strptime(_normalize_date(market_date), "%Y-%m-%d").replace(tzinfo=ET)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def date_range_strings(date_from: str, date_to: str) -> list[str]:
    start = _parse_date(date_from)
    end = _parse_date(date_to)
    if end < start:
        return []
    output: list[str] = []
    cursor = start
    while cursor <= end:
        output.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return output


def trading_date_strings(date_from: str, date_to: str) -> list[str]:
    return [item for item in date_range_strings(date_from, date_to) if is_nyse_trading_day(_parse_date(item))]


def trading_date_strings_from_ms(start_ms: int, end_ms: int) -> list[str]:
    if int(start_ms or 0) <= 0 or int(end_ms or 0) <= 0 or int(end_ms) < int(start_ms):
        return []
    return trading_date_strings(market_date_from_ms(int(start_ms)), market_date_from_ms(int(end_ms)))


def expected_bar_times_for_date(market_date: str, interval: str = "5m", session_mode: str = "regular") -> list[int]:
    day = _parse_date(market_date)
    normalized_interval = normalize_interval(interval)
    interval_minutes = max(1, interval_to_ms(normalized_interval) // 60_000)
    session = str(session_mode or "regular").strip().lower() or "regular"
    if not is_nyse_trading_day(day):
        return []

    regular_close = regular_close_minute_for_date(day)
    start_dt = datetime(day.year, day.month, day.day, tzinfo=ET)
    expected: list[int] = []
    for minute in range(EXTENDED_OPEN_MINUTE, EXTENDED_CLOSE_MINUTE, interval_minutes):
        is_regular = REGULAR_OPEN_MINUTE <= minute < regular_close
        include = is_regular if session == "regular" else (EXTENDED_OPEN_MINUTE <= minute < EXTENDED_CLOSE_MINUTE and not is_regular)
        if include:
            bucket = start_dt + timedelta(minutes=minute)
            expected.append(int(bucket.timestamp() * 1000))
    return expected


def session_mode_for_bar_time(bar_time_ms: int) -> str:
    day = _parse_date(market_date_from_ms(bar_time_ms))
    minutes = minute_of_day(bar_time_ms)
    if REGULAR_OPEN_MINUTE <= minutes < regular_close_minute_for_date(day):
        return "regular"
    if EXTENDED_OPEN_MINUTE <= minutes < EXTENDED_CLOSE_MINUTE:
        return "extended"
    return "closed"


def _mask_hex(indexes: Iterable[int], width_bits: int) -> str:
    width = max(1, (max(0, int(width_bits or 0)) + 3) // 4)
    value = 0
    for index in indexes or []:
        if int(index) >= 0:
            value |= 1 << int(index)
    return format(value, f"0{width}x")


def _missing_windows(missing_times: Sequence[int], interval_ms: int) -> list[dict[str, Any]]:
    times = sorted({int(item) for item in missing_times if int(item or 0) > 0})
    if not times:
        return []
    windows: list[dict[str, Any]] = []
    start_ms = times[0]
    previous_ms = times[0]
    count = 1
    for value in times[1:]:
        if value == previous_ms + interval_ms:
            previous_ms = value
            count += 1
            continue
        windows.append(
            {
                "start_ms": start_ms,
                "end_ms": previous_ms,
                "start_us": format_us_time(start_ms),
                "end_us": format_us_time(previous_ms),
                "missing_count": count,
                "reason": "missing_expected_bar",
            }
        )
        start_ms = value
        previous_ms = value
        count = 1
    windows.append(
        {
            "start_ms": start_ms,
            "end_ms": previous_ms,
            "start_us": format_us_time(start_ms),
            "end_us": format_us_time(previous_ms),
            "missing_count": count,
            "reason": "missing_expected_bar",
        }
    )
    return windows


def _bad_ohlc(row: dict[str, Any]) -> bool:
    try:
        open_value = float(row.get("open") or 0)
        high_value = float(row.get("high") or 0)
        low_value = float(row.get("low") or 0)
        close_value = float(row.get("close") or 0)
    except (TypeError, ValueError):
        return True
    return bool(
        high_value <= 0
        or low_value <= 0
        or high_value < low_value
        or open_value < low_value
        or open_value > high_value
        or close_value < low_value
        or close_value > high_value
    )


def build_daily_coverage_row(
    *,
    symbol: str,
    environment: str,
    market_date: str,
    interval: str = "5m",
    session_mode: str = "regular",
    bars: Sequence[dict[str, Any]] | None = None,
    source: str = "manual_scan",
    checked_at: str = "",
    active_trade_symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    normalized_date = _normalize_date(market_date)
    normalized_session = str(session_mode or "regular").strip().lower() or "regular"
    if normalized_session not in {"regular", "extended"}:
        normalized_session = "regular"
    day = _parse_date(normalized_date)
    interval_ms = interval_to_ms(normalized_interval)
    expected_times = expected_bar_times_for_date(normalized_date, normalized_interval, normalized_session)
    expected_index = {value: index for index, value in enumerate(expected_times)}
    actual_rows = [dict(row or {}) for row in (bars or [])]
    actual_times = [int(row.get("bar_time_ms", 0) or 0) for row in actual_rows if int(row.get("bar_time_ms", 0) or 0) > 0]
    actual_set = set(actual_times)
    actual_indexes = sorted(expected_index[value] for value in actual_set if value in expected_index)
    missing_times = [value for value in expected_times if value not in actual_set]
    duplicate_count = max(0, len(actual_times) - len(actual_set))
    bad_rows = [row for row in actual_rows if _bad_ohlc(row)]
    windows = _missing_windows(missing_times, interval_ms)
    missing_count = len(missing_times)
    expected_count = len(expected_times)
    issue_count = missing_count + duplicate_count + len(bad_rows)
    status = "ok"
    if issue_count > 0:
        status = "hard_gap" if normalized_session == "regular" else "soft_gap"
    if expected_count == 0 and not is_nyse_trading_day(day):
        status = "ok"
    active_symbols = {_normalize_symbol(item) for item in (active_trade_symbols or []) if _normalize_symbol(item)}
    hard_gate = bool(normalized_session == "regular" and status == "hard_gap")
    if active_symbols:
        hard_gate = hard_gate and normalized_symbol in active_symbols
    first_bar_ms = min(actual_times) if actual_times else 0
    last_bar_ms = max(actual_times) if actual_times else 0
    checked_text = str(checked_at or datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S%z"))
    return {
        "environment": normalized_environment,
        "market_date": normalized_date,
        "symbol": normalized_symbol,
        "interval": normalized_interval,
        "session_mode": normalized_session,
        "status": status,
        "hard_gate": hard_gate,
        "needs_repair": bool(missing_count > 0 or (normalized_session == "regular" and len(bad_rows) > 0)),
        "expected_count": expected_count,
        "actual_count": len(actual_rows),
        "missing_count": missing_count,
        "gap_count": len(windows),
        "duplicate_count": duplicate_count,
        "bad_ohlc_count": len(bad_rows),
        "expected_start_ms": expected_times[0] if expected_times else 0,
        "expected_end_ms": expected_times[-1] if expected_times else 0,
        "first_bar_ms": first_bar_ms,
        "last_bar_ms": last_bar_ms,
        "last_checked_at": checked_text,
        "last_repair_at": "",
        "missing_windows": windows,
        "missing_examples": windows[:5],
        "repair_windows": list(windows),
        "expected_mask_hex": _mask_hex(range(expected_count), expected_count),
        "actual_mask_hex": _mask_hex(actual_indexes, expected_count),
        "missing_mask_hex": _mask_hex((expected_index[value] for value in missing_times), expected_count),
        "source": str(source or "manual_scan").strip() or "manual_scan",
        "extra": {
            "trading_day": is_nyse_trading_day(day),
            "early_close": is_nyse_early_close_day(day),
            "expected_scope": "regular_session" if normalized_session == "regular" else "extended_hours_only",
            "actual_aligned_count": len(actual_indexes),
            "bad_ohlc_examples": [
                {
                    "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                    "us_time": format_us_time(int(row.get("bar_time_ms", 0) or 0)) if int(row.get("bar_time_ms", 0) or 0) > 0 else "",
                }
                for row in bad_rows[:5]
            ],
        },
    }


def _symbol_placeholders(symbols: Sequence[str]) -> str:
    return ", ".join("?" for _ in symbols)


def fetch_bar_rows_grouped(
    conn: sqlite3.Connection,
    *,
    symbols: Sequence[str],
    environment: str,
    interval: str,
    date_from: str,
    date_to: str,
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    normalized_symbols = sorted({_normalize_symbol(item) for item in symbols or [] if _normalize_symbol(item)})
    if not normalized_symbols:
        return {}
    start_ms, _ = date_ms_bounds(date_from)
    _, end_ms = date_ms_bounds(date_to)
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    rows = conn.execute(
        f"""
        SELECT symbol, environment, interval, bar_time_ms, open, high, low, close
        FROM ibkr_bars
        WHERE symbol IN ({_symbol_placeholders(normalized_symbols)})
          AND environment = ?
          AND interval = ?
          AND bar_time_ms >= ?
          AND bar_time_ms <= ?
        ORDER BY symbol ASC, bar_time_ms ASC
        """,
        (*normalized_symbols, normalized_environment, normalized_interval, int(start_ms), int(end_ms)),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows or []:
        item = dict(row)
        bar_ms = int(item.get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        symbol = _normalize_symbol(item.get("symbol"))
        market_date = market_date_from_ms(bar_ms)
        session = session_mode_for_bar_time(bar_ms)
        if session not in {"regular", "extended"}:
            continue
        grouped[(symbol, market_date, session)].append(item)
    return grouped


def build_range_daily_coverage(
    conn: sqlite3.Connection,
    *,
    symbols: Sequence[str],
    environment: str,
    date_from: str,
    date_to: str,
    interval: str = "5m",
    session_modes: Sequence[str] = ("regular",),
    source: str = "manual_scan",
    date_filter: Iterable[str] | None = None,
    active_trade_symbols: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_symbols = sorted({_normalize_symbol(item) for item in symbols or [] if _normalize_symbol(item)})
    normalized_sessions = [str(item or "").strip().lower() for item in (session_modes or ("regular",))]
    normalized_sessions = [item for item in dict.fromkeys(normalized_sessions) if item in {"regular", "extended"}]
    if not normalized_sessions:
        normalized_sessions = ["regular"]
    all_dates = date_range_strings(date_from, date_to)
    allowed_dates = {_normalize_date(item) for item in date_filter or [] if _normalize_date(item)}
    if allowed_dates:
        all_dates = [item for item in all_dates if item in allowed_dates]
    if not normalized_symbols or not all_dates:
        return []
    grouped = fetch_bar_rows_grouped(
        conn,
        symbols=normalized_symbols,
        environment=environment,
        interval=interval,
        date_from=min(all_dates),
        date_to=max(all_dates),
    )
    checked_at = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S%z")
    rows: list[dict[str, Any]] = []
    for symbol in normalized_symbols:
        for market_date in all_dates:
            for session_mode in normalized_sessions:
                rows.append(
                    build_daily_coverage_row(
                        symbol=symbol,
                        environment=environment,
                        market_date=market_date,
                        interval=interval,
                        session_mode=session_mode,
                        bars=grouped.get((symbol, market_date, session_mode), []),
                        source=source,
                        checked_at=checked_at,
                        active_trade_symbols=active_trade_symbols,
                    )
                )
    return rows


def load_daily_coverage_rows(
    conn: sqlite3.Connection,
    *,
    symbols: Sequence[str],
    environment: str,
    date_from: str,
    date_to: str,
    interval: str = "5m",
    session_mode: str = "regular",
) -> list[dict[str, Any]]:
    normalized_symbols = sorted({_normalize_symbol(item) for item in symbols or [] if _normalize_symbol(item)})
    if not normalized_symbols:
        return []
    rows = conn.execute(
        f"""
        SELECT *
        FROM ibkr_bar_coverage_daily
        WHERE symbol IN ({_symbol_placeholders(normalized_symbols)})
          AND environment = ?
          AND interval = ?
          AND session_mode = ?
          AND market_date >= ?
          AND market_date <= ?
        ORDER BY symbol ASC, market_date ASC
        """,
        (
            *normalized_symbols,
            str(environment or "live").strip().lower() or "live",
            normalize_interval(interval),
            str(session_mode or "regular").strip().lower() or "regular",
            _normalize_date(date_from),
            _normalize_date(date_to),
        ),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows or []:
        item = dict(row)
        for field in ("missing_windows", "missing_examples", "repair_windows"):
            item[field] = _json_list(item.get(field))
        item["extra"] = _json_dict(item.get("extra"))
        items.append(item)
    return items


def clip_windows_to_range(windows: Sequence[dict[str, Any]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    clipped: list[dict[str, Any]] = []
    for raw in windows or []:
        window_start = max(int(start_ms), int((raw or {}).get("start_ms", 0) or 0))
        window_end = min(int(end_ms), int((raw or {}).get("end_ms", 0) or 0))
        if window_start <= 0 or window_end <= 0 or window_end < window_start:
            continue
        item = dict(raw or {})
        item["start_ms"] = window_start
        item["end_ms"] = window_end
        item["start_us"] = format_us_time(window_start)
        item["end_us"] = format_us_time(window_end)
        clipped.append(item)
    return clipped


def summarize_symbol_daily_coverage(
    *,
    symbol: str,
    rows: Sequence[dict[str, Any]],
    requested_start_ms: int,
    requested_end_ms: int,
    interval: str = "5m",
) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol(symbol)
    by_date = {_normalize_date(row.get("market_date")): dict(row or {}) for row in rows or [] if _normalize_symbol(row.get("symbol")) == normalized_symbol}
    expected_dates = trading_date_strings_from_ms(requested_start_ms, requested_end_ms)
    raw_windows: list[dict[str, Any]] = []
    missing_coverage_dates: list[str] = []
    duplicate_count = 0
    bad_ohlc_count = 0
    row_count = 0
    first_bar_ms = 0
    last_bar_ms = 0
    daily_status_counts: dict[str, int] = {}

    for market_date in expected_dates:
        row = by_date.get(market_date)
        if not row:
            expected_times = expected_bar_times_for_date(market_date, interval, "regular")
            windows = _missing_windows(
                [value for value in expected_times if int(requested_start_ms) <= value <= int(requested_end_ms)],
                interval_to_ms(interval),
            )
            raw_windows.extend(windows)
            missing_coverage_dates.append(market_date)
            continue
        status = str(row.get("status") or "").strip().lower() or "missing"
        daily_status_counts[status] = daily_status_counts.get(status, 0) + 1
        row_count += int(row.get("actual_count") or 0)
        duplicate_count += int(row.get("duplicate_count") or 0)
        bad_ohlc_count += int(row.get("bad_ohlc_count") or 0)
        first = int(row.get("first_bar_ms") or 0)
        last = int(row.get("last_bar_ms") or 0)
        if first > 0 and (first_bar_ms <= 0 or first < first_bar_ms):
            first_bar_ms = first
        if last > 0 and last > last_bar_ms:
            last_bar_ms = last
        raw_windows.extend(
            clip_windows_to_range(
                _json_list(row.get("repair_windows")) or _json_list(row.get("missing_windows")),
                requested_start_ms,
                requested_end_ms,
            )
        )

    needs_backfill = bool(raw_windows or missing_coverage_dates or duplicate_count > 0 or bad_ohlc_count > 0)
    reasons: list[str] = []
    if missing_coverage_dates:
        reasons.append("missing_daily_coverage")
    if raw_windows:
        reasons.append("daily_gaps")
    if duplicate_count > 0:
        reasons.append("duplicates")
    if bad_ohlc_count > 0:
        reasons.append("bad_ohlc")
    return {
        "symbol": normalized_symbol,
        "needs_backfill": needs_backfill,
        "reasons": reasons,
        "row_count": row_count,
        "gap_count": len(raw_windows),
        "first_bar_ms": first_bar_ms,
        "last_bar_ms": last_bar_ms,
        "first_bar_us": format_us_time(first_bar_ms) if first_bar_ms > 0 else "",
        "last_bar_us": format_us_time(last_bar_ms) if last_bar_ms > 0 else "",
        "requested_start_ms": int(requested_start_ms),
        "requested_end_ms": int(requested_end_ms),
        "requested_start_us": format_us_time(int(requested_start_ms)) if int(requested_start_ms) > 0 else "",
        "requested_end_us": format_us_time(int(requested_end_ms)) if int(requested_end_ms) > 0 else "",
        "repair_window_count": len(raw_windows),
        "repair_windows": raw_windows[:20],
        "repair_windows_truncated": max(0, len(raw_windows) - 20),
        "diagnostic_source": "daily_coverage",
        "daily_coverage": {
            "expected_trading_days": len(expected_dates),
            "covered_trading_days": len([date_key for date_key in expected_dates if date_key in by_date]),
            "missing_coverage_dates": missing_coverage_dates[:20],
            "missing_coverage_dates_truncated": max(0, len(missing_coverage_dates) - 20),
            "status_counts": daily_status_counts,
        },
    }


__all__ = [
    "DAILY_COVERAGE_OK_STATUSES",
    "build_daily_coverage_row",
    "build_range_daily_coverage",
    "clip_windows_to_range",
    "date_ms_bounds",
    "date_range_strings",
    "expected_bar_times_for_date",
    "is_nyse_early_close_day",
    "is_nyse_trading_day",
    "load_daily_coverage_rows",
    "market_date_from_ms",
    "nyse_holidays",
    "summarize_symbol_daily_coverage",
    "trading_date_strings",
    "trading_date_strings_from_ms",
]
