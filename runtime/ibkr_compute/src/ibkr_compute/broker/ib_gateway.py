from __future__ import annotations

import copy
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from ibapi.client import EClient
    from ibapi.commission_report import CommissionReport
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper

    IBAPI_AVAILABLE = True
    IBAPI_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - import availability depends on runtime env
    IBAPI_AVAILABLE = False
    IBAPI_IMPORT_ERROR = str(exc)

    class EWrapper:  # type: ignore[override]
        pass

    class EClient:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

    class Contract:  # type: ignore[override]
        pass

    class Order:  # type: ignore[override]
        pass

    class CommissionReport:  # type: ignore[override]
        pass

    class ExecutionFilter:  # type: ignore[override]
        pass


logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))

DEFAULT_HOST = os.environ.get("IBGW_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("IBGW_PORT", "4001"))
DEFAULT_CLIENT_ID = int(os.environ.get("IBGW_CLIENT_ID", "31"))
DEFAULT_CONNECT_TIMEOUT_SECONDS = max(3, int(os.environ.get("IBGW_CONNECT_TIMEOUT_SEC", "10")))
DEFAULT_LOGIN_TIMEOUT_SECONDS = max(30, int(os.environ.get("IBKR_LOGIN_TIMEOUT", "180")))
DEFAULT_LOGIN_POLL_INTERVAL_SECONDS = max(1, int(os.environ.get("IBKR_LOGIN_POLL_INTERVAL_SEC", "5")))
DEFAULT_SERVICE_NAME = os.environ.get("IBKR_GATEWAY_SYSTEMD_SERVICE", "ibkr-gateway").strip() or "ibkr-gateway"
DEFAULT_ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live").strip().lower() or "live"

TICK_LAST_PRICE = 4
TICK_BID_PRICE = 1
TICK_ASK_PRICE = 2
TICK_BID_SIZE = 0
TICK_ASK_SIZE = 3
TICK_LAST_SIZE = 5
TICK_VOLUME = 8
TICK_LAST_TIMESTAMP = 45
BENIGN_ERROR_CODES = {2104, 2106, 2107, 2108, 2158}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


def _iso_now() -> str:
    return datetime.now(ET).isoformat()


def _run_command(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _systemctl_show(service: str) -> Dict[str, str]:
    proc = _run_command(
        [
            "systemctl",
            "show",
            service,
            "--property=Id,ActiveState,SubState,MainPID,ActiveEnterTimestamp,UnitFileState",
            "--no-pager",
        ],
        timeout=20,
    )
    data: Dict[str, str] = {}
    if proc is None:
        return data
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _pid_uptime_seconds(pid: int) -> Optional[float]:
    if int(pid or 0) <= 0:
        return None
    proc = _run_command(["ps", "-o", "etimes=", "-p", str(int(pid))], timeout=5)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return float((proc.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def _ib_timestamp_to_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        raw = int(text)
        return raw if raw > 1_000_000_000_000 else raw * 1000
    for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d-%H:%M:%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(text.split(" ", 1)[0] if fmt == "%Y%m%d" else text, fmt)
            return int(dt.replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    return 0


@dataclass
class _PendingRequest:
    kind: str
    event: threading.Event = field(default_factory=threading.Event)
    items: list = field(default_factory=list)
    error: str = ""


@dataclass
class _AccountUpdatesCapture:
    account: str
    event: threading.Event = field(default_factory=threading.Event)
    summary: Dict[str, dict] = field(default_factory=dict)
    positions: Dict[str, dict] = field(default_factory=dict)


class _IBGatewayApp(EWrapper, EClient):
    def __init__(self, host: str, port: int, client_id: int):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.host = host
        self.port = int(port)
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
        self._status_code = 0

        self._market_data_listeners: list[Callable[[dict], None]] = []
        self._order_update_listeners: list[Callable[[dict], None]] = []
        self._ticker_meta: Dict[int, dict] = {}
        self._ticker_payloads: Dict[int, dict] = {}
        self._conid_to_ticker: Dict[int, int] = {}
        self._open_orders: Dict[str, dict] = {}
        self._open_order_objects: Dict[str, tuple[Any, Any]] = {}
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
            self._last_disconnect_at = time.time()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

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
                super().connect(self.host, self.port, self.client_id)
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
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def connectionClosed(self):  # noqa: N802
        with self._state_lock:
            self._ready = False
            self._status_code = 0
            self._ready_event.clear()
            self._last_disconnect_at = time.time()

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
            if errorCode not in BENIGN_ERROR_CODES:
                logger.warning("IB Gateway error reqId=%s code=%s message=%s", reqId, errorCode, errorString)
            if errorCode in {502, 504, 1100, 2110}:
                self._ready = False
                self._status_code = 503
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

    def _emit_tick(self, ticker_id: int):
        payload = dict(self._ticker_payloads.get(ticker_id) or {})
        if not payload:
            return
        payload["_updated"] = int(time.time() * 1000)
        with self._listener_lock:
            listeners = list(self._market_data_listeners)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception:
                logger.exception("Market data listener failed")

    def tickPrice(self, tickerId: int, field: int, price: float, _attrib):  # noqa: N802
        payload = self._ticker_payloads.setdefault(int(tickerId), dict(self._ticker_meta.get(int(tickerId)) or {}))
        if field == TICK_LAST_PRICE:
            payload["31"] = float(price)
        elif field == TICK_BID_PRICE:
            payload["84"] = float(price)
        elif field == TICK_ASK_PRICE:
            payload["86"] = float(price)
        self._emit_tick(int(tickerId))

    def tickSize(self, tickerId: int, field: int, size: int):  # noqa: N802
        payload = self._ticker_payloads.setdefault(int(tickerId), dict(self._ticker_meta.get(int(tickerId)) or {}))
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
            payload = self._ticker_payloads.setdefault(int(tickerId), dict(self._ticker_meta.get(int(tickerId)) or {}))
            payload["_updated"] = int(float(value) * 1000)
            self._emit_tick(int(tickerId))

    def tickString(self, tickerId: int, tickType: int, value: str):  # noqa: N802
        if int(tickType) != 48:
            return
        parts = str(value or "").split(";")
        if len(parts) < 6:
            return
        payload = self._ticker_payloads.setdefault(int(tickerId), dict(self._ticker_meta.get(int(tickerId)) or {}))
        payload["31"] = _safe_float(parts[0], 0.0)
        payload["7059"] = _safe_float(parts[1], 0.0)
        payload["87"] = _safe_float(parts[3], 0.0)
        payload["_updated"] = _safe_int(parts[5], int(time.time() * 1000))
        self._emit_tick(int(tickerId))

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
        normalized = {
            "orderId": str(orderId),
            "id": str(orderId),
            "conid": int(getattr(contract, "conId", 0) or 0),
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
            "cOID": str(getattr(order, "orderRef", "") or ""),
            "orderRef": str(getattr(order, "orderRef", "") or ""),
            "tif": str(getattr(order, "tif", "") or ""),
            "filledQuantity": 0.0,
            "remainingQuantity": _safe_float(getattr(order, "totalQuantity", 0), 0.0),
            "avgFillPrice": 0.0,
            "avgPrice": 0.0,
            "updated_at": _iso_now(),
        }
        key = str(orderId)
        with self._state_lock:
            previous = dict(self._open_orders.get(key) or {})
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
        patch = {
            "orderId": key,
            "id": key,
            "status": str(status or ""),
            "filledQuantity": _safe_float(filled, 0.0),
            "remainingQuantity": _safe_float(remaining, 0.0),
            "avgFillPrice": _safe_float(avgFillPrice, 0.0),
            "avgPrice": _safe_float(avgFillPrice, 0.0),
            "updated_at": _iso_now(),
        }
        with self._state_lock:
            current = dict(self._open_orders.get(key) or {})
            merged = {**current, **patch}
            self._open_orders[key] = merged
        self._emit_order_update(merged)

    def openOrderEnd(self):  # noqa: N802
        for req_id, ctx in list(self._pending_requests.items()):
            if ctx.kind == "open_orders":
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
        self._executions[exec_id] = {
            "execId": exec_id,
            "orderId": str(getattr(execution, "orderId", "") or ""),
            "conid": int(getattr(contract, "conId", 0) or 0),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "side": str(getattr(execution, "side", "") or "").upper(),
            "shares": _safe_float(getattr(execution, "shares", 0), 0.0),
            "price": _safe_float(getattr(execution, "price", 0), 0.0),
            "time": str(getattr(execution, "time", "") or ""),
            "account": str(getattr(execution, "acctNumber", "") or ""),
            "commission": 0.0,
        }
        ctx = self._pending_requests.get(int(reqId))
        if ctx:
            ctx.items = [dict(item) for item in self._executions.values()]

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

    def next_order_ids(self, count: int) -> List[int]:
        if not self._ready_event.wait(DEFAULT_CONNECT_TIMEOUT_SECONDS):
            raise RuntimeError("ib_gateway_not_ready")
        count = max(1, int(count))
        with self._state_lock:
            if self._next_order_id is None:
                raise RuntimeError("missing_next_order_id")
            start = int(self._next_order_id)
            self._next_order_id += count
        return list(range(start, start + count))

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
        timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        req_id, ctx = self._next_request("contract_details")
        contract = Contract()
        if int(conid or 0) > 0:
            contract.conId = int(conid)
        if symbol:
            contract.symbol = str(symbol or "").upper()
            contract.secType = "STK"
            contract.exchange = str(exchange or "SMART")
            contract.currency = str(currency or "USD")
        elif exchange:
            contract.exchange = str(exchange)
        elif currency:
            contract.currency = str(currency)
        self.reqContractDetails(req_id, contract)
        return self._await(req_id, ctx, timeout)

    def request_matching_symbols(self, query: str, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        req_id, ctx = self._next_request("matching_symbols")
        self.reqMatchingSymbols(req_id, str(query or "").strip())
        return self._await(req_id, ctx, timeout)

    def request_historical_bars(
        self,
        *,
        conid: int,
        symbol: str,
        duration: str,
        bar_size: str,
        end_datetime: str = "",
        use_rth: bool = False,
        timeout: int = 30,
    ) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        details = self.request_contract_details(symbol=symbol, conid=conid, timeout=timeout)
        if not details:
            raise RuntimeError(f"contract_not_found:{symbol or conid}")
        contract = Contract()
        contract.conId = int(details[0]["conid"])
        contract.symbol = str(details[0]["symbol"])
        contract.secType = str(details[0]["sec_type"] or "STK")
        contract.exchange = str(details[0]["exchange"] or "SMART")
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
            1,
            False,
            [],
        )
        return self._await(req_id, ctx, timeout)

    def request_open_orders(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        req_id, ctx = self._next_request("open_orders")
        self.reqOpenOrders()
        return self._await(req_id, ctx, timeout)

    def request_positions(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        req_id, ctx = self._next_request("positions")
        self._positions = {}
        self.reqPositions()
        return self._await(req_id, ctx, timeout)

    def request_account_summary(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> Dict[str, dict]:
        self.connect_and_start(timeout=timeout)
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
            self.connect_and_start(timeout=timeout)
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

    def request_executions(self, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> List[dict]:
        self.connect_and_start(timeout=timeout)
        req_id, ctx = self._next_request("executions")
        self.reqExecutions(req_id, ExecutionFilter())
        return self._await(req_id, ctx, timeout)

    def place_order(self, contract: Any, order: Any, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        self.connect_and_start(timeout=timeout)
        self.placeOrder(int(order.orderId), contract, order)

    def cancel_open_order(self, order_id: str, timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS):
        self.connect_and_start(timeout=timeout)
        self.cancelOrder(int(order_id))

    def get_order_snapshot(self, order_id: str) -> dict:
        return dict(self._open_orders.get(str(order_id)) or {})

    def get_order_objects(self, order_id: str) -> tuple[Any, Any] | tuple[None, None]:
        item = self._open_order_objects.get(str(order_id))
        if not item:
            return None, None
        contract, order = item
        return copy.deepcopy(contract), copy.deepcopy(order)

    def status(self) -> dict:
        with self._state_lock:
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
                "managed_accounts": str(self._managed_accounts or ""),
                "subscriptions": len(self._conid_to_ticker),
            }


class GatewayServiceManager:
    def __init__(self, service_name: str = DEFAULT_SERVICE_NAME, broker: Optional["BrokerAdapter"] = None):
        self.service_name = str(service_name or DEFAULT_SERVICE_NAME)
        self.broker = broker

    def _systemctl(self, action: str) -> bool:
        proc = _run_command(["systemctl", action, self.service_name], timeout=30)
        return bool(proc and proc.returncode == 0)

    @property
    def pid(self) -> int:
        return _safe_int(_systemctl_show(self.service_name).get("MainPID"), 0)

    @property
    def is_running(self) -> bool:
        return str(_systemctl_show(self.service_name).get("ActiveState") or "") == "active"

    @property
    def uptime_seconds(self) -> Optional[float]:
        return _pid_uptime_seconds(self.pid)

    def start(self) -> bool:
        return self._systemctl("start") and self.is_running

    def stop(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        return self._systemctl("stop")

    def restart(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        return self._systemctl("restart") and self.is_running

    def recent_logs(self, lines: int = 50, since_minutes: int = 10) -> List[str]:
        proc = _run_command(
            [
                "journalctl",
                "-u",
                self.service_name,
                "-n",
                str(max(1, int(lines))),
                "--since",
                f"{max(1, int(since_minutes))} minutes ago",
                "--no-pager",
            ],
            timeout=20,
        )
        if proc is None:
            return []
        return [line for line in (proc.stdout or "").splitlines() if line.strip()]

    def status(self) -> dict:
        data = _systemctl_show(self.service_name)
        broker_status = self.broker.status() if self.broker else {}
        running = str(data.get("ActiveState") or "") == "active"
        status_code = int(broker_status.get("status_code", 0) or 0)
        if not running:
            status_code = 503
        elif running and not status_code:
            status_code = 401
        return {
            "managed_by": "systemd",
            "service": self.service_name,
            "pid": _safe_int(data.get("MainPID"), 0),
            "uptime_s": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
            "reachable": bool(running and status_code != 503),
            "running": running,
            "status_code": status_code,
            "active_state": str(data.get("ActiveState") or ""),
            "sub_state": str(data.get("SubState") or ""),
            "active_since": str(data.get("ActiveEnterTimestamp") or ""),
            "unit_file_state": str(data.get("UnitFileState") or ""),
            "broker": broker_status,
        }


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

    def add_market_data_listener(self, callback: Callable[[dict], None]):
        self.client.add_market_data_listener(callback)

    def remove_market_data_listener(self, callback: Callable[[dict], None]):
        self.client.remove_market_data_listener(callback)

    def add_order_update_listener(self, callback: Callable[[dict], None]):
        self.client.add_order_update_listener(callback)

    def remove_order_update_listener(self, callback: Callable[[dict], None]):
        self.client.remove_order_update_listener(callback)

    def resolve_contract(self, symbol: str = "", conid: int = 0) -> Optional[dict]:
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol and normalized_symbol in self.client._contract_cache_by_symbol:
            return dict(self.client._contract_cache_by_symbol[normalized_symbol])
        if int(conid or 0) > 0 and int(conid) in self.client._contract_cache_by_conid:
            return dict(self.client._contract_cache_by_conid[int(conid)])

        if int(conid or 0) > 0 or normalized_symbol:
            try:
                details = self.client.request_contract_details(symbol=normalized_symbol, conid=int(conid or 0))
            except Exception:
                details = []
            if details:
                return dict(details[0])

        if not normalized_symbol:
            return None

        try:
            samples = self.client.request_matching_symbols(normalized_symbol)
        except Exception:
            samples = []

        best = None
        for item in samples:
            symbol_match = str(item.get("symbol") or "").strip().upper() == normalized_symbol
            sec_type = str(item.get("sec_type") or "").strip().upper()
            if symbol_match and sec_type in {"STK", "ETF", "IND"}:
                best = item
                break
            if best is None:
                best = item
        if best:
            try:
                details = self.client.request_contract_details(
                    symbol=str(best.get("symbol") or normalized_symbol),
                    conid=int(best.get("conid") or 0),
                )
            except Exception:
                details = []
            if details:
                return dict(details[0])
            return dict(best)
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
        duration: str,
        bar_size: str,
        end_datetime: str = "",
        use_rth: bool = False,
        timeout: int = 30,
    ) -> List[dict]:
        return self.client.request_historical_bars(
            conid=conid,
            symbol=symbol,
            duration=duration,
            bar_size=bar_size,
            end_datetime=end_datetime,
            use_rth=use_rth,
            timeout=timeout,
        )

    def subscribe_market_data(self, conid: int, symbol: str, exchange: str = "SMART") -> int:
        contract = self.resolve_contract(symbol=symbol, conid=conid)
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

    def list_positions(self) -> List[dict]:
        return self.client.request_positions()

    def get_account_summary(self) -> Dict[str, dict]:
        return self.client.request_account_summary()

    def get_account_snapshot(self, account: str = "") -> Dict[str, Any]:
        return self.client.request_account_updates(account=account)

    def list_open_orders(self) -> List[dict]:
        return self.client.request_open_orders()

    def list_recent_fills(self) -> List[dict]:
        return self.client.request_executions()

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
        entry_order_type: str = "LMT",
        tif: str = "DAY",
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

        order_ids = self.client.next_order_ids(3)
        side = "BUY" if str(direction).lower() == "long" else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
        group = f"{contract.symbol}_{direction}_{stamp}"
        entry_ref = f"entry_{group}"
        tp_ref = f"tp_{group}"
        sl_ref = f"sl_{group}"

        entry = Order()
        entry.orderId = int(order_ids[0])
        entry.action = side
        entry.orderType = str(entry_order_type or "LMT").upper()
        entry.totalQuantity = float(quantity)
        entry.tif = str(tif or "DAY")
        entry.orderRef = entry_ref
        entry.transmit = False
        if entry.orderType == "LMT":
            entry.lmtPrice = float(entry_price)

        tp = Order()
        tp.orderId = int(order_ids[1])
        tp.action = close_side
        tp.orderType = "LMT"
        tp.totalQuantity = float(quantity)
        tp.lmtPrice = float(take_profit_price)
        tp.tif = "GTC"
        tp.parentId = int(order_ids[0])
        tp.orderRef = tp_ref
        tp.transmit = False

        sl = Order()
        sl.orderId = int(order_ids[2])
        sl.action = close_side
        sl.orderType = "STP"
        sl.totalQuantity = float(quantity)
        sl.auxPrice = float(stop_loss_price)
        sl.tif = "GTC"
        sl.parentId = int(order_ids[0])
        sl.orderRef = sl_ref
        sl.transmit = True

        try:
            self.client.place_order(contract, entry)
            self.client.place_order(contract, tp)
            self.client.place_order(contract, sl)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "order_ids": [str(order_ids[0]), str(order_ids[1]), str(order_ids[2])],
            "bracket_group": group,
            "entry_coid": entry_ref,
            "tp_coid": tp_ref,
            "sl_coid": sl_ref,
        }

    def place_market_close(self, *, conid: int, symbol: str, direction: str, quantity: int) -> dict:
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
        order.orderType = "MKT"
        order.totalQuantity = float(quantity)
        order.tif = "DAY"
        order.orderRef = f"close_{contract.symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        try:
            self.client.place_order(contract, order)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "order_ids": [str(order_id)],
            "bracket_group": order.orderRef,
            "entry_coid": order.orderRef,
        }

    def modify_order(self, order_id: str, updates: dict) -> dict:
        contract, order = self.client.get_order_objects(order_id)
        if not contract or not order:
            try:
                self.list_open_orders()
            except Exception:
                pass
            contract, order = self.client.get_order_objects(order_id)
        if not contract or not order:
            return {"ok": False, "error": "order_not_found"}
        if "price" in updates and updates["price"] is not None:
            if str(getattr(order, "orderType", "") or "").upper() in {"STP", "STOP"}:
                order.auxPrice = float(updates["price"])
            else:
                order.lmtPrice = float(updates["price"])
        if "auxPrice" in updates and updates["auxPrice"] is not None:
            order.auxPrice = float(updates["auxPrice"])
        if "quantity" in updates and updates["quantity"] is not None:
            order.totalQuantity = float(updates["quantity"])
        if "tif" in updates and updates["tif"]:
            order.tif = str(updates["tif"])
        try:
            self.client.place_order(contract, order)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "order_id": str(order_id)}

    def get_order_snapshot(self, order_id: str) -> dict:
        return self.client.get_order_snapshot(order_id)

    def cancel_order(self, order_id: str) -> dict:
        try:
            self.client.cancel_open_order(order_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "order_id": str(order_id)}

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


class SocketSessionKeeper:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        gateway_manager: GatewayServiceManager,
        pb_client=None,
        on_session_expired: Optional[Callable[[], None]] = None,
        on_gateway_down: Optional[Callable[[], None]] = None,
        environment: str = DEFAULT_ENVIRONMENT,
    ):
        self.broker = broker
        self.gateway_manager = gateway_manager
        self.pb_client = pb_client
        self.environment = str(environment or DEFAULT_ENVIRONMENT)
        self.on_session_expired = on_session_expired
        self.on_gateway_down = on_gateway_down
        self.is_authenticated = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_check = ""
        self._consecutive_failures = 0
        self._last_transition = ""
        self._last_status_code = 0

    def check_auth_status(self) -> dict:
        running = bool(self.gateway_manager.is_running)
        previous = bool(self.is_authenticated)
        status = {
            "authenticated": False,
            "running": running,
            "gateway_running": running,
            "consecutive_failures": int(self._consecutive_failures or 0),
            "last_check": self._last_check,
        }
        if not running:
            self._consecutive_failures += 1
            self.is_authenticated = False
            self._last_status_code = 503
            if previous and callable(self.on_gateway_down):
                self.on_gateway_down()
            self._last_transition = "gateway_down"
            self._last_check = _iso_now()
            return {
                **status,
                "status_code": self._last_status_code,
                "last_check": self._last_check,
                "last_tickle": self._last_check,
            }
        try:
            health = self.broker.health()
            authenticated = bool(health.get("ready"))
            status.update(health)
            self.is_authenticated = authenticated
            self._last_status_code = int(health.get("status_code", 0) or 0)
            self._consecutive_failures = 0 if authenticated else self._consecutive_failures + 1
            self._last_transition = "authenticated" if authenticated else "unauthenticated"
            if previous and not authenticated and callable(self.on_session_expired):
                self.on_session_expired()
        except Exception as exc:
            self.is_authenticated = False
            self._consecutive_failures += 1
            status["error"] = str(exc)
            self._last_status_code = 503
            self._last_transition = "probe_failed"
            if previous and callable(self.on_session_expired):
                self.on_session_expired()
        self._last_check = _iso_now()
        status["authenticated"] = bool(self.is_authenticated)
        status["consecutive_failures"] = int(self._consecutive_failures or 0)
        status["status_code"] = int(self._last_status_code or 0)
        status["last_check"] = self._last_check
        status["last_tickle"] = self._last_check
        return status

    def start(self):
        if self._running:
            return
        self._running = True

        def loop():
            while self._running:
                try:
                    self.check_auth_status()
                except Exception:
                    logger.exception("Session keeper check failed")
                time.sleep(30)

        self._thread = threading.Thread(target=loop, daemon=True, name="ibgw-session-keeper")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def status(self) -> dict:
        return {
            "authenticated": bool(self.is_authenticated),
            "running": bool(self._running),
            "consecutive_failures": int(self._consecutive_failures or 0),
            "last_check": self._last_check,
            "last_tickle": self._last_check,
            "last_transition": self._last_transition,
            "status_code": int(self._last_status_code or 0),
            "gateway_running": bool(self.gateway_manager.is_running),
        }


class AuthController:
    def __init__(
        self,
        pb_client=None,
        gateway_manager: Optional[GatewayServiceManager] = None,
        broker: Optional[BrokerAdapter] = None,
        session_keeper: Optional[SocketSessionKeeper] = None,
        environment: str = DEFAULT_ENVIRONMENT,
        login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
        login_poll_interval_seconds: int = DEFAULT_LOGIN_POLL_INTERVAL_SECONDS,
    ):
        self.pb_client = pb_client
        self.gateway_manager = gateway_manager
        self.broker = broker
        self.session_keeper = session_keeper
        self.environment = str(environment or DEFAULT_ENVIRONMENT)
        self.login_timeout_seconds = max(30, int(login_timeout_seconds or DEFAULT_LOGIN_TIMEOUT_SECONDS))
        self.login_poll_interval_seconds = max(1, int(login_poll_interval_seconds or DEFAULT_LOGIN_POLL_INTERVAL_SECONDS))
        self._cancelled = False

    def request_2fa_approval(
        self,
        *,
        reason: str,
        source: str,
        detail: Optional[dict] = None,
        message: str = "",
        force_reset: bool = True,
    ) -> bool:
        if not self.pb_client:
            return False
        try:
            self.pb_client.request_ibkr_2fa(
                reason=reason,
                detail=detail or {},
                source=source,
                environment=self.environment,
                message=message,
                force_reset=force_reset,
            )
            self.pb_client.report_ibkr_2fa_result(
                "pending",
                detail=detail or {},
                source=source,
                environment=self.environment,
                message=message,
                last_result="waiting_manual_approval",
            )
            return True
        except Exception as exc:
            logger.warning("2FA approval request failed: %s", exc)
            return False

    def _report_2fa_status(
        self,
        status: str,
        detail: Optional[dict] = None,
        *,
        reason: str = "",
        source: str = "ibkr_compute",
        message: str = "",
        last_result: str = "",
        error: str = "",
        state_patch: Optional[dict] = None,
    ):
        if not self.pb_client:
            return {}
        try:
            return self.pb_client.report_ibkr_2fa_result(
                status,
                detail=detail or {},
                source=source,
                environment=self.environment,
                message=message,
                last_result=last_result,
                error=error,
                state_patch=state_patch or ({
                    "reason": reason,
                } if reason else {}),
            )
        except Exception:
            logger.exception("2FA status reporting failed")
            return {}

    def login(
        self,
        *,
        reason: str,
        source: str,
        detail: Optional[dict] = None,
    ) -> bool:
        detail = detail or {}
        self._cancelled = False

        if self.gateway_manager and not self.gateway_manager.is_running:
            if not self.gateway_manager.start():
                self._report_2fa_status(
                    "failed",
                    detail,
                    reason=reason,
                    source=source,
                    message="IB Gateway 未能启动，当前 2FA 轮次无法开始。",
                    last_result="gateway_start_failed",
                    error="gateway_start_failed",
                )
                return False

        approval_requested = self.request_2fa_approval(
            reason=reason,
            source=source,
            detail=detail,
            message="IB Gateway 已启动，请在手机上确认当前 2FA。",
            force_reset=False,
        )
        if self.pb_client:
            self._report_2fa_status(
                "pending",
                detail,
                reason=reason,
                source=source,
                message="等待手机确认 IBKR 2FA。",
                last_result="waiting_mobile_approval",
            )

        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self._cancelled:
                self._report_2fa_status(
                    "failed",
                    detail,
                    reason=reason,
                    source=source,
                    message="登录轮次已取消。",
                    last_result="cancelled",
                    error="cancelled",
                )
                return False

            health: dict[str, Any] = {}
            try:
                if self.session_keeper:
                    health = self.session_keeper.check_auth_status()
                    if bool(health.get("authenticated")):
                        self._report_2fa_status(
                            "success",
                            detail,
                            reason=reason,
                            source=source,
                            message="IB Gateway 已完成认证。",
                            last_result="authenticated",
                        )
                        return True
                elif self.broker:
                    health = self.broker.health()
                    if bool(health.get("ready")):
                        self._report_2fa_status(
                            "success",
                            detail,
                            reason=reason,
                            source=source,
                            message="IB Gateway 已完成认证。",
                            last_result="authenticated",
                        )
                        return True
            except Exception as exc:
                logger.debug("IB Gateway auth poll failed: %s", exc)

            time.sleep(self.login_poll_interval_seconds)

        self._report_2fa_status(
            "failed",
            detail,
            reason=reason,
            source=source,
            message=(
                "等待手机确认 2FA 超时，请重新触发当前轮次。"
                if approval_requested else
                "2FA 请求未成功送达且等待认证超时，请检查 Gateway / IBC 配置后重试。"
            ),
            last_result="login_timeout",
            error="login_timeout",
        )
        return False

    def cancel(self):
        self._cancelled = True

    def reset_cancel(self):
        self._cancelled = False

    def status(self) -> dict:
        return {
            "cancelled": bool(self._cancelled),
            "environment": self.environment,
            "gateway_service": self.gateway_manager.service_name if self.gateway_manager else "",
        }
