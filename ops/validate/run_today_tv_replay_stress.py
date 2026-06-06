#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_paper_full_flow import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_DB_PATH,
    DEFAULT_HOST,
    ValidationError,
    as_object,
    compact_json,
    order_price,
    post_json,
    query_orders,
    query_reverses,
    query_signal,
    remote_sql,
    role_map,
    signal_rejection_reason,
    wait_for,
)

ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

DEFAULT_ARTIFACT_ROOT = Path("artifacts/validation/tv_replay_stress")
DEFAULT_PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
DEFAULT_ACCOUNT_SNAPSHOT_PATH = "/api/custom/ibkr/account_snapshot"
REPLAY_USER_AGENT = "ibkr-today-tv-replay-stress/1.0"

TOUCHED_PAPER_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled",
    "protected_active",
    "closed",
    "protection_incomplete",
}
TV_EVENT_TERMINAL_STATUSES = {"routed", "rejected", "failed"}
ORDER_TERMINAL_STATUSES = {"filled", "cancelled", "canceled", "closed", "inactive", "api_cancelled"}
SIGNAL_ATTENTION_STATUSES = {
    "expired",
    "rejected",
    "blocked",
    "cancelled",
    "canceled",
    "deferred",
    "waiting_for_capacity",
    "buying_power_blocked",
    "buying_power_unavailable",
    "order_flow_waiting",
    "pending_retry",
    "protection_incomplete",
    "submitted_waiting_fill",
    "filled",
    "closed",
}
MUTABLE_SIGNAL_STATUSES = {"awaiting_confirm", "pending", "confirm_pending"}
BROKER_CONTROLLED_REPLAY_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "executed",
    "protection_incomplete",
    "protection_reprice_failed",
}
ACTIVE_REPLAY_SIGNAL_STATUSES = MUTABLE_SIGNAL_STATUSES | BROKER_CONTROLLED_REPLAY_STATUSES
INACTIVE_SIGNAL_STATUSES = {"rejected", "expired", "closed", "cancelled", "canceled", "dropped"}
ACTIVE_POLICY_MERGE_ACTIONS = {"refreshed_active_signal", "reconfirm_same_direction_followup"}
ORDER_COMMAND_OPERATION_REGEX = (
    "place_bracket|place_entry|place_take_profit|place_stop_loss|place_bracket_order|place_market_close|"
    "adjust_stop_loss|adjust_take_profit|cancel_order|cancel_all_orders|cancel_entry|cancel_take_profit|"
    "cancel_stop_loss|modify_manual|modify_order|ibapi_place|ibapi_cancel|modify|cancel"
)


