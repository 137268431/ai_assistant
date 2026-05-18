from __future__ import annotations

import json
import re
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode


def to_finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def coerce_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "":
        return ""
    if text == "true":
        return True
    if text == "false":
        return False
    numeric = to_finite_number(text)
    if numeric is not None and text.replace(".", "", 1).replace("-", "", 1).isdigit():
        if "." not in text:
            return int(numeric)
        return numeric
    return value


def normalize_risk_reward_value(raw_value: Any, entry: Any, stop_loss: Any, take_profit: Any) -> str:
    direct_value = to_finite_number(raw_value)
    if direct_value is not None:
        return f"{direct_value:.2f}"

    text = str(raw_value or "").strip()
    if text:
        normalized_text = text.replace("：", ":")
        if ":" in normalized_text:
            parts = [segment.strip() for segment in normalized_text.split(":", 1)]
            numerator = to_finite_number(parts[0])
            denominator = to_finite_number(parts[1]) if len(parts) > 1 else None
            if numerator is not None and denominator not in {None, 0}:
                return f"{(numerator / float(denominator)):.2f}"
        ratio_value = to_finite_number(normalized_text)
        if ratio_value is not None:
            return f"{ratio_value:.2f}"

    entry_price = to_finite_number(entry)
    stop_loss_price = to_finite_number(stop_loss)
    take_profit_price = to_finite_number(take_profit)
    if entry_price is not None and stop_loss_price is not None and take_profit_price is not None:
        risk = abs(entry_price - stop_loss_price)
        reward = abs(take_profit_price - entry_price)
        if risk > 0:
            return f"{(reward / risk):.2f}"
    return text


def extract_signal_date(us_time_text: Any, *, time_strings: Callable[[], dict[str, str]]) -> str:
    text = str(us_time_text or "").strip()
    if text:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            return str(match.group(1))
    return time_strings()["date"]


