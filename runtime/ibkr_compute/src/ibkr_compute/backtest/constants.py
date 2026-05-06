from __future__ import annotations

import os

from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, ET
from ibkr_compute.order.order_lifecycle import DEFAULT_POSITION_LIMIT_MAX as LIVE_DEFAULT_POSITION_LIMIT_MAX
from ibkr_compute.signal.signal_processor import (
    DEFAULT_ORDER_WINDOW_END as LIVE_DEFAULT_ORDER_WINDOW_END,
    DEFAULT_SIGNAL_EXPIRY_MINUTES as LIVE_DEFAULT_SIGNAL_EXPIRY_MINUTES,
    DEFAULT_TRADE_WINDOW_END as LIVE_DEFAULT_TRADE_WINDOW_END,
    DEFAULT_TRADE_WINDOW_START as LIVE_DEFAULT_TRADE_WINDOW_START,
)

__all__ = [
    "ET",
    "BACKTEST_ENVIRONMENT",
    "RUN_COLLECTION",
    "TRADE_COLLECTION",
    "BATCH_COLLECTION",
    "BACKTEST_INDICATOR_COLLECTION",
    "BACKTEST_SIGNAL_COLLECTION",
    "BACKTEST_TARGET_COLLECTION",
    "BACKTEST_REVERSE_SIGNAL_COLLECTION",
    "TV_INDICATOR_COLLECTION",
    "TV_SIGNAL_COLLECTION",
    "RUN_STATUS_VALUES",
    "SESSION_MODE_VALUES",
    "SYMBOL_SOURCE_VALUES",
    "EXECUTION_MODEL_VALUES",
    "BORROW_LIMIT_MODE_VALUES",
    "SIGNAL_PRIORITY_VALUES",
    "MANUAL_CONFIRM_MODE_VALUES",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_MAX_PAGES",
    "MAX_REPLAY_ROWS",
    "MAX_BATCH_VARIANTS",
    "TV_COMPARE_SAMPLE_LIMIT",
    "BACKTEST_WARMUP_BARS",
    "DEFAULT_SCAN_CUTOFF_TIME",
    "DEFAULT_BACKTEST_RETENTION_LIMIT",
    "MAX_BACKTEST_RETENTION_LIMIT",
    "MAX_BACKTEST_WARMUP_BARS",
    "DEFAULT_BACKTEST_BACKFILL_CONCURRENCY",
    "MAX_BACKTEST_BACKFILL_CONCURRENCY",
    "BACKTEST_COVERAGE_EDGE_GRACE_DAYS",
    "DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS",
    "MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS",
    "DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES",
    "MAX_BACKTEST_BACKFILL_MAX_BATCHES",
    "DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS",
    "MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS",
    "DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES",
    "MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES",
    "BACKTEST_PREFLIGHT_HEARTBEAT_SECONDS",
    "SCAN_INTERVALS",
    "BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD",
    "BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD",
    "BACKTEST_SQLITE_PATH",
    "DEFAULT_PORTFOLIO_TRADE_WINDOW_START",
    "DEFAULT_PORTFOLIO_TRADE_WINDOW_END",
    "DEFAULT_PORTFOLIO_ORDER_WINDOW_END",
    "DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES",
    "DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX",
    "TV_INDICATOR_EXTRA_FIELDS",
    "TV_SIGNAL_CORE_FIELDS",
    "TV_SIGNAL_EXTRA_FIELDS",
    "WATCHLIST_SYMBOL_ROLE_TRADE",
]

