from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

from ibkr_compute.api.service_topology import build_service_topology
from ibkr_scheduler.cron_registry import (
    CRON_DEFINITIONS,
    NATIVE_API_HTTP_JOB_ENDPOINTS,
    NATIVE_HTTP_JOB_ENDPOINTS,
    build_cron_payload,
)
from ibkr_scheduler.jobs.compute_dispatch import build_compute_dispatch_runner
from ibkr_scheduler.jobs.upstream_http import run_upstream_http_job
from ibkr_scheduler.schedule import cron_matches_minute, cron_slot_token
from ibkr_compute.core.config import Config
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

app = Flask(__name__)
pb = PBClient(base_url=PB_BASE_URL)
config = Config(pb_client=pb)


class SchedulerService:
    def __init__(self, pb_client: PBClient, cfg: Config):
        self.pb = pb_client
        self.cfg = cfg
        self._lock = threading.Lock()
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
        state = {
            **self._load_job_state(job_id, environment),
            **(patch or {}),
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
                        **(detail or {}),
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

    def _run_native_http_job(self, job_id: str, environment: str) -> dict[str, Any]:
        method, path = NATIVE_HTTP_JOB_ENDPOINTS[job_id]
        return run_upstream_http_job(
            method=method,
            base_url=COMPUTE_BASE_URL,
            path=path,
            environment=environment,
            timeout_seconds=60,
        )

    def _run_native_api_job(self, job_id: str, environment: str) -> dict[str, Any]:
        method, path = NATIVE_API_HTTP_JOB_ENDPOINTS[job_id]
        return run_upstream_http_job(
            method=method,
            base_url=API_BASE_URL,
            path=path,
            environment=environment,
            timeout_seconds=90,
        )

    def run_job(
        self,
        job_id: str,
        environment: str,
        *,
        trigger_source: str = "scheduler_loop",
        scheduled_slot: str = "",
    ) -> dict[str, Any]:
        job = next((item for item in CRON_DEFINITIONS if item["id"] == job_id), None)
        if not job:
            return {"ok": False, "error": "unknown_job", "job_id": job_id, "environment": environment}

        existing_state = self.job_states(environment).get(job_id) or {}
        slot_token = str(scheduled_slot or "").strip()
        if slot_token and str(existing_state.get("last_scheduled_slot") or "").strip() == slot_token:
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_triggered_for_slot",
                "job_id": job_id,
                "environment": environment,
                "scheduled_slot": slot_token,
            }

        self.cfg.refresh()
        effective_items = build_cron_payload(self.cfg, environment, self.job_states(environment))
        effective_job = next((item for item in effective_items if item["id"] == job_id), None) or job
        if not effective_job.get("effective_enabled", False):
            result = {
                "ok": True,
                "skipped": True,
                "reason": "disabled",
                "job_id": job_id,
                "environment": environment,
                "scheduled_slot": slot_token,
            }
            self._save_job_state(job_id, environment, {
                "status": "disabled",
                "last_result": result,
                "last_run_started_at_ms": int(time.time() * 1000),
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
            })
            return result

        started_at_ms = int(time.time() * 1000)
        self._save_job_state(job_id, environment, {
            "status": "running",
            "last_run_started_at_ms": started_at_ms,
            "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
            "last_trigger_source": trigger_source,
        })
        try:
            if job_id == "ibkr_compute_runtime":
                result = self._run_compute_dispatch(environment)
            elif job_id in NATIVE_API_HTTP_JOB_ENDPOINTS:
                result = self._run_native_api_job(job_id, environment)
            elif job_id in NATIVE_HTTP_JOB_ENDPOINTS:
                result = self._run_native_http_job(job_id, environment)
            else:
                result = {
                    "ok": True,
                    "skipped": True,
                    "reason": "compatibility_pending",
                    "job_id": job_id,
                    "environment": environment,
                }
            status = "ok" if result.get("ok", False) and not result.get("skipped") else "idle"
            if result.get("skipped") and result.get("reason") == "disabled":
                status = "disabled"
            state_patch = {
                "status": status,
                "last_result": result,
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_trigger_source": trigger_source,
            }
            if result.get("ok", False) and not result.get("skipped"):
                state_patch["last_success_at_ms"] = int(time.time() * 1000)
            self._save_job_state(job_id, environment, state_patch)
            if not result.get("ok", False):
                self._write_system_event(
                    job_id=job_id,
                    environment=environment,
                    level="error",
                    title=f"Scheduler job failed: {job_id}",
                    detail={"result": result, "trigger_source": trigger_source},
                )
            elif not result.get("skipped") and trigger_source != "scheduler_loop":
                self._write_system_event(
                    job_id=job_id,
                    environment=environment,
                    level="info",
                    title=f"Scheduler job completed: {job_id}",
                    detail={"result": result, "trigger_source": trigger_source},
                )
            return result
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "job_id": job_id, "environment": environment}
            self._save_job_state(job_id, environment, {
                "status": "error",
                "last_result": result,
                "last_run_finished_at_ms": int(time.time() * 1000),
                "last_scheduled_slot": slot_token or str(existing_state.get("last_scheduled_slot") or ""),
                "last_trigger_source": trigger_source,
            })
            self._write_system_event(
                job_id=job_id,
                environment=environment,
                level="error",
                title=f"Scheduler job crashed: {job_id}",
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

    def _loop(self) -> None:
        environment = str(os.environ.get("IBKR_ENVIRONMENT") or "live").strip().lower() or "live"
        while not self._stop_event.wait(LOOP_INTERVAL_SECONDS):
            now_utc = datetime.now(timezone.utc)
            slot_token = cron_slot_token(now_utc)
            try:
                for definition in self._scheduled_jobs():
                    if not cron_matches_minute(str(definition.get("cron_expr") or ""), now_utc):
                        continue
                    self.run_job(
                        str(definition.get("id") or ""),
                        environment,
                        trigger_source="scheduler_loop",
                        scheduled_slot=slot_token,
                    )
            except Exception:
                continue


scheduler = SchedulerService(pb, config)
if str(os.environ.get("IBKR_SCHEDULER_AUTOSTART") or "true").strip().lower() not in {"0", "false", "no", "off"}:
    scheduler.start()


@app.route("/health", methods=["GET"])
def health():
    environment = str(request.args.get("environment") or os.environ.get("IBKR_ENVIRONMENT") or "live").strip().lower() or "live"
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "scheduler"),
            "environment": environment,
            "loop_interval_seconds": LOOP_INTERVAL_SECONDS,
            "ingest_cursor": scheduler._get_ingest_cursor(environment),
            "compute_dispatch_cursor": scheduler._get_dispatch_cursor(environment),
            "service_topology": build_service_topology(),
            "jobs": scheduler.job_states(environment),
        }
    )


