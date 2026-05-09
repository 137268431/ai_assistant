"""Shared risk-management helpers for live and backtest execution."""

from __future__ import annotations


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
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


def _policy_settings(position: dict) -> dict:
    settings = position.get("exit_policy_settings")
    if isinstance(settings, dict):
        return dict(settings)
    extra = position.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("exit_policy_settings"), dict):
        return dict(extra.get("exit_policy_settings") or {})
    return {}


def _policy_trail_state(position: dict) -> dict:
    state = position.get("trail_state")
    if isinstance(state, dict):
        return dict(state)
    extra = position.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("trail_state"), dict):
        return dict(extra.get("trail_state") or {})
    return {}


def _policy_target_state(position: dict) -> dict:
    state = position.get("target_state")
    if isinstance(state, dict):
        return dict(state)
    extra = position.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("target_state"), dict):
        return dict(extra.get("target_state") or {})
    return {}


def _initial_risk(position: dict, entry: float, old_sl: float) -> float:
    raw = _safe_float(position.get("risk_r"), 0.0)
    if raw > 0:
        return raw
    raw = _safe_float(position.get("initial_risk_r"), 0.0)
    if raw > 0:
        return raw
    initial_sl = _safe_float(position.get("initial_stop_loss", position.get("original_stop_loss", 0)), 0.0)
    if entry > 0 and initial_sl > 0:
        return abs(entry - initial_sl)
    if entry > 0 and old_sl > 0:
        return abs(entry - old_sl)
    return 0.0


def exit_policy_uses_hard_target(position: dict) -> bool:
    """Return whether a backtest target touch should close immediately."""
    settings = _policy_settings(position)
    if "target_is_hard" in settings:
        return _safe_bool(settings.get("target_is_hard"), True)
    target_mode = str(settings.get("target_mode") or "hard_rr").strip().lower()
    return target_mode not in {"soft_runner", "checkpoint_then_trail"}


def compute_exit_policy_target_update(
    position: dict,
    *,
    current_price: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
    min_change: float = 0.01,
    price_buffer_pct: float = 0.001,
) -> dict:
    """Update stateful target checkpoints and optionally tighten the stop.

    This models strategy-specific profit taking without turning every setup
    into a fixed target close. It never widens risk.
    """
    settings = _policy_settings(position)
    target_mode = str(settings.get("target_mode") or "hard_rr").strip().lower()
    target_is_hard = exit_policy_uses_hard_target(position)
    if target_mode == "hard_rr":
        return {
            "should_update_stop": False,
            "reason": "hard_target_policy",
            "target_is_hard": target_is_hard,
            "target_mode": target_mode or "hard_rr",
        }

    direction = str(position.get("direction") or "").strip().lower()
    entry = _safe_float(position.get("entry_price", position.get("entry", 0)))
    old_sl = _safe_float(position.get("stop_price", position.get("stop_loss", 0)))
    target = _safe_float(position.get("target_price", position.get("take_profit", 0)))
    current = _safe_float(current_price)
    high = _safe_float(bar_high, current) if bar_high is not None else current
    low = _safe_float(bar_low, current) if bar_low is not None else current
    risk = _initial_risk(position, entry, old_sl)
    min_delta = max(0.0, _safe_float(min_change, 0.01))

    if direction not in {"long", "short"}:
        return {"should_update_stop": False, "reason": "invalid_direction"}
    if entry <= 0 or old_sl <= 0 or current <= 0 or risk <= 0:
        return {"should_update_stop": False, "reason": "invalid_prices"}

    favorable = max(0.0, high - entry) if direction == "long" else max(0.0, entry - low)
    mfe = max(_safe_float(position.get("mfe"), 0.0), favorable)
    mfe_r = mfe / risk if risk > 0 else 0.0
    target_touched = bool(
        target > 0 and ((direction == "long" and high >= target) or (direction == "short" and low <= target))
    )
    checkpoint_r = max(0.0, _safe_float(settings.get("checkpoint_r"), 0.0))
    checkpoint_hit = checkpoint_r > 0 and mfe_r >= checkpoint_r
    old_state = _policy_target_state(position)
    next_state = {
        **old_state,
        "target_mode": target_mode or "soft_runner",
        "target_is_hard": target_is_hard,
        "highest_mfe_r": round(max(_safe_float(old_state.get("highest_mfe_r"), 0.0), mfe_r), 4),
        "target_touched": bool(old_state.get("target_touched") or target_touched),
        "checkpoint_hit": bool(old_state.get("checkpoint_hit") or checkpoint_hit),
    }

    if not checkpoint_hit:
        return {
            "should_update_stop": False,
            "reason": "checkpoint_not_hit",
            "mfe_r": round(mfe_r, 4),
            "target_touched": target_touched,
            "target_state": next_state,
        }

    lock_r = max(0.0, _safe_float(settings.get("checkpoint_lock_r"), 0.0))
    locked_stop = entry + risk * lock_r if direction == "long" else entry - risk * lock_r
    price_buffer = max(min_delta, current * max(0.0, _safe_float(price_buffer_pct, 0.001)))
    if direction == "long":
        candidate = max(old_sl, locked_stop)
        if target > 0:
            candidate = min(candidate, target - min_delta)
        candidate = min(candidate, current - price_buffer)
        improved = candidate > old_sl + min_delta - 1e-9
    else:
        candidate = min(old_sl, locked_stop)
        if target > 0:
            candidate = max(candidate, target + min_delta)
        candidate = max(candidate, current + price_buffer)
        improved = candidate < old_sl - min_delta + 1e-9

    candidate = round(float(candidate), 4)
    if not improved or candidate <= 0:
        return {
            "should_update_stop": False,
            "reason": "checkpoint_no_tightening_available",
            "old_sl": round(old_sl, 4),
            "candidate_sl": candidate,
            "mfe_r": round(mfe_r, 4),
            "target_touched": target_touched,
            "target_state": next_state,
        }

    return {
        "should_update_stop": True,
        "old_sl": round(old_sl, 4),
        "new_sl": candidate,
        "reason": "target_checkpoint_stop",
        "mfe_r": round(mfe_r, 4),
        "target_touched": target_touched,
        "target_state": next_state,
    }


