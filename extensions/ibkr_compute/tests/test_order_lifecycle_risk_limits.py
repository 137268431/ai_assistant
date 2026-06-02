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
    def __init__(self, positions=None, fill_result=None):
        self.positions = list(positions or [])
        self.fill_result = dict(fill_result or {})
        self.fill_calls = []

    def list_positions(self):
        return list(self.positions)

    def await_order_fill(self, order_id, **kwargs):
        self.fill_calls.append({"order_id": str(order_id), **dict(kwargs or {})})
        return dict(self.fill_result or {"ok": False, "error": "close_fill_unconfirmed"})


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
    def __init__(self, cancel_results=None):
        self.modifications = []
        self.cancellations = []
        self.cancel_results = list(cancel_results or [])

    def modify_order(self, order_id, updates, acct_id=None):
        self.modifications.append((str(order_id), dict(updates or {})))
        return {"ok": True, "order_id": str(order_id), "updates": dict(updates or {})}

    def update_stop_loss(self, order_id, new_sl_price, acct_id=None):
        return self.modify_order(order_id, {"auxPrice": float(new_sl_price)}, acct_id)

    def cancel_order(self, order_id, acct_id=None):
        self.cancellations.append(str(order_id))
        if self.cancel_results:
            result = dict(self.cancel_results.pop(0))
            result.setdefault("order_id", str(order_id))
            return result
        return {"ok": True, "order_id": str(order_id)}


class _FakeOrderPlacer:
    def __init__(self):
        self.brackets = []
        self.closes = []

    def place_bracket_order(self, **kwargs):
        self.brackets.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": ["201", "202", "203"],
            "bracket_group": f"{kwargs.get('symbol')}_reentry",
        }

    def place_market_close(self, **kwargs):
        self.closes.append(dict(kwargs))
        return {
            "ok": True,
            "submitted": True,
            "filled": True,
            "order_ids": ["901"],
            "entry_coid": f"close_{kwargs.get('symbol')}_order_flow",
            "bracket_group": f"close_{kwargs.get('symbol')}_order_flow",
            "order_type": str(kwargs.get("order_type") or "MKT").upper(),
            "limit_price": float(kwargs.get("limit_price") or 0.0),
        }