class ReplayAttemptError(ValidationError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        self.payload = dict(payload or {})
        super().__init__(message)


@dataclass
class ReplayChain:
    origin_signal_id: str
    origin_position_id: str
    symbol: str
    direction: str
    entry_event: dict[str, Any]
    pre_alert_events: list[dict[str, Any]] = field(default_factory=list)
    risk_events: list[dict[str, Any]] = field(default_factory=list)
    exit_events: list[dict[str, Any]] = field(default_factory=list)
    signal_row: dict[str, Any] = field(default_factory=dict)
    order_rows: list[dict[str, Any]] = field(default_factory=list)
    exclude_reason: str = ""
    synthetic_signal_id: str = ""
    synthetic_position_id: str = ""
    synthetic_trade_group_id: str = ""
    payloads: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sent_events: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    final_signal: dict[str, Any] = field(default_factory=dict)
    final_orders: list[dict[str, Any]] = field(default_factory=list)
    final_reverses: list[dict[str, Any]] = field(default_factory=list)
    active_conflict_signal_id: str = ""
    active_conflict_status: str = ""
    active_conflict_direction: str = ""
    classification: str = "planned"

    def public_summary(self) -> dict[str, Any]:
        return {
            "origin_signal_id": self.origin_signal_id,
            "synthetic_signal_id": self.synthetic_signal_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "origin_position_id": self.origin_position_id,
            "synthetic_position_id": self.synthetic_position_id,
            "risk_events": len(self.risk_events),
            "exit_events": len(self.exit_events),
            "pre_alert_events": len(self.pre_alert_events),
            "exclude_reason": self.exclude_reason,
            "classification": self.classification,
            "active_conflict_signal_id": self.active_conflict_signal_id,
            "active_conflict_status": self.active_conflict_status,
            "active_conflict_direction": self.active_conflict_direction,
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
    return value


def decode_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = parse_jsonish(event.get("payload"))
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    fallback = {
        "source": "tv",
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "signal_id": event.get("signal_id"),
        "position_id": event.get("position_id"),
        "symbol": event.get("symbol"),
        "direction": event.get("direction"),
        "position_side": event.get("position_side"),
        "date": event.get("date"),
        "market_date": event.get("date"),
        "bar_time_ms": event.get("bar_time_ms"),
        "script_tag": event.get("script_tag"),
        "strategy_version": event.get("strategy_version"),
        "timeframe_stack": event.get("timeframe_stack"),
        "tv_chart_url": event.get("tv_chart_url"),
    }
    return {key: value for key, value in fallback.items() if value not in (None, "")}


def safe_text(value: Any) -> str:
    return str(value or "").strip()


def safe_upper(value: Any) -> str:
    return safe_text(value).upper()


def safe_lower(value: Any) -> str:
    return safe_text(value).lower()


def sanitize_id_part(value: Any, *, fallback: str = "x", max_len: int = 24) -> str:
    text = safe_text(value)
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def numeric_ms(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        number = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def interval_ms(payload: dict[str, Any]) -> int:
    text = safe_lower(payload.get("interval") or payload.get("chart_tf") or "2")
    if text.endswith("m"):
        text = text[:-1]
    try:
        minutes = float(text or 2)
    except Exception:
        minutes = 2.0
    if minutes <= 0:
        minutes = 2.0
    return int(minutes * 60 * 1000)


def paper_execution_status(signal: dict[str, Any] | None, broker_mode: str = "paper") -> str:
    extra = as_object((signal or {}).get("extra"))
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra.get("execution_by_mode"), dict) else {}
    broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    if isinstance(broker_execution, dict):
        return safe_lower(broker_execution.get("status"))
    return ""


def effective_replay_signal_status(signal: dict[str, Any] | None, *, broker_mode: str, data_environment: str) -> str:
    broker_status = paper_execution_status(signal, broker_mode=broker_mode)
    if broker_status:
        return broker_status
    top_level_status = safe_lower((signal or {}).get("status"))
    if broker_mode == data_environment == "live":
        return top_level_status
    return top_level_status if top_level_status in MUTABLE_SIGNAL_STATUSES else ""


def exclusion_reason(entry: dict[str, Any], signal_row: dict[str, Any] | None, order_rows: list[dict[str, Any]], *, broker_mode: str = "paper") -> str:
    signal_id = safe_text(entry.get("signal_id"))
    if not signal_id:
        return "missing_signal_id"
    if order_rows:
        return "paper_order_exists"
    status = paper_execution_status(signal_row, broker_mode=broker_mode)
    if status in TOUCHED_PAPER_STATUSES:
        return f"paper_status_{status}"
    return ""


def _sort_events(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [dict(row) for row in rows],
        key=lambda row: (
            numeric_ms(row.get("bar_time_ms"), 0),
            safe_text(row.get("created")),
            safe_text(row.get("event_id")),
        ),
    )


def build_chains(
    events: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    *,
    broker_mode: str = "paper",
    pre_alert_lookback_minutes: float = 120.0,
) -> tuple[list[ReplayChain], list[ReplayChain]]:
    events_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        events_by_type.setdefault(safe_lower(row.get("event_type")), []).append(dict(row))

    signal_by_id = {safe_text(row.get("signal_id")): dict(row) for row in signals if safe_text(row.get("signal_id"))}
    orders_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in orders:
        sid = safe_text(row.get("signal_id"))
        if sid:
            orders_by_signal.setdefault(sid, []).append(dict(row))

    pre_by_position: dict[str, list[dict[str, Any]]] = {}
    pre_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in events_by_type.get("pre_alert", []):
        position_id = safe_text(row.get("position_id"))
        if position_id:
            pre_by_position.setdefault(position_id, []).append(dict(row))
        symbol = safe_upper(row.get("symbol"))
        if symbol:
            pre_by_symbol.setdefault(symbol, []).append(dict(row))

    risk_by_signal: dict[str, list[dict[str, Any]]] = {}
    exit_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in events_by_type.get("risk_update", []):
        sid = safe_text(row.get("signal_id"))
        if sid:
            risk_by_signal.setdefault(sid, []).append(dict(row))
    for row in events_by_type.get("exit", []):
        sid = safe_text(row.get("signal_id"))
        if sid:
            exit_by_signal.setdefault(sid, []).append(dict(row))

    candidates: list[ReplayChain] = []
    excluded: list[ReplayChain] = []
    for entry in _sort_events(row for row in events_by_type.get("entry", []) if safe_lower(row.get("status")) == "routed"):
        sid = safe_text(entry.get("signal_id"))
        position_id = safe_text(entry.get("position_id"))
        signal_row = signal_by_id.get(sid, {})
        order_rows = orders_by_signal.get(sid, [])
        symbol = safe_upper(entry.get("symbol") or signal_row.get("symbol"))
        pre_alerts = _sort_events(pre_by_position.get(position_id, []))
        if not pre_alerts and symbol:
            entry_bar_ms = numeric_ms(entry.get("bar_time_ms"), 0)
            lookback_ms = int(max(0.0, float(pre_alert_lookback_minutes or 0.0)) * 60 * 1000)
            direction = safe_lower(entry.get("direction") or signal_row.get("direction"))
            fallback: list[dict[str, Any]] = []
            for pre in pre_by_symbol.get(symbol, []):
                pre_bar_ms = numeric_ms(pre.get("bar_time_ms"), 0)
                if entry_bar_ms and pre_bar_ms and pre_bar_ms > entry_bar_ms:
                    continue
                if entry_bar_ms and pre_bar_ms and lookback_ms and entry_bar_ms - pre_bar_ms > lookback_ms:
                    continue
                pre_payload = decode_payload(pre)
                bias = safe_lower(pre.get("direction") or pre.get("position_side") or pre_payload.get("direction_bias") or pre_payload.get("direction"))
                if bias and direction and bias not in {direction, "neutral"}:
                    continue
                fallback.append(pre)
            pre_alerts = _sort_events(fallback[-1:])
        chain = ReplayChain(
            origin_signal_id=sid,
            origin_position_id=position_id,
            symbol=symbol,
            direction=safe_lower(entry.get("direction") or signal_row.get("direction")),
            entry_event=dict(entry),
            pre_alert_events=pre_alerts,
            risk_events=_sort_events(risk_by_signal.get(sid, [])),
            exit_events=_sort_events(exit_by_signal.get(sid, [])),
            signal_row=dict(signal_row),
            order_rows=[dict(row) for row in order_rows],
        )
        reason = exclusion_reason(entry, signal_row, order_rows, broker_mode=broker_mode)
        chain.exclude_reason = reason
        if reason:
            chain.classification = "excluded"
            excluded.append(chain)
        else:
            candidates.append(chain)
    return candidates, excluded


def _active_signal_sort_key(row: dict[str, Any], *, broker_mode: str, data_environment: str) -> tuple[int, int, str]:
    status = effective_replay_signal_status(row, broker_mode=broker_mode, data_environment=data_environment)
    status_weight = 3 if status in BROKER_CONTROLLED_REPLAY_STATUSES else 2 if status in MUTABLE_SIGNAL_STATUSES else 1
    return (
        status_weight,
        numeric_ms(row.get("bar_time_ms"), 0),
        safe_text(row.get("updated") or row.get("created")),
    )


def split_active_symbol_conflicts(
    chains: list[ReplayChain],
    active_signals: list[dict[str, Any]],
    *,
    broker_mode: str,
    data_environment: str,
) -> tuple[list[ReplayChain], list[ReplayChain]]:
    active_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in active_signals:
        symbol = safe_upper(row.get("symbol"))
        status = effective_replay_signal_status(row, broker_mode=broker_mode, data_environment=data_environment)
        if not symbol or status in INACTIVE_SIGNAL_STATUSES or status not in ACTIVE_REPLAY_SIGNAL_STATUSES:
            continue
        active_by_symbol.setdefault(symbol, []).append(dict(row))
    for rows in active_by_symbol.values():
        rows.sort(key=lambda row: _active_signal_sort_key(row, broker_mode=broker_mode, data_environment=data_environment), reverse=True)

    candidates: list[ReplayChain] = []
    excluded: list[ReplayChain] = []
    for chain in chains:
        conflict: dict[str, Any] = {}
        for row in active_by_symbol.get(chain.symbol, []):
            if safe_text(row.get("signal_id")) == chain.origin_signal_id:
                continue
            conflict = row
            break
        if not conflict:
            candidates.append(chain)
            continue
        status = effective_replay_signal_status(conflict, broker_mode=broker_mode, data_environment=data_environment)
        chain.active_conflict_signal_id = safe_text(conflict.get("signal_id"))
        chain.active_conflict_status = status
        chain.active_conflict_direction = safe_lower(conflict.get("direction"))
        chain.exclude_reason = f"active_symbol_{status or 'unknown'}"
        chain.classification = "excluded"
        excluded.append(chain)
    return candidates, excluded


def choose_entry_base_time(total_chains: int) -> datetime:
    current = datetime.now(ET)
    latest_offset_s = max(0.0, (max(0, total_chains - 1) * 0.5))
    base = current - timedelta(seconds=max(30.0, latest_offset_s + 5.0))
    return base.replace(microsecond=0)


def validate_replay_window(
    *,
    dry_run: bool,
    allow_outside_window: bool = False,
    market_start_et: str = "09:45",
    market_end_et: str = "14:30",
) -> None:
    if dry_run or allow_outside_window:
        return
    current_dt = datetime.now(ET)
    if not is_nyse_trading_day(current_dt.date()):
        raise ValidationError("outside_nyse_trading_day: use --dry-run or --allow-outside-window")
    current = current_dt.time()
    start_time = parse_hhmm(market_start_et, dt_time(9, 45))
    end_time = parse_hhmm(market_end_et, dt_time(14, 30))
    if current < start_time or current > end_time:
        raise ValidationError("outside_tv_entry_freshness_window: use --dry-run or --allow-outside-window")


def parse_hhmm(value: str, default: dt_time) -> dt_time:
    text = safe_text(value)
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return dt_time(hour, minute)
    except Exception:
        return default


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def _easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    holidays: set[date] = set()
    for observed in (_observed_fixed_holiday(year, 1, 1), _observed_fixed_holiday(year + 1, 1, 1)):
        if observed.year == year:
            holidays.add(observed)
    holidays.update(
        {
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            _easter_date(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed_fixed_holiday(year, 7, 4),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed_fixed_holiday(year, 12, 25),
        }
    )
    if year >= 2022:
        observed_juneteenth = _observed_fixed_holiday(year, 6, 19)
        if observed_juneteenth.year == year:
            holidays.add(observed_juneteenth)
    return holidays


def is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def next_nyse_market_window(
    *,
    market_start_et: str,
    market_end_et: str,
    now_et: datetime | None = None,
    max_days: int = 14,
) -> tuple[datetime, datetime]:
    current = now_et.astimezone(ET) if now_et else datetime.now(ET)
    start_time = parse_hhmm(market_start_et, dt_time(9, 45))
    end_time = parse_hhmm(market_end_et, dt_time(14, 30))
    for offset in range(max(1, int(max_days or 1)) + 1):
        day = (current + timedelta(days=offset)).date()
        if not is_nyse_trading_day(day):
            continue
        start_dt = datetime.combine(day, start_time, tzinfo=ET)
        end_dt = datetime.combine(day, end_time, tzinfo=ET)
        if current <= end_dt:
            return start_dt, end_dt
    raise ValidationError("next_nyse_market_window_not_found")


def market_end_deadline(end_hhmm: str) -> datetime:
    current = datetime.now(ET)
    end_time = parse_hhmm(end_hhmm, dt_time(14, 30))
    return current.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)


def market_start_deadline(start_hhmm: str) -> datetime:
    current = datetime.now(ET)
    start_time = parse_hhmm(start_hhmm, dt_time(9, 45))
    return current.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)


def market_time_remaining_s(end_hhmm: str) -> float:
    return (market_end_deadline(end_hhmm) - datetime.now(ET)).total_seconds()


def market_time_until_start_s(start_hhmm: str) -> float:
    return (market_start_deadline(start_hhmm) - datetime.now(ET)).total_seconds()


def stamp_payload_time(payload: dict[str, Any], pine_eval_ms: int, *, data_environment: str) -> None:
    pine_dt = datetime.fromtimestamp(pine_eval_ms / 1000, ET)
    close_ms = pine_eval_ms - 1000
    open_ms = close_ms - interval_ms(payload)
    open_dt = datetime.fromtimestamp(open_ms / 1000, ET)
    payload.update(
        {
            "date": open_dt.strftime("%Y-%m-%d"),
            "market_date": open_dt.strftime("%Y-%m-%d"),
            "us_time": open_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "cn_time": open_dt.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
            "bar_time_ms": open_ms,
            "bar_open_ms": open_ms,
            "bar_close_ms": close_ms,
            "pine_eval_ms": pine_eval_ms,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "environment": data_environment,
        }
    )
    extra = as_object(payload.get("extra"))
    extra.update(
        {
            "bar_time_ms": open_ms,
            "bar_open_ms": open_ms,
            "bar_close_ms": close_ms,
            "pine_eval_ms": pine_eval_ms,
            "pine_eval_time": pine_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "data_environment": data_environment,
            "environment": data_environment,
        }
    )
    payload["extra"] = extra


def _origin_for_event(event: dict[str, Any], origin_signal_id: str, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "event_id": safe_text(event.get("event_id")),
        "event_type": safe_text(event.get("event_type")),
        "signal_id": safe_text(event.get("signal_id")) or origin_signal_id,
        "position_id": safe_text(event.get("position_id")),
        "bar_time_ms": numeric_ms(event.get("bar_time_ms"), 0),
        "market_date": safe_text(event.get("date")),
        "route_status": safe_text(event.get("status")),
        "route_target": safe_text(event.get("route_target")),
        "route_record_id": safe_text(event.get("route_record_id")),
    }


def clone_event_payload(
    chain: ReplayChain,
    event: dict[str, Any],
    *,
    run_id: str,
    event_index: int,
    synthetic_signal_id: str,
    synthetic_position_id: str,
    synthetic_trade_group_id: str,
    broker_mode: str,
    data_environment: str,
    pine_eval_ms: int,
) -> dict[str, Any]:
    payload = decode_payload(event)
    event_type = safe_lower(event.get("event_type") or payload.get("event_type"))
    symbol = chain.symbol or safe_upper(payload.get("symbol"))
    direction = chain.direction or safe_lower(payload.get("direction") or payload.get("position_side"))
    event_id = f"{synthetic_signal_id}_{event_type}_{event_index:02d}"

    extra = as_object(payload.get("extra"))
    replay_origin = as_object(extra.get("replay_origin"))
    replay_origin.update(_origin_for_event(event, chain.origin_signal_id, run_id))

    payload.update(
        {
            "source": "tv",
            "event_type": event_type,
            "event_id": event_id,
            "symbol": symbol,
            "exchange": payload.get("exchange") or "BATS",
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "environment": data_environment,
            "position_id": synthetic_position_id,
            "signal_id": synthetic_signal_id,
            "trade_group_id": synthetic_trade_group_id,
            "bracket_group": synthetic_trade_group_id,
            "entry_order_unique_id": f"entry_{synthetic_trade_group_id}",
            "tp_order_unique_id": f"tp_{synthetic_trade_group_id}",
            "sl_order_unique_id": f"sl_{synthetic_trade_group_id}",
            "entry_coid": f"entry_{synthetic_trade_group_id}",
            "tp_coid": f"tp_{synthetic_trade_group_id}",
            "sl_coid": f"sl_{synthetic_trade_group_id}",
        }
    )
    if event_type == "pre_alert":
        payload["direction_bias"] = safe_lower(payload.get("direction_bias")) or direction
    else:
        payload["direction"] = direction
        payload["position_side"] = direction

    extra.update(
        {
            "replay_origin": replay_origin,
            "replay_run_id": run_id,
            "validation_tag": "today_tv_replay_stress",
            "source": "tradingview",
            "broker_mode": broker_mode,
            "data_environment": data_environment,
            "environment": data_environment,
            "trade_group_id": synthetic_trade_group_id,
            "bracket_group": synthetic_trade_group_id,
            "entry_order_unique_id": f"entry_{synthetic_trade_group_id}",
            "tp_order_unique_id": f"tp_{synthetic_trade_group_id}",
            "sl_order_unique_id": f"sl_{synthetic_trade_group_id}",
            "entry_coid": f"entry_{synthetic_trade_group_id}",
            "tp_coid": f"tp_{synthetic_trade_group_id}",
            "sl_coid": f"sl_{synthetic_trade_group_id}",
        }
    )
    payload["extra"] = extra
    stamp_payload_time(payload, pine_eval_ms, data_environment=data_environment)
    return payload


def rewrite_chain_payloads(
    chains: list[ReplayChain],
    *,
    run_id: str,
    broker_mode: str,
    data_environment: str,
    include_pre_alert: bool,
    base_time: datetime | None = None,
) -> None:
    base = base_time or choose_entry_base_time(len(chains))
    for chain_index, chain in enumerate(chains, start=1):
        symbol_part = sanitize_id_part(chain.symbol, fallback="SYM", max_len=8).upper()
        direction_part = sanitize_id_part(chain.direction, fallback="dir", max_len=5).lower()
        synthetic_signal_id = f"{run_id}_{chain_index:03d}_{symbol_part}_{direction_part}"
        chain.synthetic_signal_id = synthetic_signal_id
        chain.synthetic_position_id = f"{synthetic_signal_id}_pos"
        chain.synthetic_trade_group_id = f"{synthetic_signal_id}_grp"
        entry_eval_ms = int((base + timedelta(milliseconds=(chain_index - 1) * 500)).timestamp() * 1000)

        payloads: dict[str, list[dict[str, Any]]] = {"pre_alert": [], "entry": [], "risk_update": [], "exit": []}
        event_counter = 1
        if include_pre_alert:
            for pre_event in chain.pre_alert_events:
                payloads["pre_alert"].append(
                    clone_event_payload(
                        chain,
                        pre_event,
                        run_id=run_id,
                        event_index=event_counter,
                        synthetic_signal_id=synthetic_signal_id,
                        synthetic_position_id=chain.synthetic_position_id,
                        synthetic_trade_group_id=chain.synthetic_trade_group_id,
                        broker_mode=broker_mode,
                        data_environment=data_environment,
                        pine_eval_ms=entry_eval_ms,
                    )
                )
                event_counter += 1
        payloads["entry"].append(
            clone_event_payload(
                chain,
                chain.entry_event,
                run_id=run_id,
                event_index=event_counter,
                synthetic_signal_id=synthetic_signal_id,
                synthetic_position_id=chain.synthetic_position_id,
                synthetic_trade_group_id=chain.synthetic_trade_group_id,
                broker_mode=broker_mode,
                data_environment=data_environment,
                pine_eval_ms=entry_eval_ms,
            )
        )
        event_counter += 1
        for risk_event in chain.risk_events:
            payloads["risk_update"].append(
                clone_event_payload(
                    chain,
                    risk_event,
                    run_id=run_id,
                    event_index=event_counter,
                    synthetic_signal_id=synthetic_signal_id,
                    synthetic_position_id=chain.synthetic_position_id,
                    synthetic_trade_group_id=chain.synthetic_trade_group_id,
                    broker_mode=broker_mode,
                    data_environment=data_environment,
                    pine_eval_ms=entry_eval_ms,
                )
            )
            event_counter += 1
        for exit_event in chain.exit_events:
            payloads["exit"].append(
                clone_event_payload(
                    chain,
                    exit_event,
                    run_id=run_id,
                    event_index=event_counter,
                    synthetic_signal_id=synthetic_signal_id,
                    synthetic_position_id=chain.synthetic_position_id,
                    synthetic_trade_group_id=chain.synthetic_trade_group_id,
                    broker_mode=broker_mode,
                    data_environment=data_environment,
                    pine_eval_ms=entry_eval_ms,
                )
            )
            event_counter += 1
        chain.payloads = payloads


def fetch_latest_market_date(args: argparse.Namespace) -> str:
    rows = remote_sql(
        args.host,
        args.db_path,
        "select max(date) as market_date from tv_webhook_events where environment = ? and event_type = 'entry' and date != '' and event_id not like 'SIMTV%'",
        [args.data_environment],
    )
    market_date = safe_text(rows[0].get("market_date") if rows else "")
    if not market_date:
        raise ValidationError("latest_market_date_not_found")
    return market_date


def current_et_market_date() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def resolve_plan_market_date(args: argparse.Namespace) -> None:
    value = safe_lower(args.market_date)
    if value in {"today", "today-et", "current-et-date", "current-et-trading-day"}:
        args.market_date = current_et_market_date()
        return
    if value in {"", "latest", "latest-et-trading-day"}:
        args.market_date = fetch_latest_market_date(args)


def fetch_replay_inputs(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    events = remote_sql(
        args.host,
        args.db_path,
        """
        select * from tv_webhook_events
        where date = ? and environment = ? and event_type in ('pre_alert','entry','risk_update','exit')
          and event_id not like 'SIMTV%'
        order by bar_time_ms asc, created asc
        """,
        [args.market_date, args.data_environment],
    )
    signals = remote_sql(
        args.host,
        args.db_path,
        "select * from ibkr_signals where date = ? and environment = ?",
        [args.market_date, args.data_environment],
    )
    orders = remote_sql(
        args.host,
        args.db_path,
        "select * from orders where environment = ? and signal_id != ''",
        [args.broker_mode],
    )
    return events, signals, orders


def fetch_active_signal_inputs(args: argparse.Namespace, symbols: Iterable[str]) -> list[dict[str, Any]]:
    symbol_values = sorted({safe_upper(symbol) for symbol in symbols if safe_upper(symbol)})
    if not symbol_values:
        return []
    placeholders = ",".join("?" for _ in symbol_values)
    return remote_sql(
        args.host,
        args.db_path,
        f"""
        select * from ibkr_signals
        where environment = ? and upper(symbol) in ({placeholders})
        order by updated desc, created desc
        """,
        [args.data_environment, *symbol_values],
    )


def chain_completeness_rank(chain: ReplayChain) -> int:
    if chain.risk_events and chain.exit_events:
        return 0
    if chain.risk_events:
        return 1
    if chain.exit_events:
        return 2
    return 3


def prioritize_replay_chains(chains: list[ReplayChain]) -> list[ReplayChain]:
    return [
        chain
        for _rank, _index, chain in sorted(
            (chain_completeness_rank(chain), index, chain) for index, chain in enumerate(chains)
        )
    ]


def apply_max_chains(chains: list[ReplayChain], max_chains: str, *, prefer_full_chains: bool = False) -> list[ReplayChain]:
    text = safe_lower(max_chains or "all")
    if text in {"all", "0", "none"}:
        return chains
    try:
        limit = int(text)
    except Exception as exc:
        raise ValidationError(f"invalid_max_chains:{max_chains}") from exc
    if limit <= 0:
        return chains
    source = prioritize_replay_chains(chains) if prefer_full_chains else chains
    return source[:limit]


def build_replay_plan(args: argparse.Namespace) -> tuple[list[ReplayChain], list[ReplayChain], dict[str, Any]]:
    resolve_plan_market_date(args)
    events, signals, orders = fetch_replay_inputs(args)
    candidates, excluded = build_chains(
        events,
        signals,
        orders,
        broker_mode=args.broker_mode,
        pre_alert_lookback_minutes=args.pre_alert_lookback_minutes,
    )
    active_excluded: list[ReplayChain] = []
    if not getattr(args, "allow_active_symbols", False) and candidates:
        active_signals = fetch_active_signal_inputs(args, [chain.symbol for chain in candidates])
        candidates, active_excluded = split_active_symbol_conflicts(
            candidates,
            active_signals,
            broker_mode=args.broker_mode,
            data_environment=args.data_environment,
        )
        excluded.extend(active_excluded)
    prefer_full_chains = not getattr(args, "no_prefer_full_chains", False)
    selected = apply_max_chains(candidates, args.max_chains, prefer_full_chains=prefer_full_chains)
    rewrite_chain_payloads(
        selected,
        run_id=args.run_id,
        broker_mode=args.broker_mode,
        data_environment=args.data_environment,
        include_pre_alert=not args.skip_pre_alert,
    )
    event_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for event in events:
        event_counts[safe_lower(event.get("event_type"))] = event_counts.get(safe_lower(event.get("event_type")), 0) + 1
        status_key = f"{safe_lower(event.get('event_type'))}:{safe_lower(event.get('status'))}"
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
    summary = {
        "market_date": args.market_date,
        "broker_mode": args.broker_mode,
        "data_environment": args.data_environment,
        "run_id": args.run_id,
        "source_event_counts": event_counts,
        "source_status_counts": status_counts,
        "entry_chains": len(candidates) + len(excluded),
        "excluded_chains": len(excluded),
        "candidate_chains": len(candidates),
        "selected_chains": len(selected),
        "selected_full_chains": sum(1 for chain in selected if chain.risk_events and chain.exit_events),
        "selected_risk_chains": sum(1 for chain in selected if chain.risk_events),
        "selected_exit_chains": sum(1 for chain in selected if chain.exit_events),
        "selection_policy": "prefer_full_chains" if prefer_full_chains else "chronological",
        "followup_stress_concurrent": bool(getattr(args, "followup_stress_concurrent", False)),
        "risk_burst_workers": int(getattr(args, "risk_burst_workers", 1) or 1),
        "exit_burst_workers": int(getattr(args, "exit_burst_workers", 1) or 1),
        "active_symbol_excluded_chains": len(active_excluded),
        "full_chain_candidates": sum(1 for chain in candidates if chain.risk_events and chain.exit_events),
        "risk_candidate_chains": sum(1 for chain in candidates if chain.risk_events),
        "exit_candidate_chains": sum(1 for chain in candidates if chain.exit_events),
        "exclude_reasons": count_by(excluded, lambda chain: chain.exclude_reason or "unknown"),
        "selected": [chain.public_summary() for chain in selected],
        "excluded": [chain.public_summary() for chain in excluded],
    }
    return selected, excluded, summary


def count_by(items: Iterable[Any], key_fn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = safe_text(key_fn(item)) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def run_json_command(command: list[str], *, timeout: float = 120.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{exc.__class__.__name__}:{exc}",
            "command": command,
        }
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    payload: dict[str, Any]
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "raw_stdout": stdout[:2000]}
    payload["runner_returncode"] = proc.returncode
    if stderr:
        payload["runner_stderr"] = stderr[-2000:]
    if proc.returncode != 0:
        payload.setdefault("ok", False)
    return payload


def remote_prometheus_api(host: str, url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    if not safe_text(host):
        return {"ok": False, "error": "missing_host", "url": url}
    remote_script = f"""
import json
import urllib.request

url = {json.dumps(url)}
timeout = {float(timeout or 10.0)!r}
request = urllib.request.Request(url, headers={{"Accept": "application/json", "User-Agent": {json.dumps(REPLAY_USER_AGENT)}}})
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
except Exception as exc:
    print(json.dumps({{"ok": False, "error": str(exc), "url": url}}, ensure_ascii=False))
    raise SystemExit(0)
try:
    data = json.loads(raw or "{{}}")
except Exception:
    print(json.dumps({{"ok": False, "error": "non_json_prometheus_response", "raw": raw[:1000], "url": url}}, ensure_ascii=False))
    raise SystemExit(0)
print(json.dumps({{"ok": data.get("status") == "success", "url": url, "data": data, "source": "ssh"}}, ensure_ascii=False))
"""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-"]
    proc = subprocess.run(command, input=remote_script, text=True, capture_output=True, timeout=max(15.0, float(timeout or 10.0) + 15.0))
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip() or "remote_prometheus_failed", "url": url}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "remote_prometheus_non_json", "raw": proc.stdout[:1000], "url": url}
    return dict(payload) if isinstance(payload, dict) else {"ok": False, "error": "remote_prometheus_non_object", "url": url}


def prometheus_api(
    prometheus_url: str,
    path: str,
    params: dict[str, str] | None = None,
    *,
    timeout: float = 10.0,
    host: str = "",
) -> dict[str, Any]:
    base = safe_text(prometheus_url).rstrip("/") or DEFAULT_PROMETHEUS_URL
    query = urllib.parse.urlencode(params or {})
    url = f"{base}/{path.lstrip('/')}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": REPLAY_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        if safe_text(host):
            remote_payload = remote_prometheus_api(host, url, timeout=timeout)
            remote_payload.setdefault("local_error", str(exc))
            return remote_payload
        return {"ok": False, "error": str(exc), "url": url}
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "non_json_prometheus_response", "raw": raw[:1000], "url": url}
    return {"ok": data.get("status") == "success", "url": url, "data": data}


