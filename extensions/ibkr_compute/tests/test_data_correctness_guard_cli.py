import importlib.util
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "ops" / "validate" / "run_data_correctness_guard.py"
SPEC = importlib.util.spec_from_file_location("run_data_correctness_guard", SCRIPT_PATH)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class FakeClient:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def get_json(self, path, params, *, timeout):
        self.calls.append({"path": path, "params": params, "timeout": timeout})
        return self.states.pop(0)


class DataCorrectnessGuardCliTest(unittest.TestCase):
    def test_build_truth_repair_payload_defaults_to_trade_watchlist(self):
        args = Namespace(
            environment="live",
            scan_scope="trade_watchlist",
            market_date="2026-05-27",
            apply=True,
            delete_extra_bars=True,
            confirm_refetch=True,
            persist=True,
            symbols="aapl, MSFT",
            preset="",
        )

        payload = guard.build_truth_repair_payload(args, "op-1", True)

        self.assertEqual("trade_watchlist", payload["scan_scope"])
        self.assertEqual(["AAPL", "MSFT"], payload["symbols"])
        self.assertTrue(payload["apply"])
        self.assertTrue(payload["async"])
        self.assertEqual("op-1", payload["operation_id"])

    def test_poll_operation_returns_terminal_state(self):
        client = FakeClient([
            {"ok": True, "status": "running", "operation_id": "op-1"},
            {"ok": True, "status": "completed", "operation_id": "op-1", "result": {"proof_status": "green"}},
        ])

        with mock.patch.object(guard.time, "sleep", return_value=None):
            result = guard.poll_operation(client, operation_id="op-1", environment="live", timeout_s=5, interval_s=1)

        self.assertEqual("completed", result["status"])
        self.assertEqual(2, len(client.calls))
        self.assertEqual("/api/custom/ibkr/data_quality/operation_status", client.calls[0]["path"])


if __name__ == "__main__":
    unittest.main()
