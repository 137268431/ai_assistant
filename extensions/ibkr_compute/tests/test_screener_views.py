import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs


class _FakeArgs:
    def __init__(self, query: str = ""):
        self._values = parse_qs(query, keep_blank_values=True)

    def get(self, name, default=None):
        values = self._values.get(name)
        return values[0] if values else default

    def getlist(self, name):
        return list(self._values.get(name, []))


class _FakeRequest:
    args = _FakeArgs()

    @staticmethod
    def get_json(silent=True):
        return {}


class _FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self):
        return self._payload


if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.jsonify = lambda payload: _FakeJsonResponse(payload)
    flask_stub.request = _FakeRequest()
    sys.modules["flask"] = flask_stub


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from flask import request
from ibkr_compute.api.market import screener_views
from ibkr_compute.api.market.screener import payload as screener_payload


def _json(response):
    return response.get_json() if hasattr(response, "get_json") else response


class ScreenerViewsTest(unittest.TestCase):
    def setUp(self):
        request.args = _FakeArgs()
        screener_views.jsonify = lambda payload: _FakeJsonResponse(payload)
        self.fake_app = SimpleNamespace(
            SUPPORTED_COMPUTE_ENVIRONMENTS={"live", "paper", "backtest"},
            current_market_date=lambda: "2026-04-24",
            normalize_symbols=lambda symbols: [str(symbol).upper() for symbol in symbols],
        )

    def test_build_screener_response_accepts_valid_market_date(self):
        request.args = _FakeArgs("environment=live&market_date=2026-04-24&symbols=aapl,msft&limit=2")
        expected_payload = {"ok": True, "environment": "live", "market_date": "2026-04-24", "items": []}

        with mock.patch.object(screener_views, "get_app_module", return_value=self.fake_app):
            with mock.patch.object(screener_views, "build_screener_payload", return_value=expected_payload) as build_payload:
                response = screener_views.build_screener_response()

        self.assertEqual(_json(response), expected_payload)
        build_payload.assert_called_once_with(
            environment="live",
            market_date="2026-04-24",
            symbols=["AAPL", "MSFT"],
            limit=2,
        )

    def test_build_screener_response_rejects_invalid_market_date(self):
        request.args = _FakeArgs("environment=live&market_date=2026-02-99")

        with mock.patch.object(screener_views, "get_app_module", return_value=self.fake_app):
            with mock.patch.object(screener_views, "build_screener_payload") as build_payload:
                response, status = screener_views.build_screener_response()

        self.assertEqual(status, 400)
        self.assertEqual(_json(response)["error"], "invalid_market_date")
        build_payload.assert_not_called()


class ScreenerPayloadHelpersTest(unittest.TestCase):
    def test_build_daily_change_fields_matches_cached_daily_formula(self):
        history = [
            {"close": 100},
            {"close": 110},
            {"close": 120},
            {"close": 130},
            {"close": 140},
        ]

        fields = screener_payload._build_daily_change_fields(154, history)

        self.assertEqual(fields["day_change_pct"], 10.0)
        self.assertEqual(fields["prev_close_change_pct"], 7.69)
        self.assertEqual(fields["change_7d"], 54.0)


if __name__ == "__main__":
    unittest.main()
