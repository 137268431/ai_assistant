#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any
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
    validate_adjust_reverse,
    wait_for,
)
from run_today_tv_replay_stress import (  # noqa: E402
    DEFAULT_ACCOUNT_SNAPSHOT_PATH,
    DEFAULT_PROMETHEUS_URL,
    evaluate_stability,
    fetch_account_snapshot,
    phase0_health,
    validate_paper_account_snapshot,
)


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

CONFIRM_TEXT = "PAPER_TV_PREMARKET_LINKAGE"
DEFAULT_RUN_ID_PREFIX = "TVPREMKT"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/validation/tv_premarket_linkage")
DEFAULT_SYMBOLS = "auto"
DEFAULT_EXCLUDE_SYMBOLS = "SPY,QQQ,DIA,IWM,VIX,UVXY,SQQQ,TQQQ"
ACTIVE_ORDER_STATUSES = {
    "api_pending",
    "pending_submit",
    "presubmitted",
    "pre_submitted",
    "submitted",
    "submitted_waiting_fill",
    "init",
    "planned",
}
TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "canceled", "closed", "inactive", "api_cancelled", "rejected"}
REDACT_KEYS = {"validation_token", "tv_validation_token"}


@dataclass
class LinkageChain:
    symbol: str
    direction: str
    signal_id: str
    trade_group_id: str
    position_id: str
    quantity: int
    entry: float
    stop_loss: float
    take_profit: float
    conid: int = 0
    quote: dict[str, Any] = field(default_factory=dict)
    price_plan: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    reverses: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    completed: bool = False
    failed_reason: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "signal_id": self.signal_id,
            "trade_group_id": self.trade_group_id,
            "quantity": self.quantity,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "conid": self.conid,
            "quote": self.quote,
            "price_plan": self.price_plan,
            "completed": self.completed,
            "failed_reason": self.failed_reason,
            "checks": self.checks,
            "events": self.events,
            "orders": self.orders,
            "reverses": self.reverses,
            "fills": self.fills,
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
    except Exception:
        return default
    if number != number:
        return default
    return number


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_text(value: Any) -> str:
    return str(value or "").strip()


def round_price(value: float) -> float:
    return max(0.01, round(float(value or 0.0), 2))


def split_symbols(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        symbol = "".join(ch for ch in item.strip().upper() if ch.isalnum() or ch in {".", "-"})
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _merge_symbols(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for symbol in group:
            text = safe_text(symbol).upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def _without_excluded(symbols: list[str], excluded: set[str]) -> list[str]:
    return [symbol for symbol in symbols if safe_text(symbol).upper() and safe_text(symbol).upper() not in excluded]


def _symbols_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    return split_symbols(",".join(safe_text(row.get("symbol")) for row in rows or []))


def _discover_target_symbols(args: argparse.Namespace, *, market_date: str) -> list[str]:
    rows = remote_sql(
        args.host,
        args.db_path,
        """
        select symbol
        from ibkr_targets
        where environment = ?
          and date = ?
          and status in ('active', 'candidate')
          and coalesce(json_extract(extra, '$.blocked_from_trading'), 0) not in (1, 'true', 'TRUE')
        order by
          case status when 'active' then 0 else 1 end,
          score desc,
          updated desc
        limit 200
        """,
        [args.data_environment, market_date],
    )
    return _symbols_from_rows(rows)


def _discover_watchlist_symbols(args: argparse.Namespace) -> list[str]:
    rows = remote_sql(
        args.host,
        args.db_path,
        """
        select symbol
        from watchlist
        where environment = ?
          and lower(coalesce(symbol_role, 'trade')) in ('', 'trade')
        order by bar_time_ms desc, updated desc
        limit 300
        """,
        [args.data_environment],
    )
    return _symbols_from_rows(rows)


def resolve_validation_symbols(args: argparse.Namespace) -> tuple[list[str], dict[str, Any]]:
    excluded = set(split_symbols(getattr(args, "exclude_symbols", "")))
    raw = safe_text(args.symbols)
    if raw and raw.lower() not in {"auto", "discover", "today"}:
        explicit = _without_excluded(split_symbols(raw), excluded)
        return explicit, {
            "mode": "explicit",
            "requested": raw,
            "excluded_symbols": sorted(excluded),
            "resolved_count": len(explicit),
            "resolved_symbols": explicit,
        }

    market_date = datetime.now(ET).strftime("%Y-%m-%d")
    target_symbols = _discover_target_symbols(args, market_date=market_date)
    watchlist_symbols = _discover_watchlist_symbols(args)
    resolved = _without_excluded(_merge_symbols(target_symbols, watchlist_symbols), excluded)
    if not resolved:
        raise ValidationError("auto_symbols_empty:no_trade_watchlist_or_today_targets")
    return resolved, {
        "mode": "auto",
        "market_date": market_date,
        "excluded_symbols": sorted(excluded),
        "today_target_count": len(target_symbols),
        "watchlist_count": len(watchlist_symbols),
        "resolved_count": len(resolved),
        "resolved_symbols": resolved,
        "today_target_symbols": target_symbols[:100],
        "watchlist_symbols": watchlist_symbols[:100],
    }


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key or "").strip().lower() in REDACT_KEYS:
                result[key] = "***redacted***"
            else:
                result[key] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def parse_hhmm(raw: str, default: str) -> dt_time:
    text = safe_text(raw) or default
    try:
        hour, minute = text.split(":", 1)
        return dt_time(int(hour), int(minute))
    except Exception:
        hour, minute = default.split(":", 1)
        return dt_time(int(hour), int(minute))


def default_run_id() -> str:
    return f"{DEFAULT_RUN_ID_PREFIX}_{datetime.now(ET).strftime('%Y%m%d')}_0405_ET"


def get_json(base_url: str, path: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = f"{str(base_url or DEFAULT_BASE_URL).rstrip('/')}/{path.strip('/')}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "ibkr-tv-premarket-linkage/1.0"},
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
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"ok": False, "error": "non_json_response", "raw": text[:1000]}
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "non_object_json_response", "raw": payload}
    payload["_http_status"] = status_code
    payload["_request_url"] = url
    return payload


