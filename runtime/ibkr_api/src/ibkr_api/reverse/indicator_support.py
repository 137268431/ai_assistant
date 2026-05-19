from __future__ import annotations

from typing import Any, Callable

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment

from ibkr_api.reverse.shared import (
    DEFAULT_REVERSE_THRESHOLD,
    INDICATORS_COLLECTION,
    ensure_object,
    escape_filter_string,
    parse_integer,
    record_value,
    to_float,
    to_text,
)


def _to_number(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return fallback if parsed is None else parsed


def _is_truthy(value: Any, *, parse_boolean: Callable[[Any, bool], bool]) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return parse_boolean(value, False)


def map_strength(score: Any) -> str:
    numeric_score = _to_number(score, 0)
    if numeric_score >= 6:
        return "strong"
    if numeric_score >= 3:
        return "medium"
    return "weak"


def resolve_indicator_action(score: Any, target_state: Any, forced_action_type: Any = "") -> str:
    forced = to_text(forced_action_type)
    if forced:
        return forced
    normalized_target_state = to_text(target_state)
    numeric_score = _to_number(score, 0)
    if normalized_target_state == "pending_entry":
        return "cancel"
    if numeric_score >= 6:
        return "close"
    if numeric_score >= 3:
        return "adjust_sl"
    return "cancel"


def load_latest_indicator_record(
    pb: Any,
    symbol: str,
    environment: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> Any:
    normalized_symbol = to_text(symbol).upper()
    runtime_environment = resolve_data_environment(environment)
    if not normalized_symbol:
        return None
    return pb.get_first_record(
        INDICATORS_COLLECTION,
        filter=(
            f'(environment = "{escape_filter(runtime_environment)}" || environment = "") && '
            f'symbol = "{escape_filter(normalized_symbol)}"'
        ),
        sort="-bar_time_ms",
    )


def build_indicator_analysis(
    direction: Any,
    indicator_record: Any,
    *,
    parse_boolean: Callable[[Any, bool], bool],
) -> dict[str, Any]:
    extra = ensure_object(record_value(indicator_record, "extra"))
    normalized_direction = to_text(direction).lower()
    crsi = _to_number(extra.get("crsi"), 0)
    obv_rsi = _to_number(extra.get("obv_rsi"), 0)
    vwap_dist = to_float(extra.get("vwap_dist"))
    close = _to_number(extra.get("close"), 0)

    score = 0
    triggered_signals: list[str] = []

    if normalized_direction == "long" and crsi > 70:
        score += 2
        triggered_signals.append("cRSI超买")
    elif normalized_direction == "short" and crsi < 30:
        score += 2
        triggered_signals.append("cRSI超卖")

    is_bear_signal = normalized_direction == "long"
    crsi_reverse_div = extra.get("crsi_bear_div") if is_bear_signal else extra.get("crsi_bull_div")
    obv_reverse_div = extra.get("obv_bear_div") if is_bear_signal else extra.get("obv_bull_div")
    fractal_reverse = extra.get("fractal_bear") if is_bear_signal else extra.get("fractal_bull")
    sd_channel_reverse = extra.get("sd_upper") if is_bear_signal else extra.get("sd_lower")
    ema_touch_reverse = extra.get("ema_bear_touch") if is_bear_signal else extra.get("ema_bull_touch")

    if _is_truthy(crsi_reverse_div, parse_boolean=parse_boolean) or _is_truthy(obv_reverse_div, parse_boolean=parse_boolean):
        score += 3
        triggered_signals.append("背离")

    if _is_truthy(fractal_reverse, parse_boolean=parse_boolean) or _is_truthy(sd_channel_reverse, parse_boolean=parse_boolean):
        score += 2
        triggered_signals.append("分形/SD通道")

    if (vwap_dist is not None and abs(vwap_dist) > 2) or _is_truthy(ema_touch_reverse, parse_boolean=parse_boolean):
        score += 1
        triggered_signals.append("VWAP偏离/EMA触碰")

    return {
        "score": score,
        "triggered_signals": triggered_signals,
        "indicator_extra": extra,
        "crsi": crsi,
        "obv_rsi": obv_rsi,
        "vwap_dist": vwap_dist if vwap_dist is not None else 0,
        "close": close,
    }


def load_reverse_signal_threshold(pb: Any, environment: str, default: int = DEFAULT_REVERSE_THRESHOLD) -> int:
    runtime_environment = normalize_broker_mode(environment, configured_broker_mode())
    getter = getattr(pb, "get_runtime_config", None)
    if callable(getter):
        try:
            rows = getter(scope="all", environment=runtime_environment)
        except TypeError:
            rows = getter(environment=runtime_environment)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if to_text(row.get("key")) != "reverse_signal_threshold":
                    continue
                return parse_integer(row.get("value"), default=default, minimum=1)
    fallback_value = getattr(pb, "reverse_signal_threshold", None)
    return parse_integer(fallback_value, default=default, minimum=1)


__all__ = [
    "build_indicator_analysis",
    "load_latest_indicator_record",
    "load_reverse_signal_threshold",
    "map_strength",
    "resolve_indicator_action",
]
