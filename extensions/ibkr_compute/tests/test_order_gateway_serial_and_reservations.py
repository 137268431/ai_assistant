import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.buying_power_guard import build_buying_power_guard  # noqa: E402
from ibkr_compute.api.account.action_builders import common as action_common  # noqa: E402
from ibkr_compute.broker.ib_gateway import BrokerAdapter, _IBGatewayApp  # noqa: E402
from ibkr_compute.api.account.snapshot_builder.recovery import recover_live_open_orders  # noqa: E402
from ibkr_compute.order.buying_power_reservations import (  # noqa: E402
    BuyingPowerReservationStore,
    apply_reservations_to_buying_power_summary,
)
from ibkr_compute.order.gateway_serial import GatewayOrderMutationGate  # noqa: E402
from ibkr_compute.order.order_lifecycle import OrderLifecycle  # noqa: E402
from ibkr_compute.order.order_tracker import OrderTracker  # noqa: E402
from ibkr_compute.order.order_modifier import OrderModifier  # noqa: E402
from ibkr_compute.order.order_placer import OrderPlacer  # noqa: E402
from ibkr_compute.order.symbol_queue import SymbolOrderCommandScheduler  # noqa: E402


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.values.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    def get_float_for_environment(self, key, environment, default=0.0):
        return float(self.values.get(key, default))

    def get_int_for_environment(self, key, environment, default=0):
        return int(self.values.get(key, default))


class _StatePB:
    def __init__(self):
        self.states = {}
        self.upserts = []

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"id": f"{state_key}:{environment}:{date}", "data": dict(data or {})}
        self.states[(state_key, environment, date)] = record
        return record

    def upsert_order(self, payload):
        self.upserts.append(dict(payload))
        return dict(payload)


class _BracketBroker:
    def __init__(self, order_ids=None):
        self.order_ids = list(order_ids or ["101", "102", "103"])
        self.calls = []

    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": list(self.order_ids),
            "bracket_group": "grp-1",
            "trade_group_id": "grp-1",
            "oca_group": "grp-1",
            "entry_coid": "entry_grp-1",
            "tp_coid": "tp_grp-1",
            "sl_coid": "sl_grp-1",
            "order_family_type": "bracket_oco",
            "entry_price": kwargs["entry_price"],
            "take_profit_price": kwargs["take_profit_price"],
            "stop_loss_price": kwargs["stop_loss_price"],
            "quantity": kwargs["quantity"],
            "take_profit_quantity": kwargs["quantity"],
            "stop_loss_quantity": kwargs["quantity"],
            "protection_complete": True,
        }


class _SerialBroker(_BracketBroker):
    def __init__(self, events, started):
        super().__init__()
        self.events = events
        self.started = started

    def place_bracket_order(self, **kwargs):
        self.events.append("place_start")
        self.started.set()
        time.sleep(0.15)
        self.events.append("place_end")
        return super().place_bracket_order(**kwargs)

    def modify_order(self, order_id, updates, account_id=""):
        self.events.append("modify_start")
        return {"ok": True, "order_id": str(order_id), "order": dict(updates or {})}


