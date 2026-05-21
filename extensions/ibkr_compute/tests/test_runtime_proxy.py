import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPUTE_SRC = REPO_ROOT / "runtime" / "ibkr_compute" / "src"
if str(COMPUTE_SRC) not in sys.path:
    sys.path.insert(0, str(COMPUTE_SRC))


class _FlaskResponse:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status
        self.headers = {}


def _jsonify(payload):
    return _FlaskResponse(str(payload).encode("utf-8"), status=200)


sys.modules.setdefault(
    "flask",
    types.SimpleNamespace(Response=_FlaskResponse, jsonify=_jsonify, request=None),
)

from ibkr_compute.api import runtime_proxy


class _FakeResponse:
    ok = True
    status_code = 200
    content = b'{"ok": true}'
    headers = {"Content-Type": "application/json"}


class _FakeArgs:
    def items(self, multi=False):
        return []


class _FakeHeaders:
    def get(self, name):
        return None


class _FakeRequest:
    method = "POST"
    args = _FakeArgs()
    headers = _FakeHeaders()

    def get_data(self, cache=True):
        return b'{"ok": true}'


class RuntimeProxyTimeoutTest(unittest.TestCase):
    def _proxy_once(self, path: str):
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "IBKR_COMPUTE_RUNTIME_PROXY_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_PROXY_REPAIR_TIMEOUT_SEC",
            }
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
                with mock.patch.object(runtime_proxy, "Response", _FlaskResponse):
                    with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                        with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse()) as request_mock:
                            with mock.patch.object(runtime_proxy, "request", _FakeRequest()):
                                response = runtime_proxy.proxy_runtime_request(path)
        return response, request_mock

    def test_default_runtime_proxy_timeout_remains_short(self):
        _, request_mock = self._proxy_once("/ibkr/status")

        self.assertEqual(60, request_mock.call_args.kwargs["timeout"])

    def test_data_quality_repair_uses_long_runtime_proxy_timeout(self):
        _, request_mock = self._proxy_once("/ibkr/data-quality/repair")

        self.assertEqual(300, request_mock.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
