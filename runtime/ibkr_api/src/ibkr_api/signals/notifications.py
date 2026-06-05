from __future__ import annotations

import time
from urllib.parse import urlencode
from typing import Any, Callable

from ibkr_api.orders.values import first_defined, to_float, to_text
from ibkr_api.signals.values import get_signal_extra, merge_signal_extra, record_value
from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode


SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
SignalStatusNotifier = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]

_SIGNAL_STATUS_META = {
    "awaiting_confirm": {"emoji": "🔔", "text": "待确认", "template": "orange"},
    "pending": {"emoji": "✅", "text": "已确认", "template": "green"},
    "submitted": {"emoji": "📨", "text": "订单已提交", "template": "blue"},
    "submitted_waiting_fill": {"emoji": "⏳", "text": "已提交，等待成交", "template": "blue"},
    "filled_repricing_protection": {"emoji": "🛠️", "text": "已成交，保护单重定价中", "template": "blue"},
    "filled_position": {"emoji": "📈", "text": "持仓已建立", "template": "green"},
    "protected_active": {"emoji": "🛡️", "text": "保护单已生效", "template": "blue"},
    "protection_incomplete": {"emoji": "⚠️", "text": "保护单不完整", "template": "orange"},
    "protection_reprice_failed": {"emoji": "❌", "text": "保护单重定价失败", "template": "red"},
    "entry_missed_limit_cap": {"emoji": "⛔", "text": "入场未成交（触及限价上限）", "template": "orange"},
    "ignored_no_broker_position": {"emoji": "🚫", "text": "已忽略：无券商持仓", "template": "grey"},
    "stale_signal": {"emoji": "⏰", "text": "信号陈旧", "template": "grey"},
    "signal_clock_skew": {"emoji": "⏱️", "text": "信号时钟偏差", "template": "orange"},
    "stale_signal/signal_clock_skew": {"emoji": "⏱️", "text": "信号陈旧/时钟偏差", "template": "orange"},
    "executed": {"emoji": "🚀", "text": "已执行", "template": "blue"},
    "cancelled": {"emoji": "🚫", "text": "已取消", "template": "grey"},
    "canceled": {"emoji": "🚫", "text": "已取消", "template": "grey"},
    "rejected": {"emoji": "❌", "text": "已拒绝", "template": "red"},
    "expired": {"emoji": "⏰", "text": "已过期", "template": "grey"},
    "closed": {"emoji": "🧾", "text": "已平仓", "template": "grey"},
}

_SIGNAL_CONSUMED_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "protection_incomplete",
    "protection_reprice_failed",
    "entry_missed_limit_cap",
    "ignored_no_broker_position",
    "stale_signal",
    "signal_clock_skew",
    "stale_signal/signal_clock_skew",
    "executed",
    "cancelled",
    "canceled",
    "rejected",
    "expired",
    "closed",
}


def _status_meta(status: Any) -> dict[str, str]:
    return dict(_SIGNAL_STATUS_META.get(to_text(status).lower() or "pending", _SIGNAL_STATUS_META["pending"]))


def _format_price(value: Any) -> str:
    parsed = to_float(value)
    return "-" if parsed is None else f"{parsed:.2f}"


