"""Reusable dynamic symbol admission for IBKR universe selection."""

from __future__ import annotations

from math import isfinite
from typing import Any

DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE = 58.0
ADMISSION_SCORE_CAP = 100.0

REJECTION_BUCKET_AVG_10D = "avg_10d_volume_below_threshold"
REJECTION_BUCKET_PREMARKET = "premarket_volume_below_threshold"
REJECTION_BUCKET_ATR = "atr_pct_below_threshold"
REJECTION_BUCKET_DAY_CHANGE = "day_change_below_threshold"
REJECTION_BUCKET_ADMISSION_SCORE = "admission_score_below_threshold"
REJECTION_BUCKET_PRICE = "price_unusable"

DEFAULT_MIN_AVG_10D_VOLUME = 100_000.0
DEFAULT_MIN_PREMARKET_VOLUME = 5_000.0
DEFAULT_MIN_ATR_PCT = 0.15
DEFAULT_MIN_ABS_DAY_CHANGE_PCT = 1.0

_TRUTHY_TEXT = {"1", "true", "yes", "y", "on", "enabled", "enable"}
_FALSY_TEXT = {"0", "false", "no", "n", "off", "disabled", "disable"}


def normalize_admission_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUTHY_TEXT:
        return True
    if text in _FALSY_TEXT:
        return False
    return default


def evaluate_dynamic_admission(
    symbol: str,
    *,
    fundamentals: dict | None = None,
    metrics: dict | None = None,
    settings: dict | None = None,
    direction_bias: str | None = None,
) -> dict:
    metric_row = _as_dict(metrics)
    nested_fundamentals = _as_dict(
        _first_present(
            metric_row.get("fundamentals"),
            metric_row.get("symbol_fundamentals"),
            metric_row.get("fundamental"),
        )
    )
    fundamental_row = {
        **nested_fundamentals,
        **_as_dict(fundamentals),
    }
    settings_row = _as_dict(settings)
    normalized_symbol = str(symbol or metric_row.get("symbol") or "").strip().upper()

    profile = _build_symbol_profile(
        normalized_symbol,
        fundamentals=fundamental_row,
        metrics=metric_row,
    )
    thresholds = _build_dynamic_thresholds(profile, settings_row)
    failed_gates = _build_failed_gates(normalized_symbol, profile, thresholds)
    admission_score, score_components = _build_admission_score(profile, thresholds)

    min_score = _safe_float(
        thresholds.get("admission_score_gte"),
        _safe_float(settings_row.get("dynamic_admission_min_score"), DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE),
    )
    min_score = max(0.0, min(ADMISSION_SCORE_CAP, min_score))
    if admission_score < min_score:
        failed_gates.append(
            _failed_gate(
                symbol=normalized_symbol,
                bucket=REJECTION_BUCKET_ADMISSION_SCORE,
                metric="admission_score",
                actual=_format_number(admission_score),
                threshold=_format_number(min_score),
                note="Dynamic admission score is below the configured minimum.",
                severity="blocking",
            )
        )

    blocking_failures = [gate for gate in failed_gates if str(gate.get("severity")) == "blocking"]
    strategy_policy = _build_strategy_policy(
        profile,
        admission_score=admission_score,
        min_score=min_score,
        direction_bias=direction_bias,
        blocking_failures=blocking_failures,
    )
    reason_tags = _build_reason_tags(profile, thresholds, admission_score, min_score, strategy_policy)

    return {
        "symbol": normalized_symbol,
        "quality_gate_passed": admission_score >= min_score and not blocking_failures,
        "admission_score": round(admission_score, 3),
        "admission_score_components": score_components,
        "symbol_profile": profile,
        "dynamic_thresholds": thresholds,
        "failed_gates": failed_gates,
        "strategy_policy": strategy_policy,
        "reason_tags": reason_tags,
    }


