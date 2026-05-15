import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.buying_power_guard import (  # noqa: E402
    build_buying_power_guard,
    enrich_buying_power_summary,
    estimate_entry_exposure,
)
import ibkr_compute.api.account.action_builders.orders as order_actions  # noqa: E402


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, environment, default=None):
        return self.values.get(key, default)

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.values.get(key, default)
        return str(value).lower() in {"true", "1", "yes", "on"} if isinstance(value, str) else bool(value)

    def get_float_for_environment(self, key, environment, default=0.0):
        try:
            return float(self.values.get(key, default))
        except (TypeError, ValueError):
            return float(default)


class BuyingPowerGuardHelperTest(unittest.TestCase):
    def test_enrich_summary_adds_remaining_buying_power_pct(self):
        summary = enrich_buying_power_summary({"buying_power": 50000, "net_liquidation": 200000})

        self.assertEqual(50000, summary["remaining_buying_power"])
        self.assertEqual(25.0, summary["remaining_buying_power_pct_net_liq"])

    def test_guard_uses_hybrid_thresholds_and_requested_exposure(self):
        guard = build_buying_power_guard(
            {"buying_power": 30000, "net_liquidation": 100000},
            requested_exposure=10000,
        )

        self.assertEqual("warning", guard["state"])
        self.assertEqual(25000, guard["warn_floor"])
        self.assertEqual(10000, guard["block_floor"])
        self.assertEqual(20000, guard["remaining_after"])
        self.assertEqual(20.0, guard["remaining_after_pct_net_liq"])

    def test_guard_blocks_below_block_floor(self):
        guard = build_buying_power_guard(
            {"buying_power": 12000, "net_liquidation": 100000},
            requested_exposure=3000,
        )

        self.assertEqual("blocked", guard["state"])
        self.assertEqual("buying_power_below_block_threshold", guard["reason"])

    def test_estimate_entry_exposure_uses_market_order_conservative_price(self):
        exposure = estimate_entry_exposure(10, None, 104, 98, "long", "MKT")

        self.assertEqual(1040, exposure)


class FakeApiApp:
    def _ibkr_service_environment(self, service):
        return "live"

    def _ibkr_service_uses_paper_account(self, service):
        return False


class FakeConidResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, symbol):
        self.calls.append(symbol)
        return 123


class FakeOrderTracker:
    def find_duplicate_open_entry(self, **kwargs):
        return None


class FakeOrderPlacer:
    def __init__(self):
        self.calls = []

    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True, "order_ids": ["101", "102", "103"], "protection_complete": True}


class FakePB:
    def __init__(self):
        self.events = []

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": dict(detail or {}), **kwargs})
        return {"ok": True}


class FakeOrderService:
    is_running = True
    is_starting = False

    def __init__(self, config=None):
        self.config = config or FakeConfig()
        self.conid_resolver = FakeConidResolver()
        self.order_tracker = FakeOrderTracker()
        self.order_placer = FakeOrderPlacer()
        self.pb = FakePB()

    def status(self):
        return {"session": {"authenticated": True}}


class BuyingPowerManualOrderActionTest(unittest.TestCase):
    def setUp(self):
        self.old_api_app = order_actions._api_app
        self.old_coerce_float = order_actions._app_coerce_float
        self.old_snapshot = order_actions._build_ibkr_account_snapshot
        self.old_action_response = order_actions._build_snapshot_action_response
        order_actions._api_app = lambda: FakeApiApp()
        order_actions._app_coerce_float = lambda value, default=None: (
            default if value in (None, "") else float(value)
        )

    def tearDown(self):
        order_actions._api_app = self.old_api_app
        order_actions._app_coerce_float = self.old_coerce_float
        order_actions._build_ibkr_account_snapshot = self.old_snapshot
        order_actions._build_snapshot_action_response = self.old_action_response

    def test_manual_order_blocked_when_remaining_buying_power_would_cross_block_floor(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_snapshot = lambda _service: {
            "ok": True,
            "summary": {"buying_power": 12000, "net_liquidation": 100000},
        }

        payload, status = order_actions._build_ibkr_place_order_response(
            service,
            {
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 50,
                "order_type": "LMT",
                "entry_price": 100,
                "take_profit_price": 104,
                "stop_loss_price": 98,
            },
        )

        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("buying_power_blocked", payload["error"])
        self.assertEqual("blocked", payload["buying_power_guard"]["state"])
        self.assertEqual([], service.conid_resolver.calls)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual("手动开仓已被购买力阈值拦截", service.pb.events[-1]["title"])

    def test_manual_order_warning_continues_and_surfaces_guard(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_snapshot = lambda _service: {
            "ok": True,
            "summary": {"buying_power": 30000, "net_liquidation": 100000},
        }
        order_actions._build_snapshot_action_response = lambda _service, action, result, delay_seconds=0, extra=None: (
            {"ok": bool(result.get("ok")), "action": action, "result": result, **dict(extra or {})},
            200,
        )

        payload, status = order_actions._build_ibkr_place_order_response(
            service,
            {
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 100,
                "order_type": "LMT",
                "entry_price": 100,
                "take_profit_price": 104,
                "stop_loss_price": 98,
            },
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("warning", payload["buying_power_guard"]["state"])
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertTrue(any(event["title"] == "手动开仓购买力预警" for event in service.pb.events))
        self.assertTrue(any(event["title"] == "手动开仓已提交" for event in service.pb.events))


if __name__ == "__main__":
    unittest.main()
