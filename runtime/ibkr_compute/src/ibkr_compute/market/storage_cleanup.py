from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market import pocketbase_sqlite

logger = logging.getLogger(__name__)

GIB = 1024 * 1024 * 1024
STORAGE_CLEANUP_STATE_KEY = "ibkr_storage_cleanup"
DEFAULT_PROFILE = "tv_primary_lean"
PROFILE_ALIASES = {
    DEFAULT_PROFILE: DEFAULT_PROFILE,
    "balanced_50g": DEFAULT_PROFILE,
}
SUPPORTED_PROFILES = tuple(PROFILE_ALIASES.keys())
DEFAULT_BATCH_SIZE = max(20, int(os.environ.get("IBKR_STORAGE_CLEANUP_BATCH_SIZE", "200") or "200"))
DEFAULT_RECENT_BACKTEST_LIMIT = max(
    5,
    int(os.environ.get("IBKR_STORAGE_CLEANUP_RECENT_BACKTEST_LIMIT", "5") or "5"),
)
DEFAULT_PROTECTED_BATCH_IDS = ("3gf4ouzj7oyvlao",)
DEFAULT_PROTECTED_RUN_IDS = ("bm9wl0lagddsd6a",)
TV_TRANSIENT_SIGNAL_TERMINAL_STATUSES = {
    "rejected",
    "expired",
    "blocked",
    "entry_missed_limit_cap",
    "validation_rejected",
    "submit_failed",
}
TV_TRANSIENT_REVERSE_REASONS = {
    "adjust_bracket_targets_missing_or_invalid",
    "conid_unresolved",
}
TV_TRANSIENT_REVERSE_ACTION_TYPES = {"adjust_bracket", "close"}
TV_TRANSIENT_REVERSE_SOURCE_VALUES = {
    "tv",
    "tradingview",
    "tv_webhook",
    "webhook_tv",
    "tradingview_webhook",
}

ACTIVE_SIGNAL_STATUSES = {
    "pending",
    "awaiting_confirm",
    "generated",
    "submitted",
    "accepted",
    "init",
    "partially_filled",
    "partialfilled",
}

PROTECTED_CORE_COLLECTIONS = frozenset(
    {
        "orders",
        "ibkr_orders",
        "ibkr_order_details",
        "ibkr_signals",
        "ibkr_reverse_signals",
        "ibkr_targets",
        "watchlist",
        "config",
        "ibkr_state",
    }
)

STANDARD_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "collection": "ibkr_indicators",
        "mode": "truncate_collection",
        "include_legacy_empty": True,
        "reason": "rebuildable_indicator_cache",
        "sort": "created",
    },
    {
        "collection": "ibkr_bars",
        "field": "bar_time_ms",
        "kind": "ms",
        "retention_days": 90,
        "include_legacy_empty": True,
        "sort": "bar_time_ms",
    },
    {
        "collection": "tv_webhook_events",
        "field": "created",
        "kind": "created_text",
        "retention_days": 90,
        "include_legacy_empty": True,
        "sort": "created",
    },
    {
        "collection": "system_events",
        "field": "created",
        "kind": "created_text",
        "retention_days": 30,
        "sort": "created",
    },
    {
        "collection": "ibkr_bar_coverage_daily",
        "field": "market_date",
        "kind": "date_text",
        "retention_days": 30,
        "sort": "market_date",
    },
    {
        "collection": "ibkr_bar_integrity",
        "field": "market_date",
        "kind": "date_text",
        "retention_days": 30,
        "sort": "market_date",
    },
    {
        "collection": "ibkr_bar_truth_audit",
        "field": "market_date",
        "kind": "date_text",
        "retention_days": 30,
        "sort": "market_date",
    },
    {
        "collection": "ibkr_bar_truth_repair_events",
        "field": "market_date",
        "kind": "date_text",
        "retention_days": 30,
        "sort": "market_date",
    },
    {
        "collection": "tv_indicators",
        "field": "bar_time_ms",
        "kind": "ms",
        "retention_days": 30,
        "include_legacy_empty": True,
        "sort": "bar_time_ms",
    },
    {
        "collection": "tv_indicator_audit_snapshots",
        "field": "bar_time_ms",
        "kind": "ms",
        "retention_days": 30,
        "include_legacy_empty": True,
        "sort": "bar_time_ms",
    },
    {
        "collection": "ibkr_cache_snapshots",
        "mode": "snapshot_cache_retention",
        "field": "stale_until_ms",
        "kind": "ms",
        "retention_days": 1,
        "reason": "expired_ui_snapshot_cache",
        "run_once": True,
        "sort": "stale_until_ms",
    },
)

BACKTEST_CHILD_TABLES = (
    "ibkr_backtest_trades",
    "ibkr_backtest_signals",
    "ibkr_backtest_targets",
    "ibkr_backtest_reverse_signals",
)


def _normalize_environment(value: str) -> str:
    text = str(value or "").strip().lower()
    return text or "live"


def _unique_environments(values: Iterable[str] | None) -> list[str]:
    output: list[str] = []
    seen = set()
    for raw in values or []:
        environment = _normalize_environment(str(raw or ""))
        if environment in seen:
            continue
        seen.add(environment)
        output.append(environment)
    return output


