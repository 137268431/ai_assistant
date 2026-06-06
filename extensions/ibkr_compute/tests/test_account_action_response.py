import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.action_builders import common


class AccountActionResponseTest(unittest.TestCase):
    def test_action_response_forces_fresh_non_stale_snapshot_after_write(self):
        calls = []

        def fake_snapshot(service, **kwargs):
            calls.append((service, dict(kwargs)))
            return {"ok": True, "cache_state": "fresh", "stale": False}

        service = object()
        with mock.patch.object(common, "_build_ibkr_account_snapshot", side_effect=fake_snapshot):
            payload, status = common._build_snapshot_action_response(
                service,
                "cancel_all_orders",
                {"ok": True},
            )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual({"force_refresh": True, "allow_stale": False}, calls[-1][1])


if __name__ == "__main__":
    unittest.main()
