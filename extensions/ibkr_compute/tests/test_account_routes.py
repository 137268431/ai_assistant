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

if importlib.util.find_spec("flask") is None:
    fake_flask = types.ModuleType("flask")
    fake_flask.Response = type("Response", (), {})
    fake_flask.jsonify = lambda *args, **kwargs: args[0] if args else kwargs
    fake_flask.request = SimpleNamespace(args={})
    sys.modules["flask"] = fake_flask

from ibkr_compute.api.routes import account as account_routes


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

    def test_disabled_cache_forces_snapshot_refresh(self):
        self.assertTrue(self._bypass({"cache": "0"}))

    def test_browser_cache_bust_param_forces_snapshot_refresh(self):
        self.assertTrue(self._bypass({"_": "123"}))

    def test_default_allows_snapshot_cache(self):
        self.assertFalse(self._bypass({}))


if __name__ == "__main__":
    unittest.main()
