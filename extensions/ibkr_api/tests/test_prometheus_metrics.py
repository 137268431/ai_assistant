import contextlib
import importlib
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
for src_root in (
    REPO_ROOT / "runtime" / "ibkr_api" / "src",
    REPO_ROOT / "runtime" / "ibkr_compute" / "src",
    REPO_ROOT / "runtime" / "ibkr_scheduler" / "src",
):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")
os.environ.setdefault("PB_RETRY_ATTEMPTS", "1")
os.environ.setdefault("PB_RETRY_BACKOFF_SECONDS", "0")

OBSERVABILITY_MODULE_CANDIDATES = (
    "ibkr_compute.observability.prometheus_metrics",
    "ibkr_compute.observability.prometheus",
    "ibkr_compute.observability.metrics",
    "ibkr_compute.observability",
)
INSTALL_FUNCTION_NAMES = (
    "instrument_flask_app",
    "register_flask_metrics",
    "register_prometheus_metrics",
    "setup_prometheus_metrics",
    "init_prometheus_metrics",
)
OUTBOUND_NORMALIZER_NAMES = (
    "normalize_outbound_route",
    "normalize_outbound_http",
    "normalize_outbound_http_target",
    "normalize_outbound_url",
    "normalize_http_client_target",
)
DENYLIST_NAMES = (
    "HIGH_CARDINALITY_LABEL_DENYLIST",
    "HIGH_CARDINALITY_DENYLIST",
    "METRIC_LABEL_DENYLIST",
    "DENIED_LABEL_NAMES",
)
TARGET_SERVICE_NAMES = (
    "resolve_target_service",
)
SANITIZER_NAMES = (
    "sanitize_metric_labels",
    "sanitize_labels",
    "filter_metric_labels",
    "safe_metric_labels",
)


class _MissingObservabilityApi(Exception):
    pass


def _has_any_symbol(module, names):
    return any(hasattr(module, name) for name in names)


def _import_observability_module(*, requiring=()):
    seen = []
    for module_name in OBSERVABILITY_MODULE_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or module_name.startswith(f"{exc.name}."):
                seen.append(f"{module_name}: missing")
                continue
            raise
        if not requiring or _has_any_symbol(module, requiring):
            return module
        seen.append(f"{module_name}: missing expected symbols {', '.join(requiring)}")
    raise _MissingObservabilityApi("; ".join(seen))


def _observability_or_skip(testcase, *, requiring=()):
    try:
        return _import_observability_module(requiring=requiring)
    except _MissingObservabilityApi as exc:
        testcase.skipTest(f"shared Prometheus observability API is not available yet ({exc})")


def _first_callable(module, names):
    for name in names:
        func = getattr(module, name, None)
        if callable(func):
            return func
    return None


def _call_with_supported_kwargs(func, *args, **kwargs):
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    if accepts_kwargs:
        return func(*args, **kwargs)
    filtered_kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return func(*args, **filtered_kwargs)


def _restore_requests_metrics_patch():
    try:
        module = importlib.import_module("ibkr_compute.observability.prometheus")
        original = getattr(module, "_ORIGINAL_REQUEST", None)
        if getattr(module, "_REQUESTS_PATCHED", False) and original is not None:
            import requests

            requests.sessions.Session.request = original
            module._ORIGINAL_REQUEST = None
            module._REQUESTS_PATCHED = False
    except Exception:
        return


def _stub_pb_config_refresh():
    try:
        from ibkr_compute.integrations.pb_client import PBClient
    except Exception:
        return contextlib.nullcontext()
    return mock.patch.object(PBClient, "get_runtime_config", return_value=[])


def _collector_registry():
    try:
        from prometheus_client import CollectorRegistry
    except ModuleNotFoundError:
        return None
    return CollectorRegistry(auto_describe=True)


