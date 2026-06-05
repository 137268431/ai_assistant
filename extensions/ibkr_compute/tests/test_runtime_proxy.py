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

    def __init__(self, body=b'{"ok": true}', method=None, args=None):
        self._body = body
        if method is not None:
            self.method = method
        if args is not None:
            self.args = args

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
    def _clear_status_cache(self):
        with runtime_proxy._RUNTIME_STATUS_PROXY_CACHE_LOCK:
            runtime_proxy._RUNTIME_STATUS_PROXY_CACHE["payload"] = None
            runtime_proxy._RUNTIME_STATUS_PROXY_CACHE["cached_at"] = 0.0
            runtime_proxy._RUNTIME_STATUS_PROXY_CACHE["cache_key"] = ""
            runtime_proxy._RUNTIME_STATUS_PROXY_CACHE["entries"] = {}

    def _proxy_once(self, path: str, *, method: str = "GET"):
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "IBKR_COMPUTE_RUNTIME_PROXY_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_PROXY_REPAIR_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_STATUS_PROXY_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_STATUS_PROXY_CACHE_TTL_SEC",
            }
        }
        self._clear_status_cache()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
                with mock.patch.object(runtime_proxy, "Response", _FlaskResponse):
                    with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                        with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse()) as request_mock:
                            with mock.patch.object(runtime_proxy, "request", _FakeRequest(method=method)):
                                response = runtime_proxy.proxy_runtime_request(path)
        return response, request_mock

    def test_runtime_status_proxy_uses_short_status_timeout(self):
        _, request_mock = self._proxy_once("/ibkr/status")

        self.assertEqual(3, request_mock.call_args.kwargs["timeout"])
        self.assertIn(("skip_compute_status", "1"), request_mock.call_args.kwargs["params"])

    def test_default_runtime_proxy_timeout_remains_standard_for_non_status(self):
        _, request_mock = self._proxy_once("/ibkr/history")

        self.assertEqual(60, request_mock.call_args.kwargs["timeout"])

    def test_data_quality_repair_uses_long_runtime_proxy_timeout(self):
        _, request_mock = self._proxy_once("/ibkr/data-quality/repair")

        self.assertEqual(300, request_mock.call_args.kwargs["timeout"])

    def test_data_quality_truth_repair_uses_long_runtime_proxy_timeout(self):
        _, request_mock = self._proxy_once("/ibkr/data-quality/truth-repair")

        self.assertEqual(300, request_mock.call_args.kwargs["timeout"])

    def test_data_quality_tv_indicator_audit_uses_long_runtime_proxy_timeout(self):
        _, request_mock = self._proxy_once("/ibkr/data-quality/tv-indicator-audit")

        self.assertEqual(300, request_mock.call_args.kwargs["timeout"])

    def test_runtime_status_proxy_reuses_short_success_cache(self):
        self._clear_status_cache()
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "IBKR_COMPUTE_RUNTIME_PROXY_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_PROXY_REPAIR_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_STATUS_PROXY_TIMEOUT_SEC",
                "IBKR_COMPUTE_RUNTIME_STATUS_PROXY_CACHE_TTL_SEC",
            }
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
                with mock.patch.object(runtime_proxy, "Response", _FlaskResponse):
                    with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                        with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse({"ok": True, "environment": "paper"})) as request_mock:
                            with mock.patch.object(runtime_proxy, "request", _FakeRequest(method="GET")):
                                first = runtime_proxy.proxy_runtime_request("/ibkr/status")
                                second = runtime_proxy.proxy_runtime_request("/ibkr/status")

        self.assertEqual(1, request_mock.call_count)
        self.assertEqual(200, first.status_code)
        self.assertIn(b"runtime_status_proxy_cache_hit", second.content)

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

    def test_data_quality_truth_repair_async_returns_accepted(self):
        runtime_proxy._ASYNC_OPERATION_STATES.clear()
        body = json.dumps({
            "async": True,
            "operation_id": "op-truth-repair",
            "data_environment": "live",
        }).encode("utf-8")
        with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
            with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                with mock.patch.object(runtime_proxy.threading, "Thread", _ImmediateThread):
                    with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse({"ok": True, "proof_status": "green"})) as request_mock:
                        with mock.patch.object(runtime_proxy, "request", _FakeRequest(body)):
                            response, status = runtime_proxy.proxy_runtime_request("/ibkr/data-quality/truth-repair")

        self.assertEqual(202, status)
        self.assertIn(b"accepted", response.content)
        state = runtime_proxy._load_async_operation_state("op-truth-repair", "live")
        self.assertEqual("completed", state["status"])
        self.assertEqual("/ibkr/data-quality/truth-repair", state["path"])
        forward_payload = json.loads(request_mock.call_args.kwargs["data"].decode("utf-8"))
        self.assertNotIn("async", forward_payload)

    def test_data_quality_tv_indicator_audit_async_returns_accepted(self):
        runtime_proxy._ASYNC_OPERATION_STATES.clear()
        body = json.dumps({
            "async": True,
            "operation_id": "op-tv-audit",
            "data_environment": "live",
        }).encode("utf-8")
        with mock.patch.object(runtime_proxy, "get_runtime_internal_url", return_value="http://runtime.internal"):
            with mock.patch.object(runtime_proxy, "jsonify", _jsonify):
                with mock.patch.object(runtime_proxy.threading, "Thread", _ImmediateThread):
                    with mock.patch.object(runtime_proxy.requests, "request", return_value=_FakeResponse({"ok": True, "status": "pass"})) as request_mock:
                        with mock.patch.object(runtime_proxy, "request", _FakeRequest(body)):
                            response, status = runtime_proxy.proxy_runtime_request("/ibkr/data-quality/tv-indicator-audit")

        self.assertEqual(202, status)
        self.assertIn(b"accepted", response.content)
        state = runtime_proxy._load_async_operation_state("op-tv-audit", "live")
        self.assertEqual("completed", state["status"])
        self.assertEqual("/ibkr/data-quality/tv-indicator-audit", state["path"])
        forward_payload = json.loads(request_mock.call_args.kwargs["data"].decode("utf-8"))
        self.assertNotIn("async", forward_payload)


if __name__ == "__main__":
    unittest.main()
