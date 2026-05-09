"""仓位计算 — 入场/止损/止盈/股数"""

import math


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
    """Calculate a marketable limit entry while keeping ATR risk controls.

    Long entries use a limit above the latest close; short entries use a limit
    below the latest close. This keeps the order type as LimitOrder but makes
    the simulated/live entry intent immediate instead of waiting for a pullback.
    """
    close = float(close or 0.0)
    atr = float(atr or 0.0)
    marketable_limit_bps = max(0.0, float(params.get("marketable_limit_bps", 10) or 0.0))
    sl_atr_mult = params.get("sl_atr_mult", 2.0)
    rr_ratio = params.get("rr_ratio", 1.5)
    position_amount = params.get("position_amount", 10000)
    max_loss = params.get("max_loss_per_trade", 150)
    atr_raw = atr / params.get("atr_multiplier", 1.5) if params.get("atr_multiplier", 1.5) != 0 else atr

    offset = max(0.01, close * marketable_limit_bps / 10000.0) if close > 0 else 0.0
    if str(direction or "").lower() == "short":
        entry = close - offset
        sl_atr = entry + atr * sl_atr_mult
        shares = math.ceil(position_amount / entry) if entry > 0 else 0
        sl_max = entry + max_loss / shares if shares > 0 else sl_atr
        sl = min(sl_atr, sl_max)
        sl_dist = sl - entry
        tp = entry - sl_dist * rr_ratio
    else:
        entry = close + offset
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
