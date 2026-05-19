#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.246")
DEFAULT_IBKR_REMOTE_ROOT = os.environ.get("IBKR_REMOTE_ROOT", "/opt/ibkr_compute").rstrip("/")
DEFAULT_PB_REMOTE_ROOT = os.environ.get("PB_REMOTE_ROOT", "/opt/pocketbase").rstrip("/")
DEFAULT_REMOTE_PYTHON = os.environ.get("IBKR_REMOTE_PYTHON", f"{DEFAULT_IBKR_REMOTE_ROOT}/venv/bin/python")
DEFAULT_DB_PATH = os.environ.get("PB_DB_PATH", f"{DEFAULT_PB_REMOTE_ROOT}/pb_data/data.db")
DEFAULT_PB_LOCAL_URL = os.environ.get("PB_LOCAL_URL", "http://127.0.0.1:8090")
DEFAULT_COMPUTE_LOCAL_URL = os.environ.get("IBKR_COMPUTE_LOCAL_URL", "http://127.0.0.1:5100")
DEFAULT_BACKTEST_LOCAL_URL = os.environ.get("IBKR_BACKTEST_LOCAL_URL", "http://127.0.0.1:5105")
DEFAULT_RUNTIME_LOCAL_URL = os.environ.get("IBKR_RUNTIME_LOCAL_URL", "http://127.0.0.1:5101")
DEFAULT_API_LOCAL_URL = os.environ.get("IBKR_API_LOCAL_URL", "http://127.0.0.1:5102")
DEFAULT_SCHEDULER_LOCAL_URL = os.environ.get("IBKR_SCHEDULER_LOCAL_URL", "http://127.0.0.1:5103")
DEFAULT_CONSOLE_LOCAL_URL = os.environ.get("IBKR_CONSOLE_LOCAL_URL", "http://127.0.0.1:5104")
DEFAULT_PB_BASE_URL = os.environ.get("PB_BASE_URL", "https://pb.lzw-glory.top")
DEFAULT_CONSOLE_BASE_URL = os.environ.get("CONSOLE_BASE_URL") or os.environ.get("QUANT_BASE_URL") or "https://quant.lzw-glory.top"
DEFAULT_COMPUTE_PUBLIC_URL = os.environ.get("IBKR_COMPUTE_PUBLIC_URL", "").strip()
DEFAULT_API_PUBLIC_URL = os.environ.get("IBKR_API_PUBLIC_URL", DEFAULT_CONSOLE_BASE_URL)
DEFAULT_INDICATOR_MISSING_GRACE_SEC = int(os.environ.get("IBKR_INDICATOR_MISSING_GRACE_SEC", "90"))
DEFAULT_PRELOAD_WARN_SEC = int(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_WARN_SEC", "300"))
DEFAULT_PRELOAD_FAIL_SEC = int(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_FAIL_SEC", "900"))

_REMOTE_SCRIPT_PATH = Path(__file__).with_name("check_stack_remote.py")
REMOTE_SCRIPT = _REMOTE_SCRIPT_PATH.read_text(encoding="utf-8")


def extract_compute_startup_preload(*payloads) -> dict:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        preload = payload.get("compute_startup_preload")
        if isinstance(preload, dict) and preload:
            return preload
        nested_compute = payload.get("compute")
        if isinstance(nested_compute, dict):
            preload = nested_compute.get("compute_startup_preload")
            if isinstance(preload, dict) and preload:
                return preload
    return {}


def compact_compute_startup_preload(payload) -> dict:
    source = payload if isinstance(payload, dict) else {}
    return {
        "status": str(source.get("status") or "").strip().lower() or "idle",
        "running": bool(source.get("running")),
        "env_completed": int(source.get("env_completed") or 0),
        "env_total": int(source.get("env_total") or 0),
        "symbol_completed": int(source.get("symbol_completed") or 0),
        "symbol_total": int(source.get("symbol_total") or 0),
        "ready_count": int(source.get("ready_count") or 0),
        "elapsed_s": round(float(source.get("elapsed_s") or 0.0), 3),
        "started_at": source.get("started_at"),
        "finished_at": source.get("finished_at"),
        "environments": list(source.get("environments") or []),
    }


def evaluate_compute_startup_preload_sla(preload_payload, warn_sec, fail_sec) -> dict:
    preload = compact_compute_startup_preload(preload_payload)
    status = str(preload.get("status") or "").strip().lower() or "idle"
    elapsed_s = round(float(preload.get("elapsed_s") or 0.0), 3)
    warn_sec = max(0, int(warn_sec or 0))
    fail_sec = max(0, int(fail_sec or 0))
    level = "ok"
    reason = "none"
    if status == "failed":
        level = "fail"
        reason = "failed"
    elif status in {"running", "scheduled"}:
        level = "info"
        reason = "in_progress"
        if fail_sec > 0 and elapsed_s >= fail_sec:
            level = "fail"
            reason = "timeout"
        elif warn_sec > 0 and elapsed_s >= warn_sec:
            level = "warn"
            reason = "slow"
    return {
        "level": level,
        "reason": reason,
        "status": status,
        "elapsed_s": elapsed_s,
        "warn_sec": warn_sec,
        "fail_sec": fail_sec,
    }


def format_compute_startup_preload_issue(prefix: str, preload_payload, sla_payload) -> str:
    preload = compact_compute_startup_preload(preload_payload)
    sla = sla_payload if isinstance(sla_payload, dict) else {}
    parts = [
        prefix,
        f"status={preload.get('status') or 'idle'}",
        f"symbols={int(preload.get('symbol_completed') or 0)}/{int(preload.get('symbol_total') or 0)}",
        f"elapsed_s={round(float(preload.get('elapsed_s') or 0.0), 2)}",
    ]
    reason = str(sla.get("reason") or "").strip().lower()
    if reason == "slow" and int(sla.get("warn_sec") or 0) > 0:
        parts.append(f"warn_sec={int(sla.get('warn_sec') or 0)}")
    elif reason == "timeout" and int(sla.get("fail_sec") or 0) > 0:
        parts.append(f"fail_sec={int(sla.get('fail_sec') or 0)}")
    return ":".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a one-click health check for the IBKR + PocketBase stack.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--pb-local-url", default=DEFAULT_PB_LOCAL_URL)
    parser.add_argument("--compute-local-url", default=DEFAULT_COMPUTE_LOCAL_URL)
    parser.add_argument("--backtest-local-url", default=DEFAULT_BACKTEST_LOCAL_URL)
    parser.add_argument("--runtime-local-url", default=DEFAULT_RUNTIME_LOCAL_URL)
    parser.add_argument("--api-local-url", default=DEFAULT_API_LOCAL_URL)
    parser.add_argument("--scheduler-local-url", default=DEFAULT_SCHEDULER_LOCAL_URL)
    parser.add_argument("--console-local-url", default=DEFAULT_CONSOLE_LOCAL_URL)
    parser.add_argument("--pb-base-url", default=DEFAULT_PB_BASE_URL)
    parser.add_argument("--console-base-url", default=DEFAULT_CONSOLE_BASE_URL)
    parser.add_argument("--compute-public-url", default=DEFAULT_COMPUTE_PUBLIC_URL)
    parser.add_argument("--api-public-url", default=DEFAULT_API_PUBLIC_URL)
    parser.add_argument("--environment", default="live")
    parser.add_argument("--bar-stale-min", type=int, default=30)
    parser.add_argument("--indicator-stale-min", type=int, default=30)
    parser.add_argument("--indicator-missing-grace-sec", type=int, default=DEFAULT_INDICATOR_MISSING_GRACE_SEC)
    parser.add_argument("--preload-warn-sec", type=int, default=DEFAULT_PRELOAD_WARN_SEC)
    parser.add_argument("--preload-fail-sec", type=int, default=DEFAULT_PRELOAD_FAIL_SEC)
    parser.add_argument("--strict-runtime", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.preload_warn_sec < 0:
        parser.error("--preload-warn-sec must be >= 0")
    if args.preload_fail_sec < 0:
        parser.error("--preload-fail-sec must be >= 0")
    if args.preload_warn_sec and args.preload_fail_sec and args.preload_fail_sec < args.preload_warn_sec:
        parser.error("--preload-fail-sec must be >= --preload-warn-sec when both are set")
    return args


def _decode_json_payload(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def run_remote(args: argparse.Namespace) -> dict:
    config = {
        "db_path": args.db_path,
        "pb_local_url": args.pb_local_url,
        "compute_local_url": args.compute_local_url,
        "backtest_local_url": args.backtest_local_url,
        "runtime_local_url": args.runtime_local_url,
        "api_local_url": args.api_local_url,
        "scheduler_local_url": args.scheduler_local_url,
        "console_local_url": args.console_local_url,
        "environment": args.environment,
        "bar_stale_min": args.bar_stale_min,
        "indicator_stale_min": args.indicator_stale_min,
        "indicator_missing_grace_sec": args.indicator_missing_grace_sec,
        "preload_warn_sec": args.preload_warn_sec,
        "preload_fail_sec": args.preload_fail_sec,
        "strict_runtime": args.strict_runtime,
    }
    env_blob = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        args.host,
        "env",
        f"HEALTH_CHECK_CONFIG_B64={env_blob}",
        args.remote_python,
        "-",
    ]
    proc = subprocess.run(cmd, input=REMOTE_SCRIPT, text=True, capture_output=True, timeout=90)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or f"remote command failed with code {proc.returncode}")
    try:
        payload = _decode_json_payload(proc.stdout)
    except Exception as exc:
        raise RuntimeError(
            f"failed to parse remote output: {exc}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        ) from exc
    payload.setdefault("ssh_returncode", proc.returncode)
    if proc.stderr.strip():
        payload["ssh_stderr"] = proc.stderr.strip()
    return payload


def fetch_url(url: str, expect_json: bool = False, timeout: int = 10) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "codex-ibkr-stack-health-check"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            final_url = getattr(response, "geturl", lambda: url)()
            result = {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "url": url,
                "final_url": final_url,
                "redirected": final_url != url,
            }
            content_type = response.headers.get("Content-Type", "")
            if expect_json or "json" in content_type:
                try:
                    result["json"] = json.loads(body)
                except json.JSONDecodeError:
                    result["body_snippet"] = body[:300]
            else:
                result["body_snippet"] = body[:300]
            return result
    except urllib.error.HTTPError as exc:
        body = exc.read(65536).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": exc.code,
            "url": url,
            "final_url": getattr(exc, "geturl", lambda: url)(),
            "body_snippet": body[:300],
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def fetch_first_ok(urls: list[str], *, expect_json: bool = False, timeout: int = 10) -> dict:
    attempts = []
    seen = set()
    first_result = None
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result = fetch_url(url, expect_json=expect_json, timeout=timeout)
        attempts.append(
            {
                "url": url,
                "ok": bool(result.get("ok")),
                "status_code": int(result.get("status_code") or 0),
                "error": str(result.get("error") or ""),
            }
        )
        if first_result is None:
            first_result = result
        if result.get("ok"):
            result["attempts"] = attempts
            if url != urls[0]:
                result["fallback_used"] = True
                result["fallback_from"] = urls[0]
            return result
    if first_result is None:
        return {"ok": False, "url": "", "error": "no_public_urls"}
    first_result["attempts"] = attempts
    return first_result


def extract_service_statuses(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    candidates = []
    service_monitor = payload.get("service_monitor")
    if isinstance(service_monitor, dict):
        candidates.append(service_monitor)
    topology = payload.get("service_topology")
    if isinstance(topology, dict):
        candidates.append(topology)
    if isinstance(payload.get("services"), dict):
        candidates.append(payload)

    for candidate in candidates:
        services = candidate.get("services") if isinstance(candidate, dict) else {}
        if not isinstance(services, dict):
            continue
        statuses = {}
        for name, item in services.items():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status:
                statuses[str(name)] = status
        if statuses:
            return statuses
    return {}


def find_service_status_mismatches(named_payloads: dict[str, dict]) -> list[str]:
    by_service: dict[str, dict[str, str]] = {}
    for source_name, payload in named_payloads.items():
        for service_name, status in extract_service_statuses(payload).items():
            if status == "unknown":
                continue
            by_service.setdefault(service_name, {})[source_name] = status

    mismatches = []
    for service_name, source_statuses in sorted(by_service.items()):
        unique_statuses = sorted(set(source_statuses.values()))
        if len(unique_statuses) <= 1:
            continue
        detail = ",".join(f"{source}={status}" for source, status in sorted(source_statuses.items()))
        mismatches.append(f"{service_name}:{detail}")
    return mismatches


def add_public_checks(payload: dict, args: argparse.Namespace) -> dict:
    pb_base_url = args.pb_base_url.rstrip("/")
    api_public_url = args.api_public_url.rstrip("/")

    compute_public_url = (args.compute_public_url or "").rstrip("/")
    public = {
        "pb_api_health": fetch_url(f"{pb_base_url}/api/health", expect_json=True),
        "pb_root_page": fetch_url(f"{pb_base_url}/"),
        "api_public_health": fetch_url(f"{api_public_url}/health", expect_json=True),
        "api_public_status": fetch_url(f"{api_public_url}/status?lite=1", expect_json=True),
        "console_home_page": fetch_url(
            f"{args.console_base_url.rstrip('/')}/index.html?environment={args.environment}"
        ),
        "console_runtime_page": fetch_url(
            f"{args.console_base_url.rstrip('/')}/ibkr_runtime.html?environment={args.environment}"
        ),
        "console_system_page": fetch_url(
            f"{args.console_base_url.rstrip('/')}/ibkr_system.html?environment={args.environment}"
        ),
        "api_ibkr_statusz": fetch_url(
            f"{api_public_url}/api/custom/ibkr/statusz?environment={args.environment}",
            expect_json=True,
        ),
        "api_system_summaryz": fetch_url(
            f"{api_public_url}/api/custom/system/summaryz?lite=1&environment={args.environment}",
            expect_json=True,
        ),
        "api_system_monitorz": fetch_url(
            f"{api_public_url}/api/custom/system/monitorz?lite=1&environment={args.environment}",
            expect_json=True,
        ),
        "compute_public_health": (
            fetch_url(f"{compute_public_url}/health", expect_json=True)
            if compute_public_url
            else {"ok": True, "skipped": True, "reason": "compute_public_url_not_configured"}
        ),
    }

    payload["public"] = public
    failures = payload.setdefault("failures", [])
    warnings = payload.setdefault("warnings", [])

    for name in (
        "pb_api_health",
        "pb_root_page",
        "api_public_health",
        "api_public_status",
        "api_ibkr_statusz",
        "api_system_summaryz",
        "api_system_monitorz",
    ):
        if not public[name].get("ok"):
            failures.append(f"public:{name}")
    pb_root_final_url = str(public.get("pb_root_page", {}).get("final_url") or "")
    if pb_root_final_url and not pb_root_final_url.startswith(pb_base_url):
        failures.append("public:pb_root_page:redirected_off_origin")
    if not public["compute_public_health"].get("ok") and not public["compute_public_health"].get("skipped"):
        failures.append("public:compute_public_health")

    for name in ("console_home_page", "console_runtime_page", "console_system_page"):
        if not public[name].get("ok"):
            failures.append(f"public:{name}")
        else:
            body_snippet = (public[name].get("body_snippet") or "").lower()
            if "html" not in body_snippet:
                continue
            if name == "console_home_page":
                if "ibkr" not in body_snippet and "trading flight" not in body_snippet and "split stack" not in body_snippet:
                    warnings.append(f"public:{name}:marker_weak")
            elif "ibkr" not in body_snippet:
                warnings.append(f"public:{name}:marker_weak")

    topology_payload = (
        (public.get("api_public_status", {}).get("json") or {}).get("service_topology")
        or (public.get("api_system_summaryz", {}).get("json") or {}).get("service_topology")
        or (public.get("api_ibkr_statusz", {}).get("json") or {}).get("service_topology")
        or {}
    )
    public_compute_startup_preload = extract_compute_startup_preload(
        (public.get("api_system_summaryz", {}).get("json") or {}).get("ibkr_compute") or {},
        public.get("api_ibkr_statusz", {}).get("json") or {},
        {},
        public.get("compute_public_health", {}).get("json") or {},
    )
    public_compute_startup_preload_sla = evaluate_compute_startup_preload_sla(
        public_compute_startup_preload,
        args.preload_warn_sec,
        args.preload_fail_sec,
    )
    payload.setdefault("public_summary", {})
    payload["public_summary"]["compute_startup_preload"] = compact_compute_startup_preload(public_compute_startup_preload)
    payload["public_summary"]["compute_startup_preload_sla"] = public_compute_startup_preload_sla
    services = topology_payload.get("services") or {}
    if "ibkr-runtime" not in services:
        failures.append("public:topology:ibkr_runtime_missing")
    if "ibkr-compute" not in services:
        failures.append("public:topology:ibkr_compute_missing")
    if "ibkr-backtest" not in services:
        failures.append("public:topology:ibkr_backtest_missing")
    if "ibkr-api" not in services:
        failures.append("public:topology:ibkr_api_missing")
    if "ibkr-scheduler" not in services:
        failures.append("public:topology:ibkr_scheduler_missing")
    if "ibkr-console" not in services:
        failures.append("public:topology:ibkr_console_missing")

    status_mismatches = find_service_status_mismatches(
        {
            "statusz": public.get("api_ibkr_statusz", {}).get("json") or {},
            "summaryz": public.get("api_system_summaryz", {}).get("json") or {},
            "monitorz": public.get("api_system_monitorz", {}).get("json") or {},
        }
    )
    for mismatch in status_mismatches:
        failures.append(f"public:service_status_mismatch:{mismatch}")

    public_preload_reason = str(public_compute_startup_preload_sla.get("reason") or "").strip().lower()
    if public_preload_reason == "failed":
        failures.append("public:compute:startup_preload_failed")
    elif public_preload_reason in {"slow", "timeout"}:
        warnings.append(
            format_compute_startup_preload_issue(
                f"public:compute:startup_preload_{public_preload_reason}",
                public_compute_startup_preload,
                public_compute_startup_preload_sla,
            )
        )

    payload["ok"] = len(failures) == 0
    return payload


def _status_text(ok_value) -> str:
    return "OK" if ok_value else "FAIL"


def _safe_dict(payload) -> dict:
    return payload if isinstance(payload, dict) else {}


def _format_health_issue(issue) -> str:
    text = str(issue or "").strip()
    if not text:
        return "--"
    if text == "db:latest_indicator_5m_missing":
        return "latest 5m indicator missing [db:latest_indicator_5m_missing]"
    if text.startswith("db:latest_indicator_5m_pending:"):
        remaining = text.split(":", 2)[-1]
        return f"latest 5m indicator pending within grace ({remaining}) [{text}]"
    if text.startswith("db:latest_indicator_5m_stale:"):
        age = text.split(":", 2)[-1]
        return f"latest 5m indicator not caught up (stale {age}m) [{text}]"
    if text == "db:latest_bar_5m_missing":
        return "latest 5m bar missing [db:latest_bar_5m_missing]"
    if text.startswith("db:latest_bar_5m_stale:"):
        age = text.split(":", 2)[-1]
        return f"latest 5m bar stale ({age}m) [{text}]"
    return text


def _format_health_issue_list(items) -> str:
    return "; ".join(_format_health_issue(item) for item in (items or []))


def _service_summary_line(name: str, payload) -> str:
    service = _safe_dict(payload)
    state = service.get("active_state") or service.get("status") or "unknown"
    pid = service.get("main_pid") or service.get("pid") or "-"
    return f"{name}={state} pid={pid}"


def _build_preload_summary_line(preload_payload, sla_payload=None) -> str:
    preload = compact_compute_startup_preload(preload_payload)
    status = str(preload.get("status") or "idle").upper()
    symbol_total = int(preload.get("symbol_total") or 0)
    symbol_completed = int(preload.get("symbol_completed") or 0)
    ready_count = int(preload.get("ready_count") or 0)
    env_total = int(preload.get("env_total") or 0)
    env_completed = int(preload.get("env_completed") or 0)
    parts = [status]
    if env_total > 0:
        parts.append(f"env {env_completed}/{env_total}")
    if symbol_total > 0:
        parts.append(f"symbols {symbol_completed}/{symbol_total}")
    if ready_count > 0 or status == "COMPLETED":
        parts.append(f"ready {ready_count}/{symbol_total or 0}")
    elapsed_s = float(preload.get("elapsed_s") or 0.0)
    if elapsed_s > 0:
        parts.append(f"elapsed {round(elapsed_s, 1)}s")
    sla = _safe_dict(sla_payload)
    reason = str(sla.get("reason") or "").strip().lower()
    if reason == "in_progress":
        parts.append("in_sla")
    elif reason == "slow":
        warn_sec = int(sla.get("warn_sec") or 0)
        parts.append(f"slow>{warn_sec}s" if warn_sec > 0 else "slow")
    elif reason == "timeout":
        fail_sec = int(sla.get("fail_sec") or 0)
        parts.append(f"timeout>{fail_sec}s" if fail_sec > 0 else "timeout")
    return " · ".join(parts)


def _build_runtime_summary_line(payload: dict) -> str:
    runtime_summary = _safe_dict((_safe_dict(payload.get("remote")).get("runtime_summary")))
    gateway = _safe_dict(runtime_summary.get("gateway"))
    session = _safe_dict(runtime_summary.get("session"))
    websocket = _safe_dict(runtime_summary.get("websocket"))
    parts = [
        f"gateway={'up' if gateway.get('running') and gateway.get('reachable') else 'down'}",
        f"session={'auth' if session.get('authenticated') else 'wait'}",
        f"websocket={'ready' if websocket.get('ready') else 'not_ready'}",
    ]
    return " · ".join(parts)


def _build_data_summary_line(payload: dict) -> str:
    remote_db = _safe_dict((_safe_dict(payload.get("remote")).get("db")))
    latest_bar = _safe_dict(remote_db.get("latest_bar_5m"))
    latest_indicator = _safe_dict(remote_db.get("latest_indicator_5m"))
    latest_signal = _safe_dict(remote_db.get("latest_signal"))
    parts = []
    if latest_bar:
        parts.append(
            f"bar5m {latest_bar.get('symbol') or '--'} age={latest_bar.get('age_min') if latest_bar.get('age_min') is not None else '--'}m"
        )
    if latest_indicator:
        parts.append(
            f"ind5m {latest_indicator.get('symbol') or '--'} age={latest_indicator.get('age_min') if latest_indicator.get('age_min') is not None else '--'}m"
        )
    elif latest_bar:
        parts.append("ind5m missing (latest bar present)")
    if latest_signal:
        parts.append(f"signal {latest_signal.get('symbol') or '--'}")
    return " · ".join(parts) if parts else "--"


def _build_public_summary_line(payload: dict) -> str:
    public = _safe_dict(payload.get("public"))
    api_health = _safe_dict(public.get("api_public_health"))
    statusz = _safe_dict(public.get("api_ibkr_statusz"))
    summaryz = _safe_dict(public.get("api_system_summaryz"))
    console_home = _safe_dict(public.get("console_home_page"))
    runtime_page = _safe_dict(public.get("console_runtime_page"))
    system_page = _safe_dict(public.get("console_system_page"))
    return " · ".join(
        [
            f"api_health={_status_text(api_health.get('ok'))}",
            f"statusz={_status_text(statusz.get('ok'))}",
            f"summaryz={_status_text(summaryz.get('ok'))}",
            f"console_home={_status_text(console_home.get('ok'))}",
            f"runtime_page={_status_text(runtime_page.get('ok'))}",
            f"system_page={_status_text(system_page.get('ok'))}",
        ]
    )


def _build_thresholds_summary_line(payload: dict) -> str:
    thresholds = _safe_dict(payload.get("thresholds"))
    return " · ".join(
        [
            f"bar<={thresholds.get('bar_stale_min') or '--'}m",
            f"indicator<={thresholds.get('indicator_stale_min') or '--'}m",
            f"indicator_grace={thresholds.get('indicator_missing_grace_sec') or '--'}s",
            f"preload warn={thresholds.get('preload_warn_sec') or 0}s",
            f"fail={thresholds.get('preload_fail_sec') or 0}s",
        ]
    )


def print_human_summary(payload: dict) -> None:
    services = _safe_dict((_safe_dict(payload.get("remote")).get("services")))
    runtime_summary = _safe_dict((_safe_dict(payload.get("remote")).get("runtime_summary")))
    public_summary = _safe_dict(payload.get("public_summary"))
    lines = [
        "IBKR Stack Health",
        f"- Result: {_status_text(payload.get('ok'))}",
        f"- Environment: {payload.get('environment') or '--'}",
        "- Services: "
        + " | ".join(
            [
                _service_summary_line("runtime", services.get("ibkr-runtime")),
                _service_summary_line("gateway", services.get("ibkr-gateway")),
                _service_summary_line("compute", services.get("ibkr-compute")),
                _service_summary_line("api", services.get("ibkr-api")),
                _service_summary_line("scheduler", services.get("ibkr-scheduler")),
                _service_summary_line("console", services.get("ibkr-console")),
                _service_summary_line("pocketbase", services.get("pocketbase")),
            ]
        ),
        f"- Thresholds: {_build_thresholds_summary_line(payload)}",
        f"- Runtime: {_build_runtime_summary_line(payload)}",
        f"- Compute Preload: {_build_preload_summary_line(runtime_summary.get('compute_startup_preload'), runtime_summary.get('compute_startup_preload_sla'))}",
        f"- Public Preload: {_build_preload_summary_line(public_summary.get('compute_startup_preload'), public_summary.get('compute_startup_preload_sla'))}",
        f"- Data: {_build_data_summary_line(payload)}",
        f"- Public: {_build_public_summary_line(payload)}",
    ]

    warnings = list(payload.get("warnings") or [])
    failures = list(payload.get("failures") or [])
    if warnings:
        lines.append(f"- Warnings: {_format_health_issue_list(warnings)}")
    if failures:
        lines.append(f"- Failures: {_format_health_issue_list(failures)}")
    print("\n".join(lines))


def main() -> int:
    args = parse_args()
    try:
        payload = run_remote(args)
        payload = add_public_checks(payload, args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0 if payload.get("ok") else 1

    print_human_summary(payload)
    if payload.get("ok"):
        print("\nHealth Check OK: core services, endpoints, and freshness checks passed.")
        return 0
    print("\nHealth Check FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
