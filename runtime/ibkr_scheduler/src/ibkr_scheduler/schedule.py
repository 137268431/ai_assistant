from __future__ import annotations

from datetime import datetime, timezone


def _match_part(part: str, value: int) -> bool:
    token = str(part or "").strip()
    if not token or token == "*":
        return True
    if token.startswith("*/"):
        try:
            step = int(token[2:])
        except Exception:
            return False
        return step > 0 and value % step == 0
    if "-" in token:
        try:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
        except Exception:
            return False
        return start <= value <= end
    try:
        return int(token) == value
    except Exception:
        return False


def _match_field(field_expr: str, value: int) -> bool:
    return any(_match_part(part, value) for part in str(field_expr or "").split(","))


def cron_matches_minute(cron_expr: str, when_utc: datetime) -> bool:
    parts = str(cron_expr or "").split()
    if len(parts) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = parts
    weekday = (when_utc.weekday() + 1) % 7
    return (
        _match_field(minute, when_utc.minute)
        and _match_field(hour, when_utc.hour)
        and _match_field(day_of_month, when_utc.day)
        and _match_field(month, when_utc.month)
        and _match_field(day_of_week, weekday)
    )


def cron_slot_token(when_utc: datetime | None = None) -> str:
    current = when_utc.astimezone(timezone.utc) if isinstance(when_utc, datetime) else datetime.now(timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%MZ")
