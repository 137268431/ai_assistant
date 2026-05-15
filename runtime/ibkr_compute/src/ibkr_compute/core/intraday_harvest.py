"""Intraday volatility-harvest decisions for live and backtest execution.

The module is intentionally pure: it scores a current position/snapshot and
returns the next action plus state updates. Order placement/cancellation remains
in the order lifecycle layer.
"""

from __future__ import annotations

from typing import Any

INTRADAY_VOLATILITY_HARVEST_PROFILE = "intraday_volatility_harvest_v1"

ACTION_HOLD = "hold"
ACTION_PARTIAL_EXIT = "partial_exit"
ACTION_TIGHTEN_STOP = "tighten_stop"
ACTION_FULL_EXIT = "full_exit"
ACTION_REENTRY = "reentry"

DEFAULT_HARVEST_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "live_auto_enabled": True,
    "split_brackets_enabled": True,
    "tactical_fraction": 0.30,
    "max_daily_cycles": 2,
    "min_partial_profit_r": 0.60,
    "min_tighten_profit_r": 0.30,
    "partial_score": 2,
    "tighten_score": 1,
    "full_exit_score": 4,
    "giveback_r": 0.45,
    "breakeven_lock_r": 0.10,
    "reentry_support_bps": 35.0,
    "reentry_score": 2,
    "reentry_sl_atr_mult": 1.20,
    "reentry_tp_rr": 1.0,
    "min_tactical_shares": 1,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(default)


def _setting(settings: dict[str, Any], key: str) -> Any:
    if key in settings:
        return settings[key]
    return DEFAULT_HARVEST_SETTINGS.get(key)


def normalize_harvest_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = {**DEFAULT_HARVEST_SETTINGS, **(raw or {})}
    settings["enabled"] = _safe_bool(settings.get("enabled"), True)
    settings["live_auto_enabled"] = _safe_bool(settings.get("live_auto_enabled"), True)
    settings["split_brackets_enabled"] = _safe_bool(settings.get("split_brackets_enabled"), True)
    settings["tactical_fraction"] = min(0.90, max(0.0, _safe_float(settings.get("tactical_fraction"), 0.30)))
    settings["max_daily_cycles"] = max(0, _safe_int(settings.get("max_daily_cycles"), 2))
    settings["partial_score"] = max(1, _safe_int(settings.get("partial_score"), 2))
    settings["tighten_score"] = max(1, _safe_int(settings.get("tighten_score"), 1))
    settings["full_exit_score"] = max(1, _safe_int(settings.get("full_exit_score"), 4))
    settings["reentry_score"] = max(1, _safe_int(settings.get("reentry_score"), 2))
    settings["min_tactical_shares"] = max(1, _safe_int(settings.get("min_tactical_shares"), 1))
    for key in (
        "min_partial_profit_r",
        "min_tighten_profit_r",
        "giveback_r",
        "breakeven_lock_r",
        "reentry_support_bps",
        "reentry_sl_atr_mult",
        "reentry_tp_rr",
    ):
        settings[key] = max(0.0, _safe_float(settings.get(key), DEFAULT_HARVEST_SETTINGS[key]))
    return settings


def harvest_settings_from_config(config: Any, environment: str = "live") -> dict[str, Any]:
    if not config:
        return normalize_harvest_settings({})

    def get_bool(key: str, default: bool) -> bool:
        getter = getattr(config, "get_bool_for_environment", None)
        if callable(getter):
            return bool(getter(key, environment, default))
        return bool(default)

    def get_float(key: str, default: float) -> float:
        getter = getattr(config, "get_float_for_environment", None)
        if callable(getter):
            return _safe_float(getter(key, environment, default), default)
        getter = getattr(config, "get_for_environment", None)
        if callable(getter):
            return _safe_float(getter(key, environment, default), default)
        return float(default)

    def get_int(key: str, default: int) -> int:
        getter = getattr(config, "get_int_for_environment", None)
        if callable(getter):
            return _safe_int(getter(key, environment, default), default)
        return int(default)

    return normalize_harvest_settings(
        {
            "enabled": get_bool("intraday_harvest_enabled", True),
            "live_auto_enabled": get_bool("intraday_harvest_live_auto_enabled", True),
            "split_brackets_enabled": get_bool("intraday_harvest_split_brackets_enabled", True),
            "tactical_fraction": get_float("intraday_harvest_tactical_fraction", 0.30),
            "max_daily_cycles": get_int("intraday_harvest_max_daily_cycles", 2),
            "min_partial_profit_r": get_float("intraday_harvest_min_partial_profit_r", 0.60),
            "min_tighten_profit_r": get_float("intraday_harvest_min_tighten_profit_r", 0.30),
            "partial_score": get_int("intraday_harvest_partial_score", 2),
            "tighten_score": get_int("intraday_harvest_tighten_score", 1),
            "full_exit_score": get_int("intraday_harvest_full_exit_score", 4),
            "giveback_r": get_float("intraday_harvest_giveback_r", 0.45),
            "breakeven_lock_r": get_float("intraday_harvest_breakeven_lock_r", 0.10),
            "reentry_support_bps": get_float("intraday_harvest_reentry_support_bps", 35.0),
            "reentry_score": get_int("intraday_harvest_reentry_score", 2),
            "reentry_sl_atr_mult": get_float("intraday_harvest_reentry_sl_atr_mult", 1.20),
            "reentry_tp_rr": get_float("intraday_harvest_reentry_tp_rr", 1.0),
            "min_tactical_shares": get_int("intraday_harvest_min_tactical_shares", 1),
        }
    )


