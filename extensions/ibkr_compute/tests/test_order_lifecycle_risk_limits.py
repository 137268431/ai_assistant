import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order.order_lifecycle import OrderLifecycle


class _FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, environment, default):
        return self.values.get(key, default)

    def get_int_for_environment(self, key, environment, default):
        return int(self.values.get(key, default))

    def get_float_for_environment(self, key, environment, default):
        return float(self.values.get(key, default))

    def get_bool_for_environment(self, key, environment, default):
        value = self.values.get(key, default)
        return str(value).lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)


class _FakeBroker:
    def __init__(self, positions=None):
        self.positions = list(positions or [])

    def list_positions(self):
        return list(self.positions)


class _FakeOrderTracker:
    def __init__(self, orders=None):
        self.orders = list(orders or [])

    def get_live_orders(self):
        return list(self.orders)


class _FakePB:
    def __init__(self, orders=None, snapshot=None):
        self.orders = list(orders or [])
        self.snapshot = dict(snapshot or {})
        self.upserts = []
        self.events = []

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        if collection == "orders":
            return [dict(item) for item in self.orders]
        return []

    def get_first_record(self, collection, filter=None, sort=None):
        if collection in {"ibkr_indicators", "ibkr_bars"}:
            return dict(self.snapshot)
        return {}

    def upsert_order(self, payload):
        self.upserts.append(dict(payload))
        for idx, row in enumerate(self.orders):
            if str(row.get("id") or "") == str(payload.get("id") or ""):
                merged = dict(row)
                merged.update(dict(payload))
                self.orders[idx] = merged
                return {"ok": True, "id": row.get("id")}
        return {"ok": True}

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": dict(detail or {}), **dict(kwargs or {})})
        return {"ok": True}


class _FakeOrderModifier:
    def __init__(self):
        self.modifications = []
        self.cancellations = []

    def modify_order(self, order_id, updates, acct_id=None):
        self.modifications.append((str(order_id), dict(updates or {})))
        return {"ok": True, "order_id": str(order_id), "updates": dict(updates or {})}

    def update_stop_loss(self, order_id, new_sl_price, acct_id=None):
        return self.modify_order(order_id, {"auxPrice": float(new_sl_price)}, acct_id)

    def cancel_order(self, order_id, acct_id=None):
        self.cancellations.append(str(order_id))
        return {"ok": True, "order_id": str(order_id)}


class _FakeOrderPlacer:
    def __init__(self):
        self.brackets = []

    def place_bracket_order(self, **kwargs):
        self.brackets.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": ["201", "202", "203"],
            "bracket_group": f"{kwargs.get('symbol')}_reentry",
        }


