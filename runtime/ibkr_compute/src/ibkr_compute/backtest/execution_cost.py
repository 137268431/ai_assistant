from __future__ import annotations

import json
import math
from typing import Any

from ibkr_compute.backtest import constants


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return float(default)
    try:
        return float(text)
    except Exception:
        return float(default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(default)


def _safe_profile(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            return {}
    return {}


def normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or default).strip().lower() or default
    return text if text in allowed else default


def build_execution_cost_profile(
    request: dict | None = None,
    *,
    commission_per_share: float | None = None,
    slippage_bps: float | None = None,
) -> dict:
    request = dict(request or {})
    custom = _safe_profile(request.get("execution_cost_profile"))
    fee_model = normalize_choice(
        request.get("fee_model") or custom.get("fee_model"),
        constants.FEE_MODEL_VALUES,
        constants.DEFAULT_FEE_MODEL,
    )
    slippage_model = normalize_choice(
        request.get("slippage_model") or custom.get("slippage_model"),
        constants.SLIPPAGE_MODEL_VALUES,
        constants.DEFAULT_SLIPPAGE_MODEL,
    )
    account_model_mode = normalize_choice(
        request.get("account_model_mode") or custom.get("account_model_mode"),
        constants.ACCOUNT_MODEL_MODE_VALUES,
        constants.DEFAULT_ACCOUNT_MODEL_MODE,
    )

    commission = _safe_float(
        commission_per_share if commission_per_share is not None else request.get("commission_per_share"),
        constants.DEFAULT_COMMISSION_PER_SHARE,
    )
    slip_bps = _safe_float(
        slippage_bps if slippage_bps is not None else request.get("slippage_bps"),
        constants.DEFAULT_SLIPPAGE_BPS,
    )
    cap_to_bar_default = slippage_model in {"bar_capped_bps_v1", "volume_share_v1"}
    limit_protection_default = slippage_model in {"bar_capped_bps_v1", "volume_share_v1"}
    return {
        "account_model_mode": account_model_mode,
        "fee_model": fee_model,
        "slippage_model": slippage_model,
        "execution_cost_profile": custom,
        "commission_per_share": max(0.0, commission),
        "slippage_bps": max(0.0, slip_bps),
        "include_regulatory_fees": _safe_bool(
            request.get("include_regulatory_fees", custom.get("include_regulatory_fees")),
            False,
        ),
        "slippage_cap_to_bar": _safe_bool(
            request.get("slippage_cap_to_bar", custom.get("slippage_cap_to_bar")),
            cap_to_bar_default,
        ),
        "limit_price_protection": _safe_bool(
            request.get("limit_price_protection", custom.get("limit_price_protection")),
            limit_protection_default,
        ),
        "stop_gap_to_open": _safe_bool(
            request.get("stop_gap_to_open", custom.get("stop_gap_to_open")),
            slippage_model in {"bar_capped_bps_v1", "volume_share_v1"},
        ),
        "ibkr_fixed_per_share": max(0.0, _safe_float(custom.get("ibkr_fixed_per_share"), 0.005)),
        "ibkr_fixed_min_commission": max(0.0, _safe_float(custom.get("ibkr_fixed_min_commission"), 1.0)),
        "ibkr_fixed_max_pct_trade_value": max(0.0, _safe_float(custom.get("ibkr_fixed_max_pct_trade_value"), 0.01)),
        "ibkr_tiered_per_share": max(0.0, _safe_float(custom.get("ibkr_tiered_per_share"), 0.0035)),
        "ibkr_tiered_min_commission": max(0.0, _safe_float(custom.get("ibkr_tiered_min_commission"), 0.35)),
        "ibkr_tiered_max_pct_trade_value": max(0.0, _safe_float(custom.get("ibkr_tiered_max_pct_trade_value"), 0.01)),
        "calibrated_per_share": max(0.0, _safe_float(custom.get("calibrated_per_share"), commission)),
        "calibrated_min_commission": max(0.0, _safe_float(custom.get("calibrated_min_commission"), 0.0)),
        "calibrated_max_pct_trade_value": max(0.0, _safe_float(custom.get("calibrated_max_pct_trade_value"), 0.0)),
        "sec_fee_rate": max(0.0, _safe_float(custom.get("sec_fee_rate"), 0.0)),
        "taf_fee_per_share": max(0.0, _safe_float(custom.get("taf_fee_per_share"), 0.0)),
        "taf_fee_cap": max(0.0, _safe_float(custom.get("taf_fee_cap"), 0.0)),
        "volume_impact_coeff_bps": max(0.0, _safe_float(custom.get("volume_impact_coeff_bps"), 25.0)),
        "volume_impact_power": max(0.0, _safe_float(custom.get("volume_impact_power"), 0.5)),
        "volume_impact_cap_bps": max(0.0, _safe_float(custom.get("volume_impact_cap_bps"), 50.0)),
        "premarket_slippage_mult": max(0.0, _safe_float(custom.get("premarket_slippage_mult"), 2.0)),
        "afterhours_slippage_mult": max(0.0, _safe_float(custom.get("afterhours_slippage_mult"), 2.0)),
        "regular_slippage_mult": max(0.0, _safe_float(custom.get("regular_slippage_mult"), 1.0)),
    }


def compact_execution_cost_profile(profile: dict | None) -> dict:
    profile = dict(profile or {})
    return {
        "account_model_mode": profile.get("account_model_mode", constants.DEFAULT_ACCOUNT_MODEL_MODE),
        "fee_model": profile.get("fee_model", constants.DEFAULT_FEE_MODEL),
        "slippage_model": profile.get("slippage_model", constants.DEFAULT_SLIPPAGE_MODEL),
        "commission_per_share": round(_safe_float(profile.get("commission_per_share"), 0.0), 6),
        "slippage_bps": round(_safe_float(profile.get("slippage_bps"), 0.0), 4),
        "include_regulatory_fees": bool(profile.get("include_regulatory_fees")),
        "slippage_cap_to_bar": bool(profile.get("slippage_cap_to_bar")),
        "limit_price_protection": bool(profile.get("limit_price_protection")),
        "stop_gap_to_open": bool(profile.get("stop_gap_to_open")),
    }


def execution_side(direction: str, is_entry: bool) -> str:
    normalized = str(direction or "").strip().lower()
    if normalized == "long":
        return "buy" if is_entry else "sell"
    if normalized == "short":
        return "sell" if is_entry else "buy"
    return ""


def calculate_execution_commission(
    *,
    shares: float,
    price: float,
    side: str,
    profile: dict | None = None,
) -> dict:
    profile = dict(profile or build_execution_cost_profile({}))
    quantity = max(0.0, abs(_safe_float(shares, 0.0)))
    fill_price = max(0.0, _safe_float(price, 0.0))
    trade_value = quantity * fill_price
    fee_model = normalize_choice(profile.get("fee_model"), constants.FEE_MODEL_VALUES, constants.DEFAULT_FEE_MODEL)

    if quantity <= 0 or fill_price <= 0:
        base_commission = 0.0
    elif fee_model == "ibkr_us_equity_fixed_v1":
        base_commission = quantity * _safe_float(profile.get("ibkr_fixed_per_share"), 0.005)
        min_commission = _safe_float(profile.get("ibkr_fixed_min_commission"), 1.0)
        max_pct = _safe_float(profile.get("ibkr_fixed_max_pct_trade_value"), 0.01)
        if min_commission > 0:
            base_commission = max(base_commission, min_commission)
        if max_pct > 0 and trade_value > 0:
            base_commission = min(base_commission, trade_value * max_pct)
    elif fee_model == "ibkr_us_equity_tiered_v1":
        base_commission = quantity * _safe_float(profile.get("ibkr_tiered_per_share"), 0.0035)
        min_commission = _safe_float(profile.get("ibkr_tiered_min_commission"), 0.35)
        max_pct = _safe_float(profile.get("ibkr_tiered_max_pct_trade_value"), 0.01)
        if min_commission > 0:
            base_commission = max(base_commission, min_commission)
        if max_pct > 0 and trade_value > 0:
            base_commission = min(base_commission, trade_value * max_pct)
    elif fee_model == "calibrated_v1":
        base_commission = quantity * _safe_float(profile.get("calibrated_per_share"), profile.get("commission_per_share", 0.0))
        min_commission = _safe_float(profile.get("calibrated_min_commission"), 0.0)
        max_pct = _safe_float(profile.get("calibrated_max_pct_trade_value"), 0.0)
        if min_commission > 0:
            base_commission = max(base_commission, min_commission)
        if max_pct > 0 and trade_value > 0:
            base_commission = min(base_commission, trade_value * max_pct)
    else:
        base_commission = quantity * _safe_float(profile.get("commission_per_share"), 0.0)

    regulatory_fee = 0.0
    normalized_side = str(side or "").strip().lower()
    if profile.get("include_regulatory_fees") and normalized_side == "sell" and trade_value > 0:
        regulatory_fee += trade_value * _safe_float(profile.get("sec_fee_rate"), 0.0)
        taf_per_share = _safe_float(profile.get("taf_fee_per_share"), 0.0)
        taf_cap = _safe_float(profile.get("taf_fee_cap"), 0.0)
        taf_fee = quantity * taf_per_share
        if taf_cap > 0:
            taf_fee = min(taf_fee, taf_cap)
        regulatory_fee += taf_fee

    total = max(0.0, base_commission) + max(0.0, regulatory_fee)
    return {
        "commission": round(total, 6),
        "base_commission": round(max(0.0, base_commission), 6),
        "regulatory_fee": round(max(0.0, regulatory_fee), 6),
        "fee_model": fee_model,
        "side": normalized_side,
        "shares": round(quantity, 6),
        "trade_value": round(trade_value, 6),
    }


def _session_multiplier(session_type: str, profile: dict) -> float:
    session = str(session_type or "regular").strip().lower()
    if session == "premarket":
        return _safe_float(profile.get("premarket_slippage_mult"), 2.0)
    if session == "afterhours":
        return _safe_float(profile.get("afterhours_slippage_mult"), 2.0)
    return _safe_float(profile.get("regular_slippage_mult"), 1.0)


def _dynamic_slippage_bps(
    *,
    price: float,
    shares: float,
    bar: dict | None,
    profile: dict,
) -> float:
    model = normalize_choice(profile.get("slippage_model"), constants.SLIPPAGE_MODEL_VALUES, constants.DEFAULT_SLIPPAGE_MODEL)
    base_bps = max(0.0, _safe_float(profile.get("slippage_bps"), 0.0))
    multiplier = _session_multiplier((bar or {}).get("session_type", "regular"), profile)
    if model != "volume_share_v1":
        return base_bps * multiplier

    volume = max(0.0, _safe_float((bar or {}).get("volume"), 0.0))
    close = _safe_float((bar or {}).get("close"), price)
    dollar_volume = volume * max(close, price, 0.0)
    order_value = max(0.0, abs(_safe_float(shares, 0.0)) * max(price, 0.0))
    ratio = (order_value / dollar_volume) if dollar_volume > 0 else 0.0
    impact = _safe_float(profile.get("volume_impact_coeff_bps"), 25.0) * math.pow(max(0.0, ratio), _safe_float(profile.get("volume_impact_power"), 0.5))
    cap = _safe_float(profile.get("volume_impact_cap_bps"), 50.0)
    if cap > 0:
        impact = min(impact, cap)
    return max(0.0, (base_bps + impact) * multiplier)


def _apply_adverse_bps(price: float, direction: str, is_entry: bool, bps: float) -> float:
    if price <= 0:
        return 0.0
    slip = max(0.0, float(bps)) / 10000.0
    normalized = str(direction or "").strip().lower()
    if normalized == "long":
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    if normalized == "short":
        return price * (1.0 - slip) if is_entry else price * (1.0 + slip)
    return price


def _cap_to_bar(fill_price: float, bar: dict | None) -> tuple[float, bool]:
    if not bar or fill_price <= 0:
        return fill_price, False
    high = _safe_float(bar.get("high"), 0.0)
    low = _safe_float(bar.get("low"), 0.0)
    if high <= 0 or low <= 0 or high < low:
        return fill_price, False
    capped = min(max(fill_price, low), high)
    return capped, abs(capped - fill_price) > 1e-9


def _protect_limit_price(fill_price: float, direction: str, limit_price: float) -> tuple[float, bool]:
    limit = _safe_float(limit_price, 0.0)
    if fill_price <= 0 or limit <= 0:
        return fill_price, False
    normalized = str(direction or "").strip().lower()
    if normalized == "long" and fill_price > limit:
        return limit, True
    if normalized == "short" and fill_price < limit:
        return limit, True
    return fill_price, False


def apply_execution_slippage(
    *,
    price: float,
    direction: str,
    is_entry: bool,
    profile: dict | None = None,
    bar: dict | None = None,
    shares: float = 0.0,
    limit_price: float = 0.0,
    order_type: str = "",
) -> dict:
    profile = dict(profile or build_execution_cost_profile({}))
    reference_price = max(0.0, _safe_float(price, 0.0))
    model = normalize_choice(profile.get("slippage_model"), constants.SLIPPAGE_MODEL_VALUES, constants.DEFAULT_SLIPPAGE_MODEL)
    bps = _dynamic_slippage_bps(price=reference_price, shares=shares, bar=bar, profile=profile)
    fill_price = _apply_adverse_bps(reference_price, direction, is_entry, bps)

    bar_cap_applied = False
    if profile.get("slippage_cap_to_bar"):
        fill_price, bar_cap_applied = _cap_to_bar(fill_price, bar)

    limit_cap_applied = False
    if is_entry and profile.get("limit_price_protection"):
        normalized_order_type = str(order_type or "").strip().lower()
        if normalized_order_type in {"limit", "lmt", "marketable_limit"} or limit_price > 0:
            fill_price, limit_cap_applied = _protect_limit_price(fill_price, direction, limit_price)

    applied_bps = (abs(fill_price - reference_price) / reference_price * 10000.0) if reference_price > 0 else 0.0
    slippage_cost = abs(fill_price - reference_price) * max(0.0, abs(_safe_float(shares, 0.0)))
    return {
        "raw_reference_price": round(reference_price, 6),
        "fill_price": fill_price,
        "fill_price_rounded": round(fill_price, 4),
        "slippage_bps_requested": round(bps, 6),
        "slippage_bps_applied": round(applied_bps, 6),
        "slippage_cost": round(slippage_cost, 6),
        "slippage_model": model,
        "bar_cap_applied": bar_cap_applied,
        "limit_cap_applied": limit_cap_applied,
    }


def maybe_stop_gap_reference(
    *,
    raw_exit_price: float,
    direction: str,
    exit_reason: str,
    bar: dict,
    profile: dict | None = None,
) -> float:
    profile = dict(profile or build_execution_cost_profile({}))
    price = _safe_float(raw_exit_price, 0.0)
    if price <= 0 or str(exit_reason or "").strip().lower() != "stop_loss" or not profile.get("stop_gap_to_open"):
        return price
    bar_open = _safe_float((bar or {}).get("open"), 0.0)
    if bar_open <= 0:
        return price
    normalized = str(direction or "").strip().lower()
    if normalized == "long" and bar_open < price:
        return bar_open
    if normalized == "short" and bar_open > price:
        return bar_open
    return price


def summarize_execution_costs(trades: list[dict], profile: dict | None = None) -> dict:
    profile = dict(profile or build_execution_cost_profile({}))
    total_commission = 0.0
    total_slippage = 0.0
    gross_pnl = 0.0
    net_pnl = 0.0
    entry_slippage_bps = []
    exit_slippage_bps = []
    for trade in trades or []:
        extra = trade.get("extra") if isinstance(trade.get("extra"), dict) else {}
        total_commission += _safe_float(extra.get("total_commission"), 0.0)
        total_slippage += _safe_float(extra.get("estimated_slippage_cost"), 0.0)
        gross_pnl += _safe_float(extra.get("gross_pnl"), _safe_float(trade.get("pnl"), 0.0))
        net_pnl += _safe_float(trade.get("pnl"), 0.0)
        entry_slippage_bps.append(_safe_float(extra.get("entry_slippage_bps"), 0.0))
        exit_slippage_bps.append(_safe_float(extra.get("exit_slippage_bps"), 0.0))
    trade_count = len(trades or [])
    gross_profit_abs = abs(gross_pnl)
    return {
        **compact_execution_cost_profile(profile),
        "trade_count": trade_count,
        "total_commission": round(total_commission, 4),
        "estimated_slippage_cost": round(total_slippage, 4),
        "gross_pnl": round(gross_pnl, 4),
        "net_pnl": round(net_pnl, 4),
        "cost_drag": round(total_commission + total_slippage, 4),
        "cost_to_gross_pnl_pct": round(((total_commission + total_slippage) / gross_profit_abs * 100.0), 4) if gross_profit_abs > 0 else 0.0,
        "avg_entry_slippage_bps": round(sum(entry_slippage_bps) / trade_count, 4) if trade_count else 0.0,
        "avg_exit_slippage_bps": round(sum(exit_slippage_bps) / trade_count, 4) if trade_count else 0.0,
    }

