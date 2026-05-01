"""
Write validated bar records into PocketBase via the IBKR custom hook.
"""

from __future__ import annotations

import json
import os
import logging
import threading
import time
from typing import Dict

from .timeframe_utils import (
    build_bar_close_timestamps,
    build_runtime_timestamps,
    classify_session,
    normalize_interval,
)

logger = logging.getLogger(__name__)

DEFAULT_ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
BAR_BATCH_SIZE = max(1, int(os.environ.get("IBKR_BAR_BATCH_SIZE", "40")))
BAR_FLUSH_INTERVAL_SECONDS = max(0.5, float(os.environ.get("IBKR_BAR_FLUSH_INTERVAL", "2.0")))
BAR_FLUSH_RETRY_ATTEMPTS = max(1, int(os.environ.get("IBKR_BAR_FLUSH_RETRY_ATTEMPTS", "4")))
BAR_FLUSH_RETRY_BACKOFF_SECONDS = max(0.5, float(os.environ.get("IBKR_BAR_FLUSH_RETRY_BACKOFF_SECONDS", "1.0")))
BAR_PENDING_QUEUE_DIR = str(os.environ.get("IBKR_BAR_PENDING_QUEUE_DIR") or "").strip()
BAR_INGEST_CURSOR_STATE_KEY = "ibkr_bar_ingest_cursor"
NON_COMPUTE_DISPATCH_SOURCES = {
    "backfill",
    "history_backfill",
    "history_rebuild",
    "history_repair",
    "ibkr_history_backfill",
    "ibkr_history_rebuild",
}


def _normalize_source(value) -> str:
    return str(value or "").strip().lower()


def _bar_source(payload: dict) -> str:
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    return _normalize_source(extra.get("source") or payload.get("source"))


def _is_compute_dispatch_source(source: str) -> bool:
    normalized = _normalize_source(source)
    return not normalized or normalized not in NON_COMPUTE_DISPATCH_SOURCES


