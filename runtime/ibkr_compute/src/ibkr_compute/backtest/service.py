"""Compatibility backtest service entrypoint.

Primary implementation now lives in ibkr_compute.backtest.runtime_service.
"""

from .runtime_service import BacktestCancelled, BacktestService

__all__ = ["BacktestCancelled", "BacktestService"]