def compute_exit_policy_stop_update(
    position: dict,
    *,
    current_price: float,
    current_atr: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
    min_change: float = 0.01,
    price_buffer_pct: float = 0.001,
) -> dict:
    """Compute a signal-mode policy trailing stop update without widening risk."""
    direction = str(position.get("direction") or "").strip().lower()
    entry = _safe_float(position.get("entry_price", position.get("entry", 0)))
    old_sl = _safe_float(position.get("stop_price", position.get("stop_loss", 0)))
    target = _safe_float(position.get("target_price", position.get("take_profit", 0)))
    current = _safe_float(current_price)
    atr = _safe_float(current_atr)
    settings = _policy_settings(position)
    trail_type = str(settings.get("trail_type") or "atr_tighten").strip().lower()
    activation_r = max(0.0, _safe_float(settings.get("trail_activation_r"), 0.0))
    risk = _initial_risk(position, entry, old_sl)
    min_delta = max(0.0, _safe_float(min_change, 0.01))

    if direction not in {"long", "short"}:
        return {"should_update": False, "reason": "invalid_direction"}
    if entry <= 0 or old_sl <= 0 or current <= 0 or atr <= 0 or risk <= 0:
        return {"should_update": False, "reason": "invalid_prices"}

    high = _safe_float(bar_high, current) if bar_high is not None else current
    low = _safe_float(bar_low, current) if bar_low is not None else current
    state = _policy_trail_state(position)
    high_water = max(_safe_float(state.get("high_water"), entry), high, current) if direction == "long" else 0.0
    low_water = min(
        _safe_float(state.get("low_water"), entry) or entry,
        low,
        current,
    ) if direction == "short" else 0.0
    favorable = (high_water - entry) if direction == "long" else (entry - low_water)
    max_favorable_r = max(_safe_float(state.get("max_favorable_r"), 0.0), favorable / risk if risk > 0 else 0.0)
    next_state = {
        **state,
        "high_water": round(high_water, 4) if direction == "long" else _safe_float(state.get("high_water"), 0.0),
        "low_water": round(low_water, 4) if direction == "short" else _safe_float(state.get("low_water"), 0.0),
        "max_favorable_r": round(max_favorable_r, 4),
    }

    progress_r = position_progress_r(position, current)
    if max(max_favorable_r, progress_r) < activation_r:
        return {
            "should_update": False,
            "reason": "profit_below_policy_activation",
            "progress_r": round(progress_r, 4),
            "trail_state": next_state,
        }

    if trail_type == "breakeven":
        offset_r = max(0.0, _safe_float(settings.get("breakeven_offset_r"), 0.0))
        candidate = entry + risk * offset_r if direction == "long" else entry - risk * offset_r
    else:
        mult = max(0.0, _safe_float(settings.get("chandelier_atr_mult"), _safe_float(settings.get("trail_atr_mult"), 0.0)))
        if mult <= 0:
            mult = max(0.0, _safe_float(settings.get("sl_atr_mult"), 0.0))
        if mult <= 0:
            return {"should_update": False, "reason": "invalid_policy_atr_mult", "trail_state": next_state}
        candidate = high_water - atr * mult if direction == "long" else low_water + atr * mult

    price_buffer = max(min_delta, current * max(0.0, _safe_float(price_buffer_pct, 0.001)))
    if direction == "long":
        original_sl = _safe_float(position.get("original_stop_loss", position.get("original_sl", old_sl)), old_sl)
        max_loss_sl = _safe_float(position.get("max_loss_stop", 0))
        candidate = max(candidate, original_sl, max_loss_sl, old_sl)
        if target > 0:
            candidate = min(candidate, target - min_delta)
        candidate = min(candidate, current - price_buffer)
        improved = candidate > old_sl + min_delta - 1e-9
    else:
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
            "reason": "no_policy_tightening_available",
            "old_sl": round(old_sl, 4),
            "candidate_sl": candidate,
            "progress_r": round(progress_r, 4),
            "trail_state": next_state,
        }

    next_state["adjust_count"] = int(_safe_float(next_state.get("adjust_count"), 0)) + 1
    return {
        "should_update": True,
        "old_sl": round(old_sl, 4),
        "new_sl": candidate,
        "current_atr": round(atr, 4),
        "progress_r": round(progress_r, 4),
        "trail_state": next_state,
        "reason": f"{trail_type or 'policy'}_trail_stop",
    }