def _format_quantity(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    if int(parsed) == parsed:
        return str(int(parsed))
    return f"{parsed:.2f}"


def _format_count(value: Any, default: int = 0) -> str:
    parsed = to_float(value)
    if parsed is None:
        return str(default)
    return str(int(parsed))


def _format_money(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    return f"${parsed:,.2f}"


def _format_signed_money(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    if parsed > 0:
        return f"+${parsed:,.2f}"
    if parsed < 0:
        return f"-${abs(parsed):,.2f}"
    return "$0.00"


def _broker_badge(environment: Any) -> str:
    normalized = to_text(environment or "live").lower()
    if normalized in {"live", "paper"}:
        return f"Broker {normalized.upper()}"
    return normalized.upper() if normalized else "Broker LIVE"


def _normalized_broker_candidate(value: Any) -> str:
    normalized = to_text(value).lower()
    return normalized if normalized in {"live", "paper"} else ""


def _execution_broker_mode(signal_extra: dict[str, Any]) -> str:
    execution_by_mode = signal_extra.get("execution_by_mode")
    if not isinstance(execution_by_mode, dict):
        return ""
    scored: list[tuple[int, int, str]] = []
    for index, mode in enumerate(("paper", "live")):
        payload = execution_by_mode.get(mode)
        if not isinstance(payload, dict):
            continue
        status = to_text(payload.get("status")).lower()
        score = 2 if status in _SIGNAL_CONSUMED_STATUSES else 1 if status else 0
        scored.append((score, -index, mode))
    if not scored:
        return ""
    return max(scored)[2]


def _signal_broker_mode(record_or_data: Any, extra: dict[str, Any] | None = None) -> str:
    signal_extra = extra if isinstance(extra, dict) else get_signal_extra(record_or_data)
    candidates = (
        signal_extra.get("last_ack_broker_mode"),
        signal_extra.get("last_runtime_broker_mode"),
        signal_extra.get("signal_ack_fallback_broker_mode"),
        _execution_broker_mode(signal_extra),
        record_value(record_or_data, "broker_mode"),
        signal_extra.get("broker_mode"),
    )
    for candidate in candidates:
        normalized = _normalized_broker_candidate(candidate)
        if normalized:
            return normalized
    record_environment = to_text(record_value(record_or_data, "environment")).lower()
    if record_environment == "paper":
        return "paper"
    return normalize_broker_mode(configured_broker_mode(), "paper")


def _signal_data_environment(record_or_data: Any, extra: dict[str, Any] | None = None) -> str:
    signal_extra = extra if isinstance(extra, dict) else get_signal_extra(record_or_data)
    candidates = (
        record_value(record_or_data, "data_environment"),
        signal_extra.get("data_environment"),
        signal_extra.get("last_ack_data_environment"),
        signal_extra.get("last_runtime_data_environment"),
        signal_extra.get("signal_ack_fallback_data_environment"),
        record_value(record_or_data, "environment"),
    )
    for candidate in candidates:
        normalized = to_text(candidate).lower()
        if normalized in {"live", "paper", "backtest"}:
            return "live" if normalized == "paper" else normalized
    return "live"


def _data_badge(data_environment: Any) -> str:
    normalized = to_text(data_environment or "live").lower()
    if normalized == "live":
        return "Shared Data"
    return f"Data {normalized.upper()}" if normalized else "Shared Data"


def _broker_execution_payload(record_or_data: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    signal_extra = extra if isinstance(extra, dict) else get_signal_extra(record_or_data)
    execution_by_mode = signal_extra.get("execution_by_mode")
    if not isinstance(execution_by_mode, dict):
        return {}
    broker_mode = _signal_broker_mode(record_or_data, signal_extra)
    payload = execution_by_mode.get(broker_mode)
    return dict(payload) if isinstance(payload, dict) else {}


def _effective_signal_status(record_or_data: Any, extra: dict[str, Any] | None = None) -> str:
    signal_extra = extra if isinstance(extra, dict) else get_signal_extra(record_or_data)
    broker_payload = _broker_execution_payload(record_or_data, signal_extra)
    broker_status = to_text(broker_payload.get("status")).lower()
    return broker_status or to_text(record_value(record_or_data, "status")).lower() or "pending"


def _format_percent(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:.1f}%"


def _metric_float(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().replace(",", "").removesuffix("%")
    return to_float(value)


def _format_signed_percent(value: Any) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:+.2f}%"


def _format_signed_bps(value: Any) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:+.2f} bps"


def _format_signed_r(value: Any) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:+.3f}R"


def _format_number(value: Any, *, digits: int = 2) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:.{digits}f}"


def _has_metric_number(value: Any) -> bool:
    return _metric_float(value) is not None


def _positive_price(record_or_data: Any, *fields: str) -> float | None:
    for field in fields:
        parsed = to_float(_record_or_extra_value(record_or_data, field))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _positive_price_with_field(record_or_data: Any, *fields: str) -> tuple[float | None, str]:
    for field in fields:
        parsed = to_float(_record_or_extra_value(record_or_data, field))
        if parsed is not None and parsed > 0:
            return parsed, field
    return None, ""


def _tv_reference_prices(record_or_data: Any) -> tuple[float | None, float | None, float | None]:
    return (
        _positive_price(
            record_or_data,
            "tv_reference_entry",
            "tv_reference_entry_price",
            "reference_entry",
            "reference_entry_price",
            "original_entry",
            "initial_entry",
            "tv_reference.entry",
            "tv_reference.entry_price",
            "tv_reference.limit_price",
            "tv_reference_prices.entry",
            "tv_reference_plan.entry",
        ),
        _positive_price(
            record_or_data,
            "tv_reference_stop_loss",
            "tv_reference_sl",
            "reference_stop_loss",
            "reference_sl",
            "original_stop_loss",
            "initial_stop_loss",
            "tv_reference.stop_loss",
            "tv_reference.sl",
            "tv_reference_prices.stop_loss",
            "tv_reference_plan.stop_loss",
        ),
        _positive_price(
            record_or_data,
            "tv_reference_take_profit",
            "tv_reference_tp",
            "reference_take_profit",
            "reference_tp",
            "original_take_profit",
            "initial_take_profit",
            "tv_reference.take_profit",
            "tv_reference.tp",
            "tv_reference_prices.take_profit",
            "tv_reference_plan.take_profit",
        ),
    )


def _actual_fill_price(record_or_data: Any) -> float | None:
    return _positive_price(
        record_or_data,
        "executed_price",
        "actual_fill_price",
        "entry_fill_price",
        "fill_price",
        "avg_fill_price",
        "avgFillPrice",
        "avgPrice",
        "last_fill_price",
        "lastFillPrice",
        "protection_rebase_result.actual_fill_price",
    )


def _submitted_limit_price(record_or_data: Any) -> float | None:
    return _positive_price(
        record_or_data,
        "submitted_entry_limit_price",
        "submitted_limit_cap_price",
        "submitted_limit_price",
        "submitted_price",
        "entry_limit_cap_price",
        "limit_cap_price",
        "bounded_limit_price",
        "entry_limit_price",
        "limit_price",
        "order_flow_entry_limit_price",
        "entry",
        "protection_rebase_result.submitted_entry",
    )


def _submitted_limit_cap_price(record_or_data: Any) -> float | None:
    return _positive_price(
        record_or_data,
        "submitted_entry_limit_price",
        "submitted_limit_cap_price",
        "submitted_limit_price",
        "submitted_price",
        "entry_limit_cap_price",
        "limit_cap_price",
        "bounded_limit_price",
        "order_flow_entry_limit_price",
        "protection_rebase_result.submitted_entry",
    )


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return to_text(value).lower() in {"1", "true", "yes", "y", "on"}


def _submitted_limit_cap_lines(record_or_data: Any) -> list[str]:
    price = _submitted_limit_cap_price(record_or_data)
    cap_bps = _record_or_extra_value(
        record_or_data,
        "submitted_limit_cap_bps",
        "entry_limit_cap_bps",
        "limit_cap_bps",
        "limit_cap_applied_bps",
        "entry_limit_cap_applied_bps",
    )
    cap_applied = _truthy_value(
        _record_or_extra_value(
            record_or_data,
            "submitted_limit_cap_applied",
            "entry_limit_cap_applied",
            "limit_cap_applied",
        )
    )
    plan = to_text(
        _record_or_extra_value(record_or_data, "entry_price_plan", "entry_limit_intent", "order_flow_entry_plan")
    )
    if price is None and not _has_metric_number(cap_bps) and not cap_applied:
        return []
    suffix_parts: list[str] = []
    if _has_metric_number(cap_bps):
        suffix_parts.append(f"{_format_number(cap_bps, digits=1)} bps")
    if cap_applied:
        suffix_parts.append("cap applied")
    if plan:
        suffix_parts.append(plan)
    suffix = f" ({' · '.join(suffix_parts)})" if suffix_parts else ""
    return [f"**Submitted Limit Cap**: {_format_price(price)}{suffix}"]


def _final_protection_price_lines(record_or_data: Any) -> list[str]:
    status = _effective_signal_status(record_or_data)
    final_sl = _positive_price(
        record_or_data,
        "final_stop_loss",
        "final_sl",
        "protection_final_stop_loss",
        "repriced_stop_loss",
        "rebased_stop_loss",
        "updated_stop_loss",
        "active_stop_loss",
        "protection_rebase_result.stop_loss",
        "protection_reprice_result.stop_loss",
    )
    final_tp = _positive_price(
        record_or_data,
        "final_take_profit",
        "final_tp",
        "protection_final_take_profit",
        "repriced_take_profit",
        "rebased_take_profit",
        "updated_take_profit",
        "active_take_profit",
        "protection_rebase_result.take_profit",
        "protection_reprice_result.take_profit",
    )
    if final_sl is None and final_tp is None and status in {
        "filled_repricing_protection",
        "filled_position",
        "protected_active",
        "protection_reprice_failed",
        "protection_incomplete",
    }:
        final_sl = _positive_price(record_or_data, "stop_loss", "sl_price")
        final_tp = _positive_price(record_or_data, "take_profit", "tp_price")
    if final_sl is None and final_tp is None:
        return []
    reprice_reason = to_text(
        _record_or_extra_value(record_or_data, "protection_rebase_result.reason", "protection_reprice_result.reason")
    )
    suffix = f" · {reprice_reason}" if reprice_reason else ""
    return [f"**Final SL / TP**: {_format_price(final_sl)} / {_format_price(final_tp)}{suffix}"]


def _directional_slippage_delta(direction: str, actual_fill: float, reference_entry: float) -> float:
    if direction == "short":
        return reference_entry - actual_fill
    return actual_fill - reference_entry


def _slippage_metric_lines(record_or_data: Any) -> list[str]:
    bps_value = _record_or_extra_value(
        record_or_data,
        "entry_slippage_bps",
        "actual_slippage_bps",
        "fill_slippage_bps",
        "slippage_bps",
        "execution_slippage_bps",
        "protection_rebase_result.slippage_bps",
    )
    r_value = _record_or_extra_value(
        record_or_data,
        "entry_slippage_r",
        "actual_slippage_r",
        "fill_slippage_r",
        "slippage_r",
        "execution_slippage_r",
        "protection_rebase_result.slippage_r",
    )
    if not _has_metric_number(bps_value) or not _has_metric_number(r_value):
        actual_fill = _actual_fill_price(record_or_data)
        tv_entry, tv_sl, _tv_tp = _tv_reference_prices(record_or_data)
        reference_entry = tv_entry or _submitted_limit_price(record_or_data)
        if actual_fill is not None and reference_entry is not None and reference_entry > 0:
            direction = to_text(record_value(record_or_data, "direction")).lower()
            delta = _directional_slippage_delta(direction, actual_fill, reference_entry)
            if not _has_metric_number(bps_value):
                bps_value = delta / reference_entry * 10000.0
            if not _has_metric_number(r_value):
                risk_r = to_float(_record_or_extra_value(record_or_data, "risk_r", "initial_risk_r"))
                if (risk_r is None or risk_r <= 0) and tv_entry is not None and tv_sl is not None:
                    risk_r = abs(tv_entry - tv_sl)
                if (risk_r is None or risk_r <= 0) and reference_entry is not None:
                    plan_sl = _positive_price(record_or_data, "stop_loss", "sl_price")
                    if plan_sl is not None:
                        risk_r = abs(reference_entry - plan_sl)
                if risk_r is not None and risk_r > 0:
                    r_value = delta / risk_r
    parts = []
    if _has_metric_number(bps_value):
        parts.append(_format_signed_bps(bps_value))
    if _has_metric_number(r_value):
        parts.append(_format_signed_r(r_value))
    return [f"**Slippage**: {' / '.join(parts)}"] if parts else []


def _adaptive_priority_lines(record_or_data: Any) -> list[str]:
    priority = to_text(
        _record_or_extra_value(
            record_or_data,
            "adaptive_priority",
            "adaptivePriority",
            "ib_algo_adaptive_priority",
            "order_adaptive_priority",
            "adaptive_order_priority",
            "ibkr_adaptive_priority",
            "adaptive.priority",
            "algo.adaptive_priority",
            "order_algo.adaptive_priority",
        )
    )
    if not priority:
        return []
    return [f"**Adaptive priority**: {priority}"]


def _price_plan_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    tv_entry, tv_sl, tv_tp = _tv_reference_prices(record_or_data)
    reference_price, reference_field = _positive_price_with_field(
        record_or_data,
        "pre_submit_reference_price",
        "reference_price",
        "last_price",
        "signal_reference_price",
        "reference_entry",
        "reference_entry_price",
        "tv_reference_entry",
        "tv_reference_entry_price",
        "original_entry",
        "entry",
    )
    entry_limit = _positive_price(
        record_or_data,
        "submitted_entry_limit_price",
        "submitted_limit_price",
        "submitted_price",
        "limit_price",
        "entry_limit_price",
        "entry",
    )
    actual_fill = _actual_fill_price(record_or_data)
    reference_source = to_text(extra.get("pre_submit_reference_source") or extra.get("reference_source"))
    if reference_price is not None and not reference_source:
        reference_source = {
            "signal_reference_price": "signal_reference_price",
            "reference_entry": "tv_reference_entry",
            "reference_entry_price": "tv_reference_entry",
            "tv_reference_entry": "tv_reference_entry",
            "tv_reference_entry_price": "tv_reference_entry",
            "original_entry": "original_entry",
            "entry": "entry",
        }.get(reference_field, "")
    reference_suffix = f" ({reference_source})" if reference_price is not None and reference_source else ""
    lines = []
    if tv_entry is not None:
        lines.append(
            f"**TV参考 Entry / SL / TP**: {_format_price(tv_entry)} / {_format_price(tv_sl)} / {_format_price(tv_tp)}"
        )
    plan_take_profit = _record_or_extra_value(record_or_data, "take_profit", "tp_price")
    plan_stop_loss = _record_or_extra_value(record_or_data, "stop_loss", "sl_price")
    lines.extend(
        [
            f"**参考价**: {_format_price(reference_price)}{reference_suffix}",
            f"**入场限价 / 止盈 / 止损**: "
            f"{_format_price(entry_limit)} / {_format_price(plan_take_profit)} / {_format_price(plan_stop_loss)}",
        ]
    )
    lines.extend(_submitted_limit_cap_lines(record_or_data))
    if actual_fill is not None:
        lines.append(f"**实际成交价**: {_format_price(actual_fill)}")
    lines.extend(_final_protection_price_lines(record_or_data))
    lines.extend(_slippage_metric_lines(record_or_data))
    lines.extend(_adaptive_priority_lines(record_or_data))
    return lines


def _planned_pnl_lines(record_or_data: Any) -> list[str]:
    direction = to_text(record_value(record_or_data, "direction")).lower()
    if direction not in {"long", "short"}:
        return []
    entry = _positive_price(record_or_data, "limit_price", "entry_limit_price", "entry")
    take_profit = _positive_price(record_or_data, "take_profit", "tp_price")
    stop_loss = _positive_price(record_or_data, "stop_loss", "sl_price")
    quantity = _positive_price(record_or_data, "shares", "quantity")
    if entry is None or take_profit is None or stop_loss is None or quantity is None:
        return []
    expected_profit = (take_profit - entry) * quantity if direction == "long" else (entry - take_profit) * quantity
    expected_loss = (stop_loss - entry) * quantity if direction == "long" else (entry - stop_loss) * quantity
    if expected_profit <= 0 or expected_loss >= 0:
        return []
    return [f"**预计盈利 / 预计亏损**: {_format_signed_money(expected_profit)} / {_format_signed_money(expected_loss)}"]


def _profit_space_lines(record_or_data: Any) -> list[str]:
    net_profit = _record_or_extra_value(record_or_data, "expected_net_profit", "profit_space_expected_net_profit")
    roi_pct = _record_or_extra_value(record_or_data, "expected_net_roi_pct", "net_roi_pct", "profit_space_net_roi_pct")
    cost_pct = _record_or_extra_value(record_or_data, "cost_pct_of_reward", "profit_space_cost_pct")
    target_atr = _record_or_extra_value(record_or_data, "target_distance_atr", "profit_space_target_distance_atr")
    if not any(_has_metric_number(value) for value in (net_profit, roi_pct, cost_pct, target_atr)):
        return []

    allowed_raw = _record_or_extra_value(record_or_data, "profit_space_entry_allowed")
    reason = to_text(_record_or_extra_value(record_or_data, "profit_space_filter_reason"))
    allowed_text = to_text(allowed_raw).lower()
    if allowed_text in {"true", "1", "yes"} or reason == "pass":
        state = "过滤通过"
    elif allowed_text in {"false", "0", "no"}:
        state = f"未通过{f' · {reason}' if reason else ''}"
    else:
        state = reason if reason and reason != "disabled" else ""
    suffix = f" · {state}" if state else ""
    return [
        "**获利空间**: "
        f"净 {_format_signed_money(net_profit)} / "
        f"ROI {_format_number(roi_pct)}% / "
        f"成本 {_format_number(cost_pct, digits=1)}% / "
        f"TP {_format_number(target_atr, digits=2)} ATR"
        f"{suffix}"
    ]


def _strategy_capacity_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    capacity = extra.get("strategy_capacity")
    if not isinstance(capacity, dict):
        capacity = record_value(record_or_data, "strategy_capacity")
    if not isinstance(capacity, dict):
        return []
    if capacity.get("available") is False:
        return ["**开仓占用**: 数据暂不可用"]

    used = _format_count(capacity.get("strategy_capacity_used"))
    limit = _format_count(capacity.get("max_strategy_open_positions"))
    positions = _format_count(capacity.get("strategy_open_positions"))
    entries = _format_count(capacity.get("open_strategy_entry_orders"))
    remaining_value = capacity.get("strategy_capacity_remaining")
    remaining = "-" if remaining_value in (None, "") else _format_count(remaining_value)
    return [f"**开仓占用**: {used}/{limit}（持仓 {positions} + Entry {entries}，剩余 {remaining}）"]


def _atr_volatility_text(value: Any) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    label = ""
    if parsed >= 3:
        label = "高波动"
    elif parsed >= 1.5:
        label = "中波动"
    elif parsed > 0:
        label = "低波动"
    suffix = f" ({label})" if label else ""
    return f"{parsed:.2f}%{suffix}"


def _market_metric_lines(record_or_data: Any) -> list[str]:
    day_change_pct = _record_or_extra_value(
        record_or_data,
        "day_change_pct",
        "dayChangePct",
        "current_change_pct",
        "currentChangePct",
        "change_pct",
        "changePct",
        "pct_change",
        "change_1d_pct",
    )
    atr_value = _record_or_extra_value(record_or_data, "atr", "atr_raw", "atrRaw")
    atr_pct = _record_or_extra_value(
        record_or_data,
        "atr_pct",
        "atrPct",
        "atr_percent",
        "atrPercent",
        "atr_volatility_pct",
        "atrVolatilityPct",
    )
    sl_atr_ratio = _record_or_extra_value(
        record_or_data,
        "sl_atr_ratio",
        "slAtrRatio",
        "stop_loss_atr_ratio",
        "stopLossAtrRatio",
    )

    lines: list[str] = []
    if _has_metric_number(day_change_pct):
        lines.append(f"**当前涨幅**: {_format_signed_percent(day_change_pct)}")
    if _has_metric_number(atr_value) or _has_metric_number(atr_pct):
        lines.append(f"**ATR值 / ATR波动率**: {_format_number(atr_value)} / {_atr_volatility_text(atr_pct)}")
    if _has_metric_number(sl_atr_ratio):
        lines.append(f"**止损ATR倍数**: {_format_number(sl_atr_ratio)}x")
    return lines


def _buying_power_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    guard = extra.get("buying_power_guard") if isinstance(extra.get("buying_power_guard"), dict) else {}
    remaining = first_defined(guard.get("remaining") if guard else None, extra.get("buying_power_remaining"))
    remaining_after = first_defined(guard.get("remaining_after") if guard else None, extra.get("buying_power_remaining_after"))
    requested = first_defined(guard.get("requested_exposure") if guard else None, extra.get("buying_power_requested_exposure"))
    pct = first_defined(guard.get("remaining_pct_net_liq") if guard else None, extra.get("buying_power_remaining_pct_net_liq"))
    after_pct = first_defined(
        guard.get("remaining_after_pct_net_liq") if guard else None,
        extra.get("buying_power_remaining_after_pct_net_liq"),
    )
    state = to_text(first_defined(guard.get("state") if guard else None, extra.get("buying_power_guard_state")))
    reason = to_text(first_defined(guard.get("reason") if guard else None, extra.get("buying_power_guard_reason")))
    if remaining in (None, "") and remaining_after in (None, "") and requested in (None, ""):
        return []
    lines = [f"**当前剩余购买力**: {_format_money(remaining)} ({_format_percent(pct)} NetLiq)"]
    lines.append(
        f"**本次预估占用 / 下单后**: {_format_money(requested)} / {_format_money(remaining_after)}"
        f" ({_format_percent(after_pct)} NetLiq)"
    )
    if state and state.lower() != "ok":
        lines.append(f"**购买力状态**: {state.upper()}{f' · {reason}' if reason else ''}")
    return lines


def _status_reason(record_or_data: Any) -> str:
    extra = get_signal_extra(record_or_data)
    status = _effective_signal_status(record_or_data, extra)
    broker_payload = _broker_execution_payload(record_or_data, extra)
    reason = to_text(
        broker_payload.get("status_reason")
        or broker_payload.get("note")
        or extra.get("status_reason")
        or extra.get("initial_status_reason")
        or extra.get("expired_reason")
        or record_value(record_or_data, "note")
    )
    if status == "expired" and ":" in reason:
        _scope, scoped_reason = reason.split(":", 1)
        if scoped_reason in {"signal_expired", "confirm_too_late"}:
            return scoped_reason
        return ""
    return reason


def _page_url(console_base_url: str, path: str, **params: Any) -> str:
    base = str(console_base_url or "").rstrip("/")
    if not base:
        return ""
    query = urlencode({key: value for key, value in params.items() if value not in {None, ""}})
    url = f"{base}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _webhook_url(console_base_url: str, path: str, **params: Any) -> str:
    return _page_url(console_base_url, path, **params)


def _button_url(url: str) -> dict[str, str]:
    return {
        "url": url,
        "pc_url": url,
        "ios_url": url,
        "android_url": url,
    }


def _nested_mapping_value(source: Any, path: str) -> Any:
    if not isinstance(source, dict):
        return None
    current: Any = source
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _record_or_extra_value(record_or_data: Any, *fields: str) -> Any:
    extra = get_signal_extra(record_or_data)
    record_source = record_or_data if isinstance(record_or_data, dict) else {}
    for field in fields:
        if "." in field:
            for source in (record_source, extra):
                value = _nested_mapping_value(source, field)
                if value not in (None, ""):
                    return value
            continue
        value = first_defined(record_value(record_or_data, field), extra.get(field))
        if value not in (None, ""):
            return value
    return ""


def _text_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def _cancel_failure_order_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return _text_items(value)
    order_ids: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = to_text(
                first_defined(
                    item.get("order_id"),
                    item.get("broker_order_id"),
                    item.get("ib_order_id"),
                    item.get("unique_id"),
                    item.get("id"),
                )
            )
        else:
            text = to_text(item)
        if text:
            order_ids.append(text)
    return order_ids


def _signal_cancel_expiry_lines(record_or_data: Any, *, status: str, status_reason: str) -> list[str]:
    status_key = to_text(status).lower()
    lines: list[str] = []

    cancelled_order_ids = _text_items(_record_or_extra_value(record_or_data, "cancelled_order_ids"))
    cancel_failures = _record_or_extra_value(record_or_data, "cancel_order_failures")
    cancel_failure_ids = _cancel_failure_order_ids(cancel_failures)
    has_cancel_context = status_key in {"cancelled", "canceled"} or bool(
        _record_or_extra_value(
            record_or_data,
            "cancel_reason",
            "cancel_requested_at",
            "cancelled_at",
            "cancelled_by",
            "cancel_requested_by",
        )
        or cancelled_order_ids
        or cancel_failure_ids
    )
    if has_cancel_context:
        cancel_reason = to_text(
            _record_or_extra_value(record_or_data, "cancel_reason", "cancelled_reason", "cancellation_reason")
        ) or (status_reason if status_key in {"cancelled", "canceled"} else "")
        cancelled_at = to_text(_record_or_extra_value(record_or_data, "cancelled_at", "canceled_at"))
        requested_at = to_text(_record_or_extra_value(record_or_data, "cancel_requested_at"))
        cancelled_by = to_text(_record_or_extra_value(record_or_data, "cancelled_by", "canceled_by"))
        requested_by = to_text(_record_or_extra_value(record_or_data, "cancel_requested_by"))
        if cancel_reason:
            lines.append(f"**取消原因**: {cancel_reason}")
        if cancelled_at:
            lines.append(f"**取消时间**: {cancelled_at}")
        elif requested_at:
            lines.append(f"**取消请求时间**: {requested_at}")
        if cancelled_by:
            lines.append(f"**取消人**: {cancelled_by}")
        elif requested_by:
            lines.append(f"**取消请求人**: {requested_by}")
        if cancelled_order_ids:
            lines.append(f"**已撤订单**: {', '.join(cancelled_order_ids)}")
        if cancel_failure_ids:
            lines.append(f"**撤单失败**: {len(cancel_failure_ids)}（{', '.join(cancel_failure_ids)}）")
        elif isinstance(cancel_failures, list) and cancel_failures:
            lines.append(f"**撤单失败数**: {len(cancel_failures)}")

    has_expiry_context = status_key == "expired" or bool(
        _record_or_extra_value(
            record_or_data,
            "expired_at",
            "expired_by",
            "expired_reason",
            "expiry_reason",
            "expiry_reference",
            "signal_validity_minutes",
            "validity_minutes",
            "order_expiry_validity_minutes",
            "order_validity_minutes",
            "order_expiry_cutoff_ms",
            "order_expiry_cutoff",
        )
    )
    if has_expiry_context:
        expired_reason = to_text(_record_or_extra_value(record_or_data, "expired_reason", "expiry_reason")) or (
            status_reason if status_key == "expired" else ""
        )
        expired_at = to_text(_record_or_extra_value(record_or_data, "expired_at", "validation_expired_at"))
        expired_by = to_text(_record_or_extra_value(record_or_data, "expired_by"))
        signal_validity_minutes = to_text(_record_or_extra_value(record_or_data, "signal_validity_minutes", "validity_minutes"))
        order_validity_minutes = to_text(_record_or_extra_value(record_or_data, "order_expiry_validity_minutes", "order_validity_minutes"))
        order_cutoff = to_text(_record_or_extra_value(record_or_data, "order_expiry_cutoff", "order_expiry_cutoff_ms"))
        expiry_reference = to_text(_record_or_extra_value(record_or_data, "expiry_reference"))
        if expired_reason:
            lines.append(f"**过期原因**: {expired_reason}")
        if signal_validity_minutes:
            lines.append(f"**信号有效期**: {signal_validity_minutes} 分钟")
        if order_validity_minutes:
            lines.append(f"**订单有效期**: {order_validity_minutes} 分钟")
        if order_cutoff:
            lines.append(f"**订单截止**: {order_cutoff}")
        if expired_at:
            lines.append(f"**过期时间**: {expired_at}")
        if expired_by or expiry_reference:
            parts = [part for part in (expired_by, expiry_reference) if part]
            lines.append(f"**过期来源**: {' · '.join(parts)}")

    return lines


def _record_date(record_or_data: Any) -> str:
    explicit = to_text(_record_or_extra_value(record_or_data, "date", "market_date", "trade_date", "backtest_date"))
    if explicit:
        return explicit[:10]
    for field in ("us_time", "order_time", "created", "updated"):
        text = to_text(_record_or_extra_value(record_or_data, field))
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[:10]
    return ""


def _lifecycle_page_url(
    console_base_url: str,
    *,
    environment: str,
    data_environment: str,
    signal_id: str = "",
    symbol: str = "",
    trade_group_id: str = "",
    order_id: str = "",
    date: str = "",
) -> str:
    if not any(to_text(value) for value in (signal_id, symbol, trade_group_id, order_id)):
        return ""
    return _page_url(
        console_base_url,
        "ibkr_lifecycle_flow.html",
        mode="auto",
        broker_mode=environment,
        market_data_mode=data_environment,
        data_environment=data_environment,
        date=date,
        symbol=to_text(symbol).upper(),
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        order_id=order_id,
    )


def _callback_button(
    label: str,
    button_type: str,
    callback_url: str,
    *,
    action: str,
    signal_id: str,
    environment: str,
    data_environment: str,
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": button_type,
        "text": {"tag": "plain_text", "content": label},
        "action_type": "request",
        "url": callback_url,
        "value": {
            "action": action,
            "signal_id": signal_id,
            "broker_mode": environment,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
        },
    }


def _view_buttons(
    console_base_url: str,
    *,
    environment: str,
    data_environment: str,
    signal_id: str,
    record_or_data: Any | None = None,
) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    source = record_or_data or {}
    resolved_symbol = to_text(_record_or_extra_value(source, "symbol"))
    resolved_trade_group_id = to_text(_record_or_extra_value(source, "trade_group_id", "entry_order_unique_id"))
    resolved_order_id = to_text(_record_or_extra_value(source, "order_id", "broker_order_id", "ib_order_id", "unique_id"))
    resolved_date = _record_date(source)
    signals_url = _page_url(
        console_base_url,
        "ibkr_signals.html",
        broker_mode=environment,
        market_data_mode=data_environment,
        data_environment=data_environment,
        signal_id=signal_id,
    )
    orders_url = _page_url(
        console_base_url,
        "orders.html",
        broker_mode=environment,
        signal_id=signal_id,
        symbol=resolved_symbol,
    )
    lifecycle_url = _lifecycle_page_url(
        console_base_url,
        environment=environment,
        data_environment=data_environment,
        signal_id=signal_id,
        symbol=resolved_symbol,
        trade_group_id=resolved_trade_group_id,
        order_id=resolved_order_id,
        date=resolved_date,
    )
    tv_chart_url = to_text(_record_or_extra_value(source, "tv_chart_url", "chart_url", "tradingview_chart_url"))
    for label, url in (
        ("查看 Signals", signals_url),
        ("查看 Orders", orders_url),
        ("查看事件流", lifecycle_url),
        ("打开 TV 图表", tv_chart_url),
    ):
        if not url:
            continue
        buttons.append(
            {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": label},
                "multi_url": _button_url(url),
            }
        )
    return buttons


def _source_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    label = to_text(extra.get("signal_source_label") or extra.get("signal_source") or extra.get("source"))
    detail = to_text(extra.get("signal_source_detail"))
    lines: list[str] = []
    if label:
        lines.append(f"**来源**: {label}")
    if detail:
        lines.append(f"**由来**: {detail}")
    return lines


def _tv_context_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    fields = [
        ("TV event", first_defined(extra.get("event_type"), extra.get("tv_event_type"))),
        ("TV script", first_defined(record_value(record_or_data, "script_tag"), extra.get("script_tag"))),
        ("TV version", first_defined(extra.get("strategy_version"), extra.get("script_version"), extra.get("version"))),
        (
            "Timeframe",
            first_defined(extra.get("timeframe_stack"), record_value(record_or_data, "chart_tf"), record_value(record_or_data, "interval")),
        ),
        ("Position ID", extra.get("position_id")),
    ]
    lines = [f"**{label}**: {to_text(value)}" for label, value in fields if to_text(value)]
    event_id = to_text(extra.get("tv_event_id"))
    if event_id:
        lines.append(f"**TV event_id**: {event_id}")
    return lines


def _reconfirm_required(record_or_data: Any) -> bool:
    extra = get_signal_extra(record_or_data)
    status = _effective_signal_status(record_or_data, extra)
    return status == "awaiting_confirm" and bool(extra.get("followup_requires_reconfirm") or extra.get("confirmation_stale"))


def _notification_title_prefix(status: str, *, needs_reconfirm: bool) -> str:
    if needs_reconfirm:
        return "🔁 信号已更新，需重新确认"
    if status == "awaiting_confirm":
        return "🔔 新交易信号"
    if status == "pending":
        return "⚙️ 自动确认"
    meta = _status_meta(status)
    return f"{meta['emoji']} {meta['text']}"


def _auto_status_line(status: str) -> str:
    meta = _status_meta(status)
    if status == "pending":
        return "⚙️ **自动确认** · 已进入等待执行队列，尚未提交订单"
    if status in {"submitted", "submitted_waiting_fill"}:
        return f"{meta['emoji']} **{meta['text']}** · 等待成交/执行回报"
    if status in {
        "rejected",
        "expired",
        "cancelled",
        "canceled",
        "entry_missed_limit_cap",
        "ignored_no_broker_position",
        "stale_signal",
        "signal_clock_skew",
        "stale_signal/signal_clock_skew",
    }:
        return f"{meta['emoji']} **{meta['text']}** · 未提交新订单"
    return f"{meta['emoji']} **{meta['text']}**"


def _followup_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    lines: list[str] = []
    needs_reconfirm = _reconfirm_required(record_or_data)
    latest_id = to_text(extra.get("latest_followup_signal_id") or extra.get("latest_merged_signal_id"))
    if latest_id:
        lines.append(f"**后续信号**: {latest_id}")
    raw_changed_fields = extra.get("reconfirm_changed_fields")
    if isinstance(raw_changed_fields, list):
        changed_fields = [to_text(item) for item in raw_changed_fields if to_text(item)]
    else:
        changed_fields = [item.strip() for item in to_text(raw_changed_fields).split(",") if item.strip()]
    if changed_fields:
        label = "需重确认字段" if needs_reconfirm else "已合并字段"
        lines.append(f"**{label}**: {', '.join(changed_fields)}")
    previous = extra.get("previous_confirmed_snapshot")
    if isinstance(previous, dict) and changed_fields:
        previous_parts = []
        for field in changed_fields:
            previous_value = previous.get(field)
            current_value = record_value(record_or_data, field)
            if field in {"entry", "limit_price", "take_profit", "stop_loss"}:
                previous_text = _format_price(previous_value)
                current_text = _format_price(current_value)
            elif field == "shares":
                previous_text = _format_quantity(previous_value)
                current_text = _format_quantity(current_value)
            else:
                previous_text = to_text(previous_value or "-")
                current_text = to_text(current_value or "-")
            previous_parts.append(f"{field}: {previous_text} → {current_text}")
        if previous_parts:
            lines.append(f"**参数变化**: {'; '.join(previous_parts)}")
    return lines


def _confirmation_action_elements(
    console_base_url: str,
    *,
    environment: str,
    data_environment: str,
    signal_id: str,
) -> list[dict[str, Any]]:
    if not signal_id:
        return []
    callback_url = _webhook_url(console_base_url, "webhook/feishu/callback")
    actions: list[dict[str, Any]] = []
    if callback_url:
        actions.append(
            _callback_button(
                "确认",
                "primary",
                callback_url,
                action="confirm",
                signal_id=signal_id,
                environment=environment,
                data_environment=data_environment,
            )
        )
        actions.append(
            _callback_button(
                "拒绝",
                "danger",
                callback_url,
                action="reject",
                signal_id=signal_id,
                environment=environment,
                data_environment=data_environment,
            )
        )
    if actions:
        return [{"tag": "action", "actions": actions}]
    return [{"tag": "markdown", "content": "**人工确认** · 飞书回调未配置，请到 Signals 页面处理"}]


def build_signal_notification_card(record_or_data: Any, *, console_base_url: str = "") -> dict[str, Any]:
    extra = get_signal_extra(record_or_data)
    status = _effective_signal_status(record_or_data, extra)
    symbol = to_text(record_value(record_or_data, "symbol") or record_value(record_or_data, "signal_id") or "SIGNAL")
    direction = to_text(record_value(record_or_data, "direction")).lower()
    signal_id = to_text(record_value(record_or_data, "signal_id") or record_value(record_or_data, "id"))
    status_reason = _status_reason(record_or_data)
    environment = _signal_broker_mode(record_or_data, extra)
    data_environment = _signal_data_environment(record_or_data, extra)
    broker_badge = _broker_badge(environment)
    needs_reconfirm = _reconfirm_required(record_or_data)

    direction_text = {"long": "做多", "short": "做空"}.get(direction, direction or "-")
    body_lines = [
        f"**信号ID**: {signal_id or '-'}",
        f"**方向**: {direction_text}",
        f"**Broker**: {broker_badge}",
        f"**数据**: {_data_badge(data_environment)}",
        f"**仓位 / 风报比**: {_format_quantity(record_value(record_or_data, 'shares'))} / {to_text(record_value(record_or_data, 'rr') or '-')}",
    ]
    body_lines.extend(_price_plan_lines(record_or_data))
    body_lines.extend(_planned_pnl_lines(record_or_data))
    body_lines.extend(_profit_space_lines(record_or_data))
    body_lines.extend(_strategy_capacity_lines(record_or_data))
    body_lines.extend(_buying_power_lines(record_or_data))
    body_lines.extend(_tv_context_lines(record_or_data))
    body_lines.extend(_followup_lines(record_or_data))
    reason = to_text(record_value(record_or_data, "reason") or extra.get("reason"))
    if reason:
        body_lines.append(f"**原因**: {reason}")
    if status_reason:
        body_lines.append(f"**状态原因**: {status_reason}")
    body_lines.extend(_signal_cancel_expiry_lines(record_or_data, status=status, status_reason=status_reason))
    body_lines.append(f"**时间**: {to_text(record_value(record_or_data, 'us_time') or '-')}")

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(body_lines)},
        {"tag": "hr"},
    ]
    if status == "awaiting_confirm" and signal_id:
        elements.extend(
            _confirmation_action_elements(
                console_base_url,
                environment=environment,
                data_environment=data_environment,
                signal_id=signal_id,
            )
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": _auto_status_line(status),
            }
        )

    buttons = _view_buttons(
        console_base_url,
        environment=environment,
        data_environment=data_environment,
        signal_id=signal_id,
        record_or_data=record_or_data,
    )
    if buttons:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": buttons,
                },
            ]
        )

    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"{_notification_title_prefix(status, needs_reconfirm=needs_reconfirm)}"
                    f" · {broker_badge} · {symbol} · {to_text(record_value(record_or_data, 'us_time') or '')}"
                ),
            },
            "template": _status_meta(status)["template"] if status not in {"awaiting_confirm", "pending"} else ("green" if direction == "long" else "red"),
        },
        "elements": elements,
    }