def _install_metrics(testcase, app, *, service_name="ibkr-api", service_profile="api"):
    testcase.addCleanup(_restore_requests_metrics_patch)
    module = _observability_or_skip(testcase, requiring=INSTALL_FUNCTION_NAMES)
    installer = _first_callable(module, INSTALL_FUNCTION_NAMES)
    if installer is None:
        testcase.fail(f"observability module must expose one of {INSTALL_FUNCTION_NAMES}")
    return _call_with_supported_kwargs(
        installer,
        app,
        service_name=service_name,
        service=service_name,
        service_profile=service_profile,
        profile=service_profile,
        registry=_collector_registry(),
    )


def _flask_or_skip(testcase):
    try:
        from flask import Flask
    except ModuleNotFoundError:
        testcase.skipTest("Flask is required for /metrics integration coverage")
    return Flask


def _response_text(response):
    getter = getattr(response, "get_data", None)
    data = getter() if callable(getter) else getattr(response, "data", b"")
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _coerce_mapping(value):
    if isinstance(value, dict):
        return value
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    if isinstance(value, tuple):
        if len(value) >= 2:
            return {"target_service": value[0], "route": value[1]}
    return {"value": value}


def _field(mapping, *names):
    data = _coerce_mapping(mapping)
    for name in names:
        if name in data:
            return data[name]
    return ""


def _call_outbound_normalizer(func, method, url):
    base_urls = {
        "pocketbase": "http://127.0.0.1:8090",
        "ibkr-compute": "http://127.0.0.1:5100",
        "ibkr-backtest": "http://127.0.0.1:5105",
        "ibkr-runtime": "http://127.0.0.1:5101",
        "ibkr-api": "http://127.0.0.1:5102",
        "ibkr-scheduler": "http://127.0.0.1:5103",
    }
    attempts = (
        lambda: _call_with_supported_kwargs(func, method, url, method=method, url=url, base_urls=base_urls),
        lambda: _call_with_supported_kwargs(func, url, method=method, url=url, base_urls=base_urls),
        lambda: func(url),
    )
    last_exc = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _get_denylist(module):
    for name in DENYLIST_NAMES:
        value = getattr(module, name, None)
        if value is not None:
            return {str(item) for item in value}
    return set()


def _sanitize_labels(func, labels):
    attempts = (
        lambda: func(labels),
        lambda: func("ibkr_outbound_http_requests_total", labels),
        lambda: _call_with_supported_kwargs(func, labels=labels),
    )
    last_exc = None
    for attempt in attempts:
        try:
            return _coerce_mapping(attempt())
        except TypeError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