def prometheus_query(prometheus_url: str, query: str, *, host: str = "") -> dict[str, Any]:
    return prometheus_api(prometheus_url, "/api/v1/query", {"query": query}, host=host)


def prometheus_alerts(prometheus_url: str, *, host: str = "") -> dict[str, Any]:
    payload = prometheus_api(prometheus_url, "/api/v1/alerts", host=host)
    if not payload.get("ok"):
        return payload
    alerts = (((payload.get("data") or {}).get("data") or {}).get("alerts") or [])
    firing = [alert for alert in alerts if safe_lower(alert.get("state")) == "firing"]
    return {"ok": True, "firing_count": len(firing), "alerts": firing}


def metric_snapshot(prometheus_url: str, environment: str, *, host: str = "", lookback_minutes: float = 5.0) -> dict[str, Any]:
    lookback_m = max(1, int(math.ceil(float(lookback_minutes or 5.0))))
    lookback = f"{lookback_m}m"
    queries = {
        "broker_pending_requests": f'sum(ibkr_broker_pending_requests{{environment="{environment}"}})',
        "order_failures_window": f'sum(increase(ibkr_order_events_total{{environment="{environment}",result!~"ok|synced|seen"}}[{lookback}]))',
        "signal_attention_window": f'sum(increase(ibkr_signal_events_total{{environment="{environment}",result=~"error|rejected|blocked|deferred|unauthenticated"}}[{lookback}]))',
        "order_operation_p95": (
            "histogram_quantile(0.95, sum by (le) "
            f'(rate(ibkr_order_operation_duration_seconds_bucket{{environment="{environment}",operation=~"{ORDER_COMMAND_OPERATION_REGEX}"}}[{lookback}])))'
        ),
        "gateway_serial_wait_p95": f'histogram_quantile(0.95, sum by (le) (rate(ibkr_gateway_order_serial_queue_wait_seconds_bucket{{environment="{environment}"}}[{lookback}])))',
        "gateway_serial_timeouts_window": f'sum(increase(ibkr_gateway_order_serial_timeouts_total{{environment="{environment}"}}[{lookback}]))',
    }
    results: dict[str, Any] = {}
    for name, query in queries.items():
        results[name] = prometheus_query(prometheus_url, query, host=host)
    results["_lookback"] = {"minutes": lookback_m, "range": lookback}
    return results


def prometheus_query_scalar(payload: dict[str, Any]) -> float | None:
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    result = (((payload.get("data") or {}).get("data") or {}).get("result") or [])
    if not result:
        return 0.0
    value = (result[0].get("value") or [None, "0"])[1]
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return 0.0
    return number


def evaluate_stability(args: argparse.Namespace, health: dict[str, Any], phase: str) -> dict[str, Any]:
    if getattr(args, "skip_health", False):
        return {"ok": True, "phase": phase, "skipped": True, "reason": "skip_health"}
    if getattr(args, "skip_stability_gate", False):
        return {"ok": True, "phase": phase, "skipped": True, "reason": "skip_stability_gate"}

    failures: list[dict[str, Any]] = []
    values: dict[str, float] = {}
    thresholds = {
        "max_firing_alerts": float(getattr(args, "max_firing_alerts", 0) or 0),
        "max_broker_pending_requests": float(getattr(args, "max_broker_pending_requests", 0.0) or 0.0),
        "max_order_failures": float(getattr(args, "max_order_failures", 0.0) or 0.0),
        "max_signal_attention": float(getattr(args, "max_signal_attention", 0.0) or 0.0),
        "max_order_operation_p95": float(getattr(args, "max_order_operation_p95", 10.0) or 10.0),
        "max_gateway_serial_wait_p95": float(getattr(args, "max_gateway_serial_wait_p95", 2.0) or 2.0),
        "max_gateway_serial_timeouts": float(getattr(args, "max_gateway_serial_timeouts", 0.0) or 0.0),
    }

    if health.get("stack") and not health.get("stack", {}).get("ok"):
        failures.append({"name": "stack_health", "value": 0, "threshold": 1, "reason": "stack_health_failed"})
    if health.get("monitoring") and not health.get("monitoring", {}).get("ok"):
        failures.append({"name": "monitoring_health", "value": 0, "threshold": 1, "reason": "monitoring_health_failed"})

    alerts = health.get("alerts") if isinstance(health.get("alerts"), dict) else {}
    if not alerts.get("ok"):
        failures.append({"name": "prometheus_alerts", "reason": alerts.get("error") or "alerts_unavailable"})
    else:
        firing_count = float(alerts.get("firing_count") or 0.0)
        values["firing_alerts"] = firing_count
        if firing_count > thresholds["max_firing_alerts"]:
            failures.append({"name": "firing_alerts", "value": firing_count, "threshold": thresholds["max_firing_alerts"]})

    metric_thresholds = {
        "broker_pending_requests": thresholds["max_broker_pending_requests"],
        "order_failures_window": thresholds["max_order_failures"],
        "signal_attention_window": thresholds["max_signal_attention"],
        "order_operation_p95": thresholds["max_order_operation_p95"],
        "gateway_serial_wait_p95": thresholds["max_gateway_serial_wait_p95"],
        "gateway_serial_timeouts_window": thresholds["max_gateway_serial_timeouts"],
    }
    metrics = health.get("metrics") if isinstance(health.get("metrics"), dict) else {}
    for metric_name, threshold in metric_thresholds.items():
        payload = metrics.get(metric_name)
        value = prometheus_query_scalar(payload if isinstance(payload, dict) else {})
        if value is None:
            reason = payload.get("error") if isinstance(payload, dict) else "metric_unavailable"
            failures.append({"name": metric_name, "reason": reason})
            continue
        values[metric_name] = value
        if threshold >= 0 and value > threshold:
            failures.append({"name": metric_name, "value": value, "threshold": threshold})

    return {
        "ok": not failures,
        "phase": phase,
        "failures": failures,
        "values": values,
        "thresholds": thresholds,
        "lookback": metrics.get("_lookback", {}),
    }