def _notification_key(action: str, record_or_data: Any) -> str:
    return ":".join(
        [
            "signal_notify_v1",
            to_text(action),
            to_text(record_value(record_or_data, "signal_id") or record_value(record_or_data, "id")),
            _effective_signal_status(record_or_data),
        ]
    )


def _build_notification_patch(
    record_or_data: Any,
    *,
    action: str,
    result: dict[str, Any],
    message_id: str,
    notify_key: str,
    include_first_sent_at: bool = False,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    success = bool(result.get("success"))
    patch = {
        "feishu_signal_notify_last_action": to_text(action),
        "feishu_signal_notify_last_status": _effective_signal_status(record_or_data),
        "feishu_signal_notify_last_result": "success" if success else "failed",
        "feishu_signal_notify_last_at_ms": now_ms,
        "feishu_signal_notify_error": "" if success else to_text(result.get("error") or "unknown_error"),
    }
    if not success:
        for result_key, patch_key in (
            ("http_status", "feishu_signal_notify_http_status"),
            ("api_code", "feishu_signal_notify_api_code"),
            ("api_message", "feishu_signal_notify_api_message"),
            ("response_body", "feishu_signal_notify_response_body"),
        ):
            value = result.get(result_key)
            if value not in (None, ""):
                patch[patch_key] = value
    if success:
        patch["feishu_signal_notify_sent_at_ms"] = now_ms
        if notify_key:
            patch["feishu_signal_notify_key"] = notify_key
        resolved_message_id = to_text(result.get("message_id") or message_id)
        if resolved_message_id:
            patch["feishu_signal_message_id"] = resolved_message_id
            patch["feishu_signal_card_version"] = 1
        if include_first_sent_at and not to_text(get_signal_extra(record_or_data).get("feishu_signal_first_sent_at_ms")):
            patch["feishu_signal_first_sent_at_ms"] = now_ms
    return merge_signal_extra(record_or_data, patch)


def send_signal_notification(
    record_or_data: Any,
    *,
    send_interactive: SendInteractive | None,
    signal_chat_id: str,
    console_base_url: str = "",
) -> dict[str, Any]:
    message_id = to_text(get_signal_extra(record_or_data).get("feishu_signal_message_id"))
    notify_key = _notification_key("new", record_or_data)
    if message_id and to_text(get_signal_extra(record_or_data).get("feishu_signal_notify_key")) == notify_key:
        return {"success": True, "skipped": True, "message_id": message_id, "extra_patch": {}}
    if not callable(send_interactive) or not signal_chat_id:
        return {"success": False, "skipped": True, "message_id": message_id, "extra_patch": {}}

    card = build_signal_notification_card(record_or_data, console_base_url=console_base_url)
    environment = _signal_broker_mode(record_or_data)
    result = dict(send_interactive(card, signal_chat_id, environment) or {})
    return {
        **result,
        "message_id": to_text(result.get("message_id") or message_id),
        "extra_patch": _build_notification_patch(
            record_or_data,
            action="new",
            result=result,
            message_id=message_id,
            notify_key=notify_key,
            include_first_sent_at=True,
        ),
    }


def build_signal_status_card(record_or_data: Any, *, message: str = "", console_base_url: str = "") -> dict[str, Any]:
    extra = get_signal_extra(record_or_data)
    status = _effective_signal_status(record_or_data, extra)
    meta = _status_meta(status)
    symbol = to_text(record_value(record_or_data, "symbol") or record_value(record_or_data, "signal_id") or "SIGNAL")
    direction = to_text(record_value(record_or_data, "direction")).lower()
    environment = _signal_broker_mode(record_or_data, extra)
    data_environment = _signal_data_environment(record_or_data, extra)
    broker_badge = _broker_badge(environment)
    signal_id = to_text(record_value(record_or_data, "signal_id") or record_value(record_or_data, "id"))
    needs_reconfirm = _reconfirm_required(record_or_data)

    direction_text = {"long": "做多", "short": "做空"}.get(direction, direction or "-")
    body_lines = [
        f"**状态**: {meta['text']}",
        f"**信号ID**: {signal_id or '-'}",
        f"**方向**: {direction_text}",
        f"**Broker**: {broker_badge}",
        f"**数据**: {_data_badge(data_environment)}",
        f"**仓位**: {_format_quantity(record_value(record_or_data, 'shares'))}",
    ]
    body_lines.extend(_price_plan_lines(record_or_data))
    body_lines.extend(_planned_pnl_lines(record_or_data))
    body_lines.extend(_profit_space_lines(record_or_data))
    body_lines.extend(_strategy_capacity_lines(record_or_data))
    body_lines.extend(_buying_power_lines(record_or_data))
    body_lines.extend(_tv_context_lines(record_or_data))
    body_lines.extend(_followup_lines(record_or_data))
    if message:
        body_lines.append(f"**说明**: {message}")
    status_reason = _status_reason(record_or_data)
    if status_reason:
        body_lines.append(f"**原因**: {status_reason}")
    body_lines.extend(_signal_cancel_expiry_lines(record_or_data, status=status, status_reason=status_reason))
    body_lines.append(f"**时间**: {to_text(record_value(record_or_data, 'us_time') or '-')}")

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(body_lines)},
    ]
    if status == "awaiting_confirm" and signal_id:
        elements.append({"tag": "hr"})
        elements.extend(
            _confirmation_action_elements(
                console_base_url,
                environment=environment,
                data_environment=data_environment,
                signal_id=signal_id,
            )
        )
    buttons = _view_buttons(
        console_base_url,
        environment=environment,
        data_environment=data_environment,
        signal_id=signal_id,
        record_or_data=record_or_data,
    )
    if buttons:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": buttons,
                },
            ]
        )
    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"{'🔁 信号已更新，需重新确认' if needs_reconfirm else (meta['emoji'] + ' ' + meta['text'])}"
                    f" · {broker_badge} · {symbol} · {to_text(record_value(record_or_data, 'us_time') or '')}"
                ),
            },
            "template": meta["template"],
        },
        "elements": elements,
    }