def split_core_tactical_quantity(quantity: int, settings: dict[str, Any] | None = None) -> dict[str, int]:
    settings = normalize_harvest_settings(settings)
    total = max(0, int(quantity or 0))
    min_lot = max(1, _safe_int(_setting(settings, "min_tactical_shares"), 1))
    if total < max(2, min_lot + 1):
        return {"core": total, "tactical": 0, "total": total, "split": False}
    tactical = int(round(total * _safe_float(_setting(settings, "tactical_fraction"), 0.30)))
    tactical = max(min_lot, tactical)
    tactical = min(total - 1, tactical)
    core = total - tactical
    if core <= 0 or tactical <= 0:
        return {"core": total, "tactical": 0, "total": total, "split": False}
    return {"core": core, "tactical": tactical, "total": total, "split": True}


def _position_basics(position: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, float | str | int]:
    direction = str(position.get("direction") or "").strip().lower()
    entry = _safe_float(position.get("entry_price", position.get("entry", 0.0)), 0.0)
    stop = _safe_float(position.get("stop_price", position.get("stop_loss", 0.0)), 0.0)
    current = _safe_float(snapshot.get("close", position.get("current_price", 0.0)), 0.0)
    high = _safe_float(snapshot.get("high"), current)
    low = _safe_float(snapshot.get("low"), current)
    risk = _safe_float(position.get("risk_r", position.get("initial_risk_r", 0.0)), 0.0)
    if risk <= 0 and entry > 0 and stop > 0:
        risk = abs(entry - stop)
    shares = max(0, _safe_int(position.get("shares", position.get("quantity", 0)), 0))
    if direction == "long":
        progress = (current - entry) / risk if risk > 0 else 0.0
        favorable = max(0.0, high - entry)
    elif direction == "short":
        progress = (entry - current) / risk if risk > 0 else 0.0
        favorable = max(0.0, entry - low)
    else:
        progress = 0.0
        favorable = 0.0
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "current": current,
        "high": high,
        "low": low,
        "risk": risk,
        "shares": shares,
        "progress_r": progress,
        "bar_mfe_r": favorable / risk if risk > 0 else 0.0,
    }


def _near_level(current: float, level: float, bps: float) -> bool:
    if current <= 0 or level <= 0:
        return False
    return abs(current - level) / current * 10000.0 <= max(0.0, bps)


