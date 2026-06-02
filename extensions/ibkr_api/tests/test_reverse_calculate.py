import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
COMPUTE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
for src_root in (SERVICE_SRC_ROOT, COMPUTE_SRC_ROOT):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.reverse_calculate import build_reverse_calculate_response


class ReverseCalculateTest(unittest.TestCase):
    def test_reverse_calculate_is_disabled_tv_primary_only(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "symbol": "AAPL", "direction": "long"},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["created"])
        self.assertFalse(payload["duplicate"])
        self.assertEqual(payload["reason"], "disabled_tv_primary_only")
        self.assertIsNone(payload["signal"])
        self.assertEqual(payload["analysis"]["target_state"], "tv_primary_only")
        self.assertEqual(payload["analysis"]["action_type"], "none")

    def test_reverse_calculate_noop_preserves_request_context_without_loading_data(self):
        calls = []

        def fail_loader(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("loader should not be called while disabled")

        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={
                "environment": "paper",
                "market_data_mode": "live",
                "symbol": "msft",
                "direction": "short",
                "force_action_type": "close",
            },
            active_order_loader=fail_loader,
            indicator_loader=fail_loader,
            reverse_upsert_builder=fail_loader,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(calls, [])
        self.assertEqual(payload["broker_mode"], "paper")
        self.assertEqual(payload["data_environment"], "live")
        self.assertEqual(payload["analysis"]["symbol"], "MSFT")
        self.assertEqual(payload["analysis"]["direction"], "short")
        self.assertEqual(payload["analysis"]["action_type"], "close")

    def test_reverse_calculate_noop_does_not_validate_legacy_payload_shape(self):
        payload, status_code = build_reverse_calculate_response(
            object(),
            payload={"environment": "live", "force_action_type": "legacy_flip"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["reason"], "disabled_tv_primary_only")
        self.assertEqual(payload["analysis"]["symbol"], "")
        self.assertEqual(payload["analysis"]["direction"], "")
        self.assertEqual(payload["analysis"]["action_type"], "legacy_flip")


if __name__ == "__main__":
    unittest.main()
