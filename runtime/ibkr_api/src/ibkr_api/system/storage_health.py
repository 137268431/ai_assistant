from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ibkr_api.system.pocketbase_disk import collect_pocketbase_disk_snapshot, format_bytes

ET = ZoneInfo("America/New_York")
MS_PER_DAY = 86_400_000
GIB = 1024 * 1024 * 1024

STORAGE_HEALTH_CACHE_TTL_SECONDS = max(
    5.0,
    float(os.environ.get("IBKR_STORAGE_HEALTH_CACHE_TTL_SEC", "60.0") or "60.0"),
)
TABLE_QUERY_TIMEOUT_MS = max(
    50,
    int(os.environ.get("IBKR_STORAGE_HEALTH_TABLE_QUERY_TIMEOUT_MS", "350") or "350"),
)
SAMPLE_QUERY_TIMEOUT_MS = max(
    500,
    int(os.environ.get("IBKR_STORAGE_HEALTH_SAMPLE_QUERY_TIMEOUT_MS", "3000") or "3000"),
)
WAL_WARNING_BYTES = max(
    256 * 1024 * 1024,
    int(os.environ.get("IBKR_STORAGE_HEALTH_WAL_WARN_BYTES", str(2 * GIB)) or str(2 * GIB)),
)
WAL_WARNING_RATIO = max(
    0.05,
    float(os.environ.get("IBKR_STORAGE_HEALTH_WAL_WARN_RATIO", "0.30") or "0.30"),
)
DISK_WARNING_FREE_PCT = max(
    1.0,
    float(os.environ.get("IBKR_STORAGE_HEALTH_DISK_WARN_FREE_PCT", "15.0") or "15.0"),
)
DISK_ERROR_FREE_PCT = max(
    1.0,
    float(os.environ.get("IBKR_STORAGE_HEALTH_DISK_ERROR_FREE_PCT", "8.0") or "8.0"),
)


MONITORED_TABLES: tuple[dict[str, Any], ...] = (
    {"name": "ibkr_bars", "group": "market", "critical": True, "retention": True, "interval_scoped": True},
    {"name": "ibkr_indicators", "group": "market", "critical": True, "retention": True},
    {"name": "ibkr_signals", "group": "market", "critical": True, "retention": True},
    {"name": "ibkr_reverse_signals", "group": "market", "critical": False, "retention": True},
    {"name": "ibkr_targets", "group": "market", "critical": True, "retention": True},
    {"name": "orders", "group": "trading", "critical": True, "status_counts": True},
    {"name": "ibkr_order_details", "group": "trading", "critical": True, "status_counts": True},
    {"name": "ibkr_bar_integrity", "group": "quality", "critical": False, "retention": True, "date_field": "market_date"},
    {"name": "ibkr_bar_coverage_daily", "group": "quality", "critical": False, "retention": True, "date_field": "market_date"},
    {"name": "ibkr_bar_truth_audit", "group": "quality", "critical": False, "retention": True, "date_field": "market_date"},
    {"name": "ibkr_state", "group": "runtime", "critical": True},
    {"name": "config", "group": "runtime", "critical": True},
    {"name": "watchlist", "group": "runtime", "critical": True},
    {"name": "system_events", "group": "runtime", "critical": False},
    {"name": "ibkr_backtest_runs", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_batches", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_trades", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_signals", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_reverse_signals", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_targets", "group": "backtest", "critical": False},
    {"name": "ibkr_backtest_daily_selection_cache", "group": "backtest", "critical": False, "date_field": "market_date"},
    {"name": "ibkr_backtest_indicators", "group": "backtest", "critical": False},
    {"name": "tv_signals", "group": "compat", "critical": False},
    {"name": "tv_indicators", "group": "compat", "critical": False},
)

GROUP_LABELS = {
    "market": "行情链路",
    "trading": "交易链路",
    "quality": "数据质量",
    "runtime": "运行状态",
    "backtest": "回测产物",
    "compat": "兼容输入",
}

INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")

_STORAGE_HEALTH_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_us_ms(value: Any) -> str:
    try:
        ms = int(value or 0)
    except Exception:
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000.0, ET).strftime("%Y-%m-%d %H:%M:%S")


def _format_date_ms(value: Any) -> str:
    try:
        ms = int(value or 0)
    except Exception:
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_ident(name: str) -> str:
    text = str(name or "").strip()
    allowed = {item["name"] for item in MONITORED_TABLES}
    allowed.update({"sqlite_stat1"})
    if text not in allowed:
        raise ValueError(f"unsupported sqlite identifier: {text}")
    return '"' + text.replace('"', '""') + '"'


