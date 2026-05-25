from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


BUY_TICK_SIDES = {"ask", "at_ask", "b", "buy", "buyer", "long", "uptick"}
SELL_TICK_SIDES = {"at_bid", "bid", "downtick", "s", "sell", "seller", "short"}
LONG_SIDES = {"b", "buy", "long"}
SHORT_SIDES = {"s", "sell", "short"}


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def normalize_tick_side(side: str) -> str:
    normalized = str(side or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in BUY_TICK_SIDES:
        return "buy"
    if normalized in SELL_TICK_SIDES:
        return "sell"
    return "unknown"


def normalize_position_side(side: str) -> str:
    normalized = str(side or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in LONG_SIDES:
        return "long"
    if normalized in SHORT_SIDES:
        return "short"
    return "unknown"


def freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class OrderFlowTick:
    """A normalized trade tick with deterministic signed-volume semantics."""

    symbol: str
    timestamp_ms: int
    price: float
    size: float = 0.0
    side: str = "unknown"
    bid: float | None = None
    ask: float | None = None
    sequence: int = 0
    source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        if not symbol:
            raise ValueError("symbol is required")

        timestamp_ms = int(self.timestamp_ms)
        if timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")

        price = float(self.price)
        if price <= 0:
            raise ValueError("price must be positive")

        size = float(self.size or 0.0)
        if size < 0:
            raise ValueError("size must be non-negative")

        side = normalize_tick_side(self.side)
        if side == "unknown":
            side = self._infer_side_from_quote(price)

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp_ms", timestamp_ms)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "sequence", int(self.sequence or 0))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def _infer_side_from_quote(self, price: float) -> str:
        try:
            ask = float(self.ask) if self.ask is not None else None
            bid = float(self.bid) if self.bid is not None else None
        except (TypeError, ValueError):
            return "unknown"
        if ask is not None and price >= ask:
            return "buy"
        if bid is not None and price <= bid:
            return "sell"
        return "unknown"

    @property
    def signed_volume(self) -> float:
        if self.side == "buy":
            return self.size
        if self.side == "sell":
            return -self.size
        return 0.0


@dataclass(frozen=True, slots=True)
class OrderFlowBar:
    """A closed or preview CVD bar for one symbol and interval."""

    symbol: str
    interval_seconds: int
    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    tick_count: int
    cvd_open: float
    cvd_high: float
    cvd_low: float
    cvd_close: float
    first_sequence: int = 0
    last_sequence: int = 0

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        if not symbol:
            raise ValueError("symbol is required")
        interval_seconds = int(self.interval_seconds)
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        start_ms = int(self.start_ms)
        end_ms = int(self.end_ms)
        if end_ms <= start_ms:
            raise ValueError("end_ms must be greater than start_ms")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "interval_seconds", interval_seconds)
        object.__setattr__(self, "start_ms", start_ms)
        object.__setattr__(self, "end_ms", end_ms)
        object.__setattr__(self, "open", float(self.open))
        object.__setattr__(self, "high", float(self.high))
        object.__setattr__(self, "low", float(self.low))
        object.__setattr__(self, "close", float(self.close))
        object.__setattr__(self, "volume", float(self.volume or 0.0))
        object.__setattr__(self, "buy_volume", float(self.buy_volume or 0.0))
        object.__setattr__(self, "sell_volume", float(self.sell_volume or 0.0))
        object.__setattr__(self, "tick_count", int(self.tick_count or 0))
        object.__setattr__(self, "cvd_open", float(self.cvd_open or 0.0))
        object.__setattr__(self, "cvd_high", float(self.cvd_high or 0.0))
        object.__setattr__(self, "cvd_low", float(self.cvd_low or 0.0))
        object.__setattr__(self, "cvd_close", float(self.cvd_close or 0.0))
        object.__setattr__(self, "first_sequence", int(self.first_sequence or 0))
        object.__setattr__(self, "last_sequence", int(self.last_sequence or 0))

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def cvd(self) -> float:
        return self.cvd_close

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "delta": self.delta,
            "tick_count": self.tick_count,
            "cvd_open": self.cvd_open,
            "cvd_high": self.cvd_high,
            "cvd_low": self.cvd_low,
            "cvd_close": self.cvd_close,
            "cvd": self.cvd,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }
