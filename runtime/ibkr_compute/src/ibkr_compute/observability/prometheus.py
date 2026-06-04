from __future__ import annotations

import functools
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

try:  # prometheus-client is added to runtime requirements; keep imports safe for local tools.
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except Exception:  # pragma: no cover - exercised only on hosts before dependency install.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    Counter = Gauge = Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NUM_RE = re.compile(r"^\d+$")
_HEXISH_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_PATCH_LOCK = threading.Lock()
_REQUESTS_PATCHED = False
_ORIGINAL_REQUEST = None
_INSTALLED_APPS: set[int] = set()

_SERVICE_PROFILE_MAP = {
    "api": "ibkr-api",
    "compute": "ibkr-compute",
    "runtime": "ibkr-runtime",
    "scheduler": "ibkr-scheduler",
    "backtest": "ibkr-backtest",
    "console": "ibkr-console",
}

HIGH_CARDINALITY_LABEL_DENYLIST = {
    "account",
    "account_id",
    "advancedOrderRejectJson",
    "broker_order_id",
    "bracket_group",
    "conid",
    "cOID",
    "coid",
    "errorString",
    "id",
    "oca_group",
    "operation_id",
    "order_id",
    "orderRef",
    "path",
    "query",
    "raw_path",
    "record_id",
    "req_id",
    "request_id",
    "reverse_id",
    "run_id",
    "signal_id",
    "symbol",
    "ticker",
    "timestamp",
    "trace_id",
    "trade_group_id",
    "unique_id",
    "url",
}
HIGH_CARDINALITY_DENYLIST = HIGH_CARDINALITY_LABEL_DENYLIST
METRIC_LABEL_DENYLIST = HIGH_CARDINALITY_LABEL_DENYLIST
DENIED_LABEL_NAMES = HIGH_CARDINALITY_LABEL_DENYLIST

_DEFAULT_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)


def _enabled() -> bool:
    return str(os.environ.get("IBKR_PROMETHEUS_METRICS_ENABLED", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _client_available() -> bool:
    return bool(Counter and Gauge and Histogram and generate_latest)


def resolve_source_service(service_name: str | None = None) -> str:
    explicit = str(service_name or os.environ.get("IBKR_SERVICE_NAME") or "").strip()
    if explicit:
        return explicit if explicit.startswith("ibkr-") or explicit in {"pocketbase", "prometheus", "grafana"} else _SERVICE_PROFILE_MAP.get(explicit, explicit)
    profile = str(os.environ.get("IBKR_SERVICE_PROFILE") or "").strip().lower()
    if profile:
        return _SERVICE_PROFILE_MAP.get(profile, f"ibkr-{profile}")
    port = str(os.environ.get("PORT") or "").strip()
    return {
        "5100": "ibkr-compute",
        "5101": "ibkr-runtime",
        "5102": "ibkr-api",
        "5103": "ibkr-scheduler",
        "5105": "ibkr-backtest",
    }.get(port, "ibkr-service")


def _configured_ports() -> dict[str, str]:
    return {
        str(os.environ.get("IBKR_COMPUTE_PORT") or "5100"): "ibkr-compute",
        str(os.environ.get("IBKR_RUNTIME_PORT") or "5101"): "ibkr-runtime",
        str(os.environ.get("IBKR_API_PORT") or "5102"): "ibkr-api",
        str(os.environ.get("IBKR_SCHEDULER_PORT") or "5103"): "ibkr-scheduler",
        str(os.environ.get("IBKR_BACKTEST_PORT") or "5105"): "ibkr-backtest",
        "5104": "ibkr-console",
        "8090": "pocketbase",
        "3000": "grafana",
        "9090": "prometheus",
        "9100": "node-exporter",
    }


def resolve_target_service(url: Any) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return "unknown"
    host = (parsed.hostname or "").lower()
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return _configured_ports().get(port, "local")
    if "pocketbase" in host or host.startswith("pb."):
        return "pocketbase"
    if "quant-monitor" in host:
        return "grafana"
    if "quant" in host:
        return "ibkr-api"
    return "external"


def _status_class(status_code: Any) -> str:
    try:
        code = int(status_code or 0)
    except Exception:
        code = 0
    if code <= 0:
        return "0xx"
    return f"{code // 100}xx"


def _exception_class(exc: Any) -> str:
    name = exc.__class__.__name__ if exc is not None else "Exception"
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name or "Exception"))[:80]