class _FakeOrderFlowManager:
    def __init__(self, decision=None):
        self.decision = dict(decision or {"action": "hold", "reason": "test_hold"})
        self.positions = []
        self.decisions = []

    def sync_positions(self, positions):
        self.positions.append(list(positions or []))

    def position_decision(self, position, order_group=None):
        self.decisions.append({"position": dict(position or {}), "order_group": dict(order_group or {})})
        return dict(self.decision)


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

    def _order_flow_rows(self, *, entry_extra=None, tp_status="Submitted", sl_status="Submitted", close_status=None):
        group = "NFLX_short_20260527_105036_harvest"
        base_extra = {
            "harvest_managed": True,
            "harvest_profile": "intraday_volatility_harvest_v1",
            "harvest_lot": "primary",
            "partial_harvest_managed": True,
        }
        rows = [
            {
                "id": "entry",
                "symbol": "NFLX",
                "role": "entry",
                "status": "Filled",
                "quantity": 114,
                "filled_qty": 114,
                "fill_price": 88.01877,
                "limit_price": 88.02,
                "broker_order_id": "84",
                "trade_group_id": group,
                "entry_order_unique_id": f"entry_{group}",
                "unique_id": f"entry_{group}",
                "signal_id": "NFLX_20260527_1045_mr_U",
                "bar_time_ms": 10,
                "environment": "paper",
                "extra": {**base_extra, **dict(entry_extra or {})},
            },
            {
                "id": "tp",
                "symbol": "NFLX",
                "role": "take_profit",
                "status": tp_status,
                "quantity": 114,
                "filled_qty": 0,
                "limit_price": 86.70,
                "broker_order_id": "85",
                "trade_group_id": group,
                "entry_order_unique_id": f"entry_{group}",
                "parent_order_unique_id": f"entry_{group}",
                "unique_id": f"tp_{group}",
                "bar_time_ms": 10,
                "environment": "paper",
                "extra": dict(base_extra),
            },
            {
                "id": "sl",
                "symbol": "NFLX",
                "role": "stop_loss",
                "status": sl_status,
                "quantity": 114,
                "filled_qty": 0,
                "limit_price": 88.91,
                "broker_order_id": "86",
                "trade_group_id": group,
                "entry_order_unique_id": f"entry_{group}",
                "parent_order_unique_id": f"entry_{group}",
                "unique_id": f"sl_{group}",
                "bar_time_ms": 10,
                "environment": "paper",
                "extra": dict(base_extra),
            },
        ]
        if close_status:
            rows.append(
                {
                    "id": "close",
                    "symbol": "NFLX",
                    "role": "close",
                    "status": close_status,
                    "quantity": 114,
                    "filled_qty": 0,
                    "limit_price": 87.95,
                    "broker_order_id": "901",
                    "trade_group_id": group,
                    "entry_order_unique_id": f"entry_{group}",
                    "parent_order_unique_id": f"entry_{group}",
                    "unique_id": f"close_{group}",
                    "bar_time_ms": 11,
                    "environment": "paper",
                    "extra": {
                        **base_extra,
                        "harvest_lot": "close",
                        "close_order": True,
                    },
                }
            )
        return rows

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

    def test_missing_protection_ignores_active_or_filled_close_orders(self):
        for close_status in ("Submitted", "Filled"):
            with self.subTest(close_status=close_status):
                pb = _FakePB(
                    orders=self._order_flow_rows(
                        tp_status="Canceled",
                        sl_status="Canceled",
                        close_status=close_status,
                    )
                )
                lifecycle = OrderLifecycle(pb_client=pb, environment="paper", config=_FakeConfig({}))

                issues = lifecycle._detect_missing_protection_after_fill(
                    [{"ticker": "NFLX", "position": -114, "conid": 123}],
                    open_orders=[],
                )

                self.assertEqual([], issues)
                self.assertEqual([], pb.upserts)
                self.assertEqual([], pb.events)

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

    def test_order_flow_full_exit_persists_close_intent_when_cancel_is_pending(self):
        pb = _FakePB(orders=self._order_flow_rows())
        modifier = _FakeOrderModifier(
            cancel_results=[
                {"ok": False, "error": "order_cancel_unconfirmed"},
                {"ok": False, "error": "order_cancel_unconfirmed"},
            ]
        )
        placer = _FakeOrderPlacer()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=modifier,
            order_placer=placer,
            environment="paper",
            order_flow_manager=_FakeOrderFlowManager(
                {
                    "action": "full_exit",
                    "reason": "order_flow_adverse_delta_exit",
                    "limit_price": 87.95,
                    "marketable_limit": {"order_type": "marketable_limit"},
                }
            ),
            config=_FakeConfig({"ibkr_order_flow_close_fill_timeout_sec": 1}),
            broker=_FakeBroker(),
        )

        acted = lifecycle._maybe_apply_order_flow_risk_for_symbol({"ticker": "NFLX", "position": -114, "conid": 123})

        self.assertTrue(acted)
        self.assertEqual(["85", "86"], modifier.cancellations)
        self.assertEqual([], placer.closes)
        entry_patch = pb.upserts[-1]
        self.assertEqual("entry", entry_patch["id"])
        self.assertEqual("Closing", entry_patch["status"])
        self.assertTrue(entry_patch["extra"]["order_flow_closing"])
        self.assertEqual("protection_cancel_pending", entry_patch["extra"]["order_flow_close_intent"]["reason"])
        pending_patches = [item for item in pb.upserts if item["id"] in {"tp", "sl"}]
        self.assertTrue(all(item["extra"]["order_flow_cancel_pending"] for item in pending_patches))

    def test_order_flow_close_intent_recovers_after_protection_orders_are_canceled(self):
        decision = {
            "action": "full_exit",
            "reason": "order_flow_adverse_delta_exit",
            "limit_price": 87.95,
            "marketable_limit": {"order_type": "marketable_limit"},
        }
        pb = _FakePB(
            orders=self._order_flow_rows(
                tp_status="Canceled",
                sl_status="Canceled",
                entry_extra={
                    "order_flow_closing": True,
                    "order_flow_close_intent": {
                        "reason": "protection_cancel_pending",
                        "quantity": 114,
                        "symbol": "NFLX",
                        "conid": 123,
                        "direction": "short",
                        "decision": dict(decision),
                    },
                },
            )
        )
        placer = _FakeOrderPlacer()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=_FakeOrderModifier(),
            order_placer=placer,
            environment="paper",
            order_flow_manager=_FakeOrderFlowManager({"action": "hold", "reason": "no_new_decision"}),
            config=_FakeConfig({"ibkr_order_flow_close_fill_timeout_sec": 1}),
            broker=_FakeBroker(),
        )

        acted = lifecycle._maybe_apply_order_flow_risk_for_symbol({"ticker": "NFLX", "position": -114, "conid": 123})

        self.assertTrue(acted)
        self.assertEqual(1, len(placer.closes))
        close_call = placer.closes[0]
        self.assertEqual("short", close_call["direction"])
        self.assertEqual(114, close_call["quantity"])
        self.assertEqual("marketable_limit", close_call["order_type"])
        self.assertEqual(87.95, close_call["limit_price"])
        entry_patch = next(item for item in reversed(pb.upserts) if item["id"] == "entry")
        self.assertEqual("Closed", entry_patch["status"])
        self.assertFalse(entry_patch["extra"]["order_flow_closing"])
        self.assertEqual({}, entry_patch["extra"]["order_flow_close_intent"])

    def test_order_flow_close_intent_does_not_duplicate_active_close_order(self):
        decision = {
            "action": "full_exit",
            "reason": "order_flow_adverse_delta_exit",
            "limit_price": 87.95,
            "marketable_limit": {"order_type": "marketable_limit"},
        }
        pb = _FakePB(
            orders=self._order_flow_rows(
                tp_status="Canceled",
                sl_status="Canceled",
                close_status="Submitted",
                entry_extra={
                    "order_flow_closing": True,
                    "order_flow_close_intent": {"decision": dict(decision), "quantity": 114},
                },
            )
        )
        placer = _FakeOrderPlacer()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=_FakeOrderModifier(),
            order_placer=placer,
            environment="paper",
            order_flow_manager=_FakeOrderFlowManager({"action": "hold", "reason": "no_new_decision"}),
            broker=_FakeBroker(fill_result={"ok": False, "error": "close_fill_unconfirmed"}),
        )

        acted = lifecycle._maybe_apply_order_flow_risk_for_symbol({"ticker": "NFLX", "position": -114, "conid": 123})

        self.assertTrue(acted)
        self.assertEqual([], placer.closes)
        self.assertEqual([{"order_id": "901", "symbol": "NFLX", "expected_quantity": 114, "timeout": 5.0, "poll_interval": 0.2}], lifecycle.broker.fill_calls)
        entry_patch = next(item for item in reversed(pb.upserts) if item["id"] == "entry")
        self.assertEqual("order_flow_close_pending", entry_patch["extra"]["reason"])

    def test_order_flow_full_exit_hard_cancel_failure_skips_close_submission(self):
        pb = _FakePB(orders=self._order_flow_rows())
        modifier = _FakeOrderModifier(
            cancel_results=[
                {"ok": False, "error": "exchange_rejected_cancel"},
                {"ok": True},
            ]
        )
        placer = _FakeOrderPlacer()
        lifecycle = OrderLifecycle(
            pb_client=pb,
            order_modifier=modifier,
            order_placer=placer,
            environment="paper",
            order_flow_manager=_FakeOrderFlowManager(
                {
                    "action": "full_exit",
                    "reason": "order_flow_adverse_delta_exit",
                    "limit_price": 87.95,
                    "marketable_limit": {"order_type": "marketable_limit"},
                }
            ),
            broker=_FakeBroker(),
        )

        acted = lifecycle._maybe_apply_order_flow_risk_for_symbol({"ticker": "NFLX", "position": -114, "conid": 123})

        self.assertFalse(acted)
        self.assertEqual([], placer.closes)
        self.assertEqual(["85", "86"], modifier.cancellations)
        self.assertFalse(any(item["id"] == "tp" and item.get("status") == "Canceled" for item in pb.upserts))


if __name__ == "__main__":
    unittest.main()
