from __future__ import annotations

import time
from urllib.parse import urlencode
from typing import Any, Callable

from ibkr_api.orders.values import first_defined, to_float, to_text
from ibkr_api.signals.values import get_signal_extra, merge_signal_extra, record_value


SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
SignalStatusNotifier = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]

_SIGNAL_STATUS_META = {
    "awaiting_confirm": {"emoji": "🔔", "text": "待确认", "template": "orange"},
    "pending": {"emoji": "✅", "text": "已确认", "template": "green"},
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


def _status_reason(record_or_data: Any) -> str:
    extra = get_signal_extra(record_or_data)
    return to_text(
        extra.get("status_reason")
        or extra.get("initial_status_reason")
        or extra.get("expired_reason")
        or record_value(record_or_data, "note")
    )


def _signals_page_url(console_base_url: str, environment: str) -> str:
    base = str(console_base_url or "").rstrip("/")
    if not base:
        return ""
    runtime_environment = to_text(environment) or "live"
    return f"{base}/ibkr_signals.html?environment={runtime_environment}"


def _page_url(console_base_url: str, path: str, **params: Any) -> str:
    base = str(console_base_url or "").rstrip("/")
    if not base:
        return ""
    query = urlencode({key: value for key, value in params.items() if value not in {None, ""}})
    url = f"{base}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _view_buttons(console_base_url: str, *, environment: str, signal_id: str) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    signals_url = _page_url(
        console_base_url,
        "ibkr_signals.html",
        environment=environment,
        signal_id=signal_id,
    )
    orders_url = _page_url(
        console_base_url,
        "orders.html",
        environment=environment,
        signal_id=signal_id,
    )
    for label, url in (
        ("查看 Signals", signals_url),
        ("查看 Orders", orders_url),
    ):
        if not url:
            continue
        buttons.append(
            {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": label},
                "multi_url": {
                    "url": url,
                    "pc_url": url,
                    "ios_url": url,
                    "android_url": url,
                },
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


def build_signal_notification_card(record_or_data: Any, *, console_base_url: str = "") -> dict[str, Any]:
    status = to_text(record_value(record_or_data, "status")).lower() or "pending"
    symbol = to_text(record_value(record_or_data, "symbol") or record_value(record_or_data, "signal_id") or "SIGNAL")
    direction = to_text(record_value(record_or_data, "direction")).lower()
    environment = to_text(record_value(record_or_data, "environment") or "live")
    signal_id = to_text(record_value(record_or_data, "signal_id") or record_value(record_or_data, "id"))
    status_reason = _status_reason(record_or_data)
    extra = get_signal_extra(record_or_data)

    direction_text = {"long": "做多", "short": "做空"}.get(direction, direction or "-")
    body_lines = [
        f"**信号ID**: {signal_id or '-'}",
        f"**方向**: {direction_text}",
        f"**环境**: {environment}",
        f"**入场 / 止盈 / 止损**: {_format_price(record_value(record_or_data, 'entry'))} / {_format_price(record_value(record_or_data, 'take_profit'))} / {_format_price(record_value(record_or_data, 'stop_loss'))}",
        f"**仓位 / 风报比**: {_format_quantity(record_value(record_or_data, 'shares'))} / {to_text(record_value(record_or_data, 'rr') or '-')}",
    ]
    body_lines.extend(_source_lines(record_or_data))
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
        elements.append(
            {
                "tag": "column_set",
                "horizontal_spacing": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "type": "primary",
                                "width": "fill",
                                "text": {"tag": "plain_text", "content": "✅ 确认"},
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "action": "confirm",
                                            "signal_id": signal_id,
                                            "environment": environment,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "type": "danger",
                                "width": "fill",
                                "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "action": "reject",
                                            "signal_id": signal_id,
                                            "environment": environment,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "⚙️ **自动确认** · 信号已提交，等待执行",
            }
        )

    buttons = _view_buttons(console_base_url, environment=environment, signal_id=signal_id)
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
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"{'🔔 新交易信号' if status == 'awaiting_confirm' else '⚙️ 自动确认'}"
                    f" · {symbol} · {to_text(record_value(record_or_data, 'us_time') or '')}"
                ),
            },
            "template": "green" if direction == "long" else "red",
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
    }


def _notification_key(action: str, record_or_data: Any) -> str:
    return ":".join(
        [
            "signal_notify_v1",
            to_text(action),
            to_text(record_value(record_or_data, "signal_id") or record_value(record_or_data, "id")),
            to_text(record_value(record_or_data, "status")),
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
        "feishu_signal_notify_last_status": to_text(record_value(record_or_data, "status")),
        "feishu_signal_notify_last_result": "success" if success else "failed",
        "feishu_signal_notify_last_at_ms": now_ms,
        "feishu_signal_notify_error": "" if success else to_text(result.get("error") or "unknown_error"),
    }
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
    environment = to_text(record_value(record_or_data, "environment") or "live")
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
    status = to_text(record_value(record_or_data, "status")).lower() or "pending"
    meta = _status_meta(status)
    symbol = to_text(record_value(record_or_data, "symbol") or record_value(record_or_data, "signal_id") or "SIGNAL")
    direction = to_text(record_value(record_or_data, "direction")).lower()
    environment = to_text(record_value(record_or_data, "environment") or "live")
    extra = get_signal_extra(record_or_data)

    direction_text = {"long": "做多", "short": "做空"}.get(direction, direction or "-")
    body_lines = [
        f"**状态**: {meta['text']}",
        f"**信号ID**: {to_text(record_value(record_or_data, 'signal_id') or record_value(record_or_data, 'id') or '-')}",
        f"**方向**: {direction_text}",
        f"**环境**: {environment}",
        f"**入场 / 止盈 / 止损**: {_format_price(record_value(record_or_data, 'entry'))} / {_format_price(record_value(record_or_data, 'take_profit'))} / {_format_price(record_or_data and record_value(record_or_data, 'stop_loss'))}",
        f"**仓位**: {_format_quantity(record_value(record_or_data, 'shares'))}",
    ]
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
    signals_url = _signals_page_url(console_base_url, environment)
    if signals_url:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "default",
                            "text": {"tag": "plain_text", "content": "查看 Signals"},
                            "multi_url": {
                                "url": signals_url,
                                "pc_url": signals_url,
                                "ios_url": signals_url,
                                "android_url": signals_url,
                            },
                        }
                    ],
                },
            ]
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"{meta['emoji']} {meta['text']} · {symbol} · {to_text(record_value(record_or_data, 'us_time') or '')}",
            },
            "template": meta["template"],
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
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
    environment = to_text(record_value(record_or_data, "environment") or "live")
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
