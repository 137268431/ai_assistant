from __future__ import annotations

import copy
import hashlib
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Dict, Iterable, List, Optional

from ibkr_compute.broker.ib_gateway_compat import (
    CommissionReport,
    Contract,
    EClient,
    EWrapper,
    ExecutionFilter,
    IBAPI_AVAILABLE,
    IBAPI_IMPORT_ERROR,
    Order,
    OrderCancel,
    TagValue,
)
from ibkr_compute.broker.ib_gateway_support import (
    BENIGN_ERROR_CODES,
    DEFAULT_CLIENT_ID,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_ENVIRONMENT,
    DEFAULT_HOST,
    DEFAULT_LOGIN_POLL_INTERVAL_SECONDS,
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    DEFAULT_SERVICE_NAME,
    FRESH_PROBE_CLIENT_ID_START,
    FRESH_PROBE_RETRIES,
    PREFERRED_CONTRACT_EXCHANGES,
    PREFERRED_CONTRACT_SEC_TYPE_SCORE,
    TICK_ASK_PRICE,
    TICK_ASK_SIZE,
    TICK_BID_PRICE,
    TICK_BID_SIZE,
    TICK_CLOSE_PRICE,
    TICK_LAST_PRICE,
    TICK_LAST_SIZE,
    TICK_LAST_TIMESTAMP,
    TICK_VOLUME,
    _AccountUpdatesCapture,
    _PendingRequest,
    _ib_timestamp_to_ms,
    _iso_now,
    _next_fresh_probe_client_id,
    _run_command,
    _safe_float,
    _safe_int,
)
from ibkr_compute.core.time_utils import ET
from ibkr_compute.observability.prometheus import (
    record_broker_connect,
    record_broker_disconnect,
    record_broker_error,
    record_broker_request,
    record_broker_request_suppressed,
    record_gateway_order_serial_event,
    record_order_event,
    set_broker_pending,
)


logger = logging.getLogger(__name__)
_GLOBAL_GATEWAY_ORDER_WRITE_LOCK = threading.RLock()


TICK_BY_TICK_DUPLICATE_WINDOW_SECONDS = 15.0
# Large bracket bursts can delay openOrder/orderStatus callbacks even when the
# raw placeOrder socket write succeeds immediately.
BRACKET_SUBMISSION_CONFIRM_TIMEOUT_SECONDS = 120.0
try:
    _BRACKET_BACKGROUND_CONFIRM_TIMEOUT = float(os.environ.get("IBKR_BRACKET_BACKGROUND_CONFIRM_TIMEOUT_SEC", "360") or 360.0)
except (TypeError, ValueError):
    _BRACKET_BACKGROUND_CONFIRM_TIMEOUT = 360.0
BRACKET_BACKGROUND_CONFIRM_TIMEOUT_SECONDS = max(30.0, _BRACKET_BACKGROUND_CONFIRM_TIMEOUT)
try:
    _ORDER_MOD_CONFIRM_TIMEOUT = float(os.environ.get("IBKR_ORDER_MODIFICATION_CONFIRM_TIMEOUT_SEC", "30") or 30.0)
except (TypeError, ValueError):
    _ORDER_MOD_CONFIRM_TIMEOUT = 30.0
ORDER_MODIFICATION_CONFIRM_TIMEOUT_SECONDS = max(12.0, _ORDER_MOD_CONFIRM_TIMEOUT)
CANCEL_CONFIRM_TIMEOUT_SECONDS = 6.0
CANCEL_CONFIRM_OPEN_ORDERS_RECONCILE_ENABLED = False
CANCEL_ALL_CONFIRM_TIMEOUT_SECONDS = 12.0
CANCEL_ALL_PENDING_ON_UNCONFIRMED_ENABLED = True
CANCEL_ALL_RECONCILE_TIMEOUT_SECONDS = 180.0
CANCEL_ALL_RECONCILE_POLL_INTERVAL_SECONDS = 1.0
ACCOUNT_DATA_REQUEST_KINDS = {
    "account_pnl",
    "account_summary",
    "account_updates",
    "executions",
    "open_orders",
    "open_orders_all",
    "positions",
}
ACCOUNT_DATA_UNSUBSCRIBED_CODES = {2100}
ACCOUNT_DATA_CIRCUIT_WINDOW_SECONDS = 120.0
ACCOUNT_DATA_CIRCUIT_COOLDOWN_SECONDS = 60.0
ACCOUNT_DATA_CIRCUIT_MIN_FAILURES = 4
ACCOUNT_DATA_EXPECTED_UNSUBSCRIBE_GRACE_SECONDS = 5.0
SMART_ROUTED_US_SEC_TYPES = {"STK", "ETF", "WAR"}


def _order_error_code(order_error: dict | None) -> int:
    try:
        return int((order_error or {}).get("code") or 0)
    except Exception:
        return 0


def _order_error_message(order_error: dict | None) -> str:
    return str((order_error or {}).get("message") or (order_error or {}).get("error") or "").strip()


def _order_error_is_submission_warning(order_error: dict | None) -> bool:
    """IB sends code 399 for valid off-hours orders that are queued for the next open."""
    message = _order_error_message(order_error).lower()
    return (
        _order_error_code(order_error) == 399
        and "will not be placed at the exchange until" in message
    )


def _order_error_is_cancel_notice(order_error: dict | None) -> bool:
    code = _order_error_code(order_error)
    message = _order_error_message(order_error).lower()
    if code == 202:
        return "order canceled" in message or "order cancelled" in message
    if code == 10147:
        return "needs to be cancelled is not found" in message or "not found" in message
    if code == 10148:
        return (
            "state: cancelled" in message
            or "state: canceled" in message
            or ("cannot be cancelled" in message and ("cancelled" in message or "canceled" in message))
        )
    return False


def _submission_confirmation_is_cancel_cleanup_notice(confirmation: dict | None) -> bool:
    """Background submit checks can finish after an explicit cancel_all cleanup."""
    if not isinstance(confirmation, dict):
        return False
    failures = confirmation.get("failures") if isinstance(confirmation.get("failures"), dict) else {}
    if not failures:
        return False
    for failure in failures.values():
        if not isinstance(failure, dict):
            return False
        details = failure.get("details") if isinstance(failure.get("details"), dict) else failure
        if not _order_error_is_cancel_notice(details):
            return False
    return True


def _submission_confirmation_has_missing_orders(confirmation: dict | None) -> bool:
    if not isinstance(confirmation, dict):
        return False
    if str(confirmation.get("error") or "").strip() != "order_submission_unconfirmed":
        return False
    return bool(confirmation.get("missing_order_ids") or [])


def _account_summary_request_limit_message(message: str | None) -> bool:
    return "maximum number of account summary requests exceeded" in str(message or "").strip().lower()


def _remember_submission_warning(warnings: dict[str, list[dict]], order_id: str, order_error: dict | None) -> None:
    warning = dict(order_error or {})
    if not warning:
        return
    bucket = warnings.setdefault(str(order_id or "").strip(), [])
    signature = (_order_error_code(warning), _order_error_message(warning))
    if any((_order_error_code(item), _order_error_message(item)) == signature for item in bucket):
        return
    bucket.append(warning)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _env_bool(name: str, default: bool) -> bool:
    text = str(os.environ.get(name, "")).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


CANCEL_ALL_GLOBAL_CANCEL_ENABLED = _env_bool("IBKR_CANCEL_ALL_GLOBAL_CANCEL_ENABLED", True)
CANCEL_ALL_GLOBAL_CANCEL_GRACE_SECONDS = _env_float("IBKR_CANCEL_ALL_GLOBAL_CANCEL_GRACE_SEC", 0.25, minimum=0.0)
ACCOUNT_SUMMARY_CACHE_TTL_SECONDS = _env_float("IBKR_ACCOUNT_SUMMARY_CACHE_TTL_SEC", 600.0, minimum=0.0)
POSITIONS_CACHE_TTL_SECONDS = _env_float("IBKR_POSITIONS_CACHE_TTL_SEC", 60.0, minimum=0.0)
OPEN_ORDERS_CACHE_TTL_SECONDS = _env_float("IBKR_OPEN_ORDERS_CACHE_TTL_SEC", 30.0, minimum=0.0)
EXECUTIONS_CACHE_TTL_SECONDS = _env_float("IBKR_EXECUTIONS_CACHE_TTL_SEC", 3.0, minimum=0.0)
ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS = _env_float("IBKR_ACCOUNT_DATA_STALE_CACHE_TTL_SEC", 1800.0, minimum=0.0)
ACCOUNT_DATA_SERIAL_TIMEOUT_SECONDS = _env_float("IBKR_ACCOUNT_DATA_SERIAL_TIMEOUT_SEC", 30.0, minimum=0.1)
ACCOUNT_DATA_PACING_COOLDOWN_SECONDS = _env_float("IBKR_ACCOUNT_DATA_PACING_COOLDOWN_SEC", 900.0, minimum=5.0)
ACCOUNT_SUMMARY_MIN_INTERVAL_SECONDS = _env_float(
    "IBKR_ACCOUNT_SUMMARY_MIN_INTERVAL_SEC",
    900.0,
    minimum=0.0,
)
POSITIONS_MIN_INTERVAL_SECONDS = _env_float(
    "IBKR_POSITIONS_MIN_INTERVAL_SEC",
    POSITIONS_CACHE_TTL_SECONDS or 60.0,
    minimum=0.0,
)
OPEN_ORDERS_MIN_INTERVAL_SECONDS = _env_float(
    "IBKR_OPEN_ORDERS_MIN_INTERVAL_SEC",
    OPEN_ORDERS_CACHE_TTL_SECONDS or 30.0,
    minimum=0.0,
)
ACCOUNT_UPDATES_MIN_INTERVAL_SECONDS = _env_float("IBKR_ACCOUNT_UPDATES_MIN_INTERVAL_SEC", 180.0, minimum=0.0)
ACCOUNT_PNL_MIN_INTERVAL_SECONDS = _env_float("IBKR_ACCOUNT_PNL_MIN_INTERVAL_SEC", 180.0, minimum=0.0)
ORDER_CONID_FAST_CONTRACT_ENABLED = _env_bool("IBKR_ORDER_CONID_FAST_CONTRACT_ENABLED", True)
BRACKET_ORDER_ID_OPEN_SCAN_ENABLED = _env_bool("IBKR_BRACKET_ORDER_ID_OPEN_SCAN_ENABLED", False)
ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS = _env_float(
    "IBKR_ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SEC",
    0.75,
    minimum=0.0,
)
ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SECONDS = _env_float(
    "IBKR_ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SEC",
    2.0,
    minimum=0.05,
)
ORDER_CONFIRM_OPEN_ORDERS_SHARED_CACHE_TTL_SECONDS = _env_float(
    "IBKR_ORDER_CONFIRM_OPEN_ORDERS_SHARED_CACHE_TTL_SEC",
    1.0,
    minimum=0.0,
)
MODIFY_ORDER_OBJECT_LOOKUP_TIMEOUT_SECONDS = _env_float(
    "IBKR_MODIFY_ORDER_OBJECT_LOOKUP_TIMEOUT_SEC",
    12.0,
    minimum=0.0,
)
ATTACHED_BRACKET_EXPLICIT_OCA_ENABLED = _env_bool("IBKR_ATTACHED_BRACKET_EXPLICIT_OCA_ENABLED", False)


from ibkr_compute.broker.ib_gateway_service import (
    GatewayServiceManager,
    _pid_uptime_seconds,
    _systemctl_show,
)