def resolve_pb_db_path() -> Path:
    candidates = (
        os.environ.get("PB_DB_PATH"),
        os.environ.get("PB_SQLITE_PATH"),
        str(Path(os.environ["PB_DATA_DIR"]).expanduser() / "data.db") if os.environ.get("PB_DATA_DIR") else "",
        str(Path(os.environ["POCKETBASE_DATA_DIR"]).expanduser() / "data.db") if os.environ.get("POCKETBASE_DATA_DIR") else "",
        "/opt/pocketbase/pb_data/data.db",
    )
    for raw in candidates:
        text = str(raw or "").strip()
        if text:
            return Path(text).expanduser()
    return Path("/opt/pocketbase/pb_data/data.db")


def _sqlite_readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/:')}?mode=ro"


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_readonly_uri(db_path), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fetchone_timed(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    timeout_ms: int = TABLE_QUERY_TIMEOUT_MS,
) -> tuple[sqlite3.Row | None, float, str]:
    deadline = time.perf_counter() + max(1, int(timeout_ms or 1)) / 1000.0

    def progress_handler() -> int:
        return 1 if time.perf_counter() > deadline else 0

    start = time.perf_counter()
    conn.set_progress_handler(progress_handler, 1000)
    try:
        row = conn.execute(sql, params).fetchone()
        return row, round((time.perf_counter() - start) * 1000, 2), ""
    except Exception as exc:
        return None, round((time.perf_counter() - start) * 1000, 2), str(exc)
    finally:
        conn.set_progress_handler(None, 0)


def _fetchall_timed(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    timeout_ms: int = TABLE_QUERY_TIMEOUT_MS,
) -> tuple[list[sqlite3.Row], float, str]:
    deadline = time.perf_counter() + max(1, int(timeout_ms or 1)) / 1000.0

    def progress_handler() -> int:
        return 1 if time.perf_counter() > deadline else 0

    start = time.perf_counter()
    conn.set_progress_handler(progress_handler, 1000)
    try:
        rows = conn.execute(sql, params).fetchall()
        return rows, round((time.perf_counter() - start) * 1000, 2), ""
    except Exception as exc:
        return [], round((time.perf_counter() - start) * 1000, 2), str(exc)
    finally:
        conn.set_progress_handler(None, 0)


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _load_sqlite_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str) -> int:
        row, _, _ = _fetchone_timed(conn, sql, timeout_ms=200)
        if row is None:
            return 0
        return _to_int(row[0])

    page_size = one("pragma page_size")
    page_count = one("pragma page_count")
    freelist_count = one("pragma freelist_count")
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "estimated_db_bytes": page_size * page_count if page_size > 0 and page_count > 0 else 0,
    }


def _load_existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows, _, _ = _fetchall_timed(
        conn,
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'",
        timeout_ms=500,
    )
    return {str(row["name"] or "") for row in rows}


def _load_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"pragma table_info({_safe_ident(table)})").fetchall()
    except Exception:
        return set()
    return {str(row["name"] or "") for row in rows}


def _load_row_estimates(conn: sqlite3.Connection) -> dict[str, int]:
    rows, _, error = _fetchall_timed(
        conn,
        """
        select tbl, max(cast(substr(stat, 1, instr(stat || ' ', ' ') - 1) as integer)) as estimated_rows
        from sqlite_stat1
        group by tbl
        """,
        timeout_ms=700,
    )
    if error:
        return {}
    return {str(row["tbl"] or ""): _to_int(row["estimated_rows"]) for row in rows}


def _exact_count_if_small(conn: sqlite3.Connection, table: str, estimated_rows: int | None) -> tuple[int | None, str]:
    if estimated_rows is not None and int(estimated_rows or 0) > 50_000:
        return None, "estimated"
    row, _, error = _fetchone_timed(conn, f"select count(*) as total from {_safe_ident(table)}", timeout_ms=250)
    if error or row is None:
        return None, "estimated" if estimated_rows is not None else "unavailable"
    return _to_int(row["total"]), "exact"


