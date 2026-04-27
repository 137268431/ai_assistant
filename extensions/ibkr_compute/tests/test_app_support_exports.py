import sys
import unittest
from pathlib import Path
import types

SRC_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name, *args, **kwargs):
            self.name = name

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

        def add_url_rule(self, *args, **kwargs):
            return None

    def fake_jsonify(*args, **kwargs):
        if len(args) == 1 and not kwargs:
            return args[0]
        if args and kwargs:
            return {"args": args, **kwargs}
        if args:
            return {"args": args}
        return kwargs

    fake_flask.Flask = _FakeFlask
    fake_flask.Response = object
    fake_flask.jsonify = fake_jsonify
    fake_flask.redirect = lambda url, code=302: {"redirect": url, "code": code}
    fake_flask.request = types.SimpleNamespace(
        headers={},
        method="GET",
        args={},
        get_data=lambda: b"",
        get_json=lambda silent=True: {},
    )
    sys.modules["flask"] = fake_flask

from ibkr_compute.api.app_core.exports_support import SUPPORT_EXPORTS
from ibkr_compute.market.timeframe_utils import (
    classify_session,
    interval_to_chart_tf,
    interval_to_ms,
    ms_to_et,
)


class AppSupportExportsTest(unittest.TestCase):
    def test_timeframe_helpers_are_exported_for_api_app_consumers(self):
        self.assertIs(SUPPORT_EXPORTS["classify_session"], classify_session)
        self.assertIs(SUPPORT_EXPORTS["interval_to_chart_tf"], interval_to_chart_tf)
        self.assertIs(SUPPORT_EXPORTS["interval_to_ms"], interval_to_ms)
        self.assertIs(SUPPORT_EXPORTS["ms_to_et"], ms_to_et)


if __name__ == "__main__":
    unittest.main()
