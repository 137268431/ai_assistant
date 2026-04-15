from __future__ import annotations

import time

from ibkr_compute.market.timeframe_utils import interval_to_ms, normalize_interval

from ibkr_compute.api.chart.timeline.runtime import _api_app


def get_chart_compare_request_period(interval: str, start_ms: int = 0, end_ms: int = 0) -> str:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    interval_ms = max(1, int(interval_to_ms(normalized_interval) or 0))
    effective_end_ms = int(end_ms or 0) or int(time.time() * 1000)
    fallback_start_ms = max(0, effective_end_ms - (api_app.CHART_TIMELINE_VISIBLE_LIMIT * interval_ms))
    effective_start_ms = int(start_ms or 0) if int(start_ms or 0) > 0 else fallback_start_ms
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    day_ms = 24 * 60 * 60 * 1000
    visible_span_ms = max(interval_ms, effective_end_ms - effective_start_ms)
    warmup_span_ms = max(3 * day_ms, warmup_bars * interval_ms + (2 * day_ms))
    request_days = max(1, min(730, (visible_span_ms + warmup_span_ms + day_ms - 1) // day_ms))
    return f"{int(request_days)}d"


__all__ = ["get_chart_compare_request_period"]
