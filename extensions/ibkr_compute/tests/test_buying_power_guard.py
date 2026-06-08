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
import ibkr_compute.api.account.snapshot_builder.payload as snapshot_payload  # noqa: E402


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


class FakePaperSnapshotApiApp:
    pass


class FakePaperLifecycle:
    def __init__(self, account_summary=None, positions=None, entry_orders=None):
        self.account_summary = dict(account_summary or {})
        self.positions = list(positions or [])
        self.entry_orders = list(entry_orders or [])
        self.summary_calls = 0

    def get_account_summary(self, _account_id):
        self.summary_calls += 1
        return dict(self.account_summary)

    def get_positions(self):
        return list(self.positions)

    def strategy_open_entry_orders(self, order_tracker=None):
        return list(self.entry_orders)


class FakePaperSnapshotService:
    is_running = True
    is_starting = False

    def __init__(self, config=None, account_summary=None, positions=None, entry_orders=None):
        self.config = config or FakeConfig()
        self.order_lifecycle = FakePaperLifecycle(
            account_summary=account_summary,
            positions=positions,
            entry_orders=entry_orders,
        )
        self.order_tracker = object()


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

    def test_guard_unavailable_when_buying_power_missing(self):
        guard = build_buying_power_guard(
            {"net_liquidation": 100000},
            requested_exposure=3000,
        )

        self.assertEqual("unavailable", guard["state"])
        self.assertFalse(guard["available"])
        self.assertEqual("buying_power_unavailable", guard["reason"])
        self.assertIsNone(guard["remaining"])
        self.assertIsNone(guard["remaining_after"])

    def test_guard_unavailable_when_snapshot_summary_is_empty(self):
        guard = build_buying_power_guard({}, requested_exposure=3000)

        self.assertEqual("unavailable", guard["state"])
        self.assertFalse(guard["available"])
        self.assertEqual("account_snapshot_unavailable", guard["reason"])
        self.assertIsNone(guard["remaining"])
        self.assertIsNone(guard["remaining_after"])

    def test_guard_unavailable_for_synthetic_zero_snapshot_summary(self):
        guard = build_buying_power_guard(
            {
                "account_code": "DU123",
                "account_type": "",
                "net_liquidation": 0,
                "available_funds": 0,
                "buying_power": 0,
                "excess_liquidity": 0,
                "equity_with_loan": 0,
                "gross_position_value": 0,
                "total_cash_value": 0,
                "initial_margin": 0,
                "maintenance_margin": 0,
            },
            requested_exposure=3000,
        )

        self.assertEqual("unavailable", guard["state"])
        self.assertFalse(guard["available"])
        self.assertEqual("account_snapshot_unavailable", guard["reason"])
        self.assertIsNone(guard["remaining"])
        self.assertIsNone(guard["remaining_after"])

    def test_guard_treats_explicit_zero_buying_power_as_real_balance(self):
        guard = build_buying_power_guard(
            {"buying_power": 0, "net_liquidation": 100000},
            requested_exposure=0,
        )

        self.assertTrue(guard["available"])
        self.assertEqual(0.0, guard["remaining"])
        self.assertEqual("blocked", guard["state"])

    def test_estimate_entry_exposure_uses_market_order_conservative_price(self):
        exposure = estimate_entry_exposure(10, None, 104, 98, "long", "MKT")

        self.assertEqual(1040, exposure)


class PaperAccountBuyingPowerSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.old_context = snapshot_payload.build_snapshot_context
        self.api_app = FakePaperSnapshotApiApp()
        snapshot_payload.build_snapshot_context = lambda service, include_pnl=False, **_kwargs: {
            "api_app": self.api_app,
            "runtime_environment": "paper",
            "service_status": {
                "session": {"authenticated": True},
                "gateway": {"running": True},
            },
            "account_id": "DU-PAPER",
            "include_pnl": bool(include_pnl),
            "cache_key": ("paper", "DU-PAPER", bool(include_pnl)),
        }

    def tearDown(self):
        snapshot_payload.build_snapshot_context = self.old_context

    def test_paper_mode_uses_account_summary_even_when_legacy_paper_config_exists(self):
        service = FakePaperSnapshotService(
            config=FakeConfig(
                {
                    "ibkr_buying_power_guard_paper_source": "config",
                    "ibkr_paper_risk_buying_power_usd": "194388.61",
                    "ibkr_paper_risk_net_liquidation_usd": "61398",
                    "ibkr_paper_risk_default_entry_exposure_usd": "5000",
                }
            ),
            account_summary={
                "AccountCode": {"value": "DU-PAPER", "currency": "USD"},
                "NetLiquidation": {"value": "61398", "currency": "USD"},
                "BuyingPower": {"value": "50000", "currency": "USD"},
                "AvailableFunds": {"value": "25000", "currency": "USD"},
                "ExcessLiquidity": {"value": "20000", "currency": "USD"},
            },
            positions=[{"ticker": "AAPL", "position": 50, "mktPrice": 100}],
            entry_orders=[{"symbol": "MSFT", "remainingQuantity": 25, "price": 200}],
        )

        snapshot = snapshot_payload._build_ibkr_account_buying_power_snapshot(service)

        self.assertTrue(snapshot["ok"])
        self.assertEqual("account_summary", snapshot["source"])
        self.assertAlmostEqual(50000.0, snapshot["summary"]["buying_power"])
        self.assertAlmostEqual(61398.0, snapshot["buying_power_guard"]["net_liquidation"])
        self.assertNotIn("configured_buying_power", snapshot["buying_power_guard"])
        self.assertNotIn("risk_model_used_exposure", snapshot["buying_power_guard"])

    def test_paper_mode_fails_closed_when_account_summary_is_missing(self):
        service = FakePaperSnapshotService(config=FakeConfig({"ibkr_buying_power_guard_paper_source": "config"}))

        snapshot = snapshot_payload._build_ibkr_account_buying_power_snapshot(service)

        self.assertFalse(snapshot["ok"])
        self.assertEqual("unavailable", snapshot["buying_power_guard"]["state"])
        self.assertEqual("account_snapshot_unavailable", snapshot["buying_power_guard"]["reason"])

    def test_default_thresholds_warn_and_block_against_account_buying_power(self):
        config = FakeConfig()
        warning_service = FakePaperSnapshotService(
            config=config,
            account_summary={
                "AccountCode": {"value": "DU-PAPER"},
                "NetLiquidation": {"value": "100000"},
                "BuyingPower": {"value": "29000"},
            },
        )
        warning_snapshot = snapshot_payload._build_ibkr_account_buying_power_snapshot(warning_service)
        warning_guard = build_buying_power_guard(
            warning_snapshot["summary"],
            config=config,
            environment="paper",
            requested_exposure=5000,
        )
        self.assertEqual("warning", warning_guard["state"])

        self.api_app = FakePaperSnapshotApiApp()
        blocked_service = FakePaperSnapshotService(
            config=config,
            account_summary={
                "AccountCode": {"value": "DU-PAPER"},
                "NetLiquidation": {"value": "100000"},
                "BuyingPower": {"value": "12000"},
            },
        )
        blocked_snapshot = snapshot_payload._build_ibkr_account_buying_power_snapshot(blocked_service)
        blocked_guard = build_buying_power_guard(
            blocked_snapshot["summary"],
            config=config,
            environment="paper",
            requested_exposure=5000,
        )
        self.assertEqual("blocked", blocked_guard["state"])


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


class FakeSessionKeeper:
    is_authenticated = True


class FakeOrderService:
    is_running = True
    is_starting = False

    def __init__(self, config=None):
        self.config = config or FakeConfig()
        self.conid_resolver = FakeConidResolver()
        self.order_tracker = FakeOrderTracker()
        self.order_placer = FakeOrderPlacer()
        self.pb = FakePB()
        self.session_keeper = FakeSessionKeeper()

    def status(self):
        return {"session": {"authenticated": True}}


