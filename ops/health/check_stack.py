#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.136")
DEFAULT_IBKR_REMOTE_ROOT = os.environ.get("IBKR_REMOTE_ROOT", "/opt/ibkr_compute").rstrip("/")
DEFAULT_PB_REMOTE_ROOT = os.environ.get("PB_REMOTE_ROOT", "/opt/pocketbase").rstrip("/")
DEFAULT_REMOTE_PYTHON = os.environ.get("IBKR_REMOTE_PYTHON", f"{DEFAULT_IBKR_REMOTE_ROOT}/venv/bin/python")
DEFAULT_DB_PATH = os.environ.get("PB_DB_PATH", f"{DEFAULT_PB_REMOTE_ROOT}/pb_data/data.db")
DEFAULT_PB_LOCAL_URL = os.environ.get("PB_LOCAL_URL", "http://127.0.0.1:8090")
DEFAULT_COMPUTE_LOCAL_URL = os.environ.get("IBKR_COMPUTE_LOCAL_URL", "http://127.0.0.1:5100")
DEFAULT_RUNTIME_LOCAL_URL = os.environ.get("IBKR_RUNTIME_LOCAL_URL", "http://127.0.0.1:5101")
DEFAULT_PB_BASE_URL = os.environ.get("PB_BASE_URL", "https://pb.lzw-glory.top")
DEFAULT_COMPUTE_PUBLIC_URL = os.environ.get("IBKR_COMPUTE_PUBLIC_URL", "http://206.119.171.136:5100")
DEFAULT_INDICATOR_MISSING_GRACE_SEC = int(os.environ.get("IBKR_INDICATOR_MISSING_GRACE_SEC", "90"))

