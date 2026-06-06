#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_API_BASE_URL = (
    os.environ.get("IBKR_API_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("CONSOLE_BASE_URL")
    or "https://quant.lzw-glory.top"
)
DEFAULT_HOST = os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.246")
DEFAULT_DB_PATH = os.environ.get("PB_DB_PATH", "/opt/pocketbase/pb_data/data.db")
DEFAULT_SQLITE_SCRIPT = Path(__file__).resolve().parents[1] / "db" / "remote_pb_sqlite.sh"
CONFIRM_TEXT = "PAPER_FEE_PROBE"
DEFAULT_ACCOUNT_SNAPSHOT_PATH = "/api/custom/ibkr/account_snapshot"
DEFAULT_RUNTIME_ACCOUNT_SNAPSHOT_PATH = "/ibkr/account"

SEC_TRANSACTION_FEE_RATE = 0.0000206
FINRA_TAF_RATE = 0.000195
CAT_FEE_RATE = 0.000003
NSCC_CLEARING_RATE = 0.00020
NYSE_PASS_THROUGH_RATE = 0.000175
FINRA_PASS_THROUGH_RATE = 0.00056
FIXED_PER_SHARE_RATE = 0.005
FIXED_MINIMUM = 1.00
TIERED_PER_SHARE_RATE = 0.0035
TIERED_MINIMUM = 0.35
MAX_ORDER_VALUE_RATE = 0.01

TERMINAL_ORDER_STATUSES = {
    "API_CANCELLED",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
    "FILLED",
    "INACTIVE",
    "REJECTED",
}


class ProbeError(RuntimeError):
    pass


class HardRequestTimeout(TimeoutError):
    pass


def _run_with_hard_timeout(fn, timeout_s: float):
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return fn()
    timeout = max(1.0, float(timeout_s or 0.0))
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)

    def _raise_timeout(_signum, _frame):
        raise HardRequestTimeout(f"hard_timeout_s={timeout:g}")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer and previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


@dataclass(frozen=True)
class FeeEstimate:
    actual_total: float
    fixed_like_estimate: float
    tiered_min_estimate: float
    classification: str
    tolerance: float


def normalize_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/") or DEFAULT_API_BASE_URL


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return float(number)


def to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def normalize_symbol(value: Any) -> str:
    return to_text(value).upper()


def normalize_side(value: Any) -> str:
    text = to_text(value).strip().upper()
    if text in {"BUY", "BOT", "B"}:
        return "buy"
    if text in {"SELL", "SLD", "S"}:
        return "sell"
    lowered = text.lower()
    if lowered in {"buy", "sell"}:
        return lowered
    return lowered


def is_sell_side(value: Any) -> bool:
    return normalize_side(value) == "sell"


def clamp_order_commission(base_commission: float, trade_value: float) -> float:
    maximum = abs(float(trade_value or 0.0)) * MAX_ORDER_VALUE_RATE
    if maximum > 0:
        return min(float(base_commission), maximum)
    return float(base_commission)


def regulatory_fees(side: Any, shares: float, trade_value: float) -> float:
    qty = abs(float(shares or 0.0))
    value = abs(float(trade_value or 0.0))
    fee = qty * CAT_FEE_RATE
    if is_sell_side(side):
        fee += value * SEC_TRANSACTION_FEE_RATE
        fee += qty * FINRA_TAF_RATE
    return fee


def fixed_like_commission(fill: dict[str, Any]) -> float:
    shares = abs(to_float(fill.get("shares"), 0.0))
    price = abs(to_float(fill.get("price"), 0.0))
    trade_value = abs(to_float(fill.get("trade_value"), shares * price))
    base = max(FIXED_MINIMUM, shares * FIXED_PER_SHARE_RATE)
    return clamp_order_commission(base, trade_value) + regulatory_fees(fill.get("side"), shares, trade_value)