def _build_symbol_profile(symbol: str, *, fundamentals: dict, metrics: dict) -> dict:
    fundamentals_extra = _as_dict(fundamentals.get("extra"))
    price = _safe_float(
        _first_present(metrics.get("price"), _fundamental_value(fundamentals, fundamentals_extra, "price"))
    )
    avg_10d_volume = _safe_float(
        _first_present(
            metrics.get("avg_10d_volume"),
            metrics.get("avg_volume_10d"),
            _fundamental_value(fundamentals, fundamentals_extra, "avg_10d_volume", "avg_volume_10d_provider"),
        )
    )
    premarket_volume = _safe_float(metrics.get("premarket_volume"))
    today_volume = _safe_float(metrics.get("today_volume"))
    atr_pct = abs(_safe_float(metrics.get("atr_pct")))
    day_change_pct = _safe_float(metrics.get("day_change_pct"))
    abs_day_change_pct = abs(day_change_pct)
    rvol_20 = max(
        _safe_float(metrics.get("rvol_20")),
        _safe_float(metrics.get("relative_volume")),
        _safe_float(metrics.get("rvol")),
    )
    if rvol_20 <= 0 and avg_10d_volume > 0 and today_volume > 0:
        rvol_20 = today_volume / avg_10d_volume

    dollar_volume = _safe_float(metrics.get("dollar_volume"))
    if dollar_volume <= 0 and price > 0 and today_volume > 0:
        dollar_volume = price * today_volume
    avg_dollar_volume = price * avg_10d_volume if price > 0 and avg_10d_volume > 0 else 0.0

    market_cap = _safe_float(
        _first_present(
            metrics.get("market_cap"),
            metrics.get("marketCap"),
            _fundamental_value(
                fundamentals,
                fundamentals_extra,
                "market_cap",
                "marketCap",
                "mkt_cap",
                "market_cap_usd",
            ),
        )
    )
    if market_cap <= 0:
        market_cap_millions = _safe_float(
            _first_present(
                metrics.get("market_cap_millions"),
                _fundamental_value(fundamentals, fundamentals_extra, "market_cap_millions", "marketCapitalization"),
            )
        )
        if market_cap_millions > 0:
            market_cap = market_cap_millions * 1_000_000.0
    shares_float = _safe_float(
        _first_present(
            metrics.get("shares_float"),
            metrics.get("float_shares"),
            _fundamental_value(
                fundamentals,
                fundamentals_extra,
                "shares_float",
                "float_shares",
                "float",
                "public_float",
            ),
        )
    )
    shares_outstanding = _safe_float(
        _first_present(
            metrics.get("shares_outstanding"),
            _fundamental_value(
                fundamentals,
                fundamentals_extra,
                "shares_outstanding",
                "sharesOutstanding",
                "share_outstanding",
            ),
        )
    )
    if shares_outstanding <= 0:
        share_outstanding_millions = _safe_float(
            _first_present(
                metrics.get("share_outstanding_millions"),
                _fundamental_value(
                    fundamentals,
                    fundamentals_extra,
                    "share_outstanding_millions",
                    "shares_outstanding_millions",
                ),
            )
        )
        if share_outstanding_millions > 0:
            shares_outstanding = share_outstanding_millions * 1_000_000.0
    short_float_pct = abs(
        _safe_float(
            _first_present(
                metrics.get("short_float_pct"),
                _fundamental_value(fundamentals, fundamentals_extra, "short_float_pct", "shortPercentOfFloat"),
            )
        )
    )
    if short_float_pct > 0 and short_float_pct <= 1:
        short_float_pct *= 100.0

    data_quality = _as_dict(metrics.get("data_quality"))
    needs_repair = normalize_admission_bool(data_quality.get("needs_repair"), False)
    freshness_min = _safe_int_or_none(metrics.get("freshness_min"))
    has_live_bar = normalize_admission_bool(metrics.get("has_live_bar"), False)
    if not has_live_bar:
        has_live_bar = _safe_float(metrics.get("latest_intraday_bar_time_ms")) > 0

    exchange = str(
        _first_present(metrics.get("exchange"), _fundamental_value(fundamentals, fundamentals_extra, "exchange")) or ""
    ).strip().upper()
    sector = str(
        _first_present(metrics.get("sector"), _fundamental_value(fundamentals, fundamentals_extra, "sector")) or ""
    ).strip()
    industry = str(
        _first_present(metrics.get("industry"), _fundamental_value(fundamentals, fundamentals_extra, "industry")) or ""
    ).strip()
    country = str(
        _first_present(metrics.get("country"), _fundamental_value(fundamentals, fundamentals_extra, "country")) or ""
    ).strip().upper()
    beta = _safe_float(
        _first_present(metrics.get("beta"), _fundamental_value(fundamentals, fundamentals_extra, "beta"))
    )

    return {
        "symbol": symbol,
        "exchange": exchange,
        "sector": sector,
        "industry": industry,
        "country": country,
        "beta": round(beta, 4) if beta > 0 else 0.0,
        "price": round(price, 4) if price > 0 else 0.0,
        "price_band": _price_band(price),
        "avg_10d_volume": round(avg_10d_volume, 2),
        "premarket_volume": round(premarket_volume, 2),
        "today_volume": round(today_volume, 2),
        "dollar_volume": round(dollar_volume, 2),
        "avg_dollar_volume": round(avg_dollar_volume, 2),
        "liquidity_tier": _liquidity_tier(avg_10d_volume, avg_dollar_volume),
        "activity_profile": _activity_profile(
            premarket_volume=premarket_volume,
            today_volume=today_volume,
            rvol_20=rvol_20,
            avg_10d_volume=avg_10d_volume,
        ),
        "atr_pct": round(atr_pct, 4),
        "day_change_pct": round(day_change_pct, 4),
        "abs_day_change_pct": round(abs_day_change_pct, 4),
        "rvol_20": round(rvol_20, 4),
        "volatility_profile": _volatility_profile(atr_pct, abs_day_change_pct),
        "market_cap": round(market_cap, 2) if market_cap > 0 else 0.0,
        "market_cap_tier": _market_cap_tier(market_cap),
        "shares_float": round(shares_float, 2) if shares_float > 0 else 0.0,
        "shares_outstanding": round(shares_outstanding, 2) if shares_outstanding > 0 else 0.0,
        "float_profile": _float_profile(shares_float),
        "short_float_pct": round(short_float_pct, 4) if short_float_pct > 0 else 0.0,
        "short_interest_profile": _short_interest_profile(short_float_pct),
        "tradability_score": round(_safe_float(metrics.get("tradability_score")), 3),
        "freshness_min": freshness_min,
        "has_live_bar": has_live_bar,
        "data_quality": {
            "status": str(data_quality.get("status") or ("repairing" if needs_repair else "unknown")).strip().lower(),
            "needs_repair": needs_repair,
        },
    }


