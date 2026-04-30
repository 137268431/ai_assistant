"""Asynchronous IBKR Historical API repair queue for authoritative bars."""

from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ibkr_compute.market.bar_freshness import (
    BarFreshnessPlanner,
    _escape_pb_filter,
    extract_conid_from_bar_row,
)
from ibkr_compute.market.timeframe_utils import format_us_time, normalize_interval

STATE_KEY = "ibkr_bar_repair_queue"
STATE_DATE = "global"
DEFAULT_PERIODS = {
    "5m": "4d",
    "15m": "10d",
    "30m": "20d",
    "1h": "40d",
    "4h": "120d",
    "1d": "2y",
}
PRIORITY_RANK = {
    "trade": 10,
    "monitor": 20,
    "daily_scan": 30,
    "chart_active": 40,
    "watchlist": 50,
    "scan": 60,
    "manual": 70,
}


def _config_int(config, key: str, environment: str, default: int) -> int:
    if config is not None and hasattr(config, "get_int_for_environment"):
        try:
            return int(config.get_int_for_environment(key, environment, default))
        except Exception:
            return int(default)
    return int(default)


def _config_float(config, key: str, environment: str, default: float) -> float:
    if config is not None and hasattr(config, "get_for_environment"):
        try:
            return float(config.get_for_environment(key, environment, default))
        except Exception:
            return float(default)
    return float(default)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(order=True)
class _RepairQueueItem:
    sort_key: tuple[int, int] = field(compare=True)
    job_key: str = field(compare=False)


