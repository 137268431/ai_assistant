from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .models import OrderFlowBar, OrderFlowTick, normalize_symbol


DEFAULT_ORDER_FLOW_INTERVALS = (10, 30, 60)


@dataclass
class _WorkingOrderFlowBar:
    symbol: str
    interval_seconds: int
    start_ms: int
    end_ms: int
    cvd_open: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    tick_count: int = 0
    cvd_high: float = 0.0
    cvd_low: float = 0.0
    cvd_close: float = 0.0
    first_sequence: int = 0
    last_sequence: int = 0

    def __post_init__(self) -> None:
        self.cvd_high = float(self.cvd_open)
        self.cvd_low = float(self.cvd_open)
        self.cvd_close = float(self.cvd_open)

    def update(self, tick: OrderFlowTick, cvd_after_tick: float) -> None:
        if self.tick_count == 0:
            self.open = tick.price
            self.high = tick.price
            self.low = tick.price
            self.first_sequence = tick.sequence
        else:
            self.high = max(self.high, tick.price)
            self.low = min(self.low, tick.price)

        self.close = tick.price
        self.volume += tick.size
        if tick.side == "buy":
            self.buy_volume += tick.size
        elif tick.side == "sell":
            self.sell_volume += tick.size

        self.tick_count += 1
        self.last_sequence = tick.sequence
        self.cvd_close = float(cvd_after_tick)
        self.cvd_high = max(self.cvd_high, self.cvd_close)
        self.cvd_low = min(self.cvd_low, self.cvd_close)

    def freeze(self) -> OrderFlowBar:
        return OrderFlowBar(
            symbol=self.symbol,
            interval_seconds=self.interval_seconds,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            buy_volume=self.buy_volume,
            sell_volume=self.sell_volume,
            tick_count=self.tick_count,
            cvd_open=self.cvd_open,
            cvd_high=self.cvd_high,
            cvd_low=self.cvd_low,
            cvd_close=self.cvd_close,
            first_sequence=self.first_sequence,
            last_sequence=self.last_sequence,
        )


