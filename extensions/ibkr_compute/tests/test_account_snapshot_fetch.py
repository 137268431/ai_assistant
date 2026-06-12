import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.snapshot_builder.fetch import fetch_snapshot_sources
from ibkr_compute.api.account.snapshot_builder.context import _snapshot_cache_stale_seconds
from ibkr_compute.api.account.snapshot_builder.payload import (
    _build_ibkr_account_buying_power_snapshot,
    _build_ibkr_account_snapshot,
    refresh_account_snapshot_cache,
)
from ibkr_compute.api.account.history_builder import _build_ibkr_order_history
from ibkr_compute.orchestration.account_snapshot_refresh import TradingServiceAccountSnapshotRefreshMixin


class _Lifecycle:
    def __init__(self):
        self.pnl_calls = 0
        self.positions_calls = 0

    def get_account_snapshot(self, _account_id):
        return {
            "summary": {"NetLiquidation": {"value": "1000000", "currency": "USD"}},
            "positions": [],
        }

    def get_account_pnl(self, _account_id):
        self.pnl_calls += 1
        return {"ok": True, "daily_pnl": 12.34, "source": "reqPnL"}

    def get_positions(self, _account_id):
        self.positions_calls += 1
        raise AssertionError("empty account snapshot positions should not fall back")


class _Service:
    def __init__(self):
        self.order_lifecycle = _Lifecycle()


class _FakeLogger:
    def debug(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


class _FakePB:
    def get_records(self, *args, **kwargs):
        return []


class _RowsPB:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows or [])
        self.calls = []

    def get_records(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return list(self.rows)


class _FakeApiApp:
    IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS = 30.0
    IBKR_ACCOUNT_SNAPSHOT_STALE_SECONDS = 120.0

    def __init__(self):
        import threading

        self.pb = _FakePB()
        self.logger = _FakeLogger()
        self.ibkr_account_snapshot_cache = {}
        self.ibkr_account_snapshot_cache_lock = threading.Lock()
        self.ibkr_account_snapshot_refresh_locks = {}

    def _ibkr_service_uses_paper_account(self, _service):
        return True

    def _ibkr_service_environment(self, _service):
        return "paper"

    def _coerce_float(self, value, default=None):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def current_market_date(self):
        return "2026-06-09"


class _SnapshotOrderPlacer:
    environment = "paper"

    def get_active_account_id(self, use_paper=False):
        return "DU123"


class _SnapshotLifecycle:
    account_id = "DU123"
    environment = "paper"

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.snapshot_calls = 0
        self.pnl_calls = 0
        self.fail = False

    def get_account_snapshot(self, _account_id):
        self.snapshot_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise TimeoutError("account snapshot timeout")
        return {
            "summary": {"AccountCode": {"value": "DU123"}, "NetLiquidation": {"value": "1000000"}},
            "positions": [],
        }

    def get_account_pnl(self, _account_id):
        self.pnl_calls += 1
        return {"ok": True, "daily_pnl": 0.0}


class _SnapshotService:
    is_running = True
    is_starting = False
    config = None

    def __init__(self, lifecycle: _SnapshotLifecycle):
        self.order_placer = _SnapshotOrderPlacer()
        self.order_lifecycle = lifecycle


class _ReservationStore:
    def __init__(self, *, exposure: float = 0.0, count: int = 0):
        self.exposure = float(exposure or 0.0)
        self.count = int(count or 0)

    def snapshot(self):
        return {
            "exposure": self.exposure,
            "count": self.count,
            "order_ids": ["1001"] if self.count else [],
        }


class _FastOrderTracker:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.cached_calls = 0
        self.include_all_values: list[bool] = []

    def get_cached_live_orders(self, *, include_all: bool = False):
        self.cached_calls += 1
        self.include_all_values.append(bool(include_all))
        if include_all:
            return list(self.rows)
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}
        return [
            row
            for row in self.rows
            if str(row.get("status") or row.get("order_status") or row.get("orderStatus") or "").strip().upper()
            not in closed_statuses
        ]

    def get_live_orders(self):
        return list(self.rows)


class _DefaultConfig:
    def refresh(self):
        return None

    def get_for_environment(self, key, _environment, default=None):
        return default

    def get_bool_for_environment(self, _key, _environment, default=False):
        return default

    def get_float_for_environment(self, _key, _environment, default=0.0):
        return default


class _StatusComponent:
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.calls = 0

    def status(self):
        self.calls += 1
        return dict(self.payload)


class _RefreshService(TradingServiceAccountSnapshotRefreshMixin):
    _running = True
    config = _DefaultConfig()

    def __init__(self, *, circuit: dict | None = None):
        self.order_placer = _SnapshotOrderPlacer()
        self.order_lifecycle = _SnapshotLifecycle()
        self.order_tracker = _FastOrderTracker([])
        self.buying_power_reservations = _ReservationStore()
        payload = {
            "connected": True,
            "ready": True,
            "status_code": 200,
            "account_data_circuit": dict(circuit or {}),
        }
        self.broker = _StatusComponent(payload)
        self.gateway_manager = _StatusComponent({"running": True, "broker": payload})


