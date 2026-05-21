from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import urlencode

from ibkr_api.orders.group_common import (
    dedupe_order_rows,
    load_order_action_context,
    normalize_order_row,
    pick_primary_order_row,
)
from ibkr_api.orders.values import ORDER_STATUS_TEXT_MAP, ensure_object, first_defined, to_float, to_text

ORDER_NOTIFY_STATUSES = {
    "Submitted",
    "Filled",
    "Canceled",
    "Closed",
    "Rejected",
    "Expired",
    "protection_incomplete",
}

ORDER_ROLE_LABELS = {
    "entry": "入场",
    "take_profit": "止盈",
    "stop_loss": "止损",
    "repair_sl": "修复止损",
    "close": "平仓",
}

_ORDER_GROUP_NOTIFY_LOCKS: dict[str, threading.Lock] = {}
_ORDER_GROUP_NOTIFY_LOCKS_GUARD = threading.Lock()


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


def _order_group_notify_lock(environment: str, group_id: str) -> threading.Lock:
    lock_key = f"{to_text(environment) or 'live'}:{to_text(group_id) or '-'}"
    with _ORDER_GROUP_NOTIFY_LOCKS_GUARD:
        lock = _ORDER_GROUP_NOTIFY_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _ORDER_GROUP_NOTIFY_LOCKS[lock_key] = lock
        return lock


def _order_group_notify_identity(order_record: dict[str, Any], environment: str) -> str:
    normalized = normalize_order_row(order_record)
    return to_text(
        normalized.get("trade_group_id")
        or normalized.get("entry_order_unique_id")
        or normalized.get("unique_id")
        or _record_or_extra_value(
            order_record,
            "trade_group_id",
            "entry_order_unique_id",
            "unique_id",
            "broker_order_id",
            "order_id",
            "id",
        )
    )


def _has_entry_order_row(rows: list[dict[str, Any]]) -> bool:
    return any(normalize_order_row(row).get("role") == "entry" for row in rows or [])


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


def _status_text(status: Any) -> str:
    text = to_text(status)
    return ORDER_STATUS_TEXT_MAP.get(text, text or "-")


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
        broker_mode=environment,
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
                broker_mode=environment,
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