def _build_dynamic_thresholds(profile: dict, settings: dict) -> dict:
    base_avg = max(
        0.0,
        _safe_float(settings.get("min_avg_10d_volume"), DEFAULT_MIN_AVG_10D_VOLUME),
    )
    base_pre = max(
        0.0,
        _safe_float(settings.get("min_premarket_volume"), DEFAULT_MIN_PREMARKET_VOLUME),
    )
    base_atr = max(0.0, _safe_float(settings.get("min_atr_pct"), DEFAULT_MIN_ATR_PCT))
    base_day = max(
        0.0,
        _safe_float(settings.get("min_abs_day_change_pct"), DEFAULT_MIN_ABS_DAY_CHANGE_PCT),
    )

    price_band = str(profile.get("price_band") or "")
    liquidity_tier = str(profile.get("liquidity_tier") or "")
    activity_profile = str(profile.get("activity_profile") or "")
    float_profile = str(profile.get("float_profile") or "")
    market_cap_tier = str(profile.get("market_cap_tier") or "")
    avg_dollar_volume = _safe_float(profile.get("avg_dollar_volume"))
    atr_pct = _safe_float(profile.get("atr_pct"))
    abs_day_change_pct = _safe_float(profile.get("abs_day_change_pct"))
    rvol_20 = _safe_float(profile.get("rvol_20"))

    threshold_profile, profile_reasons = _select_threshold_profile(profile)
    avg_multiplier = 1.0
    pre_multiplier_profile = 1.0
    min_score_adjustment = 0.0
    dollar_threshold_profile = 2_000_000.0

    if threshold_profile == "mega_core":
        avg_multiplier *= 0.55
        pre_multiplier_profile *= 0.95
        dollar_threshold_profile = 10_000_000.0
        min_score_adjustment -= 2.0
    elif threshold_profile == "large_liquid":
        avg_multiplier *= 0.65
        dollar_threshold_profile = 8_000_000.0
        min_score_adjustment -= 1.0
    elif threshold_profile == "high_price_liquid":
        avg_multiplier *= 0.60
        dollar_threshold_profile = 5_000_000.0
    elif threshold_profile == "mid_active":
        avg_multiplier *= 0.85
        pre_multiplier_profile *= 0.85
        dollar_threshold_profile = 4_000_000.0
    elif threshold_profile == "low_float_hot":
        avg_multiplier *= 0.60
        pre_multiplier_profile *= 0.65
        dollar_threshold_profile = 1_500_000.0
        min_score_adjustment += 4.0
    elif threshold_profile == "thin_or_penny":
        avg_multiplier *= 1.45
        pre_multiplier_profile *= 1.25
        dollar_threshold_profile = 1_000_000.0 if price_band in {"penny", "low"} else 2_500_000.0
        min_score_adjustment += 6.0

    if price_band in {"high", "extended"}:
        avg_multiplier *= 0.75
    if avg_dollar_volume >= 25_000_000:
        avg_multiplier *= 0.70
    elif avg_dollar_volume >= 8_000_000:
        avg_multiplier *= 0.85
    if float_profile == "low_float" and activity_profile in {"in_play", "hot"}:
        avg_multiplier *= 0.75
    if market_cap_tier in {"mega", "large"} and price_band not in {"high", "extended"}:
        avg_multiplier *= 1.10
    if price_band == "penny":
        avg_multiplier *= 1.60
    avg_threshold = _bounded(base_avg * avg_multiplier, base_avg * 0.35, base_avg * 1.75)

    avg_dollar_threshold = 0.0
    if base_avg > 0:
        if price_band in {"penny", "low"}:
            avg_dollar_threshold = min(dollar_threshold_profile, 750_000.0)
        elif price_band in {"regular", "extended", "high"}:
            avg_dollar_threshold = dollar_threshold_profile

    activity_multiplier = pre_multiplier_profile
    if rvol_20 >= 3.0 or abs_day_change_pct >= base_day * 2.0:
        activity_multiplier *= 0.55
    elif rvol_20 >= 1.5 or abs_day_change_pct >= base_day * 1.25:
        activity_multiplier *= 0.75
    if liquidity_tier in {"institutional", "liquid"} and rvol_20 < 1.2:
        activity_multiplier *= 1.10
    if float_profile == "low_float":
        activity_multiplier *= 0.85
    premarket_threshold = _bounded(base_pre * activity_multiplier, base_pre * 0.30, base_pre * 1.50)
    today_threshold = max(premarket_threshold * 3.0, avg_threshold * 0.03)
    rvol_threshold = 1.10
    if liquidity_tier in {"thin", "standard"}:
        rvol_threshold = 1.20
    if float_profile == "low_float":
        rvol_threshold = 1.00
    if threshold_profile == "thin_or_penny":
        rvol_threshold = max(rvol_threshold, 1.35)
    elif threshold_profile in {"mega_core", "large_liquid"} and activity_profile in {"warming", "cold"}:
        rvol_threshold = max(1.05, rvol_threshold - 0.05)

    atr_multiplier = 1.0
    day_multiplier = 1.0
    if activity_profile == "hot":
        atr_multiplier *= 0.80
        day_multiplier *= 0.80
    elif activity_profile == "in_play":
        atr_multiplier *= 0.90
        day_multiplier *= 0.90
    if str(profile.get("volatility_profile") or "") == "extreme":
        day_multiplier *= 1.10
    if price_band == "penny":
        atr_multiplier *= 1.20
        day_multiplier *= 1.20
    if market_cap_tier in {"mega", "large"} and atr_pct > 0:
        atr_multiplier *= 0.90
    if threshold_profile == "low_float_hot":
        atr_multiplier *= 1.05
        day_multiplier *= 1.10
    elif threshold_profile == "thin_or_penny":
        atr_multiplier *= 1.20
        day_multiplier *= 1.25
    atr_threshold = _bounded(base_atr * atr_multiplier, base_atr * 0.60, base_atr * 1.40)
    day_threshold = _bounded(base_day * day_multiplier, base_day * 0.60, base_day * 1.40)

    min_score = max(
        0.0,
        min(
            ADMISSION_SCORE_CAP,
            _safe_float(settings.get("dynamic_admission_min_score"), DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE),
        ),
    )
    min_score = max(0.0, min(ADMISSION_SCORE_CAP, min_score + min_score_adjustment))

    return {
        "mode": "dynamic",
        "threshold_profile": threshold_profile,
        "threshold_profile_reasons": profile_reasons,
        "market_cap_tier": market_cap_tier,
        "liquidity_tier": liquidity_tier,
        "float_profile": float_profile,
        "activity_profile": activity_profile,
        "avg_10d_volume_gte": round(avg_threshold, 2),
        "avg_dollar_volume_gte": round(avg_dollar_threshold, 2),
        "activity_any_of": {
            "premarket_volume_gte": round(premarket_threshold, 2),
            "today_volume_gte": round(today_threshold, 2),
            "rvol_20_gte": round(rvol_threshold, 4),
        },
        "volatility_any_of": {
            "atr_pct_gte": round(atr_threshold, 4),
            "abs_day_change_pct_gte": round(day_threshold, 4),
        },
        "admission_score_gte": round(min_score, 3),
        "blocking_floors": {
            "avg_10d_volume_gte": round(max(10_000.0, base_avg * 0.20), 2),
            "avg_dollar_volume_gte": round(max(500_000.0, avg_dollar_threshold * 0.20), 2)
            if avg_dollar_threshold > 0
            else 0.0,
            "activity_volume_gte": round(max(500.0, base_pre * 0.10), 2),
            "price_gt": 0.0,
        },
        "base_thresholds": {
            "avg_10d_volume_gte": round(base_avg, 2),
            "premarket_volume_gte": round(base_pre, 2),
            "atr_pct_gte": round(base_atr, 4),
            "abs_day_change_pct_gte": round(base_day, 4),
        },
    }


