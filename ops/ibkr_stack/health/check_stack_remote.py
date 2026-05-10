import base64
import json
import os
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

cfg = json.loads(base64.b64decode(os.environ["HEALTH_CHECK_CONFIG_B64"]).decode("utf-8"))
DB = cfg["db_path"]
PB = cfg["pb_local_url"].rstrip("/")
COMPUTE = cfg["compute_local_url"].rstrip("/")
BACKTEST = cfg.get("backtest_local_url", "http://127.0.0.1:5105").rstrip("/")
RUNTIME = cfg["runtime_local_url"].rstrip("/")
API = cfg["api_local_url"].rstrip("/")
SCHEDULER = cfg["scheduler_local_url"].rstrip("/")
CONSOLE = cfg["console_local_url"].rstrip("/")
ENVIRONMENT = cfg["environment"]
BAR_STALE_MIN = int(cfg["bar_stale_min"])
INDICATOR_STALE_MIN = int(cfg["indicator_stale_min"])
INDICATOR_MISSING_GRACE_SEC = int(cfg.get("indicator_missing_grace_sec") or 90)
STRICT_RUNTIME = bool(cfg["strict_runtime"])
PRELOAD_WARN_SEC = int(cfg.get("preload_warn_sec") or 0)
PRELOAD_FAIL_SEC = int(cfg.get("preload_fail_sec") or 0)


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
    for observed in (
        _observed_fixed_holiday(year, 1, 1),
        _observed_fixed_holiday(year + 1, 1, 1),
    ):
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


def extract_compute_startup_preload(*payloads):
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


def compact_compute_startup_preload(payload):
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


def evaluate_compute_startup_preload_sla(preload_payload, warn_sec, fail_sec):
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


def format_compute_startup_preload_issue(prefix, preload_payload, sla_payload):
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


def compact_service_topology(payload):
    source = payload if isinstance(payload, dict) else {}
    services = source.get("services") if isinstance(source.get("services"), dict) else {}
    compact_services = {}
    for name, item in services.items():
        if not isinstance(item, dict):
            continue
        compact_services[str(name)] = {
            key: item.get(key)
            for key in ("status", "active_state", "service_profile", "url", "port")
            if item.get(key) not in (None, "")
        }
    return {"services": compact_services}


def compact_status_block(payload, keys):
    source = payload if isinstance(payload, dict) else {}
    return {key: source.get(key) for key in keys if source.get(key) not in (None, "", [], {})}


