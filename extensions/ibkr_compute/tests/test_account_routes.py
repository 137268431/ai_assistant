import sys
import types
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    _HAS_FLASK = importlib.util.find_spec("flask") is not None
except ValueError:
    _HAS_FLASK = False

if not _HAS_FLASK:
    fake_flask = types.ModuleType("flask")
    fake_flask.Response = type("Response", (), {})
    fake_flask.jsonify = lambda *args, **kwargs: args[0] if args else kwargs
    fake_flask.request = SimpleNamespace(args={})
    sys.modules["flask"] = fake_flask

from ibkr_compute.api.routes import account as account_routes
from ibkr_compute.api.shared.route_request import coerce_request_bool


class _Args(dict):
    def get(self, name, default=None):
        return super().get(name, default)


class AccountRouteCacheBypassTest(unittest.TestCase):
    def _bypass(self, args, *, cache_bust=False):
        with (
            mock.patch.object(account_routes, "request", SimpleNamespace(args=_Args(args))),
            mock.patch.object(account_routes, "get_query_arg_bool", return_value=cache_bust),
        ):
            return account_routes._account_snapshot_cache_bypass()

    def test_cache_bust_forces_snapshot_refresh(self):
        self.assertTrue(self._bypass({"cache_bust": "123"}, cache_bust=True))

    def test_numeric_cache_bust_bool_is_true(self):
        self.assertTrue(coerce_request_bool("1780805505054", False))

    def test_disabled_cache_forces_snapshot_refresh(self):
        self.assertTrue(self._bypass({"cache": "0"}))

    def test_browser_cache_bust_param_forces_snapshot_refresh(self):
        self.assertTrue(self._bypass({"_": "123"}))

    def test_default_allows_snapshot_cache(self):
        self.assertFalse(self._bypass({}))


class AccountRouteOrdersFastTest(unittest.TestCase):
    def _orders_fast(self, args, *, bool_value=False, default=False):
        with (
            mock.patch.object(account_routes, "request", SimpleNamespace(args=_Args(args))),
            mock.patch.object(account_routes, "get_query_arg_bool", return_value=bool_value),
        ):
            return account_routes._account_snapshot_orders_fast(default=default)

    def test_snapshot_profile_enables_orders_fast(self):
        self.assertTrue(self._orders_fast({"snapshot_profile": "orders_fast"}))

    def test_query_bool_enables_orders_fast(self):
        self.assertTrue(self._orders_fast({"orders_fast": "1"}, bool_value=True))

    def test_default_can_enable_orders_fast_for_live_orders_route(self):
        self.assertTrue(self._orders_fast({}, default=True))


class AccountRouteMetricsTest(unittest.TestCase):
    def test_record_account_snapshot_metrics_is_best_effort(self):
        payload = {"ok": True, "environment": "paper"}
        with mock.patch("ibkr_compute.observability.prometheus.set_account_snapshot_metrics") as recorder:
            account_routes._record_account_snapshot_metrics(payload)
        recorder.assert_called_once_with(payload, source="account_snapshot")

        with mock.patch(
            "ibkr_compute.observability.prometheus.set_account_snapshot_metrics",
            side_effect=RuntimeError("metrics unavailable"),
        ):
            account_routes._record_account_snapshot_metrics(payload)


if __name__ == "__main__":
    unittest.main()