BACKTEST_ENVIRONMENT = "backtest"
RUN_COLLECTION = "ibkr_backtest_runs"
TRADE_COLLECTION = "ibkr_backtest_trades"
BATCH_COLLECTION = "ibkr_backtest_batches"
BACKTEST_INDICATOR_COLLECTION = "ibkr_backtest_indicators"
BACKTEST_SIGNAL_COLLECTION = "ibkr_backtest_signals"
BACKTEST_TARGET_COLLECTION = "ibkr_backtest_targets"
BACKTEST_REVERSE_SIGNAL_COLLECTION = "ibkr_backtest_reverse_signals"
TV_INDICATOR_COLLECTION = "tv_indicators"
TV_SIGNAL_COLLECTION = "tv_signals"
RUN_STATUS_VALUES = {"queued", "running", "completed", "failed", "cancelled"}
SESSION_MODE_VALUES = {"extended", "regular"}
SYMBOL_SOURCE_VALUES = {"manual", "targets", "watchlist", "daily_scan_replay"}
EXECUTION_MODEL_VALUES = {"symbol_independent", "portfolio_stream"}
BORROW_LIMIT_MODE_VALUES = {"none", "fixed", "account_buying_power"}
SIGNAL_PRIORITY_VALUES = {"daily_target_rank", "signal_quality", "liquidity"}
MANUAL_CONFIRM_MODE_VALUES = {"auto", "delayed", "strict"}
DEFAULT_MAX_SYMBOLS = 20
DEFAULT_MAX_PAGES = 1200
MAX_REPLAY_ROWS = 240
MAX_BATCH_VARIANTS = 16
TV_COMPARE_SAMPLE_LIMIT = 8
BACKTEST_WARMUP_BARS = 320
DEFAULT_SCAN_CUTOFF_TIME = "09:20"
DEFAULT_BACKTEST_RETENTION_LIMIT = 30
MAX_BACKTEST_RETENTION_LIMIT = 200
MAX_BACKTEST_WARMUP_BARS = 2000
DEFAULT_BACKTEST_BACKFILL_CONCURRENCY = 4
MAX_BACKTEST_BACKFILL_CONCURRENCY = 5
BACKTEST_COVERAGE_EDGE_GRACE_DAYS = 5
DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS = 600
MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS = 3600
DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES = 120
MAX_BACKTEST_BACKFILL_MAX_BATCHES = 240
DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS = 20
MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS = 60
DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES = 1
MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES = 5
BACKTEST_PREFLIGHT_HEARTBEAT_SECONDS = 15
SCAN_INTERVALS = tuple(COMPUTE_INTERVALS)
BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD = 0.05
BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD = 0.1
BACKTEST_SQLITE_PATH = os.environ.get("PB_SQLITE_PATH", "/opt/pocketbase/pb_data/data.db")


def _hhmm_from_tuple(value: tuple[int, int]) -> str:
    return f"{int(value[0]):02d}:{int(value[1]):02d}"


DEFAULT_PORTFOLIO_TRADE_WINDOW_START = _hhmm_from_tuple(LIVE_DEFAULT_TRADE_WINDOW_START)
DEFAULT_PORTFOLIO_TRADE_WINDOW_END = _hhmm_from_tuple(LIVE_DEFAULT_TRADE_WINDOW_END)
DEFAULT_PORTFOLIO_ORDER_WINDOW_END = _hhmm_from_tuple(LIVE_DEFAULT_ORDER_WINDOW_END)
DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES = LIVE_DEFAULT_SIGNAL_EXPIRY_MINUTES
DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX = LIVE_DEFAULT_POSITION_LIMIT_MAX

TV_INDICATOR_EXTRA_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema_fast",
    "ema_slow",
    "ema_trend",
    "ema_longest",
    "slope_slow",
    "slope_trend",
    "slope_longest",
    "ema_bull_touch",
    "ema_bear_touch",
    "ema_bullish",
    "ema_bearish",
    "fractal_bull",
    "fractal_bear",
    "sd_reg",
    "sd_std_dev",
    "sd_upper",
    "sd_lower",
    "sd_zone",
    "sd_trend",
    "dtp_avg",
    "dtp_atr",
    "dtp_dir",
    "dtp_phase",
    "dtp_phase_bars",
    "atr",
    "atr_raw",
    "atr_pct",
    "crsi",
    "crsi_db",
    "crsi_ub",
    "crsi_ob",
    "crsi_os",
    "crsi_bull_div",
    "crsi_bear_div",
    "crsi_hid_bull",
    "crsi_hid_bear",
    "obv_rsi",
    "obv_bull_div",
    "obv_bear_div",
    "obv_hid_bull",
    "obv_hid_bear",
    "vwap",
    "vwap_upper1",
    "vwap_lower1",
    "vwap_upper2",
    "vwap_lower2",
    "vwap_dist",
    "vwap_bullish",
    "trend_dir",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
)

TV_SIGNAL_CORE_FIELDS = (
    "signal_id",
    "signal",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "rr",
    "shares",
    "interval",
    "bar_time_ms",
    "us_time",
    "cn_time",
    "reason",
)

TV_SIGNAL_EXTRA_FIELDS = (
    "close",
    "atr",
    "atr_raw",
    "atr_pct",
    "sd_zone",
    "sd_trend",
    "dtp_dir",
    "dtp_phase",
    "crsi_state",
    "sl_dist_pct",
    "sl_atr_ratio",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
    "bar_time_ms",
    "chart_tf",
)



WATCHLIST_SYMBOL_ROLE_TRADE = "trade"

