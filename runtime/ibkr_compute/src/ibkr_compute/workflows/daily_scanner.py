"""
每日标的扫描器

当前实现改为 09:20 ET 的日内波动型初筛：
  1. 读取 trade watchlist
  2. 复用 screener 聚合得到量能 / 波动指标
  3. 使用 1d/4h 定势、1h/30m 看结构、15m/5m 看触发
  4. 叠加 avg_10d_volume / premarket_volume / atr_pct / day_change_pct 的质量门
  5. 只有形成 multi-timeframe context 的标的写成 active，预算溢出才保留 candidate 兼容
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ibkr_compute.api.compute.runtime_state.universe import get_market_monitor_symbols
from ibkr_compute.api.market.screener.payload import build_screener_payload
from ibkr_compute.api.market.screener.runtime import get_api_app
from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.core.time_utils import ET
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

from .daily_scanner_constants import (
    DAILY_SCAN_LONG_PRIMARY_RULES,
    DAILY_SCAN_LONG_SECONDARY_RULES,
    DAILY_SCAN_MODE_SEED,
    DAILY_SCAN_MODE_TOPUP,
    DAILY_SCAN_PRIMARY_WEIGHT,
    DAILY_SCAN_READY_TIMEFRAME_BONUS,
    DAILY_SCAN_REASON_RULES,
    DAILY_SCAN_SECONDARY_WEIGHT,
    DAILY_SCAN_SHORT_PRIMARY_RULES,
    DAILY_SCAN_SHORT_SECONDARY_RULES,
    DAILY_SCAN_SOURCE,
    DAILY_SCAN_STAGE,
    DAILY_SCAN_TOPUP_STAGE,
    DEFAULT_DATA_COMPLETENESS_INTERVALS,
    DEFAULT_INDICATOR_SNAPSHOT_INTERVALS,
    DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
    DEFAULT_MIN_ATR_PCT,
    DEFAULT_MIN_AVG_10D_VOLUME,
    DEFAULT_MIN_PREMARKET_VOLUME,
    DEFAULT_SCAN_MATERIALIZE_INTERVALS,
    DEFAULT_SCAN_TIME_ET,
    MANUAL_TARGET_SOURCES,
    MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT,
    REJECTION_BUCKET_ATR,
    REJECTION_BUCKET_AVG_10D,
    REJECTION_BUCKET_DATA_INCOMPLETE,
    REJECTION_BUCKET_DAY_CHANGE,
    REJECTION_BUCKET_NO_SNAPSHOT,
    REJECTION_BUCKET_PREMARKET,
    REJECTION_BUCKET_VOTE_TIE,
    WATCHLIST_SYMBOL_ROLE_TRADE,
)
from .daily_scanner_evaluate import DailyScannerEvaluateMixin as _DailyScannerEvaluateMixin
from .daily_scanner_gates import DailyScannerDataCompletenessMixin as _DailyScannerDataCompletenessMixin
from .daily_scanner_materialize import DailyScannerMaterializeMixin as _DailyScannerMaterializeMixin
from .daily_scanner_reconcile import DailyScannerReconcileMixin as _DailyScannerReconcileMixin
from .daily_scanner_run import DailyScannerRunMixin as _DailyScannerRunMixin
from .daily_scanner_settings import (
    _load_scan_settings as _load_scan_settings_impl,
    build_daily_scan_rule_summary as _build_daily_scan_rule_summary_impl,
)
from .daily_scanner_snapshots import DailyScannerStoredSnapshotsMixin as _DailyScannerStoredSnapshotsMixin
from .daily_scanner_support import (
    _daily_scan_matches_any,
    _daily_scan_rule_matches,
    _flatten_rejection_examples,
    _format_metric_value,
    _format_threshold,
    _metric_rank_bonus,
    _new_rejection_trackers,
    _normalize_scan_mode,
    _record_rejection,
    _safe_extra,
    _safe_float,
    _safe_int,
    _target_row_is_manual,
)
from .daily_scanner_watchlist import DailyScannerWatchlistMixin as _DailyScannerWatchlistMixin


def _load_scan_settings(environment: str) -> dict:
    return _load_scan_settings_impl(
        environment,
        api_app_getter=get_api_app,
        monitor_symbols_getter=get_market_monitor_symbols,
    )


def build_daily_scan_rule_summary(environment: str | None = None) -> dict:
    return _build_daily_scan_rule_summary_impl(
        environment,
        api_app_getter=get_api_app,
        monitor_symbols_getter=get_market_monitor_symbols,
        settings_loader=_load_scan_settings,
    )


class DailyScanner(
    _DailyScannerRunMixin,
    _DailyScannerEvaluateMixin,
    _DailyScannerDataCompletenessMixin,
    _DailyScannerMaterializeMixin,
    _DailyScannerStoredSnapshotsMixin,
    _DailyScannerReconcileMixin,
    _DailyScannerWatchlistMixin,
):
    def __init__(self, pb_client: PBClient, engines: dict):
        self.pb_client = pb_client
        self.engines = engines
        self.api_app = get_api_app()

    def _load_scan_settings(self, environment: str) -> dict:
        return _load_scan_settings(environment)

    def _build_screener_payload(self, *args, **kwargs) -> dict:
        return build_screener_payload(*args, **kwargs)

    def _build_bar_freshness_planner(self, environment: str) -> BarFreshnessPlanner:
        return BarFreshnessPlanner(self.pb_client, getattr(self.api_app, "cfg", None), environment=environment)