def _parse_csv(value: Any, defaults: Iterable[str] = ()) -> list[str]:
    items: list[str] = []
    seen = set()
    raw_values: list[Any]
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = str(value or "").replace("\n", ",").split(",")
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    for raw in defaults:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _pb_quote(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _build_environment_filter(environment: str, include_legacy_empty: bool = False) -> str:
    runtime_environment = _normalize_environment(environment)
    clauses = [f'environment = "{_pb_quote(runtime_environment)}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def _safe_identifier(name: str) -> str:
    text = str(name or "").strip()
    if not text.replace("_", "").isalnum():
        raise ValueError(f"unsafe sqlite identifier: {text}")
    return '"' + text.replace('"', '""') + '"'


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_profile(value: Any) -> str:
    return str(value or "").strip().lower() or DEFAULT_PROFILE


def _resolve_profile(value: Any) -> tuple[str, str | None]:
    requested = _normalize_profile(value)
    return requested, PROFILE_ALIASES.get(requested)


def _compact_payload(value: Any, *, max_items: int = 40, max_text: int = 900) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                output["truncated"] = True
                break
            output[str(key)] = _compact_payload(item, max_items=max_items, max_text=max_text)
        return output
    if isinstance(value, list):
        compacted = [_compact_payload(item, max_items=max_items, max_text=max_text) for item in value[:max_items]]
        if len(value) > max_items:
            compacted.append({"truncated": len(value) - max_items})
        return compacted
    if isinstance(value, str) and len(value) > max_text:
        return value[:max_text] + "..."
    return value


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _timestamp_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0
        return int(number if number >= 100_000_000_000 else number * 1000)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = float(text)
    except Exception:
        number = 0.0
    if number > 0:
        return int(number if number >= 100_000_000_000 else number * 1000)
    normalized = text.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _parse_hhmm(value: Any, default: str = "15:55") -> tuple[int, int]:
    text = str(value or default).strip() or default
    try:
        hour_text, minute_text = text.split(":", 1)
        return max(0, min(23, int(hour_text))), max(0, min(59, int(minute_text)))
    except Exception:
        return (15, 55)


class StorageCleanup:
    def __init__(
        self,
        pb_client,
        config=None,
        default_environments: Iterable[str] | None = None,
        *,
        batch_size: int | None = None,
        recent_backtest_limit: int | None = None,
        policies: Iterable[dict[str, Any]] | None = None,
    ):
        self.pb_client = pb_client
        self.config = config
        self.default_environments = _unique_environments(default_environments or ["live"])
        self.batch_size = max(20, int(batch_size or DEFAULT_BATCH_SIZE))
        self.recent_backtest_limit = max(5, int(recent_backtest_limit or DEFAULT_RECENT_BACKTEST_LIMIT))
        self.policies = list(policies or STANDARD_POLICIES)
        self._last_summary: dict[str, Any] = {}

    def _config_value(self, key: str, environment: str, default: Any) -> Any:
        if not self.config:
            return default
        try:
            if hasattr(self.config, "get_for_environment"):
                return self.config.get_for_environment(key, environment, str(default))
            if hasattr(self.config, "get"):
                return self.config.get(key, str(default))
        except Exception:
            return default
        return default

    def _config_bool(self, key: str, environment: str, default: bool) -> bool:
        if self.config and hasattr(self.config, "get_bool_for_environment"):
            try:
                return bool(self.config.get_bool_for_environment(key, environment, default))
            except Exception:
                return default
        text = str(self._config_value(key, environment, str(default).lower()) or "").strip().lower()
        return text in {"true", "1", "yes", "on"}

    def _config_int(self, key: str, environment: str, default: int) -> int:
        if self.config and hasattr(self.config, "get_int_for_environment"):
            try:
                return int(self.config.get_int_for_environment(key, environment, default))
            except Exception:
                return default
        try:
            return int(float(self._config_value(key, environment, default)))
        except Exception:
            return default

    def _config_float(self, key: str, environment: str, default: float) -> float:
        if self.config and hasattr(self.config, "get_float_for_environment"):
            try:
                return float(self.config.get_float_for_environment(key, environment, default))
            except Exception:
                return default
        try:
            return float(self._config_value(key, environment, default))
        except Exception:
            return default

    def _config_csv(self, key: str, environment: str, defaults: Iterable[str]) -> list[str]:
        return _parse_csv(self._config_value(key, environment, ",".join(defaults)), defaults)

    def _disk_snapshot(self) -> dict[str, Any]:
        db_path = Path(str(pocketbase_sqlite.PB_SQLITE_PATH or "/opt/pocketbase/pb_data/data.db"))
        snapshot: dict[str, Any] = {
            "db_path": str(db_path),
            "db_size_bytes": 0,
            "wal_size_bytes": 0,
            "shm_size_bytes": 0,
            "page_size": 0,
            "page_count": 0,
            "freelist_count": 0,
            "freelist_bytes": 0,
            "freelist_ratio": 0.0,
            "disk_free_bytes": 0,
            "disk_total_bytes": 0,
            "disk_free_pct": None,
            "available": False,
            "error": "",
        }
        try:
            snapshot["db_size_bytes"] = int(db_path.stat().st_size)
        except OSError:
            pass
        for suffix, key in (("-wal", "wal_size_bytes"), ("-shm", "shm_size_bytes")):
            try:
                snapshot[key] = int(Path(f"{db_path}{suffix}").stat().st_size)
            except OSError:
                pass
        try:
            usage = shutil.disk_usage(str(db_path.parent))
            snapshot["disk_total_bytes"] = int(usage.total)
            snapshot["disk_free_bytes"] = int(usage.free)
            snapshot["disk_free_pct"] = round((usage.free / usage.total) * 100.0, 2) if usage.total else None
        except Exception as exc:
            snapshot["error"] = str(exc)
        try:
            conn = pocketbase_sqlite.open_pb_sqlite(readonly=True, timeout=5.0)
            try:
                page_size = int(conn.execute("pragma page_size").fetchone()[0] or 0)
                page_count = int(conn.execute("pragma page_count").fetchone()[0] or 0)
                freelist_count = int(conn.execute("pragma freelist_count").fetchone()[0] or 0)
                snapshot["page_size"] = page_size
                snapshot["page_count"] = page_count
                snapshot["freelist_count"] = freelist_count
                snapshot["freelist_bytes"] = page_size * freelist_count if page_size > 0 else 0
                snapshot["freelist_ratio"] = round(freelist_count / page_count, 4) if page_count > 0 else 0.0
                snapshot["available"] = True
            finally:
                conn.close()
        except Exception as exc:
            snapshot["error"] = str(exc)
        return snapshot

    def _needs_vacuum(self, snapshot: dict[str, Any], environment: str) -> bool:
        freelist_bytes = int(snapshot.get("freelist_bytes") or 0)
        freelist_ratio = float(snapshot.get("freelist_ratio") or 0.0)
        free_pct = snapshot.get("disk_free_pct")
        warn_gb = max(0.1, self._config_float("storage_cleanup_vacuum_warn_gb", environment, 1.0))
        warn_ratio = max(0.02, self._config_float("storage_cleanup_vacuum_warn_ratio", environment, 0.15))
        low_free_pct = max(1, self._config_int("storage_cleanup_disk_low_free_pct", environment, 15))
        if freelist_bytes >= int(warn_gb * GIB):
            return True
        if freelist_ratio >= warn_ratio:
            return True
        if isinstance(free_pct, (int, float)) and float(free_pct) < low_free_pct:
            return True
        return False

    def _open_readonly(self) -> sqlite3.Connection | None:
        try:
            return pocketbase_sqlite.open_pb_sqlite(readonly=True, timeout=8.0)
        except Exception:
            return None

    def _count_sql(self, table: str, where: str, params: tuple[Any, ...] = ()) -> tuple[int | None, str]:
        conn = self._open_readonly()
        if conn is None:
            return None, "unavailable"
        try:
            table_ident = _safe_identifier(table)
            row = conn.execute(f"select count(*) as total from {table_ident} where {where}", params).fetchone()
            return int(row["total"] or 0), "sqlite"
        except Exception as exc:
            return None, f"error:{exc}"
        finally:
            conn.close()

    def _fetch_sql_rows(
        self,
        table: str,
        columns: str,
        where: str,
        params: tuple[Any, ...],
        *,
        order_by: str = "created desc",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        conn = self._open_readonly()
        if conn is None:
            return []
        try:
            table_ident = _safe_identifier(table)
            rows = conn.execute(
                f"select {columns} from {table_ident} where {where} order by {order_by} limit ?",
                (*params, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def _environment_sql_clause(self, environment: str, include_legacy_empty: bool = False) -> tuple[str, tuple[Any, ...]]:
        runtime_environment = _normalize_environment(environment)
        if include_legacy_empty and runtime_environment == "live":
            return "(environment = ? or environment = '')", (runtime_environment,)
        return "environment = ?", (runtime_environment,)

    def _status_not_in_filters(self, statuses: Iterable[str]) -> tuple[str, str, tuple[Any, ...]]:
        normalized = [str(item or "").strip() for item in statuses or [] if str(item or "").strip()]
        if not normalized:
            return "", "", ()
        pb_filter = " && ".join(f'status != "{_pb_quote(item)}"' for item in normalized)
        placeholders = ",".join("?" for _ in normalized)
        return pb_filter, f"status not in ({placeholders})", tuple(normalized)

    def _state_key_filters(self, state_key: str) -> tuple[str, str, tuple[Any, ...]]:
        text = str(state_key or "").strip()
        if not text:
            return "", "", ()
        return f'state_key = "{_pb_quote(text)}"', "state_key = ?", (text,)

    def _cutoff_for_policy(self, policy: dict[str, Any], now_et: datetime) -> tuple[Any, str]:
        days = max(1, int(policy.get("retention_days") or 1))
        kind = str(policy.get("kind") or "ms").strip().lower()
        if kind == "ms":
            cutoff = now_et - timedelta(days=days)
            return int(cutoff.timestamp() * 1000), cutoff.strftime("%Y-%m-%d %H:%M:%S")
        if kind == "created_text":
            cutoff_utc = now_et.astimezone(timezone.utc) - timedelta(days=days)
            return cutoff_utc.strftime("%Y-%m-%d %H:%M:%S"), cutoff_utc.strftime("%Y-%m-%d %H:%M:%S")
        if kind == "date_hour_text":
            cutoff = now_et - timedelta(days=days)
            return cutoff.strftime("%Y-%m-%d %H"), cutoff.strftime("%Y-%m-%d %H")
        cutoff = now_et - timedelta(days=days)
        return cutoff.strftime("%Y-%m-%d"), cutoff.strftime("%Y-%m-%d")

    def _policy_filters(self, policy: dict[str, Any], environment: str, now_et: datetime) -> tuple[str, str, tuple[Any, ...], Any]:
        collection = str(policy.get("collection") or "")
        field = str(policy.get("field") or "")
        kind = str(policy.get("kind") or "ms").strip().lower()
        cutoff_value, _ = self._cutoff_for_policy(policy, now_et)
        pb_parts: list[str] = []
        sql_parts: list[str] = []
        sql_params: list[Any] = []

        env_pb = _build_environment_filter(environment, include_legacy_empty=bool(policy.get("include_legacy_empty")))
        env_sql, env_params = self._environment_sql_clause(environment, bool(policy.get("include_legacy_empty")))
        pb_parts.append(env_pb)
        sql_parts.append(env_sql)
        sql_params.extend(env_params)

        if kind == "ms":
            pb_parts.append(f"{field} > 0")
            pb_parts.append(f"{field} < {int(cutoff_value)}")
            sql_parts.append(f"{_safe_identifier(field)} > 0")
            sql_parts.append(f"{_safe_identifier(field)} < ?")
            sql_params.append(int(cutoff_value))
        else:
            pb_parts.append(f"{field} != ''")
            pb_parts.append(f"{field} < '{_pb_quote(cutoff_value)}'")
            sql_parts.append(f"{_safe_identifier(field)} != ''")
            sql_parts.append(f"{_safe_identifier(field)} < ?")
            sql_params.append(str(cutoff_value))

        status_pb, status_sql, status_params = self._status_not_in_filters(policy.get("status_not_in") or [])
        if status_pb:
            pb_parts.append(status_pb)
            sql_parts.append(status_sql)
            sql_params.extend(status_params)

        state_pb, state_sql, state_params = self._state_key_filters(str(policy.get("state_key") or ""))
        if state_pb:
            pb_parts.append(state_pb)
            sql_parts.append(state_sql)
            sql_params.extend(state_params)
            pb_parts.append('date != "global"')
            sql_parts.append("date != ?")
            sql_params.append("global")

        return " && ".join(pb_parts), " and ".join(sql_parts), tuple(sql_params), cutoff_value

    def _delete_records(self, collection: str, pb_filter: str, sort: str, *, dry_run: bool) -> dict[str, Any]:
        if collection in PROTECTED_CORE_COLLECTIONS:
            return {
                "deleted": 0,
                "errors": 0,
                "batches": 0,
                "dry_run": bool(dry_run),
                "skipped": True,
                "reason": "protected_core_collection",
            }
        if dry_run:
            return {"deleted": 0, "errors": 0, "batches": 0, "dry_run": True}

        deleted = 0
        errors = 0
        batches = 0
        while True:
            rows = self.pb_client.get_records(
                collection,
                filter=pb_filter,
                sort=sort,
                per_page=self.batch_size,
                page=1,
            )
            if not rows:
                break
            batches += 1
            record_ids = [str(record.get("id") or "").strip() for record in rows if str(record.get("id") or "").strip()]
            missing_ids = len(rows) - len(record_ids)
            errors += missing_ids
            batch_errors = missing_ids
            if record_ids:
                try:
                    if hasattr(self.pb_client, "delete_records"):
                        self.pb_client.delete_records(
                            collection,
                            record_ids,
                            timeout=60,
                            batch_size=min(100, max(1, self.batch_size)),
                        )
                        deleted += len(record_ids)
                    else:
                        for record_id in record_ids:
                            self.pb_client.delete_record(collection, record_id)
                            deleted += 1
                except Exception as exc:
                    logger.warning(
                        "Storage cleanup batch delete failed: collection=%s count=%s error=%s",
                        collection,
                        len(record_ids),
                        exc,
                    )
                    errors += len(record_ids)
                    batch_errors += len(record_ids)
            if batch_errors:
                break
            if len(rows) < self.batch_size:
                break
        return {"deleted": deleted, "errors": errors, "batches": batches, "dry_run": False}

    def _fetch_pb_records(self, collection: str, pb_filter: str, sort: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        per_page = min(500, max(20, self.batch_size))
        for page in range(1, max(1, int(max_pages or 1)) + 1):
            batch = self.pb_client.get_records(
                collection,
                filter=pb_filter or None,
                sort=sort or None,
                per_page=per_page,
                page=page,
            ) or []
            rows.extend(dict(item) for item in batch if isinstance(item, dict))
            if len(batch) < per_page:
                break
        return rows

    def _delete_record_ids(self, collection: str, record_ids: list[str], *, dry_run: bool) -> dict[str, Any]:
        safe_ids = [str(record_id or "").strip() for record_id in record_ids if str(record_id or "").strip()]
        if dry_run or not safe_ids:
            return {"deleted": 0, "errors": 0, "batches": 0, "dry_run": bool(dry_run)}
        deleted = 0
        errors = 0
        batches = 0
        for offset in range(0, len(safe_ids), self.batch_size):
            batch_ids = safe_ids[offset:offset + self.batch_size]
            batches += 1
            try:
                if hasattr(self.pb_client, "delete_records"):
                    self.pb_client.delete_records(
                        collection,
                        batch_ids,
                        timeout=60,
                        batch_size=min(100, max(1, self.batch_size)),
                    )
                    deleted += len(batch_ids)
                else:
                    for record_id in batch_ids:
                        self.pb_client.delete_record(collection, record_id)
                        deleted += 1
            except Exception as exc:
                logger.warning(
                    "TV transient cleanup delete failed: collection=%s count=%s error=%s",
                    collection,
                    len(batch_ids),
                    exc,
                )
                errors += len(batch_ids)
        return {"deleted": deleted, "errors": errors, "batches": batches, "dry_run": False}

    def _past_eod_lifecycle(
        self,
        row: dict[str, Any],
        now_et: datetime,
        *,
        environment: str,
        updated: bool = True,
    ) -> bool:
        stamp = _timestamp_ms(row.get("updated") if updated else row.get("created"))
        if stamp <= 0:
            stamp = _timestamp_ms(row.get("created") or row.get("bar_time_ms"))
        if stamp <= 0:
            return False
        row_et = datetime.fromtimestamp(stamp / 1000.0, ET)
        hour, minute = _parse_hhmm(self._config_value("eod_close_time", environment, "15:55"), "15:55")
        row_cutoff = row_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now_et >= row_cutoff

    @staticmethod
    def _broker_mode_from_extra(extra: dict[str, Any]) -> str:
        for key in ("broker_mode", "last_runtime_broker_mode"):
            text = str(extra.get(key) or "").strip().lower()
            if text in {"paper", "sim", "simulated", "simulation"}:
                return "paper"
            if text in {"live", "prod", "production"}:
                return "live"
        return ""

    @staticmethod
    def _execution_status(extra: dict[str, Any], broker_mode: str) -> str:
        execution_by_mode = extra.get("execution_by_mode") if isinstance(extra.get("execution_by_mode"), dict) else {}
        payload = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
        return str((payload or {}).get("status") or "").strip().lower() if isinstance(payload, dict) else ""

    def _has_matching_order(self, signal_id: str, broker_environment: str) -> bool:
        text = str(signal_id or "").strip()
        if not text:
            return False
        for environment in (broker_environment, "live"):
            try:
                rows = self.pb_client.get_records(
                    "orders",
                    filter=f'signal_id = "{_pb_quote(text)}" && environment = "{_pb_quote(environment)}"',
                    sort="-created",
                    per_page=1,
                    page=1,
                )
            except Exception:
                rows = []
            if rows:
                return True
        return False

    @staticmethod
    def _has_direct_order_trace(row: dict[str, Any], extra: dict[str, Any]) -> bool:
        keys = (
            "order_id",
            "broker_order_id",
            "entry_order_unique_id",
            "order_unique_id",
            "parent_order_unique_id",
            "trade_group_id",
        )
        nested_payloads = (
            _as_object(extra.get("reverse_runtime_detail")),
            _as_object(extra.get("reentry_blocked")),
        )
        return any(
            str(row.get(key) or extra.get(key) or "").strip()
            or any(str(payload.get(key) or "").strip() for payload in nested_payloads)
            for key in keys
        )

    def _tv_transient_signal_candidate(self, row: dict[str, Any], now_et: datetime, *, environment: str) -> bool:
        extra = _as_object(row.get("extra"))
        if str(row.get("environment") or "").strip().lower() != "live":
            return False
        if str(row.get("status") or "").strip().lower() not in {"pending", "awaiting_confirm"}:
            return False
        if self._broker_mode_from_extra(extra) != "paper":
            return False
        if self._execution_status(extra, "paper") not in TV_TRANSIENT_SIGNAL_TERMINAL_STATUSES:
            return False
        if str(row.get("order_id") or "").strip():
            return False
        if self._has_matching_order(str(row.get("signal_id") or ""), "paper"):
            return False
        return self._past_eod_lifecycle(row, now_et, environment=environment, updated=True)

    @staticmethod
    def _tv_reverse_failure_reason(row: dict[str, Any], extra: dict[str, Any]) -> str:
        runtime_detail = _as_object(extra.get("reverse_runtime_detail"))
        blocked_detail = _as_object(extra.get("reentry_blocked"))
        candidates = (
            extra.get("flow_error_code"),
            extra.get("error"),
            blocked_detail.get("reason"),
            runtime_detail.get("blocked_reason"),
            row.get("reason"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip().lower()
            for reason in TV_TRANSIENT_REVERSE_REASONS:
                if reason in text:
                    return reason
        return ""

    def _tv_transient_reverse_candidate(self, row: dict[str, Any], now_et: datetime, *, environment: str) -> bool:
        extra = _as_object(row.get("extra"))
        if str(row.get("environment") or "").strip().lower() != "paper":
            return False
        if str(row.get("source") or extra.get("source") or "").strip().lower() not in TV_TRANSIENT_REVERSE_SOURCE_VALUES:
            return False
        if str(row.get("status") or "").strip().lower() not in {"cancelled", "canceled", "expired"}:
            return False
        if str(row.get("action_type") or "").strip().lower() not in TV_TRANSIENT_REVERSE_ACTION_TYPES:
            return False
        if not self._tv_reverse_failure_reason(row, extra):
            return False
        if self._has_direct_order_trace(row, extra):
            return False
        return self._past_eod_lifecycle(row, now_et, environment=environment, updated=True)

    def _run_tv_transient_cleanup(self, environment: str, now_et: datetime, *, dry_run: bool) -> dict[str, Any]:
        if not self._config_bool("storage_cleanup_tv_transient_enabled", environment, True):
            return {
                "enabled": False,
                "skipped": True,
                "reason": "storage_cleanup_tv_transient_disabled",
                "estimated": 0,
                "deleted": 0,
                "errors": 0,
                "collections": [],
            }

        signal_rows: list[dict[str, Any]] = []
        for status in ("pending", "awaiting_confirm"):
            signal_rows.extend(
                self._fetch_pb_records(
                    "ibkr_signals",
                    f'environment = "live" && status = "{status}"',
                    "-updated",
                )
            )
        signal_ids = [
            str(row.get("id") or "").strip()
            for row in signal_rows
            if self._tv_transient_signal_candidate(row, now_et, environment=environment)
        ]

        reverse_rows: list[dict[str, Any]] = []
        for status in ("cancelled", "canceled", "expired"):
            reverse_rows.extend(
                self._fetch_pb_records(
                    "ibkr_reverse_signals",
                    f'environment = "paper" && status = "{status}"',
                    "-updated",
                )
            )
        reverse_ids = [
            str(row.get("id") or "").strip()
            for row in reverse_rows
            if self._tv_transient_reverse_candidate(row, now_et, environment=environment)
        ]

        signal_delete = self._delete_record_ids("ibkr_signals", signal_ids, dry_run=dry_run)
        reverse_delete = self._delete_record_ids("ibkr_reverse_signals", reverse_ids, dry_run=dry_run)
        collections = [
            {
                "collection": "ibkr_signals",
                "estimated": len(signal_ids),
                **signal_delete,
            },
            {
                "collection": "ibkr_reverse_signals",
                "estimated": len(reverse_ids),
                **reverse_delete,
            },
        ]
        return {
            "enabled": True,
            "skipped": False,
            "broker_environment": "paper",
            "data_environment": "live",
            "estimated": len(signal_ids) + len(reverse_ids),
            "deleted": int(signal_delete.get("deleted") or 0) + int(reverse_delete.get("deleted") or 0),
            "errors": int(signal_delete.get("errors") or 0) + int(reverse_delete.get("errors") or 0),
            "dry_run": bool(dry_run),
            "reasons": sorted(TV_TRANSIENT_REVERSE_REASONS),
            "reverse_source_values": sorted(TV_TRANSIENT_REVERSE_SOURCE_VALUES),
            "signal_terminal_statuses": sorted(TV_TRANSIENT_SIGNAL_TERMINAL_STATUSES),
            "collections": collections,
        }

    def _run_standard_policy(self, policy: dict[str, Any], environment: str, now_et: datetime, *, dry_run: bool) -> dict[str, Any]:
        collection = str(policy.get("collection") or "")
        mode = str(policy.get("mode") or "")
        if collection in PROTECTED_CORE_COLLECTIONS:
            return {
                "collection": collection,
                "mode": mode or "standard_retention",
                "estimated": 0,
                "deleted": 0,
                "errors": 0,
                "batches": 0,
                "dry_run": bool(dry_run),
                "skipped": True,
                "reason": "protected_core_collection",
            }
        if mode == "external":
            return {
                "collection": collection,
                "mode": "external",
                "retention_days": int(policy.get("retention_days") or 0),
                "reason": str(policy.get("reason") or ""),
                "estimated": 0,
                "deleted": 0,
                "errors": 0,
                "skipped": True,
            }
        if mode == "snapshot_cache_retention":
            return self._run_snapshot_cache_cleanup(policy, now_et, dry_run=dry_run)
        if mode == "truncate_collection":
            pb_filter = _build_environment_filter(environment, include_legacy_empty=bool(policy.get("include_legacy_empty")))
            sql_where, sql_params = self._environment_sql_clause(environment, bool(policy.get("include_legacy_empty")))
            count, count_source = self._count_sql(collection, sql_where, sql_params)
            delete_result = self._delete_records(collection, pb_filter, str(policy.get("sort") or "created"), dry_run=dry_run)
            return {
                "collection": collection,
                "mode": mode,
                "reason": str(policy.get("reason") or ""),
                "filter": pb_filter,
                "estimated": int(count or 0) if count is not None else None,
                "count_source": count_source,
                **delete_result,
            }

        pb_filter, sql_where, sql_params, cutoff_value = self._policy_filters(policy, environment, now_et)
        count, count_source = self._count_sql(collection, sql_where, sql_params)
        delete_result = self._delete_records(
            collection,
            pb_filter,
            str(policy.get("sort") or policy.get("field") or "created"),
            dry_run=dry_run,
        )
        return {
            "collection": collection,
            "field": str(policy.get("field") or ""),
            "kind": str(policy.get("kind") or ""),
            "retention_days": int(policy.get("retention_days") or 0),
            "cutoff": cutoff_value,
            "filter": pb_filter,
            "estimated": int(count or 0) if count is not None else None,
            "count_source": count_source,
            **delete_result,
        }

    def _run_snapshot_cache_cleanup(self, policy: dict[str, Any], now_et: datetime, *, dry_run: bool) -> dict[str, Any]:
        collection = str(policy.get("collection") or "ibkr_cache_snapshots")
        cutoff_value, _ = self._cutoff_for_policy(policy, now_et)
        cutoff_ms = int(cutoff_value)
        filters = [
            f"stale_until_ms > 0 && stale_until_ms < {cutoff_ms}",
            'status = "expired"',
            f'status = "error" && stale_until_ms < {cutoff_ms}',
            "stale_until_ms < 1",
        ]
        rows_by_id: dict[str, dict[str, Any]] = {}
        filter_counts: list[dict[str, Any]] = []
        for filter_text in filters:
            rows = self._fetch_pb_records(
                collection,
                filter_text,
                str(policy.get("sort") or "stale_until_ms"),
                max_pages=200,
            )
            filter_counts.append({"filter": filter_text, "matched": len(rows)})
            for row in rows:
                record_id = str(row.get("id") or "").strip()
                if record_id:
                    rows_by_id.setdefault(record_id, row)
        record_ids = sorted(rows_by_id)
        delete_result = self._delete_record_ids(collection, record_ids, dry_run=dry_run)
        return {
            "collection": collection,
            "mode": "snapshot_cache_retention",
            "field": str(policy.get("field") or "stale_until_ms"),
            "kind": str(policy.get("kind") or "ms"),
            "retention_days": int(policy.get("retention_days") or 0),
            "cutoff": cutoff_ms,
            "reason": str(policy.get("reason") or ""),
            "environment_scope": "all",
            "filters": filter_counts,
            "filter": " || ".join(f"({item})" for item in filters),
            "estimated": len(record_ids),
            "count_source": "pocketbase_unique_ids",
            **delete_result,
        }

    def _pb_recent_ids(self, collection: str, *, sort: str, limit: int, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        try:
            rows = self.pb_client.get_records(collection, sort=sort, per_page=limit, page=1)
        except Exception:
            return []
        output = []
        for row in rows:
            output.append({column: row.get(column) for column in columns})
        return output

    def _resolve_backtest_keep_sets(self, environment: str) -> dict[str, Any]:
        protected_batch_ids = set(
            self._config_csv("storage_cleanup_protected_batch_ids", environment, DEFAULT_PROTECTED_BATCH_IDS)
        )
        protected_run_ids = set(self._config_csv("storage_cleanup_protected_run_ids", environment, DEFAULT_PROTECTED_RUN_IDS))
        recent_limit = max(5, self._config_int("storage_cleanup_backtest_recent_limit", environment, self.recent_backtest_limit))

        recent_batches = self._fetch_sql_rows(
            "ibkr_backtest_batches",
            "id, best_run_id",
            "1=1",
            (),
            order_by="created desc",
            limit=recent_limit,
        )
        if not recent_batches:
            recent_batches = self._pb_recent_ids(
                "ibkr_backtest_batches",
                sort="-created",
                limit=recent_limit,
                columns=("id", "best_run_id"),
            )

        recent_runs = self._fetch_sql_rows(
            "ibkr_backtest_runs",
            "id",
            "1=1",
            (),
            order_by="created desc",
            limit=recent_limit,
        )
        if not recent_runs:
            recent_runs = self._pb_recent_ids("ibkr_backtest_runs", sort="-created", limit=recent_limit, columns=("id",))

        keep_batch_ids = set(protected_batch_ids)
        keep_run_ids = set(protected_run_ids)
        for row in recent_batches:
            batch_id = str(row.get("id") or "").strip()
            best_run_id = str(row.get("best_run_id") or "").strip()
            if batch_id:
                keep_batch_ids.add(batch_id)
            if best_run_id:
                keep_run_ids.add(best_run_id)
        for row in recent_runs:
            run_id = str(row.get("id") or "").strip()
            if run_id:
                keep_run_ids.add(run_id)

        if protected_batch_ids:
            placeholders = ",".join("?" for _ in protected_batch_ids)
            protected_rows = self._fetch_sql_rows(
                "ibkr_backtest_batches",
                "id, best_run_id",
                f"id in ({placeholders})",
                tuple(protected_batch_ids),
                order_by="created desc",
                limit=max(1, len(protected_batch_ids)),
            )
            for row in protected_rows:
                best_run_id = str(row.get("best_run_id") or "").strip()
                if best_run_id:
                    keep_run_ids.add(best_run_id)

        return {
            "keep_batch_ids": sorted(keep_batch_ids),
            "keep_run_ids": sorted(keep_run_ids),
            "protected_batch_ids": sorted(protected_batch_ids),
            "protected_run_ids": sorted(protected_run_ids),
            "recent_limit": recent_limit,
        }

    def _not_in_filters(self, field: str, keep_ids: Iterable[str]) -> tuple[str, str, tuple[Any, ...]]:
        ids = [str(item or "").strip() for item in keep_ids if str(item or "").strip()]
        if not ids:
            return "1 = 1", "1 = 1", ()
        pb_filter = " && ".join(f'{field} != "{_pb_quote(item)}"' for item in ids)
        placeholders = ",".join("?" for _ in ids)
        return pb_filter, f"{_safe_identifier(field)} not in ({placeholders})", tuple(ids)

    def _backtest_delete_policy(
        self,
        collection: str,
        field: str,
        keep_ids: Iterable[str],
        *,
        sort: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        if collection in PROTECTED_CORE_COLLECTIONS:
            return {
                "collection": collection,
                "field": field,
                "mode": "backtest_retention_limit",
                "estimated": 0,
                "deleted": 0,
                "errors": 0,
                "batches": 0,
                "dry_run": bool(dry_run),
                "skipped": True,
                "reason": "protected_core_collection",
            }
        pb_filter, sql_where, sql_params = self._not_in_filters(field, keep_ids)
        count, count_source = self._count_sql(collection, sql_where, sql_params)
        delete_result = self._delete_records(collection, pb_filter, sort, dry_run=dry_run)
        return {
            "collection": collection,
            "field": field,
            "mode": "backtest_retention_limit",
            "keep_count": len([item for item in keep_ids if str(item or "").strip()]),
            "filter": pb_filter,
            "estimated": int(count or 0) if count is not None else None,
            "count_source": count_source,
            **delete_result,
        }

    def _backtest_daily_cache_policy(self, now_et: datetime, *, dry_run: bool) -> dict[str, Any]:
        retention_days = 30
        cutoff_date = (now_et - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        pb_filter = f"market_date != '' && market_date < '{_pb_quote(cutoff_date)}'"
        sql_where = "market_date != '' and market_date < ?"
        count, count_source = self._count_sql("ibkr_backtest_daily_selection_cache", sql_where, (cutoff_date,))
        delete_result = self._delete_records(
            "ibkr_backtest_daily_selection_cache",
            pb_filter,
            "market_date",
            dry_run=dry_run,
        )
        return {
            "collection": "ibkr_backtest_daily_selection_cache",
            "field": "market_date",
            "mode": "backtest_cache_retention",
            "retention_days": retention_days,
            "cutoff": cutoff_date,
            "filter": pb_filter,
            "estimated": int(count or 0) if count is not None else None,
            "count_source": count_source,
            **delete_result,
        }

    def _run_backtest_policies(self, environment: str, now_et: datetime, *, dry_run: bool) -> dict[str, Any]:
        keep = self._resolve_backtest_keep_sets(environment)
        policies = []
        for table in BACKTEST_CHILD_TABLES:
            policies.append(
                self._backtest_delete_policy(
                    table,
                    "run_id",
                    keep["keep_run_ids"],
                    sort="run_id",
                    dry_run=dry_run,
                )
            )
        policies.append(
            self._backtest_delete_policy(
                "ibkr_backtest_runs",
                "id",
                keep["keep_run_ids"],
                sort="created",
                dry_run=dry_run,
            )
        )
        policies.append(
            self._backtest_delete_policy(
                "ibkr_backtest_batches",
                "id",
                keep["keep_batch_ids"],
                sort="created",
                dry_run=dry_run,
            )
        )
        policies.append(self._backtest_daily_cache_policy(now_et, dry_run=dry_run))
        return {"keep": keep, "policies": policies}

    def _get_daily_state(self, environment: str, day_token: str) -> dict[str, Any]:
        if not self.pb_client or not hasattr(self.pb_client, "get_state"):
            return {}
        try:
            row = self.pb_client.get_state(STORAGE_CLEANUP_STATE_KEY, environment, date=day_token)
        except Exception:
            return {}
        payload = (row or {}).get("data") if isinstance(row, dict) else {}
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def _mark_daily_state(self, environment: str, day_token: str, payload: dict[str, Any]) -> None:
        if not self.pb_client or not hasattr(self.pb_client, "upsert_state"):
            return
        try:
            self.pb_client.upsert_state(STORAGE_CLEANUP_STATE_KEY, environment, payload, date=day_token)
        except Exception as exc:
            logger.warning("Storage cleanup state write failed: environment=%s error=%s", environment, exc)

    def _write_system_event(self, environment: str, payload: dict[str, Any]) -> None:
        if not self.pb_client or not hasattr(self.pb_client, "create_record"):
            return
        now = datetime.now(timezone.utc)
        total_deleted = int(payload.get("total_deleted") or 0)
        total_errors = int(payload.get("total_errors") or 0)
        needs_vacuum = bool(payload.get("needs_vacuum"))
        level = "warning" if total_errors or needs_vacuum else "info"
        title = f"[{environment.upper()}] Storage cleanup deleted={total_deleted} errors={total_errors}"
        if needs_vacuum:
            title += " needs_vacuum"
        try:
            self.pb_client.create_record(
                "system_events",
                {
                    "event_type": "storage_cleanup",
                    "level": level,
                    "source": "ibkr_compute",
                    "environment": environment,
                    "title": title,
                    "detail": _compact_payload(payload),
                    "us_time": now.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "cn_time": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                    "notified": False,
                },
            )
        except Exception:
            return

    def cleanup(
        self,
        environments: Iterable[str] | None = None,
        *,
        dry_run: bool = True,
        force: bool = False,
        source: str = "",
        profile: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        runtime_environments = _unique_environments(environments or self.default_environments)
        now_et = now.astimezone(ET) if isinstance(now, datetime) else datetime.now(ET)
        day_token = now_et.strftime("%Y-%m-%d")
        source_name = str(source or "").strip().lower() or "manual"
        requested_profile, effective_profile = _resolve_profile(profile or DEFAULT_PROFILE)
        summary: dict[str, Any] = {
            "ok": True,
            "profile": effective_profile or requested_profile,
            "requested_profile": requested_profile,
            "profile_alias": requested_profile if effective_profile and requested_profile != effective_profile else "",
            "supported_profiles": list(SUPPORTED_PROFILES),
            "protected_collections": sorted(PROTECTED_CORE_COLLECTIONS),
            "dry_run": bool(dry_run),
            "force": bool(force),
            "source": source_name,
            "executed_at": now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "day_token": day_token,
            "batch_size": self.batch_size,
            "environments": [],
            "total_deleted": 0,
            "total_errors": 0,
            "needs_vacuum": False,
        }

        if not effective_profile:
            summary["ok"] = False
            summary["error"] = "unsupported_profile"
            self._last_summary = summary
            return summary

        run_once_policies_executed: set[str] = set()
        for environment in runtime_environments:
            enabled = self._config_bool("storage_cleanup_enabled", environment, True)
            env_result: dict[str, Any] = {
                "environment": environment,
                "enabled": enabled,
                "skipped": False,
                "policies": [],
                "backtest": {},
                "tv_transient_cleanup": {},
                "total_deleted": 0,
                "total_errors": 0,
            }
            if not enabled and not force:
                env_result["skipped"] = True
                env_result["reason"] = "storage_cleanup_disabled"
                summary["environments"].append(env_result)
                continue

            prior_state = self._get_daily_state(environment, day_token)
            if prior_state and not force and not dry_run:
                env_result["skipped"] = True
                env_result["reason"] = "already_executed_today"
                env_result["previous_execution"] = prior_state
                summary["environments"].append(env_result)
                continue

            env_result["disk_before"] = self._disk_snapshot()
            for policy in self.policies:
                policy_key = ":".join(
                    str(policy.get(key) or "")
                    for key in ("collection", "mode", "field", "reason")
                )
                if bool(policy.get("run_once")) and policy_key in run_once_policies_executed:
                    env_result["policies"].append(
                        {
                            "collection": str(policy.get("collection") or ""),
                            "mode": str(policy.get("mode") or "standard_retention"),
                            "estimated": 0,
                            "deleted": 0,
                            "errors": 0,
                            "batches": 0,
                            "dry_run": bool(dry_run),
                            "skipped": True,
                            "reason": "run_once_already_executed",
                        }
                    )
                    continue
                policy_result = self._run_standard_policy(policy, environment, now_et, dry_run=dry_run)
                if bool(policy.get("run_once")):
                    run_once_policies_executed.add(policy_key)
                env_result["policies"].append(policy_result)
                env_result["total_deleted"] += int(policy_result.get("deleted") or 0)
                env_result["total_errors"] += int(policy_result.get("errors") or 0)

            backtest_result = self._run_backtest_policies(environment, now_et, dry_run=dry_run)
            env_result["backtest"] = backtest_result
            for policy_result in backtest_result.get("policies") or []:
                env_result["total_deleted"] += int(policy_result.get("deleted") or 0)
                env_result["total_errors"] += int(policy_result.get("errors") or 0)

            tv_transient_result = self._run_tv_transient_cleanup(environment, now_et, dry_run=dry_run)
            env_result["tv_transient_cleanup"] = tv_transient_result
            env_result["total_deleted"] += int(tv_transient_result.get("deleted") or 0)
            env_result["total_errors"] += int(tv_transient_result.get("errors") or 0)

            env_result["disk_after"] = self._disk_snapshot()
            env_result["needs_vacuum"] = self._needs_vacuum(env_result["disk_after"], environment)
            summary["needs_vacuum"] = bool(summary["needs_vacuum"] or env_result["needs_vacuum"])
            summary["total_deleted"] += int(env_result["total_deleted"] or 0)
            summary["total_errors"] += int(env_result["total_errors"] or 0)
            if not dry_run:
                self._mark_daily_state(
                    environment,
                    day_token,
                    {
                        "completed": True,
                        "profile": effective_profile,
                        "requested_profile": requested_profile,
                        "source": source_name,
                        "executed_at": summary["executed_at"],
                        "total_deleted": env_result["total_deleted"],
                        "total_errors": env_result["total_errors"],
                        "needs_vacuum": env_result["needs_vacuum"],
                    },
                )
                self._write_system_event(environment, env_result)
            summary["environments"].append(env_result)

        summary["ok"] = int(summary["total_errors"] or 0) == 0
        self._last_summary = summary
        return summary

    def status(self) -> dict[str, Any]:
        return {
            "profile": DEFAULT_PROFILE,
            "supported_profiles": list(SUPPORTED_PROFILES),
            "protected_collections": sorted(PROTECTED_CORE_COLLECTIONS),
            "batch_size": self.batch_size,
            "recent_backtest_limit": self.recent_backtest_limit,
            "state_key": STORAGE_CLEANUP_STATE_KEY,
            "last_summary": self._last_summary,
            "policies": [str(policy.get("collection") or "") for policy in self.policies],
            "tv_transient_cleanup": {
                "enabled_default": True,
                "broker_environment": "paper",
                "data_environment": "live",
                "reverse_reasons": sorted(TV_TRANSIENT_REVERSE_REASONS),
                "reverse_source_values": sorted(TV_TRANSIENT_REVERSE_SOURCE_VALUES),
                "signal_terminal_statuses": sorted(TV_TRANSIENT_SIGNAL_TERMINAL_STATUSES),
            },
        }


__all__ = [
    "DEFAULT_PROFILE",
    "SUPPORTED_PROFILES",
    "PROTECTED_CORE_COLLECTIONS",
    "STORAGE_CLEANUP_STATE_KEY",
    "StorageCleanup",
]
