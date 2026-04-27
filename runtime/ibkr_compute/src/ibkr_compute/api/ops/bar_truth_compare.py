from __future__ import annotations

from ibkr_compute.api.chart.compare.diff import (
    CHART_COMPARE_BAR_FIELDS,
    _build_chart_compare_group,
)
from ibkr_compute.api.chart.compare.source import load_chart_compare_ibkr_source_bars
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import load_chart_timeline_source_bars
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval


def _bar_map(rows: list[dict] | None) -> dict[int, dict]:
    output: dict[int, dict] = {}
    for row in rows or []:
        bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        output[bar_ms] = dict(row)
    return output


def _build_bar_truth_summary(stored_rows: list[dict] | None, ibkr_rows: list[dict] | None) -> dict:
    api_app = _api_app()
    stored_map = _bar_map(stored_rows)
    ibkr_map = _bar_map(ibkr_rows)
    timeline = []
    mismatch_examples = []
    summary = {
        "stored_visible_bars": len(stored_rows or []),
        "ibkr_visible_bars": len(ibkr_rows or []),
        "matched_bar_count": 0,
        "missing_stored_bar_count": 0,
        "missing_ibkr_bar_count": 0,
        "bar_mismatch_count": 0,
        "indicator_mismatch_count": 0,
        "signal_mismatch_count": 0,
    }

    for bar_time_ms in sorted(set(stored_map.keys()) | set(ibkr_map.keys())):
        stored_bar = stored_map.get(bar_time_ms)
        ibkr_bar = ibkr_map.get(bar_time_ms)
        bar_diff = _build_chart_compare_group(
            stored_bar,
            ibkr_bar,
            CHART_COMPARE_BAR_FIELDS,
            text_fields=("session_type",),
        )

        if bar_diff["status"] == "match":
            summary["matched_bar_count"] += 1
        elif bar_diff["status"] == "missing_stored":
            summary["missing_stored_bar_count"] += 1
        elif bar_diff["status"] == "missing_ibkr":
            summary["missing_ibkr_bar_count"] += 1
        elif bar_diff["status"] == "mismatch":
            summary["bar_mismatch_count"] += 1

        compare_row = {
            "bar_time_ms": bar_time_ms,
            "us_time": str((stored_bar or {}).get("us_time") or "") or str((ibkr_bar or {}).get("us_time") or ""),
            "status": {"bar": bar_diff["status"]},
            "stored": {"bar": stored_bar},
            "ibkr": {"bar": ibkr_bar},
            "diff": {"bar": bar_diff},
        }
        timeline.append(compare_row)

        severity = 0
        if bar_diff["status"] in {"missing_stored", "missing_ibkr"}:
            severity = 40
        elif bar_diff["status"] == "mismatch":
            severity = 30
        if severity > 0:
            mismatch_examples.append(
                {
                    "bar_time_ms": bar_time_ms,
                    "us_time": compare_row["us_time"],
                    "status": compare_row["status"],
                    "bar_fields": bar_diff["fields"],
                    "severity": severity,
                }
            )

    mismatch_examples.sort(
        key=lambda item: (
            -int(item.get("severity", 0) or 0),
            -int(item.get("bar_time_ms", 0) or 0),
        )
    )
    return {
        "summary": summary,
        "timeline": timeline,
        "mismatch_examples": mismatch_examples[:api_app.CHART_COMPARE_MAX_MISMATCH_EXAMPLES],
    }


def build_bar_truth_compare_payload(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)

    stored_source = load_chart_timeline_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    ibkr_source = load_chart_compare_ibkr_source_bars(
        runtime_environment,
        normalized_symbol,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    comparison = _build_bar_truth_summary(
        stored_source.get("visible_rows") or [],
        ibkr_source.get("visible_rows") or [],
    )
    return {
        "ok": True,
        "comparison": comparison,
        "meta": {
            "environment": runtime_environment,
            "symbol": normalized_symbol,
            "interval": interval_to_chart_tf(normalized_interval),
            "start_ms": int(start_ms or 0),
            "end_ms": int(end_ms or 0),
            "audit_mode": "bar_only",
            "stored": {
                **(stored_source.get("meta") or {}),
                "chain": "stored_bars",
                "visible_bar_count": len(stored_source.get("visible_rows") or []),
                "source_bar_count": len(stored_source.get("source_rows") or []),
                "warmup_used": int(stored_source.get("warmup_used", 0) or 0),
            },
            "ibkr": {
                **(ibkr_source.get("meta") or {}),
                "visible_bar_count": len(ibkr_source.get("visible_rows") or []),
                "source_bar_count": len(ibkr_source.get("source_rows") or []),
                "warmup_used": int(ibkr_source.get("warmup_used", 0) or 0),
            },
        },
    }


__all__ = [
    "build_bar_truth_compare_payload",
]
