from __future__ import annotations

from ibkr_compute.market.timeframe_utils import normalize_interval

from ibkr_compute.api.chart.timeline.runtime import _api_app


def load_chart_timeline_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    visible_pages = max(1, (api_app.CHART_TIMELINE_VISIBLE_LIMIT + 199) // 200 + 1)
    warmup_pages = max(1, (warmup_bars + 199) // 200 + 1)

    base_filter_parts = [
        f'symbol = "{normalized_symbol}"',
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
    ]
    if end_ms > 0:
        base_filter_parts.append(f"bar_time_ms <= {int(end_ms)}")

    visible_filter_parts = list(base_filter_parts)
    if start_ms > 0:
        visible_filter_parts.append(f"bar_time_ms >= {int(start_ms)}")

    visible_rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(visible_filter_parts),
        sort="bar_time_ms",
        max_pages=visible_pages,
    )
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_rows = []
    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_filter_parts = list(base_filter_parts)
        warmup_filter_parts.append(f"bar_time_ms < {warmup_anchor_ms}")
        warmup_rows = api_app.pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(warmup_filter_parts),
            sort="-bar_time_ms",
            max_pages=warmup_pages,
        )
        warmup_rows = list(reversed(warmup_rows[:warmup_bars]))

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


def build_chart_source_window_from_rows(
    rows,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    deduped = {}
    for row in rows or []:
        bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        if end_ms > 0 and bar_ms > int(end_ms):
            continue
        deduped[bar_ms] = dict(row)

    ordered_rows = [deduped[bar_ms] for bar_ms in sorted(deduped)]
    visible_rows = ordered_rows
    if start_ms > 0:
        visible_rows = [
            row for row in visible_rows
            if int(row.get("bar_time_ms", 0) or 0) >= int(start_ms)
        ]
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    warmup_rows = []
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_rows = [
            row for row in ordered_rows
            if int(row.get("bar_time_ms", 0) or 0) < warmup_anchor_ms
        ][-warmup_bars:]

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


__all__ = [
    "build_chart_source_window_from_rows",
    "load_chart_timeline_source_bars",
]
