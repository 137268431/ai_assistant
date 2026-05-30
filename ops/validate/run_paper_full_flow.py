#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Any, Callable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

DEFAULT_BASE_URL = (
    os.environ.get("IBKR_FULL_FLOW_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("CONSOLE_BASE_URL")
    or "https://quant.lzw-glory.top"
)
DEFAULT_HOST = os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.246")
DEFAULT_DB_PATH = os.environ.get("PB_DB_PATH", "/opt/pocketbase/pb_data/data.db")
DEFAULT_SYMBOL = os.environ.get("IBKR_FULL_FLOW_SYMBOL", "INTC").upper()
DEFAULT_BROKER_MODE = "paper"
DEFAULT_DATA_ENVIRONMENT = "live"
VALIDATION_TAG = "paper_full_flow_validation"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ValidationError(RuntimeError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def normalize_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/") or DEFAULT_BASE_URL


def post_json(base_url: str, path: str, payload: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    url = f"{normalize_base_url(base_url)}/{str(path or '').strip('/')}"
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ibkr-paper-full-flow-validation/1.0",
        },
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
        raise ValidationError(f"http_request_failed:{url}:{exc}") from exc

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


def remote_sql(host: str, db_path: str, query: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    remote_script = f"""
import json
import sqlite3
import sys

db_path = {json.dumps(str(db_path))}
query = {json.dumps(str(query))}
params = {json.dumps(list(params))}

conn = sqlite3.connect(db_path, timeout=20)
conn.row_factory = sqlite3.Row
try:
    rows = [dict(row) for row in conn.execute(query, params).fetchall()]
finally:
    conn.close()
print(json.dumps({{"ok": True, "rows": rows}}, ensure_ascii=False))
"""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-"]
    proc = subprocess.run(command, input=remote_script, text=True, capture_output=True, timeout=45)
    if proc.returncode != 0:
        raise ValidationError(f"remote_sql_failed:{proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"remote_sql_non_json:{proc.stdout[:500]}") from exc
    if not payload.get("ok"):
        raise ValidationError(f"remote_sql_error:{payload}")
    return [dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)]


def wait_for(
    label: str,
    fn: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout_s: float,
    interval_s: float,
) -> Any:
    deadline = time.time() + max(1.0, float(timeout_s))
    last_value: Any = None
    while time.time() <= deadline:
        last_value = fn()
        if predicate(last_value):
            return last_value
        time.sleep(max(0.25, float(interval_s)))
    raise ValidationError(f"timeout_waiting_for:{label}:last={compact_json(last_value)[:1000]}")


def latest_rows(host: str, db_path: str, table: str, where: str, params: list[Any], *, order: str = "created desc") -> list[dict[str, Any]]:
    return remote_sql(
        host,
        db_path,
        f"select * from {table} where {where} order by {order} limit 50",
        params,
    )


def first_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(rows[0]) if rows else {}


def choose_validation_time() -> datetime:
    current = datetime.now(ET)
    # Keep TV route inside the configured quality window and runtime expiry fresh.
    if current.time() > dt_time(14, 30):
        return current.replace(hour=14, minute=20, second=0, microsecond=0)
    if current.time() < dt_time(9, 45):
        return current.replace(hour=9, minute=50, second=0, microsecond=0)
    return current.replace(second=0, microsecond=0)


def build_payloads(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    event_time = choose_validation_time()
    event_ms = int(event_time.timestamp() * 1000)
    unique = f"{args.prefix}_{args.symbol}_{int(time.time())}"
    position_id = f"{unique}_long_position"
    signal_id = f"{unique}_entry"
    us_time = event_time.strftime("%Y-%m-%d %H:%M:%S")
    cn_time = event_time.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")

    base = {
        "source": "tv",
        "symbol": args.symbol,
        "exchange": "BATS",
        "broker_mode": args.broker_mode,
        "market_data_mode": args.data_environment,
        "data_environment": args.data_environment,
        "environment": args.data_environment,
        "market_date": event_time.strftime("%Y-%m-%d"),
        "date": event_time.strftime("%Y-%m-%d"),
        "us_time": us_time,
        "cn_time": cn_time,
        "bar_time_ms": event_ms,
        "bar_close_ms": event_ms + 120000,
        "pine_eval_ms": event_ms + 121000,
        "interval": "2",
        "chart_tf": "2",
        "timeframe_stack": "chart=2",
        "script_tag": "Signal_Strategy_Core[Glory]",
        "strategy_version": "SSC_v1_20260529",
        "tv_chart_url": "",
        "activity_score": 100,
        "quality_score": 100,
        "qualified": True,
        "extra": {
            "source": "tradingview",
            "validation_tag": VALIDATION_TAG,
            "validation_prefix": args.prefix,
        },
    }
    entry = float(args.entry)
    stop_loss = float(args.stop_loss)
    take_profit = float(args.take_profit)
    if not (0 < stop_loss < entry < take_profit):
        raise ValidationError("default scenario expects long prices with stop_loss < entry < take_profit")

    pre_alert = {
        **base,
        "event_type": "pre_alert",
        "event_id": f"{unique}_pre_alert",
        "position_id": position_id,
        "direction_bias": "long",
        "reason": "paper full-flow pre-alert validation",
    }
    entry_payload = {
        **base,
        "event_type": "entry",
        "event_id": signal_id,
        "position_id": position_id,
        "signal_id": signal_id,
        "direction": "long",
        "position_side": "long",
        "entry_setup": "paper_full_flow_entry",
        "signal": "paper_full_flow_entry",
        "entry": entry,
        "limit_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "shares": int(args.shares),
        "rr": "1.50:1",
        "risk_r": entry - stop_loss,
        "reason": "paper full-flow entry validation",
    }
    initial_risk = {
        **base,
        "event_type": "risk_update",
        "event_id": f"{unique}_risk_initial",
        "position_id": position_id,
        "signal_id": signal_id,
        "direction": "long",
        "position_side": "long",
        "risk_update_reason": "initial_protection",
        "update_reason": "initial_protection",
        "previous_stop_loss": 0,
        "previous_take_profit": 0,
        "new_stop_loss": stop_loss,
        "new_take_profit": take_profit,
        "entry_price": entry,
        "quantity": int(args.shares),
    }
    breakeven_risk = {
        **initial_risk,
        "event_id": f"{unique}_risk_breakeven",
        "risk_update_reason": "breakeven_trail",
        "update_reason": "breakeven_trail",
        "previous_stop_loss": stop_loss,
        "previous_take_profit": take_profit,
        "new_stop_loss": entry + float(args.breakeven_offset),
        "new_take_profit": take_profit,
    }
    exit_payload = {
        **base,
        "event_type": "exit",
        "event_id": f"{unique}_exit",
        "position_id": position_id,
        "signal_id": signal_id,
        "direction": "long",
        "position_side": "long",
        "exit_reason": "paper_full_flow_exit_validation",
        "entry_price": entry,
        "stop_loss": breakeven_risk["new_stop_loss"],
        "take_profit": take_profit,
        "quantity": int(args.shares),
    }
    return {
        "pre_alert": pre_alert,
        "entry": entry_payload,
        "initial_risk": initial_risk,
        "breakeven_risk": breakeven_risk,
        "exit": exit_payload,
    }


def stamp_runtime_payloads(payloads: dict[str, dict[str, Any]], names: tuple[str, ...]) -> None:
    event_time = datetime.now(ET).replace(second=0, microsecond=0)
    event_ms = int(event_time.timestamp() * 1000)
    for name in names:
        payload = payloads.get(name)
        if not isinstance(payload, dict):
            continue
        payload.update(
            {
                "market_date": event_time.strftime("%Y-%m-%d"),
                "date": event_time.strftime("%Y-%m-%d"),
                "us_time": event_time.strftime("%Y-%m-%d %H:%M:%S"),
                "cn_time": event_time.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
                "bar_time_ms": event_ms,
                "bar_close_ms": event_ms + 120000,
                "pine_eval_ms": event_ms + 121000,
            }
        )


def check_response(name: str, response: dict[str, Any], checks: list[Check]) -> None:
    status = int(response.get("_http_status") or 0)
    ok = status < 400 and bool(response.get("ok") or response.get("success"))
    checks.append(Check(name, ok, response.get("error") or response.get("reason") or "", {"response": response}))
    if not ok:
        raise ValidationError(f"{name}_failed:{compact_json(response)[:1000]}")


def query_signal(args: argparse.Namespace, signal_id: str) -> dict[str, Any]:
    rows = latest_rows(
        args.host,
        args.db_path,
        "ibkr_signals",
        "signal_id = ? and environment = ?",
        [signal_id, args.data_environment],
    )
    return first_row(rows)


def query_orders(args: argparse.Namespace, signal_id: str) -> list[dict[str, Any]]:
    return latest_rows(
        args.host,
        args.db_path,
        "orders",
        "signal_id = ? and environment = ?",
        [signal_id, args.broker_mode],
        order="created asc",
    )


def query_reverse_by_id(args: argparse.Namespace, reverse_id: str) -> dict[str, Any]:
    rows = latest_rows(
        args.host,
        args.db_path,
        "ibkr_reverse_signals",
        "id = ? and environment = ?",
        [reverse_id, args.broker_mode],
    )
    return first_row(rows)


def signal_needs_confirm(signal: dict[str, Any], broker_mode: str) -> bool:
    extra = as_object(signal.get("extra"))
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra.get("execution_by_mode"), dict) else {}
    broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    broker_status = str((broker_execution or {}).get("status") or "").strip().lower() if isinstance(broker_execution, dict) else ""
    return broker_status in {"awaiting_confirm", "confirm_pending"} or bool(extra.get("signal_confirmation_required"))


def signal_rejection_reason(signal: dict[str, Any], broker_mode: str) -> str:
    extra = as_object(signal.get("extra"))
    if bool(extra.get("validation_rejected")):
        return str(extra.get("rejection_reason_code") or extra.get("status_reason") or "validation_rejected")
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra.get("execution_by_mode"), dict) else {}
    broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    if isinstance(broker_execution, dict):
        status = str(broker_execution.get("status") or "").strip().lower()
        if status in {"rejected", "blocked", "expired", "submit_failed"}:
            return str(broker_execution.get("status_reason") or broker_execution.get("note") or status)
    status = str(signal.get("status") or "").strip().lower()
    if status in {"rejected", "blocked", "expired"}:
        return str(signal.get("reason") or signal.get("error_msg") or status)
    return ""


def query_reverses(args: argparse.Namespace, signal_id: str, action_type: str = "") -> list[dict[str, Any]]:
    params: list[Any] = [args.broker_mode, f"%{signal_id}%"]
    where = "environment = ? and triggered_signals like ?"
    if action_type:
        where += " and action_type = ?"
        params.append(action_type)
    return latest_rows(
        args.host,
        args.db_path,
        "ibkr_reverse_signals",
        where,
        params,
    )


def role_map(orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in orders:
        role = str(row.get("role") or row.get("order_type") or "").strip().lower()
        if role and role not in out:
            out[role] = row
    return out


def order_price(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = float(row.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0:
            return value
    return 0.0


def validate_adjust_reverse(reverse: dict[str, Any], *, require_confirmed: bool = True) -> tuple[bool, str, dict[str, Any]]:
    status = str(reverse.get("status") or "").strip().lower()
    extra = as_object(reverse.get("extra"))
    adjust_results = extra.get("adjust_results") if isinstance(extra.get("adjust_results"), dict) else {}
    bracket_result = extra.get("adjust_bracket_result") if isinstance(extra.get("adjust_bracket_result"), dict) else {}
    failed = list(bracket_result.get("failed_sides") or []) if isinstance(bracket_result, dict) else []
    succeeded = list(bracket_result.get("succeeded_sides") or []) if isinstance(bracket_result, dict) else []
    required_sides = {"stop_loss", "take_profit"}
    sides_ok = required_sides.issubset(set(succeeded))
    if require_confirmed and status != "confirmed":
        return False, f"status={status or 'missing'}", extra
    if failed:
        return False, f"failed_sides={failed}", extra
    if not sides_ok:
        return False, f"succeeded_sides={succeeded}", extra
    for side in required_sides:
        result = adjust_results.get(side) if isinstance(adjust_results, dict) else {}
        if isinstance(result, dict) and result.get("ok") is False:
            return False, f"{side}_result_not_ok", extra
    return True, "adjust_bracket confirmed", extra


def wait_for_adjust_reverse(args: argparse.Namespace, response: dict[str, Any], checks: list[Check], name: str) -> dict[str, Any]:
    reverse_id = str(response.get("id") or "").strip()
    if not reverse_id:
        raise ValidationError(f"{name}_missing_reverse_id:{compact_json(response)[:1000]}")
    reverse = wait_for(
        name,
        lambda: query_reverse_by_id(args, reverse_id),
        lambda row: validate_adjust_reverse(row)[0],
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    ok, detail, extra = validate_adjust_reverse(reverse)
    checks.append(
        Check(
            name,
            ok,
            detail,
            {
                "reverse_id": reverse_id,
                "status": reverse.get("status"),
                "reason": reverse.get("reason"),
                "new_sl": extra.get("new_sl"),
                "new_tp": extra.get("new_tp"),
                "sl_order_id": extra.get("sl_order_id"),
                "tp_order_id": extra.get("tp_order_id"),
                "adjust_bracket_result": extra.get("adjust_bracket_result"),
            },
        )
    )
    if not ok:
        raise ValidationError(f"{name}_failed:{compact_json(reverse)[:1000]}")
    return reverse


def run_validation(args: argparse.Namespace) -> tuple[list[Check], dict[str, Any]]:
    checks: list[Check] = []
    payloads = build_payloads(args)
    signal_id = str(payloads["entry"]["signal_id"])
    position_id = str(payloads["entry"]["position_id"])

    entry_response: dict[str, Any] = {}
    for name in ("pre_alert", "entry"):
        response = post_json(args.base_url, "/webhook/tv", payloads[name], timeout=args.http_timeout)
        check_response(f"tv_{name}", response, checks)
        if name == "entry":
            entry_response = response

    routed_signal_id = str(entry_response.get("signal_id") or signal_id).strip()
    if routed_signal_id and routed_signal_id != signal_id:
        checks.append(
            Check(
                "entry_routed_to_active_signal",
                True,
                "entry was merged/refreshed into an existing active same-direction signal",
                {
                    "requested_signal_id": signal_id,
                    "routed_signal_id": routed_signal_id,
                    "action": entry_response.get("action"),
                    "merged_signal_id": entry_response.get("merged_signal_id"),
                    "followup_signal_id": entry_response.get("followup_signal_id"),
                },
            )
        )
        signal_id = routed_signal_id
        for name in ("initial_risk", "breakeven_risk", "exit"):
            payloads[name]["signal_id"] = signal_id

    signal = wait_for(
        "signal_record",
        lambda: query_signal(args, signal_id),
        lambda row: bool(row.get("id")),
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    checks.append(
        Check(
            "signal_persisted",
            True,
            "ibkr_signals row found",
            {"id": signal.get("id"), "signal_id": signal_id, "status": signal.get("status")},
        )
    )
    rejection_reason = signal_rejection_reason(signal, args.broker_mode)
    if rejection_reason:
        checks.append(
            Check(
                "signal_execution_rejected",
                False,
                rejection_reason,
                {"signal_id": signal_id, "record_id": signal.get("id"), "status": signal.get("status")},
            )
        )
        raise ValidationError(f"signal_execution_rejected:{rejection_reason}")

    if signal_needs_confirm(signal, args.broker_mode):
        confirm_response = post_json(
            args.base_url,
            "/webhook/feishu/callback",
            {
                "event": {
                    "token": f"{args.prefix}-signal-confirm-token",
                    "action": {
                        "value": {
                            "action": "confirm",
                            "signal_id": signal_id,
                            "broker_mode": args.broker_mode,
                            "environment": args.broker_mode,
                            "market_data_mode": args.data_environment,
                            "data_environment": args.data_environment,
                        }
                    },
                }
            },
            timeout=args.http_timeout,
        )
        status = int(confirm_response.get("_http_status") or 0)
        toast = as_object(confirm_response.get("toast"))
        confirm_ok = status < 400 and (bool(confirm_response.get("ok")) or bool(toast))
        checks.append(
            Check(
                "feishu_signal_confirm_callback",
                confirm_ok,
                toast.get("content") or confirm_response.get("error") or "",
                {"signal_id": signal_id, "response": confirm_response},
            )
        )
        if not confirm_ok:
            raise ValidationError(f"feishu_signal_confirm_callback_failed:{compact_json(confirm_response)[:1000]}")

    orders = wait_for(
        "entry_tp_sl_orders",
        lambda: query_orders(args, signal_id),
        lambda rows: {"entry", "take_profit", "stop_loss"}.issubset(set(role_map(rows))),
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    order_roles = role_map(orders)
    checks.append(
        Check(
            "auto_execution_bracket_orders",
            True,
            "entry/take_profit/stop_loss orders found",
            {
                "order_ids": {role: order_roles[role].get("id") for role in ("entry", "take_profit", "stop_loss")},
                "trade_group_id": order_roles.get("entry", {}).get("trade_group_id"),
            },
        )
    )

    # Rebase risk payload prices to the actual submitted bracket, because the
    # runtime guard can reprice TV's static intent just before IBKR submission.
    persisted_entry = order_price(order_roles.get("entry", {}), "limit_price", "entry", "fill_price") or float(
        signal.get("entry") or payloads["entry"]["entry"]
    )
    persisted_sl = order_price(order_roles.get("stop_loss", {}), "limit_price", "sl_price", "stop_loss") or float(
        signal.get("stop_loss") or payloads["entry"]["stop_loss"]
    )
    persisted_tp = order_price(order_roles.get("take_profit", {}), "limit_price", "tp_price", "take_profit") or float(
        signal.get("take_profit") or payloads["entry"]["take_profit"]
    )
    risk_r = max(0.01, persisted_entry - persisted_sl)
    breakeven_sl = round(persisted_entry + min(max(float(args.breakeven_offset), 0.01), risk_r * 0.2), 4)
    payloads["initial_risk"].update(
        {
            "entry_price": persisted_entry,
            "new_stop_loss": persisted_sl,
            "new_take_profit": persisted_tp,
        }
    )
    payloads["breakeven_risk"].update(
        {
            "entry_price": persisted_entry,
            "previous_stop_loss": persisted_sl,
            "previous_take_profit": persisted_tp,
            "new_stop_loss": breakeven_sl,
            "new_take_profit": persisted_tp,
        }
    )
    payloads["exit"].update(
        {
            "entry_price": persisted_entry,
            "stop_loss": breakeven_sl,
            "take_profit": persisted_tp,
        }
    )
    stamp_runtime_payloads(payloads, ("initial_risk", "breakeven_risk", "exit"))

    risk_reverse_rows: list[dict[str, Any]] = []
    for name in ("initial_risk", "breakeven_risk"):
        response = post_json(args.base_url, "/webhook/tv", payloads[name], timeout=args.http_timeout)
        check_response(f"tv_{name}", response, checks)
        risk_reverse_rows.append(wait_for_adjust_reverse(args, response, checks, f"{name}_broker_adjust_confirmed"))

    response = post_json(args.base_url, "/webhook/tv", payloads["exit"], timeout=args.http_timeout)
    check_response("tv_exit", response, checks)

    reverses = wait_for(
        "risk_update_reverse_records",
        lambda: query_reverses(args, signal_id, "adjust_bracket"),
        lambda rows: len(rows) >= 2,
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    close_reverses = wait_for(
        "exit_reverse_record",
        lambda: query_reverses(args, signal_id, "close"),
        lambda rows: len(rows) >= 1,
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    latest_adjust = reverses[0]
    adjust_extra = as_object(latest_adjust.get("extra"))
    checks.append(
        Check(
            "risk_update_adjust_bracket",
            bool(adjust_extra.get("sl_order_id") and adjust_extra.get("tp_order_id")),
            "adjust_bracket reverse includes child order ids",
            {
                "reverse_id": latest_adjust.get("id"),
                "status": latest_adjust.get("status"),
                "reason": latest_adjust.get("reason"),
                "new_sl": adjust_extra.get("new_sl"),
                "new_tp": adjust_extra.get("new_tp"),
                "sl_order_id": adjust_extra.get("sl_order_id"),
                "tp_order_id": adjust_extra.get("tp_order_id"),
            },
        )
    )
    checks.append(
        Check(
            "tv_exit_close_reverse",
            True,
            "close reverse record found",
            {"reverse_id": close_reverses[0].get("id"), "status": close_reverses[0].get("status")},
        )
    )

    entry_order = order_roles.get("entry", {})
    trade_group_id = str(entry_order.get("trade_group_id") or entry_order.get("unique_id") or "")
    callback_response: dict[str, Any] = {}
    if trade_group_id and not args.skip_feishu_callback:
        callback_response = post_json(
            args.base_url,
            "/webhook/feishu/callback",
            {
                "event": {
                    "token": f"{args.prefix}-callback-token",
                    "action": {
                        "value": {
                            "action": "cancel",
                            "order_id": trade_group_id,
                            "signal_id": signal_id,
                            "broker_mode": args.broker_mode,
                            "environment": args.broker_mode,
                            "market_data_mode": args.data_environment,
                            "data_environment": args.data_environment,
                        }
                    },
                }
            },
            timeout=args.http_timeout,
        )
        status = int(callback_response.get("_http_status") or 0)
        toast = as_object(callback_response.get("toast"))
        callback_ok = status < 400 and (bool(callback_response.get("ok")) or bool(toast) or bool(callback_response.get("payload")))
        checks.append(
            Check(
                "feishu_order_cancel_callback",
                callback_ok,
                toast.get("content") or callback_response.get("error") or "",
                {"trade_group_id": trade_group_id, "response": callback_response},
            )
        )
        if not callback_ok:
            raise ValidationError(f"feishu_order_cancel_callback_failed:{compact_json(callback_response)[:1000]}")

    final_orders = query_orders(args, signal_id)
    summary = {
        "ok": all(check.ok for check in checks),
        "base_url": args.base_url,
        "broker_mode": args.broker_mode,
        "data_environment": args.data_environment,
        "symbol": args.symbol,
        "signal_id": signal_id,
        "position_id": position_id,
        "trade_group_id": trade_group_id,
        "signal_record_id": signal.get("id"),
        "orders": [
            {
                "id": row.get("id"),
                "role": row.get("role"),
                "status": row.get("status"),
                "order_id": row.get("order_id") or row.get("broker_order_id"),
                "unique_id": row.get("unique_id"),
                "limit_price": row.get("limit_price"),
            }
            for row in final_orders
        ],
        "risk_adjust_reverse_ids": [row.get("id") for row in risk_reverse_rows if row.get("id")],
        "reverse_ids": [row.get("id") for row in reverses + close_reverses if row.get("id")],
        "checks": [check.__dict__ for check in checks],
    }
    return checks, summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate existing TV -> paper order -> risk update -> Feishu callback flow.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL.upper())
    parser.add_argument("--broker-mode", default=DEFAULT_BROKER_MODE, choices=["paper"])
    parser.add_argument("--data-environment", default=DEFAULT_DATA_ENVIRONMENT, choices=["live", "paper"])
    parser.add_argument("--prefix", default=f"VALTVFF{now_ms()}")
    parser.add_argument("--shares", type=int, default=1)
    parser.add_argument("--entry", type=float, default=100.0)
    parser.add_argument("--stop-loss", type=float, default=98.0)
    parser.add_argument("--take-profit", type=float, default=103.0)
    parser.add_argument("--breakeven-offset", type=float, default=0.10)
    parser.add_argument("--poll-seconds", type=float, default=90.0)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--http-timeout", type=float, default=20.0)
    parser.add_argument("--skip-feishu-callback", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _checks, summary = run_validation(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary.get("ok") else 1
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "base_url": getattr(args, "base_url", DEFAULT_BASE_URL),
            "broker_mode": getattr(args, "broker_mode", DEFAULT_BROKER_MODE),
            "data_environment": getattr(args, "data_environment", DEFAULT_DATA_ENVIRONMENT),
            "symbol": getattr(args, "symbol", DEFAULT_SYMBOL),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