def phase0_health(args: argparse.Namespace) -> dict[str, Any]:
    stack = run_json_command([sys.executable, "ops/ibkr_stack/health/check_stack.py", "--json"], timeout=180)
    monitoring = run_json_command([sys.executable, "ops/monitoring/health/check_monitoring_stack.py", "--json"], timeout=180)
    alerts = prometheus_alerts(args.prometheus_url, host=args.host)
    metrics = metric_snapshot(
        args.prometheus_url,
        args.broker_mode,
        host=args.host,
        lookback_minutes=getattr(args, "stability_lookback_minutes", 5.0),
    )
    return {"stack": stack, "monitoring": monitoring, "alerts": alerts, "metrics": metrics}


def query_tv_event(args: argparse.Namespace, event_id: str) -> dict[str, Any]:
    rows = remote_sql(
        args.host,
        args.db_path,
        "select * from tv_webhook_events where event_id = ? and environment = ? order by created desc limit 1",
        [event_id, args.data_environment],
    )
    return dict(rows[0]) if rows else {}


def wait_for_tv_event_terminal(args: argparse.Namespace, event_id: str) -> dict[str, Any]:
    return wait_for(
        f"tv_event:{event_id}",
        lambda: query_tv_event(args, event_id),
        lambda row: safe_lower(row.get("status")) in TV_EVENT_TERMINAL_STATUSES,
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )


def send_payload(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    response = post_json(args.base_url, "/webhook/tv", payload, timeout=args.http_timeout)
    status = int(response.get("_http_status") or 0)
    ok = status < 400 and bool(response.get("ok") or response.get("accepted") or response.get("success"))
    return {
        "ok": ok,
        "event_id": payload.get("event_id"),
        "event_type": payload.get("event_type"),
        "http_status": status,
        "response": response,
    }


def get_json(base_url: str, path: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    base = safe_text(base_url).rstrip("/") or DEFAULT_BASE_URL
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value not in (None, "")})
    url = f"{base}/{safe_text(path).strip('/')}" + (f"?{query}" if query else "")
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": REPLAY_USER_AGENT},
    )
    status_code = 0
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=float(timeout or 20.0)) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code or 0)
        raw = exc.read()
    except urllib.error.URLError as exc:
        raise ValidationError(f"http_get_failed:{url}:{exc}") from exc
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {"ok": False, "error": "non_json_response", "raw": text[:1000]}
    if not isinstance(data, dict):
        data = {"ok": False, "error": "non_object_json_response", "raw": data}
    data["_http_status"] = status_code
    data["_request_url"] = url
    return data


def fetch_account_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return get_json(
        args.base_url,
        args.account_snapshot_path,
        {
            "environment": args.broker_mode,
            "broker_mode": args.broker_mode,
            "cache_bust": now_ms(),
        },
        timeout=args.http_timeout,
    )


def validate_paper_account_snapshot(snapshot: dict[str, Any]) -> None:
    if snapshot.get("ok") is False:
        raise ValidationError(f"account_snapshot_not_ok:{snapshot.get('error') or compact_json(snapshot)[:300]}")
    environment = safe_lower(snapshot.get("environment") or snapshot.get("broker_mode"))
    broker_mode = safe_lower(snapshot.get("broker_mode") or snapshot.get("environment"))
    if environment != "paper" and broker_mode != "paper":
        raise ValidationError(f"refusing_non_paper_account_snapshot:environment={environment or '-'} broker_mode={broker_mode or '-'}")
    if snapshot.get("service_running") is False:
        raise ValidationError("account_snapshot_service_not_running")
    if snapshot.get("session_authenticated") is False:
        raise ValidationError("account_snapshot_session_not_authenticated")


def snapshot_positions_for_symbol(snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    wanted = safe_upper(symbol)
    return [
        dict(row)
        for row in snapshot.get("positions") or []
        if isinstance(row, dict) and safe_upper(row.get("symbol") or row.get("ticker")) == wanted
    ]


def snapshot_position_quantity(row: dict[str, Any]) -> float:
    return safe_float(row.get("quantity", row.get("position", 0.0)), 0.0)


def snapshot_order_symbol(row: dict[str, Any]) -> str:
    return safe_upper(row.get("symbol") or row.get("ticker") or row.get("contract_symbol"))


def snapshot_order_is_open(row: dict[str, Any]) -> bool:
    if row.get("is_open") is False:
        return False
    status = safe_lower(row.get("status") or row.get("order_status"))
    if status and status in ORDER_TERMINAL_STATUSES:
        return False
    return True


def snapshot_open_orders_for_symbol(snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    wanted = safe_upper(symbol)
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("live_open_orders", "orders"):
        for row in snapshot.get(key) or []:
            if not isinstance(row, dict) or snapshot_order_symbol(row) != wanted or not snapshot_order_is_open(row):
                continue
            identity = safe_text(row.get("order_id") or row.get("broker_order_id") or row.get("perm_id") or compact_json(row)[:200])
            if identity in seen:
                continue
            seen.add(identity)
            matches.append(dict(row))
    return matches


def account_residuals_for_symbol(snapshot: dict[str, Any], symbol: str) -> dict[str, Any]:
    positions = [
        row
        for row in snapshot_positions_for_symbol(snapshot, symbol)
        if abs(snapshot_position_quantity(row)) > 1e-9
    ]
    open_orders = snapshot_open_orders_for_symbol(snapshot, symbol)
    return {
        "symbol": safe_upper(symbol),
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "positions": positions,
        "open_orders": open_orders,
    }


def verify_account_flat_for_chains(
    args: argparse.Namespace,
    chains: list[ReplayChain],
    *,
    phase: str,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if getattr(args, "skip_account_flat_check", False):
        return {"ok": True, "phase": phase, "skipped": True, "reason": "skip_account_flat_check"}
    current_snapshot = dict(snapshot or fetch_account_snapshot(args))
    validate_paper_account_snapshot(current_snapshot)
    symbols = sorted({chain.symbol for chain in chains if safe_upper(chain.symbol)})
    residuals = [account_residuals_for_symbol(current_snapshot, symbol) for symbol in symbols]
    failures = [item for item in residuals if item.get("position_count") or item.get("open_order_count")]
    result = {
        "ok": not failures,
        "phase": phase,
        "symbols": symbols,
        "failures": failures,
        "residuals": residuals,
    }
    for chain in chains:
        symbol_failure = next((item for item in failures if item.get("symbol") == chain.symbol), None)
        chain.checks.append(
            {
                "name": f"account_flat_{phase}",
                "ok": symbol_failure is None,
                "symbol": chain.symbol,
                "residual": symbol_failure or {},
            }
        )
    return result


def adopt_routed_signal_id(chain: ReplayChain, response: dict[str, Any]) -> None:
    routed_signal_id = safe_text(response.get("signal_id"))
    if not routed_signal_id or routed_signal_id == chain.synthetic_signal_id:
        return
    action = safe_text(response.get("action"))
    if action and action not in ACTIVE_POLICY_MERGE_ACTIONS:
        chain.checks.append(
            {
                "name": "entry_routed_to_existing_signal_not_adopted",
                "ok": False,
                "requested_signal_id": chain.synthetic_signal_id,
                "routed_signal_id": routed_signal_id,
                "action": action,
                "reason": response.get("reason") or "active_symbol_policy_terminal",
            }
        )
        return
    requested_signal_id = chain.synthetic_signal_id
    chain.synthetic_signal_id = routed_signal_id
    chain.checks.append(
        {
            "name": "entry_routed_to_active_signal",
            "ok": True,
            "requested_signal_id": requested_signal_id,
            "routed_signal_id": routed_signal_id,
            "action": action,
            "merged_signal_id": response.get("merged_signal_id"),
            "followup_signal_id": response.get("followup_signal_id"),
        }
    )
    for event_type in ("risk_update", "exit"):
        for payload in chain.payloads.get(event_type, []):
            payload["signal_id"] = routed_signal_id
            extra = as_object(payload.get("extra"))
            extra["replay_requested_signal_id"] = requested_signal_id
            extra["replay_adopted_signal_id"] = routed_signal_id
            payload["extra"] = extra


def sync_followup_payload_linkage(chain: ReplayChain, signal: dict[str, Any]) -> None:
    extra = as_object(signal.get("extra"))
    trade_group_id = safe_text(extra.get("trade_group_id") or extra.get("bracket_group"))
    if not trade_group_id:
        return
    chain.synthetic_trade_group_id = trade_group_id
    updates = {
        "trade_group_id": trade_group_id,
        "bracket_group": trade_group_id,
        "entry_order_unique_id": safe_text(extra.get("entry_order_unique_id") or extra.get("entry_coid")),
        "tp_order_unique_id": safe_text(extra.get("tp_order_unique_id") or extra.get("tp_coid")),
        "sl_order_unique_id": safe_text(extra.get("sl_order_unique_id") or extra.get("sl_coid")),
        "entry_coid": safe_text(extra.get("entry_coid") or extra.get("entry_order_unique_id")),
        "tp_coid": safe_text(extra.get("tp_coid") or extra.get("tp_order_unique_id")),
        "sl_coid": safe_text(extra.get("sl_coid") or extra.get("sl_order_unique_id")),
    }
    for event_type in ("risk_update", "exit"):
        for payload in chain.payloads.get(event_type, []):
            for key, value in updates.items():
                if value:
                    payload[key] = value
            payload_extra = as_object(payload.get("extra"))
            for key, value in updates.items():
                if value:
                    payload_extra[key] = value
            payload["extra"] = payload_extra


def send_burst(args: argparse.Namespace, chains: list[ReplayChain]) -> list[dict[str, Any]]:
    items: list[tuple[ReplayChain, dict[str, Any]]] = []
    for chain in chains:
        for payload in chain.payloads.get("pre_alert", []):
            items.append((chain, payload))
        for payload in chain.payloads.get("entry", []):
            items.append((chain, payload))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.burst_workers or 1))) as executor:
        future_map = {executor.submit(send_payload, args, payload): (chain, payload) for chain, payload in items}
        for future in as_completed(future_map):
            chain, payload = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "ok": False,
                    "event_id": payload.get("event_id"),
                    "event_type": payload.get("event_type"),
                    "error": str(exc),
                }
            chain.sent_events.append(result)
            chain.checks.append({"name": f"send_{payload.get('event_type')}", **result})
            if safe_lower(payload.get("event_type")) == "entry" and isinstance(result.get("response"), dict):
                adopt_routed_signal_id(chain, result["response"])
            results.append(result)
    return results


def send_payload_burst(
    args: argparse.Namespace,
    items: list[tuple[ReplayChain, dict[str, Any], str]],
    *,
    workers: int,
    spacing_seconds: float = 0.0,
) -> list[tuple[ReplayChain, dict[str, Any], dict[str, Any], str]]:
    if not items:
        return []

    def send_indexed(index: int, chain: ReplayChain, payload: dict[str, Any], check_name: str) -> tuple[ReplayChain, dict[str, Any], dict[str, Any], str]:
        delay = max(0.0, float(spacing_seconds or 0.0)) * index
        if delay > 0:
            time.sleep(delay)
        try:
            result = send_payload(args, payload)
        except Exception as exc:
            result = {
                "ok": False,
                "event_id": payload.get("event_id"),
                "event_type": payload.get("event_type"),
                "error": str(exc),
            }
        return chain, payload, result, check_name

    results: list[tuple[ReplayChain, dict[str, Any], dict[str, Any], str]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers or 1))) as executor:
        future_map = {
            executor.submit(send_indexed, index, chain, payload, check_name): index
            for index, (chain, payload, check_name) in enumerate(items)
        }
        completed: list[tuple[int, ReplayChain, dict[str, Any], dict[str, Any], str]] = []
        for future in as_completed(future_map):
            index = future_map[future]
            chain, payload, result, check_name = future.result()
            completed.append((index, chain, payload, result, check_name))
        for _index, chain, payload, result, check_name in sorted(completed, key=lambda item: item[0]):
            chain.sent_events.append(result)
            chain.checks.append({"name": check_name, **result})
            results.append((chain, payload, result, check_name))
    return results


