#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
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
DEFAULT_SYMBOLS = (
    "TSLA,AAPL,MSFT,NVDA,AMD,META,GOOGL,AMZN,NFLX,ORCL,"
    "CRM,ADBE,INTC,CSCO,QCOM,AVGO,TXN,MU,IBM,NOW,SHOP,SNOW,PLTR,UBER,DDOG,NET,"
    "PANW,CRWD,ZS,TEAM,WDAY,INTU,ADP,PYPL,SQ,COIN,HOOD,ABNB,BKNG,MELI,SE,SPOT,"
    "ROKU,TWLO,OKTA,DELL,HPQ,SMCI,MRVL,LRCX,KLAC,AMAT,ASML,TSM,"
    "JPM,BAC,WFC,GS,MS,V,MA,AXP,DIS,CMCSA,PEP,KO,MCD,SBUX,COST,WMT,TGT,HD,LOW,"
    "NKE,BA,CAT,GE,GM,F,FDX,UPS,UNH,JNJ,PFE,MRK,ABBV,LLY,TMO,ISRG,VRTX"
)


class GatewayProbeError(RuntimeError):
    pass


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

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "order_type": "LMT",
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
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


def account_snapshot_client(args: argparse.Namespace) -> tuple[fee_probe.ApiClient, str]:
    base_url = fee_probe.to_text(args.account_base_url) or args.api_base_url
    snapshot_args = argparse.Namespace(
        account_base_url=fee_probe.to_text(args.account_base_url),
        account_snapshot_path=fee_probe.to_text(args.account_snapshot_path),
    )
    return fee_probe.ApiClient(base_url, timeout=float(args.http_timeout_sec)), fee_probe.account_snapshot_path(snapshot_args)


def get_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    client, path = account_snapshot_client(args)
    return fee_probe.get_account_snapshot(client, path)


def is_flat_for_symbols(snapshot: dict[str, Any], symbols: list[str]) -> bool:
    return all(fee_probe.is_flat_for_symbol(snapshot, symbol) for symbol in symbols)


def wait_for_flat_symbols(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    deadline = time.time() + float(args.cleanup_timeout_sec)
    last_snapshot: dict[str, Any] = {}
    while time.time() <= deadline:
        last_snapshot = get_snapshot(args)
        if is_flat_for_symbols(last_snapshot, symbols):
            return {"ok": True, "snapshot": last_snapshot}
        time.sleep(max(0.2, float(args.poll_interval_sec)))
    return {"ok": False, "snapshot": last_snapshot, "error": "cleanup_timeout_not_flat"}


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
        "selected_open_order_count": total_open,
        "selected_symbols": selected,
    }


def account_access_ok(summary: dict[str, Any]) -> bool:
    return (
        bool(summary.get("ok"))
        and summary.get("service_running") is not False
        and summary.get("session_authenticated") is not False
        and summary.get("websocket_ready") is not False
        and summary.get("summary_available") is not False
    )


def sample_account_access(args: argparse.Namespace, symbols: list[str], *, phase: str, sample_index: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        snapshot = get_snapshot(args)
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
    samples: list[dict[str, Any]] = []
    while True:
        samples.append(sample_account_access(args, symbols, phase="pending_hold", sample_index=len(samples) + 1))
        elapsed = time.time() - started
        if elapsed >= hold_seconds and len(samples) >= sample_goal:
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
    if not all(sample.get("ok") for sample in samples):
        failures.append({"name": "account_snapshot_samples_ok", "failed": sum(1 for sample in samples if not sample.get("ok"))})
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
        "max_snapshot_elapsed_observed_sec": round(max_observed_elapsed, 3),
        "max_selected_open_order_count_observed": max_observed_orders,
        "failures": failures,
        "samples": samples,
    }


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
    for symbol in symbols:
        if len(selected) >= max(1, int(args.orders or 1)):
            break
        try:
            fee_probe.check_clean_preflight(snapshot, symbol)
            reference_price = fee_probe.fetch_latest_reference_price(args, symbol)
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
                target_notional=float(item.get("target_notional") or getattr(args, "target_notional_per_order", 0.0) or 0.0),
                requested_exposure=round(float(item.get("requested_exposure") or entry * quantity), 4),
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


def submit_one(client: fee_probe.ApiClient, plan: OrderProbePlan, *, delay_s: float = 0.0) -> dict[str, Any]:
    if delay_s > 0:
        time.sleep(delay_s)
    started = time.perf_counter()
    response = client.post("/api/custom/ibkr/orders/place", plan.payload(), action_params())
    elapsed = time.perf_counter() - started
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return {
        "ok": bool(response.get("ok") and (result.get("ok") is not False)),
        "symbol": plan.symbol,
        "payload": plan.payload(),
        "response": response,
        "order_ids": fee_probe.extract_order_ids(response),
        "elapsed_s": round(elapsed, 3),
    }


