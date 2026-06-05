import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api import compute_status_client


class _JsonResponse:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class ComputeStatusClientScanTest(unittest.TestCase):
    def setUp(self):
        compute_status_client._compute_status_cache["expires_at"] = 0.0
        compute_status_client._compute_status_cache["payload"] = None
        compute_status_client._compute_status_cache["cache_key"] = None

    def test_get_remote_compute_status_skips_runtime_status_lookup(self):
        with mock.patch.object(compute_status_client, "get_compute_internal_url", return_value="http://127.0.0.1:5100"):
            with mock.patch.object(
                compute_status_client.requests,
                "get",
                return_value=_JsonResponse({"service_profile": "compute", "multi_timeframe_readiness": {"status": "ready"}}),
            ) as get_mock:
                payload = compute_status_client.get_remote_compute_status(symbols=["aapl"])

        self.assertEqual("compute", payload["service_profile"])
        self.assertEqual("http://127.0.0.1:5100/status", get_mock.call_args.args[0])
        self.assertEqual(
            {"lite": "1", "skip_runtime_status": "1", "symbols": "AAPL"},
            get_mock.call_args.kwargs["params"],
        )

    def test_trigger_remote_scan_timeout_is_structured_retryable_failure(self):
        with mock.patch.object(compute_status_client, "get_compute_internal_url", return_value="http://127.0.0.1:5100"):
            with mock.patch.object(
                compute_status_client.requests,
                "post",
                side_effect=requests.exceptions.Timeout("read timed out"),
            ):
                payload = compute_status_client.trigger_remote_scan({"environment": "live"})

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "compute_scan_submit_timeout")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["path"], "/scan")

    def test_get_remote_scan_status_connection_error_is_retryable(self):
        with mock.patch.object(compute_status_client, "get_compute_internal_url", return_value="http://127.0.0.1:5100"):
            with mock.patch.object(
                compute_status_client.requests,
                "get",
                side_effect=requests.exceptions.ConnectionError("connection refused"),
            ):
                payload = compute_status_client.get_remote_scan_status({"environment": "live", "run_id": "abc"})

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "compute_unreachable")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["path"], "/scan/status")


if __name__ == "__main__":
    unittest.main()
