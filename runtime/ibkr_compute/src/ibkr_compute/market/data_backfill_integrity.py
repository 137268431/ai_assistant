"""Stored-bar integrity inspection for data backfill."""

from __future__ import annotations

import logging
import sys
from typing import Dict

from .data_backfill_support import (
    _regular_session_expected_bar_times,
    _regular_session_gap_summary,
)
from .pocketbase_sqlite import (
    fetch_bar_times_in_range,
    fetch_recent_bars,
    open_pb_sqlite,
)
from .timeframe_utils import format_us_time, normalize_interval

logger = logging.getLogger("ibkr_compute.market.data_backfill")


def _facade_attr(name: str, fallback):
    facade = sys.modules.get("ibkr_compute.market.data_backfill")
    return getattr(facade, name, fallback)


class DataBackfillIntegrityMixin:
    def get_integrity_snapshot(
        self,
        symbol: str,
        interval: str = "5m",
        min_bars: int = 0,
        gap_lookback: int = 80,
    ) -> Dict:
        normalized = normalize_interval(interval)
        snapshot = {
            "symbol": str(symbol or "").upper(),
            "interval": normalized,
            "stored_bar_count": 0,
            "scanned_row_count": 0,
            "latest_stored_ms": 0,
            "oldest_loaded_ms": 0,
            "gap_count": 0,
            "gap_examples": [],
            "duplicate_count": 0,
            "duplicate_examples": [],
            "bad_ohlc_count": 0,
            "bad_ohlc_examples": [],
        }
        if not self.pb_client and not self._direct_sqlite_read_enabled():
            return snapshot

        safe_symbol = snapshot["symbol"].replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        bars_needed = max(1, int(min_bars or 0), int(gap_lookback or 0), 400)
        max_pages = max(1, min(8, (bars_needed + 199) // 200))

        rows = []
        sqlite_read_ok = False
        if self._direct_sqlite_read_enabled():
            try:
                open_sqlite = _facade_attr("open_pb_sqlite", open_pb_sqlite)
                fetch_recent = _facade_attr("fetch_recent_bars", fetch_recent_bars)
                with open_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    rows = fetch_recent(
                        conn,
                        snapshot["symbol"],
                        normalized,
                        self.environment,
                        limit=max_pages * 200,
                        include_legacy_empty=False,
                    )
                sqlite_read_ok = True
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    logger.warning(
                        "Failed to inspect stored bars via SQLite for %s/%s: %s",
                        symbol,
                        normalized,
                        exc,
                    )
                    return snapshot
                logger.debug(
                    "Direct SQLite stored bar inspection failed for %s/%s, falling back to PocketBase API: %s",
                    symbol,
                    normalized,
                    exc,
                )

        if not rows and not sqlite_read_ok:
            if not self.pb_client:
                return snapshot
            try:
                rows = self.pb_client.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        f'interval = "{safe_interval}" && '
                        f'environment = "{safe_environment}"'
                    ),
                    sort="-bar_time_ms",
                    max_pages=max_pages,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to inspect stored bars for %s/%s: %s",
                    symbol,
                    normalized,
                    exc,
                )
                return snapshot

        if not rows:
            return snapshot

        snapshot["stored_bar_count"] = len(rows)
        snapshot["scanned_row_count"] = len(rows)
        snapshot["latest_stored_ms"] = int(rows[0].get("bar_time_ms", 0) or 0)
        snapshot["oldest_loaded_ms"] = int(rows[-1].get("bar_time_ms", 0) or 0)

        seen_bar_times = set()
        for row in rows:
            bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_time_ms > 0:
                if bar_time_ms in seen_bar_times:
                    snapshot["duplicate_count"] += 1
                    if len(snapshot["duplicate_examples"]) < 4:
                        snapshot["duplicate_examples"].append({
                            "bar_time_ms": bar_time_ms,
                            "us_time": str(row.get("us_time", "") or ""),
                        })
                else:
                    seen_bar_times.add(bar_time_ms)

            try:
                open_px = float(row.get("open", 0) or 0)
                high_px = float(row.get("high", 0) or 0)
                low_px = float(row.get("low", 0) or 0)
                close_px = float(row.get("close", 0) or 0)
            except Exception:
                open_px = high_px = low_px = close_px = 0.0

            invalid_ohlc = (
                open_px <= 0
                or high_px <= 0
                or low_px <= 0
                or close_px <= 0
                or high_px < max(open_px, close_px, low_px)
                or low_px > min(open_px, close_px, high_px)
            )
            if invalid_ohlc:
                snapshot["bad_ohlc_count"] += 1
                if len(snapshot["bad_ohlc_examples"]) < 4:
                    snapshot["bad_ohlc_examples"].append({
                        "bar_time_ms": bar_time_ms,
                        "us_time": str(row.get("us_time", "") or ""),
                        "open": open_px,
                        "high": high_px,
                        "low": low_px,
                        "close": close_px,
                    })

        recent_rows = list(reversed(rows[: max(3, int(gap_lookback or 0))]))
        gap_summary = _regular_session_gap_summary(
            recent_rows,
            normalized,
            same_day_only=True,
            example_limit=4,
        )
        snapshot["gap_count"] = int(gap_summary.get("gap_count", 0) or 0)
        snapshot["gap_examples"] = list(gap_summary.get("gap_examples") or [])

        return snapshot

    def get_required_sequence_snapshot(
        self,
        symbol: str,
        interval: str = "5m",
        start_ms: int = 0,
        end_ms: int = 0,
        example_limit: int = 4,
    ) -> Dict:
        normalized = normalize_interval(interval)
        normalized_symbol = str(symbol or "").strip().upper()
        range_start_ms = int(start_ms or 0)
        range_end_ms = int(end_ms or 0)
        snapshot = {
            "symbol": normalized_symbol,
            "interval": normalized,
            "start_ms": range_start_ms,
            "start_us": format_us_time(range_start_ms) if range_start_ms > 0 else "",
            "end_ms": range_end_ms,
            "end_us": format_us_time(range_end_ms) if range_end_ms > 0 else "",
            "expected_count": 0,
            "stored_count": 0,
            "missing_count": 0,
            "missing_bar_times": [],
            "missing_us_times": [],
            "query_error": "",
        }
        if not self.pb_client and not self._direct_sqlite_read_enabled():
            return snapshot
        if range_start_ms <= 0 or range_end_ms <= 0 or range_start_ms > range_end_ms:
            return snapshot

        expected_bar_times = _regular_session_expected_bar_times(range_start_ms, range_end_ms, normalized)
        snapshot["expected_count"] = len(expected_bar_times)
        if not expected_bar_times:
            return snapshot

        safe_symbol = normalized_symbol.replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        max_pages = max(1, min(8, (len(expected_bar_times) + 199) // 200))

        stored_bar_times = []
        sqlite_read_ok = False
        if self._direct_sqlite_read_enabled():
            try:
                open_sqlite = _facade_attr("open_pb_sqlite", open_pb_sqlite)
                fetch_bar_times = _facade_attr("fetch_bar_times_in_range", fetch_bar_times_in_range)
                with open_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    stored_bar_times = fetch_bar_times(
                        conn,
                        normalized_symbol,
                        normalized,
                        self.environment,
                        start_ms=range_start_ms,
                        end_ms=range_end_ms,
                        session_type="regular",
                        include_legacy_empty=False,
                    )
                sqlite_read_ok = True
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    snapshot["query_error"] = str(exc)
                    logger.warning(
                        "Failed to inspect required sequence via SQLite for %s/%s (%s -> %s): %s",
                        normalized_symbol,
                        normalized,
                        snapshot["start_us"] or range_start_ms,
                        snapshot["end_us"] or range_end_ms,
                        exc,
                    )
                    return snapshot
                logger.debug(
                    "Direct SQLite sequence inspection failed for %s/%s, falling back to PocketBase API: %s",
                    normalized_symbol,
                    normalized,
                    exc,
                )

        if not stored_bar_times and not sqlite_read_ok:
            if not self.pb_client:
                return snapshot
            try:
                rows = self.pb_client.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        f'interval = "{safe_interval}" && '
                        f'environment = "{safe_environment}" && '
                        f'session_type = "regular" && '
                        f'bar_time_ms >= {range_start_ms} && '
                        f'bar_time_ms <= {range_end_ms}'
                    ),
                    sort="bar_time_ms",
                    max_pages=max_pages,
                )
            except Exception as exc:
                snapshot["query_error"] = str(exc)
                logger.warning(
                    "Failed to inspect required sequence for %s/%s (%s -> %s): %s",
                    normalized_symbol,
                    normalized,
                    snapshot["start_us"] or range_start_ms,
                    snapshot["end_us"] or range_end_ms,
                    exc,
                )
                return snapshot

            stored_bar_times = sorted({
                int(row.get("bar_time_ms", 0) or 0)
                for row in (rows or [])
                if int(row.get("bar_time_ms", 0) or 0) > 0
            })
        snapshot["stored_count"] = len(stored_bar_times)

        stored_set = set(stored_bar_times)
        missing_bar_times = [bar_time_ms for bar_time_ms in expected_bar_times if bar_time_ms not in stored_set]
        snapshot["missing_count"] = len(missing_bar_times)
        snapshot["missing_bar_times"] = missing_bar_times
        snapshot["missing_us_times"] = [
            format_us_time(bar_time_ms)
            for bar_time_ms in missing_bar_times[: max(1, int(example_limit or 0))]
        ]
        return snapshot
