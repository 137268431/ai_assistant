"""Shared risk-management helpers for live and backtest execution."""

from __future__ import annotations


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def position_progress_r(position: dict, current_price: float) -> float:
    """Return favorable progress measured in initial risk units."""
    direction = str(position.get("direction") or "").strip().lower()
    entry = _safe_float(position.get("entry_price", position.get("entry", 0)))
    stop = _safe_float(position.get("stop_price", position.get("stop_loss", 0)))
    current = _safe_float(current_price)
    risk = abs(entry - stop)
    if direction not in {"long", "short"} or entry <= 0 or risk <= 0 or current <= 0:
        return 0.0
    favorable = current - entry if direction == "long" else entry - current
    return favorable / risk


def compute_atr_tightened_stop(
    position: dict,
    *,
    current_price: float,
    current_atr: float,
    sl_atr_mult: float,
    min_profit_r: float = 0.3,
    deviation_threshold: float = 0.30,
    min_change: float = 0.01,
    price_buffer_pct: float = 0.001,
) -> dict:
    """Compute an ATR stop update that can only reduce risk.

    The helper never widens the stop. It returns ``{"should_update": False}``
    with a reason when no safe tightening is available.
    """
    direction = str(position.get("direction") or "").strip().lower()
    entry = _safe_float(position.get("entry_price", position.get("entry", 0)))
    old_sl = _safe_float(position.get("stop_price", position.get("stop_loss", 0)))
    target = _safe_float(position.get("target_price", position.get("take_profit", 0)))
    atr = _safe_float(current_atr)
    current = _safe_float(current_price)
    mult = max(0.0, _safe_float(sl_atr_mult, 0.0))
    min_delta = max(0.0, _safe_float(min_change, 0.01))

    if direction not in {"long", "short"}:
        return {"should_update": False, "reason": "invalid_direction"}
    if entry <= 0 or old_sl <= 0 or atr <= 0 or current <= 0 or mult <= 0:
        return {"should_update": False, "reason": "invalid_prices"}

    progress_r = position_progress_r(position, current)
    if progress_r < max(0.0, _safe_float(min_profit_r, 0.3)):
        return {"should_update": False, "reason": "profit_below_threshold", "progress_r": round(progress_r, 4)}

    last_atr = _safe_float(position.get("last_stop_atr", position.get("entry_atr", 0)))
    if last_atr > 0:
        deviation = abs(atr - last_atr) / last_atr
        if deviation < max(0.0, _safe_float(deviation_threshold, 0.30)):
            return {
                "should_update": False,
                "reason": "atr_deviation_below_threshold",
                "atr_deviation": round(deviation, 4),
                "progress_r": round(progress_r, 4),
            }
    else:
        deviation = 0.0

    price_buffer = max(min_delta, current * max(0.0, _safe_float(price_buffer_pct, 0.001)))
    if direction == "long":
        candidate = entry - atr * mult
        original_sl = _safe_float(position.get("original_stop_loss", position.get("original_sl", old_sl)), old_sl)
        max_loss_sl = _safe_float(position.get("max_loss_stop", 0))
        candidate = max(candidate, original_sl, max_loss_sl, old_sl)
        if target > 0:
            candidate = min(candidate, target - min_delta)
        candidate = min(candidate, current - price_buffer)
        improved = candidate > old_sl + min_delta - 1e-9
    else:
        candidate = entry + atr * mult
        original_sl = _safe_float(position.get("original_stop_loss", position.get("original_sl", old_sl)), old_sl)
        max_loss_sl = _safe_float(position.get("max_loss_stop", 0))
        floors = [old_sl]
        if original_sl > 0:
            floors.append(original_sl)
        if max_loss_sl > 0:
            floors.append(max_loss_sl)
        candidate = min([candidate, *floors])
        if target > 0:
            candidate = max(candidate, target + min_delta)
        candidate = max(candidate, current + price_buffer)
        improved = candidate < old_sl - min_delta + 1e-9

    candidate = round(float(candidate), 4)
    if not improved or candidate <= 0:
        return {
            "should_update": False,
            "reason": "no_tightening_available",
            "old_sl": round(old_sl, 4),
            "candidate_sl": candidate,
            "progress_r": round(progress_r, 4),
            "atr_deviation": round(deviation, 4),
        }

    return {
        "should_update": True,
        "old_sl": round(old_sl, 4),
        "new_sl": candidate,
        "current_atr": round(atr, 4),
        "previous_atr": round(last_atr, 4),
        "atr_deviation": round(deviation, 4),
        "progress_r": round(progress_r, 4),
        "reason": "atr_tighten_stop",
    }

