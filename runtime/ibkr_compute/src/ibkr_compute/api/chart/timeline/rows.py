from __future__ import annotations

from ibkr_compute.market.timeframe_utils import (
    build_runtime_timestamps,
    build_signal_id,
    interval_to_chart_tf,
    normalize_interval,
)

from ibkr_compute.api.chart.timeline.runtime import _api_app


def build_chart_indicator_row(environment: str, symbol: str, interval: str, row: dict) -> dict:
    api_app = _api_app()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    daily_fields = api_app.get_daily_change_fields(
        environment,
        normalized_symbol,
        float(row.get("close", 0) or 0),
        bar_ms,
    )
    timestamps = build_runtime_timestamps()
    return {
        **{key: value for key, value in (row or {}).items() if key != "signal"},
        **daily_fields,
        **timestamps,
        "environment": environment,
        "symbol": normalized_symbol,
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
    }


def build_chart_signal_row(environment: str, symbol: str, interval: str, row: dict) -> dict | None:
    api_app = _api_app()
    raw_signal = row.get("signal")
    if not raw_signal:
        return None

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    bar_index = int(row.get("bar_index", row.get("bar_count", 0)) or 0)
    daily_fields = api_app.get_daily_change_fields(
        environment,
        normalized_symbol,
        float(row.get("close", 0) or 0),
        bar_ms,
    )
    signal_type = str(raw_signal.get("signal", "") or "")
    signal_extra = dict(raw_signal.get("extra") or {})
    signal_extra.update(
        {
            **daily_fields,
            **build_runtime_timestamps(),
            "chart_tf": chart_tf,
            "bar_time_ms": bar_ms,
            "bar_index": bar_index,
            "close": round(float(row.get("close", 0) or 0), 2),
            "atr": signal_extra.get("atr_raw", signal_extra.get("atr", row.get("atr", 0))),
            "atr_pct": row.get("atr_pct", signal_extra.get("atr_pct", 0)),
            "environment": environment,
            "source": "ibkr_compute_timeline",
            "signal_source": "ibkr_compute_timeline",
            "signal_source_label": "IBKR 图表回放",
            "signal_source_detail": "来自缓存 bars 时间线重算",
            "computed_from": "ibkr_bars",
        }
    )

    rr_value = raw_signal.get("rr", "")
    rr_text = f"{float(rr_value):.1f}:1" if isinstance(rr_value, (int, float)) else str(rr_value or "")

    return {
        "environment": environment,
        "symbol": normalized_symbol,
        "signal_id": build_signal_id(normalized_symbol, bar_ms, signal_type),
        "direction": raw_signal.get("direction", ""),
        "signal": signal_type,
        "limit_price": round(float(row.get("close", 0) or 0), 2),
        "entry": raw_signal.get("entry", 0),
        "stop_loss": raw_signal.get("stop_loss", 0),
        "take_profit": raw_signal.get("take_profit", 0),
        "rr": rr_text,
        "shares": raw_signal.get("shares", 0),
        "exchange": str(row.get("exchange", "") or "").upper(),
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "reason": raw_signal.get("reason", ""),
        "us_time": row.get("us_time", ""),
        "cn_time": row.get("cn_time", ""),
        "date": str(row.get("us_time", "") or "")[:10],
        "bar_time_ms": bar_ms,
        "bar_index": bar_index,
        "status": "computed",
        "source": "ibkr_compute_timeline",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
        "extra": signal_extra,
    }


__all__ = [
    "build_chart_indicator_row",
    "build_chart_signal_row",
]
