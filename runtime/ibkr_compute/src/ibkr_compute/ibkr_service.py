"""Compatibility runtime entrypoint.

Primary implementation now lives in ibkr_compute.orchestration.trading_service.
"""

from ibkr_compute.orchestration.trading_service import IBKRTradingService, main

__all__ = ["IBKRTradingService", "main"]


if __name__ == "__main__":
    main()
