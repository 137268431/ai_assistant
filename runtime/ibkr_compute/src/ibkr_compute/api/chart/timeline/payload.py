from __future__ import annotations

from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

from ibkr_compute.api.chart.timeline.rows import (
    build_chart_indicator_row,
    build_chart_signal_row,
)
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import load_chart_timeline_source_bars


def build_chart_timeline_payload_from_source(
    environment: str,
    symbol: str,
    interval: str,
    source: dict,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    source_rows = source.get("source_rows") or []
    visible_rows = source.get("visible_rows") or []
    source_meta = source.get("meta") or {}

    if not visible_rows:
        return {
            "ok": True,
            "bars": [],
            "indicator_timeline": [],
            "latest_indicator": None,
            "signals": [],
            "meta": {
                "environment": runtime_environment,
                "symbol": normalized_symbol,
                "interval": chart_tf,
                "start_ms": int(start_ms or 0),
                "end_ms": int(end_ms or 0),
                "visible_bar_count": 0,
                "source_bar_count": len(source_rows),
                "warmup_bars": int(source.get("warmup_limit", 0) or 0),
                "warmup_used": int(source.get("warmup_used", 0) or 0),
                "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
                "reason": "no_visible_bars",
                **source_meta,
            },
        }

    timeline = build_runtime_timeline(
        normalized_symbol,
        normalized_interval,
        source_rows,
        params=api_app.get_signal_generator_params(runtime_environment),
        include_signals=include_signals,
        visible_start_ms=int(start_ms or 0),
        visible_end_ms=int(end_ms or 0),
    )
    timeline_rows = timeline.get("rows") or []
    bars = [
        {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "exchange": str(row.get("exchange", "") or "").upper(),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": row.get("us_time", ""),
            "cn_time": row.get("cn_time", ""),
            "session_type": row.get("session_type", "regular"),
            "open": round(float(row.get("open", 0) or 0), 4),
            "high": round(float(row.get("high", 0) or 0), 4),
            "low": round(float(row.get("low", 0) or 0), 4),
            "close": round(float(row.get("close", 0) or 0), 4),
            "volume": round(float(row.get("volume", 0) or 0), 4),
        }
        for row in timeline_rows
    ]
    indicators = [
        build_chart_indicator_row(runtime_environment, normalized_symbol, normalized_interval, row)
        for row in timeline_rows
    ]
    signals = []
    if include_signals and normalized_interval == "5m":
        signals = [
            signal
            for signal in (
                build_chart_signal_row(runtime_environment, normalized_symbol, normalized_interval, row)
                for row in timeline_rows
            )
            if signal
        ]

    return {
        "ok": True,
        "bars": bars,
        "indicator_timeline": indicators,
        "latest_indicator": indicators[-1] if indicators else None,
        "signals": signals,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": chart_tf,
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "visible_bar_count": len(bars),
            "source_bar_count": len(source_rows),
            "warmup_bars": int(source.get("warmup_limit", 0) or 0),
            "warmup_used": int(source.get("warmup_used", 0) or 0),
            "signal_mode": "computed" if include_signals and normalized_interval == "5m" else "disabled",
            **source_meta,
        },
    }


def build_chart_timeline_payload(
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
    source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    return build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        source,
        start_ms=start_ms,
        end_ms=end_ms,
        include_signals=include_signals,
    )


__all__ = [
    "build_chart_timeline_payload",
    "build_chart_timeline_payload_from_source",
]