class BuyingPowerManualOrderActionTest(unittest.TestCase):
    def setUp(self):
        self.old_api_app = order_actions._api_app
        self.old_coerce_float = order_actions._app_coerce_float
        self.old_snapshot = order_actions._build_ibkr_account_snapshot
        self.old_bp_snapshot = order_actions._build_ibkr_account_buying_power_snapshot
        self.old_action_response = order_actions._build_snapshot_action_response
        order_actions._api_app = lambda: FakeApiApp()
        order_actions._app_coerce_float = lambda value, default=None: (
            default if value in (None, "") else float(value)
        )

    def tearDown(self):
        order_actions._api_app = self.old_api_app
        order_actions._app_coerce_float = self.old_coerce_float
        order_actions._build_ibkr_account_snapshot = self.old_snapshot
        order_actions._build_ibkr_account_buying_power_snapshot = self.old_bp_snapshot
        order_actions._build_snapshot_action_response = self.old_action_response

    def test_manual_order_blocked_when_remaining_buying_power_would_cross_block_floor(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_buying_power_snapshot = lambda _service: {
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
        self.assertEqual("手动开仓已被动态购买力上限拦截", service.pb.events[-1]["title"])

    def test_manual_order_warning_continues_and_surfaces_guard(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_buying_power_snapshot = lambda _service: {
            "ok": True,
            "summary": {"buying_power": 30000, "net_liquidation": 100000},
        }
        order_actions._build_snapshot_action_response = lambda _service, action, result, delay_seconds=0, extra=None, **_kwargs: (
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

    def test_manual_order_passes_tracking_tif_and_outside_rth_to_order_placer(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_buying_power_snapshot = lambda _service: {
            "ok": True,
            "summary": {"buying_power": 100000, "net_liquidation": 120000},
        }
        order_actions._build_snapshot_action_response = lambda _service, action, result, delay_seconds=0, extra=None, **_kwargs: (
            {"ok": bool(result.get("ok")), "action": action, "result": result, **dict(extra or {})},
            200,
        )

        payload, status = order_actions._build_ibkr_place_order_response(
            service,
            {
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 10,
                "order_type": "LMT",
                "entry_price": 100,
                "take_profit_price": 104,
                "stop_loss_price": 98,
                "signal_id": "probe-signal-1",
                "trade_group_id": "probe-group-1",
                "tif": "day",
                "outsideRth": True,
                "extra": {"probe": True},
            },
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(service.order_placer.calls))
        call = service.order_placer.calls[0]
        self.assertEqual("probe-signal-1", call["signal_id"])
        self.assertEqual("probe-group-1", call["trade_group_id"])
        self.assertEqual("DAY", call["tif"])
        self.assertTrue(call["outside_rth"])
        self.assertEqual("probe-group-1", call["order_extra"]["client_order_id"])
        self.assertTrue(call["order_extra"]["probe"])

    def test_manual_order_pauses_when_buying_power_snapshot_unavailable(self):
        service = FakeOrderService()
        order_actions._build_ibkr_account_buying_power_snapshot = lambda _service: {
            "ok": False,
            "summary": {},
            "buying_power_guard": {
                "state": "unavailable",
                "reason": "gateway_unavailable",
                "available": False,
            },
            "errors": {"summary": "gateway_unavailable"},
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

        self.assertEqual(503, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("buying_power_unavailable", payload["error"])
        self.assertEqual("unavailable", payload["buying_power_guard"]["state"])
        self.assertEqual("gateway_unavailable", payload["buying_power_guard"]["reason"])
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual("手动开仓暂停：购买力风控不可用", service.pb.events[-1]["title"])


if __name__ == "__main__":
    unittest.main()