class ApiPrometheusMetricsTest(unittest.TestCase):
    def test_api_app_exposes_prometheus_metrics_endpoint(self):
        _observability_or_skip(self, requiring=INSTALL_FUNCTION_NAMES)
        self.addCleanup(_restore_requests_metrics_patch)
        with mock.patch.dict(os.environ, {"IBKR_SERVICE_PROFILE": "api", "IBKR_SCHEDULER_AUTOSTART": "false"}, clear=False):
            with _stub_pb_config_refresh():
                try:
                    app_mod = importlib.import_module("ibkr_api.api_app")
                except ModuleNotFoundError as exc:
                    self.skipTest(f"API app dependencies are not installed: {exc}")
        app = getattr(app_mod, "app", None)
        if app is None or not hasattr(app, "test_client"):
            self.skipTest("real Flask test client is unavailable")

        response = app.test_client().get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertIn("text/plain", response.headers.get("Content-Type", ""))
        body = _response_text(response)
        self.assertIn("service=\"ibkr-api\"", body)

    def test_flask_metrics_normalize_dynamic_api_routes(self):
        Flask = _flask_or_skip(self)
        app = Flask("api-prometheus-test")
        _install_metrics(self, app, service_name="ibkr-api", service_profile="api")

        @app.route("/api/custom/ibkr/signals/<signal_id>/ack", methods=["POST"])
        def signal_ack(signal_id):
            return {"ok": True, "signal_id": signal_id}

        client = app.test_client()
        client.post("/api/custom/ibkr/signals/sig_a1b2c3/ack?symbol=AAPL&request_id=req-1")
        client.post("/api/custom/ibkr/signals/sig_d4e5f6/ack?symbol=MSFT&request_id=req-2")
        response = client.get("/metrics")
        body = _response_text(response)

        self.assertEqual(200, response.status_code)
        self.assertIn("service=\"ibkr-api\"", body)
        self.assertTrue(
            any(
                route in body
                for route in (
                    "/api/custom/ibkr/signals/<signal_id>/ack",
                    "/api/custom/ibkr/signals/{signal_id}/ack",
                    "/api/custom/ibkr/signals/:signal_id/ack",
                )
            ),
            body,
        )
        for raw_value in ("sig_a1b2c3", "sig_d4e5f6", "AAPL", "MSFT", "req-1", "req-2"):
            self.assertNotIn(raw_value, body)

    def test_outbound_http_normalization_strips_query_and_record_ids(self):
        module = _observability_or_skip(self, requiring=OUTBOUND_NORMALIZER_NAMES + TARGET_SERVICE_NAMES)
        normalizer = _first_callable(module, OUTBOUND_NORMALIZER_NAMES)
        target_resolver = _first_callable(module, TARGET_SERVICE_NAMES)
        self.assertIsNotNone(normalizer)
        self.assertIsNotNone(target_resolver)

        url = "http://127.0.0.1:8090/api/collections/ibkr_signals/records/sig_a1b2c3?filter=symbol%3D'AAPL'&page=1"
        normalized = _call_outbound_normalizer(normalizer, "GET", url)
        route = str(_field(normalized, "route", "path", "normalized_route", "url_path", "endpoint", "value"))
        target_service = str(target_resolver(url))

        self.assertIn("/api/collections/ibkr_signals/records", route)
        self.assertRegex(route, r"/records/(<[^>]+>|\{[^}]+\}|:[^/]+|\*)")
        self.assertNotIn("sig_a1b2c3", route)
        self.assertNotIn("AAPL", route)
        self.assertNotIn("?", route)
        self.assertIn(target_service, {"pocketbase", "pb", "ibkr-pocketbase"})

    def test_high_cardinality_denylist_excludes_api_and_outbound_labels(self):
        module = _observability_or_skip(self)
        denylist = _get_denylist(module)
        sanitizer = _first_callable(module, SANITIZER_NAMES)
        required = {"symbol", "order_id", "signal_id", "run_id", "account_id", "request_id", "trace_id", "url", "path", "query", "raw_path"}

        if denylist:
            self.assertTrue(required.issubset(denylist), denylist)
            return
        if sanitizer is not None:
            filtered = _sanitize_labels(
                sanitizer,
                {
                    "service": "ibkr-api",
                    "service_profile": "api",
                    "method": "POST",
                    "route": "/api/custom/ibkr/signals/<signal_id>/ack",
                    "target_service": "pocketbase",
                    "target_route": "/api/collections/ibkr_signals/records/<record_id>",
                    "symbol": "AAPL",
                    "order_id": "ord-123",
                    "signal_id": "sig-456",
                    "run_id": "run-789",
                    "account_id": "DU123456",
                    "request_id": "req-abc",
                    "trace_id": "trace-def",
                    "url": "http://127.0.0.1:8090/api/collections/ibkr_signals/records/sig-456?symbol=AAPL",
                    "path": "/api/collections/ibkr_signals/records/sig-456",
                    "query": "symbol=AAPL",
                    "raw_path": "/api/custom/ibkr/signals/sig-456/ack?symbol=AAPL",
                },
            )
            self.assertEqual("ibkr-api", filtered.get("service"))
            self.assertEqual("pocketbase", filtered.get("target_service"))
            self.assertIn("target_route", filtered)
            for denied_name in required:
                self.assertNotIn(denied_name, filtered)
            return

        label_names = set()
        for metric_name in ("HTTP_SERVER_REQUESTS", "HTTP_SERVER_DURATION", "HTTP_CLIENT_REQUESTS", "HTTP_CLIENT_DURATION"):
            metric = getattr(module, metric_name, None)
            label_names.update(str(label) for label in (getattr(metric, "_labelnames", None) or ()))
        if not label_names:
            self.skipTest("prometheus_client label schemas are unavailable in this environment")
        self.assertFalse(required.intersection(label_names), label_names)


if __name__ == "__main__":
    unittest.main()