def compact_http_result_for_report(result):
    source = result if isinstance(result, dict) else {}
    compact = {
        key: source.get(key)
        for key in ("ok", "status_code", "url", "error", "text_snippet")
        if source.get(key) not in (None, "")
    }
    payload = source.get("json")
    if isinstance(payload, dict):
        summary = compact_status_block(
            payload,
            (
                "ok",
                "status",
                "service",
                "service_profile",
                "environment",
                "mode",
                "running",
                "ready",
            ),
        )
        preload = extract_compute_startup_preload(payload)
        if preload:
            summary["compute_startup_preload"] = compact_compute_startup_preload(preload)
        if isinstance(payload.get("service_topology"), dict):
            summary["service_topology"] = compact_service_topology(payload.get("service_topology"))
        if isinstance(payload.get("gateway"), dict):
            summary["gateway"] = compact_status_block(payload.get("gateway"), ("running", "reachable", "status"))
        if isinstance(payload.get("session"), dict):
            summary["session"] = compact_status_block(payload.get("session"), ("authenticated", "connected", "status"))
        if isinstance(payload.get("websocket"), dict):
            summary["websocket"] = compact_status_block(payload.get("websocket"), ("connected", "ready", "status"))
        if isinstance(payload.get("canonical_5m"), dict):
            summary["canonical_5m"] = compact_status_block(
                payload.get("canonical_5m"),
                ("status", "pending_symbols_total", "last_due_bucket_ms", "last_completed_bucket_ms", "lag_s"),
            )
        market_universe = payload.get("market_universe")
        if isinstance(market_universe, dict):
            summary["market_universe"] = compact_status_block(
                market_universe,
                ("active_target_count", "pending_symbols_total", "subscription_count", "status"),
            )
            if isinstance(market_universe.get("bar_freshness"), dict):
                summary["market_universe"]["bar_freshness"] = compact_status_block(
                    market_universe.get("bar_freshness"),
                    ("status", "pending_symbols_total", "lag_s"),
                )
        if isinstance(payload.get("realtime_compute"), dict):
            summary["realtime_compute"] = compact_status_block(
                payload.get("realtime_compute"),
                ("queue_size", "stalled", "stall_reason", "last_run", "last_bar_close"),
            )
        if not summary:
            summary["keys"] = sorted(str(key) for key in payload.keys())[:30]
        compact["json"] = summary
    return compact


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
    for name in (
        "ibkr-runtime",
        "ibkr-gateway",
        "ibkr-compute",
        "ibkr-backtest",
        "ibkr-api",
        "ibkr-scheduler",
        "ibkr-console",
        "pocketbase",
    )
}
local_http = {
    "compute_health": http_json(f"{COMPUTE}/health"),
    "compute_status": http_json(f"{COMPUTE}/status"),
    "compute_ibkr_status": http_json(f"{COMPUTE}/ibkr/status"),
    "backtest_health": http_json(f"{BACKTEST}/health"),
    "runtime_health": http_json(f"{RUNTIME}/health"),
    "runtime_status": http_json(f"{RUNTIME}/ibkr/status"),
    "api_health": http_json(f"{API}/health"),
    "api_status": http_json(f"{API}/status"),
    "api_system_cronz": http_json(f"{API}/api/custom/system/cronz?environment={ENVIRONMENT}"),
    "api_storage_health": http_json(f"{API}/api/custom/system/storagez?environment={ENVIRONMENT}"),
    "api_runtime_config": http_json(f"{API}/api/custom/ibkr/runtime/config?environment={ENVIRONMENT}"),
    "scheduler_health": http_json(f"{SCHEDULER}/health"),
    "scheduler_status": http_json(f"{SCHEDULER}/status"),
    "console_index": http_json(f"{CONSOLE}/index.html"),
    "pb_health": http_json(f"{PB}/api/health"),
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
    "backtest_health",
    "runtime_health",
    "runtime_status",
    "api_health",
    "api_status",
    "api_system_cronz",
    "api_runtime_config",
    "scheduler_health",
    "scheduler_status",
    "console_index",
    "pb_health",
):
    if not local_http[name].get("ok"):
        failures.append(f"local_http:{name}")

if not local_http.get("api_storage_health", {}).get("ok"):
    warnings.append("local_http:api_storage_health")

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
api_status_payload = local_http.get("api_status", {}).get("json") or {}
scheduler_status_payload = local_http.get("scheduler_status", {}).get("json") or {}
compute_status_payload = local_http.get("compute_status", {}).get("json") or {}
compute_health_payload = local_http.get("compute_health", {}).get("json") or {}
storage_health_payload = local_http.get("api_storage_health", {}).get("json") or {}
gateway = runtime_payload.get("gateway") or {}
session = runtime_payload.get("session") or {}
websocket = runtime_payload.get("websocket") or {}
realtime = runtime_payload.get("realtime_compute") or {}
market_universe = runtime_payload.get("market_universe") or {}
canonical_5m = runtime_payload.get("canonical_5m") or {}
bar_freshness = market_universe.get("bar_freshness") or {}
runtime_service_profile = str(runtime_payload.get("service_profile") or "").strip().lower()
runtime_topology = (
    api_status_payload.get("service_topology")
    or scheduler_status_payload.get("service_topology")
    or runtime_payload.get("service_topology")
    or compute_status_payload.get("service_topology")
    or {}
)
runtime_topology_services = runtime_topology.get("services") or {}
compute_startup_preload = compact_compute_startup_preload(
    extract_compute_startup_preload(
        compute_status_payload,
        compute_health_payload,
        {},
        {},
    )
)
compute_startup_preload_sla = evaluate_compute_startup_preload_sla(
    compute_startup_preload,
    PRELOAD_WARN_SEC,
    PRELOAD_FAIL_SEC,
)
storage_status = str(storage_health_payload.get("status") or "").strip().lower()
if storage_status in {"error", "unavailable", "offline"}:
    failures.append(f"db:storage_health:{storage_status}")
