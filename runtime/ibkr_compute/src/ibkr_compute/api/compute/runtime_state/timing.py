from __future__ import annotations

import time

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.market.timeframe_utils import interval_to_ms, normalize_interval


def is_recent_signal_bar(bar_time_ms: int, interval: str) -> bool:
    return bar_time_ms >= int(time.time() * 1000) - max(interval_to_ms(interval) * 3, 15 * 60 * 1000)


def get_fetch_since_ms(environment: str, interval: str) -> int:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    key = (environment, normalized_interval)
    last_fetch = int(api_app.last_interval_fetch_ms.get(key, 0) or 0)
    if last_fetch <= 0:
        last_fetch = max(
            (
                int(bar_ms or 0)
                for (env, _symbol, interval), bar_ms in api_app.last_processed_ms.items()
                if env == environment and interval == normalized_interval
            ),
            default=0,
        )
    if last_fetch > 0:
        return max(0, last_fetch - interval_to_ms(normalized_interval) * 2)
    return 0


__all__ = [
    "get_fetch_since_ms",
    "is_recent_signal_bar",
]