REMOTE_SCRIPT = r'''
import base64
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

cfg = json.loads(base64.b64decode(os.environ["HEALTH_CHECK_CONFIG_B64"]).decode("utf-8"))
DB = cfg["db_path"]
PB = cfg["pb_local_url"].rstrip("/")
COMPUTE = cfg["compute_local_url"].rstrip("/")
RUNTIME = cfg["runtime_local_url"].rstrip("/")
ENVIRONMENT = cfg["environment"]
BAR_STALE_MIN = int(cfg["bar_stale_min"])
INDICATOR_STALE_MIN = int(cfg["indicator_stale_min"])
INDICATOR_MISSING_GRACE_SEC = int(cfg.get("indicator_missing_grace_sec") or 90)
STRICT_RUNTIME = bool(cfg["strict_runtime"])


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def age_minutes(ms):
    if not ms:
        return None
    return round((now_ms() - int(ms)) / 60000.0, 2)


def format_us(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000.0, ET).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso_ms(text):
    value = str(text or "").strip()
    if not value:
        return 0
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except Exception:
        return 0


def normalize_interval_label(value):
    text = str(value or "").strip().lower()
    mapping = {
        "1": "1m",
        "1m": "1m",
        "5": "5m",
        "5m": "5m",
        "15": "15m",
        "15m": "15m",
        "30": "30m",
        "30m": "30m",
        "60": "1h",
        "1h": "1h",
        "240": "4h",
        "4h": "4h",
        "d": "1d",
        "1d": "1d",
    }
    return mapping.get(text, text)


def interval_sort_key(value):
    order = {
        "1m": 1,
        "5m": 2,
        "15m": 3,
        "30m": 4,
        "1h": 5,
        "4h": 6,
        "1d": 7,
    }
    return order.get(normalize_interval_label(value), 999)


def normalize_bar_bucket_lag_seconds(lag_value, due_bucket_ms, completed_bucket_ms):
    due_ms = int(due_bucket_ms or 0)
    completed_ms = int(completed_bucket_ms or 0)
    if due_ms > 0:
        reference_ms = completed_ms if completed_ms > 0 and completed_ms >= due_ms else now_ms()
        return round(max(0, reference_ms - due_ms) / 1000.0, 2)
    raw_lag = float(lag_value or 0)
    if raw_lag > 1000000000000:
        return round(max(0, now_ms() - raw_lag) / 1000.0, 2)
    if raw_lag > 1000000000:
        return round(max(0, now_ms() / 1000.0 - raw_lag), 2)
    return round(raw_lag, 2)


def open_db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def parse_show(text: str) -> dict:
    data = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def run_cmd(args):
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=20)


def systemd_status(service: str) -> dict:
    proc = run_cmd([
        "systemctl",
        "show",
        service,
        "--property=Id,ActiveState,SubState,MainPID,ActiveEnterTimestamp,UnitFileState",
        "--no-pager",
    ])
    data = parse_show(proc.stdout)
    return {
        "service": service,
        "ok": data.get("ActiveState") == "active",
        "active_state": data.get("ActiveState") or "unknown",
        "sub_state": data.get("SubState") or "unknown",
        "main_pid": data.get("MainPID") or "",
        "active_since": data.get("ActiveEnterTimestamp") or "",
        "unit_file_state": data.get("UnitFileState") or "",
        "stderr": proc.stderr.strip(),
    }


def http_json(url: str, timeout: int = 8) -> dict:
    try:
        resp = requests.get(url, timeout=timeout)
        result = {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "url": url,
        }
        try:
            result["json"] = resp.json()
        except Exception:
            result["text_snippet"] = (resp.text or "")[:300]
        return result
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def row_dict(row):
    return dict(row) if row is not None else None


def apply_interval_label(row):
    item = row_dict(row) if not isinstance(row, dict) else dict(row)
    if item is None:
        return None
    raw_interval = str(item.get("interval") or "").strip()
    if raw_interval:
        item["raw_interval"] = raw_interval
        item["interval"] = normalize_interval_label(raw_interval)
    return item


def latest_row(conn, table: str, environment: str, where_sql: str = "", params=(), indexed_by: str | None = None):
    table_ref = table if not indexed_by else f"{table} indexed by {indexed_by}"
    sql = f"select * from {table_ref} where environment=?"
    sql_params = [environment]
    if where_sql:
        sql += f" and {where_sql}"
        sql_params.extend(params)
    sql += " order by bar_time_ms desc, created desc limit 1"
    try:
        row = conn.execute(sql, tuple(sql_params)).fetchone()
    except sqlite3.OperationalError as exc:
        if indexed_by and "no such index" in str(exc).lower():
            return latest_row(conn, table, environment, where_sql, params)
        raise
    return apply_interval_label(row)


def latest_signal_row(conn, environment: str):
    row = conn.execute(
        """
        select *
        from ibkr_signals
        where environment=?
        order by created desc, bar_time_ms desc
        limit 1
        """,
        (environment,),
    ).fetchone()
    return apply_interval_label(row)


def interval_freshness(conn, table: str, environment: str) -> list[dict]:
    rows = conn.execute(
        f"""
        select interval, max(bar_time_ms) as last_ms, count(*) as row_count
        from {table}
        where environment=?
        group by interval
        """,
        (environment,),
    ).fetchall()
    by_interval = {}
    for row in rows:
        raw_interval = str(row["interval"] or "").strip()
        normalized_interval = normalize_interval_label(raw_interval)
        if not normalized_interval:
            continue
        last_ms = int(row["last_ms"] or 0)
        row_count = int(row["row_count"] or 0)
        item = by_interval.get(normalized_interval)
        if item is None:
            by_interval[normalized_interval] = {
                "interval": normalized_interval,
                "last_ms": last_ms,
                "last_us": format_us(last_ms),
                "age_min": age_minutes(last_ms),
                "row_count": row_count,
                "raw_intervals": [raw_interval] if raw_interval else [],
            }
            continue
        item["row_count"] += row_count
        if last_ms > int(item.get("last_ms") or 0):
            item["last_ms"] = last_ms
            item["last_us"] = format_us(last_ms)
            item["age_min"] = age_minutes(last_ms)
        if raw_interval and raw_interval not in item["raw_intervals"]:
            item["raw_intervals"].append(raw_interval)
    items = list(by_interval.values())
    items.sort(key=lambda item: interval_sort_key(item.get("interval")))
    return items


def latest_targets(conn, environment: str) -> dict:
    row = conn.execute(
        """
        select date as target_date,
               count(*) as total_count,
               sum(case when status='candidate' then 1 else 0 end) as candidate_count,
               sum(case when status='active' then 1 else 0 end) as active_count,
               sum(case when status in ('candidate', 'active') then 1 else 0 end) as eligible_count
        from ibkr_targets
        where environment=?
          and date=(select max(date) from ibkr_targets where environment=?)
        group by date
        limit 1
        """,
        (environment, environment),
    ).fetchone()
    if row is None:
        return {
            "target_date": None,
            "total_count": 0,
            "candidate_count": 0,
            "active_count": 0,
            "eligible_count": 0,
        }
    return {
        "target_date": row["target_date"],
        "total_count": int(row["total_count"] or 0),
        "candidate_count": int(row["candidate_count"] or 0),
        "active_count": int(row["active_count"] or 0),
        "eligible_count": int(row["eligible_count"] or 0),
    }


def classify_data_freshness_window(latest_bar_5m: dict | None, targets: dict | None) -> dict:
    now_et = datetime.now(ET)
    minute_of_day = now_et.hour * 60 + now_et.minute
    latest_bar_us = str((latest_bar_5m or {}).get("bar_time_us") or "").strip()
    latest_bar_date = latest_bar_us[:10] if latest_bar_us else ""
    target_date = str((targets or {}).get("target_date") or "").strip()
    has_trading_context = bool(
        int((targets or {}).get("total_count") or 0) > 0
        or int((targets or {}).get("active_count") or 0) > 0
        or latest_bar_date == now_et.strftime("%Y-%m-%d")
    )

    if now_et.weekday() >= 5:
        return {
            "required": False,
            "reason": "weekend",
            "label": "weekend_closed",
        }
    if minute_of_day < (9 * 60 + 40):
        return {
            "required": False,
            "reason": "pre_open",
            "label": "pre_open_grace",
            "target_date": target_date,
            "latest_bar_date": latest_bar_date,
        }
    if minute_of_day > (16 * 60 + 15):
        return {
            "required": False,
            "reason": "post_close",
            "label": "post_close_grace",
            "target_date": target_date,
            "latest_bar_date": latest_bar_date,
        }
    if not has_trading_context:
        return {
            "required": False,
            "reason": "no_trading_context",
            "label": "no_trading_context",
            "target_date": target_date,
            "latest_bar_date": latest_bar_date,
        }
    return {
        "required": True,
        "reason": "regular_session",
        "label": "regular_session",
        "target_date": target_date,
        "latest_bar_date": latest_bar_date,
    }


def latest_indicator_missing_grace(latest_bar_5m: dict | None, latest_indicator_5m: dict | None, required: bool) -> dict:
    if not required or latest_indicator_5m is not None or latest_bar_5m is None:
        return {"active": False, "elapsed_sec": 0, "remaining_sec": 0}
    created_ms = parse_iso_ms(latest_bar_5m.get("created") or latest_bar_5m.get("updated"))
    if created_ms <= 0:
        return {"active": False, "elapsed_sec": 0, "remaining_sec": 0}
    elapsed_ms = max(0, now_ms() - created_ms)
    remaining_ms = max(0, INDICATOR_MISSING_GRACE_SEC * 1000 - elapsed_ms)
    return {
        "active": remaining_ms > 0,
        "elapsed_sec": round(elapsed_ms / 1000.0, 2),
        "remaining_sec": round(remaining_ms / 1000.0, 2),
    }


services = {
    name: systemd_status(name)
    for name in ("ibkr-runtime", "ibkr-gateway", "ibkr-compute", "pocketbase")
}
local_http = {
    "compute_health": http_json(f"{COMPUTE}/health"),
    "compute_status": http_json(f"{COMPUTE}/status"),
    "compute_ibkr_status": http_json(f"{COMPUTE}/ibkr/status"),
    "runtime_health": http_json(f"{RUNTIME}/health"),
    "runtime_status": http_json(f"{RUNTIME}/ibkr/status"),
    "pb_health": http_json(f"{PB}/api/health"),
    "pb_ibkr_healthz": http_json(f"{PB}/api/custom/ibkr/healthz?environment={ENVIRONMENT}"),
    "pb_ibkr_statusz": http_json(f"{PB}/api/custom/ibkr/statusz?environment={ENVIRONMENT}"),
    "pb_system_healthz": http_json(f"{PB}/api/custom/system/healthz?environment={ENVIRONMENT}"),
    "pb_2fa_status": http_json(f"{PB}/api/custom/ibkr/2fa/status?environment={ENVIRONMENT}"),
}

conn = open_db()
latest_bar_5m = latest_row(conn, "ibkr_bars", ENVIRONMENT, "interval=?", ("5m",))
latest_indicator_5m = latest_row(
    conn,
    "ibkr_indicators",
    ENVIRONMENT,
    "interval in (?, ?)",
    ("5", "5m"),
    indexed_by="idx_ibkr_indicators_bartimems",
)
latest_signal = latest_signal_row(conn, ENVIRONMENT)
targets = latest_targets(conn, ENVIRONMENT)
bars_by_interval = interval_freshness(conn, "ibkr_bars", ENVIRONMENT)
indicators_by_interval = interval_freshness(conn, "ibkr_indicators", ENVIRONMENT)
conn.close()

for item in (latest_bar_5m, latest_indicator_5m, latest_signal):
    if item and item.get("bar_time_ms"):
        item["age_min"] = age_minutes(item["bar_time_ms"])
        item["bar_time_us"] = format_us(item["bar_time_ms"])

data_freshness_window = classify_data_freshness_window(latest_bar_5m, targets)
indicator_missing_grace = latest_indicator_missing_grace(
    latest_bar_5m,
    latest_indicator_5m,
    bool(data_freshness_window.get("required")),
)

failures = []
warnings = []

for service, status in services.items():
    if not status.get("ok"):
        failures.append(f"service:{service}:{status.get('active_state')}")

for name in (
    "compute_health",
    "compute_status",
    "compute_ibkr_status",
    "runtime_health",
    "runtime_status",
    "pb_health",
    "pb_ibkr_healthz",
    "pb_ibkr_statusz",
    "pb_system_healthz",
    "pb_2fa_status",
):
    if not local_http[name].get("ok"):
        failures.append(f"local_http:{name}")

if latest_bar_5m is None and data_freshness_window.get("required"):
    failures.append("db:latest_bar_5m_missing")
elif (
    data_freshness_window.get("required")
    and latest_bar_5m.get("age_min") is not None
    and latest_bar_5m["age_min"] > BAR_STALE_MIN
):
    failures.append(f"db:latest_bar_5m_stale:{latest_bar_5m['age_min']}")

if latest_indicator_5m is None and data_freshness_window.get("required"):
    if indicator_missing_grace.get("active"):
        warnings.append(f"db:latest_indicator_5m_pending:{indicator_missing_grace.get('remaining_sec')}s")
    else:
        failures.append("db:latest_indicator_5m_missing")
elif (
    data_freshness_window.get("required")
    and latest_indicator_5m.get("age_min") is not None
    and latest_indicator_5m["age_min"] > INDICATOR_STALE_MIN
):
    failures.append(f"db:latest_indicator_5m_stale:{latest_indicator_5m['age_min']}")

runtime_payload = (
    local_http.get("runtime_status", {}).get("json")
    or local_http.get("compute_ibkr_status", {}).get("json")
    or {}
)
compute_status_payload = local_http.get("compute_status", {}).get("json") or {}
pb_statusz_payload = local_http.get("pb_ibkr_statusz", {}).get("json") or {}
pb_healthz_payload = local_http.get("pb_ibkr_healthz", {}).get("json") or {}
pb_system_healthz_payload = local_http.get("pb_system_healthz", {}).get("json") or {}
gateway = runtime_payload.get("gateway") or {}
session = runtime_payload.get("session") or {}
websocket = runtime_payload.get("websocket") or {}
realtime = runtime_payload.get("realtime_compute") or {}
market_universe = runtime_payload.get("market_universe") or {}
canonical_5m = runtime_payload.get("canonical_5m") or {}
bar_freshness = market_universe.get("bar_freshness") or {}
runtime_service_profile = str(runtime_payload.get("service_profile") or "").strip().lower()
runtime_topology = (
    runtime_payload.get("service_topology")
    or pb_statusz_payload.get("service_topology")
    or pb_healthz_payload.get("service_topology")
    or pb_system_healthz_payload.get("service_topology")
    or compute_status_payload.get("service_topology")
    or {}
)
runtime_topology_services = runtime_topology.get("services") or {}

canonical_pending_total = int(canonical_5m.get("pending_symbols_total") or bar_freshness.get("pending_symbols_total") or 0)
canonical_due_ms = int(canonical_5m.get("last_due_bucket_ms") or 0)
canonical_completed_ms = int(canonical_5m.get("last_completed_bucket_ms") or 0)
canonical_lag_s = normalize_bar_bucket_lag_seconds(
    canonical_5m.get("lag_s") or bar_freshness.get("lag_s") or 0,
    canonical_due_ms,
    canonical_completed_ms,
)
canonical_bucket_stale = data_freshness_window.get("required") and (
    str(bar_freshness.get("status") or "").strip().lower() == "stale"
    or (
        canonical_due_ms > 0
        and (canonical_completed_ms <= 0 or canonical_completed_ms < canonical_due_ms)
    )
)
if canonical_bucket_stale:
    failures.append(
        "runtime:canonical_5m_stale:"
        f"pending={canonical_pending_total}:"
        f"lag_s={round(canonical_lag_s, 2)}"
    )

if latest_signal is None:
    last_signal_count = int(((realtime.get("last_result") or {}).get("signals") or 0))
    if last_signal_count > 0:
        warnings.append("db:latest_signal_missing")

if gateway and (gateway.get("running") is False or gateway.get("reachable") is False):
    failures.append("runtime:gateway_unhealthy")

if local_http.get("runtime_status", {}).get("ok") and runtime_service_profile != "runtime":
    warnings.append(f"runtime:unexpected_service_profile:{runtime_service_profile or 'missing'}")

if "ibkr-runtime" not in runtime_topology_services:
    failures.append("topology:ibkr_runtime_missing")
if "ibkr-compute" not in runtime_topology_services:
    failures.append("topology:ibkr_compute_missing")

if session.get("authenticated") is False:
    bucket = failures if STRICT_RUNTIME else warnings
    bucket.append("runtime:session_not_authenticated")

if websocket.get("connected") is False:
    bucket = failures if STRICT_RUNTIME else warnings
    bucket.append("runtime:websocket_disconnected")

if websocket.get("ready") is False:
    bucket = failures if STRICT_RUNTIME else warnings
    bucket.append("runtime:websocket_not_ready")

queue_size = int(realtime.get("queue_size") or 0)
if queue_size > 10:
    warnings.append(f"runtime:compute_queue_high:{queue_size}")
    if bool(realtime.get("stalled")):
        failures.append(f"runtime:compute_stalled:{str(realtime.get('stall_reason') or 'unknown')}")
    else:
        last_bar_close_ms = parse_iso_ms(realtime.get("last_bar_close"))
        last_run_ms = parse_iso_ms(realtime.get("last_run"))
        if last_bar_close_ms and last_run_ms and last_bar_close_ms > last_run_ms:
            backlog_age_min = round((last_bar_close_ms - last_run_ms) / 60000.0, 2)
            if backlog_age_min >= 10:
                failures.append(f"runtime:compute_stalled:lagging:{backlog_age_min}")

if targets.get("total_count", 0) == 0:
    warnings.append("db:targets_missing")
elif targets.get("eligible_count", 0) == 0:
    warnings.append("db:targets_no_eligible_rows")

active_target_count = int(market_universe.get("active_target_count") or 0)
if targets.get("eligible_count", 0) > 0 and active_target_count == 0:
    warnings.append("runtime:active_subscription_symbols_empty")

report = {
    "ok": len(failures) == 0,
    "environment": ENVIRONMENT,
    "strict_runtime": STRICT_RUNTIME,
    "thresholds": {
        "bar_stale_min": BAR_STALE_MIN,
        "indicator_stale_min": INDICATOR_STALE_MIN,
        "indicator_missing_grace_sec": INDICATOR_MISSING_GRACE_SEC,
    },
    "failures": failures,
    "warnings": warnings,
    "remote": {
        "services": services,
        "local_http": local_http,
        "db": {
            "latest_bar_5m": latest_bar_5m,
            "latest_indicator_5m": latest_indicator_5m,
            "latest_signal": latest_signal,
            "targets": targets,
            "data_freshness_window": data_freshness_window,
            "indicator_missing_grace": indicator_missing_grace,
            "bars_by_interval": bars_by_interval,
            "indicators_by_interval": indicators_by_interval,
        },
        "runtime_summary": {
            "service_profile": runtime_service_profile,
            "service_topology": runtime_topology,
            "gateway": gateway,
            "session": session,
            "websocket": websocket,
            "canonical_5m": canonical_5m,
            "realtime_compute": realtime,
            "market_universe": market_universe,
        },
    },
}
print(json.dumps(report, ensure_ascii=False, indent=2))
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a one-click health check for the IBKR + PocketBase stack.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--pb-local-url", default=DEFAULT_PB_LOCAL_URL)
    parser.add_argument("--compute-local-url", default=DEFAULT_COMPUTE_LOCAL_URL)
    parser.add_argument("--runtime-local-url", default=DEFAULT_RUNTIME_LOCAL_URL)
    parser.add_argument("--pb-base-url", default=DEFAULT_PB_BASE_URL)
    parser.add_argument("--compute-public-url", default=DEFAULT_COMPUTE_PUBLIC_URL)
    parser.add_argument("--environment", default="live")
    parser.add_argument("--bar-stale-min", type=int, default=30)
    parser.add_argument("--indicator-stale-min", type=int, default=30)
    parser.add_argument("--indicator-missing-grace-sec", type=int, default=DEFAULT_INDICATOR_MISSING_GRACE_SEC)
    parser.add_argument("--strict-runtime", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


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
        "runtime_local_url": args.runtime_local_url,
        "environment": args.environment,
        "bar_stale_min": args.bar_stale_min,
        "indicator_stale_min": args.indicator_stale_min,
        "indicator_missing_grace_sec": args.indicator_missing_grace_sec,
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
            body = response.read(65536).decode("utf-8", errors="replace")
            result = {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "url": url,
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
            "body_snippet": body[:300],
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def add_public_checks(payload: dict, args: argparse.Namespace) -> dict:
    public = {
        "pb_api_health": fetch_url(f"{args.pb_base_url.rstrip('/')}/api/health", expect_json=True),
        "pb_ibkr_healthz": fetch_url(
            f"{args.pb_base_url.rstrip('/')}/api/custom/ibkr/healthz?environment={args.environment}",
            expect_json=True,
        ),
        "pb_ibkr_statusz": fetch_url(
            f"{args.pb_base_url.rstrip('/')}/api/custom/ibkr/statusz?environment={args.environment}",
            expect_json=True,
        ),
        "pb_system_summaryz": fetch_url(
            f"{args.pb_base_url.rstrip('/')}/api/custom/system/summaryz?lite=1&environment={args.environment}",
            expect_json=True,
        ),
        "pb_runtime_page": fetch_url(
            f"{args.pb_base_url.rstrip('/')}/ibkr_runtime.html?environment={args.environment}"
        ),
        "pb_system_page": fetch_url(
            f"{args.pb_base_url.rstrip('/')}/ibkr_system.html?environment={args.environment}"
        ),
        "compute_public_health": fetch_url(f"{args.compute_public_url.rstrip('/')}/health", expect_json=True),
    }

    payload["public"] = public
    failures = payload.setdefault("failures", [])
    warnings = payload.setdefault("warnings", [])

    for name in ("pb_api_health", "pb_ibkr_healthz", "pb_ibkr_statusz", "pb_system_summaryz", "compute_public_health"):
        if not public[name].get("ok"):
            failures.append(f"public:{name}")

    for name in ("pb_runtime_page", "pb_system_page"):
        if not public[name].get("ok"):
            failures.append(f"public:{name}")
        elif "html" in (public[name].get("body_snippet") or "").lower() and "ibkr" not in (public[name].get("body_snippet") or "").lower():
            warnings.append(f"public:{name}:marker_weak")

    topology_payload = (
        (public.get("pb_system_summaryz", {}).get("json") or {}).get("service_topology")
        or (public.get("pb_ibkr_statusz", {}).get("json") or {}).get("service_topology")
        or {}
    )
    services = topology_payload.get("services") or {}
    if "ibkr-runtime" not in services:
        failures.append("public:topology:ibkr_runtime_missing")
    if "ibkr-compute" not in services:
        failures.append("public:topology:ibkr_compute_missing")

    payload["ok"] = len(failures) == 0
    return payload


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

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("ok"):
        print("\nHealth Check OK: core services, endpoints, and freshness checks passed.")
        return 0
    print("\nHealth Check FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
