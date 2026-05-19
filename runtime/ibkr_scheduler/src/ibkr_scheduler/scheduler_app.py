from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

from ibkr_compute.api.service_topology import build_service_topology
from ibkr_scheduler.cron_registry import (
    CRON_DEFINITIONS,
    NATIVE_API_HTTP_JOB_ENDPOINTS,
    NATIVE_HTTP_JOB_ENDPOINTS,
    build_cron_families,
    build_cron_payload,
    build_cron_payload_for_modes,
    get_schedule,
    mode_for_scope,
    resolve_cron_job,
)
from ibkr_scheduler.jobs.compute_dispatch import build_compute_dispatch_runner
from ibkr_scheduler.jobs.upstream_http import run_upstream_http_job
from ibkr_scheduler.schedule import cron_matches_minute, cron_slot_token
from ibkr_compute.core.broker_mode import (
    mode_context,
    normalize_broker_mode,
    normalize_market_data_mode,
)
from ibkr_compute.core.config import Config
from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.integrations.pb_client import PBClient


PB_BASE_URL = str(os.environ.get("PB_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")
COMPUTE_BASE_URL = str(os.environ.get("IBKR_COMPUTE_INTERNAL_URL") or "http://127.0.0.1:5100").rstrip("/")
RUNTIME_BASE_URL = str(os.environ.get("IBKR_RUNTIME_INTERNAL_URL") or "http://127.0.0.1:5101").rstrip("/")
API_BASE_URL = str(os.environ.get("IBKR_API_INTERNAL_URL") or "http://127.0.0.1:5102").rstrip("/")
LOOP_INTERVAL_SECONDS = max(5.0, float(os.environ.get("IBKR_SCHEDULER_LOOP_INTERVAL_SEC", "30")))
BAR_INGEST_CURSOR_STATE_KEY = "ibkr_bar_ingest_cursor"
COMPUTE_DISPATCH_CURSOR_STATE_KEY = "ibkr_compute_dispatch_cursor"
SCHEDULER_JOB_STATE_PREFIX = "ibkr_scheduler_job_state:"
US_TZ = ZoneInfo("America/New_York")
CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_NATIVE_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_NATIVE_API_HTTP_TIMEOUT_SECONDS = 90
JOB_TIMEOUT_SECONDS = {
    "system_status_reminder": 150,
    "ibkr_scan_runtime": 180,
    "ibkr_data_quality_truth_audit_cycle": 300,
}
BAR_TRUTH_AUDIT_JOB_IDS = {
    "ibkr_data_quality_truth_audit_cycle",
}


def _compact_scheduler_payload(payload: Any) -> Any:
    return compact_json_payload(
        payload,
        max_list_items=60,
        max_dict_items=160,
        max_string_length=1200,
        max_depth=8,
    )


def _mapping_value(source: Mapping[str, Any] | None, key: str) -> Any:
    if not source:
        return None
    getter = getattr(source, "get", None)
    if callable(getter):
        return getter(key)
    return None


def _job_timeout_seconds(job_id: str, default: int) -> int:
    normalized_job_id = str(job_id or "").strip()
    env_key = f"IBKR_SCHEDULER_JOB_TIMEOUT_{normalized_job_id.upper().replace('-', '_')}_SEC"
    raw_value = os.environ.get(env_key)
    if raw_value is None:
        raw_value = JOB_TIMEOUT_SECONDS.get(normalized_job_id, default)
    try:
        return max(1, int(float(raw_value)))
    except (TypeError, ValueError):
        return max(1, int(default))


def _scheduler_mode_context(
    source: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    context = mode_context(
        requested_broker_mode=_mapping_value(source, "broker_mode"),
        requested_market_data_mode=_mapping_value(source, "market_data_mode"),
        requested_gateway_mode=_mapping_value(source, "gateway_mode"),
        env=env,
    )
    return {
        "broker_mode": str(context.get("broker_mode") or "paper"),
        "market_data_mode": str(context.get("market_data_mode") or "live"),
        "gateway_mode": str(context.get("gateway_mode") or context.get("broker_mode") or "paper"),
    }


app = Flask(__name__)
pb = PBClient(base_url=PB_BASE_URL)
config = Config(pb_client=pb)


class SchedulerService:
    def __init__(self, pb_client: PBClient, cfg: Config):
        self.pb = pb_client
        self.cfg = cfg
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._job_cache: dict[str, dict[str, Any]] = {}
        self._run_compute_dispatch = build_compute_dispatch_runner(
            pb=self.pb,
            compute_base_url=COMPUTE_BASE_URL,
            get_ingest_cursor=self._get_ingest_cursor,
            get_dispatch_cursor=self._get_dispatch_cursor,
            save_dispatch_cursor=self._save_dispatch_cursor,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ibkr-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _load_job_state(self, job_id: str, environment: str) -> dict[str, Any]:
        try:
            record = self.pb.get_state(f"{SCHEDULER_JOB_STATE_PREFIX}{job_id}", environment, date="global")
        except Exception:
            record = None
        payload = (record or {}).get("data") if isinstance(record, dict) else {}
        if isinstance(payload, dict):
            return dict(payload)
        return {}

    def _save_job_state(self, job_id: str, environment: str, patch: dict[str, Any]) -> dict[str, Any]:
        safe_patch = dict(patch or {})
        if "last_result" in safe_patch:
            safe_patch["last_result"] = _compact_scheduler_payload(safe_patch["last_result"])
        state = {
            **self._load_job_state(job_id, environment),
            **safe_patch,
            "job_id": job_id,
            "environment": environment,
            "updated_at_ms": int(time.time() * 1000),
        }
        self.pb.upsert_state(f"{SCHEDULER_JOB_STATE_PREFIX}{job_id}", environment, state, date="global")
        with self._lock:
            self._job_cache[f"{environment}:{job_id}"] = dict(state)
        return state

    def _write_system_event(
        self,
        *,
        job_id: str,
        environment: str,
        level: str,
        title: str,
        detail: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            self.pb.create_record(
                "system_events",
                {
                    "event_type": "scheduler_job",
                    "level": level,
                    "source": "ibkr_scheduler",
                    "environment": environment,
                    "title": f"[{environment.upper()}] {title}",
                    "detail": {
                        "job_id": job_id,
                        **(_compact_scheduler_payload(detail or {}) or {}),
                    },
                    "us_time": now.astimezone(US_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "cn_time": now.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "notified": False,
                },
            )
        except Exception:
            return

    def _get_ingest_cursor(self, environment: str) -> dict[str, Any]:
        record = self.pb.get_state(BAR_INGEST_CURSOR_STATE_KEY, environment, date="global")
        data = (record or {}).get("data") if isinstance(record, dict) else {}
        return dict(data) if isinstance(data, dict) else {}

    def _get_dispatch_cursor(self, environment: str) -> dict[str, Any]:
        record = self.pb.get_state(COMPUTE_DISPATCH_CURSOR_STATE_KEY, environment, date="global")
        data = (record or {}).get("data") if isinstance(record, dict) else {}
        return dict(data) if isinstance(data, dict) else {}

    def _save_dispatch_cursor(self, environment: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = {
            "version": 1,
            "environment": environment,
            "updated_at_ms": int(time.time() * 1000),
            **(payload or {}),
        }
        self.pb.upsert_state(COMPUTE_DISPATCH_CURSOR_STATE_KEY, environment, state, date="global")
        return state

    @staticmethod
    def _job_runtime_context(
        job: dict[str, Any],
        *,
        broker_mode: Any = None,
        market_data_mode: Any = None,
    ) -> dict[str, str]:
        mode_defaults = _scheduler_mode_context(
            {"broker_mode": broker_mode, "market_data_mode": market_data_mode}
        )
        job_mode_scope = str((job or {}).get("mode_scope") or "").strip().lower()
        normalized_broker_mode = mode_defaults["broker_mode"]
        normalized_market_data_mode = mode_defaults["market_data_mode"]
        runtime_environment = mode_for_scope(job, normalized_broker_mode, normalized_market_data_mode)
        return {
            "environment": runtime_environment,
            "broker_mode": normalized_broker_mode,
            "market_data_mode": normalized_market_data_mode,
            "mode_scope": job_mode_scope,
        }

    def job_states_for_modes(self, broker_mode: str, market_data_mode: str) -> dict[str, Any]:
        normalized_broker_mode = normalize_broker_mode(broker_mode, "paper")
        normalized_market_data_mode = normalize_market_data_mode(market_data_mode, "live")
        states_by_mode: dict[str, dict[str, Any]] = {}
        state_map: dict[str, Any] = {}
        for job in CRON_DEFINITIONS:
            runtime_environment = mode_for_scope(job, normalized_broker_mode, normalized_market_data_mode)
            if runtime_environment not in states_by_mode:
                states_by_mode[runtime_environment] = self.job_states(runtime_environment)
            state_map[job["id"]] = states_by_mode[runtime_environment].get(job["id"], {})
        return state_map

    @staticmethod
    def _previous_us_business_date(now_et: datetime | None = None) -> str:
        current = now_et.astimezone(US_TZ) if isinstance(now_et, datetime) else datetime.now(US_TZ)
        candidate = current.date() - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate.isoformat()

    def _native_http_job_payload(self, job_id: str, schedule: dict[str, Any] | None = None) -> dict[str, Any]:
        if job_id in BAR_TRUTH_AUDIT_JOB_IDS:
            now_et = datetime.now(US_TZ)
            payload_mode = str((schedule or {}).get("payload_mode") or "").strip().lower()
            market_date = (
                self._previous_us_business_date(now_et)
                if payload_mode == "previous_business_day"
                else now_et.date().isoformat()
            )
            return {
                "scan_scope": "watchlist_full",
                "persist": True,
                "market_date": market_date,
                "payload_mode": payload_mode or "current_day",
            }
        if job_id == "ibkr_storage_governor":
            return {
                "dry_run": False,
                "force": False,
                "profile": "balanced_50g",
                "source": "ibkr_scheduler",
            }
        return {}

    def _run_native_http_job(
        self,
        job_id: str,
        environment: str,
        schedule: dict[str, Any] | None = None,
        *,
        broker_mode: str,
        market_data_mode: str,
        mode_scope: str,
    ) -> dict[str, Any]:
        method, path = NATIVE_HTTP_JOB_ENDPOINTS[job_id]
        return run_upstream_http_job(
            method=method,
            base_url=COMPUTE_BASE_URL,
            path=path,
            environment=environment,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
            mode_scope=mode_scope,
            timeout_seconds=_job_timeout_seconds(job_id, DEFAULT_NATIVE_HTTP_TIMEOUT_SECONDS),
            payload=self._native_http_job_payload(job_id, schedule),
        )

    def _run_native_api_job(
        self,
        job_id: str,
        environment: str,
        schedule: dict[str, Any] | None = None,
        *,
        broker_mode: str,
        market_data_mode: str,
        mode_scope: str,
    ) -> dict[str, Any]:
        method, path = NATIVE_API_HTTP_JOB_ENDPOINTS[job_id]
        return run_upstream_http_job(
            method=method,
            base_url=API_BASE_URL,
            path=path,
            environment=environment,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
            mode_scope=mode_scope,
            timeout_seconds=_job_timeout_seconds(job_id, DEFAULT_NATIVE_API_HTTP_TIMEOUT_SECONDS),
            payload={
                "schedule_id": str((schedule or {}).get("id") or "").strip(),
                "schedule_payload_mode": str((schedule or {}).get("payload_mode") or "").strip(),
            } if schedule else None,
        )

    def run_job(
        self,
        job_id: str,
        *,
        broker_mode: str = "",
        market_data_mode: str = "",
        trigger_source: str = "scheduler_loop",
        scheduled_slot: str = "",
        schedule_id: str = "",
    ) -> dict[str, Any]:
        requested_job_id = str(job_id or "").strip()
        job, canonical_job_id, alias_job_id, resolved_schedule_id = resolve_cron_job(requested_job_id, schedule_id)
        if not job:
            fallback_context = _scheduler_mode_context(
                {"broker_mode": broker_mode, "market_data_mode": market_data_mode}
            )
            return {
                "ok": False,
                "error": "unknown_job",
                "job_id": requested_job_id,
                "environment": fallback_context["market_data_mode"],
                "broker_mode": fallback_context["broker_mode"],
                "market_data_mode": fallback_context["market_data_mode"],
            }
        runtime_context = self._job_runtime_context(
            job,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
        )
        environment = runtime_context["environment"]
        broker_mode = runtime_context["broker_mode"]
        market_data_mode = runtime_context["market_data_mode"]
        job_mode_scope = runtime_context["mode_scope"]
        schedule = get_schedule(job, resolved_schedule_id)
        effective_schedule_id = str(schedule.get("id") or resolved_schedule_id or "default").strip() or "default"

        existing_state = self.job_states(environment).get(canonical_job_id) or {}
        self.cfg.refresh()
        effective_items = build_cron_payload(self.cfg, environment, self.job_states(environment))
        effective_job = next((item for item in effective_items if item["id"] == canonical_job_id), None) or job
        slot_token = str(scheduled_slot or "").strip()
        if not effective_job.get("effective_enabled", False):
            result = {
                "ok": True,
                "skipped": True,
                "reason": "disabled",
                "job_id": canonical_job_id,
                "requested_job_id": requested_job_id,
                "canonical_job_id": canonical_job_id,
                "alias_job_id": alias_job_id,
                "schedule_id": effective_schedule_id,
                "environment": environment,
                "broker_mode": broker_mode,
                "market_data_mode": market_data_mode,
                "mode_scope": job_mode_scope,
                "scheduled_slot": slot_token,
            }
            self._save_job_state(canonical_job_id, environment, {
                "status": "disabled",
                "last_result": result,
                "last_run_started_at_ms": int(time.time() * 1000),
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_schedule_id": effective_schedule_id,
            })
            return result

        started_at_ms = int(time.time() * 1000)
        with self._lock:
            latest_state = self.job_states(environment).get(canonical_job_id) or {}
            latest_result = latest_state.get("last_result") if isinstance(latest_state.get("last_result"), dict) else {}
            last_runs = latest_state.get("last_runs") if isinstance(latest_state.get("last_runs"), dict) else {}
            latest_schedule_state = last_runs.get(effective_schedule_id) if isinstance(last_runs.get(effective_schedule_id), dict) else {}
            schedule_result = latest_schedule_state.get("last_result") if isinstance(latest_schedule_state.get("last_result"), dict) else {}
            same_completed_slot = slot_token and (
                str(latest_schedule_state.get("last_scheduled_slot") or "").strip() == slot_token
                or (
                    not latest_schedule_state
                    and str(latest_state.get("last_scheduled_slot") or "").strip() == slot_token
                    and str(latest_state.get("last_schedule_id") or effective_schedule_id).strip() == effective_schedule_id
                )
            )
            if same_completed_slot and (
                str(latest_schedule_state.get("status") or latest_state.get("status") or "").strip().lower() in {"running", "ok", "idle"}
                or schedule_result.get("ok") is True
                or (not schedule_result and latest_result.get("ok") is True)
            ):
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_triggered_for_slot",
                    "job_id": canonical_job_id,
                    "requested_job_id": requested_job_id,
                    "canonical_job_id": canonical_job_id,
                    "alias_job_id": alias_job_id,
                    "schedule_id": effective_schedule_id,
                    "environment": environment,
                    "broker_mode": broker_mode,
                    "market_data_mode": market_data_mode,
                    "mode_scope": job_mode_scope,
                    "scheduled_slot": slot_token,
                }
            running_last_runs = dict(last_runs)
            running_last_runs[effective_schedule_id] = {
                **latest_schedule_state,
                "status": "running",
                "last_run_started_at_ms": started_at_ms,
                "last_scheduled_slot": slot_token or str(latest_schedule_state.get("last_scheduled_slot") or ""),
                "last_trigger_source": trigger_source,
            }
            self._save_job_state(
                canonical_job_id,
                environment,
                {
                    "status": "running",
                    "last_run_started_at_ms": started_at_ms,
                    "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                    "last_schedule_id": effective_schedule_id,
                    "last_trigger_source": trigger_source,
                    "last_runs": running_last_runs,
                    "canonical_job_id": canonical_job_id,
                    "alias_job_id": alias_job_id,
                },
            )
        try:
            if canonical_job_id == "ibkr_compute_runtime":
                result = self._run_compute_dispatch(environment)
            elif canonical_job_id in NATIVE_API_HTTP_JOB_ENDPOINTS:
                result = self._run_native_api_job(
                    canonical_job_id,
                    environment,
                    schedule,
                    broker_mode=broker_mode,
                    market_data_mode=market_data_mode,
                    mode_scope=job_mode_scope,
                )
            elif canonical_job_id in NATIVE_HTTP_JOB_ENDPOINTS:
                result = self._run_native_http_job(
                    canonical_job_id,
                    environment,
                    schedule,
                    broker_mode=broker_mode,
                    market_data_mode=market_data_mode,
                    mode_scope=job_mode_scope,
                )
            else:
                result = {
                    "ok": True,
                    "skipped": True,
                    "reason": "compatibility_pending",
                    "job_id": canonical_job_id,
                    "environment": environment,
                    "broker_mode": broker_mode,
                    "market_data_mode": market_data_mode,
                    "mode_scope": job_mode_scope,
                }
            result = {
                **(result if isinstance(result, dict) else {"ok": False, "error": "invalid_job_result"}),
                "job_id": canonical_job_id,
                "requested_job_id": requested_job_id,
                "canonical_job_id": canonical_job_id,
                "alias_job_id": alias_job_id,
                "schedule_id": effective_schedule_id,
                "environment": environment,
                "broker_mode": broker_mode,
                "market_data_mode": market_data_mode,
                "mode_scope": job_mode_scope,
                "scheduled_slot": slot_token,
            }
            status = "ok" if result.get("ok", False) and not result.get("skipped") else "idle"
            if result.get("skipped") and result.get("reason") == "disabled":
                status = "disabled"
            latest_state = self.job_states(environment).get(canonical_job_id) or {}
            last_runs = latest_state.get("last_runs") if isinstance(latest_state.get("last_runs"), dict) else {}
            schedule_patch = {
                **(last_runs.get(effective_schedule_id) if isinstance(last_runs.get(effective_schedule_id), dict) else {}),
                "status": status,
                "last_result": _compact_scheduler_payload(result),
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_trigger_source": trigger_source,
            }
            if result.get("ok", False) and not result.get("skipped"):
                schedule_patch["last_success_at_ms"] = int(time.time() * 1000)
            next_last_runs = dict(last_runs)
            next_last_runs[effective_schedule_id] = schedule_patch
            state_patch = {
                "status": status,
                "last_result": result,
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_schedule_id": effective_schedule_id,
                "last_trigger_source": trigger_source,
                "last_runs": next_last_runs,
                "canonical_job_id": canonical_job_id,
                "alias_job_id": alias_job_id,
            }
            if result.get("ok", False) and not result.get("skipped"):
                state_patch["last_success_at_ms"] = int(time.time() * 1000)
            self._save_job_state(canonical_job_id, environment, state_patch)
            if not result.get("ok", False):
                self._write_system_event(
                    job_id=canonical_job_id,
                    environment=environment,
                    level="error",
                    title=f"Scheduler job failed: {canonical_job_id}",
                    detail={"result": result, "trigger_source": trigger_source},
                )
            elif not result.get("skipped") and trigger_source != "scheduler_loop":
                self._write_system_event(
                    job_id=canonical_job_id,
                    environment=environment,
                    level="info",
                    title=f"Scheduler job completed: {canonical_job_id}",
                    detail={"result": result, "trigger_source": trigger_source},
                )
            return result
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc),
                "job_id": canonical_job_id,
                "requested_job_id": requested_job_id,
                "canonical_job_id": canonical_job_id,
                "alias_job_id": alias_job_id,
                "schedule_id": effective_schedule_id,
                "environment": environment,
                "broker_mode": broker_mode,
                "market_data_mode": market_data_mode,
                "mode_scope": job_mode_scope,
                "scheduled_slot": slot_token,
            }
            latest_state = self.job_states(environment).get(canonical_job_id) or {}
            last_runs = latest_state.get("last_runs") if isinstance(latest_state.get("last_runs"), dict) else {}
            schedule_patch = {
                **(last_runs.get(effective_schedule_id) if isinstance(last_runs.get(effective_schedule_id), dict) else {}),
                "status": "error",
                "last_result": _compact_scheduler_payload(result),
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_trigger_source": trigger_source,
            }
            next_last_runs = dict(last_runs)
            next_last_runs[effective_schedule_id] = schedule_patch
            self._save_job_state(canonical_job_id, environment, {
                "status": "error",
                "last_result": result,
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_schedule_id": effective_schedule_id,
                "last_trigger_source": trigger_source,
                "last_runs": next_last_runs,
                "canonical_job_id": canonical_job_id,
                "alias_job_id": alias_job_id,
            })
            self._write_system_event(
                job_id=canonical_job_id,
                environment=environment,
                level="error",
                title=f"Scheduler job crashed: {canonical_job_id}",
                detail={"result": result, "trigger_source": trigger_source},
            )
            return result

    def job_states(self, environment: str) -> dict[str, Any]:
        state_map = {}
        for job in CRON_DEFINITIONS:
            cache_key = f"{environment}:{job['id']}"
            with self._lock:
                cached = self._job_cache.get(cache_key)
            if isinstance(cached, dict) and cached:
                state_map[job["id"]] = dict(cached)
            else:
                state_map[job["id"]] = self._load_job_state(job["id"], environment)
        return state_map

    def _scheduled_jobs(self) -> list[dict[str, Any]]:
        return [
            definition
            for definition in CRON_DEFINITIONS
            if str(definition.get("runner_kind") or "").strip().lower().startswith("native_")
        ]

    def run_due_jobs(
        self,
        when_utc: datetime,
        *,
        broker_mode: str = "",
        market_data_mode: str = "",
    ) -> list[dict[str, Any]]:
        mode_defaults = _scheduler_mode_context(
            {"broker_mode": broker_mode, "market_data_mode": market_data_mode}
        )
        normalized_broker_mode = mode_defaults["broker_mode"]
        normalized_market_data_mode = mode_defaults["market_data_mode"]
        slot_token = cron_slot_token(when_utc)
        results: list[dict[str, Any]] = []
        for definition in self._scheduled_jobs():
            schedules = definition.get("schedules") if isinstance(definition.get("schedules"), list) else []
            for schedule in schedules or [get_schedule(definition)]:
                if not cron_matches_minute(
                    str((schedule or {}).get("cron_expr") or ""),
                    when_utc,
                    str((schedule or {}).get("cron_timezone") or definition.get("cron_timezone") or "UTC"),
                ):
                    continue
                results.append(
                    self.run_job(
                        str(definition.get("id") or ""),
                        broker_mode=normalized_broker_mode,
                        market_data_mode=normalized_market_data_mode,
                        trigger_source="scheduler_loop",
                        scheduled_slot=slot_token,
                        schedule_id=str((schedule or {}).get("id") or "default"),
                    )
                )
        return results

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now_utc = datetime.now(timezone.utc)
            try:
                scheduler_modes = _scheduler_mode_context()
                self.run_due_jobs(
                    now_utc,
                    broker_mode=scheduler_modes["broker_mode"],
                    market_data_mode=scheduler_modes["market_data_mode"],
                )
            except Exception:
                pass
            if self._stop_event.wait(LOOP_INTERVAL_SECONDS):
                break


scheduler = SchedulerService(pb, config)
if str(os.environ.get("IBKR_SCHEDULER_AUTOSTART") or "true").strip().lower() not in {"0", "false", "no", "off"}:
    scheduler.start()


@app.route("/health", methods=["GET"])
def health():
    mode_context = _scheduler_mode_context(request.args)
    broker_mode = mode_context["broker_mode"]
    market_data_mode = mode_context["market_data_mode"]
    jobs = scheduler.job_states_for_modes(broker_mode, market_data_mode)
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "scheduler"),
            "environment": market_data_mode,
            "broker_mode": broker_mode,
            "market_data_mode": market_data_mode,
            "data_environment": market_data_mode,
            "market_data_environment": market_data_mode,
            "shared_market_data": market_data_mode == "live",
            "gateway_mode": mode_context["gateway_mode"],
            "loop_interval_seconds": LOOP_INTERVAL_SECONDS,
            "ingest_cursor": scheduler._get_ingest_cursor(market_data_mode),
            "compute_dispatch_cursor": scheduler._get_dispatch_cursor(market_data_mode),
            "service_topology": build_service_topology(),
            "jobs": jobs,
        }
    )


