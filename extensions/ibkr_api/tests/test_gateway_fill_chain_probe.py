import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.validate import run_gateway_fill_chain_probe as probe  # noqa: E402


def _plan(
    *,
    symbol: str = "AAPL",
    direction: str = "long",
    entry: float = 100.0,
    take_profit: float = 102.0,
    stop_loss: float = 99.0,
) -> probe.FillChainPlan:
    return probe.FillChainPlan(
        symbol=symbol,
        direction=direction,
        quantity=1,
        target_notional=0.0,
        requested_exposure=entry,
        price=probe.PriceContext(
            symbol=symbol,
            bid=99.9,
            ask=100.0,
            last_price=99.95,
            reference_price=99.95,
            reference_source="mock",
            entry_reference_price=100.0,
            entry_reference_source="ask",
            entry_price=entry,
            take_profit_price=take_profit,
            stop_loss_price=stop_loss,
            conid=265598,
            quote_source="mock",
        ),
        ids=probe.ProbeIds(
            signal_id="SIG_AAPL_PROBE",
            trade_group_id="GRP_AAPL_PROBE",
            client_order_id="COID_AAPL_PROBE",
        ),
        outside_rth=True,
        tif="DAY",
    )


class GatewayFillChainProbeTest(unittest.TestCase):
    def test_marketable_price_uses_ask_bid_and_marks_reference_fallback(self):
        long_context = probe.build_price_context(
            symbol="AAPL",
            direction="long",
            quote={"symbol": "AAPL", "bid": 99.95, "ask": 100.0, "last_price": 99.98, "source": "mock_quote"},
            reference={"close": 99.98, "source": "mock_close"},
            entry_buffer_pct=0.10,
            tp_pct=2.0,
            sl_pct=1.0,
        )
        short_context = probe.build_price_context(
            symbol="MSFT",
            direction="short",
            quote={"symbol": "MSFT", "bid": 99.8, "ask": 100.0, "last_price": 99.9, "source": "mock_quote"},
            reference={"close": 99.9, "source": "mock_close"},
            entry_buffer_pct=0.10,
            tp_pct=2.0,
            sl_pct=1.0,
        )
        fallback_context = probe.build_price_context(
            symbol="TSLA",
            direction="long",
            quote={},
            reference={"close": 50.0, "source": "ibkr_bars_close"},
            entry_buffer_pct=0.10,
            tp_pct=2.0,
            sl_pct=1.0,
        )

        self.assertEqual(100.10, long_context.entry_price)
        self.assertEqual("ask", long_context.entry_reference_source)
        self.assertEqual(99.70, short_context.entry_price)
        self.assertEqual("bid", short_context.entry_reference_source)
        self.assertEqual(50.05, fallback_context.entry_price)
        self.assertEqual("reference_close_fallback_no_bid_ask", fallback_context.entry_reference_source)
        self.assertTrue(fallback_context.fallback_without_bid_ask)

    def test_place_payload_includes_outside_rth_tif_and_unique_ids(self):
        plan = _plan()

        payload = probe.build_place_payload(plan)

        self.assertEqual("LMT", payload["order_type"])
        self.assertTrue(payload["outside_rth"])
        self.assertTrue(payload["outsideRth"])
        self.assertEqual("DAY", payload["tif"])
        self.assertEqual("SIG_AAPL_PROBE", payload["signal_id"])
        self.assertEqual("GRP_AAPL_PROBE", payload["trade_group_id"])
        self.assertEqual("COID_AAPL_PROBE", payload["client_order_id"])
        self.assertEqual("COID_AAPL_PROBE", payload["order_ref"])
        self.assertEqual("GRP_AAPL_PROBE", payload["extra"]["trade_group_id"])
        self.assertEqual("COID_AAPL_PROBE", payload["order_extra"]["client_order_id"])

    def test_protection_prices_are_recomputed_from_actual_fill_price(self):
        long_tp, long_sl = probe.compute_protection_prices(101.0, "long", tp_pct=2.0, sl_pct=1.0)
        short_tp, short_sl = probe.compute_protection_prices(101.0, "short", tp_pct=2.0, sl_pct=1.0)

        self.assertEqual(103.02, long_tp)
        self.assertEqual(99.99, long_sl)
        self.assertEqual(98.98, short_tp)
        self.assertEqual(102.01, short_sl)

    def test_modify_payload_uses_actual_fill_repriced_tp_sl(self):
        class _Client:
            posts = []

            def __init__(self, *_args, **_kwargs):
                pass

            def post(self, path, payload, params):
                self.posts.append((path, dict(payload), dict(params)))
                return {"ok": True, "result": {"ok": True}}

        args = Namespace(api_base_url="http://api", http_timeout_sec=1.0, tp_pct=2.0, sl_pct=1.0)
        plan = _plan()
        place_response = {"ok": True, "result": {"order_ids": ["101", "102", "103"]}}
        snapshot_before = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "orders": [],
            "live_open_orders": [],
            "positions": [],
        }
        snapshot_after = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "orders": [
                {"order_id": "102", "symbol": "AAPL", "role": "take_profit", "price": 103.02},
                {"order_id": "103", "symbol": "AAPL", "role": "stop_loss", "auxPrice": 99.99},
            ],
            "live_open_orders": [],
            "positions": [],
        }

        with mock.patch.object(probe.fee_probe, "ApiClient", _Client), mock.patch.object(
            probe,
            "get_snapshot",
            side_effect=[snapshot_before, snapshot_after],
        ):
            result = probe.modify_protection_orders(args, plan, place_response, actual_fill_price=101.0)

        self.assertTrue(result["ok"])
        self.assertEqual(103.02, result["expected_new_tp"])
        self.assertEqual(99.99, result["expected_new_sl"])
        self.assertEqual(2, len(_Client.posts))
        self.assertEqual("/api/custom/ibkr/orders/modify", _Client.posts[0][0])
        self.assertEqual("take_profit", _Client.posts[0][1]["order_family_type"])
        self.assertEqual(103.02, _Client.posts[0][1]["price"])
        self.assertEqual("stop_loss", _Client.posts[1][1]["order_family_type"])
        self.assertEqual(99.99, _Client.posts[1][1]["price"])
        self.assertEqual("DAY", _Client.posts[1][1]["tif"])

    def test_entry_timeout_runs_cleanup_and_skips_modify(self):
        args = Namespace(
            dry_run=False,
            timeout_sec=0.01,
            poll_interval_sec=0.001,
            http_timeout_sec=1.0,
            cleanup_timeout_sec=1.0,
            tp_pct=2.0,
            sl_pct=1.0,
        )
        plan = _plan()
        place_result = {
            "ok": True,
            "response": {"ok": True, "result": {"order_ids": ["101", "102", "103"]}},
            "order_ids": ["101", "102", "103"],
        }

        with mock.patch.object(probe, "build_plan", return_value=plan), mock.patch.object(
            probe,
            "place_bracket_order",
            return_value=place_result,
        ), mock.patch.object(
            probe,
            "wait_for_entry_fill",
            return_value={"ok": False, "error": "entry_fill_timeout", "elapsed_s": 0.01},
        ), mock.patch.object(
            probe,
            "cleanup_symbol",
            return_value={"attempted": True, "flat": True, "reason": "entry_fill_timeout"},
        ) as cleanup, mock.patch.object(
            probe,
            "modify_protection_orders",
        ) as modify:
            result = probe.run_symbol_probe(args, "AAPL", 1)

        self.assertFalse(result["ok"])
        self.assertEqual("entry_fill_timeout", result["error"])
        cleanup.assert_called_once()
        self.assertEqual("entry_fill_timeout", cleanup.call_args.kwargs["reason"])
        modify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
