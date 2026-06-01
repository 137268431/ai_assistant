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
        capacity_provider: Callable | None = None,
    ):
        self.config = config
        self.order_lifecycle = order_lifecycle
        self.environment = environment
        self.readiness_provider = readiness_provider
        self.target_direction_provider = target_direction_provider
        self.capacity_provider = capacity_provider
        self._active_positions: Dict[str, dict] = {}
        self._cooldowns: Dict[str, dict] = {}
        self._daily_entry_counts: Dict[str, int] = {}
        self._daily_entry_count_keys: set[str] = set()
        self._daily_entry_counts_date = ""

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

        if self._is_fixed_position_symbol(symbol):
            return False, "fixed_position_symbol_blocked"

        capacity_ok, capacity_reason = self._strategy_capacity_status()
        if not capacity_ok:
            return False, capacity_reason

        cooldown_active, cooldown_reason = self._cooldown_status(symbol, et_now)
        if cooldown_active:
            return False, cooldown_reason

        if self._has_conflicting_position(symbol, direction):
            return False, "direction_conflict"

        daily_entry_ok, daily_entry_reason = self._daily_entry_limit_status(symbol, et_now)
        if not daily_entry_ok:
            return False, daily_entry_reason

        aligned, alignment_reason = self._target_direction_alignment_status(symbol, direction)
        if not aligned:
            return False, alignment_reason

        if not self._validate_prices(signal):
            return False, "invalid_prices"

        return True, "ok"

    def _is_trading_enabled(self) -> bool:
        trading_enabled = self.config.get_bool_for_environment(
            "ibkr_trading_enabled", self.environment, True
        )
        if str(self.environment or "").strip().lower() != "live":
            return trading_enabled
        live_trading_enabled = self.config.get_bool_for_environment(
            "ibkr_live_trading_enabled", self.environment, True
        )
        return bool(trading_enabled and live_trading_enabled)

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
        defaults = getattr(self.config, "DEFAULTS", {}) or {}
        default_text = str(defaults.get("ibkr_require_target_direction_alignment", "false")).strip().lower()
        default_enabled = default_text in {"1", "true", "yes", "on"}
        return bool(getter("ibkr_require_target_direction_alignment", self.environment, default_enabled))

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

    def _strategy_capacity_status(self) -> Tuple[bool, str]:
        if callable(self.capacity_provider):
            try:
                payload = self.capacity_provider()
            except Exception as exc:
                logger.warning("Signal capacity provider failed: %s", exc)
                return True, "capacity_provider_error"
            if isinstance(payload, dict) and payload.get("capacity_full"):
                return False, "strategy_capacity_full"
            return True, "ok"

        if self.order_lifecycle and getattr(self.order_lifecycle, "is_strategy_capacity_full", False):
            return False, "strategy_capacity_full"
        return True, "ok"

    def _is_fixed_position_symbol(self, symbol: str) -> bool:
        checker = getattr(self.order_lifecycle, "is_fixed_position_symbol", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(symbol))
        except Exception as exc:
            logger.warning("Fixed-position symbol check failed: %s", exc)
            return False

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

    def _reentry_policy(self) -> str:
        getter = getattr(self.config, "get_for_environment", None)
        if not callable(getter):
            return "controlled"
        try:
            return str(getter("intraday_reentry_policy", self.environment, "controlled") or "controlled").strip().lower()
        except Exception:
            return "controlled"

    def _controlled_reentry_enabled(self) -> bool:
        return self._reentry_policy() in {"controlled", "enabled", "allow", "allow_reentry", "reentry"}

    def _symbol_daily_entry_limit(self) -> int:
        getter = getattr(self.config, "get_int_for_environment", None)
        if not callable(getter):
            return 3
        try:
            return max(0, int(getter("intraday_symbol_daily_entry_limit", self.environment, 3)))
        except Exception:
            return 3

    @staticmethod
    def _market_date(et_now: datetime) -> str:
        return et_now.astimezone(ET).strftime("%Y-%m-%d")

    def _ensure_daily_entry_count_date(self, et_now: datetime | None = None) -> str:
        now = et_now.astimezone(ET) if isinstance(et_now, datetime) else datetime.now(ET)
        market_date = self._market_date(now)
        if self._daily_entry_counts_date != market_date:
            self._daily_entry_counts.clear()
            self._daily_entry_count_keys.clear()
            self._daily_entry_counts_date = market_date
        return market_date

    def _daily_entry_limit_status(self, symbol: str, et_now: datetime) -> Tuple[bool, str]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol or not self._controlled_reentry_enabled():
            return True, "ok"
        limit = self._symbol_daily_entry_limit()
        if limit <= 0:
            return True, "ok"
        self._ensure_daily_entry_count_date(et_now)
        count = int(self._daily_entry_counts.get(normalized_symbol, 0) or 0)
        if count >= limit:
            return False, "symbol_daily_entry_limit_reached"
        return True, "ok"

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
        self._record_daily_entry(symbol, position_data)
        payload = dict(position_data or {})
        payload["state"] = "filled_position"
        self.register_position(symbol, payload)

    def remove_position(self, symbol: str):
        self._active_positions.pop(str(symbol or "").upper(), None)

    def daily_reset(self):
        self._active_positions.clear()
        self._cooldowns.clear()
        self._daily_entry_counts.clear()
        self._daily_entry_count_keys.clear()
        self._daily_entry_counts_date = self._market_date(datetime.now(ET))
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
            "ibkr_live_trading_enabled": self.config.get_bool_for_environment(
                "ibkr_live_trading_enabled", self.environment, True
            ),
            "trading_gate_open": trade_ready,
            "trading_gate_reason": trade_ready_reason,
            "target_direction_alignment_required": self._target_direction_alignment_enabled(),
            "strategy_capacity_provider_enabled": callable(self.capacity_provider),
            "reentry_policy": self._reentry_policy(),
            "symbol_daily_entry_limit": self._symbol_daily_entry_limit(),
            "daily_entry_counts_date": self._ensure_daily_entry_count_date(datetime.now(ET)),
            "daily_entry_counts": dict(self._daily_entry_counts),
            "trade_window_start_time": f"{self._trade_window_start()[0]:02d}:{self._trade_window_start()[1]:02d}",
            "trade_window_end_time": f"{self._trade_window_end()[0]:02d}:{self._trade_window_end()[1]:02d}",
            "order_window_end_time": f"{self._order_window_end()[0]:02d}:{self._order_window_end()[1]:02d}",
            "signal_validity_minutes": self._signal_validity_minutes(),
        }

    def _record_daily_entry(self, symbol: str, position_data: dict | None = None) -> None:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return
        now = datetime.now(ET)
        market_date = self._ensure_daily_entry_count_date(now)
        payload = dict(position_data or {})
        existing = self._active_positions.get(normalized_symbol)
        if isinstance(existing, dict) and str(existing.get("state") or "").strip().lower() == "filled_position":
            existing_key = self._daily_entry_count_key(market_date, normalized_symbol, existing)
            if not existing_key or existing_key == self._daily_entry_count_key(market_date, normalized_symbol, payload):
                return
        entry_key = self._daily_entry_count_key(market_date, normalized_symbol, payload)
        if entry_key and entry_key in self._daily_entry_count_keys:
            return
        self._daily_entry_counts[normalized_symbol] = int(self._daily_entry_counts.get(normalized_symbol, 0) or 0) + 1
        if entry_key:
            self._daily_entry_count_keys.add(entry_key)

    @staticmethod
    def _daily_entry_count_key(market_date: str, symbol: str, payload: dict | None = None) -> str:
        row = payload if isinstance(payload, dict) else {}
        for key in (
            "signal_id",
            "id",
            "order_id",
            "broker_order_id",
            "orderId",
            "entry_order_id",
            "entry_coid",
            "cOID",
            "coid",
            "orderRef",
            "bracket_group",
            "trade_group_id",
        ):
            value = str(row.get(key) or "").strip()
            if value:
                return f"{market_date}:{symbol}:{key}:{value}"
        return ""