class _BuyingPowerLifecycle:
    account_id = "DU123"

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.snapshot_calls = 0
        self.summary_calls = 0

    def get_account_snapshot(self, _account_id):
        self.snapshot_calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {
            "summary": {
                "AccountCode": {"value": "DU123"},
                "NetLiquidation": {"value": "100000", "currency": "USD"},
                "BuyingPower": {"value": "50000", "currency": "USD"},
                "AvailableFunds": {"value": "25000", "currency": "USD"},
            },
            "positions": [],
        }

    def get_account_summary(self, _account_id):
        self.summary_calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {
            "AccountCode": {"value": "DU123"},
            "NetLiquidation": {"value": "100000", "currency": "USD"},
            "BuyingPower": {"value": "50000", "currency": "USD"},
            "AvailableFunds": {"value": "25000", "currency": "USD"},
        }


class _BuyingPowerService(_SnapshotService):
    config = _DefaultConfig()

    def __init__(self, lifecycle: _BuyingPowerLifecycle, *, circuit: dict | None = None):
        super().__init__(lifecycle)
        self._circuit = dict(circuit or {})
        self.status_calls = 0
        self.gateway_manager = _StatusComponent(
            {
                "running": True,
                "reachable": True,
                "broker": {"account_data_circuit": self._circuit},
            }
        )
        self.broker = _StatusComponent(
            {
                "connected": True,
                "ready": True,
                "status_code": 200,
                "account_data_circuit": self._circuit,
            }
        )
        self.session_keeper = _StatusComponent({"authenticated": True})
        self.session_keeper.is_authenticated = True
        self.ws_client = _StatusComponent({"ready": True})
        self.ws_client._ready = True

    def status(self):
        self.status_calls += 1
        return {
            "gateway": {"running": True, "reachable": True},
            "session": {"authenticated": True},
            "account_data_circuit": self._circuit,
        }


def _with_fake_api_app(app, fn):
    with (
        mock.patch("ibkr_compute.api.account.history_builder.payload._api_app", return_value=app),
        mock.patch("ibkr_compute.api.account.history_builder.reconcile._api_app", return_value=app),
        mock.patch("ibkr_compute.api.account.snapshot_builder.context._api_app", return_value=app),
        mock.patch("ibkr_compute.api.account.live.runtime._api_app", return_value=app),
    ):
        return fn()


