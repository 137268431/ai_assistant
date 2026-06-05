import sys
import unittest
import json
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.integrations.feishu import feishu_send_interactive, feishu_token, feishu_update_interactive
from ibkr_api.integrations.feishu_time import normalize_feishu_time_text


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = b"payload" if payload is not None else b""

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, *, post_responses=None, patch_responses=None):
        self._post_responses = list(post_responses or [])
        self._patch_responses = list(patch_responses or [])
        self.post_calls = []
        self.patch_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        if not self._post_responses:
            raise AssertionError(f"unexpected post call: {url}")
        return self._post_responses.pop(0)

    def patch(self, url, **kwargs):
        self.patch_calls.append({"url": url, **kwargs})
        if not self._patch_responses:
            raise AssertionError(f"unexpected patch call: {url}")
        return self._patch_responses.pop(0)


class FeishuIntegrationTest(unittest.TestCase):
    @staticmethod
    def _normalize_environment(value, default):
        return str(value or default).strip().lower() or default

    def test_feishu_token_accepts_success_code_zero(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 0, "app_access_token": "token-123", "expire": 7200}),
            ]
        )

        token = feishu_token(requests_module=requests_module, cache={}, now_fn=lambda: 1000.0)

        self.assertEqual(token, "token-123")
        self.assertEqual(len(requests_module.post_calls), 1)
        self.assertEqual(
            requests_module.post_calls[0]["url"],
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        )

    def test_feishu_send_interactive_uses_token_when_code_is_zero(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 0, "app_access_token": "token-123", "expire": 7200}),
                _FakeResponse({"code": 0, "data": {"message_id": "msg-1"}}),
            ]
        )

        result = feishu_send_interactive(
            {"elements": []},
            "oc_system_chat",
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: feishu_token(requests_module=requests_module, cache={}, now_fn=lambda: 1000.0),
            requests_module=requests_module,
        )

        self.assertEqual(
            result,
            {"success": True, "message_id": "msg-1", "data": {"message_id": "msg-1"}},
        )
        self.assertEqual(len(requests_module.post_calls), 2)
        self.assertEqual(
            requests_module.post_calls[1]["headers"]["Authorization"],
            "Bearer token-123",
        )

    def test_feishu_send_interactive_normalizes_visible_times(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 0, "data": {"message_id": "msg-1"}}),
            ]
        )

        result = feishu_send_interactive(
            {
                "header": {"title": {"tag": "plain_text", "content": "信号 · 2026-06-04 20:05:31"}},
                "elements": [
                    {"tag": "markdown", "content": "**时间**: 2026-06-04 20:05:31\n**本轮截止**: 2026-06-05 08:05:31 CN / 2026-06-04 20:05:31 US"}
                ],
            },
            "oc_system_chat",
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: "token-123",
            requests_module=requests_module,
        )

        self.assertTrue(result["success"])
        payload = json.loads(requests_module.post_calls[0]["json"]["content"])
        title = payload["header"]["title"]["content"]
        content = payload["elements"][0]["content"]
        expected = "美东 2026-06-04 20:05:31 | 北京 2026-06-05 08:05:31"
        self.assertIn(expected, title)
        self.assertIn(f"**时间**: {expected}", content)
        self.assertIn(f"**本轮截止**: {expected}", content)

    def test_normalize_feishu_time_text_handles_beijing_first_sentence(self):
        text = "当前预计截止为北京时间 2026-06-05 08:05:31 / 美东 2026-06-04 20:05:31。"

        self.assertEqual(
            normalize_feishu_time_text(text),
            "当前预计截止为美东 2026-06-04 20:05:31 | 北京 2026-06-05 08:05:31。",
        )

    def test_feishu_update_interactive_uses_token_when_code_is_zero(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 0, "app_access_token": "token-456", "expire": 7200}),
            ],
            patch_responses=[
                _FakeResponse({"code": 0, "data": {"message_id": "msg-2", "revision": "2"}}),
            ],
        )

        result = feishu_update_interactive(
            "msg-2",
            {"elements": []},
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: feishu_token(requests_module=requests_module, cache={}, now_fn=lambda: 1000.0),
            requests_module=requests_module,
        )

        self.assertEqual(
            result,
            {"success": True, "message_id": "msg-2", "data": {"message_id": "msg-2", "revision": "2"}},
        )
        self.assertEqual(len(requests_module.post_calls), 1)
        self.assertEqual(len(requests_module.patch_calls), 1)
        self.assertEqual(
            requests_module.patch_calls[0]["headers"]["Authorization"],
            "Bearer token-456",
        )

    def test_feishu_send_interactive_still_rejects_non_zero_code(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 99991663, "msg": "invalid request"}),
            ]
        )

        result = feishu_send_interactive(
            {"elements": []},
            "oc_system_chat",
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: feishu_token(requests_module=requests_module, cache={}, now_fn=lambda: 1000.0),
            requests_module=requests_module,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "missing_token")
        self.assertEqual(len(requests_module.post_calls), 1)

    def test_feishu_send_interactive_records_error_detail(self):
        requests_module = _FakeRequests(
            post_responses=[
                _FakeResponse({"code": 99991663, "msg": "invalid card payload"}, status_code=400),
            ]
        )

        result = feishu_send_interactive(
            {"elements": []},
            "oc_system_chat",
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: "token-789",
            requests_module=requests_module,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "http_400")
        self.assertEqual(result["http_status"], 400)
        self.assertEqual(result["api_code"], 99991663)
        self.assertEqual(result["api_message"], "invalid card payload")
        self.assertIn("invalid card payload", result["response_body"])

    def test_feishu_update_interactive_records_error_detail(self):
        requests_module = _FakeRequests(
            patch_responses=[
                _FakeResponse({"code": 230099, "msg": "message not found"}, status_code=200),
            ]
        )

        result = feishu_update_interactive(
            "om-missing",
            {"elements": []},
            "live",
            normalize_environment=self._normalize_environment,
            token_loader=lambda: "token-789",
            requests_module=requests_module,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["message_id"], "om-missing")
        self.assertEqual(result["error"], "api_230099")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["api_code"], 230099)
        self.assertEqual(result["api_message"], "message not found")


if __name__ == "__main__":
    unittest.main()