class DataWriter:
    def __init__(self, pb_client, collection: str = "ibkr_bars", config=None, environment: str = DEFAULT_ENVIRONMENT):
        self.pb_client = pb_client
        self.collection = collection
        self.config = config
        self.environment = str(environment or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT
        self._write_count = 0
        self._skip_count = 0
        self._error_count = 0
        self._pending_batch = []
        self._inflight_batch = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._queue_path = self._build_queue_path()
        self._load_persistent_queue()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="ibkr-bar-writer",
        )
        self._flush_thread.start()

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _get_float_setting(self, key: str, fallback: float) -> float:
        if not self.config:
            return fallback
        return self.config.get_float_for_environment(key, self.environment, fallback)

    def _batch_size(self) -> int:
        return max(1, self._get_int_setting("ibkr_bar_batch_size", BAR_BATCH_SIZE))

    def _flush_interval(self) -> float:
        return max(0.5, self._get_float_setting("ibkr_bar_flush_interval", BAR_FLUSH_INTERVAL_SECONDS))

    def _retry_attempts(self) -> int:
        return max(1, self._get_int_setting("ibkr_bar_flush_retry_attempts", BAR_FLUSH_RETRY_ATTEMPTS))

    def _retry_backoff_seconds(self) -> float:
        return max(0.5, self._get_float_setting("ibkr_bar_flush_retry_backoff_seconds", BAR_FLUSH_RETRY_BACKOFF_SECONDS))

    def _build_queue_path(self) -> str:
        base_dir = BAR_PENDING_QUEUE_DIR or os.path.join(os.getcwd(), "runtime_state")
        os.makedirs(base_dir, exist_ok=True)
        safe_environment = str(self.environment or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT
        return os.path.join(base_dir, f"ibkr_bar_pending_{safe_environment}.json")

    def _persist_queue_locked(self) -> None:
        payload = {
            "pending": list(self._pending_batch),
            "inflight": list(self._inflight_batch),
        }
        tmp_path = f"{self._queue_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)
        os.replace(tmp_path, self._queue_path)

    def _load_persistent_queue(self) -> None:
        try:
            with open(self._queue_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Failed to load pending bar queue %s: %s", self._queue_path, exc)
            return

        pending = payload.get("pending") if isinstance(payload, dict) else []
        inflight = payload.get("inflight") if isinstance(payload, dict) else []
        loaded = [
            item
            for item in list(inflight or []) + list(pending or [])
            if isinstance(item, dict)
        ]
        if loaded:
            self._pending_batch.extend(loaded)
            logger.info("Recovered %d pending bars from %s", len(loaded), self._queue_path)
        self._inflight_batch = []
        with self._lock:
            self._persist_queue_locked()

    def write_bar(self, bar_data: dict) -> bool:
        if not self._validate_bar(bar_data):
            logger.warning("Bar validation failed: %s %s", bar_data.get("symbol"), bar_data.get("us_time"))
            self._error_count += 1
            return False

        payload = self._build_payload(bar_data)
        batch_to_flush = None
        with self._lock:
            self._pending_batch.append(payload)
            self._persist_queue_locked()
            if len(self._pending_batch) >= self._batch_size():
                batch_to_flush = self._drain_batch_locked()

        if batch_to_flush:
            return self._flush_batch(batch_to_flush)
        return True

    def _drain_batch_locked(self):
        batch = list(self._pending_batch)
        self._pending_batch = []
        self._inflight_batch = list(batch)
        self._persist_queue_locked()
        return batch

    def _requeue_batch(self, batch) -> None:
        if not batch:
            return
        with self._lock:
            self._pending_batch = list(batch) + self._pending_batch
            self._inflight_batch = []
            self._persist_queue_locked()

    def _clear_inflight_batch(self) -> None:
        with self._lock:
            self._inflight_batch = []
            self._persist_queue_locked()

    def _advance_ingest_cursor(self, batch) -> None:
        if not batch:
            return

        now_ms = int(time.time() * 1000)
        interval_updates = {}
        for item in batch:
            if not isinstance(item, dict):
                continue
            interval = normalize_interval(item.get("interval", "5m"))
            current = interval_updates.setdefault(
                interval,
                {
                    "latest_bar_time_ms": 0,
                    "latest_bar_us": "",
                    "latest_bar_cn": "",
                    "latest_batch_symbols": set(),
                    "latest_batch_size": 0,
                    "latest_sources": set(),
                    "latest_compute_ingest_bar_time_ms": 0,
                    "latest_compute_ingest_bar_us": "",
                    "latest_compute_ingest_bar_cn": "",
                    "latest_compute_ingest_symbols": set(),
                    "latest_compute_ingest_sources": set(),
                    "latest_compute_ingest_batch_size": 0,
                },
            )
            bar_time_ms = int(item.get("bar_time_ms", 0) or 0)
            if bar_time_ms >= current["latest_bar_time_ms"]:
                current["latest_bar_time_ms"] = bar_time_ms
                current["latest_bar_us"] = str(item.get("us_time") or "")
                current["latest_bar_cn"] = str(item.get("cn_time") or "")
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                current["latest_batch_symbols"].add(symbol)
            source = _bar_source(item)
            if source:
                current["latest_sources"].add(source)
            current["latest_batch_size"] += 1
            if _is_compute_dispatch_source(source):
                if bar_time_ms >= current["latest_compute_ingest_bar_time_ms"]:
                    current["latest_compute_ingest_bar_time_ms"] = bar_time_ms
                    current["latest_compute_ingest_bar_us"] = str(item.get("us_time") or "")
                    current["latest_compute_ingest_bar_cn"] = str(item.get("cn_time") or "")
                if symbol:
                    current["latest_compute_ingest_symbols"].add(symbol)
                if source:
                    current["latest_compute_ingest_sources"].add(source)
                current["latest_compute_ingest_batch_size"] += 1

        try:
            existing = self.pb_client.get_state(BAR_INGEST_CURSOR_STATE_KEY, self.environment, date="global")
        except Exception as exc:
            logger.warning("Failed to load bar ingest cursor state: %s", exc)
            existing = None

        payload = (existing or {}).get("data") if isinstance(existing, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        intervals = payload.get("intervals") if isinstance(payload.get("intervals"), dict) else {}

        for interval, update in interval_updates.items():
            previous = intervals.get(interval) if isinstance(intervals.get(interval), dict) else {}
            latest_bar_time_ms = max(
                int(previous.get("latest_bar_time_ms", 0) or 0),
                int(update.get("latest_bar_time_ms", 0) or 0),
            )
            latest_sources = sorted(update["latest_sources"])
            next_interval = {
                **previous,
                "latest_bar_time_ms": latest_bar_time_ms,
                "latest_bar_us": update.get("latest_bar_us") or previous.get("latest_bar_us") or "",
                "latest_bar_cn": update.get("latest_bar_cn") or previous.get("latest_bar_cn") or "",
                "latest_batch_symbols": sorted(update["latest_batch_symbols"]),
                "latest_batch_size": int(update.get("latest_batch_size", 0) or 0),
                "latest_sources": latest_sources,
                "last_write_at_ms": now_ms,
            }
            previous_compute_ms = int(previous.get("latest_compute_ingest_bar_time_ms", 0) or 0)
            update_compute_ms = int(update.get("latest_compute_ingest_bar_time_ms", 0) or 0)
            if update_compute_ms > 0 and update_compute_ms >= previous_compute_ms:
                next_interval.update(
                    {
                        "latest_compute_ingest_bar_time_ms": update_compute_ms,
                        "latest_compute_ingest_bar_us": update.get("latest_compute_ingest_bar_us") or "",
                        "latest_compute_ingest_bar_cn": update.get("latest_compute_ingest_bar_cn") or "",
                        "latest_compute_ingest_symbols": sorted(update["latest_compute_ingest_symbols"]),
                        "latest_compute_ingest_sources": sorted(update["latest_compute_ingest_sources"]),
                        "latest_compute_ingest_batch_size": int(update.get("latest_compute_ingest_batch_size", 0) or 0),
                    }
                )
            else:
                next_interval.setdefault("latest_compute_ingest_bar_time_ms", previous_compute_ms)
                next_interval.setdefault("latest_compute_ingest_bar_us", previous.get("latest_compute_ingest_bar_us") or "")
                next_interval.setdefault("latest_compute_ingest_bar_cn", previous.get("latest_compute_ingest_bar_cn") or "")
                next_interval.setdefault("latest_compute_ingest_symbols", list(previous.get("latest_compute_ingest_symbols") or []))
                next_interval.setdefault("latest_compute_ingest_sources", list(previous.get("latest_compute_ingest_sources") or []))
                next_interval.setdefault("latest_compute_ingest_batch_size", int(previous.get("latest_compute_ingest_batch_size", 0) or 0))
            next_interval["compute_ingest_source_policy"] = "non_compute_sources_filtered"
            intervals[interval] = next_interval

        next_payload = {
            **payload,
            "version": 1,
            "environment": self.environment,
            "updated_at_ms": now_ms,
            "intervals": intervals,
        }
        self.pb_client.upsert_state(
            BAR_INGEST_CURSOR_STATE_KEY,
            self.environment,
            next_payload,
            date="global",
        )

    def _flush_batch(self, batch) -> bool:
        if not batch:
            return True

        max_attempts = self._retry_attempts()
        backoff_seconds = self._retry_backoff_seconds()
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.pb_client.upsert_bars(batch)
                if not result.get("ok", False):
                    raise RuntimeError(result.get("error") or "bar_upsert_failed")

                changed = int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
                skipped = int(result.get("skipped", 0) or 0)
                self._write_count += changed
                self._skip_count += skipped if skipped > 0 else max(0, len(batch) - changed)
                self._clear_inflight_batch()
                try:
                    self._advance_ingest_cursor(batch)
                except Exception as cursor_exc:
                    logger.warning("Failed to advance bar ingest cursor: %s", cursor_exc)
                return True
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts:
                    logger.warning(
                        "Retrying %d bars batch write (%d/%d): %s",
                        len(batch),
                        attempt,
                        max_attempts,
                        exc,
                    )
                    time.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 5.0)
                    continue
                logger.error(
                    "Failed to write %d bars batch after %d attempts: %s",
                    len(batch),
                    max_attempts,
                    exc,
                )

        self._error_count += len(batch)
        self._requeue_batch(batch)
        if last_error is not None:
            logger.warning("Re-queued %d bars batch after persistent write failure", len(batch))
        return False

    def flush(self) -> bool:
        batch = None
        with self._lock:
            if self._pending_batch:
                batch = self._drain_batch_locked()
        return self._flush_batch(batch)

    def _flush_loop(self):
        while not self._stop_event.wait(self._flush_interval()):
            try:
                self.flush()
            except Exception as exc:
                logger.error("Bar writer flush loop error: %s", exc)

    def close(self):
        self._stop_event.set()
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5)
        self.flush()

    def _build_payload(self, bar: Dict) -> Dict:
        normalized_interval = normalize_interval(bar.get("interval", "5m"))
        bar_time_ms = int(bar["bar_time_ms"])
        base_extra = dict(bar.get("extra") or {})
        base_extra.setdefault("source", bar.get("source", "ibkr_compute"))
        base_extra.setdefault("tick_count", int(bar.get("tick_count", 0) or 0))
        base_extra.setdefault("conid", int(bar.get("conid", 0) or 0))
        base_extra.setdefault("interval", normalized_interval)
        base_extra.setdefault("session_type", classify_session(bar.get("us_time", ""), bar_time_ms))
        base_extra.update(build_bar_close_timestamps(bar_time_ms, normalized_interval))
        base_extra.update(build_runtime_timestamps())

        return {
            "symbol": str(bar["symbol"]).upper(),
            "environment": str(bar.get("environment") or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT,
            "exchange": str(bar.get("exchange") or "").strip().upper(),
            "interval": normalized_interval,
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume", 0) or 0),
            "session_type": str(bar.get("session_type") or base_extra["session_type"]),
            "us_time": str(bar.get("us_time") or ""),
            "cn_time": str(bar.get("cn_time") or ""),
            "bar_time_ms": bar_time_ms,
            "extra": base_extra,
        }

    def _validate_bar(self, bar: dict) -> bool:
        required = ["symbol", "bar_time_ms", "open", "high", "low", "close"]
        for field in required:
            if field not in bar or bar[field] is None:
                return False

        try:
            o = float(bar["open"])
            h = float(bar["high"])
            l = float(bar["low"])
            c = float(bar["close"])
        except (TypeError, ValueError):
            return False

        if any(v <= 0 for v in (o, h, l, c)):
            return False

        if h < max(o, c) or l > min(o, c):
            logger.warning(
                "OHLC relationship invalid: %s O=%.2f H=%.2f L=%.2f C=%.2f",
                bar.get("symbol"),
                o,
                h,
                l,
                c,
            )
            return False

        if l > 0:
            spread_pct = (h - l) / l * 100
            if spread_pct > 50:
                logger.warning("Abnormal spread %.1f%% for %s", spread_pct, bar.get("symbol"))
                return False

        return True

    def status(self) -> dict:
        with self._lock:
            pending = len(self._pending_batch)
            inflight = len(self._inflight_batch)
        return {
            "writes": self._write_count,
            "skips_dedup": self._skip_count,
            "errors": self._error_count,
            "collection": self.collection,
            "pending_batch": pending,
            "inflight_batch": inflight,
            "batch_size": self._batch_size(),
            "flush_interval_s": self._flush_interval(),
            "pending_queue_path": self._queue_path,
        }
