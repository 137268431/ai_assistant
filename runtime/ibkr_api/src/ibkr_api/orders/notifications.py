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

TRADE_LEDGER_NON_MATERIAL_CALLBACK_STATUSES = {
    "Init",
    "Submitted",
    "PreSubmitted",
    "ApiPending",
    "PendingSubmit",
}

TRADE_LEDGER_TERMINAL_STATUSES = {
    "Canceled",
    "Cancelled",
    "Rejected",
    "Expired",
    "Inactive",
}

ORDER_ROLE_LABELS = {
    "entry": "入场",
    "take_profit": "止盈",
    "repair_tp": "修复止盈",
    "stop_loss": "止损",
    "repair_sl": "修复止损",
    "tp": "止盈",
    "sl": "止损",
    "close": "平仓",
    "manual_close": "手动平仓",
    "market_close": "手动平仓",
    "close_order": "平仓",
    "reverse_close": "反向平仓",
}

EXIT_REASON_LABELS = {
    "take_profit": "止盈",
    "tp": "止盈",
    "repair_tp": "修复止盈",
    "stop_loss": "止损",
    "sl": "止损",
    "repair_sl": "修复止损",
    "closed_by_stop_loss": "止损",
    "runner_stop": "Runner 止损",
    "force_flat_eod": "EOD 平仓",
    "eod": "EOD 平仓",
    "eod_force_close": "EOD 平仓",
    "order_flow_adverse_delta_exit": "订单流提前平仓",
    "order_flow_full_exit": "订单流风控平仓",
    "order_flow_close_pending": "订单流风控平仓",
    "intraday_harvest_full_exit": "日内收割平仓",
    "intraday_harvest_partial_exit": "日内收割平仓",
    "reverse_signal_close": "反向平仓",
    "closed_by_reverse_signal": "反向平仓",
    "closed_by_reverse_close": "反向平仓",
    "manual_close": "手动平仓",
    "market_close": "手动平仓",
    "positions_close": "手动平仓",
}

EXIT_SOURCE_REASON_LABELS = {
    "eod_force_close": ("force_flat_eod", "EOD 平仓"),
    "order_flow_full_exit": ("order_flow_full_exit", "订单流风控平仓"),
    "intraday_harvest_full_exit": ("intraday_harvest_full_exit", "日内收割平仓"),
    "intraday_harvest_partial_exit": ("intraday_harvest_partial_exit", "日内收割平仓"),
    "reverse_signal_close": ("reverse_signal_close", "反向平仓"),
    "positions_close": ("positions_close", "手动平仓"),
    "manual_close": ("manual_close", "手动平仓"),
}

UNKNOWN_CLOSE_LABEL = "平仓（原因未记录）"

EXIT_ORDER_ROLES = {
    "take_profit",
    "repair_tp",
    "tp",
    "stop_loss",
    "repair_sl",
    "sl",
    "close",
    "manual_close",
    "market_close",
    "close_order",
    "reverse_close",
}

PROTECTION_TP_ROLES = {"take_profit", "repair_tp", "tp"}
PROTECTION_SL_ROLES = {"stop_loss", "repair_sl", "sl"}
PROTECTION_ROLES = PROTECTION_TP_ROLES | PROTECTION_SL_ROLES
ACTIVE_CLOSE_ROLES = {"close", "manual_close", "market_close", "close_order", "reverse_close"}
TERMINAL_ORDER_STATUSES = {"filled", "executed", "canceled", "cancelled", "apicancelled", "closed", "inactive", "rejected", "expired"}
CANCELLED_ORDER_STATUS_KEYS = {"canceled", "cancelled", "apicancelled", "apicanceled"}
GENERIC_ENTRY_REASONS = {
    "order_submitted_by_ibkr_compute",
    "entry_filled_and_protection_submitted",
    "submitted_waiting_fill",
    "tv_direct_ready",
    "tv_direct_submitted",
}

COMMISSION_FIELDS = ("commission", "actual_fill_commission", "ibkr_commission")
PNL_EPSILON = 1e-9
ORDER_FLOW_DEFAULT_EXIT_DELTA_RATIO = 0.18
ORDER_FLOW_DEFAULT_PROFIT_EXIT_R = 0.15

