from __future__ import annotations

from .market_universe_support import *
from .market_universe_support import (
    _classify_daily_scan_failure,
    _compact_daily_scan_diagnostics,
    _compact_daily_scan_result_for_state,
    _daily_scan_all_snapshotless,
    _daily_scan_running_age_seconds,
    _extract_daily_scan_failure_evidence,
    _parse_iso_datetime,
    _safe_extra,
    _safe_float,
    _safe_int,
    _service_mod,
    _target_row_is_daily_scan_active,
    _target_row_is_manual,
)
from .market_universe_active_repair import TradingServiceMarketUniverseActiveRepairMixin
from .market_universe_daily_scan import TradingServiceMarketUniverseDailyScanMixin
from .market_universe_reconcile import TradingServiceMarketUniverseReconcileMixin
from .market_universe_targets import TradingServiceMarketUniverseTargetsMixin
from .market_universe_watchlist_idle_topup import TradingServiceMarketUniverseWatchlistIdleTopupMixin


class TradingServiceMarketUniverseMixin(
    TradingServiceMarketUniverseDailyScanMixin,
    TradingServiceMarketUniverseTargetsMixin,
    TradingServiceMarketUniverseWatchlistIdleTopupMixin,
    TradingServiceMarketUniverseActiveRepairMixin,
    TradingServiceMarketUniverseReconcileMixin,
):
    """Compatibility facade for the market-universe orchestration mixins."""

    pass