def compute_exit_policy_time_exit(position: dict) -> dict:
    """Return whether a signal-mode policy time exit should close the position."""
    settings = _policy_settings(position)
    bars_held = int(_safe_float(position.get("bars_held"), 0))
    hard_bars = int(_safe_float(settings.get("hard_time_stop_bars"), 0))
    time_bars = int(_safe_float(settings.get("time_stop_bars"), 0))
    risk = _initial_risk(
        position,
        _safe_float(position.get("entry_price", position.get("entry", 0))),
        _safe_float(position.get("stop_price", position.get("stop_loss", 0))),
    )
    mfe = max(0.0, _safe_float(position.get("mfe"), 0.0))
    mfe_r = mfe / risk if risk > 0 else 0.0
    min_mfe_r = max(0.0, _safe_float(settings.get("time_stop_min_mfe_r"), 0.0))
    if hard_bars > 0 and bars_held >= hard_bars:
        return {"should_exit": True, "reason": "exit_policy_hard_time_stop", "bars_held": bars_held, "mfe_r": round(mfe_r, 4)}
    failure_bars = int(_safe_float(settings.get("failure_exit_bars"), 0))
    failure_enabled = _safe_bool(settings.get("failure_exit_enabled"), False)
    failure_min_mfe_r = max(0.0, _safe_float(settings.get("failure_exit_min_mfe_r"), 0.0))
    if failure_enabled and failure_bars > 0 and bars_held >= failure_bars and mfe_r < failure_min_mfe_r:
        return {
            "should_exit": True,
            "reason": "exit_policy_breakout_failure",
            "bars_held": bars_held,
            "mfe_r": round(mfe_r, 4),
        }
    if time_bars > 0 and bars_held >= time_bars and mfe_r < min_mfe_r:
        return {"should_exit": True, "reason": "exit_policy_time_stop", "bars_held": bars_held, "mfe_r": round(mfe_r, 4)}
    return {"should_exit": False, "reason": "", "bars_held": bars_held, "mfe_r": round(mfe_r, 4)}