def _select_threshold_profile(profile: dict) -> tuple[str, list[str]]:
    price_band = str(profile.get("price_band") or "")
    liquidity_tier = str(profile.get("liquidity_tier") or "")
    activity_profile = str(profile.get("activity_profile") or "")
    float_profile = str(profile.get("float_profile") or "")
    market_cap_tier = str(profile.get("market_cap_tier") or "")
    avg_dollar_volume = _safe_float(profile.get("avg_dollar_volume"))
    avg_volume = _safe_float(profile.get("avg_10d_volume"))
    beta = _safe_float(profile.get("beta"))
    rvol_20 = _safe_float(profile.get("rvol_20"))
    reasons = [
        f"cap={market_cap_tier or 'unknown'}",
        f"liq={liquidity_tier or 'unknown'}",
        f"activity={activity_profile or 'unknown'}",
        f"float={float_profile or 'unknown'}",
    ]

    if price_band == "penny" or (
        liquidity_tier == "thin" and activity_profile in {"cold", "warming"} and avg_volume < 100_000
    ):
        reasons.append("thin/penny names need stricter activity and volatility confirmation")
        return "thin_or_penny", reasons
    if float_profile == "low_float" and activity_profile in {"in_play", "hot"}:
        reasons.append("low float is tradable only when current activity is present")
        return "low_float_hot", reasons
    if market_cap_tier == "mega" and (
        liquidity_tier in {"institutional", "liquid"} or avg_dollar_volume >= 8_000_000
    ):
        reasons.append("mega-cap liquidity can qualify by dollar volume instead of raw shares")
        return "mega_core", reasons
    if market_cap_tier in {"mega", "large"} and (
        avg_dollar_volume >= 25_000_000 or liquidity_tier in {"institutional", "liquid"}
    ):
        reasons.append("large liquid name: lower raw-share hurdle but require strong dollar volume")
        return "large_liquid", reasons
    if price_band in {"high", "extended"} and avg_dollar_volume >= 8_000_000:
        reasons.append("high-price stock: raw volume is normalized by dollar volume")
        return "high_price_liquid", reasons
    if market_cap_tier in {"large", "mid", "small"} and (
        activity_profile in {"in_play", "hot"} or rvol_20 >= 1.5 or beta >= 1.3
    ):
        reasons.append("active growth stock: accept dynamic activity/catalyst evidence")
        return "mid_active", reasons
    return "default_dynamic", reasons


