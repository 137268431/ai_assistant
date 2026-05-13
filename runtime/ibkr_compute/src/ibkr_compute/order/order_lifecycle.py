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
DEFAULT_LIVE_EXIT_POLICY_UPDATE_ENABLED = True
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
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.order_modifier = order_modifier
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0
        self._last_live_exit_policy_update_ms = 0
        self._live_exit_policy_update_count = 0
        self._live_exit_policy_update_error_count = 0

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
            "sl_circuit_breaker": self.is_sl_circuit_breaker,
            "position_limit_reached": self.is_position_limit_reached,
        }