class OrderFlowAggregator:
    """Aggregates signed trade ticks into aligned 10s/30s/1m CVD bars."""

    def __init__(
        self,
        intervals: Iterable[int] = DEFAULT_ORDER_FLOW_INTERVALS,
        *,
        intervals_sec: Iterable[int] | None = None,
        large_trade_size: float | None = None,
    ) -> None:
        if intervals_sec is not None:
            intervals = intervals_sec
        normalized = tuple(sorted({int(interval) for interval in intervals if int(interval) > 0}))
        if not normalized:
            raise ValueError("at least one positive interval is required")
        self.intervals = normalized
        self.large_trade_size = float(large_trade_size or 0.0)
        self._bars: dict[tuple[str, int], _WorkingOrderFlowBar] = {}
        self._latest_closed: dict[tuple[str, int], OrderFlowBar] = {}
        self._cvd_by_symbol: dict[str, float] = {}
        self._last_tick_ms_by_symbol: dict[str, int] = {}

    def update(self, tick: OrderFlowTick) -> list[OrderFlowBar]:
        if not isinstance(tick, OrderFlowTick):
            raise TypeError("tick must be an OrderFlowTick")

        last_tick_ms = self._last_tick_ms_by_symbol.get(tick.symbol)
        if last_tick_ms is not None and tick.timestamp_ms < last_tick_ms:
            raise ValueError(
                f"out-of-order tick for {tick.symbol}: {tick.timestamp_ms} < {last_tick_ms}"
            )

        cvd_before_tick = self._cvd_by_symbol.get(tick.symbol, 0.0)
        cvd_after_tick = cvd_before_tick + tick.signed_volume
        self._cvd_by_symbol[tick.symbol] = cvd_after_tick
        self._last_tick_ms_by_symbol[tick.symbol] = tick.timestamp_ms

        closed: list[OrderFlowBar] = []
        for interval_seconds in self.intervals:
            interval_ms = interval_seconds * 1000
            start_ms = (tick.timestamp_ms // interval_ms) * interval_ms
            key = (tick.symbol, interval_seconds)
            current = self._bars.get(key)
            if current is None:
                current = self._new_bar(tick.symbol, interval_seconds, start_ms, cvd_before_tick)
                self._bars[key] = current
            elif current.start_ms != start_ms:
                if current.tick_count > 0:
                    closed.append(current.freeze())
                current = self._new_bar(tick.symbol, interval_seconds, start_ms, cvd_before_tick)
                self._bars[key] = current

            current.update(tick, cvd_after_tick)

        closed = self._sort_bars(closed)
        for bar in closed:
            self._latest_closed[(bar.symbol, bar.interval_seconds)] = bar
        return closed

    def on_tick(self, tick_data: OrderFlowTick | Mapping[str, Any]) -> list[OrderFlowBar]:
        tick = tick_data if isinstance(tick_data, OrderFlowTick) else self._tick_from_mapping(tick_data)
        return self.update(tick)

    def latest_bar(self, symbol: str, *, interval_sec: int = 60) -> OrderFlowBar | None:
        normalized_symbol = normalize_symbol(symbol)
        interval = int(interval_sec)
        latest = self._latest_closed.get((normalized_symbol, interval))
        if latest is not None:
            return latest
        for bar in self.snapshot(normalized_symbol):
            if bar.interval_seconds == interval:
                return bar
        return None

    def confirmation(
        self,
        symbol: str,
        direction: str,
        *,
        min_delta_ratio: float = 0.0,
        interval_sec: int = 60,
    ) -> dict[str, Any]:
        bar = self.latest_bar(symbol, interval_sec=interval_sec)
        if bar is None:
            return {"ok": False, "reason": "bar_not_found", "interval_sec": int(interval_sec)}

        direction = str(direction or "").strip().lower()
        delta_ratio = abs(bar.delta) / bar.volume if bar.volume > 0 else 0.0
        directional_ok = bar.delta > 0 if direction in {"buy", "long"} else bar.delta < 0
        ratio_ok = delta_ratio >= float(min_delta_ratio or 0.0)
        return {
            "ok": bool(directional_ok and ratio_ok),
            "reason": "confirmed" if directional_ok and ratio_ok else "delta_not_confirmed",
            "symbol": bar.symbol,
            "direction": direction,
            "interval_sec": int(interval_sec),
            "delta": bar.delta,
            "volume": bar.volume,
            "delta_ratio": delta_ratio,
            "cvd": bar.cvd_close,
        }

    def close_all(self, symbol: str | None = None) -> list[OrderFlowBar]:
        normalized_symbol = normalize_symbol(symbol) if symbol else ""
        closed: list[OrderFlowBar] = []
        for key, current in list(self._bars.items()):
            if normalized_symbol and key[0] != normalized_symbol:
                continue
            if current.tick_count > 0:
                closed.append(current.freeze())
            self._bars.pop(key, None)
        closed = self._sort_bars(closed)
        for bar in closed:
            self._latest_closed[(bar.symbol, bar.interval_seconds)] = bar
        return closed

    def snapshot(self, symbol: str | None = None) -> list[OrderFlowBar]:
        normalized_symbol = normalize_symbol(symbol) if symbol else ""
        bars = [
            current.freeze()
            for key, current in self._bars.items()
            if current.tick_count > 0 and (not normalized_symbol or key[0] == normalized_symbol)
        ]
        return self._sort_bars(bars)

    def reset(self) -> None:
        self._bars.clear()
        self._latest_closed.clear()
        self._cvd_by_symbol.clear()
        self._last_tick_ms_by_symbol.clear()

    def current_cvd(self, symbol: str) -> float:
        return self._cvd_by_symbol.get(normalize_symbol(symbol), 0.0)

    @staticmethod
    def _new_bar(
        symbol: str,
        interval_seconds: int,
        start_ms: int,
        cvd_open: float,
    ) -> _WorkingOrderFlowBar:
        interval_ms = interval_seconds * 1000
        return _WorkingOrderFlowBar(
            symbol=symbol,
            interval_seconds=interval_seconds,
            start_ms=start_ms,
            end_ms=start_ms + interval_ms,
            cvd_open=cvd_open,
        )

    @staticmethod
    def _sort_bars(bars: list[OrderFlowBar]) -> list[OrderFlowBar]:
        return sorted(bars, key=lambda bar: (bar.end_ms, bar.interval_seconds, bar.symbol))

    @staticmethod
    def _tick_from_mapping(tick_data: Mapping[str, Any]) -> OrderFlowTick:
        data = dict(tick_data or {})
        timestamp_ms = data.get("timestamp_ms", data.get("_updated", data.get("time_ms", 0)))
        price = data.get("price", data.get("last_price", data.get("last", data.get("31"))))
        size = data.get("size", data.get("volume", data.get("last_size", 0.0)))
        return OrderFlowTick(
            symbol=data.get("symbol", ""),
            timestamp_ms=int(timestamp_ms or 0),
            price=float(price),
            size=float(size or 0.0),
            side=str(data.get("side", data.get("aggressor_side", ""))),
            bid=data.get("bid"),
            ask=data.get("ask"),
            sequence=int(data.get("sequence", data.get("seq", 0)) or 0),
            source=str(data.get("source", "")),
            metadata=data.get("metadata", {}),
        )
