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

CONTEXT_ACTIVE_TARGET_SOURCES = {"daily_scan", "intraday_window_admission"}

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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _target_row_is_daily_scan_active(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source not in CONTEXT_ACTIVE_TARGET_SOURCES:
        return False
    return (
        _truthy(extra.get("active_gate_passed"))
        or _truthy(extra.get("context_active"))
        or _truthy(extra.get("context_gate_passed"))
    )


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


def _truthy_scan_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "up", "down", "long", "short", "ready", "ok"}


def _first_present_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _snapshot_best_float(snapshots: dict, key: str, default: float = 0.0) -> float:
    best = default
    for snapshot in (snapshots or {}).values():
        if not isinstance(snapshot, dict):
            continue
        best = max(best, _safe_float(snapshot.get(key), default))
    return best


def _snapshot_first_text(snapshots: dict, key: str) -> str:
    for snapshot in (snapshots or {}).values():
        if not isinstance(snapshot, dict):
            continue
        text = str(snapshot.get(key) or "").strip().lower()
        if text:
            return text
    return ""


def _build_stocks_in_play_bonus(metric_row: dict, snapshots: dict, direction_bias: str) -> tuple[int, list[str], dict]:
    bonus = 0
    reasons: list[str] = []
    details: dict[str, Any] = {}
    direction = str(direction_bias or "").strip().lower()

    sd_regime = str(
        _first_present_value(metric_row.get("sd_regime"), _snapshot_first_text(snapshots, "sd_regime")) or ""
    ).strip().lower()
    if sd_regime:
        details["sd_regime"] = sd_regime
        aligned_sd = (
            (direction == "long" and sd_regime in {"breakout_up", "trend_walk_up", "trend_up"})
            or (direction == "short" and sd_regime in {"breakout_down", "trend_walk_down", "trend_down"})
        )
        if aligned_sd:
            bonus += 3
            reasons.append(f"sd_regime={sd_regime}")
        elif sd_regime in {"squeeze", "flat"}:
            bonus += 1
            reasons.append(f"sd_regime={sd_regime}")

    orb_text = str(_first_present_value(metric_row.get("orb_breakout"), "") or "").strip().lower()
    orb_up = _truthy_scan_value(metric_row.get("orb_breakout_up")) or _truthy_scan_value(_snapshot_first_text(snapshots, "orb_breakout_up"))
    orb_down = _truthy_scan_value(metric_row.get("orb_breakout_down")) or _truthy_scan_value(_snapshot_first_text(snapshots, "orb_breakout_down"))
    if orb_text in {"up", "long", "breakout_up"}:
        orb_up = True
    elif orb_text in {"down", "short", "breakout_down"}:
        orb_down = True
    elif _truthy_scan_value(orb_text):
        orb_up = direction == "long"
        orb_down = direction == "short"
    details["orb_breakout"] = "up" if orb_up else ("down" if orb_down else "")
    if (direction == "long" and orb_up) or (direction == "short" and orb_down):
        bonus += 3
        reasons.append(f"orb_breakout={details['orb_breakout']}")

    vwap_alignment = str(
        _first_present_value(metric_row.get("vwap_alignment"), _snapshot_first_text(snapshots, "vwap_alignment")) or ""
    ).strip().lower()
    if not vwap_alignment:
        vwap_dist = _first_present_value(metric_row.get("vwap_dist"), _snapshot_best_float(snapshots, "vwap_dist", 0.0))
        vwap_dist_float = _safe_float(vwap_dist)
        if vwap_dist_float > 0:
            vwap_alignment = "above"
        elif vwap_dist_float < 0:
            vwap_alignment = "below"
    if vwap_alignment:
        details["vwap_alignment"] = vwap_alignment
        aligned_vwap = (
            (direction == "long" and vwap_alignment in {"above", "bullish", "long"})
            or (direction == "short" and vwap_alignment in {"below", "bearish", "short"})
        )
        if aligned_vwap:
            bonus += 2
            reasons.append(f"vwap_alignment={vwap_alignment}")

    rvol_20 = max(_safe_float(metric_row.get("rvol_20")), _snapshot_best_float(snapshots, "rvol_20", 0.0))
    details["rvol_20"] = round(rvol_20, 4)
    if rvol_20 >= 3.0:
        bonus += 3
        reasons.append(f"rvol20={_format_metric_value(round(rvol_20, 2))}")
    elif rvol_20 >= 1.5:
        bonus += 2
        reasons.append(f"rvol20={_format_metric_value(round(rvol_20, 2))}")
    elif rvol_20 >= 1.1:
        bonus += 1
        reasons.append(f"rvol20={_format_metric_value(round(rvol_20, 2))}")

    dollar_volume = max(_safe_float(metric_row.get("dollar_volume")), _snapshot_best_float(snapshots, "dollar_volume", 0.0))
    if dollar_volume <= 0:
        price = _safe_float(_first_present_value(metric_row.get("price"), _snapshot_best_float(snapshots, "close", 0.0)))
        today_volume = _safe_float(metric_row.get("today_volume"))
        if price > 0 and today_volume > 0:
            dollar_volume = price * today_volume
    details["dollar_volume"] = round(dollar_volume, 2)
    if dollar_volume >= 50_000_000:
        bonus += 3
        reasons.append("dollar_volume>=50M")
    elif dollar_volume >= 10_000_000:
        bonus += 2
        reasons.append("dollar_volume>=10M")
    elif dollar_volume >= 2_000_000:
        bonus += 1
        reasons.append("dollar_volume>=2M")

    data_quality = metric_row.get("data_quality") if isinstance(metric_row.get("data_quality"), dict) else {}
    needs_repair = bool(data_quality.get("needs_repair")) if data_quality else False
    status = str(data_quality.get("status") or ("repairing" if needs_repair else "ready")).strip().lower()
    details["data_quality"] = {"status": status, "needs_repair": needs_repair}
    if not needs_repair:
        bonus += 1
        reasons.append("data_quality=ready")

    details["stocks_in_play_score"] = bonus
    details["stocks_in_play_reasons"] = reasons
    return bonus, reasons, details
