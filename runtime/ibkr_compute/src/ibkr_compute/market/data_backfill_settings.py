"""Runtime settings helpers for :mod:`ibkr_compute.market.data_backfill`."""

from __future__ import annotations

import time
from typing import List, Optional, Sequence

from .data_backfill_support import (
    DEFAULT_CHUNK_DAYS,
    DEFAULT_HISTORY_CLOSE_DELAY_SECONDS,
    INTERVAL_DELAY_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    REQUEST_SPACING_SECONDS,
    RETRY_BASE_DELAY_SECONDS,
    TRACE_RECENT_LIMIT,
    TRACE_SLOW_SECONDS,
    _parse_intervals,
)
from .timeframe_utils import latest_safe_closed_bucket_ms, normalize_interval


class DataBackfillSettingsMixin:
    def _resolve_intervals(self, intervals: Optional[Sequence[str]]) -> List[str]:
        if intervals is None:
            return list(self.default_intervals)
        return _parse_intervals(",".join(str(item) for item in intervals), fallback=self.default_intervals)

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _get_float_setting(self, key: str, fallback: float) -> float:
        if not self.config:
            return fallback
        return self.config.get_float_for_environment(key, self.environment, fallback)

    def _get_bool_setting(self, key: str, fallback: bool) -> bool:
        if not self.config or not hasattr(self.config, "get_bool_for_environment"):
            return bool(fallback)
        return bool(self.config.get_bool_for_environment(key, self.environment, fallback))

    def _request_spacing(self) -> float:
        return max(0.0, self._get_float_setting("ibkr_history_request_spacing", REQUEST_SPACING_SECONDS))

    def _interval_delay(self) -> float:
        return max(0.0, self._get_float_setting("ibkr_history_interval_delay", INTERVAL_DELAY_SECONDS))

    def _max_concurrency(self) -> int:
        return max(1, min(20, self._get_int_setting("ibkr_history_max_concurrency", MAX_CONCURRENT_REQUESTS)))

    def _trace_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_history_trace_enabled", True)

    def _trace_log_all_requests(self) -> bool:
        return self._get_bool_setting("ibkr_history_trace_log_all_requests", True)

    def _trace_recent_limit(self) -> int:
        return max(1, min(100, self._get_int_setting("ibkr_history_trace_recent_limit", TRACE_RECENT_LIMIT)))

    def _trace_slow_seconds(self) -> float:
        return max(0.1, self._get_float_setting("ibkr_history_trace_slow_sec", TRACE_SLOW_SECONDS))

    def _direct_sqlite_read_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_bar_direct_sqlite_read_enabled", True)

    def _direct_sqlite_read_fallback_api_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_bar_direct_sqlite_read_fallback_api_enabled", True)

    def _direct_sqlite_read_timeout(self) -> float:
        fallback = self._get_float_setting("ibkr_bar_direct_sqlite_timeout_sec", 30.0)
        return max(0.5, self._get_float_setting("ibkr_bar_direct_sqlite_read_timeout_sec", fallback))

    def _max_retries(self) -> int:
        return max(0, self._get_int_setting("ibkr_history_max_retries", MAX_RETRIES))

    def _retry_base_delay(self) -> float:
        return max(0.5, self._get_float_setting("ibkr_history_retry_base_delay", RETRY_BASE_DELAY_SECONDS))

    def _chunked_backfill_enabled(self) -> bool:
        if not self.config:
            return True
        return self.config.get_bool_for_environment(
            "ibkr_history_chunked_backfill_enabled",
            self.environment,
            True,
        )

    def _history_chunk_days(self, interval: str) -> int:
        normalized = normalize_interval(interval)
        fallback = DEFAULT_CHUNK_DAYS.get(normalized, 0)
        if fallback <= 0:
            return 0
        return max(
            1,
            self._get_int_setting(
                f"ibkr_history_chunk_days_{normalized}",
                fallback,
            ),
        )

    def _close_delay_seconds(self) -> int:
        return max(
            0,
            self._get_int_setting(
                "ibkr_official_5m_close_delay_sec",
                DEFAULT_HISTORY_CLOSE_DELAY_SECONDS,
            ),
        )

    def _safe_history_upper_bound_ms(self, interval: str, *, now_ts: float | None = None) -> int:
        normalized = normalize_interval(interval)
        return latest_safe_closed_bucket_ms(
            normalized,
            delay_seconds=self._close_delay_seconds(),
            now_ms=int((now_ts or time.time()) * 1000),
        )
