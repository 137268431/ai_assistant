import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api import compute_status_client


class ComputeStatusClientScanTest(unittest.TestCase):
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