class BarRepairCoordinator:
    """Small in-process repair queue with PB state mirroring.

    The coordinator is deliberately conservative: it de-duplicates jobs by
    env/symbol/interval/expected bucket and performs API repair through
    DataBackfill rather than synthetic rollup.
    """

    def __init__(
        self,
        *,
        pb_client,
        config=None,
        environment: str = "live",
        data_backfill=None,
        data_writer=None,
        conid_resolver=None,
        symbol_meta_provider: Callable[[Iterable[str]], dict[str, dict]] | None = None,
        materialize_callback: Callable[[str, list[str], str], Any] | None = None,
    ):
        self.pb = pb_client
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.data_backfill = data_backfill
        self.data_writer = data_writer
        self.conid_resolver = conid_resolver
        self.symbol_meta_provider = symbol_meta_provider
        self.materialize_callback = materialize_callback
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queue: list[_RepairQueueItem] = []
        self._jobs: dict[str, dict] = {}
        self._seq = 0
        self._workers: list[threading.Thread] = []
        self._stop = False
        self._last_request_at = 0.0

    def _max_workers(self) -> int:
        return max(1, min(2, _config_int(self.config, "ibkr_bar_repair_max_concurrency", self.environment, 2)))

    def _request_spacing(self) -> float:
        return max(0.0, _config_float(self.config, "ibkr_bar_repair_request_spacing", self.environment, 1.0))

    def _period_for_interval(self, environment: str, interval: str) -> str:
        normalized = normalize_interval(interval)
        if self.config is not None and hasattr(self.config, "get_for_environment"):
            try:
                value = str(self.config.get_for_environment(f"ibkr_bar_repair_period_{normalized}", environment, DEFAULT_PERIODS.get(normalized, "4d")) or "").strip()
                if value:
                    return value
            except Exception:
                pass
        return DEFAULT_PERIODS.get(normalized, "4d")

    def _publish_state_locked(self) -> None:
        if self.pb is None or not hasattr(self.pb, "upsert_state"):
            return
        try:
            self.pb.upsert_state(STATE_KEY, self.environment, self.status(include_jobs=True), date=STATE_DATE)
        except Exception:
            return

    def _ensure_workers_locked(self) -> None:
        live_workers = [worker for worker in self._workers if worker.is_alive()]
        self._workers = live_workers
        target = self._max_workers()
        while len(self._workers) < target:
            worker = threading.Thread(target=self._worker_loop, name="bar-repair-worker", daemon=True)
            self._workers.append(worker)
            worker.start()

    def _priority_value(self, priority: str) -> int:
        return int(PRIORITY_RANK.get(str(priority or "").strip().lower(), PRIORITY_RANK["manual"]))

    def enqueue(
        self,
        *,
        environment: str | None = None,
        symbol: str,
        interval: str,
        expected_closed_ms: int = 0,
        priority: str = "manual",
        trigger: str = "manual",
        reason: str = "",
        period: str = "",
        source_payload: dict | None = None,
    ) -> dict:
        runtime_environment = str(environment or self.environment or "live").strip().lower() or "live"
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_interval = normalize_interval(interval)
        expected_ms = int(expected_closed_ms or 0)
        if not normalized_symbol:
            return {"queued": False, "error": "missing_symbol"}
        job_key = f"{runtime_environment}:{normalized_symbol}:{normalized_interval}:{expected_ms}"
        now = _now_ms()
        with self._condition:
            existing = self._jobs.get(job_key)
            if existing and str(existing.get("status") or "") in {"queued", "inflight", "succeeded"}:
                existing["dedupe_hits"] = int(existing.get("dedupe_hits", 0) or 0) + 1
                existing["updated_at_ms"] = now
                self._publish_state_locked()
                return {"queued": False, "deduped": True, "job": dict(existing)}
            self._seq += 1
            job = {
                "job_key": job_key,
                "environment": runtime_environment,
                "symbol": normalized_symbol,
                "interval": normalized_interval,
                "expected_closed_ms": expected_ms,
                "expected_closed_us": format_us_time(expected_ms) if expected_ms > 0 else "",
                "priority": str(priority or "manual").strip().lower() or "manual",
                "trigger": str(trigger or "manual").strip() or "manual",
                "reason": str(reason or "").strip(),
                "period": str(period or self._period_for_interval(runtime_environment, normalized_interval)).strip(),
                "status": "queued",
                "attempts": 0,
                "dedupe_hits": 0,
                "created_at_ms": now,
                "updated_at_ms": now,
                "last_error": "",
                "source_payload": dict(source_payload or {}),
            }
            self._jobs[job_key] = job
            heapq.heappush(self._queue, _RepairQueueItem((self._priority_value(job["priority"]), self._seq), job_key))
            self._ensure_workers_locked()
            self._publish_state_locked()
            self._condition.notify_all()
            return {"queued": True, "job": dict(job)}

    def enqueue_from_freshness(
        self,
        freshness: dict,
        *,
        priority: str = "manual",
        trigger: str = "manual",
    ) -> list[dict]:
        queued: list[dict] = []
        environment = str(freshness.get("environment") or self.environment)
        symbol = str(freshness.get("symbol") or "")
        for interval in freshness.get("needs_repair_intervals") or []:
            interval_payload = (freshness.get("intervals") or {}).get(interval) or {}
            queued.append(
                self.enqueue(
                    environment=environment,
                    symbol=symbol,
                    interval=interval,
                    expected_closed_ms=int(interval_payload.get("expected_closed_ms", 0) or 0),
                    priority=priority,
                    trigger=trigger,
                    reason=str(interval_payload.get("reason") or freshness.get("status") or ""),
                    source_payload=interval_payload,
                )
            )
        return queued

    def _resolve_conid(self, environment: str, symbol: str) -> int:
        if self.conid_resolver is not None and hasattr(self.conid_resolver, "resolve_bulk"):
            try:
                resolved = self.conid_resolver.resolve_bulk([symbol]) or {}
                conid = int(resolved.get(symbol) or 0)
                if conid > 0:
                    return conid
            except Exception:
                pass
        if self.pb is not None and hasattr(self.pb, "get_all_records"):
            try:
                rows = self.pb.get_all_records("ibkr_conid_cache", max_pages=20) or []
                for row in rows:
                    if str((row or {}).get("symbol") or "").strip().upper() == symbol:
                        conid = int((row or {}).get("conid") or 0)
                        if conid > 0:
                            return conid
            except Exception:
                pass
            try:
                rows = self.pb.get_all_records(
                    "ibkr_bars",
                    filter=f'symbol = "{_escape_pb_filter(symbol)}" && environment = "{_escape_pb_filter(environment)}"',
                    sort="-bar_time_ms",
                    max_pages=3,
                ) or []
                for row in rows:
                    conid = extract_conid_from_bar_row(row)
                    if conid > 0:
                        return conid
            except Exception:
                pass
        return 0

    def _symbol_meta(self, symbols: list[str]) -> dict[str, dict]:
        if callable(self.symbol_meta_provider):
            try:
                return dict(self.symbol_meta_provider(symbols) or {})
            except Exception:
                return {}
        return {symbol: {} for symbol in symbols}

    def _get_backfill_components(self, environment: str):
        if self.data_backfill is not None:
            return self.data_backfill, self.data_writer
        from ibkr_compute.broker import BrokerAdapter
        from ibkr_compute.market.data_backfill import DataBackfill
        from ibkr_compute.market.data_writer import DataWriter

        writer = DataWriter(pb_client=self.pb, config=self.config, environment=environment)
        backfill = DataBackfill(data_writer=writer, config=self.config, environment=environment, broker=BrokerAdapter())
        return backfill, writer

    def _wait_for_request_slot(self) -> None:
        spacing = self._request_spacing()
        if spacing <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_s = max(0.0, self._last_request_at + spacing - now)
            if wait_s <= 0:
                self._last_request_at = now
                return
            self._last_request_at = now + wait_s
        if wait_s > 0:
            time.sleep(wait_s)

    def _verify_job(self, job: dict) -> tuple[bool, dict]:
        planner = BarFreshnessPlanner(self.pb, self.config, environment=str(job.get("environment") or self.environment))
        freshness = planner.plan_symbol(
            str(job.get("symbol") or ""),
            [str(job.get("interval") or "5m")],
            environment=str(job.get("environment") or self.environment),
            required_bars=0,
        )
        interval_payload = (freshness.get("intervals") or {}).get(str(job.get("interval") or "")) or {}
        expected_ms = int(job.get("expected_closed_ms", 0) or 0)
        latest_ms = int(interval_payload.get("latest_stored_ms", 0) or 0)
        ok = latest_ms > 0 and (expected_ms <= 0 or latest_ms >= expected_ms)
        return ok, interval_payload

    def _run_job(self, job_key: str) -> None:
        with self._condition:
            job = self._jobs.get(job_key)
            if not job:
                return
            job["status"] = "inflight"
            job["attempts"] = int(job.get("attempts", 0) or 0) + 1
            job["updated_at_ms"] = _now_ms()
            self._publish_state_locked()
        job = dict(job)
        environment = str(job.get("environment") or self.environment)
        symbol = str(job.get("symbol") or "")
        interval = normalize_interval(str(job.get("interval") or "5m"))
        started = time.time()
        try:
            conid = self._resolve_conid(environment, symbol)
            if conid <= 0:
                raise RuntimeError("conid_unresolved")
            self._wait_for_request_slot()
            backfill, writer = self._get_backfill_components(environment)
            period = str(job.get("period") or self._period_for_interval(environment, interval))
            results = backfill.backfill_all(
                {symbol: conid},
                symbol_meta=self._symbol_meta([symbol]),
                intervals=[interval],
                repair_symbols=[symbol],
                period_overrides={symbol: {interval: period}},
            )
            if writer is not None and hasattr(writer, "flush"):
                writer.flush()
            if callable(self.materialize_callback):
                try:
                    self.materialize_callback(environment, [symbol], interval)
                except Exception:
                    pass
            ok, verify_payload = self._verify_job(job)
            written = int(((results or {}).get(symbol) or {}).get(interval, 0) or 0)
            if not ok:
                raise RuntimeError("freshness_still_incomplete")
            with self._condition:
                current = self._jobs.get(job_key)
                if current:
                    current.update(
                        {
                            "status": "succeeded",
                            "updated_at_ms": _now_ms(),
                            "finished_at_ms": _now_ms(),
                            "duration_s": round(max(0.0, time.time() - started), 3),
                            "written": written,
                            "last_error": "",
                            "verify": verify_payload,
                        }
                    )
                    self._publish_state_locked()
        except Exception as exc:
            with self._condition:
                current = self._jobs.get(job_key)
                if current:
                    max_retries = max(1, _config_int(self.config, "ibkr_bar_repair_max_retries", environment, 3))
                    attempts = int(current.get("attempts", 0) or 0)
                    current["last_error"] = str(exc)
                    current["updated_at_ms"] = _now_ms()
                    if attempts < max_retries:
                        current["status"] = "queued"
                        self._seq += 1
                        backoff_rank = self._priority_value(str(current.get("priority") or "manual")) + attempts
                        heapq.heappush(self._queue, _RepairQueueItem((backoff_rank, self._seq), job_key))
                        self._condition.notify_all()
                    else:
                        current["status"] = "failed"
                        current["finished_at_ms"] = _now_ms()
                    self._publish_state_locked()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop:
                    self._condition.wait(timeout=30)
                if self._stop:
                    return
                item = heapq.heappop(self._queue)
                job = self._jobs.get(item.job_key)
                if not job or str(job.get("status") or "") != "queued":
                    continue
            self._run_job(item.job_key)

    def status(self, *, include_jobs: bool = False) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
            counts: dict[str, int] = {}
            for job in jobs:
                status = str(job.get("status") or "unknown")
                counts[status] = int(counts.get(status, 0) or 0) + 1
            recent = sorted(jobs, key=lambda item: int(item.get("updated_at_ms", 0) or 0), reverse=True)[:12]
            payload = {
                "ok": True,
                "environment": self.environment,
                "pending": counts.get("queued", 0),
                "inflight": counts.get("inflight", 0),
                "succeeded": counts.get("succeeded", 0),
                "failed": counts.get("failed", 0),
                "counts": counts,
                "worker_count": len([worker for worker in self._workers if worker.is_alive()]),
                "max_concurrency": self._max_workers(),
                "request_spacing_s": self._request_spacing(),
                "recent_failures": [dict(job) for job in recent if str(job.get("status") or "") == "failed"][:6],
                "recent_jobs": [dict(job) for job in recent],
            }
            if include_jobs:
                payload["jobs"] = [dict(job) for job in jobs]
            return payload

    def shutdown(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
