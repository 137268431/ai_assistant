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
    fake_flask.Response = object
    fake_flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    fake_flask.request = types.SimpleNamespace(
        headers={},
        method="GET",
        args={},
        get_data=lambda: b"",
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
