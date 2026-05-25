"""
Order lifecycle helpers built on top of IB Gateway socket API.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from ibkr_compute.core.time_utils import ET
from typing import Any, Dict, List, Optional

from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.core.exit_policy import is_signal_mode_adaptive_exit_profile
from ibkr_compute.core.intraday_harvest import (
    ACTION_FULL_EXIT,
    ACTION_PARTIAL_EXIT,
    ACTION_REENTRY,
    ACTION_TIGHTEN_STOP,
    build_reentry_prices,
    evaluate_intraday_harvest,
    harvest_settings_from_config,
    suggested_stop_price,
)
from ibkr_compute.core.risk_management import (
    compute_atr_tightened_stop,
    compute_exit_policy_stop_update,
    compute_exit_policy_target_update,
)

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")

DEFAULT_EOD_CLOSE_TIME = (15, 55)
DEFAULT_POSITION_LIMIT_MAX = 0
DEFAULT_MAX_STRATEGY_OPEN_POSITIONS = 5
DEFAULT_CONSECUTIVE_STOP_LOSS_LIMIT = 3
DEFAULT_FIXED_POSITION_SYMBOLS = ("BOXX", "IBKR")
DEFAULT_LIVE_EXIT_POLICY_UPDATE_ENABLED = False
DEFAULT_KEEP_SYMBOLS = tuple(
    symbol.strip().upper()
    for symbol in os.environ.get("IBKR_EOD_KEEP_SYMBOLS", "").split(",")
    if symbol.strip()
)


class OrderLifecycle:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        order_modifier=None,
        order_placer=None,
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
        order_flow_manager=None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.order_modifier = order_modifier
        self.order_placer = order_placer
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()
        self.order_flow_manager = order_flow_manager

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0
        self._last_live_exit_policy_update_ms = 0
        self._live_exit_policy_update_count = 0
        self._live_exit_policy_update_error_count = 0
        self._harvest_locks: dict[str, threading.Lock] = {}
        self._harvest_frozen_symbols: dict[str, str] = {}
        self._last_harvest_action_ms: dict[str, int] = {}
        self._intraday_harvest_action_count = 0
        self._intraday_harvest_error_count = 0
        self._last_intraday_harvest_action_ms = 0
        self._order_flow_risk_action_count = 0
        self._order_flow_risk_error_count = 0
        self._last_order_flow_risk_action_ms = 0

    def _get_config_value(self, key: str, default: str) -> str:
        if not self.config:
            return default
        return str(self.config.get_for_environment(key, self.environment, default) or default)

    def _get_config_int(self, key: str, default: int) -> int:
        if not self.config:
            return default
        return self.config.get_int_for_environment(key, self.environment, default)

    def _get_config_float(self, key: str, default: float) -> float:
        if not self.config:
            return float(default)
        getter = getattr(self.config, "get_float_for_environment", None)
        if callable(getter):
            return float(getter(key, self.environment, default))
        try:
            return float(self.config.get_for_environment(key, self.environment, default))
        except Exception:
            return float(default)

    def _get_config_bool(self, key: str, default: bool) -> bool:
        if not self.config:
            return bool(default)
        getter = getattr(self.config, "get_bool_for_environment", None)
        if callable(getter):
            return bool(getter(key, self.environment, default))
        raw = str(self.config.get_for_environment(key, self.environment, str(default)) or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    def _eod_close_time(self) -> tuple[int, int]:
        raw_value = self._get_config_value(
            "eod_close_time",
            f"{DEFAULT_EOD_CLOSE_TIME[0]:02d}:{DEFAULT_EOD_CLOSE_TIME[1]:02d}",
        ).strip()
        try:
            hour_text, minute_text = raw_value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return DEFAULT_EOD_CLOSE_TIME

    def _position_limit_max(self) -> int:
        return max(0, self._get_config_int("position_limit_max", DEFAULT_POSITION_LIMIT_MAX))

    def _max_strategy_open_positions(self) -> int:
        return max(
            0,
            self._get_config_int(
                "max_strategy_open_positions",
                DEFAULT_MAX_STRATEGY_OPEN_POSITIONS,
            ),
        )

    def _consecutive_stop_loss_limit(self) -> int:
        return max(1, self._get_config_int("consecutive_stop_loss_limit", DEFAULT_CONSECUTIVE_STOP_LOSS_LIMIT))

    def _live_exit_policy_updates_enabled(self) -> bool:
        return bool(
            self._get_config_bool("atr_dynamic_stop_enabled", True)
            and self._get_config_bool(
                "live_exit_policy_stop_update_enabled",
                DEFAULT_LIVE_EXIT_POLICY_UPDATE_ENABLED,
            )
        )

    @staticmethod
    def _parse_symbol_set(raw_value: str) -> set[str]:
        return {
            symbol.strip().upper()
            for symbol in str(raw_value or "").split(",")
            if symbol.strip()
        }

    def _keep_symbols(self) -> set[str]:
        raw_value = self._get_config_value("eod_keep_symbols", ",".join(DEFAULT_KEEP_SYMBOLS))
        return self._parse_symbol_set(raw_value)

    def _fixed_position_symbols(self) -> set[str]:
        symbols = set(DEFAULT_FIXED_POSITION_SYMBOLS)
        if self.config:
            raw_value = str(
                self.config.get_for_environment("fixed_position_symbols", self.environment, "")
                or ""
            )
        else:
            raw_value = ""
        configured = self._parse_symbol_set(raw_value)
        if configured:
            symbols.update(configured)
        else:
            symbols.update(self._keep_symbols())
        return symbols

    def is_fixed_position_symbol(self, symbol: str) -> bool:
        return str(symbol or "").strip().upper() in self._fixed_position_symbols()

    def strategy_open_position_symbols(self, positions: Optional[List[Dict]] = None) -> set[str]:
        source_positions = positions if positions is not None else self.get_positions()
        fixed_symbols = self._fixed_position_symbols()
        symbols: set[str] = set()
        for pos in source_positions or []:
            symbol = str(pos.get("ticker") or pos.get("symbol") or pos.get("contractDesc") or "").strip().upper()
            if not symbol or symbol in fixed_symbols:
                continue
            try:
                quantity = float(pos.get("position", pos.get("quantity", 0)) or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if quantity:
                symbols.add(symbol)
        return symbols

    @staticmethod
    def _open_order_status(order: Dict) -> str:
        return str(
            order.get("status")
            or order.get("order_status")
            or order.get("orderStatus")
            or order.get("state")
            or ""
        ).strip().upper()

    @staticmethod
    def _is_open_order_status(status: str) -> bool:
        return str(status or "").strip().upper() not in {
            "",
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }

    @staticmethod
    def _is_strategy_entry_order(order: Dict) -> bool:
        parent_id = str(order.get("parentId") or order.get("parent_id") or "").strip()
        if parent_id:
            return False
        role = str(order.get("role") or "").strip().lower()
        order_type = str(order.get("order_type") or order.get("orderType") or "").strip().upper()
        coid = str(
            order.get("cOID")
            or order.get("coid")
            or order.get("orderRef")
            or order.get("order_ref")
            or order.get("unique_id")
            or ""
        ).strip()
        if role == "entry":
            return True
        if coid.startswith("entry_"):
            return True
        if order_type in {"TAKEPROFIT", "STOPLOSS"}:
            return False
        return False

    def strategy_open_entry_orders(
        self,
        order_tracker=None,
        open_orders: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        orders = open_orders
        if orders is None and order_tracker is not None:
            getter = getattr(order_tracker, "get_live_orders", None)
            if callable(getter):
                try:
                    orders = list(getter() or [])
                except Exception as exc:
                    logger.warning("Failed to get open orders for strategy capacity: %s", exc)
                    orders = []
        fixed_symbols = self._fixed_position_symbols()
        entry_orders: List[Dict] = []
        seen_order_ids: set[str] = set()
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = self._open_order_status(order)
            if not self._is_open_order_status(status):
                continue
            if not self._is_strategy_entry_order(order):
                continue
            symbol = str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip().upper()
            if not symbol or symbol in fixed_symbols:
                continue
            order_id = str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip()
            if order_id and order_id in seen_order_ids:
                continue
            if order_id:
                seen_order_ids.add(order_id)
            entry_orders.append(dict(order))
        return entry_orders

    def strategy_capacity_snapshot(
        self,
        order_tracker=None,
        positions: Optional[List[Dict]] = None,
        open_orders: Optional[List[Dict]] = None,
    ) -> Dict:
        max_positions = self._max_strategy_open_positions()
        position_symbols = self.strategy_open_position_symbols(positions)
        entry_orders = self.strategy_open_entry_orders(order_tracker, open_orders)
        entry_symbols = {
            str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip().upper()
            for order in entry_orders
            if str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip()
        }
        used = len(position_symbols) + len(entry_orders)
        return {
            "max_strategy_open_positions": max_positions,
            "strategy_open_positions": len(position_symbols),
            "strategy_open_position_symbols": sorted(position_symbols),
            "open_strategy_entry_orders": len(entry_orders),
            "open_strategy_entry_order_symbols": sorted(entry_symbols),
            "strategy_capacity_used": used,
            "strategy_capacity_remaining": max(0, max_positions - used) if max_positions > 0 else None,
            "capacity_full": max_positions > 0 and used >= max_positions,
            "fixed_position_symbols": sorted(self._fixed_position_symbols()),
        }

    @property
    def is_strategy_capacity_full(self) -> bool:
        return bool(self.strategy_capacity_snapshot().get("capacity_full"))


    def get_positions(self, acct_id: str = None) -> List[Dict]:
        try:
            return list(self.broker.list_positions() or [])
        except Exception as exc:
            logger.warning("Failed to get positions: %s", exc)
            return []

    def get_account_summary(self, acct_id: str = None) -> Dict:
        try:
            return dict(self.broker.get_account_summary() or {})
        except Exception as exc:
            logger.warning("Failed to get account summary: %s", exc)
            return {}

    def get_account_snapshot(self, acct_id: str = None) -> Dict:
        try:
            return dict(self.broker.get_account_snapshot(account=acct_id) or {})
        except Exception as exc:
            logger.warning("Failed to get account snapshot: %s", exc)
            return {}

    def get_account_pnl(self, acct_id: str = None) -> Dict:
        try:
            return dict(self.broker.get_account_pnl(account=acct_id) or {})
        except Exception as exc:
            logger.warning("Failed to get account PnL: %s", exc)
            return {"ok": False, "error": str(exc)}

    def eod_close_all(self, acct_id: str = None) -> Dict:
        positions = self.get_positions(acct_id)
        closed = 0
        errors = 0

        if self.order_modifier:
            self.order_modifier.cancel_all_orders(acct_id)

        for pos in positions:
            symbol = str(pos.get("ticker") or pos.get("contractDesc") or "").upper()
            position_qty = float(pos.get("position", 0) or 0)
            conid = int(pos.get("conid", 0) or 0)
            if not position_qty or not conid:
                continue
            if symbol in self._keep_symbols():
                logger.info("Keeping EOD position: %s", symbol)
                continue

            direction = "long" if position_qty > 0 else "short"
            result = self.broker.place_market_close(
                conid=conid,
                symbol=symbol,
                direction=direction,
                quantity=abs(int(round(position_qty))),
                account_id=str(acct_id or self.account_id or "").strip(),
            )
            if result.get("ok"):
                closed += 1
                logger.info("EOD close: %s %s %s shares", symbol, direction, abs(position_qty))
            else:
                errors += 1
                logger.error("EOD close failed for %s: %s", symbol, result.get("error"))

        self._eod_closed_today = True
        logger.info("EOD close complete: %d closed, %d errors", closed, errors)
        return {"closed": closed, "errors": errors}

    def daily_reset(self):
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0
        logger.info("Daily lifecycle reset")

    def increment_sl_count(self):
        self._daily_sl_count += 1

    def reset_sl_count(self):
        self._daily_sl_count = 0

    def increment_position_count(self):
        self._daily_position_count += 1

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _order_extra(row: dict | None) -> dict:
        extra = (row or {}).get("extra")
        if isinstance(extra, dict):
            return dict(extra)
        return {}

    @staticmethod
    def _escape_pb_value(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _order_role(row: dict | None) -> str:
        row = row or {}
        extra = OrderLifecycle._order_extra(row)
        return str(row.get("role") or extra.get("role") or "").strip().lower()

    @staticmethod
    def _order_group_key(row: dict | None) -> str:
        row = row or {}
        extra = OrderLifecycle._order_extra(row)
        return str(
            row.get("trade_group_id")
            or row.get("entry_order_unique_id")
            or extra.get("trade_group_id")
            or extra.get("entry_order_unique_id")
            or row.get("parent_order_unique_id")
            or extra.get("parent_order_unique_id")
            or ""
        ).strip()

    @staticmethod
    def _order_is_open(row: dict | None) -> bool:
        status = str((row or {}).get("status") or "").strip().upper()
        return status not in {
            "",
            "FILLED",
            "EXECUTED",
            "CANCELED",
            "CANCELLED",
            "CLOSED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
        }

    def _load_latest_5m_risk_snapshot(self, symbol: str) -> dict:
        if not self.pb_client:
            return {}
        safe_symbol = self._escape_pb_value(symbol)
        safe_env = self._escape_pb_value(self.environment)
        try:
            row = self.pb_client.get_first_record(
                "ibkr_indicators",
                filter=(
                    f'symbol = "{safe_symbol}" && interval = "5m" && '
                    f'environment = "{safe_env}"'
                ),
                sort="-bar_time_ms",
            )
        except Exception as exc:
            logger.debug("Latest indicator lookup failed for %s: %s", symbol, exc)
            row = {}
        if isinstance(row, dict) and row:
            return row
        try:
            row = self.pb_client.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{safe_symbol}" && interval = "5m" && '
                    f'environment = "{safe_env}"'
                ),
                sort="-bar_time_ms",
            )
        except Exception as exc:
            logger.debug("Latest bar lookup failed for %s: %s", symbol, exc)
            row = {}
        return row if isinstance(row, dict) else {}

    def _load_live_order_rows_for_symbol(self, symbol: str) -> list[dict]:
        if not self.pb_client:
            return []
        try:
            return list(
                self.pb_client.get_records(
                    "orders",
                    filter=(
                        f'environment = "{self._escape_pb_value(self.environment)}" && '
                        f'symbol = "{self._escape_pb_value(symbol)}"'
                    ),
                    sort="-updated",
                    per_page=80,
                    page=1,
                )
                or []
            )
        except Exception as exc:
            logger.debug("Live order row lookup failed for %s: %s", symbol, exc)
            return []

    def _select_live_exit_policy_group(self, rows: list[dict]) -> tuple[dict, dict, dict]:
        groups: dict[str, dict[str, dict]] = {}
        for row in rows or []:
            group_key = self._order_group_key(row)
            if not group_key:
                continue
            role = self._order_role(row)
            bucket = groups.setdefault(group_key, {})
            if role == "entry" and "entry" not in bucket:
                bucket["entry"] = row
            elif role == "stop_loss" and self._order_is_open(row) and "stop_loss" not in bucket:
                bucket["stop_loss"] = row
            elif role == "take_profit" and self._order_is_open(row) and "take_profit" not in bucket:
                bucket["take_profit"] = row
        candidates = [
            (bucket.get("entry") or {}, bucket.get("stop_loss") or {}, bucket.get("take_profit") or {})
            for bucket in groups.values()
            if bucket.get("stop_loss")
        ]
        if not candidates:
            return {}, {}, {}
        return max(
            candidates,
            key=lambda triple: int((triple[1] or triple[0] or {}).get("bar_time_ms", 0) or 0),
        )

    def _build_live_risk_position(
        self,
        broker_position: dict,
        entry_row: dict,
        stop_row: dict,
        target_row: dict,
    ) -> dict:
        quantity = self._coerce_float(
            broker_position.get("position", broker_position.get("quantity", 0)),
            0.0,
        )
        direction = "long" if quantity > 0 else "short"
        entry_extra = self._order_extra(entry_row)
        stop_extra = self._order_extra(stop_row)
        merged_extra = {**entry_extra, **stop_extra}
        entry_price = (
            self._coerce_float(entry_row.get("fill_price"), 0.0)
            or self._coerce_float(entry_row.get("limit_price"), 0.0)
            or self._coerce_float(broker_position.get("avgCost"), 0.0)
            or self._coerce_float(broker_position.get("avg_cost"), 0.0)
        )
        stop_price = (
            self._coerce_float(stop_row.get("limit_price"), 0.0)
            or self._coerce_float(stop_row.get("sl_price"), 0.0)
            or self._coerce_float(stop_extra.get("limit_price"), 0.0)
        )
        target_price = (
            self._coerce_float(target_row.get("limit_price"), 0.0)
            or self._coerce_float(entry_row.get("take_profit"), 0.0)
            or self._coerce_float(entry_row.get("tp_price"), 0.0)
            or self._coerce_float(entry_extra.get("initial_take_profit"), 0.0)
        )
        risk_r = (
            self._coerce_float(merged_extra.get("risk_r"), 0.0)
            or abs(entry_price - self._coerce_float(merged_extra.get("initial_stop_loss"), stop_price))
            or abs(entry_price - stop_price)
        )
        return {
            "direction": direction,
            "entry_price": entry_price,
            "entry": entry_price,
            "stop_price": stop_price,
            "stop_loss": stop_price,
            "target_price": target_price,
            "take_profit": target_price,
            "shares": abs(int(round(quantity))),
            "risk_r": risk_r,
            "initial_risk_r": risk_r,
            "initial_stop_loss": self._coerce_float(merged_extra.get("initial_stop_loss"), stop_price),
            "initial_take_profit": self._coerce_float(merged_extra.get("initial_take_profit"), target_price),
            "original_stop_loss": self._coerce_float(merged_extra.get("initial_stop_loss"), stop_price),
            "entry_atr": self._coerce_float(merged_extra.get("atr"), 0.0),
            "last_stop_atr": self._coerce_float(merged_extra.get("last_stop_atr"), self._coerce_float(merged_extra.get("atr"), 0.0)),
            "exit_policy_profile": str(merged_extra.get("exit_policy_profile") or ""),
            "exit_policy_settings": dict(merged_extra.get("exit_policy_settings") or {}),
            "target_state": dict(merged_extra.get("target_state") or {}),
            "trail_state": dict(merged_extra.get("trail_state") or {}),
            "extra": merged_extra,
        }

    def _apply_live_exit_policy_stop_update(
        self,
        *,
        position: dict,
        stop_row: dict,
        snapshot: dict,
    ) -> dict:
        current_price = self._coerce_float(snapshot.get("close"), 0.0)
        current_atr = self._coerce_float(snapshot.get("atr"), 0.0)
        if current_price <= 0 or current_atr <= 0:
            return {"updated": False, "reason": "missing_live_price_or_atr"}

        min_change = self._get_config_float("atr_stop_min_change", 0.01)
        if is_signal_mode_adaptive_exit_profile(position.get("exit_policy_profile")):
            target_result = compute_exit_policy_target_update(
                position,
                current_price=current_price,
                bar_high=self._coerce_float(snapshot.get("high"), current_price),
                bar_low=self._coerce_float(snapshot.get("low"), current_price),
                min_change=min_change,
            )
            if target_result.get("target_state"):
                position["target_state"] = dict(target_result.get("target_state") or {})
            if target_result.get("should_update_stop"):
                position["stop_price"] = float(target_result["new_sl"])
                position["stop_loss"] = float(target_result["new_sl"])
            result = compute_exit_policy_stop_update(
                position,
                current_price=current_price,
                current_atr=current_atr,
                bar_high=self._coerce_float(snapshot.get("high"), current_price),
                bar_low=self._coerce_float(snapshot.get("low"), current_price),
                min_change=min_change,
            )
            if result.get("trail_state"):
                position["trail_state"] = dict(result.get("trail_state") or {})
            if not result.get("should_update") and target_result.get("should_update_stop"):
                result = {
                    "should_update": True,
                    "new_sl": target_result.get("new_sl"),
                    "old_sl": target_result.get("old_sl"),
                    "current_atr": current_atr,
                    "reason": target_result.get("reason") or "target_policy_stop_adjust",
                }
        else:
            result = compute_atr_tightened_stop(
                position,
                current_price=current_price,
                current_atr=current_atr,
                sl_atr_mult=self._get_config_float("sl_atr_mult", 2.0),
                min_profit_r=self._get_config_float("atr_stop_min_profit_r", 0.3),
                deviation_threshold=self._get_config_float("atr_stop_deviation_threshold", 0.30),
                min_change=min_change,
            )

        if not result.get("should_update"):
            return {"updated": False, "reason": str(result.get("reason") or "no_update"), "result": result}

        stop_order_id = str(stop_row.get("broker_order_id") or stop_row.get("order_id") or "").strip()
        new_sl = self._coerce_float(result.get("new_sl"), 0.0)
        if not stop_order_id or new_sl <= 0:
            return {"updated": False, "reason": "missing_stop_order_or_price", "result": result}
        if not self.order_modifier:
            return {"updated": False, "reason": "order_modifier_unavailable", "result": result}

        modify_result = self.order_modifier.update_stop_loss(stop_order_id, new_sl)
        if not modify_result.get("ok"):
            return {"updated": False, "reason": "broker_modify_failed", "result": result, "modify_result": modify_result}

        self._live_exit_policy_update_count += 1
        self._last_live_exit_policy_update_ms = int(time.time() * 1000)
        if self.pb_client:
            stop_extra = self._order_extra(stop_row)
            update_extra = {
                **stop_extra,
                "live_exit_policy_stop_update": {
                    "updated_at_ms": self._last_live_exit_policy_update_ms,
                    "reason": str(result.get("reason") or "exit_policy_stop_adjust"),
                    "old_sl": self._coerce_float(result.get("old_sl"), 0.0),
                    "new_sl": new_sl,
                    "current_price": current_price,
                    "current_atr": current_atr,
                },
                "target_state": dict(position.get("target_state") or {}),
                "trail_state": dict(position.get("trail_state") or {}),
                "last_stop_atr": current_atr,
            }
            try:
                self.pb_client.upsert_order(
                    {
                        **stop_row,
                        "limit_price": new_sl,
                        "status": stop_row.get("status") or "Submitted",
                        "extra": update_extra,
                    }
                )
            except Exception as exc:
                logger.debug("PB stop update sync failed for %s: %s", stop_order_id, exc)
        return {"updated": True, "new_sl": new_sl, "result": result, "modify_result": modify_result}

    def _maybe_update_live_exit_policy_stops(self, positions: Optional[List[Dict]] = None):
        if not self._live_exit_policy_updates_enabled() or not self.pb_client or not self.order_modifier:
            return
        try:
            source_positions = positions if positions is not None else self.get_positions()
            fixed_symbols = self._fixed_position_symbols()
            for broker_position in source_positions or []:
                symbol = str(
                    broker_position.get("ticker")
                    or broker_position.get("symbol")
                    or broker_position.get("contractDesc")
                    or ""
                ).strip().upper()
                if not symbol or symbol in fixed_symbols:
                    continue
                quantity = self._coerce_float(
                    broker_position.get("position", broker_position.get("quantity", 0)),
                    0.0,
                )
                if not quantity:
                    continue
                rows = self._load_live_order_rows_for_symbol(symbol)
                entry_row, stop_row, target_row = self._select_live_exit_policy_group(rows)
                if not stop_row:
                    continue
                snapshot = self._load_latest_5m_risk_snapshot(symbol)
                position = self._build_live_risk_position(broker_position, entry_row, stop_row, target_row)
                update = self._apply_live_exit_policy_stop_update(
                    position=position,
                    stop_row=stop_row,
                    snapshot=snapshot,
                )
                if update.get("updated"):
                    logger.info(
                        "Live exit-policy stop updated: %s stop_order=%s new_sl=%.4f",
                        symbol,
                        stop_row.get("broker_order_id") or stop_row.get("order_id") or "",
                        float(update.get("new_sl") or 0.0),
                    )
        except Exception as exc:
            self._live_exit_policy_update_error_count += 1
            logger.warning("Live exit-policy stop update loop failed: %s", exc)

    def _order_flow_groups(self, rows: list[dict]) -> list[dict]:
        groups = [group for group in self._group_harvest_rows(rows) if self._harvest_group_active(group)]
        if groups:
            return groups
        entry_row, stop_row, target_row = self._select_live_exit_policy_group(rows)
        if not entry_row and not stop_row:
            return []
        group_key = self._order_group_key(entry_row) or self._order_group_key(stop_row) or self._order_group_key(target_row)
        return [
            {
                "group_key": group_key,
                "rows": [row for row in (entry_row, stop_row, target_row) if row],
                "lot": "primary",
                "entry": entry_row,
                "stop_loss": stop_row,
                "take_profit": target_row,
            }
        ]

    def _execute_order_flow_tighten_stop(
        self,
        *,
        symbol: str,
        groups: list[dict],
        direction: str,
        stop_price: float,
        decision: dict,
    ) -> int:
        updated = 0
        if not self.order_modifier or stop_price <= 0:
            return 0
        for group in groups:
            stop_row = group.get("stop_loss") or {}
            if not stop_row or not self._order_is_open(stop_row):
                continue
            old_stop = (
                self._coerce_float(stop_row.get("limit_price"), 0.0)
                or self._coerce_float(stop_row.get("sl_price"), 0.0)
            )
            if not self._stop_tightens(direction, old_stop, stop_price):
                continue
            stop_order_id = self._order_broker_id(stop_row)
            if not stop_order_id:
                continue
            result = self.order_modifier.update_stop_loss(stop_order_id, stop_price)
            if not result.get("ok"):
                logger.warning("Order-flow stop tighten failed: %s order=%s error=%s", symbol, stop_order_id, result.get("error"))
                continue
            self._upsert_harvest_order_patch(
                stop_row,
                limit_price=stop_price,
                extra_patch={
                    "reason": str(decision.get("reason") or "order_flow_tighten_stop"),
                    "order_flow_stop_tightened": True,
                    "order_flow_stop_tightened_at_ms": int(time.time() * 1000),
                    "order_flow_old_stop": old_stop,
                    "order_flow_new_stop": stop_price,
                    "order_flow_decision": dict(decision or {}),
                },
            )
            updated += 1
        if updated:
            self._order_flow_risk_action_count += updated
            self._last_order_flow_risk_action_ms = int(time.time() * 1000)
        return updated

    def _cancel_order_flow_protection(self, group: dict, reason: str) -> list[dict]:
        errors: list[dict] = []
        for role in ("take_profit", "stop_loss"):
            row = group.get(role) or {}
            if not row or not self._order_is_open(row):
                continue
            broker_id = self._order_broker_id(row)
            if not broker_id:
                continue
            result = self.order_modifier.cancel_order(broker_id) if self.order_modifier else {"ok": False, "error": "order_modifier_unavailable"}
            if not result.get("ok") and not self._cancel_result_looks_closed(result):
                errors.append({"order_id": broker_id, "role": role, "error": result.get("error") or "cancel_failed"})
                continue
            self._upsert_harvest_order_patch(
                row,
                status="Canceled",
                relation_status="closed",
                extra_patch={
                    "reason": reason,
                    "order_flow_cancelled_by": "order_flow_full_exit",
                    "order_flow_cancel_result": dict(result or {}),
                },
            )
        return errors

    def _await_order_flow_close_fill(
        self,
        *,
        close_row: dict,
        symbol: str,
        quantity: int,
    ) -> dict:
        broker_id = self._order_broker_id(close_row)
        if not broker_id:
            return {"ok": False, "error": "missing_close_order_id"}
        waiter = getattr(self.broker, "await_order_fill", None)
        timeout = max(1.0, self._get_config_float("ibkr_order_flow_close_fill_timeout_sec", 5.0))
        if callable(waiter):
            return waiter(
                broker_id,
                symbol=symbol,
                expected_quantity=quantity,
                timeout=timeout,
                poll_interval=0.2,
            )
        status = self._order_status(close_row).upper()
        if status in {"FILLED", "EXECUTED"}:
            return {"ok": True, "order_id": broker_id, "status": status, "order": dict(close_row)}
        return {"ok": False, "order_id": broker_id, "error": "close_fill_unconfirmed", "status": status}

    def _mark_order_flow_closing(
        self,
        *,
        groups: list[dict],
        quantity: int,
        result: dict,
        decision: dict,
        reason: str,
    ) -> None:
        now_ms = int(time.time() * 1000)
        for group in groups or []:
            entry = group.get("entry") or {}
            if not entry:
                continue
            self._upsert_harvest_order_patch(
                entry,
                status="Closing",
                relation_status="active",
                extra_patch={
                    "reason": reason,
                    "order_flow_closing": True,
                    "order_flow_closing_quantity": quantity,
                    "order_flow_closing_at_ms": now_ms,
                    "order_flow_close_result": dict(result or {}),
                    "order_flow_decision": dict(decision or {}),
                },
            )

    def _mark_order_flow_closed(
        self,
        *,
        groups: list[dict],
        quantity: int,
        result: dict,
        decision: dict,
    ) -> None:
        now_ms = int(time.time() * 1000)
        for group in groups or []:
            entry = group.get("entry") or {}
            if entry:
                self._upsert_harvest_order_patch(
                    entry,
                    status="Closed",
                    relation_status="closed",
                    extra_patch={
                        "reason": str(decision.get("reason") or "order_flow_full_exit"),
                        "order_flow_closing": False,
                        "order_flow_full_exit": {
                            "closed_quantity": quantity,
                            "closed_at_ms": now_ms,
                            "market_close_result": dict(result or {}),
                            "decision": dict(decision or {}),
                        },
                    },
                )
            for row in group.get("rows") or []:
                if self._order_role(row) != "close":
                    continue
                self._upsert_harvest_order_patch(
                    row,
                    status="Filled",
                    relation_status="closed",
                    extra_patch={
                        "order_flow_close_filled": True,
                        "order_flow_close_filled_at_ms": now_ms,
                        "order_flow_close_fill_result": dict(result or {}),
                    },
                )

    def _execute_order_flow_full_exit(
        self,
        *,
        symbol: str,
        broker_position: dict,
        groups: list[dict],
        decision: dict,
    ) -> dict:
        if not self.order_placer and not self.broker:
            return {"ok": False, "reason": "close_dependency_unavailable"}
        conid = self._broker_position_conid(broker_position)
        broker_qty = self._broker_position_quantity(broker_position)
        quantity = abs(int(round(broker_qty)))
        direction = "long" if broker_qty > 0 else "short"
        if quantity <= 0 or conid <= 0:
            return {"ok": False, "reason": "missing_quantity_or_conid"}
        active_close_rows = self._active_close_rows(groups)
        if active_close_rows:
            fill_result = self._await_order_flow_close_fill(
                close_row=active_close_rows[0],
                symbol=symbol,
                quantity=quantity,
            )
            if fill_result.get("ok"):
                self._mark_order_flow_closed(
                    groups=groups,
                    quantity=quantity,
                    result=fill_result,
                    decision=decision,
                )
                self._order_flow_risk_action_count += 1
                self._last_order_flow_risk_action_ms = int(time.time() * 1000)
                return {"ok": True, "closed_quantity": quantity, "result": fill_result}
            if str(fill_result.get("error") or "") == "order_terminal_before_fill":
                status = str(fill_result.get("status") or "Canceled").title()
                self._upsert_harvest_order_patch(
                    active_close_rows[0],
                    status=status,
                    relation_status="closed",
                    extra_patch={
                        "order_flow_close_terminal_before_fill": True,
                        "order_flow_close_fill_result": dict(fill_result or {}),
                    },
                )
                return {"ok": False, "reason": "close_terminal_before_fill", "result": fill_result}
            self._mark_order_flow_closing(
                groups=groups,
                quantity=quantity,
                result=fill_result,
                decision=decision,
                reason="order_flow_close_pending",
            )
            return {"ok": True, "reason": "close_pending", "result": fill_result}
        cancel_errors: list[dict] = []
        for group in groups:
            cancel_errors.extend(self._cancel_order_flow_protection(group, "order_flow_full_exit"))
        if cancel_errors:
            return {"ok": False, "reason": "protection_cancel_failed", "errors": cancel_errors}
        first_group = groups[0] if groups else {}
        first_entry = first_group.get("entry") or {}
        result = self._place_harvest_market_close(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            trade_group_id=str(first_group.get("group_key") or ""),
            entry_order_unique_id=str(first_entry.get("entry_order_unique_id") or first_entry.get("unique_id") or ""),
            source="order_flow_full_exit",
            order_type=str((decision.get("marketable_limit") or {}).get("order_type") or "marketable_limit"),
            limit_price=self._coerce_float(decision.get("limit_price"), 0.0),
            wait_for_fill=True,
            fill_timeout=max(1.0, self._get_config_float("ibkr_order_flow_close_fill_timeout_sec", 5.0)),
        )
        if not result.get("ok") and result.get("submitted"):
            self._mark_order_flow_closing(
                groups=groups,
                quantity=quantity,
                result=result,
                decision=decision,
                reason=str(result.get("error") or "order_flow_close_pending"),
            )
            self._order_flow_risk_action_count += 1
            self._last_order_flow_risk_action_ms = int(time.time() * 1000)
            logger.warning("Order-flow full exit close pending: %s qty=%s reason=%s", symbol, quantity, result.get("error"))
            return {"ok": True, "reason": "close_pending", "closed_quantity": 0, "result": result}
        if not result.get("ok"):
            return {"ok": False, "reason": "marketable_close_failed", "result": result}
        self._mark_order_flow_closed(
            groups=groups,
            quantity=quantity,
            result=result,
            decision=decision,
        )
        self._order_flow_risk_action_count += 1
        self._last_order_flow_risk_action_ms = int(time.time() * 1000)
        logger.info("Order-flow full exit: %s qty=%s reason=%s", symbol, quantity, decision.get("reason"))
        return {"ok": True, "closed_quantity": quantity, "result": result}

    def _maybe_apply_order_flow_risk_for_symbol(self, broker_position: dict) -> bool:
        manager = getattr(self, "order_flow_manager", None)
        if manager is None or not callable(getattr(manager, "position_decision", None)):
            return False
        symbol = self._broker_position_symbol(broker_position)
        if not symbol or self.is_fixed_position_symbol(symbol):
            return False
        quantity = self._broker_position_quantity(broker_position)
        if not quantity:
            return False
        rows = self._load_live_order_rows_for_symbol(symbol)
        groups = self._order_flow_groups(rows)
        if not groups:
            return False
        if self._active_close_rows(groups):
            result = self._execute_order_flow_full_exit(
                symbol=symbol,
                broker_position=broker_position,
                groups=groups,
                decision={"reason": "order_flow_close_pending_monitor"},
            )
            if not result.get("ok"):
                logger.warning("Order-flow pending close monitor failed: %s reason=%s", symbol, result.get("reason"))
            return True
        latest_group = self._latest_group(groups)
        position = self._build_harvest_position(broker_position, latest_group)
        position["symbol"] = symbol
        decision = manager.position_decision(position, order_group=latest_group)
        action = str(decision.get("action") or "").strip().lower()
        direction = "long" if quantity > 0 else "short"
        if action == "full_exit":
            result = self._execute_order_flow_full_exit(
                symbol=symbol,
                broker_position=broker_position,
                groups=groups,
                decision=decision,
            )
            if not result.get("ok"):
                logger.warning("Order-flow full exit failed: %s reason=%s", symbol, result.get("reason"))
            return bool(result.get("ok"))
        if action == "tighten_stop":
            updated = self._execute_order_flow_tighten_stop(
                symbol=symbol,
                groups=groups,
                direction=direction,
                stop_price=self._coerce_float(decision.get("stop_price"), 0.0),
                decision=decision,
            )
            return updated > 0
        return False

    def _maybe_apply_order_flow_risk(self, positions: Optional[List[Dict]] = None) -> bool:
        manager = getattr(self, "order_flow_manager", None)
        if manager is None:
            return False
        try:
            if callable(getattr(manager, "sync_positions", None)):
                manager.sync_positions(list(positions or []))
            acted = False
            if not self._get_config_bool("ibkr_order_flow_auto_exit_enabled", True) and not self._get_config_bool("ibkr_order_flow_stop_tighten_enabled", True):
                return False
            for broker_position in positions or []:
                symbol = self._broker_position_symbol(broker_position)
                if not symbol:
                    continue
                lock = self._harvest_lock_for_symbol(symbol)
                if not lock.acquire(blocking=False):
                    continue
                try:
                    acted = self._maybe_apply_order_flow_risk_for_symbol(broker_position) or acted
                finally:
                    lock.release()
            return acted
        except Exception as exc:
            self._order_flow_risk_error_count += 1
            logger.warning("Order-flow risk loop failed: %s", exc)
            return False

    def _intraday_harvest_settings(self) -> dict:
        try:
            settings = harvest_settings_from_config(self.config, self.environment)
        except Exception:
            settings = {}
        settings["enabled"] = True
        settings["live_auto_enabled"] = True
        settings["split_brackets_enabled"] = False
        return settings

    def _intraday_harvest_enabled(self) -> bool:
        settings = self._intraday_harvest_settings()
        return bool(settings.get("enabled") and settings.get("live_auto_enabled"))

    @staticmethod
    def _broker_position_symbol(position: dict | None) -> str:
        return str(
            (position or {}).get("ticker")
            or (position or {}).get("symbol")
            or (position or {}).get("contractDesc")
            or ""
        ).strip().upper()

    @staticmethod
    def _broker_position_quantity(position: dict | None) -> float:
        return OrderLifecycle._coerce_float(
            (position or {}).get("position", (position or {}).get("quantity", 0)),
            0.0,
        )

    @staticmethod
    def _broker_position_conid(position: dict | None) -> int:
        try:
            return int((position or {}).get("conid", 0) or 0)
        except Exception:
            return 0

    def _harvest_lock_for_symbol(self, symbol: str) -> threading.Lock:
        normalized = str(symbol or "").strip().upper()
        lock = self._harvest_locks.get(normalized)
        if lock is None:
            lock = threading.Lock()
            self._harvest_locks[normalized] = lock
        return lock

    @staticmethod
    def _harvest_lot(row: dict | None) -> str:
        extra = OrderLifecycle._order_extra(row)
        return str(extra.get("harvest_lot") or extra.get("intraday_harvest_lot") or "").strip().lower()

    @staticmethod
    def _is_harvest_managed_order(row: dict | None) -> bool:
        extra = OrderLifecycle._order_extra(row)
        return bool(extra.get("harvest_managed") or extra.get("intraday_harvest_managed") or extra.get("harvest_profile"))

    @staticmethod
    def _is_partial_harvest_order(row: dict | None) -> bool:
        extra = OrderLifecycle._order_extra(row)
        family = str((row or {}).get("order_family_type") or extra.get("order_family_type") or "").strip().lower()
        return bool(
            extra.get("partial_harvest_managed")
            or family == "partial_harvest_bracket"
        )

    @staticmethod
    def _order_status(row: dict | None) -> str:
        return str((row or {}).get("status") or "").strip()

    @staticmethod
    def _order_is_closed(row: dict | None) -> bool:
        return OrderLifecycle._order_status(row).upper() in {
            "FILLED",
            "EXECUTED",
            "CANCELED",
            "CANCELLED",
            "CLOSED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
        }

    @staticmethod
    def _entry_is_filled(row: dict | None) -> bool:
        status = OrderLifecycle._order_status(row).upper()
        filled_qty = OrderLifecycle._coerce_float((row or {}).get("filled_qty"), 0.0)
        return filled_qty > 0 or status in {"FILLED", "EXECUTED"}

    @staticmethod
    def _order_quantity(row: dict | None) -> int:
        return abs(int(round(OrderLifecycle._coerce_float((row or {}).get("quantity"), 0.0))))

    @staticmethod
    def _order_broker_id(row: dict | None) -> str:
        return str((row or {}).get("broker_order_id") or (row or {}).get("order_id") or "").strip()

    @staticmethod
    def _cancel_result_looks_closed(result: dict | None) -> bool:
        text = str(
            (result or {}).get("error")
            or (result or {}).get("message")
            or (result or {}).get("raw")
            or ""
        ).lower()
        return any(marker in text for marker in ("not found", "inactive", "filled", "cancelled", "canceled"))

    def _active_close_rows(self, groups: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for group in groups or []:
            for row in group.get("rows") or []:
                if self._order_role(row) == "close" and self._order_is_open(row):
                    rows.append(row)
        return rows

    @staticmethod
    def _harvest_state(row: dict | None) -> dict:
        extra = OrderLifecycle._order_extra(row)
        state = extra.get("harvest_state")
        return dict(state) if isinstance(state, dict) else {}

    def _group_harvest_rows(self, rows: list[dict]) -> list[dict]:
        groups: dict[str, dict] = {}
        for row in rows or []:
            if not self._is_harvest_managed_order(row):
                continue
            group_key = self._order_group_key(row)
            if not group_key:
                continue
            bucket = groups.setdefault(
                group_key,
                {
                    "group_key": group_key,
                    "rows": [],
                    "lot": "",
                    "entry": {},
                    "stop_loss": {},
                    "take_profit": {},
                },
            )
            bucket["rows"].append(row)
            lot = self._harvest_lot(row)
            if lot and not bucket.get("lot"):
                bucket["lot"] = lot
            role = self._order_role(row)
            if role == "entry" and not bucket.get("entry"):
                bucket["entry"] = row
            elif role == "stop_loss" and not bucket.get("stop_loss"):
                bucket["stop_loss"] = row
            elif role == "take_profit" and not bucket.get("take_profit"):
                bucket["take_profit"] = row
        return [
            bucket
            for bucket in groups.values()
            if bucket.get("entry")
            and (
                bucket.get("lot") in {"core", "tactical", "primary"}
                or self._is_partial_harvest_order(bucket.get("entry"))
            )
        ]

    def _is_partial_harvest_group(self, group: dict | None) -> bool:
        group = group or {}
        return self._is_partial_harvest_order(group.get("entry")) or self._is_partial_harvest_order(group.get("take_profit"))

    def _harvest_group_active(self, group: dict) -> bool:
        entry = group.get("entry") or {}
        entry_status = self._order_status(entry).upper()
        if not self._entry_is_filled(entry) or entry_status in {
            "CANCELED",
            "CANCELLED",
            "CLOSED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
        }:
            return False
        state = self._harvest_state(entry)
        if bool(state.get("partial_exited")):
            return False
        stop_row = group.get("stop_loss") or {}
        target_row = group.get("take_profit") or {}
        return (
            self._order_is_open(stop_row)
            or self._order_is_open(target_row)
            or bool(self._active_close_rows([group]))
        )

    def _order_filled_quantity(self, row: dict | None) -> int:
        row = row or {}
        filled = self._coerce_float(row.get("filled_qty"), 0.0)
        if filled > 0:
            return abs(int(round(filled)))
        if self._order_status(row).upper() in {"FILLED", "EXECUTED"}:
            return self._order_quantity(row)
        return 0

    def _partial_harvest_quantity(self, group: dict) -> int:
        entry = group.get("entry") or {}
        target = group.get("take_profit") or {}
        entry_extra = self._order_extra(entry)
        target_extra = self._order_extra(target)
        configured = (
            self._coerce_float(entry_extra.get("partial_tp_quantity"), 0.0)
            or self._coerce_float(target_extra.get("partial_tp_quantity"), 0.0)
            or self._coerce_float(target.get("quantity"), 0.0)
        )
        return max(0, int(round(configured)))

    def _partial_harvest_remaining_quantity(self, group: dict, broker_position: dict) -> int:
        entry = group.get("entry") or {}
        target = group.get("take_profit") or {}
        entry_qty = self._order_filled_quantity(entry) or self._order_quantity(entry)
        tp_qty = self._order_filled_quantity(target) or self._partial_harvest_quantity(group)
        broker_qty = abs(int(round(self._broker_position_quantity(broker_position))))
        remaining = max(0, entry_qty - tp_qty)
        return min(remaining, broker_qty) if broker_qty > 0 else remaining

    def _partial_harvest_handled(self, group: dict) -> bool:
        extra = self._order_extra(group.get("entry"))
        return bool(extra.get("partial_tp_handled") or extra.get("partial_harvest_stop_adjusted"))

    def _partial_harvest_reentry_done(self, group: dict) -> bool:
        extra = self._order_extra(group.get("entry"))
        return bool(extra.get("reentry_done") or extra.get("partial_harvest_reentry_done"))

    def _partial_harvest_stop_price(self, broker_position: dict, group: dict, snapshot: dict, settings: dict) -> float:
        position = self._build_harvest_position(broker_position, group)
        stop_price = suggested_stop_price(position, snapshot, settings)
        if stop_price > 0:
            return stop_price
        entry_price = self._coerce_float(position.get("entry_price"), 0.0)
        initial_stop = self._coerce_float(position.get("initial_stop_loss"), self._coerce_float(position.get("stop_price"), 0.0))
        risk = abs(entry_price - initial_stop) if entry_price > 0 and initial_stop > 0 else 0.0
        lock_r = max(0.0, self._get_config_float("intraday_harvest_breakeven_lock_r", 0.10))
        if entry_price <= 0:
            return 0.0
        if str(position.get("direction") or "").lower() == "short":
            return round(entry_price - risk * lock_r, 4)
        return round(entry_price + risk * lock_r, 4)

    def _near_partial_harvest_reentry_support(self, snapshot: dict, settings: dict) -> tuple[bool, list[str]]:
        close = self._coerce_float(snapshot.get("close"), 0.0)
        if close <= 0:
            return False, []
        support_bps = max(0.0, float(settings.get("reentry_support_bps") or self._get_config_float("intraday_harvest_reentry_support_bps", 35.0)))
        reasons: list[str] = []
        for field, reason in (("vwap", "near_vwap_support"), ("ema_fast", "near_ema_fast_support")):
            level = self._coerce_float(snapshot.get(field), 0.0)
            if level > 0 and abs(close - level) / close * 10000.0 <= support_bps:
                reasons.append(reason)
        return bool(reasons), reasons

    def _has_reentry_for_source(self, groups: list[dict], source_group_key: str) -> bool:
        source_group_key = str(source_group_key or "").strip()
        if not source_group_key:
            return False
        for group in groups or []:
            entry = group.get("entry") or {}
            extra = self._order_extra(entry)
            if str(extra.get("harvest_reentry_source_group") or "").strip() != source_group_key:
                continue
            if self._order_is_open(entry) or self._harvest_group_active(group) or self._entry_is_filled(entry):
                return True
        return False

    @staticmethod
    def _latest_group(groups: list[dict]) -> dict:
        if not groups:
            return {}
        return max(
            groups,
            key=lambda group: int(((group.get("entry") or {}).get("bar_time_ms") or 0)),
        )

    def _build_harvest_position(self, broker_position: dict, group: dict, fallback_group: dict | None = None) -> dict:
        entry_row = group.get("entry") or (fallback_group or {}).get("entry") or {}
        stop_row = group.get("stop_loss") or (fallback_group or {}).get("stop_loss") or {}
        target_row = group.get("take_profit") or (fallback_group or {}).get("take_profit") or {}
        position = self._build_live_risk_position(broker_position, entry_row, stop_row, target_row)
        quantity = self._order_quantity(entry_row)
        if quantity > 0:
            position["shares"] = quantity
            position["quantity"] = quantity
        return position

    def _upsert_harvest_order_patch(
        self,
        row: dict,
        *,
        status: str | None = None,
        relation_status: str | None = None,
        limit_price: float | None = None,
        extra_patch: dict | None = None,
    ) -> None:
        if not self.pb_client or not row:
            return
        extra = {
            **self._order_extra(row),
            **(extra_patch or {}),
            "harvest_last_update_ms": int(time.time() * 1000),
        }
        payload = {**row, "extra": extra}
        if status:
            payload["status"] = status
        if relation_status:
            payload["relation_status"] = relation_status
        if limit_price is not None:
            payload["limit_price"] = limit_price
        try:
            if hasattr(self.pb_client, "upsert_order"):
                self.pb_client.upsert_order(payload)
            elif row.get("id"):
                self.pb_client.update_record("orders", row["id"], payload)
        except Exception as exc:
            logger.debug("Harvest PB patch failed for %s: %s", row.get("unique_id"), exc)

    def _persist_harvest_state(self, row: dict, state: dict, decision: dict | None = None) -> None:
        if not row:
            return
        decision = decision or {}
        self._upsert_harvest_order_patch(
            row,
            extra_patch={
                "harvest_state": dict(state or {}),
                "harvest_last_decision": {
                    "action": str(decision.get("action") or ""),
                    "reason": str(decision.get("reason") or ""),
                    "score": int(decision.get("score") or 0),
                    "reasons": list(decision.get("reasons") or []),
                    "progress_r": decision.get("progress_r"),
                    "mfe_r": decision.get("mfe_r"),
                    "decided_at_ms": int(time.time() * 1000),
                },
            },
        )

    def _record_harvest_action(self, symbol: str) -> None:
        now_ms = int(time.time() * 1000)
        self._last_harvest_action_ms[str(symbol or "").strip().upper()] = now_ms
        self._last_intraday_harvest_action_ms = now_ms
        self._intraday_harvest_action_count += 1

    def _harvest_action_allowed(self, symbol: str) -> bool:
        cooldown_ms = int(max(0.0, self._get_config_float("intraday_harvest_action_cooldown_sec", 10.0)) * 1000)
        if cooldown_ms <= 0:
            return True
        last_ms = int(self._last_harvest_action_ms.get(str(symbol or "").strip().upper(), 0) or 0)
        return int(time.time() * 1000) - last_ms >= cooldown_ms

    def _freeze_harvest_symbol(self, symbol: str, reason: str, rows: list[dict] | None = None) -> None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return
        self._harvest_frozen_symbols[normalized] = str(reason or "harvest_safety_freeze")
        for row in rows or []:
            self._upsert_harvest_order_patch(
                row,
                extra_patch={
                    "harvest_frozen": True,
                    "harvest_freeze_reason": str(reason or "harvest_safety_freeze"),
                    "harvest_frozen_at_ms": int(time.time() * 1000),
                },
            )
        logger.warning("Intraday harvest frozen for %s: %s", normalized, reason)

    def _cancel_harvest_protection(self, group: dict, reason: str) -> list[dict]:
        errors: list[dict] = []
        for role in ("take_profit", "stop_loss"):
            row = group.get(role) or {}
            if not row or not self._order_is_open(row):
                continue
            broker_id = self._order_broker_id(row)
            if not broker_id:
                continue
            result = self.order_modifier.cancel_order(broker_id) if self.order_modifier else {"ok": False, "error": "order_modifier_unavailable"}
            if not result.get("ok") and not self._cancel_result_looks_closed(result):
                errors.append({"order_id": broker_id, "role": role, "error": result.get("error") or "cancel_failed"})
                continue
            self._upsert_harvest_order_patch(
                row,
                status="Canceled",
                relation_status="closed",
                extra_patch={
                    "reason": reason,
                    "harvest_cancelled_by": "intraday_harvest",
                    "harvest_cancel_result": dict(result or {}),
                },
            )
        return errors

    def _place_harvest_market_close(
        self,
        *,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        trade_group_id: str,
        entry_order_unique_id: str,
        source: str,
        order_type: str = "MKT",
        limit_price: float = 0.0,
        wait_for_fill: bool = False,
        fill_timeout: float = 5.0,
    ) -> dict:
        if self.order_placer and hasattr(self.order_placer, "place_market_close"):
            return self.order_placer.place_market_close(
                conid=conid,
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                use_paper=self.environment == "paper",
                trade_group_id=trade_group_id,
                entry_order_unique_id=entry_order_unique_id,
                source=source,
                order_type=order_type,
                limit_price=limit_price,
                wait_for_fill=wait_for_fill,
                fill_timeout=fill_timeout,
            )
        return self.broker.place_market_close(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            account_id=str(self.account_id or "").strip(),
            order_ref=f"{source}_{symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}",
            order_type=order_type,
            limit_price=limit_price,
            wait_for_fill=wait_for_fill,
            fill_timeout=fill_timeout,
        )

    @staticmethod
    def _stop_tightens(direction: str, old_stop: float, new_stop: float) -> bool:
        if new_stop <= 0:
            return False
        if old_stop <= 0:
            return True
        if str(direction or "").lower() == "short":
            return new_stop < old_stop - 0.005
        return new_stop > old_stop + 0.005

    def _execute_harvest_tighten_stop(
        self,
        *,
        symbol: str,
        groups: list[dict],
        direction: str,
        stop_price: float,
        reason: str,
    ) -> int:
        updated = 0
        if not self.order_modifier or stop_price <= 0:
            return 0
        for group in groups:
            stop_row = group.get("stop_loss") or {}
            if not stop_row or not self._order_is_open(stop_row):
                continue
            old_stop = (
                self._coerce_float(stop_row.get("limit_price"), 0.0)
                or self._coerce_float(stop_row.get("sl_price"), 0.0)
            )
            if not self._stop_tightens(direction, old_stop, stop_price):
                continue
            stop_order_id = self._order_broker_id(stop_row)
            if not stop_order_id:
                continue
            result = self.order_modifier.update_stop_loss(stop_order_id, stop_price)
            if not result.get("ok"):
                logger.warning("Harvest stop tighten failed: %s order=%s error=%s", symbol, stop_order_id, result.get("error"))
                continue
            self._upsert_harvest_order_patch(
                stop_row,
                limit_price=stop_price,
                extra_patch={
                    "reason": reason,
                    "harvest_stop_tightened": True,
                    "harvest_stop_tightened_at_ms": int(time.time() * 1000),
                    "harvest_old_stop": old_stop,
                    "harvest_new_stop": stop_price,
                },
            )
            updated += 1
        if updated:
            self._record_harvest_action(symbol)
        return updated

    def _modify_partial_harvest_stop(
        self,
        *,
        symbol: str,
        group: dict,
        broker_position: dict,
        snapshot: dict,
        settings: dict,
    ) -> bool:
        if not self.order_modifier:
            return False
        entry_row = group.get("entry") or {}
        target_row = group.get("take_profit") or {}
        stop_row = group.get("stop_loss") or {}
        if not stop_row or not self._order_is_open(stop_row):
            return False
        remaining_qty = self._partial_harvest_remaining_quantity(group, broker_position)
        partial_qty = self._order_filled_quantity(target_row) or self._partial_harvest_quantity(group)
        if remaining_qty <= 0 or partial_qty <= 0:
            return False
        stop_order_id = self._order_broker_id(stop_row)
        if not stop_order_id:
            return False
        stop_price = self._partial_harvest_stop_price(broker_position, group, snapshot, settings)
        if stop_price <= 0:
            return False
        modifier = getattr(self.order_modifier, "modify_order", None)
        updates = {"quantity": remaining_qty, "auxPrice": stop_price}
        result = modifier(stop_order_id, updates) if callable(modifier) else self.order_modifier.update_stop_loss(stop_order_id, stop_price)
        if not result.get("ok"):
            logger.warning("Partial harvest stop adjust failed: %s order=%s error=%s", symbol, stop_order_id, result.get("error"))
            return False
        now_ms = int(time.time() * 1000)
        state = {
            **self._harvest_state(entry_row),
            "partial_exited": True,
            "cycles": max(1, int(self._harvest_state(entry_row).get("cycles") or 0)),
            "last_action": ACTION_PARTIAL_EXIT,
            "last_action_bar_ms": int(snapshot.get("bar_time_ms") or 0),
        }
        self._upsert_harvest_order_patch(
            entry_row,
            extra_patch={
                "partial_tp_handled": True,
                "partial_harvest_stop_adjusted": True,
                "partial_tp_quantity": partial_qty,
                "partial_tp_filled_quantity": partial_qty,
                "remaining_after_partial_tp": remaining_qty,
                "breakeven_stop_price": stop_price,
                "reentry_allowed": True,
                "harvest_state": state,
                "partial_harvest_stop_adjust_result": dict(result or {}),
                "partial_tp_handled_at_ms": now_ms,
            },
        )
        self._upsert_harvest_order_patch(
            stop_row,
            limit_price=stop_price,
            extra_patch={
                "partial_harvest_stop_adjusted": True,
                "partial_harvest_remaining_quantity": remaining_qty,
                "partial_harvest_old_quantity": self._order_quantity(stop_row),
                "partial_harvest_new_stop": stop_price,
                "partial_harvest_stop_adjusted_at_ms": now_ms,
            },
        )
        self._record_harvest_action(symbol)
        logger.info("Partial harvest TP handled: %s partial=%s remaining=%s stop=%s", symbol, partial_qty, remaining_qty, stop_price)
        return True

    def _cancel_partial_harvest_target_after_stop(self, *, symbol: str, group: dict) -> bool:
        target_row = group.get("take_profit") or {}
        stop_row = group.get("stop_loss") or {}
        if self._order_status(stop_row).upper() not in {"FILLED", "EXECUTED"}:
            return False
        if not target_row or not self._order_is_open(target_row):
            return False
        broker_id = self._order_broker_id(target_row)
        if not broker_id or not self.order_modifier:
            return False
        result = self.order_modifier.cancel_order(broker_id)
        if not result.get("ok") and not self._cancel_result_looks_closed(result):
            logger.warning("Partial harvest stale target cancel failed: %s order=%s error=%s", symbol, broker_id, result.get("error"))
            return False
        self._upsert_harvest_order_patch(
            target_row,
            status="Canceled",
            relation_status="closed",
            extra_patch={
                "partial_harvest_cancelled_after_stop": True,
                "partial_harvest_cancel_result": dict(result or {}),
            },
        )
        self._record_harvest_action(symbol)
        return True

    def _maybe_reenter_partial_harvest(
        self,
        *,
        symbol: str,
        broker_position: dict,
        group: dict,
        groups: list[dict],
        snapshot: dict,
        settings: dict,
    ) -> bool:
        entry_row = group.get("entry") or {}
        extra = self._order_extra(entry_row)
        if not bool(extra.get("partial_tp_handled")) or not bool(extra.get("reentry_allowed")):
            return False
        if self._partial_harvest_reentry_done(group) or self._has_reentry_for_source(groups, str(group.get("group_key") or "")):
            return False
        near_support, reasons = self._near_partial_harvest_reentry_support(snapshot, settings)
        if not near_support:
            return False
        quantity = int(extra.get("partial_tp_filled_quantity") or extra.get("partial_tp_quantity") or self._partial_harvest_quantity(group) or 0)
        if quantity <= 0:
            return False
        current = self._coerce_float(snapshot.get("close"), 0.0)
        atr = self._coerce_float(snapshot.get("atr"), 0.0)
        direction = "long" if self._broker_position_quantity(broker_position) > 0 else "short"
        prices = build_reentry_prices(direction, current, atr, settings)
        decision = {
            "action": ACTION_REENTRY,
            "reason": ",".join(reasons) or "near_vwap_or_ema_support",
            "reasons": reasons,
            "score": len(reasons),
            "quantity_fraction": float(settings.get("tactical_fraction") or 0.30),
            "state": {
                **self._harvest_state(entry_row),
                "partial_exited": True,
                "last_action": ACTION_REENTRY,
                "last_action_bar_ms": int(snapshot.get("bar_time_ms") or 0),
            },
            "reentry_prices": prices,
            "current_price": current,
        }
        result = self._execute_harvest_reentry(
            symbol=symbol,
            broker_position=broker_position,
            source_group=group,
            quantity=quantity,
            decision=decision,
            settings=settings,
        )
        return bool(result.get("ok"))

    def _handle_partial_harvest_group(
        self,
        *,
        symbol: str,
        broker_position: dict,
        group: dict,
        groups: list[dict],
        snapshot: dict,
        settings: dict,
    ) -> bool:
        if not self._is_partial_harvest_group(group):
            return False
        if self._cancel_partial_harvest_target_after_stop(symbol=symbol, group=group):
            return True
        target_row = group.get("take_profit") or {}
        if self._order_status(target_row).upper() in {"FILLED", "EXECUTED"} and not self._partial_harvest_handled(group):
            return self._modify_partial_harvest_stop(
                symbol=symbol,
                group=group,
                broker_position=broker_position,
                snapshot=snapshot,
                settings=settings,
            )
        return self._maybe_reenter_partial_harvest(
            symbol=symbol,
            broker_position=broker_position,
            group=group,
            groups=groups,
            snapshot=snapshot,
            settings=settings,
        )

    def _execute_harvest_partial_exit(
        self,
        *,
        symbol: str,
        broker_position: dict,
        tactical_group: dict,
        core_groups: list[dict],
        decision: dict,
    ) -> dict:
        entry_row = tactical_group.get("entry") or {}
        quantity = min(self._order_quantity(entry_row), abs(int(round(self._broker_position_quantity(broker_position)))))
        conid = self._broker_position_conid(broker_position)
        direction = "long" if self._broker_position_quantity(broker_position) > 0 else "short"
        if quantity <= 0 or conid <= 0:
            return {"ok": False, "reason": "missing_quantity_or_conid"}
        cancel_errors = self._cancel_harvest_protection(tactical_group, "intraday_harvest_partial_exit")
        if cancel_errors:
            self._freeze_harvest_symbol(symbol, "partial_exit_protection_cancel_failed", tactical_group.get("rows") or [])
            return {"ok": False, "reason": "protection_cancel_failed", "errors": cancel_errors}
        result = self._place_harvest_market_close(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            trade_group_id=str(tactical_group.get("group_key") or ""),
            entry_order_unique_id=str(entry_row.get("entry_order_unique_id") or entry_row.get("unique_id") or ""),
            source="intraday_harvest_partial_exit",
        )
        if not result.get("ok"):
            self._freeze_harvest_symbol(symbol, "partial_exit_market_close_failed", tactical_group.get("rows") or [])
            return {"ok": False, "reason": "market_close_failed", "result": result}
        self._upsert_harvest_order_patch(
            entry_row,
            status="Closed",
            relation_status="closed",
            extra_patch={
                "reason": str(decision.get("reason") or "intraday_harvest_partial_exit"),
                "harvest_state": dict(decision.get("state") or {}),
                "harvest_partial_exit": {
                    "closed_quantity": quantity,
                    "closed_at_ms": int(time.time() * 1000),
                    "market_close_result": dict(result or {}),
                },
            },
        )
        stop_price = self._coerce_float(decision.get("stop_price"), 0.0)
        if stop_price > 0 and core_groups:
            self._execute_harvest_tighten_stop(
                symbol=symbol,
                groups=core_groups,
                direction=direction,
                stop_price=stop_price,
                reason="intraday_harvest_partial_exit_lock_core_stop",
            )
        self._record_harvest_action(symbol)
        logger.info("Intraday harvest partial exit: %s qty=%s reason=%s", symbol, quantity, decision.get("reason"))
        return {"ok": True, "closed_quantity": quantity, "result": result}

    def _execute_harvest_full_exit(
        self,
        *,
        symbol: str,
        broker_position: dict,
        groups: list[dict],
        decision: dict,
    ) -> dict:
        conid = self._broker_position_conid(broker_position)
        broker_qty = self._broker_position_quantity(broker_position)
        quantity = abs(int(round(broker_qty)))
        direction = "long" if broker_qty > 0 else "short"
        if quantity <= 0 or conid <= 0:
            return {"ok": False, "reason": "missing_quantity_or_conid"}
        cancel_errors: list[dict] = []
        for group in groups:
            cancel_errors.extend(self._cancel_harvest_protection(group, "intraday_harvest_full_exit"))
        if cancel_errors:
            self._freeze_harvest_symbol(symbol, "full_exit_protection_cancel_failed", [row for group in groups for row in group.get("rows") or []])
            return {"ok": False, "reason": "protection_cancel_failed", "errors": cancel_errors}
        first_group = groups[0] if groups else {}
        first_entry = first_group.get("entry") or {}
        result = self._place_harvest_market_close(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            trade_group_id=str(first_group.get("group_key") or ""),
            entry_order_unique_id=str(first_entry.get("entry_order_unique_id") or first_entry.get("unique_id") or ""),
            source="intraday_harvest_full_exit",
            order_type=str((decision.get("marketable_limit") or {}).get("order_type") or "MKT"),
            limit_price=self._coerce_float(decision.get("limit_price"), 0.0),
        )
        if not result.get("ok"):
            self._freeze_harvest_symbol(symbol, "full_exit_market_close_failed", [row for group in groups for row in group.get("rows") or []])
            return {"ok": False, "reason": "market_close_failed", "result": result}
        for group in groups:
            entry = group.get("entry") or {}
            self._upsert_harvest_order_patch(
                entry,
                status="Closed",
                relation_status="closed",
                extra_patch={
                    "reason": str(decision.get("reason") or "intraday_harvest_full_exit"),
                    "harvest_state": dict(decision.get("state") or self._harvest_state(entry)),
                    "harvest_full_exit": {
                        "closed_quantity": quantity,
                        "closed_at_ms": int(time.time() * 1000),
                        "market_close_result": dict(result or {}),
                    },
                },
            )
        self._record_harvest_action(symbol)
        logger.info("Intraday harvest full exit: %s qty=%s reason=%s", symbol, quantity, decision.get("reason"))
        return {"ok": True, "closed_quantity": quantity, "result": result}

    def _execute_harvest_reentry(
        self,
        *,
        symbol: str,
        broker_position: dict,
        source_group: dict,
        quantity: int,
        decision: dict,
        settings: dict,
    ) -> dict:
        if not self.order_placer or not hasattr(self.order_placer, "place_bracket_order"):
            return {"ok": False, "reason": "order_placer_unavailable"}
        conid = self._broker_position_conid(broker_position)
        if conid <= 0 or quantity <= 0:
            return {"ok": False, "reason": "missing_quantity_or_conid"}
        direction = "long" if self._broker_position_quantity(broker_position) > 0 else "short"
        prices = dict(decision.get("reentry_prices") or {})
        if not all(self._coerce_float(prices.get(key), 0.0) > 0 for key in ("entry", "stop_loss", "take_profit")):
            snapshot_price = self._coerce_float(decision.get("current_price"), 0.0)
            prices = build_reentry_prices(direction, snapshot_price, 0.0, settings)
        if not all(self._coerce_float(prices.get(key), 0.0) > 0 for key in ("entry", "stop_loss", "take_profit")):
            return {"ok": False, "reason": "invalid_reentry_prices"}
        entry_row = source_group.get("entry") or {}
        state = dict(decision.get("state") or self._harvest_state(entry_row))
        state["partial_exited"] = False
        state["last_action"] = ACTION_REENTRY
        state["reentered_at_ms"] = int(time.time() * 1000)
        cycles = int(state.get("cycles") or 0)
        result = self.order_placer.place_bracket_order(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=float(prices["entry"]),
            take_profit_price=float(prices["take_profit"]),
            stop_loss_price=float(prices["stop_loss"]),
            use_paper=self.environment == "paper",
            signal_id=str(entry_row.get("signal_id") or ""),
            order_extra={
                "harvest_managed": True,
                "harvest_profile": "intraday_volatility_harvest_v1",
                "harvest_lot": "tactical",
                "harvest_reentry": True,
                "harvest_reentry_source_group": str(source_group.get("group_key") or ""),
                "harvest_state": state,
            },
            order_ref_suffix=f"tactical_r{cycles}",
        )
        if not result.get("ok"):
            return {"ok": False, "reason": "reentry_submit_failed", "result": result}
        self._upsert_harvest_order_patch(
            entry_row,
            extra_patch={
                "harvest_state": {**state, "last_reentry_at_ms": int(time.time() * 1000)},
                "reentry_done": True,
                "partial_harvest_reentry_done": True,
                "harvest_last_reentry_result": {
                    "order_ids": list(result.get("order_ids") or []),
                    "bracket_group": result.get("bracket_group") or "",
                    "quantity": quantity,
                },
            },
        )
        self._record_harvest_action(symbol)
        logger.info("Intraday harvest tactical reentry: %s qty=%s reason=%s", symbol, quantity, decision.get("reason"))
        return {"ok": True, "quantity": quantity, "result": result}

    def _maybe_apply_intraday_harvest_for_symbol(self, broker_position: dict, settings: dict) -> None:
        symbol = self._broker_position_symbol(broker_position)
        if not symbol or symbol in self._fixed_position_symbols():
            return
        if symbol in self._harvest_frozen_symbols:
            return
        broker_qty = self._broker_position_quantity(broker_position)
        rows = self._load_live_order_rows_for_symbol(symbol)
        groups = self._group_harvest_rows(rows)
        if not groups:
            return
        partial_groups = [group for group in groups if self._is_partial_harvest_group(group)]
        for partial_group in partial_groups:
            if self._cancel_partial_harvest_target_after_stop(symbol=symbol, group=partial_group):
                return
        if not broker_qty:
            return
        if not self._harvest_action_allowed(symbol):
            return
        snapshot = self._load_latest_5m_risk_snapshot(symbol)
        if self._coerce_float(snapshot.get("close"), 0.0) <= 0:
            return
        for partial_group in partial_groups:
            if self._handle_partial_harvest_group(
                symbol=symbol,
                broker_position=broker_position,
                group=partial_group,
                groups=groups,
                snapshot=snapshot,
                settings=settings,
            ):
                return
        core_groups = [group for group in groups if group.get("lot") == "core" and self._harvest_group_active(group)]
        tactical_groups = [group for group in groups if group.get("lot") == "tactical" and self._harvest_group_active(group)]
        active_groups = [*core_groups, *tactical_groups]
        direction = "long" if broker_qty > 0 else "short"

        if tactical_groups:
            tactical_group = self._latest_group(tactical_groups)
            position = self._build_harvest_position(broker_position, tactical_group)
            state = self._harvest_state(tactical_group.get("entry"))
            decision = evaluate_intraday_harvest(position, snapshot, state=state, settings=settings)
            self._persist_harvest_state(tactical_group.get("entry") or {}, decision.get("state") or state, decision)
            action = str(decision.get("action") or "")
            if action == ACTION_FULL_EXIT:
                self._execute_harvest_full_exit(
                    symbol=symbol,
                    broker_position=broker_position,
                    groups=active_groups,
                    decision=decision,
                )
                return
            if action == ACTION_PARTIAL_EXIT:
                self._execute_harvest_partial_exit(
                    symbol=symbol,
                    broker_position=broker_position,
                    tactical_group=tactical_group,
                    core_groups=core_groups,
                    decision=decision,
                )
                return
            if action == ACTION_TIGHTEN_STOP:
                self._execute_harvest_tighten_stop(
                    symbol=symbol,
                    groups=active_groups,
                    direction=direction,
                    stop_price=self._coerce_float(decision.get("stop_price"), 0.0),
                    reason=str(decision.get("reason") or "intraday_harvest_tighten_stop"),
                )
                return

        core_group = self._latest_group(core_groups)
        if core_group:
            position = self._build_harvest_position(broker_position, core_group)
            state = self._harvest_state(core_group.get("entry"))
            decision = evaluate_intraday_harvest(position, snapshot, state=state, settings=settings)
            self._persist_harvest_state(core_group.get("entry") or {}, decision.get("state") or state, decision)
            action = str(decision.get("action") or "")
            if action == ACTION_FULL_EXIT:
                self._execute_harvest_full_exit(
                    symbol=symbol,
                    broker_position=broker_position,
                    groups=active_groups or core_groups,
                    decision=decision,
                )
                return
            if action == ACTION_TIGHTEN_STOP:
                self._execute_harvest_tighten_stop(
                    symbol=symbol,
                    groups=active_groups or core_groups,
                    direction=direction,
                    stop_price=self._coerce_float(decision.get("stop_price"), 0.0),
                    reason=str(decision.get("reason") or "intraday_harvest_core_tighten_stop"),
                )

        if tactical_groups or not core_group:
            return
        tactical_state_groups = [
            group
            for group in groups
            if group.get("lot") == "tactical" and bool(self._harvest_state(group.get("entry")).get("partial_exited"))
        ]
        source_group = self._latest_group(tactical_state_groups)
        if not source_group:
            return
        source_entry = source_group.get("entry") or {}
        state = self._harvest_state(source_entry)
        tactical_qty = self._order_quantity(source_entry)
        if tactical_qty <= 0:
            return
        position = self._build_harvest_position(broker_position, core_group)
        position["shares"] = tactical_qty
        position["quantity"] = tactical_qty
        decision = evaluate_intraday_harvest(
            position,
            snapshot,
            state=state,
            settings=settings,
            allow_reentry=True,
        )
        self._persist_harvest_state(source_entry, decision.get("state") or state, decision)
        if str(decision.get("action") or "") == ACTION_REENTRY:
            self._execute_harvest_reentry(
                symbol=symbol,
                broker_position=broker_position,
                source_group=source_group,
                quantity=tactical_qty,
                decision=decision,
                settings=settings,
            )

    def _maybe_apply_intraday_harvest(self, positions: Optional[List[Dict]] = None):
        if not self._intraday_harvest_enabled() or not self.pb_client or not self.order_modifier:
            return
        settings = self._intraday_harvest_settings()
        try:
            source_positions = positions if positions is not None else self.get_positions()
            for broker_position in source_positions or []:
                symbol = self._broker_position_symbol(broker_position)
                if not symbol:
                    continue
                lock = self._harvest_lock_for_symbol(symbol)
                if not lock.acquire(blocking=False):
                    continue
                try:
                    self._maybe_apply_intraday_harvest_for_symbol(broker_position, settings)
                finally:
                    lock.release()
        except Exception as exc:
            self._intraday_harvest_error_count += 1
            logger.warning("Intraday harvest loop failed: %s", exc)

    def handle_protection_incomplete(
        self,
        *,
        signal_id: str = "",
        symbol: str = "",
        direction: str = "",
        result: Optional[Dict] = None,
        order: Optional[Dict] = None,
        reason: str = "protection_incomplete",
        auto_cancel: bool = False,
    ) -> Dict:
        result = result if isinstance(result, dict) else {}
        order = order if isinstance(order, dict) else {}
        order_ids = [
            str(item or "").strip()
            for item in (result.get("order_ids") or [])
            if str(item or "").strip()
        ]
        missing_order_ids = [
            str(item or "").strip()
            for item in (result.get("missing_order_ids") or [])
            if str(item or "").strip()
        ]
        missing_protection_roles = [
            str(item or "").strip()
            for item in (result.get("missing_protection_roles") or [])
            if str(item or "").strip()
        ]
        diagnostic = {
            "status": "protection_incomplete",
            "reason": str(reason or "protection_incomplete"),
            "signal_id": str(signal_id or result.get("signal_id") or order.get("signal_id") or "").strip(),
            "symbol": str(symbol or order.get("ticker") or order.get("symbol") or "").strip().upper(),
            "direction": str(direction or result.get("direction") or "").strip().lower(),
            "protection_complete": False,
            "missing_order_ids": missing_order_ids,
            "submitted_order_ids": order_ids,
            "missing_protection_roles": missing_protection_roles,
            "protection_order_statuses": dict(result.get("protection_order_statuses") or {}),
            "protection_orders_checked": int(result.get("protection_orders_checked") or 0),
            "entry_order_id": str(
                order.get("orderId")
                or order.get("order_id")
                or (order_ids[0] if order_ids else "")
                or ""
            ).strip(),
            "bracket_group": str(result.get("bracket_group") or "").strip(),
            "auto_cancel_requested": bool(auto_cancel),
            "auto_cancel_executed": False,
            "safe_action": "diagnostic_only_no_broker_call",
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
            "cancel_recommended": True,
        }
        logger.error(
            "Bracket protection incomplete: signal_id=%s symbol=%s missing_order_ids=%s action=%s",
            diagnostic["signal_id"] or "-",
            diagnostic["symbol"] or "-",
            ",".join(missing_order_ids) or "-",
            diagnostic["safe_action"],
        )
        return diagnostic

    @property
    def is_sl_circuit_breaker(self) -> bool:
        return self._daily_sl_count >= self._consecutive_stop_loss_limit()

    @property
    def is_position_limit_reached(self) -> bool:
        position_limit = self._position_limit_max()
        return position_limit > 0 and self._daily_position_count >= position_limit

    def _lifecycle_loop(self):
        logger.info("Order lifecycle monitor started")
        while self._running:
            et_now = datetime.now(ET)
            if et_now.hour == 0 and et_now.minute < 5:
                self.daily_reset()

            eod_close_hour, eod_close_minute = self._eod_close_time()
            if (et_now.hour, et_now.minute) >= (eod_close_hour, eod_close_minute) and not self._eod_closed_today:
                logger.info("EOD close triggered at %s", et_now.strftime("%H:%M:%S"))
                self.eod_close_all()

            positions = self.get_positions()
            self._sync_positions_to_pb_from_snapshot(positions)
            order_flow_acted = self._maybe_apply_order_flow_risk(positions)
            if order_flow_acted:
                logger.info("Order-flow risk action executed; skip secondary exit policy for this cycle")
            elif self._intraday_harvest_enabled():
                self._maybe_apply_intraday_harvest(positions)
            else:
                self._maybe_update_live_exit_policy_stops(positions)
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    def _sync_positions_to_pb(self):
        self._sync_positions_to_pb_from_snapshot(self.get_positions())

    def _sync_positions_to_pb_from_snapshot(self, positions: Optional[List[Dict]] = None):
        if not self.pb_client:
            return
        try:
            for pos in positions or []:
                symbol = str(pos.get("ticker") or pos.get("contractDesc") or "").upper()
                conid = int(pos.get("conid", 0) or 0)
                if not symbol or not conid:
                    continue
                data = {
                    "symbol": symbol,
                    "conid": conid,
                    "quantity": pos.get("position", 0),
                    "avgCost": pos.get("avgCost", 0),
                    "mktPrice": pos.get("mktPrice", 0),
                    "unrealizedPnl": pos.get("unrealizedPnl", 0),
                    "account": self.account_id,
                    "us_time": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                }
                existing = self.pb_client.get_records(
                    "ibkr_positions",
                    filter=f'symbol = "{symbol}" && account = "{self.account_id}"',
                    per_page=1,
                )
                if existing:
                    self.pb_client.update_record("ibkr_positions", existing[0]["id"], data)
                else:
                    self.pb_client.create_record("ibkr_positions", data)
        except Exception as exc:
            logger.debug("Position sync failed: %s", exc)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._lifecycle_loop, daemon=True, name="order-lifecycle")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def status(self) -> dict:
        eod_close_hour, eod_close_minute = self._eod_close_time()
        return {
            "running": self._running,
            "environment": self.environment,
            "eod_closed_today": self._eod_closed_today,
            "eod_close_time": f"{eod_close_hour:02d}:{eod_close_minute:02d}",
            "eod_keep_symbols": sorted(self._keep_symbols()),
            "fixed_position_symbols": sorted(self._fixed_position_symbols()),
            "daily_sl_count": self._daily_sl_count,
            "consecutive_stop_loss_count": self._daily_sl_count,
            "consecutive_stop_loss_limit": self._consecutive_stop_loss_limit(),
            "daily_position_count": self._daily_position_count,
            "position_limit_max": self._position_limit_max(),
            "max_strategy_open_positions": self._max_strategy_open_positions(),
            "live_exit_policy_stop_update_enabled": self._live_exit_policy_updates_enabled(),
            "live_exit_policy_stop_update_count": self._live_exit_policy_update_count,
            "live_exit_policy_stop_update_error_count": self._live_exit_policy_update_error_count,
            "last_live_exit_policy_update_ms": self._last_live_exit_policy_update_ms,
            "intraday_harvest_enabled": self._intraday_harvest_enabled(),
            "intraday_harvest_action_count": self._intraday_harvest_action_count,
            "intraday_harvest_error_count": self._intraday_harvest_error_count,
            "last_intraday_harvest_action_ms": self._last_intraday_harvest_action_ms,
            "intraday_harvest_frozen_symbols": dict(self._harvest_frozen_symbols),
            "order_flow_risk_action_count": self._order_flow_risk_action_count,
            "order_flow_risk_error_count": self._order_flow_risk_error_count,
            "last_order_flow_risk_action_ms": self._last_order_flow_risk_action_ms,
            "sl_circuit_breaker": self.is_sl_circuit_breaker,
            "position_limit_reached": self.is_position_limit_reached,
        }