def _score_decay(direction: str, snapshot: dict[str, Any], state: dict[str, Any]) -> tuple[int, list[str], dict[str, Any]]:
    score = 0
    reasons: list[str] = []
    next_state = dict(state or {})
    close = _safe_float(snapshot.get("close"), 0.0)
    ema_fast = _safe_float(snapshot.get("ema_fast"), 0.0)
    vwap = _safe_float(snapshot.get("vwap"), 0.0)
    dtp_dir = _safe_int(snapshot.get("dtp_dir"), 0)
    dtp_phase = str(snapshot.get("dtp_phase") or "").strip().lower()
    crsi = _safe_float(snapshot.get("crsi"), 50.0)
    crsi_ub = _safe_float(snapshot.get("crsi_ub"), 70.0) or 70.0
    crsi_db = _safe_float(snapshot.get("crsi_db"), 30.0) or 30.0

    if direction == "long":
        overheated = bool(snapshot.get("crsi_ob")) or crsi >= max(70.0, crsi_ub)
        if overheated:
            next_state["saw_overheated"] = True
        if next_state.get("saw_overheated") and crsi < crsi_ub:
            score += 1
            reasons.append("crsi_overbought_reversal")
        if dtp_phase == "weakening":
            score += 1
            reasons.append("dtp_phase_weakening")
        if dtp_dir <= 0:
            score += 1
            reasons.append("dtp_no_long_confirmation")
        if bool(snapshot.get("ema_weak")):
            score += 1
            reasons.append("ema_weak")
        if close > 0 and ema_fast > 0 and close < ema_fast:
            score += 1
            reasons.append("close_below_ema_fast")
        if close > 0 and vwap > 0 and close < vwap:
            score += 1
            reasons.append("close_below_vwap")
        if bool(snapshot.get("any_bear_div") or snapshot.get("crsi_bear_div") or snapshot.get("obv_bear_div")):
            score += 2
            reasons.append("bearish_divergence")
    elif direction == "short":
        overheated = bool(snapshot.get("crsi_os")) or crsi <= min(30.0, crsi_db)
        if overheated:
            next_state["saw_overheated"] = True
        if next_state.get("saw_overheated") and crsi > crsi_db:
            score += 1
            reasons.append("crsi_oversold_reversal")
        if dtp_phase == "weakening":
            score += 1
            reasons.append("dtp_phase_weakening")
        if dtp_dir >= 0:
            score += 1
            reasons.append("dtp_no_short_confirmation")
        if bool(snapshot.get("ema_weak")):
            score += 1
            reasons.append("ema_weak")
        if close > 0 and ema_fast > 0 and close > ema_fast:
            score += 1
            reasons.append("close_above_ema_fast")
        if close > 0 and vwap > 0 and close > vwap:
            score += 1
            reasons.append("close_above_vwap")
        if bool(snapshot.get("any_bull_div") or snapshot.get("crsi_bull_div") or snapshot.get("obv_bull_div")):
            score += 2
            reasons.append("bullish_divergence")
    return score, reasons, next_state


