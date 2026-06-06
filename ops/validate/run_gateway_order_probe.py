#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
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


def select_probe_plan(args: argparse.Namespace) -> tuple[list[OrderProbePlan], list[dict[str, Any]], dict[str, Any]]:
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
        except Exception as exc:
            excluded.append({"symbol": symbol, "reason": str(exc)})
            continue
        selected.append(
            OrderProbePlan(
                symbol=symbol,
                direction=args.direction,
                quantity=max(1, int(args.quantity or 1)),
                reference_price=round(float(reference_price), 4),
                entry_price=entry,
                take_profit_price=tp,
                stop_loss_price=sl,
            )
        )
    summary = {
        "requested_symbols": symbols,
        "selected_symbols": [plan.symbol for plan in selected],
        "selected_orders": len(selected),
        "excluded": excluded,
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
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(submit_one, client, plan, delay_s=index * spacing): plan.symbol
            for index, plan in enumerate(plans)
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ok": False, "symbol": futures[future], "error": str(exc), "order_ids": []})
    results.sort(key=lambda item: [plan.symbol for plan in plans].index(str(item.get("symbol"))) if str(item.get("symbol")) in [plan.symbol for plan in plans] else 999)
    return results


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
    cleanup_results: list[dict[str, Any]] = []
    flat_after: dict[str, Any] = {}
    try:
        place_results = submit_burst(args, plans)
        time.sleep(max(0.0, float(args.post_place_sleep_seconds or 0.0)))
        cancel_results = cancel_known_order_ids(args, place_results)
    finally:
        cleanup_results = cleanup_symbols(args, plans, place_results)
        flat_after = wait_for_flat_symbols(args, symbols)

    after = collect_health_and_stability(args, "gateway_probe_after")
    place_ok = all(item.get("ok") for item in place_results) and len(place_results) == len(plans)
    cancel_attempted = bool(cancel_results) or all(item.get("order_ids") == [] for item in place_results)
    cleanup_ok = all(item.get("ok") for item in cleanup_results) and bool(flat_after.get("ok"))
    stability_ok = bool((before.get("stability") or {}).get("ok")) and bool((after.get("stability") or {}).get("ok"))
    payload.update(
        {
            "ok": bool(place_ok and cancel_attempted and cleanup_ok and stability_ok),
            "reason": "gateway_order_probe_complete",
            "place_results": {
                "total": len(place_results),
                "ok": sum(1 for item in place_results if item.get("ok")),
                "failed": sum(1 for item in place_results if not item.get("ok")),
            },
            "cancel_results": {
                "total": len(cancel_results),
                "ok": sum(1 for item in cancel_results if item.get("ok")),
                "failed": sum(1 for item in cancel_results if not item.get("ok")),
            },
            "cleanup_results": {
                "total": len(cleanup_results),
                "ok": sum(1 for item in cleanup_results if item.get("ok")),
                "failed": sum(1 for item in cleanup_results if not item.get("ok")),
            },
            "account_flat": {"before": True, "after": bool(flat_after.get("ok"))},
            "stability": {"before": before.get("stability"), "after": after.get("stability")},
            "health": {"before": before.get("health"), "after": after.get("health")},
            "details": {
                "place": place_results,
                "cancel": cancel_results,
                "cleanup": cleanup_results,
                "flat_after": flat_after,
            },
        }
    )
    payload["artifact"] = write_artifact(args, payload)
    return payload


def apply_stress_preset(args: argparse.Namespace) -> argparse.Namespace:
    if not args.gateway_stress:
        return args
    args.orders = max(int(args.orders or 0), 3)
    args.min_orders = max(int(args.min_orders or 0), 3)
    args.burst_workers = max(int(args.burst_workers or 0), 3)
    args.quantity = max(1, int(args.quantity or 1))
    args.direction = "long"
    args.entry_distance_pct = max(float(args.entry_distance_pct or 0.0), 0.50)
    args.protection_gap_pct = max(float(args.protection_gap_pct or 0.0), 0.15)
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
    parser.add_argument("--symbols", default=os.environ.get("IBKR_GATEWAY_PROBE_SYMBOLS", "TSLA,AAPL,MSFT,NVDA,AMD,META,GOOGL"))
    parser.add_argument("--orders", type=int, default=1)
    parser.add_argument("--min-orders", type=int, default=1)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--direction", choices=("long", "short"), default="long")
    parser.add_argument("--reference-price", type=float, default=0.0)
    parser.add_argument("--entry-distance-pct", type=float, default=0.50)
    parser.add_argument("--protection-gap-pct", type=float, default=0.15)
    parser.add_argument("--burst-workers", type=int, default=1)
    parser.add_argument("--burst-spacing-seconds", type=float, default=0.0)
    parser.add_argument("--cancel-spacing-seconds", type=float, default=0.75)
    parser.add_argument("--post-place-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--cleanup-timeout-sec", type=float, default=60.0)
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
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
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-stability-gate", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Submit paper orders. Without this flag only the safe plan is built.")
    parser.add_argument("--confirm", default="", help=f"Required with --execute: {CONFIRM_TEXT}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return apply_stress_preset(parser.parse_args(argv))


def print_text(payload: dict[str, Any]) -> None:
    print(f"ok={payload.get('ok')} dry_run={payload.get('dry_run')} run_id={payload.get('run_id')} reason={payload.get('reason')}")
    print(
        "selected_orders={selected_orders} symbols={symbols}".format(
            selected_orders=(payload.get("plan_summary") or {}).get("selected_orders"),
            symbols=",".join((payload.get("plan_summary") or {}).get("selected_symbols") or []),
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
