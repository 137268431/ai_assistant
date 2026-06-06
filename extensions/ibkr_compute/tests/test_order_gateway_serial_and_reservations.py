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
from ibkr_compute.order.buying_power_reservations import (  # noqa: E402
    BuyingPowerReservationStore,
    apply_reservations_to_buying_power_summary,
)
from ibkr_compute.order.gateway_serial import GatewayOrderMutationGate  # noqa: E402
from ibkr_compute.order.order_modifier import OrderModifier  # noqa: E402
from ibkr_compute.order.order_placer import OrderPlacer  # noqa: E402


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
