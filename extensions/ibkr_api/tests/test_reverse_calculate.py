import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.reverse_calculate import build_reverse_calculate_response


class ReverseCalculateTest(unittest.TestCase):
    def test_requires_symbol_and_direction(self):
        payload, status_code = build_reverse_calculate_response(object(), payload={"environment": "live"})
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"], "Missing symbol or direction")

    def test_rejects_invalid_force_action_type(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long", "force_action_type": "flip"},
        )
        self.assertEqual(status_code, 400)
        self.assertIn("Invalid force_action_type", payload["error"])

    def test_returns_no_conflict_target_when_no_active_order(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long"},
            active_order_loader=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["created"])
        self.assertEqual(payload["reason"], "no_conflict_target")
        self.assertEqual(payload["analysis"]["action_type"], "cancel")

    def test_force_cancel_requires_pending_entry(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long", "force_action_type": "cancel"},
            active_order_loader=lambda *_args, **_kwargs: {"id": "order-1"},
            order_context_builder=lambda _order: {"direction": "long", "target_state": "filled_position"},
        )
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"], "Action cancel requires pending_entry")

    def test_returns_not_found_when_indicator_missing(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long"},
            active_order_loader=lambda *_args, **_kwargs: {"id": "order-1"},
            order_context_builder=lambda _order: {"direction": "long", "target_state": "pending_entry"},
            indicator_loader=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"], "No ibkr_indicators found for symbol")

    def test_returns_no_conditions_when_score_is_empty(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long"},
            active_order_loader=lambda *_args, **_kwargs: {"id": "order-1"},
            order_context_builder=lambda _order: {"direction": "long", "target_state": "pending_entry"},
            indicator_loader=lambda *_args, **_kwargs: {"id": "ind-1", "bar_time_ms": 1},
            indicator_analyzer=lambda *_args, **_kwargs: {
                "score": 0,
                "triggered_signals": [],
                "indicator_extra": {},
                "crsi": 50,
                "obv_rsi": 50,
                "vwap_dist": 0,
                "close": 100,
            },
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["reason"], "no_reverse_conditions")
        self.assertEqual(payload["analysis"]["target_state"], "pending_entry")
        self.assertEqual(payload["analysis"]["action_type"], "cancel")

    def test_creates_reverse_signal_and_notifies_at_threshold(self):
        captured = {}
        notifications = []

        def fake_upsert(_pb, upsert_payload, escape_filter=None):
            captured["upsert_payload"] = dict(upsert_payload)
            record = {
                "id": "rev-1",
                **upsert_payload,
                "extra": dict(upsert_payload.get("extra") or {}),
            }
            return {"record": record, "created": True}

        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long", "origin_signal_id": "sig-1"},
            escape_filter=lambda value: str(value),
            active_order_loader=lambda *_args, **_kwargs: {"id": "order-1"},
            order_context_builder=lambda _order: {
                "direction": "long",
                "target_state": "filled_position",
                "order_status": "Filled",
                "relation_status": "active",
                "position_side": "long",
                "signal_id": "sig-base",
                "order_unique_id": "entry-1",
                "broker_order_id": "1001",
                "trade_group_id": "grp-1",
                "entry_order_unique_id": "entry-1",
                "entry_price": 101.5,
                "quantity": 10,
                "take_profit": 110.0,
                "stop_loss": 96.0,
            },
            indicator_loader=lambda *_args, **_kwargs: {
                "id": "ind-1",
                "bar_time_ms": 1713797700000,
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
            },
            indicator_analyzer=lambda *_args, **_kwargs: {
                "score": 6,
                "triggered_signals": ["背离"],
                "indicator_extra": {"close": 102.2, "crsi": 75},
                "crsi": 75,
                "obv_rsi": 66,
                "vwap_dist": 2.4,
                "close": 102.2,
            },
            reverse_upsert_builder=fake_upsert,
            threshold_loader=lambda *_args, **_kwargs: 6,
            notify_reverse_signal=lambda record, context: notifications.append((record, context)),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["created"])
        self.assertFalse(payload["duplicate"])
        self.assertEqual(payload["signal"]["id"], "rev-1")
        self.assertEqual(payload["signal"]["action_type"], "close")
        self.assertEqual(captured["upsert_payload"]["extra"]["trade_group_id"], "grp-1")
        self.assertEqual(captured["upsert_payload"]["extra"]["origin_signal_id"], "sig-1")
        self.assertEqual(captured["upsert_payload"]["triggered_signals"], ["背离"])
        self.assertEqual(len(notifications), 1)
        self.assertIn("等待 IBKR 执行", notifications[0][1]["message"])


if __name__ == "__main__":
    unittest.main()