ORDER_GROUP_REASON_LABELS = {
    "order_flow_adverse_delta_exit": "订单流反向 Delta 过强，且利润未达到保护阈值，触发提前平仓",
    "order_flow_full_exit": "订单流风控触发全平",
    "order_flow_close_pending": "订单流风控平仓单仍在等待成交确认",
    "order_flow_adverse_delta_tighten_stop": "订单流反向 Delta 过强，触发收紧止损",
    "intraday_harvest_full_exit": "日内波动收割策略触发全平",
    "intraday_harvest_partial_exit": "日内波动收割策略触发部分平仓",
    "intraday_harvest_tighten_stop": "日内波动收割策略触发收紧止损",
    "protection_missing_after_entry_fill": "入场已成交，但当前没有完整有效的止盈/止损保护单",
    "protection_leg_canceled": "保护单已取消，当前保护不完整",
    "protection_leg_rejected": "保护单被券商拒绝，当前保护不完整",
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


def _format_signed_bps(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    if parsed > 0:
        return f"+{parsed:,.2f}bps"
    if parsed < 0:
        return f"-{abs(parsed):,.2f}bps"
    return "0.00bps"


def _format_signed_price_delta(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    if parsed > 0:
        return f"+${parsed:,.2f}"
    if parsed < 0:
        return f"-${abs(parsed):,.2f}"
    return "$0.00"


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


def _status_key(value: Any) -> str:
    return to_text(value).strip().lower().replace("_", "")


def _row_status_key(row: dict[str, Any]) -> str:
    return _status_key(normalize_order_row(row).get("status") or _record_or_extra_value(row, "status", "order_status", "current_status"))


def _row_is_active(row: dict[str, Any]) -> bool:
    status = _row_status_key(row)
    return bool(status and status not in TERMINAL_ORDER_STATUSES)


def _row_is_filled(row: dict[str, Any]) -> bool:
    normalized = normalize_order_row(row)
    status = _status_key(normalized.get("status"))
    filled_qty = to_float(_record_or_extra_value(row, "filled_qty", "filledQuantity", "filled"))
    return status in {"filled", "executed", "closed"} or (filled_qty is not None and filled_qty > 0)


def _protection_role_family(role: str) -> str:
    if role in PROTECTION_TP_ROLES:
        return "take_profit"
    if role in PROTECTION_SL_ROLES:
        return "stop_loss"
    return ""


def _protection_role_label(role_or_family: str) -> str:
    family = _protection_role_family(role_or_family) or role_or_family
    if family == "take_profit":
        return "止盈"
    if family == "stop_loss":
        return "止损"
    return ORDER_ROLE_LABELS.get(role_or_family, role_or_family or "保护单")


def _protection_state_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(dedupe_order_rows(rows or []), key=_role_sort_key)
    primary = pick_primary_order_row(rows, rows[0] if rows else {}) or {}
    if not primary or not _row_is_filled(primary):
        return {"state": "not_filled", "missing_roles": []}
    if _row_status_key(primary) == "closed":
        return {"state": "closed", "missing_roles": []}

    active_close_rows = [
        row
        for row in rows
        if (normalize_order_row(row).get("role") or "") in ACTIVE_CLOSE_ROLES and _row_is_active(row)
    ]
    if active_close_rows:
        return {"state": "closing", "missing_roles": []}
    if any(
        (normalize_order_row(row).get("role") or "") in EXIT_ORDER_ROLES and _row_is_filled(row)
        for row in rows
    ):
        return {"state": "closed", "missing_roles": []}

    active_roles: set[str] = set()
    terminal_roles: list[dict[str, str]] = []
    tp_already_filled = False
    for row in rows:
        normalized = normalize_order_row(row)
        role = normalized.get("role") or ""
        family = _protection_role_family(role)
        if not family:
            continue
        status = _row_status_key(row)
        if _row_is_active(row):
            active_roles.add(family)
        elif status in TERMINAL_ORDER_STATUSES:
            terminal_roles.append(
                {
                    "role": family,
                    "label": _protection_role_label(family),
                    "status": status,
                    "status_text": _status_text(normalized.get("status")),
                }
            )
        if family == "take_profit" and _row_is_filled(row):
            tp_already_filled = True

    missing_roles: list[str] = []
    if "take_profit" not in active_roles and not tp_already_filled:
        missing_roles.append("take_profit")
    if "stop_loss" not in active_roles:
        missing_roles.append("stop_loss")
    if not missing_roles:
        return {"state": "protected", "missing_roles": [], "active_roles": sorted(active_roles)}
    return {
        "state": "missing_after_fill",
        "missing_roles": missing_roles,
        "active_roles": sorted(active_roles),
        "terminal_roles": terminal_roles,
    }


def _protection_status_line(rows: list[dict[str, Any]]) -> str:
    model = _protection_state_model(rows)
    if model.get("state") != "missing_after_fill":
        return ""
    missing_labels = [_protection_role_label(role) for role in model.get("missing_roles") or []]
    parts = [f"缺少有效{'/'.join(missing_labels)}"]
    terminal_parts: list[str] = []
    for item in model.get("terminal_roles") or []:
        text = f"{item.get('label') or '保护单'}{item.get('status_text') or ''}"
        if text and text not in terminal_parts:
            terminal_parts.append(text)
    if terminal_parts:
        parts.append("；".join(terminal_parts))
    return f"**保护状态**: 异常 - {'；'.join(parts)}"


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
    if _protection_state_model(rows).get("state") == "missing_after_fill":
        return "protection_incomplete"
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


def _group_status_display_text(status: str, rows: list[dict[str, Any]]) -> str:
    protection_model = _protection_state_model(rows)
    if status == "Filled" and protection_model.get("state") == "protected":
        return "入场已成交 · 保护单挂单中"
    return _status_text(status)


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


def _positive_number(record_or_data: Any, *fields: str) -> float | None:
    for field in fields:
        value = to_float(_record_or_extra_value(record_or_data, field))
        if value is not None and value > 0:
            return value
    return None


def _position_entry_price_hint(record_or_data: Any) -> float | None:
    explicit_fields = (
        "entry_price_for_pnl",
        "position_entry_price",
        "position_avg_cost",
        "position_avg_price",
        "avg_cost_for_pnl",
        "average_cost_for_pnl",
        "avg_cost",
        "avgCost",
    )
    for field in explicit_fields:
        parsed = to_float(_record_or_extra_value(record_or_data, field))
        if parsed is not None and abs(parsed) > PNL_EPSILON:
            return abs(parsed)

    extra = _extra(record_or_data)
    snapshot = extra.get("position_snapshot")
    if isinstance(snapshot, dict):
        for field in ("avg_cost", "avgCost", "avg_price", "avgPrice", "average_cost", "averageCost"):
            parsed = to_float(snapshot.get(field))
            if parsed is not None and abs(parsed) > PNL_EPSILON:
                return abs(parsed)
    return None


def _number_with_presence(record_or_data: Any, *fields: str) -> tuple[float | None, str]:
    extra = _extra(record_or_data)
    for field in fields:
        for value in (_record_value(record_or_data, field), extra.get(field)):
            if value in (None, ""):
                continue
            parsed = to_float(value)
            if parsed is not None:
                return parsed, field
    return None, ""


def _number_field_sources(record_or_data: Any, field: str) -> list[tuple[float, str]]:
    extra = _extra(record_or_data)
    values = ((_record_value(record_or_data, field), "record"), (extra.get(field), "extra"))
    parsed_values: list[tuple[float, str]] = []
    for value, source in values:
        if value in (None, ""):
            continue
        parsed = to_float(value)
        if parsed is not None:
            parsed_values.append((parsed, source))
    return parsed_values


def _stored_net_pnl(record_or_data: Any) -> tuple[float | None, str]:
    return _number_with_presence(record_or_data, "realized_net_pnl", "net_pnl")


def _stored_gross_pnl(record_or_data: Any) -> tuple[float | None, str]:
    value, field = _number_with_presence(record_or_data, "realized_gross_pnl", "gross_pnl")
    if value is not None:
        return value, field
    for parsed, source in _number_field_sources(record_or_data, "pnl"):
        if abs(parsed) > PNL_EPSILON or source == "extra":
            return parsed, "pnl"
    return None, ""


def _normalize_trade_side(value: Any, *, exit_order_direction: bool = False) -> str:
    text = to_text(value).lower()
    if text in {"long", "buy_to_open"}:
        return "long"
    if text in {"short", "sell_to_open"}:
        return "short"
    if text == "buy":
        return "short" if exit_order_direction else "long"
    if text == "sell":
        return "long" if exit_order_direction else "short"
    return ""


def _direction_display(value: Any) -> str:
    text = to_text(value).strip()
    normalized = text.lower()
    if normalized in {"long", "buy", "buy_to_open"}:
        return "做多 / LONG"
    if normalized in {"short", "sell", "sell_to_open"}:
        return "做空 / SHORT"
    return text or "方向未知"


def _order_group_direction(rows: list[dict[str, Any]], primary: dict[str, Any]) -> str:
    for row in [primary, *(rows or [])]:
        normalized = normalize_order_row(row)
        for field_value in (
            normalized.get("position_side"),
            normalized.get("direction"),
            _record_or_extra_value(row, "position_side", "direction", "trade_side"),
        ):
            side = _normalize_trade_side(field_value)
            if side:
                return side
        role = normalized.get("role") or to_text(_record_or_extra_value(row, "role"))
        if role in ACTIVE_CLOSE_ROLES:
            side = _normalize_trade_side(_record_or_extra_value(row, "side", "action"), exit_order_direction=True)
            if side:
                return side
    return ""


def _trade_side(entry_order: dict[str, Any], exit_order: dict[str, Any]) -> str:
    for row, fields in (
        (entry_order, ("position_side", "direction", "side")),
        (exit_order, ("position_side", "trade_side")),
    ):
        for field in fields:
            side = _normalize_trade_side(_record_or_extra_value(row, field))
            if side:
                return side
    for field in ("direction", "side", "action"):
        side = _normalize_trade_side(_record_or_extra_value(exit_order, field), exit_order_direction=True)
        if side:
            return side
    return ""


def _directional_pnl(direction: Any, entry_price: Any, exit_price: Any, quantity: Any) -> float | None:
    side = _normalize_trade_side(direction)
    entry = to_float(entry_price)
    exit_ = to_float(exit_price)
    qty = to_float(quantity)
    if not side or entry is None or exit_ is None or qty is None or entry <= 0 or exit_ <= 0 or qty <= 0:
        return None
    per_share = entry - exit_ if side == "short" else exit_ - entry
    return per_share * abs(qty)


def _entry_reference_price(entry_order: dict[str, Any]) -> float | None:
    return _positive_number(
        entry_order,
        "reference_entry",
        "tv_reference_entry",
        "original_entry",
        "signal_reference_price",
        "entry_anchor",
        "entry",
    )


def _entry_submitted_price(entry_order: dict[str, Any]) -> float | None:
    return _positive_number(
        entry_order,
        "submitted_entry_limit_price",
        "submitted_limit_cap_price",
        "planned_entry_price",
        "entry_limit_price",
        "bounded_limit_price",
        "limit_price",
        "price",
    )


def _entry_slippage_model(entry_order: dict[str, Any], exit_order: dict[str, Any], entry_price: Any) -> dict[str, Any]:
    reference_price = _entry_reference_price(entry_order)
    submitted_price = _entry_submitted_price(entry_order)
    side = _trade_side(entry_order, exit_order)
    actual_entry = to_float(entry_price)
    cost_per_share: float | None = None
    cost_bps: float | None = None
    if side in {"long", "short"} and actual_entry is not None and reference_price is not None and reference_price > 0:
        cost_per_share = reference_price - actual_entry if side == "short" else actual_entry - reference_price
        cost_bps = (cost_per_share / reference_price) * 10000.0
    return {
        "entry_reference_price": reference_price,
        "entry_submitted_price": submitted_price,
        "entry_cost_per_share": cost_per_share,
        "entry_cost_bps": cost_bps,
    }


def _exit_order_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in sorted(rows or [], key=_role_sort_key):
        normalized = normalize_order_row(row)
        if normalized.get("role") in EXIT_ORDER_ROLES and to_text(normalized.get("status")).lower() in {"filled", "closed"}:
            return row
    return None


def _sum_known_commissions(rows: list[dict[str, Any]]) -> tuple[float, bool]:
    total = 0.0
    found = False
    for row in rows or []:
        row_found = False
        for field in COMMISSION_FIELDS:
            for value, source in _number_field_sources(row, field):
                if abs(value) <= PNL_EPSILON and source != "extra":
                    continue
                total += abs(value)
                found = True
                row_found = True
                break
            if row_found:
                break
    return total, found


def _pnl_outcome_label(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "盈亏"
    if parsed > 0:
        return "盈利"
    if parsed < 0:
        return "亏损"
    return "持平"


def _realized_pnl_model_for_entry_exit(
    entry_order: dict[str, Any] | None,
    exit_order: dict[str, Any] | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not entry_order or not exit_order:
        return None

    net_pnl, net_field = _stored_net_pnl(exit_order)
    if net_pnl is None:
        net_pnl, net_field = _stored_net_pnl(entry_order)

    commission, commission_known = _sum_known_commissions(rows)
    gross_pnl: float | None = None
    gross_field = ""
    if net_pnl is None:
        gross_pnl, gross_field = _stored_gross_pnl(exit_order)
        if gross_pnl is None:
            gross_pnl, gross_field = _stored_gross_pnl(entry_order)

    entry_price = _positive_number(
        entry_order,
        "fill_price",
        "avg_price",
        "avg_fill_price",
        "avgFillPrice",
        "avgPrice",
        "actual_fill_price",
        "entry_fill_price",
        "limit_price",
        "price",
        "entry_price",
    )
    exit_price = _positive_number(
        exit_order,
        "fill_price",
        "avg_price",
        "avg_fill_price",
        "avgFillPrice",
        "avgPrice",
        "actual_fill_price",
        "exit_fill_price",
        "last_fill_price",
        "lastFillPrice",
        "execution_price",
        "price",
        "limit_price",
        "tp_price",
        "take_profit",
        "sl_price",
        "stop_loss",
    )
    exit_quantity = _positive_number(exit_order, "filled_qty", "quantity")
    entry_filled_quantity = _positive_number(entry_order, "filled_qty")
    entry_order_quantity = _positive_number(entry_order, "quantity")
    entry_quantity = first_defined(entry_filled_quantity, entry_order_quantity)
    quantity = first_defined(exit_quantity, entry_quantity)
    quantity_capped = False
    cap_candidates = [value for value in (entry_filled_quantity, entry_order_quantity) if value is not None and value > 0]
    if exit_quantity is not None and cap_candidates:
        cap_value = min(cap_candidates)
        if exit_quantity > cap_value + PNL_EPSILON:
            quantity = cap_value
            quantity_capped = True
    if quantity_capped:
        entry_event_price = _positive_number(
            entry_order,
            "execution_price",
            "last_fill_price",
            "lastFillPrice",
            "actual_fill_price",
            "entry_fill_price",
        )
        exit_event_price = _positive_number(
            exit_order,
            "execution_price",
            "last_fill_price",
            "lastFillPrice",
            "actual_fill_price",
            "exit_fill_price",
        )
        if entry_event_price is not None:
            entry_price = entry_event_price
        if exit_event_price is not None:
            exit_price = exit_event_price

    value_is_net = net_pnl is not None
    value_source = net_field
    if net_pnl is not None:
        value = net_pnl
    else:
        if gross_pnl is None:
            side = _trade_side(entry_order, exit_order)
            gross_pnl = _directional_pnl(side, entry_price, exit_price, quantity)
            gross_field = "computed_gross_pnl" if gross_pnl is not None else ""
        if gross_pnl is None:
            return None
        value = gross_pnl - commission if commission_known else gross_pnl
        value_is_net = commission_known
        value_source = "computed_net_pnl" if commission_known and gross_field == "computed_gross_pnl" else gross_field

    normalized_exit = normalize_order_row(exit_order)
    return {
        "value": value,
        "is_net": value_is_net,
        "source": value_source,
        "commission": commission,
        "commission_known": commission_known,
        "entry_price": entry_price,
        **_entry_slippage_model(entry_order, exit_order, entry_price),
        "exit_price": exit_price,
        "quantity": quantity,
        "entry_quantity": entry_quantity,
        "entry_filled_quantity": entry_filled_quantity,
        "entry_order_quantity": entry_order_quantity,
        "exit_quantity_raw": exit_quantity,
        "quantity_capped": quantity_capped,
        "exit_role": normalized_exit.get("role") or to_text(_record_or_extra_value(exit_order, "role")) or "exit",
    }


def _realized_group_pnl_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    entry_order = pick_primary_order_row(rows) or {}
    exit_order = _exit_order_row(rows)
    return _realized_pnl_model_for_entry_exit(entry_order, exit_order, rows)


def _realized_pnl_detail(model: dict[str, Any]) -> str:
    parts: list[str] = []
    role = to_text(model.get("exit_label")) or ORDER_ROLE_LABELS.get(to_text(model.get("exit_role")), "退出")
    exit_price = model.get("exit_price")
    entry_price = model.get("entry_price")
    quantity = model.get("quantity")
    if exit_price is not None:
        parts.append(f"{role} @{_format_price(exit_price)}")
    if entry_price is not None:
        entry_parts = [f"入场 @{_format_price(entry_price)}"]
        entry_context: list[str] = []
        reference_price = model.get("entry_reference_price")
        submitted_price = model.get("entry_submitted_price")
        cost_bps = model.get("entry_cost_bps")
        cost_per_share = model.get("entry_cost_per_share")
        if reference_price is not None:
            entry_context.append(f"参考 {_format_price(reference_price)}")
        if submitted_price is not None:
            entry_context.append(f"提交 {_format_price(submitted_price)}")
        if cost_bps is not None:
            entry_context.append(f"成本/滑点 {_format_signed_bps(cost_bps)}")
        if cost_per_share is not None:
            entry_context.append(f"{_format_signed_price_delta(cost_per_share)}/股")
        if entry_context:
            entry_parts.append(f"（{'，'.join(entry_context)}）")
        parts.append("".join(entry_parts))
    if quantity is not None:
        parts.append(f"{_format_quantity(quantity)}股")
    if bool(model.get("is_net")):
        commission = to_float(model.get("commission")) or 0.0
        if commission > 0:
            parts.append(f"含手续费 {_format_money(commission)}")
    else:
        parts.append("未计手续费")
    return f"（{'，'.join(parts)}）" if parts else ""


def _realized_pnl_line(model: dict[str, Any] | None) -> str:
    if not model:
        return ""
    value = model.get("value")
    return f"**实际盈亏**: {_pnl_outcome_label(value)} {_format_signed_money(value)}{_realized_pnl_detail(model)}"


def _trade_ledger_price_context(
    order_record: dict[str, Any],
    *,
    related_rows: list[dict[str, Any]] | None = None,
    pnl_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = dedupe_order_rows([order_record, *(related_rows or [])])
    normalized = normalize_order_row(order_record)
    role = to_text(normalized.get("role") or _record_or_extra_value(order_record, "role"))
    entry_order = next((row for row in rows if normalize_order_row(row).get("role") == "entry"), None)
    exit_order = next(
        (row for row in rows if normalize_order_row(row).get("role") in EXIT_ORDER_ROLES),
        None,
    )

    entry_price = (pnl_model or {}).get("entry_price")
    exit_price = (pnl_model or {}).get("exit_price")
    if entry_price is None and entry_order:
        entry_price = _positive_number(
            entry_order,
            "fill_price",
            "avg_price",
            "avg_fill_price",
            "avgFillPrice",
            "avgPrice",
            "actual_fill_price",
            "entry_fill_price",
            "last_fill_price",
            "lastFillPrice",
            "execution_price",
            "limit_price",
            "price",
        )
    if entry_price is None and role == "entry":
        entry_price = _trade_ledger_fill_price(order_record) or _positive_number(order_record, "limit_price", "price", "entry_price")
    if exit_price is None and exit_order:
        exit_price = _positive_number(
            exit_order,
            "fill_price",
            "avg_price",
            "avg_fill_price",
            "avgFillPrice",
            "avgPrice",
            "actual_fill_price",
            "exit_fill_price",
            "last_fill_price",
            "lastFillPrice",
            "execution_price",
            "price",
            "limit_price",
            "tp_price",
            "take_profit",
            "sl_price",
            "stop_loss",
        )
    if exit_price is None and role in EXIT_ORDER_ROLES:
        exit_price = _trade_ledger_fill_price(order_record)

    latest_fill_price = _positive_number(
        order_record,
        "last_fill_price",
        "lastFillPrice",
        "execution_price",
        "fill_price",
        "avg_price",
        "avg_fill_price",
        "avgFillPrice",
        "avgPrice",
    )
    pnl_quantity = (pnl_model or {}).get("quantity")
    if pnl_quantity is None:
        pnl_quantity = first_defined(
            _positive_number(order_record, "quantity_for_pnl", "filled_qty", "quantity"),
            _positive_number(entry_order or {}, "filled_qty", "quantity") if entry_order else None,
        )
    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "latest_fill_price": latest_fill_price,
        "pnl_quantity": pnl_quantity,
    }


def _single_order_pnl_model(order_record: Any) -> dict[str, Any] | None:
    net_pnl, net_field = _stored_net_pnl(order_record)
    commission, commission_known = _sum_known_commissions([order_record])
    exit_price = _positive_number(
        order_record,
        "fill_price",
        "avg_price",
        "avg_fill_price",
        "actual_fill_price",
        "last_fill_price",
        "lastFillPrice",
        "execution_price",
        "price",
        "limit_price",
    )
    quantity = _positive_number(order_record, "filled_qty", "quantity")
    if net_pnl is not None:
        value = net_pnl
        value_is_net = True
        value_source = net_field
    else:
        gross_pnl, gross_field = _stored_gross_pnl(order_record)
        role = normalize_order_row(order_record).get("role") or to_text(_record_or_extra_value(order_record, "role"))
        if gross_pnl is None and role in EXIT_ORDER_ROLES:
            entry_price = _position_entry_price_hint(order_record)
            side = _trade_side({}, order_record)
            gross_pnl = _directional_pnl(side, entry_price, exit_price, quantity)
            gross_field = "computed_gross_pnl_from_position_avg_cost" if gross_pnl is not None else ""
        if gross_pnl is None:
            return None
        value = gross_pnl - commission if commission_known else gross_pnl
        value_is_net = commission_known
        value_source = gross_field
    return {
        "value": value,
        "is_net": value_is_net,
        "source": value_source,
        "commission": commission,
        "commission_known": commission_known,
        "entry_price": _position_entry_price_hint(order_record),
        "exit_price": exit_price,
        "quantity": quantity,
        "exit_role": normalize_order_row(order_record).get("role") or to_text(_record_or_extra_value(order_record, "role")),
    }


def _as_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _nested_object(source: dict[str, Any], *path: str) -> dict[str, Any]:
    current: Any = source
    for key in path:
        current = _as_object(current).get(key)
    return _as_object(current)


def _nested_value(source: dict[str, Any], *path: str) -> Any:
    current: Any = source
    for key in path:
        current = _as_object(current).get(key)
        if current is None:
            return None
    return current


def _compact_number(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return ""
    text = f"{parsed:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _reason_label(reason: Any) -> str:
    normalized = to_text(reason)
    if not normalized:
        return ""
    lowered = normalized.lower()
    if "invalid price" in lowered:
        return f"券商拒绝保护单价格（{normalized}）"
    if "order canceled" in lowered or "order cancelled" in lowered:
        return f"保护单已被券商取消（{normalized}）"
    return ORDER_GROUP_REASON_LABELS.get(normalized, f"系统记录原因 {normalized}")


def _normalized_exit_reason(reason: Any) -> str:
    normalized = to_text(reason).strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    if lowered.startswith("closed_by_reverse_"):
        return "closed_by_reverse_close" if lowered.endswith("_close") else "closed_by_reverse_signal"
    if lowered.startswith("closed_by_tv_execution_action"):
        return "tv_execution_action_close"
    return lowered


def _exit_reason_label(reason: Any, *, fallback: str = "") -> str:
    code = _normalized_exit_reason(reason)
    if not code:
        return fallback
    if code in EXIT_REASON_LABELS:
        return EXIT_REASON_LABELS[code]
    if code == "tv_execution_action_close":
        return "TV 退出平仓"
    if code.startswith("force_flat") or "eod" in code:
        return "EOD 平仓"
    if "runner_stop" in code:
        return "Runner 止损"
    if "take_profit" in code or code.endswith("_tp") or code == "tp":
        return "止盈"
    if "stop_loss" in code or code.endswith("_sl") or code == "sl":
        return "止损"
    if "reverse" in code:
        return "反向平仓"
    if "order_flow" in code and "exit" in code:
        return "订单流平仓"
    if "harvest" in code and "exit" in code:
        return "日内收割平仓"
    if "manual" in code or "positions_close" in code:
        return "手动平仓"
    return fallback or _reason_label(code)


def _direct_exit_reason(row: dict[str, Any]) -> str:
    for field in (
        "exit_reason",
        "close_reason",
        "close_reason_code",
        "status_reason",
        "reason",
        "last_status_reason",
    ):
        reason = to_text(_record_or_extra_value(row, field))
        if reason and reason.lower() not in GENERIC_ENTRY_REASONS:
            return reason
    return ""


def _source_exit_reason(row: dict[str, Any]) -> tuple[str, str]:
    source = to_text(_record_or_extra_value(row, "source", "submitted_via")).lower()
    if source in EXIT_SOURCE_REASON_LABELS:
        return EXIT_SOURCE_REASON_LABELS[source]
    coid = to_text(_record_or_extra_value(row, "coid", "order_ref", "orderRef", "unique_id")).lower()
    for prefix, model in EXIT_SOURCE_REASON_LABELS.items():
        if prefix and coid.startswith(prefix):
            return model
    return "", ""


def _exit_reason_model(
    row: dict[str, Any],
    *,
    related_rows: list[dict[str, Any]] | None = None,
) -> dict[str, str] | None:
    normalized = normalize_order_row(row)
    role = normalized.get("role") or to_text(_record_or_extra_value(row, "role"))
    if role in PROTECTION_TP_ROLES:
        if not _row_is_filled(row):
            return None
        return {"code": "take_profit", "label": ORDER_ROLE_LABELS.get(role, "止盈"), "source": "role"}
    if role in PROTECTION_SL_ROLES:
        if not _row_is_filled(row):
            return None
        reason = _direct_exit_reason(row)
        label = _exit_reason_label(reason, fallback=ORDER_ROLE_LABELS.get(role, "止损"))
        return {"code": _normalized_exit_reason(reason) or "stop_loss", "label": label, "source": "role"}
    if role not in ACTIVE_CLOSE_ROLES:
        return None

    reason = _direct_exit_reason(row)
    if reason:
        return {"code": _normalized_exit_reason(reason), "label": _exit_reason_label(reason, fallback=UNKNOWN_CLOSE_LABEL), "source": "row"}

    reason_model = _reason_source_for_row(row)
    reason = to_text((reason_model or {}).get("reason"))
    if reason and reason.lower() not in GENERIC_ENTRY_REASONS:
        return {
            "code": _normalized_exit_reason(reason),
            "label": _exit_reason_label(reason, fallback=UNKNOWN_CLOSE_LABEL),
            "source": to_text((reason_model or {}).get("source")) or "row",
        }

    group_rows = dedupe_order_rows([row, *(related_rows or [])])
    group_reason_model = _order_group_reason_model(group_rows)
    group_reason = to_text((group_reason_model or {}).get("reason"))
    if group_reason and group_reason.lower() not in GENERIC_ENTRY_REASONS:
        return {
            "code": _normalized_exit_reason(group_reason),
            "label": _exit_reason_label(group_reason, fallback=UNKNOWN_CLOSE_LABEL),
            "source": to_text((group_reason_model or {}).get("source")) or "group",
        }

    source_code, source_label = _source_exit_reason(row)
    if source_label:
        return {"code": source_code, "label": source_label, "source": "source"}

    return {"code": "", "label": UNKNOWN_CLOSE_LABEL, "source": "missing"}


def _reason_source_for_row(row: dict[str, Any]) -> dict[str, Any] | None:
    extra = _extra(row)
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for field in ("order_flow_full_exit", "harvest_full_exit", "harvest_partial_exit", "order_flow_close_result"):
        payload = _as_object(extra.get(field))
        if not payload:
            continue
        decision = _as_object(payload.get("decision")) or _as_object(payload.get("order_flow_decision"))
        candidates.append((field, payload, decision))
    for field in ("order_flow_decision", "harvest_last_decision"):
        decision = _as_object(extra.get(field))
        if decision:
            candidates.append((field, {}, decision))

    for field, payload, decision in candidates:
        reason = to_text(decision.get("reason") or payload.get("reason"))
        if reason:
            return {"reason": reason, "source": field, "payload": payload, "decision": decision, "row": row}

    reason = to_text(extra.get("last_status_reason") or extra.get("reason") or extra.get("status_reason"))
    if reason:
        return {"reason": reason, "source": "row_extra", "payload": {}, "decision": {}, "row": row}
    for field in ("broker_last_error", "order_error", "broker_error", "ib_error"):
        payload = _as_object(extra.get(field))
        if not payload:
            continue
        reason = to_text(payload.get("message") or payload.get("error") or payload.get("reason"))
        if reason:
            return {"reason": reason, "source": field, "payload": payload, "decision": {}, "row": row}
    return None


def _row_reason_recency(row: dict[str, Any]) -> tuple[int, str]:
    extra = _extra(row)
    for field in ("status_updated_bar_time_ms", "bar_time_ms", "updated_bar_time_ms", "created_bar_time_ms"):
        parsed = to_float(_record_or_extra_value(row, field))
        if parsed is not None:
            return int(parsed), to_text(normalize_order_row(row).get("unique_id") or row.get("id"))
    for field in ("updated", "created", "us_time", "order_time", "fill_time"):
        text = to_text(_record_or_extra_value(row, field) or extra.get(field))
        if text:
            return 0, text
    return 0, to_text(normalize_order_row(row).get("unique_id") or row.get("id"))


def _order_group_reason_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    protection_model = _protection_state_model(rows)
    terminal_protection_rows = [
        row
        for row in rows or []
        if (normalize_order_row(row).get("role") or "") in PROTECTION_ROLES
        and _row_status_key(row) in {"canceled", "cancelled", "inactive", "rejected", "expired"}
    ]
    for row in sorted(terminal_protection_rows, key=_row_reason_recency, reverse=True):
        model = _reason_source_for_row(row)
        reason = to_text((model or {}).get("reason"))
        if model and reason.lower() not in GENERIC_ENTRY_REASONS:
            return model

    fallback_model: dict[str, Any] | None = None
    for row in sorted(rows or [], key=_role_sort_key):
        model = _reason_source_for_row(row)
        reason = to_text((model or {}).get("reason"))
        if model and reason.lower() in GENERIC_ENTRY_REASONS and protection_model.get("state") == "missing_after_fill":
            fallback_model = model
            continue
        if model:
            return model
    if protection_model.get("state") == "missing_after_fill":
        if any(_row_status_key(row) == "rejected" for row in terminal_protection_rows):
            reason = "protection_leg_rejected"
        elif terminal_protection_rows:
            reason = "protection_leg_canceled"
        else:
            reason = "protection_missing_after_entry_fill"
        return {"reason": reason, "source": "protection_state", "payload": protection_model, "decision": {}, "row": {}}
    if fallback_model:
        return fallback_model
    return None


def _delta_evidence_line(reason: str, decision: dict[str, Any]) -> str:
    if reason != "order_flow_adverse_delta_exit":
        return ""
    confirmation = _as_object(decision.get("confirmation"))
    direction = to_text(decision.get("direction")).lower()
    direction_label = "多头" if direction == "long" else ("空头" if direction == "short" else "持仓")
    adverse_label = "空方" if direction == "long" else ("多方" if direction == "short" else "反向")
    interval = _compact_number(confirmation.get("interval_sec")) or "60"
    delta_ratio = to_float(confirmation.get("delta_ratio"))
    abs_delta_ratio = abs(delta_ratio) if delta_ratio is not None else None
    exit_threshold = first_defined(
        decision.get("exit_delta_ratio"),
        decision.get("exit_ratio"),
        confirmation.get("exit_delta_ratio"),
        ORDER_FLOW_DEFAULT_EXIT_DELTA_RATIO,
    )
    pnl_r = decision.get("pnl_r")
    profit_threshold = first_defined(decision.get("profit_exit_r"), decision.get("max_exit_pnl_r"), ORDER_FLOW_DEFAULT_PROFIT_EXIT_R)

    parts = [f"{direction_label}遇到 {interval}s {adverse_label} Delta"]
    if delta_ratio is not None:
        parts.append(
            f"delta_ratio {_compact_number(delta_ratio)}"
            f"（abs {_compact_number(abs_delta_ratio)}）≥ 平仓阈值 {_compact_number(exit_threshold)}"
        )
    if to_float(confirmation.get("delta")) is not None:
        parts.append(f"delta {_compact_number(confirmation.get('delta'))}")
    if to_float(confirmation.get("cvd")) is not None:
        parts.append(f"CVD {_compact_number(confirmation.get('cvd'))}")
    if to_float(pnl_r) is not None:
        parts.append(f"pnl_r {_compact_number(pnl_r)} ≤ {_compact_number(profit_threshold)}")
    return f"**依据**: {'，'.join(parts)}"


def _action_summary_line(rows: list[dict[str, Any]], reason_model: dict[str, Any], pnl_model: dict[str, Any] | None) -> str:
    reason = to_text(reason_model.get("reason"))
    if reason not in {"order_flow_adverse_delta_exit", "order_flow_full_exit", "intraday_harvest_full_exit"}:
        return ""

    decision = _as_object(reason_model.get("decision"))
    canceled_roles: list[str] = []
    for row in sorted(rows or [], key=_role_sort_key):
        normalized = normalize_order_row(row)
        role = normalized.get("role") or ""
        if role not in {"take_profit", "stop_loss", "tp", "sl", "repair_tp", "repair_sl"}:
            continue
        if to_text(normalized.get("status")).lower() not in {"canceled", "cancelled"}:
            continue
        label = ORDER_ROLE_LABELS.get(role, role)
        if label not in canceled_roles:
            canceled_roles.append(label)
    quantity = first_defined(
        (pnl_model or {}).get("quantity"),
        decision.get("quantity"),
        decision.get("closed_quantity"),
        _nested_value(_as_object(reason_model.get("payload")), "market_close_result", "fill", "filled_quantity"),
    )
    direction = to_text(decision.get("direction") or _record_or_extra_value(reason_model.get("row"), "position_side", "direction")).lower()
    close_side = "卖出" if direction == "long" else ("买入" if direction == "short" else "平仓")
    avg_fill = first_defined(
        (pnl_model or {}).get("exit_price"),
        _nested_value(_as_object(reason_model.get("payload")), "market_close_result", "fill", "order", "avgFillPrice"),
        _nested_value(_as_object(reason_model.get("payload")), "market_close_result", "fill", "order", "avgPrice"),
    )
    limit_price = first_defined(decision.get("limit_price"), _nested_value(_as_object(reason_model.get("payload")), "market_close_result", "limit_price"))

    parts: list[str] = []
    if canceled_roles:
        parts.append(f"已取消{'/'.join(canceled_roles)}保护单")
    if quantity:
        close_text = f"用平仓单{close_side} {_format_quantity(quantity)} 股"
    else:
        close_text = f"用平仓单{close_side}"
    if limit_price not in (None, ""):
        close_text = f"{close_text}，限价 {_format_price(limit_price)}"
    if avg_fill not in (None, ""):
        close_text = f"{close_text}，均价 {_format_price(avg_fill)}"
    parts.append(close_text)
    return f"**处理**: {'，'.join(parts)}" if parts else ""


def _order_group_reason_lines(rows: list[dict[str, Any]], pnl_model: dict[str, Any] | None) -> list[str]:
    reason_model = _order_group_reason_model(rows)
    if not reason_model:
        return []
    reason = to_text(reason_model.get("reason"))
    label = _reason_label(reason)
    lines = [f"**原因**: {label}（{reason}）" if reason in ORDER_GROUP_REASON_LABELS else f"**原因**: {label}"]
    evidence_line = _delta_evidence_line(reason, _as_object(reason_model.get("decision")))
    if evidence_line:
        lines.append(evidence_line)
    action_line = _action_summary_line(rows, reason_model, pnl_model)
    if action_line:
        lines.append(action_line)
    return lines


def _leg_line(row: dict[str, Any]) -> str:
    normalized = normalize_order_row(row)
    role = normalized.get("role") or to_text(_record_or_extra_value(row, "order_type")) or "order"
    label = ORDER_ROLE_LABELS.get(role, role or "订单")
    status = normalized.get("status") or "-"
    order_id = normalized.get("broker_order_id") or to_text(_record_or_extra_value(row, "order_id", "broker_order_id"))
    quantity = _format_quantity(_record_or_extra_value(row, "quantity"))
    filled = _format_quantity(_record_or_extra_value(row, "filled_qty"))
    fill_price = _positive_number(
        row,
        "fill_price",
        "avg_price",
        "avg_fill_price",
        "actual_fill_price",
        "last_fill_price",
        "lastFillPrice",
        "execution_price",
    )
    planned_price = _positive_number(
        row,
        "limit_price",
        "price",
        "tp_price",
        "take_profit",
        "sl_price",
        "stop_loss",
    )
    display_price = fill_price if _row_is_filled(row) and fill_price is not None else first_defined(planned_price, fill_price)
    price = _format_price(display_price)
    if fill_price is not None and planned_price is not None and abs(fill_price - planned_price) > 0.005:
        price = f"{price}（限价 {_format_price(planned_price)}）"
    return f"**{label}**: {_status_text(status)} · ID {order_id or '-'} · {filled}/{quantity} · {price}"


def _group_status_template(status: str, pnl_value: Any = None) -> str:
    parsed_pnl = to_float(pnl_value)
    if parsed_pnl is not None and parsed_pnl < 0:
        return "red"
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
    trade_group_id = to_text(_record_or_extra_value(primary, "trade_group_id", "entry_order_unique_id"))
    signal_id = to_text(_record_or_extra_value(primary, "signal_id"))
    protection_model = _protection_state_model(rows)
    resolved_status = to_text(status or _group_status(rows) or _record_or_extra_value(primary, "status", "order_status", "current_status"))
    if protection_model.get("state") == "missing_after_fill" and resolved_status in {"", "Filled", "Submitted"}:
        resolved_status = "protection_incomplete"
    status_text = _group_status_display_text(resolved_status, rows)
    direction_display = _direction_display(_order_group_direction(rows, primary))
    pnl_model = _realized_group_pnl_model(rows)
    pnl_line = _realized_pnl_line(pnl_model)
    title_pnl = f" · {_pnl_outcome_label(pnl_model.get('value'))} {_format_signed_money(pnl_model.get('value'))}" if pnl_model else ""

    body_lines = [
        f"**状态**: {status_text}",
        f"**Symbol**: {symbol}",
        f"**Broker**: {broker_badge}",
        f"**交易方向**: {direction_display}",
        f"**信号ID / 交易组**: {signal_id or '-'} / {trade_group_id or '-'}",
    ]
    protection_line = _protection_status_line(rows)
    if protection_line:
        body_lines.append(protection_line)
    if rows:
        body_lines.extend(_leg_line(row) for row in rows)
    else:
        body_lines.append("**订单**: -")
    body_lines.extend(_order_group_reason_lines(rows, pnl_model))
    if pnl_line:
        body_lines.append(pnl_line)
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
                "content": f"📦 订单组 · {broker_badge} · {direction_display} · {status_text}{title_pnl} · {symbol}",
            },
            "template": _group_status_template(resolved_status, pnl_model.get("value") if pnl_model else None),
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
    merged_patch = dict(extra_patch)
    try:
        getter = getattr(pb, "get_first_record", None)
        if callable(getter):
            safe_id = record_id.replace("\\", "\\\\").replace('"', '\\"')
            latest = getter("orders", filter=f'id = "{safe_id}"')
            latest_extra = ensure_object((latest or {}).get("extra")) if isinstance(latest, dict) else {}
            if latest_extra:
                merged_patch = {**latest_extra, **merged_patch}
    except Exception:
        merged_patch = dict(extra_patch)
    try:
        pb.update_record("orders", record_id, {"extra": merged_patch})
    except Exception:
        return


def _truthy(value: Any) -> bool:
    return to_text(value).lower() in {"1", "true", "yes", "y", "on"}


def _trade_ledger_environment_allowed(environment: Any) -> bool:
    return to_text(environment or "live").lower() in {"live", "paper"}


def _trade_ledger_role_label(order_record: Any) -> str:
    role = to_text(_record_or_extra_value(order_record, "role"))
    return ORDER_ROLE_LABELS.get(role, role or "-")


def _trade_ledger_display_role_label(
    order_record: dict[str, Any],
    *,
    related_rows: list[dict[str, Any]] | None = None,
) -> str:
    exit_model = _exit_reason_model(order_record, related_rows=related_rows)
    return to_text((exit_model or {}).get("label")) or _trade_ledger_role_label(order_record)


def _trade_ledger_callback_type(order_record: Any) -> str:
    return to_text(_record_or_extra_value(order_record, "ib_callback_type", "callback_type")) or "broker_callback"


def _trade_ledger_exec_id(order_record: Any) -> str:
    return to_text(_record_or_extra_value(order_record, "ib_exec_id", "exec_id", "execution_id"))


def _trade_ledger_filled_qty(order_record: Any) -> float:
    return to_float(_record_or_extra_value(order_record, "filled_qty", "filledQuantity", "filled")) or 0.0


def _trade_ledger_order_qty(order_record: Any) -> float:
    return to_float(_record_or_extra_value(order_record, "quantity", "totalSize", "totalQuantity")) or 0.0


def _order_status_key(status: Any) -> str:
    return to_text(status).replace("_", "").lower()


def _is_close_order_role(order_record: Any) -> bool:
    return to_text(_record_or_extra_value(order_record, "role")).lower() in ACTIVE_CLOSE_ROLES


def _is_incomplete_close_cancel(
    order_record: Any,
    *,
    status: Any = "",
    quantity: float | None = None,
    filled_qty: float | None = None,
) -> bool:
    if not _is_close_order_role(order_record):
        return False
    if _order_status_key(status or _record_or_extra_value(order_record, "status", "order_status", "current_status")) not in CANCELLED_ORDER_STATUS_KEYS:
        return False
    resolved_quantity = _trade_ledger_order_qty(order_record) if quantity is None else float(quantity or 0.0)
    resolved_filled = _trade_ledger_filled_qty(order_record) if filled_qty is None else float(filled_qty or 0.0)
    return resolved_quantity <= 0 or resolved_filled + 1e-8 < resolved_quantity


def _trade_ledger_fill_price(order_record: Any) -> float | None:
    return _positive_number(
        order_record,
        "fill_price",
        "avg_price",
        "avgFillPrice",
        "avgPrice",
        "last_fill_price",
        "lastFillPrice",
        "execution_price",
    )


def _trade_ledger_exit_fill_pnl_candidate(order_record: Any, event_model: dict[str, Any]) -> bool:
    role = normalize_order_row(order_record).get("role") or to_text(_record_or_extra_value(order_record, "role"))
    if role not in EXIT_ORDER_ROLES:
        return False
    if to_text(event_model.get("event_type")) != "fill":
        return False
    filled_qty = first_defined(to_float(event_model.get("filled_qty")), _trade_ledger_filled_qty(order_record))
    return bool(filled_qty and filled_qty > 0)


def _trade_ledger_related_rows_for_pnl(
    pb: Any,
    order_record: dict[str, Any],
    event_model: dict[str, Any],
) -> list[dict[str, Any]]:
    if not _trade_ledger_exit_fill_pnl_candidate(order_record, event_model):
        return []
    environment = to_text(event_model.get("environment") or _record_or_extra_value(order_record, "environment")) or "live"
    try:
        context = _order_group_context(pb, order_record, environment=environment)
    except Exception:
        return []
    return [dict(row) for row in (context.get("related_rows") or []) if isinstance(row, dict)]


def _trade_ledger_exit_pnl_model(
    order_record: Any,
    event_model: dict[str, Any],
    *,
    related_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not _trade_ledger_exit_fill_pnl_candidate(order_record, event_model):
        return None

    exit_order = dict(order_record) if isinstance(order_record, dict) else {}
    if not exit_order:
        return None
    if _status_key(_record_or_extra_value(exit_order, "status", "order_status", "current_status")) not in {
        "filled",
        "closed",
        "executed",
    }:
        exit_order["status"] = "Filled"
    if _trade_ledger_filled_qty(exit_order) <= 0 and to_float(event_model.get("filled_qty")):
        exit_order["filled_qty"] = to_float(event_model.get("filled_qty"))

    rows = dedupe_order_rows([exit_order, *(related_rows or [])])
    entry_order = next((row for row in rows if normalize_order_row(row).get("role") == "entry"), None)
    model = _realized_pnl_model_for_entry_exit(entry_order, exit_order, rows) if entry_order else None
    return model or _single_order_pnl_model(exit_order)


def _trade_ledger_notified_keys(extra: Any) -> set[str]:
    source = ensure_object(extra)
    raw_keys = source.get("feishu_trade_ledger_notified_keys")
    keys: set[str] = set()
    if isinstance(raw_keys, list):
        keys.update(to_text(item) for item in raw_keys if to_text(item))
    elif isinstance(raw_keys, dict):
        keys.update(to_text(key) for key, value in raw_keys.items() if value and to_text(key))
    elif isinstance(raw_keys, str):
        try:
            parsed = json.loads(raw_keys)
            if isinstance(parsed, list):
                keys.update(to_text(item) for item in parsed if to_text(item))
            elif isinstance(parsed, dict):
                keys.update(to_text(key) for key, value in parsed.items() if value and to_text(key))
            elif to_text(parsed):
                keys.add(to_text(parsed))
        except Exception:
            keys.add(to_text(raw_keys))
    last_key = to_text(source.get("feishu_trade_ledger_notify_key"))
    if last_key:
        keys.add(last_key)
    return {key for key in keys if key}


def _trade_ledger_fill_exec_notify_key(
    *,
    environment: str,
    broker_order_id: str,
    unique_id: str,
    exec_id: str,
    event_type: str,
) -> str:
    if to_text(event_type) != "fill" or not to_text(exec_id):
        return ""
    order_identity = to_text(broker_order_id) or to_text(unique_id)
    if not order_identity:
        return ""
    return f"trade_ledger_callback_v2:{to_text(environment) or 'live'}:{order_identity}:{to_text(exec_id)}:fill"


def _trade_ledger_event_model(order_record: dict[str, Any], previous_order: dict[str, Any] | None) -> dict[str, Any]:
    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    if not _trade_ledger_environment_allowed(environment):
        return {"skipped": True, "reason": "environment_not_notifiable", "environment": environment}
    if not _truthy(_record_or_extra_value(order_record, "broker_realtime_callback")):
        return {"skipped": True, "reason": "not_realtime_broker_callback", "environment": environment}

    previous = previous_order if isinstance(previous_order, dict) else {}
    previous_extra = ensure_object(previous.get("extra"))
    current_status = to_text(_record_or_extra_value(order_record, "status", "order_status", "current_status"))
    previous_status = to_text(_record_or_extra_value(previous, "status", "order_status", "current_status"))
    current_filled = _trade_ledger_filled_qty(order_record)
    previous_filled = _trade_ledger_filled_qty(previous)
    fill_delta = max(0.0, current_filled - previous_filled)
    quantity = _trade_ledger_order_qty(order_record)
    current_broker_order_id = to_text(_record_or_extra_value(order_record, "broker_order_id", "order_id", "orderId"))
    previous_broker_order_id = to_text(_record_or_extra_value(previous, "broker_order_id", "order_id", "orderId"))
    first_realtime_callback = not _truthy(previous_extra.get("broker_realtime_callback"))
    broker_order_id_appeared = bool(current_broker_order_id and not previous_broker_order_id)
    status_changed = bool(current_status and previous_status and current_status != previous_status)
    callback_type = _trade_ledger_callback_type(order_record)
    current_status_key = current_status.replace("_", "").lower()
    terminal_status = current_status in TRADE_LEDGER_TERMINAL_STATUSES or current_status_key in {
        "canceled",
        "cancelled",
        "rejected",
        "expired",
        "inactive",
    }
    is_full_fill = quantity > 0 and current_filled + 1e-8 >= quantity
    has_trade_ledger_notification = bool(_trade_ledger_notified_keys(order_record.get("extra")))
    incomplete_close_cancel = _is_incomplete_close_cancel(
        order_record,
        status=current_status,
        quantity=quantity,
        filled_qty=current_filled,
    )

    if fill_delta > 0:
        event_type = "fill"
        event_label = "已成交" if current_status in {"Filled", "Closed", "Executed"} or is_full_fill else "部分成交"
        reason = "fill_quantity_increased"
    elif (
        current_status in {"Filled", "Closed", "Executed"}
        and quantity > 0
        and previous_filled + 1e-8 >= quantity
        and has_trade_ledger_notification
    ):
        return {
            "skipped": True,
            "reason": "full_fill_already_seen",
            "environment": environment,
            "status": current_status,
        }
    elif current_status in {"Filled", "Closed", "Executed"} and quantity > 0 and previous_filled + 1e-8 >= quantity:
        event_type = "fill"
        event_label = "已成交"
        reason = "full_fill_status_confirmed"
    elif terminal_status and (status_changed or first_realtime_callback):
        event_type = "terminal_status"
        event_label = "平仓单已取消（未完全成交）" if incomplete_close_cancel else _status_text(current_status)
        reason = "close_order_cancelled_incomplete" if incomplete_close_cancel else "terminal_status"
    elif first_realtime_callback:
        if current_status in TRADE_LEDGER_NON_MATERIAL_CALLBACK_STATUSES and callback_type in {"openOrder", "orderStatus"}:
            return {
                "skipped": True,
                "reason": "non_terminal_callback_noise",
                "environment": environment,
                "status": current_status,
            }
        if callback_type == "execDetails" and current_filled > 0:
            event_type = "fill"
            event_label = "成交明细首次入流水"
            reason = "first_exec_details_callback"
        else:
            event_type = "first_callback"
            event_label = "首次入交易流水"
            reason = "first_realtime_callback"
    elif broker_order_id_appeared:
        if current_status in TRADE_LEDGER_NON_MATERIAL_CALLBACK_STATUSES and callback_type in {"openOrder", "orderStatus"}:
            return {
                "skipped": True,
                "reason": "non_terminal_callback_noise",
                "environment": environment,
                "status": current_status,
            }
        event_type = "broker_order_confirmed"
        event_label = "Broker订单确认"
        reason = "broker_order_id_appeared"
    elif status_changed:
        if current_status in TRADE_LEDGER_NON_MATERIAL_CALLBACK_STATUSES and callback_type in {"openOrder", "orderStatus"}:
            return {
                "skipped": True,
                "reason": "non_terminal_callback_noise",
                "environment": environment,
                "status": current_status,
            }
        event_type = "status_change"
        event_label = _status_text(current_status)
        reason = "status_changed"
    else:
        return {
            "skipped": True,
            "reason": "no_material_callback_delta",
            "environment": environment,
            "status": current_status,
        }

    exec_id = _trade_ledger_exec_id(order_record)
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    unique_id = to_text(_record_or_extra_value(order_record, "unique_id", "id"))
    digest_payload = {
        "environment": environment,
        "broker_order_id": current_broker_order_id,
        "unique_id": unique_id,
        "trade_group_id": trade_group_id,
        "callback_type": callback_type,
        "exec_id": exec_id,
        "event_type": event_type,
        "status": current_status,
        "filled_qty": round(current_filled, 8),
        "fill_price": round(_trade_ledger_fill_price(order_record) or 0.0, 8),
    }
    digest = hashlib.sha1(json.dumps(digest_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:16]
    legacy_notify_key = f"trade_ledger_callback_v1:{environment}:{current_broker_order_id or unique_id}:{digest}"
    notify_key = _trade_ledger_fill_exec_notify_key(
        environment=environment,
        broker_order_id=current_broker_order_id,
        unique_id=unique_id,
        exec_id=exec_id,
        event_type=event_type,
    ) or legacy_notify_key
    display_status = current_status
    if event_type == "fill" and is_full_fill and current_status not in {"Filled", "Closed", "Executed"}:
        display_status = "Filled"
    return {
        "skipped": False,
        "reason": reason,
        "event_type": event_type,
        "event_label": event_label,
        "environment": environment,
        "status": display_status,
        "previous_status": previous_status,
        "filled_qty": current_filled,
        "previous_filled_qty": previous_filled,
        "fill_delta": fill_delta,
        "callback_type": callback_type,
        "exec_id": exec_id,
        "close_remaining_qty": max(0.0, quantity - current_filled) if incomplete_close_cancel else 0.0,
        "notify_key": notify_key,
        "legacy_notify_key": legacy_notify_key,
    }


def _trade_ledger_decision_line(order_record: dict[str, Any], event_model: dict[str, Any]) -> str:
    reason = to_text(event_model.get("reason"))
    callback_type = to_text(event_model.get("callback_type")) or _trade_ledger_callback_type(order_record)
    status = to_text(event_model.get("status") or _record_or_extra_value(order_record, "status", "order_status", "current_status"))
    previous_status = to_text(event_model.get("previous_status"))
    filled_qty = to_float(event_model.get("filled_qty"))
    previous_filled_qty = to_float(event_model.get("previous_filled_qty"))
    fill_delta = to_float(event_model.get("fill_delta")) or 0.0
    quantity = _trade_ledger_order_qty(order_record)
    broker_order_id = to_text(_record_or_extra_value(order_record, "broker_order_id", "order_id", "orderId"))

    if reason == "fill_quantity_increased":
        return (
            f"成交数量增加：前次 {_format_quantity(previous_filled_qty)} -> 本次 {_format_quantity(filled_qty)}"
            f"（+{_format_quantity(fill_delta)}）。"
        )
    if reason == "first_exec_details_callback":
        qty_label = (
            f"{_format_quantity(filled_qty)}/{_format_quantity(quantity)}"
            if quantity > 0
            else _format_quantity(filled_qty)
        )
        return (
            f"上一条记录无 broker 实时回调标记；当前是 IB {callback_type} 成交明细，"
            f"已成交 {qty_label}，成交增量 {_format_quantity(fill_delta)}。"
        )
    if reason == "full_fill_status_confirmed":
        return "本地已记录满成交；当前 Broker 状态确认已成交，且交易流水此前未成功发送。"
    if reason == "close_order_cancelled_incomplete":
        return (
            f"平仓单状态变为 {_status_text(status)}，但已成交 {_format_quantity(filled_qty)}/"
            f"{_format_quantity(quantity)}，仍有未成交数量。"
        )
    if reason == "terminal_status":
        prefix = (
            f"状态变化：{_status_text(previous_status)} -> {_status_text(status)}"
            if previous_status
            else f"首次回调即为终态：{_status_text(status)}"
        )
        return f"{prefix}。"
    if reason == "first_realtime_callback":
        return f"上一条记录无 broker 实时回调标记；当前回调类型 {callback_type}，状态 {_status_text(status)}。"
    if reason == "broker_order_id_appeared":
        return f"本次首次拿到 Broker 订单 ID：{broker_order_id or '-'}。"
    if reason == "status_changed":
        return f"状态变化：{_status_text(previous_status)} -> {_status_text(status)}。"
    return ""


def _trade_ledger_template(event_model: dict[str, Any], status: str, pnl_value: Any = None) -> str:
    if event_model.get("reason") == "close_order_cancelled_incomplete":
        return "red"
    parsed_pnl = to_float(pnl_value)
    if parsed_pnl is not None and parsed_pnl < 0:
        return "red"
    if event_model.get("event_type") == "fill" or status in {"Filled", "Closed", "Executed"}:
        return "green"
    if status in {"Canceled", "Cancelled", "Rejected", "Expired", "Inactive"}:
        return "orange"
    return "blue"


def build_order_callback_ledger_card(
    order_record: dict[str, Any],
    event_model: dict[str, Any],
    *,
    console_base_url: str = "",
    related_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    environment = to_text(_record_or_extra_value(order_record, "environment")) or "live"
    broker_badge = _broker_badge(environment)
    symbol = to_text(_record_or_extra_value(order_record, "symbol")).upper() or "ORDER"
    status = to_text(event_model.get("status") or _record_or_extra_value(order_record, "status", "order_status", "current_status"))
    status_text = _status_text(status)
    callback_type = to_text(event_model.get("callback_type")) or _trade_ledger_callback_type(order_record)
    event_label = to_text(event_model.get("event_label")) or status_text
    broker_order_id = to_text(_record_or_extra_value(order_record, "broker_order_id", "order_id", "orderId"))
    signal_id = to_text(_record_or_extra_value(order_record, "signal_id"))
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    direction = to_text(_record_or_extra_value(order_record, "position_side", "direction", "side"))
    direction_display = _direction_display(direction)
    order_type = to_text(_record_or_extra_value(order_record, "order_type", "orderType"))
    callback_time = to_text(_record_or_extra_value(order_record, "broker_callback_received_at", "us_time", "order_time", "fill_time"))
    fill_delta = to_float(event_model.get("fill_delta")) or 0.0
    filled_qty = to_float(event_model.get("filled_qty"))
    previous_filled_qty = to_float(event_model.get("previous_filled_qty"))
    fill_line = (
        f"**成交增量 / 累计**: +{_format_quantity(fill_delta)} / {_format_quantity(filled_qty)}"
        if fill_delta > 0
        else f"**成交数量**: {_format_quantity(filled_qty)}"
    )
    if previous_filled_qty and previous_filled_qty > 0 and fill_delta > 0:
        fill_line = f"{fill_line}（前次 {_format_quantity(previous_filled_qty)}）"
    pnl_model = _trade_ledger_exit_pnl_model(order_record, event_model, related_rows=related_rows)
    exit_model = _exit_reason_model(order_record, related_rows=related_rows)
    display_role_label = to_text((exit_model or {}).get("label")) or _trade_ledger_role_label(order_record)
    if pnl_model and exit_model:
        pnl_model = {**pnl_model, "exit_label": display_role_label}
    pnl_line = _realized_pnl_line(pnl_model)
    title_pnl = f" · {_pnl_outcome_label(pnl_model.get('value'))} {_format_signed_money(pnl_model.get('value'))}" if pnl_model else ""
    price_context = _trade_ledger_price_context(order_record, related_rows=related_rows, pnl_model=pnl_model)

    body_lines = [
        f"**回调判定**: {event_label}",
    ]
    decision_line = _trade_ledger_decision_line(order_record, event_model)
    if decision_line:
        body_lines.append(f"**判定依据**: {decision_line}")
    body_lines.extend(
        [
            f"**回调类型**: {callback_type}",
            f"**状态**: {status_text}",
            f"**Symbol / Broker**: {symbol} / {broker_badge}",
            f"**交易方向**: {direction_display}",
            f"**Broker订单ID**: {broker_order_id or '-'}",
            f"**信号ID / 交易组**: {signal_id or '-'} / {trade_group_id or '-'}",
            f"**角色 / 类型**: {display_role_label} / {order_type or '-'}",
            f"**数量 / 已成交**: {_format_quantity(_record_or_extra_value(order_record, 'quantity'))} / {_format_quantity(filled_qty)}",
            f"**均价 / 最新成交价**: {_format_price(_trade_ledger_fill_price(order_record))} / {_format_price(_record_or_extra_value(order_record, 'last_fill_price', 'lastFillPrice', 'execution_price'))}",
            f"**入场价格**: {_format_price(price_context.get('entry_price'))}",
            f"**出场价格**: {_format_price(price_context.get('exit_price'))}",
            f"**本次成交价**: {_format_price(price_context.get('latest_fill_price'))}",
            f"**PnL计算数量**: {_format_quantity(price_context.get('pnl_quantity'))}",
            fill_line,
        ]
    )
    if event_model.get("reason") == "close_order_cancelled_incomplete":
        close_remaining_qty = to_float(event_model.get("close_remaining_qty")) or 0.0
        body_lines.insert(
            0,
            f"**⚠️ 平仓未完成**: 平仓单已取消但未完全成交，剩余 {_format_quantity(close_remaining_qty)} 可能仍是持仓，请重新提交限价平仓或改到更容易成交的价格。",
        )
    if exit_model:
        exit_code = to_text(exit_model.get("code"))
        exit_label = to_text(exit_model.get("label"))
        if exit_label:
            suffix = f"（{exit_code}）" if exit_code else ""
            body_lines.append(f"**平仓原因**: {exit_label}{suffix}")
    if pnl_line:
        body_lines.append(pnl_line)
    exec_id = to_text(event_model.get("exec_id"))
    if exec_id:
        body_lines.append(f"**Exec ID**: {exec_id}")
    body_lines.append(f"**回调时间**: {callback_time or '-'}")

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(body_lines)}]
    buttons = order_view_buttons(console_base_url, order_record)
    if buttons:
        elements.extend([{"tag": "hr"}, {"tag": "action", "actions": buttons}])

    title_event_label = event_label
    if event_model.get("reason") == "close_order_cancelled_incomplete":
        title_event_label = f"⚠️ 平仓未完成 · {event_label}"

    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🧾 订单真实回调 · {broker_badge} · {direction_display} · {display_role_label} · {title_event_label}{title_pnl} · {symbol}",
            },
            "template": _trade_ledger_template(event_model, status, pnl_model.get("value") if pnl_model else None),
        },
        "elements": elements,
    }


def _trade_ledger_notification_patch(
    order_record: dict[str, Any],
    *,
    event_model: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    success = bool(result.get("success") or result.get("ok"))
    notified_keys = list(_trade_ledger_notified_keys(order_record.get("extra")))
    notify_key = to_text(event_model.get("notify_key"))
    if success and notify_key and notify_key not in notified_keys:
        notified_keys.append(notify_key)
    if len(notified_keys) > 50:
        notified_keys = notified_keys[-50:]
    patch: dict[str, Any] = {
        **ensure_object(order_record.get("extra")),
        "feishu_trade_ledger_notify_key": notify_key,
        "feishu_trade_ledger_notified_keys": notified_keys,
        "feishu_trade_ledger_last_result": "success" if success else "failed",
        "feishu_trade_ledger_last_at_ms": now_ms,
        "feishu_trade_ledger_last_reason": to_text(event_model.get("reason")),
        "feishu_trade_ledger_error": "" if success else to_text(result.get("error") or "unknown_error"),
    }
    exec_id = to_text(event_model.get("exec_id"))
    if exec_id:
        patch["feishu_trade_ledger_last_exec_id"] = exec_id
    message_id = to_text(result.get("message_id"))
    if message_id:
        patch["feishu_trade_ledger_message_id"] = message_id
    for source_key, target_key in (
        ("http_status", "feishu_trade_ledger_http_status"),
        ("api_code", "feishu_trade_ledger_api_code"),
        ("api_message", "feishu_trade_ledger_api_message"),
        ("response_body", "feishu_trade_ledger_response_body"),
    ):
        if result.get(source_key) not in (None, ""):
            patch[target_key] = result.get(source_key)
    return patch


def sync_order_callback_ledger_notification(
    pb: Any,
    order_record: dict[str, Any],
    *,
    previous_order: dict[str, Any] | None = None,
    send_interactive: Any = None,
    trade_ledger_chat_id: str = "",
    console_base_url: str = "",
) -> dict[str, Any]:
    if not isinstance(order_record, dict) or not order_record:
        return {"success": False, "skipped": True, "reason": "missing_order"}

    event_model = _trade_ledger_event_model(order_record, previous_order)
    if bool(event_model.get("skipped")):
        return {"success": False, "skipped": True, **event_model}

    current_extra = ensure_object(order_record.get("extra"))
    notify_key = to_text(event_model.get("notify_key"))
    notified_keys = _trade_ledger_notified_keys(current_extra)
    last_result_success = current_extra.get("feishu_trade_ledger_last_result") == "success"
    if notify_key in notified_keys and last_result_success:
        return {"success": True, "skipped": True, "reason": "already_notified", "notify_key": notify_key}
    exec_id = to_text(event_model.get("exec_id"))
    if (
        to_text(event_model.get("event_type")) == "fill"
        and exec_id
        and last_result_success
        and to_text(current_extra.get("feishu_trade_ledger_last_exec_id")) == exec_id
    ):
        return {"success": True, "skipped": True, "reason": "already_notified_exec", "notify_key": notify_key}
    if not callable(send_interactive) or not trade_ledger_chat_id:
        return {"success": False, "skipped": True, "reason": "missing_send_target", "notify_key": notify_key}

    related_rows = _trade_ledger_related_rows_for_pnl(pb, order_record, event_model)
    card = build_order_callback_ledger_card(
        order_record,
        event_model,
        console_base_url=console_base_url,
        related_rows=related_rows,
    )
    try:
        result = dict(send_interactive(card, trade_ledger_chat_id, event_model.get("environment") or "live") or {})
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    extra_patch = _trade_ledger_notification_patch(order_record, event_model=event_model, result=result)
    _apply_order_notification_patch(pb, order_record, extra_patch)
    return {
        **result,
        "message_id": to_text(result.get("message_id")),
        "notify_key": notify_key,
        "event_type": event_model.get("event_type"),
        "reason": event_model.get("reason"),
        "extra_patch": extra_patch,
    }


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
    signal_id = to_text(_record_or_extra_value(order_record, "signal_id"))
    trade_group_id = to_text(_record_or_extra_value(order_record, "trade_group_id", "entry_order_unique_id"))
    role = to_text(_record_or_extra_value(order_record, "role"))
    order_type = to_text(_record_or_extra_value(order_record, "order_type"))
    direction = to_text(_record_or_extra_value(order_record, "position_side", "direction", "side", "action"))
    direction_display = _direction_display(direction)
    quantity = to_float(_record_or_extra_value(order_record, "quantity"))
    filled_qty = to_float(_record_or_extra_value(order_record, "filled_qty"))
    remaining_qty = to_float(_record_or_extra_value(order_record, "remaining_qty", "remaining"))
    if remaining_qty is None and quantity is not None and filled_qty is not None:
        remaining_qty = max(0.0, quantity - filled_qty)
    pnl_line = _realized_pnl_line(_single_order_pnl_model(order_record))
    protection_line = _protection_status_line([order_record])
    reason = to_text(_record_or_extra_value(order_record, "status_reason", "reason", "message", "broker_last_error", "order_error"))

    body_lines = [
        f"**状态**: {status_text}",
        f"**Symbol**: {symbol}",
        f"**Broker**: {broker_badge}",
        f"**交易方向**: {direction_display}",
        f"**Broker订单ID**: {order_id or '-'}",
        f"**信号ID / 交易组**: {signal_id or '-'} / {trade_group_id or '-'}",
        f"**角色 / 类型**: {role or '-'} / {order_type or '-'}",
        f"**数量 / 已成交 / 剩余**: {_format_quantity(quantity)} / {_format_quantity(filled_qty)} / {_format_quantity(remaining_qty)}",
        f"**均价 / 成交价**: {_format_price(_record_or_extra_value(order_record, 'avg_price', 'avg_fill_price'))} / {_format_price(_record_or_extra_value(order_record, 'fill_price', 'last_fill_price', 'execution_price', 'price', 'limit_price'))}",
    ]
    if protection_line:
        body_lines.append(protection_line)
    if reason:
        body_lines.append(f"**原因**: {reason}")
    if pnl_line:
        body_lines.append(pnl_line)
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
                "content": f"📦 订单状态 · {broker_badge} · {direction_display} · {status_text} · {symbol}",
            },
            "template": template,
        },
        "elements": elements,
    }


__all__ = [
    "build_order_callback_ledger_card",
    "build_order_group_status_card",
    "build_order_status_card",
    "order_view_buttons",
    "sync_order_callback_ledger_notification",
    "sync_order_status_notification",
]
