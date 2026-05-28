from __future__ import annotations

import json
from typing import Any


BACKTEST_DELTA_MODE_OFF = "off"
BACKTEST_DELTA_MODE_PROXY_SHADOW = "proxy_shadow"
BACKTEST_DELTA_MODE_PROXY_FILTER = "proxy_filter"
BACKTEST_DELTA_MODES = {
    BACKTEST_DELTA_MODE_OFF,
    BACKTEST_DELTA_MODE_PROXY_SHADOW,
    BACKTEST_DELTA_MODE_PROXY_FILTER,
}
DEFAULT_BACKTEST_DELTA_PROXY_THRESHOLD = 0.12
PROXY_DELTA_VERSION = "proxy_5m_delta_v1"
PROXY_DELTA_FILTER_REASON = "proxy_delta_filter_failed"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_extra(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def normalize_backtest_delta_mode(raw_value: Any) -> str:
    mode = str(raw_value or BACKTEST_DELTA_MODE_OFF).strip().lower()
    if mode not in BACKTEST_DELTA_MODES:
        return BACKTEST_DELTA_MODE_OFF
    return mode


def normalize_backtest_delta_proxy_threshold(raw_value: Any) -> float:
    threshold = _safe_float(raw_value, DEFAULT_BACKTEST_DELTA_PROXY_THRESHOLD)
    return max(0.0, min(1.0, threshold))


def compute_proxy_5m_delta_v1(bar: dict) -> dict:
    open_price = _safe_float(bar.get("open"))
    high_price = _safe_float(bar.get("high"))
    low_price = _safe_float(bar.get("low"))
    close_price = _safe_float(bar.get("close"))
    volume = max(0.0, _safe_float(bar.get("volume")))
    price_range = high_price - low_price
    close_location = 0.0
    body_bias = 0.0
    delta_ratio = 0.0
    quality = "ok"

    if volume <= 0:
        quality = "no_volume"
    elif price_range <= 0:
        quality = "zero_range"
    else:
        close_location = ((close_price - low_price) / price_range) * 2.0 - 1.0
        body_bias = (close_price - open_price) / price_range
        delta_ratio = max(-1.0, min(1.0, 0.7 * close_location + 0.3 * body_bias))
        if abs(delta_ratio) < 1e-12:
            quality = "neutral"

    delta = volume * delta_ratio
    buy_volume = (volume + delta) / 2.0 if volume > 0 else 0.0
    sell_volume = (volume - delta) / 2.0 if volume > 0 else 0.0
    return {
        "version": PROXY_DELTA_VERSION,
        "delta": round(delta, 4),
        "delta_ratio": round(delta_ratio, 6),
        "volume": round(volume, 4),
        "buy_volume": round(max(0.0, buy_volume), 4),
        "sell_volume": round(max(0.0, sell_volume), 4),
        "close_location": round(close_location, 6),
        "body_bias": round(body_bias, 6),
        "quality": quality,
    }


def evaluate_proxy_delta_gate(
    bar: dict,
    direction: Any,
    *,
    mode: Any = BACKTEST_DELTA_MODE_OFF,
    threshold: Any = DEFAULT_BACKTEST_DELTA_PROXY_THRESHOLD,
) -> dict:
    normalized_mode = normalize_backtest_delta_mode(mode)
    threshold_value = normalize_backtest_delta_proxy_threshold(threshold)
    proxy = compute_proxy_5m_delta_v1(bar)
    signal_direction = str(direction or "").strip().lower()
    enabled = normalized_mode != BACKTEST_DELTA_MODE_OFF
    passed = True
    reason = "disabled"

    if enabled:
        ratio = float(proxy.get("delta_ratio", 0.0) or 0.0)
        if signal_direction in {"long", "buy"}:
            passed = ratio >= threshold_value
            reason = "passed" if passed else "delta_ratio_not_confirmed"
        elif signal_direction in {"short", "sell"}:
            passed = ratio <= -threshold_value
            reason = "passed" if passed else "delta_ratio_not_confirmed"
        else:
            passed = False
            reason = "unsupported_direction"

    return {
        "enabled": enabled,
        "mode": normalized_mode,
        "version": PROXY_DELTA_VERSION,
        "threshold": round(threshold_value, 6),
        "direction": signal_direction,
        "passed": bool(passed),
        "reason": reason,
        "delta": proxy["delta"],
        "delta_ratio": proxy["delta_ratio"],
        "volume": proxy["volume"],
        "buy_volume": proxy["buy_volume"],
        "sell_volume": proxy["sell_volume"],
        "close_location": proxy["close_location"],
        "body_bias": proxy["body_bias"],
        "quality": proxy["quality"],
        "would_filter": bool(enabled and not passed),
        "filtered": False,
    }


def build_delta_ab_summary(signal_rows: list[dict], mode: Any, threshold: Any) -> dict:
    normalized_mode = normalize_backtest_delta_mode(mode)
    threshold_value = normalize_backtest_delta_proxy_threshold(threshold)
    summary = {
        "enabled": normalized_mode != BACKTEST_DELTA_MODE_OFF,
        "mode": normalized_mode,
        "version": PROXY_DELTA_VERSION,
        "threshold": round(threshold_value, 6),
        "signal_count": len(signal_rows or []),
        "evaluated_count": 0,
        "pass_count": 0,
        "fail_count": 0,
        "filter_count": 0,
        "filtered_count": 0,
    }
    for row in signal_rows or []:
        extra = _parse_extra(row.get("extra"))
        gate = extra.get("delta_gate")
        if not isinstance(gate, dict) or gate.get("version") != PROXY_DELTA_VERSION:
            continue
        summary["evaluated_count"] += 1
        if bool(gate.get("passed")):
            summary["pass_count"] += 1
        else:
            summary["fail_count"] += 1
        if bool(gate.get("filtered")) or str(extra.get("signal_status_reason") or "") == PROXY_DELTA_FILTER_REASON:
            summary["filter_count"] += 1
    summary["filtered_count"] = summary["filter_count"]
    return summary