def remote_sql_exec(host: str, db_path: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    remote_script = f"""
import json
import sqlite3
from datetime import datetime, timezone

db_path = {json.dumps(str(db_path))}
operations = {json.dumps(operations)}
now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%fZ')

conn = sqlite3.connect(db_path, timeout=20)
conn.row_factory = sqlite3.Row
updated = []
try:
    for op in operations:
        if op.get('kind') == 'upsert_config':
            key = str(op.get('key') or '')
            environment = str(op.get('environment') or 'paper')
            value = str(op.get('value') or '')
            row = conn.execute(
                'select id from config where key = ? and environment = ? limit 1',
                (key, environment),
            ).fetchone()
            payload = (
                key,
                environment,
                value,
                str(op.get('display_name') or key),
                str(op.get('description') or ''),
                str(op.get('group_name') or 'TV premarket validation'),
                str(op.get('default_value') or ''),
                int(op.get('sort_order') or 0),
                now,
            )
            if row:
                conn.execute(
                    'update config set key=?, environment=?, value=?, display_name=?, description=?, group_name=?, default_value=?, sort_order=?, updated=? where id=?',
                    (*payload, row['id']),
                )
                updated.append({{'key': key, 'environment': environment, 'value': value, 'action': 'updated'}})
            else:
                conn.execute(
                    'insert into config (key, environment, value, display_name, description, group_name, default_value, sort_order, created, updated) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (*payload[:-1], now, now),
                )
                updated.append({{'key': key, 'environment': environment, 'value': value, 'action': 'created'}})
    conn.commit()
finally:
    conn.close()
print(json.dumps({{'ok': True, 'updated': updated}}, ensure_ascii=False))
"""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-"]
    proc = subprocess.run(command, input=remote_script, text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise ValidationError(f"remote_sql_exec_failed:{proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"remote_sql_exec_non_json:{proc.stdout[:500]}") from exc
    if not payload.get("ok"):
        raise ValidationError(f"remote_sql_exec_error:{payload}")
    return payload


def config_operations(args: argparse.Namespace, *, enabled: bool) -> list[dict[str, Any]]:
    return [
        {
            "kind": "upsert_config",
            "key": "tv_premarket_validation_enabled",
            "environment": "paper",
            "value": "true" if enabled else "false",
            "display_name": "TV premarket validation enabled",
            "description": "Temporary paper-only whitelist for premarket TV linkage validation.",
            "sort_order": 7001,
        },
        {
            "kind": "upsert_config",
            "key": "tv_premarket_validation_run_id",
            "environment": "paper",
            "value": args.run_id if enabled else "",
            "display_name": "TV premarket validation run id",
            "description": "Only this validation run id may bypass the TV entry window.",
            "sort_order": 7002,
        },
        {
            "kind": "upsert_config",
            "key": "tv_premarket_validation_token",
            "environment": "paper",
            "value": args.validation_token if enabled else "",
            "display_name": "TV premarket validation token",
            "description": "Temporary token for paper-only premarket TV linkage validation.",
            "sort_order": 7003,
        },
        {
            "kind": "upsert_config",
            "key": "tv_premarket_validation_expires_at_ms",
            "environment": "paper",
            "value": str(int(args.expires_at_ms or 0)) if enabled else "0",
            "display_name": "TV premarket validation expiry",
            "description": "Epoch-ms expiry for the temporary validation whitelist.",
            "sort_order": 7004,
        },
    ]


def refresh_api_config(args: argparse.Namespace) -> dict[str, Any]:
    return get_json(args.base_url, "/status", {"environment": "paper", "cache_bust": now_ms()}, timeout=args.http_timeout)


def enable_validation_whitelist(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"ok": True, "dry_run": True, "enabled": True}
    payload = remote_sql_exec(args.host, args.db_path, config_operations(args, enabled=True))
    payload["refresh"] = refresh_api_config(args)
    return payload


def disable_validation_whitelist(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"ok": True, "dry_run": True, "enabled": False}
    payload = remote_sql_exec(args.host, args.db_path, config_operations(args, enabled=False))
    payload["refresh"] = refresh_api_config(args)
    return payload


def artifact_dir(args: argparse.Namespace) -> Path:
    root = Path(args.artifact_root)
    return root / args.run_id


def write_artifact(args: argparse.Namespace, name: str, payload: dict[str, Any]) -> Path:
    root = artifact_dir(args)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(redact_sensitive(payload), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def check_account_clean(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = fetch_account_snapshot(args)
    validate_paper_account_snapshot(snapshot)
    positions = [row for row in snapshot.get("positions") or [] if abs(safe_float(row.get("quantity", row.get("position")))) > 1e-9]
    open_orders = []
    for key in ("live_open_orders", "orders"):
        for row in snapshot.get(key) or []:
            status = safe_text(row.get("status") or row.get("order_status")).lower()
            if status and status in TERMINAL_ORDER_STATUSES:
                continue
            if row.get("is_open") is False:
                continue
            open_orders.append(row)
    return {
        "ok": not positions and not open_orders,
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "positions": positions,
        "open_orders": open_orders,
        "snapshot_summary": snapshot.get("summary") or {},
        "broker_mode": snapshot.get("broker_mode") or snapshot.get("environment"),
    }


def wait_until_start(args: argparse.Namespace) -> dict[str, Any]:
    start_time = parse_hhmm(args.start_et, "04:05")
    stop_time = parse_hhmm(args.stop_new_entry_et, "09:20")
    events: list[dict[str, Any]] = []
    while True:
        current = datetime.now(ET)
        start_dt = current.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
        stop_dt = current.replace(hour=stop_time.hour, minute=stop_time.minute, second=0, microsecond=0)
        if current >= stop_dt:
            return {"ok": False, "reason": "stop_window_reached_before_start", "events": events}
        if current >= start_dt:
            return {"ok": True, "reason": "start_window_open", "events": events}
        sleep_s = min(max(1.0, float(args.retry_interval_seconds or 30.0)), max(0.0, (start_dt - current).total_seconds()))
        event = {
            "now_et": current.isoformat(),
            "now_cn": current.astimezone(CN).isoformat(),
            "start_et": start_dt.isoformat(),
            "sleep_seconds": round(sleep_s, 3),
        }
        events.append(event)
        write_artifact(args, "waiting.json", {"ok": False, "run_id": args.run_id, **event, "events": events[-20:]})
        time.sleep(sleep_s)


def within_new_entry_window(args: argparse.Namespace) -> bool:
    stop_time = parse_hhmm(args.stop_new_entry_et, "09:20")
    current = datetime.now(ET)
    stop_dt = current.replace(hour=stop_time.hour, minute=stop_time.minute, second=0, microsecond=0)
    return current < stop_dt


def fetch_quotes(args: argparse.Namespace, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    payload = get_json(
        args.base_url,
        "/api/custom/ibkr/quotes",
        {"symbols": ",".join(symbols), "environment": args.data_environment, "market_data_mode": args.data_environment},
        timeout=args.http_timeout,
    )
    if int(payload.get("_http_status") or 0) >= 400 or not payload.get("ok"):
        raise ValidationError(f"quotes_failed:{compact_json(payload)[:1000]}")
    out: dict[str, dict[str, Any]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        symbol = safe_text(item.get("symbol")).upper()
        if symbol:
            out[symbol] = dict(item)
    return out


def quote_price(quote: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = safe_float(quote.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def build_chain_from_quote(args: argparse.Namespace, symbol: str, quote: dict[str, Any], sequence: int) -> LinkageChain | None:
    direction = args.direction.lower()
    bid = quote_price(quote, "bid", "bid_price")
    ask = quote_price(quote, "ask", "ask_price")
    last = quote_price(quote, "last_price", "last", "close")
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0 if mid > 0 else 999999.0
    if spread_bps > float(args.max_spread_bps):
        return None
    age = safe_float(quote.get("quote_age_s"), 0.0)
    if age > float(args.max_quote_age_sec):
        return None
    if bid <= last <= ask:
        reference = last
        reference_source = "last_inside_bbo"
    else:
        reference = mid
        reference_source = "mid_bbo"
    if reference <= 0:
        return None

    max_slippage_bps = max(0.0, float(args.max_limit_slippage_bps))
    cushion_bps = max(0.0, float(args.marketable_bps))
    if direction == "long":
        required_bps = max(0.0, (ask / reference - 1.0) * 10000.0)
        if required_bps > max_slippage_bps:
            return None
        limit_cap_bps = min(max_slippage_bps, required_bps + cushion_bps)
        submitted_preview = round_price(reference * (1.0 + limit_cap_bps / 10000.0))
        risk = max(0.05, reference * float(args.protection_gap_pct))
        stop = round_price(reference - risk)
        take = round_price(reference + risk * float(args.reward_r))
    else:
        required_bps = max(0.0, (1.0 - bid / reference) * 10000.0)
        if required_bps > max_slippage_bps:
            return None
        limit_cap_bps = min(max_slippage_bps, required_bps + cushion_bps)
        submitted_preview = round_price(reference * (1.0 - limit_cap_bps / 10000.0))
        risk = max(0.05, reference * float(args.protection_gap_pct))
        stop = round_price(reference + risk)
        take = round_price(reference - risk * float(args.reward_r))
    entry = round_price(reference)
    if entry <= 0 or stop <= 0 or take <= 0 or submitted_preview <= 0:
        return None
    quantity = max(1, int(math.ceil(float(args.target_notional_per_chain) / entry)))
    unique = f"{args.run_id}_{sequence:03d}_{symbol}_{int(time.time())}"
    quote_snapshot = {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last_price": last,
        "mid": round_price(mid),
        "quote_age_s": age,
        "spread_bps": round(spread_bps, 4),
        "conid": safe_int(quote.get("conid") or quote.get("conidEx"), 0),
    }
    price_plan = {
        "entry_reference_price": entry,
        "entry_reference_source": reference_source,
        "submitted_limit_preview": submitted_preview,
        "submitted_limit_cap_bps": round(limit_cap_bps, 4),
        "required_to_cross_bps": round(required_bps, 4),
        "marketable_cushion_bps": cushion_bps,
        "max_limit_slippage_bps": max_slippage_bps,
        "entry_order_type": "LMT",
        "entry_limit_intent": "bounded_marketable",
        "entry_price_plan": "tv_reference_then_runtime_bounded_limit",
    }
    return LinkageChain(
        symbol=symbol,
        direction=direction,
        signal_id=f"{unique}_entry",
        trade_group_id=f"{unique}_grp",
        position_id=f"{unique}_pos",
        quantity=quantity,
        entry=entry,
        stop_loss=stop,
        take_profit=take,
        conid=safe_int(quote.get("conid") or quote.get("conidEx"), 0),
        quote=quote_snapshot,
        price_plan=price_plan,
    )


def stamp_payload(payload: dict[str, Any], args: argparse.Namespace, *, event_time: datetime | None = None) -> None:
    # Use a just-closed bar so runtime freshness checks do not see a future Pine timestamp.
    event_time = (event_time or (datetime.now(ET) - timedelta(seconds=1))).astimezone(ET).replace(microsecond=0)
    event_ms = int(event_time.timestamp() * 1000)
    interval_ms = 120000
    payload.update(
        {
            "market_date": event_time.strftime("%Y-%m-%d"),
            "date": event_time.strftime("%Y-%m-%d"),
            "us_time": event_time.strftime("%Y-%m-%d %H:%M:%S"),
            "cn_time": event_time.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
            "bar_time_ms": event_ms - interval_ms,
            "bar_close_ms": event_ms,
            "pine_eval_ms": event_ms,
            "data_environment": args.data_environment,
            "market_data_mode": args.data_environment,
            "environment": args.data_environment,
            "broker_mode": args.broker_mode,
        }
    )


def validation_fields(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "validation_mode": "premarket_linkage",
        "validation_run_id": args.run_id,
        "validation_token": args.validation_token,
        "paper_only": True,
        "premarket_validation": True,
        "outside_rth": True,
    }


def base_tv_payload(args: argparse.Namespace, chain: LinkageChain) -> dict[str, Any]:
    return {
        "source": "tv",
        "symbol": chain.symbol,
        "exchange": "SMART",
        "interval": "2",
        "chart_tf": "2",
        "timeframe_stack": "chart=2",
        "script_tag": "Signal_Strategy_Core[Glory]",
        "strategy_version": "SSC_v1_premarket_validation",
        "activity_score": 100,
        "quality_score": 100,
        "qualified": True,
        "position_id": chain.position_id,
        "trade_group_id": chain.trade_group_id,
        "bracket_group": chain.trade_group_id,
        **validation_fields(args),
        "extra": {
            "source": "tradingview",
            "validation_tag": "tv_premarket_linkage",
            "validation_run_id": args.run_id,
            "validation_mode": "premarket_linkage",
            "paper_only": True,
            "outside_rth": True,
            "tv_entry_reference_price": chain.entry,
            "tv_reference_entry": chain.entry,
            "reference_entry": chain.entry,
            "tv_reference_stop_loss": chain.stop_loss,
            "reference_stop_loss": chain.stop_loss,
            "tv_reference_take_profit": chain.take_profit,
            "reference_take_profit": chain.take_profit,
            "final_protection_from_fill": True,
            "reference_protection_only": True,
            "submitted_limit_cap_bps": chain.price_plan.get("submitted_limit_cap_bps", 0.0),
            "submitted_limit_cap_price": chain.price_plan.get("submitted_limit_preview", 0.0),
            "submitted_limit_price": chain.price_plan.get("submitted_limit_preview", 0.0),
            "entry_limit_cap_price": chain.price_plan.get("submitted_limit_preview", 0.0),
            "submitted_limit_cap_applied": True,
            "entry_order_type": "LMT",
            "entry_limit_intent": "bounded_marketable",
            "entry_price_plan": chain.price_plan.get("entry_price_plan", "tv_reference_then_runtime_bounded_limit"),
            "premarket_validation_quote": chain.quote,
            "premarket_validation_price_plan": chain.price_plan,
        },
    }


def pre_alert_payload(args: argparse.Namespace, chain: LinkageChain) -> dict[str, Any]:
    payload = {
        **base_tv_payload(args, chain),
        "event_type": "pre_alert",
        "event_id": f"{chain.signal_id}_pre",
        "direction_bias": chain.direction,
        "reason": "paper premarket linkage pre-alert validation",
    }
    stamp_payload(payload, args)
    return payload


def entry_payload(args: argparse.Namespace, chain: LinkageChain) -> dict[str, Any]:
    payload = {
        **base_tv_payload(args, chain),
        "event_type": "entry",
        "event_id": chain.signal_id,
        "signal_id": chain.signal_id,
        "direction": chain.direction,
        "position_side": chain.direction,
        "entry_setup": "paper_premarket_linkage_entry",
        "signal": "paper_premarket_linkage_entry",
        "entry": chain.entry,
        "limit_price": chain.entry,
        "stop_loss": chain.stop_loss,
        "take_profit": chain.take_profit,
        "shares": chain.quantity,
        "quantity": chain.quantity,
        "rr": f"{float(args.reward_r):.2f}:1",
        "risk_r": abs(chain.entry - chain.stop_loss),
        "reason": "paper premarket linkage entry validation",
    }
    if chain.conid > 0:
        payload["conid"] = chain.conid
    stamp_payload(payload, args)
    return payload


def risk_payload(args: argparse.Namespace, chain: LinkageChain, orders: list[dict[str, Any]], sequence: int) -> dict[str, Any]:
    roles = role_map(orders)
    entry_order = roles.get("entry", {})
    tp_order = roles.get("take_profit", {})
    sl_order = roles.get("stop_loss", {})
    entry = order_price(entry_order, "fill_price", "limit_price", "entry") or chain.entry
    old_sl = order_price(sl_order, "limit_price", "sl_price", "stop_loss") or chain.stop_loss
    old_tp = order_price(tp_order, "limit_price", "tp_price", "take_profit") or chain.take_profit
    risk = max(0.05, abs(entry - old_sl))
    if chain.direction == "long":
        new_sl = round_price(min(entry - 0.01, old_sl + risk * 0.25))
        new_tp = round_price(old_tp + risk * 0.10)
    else:
        new_sl = round_price(max(entry + 0.01, old_sl - risk * 0.25))
        new_tp = round_price(max(0.01, old_tp - risk * 0.10))
    payload = {
        **base_tv_payload(args, chain),
        "event_type": "risk_update",
        "event_id": f"{chain.signal_id}_risk_{sequence}",
        "signal_id": chain.signal_id,
        "origin_signal_id": chain.signal_id,
        "direction": chain.direction,
        "position_side": chain.direction,
        "risk_update_seq": sequence,
        "risk_update_reason": "premarket_linkage_tv_dynamic_adjust",
        "update_reason": "premarket_linkage_tv_dynamic_adjust",
        "requested_sides": ["stop_loss", "take_profit"],
        "entry_price": entry,
        "previous_stop_loss": old_sl,
        "previous_take_profit": old_tp,
        "new_stop_loss": new_sl,
        "new_take_profit": new_tp,
        "quantity": int(entry_order.get("quantity") or chain.quantity),
        "reason": "paper premarket linkage risk update validation",
    }
    stamp_payload(payload, args)
    return payload


def exit_payload(args: argparse.Namespace, chain: LinkageChain, orders: list[dict[str, Any]]) -> dict[str, Any]:
    roles = role_map(orders)
    entry_order = roles.get("entry", {})
    tp_order = roles.get("take_profit", {})
    sl_order = roles.get("stop_loss", {})
    payload = {
        **base_tv_payload(args, chain),
        "event_type": "exit",
        "event_id": f"{chain.signal_id}_exit_cleanup",
        "signal_id": chain.signal_id,
        "origin_signal_id": chain.signal_id,
        "direction": chain.direction,
        "position_side": chain.direction,
        "exit_reason": "premarket_linkage_cleanup",
        "entry_price": order_price(entry_order, "fill_price", "limit_price", "entry") or chain.entry,
        "stop_loss": order_price(sl_order, "limit_price", "sl_price", "stop_loss") or chain.stop_loss,
        "take_profit": order_price(tp_order, "limit_price", "tp_price", "take_profit") or chain.take_profit,
        "quantity": int(entry_order.get("quantity") or chain.quantity),
        "reason": "paper premarket linkage exit cleanup",
    }
    stamp_payload(payload, args)
    return payload


def http_ok(response: dict[str, Any]) -> bool:
    return int(response.get("_http_status") or 0) < 400 and bool(response.get("ok") or response.get("success"))


def add_check(chain: LinkageChain, name: str, ok: bool, **data: Any) -> None:
    chain.checks.append({"name": name, "ok": bool(ok), **data})


def order_is_entry_filled(row: dict[str, Any]) -> bool:
    status = safe_text(row.get("status")).lower()
    return status in {"filled", "executed", "closed"} or safe_float(row.get("filled_qty")) > 0 or safe_float(row.get("actual_filled_qty")) > 0


def order_broker_ids(orders: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for row in orders or []:
        for key in ("order_id", "broker_order_id"):
            value = safe_text(row.get(key))
            if value and value not in ids:
                ids.append(value)
    return ids


def query_execution_fills(args: argparse.Namespace, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = order_broker_ids(orders)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return remote_sql(
        args.host,
        args.db_path,
        f"""
        select *
        from ibkr_execution_fills
        where environment = ? and order_id in ({placeholders})
        order by trade_time_ms asc, created asc, order_id asc
        limit 100
        """,
        [args.broker_mode, *ids],
    )


def fill_evidence(orders: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    entry_rows = [row for row in orders or [] if safe_text(row.get("role")).lower() == "entry"]
    filled_entries = [row for row in entry_rows if order_is_entry_filled(row)]
    return {
        "ok": bool(filled_entries or fills),
        "entry_order_filled": bool(filled_entries),
        "execution_fill_count": len(fills or []),
        "filled_entry_orders": filled_entries,
        "execution_fills": fills or [],
    }


def orders_have_bracket(orders: list[dict[str, Any]]) -> bool:
    return {"entry", "take_profit", "stop_loss"}.issubset(set(role_map(orders)))


def wait_for_entry_fill(args: argparse.Namespace, chain: LinkageChain) -> list[dict[str, Any]]:
    def state() -> dict[str, Any]:
        orders = query_orders(args, chain.signal_id)
        fills = query_execution_fills(args, orders)
        return {"orders": orders, "fills": fills, "evidence": fill_evidence(orders, fills)}

    state_payload = wait_for(
        f"entry_fill:{chain.signal_id}",
        state,
        lambda payload: bool((payload or {}).get("evidence", {}).get("ok")),
        timeout_s=args.fill_timeout_seconds,
        interval_s=args.poll_interval,
    )
    chain.fills = [dict(row) for row in state_payload.get("fills") or []]
    return [dict(row) for row in state_payload.get("orders") or []]


def wait_for_bracket(args: argparse.Namespace, chain: LinkageChain) -> list[dict[str, Any]]:
    orders = wait_for(
        f"bracket_orders:{chain.signal_id}",
        lambda: query_orders(args, chain.signal_id),
        orders_have_bracket,
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    return [dict(row) for row in orders]


def wait_for_adjust_confirmed(args: argparse.Namespace, chain: LinkageChain) -> dict[str, Any]:
    rows = wait_for(
        f"adjust_bracket:{chain.signal_id}",
        lambda: query_reverses(args, chain.signal_id, "adjust_bracket"),
        lambda items: any(
            validate_adjust_reverse(row)[0]
            or safe_text(row.get("reason")).lower() == "risk_update_seq_stale"
            for row in items or []
        ),
        timeout_s=args.poll_seconds,
        interval_s=args.poll_interval,
    )
    for row in rows:
        if validate_adjust_reverse(row)[0] or safe_text(row.get("reason")).lower() == "risk_update_seq_stale":
            return dict(row)
    return dict(rows[0]) if rows else {}


def prices_match_after_adjust(orders: list[dict[str, Any]], risk: dict[str, Any], tolerance: float = 0.011) -> bool:
    roles = role_map(orders)
    sl = order_price(roles.get("stop_loss", {}), "limit_price", "sl_price", "stop_loss")
    tp = order_price(roles.get("take_profit", {}), "limit_price", "tp_price", "take_profit")
    return abs(float(sl or 0.0) - float(risk.get("new_stop_loss") or 0.0)) <= tolerance and abs(
        float(tp or 0.0) - float(risk.get("new_take_profit") or 0.0)
    ) <= tolerance


def reverse_prices_match_after_adjust(reverse: dict[str, Any], risk: dict[str, Any], tolerance: float = 0.011) -> dict[str, Any]:
    ok, detail, extra = validate_adjust_reverse(reverse)
    results = extra.get("adjust_results") if isinstance(extra.get("adjust_results"), dict) else {}
    expected = {
        "stop_loss": float(risk.get("new_stop_loss") or 0.0),
        "take_profit": float(risk.get("new_take_profit") or 0.0),
    }
    observed: dict[str, float] = {}
    confirmations: dict[str, Any] = {}
    for side, expected_price in expected.items():
        side_detail = results.get(side) if isinstance(results.get(side), dict) else {}
        confirmation = side_detail.get("confirmation") if isinstance(side_detail.get("confirmation"), dict) else {}
        modify_result = side_detail.get("result") if isinstance(side_detail.get("result"), dict) else {}
        observed_price = (
            safe_float(side_detail.get("new_price"), 0.0)
            or safe_float(confirmation.get("expected_price"), 0.0)
            or safe_float(confirmation.get("target_price"), 0.0)
            or safe_float(modify_result.get("price"), 0.0)
        )
        observed[side] = observed_price
        confirmations[side] = confirmation
    prices_ok = all(abs(observed[side] - price) <= tolerance for side, price in expected.items() if price > 0)
    return {
        "ok": bool(ok and prices_ok),
        "detail": detail,
        "expected": expected,
        "observed": observed,
        "confirmations": confirmations,
        "adjust_bracket_result": extra.get("adjust_bracket_result"),
        "adjust_results": results,
    }


def order_is_open(row: dict[str, Any]) -> bool:
    status = safe_text(row.get("status")).lower()
    if row.get("is_open") is False:
        return False
    return status not in TERMINAL_ORDER_STATUSES


def resolve_order_trade_group_id(orders: list[dict[str, Any]], chain: LinkageChain) -> str:
    roles = role_map(orders)
    for row in [roles.get("entry"), roles.get("take_profit"), roles.get("stop_loss"), *orders]:
        if not isinstance(row, dict) or not row:
            continue
        for key in ("trade_group_id", "bracket_group", "group_id", "parent_group_id", "unique_id"):
            value = safe_text(row.get(key))
            if value:
                return value
        extra = as_object(row.get("extra"))
        for key in ("trade_group_id", "bracket_group", "group_id", "parent_group_id"):
            value = safe_text(extra.get(key))
            if value:
                return value
    return chain.trade_group_id


def cleanup_chain(args: argparse.Namespace, chain: LinkageChain) -> dict[str, Any]:
    orders = query_orders(args, chain.signal_id)
    chain.orders = [dict(row) for row in orders]
    open_orders = [row for row in orders if order_is_open(row)]
    if not orders or not open_orders:
        return {"ok": True, "skipped": True, "reason": "no_orders"}
    entry_filled = any(order_is_entry_filled(row) for row in orders if safe_text(row.get("role")).lower() == "entry")
    trade_group_id = resolve_order_trade_group_id(orders, chain)
    if entry_filled:
        payload = exit_payload(args, chain, orders)
        response = post_json(args.base_url, "/webhook/tv", payload, timeout=args.http_timeout)
        chain.events.append({"event_type": "exit", "response": response})
        if not http_ok(response):
            return {"ok": False, "action": "exit_cleanup", "error": "exit_webhook_failed", "response": response}
        try:
            rows = wait_for(
                f"close_reverse:{chain.signal_id}",
                lambda: query_reverses(args, chain.signal_id, "close"),
                lambda items: any(safe_text(row.get("status")).lower() in {"confirmed", "closed"} for row in items or []),
                timeout_s=min(float(args.cleanup_timeout_seconds), float(args.poll_seconds)),
                interval_s=args.poll_interval,
            )
            return {"ok": True, "action": "exit_cleanup", "close_reverse": dict(rows[0]) if rows else {}, "trade_group_id": trade_group_id}
        except Exception as exc:
            return {"ok": False, "action": "exit_cleanup", "error": str(exc), "response": response, "trade_group_id": trade_group_id}

    target_id = safe_text((role_map(orders).get("entry") or {}).get("unique_id")) or trade_group_id
    response = post_json(
        args.base_url,
        "/api/custom/ibkr/orders/cancel_group",
        {
            "id": target_id,
            "trade_group_id": trade_group_id,
            "signal_id": chain.signal_id,
            "broker_mode": args.broker_mode,
            "environment": args.broker_mode,
            "market_data_mode": args.data_environment,
            "data_environment": args.data_environment,
            "source": "tv_premarket_linkage_cleanup",
            "reason": "paper premarket linkage unfilled bracket cleanup",
        },
        timeout=args.http_timeout,
    )
    chain.events.append({"event_type": "cancel_group", "response": response})
    ok = int(response.get("_http_status") or 0) < 400 and bool(response.get("ok") or response.get("accepted"))
    return {
        "ok": ok,
        "action": "cancel_group",
        "response": response,
        "trade_group_id": trade_group_id,
        "target_id": target_id,
    }


def execute_chain(args: argparse.Namespace, chain: LinkageChain, sequence: int) -> LinkageChain:
    started = time.perf_counter()
    try:
        if args.dry_run:
            add_check(chain, "dry_run_plan", True, payload=entry_payload(args, chain))
            return chain
        for name, payload in (("pre_alert", pre_alert_payload(args, chain)), ("entry", entry_payload(args, chain))):
            sent_at = time.perf_counter()
            response = post_json(args.base_url, "/webhook/tv", payload, timeout=args.http_timeout)
            elapsed = time.perf_counter() - sent_at
            chain.events.append({"event_type": name, "payload": payload, "response": response, "elapsed_s": round(elapsed, 3)})
            add_check(chain, f"tv_{name}_routed", http_ok(response), response=response, elapsed_s=round(elapsed, 3))
            if not http_ok(response):
                raise ValidationError(f"tv_{name}_failed:{compact_json(response)[:600]}")

        signal = wait_for(
            f"signal:{chain.signal_id}",
            lambda: query_signal(args, chain.signal_id),
            lambda row: bool(row.get("id")),
            timeout_s=args.poll_seconds,
            interval_s=args.poll_interval,
        )
        rejection = signal_rejection_reason(signal, args.broker_mode)
        add_check(chain, "signal_persisted", bool(signal.get("id")) and not rejection, signal=signal, rejection=rejection)
        if rejection:
            raise ValidationError(f"signal_rejected:{rejection}")

        chain.orders = wait_for_bracket(args, chain)
        add_check(chain, "bracket_orders", True, orders=chain.orders)

        chain.orders = wait_for_entry_fill(args, chain)
        evidence = fill_evidence(chain.orders, chain.fills)
        add_check(chain, "entry_filled", bool(evidence.get("ok")), **evidence)
        if not evidence.get("ok"):
            raise ValidationError("entry_fill_not_observed")

        risk = risk_payload(args, chain, chain.orders, sequence=1)
        risk_started = time.perf_counter()
        risk_response = post_json(args.base_url, "/webhook/tv", risk, timeout=args.http_timeout)
        add_check(
            chain,
            "tv_risk_update_routed",
            http_ok(risk_response),
            response=risk_response,
            elapsed_s=round(time.perf_counter() - risk_started, 3),
        )
        chain.events.append({"event_type": "risk_update", "payload": risk, "response": risk_response})
        if not http_ok(risk_response):
            raise ValidationError(f"risk_update_failed:{compact_json(risk_response)[:600]}")

        reverse = wait_for_adjust_confirmed(args, chain)
        chain.reverses.append(reverse)
        reverse_price_check = reverse_prices_match_after_adjust(reverse, risk)
        add_check(chain, "adjust_bracket_confirmed", bool(reverse) and bool(reverse_price_check.get("ok")), reverse=reverse, validation=reverse_price_check)
        if not reverse_price_check.get("ok"):
            raise ValidationError(f"adjust_bracket_price_confirmation_failed:{compact_json(reverse_price_check)[:600]}")

        try:
            adjusted_orders = wait_for(
                f"adjusted_order_prices:{chain.signal_id}",
                lambda: query_orders(args, chain.signal_id),
                lambda rows: prices_match_after_adjust(rows, risk),
                timeout_s=min(float(args.poll_seconds), 60.0),
                interval_s=args.poll_interval,
            )
            chain.orders = [dict(row) for row in adjusted_orders]
            add_check(chain, "order_rows_tp_sl_prices_adjusted", True, expected={"sl": risk["new_stop_loss"], "tp": risk["new_take_profit"]}, orders=chain.orders)
        except Exception as exc:
            chain.orders = query_orders(args, chain.signal_id)
            add_check(
                chain,
                "order_rows_tp_sl_prices_adjusted",
                False,
                non_blocking=True,
                error=str(exc),
                expected={"sl": risk["new_stop_loss"], "tp": risk["new_take_profit"]},
                orders=chain.orders,
            )

        cleanup = cleanup_chain(args, chain)
        add_check(chain, "exit_cleanup", bool(cleanup.get("ok")), cleanup=cleanup)
        if not cleanup.get("ok"):
            raise ValidationError(f"cleanup_failed:{compact_json(cleanup)[:600]}")

        chain.completed = True
        add_check(chain, "chain_complete", True, elapsed_s=round(time.perf_counter() - started, 3))
    except Exception as exc:
        chain.failed_reason = str(exc)
        add_check(chain, "chain_failed", False, error=str(exc), elapsed_s=round(time.perf_counter() - started, 3))
        try:
            cleanup = cleanup_chain(args, chain)
            add_check(chain, "rescue_cleanup", bool(cleanup.get("ok")), cleanup=cleanup)
        except Exception as cleanup_exc:
            add_check(chain, "rescue_cleanup", False, error=str(cleanup_exc))
    return chain


def run_negative_premarket_check(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    symbol = split_symbols(args.symbols)[0]
    stamp = int(time.time())
    payload = {
        "source": "tv",
        "event_type": "entry",
        "event_id": f"{args.run_id}_negative_entry_{stamp}",
        "signal_id": f"{args.run_id}_negative_entry_{stamp}",
        "symbol": symbol,
        "direction": "long",
        "position_side": "long",
        "entry": 100.0,
        "limit_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "shares": 1,
        "activity_score": 100,
        "quality_score": 100,
        "qualified": True,
        "broker_mode": "paper",
        "market_data_mode": args.data_environment,
        "data_environment": args.data_environment,
        "environment": args.data_environment,
    }
    event_time = datetime.now(ET).replace(hour=4, minute=10, second=0, microsecond=0)
    stamp_payload(payload, args, event_time=event_time)
    response = post_json(args.base_url, "/webhook/tv", payload, timeout=args.http_timeout)
    rejected = bool(response.get("rejected")) and response.get("reason") in {"outside_tv_entry_window", "premarket_validation_not_authorized"}
    return {"ok": rejected, "response": response}


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    if args.execute and args.confirm != CONFIRM_TEXT:
        raise ValidationError(f"--confirm must be {CONFIRM_TEXT}")
    if not args.execute and not args.dry_run:
        raise ValidationError("refusing_to_submit_without_execute_or_dry_run")
    if args.broker_mode != "paper":
        raise ValidationError("broker_mode_must_be_paper")
    if not args.validation_token:
        args.validation_token = secrets.token_urlsafe(24)
    if not int(args.expires_at_ms or 0):
        stop = parse_hhmm(args.stop_new_entry_et, "09:20")
        today = datetime.now(ET)
        expires = today.replace(hour=stop.hour, minute=stop.minute, second=0, microsecond=0) + timedelta(minutes=10)
        args.expires_at_ms = int(expires.timestamp() * 1000)

    summary: dict[str, Any] = {
        "ok": False,
        "run_id": args.run_id,
        "started_at_et": datetime.now(ET).isoformat(),
        "started_at_cn": datetime.now(CN).isoformat(),
        "target_completed_chains": int(args.target_completed_chains),
        "broker_mode": args.broker_mode,
        "data_environment": args.data_environment,
        "base_url": args.base_url,
        "artifact_dir": str(artifact_dir(args)),
        "chains": [],
        "failed_chains": [],
        "config": {
            "validation_run_id": args.run_id,
            "expires_at_ms": int(args.expires_at_ms or 0),
            "target_notional_per_chain": float(args.target_notional_per_chain),
        },
        "premarket_trading_allowed": False,
    }

    if args.wait_until_start:
        wait_result = wait_until_start(args)
        summary["wait_until_start"] = wait_result
        if not wait_result.get("ok"):
            write_artifact(args, "summary.json", summary)
            return summary

    account_before = check_account_clean(args)
    summary["account_before"] = account_before
    if not account_before.get("ok") and not args.allow_existing_paper_state:
        summary["reason"] = "account_not_flat_before_validation"
        write_artifact(args, "summary.json", summary)
        return summary

    health_before: dict[str, Any] = {}
    stability_before: dict[str, Any] = {"ok": True, "phase": "before", "skipped": True, "reason": "skip_health"}
    if not args.skip_health:
        health_before = phase0_health(args)
        stability_before = evaluate_stability(args, health_before, "before")
        summary["health"] = {"before": health_before}
        summary["stability"] = {"before": stability_before}
        write_artifact(args, "progress.json", summary)
        if not stability_before.get("ok"):
            summary["reason"] = "stability_precheck_failed"
            write_artifact(args, "summary.json", summary)
            return summary

    symbols, symbol_discovery = resolve_validation_symbols(args)
    summary["symbol_discovery"] = symbol_discovery
    if not symbols:
        raise ValidationError("symbols_empty")
    completed: list[LinkageChain] = []
    failed: list[LinkageChain] = []
    attempts = 0
    cursor = 0
    whitelist_enable: dict[str, Any] = {}

    try:
        whitelist_enable = enable_validation_whitelist(args)
        summary["whitelist_enable"] = whitelist_enable
        while len(completed) < int(args.target_completed_chains):
            if not args.dry_run and not within_new_entry_window(args):
                summary["reason"] = "stop_new_entry_window_reached"
                break
            if int(args.max_attempts or 0) > 0 and attempts >= int(args.max_attempts):
                summary["reason"] = "max_attempts_reached"
                break
            batch = [symbols[(cursor + offset) % len(symbols)] for offset in range(min(len(symbols), int(args.quote_batch_size)))]
            cursor = (cursor + len(batch)) % len(symbols)
            quotes = fetch_quotes(args, batch)
            selected = None
            for symbol in batch:
                attempts += 1
                quote = quotes.get(symbol)
                if not isinstance(quote, dict) or not quote:
                    continue
                selected = build_chain_from_quote(args, symbol, quote, len(completed) + len(failed) + 1)
                if selected:
                    break
            if not selected:
                time.sleep(max(0.0, float(args.no_candidate_sleep_seconds or 1.0)))
                continue
            chain = execute_chain(args, selected, sequence=len(completed) + 1)
            if chain.completed:
                completed.append(chain)
            else:
                failed.append(chain)
            summary["chains"] = [chain.summary() for chain in completed]
            summary["failed_chains"] = [chain.summary() for chain in failed[-20:]]
            summary["completed_count"] = len(completed)
            summary["attempts"] = attempts
            write_artifact(args, "progress.json", summary)
            if args.dry_run:
                break
            time.sleep(max(0.0, float(args.chain_spacing_seconds or 0.0)))
    finally:
        summary["whitelist_disable"] = disable_validation_whitelist(args)

    summary["negative_premarket_entry_check"] = run_negative_premarket_check(args)
    account_after = check_account_clean(args)
    summary["account_after"] = account_after
    health_after: dict[str, Any] = {}
    stability_after: dict[str, Any] = {"ok": True, "phase": "after", "skipped": True, "reason": "skip_health"}
    if not args.skip_health:
        health_after = phase0_health(args)
        stability_after = evaluate_stability(args, health_after, "after")
    summary["health"] = {"before": health_before, "after": health_after}
    summary["stability"] = {"before": stability_before, "after": stability_after}
    summary["completed_count"] = len(completed)
    summary["attempts"] = attempts
    summary["finished_at_et"] = datetime.now(ET).isoformat()
    summary["finished_at_cn"] = datetime.now(CN).isoformat()
    summary["ok"] = (
        len(completed) >= int(args.target_completed_chains)
        and bool(account_after.get("ok"))
        and bool(summary["negative_premarket_entry_check"].get("ok"))
        and bool(stability_after.get("ok"))
    )
    summary["premarket_validation_status"] = "GO" if summary["ok"] else "NO-GO"
    summary["premarket_trading_allowed"] = False
    write_artifact(args, "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-only premarket TV -> IBKR linkage validation.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--account-snapshot-path", default=DEFAULT_ACCOUNT_SNAPSHOT_PATH)
    parser.add_argument("--run-id", default=default_run_id())
    parser.add_argument("--validation-token", default=os.environ.get("TV_PREMARKET_VALIDATION_TOKEN", ""))
    parser.add_argument("--broker-mode", default="paper", choices=["paper"])
    parser.add_argument("--data-environment", default="live", choices=["live", "paper"])
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--exclude-symbols", default=DEFAULT_EXCLUDE_SYMBOLS)
    parser.add_argument("--target-completed-chains", type=int, default=20)
    parser.add_argument("--target-notional-per-chain", type=float, default=5000.0)
    parser.add_argument("--direction", default="long", choices=["long", "short"])
    parser.add_argument("--marketable-bps", type=float, default=2.0)
    parser.add_argument("--max-limit-slippage-bps", type=float, default=15.0)
    parser.add_argument("--max-spread-bps", type=float, default=80.0)
    parser.add_argument("--max-quote-age-sec", type=float, default=600.0)
    parser.add_argument("--protection-gap-pct", type=float, default=0.01)
    parser.add_argument("--reward-r", type=float, default=1.5)
    parser.add_argument("--quote-batch-size", type=int, default=40)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--start-et", default="04:05")
    parser.add_argument("--stop-new-entry-et", default="09:20")
    parser.add_argument("--wait-until-start", action="store_true")
    parser.add_argument("--expires-at-ms", type=int, default=0)
    parser.add_argument("--poll-seconds", type=float, default=180.0)
    parser.add_argument("--fill-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--cleanup-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--retry-interval-seconds", type=float, default=30.0)
    parser.add_argument("--no-candidate-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--chain-spacing-seconds", type=float, default=1.0)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--stability-lookback-minutes", type=float, default=5.0)
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-stability-gate", action="store_true")
    parser.add_argument("--allow-existing-paper-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = run_validation(args)
        if args.format == "json":
            print(json.dumps(redact_sensitive(payload), ensure_ascii=False, indent=2, default=str))
        else:
            print(
                "run_id={run_id} status={status} completed={completed}/{target} ok={ok} artifact_dir={artifact_dir}".format(
                    run_id=payload.get("run_id"),
                    status=payload.get("premarket_validation_status", "NO-GO"),
                    completed=payload.get("completed_count", 0),
                    target=payload.get("target_completed_chains", 0),
                    ok=payload.get("ok"),
                    artifact_dir=payload.get("artifact_dir"),
                )
            )
        return 0 if payload.get("ok") else 1
    except Exception as exc:
        error_payload = {
            "ok": False,
            "premarket_validation_status": "NO-GO",
            "premarket_trading_allowed": False,
            "error": str(exc),
            "run_id": getattr(args, "run_id", ""),
        }
        try:
            write_artifact(args, "summary.json", error_payload)
        except Exception:
            pass
        print(json.dumps(error_payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
