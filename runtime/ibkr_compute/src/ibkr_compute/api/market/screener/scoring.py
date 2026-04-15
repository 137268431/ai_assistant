from __future__ import annotations

from ibkr_compute.api.market.screener.coercion import coerce_float


def build_tradability_assessment(row: dict) -> tuple[int, list[str]]:
    score = 0
    notes = []

    price = coerce_float(row.get("price"))
    avg_10d_volume = coerce_float(row.get("avg_10d_volume"))
    premarket_volume = coerce_float(row.get("premarket_volume"))
    today_volume = coerce_float(row.get("today_volume"))
    atr_pct = abs(coerce_float(row.get("atr_pct")))
    day_change_pct = abs(coerce_float(row.get("day_change_pct")))
    freshness_min = row.get("freshness_min")
    target_score = coerce_float(row.get("target_score"))

    if 2 <= price <= 80:
        score += 12
        notes.append("价位适中")
    elif 1 <= price <= 150:
        score += 6

    if avg_10d_volume >= 5_000_000:
        score += 18
        notes.append("10日均量>500万")
    elif avg_10d_volume >= 1_000_000:
        score += 12
        notes.append("10日均量>100万")
    elif avg_10d_volume >= 500_000:
        score += 6

    if premarket_volume >= 500_000:
        score += 18
        notes.append("盘前量能>50万")
    elif premarket_volume >= 100_000:
        score += 12
        notes.append("盘前量能>10万")
    elif today_volume >= 300_000:
        score += 8
        notes.append("当日成交活跃")

    if 2 <= atr_pct <= 12:
        score += 16
        notes.append("ATR波动充足")
    elif 1 <= atr_pct <= 20:
        score += 8

    if day_change_pct >= 2:
        score += 12
        notes.append("日内波动>2%")
    elif day_change_pct >= 0.8:
        score += 6

    if isinstance(freshness_min, int):
        if freshness_min <= 20:
            score += 14
            notes.append("bars新鲜")
        elif freshness_min <= 60:
            score += 8
        elif freshness_min <= 180:
            score += 3

    if target_score >= 10:
        score += 10
        notes.append("已入目标池")
    elif target_score >= 5:
        score += 6

    if str(row.get("direction_bias") or "").strip().lower() in {"long", "short"}:
        score += 4

    return min(100, score), notes


__all__ = ["build_tradability_assessment"]
