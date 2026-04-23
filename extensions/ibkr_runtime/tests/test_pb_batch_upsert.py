import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.integrations.pb_client import PBClient


class PBClientBatchUpsertTest(unittest.TestCase):
    def test_upsert_bars_uses_batch_create_and_patch_requests(self):
        client = PBClient(base_url="http://pb.test")
        captured_requests = []

        bars = [
            {
                "symbol": "aapl",
                "interval": "5m",
                "bar_time_ms": 1000,
                "environment": "live",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
            },
            {
                "symbol": "msft",
                "interval": "5m",
                "bar_time_ms": 2000,
                "environment": "live",
                "open": 3,
                "high": 4,
                "low": 3,
                "close": 4,
            },
        ]

        existing = {
            ("AAPL", "5m", 1000, "live"): {"id": "row_existing"},
        }

        with mock.patch.object(client, "_find_existing_records", return_value=existing):
            with mock.patch.object(
                client,
                "_execute_batch_requests",
                side_effect=lambda requests_payload, timeout=30, batch_size=50: captured_requests.extend(requests_payload),
            ):
                result = client.upsert_bars(bars)

        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(captured_requests), 2)

        patch_request = captured_requests[0]
        create_request = captured_requests[1]
        self.assertEqual(patch_request["method"], "PATCH")
        self.assertEqual(patch_request["url"], "/api/collections/ibkr_bars/records/row_existing")
        self.assertEqual(patch_request["body"]["symbol"], "AAPL")
        self.assertEqual(create_request["method"], "POST")
        self.assertEqual(create_request["url"], "/api/collections/ibkr_bars/records")
        self.assertEqual(create_request["body"]["symbol"], "MSFT")

    def test_call_custom_api_prefers_ibkr_api_internal_url(self):
        with mock.patch.dict(os.environ, {"IBKR_API_INTERNAL_URL": "http://api.test"}, clear=False):
            client = PBClient(base_url="http://pb.test")
        with mock.patch.object(client, "_request", return_value=mock.Mock(json=lambda: {"ok": True})) as request_mock:
            payload = client.call_custom_api("ibkr/statusz", method="GET", params={"environment": "live"})
        self.assertEqual({"ok": True}, payload)
        request_mock.assert_called_once()
        self.assertEqual("http://api.test/api/custom/ibkr/statusz", request_mock.call_args.args[1])

    def test_runtime_config_can_skip_custom_api_to_avoid_self_proxy_recursion(self):
        client = PBClient(base_url="http://pb.test", prefer_runtime_config_api=False)
        rows = [{"key": "alpha", "value": "1", "environment": "live"}]

        with mock.patch.object(client, "call_custom_api") as call_custom_api:
            with mock.patch.object(client, "get_all_records", return_value=rows) as get_all_records:
                payload = client.get_runtime_config(scope="all", environment="live")

        self.assertEqual(rows, payload)
        call_custom_api.assert_not_called()
        get_all_records.assert_called_once_with("config", sort="sort_order,key", max_pages=20)


if __name__ == "__main__":
    unittest.main()