def _build_failed_gates(symbol: str, profile: dict, thresholds: dict) -> list[dict]:
    failed_gates: list[dict] = []

    avg_volume = _safe_float(profile.get("avg_10d_volume"))
    avg_dollar_volume = _safe_float(profile.get("avg_dollar_volume"))
    avg_threshold = _safe_float(thresholds.get("avg_10d_volume_gte"))
    avg_dollar_threshold = _safe_float(thresholds.get("avg_dollar_volume_gte"))
    floors = thresholds.get("blocking_floors") or {}
    floor_avg = _safe_float(floors.get("avg_10d_volume_gte"))
    floor_dollar = _safe_float(floors.get("avg_dollar_volume_gte"))
    liquidity_passed = avg_volume >= avg_threshold
    if avg_dollar_threshold > 0:
        liquidity_passed = liquidity_passed or avg_dollar_volume >= avg_dollar_threshold
    if not liquidity_passed:
        hard_liquidity_miss = avg_volume < floor_avg and (
            avg_dollar_threshold <= 0 or avg_dollar_volume < floor_dollar
        )
        failed_gates.append(
            _failed_gate(
                symbol=symbol,
                bucket=REJECTION_BUCKET_AVG_10D,
                metric="avg_10d_volume|avg_dollar_volume",
                actual=f"avg={_format_number(avg_volume)}, dollar={_format_number(avg_dollar_volume)}",
                threshold=(
                    f"avg>={_format_number(avg_threshold)}"
                    + (f" or dollar>={_format_number(avg_dollar_threshold)}" if avg_dollar_threshold > 0 else "")
                ),
                note="Liquidity is below the dynamic threshold.",
                severity="blocking" if hard_liquidity_miss else "soft",
            )
        )

    activity_thresholds = thresholds.get("activity_any_of") or {}
    premarket_volume = _safe_float(profile.get("premarket_volume"))
    today_volume = _safe_float(profile.get("today_volume"))
    rvol_20 = _safe_float(profile.get("rvol_20"))
    pre_threshold = _safe_float(activity_thresholds.get("premarket_volume_gte"))
    today_threshold = _safe_float(activity_thresholds.get("today_volume_gte"))
    rvol_threshold = _safe_float(activity_thresholds.get("rvol_20_gte"))
    activity_passed = (
        premarket_volume >= pre_threshold
        or today_volume >= today_threshold
        or (rvol_threshold > 0 and rvol_20 >= rvol_threshold)
    )
    floor_activity = _safe_float((thresholds.get("blocking_floors") or {}).get("activity_volume_gte"))
    if not activity_passed:
        failed_gates.append(
            _failed_gate(
                symbol=symbol,
                bucket=REJECTION_BUCKET_PREMARKET,
                metric="premarket_volume|today_volume|rvol_20",
                actual=(
                    f"pre={_format_number(premarket_volume)}, today={_format_number(today_volume)}, "
                    f"rvol={_format_number(rvol_20)}"
                ),
                threshold=(
                    f"pre>={_format_number(pre_threshold)} or today>={_format_number(today_threshold)} "
                    f"or rvol>={_format_number(rvol_threshold)}"
                ),
                note="Intraday activity is below the dynamic threshold.",
                severity=(
                    "blocking"
                    if max(premarket_volume, today_volume) < floor_activity and rvol_20 <= 0
                    else "soft"
                ),
            )
        )

    volatility_thresholds = thresholds.get("volatility_any_of") or {}
    atr_pct = _safe_float(profile.get("atr_pct"))
    abs_day_change_pct = _safe_float(profile.get("abs_day_change_pct"))
    atr_threshold = _safe_float(volatility_thresholds.get("atr_pct_gte"))
    day_threshold = _safe_float(volatility_thresholds.get("abs_day_change_pct_gte"))
    volatility_passed = atr_pct >= atr_threshold or abs_day_change_pct >= day_threshold
    if not volatility_passed:
        severity = "blocking" if atr_pct <= 0 and abs_day_change_pct <= 0 else "soft"
        failed_gates.append(
            _failed_gate(
                symbol=symbol,
                bucket=REJECTION_BUCKET_ATR,
                metric="atr_pct",
                actual=_format_number(atr_pct),
                threshold=_format_number(atr_threshold),
                note="ATR is below the dynamic volatility threshold.",
                severity=severity,
            )
        )
        failed_gates.append(
            _failed_gate(
                symbol=symbol,
                bucket=REJECTION_BUCKET_DAY_CHANGE,
                metric="abs_day_change_pct",
                actual=_format_number(abs_day_change_pct),
                threshold=_format_number(day_threshold),
                note="Day change is below the dynamic catalyst threshold.",
                severity=severity,
            )
        )

    price = _safe_float(profile.get("price"))
    if price <= 0:
        failed_gates.append(
            _failed_gate(
                symbol=symbol,
                bucket=REJECTION_BUCKET_PRICE,
                metric="price",
                actual=_format_number(price),
                threshold=">0",
                note="Price is not usable.",
                severity="blocking",
            )
        )

    return failed_gates


