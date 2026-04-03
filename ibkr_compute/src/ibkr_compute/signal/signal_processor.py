"""
信号验证与处理
从 quant_trading/signal_processor.py 迁移核心逻辑:
- 交易窗口检查
- 持仓限制 (max 3)
- 日止损熔断 (max 3 stops)
- 信号有效期 (30-60 分钟)
- 方向冲突检测
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))

TRADE_WINDOW_START = (9, 35)
TRADE_WINDOW_END = (15, 30)
ORDER_WINDOW_END = (15, 0)
SIGNAL_EXPIRY_MINUTES = 60
MAX_POSITIONS = 3
MAX_DAILY_SL = 3


class SignalProcessor:
    def __init__(self, config, order_lifecycle=None, environment: str = "live"):
        self.config = config
        self.order_lifecycle = order_lifecycle
        self.environment = environment
        self._active_positions: Dict[str, dict] = {}

    def validate_signal(self, signal: dict) -> Tuple[bool, str]:
        et_now = datetime.now(ET)

        if not self._is_trading_enabled():
            return False, "trading_disabled"

        if not self._in_trade_window(et_now):
            return False, "outside_trade_window"

        if not self._in_order_window(et_now):
            return False, "outside_order_window"

        if self._is_signal_expired(signal, et_now):
            return False, "signal_expired"

        if self.order_lifecycle and self.order_lifecycle.is_sl_circuit_breaker:
            return False, "sl_circuit_breaker"

        if self.order_lifecycle and self.order_lifecycle.is_position_limit_reached:
            return False, "position_limit_reached"

        symbol = signal.get("symbol", "").upper()
        direction = signal.get("direction", "")

        if self._has_conflicting_position(symbol, direction):
            return False, "direction_conflict"

        if not self._validate_prices(signal):
            return False, "invalid_prices"

        return True, "ok"

    def _is_trading_enabled(self) -> bool:
        return self.config.get_bool_for_environment(
            "ibkr_trading_enabled", self.environment, True
        )

    def _in_trade_window(self, et_now: datetime) -> bool:
        current = (et_now.hour, et_now.minute)
        return TRADE_WINDOW_START <= current <= TRADE_WINDOW_END

    def _in_order_window(self, et_now: datetime) -> bool:
        current = (et_now.hour, et_now.minute)
        return TRADE_WINDOW_START <= current <= ORDER_WINDOW_END

    def _is_signal_expired(self, signal: dict, et_now: datetime) -> bool:
        signal_time_str = signal.get("signal_time", "")
        if not signal_time_str:
            return False

        try:
            if "T" in signal_time_str:
                signal_time = datetime.fromisoformat(signal_time_str)
            else:
                signal_time = datetime.strptime(signal_time_str, "%Y-%m-%d %H:%M:%S")
                signal_time = signal_time.replace(tzinfo=ET)

            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=ET)

            age_minutes = (et_now - signal_time).total_seconds() / 60
            return age_minutes > SIGNAL_EXPIRY_MINUTES
        except Exception:
            return False

    def _has_conflicting_position(self, symbol: str, direction: str) -> bool:
        if symbol in self._active_positions:
            existing = self._active_positions[symbol]
            if existing.get("direction") == direction:
                return True
        return False

    def _validate_prices(self, signal: dict) -> bool:
        entry = signal.get("entry", 0)
        sl = signal.get("stop_loss", 0)
        tp = signal.get("take_profit", 0)
        direction = signal.get("direction", "")

        if entry <= 0 or sl <= 0 or tp <= 0:
            return False

        if direction == "long":
            if sl >= entry or tp <= entry:
                return False
        elif direction == "short":
            if sl <= entry or tp >= entry:
                return False
        else:
            return False

        return True

    def register_position(self, symbol: str, position_data: dict):
        self._active_positions[symbol] = position_data

    def remove_position(self, symbol: str):
        self._active_positions.pop(symbol, None)

    def daily_reset(self):
        self._active_positions.clear()
        logger.info("Signal processor daily reset")

    def status(self) -> dict:
        return {
            "environment": self.environment,
            "active_positions": len(self._active_positions),
            "positions": list(self._active_positions.keys()),
            "ibkr_trading_enabled": self._is_trading_enabled(),
        }
