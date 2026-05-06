from __future__ import annotations

from .runtime_support import *
from .market_data_backfill import BacktestMarketDataBackfillMixin
from .market_data_coverage import BacktestMarketDataCoverageMixin
from .market_data_loading import BacktestMarketDataLoadingMixin


class BacktestMarketDataMixin(
    BacktestMarketDataLoadingMixin,
    BacktestMarketDataCoverageMixin,
    BacktestMarketDataBackfillMixin,
):
    pass