def _edge_numeric_time(
    conn: sqlite3.Connection,
    table: str,
    field: str,
    environment: str,
    columns: set[str],
    *,
    descending: bool,
    interval: str = "",
    timeout_ms: int = TABLE_QUERY_TIMEOUT_MS,
) -> tuple[int, float, str]:
    where = [f"{field} > 0"]
    params: list[Any] = []
    if "environment" in columns:
        where.append("environment = ?")
        params.append(environment)
    if interval and "interval" in columns:
        where.append("interval = ?")
        params.append(interval)
    direction = "desc" if descending else "asc"
    row, elapsed_ms, error = _fetchone_timed(
        conn,
        f"""
        select {field} as value
        from {_safe_ident(table)}
        where {' and '.join(where)}
        order by {field} {direction}
        limit 1
        """,
        tuple(params),
        timeout_ms=timeout_ms,
    )
    if error or row is None:
        return 0, elapsed_ms, error
    return _to_int(row["value"]), elapsed_ms, ""


def _edge_text_time(
    conn: sqlite3.Connection,
    table: str,
    field: str,
    environment: str,
    columns: set[str],
    *,
    descending: bool,
) -> tuple[str, float, str]:
    where = [f"{field} != ''"]
    params: list[Any] = []
    if "environment" in columns:
        where.append("environment = ?")
        params.append(environment)
    direction = "desc" if descending else "asc"
    row, elapsed_ms, error = _fetchone_timed(
        conn,
        f"""
        select {field} as value
        from {_safe_ident(table)}
        where {' and '.join(where)}
        order by {field} {direction}
        limit 1
        """,
        tuple(params),
        timeout_ms=TABLE_QUERY_TIMEOUT_MS,
    )
    if error or row is None:
        return "", elapsed_ms, error
    return str(row["value"] or ""), elapsed_ms, ""


def _bar_time_edges_by_interval(
    conn: sqlite3.Connection,
    table: str,
    environment: str,
    columns: set[str],
) -> tuple[int, int, float, list[str]]:
    latest_values: list[int] = []
    oldest_values: list[int] = []
    elapsed_total = 0.0
    errors: list[str] = []
    for interval in INTERVALS:
        latest, elapsed_ms, error = _edge_numeric_time(
            conn,
            table,
            "bar_time_ms",
            environment,
            columns,
            descending=True,
            interval=interval,
            timeout_ms=TABLE_QUERY_TIMEOUT_MS,
        )
        elapsed_total += elapsed_ms
        if latest > 0:
            latest_values.append(latest)
        elif error:
            errors.append(f"{interval}:latest:{error}")
        oldest, elapsed_ms, error = _edge_numeric_time(
            conn,
            table,
            "bar_time_ms",
            environment,
            columns,
            descending=False,
            interval=interval,
            timeout_ms=TABLE_QUERY_TIMEOUT_MS,
        )
        elapsed_total += elapsed_ms
        if oldest > 0:
            oldest_values.append(oldest)
        elif error:
            errors.append(f"{interval}:oldest:{error}")
    return (
        max(latest_values) if latest_values else 0,
        min(oldest_values) if oldest_values else 0,
        round(elapsed_total, 2),
        errors[:4],
    )


def _status_counts(conn: sqlite3.Connection, table: str, environment: str, columns: set[str]) -> dict[str, int]:
    if "status" not in columns:
        return {}
    where = []
    params: list[Any] = []
    if "environment" in columns:
        where.append("environment = ?")
        params.append(environment)
    sql = f"select status, count(*) as total from {_safe_ident(table)}"
    if where:
        sql += f" where {' and '.join(where)}"
    sql += " group by status"
    rows, _, error = _fetchall_timed(conn, sql, tuple(params), timeout_ms=300)
    if error:
        return {}
    return {str(row["status"] or "blank"): _to_int(row["total"]) for row in rows}


