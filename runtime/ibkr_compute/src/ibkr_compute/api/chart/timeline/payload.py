from __future__ import annotations

import json
from datetime import datetime, timezone

from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

from ibkr_compute.api.chart.timeline.rows import (
    build_chart_indicator_row,
    build_chart_signal_row,
    build_chart_trace_row,
)
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import (
    build_chart_source_window_from_rows,
    load_chart_timeline_source_bars,
)

BACKTEST_TRADE_COLLECTION = "ibkr_backtest_trades"
BACKTEST_SIGNAL_COLLECTION = "ibkr_backtest_signals"
TV_EVENT_COLLECTION = "tv_webhook_events"
ORDER_COLLECTION = "orders"


def _parse_extra(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _escape_filter_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _coerce_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _coerce_float(value, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _timestamp_ms(value) -> int:
    if value in (None, ""):
        return 0
    try:
        number = float(value)
        if number > 1_000_000_000_000:
            return int(number)
        if number > 1_000_000_000:
            return int(number * 1000)
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_number(*values) -> float | None:
    for value in values:
        parsed = _coerce_float(value, None)
        if parsed is not None:
            return parsed
    return None


def _load_collection_records(collection: str, filter_expr: str, sort: str = "bar_time_ms", max_pages: int = 20) -> list[dict]:
    api_app = _api_app()
    pb = getattr(api_app, "pb", None)
    if pb is None:
        return []
    try:
        if hasattr(pb, "get_all_records"):
            rows = pb.get_all_records(collection, filter=filter_expr, sort=sort, max_pages=max_pages)
        elif hasattr(pb, "get_records"):
            rows = pb.get_records(collection, filter=filter_expr, sort=sort, per_page=200, page=1)
        else:
            rows = []
    except Exception:
        return []
    return [dict(row) for row in (rows or []) if isinstance(row, dict)]


def _row_time_ms(row: dict) -> int:
    return (
        _coerce_int(row.get("bar_time_ms"), 0)
        or _coerce_int(row.get("time_ms"), 0)
        or _timestamp_ms(row.get("created"))
        or _timestamp_ms(row.get("updated"))
    )


def _within_window(row: dict, start_ms: int, end_ms: int) -> bool:
    bar_ms = _row_time_ms(row)
    if bar_ms <= 0:
        return False
    if start_ms > 0 and bar_ms < start_ms:
        return False
    if end_ms > 0 and bar_ms > end_ms:
        return False
    return True


def _environment_matches(row: dict, runtime_environment: str, data_environment: str) -> bool:
    allowed = {str(runtime_environment or "").strip().lower(), str(data_environment or "").strip().lower()}
    allowed.discard("")
    row_environment = str(row.get("environment") or "").strip().lower()
    row_broker_mode = str(row.get("broker_mode") or "").strip().lower()
    if not row_environment and not row_broker_mode:
        return True
    return row_environment in allowed or row_broker_mode in allowed


def _order_extra(row: dict | None) -> dict:
    return _parse_extra((row or {}).get("extra"))


def _order_role(row: dict | None) -> str:
    row = row or {}
    extra = _order_extra(row)
    return str(row.get("role") or extra.get("role") or row.get("order_type") or "").strip().lower()


def _order_status(row: dict | None) -> str:
    return str((row or {}).get("status") or (row or {}).get("relation_status") or "").strip().upper()


def _order_is_filled(row: dict | None) -> bool:
    filled_qty = _coerce_float((row or {}).get("filled_qty"), 0.0) or 0.0
    return filled_qty > 0 or _order_status(row) in {"FILLED", "EXECUTED", "CLOSED"}


def _order_group_key(row: dict | None) -> str:
    row = row or {}
    extra = _order_extra(row)
    return str(
        row.get("trade_group_id")
        or row.get("entry_order_unique_id")
        or extra.get("trade_group_id")
        or extra.get("entry_order_unique_id")
        or row.get("parent_order_unique_id")
        or extra.get("parent_order_unique_id")
        or ""
    ).strip()


def _order_price(row: dict | None, *, role: str = "") -> float | None:
    row = row or {}
    extra = _order_extra(row)
    result = extra.get("market_close_result")
    result = result if isinstance(result, dict) else {}
    role = str(role or _order_role(row)).strip().lower()
    if role == "entry":
        return _first_number(
            row.get("filled_avg_price"),
            row.get("avg_fill_price"),
            row.get("filled_price"),
            row.get("fill_price"),
            row.get("entry_price"),
            extra.get("entry_price"),
            row.get("limit_price"),
            extra.get("limit_price"),
        )
    return _first_number(
        row.get("realized_exit_price"),
        row.get("exit_price"),
        extra.get("exit_price"),
        row.get("filled_avg_price"),
        row.get("avg_fill_price"),
        row.get("filled_price"),
        row.get("fill_price"),
        result.get("avg_fill_price"),
        result.get("average_price"),
        result.get("avg_price"),
        result.get("filled_price"),
        result.get("fill_price"),
        result.get("limit_price"),
        row.get("limit_price"),
        extra.get("limit_price"),
    )


def _directional_pnl(direction: str, entry_price: float | None, exit_price: float | None, quantity: float | None) -> float | None:
    if not entry_price or not exit_price or not quantity:
        return None
    if str(direction or "").strip().lower() == "short":
        return (entry_price - exit_price) * quantity
    return (exit_price - entry_price) * quantity


def _order_event_type(row: dict) -> str:
    role = _order_role(row)
    extra = _order_extra(row)
    source_text = " ".join(
        str(value or "").strip().lower()
        for value in (
            role,
            row.get("order_type"),
            row.get("status"),
            row.get("relation_status"),
            extra.get("source"),
            extra.get("reason"),
            extra.get("exit_reason"),
            extra.get("submitted_via"),
        )
        if str(value or "").strip()
    )
    if role == "entry":
        return "live_entry"
    if role in {"take_profit", "tp"}:
        return "live_exit_tp"
    if role in {"stop_loss", "sl"}:
        return "live_exit_sl"
    if "eod" in source_text or "force_flat" in source_text:
        return "live_exit_eod"
    if role in {"close", "manual_close", "market_close", "close_order", "reverse_close"}:
        return "live_exit"
    return ""


def _normalize_tv_event(row: dict) -> dict:
    payload = _parse_extra(row.get("payload"))
    extra = _parse_extra(row.get("extra"))
    event_type = _first_text(row.get("event_type"), payload.get("event_type"), extra.get("event_type"))
    direction = _first_text(
        row.get("direction"),
        row.get("position_side"),
        payload.get("direction"),
        payload.get("direction_bias"),
        payload.get("candidate_direction"),
    ).lower()
    price = _first_number(
        payload.get("exit_price"),
        payload.get("entry_price"),
        payload.get("entry"),
        payload.get("close"),
        extra.get("exit_price"),
        extra.get("entry_price"),
        extra.get("close"),
    )
    exit_reason = _first_text(row.get("exit_reason"), payload.get("exit_reason"), extra.get("exit_reason"))
    exit_fill_role = _first_text(
        row.get("exit_fill_role"),
        payload.get("exit_fill_role"),
        payload.get("fill_role"),
        extra.get("exit_fill_role"),
        extra.get("fill_role"),
    )
    reason = _first_text(row.get("reason"), payload.get("reason"), extra.get("reason"), exit_reason)
    return {
        "event_type": event_type,
        "bar_time_ms": _row_time_ms(row),
        "us_time": _first_text(row.get("us_time"), payload.get("us_time"), extra.get("us_time")),
        "symbol": _first_text(row.get("symbol"), payload.get("symbol")).upper(),
        "direction": direction if direction in {"long", "short", "neutral"} else "",
        "signal_id": _first_text(row.get("signal_id"), payload.get("signal_id"), extra.get("signal_id")),
        "position_id": _first_text(row.get("position_id"), payload.get("position_id"), extra.get("position_id")),
        "setup": _first_text(payload.get("candidate_setup"), payload.get("setup"), payload.get("entry_setup"), extra.get("setup")),
        "status": _first_text(row.get("status"), extra.get("status")),
        "route_target": _first_text(row.get("route_target")),
        "route_record_id": _first_text(row.get("route_record_id")),
        "price": round(float(price), 4) if price is not None else 0,
        "reason": reason,
        "exit_reason": exit_reason,
        "exit_fill_role": exit_fill_role,
        "activity_score": _coerce_float(payload.get("activity_score"), _coerce_float(extra.get("activity_score"), 0.0)),
        "quality_score": _coerce_float(payload.get("quality_score"), _coerce_float(extra.get("quality_score"), 0.0)),
        "mtf_status": _first_text(payload.get("mtf_status"), extra.get("mtf_last_status"), extra.get("mtf_status")),
        "mtf_score": _coerce_float(payload.get("mtf_score"), _coerce_float(extra.get("mtf_last_score"), 0.0)),
        "source": TV_EVENT_COLLECTION,
        "extra": {
            "activity_grade": _first_text(payload.get("activity_grade"), extra.get("activity_grade")),
            "target_status": _first_text(row.get("route_target")) == "ibkr_targets" and _first_text(row.get("status")) or "",
            "missing_components": _first_text(payload.get("missing_components"), extra.get("missing_components")),
            "reason": reason,
            "exit_reason": exit_reason,
            "exit_fill_role": exit_fill_role,
        },
    }


def _normalize_order_event(row: dict, entry_by_group: dict[str, dict]) -> dict:
    role = _order_role(row)
    event_type = _order_event_type(row)
    extra = _order_extra(row)
    group_key = _order_group_key(row)
    entry_row = entry_by_group.get(group_key) or {}
    entry_extra = _order_extra(entry_row)
    direction = _first_text(row.get("direction"), row.get("position_side"), entry_row.get("direction"), entry_row.get("position_side")).lower()
    quantity = _first_number(row.get("filled_qty"), row.get("quantity"), entry_row.get("filled_qty"), entry_row.get("quantity")) or 0.0
    price = _order_price(row, role=role)
    entry_price = _order_price(entry_row, role="entry")
    pnl = _first_number(row.get("realized_net_pnl"), row.get("realized_pnl"), row.get("realized_gross_pnl"), row.get("pnl"), extra.get("pnl"))
    if pnl is None and event_type.startswith("live_exit"):
        pnl = _directional_pnl(direction, entry_price, price, quantity)
    pnl_pct = _first_number(row.get("pnl_pct"), extra.get("pnl_pct"))
    if pnl_pct is None and pnl is not None and entry_price and quantity:
        pnl_pct = pnl / (entry_price * quantity) * 100.0
    return {
        "event_type": event_type,
        "bar_time_ms": _row_time_ms(row),
        "us_time": _first_text(row.get("us_time"), extra.get("us_time")),
        "symbol": _first_text(row.get("symbol"), row.get("ticker")).upper(),
        "direction": direction if direction in {"long", "short"} else "",
        "signal_id": _first_text(row.get("signal_id"), extra.get("signal_id"), entry_row.get("signal_id"), entry_extra.get("signal_id")),
        "trade_group_id": group_key,
        "entry_order_unique_id": _first_text(row.get("entry_order_unique_id"), extra.get("entry_order_unique_id"), entry_row.get("unique_id")),
        "role": role,
        "status": _first_text(row.get("status"), row.get("relation_status")),
        "price": round(float(price), 4) if price is not None else 0,
        "entry_price": round(float(entry_price), 4) if entry_price is not None else 0,
        "quantity": round(float(quantity), 4),
        "pnl": round(float(pnl), 4) if pnl is not None else None,
        "pnl_pct": round(float(pnl_pct), 4) if pnl_pct is not None else None,
        "reason": _first_text(extra.get("exit_reason"), extra.get("reason"), extra.get("source"), event_type),
        "source": ORDER_COLLECTION,
        "extra": {
            "submitted_via": _first_text(extra.get("submitted_via")),
            "source": _first_text(extra.get("source")),
        },
    }


def _load_backtest_records(collection: str, run_id: str, symbol: str, start_ms: int, end_ms: int, time_field: str) -> list[dict]:
    api_app = _api_app()
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "get_all_records"):
        return []
    filter_parts = [
        f'run_id = "{_escape_filter_string(run_id)}"',
        f'symbol = "{_escape_filter_string(symbol)}"',
    ]
    if start_ms > 0:
        filter_parts.append(f"{time_field} >= {int(start_ms)}")
    if end_ms > 0:
        filter_parts.append(f"{time_field} <= {int(end_ms)}")
    return list(
        pb.get_all_records(
            collection,
            filter=" && ".join(filter_parts),
            sort=time_field,
            max_pages=20,
        )
        or []
    )


def load_tv_chart_events(environment: str, data_environment: str, symbol: str, start_ms: int = 0, end_ms: int = 0) -> list[dict]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return []
    filter_parts = [f'symbol = "{_escape_filter_string(normalized_symbol)}"']
    if start_ms > 0:
        filter_parts.append(f"bar_time_ms >= {int(start_ms)}")
    if end_ms > 0:
        filter_parts.append(f"bar_time_ms <= {int(end_ms)}")
    rows = _load_collection_records(TV_EVENT_COLLECTION, " && ".join(filter_parts), sort="bar_time_ms", max_pages=20)
    events = []
    for row in rows:
        if not _environment_matches(row, environment, data_environment):
            continue
        event_type = str(row.get("event_type") or _parse_extra(row.get("payload")).get("event_type") or "").strip().lower()
        if event_type in {"", "heartbeat"}:
            continue
        if not _within_window(row, start_ms, end_ms):
            continue
        event = _normalize_tv_event(row)
        if event.get("bar_time_ms"):
            events.append(event)
    events.sort(key=lambda item: (int(item.get("bar_time_ms") or 0), str(item.get("event_type") or "")))
    return events


def load_order_chart_events(environment: str, symbol: str, start_ms: int = 0, end_ms: int = 0) -> list[dict]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return []
    filter_parts = [f'symbol = "{_escape_filter_string(normalized_symbol)}"']
    if str(environment or "").strip():
        filter_parts.append(f'environment = "{_escape_filter_string(environment)}"')
    if end_ms > 0:
        filter_parts.append(f"bar_time_ms <= {int(end_ms)}")
    rows = _load_collection_records(ORDER_COLLECTION, " && ".join(filter_parts), sort="bar_time_ms", max_pages=30)
    entry_by_group: dict[str, dict] = {}
    for row in rows:
        if _order_role(row) != "entry":
            continue
        group_key = _order_group_key(row)
        if not group_key:
            continue
        existing = entry_by_group.get(group_key)
        if existing is None or _row_time_ms(row) >= _row_time_ms(existing):
            entry_by_group[group_key] = row
    events = []
    for row in rows:
        event_type = _order_event_type(row)
        if not event_type or not _within_window(row, start_ms, end_ms):
            continue
        if not _order_is_filled(row):
            continue
        event = _normalize_order_event(row, entry_by_group)
        if event.get("bar_time_ms"):
            events.append(event)
    events.sort(key=lambda item: (int(item.get("bar_time_ms") or 0), str(item.get("event_type") or "")))
    return events


def _backtest_exit_event_type(exit_reason: str) -> str:
    reason = str(exit_reason or "").strip().lower()
    if reason == "take_profit":
        return "exit_take_profit"
    if reason == "stop_loss":
        return "exit_stop_loss"
    if reason.startswith("reverse_"):
        return "exit_reverse"
    if reason in {"eod", "force_flat_eod"}:
        return "exit_eod"
    if reason == "last_bar":
        return "exit_last_bar"
    return f"exit_{reason}" if reason else "exit"


def _risk_stats_key(symbol: str, direction: str, signal: str) -> str:
    return "|".join(
        [
            str(symbol or "").strip().upper(),
            str(direction or "").strip().lower(),
            str(signal or "").strip(),
        ]
    )


def load_backtest_risk_stats(run_id: str, symbol: str) -> dict:
    safe_run_id = str(run_id or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    if not safe_run_id or not normalized_symbol:
        return {}

    rows = _load_backtest_records(
        BACKTEST_TRADE_COLLECTION,
        safe_run_id,
        normalized_symbol,
        0,
        0,
        "entry_bar_ms",
    )
    buckets: dict[str, dict] = {}
    for row in rows:
        row_symbol = str(row.get("symbol", "") or "").strip().upper()
        direction = str(row.get("direction", "") or "").strip().lower()
        signal = str(row.get("signal", "") or "").strip()
        if not row_symbol or not direction or not signal:
            continue
        key = _risk_stats_key(row_symbol, direction, signal)
        bucket = buckets.setdefault(
            key,
            {
                "symbol": row_symbol,
                "direction": direction,
                "signal": signal,
                "sample_count": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": None,
                "avg_pnl_pct": None,
                "source_run_id": safe_run_id,
                "status": "insufficient_sample",
            },
        )
        pnl = round(float(row.get("pnl", 0) or 0), 4)
        pnl_pct = round(float(row.get("pnl_pct", 0) or 0), 4)
        bucket["sample_count"] += 1
        if pnl > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket.setdefault("_pnl_pct_sum", 0.0)
        bucket["_pnl_pct_sum"] += pnl_pct

    for bucket in buckets.values():
        sample_count = int(bucket.get("sample_count", 0) or 0)
        wins = int(bucket.get("wins", 0) or 0)
        if sample_count > 0:
            bucket["win_rate"] = round(wins / sample_count * 100.0, 2)
            bucket["avg_pnl_pct"] = round(float(bucket.get("_pnl_pct_sum", 0.0)) / sample_count, 4)
            bucket["status"] = "ok" if sample_count >= 10 else "low_sample"
        bucket.pop("_pnl_pct_sum", None)
    return buckets


def _build_backtest_trade_events(run_id: str, row: dict) -> list[dict]:
    extra = _parse_extra(row.get("extra"))
    base = {
        "run_id": run_id,
        "symbol": str(row.get("symbol", "") or "").strip().upper(),
        "direction": str(row.get("direction", "") or "").strip().lower(),
        "signal": str(row.get("signal", "") or ""),
        "signal_id": str(row.get("signal_id", "") or ""),
        "trade_index": int(row.get("trade_index", 0) or 0),
        "shares": int(row.get("shares", 0) or 0),
        "pnl": round(float(row.get("pnl", 0) or 0), 4),
        "pnl_pct": round(float(row.get("pnl_pct", 0) or 0), 4),
        "exit_reason": str(row.get("exit_reason", "") or ""),
        "source": "ibkr_backtest_trades",
    }
    events = []
    entry_ms = int(row.get("entry_bar_ms", 0) or 0)
    if entry_ms > 0:
        events.append(
            {
                **base,
                "event_type": "entry_filled",
                "bar_time_ms": entry_ms,
                "us_time": str(row.get("entry_us_time", "") or ""),
                "price": round(float(row.get("entry_price", 0) or 0), 4),
                "extra": {
                    "entry_price": round(float(row.get("entry_price", 0) or 0), 4),
                    "exit_price": round(float(row.get("exit_price", 0) or 0), 4),
                    **extra,
                },
            }
        )
    exit_ms = int(row.get("exit_bar_ms", 0) or 0)
    if exit_ms > 0:
        events.append(
            {
                **base,
                "event_type": _backtest_exit_event_type(str(row.get("exit_reason", "") or "")),
                "bar_time_ms": exit_ms,
                "us_time": str(row.get("exit_us_time", "") or ""),
                "price": round(float(row.get("exit_price", 0) or 0), 4),
                "extra": {
                    "entry_price": round(float(row.get("entry_price", 0) or 0), 4),
                    "exit_price": round(float(row.get("exit_price", 0) or 0), 4),
                    **extra,
                },
            }
        )
    return events


def _build_backtest_signal_event(run_id: str, row: dict) -> dict:
    extra = _parse_extra(row.get("extra"))
    status = str(row.get("status", "") or "generated").strip().lower() or "generated"
    return {
        "run_id": run_id,
        "event_type": f"signal_{status}",
        "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
        "us_time": str(row.get("us_time", "") or ""),
        "symbol": str(row.get("symbol", "") or "").strip().upper(),
        "direction": str(row.get("direction", "") or "").strip().lower(),
        "signal": str(row.get("signal", "") or ""),
        "signal_id": str(row.get("signal_id", "") or ""),
        "status": status,
        "reason": str(extra.get("signal_status_reason") or row.get("reason", "") or ""),
        "price": round(float(row.get("entry", 0) or 0), 4),
        "shares": int(row.get("shares", 0) or 0),
        "source": "ibkr_backtest_signals",
        "extra": {
            "entry": round(float(row.get("entry", 0) or 0), 4),
            "stop_loss": round(float(row.get("stop_loss", 0) or 0), 4),
            "take_profit": round(float(row.get("take_profit", 0) or 0), 4),
            **extra,
        },
    }


def load_backtest_chart_events(run_id: str, symbol: str, interval: str, start_ms: int = 0, end_ms: int = 0) -> list[dict]:
    safe_run_id = str(run_id or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    if not safe_run_id or not normalized_symbol or normalize_interval(interval) != "5m":
        return []
    safe_start_ms = int(start_ms or 0)
    safe_end_ms = int(end_ms or 0)
    trade_rows = _load_backtest_records(
        BACKTEST_TRADE_COLLECTION,
        safe_run_id,
        normalized_symbol,
        safe_start_ms,
        safe_end_ms,
        "entry_bar_ms",
    )
    exit_trade_rows = _load_backtest_records(
        BACKTEST_TRADE_COLLECTION,
        safe_run_id,
        normalized_symbol,
        safe_start_ms,
        safe_end_ms,
        "exit_bar_ms",
    )
    signal_rows = _load_backtest_records(
        BACKTEST_SIGNAL_COLLECTION,
        safe_run_id,
        normalized_symbol,
        safe_start_ms,
        safe_end_ms,
        "bar_time_ms",
    )
    trades_by_key = {}
    for row in [*trade_rows, *exit_trade_rows]:
        key = str(row.get("id") or row.get("trade_index") or f"{row.get('entry_bar_ms')}-{row.get('exit_bar_ms')}")
        trades_by_key[key] = row
    events = []
    for row in trades_by_key.values():
        events.extend(_build_backtest_trade_events(safe_run_id, row))
    events.extend(_build_backtest_signal_event(safe_run_id, row) for row in signal_rows)
    clipped_events = [
        event
        for event in events
        if int(event.get("bar_time_ms", 0) or 0) > 0
        and (safe_start_ms <= 0 or int(event.get("bar_time_ms", 0) or 0) >= safe_start_ms)
        and (safe_end_ms <= 0 or int(event.get("bar_time_ms", 0) or 0) <= safe_end_ms)
    ]
    return sorted(
        clipped_events,
        key=lambda item: (
            int(item.get("bar_time_ms", 0) or 0),
            str(item.get("symbol", "") or ""),
            str(item.get("event_type", "") or ""),
        ),
    )


def _normalize_preview_bar(preview_bar: dict | None, symbol: str, interval: str) -> dict | None:
    if not isinstance(preview_bar, dict):
        return None
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    bar_ms = int(preview_bar.get("bar_time_ms", 0) or 0)
    if bar_ms <= 0:
        return None
    return {
        **preview_bar,
        "symbol": normalized_symbol,
        "interval": normalized_interval,
        "bar_time_ms": bar_ms,
        "open": round(float(preview_bar.get("open", 0) or 0), 4),
        "high": round(float(preview_bar.get("high", 0) or 0), 4),
        "low": round(float(preview_bar.get("low", 0) or 0), 4),
        "close": round(float(preview_bar.get("close", 0) or 0), 4),
        "volume": round(float(preview_bar.get("volume", 0) or 0), 4),
        "us_time": str(preview_bar.get("us_time", "") or ""),
        "cn_time": str(preview_bar.get("cn_time", "") or ""),
        "session_type": str(preview_bar.get("session_type", "regular") or "regular"),
        "exchange": str(preview_bar.get("exchange", "") or "").upper(),
        "preview": True,
        "is_preview": True,
    }


def build_chart_timeline_payload_from_source(
    environment: str,
    symbol: str,
    interval: str,
    source: dict,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
    include_trace: bool = False,
    backtest_run_id: str = "",
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    data_environment = resolve_data_environment(runtime_environment)
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    source_rows = source.get("source_rows") or []
    visible_rows = source.get("visible_rows") or []
    source_meta = source.get("meta") or {}

    if not visible_rows:
        payload = {
            "ok": True,
            "bars": [],
            "indicator_timeline": [],
            "latest_indicator": None,
            "signals": [],
            "trace_timeline": [],
            "tv_events": [],
            "order_events": [],
            "meta": {
                "environment": runtime_environment,
                "broker_mode": runtime_environment,
                "data_environment": data_environment,
                "shared_market_data": data_environment == "live",
                "symbol": normalized_symbol,
                "interval": chart_tf,
                "start_ms": int(start_ms or 0),
                "end_ms": int(end_ms or 0),
                "visible_bar_count": 0,
                "source_bar_count": len(source_rows),
                "warmup_bars": int(source.get("warmup_limit", 0) or 0),
                "warmup_used": int(source.get("warmup_used", 0) or 0),
                "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
                "trace_mode": "computed" if include_trace else "disabled",
                "tv_event_count": 0,
                "order_event_count": 0,
                "reason": "no_visible_bars",
                **source_meta,
            },
        }
        if str(backtest_run_id or "").strip():
            payload["backtest_events"] = []
            payload["risk_stats"] = {}
            payload["meta"]["backtest_run_id"] = str(backtest_run_id or "").strip()
            payload["meta"]["backtest_event_count"] = 0
            payload["meta"]["risk_stats_count"] = 0
        return payload

    timeline = build_runtime_timeline(
        normalized_symbol,
        normalized_interval,
        source_rows,
        params=api_app.get_signal_generator_params(runtime_environment),
        include_signals=include_signals,
        include_trace=include_trace,
        visible_start_ms=int(start_ms or 0),
        visible_end_ms=int(end_ms or 0),
    )
    timeline_rows = timeline.get("rows") or []
    bars = [
        {
            "environment": runtime_environment,
            "broker_mode": runtime_environment,
            "data_environment": data_environment,
            "shared_market_data": data_environment == "live",
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "exchange": str(row.get("exchange", "") or "").upper(),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": row.get("us_time", ""),
            "cn_time": row.get("cn_time", ""),
            "session_type": row.get("session_type", "regular"),
            "open": round(float(row.get("open", 0) or 0), 4),
            "high": round(float(row.get("high", 0) or 0), 4),
            "low": round(float(row.get("low", 0) or 0), 4),
            "close": round(float(row.get("close", 0) or 0), 4),
            "volume": round(float(row.get("volume", 0) or 0), 4),
            "preview": bool(row.get("preview") or row.get("is_preview")),
            "is_preview": bool(row.get("preview") or row.get("is_preview")),
        }
        for row in timeline_rows
    ]
    indicators = [
        build_chart_indicator_row(runtime_environment, normalized_symbol, normalized_interval, row)
        for row in timeline_rows
    ]
    signals = []
    if include_signals and normalized_interval == "5m":
        signals = [
            signal
            for signal in (
                build_chart_signal_row(runtime_environment, normalized_symbol, normalized_interval, row)
                for row in timeline_rows
            )
            if signal
        ]
    trace_timeline = []
    if include_trace:
        trace_timeline = [
            build_chart_trace_row(runtime_environment, normalized_symbol, normalized_interval, row)
            for row in timeline_rows
        ]

    backtest_events = load_backtest_chart_events(
        backtest_run_id,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    ) if str(backtest_run_id or "").strip() else []
    risk_stats = load_backtest_risk_stats(
        backtest_run_id,
        normalized_symbol,
    ) if str(backtest_run_id or "").strip() and normalized_interval == "5m" else {}
    tv_events = load_tv_chart_events(
        runtime_environment,
        data_environment,
        normalized_symbol,
        start_ms=start_ms,
        end_ms=end_ms,
    ) if normalized_interval == "5m" else []
    order_events = load_order_chart_events(
        runtime_environment,
        normalized_symbol,
        start_ms=start_ms,
        end_ms=end_ms,
    ) if normalized_interval == "5m" else []
    payload = {
        "ok": True,
        "bars": bars,
        "indicator_timeline": indicators,
        "latest_indicator": indicators[-1] if indicators else None,
        "signals": signals,
        "trace_timeline": trace_timeline,
        "tv_events": tv_events,
        "order_events": order_events,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "visible_bar_count": len(bars),
            "source_bar_count": len(source_rows),
            "warmup_bars": int(source.get("warmup_limit", 0) or 0),
            "warmup_used": int(source.get("warmup_used", 0) or 0),
            "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
            "trace_mode": "computed" if include_trace else "disabled",
            "tv_event_count": len(tv_events),
            "order_event_count": len(order_events),
            **source_meta,
        },
    }
    if str(backtest_run_id or "").strip():
        payload["backtest_events"] = backtest_events
        payload["risk_stats"] = risk_stats
        payload["meta"]["backtest_run_id"] = str(backtest_run_id or "").strip()
        payload["meta"]["backtest_event_count"] = len(backtest_events)
        payload["meta"]["risk_stats_count"] = len(risk_stats)
    return payload


def build_chart_timeline_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
    include_trace: bool = False,
    preview_bar: dict | None = None,
    backtest_run_id: str = "",
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    data_environment = resolve_data_environment(runtime_environment)
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    normalized_preview_bar = _normalize_preview_bar(preview_bar, normalized_symbol, normalized_interval)
    effective_end_ms = int(end_ms or 0)
    if normalized_preview_bar:
        effective_end_ms = max(effective_end_ms, int(normalized_preview_bar.get("bar_time_ms", 0) or 0))

    api_app.refresh_daily_close_cache([runtime_environment])
    source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=effective_end_ms,
    )
    chart_freshness = {}
    repair_meta = {"queued": False, "status": "not_required"}
    try:
        planner = getattr(api_app, "bar_freshness_planner", None) or BarFreshnessPlanner(
            getattr(api_app, "pb", None),
            getattr(api_app, "cfg", None),
            environment=runtime_environment,
        )
        chart_freshness = planner.plan_symbol(
            normalized_symbol,
            [normalized_interval],
            environment=runtime_environment,
            required_bars=0,
        )
        if bool(chart_freshness.get("needs_repair")):
            coordinator = getattr(api_app, "bar_repair_coordinator", None)
            if coordinator is not None and hasattr(coordinator, "enqueue_from_freshness"):
                jobs = coordinator.enqueue_from_freshness(
                    chart_freshness,
                    priority="chart_active",
                    trigger="chart_timeline",
                )
                repair_meta = {
                    "queued": any(bool((job or {}).get("queued")) for job in jobs),
                    "status": "queued" if jobs else "repairing",
                    "jobs": jobs,
                    "trigger": "chart_timeline",
                }
            else:
                repair_meta = {"queued": False, "status": "planner_only", "trigger": "chart_timeline"}
    except Exception as exc:
        chart_freshness = {"status": "unknown", "error": str(exc), "symbol": normalized_symbol, "environment": runtime_environment}
        repair_meta = {"queued": False, "status": "error", "error": str(exc)}
    if normalized_preview_bar:
        source_rows_by_ms = {
            int((row or {}).get("bar_time_ms", 0) or 0): dict(row)
            for row in (source.get("source_rows") or [])
            if int((row or {}).get("bar_time_ms", 0) or 0) > 0
        }
        source_rows_by_ms[int(normalized_preview_bar.get("bar_time_ms", 0) or 0)] = normalized_preview_bar
        source = build_chart_source_window_from_rows(
            source_rows_by_ms.values(),
            normalized_interval,
            start_ms=start_ms,
            end_ms=effective_end_ms,
        )
        source["meta"] = {
            "preview_bar": True,
            "preview_bar_time_ms": int(normalized_preview_bar.get("bar_time_ms", 0) or 0),
        }
    source["meta"] = {
        **(source.get("meta") or {}),
        "freshness": chart_freshness,
        "repair": repair_meta,
        "broker_mode": runtime_environment,
        "data_environment": data_environment,
        "shared_market_data": data_environment == "live",
        "progress": {
            "status": "completed",
            "phase": "timeline_ready",
            "percent": 100,
            "current_step": "bars_loaded_indicators_recomputed",
        },
    }
    return build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        source,
        start_ms=start_ms,
        end_ms=effective_end_ms,
        include_signals=include_signals,
        include_trace=include_trace,
        backtest_run_id=backtest_run_id,
    )


__all__ = [
    "build_chart_timeline_payload",
    "build_chart_timeline_payload_from_source",
    "load_backtest_chart_events",
    "load_order_chart_events",
    "load_tv_chart_events",
]
