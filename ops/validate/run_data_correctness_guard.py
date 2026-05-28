#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
DEFAULT_BASE_URL = (
    os.environ.get("IBKR_DATA_CORRECTNESS_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("CONSOLE_BASE_URL")
    or "https://quant.lzw-glory.top"
)
DEFAULT_ENVIRONMENT = "live"
DEFAULT_SCAN_SCOPE = "trade_watchlist"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 600.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
SYMBOL_PRESETS: dict[str, tuple[str, ...]] = {
    "core": ("AAPL", "MSFT", "NVDA", "TSM", "AVGO", "MU", "PLTR", "INTC"),
    "expanded": (
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TSLA",
        "AVGO",
        "AMD",
        "MU",
        "TSM",
        "PLTR",
        "INTC",
    ),
}


def normalize_base_url(base_url: str) -> str:
    return (str(base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL.rstrip("/"))


def build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    clean_path = str(path or "").strip()
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    url = f"{normalize_base_url(base_url)}{clean_path}"
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
    )
    return f"{url}?{query}" if query else url


def split_symbols(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    pieces: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            pieces.extend(split_symbols(str(item or "")))
    else:
        for item in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
            symbol = item.strip().upper()
            if symbol:
                pieces.append(symbol)
    output = []
    seen = set()
    for symbol in pieces:
        if symbol in seen:
            continue
        seen.add(symbol)
        output.append(symbol)
    return output


def previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def latest_complete_market_date(now: datetime | None = None) -> str:
    current = now.astimezone(ET) if isinstance(now, datetime) else datetime.now(ET)
    candidate = current.date()
    if candidate.weekday() >= 5:
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate.isoformat()
    if current.time() < dt_time(17, 0):
        candidate = previous_weekday(candidate)
    return candidate.isoformat()


class JsonHttpClient:
    def __init__(self, base_url: str):
        self.base_url = normalize_base_url(base_url)

    def request_json(self, method: str, path: str, *, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
        method_text = str(method or "GET").upper()
        body = json.dumps(payload or {}, ensure_ascii=True).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            build_url(self.base_url, path, params),
            data=body,
            method=method_text,
            headers={
                "Accept": "application/json",
                "User-Agent": "ibkr-data-correctness-guard/1.0",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        status_code = 0
        raw = b""
        try:
            with urllib.request.urlopen(request, timeout=float(timeout or DEFAULT_TIMEOUT_SECONDS)) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code or 0)
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"request_failed:{request.full_url}:{exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"ok": False, "error": "non_json_response", "raw": text[:1000]}
        if not isinstance(data, dict):
            data = {"ok": False, "error": "non_object_json_response", "raw": data}
        data["_http_status"] = status_code
        return data

    def post_json(self, path: str, payload: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
        return self.request_json("POST", path, payload=payload, timeout=timeout)

    def get_json(self, path: str, params: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
        return self.request_json("GET", path, params=params, timeout=timeout)


def build_truth_repair_payload(args: argparse.Namespace, operation_id: str, async_mode: bool) -> dict[str, Any]:
    symbols = split_symbols(args.symbols)
    if not symbols and args.preset:
        symbols = list(SYMBOL_PRESETS.get(str(args.preset), ()))
    payload = {
        "market_data_mode": args.environment,
        "data_environment": args.environment,
        "scan_scope": args.scan_scope,
        "market_date": args.market_date or latest_complete_market_date(),
        "apply": bool(args.apply),
        "delete_extra_bars": bool(args.delete_extra_bars),
        "confirm_refetch": bool(args.confirm_refetch),
        "persist": bool(args.persist),
        "operation_id": operation_id,
    }
    if symbols:
        payload["symbols"] = symbols
    if async_mode:
        payload["async"] = True
        payload["run_id"] = operation_id
    return payload


def is_terminal(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "failed", "cancelled"}


def poll_operation(client: JsonHttpClient, *, operation_id: str, environment: str, timeout_s: float, interval_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_s or DEFAULT_POLL_SECONDS))
    last = {}
    while time.monotonic() <= deadline:
        last = client.get_json(
            "/api/custom/ibkr/data_quality/operation_status",
            {
                "operation_id": operation_id,
                "market_data_mode": environment,
                "data_environment": environment,
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if is_terminal(str(last.get("status") or "")):
            return last
        time.sleep(max(1.0, float(interval_s or DEFAULT_POLL_INTERVAL_SECONDS)))
    return {"ok": False, "status": "timeout", "operation_id": operation_id, "last": last}


def print_summary(result: dict[str, Any]) -> None:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    final_summary = payload.get("final_truth_summary") if isinstance(payload.get("final_truth_summary"), dict) else {}
    plan_summary = (payload.get("repair_plan") or {}).get("summary") if isinstance(payload.get("repair_plan"), dict) else {}
    applied_summary = payload.get("applied_summary") if isinstance(payload.get("applied_summary"), dict) else {}
    print("Data Correctness Guard")
    print(f"- Operation: {payload.get('operation_id') or result.get('operation_id')}")
    print(f"- Proof: {payload.get('proof_status')} ok={bool(payload.get('ok'))} apply={bool(payload.get('apply'))}")
    print(f"- Plan: total={plan_summary.get('total', 0)} actions={plan_summary.get('action_counts') or {}}")
    print(
        "- Applied: "
        f"items={applied_summary.get('applied_items', 0)} "
        f"upserted={applied_summary.get('upserted_bars', 0)} "
        f"deleted={applied_summary.get('deleted_bars', 0)}"
    )
    print(
        "- Final truth: "
        f"status={final_summary.get('status_counts') or {}} "
        f"blocked={payload.get('blocked_symbols') or []}"
    )
    print("- Rule: proof_status must be green before trusting indicators, backtests, or live candidates.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the repair -> truth audit -> proof gate loop.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    parser.add_argument("--market-date", default="")
    parser.add_argument("--scan-scope", default=DEFAULT_SCAN_SCOPE)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--preset", choices=sorted(SYMBOL_PRESETS), default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-extra-bars", dest="delete_extra_bars", action="store_true", default=True)
    parser.add_argument("--no-delete-extra-bars", dest="delete_extra_bars", action="store_false")
    parser.add_argument("--confirm-refetch", dest="confirm_refetch", action="store_true", default=True)
    parser.add_argument("--no-confirm-refetch", dest="confirm_refetch", action="store_false")
    parser.add_argument("--persist", dest="persist", action="store_true", default=True)
    parser.add_argument("--no-persist", dest="persist", action="store_false")
    parser.add_argument("--async", dest="async_submit", action="store_true", default=None)
    parser.add_argument("--sync", dest="async_submit", action="store_false")
    parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--soft-exit", action="store_true")
    return parser.parse_args(argv)


def write_output(path: str, result: dict[str, Any]) -> str:
    if not str(path or "").strip():
        return ""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    args.environment = str(args.environment or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT
    args.scan_scope = str(args.scan_scope or DEFAULT_SCAN_SCOPE).strip().lower() or DEFAULT_SCAN_SCOPE
    args.market_date = str(args.market_date or "").strip() or latest_complete_market_date()
    operation_id = str(args.operation_id or "").strip() or f"manual:truth_repair:{args.environment}:{args.market_date}:{int(time.time() * 1000)}"
    async_mode = bool(args.apply if args.async_submit is None else args.async_submit)
    client = JsonHttpClient(args.base_url)
    payload = build_truth_repair_payload(args, operation_id, async_mode)
    result = client.post_json("/api/custom/ibkr/data_quality/truth_repair", payload, timeout=args.timeout)
    if result.get("accepted") and result.get("async"):
        result = poll_operation(
            client,
            operation_id=str(result.get("operation_id") or operation_id),
            environment=args.environment,
            timeout_s=args.poll_timeout,
            interval_s=args.poll_interval,
        )
    output_path = write_output(args.output, result)
    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result)
        if output_path:
            print(f"- Output: {output_path}")
    ok = bool(result.get("ok"))
    if "result" in result and isinstance(result.get("result"), dict):
        ok = bool(result["result"].get("ok"))
    return 0 if ok or args.soft_exit else 2


if __name__ == "__main__":
    raise SystemExit(main())
