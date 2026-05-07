from __future__ import annotations

from .runtime_support import *


class BacktestSymbolRowsRowBuildersMixin:
    def _should_compare_with_tv(self, request: dict) -> bool:
        return bool(request.get("compare_with_tv", True)) and str(request.get("source_environment") or "").strip().lower() != BACKTEST_ENVIRONMENT

    def _should_compare_tv_signals(self, request: dict) -> bool:
        return self._should_compare_with_tv(request) and bool(request.get("compare_tv_signals", False))

    def _should_persist_backtest_indicators(self, request: dict) -> bool:
        return bool(request.get("persist_backtest_indicators", False))

    def _clear_backtest_row_buffers(self, *buffers: Any, collect: bool = True):
        for buffer in buffers:
            if hasattr(buffer, "clear"):
                try:
                    buffer.clear()
                except Exception:
                    continue
        if collect:
            gc.collect()

    def _clear_portfolio_working_sets(
        self,
        states: dict[str, dict],
        bars_by_time: dict[int, list[tuple[str, int, dict]]],
        signal_index: dict,
        target_lookup: dict,
    ):
        for state in list(states.values()):
            self._clear_backtest_row_buffers(
                state.get("bars"),
                state.get("daily_close_lookup"),
                state.get("reverse_index"),
                collect=False,
            )
            state.pop("previous_bar", None)
            state.pop("last_bar", None)
            state.pop("open_position", None)
            state.pop("pending_signal", None)
        self._clear_backtest_row_buffers(states, bars_by_time, signal_index, target_lookup)

    def _parse_object(self, value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    def _load_daily_close_lookup(self, symbol: str, source_environment: str, date_from: str, date_to: str) -> list[dict]:
        if not self.pb:
            return []
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        lookback_start_ms = max(0, start_ms - (interval_to_ms("1d") * 40))
        rows = self.pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{symbol}" && interval = "1d" && environment = "{source_environment}" '
                f"&& bar_time_ms >= {lookback_start_ms} && bar_time_ms <= {end_ms}"
            ),
            sort="bar_time_ms",
            max_pages=120,
        )
        lookup = []
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            close = float(row.get("close", 0) or 0)
            if bar_ms <= 0 or close <= 0:
                continue
            lookup.append(
                {
                    "bar_time_ms": bar_ms,
                    "date": ms_to_et(bar_ms).strftime("%Y-%m-%d"),
                    "close": close,
                }
            )
        return lookup

    def _get_daily_change_fields_from_lookup(self, lookup: list[dict], current_close: float, bar_time_ms: int) -> dict:
        current_date = ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
        history = [row for row in lookup if str(row.get("date") or "") < current_date]
        prev_close = float(history[-1]["close"]) if len(history) >= 1 else 0.0
        prev_prev_close = float(history[-2]["close"]) if len(history) >= 2 else 0.0
        close_5 = float(history[-5]["close"]) if len(history) >= 5 else 0.0

        day_change_pct = ((current_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
        prev_close_change_pct = ((prev_close - prev_prev_close) / prev_prev_close * 100.0) if prev_prev_close > 0 else 0.0
        change_7d = ((current_close - close_5) / close_5 * 100.0) if close_5 > 0 else 0.0

        return {
            "day_change_pct": round(day_change_pct, 2),
            "prev_close_change_pct": round(prev_close_change_pct, 2),
            "change_7d": round(change_7d, 2),
        }

    def _build_tv_indicator_compare_payload(
        self,
        symbol: str,
        bar: dict,
        bar_index: int,
        snapshot: dict,
        source_environment: str,
        daily_fields: dict,
    ) -> dict:
        chart_tf = interval_to_chart_tf("5m")
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        extra = {
            **snapshot,
            **(daily_fields or {}),
            "symbol": symbol,
            "interval": chart_tf,
            "chart_tf": chart_tf,
            "bar_time_ms": bar_ms,
            "bar_index": int(bar_index or 0),
            "environment": source_environment,
            "session_type": bar.get("session_type", "regular"),
            "source": "ibkr_compute_backtest",
        }
        return {
            "symbol": symbol,
            "interval": chart_tf,
            "bar_time_ms": bar_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "extra": extra,
        }

    def _build_tv_signal_compare_payload(
        self,
        symbol: str,
        bar: dict,
        bar_index: int,
        signal: dict,
        source_environment: str,
        daily_fields: dict,
    ) -> dict:
        chart_tf = interval_to_chart_tf("5m")
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        signal_extra = dict(signal.get("extra") or {})
        signal_extra.update(
            {
                **(daily_fields or {}),
                "chart_tf": chart_tf,
                "bar_time_ms": bar_ms,
                "bar_index": int(bar_index or 0),
                "close": round(float(bar.get("close", 0) or 0), 2),
                "environment": source_environment,
                "source": "ibkr_compute_backtest",
            }
        )
        return {
            "symbol": symbol,
            "signal_id": build_signal_id(symbol, bar_ms, str(signal.get("signal", "") or "")),
            "signal": str(signal.get("signal", "") or ""),
            "direction": str(signal.get("direction", "") or ""),
            "entry": float(signal.get("entry", 0) or 0),
            "stop_loss": float(signal.get("stop_loss", 0) or 0),
            "take_profit": float(signal.get("take_profit", 0) or 0),
            "rr": signal.get("rr"),
            "shares": int(signal.get("shares", 0) or 0),
            "interval": chart_tf,
            "bar_time_ms": bar_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "reason": str(signal.get("reason", "") or ""),
            "extra": signal_extra,
        }

    def _build_backtest_indicator_row(self, request: dict, bar: dict, indicator_payload: dict, indicator_audit: dict | None = None) -> dict:
        indicator_extra = dict(indicator_payload.get("extra") or {})
        audit = indicator_audit or {}
        indicator_extra.update(
            {
                "backtest_source_environment": request.get("source_environment") or "",
                "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
                "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
                "tv_parity_status": audit.get("status") or "unverified",
                "tv_parity_field_count": int(audit.get("field_count", 0) or 0),
                "script_tag": str(request.get("strategy_tag") or ""),
                **build_runtime_timestamps(),
            }
        )
        if audit.get("mismatches"):
            indicator_extra["tv_parity_fields"] = audit.get("mismatches")

        return {
            "symbol": indicator_payload.get("symbol", ""),
            "exchange": str(bar.get("exchange", "") or "").upper(),
            "interval": indicator_payload.get("interval", interval_to_chart_tf("5m")),
            "script_tag": str(request.get("strategy_tag") or ""),
            "us_time": indicator_payload.get("us_time", ""),
            "cn_time": indicator_payload.get("cn_time", ""),
            "bar_time_ms": int(indicator_payload.get("bar_time_ms", 0) or 0),
            "bar_index": int(indicator_extra.get("bar_index", 0) or 0),
            "environment": BACKTEST_ENVIRONMENT,
            "extra": indicator_extra,
        }

    def _build_backtest_signal_row(self, request: dict, bar: dict, signal_payload: dict) -> dict:
        signal_extra = dict(signal_payload.get("extra") or {})
        signal_extra.update(
            {
                "backtest_source_environment": request.get("source_environment") or "",
                "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
                "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
                "script_tag": str(request.get("strategy_tag") or ""),
                **build_runtime_timestamps(),
            }
        )
        return {
            "symbol": signal_payload.get("symbol", ""),
            "direction": signal_payload.get("direction", ""),
            "signal": signal_payload.get("signal", ""),
            "entry": float(signal_payload.get("entry", 0) or 0),
            "stop_loss": float(signal_payload.get("stop_loss", 0) or 0),
            "take_profit": float(signal_payload.get("take_profit", 0) or 0),
            "rr": "" if signal_payload.get("rr") in (None, "") else str(signal_payload.get("rr")),
            "shares": int(signal_payload.get("shares", 0) or 0),
            "signal_id": signal_payload.get("signal_id", ""),
            "exchange": str(bar.get("exchange", "") or "").upper(),
            "interval": signal_payload.get("interval", interval_to_chart_tf("5m")),
            "reason": signal_payload.get("reason", ""),
            "us_time": signal_payload.get("us_time", ""),
            "cn_time": signal_payload.get("cn_time", ""),
            "date": str(signal_payload.get("us_time", "") or "")[:10],
            "bar_time_ms": int(signal_payload.get("bar_time_ms", 0) or 0),
            "bar_index": int(signal_extra.get("bar_index", 0) or 0),
            "script_tag": str(request.get("strategy_tag") or ""),
            "status": "generated",
            "environment": BACKTEST_ENVIRONMENT,
            "extra": signal_extra,
        }

    def _mark_backtest_signal_status(
        self,
        signal_index: dict,
        signal_id: Any,
        status: str,
        reason: str,
        extra_patch: dict | None = None,
    ):
        safe_signal_id = str(signal_id or "").strip()
        if not safe_signal_id:
            return
        row = signal_index.get(safe_signal_id)
        if not row:
            return
        row["status"] = status
        extra = self._parse_object(row.get("extra"))
        extra["signal_status_reason"] = reason
        if extra_patch:
            extra.update(extra_patch)
        row["extra"] = extra