def _build_admission_score(profile: dict, thresholds: dict) -> tuple[float, dict]:
    avg_threshold = _safe_float(thresholds.get("avg_10d_volume_gte"))
    avg_dollar_threshold = _safe_float(thresholds.get("avg_dollar_volume_gte"))
    activity_thresholds = thresholds.get("activity_any_of") or {}
    volatility_thresholds = thresholds.get("volatility_any_of") or {}

    liquidity_score = max(
        _score_ratio(_safe_float(profile.get("avg_10d_volume")), avg_threshold, 24.0),
        _score_ratio(_safe_float(profile.get("avg_dollar_volume")), avg_dollar_threshold, 24.0)
        if avg_dollar_threshold > 0
        else 0.0,
    )
    activity_score = max(
        _score_ratio(
            _safe_float(profile.get("premarket_volume")),
            _safe_float(activity_thresholds.get("premarket_volume_gte")),
            22.0,
        ),
        _score_ratio(
            _safe_float(profile.get("today_volume")),
            _safe_float(activity_thresholds.get("today_volume_gte")),
            22.0,
        ),
        _score_ratio(
            _safe_float(profile.get("rvol_20")),
            _safe_float(activity_thresholds.get("rvol_20_gte")),
            22.0,
        ),
    )
    volatility_score = max(
        _score_ratio(
            _safe_float(profile.get("atr_pct")),
            _safe_float(volatility_thresholds.get("atr_pct_gte")),
            18.0,
        ),
        _score_ratio(
            _safe_float(profile.get("abs_day_change_pct")),
            _safe_float(volatility_thresholds.get("abs_day_change_pct_gte")),
            18.0,
        ),
    )
    price_score = _price_score(_safe_float(profile.get("price")))
    fundamentals_score = _fundamentals_score(profile)
    tradability_score = min(10.0, max(0.0, _safe_float(profile.get("tradability_score")) / 10.0))
    freshness_score = _freshness_score(profile)
    data_penalty = 4.0 if (profile.get("data_quality") or {}).get("needs_repair") else 0.0

    total = (
        liquidity_score
        + activity_score
        + volatility_score
        + price_score
        + fundamentals_score
        + tradability_score
        + freshness_score
        - data_penalty
    )
    total = max(0.0, min(ADMISSION_SCORE_CAP, total))
    components = {
        "liquidity": round(liquidity_score, 3),
        "activity": round(activity_score, 3),
        "volatility": round(volatility_score, 3),
        "price": round(price_score, 3),
        "fundamentals": round(fundamentals_score, 3),
        "tradability": round(tradability_score, 3),
        "freshness": round(freshness_score, 3),
        "data_penalty": round(data_penalty, 3),
    }
    return total, components


