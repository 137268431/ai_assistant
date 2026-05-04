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


def build_chart_trace_row(environment: str, symbol: str, interval: str, row: dict) -> dict:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    chart_tf = interval_to_chart_tf(normalized_interval)
    bar_ms = int(row.get("bar_time_ms", 0) or 0)
    trace = dict(row.get("trace") or {})
    signal_state = dict(trace.get("signal_state") or {})
    signal_payload = dict(signal_state.get("signal_payload") or row.get("signal_preview") or row.get("signal") or {})
    signal_extra = dict(signal_payload.get("extra") or {})
    divergence_tokens = []
    if row.get("crsi_bull_div") or row.get("crsi_hid_bull"):
        divergence_tokens.append("cRSI 多")
    if row.get("crsi_bear_div") or row.get("crsi_hid_bear"):
        divergence_tokens.append("cRSI 空")
    if row.get("obv_bull_div") or row.get("obv_hid_bull"):
        divergence_tokens.append("OBV 多")
    if row.get("obv_bear_div") or row.get("obv_hid_bear"):
        divergence_tokens.append("OBV 空")

    touch_tokens = []
    if row.get("bull_touch_fast"):
        touch_tokens.append("多触快")
    if row.get("bull_touch_slow"):
        touch_tokens.append("多触慢")
    if row.get("bear_touch_fast"):
        touch_tokens.append("空触快")
    if row.get("bear_touch_slow"):
        touch_tokens.append("空触慢")

    fractal_tokens = []
    if row.get("fractal_bull"):
        fractal_tokens.append("分形多")
    if row.get("fractal_bear"):
        fractal_tokens.append("分形空")

    signal_id = ""
    signal_name = str(signal_payload.get("signal", "") or "")
    if signal_name:
        signal_id = build_signal_id(normalized_symbol, bar_ms, signal_name)

    return {
        "environment": environment,
        "symbol": normalized_symbol,
        "interval": chart_tf,
        "bar_time_ms": bar_ms,
        "bar_index": int(row.get("bar_index", row.get("bar_count", 0)) or 0),
        "us_time": row.get("us_time", ""),
        "cn_time": row.get("cn_time", ""),
        "session_type": row.get("session_type", "regular"),
        "is_preview": bool(row.get("preview") or row.get("is_preview")),
        "close": round(float(row.get("close", 0) or 0), 4),
        "structure": {
            "trend_dir": int(row.get("trend_dir", 0) or 0),
            "ema_bullish": bool(row.get("ema_bullish")),
            "ema_bearish": bool(row.get("ema_bearish")),
            "dtp_dir": int(row.get("dtp_dir", 0) or 0),
            "dtp_phase": row.get("dtp_phase", ""),
            "dtp_phase_bars": int(row.get("dtp_phase_bars", 0) or 0),
            "fractal_tokens": fractal_tokens,
            "touch_tokens": touch_tokens,
        },
        "position": {
            "vwap_dist": row.get("vwap_dist", 0),
            "sd_zone": int(row.get("sd_zone", 0) or 0),
            "sd_trend": int(row.get("sd_trend", 0) or 0),
        },
        "volatility": {
            "atr": row.get("atr", 0),
            "atr_pct": row.get("atr_pct", 0),
        },
        "momentum": {
            "crsi": row.get("crsi", 0),
            "obv_rsi": row.get("obv_rsi", 0),
            "divergence_tokens": divergence_tokens,
        },
        "event_chain": list(trace.get("events") or []),
        "filters": list(trace.get("filters") or []),
        "signal_state": {
            "stage": str(signal_state.get("stage", "none") or "none"),
            "direction": str(signal_state.get("direction", "") or ""),
            "signal": str(signal_state.get("signal", "") or ""),
            "label": str(signal_state.get("label", "") or "无信号"),
            "reason": str(signal_state.get("reason", "") or ""),
            "filter_reason": str(signal_state.get("filter_reason", "") or ""),
            "signal_window": str(signal_state.get("signal_window", "") or ""),
            "signal_mode": str(signal_state.get("signal_mode", "") or ""),
            "ema_touch_line": str(signal_state.get("ema_touch_line", "") or ""),
            "div_source": str(signal_state.get("div_source", "") or ""),
            "signal_payload": signal_payload,
        },
        "window_flags": dict(trace.get("window_flags") or {}),
        "component_flags": dict(trace.get("component_flags") or {}),
        "stored_refs": {
            "indicator_bar_time_ms": bar_ms,
            "computed_signal_id": signal_id,
            "signal_source": "candidate" if row.get("signal_preview") else ("computed" if row.get("signal") else ""),
            "signal_mode": str(signal_extra.get("signal_mode", "") or ""),
        },
    }


__all__ = [
    "build_chart_indicator_row",
    "build_chart_signal_row",
    "build_chart_trace_row",
]
