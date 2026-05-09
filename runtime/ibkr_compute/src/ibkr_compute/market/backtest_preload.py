"""Asynchronous preloader for the default backtest 5m history window."""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from ibkr_compute.backtest.constants import (
    BACKTEST_WARMUP_BARS,
    DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
    DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
    DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES,
    DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
    ET,
    MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
    MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
    MAX_BACKTEST_BACKFILL_MAX_BATCHES,
    MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
    MAX_BACKTEST_WARMUP_BARS,
)
from ibkr_compute.market.timeframe_utils import build_runtime_timestamps, format_us_time, interval_to_ms

logger = logging.getLogger(__name__)

STATE_KEY = "ibkr_backtest_preload_queue"
STATE_DATE = "global"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_BUFFER_BARS = 20
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_REQUEST_SPACING_SECONDS = 1.0
DEFAULT_MAX_RETRIES = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


def _config_bool(config, key: str, environment: str, default: bool) -> bool:
    if config is not None and hasattr(config, "get_bool_for_environment"):
        try:
            return bool(config.get_bool_for_environment(key, environment, default))
        except Exception:
            return bool(default)
    if config is not None and hasattr(config, "get_bool"):
        try:
            return bool(config.get_bool(key, default))
        except Exception:
            return bool(default)
    return bool(default)


def _config_int(config, key: str, environment: str, default: int) -> int:
    if config is not None and hasattr(config, "get_int_for_environment"):
        try:
            return int(config.get_int_for_environment(key, environment, default))
        except Exception:
            return int(default)
    if config is not None and hasattr(config, "get_int"):
        try:
            return int(config.get_int(key, default))
        except Exception:
            return int(default)
    return int(default)