class GatewaySerialAndReservationTest(unittest.TestCase):
    def test_symbol_queue_serializes_same_symbol_and_limits_cross_symbol_parallelism(self):
        config = _Config({"ibkr_order_symbol_queue_max_active_symbols": 2})
        scheduler = SymbolOrderCommandScheduler(config=config, environment="paper")
        events = []
        events_lock = threading.Lock()
        active = 0
        max_active = 0

        def run(symbol, label, delay=0.05):
            def command():
                nonlocal active, max_active
                with events_lock:
                    active += 1
                    max_active = max(max_active, active)
                    events.append((label, "start"))
                time.sleep(delay)
                with events_lock:
                    events.append((label, "end"))
                    active -= 1
                return {"ok": True, "label": label}

            return scheduler.submit(symbol=symbol, operation="test", fn=command)

        threads = [
            threading.Thread(target=lambda: run("AAPL", "aapl-1", 0.08)),
            threading.Thread(target=lambda: run("AAPL", "aapl-2", 0.01)),
            threading.Thread(target=lambda: run("MSFT", "msft-1", 0.05)),
        ]
        threads[0].start()
        time.sleep(0.01)
        for thread in threads[1:]:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertLessEqual(max_active, 2)
        self.assertLess(
            events.index(("aapl-1", "end")),
            events.index(("aapl-2", "start")),
        )
        self.assertIn(("msft-1", "start"), events)

    def test_buying_power_pre_reservation_is_atomic_under_parallel_requests(self):
        pb = _StatePB()
        store = BuyingPowerReservationStore(pb, environment="paper")
        config = _Config({"ibkr_buying_power_guard_enabled": "true"})
        stale_guard = build_buying_power_guard(
            {"buying_power": 19000.0, "net_liquidation": 100000.0},
            config=config,
            environment="paper",
            requested_exposure=5000.0,
        )
        results = []
        lock = threading.Lock()

        def reserve(symbol):
            result = store.reserve_entry_if_available(
                pre_submit_guard=dict(stale_guard),
                config=config,
                signal_id=f"sig-{symbol.lower()}",
                trade_group_id=f"grp-{symbol.lower()}",
                symbol=symbol,
                direction="long",
                quantity=50,
                entry_price=100.0,
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=reserve, args=(symbol,)) for symbol in ("AAPL", "MSFT")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(1 for item in results if item.get("ok")))
        self.assertEqual(1, sum(1 for item in results if item.get("error") == "buying_power_blocked"))
        self.assertEqual(1, store.snapshot()["count"])
        blocked = next(item for item in results if item.get("error") == "buying_power_blocked")
        self.assertEqual(5000.0, blocked["buying_power_guard"]["local_reserved_exposure"])

    def test_bracket_submission_reserves_positive_exposure_for_short_orders(self):
        pb = _StatePB()
        store = BuyingPowerReservationStore(pb, environment="paper")
        placer = OrderPlacer(
            pb_client=pb,
            broker=_BracketBroker(["201", "202", "203"]),
            account_id="DU123",
            environment="paper",
            reservation_store=store,
        )

        result = placer.place_bracket_order(
            conid=123,
            symbol="TSLA",
            direction="short",
            quantity=7,
            entry_price=600.0,
            take_profit_price=580.0,
            stop_loss_price=620.0,
            signal_id="sig-short",
            trade_group_id="grp-short",
        )

        self.assertTrue(result["ok"])
        snapshot = store.snapshot()
        self.assertEqual(1, snapshot["count"])
        self.assertEqual(4200.0, snapshot["exposure"])
        self.assertEqual("short", snapshot["active"][0]["direction"])
        self.assertEqual("201", snapshot["active"][0]["entry_order_id"])
        self.assertTrue(result["symbol_queue"]["enabled"])
        self.assertEqual("TSLA", result["symbol_queue"]["symbol"])

    def test_bracket_order_ids_do_not_scan_open_orders_by_default(self):
        class _Client:
            def __init__(self):
                self.calls = 0

            def max_seen_order_id(self):
                return 900

            def next_order_ids_above(self, count, minimum=0):
                self.calls += 1
                return list(range(max(1000, int(minimum or 0)), max(1000, int(minimum or 0)) + int(count)))

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()
        adapter.list_open_orders = mock.Mock(side_effect=AssertionError("open order scan should be skipped"))

        self.assertEqual([1000, 1001, 1002], adapter._next_bracket_order_ids())
        self.assertEqual(1, adapter.client.calls)
        adapter.list_open_orders.assert_not_called()

    def test_resolve_contract_uses_conid_fast_path_for_order_contracts(self):
        class _Client:
            _contract_cache_by_symbol = {}
            _contract_cache_by_conid = {}

            def request_contract_details(self, **kwargs):
                raise AssertionError("contract details should not be requested")

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()

        contract = adapter.resolve_contract(symbol="AAPL", conid=265598)

        self.assertEqual(265598, contract["conid"])
        self.assertEqual("AAPL", contract["symbol"])
        self.assertEqual("SMART", contract["exchange"])

    def test_bracket_submission_confirms_from_callbacks_without_open_order_refresh(self):
        class _Client:
            def __init__(self):
                self.open_order_refresh_calls = 0

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

            def clear_order_error(self, order_id):
                pass

            def request_open_orders_for_order_confirmation(self, **kwargs):
                self.open_order_refresh_calls += 1
                raise AssertionError("callback snapshots should confirm the bracket")

        client = _Client()
        result = _IBGatewayApp.await_order_submissions(
            client,
            ["401", "402", "403"],
            timeout=0.5,
            poll_interval=0.01,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(0, client.open_order_refresh_calls)
        self.assertEqual({"401", "402", "403"}, set(result["orders"].keys()))
        self.assertEqual("snapshot", result["orders"]["401"]["source"])

    def test_order_confirmation_open_order_fallback_is_shared_and_throttled(self):
        class _Client:
            def __init__(self):
                self._state_lock = threading.Lock()
                self._order_confirmation_open_orders_cache = {}
                self.request_calls = 0

            def request_open_orders(self, **kwargs):
                self.request_calls += 1
                return []

        client = _Client()

        with mock.patch("ibkr_compute.broker.ib_gateway.ORDER_CONFIRM_OPEN_ORDERS_SHARED_CACHE_TTL_SECONDS", 10.0):
            first = _IBGatewayApp.request_open_orders_for_order_confirmation(client, timeout=1)
            second = _IBGatewayApp.request_open_orders_for_order_confirmation(client, timeout=1)

        self.assertEqual([], first)
        self.assertEqual([], second)
        self.assertEqual(1, client.request_calls)

    def test_cancel_not_found_notice_is_idempotent_without_account_refresh(self):
        class _Client:
            def __init__(self):
                self.cleared = []
                self.refresh_calls = 0

            def get_order_error(self, order_id):
                return {
                    "order_id": str(order_id),
                    "code": 10147,
                    "message": f"OrderId {order_id} that needs to be cancelled is not found",
                }

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

            def request_open_orders_for_order_confirmation(self, **kwargs):
                self.refresh_calls += 1
                raise AssertionError("10147 should finish cancel confirmation immediately")

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()

        result = adapter.await_order_cancelled("999", timeout=1.0, poll_interval=0.01)

        self.assertTrue(result["ok"])
        self.assertEqual("NOT_OPEN", result["status"])
        self.assertEqual("cancel_terminal_notice", result["source"])
        self.assertEqual(["999"], adapter.client.cleared)
        self.assertEqual(0, adapter.client.refresh_calls)

    def test_cancel_missing_from_open_orders_overrides_stale_submitted_snapshot(self):
        class _Client:
            def __init__(self):
                self.refresh_calls = []

            def get_order_error(self, order_id):
                return {}

            def clear_order_error(self, order_id):
                pass

            def get_order_snapshot(self, order_id):
                return {
                    "orderId": str(order_id),
                    "status": "PreSubmitted",
                    "filledQuantity": 0,
                    "remainingQuantity": 10,
                }

            def request_open_orders_for_order_confirmation(self, **kwargs):
                self.refresh_calls.append(dict(kwargs))
                return []

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()

        with mock.patch("ibkr_compute.broker.ib_gateway.ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS", 0.0), \
             mock.patch("ibkr_compute.broker.ib_gateway.CANCEL_CONFIRM_OPEN_ORDERS_RECONCILE_ENABLED", True):
            result = adapter.await_order_cancelled("999", timeout=1.0, poll_interval=0.01)

        self.assertTrue(result["ok"])
        self.assertEqual("NOT_OPEN", result["status"])
        self.assertEqual("open_orders_missing_stale_snapshot", result["source"])
        self.assertEqual("PreSubmitted", result["order"]["status"])
        self.assertEqual(1, len(adapter.client.refresh_calls))
        self.assertTrue(adapter.client.refresh_calls[0]["include_all"])

    def test_cancel_missing_from_open_orders_does_not_hide_fill_evidence(self):
        class _Client:
            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {
                    "orderId": str(order_id),
                    "status": "PreSubmitted",
                    "filledQuantity": 1,
                    "remainingQuantity": 9,
                }

            def request_open_orders_for_order_confirmation(self, **kwargs):
                return []

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()

        with mock.patch("ibkr_compute.broker.ib_gateway.ORDER_CONFIRM_OPEN_ORDERS_FALLBACK_DELAY_SECONDS", 0.0), \
             mock.patch("ibkr_compute.broker.ib_gateway.CANCEL_CONFIRM_OPEN_ORDERS_RECONCILE_ENABLED", True):
            result = adapter.await_order_cancelled("999", timeout=1.0, poll_interval=0.01)

        self.assertFalse(result["ok"])
        self.assertEqual("order_filled_during_cancel", result["error"])

    def test_cancel_all_uses_callback_cache_when_account_open_orders_unavailable(self):
        class _Client:
            def __init__(self):
                self.cancelled = []

            def get_order_snapshots(self, include_all=False):
                return [
                    {"orderId": "901", "status": "PreSubmitted"},
                    {"orderId": "902", "status": "Submitted"},
                ]

            def clear_order_error(self, order_id):
                pass

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "PreSubmitted"}

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()
        adapter.list_open_orders = mock.Mock(side_effect=TimeoutError("account_data_circuit_open:open_orders_timeout"))

        with mock.patch("ibkr_compute.broker.ib_gateway.CANCEL_ALL_CONFIRM_TIMEOUT_SECONDS", 0.03), \
             mock.patch("ibkr_compute.broker.ib_gateway.CANCEL_ALL_RECONCILE_POLL_INTERVAL_SECONDS", 0.01):
            result = adapter.cancel_all_orders(metric_environment="paper")

        self.assertTrue(result["ok"])
        self.assertEqual("CANCEL_ALL_REQUESTED", result["status"])
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual("callback_cache", result["order_list_source"])
        self.assertEqual(["901", "902"], adapter.client.cancelled)
        self.assertEqual(2, result["submitted"])

    def test_action_response_reports_total_operation_and_snapshot_elapsed(self):
        class _Tracker:
            def get_cached_live_orders(self, *, include_all=False):
                self.include_all = include_all
                return [
                    {"orderId": "101", "status": "Submitted"},
                    {"orderId": "102", "status": "Cancelled"},
                ]

        class _Service:
            environment = "paper"
            order_tracker = _Tracker()

        started_at = time.perf_counter() - 0.05
        with mock.patch.object(action_common, "_build_ibkr_account_snapshot", return_value={"ok": True}) as snapshot_builder:
            payload, status = action_common._build_snapshot_action_response(
                _Service(),
                "place_order",
                {"ok": True},
                action_started_at=started_at,
                operation_elapsed_s=0.04,
            )

        self.assertEqual(200, status)
        self.assertGreaterEqual(payload["order_action_elapsed_s"], 0.04)
        self.assertEqual(0.04, payload["order_operation_elapsed_s"])
        self.assertGreaterEqual(payload["order_snapshot_elapsed_s"], 0.0)
        snapshot_builder.assert_not_called()
        self.assertEqual("account_action_orders_fast", payload["snapshot"]["source"])
        self.assertEqual(2, payload["snapshot"]["counts"]["orders"])
        self.assertEqual(1, payload["snapshot"]["counts"]["open_orders"])

    def test_modify_price_confirmation_uses_local_callback_before_refresh(self):
        class _Client:
            def __init__(self):
                self.refresh_calls = 0

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted", "auxPrice": 97.25}

            def request_open_orders_for_order_confirmation(self, **kwargs):
                self.refresh_calls += 1
                raise AssertionError("local modify callback should confirm price update")

        adapter = BrokerAdapter.__new__(BrokerAdapter)
        adapter.client = _Client()

        result = adapter.await_order_price_update(
            "888",
            expected_price=97.25,
            fields=["auxPrice"],
            timeout=1.0,
            poll_interval=0.01,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(0, adapter.client.refresh_calls)

    def test_order_tracker_recovers_live_orders_from_cached_gateway_callbacks(self):
        class _Broker:
            def __init__(self):
                self.poll_calls = 0

            def list_cached_open_orders(self, include_all=False):
                return [
                    {"orderId": "701", "ticker": "AAPL", "status": "Submitted", "side": "BUY", "totalSize": 10},
                    {"orderId": "702", "ticker": "AAPL", "status": "Cancelled", "side": "SELL", "totalSize": 10},
                    {"orderId": "703", "ticker": "MSFT", "status": "PreSubmitted", "side": "SELL", "totalSize": 5},
                ]

            def list_open_orders(self, **kwargs):
                self.poll_calls += 1
                raise AssertionError("account snapshot recovery should not poll open orders when callback cache is present")

            def get_order_snapshot(self, order_id):
                return {}

            def list_recent_fills(self):
                return []

        broker = _Broker()
        tracker = OrderTracker(broker=broker, environment="paper")

        cached = tracker.get_cached_live_orders()
        recovered = tracker.get_complete_live_open_orders(bulk_orders=[])

        self.assertEqual(["701", "703"], [item["orderId"] for item in cached])
        self.assertEqual(["701", "703"], sorted(item["orderId"] for item in recovered["orders"]))
        self.assertEqual(["701", "702", "703"], recovered["diagnostics"]["cached_order_ids"])
        self.assertEqual(0, broker.poll_calls)

    def test_account_snapshot_recovers_unresolved_pb_active_orders(self):
        class _ApiApp:
            class _Logger:
                def info(self, *args, **kwargs):
                    pass

                def debug(self, *args, **kwargs):
                    pass

            logger = _Logger()

        class _Tracker:
            def get_complete_live_open_orders(self, **kwargs):
                return {
                    "orders": [],
                    "coverage": {
                        "coverage_state": "degraded",
                        "unresolved_order_ids": ["801"],
                        "unresolved_seed_count": 1,
                    },
                    "diagnostics": {},
                }

        class _Service:
            order_tracker = _Tracker()

        merged, payload = recover_live_open_orders(
            _ApiApp(),
            _Service(),
            [],
            ["801"],
            fallback_rows=[
                {
                    "broker_order_id": "801",
                    "symbol": "AAPL",
                    "role": "entry",
                    "direction": "long",
                    "quantity": 10,
                    "limit_price": 100,
                    "status": "Submitted",
                    "relation_status": "active",
                }
            ],
        )

        self.assertEqual("801", merged[0]["orderId"])
        self.assertEqual("AAPL", merged[0]["ticker"])
        self.assertEqual("pb_active_seed", merged[0]["_recovery_source"])
        self.assertEqual("recovered", payload["coverage"]["coverage_state"])
        self.assertEqual([], payload["coverage"]["unresolved_order_ids"])

    def test_lifecycle_backoff_does_not_block_independent_account_summary_retry(self):
        lifecycle = OrderLifecycle.__new__(OrderLifecycle)
        lifecycle._account_data_backoff_until = time.time() + 30.0
        lifecycle._account_data_backoff_reason = "account_updates_timeout"
        lifecycle._account_data_backoff_last_warn_at = time.time()

        self.assertTrue(lifecycle._should_skip_account_data_fetch(operation="account_snapshot"))
        self.assertFalse(lifecycle._should_skip_account_data_fetch(operation="account_summary"))

    def test_open_orders_queue_timeout_does_not_block_summary_snapshot_retry(self):
        lifecycle = OrderLifecycle.__new__(OrderLifecycle)
        lifecycle._account_data_backoff_until = time.time() + 30.0
        lifecycle._account_data_backoff_reason = "account_data_request_queue_timeout:open_orders_all:timeout_s=3"
        lifecycle._account_data_backoff_last_warn_at = time.time()

        self.assertFalse(lifecycle._should_skip_account_data_fetch(operation="account_snapshot"))
        self.assertFalse(lifecycle._should_skip_account_data_fetch(operation="account_summary"))
        self.assertFalse(lifecycle._should_skip_account_data_fetch(operation="positions"))
        self.assertTrue(lifecycle._should_skip_account_data_fetch(operation="open_orders"))

    def test_tracker_backoff_is_specific_to_order_fetch_kind(self):
        tracker = OrderTracker.__new__(OrderTracker)
        tracker._account_data_backoff_until = time.time() + 30.0
        tracker._account_data_backoff_reason = "account_data_request_queue_timeout:executions:timeout_s=3"
        tracker._account_data_backoff_last_warn_at = time.time()

        self.assertFalse(tracker._should_skip_account_data_fetch(operation="live_orders"))
        self.assertTrue(tracker._should_skip_account_data_fetch(operation="recent_execution_fills"))

    def test_buying_power_guard_subtracts_local_reservations_before_next_order(self):
        pb = _StatePB()
        store = BuyingPowerReservationStore(pb, environment="paper")
        store.reserve_entry(
            signal_id="sig-a",
            trade_group_id="grp-a",
            entry_order_id="301",
            symbol="AAPL",
            direction="long",
            quantity=50,
            entry_price=100.0,
        )

        adjusted = apply_reservations_to_buying_power_summary(
            {"buying_power": 12000.0, "net_liquidation": 100000.0},
            store.snapshot(),
        )
        guard = build_buying_power_guard(adjusted, config=_Config(), environment="paper", requested_exposure=1000.0)

        self.assertEqual(7000.0, adjusted["buying_power"])
        self.assertEqual("blocked", guard["state"])
        self.assertEqual(6000.0, guard["remaining_after"])

    def test_gateway_gate_rechecks_stale_buying_power_guard_against_latest_reservations(self):
        class _IncrementingBroker(_BracketBroker):
            def place_bracket_order(self, **kwargs):
                base = 301 + len(self.calls) * 10
                self.order_ids = [str(base), str(base + 1), str(base + 2)]
                return super().place_bracket_order(**kwargs)

        pb = _StatePB()
        store = BuyingPowerReservationStore(pb, environment="paper")
        config = _Config({"ibkr_buying_power_guard_enabled": "true"})
        broker = _IncrementingBroker()
        placer = OrderPlacer(
            pb_client=pb,
            config=config,
            environment="paper",
            broker=broker,
            reservation_store=store,
        )
        stale_guard = build_buying_power_guard(
            {"buying_power": 19000.0, "net_liquidation": 100000.0},
            config=config,
            environment="paper",
            requested_exposure=5000.0,
        )

        first = placer.place_bracket_order(
            conid=123,
            symbol="AAPL",
            direction="long",
            quantity=50,
            entry_price=100.0,
            take_profit_price=104.0,
            stop_loss_price=98.0,
            signal_id="sig-a",
            trade_group_id="grp-a",
            buying_power_guard=dict(stale_guard),
        )
        second = placer.place_bracket_order(
            conid=456,
            symbol="MSFT",
            direction="long",
            quantity=50,
            entry_price=100.0,
            take_profit_price=104.0,
            stop_loss_price=98.0,
            signal_id="sig-b",
            trade_group_id="grp-b",
            buying_power_guard=dict(stale_guard),
        )

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual("buying_power_blocked", second["error"])
        self.assertEqual(1, len(broker.calls))
        self.assertEqual(1, store.snapshot()["count"])
        self.assertEqual(5000.0, second["buying_power_guard"]["local_reserved_exposure"])
        self.assertEqual(9000.0, second["buying_power_guard"]["remaining_after"])

    def test_shared_gateway_gate_serializes_place_and_modify_operations(self):
        events = []
        started = threading.Event()
        config = _Config(
            {
                "ibkr_gateway_order_serial_enabled": "true",
                "ibkr_gateway_order_serial_timeout_sec": "5",
            }
        )
        gate = GatewayOrderMutationGate(config=config, environment="paper")
        broker = _SerialBroker(events, started)
        placer = OrderPlacer(config=config, environment="paper", broker=broker, gateway_gate=gate)
        modifier = OrderModifier(config=config, environment="paper", broker=broker, gateway_gate=gate)

        place_thread = threading.Thread(
            target=lambda: placer.place_bracket_order(
                conid=123,
                symbol="MSFT",
                direction="long",
                quantity=1,
                entry_price=100.0,
                take_profit_price=104.0,
                stop_loss_price=98.0,
                signal_id="sig-msft",
            )
        )
        modify_thread = threading.Thread(target=lambda: modifier.modify_order("999", {"auxPrice": 99.0}))

        with mock.patch("ibkr_compute.order.order_placer.record_gateway_order_serial_event") as placer_metric, mock.patch(
            "ibkr_compute.order.order_modifier.record_gateway_order_serial_event"
        ) as modifier_metric:
            place_thread.start()
            self.assertTrue(started.wait(timeout=1.0))
            modify_thread.start()
            place_thread.join(timeout=2.0)
            modify_thread.join(timeout=2.0)

        self.assertEqual(["place_start", "place_end", "modify_start"], events)
        placer_metric.assert_called()
        modifier_metric.assert_called()
        self.assertEqual("place_bracket_order", placer_metric.call_args.kwargs["operation"])
        self.assertEqual("modify_order", modifier_metric.call_args.kwargs["operation"])
        self.assertGreaterEqual(modifier_metric.call_args.kwargs["queue_wait_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
