#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any
import urllib.request
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_paper_commission_probe as fee_probe  # noqa: E402


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

DEFAULT_ARTIFACT_ROOT = Path("artifacts/validation/gateway_order_probe")
DEFAULT_PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
CONFIRM_TEXT = "PAPER_GATEWAY_ORDER_PROBE"
REFERENCE_PRICE_INTERVAL = "5m"
DEFAULT_SYMBOLS = (
    "TSLA,AAPL,MSFT,NVDA,AMD,META,GOOGL,AMZN,NFLX,ORCL,"
    "CRM,ADBE,INTC,CSCO,QCOM,AVGO,TXN,MU,IBM,NOW,SHOP,SNOW,PLTR,UBER,DDOG,NET,"
    "PANW,CRWD,ZS,TEAM,WDAY,INTU,ADP,PYPL,SQ,COIN,HOOD,ABNB,BKNG,MELI,SE,SPOT,"
    "ROKU,TWLO,OKTA,DELL,HPQ,SMCI,MRVL,LRCX,KLAC,AMAT,ASML,TSM,"
    "JPM,BAC,WFC,GS,MS,V,MA,AXP,DIS,CMCSA,PEP,KO,MCD,SBUX,COST,WMT,TGT,HD,LOW,"
    "NKE,BA,CAT,GE,GM,F,FDX,UPS,UNH,JNJ,PFE,MRK,ABBV,LLY,TMO,ISRG,VRTX"
)


class GatewayProbeError(RuntimeError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass(frozen=True)
class OrderProbePlan:
    symbol: str
    direction: str
    quantity: int
    reference_price: float
    entry_price: float
    take_profit_price: float
    stop_loss_price: float
    target_notional: float = 0.0
    requested_exposure: float = 0.0
    conid: int = 0

    def payload(self) -> dict[str, Any]:
        payload = {
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "order_type": "LMT",
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
        }
        if int(self.conid or 0) > 0:
            payload["conid"] = int(self.conid)
        return payload

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "conid": int(self.conid or 0),
            "direction": self.direction,
            "quantity": self.quantity,
            "reference_price": self.reference_price,
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "target_notional": self.target_notional,
            "requested_exposure": self.requested_exposure,
        }


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def split_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    for piece in str(raw or "").replace(";", ",").split(","):
        symbol = fee_probe.normalize_symbol(piece)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def round_price(value: float) -> float:
    return max(0.01, round(float(value or 0.0), 2))


def build_non_marketable_bracket(
    reference_price: float,
    *,
    direction: str = "long",
    entry_distance_pct: float = 0.50,
    protection_gap_pct: float = 0.15,
) -> tuple[float, float, float]:
    price = float(reference_price or 0.0)
    if price <= 0:
        raise GatewayProbeError("reference_price_required")
    distance = min(0.95, max(0.05, float(entry_distance_pct or 0.50)))
    gap = min(0.90, max(0.01, float(protection_gap_pct or 0.15)))
    normalized_direction = str(direction or "long").strip().lower()
    if normalized_direction == "long":
        entry = round_price(price * (1.0 - distance))
        stop_loss = round_price(entry * (1.0 - gap))
        take_profit = round_price(entry * (1.0 + gap))
        if not (0 < stop_loss < entry < take_profit):
            raise GatewayProbeError("invalid_long_probe_prices")
        return entry, take_profit, stop_loss
    if normalized_direction == "short":
        entry = round_price(price * (1.0 + distance))
        take_profit = round_price(entry * (1.0 - gap))
        stop_loss = round_price(entry * (1.0 + gap))
        if not (0 < take_profit < entry < stop_loss):
            raise GatewayProbeError("invalid_short_probe_prices")
        return entry, take_profit, stop_loss
    raise GatewayProbeError("direction must be long or short")


def quantity_for_target_notional(entry_price: float, target_notional: float, fallback_quantity: int) -> tuple[int, float]:
    target = float(target_notional or 0.0)
    if target <= 0:
        quantity = max(1, int(fallback_quantity or 1))
    else:
        if entry_price <= 0:
            raise GatewayProbeError("entry_price_required_for_target_notional")
        quantity = max(1, int(ceil(target / float(entry_price))))
    exposure = round(float(entry_price) * quantity, 4)
    return quantity, exposure


def _extract_conid_from_bar_row(row: dict[str, Any] | None) -> int:
    if not isinstance(row, dict):
        return 0
    for key in ("conid", "conidEx"):
        try:
            conid = int(float(row.get(key) or 0))
        except (TypeError, ValueError):
            conid = 0
        if conid > 0:
            return conid
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict):
        for key in ("conid", "conidEx"):
            try:
                conid = int(float(extra.get(key) or 0))
            except (TypeError, ValueError):
                conid = 0
            if conid > 0:
                return conid
    return 0


def fetch_latest_reference_context(args: argparse.Namespace, symbol: str) -> tuple[float, int]:
    normalized = fee_probe.normalize_symbol(symbol)
    price_override = float(getattr(args, "reference_price", 0.0) or 0.0)
    rows: list[dict[str, Any]] = []
    try:
        safe_symbol = fee_probe.sqlite_literal(normalized)
        rows = fee_probe.run_remote_sql(
            args,
            f"""
            select close, environment, interval, us_time, bar_time_ms, extra
            from ibkr_bars
            where symbol = {safe_symbol}
              and close > 0
              and interval = {fee_probe.sqlite_literal(REFERENCE_PRICE_INTERVAL)}
              and environment in ('live', 'paper')
            order by bar_time_ms desc
            limit 1;
            """,
        )
    except Exception:
        rows = []
    row = rows[0] if rows else {}
    if price_override > 0:
        price = price_override
    else:
        price = fee_probe.to_float(row.get("close"), 0.0) if row else 0.0
        if price <= 0:
            # Keep the historical behavior and error message for callers/tests that mock this helper.
            price = fee_probe.fetch_latest_reference_price(args, normalized)
    conid = _extract_conid_from_bar_row(row)
    return float(price), int(conid or 0)


