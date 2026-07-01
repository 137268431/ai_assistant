"""
Order tracking built on top of IB Gateway socket events plus polling fallback.
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timezone
from ibkr_compute.core.time_utils import CN, ET

from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.backtest.execution_fills import normalize_execution_fill, normalize_execution_fills
from ibkr_compute.observability.prometheus import record_order_event

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
ORDER_UPDATES_MODE = str(os.environ.get("IBKR_ORDER_UPDATES_MODE", "hybrid") or "hybrid").strip().lower() or "hybrid"
POLL_INTERVAL_ACTIVE = max(5, int(os.environ.get("IBKR_ORDER_POLL_INTERVAL_ACTIVE_SEC", "5")))
POLL_INTERVAL_IDLE = max(POLL_INTERVAL_ACTIVE, int(os.environ.get("IBKR_ORDER_POLL_INTERVAL_IDLE_SEC", "15")))
ORDER_FAST_TRACK_WINDOW = max(POLL_INTERVAL_ACTIVE, int(os.environ.get("IBKR_ORDER_FAST_TRACK_SEC", "30")))
EXECUTION_FILL_SYNC_INTERVAL = max(0, int(os.environ.get("IBKR_EXECUTION_FILL_SYNC_INTERVAL_SEC", "0")))
ACCOUNT_DATA_BACKOFF_SECONDS = max(5.0, float(os.environ.get("IBKR_ACCOUNT_DATA_CIRCUIT_POLL_BACKOFF_SEC", "30") or 30))


class OrderTracker:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        on_fill: Callable = None,
        on_cancel: Callable = None,
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.on_fill = on_fill
        self.on_cancel = on_cancel
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_orders: Dict[str, dict] = {}
        self._last_poll: Optional[float] = None
        self._last_live_update: Optional[float] = None
        self._last_order_activity: Optional[float] = None
        self._live_update_count = 0
        self._poll_wakeup = threading.Event()
        self._initial_snapshot_pending = True
        self._last_execution_fill_sync_at = 0.0
        self._account_data_backoff_until = 0.0
        self._account_data_backoff_reason = ""
        self._account_data_backoff_last_warn_at = 0.0
        self._last_live_orders_fetch_unavailable = False
        self._emitted_terminal_order_keys: set[str] = set()
        self._emitted_terminal_order_key_order: list[str] = []

        add_fill_listener = getattr(self.broker, "add_execution_fill_listener", None)
        if callable(add_fill_listener):
            try:
                add_fill_listener(self.on_execution_fill_update)
            except Exception as exc:
                logger.debug("Failed to register execution fill listener: %s", exc)
        add_order_listener = getattr(self.broker, "add_order_update_listener", None)
        if callable(add_order_listener):
            try:
                add_order_listener(self.on_broker_order_update)
            except Exception as exc:
                logger.debug("Failed to register broker order listener: %s", exc)

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _get_bool_setting(self, key: str, fallback: bool) -> bool:
        if not self.config:
            return bool(fallback)
        getter = getattr(self.config, "get_bool_for_environment", None)
        if callable(getter):
            try:
                return bool(getter(key, self.environment, fallback))
            except Exception:
                return bool(fallback)
        return bool(fallback)

    def _get_mode_setting(self, key: str, fallback: str) -> str:
        if not self.config:
            return fallback
        return str(self.config.get_for_environment(key, self.environment, fallback) or fallback).strip().lower() or fallback

    def _updates_mode(self) -> str:
        mode = self._get_mode_setting("ibkr_order_updates_mode", ORDER_UPDATES_MODE)
        if mode not in {"poll", "websocket", "hybrid"}:
            return ORDER_UPDATES_MODE
        return mode

    def uses_websocket_updates(self) -> bool:
        return self._updates_mode() in {"websocket", "hybrid"}

    def _active_poll_interval(self) -> int:
        return max(5, self._get_int_setting("ibkr_order_poll_interval_active_sec", POLL_INTERVAL_ACTIVE))

    def _idle_poll_interval(self) -> int:
        return max(self._active_poll_interval(), self._get_int_setting("ibkr_order_poll_interval_idle_sec", POLL_INTERVAL_IDLE))

    def _fast_track_window(self) -> int:
        return max(self._active_poll_interval(), self._get_int_setting("ibkr_order_fast_track_sec", ORDER_FAST_TRACK_WINDOW))

    def _execution_fill_sync_interval(self) -> int:
        return max(0, self._get_int_setting("ibkr_execution_fill_sync_interval_sec", EXECUTION_FILL_SYNC_INTERVAL))

    def _use_callback_cache_during_activity(self) -> bool:
        return self._get_bool_setting("ibkr_order_tracker_callback_cache_during_activity", True)

    def _active_reservation_count(self) -> int:
        getter = getattr(self.pb_client, "get_state", None)
        if not callable(getter):
            return 0
        try:
            record = getter(
                "buying_power_reservations:v1",
                self.environment,
                datetime.now(ET).strftime("%Y-%m-%d"),
            )
        except Exception:
            return 0
        data = (record or {}).get("data") if isinstance(record, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        reservations = data.get("reservations") if isinstance(data, dict) else []
        if not isinstance(reservations, list):
            return 0
        count = 0
        now = time.time()
        for item in reservations:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "active").strip().lower() != "active":
                continue
            expires_at = str(item.get("expires_at") or "").strip()
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp() <= now:
                        continue
                except Exception:
                    pass
            count += 1
        return count

    def _skip_live_order_fetch_during_order_pressure(self) -> bool:
        if not self._get_bool_setting("ibkr_order_tracker_skip_live_fetch_during_order_pressure", True):
            return False
        return self._active_reservation_count() > 0

    def _mark_order_activity(self):
        self._last_order_activity = time.time()

    @staticmethod
    def _account_data_error_text(exc: Exception | str) -> str:
        return str(exc or "")

    @classmethod
    def _is_account_data_unavailable_error(cls, exc: Exception | str) -> bool:
        text = cls._account_data_error_text(exc)
        lowered = text.lower()
        return (
            "account_data_circuit_open" in lowered
            or "account_data_request_queue_timeout" in lowered
            or "positions_timeout" in lowered
            or "open_orders_timeout" in lowered
            or "open_orders_all_timeout" in lowered
            or "executions_timeout" in lowered
            or "account_summary_timeout" in lowered
            or "account_updates_timeout" in lowered
        )

    @classmethod
    def _retry_after_seconds(cls, exc: Exception | str, default: float = ACCOUNT_DATA_BACKOFF_SECONDS) -> float:
        text = cls._account_data_error_text(exc)
        match = re.search(r"retry_after_s=([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            try:
                return max(5.0, min(120.0, float(match.group(1))))
            except Exception:
                return float(default)
        return float(default)

    def _account_data_backoff_remaining(self) -> float:
        return max(0.0, float(self._account_data_backoff_until or 0.0) - time.time())

    def _account_data_backoff_applies(self, operation: str) -> bool:
        reason = str(self._account_data_backoff_reason or "").strip().lower()
        if not reason:
            return True
        if "account_data_circuit_open" in reason:
            return True
        normalized = str(operation or "").strip().lower()
        if "account_data_request_queue_timeout" in reason:
            if normalized == "live_orders":
                return "open_orders" in reason
            if normalized == "recent_execution_fills":
                return "executions" in reason
            return True
        if normalized == "live_orders":
            return "open_orders" in reason
        if normalized == "recent_execution_fills":
            return "executions" in reason
        return True

    def _mark_account_data_backoff(self, exc: Exception | str, *, operation: str) -> None:
        reason = self._account_data_error_text(exc) or "account_data_unavailable"
        retry_after = self._retry_after_seconds(reason)
        self._account_data_backoff_until = max(self._account_data_backoff_until, time.time() + retry_after)
        self._account_data_backoff_reason = reason
        now = time.time()
        if now - float(self._account_data_backoff_last_warn_at or 0.0) >= 15.0:
            logger.warning(
                "Skipping broker account/order polling during account-data backoff: operation=%s retry_after_s=%.1f reason=%s",
                operation,
                self._account_data_backoff_remaining(),
                reason,
            )
            self._account_data_backoff_last_warn_at = now

    def _should_skip_account_data_fetch(self, *, operation: str) -> bool:
        remaining = self._account_data_backoff_remaining()
        if remaining <= 0:
            return False
        if not self._account_data_backoff_applies(operation):
            return False
        now = time.time()
        if now - float(self._account_data_backoff_last_warn_at or 0.0) >= 15.0:
            logger.warning(
                "Skipping broker account/order polling during account-data backoff: operation=%s retry_after_s=%.1f reason=%s",
                operation,
                remaining,
                self._account_data_backoff_reason or "account_data_backoff",
            )
            self._account_data_backoff_last_warn_at = now
        return True

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return default if number != number or abs(number) >= 1e100 else number

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _ensure_object(value: Any) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @classmethod
    def _first_positive_float(cls, *values: Any) -> float:
        for value in values:
            number = cls._to_float(value, 0.0)
            if number > 0:
                return number
        return 0.0

    @classmethod
    def _existing_planned_limit_price(
        cls,
        *,
        role: str,
        existing_order: dict | None,
        existing_extra: dict,
        stop_trigger_price: float = 0.0,
    ) -> float:
        existing = existing_order or {}
        normalized_role = str(role or "").strip().lower()
        common = (
            existing.get("limit_price"),
            existing_extra.get("limit_price"),
        )
        if normalized_role == "entry":
            return cls._first_positive_float(
                *common,
                existing_extra.get("submitted_entry_limit_price"),
                existing_extra.get("submitted_limit_price"),
                existing_extra.get("submitted_limit_cap_price"),
                existing_extra.get("bounded_limit_price"),
                existing_extra.get("entry_limit_price"),
                existing_extra.get("order_flow_entry_limit_price"),
            )
        if normalized_role in {"take_profit", "repair_tp"}:
            return cls._first_positive_float(
                *common,
                existing.get("tp_price"),
                existing_extra.get("tp_price"),
                existing_extra.get("take_profit"),
                existing_extra.get("initial_take_profit"),
            )
        if normalized_role in {"stop_loss", "repair_sl"}:
            return cls._first_positive_float(
                stop_trigger_price,
                *common,
                existing.get("sl_price"),
                existing_extra.get("sl_price"),
                existing_extra.get("stop_loss"),
                existing_extra.get("stop_price"),
                existing_extra.get("auxPrice"),
                existing_extra.get("aux_price"),
            )
        return 0.0

    @classmethod
    def _parse_ibkr_execution_time_ms(cls, value: Any) -> int:
        text = cls._normalize_text(value)
        if not text:
            return 0
        compact = " ".join(text.split())
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
            try:
                return int(datetime.strptime(compact, fmt).replace(tzinfo=ET).timestamp() * 1000)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ET)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return 0

    @staticmethod
    def _time_fields_from_ms(timestamp_ms: int) -> dict:
        ms = int(timestamp_ms or 0)
        if ms <= 0:
            ms = int(time.time() * 1000)
        utc_dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return {
            "us_time": utc_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S"),
            "cn_time": utc_dt.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
            "bar_time_ms": ms,
        }

    @classmethod
    def _normalize_trade_direction(cls, value: Any) -> str:
        normalized = cls._normalize_text(value).lower()
        return normalized if normalized in {"long", "short"} else ""

    @classmethod
    def _direction_from_identifier(cls, *values: Any, allow_signal_suffix: bool = False) -> str:
        for value in values:
            text = cls._normalize_text(value).lower()
            if not text:
                continue
            normalized = text
            for separator in ("-", ":", "/", ".", " "):
                normalized = normalized.replace(separator, "_")
            tokens = [token for token in normalized.split("_") if token]
            direction_tokens = [(index, token) for index, token in enumerate(tokens) if token in {"long", "short"}]
            if direction_tokens:
                return direction_tokens[-1][1]
            if allow_signal_suffix and tokens:
                if tokens[-1] == "l":
                    return "long"
                if tokens[-1] == "s":
                    return "short"
        return ""

    @classmethod
    def _record_identifier_direction(cls, record: Optional[Dict[str, Any]]) -> str:
        if not isinstance(record, dict):
            return ""
        return cls._direction_from_identifier(
            record.get("trade_group_id"),
            record.get("entry_order_unique_id"),
            record.get("unique_id"),
        ) or cls._direction_from_identifier(record.get("signal_id"), allow_signal_suffix=True)

    @classmethod
    def _record_explicit_direction(cls, record: Optional[Dict[str, Any]]) -> str:
        if not isinstance(record, dict):
            return ""
        return cls._normalize_trade_direction(record.get("position_side")) or cls._normalize_trade_direction(record.get("direction"))

    @classmethod
    def _position_side_from_close_order_side(cls, side: Any) -> str:
        normalized_side = cls._normalize_text(side).upper()
        if normalized_side == "SELL":
            return "long"
        if normalized_side == "BUY":
            return "short"
        return ""

    @classmethod
    def _looks_like_close_order_ref(cls, value: Any) -> bool:
        return cls._normalize_text(value).lower().startswith(("close_", "manual_close_", "market_close_"))

    def _eod_close_time_tuple(self) -> tuple[int, int]:
        raw = "15:55"
        getter = getattr(self.config, "get_for_environment", None)
        if callable(getter):
            try:
                raw = str(getter("eod_close_time", self.environment, raw) or raw)
            except Exception:
                raw = "15:55"
        try:
            hour_text, minute_text = raw.strip().split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return (15, 55)

    def _looks_like_eod_close_ref(self, value: Any) -> bool:
        text = self._normalize_text(value)
        match = re.match(r"^close_[A-Z0-9.]+_\d{8}_(\d{6})$", text, flags=re.IGNORECASE)
        if not match:
            return False
        time_text = match.group(1)
        try:
            order_hour = int(time_text[:2])
            order_minute = int(time_text[2:4])
        except Exception:
            return False
        eod_hour, eod_minute = self._eod_close_time_tuple()
        return (order_hour, order_minute) >= (eod_hour, eod_minute)

    @classmethod
    def _normalize_pb_order_role(cls, value: Any) -> str:
        normalized = cls._normalize_text(value).lower()
        if normalized in {"tp", "takeprofit", "take_profit", "profit_target", "target"}:
            return "take_profit"
        if normalized in {"sl", "stop", "stoploss", "stop_loss"}:
            return "stop_loss"
        if normalized in {"close", "manual_close", "market_close", "close_order", "reverse_close"}:
            return "close"
        return normalized

    @classmethod
    def _pb_order_row_role(cls, row: Optional[Dict[str, Any]]) -> str:
        if not isinstance(row, dict):
            return ""
        role = cls._normalize_pb_order_role(row.get("role") or row.get("order_role") or row.get("leg_role"))
        if role:
            return role
        order_type_role = cls._normalize_pb_order_role(row.get("order_type") or row.get("orderType"))
        if order_type_role in {"entry", "take_profit", "stop_loss", "close"}:
            return order_type_role
        unique_id = cls._normalize_text(row.get("unique_id")).lower()
        if unique_id.startswith("entry_"):
            return "entry"
        if unique_id.startswith("tp_"):
            return "take_profit"
        if unique_id.startswith("sl_"):
            return "stop_loss"
        if cls._looks_like_close_order_ref(unique_id):
            return "close"
        return ""

    @classmethod
    def _pb_order_matches_identity_hints(cls, row: Optional[Dict[str, Any]], *, symbol: str = "", role: str = "") -> bool:
        if not isinstance(row, dict):
            return False
        normalized_symbol = cls._normalize_text(symbol).upper()
        row_symbol = cls._normalize_text(row.get("symbol")).upper()
        if normalized_symbol and row_symbol and row_symbol != normalized_symbol:
            return False
        normalized_role = cls._normalize_pb_order_role(role)
        if normalized_role:
            row_role = cls._pb_order_row_role(row)
            if row_role != normalized_role:
                return False
        return True

    @classmethod
    def _pb_order_matches_client_order_id(cls, row: Optional[Dict[str, Any]], client_order_id: str) -> bool:
        if not isinstance(row, dict):
            return False
        normalized = cls._normalize_text(client_order_id)
        if not normalized:
            return False
        extra = cls._ensure_object(row.get("extra"))
        candidates = (
            row.get("unique_id"),
            row.get("client_order_id"),
            row.get("order_ref"),
            row.get("orderRef"),
            row.get("cOID"),
            row.get("coid"),
            extra.get("unique_id"),
            extra.get("client_order_id"),
            extra.get("order_ref"),
            extra.get("orderRef"),
            extra.get("cOID"),
            extra.get("coid"),
        )
        return any(cls._normalize_text(candidate) == normalized for candidate in candidates)

    @classmethod
    def _pb_order_row_is_terminal(cls, row: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(row, dict):
            return False
        relation_status = cls._normalize_text(row.get("relation_status")).lower()
        status = cls._normalize_text(row.get("status") or row.get("order_status") or row.get("orderStatus")).upper()
        return relation_status in {"closed", "canceled", "cancelled"} or status in {
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }

    @classmethod
    def _pb_rows_have_identity_conflict(cls, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        comparisons = (
            (cls._normalize_text(left.get("unique_id")), cls._normalize_text(right.get("unique_id"))),
            (cls._normalize_text(left.get("trade_group_id")), cls._normalize_text(right.get("trade_group_id"))),
            (cls._normalize_text(left.get("entry_order_unique_id")), cls._normalize_text(right.get("entry_order_unique_id"))),
            (cls._normalize_text(left.get("symbol")).upper(), cls._normalize_text(right.get("symbol")).upper()),
            (cls._normalize_text(left.get("conid")), cls._normalize_text(right.get("conid"))),
        )
        for left_value, right_value in comparisons:
            if left_value and right_value and left_value != right_value:
                return True
        return False

    @classmethod
    def _candidate_has_terminal_identity_conflict(
        cls,
        candidate: Dict[str, Any],
        matches: List[Dict[str, Any]],
    ) -> bool:
        if cls._pb_order_row_role(candidate) == "close":
            return False
        candidate_id = cls._normalize_text(candidate.get("broker_order_id") or candidate.get("order_id"))
        if not candidate_id:
            return False
        for row in matches or []:
            if row is candidate:
                continue
            row_id = cls._normalize_text(row.get("broker_order_id") or row.get("order_id"))
            if row_id != candidate_id or not cls._pb_order_row_is_terminal(row):
                continue
            if cls._pb_rows_have_identity_conflict(candidate, row):
                return True
        return False

    @classmethod
    def _infer_position_side(
        cls,
        *,
        side: Any,
        role: Any,
        parent_id: Any,
        coid: Any,
        trade_group_id: Any,
        entry_order_unique_id: Any,
        signal_id: Any,
        existing_order: Optional[Dict[str, Any]],
        parent_record: Optional[Dict[str, Any]],
    ) -> str:
        identity_direction = cls._direction_from_identifier(
            coid,
            trade_group_id,
            entry_order_unique_id,
        ) or cls._direction_from_identifier(signal_id, allow_signal_suffix=True)
        if identity_direction:
            return identity_direction

        for record in (existing_order, parent_record):
            identity_direction = cls._record_identifier_direction(record)
            if identity_direction:
                return identity_direction

        parent_direction = cls._record_explicit_direction(parent_record)
        if parent_direction:
            return parent_direction

        existing_direction = cls._record_explicit_direction(existing_order)
        if existing_direction:
            return existing_direction

        normalized_side = cls._normalize_text(side).upper()
        normalized_role = cls._normalize_text(role).lower()
        is_exit_leg = bool(cls._normalize_text(parent_id)) or normalized_role in {"take_profit", "stop_loss"}
        if normalized_side == "BUY":
            return "short" if is_exit_leg else "long"
        if normalized_side == "SELL":
            return "long" if is_exit_leg else "short"
        return ""

    @staticmethod
    def _extract_live_symbol(order: Dict) -> str:
        return str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip().upper()

    @staticmethod
    def _extract_live_side(order: Dict) -> str:
        return str(order.get("side") or "").strip().upper()

    @staticmethod
    def _extract_live_order_type(order: Dict) -> str:
        return str(order.get("orderType") or order.get("order_type") or "").strip().upper()

    @staticmethod
    def _extract_live_parent_id(order: Dict) -> str:
        return str(order.get("parentId") or order.get("parent_id") or "").strip()

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
    def _is_terminal_order_status(status: Any) -> bool:
        return str(status or "").strip().upper() in {
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }

    def _has_broker_live_identity(self, order: Dict) -> bool:
        if not isinstance(order, dict):
            return False
        if self._extract_live_symbol(order):
            return True
        if self._normalize_text(
            order.get("cOID")
            or order.get("coid")
            or order.get("client_order_id")
            or order.get("order_ref")
            or order.get("orderRef")
        ):
            return True
        if self._extract_live_side(order) and self._extract_live_order_type(order):
            return True
        quantity = self._to_float(
            order.get("totalSize")
            if order.get("totalSize") not in (None, "")
            else order.get("quantity"),
            0.0,
        )
        return quantity > 0 and bool(self._extract_live_side(order))

    def _should_skip_unidentified_live_order(self, order_id: str, order: Dict, sources: List[str]) -> bool:
        # PB-seeded orders can be enriched later by account snapshot matching. Tracker-only
        # status patches without contract/client identity are stale-prone and should not
        # appear as broker-confirmed live orders.
        if "pb" in set(sources or []):
            return False
        if self._has_broker_live_identity(order):
            return False
        logger.debug(
            "Skipping unidentified live open order: order_id=%s status=%s sources=%s payload_keys=%s",
            order_id,
            self._extract_order_status(order),
            sources,
            sorted((order or {}).keys()),
        )
        return True

    def get_live_orders(
        self,
        *,
        retries: int = 1,
        retry_delay: float = 0.5,
        force: bool = False,
        include_all: bool = False,
    ) -> List[Dict]:
        started = time.perf_counter()
        if self._use_callback_cache_during_activity():
            last_activity = float(self._last_order_activity or 0.0)
            if last_activity > 0 and (time.time() - last_activity) <= self._fast_track_window():
                cached = self.get_cached_live_orders(include_all=include_all)
                if cached:
                    self._last_live_orders_fetch_unavailable = False
                    record_order_event(
                        environment=self.environment,
                        operation="tracker_live_orders_fetch",
                        order_family_type="all" if include_all else "open",
                        result="ok",
                        reason_code="callback_cache_recent_activity",
                        duration_s=time.perf_counter() - started,
                    )
                    return cached
        if self._skip_live_order_fetch_during_order_pressure():
            cached = self.get_cached_live_orders(include_all=include_all)
            if cached:
                self._last_live_orders_fetch_unavailable = False
                record_order_event(
                    environment=self.environment,
                    operation="tracker_live_orders_fetch",
                    order_family_type="all" if include_all else "open",
                    result="ok",
                    reason_code="callback_cache_order_pressure",
                    duration_s=time.perf_counter() - started,
                )
                return cached
            self._last_live_orders_fetch_unavailable = True
            record_order_event(
                environment=self.environment,
                operation="tracker_live_orders_fetch",
                order_family_type="all" if include_all else "open",
                result="skipped",
                reason_code="order_pressure_reservations",
                duration_s=time.perf_counter() - started,
            )
            return []
        if self._should_skip_account_data_fetch(operation="live_orders"):
            self._last_live_orders_fetch_unavailable = True
            record_order_event(
                environment=self.environment,
                operation="tracker_live_orders_fetch",
                order_family_type="all" if include_all else "open",
                result="skipped",
                reason_code="account_data_backoff",
                duration_s=time.perf_counter() - started,
            )
            return []
        self._last_live_orders_fetch_unavailable = False
        attempts = max(1, int(retries or 1))
        for attempt in range(attempts):
            try:
                try:
                    orders = list(self.broker.list_open_orders(include_all=include_all, force=force) or [])
                except TypeError:
                    orders = list(self.broker.list_open_orders(include_all=include_all) or [])
                if orders or attempt + 1 >= attempts:
                    record_order_event(
                        environment=self.environment,
                        operation="tracker_live_orders_fetch",
                        order_family_type="all" if include_all else "open",
                        result="ok",
                        reason_code="orders_found" if orders else "empty",
                        duration_s=time.perf_counter() - started,
                    )
                    return orders
            except Exception as exc:
                if self._is_account_data_unavailable_error(exc):
                    self._mark_account_data_backoff(exc, operation="live_orders")
                    self._last_live_orders_fetch_unavailable = True
                    record_order_event(
                        environment=self.environment,
                        operation="tracker_live_orders_fetch",
                        order_family_type="all" if include_all else "open",
                        result="skipped",
                        reason_code="account_data_unavailable",
                        duration_s=time.perf_counter() - started,
                    )
                    return []
                logger.warning("Failed to get live orders: %s", exc)
                if attempt + 1 >= attempts:
                    record_order_event(
                        environment=self.environment,
                        operation="tracker_live_orders_fetch",
                        order_family_type="all" if include_all else "open",
                        result="error",
                        reason_code=exc.__class__.__name__,
                        duration_s=time.perf_counter() - started,
                    )
                    return []
            time.sleep(max(0.0, float(retry_delay or 0.0)))
        record_order_event(
            environment=self.environment,
            operation="tracker_live_orders_fetch",
            order_family_type="all" if include_all else "open",
            result="ok",
            reason_code="empty",
            duration_s=time.perf_counter() - started,
        )
        return []

    def get_cached_live_orders(self, *, include_all: bool = False) -> List[Dict]:
        getter = getattr(self.broker, "list_cached_open_orders", None)
        if not callable(getter):
            return []
        try:
            rows = list(getter(include_all=include_all) or [])
        except TypeError:
            rows = list(getter() or [])
        except Exception as exc:
            logger.debug("Cached live orders fetch failed: %s", exc)
            return []

        results: List[Dict] = []
        seen: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            order_id = self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
            if not order_id or order_id in seen:
                continue
            merged = self._merge_order_with_known_state(order_id, item)
            status = self._extract_order_status(merged)
            if include_all or self._is_open_order_status(status):
                results.append(merged)
                seen.add(order_id)
        return results

    def _build_fill_history_index(self, fills: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        if fills is None:
            try:
                fills = list(self.broker.list_recent_fills() or [])
            except Exception as exc:
                logger.debug("Recent fills fetch failed: %s", exc)
                return index

        for fill in fills:
            order_id = self._normalize_text(fill.get("orderId"))
            if not order_id:
                continue
            bucket = index.setdefault(order_id, {
                "orderId": order_id,
                "id": order_id,
                "conid": int(fill.get("conid", 0) or 0),
                "ticker": self._normalize_text(fill.get("ticker")).upper(),
                "side": self._normalize_text(fill.get("side")).upper(),
                "status": "FILLED",
                "orderType": self._normalize_text(fill.get("orderType")).upper(),
                "filledQuantity": 0.0,
                "remainingQuantity": 0.0,
                "avgPrice": 0.0,
                "price": 0.0,
                "commission": 0.0,
                "submittedTime": self._normalize_text(fill.get("time")),
                "lastExecutionTime": self._normalize_text(fill.get("time")),
                "_fill_value": 0.0,
            })
            shares = self._to_float(fill.get("shares"), 0.0)
            price = self._to_float(fill.get("price"), 0.0)
            commission = self._to_float(fill.get("commission"), 0.0)
            bucket["filledQuantity"] += shares
            bucket["_fill_value"] += shares * price
            bucket["commission"] += commission
            if price > 0:
                bucket["price"] = price
            if self._normalize_text(fill.get("time")):
                bucket["lastExecutionTime"] = self._normalize_text(fill.get("time"))

        for payload in index.values():
            filled_qty = self._to_float(payload.get("filledQuantity"), 0.0)
            payload["avgPrice"] = round(payload.get("_fill_value", 0.0) / filled_qty, 6) if filled_qty > 0 else 0.0
            payload["totalSize"] = filled_qty
            payload.pop("_fill_value", None)
        return index

    def get_order_status(self, order_id: str) -> Dict:
        normalized = self._normalize_text(order_id)
        if not normalized:
            return {}

        snapshot = self.broker.get_order_snapshot(normalized)
        if snapshot:
            fill_payload = dict(self._build_fill_history_index().get(normalized) or {})
            if fill_payload:
                merged = dict(snapshot)
                merged.update({key: value for key, value in fill_payload.items() if value not in (None, "")})
                return merged
            return snapshot

        for order in self.get_live_orders():
            live_order_id = self._normalize_text(order.get("orderId") or order.get("order_id") or order.get("id"))
            if live_order_id == normalized:
                return dict(order)

        return dict(self._build_fill_history_index().get(normalized) or {})

    def get_orders_by_ids(self, broker_order_ids: List[str]) -> List[Dict]:
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}
        results = []
        for oid in broker_order_ids or []:
            payload = self.get_order_status(str(oid or "").strip())
            if not payload:
                continue
            status = self._extract_order_status(payload)
            if status and status.upper() not in closed_statuses:
                results.append(payload)
        return results

    def _build_live_seed_source_map(self, pb_seed_ids: Optional[List[str]] = None) -> Dict[str, List[str]]:
        seed_sources: Dict[str, List[str]] = {}
        for tracked in list(self._known_orders.values()):
            order_id = self._normalize_text(tracked.get("orderId") or tracked.get("order_id"))
            if not order_id:
                continue
            status = self._extract_order_status(tracked)
            if not self._is_open_order_status(status):
                continue
            seed_sources.setdefault(order_id, [])
            if "tracker" not in seed_sources[order_id]:
                seed_sources[order_id].append("tracker")

        for oid in pb_seed_ids or []:
            order_id = self._normalize_text(oid)
            if not order_id:
                continue
            seed_sources.setdefault(order_id, [])
            if "pb" not in seed_sources[order_id]:
                seed_sources[order_id].append("pb")
        return seed_sources

    def _merge_order_with_known_state(self, order_id: str, payload: Optional[Dict]) -> Dict[str, Any]:
        merged = dict(self._known_orders.get(order_id) or {})
        if isinstance(payload, dict):
            merged.update(payload)
        if order_id:
            merged["orderId"] = order_id
        return merged

    def get_complete_live_open_orders(
        self,
        *,
        pb_seed_ids: Optional[List[str]] = None,
        bulk_orders: Optional[List[Dict]] = None,
        retries: int = 1,
        retry_delay: float = 0.5,
        force: bool = False,
    ) -> Dict[str, Any]:
        seed_sources = self._build_live_seed_source_map(pb_seed_ids)
        bulk_list = bulk_orders if bulk_orders is not None else self.get_live_orders(
            retries=retries,
            retry_delay=retry_delay,
            force=force,
        )
        cached_broker_orders = self.get_cached_live_orders(include_all=True)
        if cached_broker_orders:
            bulk_by_id: Dict[str, Dict[str, Any]] = {}
            for item in bulk_list or []:
                if not isinstance(item, dict):
                    continue
                order_id = self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                if order_id:
                    bulk_by_id[order_id] = dict(item)
            for item in cached_broker_orders:
                order_id = self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                if not order_id:
                    continue
                existing = dict(bulk_by_id.get(order_id) or {})
                existing.update(item)
                bulk_by_id[order_id] = existing
            bulk_list = list(bulk_by_id.values())
        if not bulk_list and seed_sources:
            try:
                bulk_list = self.get_live_orders(
                    retries=1,
                    retry_delay=retry_delay,
                    force=force,
                    include_all=True,
                )
            except Exception as exc:
                logger.debug("All-open-orders recovery fallback failed: %s", exc)
        existing_ids = set()
        open_orders: List[Dict[str, Any]] = []
        skipped_unidentified_order_ids: List[str] = []

        for item in bulk_list or []:
            if not isinstance(item, dict):
                continue
            order_id = self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
            if not order_id or order_id in existing_ids:
                continue
            merged = self._merge_order_with_known_state(order_id, item)
            status = self._extract_order_status(merged)
            if not self._is_open_order_status(status):
                continue
            sources = list(seed_sources.get(order_id) or [])
            if self._should_skip_unidentified_live_order(order_id, merged, sources):
                skipped_unidentified_order_ids.append(order_id)
                existing_ids.add(order_id)
                continue
            merged["_recovery_source"] = "bulk"
            merged["_seed_sources"] = sources
            open_orders.append(merged)
            existing_ids.add(order_id)

        recovered_order_ids: List[str] = []
        resolved_closed_ids: List[str] = []
        unresolved_order_ids: List[str] = []

        for order_id, sources in seed_sources.items():
            if order_id in existing_ids:
                continue
            payload = self.get_order_status(order_id)
            if payload and isinstance(payload, dict):
                merged = self._merge_order_with_known_state(order_id, payload)
                status = self._extract_order_status(merged)
                if status and self._is_open_order_status(status):
                    if self._should_skip_unidentified_live_order(order_id, merged, list(sources or [])):
                        skipped_unidentified_order_ids.append(order_id)
                        existing_ids.add(order_id)
                        continue
                    merged["_recovery_source"] = "status_recovered"
                    merged["_seed_sources"] = list(sources or [])
                    open_orders.append(merged)
                    existing_ids.add(order_id)
                    recovered_order_ids.append(order_id)
                    continue
                if status:
                    resolved_closed_ids.append(order_id)
                    continue
            unresolved_order_ids.append(order_id)

        tracker_seed_count = len([order_id for order_id, sources in seed_sources.items() if "tracker" in sources])
        pb_seed_count = len([order_id for order_id, sources in seed_sources.items() if "pb" in sources])
        coverage_state = "degraded" if unresolved_order_ids else ("recovered" if recovered_order_ids else "complete")

        return {
            "orders": open_orders,
            "coverage": {
                "coverage_state": coverage_state,
                "bulk_open_count": len([item for item in open_orders if str(item.get("_recovery_source") or "") == "bulk"]),
                "recovered_from_status_count": len(recovered_order_ids),
                "tracker_seed_count": tracker_seed_count,
                "pb_seed_count": pb_seed_count,
                "unresolved_seed_count": len(unresolved_order_ids),
                "unresolved_order_ids": unresolved_order_ids,
            },
            "diagnostics": {
                "seed_sources": seed_sources,
                "recovered_order_ids": recovered_order_ids,
                "resolved_closed_order_ids": resolved_closed_ids,
                "skipped_unidentified_order_ids": skipped_unidentified_order_ids,
                "bulk_order_ids": [
                    self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                    for item in bulk_list or []
                    if isinstance(item, dict) and self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                ],
                "cached_order_ids": [
                    self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                    for item in cached_broker_orders or []
                    if isinstance(item, dict) and self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                ],
            },
        }

    def sync_live_orders_snapshot(self, orders: Optional[List[Dict]] = None) -> int:
        live_orders = orders if orders is not None else self.get_live_orders()
        synced = 0
        for order in live_orders or []:
            order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
            if not order_id:
                continue
            try:
                self._sync_to_pb(order)
                synced += 1
            except Exception as exc:
                logger.debug("sync_live_orders_snapshot failed for %s: %s", order_id, exc)
        return synced

    def find_duplicate_open_entry(
        self,
        *,
        symbol: str,
        direction: str,
        quantity: Any,
        entry_price: Any,
        entry_order_type: str = "LMT",
        price_tolerance: float = 0.02,
    ) -> Optional[Dict[str, Any]]:
        normalized_symbol = self._normalize_text(symbol).upper()
        normalized_direction = self._normalize_text(direction).lower()
        expected_side = "BUY" if normalized_direction == "long" else "SELL" if normalized_direction == "short" else ""
        expected_qty = round(self._to_float(quantity, 0.0), 8)
        expected_price = self._to_float(entry_price, 0.0)
        normalized_order_type = self._normalize_text(entry_order_type).upper() or "LMT"

        if not normalized_symbol or not expected_side or expected_qty <= 0:
            return None

        live_orders = self.get_cached_live_orders()
        live_fetch_enabled = self._get_bool_setting(
            "ibkr_duplicate_order_live_fetch_enabled",
            self.environment == "live",
        )
        if not live_orders and live_fetch_enabled:
            live_orders = self.get_live_orders(retries=1, retry_delay=0.0)
        for order in live_orders:
            if self._extract_live_parent_id(order):
                continue
            live_status = self._extract_order_status(order)
            if not self._is_open_order_status(live_status):
                continue
            if self._extract_live_symbol(order) != normalized_symbol:
                continue
            if self._extract_live_side(order) != expected_side:
                continue

            live_qty = round(
                self._to_float(
                    order.get("totalSize") if order.get("totalSize") is not None else order.get("quantity"),
                    0.0,
                ),
                8,
            )
            if abs(live_qty - expected_qty) > 1e-8:
                continue

            live_type = self._extract_live_order_type(order)
            if normalized_order_type == "MKT":
                if live_type and live_type != "MKT":
                    continue
                return order

            if live_type and live_type not in {"LMT", "LIMIT"}:
                continue

            live_price = self._to_float(order.get("price"), 0.0)
            if expected_price <= 0 or live_price <= 0:
                continue
            if abs(live_price - expected_price) > max(price_tolerance, expected_price * 0.0005):
                continue
            return order

        return None

    def get_broker_order_history(
        self,
        days: int = 1,
        force: bool = False,
        *,
        include_executions: bool = False,
    ) -> Dict[str, Any]:
        requested_days = max(1, int(days or 1))
        orders_by_id: Dict[str, Dict[str, Any]] = {}

        try:
            if force:
                source_orders = self.get_live_orders(force=True)
            else:
                source_orders = self.get_cached_live_orders(include_all=True)
            for order in source_orders:
                order_id = self._normalize_text(order.get("orderId") or order.get("order_id") or order.get("id"))
                if order_id:
                    orders_by_id[order_id] = dict(order)
        except Exception as exc:
            logger.warning("Failed to get live orders for history: %s", exc)

        raw_executions = []
        if include_executions:
            try:
                raw_executions = list(self.broker.list_recent_fills() or [])
                fill_index = self._build_fill_history_index(raw_executions)
                for order_id, payload in fill_index.items():
                    merged = dict(payload)
                    merged.update(orders_by_id.get(order_id) or {})
                    merged["orderId"] = order_id
                    if "status" not in merged or not merged["status"]:
                        merged["status"] = "FILLED"
                    orders_by_id[order_id] = merged
            except Exception as exc:
                logger.warning("Failed to get recent fills for history: %s", exc)
                raw_executions = []

        return {
            "ok": True,
            "requested_days": requested_days,
            "effective_days": 1,
            "current_day_only": False,
            "source": "broker_force" if force else "broker_callback_cache",
            "executions_requested": bool(include_executions),
            "orders": list(orders_by_id.values()),
            "executions": raw_executions,
            "raw": {"orders": list(orders_by_id.values()), "executions": raw_executions},
            "limitations": [
                "IB Gateway socket API 当前返回 open orders 与最近 executions 的组合视图。",
                "如果需要完整跨日订单历史，请补充 Flex / Statement 链路。",
            ],
        }

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _extract_order_status(order: Dict) -> str:
        return str(
            order.get("status")
            or order.get("order_status")
            or order.get("orderStatus")
            or order.get("state")
            or ""
        ).strip().upper()

    def _find_pb_order_by_broker_id(
        self,
        order_id: str,
        *,
        runtime_environment: str,
        symbol: str = "",
        role: str = "",
        client_order_id: str = "",
        per_page: int = 5,
    ) -> Optional[Dict[str, Any]]:
        normalized_order_id = self._normalize_text(order_id)
        if not normalized_order_id:
            return None

        order_id_filter = self._escape_filter_value(normalized_order_id)
        environment_filter = self._escape_filter_value(runtime_environment)
        matches = self.pb_client.get_records(
            "orders",
            filter=(
                f'(broker_order_id = "{order_id_filter}" || order_id = "{order_id_filter}") '
                f'&& environment = "{environment_filter}"'
            ),
            sort="-updated",
            per_page=max(1, int(per_page or 1)),
        )
        if not matches:
            return None

        raw_matches = [dict(item) for item in matches if isinstance(item, dict)]
        matches = raw_matches
        normalized_symbol = self._normalize_text(symbol).upper()
        normalized_role = self._normalize_pb_order_role(role)
        normalized_client_order_id = self._normalize_text(client_order_id)

        def safe_candidate(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if (
                not normalized_client_order_id
                and len(raw_matches) > 1
                and self._candidate_has_terminal_identity_conflict(candidate, raw_matches)
            ):
                logger.warning(
                    "PB broker_order_id candidate rejected due terminal identity conflict: order_id=%s symbol=%s role=%s environment=%s candidate=%s",
                    normalized_order_id,
                    normalized_symbol,
                    normalized_role,
                    runtime_environment,
                    {
                        "id": str(candidate.get("id") or ""),
                        "unique_id": str(candidate.get("unique_id") or ""),
                        "symbol": str(candidate.get("symbol") or ""),
                        "role": str(candidate.get("role") or ""),
                        "status": str(candidate.get("status") or ""),
                    },
                )
                return None
            return candidate

        if normalized_client_order_id:
            exact_matches = [
                item for item in matches
                if self._pb_order_matches_client_order_id(item, normalized_client_order_id)
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]
            if len(exact_matches) > 1:
                matches = exact_matches
            else:
                logger.warning(
                    "PB broker_order_id match rejected without exact client identity: order_id=%s client_order_id=%s symbol=%s role=%s environment=%s candidates=%s",
                    normalized_order_id,
                    normalized_client_order_id,
                    normalized_symbol,
                    normalized_role,
                    runtime_environment,
                    [
                        {
                            "id": str(item.get("id") or ""),
                            "unique_id": str(item.get("unique_id") or ""),
                            "symbol": str(item.get("symbol") or ""),
                            "role": str(item.get("role") or ""),
                            "status": str(item.get("status") or ""),
                        }
                        for item in matches
                    ],
                )
                return None

        has_identity_hint = bool(normalized_symbol or normalized_role)
        if has_identity_hint:
            hinted_matches = [
                item for item in matches
                if self._pb_order_matches_identity_hints(item, symbol=normalized_symbol, role=normalized_role)
            ]
            if len(hinted_matches) == 1:
                return safe_candidate(hinted_matches[0])
            if len(hinted_matches) > 1:
                matches = hinted_matches
            else:
                logger.warning(
                    "PB broker_order_id match rejected by identity hints: order_id=%s symbol=%s role=%s environment=%s candidates=%s",
                    normalized_order_id,
                    normalized_symbol,
                    normalized_role,
                    runtime_environment,
                    [
                        {
                            "id": str(item.get("id") or ""),
                            "unique_id": str(item.get("unique_id") or ""),
                            "symbol": str(item.get("symbol") or ""),
                            "role": str(item.get("role") or ""),
                            "status": str(item.get("status") or ""),
                        }
                        for item in matches
                    ],
                )
                return None
        elif len(matches) == 1:
            return safe_candidate(matches[0])

        open_matches = [
            item for item in matches
            if self._normalize_text(item.get("relation_status")).lower() in {"active", "planned"}
            or self._normalize_text(item.get("status")).upper() in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "PENDING", "INIT", "APIPENDING", "API_PENDING"}
        ]
        if len(open_matches) == 1:
            return safe_candidate(open_matches[0])
        if len(open_matches) > 1:
            matches = open_matches

        if normalized_symbol:
            symbol_matches = [
                item for item in matches
                if self._normalize_text(item.get("symbol")).upper() == normalized_symbol
            ]
            if len(symbol_matches) == 1:
                return safe_candidate(symbol_matches[0])
            if len(symbol_matches) > 1:
                matches = symbol_matches

        if not normalized_role:
            close_matches = [
                item for item in matches
                if self._pb_order_row_role(item) == "close"
            ]
            if len(close_matches) == 1:
                return safe_candidate(close_matches[0])
            if len(close_matches) > 1:
                matches = close_matches
        if normalized_role:
            role_matches = [
                item for item in matches
                if self._pb_order_row_role(item) == normalized_role
            ]
            if len(role_matches) == 1:
                return safe_candidate(role_matches[0])
            if len(role_matches) > 1:
                matches = role_matches

        logger.warning(
            "Ambiguous PB order match by broker_order_id: order_id=%s symbol=%s role=%s environment=%s candidates=%s",
            normalized_order_id,
            normalized_symbol,
            normalized_role,
            runtime_environment,
            [
                {
                    "id": str(item.get("id") or ""),
                    "unique_id": str(item.get("unique_id") or ""),
                    "symbol": str(item.get("symbol") or ""),
                    "role": str(item.get("role") or ""),
                    "status": str(item.get("status") or ""),
                }
                for item in matches
            ],
        )
        return None

    def _find_pb_entry_by_unique_id(
        self,
        unique_id: str,
        *,
        runtime_environment: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_unique_id = self._normalize_text(unique_id)
        if not normalized_unique_id or not getattr(self, "pb_client", None):
            return None
        try:
            rows = self.pb_client.get_records(
                "orders",
                filter=(
                    f'unique_id = "{self._escape_filter_value(normalized_unique_id)}" && '
                    f'environment = "{self._escape_filter_value(runtime_environment)}" && '
                    f'role = "entry"'
                ),
                sort="-updated,-created",
                per_page=1,
            )
        except Exception as exc:
            logger.debug("PB entry lookup by unique_id failed: unique_id=%s error=%s", normalized_unique_id, exc)
            return None
        return dict(rows[0]) if rows else None

    def _find_pb_entry_for_close_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Any,
        runtime_environment: str,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort link for broker-side market closes that lack PB identity."""
        if not getattr(self, "pb_client", None):
            return None
        normalized_symbol = self._normalize_text(symbol).upper()
        position_side = self._position_side_from_close_order_side(side)
        if not normalized_symbol or not position_side:
            return None

        try:
            rows = self.pb_client.get_records(
                "orders",
                filter=(
                    f'symbol = "{self._escape_filter_value(normalized_symbol)}" && '
                    f'environment = "{self._escape_filter_value(runtime_environment)}" && '
                    f'role = "entry"'
                ),
                sort="-updated,-created",
                per_page=25,
            )
        except Exception as exc:
            logger.debug("Close-order PB entry lookup failed: symbol=%s error=%s", normalized_symbol, exc)
            return None

        close_qty = self._to_float(quantity, 0.0)
        candidates: list[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            unique_id = self._normalize_text(row.get("unique_id"))
            if unique_id.lower().startswith("close_"):
                continue
            row_side = self._record_explicit_direction(row) or self._record_identifier_direction(row)
            if row_side and row_side != position_side:
                continue
            row_qty = self._to_float(
                row.get("filled_qty")
                if row.get("filled_qty") not in (None, "")
                else row.get("quantity"),
                0.0,
            )
            if close_qty > 0 and row_qty > 0 and close_qty - row_qty > 1e-6:
                continue
            status = self._normalize_text(row.get("status")).lower()
            if status not in {"filled", "closed", "protected_active"}:
                continue
            candidates.append(dict(row))

        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                1 if self._normalize_text(item.get("status")).lower() == "filled" else 0,
                self._normalize_text(item.get("updated") or item.get("created")),
            ),
            reverse=True,
        )
        return candidates[0]

    def _close_same_trade_group_active_protection_rows(
        self,
        *,
        runtime_environment: str,
        trade_group_id: str,
        entry_order_unique_id: str,
        close_order_unique_id: str,
        close_order_id: str,
    ) -> None:
        updater = getattr(self.pb_client, "update_record", None)
        if not callable(updater):
            return
        group = self._normalize_text(trade_group_id)
        entry_unique_id = self._normalize_text(entry_order_unique_id)
        if not group and not entry_unique_id:
            return
        parts = []
        if group:
            parts.append(f'trade_group_id = "{self._escape_filter_value(group)}"')
        if entry_unique_id:
            parts.append(f'entry_order_unique_id = "{self._escape_filter_value(entry_unique_id)}"')
        try:
            rows = self.pb_client.get_records(
                "orders",
                filter=(
                    f'environment = "{self._escape_filter_value(runtime_environment)}" && '
                    f'({" || ".join(parts)}) && '
                    '(role = "take_profit" || role = "stop_loss") && '
                    '(relation_status = "active" || relation_status = "planned" || relation_status = "")'
                ),
                sort="-updated",
                per_page=25,
            )
        except Exception as exc:
            logger.debug("Protection close lookup failed after close fill: trade_group=%s error=%s", group, exc)
            return
        for row in rows or []:
            if not isinstance(row, dict) or not self._normalize_text(row.get("id")):
                continue
            row_unique_id = self._normalize_text(row.get("unique_id"))
            if row_unique_id and row_unique_id == self._normalize_text(close_order_unique_id):
                continue
            status = self._normalize_text(row.get("status")).upper()
            relation_status = self._normalize_text(row.get("relation_status")).lower()
            if relation_status == "closed" or status in {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}:
                continue
            extra = self._ensure_object(row.get("extra"))
            extra.update(
                {
                    "closed_by_close_order_unique_id": close_order_unique_id,
                    "closed_by_close_order_id": close_order_id,
                    "closed_by_order_tracker": True,
                    "close_reason": "same_trade_group_close_filled",
                }
            )
            try:
                updater(
                    "orders",
                    row["id"],
                    {
                        "status": "Closed",
                        "relation_status": "closed",
                        "extra": extra,
                    },
                )
            except Exception as exc:
                logger.debug("Protection close update failed: id=%s error=%s", row.get("id"), exc)

    def _stamp_known_order(self, order: Dict, *, seen_live: bool) -> Dict:
        stamped = dict(order or {})
        stamped["_seen_live"] = bool(seen_live)
        stamped["_last_seen_at"] = time.time() if seen_live else float(order.get("_last_seen_at") or 0.0)
        stamped["_missing_poll_count"] = 0
        stamped["_first_missing_at"] = 0.0
        return stamped

    def _order_sync_signature(self, order: Dict) -> tuple:
        return (
            self._extract_order_status(order),
            round(self._to_float(order.get("filledQuantity"), 0.0), 8),
            round(self._to_float(order.get("avgPrice"), 0.0), 8),
            round(self._to_float(order.get("price"), 0.0), 8),
            round(self._to_float(order.get("totalSize"), 0.0), 8),
            str(order.get("parentId") or "").strip(),
            str(order.get("cOID") or order.get("coid") or "").strip(),
            str(order.get("side") or "").strip().upper(),
            str(order.get("orderType") or "").strip().upper(),
            bool(order.get("broker_realtime_callback")),
            str(order.get("ib_callback_type") or "").strip(),
            str(order.get("ib_exec_id") or "").strip(),
        )

    def _order_needs_sync(self, previous: Dict, current: Dict) -> bool:
        if not previous:
            return True
        if not bool(previous.get("_seen_live")):
            return True
        return self._order_sync_signature(previous) != self._order_sync_signature(current)

    def _terminal_order_event_key(self, order: Dict, status: str) -> str:
        order_id = self._normalize_text(
            order.get("orderId")
            or order.get("order_id")
            or order.get("broker_order_id")
            or order.get("id")
        )
        exec_id = self._normalize_text(order.get("ib_exec_id") or order.get("exec_id"))
        role_hint = self._normalize_text(
            order.get("role")
            or order.get("order_role")
            or order.get("cOID")
            or order.get("coid")
            or order.get("orderRef")
            or order.get("order_ref")
        )
        return "|".join(
            [
                order_id,
                str(status or "").strip().upper(),
                exec_id,
                role_hint,
                str(round(self._to_float(order.get("filledQuantity"), 0.0), 8)),
                str(round(self._to_float(order.get("avgPrice"), 0.0), 8)),
            ]
        )

    def _terminal_order_event_seen(self, order: Dict, status: str) -> bool:
        key = self._terminal_order_event_key(order, status)
        if not key.strip("|"):
            return False
        if key in self._emitted_terminal_order_keys:
            return True
        self._emitted_terminal_order_keys.add(key)
        self._emitted_terminal_order_key_order.append(key)
        while len(self._emitted_terminal_order_key_order) > 2000:
            old_key = self._emitted_terminal_order_key_order.pop(0)
            self._emitted_terminal_order_keys.discard(old_key)
        return False

    def _stabilize_live_order_merge(self, previous: Dict, current: Dict) -> Dict:
        if not previous:
            return current
        stabilized = dict(current)
        previous_filled = self._to_float(previous.get("filledQuantity"), 0.0)
        current_filled = self._to_float(stabilized.get("filledQuantity"), 0.0)
        if previous_filled > current_filled:
            stabilized["filledQuantity"] = previous_filled
            if previous.get("remainingQuantity") not in (None, ""):
                stabilized["remainingQuantity"] = previous.get("remainingQuantity")
        previous_avg = self._to_float(previous.get("avgPrice") or previous.get("avgFillPrice"), 0.0)
        current_avg = self._to_float(stabilized.get("avgPrice") or stabilized.get("avgFillPrice"), 0.0)
        if previous_avg > 0 and current_avg <= 0:
            stabilized["avgPrice"] = previous_avg
            stabilized["avgFillPrice"] = previous_avg
        for field in ("lastFillPrice", "lastExecutionTime", "ib_exec_id", "execution_shares", "execution_price"):
            if previous.get(field) not in (None, "") and stabilized.get(field) in (None, ""):
                stabilized[field] = previous.get(field)
        previous_status = self._extract_order_status(previous)
        current_status = self._extract_order_status(stabilized)
        if self._is_terminal_order_status(previous_status) and not self._is_terminal_order_status(current_status):
            stabilized["status"] = previous.get("status")
        return stabilized

    def _emit_order_transition_callbacks(self, previous_status: str, merged: Dict):
        status = self._extract_order_status(merged)
        if status in ("FILLED", "EXECUTED") and previous_status not in ("FILLED", "EXECUTED"):
            if self._terminal_order_event_seen(merged, status):
                logger.debug(
                    "Skipping duplicate terminal fill callback: order_id=%s status=%s",
                    merged.get("orderId") or merged.get("order_id"),
                    status,
                )
                return
            logger.info(
                "Order FILLED: %s %s %s@%s",
                merged.get("ticker"),
                merged.get("side"),
                merged.get("filledQuantity"),
                merged.get("avgPrice"),
            )
            if self.on_fill:
                try:
                    self.on_fill(merged)
                except Exception as exc:
                    logger.error("on_fill callback error: %s", exc)
        elif status in ("CANCELLED", "CANCELED", "INACTIVE", "REJECTED") and previous_status not in ("CANCELLED", "CANCELED", "INACTIVE", "REJECTED"):
            if self._terminal_order_event_seen(merged, status):
                logger.debug(
                    "Skipping duplicate terminal close callback: order_id=%s status=%s",
                    merged.get("orderId") or merged.get("order_id"),
                    status,
                )
                return
            logger.info("Order CLOSED: %s %s status=%s", merged.get("ticker"), merged.get("orderId"), status)
            if self.on_cancel:
                try:
                    self.on_cancel(merged)
                except Exception as exc:
                    logger.error("on_cancel callback error: %s", exc)

    def _handle_live_order_payload(self, order: Dict, source: str) -> bool:
        order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
        if not order_id:
            return False

        prev = dict(self._known_orders.get(order_id, {}))
        prev_status = self._extract_order_status(prev)
        merged = dict(prev)
        merged.update(order)
        merged["orderId"] = order_id
        merged["_order_update_source"] = str(source or "")
        merged["broker_realtime_callback"] = bool(source in {"ws", "broker"} and order.get("broker_realtime_callback"))
        merged = self._stabilize_live_order_merge(prev, merged)
        merged = self._stamp_known_order(merged, seen_live=True)
        should_sync = self._order_needs_sync(prev, merged)
        self._known_orders[order_id] = merged

        if should_sync:
            self._sync_to_pb(merged)
            self._emit_order_transition_callbacks(prev_status, merged)

        record_order_event(
            environment=self.environment,
            operation="tracker_update",
            order_family_type=str(source or "unknown"),
            result="synced" if should_sync else "seen",
            reason_code=self._extract_order_status(merged) or "unknown",
        )
        logger.debug(
            "Order update applied: source=%s order_id=%s status=%s sync=%s",
            source,
            order_id,
            self._extract_order_status(merged),
            should_sync,
        )
        return True

    def _infer_disappeared_order_status(self, previous: Dict) -> str:
        status = self._extract_order_status(previous)
        if status in {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}:
            return status
        filled_qty = self._to_float(previous.get("filledQuantity"), 0.0)
        avg_price = self._to_float(previous.get("avgPrice"), 0.0)
        if filled_qty > 0 or avg_price > 0:
            return "FILLED"
        return "CANCELED"

    def register_submitted_orders(self, order_ids: List[str], seed: Optional[Dict] = None):
        seed = seed or {}
        indexed_ids = [str(item or "").strip() for item in (order_ids or [])]
        if not any(indexed_ids):
            return

        symbol = str(seed.get("symbol") or "").strip().upper()
        direction = str(seed.get("direction") or "").strip().lower()
        entry_unique_id = str(seed.get("entry_unique_id") or "").strip()
        tp_unique_id = str(seed.get("tp_unique_id") or "").strip()
        sl_unique_id = str(seed.get("sl_unique_id") or "").strip()
        side = "BUY" if direction == "long" else "SELL" if direction == "short" else ""
        close_side = "SELL" if side == "BUY" else "BUY" if side == "SELL" else ""
        quantity = seed.get("quantity", 0)
        entry_price = seed.get("entry_price", 0)
        tp_price = seed.get("tp_price", 0)
        sl_price = seed.get("sl_price", 0)
        entry_order_type = str(seed.get("entry_order_type") or "LMT").strip().upper() or "LMT"

        entry_order_id = indexed_ids[0] if len(indexed_ids) > 0 else ""
        tp_order_id = indexed_ids[1] if len(indexed_ids) > 1 else ""
        sl_order_id = indexed_ids[2] if len(indexed_ids) > 2 else ""

        if entry_order_id:
            self._known_orders[entry_order_id] = self._stamp_known_order({
                "orderId": entry_order_id,
                "ticker": symbol,
                "side": side,
                "orderType": entry_order_type,
                "price": entry_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": "",
                "status": "SUBMITTED",
                "cOID": entry_unique_id,
            }, seen_live=False)
        if tp_order_id:
            self._known_orders[tp_order_id] = self._stamp_known_order({
                "orderId": tp_order_id,
                "ticker": symbol,
                "side": close_side,
                "orderType": "LMT",
                "price": tp_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": entry_order_id,
                "status": "SUBMITTED",
                "cOID": tp_unique_id,
            }, seen_live=False)
        if sl_order_id:
            self._known_orders[sl_order_id] = self._stamp_known_order({
                "orderId": sl_order_id,
                "ticker": symbol,
                "side": close_side,
                "orderType": "STP",
                "price": sl_price,
                "auxPrice": sl_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": entry_order_id,
                "status": "SUBMITTED",
                "cOID": sl_unique_id,
            }, seen_live=False)

        self._mark_order_activity()
        self._initial_snapshot_pending = True
        self._poll_wakeup.set()

    def on_order_update(self, order: Dict):
        if not isinstance(order, dict):
            return
        applied = self._handle_live_order_payload(order, source="ws")
        if applied:
            self._live_update_count += 1
            self._last_live_update = time.time()
            self._mark_order_activity()

    def on_broker_order_update(self, order: Dict):
        if not isinstance(order, dict):
            return
        applied = self._handle_live_order_payload(order, source="broker")
        if applied:
            self._live_update_count += 1
            self._last_live_update = time.time()
            self._mark_order_activity()

    def on_execution_fill_update(self, fill: Dict):
        if not isinstance(fill, dict):
            return
        persisted = self._persist_execution_fill(fill, source="ib_socket_callback")
        record_order_event(
            environment=self.environment,
            operation="execution_fill",
            order_family_type="ib_socket_callback",
            result="ok" if persisted else "skipped",
            reason_code="persisted" if persisted else "not_persisted",
        )
        if persisted:
            self._mark_order_activity()

    def _finalize_disappeared_orders(self, current_order_ids: set[str]):
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
        missing_ids = [order_id for order_id in list(self._known_orders.keys()) if order_id not in current_order_ids]

        for order_id in missing_ids:
            previous = dict(self._known_orders.get(order_id, {}))
            previous_status = self._extract_order_status(previous)
            missing_poll_count = int(previous.get("_missing_poll_count") or 0) + 1
            previous["_missing_poll_count"] = missing_poll_count
            previous["_first_missing_at"] = float(previous.get("_first_missing_at") or 0.0) or time.time()

            payload = self.get_order_status(order_id)
            if not payload:
                if missing_poll_count < 2:
                    self._known_orders[order_id] = previous
                    continue
                merged = dict(previous)
                status = self._infer_disappeared_order_status(previous)
                merged["status"] = status
                merged["_status_inferred"] = True
                merged["_status_inferred_reason"] = "missing_from_live_orders_without_status"
            else:
                merged = dict(previous)
                merged.update(payload)
                merged["orderId"] = order_id
                merged = self._stabilize_live_order_merge(previous, merged)
                status = self._extract_order_status(merged)
                if not status:
                    self._known_orders[order_id] = previous
                    continue
                merged["status"] = status

            if status != previous_status:
                self._sync_to_pb(merged)
                self._emit_order_transition_callbacks(previous_status, merged)

            if status in closed_statuses:
                self._known_orders.pop(order_id, None)
            else:
                self._known_orders[order_id] = merged

    def _poll_loop(self):
        logger.info(
            "Order tracker started (mode=%s active_poll=%ds idle_poll=%ds)",
            self._updates_mode(),
            self._active_poll_interval(),
            self._idle_poll_interval(),
        )
        while self._running:
            try:
                self._poll_orders(force=self._initial_snapshot_pending or self._updates_mode() == "poll")
                self._maybe_sync_recent_execution_fills()
                self._initial_snapshot_pending = False
            except Exception as exc:
                record_order_event(
                    environment=self.environment,
                    operation="tracker_poll",
                    result="error",
                    reason_code=exc.__class__.__name__,
                )
                logger.error("Order poll error: %s", exc)

            if not self._running:
                break
            self._poll_wakeup.wait(timeout=self._next_poll_interval())
            self._poll_wakeup.clear()

    def _next_poll_interval(self) -> int:
        if self._updates_mode() == "poll":
            return self._active_poll_interval()
        active_orders = any(self._is_open_order_status(self._extract_order_status(order)) for order in self._known_orders.values())
        last_activity = float(self._last_order_activity or 0.0)
        if last_activity > 0 and (time.time() - last_activity) <= self._fast_track_window():
            active_orders = True
        return self._active_poll_interval() if active_orders else self._idle_poll_interval()

    def _poll_orders(self, force: bool = False):
        started = time.perf_counter()
        try:
            orders = self.get_live_orders(force=bool(force))
            fetch_unavailable = bool(self._last_live_orders_fetch_unavailable)
            self._last_poll = time.time()
            current_order_ids = set()
            for order in orders:
                order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
                if not order_id:
                    continue
                current_order_ids.add(order_id)
                self._handle_live_order_payload(order, source="poll")
            if not fetch_unavailable:
                self._finalize_disappeared_orders(current_order_ids)
        except Exception:
            record_order_event(
                environment=self.environment,
                operation="tracker_poll",
                order_family_type="force" if force else "scheduled",
                result="error",
                duration_s=time.perf_counter() - started,
            )
            raise
        record_order_event(
            environment=self.environment,
            operation="tracker_poll",
            order_family_type="force" if force else "scheduled",
            result="skipped" if bool(self._last_live_orders_fetch_unavailable) else "ok",
            reason_code="account_data_unavailable"
            if bool(self._last_live_orders_fetch_unavailable)
            else "orders_seen"
            if orders
            else "empty",
            duration_s=time.perf_counter() - started,
        )

    def _persist_execution_fill(self, raw_fill: Dict[str, Any], *, source: str) -> bool:
        if not getattr(self, "pb_client", None):
            return False
        if not raw_fill:
            return False
        try:
            normalized = normalize_execution_fill(
                raw_fill,
                environment=self.environment,
                account=self.account_id,
                source=source,
            )
            if not normalized:
                return False
            self.pb_client.upsert_execution_fills([normalized])
            return True
        except AttributeError:
            logger.debug("PB client does not support execution fill upserts")
        except Exception as exc:
            logger.debug("PB execution fill sync failed: %s", exc)
        return False

    def _maybe_sync_recent_execution_fills(self) -> int:
        interval = self._execution_fill_sync_interval()
        if interval <= 0 or not getattr(self, "pb_client", None):
            return 0
        now = time.time()
        if self._last_execution_fill_sync_at and now - self._last_execution_fill_sync_at < interval:
            return 0
        if self._should_skip_account_data_fetch(operation="recent_execution_fills"):
            return 0
        self._last_execution_fill_sync_at = now
        try:
            raw_fills = list(self.broker.list_recent_fills() or [])
        except Exception as exc:
            if self._is_account_data_unavailable_error(exc):
                self._mark_account_data_backoff(exc, operation="recent_execution_fills")
                return 0
            logger.debug("Recent execution fills sync failed: %s", exc)
            return 0
        fills = normalize_execution_fills(
            raw_fills,
            environment=self.environment,
            account=self.account_id,
            source="recent_fills",
        )
        if not fills:
            return 0
        try:
            self.pb_client.upsert_execution_fills(fills)
            return len(fills)
        except AttributeError:
            logger.debug("PB client does not support execution fill upserts")
        except Exception as exc:
            logger.debug("PB recent execution fill upsert failed: %s", exc)
        return 0

    def _partial_harvest_quantity_mismatch(
        self,
        *,
        role: str,
        existing_order: Optional[Dict[str, Any]],
        broker_quantity: Any,
    ) -> dict[str, Any]:
        if str(role or "").strip() != "take_profit" or not isinstance(existing_order, dict):
            return {}
        existing_extra = self._ensure_object(existing_order.get("extra"))
        family = str(
            existing_order.get("order_family_type")
            or existing_extra.get("order_family_type")
            or existing_extra.get("family")
            or ""
        ).strip()
        expected = int(round(self._to_float(
            existing_order.get("partial_tp_quantity")
            or existing_extra.get("partial_tp_quantity")
            or existing_extra.get("expected_quantity"),
            0.0,
        )))
        broker_qty = int(round(self._to_float(broker_quantity, 0.0)))
        is_partial_harvest = (
            family == "partial_harvest_bracket"
            or bool(existing_extra.get("partial_harvest_managed"))
            or expected > 0
        )
        if not is_partial_harvest or expected <= 0 or broker_qty <= 0 or broker_qty == expected:
            return {}
        return {
            "protection_quantity_mismatch": True,
            "quantity_mismatch_reason": "partial_harvest_take_profit_quantity_mismatch",
            "expected_quantity": expected,
            "broker_quantity": broker_qty,
            "protection_expected_quantity": expected,
            "protection_broker_quantity": broker_qty,
            "safety_cancel_recommended": True,
        }

    def _find_signal_row(self, *, signal_id: str, environment: str) -> Optional[Dict[str, Any]]:
        signal_id = self._normalize_text(signal_id)
        environment = self._normalize_text(environment) or self.environment
        if not signal_id or not getattr(self, "pb_client", None):
            return None
        filter_text = (
            f'signal_id = "{self._escape_filter_value(signal_id)}" && '
            f'environment = "{self._escape_filter_value(environment)}"'
        )
        getter = getattr(self.pb_client, "get_first_record", None)
        try:
            if callable(getter):
                row = getter("ibkr_signals", filter=filter_text)
                return dict(row) if isinstance(row, dict) else None
            rows = self.pb_client.get_records("ibkr_signals", filter=filter_text, sort="-updated", per_page=1)
            return dict(rows[0]) if rows else None
        except Exception as exc:
            logger.debug("Signal lookup for protection mismatch failed: signal_id=%s error=%s", signal_id, exc)
            return None

    def _mark_protection_quantity_mismatch(
        self,
        *,
        signal_id: str,
        environment: str,
        symbol: str,
        trade_group_id: str,
        order_id: str,
        unique_id: str,
        mismatch: dict[str, Any],
    ) -> None:
        if not mismatch or not getattr(self, "pb_client", None):
            return
        signal = self._find_signal_row(signal_id=signal_id, environment=environment)
        if not signal:
            return
        signal_extra = self._ensure_object(signal.get("extra"))
        mismatch_key = (
            f"{unique_id or order_id}:"
            f"{mismatch.get('expected_quantity')}:{mismatch.get('broker_quantity')}"
        )
        already_notified = signal_extra.get("protection_quantity_mismatch_key") == mismatch_key
        diagnostic = {
            "reason": "protection_quantity_mismatch",
            "symbol": str(symbol or "").strip().upper(),
            "signal_id": signal_id,
            "trade_group_id": trade_group_id,
            "order_id": order_id,
            "unique_id": unique_id,
            "expected_quantity": mismatch.get("expected_quantity"),
            "broker_quantity": mismatch.get("broker_quantity"),
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
            "safe_action": "diagnostic_only_no_broker_call",
        }
        patch = {
            "status": "protection_incomplete",
            "note": "protection_quantity_mismatch",
            "extra": {
                **signal_extra,
                "status_reason": "protection_quantity_mismatch",
                "protection_complete": False,
                "protection_incomplete": True,
                "protection_quantity_mismatch": True,
                "protection_quantity_mismatch_key": mismatch_key,
                "protection_quantity_mismatch_detail": diagnostic,
                "safety_cancel_recommended": True,
            },
        }
        updater = getattr(self.pb_client, "update_record", None)
        if callable(updater) and signal.get("id"):
            try:
                updater("ibkr_signals", str(signal.get("id")), patch)
            except Exception as exc:
                logger.debug("Signal protection mismatch update failed: signal_id=%s error=%s", signal_id, exc)
        notifier = getattr(self.pb_client, "notify_system_event", None)
        if callable(notifier) and not already_notified:
            try:
                notifier(
                    "保护单数量不一致",
                    {
                        "标的": diagnostic["symbol"] or "-",
                        "信号ID": signal_id or "-",
                        "交易组": trade_group_id or "-",
                        "Broker订单ID": order_id or "-",
                        "UniqueID": unique_id or "-",
                        "计划数量": mismatch.get("expected_quantity"),
                        "Broker数量": mismatch.get("broker_quantity"),
                        "处理建议": "检查保护单数量，必要时取消或修复该交易组。",
                    },
                    event_type="alert",
                    level="error",
                    source="ibkr_compute",
                    environment=environment,
                )
            except Exception as exc:
                logger.debug("Protection mismatch notification failed: signal_id=%s error=%s", signal_id, exc)

    def _sync_to_pb(self, order: dict):
        if not self.pb_client:
            return
        try:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                return
            coid = str(order.get("cOID") or order.get("coid") or order.get("order_ref") or order.get("orderRef") or "").strip()
            symbol = order.get("ticker", "")
            status = self._extract_order_status(order)
            now_times = self._time_fields_from_ms(int(time.time() * 1000))
            order_id_filter = self._escape_filter_value(order_id)
            coid_filter = self._escape_filter_value(coid)
            runtime_environment = self.environment

            if hasattr(self.pb_client, "upsert_order"):
                normalized_side = str(order.get("side", "")).upper()
                quantity = order.get("totalSize") if order.get("totalSize") not in (None, "") else order.get("quantity", 0)
                fill_qty = order.get("filledQuantity", 0)
                incoming_quantity_was_zero = self._to_float(quantity, 0.0) <= 0
                if incoming_quantity_was_zero and self._to_float(fill_qty, 0.0) > 0:
                    quantity = fill_qty
                avg_price = order.get("avgPrice", 0)
                commission = abs(self._to_float(order.get("commission"), 0.0))
                limit_price = self._to_float(order.get("price", 0), 0.0)
                stop_trigger_price = self._to_float(order.get("auxPrice", order.get("stop_price", 0)), 0.0)
                parent_id = order.get("parentId") or ""
                order_type = order.get("orderType") if order.get("orderType") not in (None, "") else order.get("order_type", "")
                upper_type = str(order_type or "").upper()
                if not parent_id:
                    role = "entry"
                    if coid.lower().startswith("close_"):
                        role = "close"
                elif upper_type in ("STP", "STOP", "STOPLOSS"):
                    role = "stop_loss"
                else:
                    role = "take_profit"
                if role == "stop_loss" and limit_price <= 0 and stop_trigger_price > 0:
                    limit_price = stop_trigger_price
                relation_status = (
                    "closed"
                    if str(status).upper()
                    in ("FILLED", "EXECUTED", "CANCELLED", "CANCELED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED", "CLOSED")
                    else "active"
                )
                mapped_status = {
                    "PRESUBMITTED": "Submitted",
                    "SUBMITTED": "Submitted",
                    "APIPENDING": "Submitted",
                    "API_PENDING": "Submitted",
                    "FILLED": "Filled",
                    "EXECUTED": "Filled",
                    "CANCELLED": "Canceled",
                    "CANCELED": "Canceled",
                    "INACTIVE": "Canceled",
                    "REJECTED": "Canceled",
                }.get(str(status).upper(), "Submitted")
                signal_id = ""
                trade_group_id = ""
                entry_order_unique_id = str(coid or "").strip()
                parent_order_unique_id = ""
                canonical_unique_id = entry_order_unique_id if not parent_id else coid
                existing_order = None
                parent_record = None
                linked_close_entry = None

                if coid:
                    coid_order_filter = (
                        f'unique_id = "{coid_filter}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    matches = self.pb_client.get_records("orders", filter=coid_order_filter, sort="-updated", per_page=1)
                    existing_order = matches[0] if matches else None
                if not existing_order and order_id:
                    broker_id_lookup_role = (
                        ""
                        if (not coid and not parent_id and upper_type in {"", "MKT", "MARKET"})
                        else role
                    )
                    existing_order = self._find_pb_order_by_broker_id(
                        order_id,
                        runtime_environment=runtime_environment,
                        symbol=symbol,
                        role=broker_id_lookup_role,
                        client_order_id=coid,
                    )

                if existing_order:
                    if not self._normalize_text(symbol):
                        symbol = str(existing_order.get("symbol") or "").strip().upper()
                    if not self._normalize_text(order_type):
                        existing_order_type = self._normalize_text(existing_order.get("order_type") or existing_order.get("orderType"))
                        if existing_order_type:
                            order_type = existing_order_type
                            upper_type = str(order_type).upper()
                    if incoming_quantity_was_zero:
                        existing_quantity = existing_order.get("quantity")
                        if self._to_float(existing_quantity, 0.0) <= 0:
                            existing_quantity = existing_order.get("totalSize") or existing_order.get("total_size")
                        if self._to_float(existing_quantity, 0.0) <= 0:
                            existing_quantity = existing_order.get("filled_qty") or existing_order.get("filledQuantity")
                        if self._to_float(existing_quantity, 0.0) > self._to_float(quantity, 0.0):
                            quantity = existing_quantity
                    canonical_unique_id = str(existing_order.get("unique_id") or canonical_unique_id or order_id).strip()
                    signal_id = signal_id or str(existing_order.get("signal_id") or "").strip()
                    trade_group_id = str(existing_order.get("trade_group_id") or trade_group_id or "").strip()
                    entry_order_unique_id = str(existing_order.get("entry_order_unique_id") or entry_order_unique_id or canonical_unique_id).strip()
                    parent_order_unique_id = str(existing_order.get("parent_order_unique_id") or "").strip()
                    role = str(existing_order.get("role") or role).strip() or role
                    parent_linked_close = (
                        role == "close"
                        and bool(parent_order_unique_id)
                        and (
                            self._looks_like_close_order_ref(trade_group_id)
                            or self._looks_like_close_order_ref(entry_order_unique_id)
                            or trade_group_id == canonical_unique_id
                            or entry_order_unique_id == canonical_unique_id
                        )
                    )
                    if parent_linked_close:
                        linked_close_entry = self._find_pb_entry_by_unique_id(
                            parent_order_unique_id,
                            runtime_environment=runtime_environment,
                        )
                        if linked_close_entry:
                            trade_group_id = str(
                                linked_close_entry.get("trade_group_id")
                                or linked_close_entry.get("entry_order_unique_id")
                                or parent_order_unique_id
                                or trade_group_id
                            ).strip()
                            entry_order_unique_id = str(
                                linked_close_entry.get("entry_order_unique_id")
                                or parent_order_unique_id
                                or entry_order_unique_id
                            ).strip()
                            signal_id = str(linked_close_entry.get("signal_id") or signal_id or "").strip()
                    self_linked_close = (
                        role == "close"
                        and not parent_order_unique_id
                        and not signal_id
                        and (
                            not trade_group_id
                            or trade_group_id == entry_order_unique_id
                            or trade_group_id == canonical_unique_id
                            or self._looks_like_close_order_ref(trade_group_id)
                        )
                    )
                    if self_linked_close and not parent_id:
                        linked_close_entry = self._find_pb_entry_for_close_order(
                            symbol=symbol,
                            side=normalized_side,
                            quantity=quantity,
                            runtime_environment=runtime_environment,
                        )
                        if linked_close_entry:
                            parent_order_unique_id = str(linked_close_entry.get("unique_id") or "").strip()
                            trade_group_id = str(
                                linked_close_entry.get("trade_group_id")
                                or linked_close_entry.get("entry_order_unique_id")
                                or parent_order_unique_id
                                or trade_group_id
                            ).strip()
                            entry_order_unique_id = str(
                                linked_close_entry.get("entry_order_unique_id")
                                or parent_order_unique_id
                                or entry_order_unique_id
                            ).strip()
                            signal_id = str(linked_close_entry.get("signal_id") or signal_id or "").strip()
                elif parent_id:
                    parent_record = self._find_pb_order_by_broker_id(
                        str(parent_id),
                        runtime_environment=runtime_environment,
                        symbol=symbol,
                        role="entry",
                    )
                    if parent_record:
                        parent_order_unique_id = str(parent_record.get("unique_id") or "").strip()
                        trade_group_id = str(parent_record.get("trade_group_id") or parent_record.get("entry_order_unique_id") or trade_group_id).strip()
                        entry_order_unique_id = str(parent_record.get("entry_order_unique_id") or parent_order_unique_id or entry_order_unique_id).strip()
                        signal_id = signal_id or str(parent_record.get("signal_id") or "").strip()
                elif not parent_id and role in {"entry", "close"} and upper_type in {"MKT", "MARKET"}:
                    linked_close_entry = self._find_pb_entry_for_close_order(
                        symbol=symbol,
                        side=normalized_side,
                        quantity=quantity,
                        runtime_environment=runtime_environment,
                    )
                    if linked_close_entry:
                        role = "close"
                        parent_order_unique_id = str(linked_close_entry.get("unique_id") or "").strip()
                        trade_group_id = str(
                            linked_close_entry.get("trade_group_id")
                            or linked_close_entry.get("entry_order_unique_id")
                            or parent_order_unique_id
                            or trade_group_id
                        ).strip()
                        entry_order_unique_id = str(
                            linked_close_entry.get("entry_order_unique_id")
                            or parent_order_unique_id
                            or entry_order_unique_id
                        ).strip()
                        signal_id = signal_id or str(linked_close_entry.get("signal_id") or "").strip()

                if not canonical_unique_id:
                    canonical_unique_id = order_id
                if not trade_group_id:
                    trade_group_id = entry_order_unique_id or canonical_unique_id
                if not entry_order_unique_id:
                    entry_order_unique_id = canonical_unique_id
                if not self._normalize_text(order_type):
                    order_type = "LMT" if role == "close" else "Entry"

                position_side = self._infer_position_side(
                    side=normalized_side,
                    role=role,
                    parent_id=parent_id,
                    coid=coid,
                    trade_group_id=trade_group_id,
                    entry_order_unique_id=entry_order_unique_id,
                    signal_id=signal_id,
                    existing_order=existing_order,
                    parent_record=parent_record,
                )
                if role == "close":
                    position_side = (
                        self._record_explicit_direction(existing_order)
                        or self._record_explicit_direction(linked_close_entry)
                        or self._position_side_from_close_order_side(normalized_side)
                        or position_side
                    )

                existing_extra = self._ensure_object((existing_order or {}).get("extra"))
                if limit_price <= 0:
                    preserved_limit_price = self._existing_planned_limit_price(
                        role=role,
                        existing_order=existing_order,
                        existing_extra=existing_extra,
                        stop_trigger_price=stop_trigger_price,
                    )
                    if preserved_limit_price > 0:
                        limit_price = preserved_limit_price
                event_times = dict(now_times)
                fill_times: dict[str, Any] = {}
                if mapped_status == "Filled":
                    execution_ms = self._parse_ibkr_execution_time_ms(
                        order.get("lastExecutionTime")
                        or order.get("lastFillTime")
                        or order.get("last_execution_time")
                    )
                    if execution_ms <= 0:
                        execution_ms = int(existing_extra.get("filled_bar_time_ms") or 0)
                    if execution_ms > 0:
                        event_times = self._time_fields_from_ms(execution_ms)
                        fill_times = {
                            "fill_time": event_times["us_time"],
                            "fill_us_time": event_times["us_time"],
                            "fill_cn_time": event_times["cn_time"],
                            "fill_bar_time_ms": event_times["bar_time_ms"],
                        }

                extra = {
                    "source": "order_tracker",
                    "seen_live": bool(order.get("_seen_live")),
                    "broker_update_source": str(order.get("_order_update_source") or ""),
                    "broker_realtime_callback": bool(order.get("broker_realtime_callback")),
                }
                for source_key, target_key in (
                    ("ib_callback_type", "ib_callback_type"),
                    ("broker_callback_source", "broker_callback_source"),
                    ("broker_callback_received_at", "broker_callback_received_at"),
                    ("broker_callback_received_at_ms", "broker_callback_received_at_ms"),
                    ("ib_exec_id", "ib_exec_id"),
                    ("execution_shares", "execution_shares"),
                    ("execution_price", "execution_price"),
                    ("lastFillPrice", "last_fill_price"),
                    ("lastExecutionTime", "last_execution_time"),
                ):
                    if order.get(source_key) not in (None, ""):
                        extra[target_key] = order.get(source_key)
                if role == "close" and existing_extra:
                    for key in (
                        "source",
                        "submitted_via",
                        "reason",
                        "close_reason",
                        "close_reason_code",
                        "close_reason_human",
                        "close_execution_plan",
                        "market_close_result",
                        "position_snapshot",
                        "position_avg_cost",
                        "entry_price_for_pnl",
                        "eod_close_request_id",
                        "eod_close_guard_state",
                        "precreated_close_order",
                    ):
                        if existing_extra.get(key) not in (None, ""):
                            extra[key] = existing_extra.get(key)
                if coid:
                    extra["coid"] = coid
                quantity_mismatch = self._partial_harvest_quantity_mismatch(
                    role=role,
                    existing_order=existing_order,
                    broker_quantity=quantity,
                )
                if quantity_mismatch:
                    extra.update(quantity_mismatch)
                    self._mark_protection_quantity_mismatch(
                        signal_id=signal_id,
                        environment=runtime_environment,
                        symbol=symbol,
                        trade_group_id=trade_group_id,
                        order_id=order_id,
                        unique_id=canonical_unique_id,
                        mismatch=quantity_mismatch,
                    )
                if role == "close":
                    extra.update(
                        {
                            "close_order": True,
                            "close_link_source": (
                                "precreated_order"
                                if bool(existing_extra.get("precreated_close_order"))
                                else ("existing_order" if existing_order else ("symbol_side_quantity" if linked_close_entry else "client_order_id"))
                            ),
                            "linked_entry_order_unique_id": entry_order_unique_id,
                            "linked_trade_group_id": trade_group_id,
                            "close_side": normalized_side,
                        }
                    )
                    if (
                        not any(extra.get(key) not in (None, "") for key in ("reason", "close_reason", "close_reason_code"))
                        and self._looks_like_eod_close_ref(coid or canonical_unique_id)
                    ):
                        extra["reason"] = "force_flat_eod"
                        extra["close_reason"] = "force_flat_eod"
                        extra["close_reason_code"] = "force_flat_eod"
                        extra["close_reason_human"] = "EOD 平仓"
                if commission:
                    extra.update(
                        {
                            "commission": commission,
                            "ibkr_commission": commission,
                            "commission_currency": str(order.get("commissionCurrency") or "USD"),
                        }
                    )
                if order.get("_status_inferred"):
                    extra["status_inferred"] = True
                    extra["status_inferred_reason"] = str(order.get("_status_inferred_reason") or "")

                order_payload = {
                    "unique_id": canonical_unique_id,
                    "order_id": order_id,
                    "broker_order_id": order_id,
                    "order_type": order_type,
                    "symbol": symbol,
                    "direction": position_side,
                    "position_side": position_side,
                    "trade_group_id": trade_group_id,
                    "entry_order_unique_id": entry_order_unique_id,
                    "parent_order_unique_id": parent_order_unique_id,
                    "role": role,
                    "relation_status": relation_status,
                    "signal_id": signal_id,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "status": mapped_status,
                    "filled_qty": fill_qty,
                    "fill_price": avg_price,
                    "commission": commission,
                    "us_time": event_times["us_time"],
                    "cn_time": event_times["cn_time"],
                    "bar_time_ms": event_times["bar_time_ms"],
                    "extra": extra,
                }
                order_payload.update(fill_times)
                if role == "stop_loss" and limit_price > 0:
                    order_payload["sl_price"] = limit_price
                elif role == "take_profit" and limit_price > 0:
                    order_payload["tp_price"] = limit_price
                self.pb_client.upsert_order(order_payload)
                if role == "close" and mapped_status == "Filled":
                    self._close_same_trade_group_active_protection_rows(
                        runtime_environment=runtime_environment,
                        trade_group_id=trade_group_id,
                        entry_order_unique_id=entry_order_unique_id,
                        close_order_unique_id=canonical_unique_id,
                        close_order_id=order_id,
                    )
                record_order_event(
                    environment=self.environment,
                    operation="pb_order_sync",
                    order_family_type=role or "unknown",
                    result="ok",
                    reason_code=mapped_status or "unknown",
                )
        except Exception as exc:
            record_order_event(
                environment=self.environment,
                operation="pb_order_sync",
                result="error",
                reason_code=exc.__class__.__name__,
            )
            logger.debug("PB order sync failed: %s", exc)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="order-tracker")
        self._thread.start()
        self._poll_wakeup.set()

    def stop(self):
        self._running = False
        self._poll_wakeup.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        remove_fill_listener = getattr(self.broker, "remove_execution_fill_listener", None)
        if callable(remove_fill_listener):
            try:
                remove_fill_listener(self.on_execution_fill_update)
            except Exception as exc:
                logger.debug("Failed to remove execution fill listener: %s", exc)

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self._updates_mode(),
            "tracked_orders": len(self._known_orders),
            "active_poll_interval_s": self._active_poll_interval(),
            "idle_poll_interval_s": self._idle_poll_interval(),
            "fast_track_window_s": self._fast_track_window(),
            "live_update_count": self._live_update_count,
            "last_poll": datetime.fromtimestamp(self._last_poll, timezone.utc).isoformat() if self._last_poll else None,
            "last_live_update": datetime.fromtimestamp(self._last_live_update, timezone.utc).isoformat() if self._last_live_update else None,
        }
