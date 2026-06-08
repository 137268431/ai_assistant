#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_gateway_order_probe as order_probe  # noqa: E402


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")
CONFIRM_TEXT = "PAPER_GATEWAY_PRESSURE_SUITE"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/validation/gateway_pressure_suite")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def request_json(base_url: str, path: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict:
    url = str(base_url or "").rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def now_run_id() -> str:
    return "GWPSUITE_" + datetime.now(ET).strftime("%Y%m%d_%H%M%S_ET")


def write_summary(suite_dir: Path, payload: dict[str, Any]) -> str:
    suite_dir.mkdir(parents=True, exist_ok=True)
    path = suite_dir / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return str(path)


def account_fast_snapshot(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    try:
        payload = request_json(
            args.account_base_url,
            args.account_snapshot_path,
            {
                "environment": "paper",
                "broker_mode": "paper",
                "orders_fast": 1,
                "snapshot_profile": "orders_fast",
                "include_pnl": 0,
                "open_orders_only": 1,
                "cache": 0,
                "cache_bust": int(time.time() * 1000),
            },
            timeout=args.account_timeout_sec,
        )
    except Exception as exc:
        return {"ok": False, "elapsed_s": round(time.perf_counter() - started, 3), "error": str(exc)}
    diagnostics = payload.get("orders_fast_diagnostics") if isinstance(payload.get("orders_fast_diagnostics"), dict) else {}
    trust = diagnostics.get("pb_fallback_trust") if isinstance(diagnostics.get("pb_fallback_trust"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "ok": payload.get("ok"),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "open_orders": (payload.get("counts") or {}).get("open_orders") if isinstance(payload.get("counts"), dict) else None,
        "buying_power": summary.get("buying_power"),
        "local_reserved_exposure": summary.get("local_reserved_exposure"),
        "local_reserved_count": summary.get("local_reserved_count"),
        "summary_source": diagnostics.get("summary_source"),
        "pb_fallback_order_count": diagnostics.get("pb_fallback_order_count"),
        "broker_open_count": trust.get("broker_open_count"),
        "active_order_command_count": trust.get("active_order_command_count"),
        "elapsed_ms": diagnostics.get("elapsed_ms"),
    }


def firing_alerts(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    if not str(args.prometheus_url or "").strip():
        return {"ok": True, "skipped": True, "alerts": []}
    try:
        payload = request_json(args.prometheus_url, "/api/v1/alerts", timeout=args.account_timeout_sec)
    except Exception as exc:
        return {"ok": False, "elapsed_s": round(time.perf_counter() - started, 3), "error": str(exc), "alerts": []}
    alerts = []
    for item in ((payload.get("data") or {}).get("alerts") or []):
        if item.get("state") != "firing":
            continue
        labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        alerts.append(
            {
                "alertname": labels.get("alertname"),
                "state": item.get("state"),
                "summary": annotations.get("summary"),
            }
        )
    return {"ok": True, "elapsed_s": round(time.perf_counter() - started, 3), "alerts": alerts}


def readiness_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        account_base_url=args.account_base_url,
        account_snapshot_path=args.account_snapshot_path,
        http_timeout_sec=args.http_timeout_sec,
        use_system_proxy=False,
    )


def wait_for_gateway_ready(args: argparse.Namespace, suite_dir: Path, payload: dict[str, Any]) -> dict:
    started = time.time()
    attempts: list[dict[str, Any]] = []
    deadline = None if float(args.readiness_timeout_sec or 0.0) <= 0 else started + float(args.readiness_timeout_sec)
    while True:
        precheck = order_probe.collect_gateway_readiness_precheck(readiness_namespace(args))
        attempts.append(
            {
                "at_et": datetime.now(ET).isoformat(),
                "ok": precheck.get("ok"),
                "elapsed_s": precheck.get("elapsed_s"),
                "gateway_running": precheck.get("gateway_running"),
                "gateway_reachable": precheck.get("gateway_reachable"),
                "api_socket_listening": precheck.get("api_socket_listening"),
                "api_socket_reason": precheck.get("api_socket_reason"),
                "session_authenticated": precheck.get("session_authenticated"),
                "topology_gateway_status": precheck.get("topology_gateway_status"),
                "topology_runtime_status": precheck.get("topology_runtime_status"),
                "failures": precheck.get("failures") or [],
            }
        )
        payload["readiness"] = {
            "ok": bool(precheck.get("ok")),
            "attempt_count": len(attempts),
            "attempts": attempts[-args.max_readiness_attempts_in_summary :],
            "last": attempts[-1],
        }
        write_summary(suite_dir, payload)
        if precheck.get("ok"):
            return payload["readiness"]
        if not args.wait_for_ready:
            payload["error"] = "gateway_not_ready"
            return payload["readiness"]
        if deadline is not None and time.time() >= deadline:
            payload["error"] = "gateway_readiness_timeout"
            return payload["readiness"]
        time.sleep(max(1.0, float(args.readiness_poll_sec or 30.0)))


def base_probe_command(args: argparse.Namespace, suite_dir: Path, stage_name: str) -> list[str]:
    cmd = [
        args.python,
        str(SCRIPT_DIR / "run_gateway_order_probe.py"),
        "--api-base-url",
        args.api_base_url,
        "--account-base-url",
        args.account_base_url,
        "--account-snapshot-path",
        args.account_snapshot_path,
        "--prometheus-url",
        args.prometheus_url,
        "--artifact-root",
        str(suite_dir / "gateway_order_probe"),
        "--run-id",
        f"{args.run_id}_{stage_name}",
        "--format",
        "json",
        "--http-timeout-sec",
        str(args.http_timeout_sec),
        "--submit-timeout-sec",
        str(args.submit_timeout_sec),
        "--cleanup-timeout-sec",
        str(args.cleanup_timeout_sec),
        "--cleanup-http-timeout-sec",
        str(args.cleanup_http_timeout_sec),
        "--symbol-cleanup-timeout-sec",
        str(args.symbol_cleanup_timeout_sec),
        "--max-firing-alerts",
        str(args.max_firing_alerts),
        "--max-broker-pending-requests",
        str(args.max_broker_pending_requests),
        "--max-order-operation-p95",
        str(args.max_order_operation_p95),
        "--max-gateway-serial-wait-p95",
        str(args.max_gateway_serial_wait_p95),
    ]
    if args.execute:
        cmd.extend(["--execute", "--confirm", order_probe.CONFIRM_TEXT])
    return cmd


def stage_commands(args: argparse.Namespace, suite_dir: Path) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}

    cmd = base_probe_command(args, suite_dir, "20_open_modify_exit")
    cmd.extend(
        [
            "--orders",
            str(args.open20_orders),
            "--min-orders",
            str(args.open20_orders),
            "--target-notional-per-order",
            str(args.open20_notional),
            "--burst-workers",
            str(args.open20_orders),
            "--burst-spacing-seconds",
            "0",
            "--pending-hold-seconds",
            str(args.open20_pending_hold_sec),
            "--pending-hold-sample-interval-sec",
            "10",
            "--min-pending-hold-samples",
            "4",
            "--min-pending-visible-orders",
            str(args.open20_orders * 3),
            "--max-pending-snapshot-elapsed-sec",
            "10",
            "--pre-cancel-quiesce-sec",
            "360",
            "--pre-cancel-quiet-sec",
            "20",
            "--min-pre-cancel-visible-orders",
            str(args.open20_orders * 3),
            "--modify-stop-loss-storm",
            "--modify-burst-workers",
            str(args.open20_orders),
            "--stop-loss-modify-repeat",
            "1",
            "--exit-cancel-storm",
            "--exit-cancel-scope",
            "all",
            "--exit-burst-workers",
            str(args.open20_orders),
            "--stability-settle-seconds",
            str(args.stability_settle_seconds),
            "--stability-settle-interval-sec",
            "20",
        ]
    )
    commands["20_open_modify_exit"] = cmd

    cmd = base_probe_command(args, suite_dir, "over_bp")
    cmd.extend(
        [
            "--orders",
            str(args.over_bp_orders),
            "--min-orders",
            str(args.over_bp_orders),
            "--target-notional-per-order",
            str(args.over_bp_notional),
            "--burst-workers",
            str(args.over_bp_orders),
            "--burst-spacing-seconds",
            "0",
            "--pending-hold-seconds",
            "30",
            "--pending-hold-sample-interval-sec",
            "10",
            "--min-pending-hold-samples",
            "3",
            "--min-pending-visible-orders",
            str(args.over_bp_min_visible_orders),
            "--max-pending-snapshot-elapsed-sec",
            "10",
            "--bulk-cancel-all",
            "--pre-cancel-quiesce-sec",
            "300",
            "--pre-cancel-quiet-sec",
            "20",
            "--min-pre-cancel-visible-orders",
            str(args.over_bp_min_visible_orders),
            "--bulk-cancel-settle-before-rescue-sec",
            "180",
            "--cancel-all-http-timeout-sec",
            "180",
            "--expect-buying-power-blocks",
            "--min-buying-power-blocks",
            str(args.min_buying_power_blocks),
            "--stability-settle-seconds",
            str(args.stability_settle_seconds),
            "--stability-settle-interval-sec",
            "20",
        ]
    )
    commands["over_bp"] = cmd

    cmd = base_probe_command(args, suite_dir, "45_open_pressure")
    cmd.extend(
        [
            "--orders",
            str(args.open45_orders),
            "--min-orders",
            str(args.open45_orders),
            "--target-notional-per-order",
            str(args.open45_notional),
            "--burst-workers",
            str(args.open45_orders),
            "--burst-spacing-seconds",
            "0",
            "--pending-hold-seconds",
            str(args.open45_pending_hold_sec),
            "--pending-hold-sample-interval-sec",
            "10",
            "--min-pending-hold-samples",
            "4",
            "--min-pending-visible-orders",
            str(args.open45_orders * 3),
            "--max-pending-snapshot-elapsed-sec",
            "10",
            "--bulk-cancel-all",
            "--pre-cancel-quiesce-sec",
            "360",
            "--pre-cancel-quiet-sec",
            "20",
            "--min-pre-cancel-visible-orders",
            str(args.open45_orders * 3),
            "--bulk-cancel-settle-before-rescue-sec",
            "240",
            "--cancel-all-http-timeout-sec",
            "240",
            "--stability-settle-seconds",
            str(args.stability_settle_seconds),
            "--stability-settle-interval-sec",
            "20",
        ]
    )
    commands["45_open_pressure"] = cmd
    return commands


def run_stage(args: argparse.Namespace, suite_dir: Path, name: str, cmd: list[str]) -> dict[str, Any]:
    stdout_path = suite_dir / f"{name}.json"
    stderr_path = suite_dir / f"{name}.stderr.log"
    started = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            proc = subprocess.run(cmd, stdout=stdout, stderr=stderr, text=True, timeout=args.stage_timeout_sec, check=False)
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stderr.write(str(exc) + "\n")
            proc = None
            timed_out = True
    child_payload: dict[str, Any] = {}
    try:
        child_payload = json.loads(stdout_path.read_text(encoding="utf-8"))
    except Exception as exc:
        child_payload = {"ok": False, "parse_error": str(exc)}
    return {
        "name": name,
        "ok": bool(child_payload.get("ok")) and not timed_out and (proc is not None and proc.returncode == 0),
        "returncode": None if proc is None else proc.returncode,
        "timed_out": timed_out,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "child_artifact": child_payload.get("artifact"),
        "child_ok": child_payload.get("ok"),
        "child_error": child_payload.get("error"),
        "gateway_readiness_precheck": child_payload.get("gateway_readiness_precheck"),
        "plan_summary": child_payload.get("plan_summary"),
        "place_results": child_payload.get("place_results"),
        "modify_results": child_payload.get("modify_results"),
        "cancel_results": child_payload.get("cancel_results"),
        "account_flat": child_payload.get("account_flat"),
        "stability": child_payload.get("stability"),
    }


def run_suite(args: argparse.Namespace) -> dict[str, Any]:
    if args.execute and args.confirm != CONFIRM_TEXT:
        raise SystemExit(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")

    suite_dir = Path(args.artifact_root) / args.run_id
    payload: dict[str, Any] = {
        "ok": False,
        "run_id": args.run_id,
        "dry_run": not bool(args.execute),
        "created_at_et": datetime.now(ET).isoformat(),
        "created_at_cn": datetime.now(CN).isoformat(),
        "suite_dir": str(suite_dir),
        "stages_requested": args.stages,
        "pre_account": account_fast_snapshot(args),
        "pre_alerts": firing_alerts(args),
        "stages": [],
    }
    write_summary(suite_dir, payload)

    readiness = wait_for_gateway_ready(args, suite_dir, payload)
    if not readiness.get("ok"):
        payload["post_account"] = account_fast_snapshot(args)
        payload["post_alerts"] = firing_alerts(args)
        payload["ok"] = False
        payload["artifact"] = write_summary(suite_dir, payload)
        return payload

    commands = stage_commands(args, suite_dir)
    for stage in [item.strip() for item in str(args.stages or "").split(",") if item.strip()]:
        if stage not in commands:
            payload["error"] = f"unknown_stage:{stage}"
            break
        result = run_stage(args, suite_dir, stage, commands[stage])
        payload["stages"].append(result)
        payload["post_account"] = account_fast_snapshot(args)
        payload["post_alerts"] = firing_alerts(args)
        write_summary(suite_dir, payload)
        if not result.get("ok") and not args.continue_after_stage_failure:
            payload["error"] = f"stage_failed:{stage}"
            break

    payload["post_account"] = account_fast_snapshot(args)
    payload["post_alerts"] = firing_alerts(args)
    requested = [item.strip() for item in str(args.stages or "").split(",") if item.strip()]
    payload["ok"] = bool(requested) and len(payload["stages"]) == len(requested) and all(item.get("ok") for item in payload["stages"])
    if payload["ok"]:
        account = payload.get("post_account") if isinstance(payload.get("post_account"), dict) else {}
        payload["ok"] = (
            int(account.get("open_orders") or 0) == 0
            and int(account.get("pb_fallback_order_count") or 0) == 0
            and int(account.get("active_order_command_count") or 0) == 0
        )
        if not payload["ok"]:
            payload["error"] = "post_account_not_clean"
    payload["artifact"] = write_summary(suite_dir, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full paper Gateway pressure validation suite with readiness gating.")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:15112")
    parser.add_argument("--account-base-url", default="http://127.0.0.1:15101")
    parser.add_argument("--account-snapshot-path", default="/ibkr/account")
    parser.add_argument("--prometheus-url", default=order_probe.DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--run-id", default=now_run_id())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--stages", default="20_open_modify_exit,over_bp,45_open_pressure")
    parser.add_argument("--wait-for-ready", action="store_true")
    parser.add_argument("--readiness-timeout-sec", type=float, default=0.0, help="0 means wait forever when --wait-for-ready is set.")
    parser.add_argument("--readiness-poll-sec", type=float, default=30.0)
    parser.add_argument("--max-readiness-attempts-in-summary", type=int, default=20)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--continue-after-stage-failure", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="text")

    parser.add_argument("--http-timeout-sec", type=float, default=900.0)
    parser.add_argument("--account-timeout-sec", type=float, default=20.0)
    parser.add_argument("--submit-timeout-sec", type=float, default=900.0)
    parser.add_argument("--cleanup-timeout-sec", type=float, default=720.0)
    parser.add_argument("--cleanup-http-timeout-sec", type=float, default=60.0)
    parser.add_argument("--symbol-cleanup-timeout-sec", type=float, default=60.0)
    parser.add_argument("--stage-timeout-sec", type=float, default=3600.0)
    parser.add_argument("--stability-settle-seconds", type=float, default=180.0)
    parser.add_argument("--max-firing-alerts", type=float, default=1.0)
    parser.add_argument("--max-broker-pending-requests", type=float, default=1.0)
    parser.add_argument("--max-order-operation-p95", type=float, default=300.0)
    parser.add_argument("--max-gateway-serial-wait-p95", type=float, default=240.0)

    parser.add_argument("--open20-orders", type=int, default=20)
    parser.add_argument("--open20-notional", type=float, default=5000.0)
    parser.add_argument("--open20-pending-hold-sec", type=float, default=45.0)
    parser.add_argument("--open45-orders", type=int, default=45)
    parser.add_argument("--open45-notional", type=float, default=5000.0)
    parser.add_argument("--open45-pending-hold-sec", type=float, default=45.0)
    parser.add_argument("--over-bp-orders", type=int, default=9)
    parser.add_argument("--over-bp-notional", type=float, default=30000.0)
    parser.add_argument("--over-bp-min-visible-orders", type=int, default=21)
    parser.add_argument("--min-buying-power-blocks", type=int, default=1)
    return parser.parse_args(argv)


def print_text(payload: dict[str, Any]) -> None:
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    print(f"ok={payload.get('ok')} dry_run={payload.get('dry_run')} run_id={payload.get('run_id')} error={payload.get('error') or ''}")
    if readiness:
        last = readiness.get("last") if isinstance(readiness.get("last"), dict) else {}
        failures = ",".join(str(item.get("name")) for item in last.get("failures") or [])
        print(
            "readiness ok={ok} attempts={attempts} socket={socket} reachable={reachable} session={session} reason={reason}".format(
                ok=readiness.get("ok"),
                attempts=readiness.get("attempt_count"),
                socket=last.get("api_socket_listening"),
                reachable=last.get("gateway_reachable"),
                session=last.get("session_authenticated"),
                reason=failures,
            )
        )
    for stage in payload.get("stages") or []:
        print(f"stage={stage.get('name')} ok={stage.get('ok')} elapsed_s={stage.get('elapsed_s')} artifact={stage.get('child_artifact')}")
    print(f"artifact={payload.get('artifact')}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_suite(args)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print_text(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
