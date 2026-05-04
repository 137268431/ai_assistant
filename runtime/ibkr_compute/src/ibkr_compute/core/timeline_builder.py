from __future__ import annotations

from typing import Any

from ibkr_compute.market.timeframe_utils import normalize_interval

from .indicator_engine import IndicatorEngine
from .signal_generator import SignalGenerator


def _normalize_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        **(bar or {}),
        "open": float((bar or {}).get("open", 0) or 0),
        "high": float((bar or {}).get("high", 0) or 0),
        "low": float((bar or {}).get("low", 0) or 0),
        "close": float((bar or {}).get("close", 0) or 0),
        "volume": float((bar or {}).get("volume", 0) or 0),
        "bar_time_ms": int((bar or {}).get("bar_time_ms", 0) or 0),
        "us_time": str((bar or {}).get("us_time", "") or ""),
        "cn_time": str((bar or {}).get("cn_time", "") or ""),
        "session_type": str((bar or {}).get("session_type", "regular") or "regular"),
        "exchange": str((bar or {}).get("exchange", "") or "").upper(),
        "preview": bool((bar or {}).get("preview") or (bar or {}).get("is_preview")),
        "is_preview": bool((bar or {}).get("preview") or (bar or {}).get("is_preview")),
    }


def build_runtime_timeline(
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    *,
    params: dict[str, Any] | None = None,
    include_signals: bool = False,
    include_trace: bool = False,
    visible_start_ms: int = 0,
    visible_end_ms: int = 0,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    engine = IndicatorEngine(normalized_symbol, normalized_interval, params=params)
    signal_gen = (
        SignalGenerator(normalized_symbol, normalized_interval, params=params)
        if include_signals and normalized_interval == "5m"
        else None
    )

    bars_by_ms: dict[int, dict[str, Any]] = {}
    for raw_bar in bars or []:
        bar_ms = int((raw_bar or {}).get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        bars_by_ms[bar_ms] = _normalize_bar(raw_bar)
    sorted_bars = [bars_by_ms[bar_ms] for bar_ms in sorted(bars_by_ms)]

    rows = []
    previous_day = ""
    last_bar_ms = 0
    processed = 0

    for bar in sorted_bars:
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        if bar_ms <= 0 or bar_ms == last_bar_ms:
            continue
        last_bar_ms = bar_ms
        processed += 1

        current_day = str(bar.get("us_time", "") or "")[:10]
        if signal_gen and previous_day and current_day and current_day != previous_day:
            signal_gen.daily_reset()
        if current_day:
            previous_day = current_day

        snapshot = engine.update(bar) or {}
        signal = None
        if signal_gen and snapshot and engine.is_ready():
            signal = signal_gen.update(snapshot)

        in_visible_range = True
        if visible_start_ms > 0 and bar_ms < int(visible_start_ms):
            in_visible_range = False
        if visible_end_ms > 0 and bar_ms > int(visible_end_ms):
            in_visible_range = False
        if not in_visible_range:
            continue

        row = {
            "symbol": normalized_symbol,
            "interval": normalized_interval,
            "exchange": bar.get("exchange", ""),
            "bar_time_ms": bar_ms,
            "bar_index": int(snapshot.get("bar_count", engine.bar_count) or engine.bar_count or 0),
            "us_time": bar.get("us_time", ""),
            "cn_time": bar.get("cn_time", ""),
            "session_type": bar.get("session_type", "regular"),
            "preview": bool(bar.get("preview") or bar.get("is_preview")),
            "is_preview": bool(bar.get("preview") or bar.get("is_preview")),
            "open": round(float(bar.get("open", 0) or 0), 4),
            "high": round(float(bar.get("high", 0) or 0), 4),
            "low": round(float(bar.get("low", 0) or 0), 4),
            "close": round(float(bar.get("close", 0) or 0), 4),
            "volume": round(float(bar.get("volume", 0) or 0), 4),
            "indicator_ready": bool(engine.is_ready()),
            **snapshot,
        }
        trace = None
        if signal_gen and include_trace:
            trace = signal_gen.get_trace_snapshot()
            if row["preview"] and trace.get("signal_state", {}).get("stage") == "confirmed":
                trace_signal_state = dict(trace.get("signal_state") or {})
                trace_signal_state["stage"] = "candidate"
                trace_signal_state["label"] = signal_gen._build_signal_state_label(  # type: ignore[attr-defined]
                    trace_signal_state.get("signal_payload"),
                    "candidate",
                )
                trace = {
                    **trace,
                    "signal_state": trace_signal_state,
                }
        if signal and not row["preview"]:
            row["signal"] = signal
        elif signal and row["preview"]:
            row["signal_preview"] = signal
        if include_trace:
            row["trace"] = trace or {
                "signal_state": {
                    "stage": "none",
                    "direction": "",
                    "signal": "",
                    "label": "无信号",
                    "reason": "",
                    "filter_reason": "",
                    "signal_window": "",
                    "signal_mode": "",
                    "ema_touch_line": "",
                    "div_source": "",
                    "signal_payload": row.get("signal_preview"),
                },
                "events": [],
                "filters": [],
                "window_flags": {},
                "component_flags": {},
            }
        rows.append(row)

    return {
        "rows": rows,
        "latest_row": rows[-1] if rows else None,
        "processed_bars": processed,
        "visible_rows": len(rows),
        "warmup_rows": max(0, processed - len(rows)),
    }
