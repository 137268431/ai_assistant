from __future__ import annotations

from datetime import datetime, timezone

from ibkr_compute.core.time_utils import ET


def _coerce_time_ms(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except Exception:
            return 0
        if number <= 0:
            return 0
        return int(number if number > 1e12 else number * 1000)

    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        number = int(text)
        return int(number if number > 1_000_000_000_000 else number * 1000)
    if len(text) == 17 and text[8] == "-" and text[:8].isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}T{text[9:]}"
    elif len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    else:
        text = text.replace(" ", "T")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _extract_market_date_text(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    normalized = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except Exception:
        return ""
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d")
    return parsed.astimezone(ET).strftime("%Y-%m-%d")


def _order_history_time_value(record: dict) -> str:
    for key in ("order_time", "us_time", "updated", "created", "fill_time"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


__all__ = [
    "_coerce_time_ms",
    "_extract_market_date_text",
    "_order_history_time_value",
]
