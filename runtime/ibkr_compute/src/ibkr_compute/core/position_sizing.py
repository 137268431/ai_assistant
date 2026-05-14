"""仓位计算 — 入场/止损/止盈/股数"""

import math


PASSIVE_LIMIT_DYNAMIC_MODES = {
    "dynamic",
    "passive_limit_dynamic",
    "passive-limit-dynamic",
    "passive_limit_dynamic_v1",
    "marketable_limit_dynamic",
    "marketable-limit-dynamic",
    "marketable_limit_dynamic_v1",
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _marketable_limit_offset(close: float, atr: float, params: dict) -> tuple[float, str]:
    mode = str(
        params.get("entry_limit_mode")
        or params.get("entry_price_plan")
        or params.get("entry_plan")
        or "passive_limit_dynamic"
    ).strip().lower()
    if mode in PASSIVE_LIMIT_DYNAMIC_MODES:
        atr_mult = max(0.0, _safe_float(params.get("entry_limit_atr_mult"), 0.30))
        floor_bps = max(0.0, _safe_float(params.get("entry_limit_floor_bps"), 15.0))
        cap_bps = max(0.0, _safe_float(params.get("entry_limit_cap_bps"), 30.0))
        floor = close * floor_bps / 10000.0
        cap = max(floor, close * cap_bps / 10000.0)
        raw = max(0.0, atr) * atr_mult
        offset = min(max(raw, floor), cap)
        return (max(0.01, offset) if close > 0 else 0.0), "passive_limit_dynamic"

    marketable_limit_bps = max(0.0, _safe_float(params.get("marketable_limit_bps"), 10.0))
    offset = max(0.01, close * marketable_limit_bps / 10000.0) if close > 0 else 0.0
    return offset, "marketable_limit_bps"


def calc_long_position(close: float, atr: float, params: dict) -> dict:
    """Calculate long entry / stop-loss / take-profit / shares.

    params: entry_atr_mult, sl_atr_mult, rr_ratio, position_amount,
            max_loss_per_trade, atr_multiplier
    """
    entry_atr_mult = params.get("entry_atr_mult", 1.0)
    sl_atr_mult = params.get("sl_atr_mult", 2.0)
    rr_ratio = params.get("rr_ratio", 1.5)
    position_amount = params.get("position_amount", 10000)
    max_loss = params.get("max_loss_per_trade", 150)
    atr_raw = atr / params.get("atr_multiplier", 1.5) if params.get("atr_multiplier", 1.5) != 0 else atr

    entry = close - atr * entry_atr_mult
    sl_atr = entry - atr * sl_atr_mult
    shares = math.ceil(position_amount / entry) if entry > 0 else 0
    sl_max = entry - max_loss / shares if shares > 0 else sl_atr
    sl = max(sl_atr, sl_max)
    sl_dist = entry - sl
    tp = entry + sl_dist * rr_ratio
    sl_dist_pct = sl_dist / entry * 100 if entry > 0 else 0.0
    sl_atr_ratio = sl_dist / atr_raw if atr_raw > 0 else 0.0

    return {
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "shares": shares,
        "rr": rr_ratio,
        "sl_dist_pct": sl_dist_pct,
        "sl_atr_ratio": sl_atr_ratio,
    }


def calc_short_position(close: float, atr: float, params: dict) -> dict:
    """Calculate short entry / stop-loss / take-profit / shares.

    params: entry_atr_mult, sl_atr_mult, rr_ratio, position_amount,
            max_loss_per_trade, atr_multiplier
    """
    entry_atr_mult = params.get("entry_atr_mult", 1.0)
    sl_atr_mult = params.get("sl_atr_mult", 2.0)
    rr_ratio = params.get("rr_ratio", 1.5)
    position_amount = params.get("position_amount", 10000)
    max_loss = params.get("max_loss_per_trade", 150)
    atr_raw = atr / params.get("atr_multiplier", 1.5) if params.get("atr_multiplier", 1.5) != 0 else atr

    entry = close + atr * entry_atr_mult
    sl_atr = entry + atr * sl_atr_mult
    shares = math.ceil(position_amount / entry) if entry > 0 else 0
    sl_max = entry + max_loss / shares if shares > 0 else sl_atr
    sl = min(sl_atr, sl_max)
    sl_dist = sl - entry
    tp = entry - sl_dist * rr_ratio
    sl_dist_pct = sl_dist / entry * 100 if entry > 0 else 0.0
    sl_atr_ratio = sl_dist / atr_raw if atr_raw > 0 else 0.0

    return {
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "shares": shares,
        "rr": rr_ratio,
        "sl_dist_pct": sl_dist_pct,
        "sl_atr_ratio": sl_atr_ratio,
    }


def calc_marketable_limit_position(close: float, atr: float, params: dict, direction: str) -> dict:
    """Calculate limit entry pricing while keeping ATR risk controls.

    Dynamic entries are passive: longs bid below the latest close and shorts
    offer above it. The old fixed-bps marketable mode keeps its legacy side.
    """
    close = float(close or 0.0)
    atr = float(atr or 0.0)
    sl_atr_mult = params.get("sl_atr_mult", 2.0)
    rr_ratio = params.get("rr_ratio", 1.5)
    position_amount = params.get("position_amount", 10000)
    max_loss = params.get("max_loss_per_trade", 150)
    atr_raw = atr / params.get("atr_multiplier", 1.5) if params.get("atr_multiplier", 1.5) != 0 else atr

    offset, entry_limit_mode = _marketable_limit_offset(close, atr, params or {})
    passive_limit = entry_limit_mode == "passive_limit_dynamic"
    if str(direction or "").lower() == "short":
        entry = close + offset if passive_limit else close - offset
        sl_atr = entry + atr * sl_atr_mult
        shares = math.ceil(position_amount / entry) if entry > 0 else 0
        sl_max = entry + max_loss / shares if shares > 0 else sl_atr
        sl = min(sl_atr, sl_max)
        sl_dist = sl - entry
        tp = entry - sl_dist * rr_ratio
    else:
        entry = close - offset if passive_limit else close + offset
        sl_atr = entry - atr * sl_atr_mult
        shares = math.ceil(position_amount / entry) if entry > 0 else 0
        sl_max = entry - max_loss / shares if shares > 0 else sl_atr
        sl = max(sl_atr, sl_max)
        sl_dist = entry - sl
        tp = entry + sl_dist * rr_ratio

    sl_dist_pct = sl_dist / entry * 100 if entry > 0 else 0.0
    sl_atr_ratio = sl_dist / atr_raw if atr_raw > 0 else 0.0
    return {
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "shares": shares,
        "rr": rr_ratio,
        "sl_dist_pct": sl_dist_pct,
        "sl_atr_ratio": sl_atr_ratio,
        "entry_limit_offset": offset,
        "entry_limit_mode": entry_limit_mode,
    }