def _build_strategy_policy(
    profile: dict,
    *,
    admission_score: float,
    min_score: float,
    direction_bias: str | None,
    blocking_failures: list[dict],
) -> dict:
    direction = str(direction_bias or "").strip().lower()
    allowed_sides = [direction] if direction in {"long", "short"} else ["long", "short"]
    activity_profile = str(profile.get("activity_profile") or "")
    volatility_profile = str(profile.get("volatility_profile") or "")
    liquidity_tier = str(profile.get("liquidity_tier") or "")
    float_profile = str(profile.get("float_profile") or "")

    if blocking_failures or admission_score < min_score:
        risk_profile = "avoid"
        setup_type = "reject"
        confirmation = "blocked"
    elif volatility_profile == "extreme" or liquidity_tier == "thin" or float_profile == "low_float":
        risk_profile = "aggressive"
        setup_type = "momentum_breakout" if activity_profile in {"in_play", "hot"} else "watch_only"
        confirmation = "strict"
    elif activity_profile in {"in_play", "hot"}:
        risk_profile = "balanced"
        setup_type = "stocks_in_play"
        confirmation = "standard"
    else:
        risk_profile = "conservative"
        setup_type = "liquidity_qualified"
        confirmation = "strict"

    size_multiplier = 1.0
    if liquidity_tier == "institutional":
        size_multiplier += 0.20
    elif liquidity_tier == "liquid":
        size_multiplier += 0.10
    elif liquidity_tier == "thin":
        size_multiplier -= 0.35
    if volatility_profile == "extreme":
        size_multiplier -= 0.25
    elif volatility_profile == "high":
        size_multiplier -= 0.10
    if float_profile == "low_float":
        size_multiplier -= 0.25
    if risk_profile == "avoid":
        size_multiplier = 0.0

    exit_policy = _policy_exit_profile(
        risk_profile=risk_profile,
        setup_type=setup_type,
        volatility_profile=volatility_profile,
        liquidity_tier=liquidity_tier,
        float_profile=float_profile,
    )

    return {
        "risk_profile": risk_profile,
        "setup_type": setup_type,
        "allowed_sides": allowed_sides,
        "signal_confirmation": confirmation,
        "entry_style": "wait_for_confirmation" if confirmation in {"strict", "blocked"} else "momentum_or_pullback",
        "position_size_multiplier": round(max(0.0, min(1.25, size_multiplier)), 3),
        "recommended_signal_profile": "intraday_sd_v1",
        "recommended_exit_policy": exit_policy,
        "avoid_new_entries": risk_profile == "avoid",
    }


def _policy_exit_profile(
    *,
    risk_profile: str,
    setup_type: str,
    volatility_profile: str,
    liquidity_tier: str,
    float_profile: str,
) -> dict:
    if risk_profile == "avoid":
        return {
            "exit_policy_profile": "fixed_atr_rr",
            "policy_type": "reject",
            "sl_atr_mult": 0.0,
            "tp_rr": 0.0,
        }
    if setup_type == "momentum_breakout":
        return {
            "exit_policy_profile": "signal_mode_adaptive_v1",
            "policy_type": "breakout",
            "sl_atr_mult": 1.8 if volatility_profile != "extreme" else 1.6,
            "tp_rr": 2.5,
        }
    if setup_type == "stocks_in_play":
        return {
            "exit_policy_profile": "signal_mode_adaptive_v1",
            "policy_type": "trend_pullback",
            "sl_atr_mult": 2.0,
            "tp_rr": 2.0,
        }
    if liquidity_tier == "thin" or float_profile == "low_float":
        return {
            "exit_policy_profile": "fixed_atr_rr",
            "policy_type": "strict_liquidity",
            "sl_atr_mult": 1.6,
            "tp_rr": 1.6,
        }
    return {
        "exit_policy_profile": "fixed_atr_rr",
        "policy_type": "liquidity_qualified",
        "sl_atr_mult": 2.0,
        "tp_rr": 1.8,
    }


def _build_reason_tags(
    profile: dict,
    thresholds: dict,
    admission_score: float,
    min_score: float,
    strategy_policy: dict,
) -> list[str]:
    return [
        f"admission={_format_number(admission_score)}>={_format_number(min_score)}",
        f"threshold_profile={thresholds.get('threshold_profile')}",
        f"profile={profile.get('liquidity_tier')}/{profile.get('activity_profile')}/{profile.get('volatility_profile')}",
        f"policy={strategy_policy.get('setup_type')}",
        f"dyn_avg>={_format_number(thresholds.get('avg_10d_volume_gte'))}",
        f"dyn_pre>={_format_number((thresholds.get('activity_any_of') or {}).get('premarket_volume_gte'))}",
    ]


def _failed_gate(
    *,
    symbol: str,
    bucket: str,
    metric: str,
    actual: str,
    threshold: str,
    note: str,
    severity: str,
) -> dict:
    return {
        "bucket": bucket,
        "symbol": symbol,
        "metric": metric,
        "actual": str(actual or ""),
        "threshold": str(threshold or ""),
        "note": note,
        "severity": severity,
    }


def _score_ratio(value: float, threshold: float, points: float) -> float:
    if points <= 0:
        return 0.0
    if threshold <= 0:
        return points if value > 0 else points * 0.40
    ratio = max(0.0, value / threshold)
    return min(points, points * min(1.0, ratio))


