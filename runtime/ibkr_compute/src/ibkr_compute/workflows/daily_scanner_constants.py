"""Constants for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
DAILY_SCAN_SOURCE = "daily_scan"
DAILY_SCAN_MODE_SEED = "seed"
DAILY_SCAN_MODE_TOPUP = "topup"
DAILY_SCAN_STAGE = "early_expansion_seed"
DAILY_SCAN_TOPUP_STAGE = "early_expansion_topup"
DAILY_SCAN_PRIMARY_WEIGHT = 2
DAILY_SCAN_SECONDARY_WEIGHT = 1
DAILY_SCAN_READY_TIMEFRAME_BONUS = 1
DEFAULT_SCAN_TIME_ET = "09:20"
DEFAULT_MIN_AVG_10D_VOLUME = 100_000
DEFAULT_MIN_ATR_PCT = 0.15
DEFAULT_MIN_ABS_DAY_CHANGE_PCT = 1.0
DEFAULT_MIN_PREMARKET_VOLUME = 5_000
DEFAULT_DAY_GAIN_TRIGGER_PCT = 4.0
DEFAULT_ACTIVE_TARGET_LIMIT = 24
DEFAULT_ACTIVE_MIN_SCORE = 40.0
DEFAULT_DATA_COMPLETENESS_INTERVALS = "5m,15m,30m,1h,4h,1d"
DEFAULT_DATA_COMPLETENESS_BLOCKING_INTERVALS = "5m"
DEFAULT_INDICATOR_SNAPSHOT_INTERVALS = "5m,15m,30m,1h,4h,1d"
DEFAULT_SCAN_ROLLUP_INTERVALS = "15m,30m,1h,4h,1d"
DEFAULT_SCAN_MATERIALIZE_INTERVALS = "5m,15m,30m,1h"
MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT = 40
MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}

REJECTION_BUCKET_NO_SNAPSHOT = "no_snapshot"
REJECTION_BUCKET_VOTE_TIE = "vote_tie"
REJECTION_BUCKET_AVG_10D = "avg_10d_volume_below_threshold"
REJECTION_BUCKET_PREMARKET = "premarket_volume_below_threshold"
REJECTION_BUCKET_ATR = "atr_pct_below_threshold"
REJECTION_BUCKET_DAY_CHANGE = "day_change_below_threshold"
REJECTION_BUCKET_DATA_INCOMPLETE = "data_incomplete_repairing"
REJECTION_BUCKET_CONTEXT_GATE = "context_gate_not_passed"
REJECTION_BUCKET_ACTIVE_BUDGET = "active_target_limit_full"
REJECTION_BUCKET_TOPUP_ACTIVE_SCORE = "topup_context_gate_not_passed"
REJECTION_BUCKET_TOPUP_ACTIVE_BUDGET = "topup_active_budget_full"

DAILY_SCAN_LONG_PRIMARY_RULES = (
    ("trend_dir", 1, "trend_dir=1"),
    ("dtp_dir", 1, "dtp_dir=1"),
    ("ema_bullish", True, "ema_bullish=true"),
)
DAILY_SCAN_SHORT_PRIMARY_RULES = (
    ("trend_dir", -1, "trend_dir=-1"),
    ("dtp_dir", -1, "dtp_dir=-1"),
    ("ema_bearish", True, "ema_bearish=true"),
)
DAILY_SCAN_LONG_SECONDARY_RULES = (
    ("fractal_bull", True, "fractal_bull=true"),
    ("crsi_os", True, "crsi_os=true"),
    ("sd_lower", True, "sd_lower=true"),
)
DAILY_SCAN_SHORT_SECONDARY_RULES = (
    ("fractal_bear", True, "fractal_bear=true"),
    ("crsi_ob", True, "crsi_ob=true"),
    ("sd_upper", True, "sd_upper=true"),
)
DAILY_SCAN_REASON_RULES = (
    ("fractal_bull", "fractal_bull"),
    ("fractal_bear", "fractal_bear"),
    ("ema_bullish", "ema_bullish"),
    ("ema_bearish", "ema_bearish"),
    ("sd_lower", "sd_lower"),
    ("sd_upper", "sd_upper"),
)
