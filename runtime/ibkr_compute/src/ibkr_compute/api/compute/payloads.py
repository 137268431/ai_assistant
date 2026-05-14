from __future__ import annotations

from ibkr_compute.market.timeframe_utils import (
    build_bar_close_timestamps,
    build_runtime_timestamps,
    build_signal_id,
    interval_to_chart_tf,
)

from ibkr_compute.api.compute.runtime_state.caches import (
    get_daily_change_fields,
    refresh_symbol_metadata,
)
from ibkr_compute.api.compute.market_sentiment import build_market_sentiment_extra
from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.core.setup_registry import build_setup_metadata


def build_indicator_payload(environment: str, symbol: str, interval: str, bar: dict, engine, snapshot: dict):
    api_app = _api_app()
    chart_tf = interval_to_chart_tf(interval)
    bar_ms = int(bar.get("bar_time_ms", 0) or 0)
    daily_fields = get_daily_change_fields(environment, symbol, float(snapshot.get("close", 0) or 0), bar_ms)
    extra = {
        **snapshot,
        **daily_fields,
        "symbol": symbol,
        "interval": chart_tf,
        "chart_tf": chart_tf,
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "script_tag": api_app.IBKR_SCRIPT_TAG,
        "environment": environment,
        "session_type": bar.get("session_type", "regular"),
        "source": "ibkr_compute",
        **build_bar_close_timestamps(bar_ms, interval),
        **build_runtime_timestamps(),
    }

    return {
        "environment": environment,
        "symbol": symbol,
        "exchange": str(bar.get("exchange", "") or "").upper(),
        "interval": chart_tf,
        "script_tag": api_app.IBKR_SCRIPT_TAG,
        "us_time": bar.get("us_time", ""),
        "cn_time": bar.get("cn_time", ""),
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "extra": extra,
    }


def build_signal_payload(environment: str, symbol: str, interval: str, bar: dict, engine, signal: dict):
    api_app = _api_app()
    chart_tf = interval_to_chart_tf(interval)
    bar_ms = int(bar.get("bar_time_ms", 0) or 0)
    reference_price = round(float(bar.get("close", 0) or 0), 2)
    entry_price = round(float(signal.get("entry", 0) or 0), 2)
    symbol_meta = refresh_symbol_metadata().get(symbol, {})
    signal_type = str(signal.get("signal", "") or "")
    initial_status, initial_status_reason = api_app.resolve_initial_signal_state(environment, bar_ms)
    signal_extra = dict(signal.get("extra") or {})
    setup_meta = build_setup_metadata(
        signal_extra.get("setup") or signal.get("setup") or signal_type,
        fallback_signal=signal_type,
        direction=signal.get("direction", ""),
        signal_mode=signal_extra.get("signal_mode") or signal.get("signal_mode", ""),
        strategy_profile=signal_extra.get("strategy_profile", ""),
        setup_priority=signal_extra.get("setup_priority"),
        exit_policy_type=signal_extra.get("exit_policy_type", ""),
    )
    signal_extra.update(setup_meta)
    market_sentiment_extra = build_market_sentiment_extra(
        api_app=api_app,
        environment=environment,
        signal_direction=signal.get("direction", ""),
        signal_bar_time_ms=bar_ms,
        interval=interval,
    )
    signal_extra.update({
        "industry": symbol_meta.get("industry", ""),
        **market_sentiment_extra,
        **get_daily_change_fields(environment, symbol, float(bar.get("close", 0) or 0), bar_ms),
        "script_tag": api_app.IBKR_SCRIPT_TAG,
        "chart_tf": chart_tf,
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "close": reference_price,
        "reference_price": reference_price,
        "reference_source": "bar_close",
        "limit_price": entry_price,
        "atr": signal_extra.get("atr_raw", signal_extra.get("atr", 0)),
        "environment": environment,
        "source": "ibkr_compute",
        "signal_source": "ibkr_compute_realtime",
        "signal_source_label": "IBKR 实时计算",
        "signal_source_detail": "来自 IBKR 实盘 bars 收盘计算",
        "source_kind": "computed",
        "computed_from": "ibkr_bars",
        "status_reason": initial_status_reason,
        "initial_status": initial_status,
        "initial_status_reason": initial_status_reason,
        **build_runtime_timestamps(),
    })

    rr_value = signal.get("rr", "")
    rr_text = f"{float(rr_value):.1f}:1" if isinstance(rr_value, (int, float)) else str(rr_value or "")

    return {
        "environment": environment,
        "symbol": symbol,
        "signal_id": build_signal_id(symbol, bar_ms, signal_type),
        "direction": signal.get("direction", ""),
        "signal": signal_type,
        "limit_price": entry_price,
        "entry": entry_price,
        "stop_loss": signal.get("stop_loss", 0),
        "take_profit": signal.get("take_profit", 0),
        "rr": rr_text,
        "shares": signal.get("shares", 0),
        "risk_r": signal.get("risk_r", signal_extra.get("risk_r", 0)),
        "exit_policy": signal.get("exit_policy", signal_extra.get("exit_policy", "")),
        "exchange": str(bar.get("exchange", "") or symbol_meta.get("exchange", "")).upper(),
        "interval": chart_tf,
        "reason": signal.get("reason", ""),
        "us_time": bar.get("us_time", ""),
        "cn_time": bar.get("cn_time", ""),
        "date": (bar.get("us_time", "") or "")[:10],
        "bar_time_ms": bar_ms,
        "bar_index": engine.bar_count,
        "script_tag": api_app.IBKR_SCRIPT_TAG,
        "chart_tf": chart_tf,
        "status": initial_status,
        "note": initial_status_reason,
        "extra": signal_extra,
    }