def submit_burst(args: argparse.Namespace, plans: list[OrderProbePlan]) -> list[dict[str, Any]]:
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    workers = max(1, min(len(plans), int(args.burst_workers or 1)))
    spacing = max(0.0, float(args.burst_spacing_seconds or 0.0))
    submit_timeout = float(getattr(args, "submit_timeout_sec", 0.0) or 0.0)
    if submit_timeout <= 0:
        submit_timeout = max(float(args.http_timeout_sec) + 5.0, spacing * max(0, len(plans) - 1) + float(args.http_timeout_sec) + 5.0)

    work: queue.Queue[tuple[int, OrderProbePlan]] = queue.Queue()
    result_queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
    stop_event = threading.Event()
    for index, plan in enumerate(plans):
        work.put((index, plan))

    def worker() -> None:
        while not stop_event.is_set():
            try:
                index, plan = work.get_nowait()
            except queue.Empty:
                return
            if stop_event.is_set():
                return
            try:
                result = submit_one(client, plan, delay_s=index * spacing)
            except Exception as exc:
                result = {"ok": False, "symbol": plan.symbol, "error": str(exc), "order_ids": []}
            result_queue.put((index, result))

    threads = [
        threading.Thread(target=worker, name=f"gateway-probe-submit-{index + 1}", daemon=True)
        for index in range(workers)
    ]
    for thread in threads:
        thread.start()

    effective_timeout = max(0.01, submit_timeout)
    deadline = time.monotonic() + effective_timeout
    results_by_index: dict[int, dict[str, Any]] = {}
    while len(results_by_index) < len(plans):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, result = result_queue.get(timeout=min(0.5, remaining))
        except queue.Empty:
            continue
        results_by_index[index] = result

    if len(results_by_index) < len(plans):
        stop_event.set()
        for index, plan in enumerate(plans):
            if index not in results_by_index:
                results_by_index[index] = {
                    "ok": False,
                    "symbol": plan.symbol,
                    "payload": plan.payload(),
                    "error": "submit_timeout",
                    "timed_out": True,
                    "timeout_s": round(effective_timeout, 3),
                    "order_ids": [],
                }

    return [results_by_index[index] for index in range(len(plans))]


def cancel_visible_orders_for_symbols(args: argparse.Namespace, symbols: list[str]) -> list[dict[str, Any]]:
    snapshot = get_snapshot(args)
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
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
                response = client.post("/api/custom/ibkr/orders/cancel", {"order_id": oid}, action_params())
                ok = bool(response.get("ok"))
                error = response.get("error") or ((response.get("result") or {}).get("error") if isinstance(response.get("result"), dict) else "")
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
            response = client.post("/api/custom/ibkr/orders/cancel", {"order_id": oid}, action_params())
            cancel_results.append(
                {
                    "ok": bool(response.get("ok")),
                    "order_id": oid,
                    "symbol": item.get("symbol"),
                    "response": response,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                }
            )
            time.sleep(max(0.0, float(args.cancel_spacing_seconds or 0.0)))
    return cancel_results


