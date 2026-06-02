from __future__ import annotations

import copy
import hashlib
import logging
import math
import threading
import time
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


logger = logging.getLogger(__name__)


TICK_BY_TICK_DUPLICATE_WINDOW_SECONDS = 15.0


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
        self._status_code = 0

        self._market_data_listeners: list[Callable[[dict], None]] = []
        self._order_update_listeners: list[Callable[[dict], None]] = []
        self._ticker_meta: Dict[int, dict] = {}
        self._ticker_payloads: Dict[int, dict] = {}
        self._conid_to_ticker: Dict[int, int] = {}
        self._tick_by_tick_meta: Dict[int, dict] = {}
        self._tick_by_tick_by_conid: Dict[tuple[int, str], int] = {}
        self._tick_by_tick_last_request_at: Dict[tuple[int, str], float] = {}
        self._open_orders: Dict[str, dict] = {}
        self._open_order_objects: Dict[str, tuple[Any, Any]] = {}
        self._order_errors: Dict[str, dict] = {}
        self._positions: Dict[str, dict] = {}
        self._executions: Dict[str, dict] = {}
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

    def connect_and_start(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> bool:
        if not IBAPI_AVAILABLE:
            raise RuntimeError(f"ibapi not available: {IBAPI_IMPORT_ERROR}")
        with self._connect_lock:
            if self._ready and self.isConnected():
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
            return ready

    def disconnect_and_stop(self):
        with self._connect_lock:
            try:
                if self.isConnected():
                    self.disconnect()
            finally:
                self._ready = False
                self._ready_event.clear()
                self._last_disconnect_at = time.time()
                self._status_code = 0
                self._next_order_id = None
                self._order_errors = {}
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

    def error(self, reqId: int, errorCode: int, errorString: str, _advancedOrderRejectJson: str = ""):  # noqa: N802
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
            if errorCode not in BENIGN_ERROR_CODES:
                logger.warning("IB Gateway error reqId=%s code=%s message=%s", reqId, errorCode, errorString)
                numeric_req_id = int(reqId or 0)
                if numeric_req_id > 0:
                    self._order_errors[str(numeric_req_id)] = {
                        "order_id": str(numeric_req_id),
                        "code": int(errorCode or 0),
                        "message": str(errorString or ""),
                        "advanced_reject_json": str(_advancedOrderRejectJson or ""),
                        "at": _iso_now(),
                    }
            if errorCode in {502, 504, 1100, 2110}:
                self._ready = False
                self._status_code = 503
                self._next_order_id = None
                self._ready_event.clear()
        ctx = self._pending_requests.get(int(reqId or 0))
        if ctx and errorCode not in BENIGN_ERROR_CODES:
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

    def _next_request(self, kind: str) -> tuple[int, _PendingRequest]:
        with self._state_lock:
            self._request_seq += 1
            req_id = self._request_seq
        ctx = _PendingRequest(kind=kind)
        self._pending_requests[req_id] = ctx
        return req_id, ctx

    def _await(self, req_id: int, ctx: _PendingRequest, timeout: int) -> list:
        if not ctx.event.wait(timeout=max(1, int(timeout))):
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"{ctx.kind}_timeout")
        self._pending_requests.pop(req_id, None)
        if ctx.error:
            raise RuntimeError(ctx.error)
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
                    ctx.items = [dict(item) for item in self._open_orders.values()]
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
        self._executions[exec_id] = {
            "execId": exec_id,
            "orderId": order_id,
            "conid": int(getattr(contract, "conId", 0) or 0),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "side": str(getattr(execution, "side", "") or "").upper(),
            "shares": shares,
            "price": price,
            "time": str(getattr(execution, "time", "") or ""),
            "account": str(getattr(execution, "acctNumber", "") or ""),
            "commission": 0.0,
        }
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.items = [dict(item) for item in self._executions.values()]
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
        if exec_id and exec_id in self._executions:
            self._executions[exec_id]["commission"] = _safe_float(
                getattr(commissionReport, "commission", 0),
                0.0,
            )

    def _emit_order_update(self, payload: dict):
        with self._listener_lock:
            listeners = list(self._order_update_listeners)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception:
                logger.exception("Order update listener failed")

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
            self.reqMktData(req_id, contract, "233", True, False, [])

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

    def request_open_orders(
        self,
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        *,
        include_all: bool = False,
    ) -> List[dict]:
        self._ensure_ready(timeout, "request_open_orders")
        req_id, ctx = self._next_request("open_orders_all" if include_all else "open_orders")
        if include_all:
            self.reqAllOpenOrders()
        else:
            self.reqOpenOrders()
        return self._await(req_id, ctx, timeout)

    def request_positions(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self._ensure_ready(timeout, "request_positions")
        req_id, ctx = self._next_request("positions")
        self._positions = {}
        self.reqPositions()
        return self._await(req_id, ctx, timeout)

    def request_account_summary(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> Dict[str, dict]:
        self._ensure_ready(timeout, "request_account_summary")
        req_id, ctx = self._next_request("account_summary")
        self.reqAccountSummary(req_id, "All", "NetLiquidation,BuyingPower,AvailableFunds,ExcessLiquidity")
        items = self._await(req_id, ctx, timeout)
        if not items:
            return {}
        account = self._managed_accounts.split(",", 1)[0].strip() if self._managed_accounts else ""
        return dict(self._account_summary.get(account) or items[0] or {})

    def request_account_updates(
        self,
        *,
        account: str = "",
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        with self._account_updates_request_lock:
            self._ensure_ready(timeout, "request_account_updates")
            requested_account = str(account or "").strip()
            if not requested_account and self._managed_accounts:
                requested_account = self._managed_accounts.split(",", 1)[0].strip()
            if not requested_account:
                raise RuntimeError("missing_managed_account")

            capture = _AccountUpdatesCapture(account=requested_account)
            with self._account_updates_lock:
                self._account_updates_capture = capture
            try:
                self.reqAccountUpdates(True, requested_account)
                if not capture.event.wait(timeout=max(1, int(timeout))):
                    raise TimeoutError("account_updates_timeout")
                return {
                    "account": requested_account,
                    "summary": dict(capture.summary),
                    "positions": [dict(item) for item in capture.positions.values()],
                }
            finally:
                try:
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
        self._ensure_ready(timeout, "request_account_pnl")
        requested_account = str(account or "").strip()
        if not requested_account and self._managed_accounts:
            requested_account = self._managed_accounts.split(",", 1)[0].strip()
        if not requested_account:
            raise RuntimeError("missing_managed_account")
        if not callable(getattr(self, "reqPnL", None)):
            raise RuntimeError("req_pnl_unavailable")

        req_id, ctx = self._next_request("account_pnl")
        subscribed = False
        try:
            self.reqPnL(req_id, requested_account, str(model_code or ""))
            subscribed = True
            items = self._await(req_id, ctx, timeout)
            payload = dict(items[0] or {}) if items else {}
            if not payload:
                return {}
            payload.setdefault("source", "reqPnL")
            payload["account"] = requested_account
            payload["model_code"] = str(model_code or "")
            return payload
        finally:
            self._pending_requests.pop(req_id, None)
            if subscribed:
                try:
                    self.cancelPnL(req_id)
                except Exception:
                    logger.debug("cancelPnL failed for req_id=%s account=%s", req_id, requested_account, exc_info=True)

    def request_executions(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self._ensure_ready(timeout, "request_executions")
        req_id, ctx = self._next_request("executions")
        self.reqExecutions(req_id, ExecutionFilter())
        items = self._await(req_id, ctx, timeout)
        # IB sends commissionReport callbacks separately from execDetailsEnd.
        # Give those callbacks a short window, then return the refreshed cache.
        exec_ids = [str(item.get("execId") or "") for item in (items or []) if str(item.get("execId") or "")]
        if exec_ids:
            deadline = time.time() + min(2.0, max(0.0, float(timeout or 0)) * 0.25)
            while time.time() < deadline:
                if all(abs(_safe_float(self._executions.get(exec_id, {}).get("commission"), 0.0)) > 0 for exec_id in exec_ids):
                    break
                time.sleep(0.1)
            return [dict(self._executions.get(exec_id) or item) for exec_id, item in zip(exec_ids, items)]
        return items

    def place_order(self, contract: Any, order: Any, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        self._ensure_ready(timeout, "place_order")
        self.placeOrder(int(order.orderId), contract, order)

    def cancel_open_order(self, order_id: str, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        self._ensure_ready(timeout, "cancel_open_order")
        self.cancelOrder(int(order_id))

    def get_order_snapshot(self, order_id: str) -> dict:
        return dict(self._open_orders.get(str(order_id)) or {})

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

        deadline = time.time() + max(0.5, float(timeout or 0.0))
        request_timeout = max(1, min(3, int(max(1.0, float(timeout or 0.0)))))

        while time.time() < deadline:
            order_error = self.get_order_error(normalized_order_id)
            if order_error:
                return {
                    "ok": False,
                    "order_id": normalized_order_id,
                    "error": str(order_error.get("message") or "order_rejected"),
                    "details": order_error,
                }

            snapshot = self.get_order_snapshot(normalized_order_id)
            if snapshot:
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "source": "snapshot",
                    "order": snapshot,
                }

            try:
                open_orders = self.request_open_orders(timeout=request_timeout)
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
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "source": "open_orders",
                        "order": dict(open_order),
                    }

            time.sleep(max(0.05, float(poll_interval or 0.2)))

        order_error = self.get_order_error(normalized_order_id)
        if order_error:
            return {
                "ok": False,
                "order_id": normalized_order_id,
                "error": str(order_error.get("message") or "order_rejected"),
                "details": order_error,
            }

        return {
            "ok": False,
            "order_id": normalized_order_id,
            "error": "order_submission_unconfirmed",
        }

    def await_order_submissions(
        self,
        order_ids: Iterable[str],
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.2,
    ) -> dict:
        expected_ids = [str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()]
        if not expected_ids:
            return {"ok": False, "error": "missing_order_ids", "orders": {}, "missing_order_ids": []}

        deadline = time.time() + max(0.5, float(timeout or 0.0))
        request_timeout = max(1, min(3, int(max(1.0, float(timeout or 0.0)))))
        confirmed: dict[str, dict] = {}
        failures: dict[str, dict] = {}

        while time.time() < deadline and len(confirmed) + len(failures) < len(expected_ids):
            for order_id in expected_ids:
                if order_id in confirmed or order_id in failures:
                    continue
                order_error = self.get_order_error(order_id)
                if order_error:
                    failures[order_id] = {
                        "ok": False,
                        "order_id": order_id,
                        "error": str(order_error.get("message") or "order_rejected"),
                        "details": order_error,
                    }
                    continue
                snapshot = self.get_order_snapshot(order_id)
                if snapshot:
                    confirmed[order_id] = {
                        "ok": True,
                        "order_id": order_id,
                        "source": "snapshot",
                        "order": snapshot,
                    }

            missing_ids = [
                order_id
                for order_id in expected_ids
                if order_id not in confirmed and order_id not in failures
            ]
            if not missing_ids:
                break

            try:
                open_orders = self.request_open_orders(timeout=request_timeout)
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
                    confirmed[open_order_id] = {
                        "ok": True,
                        "order_id": open_order_id,
                        "source": "open_orders",
                        "order": dict(open_order),
                    }

            time.sleep(max(0.05, float(poll_interval or 0.2)))

        for order_id in expected_ids:
            if order_id in confirmed or order_id in failures:
                continue
            order_error = self.get_order_error(order_id)
            if order_error:
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
        if failures:
            first = next(iter(failures.values()))
            return {
                "ok": False,
                "error": str(first.get("error") or "order_submission_failed"),
                "orders": confirmed,
                "failures": failures,
                "missing_order_ids": missing_ids,
            }
        if missing_ids:
            return {
                "ok": False,
                "error": "order_submission_unconfirmed",
                "orders": confirmed,
                "failures": failures,
                "missing_order_ids": missing_ids,
            }
        return {
            "ok": True,
            "orders": confirmed,
            "failures": {},
            "missing_order_ids": [],
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
        contract = self.resolve_contract(symbol=symbol, conid=conid, exchange=exchange, sec_type=sec_type)
        if not contract:
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        return self.client.request_historical_bars(
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

    def list_open_orders(self, *, include_all: bool = False) -> List[dict]:
        return self.client.request_open_orders(include_all=include_all)

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
    ) -> tuple[dict, bool, bool]:
        normalized_order_id = str(order_id or "").strip()
        snapshot: dict = {}
        getter = getattr(self.client, "get_order_snapshot", None)
        if callable(getter):
            try:
                snapshot = dict(getter(normalized_order_id) or {})
            except Exception:
                snapshot = {}

        request_open_orders = getattr(self.client, "request_open_orders", None)
        if not callable(request_open_orders):
            return snapshot, False, False

        refreshed = False
        try:
            try:
                open_orders = request_open_orders(
                    timeout=max(1, min(3, int(max(1.0, float(timeout or 0.0))))),
                    include_all=include_all,
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
        deadline = time.time() + max(0.5, float(timeout or 0.0))
        last_snapshot: dict = {}
        ignored_errors: list[dict] = []
        while time.time() < deadline:
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
                else:
                    return {
                        "ok": False,
                        "order_id": normalized_order_id,
                        "error": str(order_error.get("message") or "order_cancel_rejected"),
                        "details": order_error,
                    }

            snapshot, found_open, refreshed = self._request_order_snapshot(
                normalized_order_id,
                timeout=timeout,
                include_all=True,
            )
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
                return {
                    "ok": True,
                    "order_id": normalized_order_id,
                    "status": status,
                    "order": snapshot,
                    "ignored_errors": ignored_errors,
                }
            if refreshed and not found_open:
                if not snapshot:
                    return {
                        "ok": True,
                        "order_id": normalized_order_id,
                        "status": "NOT_OPEN",
                        "order": {},
                        "source": "open_orders_missing",
                        "ignored_errors": ignored_errors,
                    }
                # A missing open-order row with a stale Submitted snapshot is
                # ambiguous (cancel vs. fill); wait for a terminal callback.
            time.sleep(max(0.05, float(poll_interval or 0.2)))
        return {
            "ok": False,
            "order_id": normalized_order_id,
            "error": "order_cancel_unconfirmed",
            "order": last_snapshot,
            "ignored_errors": ignored_errors,
        }

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
                    "error": str(order_error.get("message") or "order_modify_rejected"),
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
    ) -> dict:
        contract_info = self.resolve_contract(symbol=symbol, conid=conid)
        if not contract_info:
            return {"ok": False, "error": "contract_not_found"}
        contract = Contract()
        contract.conId = int(contract_info.get("conid") or conid)
        contract.symbol = str(contract_info.get("symbol") or symbol)
        contract.secType = str(contract_info.get("sec_type") or "STK")
        contract.exchange = str(contract_info.get("exchange") or "SMART")
        contract.currency = str(contract_info.get("currency") or "USD")

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
        oca_group = group if order_family_type == "bracket_oco" else ""
        entry_ref = f"entry_{group}"
        tp_ref = f"tp_{group}"
        sl_ref = f"sl_{group}"
        account_id = str(account_id or "").strip()
        normalized_entry_price = self._normalize_order_price(entry_price)
        normalized_tp_price = self._normalize_order_price(take_profit_price)
        normalized_sl_price = self._normalize_order_price(stop_loss_price)
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
        if entry.orderType == "LMT":
            entry.lmtPrice = float(normalized_entry_price)

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

        try:
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
                "price_normalization": price_normalization,
            }

        submission_result = self.client.await_order_submissions(
            [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
            timeout=5.0,
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
                    "price_normalization": price_normalization,
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
                "price_normalization": price_normalization,
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
            "price_normalization": price_normalization,
            "submission": submission_result,
            "protection_complete": True,
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
        order_type: str = "MKT",
        limit_price: float = 0.0,
        wait_for_fill: bool = False,
        fill_timeout: float = 5.0,
    ) -> dict:
        contract_info = self.resolve_contract(symbol=symbol, conid=conid)
        if not contract_info:
            return {"ok": False, "error": "contract_not_found"}
        contract = Contract()
        contract.conId = int(contract_info.get("conid") or conid)
        contract.symbol = str(contract_info.get("symbol") or symbol)
        contract.secType = str(contract_info.get("sec_type") or "STK")
        contract.exchange = str(contract_info.get("exchange") or "SMART")
        contract.currency = str(contract_info.get("currency") or "USD")
        order_id = self.client.next_order_ids(1)[0]
        side = "SELL" if str(direction).lower() == "long" else "BUY"
        order = Order()
        order.orderId = int(order_id)
        order.action = side
        normalized_order_type = str(order_type or "MKT").strip().upper()
        wants_limit = normalized_order_type in {"LMT", "LIMIT", "MARKETABLE_LIMIT"}
        normalized_limit_price = self._normalize_order_price(limit_price) if wants_limit else 0.0
        price_normalization = self._price_normalization_details(limit_price=limit_price) if wants_limit else {}
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
        order.tif = "DAY"
        order_ref = str(order_ref or "").strip()
        order.orderRef = order_ref or f"close_{contract.symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        account_id = str(account_id or "").strip()
        if account_id:
            order.account = account_id
        self._clear_legacy_order_flags(order)
        try:
            self.client.clear_order_error(str(order_id))
            self.client.place_order(contract, order)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        entry_result = self.client.await_order_submission(str(order_id), timeout=3.0, poll_interval=0.2)
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

    def modify_order(self, order_id: str, updates: dict, account_id: str = "") -> dict:
        contract, order = self.client.get_order_objects(order_id)
        if not contract or not order:
            try:
                self.list_open_orders()
            except Exception:
                pass
            contract, order = self.client.get_order_objects(order_id)
        if not contract or not order:
            return {"ok": False, "error": "order_not_found"}
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
        if "tif" in updates and updates["tif"]:
            order.tif = str(updates["tif"])
        account_id = str(account_id or "").strip()
        if account_id:
            order.account = account_id
        self._clear_legacy_order_flags(order)
        try:
            self.client.clear_order_error(str(order_id))
            self.client.place_order(contract, order)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        entry_result = self.client.await_order_submission(str(order_id), timeout=3.0, poll_interval=0.2)
        if not entry_result.get("ok"):
            order_error = entry_result.get("details") or {}
            error_message = str(entry_result.get("error") or "order_submission_failed")
            if order_error.get("code"):
                error_message = f"{error_message} (code={order_error.get('code')})"
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
                timeout=3.0,
                poll_interval=0.2,
            )
            if not confirm_result.get("ok"):
                return {
                    "ok": False,
                    "error": str(confirm_result.get("error") or "order_modify_unconfirmed"),
                    "entry_error": entry_result,
                    "confirm": confirm_result,
                    "order_id": str(order_id),
                    "price_normalization": price_normalization,
                }
            return {
                "ok": True,
                "order_id": str(order_id),
                "order": confirm_result.get("order") or {},
                "price_normalization": price_normalization,
            }
        return {
            "ok": True,
            "order_id": str(order_id),
            "order": entry_result.get("order") or {},
            "price_normalization": price_normalization,
        }

    def get_order_snapshot(self, order_id: str) -> dict:
        return self.client.get_order_snapshot(order_id)

    def cancel_order(self, order_id: str) -> dict:
        try:
            clearer = getattr(self.client, "clear_order_error", None)
            if callable(clearer):
                clearer(str(order_id))
            self.client.cancel_open_order(order_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        confirm_result = self.await_order_cancelled(str(order_id), timeout=3.0, poll_interval=0.2)
        if not confirm_result.get("ok"):
            return {
                "ok": False,
                "order_id": str(order_id),
                "error": str(confirm_result.get("error") or "order_cancel_unconfirmed"),
                "confirm": confirm_result,
            }
        return {
            "ok": True,
            "order_id": str(order_id),
            "status": confirm_result.get("status") or "",
            "confirm": confirm_result,
        }

    def cancel_all_orders(self) -> dict:
        orders = self.list_open_orders()
        cancelled = 0
        errors = []
        for order in orders:
            status = str(order.get("status") or "").strip().lower()
            if status in {"filled", "cancelled", "canceled", "inactive"}:
                continue
            result = self.cancel_order(str(order.get("orderId") or order.get("id") or ""))
            if result.get("ok"):
                cancelled += 1
            else:
                errors.append(result.get("error") or "cancel_failed")
        return {"ok": not errors, "cancelled": cancelled, "errors": errors}