elif storage_status in {"warning", "degraded", "partial"}:
    warnings.append(f"db:storage_health:{storage_status}")

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
if "ibkr-backtest" not in runtime_topology_services:
    failures.append("topology:ibkr_backtest_missing")
if "ibkr-api" not in runtime_topology_services:
    failures.append("topology:ibkr_api_missing")
if "ibkr-scheduler" not in runtime_topology_services:
    failures.append("topology:ibkr_scheduler_missing")
if "ibkr-console" not in runtime_topology_services:
    failures.append("topology:ibkr_console_missing")

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

current_et_day = datetime.now(ET).date()
current_nyse_trading_day = is_nyse_trading_day(current_et_day)

if targets.get("total_count", 0) == 0:
    warnings.append("db:targets_missing")
elif targets.get("eligible_count", 0) == 0 and current_nyse_trading_day:
    warnings.append("db:targets_no_eligible_rows")

preload_reason = str(compute_startup_preload_sla.get("reason") or "").strip().lower()
if preload_reason == "failed":
    failures.append("compute:startup_preload_failed")
elif preload_reason == "timeout":
    failures.append(
        format_compute_startup_preload_issue(
            "compute:startup_preload_timeout",
            compute_startup_preload,
            compute_startup_preload_sla,
        )
    )
elif preload_reason == "slow":
    warnings.append(
        format_compute_startup_preload_issue(
            "compute:startup_preload_slow",
            compute_startup_preload,
            compute_startup_preload_sla,
        )
    )

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
        "preload_warn_sec": PRELOAD_WARN_SEC,
        "preload_fail_sec": PRELOAD_FAIL_SEC,
    },
    "market_calendar": {
        "current_et_date": current_et_day.isoformat(),
        "nyse_trading_day": current_nyse_trading_day,
    },
    "failures": failures,
    "warnings": warnings,
    "remote": {
        "services": services,
        "local_http": {name: compact_http_result_for_report(item) for name, item in local_http.items()},
        "db": {
            "latest_bar_5m": latest_bar_5m,
            "latest_indicator_5m": latest_indicator_5m,
            "latest_signal": latest_signal,
            "targets": targets,
            "data_freshness_window": data_freshness_window,
            "indicator_missing_grace": indicator_missing_grace,
            "bars_by_interval": bars_by_interval,
            "indicators_by_interval": indicators_by_interval,
            "storage_health": storage_health_payload,
        },
        "runtime_summary": {
            "service_profile": runtime_service_profile,
            "service_topology": compact_service_topology(runtime_topology),
            "compute_startup_preload": compute_startup_preload,
            "compute_startup_preload_sla": compute_startup_preload_sla,
            "gateway": compact_status_block(gateway, ("running", "reachable", "status")),
            "session": compact_status_block(session, ("authenticated", "connected", "status")),
            "websocket": compact_status_block(websocket, ("connected", "ready", "status")),
            "canonical_5m": compact_status_block(
                canonical_5m,
                ("status", "pending_symbols_total", "last_due_bucket_ms", "last_completed_bucket_ms", "lag_s"),
            ),
            "realtime_compute": compact_status_block(
                realtime,
                ("queue_size", "stalled", "stall_reason", "last_run", "last_bar_close"),
            ),
            "market_universe": compact_status_block(
                market_universe,
                ("active_target_count", "pending_symbols_total", "subscription_count", "status"),
            ),
        },
    },
}
print(json.dumps(report, ensure_ascii=False, indent=2))