def cleanup_symbols(args: argparse.Namespace, plans: list[OrderProbePlan], place_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    snapshot_client, snapshot_path = account_snapshot_client(args)
    latest_place_by_symbol = {str(item.get("symbol")): item.get("response") or item for item in place_results}
    cleanup_results: list[dict[str, Any]] = []
    for plan in plans:
        try:
            result = fee_probe.cleanup_symbol(
                client,
                plan.symbol,
                plan.direction,
                plan.quantity,
                latest_place_by_symbol.get(plan.symbol) or {},
                snapshot_fn=lambda path=snapshot_path: fee_probe.get_account_snapshot(snapshot_client, path),
                timeout_s=float(args.cleanup_timeout_sec),
                interval_s=float(args.poll_interval_sec),
            )
            cleanup_results.append({"ok": fee_probe.is_flat_for_symbol(result.get("final_snapshot") or {}, plan.symbol), "symbol": plan.symbol, "result": result})
        except Exception as exc:
            cleanup_results.append({"ok": False, "symbol": plan.symbol, "error": str(exc)})
        time.sleep(max(0.0, float(args.cancel_spacing_seconds or 0.0)))
    return cleanup_results


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
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
    if not args.execute:
        payload["ok"] = True
        payload["reason"] = "dry_run_plan_ready"
        payload["artifact"] = write_artifact(args, payload)
        return payload
    if args.confirm != CONFIRM_TEXT:
        raise GatewayProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")

    symbols = [plan.symbol for plan in plans]
    before_snapshot = get_snapshot(args)
    if not is_flat_for_symbols(before_snapshot, symbols):
        raise GatewayProbeError(f"preflight_not_flat:{compact_json({symbol: fee_probe.open_orders_for_symbol(before_snapshot, symbol) for symbol in symbols})[:1200]}")
    before = collect_health_and_stability(args, "gateway_probe_before")
    if not (before.get("stability") or {}).get("ok"):
        raise GatewayProbeError(f"stability_precheck_failed:{compact_json(before.get('stability'))[:1200]}")

    place_results: list[dict[str, Any]] = []
    cancel_results: list[dict[str, Any]] = []
    rescue_cancel_results: list[dict[str, Any]] = []
    cleanup_results: list[dict[str, Any]] = []
    pending_observation: dict[str, Any] = {}
    flat_after: dict[str, Any] = {}
    try:
        place_results = submit_burst(args, plans)
        pending_observation = observe_pending_account_access(args, symbols)
        cancel_results = cancel_known_order_ids(args, place_results)
    finally:
        if any(item.get("timed_out") for item in place_results):
            rescue_cancel_results = cancel_visible_orders_for_symbols(args, symbols)
        cleanup_results = cleanup_symbols(args, plans, place_results)
        flat_after = wait_for_flat_symbols(args, symbols)

    after = collect_health_and_stability(args, "gateway_probe_after")
    place_ok = all(item.get("ok") for item in place_results) and len(place_results) == len(plans)
    cancel_attempted = bool(cancel_results) or all(item.get("order_ids") == [] for item in place_results)
    cleanup_ok = all(item.get("ok") for item in cleanup_results) and bool(flat_after.get("ok"))
    account_lock_ok = bool(pending_observation.get("ok"))
    stability_ok = bool((before.get("stability") or {}).get("ok")) and bool((after.get("stability") or {}).get("ok"))
    payload.update(
        {
            "ok": bool(place_ok and cancel_attempted and cleanup_ok and account_lock_ok and stability_ok),
            "reason": "gateway_order_probe_complete",
            "place_results": {
                "total": len(place_results),
                "ok": sum(1 for item in place_results if item.get("ok")),
                "failed": sum(1 for item in place_results if not item.get("ok")),
            },
            "cancel_results": {
                "total": len(cancel_results) + len(rescue_cancel_results),
                "ok": sum(1 for item in [*cancel_results, *rescue_cancel_results] if item.get("ok")),
                "failed": sum(1 for item in [*cancel_results, *rescue_cancel_results] if not item.get("ok")),
                "rescue_total": len(rescue_cancel_results),
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
                "max_selected_open_order_count_observed": pending_observation.get("max_selected_open_order_count_observed"),
                "max_snapshot_elapsed_observed_sec": pending_observation.get("max_snapshot_elapsed_observed_sec"),
                "failures": pending_observation.get("failures") or [],
            },
            "account_flat": {"before": True, "after": bool(flat_after.get("ok"))},
            "stability": {"before": before.get("stability"), "after": after.get("stability")},
            "health": {"before": before.get("health"), "after": after.get("health")},
            "details": {
                "place": place_results,
                "pending_hold": pending_observation,
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
        args.max_pending_snapshot_elapsed_sec = min(float(args.max_pending_snapshot_elapsed_sec or 10.0), 10.0)
        args.submit_timeout_sec = max(float(args.submit_timeout_sec or 0.0), 90.0)
    args.skip_health = False
    args.skip_stability_gate = False
    args.max_firing_alerts = 0.0
    args.max_broker_pending_requests = 0.0
    args.max_order_failures = 0.0
    args.max_signal_attention = 0.0
    args.max_order_operation_p95 = min(float(args.max_order_operation_p95 or 10.0), 10.0)
    args.max_gateway_serial_wait_p95 = min(float(args.max_gateway_serial_wait_p95 or 2.0), 2.0)
    args.max_gateway_serial_timeouts = 0.0
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
    parser.add_argument("--burst-spacing-seconds", type=float, default=0.0)
    parser.add_argument("--cancel-spacing-seconds", type=float, default=0.75)
    parser.add_argument("--post-place-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--pending-hold-seconds", type=float, default=0.0, help="Keep accepted paper orders pending for this long while sampling account access before cancel.")
    parser.add_argument("--pending-hold-sample-interval-sec", type=float, default=5.0)
    parser.add_argument("--min-pending-hold-samples", type=int, default=0)
    parser.add_argument("--min-pending-visible-orders", type=int, default=0)
    parser.add_argument("--max-pending-snapshot-elapsed-sec", type=float, default=10.0)
    parser.add_argument("--cleanup-timeout-sec", type=float, default=60.0)
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
    parser.add_argument("--gateway-stress", action="store_true", help="Require 3 safe paper orders, concurrent placement, staggered cancel, and strict stability gates.")
    parser.add_argument("--account-lock-stress", action="store_true", help="Require 5 concurrent safe paper orders and hold them pending while account/Gateway access is sampled.")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-stability-gate", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Submit paper orders. Without this flag only the safe plan is built.")
    parser.add_argument("--confirm", default="", help=f"Required with --execute: {CONFIRM_TEXT}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--use-system-proxy", action="store_true", help="Allow urllib to use OS/env proxy settings. By default the probe bypasses system proxies.")
    return apply_stress_preset(parser.parse_args(argv))


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
