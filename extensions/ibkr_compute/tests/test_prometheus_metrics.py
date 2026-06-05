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
    REPO_ROOT / "runtime" / "ibkr_compute" / "src",
    REPO_ROOT / "runtime" / "ibkr_api" / "src",
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
ROUTE_NORMALIZER_NAMES = (
    "normalize_path",
    "normalize_route_label",
    "normalize_flask_route",
    "normalize_route",
    "route_label",
)
SERVICE_LABEL_NAMES = (
    "resolve_source_service",
    "resolve_service_labels",
    "resolve_service_identity",
    "build_service_labels",
    "service_labels_for_profile",
)
DENYLIST_NAMES = (
    "HIGH_CARDINALITY_LABEL_DENYLIST",
    "HIGH_CARDINALITY_DENYLIST",
    "METRIC_LABEL_DENYLIST",
    "DENIED_LABEL_NAMES",
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


def _install_metrics(testcase, app, *, service_name="ibkr-compute", service_profile="compute"):
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
    if isinstance(value, tuple) and len(value) == 2:
        return {"service": value[0], "service_profile": value[1]}
    return {"value": value}


def _field(mapping, *names):
    data = _coerce_mapping(mapping)
    for name in names:
        if name in data:
            return data[name]
    return ""


def _call_route_normalizer(func, raw_path, *, rule=""):
    attempts = (
        lambda: func(raw_path),
        lambda: func(path=raw_path, route=rule, rule=rule),
        lambda: func(rule or raw_path, raw_path),
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


def _get_denylist(module):
    for name in DENYLIST_NAMES:
        value = getattr(module, name, None)
        if value is not None:
            return {str(item) for item in value}
    return set()


def _sanitize_labels(func, labels):
    attempts = (
        lambda: func(labels),
        lambda: func("ibkr_http_requests_total", labels),
        lambda: _call_with_supported_kwargs(func, labels=labels),
    )
    last_exc = None
    for attempt in attempts:
        try:
            return _coerce_mapping(attempt())
        except TypeError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


class ComputePrometheusMetricsTest(unittest.TestCase):
    def test_compute_app_exposes_prometheus_metrics_endpoint(self):
        _observability_or_skip(self, requiring=INSTALL_FUNCTION_NAMES)
        self.addCleanup(_restore_requests_metrics_patch)
        with mock.patch.dict(os.environ, {"IBKR_SERVICE_PROFILE": "compute", "IBKR_SCHEDULER_AUTOSTART": "false"}, clear=False):
            with _stub_pb_config_refresh():
                try:
                    app_mod = importlib.import_module("ibkr_compute.api.app")
                except ModuleNotFoundError as exc:
                    self.skipTest(f"compute app dependencies are not installed: {exc}")
        app = getattr(app_mod, "app", None)
        if app is None or not hasattr(app, "test_client"):
            self.skipTest("real Flask test client is unavailable")

        response = app.test_client().get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertIn("text/plain", response.headers.get("Content-Type", ""))
        body = _response_text(response)
        self.assertIn("service=\"ibkr-compute\"", body)

    def test_route_label_normalization_strips_ids_and_query_values(self):
        module = _observability_or_skip(self, requiring=ROUTE_NORMALIZER_NAMES)
        normalizer = _first_callable(module, ROUTE_NORMALIZER_NAMES)
        self.assertIsNotNone(normalizer)

        label = str(
            _call_route_normalizer(
                normalizer,
                "/api/custom/ibkr/orders/ord_9d9f31ff?symbol=AAPL&account=DU123456",
                rule="/api/custom/ibkr/orders/<order_id>",
            )
        )

        self.assertIn("/api/custom/ibkr/orders", label)
        self.assertNotIn("ord_9d9f31ff", label)
        self.assertNotIn("AAPL", label)
        self.assertNotIn("DU123456", label)
        self.assertNotIn("?", label)
        self.assertRegex(label, r"/api/custom/ibkr/orders/(<[^>]+>|\{[^}]+\}|:[^/]+|\*)")

    def test_compute_backtest_profile_maps_to_backtest_service_label(self):
        module = _observability_or_skip(self, requiring=SERVICE_LABEL_NAMES)
        mapper = _first_callable(module, SERVICE_LABEL_NAMES)
        self.assertIsNotNone(mapper)

        with mock.patch.dict(os.environ, {"IBKR_SERVICE_PROFILE": "backtest", "IBKR_SERVICE_NAME": ""}, clear=False):
            labels = _call_service_mapper(mapper, service_name="", service_profile="backtest")
        service = str(_field(labels, "service", "service_name", "app", "name", "value"))
        profile = str(_field(labels, "service_profile", "profile") or "backtest")

        self.assertEqual("backtest", profile)
        self.assertEqual("ibkr-backtest", service)

    def test_flask_metrics_use_route_templates_and_backtest_profile_mapping(self):
        Flask = _flask_or_skip(self)
        app = Flask("compute-prometheus-test")
        _install_metrics(self, app, service_name="backtest", service_profile="backtest")

        @app.route("/backtest/run/<run_id>")
        def backtest_run(run_id):
            return {"ok": True, "run_id": run_id}

        client = app.test_client()
        client.get("/backtest/run/run_20260604_abcdef?symbol=AAPL&request_id=req-123")
        client.get("/backtest/run/run_20260604_ghijkl?symbol=MSFT&request_id=req-456")
        response = client.get("/metrics")
        body = _response_text(response)

        self.assertEqual(200, response.status_code)
        self.assertIn("service=\"ibkr-backtest\"", body)
        self.assertTrue(
            any(route in body for route in ("/backtest/run/<run_id>", "/backtest/run/{run_id}", "/backtest/run/:run_id")),
            body,
        )
        for raw_value in ("run_20260604_abcdef", "run_20260604_ghijkl", "AAPL", "MSFT", "req-123", "req-456"):
            self.assertNotIn(raw_value, body)

    def test_high_cardinality_label_denylist_filters_compute_identifiers(self):
        module = _observability_or_skip(self)
        denylist = _get_denylist(module)
        sanitizer = _first_callable(module, SANITIZER_NAMES)
        required = {"symbol", "order_id", "signal_id", "run_id", "account_id", "request_id", "trace_id", "query", "raw_path"}

        if denylist:
            self.assertTrue(required.issubset(denylist), denylist)
            return
        if sanitizer is not None:
            filtered = _sanitize_labels(
                sanitizer,
                {
                    "service": "ibkr-compute",
                    "service_profile": "compute",
                    "method": "GET",
                    "route": "/ibkr/account/<account_id>",
                    "symbol": "AAPL",
                    "order_id": "ord-123",
                    "signal_id": "sig-456",
                    "run_id": "run-789",
                    "account_id": "DU123456",
                    "request_id": "req-abc",
                    "trace_id": "trace-def",
                    "query": "symbol=AAPL",
                    "raw_path": "/ibkr/account/DU123456?symbol=AAPL",
                },
            )
            self.assertEqual("ibkr-compute", filtered.get("service"))
            self.assertEqual("GET", filtered.get("method"))
            self.assertIn("route", filtered)
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

    def test_account_and_gateway_metric_helpers_use_low_cardinality_labels(self):
        module = _observability_or_skip(
            self,
            requiring=("set_account_snapshot_metrics", "set_runtime_status_metrics"),
        )

        account_labels = set(getattr(getattr(module, "ACCOUNT_BUYING_POWER_REMAINING", None), "_labelnames", None) or ())
        capital_metric_names = (
            "ACCOUNT_AVAILABLE_FUNDS",
            "ACCOUNT_EXCESS_LIQUIDITY",
            "ACCOUNT_TOTAL_CASH",
            "ACCOUNT_GROSS_POSITION_VALUE",
            "ACCOUNT_INITIAL_MARGIN",
            "ACCOUNT_MAINTENANCE_MARGIN",
        )
        capital_label_sets = [
            set(getattr(getattr(module, name, None), "_labelnames", None) or ())
            for name in capital_metric_names
        ]
        state_labels = set(getattr(getattr(module, "ACCOUNT_BUYING_POWER_GUARD_STATE", None), "_labelnames", None) or ())
        position_current_labels = set(getattr(getattr(module, "POSITION_CURRENT_COUNT", None), "_labelnames", None) or ())
        order_current_labels = set(getattr(getattr(module, "ORDER_CURRENT_COUNT", None), "_labelnames", None) or ())
        signal_current_labels = set(getattr(getattr(module, "SIGNAL_CURRENT_COUNT", None), "_labelnames", None) or ())
        gateway_labels = set(getattr(getattr(module, "GATEWAY_SESSION_AUTHENTICATED", None), "_labelnames", None) or ())
        if (
            not account_labels
            or any(not labels for labels in capital_label_sets)
            or not state_labels
            or not position_current_labels
            or not order_current_labels
            or not signal_current_labels
            or not gateway_labels
        ):
            self.skipTest("prometheus_client label schemas are unavailable in this environment")

        denied = _get_denylist(module)
        for label_set in (
            account_labels,
            *capital_label_sets,
            state_labels,
            position_current_labels,
            order_current_labels,
            signal_current_labels,
            gateway_labels,
        ):
            self.assertFalse(denied.intersection(label_set), label_set)
        self.assertEqual({"service", "environment", "source"}, account_labels)
        for label_set in capital_label_sets:
            self.assertEqual({"service", "environment", "source"}, label_set)
        self.assertEqual({"service", "environment", "source", "state"}, state_labels)
        self.assertEqual({"service", "environment", "direction"}, position_current_labels)
        self.assertEqual({"service", "environment", "kind"}, order_current_labels)
        self.assertEqual({"service", "environment", "direction", "state"}, signal_current_labels)
        self.assertEqual({"service", "environment"}, gateway_labels)

    def test_market_data_subscription_metric_helpers_use_low_cardinality_labels(self):
        module = _observability_or_skip(
            self,
            requiring=("set_runtime_status_metrics",),
        )

        universe_labels = set(getattr(getattr(module, "MARKET_UNIVERSE_SYMBOLS", None), "_labelnames", None) or ())
        subscription_metric_names = (
            "MARKET_DATA_SUBSCRIPTION_ACTIVE",
            "MARKET_DATA_SUBSCRIPTION_LIMIT",
            "MARKET_DATA_SUBSCRIPTION_UTILIZATION",
            "MARKET_DATA_SUBSCRIPTION_PENDING",
        )
        subscription_label_sets = [
            set(getattr(getattr(module, name, None), "_labelnames", None) or ())
            for name in subscription_metric_names
        ]
        websocket_label_sets = [
            set(getattr(getattr(module, name, None), "_labelnames", None) or ())
            for name in ("MARKET_DATA_WS_LAST_MESSAGE_AGE", "MARKET_DATA_WS_LAST_TIC_AGE")
        ]
        bar_label_sets = [
            set(getattr(getattr(module, name, None), "_labelnames", None) or ())
            for name in ("MARKET_DATA_BAR_LAG", "MARKET_DATA_BAR_PENDING_SYMBOLS")
        ]
        if (
            not universe_labels
            or any(not labels for labels in subscription_label_sets)
            or any(not labels for labels in websocket_label_sets)
            or any(not labels for labels in bar_label_sets)
        ):
            self.skipTest("prometheus_client label schemas are unavailable in this environment")

        denied = _get_denylist(module)
        for label_set in (universe_labels, *subscription_label_sets, *websocket_label_sets, *bar_label_sets):
            self.assertFalse(denied.intersection(label_set), label_set)
        self.assertEqual({"service", "environment", "kind"}, universe_labels)
        for label_set in subscription_label_sets:
            self.assertEqual({"service", "environment", "kind"}, label_set)
        for label_set in websocket_label_sets:
            self.assertEqual({"service", "environment"}, label_set)
        for label_set in bar_label_sets:
            self.assertEqual({"service", "environment", "interval"}, label_set)

    def test_signal_record_counter_uses_low_cardinality_labels(self):
        module = _observability_or_skip(
            self,
            requiring=("record_signal_record_created",),
        )
        generate_latest = getattr(module, "generate_latest", None)
        if not callable(generate_latest):
            self.skipTest("prometheus_client generate_latest is unavailable in this environment")

        label_names = set(getattr(getattr(module, "SIGNAL_RECORDS", None), "_labelnames", None) or ())
        if not label_names:
            self.skipTest("prometheus_client label schemas are unavailable in this environment")

        denied = _get_denylist(module)
        self.assertFalse(denied.intersection(label_names), label_names)
        self.assertEqual({"service", "environment", "signal_source", "direction", "initial_status"}, label_names)

        environment = "metric_signal_records_test"
        with mock.patch.dict(
            os.environ,
            {"IBKR_SERVICE_PROFILE": "api", "IBKR_SERVICE_NAME": "", "IBKR_BROKER_MODE": ""},
            clear=False,
        ):
            module.record_signal_record_created(
                environment=environment,
                signal_source="tradingview_webhook",
                direction="long",
                initial_status="pending",
            )
        metrics_text = generate_latest().decode("utf-8", errors="replace")

        self.assertIn("ibkr_signal_records_total", metrics_text)
        self.assertIn('service="ibkr-api"', metrics_text)
        self.assertIn(f'environment="{environment}"', metrics_text)
        self.assertIn('signal_source="tradingview"', metrics_text)
        self.assertIn('direction="long"', metrics_text)
        self.assertIn('initial_status="pending"', metrics_text)

    def test_runtime_config_switch_gauge_uses_low_cardinality_labels(self):
        module = _observability_or_skip(
            self,
            requiring=("set_runtime_config_switch_metrics",),
        )
        generate_latest = getattr(module, "generate_latest", None)
        if not callable(generate_latest):
            self.skipTest("prometheus_client generate_latest is unavailable in this environment")

        label_names = set(getattr(getattr(module, "RUNTIME_CONFIG_SWITCH_ENABLED", None), "_labelnames", None) or ())
        if not label_names:
            self.skipTest("prometheus_client label schemas are unavailable in this environment")

        denied = _get_denylist(module)
        self.assertFalse(denied.intersection(label_names), label_names)
        self.assertEqual(
            {"service", "environment", "runtime_environment", "key", "mode_scope", "importance"},
            label_names,
        )

        environment = "metric_switch_config_test"
        with mock.patch.dict(
            os.environ,
            {"IBKR_SERVICE_PROFILE": "runtime", "IBKR_SERVICE_NAME": "", "IBKR_BROKER_MODE": ""},
            clear=False,
        ):
            module.set_runtime_config_switch_metrics(
                {
                    "environment": "paper",
                    "runtime_config_switches": {
                        "items": [
                            {
                                "key": "ibkr_trading_enabled",
                                "enabled": True,
                                "mode_scope": "broker",
                                "config_environment": environment,
                                "importance": "critical",
                            },
                            {
                                "key": "ibkr_order_flow_enabled",
                                "enabled": False,
                                "mode_scope": "broker",
                                "config_environment": environment,
                                "importance": "optional",
                            },
                        ]
                    },
                },
                environment="paper",
            )
        metrics_text = generate_latest().decode("utf-8", errors="replace")

        self.assertIn("ibkr_runtime_config_switch_enabled", metrics_text)
        self.assertIn('service="ibkr-runtime"', metrics_text)
        self.assertIn(f'environment="{environment}"', metrics_text)
        self.assertIn('runtime_environment="paper"', metrics_text)
        self.assertIn('key="ibkr_trading_enabled"', metrics_text)
        self.assertIn('mode_scope="broker"', metrics_text)
        self.assertIn('importance="critical"', metrics_text)

    def test_account_snapshot_metrics_publish_current_position_and_order_counts(self):
        module = _observability_or_skip(
            self,
            requiring=("set_account_snapshot_metrics",),
        )
        generate_latest = getattr(module, "generate_latest", None)
        if not callable(generate_latest):
            self.skipTest("prometheus_client generate_latest is unavailable in this environment")

        environment = "metric_current_snapshot_test"
        with mock.patch.dict(
            os.environ,
            {"IBKR_SERVICE_PROFILE": "runtime", "IBKR_SERVICE_NAME": "", "IBKR_BROKER_MODE": ""},
            clear=False,
        ):
            module.set_account_snapshot_metrics(
                {
                    "ok": True,
                    "environment": environment,
                    "summary": {
                        "available_funds": 10000,
                        "total_cash_value": 12000,
                        "remaining_buying_power": 50000,
                    },
                    "buying_power_guard": {"state": "ok", "source": "account_snapshot"},
                    "positions": [
                        {"symbol": "AAPL", "quantity": 10},
                        {"symbol": "MSFT", "quantity": -5},
                        {"symbol": "FLAT", "quantity": 0},
                    ],
                    "live_open_orders": [
                        {"order_id": "9001", "can_cancel": True, "can_modify": True},
                        {"order_id": "9002", "parent_id": "9001", "can_cancel": True, "can_modify": False},
                        {"order_id": "9003", "parent_id": "9001", "can_cancel": False, "can_modify": True},
                        {"order_id": "9004", "can_cancel": False, "can_modify": False},
                    ],
                    "live_order_groups": [
                        {"group_key": "runtime-group", "live_order_count": 4, "cancelable_orders": 2, "editable_orders": 2},
                    ],
                },
                source="account_snapshot",
                environment=environment,
            )
        metrics_text = generate_latest().decode("utf-8", errors="replace")

        self.assertRegex(metrics_text, rf'ibkr_position_current_count\{{(?=[^}}]*service="ibkr-runtime")(?=[^}}]*environment="{environment}")(?=[^}}]*direction="total")[^}}]*\}} 2\.0')
        self.assertRegex(metrics_text, rf'ibkr_position_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*direction="long")[^}}]*\}} 1\.0')
        self.assertRegex(metrics_text, rf'ibkr_position_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*direction="short")[^}}]*\}} 1\.0')
        self.assertRegex(metrics_text, rf'ibkr_order_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*kind="open_legs")[^}}]*\}} 4\.0')
        self.assertRegex(metrics_text, rf'ibkr_order_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*kind="open_groups")[^}}]*\}} 1\.0')
        self.assertRegex(metrics_text, rf'ibkr_order_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*kind="cancelable")[^}}]*\}} 2\.0')
        self.assertRegex(metrics_text, rf'ibkr_order_current_count\{{(?=[^}}]*environment="{environment}")(?=[^}}]*kind="editable")[^}}]*\}} 2\.0')
        self.assertIn("ibkr_account_buying_power_remaining_usd", metrics_text)

    def test_current_signal_count_gauge_publishes_long_short_snapshot(self):
        module = _observability_or_skip(
            self,
            requiring=("set_current_signal_count_metrics",),
        )
        generate_latest = getattr(module, "generate_latest", None)
        if not callable(generate_latest):
            self.skipTest("prometheus_client generate_latest is unavailable in this environment")

        environment = "metric_current_signal_test"
        module.set_current_signal_count_metrics(
            {"long": 2, "short": 1},
            environment=environment,
            state="active",
            service_name="ibkr-api",
        )
        metrics_text = generate_latest().decode("utf-8", errors="replace")

        self.assertRegex(metrics_text, rf'ibkr_signal_current_count\{{(?=[^}}]*service="ibkr-api")(?=[^}}]*environment="{environment}")(?=[^}}]*direction="long")(?=[^}}]*state="active")[^}}]*\}} 2\.0')
        self.assertRegex(metrics_text, rf'ibkr_signal_current_count\{{(?=[^}}]*service="ibkr-api")(?=[^}}]*environment="{environment}")(?=[^}}]*direction="short")(?=[^}}]*state="active")[^}}]*\}} 1\.0')

    def test_runtime_status_metrics_publish_market_data_subscription_values(self):
        module = _observability_or_skip(
            self,
            requiring=("set_runtime_status_metrics",),
        )
        generate_latest = getattr(module, "generate_latest", None)
        if not callable(generate_latest):
            self.skipTest("prometheus_client generate_latest is unavailable in this environment")

        environment = "metric_test"
        module.set_runtime_status_metrics(
            {
                "environment": environment,
                "websocket": {
                    "ready": True,
                    "subscribed_count": 5,
                    "pending_count": 1,
                    "last_message_age_s": 12,
                    "last_tic_age_s": 4,
                },
                "market_universe": {
                    "active_subscription_count": 7,
                    "active_trade_symbol_count": 4,
                    "watchlist_pool_count": 276,
                    "watchlist_trade_count": 273,
                    "active_monitor_symbol_count": 3,
                    "target_subscription_limit": 10,
                    "total_subscription_limit": 20,
                    "trade_subscription_limit": 10,
                    "market_monitor_subscription_limit": 5,
                    "bar_freshness": {"lag_s": 45, "pending_symbols_total": 2},
                },
            },
            environment=environment,
        )
        metrics_text = generate_latest().decode("utf-8", errors="replace")

        self.assertIn('ibkr_market_universe_symbols_count', metrics_text)
        self.assertIn(f'environment="{environment}",kind="pool"', metrics_text)
        self.assertIn(f'environment="{environment}",kind="total"', metrics_text)
        self.assertIn('ibkr_market_data_subscription_active_count', metrics_text)
        self.assertIn('ibkr_market_data_subscription_utilization_pct', metrics_text)
        self.assertIn('ibkr_market_data_websocket_last_message_age_seconds', metrics_text)
        self.assertIn('ibkr_market_data_bar_lag_seconds', metrics_text)


if __name__ == "__main__":
    unittest.main()