def tiered_min_commission(fill: dict[str, Any]) -> float:
    shares = abs(to_float(fill.get("shares"), 0.0))
    price = abs(to_float(fill.get("price"), 0.0))
    trade_value = abs(to_float(fill.get("trade_value"), shares * price))
    base = max(TIERED_MINIMUM, shares * TIERED_PER_SHARE_RATE)
    base = clamp_order_commission(base, trade_value)
    pass_through = base * (NYSE_PASS_THROUGH_RATE + FINRA_PASS_THROUGH_RATE)
    clearing = shares * NSCC_CLEARING_RATE
    return base + regulatory_fees(fill.get("side"), shares, trade_value) + clearing + pass_through


def fee_tolerance(estimate: float) -> float:
    return max(0.02, abs(float(estimate or 0.0)) * 0.02)


def classify_fees(fills: list[dict[str, Any]]) -> FeeEstimate:
    actual = sum(abs(to_float(fill.get("commission"), 0.0)) for fill in fills)
    fixed = sum(fixed_like_commission(fill) for fill in fills)
    tiered = sum(tiered_min_commission(fill) for fill in fills)
    fixed_diff = abs(actual - fixed)
    tiered_diff = abs(actual - tiered)
    fixed_tol = fee_tolerance(fixed)
    tiered_tol = fee_tolerance(tiered)
    if actual <= 0:
        classification = "unknown"
        tolerance = max(fixed_tol, tiered_tol)
    elif fixed_diff <= fixed_tol and fixed_diff <= tiered_diff:
        classification = "fixed_like"
        tolerance = fixed_tol
    elif tiered_diff <= tiered_tol and tiered_diff < fixed_diff:
        classification = "tiered_like"
        tolerance = tiered_tol
    else:
        classification = "unknown"
        tolerance = max(fixed_tol, tiered_tol)
    return FeeEstimate(
        actual_total=round(actual, 6),
        fixed_like_estimate=round(fixed, 6),
        tiered_min_estimate=round(tiered, 6),
        classification=classification,
        tolerance=round(tolerance, 6),
    )


def build_bracket_prices(reference_price: float, direction: str, gap: float = 0.20) -> tuple[float, float]:
    price = float(reference_price or 0.0)
    if price <= 0:
        raise ProbeError("reference_price_required")
    normalized_direction = to_text(direction).lower()
    if normalized_direction == "long":
        take_profit = price * (1.0 + gap)
        stop_loss = price * (1.0 - gap)
    elif normalized_direction == "short":
        take_profit = price * (1.0 - gap)
        stop_loss = price * (1.0 + gap)
    else:
        raise ProbeError("direction must be long or short")
    return max(0.01, round(take_profit, 2)), max(0.01, round(stop_loss, 2))


def order_status(order: dict[str, Any]) -> str:
    raw = (
        order.get("status_key")
        or order.get("status")
        or order.get("order_status")
        or order.get("orderStatus")
        or ""
    )
    return to_text(raw).upper()


def order_symbol(order: dict[str, Any]) -> str:
    raw = order.get("symbol") or order.get("ticker") or order.get("contractDesc") or ""
    if not raw and isinstance(order.get("raw"), dict):
        raw = order["raw"].get("symbol") or order["raw"].get("ticker") or ""
    return normalize_symbol(raw)


def order_id(order: dict[str, Any]) -> str:
    raw = order.get("order_id") or order.get("orderId") or order.get("id") or ""
    if not raw and isinstance(order.get("raw"), dict):
        raw = order["raw"].get("order_id") or order["raw"].get("orderId") or order["raw"].get("id") or ""
    return to_text(raw)


def is_open_order(order: dict[str, Any]) -> bool:
    if order.get("is_open") is False:
        return False
    status = order_status(order)
    if status and status in TERMINAL_ORDER_STATUSES:
        return False
    return True


