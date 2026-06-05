from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_TIMESTAMP_PATTERN = (
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
    r"(?:\s*(?:US/Eastern|America/New_York|Asia/Shanghai|北京时间|UTC|GMT|EDT|EST|ET|US|CN|CST|北京|美东))?"
)
_TIMESTAMP_RE = re.compile(_TIMESTAMP_PATTERN)
_STANDARD_DUAL_RE = re.compile(r"美东\s*" + _TIMESTAMP_PATTERN + r"\s*\|\s*北京\s*" + _TIMESTAMP_PATTERN)
_CLOCK_LINE_RE = re.compile(r"(?m)^(?P<prefix>\s*🕐\s*)(?P<value>" + _TIMESTAMP_PATTERN + r")(?P<suffix>\s*)$")
_BOLD_VALUE_LINE_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:[-*]\s*)?\*\*[^*\n]{1,60}\*\*\s*[:：]\s*)"
    r"(?P<value>"
    + _TIMESTAMP_PATTERN
    + r")(?P<suffix>\s*)$"
)

_LABEL_BEFORE_PAIR_RE = re.compile(
    r"(?P<us_label>美东|US|ET)\s*(?P<us>" + _TIMESTAMP_PATTERN + r"|-)?\s*[/|]\s*"
    r"(?P<cn_label>北京时间|北京|CN)\s*(?P<cn>" + _TIMESTAMP_PATTERN + r"|-)?"
)
_LABEL_BEFORE_CN_US_PAIR_RE = re.compile(
    r"(?P<cn_label>北京时间|北京|CN)\s*(?P<cn>" + _TIMESTAMP_PATTERN + r"|-)?\s*[/|]\s*"
    r"(?P<us_label>美东|US|ET)\s*(?P<us>" + _TIMESTAMP_PATTERN + r"|-)?"
)
_CN_BEFORE_US_PAIR_RE = re.compile(
    r"(?P<cn>" + _TIMESTAMP_PATTERN + r")\s*(?P<cn_label>北京时间|北京|CN)\s*/\s*"
    r"(?P<us>" + _TIMESTAMP_PATTERN + r"|-)?\s*(?P<us_label>美东|US|ET)"
)
_US_BEFORE_CN_PAIR_RE = re.compile(
    r"(?P<us>" + _TIMESTAMP_PATTERN + r")\s*(?P<us_label>美东|US|ET)\s*/\s*"
    r"(?P<cn>" + _TIMESTAMP_PATTERN + r"|-)?\s*(?P<cn_label>北京时间|北京|CN)"
)

_DISPLAY_CONTENT_KEYS = {"content"}


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def _timezone_hint_from_suffix(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    upper = cleaned.upper()
    suffixes = (
        ("AMERICA/NEW_YORK", "et"),
        ("US/EASTERN", "et"),
        ("北京时间", "cn"),
        ("ASIA/SHANGHAI", "cn"),
        ("UTC", "utc"),
        ("GMT", "utc"),
        ("EDT", "et"),
        ("EST", "et"),
        (" ET", "et"),
        (" US", "et"),
        (" CN", "cn"),
        (" CST", "cn"),
        ("北京", "cn"),
        ("美东", "et"),
    )
    for suffix, hint in suffixes:
        target = upper if suffix.isascii() else cleaned
        if target.endswith(suffix):
            return cleaned[: -len(suffix)].strip(), hint
    return cleaned, ""


def parse_feishu_time(value: Any, *, assume_tz: str = "et") -> datetime | None:
    """Parse common IBKR/Feishu timestamp shapes into an aware datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=CN if assume_tz == "cn" else ET)
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        seconds = number / 1000.0 if number > 10_000_000_000 else number
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    text = _to_text(value)
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "待确认"}:
        return None
    if text.isdigit():
        return parse_feishu_time(int(text), assume_tz=assume_tz)

    match = _TIMESTAMP_RE.search(text)
    if not match:
        return None
    raw = match.group(0).strip()
    raw, suffix_hint = _timezone_hint_from_suffix(raw)
    hint = suffix_hint or assume_tz
    normalized = raw.replace("T", " ").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}\d{2}$", normalized):
        normalized = normalized[:-5] + normalized[-5:-2] + ":" + normalized[-2:]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                parsed = None  # type: ignore[assignment]
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        if hint == "cn":
            return parsed.replace(tzinfo=CN)
        if hint == "utc":
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(tzinfo=ET)
    return parsed


def format_feishu_dual_time(value: Any, *, assume_tz: str = "et", fallback: str = "-") -> str:
    parsed = parse_feishu_time(value, assume_tz=assume_tz)
    if parsed is None:
        return fallback
    return f"美东 {parsed.astimezone(ET).strftime(_TIME_FORMAT)} | 北京 {parsed.astimezone(CN).strftime(_TIME_FORMAT)}"


def format_feishu_dual_time_pair(us_value: Any = None, cn_value: Any = None, *, fallback: str = "-") -> str:
    parsed = parse_feishu_time(us_value, assume_tz="et") or parse_feishu_time(cn_value, assume_tz="cn")
    if parsed is None:
        return fallback
    return format_feishu_dual_time(parsed, fallback=fallback)


def _pair_replacement(match: re.Match[str]) -> str:
    us_value = match.groupdict().get("us")
    cn_value = match.groupdict().get("cn")
    return format_feishu_dual_time_pair(us_value, cn_value, fallback=match.group(0))


def _replace_labeled_pairs(text: str) -> str:
    updated = _LABEL_BEFORE_PAIR_RE.sub(_pair_replacement, text)
    updated = _LABEL_BEFORE_CN_US_PAIR_RE.sub(_pair_replacement, updated)
    updated = _CN_BEFORE_US_PAIR_RE.sub(_pair_replacement, updated)
    updated = _US_BEFORE_CN_PAIR_RE.sub(_pair_replacement, updated)
    return updated


def _replace_single_value_lines(text: str) -> str:
    def replace_clock(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{format_feishu_dual_time(match.group('value'), fallback=match.group('value'))}{match.group('suffix')}"

    def replace_bold(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{format_feishu_dual_time(match.group('value'), fallback=match.group('value'))}{match.group('suffix')}"

    updated = _CLOCK_LINE_RE.sub(replace_clock, text)
    updated = _BOLD_VALUE_LINE_RE.sub(replace_bold, updated)
    return updated


def _replace_single_timestamp_text(text: str) -> str:
    if "\n" in text or _STANDARD_DUAL_RE.search(text):
        return text
    matches = list(_TIMESTAMP_RE.finditer(text))
    if len(matches) != 1:
        return text
    match = matches[0]
    return f"{text[:match.start()]}{format_feishu_dual_time(match.group(0), fallback=match.group(0))}{text[match.end():]}"


def normalize_feishu_time_text(text: str) -> str:
    if not text:
        return text
    updated = _replace_labeled_pairs(text)
    updated = _replace_single_value_lines(updated)
    updated = _replace_single_timestamp_text(updated)
    return updated


def normalize_feishu_card_times(value: Any, *, _key: str = "") -> Any:
    """Return a copy of a Feishu card with visible timestamps rendered as ET + Beijing."""
    if isinstance(value, dict):
        return {str(key): normalize_feishu_card_times(item, _key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_feishu_card_times(item, _key=_key) for item in value]
    if isinstance(value, str) and _key in _DISPLAY_CONTENT_KEYS:
        return normalize_feishu_time_text(value)
    return value