@app.route("/status", methods=["GET"])
def status():
    environment = str(request.args.get("environment") or os.environ.get("IBKR_ENVIRONMENT") or "live").strip().lower() or "live"
    config.refresh()
    jobs = scheduler.job_states(environment)
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "scheduler"),
            "environment": environment,
            "loop_interval_seconds": LOOP_INTERVAL_SECONDS,
            "ingest_cursor": scheduler._get_ingest_cursor(environment),
            "compute_dispatch_cursor": scheduler._get_dispatch_cursor(environment),
            "service_topology": build_service_topology(),
            "jobs": jobs,
            "items": build_cron_payload(config, environment, jobs),
        }
    )


@app.route("/jobs/run/<job_id>", methods=["POST"])
def run_job(job_id: str):
    request_payload = request.get_json(silent=True) or {}
    environment = str(request_payload.get("environment") or os.environ.get("IBKR_ENVIRONMENT") or "live").strip().lower() or "live"
    trigger_source = str(request_payload.get("trigger_source") or "api_manual").strip() or "api_manual"
    scheduled_slot = str(request_payload.get("scheduled_slot") or "").strip()
    result = scheduler.run_job(job_id, environment, trigger_source=trigger_source, scheduled_slot=scheduled_slot)
    return jsonify(result), (200 if result.get("ok", False) else 500)