def _collect_table(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    *,
    environment: str,
    existing_tables: set[str],
    row_estimates: dict[str, int],
) -> dict[str, Any]:
    name = str(config["name"])
    item: dict[str, Any] = {
        "name": name,
        "group": str(config.get("group") or "other"),
        "group_label": GROUP_LABELS.get(str(config.get("group") or ""), str(config.get("group") or "other")),
        "critical": bool(config.get("critical")),
        "exists": name in existing_tables,
        "status": "ok",
        "reason": "",
        "row_count": None,
        "row_count_source": "unavailable",
        "latest_ms": 0,
        "latest_us": "",
        "oldest_ms": 0,
        "oldest_us": "",
        "latest_text": "",
        "oldest_text": "",
        "query_elapsed_ms": 0.0,
        "query_errors": [],
    }
    if name not in existing_tables:
        item["status"] = "error" if bool(config.get("critical")) else "warning"
        item["reason"] = "table_missing"
        return item

    columns = _load_columns(conn, name)
    estimated_rows = row_estimates.get(name)
    exact_rows, row_source = _exact_count_if_small(conn, name, estimated_rows)
    if exact_rows is not None:
        item["row_count"] = exact_rows
        item["row_count_source"] = row_source
    elif estimated_rows is not None:
        item["row_count"] = int(estimated_rows)
        item["row_count_source"] = "estimated"

    query_elapsed = 0.0
    query_errors: list[str] = []
    if "bar_time_ms" in columns:
        if bool(config.get("interval_scoped")) and "interval" in columns:
            latest_ms, oldest_ms, elapsed_ms, errors = _bar_time_edges_by_interval(conn, name, environment, columns)
            query_elapsed += elapsed_ms
            query_errors.extend(errors)
        else:
            latest_ms, elapsed_ms, error = _edge_numeric_time(
                conn,
                name,
                "bar_time_ms",
                environment,
                columns,
                descending=True,
            )
            query_elapsed += elapsed_ms
            if error:
                query_errors.append(f"latest:{error}")
            oldest_ms = 0
            if bool(config.get("retention")):
                oldest_ms, elapsed_ms, error = _edge_numeric_time(
                    conn,
                    name,
                    "bar_time_ms",
                    environment,
                    columns,
                    descending=False,
                )
                query_elapsed += elapsed_ms
                if error:
                    query_errors.append(f"oldest:{error}")
        item["latest_ms"] = latest_ms
        item["oldest_ms"] = oldest_ms
        item["latest_us"] = _format_us_ms(latest_ms)
        item["oldest_us"] = _format_us_ms(oldest_ms)
    elif str(config.get("date_field") or "") in columns:
        field = str(config.get("date_field") or "")
        latest_text, elapsed_ms, error = _edge_text_time(conn, name, field, environment, columns, descending=True)
        query_elapsed += elapsed_ms
        if error:
            query_errors.append(f"latest:{error}")
        oldest_text, elapsed_ms, error = _edge_text_time(conn, name, field, environment, columns, descending=False)
        query_elapsed += elapsed_ms
        if error:
            query_errors.append(f"oldest:{error}")
        item["latest_text"] = latest_text
        item["oldest_text"] = oldest_text
    elif "created" in columns:
        latest_text, elapsed_ms, error = _edge_text_time(conn, name, "created", environment, columns, descending=True)
        query_elapsed += elapsed_ms
        if error:
            query_errors.append(f"latest:{error}")
        item["latest_text"] = latest_text

    if bool(config.get("status_counts")):
        item["status_counts"] = _status_counts(conn, name, environment, columns)
    if query_errors:
        item["query_errors"] = query_errors[:4]
    item["query_elapsed_ms"] = round(query_elapsed, 2)
    return item


def _sample_queries(conn: sqlite3.Connection, environment: str, existing_tables: set[str]) -> list[dict[str, Any]]:
    if "ibkr_bars" not in existing_tables:
        return []
    start = time.perf_counter()
    rows, _, error = _fetchall_timed(
        conn,
        """
        select id, bar_time_ms
        from ibkr_bars
        where symbol = ? and interval = ? and environment = ?
        order by bar_time_ms desc
        limit 400
        """,
        ("SPY", "5m", environment),
        timeout_ms=SAMPLE_QUERY_TIMEOUT_MS,
    )
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    return [
        {
            "name": "ibkr_bars_spy_5m_recent_400",
            "ok": not bool(error),
            "elapsed_ms": elapsed_ms,
            "row_count": len(rows),
            "threshold_ms": 2000,
            "error": error,
        }
    ]


def _retention_days(config_map: dict[str, Any] | None) -> int:
    source = config_map if isinstance(config_map, dict) else {}
    value = source.get("ibkr_history_retention_days")
    if value is None:
        value = os.environ.get("IBKR_HISTORY_RETENTION_DAYS", "365")
    return max(30, _to_int(value, 365))


def _flag(severity: str, code: str, title: str, detail: str) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "title": title,
        "detail": detail,
    }


def _status_from_flags(flags: list[dict[str, Any]], default: str = "ok") -> str:
    severities = {str((flag or {}).get("severity") or "").strip().lower() for flag in flags}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return default