def _score_reentry(direction: str, snapshot: dict[str, Any], settings: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    close = _safe_float(snapshot.get("close"), 0.0)
    vwap = _safe_float(snapshot.get("vwap"), 0.0)
    ema_fast = _safe_float(snapshot.get("ema_fast"), 0.0)
    sd_mid = _safe_float(snapshot.get("sd_mid", snapshot.get("sd_basis", 0.0)), 0.0)
    support_bps = _safe_float(_setting(settings, "reentry_support_bps"), 35.0)
    dtp_dir = _safe_int(snapshot.get("dtp_dir"), 0)
    dtp_phase = str(snapshot.get("dtp_phase") or "").strip().lower()
    crsi = _safe_float(snapshot.get("crsi"), 50.0)
    crsi_ub = _safe_float(snapshot.get("crsi_ub"), 70.0) or 70.0
    crsi_db = _safe_float(snapshot.get("crsi_db"), 30.0) or 30.0

    if _near_level(close, vwap, support_bps):
        score += 1
        reasons.append("near_vwap_support")
    if _near_level(close, ema_fast, support_bps):
        score += 1
        reasons.append("near_ema_fast_support")
    if _near_level(close, sd_mid, support_bps):
        score += 1
        reasons.append("near_sd_mid_support")

    if direction == "long":
        if dtp_dir >= 1 or dtp_phase in {"early", "confirmed"}:
            score += 1
            reasons.append("dtp_long_recovery")
        if crsi_db < crsi < crsi_ub:
            score += 1
            reasons.append("crsi_back_to_normal")
        if bool(snapshot.get("ema_strong_bull")) and not bool(snapshot.get("ema_weak")):
            score += 1
            reasons.append("ema_bull_recovery")
    elif direction == "short":
        if dtp_dir <= -1 or dtp_phase in {"early", "confirmed"}:
            score += 1
            reasons.append("dtp_short_recovery")
        if crsi_db < crsi < crsi_ub:
            score += 1
            reasons.append("crsi_back_to_normal")
        if bool(snapshot.get("ema_strong_bear")) and not bool(snapshot.get("ema_weak")):
            score += 1
            reasons.append("ema_bear_recovery")
    return score, reasons


def suggested_stop_price(position: dict[str, Any], snapshot: dict[str, Any], settings: dict[str, Any] | None = None) -> float:
    settings = normalize_harvest_settings(settings)
    basics = _position_basics(position, snapshot)
    direction = str(basics["direction"])
    entry = float(basics["entry"])
    risk = float(basics["risk"])
    current = float(basics["current"])
    old_stop = float(basics["stop"])
    if direction not in {"long", "short"} or entry <= 0 or risk <= 0 or current <= 0:
        return 0.0
    lock_r = _safe_float(_setting(settings, "breakeven_lock_r"), 0.10)
    price_buffer = max(0.01, current * 0.001)
    if direction == "long":
        candidate = max(old_stop, entry + risk * lock_r)
        ceiling = current - price_buffer
        if old_stop > 0 and ceiling <= old_stop:
            return 0.0
        return round(min(candidate, ceiling), 4)
    candidate = min(old_stop, entry - risk * lock_r) if old_stop > 0 else entry - risk * lock_r
    floor = current + price_buffer
    if old_stop > 0 and floor >= old_stop:
        return 0.0
    return round(max(candidate, floor), 4)


def build_reentry_prices(direction: str, current_price: float, atr: float, settings: dict[str, Any] | None = None) -> dict[str, float]:
    settings = normalize_harvest_settings(settings)
    direction = str(direction or "").strip().lower()
    entry = _safe_float(current_price, 0.0)
    atr_value = _safe_float(atr, 0.0)
    if direction not in {"long", "short"} or entry <= 0 or atr_value <= 0:
        return {"entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0}
    sl_mult = _safe_float(_setting(settings, "reentry_sl_atr_mult"), 1.20)
    rr = _safe_float(_setting(settings, "reentry_tp_rr"), 1.0)
    risk = max(0.01, atr_value * sl_mult)
    if direction == "long":
        stop = entry - risk
        target = entry + risk * rr
    else:
        stop = entry + risk
        target = entry - risk * rr
    return {"entry": round(entry, 4), "stop_loss": round(stop, 4), "take_profit": round(target, 4)}


def evaluate_intraday_harvest(
    position: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    allow_reentry: bool = False,
) -> dict[str, Any]:
    settings = normalize_harvest_settings(settings)
    state = dict(state or {})
    basics = _position_basics(position, snapshot)
    direction = str(basics["direction"])
    current = float(basics["current"])
    risk = float(basics["risk"])
    shares = int(basics["shares"])
    if not _safe_bool(_setting(settings, "enabled"), True):
        return {"action": ACTION_HOLD, "reason": "harvest_disabled", "state": state, "score": 0, "reasons": []}
    if direction not in {"long", "short"} or current <= 0 or risk <= 0 or shares <= 0:
        return {"action": ACTION_HOLD, "reason": "invalid_position", "state": state, "score": 0, "reasons": []}

    bar_ms = _safe_int(snapshot.get("bar_time_ms"), 0)
    if bar_ms > 0 and _safe_int(state.get("last_action_bar_ms"), 0) == bar_ms:
        return {"action": ACTION_HOLD, "reason": "already_acted_this_bar", "state": state, "score": 0, "reasons": []}

    score, reasons, next_state = _score_decay(direction, snapshot, state)
    progress_r = float(basics["progress_r"])
    mfe_r = max(_safe_float(next_state.get("highest_mfe_r"), 0.0), float(basics["bar_mfe_r"]), _safe_float(position.get("mfe"), 0.0) / risk)
    next_state["highest_mfe_r"] = round(mfe_r, 4)
    next_state["last_progress_r"] = round(progress_r, 4)
    next_state["last_score"] = score
    next_state["last_reasons"] = list(reasons)
    next_state["profile"] = INTRADAY_VOLATILITY_HARVEST_PROFILE
    if bar_ms > 0:
        next_state["last_bar_ms"] = bar_ms

    giveback = max(0.0, mfe_r - progress_r)
    if mfe_r >= _safe_float(_setting(settings, "min_partial_profit_r"), 0.60) and giveback >= _safe_float(_setting(settings, "giveback_r"), 0.45):
        score += 1
        reasons.append("mfe_giveback")

    partial_exited = _safe_bool(next_state.get("partial_exited"), False)
    cycles = max(0, _safe_int(next_state.get("cycles"), 0))
    max_cycles = max(0, _safe_int(_setting(settings, "max_daily_cycles"), 2))

    if allow_reentry and partial_exited and cycles < max_cycles:
        reentry_score, reentry_reasons = _score_reentry(direction, snapshot, settings)
        if reentry_score >= _safe_int(_setting(settings, "reentry_score"), 2):
            prices = build_reentry_prices(direction, current, _safe_float(snapshot.get("atr"), 0.0), settings)
            next_state["last_action"] = ACTION_REENTRY
            next_state["last_action_bar_ms"] = bar_ms
            return {
                "action": ACTION_REENTRY,
                "score": reentry_score,
                "reasons": reentry_reasons,
                "reason": ",".join(reentry_reasons) or "reentry_conditions_met",
                "quantity_fraction": _safe_float(_setting(settings, "tactical_fraction"), 0.30),
                "state": next_state,
                "reentry_prices": prices,
                "progress_r": round(progress_r, 4),
                "mfe_r": round(mfe_r, 4),
            }

    trend_break = any(item in reasons for item in ("close_below_vwap", "close_above_vwap")) and any(
        item in reasons for item in ("bearish_divergence", "bullish_divergence", "dtp_no_long_confirmation", "dtp_no_short_confirmation")
    )
    if score >= _safe_int(_setting(settings, "full_exit_score"), 4) or (trend_break and progress_r <= 0.2):
        next_state["last_action"] = ACTION_FULL_EXIT
        next_state["last_action_bar_ms"] = bar_ms
        return {
            "action": ACTION_FULL_EXIT,
            "score": score,
            "reasons": reasons,
            "reason": ",".join(reasons) or "trend_decay_full_exit",
            "quantity_fraction": 1.0,
            "state": next_state,
            "progress_r": round(progress_r, 4),
            "mfe_r": round(mfe_r, 4),
        }

    if (not partial_exited) and cycles < max_cycles and progress_r >= _safe_float(_setting(settings, "min_partial_profit_r"), 0.60) and score >= _safe_int(_setting(settings, "partial_score"), 2):
        next_state["partial_exited"] = True
        next_state["cycles"] = cycles + 1
        next_state["last_action"] = ACTION_PARTIAL_EXIT
        next_state["last_action_bar_ms"] = bar_ms
        stop_price = suggested_stop_price(position, snapshot, settings)
        return {
            "action": ACTION_PARTIAL_EXIT,
            "score": score,
            "reasons": reasons,
            "reason": ",".join(reasons) or "momentum_decay_partial_exit",
            "quantity_fraction": _safe_float(_setting(settings, "tactical_fraction"), 0.30),
            "state": next_state,
            "stop_price": stop_price,
            "progress_r": round(progress_r, 4),
            "mfe_r": round(mfe_r, 4),
        }

    if progress_r >= _safe_float(_setting(settings, "min_tighten_profit_r"), 0.30) and score >= _safe_int(_setting(settings, "tighten_score"), 1):
        next_state["last_action"] = ACTION_TIGHTEN_STOP
        stop_price = suggested_stop_price(position, snapshot, settings)
        return {
            "action": ACTION_TIGHTEN_STOP,
            "score": score,
            "reasons": reasons,
            "reason": ",".join(reasons) or "momentum_decay_tighten_stop",
            "quantity_fraction": 0.0,
            "state": next_state,
            "stop_price": stop_price,
            "progress_r": round(progress_r, 4),
            "mfe_r": round(mfe_r, 4),
        }

    return {
        "action": ACTION_HOLD,
        "score": score,
        "reasons": reasons,
        "reason": "no_harvest_action",
        "quantity_fraction": 0.0,
        "state": next_state,
        "progress_r": round(progress_r, 4),
        "mfe_r": round(mfe_r, 4),
    }


__all__ = [
    "ACTION_FULL_EXIT",
    "ACTION_HOLD",
    "ACTION_PARTIAL_EXIT",
    "ACTION_REENTRY",
    "ACTION_TIGHTEN_STOP",
    "DEFAULT_HARVEST_SETTINGS",
    "INTRADAY_VOLATILITY_HARVEST_PROFILE",
    "build_reentry_prices",
    "evaluate_intraday_harvest",
    "harvest_settings_from_config",
    "normalize_harvest_settings",
    "split_core_tactical_quantity",
    "suggested_stop_price",
]