def _escape_filter_string(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _role_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    normalized = normalize_order_row(row)
    role_order = {
        "entry": 0,
        "close": 1,
        "take_profit": 2,
        "stop_loss": 3,
        "repair_sl": 4,
    }.get(normalized.get("role") or "", 9)
    return role_order, normalized.get("unique_id") or normalized.get("id") or ""


def _order_group_context(
    pb: Any,
    order_record: dict[str, Any],
    *,
    environment: str,
    related_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    seed_rows = dedupe_order_rows([order_record, *(related_rows or [])])
    primary_seed = pick_primary_order_row(seed_rows, order_record) or order_record
    if related_rows:
        return {
            "primary_row": primary_seed,
            "related_rows": sorted(seed_rows, key=_role_sort_key),
            "trade_group_id": normalize_order_row(primary_seed).get("trade_group_id") or "",
        }

    target_id = to_text(
        _record_or_extra_value(
            primary_seed,
            "trade_group_id",
            "entry_order_unique_id",
            "unique_id",
            "broker_order_id",
            "order_id",
            "id",
        )
    )
    if not target_id:
        return {
            "primary_row": primary_seed,
            "related_rows": sorted(seed_rows, key=_role_sort_key),
            "trade_group_id": normalize_order_row(primary_seed).get("trade_group_id") or "",
        }

    try:
        context = load_order_action_context(
            pb,
            payload={"id": target_id, "broker_mode": environment},
            environment=environment,
            escape_filter_string=_escape_filter_string,
        )
    except Exception:
        context = {}

    rows = dedupe_order_rows((context or {}).get("related_rows") or seed_rows)
    primary = (context or {}).get("primary_row") or pick_primary_order_row(rows, primary_seed) or primary_seed
    primary_id = normalize_order_row(primary).get("id")
    if primary_id and all(normalize_order_row(row).get("id") != primary_id for row in rows):
        rows = dedupe_order_rows([primary, *rows])
    return {
        "primary_row": primary,
        "related_rows": sorted(rows or [primary_seed], key=_role_sort_key),
        "trade_group_id": to_text((context or {}).get("trade_group_id") or normalize_order_row(primary).get("trade_group_id")),
    }


def _group_status(rows: list[dict[str, Any]]) -> str:
    normalized_rows = [normalize_order_row(row) for row in rows or []]
    primary = normalize_order_row(pick_primary_order_row(rows) or (rows[0] if rows else {}))
    primary_status = primary.get("status") or ""
    if primary_status == "Closed":
        return "Closed"
    if any(
        row.get("role") in {"take_profit", "stop_loss", "close", "repair_sl"} and row.get("status") in {"Filled", "Closed"}
        for row in normalized_rows
    ):
        return "Closed"
    if primary_status in {"Filled", "Rejected", "Expired"}:
        return primary_status
    statuses = {row.get("status") for row in normalized_rows if row.get("status")}
    if statuses and statuses.issubset({"Canceled", "Closed"}):
        return "Canceled"
    if "Submitted" in statuses or primary_status == "Submitted":
        return "Submitted"
    if any(to_text(row.get("status")).lower() == "protection_incomplete" for row in normalized_rows):
        return "protection_incomplete"
    return primary_status or (normalized_rows[0].get("status") if normalized_rows else "")


def _group_notify_key(rows: list[dict[str, Any]], *, action: str, group_status: str, environment: str, trade_group_id: str) -> str:
    digest_rows = []
    for row in sorted(rows or [], key=_role_sort_key):
        normalized = normalize_order_row(row)
        digest_rows.append(
            {
                "unique_id": normalized.get("unique_id") or normalized.get("id"),
                "role": normalized.get("role"),
                "status": normalized.get("status"),
                "broker_order_id": normalized.get("broker_order_id"),
                "filled_qty": _format_quantity(_record_or_extra_value(row, "filled_qty")),
                "fill_price": _format_price(_record_or_extra_value(row, "fill_price", "avg_price", "avg_fill_price")),
            }
        )
    digest = hashlib.sha1(json.dumps(digest_rows, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:16]
    action_key = to_text(action or group_status).lower() or "status"
    return f"order_group_notify_v1:{environment}:{trade_group_id}:{action_key}:{digest}"


def _leg_line(row: dict[str, Any]) -> str:
    normalized = normalize_order_row(row)
    role = normalized.get("role") or to_text(_record_or_extra_value(row, "order_type")) or "order"
    label = ORDER_ROLE_LABELS.get(role, role or "订单")
    status = normalized.get("status") or "-"
    order_id = normalized.get("broker_order_id") or to_text(_record_or_extra_value(row, "order_id", "broker_order_id"))
    quantity = _format_quantity(_record_or_extra_value(row, "quantity"))
    filled = _format_quantity(_record_or_extra_value(row, "filled_qty"))
    price = _format_price(
        _record_or_extra_value(
            row,
            "limit_price",
            "price",
            "fill_price",
            "avg_price",
            "avg_fill_price",
        )
    )
    return f"**{label}**: {_status_text(status)} · ID {order_id or '-'} · {filled}/{quantity} · {price}"


def _group_status_template(status: str) -> str:
    if status in {"Filled", "Closed"}:
        return "green"
    if status in {"Canceled", "Expired"}:
        return "grey"
    if status in {"Rejected", "protection_incomplete"}:
        return "orange"
    return "blue"


def build_order_group_status_card(
    order_rows: list[dict[str, Any]] | None,
    *,
    status: str = "",
    message: str = "",
    console_base_url: str = "",
) -> dict[str, Any]:
    rows = sorted(dedupe_order_rows(order_rows or []), key=_role_sort_key)
    primary = pick_primary_order_row(rows, rows[0] if rows else {}) or {}
    environment = to_text(_record_or_extra_value(primary, "environment")) or "live"
    broker_badge = _broker_badge(environment)
    symbol = to_text(_record_or_extra_value(primary, "symbol")).upper() or "ORDER"
    trade_group_id = to_text(_record_or_extra_value(primary, "trade_group_id", "entry_order_unique_id", "unique_id"))
    signal_id = to_text(_record_or_extra_value(primary, "signal_id"))
    resolved_status = to_text(status or _group_status(rows) or _record_or_extra_value(primary, "status", "order_status", "current_status"))
    status_text = _status_text(resolved_status)

    body_lines = [
        f"**状态**: {status_text}",
        f"**Symbol**: {symbol}",
        f"**Broker**: {broker_badge}",
        f"**信号ID / 交易组**: {signal_id or '-'} / {trade_group_id or '-'}",
    ]
    if rows:
        body_lines.extend(_leg_line(row) for row in rows)
    else:
        body_lines.append("**订单**: -")
    if message:
        body_lines.append(f"**说明**: {message}")
    body_lines.append(f"**时间**: {to_text(_record_or_extra_value(primary, 'us_time', 'order_time', 'fill_time')) or '-'}")

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(body_lines)}]
    buttons = order_view_buttons(console_base_url, primary)
    if buttons:
        elements.extend([{"tag": "hr"}, {"tag": "action", "actions": buttons}])

    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📦 订单组 · {broker_badge} · {status_text} · {symbol}",
            },
            "template": _group_status_template(resolved_status),
        },
        "elements": elements,
    }