@app.route("/status", methods=["GET"])
def status():
    mode_context = _scheduler_mode_context(request.args)
    broker_mode = mode_context["broker_mode"]
    market_data_mode = mode_context["market_data_mode"]
    lite = str(request.args.get("lite") or "").strip().lower() in {"1", "true", "yes", "on"}
    config.refresh()
    jobs = scheduler.job_states_for_modes(broker_mode, market_data_mode)
    payload = {
        "ok": True,
        "status": "running",
        "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "scheduler"),
        "environment": market_data_mode,
        "broker_mode": broker_mode,
        "market_data_mode": market_data_mode,
        "data_environment": market_data_mode,
        "market_data_environment": market_data_mode,
        "shared_market_data": market_data_mode == "live",
        "gateway_mode": mode_context["gateway_mode"],
        "loop_interval_seconds": LOOP_INTERVAL_SECONDS,
        "ingest_cursor": scheduler._get_ingest_cursor(market_data_mode),
        "compute_dispatch_cursor": scheduler._get_dispatch_cursor(market_data_mode),
        "service_topology": build_service_topology(),
        "jobs": jobs,
    }
    if not lite:
        items = build_cron_payload_for_modes(
            config,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
            job_states=jobs,
        )
        payload.update({"items": items, "families": build_cron_families(items)})
    return jsonify(payload)


@app.route("/jobs/run/<job_id>", methods=["POST"])
def run_job(job_id: str):
    request_payload = request.get_json(silent=True) or {}
    if not isinstance(request_payload, dict):
        request_payload = {}
    if "environment" in request_payload:
        return jsonify(
            {
                "ok": False,
                "error": "environment_not_supported",
                "message": "Use broker_mode/market_data_mode or omit mode so scheduler selects by job mode_scope.",
            }
        ), 400
    mode_context = _scheduler_mode_context(request_payload)
    trigger_source = str(request_payload.get("trigger_source") or "api_manual").strip() or "api_manual"
    scheduled_slot = str(request_payload.get("scheduled_slot") or "").strip()
    schedule_id = str(request_payload.get("schedule_id") or "").strip()
    result = scheduler.run_job(
        job_id,
        broker_mode=mode_context["broker_mode"],
        market_data_mode=mode_context["market_data_mode"],
        trigger_source=trigger_source,
        scheduled_slot=scheduled_slot,
        schedule_id=schedule_id,
    )
    return jsonify(result), (200 if result.get("ok", False) else 500)
