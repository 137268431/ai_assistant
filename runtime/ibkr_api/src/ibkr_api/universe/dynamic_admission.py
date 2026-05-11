from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import to_float, to_text


BASE_ADMISSION_THRESHOLDS: dict[str, float] = {
    "avg_10d_volume_gte": 100_000,
    "premarket_volume_gte": 5_000,
    "atr_pct_gte": 0.15,
    "abs_day_change_pct_gte": 1.0,
}

PROFILE_ADMISSION_THRESHOLDS: dict[str, dict[str, float]] = {
    "mega_cap": {
        "avg_10d_volume_gte": 500_000,
        "premarket_volume_gte": 10_000,
        "atr_pct_gte": 0.20,
        "abs_day_change_pct_gte": 0.60,
    },
    "large_cap": {
        "avg_10d_volume_gte": 500_000,
        "premarket_volume_gte": 10_000,
        "atr_pct_gte": 0.25,
        "abs_day_change_pct_gte": 0.75,
    },
    "mid_cap": {
        "avg_10d_volume_gte": 750_000,
        "premarket_volume_gte": 20_000,
        "atr_pct_gte": 0.50,
        "abs_day_change_pct_gte": 1.00,
    },
    "small_cap": {
        "avg_10d_volume_gte": 1_000_000,
        "premarket_volume_gte": 50_000,
        "atr_pct_gte": 1.00,
        "abs_day_change_pct_gte": 1.50,
    },
    "micro_cap": {
        "avg_10d_volume_gte": 2_000_000,
        "premarket_volume_gte": 100_000,
        "atr_pct_gte": 1.50,
        "abs_day_change_pct_gte": 2.00,
    },
    "high_liquidity": {
        "avg_10d_volume_gte": 500_000,
        "premarket_volume_gte": 10_000,
        "atr_pct_gte": 0.20,
        "abs_day_change_pct_gte": 0.80,
    },
    "standard_liquidity": {
        "avg_10d_volume_gte": 250_000,
        "premarket_volume_gte": 7_500,
        "atr_pct_gte": 0.30,
        "abs_day_change_pct_gte": 1.00,
    },
    "thin_liquidity": BASE_ADMISSION_THRESHOLDS,
    "unknown": BASE_ADMISSION_THRESHOLDS,
}

FUNDAMENTAL_PROFILE_ORDER = (
    (200_000_000_000, "mega_cap"),
    (10_000_000_000, "large_cap"),
    (2_000_000_000, "mid_cap"),
    (300_000_000, "small_cap"),
    (0, "micro_cap"),
)


def safe_float(value: Any, default: float = 0.0) -> float:
    parsed = to_float(value)
    if parsed is None:
        return float(default)
    return float(parsed)


def normalize_market_cap_usd(value: Any, *, market_cap_millions: Any = None) -> float:
    direct = safe_float(value)
    if direct > 0:
        return direct
    millions = safe_float(market_cap_millions)
    if millions > 0:
        return millions * 1_000_000.0
    return 0.0


def classify_fundamental_profile(market_cap_usd: Any) -> str:
    market_cap = safe_float(market_cap_usd)
    if market_cap <= 0:
        return "unknown"
    for minimum, profile_name in FUNDAMENTAL_PROFILE_ORDER:
        if market_cap >= minimum:
            return profile_name
    return "unknown"


def classify_market_data_profile(row: dict[str, Any] | None) -> str:
    source = row or {}
    avg_volume = safe_float(source.get("avg_10d_volume"))
    price = safe_float(source.get("price"))
    premarket_volume = safe_float(source.get("premarket_volume"))
    if avg_volume >= 5_000_000 and price >= 2:
        return "high_liquidity"
    if avg_volume >= 1_000_000 or premarket_volume >= 100_000:
        return "standard_liquidity"
    if avg_volume > 0 or premarket_volume > 0:
        return "thin_liquidity"
    return "unknown"


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


def _liquidity_tier(avg_volume: float, avg_dollar_volume: float) -> str:
    if avg_volume >= 5_000_000 or avg_dollar_volume >= 100_000_000:
        return "institutional"
    if avg_volume >= 1_000_000 or avg_dollar_volume >= 25_000_000:
        return "liquid"
    if avg_volume >= 150_000 or avg_dollar_volume >= 3_000_000:
        return "standard"
    return "thin"


