"""Compatibility exports for chart API helpers."""

from ibkr_compute.api.chart.compare.diff import (
    CHART_COMPARE_BAR_FIELDS,
    CHART_COMPARE_INDICATOR_FIELDS,
    CHART_COMPARE_SIGNAL_FIELDS,
    _build_chart_compare_group,
    _build_chart_compare_summary,
    _normalize_chart_compare_number,
    _normalize_chart_compare_values,
)
from ibkr_compute.api.chart.compare.payload import build_chart_compare_payload
from ibkr_compute.api.chart.compare.request import get_chart_compare_request_period
from ibkr_compute.api.chart.compare.source import load_chart_compare_ibkr_source_bars
from ibkr_compute.api.chart.timeline.payload import (
    build_chart_timeline_payload,
    build_chart_timeline_payload_from_source,
)
from ibkr_compute.api.chart.timeline.rows import (
    build_chart_indicator_row,
    build_chart_signal_row,
)
from ibkr_compute.api.chart.timeline.source import (
    build_chart_source_window_from_rows,
    load_chart_timeline_source_bars,
)


__all__ = [
    "CHART_COMPARE_BAR_FIELDS",
    "CHART_COMPARE_INDICATOR_FIELDS",
    "CHART_COMPARE_SIGNAL_FIELDS",
    "_build_chart_compare_group",
    "_build_chart_compare_summary",
    "_normalize_chart_compare_number",
    "_normalize_chart_compare_values",
    build_chart_compare_payload,
    get_chart_compare_request_period,
    load_chart_compare_ibkr_source_bars,
    build_chart_indicator_row,
    build_chart_signal_row,
    build_chart_source_window_from_rows,
    build_chart_timeline_payload,
    build_chart_timeline_payload_from_source,
    load_chart_timeline_source_bars,
]