class OrderLifecycleRiskLimitTests(unittest.TestCase):
    def _partial_harvest_rows(self, *, tp_status="Filled", sl_status="Submitted", handled=False):
        base_extra = {
            "harvest_managed": True,
            "harvest_profile": "intraday_volatility_harvest_v1",
            "harvest_lot": "primary",
            "partial_harvest_managed": True,
            "partial_tp_quantity": 30,
            "reentry_allowed": True,
            "harvest_state": {"partial_exited": handled, "cycles": 1 if handled else 0},
        }
        if handled:
            base_extra.update({
                "partial_tp_handled": True,
                "partial_tp_filled_quantity": 30,
                "remaining_after_partial_tp": 70,
            })
        return [
            {
                "id": "entry",
                "symbol": "AAPL",
                "role": "entry",
                "status": "Filled",
                "quantity": 100,
                "filled_qty": 100,
                "fill_price": 100.0,
                "limit_price": 100.0,
                "broker_order_id": "101",
                "trade_group_id": "AAPL_long_harvest",
                "entry_order_unique_id": "entry_AAPL_long_harvest",
                "unique_id": "entry_AAPL_long_harvest",
                "signal_id": "sig-aapl",
                "bar_time_ms": 1,
                "extra": dict(base_extra),
            },
            {
                "id": "tp",
                "symbol": "AAPL",
                "role": "take_profit",
                "status": tp_status,
                "quantity": 30,
                "filled_qty": 30 if tp_status == "Filled" else 0,
                "limit_price": 106.0,
                "broker_order_id": "102",
                "trade_group_id": "AAPL_long_harvest",
                "entry_order_unique_id": "entry_AAPL_long_harvest",
                "parent_order_unique_id": "entry_AAPL_long_harvest",
                "unique_id": "tp_AAPL_long_harvest",
                "bar_time_ms": 1,
                "extra": dict(base_extra),
            },
            {
                "id": "sl",
                "symbol": "AAPL",
                "role": "stop_loss",
                "status": sl_status,
                "quantity": 100,
                "filled_qty": 100 if sl_status == "Filled" else 0,
                "limit_price": 98.0,
                "broker_order_id": "103",
                "trade_group_id": "AAPL_long_harvest",
                "entry_order_unique_id": "entry_AAPL_long_harvest",
                "parent_order_unique_id": "entry_AAPL_long_harvest",
                "unique_id": "sl_AAPL_long_harvest",
                "bar_time_ms": 1,
                "extra": dict(base_extra),
            },
        ]

    def test_zero_position_limit_disables_daily_trade_count_cap(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"position_limit_max": 0}))

        for _ in range(25):
            lifecycle.increment_position_count()

        self.assertFalse(lifecycle.is_position_limit_reached)
        self.assertEqual(lifecycle.status()["position_limit_max"], 0)

    def test_positive_position_limit_still_blocks_after_count_reached(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"position_limit_max": 2}))

        lifecycle.increment_position_count()
        self.assertFalse(lifecycle.is_position_limit_reached)
        lifecycle.increment_position_count()

        self.assertTrue(lifecycle.is_position_limit_reached)

    def test_stop_loss_breaker_uses_consecutive_count(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"consecutive_stop_loss_limit": 3}))

        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()
        self.assertFalse(lifecycle.is_sl_circuit_breaker)
        lifecycle.reset_sl_count()
        self.assertFalse(lifecycle.is_sl_circuit_breaker)
        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()

        self.assertTrue(lifecycle.is_sl_circuit_breaker)

    def test_fixed_position_symbols_default_to_boxx_ibkr(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({}))

        self.assertTrue(lifecycle.is_fixed_position_symbol("BOXX"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("ibkr"))
        self.assertEqual(["BOXX", "IBKR"], lifecycle.status()["fixed_position_symbols"])

    def test_fixed_position_symbols_can_fallback_to_eod_keep_symbols(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"eod_keep_symbols": "SGOV, BIL"}))

        self.assertTrue(lifecycle.is_fixed_position_symbol("SGOV"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("bil"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("BOXX"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("IBKR"))

    def test_detects_filled_position_without_active_protection_and_alerts(self):
        pb = _FakePB(
            orders=[
                {
                    "id": "entry",
                    "symbol": "NFLX",
                    "role": "entry",
                    "status": "Filled",
                    "quantity": 114,
                    "filled_qty": 114,
                    "fill_price": 88.01877,
                    "broker_order_id": "84",
                    "trade_group_id": "NFLX_short_20260527_105036_harvest",
                    "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                    "unique_id": "entry_NFLX_short_20260527_105036_harvest",
                    "signal_id": "NFLX_20260527_1045_mr_U",
                    "environment": "paper",
                    "extra": {"reason": "order_submitted_by_ibkr_compute"},
                },
                {
                    "id": "tp",
                    "symbol": "NFLX",
                    "role": "take_profit",
                    "status": "Canceled",
                    "quantity": 114,
                    "filled_qty": 0,
                    "limit_price": 86.69,
                    "broker_order_id": "85",
                    "trade_group_id": "NFLX_short_20260527_105036_harvest",
                    "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                    "unique_id": "tp_NFLX_short_20260527_105036_harvest",
                    "environment": "paper",
                    "extra": {"broker_last_error": {"code": 201, "message": "Invalid Price"}},
                },
                {
                    "id": "sl",
                    "symbol": "NFLX",
                    "role": "stop_loss",
                    "status": "Canceled",
                    "quantity": 114,
                    "filled_qty": 0,
                    "limit_price": 88.90,
                    "broker_order_id": "86",
                    "trade_group_id": "NFLX_short_20260527_105036_harvest",
                    "entry_order_unique_id": "entry_NFLX_short_20260527_105036_harvest",
                    "unique_id": "sl_NFLX_short_20260527_105036_harvest",
                    "environment": "paper",
                    "extra": {"reason": "Order Canceled"},
                },
            ]
        )
        lifecycle = OrderLifecycle(pb_client=pb, environment="paper", config=_FakeConfig({}))

        issues = lifecycle._detect_missing_protection_after_fill(
            [{"ticker": "NFLX", "position": -114, "conid": 123}],
            open_orders=[],
        )

        self.assertEqual(1, len(issues))
        self.assertEqual(["take_profit", "stop_loss"], issues[0]["missing_roles"])
        entry_patch = next(item for item in pb.upserts if item["id"] == "entry")
        extra = entry_patch["extra"]
        self.assertEqual("missing_after_fill", extra["protection_state"])
        self.assertFalse(extra["protection_complete"])
        self.assertTrue(extra["protection_incomplete"])
        self.assertEqual(["take_profit", "stop_loss"], extra["missing_protection_roles"])
        self.assertEqual(-114, extra["broker_position_quantity"])
        self.assertEqual("成交后保护单缺失", pb.events[0]["title"])
        self.assertEqual("error", pb.events[0]["level"])
        self.assertIn("止盈/止损", pb.events[0]["detail"]["缺失保护"])

    def test_strategy_capacity_excludes_fixed_positions_and_counts_open_entries(self):
        lifecycle = OrderLifecycle(
            config=_FakeConfig({
                "max_strategy_open_positions": 2,
                "fixed_position_symbols": "BOXX,IBKR",
            }),
            broker=_FakeBroker(
                [
                    {"ticker": "BOXX", "position": 100},
                    {"ticker": "AAPL", "position": 5},
                ]
            ),
        )
        tracker = _FakeOrderTracker(
            [
                {"orderId": "101", "ticker": "MSFT", "status": "Submitted", "cOID": "entry_MSFT_long_20260513_100000"},
                {"orderId": "102", "ticker": "MSFT", "status": "Submitted", "parentId": "101", "cOID": "tp_MSFT_long_20260513_100000"},
                {"orderId": "103", "ticker": "IBKR", "status": "Submitted", "cOID": "entry_IBKR_long_20260513_100000"},
            ]
        )

        snapshot = lifecycle.strategy_capacity_snapshot(order_tracker=tracker)

        self.assertTrue(snapshot["capacity_full"])
        self.assertEqual(2, snapshot["strategy_capacity_used"])
        self.assertEqual(["AAPL"], snapshot["strategy_open_position_symbols"])
        self.assertEqual(["MSFT"], snapshot["open_strategy_entry_order_symbols"])

    def test_partial_harvest_tp_fill_reduces_stop_quantity_and_locks_breakeven(self):
        pb = _FakePB(
            orders=self._partial_harvest_rows(),
            snapshot={"close": 105.0, "high": 106.2, "low": 104.5, "vwap": 104.8, "ema_fast": 104.7, "atr": 1.0, "bar_time_ms": 2},
        )
        modifier = _FakeOrderModifier()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=modifier,
            config=_FakeConfig({"intraday_harvest_action_cooldown_sec": 0}),
        )

        lifecycle._maybe_apply_intraday_harvest_for_symbol({"ticker": "AAPL", "position": 70, "conid": 123}, lifecycle._intraday_harvest_settings())

        self.assertEqual([("103", {"quantity": 70, "auxPrice": 100.2})], modifier.modifications)
        entry_patch = next(item for item in pb.upserts if item["id"] == "entry")
        stop_patch = next(item for item in pb.upserts if item["id"] == "sl")
        self.assertTrue(entry_patch["extra"]["partial_tp_handled"])
        self.assertEqual(70, entry_patch["extra"]["remaining_after_partial_tp"])
        self.assertEqual(70, stop_patch["extra"]["partial_harvest_remaining_quantity"])

    def test_partial_harvest_reenters_sold_quantity_near_vwap_or_ema_support(self):
        pb = _FakePB(
            orders=self._partial_harvest_rows(handled=True),
            snapshot={"close": 101.0, "high": 101.2, "low": 100.8, "vwap": 101.05, "ema_fast": 103.0, "atr": 1.0, "bar_time_ms": 3},
        )
        placer = _FakeOrderPlacer()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=_FakeOrderModifier(),
            order_placer=placer,
            config=_FakeConfig({"intraday_harvest_action_cooldown_sec": 0}),
        )

        lifecycle._maybe_apply_intraday_harvest_for_symbol({"ticker": "AAPL", "position": 70, "conid": 123}, lifecycle._intraday_harvest_settings())

        self.assertEqual(1, len(placer.brackets))
        reentry = placer.brackets[0]
        self.assertEqual(30, reentry["quantity"])
        self.assertEqual("tactical_r1", reentry["order_ref_suffix"])
        self.assertTrue(reentry["order_extra"]["harvest_reentry"])
        entry_patch = next(item for item in pb.upserts if item["id"] == "entry")
        self.assertTrue(entry_patch["extra"]["reentry_done"])
        self.assertTrue(entry_patch["extra"]["partial_harvest_reentry_done"])

    def test_partial_harvest_stop_fill_cancels_remaining_target(self):
        pb = _FakePB(
            orders=self._partial_harvest_rows(tp_status="Submitted", sl_status="Filled"),
            snapshot={"close": 98.0, "high": 99.0, "low": 97.5, "vwap": 99.2, "ema_fast": 99.0, "atr": 1.0, "bar_time_ms": 4},
        )
        modifier = _FakeOrderModifier()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=modifier,
            config=_FakeConfig({"intraday_harvest_action_cooldown_sec": 0}),
        )

        lifecycle._maybe_apply_intraday_harvest_for_symbol({"ticker": "AAPL", "position": 0, "conid": 123}, lifecycle._intraday_harvest_settings())

        self.assertEqual(["102"], modifier.cancellations)
        target_patch = next(item for item in pb.upserts if item["id"] == "tp")
        self.assertEqual("Canceled", target_patch["status"])
        self.assertTrue(target_patch["extra"]["partial_harvest_cancelled_after_stop"])


if __name__ == "__main__":
    unittest.main()
