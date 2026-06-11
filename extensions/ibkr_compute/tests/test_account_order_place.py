import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.action_builders import orders as order_actions


class _FakeApiApp:
    def _ibkr_service_environment(self, service):
        return getattr(service, "environment", "paper")

    def _ibkr_service_uses_paper_account(self, service):
        return getattr(service, "environment", "paper") == "paper"


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def has_value_for_environment(self, key, environment):
        return key in self.values

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.values.get(key, default)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def get_for_environment(self, key, environment, default=None):
        return self.values.get(key, default)


class _ConidResolver:
    def resolve(self, symbol):
        return 265598


class _OrderTracker:
    def find_duplicate_open_entry(self, **kwargs):
        return None

    def get_cached_live_orders(self, *, include_all=False):
        return []


class _ReservationStore:
    def snapshot(self):
        return {}


class _PB:
    def notify_system_event(self, *args, **kwargs):
        return {"ok": True}


class _OrderPlacer:
    def __init__(self, result):
        self.result = dict(result)
        self.calls = []

    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return dict(self.result)


class _Service:
    def __init__(self, result, *, config=None):
        self.environment = "paper"
        self.is_running = True
        self.is_starting = False
        self.config = config or _Config()
        self.conid_resolver = _ConidResolver()
        self.order_tracker = _OrderTracker()
        self.buying_power_reservations = _ReservationStore()
        self.pb = _PB()
        self.order_placer = _OrderPlacer(result)


def _coerce_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AccountOrderPlaceActionTest(unittest.TestCase):
    def _call(self, service, payload):
        with (
            mock.patch.object(order_actions, "_api_app", return_value=_FakeApiApp()),
            mock.patch.object(order_actions, "_app_coerce_float", side_effect=_coerce_float),
            mock.patch.object(order_actions, "build_fast_snapshot_status", return_value={"session": {"authenticated": True}}),
            mock.patch.object(
                order_actions,
                "_build_ibkr_account_buying_power_snapshot",
                return_value={"summary": {"remaining_buying_power": 250000.0, "net_liquidation": 300000.0}},
            ),
        ):
            return order_actions._build_ibkr_place_order_response(service, payload)

    def test_wait_for_confirmation_forces_sync_even_when_fast_accept_configured(self):
        service = _Service(
            {
                "ok": True,
                "order_ids": ["101", "102", "103"],
                "entry_coid": "entry_AAPL",
                "tp_coid": "tp_AAPL",
                "sl_coid": "sl_AAPL",
                "protection_complete": True,
            },
            config=_Config({"ibkr_order_place_fast_accept_enabled": "true"}),
        )

        payload, status = self._call(
            service,
            {
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 10,
                "order_type": "LMT",
                "entry_price": 100.0,
                "take_profit_price": 103.0,
                "stop_loss_price": 98.0,
                "wait_for_confirmation": True,
            },
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("sync", service.order_placer.calls[0]["confirmation_mode"])
        self.assertEqual("sync", payload["confirmation_mode"])
        self.assertFalse(payload["fast_ack"])
        self.assertTrue(payload["wait_for_confirmation"])

    def test_protection_incomplete_returns_failure_but_preserves_entry_diagnostics(self):
        service = _Service(
            {
                "ok": True,
                "order_ids": ["101", "102", "103"],
                "entry_coid": "entry_AAPL",
                "tp_coid": "tp_AAPL",
                "sl_coid": "sl_AAPL",
                "error": "order_submission_unconfirmed:missing=103",
                "protection_complete": False,
                "protection_incomplete": True,
                "missing_order_ids": ["103"],
                "missing_protection_roles": ["stop_loss"],
            }
        )

        payload, status = self._call(
            service,
            {
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 10,
                "order_type": "LMT",
                "entry_price": 100.0,
                "take_profit_price": 103.0,
                "stop_loss_price": 98.0,
                "wait_for_confirmation": True,
            },
        )

        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("protection_incomplete", payload["error"])
        self.assertFalse(payload["result"]["ok"])
        self.assertTrue(payload["result"]["entry_submitted"])
        self.assertTrue(payload["result"]["protection_incomplete"])
        self.assertEqual("order_submission_unconfirmed:missing=103", payload["result"]["broker_error"])
        self.assertEqual(["stop_loss"], payload["result"]["missing_protection_roles"])
        self.assertEqual(["103"], payload["result"]["missing_order_ids"])


if __name__ == "__main__":
    unittest.main()