class _IBGatewayApp(EWrapper, EClient):
    def __init__(self, host: str, port: int, client_id: int):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        # ibapi EClient.reset() clears self.host/self.port on disconnect, so keep
        # a stable copy for later reconnects after auth or gateway churn.
        self._gateway_host = str(host or DEFAULT_HOST)
        self._gateway_port = int(port or DEFAULT_PORT)
        self.client_id = int(client_id)

        self._connect_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._listener_lock = threading.RLock()
        self._account_updates_lock = threading.RLock()
        self._account_updates_request_lock = threading.Lock()
        self._account_data_request_lock = threading.RLock()
        self._account_request_locks: Dict[str, threading.Lock] = {}
        self._account_request_cache: Dict[tuple, dict[str, Any]] = {}
        self._account_data_request_owner_kind = ""
        self._account_data_request_owner_since = 0.0
        self._account_data_request_queue_timeouts = 0
        self._account_request_last_started_at: Dict[str, float] = {}
        self._account_request_cooldown_until: Dict[str, float] = {}
        self._account_request_cooldown_reason: Dict[str, str] = {}
        self._account_request_suppressed_counts: Dict[str, int] = {}
        self._account_updates_expected_unsubscribe_until = 0.0
        self._request_seq = 1000
        self._ticker_seq = 50_000
        self._pending_requests: Dict[int, _PendingRequest] = {}
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._ready = False
        self._managed_accounts = ""
        self._next_order_id: Optional[int] = None
        self._last_message_at = 0.0
        self._last_connect_at = 0.0
        self._last_disconnect_at = 0.0
        self._last_error_code = 0
        self._last_error_message = ""
        self._last_error_at = 0.0
        self._recent_errors: list[dict[str, Any]] = []
        self._account_data_failures: list[dict[str, Any]] = []
        self._account_data_circuit_until = 0.0
        self._account_data_circuit_reason = ""
        self._account_data_circuit_last_trip_at = 0.0
        self._status_code = 0

        self._market_data_listeners: list[Callable[[dict], None]] = []
        self._order_update_listeners: list[Callable[[dict], None]] = []
        self._execution_fill_listeners: list[Callable[[dict], None]] = []
        self._ticker_meta: Dict[int, dict] = {}
        self._ticker_payloads: Dict[int, dict] = {}
        self._conid_to_ticker: Dict[int, int] = {}
        self._tick_by_tick_meta: Dict[int, dict] = {}
        self._tick_by_tick_by_conid: Dict[tuple[int, str], int] = {}
        self._tick_by_tick_last_request_at: Dict[tuple[int, str], float] = {}
        self._open_orders: Dict[str, dict] = {}
        self._open_order_objects: Dict[str, tuple[Any, Any]] = {}
        self._order_errors: Dict[str, dict] = {}
        self._order_confirmation_open_orders_cache: Dict[tuple[bool, ...], tuple[float, list[dict]]] = {}
        self._positions: Dict[str, dict] = {}
        self._executions: Dict[str, dict] = {}
        self._commission_reports: Dict[str, dict] = {}
        self._account_summary: Dict[str, dict] = {}
        self._account_updates_capture: Optional[_AccountUpdatesCapture] = None
        self._contract_cache_by_symbol: Dict[str, dict] = {}
        self._contract_cache_by_conid: Dict[int, dict] = {}

    def _start_network_thread_locked(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run,
            daemon=True,
            name=f"ibgw-client-{self.client_id}",
        )
        self._thread.start()

    def _reset_transport_locked(self, reason: str = ""):
        if reason:
            logger.info("Resetting IB Gateway socket transport: client_id=%s reason=%s", self.client_id, reason)
        try:
            if self.isConnected():
                super().disconnect()
        except Exception:
            logger.debug("Failed to disconnect stale IB Gateway transport", exc_info=True)
        finally:
            self._ready = False
            self._ready_event.clear()
            self._status_code = 0
            self._next_order_id = None
            self._last_disconnect_at = time.time()
            self._order_confirmation_open_orders_cache = {}
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _broker_not_ready_error(self, action: str, status: dict | None = None) -> RuntimeError:
        snapshot = status or self.status()
        parts = [str(action or "broker_request").strip() or "broker_request", "broker_not_ready"]
        status_code = int(snapshot.get("status_code", 0) or 0)
        last_error_code = int(snapshot.get("last_error_code", 0) or 0)
        last_error = str(snapshot.get("last_error") or "").strip().replace("\n", " ")
        if status_code:
            parts.append(f"status_code={status_code}")
        if last_error_code:
            parts.append(f"last_error_code={last_error_code}")
        if last_error:
            parts.append(last_error)
        return RuntimeError(":".join(parts))

    def _ensure_ready(self, timeout: int, action: str) -> dict:
        ready = self.connect_and_start(timeout=timeout)
        status = self.status()
        if ready:
            return status
        raise self._broker_not_ready_error(action, status=status)

    def _account_data_circuit_snapshot_locked(self, now: float | None = None) -> dict:
        current = float(now or time.time())
        self._account_data_failures = [
            item
            for item in self._account_data_failures[-20:]
            if current - float(item.get("ts", 0) or 0) <= ACCOUNT_DATA_CIRCUIT_WINDOW_SECONDS
        ]
        active = bool(self._account_data_circuit_until and current < self._account_data_circuit_until)
        return {
            "active": active,
            "reason": str(self._account_data_circuit_reason or ""),
            "until": (
                datetime.fromtimestamp(self._account_data_circuit_until, ET).isoformat()
                if active else ""
            ),
            "remaining_s": round(max(0.0, self._account_data_circuit_until - current), 1) if active else 0.0,
            "recent_failure_count": len(self._account_data_failures),
            "recent_failures": [
                {key: value for key, value in item.items() if key != "ts"}
                for item in self._account_data_failures[-10:]
            ],
            "last_trip_at": (
                datetime.fromtimestamp(self._account_data_circuit_last_trip_at, ET).isoformat()
                if self._account_data_circuit_last_trip_at else ""
            ),
        }

    def _account_data_circuit_snapshot(self) -> dict:
        with self._state_lock:
            return self._account_data_circuit_snapshot_locked()

    def _record_account_data_issue(self, kind: str, reason: str) -> None:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in ACCOUNT_DATA_REQUEST_KINDS:
            return
        normalized_reason = str(reason or "account_data_request_failed").strip() or "account_data_request_failed"
        now = time.time()
        with self._state_lock:
            self._account_data_failures.append(
                {
                    "kind": normalized_kind,
                    "reason": normalized_reason,
                    "at": datetime.fromtimestamp(now, ET).isoformat(),
                    "ts": now,
                }
            )
            snapshot = self._account_data_circuit_snapshot_locked(now)
            if snapshot["recent_failure_count"] >= ACCOUNT_DATA_CIRCUIT_MIN_FAILURES:
                self._account_data_circuit_until = now + ACCOUNT_DATA_CIRCUIT_COOLDOWN_SECONDS
                self._account_data_circuit_reason = normalized_reason
                self._account_data_circuit_last_trip_at = now

    def _record_account_data_success(self, kind: str) -> None:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in ACCOUNT_DATA_REQUEST_KINDS:
            return
        with self._state_lock:
            self._account_data_failures = []
            self._account_data_circuit_until = 0.0
            self._account_data_circuit_reason = ""

    def _raise_if_account_data_circuit_open(self, kind: str) -> None:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in ACCOUNT_DATA_REQUEST_KINDS:
            return
        snapshot = self._account_data_circuit_snapshot()
        if bool(snapshot.get("active")):
            reason = str(snapshot.get("reason") or "account_data_circuit_open")
            remaining_s = float(snapshot.get("remaining_s") or 0.0)
            raise TimeoutError(f"account_data_circuit_open:{reason}:retry_after_s={round(remaining_s, 1)}")

    def _account_data_circuit_active(self) -> bool:
        return bool((self._account_data_circuit_snapshot() or {}).get("active"))

    def _account_data_gate_snapshot_locked(self, now: float | None = None) -> dict[str, Any]:
        current = float(now or time.time())
        owner_since = float(self._account_data_request_owner_since or 0.0)
        return {
            "serial_enabled": True,
            "serial_timeout_s": ACCOUNT_DATA_SERIAL_TIMEOUT_SECONDS,
            "owner_kind": str(self._account_data_request_owner_kind or ""),
            "owner_age_s": round(max(0.0, current - owner_since), 3) if owner_since else 0.0,
            "queue_timeouts": int(self._account_data_request_queue_timeouts or 0),
        }

    @contextmanager
    def _account_data_request_gate(self, kind: str, timeout: int | float):
        normalized_kind = str(kind or "").strip().lower() or "account_data"
        if normalized_kind not in ACCOUNT_DATA_REQUEST_KINDS:
            yield {"serialized": False, "kind": normalized_kind, "queue_wait_s": 0.0}
            return
        request_timeout = max(0.1, float(timeout or DEFAULT_CONNECT_TIMEOUT_SECONDS))
        queue_timeout = max(0.1, min(ACCOUNT_DATA_SERIAL_TIMEOUT_SECONDS, request_timeout))
        started = time.perf_counter()
        acquired = self._account_data_request_lock.acquire(timeout=queue_timeout)
        queue_wait_s = time.perf_counter() - started
        if not acquired:
            with self._state_lock:
                self._account_data_request_queue_timeouts += 1
            raise TimeoutError(
                f"account_data_request_queue_timeout:{normalized_kind}:timeout_s={round(queue_timeout, 1)}"
            )
        with self._state_lock:
            self._account_data_request_owner_kind = normalized_kind
            self._account_data_request_owner_since = time.time()
        try:
            yield {"serialized": True, "kind": normalized_kind, "queue_wait_s": queue_wait_s}
        finally:
            with self._state_lock:
                self._account_data_request_owner_kind = ""
                self._account_data_request_owner_since = 0.0
            self._account_data_request_lock.release()

    def _account_request_lock(self, kind: str) -> threading.Lock:
        normalized_kind = str(kind or "").strip().lower() or "account_data"
        with self._state_lock:
            lock = self._account_request_locks.get(normalized_kind)
            if lock is None:
                lock = threading.Lock()
                self._account_request_locks[normalized_kind] = lock
            return lock

    def _account_cache_get(
        self,
        key: tuple,
        *,
        allow_stale: bool = False,
        stale_ttl_seconds: float | None = None,
    ) -> Any:
        now = time.time()
        with self._state_lock:
            entry = self._account_request_cache.get(key)
            if not entry:
                return None
            expires_at = float(entry.get("expires_at") or 0.0)
            if expires_at > now:
                return copy.deepcopy(entry.get("value"))
            stored_at = float(entry.get("stored_at") or 0.0)
            max_stale_age = max(
                0.0,
                float(
                    ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS
                    if stale_ttl_seconds is None
                    else stale_ttl_seconds
                ),
            )
            age_s = now - stored_at if stored_at else 0.0
            if allow_stale and stored_at > 0 and age_s <= max_stale_age:
                return copy.deepcopy(entry.get("value"))
            if not stored_at or age_s > max_stale_age:
                self._account_request_cache.pop(key, None)
                return None
            return None

    def _account_cache_store(self, key: tuple, value: Any, ttl_seconds: float) -> Any:
        ttl = max(0.0, float(ttl_seconds or 0.0))
        if ttl <= 0:
            return value
        with self._state_lock:
            self._account_request_cache[key] = {
                "stored_at": time.time(),
                "expires_at": time.time() + ttl,
                "value": copy.deepcopy(value),
            }
        return value

    def _account_cache_get_if_circuit_open(self, kind: str, key: tuple) -> Any:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in ACCOUNT_DATA_REQUEST_KINDS:
            return None
        if not self._account_data_circuit_active():
            return None
        return self._account_cache_get(
            key,
            allow_stale=True,
            stale_ttl_seconds=ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS,
        )

    @staticmethod
    def _account_request_min_interval(kind: str) -> float:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind == "account_summary":
            return ACCOUNT_SUMMARY_MIN_INTERVAL_SECONDS
        if normalized_kind == "positions":
            return POSITIONS_MIN_INTERVAL_SECONDS
        if normalized_kind in {"open_orders", "open_orders_all"}:
            return OPEN_ORDERS_MIN_INTERVAL_SECONDS
        if normalized_kind == "account_updates":
            return ACCOUNT_UPDATES_MIN_INTERVAL_SECONDS
        if normalized_kind == "account_pnl":
            return ACCOUNT_PNL_MIN_INTERVAL_SECONDS
        return 0.0

    def _ensure_account_pacing_state_locked(self) -> None:
        if not hasattr(self, "_account_request_last_started_at"):
            self._account_request_last_started_at = {}
        if not hasattr(self, "_account_request_cooldown_until"):
            self._account_request_cooldown_until = {}
        if not hasattr(self, "_account_request_cooldown_reason"):
            self._account_request_cooldown_reason = {}
        if not hasattr(self, "_account_request_suppressed_counts"):
            self._account_request_suppressed_counts = {}

    def _account_request_pacing_block_locked(self, kind: str, now: float | None = None) -> dict[str, Any]:
        self._ensure_account_pacing_state_locked()
        current = float(now or time.time())
        normalized_kind = str(kind or "").strip().lower()
        cooldown_until = float(self._account_request_cooldown_until.get(normalized_kind, 0.0) or 0.0)
        if cooldown_until > current:
            return {
                "blocked": True,
                "kind": normalized_kind,
                "reason": self._account_request_cooldown_reason.get(normalized_kind) or "cooldown",
                "retry_after_s": round(cooldown_until - current, 1),
                "source": "cooldown",
            }
        min_interval = self._account_request_min_interval(normalized_kind)
        last_started = float(self._account_request_last_started_at.get(normalized_kind, 0.0) or 0.0)
        if min_interval > 0 and last_started > 0:
            retry_after = min_interval - (current - last_started)
            if retry_after > 0:
                return {
                    "blocked": True,
                    "kind": normalized_kind,
                    "reason": "min_interval",
                    "retry_after_s": round(retry_after, 1),
                    "source": "min_interval",
                }
        return {"blocked": False, "kind": normalized_kind, "reason": "", "retry_after_s": 0.0, "source": ""}

    def _record_account_request_suppressed(self, kind: str, reason: str) -> None:
        normalized_kind = str(kind or "").strip().lower() or "account_data"
        normalized_reason = str(reason or "pacing").strip().lower() or "pacing"
        with self._state_lock:
            self._ensure_account_pacing_state_locked()
            key = f"{normalized_kind}:{normalized_reason}"
            self._account_request_suppressed_counts[key] = int(self._account_request_suppressed_counts.get(key) or 0) + 1
        record_broker_request_suppressed(self, request_kind=normalized_kind, reason_code=normalized_reason)

    def _account_pacing_stale_or_raise(self, kind: str, cache_key: tuple) -> Any:
        normalized_kind = str(kind or "").strip().lower()
        with self._state_lock:
            block = self._account_request_pacing_block_locked(normalized_kind)
        if not bool(block.get("blocked")):
            return None
        reason = str(block.get("reason") or "pacing")
        self._record_account_request_suppressed(normalized_kind, reason)
        stale = self._account_cache_get(
            cache_key,
            allow_stale=True,
            stale_ttl_seconds=ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS,
        )
        if stale is not None:
            return stale
        retry_after_s = float(block.get("retry_after_s") or 0.0)
        raise TimeoutError(
            f"account_data_pacing_cooldown:{normalized_kind}:reason={reason}:retry_after_s={round(retry_after_s, 1)}"
        )

    def _mark_account_request_started(self, kind: str) -> None:
        normalized_kind = str(kind or "").strip().lower()
        if not normalized_kind:
            return
        with self._state_lock:
            self._ensure_account_pacing_state_locked()
            self._account_request_last_started_at[normalized_kind] = time.time()

    def _mark_account_request_cooldown(self, kind: str, reason: str, seconds: float | None = None) -> None:
        normalized_kind = str(kind or "").strip().lower()
        if not normalized_kind:
            return
        duration = max(0.0, float(ACCOUNT_DATA_PACING_COOLDOWN_SECONDS if seconds is None else seconds))
        if duration <= 0:
            return
        with self._state_lock:
            self._ensure_account_pacing_state_locked()
            self._account_request_cooldown_until[normalized_kind] = max(
                float(self._account_request_cooldown_until.get(normalized_kind, 0.0) or 0.0),
                time.time() + duration,
            )
            self._account_request_cooldown_reason[normalized_kind] = str(reason or "cooldown").strip() or "cooldown"

    def _account_request_pacing_snapshot_locked(self, now: float | None = None) -> dict[str, Any]:
        self._ensure_account_pacing_state_locked()
        current = float(now or time.time())
        kinds = sorted(
            set(ACCOUNT_DATA_REQUEST_KINDS)
            | set(self._account_request_last_started_at)
            | set(self._account_request_cooldown_until)
        )
        by_kind: dict[str, Any] = {}
        for kind in kinds:
            block = self._account_request_pacing_block_locked(kind, current)
            cooldown_until = float(self._account_request_cooldown_until.get(kind, 0.0) or 0.0)
            by_kind[kind] = {
                "min_interval_s": self._account_request_min_interval(kind),
                "last_started_age_s": (
                    round(max(0.0, current - float(self._account_request_last_started_at.get(kind, 0.0) or 0.0)), 1)
                    if self._account_request_last_started_at.get(kind)
                    else None
                ),
                "cooldown_active": cooldown_until > current,
                "cooldown_remaining_s": round(max(0.0, cooldown_until - current), 1),
                "cooldown_reason": self._account_request_cooldown_reason.get(kind, ""),
                "blocked": bool(block.get("blocked")),
                "blocked_reason": str(block.get("reason") or ""),
                "retry_after_s": float(block.get("retry_after_s") or 0.0),
            }
        return {
            "enabled": True,
            "cooldown_default_s": ACCOUNT_DATA_PACING_COOLDOWN_SECONDS,
            "stale_cache_ttl_s": ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS,
            "by_kind": by_kind,
            "suppressed_counts": dict(self._account_request_suppressed_counts),
        }

    def _mark_expected_account_updates_unsubscribe(self) -> None:
        with self._state_lock:
            self._account_updates_expected_unsubscribe_until = max(
                float(self._account_updates_expected_unsubscribe_until or 0.0),
                time.time() + ACCOUNT_DATA_EXPECTED_UNSUBSCRIBE_GRACE_SECONDS,
            )

    def _is_expected_account_updates_unsubscribe_error(self, error_code: int) -> bool:
        if int(error_code or 0) not in ACCOUNT_DATA_UNSUBSCRIBED_CODES:
            return False
        with self._state_lock:
            return time.time() <= float(self._account_updates_expected_unsubscribe_until or 0.0)

    def connect_and_start(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> bool:
        if not IBAPI_AVAILABLE:
            raise RuntimeError(f"ibapi not available: {IBAPI_IMPORT_ERROR}")
        connect_started = time.perf_counter()
        with self._connect_lock:
            if self._ready and self.isConnected():
                record_broker_connect(self, result="ok", duration_s=0.0, status_code=200, reason_code="already_ready")
                return True
            # After a gateway restart, ibapi can remain in a half-open state:
            # isConnected() still reports true, but nextValidId never arrives.
            # Force a clean reconnect so auth polling and status pages recover.
            if self.isConnected() and not self._ready:
                self._reset_transport_locked(reason="stale_not_ready")
            if not self.isConnected():
                self._ready = False
                self._ready_event.clear()
                self._status_code = 0
                self._last_error_code = 0
                self._last_error_message = ""
                super().connect(self._gateway_host, self._gateway_port, self.client_id)
                self._start_network_thread_locked()
                self._last_connect_at = time.time()
            ready = self._ready_event.wait(timeout=max(1, int(timeout)))
            if ready:
                self._status_code = 200
            elif self.isConnected():
                self._status_code = 401
                self._reset_transport_locked(reason="ready_timeout")
            else:
                self._status_code = 503
            record_broker_connect(
                self,
                result="ok" if ready else ("timeout" if self._status_code == 401 else "error"),
                duration_s=time.perf_counter() - connect_started,
                status_code=self._status_code,
                reason_code="ready" if ready else ("ready_timeout" if self._status_code == 401 else "connect_failed"),
            )
            return ready

    def disconnect_and_stop(self):
        with self._connect_lock:
            try:
                if self.isConnected():
                    self.disconnect()
                    record_broker_disconnect(self, source="manual", reason_code="disconnect_and_stop")
            finally:
                self._ready = False
                self._ready_event.clear()
                self._last_disconnect_at = time.time()
                self._status_code = 0
                self._next_order_id = None
                self._order_errors = {}
                self._order_confirmation_open_orders_cache = {}
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def connectionClosed(self):  # noqa: N802
        with self._state_lock:
            self._ready = False
            self._status_code = 0
            self._ready_event.clear()
            self._last_disconnect_at = time.time()
            self._next_order_id = None
            self._order_errors = {}
            self._order_confirmation_open_orders_cache = {}
        record_broker_disconnect(self, source="callback", reason_code="connection_closed")

    def nextValidId(self, orderId: int):  # noqa: N802
        with self._state_lock:
            self._next_order_id = int(orderId)
            self._ready = True
            self._status_code = 200
            self._last_message_at = time.time()
            self._ready_event.set()

    def managedAccounts(self, accountsList: str):  # noqa: N802
        with self._state_lock:
            self._managed_accounts = str(accountsList or "")
            self._last_message_at = time.time()

    def error(self, reqId: int, *args):  # noqa: N802
        if len(args) >= 4:
            _error_time, errorCode, errorString, _advancedOrderRejectJson = args[:4]
        elif len(args) >= 3:
            errorCode, errorString, _advancedOrderRejectJson = args[:3]
        elif len(args) >= 2:
            errorCode, errorString = args[:2]
            _advancedOrderRejectJson = ""
        else:
            errorCode = 0
            errorString = args[0] if args else ""
            _advancedOrderRejectJson = ""
        normalized_error_code = int(errorCode or 0)
        order_error_payload = {"code": normalized_error_code, "message": str(errorString or "")}
        order_warning = _order_error_is_submission_warning(order_error_payload)
        cancel_notice = _order_error_is_cancel_notice(order_error_payload)
        expected_account_unsubscribe = self._is_expected_account_updates_unsubscribe_error(normalized_error_code)
        pending_ctx = self._pending_requests.get(int(reqId or 0))
        account_summary_request_limit = bool(
            normalized_error_code == 322
            and (pending_ctx is None or pending_ctx.kind == "account_summary")
            and _account_summary_request_limit_message(str(errorString or ""))
        )
        error_request_kind = (
            pending_ctx.kind
            if pending_ctx
            else "account_summary"
            if account_summary_request_limit
            else "unknown"
        )
        severity = (
            "benign"
            if (
                normalized_error_code in BENIGN_ERROR_CODES
                or expected_account_unsubscribe
                or order_warning
                or cancel_notice
                or account_summary_request_limit
            )
            else "warning"
        )
        record_broker_error(
            self,
            ib_error_code=normalized_error_code,
            request_kind=error_request_kind,
            severity=severity,
        )
        with self._state_lock:
            self._last_message_at = time.time()
            self._last_error_code = int(errorCode or 0)
            self._last_error_message = str(errorString or "")
            self._last_error_at = time.time()
            self._recent_errors.append(
                {
                    "code": int(errorCode or 0),
                    "message": str(errorString or ""),
                    "req_id": int(reqId or 0),
                    "at": datetime.fromtimestamp(self._last_error_at, ET).isoformat(),
                    "ts": self._last_error_at,
                }
            )
            self._recent_errors = [
                item
                for item in self._recent_errors[-20:]
                if self._last_error_at - float(item.get("ts", 0) or 0) <= 120
            ]
            if (
                normalized_error_code not in BENIGN_ERROR_CODES
                and not expected_account_unsubscribe
                and not order_warning
                and not cancel_notice
                and not account_summary_request_limit
            ):
                logger.warning("IB Gateway error reqId=%s code=%s message=%s", reqId, errorCode, errorString)
                if normalized_error_code in ACCOUNT_DATA_UNSUBSCRIBED_CODES:
                    self._record_account_data_issue("account_updates", str(errorString or "account_data_unsubscribed"))
            elif account_summary_request_limit:
                self._mark_account_request_cooldown(
                    "account_summary",
                    "account_summary_request_limit",
                    ACCOUNT_DATA_PACING_COOLDOWN_SECONDS,
                )
                logger.info("IB account summary request limit hit; using cache/backoff reqId=%s code=%s", reqId, errorCode)
            elif order_warning:
                logger.info("Ignoring non-fatal IB order submission warning reqId=%s code=%s", reqId, errorCode)
            elif cancel_notice:
                logger.debug("Ignoring non-fatal IB cancel notice reqId=%s code=%s", reqId, errorCode)
            elif expected_account_unsubscribe:
                logger.debug("Ignoring expected account update unsubscribe error reqId=%s code=%s", reqId, errorCode)
            numeric_req_id = int(reqId or 0)
            if (
                numeric_req_id > 0
                and normalized_error_code not in BENIGN_ERROR_CODES
                and not expected_account_unsubscribe
                and not order_warning
                and not account_summary_request_limit
            ):
                self._order_errors[str(numeric_req_id)] = {
                    "order_id": str(numeric_req_id),
                    "code": normalized_error_code,
                    "message": str(errorString or ""),
                    "advanced_reject_json": str(_advancedOrderRejectJson or ""),
                    "at": _iso_now(),
                }
            if normalized_error_code in {502, 504, 1100, 2110}:
                self._ready = False
                self._status_code = 503
                self._next_order_id = None
                self._ready_event.clear()
        ctx = self._pending_requests.get(int(reqId or 0))
        if ctx and errorCode not in BENIGN_ERROR_CODES and not expected_account_unsubscribe:
            ctx.error = str(errorString or f"ib_error_{errorCode}")
            ctx.event.set()

    def add_market_data_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback not in self._market_data_listeners:
                self._market_data_listeners.append(callback)

    def remove_market_data_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback in self._market_data_listeners:
                self._market_data_listeners.remove(callback)

    def add_order_update_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback not in self._order_update_listeners:
                self._order_update_listeners.append(callback)

    def remove_order_update_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback in self._order_update_listeners:
                self._order_update_listeners.remove(callback)

    def add_execution_fill_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback not in self._execution_fill_listeners:
                self._execution_fill_listeners.append(callback)

    def remove_execution_fill_listener(self, callback: Callable[[dict], None]):
        with self._listener_lock:
            if callback in self._execution_fill_listeners:
                self._execution_fill_listeners.remove(callback)

    def _has_pending_request_kind(self, *kinds: str) -> bool:
        requested = {str(kind or "") for kind in kinds}
        return any(str(ctx.kind or "") in requested for ctx in list(self._pending_requests.values()))

    def _order_callback_metadata(self, callback_type: str, *, requested_snapshot: bool = False) -> dict[str, Any]:
        received_ms = int(time.time() * 1000)
        return {
            "ib_callback_type": str(callback_type or ""),
            "broker_realtime_callback": not bool(requested_snapshot),
            "broker_callback_received_at_ms": received_ms,
            "broker_callback_received_at": _iso_now(),
            "broker_callback_source": "request_snapshot" if requested_snapshot else "ib_socket_callback",
        }

    def _record_pending_open_order_snapshot_item(self, item: dict) -> None:
        order_id = str((item or {}).get("orderId") or (item or {}).get("order_id") or (item or {}).get("id") or "").strip()
        if not order_id:
            return
        for ctx in list(self._pending_requests.values()):
            if ctx.kind not in {"open_orders", "open_orders_all"}:
                continue
            for index, existing in enumerate(list(ctx.items or [])):
                existing_id = str(
                    (existing or {}).get("orderId")
                    or (existing or {}).get("order_id")
                    or (existing or {}).get("id")
                    or ""
                ).strip()
                if existing_id == order_id:
                    ctx.items[index] = dict(item)
                    break
            else:
                ctx.items.append(dict(item))

    def _next_request(self, kind: str) -> tuple[int, _PendingRequest]:
        with self._state_lock:
            self._request_seq += 1
            req_id = self._request_seq
        ctx = _PendingRequest(kind=kind)
        self._pending_requests[req_id] = ctx
        set_broker_pending(
            self,
            request_kind=kind,
            value=sum(1 for item in self._pending_requests.values() if item.kind == kind),
        )
        return req_id, ctx

    def _await(self, req_id: int, ctx: _PendingRequest, timeout: int) -> list:
        started = time.perf_counter()
        if not ctx.event.wait(timeout=max(1, int(timeout))):
            self._pending_requests.pop(req_id, None)
            set_broker_pending(
                self,
                request_kind=ctx.kind,
                value=sum(1 for item in self._pending_requests.values() if item.kind == ctx.kind),
            )
            record_broker_request(
                self,
                request_kind=ctx.kind,
                result="timeout",
                duration_s=time.perf_counter() - started,
                error_class="TimeoutError",
            )
            self._record_account_data_issue(ctx.kind, f"{ctx.kind}_timeout")
            raise TimeoutError(f"{ctx.kind}_timeout")
        self._pending_requests.pop(req_id, None)
        set_broker_pending(
            self,
            request_kind=ctx.kind,
            value=sum(1 for item in self._pending_requests.values() if item.kind == ctx.kind),
        )
        if ctx.error:
            record_broker_request(
                self,
                request_kind=ctx.kind,
                result="error",
                duration_s=time.perf_counter() - started,
                error_class="RuntimeError",
            )
            self._record_account_data_issue(ctx.kind, ctx.error)
            raise RuntimeError(ctx.error)
        self._record_account_data_success(ctx.kind)
        record_broker_request(self, request_kind=ctx.kind, result="ok", duration_s=time.perf_counter() - started)
        return list(ctx.items)

    @staticmethod
    def _account_matches(requested: str, actual: str) -> bool:
        requested_text = str(requested or "").strip()
        actual_text = str(actual or "").strip()
        if requested_text and actual_text:
            return requested_text == actual_text
        return True

    def _get_account_updates_capture(self, account: str = "") -> Optional[_AccountUpdatesCapture]:
        with self._account_updates_lock:
            capture = self._account_updates_capture
        if capture and self._account_matches(capture.account, account):
            return capture
        return None

    @staticmethod
    def _pnl_value(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except Exception:
            return None
        if not math.isfinite(number) or abs(number) >= 1e100:
            return None
        return number

    def _emit_tick(self, ticker_id: int):
        current = self._ticker_payloads.get(ticker_id)
        if not current:
            return
        current["_updated"] = int(time.time() * 1000)
        payload = dict(current)
        self._emit_market_payload(payload)
        ctx = self._pending_requests.get(int(ticker_id))
        if ctx and ctx.kind == "market_data_snapshot" and self._payload_has_quote(payload):
            ctx.items = [payload]

    def _ticker_payload_for_update(self, ticker_id: int) -> Optional[dict]:
        normalized = int(ticker_id)
        payload = self._ticker_payloads.get(normalized)
        if payload is not None:
            return payload
        meta = self._ticker_meta.get(normalized)
        if not meta:
            return None
        payload = dict(meta)
        self._ticker_payloads[normalized] = payload
        return payload

    @staticmethod
    def _payload_has_quote(payload: dict | None) -> bool:
        if not isinstance(payload, dict):
            return False
        for key in ("31", "84", "86", "last_price", "last", "bid", "ask", "bid_price", "ask_price"):
            if _safe_float(payload.get(key), 0.0) > 0:
                return True
        return False

    @staticmethod
    def _quote_from_market_payload(payload: dict | None) -> dict:
        payload = dict(payload or {})

        def positive_or_none(*keys: str) -> float | None:
            for key in keys:
                value = _safe_float(payload.get(key), 0.0)
                if value > 0:
                    return value
            return None

        def non_negative_or_none(*keys: str) -> float | None:
            for key in keys:
                if key not in payload:
                    continue
                value = _safe_float(payload.get(key), -1.0)
                if value >= 0:
                    return value
            return None

        return {
            "symbol": str(payload.get("symbol") or "").upper(),
            "conid": int(payload.get("conid") or payload.get("conidEx") or 0),
            "last_price": positive_or_none("31", "last_price", "last"),
            "bid": positive_or_none("84", "bid", "bid_price"),
            "ask": positive_or_none("86", "ask", "ask_price"),
            "last_size": non_negative_or_none("7059", "last_size", "lastSize", "size"),
            "volume": non_negative_or_none("87", "volume", "vol"),
            "bid_size": non_negative_or_none("bid_size"),
            "ask_size": non_negative_or_none("ask_size"),
            "updated_ms": int(payload.get("_updated") or 0),
        }

    def _emit_market_payload(self, payload: dict):
        with self._listener_lock:
            listeners = list(self._market_data_listeners)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception:
                logger.exception("Market data listener failed")

    def tickPrice(self, tickerId: int, field: int, price: float, _attrib):  # noqa: N802
        payload = self._ticker_payload_for_update(int(tickerId))
        if payload is None:
            return
        if field == TICK_LAST_PRICE:
            payload["31"] = float(price)
        elif field == TICK_BID_PRICE:
            payload["84"] = float(price)
        elif field == TICK_ASK_PRICE:
            payload["86"] = float(price)
        elif field == TICK_CLOSE_PRICE:
            payload["prev_close"] = float(price)
        self._emit_tick(int(tickerId))

    def tickSize(self, tickerId: int, field: int, size: int):  # noqa: N802
        payload = self._ticker_payload_for_update(int(tickerId))
        if payload is None:
            return
        if field == TICK_LAST_SIZE:
            payload["7059"] = int(size)
        elif field == TICK_VOLUME:
            payload["87"] = int(size)
        elif field == TICK_BID_SIZE:
            payload["bid_size"] = int(size)
        elif field == TICK_ASK_SIZE:
            payload["ask_size"] = int(size)
        self._emit_tick(int(tickerId))

    def tickGeneric(self, tickerId: int, field: int, value: float):  # noqa: N802
        if field == TICK_LAST_TIMESTAMP:
            payload = self._ticker_payload_for_update(int(tickerId))
            if payload is None:
                return
            payload["_updated"] = int(float(value) * 1000)
            self._emit_tick(int(tickerId))

    def tickString(self, tickerId: int, tickType: int, value: str):  # noqa: N802
        if int(tickType) != 48:
            return
        parts = str(value or "").split(";")
        if len(parts) < 6:
            return
        payload = self._ticker_payload_for_update(int(tickerId))
        if payload is None:
            return
        payload["31"] = _safe_float(parts[0], 0.0)
        payload["7059"] = _safe_float(parts[1], 0.0)
        payload["87"] = _safe_float(parts[3], 0.0)
        payload["_updated"] = _safe_int(parts[5], int(time.time() * 1000))
        self._emit_tick(int(tickerId))

    def tickSnapshotEnd(self, reqId: int):  # noqa: N802
        with self._state_lock:
            self._last_message_at = time.time()
        ctx = self._pending_requests.get(int(reqId))
        if ctx and ctx.kind == "market_data_snapshot":
            payload = dict(self._ticker_payloads.get(int(reqId)) or {})
            if payload:
                ctx.items = [payload]
            ctx.event.set()

    def tickByTickAllLast(  # noqa: N802
        self,
        reqId: int,
        tickType: int,
        time_: int,
        price: float,
        size: int,
        _tickAttribLast,
        exchange: str,
        specialConditions: str,
    ):
        meta = dict(self._tick_by_tick_meta.get(int(reqId)) or {})
        if not meta:
            return
        payload = {
            **meta,
            "source": "tick_by_tick",
            "tick_type_code": int(tickType or 0),
            "timestamp_ms": int(time_ or time.time()) * 1000,
            "price": float(price or 0.0),
            "31": float(price or 0.0),
            "size": int(size or 0),
            "7059": int(size or 0),
            "exchange": str(exchange or ""),
            "special_conditions": str(specialConditions or ""),
            "_updated": int(time.time() * 1000),
        }
        self._emit_market_payload(payload)

    def tickByTickBidAsk(  # noqa: N802
        self,
        reqId: int,
        time_: int,
        bidPrice: float,
        askPrice: float,
        bidSize: int,
        askSize: int,
        _tickAttribBidAsk,
    ):
        meta = dict(self._tick_by_tick_meta.get(int(reqId)) or {})
        if not meta:
            return
        payload = {
            **meta,
            "source": "tick_by_tick",
            "tick_type": "BidAsk",
            "timestamp_ms": int(time_ or time.time()) * 1000,
            "bid": float(bidPrice or 0.0),
            "84": float(bidPrice or 0.0),
            "ask": float(askPrice or 0.0),
            "86": float(askPrice or 0.0),
            "bid_size": int(bidSize or 0),
            "ask_size": int(askSize or 0),
            "_updated": int(time.time() * 1000),
        }
        self._emit_market_payload(payload)

    def contractDetails(self, reqId: int, contractDetails):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if not ctx:
            return
        contract = contractDetails.contract
        item = {
            "conid": int(contract.conId or 0),
            "symbol": str(contract.symbol or "").upper(),
            "sec_type": str(contract.secType or "").upper(),
            "currency": str(contract.currency or "").upper(),
            "exchange": str(contract.exchange or "").upper(),
            "primary_exchange": str(contract.primaryExchange or "").upper(),
            "local_symbol": str(contract.localSymbol or ""),
            "long_name": str(getattr(contractDetails, "longName", "") or ""),
            "market_name": str(getattr(contractDetails, "marketName", "") or ""),
            "valid_exchanges": str(getattr(contractDetails, "validExchanges", "") or ""),
            "min_tick": _safe_float(getattr(contractDetails, "minTick", 0) or 0, 0.0),
            "trading_class": str(getattr(contractDetails, "tradingClass", "") or ""),
            "trading_hours": str(getattr(contractDetails, "tradingHours", "") or ""),
            "liquid_hours": str(getattr(contractDetails, "liquidHours", "") or ""),
            "time_zone_id": str(getattr(contractDetails, "timeZoneId", "") or ""),
        }
        self._contract_cache_by_symbol[item["symbol"]] = item
        if item["conid"] > 0:
            self._contract_cache_by_conid[item["conid"]] = item
        ctx.items.append(item)

    def contractDetailsEnd(self, reqId: int):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.event.set()

    def symbolSamples(self, reqId: int, contractDescriptions):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if not ctx:
            return
        for desc in contractDescriptions or []:
            contract = getattr(desc, "contract", None)
            if contract is None:
                continue
            sec_types = list(getattr(desc, "derivativeSecTypes", []) or [])
            ctx.items.append(
                {
                    "conid": int(getattr(contract, "conId", 0) or 0),
                    "symbol": str(getattr(contract, "symbol", "") or "").upper(),
                    "sec_type": str(getattr(contract, "secType", "") or "").upper(),
                    "currency": str(getattr(contract, "currency", "") or "").upper(),
                    "exchange": str(getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "") or "").upper(),
                    "description": str(getattr(desc, "description", "") or ""),
                    "derivative_sec_types": sec_types,
                }
            )
        ctx.event.set()

    def historicalData(self, reqId: int, bar):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if not ctx:
            return
        ctx.items.append(
            {
                "t": _ib_timestamp_to_ms(getattr(bar, "date", "")),
                "o": _safe_float(getattr(bar, "open", 0), 0.0),
                "h": _safe_float(getattr(bar, "high", 0), 0.0),
                "l": _safe_float(getattr(bar, "low", 0), 0.0),
                "c": _safe_float(getattr(bar, "close", 0), 0.0),
                "v": _safe_float(getattr(bar, "volume", 0), 0.0),
            }
        )

    def historicalDataEnd(self, reqId: int, _start: str, _end: str):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.event.set()

    def openOrder(self, orderId: int, contract, order, orderState):  # noqa: N802
        requested_snapshot = self._has_pending_request_kind("open_orders", "open_orders_all")
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}
        normalized = {
            "orderId": str(orderId),
            "id": str(orderId),
            "conid": int(getattr(contract, "conId", 0) or 0),
            "account": str(getattr(order, "account", "") or "").strip(),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "contractDesc": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
            "side": str(getattr(order, "action", "") or "").upper(),
            "orderType": str(getattr(order, "orderType", "") or "").upper(),
            "quantity": _safe_float(getattr(order, "totalQuantity", 0), 0.0),
            "totalSize": _safe_float(getattr(order, "totalQuantity", 0), 0.0),
            "price": _safe_float(getattr(order, "lmtPrice", 0), 0.0),
            "auxPrice": _safe_float(getattr(order, "auxPrice", 0), 0.0),
            "status": str(getattr(orderState, "status", "") or ""),
            "parentId": str(getattr(order, "parentId", 0) or ""),
            "ocaGroup": str(getattr(order, "ocaGroup", "") or ""),
            "ocaType": _safe_int(getattr(order, "ocaType", 0), 0),
            "cOID": str(getattr(order, "orderRef", "") or ""),
            "orderRef": str(getattr(order, "orderRef", "") or ""),
            "tif": str(getattr(order, "tif", "") or ""),
            "filledQuantity": 0.0,
            "remainingQuantity": _safe_float(getattr(order, "totalQuantity", 0), 0.0),
            "avgFillPrice": 0.0,
            "avgPrice": 0.0,
            "updated_at": _iso_now(),
            **self._order_callback_metadata("openOrder", requested_snapshot=requested_snapshot),
        }
        key = str(orderId)
        with self._state_lock:
            previous = dict(self._open_orders.get(key) or {})
            previous_filled = _safe_float(previous.get("filledQuantity"), 0.0)
            previous_avg = _safe_float(previous.get("avgPrice") or previous.get("avgFillPrice"), 0.0)
            previous_remaining = _safe_float(previous.get("remainingQuantity"), 0.0)
            if previous_filled > _safe_float(normalized.get("filledQuantity"), 0.0):
                normalized["filledQuantity"] = previous_filled
                normalized["remainingQuantity"] = min(
                    _safe_float(normalized.get("remainingQuantity"), 0.0),
                    previous_remaining,
                )
            if previous_avg > 0 and _safe_float(normalized.get("avgPrice"), 0.0) <= 0:
                normalized["avgPrice"] = previous_avg
                normalized["avgFillPrice"] = previous_avg
            for key_name in ("lastFillPrice", "lastExecutionTime", "ib_exec_id", "execution_shares", "execution_price"):
                if previous.get(key_name) not in (None, "") and normalized.get(key_name) in (None, ""):
                    normalized[key_name] = previous.get(key_name)
            if str(previous.get("status") or "").strip().upper() in closed_statuses and str(normalized.get("status") or "").strip().upper() not in closed_statuses:
                normalized["status"] = previous.get("status")
            merged = {**previous, **normalized}
            self._open_orders[key] = merged
            self._open_order_objects[key] = (copy.deepcopy(contract), copy.deepcopy(order))
            self._record_pending_open_order_snapshot_item(merged)
        self._emit_order_update(merged)

    def orderStatus(  # noqa: N802
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        _permId: int,
        _parentId: int,
        _lastFillPrice: float,
        _clientId: int,
        _whyHeld: str,
        _mktCapPrice: float,
    ):
        key = str(orderId)
        requested_snapshot = self._has_pending_request_kind("open_orders", "open_orders_all")
        patch = {
            "orderId": key,
            "id": key,
            "status": str(status or ""),
            "filledQuantity": _safe_float(filled, 0.0),
            "remainingQuantity": _safe_float(remaining, 0.0),
            "avgFillPrice": _safe_float(avgFillPrice, 0.0),
            "avgPrice": _safe_float(avgFillPrice, 0.0),
            "updated_at": _iso_now(),
            **self._order_callback_metadata("orderStatus", requested_snapshot=requested_snapshot),
        }
        with self._state_lock:
            current = dict(self._open_orders.get(key) or {})
            current_filled = _safe_float(current.get("filledQuantity"), 0.0)
            patch_filled = _safe_float(patch.get("filledQuantity"), 0.0)
            if current_filled > patch_filled:
                patch["filledQuantity"] = current_filled
                patch["remainingQuantity"] = _safe_float(current.get("remainingQuantity"), 0.0)
            current_avg = _safe_float(current.get("avgPrice") or current.get("avgFillPrice"), 0.0)
            if current_avg > 0 and _safe_float(patch.get("avgPrice"), 0.0) <= 0:
                patch["avgPrice"] = current_avg
                patch["avgFillPrice"] = current_avg
            closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}
            if str(current.get("status") or "").strip().upper() in closed_statuses and str(patch.get("status") or "").strip().upper() not in closed_statuses:
                patch["status"] = current.get("status")
            merged = {**current, **patch}
            self._open_orders[key] = merged
        self._emit_order_update(merged)

    def openOrderEnd(self):  # noqa: N802
        for req_id, ctx in list(self._pending_requests.items()):
            if ctx.kind in {"open_orders", "open_orders_all"}:
                with self._state_lock:
                    ctx.items = [dict(item) for item in (ctx.items or [])]
                ctx.event.set()

    def position(self, account: str, contract, position: float, avgCost: float):  # noqa: N802
        key = f"{account}:{getattr(contract, 'conId', 0)}"
        self._positions[key] = {
            "account": str(account or ""),
            "conid": int(getattr(contract, "conId", 0) or 0),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "contractDesc": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
            "position": _safe_float(position, 0.0),
            "avgCost": _safe_float(avgCost, 0.0),
            "updated_at": _iso_now(),
        }

    def positionEnd(self):  # noqa: N802
        for req_id, ctx in list(self._pending_requests.items()):
            if ctx.kind == "positions":
                with self._state_lock:
                    ctx.items = [dict(item) for item in self._positions.values()]
                ctx.event.set()

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        bucket = self._account_summary.setdefault(str(account or ""), {})
        bucket[str(tag or "")] = {
            "value": str(value or ""),
            "currency": str(currency or ""),
        }
        if ctx is not None:
            ctx.items = [dict(bucket)]

    def accountSummaryEnd(self, reqId: int):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.event.set()

    def pnl(self, reqId: int, dailyPnL: float, unrealizedPnL: float, realizedPnL: float):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        payload = {
            "daily_pnl": self._pnl_value(dailyPnL),
            "unrealized_pnl": self._pnl_value(unrealizedPnL),
            "realized_pnl": self._pnl_value(realizedPnL),
            "source": "reqPnL",
            "updated_at": _iso_now(),
        }
        with self._state_lock:
            self._last_message_at = time.time()
        if ctx is not None:
            ctx.items = [payload]
            ctx.event.set()

    def updateAccountValue(self, key: str, val: str, currency: str, accountName: str):  # noqa: N802
        account = str(accountName or "").strip()
        bucket = self._account_summary.setdefault(account, {})
        bucket[str(key or "")] = {
            "value": str(val or ""),
            "currency": str(currency or ""),
        }
        with self._state_lock:
            self._last_message_at = time.time()
        capture = self._get_account_updates_capture(account)
        if capture:
            capture.summary[str(key or "")] = {
                "value": str(val or ""),
                "currency": str(currency or ""),
            }

    def updatePortfolio(  # noqa: N802
        self,
        contract,
        position: float,
        marketPrice: float,
        marketValue: float,
        averageCost: float,
        unrealizedPNL: float,
        realizedPNL: float,
        accountName: str,
    ):
        account = str(accountName or "").strip()
        payload = {
            "account": account,
            "acctId": account,
            "conid": int(getattr(contract, "conId", 0) or 0),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "contractDesc": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
            "position": _safe_float(position, 0.0),
            "avgCost": _safe_float(averageCost, 0.0),
            "avgPrice": _safe_float(averageCost, 0.0),
            "mktPrice": _safe_float(marketPrice, 0.0),
            "mktValue": _safe_float(marketValue, 0.0),
            "unrealizedPnl": _safe_float(unrealizedPNL, 0.0),
            "realizedPnl": _safe_float(realizedPNL, 0.0),
            "currency": str(getattr(contract, "currency", "") or "").upper(),
            "assetClass": str(getattr(contract, "secType", "") or "").upper(),
            "updated_at": _iso_now(),
        }
        with self._state_lock:
            self._last_message_at = time.time()
        capture = self._get_account_updates_capture(account)
        if capture:
            position_key = f"{account}:{payload['conid'] or payload['ticker']}"
            capture.positions[position_key] = payload

    def accountDownloadEnd(self, accountName: str):  # noqa: N802
        with self._state_lock:
            self._last_message_at = time.time()
        capture = self._get_account_updates_capture(str(accountName or "").strip())
        if capture:
            capture.event.set()

    def execDetails(self, reqId: int, contract, execution):  # noqa: N802
        exec_id = str(getattr(execution, "execId", "") or "")
        if not exec_id:
            return
        order_id = str(getattr(execution, "orderId", "") or "")
        shares = _safe_float(getattr(execution, "shares", 0), 0.0)
        price = _safe_float(getattr(execution, "price", 0), 0.0)
        pending_commission = dict(self._commission_reports.get(exec_id) or {})
        execution_payload = {
            "execId": exec_id,
            "exec_id": exec_id,
            "orderId": order_id,
            "order_id": order_id,
            "perm_id": str(getattr(execution, "permId", "") or ""),
            "client_id": _safe_int(getattr(execution, "clientId", 0), 0),
            "order_ref": str(getattr(execution, "orderRef", "") or ""),
            "conid": int(getattr(contract, "conId", 0) or 0),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "symbol": str(getattr(contract, "symbol", "") or "").upper(),
            "side": str(getattr(execution, "side", "") or "").upper(),
            "shares": shares,
            "price": price,
            "time": str(getattr(execution, "time", "") or ""),
            "account": str(getattr(execution, "acctNumber", "") or ""),
            "exchange": str(getattr(execution, "exchange", "") or getattr(contract, "exchange", "") or ""),
            "asset_category": str(getattr(contract, "secType", "") or ""),
            "contract_multiplier": _safe_float(getattr(contract, "multiplier", 0), 0.0),
            "commission": 0.0,
            "commission_known": False,
            "commission_currency": "",
            "realized_pnl": 0.0,
            "realized_pnl_known": False,
            **pending_commission,
        }
        self._executions[exec_id] = execution_payload
        self._emit_execution_fill_update(execution_payload)
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            existing_index = next(
                (
                    index
                    for index, item in enumerate(ctx.items)
                    if str((item or {}).get("execId") or (item or {}).get("exec_id") or "") == exec_id
                ),
                -1,
            )
            if existing_index >= 0:
                ctx.items[existing_index] = dict(execution_payload)
            else:
                ctx.items.append(dict(execution_payload))
        if ctx and ctx.kind == "executions":
            return
        if not order_id:
            return
        with self._state_lock:
            current = dict(self._open_orders.get(order_id) or {})
            order_execs = [
                item
                for item in self._executions.values()
                if str(item.get("orderId") or "") == order_id
            ]
            cumulative_shares = sum(_safe_float(item.get("shares"), 0.0) for item in order_execs)
            fill_value = sum(
                _safe_float(item.get("shares"), 0.0) * _safe_float(item.get("price"), 0.0)
                for item in order_execs
            )
            commission = sum(abs(_safe_float(item.get("commission"), 0.0)) for item in order_execs)
            commission_known = bool(order_execs) and all(bool(item.get("commission_known")) for item in order_execs)
            average_price = (fill_value / cumulative_shares) if cumulative_shares > 0 else price
            remaining_quantity = _safe_float(current.get("remainingQuantity"), 0.0)
            status = str(current.get("status") or "")
            if remaining_quantity <= 0 and cumulative_shares > 0:
                status = status or "Filled"
            payload = {
                **current,
                "orderId": order_id,
                "id": order_id,
                "conid": int(getattr(contract, "conId", 0) or current.get("conid") or 0),
                "ticker": str(getattr(contract, "symbol", "") or current.get("ticker") or "").upper(),
                "side": str(getattr(execution, "side", "") or current.get("side") or "").upper(),
                "status": status or "Submitted",
                "filledQuantity": cumulative_shares,
                "avgFillPrice": average_price,
                "avgPrice": average_price,
                "lastFillPrice": price,
                "lastExecutionTime": str(getattr(execution, "time", "") or ""),
                "ib_exec_id": exec_id,
                "execution_shares": shares,
                "execution_price": price,
                "commission": commission,
                "commission_known": commission_known,
                "commission_currency": str(pending_commission.get("commission_currency") or current.get("commissionCurrency") or current.get("commission_currency") or ""),
                "updated_at": _iso_now(),
                **self._order_callback_metadata("execDetails", requested_snapshot=False),
            }
            self._open_orders[order_id] = payload
        self._emit_order_update(payload)

    def execDetailsEnd(self, reqId: int):  # noqa: N802
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.event.set()

    def commissionReport(self, commissionReport: CommissionReport):  # noqa: N802
        exec_id = str(getattr(commissionReport, "execId", "") or "")
        if not exec_id:
            return
        realized_pnl = _safe_float(getattr(commissionReport, "realizedPNL", 0), 0.0)
        realized_known = math.isfinite(realized_pnl) and abs(realized_pnl) < 1e100
        commission = _safe_float(
            getattr(commissionReport, "commission", getattr(commissionReport, "commissionAndFees", 0)),
            0.0,
        )
        payload = {
            "commission": abs(commission),
            "commission_currency": str(getattr(commissionReport, "currency", "") or ""),
            "commission_known": True,
            "realized_pnl": realized_pnl if realized_known else 0.0,
            "realized_pnl_known": realized_known,
            "commission_report_received_at": _iso_now(),
        }
        order_update: dict[str, Any] | None = None
        execution_update: dict[str, Any] | None = None
        with self._state_lock:
            self._commission_reports[exec_id] = payload
            if exec_id in self._executions:
                self._executions[exec_id].update(payload)
                execution_update = dict(self._executions[exec_id])
                order_id = str(self._executions[exec_id].get("orderId") or self._executions[exec_id].get("order_id") or "")
                if order_id:
                    current = dict(self._open_orders.get(order_id) or {})
                    order_execs = [
                        item
                        for item in self._executions.values()
                        if str(item.get("orderId") or item.get("order_id") or "") == order_id
                    ]
                    commission = sum(abs(_safe_float(item.get("commission"), 0.0)) for item in order_execs)
                    commission_known = bool(order_execs) and all(bool(item.get("commission_known")) for item in order_execs)
                    current.update(
                        {
                            "commission": commission,
                            "commission_known": commission_known,
                            "commission_currency": payload["commission_currency"] or current.get("commission_currency") or current.get("commissionCurrency") or "",
                            "updated_at": _iso_now(),
                            **self._order_callback_metadata("commissionReport", requested_snapshot=False),
                        }
                    )
                    self._open_orders[order_id] = current
                    order_update = current
            for ctx in self._pending_requests.values():
                if ctx.kind != "executions":
                    continue
                for index, item in enumerate(ctx.items):
                    if str((item or {}).get("execId") or (item or {}).get("exec_id") or "") == exec_id:
                        refreshed = dict(item)
                        refreshed.update(payload)
                        ctx.items[index] = refreshed
        if execution_update:
            self._emit_execution_fill_update(execution_update)
        if order_update:
            self._emit_order_update(order_update)

    def commissionAndFeesReport(self, commissionAndFeesReport: CommissionReport):  # noqa: N802
        self.commissionReport(commissionAndFeesReport)

    def _emit_order_update(self, payload: dict):
        with self._listener_lock:
            listeners = list(self._order_update_listeners)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception:
                logger.exception("Order update listener failed")

    def _emit_execution_fill_update(self, payload: dict):
        with self._listener_lock:
            listeners = list(self._execution_fill_listeners)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception:
                logger.exception("Execution fill listener failed")

    def next_order_ids(self, count: int, minimum: int = 0) -> List[int]:
        if not self._ready_event.wait(DEFAULT_CONNECT_TIMEOUT_SECONDS):
            raise RuntimeError("ib_gateway_not_ready")
        count = max(1, int(count))
        with self._state_lock:
            if self._next_order_id is None:
                raise RuntimeError("missing_next_order_id")
            start = max(int(self._next_order_id), int(minimum or 0))
            self._next_order_id += count
            self._next_order_id = max(int(self._next_order_id), start + count)
        return list(range(start, start + count))

    @staticmethod
    def _extract_order_id(payload: dict | None) -> int:
        if not isinstance(payload, dict):
            return 0
        return _safe_int(
            payload.get("orderId")
            or payload.get("order_id")
            or payload.get("id")
            or payload.get("broker_order_id"),
            0,
        )

    def max_seen_order_id(self) -> int:
        with self._state_lock:
            candidates = [
                self._extract_order_id(item)
                for item in list(self._open_orders.values())
            ]
            candidates.append(_safe_int(self._next_order_id, 0) - 1)
        return max([0, *candidates])

    def next_order_ids_above(self, count: int, minimum: int = 0) -> List[int]:
        return self.next_order_ids(count, minimum=max(0, int(minimum or 0)))

    def next_ticker_ids(self, count: int) -> List[int]:
        count = max(1, int(count))
        with self._state_lock:
            start = int(self._ticker_seq)
            self._ticker_seq += count
        return list(range(start, start + count))

    def request_contract_details(
        self,
        *,
        symbol: str = "",
        conid: int = 0,
        exchange: str = "SMART",
        currency: str = "USD",
        sec_type: str = "STK",
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> List[dict]:
        self._ensure_ready(timeout, "request_contract_details")
        req_id, ctx = self._next_request("contract_details")
        contract = Contract()
        if int(conid or 0) > 0:
            contract.conId = int(conid)
        else:
            if symbol:
                contract.symbol = str(symbol or "").upper()
                if sec_type:
                    contract.secType = str(sec_type or "").upper()
                contract.exchange = str(exchange or "SMART")
                contract.currency = str(currency or "USD")
            elif exchange:
                contract.exchange = str(exchange)
            elif currency:
                contract.currency = str(currency)
        self.reqContractDetails(req_id, contract)
        return self._await(req_id, ctx, timeout)

    def request_matching_symbols(self, query: str, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self._ensure_ready(timeout, "request_matching_symbols")
        req_id, ctx = self._next_request("matching_symbols")
        self.reqMatchingSymbols(req_id, str(query or "").strip())
        return self._await(req_id, ctx, timeout)

    def request_historical_bars(
        self,
        *,
        conid: int,
        symbol: str,
        exchange: str = "",
        sec_type: str = "",
        duration: str,
        bar_size: str,
        end_datetime: str = "",
        use_rth: bool = False,
        timeout: int = 30,
        contract_details: Optional[dict] = None,
    ) -> List[dict]:
        self._ensure_ready(timeout, "request_historical_bars")
        details = [dict(contract_details)] if isinstance(contract_details, dict) and contract_details.get("conid") else []
        if not details:
            details = self.request_contract_details(conid=conid, timeout=timeout)
            if not details and symbol:
                details = self.request_contract_details(
                    symbol=symbol,
                    exchange=exchange or "SMART",
                    sec_type=sec_type or "STK",
                    timeout=timeout,
                )
        if not details:
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        contract = Contract()
        contract.conId = int(details[0]["conid"])
        contract.symbol = str(details[0]["symbol"])
        contract.secType = str(details[0]["sec_type"] or sec_type or "STK")
        contract.exchange = str(details[0]["exchange"] or details[0].get("primary_exchange") or exchange or "SMART")
        contract.currency = str(details[0]["currency"] or "USD")
        req_id, ctx = self._next_request("historical")
        self.reqHistoricalData(
            req_id,
            contract,
            str(end_datetime or ""),
            str(duration),
            str(bar_size),
            "TRADES",
            1 if use_rth else 0,
            2,
            False,
            [],
        )
        return self._await(req_id, ctx, timeout)

    def request_market_data_snapshot(
        self,
        *,
        conid: int,
        symbol: str = "",
        exchange: str = "SMART",
        sec_type: str = "STK",
        currency: str = "USD",
        timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        contract_details: Optional[dict] = None,
    ) -> Dict[str, Any]:
        try:
            normalized_timeout = max(0.1, float(timeout or DEFAULT_CONNECT_TIMEOUT_SECONDS))
            request_timeout = max(1, int(math.ceil(normalized_timeout)))
            self._ensure_ready(request_timeout, "request_market_data_snapshot")

            details = dict(contract_details or {})
            normalized_conid = int(details.get("conid") or conid or 0)
            normalized_symbol = str(details.get("symbol") or symbol or "").upper()
            if normalized_conid <= 0 and not normalized_symbol:
                return {"ok": False, "error": "missing_contract", "quote": {}, "payload": {}}

            contract = Contract()
            contract.conId = normalized_conid
            contract.symbol = normalized_symbol
            contract.secType = str(details.get("sec_type") or sec_type or "STK").upper()
            contract.exchange = str(details.get("exchange") or details.get("primary_exchange") or exchange or "SMART")
            contract.currency = str(details.get("currency") or currency or "USD").upper()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "source": "ibkr_market_data_snapshot",
                "quote": {},
                "payload": {},
            }

        req_id, ctx = self._next_request("market_data_snapshot")
        meta = {
            "tickerId": req_id,
            "reqId": req_id,
            "conid": int(normalized_conid),
            "conidEx": int(normalized_conid),
            "symbol": normalized_symbol,
            "snapshot": True,
            "broker_quote_snapshot": True,
        }
        self._ticker_meta[req_id] = meta
        self._ticker_payloads[req_id] = dict(meta)
        deadline = time.time() + normalized_timeout
        first_quote_at = 0.0
        try:
            try:
                self.reqMarketDataType(1)
            except Exception:
                logger.debug("reqMarketDataType(1) failed before snapshot conid=%s", normalized_conid, exc_info=True)
            # IB rejects snapshot requests that include generic tick lists; bid/ask/last
            # snapshots do not need generic ticks.
            self.reqMktData(req_id, contract, "", True, False, [])

            while True:
                now_ts = time.time()
                remaining = deadline - now_ts
                payload = dict(self._ticker_payloads.get(req_id) or {})
                has_quote = self._payload_has_quote(payload)
                if has_quote:
                    if first_quote_at <= 0:
                        first_quote_at = now_ts
                    if ctx.event.is_set() or now_ts - first_quote_at >= 0.15 or remaining <= 0:
                        break
                elif ctx.event.is_set() or remaining <= 0:
                    break

                wait_for = min(0.05, max(0.0, remaining))
                if wait_for > 0:
                    ctx.event.wait(wait_for)

            payload = dict(self._ticker_payloads.get(req_id) or {})
            quote = self._quote_from_market_payload(payload)
            if self._payload_has_quote(payload):
                return {
                    "ok": True,
                    "source": "ibkr_market_data_snapshot",
                    "quote": quote,
                    "payload": payload,
                    **quote,
                }
            error = str(ctx.error or "")
            return {
                "ok": False,
                "error": error or ("market_data_snapshot_no_quote" if ctx.event.is_set() else "market_data_snapshot_timeout"),
                "source": "ibkr_market_data_snapshot",
                "quote": quote,
                "payload": payload,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "source": "ibkr_market_data_snapshot",
                "quote": {},
                "payload": {},
            }
        finally:
            self._pending_requests.pop(req_id, None)
            self._ticker_meta.pop(req_id, None)
            self._ticker_payloads.pop(req_id, None)

    @staticmethod
    def _filter_open_order_snapshots(rows: Iterable[dict] | None) -> List[dict]:
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}
        result: List[dict] = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or item.get("order_status") or item.get("orderStatus") or "").strip().upper()
            if status in closed_statuses:
                continue
            result.append(dict(item))
        return result

    def request_open_orders(
        self,
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        *,
        include_all: bool = False,
        force: bool = False,
    ) -> List[dict]:
        kind = "open_orders_all" if include_all else "open_orders"
        cache_key = (kind, bool(include_all))
        all_cache_key = ("open_orders_all", True)
        if not force:
            if not include_all:
                cached_all = self._account_cache_get(all_cache_key)
                if cached_all is not None:
                    open_orders = self._filter_open_order_snapshots(cached_all)
                    self._account_cache_store(cache_key, open_orders, OPEN_ORDERS_CACHE_TTL_SECONDS)
                    return list(open_orders or [])
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return list(cached or [])
        with self._account_request_lock(kind):
            if not force:
                if not include_all:
                    cached_all = self._account_cache_get(all_cache_key)
                    if cached_all is not None:
                        open_orders = self._filter_open_order_snapshots(cached_all)
                        self._account_cache_store(cache_key, open_orders, OPEN_ORDERS_CACHE_TTL_SECONDS)
                        return list(open_orders or [])
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return list(cached or [])
            self._ensure_ready(timeout, "request_open_orders")
            stale = self._account_cache_get_if_circuit_open(kind, cache_key)
            if stale is not None:
                return list(stale or [])
            if not force:
                paced = self._account_pacing_stale_or_raise(kind, cache_key)
                if paced is not None:
                    return list(paced or [])
            with self._account_data_request_gate(kind, timeout):
                if not force:
                    cached = self._account_cache_get(cache_key)
                    if cached is not None:
                        return list(cached or [])
                stale = self._account_cache_get_if_circuit_open(kind, cache_key)
                if stale is not None:
                    return list(stale or [])
                if not force:
                    paced = self._account_pacing_stale_or_raise(kind, cache_key)
                    if paced is not None:
                        return list(paced or [])
                self._raise_if_account_data_circuit_open(kind)
                req_id, ctx = self._next_request(kind)
                self._mark_account_request_started(kind)
                if include_all:
                    self.reqAllOpenOrders()
                else:
                    self.reqOpenOrders()
                orders = self._await(req_id, ctx, timeout)
                self._account_cache_store(cache_key, orders, OPEN_ORDERS_CACHE_TTL_SECONDS)
                if include_all:
                    self._account_cache_store(
                        ("open_orders", False),
                        self._filter_open_order_snapshots(orders),
                        OPEN_ORDERS_CACHE_TTL_SECONDS,
                    )
                return orders

    def request_open_orders_for_order_confirmation(
        self,
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        *,
        include_all: bool = False,
        force: bool = False,
    ) -> List[dict]:
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        if not hasattr(self, "_order_confirmation_open_orders_cache"):
            self._order_confirmation_open_orders_cache = {}
        cache_key = (bool(include_all),)
        now = time.time()
        ttl = float(ORDER_CONFIRM_OPEN_ORDERS_SHARED_CACHE_TTL_SECONDS or 0.0)
        if not force and ttl > 0:
            with self._state_lock:
                cached = self._order_confirmation_open_orders_cache.get(cache_key)
                if cached and now - float(cached[0] or 0.0) <= ttl:
                    return [dict(item) for item in (cached[1] or [])]
        try:
            orders = self.request_open_orders(timeout=timeout, include_all=include_all, force=True)
        except TypeError:
            try:
                orders = self.request_open_orders(timeout=timeout, force=True)
            except TypeError:
                orders = self.request_open_orders(timeout=timeout)
        if ttl > 0:
            with self._state_lock:
                self._order_confirmation_open_orders_cache[cache_key] = (
                    time.time(),
                    [dict(item) for item in (orders or [])],
                )
        return list(orders or [])

    def request_positions(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        cache_key = ("positions",)
        cached = self._account_cache_get(cache_key)
        if cached is not None:
            return list(cached or [])
        with self._account_request_lock("positions"):
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return list(cached or [])
            self._ensure_ready(timeout, "request_positions")
            stale = self._account_cache_get_if_circuit_open("positions", cache_key)
            if stale is not None:
                return list(stale or [])
            paced = self._account_pacing_stale_or_raise("positions", cache_key)
            if paced is not None:
                return list(paced or [])
            with self._account_data_request_gate("positions", timeout):
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return list(cached or [])
                stale = self._account_cache_get_if_circuit_open("positions", cache_key)
                if stale is not None:
                    return list(stale or [])
                paced = self._account_pacing_stale_or_raise("positions", cache_key)
                if paced is not None:
                    return list(paced or [])
                self._raise_if_account_data_circuit_open("positions")
                req_id, ctx = self._next_request("positions")
                self._mark_account_request_started("positions")
                self._positions = {}
                try:
                    self.reqPositions()
                    positions = self._await(req_id, ctx, timeout)
                    self._account_cache_store(cache_key, positions, POSITIONS_CACHE_TTL_SECONDS)
                    return positions
                finally:
                    try:
                        self.cancelPositions()
                    except Exception:
                        logger.debug("cancelPositions failed", exc_info=True)

    def request_account_summary(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> Dict[str, dict]:
        cache_key = ("account_summary",)
        cached = self._account_cache_get(cache_key)
        if cached is not None:
            return dict(cached or {})
        with self._account_request_lock("account_summary"):
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return dict(cached or {})
            self._ensure_ready(timeout, "request_account_summary")
            stale = self._account_cache_get_if_circuit_open("account_summary", cache_key)
            if stale is not None:
                return dict(stale or {})
            paced = self._account_pacing_stale_or_raise("account_summary", cache_key)
            if paced is not None:
                return dict(paced or {})
            with self._account_data_request_gate("account_summary", timeout):
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return dict(cached or {})
                stale = self._account_cache_get_if_circuit_open("account_summary", cache_key)
                if stale is not None:
                    return dict(stale or {})
                paced = self._account_pacing_stale_or_raise("account_summary", cache_key)
                if paced is not None:
                    return dict(paced or {})
                self._raise_if_account_data_circuit_open("account_summary")
                req_id, ctx = self._next_request("account_summary")
                self._mark_account_request_started("account_summary")
                try:
                    self.reqAccountSummary(req_id, "All", "NetLiquidation,BuyingPower,AvailableFunds,ExcessLiquidity")
                    try:
                        items = self._await(req_id, ctx, timeout)
                    except Exception as exc:
                        if _account_summary_request_limit_message(str(exc)):
                            self._mark_account_request_cooldown(
                                "account_summary",
                                "account_summary_request_limit",
                                ACCOUNT_DATA_PACING_COOLDOWN_SECONDS,
                            )
                            stale = self._account_cache_get(
                                cache_key,
                                allow_stale=True,
                                stale_ttl_seconds=ACCOUNT_DATA_STALE_CACHE_TTL_SECONDS,
                            )
                            if stale is not None:
                                self._account_cache_store(cache_key, stale, ACCOUNT_SUMMARY_CACHE_TTL_SECONDS)
                                return dict(stale or {})
                        raise
                    if not items:
                        return {}
                    account = self._managed_accounts.split(",", 1)[0].strip() if self._managed_accounts else ""
                    summary = dict(self._account_summary.get(account) or items[0] or {})
                    self._account_cache_store(cache_key, summary, ACCOUNT_SUMMARY_CACHE_TTL_SECONDS)
                    return summary
                finally:
                    try:
                        self.cancelAccountSummary(req_id)
                    except Exception:
                        logger.debug("cancelAccountSummary failed for req_id=%s", req_id, exc_info=True)

    def request_account_updates(
        self,
        *,
        account: str = "",
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        requested_account = str(account or "").strip()
        if not requested_account and self._managed_accounts:
            requested_account = self._managed_accounts.split(",", 1)[0].strip()
        cache_key = ("account_updates", requested_account or str(account or "").strip())
        cached = self._account_cache_get(cache_key)
        if cached is not None:
            return dict(cached or {})
        with self._account_updates_request_lock:
            if requested_account:
                cache_key = ("account_updates", requested_account)
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return dict(cached or {})
            self._ensure_ready(timeout, "request_account_updates")
            if not requested_account and self._managed_accounts:
                requested_account = self._managed_accounts.split(",", 1)[0].strip()
                cache_key = ("account_updates", requested_account)
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return dict(cached or {})
            stale = self._account_cache_get_if_circuit_open("account_updates", cache_key)
            if stale is not None:
                return dict(stale or {})
            paced = self._account_pacing_stale_or_raise("account_updates", cache_key)
            if paced is not None:
                return dict(paced or {})
            with self._account_data_request_gate("account_updates", timeout):
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return dict(cached or {})
                stale = self._account_cache_get_if_circuit_open("account_updates", cache_key)
                if stale is not None:
                    return dict(stale or {})
                paced = self._account_pacing_stale_or_raise("account_updates", cache_key)
                if paced is not None:
                    return dict(paced or {})
                self._raise_if_account_data_circuit_open("account_updates")
                if not requested_account:
                    raise RuntimeError("missing_managed_account")
                cache_key = ("account_updates", requested_account)

                capture = _AccountUpdatesCapture(account=requested_account)
                with self._account_updates_lock:
                    self._account_updates_capture = capture
                self._mark_account_request_started("account_updates")
                try:
                    self.reqAccountUpdates(True, requested_account)
                    if not capture.event.wait(timeout=max(1, int(timeout))):
                        self._record_account_data_issue("account_updates", "account_updates_timeout")
                        raise TimeoutError("account_updates_timeout")
                    self._record_account_data_success("account_updates")
                    payload = {
                        "account": requested_account,
                        "summary": dict(capture.summary),
                        "positions": [dict(item) for item in capture.positions.values()],
                    }
                    self._account_cache_store(cache_key, payload, ACCOUNT_SUMMARY_CACHE_TTL_SECONDS)
                    return payload
                finally:
                    try:
                        self._mark_expected_account_updates_unsubscribe()
                        self.reqAccountUpdates(False, requested_account)
                    except Exception:
                        logger.debug("reqAccountUpdates(False) failed for %s", requested_account, exc_info=True)
                    with self._account_updates_lock:
                        if self._account_updates_capture is capture:
                            self._account_updates_capture = None

    def request_account_pnl(
        self,
        *,
        account: str = "",
        model_code: str = "",
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        requested_account = str(account or "").strip()
        if not requested_account and self._managed_accounts:
            requested_account = self._managed_accounts.split(",", 1)[0].strip()
        model_code_text = str(model_code or "")
        cache_key = ("account_pnl", requested_account or str(account or "").strip(), model_code_text)
        cached = self._account_cache_get(cache_key)
        if cached is not None:
            return dict(cached or {})
        with self._account_request_lock("account_pnl"):
            if requested_account:
                cache_key = ("account_pnl", requested_account, model_code_text)
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return dict(cached or {})
            self._ensure_ready(timeout, "request_account_pnl")
            requested_account = str(account or "").strip()
            if not requested_account and self._managed_accounts:
                requested_account = self._managed_accounts.split(",", 1)[0].strip()
            if not requested_account:
                raise RuntimeError("missing_managed_account")
            cache_key = ("account_pnl", requested_account, model_code_text)
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return dict(cached or {})
            stale = self._account_cache_get_if_circuit_open("account_pnl", cache_key)
            if stale is not None:
                return dict(stale or {})
            paced = self._account_pacing_stale_or_raise("account_pnl", cache_key)
            if paced is not None:
                return dict(paced or {})
            if not callable(getattr(self, "reqPnL", None)):
                raise RuntimeError("req_pnl_unavailable")

            with self._account_data_request_gate("account_pnl", timeout):
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return dict(cached or {})
                stale = self._account_cache_get_if_circuit_open("account_pnl", cache_key)
                if stale is not None:
                    return dict(stale or {})
                paced = self._account_pacing_stale_or_raise("account_pnl", cache_key)
                if paced is not None:
                    return dict(paced or {})
                self._raise_if_account_data_circuit_open("account_pnl")
                req_id, ctx = self._next_request("account_pnl")
                self._mark_account_request_started("account_pnl")
                subscribed = False
                try:
                    self.reqPnL(req_id, requested_account, model_code_text)
                    subscribed = True
                    items = self._await(req_id, ctx, timeout)
                    payload = dict(items[0] or {}) if items else {}
                    if not payload:
                        return {}
                    payload.setdefault("source", "reqPnL")
                    payload["account"] = requested_account
                    payload["model_code"] = model_code_text
                    self._account_cache_store(cache_key, payload, ACCOUNT_PNL_MIN_INTERVAL_SECONDS)
                    return payload
                finally:
                    self._pending_requests.pop(req_id, None)
                    if subscribed:
                        try:
                            self.cancelPnL(req_id)
                        except Exception:
                            logger.debug("cancelPnL failed for req_id=%s account=%s", req_id, requested_account, exc_info=True)

    def request_executions(
        self,
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        *,
        force: bool = False,
    ) -> List[dict]:
        cache_key = ("executions",)
        if not force:
            cached = self._account_cache_get(cache_key)
            if cached is not None:
                return list(cached or [])
        with self._account_request_lock("executions"):
            if not force:
                cached = self._account_cache_get(cache_key)
                if cached is not None:
                    return list(cached or [])
            self._ensure_ready(timeout, "request_executions")
            stale = self._account_cache_get_if_circuit_open("executions", cache_key)
            if stale is not None:
                return list(stale or [])
            with self._account_data_request_gate("executions", timeout):
                if not force:
                    cached = self._account_cache_get(cache_key)
                    if cached is not None:
                        return list(cached or [])
                stale = self._account_cache_get_if_circuit_open("executions", cache_key)
                if stale is not None:
                    return list(stale or [])
                self._raise_if_account_data_circuit_open("executions")
                req_id, ctx = self._next_request("executions")
                self.reqExecutions(req_id, ExecutionFilter())
                items = self._await(req_id, ctx, timeout)
                # IB sends commissionReport callbacks separately from execDetailsEnd.
                # Give those callbacks a short window, then return the refreshed cache.
                exec_ids = [str(item.get("execId") or "") for item in (items or []) if str(item.get("execId") or "")]
                if exec_ids:
                    deadline = time.time() + min(2.0, max(0.0, float(timeout or 0)) * 0.25)
                    while time.time() < deadline:
                        if all(bool(self._executions.get(exec_id, {}).get("commission_known")) for exec_id in exec_ids):
                            break
                        time.sleep(0.1)
                    items = [dict(self._executions.get(exec_id) or item) for exec_id, item in zip(exec_ids, items)]
                self._account_cache_store(cache_key, items, EXECUTIONS_CACHE_TTL_SECONDS)
                return list(items or [])

    def place_order(self, contract: Any, order: Any, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        started = time.perf_counter()
        self._ensure_ready(timeout, "place_order")
        order_type = str(getattr(order, "orderType", "") or "unknown")
        try:
            self.placeOrder(int(order.orderId), contract, order)
        except Exception as exc:
            record_order_event(
                operation="ibapi_place",
                order_family_type=order_type,
                result="error",
                reason_code=exc.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            raise
        try:
            order_id = str(int(order.orderId))
            with self._state_lock:
                if not hasattr(self, "_open_order_objects"):
                    self._open_order_objects = {}
                self._open_order_objects[order_id] = (copy.deepcopy(contract), copy.deepcopy(order))
        except Exception:
            logger.debug("Failed to seed local order object after placeOrder", exc_info=True)
        record_order_event(
            operation="ibapi_place",
            order_family_type=order_type,
            result="ok",
            duration_s=time.perf_counter() - started,
        )

    def cancel_open_order(self, order_id: str, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        started = time.perf_counter()
        self._ensure_ready(timeout, "cancel_open_order")
        try:
            if OrderCancel is not None:
                try:
                    self.cancelOrder(int(order_id), OrderCancel())
                except TypeError:
                    # Older ibapi releases only accept orderId.
                    self.cancelOrder(int(order_id))
            else:
                self.cancelOrder(int(order_id))
        except Exception as exc:
            record_order_event(
                operation="ibapi_cancel",
                result="error",
                reason_code=exc.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            raise
        record_order_event(operation="ibapi_cancel", result="ok", duration_s=time.perf_counter() - started)

    def request_global_cancel(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        started = time.perf_counter()
        self._ensure_ready(timeout, "request_global_cancel")
        try:
            if OrderCancel is not None:
                try:
                    self.reqGlobalCancel(OrderCancel())
                except TypeError:
                    # Older ibapi releases only accept no arguments.
                    self.reqGlobalCancel()
            else:
                self.reqGlobalCancel()
        except Exception as exc:
            record_order_event(
                operation="ibapi_global_cancel",
                result="error",
                reason_code=exc.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            raise
        record_order_event(operation="ibapi_global_cancel", result="ok", duration_s=time.perf_counter() - started)

    def get_order_snapshot(self, order_id: str) -> dict:
        return dict(self._open_orders.get(str(order_id)) or {})

    def mark_order_terminal(self, order_id: str, *, status: str = "CANCELLED", reason: str = "") -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {}
        terminal_status = str(status or "CANCELLED").strip().upper()
        if terminal_status in {"", "NOT_OPEN", "CANCELED"}:
            terminal_status = "CANCELLED"
        payload: dict[str, Any]
        changed = False
        with self._state_lock:
            current = dict(self._open_orders.get(normalized_order_id) or {})
            previous_status = str(current.get("status") or "").strip().upper()
            current.update(
                {
                    "orderId": normalized_order_id,
                    "id": normalized_order_id,
                    "status": terminal_status,
                    "remainingQuantity": 0.0,
                    "updated_at": _iso_now(),
                    "_status_inferred": True,
                    "_status_inferred_reason": str(reason or "open_orders_reconcile_not_open"),
                    **self._order_callback_metadata("terminalReconcile", requested_snapshot=True),
                }
            )
            current.setdefault("filledQuantity", 0.0)
            current.setdefault("avgFillPrice", current.get("avgPrice") or 0.0)
            current.setdefault("avgPrice", current.get("avgFillPrice") or 0.0)
            self._open_orders[normalized_order_id] = current
            self._open_order_objects.pop(normalized_order_id, None)
            self._order_confirmation_open_orders_cache = {}
            for cache_key in list(self._account_request_cache.keys()):
                if cache_key and str(cache_key[0]) in {"open_orders", "open_orders_all"}:
                    self._account_request_cache.pop(cache_key, None)
            payload = dict(current)
            changed = previous_status != terminal_status
        if changed:
            self._emit_order_update(payload)
        return payload

    def get_order_snapshots(self, *, include_all: bool = False) -> List[dict]:
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}
        with self._state_lock:
            rows = [dict(item) for item in self._open_orders.values()]
        if include_all:
            return rows
        return [
            item
            for item in rows
            if str(item.get("status") or "").strip().upper() not in closed_statuses
        ]

    def get_order_objects(self, order_id: str) -> tuple[Any, Any] | tuple[None, None]:
        item = self._open_order_objects.get(str(order_id))
        if not item:
            return None, None
        contract, order = item
        return copy.deepcopy(contract), copy.deepcopy(order)

    def clear_order_error(self, order_id: str):
        self._order_errors.pop(str(order_id or "").strip(), None)

    def get_order_error(self, order_id: str) -> dict:
        return dict(self._order_errors.get(str(order_id or "").strip()) or {})

    def await_order_submission(
        self,
        order_id: str,
        *,
        timeout: float = 3.0,
        poll_interval: float = 0.2,
    ) -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {"ok": False, "error": "missing_order_id"}

        started_at = time.time()
        deadline = started_at + max(0.5, float(timeout or 0.0))
        request_timeout = max(1, min(30, int(max(1.0, float(timeout or 0.0)))))
        next_open_orders_at = started_at + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS or 0.0)
        ignored_warnings: dict[str, list[dict]] = {}

        while time.time() < deadline:
            order_error = self.get_order_error(normalized_order_id)
            if order_error:
                if _order_error_is_submission_warning(order_error):
                    _remember_submission_warning(ignored_warnings, normalized_order_id, order_error)
                else:
                    return {
                        "ok": False,
                        "order_id": normalized_order_id,
                        "error": str(order_error.get("message") or "order_rejected"),
                        "details": order_error,
                    }

            snapshot = self.get_order_snapshot(normalized_order_id)
            if snapshot:
                if ignored_warnings.get(normalized_order_id):
                    self.clear_order_error(normalized_order_id)
                result = {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "source": "snapshot",
                    "order": snapshot,
                }
                if ignored_warnings.get(normalized_order_id):
                    result["ignored_warnings"] = list(ignored_warnings[normalized_order_id])
                return result

            now = time.time()
            if now >= next_open_orders_at:
                try:
                    requester = getattr(self, "request_open_orders_for_order_confirmation", None)
                    if callable(requester):
                        open_orders = requester(timeout=request_timeout)
                    else:
                        open_orders = self.request_open_orders(timeout=request_timeout, force=True)
                except Exception as exc:
                    logger.debug("await_order_submission reqOpenOrders failed for %s: %s", normalized_order_id, exc)
                    open_orders = []
                for open_order in open_orders or []:
                    open_order_id = str(
                        open_order.get("orderId")
                        or open_order.get("order_id")
                        or open_order.get("id")
                        or ""
                    ).strip()
                    if open_order_id == normalized_order_id:
                        if ignored_warnings.get(normalized_order_id):
                            self.clear_order_error(normalized_order_id)
                        result = {
                            "ok": True,
                            "order_id": normalized_order_id,
                            "source": "open_orders",
                            "order": dict(open_order),
                        }
                        if ignored_warnings.get(normalized_order_id):
                            result["ignored_warnings"] = list(ignored_warnings[normalized_order_id])
                        return result
                next_open_orders_at = time.time() + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SECONDS or 0.05)

            time.sleep(max(0.05, float(poll_interval or 0.2)))

        order_error = self.get_order_error(normalized_order_id)
        if order_error:
            if _order_error_is_submission_warning(order_error):
                _remember_submission_warning(ignored_warnings, normalized_order_id, order_error)
            else:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": str(order_error.get("message") or "order_rejected"),
                    "details": order_error,
                }

        try:
            requester = getattr(self, "request_open_orders_for_order_confirmation", None)
            if callable(requester):
                open_orders = requester(timeout=request_timeout)
            else:
                open_orders = self.request_open_orders(timeout=request_timeout, force=True)
        except Exception as exc:
            logger.debug("await_order_submission final reqOpenOrders failed for %s: %s", normalized_order_id, exc)
            open_orders = []
        for open_order in open_orders or []:
            open_order_id = str(
                open_order.get("orderId")
                or open_order.get("order_id")
                or open_order.get("id")
                or ""
            ).strip()
            if open_order_id == normalized_order_id:
                if ignored_warnings.get(normalized_order_id):
                    self.clear_order_error(normalized_order_id)
                result = {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "source": "open_orders",
                    "order": dict(open_order),
                }
                if ignored_warnings.get(normalized_order_id):
                    result["ignored_warnings"] = list(ignored_warnings[normalized_order_id])
                return result

        result = {
            "ok": False,
            "order_id": normalized_order_id,
            "error": "order_submission_unconfirmed",
        }
        if ignored_warnings.get(normalized_order_id):
            result["ignored_warnings"] = list(ignored_warnings[normalized_order_id])
        return result

    def await_order_submissions(
        self,
        order_ids: Iterable[str],
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.2,
        open_orders_fallback_delay: float | None = None,
        open_orders_fallback_interval: float | None = None,
    ) -> dict:
        expected_ids = [str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()]
        if not expected_ids:
            return {"ok": False, "error": "missing_order_ids", "orders": {}, "missing_order_ids": []}

        started_at = time.time()
        deadline = started_at + max(0.5, float(timeout or 0.0))
        request_timeout = max(1, min(30, int(max(1.0, float(timeout or 0.0)))))
        fallback_delay_s = (
            float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS or 0.0)
            if open_orders_fallback_delay is None
            else max(0.0, float(open_orders_fallback_delay or 0.0))
        )
        fallback_interval_s = (
            float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SECONDS or 0.05)
            if open_orders_fallback_interval is None
            else max(0.05, float(open_orders_fallback_interval or 0.05))
        )
        next_open_orders_at = started_at + fallback_delay_s
        confirmed: dict[str, dict] = {}
        failures: dict[str, dict] = {}
        ignored_warnings: dict[str, list[dict]] = {}

        def confirm_order(order_id: str, *, source: str, order: dict) -> None:
            if ignored_warnings.get(order_id):
                self.clear_order_error(order_id)
            payload = {
                "ok": True,
                "order_id": order_id,
                "source": source,
                "order": dict(order),
            }
            if ignored_warnings.get(order_id):
                payload["ignored_warnings"] = list(ignored_warnings[order_id])
            confirmed[order_id] = payload

        while time.time() < deadline and len(confirmed) + len(failures) < len(expected_ids):
            for order_id in expected_ids:
                if order_id in confirmed or order_id in failures:
                    continue
                order_error = self.get_order_error(order_id)
                if order_error:
                    if _order_error_is_submission_warning(order_error):
                        _remember_submission_warning(ignored_warnings, order_id, order_error)
                    else:
                        failures[order_id] = {
                            "ok": False,
                            "order_id": order_id,
                            "error": str(order_error.get("message") or "order_rejected"),
                            "details": order_error,
                        }
                        continue
                snapshot = self.get_order_snapshot(order_id)
                if snapshot:
                    confirm_order(order_id, source="snapshot", order=snapshot)

            missing_ids = [
                order_id
                for order_id in expected_ids
                if order_id not in confirmed and order_id not in failures
            ]
            if not missing_ids:
                break

            now = time.time()
            if now >= next_open_orders_at:
                try:
                    requester = getattr(self, "request_open_orders_for_order_confirmation", None)
                    if callable(requester):
                        open_orders = requester(timeout=request_timeout)
                    else:
                        open_orders = self.request_open_orders(timeout=request_timeout, force=True)
                except Exception as exc:
                    logger.debug("await_order_submissions reqOpenOrders failed for %s: %s", ",".join(missing_ids), exc)
                    open_orders = []
                for open_order in open_orders or []:
                    open_order_id = str(
                        open_order.get("orderId")
                        or open_order.get("order_id")
                        or open_order.get("id")
                        or ""
                    ).strip()
                    if open_order_id in missing_ids:
                        confirm_order(open_order_id, source="open_orders", order=dict(open_order))
                next_open_orders_at = time.time() + fallback_interval_s

            time.sleep(max(0.05, float(poll_interval or 0.2)))

        for order_id in expected_ids:
            if order_id in confirmed or order_id in failures:
                continue
            order_error = self.get_order_error(order_id)
            if order_error:
                if _order_error_is_submission_warning(order_error):
                    _remember_submission_warning(ignored_warnings, order_id, order_error)
                else:
                    failures[order_id] = {
                        "ok": False,
                        "order_id": order_id,
                        "error": str(order_error.get("message") or "order_rejected"),
                        "details": order_error,
                    }

        missing_ids = [
            order_id
            for order_id in expected_ids
            if order_id not in confirmed and order_id not in failures
        ]
        if missing_ids and not failures:
            try:
                requester = getattr(self, "request_open_orders_for_order_confirmation", None)
                if callable(requester):
                    open_orders = requester(timeout=request_timeout)
                else:
                    open_orders = self.request_open_orders(timeout=request_timeout, force=True)
            except Exception as exc:
                logger.debug("await_order_submissions final reqOpenOrders failed for %s: %s", ",".join(missing_ids), exc)
                open_orders = []
            for open_order in open_orders or []:
                open_order_id = str(
                    open_order.get("orderId")
                    or open_order.get("order_id")
                    or open_order.get("id")
                    or ""
                ).strip()
                if open_order_id in missing_ids:
                    confirm_order(open_order_id, source="open_orders", order=dict(open_order))
            missing_ids = [
                order_id
                for order_id in expected_ids
                if order_id not in confirmed and order_id not in failures
            ]
        if failures:
            first = next(iter(failures.values()))
            return {
                "ok": False,
                "error": str(first.get("error") or "order_submission_failed"),
                "orders": confirmed,
                "failures": failures,
                "missing_order_ids": missing_ids,
                "ignored_warnings": ignored_warnings,
            }
        if missing_ids:
            return {
                "ok": False,
                "error": "order_submission_unconfirmed",
                "orders": confirmed,
                "failures": failures,
                "missing_order_ids": missing_ids,
                "ignored_warnings": ignored_warnings,
            }
        return {
            "ok": True,
            "orders": confirmed,
            "failures": {},
            "missing_order_ids": [],
            "ignored_warnings": ignored_warnings,
        }

    def status(self) -> dict:
        with self._state_lock:
            now = time.time()
            self._recent_errors = [
                item
                for item in self._recent_errors[-20:]
                if now - float(item.get("ts", 0) or 0) <= 120
            ]
            return {
                "ready": bool(self._ready),
                "connected": bool(getattr(self, "isConnected", lambda: False)()),
                "status_code": int(self._status_code or 0),
                "account_data_circuit": self._account_data_circuit_snapshot_locked(now),
                "account_data_request_gate": self._account_data_gate_snapshot_locked(now),
                "account_data_pacing": self._account_request_pacing_snapshot_locked(now),
                "last_connect_at": (
                    datetime.fromtimestamp(self._last_connect_at, ET).isoformat()
                    if self._last_connect_at else ""
                ),
                "last_disconnect_at": (
                    datetime.fromtimestamp(self._last_disconnect_at, ET).isoformat()
                    if self._last_disconnect_at else ""
                ),
                "last_error_code": int(self._last_error_code or 0),
                "last_error": str(self._last_error_message or ""),
                "last_error_at": (
                    datetime.fromtimestamp(self._last_error_at, ET).isoformat()
                    if self._last_error_at else ""
                ),
                "recent_errors": [
                    {key: value for key, value in item.items() if key != "ts"}
                    for item in self._recent_errors
                ],
                "managed_accounts": str(self._managed_accounts or ""),
                "subscriptions": len(self._conid_to_ticker),
                "tick_by_tick_subscriptions": len(self._tick_by_tick_meta),
            }


from ibkr_compute.broker.ib_gateway_session import SocketSessionKeeper
from ibkr_compute.broker.ib_gateway_auth import AuthController


class BrokerAdapter:
    uses_internal_gateway_write_lock = True

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        client_id: int = DEFAULT_CLIENT_ID,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ):
        self.host = str(host or DEFAULT_HOST)
        self.port = int(port or DEFAULT_PORT)
        self.client_id = int(client_id or DEFAULT_CLIENT_ID)
        self.connect_timeout = max(3, int(connect_timeout or DEFAULT_CONNECT_TIMEOUT_SECONDS))
        self.client = _IBGatewayApp(self.host, self.port, self.client_id)
        self._recent_cancel_order_ids: set[str] = set()
        self._recent_cancel_until = 0.0
        self._recent_cancel_all_until = 0.0
        self._recent_cancel_lock = threading.RLock()

    def _recent_cancel_lock_ref(self) -> threading.RLock:
        lock = getattr(self, "_recent_cancel_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._recent_cancel_lock = lock
        return lock

    def _remember_recent_cancel_order_ids(self, order_ids: Iterable[Any], *, ttl: float | None = None) -> None:
        normalized = {str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()}
        if not normalized:
            return
        ttl_s = float(ttl if ttl is not None else BRACKET_BACKGROUND_CONFIRM_TIMEOUT_SECONDS + 30.0)
        with self._recent_cancel_lock_ref():
            current = {
                str(item or "").strip()
                for item in getattr(self, "_recent_cancel_order_ids", set())
                if str(item or "").strip()
            }
            current.update(normalized)
            self._recent_cancel_order_ids = current
            self._recent_cancel_until = max(float(getattr(self, "_recent_cancel_until", 0.0) or 0.0), time.time() + max(1.0, ttl_s))

    def _remember_recent_cancel_all(self, *, ttl: float | None = None) -> None:
        ttl_s = float(ttl if ttl is not None else BRACKET_BACKGROUND_CONFIRM_TIMEOUT_SECONDS + 30.0)
        with self._recent_cancel_lock_ref():
            self._recent_cancel_all_until = max(
                float(getattr(self, "_recent_cancel_all_until", 0.0) or 0.0),
                time.time() + max(1.0, ttl_s),
            )

    def _recent_cancel_all_active(self) -> bool:
        with self._recent_cancel_lock_ref():
            until = float(getattr(self, "_recent_cancel_all_until", 0.0) or 0.0)
            if time.time() <= until:
                return True
            self._recent_cancel_all_until = 0.0
        return False

    def _recent_cancel_overlaps_order_ids(self, order_ids: Iterable[Any]) -> bool:
        normalized = {str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()}
        if not normalized:
            return False
        with self._recent_cancel_lock_ref():
            until = float(getattr(self, "_recent_cancel_until", 0.0) or 0.0)
            if time.time() > until:
                self._recent_cancel_order_ids = set()
                self._recent_cancel_until = 0.0
                return False
            recent_ids = {
                str(item or "").strip()
                for item in getattr(self, "_recent_cancel_order_ids", set())
                if str(item or "").strip()
            }
        if normalized & recent_ids:
            return True
        terminal_statuses = {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}
        getter = getattr(self.client, "get_order_snapshot", None)
        if not callable(getter):
            return False
        for order_id in normalized:
            try:
                snapshot = dict(getter(order_id) or {})
            except Exception:
                snapshot = {}
            status = self._order_snapshot_status(snapshot)
            if status in terminal_statuses:
                return True
        return False

    @contextmanager
    def _gateway_write_lock(self, operation: str, *, environment: str = ""):
        operation_name = str(operation or "gateway_order_write").strip() or "gateway_order_write"
        started = time.perf_counter()
        _GLOBAL_GATEWAY_ORDER_WRITE_LOCK.acquire()
        wait_s = time.perf_counter() - started
        result_label = "ok"
        try:
            yield {"queue_wait_s": wait_s}
        except Exception:
            result_label = "error"
            raise
        finally:
            record_gateway_order_serial_event(
                environment=str(environment or ""),
                operation=operation_name,
                result=result_label,
                queue_wait_s=wait_s,
            )
            _GLOBAL_GATEWAY_ORDER_WRITE_LOCK.release()

    def _confirm_bracket_submission_background(
        self,
        *,
        order_ids: list[int],
        group: str,
        order_family_type: str,
        metric_environment: str = "",
    ) -> None:
        expected_ids = [str(item) for item in order_ids if str(item or "").strip()]
        if not expected_ids:
            return
        started = time.perf_counter()
        result = "error"
        reason = "order_submission_unconfirmed"
        try:
            confirmation = self.client.await_order_submissions(
                expected_ids,
                timeout=BRACKET_BACKGROUND_CONFIRM_TIMEOUT_SECONDS,
                poll_interval=0.5,
                open_orders_fallback_delay=5.0,
                open_orders_fallback_interval=5.0,
            )
            result = "ok" if confirmation.get("ok") else "error"
            reason = str(confirmation.get("error") or "ok")
            if not confirmation.get("ok"):
                if (
                    _submission_confirmation_is_cancel_cleanup_notice(confirmation)
                    or self._recent_cancel_overlaps_order_ids(expected_ids)
                    or (self._recent_cancel_all_active() and _submission_confirmation_has_missing_orders(confirmation))
                ):
                    result = "canceled"
                    reason = "order_canceled_before_background_confirm"
                    logger.info(
                        "Background bracket confirmation ended after cancellation: group=%s order_ids=%s result=%s",
                        group,
                        ",".join(expected_ids),
                        confirmation,
                    )
                else:
                    logger.warning(
                        "Background bracket confirmation failed: group=%s order_ids=%s result=%s",
                        group,
                        ",".join(expected_ids),
                        confirmation,
                    )
            else:
                logger.info("Background bracket confirmation complete: group=%s order_ids=%s", group, ",".join(expected_ids))
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            reason = exc.__class__.__name__
            logger.warning(
                "Background bracket confirmation crashed: group=%s order_ids=%s error=%s",
                group,
                ",".join(expected_ids),
                exc,
            )
        finally:
            record_order_event(
                environment=str(metric_environment or ""),
                operation="place_bracket_background_confirm",
                order_family_type=str(order_family_type or "bracket_oco"),
                result=result,
                reason_code=reason,
                duration_s=time.perf_counter() - started,
            )

    def _start_bracket_submission_background_confirmation(
        self,
        *,
        order_ids: list[int],
        group: str,
        order_family_type: str,
        metric_environment: str = "",
    ) -> None:
        thread = threading.Thread(
            target=self._confirm_bracket_submission_background,
            kwargs={
                "order_ids": list(order_ids or []),
                "group": str(group or ""),
                "order_family_type": str(order_family_type or "bracket_oco"),
                "metric_environment": str(metric_environment or ""),
            },
            name=f"bracket-confirm-{str(group or 'order')[:32]}",
            daemon=True,
        )
        thread.start()

    def connect(self) -> bool:
        return self.client.connect_and_start(timeout=self.connect_timeout)

    def disconnect(self):
        self.client.disconnect_and_stop()

    def status(self) -> dict:
        payload = self.client.status()
        payload.update(
            {
                "host": self.host,
                "port": self.port,
                "client_id": self.client_id,
                "ibapi_available": bool(IBAPI_AVAILABLE),
                "ibapi_import_error": IBAPI_IMPORT_ERROR,
            }
        )
        return payload

    def health(self) -> dict:
        try:
            ready = self.connect()
        except Exception as exc:
            return {
                "ok": False,
                "ready": False,
                "error": str(exc),
                **self.status(),
            }
        return {
            "ok": bool(ready),
            "ready": bool(ready),
            "authenticated": bool(ready),
            **self.status(),
        }

    def fresh_health_probe(self, reason: str = "", retries: int = FRESH_PROBE_RETRIES) -> dict:
        attempts = max(1, int(retries or FRESH_PROBE_RETRIES))
        last_payload: dict[str, Any] = {
            "ok": False,
            "ready": False,
            "authenticated": False,
            "status_code": 0,
            "last_error_code": 0,
            "last_error": "",
            "probe_client_id": 0,
        }
        for _ in range(attempts):
            probe_client_id = _next_fresh_probe_client_id(self.client_id)
            if reason:
                logger.info(
                    "Running fresh IB Gateway health probe: base_client_id=%s probe_client_id=%s reason=%s",
                    self.client_id,
                    probe_client_id,
                    reason,
                )
            probe = type(self)(
                host=self.host,
                port=self.port,
                client_id=probe_client_id,
                connect_timeout=self.connect_timeout,
            )
            try:
                payload = probe.health()
            finally:
                try:
                    probe.disconnect()
                except Exception:
                    logger.debug("Failed to stop fresh IB Gateway probe client_id=%s", probe_client_id, exc_info=True)
            payload = dict(payload or {})
            payload["probe_client_id"] = probe_client_id
            last_payload = payload
            if bool(payload.get("authenticated") or payload.get("ready")):
                return payload
            if int(payload.get("last_error_code", 0) or 0) != 326:
                return payload
        return last_payload

    def force_reconnect(self, reason: str = "", settle_seconds: float = 1.0) -> dict:
        if reason:
            logger.warning(
                "Forcing IB Gateway broker reconnect: client_id=%s reason=%s",
                self.client_id,
                reason,
            )
        try:
            self.disconnect()
        except Exception:
            logger.debug("Failed to disconnect broker before forced reconnect", exc_info=True)
        if settle_seconds and float(settle_seconds) > 0:
            time.sleep(max(0.0, float(settle_seconds)))
        return self.health()

    @staticmethod
    def _clear_legacy_order_flags(order: Any):
        if order is None:
            return
        if hasattr(order, "eTradeOnly"):
            order.eTradeOnly = False
        if hasattr(order, "firmQuoteOnly"):
            order.firmQuoteOnly = False

    @staticmethod
    def _normalize_tif_and_overnight_flag(tif: Any) -> tuple[str, bool]:
        text = str(tif or "").strip().upper()
        include_overnight = "OVERNIGHT" in text
        if not text:
            return "DAY", False
        if not include_overnight:
            return text, False
        cleaned = text.replace("OVERNIGHT", " ").replace("+", " ").replace("/", " ")
        for candidate in cleaned.split():
            candidate = candidate.strip().upper()
            if candidate in {"DAY", "GTC", "GTD", "IOC", "FOK", "OPG"}:
                return candidate, True
        return "DAY", True

    @staticmethod
    def _order_attr_truthy(order: Any, attr: str) -> bool:
        try:
            value = getattr(order, attr)
        except Exception:
            return False
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _apply_order_session_flags_for_submit(
        self,
        order: Any,
        *,
        tif: Any = "",
        include_overnight: bool = False,
    ) -> dict[str, Any]:
        normalized_tif, tif_requests_overnight = self._normalize_tif_and_overnight_flag(tif)
        order.tif = normalized_tif
        include_overnight_enabled = bool(include_overnight or tif_requests_overnight)
        include_overnight_supported = hasattr(order, "includeOvernight")
        if include_overnight_enabled:
            if include_overnight_supported:
                try:
                    order.includeOvernight = True
                except Exception:
                    include_overnight_supported = False
            if hasattr(order, "outsideRth"):
                try:
                    order.outsideRth = True
                except Exception:
                    pass
        return {
            "tif": normalized_tif,
            "include_overnight": include_overnight_enabled,
            "include_overnight_supported": include_overnight_supported,
        }

    @staticmethod
    def _sanitize_order_ref_group(value: Any, max_length: int = 180) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        safe_chars: list[str] = []
        last_was_separator = False
        for ch in text:
            if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}):
                safe_chars.append(ch)
                last_was_separator = ch in {"_", "-"}
            elif not last_was_separator:
                safe_chars.append("_")
                last_was_separator = True
        safe = "".join(safe_chars).strip("_-")
        if not safe:
            return ""
        max_length = max(16, int(max_length or 180))
        if len(safe) <= max_length:
            return safe
        digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:10]
        keep = max(1, max_length - len(digest) - 1)
        prefix = safe[:keep].rstrip("_-") or safe[:keep]
        return f"{prefix}_{digest}"

    @staticmethod
    def _normalize_order_price(price: float, min_tick: float = 0.01) -> float:
        try:
            value = Decimal(str(price or 0))
            tick = Decimal(str(min_tick or 0.01))
        except Exception:
            return 0.0
        if not value.is_finite() or not tick.is_finite() or value <= 0 or tick <= 0:
            return 0.0
        steps = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        normalized = (steps * tick).quantize(tick, rounding=ROUND_HALF_UP)
        return float(normalized)

    @classmethod
    def _price_normalization_details(cls, **prices: float) -> dict:
        details: dict[str, dict[str, float | bool]] = {}
        for name, raw_price in prices.items():
            raw = _safe_float(raw_price, 0.0)
            normalized = cls._normalize_order_price(raw)
            details[name] = {
                "raw": raw,
                "normalized": normalized,
                "changed": abs(raw - normalized) > 0.0000001,
            }
        return details

    @staticmethod
    def _contract_text(value: Any) -> str:
        return str(value or "").strip().upper()

    @classmethod
    def _build_order_contract(cls, contract_info: dict, *, conid: int = 0, symbol: str = "") -> Any:
        contract_payload = dict(contract_info or {})
        contract = Contract()
        contract.conId = int(contract_payload.get("conid") or conid or 0)
        contract.symbol = str(contract_payload.get("symbol") or symbol or "").strip()
        contract.secType = cls._contract_text(contract_payload.get("sec_type") or "STK")
        contract.currency = cls._contract_text(contract_payload.get("currency") or "USD")

        requested_exchange = cls._contract_text(contract_payload.get("exchange") or "SMART")
        primary_exchange = cls._contract_text(
            contract_payload.get("primary_exchange") or contract_payload.get("listing_exchange")
        )
        if not primary_exchange and requested_exchange and requested_exchange != "SMART":
            primary_exchange = requested_exchange

        if contract.secType in SMART_ROUTED_US_SEC_TYPES and contract.currency == "USD":
            contract.exchange = "SMART"
            if primary_exchange and primary_exchange != "SMART":
                contract.primaryExchange = primary_exchange
        else:
            contract.exchange = requested_exchange or primary_exchange or "SMART"
        return contract

    @staticmethod
    def _contract_exchange_values(item: dict) -> set[str]:
        values: set[str] = set()
        for key in ("exchange", "primary_exchange", "listing_exchange"):
            text = str((item or {}).get(key) or "").strip().upper()
            if text:
                values.add(text)
        valid_exchanges = str((item or {}).get("valid_exchanges") or "")
        for part in valid_exchanges.replace(";", ",").split(","):
            text = str(part or "").strip().upper()
            if text:
                values.add(text)
        return values

    @classmethod
    def _contract_matches(
        cls,
        item: dict,
        *,
        symbol: str = "",
        exchange: str = "",
        sec_type: str = "",
    ) -> bool:
        if not item:
            return False
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        normalized_sec_type = str(sec_type or "").strip().upper()
        if normalized_symbol and str(item.get("symbol") or "").strip().upper() != normalized_symbol:
            return False
        if normalized_sec_type and str(item.get("sec_type") or "").strip().upper() != normalized_sec_type:
            return False
        if normalized_exchange and normalized_exchange not in cls._contract_exchange_values(item):
            return False
        return True

    @classmethod
    def _contract_has_preferred_exchange(cls, item: dict) -> bool:
        values = cls._contract_exchange_values(item)
        return any(exchange in values for exchange in PREFERRED_CONTRACT_EXCHANGES)

    @classmethod
    def _contract_rank(
        cls,
        item: dict,
        *,
        symbol: str = "",
        exchange: str = "",
        sec_type: str = "",
        conid: int = 0,
    ) -> int:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        normalized_sec_type = str(sec_type or "").strip().upper()
        item_symbol = str(item.get("symbol") or "").strip().upper()
        item_sec_type = str(item.get("sec_type") or "").strip().upper()
        item_conid = int(item.get("conid") or 0)
        exchange_values = cls._contract_exchange_values(item)

        score = 0
        if normalized_symbol:
            score += 1000 if item_symbol == normalized_symbol else -1000
        if normalized_exchange:
            score += 300 if normalized_exchange in exchange_values else -300
        else:
            preferred_count = len(PREFERRED_CONTRACT_EXCHANGES)
            for idx, preferred in enumerate(PREFERRED_CONTRACT_EXCHANGES):
                if preferred in exchange_values:
                    score += preferred_count - idx
                    break
        if normalized_sec_type:
            score += 200 if item_sec_type == normalized_sec_type else -100
        else:
            score += PREFERRED_CONTRACT_SEC_TYPE_SCORE.get(item_sec_type, 0)
        if int(conid or 0) > 0 and item_conid == int(conid):
            score += 10
        return score

    @classmethod
    def _select_contract_candidate(
        cls,
        items: Iterable[dict],
        *,
        symbol: str = "",
        exchange: str = "",
        sec_type: str = "",
        conid: int = 0,
    ) -> Optional[dict]:
        candidates = [dict(item) for item in (items or []) if int((item or {}).get("conid") or 0) > 0]
        if not candidates:
            return None
        strict = [
            item
            for item in candidates
            if cls._contract_matches(item, symbol=symbol, exchange=exchange, sec_type=sec_type)
        ]
        pool = strict
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_sec_type = str(sec_type or "").strip().upper()
        if not pool and normalized_symbol:
            pool = [
                item
                for item in candidates
                if str(item.get("symbol") or "").strip().upper() == normalized_symbol
                and (
                    not normalized_sec_type
                    or str(item.get("sec_type") or "").strip().upper() == normalized_sec_type
                )
            ]
        if not pool:
            pool = candidates
        return max(
            pool,
            key=lambda item: cls._contract_rank(
                item,
                symbol=symbol,
                exchange=exchange,
                sec_type=sec_type,
                conid=conid,
            ),
        )

    def add_market_data_listener(self, callback: Callable[[dict], None]):
        self.client.add_market_data_listener(callback)

    def remove_market_data_listener(self, callback: Callable[[dict], None]):
        self.client.remove_market_data_listener(callback)

    def add_order_update_listener(self, callback: Callable[[dict], None]):
        self.client.add_order_update_listener(callback)

    def remove_order_update_listener(self, callback: Callable[[dict], None]):
        self.client.remove_order_update_listener(callback)

    def add_execution_fill_listener(self, callback: Callable[[dict], None]):
        self.client.add_execution_fill_listener(callback)

    def remove_execution_fill_listener(self, callback: Callable[[dict], None]):
        self.client.remove_execution_fill_listener(callback)

    def resolve_contract(
        self,
        symbol: str = "",
        conid: int = 0,
        exchange: str = "",
        sec_type: str = "",
    ) -> Optional[dict]:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        normalized_sec_type = str(sec_type or "").strip().upper()
        target_conid = int(conid or 0)
        lookup_errors: list[str] = []

        cached_candidates = []
        if normalized_symbol and normalized_symbol in self.client._contract_cache_by_symbol:
            cached_candidates.append(dict(self.client._contract_cache_by_symbol[normalized_symbol]))
        if target_conid > 0 and target_conid in self.client._contract_cache_by_conid:
            cached_candidates.append(dict(self.client._contract_cache_by_conid[target_conid]))
        cached = self._select_contract_candidate(
            cached_candidates,
            symbol=normalized_symbol,
            exchange=normalized_exchange,
            sec_type=normalized_sec_type,
            conid=target_conid,
        )
        if cached and self._contract_matches(
            cached,
            symbol=normalized_symbol,
            exchange=normalized_exchange,
            sec_type=normalized_sec_type,
        ):
            if normalized_exchange or not normalized_symbol or self._contract_has_preferred_exchange(cached):
                return dict(cached)

        if (
            ORDER_CONID_FAST_CONTRACT_ENABLED
            and target_conid > 0
            and normalized_symbol
            and not normalized_exchange
            and not normalized_sec_type
        ):
            # For order placement, conId + SMART stock contract is enough and
            # avoids serial reqContractDetails calls during burst submissions.
            return {
                "conid": target_conid,
                "conidEx": target_conid,
                "symbol": normalized_symbol,
                "sec_type": "STK",
                "exchange": "SMART",
                "currency": "USD",
                "primary_exchange": "",
            }

        if target_conid > 0:
            try:
                details = self.client.request_contract_details(conid=target_conid)
            except Exception as exc:
                lookup_errors.append(f"contract_details_conid:{exc}")
                details = []
            selected = self._select_contract_candidate(
                details,
                symbol=normalized_symbol,
                exchange=normalized_exchange,
                sec_type=normalized_sec_type,
                conid=target_conid,
            )
            if selected and (
                not normalized_symbol
                or (
                    self._contract_matches(
                        selected,
                        symbol=normalized_symbol,
                        exchange=normalized_exchange,
                        sec_type=normalized_sec_type,
                    )
                    and (normalized_exchange or self._contract_has_preferred_exchange(selected))
                )
            ):
                return dict(selected)

        if not normalized_symbol:
            return None

        try:
            samples = self.client.request_matching_symbols(normalized_symbol)
        except Exception as exc:
            lookup_errors.append(f"matching_symbols:{exc}")
            samples = []

        best = self._select_contract_candidate(
            samples,
            symbol=normalized_symbol,
            exchange=normalized_exchange,
            sec_type=normalized_sec_type,
            conid=target_conid,
        )
        if best:
            try:
                details = self.client.request_contract_details(conid=int(best.get("conid") or 0))
            except Exception as exc:
                lookup_errors.append(f"contract_details_match:{exc}")
                details = []
            selected = self._select_contract_candidate(
                details,
                symbol=normalized_symbol,
                exchange=normalized_exchange,
                sec_type=normalized_sec_type,
                conid=int(best.get("conid") or 0),
            )
            if selected and self._contract_matches(
                selected,
                symbol=normalized_symbol,
                exchange=normalized_exchange,
                sec_type=normalized_sec_type,
            ):
                return dict(selected)
            return dict(best)
        if lookup_errors:
            target = normalized_symbol or str(target_conid or "")
            raise RuntimeError(f"contract_lookup_failed:{target}:{'; '.join(lookup_errors)[:600]}")
        return None

    def search_contracts(self, query: str, limit: int = 10) -> List[dict]:
        text = str(query or "").strip()
        if not text:
            return []
        try:
            items = self.client.request_matching_symbols(text)
        except Exception as exc:
            logger.warning("Contract search failed for %s: %s", text, exc)
            return []
        results = []
        for item in items[: max(1, int(limit))]:
            results.append(
                {
                    "symbol": str(item.get("symbol") or "").upper(),
                    "conid": int(item.get("conid") or 0),
                    "sec_type": str(item.get("sec_type") or "").upper(),
                    "asset_class": str(item.get("sec_type") or "").upper(),
                    "exchange": str(item.get("exchange") or "").upper(),
                    "description": str(item.get("description") or ""),
                    "score": 100 if str(item.get("symbol") or "").upper() == text.upper() else 80,
                }
            )
        return results

    def request_historical_bars(
        self,
        *,
        conid: int,
        symbol: str,
        exchange: str = "",
        sec_type: str = "",
        duration: str,
        bar_size: str,
        end_datetime: str = "",
        use_rth: bool = False,
        timeout: int = 30,
    ) -> List[dict]:
        started = time.perf_counter()
        contract = self.resolve_contract(symbol=symbol, conid=conid, exchange=exchange, sec_type=sec_type)
        if not contract:
            record_broker_request(
                self.client,
                request_kind="historical_bars",
                result="error",
                duration_s=time.perf_counter() - started,
                error_class="contract_not_found",
            )
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        try:
            rows = self.client.request_historical_bars(
                conid=int(contract.get("conid") or conid),
                symbol=str(contract.get("symbol") or symbol),
                exchange=str(contract.get("exchange") or exchange or ""),
                sec_type=str(contract.get("sec_type") or sec_type or ""),
                duration=duration,
                bar_size=bar_size,
                end_datetime=end_datetime,
                use_rth=use_rth,
                timeout=timeout,
                contract_details=contract,
            )
        except Exception as exc:
            record_broker_request(
                self.client,
                request_kind="historical_bars",
                result="error",
                duration_s=time.perf_counter() - started,
                error_class=exc.__class__.__name__,
            )
            raise
        record_broker_request(self.client, request_kind="historical_bars", result="ok", duration_s=time.perf_counter() - started)
        return rows

    def request_market_data_snapshot(
        self,
        *,
        conid: int = 0,
        symbol: str = "",
        exchange: str = "SMART",
        sec_type: str = "",
        timeout: float = 3.0,
    ) -> Dict[str, Any]:
        try:
            contract = self.resolve_contract(symbol=symbol, conid=conid, exchange=exchange, sec_type=sec_type)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "source": "ibkr_market_data_snapshot",
                "quote": {},
                "payload": {},
            }
        if not contract:
            return {
                "ok": False,
                "error": f"contract_not_found:{symbol or conid}",
                "source": "ibkr_market_data_snapshot",
                "quote": {},
                "payload": {},
            }
        return self.client.request_market_data_snapshot(
            conid=int(contract.get("conid") or conid or 0),
            symbol=str(contract.get("symbol") or symbol or "").upper(),
            exchange=str(contract.get("exchange") or exchange or "SMART"),
            sec_type=str(contract.get("sec_type") or sec_type or "STK"),
            currency=str(contract.get("currency") or "USD"),
            timeout=timeout,
            contract_details=contract,
        )

    def subscribe_market_data(self, conid: int, symbol: str, exchange: str = "SMART") -> int:
        contract = self.resolve_contract(symbol=symbol, conid=conid, exchange=exchange)
        if not contract:
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        self.connect()
        ticker_ids = self.client.next_ticker_ids(1)
        ticker_id = int(ticker_ids[0])
        ib_contract = Contract()
        ib_contract.conId = int(contract.get("conid") or conid)
        ib_contract.symbol = str(contract.get("symbol") or symbol)
        ib_contract.secType = str(contract.get("sec_type") or "STK")
        ib_contract.exchange = str(contract.get("exchange") or exchange or "SMART")
        ib_contract.currency = str(contract.get("currency") or "USD")
        self.client._ticker_meta[ticker_id] = {
            "tickerId": ticker_id,
            "conid": int(ib_contract.conId),
            "conidEx": int(ib_contract.conId),
            "symbol": str(ib_contract.symbol).upper(),
        }
        self.client._ticker_payloads[ticker_id] = dict(self.client._ticker_meta[ticker_id])
        self.client._conid_to_ticker[int(ib_contract.conId)] = ticker_id
        try:
            self.client.reqMarketDataType(1)
        except Exception:
            logger.debug("reqMarketDataType(1) failed before subscribing conid=%s", ib_contract.conId, exc_info=True)
        self.client.reqMktData(ticker_id, ib_contract, "233", False, False, [])
        return ticker_id

    def unsubscribe_market_data(self, conid: int):
        ticker_id = self.client._conid_to_ticker.pop(int(conid or 0), None)
        if ticker_id is None:
            return
        try:
            self.client.cancelMktData(int(ticker_id))
        except Exception:
            logger.exception("cancelMktData failed for conid=%s ticker=%s", conid, ticker_id)
        self.client._ticker_meta.pop(int(ticker_id), None)
        self.client._ticker_payloads.pop(int(ticker_id), None)

    @staticmethod
    def _normalize_tick_by_tick_type(tick_type: str = "Last") -> str:
        normalized = str(tick_type or "Last").strip() or "Last"
        if normalized.lower() != "last":
            raise ValueError(f"unsupported_tick_by_tick_type:{normalized}")
        return "Last"

    def subscribe_tick_by_tick(
        self,
        conid: int,
        symbol: str = "",
        exchange: str = "SMART",
        tick_type: str = "Last",
    ) -> int:
        normalized_tick_type = self._normalize_tick_by_tick_type(tick_type)
        contract = self.resolve_contract(symbol=symbol, conid=conid, exchange=exchange)
        if not contract:
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        self.connect()
        if not callable(getattr(self.client, "reqTickByTickData", None)):
            raise RuntimeError("req_tick_by_tick_unavailable")
        normalized_conid = int(contract.get("conid") or conid)
        key = (normalized_conid, normalized_tick_type.lower())
        now_ts = time.time()
        with self.client._state_lock:
            existing = self.client._tick_by_tick_by_conid.get(key)
            if existing and existing in self.client._tick_by_tick_meta:
                return int(existing)
            last_request_at = float(self.client._tick_by_tick_last_request_at.get(key, 0.0) or 0.0)
            if now_ts - last_request_at < TICK_BY_TICK_DUPLICATE_WINDOW_SECONDS:
                raise RuntimeError(f"tick_by_tick_duplicate_cooldown:{normalized_conid}:{normalized_tick_type}")
            req_id = int(self.client.next_ticker_ids(1)[0])
            self.client._tick_by_tick_last_request_at[key] = now_ts

        ib_contract = Contract()
        ib_contract.conId = normalized_conid
        ib_contract.symbol = str(contract.get("symbol") or symbol)
        ib_contract.secType = str(contract.get("sec_type") or "STK")
        ib_contract.exchange = str(contract.get("exchange") or exchange or "SMART")
        ib_contract.currency = str(contract.get("currency") or "USD")
        meta = {
            "tickerId": req_id,
            "reqId": req_id,
            "conid": int(ib_contract.conId),
            "conidEx": int(ib_contract.conId),
            "symbol": str(ib_contract.symbol).upper(),
            "tick_type": normalized_tick_type,
        }
        with self.client._state_lock:
            self.client._tick_by_tick_meta[req_id] = meta
            self.client._tick_by_tick_by_conid[key] = req_id
        try:
            self.client.reqTickByTickData(req_id, ib_contract, normalized_tick_type, 0, False)
        except Exception:
            with self.client._state_lock:
                self.client._tick_by_tick_meta.pop(req_id, None)
                self.client._tick_by_tick_by_conid.pop(key, None)
            raise
        return req_id

    def unsubscribe_tick_by_tick(self, conid: int, tick_type: str = ""):
        normalized_conid = int(conid or 0)
        normalized_tick_type = str(tick_type or "").strip().lower()
        now_ts = time.time()
        with self.client._state_lock:
            reqs = [
                (key, int(req_id))
                for key, req_id in list(self.client._tick_by_tick_by_conid.items())
                if key[0] == normalized_conid
                and (not normalized_tick_type or key[1] == normalized_tick_type)
            ]
            for key, req_id in reqs:
                self.client._tick_by_tick_by_conid.pop(key, None)
                self.client._tick_by_tick_meta.pop(int(req_id), None)
                self.client._tick_by_tick_last_request_at[key] = now_ts
        for _key, req_id in reqs:
            try:
                self.client.cancelTickByTickData(int(req_id))
            except Exception:
                logger.exception("cancelTickByTickData failed for conid=%s req_id=%s", conid, req_id)

    def list_tick_by_tick_subscriptions(self) -> List[dict]:
        with self.client._state_lock:
            return sorted(
                [dict(item) for item in self.client._tick_by_tick_meta.values()],
                key=lambda item: (
                    int(item.get("conid") or 0),
                    str(item.get("tick_type") or ""),
                    int(item.get("reqId") or 0),
                ),
            )

    def list_positions(self) -> List[dict]:
        return self.client.request_positions()

    def get_account_summary(self) -> Dict[str, dict]:
        return self.client.request_account_summary()

    def get_account_snapshot(self, account: str = "") -> Dict[str, Any]:
        return self.client.request_account_updates(account=account)

    def get_account_pnl(self, account: str = "", model_code: str = "") -> Dict[str, Any]:
        return self.client.request_account_pnl(account=account, model_code=model_code)

    def list_open_orders(self, *, include_all: bool = False, force: bool = False, timeout: float | None = None) -> List[dict]:
        request_open_orders = getattr(self.client, "request_open_orders", None)
        if not callable(request_open_orders):
            return []
        request_timeout = max(1, min(5, int(max(1.0, float(timeout or DEFAULT_CONNECT_TIMEOUT_SECONDS)))))
        try:
            return list(request_open_orders(timeout=request_timeout, include_all=include_all, force=force) or [])
        except TypeError:
            try:
                return list(request_open_orders(include_all=include_all, force=force) or [])
            except TypeError:
                return list(request_open_orders(include_all=include_all) or [])

    def list_cached_open_orders(self, *, include_all: bool = False) -> List[dict]:
        getter = getattr(self.client, "get_order_snapshots", None)
        if not callable(getter):
            return []
        try:
            return list(getter(include_all=include_all) or [])
        except TypeError:
            return list(getter() or [])

    def list_recent_fills(self) -> List[dict]:
        return self.client.request_executions()

    @staticmethod
    def _order_snapshot_id(snapshot: dict | None) -> str:
        snapshot = snapshot or {}
        return str(snapshot.get("orderId") or snapshot.get("order_id") or snapshot.get("id") or "").strip()

    @staticmethod
    def _order_snapshot_status(snapshot: dict | None) -> str:
        snapshot = snapshot or {}
        return str(snapshot.get("status") or snapshot.get("order_status") or snapshot.get("orderStatus") or "").strip().upper()

    @staticmethod
    def _order_error_is_cancelled(order_error: dict | None) -> bool:
        order_error = order_error or {}
        try:
            code = int(order_error.get("code") or 0)
        except Exception:
            code = 0
        message = str(order_error.get("message") or order_error.get("error") or "").strip().lower()
        return code == 202 or "order canceled" in message or "order cancelled" in message

    @staticmethod
    def _order_error_is_cancel_terminal_notice(order_error: dict | None) -> bool:
        return _order_error_is_cancel_notice(order_error)

    @staticmethod
    def _order_error_is_stale_invalid_price_rejection(order_error: dict | None) -> bool:
        order_error = order_error or {}
        try:
            code = int(order_error.get("code") or 0)
        except Exception:
            code = 0
        message = str(order_error.get("message") or order_error.get("error") or "").strip().lower()
        return code == 201 and "invalid price" in message

    @staticmethod
    def _order_snapshot_remaining(snapshot: dict | None) -> float:
        snapshot = snapshot or {}
        return _safe_float(
            snapshot.get("remainingQuantity")
            or snapshot.get("remaining")
            or snapshot.get("remaining_quantity"),
            0.0,
        )

    @staticmethod
    def _order_snapshot_filled(snapshot: dict | None) -> float:
        snapshot = snapshot or {}
        return _safe_float(
            snapshot.get("filledQuantity")
            or snapshot.get("filled")
            or snapshot.get("filled_qty")
            or snapshot.get("filled_quantity"),
            0.0,
        )

    @staticmethod
    def _order_snapshot_has_fill_evidence(snapshot: dict | None) -> bool:
        snapshot = snapshot or {}
        if BrokerAdapter._order_snapshot_status(snapshot) in {"FILLED", "EXECUTED"}:
            return True
        if BrokerAdapter._order_snapshot_filled(snapshot) > 1e-9:
            return True
        return any(
            str(snapshot.get(field) or "").strip()
            for field in ("ib_exec_id", "execId", "execution_id", "lastExecutionTime")
        )

    @staticmethod
    def _order_price_matches(snapshot: dict | None, fields: Iterable[str], expected_price: float, tolerance: float = 0.005) -> bool:
        snapshot = snapshot or {}
        expected = float(expected_price or 0.0)
        if expected <= 0:
            return False
        for field in fields or []:
            value = _safe_float(snapshot.get(field), 0.0)
            if value > 0 and abs(value - expected) <= tolerance:
                return True
        return False

    def _request_order_snapshot(
        self,
        order_id: str,
        *,
        timeout: float = 3.0,
        include_all: bool = True,
        refresh: bool = True,
        force: bool = False,
    ) -> tuple[dict, bool, bool]:
        normalized_order_id = str(order_id or "").strip()
        snapshot: dict = {}
        getter = getattr(self.client, "get_order_snapshot", None)
        if callable(getter):
            try:
                snapshot = dict(getter(normalized_order_id) or {})
            except Exception:
                snapshot = {}

        if not refresh:
            return snapshot, False, False

        request_open_orders_for_confirmation = getattr(self.client, "request_open_orders_for_order_confirmation", None)
        request_open_orders = getattr(self.client, "request_open_orders", None)
        if not callable(request_open_orders_for_confirmation) and not callable(request_open_orders):
            return snapshot, False, False

        refreshed = False
        try:
            if callable(request_open_orders_for_confirmation):
                request_timeout = max(1, min(3, int(max(1.0, float(timeout or 0.0)))))
                try:
                    open_orders = request_open_orders_for_confirmation(
                        timeout=request_timeout,
                        include_all=include_all,
                        force=force,
                    )
                except TypeError:
                    open_orders = request_open_orders_for_confirmation(
                        timeout=request_timeout,
                        include_all=include_all,
                    )
            else:
                try:
                    open_orders = request_open_orders(
                        timeout=max(1, min(3, int(max(1.0, float(timeout or 0.0))))),
                        include_all=include_all,
                        force=True,
                    )
                except TypeError:
                    open_orders = request_open_orders(include_all=include_all)
            refreshed = True
        except Exception as exc:
            logger.debug("request_open_orders failed while awaiting order %s: %s", normalized_order_id, exc)
            return snapshot, False, False

        for item in open_orders or []:
            if self._order_snapshot_id(item) == normalized_order_id:
                return dict(item), True, refreshed

        if callable(getter):
            try:
                refreshed_snapshot = dict(getter(normalized_order_id) or {})
                if refreshed_snapshot:
                    snapshot = {**snapshot, **refreshed_snapshot}
            except Exception:
                pass
        return snapshot, False, refreshed

    def _mark_client_order_terminal(self, order_id: str, *, status: str = "CANCELLED", reason: str = "") -> dict:
        marker = getattr(self.client, "mark_order_terminal", None)
        if not callable(marker):
            return {}
        try:
            return dict(marker(str(order_id or "").strip(), status=status, reason=reason) or {})
        except TypeError:
            try:
                return dict(marker(str(order_id or "").strip(), status=status) or {})
            except Exception:
                return {}
        except Exception:
            return {}

    def await_order_cancelled(
        self,
        order_id: str,
        *,
        timeout: float = 3.0,
        poll_interval: float = 0.2,
    ) -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {"ok": False, "error": "missing_order_id"}
        started_at = time.time()
        deadline = started_at + max(0.5, float(timeout or 0.0))
        next_open_orders_at = started_at + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS or 0.0)
        last_snapshot: dict = {}
        ignored_errors: list[dict] = []

        def open_orders_missing_result(snapshot: dict, *, source: str) -> dict:
            self._mark_client_order_terminal(normalized_order_id, status="CANCELLED", reason=source)
            return {
                "ok": True,
                "order_id": normalized_order_id,
                "status": "NOT_OPEN",
                "order": dict(snapshot or {}),
                "source": source,
                "ignored_errors": ignored_errors,
            }

        def cancel_request_pending_result(snapshot: dict) -> dict:
            return {
                "ok": True,
                "order_id": normalized_order_id,
                "status": "CANCEL_REQUESTED",
                "order": dict(snapshot or {}),
                "source": "cancel_request_submitted_unconfirmed",
                "pending_confirmation": True,
                "ignored_errors": ignored_errors,
            }

        while time.time() < deadline:
            snapshot, found_open, refreshed = self._request_order_snapshot(
                normalized_order_id,
                timeout=timeout,
                include_all=True,
                refresh=False,
            )
            if snapshot:
                last_snapshot = dict(snapshot)
            status = self._order_snapshot_status(snapshot)
            order_error = {}
            getter = getattr(self.client, "get_order_error", None)
            if callable(getter):
                try:
                    order_error = dict(getter(normalized_order_id) or {})
                except Exception:
                    order_error = {}
            if order_error:
                if self._order_error_is_cancelled(order_error):
                    clearer = getattr(self.client, "clear_order_error", None)
                    if callable(clearer):
                        try:
                            clearer(normalized_order_id)
                        except Exception:
                            pass
                    self._mark_client_order_terminal(
                        normalized_order_id,
                        status="CANCELLED",
                        reason="cancel_terminal_notice",
                    )
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "status": "CANCELLED",
                        "details": order_error,
                        "ignored_errors": ignored_errors,
                    }
                if self._order_error_is_stale_invalid_price_rejection(order_error):
                    ignored_errors.append(dict(order_error))
                    clearer = getattr(self.client, "clear_order_error", None)
                    if callable(clearer):
                        try:
                            clearer(normalized_order_id)
                        except Exception:
                            pass
                elif self._order_error_is_cancel_terminal_notice(order_error):
                    ignored_errors.append(dict(order_error))
                    clearer = getattr(self.client, "clear_order_error", None)
                    if callable(clearer):
                        try:
                            clearer(normalized_order_id)
                        except Exception:
                            pass
                    terminal_status = "NOT_OPEN" if _order_error_code(order_error) == 10147 else "CANCELLED"
                    self._mark_client_order_terminal(
                        normalized_order_id,
                        status=terminal_status,
                        reason="cancel_terminal_notice",
                    )
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "status": terminal_status,
                        "order": snapshot,
                        "details": order_error,
                        "source": "cancel_terminal_notice",
                        "ignored_errors": ignored_errors,
                    }
                else:
                    return {
                        "ok": False,
                        "order_id": normalized_order_id,
                        "error": str(order_error.get("message") or "order_cancel_rejected"),
                        "details": order_error,
                    }

            if self._order_snapshot_has_fill_evidence(snapshot):
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": "order_filled_during_cancel",
                    "order": snapshot,
                    "ignored_errors": ignored_errors,
                }
            if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                self._mark_client_order_terminal(normalized_order_id, status=status, reason="cancel_status_callback")
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "status": status,
                    "order": snapshot,
                    "ignored_errors": ignored_errors,
                }

            now = time.time()
            if CANCEL_CONFIRM_OPEN_ORDERS_RECONCILE_ENABLED and now >= next_open_orders_at:
                snapshot, found_open, refreshed = self._request_order_snapshot(
                    normalized_order_id,
                    timeout=timeout,
                    include_all=True,
                )
                next_open_orders_at = time.time() + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SECONDS or 0.05)
                if snapshot:
                    last_snapshot = dict(snapshot)
                status = self._order_snapshot_status(snapshot)
                if status in {"FILLED", "EXECUTED"}:
                    return {
                        "ok": False,
                        "order_id": normalized_order_id,
                        "error": "order_filled_during_cancel",
                        "order": snapshot,
                    }
                if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                    self._mark_client_order_terminal(normalized_order_id, status=status, reason="cancel_status_callback")
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "status": status,
                        "order": snapshot,
                        "ignored_errors": ignored_errors,
                    }
                if refreshed and not found_open:
                    if self._order_snapshot_has_fill_evidence(snapshot):
                        return {
                            "ok": False,
                            "order_id": normalized_order_id,
                            "error": "order_filled_during_cancel",
                            "order": snapshot,
                        }
                    # reqAllOpenOrders is the authoritative open-order set for
                    # cancel confirmation; callback snapshots can remain stale
                    # during paper Gateway bursts even after the order is gone.
                    return open_orders_missing_result(
                        snapshot,
                        source="open_orders_missing" if not snapshot else "open_orders_missing_stale_snapshot",
                    )
            time.sleep(max(0.05, float(poll_interval or 0.2)))
        if CANCEL_CONFIRM_OPEN_ORDERS_RECONCILE_ENABLED:
            snapshot, found_open, refreshed = self._request_order_snapshot(
                normalized_order_id,
                timeout=max(3.0, float(timeout or 0.0)),
                include_all=True,
                force=True,
            )
            if snapshot:
                last_snapshot = dict(snapshot)
            status = self._order_snapshot_status(snapshot)
            if self._order_snapshot_has_fill_evidence(snapshot):
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": "order_filled_during_cancel",
                    "order": snapshot,
                    "ignored_errors": ignored_errors,
                }
            if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                self._mark_client_order_terminal(normalized_order_id, status=status, reason="cancel_status_callback")
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "status": status,
                    "order": snapshot,
                    "ignored_errors": ignored_errors,
                }
            if refreshed and not found_open:
                return open_orders_missing_result(
                    snapshot,
                    source="final_open_orders_missing" if not snapshot else "final_open_orders_missing_stale_snapshot",
                )

        if self._order_snapshot_has_fill_evidence(last_snapshot):
            return {
                "ok": False,
                "order_id": normalized_order_id,
                "error": "order_filled_during_cancel",
                "order": last_snapshot,
                "ignored_errors": ignored_errors,
            }
        status = self._order_snapshot_status(last_snapshot)
        if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
            self._mark_client_order_terminal(normalized_order_id, status=status, reason="cancel_status_callback")
            return {
                "ok": True,
                "order_id": normalized_order_id,
                "status": status,
                "order": last_snapshot,
                "ignored_errors": ignored_errors,
            }
        return cancel_request_pending_result(last_snapshot)

    def await_order_price_update(
        self,
        order_id: str,
        *,
        expected_price: float,
        fields: Iterable[str],
        timeout: float = 3.0,
        poll_interval: float = 0.2,
    ) -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {"ok": False, "error": "missing_order_id"}
        started_at = time.time()
        deadline = started_at + max(0.5, float(timeout or 0.0))
        next_open_orders_at = started_at + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS or 0.0)
        last_snapshot: dict = {}
        while time.time() < deadline:
            order_error = {}
            getter = getattr(self.client, "get_order_error", None)
            if callable(getter):
                try:
                    order_error = dict(getter(normalized_order_id) or {})
                except Exception:
                    order_error = {}
            if order_error:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": str(order_error.get("message") or "order_modify_rejected"),
                    "details": order_error,
                }
            snapshot, _found_open, _refreshed = self._request_order_snapshot(
                normalized_order_id,
                timeout=timeout,
                include_all=True,
                refresh=False,
            )
            if snapshot:
                last_snapshot = dict(snapshot)
            status = self._order_snapshot_status(snapshot)
            if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": "order_not_modifiable",
                    "order": snapshot,
                }
            if self._order_price_matches(snapshot, fields, float(expected_price or 0.0)):
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "order": snapshot,
                }
            now = time.time()
            if now >= next_open_orders_at:
                snapshot, _found_open, _refreshed = self._request_order_snapshot(
                    normalized_order_id,
                    timeout=timeout,
                    include_all=True,
                )
                next_open_orders_at = time.time() + float(ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_INTERVAL_SECONDS or 0.05)
                if snapshot:
                    last_snapshot = dict(snapshot)
                status = self._order_snapshot_status(snapshot)
                if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                    return {
                        "ok": False,
                        "order_id": normalized_order_id,
                        "error": "order_not_modifiable",
                        "order": snapshot,
                    }
                if self._order_price_matches(snapshot, fields, float(expected_price or 0.0)):
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "order": snapshot,
                    }
            time.sleep(max(0.05, float(poll_interval or 0.2)))
        return {
            "ok": False,
            "order_id": normalized_order_id,
            "error": "order_modify_price_unconfirmed",
            "order": last_snapshot,
            "expected_price": float(expected_price or 0.0),
            "fields": list(fields or []),
        }

    def _get_order_objects_for_modify(self, order_id: str) -> tuple[Any, Any, dict]:
        normalized_order_id = str(order_id or "").strip()
        getter = getattr(self.client, "get_order_objects", None)
        if not normalized_order_id or not callable(getter):
            return None, None, {"source": "unavailable", "attempts": 0}
        try:
            contract, order = getter(normalized_order_id)
        except Exception:
            contract, order = None, None
        if contract and order:
            return contract, order, {"source": "callback_cache", "attempts": 0}

        deadline = time.time() + max(0.0, float(MODIFY_ORDER_OBJECT_LOOKUP_TIMEOUT_SECONDS or 0.0))
        attempts = 0
        last_error = ""
        while time.time() <= deadline:
            attempts += 1
            remaining = max(0.2, deadline - time.time())
            request_timeout = max(1, min(5, int(max(1.0, remaining))))
            try:
                requester = getattr(self.client, "request_open_orders_for_order_confirmation", None)
                if callable(requester):
                    try:
                        requester(timeout=request_timeout, include_all=True, force=attempts > 1)
                    except TypeError:
                        try:
                            requester(timeout=request_timeout, include_all=True)
                        except TypeError:
                            requester(timeout=request_timeout)
                else:
                    self.list_open_orders(include_all=True, force=True, timeout=request_timeout)
            except Exception as exc:
                last_error = str(exc)
            try:
                contract, order = getter(normalized_order_id)
            except Exception as exc:
                last_error = str(exc)
                contract, order = None, None
            if contract and order:
                return contract, order, {
                    "source": "open_orders_refresh",
                    "attempts": attempts,
                    "last_error": last_error,
                }
            if time.time() >= deadline:
                break
            time.sleep(min(0.25, max(0.05, deadline - time.time())))
        return None, None, {
            "source": "not_found_after_refresh",
            "attempts": attempts,
            "last_error": last_error,
        }

    def await_order_fill(
        self,
        order_id: str,
        *,
        symbol: str = "",
        expected_quantity: int = 0,
        timeout: float = 5.0,
        poll_interval: float = 0.2,
    ) -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {"ok": False, "error": "missing_order_id"}
        normalized_symbol = str(symbol or "").strip().upper()
        expected_qty = abs(int(round(float(expected_quantity or 0))))
        deadline = time.time() + max(0.5, float(timeout or 0.0))
        last_snapshot: dict = {}
        while time.time() < deadline:
            order_error = {}
            getter = getattr(self.client, "get_order_error", None)
            if callable(getter):
                try:
                    order_error = dict(getter(normalized_order_id) or {})
                except Exception:
                    order_error = {}
            if order_error:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": str(order_error.get("message") or "order_rejected"),
                    "details": order_error,
                }
            snapshot, _found_open, _refreshed = self._request_order_snapshot(
                normalized_order_id,
                timeout=timeout,
                include_all=True,
            )
            if snapshot:
                last_snapshot = dict(snapshot)
            status = self._order_snapshot_status(snapshot)
            filled_qty = self._order_snapshot_filled(snapshot)
            remaining_qty = self._order_snapshot_remaining(snapshot)
            if status in {"FILLED", "EXECUTED"} or (expected_qty > 0 and filled_qty >= expected_qty and remaining_qty <= 0.0001):
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "status": status or "FILLED",
                    "order": snapshot,
                    "filled_quantity": filled_qty,
                }
            if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": "order_terminal_before_fill",
                    "status": status,
                    "order": snapshot,
                }

            if normalized_symbol:
                request_positions = getattr(self.client, "request_positions", None)
                if callable(request_positions):
                    try:
                        try:
                            positions = request_positions(timeout=max(1, min(3, int(max(1.0, float(timeout or 0.0))))))
                        except TypeError:
                            positions = request_positions()
                        symbol_positions = [
                            item for item in (positions or [])
                            if str(item.get("ticker") or item.get("symbol") or item.get("contractDesc") or "").strip().upper() == normalized_symbol
                        ]
                        if not symbol_positions:
                            return {
                                "ok": True,
                                "order_id": normalized_order_id,
                                "status": status or "POSITION_FLAT",
                                "order": snapshot,
                                "source": "positions_flat",
                            }
                        max_abs_position = max(abs(_safe_float(item.get("position", item.get("quantity", 0)), 0.0)) for item in symbol_positions)
                        if max_abs_position <= 0.0001:
                            return {
                                "ok": True,
                                "order_id": normalized_order_id,
                                "status": status or "POSITION_FLAT",
                                "order": snapshot,
                                "source": "positions_flat",
                            }
                    except Exception as exc:
                        logger.debug("request_positions failed while awaiting fill %s: %s", normalized_order_id, exc)
            time.sleep(max(0.05, float(poll_interval or 0.2)))
        return {
            "ok": False,
            "order_id": normalized_order_id,
            "error": "order_fill_unconfirmed",
            "order": last_snapshot,
        }

    def _next_bracket_order_ids(self) -> List[int]:
        high_water = self.client.max_seen_order_id() if hasattr(self.client, "max_seen_order_id") else 0
        if BRACKET_ORDER_ID_OPEN_SCAN_ENABLED:
            try:
                open_orders = self.list_open_orders(include_all=True)
            except Exception as exc:
                logger.debug("Bracket order high-water open-order scan failed: %s", exc)
                open_orders = []
            for item in open_orders or []:
                high_water = max(high_water, _IBGatewayApp._extract_order_id(item))
        if hasattr(self.client, "next_order_ids_above"):
            return self.client.next_order_ids_above(3, minimum=high_water + 1)
        return self.client.next_order_ids(3)

    def place_bracket_order(
        self,
        *,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        take_profit_quantity: int | None = None,
        stop_loss_quantity: int | None = None,
        entry_order_type: str = "LMT",
        tif: str = "DAY",
        account_id: str = "",
        order_ref_suffix: str = "",
        trade_group_id: str = "",
        bracket_group: str = "",
        order_family_type: str = "",
        entry_algo_strategy: str = "",
        entry_adaptive_priority: str = "",
        metric_environment: str = "",
        confirmation_mode: str = "",
        outside_rth: bool = False,
    ) -> dict:
        contract_info = self.resolve_contract(symbol=symbol, conid=conid)
        if not contract_info:
            return {"ok": False, "error": "contract_not_found"}
        contract = self._build_order_contract(contract_info, conid=conid, symbol=symbol)

        order_ids = self._next_bracket_order_ids()
        side = "BUY" if str(direction).lower() == "long" else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        explicit_group = self._sanitize_order_ref_group(trade_group_id or bracket_group)
        if explicit_group:
            group = explicit_group
        else:
            stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
            suffix = str(order_ref_suffix or "").strip()
            safe_suffix = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in suffix)
            group = f"{contract.symbol}_{direction}_{stamp}" + (f"_{safe_suffix}" if safe_suffix else "")
        entry_quantity = int(quantity or 0)
        tp_quantity = int(take_profit_quantity if take_profit_quantity is not None else entry_quantity)
        sl_quantity = int(stop_loss_quantity if stop_loss_quantity is not None else entry_quantity)
        tp_quantity = max(0, tp_quantity)
        sl_quantity = max(0, sl_quantity)
        requested_family_type = str(order_family_type or "").strip()
        order_family_type = requested_family_type or (
            "bracket_oco" if tp_quantity == entry_quantity and sl_quantity == entry_quantity else "partial_harvest_bracket"
        )
        # Native IB attached brackets use parentId/transmit sequencing. Adding
        # an explicit OCA group to those child legs makes later price modifies
        # fail on Gateway with code 10326 ("OCA group revision is not allowed").
        oca_group = group if order_family_type == "bracket_oco" and ATTACHED_BRACKET_EXPLICIT_OCA_ENABLED else ""
        entry_ref = f"entry_{group}"
        tp_ref = f"tp_{group}"
        sl_ref = f"sl_{group}"
        account_id = str(account_id or "").strip()
        normalized_entry_price = self._normalize_order_price(entry_price)
        normalized_tp_price = self._normalize_order_price(take_profit_price)
        normalized_sl_price = self._normalize_order_price(stop_loss_price)
        if isinstance(outside_rth, str):
            outside_rth_enabled = outside_rth.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            outside_rth_enabled = bool(outside_rth)
        price_normalization = self._price_normalization_details(
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )

        entry = Order()
        entry.orderId = int(order_ids[0])
        entry.action = side
        entry.orderType = str(entry_order_type or "LMT").upper()
        entry.totalQuantity = float(entry_quantity)
        entry.tif = str(tif or "DAY")
        entry.orderRef = entry_ref
        if account_id:
            entry.account = account_id
        entry.transmit = False
        self._clear_legacy_order_flags(entry)
        entry.outsideRth = outside_rth_enabled
        if entry.orderType == "LMT":
            entry.lmtPrice = float(normalized_entry_price)
        algo_strategy = str(entry_algo_strategy or "").strip()
        adaptive_priority = str(entry_adaptive_priority or "").strip() or "Normal"
        if algo_strategy.lower() == "adaptive":
            entry.algoStrategy = "Adaptive"
            entry.algoParams = [TagValue("adaptivePriority", adaptive_priority)]

        tp = Order()
        tp.orderId = int(order_ids[1])
        tp.action = close_side
        tp.orderType = "LMT"
        tp.totalQuantity = float(tp_quantity)
        tp.lmtPrice = float(normalized_tp_price)
        tp.tif = "GTC"
        tp.parentId = int(order_ids[0])
        tp.orderRef = tp_ref
        if account_id:
            tp.account = account_id
        if oca_group:
            tp.ocaGroup = oca_group
            tp.ocaType = 1
        tp.transmit = False
        self._clear_legacy_order_flags(tp)
        tp.outsideRth = outside_rth_enabled

        sl = Order()
        sl.orderId = int(order_ids[2])
        sl.action = close_side
        sl.orderType = "STP"
        sl.totalQuantity = float(sl_quantity)
        sl.auxPrice = float(normalized_sl_price)
        sl.tif = "GTC"
        sl.parentId = int(order_ids[0])
        sl.orderRef = sl_ref
        if account_id:
            sl.account = account_id
        if oca_group:
            sl.ocaGroup = oca_group
            sl.ocaType = 1
        sl.transmit = True
        self._clear_legacy_order_flags(sl)
        sl.outsideRth = outside_rth_enabled

        try:
            with self._gateway_write_lock("place_bracket_order", environment=metric_environment):
                for broker_order_id in order_ids:
                    self.client.clear_order_error(str(broker_order_id))
                self.client.place_order(contract, entry)
                self.client.place_order(contract, tp)
                self.client.place_order(contract, sl)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
                "bracket_group": group,
                "trade_group_id": group,
                "oca_group": oca_group,
                "order_family_type": order_family_type,
                "quantity": entry_quantity,
                "take_profit_quantity": tp_quantity,
                "stop_loss_quantity": sl_quantity,
                "entry_coid": entry_ref,
                "tp_coid": tp_ref,
                "sl_coid": sl_ref,
                "entry_price": normalized_entry_price if entry.orderType == "LMT" else float(entry_price or 0.0),
                "take_profit_price": normalized_tp_price,
                "stop_loss_price": normalized_sl_price,
                "outside_rth": outside_rth_enabled,
                "price_normalization": price_normalization,
                "entry_algo_strategy": algo_strategy,
                "entry_adaptive_priority": adaptive_priority if algo_strategy.lower() == "adaptive" else "",
            }

        normalized_confirmation_mode = str(confirmation_mode or "").strip().lower()
        if normalized_confirmation_mode in {"async", "background", "fast_ack", "fast-ack"}:
            self._start_bracket_submission_background_confirmation(
                order_ids=list(order_ids),
                group=group,
                order_family_type=order_family_type,
                metric_environment=metric_environment,
            )
            return {
                "ok": True,
                "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
                "bracket_group": group,
                "trade_group_id": group,
                "oca_group": oca_group,
                "order_family_type": order_family_type,
                "quantity": entry_quantity,
                "take_profit_quantity": tp_quantity,
                "stop_loss_quantity": sl_quantity,
                "entry_coid": entry_ref,
                "tp_coid": tp_ref,
                "sl_coid": sl_ref,
                "entry_price": normalized_entry_price if entry.orderType == "LMT" else float(entry_price or 0.0),
                "take_profit_price": normalized_tp_price,
                "stop_loss_price": normalized_sl_price,
                "outside_rth": outside_rth_enabled,
                "price_normalization": price_normalization,
                "submission": {
                    "ok": True,
                    "pending_confirmation": True,
                    "confirmation_mode": "background",
                    "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
                    "orders": {},
                    "missing_order_ids": [],
                },
                "confirmation_mode": "background",
                "pending_confirmation": True,
                "protection_confirmation_pending": True,
                "entry_algo_strategy": algo_strategy,
                "entry_adaptive_priority": adaptive_priority if algo_strategy.lower() == "adaptive" else "",
            }

        submission_result = self.client.await_order_submissions(
            [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
            timeout=BRACKET_SUBMISSION_CONFIRM_TIMEOUT_SECONDS,
            poll_interval=0.2,
        )
        if not submission_result.get("ok"):
            failures = submission_result.get("failures") if isinstance(submission_result.get("failures"), dict) else {}
            entry_result = failures.get(str(order_ids[0])) if isinstance(failures.get(str(order_ids[0])), dict) else {}
            entry_error = entry_result.get("details") or {}
            missing_order_ids = [
                str(item or "").strip()
                for item in (submission_result.get("missing_order_ids") or [])
                if str(item or "").strip()
            ]
            child_failure_ids = [
                str(item or "").strip()
                for item in failures.keys()
                if str(item or "").strip() and str(item or "").strip() != str(order_ids[0])
            ]
            protection_issue_ids = list(dict.fromkeys([*missing_order_ids, *child_failure_ids]))
            missing_roles = []
            if str(order_ids[1]) in protection_issue_ids:
                missing_roles.append("take_profit")
            if str(order_ids[2]) in protection_issue_ids:
                missing_roles.append("stop_loss")
            error_message = str(submission_result.get("error") or "order_submission_failed")
            if entry_error.get("code"):
                error_message = f"{error_message} (code={entry_error.get('code')})"
            if protection_issue_ids:
                error_message = f"{error_message}:missing={','.join(protection_issue_ids)}"
            entry_confirmed = str(order_ids[0]) in (submission_result.get("orders") or {})
            entry_failed = bool(entry_result) or str(order_ids[0]) in missing_order_ids
            if entry_confirmed and not entry_failed and protection_issue_ids:
                return {
                    "ok": True,
                    "error": error_message,
                    "entry_error": {},
                    "submission": submission_result,
                    "protection_complete": False,
                    "protection_incomplete": True,
                    "missing_order_ids": protection_issue_ids,
                    "missing_protection_roles": missing_roles,
                    "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
                    "bracket_group": group,
                    "trade_group_id": group,
                    "oca_group": oca_group,
                    "order_family_type": order_family_type,
                    "quantity": entry_quantity,
                    "take_profit_quantity": tp_quantity,
                    "stop_loss_quantity": sl_quantity,
                    "entry_coid": entry_ref,
                    "tp_coid": tp_ref,
                    "sl_coid": sl_ref,
                    "entry_price": normalized_entry_price if entry.orderType == "LMT" else float(entry_price or 0.0),
                    "take_profit_price": normalized_tp_price,
                    "stop_loss_price": normalized_sl_price,
                    "outside_rth": outside_rth_enabled,
                    "price_normalization": price_normalization,
                    "entry_algo_strategy": algo_strategy,
                    "entry_adaptive_priority": adaptive_priority if algo_strategy.lower() == "adaptive" else "",
                }
            return {
                "ok": False,
                "error": error_message,
                "entry_error": entry_result or submission_result,
                "submission": submission_result,
                "protection_complete": False,
                "protection_incomplete": bool(protection_issue_ids),
                "missing_order_ids": protection_issue_ids,
                "missing_protection_roles": missing_roles,
                "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
                "bracket_group": group,
                "trade_group_id": group,
                "oca_group": oca_group,
                "order_family_type": order_family_type,
                "quantity": entry_quantity,
                "take_profit_quantity": tp_quantity,
                "stop_loss_quantity": sl_quantity,
                "entry_coid": entry_ref,
                "tp_coid": tp_ref,
                "sl_coid": sl_ref,
                "entry_price": normalized_entry_price if entry.orderType == "LMT" else float(entry_price or 0.0),
                "take_profit_price": normalized_tp_price,
                "stop_loss_price": normalized_sl_price,
                "outside_rth": outside_rth_enabled,
                "price_normalization": price_normalization,
                "entry_algo_strategy": algo_strategy,
                "entry_adaptive_priority": adaptive_priority if algo_strategy.lower() == "adaptive" else "",
            }

        return {
            "ok": True,
            "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
            "bracket_group": group,
            "trade_group_id": group,
            "oca_group": oca_group,
            "order_family_type": order_family_type,
            "quantity": entry_quantity,
            "take_profit_quantity": tp_quantity,
            "stop_loss_quantity": sl_quantity,
            "entry_coid": entry_ref,
            "tp_coid": tp_ref,
            "sl_coid": sl_ref,
            "entry_price": normalized_entry_price if entry.orderType == "LMT" else float(entry_price or 0.0),
            "take_profit_price": normalized_tp_price,
            "stop_loss_price": normalized_sl_price,
            "outside_rth": outside_rth_enabled,
            "price_normalization": price_normalization,
            "submission": submission_result,
            "protection_complete": True,
            "entry_algo_strategy": algo_strategy,
            "entry_adaptive_priority": adaptive_priority if algo_strategy.lower() == "adaptive" else "",
        }

    def place_protection_repair_orders(
        self,
        *,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        take_profit_price: float,
        stop_loss_price: float,
        account_id: str = "",
        trade_group_id: str = "",
        entry_order_unique_id: str = "",
        signal_id: str = "",
        metric_environment: str = "",
        outside_rth: bool = False,
    ) -> dict:
        contract_info = self.resolve_contract(symbol=symbol, conid=conid)
        if not contract_info:
            return {"ok": False, "error": "contract_not_found"}
        contract = self._build_order_contract(contract_info, conid=conid, symbol=symbol)

        high_water = self.client.max_seen_order_id() if hasattr(self.client, "max_seen_order_id") else 0
        if hasattr(self.client, "next_order_ids_above"):
            order_ids = self.client.next_order_ids_above(2, minimum=high_water + 1)
        else:
            order_ids = self.client.next_order_ids(2)
        close_side = "SELL" if str(direction or "").strip().lower() == "long" else "BUY"
        base_group = self._sanitize_order_ref_group(trade_group_id or entry_order_unique_id or signal_id)
        if not base_group:
            stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
            base_group = f"{str(symbol or '').upper()}_{str(direction or '').lower()}_{stamp}"
        oca_group = f"repair_{base_group}"
        tp_ref = f"repair_tp_{base_group}"
        sl_ref = f"repair_sl_{base_group}"
        account_id = str(account_id or "").strip()
        normalized_tp_price = self._normalize_order_price(take_profit_price)
        normalized_sl_price = self._normalize_order_price(stop_loss_price)

        tp = Order()
        tp.orderId = int(order_ids[0])
        tp.action = close_side
        tp.orderType = "LMT"
        tp.totalQuantity = float(quantity or 0)
        tp.lmtPrice = float(normalized_tp_price)
        tp.tif = "GTC"
        tp.orderRef = tp_ref
        if account_id:
            tp.account = account_id
        tp.ocaGroup = oca_group
        tp.ocaType = 1
        tp.transmit = False
        self._clear_legacy_order_flags(tp)
        tp.outsideRth = bool(outside_rth)

        sl = Order()
        sl.orderId = int(order_ids[1])
        sl.action = close_side
        sl.orderType = "STP"
        sl.totalQuantity = float(quantity or 0)
        sl.auxPrice = float(normalized_sl_price)
        sl.tif = "GTC"
        sl.orderRef = sl_ref
        if account_id:
            sl.account = account_id
        sl.ocaGroup = oca_group
        sl.ocaType = 1
        sl.transmit = True
        self._clear_legacy_order_flags(sl)
        sl.outsideRth = bool(outside_rth)

        try:
            with self._gateway_write_lock("place_protection_repair_orders", environment=metric_environment):
                for broker_order_id in order_ids:
                    self.client.clear_order_error(str(broker_order_id))
                self.client.place_order(contract, tp)
                self.client.place_order(contract, sl)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "order_ids": [str(order_ids[0]), str(order_ids[1])],
                "trade_group_id": str(trade_group_id or ""),
                "entry_order_unique_id": str(entry_order_unique_id or ""),
                "signal_id": str(signal_id or ""),
                "oca_group": oca_group,
                "tp_coid": tp_ref,
                "sl_coid": sl_ref,
                "take_profit_price": normalized_tp_price,
                "stop_loss_price": normalized_sl_price,
                "quantity": int(quantity or 0),
            }

        submission_result = self.client.await_order_submissions(
            [str(order_ids[0]), str(order_ids[1])],
            timeout=BRACKET_SUBMISSION_CONFIRM_TIMEOUT_SECONDS,
            poll_interval=0.2,
        )
        missing_order_ids = [
            str(item or "").strip()
            for item in (submission_result.get("missing_order_ids") or [])
            if str(item or "").strip()
        ]
        ok = bool(submission_result.get("ok"))
        return {
            "ok": ok,
            "error": "" if ok else str(submission_result.get("error") or "repair_order_submission_failed"),
            "submission": submission_result,
            "missing_order_ids": missing_order_ids,
            "missing_protection_roles": [
                role
                for role, order_id in (("take_profit", str(order_ids[0])), ("stop_loss", str(order_ids[1])))
                if order_id in missing_order_ids
            ],
            "order_ids": [str(order_ids[0]), str(order_ids[1])],
            "trade_group_id": str(trade_group_id or ""),
            "entry_order_unique_id": str(entry_order_unique_id or ""),
            "signal_id": str(signal_id or ""),
            "oca_group": oca_group,
            "tp_coid": tp_ref,
            "sl_coid": sl_ref,
            "take_profit_price": normalized_tp_price,
            "stop_loss_price": normalized_sl_price,
            "quantity": int(quantity or 0),
            "outside_rth": bool(outside_rth),
            "protection_complete": ok,
        }

    def place_market_close(
        self,
        *,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        account_id: str = "",
        order_ref: str = "",
        order_type: str = "LMT",
        limit_price: float = 0.0,
        outside_rth: bool = False,
        tif: str = "DAY",
        exchange: str = "",
        include_overnight: bool = False,
        wait_for_fill: bool = False,
        fill_timeout: float = 5.0,
        metric_environment: str = "",
    ) -> dict:
        normalized_exchange = str(exchange or "").strip().upper()
        resolve_exchange = "" if normalized_exchange in {"OVERNIGHT"} else normalized_exchange
        contract_info = self.resolve_contract(symbol=symbol, conid=conid, exchange=resolve_exchange)
        if not contract_info:
            return {"ok": False, "error": "contract_not_found"}
        contract = self._build_order_contract(contract_info, conid=conid, symbol=symbol)
        if normalized_exchange and normalized_exchange != "SMART":
            contract.exchange = normalized_exchange
        order_id = self.client.next_order_ids(1)[0]
        side = "SELL" if str(direction).lower() == "long" else "BUY"
        order = Order()
        order.orderId = int(order_id)
        order.action = side
        normalized_order_type = str(order_type or "LMT").strip().upper()
        wants_limit = normalized_order_type in {"LMT", "LIMIT", "MARKETABLE_LIMIT"}
        normalized_limit_price = self._normalize_order_price(limit_price) if wants_limit else 0.0
        price_normalization = self._price_normalization_details(limit_price=limit_price) if wants_limit else {}
        if isinstance(outside_rth, str):
            outside_rth_enabled = outside_rth.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            outside_rth_enabled = bool(outside_rth)
        if wants_limit and normalized_limit_price <= 0:
            return {
                "ok": False,
                "error": "limit_price_required_for_marketable_limit",
                "order_type": normalized_order_type,
                "limit_price": float(limit_price or 0.0),
                "price_normalization": price_normalization,
            }
        use_limit = wants_limit
        order.orderType = "LMT" if use_limit else "MKT"
        if use_limit:
            order.lmtPrice = float(normalized_limit_price)
        order.totalQuantity = float(quantity)
        order.outsideRth = outside_rth_enabled
        session_flags = self._apply_order_session_flags_for_submit(
            order,
            tif=tif,
            include_overnight=bool(include_overnight),
        )
        include_overnight_enabled = bool(session_flags.get("include_overnight"))
        include_overnight_supported = bool(session_flags.get("include_overnight_supported"))
        order_ref = str(order_ref or "").strip()
        order.orderRef = order_ref or f"close_{contract.symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        account_id = str(account_id or "").strip()
        if account_id:
            order.account = account_id
        self._clear_legacy_order_flags(order)
        try:
            with self._gateway_write_lock("place_market_close", environment=metric_environment):
                self.client.clear_order_error(str(order_id))
                self.client.place_order(contract, order)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        entry_result = self.client.await_order_submission(
            str(order_id),
            timeout=ORDER_MODIFICATION_CONFIRM_TIMEOUT_SECONDS,
            poll_interval=0.2,
        )
        if not entry_result.get("ok"):
            order_error = entry_result.get("details") or {}
            error_message = str(entry_result.get("error") or "order_submission_failed")
            if order_error.get("code"):
                error_message = f"{error_message} (code={order_error.get('code')})"
            return {
                "ok": False,
                "error": error_message,
                "entry_error": entry_result,
                "order_ids": [str(order_id)],
                "bracket_group": order.orderRef,
                "entry_coid": order.orderRef,
            }
        result = {
            "ok": True,
            "submitted": True,
            "order_ids": [str(order_id)],
            "bracket_group": order.orderRef,
            "entry_coid": order.orderRef,
            "order_type": order.orderType,
            "limit_price": float(normalized_limit_price) if use_limit else 0.0,
            "outside_rth": outside_rth_enabled,
            "tif": order.tif,
            "exchange": str(getattr(contract, "exchange", "") or ""),
            "include_overnight": include_overnight_enabled,
            "include_overnight_supported": include_overnight_supported,
            "price_normalization": price_normalization,
        }
        if wait_for_fill:
            fill_result = self.await_order_fill(
                str(order_id),
                symbol=contract.symbol,
                expected_quantity=int(quantity or 0),
                timeout=float(fill_timeout or 5.0),
                poll_interval=0.2,
            )
            result["fill"] = fill_result
            result["filled"] = bool(fill_result.get("ok"))
            if not fill_result.get("ok"):
                return {
                    **result,
                    "ok": False,
                    "error": str(fill_result.get("error") or "order_fill_unconfirmed"),
                }
        return result

    def modify_order(self, order_id: str, updates: dict, account_id: str = "", metric_environment: str = "") -> dict:
        started = time.perf_counter()
        contract, order, lookup_diagnostics = self._get_order_objects_for_modify(str(order_id or "").strip())
        if not contract or not order:
            record_order_event(
                operation="modify",
                result="error",
                reason_code="order_not_found",
                duration_s=time.perf_counter() - started,
            )
            return {"ok": False, "error": "order_not_found", "order_lookup": lookup_diagnostics}
        price_normalization: dict = {}
        if "price" in updates and updates["price"] is not None:
            normalized_price = self._normalize_order_price(updates["price"])
            price_normalization["price"] = {
                "raw": _safe_float(updates["price"], 0.0),
                "normalized": normalized_price,
                "changed": abs(_safe_float(updates["price"], 0.0) - normalized_price) > 0.0000001,
            }
            if str(getattr(order, "orderType", "") or "").upper() in {"STP", "STOP"}:
                order.auxPrice = float(normalized_price)
            else:
                order.lmtPrice = float(normalized_price)
        if "auxPrice" in updates and updates["auxPrice"] is not None:
            normalized_aux_price = self._normalize_order_price(updates["auxPrice"])
            price_normalization["auxPrice"] = {
                "raw": _safe_float(updates["auxPrice"], 0.0),
                "normalized": normalized_aux_price,
                "changed": abs(_safe_float(updates["auxPrice"], 0.0) - normalized_aux_price) > 0.0000001,
            }
            order.auxPrice = float(normalized_aux_price)
        if "lmtPrice" in updates and updates["lmtPrice"] is not None:
            normalized_lmt_price = self._normalize_order_price(updates["lmtPrice"])
            price_normalization["lmtPrice"] = {
                "raw": _safe_float(updates["lmtPrice"], 0.0),
                "normalized": normalized_lmt_price,
                "changed": abs(_safe_float(updates["lmtPrice"], 0.0) - normalized_lmt_price) > 0.0000001,
            }
            order.lmtPrice = float(normalized_lmt_price)
        if "quantity" in updates and updates["quantity"] is not None:
            order.totalQuantity = float(updates["quantity"])
        original_tif = str(getattr(order, "tif", "") or "")
        requested_tif = updates.get("tif") if updates.get("tif") else original_tif
        original_include_overnight = self._order_attr_truthy(order, "includeOvernight")
        session_flags = self._apply_order_session_flags_for_submit(
            order,
            tif=requested_tif,
            include_overnight=original_include_overnight,
        )
        account_id = str(account_id or "").strip()
        if account_id:
            order.account = account_id
        self._clear_legacy_order_flags(order)
        try:
            with self._gateway_write_lock("modify_order", environment=metric_environment):
                self.client.clear_order_error(str(order_id))
                self.client.place_order(contract, order)
        except Exception as exc:
            record_order_event(
                operation="modify",
                result="error",
                reason_code=exc.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            return {"ok": False, "error": str(exc)}
        entry_result = self.client.await_order_submission(
            str(order_id),
            timeout=max(3.0, min(float(ORDER_MODIFICATION_CONFIRM_TIMEOUT_SECONDS or 0.0), 15.0)),
            poll_interval=0.2,
        )
        entry_unconfirmed = (
            not entry_result.get("ok")
            and str(entry_result.get("error") or "").strip() == "order_submission_unconfirmed"
        )
        if not entry_result.get("ok") and not entry_unconfirmed:
            order_error = entry_result.get("details") or {}
            error_message = str(entry_result.get("error") or "order_submission_failed")
            if order_error.get("code"):
                error_message = f"{error_message} (code={order_error.get('code')})"
            record_order_event(
                operation="modify",
                result="unconfirmed",
                reason_code=str(entry_result.get("error") or "order_submission_failed"),
                duration_s=time.perf_counter() - started,
            )
            return {"ok": False, "error": error_message, "entry_error": entry_result, "order_id": str(order_id)}
        expected_price = 0.0
        expected_fields: list[str] = []
        if "auxPrice" in updates and updates["auxPrice"] is not None:
            expected_price = float(price_normalization.get("auxPrice", {}).get("normalized") or 0.0)
            expected_fields = ["auxPrice"]
        elif "lmtPrice" in updates and updates["lmtPrice"] is not None:
            expected_price = float(price_normalization.get("lmtPrice", {}).get("normalized") or 0.0)
            expected_fields = ["lmtPrice", "price"]
        elif "price" in updates and updates["price"] is not None:
            expected_price = float(price_normalization.get("price", {}).get("normalized") or 0.0)
            if str(getattr(order, "orderType", "") or "").upper() in {"STP", "STOP"}:
                expected_fields = ["auxPrice", "price"]
            else:
                expected_fields = ["price", "lmtPrice"]
        if expected_price > 0 and expected_fields:
            confirm_result = self.await_order_price_update(
                str(order_id),
                expected_price=expected_price,
                fields=expected_fields,
                timeout=ORDER_MODIFICATION_CONFIRM_TIMEOUT_SECONDS,
                poll_interval=0.2,
            )
            if not confirm_result.get("ok"):
                if str(confirm_result.get("error") or "") == "order_modify_price_unconfirmed":
                    record_order_event(
                        operation="modify",
                        result="pending",
                        reason_code="order_modify_price_unconfirmed",
                        duration_s=time.perf_counter() - started,
                    )
                    return {
                        "ok": True,
                        "pending_confirmation": True,
                        "modify_confirmation_pending": True,
                        "warning": "order_modify_price_unconfirmed",
                        "entry": entry_result,
                        "entry_submission_unconfirmed": entry_unconfirmed,
                        "confirm": confirm_result,
                        "order_id": str(order_id),
                        "price_normalization": price_normalization,
                        "session_flags": session_flags,
                    }
                record_order_event(
                    operation="modify",
                    result="unconfirmed",
                    reason_code=str(confirm_result.get("error") or "order_modify_unconfirmed"),
                    duration_s=time.perf_counter() - started,
                )
                return {
                    "ok": False,
                    "error": str(confirm_result.get("error") or "order_modify_unconfirmed"),
                    "entry_error": entry_result,
                    "confirm": confirm_result,
                    "order_id": str(order_id),
                    "price_normalization": price_normalization,
                    "session_flags": session_flags,
                }
            record_order_event(operation="modify", result="ok", duration_s=time.perf_counter() - started)
            result = {
                "ok": True,
                "order_id": str(order_id),
                "order": confirm_result.get("order") or {},
                "price_normalization": price_normalization,
                "session_flags": session_flags,
            }
            if entry_unconfirmed:
                result["entry_submission_unconfirmed"] = True
                result["entry"] = entry_result
            return result
        record_order_event(operation="modify", result="ok", duration_s=time.perf_counter() - started)
        result = {
            "ok": True,
            "order_id": str(order_id),
            "order": entry_result.get("order") or {},
            "price_normalization": price_normalization,
            "session_flags": session_flags,
        }
        if entry_unconfirmed:
            result["pending_confirmation"] = True
            result["modify_confirmation_pending"] = True
            result["warning"] = "order_modify_submission_unconfirmed"
            result["entry_submission_unconfirmed"] = True
            result["entry"] = entry_result
        return result

    def get_order_snapshot(self, order_id: str) -> dict:
        return self.client.get_order_snapshot(order_id)

    def _await_orders_not_open(
        self,
        order_ids: Iterable[str],
        *,
        timeout: float = CANCEL_ALL_RECONCILE_TIMEOUT_SECONDS,
        poll_interval: float = CANCEL_ALL_RECONCILE_POLL_INTERVAL_SECONDS,
    ) -> dict:
        remaining = {str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()}
        if not remaining:
            return {"ok": True, "remaining_order_ids": [], "open_orders": [], "source": "empty"}

        deadline = time.time() + max(1.0, float(timeout or 0.0))
        last_open_orders: list[dict] = []
        last_error = ""
        filled_order_ids: set[str] = set()
        ignored_errors: dict[str, list[dict]] = {}

        def cached_open_orders() -> list[dict]:
            getter = getattr(self.client, "get_order_snapshots", None)
            if not callable(getter):
                return []
            try:
                return [dict(item) for item in (getter(include_all=True) or []) if isinstance(item, dict)]
            except TypeError:
                try:
                    return [dict(item) for item in (getter() or []) if isinstance(item, dict)]
                except Exception:
                    return []
            except Exception:
                return []

        def reconcile_from_open_orders(open_orders: list[dict], *, authoritative: bool = False) -> set[str]:
            open_by_id = {
                self._order_snapshot_id(item): dict(item)
                for item in (open_orders or [])
                if self._order_snapshot_id(item)
            }
            still_open: set[str] = set()
            for order_id in list(remaining):
                order_error = {}
                getter = getattr(self.client, "get_order_error", None)
                if callable(getter):
                    try:
                        order_error = dict(getter(order_id) or {})
                    except Exception:
                        order_error = {}
                if order_error:
                    cancel_confirmed = (
                        self._order_error_is_cancelled(order_error)
                        or self._order_error_is_cancel_terminal_notice(order_error)
                    )
                    ignorable_error = cancel_confirmed or self._order_error_is_stale_invalid_price_rejection(order_error)
                    if ignorable_error:
                        ignored_errors.setdefault(order_id, []).append(dict(order_error))
                        clearer = getattr(self.client, "clear_order_error", None)
                        if callable(clearer):
                            try:
                                clearer(order_id)
                            except Exception:
                                pass
                    if cancel_confirmed:
                        terminal_status = "NOT_OPEN" if _order_error_code(order_error) == 10147 else "CANCELLED"
                        self._mark_client_order_terminal(
                            order_id,
                            status=terminal_status,
                            reason="cancel_all_terminal_notice",
                        )
                        continue

                snapshot = open_by_id.get(order_id) or {}
                if not snapshot:
                    getter_snapshot = getattr(self.client, "get_order_snapshot", None)
                    if callable(getter_snapshot):
                        try:
                            snapshot = dict(getter_snapshot(order_id) or {})
                        except Exception:
                            snapshot = {}
                status = self._order_snapshot_status(snapshot)
                if status in {"FILLED", "EXECUTED"}:
                    filled_order_ids.add(order_id)
                    continue
                if order_id in open_by_id and status not in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                    still_open.add(order_id)
                else:
                    if status in {"CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                        self._mark_client_order_terminal(order_id, status=status, reason="cancel_all_status_callback")
                    elif authoritative:
                        self._mark_client_order_terminal(
                            order_id,
                            status="CANCELLED",
                            reason="cancel_all_open_orders_reconciled_missing",
                        )
            return still_open

        while time.time() < deadline:
            try:
                open_orders = self.list_open_orders(include_all=True, force=True, timeout=5.0)
                last_open_orders = [dict(item) for item in (open_orders or [])]
                last_error = ""
            except Exception as exc:
                last_error = str(exc)
                cached_orders = cached_open_orders()
                if cached_orders:
                    last_open_orders = cached_orders
                    remaining = reconcile_from_open_orders(cached_orders, authoritative=False)
                    if filled_order_ids:
                        return {
                            "ok": False,
                            "error": "order_filled_during_cancel_all",
                            "filled_order_ids": sorted(filled_order_ids),
                            "remaining_order_ids": sorted(remaining),
                            "open_orders": last_open_orders,
                            "ignored_errors": ignored_errors,
                            "last_error": last_error,
                        }
                    if not remaining:
                        return {
                            "ok": True,
                            "remaining_order_ids": [],
                            "open_orders": last_open_orders,
                            "ignored_errors": ignored_errors,
                            "source": "callback_cache_reconciled",
                            "last_error": last_error,
                        }
                time.sleep(max(0.1, float(poll_interval or 1.0)))
                continue

            remaining = reconcile_from_open_orders(last_open_orders, authoritative=True)
            if filled_order_ids:
                return {
                    "ok": False,
                    "error": "order_filled_during_cancel_all",
                    "filled_order_ids": sorted(filled_order_ids),
                    "remaining_order_ids": sorted(remaining),
                    "open_orders": last_open_orders,
                    "ignored_errors": ignored_errors,
                }
            if not remaining:
                return {
                    "ok": True,
                    "remaining_order_ids": [],
                    "open_orders": last_open_orders,
                    "ignored_errors": ignored_errors,
                    "source": "open_orders_reconciled",
                }
            time.sleep(max(0.1, float(poll_interval or 1.0)))

        return {
            "ok": False,
            "error": "order_cancel_unconfirmed",
            "remaining_order_ids": sorted(remaining),
            "open_orders": last_open_orders,
            "last_error": last_error,
            "ignored_errors": ignored_errors,
        }

    def cancel_order(self, order_id: str, metric_environment: str = "") -> dict:
        started = time.perf_counter()
        self._remember_recent_cancel_order_ids([order_id])
        try:
            with self._gateway_write_lock("cancel_order", environment=metric_environment):
                clearer = getattr(self.client, "clear_order_error", None)
                if callable(clearer):
                    clearer(str(order_id))
                self.client.cancel_open_order(order_id)
        except Exception as exc:
            record_order_event(
                operation="cancel",
                result="error",
                reason_code=exc.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            return {"ok": False, "error": str(exc)}
        confirm_result = self.await_order_cancelled(
            str(order_id),
            timeout=CANCEL_CONFIRM_TIMEOUT_SECONDS,
            poll_interval=0.2,
        )
        if not confirm_result.get("ok"):
            record_order_event(
                operation="cancel",
                result="unconfirmed",
                reason_code=str(confirm_result.get("error") or "order_cancel_unconfirmed"),
                duration_s=time.perf_counter() - started,
            )
            return {
                "ok": False,
                "order_id": str(order_id),
                "error": str(confirm_result.get("error") or "order_cancel_unconfirmed"),
                "confirm": confirm_result,
            }
        record_order_event(operation="cancel", result="ok", duration_s=time.perf_counter() - started)
        return {
            "ok": True,
            "order_id": str(order_id),
            "status": confirm_result.get("status") or "",
            "confirm": confirm_result,
        }

    def cancel_order_ids(self, order_ids: Iterable[Any], metric_environment: str = "", source: str = "explicit_order_ids") -> dict:
        started = time.perf_counter()
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for item in order_ids or []:
            order_id = str(item or "").strip()
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            normalized_ids.append(order_id)
        if not normalized_ids:
            return {
                "ok": True,
                "status": "NO_ORDERS",
                "pending_confirmation": False,
                "requested": 0,
                "submitted": 0,
                "active_order_ids": [],
                "submitted_order_ids": [],
                "order_ids": [],
                "errors": [],
                "error_details": [],
                "source": source,
            }

        self._remember_recent_cancel_order_ids(normalized_ids)
        submitted_order_ids: list[str] = []
        errors: list[str] = []
        error_details: list[dict] = []
        clearer = getattr(self.client, "clear_order_error", None)
        with self._gateway_write_lock("cancel_order_ids", environment=metric_environment):
            for order_id in normalized_ids:
                order_started = time.perf_counter()
                try:
                    if callable(clearer):
                        clearer(order_id)
                    self.client.cancel_open_order(order_id)
                    submitted_order_ids.append(order_id)
                    record_order_event(operation="cancel", result="pending", reason_code=source, duration_s=time.perf_counter() - order_started)
                except Exception as exc:
                    error = str(exc)
                    errors.append(error or "cancel_failed")
                    error_details.append({"order_id": order_id, "error": error or "cancel_failed"})
                    record_order_event(
                        operation="cancel",
                        result="error",
                        reason_code=exc.__class__.__name__,
                        duration_s=time.perf_counter() - order_started,
                    )
        result_ok = not errors or bool(submitted_order_ids)
        record_order_event(
            operation="cancel_order_ids",
            result="pending" if result_ok else "error",
            reason_code=source if result_ok else (errors[0] if errors else "cancel_failed"),
            duration_s=time.perf_counter() - started,
        )
        return {
            "ok": result_ok,
            "status": "CANCEL_REQUESTED" if submitted_order_ids else "",
            "pending_confirmation": bool(submitted_order_ids),
            "requested": len(normalized_ids),
            "submitted": len(submitted_order_ids),
            "active_order_ids": list(normalized_ids),
            "submitted_order_ids": list(submitted_order_ids),
            "order_ids": list(submitted_order_ids),
            "errors": errors,
            "error_details": error_details,
            "source": source,
        }

    def cancel_all_orders(self, metric_environment: str = "") -> dict:
        started = time.perf_counter()
        self._remember_recent_cancel_all()
        order_list_source = "open_orders"
        order_list_error = ""
        def cached_order_snapshots() -> list[dict]:
            getter = getattr(self.client, "get_order_snapshots", None)
            try:
                return list(getter(include_all=True) or []) if callable(getter) else []
            except TypeError:
                try:
                    return list(getter() or []) if callable(getter) else []
                except Exception:
                    return []
            except Exception:
                return []

        try:
            orders = self.list_open_orders(include_all=True, force=True, timeout=5.0)
            if not orders:
                cached_orders = cached_order_snapshots()
                if cached_orders:
                    # reqAllOpenOrders can race with large bracket bursts and
                    # briefly return empty even though callback/account caches
                    # still contain active paper orders. Canceling those ids is
                    # safer than reporting a false flat cancel_all.
                    orders = cached_orders
                    order_list_source = "callback_cache_empty_open_orders"
        except Exception as exc:
            order_list_error = str(exc)
            orders = cached_order_snapshots()
            if orders:
                order_list_source = "callback_cache"
            else:
                record_order_event(
                    operation="cancel_all_orders",
                    result="error",
                    reason_code=exc.__class__.__name__,
                    duration_s=time.perf_counter() - started,
                )
                return {"ok": False, "cancelled": 0, "errors": [order_list_error], "error_details": [{"error": order_list_error}]}

        active_order_ids: list[str] = []
        skipped_terminal = 0
        for order in orders:
            order_id = self._order_snapshot_id(order)
            if not order_id:
                continue
            status = self._order_snapshot_status(order)
            if status in {"FILLED", "EXECUTED", "CANCELED", "CANCELLED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
                skipped_terminal += 1
                continue
            active_order_ids.append(order_id)
        self._remember_recent_cancel_order_ids(active_order_ids)

        submitted_order_ids: list[str] = []
        errors: list[str] = []
        error_details: list[dict] = []
        global_cancel_submitted = False
        global_cancel_error = ""
        clearer = getattr(self.client, "clear_order_error", None)
        global_canceller = getattr(self.client, "request_global_cancel", None)
        environment_key = str(metric_environment or os.environ.get("IBKR_BROKER_MODE") or DEFAULT_ENVIRONMENT).strip().lower()
        global_cancel_enabled = (
            CANCEL_ALL_GLOBAL_CANCEL_ENABLED
            and environment_key == "paper"
            and callable(global_canceller)
        )
        with self._gateway_write_lock("cancel_all_orders", environment=metric_environment):
            if global_cancel_enabled:
                try:
                    global_canceller()
                    global_cancel_submitted = True
                    if CANCEL_ALL_GLOBAL_CANCEL_GRACE_SECONDS > 0:
                        time.sleep(min(2.0, CANCEL_ALL_GLOBAL_CANCEL_GRACE_SECONDS))
                except Exception as exc:
                    global_cancel_error = str(exc) or "global_cancel_failed"
                    error_details.append({"order_id": "", "error": global_cancel_error, "source": "global_cancel"})
            for order_id in active_order_ids:
                order_started = time.perf_counter()
                try:
                    if callable(clearer):
                        clearer(order_id)
                    self.client.cancel_open_order(order_id)
                    submitted_order_ids.append(order_id)
                except Exception as exc:
                    error = str(exc)
                    errors.append(error or "cancel_failed")
                    error_details.append({"order_id": order_id, "error": error or "cancel_failed"})
                    record_order_event(
                        operation="cancel",
                        result="error",
                        reason_code=exc.__class__.__name__,
                        duration_s=time.perf_counter() - order_started,
                    )

        reconcile = (
            self._await_orders_not_open(
                submitted_order_ids,
                timeout=CANCEL_ALL_CONFIRM_TIMEOUT_SECONDS,
                poll_interval=CANCEL_ALL_RECONCILE_POLL_INTERVAL_SECONDS,
            )
            if submitted_order_ids
            else {"ok": True}
        )
        unconfirmed_ids = {
            str(item or "").strip()
            for item in (reconcile.get("remaining_order_ids") or [])
            if str(item or "").strip()
        }
        filled_ids = {
            str(item or "").strip()
            for item in (reconcile.get("filled_order_ids") or [])
            if str(item or "").strip()
        }
        write_error_count = len(errors)
        pending_error_details: list[dict] = []
        for order_id in submitted_order_ids:
            if order_id in filled_ids:
                reason = "order_filled_during_cancel_all"
                errors.append(reason)
                error_details.append({"order_id": order_id, "error": reason})
                record_order_event(
                    operation="cancel",
                    result="unconfirmed",
                    reason_code=reason,
                    duration_s=time.perf_counter() - started,
                )
            elif order_id in unconfirmed_ids:
                reason = "order_cancel_unconfirmed"
                pending_error_details.append({"order_id": order_id, "error": reason})
                if not CANCEL_ALL_PENDING_ON_UNCONFIRMED_ENABLED:
                    errors.append(reason)
                    error_details.append({"order_id": order_id, "error": reason})
                record_order_event(
                    operation="cancel",
                    result="pending" if CANCEL_ALL_PENDING_ON_UNCONFIRMED_ENABLED else "unconfirmed",
                    reason_code=reason,
                    duration_s=time.perf_counter() - started,
                )
            else:
                record_order_event(
                    operation="cancel",
                    result="ok",
                    duration_s=time.perf_counter() - started,
                )

        cancelled = max(0, len(submitted_order_ids) - len(unconfirmed_ids) - len(filled_ids))
        pending_confirmation = (
            bool(unconfirmed_ids)
            and not filled_ids
            and len(errors) == write_error_count
            and CANCEL_ALL_PENDING_ON_UNCONFIRMED_ENABLED
        )
        result_ok = (not errors and bool(reconcile.get("ok", True))) or pending_confirmation
        record_order_event(
            operation="cancel_all_orders",
            result="pending" if pending_confirmation else "ok" if result_ok else "error",
            reason_code=str(reconcile.get("error") or (errors[0] if errors else "ok")),
            duration_s=time.perf_counter() - started,
        )
        return {
            "ok": result_ok,
            "status": "CANCEL_ALL_REQUESTED" if pending_confirmation else "CANCELLED" if result_ok else "",
            "pending_confirmation": pending_confirmation,
            "cancelled": cancelled,
            "requested": len(active_order_ids),
            "submitted": len(submitted_order_ids),
            "active_order_ids": list(active_order_ids),
            "submitted_order_ids": list(submitted_order_ids),
            "order_ids": list(submitted_order_ids),
            "remaining_order_ids": list(reconcile.get("remaining_order_ids") or []),
            "skipped_terminal": skipped_terminal,
            "errors": errors,
            "error_details": error_details,
            "pending_error_details": pending_error_details,
            "reconcile": reconcile,
            "order_list_source": order_list_source,
            "order_list_error": order_list_error,
            "global_cancel_submitted": global_cancel_submitted,
            "global_cancel_error": global_cancel_error,
        }
