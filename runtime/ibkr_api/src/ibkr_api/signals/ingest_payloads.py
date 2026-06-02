from __future__ import annotations

import json
from typing import Any

from ibkr_api.orders.values import to_float, to_text
from ibkr_compute.market.timeframe_utils import format_cn_time, format_us_time


def as_object(value: Any) -> dict[str, Any]:
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


def first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _to_number_or_default(value: Any, default: float = 0) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float(default)


def _to_int_or_none(value: Any) -> int | None:
    parsed = to_float(value)
    return int(parsed) if parsed is not None else None


def normalize_risk_reward_value(raw_value: Any, entry: Any, stop_loss: Any, take_profit: Any) -> str:
    direct_value = to_float(raw_value)
    if direct_value is not None:
        return f"{direct_value:.2f}"

    text = to_text(raw_value).strip()
    if text:
        normalized_text = text.replace("：", ":")
        if ":" in normalized_text:
            numerator_text, denominator_text = (normalized_text.split(":", 1) + [""])[:2]
            numerator = to_float(numerator_text)
            denominator = to_float(denominator_text)
            if numerator is not None and denominator not in {None, 0}:
                return f"{(numerator / float(denominator)):.2f}"
        ratio_value = to_float(normalized_text)
        if ratio_value is not None:
            return f"{ratio_value:.2f}"

    entry_price = to_float(entry)
    stop_loss_price = to_float(stop_loss)
    take_profit_price = to_float(take_profit)
    if entry_price is None or stop_loss_price is None or take_profit_price is None:
        return text
    risk = abs(entry_price - stop_loss_price)
    reward = abs(take_profit_price - entry_price)
    if risk <= 0:
        return text
    return f"{(reward / risk):.2f}"


def normalize_signal_source_meta(raw_value: Any) -> dict[str, str]:
    raw = to_text(raw_value).lower()
    if raw in {"tv", "tradingview", "webhook_tv", "tv_webhook", "signal"}:
        return {
            "route_source": "tradingview",
            "signal_source": "tradingview_webhook",
            "signal_source_label": "TradingView Webhook",
            "signal_source_detail": "来自 TradingView webhook 信号",
        }
    if raw in {"ibkr_compute_timeline", "timeline", "chart_timeline"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_compute_timeline",
            "signal_source_label": "IBKR 图表回放",
            "signal_source_detail": "来自缓存 bars 时间线重算",
        }
    if raw in {"history_repair", "recompute", "backfill_recompute"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_history_recompute",
            "signal_source_label": "IBKR 历史重算",
            "signal_source_detail": "来自历史回补/重算链路",
        }
    if raw in {"manual", "manual_order", "runtime_page", "account_page"}:
        return {
            "route_source": "manual",
            "signal_source": "manual_order",
            "signal_source_label": "手动触发",
            "signal_source_detail": "来自账户页/人工操作",
        }
    if raw in {"ibkr_runtime", "ibkr_compute_realtime", "ibkr_compute", "ibkr"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_compute_realtime",
            "signal_source_label": "IBKR 实时计算",
            "signal_source_detail": "来自 IBKR 实盘 bars 收盘计算",
        }
    return {
        "route_source": raw or "unknown",
        "signal_source": raw or "unknown",
        "signal_source_label": raw.upper() if raw else "未知来源",
        "signal_source_detail": "",
    }


def build_signal_record_payload(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = to_text(payload.get("symbol")).upper()
    signal_id = to_text(payload.get("signal_id"))
    incoming_extra = as_object(payload.get("extra"))
    if not symbol or not signal_id:
        return None, "Missing symbol or signal_id"
    bar_time_ms = int(_to_number_or_default(payload.get("bar_time_ms"), 0))
    us_time = to_text(payload.get("us_time")) or (format_us_time(bar_time_ms) if bar_time_ms > 0 else "")
    cn_time = to_text(payload.get("cn_time")) or (format_cn_time(bar_time_ms) if bar_time_ms > 0 else "")
    date_value = to_text(payload.get("date")) or us_time[:10]

    source_meta = normalize_signal_source_meta(
        first_defined(
            payload.get("signal_source"),
            incoming_extra.get("signal_source"),
            payload.get("source"),
            incoming_extra.get("source"),
            "ibkr_compute",
        )
    )
    record = {
        "symbol": symbol,
        "environment": environment,
        "direction": to_text(payload.get("direction")).lower(),
        "signal": to_text(payload.get("signal")),
        "limit_price": _to_number_or_default(payload.get("limit_price"), 0),
        "entry": _to_number_or_default(payload.get("entry"), 0),
        "stop_loss": _to_number_or_default(payload.get("stop_loss"), 0),
        "take_profit": _to_number_or_default(payload.get("take_profit"), 0),
        "rr": normalize_risk_reward_value(
            payload.get("rr"),
            payload.get("entry"),
            payload.get("stop_loss"),
            payload.get("take_profit"),
        ),
        "shares": _to_number_or_default(payload.get("shares"), 0),
        "signal_id": signal_id,
        "exchange": to_text(payload.get("exchange")).upper(),
        "interval": to_text(payload.get("interval")),
        "reason": to_text(payload.get("reason")),
        "us_time": us_time,
        "cn_time": cn_time,
        "date": date_value,
        "bar_time_ms": bar_time_ms,
        "bar_index": _to_int_or_none(payload.get("bar_index")),
        "script_tag": to_text(payload.get("script_tag")),
        "chart_tf": to_text(payload.get("chart_tf")),
        "extra": {
            **incoming_extra,
            "source": to_text(first_defined(incoming_extra.get("source"), source_meta.get("route_source"))),
            "signal_source": to_text(
                first_defined(incoming_extra.get("signal_source"), payload.get("signal_source"), source_meta.get("signal_source"))
            ),
            "signal_source_label": to_text(
                first_defined(
                    incoming_extra.get("signal_source_label"),
                    payload.get("signal_source_label"),
                    source_meta.get("signal_source_label"),
                )
            ),
            "signal_source_detail": to_text(
                first_defined(
                    incoming_extra.get("signal_source_detail"),
                    payload.get("signal_source_detail"),
                    source_meta.get("signal_source_detail"),
                )
            ),
            "risk_r": first_defined(incoming_extra.get("risk_r"), payload.get("risk_r")),
            "exit_policy": to_text(first_defined(incoming_extra.get("exit_policy"), payload.get("exit_policy"))),
            "exit_policy_profile": to_text(
                first_defined(incoming_extra.get("exit_policy_profile"), payload.get("exit_policy_profile"))
            ),
            "exit_policy_type": to_text(first_defined(incoming_extra.get("exit_policy_type"), payload.get("exit_policy_type"))),
            "environment": environment,
        },
        "status": to_text(payload.get("status") or "pending").lower() or "pending",
        "note": to_text(payload.get("note")),
    }
    return record, ""


__all__ = [
    "as_object",
    "build_signal_record_payload",
    "first_defined",
    "normalize_risk_reward_value",
    "normalize_signal_source_meta",
]