def _sanitize_label(value: Any, default: str = "unknown") -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_./:<>{}|=-]", "_", text)
    return text[:160] or default


def _sanitize_reason_code(value: Any, default: str = "unknown") -> str:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return default
    if len(raw) > 96 or any(token in raw for token in ("?", "&", "=", "{", "}", "[", "]", '"', "'", "\n")):
        return default
    if " " in raw or "\t" in raw:
        return default
    text = _sanitize_label(raw, default)
    if _UUID_RE.match(text) or _HEXISH_RE.match(text) or _ID_RE.match(text):
        return default
    return text[:96] or default


def sanitize_metric_labels(labels: dict[str, Any] | None = None, **extra: Any) -> dict[str, str]:
    safe: dict[str, str] = {}
    combined: dict[str, Any] = {}
    if isinstance(labels, dict):
        combined.update(labels)
    combined.update(extra)
    for key, value in combined.items():
        label_name = str(key or "").strip()
        if not label_name or label_name in HIGH_CARDINALITY_LABEL_DENYLIST:
            continue
        if label_name in {"route", "target_route", "normalized_route"}:
            safe[label_name] = normalize_path(value)
        elif label_name in {"reason_code", "error_class"}:
            safe[label_name] = _sanitize_reason_code(value, "unknown")
        else:
            safe[label_name] = _sanitize_label(value)
    return safe


sanitize_labels = sanitize_metric_labels
filter_metric_labels = sanitize_metric_labels
safe_metric_labels = sanitize_metric_labels


def normalize_flask_route() -> str:
    try:
        from flask import request

        rule = getattr(getattr(request, "url_rule", None), "rule", "") or ""
        if rule:
            return str(rule)
        return normalize_path(getattr(request, "path", "") or "/")
    except Exception:
        return "unknown"


def _normalize_segment(segment: str, previous: str = "") -> str:
    if not segment:
        return segment
    lower_prev = previous.lower()
    if _UUID_RE.match(segment):
        return "{uuid}"
    if _NUM_RE.match(segment):
        return "{id}"
    if _HEXISH_RE.match(segment):
        return "{hex}"
    if _ID_RE.match(segment) and lower_prev in {
        "records",
        "runs",
        "batches",
        "orders",
        "signals",
        "targets",
        "watchlist",
        "states",
        "jobs",
    }:
        return "{id}"
    return segment


def normalize_path(path: Any) -> str:
    raw = str(path or "/").split("?", 1)[0]
    if not raw.startswith("/"):
        raw = f"/{raw}"
    parts = [part for part in raw.split("/") if part]
    normalized: list[str] = []
    for part in parts[:12]:
        previous = normalized[-1] if normalized else ""
        normalized.append(_normalize_segment(part, previous))
    return "/" + "/".join(normalized) if normalized else "/"


def normalize_outbound_route(url: Any) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return "unknown"
    path = normalize_path(parsed.path or "/")
    if path.startswith("/api/collections/"):
        pieces = path.split("/")
        if len(pieces) >= 6 and pieces[4] == "records":
            pieces[5] = "{record_id}"
            path = "/".join(pieces)
    return path


