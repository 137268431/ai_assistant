"""
Write validated bar records into PocketBase via the IBKR custom hook.
"""

from __future__ import annotations

import os
import logging
import threading
import time
from typing import Dict

from .timeframe_utils import build_runtime_timestamps, classify_session, normalize_interval

logger = logging.getLogger(__name__)

DEFAULT_ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
BAR_BATCH_SIZE = max(1, int(os.environ.get("IBKR_BAR_BATCH_SIZE", "40")))
BAR_FLUSH_INTERVAL_SECONDS = max(0.5, float(os.environ.get("IBKR_BAR_FLUSH_INTERVAL", "2.0")))
BAR_FLUSH_RETRY_ATTEMPTS = max(1, int(os.environ.get("IBKR_BAR_FLUSH_RETRY_ATTEMPTS", "4")))
BAR_FLUSH_RETRY_BACKOFF_SECONDS = max(0.5, float(os.environ.get("IBKR_BAR_FLUSH_RETRY_BACKOFF_SECONDS", "1.0")))


class DataWriter:
    def __init__(self, pb_client, collection: str = "ibkr_bars"):
        self.pb_client = pb_client
        self.collection = collection
        self._write_count = 0
        self._skip_count = 0
        self._error_count = 0
        self._batch_size = BAR_BATCH_SIZE
        self._flush_interval = BAR_FLUSH_INTERVAL_SECONDS
        self._pending_batch = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="ibkr-bar-writer",
        )
        self._flush_thread.start()

    def write_bar(self, bar_data: dict) -> bool:
        if not self._validate_bar(bar_data):
            logger.warning("Bar validation failed: %s %s", bar_data.get("symbol"), bar_data.get("us_time"))
            self._error_count += 1
            return False

        payload = self._build_payload(bar_data)
        batch_to_flush = None
        with self._lock:
            self._pending_batch.append(payload)
            if len(self._pending_batch) >= self._batch_size:
                batch_to_flush = self._drain_batch_locked()

        if batch_to_flush:
            return self._flush_batch(batch_to_flush)
        return True

    def _drain_batch_locked(self):
        batch = self._pending_batch
        self._pending_batch = []
        return batch

    def _requeue_batch(self, batch) -> None:
        if not batch:
            return
        with self._lock:
            self._pending_batch = list(batch) + self._pending_batch

    def _flush_batch(self, batch) -> bool:
        if not batch:
            return True

        backoff_seconds = BAR_FLUSH_RETRY_BACKOFF_SECONDS
        last_error = None
        for attempt in range(1, BAR_FLUSH_RETRY_ATTEMPTS + 1):
            try:
                result = self.pb_client.upsert_bars(batch)
                if not result.get("ok", False):
                    raise RuntimeError(result.get("error") or "bar_upsert_failed")

                changed = int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
                skipped = int(result.get("skipped", 0) or 0)
                self._write_count += changed
                self._skip_count += skipped if skipped > 0 else max(0, len(batch) - changed)
                return True
            except Exception as exc:
                last_error = exc
                if attempt < BAR_FLUSH_RETRY_ATTEMPTS:
                    logger.warning(
                        "Retrying %d bars batch write (%d/%d): %s",
                        len(batch),
                        attempt,
                        BAR_FLUSH_RETRY_ATTEMPTS,
                        exc,
                    )
                    time.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 5.0)
                    continue
                logger.error(
                    "Failed to write %d bars batch after %d attempts: %s",
                    len(batch),
                    BAR_FLUSH_RETRY_ATTEMPTS,
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
        while not self._stop_event.wait(self._flush_interval):
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
        base_extra = dict(bar.get("extra") or {})
        base_extra.setdefault("source", bar.get("source", "ibkr_compute"))
        base_extra.setdefault("tick_count", int(bar.get("tick_count", 0) or 0))
        base_extra.setdefault("conid", int(bar.get("conid", 0) or 0))
        base_extra.setdefault("interval", normalize_interval(bar.get("interval", "5m")))
        base_extra.setdefault("session_type", classify_session(bar.get("us_time", ""), bar.get("bar_time_ms")))
        base_extra.update(build_runtime_timestamps())

        return {
            "symbol": str(bar["symbol"]).upper(),
            "environment": str(bar.get("environment") or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT,
            "exchange": str(bar.get("exchange") or "").strip().upper(),
            "interval": normalize_interval(bar.get("interval", "5m")),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume", 0) or 0),
            "session_type": str(bar.get("session_type") or base_extra["session_type"]),
            "us_time": str(bar.get("us_time") or ""),
            "cn_time": str(bar.get("cn_time") or ""),
            "bar_time_ms": int(bar["bar_time_ms"]),
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
        return {
            "writes": self._write_count,
            "skips_dedup": self._skip_count,
            "errors": self._error_count,
            "collection": self.collection,
            "pending_batch": pending,
            "batch_size": self._batch_size,
            "flush_interval_s": self._flush_interval,
        }
