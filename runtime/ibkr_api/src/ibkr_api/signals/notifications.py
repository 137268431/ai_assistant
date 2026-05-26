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
    "protected_active": {"emoji": "🛡️", "text": "保护单已生效", "template": "blue"},
    "protection_incomplete": {"emoji": "⚠️", "text": "保护单不完整", "template": "orange"},
    "executed": {"emoji": "🚀", "text": "已执行", "template": "blue"},
    "rejected": {"emoji": "❌", "text": "已拒绝", "template": "red"},
    "expired": {"emoji": "⏰", "text": "已过期", "template": "grey"},
    "closed": {"emoji": "🧾", "text": "已平仓", "template": "grey"},
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
    final_statuses = {
        "submitted",
        "protected_active",
        "protection_incomplete",
        "executed",
        "rejected",
        "expired",
        "closed",
    }
    for index, mode in enumerate(("paper", "live")):
        payload = execution_by_mode.get(mode)
        if not isinstance(payload, dict):
            continue
        status = to_text(payload.get("status")).lower()
        score = 2 if status in final_statuses else 1 if status else 0
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


def _format_number(value: Any, *, digits: int = 2) -> str:
    parsed = _metric_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:.{digits}f}"


def _has_metric_number(value: Any) -> bool:
    return _metric_float(value) is not None


def _positive_price(record_or_data: Any, *fields: str) -> float | None:
    extra = get_signal_extra(record_or_data)
    for field in fields:
        for value in (record_value(record_or_data, field), extra.get(field)):
            parsed = to_float(value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _price_plan_lines(record_or_data: Any) -> list[str]:
    extra = get_signal_extra(record_or_data)
    reference_price = _positive_price(
        record_or_data,
        "pre_submit_reference_price",
        "reference_price",
        "last_price",
    )
    entry_limit = _positive_price(
        record_or_data,
        "limit_price",
        "entry_limit_price",
        "entry",
    )
    actual_fill = _positive_price(
        record_or_data,
        "executed_price",
        "entry_fill_price",
        "fill_price",
        "avg_fill_price",
        "avgPrice",
    )
    reference_source = to_text(extra.get("pre_submit_reference_source") or extra.get("reference_source"))
    reference_suffix = f" ({reference_source})" if reference_price is not None and reference_source else ""
    lines = [
        f"**参考价**: {_format_price(reference_price)}{reference_suffix}",
        f"**入场限价 / 止盈 / 止损**: {_format_price(entry_limit)} / {_format_price(record_value(record_or_data, 'take_profit'))} / {_format_price(record_value(record_or_data, 'stop_loss'))}",
    ]
    if actual_fill is not None:
        lines.append(f"**实际成交价**: {_format_price(actual_fill)}")
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


def _record_or_extra_value(record_or_data: Any, *fields: str) -> Any:
    extra = get_signal_extra(record_or_data)
    for field in fields:
        value = first_defined(record_value(record_or_data, field), extra.get(field))
        if value not in (None, ""):
            return value
    return ""


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
    for label, url in (
        ("查看 Signals", signals_url),
        ("查看 Orders", orders_url),
        ("查看事件流", lifecycle_url),
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


def _reconfirm_required(record_or_data: Any) -> bool:
    extra = get_signal_extra(record_or_data)
    status = _effective_signal_status(record_or_data, extra)
    return status == "awaiting_confirm" and bool(extra.get("followup_requires_reconfirm") or extra.get("confirmation_stale"))


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
    body_lines.extend(_market_metric_lines(record_or_data))
    body_lines.extend(_source_lines(record_or_data))
    body_lines.extend(_followup_lines(record_or_data))
    reason = to_text(record_value(record_or_data, "reason") or extra.get("reason"))
    if reason:
        body_lines.append(f"**原因**: {reason}")
    if status_reason:
        body_lines.append(f"**状态原因**: {status_reason}")
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
                "content": "⚙️ **自动确认** · 信号已提交，等待执行",
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
                    f"{'🔁 信号已更新，需重新确认' if needs_reconfirm else ('🔔 新交易信号' if status == 'awaiting_confirm' else '⚙️ 自动确认')}"
                    f" · {broker_badge} · {symbol} · {to_text(record_value(record_or_data, 'us_time') or '')}"
                ),
            },
            "template": "green" if direction == "long" else "red",
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
    body_lines.extend(_market_metric_lines(record_or_data))
    body_lines.extend(_buying_power_lines(record_or_data))
    body_lines.extend(_followup_lines(record_or_data))
    if message:
        body_lines.append(f"**说明**: {message}")
    status_reason = _status_reason(record_or_data)
    if status_reason:
        body_lines.append(f"**原因**: {status_reason}")
    if extra.get("cancelled_order_ids"):
        body_lines.append(f"**已撤订单**: {', '.join(to_text(item) for item in extra.get('cancelled_order_ids') if to_text(item))}")
    if extra.get("cancel_order_failures"):
        body_lines.append(f"**撤单失败数**: {len(extra.get('cancel_order_failures') or [])}")
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