def _activity_profile(row: dict[str, Any] | None, avg_volume: float) -> str:
    source = row or {}
    premarket_volume = safe_float(source.get("premarket_volume"))
    today_volume = max(safe_float(source.get("today_volume")), safe_float(source.get("volume")))
    rvol_20 = max(
        safe_float(source.get("rvol_20")),
        safe_float(source.get("relative_volume")),
        safe_float(source.get("rvol")),
    )
    premarket_ratio = premarket_volume / avg_volume if avg_volume > 0 else 0.0
    today_ratio = today_volume / avg_volume if avg_volume > 0 else 0.0
    if premarket_volume >= 100_000 or today_volume >= 300_000 or rvol_20 >= 3.0 or premarket_ratio >= 0.20:
        return "hot"
    if premarket_volume >= 25_000 or today_volume >= 100_000 or rvol_20 >= 1.5 or premarket_ratio >= 0.08:
        return "in_play"
    if premarket_volume >= 5_000 or today_volume > 0 or rvol_20 >= 0.8 or today_ratio >= 0.02:
        return "warming"
    return "cold"


def _float_profile(shares_float: float) -> str:
    if shares_float <= 0:
        return "unknown"
    if shares_float < 20_000_000:
        return "low_float"
    if shares_float > 500_000_000:
        return "large_float"
    return "normal_float"


