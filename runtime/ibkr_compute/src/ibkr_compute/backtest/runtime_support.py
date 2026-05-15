from __future__ import annotations

import gc
import json
import math
import os
import sqlite3
import statistics
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest.constants import *
from ibkr_compute.backtest.execution_cost import (
    apply_execution_slippage,
    build_execution_cost_profile,
    calculate_execution_commission,
    compact_execution_cost_profile,
    execution_side,
    maybe_stop_gap_reference,
    summarize_execution_costs,
)
from ibkr_compute.backtest.exceptions import BacktestCancelled
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS, IndicatorEngine, indicator_ready_bar_count
from ibkr_compute.core.active_window_admission import build_active_window_admission_item
from ibkr_compute.core.risk_management import (
    compute_atr_tightened_stop,
    compute_exit_policy_stop_update,
    compute_exit_policy_target_update,
    compute_exit_policy_time_exit,
    exit_policy_uses_hard_target,
    exit_policy_uses_safety_target,
)
from ibkr_compute.core.exit_policy import is_signal_mode_adaptive_exit_profile
from ibkr_compute.core.intraday_harvest import (
    ACTION_FULL_EXIT,
    ACTION_TIGHTEN_STOP,
    evaluate_intraday_harvest,
    normalize_harvest_settings,
)
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.setup_registry import build_setup_metadata, normalize_setup_name
from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.bar_coverage_daily import (
    DAILY_COVERAGE_OK_STATUSES,
    build_range_daily_coverage,
    load_daily_coverage_rows,
    market_date_from_ms,
    session_mode_for_bar_time,
    summarize_symbol_daily_coverage,
    trading_date_strings_from_ms,
)
from ibkr_compute.market.data_backfill import DataBackfill
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite, upsert_bar_coverage_daily, upsert_bars
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import (
    ET,
    HIGHER_INTERVALS,
    build_runtime_timestamps,
    build_signal_id,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
    normalize_interval,
)
from ibkr_compute.workflows.daily_scanner import DailyScanner, _load_scan_settings


def runtime_backtest_sqlite_path() -> str:
    module = sys.modules.get("ibkr_compute.backtest.runtime_service")
    return str(getattr(module, "BACKTEST_SQLITE_PATH", BACKTEST_SQLITE_PATH) or "")


def runtime_indicator_engine():
    module = sys.modules.get("ibkr_compute.backtest.runtime_service")
    return getattr(module, "IndicatorEngine", IndicatorEngine)


def runtime_signal_generator():
    module = sys.modules.get("ibkr_compute.backtest.runtime_service")
    return getattr(module, "SignalGenerator", SignalGenerator)


__all__ = [name for name in globals() if not name.startswith("__")]