class AccountSnapshotFetchTest(unittest.TestCase):
    def test_include_pnl_false_skips_pnl_fetcher(self):
        service = _Service()

        payload = fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual(0, service.order_lifecycle.pnl_calls)
        self.assertEqual("", payload["errors"]["pnl"])
        self.assertEqual({}, payload["pnl_raw"])

    def test_empty_snapshot_positions_do_not_trigger_fallback(self):
        service = _Service()

        payload = fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual([], payload["positions_raw"])
        self.assertEqual(0, service.order_lifecycle.positions_calls)
        self.assertEqual("", payload["errors"]["positions"])

    def test_positions_fallback_can_be_enabled_explicitly(self):
        class _MissingPositionsLifecycle:
            environment = "paper"

            def __init__(self):
                self.positions_calls = 0

            def get_account_snapshot(self, _account_id):
                return {"summary": {"NetLiquidation": {"value": "1000000", "currency": "USD"}}}

            def get_positions(self, _account_id):
                self.positions_calls += 1
                return [{"symbol": "AAPL", "position": 5}]

        class _EnabledConfig(_DefaultConfig):
            def get_bool_for_environment(self, key, _environment, default=False):
                if key == "ibkr_account_snapshot_positions_fallback_enabled":
                    return True
                return default

        service = _Service()
        service.order_lifecycle = _MissingPositionsLifecycle()
        service.config = _EnabledConfig()

        payload = fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual([{"symbol": "AAPL", "position": 5}], payload["positions_raw"])
        self.assertEqual(1, service.order_lifecycle.positions_calls)
        self.assertEqual("", payload["errors"]["positions"])

    def test_account_snapshot_refresh_uses_orders_fast_when_account_data_circuit_open(self):
        service = _RefreshService(circuit={"active": True, "reason": "positions_timeout", "remaining_s": 42.0})

        needed, details = service._account_snapshot_refresh_orders_fast_needed()

        self.assertTrue(needed)
        self.assertTrue(details["account_data_guard_active"])
        self.assertTrue(details["account_data_circuit_active"])
        self.assertEqual("positions_timeout", details["account_data_circuit_reason"])
        self.assertEqual(42.0, details["account_data_circuit_remaining_s"])

    def test_account_snapshot_refresh_uses_orders_fast_when_lifecycle_backoff_active(self):
        service = _RefreshService()
        service.order_lifecycle._account_data_backoff_reason = "account_data_circuit_open:account_summary"
        service.order_lifecycle._account_data_backoff_remaining = lambda: 31.4

        needed, details = service._account_snapshot_refresh_orders_fast_needed()

        self.assertTrue(needed)
        self.assertTrue(details["account_data_guard_active"])
        self.assertEqual(31.4, details["account_lifecycle_backoff_remaining_s"])
        self.assertEqual("account_data_circuit_open:account_summary", details["account_lifecycle_backoff_reason"])

    def test_account_snapshot_refresh_uses_orders_fast_during_startup_warmup(self):
        service = _RefreshService()
        service._runtime_started_at = time.time()

        needed, details = service._account_snapshot_refresh_orders_fast_needed()

        self.assertTrue(needed)
        self.assertTrue(details["startup_warmup_active"])
        self.assertGreater(details["startup_warmup_remaining_s"], 0)

    def test_account_snapshot_refresh_uses_orders_fast_when_account_data_gate_busy(self):
        service = _RefreshService()
        gate = {"owner_kind": "positions", "owner_age_s": 12.5, "serial_timeout_s": 30.0}
        service.broker.payload["account_data_request_gate"] = gate

        needed, details = service._account_snapshot_refresh_orders_fast_needed()

        self.assertTrue(needed)
        self.assertTrue(details["account_data_guard_active"])
        self.assertTrue(details["account_data_gate_active"])
        self.assertEqual("positions", details["account_data_gate_owner_kind"])

    def test_order_history_defaults_to_cached_broker_and_pb_today_rows(self):
        class _HistoryTracker:
            def __init__(self):
                self.calls = []

            def get_broker_order_history(self, **kwargs):
                self.calls.append(dict(kwargs))
                return {
                    "ok": True,
                    "effective_days": 1,
                    "current_day_only": False,
                    "orders": [],
                    "raw": {},
                    "executions_requested": bool(kwargs.get("include_executions")),
                }

        class _HistoryService:
            is_running = True

            def __init__(self):
                self.order_tracker = _HistoryTracker()
                self.order_placer = _SnapshotOrderPlacer()
                self.pb = _RowsPB(
                    [
                        {
                            "id": "pb-order-1",
                            "broker_order_id": "1001",
                            "order_id": "1001",
                            "symbol": "AAPL",
                            "status": "Submitted",
                            "direction": "long",
                            "quantity": 5,
                            "limit_price": 123.45,
                            "role": "entry",
                            "environment": "paper",
                            "updated": "2026-06-09T10:00:00-04:00",
                        }
                    ]
                )

            def status(self):
                return {"session": {"authenticated": True}, "gateway": {"running": True}}

        app = _FakeApiApp()
        service = _HistoryService()

        payload = _with_fake_api_app(app, lambda: _build_ibkr_order_history(service, requested_days=1))

        self.assertTrue(payload["ok"])
        self.assertEqual("ibkr_cached_order_history", payload["source"])
        self.assertFalse(payload["broker_force"])
        self.assertFalse(payload["executions_requested"])
        self.assertEqual(
            [{"days": 1, "force": False, "include_executions": False}],
            service.order_tracker.calls,
        )
        self.assertEqual(1, payload["counts"]["total"])
        self.assertEqual("pb_cache", payload["items"][0]["source"])
        self.assertEqual("1001", payload["items"][0]["broker_order_id"])
        self.assertEqual("pb_cache_only", payload["items"][0]["diagnostic_state"])
        self.assertEqual(1, payload["reconciliation"]["pb_today_count"])

    def test_account_snapshot_preserves_already_normalized_positions(self):
        class _NormalizedPositionLifecycle(_SnapshotLifecycle):
            def get_account_snapshot(self, _account_id):
                self.snapshot_calls += 1
                return {
                    "summary": {"AccountCode": {"value": "DU123"}, "NetLiquidation": {"value": "1000000"}},
                    "positions": [
                        {
                            "symbol": "MSTR",
                            "conid": 272110,
                            "quantity": -39,
                            "direction": "short",
                            "avg_cost": 127.34,
                            "avg_price": 127.34,
                            "market_price": 126.2,
                            "market_value": -4921.8,
                            "unrealized_pnl": 44.52,
                            "realized_pnl": 0,
                            "account": "DU123",
                            "currency": "USD",
                            "asset_class": "STK",
                            "raw": {
                                "ticker": "MSTR",
                                "position": -39,
                                "mktPrice": 126.2,
                                "avgCost": 127.34,
                            },
                        }
                    ],
                }

        app = _FakeApiApp()
        service = _SnapshotService(_NormalizedPositionLifecycle())

        payload = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))

        self.assertEqual(1, payload["counts"]["positions"])
        self.assertEqual(1, payload["counts"]["open_positions"])
        self.assertEqual("MSTR", payload["positions"][0]["symbol"])
        self.assertEqual(-39.0, payload["positions"][0]["quantity"])
        self.assertEqual("short", payload["positions"][0]["direction"])

    def test_account_snapshot_empty_cache_single_flight(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle(delay=0.05)
        service = _SnapshotService(lifecycle)

        def run():
            with ThreadPoolExecutor(max_workers=3) as executor:
                return list(executor.map(lambda _idx: _build_ibkr_account_snapshot(service, include_pnl=False), range(3)))

        payloads = _with_fake_api_app(app, run)

        self.assertEqual(1, lifecycle.snapshot_calls)
        self.assertEqual(0, lifecycle.pnl_calls)
        self.assertTrue(all(payload["ok"] for payload in payloads))
        self.assertTrue(all(payload["cache_state"] == "fresh" for payload in payloads))
        self.assertTrue(all(payload["positions"] == [] for payload in payloads))

    def test_account_snapshot_returns_stale_cache_and_keeps_old_payload_on_refresh_error(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle()
        service = _SnapshotService(lifecycle)

        first = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        self.assertFalse(first["stale"])

        cache_key = ("paper", "DU123", False)
        with app.ibkr_account_snapshot_cache_lock:
            app.ibkr_account_snapshot_cache[cache_key]["fresh_until"] = time.time() - 1
            app.ibkr_account_snapshot_cache[cache_key]["stale_until"] = time.time() + 120

        stale = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        self.assertEqual("stale", stale["cache_state"])
        self.assertTrue(stale["stale"])
        self.assertEqual(1, lifecycle.snapshot_calls)

        lifecycle.fail = True
        fallback = _with_fake_api_app(app, lambda: refresh_account_snapshot_cache(service, include_pnl=False))
        self.assertEqual("stale_after_error", fallback["cache_state"])
        self.assertTrue(fallback["stale"])
        self.assertEqual("account snapshot timeout", fallback["refresh_error"])
        self.assertEqual([], fallback["positions"])
        self.assertEqual(2, lifecycle.snapshot_calls)

    def test_account_snapshot_force_refresh_can_refuse_stale_cache_on_refresh_error(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle()
        service = _SnapshotService(lifecycle)

        first = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        self.assertFalse(first["stale"])

        cache_key = ("paper", "DU123", False)
        with app.ibkr_account_snapshot_cache_lock:
            app.ibkr_account_snapshot_cache[cache_key]["fresh_until"] = time.time() - 1
            app.ibkr_account_snapshot_cache[cache_key]["stale_until"] = time.time() + 120

        lifecycle.fail = True
        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
            ),
        )

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["stale"])
        self.assertEqual("empty_error", payload["cache_state"])
        self.assertEqual("account snapshot timeout", payload["refresh_error"])
        self.assertEqual(2, lifecycle.snapshot_calls)

    def test_orders_fast_snapshot_uses_cached_orders_without_account_snapshot_fetch(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle(delay=0.05)
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker(
            [
                {
                    "orderId": "1001",
                    "ticker": "AAPL",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 10,
                    "price": 123.45,
                }
            ]
        )

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual("orders_fast", payload["snapshot_profile"])
        self.assertEqual("account_snapshot_orders_fast", payload["source"])
        self.assertEqual(0, lifecycle.snapshot_calls)
        self.assertEqual(1, service.order_tracker.cached_calls)
        self.assertEqual(1, payload["counts"]["open_orders"])
        self.assertEqual("1001", payload["live_open_orders"][0]["order_id"])
        self.assertEqual("orders_fast_summary_cache_unavailable", payload["errors"]["summary"])

    def test_orders_fast_snapshot_infers_strategy_position_from_filled_entry_and_open_protection(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle(delay=0.05)
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker(
            [
                {
                    "orderId": "11329",
                    "ticker": "STM",
                    "conid": 123456,
                    "acct": "DU123",
                    "status": "Filled",
                    "side": "SLD",
                    "orderType": "LMT",
                    "totalSize": 66,
                    "filledQuantity": 66,
                    "avgPrice": 75.10,
                    "price": 75.10,
                    "cOID": "entry_BATS_STM_short_20260611_134700",
                    "currency": "USD",
                    "secType": "STK",
                },
                {
                    "orderId": "11330",
                    "parentId": "11329",
                    "ticker": "STM",
                    "conid": 123456,
                    "acct": "DU123",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 66,
                    "price": 70.25,
                    "cOID": "tp_BATS_STM_short_20260611_134700",
                    "currency": "USD",
                    "secType": "STK",
                },
                {
                    "orderId": "11331",
                    "parentId": "11329",
                    "ticker": "STM",
                    "conid": 123456,
                    "acct": "DU123",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "STP",
                    "totalSize": 66,
                    "auxPrice": 77.90,
                    "cOID": "sl_BATS_STM_short_20260611_134700",
                    "currency": "USD",
                    "secType": "STK",
                },
            ]
        )

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(0, lifecycle.snapshot_calls)
        self.assertEqual(1, len(payload["inferred_strategy_positions"]))
        inferred = payload["inferred_strategy_positions"][0]
        self.assertEqual("STM", inferred["symbol"])
        self.assertEqual("short", inferred["direction"])
        self.assertEqual(-66.0, inferred["quantity"])
        self.assertEqual(75.10, inferred["avg_price"])
        self.assertEqual("complete", inferred["protection_status"])
        self.assertEqual("11330", inferred["take_profit_order_id"])
        self.assertEqual(70.25, inferred["take_profit_price"])
        self.assertEqual("11331", inferred["stop_loss_order_id"])
        self.assertEqual(77.90, inferred["stop_loss_price"])
        self.assertEqual(1, payload["counts"]["inferred_strategy_positions"])
        self.assertEqual(1, payload["counts"]["effective_open_positions"])
        self.assertTrue(payload["positions_detail_available"])
        self.assertTrue(payload["positions_count_available"])
        self.assertTrue(payload["positions_inference"]["display_fallback"])

    def test_orders_fast_snapshot_infers_position_from_open_protection_pair_after_restart(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle(delay=0.05)
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker(
            [
                {
                    "orderId": "11333",
                    "parentId": "11332",
                    "ticker": "WDC",
                    "acct": "DU123",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 9,
                    "price": 484.85,
                    "cOID": "tp_BATS_WDC_short_20260611_0954_2_mr_sdUpper",
                    "currency": "USD",
                    "secType": "STK",
                },
                {
                    "orderId": "11334",
                    "parentId": "11332",
                    "ticker": "WDC",
                    "acct": "DU123",
                    "status": "PreSubmitted",
                    "side": "BUY",
                    "orderType": "STP",
                    "totalSize": 9,
                    "auxPrice": 522.44,
                    "cOID": "sl_BATS_WDC_short_20260611_0954_2_mr_sdUpper",
                    "currency": "USD",
                    "secType": "STK",
                },
            ]
        )

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(payload["inferred_strategy_positions"]))
        inferred = payload["inferred_strategy_positions"][0]
        self.assertEqual("WDC", inferred["symbol"])
        self.assertEqual("short", inferred["direction"])
        self.assertEqual(-9.0, inferred["quantity"])
        self.assertEqual("medium", inferred["inference_confidence"])
        self.assertEqual("complete", inferred["protection_status"])
        self.assertEqual("11332", inferred["entry_order_id"])
        self.assertEqual("open_protection_orders_without_filled_entry", inferred["relation"]["reason"])
        self.assertEqual(1, payload["counts"]["effective_open_positions"])

    def test_orders_fast_open_only_omits_closed_history_rows(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle(delay=0.05)
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker(
            [
                {
                    "orderId": "1001",
                    "ticker": "AAPL",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 10,
                    "price": 123.45,
                },
                {
                    "orderId": "1002",
                    "ticker": "MSFT",
                    "status": "Filled",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 2,
                    "price": 250.00,
                },
            ]
        )

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual([False], service.order_tracker.include_all_values)
        self.assertTrue(payload["orders_fast_open_orders_only"])
        self.assertTrue(payload["orders_fast_diagnostics"]["historical_orders_omitted"])
        self.assertEqual(["1001"], [order["order_id"] for order in payload["orders"]])
        self.assertEqual(["1001"], [order["order_id"] for order in payload["live_open_orders"]])
        self.assertEqual(1, payload["counts"]["orders"])
        self.assertEqual(1, payload["counts"]["open_orders"])

    def test_orders_fast_snapshot_skips_full_runtime_status(self):
        class _SlowStatusService(_SnapshotService):
            status_calls = 0

            def status(self, *args, **kwargs):
                self.status_calls += 1
                time.sleep(0.05)
                return {"gateway": {"running": True}}

        app = _FakeApiApp()
        service = _SlowStatusService(_SnapshotLifecycle(delay=0.05))
        service.order_tracker = _FastOrderTracker([])
        service.broker = _StatusComponent({"connected": True, "ready": True, "status_code": 200})
        service.gateway_manager = _StatusComponent({"running": True})
        service.session_keeper = _StatusComponent({"authenticated": True})
        service.session_keeper.is_authenticated = True
        service.ws_client = _StatusComponent({"ready": True})
        service.ws_client._ready = True

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(0, service.status_calls)
        self.assertEqual(1, service.broker.calls)
        self.assertEqual(0, service.gateway_manager.calls)
        self.assertEqual(0, service.session_keeper.calls)
        self.assertEqual(0, service.ws_client.calls)
        self.assertEqual("fast_runtime_state", payload["orders_fast_diagnostics"]["status_source"])

    def test_orders_fast_snapshot_reads_gateway_flags_without_status_lock(self):
        class _ReadyEvent:
            def is_set(self):
                return True

        class _FastClient:
            _ready = True
            _ready_event = _ReadyEvent()
            _status_code = 200
            _account_data_circuit_until = 0.0
            _account_data_circuit_reason = ""

            def isConnected(self):
                return True

        class _SlowBroker:
            client = _FastClient()

            def __init__(self):
                self.status_calls = 0

            def status(self):
                self.status_calls += 1
                raise AssertionError("orders_fast should not wait for broker.status lock")

        app = _FakeApiApp()
        service = _SnapshotService(_SnapshotLifecycle(delay=0.05))
        service.order_tracker = _FastOrderTracker([])
        broker = _SlowBroker()
        service.broker = broker

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["gateway_running"])
        self.assertEqual(0, broker.status_calls)
        self.assertEqual("fast_runtime_state", payload["orders_fast_diagnostics"]["status_source"])

    def test_orders_fast_snapshot_resolves_environment_without_full_status(self):
        class _EnvFailApp(_FakeApiApp):
            def __init__(self):
                super().__init__()
                self.environment_calls = 0

            def _ibkr_service_environment(self, _service):
                self.environment_calls += 1
                raise AssertionError("orders_fast should use service-local environment")

        app = _EnvFailApp()
        service = _SnapshotService(_SnapshotLifecycle(delay=0.05))
        service.order_tracker = _FastOrderTracker([])

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=False,
                orders_fast=True,
            ),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual("paper", payload["environment"])
        self.assertEqual(0, app.environment_calls)

    def test_orders_fast_snapshot_reuses_cached_full_summary(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle()
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker([])

        full = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        lifecycle.fail = True
        fast = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(service, include_pnl=False, force_refresh=True, orders_fast=True),
        )

        self.assertTrue(full["summary_available"])
        self.assertTrue(fast["summary_available"])
        self.assertEqual("snapshot_cache", fast["orders_fast_diagnostics"]["summary_source"])
        self.assertEqual(1, lifecycle.snapshot_calls)

    def test_orders_fast_open_only_preserves_cached_position_counts(self):
        class _PositionLifecycle(_SnapshotLifecycle):
            def get_account_snapshot(self, _account_id):
                self.snapshot_calls += 1
                return {
                    "summary": {"AccountCode": {"value": "DU123"}, "NetLiquidation": {"value": "1000000"}},
                    "positions": [
                        {"symbol": "INTU", "conid": 270662, "quantity": 16, "mktPrice": 293.19, "avgCost": 307.33},
                        {"symbol": "FLAT", "conid": 123, "quantity": 0, "mktPrice": 10, "avgCost": 0},
                    ],
                }

        app = _FakeApiApp()
        lifecycle = _PositionLifecycle()
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker([])

        full = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        fast = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertEqual(1, full["counts"]["open_positions"])
        self.assertEqual([], fast["positions"])
        self.assertFalse(fast["positions_detail_available"])
        self.assertTrue(fast["positions_count_available"])
        self.assertEqual("omitted_open_orders_only", fast["positions_source"])
        self.assertEqual(1, fast["counts"]["open_positions"])
        self.assertEqual(1, fast["counts"]["long_positions"])
        self.assertEqual(0, fast["counts"]["short_positions"])
        self.assertEqual(1, fast["counts"]["flat_positions"])
        self.assertEqual(2, fast["counts"]["position_rows"])

    def test_orders_fast_snapshot_reuses_stale_full_summary(self):
        app = _FakeApiApp()
        lifecycle = _SnapshotLifecycle()
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker([])

        full = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        now = time.time()
        with app.ibkr_account_snapshot_cache_lock:
            entry = app.ibkr_account_snapshot_cache[(full["environment"], full["account_id"], False)]
            entry["fresh_until"] = now - 1
            entry["expires_at"] = now - 1
            entry["stale_until"] = now + 120

        fast = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertTrue(fast["summary_available"])
        self.assertEqual("snapshot_cache", fast["orders_fast_diagnostics"]["summary_source"])
        self.assertEqual("stale", fast["orders_fast_diagnostics"]["summary_cache_state"])

    def test_snapshot_cache_stale_window_has_pressure_floor(self):
        app = _FakeApiApp()

        self.assertGreaterEqual(_snapshot_cache_stale_seconds(app), 900.0)

    def test_orders_fast_snapshot_merges_pb_rows_when_callback_cache_is_partial(self):
        app = _FakeApiApp()
        app.pb = _RowsPB(
            [
                {
                    "broker_order_id": "1002",
                    "symbol": "MSFT",
                    "status": "Submitted",
                    "relation_status": "active",
                    "role": "entry",
                    "direction": "long",
                    "quantity": 5,
                    "limit_price": 250.0,
                    "environment": "paper",
                }
            ]
        )
        service = _SnapshotService(_SnapshotLifecycle())
        service.order_tracker = _FastOrderTracker(
            [
                {
                    "orderId": "1001",
                    "ticker": "AAPL",
                    "status": "Submitted",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 10,
                    "price": 123.45,
                }
            ]
        )

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertEqual(2, payload["counts"]["open_orders"])
        self.assertEqual({"1001", "1002"}, {order["order_id"] for order in payload["live_open_orders"]})
        self.assertEqual(1, payload["orders_fast_diagnostics"]["pb_fallback_order_count"])
        self.assertFalse(payload["orders_fast_diagnostics"]["pb_fallback_skipped"])

    def test_orders_fast_snapshot_ignores_untrusted_stale_pb_rows(self):
        app = _FakeApiApp()
        app.pb = _RowsPB(
            [
                {
                    "broker_order_id": "2002",
                    "symbol": "MSFT",
                    "status": "Submitted",
                    "relation_status": "active",
                    "role": "entry",
                    "direction": "long",
                    "quantity": 5,
                    "limit_price": 250.0,
                    "environment": "paper",
                }
            ]
        )
        service = _SnapshotService(_SnapshotLifecycle())
        service.order_tracker = _FastOrderTracker([])

        payload = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertEqual(0, payload["counts"]["open_orders"])
        self.assertEqual([], payload["live_open_orders"])
        self.assertEqual(1, payload["orders_fast_diagnostics"]["pb_fallback_raw_order_count"])
        self.assertEqual(0, payload["orders_fast_diagnostics"]["pb_fallback_order_count"])
        self.assertTrue(payload["orders_fast_diagnostics"]["pb_fallback_skipped"])
        self.assertEqual(
            "no_live_or_pressure_evidence",
            payload["orders_fast_diagnostics"]["pb_fallback_trust"]["filter_reason"],
        )

    def test_orders_fast_snapshot_applies_buying_power_reservation_overlay(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)
        service.order_tracker = _FastOrderTracker([])

        full = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        service.buying_power_reservations = _ReservationStore(exposure=5000.0, count=1)
        fast = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertEqual(50000.0, full["summary"]["buying_power"])
        self.assertEqual(45000.0, fast["summary"]["buying_power"])
        self.assertEqual(5000.0, fast["summary"]["local_reserved_exposure"])
        self.assertEqual(1, fast["buying_power_guard"]["local_reserved_count"])

    def test_invalid_zero_summary_does_not_replace_valid_snapshot_cache(self):
        class _ZeroLifecycle(_SnapshotLifecycle):
            def __init__(self):
                super().__init__()
                self.zero = False

            def get_account_snapshot(self, _account_id):
                self.snapshot_calls += 1
                if self.zero:
                    return {
                        "summary": {
                            "AccountCode": {"value": "DU123"},
                            "NetLiquidation": {"value": "0"},
                            "BuyingPower": {"value": "0"},
                            "AvailableFunds": {"value": "0"},
                        },
                        "positions": [],
                    }
                return {
                    "summary": {
                        "AccountCode": {"value": "DU123"},
                        "NetLiquidation": {"value": "100000"},
                        "BuyingPower": {"value": "50000"},
                        "AvailableFunds": {"value": "25000"},
                    },
                    "positions": [],
                }

        app = _FakeApiApp()
        lifecycle = _ZeroLifecycle()
        service = _SnapshotService(lifecycle)

        first = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        lifecycle.zero = True
        refreshed = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                allow_stale=True,
            ),
        )

        self.assertTrue(first["summary_available"])
        self.assertTrue(refreshed["stale"])
        self.assertEqual("stale_after_error", refreshed["cache_state"])
        self.assertEqual(50000.0, refreshed["summary"]["buying_power"])

    def test_orders_fast_open_only_omits_cached_full_position_rows_but_keeps_counts(self):
        class _PositionLifecycle(_SnapshotLifecycle):
            def get_account_snapshot(self, _account_id):
                self.snapshot_calls += 1
                return {
                    "summary": {"AccountCode": {"value": "DU123"}, "NetLiquidation": {"value": "1000000"}},
                    "positions": [
                        {
                            "ticker": "AAPL",
                            "conid": 265598,
                            "position": 5,
                            "mktPrice": 100.0,
                            "acctId": "DU123",
                        }
                    ],
                }

        app = _FakeApiApp()
        lifecycle = _PositionLifecycle()
        service = _SnapshotService(lifecycle)
        service.order_tracker = _FastOrderTracker([])

        full = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        fast = _with_fake_api_app(
            app,
            lambda: _build_ibkr_account_snapshot(
                service,
                include_pnl=False,
                force_refresh=True,
                orders_fast=True,
                orders_fast_open_only=True,
            ),
        )

        self.assertEqual(1, full["counts"]["positions"])
        self.assertEqual(1, fast["counts"]["positions"])
        self.assertEqual(1, fast["counts"]["open_positions"])
        self.assertEqual(1, fast["counts"]["long_positions"])
        self.assertEqual([], fast["positions"])
        self.assertFalse(fast["positions_detail_available"])
        self.assertTrue(fast["positions_count_available"])
        self.assertEqual("omitted_open_orders_only", fast["orders_fast_diagnostics"]["positions_source"])
        self.assertTrue(fast["orders_fast_diagnostics"]["positions_omitted"])
        self.assertIn("runtime_elapsed_ms", fast["diagnostics"]["account_snapshot"])

    def test_buying_power_reuses_fresh_full_snapshot_cache(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        status_calls_after_full = service.status_calls
        buying_power = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertTrue(full_snapshot["summary_available"])
        self.assertEqual("account_snapshot", buying_power["source"])
        self.assertEqual(1, lifecycle.snapshot_calls)
        self.assertEqual(0, lifecycle.summary_calls)
        self.assertEqual(status_calls_after_full, service.status_calls)
        self.assertEqual("ok", buying_power["account_snapshot_health"]["state"])

    def test_buying_power_uses_full_snapshot_cache_when_account_data_circuit_open(self):
        app = _FakeApiApp()
        warm_lifecycle = _BuyingPowerLifecycle()
        warm_service = _BuyingPowerService(warm_lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(warm_service, include_pnl=False))
        active_lifecycle = _BuyingPowerLifecycle()
        active_service = _BuyingPowerService(
            active_lifecycle,
            circuit={"active": True, "reason": "executions_timeout", "remaining_s": 35.0},
        )
        buying_power = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(active_service))

        self.assertTrue(full_snapshot["summary_available"])
        self.assertTrue(buying_power["ok"])
        self.assertEqual("account_snapshot", buying_power["source"])
        self.assertEqual(50000.0, buying_power["summary"]["buying_power"])
        self.assertEqual(0, active_lifecycle.summary_calls)
        self.assertEqual(0, active_lifecycle.snapshot_calls)

    def test_buying_power_uses_stale_full_snapshot_cache_when_account_data_circuit_open(self):
        app = _FakeApiApp()
        warm_lifecycle = _BuyingPowerLifecycle()
        warm_service = _BuyingPowerService(warm_lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(warm_service, include_pnl=False))
        now = time.time()
        with app.ibkr_account_snapshot_cache_lock:
            entry = app.ibkr_account_snapshot_cache[(full_snapshot["environment"], full_snapshot["account_id"], False)]
            entry["fresh_until"] = now - 1
            entry["expires_at"] = now - 1
            entry["stale_until"] = now + 120
        active_lifecycle = _BuyingPowerLifecycle()
        active_service = _BuyingPowerService(
            active_lifecycle,
            circuit={"active": True, "reason": "executions_timeout", "remaining_s": 35.0},
        )

        buying_power = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(active_service))

        self.assertTrue(buying_power["ok"])
        self.assertEqual("account_snapshot", buying_power["source"])
        self.assertEqual("stale_after_account_data_circuit", buying_power["cache_state"])
        self.assertTrue(buying_power["stale"])
        self.assertEqual(50000.0, buying_power["summary"]["buying_power"])
        self.assertEqual(0, active_lifecycle.summary_calls)
        self.assertEqual(0, active_lifecycle.snapshot_calls)

    def test_buying_power_reuses_fresh_include_pnl_full_snapshot_cache(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=True))
        buying_power = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertTrue(full_snapshot["summary_available"])
        self.assertEqual("account_snapshot", buying_power["source"])
        self.assertEqual(1, lifecycle.snapshot_calls)
        self.assertEqual(0, lifecycle.summary_calls)
        self.assertEqual("ok", buying_power["account_snapshot_health"]["state"])

    def test_buying_power_snapshot_single_flight_refreshes_full_snapshot_when_cache_empty(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle(delay=0.05)
        service = _BuyingPowerService(lifecycle)

        def run():
            with ThreadPoolExecutor(max_workers=4) as executor:
                return list(executor.map(lambda _idx: _build_ibkr_account_buying_power_snapshot(service), range(4)))

        payloads = _with_fake_api_app(app, run)

        self.assertEqual(1, lifecycle.snapshot_calls)
        self.assertEqual(0, lifecycle.summary_calls)
        self.assertEqual(0, service.status_calls)
        self.assertTrue(all(payload["source"] == "account_snapshot" for payload in payloads))
        self.assertTrue(all(payload["buying_power_guard"]["state"] == "ok" for payload in payloads))

    def test_buying_power_snapshot_uses_stale_guard_when_refresh_unavailable(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)

        first = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))
        self.assertTrue(first["ok"])

        now = time.time()
        with app.ibkr_account_snapshot_cache_lock:
            for entry in app.ibkr_account_snapshot_cache.values():
                entry["fresh_until"] = now - 1
                entry["expires_at"] = now - 1
                entry["stale_until"] = now + 120

        def fail_snapshot(_account_id):
            raise TimeoutError("account snapshot timeout")

        def fail_summary(_account_id):
            raise TimeoutError("account summary timeout")

        lifecycle.get_account_snapshot = fail_snapshot
        lifecycle.get_account_summary = fail_summary

        stale = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertTrue(stale["ok"])
        self.assertTrue(stale["stale"])
        self.assertIn(stale["cache_state"], {"stale", "stale_after_error"})
        self.assertTrue(stale.get("refresh_error"))
        self.assertEqual("ok", stale["buying_power_guard"]["state"])

    def test_buying_power_snapshot_blocks_when_full_snapshot_too_stale(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        now = time.time()
        with app.ibkr_account_snapshot_cache_lock:
            entry = app.ibkr_account_snapshot_cache[(full_snapshot["environment"], full_snapshot["account_id"], False)]
            entry["stored_at"] = now - 600
            entry["fresh_until"] = now - 1
            entry["expires_at"] = now - 1
            entry["stale_until"] = now + 120

        def fail_snapshot(_account_id):
            raise TimeoutError("account snapshot timeout")

        lifecycle.get_account_snapshot = fail_snapshot

        payload = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertTrue(payload["stale"])
        self.assertEqual("unavailable", payload["buying_power_guard"]["state"])
        self.assertEqual("account_snapshot_stale", payload["buying_power_guard"]["reason"])
        self.assertTrue(payload["buying_power_guard"]["stale_blocked"])

    def test_buying_power_snapshot_short_circuits_when_account_data_circuit_open(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle, circuit={"active": True, "reason": "positions_timeout", "remaining_s": 42.0})

        payload = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertFalse(payload["ok"])
        self.assertEqual("account_data_circuit_open", payload["buying_power_guard"]["reason"])
        self.assertEqual(42.0, payload["retry_after_s"])
        self.assertEqual(0, lifecycle.summary_calls)
        self.assertEqual(0, service.status_calls)
        with app.ibkr_account_snapshot_cache_lock:
            self.assertNotIn(("paper", "DU123::buying_power", False), app.ibkr_account_snapshot_cache)


if __name__ == "__main__":
    unittest.main()