def fetch_latest_reference_contexts(args: argparse.Namespace, symbols: list[str]) -> dict[str, tuple[float, int]] | None:
    normalized_symbols = [fee_probe.normalize_symbol(symbol) for symbol in symbols if fee_probe.normalize_symbol(symbol)]
    if not normalized_symbols:
        return {}
    price_override = float(getattr(args, "reference_price", 0.0) or 0.0)
    if price_override > 0:
        return {symbol: (price_override, 0) for symbol in normalized_symbols}
    requested_values = ", ".join(f"({fee_probe.sqlite_literal(symbol)})" for symbol in normalized_symbols)
    interval_literal = fee_probe.sqlite_literal(REFERENCE_PRICE_INTERVAL)
    rows: list[dict[str, Any]] = []
    try:
        rows = fee_probe.run_remote_sql(
            args,
            f"""
            with requested(symbol) as (
              values {requested_values}
            ),
            latest as (
              select
                requested.symbol as symbol,
                (
                  select rowid
                  from ibkr_bars
                  where ibkr_bars.symbol = requested.symbol
                    and close > 0
                    and interval = {interval_literal}
                    and environment in ('live', 'paper')
                  order by bar_time_ms desc
                  limit 1
                ) as rowid
              from requested
            )
            select latest.symbol, ibkr_bars.close, ibkr_bars.environment, ibkr_bars.interval,
                   ibkr_bars.us_time, ibkr_bars.bar_time_ms, ibkr_bars.extra
            from latest
            join ibkr_bars on ibkr_bars.rowid = latest.rowid
            where latest.rowid is not null;
            """,
        )
    except Exception:
        return None

    contexts: dict[str, tuple[float, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = fee_probe.normalize_symbol(row.get("symbol"))
        price = fee_probe.to_float(row.get("close"), 0.0)
        if symbol and price > 0:
            contexts[symbol] = (float(price), int(_extract_conid_from_bar_row(row) or 0))
    return contexts


def fetch_latest_conid(args: argparse.Namespace, symbol: str) -> int:
    normalized = fee_probe.normalize_symbol(symbol)
    try:
        safe_symbol = fee_probe.sqlite_literal(normalized)
        rows = fee_probe.run_remote_sql(
            args,
            f"""
            select extra
            from ibkr_bars
            where symbol = {safe_symbol}
              and environment in ('live', 'paper')
            order by bar_time_ms desc
            limit 1;
            """,
        )
    except Exception:
        return 0
    return _extract_conid_from_bar_row(rows[0] if rows else {})


def artifact_dir(args: argparse.Namespace) -> Path:
    return Path(args.artifact_root) / args.run_id


def write_artifact(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    out_dir = artifact_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.json"
    tmp_path = out_dir / "summary.json.tmp"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp_path.replace(summary_path)
    return str(summary_path)


def configure_http_proxy(args: argparse.Namespace) -> None:
    if bool(getattr(args, "use_system_proxy", False)):
        return
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def action_params() -> dict[str, Any]:
    return {"environment": "paper", "broker_mode": "paper"}


def nested_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return dict(result) if isinstance(result, dict) else {}


def action_error_code(item: dict[str, Any] | None) -> str:
    item = item or {}
    response = item.get("response") if isinstance(item.get("response"), dict) else {}
    result = nested_result(response)
    for source in (item, response, result):
        for key in ("error", "reason", "status_reason"):
            text = fee_probe.to_text(source.get(key) if isinstance(source, dict) else "")
            if text:
                return text
    return ""


def response_action_ok(response: dict[str, Any]) -> bool:
    result = nested_result(response)
    return bool(response.get("ok") and result.get("ok", True) is not False)


def is_expected_buying_power_block(item: dict[str, Any] | None) -> bool:
    return action_error_code(item) == "buying_power_blocked"


def cleanup_http_timeout(args: argparse.Namespace) -> float:
    explicit = float(getattr(args, "cleanup_http_timeout_sec", 0.0) or 0.0)
    if explicit > 0:
        return explicit
    return max(5.0, min(float(getattr(args, "http_timeout_sec", 30.0) or 30.0), 60.0))


def cancel_all_http_timeout(args: argparse.Namespace) -> float:
    explicit = float(getattr(args, "cancel_all_http_timeout_sec", 0.0) or 0.0)
    if explicit > 0:
        return explicit
    http_timeout = float(getattr(args, "http_timeout_sec", 30.0) or 30.0)
    # Runtime cancel_all can reconcile open_orders_all for up to ~180s under
    # large bracket bursts, so it needs a larger client timeout than snapshots.
    return max(cleanup_http_timeout(args), min(http_timeout, 240.0))


def symbol_cleanup_timeout(args: argparse.Namespace) -> float:
    explicit = float(getattr(args, "symbol_cleanup_timeout_sec", 0.0) or 0.0)
    if explicit > 0:
        return explicit
    return max(5.0, min(float(getattr(args, "cleanup_timeout_sec", 60.0) or 60.0), 45.0))


def account_snapshot_client(args: argparse.Namespace, *, timeout_sec: float | None = None) -> tuple[fee_probe.ApiClient, str]:
    base_url = fee_probe.to_text(args.account_base_url) or args.api_base_url
    snapshot_args = argparse.Namespace(
        account_base_url=fee_probe.to_text(args.account_base_url),
        account_snapshot_path=fee_probe.to_text(args.account_snapshot_path),
    )
    timeout = float(timeout_sec if timeout_sec is not None else args.http_timeout_sec)
    return fee_probe.ApiClient(base_url, timeout=timeout), fee_probe.account_snapshot_path(snapshot_args)


def account_snapshot_params(*, orders_fast: bool = False) -> dict[str, Any]:
    params = dict(fee_probe.account_params())
    if orders_fast:
        params.update(
            {
                "include_pnl": "0",
                "orders_fast": "1",
                "snapshot_profile": "orders_fast",
                "open_orders_only": "1",
                "cache": "0",
            }
        )
    return params


def get_snapshot(
    args: argparse.Namespace,
    *,
    timeout_sec: float | None = None,
    orders_fast: bool = False,
) -> dict[str, Any]:
    client, path = account_snapshot_client(args, timeout_sec=timeout_sec)
    return client.get(path or fee_probe.DEFAULT_ACCOUNT_SNAPSHOT_PATH, account_snapshot_params(orders_fast=orders_fast))


def get_gateway_status_lite(args: argparse.Namespace, *, timeout_sec: float | None = None) -> dict[str, Any]:
    client, _path = account_snapshot_client(args, timeout_sec=timeout_sec)
    return client.get(
        "/ibkr/status",
        {
            "environment": "paper",
            "broker_mode": "paper",
            "lite": "1",
            "skip_compute_status": "1",
        },
    )


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def summarize_gateway_readiness(status: dict[str, Any]) -> dict[str, Any]:
    payload = status if isinstance(status, dict) else {}
    gateway = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
    broker = gateway.get("broker") if isinstance(gateway.get("broker"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    websocket = payload.get("websocket") if isinstance(payload.get("websocket"), dict) else {}
    topology = payload.get("service_topology") if isinstance(payload.get("service_topology"), dict) else {}
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    topology_gateway = services.get("ibkr-gateway") if isinstance(services.get("ibkr-gateway"), dict) else {}
    environment = fee_probe.to_text(payload.get("broker_mode") or payload.get("environment")).lower()
    broker_status_code = 0
    for source in (broker, gateway):
        try:
            broker_status_code = int(float(source.get("status_code") or 0))
        except Exception:
            broker_status_code = 0
        if broker_status_code:
            break
    api_socket_listening = _bool_or_none(gateway.get("api_socket_listening"))
    gateway_reachable = _bool_or_none(gateway.get("reachable"))
    broker_connected = _bool_or_none(broker.get("connected"))
    broker_ready = _bool_or_none(broker.get("ready"))
    session_authenticated = _bool_or_none(session.get("authenticated"))
    failures: list[dict[str, Any]] = []

    def add_failure(name: str, expected: Any, actual: Any) -> None:
        failures.append({"name": name, "expected": expected, "actual": actual})

    if payload.get("ok") is False:
        add_failure("runtime_status_ok", True, payload.get("ok"))
    if environment and environment != "paper":
        add_failure("broker_mode", "paper", environment)
    if gateway.get("running") is not True:
        add_failure("gateway_running", True, gateway.get("running"))
    if api_socket_listening is not True:
        add_failure("api_socket_listening", True, api_socket_listening)
    if gateway_reachable is not True:
        add_failure("gateway_reachable", True, gateway_reachable)
    if broker_connected is not True:
        add_failure("broker_connected", True, broker_connected)
    if broker_ready is not True:
        add_failure("broker_ready", True, broker_ready)
    if session_authenticated is not True:
        add_failure("session_authenticated", True, session_authenticated)
    if broker_status_code and broker_status_code not in {200, 2104, 2106, 2158}:
        add_failure("broker_status_code", "ready", broker_status_code)
    return {
        "ok": not failures,
        "environment": payload.get("environment"),
        "broker_mode": payload.get("broker_mode"),
        "status_mode": payload.get("status_mode"),
        "gateway_running": gateway.get("running"),
        "gateway_reachable": gateway.get("reachable"),
        "gateway_status_code": gateway.get("status_code"),
        "api_socket_listening": api_socket_listening,
        "api_socket_host": gateway.get("api_socket_host"),
        "api_socket_port": gateway.get("api_socket_port"),
        "api_socket_reason": gateway.get("api_socket_reason"),
        "broker_connected": broker_connected,
        "broker_ready": broker_ready,
        "broker_status_code": broker_status_code,
        "session_authenticated": session_authenticated,
        "websocket_ready": websocket.get("ready"),
        "topology_gateway_status": topology_gateway.get("status"),
        "topology_runtime_status": (services.get("ibkr-runtime") or {}).get("status") if isinstance(services.get("ibkr-runtime"), dict) else None,
        "failures": failures,
    }


def collect_gateway_readiness_precheck(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    timeout = min(10.0, max(1.0, float(getattr(args, "http_timeout_sec", 30.0) or 30.0)))
    try:
        status = get_gateway_status_lite(args, timeout_sec=timeout)
        summary = summarize_gateway_readiness(status)
        return {
            **summary,
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": str(exc),
            "failures": [{"name": "runtime_status_request", "expected": "success", "actual": str(exc)}],
        }


def build_gateway_precheck_failure_payload(args: argparse.Namespace, gateway_precheck: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "run_id": getattr(args, "run_id", ""),
        "dry_run": not bool(getattr(args, "execute", False)),
        "error": "gateway_readiness_precheck_failed",
        "created_at_et": datetime.now(ET).isoformat(),
        "created_at_cn": datetime.now(CN).isoformat(),
        "paper_only": True,
        "gateway_readiness_precheck": json_clone(gateway_precheck),
    }


def is_flat_for_symbols(snapshot: dict[str, Any], symbols: list[str]) -> bool:
    if not fee_probe.supports_symbol_flat_check(snapshot):
        return False
    return all(fee_probe.is_flat_for_symbol(snapshot, symbol) for symbol in symbols)


def wait_for_flat_symbols(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    deadline = time.time() + float(args.cleanup_timeout_sec)
    last_snapshot: dict[str, Any] = {}
    snapshot_timeout = cleanup_http_timeout(args)
    while time.time() <= deadline:
        try:
            last_snapshot = get_snapshot(args, timeout_sec=snapshot_timeout, orders_fast=True)
        except Exception as exc:
            last_snapshot = {"ok": False, "error": str(exc)}
        if is_flat_for_symbols(last_snapshot, symbols):
            return {"ok": True, "snapshot": last_snapshot}
        time.sleep(max(0.2, float(args.poll_interval_sec)))
    error = "cleanup_timeout_not_flat"
    if not fee_probe.supports_symbol_flat_check(last_snapshot):
        error = "cleanup_timeout_snapshot_unusable"
    return {"ok": False, "snapshot": last_snapshot, "error": error}


def summarize_account_snapshot(snapshot: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    selected: dict[str, dict[str, Any]] = {}
    total_open = 0
    for symbol in symbols:
        open_orders = fee_probe.open_orders_for_symbol(snapshot, symbol)
        total_open += len(open_orders)
        selected[symbol] = {
            "position_qty": fee_probe.net_position_quantity(snapshot, symbol),
            "open_order_count": len(open_orders),
            "open_order_ids": [fee_probe.order_id(order) for order in open_orders if fee_probe.order_id(order)],
        }
    cache_meta = snapshot.get("_cache") if isinstance(snapshot.get("_cache"), dict) else {}
    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    account_diagnostics = diagnostics.get("account_snapshot") if isinstance(diagnostics.get("account_snapshot"), dict) else {}
    orders_fast_diagnostics = (
        snapshot.get("orders_fast_diagnostics") if isinstance(snapshot.get("orders_fast_diagnostics"), dict) else {}
    )
    cache_state = fee_probe.to_text(cache_meta.get("state"))
    cache_stale = bool(cache_meta.get("stale")) or cache_state in {"stale", "stale_error", "bypass_stale_error"}
    return {
        "ok": snapshot.get("ok") is not False,
        "environment": snapshot.get("environment"),
        "broker_mode": snapshot.get("broker_mode"),
        "service_running": snapshot.get("service_running"),
        "service_starting": snapshot.get("service_starting"),
        "session_authenticated": snapshot.get("session_authenticated"),
        "websocket_ready": snapshot.get("websocket_ready"),
        "summary_available": snapshot.get("summary_available"),
        "stale": snapshot.get("stale"),
        "snapshot_profile": snapshot.get("snapshot_profile"),
        "source": snapshot.get("source"),
        "counts": snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {},
        "route_cache": cache_meta,
        "route_cache_state": cache_state,
        "route_cache_stale": cache_stale,
        "account_snapshot_diagnostics": account_diagnostics,
        "orders_fast_diagnostics": orders_fast_diagnostics,
        "selected_open_order_count": total_open,
        "selected_symbols": selected,
    }


def _active_order_command_count(snapshot: dict[str, Any]) -> int:
    diagnostics = snapshot.get("orders_fast_diagnostics") if isinstance(snapshot.get("orders_fast_diagnostics"), dict) else {}
    trust = diagnostics.get("pb_fallback_trust") if isinstance(diagnostics.get("pb_fallback_trust"), dict) else {}
    try:
        return max(0, int(float(trust.get("active_order_command_count") or 0)))
    except Exception:
        return 0


def _broker_open_count(snapshot: dict[str, Any]) -> int:
    diagnostics = snapshot.get("orders_fast_diagnostics") if isinstance(snapshot.get("orders_fast_diagnostics"), dict) else {}
    trust = diagnostics.get("pb_fallback_trust") if isinstance(diagnostics.get("pb_fallback_trust"), dict) else {}
    try:
        return max(0, int(float(trust.get("broker_open_count") or 0)))
    except Exception:
        return 0


def account_access_ok(summary: dict[str, Any]) -> bool:
    return (
        bool(summary.get("ok"))
        and summary.get("service_running") is not False
        and summary.get("session_authenticated") is not False
        and summary.get("websocket_ready") is not False
        and summary.get("stale") is not True
        and summary.get("route_cache_stale") is not True
    )


def sample_account_access(args: argparse.Namespace, symbols: list[str], *, phase: str, sample_index: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        snapshot = get_snapshot(args, orders_fast=bool(getattr(args, "pending_snapshot_orders_fast", True)))
        fee_probe.assert_paper_snapshot(snapshot)
        summary = summarize_account_snapshot(snapshot, symbols)
        ok = account_access_ok(summary)
        return {
            "ok": ok,
            "phase": phase,
            "sample_index": int(sample_index),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "summary": summary,
        }
    except Exception as exc:
        return {
            "ok": False,
            "phase": phase,
            "sample_index": int(sample_index),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def observe_pending_account_access(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    hold_seconds = max(float(args.pending_hold_seconds or 0.0), float(args.post_place_sleep_seconds or 0.0))
    min_samples = max(0, int(args.min_pending_hold_samples or 0))
    min_visible_orders = max(0, int(args.min_pending_visible_orders or 0))
    max_elapsed = max(0.1, float(args.max_pending_snapshot_elapsed_sec or 10.0))
    sample_goal = max(1, min_samples) if hold_seconds > 0 or min_samples > 0 else 0
    if sample_goal <= 0:
        return {"ok": True, "skipped": True, "reason": "pending_hold_not_requested", "samples": []}

    started = time.time()
    interval = max(0.25, float(args.pending_hold_sample_interval_sec or 1.0))
    max_samples = max(sample_goal, int(ceil(hold_seconds / interval)) + 1 if hold_seconds > 0 else sample_goal)
    samples: list[dict[str, Any]] = []
    while True:
        samples.append(sample_account_access(args, symbols, phase="pending_hold", sample_index=len(samples) + 1))
        elapsed = time.time() - started
        if elapsed >= hold_seconds and len(samples) >= sample_goal:
            break
        if len(samples) >= max_samples:
            break
        if elapsed < hold_seconds:
            sleep_for = min(interval, max(0.0, hold_seconds - elapsed))
        else:
            sleep_for = interval
        time.sleep(max(0.25, sleep_for))

    visible_counts = [
        int(((sample.get("summary") or {}).get("selected_open_order_count") or 0))
        for sample in samples
        if isinstance(sample, dict)
    ]
    elapsed_values = [float(sample.get("elapsed_s") or 0.0) for sample in samples if isinstance(sample, dict)]
    max_observed_orders = max(visible_counts or [0])
    max_observed_elapsed = max(elapsed_values or [0.0])
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ok_sample_count = sum(1 for sample in samples if sample.get("ok"))
    failed_sample_count = sum(1 for sample in samples if not sample.get("ok"))
    if ok_sample_count < sample_goal:
        failures.append({"name": "account_snapshot_samples_ok", "value": ok_sample_count, "threshold": sample_goal})
    if failed_sample_count:
        warnings.append({"name": "account_snapshot_sample_failures", "count": failed_sample_count})
    summary_unavailable_count = sum(
        1 for sample in samples if ((sample.get("summary") or {}).get("summary_available") is False)
    )
    if summary_unavailable_count:
        warnings.append({"name": "account_summary_unavailable", "count": summary_unavailable_count})
    if len(samples) < sample_goal:
        failures.append({"name": "account_snapshot_sample_count", "value": len(samples), "threshold": sample_goal})
    if max_observed_elapsed > max_elapsed:
        failures.append({"name": "account_snapshot_latency", "value": max_observed_elapsed, "threshold": max_elapsed})
    if max_observed_orders < min_visible_orders:
        failures.append({"name": "visible_pending_orders", "value": max_observed_orders, "threshold": min_visible_orders})
    return {
        "ok": not failures,
        "skipped": False,
        "hold_seconds": hold_seconds,
        "sample_interval_sec": interval,
        "min_samples": min_samples,
        "min_visible_orders": min_visible_orders,
        "max_snapshot_elapsed_sec": max_elapsed,
        "sample_count": len(samples),
        "ok_sample_count": ok_sample_count,
        "max_snapshot_elapsed_observed_sec": round(max_observed_elapsed, 3),
        "max_selected_open_order_count_observed": max_observed_orders,
        "failures": failures,
        "warnings": warnings,
        "samples": samples,
    }


def wait_for_submission_quiescence(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    timeout = max(0.0, float(getattr(args, "pre_cancel_quiesce_sec", 0.0) or 0.0))
    if timeout <= 0:
        return {"ok": True, "skipped": True, "reason": "pre_cancel_quiesce_not_requested", "samples": []}
    quiet_seconds = max(0.0, float(getattr(args, "pre_cancel_quiet_sec", 0.0) or 0.0))
    min_visible_orders = max(0, int(getattr(args, "min_pre_cancel_visible_orders", 0) or 0))
    if min_visible_orders <= 0:
        min_visible_orders = max(0, int(getattr(args, "min_pending_visible_orders", 0) or 0))
    poll_interval = max(0.25, float(getattr(args, "poll_interval_sec", 1.0) or 1.0))
    deadline = time.time() + timeout
    stable_since = 0.0
    last_signature: tuple[int, int, tuple[str, ...]] | None = None
    last_snapshot: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []

    while True:
        now = time.time()
        try:
            snapshot = get_snapshot(args, timeout_sec=cleanup_http_timeout(args), orders_fast=True)
            summary = summarize_account_snapshot(snapshot, symbols)
            selected_ids = sorted(
                str(order_id)
                for symbol in symbols
                for order_id in ((summary.get("selected_symbols") or {}).get(symbol, {}) or {}).get("open_order_ids", [])
                if str(order_id or "").strip()
            )
            selected_open_count = int(summary.get("selected_open_order_count") or 0)
            active_commands = _active_order_command_count(snapshot)
            broker_open_count = _broker_open_count(snapshot)
            signature = (selected_open_count, active_commands, tuple(selected_ids))
            if signature != last_signature:
                stable_since = now
                last_signature = signature
            stable_for = max(0.0, now - stable_since)
            ready = (
                account_access_ok(summary)
                and selected_open_count >= min_visible_orders
                and active_commands == 0
                and stable_for >= quiet_seconds
            )
            last_snapshot = snapshot
            samples.append(
                {
                    "ok": True,
                    "selected_open_order_count": selected_open_count,
                    "broker_open_count": broker_open_count,
                    "active_order_command_count": active_commands,
                    "stable_for_sec": round(stable_for, 3),
                    "ready": ready,
                }
            )
            if ready:
                return {
                    "ok": True,
                    "skipped": False,
                    "timeout_sec": timeout,
                    "quiet_sec": quiet_seconds,
                    "min_visible_orders": min_visible_orders,
                    "sample_count": len(samples),
                    "last_sample": samples[-1],
                    "samples": samples,
                    "snapshot": snapshot,
                }
        except Exception as exc:
            samples.append({"ok": False, "error": str(exc), "ready": False})

        if now >= deadline:
            return {
                "ok": False,
                "skipped": False,
                "error": "pre_cancel_quiesce_timeout",
                "timeout_sec": timeout,
                "quiet_sec": quiet_seconds,
                "min_visible_orders": min_visible_orders,
                "sample_count": len(samples),
                "last_sample": samples[-1] if samples else {},
                "samples": samples,
                "snapshot": last_snapshot,
            }
        time.sleep(min(poll_interval, max(0.0, deadline - now)))


def select_probe_plan(args: argparse.Namespace) -> tuple[list[OrderProbePlan], list[dict[str, Any]], dict[str, Any]]:
    if fee_probe.to_text(getattr(args, "plan_path", "")):
        return load_probe_plan(args)

    symbols = split_symbols(args.symbols)
    if not symbols:
        raise GatewayProbeError("no_symbols_configured")
    snapshot = get_snapshot(args)
    fee_probe.assert_paper_snapshot(snapshot)

    selected: list[OrderProbePlan] = []
    excluded: list[dict[str, Any]] = []
    reference_contexts = fetch_latest_reference_contexts(args, symbols)
    reference_contexts_available = reference_contexts is not None
    reference_contexts = reference_contexts or {}
    for symbol in symbols:
        if len(selected) >= max(1, int(args.orders or 1)):
            break
        try:
            fee_probe.check_clean_preflight(snapshot, symbol)
            if reference_contexts_available:
                if symbol not in reference_contexts:
                    raise GatewayProbeError("latest_reference_context_unavailable")
                reference_price, conid = reference_contexts[symbol]
            else:
                reference_price, conid = fetch_latest_reference_context(args, symbol)
            entry, tp, sl = build_non_marketable_bracket(
                reference_price,
                direction=args.direction,
                entry_distance_pct=float(args.entry_distance_pct),
                protection_gap_pct=float(args.protection_gap_pct),
            )
            quantity, requested_exposure = quantity_for_target_notional(
                entry,
                float(getattr(args, "target_notional_per_order", 0.0) or 0.0),
                int(args.quantity or 1),
            )
        except Exception as exc:
            excluded.append({"symbol": symbol, "reason": str(exc)})
            continue
        selected.append(
            OrderProbePlan(
                symbol=symbol,
                direction=args.direction,
                quantity=quantity,
                reference_price=round(float(reference_price), 4),
                entry_price=entry,
                take_profit_price=tp,
                stop_loss_price=sl,
                target_notional=round(float(getattr(args, "target_notional_per_order", 0.0) or 0.0), 4),
                requested_exposure=requested_exposure,
                conid=conid,
            )
        )
    summary = {
        "requested_symbols": symbols,
        "selected_symbols": [plan.symbol for plan in selected],
        "selected_orders": len(selected),
        "target_notional_per_order": float(getattr(args, "target_notional_per_order", 0.0) or 0.0),
        "total_requested_exposure": round(sum(plan.requested_exposure for plan in selected), 4),
        "excluded": excluded,
    }
    return selected, excluded, summary


def load_probe_plan(args: argparse.Namespace) -> tuple[list[OrderProbePlan], list[dict[str, Any]], dict[str, Any]]:
    plan_path = Path(fee_probe.to_text(getattr(args, "plan_path", "")))
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GatewayProbeError("plan_path_payload_not_object")
    raw_plan = payload.get("plan") if isinstance(payload.get("plan"), list) else []
    if not raw_plan:
        raise GatewayProbeError("plan_path_missing_plan")
    snapshot = get_snapshot(args)
    fee_probe.assert_paper_snapshot(snapshot)

    selected: list[OrderProbePlan] = []
    excluded: list[dict[str, Any]] = []
    for item in raw_plan:
        if not isinstance(item, dict):
            continue
        if len(selected) >= max(1, int(args.orders or 1)):
            break
        symbol = fee_probe.normalize_symbol(item.get("symbol"))
        try:
            fee_probe.check_clean_preflight(snapshot, symbol)
            entry = round_price(float(item.get("entry_price") or 0.0))
            tp = round_price(float(item.get("take_profit_price") or 0.0))
            sl = round_price(float(item.get("stop_loss_price") or 0.0))
            quantity = max(1, int(float(item.get("quantity") or 0)))
            conid = int(float(item.get("conid") or 0))
            target_notional = float(getattr(args, "target_notional_per_order", 0.0) or item.get("target_notional") or 0.0)
            if target_notional > 0:
                quantity, requested_exposure = quantity_for_target_notional(entry, target_notional, quantity)
            else:
                requested_exposure = round(float(item.get("requested_exposure") or entry * quantity), 4)
            if conid <= 0:
                conid = fetch_latest_conid(args, symbol)
            if not symbol or entry <= 0 or tp <= 0 or sl <= 0:
                raise GatewayProbeError("invalid_plan_item_prices")
        except Exception as exc:
            excluded.append({"symbol": symbol, "reason": str(exc)})
            continue
        selected.append(
            OrderProbePlan(
                symbol=symbol,
                direction=fee_probe.to_text(item.get("direction") or args.direction).lower() or args.direction,
                quantity=quantity,
                reference_price=float(item.get("reference_price") or 0.0),
                entry_price=entry,
                take_profit_price=tp,
                stop_loss_price=sl,
                target_notional=target_notional,
                requested_exposure=requested_exposure,
                conid=conid,
            )
        )
    summary = {
        "requested_symbols": [fee_probe.normalize_symbol(item.get("symbol")) for item in raw_plan if isinstance(item, dict)],
        "selected_symbols": [plan.symbol for plan in selected],
        "selected_orders": len(selected),
        "target_notional_per_order": float(getattr(args, "target_notional_per_order", 0.0) or (payload.get("plan_summary") or {}).get("target_notional_per_order") or 0.0),
        "total_requested_exposure": round(sum(plan.requested_exposure for plan in selected), 4),
        "excluded": excluded,
        "plan_path": str(plan_path),
    }
    return selected, excluded, summary


def stability_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        base_url=args.api_base_url,
        host=args.host,
        broker_mode="paper",
        prometheus_url=args.prometheus_url,
        stability_lookback_minutes=args.stability_lookback_minutes,
        skip_health=bool(args.skip_health),
        skip_stability_gate=bool(args.skip_stability_gate),
        max_firing_alerts=args.max_firing_alerts,
        max_broker_pending_requests=args.max_broker_pending_requests,
        max_order_failures=args.max_order_failures,
        max_signal_attention=args.max_signal_attention,
        max_order_operation_p95=args.max_order_operation_p95,
        max_gateway_serial_wait_p95=args.max_gateway_serial_wait_p95,
        max_gateway_serial_timeouts=args.max_gateway_serial_timeouts,
    )


def collect_health_and_stability(args: argparse.Namespace, phase: str) -> dict[str, Any]:
    if args.skip_health:
        return {
            "health": {},
            "stability": {"ok": True, "phase": phase, "skipped": True, "reason": "skip_health"},
        }
    import run_today_tv_replay_stress as replay_stress  # Local import keeps dry-run planning lightweight.

    replay_args = stability_args(args)
    health = replay_stress.phase0_health(replay_args)
    stability = replay_stress.evaluate_stability(replay_args, health, phase)
    return {"health": health, "stability": stability}


def collect_stability_with_settle(args: argparse.Namespace, phase: str) -> dict[str, Any]:
    result = collect_health_and_stability(args, phase)
    max_wait = max(0.0, float(getattr(args, "stability_settle_seconds", 0.0) or 0.0))
    if bool((result.get("stability") or {}).get("ok")) or max_wait <= 0:
        return result

    interval = max(5.0, float(getattr(args, "stability_settle_interval_sec", 30.0) or 30.0))
    attempts = [json_clone(result.get("stability") or {})]
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(min(interval, max(0.0, deadline - time.time())))
        result = collect_health_and_stability(args, phase)
        attempts.append(json_clone(result.get("stability") or {}))
        if bool((result.get("stability") or {}).get("ok")):
            break
    if isinstance(result.get("stability"), dict):
        result["stability"]["settle_attempts"] = attempts
    return result


def submit_one(client: fee_probe.ApiClient, plan: OrderProbePlan, *, delay_s: float = 0.0) -> dict[str, Any]:
    if delay_s > 0:
        time.sleep(delay_s)
    started = time.perf_counter()
    request_payload = plan.payload()
    request_payload["include_snapshot"] = False
    response = client.post("/api/custom/ibkr/orders/place", request_payload, action_params())
    elapsed = time.perf_counter() - started
    result = nested_result(response)
    return {
        "ok": response_action_ok(response),
        "symbol": plan.symbol,
        "payload": request_payload,
        "response": response,
        "error": response.get("error") or result.get("error") or result.get("reason") or "",
        "order_ids": fee_probe.extract_order_ids(response),
        "elapsed_s": round(elapsed, 3),
    }


def submit_timeout_result(
    plan: OrderProbePlan,
    *,
    timeout_s: float,
    reason: str = "submit_timeout",
    late_result: dict[str, Any] | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    late_payload = dict(late_result or {})
    result = {
        "ok": False,
        "symbol": plan.symbol,
        "payload": plan.payload(),
        "error": reason,
        "timed_out": True,
        "timeout_s": round(max(0.0, float(timeout_s or 0.0)), 3),
        "order_ids": list(late_payload.get("order_ids") or []),
    }
    if cancelled:
        result["cancelled_before_start"] = True
    if late_payload:
        result["late_after_submit_timeout"] = True
        result["late_ok"] = bool(late_payload.get("ok"))
        result["late_error"] = late_payload.get("error") or ""
        result["late_elapsed_s"] = late_payload.get("elapsed_s")
        result["response"] = late_payload.get("response") or {}
    return result


def submit_burst(args: argparse.Namespace, plans: list[OrderProbePlan]) -> list[dict[str, Any]]:
    workers = max(1, min(len(plans), int(args.burst_workers or 1)))
    spacing = max(0.0, float(args.burst_spacing_seconds or 0.0))
    submit_timeout = float(getattr(args, "submit_timeout_sec", 0.0) or 0.0)
    if submit_timeout <= 0:
        submit_timeout = max(float(args.http_timeout_sec) + 5.0, spacing * max(0, len(plans) - 1) + float(args.http_timeout_sec) + 5.0)

    effective_timeout = max(0.01, submit_timeout)
    started_at = time.monotonic()
    deadline = started_at + effective_timeout
    results_by_index: dict[int, dict[str, Any]] = {}

    def run_submit(index: int, plan: OrderProbePlan) -> dict[str, Any]:
        target_start = started_at + (index * spacing)
        while True:
            sleep_for = target_start - time.monotonic()
            if sleep_for <= 0:
                break
            remaining_before_delay = deadline - time.monotonic()
            if remaining_before_delay <= 0:
                return submit_timeout_result(plan, timeout_s=effective_timeout, reason="submit_timeout_before_start")
            time.sleep(min(sleep_for, remaining_before_delay, 0.25))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return submit_timeout_result(plan, timeout_s=effective_timeout, reason="submit_timeout_before_start")
        request_timeout = max(1.0, min(float(args.http_timeout_sec), remaining + 1.0))
        client = fee_probe.ApiClient(args.api_base_url, timeout=request_timeout)
        try:
            result = submit_one(client, plan, delay_s=0.0)
        except Exception as exc:
            result = {"ok": False, "symbol": plan.symbol, "payload": plan.payload(), "error": str(exc), "order_ids": []}
        result["_finished_monotonic"] = time.monotonic()
        return result

    def collect_result(index: int, plan: OrderProbePlan, future, *, timed_out: bool = False) -> None:
        try:
            result = future.result()
        except Exception as exc:
            result = {"ok": False, "symbol": plan.symbol, "payload": plan.payload(), "error": str(exc), "order_ids": []}
        finished_at = float(result.pop("_finished_monotonic", time.monotonic()) or time.monotonic())
        if timed_out or finished_at > deadline:
            results_by_index[index] = submit_timeout_result(plan, timeout_s=effective_timeout, late_result=result)
        else:
            results_by_index[index] = result

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gateway-probe-submit")
    try:
        future_by_index = {
            executor.submit(run_submit, index, plan): index
            for index, plan in enumerate(plans)
        }
        pending = set(future_by_index)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(0.5, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                index = future_by_index[future]
                collect_result(index, plans[index], future)

        if pending:
            running_after_deadline = set()
            for future in list(pending):
                index = future_by_index[future]
                if future.cancel():
                    results_by_index[index] = submit_timeout_result(
                        plans[index],
                        timeout_s=effective_timeout,
                        cancelled=True,
                    )
                    pending.remove(future)
                else:
                    running_after_deadline.add(future)
            if running_after_deadline:
                # Do not proceed to cleanup while local submit workers are still active;
                # late order ids are kept so cleanup can still cancel broker-side orders.
                done, _ = wait(running_after_deadline)
                for future in done:
                    index = future_by_index[future]
                    collect_result(index, plans[index], future, timed_out=True)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return [results_by_index[index] for index in range(len(plans))]


def summarize_place_acceptance(
    args: argparse.Namespace,
    place_results: list[dict[str, Any]],
    plans: list[OrderProbePlan],
) -> dict[str, Any]:
    expected_bp_blocks = [item for item in place_results if is_expected_buying_power_block(item)]
    unexpected_failures = [
        {
            "symbol": item.get("symbol"),
            "error": action_error_code(item) or item.get("error") or "place_failed",
        }
        for item in place_results
        if not item.get("ok") and not is_expected_buying_power_block(item)
    ]
    expect_blocks = bool(getattr(args, "expect_buying_power_blocks", False))
    min_blocks = max(0, int(getattr(args, "min_buying_power_blocks", 0) or 0))
    if expect_blocks and min_blocks <= 0:
        min_blocks = 1
    accepted_count = sum(1 for item in place_results if item.get("ok")) + len(expected_bp_blocks)
    ok = (
        len(place_results) == len(plans)
        and not unexpected_failures
        and (expect_blocks or not expected_bp_blocks)
        and len(expected_bp_blocks) >= min_blocks
        and accepted_count == len(plans)
    )
    return {
        "ok": ok,
        "accepted": accepted_count,
        "placed_ok": sum(1 for item in place_results if item.get("ok")),
        "buying_power_blocked": len(expected_bp_blocks),
        "expected_buying_power_blocks": expect_blocks,
        "min_buying_power_blocks": min_blocks,
        "unexpected_failures": unexpected_failures,
        "blocked_symbols": [str(item.get("symbol") or "") for item in expected_bp_blocks],
    }


def next_stop_loss_price(plan: OrderProbePlan, *, repeat_index: int = 0) -> float:
    step = max(0, int(repeat_index or 0)) + 1
    direction = str(plan.direction or "long").lower()
    if direction == "short":
        candidate = max(float(plan.entry_price) * 1.05, float(plan.stop_loss_price) * (1.0 - 0.01 * step))
        if candidate <= float(plan.entry_price):
            candidate = float(plan.entry_price) * 1.05
    else:
        candidate = min(float(plan.entry_price) * 0.95, float(plan.stop_loss_price) * (1.0 + 0.01 * step))
        if candidate >= float(plan.entry_price):
            candidate = float(plan.entry_price) * 0.95
    return round_price(candidate)


def build_stop_loss_modify_items(
    plans: list[OrderProbePlan],
    place_results: list[dict[str, Any]],
    *,
    repeat: int = 1,
) -> list[dict[str, Any]]:
    plan_by_symbol = {plan.symbol: plan for plan in plans}
    items: list[dict[str, Any]] = []
    for result in place_results:
        if not result.get("ok"):
            continue
        symbol = str(result.get("symbol") or "").upper()
        plan = plan_by_symbol.get(symbol)
        order_ids = [fee_probe.to_text(item) for item in (result.get("order_ids") or []) if fee_probe.to_text(item)]
        if not plan or len(order_ids) < 3:
            continue
        stop_order_id = order_ids[2]
        for repeat_index in range(max(1, int(repeat or 1))):
            items.append(
                {
                    "symbol": symbol,
                    "order_id": stop_order_id,
                    "old_stop_loss_price": plan.stop_loss_price,
                    "new_stop_loss_price": next_stop_loss_price(plan, repeat_index=repeat_index),
                    "repeat_index": repeat_index + 1,
                }
            )
    return items


def modify_stop_loss_storm(
    args: argparse.Namespace,
    plans: list[OrderProbePlan],
    place_results: list[dict[str, Any]],
) -> dict[str, Any]:
    items = build_stop_loss_modify_items(
        plans,
        place_results,
        repeat=max(1, int(getattr(args, "stop_loss_modify_repeat", 1) or 1)),
    )
    if not items:
        return {"ok": False, "requested": 0, "ok_count": 0, "failed": 0, "results": [], "error": "no_stop_loss_orders"}

    workers = max(1, min(len(items), int(getattr(args, "modify_burst_workers", 0) or len(items))))
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))

    def submit_modify(item: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        payload = {
            "order_id": item["order_id"],
            "price": item["new_stop_loss_price"],
            "symbol": item.get("symbol"),
            "order_family_type": "stop_loss",
            "source": "gateway_probe_stop_loss_modify_storm",
            "include_snapshot": False,
        }
        try:
            response = client.post("/api/custom/ibkr/orders/modify", payload, action_params())
            ok = response_action_ok(response)
            error = action_error_code({"response": response})
        except Exception as exc:
            response = {}
            ok = False
            error = str(exc)
        return {
            **item,
            "ok": ok,
            "payload": payload,
            "response": response,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": error,
        }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gateway-probe-modify") as executor:
        futures = [executor.submit(submit_modify, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (str(item.get("symbol") or ""), int(item.get("repeat_index") or 0)))
    ok_count = sum(1 for item in results if item.get("ok"))
    return {
        "ok": ok_count == len(results),
        "requested": len(items),
        "workers": workers,
        "ok_count": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }


def build_exit_cancel_items(place_results: list[dict[str, Any]], *, scope: str = "entry") -> list[dict[str, Any]]:
    normalized_scope = str(scope or "entry").strip().lower()
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for result in place_results:
        if not result.get("ok"):
            continue
        order_ids = [fee_probe.to_text(item) for item in (result.get("order_ids") or []) if fee_probe.to_text(item)]
        if not order_ids:
            continue
        selected = order_ids if normalized_scope == "all" else order_ids[:1]
        for index, order_id in enumerate(selected):
            if order_id in seen:
                continue
            seen.add(order_id)
            role = "entry" if index == 0 else "take_profit" if index == 1 else "stop_loss" if index == 2 else "unknown"
            items.append({"symbol": result.get("symbol"), "order_id": order_id, "role": role})
    return items


def cancel_order_items_burst(
    args: argparse.Namespace,
    items: list[dict[str, Any]],
    *,
    source: str,
    workers: int,
) -> dict[str, Any]:
    if not items:
        return {"ok": False, "requested": 0, "ok_count": 0, "failed": 0, "results": [], "error": "no_cancel_order_ids"}
    worker_count = max(1, min(len(items), int(workers or len(items))))
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))

    def submit_cancel(item: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        payload = {"order_id": item["order_id"], "source": source, "include_snapshot": False}
        try:
            response = client.post("/api/custom/ibkr/orders/cancel", payload, action_params())
            ok = response_action_ok(response)
            error = action_error_code({"response": response})
        except Exception as exc:
            response = {}
            ok = False
            error = str(exc)
        return {
            **item,
            "ok": ok,
            "payload": payload,
            "response": response,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": error,
            "source": source,
        }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="gateway-probe-exit") as executor:
        futures = [executor.submit(submit_cancel, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (str(item.get("symbol") or ""), str(item.get("order_id") or "")))
    ok_count = sum(1 for item in results if item.get("ok"))
    return {
        "ok": ok_count == len(results),
        "requested": len(items),
        "workers": worker_count,
        "ok_count": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }


def cancel_order_items_by_symbol_burst(
    args: argparse.Namespace,
    items: list[dict[str, Any]],
    *,
    source: str,
    workers: int,
) -> dict[str, Any]:
    if not items:
        return {"ok": False, "requested": 0, "ok_count": 0, "failed": 0, "results": [], "error": "no_cancel_order_ids"}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        symbol = fee_probe.normalize_symbol(item.get("symbol")) or "UNKNOWN"
        grouped.setdefault(symbol, []).append(item)
    groups = [
        {
            "symbol": symbol,
            "order_ids": [str(item.get("order_id") or "").strip() for item in rows if str(item.get("order_id") or "").strip()],
            "roles": [str(item.get("role") or "") for item in rows],
        }
        for symbol, rows in sorted(grouped.items())
    ]
    groups = [item for item in groups if item["order_ids"]]
    worker_count = max(1, min(len(groups), int(workers or len(groups) or 1)))
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))

    def submit_cancel_group(item: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        payload = {
            "symbol": item["symbol"],
            "order_ids": item["order_ids"],
            "source": source,
            "include_snapshot": False,
        }
        try:
            response = client.post("/api/custom/ibkr/orders/cancel", payload, action_params())
            ok = response_action_ok(response)
            error = action_error_code({"response": response})
            result = nested_result(response)
            submitted = len(result.get("submitted_order_ids") or result.get("order_ids") or []) if isinstance(result, dict) else 0
        except Exception as exc:
            response = {}
            ok = False
            error = str(exc)
            submitted = 0
        requested = len(item.get("order_ids") or [])
        return {
            **item,
            "ok": ok,
            "payload": payload,
            "response": response,
            "requested": requested,
            "submitted": submitted,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": error,
            "source": source,
            "batch_by_symbol": True,
        }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="gateway-probe-exit-symbol") as executor:
        futures = [executor.submit(submit_cancel_group, item) for item in groups]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: str(item.get("symbol") or ""))
    requested_total = sum(int(item.get("requested") or 0) for item in results)
    ok_count = sum(int(item.get("submitted") or 0) for item in results if item.get("ok"))
    return {
        "ok": ok_count == requested_total and requested_total > 0,
        "requested": requested_total,
        "group_count": len(results),
        "workers": worker_count,
        "ok_count": ok_count,
        "failed": requested_total - ok_count,
        "results": results,
        "batch_by_symbol": True,
    }


def exit_cancel_storm(args: argparse.Namespace, place_results: list[dict[str, Any]]) -> dict[str, Any]:
    items = build_exit_cancel_items(
        place_results,
        scope=str(getattr(args, "exit_cancel_scope", "entry") or "entry"),
    )
    if bool(getattr(args, "exit_cancel_batch_by_symbol", True)):
        return cancel_order_items_by_symbol_burst(
            args,
            items,
            source="gateway_probe_exit_cancel_storm",
            workers=max(1, int(getattr(args, "exit_burst_workers", 0) or len({item.get("symbol") for item in items}) or 1)),
        )
    return cancel_order_items_burst(
        args,
        items,
        source="gateway_probe_exit_cancel_storm",
        workers=max(1, int(getattr(args, "exit_burst_workers", 0) or len(items) or 1)),
    )


def cancel_visible_orders_for_symbols(args: argparse.Namespace, symbols: list[str]) -> list[dict[str, Any]]:
    snapshot = get_snapshot(args, timeout_sec=cleanup_http_timeout(args), orders_fast=True)
    if not fee_probe.supports_symbol_flat_check(snapshot):
        return [
            {
                "ok": False,
                "error": "account_snapshot_visibility_unavailable",
                "source": "visible_order_rescue",
                "snapshot": summarize_account_snapshot(snapshot, symbols),
            }
        ]
    client = fee_probe.ApiClient(args.api_base_url, timeout=cleanup_http_timeout(args))
    normalized_symbols = {fee_probe.normalize_symbol(symbol) for symbol in symbols if fee_probe.normalize_symbol(symbol)}
    seen: set[str] = set()
    cancel_results: list[dict[str, Any]] = []
    for key in ("live_open_orders", "orders"):
        for order in snapshot.get(key) or []:
            if not isinstance(order, dict) or not fee_probe.is_open_order(order):
                continue
            if fee_probe.order_symbol(order) not in normalized_symbols:
                continue
            oid = fee_probe.order_id(order)
            identity = oid or compact_json(order)[:200]
            if not oid or identity in seen:
                continue
            seen.add(identity)
            started = time.perf_counter()
            try:
                response = client.post(
                    "/api/custom/ibkr/orders/cancel",
                    {"order_id": oid, "include_snapshot": False},
                    action_params(),
                )
                ok = response_action_ok(response)
                error = action_error_code({"response": response})
            except Exception as exc:
                response = {}
                ok = False
                error = str(exc)
            cancel_results.append(
                {
                    "ok": ok,
                    "order_id": oid,
                    "symbol": fee_probe.order_symbol(order),
                    "response": response,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                    "error": error,
                    "source": "visible_order_rescue",
                }
            )
            time.sleep(max(0.0, float(args.cancel_spacing_seconds or 0.0)))
    return cancel_results


def cancel_known_order_ids(args: argparse.Namespace, place_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    seen: set[str] = set()
    cancel_results: list[dict[str, Any]] = []
    for item in place_results:
        for order_id in item.get("order_ids") or []:
            oid = fee_probe.to_text(order_id)
            if not oid or oid in seen:
                continue
            seen.add(oid)
            started = time.perf_counter()
            response = client.post(
                "/api/custom/ibkr/orders/cancel",
                {"order_id": oid, "include_snapshot": False},
                action_params(),
            )
            cancel_results.append(
                {
                    "ok": response_action_ok(response),
                    "order_id": oid,
                    "symbol": item.get("symbol"),
                    "response": response,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                    "error": action_error_code({"response": response}),
                }
            )
            time.sleep(max(0.0, float(args.cancel_spacing_seconds or 0.0)))
    return cancel_results


def cancel_all_orders(args: argparse.Namespace, *, source: str = "gateway_order_probe_cancel_all") -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    max_attempts = max(1, int(getattr(args, "cancel_all_attempts", 3) or 3))
    retry_delay = max(0.0, float(getattr(args, "cancel_all_retry_delay_sec", 10.0) or 0.0))
    final_response: dict[str, Any] = {}
    final_result: dict[str, Any] = {}
    final_ok = False
    final_error = ""
    submitted_ok = False
    total_started = time.perf_counter()
    for attempt in range(1, max_attempts + 1):
        client = fee_probe.ApiClient(args.api_base_url, timeout=cancel_all_http_timeout(args))
        started = time.perf_counter()
        try:
            response = client.post(
                "/api/custom/ibkr/orders/cancel_all",
                {"source": source, "attempt": attempt, "include_snapshot": False},
                action_params(),
            )
            result = nested_result(response)
            ok = response_action_ok(response)
            error = action_error_code({"response": response})
        except Exception as exc:
            response = {}
            result = {}
            ok = False
            error = str(exc)
        attempt_payload = {
            "attempt": attempt,
            "ok": ok,
            "response": response,
            "cancelled": result.get("cancelled"),
            "global_cancel_submitted": result.get("global_cancel_submitted"),
            "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
            "elapsed_s": round(time.perf_counter() - started, 3),
            "error": error,
        }
        attempts.append(attempt_payload)
        final_response = response
        final_result = result
        final_ok = ok
        final_error = error
        if ok and bool(result.get("global_cancel_submitted")):
            submitted_ok = True
            break
        if ok and not bool(result.get("pending_confirmation")):
            break
        if attempt < max_attempts:
            time.sleep(retry_delay * attempt)
    return [
        {
            "ok": bool(final_ok or submitted_ok),
            "source": "cancel_all",
            "response": final_response,
            "cancelled": final_result.get("cancelled"),
            "errors": final_result.get("errors") if isinstance(final_result.get("errors"), list) else [],
            "global_cancel_submitted": bool(final_result.get("global_cancel_submitted")) or submitted_ok,
            "pending_confirmation": bool(final_result.get("pending_confirmation")),
            "attempts": attempts,
            "elapsed_s": round(time.perf_counter() - total_started, 3),
            "error": final_error,
        }
    ]


def already_flat_cleanup_results(
    plans: list[OrderProbePlan],
    snapshot: dict[str, Any],
    *,
    reason: str,
    rescue_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, plan in enumerate(plans):
        result = {"final_snapshot": snapshot, "flat": True}
        if index == 0 and rescue_results is not None:
            result["visible_rescue_cancel_results"] = rescue_results
            result["visible_rescue_cancel_count"] = len(rescue_results)
            result["visible_rescue_cancel_ok"] = all(item.get("ok") for item in rescue_results)
        results.append(
            {
                "ok": True,
                "symbol": plan.symbol,
                "skipped": True,
                "reason": reason,
                "result": result,
            }
        )
    return results


def _bulk_cancel_was_submitted(cancel_results: list[dict[str, Any]] | None) -> bool:
    for item in cancel_results or []:
        if not isinstance(item, dict) or item.get("source") != "cancel_all":
            continue
        if item.get("ok") or int(item.get("cancelled") or 0) > 0:
            return True
        for attempt in item.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            if attempt.get("ok") or int(attempt.get("cancelled") or 0) > 0:
                return True
    return False


def _bulk_cancel_settle_timeout(args: argparse.Namespace) -> float:
    explicit = float(getattr(args, "bulk_cancel_settle_before_rescue_sec", 0.0) or 0.0)
    if explicit > 0:
        return explicit
    cleanup_timeout = float(getattr(args, "cleanup_timeout_sec", 0.0) or 0.0)
    return max(0.0, min(cleanup_timeout, 240.0))


def _wait_after_bulk_cancel(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    timeout = _bulk_cancel_settle_timeout(args)
    if timeout <= 0:
        return {"ok": False, "skipped": True, "reason": "bulk_cancel_settle_disabled"}
    wait_args = argparse.Namespace(**vars(args))
    wait_args.cleanup_timeout_sec = timeout
    return wait_for_flat_symbols(wait_args, symbols)


def cleanup_symbols(
    args: argparse.Namespace,
    plans: list[OrderProbePlan],
    place_results: list[dict[str, Any]],
    cancel_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    symbols = [plan.symbol for plan in plans]
    snapshot_timeout = cleanup_http_timeout(args)
    try:
        global_snapshot = get_snapshot(args, timeout_sec=snapshot_timeout, orders_fast=True)
    except Exception:
        global_snapshot = {}
    if global_snapshot and is_flat_for_symbols(global_snapshot, symbols):
        return already_flat_cleanup_results(plans, global_snapshot, reason="already_flat_after_bulk_cancel")

    if bool(getattr(args, "bulk_cancel_all", False)) and _bulk_cancel_was_submitted(cancel_results):
        try:
            post_bulk_cancel = _wait_after_bulk_cancel(args, symbols)
            post_bulk_snapshot = post_bulk_cancel.get("snapshot") or {}
        except Exception:
            post_bulk_cancel = {"ok": False}
            post_bulk_snapshot = {}
        if post_bulk_cancel.get("ok") and is_flat_for_symbols(post_bulk_snapshot, symbols):
            return already_flat_cleanup_results(plans, post_bulk_snapshot, reason="already_flat_after_bulk_cancel_wait")

        post_visibility_cancel_results = cancel_all_orders(
            args,
            source="gateway_order_probe_post_visibility_cancel_all",
        )
        try:
            post_visibility_wait = _wait_after_bulk_cancel(args, symbols)
            post_visibility_snapshot = post_visibility_wait.get("snapshot") or {}
        except Exception:
            post_visibility_wait = {"ok": False}
            post_visibility_snapshot = {}
        if post_visibility_wait.get("ok") and is_flat_for_symbols(post_visibility_snapshot, symbols):
            return already_flat_cleanup_results(
                plans,
                post_visibility_snapshot,
                reason="already_flat_after_post_visibility_cancel_all",
                rescue_results=post_visibility_cancel_results,
            )

    visible_rescue_cancel_results = cancel_visible_orders_for_symbols(args, symbols)
    if visible_rescue_cancel_results and all(item.get("ok") for item in visible_rescue_cancel_results):
        try:
            post_rescue_snapshot = wait_for_flat_symbols(args, symbols)
            post_rescue_final_snapshot = post_rescue_snapshot.get("snapshot") or {}
        except Exception:
            post_rescue_snapshot = {"ok": False}
            post_rescue_final_snapshot = {}
        if post_rescue_snapshot.get("ok") and is_flat_for_symbols(post_rescue_final_snapshot, symbols):
            return already_flat_cleanup_results(
                plans,
                post_rescue_final_snapshot,
                reason="already_flat_after_visible_order_rescue",
                rescue_results=visible_rescue_cancel_results,
            )

    client = fee_probe.ApiClient(args.api_base_url, timeout=snapshot_timeout)
    latest_place_by_symbol = {str(item.get("symbol")): item.get("response") or item for item in place_results}
    cleanup_results: list[dict[str, Any]] = []
    per_symbol_timeout = symbol_cleanup_timeout(args)
    for plan in plans:
        try:
            result = fee_probe.cleanup_symbol(
                client,
                plan.symbol,
                plan.direction,
                plan.quantity,
                latest_place_by_symbol.get(plan.symbol) or {},
                snapshot_fn=lambda: get_snapshot(args, timeout_sec=snapshot_timeout, orders_fast=True),
                timeout_s=per_symbol_timeout,
                interval_s=float(args.poll_interval_sec),
            )
            cleanup_results.append({"ok": fee_probe.is_flat_for_symbol(result.get("final_snapshot") or {}, plan.symbol), "symbol": plan.symbol, "result": result})
        except Exception as exc:
            cleanup_results.append({"ok": False, "symbol": plan.symbol, "error": str(exc)})
        time.sleep(max(0.0, float(args.cancel_spacing_seconds or 0.0)))
    return cleanup_results


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.execute) and args.confirm != CONFIRM_TEXT:
        raise GatewayProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")
    gateway_precheck: dict[str, Any] = {}
    if bool(args.execute):
        if bool(getattr(args, "skip_gateway_readiness_precheck", False)):
            gateway_precheck = {"ok": True, "skipped": True, "reason": "skip_gateway_readiness_precheck"}
        else:
            gateway_precheck = collect_gateway_readiness_precheck(args)
            if not gateway_precheck.get("ok"):
                raise GatewayProbeError(
                    f"gateway_readiness_precheck_failed:{compact_json(gateway_precheck)}",
                    payload=build_gateway_precheck_failure_payload(args, gateway_precheck),
                )

    plans, excluded, plan_summary = select_probe_plan(args)
    if len(plans) < max(1, int(args.min_orders or 1)):
        raise GatewayProbeError(f"insufficient_clean_probe_symbols:selected={len(plans)} required={args.min_orders}")

    payload: dict[str, Any] = {
        "ok": False,
        "dry_run": not bool(args.execute),
        "run_id": args.run_id,
        "created_at_et": datetime.now(ET).isoformat(),
        "created_at_cn": datetime.now(CN).isoformat(),
        "paper_only": True,
        "plan": [plan.summary() for plan in plans],
        "plan_summary": plan_summary,
        "excluded": excluded,
    }
    if gateway_precheck:
        payload["gateway_readiness_precheck"] = gateway_precheck
    if not args.execute:
        payload["ok"] = True
        payload["reason"] = "dry_run_plan_ready"
        payload["artifact"] = write_artifact(args, payload)
        return payload

    symbols = [plan.symbol for plan in plans]
    before_snapshot = get_snapshot(args)
    if not is_flat_for_symbols(before_snapshot, symbols):
        raise GatewayProbeError(f"preflight_not_flat:{compact_json({symbol: fee_probe.open_orders_for_symbol(before_snapshot, symbol) for symbol in symbols})[:1200]}")
    before = collect_health_and_stability(args, "gateway_probe_before")
    if not (before.get("stability") or {}).get("ok"):
        raise GatewayProbeError(f"stability_precheck_failed:{compact_json(before.get('stability'))[:1200]}")
    try:
        payload["orders_fast_cache_warmup"] = {
            "ok": True,
            "snapshot": get_snapshot(args, orders_fast=True, timeout_sec=min(30.0, float(args.http_timeout_sec))),
        }
    except Exception as exc:
        payload["orders_fast_cache_warmup"] = {"ok": False, "error": str(exc)}

    place_results: list[dict[str, Any]] = []
    cancel_results: list[dict[str, Any]] = []
    rescue_cancel_results: list[dict[str, Any]] = []
    cleanup_results: list[dict[str, Any]] = []
    modify_observation: dict[str, Any] = {"ok": True, "skipped": True, "reason": "modify_stop_loss_storm_not_requested"}
    exit_observation: dict[str, Any] = {"ok": True, "skipped": True, "reason": "exit_cancel_storm_not_requested"}
    pending_observation: dict[str, Any] = {}
    post_place_observation: dict[str, Any] = {"ok": True, "skipped": True, "reason": "post_place_quiesce_not_requested"}
    pre_cancel_observation: dict[str, Any] = {"ok": True, "skipped": True, "reason": "pre_cancel_quiesce_not_requested"}
    flat_after: dict[str, Any] = {}
    try:
        place_results = submit_burst(args, plans)
        submitted_order_count = sum(len(item.get("order_ids") or []) for item in place_results)
        if submitted_order_count <= 0:
            pending_observation = {"ok": True, "skipped": True, "reason": "no_submitted_orders"}
            post_place_observation = {"ok": True, "skipped": True, "reason": "no_submitted_orders"}
            pre_cancel_observation = {"ok": True, "skipped": True, "reason": "no_submitted_orders"}
            cancel_results = []
        else:
            pending_observation = observe_pending_account_access(args, symbols)
            post_place_observation = wait_for_submission_quiescence(args, symbols)
            if bool(getattr(args, "modify_stop_loss_storm", False)):
                modify_observation = modify_stop_loss_storm(args, plans, place_results)
            pre_cancel_observation = wait_for_submission_quiescence(args, symbols)
            if bool(getattr(args, "exit_cancel_storm", False)):
                exit_observation = exit_cancel_storm(args, place_results)
                cancel_results = list(exit_observation.get("results") or [])
            elif bool(getattr(args, "bulk_cancel_all", False)):
                cancel_results = cancel_all_orders(args)
            else:
                cancel_results = cancel_known_order_ids(args, place_results)
    finally:
        if any(item.get("timed_out") for item in place_results):
            rescue_cancel_results = cancel_visible_orders_for_symbols(args, symbols)
        cleanup_results = cleanup_symbols(args, plans, place_results, cancel_results)
        flat_after = wait_for_flat_symbols(args, symbols)

    after = collect_stability_with_settle(args, "gateway_probe_after")
    place_acceptance = summarize_place_acceptance(args, place_results, plans)
    place_ok = bool(place_acceptance.get("ok"))
    cancel_attempted = bool(cancel_results) or all(item.get("order_ids") == [] for item in place_results)
    cancel_ok = (not cancel_results and all(item.get("order_ids") == [] for item in place_results)) or all(
        item.get("ok") for item in cancel_results
    )
    modify_ok = bool(modify_observation.get("ok")) if bool(getattr(args, "modify_stop_loss_storm", False)) else True
    exit_ok = bool(exit_observation.get("ok")) if bool(getattr(args, "exit_cancel_storm", False)) else True
    cleanup_ok = all(item.get("ok") for item in cleanup_results) and bool(flat_after.get("ok"))
    account_lock_ok = bool(pending_observation.get("ok"))
    post_place_ok = bool(post_place_observation.get("ok"))
    pre_cancel_ok = bool(pre_cancel_observation.get("ok"))
    stability_ok = bool((before.get("stability") or {}).get("ok")) and bool((after.get("stability") or {}).get("ok"))
    payload.update(
        {
            "ok": bool(
                place_ok
                and cancel_attempted
                and cancel_ok
                and modify_ok
                and exit_ok
                and cleanup_ok
                and account_lock_ok
                and post_place_ok
                and pre_cancel_ok
                and stability_ok
            ),
            "reason": "gateway_order_probe_complete",
            "place_results": {
                "total": len(place_results),
                "ok": sum(1 for item in place_results if item.get("ok")),
                "failed": sum(1 for item in place_results if not item.get("ok")),
                "acceptance": place_acceptance,
            },
            "cancel_results": {
                "total": len(cancel_results) + len(rescue_cancel_results),
                "ok": sum(1 for item in [*cancel_results, *rescue_cancel_results] if item.get("ok")),
                "failed": sum(1 for item in [*cancel_results, *rescue_cancel_results] if not item.get("ok")),
                "rescue_total": len(rescue_cancel_results),
                "cancel_attempted": cancel_attempted,
                "cancel_ok": cancel_ok,
            },
            "modify_stop_loss_storm": {
                "ok": modify_observation.get("ok"),
                "skipped": modify_observation.get("skipped"),
                "requested": modify_observation.get("requested"),
                "ok_count": modify_observation.get("ok_count"),
                "failed": modify_observation.get("failed"),
            },
            "exit_cancel_storm": {
                "ok": exit_observation.get("ok"),
                "skipped": exit_observation.get("skipped"),
                "requested": exit_observation.get("requested"),
                "ok_count": exit_observation.get("ok_count"),
                "failed": exit_observation.get("failed"),
                "scope": getattr(args, "exit_cancel_scope", "entry"),
            },
            "cleanup_results": {
                "total": len(cleanup_results),
                "ok": sum(1 for item in cleanup_results if item.get("ok")),
                "failed": sum(1 for item in cleanup_results if not item.get("ok")),
            },
            "pending_hold": {
                "ok": pending_observation.get("ok"),
                "skipped": pending_observation.get("skipped"),
                "hold_seconds": pending_observation.get("hold_seconds"),
                "sample_count": pending_observation.get("sample_count"),
                "ok_sample_count": pending_observation.get("ok_sample_count"),
                "max_selected_open_order_count_observed": pending_observation.get("max_selected_open_order_count_observed"),
                "max_snapshot_elapsed_observed_sec": pending_observation.get("max_snapshot_elapsed_observed_sec"),
                "failures": pending_observation.get("failures") or [],
                "warnings": pending_observation.get("warnings") or [],
            },
            "post_place_quiesce": {
                "ok": post_place_observation.get("ok"),
                "skipped": post_place_observation.get("skipped"),
                "timeout_sec": post_place_observation.get("timeout_sec"),
                "quiet_sec": post_place_observation.get("quiet_sec"),
                "min_visible_orders": post_place_observation.get("min_visible_orders"),
                "sample_count": post_place_observation.get("sample_count"),
                "last_sample": post_place_observation.get("last_sample") or {},
                "error": post_place_observation.get("error"),
            },
            "pre_cancel_quiesce": {
                "ok": pre_cancel_observation.get("ok"),
                "skipped": pre_cancel_observation.get("skipped"),
                "timeout_sec": pre_cancel_observation.get("timeout_sec"),
                "quiet_sec": pre_cancel_observation.get("quiet_sec"),
                "min_visible_orders": pre_cancel_observation.get("min_visible_orders"),
                "sample_count": pre_cancel_observation.get("sample_count"),
                "last_sample": pre_cancel_observation.get("last_sample") or {},
                "error": pre_cancel_observation.get("error"),
            },
            "account_flat": {"before": True, "after": bool(flat_after.get("ok"))},
            "stability": {"before": before.get("stability"), "after": after.get("stability")},
            "health": {"before": before.get("health"), "after": after.get("health")},
            "details": {
                "place": place_results,
                "pending_hold": pending_observation,
                "post_place_quiesce": post_place_observation,
                "pre_cancel_quiesce": pre_cancel_observation,
                "modify_stop_loss_storm": modify_observation,
                "exit_cancel_storm": exit_observation,
                "cancel": cancel_results,
                "rescue_cancel": rescue_cancel_results,
                "cleanup": cleanup_results,
                "flat_after": flat_after,
            },
        }
    )
    payload["artifact"] = write_artifact(args, payload)
    return payload


def apply_stress_preset(args: argparse.Namespace) -> argparse.Namespace:
    if args.account_lock_stress:
        args.gateway_stress = True
    if not args.gateway_stress:
        return args
    required_orders = 45 if args.account_lock_stress else 3
    target_orders = max(int(args.orders or 0), required_orders)
    args.orders = target_orders
    args.min_orders = max(int(args.min_orders or 0), target_orders)
    args.burst_workers = max(int(args.burst_workers or 0), target_orders)
    args.quantity = max(1, int(args.quantity or 1))
    args.direction = "long"
    args.entry_distance_pct = max(float(args.entry_distance_pct or 0.0), 0.50)
    args.protection_gap_pct = max(float(args.protection_gap_pct or 0.0), 0.15)
    if args.account_lock_stress:
        args.target_notional_per_order = max(float(args.target_notional_per_order or 0.0), 5000.0)
        args.pending_hold_seconds = max(float(args.pending_hold_seconds or 0.0), 45.0)
        args.pending_hold_sample_interval_sec = max(1.0, min(float(args.pending_hold_sample_interval_sec or 10.0), 10.0))
        args.min_pending_hold_samples = max(int(args.min_pending_hold_samples or 0), 3)
        args.min_pending_visible_orders = max(int(args.min_pending_visible_orders or 0), target_orders)
        args.max_pending_snapshot_elapsed_sec = max(float(args.max_pending_snapshot_elapsed_sec or 10.0), 10.0)
        args.submit_timeout_sec = max(float(args.submit_timeout_sec or 0.0), 90.0)
    args.skip_health = False
    args.skip_stability_gate = False
    if target_orders >= 10:
        args.bulk_cancel_all = True
        args.stability_settle_seconds = max(float(getattr(args, "stability_settle_seconds", 0.0) or 0.0), 300.0)
    args.max_firing_alerts = 0.0
    args.max_broker_pending_requests = 0.0
    args.max_order_failures = 0.0
    args.max_signal_attention = 0.0
    if target_orders > 3:
        args.max_order_operation_p95 = max(float(args.max_order_operation_p95 or 0.0), 900.0)
        expected_queue_wait = max(2.0, min(float(args.submit_timeout_sec or 90.0), float(target_orders) * 20.0))
        args.max_gateway_serial_wait_p95 = max(float(args.max_gateway_serial_wait_p95 or 0.0), expected_queue_wait)
    else:
        args.max_order_operation_p95 = max(float(args.max_order_operation_p95 or 10.0), 10.0)
        args.max_gateway_serial_wait_p95 = max(float(args.max_gateway_serial_wait_p95 or 2.0), 2.0)
    args.max_gateway_serial_timeouts = 0.0
    return args


def apply_probe_safety_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if bool(getattr(args, "bulk_cancel_all", False)) and int(getattr(args, "orders", 0) or 0) >= 10:
        args.pre_cancel_quiesce_sec = max(float(getattr(args, "pre_cancel_quiesce_sec", 0.0) or 0.0), 300.0)
        args.pre_cancel_quiet_sec = max(float(getattr(args, "pre_cancel_quiet_sec", 0.0) or 0.0), 30.0)
        if int(getattr(args, "min_pre_cancel_visible_orders", 0) or 0) <= 0:
            args.min_pre_cancel_visible_orders = max(
                int(getattr(args, "min_pending_visible_orders", 0) or 0),
                int(getattr(args, "orders", 0) or 0) * 3,
            )
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_run_id = "GWPROBE_" + datetime.now(ET).strftime("%Y%m%d_%H%M%S_ET")
    parser = argparse.ArgumentParser(description="Safely validate paper IB Gateway order placement/cancel outside market hours.")
    parser.add_argument("--api-base-url", default=fee_probe.DEFAULT_API_BASE_URL)
    parser.add_argument("--account-base-url", default=os.environ.get("IBKR_ACCOUNT_BASE_URL") or "")
    parser.add_argument("--account-snapshot-path", default="")
    parser.add_argument("--host", default=fee_probe.DEFAULT_HOST)
    parser.add_argument("--db-path", default=fee_probe.DEFAULT_DB_PATH)
    parser.add_argument("--sqlite-script", default=str(fee_probe.DEFAULT_SQLITE_SCRIPT))
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--run-id", default=default_run_id)
    parser.add_argument("--plan-path", default="", help="Reuse a previously written dry-run summary.json plan instead of fetching reference prices again.")
    parser.add_argument("--symbols", default=os.environ.get("IBKR_GATEWAY_PROBE_SYMBOLS", DEFAULT_SYMBOLS))
    parser.add_argument("--orders", type=int, default=1)
    parser.add_argument("--min-orders", type=int, default=1)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--target-notional-per-order", type=float, default=0.0, help="Approximate entry notional per symbol; overrides quantity by sizing from the non-marketable entry price.")
    parser.add_argument("--direction", choices=("long", "short"), default="long")
    parser.add_argument("--reference-price", type=float, default=0.0)
    parser.add_argument("--entry-distance-pct", type=float, default=0.50)
    parser.add_argument("--protection-gap-pct", type=float, default=0.15)
    parser.add_argument("--burst-workers", type=int, default=1)
    parser.add_argument(
        "--burst-spacing-seconds",
        type=float,
        default=0.25,
        help="Stagger simulated signal submissions so the Gateway is stressed without flooding API/runtime threads at once.",
    )
    parser.add_argument("--cancel-spacing-seconds", type=float, default=0.75)
    parser.add_argument("--bulk-cancel-all", action="store_true", help="Use one paper cancel_all request for stress cleanup instead of cancelling every known order leg.")
    parser.add_argument("--post-place-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--pending-hold-seconds", type=float, default=0.0, help="Keep accepted paper orders pending for this long while sampling account access before cancel.")
    parser.add_argument("--pending-hold-sample-interval-sec", type=float, default=5.0)
    parser.add_argument("--min-pending-hold-samples", type=int, default=0)
    parser.add_argument("--min-pending-visible-orders", type=int, default=0)
    parser.add_argument("--max-pending-snapshot-elapsed-sec", type=float, default=10.0)
    parser.set_defaults(pending_snapshot_orders_fast=True)
    parser.add_argument("--no-pending-snapshot-orders-fast", action="store_false", dest="pending_snapshot_orders_fast", help="Use the full account snapshot during pending-hold samples instead of the cache-only live-orders fast path.")
    parser.add_argument("--pre-cancel-quiesce-sec", type=float, default=0.0, help="Before any bulk/exit cancel, wait for broker-visible orders to stop changing and active order commands to drain.")
    parser.add_argument("--pre-cancel-quiet-sec", type=float, default=15.0, help="Stable-order quiet window required by --pre-cancel-quiesce-sec.")
    parser.add_argument("--min-pre-cancel-visible-orders", type=int, default=0, help="Minimum selected open orders required before pre-cancel quiesce succeeds; defaults to --min-pending-visible-orders.")
    parser.add_argument("--cleanup-timeout-sec", type=float, default=60.0)
    parser.add_argument("--cleanup-http-timeout-sec", type=float, default=0.0, help="Bound account snapshot/order cleanup HTTP calls; defaults to min(http timeout, 60s).")
    parser.add_argument("--symbol-cleanup-timeout-sec", type=float, default=0.0, help="Per-symbol cleanup wait cap; defaults to min(cleanup timeout, 45s).")
    parser.add_argument("--cancel-all-http-timeout-sec", type=float, default=0.0, help="Bulk cancel_all HTTP timeout; defaults to up to 240s so 135-leg bracket bursts can reconcile.")
    parser.add_argument("--cancel-all-attempts", type=int, default=3, help="Retry bulk paper cancel_all before falling back to per-symbol cleanup.")
    parser.add_argument("--cancel-all-retry-delay-sec", type=float, default=10.0)
    parser.add_argument("--bulk-cancel-settle-before-rescue-sec", type=float, default=0.0, help="After a successful bulk cancel_all request, wait this long for broker callbacks before issuing rescue cancels; defaults to min(cleanup timeout, 240s).")
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    parser.add_argument("--submit-timeout-sec", type=float, default=0.0, help="Hard wall-clock limit for the concurrent place burst before timed-out symbols are failed and cleanup starts.")
    parser.add_argument("--sqlite-timeout-sec", type=float, default=45.0)
    parser.add_argument("--stability-lookback-minutes", type=float, default=5.0)
    parser.add_argument("--max-firing-alerts", type=float, default=0.0)
    parser.add_argument("--max-broker-pending-requests", type=float, default=0.0)
    parser.add_argument("--max-order-failures", type=float, default=0.0)
    parser.add_argument("--max-signal-attention", type=float, default=0.0)
    parser.add_argument("--max-order-operation-p95", type=float, default=10.0)
    parser.add_argument("--max-gateway-serial-wait-p95", type=float, default=2.0)
    parser.add_argument("--max-gateway-serial-timeouts", type=float, default=0.0)
    parser.add_argument("--stability-settle-seconds", type=float, default=0.0, help="Retry after-run stability for transient alert windows once cleanup is flat.")
    parser.add_argument("--stability-settle-interval-sec", type=float, default=30.0)
    parser.add_argument("--expect-buying-power-blocks", action="store_true", help="Treat buying_power_blocked placement responses as the expected over-BP outcome.")
    parser.add_argument("--min-buying-power-blocks", type=int, default=0, help="Minimum buying_power_blocked responses required when --expect-buying-power-blocks is set.")
    parser.add_argument("--modify-stop-loss-storm", action="store_true", help="After placement, modify all submitted stop-loss legs concurrently before cleanup.")
    parser.add_argument("--stop-loss-modify-repeat", type=int, default=1, help="Number of stop-loss modify requests per submitted bracket.")
    parser.add_argument("--modify-burst-workers", type=int, default=0, help="Concurrent workers for stop-loss modify storm; defaults to all modify requests.")
    parser.add_argument("--exit-cancel-storm", action="store_true", help="Use concurrent cancel requests to simulate simultaneous exit signals after placement.")
    parser.add_argument("--exit-cancel-scope", choices=("entry", "all"), default="entry", help="Cancel only entry legs or all known bracket legs during exit storm.")
    parser.add_argument("--exit-burst-workers", type=int, default=0, help="Concurrent workers for exit cancel storm; defaults to all selected cancel requests.")
    parser.set_defaults(exit_cancel_batch_by_symbol=True)
    parser.add_argument("--no-exit-cancel-batch-by-symbol", action="store_false", dest="exit_cancel_batch_by_symbol", help="Send one cancel request per leg instead of one batch request per symbol.")
    parser.add_argument("--gateway-stress", action="store_true", help="Require 3 safe paper orders, concurrent placement, staggered cancel, and strict stability gates.")
    parser.add_argument("--account-lock-stress", action="store_true", help="Require 5 concurrent safe paper orders and hold them pending while account/Gateway access is sampled.")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-stability-gate", action="store_true")
    parser.add_argument("--skip-gateway-readiness-precheck", action="store_true", help="Bypass the hard paper Gateway API/socket/auth precheck before submitting orders.")
    parser.add_argument("--execute", action="store_true", help="Submit paper orders. Without this flag only the safe plan is built.")
    parser.add_argument("--confirm", default="", help=f"Required with --execute: {CONFIRM_TEXT}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--use-system-proxy", action="store_true", help="Allow urllib to use OS/env proxy settings. By default the probe bypasses system proxies.")
    return apply_probe_safety_defaults(apply_stress_preset(parser.parse_args(argv)))


def print_text(payload: dict[str, Any]) -> None:
    print(f"ok={payload.get('ok')} dry_run={payload.get('dry_run')} run_id={payload.get('run_id')} reason={payload.get('reason')}")
    print(
        "selected_orders={selected_orders} symbols={symbols}".format(
            selected_orders=(payload.get("plan_summary") or {}).get("selected_orders"),
            symbols=",".join((payload.get("plan_summary") or {}).get("selected_symbols") or []),
        )
    )
    plan_summary = payload.get("plan_summary") or {}
    if plan_summary.get("total_requested_exposure") is not None:
        print(
            "target_notional_per_order={target} total_requested_exposure={total}".format(
                target=plan_summary.get("target_notional_per_order"),
                total=plan_summary.get("total_requested_exposure"),
            )
        )
    gateway_precheck = payload.get("gateway_readiness_precheck") or {}
    if gateway_precheck:
        print(
            "gateway_readiness_ok={ok} socket={socket} reachable={reachable} session={session} reason={reason}".format(
                ok=gateway_precheck.get("ok"),
                socket=gateway_precheck.get("api_socket_listening"),
                reachable=gateway_precheck.get("gateway_reachable"),
                session=gateway_precheck.get("session_authenticated"),
                reason=",".join(str(item.get("name")) for item in gateway_precheck.get("failures") or []),
            )
        )
    if "place_results" in payload:
        print(
            "place={place_ok}/{place_total} cancel={cancel_ok}/{cancel_total} cleanup={cleanup_ok}/{cleanup_total} flat_after={flat_after}".format(
                place_ok=(payload.get("place_results") or {}).get("ok"),
                place_total=(payload.get("place_results") or {}).get("total"),
                cancel_ok=(payload.get("cancel_results") or {}).get("ok"),
                cancel_total=(payload.get("cancel_results") or {}).get("total"),
                cleanup_ok=(payload.get("cleanup_results") or {}).get("ok"),
                cleanup_total=(payload.get("cleanup_results") or {}).get("total"),
                flat_after=(payload.get("account_flat") or {}).get("after"),
            )
        )
        acceptance = ((payload.get("place_results") or {}).get("acceptance") or {})
        if acceptance:
            print(
                "place_acceptance_ok={ok} accepted={accepted} bp_blocked={blocked} unexpected_failures={unexpected}".format(
                    ok=acceptance.get("ok"),
                    accepted=acceptance.get("accepted"),
                    blocked=acceptance.get("buying_power_blocked"),
                    unexpected=len(acceptance.get("unexpected_failures") or []),
                )
            )
        modify = payload.get("modify_stop_loss_storm") or {}
        if not modify.get("skipped", True):
            print(
                "modify_stop_loss_storm_ok={ok} ok={ok_count}/{requested} failed={failed}".format(
                    ok=modify.get("ok"),
                    ok_count=modify.get("ok_count"),
                    requested=modify.get("requested"),
                    failed=modify.get("failed"),
                )
            )
        exit_storm = payload.get("exit_cancel_storm") or {}
        if not exit_storm.get("skipped", True):
            print(
                "exit_cancel_storm_ok={ok} scope={scope} ok={ok_count}/{requested} failed={failed}".format(
                    ok=exit_storm.get("ok"),
                    scope=exit_storm.get("scope"),
                    ok_count=exit_storm.get("ok_count"),
                    requested=exit_storm.get("requested"),
                    failed=exit_storm.get("failed"),
                )
            )
        pending = payload.get("pending_hold") or {}
        print(
            "pending_hold_ok={ok} samples={samples} max_open_orders={open_orders} max_snapshot_elapsed={elapsed} failures={failures}".format(
                ok=pending.get("ok"),
                samples=pending.get("sample_count"),
                open_orders=pending.get("max_selected_open_order_count_observed"),
                elapsed=pending.get("max_snapshot_elapsed_observed_sec"),
                failures=",".join(str(item.get("name")) for item in pending.get("failures") or []),
            )
        )
        post_place = payload.get("post_place_quiesce") or {}
        if not post_place.get("skipped", True):
            print(
                "post_place_quiesce_ok={ok} samples={samples} last={last} error={error}".format(
                    ok=post_place.get("ok"),
                    samples=post_place.get("sample_count"),
                    last=compact_json(post_place.get("last_sample") or {}),
                    error=post_place.get("error") or "",
                )
            )
        pre_cancel = payload.get("pre_cancel_quiesce") or {}
        if not pre_cancel.get("skipped", True):
            print(
                "pre_cancel_quiesce_ok={ok} samples={samples} last={last} error={error}".format(
                    ok=pre_cancel.get("ok"),
                    samples=pre_cancel.get("sample_count"),
                    last=compact_json(pre_cancel.get("last_sample") or {}),
                    error=pre_cancel.get("error") or "",
                )
            )
        after_values = (((payload.get("stability") or {}).get("after") or {}).get("values") or {})
        print(
            "stability_after_ok={ok} gateway_wait_p95={wait} gateway_timeouts={timeouts} order_p95={order_p95}".format(
                ok=(((payload.get("stability") or {}).get("after") or {}).get("ok")),
                wait=after_values.get("gateway_serial_wait_p95"),
                timeouts=after_values.get("gateway_serial_timeouts_window"),
                order_p95=after_values.get("order_operation_p95"),
            )
        )
    print(f"artifact={payload.get('artifact')}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_http_proxy(args)
    try:
        payload = run_probe(args)
    except Exception as exc:
        structured_payload = getattr(exc, "payload", None)
        if isinstance(structured_payload, dict):
            payload = dict(structured_payload)
        else:
            payload = {
                "ok": False,
                "run_id": args.run_id,
                "dry_run": not bool(args.execute),
                "error": str(exc),
                "created_at_et": datetime.now(ET).isoformat(),
                "created_at_cn": datetime.now(CN).isoformat(),
            }
        try:
            payload["artifact"] = write_artifact(args, payload)
        except Exception:
            pass
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print_text(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
