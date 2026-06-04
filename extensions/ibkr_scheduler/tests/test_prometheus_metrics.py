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
    REPO_ROOT / "runtime" / "ibkr_scheduler" / "src",
    REPO_ROOT / "runtime" / "ibkr_compute" / "src",
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
TARGET_SERVICE_NAMES = (
    "resolve_target_service",
)
SERVICE_LABEL_NAMES = (
    "resolve_source_service",
    "resolve_service_labels",
    "resolve_service_identity",
    "build_service_labels",
    "service_labels_for_profile",
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


def _install_metrics(testcase, app, *, service_name="ibkr-scheduler", service_profile="scheduler"):
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


def _call_service_mapper(func, *, service_name, service_profile):
    env = {"IBKR_SERVICE_PROFILE": service_profile}
    attempts = (
        lambda: _call_with_supported_kwargs(
            func,
            service_name,
            service_name=service_name,
            service=service_name,
            service_profile=service_profile,
            profile=service_profile,
            env=env,
        ),
        lambda: _call_with_supported_kwargs(func, service_profile, service_profile=service_profile, profile=service_profile, env=env),
        lambda: func(),
    )
    last_exc = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


class SchedulerPrometheusMetricsTest(unittest.TestCase):
    def test_scheduler_app_exposes_prometheus_metrics_endpoint(self):
        _observability_or_skip(self, requiring=INSTALL_FUNCTION_NAMES)
        self.addCleanup(_restore_requests_metrics_patch)
        with mock.patch.dict(os.environ, {"IBKR_SERVICE_PROFILE": "scheduler", "IBKR_SCHEDULER_AUTOSTART": "false"}, clear=False):
            with _stub_pb_config_refresh():
                try:
                    app_mod = importlib.import_module("ibkr_scheduler.scheduler_app")
                except ModuleNotFoundError as exc:
                    self.skipTest(f"scheduler app dependencies are not installed: {exc}")
        app = getattr(app_mod, "app", None)
        if app is None or not hasattr(app, "test_client"):
            self.skipTest("real Flask test client is unavailable")

        response = app.test_client().get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertIn("text/plain", response.headers.get("Content-Type", ""))
        body = _response_text(response)
        self.assertIn("service=\"ibkr-scheduler\"", body)

    def test_flask_metrics_normalize_scheduler_job_routes(self):
        Flask = _flask_or_skip(self)
        app = Flask("scheduler-prometheus-test")
        _install_metrics(self, app, service_name="ibkr-scheduler", service_profile="scheduler")

        @app.route("/jobs/run/<job_id>", methods=["POST"])
        def run_job(job_id):
            return {"ok": True, "job_id": job_id}

        client = app.test_client()
        client.post("/jobs/run/ibkr_scan_runtime?scheduled_slot=2026-06-04T14:30&request_id=req-1")
        client.post("/jobs/run/system_status_reminder?scheduled_slot=2026-06-04T14:35&request_id=req-2")
        response = client.get("/metrics")
        body = _response_text(response)

        self.assertEqual(200, response.status_code)
        self.assertIn("service=\"ibkr-scheduler\"", body)
        self.assertTrue(
            any(route in body for route in ("/jobs/run/<job_id>", "/jobs/run/{job_id}", "/jobs/run/:job_id")),
            body,
        )
        for raw_value in ("ibkr_scan_runtime", "system_status_reminder", "2026-06-04T14:30", "2026-06-04T14:35", "req-1", "req-2"):
            self.assertNotIn(raw_value, body)

    def test_outbound_http_normalization_maps_internal_services(self):
        module = _observability_or_skip(self, requiring=OUTBOUND_NORMALIZER_NAMES + TARGET_SERVICE_NAMES)
        normalizer = _first_callable(module, OUTBOUND_NORMALIZER_NAMES)
        target_resolver = _first_callable(module, TARGET_SERVICE_NAMES)
        self.assertIsNotNone(normalizer)
        self.assertIsNotNone(target_resolver)

        url = "http://127.0.0.1:5100/compute?symbol=AAPL&request_id=req-123&trigger_source=scheduler"
        normalized = _call_outbound_normalizer(normalizer, "POST", url)
        route = str(_field(normalized, "route", "path", "normalized_route", "url_path", "endpoint", "value"))
        target_service = str(target_resolver(url))

        self.assertEqual("/compute", route)
        self.assertNotIn("AAPL", route)
        self.assertNotIn("req-123", route)
        self.assertNotIn("?", route)
        self.assertEqual("ibkr-compute", target_service)

    def test_scheduler_profile_maps_to_scheduler_service_label(self):
        module = _observability_or_skip(self, requiring=SERVICE_LABEL_NAMES)
        mapper = _first_callable(module, SERVICE_LABEL_NAMES)
        self.assertIsNotNone(mapper)

        labels = _call_service_mapper(mapper, service_name="ibkr-scheduler", service_profile="scheduler")
        service = str(_field(labels, "service", "service_name", "app", "name", "value"))
        profile = str(_field(labels, "service_profile", "profile") or "scheduler")

        self.assertEqual("scheduler", profile)
        self.assertEqual("ibkr-scheduler", service)


if __name__ == "__main__":
    unittest.main()