def wait_for_routing(args: argparse.Namespace, chains: list[ReplayChain], event_types: set[str] | None = None) -> None:
    wanted = event_types or {"pre_alert", "entry", "risk_update", "exit"}
    for chain in chains:
        for payload_list in chain.payloads.values():
            for payload in payload_list:
                if safe_lower(payload.get("event_type")) not in wanted:
                    continue
                event_id = safe_text(payload.get("event_id"))
                if not event_id:
                    continue
                row = wait_for_tv_event_terminal(args, event_id)
                status = safe_lower(row.get("status"))
                chain.checks.append(
                    {
                        "name": f"route_{payload.get('event_type')}",
                        "ok": status == "routed",
                        "event_id": event_id,
                        "status": status,
                        "route_target": row.get("route_target"),
                        "route_record_id": row.get("route_record_id"),
                        "error_msg": row.get("error_msg"),
                    }
                )


def orders_have_bracket(orders: list[dict[str, Any]]) -> bool:
    roles = role_map(orders)
    return {"entry", "take_profit", "stop_loss"}.issubset(set(roles))


def has_filled_entry_or_position(orders: list[dict[str, Any]]) -> bool:
    for row in orders:
        role = safe_lower(row.get("role"))
        status = safe_lower(row.get("status"))
        filled_qty = numeric_ms(row.get("filled_qty"), 0)
        if role == "entry" and (status == "filled" or filled_qty > 0):
            return True
    return False


