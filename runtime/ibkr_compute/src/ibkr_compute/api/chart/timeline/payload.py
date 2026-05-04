from __future__ import annotations

from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

from ibkr_compute.api.chart.timeline.rows import (
    build_chart_indicator_row,
    build_chart_signal_row,
    build_chart_trace_row,
)
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import (
    build_chart_source_window_from_rows,
    load_chart_timeline_source_bars,
)


def _normalize_preview_bar(preview_bar: dict | None, symbol: str, interval: str) -> dict | None:
    if not isinstance(preview_bar, dict):
        return None
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    bar_ms = int(preview_bar.get("bar_time_ms", 0) or 0)
    if bar_ms <= 0:
        return None
    return {
        **preview_bar,
        "symbol": normalized_symbol,
        "interval": normalized_interval,
        "bar_time_ms": bar_ms,
        "open": round(float(preview_bar.get("open", 0) or 0), 4),
        "high": round(float(preview_bar.get("high", 0) or 0), 4),
        "low": round(float(preview_bar.get("low", 0) or 0), 4),
        "close": round(float(preview_bar.get("close", 0) or 0), 4),
        "volume": round(float(preview_bar.get("volume", 0) or 0), 4),
        "us_time": str(preview_bar.get("us_time", "") or ""),
        "cn_time": str(preview_bar.get("cn_time", "") or ""),
        "session_type": str(preview_bar.get("session_type", "regular") or "regular"),
        "exchange": str(preview_bar.get("exchange", "") or "").upper(),
        "preview": True,
        "is_preview": True,
    }


def build_chart_timeline_payload_from_source(
    environment: str,
    symbol: str,
    interval: str,
    source: dict,
    start_ms: int = 0,
    end_ms: int = 0,
    include_signals: bool = True,
    include_trace: bool = False,
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
            "trace_timeline": [],
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
                "trace_mode": "computed" if include_trace else "disabled",
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
        include_trace=include_trace,
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
            "preview": bool(row.get("preview") or row.get("is_preview")),
            "is_preview": bool(row.get("preview") or row.get("is_preview")),
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
    trace_timeline = []
    if include_trace:
        trace_timeline = [
            build_chart_trace_row(runtime_environment, normalized_symbol, normalized_interval, row)
            for row in timeline_rows
        ]

    return {
        "ok": True,
        "bars": bars,
        "indicator_timeline": indicators,
        "latest_indicator": indicators[-1] if indicators else None,
        "signals": signals,
        "trace_timeline": trace_timeline,
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
            "trace_mode": "computed" if include_trace else "disabled",
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
    include_trace: bool = False,
    preview_bar: dict | None = None,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    normalized_preview_bar = _normalize_preview_bar(preview_bar, normalized_symbol, normalized_interval)
    effective_end_ms = int(end_ms or 0)
    if normalized_preview_bar:
        effective_end_ms = max(effective_end_ms, int(normalized_preview_bar.get("bar_time_ms", 0) or 0))

    api_app.refresh_daily_close_cache([runtime_environment])
    source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=effective_end_ms,
    )
    chart_freshness = {}
    repair_meta = {"queued": False, "status": "not_required"}
    try:
        planner = getattr(api_app, "bar_freshness_planner", None) or BarFreshnessPlanner(
            getattr(api_app, "pb", None),
            getattr(api_app, "cfg", None),
            environment=runtime_environment,
        )
        chart_freshness = planner.plan_symbol(
            normalized_symbol,
            [normalized_interval],
            environment=runtime_environment,
            required_bars=0,
        )
        if bool(chart_freshness.get("needs_repair")):
            coordinator = getattr(api_app, "bar_repair_coordinator", None)
            if coordinator is not None and hasattr(coordinator, "enqueue_from_freshness"):
                jobs = coordinator.enqueue_from_freshness(
                    chart_freshness,
                    priority="chart_active",
                    trigger="chart_timeline",
                )
                repair_meta = {
                    "queued": any(bool((job or {}).get("queued")) for job in jobs),
                    "status": "queued" if jobs else "repairing",
                    "jobs": jobs,
                    "trigger": "chart_timeline",
                }
            else:
                repair_meta = {"queued": False, "status": "planner_only", "trigger": "chart_timeline"}
    except Exception as exc:
        chart_freshness = {"status": "unknown", "error": str(exc), "symbol": normalized_symbol, "environment": runtime_environment}
        repair_meta = {"queued": False, "status": "error", "error": str(exc)}
    if normalized_preview_bar:
        source_rows_by_ms = {
            int((row or {}).get("bar_time_ms", 0) or 0): dict(row)
            for row in (source.get("source_rows") or [])
            if int((row or {}).get("bar_time_ms", 0) or 0) > 0
        }
        source_rows_by_ms[int(normalized_preview_bar.get("bar_time_ms", 0) or 0)] = normalized_preview_bar
        source = build_chart_source_window_from_rows(
            source_rows_by_ms.values(),
            normalized_interval,
            start_ms=start_ms,
            end_ms=effective_end_ms,
        )
        source["meta"] = {
            "preview_bar": True,
            "preview_bar_time_ms": int(normalized_preview_bar.get("bar_time_ms", 0) or 0),
        }
    source["meta"] = {
        **(source.get("meta") or {}),
        "freshness": chart_freshness,
        "repair": repair_meta,
        "progress": {
            "status": "completed",
            "phase": "timeline_ready",
            "percent": 100,
            "current_step": "bars_loaded_indicators_recomputed",
        },
    }
    return build_chart_timeline_payload_from_source(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        source,
        start_ms=start_ms,
        end_ms=effective_end_ms,
        include_signals=include_signals,
        include_trace=include_trace,
    )


__all__ = [
    "build_chart_timeline_payload",
    "build_chart_timeline_payload_from_source",
]