def _config_float(config, key: str, environment: str, default: float) -> float:
    if config is not None and hasattr(config, "get_float_for_environment"):
        try:
            return float(config.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    if config is not None and hasattr(config, "get_float"):
        try:
            return float(config.get_float(key, default))
        except Exception:
            return float(default)
    return float(default)


def _normalize_environment(value: str | None, fallback: str = "live") -> str:
    return str(value or fallback or "live").strip().lower() or "live"


def _normalize_symbols(symbols: Iterable[str] | str | None) -> list[str]:
    raw_items = symbols if isinstance(symbols, (list, tuple, set)) else [symbols]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_items or []:
        parts = raw if isinstance(raw, (list, tuple, set)) else str(raw or "").replace("\n", ",").split(",")
        for part in parts:
            symbol = str(part or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
    return normalized


def _coerce_yyyy_mm_dd(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    datetime.strptime(text, "%Y-%m-%d")
    return text


@dataclass(order=True)
class _PreloadQueueItem:
    sort_key: tuple[int, int] = field(compare=True)
    job_key: str = field(compare=False)


class BacktestPreloadCoordinator:
    """Conservative background queue for default backtest 5m preloads.

    The queue is intentionally narrow: it de-duplicates per
    environment/symbol/date-range/warmup tuple and delegates the actual history
    request/persistence to the canonical BacktestService helpers, so the data
    shape matches normal backtest preflight backfill.
    """

    def __init__(
        self,
        *,
        pb_client=None,
        config=None,
        environment: str = "live",
        backtest_service=None,
        autostart_workers: bool = True,
    ):
        self.pb = pb_client
        self.config = config
        self.environment = _normalize_environment(environment)
        self.backtest_service = backtest_service
        self.autostart_workers = bool(autostart_workers)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queue: list[_PreloadQueueItem] = []
        self._jobs: dict[str, dict] = {}
        self._seq = 0
        self._workers: list[threading.Thread] = []
        self._stop = False
        self._last_request_at = 0.0
        self._last_enqueue_ms = 0

    def _enabled(self, environment: str) -> bool:
        return _config_bool(self.config, "ibkr_backtest_preload_enabled", environment, True)

    def _lookback_days(self, environment: str, explicit: int | None = None) -> int:
        value = explicit if explicit is not None else _config_int(
            self.config,
            "ibkr_backtest_preload_lookback_days",
            environment,
            DEFAULT_LOOKBACK_DAYS,
        )
        return max(1, min(90, int(value or DEFAULT_LOOKBACK_DAYS)))

    def _warmup_bars(self, environment: str, explicit: int | None = None) -> int:
        value = explicit if explicit is not None else _config_int(
            self.config,
            "ibkr_backtest_preload_warmup_bars",
            environment,
            BACKTEST_WARMUP_BARS,
        )
        return max(BACKTEST_WARMUP_BARS, min(MAX_BACKTEST_WARMUP_BARS, int(value or BACKTEST_WARMUP_BARS)))

    def _buffer_bars(self, environment: str) -> int:
        return max(0, min(500, _config_int(self.config, "ibkr_backtest_preload_buffer_bars", environment, DEFAULT_BUFFER_BARS)))

    def _max_workers(self) -> int:
        return max(
            1,
            min(
                2,
                _config_int(
                    self.config,
                    "ibkr_backtest_preload_max_concurrency",
                    self.environment,
                    DEFAULT_MAX_CONCURRENCY,
                ),
            ),
        )

    def _request_spacing(self) -> float:
        return max(
            0.0,
            _config_float(
                self.config,
                "ibkr_backtest_preload_request_spacing",
                self.environment,
                DEFAULT_REQUEST_SPACING_SECONDS,
            ),
        )

    def _max_retries(self, environment: str) -> int:
        return max(0, min(5, _config_int(self.config, "ibkr_backtest_preload_max_retries", environment, DEFAULT_MAX_RETRIES)))

    def _symbol_timeout_s(self, environment: str) -> int:
        return min(
            MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
            max(
                30,
                _config_int(
                    self.config,
                    "ibkr_backtest_preload_symbol_timeout_s",
                    environment,
                    DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
                ),
            ),
        )

    def _max_batches(self, environment: str) -> int:
        return min(
            MAX_BACKTEST_BACKFILL_MAX_BATCHES,
            max(
                1,
                _config_int(
                    self.config,
                    "ibkr_backtest_preload_max_batches",
                    environment,
                    DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES,
                ),
            ),
        )

    def _history_timeout_s(self, environment: str) -> int:
        return min(
            MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
            max(
                5,
                _config_int(
                    self.config,
                    "ibkr_backtest_preload_history_timeout_s",
                    environment,
                    DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
                ),
            ),
        )

    def _history_max_retries(self, environment: str) -> int:
        return min(
            MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
            max(
                0,
                _config_int(
                    self.config,
                    "ibkr_backtest_preload_history_max_retries",
                    environment,
                    DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
                ),
            ),
        )

    def _publish_state_locked(self) -> None:
        if self.pb is None or not hasattr(self.pb, "upsert_state"):
            return
        try:
            self.pb.upsert_state(STATE_KEY, self.environment, self.status(include_jobs=True), date=STATE_DATE)
        except Exception:
            return

    def _ensure_workers_locked(self) -> None:
        if not self.autostart_workers:
            return
        live_workers = [worker for worker in self._workers if worker.is_alive()]
        self._workers = live_workers
        target = self._max_workers()
        while len(self._workers) < target:
            worker = threading.Thread(target=self._worker_loop, name="backtest-preload-worker", daemon=True)
            self._workers.append(worker)
            worker.start()

    def _resolve_date_range(
        self,
        *,
        environment: str,
        date_from: str | None = None,
        date_to: str | None = None,
        lookback_days: int | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str, int]:
        resolved_to = _coerce_yyyy_mm_dd(date_to)
        days = self._lookback_days(environment, lookback_days)
        if not resolved_to:
            current = now.astimezone(ET) if now else datetime.now(ET)
            resolved_to = (current.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        resolved_from = _coerce_yyyy_mm_dd(date_from)
        if not resolved_from:
            to_date = datetime.strptime(resolved_to, "%Y-%m-%d").date()
            resolved_from = (to_date - timedelta(days=days)).strftime("%Y-%m-%d")
        if resolved_from > resolved_to:
            resolved_from = resolved_to
        return resolved_from, resolved_to, days

    def _date_to_ms_range(self, date_from: str, date_to: str) -> tuple[int, int]:
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _build_window(self, *, date_from: str, date_to: str, warmup_bars: int, buffer_bars: int) -> dict:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        preload_start_ms = max(0, start_ms - interval_to_ms("5m") * (int(warmup_bars or 0) + int(buffer_bars or 0)))
        return {
            "date_from": date_from,
            "date_to": date_to,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "preload_start_ms": preload_start_ms,
            "start_us": format_us_time(start_ms),
            "end_us": format_us_time(end_ms),
            "preload_start_us": format_us_time(preload_start_ms) if preload_start_ms > 0 else "",
        }

    def enqueue(
        self,
        symbols: Iterable[str] | str | None = None,
        *,
        environment: str | None = None,
        trigger: str = "manual",
        reason: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        lookback_days: int | None = None,
        warmup_bars: int | None = None,
        source_payload: dict | None = None,
    ) -> dict:
        runtime_environment = _normalize_environment(environment, self.environment)
        normalized_symbols = _normalize_symbols(symbols)
        if not self._enabled(runtime_environment):
            return {
                "ok": True,
                "enabled": False,
                "available": True,
                "symbols": normalized_symbols,
                "queued": [],
                "deduped": [],
                "skipped": normalized_symbols,
                "reason": "disabled",
            }
        if not normalized_symbols:
            return {
                "ok": True,
                "enabled": True,
                "available": True,
                "symbols": [],
                "queued": [],
                "deduped": [],
                "skipped": [],
                "reason": "empty_symbols",
            }

        resolved_from, resolved_to, days = self._resolve_date_range(
            environment=runtime_environment,
            date_from=date_from,
            date_to=date_to,
            lookback_days=lookback_days,
        )
        effective_warmup_bars = self._warmup_bars(runtime_environment, warmup_bars)
        buffer_bars = self._buffer_bars(runtime_environment)
        window = self._build_window(
            date_from=resolved_from,
            date_to=resolved_to,
            warmup_bars=effective_warmup_bars,
            buffer_bars=buffer_bars,
        )
        now = _now_ms()
        queued: list[dict] = []
        deduped: list[dict] = []
        with self._condition:
            self._last_enqueue_ms = now
            for symbol in normalized_symbols:
                job_key = f"{runtime_environment}:{symbol}:{resolved_from}:{resolved_to}:{effective_warmup_bars}:{buffer_bars}"
                existing = self._jobs.get(job_key)
                if existing and str(existing.get("status") or "") in {"queued", "inflight", "succeeded"}:
                    existing["dedupe_hits"] = int(existing.get("dedupe_hits", 0) or 0) + 1
                    existing["updated_at_ms"] = now
                    deduped.append(dict(existing))
                    continue
                self._seq += 1
                job = {
                    "job_key": job_key,
                    "environment": runtime_environment,
                    "symbol": symbol,
                    "interval": "5m",
                    "date_from": resolved_from,
                    "date_to": resolved_to,
                    "lookback_days": days,
                    "warmup_bars": effective_warmup_bars,
                    "buffer_bars": buffer_bars,
                    **window,
                    "trigger": str(trigger or "manual").strip() or "manual",
                    "reason": str(reason or "").strip(),
                    "source_payload": dict(source_payload or {}),
                    "status": "queued",
                    "attempts": 0,
                    "dedupe_hits": 0,
                    "created_at_ms": now,
                    "updated_at_ms": now,
                    "last_error": "",
                    "fetched_rows": 0,
                    "persisted_rows": 0,
                    "batches": 0,
                }
                self._jobs[job_key] = job
                heapq.heappush(self._queue, _PreloadQueueItem((now, self._seq), job_key))
                queued.append(dict(job))
            self._ensure_workers_locked()
            self._publish_state_locked()
            self._condition.notify_all()

        return {
            "ok": True,
            "enabled": True,
            "available": True,
            "symbols": normalized_symbols,
            "date_from": resolved_from,
            "date_to": resolved_to,
            "lookback_days": days,
            "warmup_bars": effective_warmup_bars,
            "buffer_bars": buffer_bars,
            "queued": queued,
            "deduped": deduped,
            "skipped": [],
            "pending": self.status().get("pending", 0),
            "inflight": self.status().get("inflight", 0),
        }

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

    def _annotate_rows(self, rows: list[dict], job: dict) -> list[dict]:
        annotated: list[dict] = []
        runtime_fields = build_runtime_timestamps()
        for row in rows or []:
            payload = dict(row or {})
            extra = dict(payload.get("extra") or {})
            extra.update(
                {
                    "source": "ibkr_backtest_preload",
                    "canonical": True,
                    "backfill_scope": "default_backtest_range",
                    "preload_job_key": str(job.get("job_key") or ""),
                    "preload_trigger": str(job.get("trigger") or ""),
                    "preload_date_from": str(job.get("date_from") or ""),
                    "preload_date_to": str(job.get("date_to") or ""),
                    **runtime_fields,
                }
            )
            payload["source"] = "backfill"
            payload["extra"] = extra
            annotated.append(payload)
        return annotated

    def _run_preload(self, job: dict) -> dict:
        service = self.backtest_service
        if service is None:
            raise RuntimeError("backtest_service_unavailable")
        required_methods = (
            "_backfill_symbol_history",
            "_dedupe_backfill_rows",
            "_persist_backfill_rows",
        )
        missing = [name for name in required_methods if not hasattr(service, name)]
        if missing:
            raise RuntimeError(f"backtest_service_missing_methods:{','.join(missing)}")

        environment = _normalize_environment(str(job.get("environment") or self.environment))
        symbol = str(job.get("symbol") or "").strip().upper()
        started = time.monotonic()
        self._wait_for_request_slot()
        repair = service._backfill_symbol_history(
            symbol,
            environment,
            int(job.get("preload_start_ms", 0) or 0),
            int(job.get("end_ms", 0) or 0),
            interval="5m",
            max_elapsed_s=self._symbol_timeout_s(environment),
            max_batches=self._max_batches(environment),
            history_timeout_s=self._history_timeout_s(environment),
            history_max_retries=self._history_max_retries(environment),
        ) or {}
        repair_rows = service._dedupe_backfill_rows(list(repair.get("rows") or []))
        annotated_rows = self._annotate_rows(repair_rows, job)
        persisted_rows = int(service._persist_backfill_rows(annotated_rows) if annotated_rows else 0)
        ok = bool(annotated_rows) and persisted_rows > 0
        reason = str(repair.get("reason") or ("ok" if ok else "no_rows_fetched"))
        if not ok and reason == "ok":
            reason = "persist_failed" if annotated_rows else "no_rows_fetched"
        return {
            "ok": ok,
            "reason": reason,
            "fetched_rows": len(annotated_rows),
            "persisted_rows": persisted_rows,
            "batches": int(repair.get("batches", 0) or 0),
            "raw_points": int(repair.get("raw_points", 0) or 0),
            "raw_first_us": str(repair.get("raw_first_us") or ""),
            "raw_last_us": str(repair.get("raw_last_us") or ""),
            "request_start_times": list(repair.get("request_start_times") or [])[:10],
            "duration_s": round(time.monotonic() - started, 3),
            "repair": {key: value for key, value in repair.items() if key != "rows"},
        }

    def _mark_job_failed_or_requeue(self, job_key: str, error: Any) -> None:
        now = _now_ms()
        with self._condition:
            current = self._jobs.get(job_key)
            if not current:
                return
            environment = _normalize_environment(str(current.get("environment") or self.environment))
            attempts = int(current.get("attempts", 0) or 0)
            max_retries = self._max_retries(environment)
            current["last_error"] = str(error or "")[:1000]
            current["updated_at_ms"] = now
            if attempts <= max_retries and not self._stop:
                current["status"] = "queued"
                self._seq += 1
                heapq.heappush(self._queue, _PreloadQueueItem((now + attempts, self._seq), job_key))
                self._condition.notify_all()
            else:
                current["status"] = "failed"
                current["finished_at_ms"] = now
            self._publish_state_locked()

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
        try:
            result = self._run_preload(job)
            if not bool(result.get("ok")):
                with self._condition:
                    current = self._jobs.get(job_key)
                    if current:
                        current.update({key: value for key, value in result.items() if key not in {"ok"}})
                        self._publish_state_locked()
                self._mark_job_failed_or_requeue(job_key, result.get("reason") or "preload_failed")
                return
            now = _now_ms()
            with self._condition:
                current = self._jobs.get(job_key)
                if current:
                    current.update(
                        {
                            "status": "succeeded",
                            "updated_at_ms": now,
                            "finished_at_ms": now,
                            "last_error": "",
                            **{key: value for key, value in result.items() if key != "ok"},
                        }
                    )
                    self._publish_state_locked()
        except Exception as exc:
            logger.warning("Backtest preload job failed: %s", exc, exc_info=True)
            self._mark_job_failed_or_requeue(job_key, exc)

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
                "available": True,
                "enabled": self._enabled(self.environment),
                "environment": self.environment,
                "pending": counts.get("queued", 0),
                "inflight": counts.get("inflight", 0),
                "succeeded": counts.get("succeeded", 0),
                "failed": counts.get("failed", 0),
                "counts": counts,
                "worker_count": len([worker for worker in self._workers if worker.is_alive()]),
                "max_concurrency": self._max_workers(),
                "request_spacing_s": self._request_spacing(),
                "lookback_days": self._lookback_days(self.environment),
                "warmup_bars": self._warmup_bars(self.environment),
                "buffer_bars": self._buffer_bars(self.environment),
                "last_enqueue_ms": self._last_enqueue_ms,
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