if _client_available():
    SERVICE_INFO = Gauge(
        "ibkr_service_info",
        "IBKR service metadata marker for Prometheus scrapes.",
        ("service",),
    )
    HTTP_SERVER_REQUESTS = Counter(
        "ibkr_http_server_requests_total",
        "Inbound HTTP requests handled by IBKR services.",
        ("service", "method", "route", "status_code", "status_class"),
    )
    HTTP_SERVER_DURATION = Histogram(
        "ibkr_http_server_request_duration_seconds",
        "Inbound HTTP request duration for IBKR services.",
        ("service", "method", "route", "status_class"),
        buckets=_DEFAULT_BUCKETS,
    )
    HTTP_SERVER_IN_FLIGHT = Gauge(
        "ibkr_http_server_requests_in_flight",
        "Inbound HTTP requests currently in flight.",
        ("service", "method", "route"),
    )
    HTTP_SERVER_EXCEPTIONS = Counter(
        "ibkr_http_server_exceptions_total",
        "Inbound HTTP exceptions raised by IBKR services.",
        ("service", "method", "route", "exception_class"),
    )
    HTTP_CLIENT_REQUESTS = Counter(
        "ibkr_http_client_requests_total",
        "Outbound HTTP requests made by IBKR services.",
        ("service", "target_service", "method", "route", "status_code", "status_class", "outcome"),
    )
    HTTP_CLIENT_DURATION = Histogram(
        "ibkr_http_client_request_duration_seconds",
        "Outbound HTTP request duration for IBKR services.",
        ("service", "target_service", "method", "route", "status_class", "outcome"),
        buckets=_DEFAULT_BUCKETS,
    )
    HTTP_CLIENT_IN_FLIGHT = Gauge(
        "ibkr_http_client_requests_in_flight",
        "Outbound HTTP requests currently in flight.",
        ("service", "target_service", "method", "route"),
    )
    HTTP_CLIENT_EXCEPTIONS = Counter(
        "ibkr_http_client_exceptions_total",
        "Outbound HTTP exceptions raised by IBKR services.",
        ("service", "target_service", "method", "route", "exception_class"),
    )
    GATEWAY_SOCKET_PROBES = Counter(
        "ibkr_gateway_socket_probe_total",
        "IB Gateway socket probe results.",
        ("service", "environment", "host", "port", "source", "result", "reason_code"),
    )
    GATEWAY_SOCKET_DURATION = Histogram(
        "ibkr_gateway_socket_probe_duration_seconds",
        "IB Gateway socket probe duration.",
        ("service", "environment", "source", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    GATEWAY_SOCKET_LISTENING = Gauge(
        "ibkr_gateway_socket_listening",
        "Whether the IB Gateway API socket is listening.",
        ("service", "environment", "host", "port"),
    )
    GATEWAY_SERVICE_ACTIONS = Counter(
        "ibkr_gateway_service_action_total",
        "Gateway systemd/service actions.",
        ("service", "environment", "action", "result"),
    )
    GATEWAY_SERVICE_RUNNING = Gauge(
        "ibkr_gateway_service_running",
        "Whether the gateway systemd service is running.",
        ("service", "environment"),
    )
    GATEWAY_SERVICE_UPTIME = Gauge(
        "ibkr_gateway_service_uptime_seconds",
        "Gateway service uptime seconds.",
        ("service", "environment"),
    )
    GATEWAY_STATUS_CODE = Gauge(
        "ibkr_gateway_status_code",
        "Gateway status code reported by runtime.",
        ("service", "environment"),
    )
    BROKER_CONNECTS = Counter(
        "ibkr_broker_connect_attempts_total",
        "IB API broker connect attempts.",
        ("service", "environment", "client_role", "client_id", "result", "status_code", "reason_code"),
    )
    BROKER_CONNECT_DURATION = Histogram(
        "ibkr_broker_connect_duration_seconds",
        "IB API broker connect duration.",
        ("service", "environment", "client_role", "result", "status_code"),
        buckets=_DEFAULT_BUCKETS,
    )
    BROKER_READY = Gauge(
        "ibkr_broker_ready",
        "Whether the IB API client is ready.",
        ("service", "environment", "client_role", "client_id"),
    )
    BROKER_CONNECTED = Gauge(
        "ibkr_broker_connected",
        "Whether the IB API client is connected.",
        ("service", "environment", "client_role", "client_id"),
    )
    BROKER_DISCONNECTS = Counter(
        "ibkr_broker_disconnect_total",
        "IB API broker disconnects.",
        ("service", "environment", "client_role", "source", "reason_code"),
    )
    BROKER_ERRORS = Counter(
        "ibkr_broker_error_total",
        "IB API error callback counts.",
        ("service", "environment", "client_role", "ib_error_code", "request_kind", "severity"),
    )
    BROKER_REQUESTS = Counter(
        "ibkr_broker_request_total",
        "IB API synchronous request counts.",
        ("service", "environment", "client_role", "request_kind", "result", "error_class"),
    )
    BROKER_REQUEST_DURATION = Histogram(
        "ibkr_broker_request_duration_seconds",
        "IB API synchronous request duration.",
        ("service", "environment", "client_role", "request_kind", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    BROKER_PENDING_REQUESTS = Gauge(
        "ibkr_broker_pending_requests",
        "IB API pending synchronous requests.",
        ("service", "environment", "client_role", "request_kind"),
    )
    HISTORY_EVENTS = Counter(
        "ibkr_history_events_total",
        "History/backfill events.",
        ("service", "environment", "source", "interval", "operation", "result", "error_class"),
    )
    HISTORY_DURATION = Histogram(
        "ibkr_history_operation_duration_seconds",
        "History/backfill operation duration.",
        ("service", "environment", "source", "interval", "operation", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    HISTORY_ACTIVE = Gauge(
        "ibkr_history_active_requests",
        "Active IBKR history requests.",
        ("service", "environment", "source"),
    )
    HISTORY_ROWS = Counter(
        "ibkr_history_rows_total",
        "History rows fetched or written.",
        ("service", "environment", "source", "interval", "operation"),
    )
    ORDER_EVENTS = Counter(
        "ibkr_order_events_total",
        "Order lifecycle, broker, and tracker events.",
        ("service", "environment", "operation", "order_family_type", "result", "reason_code"),
    )
    ORDER_DURATION = Histogram(
        "ibkr_order_operation_duration_seconds",
        "Order operation duration.",
        ("service", "environment", "operation", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    SIGNAL_EVENTS = Counter(
        "ibkr_signal_events_total",
        "Signal processing events.",
        ("service", "environment", "stage", "signal_source", "result", "reason_code"),
    )
    SIGNAL_DURATION = Histogram(
        "ibkr_signal_stage_duration_seconds",
        "Signal processing stage duration.",
        ("service", "environment", "stage", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
else:  # pragma: no cover
    SERVICE_INFO = None
    HTTP_SERVER_REQUESTS = HTTP_SERVER_DURATION = HTTP_SERVER_IN_FLIGHT = HTTP_SERVER_EXCEPTIONS = None
    HTTP_CLIENT_REQUESTS = HTTP_CLIENT_DURATION = HTTP_CLIENT_IN_FLIGHT = HTTP_CLIENT_EXCEPTIONS = None
    GATEWAY_SOCKET_PROBES = GATEWAY_SOCKET_DURATION = GATEWAY_SOCKET_LISTENING = None
    GATEWAY_SERVICE_ACTIONS = GATEWAY_SERVICE_RUNNING = GATEWAY_SERVICE_UPTIME = GATEWAY_STATUS_CODE = None
    BROKER_CONNECTS = BROKER_CONNECT_DURATION = BROKER_READY = BROKER_CONNECTED = BROKER_DISCONNECTS = BROKER_ERRORS = None
    BROKER_REQUESTS = BROKER_REQUEST_DURATION = BROKER_PENDING_REQUESTS = None
    HISTORY_EVENTS = HISTORY_DURATION = HISTORY_ACTIVE = HISTORY_ROWS = None
    ORDER_EVENTS = ORDER_DURATION = SIGNAL_EVENTS = SIGNAL_DURATION = None


def install_flask_metrics(app: Any, service_name: str | None = None) -> None:
    if not _enabled():
        return
    app_id = id(app)
    if app_id in _INSTALLED_APPS:
        return
    _INSTALLED_APPS.add(app_id)
    service = resolve_source_service(service_name)
    if SERVICE_INFO is not None:
        SERVICE_INFO.labels(service).set(1.0)
    install_requests_metrics()

    @app.before_request
    def _ibkr_observability_before_request():  # type: ignore[unused-ignore]
        try:
            from flask import g, request

            if request.path == "/metrics":
                return None
            route = normalize_flask_route()
            method = _sanitize_label(request.method, "GET")
            g.ibkr_metrics_started = time.perf_counter()
            g.ibkr_metrics_route = route
            g.ibkr_metrics_method = method
            if HTTP_SERVER_IN_FLIGHT is not None:
                HTTP_SERVER_IN_FLIGHT.labels(service, method, route).inc()
        except Exception:
            return None
        return None

    @app.after_request
    def _ibkr_observability_after_request(response):  # type: ignore[unused-ignore]
        try:
            from flask import g, request

            if request.path == "/metrics":
                return response
            started = getattr(g, "ibkr_metrics_started", None)
            route = getattr(g, "ibkr_metrics_route", None) or normalize_flask_route()
            method = getattr(g, "ibkr_metrics_method", None) or _sanitize_label(request.method, "GET")
            status_code = int(getattr(response, "status_code", 0) or 0)
            status_class = _status_class(status_code)
            if HTTP_SERVER_REQUESTS is not None:
                HTTP_SERVER_REQUESTS.labels(service, method, route, str(status_code), status_class).inc()
            if HTTP_SERVER_DURATION is not None and started is not None:
                HTTP_SERVER_DURATION.labels(service, method, route, status_class).observe(max(0.0, time.perf_counter() - float(started)))
        except Exception:
            return response
        return response

    @app.teardown_request
    def _ibkr_observability_teardown_request(exc):  # type: ignore[unused-ignore]
        try:
            from flask import g, request

            if request.path == "/metrics":
                return None
            route = getattr(g, "ibkr_metrics_route", None) or normalize_flask_route()
            method = getattr(g, "ibkr_metrics_method", None) or _sanitize_label(request.method, "GET")
            if exc is not None and HTTP_SERVER_EXCEPTIONS is not None:
                HTTP_SERVER_EXCEPTIONS.labels(service, method, route, _exception_class(exc)).inc()
            if HTTP_SERVER_IN_FLIGHT is not None:
                HTTP_SERVER_IN_FLIGHT.labels(service, method, route).dec()
        except Exception:
            return None
        return None

    @app.route("/metrics", methods=["GET"])
    def ibkr_prometheus_metrics():  # type: ignore[unused-ignore]
        if generate_latest is None:
            return "# prometheus_client_missing 1\n", 503, {"Content-Type": CONTENT_TYPE_LATEST}
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


def install_requests_metrics() -> None:
    global _ORIGINAL_REQUEST, _REQUESTS_PATCHED
    if not _enabled() or not _client_available():
        return
    with _PATCH_LOCK:
        if _REQUESTS_PATCHED:
            return
        try:
            import requests
        except Exception:
            return
        _ORIGINAL_REQUEST = requests.sessions.Session.request

        @functools.wraps(_ORIGINAL_REQUEST)
        def _observed_request(session, method, url, **kwargs):
            service = resolve_source_service()
            target_service = resolve_target_service(url)
            route = normalize_outbound_route(url)
            method_label = _sanitize_label(str(method or "GET").upper(), "GET")
            started = time.perf_counter()
            if HTTP_CLIENT_IN_FLIGHT is not None:
                HTTP_CLIENT_IN_FLIGHT.labels(service, target_service, method_label, route).inc()
            try:
                response = _ORIGINAL_REQUEST(session, method, url, **kwargs)
            except Exception as exc:
                elapsed = max(0.0, time.perf_counter() - started)
                if HTTP_CLIENT_REQUESTS is not None:
                    HTTP_CLIENT_REQUESTS.labels(service, target_service, method_label, route, "0", "0xx", "exception").inc()
                if HTTP_CLIENT_DURATION is not None:
                    HTTP_CLIENT_DURATION.labels(service, target_service, method_label, route, "0xx", "exception").observe(elapsed)
                if HTTP_CLIENT_EXCEPTIONS is not None:
                    HTTP_CLIENT_EXCEPTIONS.labels(service, target_service, method_label, route, _exception_class(exc)).inc()
                raise
            else:
                status_code = int(getattr(response, "status_code", 0) or 0)
                status_class = _status_class(status_code)
                outcome = "success" if status_code < 400 else "http_error"
                elapsed = max(0.0, time.perf_counter() - started)
                if HTTP_CLIENT_REQUESTS is not None:
                    HTTP_CLIENT_REQUESTS.labels(service, target_service, method_label, route, str(status_code), status_class, outcome).inc()
                if HTTP_CLIENT_DURATION is not None:
                    HTTP_CLIENT_DURATION.labels(service, target_service, method_label, route, status_class, outcome).observe(elapsed)
                return response
            finally:
                if HTTP_CLIENT_IN_FLIGHT is not None:
                    HTTP_CLIENT_IN_FLIGHT.labels(service, target_service, method_label, route).dec()

        requests.sessions.Session.request = _observed_request
        _REQUESTS_PATCHED = True


def _env_from_obj(obj: Any = None, default: str = "") -> str:
    for attr in ("environment", "broker_mode", "mode"):
        value = getattr(obj, attr, None) if obj is not None else None
        if value:
            return _sanitize_label(value, default or "unknown")
    return _sanitize_label(default or os.environ.get("IBKR_BROKER_MODE") or os.environ.get("BROKER_MODE") or "unknown")


def _client_role(obj: Any = None) -> str:
    profile = str(os.environ.get("IBKR_SERVICE_PROFILE") or "").strip().lower()
    if profile:
        return _sanitize_label(profile)
    return _sanitize_label(getattr(obj, "client_role", None) or "runtime")


def _client_id(obj: Any = None) -> str:
    return _sanitize_label(getattr(obj, "client_id", None) or os.environ.get("IBGW_CLIENT_ID") or "0")


def record_gateway_socket_probe(*, environment: str = "", host: Any = "", port: Any = "", source: str = "status", result: str = "unknown", reason_code: str = "", duration_s: float | None = None, listening: bool | None = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    host_label = _sanitize_label(host or "unknown")
    port_label = _sanitize_label(port or "0")
    result_label = _sanitize_label(result)
    source_label = _sanitize_label(source)
    reason = _sanitize_reason_code(reason_code or result_label, result_label)
    if GATEWAY_SOCKET_PROBES is not None:
        GATEWAY_SOCKET_PROBES.labels(service, env, host_label, port_label, source_label, result_label, reason).inc()
    if GATEWAY_SOCKET_DURATION is not None and duration_s is not None:
        GATEWAY_SOCKET_DURATION.labels(service, env, source_label, result_label).observe(max(0.0, float(duration_s or 0.0)))
    if GATEWAY_SOCKET_LISTENING is not None and listening is not None:
        GATEWAY_SOCKET_LISTENING.labels(service, env, host_label, port_label).set(1.0 if listening else 0.0)


def record_gateway_service_action(*, environment: str = "", action: str, result: str) -> None:
    if GATEWAY_SERVICE_ACTIONS is not None:
        GATEWAY_SERVICE_ACTIONS.labels(resolve_source_service(), _sanitize_label(environment or "unknown"), _sanitize_label(action), _sanitize_label(result)).inc()


def set_gateway_status(*, environment: str = "", running: Any = None, uptime_s: Any = None, status_code: Any = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or "unknown")
    if GATEWAY_SERVICE_RUNNING is not None and running is not None:
        GATEWAY_SERVICE_RUNNING.labels(service, env).set(1.0 if bool(running) else 0.0)
    if GATEWAY_SERVICE_UPTIME is not None and uptime_s is not None:
        try:
            GATEWAY_SERVICE_UPTIME.labels(service, env).set(max(0.0, float(uptime_s or 0.0)))
        except Exception:
            pass
    if GATEWAY_STATUS_CODE is not None and status_code is not None:
        try:
            GATEWAY_STATUS_CODE.labels(service, env).set(float(status_code or 0))
        except Exception:
            pass


def record_broker_connect(obj: Any = None, *, result: str, duration_s: float, status_code: Any = 0, reason_code: str = "") -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _env_from_obj(obj)
    role = _client_role(obj)
    cid = _client_id(obj)
    result_label = _sanitize_label(result)
    code = _sanitize_label(status_code or 0)
    reason = _sanitize_reason_code(reason_code or result_label, result_label)
    if BROKER_CONNECTS is not None:
        BROKER_CONNECTS.labels(service, env, role, cid, result_label, code, reason).inc()
    if BROKER_CONNECT_DURATION is not None:
        BROKER_CONNECT_DURATION.labels(service, env, role, result_label, code).observe(max(0.0, float(duration_s or 0.0)))
    if BROKER_READY is not None:
        BROKER_READY.labels(service, env, role, cid).set(1.0 if result_label == "ok" else 0.0)
    if BROKER_CONNECTED is not None:
        BROKER_CONNECTED.labels(service, env, role, cid).set(1.0 if result_label == "ok" else 0.0)


def record_broker_disconnect(obj: Any = None, *, source: str = "unknown", reason_code: str = "disconnect") -> None:
    if BROKER_DISCONNECTS is not None:
        BROKER_DISCONNECTS.labels(resolve_source_service(), _env_from_obj(obj), _client_role(obj), _sanitize_label(source), _sanitize_reason_code(reason_code, "disconnect")).inc()
    if BROKER_CONNECTED is not None:
        BROKER_CONNECTED.labels(resolve_source_service(), _env_from_obj(obj), _client_role(obj), _client_id(obj)).set(0.0)


def record_broker_error(obj: Any = None, *, ib_error_code: Any, request_kind: str = "unknown", severity: str = "warning") -> None:
    if BROKER_ERRORS is not None:
        BROKER_ERRORS.labels(resolve_source_service(), _env_from_obj(obj), _client_role(obj), _sanitize_label(ib_error_code or 0), _sanitize_label(request_kind), _sanitize_label(severity)).inc()


def record_broker_request(obj: Any = None, *, request_kind: str, result: str, duration_s: float | None = None, error_class: str = "") -> None:
    if not _client_available():
        return
    labels = (resolve_source_service(), _env_from_obj(obj), _client_role(obj), _sanitize_label(request_kind), _sanitize_label(result), _sanitize_reason_code(error_class or result, _sanitize_label(result)))
    if BROKER_REQUESTS is not None:
        BROKER_REQUESTS.labels(*labels).inc()
    if BROKER_REQUEST_DURATION is not None and duration_s is not None:
        BROKER_REQUEST_DURATION.labels(labels[0], labels[1], labels[2], labels[3], labels[4]).observe(max(0.0, float(duration_s or 0.0)))


def set_broker_pending(obj: Any = None, *, request_kind: str, value: Any) -> None:
    if BROKER_PENDING_REQUESTS is None:
        return
    try:
        BROKER_PENDING_REQUESTS.labels(resolve_source_service(), _env_from_obj(obj), _client_role(obj), _sanitize_label(request_kind)).set(max(0.0, float(value or 0.0)))
    except Exception:
        pass


def record_history_event(*, environment: str = "", source: str = "runtime", interval: str = "", operation: str, result: str, duration_s: float | None = None, rows: Any = None, error_class: str = "") -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    src = _sanitize_label(source or "runtime")
    interval_label = _sanitize_label(interval or "unknown")
    op = _sanitize_label(operation)
    result_label = _sanitize_label(result)
    err = _sanitize_reason_code(error_class or result_label, result_label)
    if HISTORY_EVENTS is not None:
        HISTORY_EVENTS.labels(service, env, src, interval_label, op, result_label, err).inc()
    if HISTORY_DURATION is not None and duration_s is not None:
        HISTORY_DURATION.labels(service, env, src, interval_label, op, result_label).observe(max(0.0, float(duration_s or 0.0)))
    if HISTORY_ROWS is not None and rows is not None:
        try:
            count = max(0.0, float(rows or 0.0))
        except Exception:
            count = 0.0
        if count:
            HISTORY_ROWS.labels(service, env, src, interval_label, op).inc(count)


def set_history_active(*, environment: str = "", source: str = "runtime", value: Any) -> None:
    if HISTORY_ACTIVE is None:
        return
    try:
        HISTORY_ACTIVE.labels(resolve_source_service(), _sanitize_label(environment or "unknown"), _sanitize_label(source or "runtime")).set(max(0.0, float(value or 0.0)))
    except Exception:
        pass


def record_order_event(*, environment: str = "", operation: str, order_family_type: str = "unknown", result: str, reason_code: str = "", duration_s: float | None = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    op = _sanitize_label(operation)
    result_label = _sanitize_label(result)
    if ORDER_EVENTS is not None:
        ORDER_EVENTS.labels(service, env, op, _sanitize_label(order_family_type or "unknown"), result_label, _sanitize_reason_code(reason_code or result_label, result_label)).inc()
    if ORDER_DURATION is not None and duration_s is not None:
        ORDER_DURATION.labels(service, env, op, result_label).observe(max(0.0, float(duration_s or 0.0)))


def record_signal_event(*, environment: str = "", stage: str, signal_source: str = "unknown", result: str, reason_code: str = "", duration_s: float | None = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    stage_label = _sanitize_label(stage)
    result_label = _sanitize_label(result)
    if SIGNAL_EVENTS is not None:
        SIGNAL_EVENTS.labels(service, env, stage_label, _sanitize_label(signal_source or "unknown"), result_label, _sanitize_reason_code(reason_code or result_label, result_label)).inc()
    if SIGNAL_DURATION is not None and duration_s is not None:
        SIGNAL_DURATION.labels(service, env, stage_label, result_label).observe(max(0.0, float(duration_s or 0.0)))


instrument_flask_app = install_flask_metrics
register_flask_metrics = install_flask_metrics
register_prometheus_metrics = install_flask_metrics
setup_prometheus_metrics = install_flask_metrics
init_prometheus_metrics = install_flask_metrics


__all__ = [
    "DENIED_LABEL_NAMES",
    "HIGH_CARDINALITY_DENYLIST",
    "HIGH_CARDINALITY_LABEL_DENYLIST",
    "METRIC_LABEL_DENYLIST",
    "filter_metric_labels",
    "init_prometheus_metrics",
    "install_flask_metrics",
    "install_requests_metrics",
    "instrument_flask_app",
    "normalize_flask_route",
    "normalize_outbound_route",
    "normalize_path",
    "record_broker_connect",
    "record_broker_disconnect",
    "record_broker_error",
    "record_broker_request",
    "record_gateway_service_action",
    "record_gateway_socket_probe",
    "record_history_event",
    "record_order_event",
    "record_signal_event",
    "register_flask_metrics",
    "register_prometheus_metrics",
    "resolve_source_service",
    "resolve_target_service",
    "safe_metric_labels",
    "sanitize_labels",
    "sanitize_metric_labels",
    "set_broker_pending",
    "set_gateway_status",
    "set_history_active",
    "setup_prometheus_metrics",
]