def _build_db_files(db_path: Path, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    data_size = _file_size(db_path)
    wal_size = _file_size(Path(f"{db_path}-wal"))
    shm_size = _file_size(Path(f"{db_path}-shm"))
    disk_snapshot = collect_pocketbase_disk_snapshot()
    filesystem = disk_snapshot.get("filesystem") if isinstance(disk_snapshot.get("filesystem"), dict) else {}
    total_bytes = _to_int(filesystem.get("total_bytes"))
    available_bytes = _to_int(filesystem.get("available_bytes"))
    free_pct = round((available_bytes / total_bytes) * 100, 2) if total_bytes > 0 else None
    pragmas = _load_sqlite_pragmas(conn) if conn is not None else {}
    wal_ratio = round(wal_size / data_size, 4) if data_size > 0 else 0.0
    return {
        "db_path": str(db_path),
        "db_size_bytes": data_size,
        "db_size_label": format_bytes(data_size),
        "wal_size_bytes": wal_size,
        "wal_size_label": format_bytes(wal_size),
        "shm_size_bytes": shm_size,
        "shm_size_label": format_bytes(shm_size),
        "wal_to_db_ratio": wal_ratio,
        "wal_to_db_pct": round(wal_ratio * 100, 2),
        "filesystem": filesystem,
        "disk_free_pct": free_pct,
        "disk_free_bytes": available_bytes,
        "disk_free_label": format_bytes(available_bytes),
        "data_path": disk_snapshot.get("data_path") or str(db_path.parent),
        "data_size_bytes": _to_int(disk_snapshot.get("data_size_bytes")),
        "data_size_label": format_bytes(disk_snapshot.get("data_size_bytes")),
        **pragmas,
    }


def _collect_storage_health_uncached(
    environment: str,
    *,
    config_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    retention_days = _retention_days(config_map)
    db_path = resolve_pb_db_path()
    collected_at = _utc_stamp()
    flags: list[dict[str, Any]] = []

    if not db_path.exists():
        return {
            "ok": False,
            "status": "unavailable",
            "environment": runtime_environment,
            "source": "ibkr_api_storage_health",
            "collected_at": collected_at,
            "db_files": _build_db_files(db_path, None),
            "tables": [],
            "groups": GROUP_LABELS,
            "flags": [
                _flag("error", "pb_db_missing", "PocketBase data.db missing", f"{db_path} does not exist"),
            ],
            "summary": {
                "monitored_tables": len(MONITORED_TABLES),
                "existing_tables": 0,
                "estimated_rows": 0,
                "retention_days": retention_days,
            },
        }

    try:
        conn = _open_readonly(db_path)
    except Exception as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "environment": runtime_environment,
            "source": "ibkr_api_storage_health",
            "collected_at": collected_at,
            "db_files": _build_db_files(db_path, None),
            "tables": [],
            "groups": GROUP_LABELS,
            "flags": [
                _flag("error", "pb_db_open_failed", "PocketBase data.db open failed", str(exc)),
            ],
            "summary": {
                "monitored_tables": len(MONITORED_TABLES),
                "existing_tables": 0,
                "estimated_rows": 0,
                "retention_days": retention_days,
            },
        }

    try:
        db_files = _build_db_files(db_path, conn)
        existing_tables = _load_existing_tables(conn)
        row_estimates = _load_row_estimates(conn)
        tables = [
            _collect_table(
                conn,
                table_config,
                environment=runtime_environment,
                existing_tables=existing_tables,
                row_estimates=row_estimates,
            )
            for table_config in MONITORED_TABLES
        ]
        samples = _sample_queries(conn, runtime_environment, existing_tables)
    finally:
        conn.close()

    wal_size = _to_int(db_files.get("wal_size_bytes"))
    data_size = _to_int(db_files.get("db_size_bytes"))
    wal_ratio = _to_float(db_files.get("wal_to_db_ratio"))
    if wal_size >= WAL_WARNING_BYTES or (data_size > 0 and wal_ratio >= WAL_WARNING_RATIO):
        flags.append(
            _flag(
                "warning",
                "pb_wal_high",
                "PocketBase WAL high",
                (
                    f"WAL {format_bytes(wal_size)} / DB {format_bytes(data_size)} "
                    f"({round(wal_ratio * 100, 1)}%)"
                ),
            )
        )

    disk_free_pct = db_files.get("disk_free_pct")
    if isinstance(disk_free_pct, (int, float)):
        if float(disk_free_pct) < DISK_ERROR_FREE_PCT:
            flags.append(
                _flag(
                    "error",
                    "pb_disk_free_critical",
                    "PocketBase disk critically low",
                    f"free {float(disk_free_pct):.1f}% / {db_files.get('disk_free_label')}",
                )
            )
        elif float(disk_free_pct) < DISK_WARNING_FREE_PCT:
            flags.append(
                _flag(
                    "warning",
                    "pb_disk_free_low",
                    "PocketBase disk free low",
                    f"free {float(disk_free_pct):.1f}% / {db_files.get('disk_free_label')}",
                )
            )

    table_status_counts = {"ok": 0, "warning": 0, "error": 0, "unavailable": 0}
    for table in tables:
        status = str(table.get("status") or "ok")
        table_status_counts[status] = table_status_counts.get(status, 0) + 1
        if status in {"warning", "error"}:
            flags.append(
                _flag(
                    "error" if status == "error" else "warning",
                    f"table_{table.get('name')}_{table.get('reason') or status}",
                    f"{table.get('name')} {status}",
                    str(table.get("reason") or ""),
                )
            )

    bars = next((table for table in tables if table.get("name") == "ibkr_bars"), {})
    oldest_bar_ms = _to_int(bars.get("oldest_ms"))
    retention_cutoff_ms = _now_ms() - (retention_days + 7) * MS_PER_DAY
    if oldest_bar_ms > 0 and oldest_bar_ms < retention_cutoff_ms:
        flags.append(
            _flag(
                "warning",
                "ibkr_bars_retention_lag",
                "ibkr_bars retention lag",
                (
                    f"oldest {_format_us_ms(oldest_bar_ms)} exceeds "
                    f"{retention_days}+7 day online window"
                ),
            )
        )

    for sample in samples:
        if not bool(sample.get("ok")):
            flags.append(
                _flag(
                    "warning",
                    f"query_{sample.get('name')}_failed",
                    "Storage health query failed",
                    str(sample.get("error") or ""),
                )
            )
        elif _to_float(sample.get("elapsed_ms")) > _to_float(sample.get("threshold_ms"), 2000):
            flags.append(
                _flag(
                    "warning",
                    f"query_{sample.get('name')}_slow",
                    "Storage health query slow",
                    f"{sample.get('elapsed_ms')}ms > {sample.get('threshold_ms')}ms",
                )
            )

    row_total = sum(_to_int(table.get("row_count")) for table in tables if table.get("row_count") is not None)
    largest_tables = sorted(
        [
            {
                "name": str(table.get("name") or ""),
                "group": str(table.get("group") or ""),
                "row_count": _to_int(table.get("row_count")),
                "row_count_source": str(table.get("row_count_source") or ""),
            }
            for table in tables
            if table.get("row_count") is not None
        ],
        key=lambda item: item["row_count"],
        reverse=True,
    )[:8]
    status = _status_from_flags(flags)
    return {
        "ok": status == "ok",
        "status": status,
        "environment": runtime_environment,
        "source": "ibkr_api_storage_health",
        "collected_at": collected_at,
        "retention_days": retention_days,
        "db_files": db_files,
        "tables": tables,
        "groups": GROUP_LABELS,
        "flags": flags,
        "query_samples": samples,
        "summary": {
            "monitored_tables": len(MONITORED_TABLES),
            "existing_tables": sum(1 for table in tables if table.get("exists")),
            "table_status_counts": table_status_counts,
            "estimated_rows": row_total,
            "largest_tables": largest_tables,
            "retention_days": retention_days,
            "retention_cutoff_date": _format_date_ms(retention_cutoff_ms),
            "cache_ttl_s": int(STORAGE_HEALTH_CACHE_TTL_SECONDS),
        },
    }


def collect_storage_health(
    environment: str,
    *,
    config_map: dict[str, Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    retention_days = _retention_days(config_map)
    db_path = resolve_pb_db_path()
    cache_key = (str(db_path), runtime_environment, retention_days)
    now = time.time()
    cached = _STORAGE_HEALTH_CACHE.get(cache_key)
    if (
        not force_refresh
        and isinstance(cached, dict)
        and cached
        and float(cached.get("_expires_at") or 0.0) > now
    ):
        payload = dict(cached)
        payload.pop("_expires_at", None)
        payload["cached"] = True
        return payload

    payload = _collect_storage_health_uncached(runtime_environment, config_map=config_map)
    _STORAGE_HEALTH_CACHE[cache_key] = {
        **payload,
        "_expires_at": now + STORAGE_HEALTH_CACHE_TTL_SECONDS,
    }
    return payload


__all__ = [
    "GROUP_LABELS",
    "MONITORED_TABLES",
    "collect_storage_health",
    "resolve_pb_db_path",
]