def _price_score(price: float) -> float:
    if price <= 0:
        return 4.0
    if 2.0 <= price <= 80.0:
        return 8.0
    if 1.0 <= price <= 150.0:
        return 6.0
    if price < 1.0:
        return 1.0
    return 4.0


def _fundamentals_score(profile: dict) -> float:
    score = 2.0
    market_cap_tier = str(profile.get("market_cap_tier") or "")
    float_profile = str(profile.get("float_profile") or "")
    short_interest_profile = str(profile.get("short_interest_profile") or "")
    if market_cap_tier in {"mega", "large"}:
        score += 3.0
    elif market_cap_tier in {"mid", "small"}:
        score += 2.0
    elif market_cap_tier == "micro":
        score += 0.5
    if float_profile in {"normal_float", "large_float"}:
        score += 2.0
    elif float_profile == "low_float":
        score += 0.5
    if short_interest_profile in {"moderate", "high"}:
        score += 1.0
    return min(8.0, score)


def _freshness_score(profile: dict) -> float:
    data_quality = profile.get("data_quality") or {}
    if data_quality.get("needs_repair"):
        return 0.0
    freshness_min = profile.get("freshness_min")
    if not isinstance(freshness_min, int):
        return 2.0
    if freshness_min <= 20:
        return 6.0
    if freshness_min <= 60:
        return 4.0
    if freshness_min <= 180:
        return 2.0
    return 0.0


def _price_band(price: float) -> str:
    if price <= 0:
        return "unknown"
    if price < 1:
        return "penny"
    if price < 5:
        return "low"
    if price <= 80:
        return "regular"
    if price <= 150:
        return "extended"
    return "high"


def _liquidity_tier(avg_10d_volume: float, avg_dollar_volume: float) -> str:
    if avg_10d_volume >= 5_000_000 or avg_dollar_volume >= 100_000_000:
        return "institutional"
    if avg_10d_volume >= 1_000_000 or avg_dollar_volume >= 25_000_000:
        return "liquid"
    if avg_10d_volume >= 150_000 or avg_dollar_volume >= 3_000_000:
        return "standard"
    return "thin"


def _activity_profile(
    *,
    premarket_volume: float,
    today_volume: float,
    rvol_20: float,
    avg_10d_volume: float,
) -> str:
    premarket_ratio = premarket_volume / avg_10d_volume if avg_10d_volume > 0 else 0.0
    today_ratio = today_volume / avg_10d_volume if avg_10d_volume > 0 else 0.0
    if premarket_volume >= 100_000 or today_volume >= 300_000 or rvol_20 >= 3.0 or premarket_ratio >= 0.20:
        return "hot"
    if premarket_volume >= 25_000 or today_volume >= 100_000 or rvol_20 >= 1.5 or premarket_ratio >= 0.08:
        return "in_play"
    if premarket_volume >= 5_000 or today_volume > 0 or rvol_20 >= 0.8 or today_ratio >= 0.02:
        return "warming"
    return "cold"


def _volatility_profile(atr_pct: float, abs_day_change_pct: float) -> str:
    marker = max(atr_pct, abs_day_change_pct)
    if marker >= 8.0:
        return "extreme"
    if marker >= 3.0:
        return "high"
    if marker >= 1.0:
        return "normal"
    return "low"


def _market_cap_tier(market_cap: float) -> str:
    if market_cap <= 0:
        return "unknown"
    if market_cap >= 200_000_000_000:
        return "mega"
    if market_cap >= 10_000_000_000:
        return "large"
    if market_cap >= 2_000_000_000:
        return "mid"
    if market_cap >= 300_000_000:
        return "small"
    return "micro"


def _float_profile(shares_float: float) -> str:
    if shares_float <= 0:
        return "unknown"
    if shares_float < 20_000_000:
        return "low_float"
    if shares_float > 500_000_000:
        return "large_float"
    return "normal_float"


def _short_interest_profile(short_float_pct: float) -> str:
    if short_float_pct <= 0:
        return "unknown"
    if short_float_pct >= 30.0:
        return "crowded"
    if short_float_pct >= 15.0:
        return "high"
    if short_float_pct >= 5.0:
        return "moderate"
    return "normal"


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _fundamental_value(row: dict, extra: dict, *keys: str) -> Any:
    return _first_present(*(row.get(key) for key in keys), *(extra.get(key) for key in keys))


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if isfinite(number) else default
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return default
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b", "t"}:
        text = text[:-1].strip()
        multiplier = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0, "t": 1_000_000_000_000.0}[suffix]
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        number = float(text) * multiplier
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bounded(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        lower, upper = upper, lower
    return max(lower, min(upper, value))


def _format_number(value: Any) -> str:
    number = _safe_float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_DYNAMIC_ADMISSION_MIN_SCORE",
    "evaluate_dynamic_admission",
    "normalize_admission_bool",
]
