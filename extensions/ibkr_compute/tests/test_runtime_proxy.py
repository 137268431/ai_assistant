import os
import json
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
    def __init__(self, content=b"", status=200, payload=None):
        self.payload = payload
        self.content = str(payload).encode("utf-8") if payload is not None else content
        self.status_code = status
        self.headers = {}

    def __getitem__(self, key):
        return self.payload[key]

    def __contains__(self, key):
        return key in (self.payload or {})

    def get(self, key, default=None):
        return (self.payload or {}).get(key, default)


def _jsonify(payload):
    return _FlaskResponse(status=200, payload=payload)


class _FakeFlask:
    def __init__(self, name):
        self.name = name

    def route(self, _path, methods=None):
        def decorator(func):
            return func
        return decorator


class _BootstrapRequest:
    args = {}
    headers = {}
    method = "GET"

    def get_json(self, silent=True):
        return {}

    def get_data(self, cache=True):
        return b"{}"


sys.modules.setdefault(
    "flask",
    types.SimpleNamespace(Flask=_FakeFlask, Response=_FlaskResponse, jsonify=_jsonify, request=_BootstrapRequest()),
)

from ibkr_compute.api import runtime_proxy


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = {"ok": True} if payload is None else payload
        self.ok = 200 <= int(status_code) < 300
        self.status_code = int(status_code)
        self.content = json.dumps(self._payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload


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

    def __init__(self, body=b'{"ok": true}'):
        self._body = body

    def get_data(self, cache=True):
        return self._body


class _ImmediateThread:
    def __init__(self, target=None, args=None, kwargs=None, **_):
        self.target = target
        self.args = args or ()
        self.kwargs = kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)


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

    def test_data_quality_repair_async_returns_accepted_and_persists_status(self):
        runtime_proxy._ASYNC_OPERATION_STATES.clear()
        body = json.dumps({
            "async": True,
            "operation_id": "op-test",
            "data_environment": "live",
        }).encode("utf-8")
        with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
            with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                with mock.patch.object(runtime_proxy.threading, "Thread", _ImmediateThread):
                    with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse({"ok": True, "summary": {"expected_symbols_total": 1}})) as request_mock:
                        with mock.patch.object(runtime_proxy, "request", _FakeRequest(body)):
                            response, status = runtime_proxy.proxy_runtime_request("/ibkr/data-quality/repair")

        self.assertEqual(202, status)
        self.assertIn(b"accepted", response.content)
        state = runtime_proxy._load_async_operation_state("op-test", "live")
        self.assertEqual("completed", state["status"])
        self.assertEqual("/ibkr/data-quality/repair", state["path"])
        forward_payload = json.loads(request_mock.call_args.kwargs["data"].decode("utf-8"))
        self.assertNotIn("async", forward_payload)
        self.assertEqual("op-test", forward_payload["operation_id"])


if __name__ == "__main__":
    unittest.main()