def open_orders_for_symbol(snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    normalized_symbol = normalize_symbol(symbol)
    seen: set[str] = set()
    matches: list[dict[str, Any]] = []
    for key in ("live_open_orders", "orders"):
        for raw in snapshot.get(key) or []:
            if not isinstance(raw, dict):
                continue
            if order_symbol(raw) != normalized_symbol:
                continue
            if not is_open_order(raw):
                continue
            identity = order_id(raw) or compact_json(raw)[:200]
            if identity in seen:
                continue
            seen.add(identity)
            matches.append(dict(raw))
    return matches


def positions_for_symbol(snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    normalized_symbol = normalize_symbol(symbol)
    return [
        dict(position)
        for position in snapshot.get("positions") or []
        if isinstance(position, dict) and normalize_symbol(position.get("symbol")) == normalized_symbol
    ]


def net_position_quantity(snapshot: dict[str, Any], symbol: str) -> float:
    return sum(to_float(position.get("quantity"), 0.0) for position in positions_for_symbol(snapshot, symbol))


def has_nonzero_position(snapshot: dict[str, Any], symbol: str) -> bool:
    return any(abs(to_float(position.get("quantity"), 0.0)) > 1e-9 for position in positions_for_symbol(snapshot, symbol))


def is_flat_for_symbol(snapshot: dict[str, Any], symbol: str) -> bool:
    return not has_nonzero_position(snapshot, symbol) and not open_orders_for_symbol(snapshot, symbol)


def assert_paper_snapshot(snapshot: dict[str, Any]) -> None:
    if snapshot.get("ok") is False:
        raise ProbeError(f"account_snapshot_not_ok:{to_text(snapshot.get('error')) or compact_json(snapshot)[:300]}")
    environment = to_text(snapshot.get("environment") or snapshot.get("broker_mode")).lower()
    broker_mode = to_text(snapshot.get("broker_mode") or snapshot.get("environment")).lower()
    if environment != "paper" and broker_mode != "paper":
        raise ProbeError(f"refusing_non_paper_environment:environment={environment or '-'} broker_mode={broker_mode or '-'}")
    if snapshot.get("service_running") is False:
        raise ProbeError("ibkr_service_not_running")
    if snapshot.get("session_authenticated") is False:
        raise ProbeError("ibkr_session_not_authenticated")


def wait_for(
    label: str,
    fn: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout_s: float,
    interval_s: float,
) -> Any:
    deadline = time.time() + max(1.0, float(timeout_s))
    last_value: Any = None
    while time.time() <= deadline:
        last_value = fn()
        if predicate(last_value):
            return last_value
        time.sleep(max(0.25, float(interval_s)))
    raise ProbeError(f"timeout_waiting_for:{label}:last={compact_json(last_value)[:1000]}")


class ApiClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = float(timeout or 30.0)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/{to_text(path).strip('/')}"
        query = {
            str(key): value
            for key, value in (params or {}).items()
            if value is not None and str(value) != ""
        }
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, None, params=params)

    def post(self, path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, payload, params=params)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "ibkr-paper-commission-probe/1.0",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self._url(path, params)
        request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
        status_code = 0
        raw = b""
        try:
            def _open_and_read():
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return int(getattr(response, "status", 200) or 200), response.read()

            status_code, raw = _run_with_hard_timeout(_open_and_read, self.timeout + 2.0)
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code or 0)
            raw = exc.read()
        except HardRequestTimeout as exc:
            raise ProbeError(f"http_request_hard_timeout:{url}:{exc}") from exc
        except urllib.error.URLError as exc:
            raise ProbeError(f"http_request_failed:{url}:{exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"ok": False, "error": "non_json_response", "raw": text[:1000]}
        if not isinstance(data, dict):
            data = {"ok": False, "error": "non_object_json_response", "raw": data}
        data["_http_status"] = status_code
        data["_request_url"] = url
        if status_code >= 400 and data.get("ok") is not False:
            data["ok"] = False
            data.setdefault("error", f"http_status_{status_code}")
        return data


def sqlite_literal(value: Any) -> str:
    return "'" + str(value if value is not None else "").replace("'", "''") + "'"


def run_remote_sql(args: argparse.Namespace, sql: str) -> list[dict[str, Any]]:
    script = Path(args.sqlite_script)
    command = [
        "bash",
        str(script),
        "--host",
        str(args.host),
        "--db",
        str(args.db_path),
        "--format",
        "json",
        "--sql",
        sql,
    ]
    proc = subprocess.run(command, text=True, capture_output=True, timeout=float(args.sqlite_timeout_sec))
    if proc.returncode != 0:
        raise ProbeError(f"remote_sql_failed:{proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"remote_sql_non_json:{proc.stdout[:500]}") from exc
    if not isinstance(payload, list):
        raise ProbeError(f"remote_sql_unexpected_payload:{compact_json(payload)[:300]}")
    return [dict(row) for row in payload if isinstance(row, dict)]


def fetch_latest_reference_price(args: argparse.Namespace, symbol: str) -> float:
    if args.reference_price and float(args.reference_price) > 0:
        return float(args.reference_price)
    safe_symbol = sqlite_literal(normalize_symbol(symbol))
    rows = run_remote_sql(
        args,
        f"""
        select close, environment, interval, us_time, bar_time_ms
        from ibkr_bars
        where symbol = {safe_symbol}
          and close > 0
          and environment in ('live', 'paper')
        order by bar_time_ms desc
        limit 1;
        """,
    )
    if not rows:
        raise ProbeError(f"reference_price_unavailable:{symbol}")
    price = to_float(rows[0].get("close"), 0.0)
    if price <= 0:
        raise ProbeError(f"reference_price_invalid:{compact_json(rows[0])}")
    return price


def fetch_execution_fills(args: argparse.Namespace, symbol: str, started_ms: int, order_ids: list[str]) -> list[dict[str, Any]]:
    safe_symbol = sqlite_literal(normalize_symbol(symbol))
    safe_started = int(max(0, started_ms - 60_000))
    clauses = [f"(environment = 'paper' and symbol = {safe_symbol} and trade_time_ms >= {safe_started})"]
    cleaned_order_ids = [to_text(value) for value in order_ids if to_text(value)]
    if cleaned_order_ids:
        joined = ", ".join(sqlite_literal(value) for value in cleaned_order_ids)
        clauses.insert(0, f"(environment = 'paper' and order_id in ({joined}))")
    where = " or ".join(clauses)
    return run_remote_sql(
        args,
        f"""
        select environment, account, symbol, order_id, exec_id, side, shares, price,
               trade_value, commission, commission_currency, commission_known,
               realized_pnl, realized_pnl_known, exchange, source, trade_time, trade_time_ms
        from ibkr_execution_fills
        where {where}
        order by trade_time_ms asc, created asc, order_id asc, exec_id asc;
        """,
    )


def account_params() -> dict[str, Any]:
    return {
        "environment": "paper",
        "broker_mode": "paper",
        "cache_bust": int(time.time() * 1000),
    }


def action_params() -> dict[str, Any]:
    return {
        "environment": "paper",
        "broker_mode": "paper",
    }


def account_snapshot_path(args: argparse.Namespace) -> str:
    explicit_path = to_text(getattr(args, "account_snapshot_path", ""))
    if explicit_path:
        return explicit_path
    if to_text(getattr(args, "account_base_url", "")):
        return DEFAULT_RUNTIME_ACCOUNT_SNAPSHOT_PATH
    return DEFAULT_ACCOUNT_SNAPSHOT_PATH


def get_account_snapshot(client: ApiClient, path: str = DEFAULT_ACCOUNT_SNAPSHOT_PATH) -> dict[str, Any]:
    return client.get(path or DEFAULT_ACCOUNT_SNAPSHOT_PATH, account_params())


def check_clean_preflight(snapshot: dict[str, Any], symbol: str) -> None:
    assert_paper_snapshot(snapshot)
    positions = [item for item in positions_for_symbol(snapshot, symbol) if abs(to_float(item.get("quantity"), 0.0)) > 1e-9]
    if positions:
        raise ProbeError(f"pre_existing_position:{compact_json(positions)[:500]}")
    orders = open_orders_for_symbol(snapshot, symbol)
    if orders:
        raise ProbeError(f"pre_existing_open_order:{compact_json(orders)[:500]}")


def extract_order_ids(*payloads: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for payload in payloads:
        candidates: list[Any] = []
        if isinstance(payload, dict):
            candidates.extend(payload.get("order_ids") or [])
            candidates.extend(payload.get("submitted_order_ids") or [])
            candidates.extend(payload.get("broker_order_ids") or [])
            result = payload.get("result")
            if isinstance(result, dict):
                candidates.extend(result.get("order_ids") or [])
                candidates.extend(result.get("submitted_order_ids") or [])
                candidates.extend(result.get("broker_order_ids") or [])
        for item in candidates:
            text = to_text(item)
            if text and text not in ids:
                ids.append(text)
    return ids


def close_payload_from_snapshot(
    snapshot: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    quantity: int,
    place_result: dict[str, Any],
) -> dict[str, Any]:
    positions = positions_for_symbol(snapshot, symbol)
    position = next((item for item in positions if abs(to_float(item.get("quantity"), 0.0)) > 1e-9), {})
    result = place_result.get("result") if isinstance(place_result.get("result"), dict) else place_result
    payload: dict[str, Any] = {
        "symbol": normalize_symbol(symbol),
        "quantity": abs(to_float(position.get("quantity"), quantity)) or quantity,
        "direction": direction,
        "cancel_bracket_after_close": True,
        "source": "paper_commission_probe",
        "close_reason": "paper_commission_probe_cleanup",
        "close_reason_human": "Paper 手续费验证自动平仓",
    }
    for key in ("conid", "account", "currency", "asset_class", "avg_cost", "avg_price", "market_price", "market_value"):
        if position.get(key) not in (None, ""):
            payload[key] = position.get(key)
    trade_group_id = to_text(result.get("trade_group_id") or result.get("bracket_group"))
    entry_order_unique_id = to_text(result.get("entry_coid") or result.get("entry_order_unique_id"))
    signal_id = to_text(place_result.get("signal_id") or result.get("signal_id"))
    if trade_group_id:
        payload["trade_group_id"] = trade_group_id
    if entry_order_unique_id:
        payload["entry_order_unique_id"] = entry_order_unique_id
    if signal_id:
        payload["signal_id"] = signal_id
    return payload


def cancel_residual_orders(client: ApiClient, snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for order in open_orders_for_symbol(snapshot, symbol):
        oid = order_id(order)
        if not oid:
            continue
        results.append(client.post("/api/custom/ibkr/orders/cancel", {"order_id": oid}, action_params()))
    return results


def cleanup_symbol(
    client: ApiClient,
    symbol: str,
    direction: str,
    quantity: int,
    place_result: dict[str, Any],
    *,
    snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    timeout_s: float = 60.0,
    interval_s: float = 2.0,
) -> dict[str, Any]:
    cleanup: dict[str, Any] = {"attempted": True, "close": {}, "cancel_results": []}
    load_snapshot = snapshot_fn or (lambda: get_account_snapshot(client))
    snapshot = load_snapshot()
    if has_nonzero_position(snapshot, symbol):
        close_payload = close_payload_from_snapshot(
            snapshot,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            place_result=place_result,
        )
        cleanup["close_payload"] = close_payload
        cleanup["close"] = client.post("/api/custom/ibkr/positions/close", close_payload, action_params())
        time.sleep(1.0)
        snapshot = load_snapshot()
    cleanup["cancel_results"] = cancel_residual_orders(client, snapshot, symbol)
    try:
        cleanup["final_snapshot"] = wait_for(
            f"{symbol}_flat_cleanup",
            load_snapshot,
            lambda value: is_flat_for_symbol(value, symbol),
            timeout_s=timeout_s,
            interval_s=interval_s,
        )
    except ProbeError:
        latest = load_snapshot()
        cleanup["cancel_results"].extend(cancel_residual_orders(client, latest, symbol))
        cleanup["final_snapshot"] = load_snapshot()
    cleanup["flat"] = is_flat_for_symbol(cleanup["final_snapshot"], symbol)
    return cleanup


def print_report(args: argparse.Namespace, fills: list[dict[str, Any]]) -> None:
    if not fills:
        print("No ibkr_execution_fills rows found for the probe window/order ids.")
        return
    print("\nExecution fills:")
    for fill in fills:
        print(
            "- "
            f"order={to_text(fill.get('order_id')) or '-'} "
            f"side={normalize_side(fill.get('side')) or '-'} "
            f"shares={to_float(fill.get('shares'), 0.0):g} "
            f"price={to_float(fill.get('price'), 0.0):.4f} "
            f"value={to_float(fill.get('trade_value'), 0.0):.4f} "
            f"exchange={to_text(fill.get('exchange')) or '-'} "
            f"commission={to_float(fill.get('commission'), 0.0):.6f} "
            f"known={to_text(fill.get('commission_known')) or '-'} "
            f"exec={to_text(fill.get('exec_id')) or '-'}"
        )
    estimate = classify_fees(fills)
    print("\nFee classification:")
    print(f"- actual_total: ${estimate.actual_total:.6f}")
    print(f"- fixed_like_estimate: ${estimate.fixed_like_estimate:.6f}")
    print(f"- tiered_min_estimate: ${estimate.tiered_min_estimate:.6f}")
    print(f"- tolerance: ${estimate.tolerance:.6f}")
    print(f"- classification: {estimate.classification}")


def run_probe(args: argparse.Namespace) -> int:
    symbol = normalize_symbol(args.symbol)
    direction = to_text(args.direction).lower()
    quantity = int(args.quantity)
    client = ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    snapshot_base_url = to_text(args.account_base_url) or args.api_base_url
    snapshot_path = account_snapshot_path(args)
    snapshot_client = ApiClient(snapshot_base_url, timeout=float(args.http_timeout_sec))
    snapshot_fn = lambda: get_account_snapshot(snapshot_client, snapshot_path)
    started_ms = int(time.time() * 1000)

    print(f"Paper commission probe: symbol={symbol} qty={quantity} direction={direction}")
    if snapshot_base_url != args.api_base_url or snapshot_path != DEFAULT_ACCOUNT_SNAPSHOT_PATH:
        print(f"Account snapshot source: {normalize_base_url(snapshot_base_url)}{snapshot_path}")
    snapshot = snapshot_fn()
    check_clean_preflight(snapshot, symbol)
    reference_price = fetch_latest_reference_price(args, symbol)
    take_profit, stop_loss = build_bracket_prices(reference_price, direction)
    print(f"Preflight OK: paper account, no {symbol} exposure/open orders")
    print(f"Reference price: {reference_price:.4f}; TP={take_profit:.2f}; SL={stop_loss:.2f}")

    if not args.execute:
        print(f"Dry run only. Add --execute --confirm {CONFIRM_TEXT} to submit a paper order.")
        return 0
    if args.confirm != CONFIRM_TEXT:
        raise ProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")

    place_payload = {
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "order_type": "MKT",
        "take_profit_price": take_profit,
        "stop_loss_price": stop_loss,
    }
    print("Submitting paper bracket entry...")
    place_response = client.post("/api/custom/ibkr/orders/place", place_payload, action_params())
    if not place_response.get("ok"):
        raise ProbeError(f"place_order_failed:{compact_json(place_response)[:1000]}")
    place_result = place_response.get("result") if isinstance(place_response.get("result"), dict) else {}
    order_ids = extract_order_ids(place_response)
    print(f"Submitted order ids: {', '.join(order_ids) or '-'}")

    entry_error: ProbeError | None = None
    try:
        filled_snapshot = wait_for(
            f"{symbol}_entry_position",
            snapshot_fn,
            lambda value: has_nonzero_position(value, symbol),
            timeout_s=float(args.timeout_sec),
            interval_s=float(args.poll_interval_sec),
        )
        print(f"Entry exposure detected: quantity={net_position_quantity(filled_snapshot, symbol):g}")
    except ProbeError as exc:
        entry_error = exc
    finally:
        cleanup = cleanup_symbol(
            client,
            symbol,
            direction,
            quantity,
            place_response,
            snapshot_fn=snapshot_fn,
            timeout_s=float(args.timeout_sec),
            interval_s=float(args.poll_interval_sec),
        )

    close_ids = extract_order_ids(cleanup.get("close") if isinstance(cleanup.get("close"), dict) else {})
    order_ids = list(dict.fromkeys([*order_ids, *close_ids]))
    final_snapshot = cleanup.get("final_snapshot") if isinstance(cleanup.get("final_snapshot"), dict) else snapshot_fn()
    if not is_flat_for_symbol(final_snapshot, symbol):
        raise ProbeError(f"cleanup_failed_not_flat:{compact_json(final_snapshot)[:1500]}")
    if entry_error is not None:
        raise ProbeError(f"entry_fill_unconfirmed_after_cleanup:{entry_error}")
    print("Cleanup OK: final state is FLAT with no symbol open orders")

    fills = fetch_execution_fills(args, symbol, started_ms, order_ids)
    print_report(args, fills)
    if not fills:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Place and clean up a tiny paper order to classify IBKR paper commissions.")
    parser.add_argument("--api-base-url", "--runtime-url", dest="api_base_url", default=DEFAULT_API_BASE_URL)
    parser.add_argument(
        "--account-base-url",
        default=os.environ.get("IBKR_ACCOUNT_BASE_URL") or os.environ.get("IBKR_RUNTIME_BASE_URL") or "",
        help="Optional direct runtime/compute base URL for account snapshots; defaults to --api-base-url.",
    )
    parser.add_argument(
        "--account-snapshot-path",
        default="",
        help="Override account snapshot path. Defaults to /ibkr/account when --account-base-url is set, otherwise /api/custom/ibkr/account_snapshot.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--sqlite-script", default=str(DEFAULT_SQLITE_SCRIPT))
    parser.add_argument("--symbol", default=os.environ.get("IBKR_PAPER_FEE_PROBE_SYMBOL", "TSLA"))
    parser.add_argument("--quantity", type=int, default=int(os.environ.get("IBKR_PAPER_FEE_PROBE_QTY", "1")))
    parser.add_argument("--direction", choices=("long", "short"), default=os.environ.get("IBKR_PAPER_FEE_PROBE_DIRECTION", "long"))
    parser.add_argument("--reference-price", type=float, default=0.0)
    parser.add_argument("--timeout-sec", type=float, default=60.0)
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    parser.add_argument("--sqlite-timeout-sec", type=float, default=45.0)
    parser.add_argument("--execute", action="store_true", help="Submit the paper order. Without this flag the script only runs preflight.")
    parser.add_argument("--confirm", default="", help=f"Required with --execute: {CONFIRM_TEXT}")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if int(args.quantity or 0) <= 0:
        raise ProbeError("quantity must be a positive integer")
    if args.execute and args.confirm != CONFIRM_TEXT:
        raise ProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")
    if not Path(args.sqlite_script).is_file():
        raise ProbeError(f"sqlite_script_missing:{args.sqlite_script}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        return run_probe(args)
    except ProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
