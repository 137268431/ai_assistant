from __future__ import annotations

from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

from ibkr_compute.api.chart.compare.diff import _build_chart_compare_summary
from ibkr_compute.api.chart.compare.source import load_chart_compare_ibkr_source_bars
from ibkr_compute.api.chart.timeline.payload import (
    build_chart_timeline_payload_from_source,
)
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import (
    load_chart_timeline_source_bars,
)


def build_chart_compare_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)

    api_app.refresh_daily_close_cache([runtime_environment])
    stored_source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    stored_source["meta"] = {"chain": "stored_bars"}
    stored_timeline = build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        stored_source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )

    ibkr_source = load_chart_compare_ibkr_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    ibkr_timeline = build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        ibkr_source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )

    comparison = _build_chart_compare_summary(stored_timeline, ibkr_timeline)
    return {
        "ok": True,
        "stored_timeline": stored_timeline,
        "ibkr_timeline": ibkr_timeline,
        "comparison": comparison,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": interval_to_chart_tf(normalized_interval),
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "include_signals": bool(include_signals and normalized_interval == "5m"),
            "stored": stored_timeline.get("meta") or {},
            "ibkr": ibkr_timeline.get("meta") or {},
        },
    }


__all__ = ["build_chart_compare_payload"]