def wait_for_entry_processing(args: argparse.Namespace, chain: ReplayChain) -> None:
    signal_id = chain.synthetic_signal_id
    terminal_policy = next((check for check in chain.checks if check.get("name") == "entry_routed_to_existing_signal_not_adopted"), None)
    if terminal_policy:
        chain.checks.append(
            {
                "name": "entry_processing_skipped_policy_terminal",
                "ok": False,
                "signal_id": signal_id,
                "action": terminal_policy.get("action"),
                "reason": terminal_policy.get("reason"),
            }
        )
        return

    def state() -> dict[str, Any]:
        signal = query_signal(args, signal_id)
        orders = query_orders(args, signal_id)
        paper_status = paper_execution_status(signal, broker_mode=args.broker_mode)
        rejection = signal_rejection_reason(signal, args.broker_mode) if signal else ""
        return {"signal": signal, "orders": orders, "paper_status": paper_status, "rejection": rejection}

    result = wait_for(
        f"entry_processing:{signal_id}",
        state,
        lambda value: bool(value.get("signal", {}).get("id"))
        and (
            orders_have_bracket(value.get("orders") or [])
            or safe_lower(value.get("paper_status")) in SIGNAL_ATTENTION_STATUSES
            or bool(value.get("rejection"))
        ),
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    chain.final_signal = dict(result.get("signal") or {})
    chain.final_orders = [dict(row) for row in result.get("orders") or []]
    sync_followup_payload_linkage(chain, chain.final_signal)
    chain.checks.append(
        {
            "name": "entry_processed",
            "ok": orders_have_bracket(chain.final_orders) or not result.get("rejection"),
            "signal_id": signal_id,
            "signal_record_id": chain.final_signal.get("id"),
            "signal_status": chain.final_signal.get("status"),
            "paper_status": result.get("paper_status"),
            "order_count": len(chain.final_orders),
            "rejection": result.get("rejection"),
        }
    )


def rebase_risk_payload(payload: dict[str, Any], chain: ReplayChain, orders: list[dict[str, Any]], previous: dict[str, float]) -> None:
    roles = role_map(orders)
    entry_order = roles.get("entry", {})
    sl_order = roles.get("stop_loss", {})
    tp_order = roles.get("take_profit", {})
    persisted_entry = order_price(entry_order, "limit_price", "entry", "fill_price") or float(payload.get("entry_price") or payload.get("entry") or 0.0)
    persisted_sl = order_price(sl_order, "limit_price", "sl_price", "stop_loss") or float(payload.get("new_stop_loss") or payload.get("stop_loss") or 0.0)
    persisted_tp = order_price(tp_order, "limit_price", "tp_price", "take_profit") or float(payload.get("new_take_profit") or payload.get("take_profit") or 0.0)
    origin_entry_payload = decode_payload(chain.entry_event)
    origin_entry = float(payload.get("entry_price") or origin_entry_payload.get("entry") or origin_entry_payload.get("limit_price") or persisted_entry or 0.0)

    def rebase_price(value: Any, fallback: float) -> float:
        try:
            raw = float(value or 0.0)
        except Exception:
            raw = 0.0
        if raw <= 0 or origin_entry <= 0 or persisted_entry <= 0:
            return round(float(fallback or 0.0), 4)
        return round(float(persisted_entry + (raw - origin_entry)), 4)

    previous_sl = previous.get("stop_loss") or persisted_sl
    previous_tp = previous.get("take_profit") or persisted_tp
    new_sl = rebase_price(payload.get("new_stop_loss") or payload.get("stop_loss"), persisted_sl)
    new_tp = rebase_price(payload.get("new_take_profit") or payload.get("take_profit"), persisted_tp)
    if new_sl <= 0:
        new_sl = float(persisted_sl or previous_sl or 0.0)
    if new_tp <= 0:
        new_tp = float(persisted_tp or previous_tp or 0.0)
    payload.update(
        {
            "entry_price": round(float(persisted_entry or 0.0), 4),
            "previous_stop_loss": round(float(previous_sl or 0.0), 4),
            "previous_take_profit": round(float(previous_tp or 0.0), 4),
            "new_stop_loss": round(float(new_sl or 0.0), 4),
            "new_take_profit": round(float(new_tp or 0.0), 4),
            "quantity": int(float(entry_order.get("quantity") or payload.get("quantity") or payload.get("shares") or 0)),
        }
    )
    previous["stop_loss"] = float(payload["new_stop_loss"] or 0.0)
    previous["take_profit"] = float(payload["new_take_profit"] or 0.0)


def rebase_exit_payload(payload: dict[str, Any], orders: list[dict[str, Any]], previous: dict[str, float]) -> None:
    roles = role_map(orders)
    entry_order = roles.get("entry", {})
    sl_order = roles.get("stop_loss", {})
    tp_order = roles.get("take_profit", {})
    persisted_entry = order_price(entry_order, "limit_price", "entry", "fill_price") or float(payload.get("entry_price") or payload.get("entry") or 0.0)
    persisted_sl = previous.get("stop_loss") or order_price(sl_order, "limit_price", "sl_price", "stop_loss") or float(payload.get("stop_loss") or 0.0)
    persisted_tp = previous.get("take_profit") or order_price(tp_order, "limit_price", "tp_price", "take_profit") or float(payload.get("take_profit") or 0.0)
    quantity = int(float(entry_order.get("quantity") or payload.get("quantity") or payload.get("shares") or 0))
    payload.update(
        {
            "entry_price": round(float(persisted_entry or 0.0), 4),
            "stop_loss": round(float(persisted_sl or 0.0), 4),
            "take_profit": round(float(persisted_tp or 0.0), 4),
            "quantity": quantity,
        }
    )


def stamp_runtime_payload(payload: dict[str, Any], args: argparse.Namespace) -> None:
    pine_eval_ms = now_ms() - 5000
    stamp_payload_time(payload, pine_eval_ms, data_environment=args.data_environment)


def send_risk_and_exit(args: argparse.Namespace, chain: ReplayChain) -> None:
    signal_id = chain.synthetic_signal_id
    previous_prices: dict[str, float] = {}
    orders = query_orders(args, signal_id)
    chain.final_orders = orders
    if not orders_have_bracket(orders):
        for payload in chain.payloads.get("risk_update", []):
            chain.checks.append(
                {
                    "name": "risk_skipped_no_child_orders",
                    "ok": True,
                    "event_id": payload.get("event_id"),
                    "reason": "risk_update_requires_tp_sl_orders",
                }
            )
        for payload in chain.payloads.get("exit", []):
            chain.checks.append(
                {
                    "name": "exit_skipped_no_bracket",
                    "ok": True,
                    "event_id": payload.get("event_id"),
                    "reason": "exit_requires_entry_bracket_first",
                }
            )
        return

    for payload in chain.payloads.get("risk_update", []):
        stamp_runtime_payload(payload, args)
        rebase_risk_payload(payload, chain, orders, previous_prices)
        result = send_payload(args, payload)
        chain.sent_events.append(result)
        chain.checks.append({"name": "send_risk_update", **result})
        if not result.get("ok"):
            continue
        event_row = wait_for_tv_event_terminal(args, safe_text(payload.get("event_id")))
        chain.checks.append(
            {
                "name": "route_risk_update",
                "ok": safe_lower(event_row.get("status")) == "routed",
                "event_id": payload.get("event_id"),
                "status": event_row.get("status"),
                "route_record_id": event_row.get("route_record_id"),
                "error_msg": event_row.get("error_msg"),
            }
        )
        time.sleep(max(0.1, float(args.risk_spacing_seconds or 0.1)))

    orders = query_orders(args, signal_id)
    chain.final_orders = orders
    if not chain.payloads.get("exit"):
        chain.checks.append({"name": "exit_skipped_no_source_event", "ok": True, "reason": "no_exit_event"})
        return
    if not has_filled_entry_or_position(orders):
        for payload in chain.payloads.get("exit", []):
            chain.checks.append(
                {
                    "name": "exit_skipped_no_fill",
                    "ok": True,
                    "event_id": payload.get("event_id"),
                    "reason": "real_filled_order_required_for_close",
                }
            )
        return

    for payload in chain.payloads.get("exit", []):
        stamp_runtime_payload(payload, args)
        rebase_exit_payload(payload, orders, previous_prices)
        result = send_payload(args, payload)
        chain.sent_events.append(result)
        chain.checks.append({"name": "send_exit", **result})
        if not result.get("ok"):
            continue
        event_row = wait_for_tv_event_terminal(args, safe_text(payload.get("event_id")))
        chain.checks.append(
            {
                "name": "route_exit",
                "ok": safe_lower(event_row.get("status")) == "routed",
                "event_id": payload.get("event_id"),
                "status": event_row.get("status"),
                "route_record_id": event_row.get("route_record_id"),
                "error_msg": event_row.get("error_msg"),
            }
        )
        time.sleep(max(0.1, float(args.exit_spacing_seconds or 0.1)))


def route_followup_results(
    args: argparse.Namespace,
    sent: list[tuple[ReplayChain, dict[str, Any], dict[str, Any], str]],
    *,
    route_name: str,
) -> None:
    for chain, payload, result, _check_name in sent:
        if not result.get("ok"):
            continue
        event_row = wait_for_tv_event_terminal(args, safe_text(payload.get("event_id")))
        chain.checks.append(
            {
                "name": route_name,
                "ok": safe_lower(event_row.get("status")) == "routed",
                "event_id": payload.get("event_id"),
                "status": event_row.get("status"),
                "route_record_id": event_row.get("route_record_id"),
                "error_msg": event_row.get("error_msg"),
            }
        )


def send_risk_updates_burst(args: argparse.Namespace, chains: list[ReplayChain]) -> None:
    items: list[tuple[ReplayChain, dict[str, Any], str]] = []
    for chain in chains:
        previous_prices: dict[str, float] = {}
        orders = query_orders(args, chain.synthetic_signal_id)
        chain.final_orders = orders
        if not orders_have_bracket(orders):
            for payload in chain.payloads.get("risk_update", []):
                chain.checks.append(
                    {
                        "name": "risk_skipped_no_child_orders",
                        "ok": True,
                        "event_id": payload.get("event_id"),
                        "reason": "risk_update_requires_tp_sl_orders",
                    }
                )
            continue
        for payload in chain.payloads.get("risk_update", []):
            stamp_runtime_payload(payload, args)
            rebase_risk_payload(payload, chain, orders, previous_prices)
            items.append((chain, payload, "send_risk_update"))
    sent = send_payload_burst(
        args,
        items,
        workers=max(1, int(getattr(args, "risk_burst_workers", 1) or 1)),
        spacing_seconds=max(0.0, float(getattr(args, "followup_burst_spacing_seconds", 0.0) or 0.0)),
    )
    route_followup_results(args, sent, route_name="route_risk_update")


def send_exits_burst(args: argparse.Namespace, chains: list[ReplayChain]) -> None:
    items: list[tuple[ReplayChain, dict[str, Any], str]] = []
    for chain in chains:
        orders = query_orders(args, chain.synthetic_signal_id)
        chain.final_orders = orders
        if not chain.payloads.get("exit"):
            chain.checks.append({"name": "exit_skipped_no_source_event", "ok": True, "reason": "no_exit_event"})
            continue
        if not has_filled_entry_or_position(orders):
            for payload in chain.payloads.get("exit", []):
                chain.checks.append(
                    {
                        "name": "exit_skipped_no_fill",
                        "ok": True,
                        "event_id": payload.get("event_id"),
                        "reason": "real_filled_order_required_for_close",
                    }
                )
            continue
        previous_prices = {
            "stop_loss": order_price(role_map(orders).get("stop_loss", {}), "limit_price", "sl_price", "stop_loss"),
            "take_profit": order_price(role_map(orders).get("take_profit", {}), "limit_price", "tp_price", "take_profit"),
        }
        for payload in chain.payloads.get("exit", []):
            stamp_runtime_payload(payload, args)
            rebase_exit_payload(payload, orders, previous_prices)
            items.append((chain, payload, "send_exit"))
    sent = send_payload_burst(
        args,
        items,
        workers=max(1, int(getattr(args, "exit_burst_workers", 1) or 1)),
        spacing_seconds=max(0.0, float(getattr(args, "followup_burst_spacing_seconds", 0.0) or 0.0)),
    )
    route_followup_results(args, sent, route_name="route_exit")


def send_followups(args: argparse.Namespace, chains: list[ReplayChain]) -> None:
    if not getattr(args, "followup_stress_concurrent", False):
        for chain in chains:
            send_risk_and_exit(args, chain)
        return
    send_risk_updates_burst(args, chains)
    send_exits_burst(args, chains)


def finalize_chain(args: argparse.Namespace, chain: ReplayChain) -> None:
    chain.final_signal = query_signal(args, chain.synthetic_signal_id)
    chain.final_orders = query_orders(args, chain.synthetic_signal_id)
    reverses: list[dict[str, Any]] = []
    for action_type in ("adjust_bracket", "close"):
        reverses.extend(query_reverses(args, chain.synthetic_signal_id, action_type))
    chain.final_reverses = reverses
    failures = [check for check in chain.checks if check.get("ok") is False]
    if failures:
        chain.classification = "attention"
    elif any(check.get("name") == "exit_skipped_no_fill" for check in chain.checks):
        chain.classification = "exit_skipped_no_fill"
    elif any(check.get("name") == "exit_skipped_no_source_event" for check in chain.checks):
        chain.classification = "no_exit_event"
    elif orders_have_bracket(chain.final_orders):
        chain.classification = "bracket_submitted"
    else:
        paper_status = paper_execution_status(chain.final_signal, broker_mode=args.broker_mode)
        chain.classification = paper_status or safe_lower(chain.final_signal.get("status")) or "unknown"


def order_is_open(row: dict[str, Any]) -> bool:
    return safe_lower(row.get("status")) not in ORDER_TERMINAL_STATUSES


def open_order_summary(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for row in orders:
        if not order_is_open(row):
            continue
        summary.append(
            {
                "id": row.get("id"),
                "role": row.get("role"),
                "status": row.get("status"),
                "order_id": row.get("order_id") or row.get("broker_order_id"),
                "unique_id": row.get("unique_id"),
                "trade_group_id": row.get("trade_group_id"),
            }
        )
    return summary


def resolve_order_trade_group_id(orders: list[dict[str, Any]]) -> str:
    roles = role_map(orders)
    preferred_rows = [roles.get("entry"), roles.get("take_profit"), roles.get("stop_loss"), *orders]
    for row in preferred_rows:
        if not isinstance(row, dict) or not row:
            continue
        for key in ("trade_group_id", "bracket_group", "group_id", "parent_group_id"):
            value = safe_text(row.get(key))
            if value:
                return value
        extra = as_object(row.get("extra"))
        for key in ("trade_group_id", "bracket_group", "group_id", "parent_group_id"):
            value = safe_text(extra.get(key))
            if value:
                return value
    return ""


def build_cleanup_exit_payload(args: argparse.Namespace, chain: ReplayChain, orders: list[dict[str, Any]], trade_group_id: str) -> dict[str, Any]:
    roles = role_map(orders)
    entry_order = roles.get("entry") or orders[0]
    sl_order = roles.get("stop_loss", {})
    tp_order = roles.get("take_profit", {})
    event_id = f"{chain.synthetic_signal_id}_cleanup_exit_{now_ms()}"
    direction = safe_lower(entry_order.get("direction") or chain.direction)
    payload = {
        "source": "tv",
        "event_type": "exit",
        "event_id": event_id,
        "symbol": safe_upper(entry_order.get("symbol") or chain.symbol),
        "exchange": "BATS",
        "direction": direction,
        "position_side": direction,
        "position_id": chain.synthetic_position_id or f"{chain.synthetic_signal_id}_pos",
        "signal_id": chain.synthetic_signal_id,
        "trade_group_id": trade_group_id,
        "bracket_group": trade_group_id,
        "broker_mode": args.broker_mode,
        "market_data_mode": args.data_environment,
        "data_environment": args.data_environment,
        "environment": args.data_environment,
        "interval": "2",
        "chart_tf": "2",
        "script_tag": "Signal_Strategy_Core[Glory]",
        "strategy_version": "today_tv_replay_cleanup",
        "entry_price": order_price(entry_order, "fill_price", "limit_price", "entry") or 0.0,
        "stop_loss": order_price(sl_order, "limit_price", "sl_price", "stop_loss") or 0.0,
        "take_profit": order_price(tp_order, "limit_price", "tp_price", "take_profit") or 0.0,
        "quantity": int(float(entry_order.get("quantity") or entry_order.get("filled_qty") or 0)),
        "exit_reason": "today_tv_replay_synthetic_cleanup",
        "extra": {
            "source": "tradingview",
            "validation_tag": "today_tv_replay_cleanup",
            "replay_run_id": args.run_id,
            "trade_group_id": trade_group_id,
            "bracket_group": trade_group_id,
        },
    }
    stamp_runtime_payload(payload, args)
    return payload


def cleanup_synthetic_orders(args: argparse.Namespace, chains: list[ReplayChain]) -> list[dict[str, Any]]:
    if args.no_cleanup:
        return []
    results: list[dict[str, Any]] = []
    for chain in chains:
        orders = query_orders(args, chain.synthetic_signal_id)
        if not any(order_is_open(row) for row in orders):
            continue
        trade_group_id = resolve_order_trade_group_id(orders) or chain.synthetic_trade_group_id
        if not trade_group_id:
            result = {
                "ok": False,
                "action": "missing_trade_group_id",
                "signal_id": chain.synthetic_signal_id,
                "error": "missing_trade_group_id",
                "open_orders": open_order_summary(orders),
            }
            chain.checks.append({"name": "cleanup_synthetic_orders", **result})
            results.append(result)
            continue
        chain.synthetic_trade_group_id = trade_group_id
        roles = role_map(orders)
        entry_order = roles.get("entry") or orders[0]
        entry_status = safe_lower(entry_order.get("status"))
        entry_filled = entry_status == "filled" or numeric_ms(entry_order.get("filled_qty"), 0) > 0
        target_id = (
            safe_text(entry_order.get("unique_id"))
            or safe_text(entry_order.get("entry_order_unique_id"))
            or safe_text(entry_order.get("order_id"))
            or trade_group_id
        )
        action = "exit_cleanup" if entry_filled else "cancel_group"
        if entry_filled:
            cleanup_payload = build_cleanup_exit_payload(args, chain, orders, trade_group_id)
            response = post_json(args.base_url, "/webhook/tv", cleanup_payload, timeout=args.http_timeout)
        else:
            response = post_json(
                args.base_url,
                "/api/custom/ibkr/orders/cancel_group",
                {
                    "id": target_id,
                    "trade_group_id": trade_group_id,
                    "signal_id": chain.synthetic_signal_id,
                    "broker_mode": args.broker_mode,
                    "environment": args.broker_mode,
                    "market_data_mode": args.data_environment,
                    "data_environment": args.data_environment,
                    "source": "today_tv_replay_cleanup",
                    "reason": "today TV replay synthetic order cleanup",
                },
                timeout=args.http_timeout,
            )
        status = int(response.get("_http_status") or 0)
        ok = status < 400 and bool(response.get("ok") or response.get("accepted"))
        if ok and entry_filled:
            try:
                wait_for_tv_event_terminal(args, safe_text(cleanup_payload.get("event_id")))
                wait_for(
                    f"cleanup_exit:{chain.synthetic_signal_id}",
                    lambda: query_reverses(args, chain.synthetic_signal_id, "close"),
                    lambda rows: any(safe_lower(row.get("status")) in {"confirmed", "closed"} for row in rows or []),
                    timeout_s=min(float(args.poll_seconds or 60.0), 120.0),
                    interval_s=args.poll_interval,
                )
            except Exception as exc:
                ok = False
                response = {**response, "cleanup_wait_error": str(exc)}
        result = {
            "ok": ok,
            "action": action,
            "signal_id": chain.synthetic_signal_id,
            "trade_group_id": trade_group_id,
            "target_id": target_id,
            "http_status": status,
            "response": response,
        }
        chain.checks.append({"name": "cleanup_synthetic_orders", **result})
        results.append(result)
        time.sleep(max(0.1, float(args.cleanup_spacing_seconds or 0.1)))
    return results


def verify_no_open_synthetic_orders(args: argparse.Namespace, chains: list[ReplayChain]) -> list[dict[str, Any]]:
    if args.no_cleanup:
        return []
    results: list[dict[str, Any]] = []
    for chain in chains:
        orders = query_orders(args, chain.synthetic_signal_id)
        chain.final_orders = [dict(row) for row in orders]
        open_orders = open_order_summary(orders)
        result = {
            "ok": not open_orders,
            "signal_id": chain.synthetic_signal_id,
            "open_order_count": len(open_orders),
            "open_orders": open_orders,
        }
        chain.checks.append({"name": "post_cleanup_no_open_synthetic_orders", **result})
        results.append(result)
    return results


def count_routed_exit_chains(chains: list[ReplayChain]) -> int:
    return sum(
        1
        for chain in chains
        if any(check.get("name") == "route_exit" and check.get("ok") is True for check in chain.checks)
    )


def count_confirmed_close_reverses(chains: list[ReplayChain]) -> int:
    return sum(
        1
        for chain in chains
        if any(
            safe_lower(row.get("action_type")) == "close" and safe_lower(row.get("status")) in {"confirmed", "closed"}
            for row in chain.final_reverses
        )
    )


def evaluate_flow_requirements(
    args: argparse.Namespace,
    chains: list[ReplayChain],
    cleanup_results: list[dict[str, Any]],
) -> dict[str, Any]:
    thresholds = {
        "min_full_chains": max(0, int(getattr(args, "min_full_chains", 0) or 0)),
        "min_bracket_chains": max(0, int(getattr(args, "min_bracket_chains", 0) or 0)),
        "min_filled_entry_chains": max(0, int(getattr(args, "min_filled_entry_chains", 0) or 0)),
        "min_routed_exit_chains": max(0, int(getattr(args, "min_routed_exit_chains", 0) or 0)),
        "min_cleanup_exit_chains": max(0, int(getattr(args, "min_cleanup_exit_chains", 0) or 0)),
        "min_closed_reverse_chains": max(0, int(getattr(args, "min_closed_reverse_chains", 0) or 0)),
    }
    counts = {
        "selected_chains": len(chains),
        "full_chains": sum(1 for chain in chains if chain.risk_events and chain.exit_events),
        "bracket_chains": sum(1 for chain in chains if orders_have_bracket(chain.final_orders)),
        "filled_entry_chains": sum(1 for chain in chains if has_filled_entry_or_position(chain.final_orders)),
        "routed_exit_chains": count_routed_exit_chains(chains),
        "cleanup_exit_chains": sum(1 for result in cleanup_results if result.get("ok") and result.get("action") == "exit_cleanup"),
        "closed_reverse_chains": count_confirmed_close_reverses(chains),
    }
    checks = [
        ("full_chains", "min_full_chains"),
        ("bracket_chains", "min_bracket_chains"),
        ("filled_entry_chains", "min_filled_entry_chains"),
        ("routed_exit_chains", "min_routed_exit_chains"),
        ("cleanup_exit_chains", "min_cleanup_exit_chains"),
        ("closed_reverse_chains", "min_closed_reverse_chains"),
    ]
    failures = [
        {"name": count_name, "value": counts[count_name], "threshold": thresholds[threshold_name]}
        for count_name, threshold_name in checks
        if counts[count_name] < thresholds[threshold_name]
    ]
    return {"ok": not failures, "counts": counts, "thresholds": thresholds, "failures": failures}


def chain_report(chain: ReplayChain) -> dict[str, Any]:
    return {
        **chain.public_summary(),
        "checks": chain.checks,
        "sent_events": chain.sent_events,
        "final_signal": {
            "id": chain.final_signal.get("id"),
            "status": chain.final_signal.get("status"),
            "paper_status": paper_execution_status(chain.final_signal),
            "reason": chain.final_signal.get("reason"),
            "error_msg": chain.final_signal.get("error_msg"),
        },
        "orders": [
            {
                "id": row.get("id"),
                "role": row.get("role"),
                "status": row.get("status"),
                "order_id": row.get("order_id") or row.get("broker_order_id"),
                "unique_id": row.get("unique_id"),
                "trade_group_id": row.get("trade_group_id"),
                "filled_qty": row.get("filled_qty"),
            }
            for row in chain.final_orders
        ],
        "reverses": [
            {
                "id": row.get("id"),
                "action_type": row.get("action_type"),
                "status": row.get("status"),
                "reason": row.get("reason"),
            }
            for row in chain.final_reverses
        ],
    }


def write_artifacts(args: argparse.Namespace, summary: dict[str, Any], chains: list[ReplayChain], health: dict[str, Any]) -> Path:
    root = Path(args.artifact_root)
    output_dir = root / args.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = [chain_report(chain) for chain in chains]
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "health_and_metrics.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "chains.jsonl").open("w", encoding="utf-8") as fh:
        for report in reports:
            fh.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    lines = [f"# Today TV Replay Stress {args.run_id}", "", f"market_date: {args.market_date}", ""]
    lines.append("| chain | symbol | direction | classification | orders | reverses |")
    lines.append("| --- | --- | --- | --- | ---: | ---: |")
    for chain in chains:
        lines.append(
            f"| `{chain.synthetic_signal_id}` | {chain.symbol} | {chain.direction} | {chain.classification} | {len(chain.final_orders)} | {len(chain.final_reverses)} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_dir


def should_write_retry_artifacts(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "write_artifacts", False) or not getattr(args, "dry_run", False))


def persist_retry_summary(args: argparse.Namespace, base_run_id: str, payload: dict[str, Any]) -> str:
    if not should_write_retry_artifacts(args):
        return ""
    output_dir = Path(args.artifact_root) / base_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    payload["artifact_dir"] = str(output_dir)
    stored = {
        **dict(payload or {}),
        "run_id": base_run_id,
        "updated_at_et": datetime.now(ET).isoformat(),
        "artifact_dir": str(output_dir),
    }
    tmp_path = output_dir / "retry_summary.json.tmp"
    final_path = output_dir / "retry_summary.json"
    tmp_path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(final_path)

    attempts = stored.get("attempts") if isinstance(stored.get("attempts"), list) else []
    lines = [
        f"# Today TV Replay Stress Retry {base_run_id}",
        "",
        f"ok: {stored.get('ok')}",
        f"reason: {stored.get('reason')}",
        f"updated_at_et: {stored.get('updated_at_et')}",
        f"attempts: {len(attempts)}",
        "",
        "| attempt | ok | run_id | market_date | artifact | error |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for item in attempts:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {attempt} | {ok} | `{run_id}` | {market_date} | {artifact} | {error} |".format(
                attempt=item.get("attempt", ""),
                ok=item.get("ok", ""),
                run_id=item.get("run_id", ""),
                market_date=item.get("market_date", ""),
                artifact=item.get("artifact_dir", ""),
                error=safe_text(item.get("error"))[:160],
            )
        )
    (output_dir / "retry_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(output_dir)


def retry_payload(
    *,
    ok: bool,
    reason: str,
    base_run_id: str,
    args: argparse.Namespace,
    success_streak: int,
    attempts: list[dict[str, Any]],
    wait_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "retry_until_market_end": True,
        "reason": reason,
        "stable_success_runs": success_streak,
        "attempts": attempts,
        "run_id": base_run_id,
        "market_start_et": args.market_start_et,
        "market_end_et": args.market_end_et,
        "market_window_wait": wait_result or {},
    }


def execute_replay(args: argparse.Namespace, chains: list[ReplayChain], summary: dict[str, Any]) -> dict[str, Any]:
    validate_replay_window(
        dry_run=args.dry_run,
        allow_outside_window=args.allow_outside_window,
        market_start_et=args.market_start_et,
        market_end_et=args.market_end_et,
    )
    health_before: dict[str, Any] = {}
    stability_before: dict[str, Any] = {"ok": True, "phase": "before", "skipped": True, "reason": "skip_health"}
    if not args.skip_health:
        health_before = phase0_health(args)
        stability_before = evaluate_stability(args, health_before, "before")
        if not stability_before.get("ok"):
            payload = attempt_diagnostic_payload(
                args,
                summary,
                chains,
                error=f"stability_precheck_failed:{compact_json(stability_before)[:1000]}",
                stage="stability_precheck",
            )
            payload["stability"] = {"before": stability_before}
            payload["health"] = {"before": health_before}
            raise ReplayAttemptError(payload["error"], payload)
    try:
        account_before = verify_account_flat_for_chains(args, chains, phase="before")
    except Exception as exc:
        account_before = {"ok": False, "phase": "before", "error": str(exc)}
        payload = attempt_diagnostic_payload(
            args,
            summary,
            chains,
            error=f"account_preflight_error:{str(exc)[:1000]}",
            stage="account_preflight",
        )
        payload["account_flat"] = {"before": account_before}
        raise ReplayAttemptError(payload["error"], payload) from exc
    if not account_before.get("ok"):
        payload = attempt_diagnostic_payload(
            args,
            summary,
            chains,
            error=f"account_preflight_not_flat:{compact_json(account_before)[:1000]}",
            stage="account_preflight",
        )
        payload["account_flat"] = {"before": account_before}
        raise ReplayAttemptError(payload["error"], payload)
    burst_results = send_burst(args, chains)
    wait_for_routing(args, chains, {"pre_alert", "entry"})
    for chain in chains:
        wait_for_entry_processing(args, chain)
    send_followups(args, chains)
    for chain in chains:
        finalize_chain(args, chain)
    cleanup_results = cleanup_synthetic_orders(args, chains)
    post_cleanup_results = verify_no_open_synthetic_orders(args, chains)
    for chain in chains:
        finalize_chain(args, chain)
    try:
        account_after = verify_account_flat_for_chains(args, chains, phase="after")
    except Exception as exc:
        account_after = {"ok": False, "phase": "after", "error": str(exc)}
        for chain in chains:
            chain.checks.append({"name": "account_flat_after", "ok": False, "symbol": chain.symbol, "error": str(exc)})
    flow_requirements = evaluate_flow_requirements(args, chains, cleanup_results)
    if not args.skip_health:
        health_after = phase0_health(args)
    else:
        health_after = {}
    stability_after = evaluate_stability(args, health_after, "after")
    chain_ok = bool(chains) and all(not [check for check in chain.checks if check.get("ok") is False] for chain in chains)
    cleanup_ok = all(item.get("ok") for item in cleanup_results) and all(item.get("ok") for item in post_cleanup_results)
    account_ok = bool(account_before.get("ok")) and bool(account_after.get("ok"))
    flow_ok = bool(flow_requirements.get("ok"))
    stability_ok = bool(stability_after.get("ok"))
    summary.update(
        {
            "ok": chain_ok and cleanup_ok and account_ok and flow_ok and stability_ok,
            "burst_results": {
                "total": len(burst_results),
                "ok": sum(1 for item in burst_results if item.get("ok")),
                "failed": sum(1 for item in burst_results if not item.get("ok")),
            },
            "cleanup_results": {
                "total": len(cleanup_results),
                "ok": sum(1 for item in cleanup_results if item.get("ok")),
                "failed": sum(1 for item in cleanup_results if not item.get("ok")),
            },
            "post_cleanup_results": {
                "total": len(post_cleanup_results),
                "ok": sum(1 for item in post_cleanup_results if item.get("ok")),
                "failed": sum(1 for item in post_cleanup_results if not item.get("ok")),
            },
            "classifications": count_by(chains, lambda chain: chain.classification),
            "account_flat": {"before": account_before, "after": account_after},
            "flow_requirements": flow_requirements,
            "stability": {"before": stability_before, "after": stability_after},
            "chains": [chain_report(chain) for chain in chains],
        }
    )
    health = {"before": health_before, "after": health_after}
    if args.write_artifacts or not args.dry_run:
        summary["artifact_dir"] = str(write_artifacts(args, summary, chains, health))
    return summary


def run_single_attempt(args: argparse.Namespace) -> dict[str, Any]:
    chains, _excluded, summary = build_replay_plan(args)
    if args.dry_run:
        return dry_run_output(summary, chains, args)
    min_selected = max(0, int(getattr(args, "min_selected_chains", 1) or 0))
    if len(chains) < min_selected:
        error = f"insufficient_replay_chains:selected={len(chains)}:required={min_selected}"
        payload = attempt_diagnostic_payload(args, summary, chains, error=error, stage="plan_preflight")
        raise ReplayAttemptError(error, payload)
    min_full = max(0, int(getattr(args, "min_full_chains", 0) or 0))
    selected_full = sum(1 for chain in chains if chain.risk_events and chain.exit_events)
    if selected_full < min_full:
        error = f"insufficient_full_replay_chains:selected_full={selected_full}:required={min_full}"
        payload = attempt_diagnostic_payload(args, summary, chains, error=error, stage="plan_preflight")
        raise ReplayAttemptError(error, payload)
    return execute_replay(args, chains, summary)


def wait_for_next_market_window(
    args: argparse.Namespace,
    *,
    base_run_id: str = "",
    attempts: list[dict[str, Any]] | None = None,
    success_streak: int = 0,
) -> dict[str, Any]:
    wait_events: list[dict[str, Any]] = []
    while True:
        now = datetime.now(ET)
        start_dt, end_dt = next_nyse_market_window(
            market_start_et=args.market_start_et,
            market_end_et=args.market_end_et,
            now_et=now,
        )
        if now > end_dt:
            return {"ok": False, "reason": "market_end_reached_before_wait", "wait_events": wait_events}
        if now >= start_dt:
            return {
                "ok": True,
                "reason": "market_window_open",
                "window_start_et": start_dt.isoformat(),
                "window_end_et": end_dt.isoformat(),
                "wait_events": wait_events,
            }
        wait_s = (start_dt - now).total_seconds()
        sleep_s = min(max(1.0, float(args.retry_interval_seconds or 60.0)), max(0.0, wait_s))
        wait_events.append(
            {
                "now_et": now.isoformat(),
                "window_start_et": start_dt.isoformat(),
                "window_end_et": end_dt.isoformat(),
                "sleep_seconds": round(sleep_s, 3),
            }
        )
        if base_run_id:
            payload = retry_payload(
                ok=False,
                reason="waiting_market_window",
                base_run_id=base_run_id,
                args=args,
                success_streak=success_streak,
                attempts=attempts or [],
                wait_result={
                    "ok": False,
                    "reason": "waiting_market_window",
                    "window_start_et": start_dt.isoformat(),
                    "window_end_et": end_dt.isoformat(),
                    "wait_events": wait_events,
                },
            )
            persist_retry_summary(args, base_run_id, payload)
        time.sleep(sleep_s)


def run_retry_until_market_end(args: argparse.Namespace) -> dict[str, Any]:
    base_run_id = args.run_id
    base_market_date = args.market_date
    retry_uses_today_date = safe_lower(base_market_date) in {"", "latest", "latest-et-trading-day", "today", "today-et"}
    original_poll_seconds = float(args.poll_seconds or 0.0)
    attempts: list[dict[str, Any]] = []
    success_streak = 0
    attempt_no = 0
    wait_result: dict[str, Any] = {}
    if getattr(args, "wait_for_next_market_window", False):
        persist_retry_summary(
            args,
            base_run_id,
            retry_payload(
                ok=False,
                reason="waiting_market_window",
                base_run_id=base_run_id,
                args=args,
                success_streak=success_streak,
                attempts=attempts,
                wait_result={"ok": False, "reason": "waiting_market_window", "wait_events": []},
            ),
        )
        wait_result = wait_for_next_market_window(
            args,
            base_run_id=base_run_id,
            attempts=attempts,
            success_streak=success_streak,
        )
        if not wait_result.get("ok"):
            payload = retry_payload(
                ok=False,
                reason=wait_result.get("reason") or "market_window_wait_failed",
                base_run_id=base_run_id,
                args=args,
                success_streak=success_streak,
                attempts=attempts,
                wait_result=wait_result,
            )
            persist_retry_summary(args, base_run_id, payload)
            return payload
    while market_time_remaining_s(args.market_end_et) > 0:
        until_start = market_time_until_start_s(args.market_start_et)
        if until_start > 0:
            sleep_s = min(max(1.0, float(args.retry_interval_seconds or 60.0)), until_start, market_time_remaining_s(args.market_end_et))
            time.sleep(max(0.0, sleep_s))
            continue
        remaining_before = market_time_remaining_s(args.market_end_et)
        if remaining_before < max(1.0, float(args.min_attempt_seconds or 1.0)):
            break
        attempt_no += 1
        args.run_id = f"{base_run_id}_R{attempt_no:02d}"
        chain_budget = 1
        if safe_lower(args.max_chains or "all") not in {"all", "0", "none"}:
            try:
                chain_budget = max(1, int(args.max_chains))
            except Exception:
                chain_budget = 1
        per_wait_budget = max(5.0, (remaining_before - 30.0) / max(1, chain_budget * 2))
        args.poll_seconds = min(original_poll_seconds, per_wait_budget) if original_poll_seconds > 0 else per_wait_budget
        if retry_uses_today_date:
            args.market_date = "today-et"
        try:
            result = run_single_attempt(args)
        except ReplayAttemptError as exc:
            result = dict(exc.payload or {})
            result.setdefault("ok", False)
            result.setdefault("error", str(exc))
            result.setdefault("run_id", args.run_id)
            result.setdefault("market_date", args.market_date)
            result.setdefault("attempt", attempt_no)
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc),
                "run_id": args.run_id,
                "market_date": args.market_date,
                "attempt": attempt_no,
            }
        result["attempt"] = attempt_no
        result["remaining_market_seconds"] = max(0.0, round(market_time_remaining_s(args.market_end_et), 1))
        attempts.append(result)
        if result.get("ok"):
            success_streak += 1
            if success_streak >= max(1, int(args.stable_success_runs or 1)):
                args.poll_seconds = original_poll_seconds
                payload = retry_payload(
                    ok=True,
                    reason="stable_success_reached",
                    base_run_id=base_run_id,
                    args=args,
                    success_streak=success_streak,
                    attempts=attempts,
                    wait_result=wait_result,
                )
                persist_retry_summary(args, base_run_id, payload)
                return payload
        else:
            success_streak = 0
        persist_retry_summary(
            args,
            base_run_id,
            retry_payload(
                ok=False,
                reason="retrying_until_market_end",
                base_run_id=base_run_id,
                args=args,
                success_streak=success_streak,
                attempts=attempts,
                wait_result=wait_result,
            ),
        )
        remaining = market_time_remaining_s(args.market_end_et)
        if remaining <= 0:
            break
        sleep_s = min(max(1.0, float(args.retry_interval_seconds or 60.0)), remaining)
        time.sleep(sleep_s)
    args.poll_seconds = original_poll_seconds
    payload = retry_payload(
        ok=False,
        reason="market_end_reached",
        base_run_id=base_run_id,
        args=args,
        success_streak=success_streak,
        attempts=attempts,
        wait_result=wait_result,
    )
    persist_retry_summary(args, base_run_id, payload)
    return payload

def dry_run_output(summary: dict[str, Any], chains: list[ReplayChain], args: argparse.Namespace) -> dict[str, Any]:
    preview_limit = max(0, int(args.preview_payloads or 0))
    preview: list[dict[str, Any]] = []
    for chain in chains[:preview_limit]:
        events = []
        for event_type in ("pre_alert", "entry", "risk_update", "exit"):
            for payload in chain.payloads.get(event_type, []):
                events.append(
                    {
                        "event_type": event_type,
                        "event_id": payload.get("event_id"),
                        "signal_id": payload.get("signal_id"),
                        "position_id": payload.get("position_id"),
                        "bar_time_ms": payload.get("bar_time_ms"),
                        "pine_eval_ms": payload.get("pine_eval_ms"),
                    }
                )
        preview.append({"chain": chain.public_summary(), "payloads": events})
    return {"ok": True, "dry_run": True, **summary, "payload_preview": preview}


def attempt_diagnostic_payload(
    args: argparse.Namespace,
    summary: dict[str, Any],
    chains: list[ReplayChain],
    *,
    error: str,
    stage: str,
) -> dict[str, Any]:
    payload = dry_run_output(summary, chains, args)
    payload.update(
        {
            "ok": False,
            "dry_run": bool(getattr(args, "dry_run", False)),
            "plan_diagnostic": True,
            "error": error,
            "stage": stage,
        }
    )
    return payload


def apply_strict_canary_preset(args: argparse.Namespace) -> argparse.Namespace:
    if not getattr(args, "strict_canary", False):
        return args
    args.market_date = "today-et"
    args.max_chains = "3"
    args.burst_workers = 3
    args.poll_seconds = 240.0
    args.retry_until_market_end = True
    args.wait_for_next_market_window = True
    args.market_start_et = "09:45"
    args.market_end_et = "14:30"
    args.retry_interval_seconds = 60.0
    args.stable_success_runs = 1
    args.min_attempt_seconds = 180.0
    args.min_selected_chains = 3
    args.min_full_chains = 3
    args.min_bracket_chains = 3
    args.min_filled_entry_chains = 1
    args.min_routed_exit_chains = 1
    args.min_cleanup_exit_chains = 0
    args.min_closed_reverse_chains = 1
    args.stability_lookback_minutes = 5.0
    args.skip_health = False
    args.skip_stability_gate = False
    args.skip_account_flat_check = False
    args.no_cleanup = False
    args.no_prefer_full_chains = False
    args.followup_stress_concurrent = True
    args.risk_burst_workers = 3
    args.exit_burst_workers = 3
    args.followup_burst_spacing_seconds = 0.0
    args.risk_spacing_seconds = 0.0
    args.exit_spacing_seconds = 0.0
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_run_id = "SIMTV" + datetime.now(ET).strftime("%Y%m%d%H%M%S")
    parser = argparse.ArgumentParser(description="Replay today's untouched TV signal chains into paper for stress validation.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--market-date", default="latest-et-trading-day")
    parser.add_argument("--broker-mode", default="paper", choices=["paper"])
    parser.add_argument("--data-environment", default="live", choices=["live", "paper"])
    parser.add_argument("--run-id", default=default_run_id)
    parser.add_argument("--max-chains", default="all", help='Number of candidate chains to replay, or "all".')
    parser.add_argument("--burst-workers", type=int, default=12)
    parser.add_argument("--poll-seconds", type=float, default=360.0)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--http-timeout", type=float, default=20.0)
    parser.add_argument("--account-snapshot-path", default=DEFAULT_ACCOUNT_SNAPSHOT_PATH)
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument(
        "--strict-canary",
        action="store_true",
        help="Apply the recommended intraday retry stress preset with strict flow and stability gates.",
    )
    parser.add_argument("--skip-pre-alert", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-stability-gate", action="store_true")
    parser.add_argument("--skip-account-flat-check", action="store_true")
    parser.add_argument("--allow-outside-window", action="store_true")
    parser.add_argument(
        "--no-prefer-full-chains",
        action="store_true",
        help="When max-chains limits the canary, keep chronological order instead of preferring chains with risk_update and exit events.",
    )
    parser.add_argument(
        "--allow-active-symbols",
        action="store_true",
        help="Do not pre-filter symbols that already have active paper/live signal state; use only for targeted active-policy debugging.",
    )
    parser.add_argument("--retry-until-market-end", action="store_true")
    parser.add_argument(
        "--wait-for-next-market-window",
        action="store_true",
        help="If started before today's window, after the window, or on a NYSE holiday/weekend, wait for the next NYSE market window before retrying.",
    )
    parser.add_argument("--market-start-et", default="09:45")
    parser.add_argument("--market-end-et", default="14:30")
    parser.add_argument("--retry-interval-seconds", type=float, default=60.0)
    parser.add_argument("--stable-success-runs", type=int, default=1)
    parser.add_argument("--min-attempt-seconds", type=float, default=180.0)
    parser.add_argument("--min-selected-chains", type=int, default=1)
    parser.add_argument("--min-full-chains", type=int, default=0)
    parser.add_argument("--min-bracket-chains", type=int, default=0)
    parser.add_argument("--min-filled-entry-chains", type=int, default=0)
    parser.add_argument("--min-routed-exit-chains", type=int, default=0)
    parser.add_argument("--min-cleanup-exit-chains", type=int, default=0)
    parser.add_argument("--min-closed-reverse-chains", type=int, default=0)
    parser.add_argument("--stability-lookback-minutes", type=float, default=5.0)
    parser.add_argument("--max-firing-alerts", type=float, default=0.0)
    parser.add_argument("--max-broker-pending-requests", type=float, default=0.0)
    parser.add_argument("--max-order-failures", type=float, default=0.0)
    parser.add_argument("--max-signal-attention", type=float, default=0.0)
    parser.add_argument("--max-order-operation-p95", type=float, default=10.0)
    parser.add_argument("--max-gateway-serial-wait-p95", type=float, default=2.0)
    parser.add_argument("--max-gateway-serial-timeouts", type=float, default=0.0)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--preview-payloads", type=int, default=3)
    parser.add_argument(
        "--followup-stress-concurrent",
        action="store_true",
        help="Send all eligible risk_update payloads as one burst and all eligible exit payloads as one burst after entry processing.",
    )
    parser.add_argument("--risk-burst-workers", type=int, default=1)
    parser.add_argument("--exit-burst-workers", type=int, default=1)
    parser.add_argument("--followup-burst-spacing-seconds", type=float, default=0.0)
    parser.add_argument("--risk-spacing-seconds", type=float, default=0.5)
    parser.add_argument("--exit-spacing-seconds", type=float, default=0.5)
    parser.add_argument("--cleanup-spacing-seconds", type=float, default=0.5)
    parser.add_argument("--pre-alert-lookback-minutes", type=float, default=120.0)
    return apply_strict_canary_preset(parser.parse_args(argv))


def print_text_summary(payload: dict[str, Any]) -> None:
    print(f"ok={payload.get('ok')} dry_run={payload.get('dry_run', False)} run_id={payload.get('run_id')}")
    if payload.get("retry_until_market_end"):
        print(
            f"retry_until_market_end=True reason={payload.get('reason')} "
            f"attempts={len(payload.get('attempts') or [])} "
            f"stable_success_runs={payload.get('stable_success_runs')} "
            f"market_start_et={payload.get('market_start_et')} market_end_et={payload.get('market_end_et')}"
        )
        return
    print(
        "market_date={market_date} entry_chains={entry_chains} excluded={excluded_chains} "
        "candidates={candidate_chains} selected={selected_chains} "
        "active_symbol_excluded={active_symbol_excluded_chains}".format(**payload)
    )
    if payload.get("exclude_reasons"):
        print(f"exclude_reasons={compact_json(payload.get('exclude_reasons'))}")
    if payload.get("classifications"):
        print(f"classifications={compact_json(payload.get('classifications'))}")
    if payload.get("artifact_dir"):
        print(f"artifact_dir={payload.get('artifact_dir')}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.retry_until_market_end and not args.dry_run:
            payload = run_retry_until_market_end(args)
        else:
            payload = run_single_attempt(args)
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_text_summary(payload)
        return 0 if payload.get("ok") else 1
    except ReplayAttemptError as exc:
        payload = dict(exc.payload or {})
        payload.setdefault("ok", False)
        payload.setdefault("error", str(exc))
        payload.setdefault("run_id", getattr(args, "run_id", ""))
        payload.setdefault("market_date", getattr(args, "market_date", ""))
        payload.setdefault("broker_mode", getattr(args, "broker_mode", "paper"))
        payload.setdefault("data_environment", getattr(args, "data_environment", "live"))
        if getattr(args, "format", "text") == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"ok=False error={payload['error']}", file=sys.stderr)
        return 1
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "run_id": getattr(args, "run_id", ""),
            "market_date": getattr(args, "market_date", ""),
            "broker_mode": getattr(args, "broker_mode", "paper"),
            "data_environment": getattr(args, "data_environment", "live"),
        }
        if getattr(args, "format", "text") == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"ok=False error={payload['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
