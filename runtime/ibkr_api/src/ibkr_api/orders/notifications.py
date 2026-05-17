from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ibkr_api.orders.values import ORDER_STATUS_TEXT_MAP, ensure_object, first_defined, to_float, to_text


def _record_value(record_or_data: Any, field_name: str, default: Any = None) -> Any:
    if record_or_data is None:
        return default
    if isinstance(record_or_data, dict):
        return record_or_data.get(field_name, default)
    getter = getattr(record_or_data, "get", None)
    if callable(getter):
        value = getter(field_name)
        return default if value is None else value
    return getattr(record_or_data, field_name, default)


def _extra(record_or_data: Any) -> dict[str, Any]:
    return ensure_object(_record_value(record_or_data, "extra"))


def _record_or_extra_value(record_or_data: Any, *fields: str) -> Any:
    extra = _extra(record_or_data)
    for field in fields:
        value = first_defined(_record_value(record_or_data, field), extra.get(field))
        if value not in (None, ""):
            return value
    return ""


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


def _broker_badge(environment: Any) -> str:
    normalized = to_text(environment or "live").lower()
    if normalized in {"live", "paper"}:
        return f"Broker {normalized.upper()}"
    return normalized.upper() if normalized else "Broker LIVE"


def _record_date(record_or_data: Any) -> str:
    explicit = to_text(_record_or_extra_value(record_or_data, "date", "market_date", "trade_date", "backtest_date"))
    if explicit:
        return explicit[:10]
    for field in ("us_time", "order_time", "fill_time", "created", "updated"):
        text = to_text(_record_or_extra_value(record_or_data, field))
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[:10]
    return ""


def _page_url(console_base_url: str, path: str, **params: Any) -> str:
    base = str(console_base_url or "").rstrip("/")
    if not base:
        return ""
    query = urlencode({key: value for key, value in params.items() if value not in {None, ""}})
    url = f"{base}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _button_url(url: str) -> dict[str, str]:
    return {
        "url": url,
        "pc_url": url,
        "ios_url": url,
        "android_url": url,
    }


def _button(label: str, url: str) -> dict[str, Any] | None:
    if not url:
        return None
    return {
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": label},
        "multi_url": _button_url(url),
    }


def _lifecycle_url(console_base_url: str, order_record: Any, *, environment: str) -> str:
    symbol = to_text(_record_or_extra_value(order_record, "symbol")).upper()
    signal_id = to_text(_record_or_extra_value(order_record, "signal_id"))
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    order_id = to_text(_record_or_extra_value(order_record, "order_id", "broker_order_id", "ib_order_id", "unique_id"))
    if not any((symbol, signal_id, trade_group_id, order_id)):
        return ""
    return _page_url(
        console_base_url,
        "ibkr_lifecycle_flow.html",
        mode="auto",
        environment=environment,
        date=_record_date(order_record),
        symbol=symbol,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        order_id=order_id,
    )


def order_view_buttons(console_base_url: str, order_record: Any) -> list[dict[str, Any]]:
    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    symbol = to_text(_record_or_extra_value(order_record, "symbol")).upper()
    signal_id = to_text(_record_or_extra_value(order_record, "signal_id"))
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    order_id = to_text(_record_or_extra_value(order_record, "order_id", "broker_order_id", "ib_order_id", "unique_id"))
    urls = (
        (
            "查看 Orders",
            _page_url(
                console_base_url,
                "orders.html",
                environment=environment,
                symbol=symbol,
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                order_id=order_id,
            ),
        ),
        (
            "查看事件流",
            _lifecycle_url(console_base_url, order_record, environment=environment),
        ),
    )
    return [button for label, url in urls if (button := _button(label, url))]


def build_order_status_card(order_record: Any, *, status: str = "", message: str = "", console_base_url: str = "") -> dict[str, Any]:
    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    broker_badge = _broker_badge(environment)
    symbol = to_text(_record_or_extra_value(order_record, "symbol")).upper() or "ORDER"
    resolved_status = to_text(status or _record_or_extra_value(order_record, "status", "order_status", "current_status"))
    status_text = ORDER_STATUS_TEXT_MAP.get(resolved_status, resolved_status or "-")
    order_id = to_text(_record_or_extra_value(order_record, "order_id", "broker_order_id", "ib_order_id"))
    unique_id = to_text(_record_or_extra_value(order_record, "unique_id", "id"))
    signal_id = to_text(_record_or_extra_value(order_record, "signal_id"))
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    role = to_text(_record_or_extra_value(order_record, "role"))
    order_type = to_text(_record_or_extra_value(order_record, "order_type"))

    body_lines = [
        f"**状态**: {status_text}",
        f"**Symbol**: {symbol}",
        f"**Broker**: {broker_badge}",
        f"**订单ID / UniqueID**: {order_id or '-'} / {unique_id or '-'}",
        f"**信号ID / 交易组**: {signal_id or '-'} / {trade_group_id or '-'}",
        f"**角色 / 类型**: {role or '-'} / {order_type or '-'}",
        f"**数量 / 已成交**: {_format_quantity(_record_or_extra_value(order_record, 'quantity'))} / {_format_quantity(_record_or_extra_value(order_record, 'filled_qty'))}",
        f"**价格 / 止盈 / 止损**: {_format_price(_record_or_extra_value(order_record, 'price', 'limit_price', 'avg_price', 'avg_fill_price'))} / {_format_price(_record_or_extra_value(order_record, 'tp_price', 'take_profit'))} / {_format_price(_record_or_extra_value(order_record, 'sl_price', 'stop_loss'))}",
    ]
    if message:
        body_lines.append(f"**说明**: {message}")
    body_lines.append(f"**时间**: {to_text(_record_or_extra_value(order_record, 'us_time', 'order_time', 'fill_time')) or '-'}")

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(body_lines)}]
    buttons = order_view_buttons(console_base_url, order_record)
    if buttons:
        elements.extend([{"tag": "hr"}, {"tag": "action", "actions": buttons}])

    template = "green" if resolved_status in {"Filled", "Closed"} else ("grey" if resolved_status == "Canceled" else "blue")
    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📦 订单状态 · {broker_badge} · {status_text} · {symbol}",
            },
            "template": template,
        },
        "elements": elements,
    }


__all__ = ["build_order_status_card", "order_view_buttons"]