def upsert_tv_indicator(
    payload: dict[str, Any],
    *,
    pb: Any,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    jsonify_fn: Callable[[dict[str, Any]], Any],
):
    alias_map = {
        "dayChangePct": "day_change_pct",
        "prevCloseChangePct": "prev_close_change_pct",
        "change7d": "change_7d",
        "obvRsi": "obv_rsi",
        "emaBullTouch": "ema_bull_touch",
        "emaBearTouch": "ema_bear_touch",
        "emaBullish": "ema_bullish",
        "emaBearish": "ema_bearish",
        "vwapUpper1": "vwap_upper1",
        "vwapLower1": "vwap_lower1",
        "vwapUpper2": "vwap_upper2",
        "vwapLower2": "vwap_lower2",
        "vwapDist": "vwap_dist",
        "sdStdDev": "sd_std_dev",
        "dtpPhaseBars": "dtp_phase_bars",
    }
    raw_extra = parse_object(payload.get("extra"))
    extra = {key: coerce_scalar(value) for key, value in raw_extra.items()}
    for from_key, to_key in alias_map.items():
        if extra.get(from_key) is not None and extra.get(to_key) is None:
            extra[to_key] = extra.get(from_key)

    symbol = str(payload.get("symbol") or extra.get("symbol") or "").strip().upper()
    interval = str(payload.get("interval") or extra.get("interval") or "").strip()
    exchange = str(payload.get("exchange") or extra.get("exchange") or "").strip().upper()
    script_tag = str(payload.get("script_tag") or extra.get("script_tag") or "").strip()
    us_time = str(payload.get("us_time") or extra.get("us_time") or "").strip()
    cn_time = str(payload.get("cn_time") or extra.get("cn_time") or "").strip()
    bar_time_ms = to_finite_number(payload.get("bar_time_ms") if payload.get("bar_time_ms") is not None else extra.get("bar_time_ms"))
    raw_bar_index = payload.get("bar_index") if payload.get("bar_index") is not None else extra.get("bar_index")
    bar_index = to_finite_number(raw_bar_index)

    if not symbol:
        return jsonify_fn({"ok": False, "error": "Missing required field: symbol", "type": "indicator"}), 400
    if not interval:
        return jsonify_fn({"ok": False, "error": "Missing required field: interval", "type": "indicator", "symbol": symbol}), 400
    if bar_time_ms is None or bar_time_ms <= 0:
        return (
            jsonify_fn({"ok": False, "error": "Invalid bar_time_ms", "type": "indicator", "symbol": symbol, "interval": interval}),
            400,
        )

    broker_mode = request_broker_mode(payload)
    environment = request_market_data_mode(payload)
    extra.update(
        {
            "symbol": symbol,
            "interval": interval,
            "bar_time_ms": int(bar_time_ms),
            "environment": environment,
            "broker_mode": broker_mode,
            "data_environment": environment,
        }
    )
    if exchange:
        extra["exchange"] = exchange
    if script_tag:
        extra["script_tag"] = script_tag
    if us_time:
        extra["us_time"] = us_time
    if cn_time:
        extra["cn_time"] = cn_time
    if bar_index is not None:
        extra["bar_index"] = int(bar_index)
    extra.setdefault("source", "tradingview")

    dedup_key = f"{int(bar_time_ms)}_{symbol}_{interval}"
    existing = pb.get_first_record(
        "tv_indicators",
        filter=(
            f'bar_time_ms = {int(bar_time_ms)} && '
            f'symbol = "{escape_filter_string(symbol)}" && '
            f'interval = "{escape_filter_string(interval)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
    )
    if existing:
        return jsonify_fn({"ok": True, "msg": "duplicate indicator, skipped", "key": dedup_key})

    record = pb.create_record(
        "tv_indicators",
        {
            "symbol": symbol,
            "environment": environment,
            "exchange": exchange,
            "interval": interval,
            "script_tag": script_tag,
            "us_time": us_time,
            "cn_time": cn_time,
            "bar_time_ms": int(bar_time_ms),
            "bar_index": int(bar_index) if bar_index is not None else None,
            "extra": extra,
        },
    )
    return jsonify_fn({"ok": True, "type": "indicator", "key": dedup_key, "id": str(record.get("id") or "")})


def upsert_tv_signal(
    payload: dict[str, Any],
    *,
    pb: Any,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    jsonify_fn: Callable[[dict[str, Any]], Any],
    time_strings: Callable[[], dict[str, str]],
):
    required_fields = ("symbol", "direction", "entry", "stop_loss", "take_profit", "signal_id")
    for field in required_fields:
        if payload.get(field) in {None, ""}:
            return jsonify_fn({"ok": False, "error": f"Missing required field: {field}", "signal_id": payload.get("signal_id") or "unknown"}), 400

    for field in ("entry", "stop_loss", "take_profit"):
        number = to_finite_number(payload.get(field))
        if number is None or number <= 0:
            return jsonify_fn({"ok": False, "error": f"Invalid {field}: must be positive number", "signal_id": payload.get("signal_id")}), 400

    direction = str(payload.get("direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        return jsonify_fn({"ok": False, "error": "Invalid direction: must be 'long' or 'short'", "signal_id": payload.get("signal_id")}), 400

    broker_mode = request_broker_mode(payload)
    environment = request_market_data_mode(payload)
    base_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    extra = dict(base_extra)
    extra["environment"] = environment
    extra["broker_mode"] = broker_mode
    extra["data_environment"] = environment
    extra.setdefault("source", "tradingview")
    date_str = extract_signal_date(payload.get("us_time"), time_strings=time_strings)

    signal_id = str(payload.get("signal_id") or "").strip()
    existing = pb.get_first_record(
        "tv_signals",
        filter=(
            f'signal_id = "{escape_filter_string(signal_id)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
    )
    if existing:
        return jsonify_fn(
            {
                "ok": True,
                "type": "signal",
                "created_environments": [],
                "skipped_environments": [environment],
            }
        )

    pb.create_record(
        "tv_signals",
        {
            "symbol": payload.get("symbol"),
            "environment": environment,
            "direction": direction,
            "signal": payload.get("signal"),
            "limit_price": payload.get("limit_price"),
            "entry": payload.get("entry"),
            "stop_loss": payload.get("stop_loss"),
            "take_profit": payload.get("take_profit"),
            "rr": normalize_risk_reward_value(
                payload.get("rr"),
                payload.get("entry"),
                payload.get("stop_loss"),
                payload.get("take_profit"),
            ),
            "shares": payload.get("shares"),
            "signal_id": signal_id,
            "exchange": payload.get("exchange"),
            "interval": payload.get("interval"),
            "reason": extra.get("reason"),
            "us_time": str(payload.get("us_time") or ""),
            "cn_time": str(payload.get("cn_time") or ""),
            "date": date_str,
            "bar_time_ms": extra.get("bar_time_ms"),
            "bar_index": extra.get("bar_index"),
            "script_tag": extra.get("script_tag"),
            "chart_tf": extra.get("chart_tf"),
            "extra": extra,
            "status": str(payload.get("status") or "pending"),
        },
    )
    return jsonify_fn(
        {
            "ok": True,
            "type": "signal",
            "created_environments": [environment],
            "skipped_environments": [],
        }
    )