def _notification_patch(
    primary_row: dict[str, Any],
    *,
    action: str,
    group_status: str,
    notify_key: str,
    result: dict[str, Any],
    message_id: str,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    success = bool(result.get("success") or result.get("ok"))
    patch: dict[str, Any] = {
        "feishu_order_notify_key": notify_key,
        "feishu_order_notify_last_action": to_text(action),
        "feishu_order_notify_last_status": group_status,
        "feishu_order_notify_last_result": "success" if success else "failed",
        "feishu_order_notify_last_at_ms": now_ms,
        "feishu_order_notify_error": "" if success else to_text(result.get("error") or "unknown_error"),
    }
    for source_key, target_key in (
        ("http_status", "feishu_order_notify_http_status"),
        ("api_code", "feishu_order_notify_api_code"),
        ("api_message", "feishu_order_notify_api_message"),
        ("response_body", "feishu_order_notify_response_body"),
    ):
        if result.get(source_key) not in (None, ""):
            patch[target_key] = result.get(source_key)
    resolved_message_id = to_text(result.get("message_id") or message_id)
    if resolved_message_id:
        patch["feishu_order_message_id"] = resolved_message_id
        patch["feishu_order_card_version"] = 2
    return {**ensure_object(primary_row.get("extra")), **patch}


def _apply_order_notification_patch(pb: Any, primary_row: dict[str, Any], extra_patch: dict[str, Any]) -> None:
    record_id = to_text(primary_row.get("id"))
    if not record_id or not extra_patch:
        return
    try:
        pb.update_record("orders", record_id, {"extra": extra_patch})
    except Exception:
        return


def sync_order_status_notification(
    pb: Any,
    order_record: dict[str, Any],
    *,
    action: str = "",
    message: str = "",
    message_id: str = "",
    send_interactive: Any = None,
    update_interactive: Any = None,
    order_chat_id: str = "",
    console_base_url: str = "",
    related_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(order_record, dict) or not order_record:
        return {"success": False, "skipped": True, "message_id": to_text(message_id), "reason": "missing_order"}

    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    group_identity = _order_group_notify_identity(order_record, environment)
    notify_lock = _order_group_notify_lock(environment, group_identity or to_text(order_record.get("id")))
    with notify_lock:
        return _sync_order_status_notification_locked(
            pb,
            order_record,
            action=action,
            message=message,
            message_id=message_id,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            order_chat_id=order_chat_id,
            console_base_url=console_base_url,
            related_rows=related_rows,
            environment=environment,
        )


def _sync_order_status_notification_locked(
    pb: Any,
    order_record: dict[str, Any],
    *,
    action: str = "",
    message: str = "",
    message_id: str = "",
    send_interactive: Any = None,
    update_interactive: Any = None,
    order_chat_id: str = "",
    console_base_url: str = "",
    related_rows: list[dict[str, Any]] | None = None,
    environment: str = "live",
) -> dict[str, Any]:
    context = _order_group_context(pb, order_record, environment=environment, related_rows=related_rows)
    rows = context.get("related_rows") or [order_record]
    primary = context.get("primary_row") or pick_primary_order_row(rows, order_record) or order_record
    group_status = _group_status(rows)
    current_extra = ensure_object(primary.get("extra"))
    current_message_id = to_text(message_id or current_extra.get("feishu_order_message_id"))
    trade_group_id = to_text(context.get("trade_group_id") or _record_or_extra_value(primary, "trade_group_id", "entry_order_unique_id", "unique_id"))
    notify_key = _group_notify_key(
        rows,
        action=action or group_status,
        group_status=group_status,
        environment=environment,
        trade_group_id=trade_group_id or to_text(primary.get("id")),
    )

    if not current_message_id and not _has_entry_order_row(rows):
        incoming_role = normalize_order_row(order_record).get("role")
        if incoming_role and incoming_role != "entry":
            return {
                "success": False,
                "skipped": True,
                "message_id": "",
                "notify_key": notify_key,
                "group_status": group_status,
                "reason": "waiting_for_primary_order",
                "extra_patch": current_extra,
            }

    if current_extra.get("feishu_order_notify_key") == notify_key and current_extra.get("feishu_order_notify_last_result") == "success":
        return {
            "success": True,
            "skipped": True,
            "message_id": current_message_id,
            "notify_key": notify_key,
            "reason": "already_notified",
            "extra_patch": current_extra,
        }

    if not current_message_id and group_status not in ORDER_NOTIFY_STATUSES:
        return {
            "success": False,
            "skipped": True,
            "message_id": "",
            "notify_key": notify_key,
            "reason": "status_not_notifiable",
            "extra_patch": current_extra,
        }
    if current_message_id and not callable(update_interactive):
        return {
            "success": False,
            "skipped": True,
            "message_id": current_message_id,
            "notify_key": notify_key,
            "reason": "missing_update_interactive",
            "extra_patch": current_extra,
        }
    if not current_message_id and (not callable(send_interactive) or not order_chat_id):
        return {
            "success": False,
            "skipped": True,
            "message_id": "",
            "notify_key": notify_key,
            "reason": "missing_send_target",
            "extra_patch": current_extra,
        }

    card = build_order_group_status_card(rows, status=group_status, message=message, console_base_url=console_base_url)
    try:
        if current_message_id:
            result = dict(update_interactive(current_message_id, card, environment) or {})
        else:
            result = dict(send_interactive(card, order_chat_id, environment) or {})
    except Exception as exc:
        result = {"success": False, "message_id": current_message_id, "error": str(exc)}

    extra_patch = _notification_patch(
        primary,
        action=action or group_status,
        group_status=group_status,
        notify_key=notify_key,
        result=result,
        message_id=current_message_id,
    )
    _apply_order_notification_patch(pb, primary, extra_patch)
    return {
        **result,
        "message_id": to_text(result.get("message_id") or current_message_id),
        "notify_key": notify_key,
        "group_status": group_status,
        "extra_patch": extra_patch,
    }


def build_order_status_card(order_record: Any, *, status: str = "", message: str = "", console_base_url: str = "") -> dict[str, Any]:
    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    broker_badge = _broker_badge(environment)
    symbol = to_text(_record_or_extra_value(order_record, "symbol")).upper() or "ORDER"
    resolved_status = to_text(status or _record_or_extra_value(order_record, "status", "order_status", "current_status"))
    status_text = _status_text(resolved_status)
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


__all__ = [
    "build_order_group_status_card",
    "build_order_status_card",
    "order_view_buttons",
    "sync_order_status_notification",
]
