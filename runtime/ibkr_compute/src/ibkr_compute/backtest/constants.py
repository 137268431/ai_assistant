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
    "BACKTEST_DAILY_SELECTION_CACHE_COLLECTION",
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
    "DEFAULT_WATCHLIST_MAX_SYMBOLS",
    "MAX_BACKTEST_SYMBOLS",
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
    "DEFAULT_BACKTEST_RESOURCE_GUARD_ENABLED",
    "DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD",
    "DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB",
    "DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB",
    "DEFAULT_BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS",
    "DEFAULT_BACKTEST_RESOURCE_GUARD_CHECK_STEPS",
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
    "WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR",
    "DEFAULT_MARKET_MONITOR_SYMBOLS",
]

BACKTEST_ENVIRONMENT = "backtest"
RUN_COLLECTION = "ibkr_backtest_runs"
TRADE_COLLECTION = "ibkr_backtest_trades"
BATCH_COLLECTION = "ibkr_backtest_batches"
BACKTEST_INDICATOR_COLLECTION = "ibkr_backtest_indicators"
BACKTEST_SIGNAL_COLLECTION = "ibkr_backtest_signals"
BACKTEST_TARGET_COLLECTION = "ibkr_backtest_targets"
BACKTEST_DAILY_SELECTION_CACHE_COLLECTION = "ibkr_backtest_daily_selection_cache"
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


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 500) -> int:
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1000000.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except Exception:
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return bool(default)
    text = str(raw_value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _env_symbol_tuple(name: str, default: str) -> tuple[str, ...]:
    items = []
    for chunk in str(os.environ.get(name, default) or "").replace("\n", ",").split(","):
        symbol = str(chunk or "").strip().upper()
        if symbol and symbol not in items:
            items.append(symbol)
    return tuple(items)


DEFAULT_MAX_SYMBOLS = 20
MAX_BACKTEST_SYMBOLS = _env_int("BACKTEST_MAX_SYMBOLS", 200, minimum=1, maximum=500)
DEFAULT_WATCHLIST_MAX_SYMBOLS = _env_int(
    "BACKTEST_DEFAULT_WATCHLIST_MAX_SYMBOLS",
    MAX_BACKTEST_SYMBOLS,
    minimum=1,
    maximum=MAX_BACKTEST_SYMBOLS,
)
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
DEFAULT_BACKTEST_RESOURCE_GUARD_ENABLED = _env_bool("BACKTEST_RESOURCE_GUARD_ENABLED", True)
DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD = _env_float("BACKTEST_RESOURCE_GUARD_MAX_LOAD", 4.0, minimum=0.0, maximum=128.0)
DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB = _env_int(
    "BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB",
    3500,
    minimum=0,
    maximum=65536,
)
DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB = _env_int(
    "BACKTEST_RESOURCE_GUARD_MAX_RSS_MB",
    2500,
    minimum=0,
    maximum=65536,
)
DEFAULT_BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS = _env_float(
    "BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS",
    0.5,
    minimum=0.0,
    maximum=30.0,
)
DEFAULT_BACKTEST_RESOURCE_GUARD_CHECK_STEPS = _env_int(
    "BACKTEST_RESOURCE_GUARD_CHECK_STEPS",
    50,
    minimum=1,
    maximum=10000,
)
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
WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR = "market_monitor"
DEFAULT_MARKET_MONITOR_SYMBOLS = _env_symbol_tuple("BACKTEST_MARKET_MONITOR_SYMBOLS", "QQQ,SPY,VIX")