def sync_signal_status_notification(
    record_or_data: Any,
    *,
    action: str,
    message: str,
    send_interactive: SendInteractive | None = None,
    signal_chat_id: str = "",
    update_interactive: UpdateInteractive | None,
    console_base_url: str = "",
) -> dict[str, Any]:
    extra = get_signal_extra(record_or_data)
    message_id = to_text(extra.get("feishu_signal_message_id"))
    if not message_id and (not callable(send_interactive) or not signal_chat_id):
        return {"success": False, "skipped": True, "message_id": message_id, "extra_patch": {}}
    if message_id and not callable(update_interactive) and not callable(send_interactive):
        return {"success": False, "skipped": True, "message_id": message_id, "extra_patch": {}}

    card = build_signal_status_card(record_or_data, message=message, console_base_url=console_base_url)
    environment = _signal_broker_mode(record_or_data, extra)
    if message_id and callable(update_interactive):
        result = dict(update_interactive(message_id, card, environment) or {})
    else:
        result = dict(send_interactive(card, signal_chat_id, environment) or {}) if callable(send_interactive) else {}
    notify_key = _notification_key(action, record_or_data)
    merged_extra = _build_notification_patch(
        record_or_data,
        action=action,
        result=result,
        message_id=message_id,
        notify_key=notify_key,
    )
    return {
        **result,
        "message_id": to_text(result.get("message_id") or message_id),
        "extra_patch": merged_extra,
    }


def apply_signal_status_notification(
    pb: Any,
    *,
    signal_row: dict[str, Any],
    status: str,
    message: str,
    notifier: SignalStatusNotifier | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_row = dict(signal_row or {})
    if not callable(notifier):
        return {}, current_row

    extra = get_signal_extra(current_row)
    current_message_id = to_text(extra.get("feishu_signal_message_id"))
    options = {
        "message": to_text(message),
        "message_id": current_message_id,
        "messageId": current_message_id,
    }
    try:
        result = dict(notifier(to_text(status), current_row, options) or {})
    except Exception:
        return {}, current_row

    next_message_id = to_text(first_defined(result.get("message_id"), result.get("messageId")))
    if not bool(result.get("success") or result.get("ok")):
        return result, current_row
    if not next_message_id or next_message_id == current_message_id:
        return result, current_row

    patch = {
        "extra": merge_signal_extra(
            current_row,
            {
                "feishu_signal_message_id": next_message_id,
                "feishu_signal_card_version": 1,
            },
        )
    }
    updated_row = pb.update_record("ibkr_signals", to_text(current_row.get("id")), patch)
    return result, dict(updated_row) if isinstance(updated_row, dict) else {**current_row, **patch}
