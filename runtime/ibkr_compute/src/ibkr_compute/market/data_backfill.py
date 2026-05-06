"""
Historical bar backfill across the IBKR timeframes used by the pipeline.
"""

from __future__ import annotations

from collections import deque
import logging
import threading
import time
from typing import Dict

from ibkr_compute.broker import BrokerAdapter

from .data_backfill_backfill import DataBackfillBackfillMixin
from .data_backfill_history import DataBackfillHistoryMixin
from .data_backfill_integrity import DataBackfillIntegrityMixin
from .data_backfill_settings import DataBackfillSettingsMixin
from .data_backfill_support import (
    DEFAULT_BACKFILL_INTERVALS,
    DEFAULT_CHUNK_DAYS,
    DEFAULT_HISTORY_CLOSE_DELAY_SECONDS,
    ENVIRONMENT,
    IB_BAR_SIZE_MAP,
    IB_DURATION_SUFFIX,
    INTERVAL_DELAY_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    PERIOD_MAP,
    REQUEST_SPACING_SECONDS,
    RETRYABLE_STATUS_CODES,
    RETRY_BASE_DELAY_SECONDS,
    TRACE_RECENT_LIMIT,
    TRACE_SLOW_SECONDS,
    _format_ib_end_datetime,
    _parse_intervals,
    _parse_period_days,
    _period_from_days,
    _regular_session_expected_bar_times,
    _regular_session_gap_summary,
    _to_ib_bar_size,
    _to_ib_duration,
)
from .data_backfill_tracing import DataBackfillTracingMixin
from .pocketbase_sqlite import (
    fetch_bar_times_in_range,
    fetch_latest_bar,
    fetch_recent_bars,
    open_pb_sqlite,
)
from .timeframe_utils import (
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
    latest_safe_closed_bucket_ms,
    normalize_interval,
)

logger = logging.getLogger(__name__)


class DataBackfill(
    DataBackfillSettingsMixin,
    DataBackfillTracingMixin,
    DataBackfillHistoryMixin,
    DataBackfillIntegrityMixin,
    DataBackfillBackfillMixin,
):
    def __init__(
        self,
        gateway_url: str = None,
        data_writer=None,
        config=None,
        environment: str = ENVIRONMENT,
        broker: BrokerAdapter | None = None,
    ):
        self.data_writer = data_writer
        self.pb_client = getattr(data_writer, "pb_client", None) if data_writer else None
        self.config = config
        self.environment = str(environment or ENVIRONMENT).strip().lower() or ENVIRONMENT
        self.broker = broker or BrokerAdapter()
        self.default_intervals = list(DEFAULT_BACKFILL_INTERVALS)
        self._backfill_count = 0
        self._request_count = 0
        self._retry_count = 0
        self._throttle_count = 0
        self._count_lock = threading.Lock()
        self._request_gate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._trace_lock = threading.RLock()
        self._recent_traces = deque(maxlen=max(TRACE_RECENT_LIMIT, 100))
        self._last_trace: Dict = {}
        self._active_requests = 0
        self._active_symbol_counts: Dict[str, int] = {}
