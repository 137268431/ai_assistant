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
from ibkr_compute.api.account.snapshot_builder.payload import (
    _build_ibkr_account_buying_power_snapshot,
    _build_ibkr_account_snapshot,
    refresh_account_snapshot_cache,
)


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


class _SnapshotOrderPlacer:
    def get_active_account_id(self, use_paper=False):
        return "DU123"


class _SnapshotLifecycle:
    account_id = "DU123"

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


class _DefaultConfig:
    def get_for_environment(self, key, _environment, default=None):
        return default

    def get_bool_for_environment(self, _key, _environment, default=False):
        return default

    def get_float_for_environment(self, _key, _environment, default=0.0):
        return default


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

    def status(self):
        return {
            "gateway": {"running": True, "reachable": True},
            "session": {"authenticated": True},
            "account_data_circuit": self._circuit,
        }


def _with_fake_api_app(app, fn):
    with mock.patch("ibkr_compute.api.account.snapshot_builder.context._api_app", return_value=app):
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

    def test_buying_power_reuses_fresh_full_snapshot_cache(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle)

        full_snapshot = _with_fake_api_app(app, lambda: _build_ibkr_account_snapshot(service, include_pnl=False))
        buying_power = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertTrue(full_snapshot["summary_available"])
        self.assertEqual("account_snapshot", buying_power["source"])
        self.assertEqual(1, lifecycle.snapshot_calls)
        self.assertEqual(0, lifecycle.summary_calls)
        self.assertEqual("ok", buying_power["account_snapshot_health"]["state"])

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
        self.assertTrue(all(payload["source"] == "account_snapshot" for payload in payloads))
        self.assertTrue(all(payload["buying_power_guard"]["state"] == "ok" for payload in payloads))

    def test_buying_power_snapshot_short_circuits_when_account_data_circuit_open(self):
        app = _FakeApiApp()
        lifecycle = _BuyingPowerLifecycle()
        service = _BuyingPowerService(lifecycle, circuit={"active": True, "reason": "positions_timeout", "remaining_s": 42.0})

        payload = _with_fake_api_app(app, lambda: _build_ibkr_account_buying_power_snapshot(service))

        self.assertFalse(payload["ok"])
        self.assertEqual("account_data_circuit_open", payload["buying_power_guard"]["reason"])
        self.assertEqual(42.0, payload["retry_after_s"])
        self.assertEqual(0, lifecycle.summary_calls)


if __name__ == "__main__":
    unittest.main()
