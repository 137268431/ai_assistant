"""
历史数据留存管理
- 按环境清理超过留存窗口的历史行情链路数据
- 默认保留近 365 天
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from ibkr_compute.core.time_utils import ET
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = max(30, int(os.environ.get("IBKR_HISTORY_RETENTION_DAYS", "365")))
RETENTION_BATCH_SIZE = max(20, int(os.environ.get("IBKR_HISTORY_RETENTION_BATCH_SIZE", "200")))
RETENTION_STATE_KEY = "ibkr_history_retention"

DEFAULT_RETENTION_POLICIES = (
    {
        "collection": "ibkr_bars",
        "field": "bar_time_ms",
        "kind": "ms",
        "include_legacy_empty": True,
    },
    {
        "collection": "ibkr_indicators",
        "field": "bar_time_ms",
        "kind": "ms",
        "include_legacy_empty": True,
    },
    {
        "collection": "ibkr_signals",
        "field": "bar_time_ms",
        "kind": "ms",
        "include_legacy_empty": True,
    },
    {
        "collection": "ibkr_reverse_signals",
        "field": "bar_time_ms",
        "kind": "ms",
        "include_legacy_empty": True,
    },
    {
        "collection": "ibkr_targets",
        "field": "bar_time_ms",
        "kind": "ms",
        "include_legacy_empty": True,
    },
    {
        "collection": "ibkr_bar_integrity",
        "field": "market_date",
        "kind": "date_text",
        "include_legacy_empty": False,
    },
    {
        "collection": "ibkr_bar_truth_audit",
        "field": "market_date",
        "kind": "date_text",
        "include_legacy_empty": False,
    },
)


def _normalize_environment(value: str) -> str:
    text = str(value or "").strip().lower()
    return text or "live"


def _unique_environments(values: Iterable[str] | None) -> list[str]:
    output: list[str] = []
    seen = set()
    for raw in values or []:
        environment = _normalize_environment(raw)
        if environment in seen:
            continue
        seen.add(environment)
        output.append(environment)
    return output


def _build_environment_filter(environment: str, include_legacy_empty: bool = False) -> str:
    runtime_environment = _normalize_environment(environment)
    clauses = [f'environment = "{runtime_environment}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def _parse_state_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


class DataRetention:
    def __init__(
        self,
        pb_client,
        config=None,
        default_environments: Iterable[str] | None = None,
        policies: Iterable[dict[str, Any]] | None = None,
    ):
        self.pb_client = pb_client
        self.config = config
        self.default_environments = _unique_environments(default_environments or ["live"])
        self.policies = list(policies or DEFAULT_RETENTION_POLICIES)
        self._last_cleanup = ""
        self._total_deleted = 0
        self._last_summary: dict[str, Any] = {}

    def _retention_enabled(self, environment: str) -> bool:
        if not self.config:
            return True
        try:
            return bool(self.config.get_bool_for_environment("ibkr_history_retention_enabled", environment, True))
        except Exception:
            return True

    def _resolve_retention_days(self, environment: str, override_days: int | None = None) -> int:
        if override_days and int(override_days) > 0:
            return max(30, int(override_days))
        if not self.config:
            return DEFAULT_RETENTION_DAYS
        try:
            days = int(self.config.get_int_for_environment("ibkr_history_retention_days", environment, DEFAULT_RETENTION_DAYS))
        except Exception:
            days = DEFAULT_RETENTION_DAYS
        return max(30, days)

    def _load_expired_batch(
        self,
        collection: str,
        filter_str: str,
        sort: str,
    ) -> list[dict[str, Any]]:
        try:
            return self.pb_client.get_records(
                collection,
                filter=filter_str,
                sort=sort,
                per_page=RETENTION_BATCH_SIZE,
                page=1,
            )
        except Exception as exc:
            logger.error("Retention query failed: collection=%s error=%s", collection, exc)
            raise

    def _cleanup_policy(
        self,
        environment: str,
        cutoff_ms: int,
        cutoff_date: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        collection = str(policy.get("collection") or "").strip()
        field = str(policy.get("field") or "").strip()
        kind = str(policy.get("kind") or "ms").strip().lower()
        include_legacy_empty = bool(policy.get("include_legacy_empty"))
        filter_prefix = _build_environment_filter(environment, include_legacy_empty=include_legacy_empty)

        if not collection or not field:
            return {"collection": collection, "deleted": 0, "errors": 1, "batches": 0, "skipped": True}

        if kind == "date_text":
            filter_str = f"{filter_prefix} && {field} != '' && {field} < '{cutoff_date}'"
        else:
            filter_str = f"{filter_prefix} && {field} > 0 && {field} < {int(cutoff_ms)}"

        deleted = 0
        errors = 0
        batches = 0

        while True:
            rows = self._load_expired_batch(collection, filter_str, field)
            if not rows:
                break
            batches += 1
            for record in rows:
                record_id = str(record.get("id") or "").strip()
                if not record_id:
                    errors += 1
                    continue
                try:
                    self.pb_client.delete_record(collection, record_id)
                    deleted += 1
                except Exception as exc:
                    errors += 1
                    logger.warning(
                        "Retention delete failed: collection=%s environment=%s record_id=%s error=%s",
                        collection,
                        environment,
                        record_id,
                        exc,
                    )
            if len(rows) < RETENTION_BATCH_SIZE:
                break

        return {
            "collection": collection,
            "field": field,
            "kind": kind,
            "deleted": deleted,
            "errors": errors,
            "batches": batches,
        }

    def _get_hourly_cleanup_state(self, environment: str, hour_token: str) -> dict[str, Any]:
        if not self.pb_client or not hasattr(self.pb_client, "get_state"):
            return {}
        try:
            row = self.pb_client.get_state(RETENTION_STATE_KEY, environment, date=hour_token)
        except Exception as exc:
            logger.warning(
                "Retention state query failed: environment=%s hour=%s error=%s",
                environment,
                hour_token,
                exc,
            )
            return {}
        if not isinstance(row, dict):
            return {}
        return _parse_state_data(row.get("data"))

    def _mark_hourly_cleanup_state(
        self,
        environment: str,
        hour_token: str,
        data: dict[str, Any],
    ) -> None:
        if not self.pb_client or not hasattr(self.pb_client, "upsert_state"):
            return
        try:
            self.pb_client.upsert_state(RETENTION_STATE_KEY, environment, data, date=hour_token)
        except Exception as exc:
            logger.warning(
                "Retention state write failed: environment=%s hour=%s error=%s",
                environment,
                hour_token,
                exc,
            )

    def cleanup(
        self,
        environments: Iterable[str] | None = None,
        *,
        retention_days: int | None = None,
        source: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        runtime_environments = _unique_environments(environments or self.default_environments)
        now = datetime.now(ET)
        hour_token = now.strftime("%Y-%m-%d %H")
        source_name = str(source or "").strip().lower() or "manual"
        summary: dict[str, Any] = {
            "ok": True,
            "executed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "hour_token": hour_token,
            "source": source_name,
            "force": bool(force),
            "default_retention_days": DEFAULT_RETENTION_DAYS,
            "batch_size": RETENTION_BATCH_SIZE,
            "policies": [str(item.get("collection") or "") for item in self.policies],
            "environments": [],
            "total_deleted": 0,
            "total_errors": 0,
            "skipped": False,
        }

        if not runtime_environments:
            summary["skipped"] = True
            summary["reason"] = "no_environments"
            self._last_summary = summary
            self._last_cleanup = summary["executed_at"]
            return summary

        for environment in runtime_environments:
            enabled = self._retention_enabled(environment)
            effective_days = self._resolve_retention_days(environment, override_days=retention_days)
            cutoff = now - timedelta(days=effective_days)
            cutoff_ms = int(cutoff.timestamp() * 1000)
            cutoff_date = cutoff.strftime("%Y-%m-%d")
            env_result = {
                "environment": environment,
                "enabled": enabled,
                "retention_days": effective_days,
                "cutoff_ms": cutoff_ms,
                "cutoff_date": cutoff_date,
                "collections": [],
                "total_deleted": 0,
                "total_errors": 0,
                "skipped": False,
            }

            if not enabled:
                env_result["skipped"] = True
                env_result["reason"] = "retention_disabled"
                summary["environments"].append(env_result)
                continue

            prior_state = self._get_hourly_cleanup_state(environment, hour_token)
            if prior_state and not force:
                env_result["skipped"] = True
                env_result["reason"] = "already_executed_this_hour"
                env_result["previous_execution"] = prior_state
                summary["environments"].append(env_result)
                continue

            for policy in self.policies:
                policy_result = self._cleanup_policy(environment, cutoff_ms, cutoff_date, policy)
                env_result["collections"].append(policy_result)
                env_result["total_deleted"] += int(policy_result.get("deleted", 0) or 0)
                env_result["total_errors"] += int(policy_result.get("errors", 0) or 0)

            self._mark_hourly_cleanup_state(
                environment,
                hour_token,
                {
                    "completed": True,
                    "source": source_name,
                    "force": bool(force),
                    "executed_at": summary["executed_at"],
                    "cutoff_date": cutoff_date,
                    "retention_days": effective_days,
                    "total_deleted": env_result["total_deleted"],
                    "total_errors": env_result["total_errors"],
                },
            )
            summary["environments"].append(env_result)
            summary["total_deleted"] += env_result["total_deleted"]
            summary["total_errors"] += env_result["total_errors"]

        summary["ok"] = summary["total_errors"] == 0
        self._total_deleted += int(summary["total_deleted"] or 0)
        self._last_cleanup = summary["executed_at"]
        self._last_summary = summary
        return summary

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self._last_summary.get("skipped") is not True or self._last_summary.get("environments")),
            "default_retention_days": DEFAULT_RETENTION_DAYS,
            "batch_size": RETENTION_BATCH_SIZE,
            "last_cleanup": self._last_cleanup,
            "total_deleted": self._total_deleted,
            "state_key": RETENTION_STATE_KEY,
            "policies": [str(item.get("collection") or "") for item in self.policies],
            "last_summary": self._last_summary,
        }
