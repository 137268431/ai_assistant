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
from datetime import datetime, timedelta
from ibkr_compute.core.time_utils import ET
from typing import Callable, Dict, Tuple

logger = logging.getLogger(__name__)


DEFAULT_TRADE_WINDOW_START = (9, 35)
DEFAULT_TRADE_WINDOW_END = (15, 30)
DEFAULT_ORDER_WINDOW_END = (15, 0)
DEFAULT_SIGNAL_EXPIRY_MINUTES = 30
DEFAULT_COOLDOWN_BAR_MINUTES = 5


class SignalProcessor:
    def __init__(
        self,
        config,
        order_lifecycle=None,
        environment: str = "live",
        readiness_provider: Callable | None = None,
        target_direction_provider: Callable | None = None,
    ):
        self.config = config
        self.order_lifecycle = order_lifecycle
        self.environment = environment
        self.readiness_provider = readiness_provider
        self.target_direction_provider = target_direction_provider
        self._active_positions: Dict[str, dict] = {}
        self._cooldowns: Dict[str, dict] = {}

    def validate_signal(self, signal: dict) -> Tuple[bool, str]:
        et_now = datetime.now(ET)

        if not self._is_trading_enabled():
            return False, "trading_disabled"

        trade_ready, trade_ready_reason = self._trade_readiness_status()
        if not trade_ready:
            return False, trade_ready_reason

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

        cooldown_active, cooldown_reason = self._cooldown_status(symbol, et_now)
        if cooldown_active:
            return False, cooldown_reason

        if self._has_conflicting_position(symbol, direction):
            return False, "direction_conflict"

        aligned, alignment_reason = self._target_direction_alignment_status(symbol, direction)
        if not aligned:
            return False, alignment_reason

        if not self._validate_prices(signal):
            return False, "invalid_prices"

        return True, "ok"

    def _is_trading_enabled(self) -> bool:
        return self.config.get_bool_for_environment(
            "ibkr_trading_enabled", self.environment, True
        )

    def _trade_readiness_status(self) -> Tuple[bool, str]:
        if not callable(self.readiness_provider):
            return True, "ok"
        try:
            payload = self.readiness_provider()
        except Exception as exc:
            logger.warning("Signal readiness provider failed: %s", exc)
            return False, "warmup_status_error"

        if isinstance(payload, dict):
            if payload.get("open"):
                return True, str(payload.get("reason") or "ok")
            return False, str(payload.get("reason") or "warmup_incomplete")

        return (True, "ok") if payload else (False, "warmup_incomplete")

    def _target_direction_alignment_enabled(self) -> bool:
        getter = getattr(self.config, "get_bool_for_environment", None)
        if not callable(getter):
            return False
        return bool(getter("ibkr_require_target_direction_alignment", self.environment, False))

    def _target_direction_alignment_status(self, symbol: str, direction: str) -> Tuple[bool, str]:
        if not self._target_direction_alignment_enabled():
            return True, "ok"
        if not callable(self.target_direction_provider):
            return True, "ok"
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_direction = str(direction or "").strip().lower()
        if not normalized_symbol or normalized_direction not in {"long", "short"}:
            return False, "target_direction_missing"
        try:
            payload = self.target_direction_provider()
        except Exception as exc:
            logger.warning("Target direction provider failed: %s", exc)
            return False, "target_direction_provider_error"
        if not isinstance(payload, dict):
            return False, "target_direction_provider_error"
        target_direction = str(payload.get(normalized_symbol, "") or "").strip().lower()
        if target_direction not in {"long", "short"}:
            return False, "target_direction_missing"
        if normalized_direction != target_direction:
            return False, "target_direction_mismatch"
        return True, "ok"

    def _get_time_window(self, key: str, default: Tuple[int, int]) -> Tuple[int, int]:
        raw_value = str(
            self.config.get_for_environment(
                key, self.environment, f"{default[0]:02d}:{default[1]:02d}"
            )
            or ""
        ).strip()
        try:
            hour_text, minute_text = raw_value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return default

    def _trade_window_start(self) -> Tuple[int, int]:
        return self._get_time_window("trade_window_start_time", DEFAULT_TRADE_WINDOW_START)

    def _trade_window_end(self) -> Tuple[int, int]:
        return self._get_time_window("trade_window_end_time", DEFAULT_TRADE_WINDOW_END)

    def _order_window_end(self) -> Tuple[int, int]:
        return self._get_time_window("order_window_end_time", DEFAULT_ORDER_WINDOW_END)

    def _signal_validity_minutes(self) -> int:
        return max(
            1,
            self.config.get_int_for_environment(
                "signal_validity_minutes",
                self.environment,
                DEFAULT_SIGNAL_EXPIRY_MINUTES,
            ),
        )

    def _in_trade_window(self, et_now: datetime) -> bool:
        current = (et_now.hour, et_now.minute)
        return self._trade_window_start() <= current <= self._trade_window_end()

    def _in_order_window(self, et_now: datetime) -> bool:
        current = (et_now.hour, et_now.minute)
        return self._trade_window_start() <= current <= self._order_window_end()

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
            return age_minutes > self._signal_validity_minutes()
        except Exception:
            return False

    def _has_conflicting_position(self, symbol: str, direction: str) -> bool:
        if symbol in self._active_positions:
            return True
        return False

    def _cooldown_status(self, symbol: str, et_now: datetime) -> Tuple[bool, str]:
        if not symbol:
            return False, "ok"
        row = self._cooldowns.get(symbol)
        if not row:
            return False, "ok"
        until = row.get("until")
        if not isinstance(until, datetime):
            self._cooldowns.pop(symbol, None)
            return False, "ok"
        if et_now >= until:
            self._cooldowns.pop(symbol, None)
            return False, "ok"
        reason = str(row.get("reason") or "cooldown_active").strip() or "cooldown_active"
        return True, reason

    def cooldown_bars_after_sl(self) -> int:
        return max(0, self.config.get_int_for_environment("cooldown_bars_after_sl", self.environment, 6))

    def cooldown_bars_after_reverse(self) -> int:
        return max(0, self.config.get_int_for_environment("cooldown_bars_after_reverse", self.environment, 3))

    def start_cooldown(self, symbol: str, bars: int, reason: str, now: datetime | None = None):
        text = str(symbol or "").strip().upper()
        safe_bars = max(0, int(bars or 0))
        if not text or safe_bars <= 0:
            return
        et_now = now.astimezone(ET) if isinstance(now, datetime) else datetime.now(ET)
        self._cooldowns[text] = {
            "until": et_now + timedelta(minutes=safe_bars * DEFAULT_COOLDOWN_BAR_MINUTES),
            "bars": safe_bars,
            "reason": str(reason or "cooldown_active").strip() or "cooldown_active",
        }

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
        self._active_positions[str(symbol or "").upper()] = position_data

    def register_pending_entry(self, symbol: str, position_data: dict):
        payload = dict(position_data or {})
        payload["state"] = "pending_entry"
        self.register_position(symbol, payload)

    def register_filled_position(self, symbol: str, position_data: dict):
        payload = dict(position_data or {})
        payload["state"] = "filled_position"
        self.register_position(symbol, payload)

    def remove_position(self, symbol: str):
        self._active_positions.pop(str(symbol or "").upper(), None)

    def daily_reset(self):
        self._active_positions.clear()
        self._cooldowns.clear()
        logger.info("Signal processor daily reset")

    def status(self) -> dict:
        trade_ready, trade_ready_reason = self._trade_readiness_status()
        return {
            "environment": self.environment,
            "active_positions": len(self._active_positions),
            "positions": list(self._active_positions.keys()),
            "cooldowns": {
                symbol: {
                    "until": row.get("until").isoformat() if isinstance(row.get("until"), datetime) else "",
                    "reason": row.get("reason", ""),
                    "bars": row.get("bars", 0),
                }
                for symbol, row in self._cooldowns.items()
            },
            "ibkr_trading_enabled": self._is_trading_enabled(),
            "trading_gate_open": trade_ready,
            "trading_gate_reason": trade_ready_reason,
            "target_direction_alignment_required": self._target_direction_alignment_enabled(),
            "trade_window_start_time": f"{self._trade_window_start()[0]:02d}:{self._trade_window_start()[1]:02d}",
            "trade_window_end_time": f"{self._trade_window_end()[0]:02d}:{self._trade_window_end()[1]:02d}",
            "order_window_end_time": f"{self._order_window_end()[0]:02d}:{self._order_window_end()[1]:02d}",
            "signal_validity_minutes": self._signal_validity_minutes(),
        }
