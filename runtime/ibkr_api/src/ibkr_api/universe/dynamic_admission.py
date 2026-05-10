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
        return {
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
        }

    market_profile = classify_market_data_profile(latest_row)
    return {
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
    }


def build_dynamic_thresholds(profile: dict[str, Any] | None) -> dict[str, float]:
    profile_name = to_text((profile or {}).get("name")).lower() or "unknown"
    thresholds = dict(BASE_ADMISSION_THRESHOLDS)
    thresholds.update(PROFILE_ADMISSION_THRESHOLDS.get(profile_name, {}))
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
    checks = (
        (
            "avg_10d_volume",
            "avg_10d_volume",
            float(thresholds.get("avg_10d_volume_gte") or BASE_ADMISSION_THRESHOLDS["avg_10d_volume_gte"]),
            "Average 10-day volume is below the dynamic admission threshold.",
        ),
        (
            "premarket_volume",
            "premarket_volume",
            float(thresholds.get("premarket_volume_gte") or BASE_ADMISSION_THRESHOLDS["premarket_volume_gte"]),
            "Premarket volume is below the dynamic admission threshold.",
        ),
        (
            "atr_pct",
            "atr_pct",
            float(thresholds.get("atr_pct_gte") or BASE_ADMISSION_THRESHOLDS["atr_pct_gte"]),
            "ATR percent is below the dynamic admission threshold.",
        ),
        (
            "abs_day_change_pct",
            "day_change_pct",
            float(thresholds.get("abs_day_change_pct_gte") or BASE_ADMISSION_THRESHOLDS["abs_day_change_pct_gte"]),
            "Absolute day-change percent is below the dynamic admission threshold.",
        ),
    )
    failures: list[dict[str, Any]] = []
    for gate, metric, threshold, note in checks:
        actual = metric_value(row, metric)
        if actual < threshold:
            failures.append(
                build_failed_gate(
                    gate=gate,
                    metric=metric,
                    actual=actual,
                    threshold=threshold,
                    note=note,
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
