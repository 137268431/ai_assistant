from __future__ import annotations

from ibkr_compute.api.market.screener.coercion import coerce_float

TRADABILITY_SCORE_CAP = 100
TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME = 500_000
TRADABILITY_OPERABLE_MIN_SCORE = 60
TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN = 90

TRADABILITY_PRICE_RULES = (
    {"minimum": 2, "maximum": 80, "score": 12, "note": "价位适中", "summary": "$2-$80"},
    {"minimum": 1, "maximum": 150, "score": 6, "summary": "$1-$150"},
)
TRADABILITY_AVG_10D_VOLUME_RULES = (
    {"minimum": 5_000_000, "score": 18, "note": "10日均量>500万", "summary": "10D均量>=5M"},
    {"minimum": 1_000_000, "score": 12, "note": "10日均量>100万", "summary": "10D均量>=1M"},
    {"minimum": 500_000, "score": 6, "summary": "10D均量>=500K"},
)
TRADABILITY_ACTIVITY_RULES = (
    {"field": "premarket_volume", "minimum": 500_000, "score": 18, "note": "盘前量能>50万", "summary": "盘前量>=500K"},
    {"field": "premarket_volume", "minimum": 100_000, "score": 12, "note": "盘前量能>10万", "summary": "盘前量>=100K"},
    {"field": "today_volume", "minimum": 300_000, "score": 8, "note": "当日成交活跃", "summary": "当日成交>=300K"},
)
TRADABILITY_ATR_PCT_RULES = (
    {"minimum": 2, "maximum": 12, "score": 16, "note": "ATR波动充足", "summary": "ATR%=2-12"},
    {"minimum": 1, "maximum": 20, "score": 8, "summary": "ATR%=1-20"},
)
TRADABILITY_DAY_CHANGE_RULES = (
    {"minimum": 2, "score": 12, "note": "日内波动>2%", "summary": "|Day%|>=2"},
    {"minimum": 0.8, "score": 6, "summary": "|Day%|>=0.8"},
)
TRADABILITY_FRESHNESS_RULES = (
    {"maximum": 20, "score": 14, "note": "bars新鲜", "summary": "freshness<=20m"},
    {"maximum": 60, "score": 8, "summary": "freshness<=60m"},
    {"maximum": 180, "score": 3, "summary": "freshness<=180m"},
)
TRADABILITY_TARGET_SCORE_RULES = (
    {"minimum": 10, "score": 10, "note": "已入目标池", "summary": "target_score>=10"},
    {"minimum": 5, "score": 6, "summary": "target_score>=5"},
)
TRADABILITY_DIRECTION_RULE = {
    "values": {"long", "short"},
    "score": 4,
    "summary": "direction_bias in {long, short}",
}


def _match_range_rule(value: float, rules) -> dict | None:
    for rule in rules:
        if rule["minimum"] <= value <= rule["maximum"]:
            return rule
    return None


def _match_min_rule(value: float, rules) -> dict | None:
    for rule in rules:
        if value >= rule["minimum"]:
            return rule
    return None


def _match_max_rule(value: int, rules) -> dict | None:
    for rule in rules:
        if value <= rule["maximum"]:
            return rule
    return None


def _match_activity_rule(row: dict, rules) -> dict | None:
    for rule in rules:
        if coerce_float(row.get(rule["field"])) >= rule["minimum"]:
            return rule
    return None


def build_tradability_rule_summary() -> dict:
    return {
        "score_cap": TRADABILITY_SCORE_CAP,
        "price": TRADABILITY_PRICE_RULES,
        "avg_10d_volume": TRADABILITY_AVG_10D_VOLUME_RULES,
        "activity": TRADABILITY_ACTIVITY_RULES,
        "atr_pct": TRADABILITY_ATR_PCT_RULES,
        "day_change_pct": TRADABILITY_DAY_CHANGE_RULES,
        "freshness": TRADABILITY_FRESHNESS_RULES,
        "target_score": TRADABILITY_TARGET_SCORE_RULES,
        "direction": TRADABILITY_DIRECTION_RULE,
        "operable_requirements": {
            "has_live_bar": True,
            "price_gt": 0,
            "avg_10d_volume_gte": TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
            "score_gte": TRADABILITY_OPERABLE_MIN_SCORE,
            "freshness_lte_min": TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
        },
    }


def build_tradability_assessment(row: dict) -> tuple[int, list[str]]:
    score = 0
    notes = []

    price = coerce_float(row.get("price"))
    avg_10d_volume = coerce_float(row.get("avg_10d_volume"))
    atr_pct = abs(coerce_float(row.get("atr_pct")))
    day_change_pct = abs(coerce_float(row.get("day_change_pct")))
    freshness_min = row.get("freshness_min")
    target_score = coerce_float(row.get("target_score"))

    price_rule = _match_range_rule(price, TRADABILITY_PRICE_RULES)
    if price_rule:
        score += price_rule["score"]
        if price_rule.get("note"):
            notes.append(price_rule["note"])

    avg_volume_rule = _match_min_rule(avg_10d_volume, TRADABILITY_AVG_10D_VOLUME_RULES)
    if avg_volume_rule:
        score += avg_volume_rule["score"]
        if avg_volume_rule.get("note"):
            notes.append(avg_volume_rule["note"])

    activity_rule = _match_activity_rule(row, TRADABILITY_ACTIVITY_RULES)
    if activity_rule:
        score += activity_rule["score"]
        if activity_rule.get("note"):
            notes.append(activity_rule["note"])

    atr_rule = _match_range_rule(atr_pct, TRADABILITY_ATR_PCT_RULES)
    if atr_rule:
        score += atr_rule["score"]
        if atr_rule.get("note"):
            notes.append(atr_rule["note"])

    day_change_rule = _match_min_rule(day_change_pct, TRADABILITY_DAY_CHANGE_RULES)
    if day_change_rule:
        score += day_change_rule["score"]
        if day_change_rule.get("note"):
            notes.append(day_change_rule["note"])

    if isinstance(freshness_min, int):
        freshness_rule = _match_max_rule(freshness_min, TRADABILITY_FRESHNESS_RULES)
        if freshness_rule:
            score += freshness_rule["score"]
            if freshness_rule.get("note"):
                notes.append(freshness_rule["note"])

    target_score_rule = _match_min_rule(target_score, TRADABILITY_TARGET_SCORE_RULES)
    if target_score_rule:
        score += target_score_rule["score"]
        if target_score_rule.get("note"):
            notes.append(target_score_rule["note"])

    if str(row.get("direction_bias") or "").strip().lower() in TRADABILITY_DIRECTION_RULE["values"]:
        score += TRADABILITY_DIRECTION_RULE["score"]

    return min(TRADABILITY_SCORE_CAP, score), notes


__all__ = [
    "TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN",
    "TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME",
    "TRADABILITY_OPERABLE_MIN_SCORE",
    "build_tradability_assessment",
    "build_tradability_rule_summary",
]