def _bounded(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        lower, upper = upper, lower
    return max(lower, min(upper, value))


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _fundamental_value(row: dict[str, Any], extra: dict[str, Any], *keys: str) -> Any:
    return _first_present(*(row.get(key) for key in keys), *(extra.get(key) for key in keys))


def _positive_or_none(value: Any) -> float | None:
    parsed = safe_float(value)
    return round(parsed, 4) if parsed > 0 else None


def _with_market_metrics(profile: dict[str, Any], latest_row: dict[str, Any] | None) -> dict[str, Any]:
    row = latest_row or {}
    price = safe_float(row.get("price"))
    avg_volume = safe_float(row.get("avg_10d_volume"))
    avg_dollar_volume = price * avg_volume if price > 0 and avg_volume > 0 else 0.0
    rvol_20 = max(safe_float(row.get("rvol_20")), safe_float(row.get("relative_volume")), safe_float(row.get("rvol")))
    if rvol_20 <= 0 and avg_volume > 0:
        today_volume = max(safe_float(row.get("today_volume")), safe_float(row.get("volume")))
        if today_volume > 0:
            rvol_20 = today_volume / avg_volume
    shares_float = safe_float(profile.get("shares_float"))
    enriched = dict(profile)
    enriched.update(
        {
            "price": round(price, 4) if price > 0 else None,
            "price_band": _price_band(price),
            "avg_10d_volume": round(avg_volume, 4) if avg_volume > 0 else None,
            "premarket_volume": safe_float(row.get("premarket_volume")),
            "today_volume": max(safe_float(row.get("today_volume")), safe_float(row.get("volume"))),
            "rvol_20": round(rvol_20, 4) if rvol_20 > 0 else 0.0,
            "avg_dollar_volume": round(avg_dollar_volume, 4) if avg_dollar_volume > 0 else 0.0,
            "liquidity_tier": _liquidity_tier(avg_volume, avg_dollar_volume),
            "activity_profile": _activity_profile(row, avg_volume),
            "float_profile": _float_profile(shares_float),
        }
    )
    threshold_profile, reasons = _select_threshold_profile(enriched)
    enriched["threshold_profile"] = threshold_profile
    enriched["threshold_profile_reasons"] = reasons
    return enriched


def _select_threshold_profile(profile: dict[str, Any]) -> tuple[str, list[str]]:
    profile_name = to_text(profile.get("name")).lower() or "unknown"
    price_band = to_text(profile.get("price_band")).lower()
    liquidity_tier = to_text(profile.get("liquidity_tier")).lower()
    activity_profile = to_text(profile.get("activity_profile")).lower()
    float_profile = to_text(profile.get("float_profile")).lower()
    avg_dollar_volume = safe_float(profile.get("avg_dollar_volume"))
    avg_volume = safe_float(profile.get("avg_10d_volume"))
    beta = safe_float(profile.get("beta"))
    reasons = [
        f"profile={profile_name}",
        f"liq={liquidity_tier or 'unknown'}",
        f"activity={activity_profile or 'unknown'}",
        f"float={float_profile or 'unknown'}",
    ]
    if price_band == "penny" or (
        liquidity_tier == "thin" and activity_profile in {"cold", "warming"} and avg_volume < 100_000
    ):
        reasons.append("thin/penny or inactive symbols keep strict baseline gates")
        return "thin_or_inactive", reasons
    if float_profile == "low_float" and activity_profile in {"in_play", "hot"}:
        reasons.append("low float requires active intraday confirmation")
        return "low_float_hot", reasons
    if profile_name == "mega_cap" and (
        liquidity_tier in {"institutional", "liquid"} or avg_dollar_volume >= 8_000_000
    ):
        reasons.append("mega cap may qualify by dollar volume")
        return "mega_core", reasons
    if profile_name in {"mega_cap", "large_cap"} and (
        avg_dollar_volume >= 25_000_000 or liquidity_tier in {"institutional", "liquid"}
    ):
        reasons.append("large liquid symbol uses dollar-volume-aware gates")
        return "large_liquid", reasons
    if price_band in {"high", "extended"} and avg_dollar_volume >= 8_000_000 and activity_profile in {"in_play", "hot"}:
        reasons.append("high-price active symbol uses dollar-volume-aware gates")
        return "high_price_liquid", reasons
    if profile_name in {"large_cap", "mid_cap", "small_cap"} and (
        activity_profile in {"in_play", "hot"} or beta >= 1.3
    ):
        reasons.append("active growth symbol uses dynamic activity/catalyst gates")
        return "mid_active", reasons
    return "default_dynamic", reasons


def build_admission_profile(
    *,
    symbol: str,
    fundamentals: dict[str, Any] | None = None,
    latest_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fundamental_row = fundamentals or {}
    extra = _as_dict(fundamental_row.get("extra"))
    market_cap_usd = normalize_market_cap_usd(
        _fundamental_value(fundamental_row, extra, "market_cap_usd", "market_cap", "marketCap"),
        market_cap_millions=_fundamental_value(
            fundamental_row,
            extra,
            "market_cap_millions",
            "marketCapitalization",
        ),
    )
    share_outstanding_millions = safe_float(
        _fundamental_value(fundamental_row, extra, "share_outstanding_millions", "shares_outstanding_millions")
    )
    provider = to_text(fundamental_row.get("provider"))
    cached_profile = to_text(_fundamental_value(fundamental_row, extra, "profile")).lower()
    if market_cap_usd > 0 or cached_profile in PROFILE_ADMISSION_THRESHOLDS:
        name = (
            cached_profile
            if cached_profile in PROFILE_ADMISSION_THRESHOLDS
            else classify_fundamental_profile(market_cap_usd)
        )
        return _with_market_metrics({
            "name": name,
            "source": "fundamentals_cache",
            "provider": provider,
            "market_cap_usd": round(market_cap_usd, 2) if market_cap_usd > 0 else None,
            "share_outstanding_millions": (
                round(share_outstanding_millions, 4) if share_outstanding_millions > 0 else None
            ),
            "sector": to_text(_fundamental_value(fundamental_row, extra, "sector")),
            "country": to_text(_fundamental_value(fundamental_row, extra, "country")),
            "shares_float": _positive_or_none(
                _fundamental_value(fundamental_row, extra, "shares_float", "float_shares")
            ),
            "short_float_pct": _positive_or_none(_fundamental_value(fundamental_row, extra, "short_float_pct")),
            "beta": _positive_or_none(_fundamental_value(fundamental_row, extra, "beta")),
            "avg_volume_10d_provider": _positive_or_none(
                _fundamental_value(fundamental_row, extra, "avg_volume_10d_provider", "avg_10d_volume")
            ),
            "reason": "market_cap_profile" if market_cap_usd > 0 else "cached_profile",
        }, latest_row)

    market_profile = classify_market_data_profile(latest_row)
    return _with_market_metrics({
        "name": market_profile,
        "source": "market_data" if market_profile != "unknown" else "fallback",
        "provider": "",
        "market_cap_usd": None,
        "share_outstanding_millions": None,
        "sector": to_text(_fundamental_value(fundamental_row, extra, "sector")),
        "country": to_text(_fundamental_value(fundamental_row, extra, "country")),
        "shares_float": _positive_or_none(
            _fundamental_value(fundamental_row, extra, "shares_float", "float_shares")
        ),
        "short_float_pct": _positive_or_none(_fundamental_value(fundamental_row, extra, "short_float_pct")),
        "beta": _positive_or_none(_fundamental_value(fundamental_row, extra, "beta")),
        "avg_volume_10d_provider": _positive_or_none(
            _fundamental_value(fundamental_row, extra, "avg_volume_10d_provider", "avg_10d_volume")
        ),
        "reason": "liquidity_proxy" if market_profile != "unknown" else "fundamentals_missing",
    }, latest_row)


def build_dynamic_thresholds(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile_name = to_text((profile or {}).get("name")).lower() or "unknown"
    thresholds = dict(BASE_ADMISSION_THRESHOLDS)
    thresholds.update(PROFILE_ADMISSION_THRESHOLDS.get(profile_name, {}))
    base_thresholds = dict(thresholds)
    threshold_profile = to_text((profile or {}).get("threshold_profile")).lower() or "default_dynamic"
    price_band = to_text((profile or {}).get("price_band")).lower()
    activity_profile = to_text((profile or {}).get("activity_profile")).lower()
    rvol_20 = safe_float((profile or {}).get("rvol_20"))
    pre_mult = 1.0
    avg_mult = 1.0
    dollar_threshold = 0.0
    if threshold_profile == "mega_core":
        avg_mult *= 0.55
        dollar_threshold = 10_000_000.0
    elif threshold_profile == "large_liquid":
        avg_mult *= 0.65
        dollar_threshold = 8_000_000.0
    elif threshold_profile == "high_price_liquid":
        avg_mult *= 0.60
        dollar_threshold = 5_000_000.0
    elif threshold_profile == "mid_active":
        avg_mult *= 0.85
        pre_mult *= 0.85
        dollar_threshold = 4_000_000.0
    elif threshold_profile == "low_float_hot":
        avg_mult *= 0.60
        pre_mult *= 0.65
        dollar_threshold = 1_500_000.0
    elif threshold_profile == "thin_or_inactive":
        dollar_threshold = 0.0

    if price_band in {"high", "extended"} and dollar_threshold > 0:
        avg_mult *= 0.75
    avg_threshold = _bounded(
        float(base_thresholds["avg_10d_volume_gte"]) * avg_mult,
        float(base_thresholds["avg_10d_volume_gte"]) * 0.35,
        float(base_thresholds["avg_10d_volume_gte"]) * 1.75,
    )
    pre_threshold = float(base_thresholds["premarket_volume_gte"]) * pre_mult
    if rvol_20 >= 3.0:
        pre_threshold *= 0.55
    elif rvol_20 >= 1.5 or activity_profile in {"in_play", "hot"}:
        pre_threshold *= 0.75
    pre_threshold = _bounded(
        pre_threshold,
        float(base_thresholds["premarket_volume_gte"]) * 0.30,
        float(base_thresholds["premarket_volume_gte"]) * 1.50,
    )
    thresholds.update(
        {
            "threshold_profile": threshold_profile,
            "threshold_profile_reasons": list((profile or {}).get("threshold_profile_reasons") or []),
            "avg_10d_volume_gte": round(avg_threshold, 4),
            "avg_dollar_volume_gte": round(dollar_threshold, 4),
            "premarket_volume_gte": round(pre_threshold, 4),
            "activity_any_of": {
                "premarket_volume_gte": round(pre_threshold, 4),
                "today_volume_gte": round(max(pre_threshold * 3.0, avg_threshold * 0.03), 4),
                "rvol_20_gte": 1.0 if threshold_profile == "low_float_hot" else 1.2,
            },
            "volatility_any_of": {
                "atr_pct_gte": thresholds["atr_pct_gte"],
                "abs_day_change_pct_gte": thresholds["abs_day_change_pct_gte"],
            },
            "base_thresholds": base_thresholds,
        }
    )
    return thresholds


def metric_value(row: dict[str, Any], key: str) -> float:
    value = safe_float((row or {}).get(key))
    if key in {"atr_pct", "day_change_pct"}:
        return abs(value)
    return value


def build_failed_gate(
    *,
    gate: str,
    metric: str,
    actual: float,
    threshold: float,
    note: str,
    market_date: str = "",
    severity: str = "hard",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "gate": gate,
        "bucket": f"{metric}_below_threshold",
        "metric": metric,
        "op": "gte",
        "actual": round(float(actual), 4),
        "threshold": threshold,
        "severity": severity,
        "note": note,
    }
    if market_date:
        item["market_date"] = market_date
    return item


def evaluate_dynamic_admission_gates(
    row: dict[str, Any],
    thresholds: dict[str, Any],
    *,
    market_date: str = "",
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    avg_threshold = float(thresholds.get("avg_10d_volume_gte") or BASE_ADMISSION_THRESHOLDS["avg_10d_volume_gte"])
    avg_dollar_threshold = float(thresholds.get("avg_dollar_volume_gte") or 0)
    avg_volume = metric_value(row, "avg_10d_volume")
    price = metric_value(row, "price")
    avg_dollar_volume = price * avg_volume if price > 0 and avg_volume > 0 else 0.0
    liquidity_passed = avg_volume >= avg_threshold
    if avg_dollar_threshold > 0:
        liquidity_passed = liquidity_passed or avg_dollar_volume >= avg_dollar_threshold
    if not liquidity_passed:
        gate = build_failed_gate(
            gate="avg_10d_volume",
            metric="avg_10d_volume",
            actual=avg_volume,
            threshold=avg_threshold,
            note="Average 10-day volume/dollar-volume is below the dynamic admission threshold.",
            market_date=market_date,
        )
        if avg_dollar_threshold > 0:
            gate["alternate_metric"] = "avg_dollar_volume"
            gate["alternate_actual"] = round(avg_dollar_volume, 4)
            gate["alternate_threshold"] = avg_dollar_threshold
        failures.append(gate)

    activity = thresholds.get("activity_any_of") if isinstance(thresholds.get("activity_any_of"), dict) else {}
    pre_threshold = float(activity.get("premarket_volume_gte") or thresholds.get("premarket_volume_gte") or BASE_ADMISSION_THRESHOLDS["premarket_volume_gte"])
    today_threshold = float(activity.get("today_volume_gte") or 0)
    rvol_threshold = float(activity.get("rvol_20_gte") or 0)
    premarket_volume = metric_value(row, "premarket_volume")
    today_volume = max(metric_value(row, "today_volume"), metric_value(row, "volume"))
    rvol_20 = max(metric_value(row, "rvol_20"), metric_value(row, "relative_volume"), metric_value(row, "rvol"))
    if rvol_20 <= 0 and avg_volume > 0 and today_volume > 0:
        rvol_20 = today_volume / avg_volume
    activity_passed = (
        premarket_volume >= pre_threshold
        or (today_threshold > 0 and today_volume >= today_threshold)
        or (rvol_threshold > 0 and rvol_20 >= rvol_threshold)
    )
    if not activity_passed:
        gate = build_failed_gate(
            gate="premarket_volume",
            metric="premarket_volume",
            actual=premarket_volume,
            threshold=pre_threshold,
            note="Premarket/current activity is below the dynamic admission threshold.",
            market_date=market_date,
        )
        gate["alternate_metrics"] = {
            "today_volume": round(today_volume, 4),
            "today_volume_gte": today_threshold,
            "rvol_20": round(rvol_20, 4),
            "rvol_20_gte": rvol_threshold,
        }
        failures.append(gate)

    volatility = thresholds.get("volatility_any_of") if isinstance(thresholds.get("volatility_any_of"), dict) else {}
    atr_threshold = float(volatility.get("atr_pct_gte") or thresholds.get("atr_pct_gte") or BASE_ADMISSION_THRESHOLDS["atr_pct_gte"])
    day_threshold = float(volatility.get("abs_day_change_pct_gte") or thresholds.get("abs_day_change_pct_gte") or BASE_ADMISSION_THRESHOLDS["abs_day_change_pct_gte"])
    atr_actual = metric_value(row, "atr_pct")
    day_actual = metric_value(row, "day_change_pct")
    if atr_actual < atr_threshold and day_actual < day_threshold:
        failures.append(
            build_failed_gate(
                gate="atr_pct",
                metric="atr_pct",
                actual=atr_actual,
                threshold=atr_threshold,
                note="ATR percent is below the dynamic admission threshold.",
                market_date=market_date,
            )
        )
        failures.append(
            build_failed_gate(
                gate="abs_day_change_pct",
                metric="day_change_pct",
                actual=day_actual,
                threshold=day_threshold,
                note="Absolute day-change percent is below the dynamic admission threshold.",
                market_date=market_date,
            )
        )
    return failures


__all__ = [
    "BASE_ADMISSION_THRESHOLDS",
    "PROFILE_ADMISSION_THRESHOLDS",
    "build_admission_profile",
    "build_dynamic_thresholds",
    "classify_fundamental_profile",
    "evaluate_dynamic_admission_gates",
    "metric_value",
    "normalize_market_cap_usd",
    "safe_float",
]
