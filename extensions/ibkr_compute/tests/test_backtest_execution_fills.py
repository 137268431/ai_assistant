import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest.execution_fills import (
    build_calibrated_execution_cost_profile,
    fetch_execution_fills,
    normalize_execution_fills,
    parse_flex_xml_fills,
)
try:
    from ibkr_compute.api.ops import action_views
except ModuleNotFoundError:
    action_views = None


class BacktestExecutionFillCalibrationTests(unittest.TestCase):
    def test_parse_flex_xml_normalizes_commission_and_time(self):
        xml = """
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement accountId="DU123">
              <Trades>
                <Trade execID="0001" orderID="42" symbol="AAPL" buySell="BUY"
                       quantity="100" tradePrice="25.5" ibCommission="-1"
                       ibCommissionCurrency="USD" tradeDate="20260401" tradeTime="09:35:02" />
              </Trades>
            </FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>
        """

        fills = parse_flex_xml_fills(xml, environment="paper", account="DU123")

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["exec_id"], "0001")
        self.assertEqual(fills[0]["symbol"], "AAPL")
        self.assertEqual(fills[0]["side"], "buy")
        self.assertEqual(fills[0]["commission"], 1.0)
        self.assertGreater(fills[0]["trade_time_ms"], 0)

    def test_recent_fill_shape_normalizes_commission(self):
        fills = normalize_execution_fills(
            [
                {
                    "orderId": "99",
                    "ticker": "NVDA",
                    "side": "SLD",
                    "filledQuantity": 20,
                    "avgPrice": 900,
                    "commission": 1.25,
                    "lastExecutionTime": "20260401 15:59:01",
                }
            ],
            environment="live",
            source="recent_fills",
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["side"], "sell")
        self.assertAlmostEqual(fills[0]["shares"], 20.0)
        self.assertAlmostEqual(fills[0]["price"], 900.0)
        self.assertAlmostEqual(fills[0]["commission"], 1.25)

    def test_recent_fill_runtime_fetch_forces_broker_executions(self):
        if action_views is None:
            self.skipTest("flask dependency unavailable")
        calls = []

        class _Response:
            ok = True
            content = b"{}"
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "raw": {"executions": []}, "executions_requested": True}

        def fake_get(url, params=None, timeout=None):
            calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
            return _Response()

        with mock.patch.object(action_views, "get_runtime_internal_url", return_value="http://runtime"):
            with mock.patch.object(action_views.requests, "get", side_effect=fake_get):
                payload = action_views._fetch_recent_fills_from_runtime("paper", 1)

        self.assertTrue(payload["ok"])
        self.assertEqual("true", calls[0]["params"]["broker_force"])
        self.assertEqual("paper", calls[0]["params"]["broker_mode"])

    def test_recent_fill_local_tracker_requests_execution_rows(self):
        if action_views is None:
            self.skipTest("flask dependency unavailable")
        class _Tracker:
            def __init__(self):
                self.calls = []

            def get_broker_order_history(self, **kwargs):
                self.calls.append(dict(kwargs))
                return {"ok": True, "executions_requested": bool(kwargs.get("include_executions"))}

        class _App:
            def __init__(self):
                self.tracker = _Tracker()

            def get_ibkr_service(self):
                return type("_Service", (), {"order_tracker": self.tracker})()

        app = _App()
        payload, source = action_views._load_recent_fill_broker_payload(app, "paper", 1)

        self.assertEqual("local_order_tracker", source)
        self.assertTrue(payload["executions_requested"])
        self.assertEqual([{"days": 1, "force": True, "include_executions": True}], app.tracker.calls)

    def test_calibrated_profile_infers_per_share_and_minimum(self):
        fills = normalize_execution_fills(
            [
                {"execId": "small", "symbol": "AAPL", "side": "BUY", "shares": 100, "price": 25, "commission": 1.0},
                {"execId": "large1", "symbol": "AAPL", "side": "BUY", "shares": 1000, "price": 25, "commission": 5.0},
                {"execId": "large2", "symbol": "MSFT", "side": "SELL", "shares": 2000, "price": 50, "commission": 10.0},
            ],
            environment="live",
            source="test",
        )

        payload = build_calibrated_execution_cost_profile(fills, environment="live")
        profile = payload["profile"]

        self.assertTrue(payload["profile_available"])
        self.assertEqual(profile["fee_model"], "calibrated_v1")
        self.assertAlmostEqual(profile["calibrated_per_share"], 0.005)
        self.assertAlmostEqual(profile["calibrated_min_commission"], 1.0)
        self.assertEqual(payload["backtest_payload_patch"]["fee_model"], "calibrated_v1")

    def test_fetch_execution_fills_reads_sqlite_collection(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            conn.execute(
                """
                CREATE TABLE ibkr_execution_fills (
                    id TEXT,
                    exec_id TEXT,
                    order_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    shares REAL,
                    price REAL,
                    trade_value REAL,
                    commission REAL,
                    commission_currency TEXT,
                    currency TEXT,
                    trade_time TEXT,
                    trade_time_ms INTEGER,
                    account TEXT,
                    source TEXT,
                    environment TEXT,
                    asset_category TEXT,
                    exchange TEXT,
                    order_type TEXT,
                    reference_price REAL,
                    slippage_bps REAL,
                    raw TEXT,
                    created TEXT,
                    updated TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO ibkr_execution_fills VALUES (
                    'id1', 'exec1', 'order1', 'AAPL', 'buy', 100, 25, 2500,
                    1, 'USD', 'USD', '2026-04-01 09:35:00', 1775050500000,
                    'DU123', 'flex', 'live', 'STK', 'SMART', 'LMT', 0, 0,
                    '{"source":"fixture"}', 'now', 'now'
                )
                """
            )
            conn.commit()
            conn.close()
            with mock.patch.dict(os.environ, {"PB_SQLITE_PATH": tmp.name}):
                with mock.patch("ibkr_compute.backtest.execution_fills.open_pb_sqlite") as open_sqlite:
                    def _open(*, readonly=False, timeout=10.0):
                        db = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
                        db.row_factory = sqlite3.Row
                        return db

                    open_sqlite.side_effect = _open
                    payload = fetch_execution_fills(environment="live", symbols=["AAPL"], account="DU123")

        self.assertTrue(payload["available"])
        self.assertEqual(payload["summary"]["fill_count"], 1)
        self.assertEqual(payload["items"][0]["raw"]["source"], "fixture")


if __name__ == "__main__":
    unittest.main()
