"""Shared helpers for the daily IBKR scanner."""

from __future__ import annotations

import json
from typing import Any

from .daily_scanner_constants import (
    DAILY_SCAN_MODE_SEED,
    DAILY_SCAN_MODE_TOPUP,
    DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
    DEFAULT_MIN_ATR_PCT,
    DEFAULT_MIN_AVG_10D_VOLUME,
    DEFAULT_MIN_PREMARKET_VOLUME,
    MANUAL_TARGET_SOURCES,
)

def _daily_scan_rule_matches(snapshot: dict, rule: tuple[str, object, str]) -> bool:
    key, expected, _ = rule
    value = snapshot.get(key)
    if isinstance(expected, bool):
        return bool(value) is expected
    return value == expected


def _daily_scan_matches_any(snapshot: dict, rules) -> bool:
    return any(_daily_scan_rule_matches(snapshot, rule) for rule in rules)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_extra(row: dict | None) -> dict:
    payload = (row or {}).get("extra")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _target_row_is_manual(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source.startswith("manual_"):
        return True
    return source in MANUAL_TARGET_SOURCES


def _normalize_scan_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {DAILY_SCAN_MODE_TOPUP, "incremental", "early_expansion_topup"}:
        return DAILY_SCAN_MODE_TOPUP
    return DAILY_SCAN_MODE_SEED


def _format_threshold(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _format_metric_value(value: float | int) -> str:
    return _format_threshold(value)


def _new_rejection_trackers() -> tuple[dict[str, int], dict[str, list[dict[str, str]]]]:
    return {}, {}


def _record_rejection(
    summary: dict[str, int],
    examples: dict[str, list[dict[str, str]]],
    *,
    bucket: str,
    symbol: str,
    actual: str = "",
    threshold: str = "",
    note: str = "",
    limit: int = 3,
) -> None:
    normalized_bucket = str(bucket or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_bucket or not normalized_symbol:
        return
    summary[normalized_bucket] = int(summary.get(normalized_bucket, 0) or 0) + 1
    bucket_examples = examples.setdefault(normalized_bucket, [])
    if len(bucket_examples) >= max(1, int(limit or 0)):
        return
    bucket_examples.append(
        {
            "bucket": normalized_bucket,
            "symbol": normalized_symbol,
            "actual": str(actual or "").strip(),
            "threshold": str(threshold or "").strip(),
            "note": str(note or "").strip(),
        }
    )


def _flatten_rejection_examples(examples: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    rows = []
    for bucket in sorted(examples.keys()):
        rows.extend(examples.get(bucket) or [])
    return rows


def _metric_rank_bonus(row: dict) -> int:
    avg_10d_volume = _safe_float(row.get("avg_10d_volume"))
    premarket_volume = _safe_float(row.get("premarket_volume"))
    atr_pct = abs(_safe_float(row.get("atr_pct")))
    day_change_pct = abs(_safe_float(row.get("day_change_pct")))
    bonus = 0

    if avg_10d_volume >= 1_000_000:
        bonus += 5
    elif avg_10d_volume >= 500_000:
        bonus += 3
    elif avg_10d_volume >= DEFAULT_MIN_AVG_10D_VOLUME:
        bonus += 1

    if premarket_volume >= 30_000:
        bonus += 5
    elif premarket_volume >= 10_000:
        bonus += 3
    elif premarket_volume >= DEFAULT_MIN_PREMARKET_VOLUME:
        bonus += 1

    if atr_pct >= 0.30:
        bonus += 5
    elif atr_pct >= 0.20:
        bonus += 3
    elif atr_pct >= DEFAULT_MIN_ATR_PCT:
        bonus += 1

    if day_change_pct >= 4.0:
        bonus += 5
    elif day_change_pct >= 2.0:
        bonus += 3
    elif day_change_pct >= DEFAULT_MIN_ABS_DAY_CHANGE_PCT:
        bonus += 1

    return bonus
