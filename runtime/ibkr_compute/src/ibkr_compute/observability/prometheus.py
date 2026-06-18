from __future__ import annotations

import functools
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ibkr_compute.observability.ib_error_catalog import describe_ib_error

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
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
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


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return float(number)


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


def _sanitize_display_label(value: Any, default: str = "unknown") -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    text = _CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or default


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
    COMPONENT_VERSION_INFO = Gauge(
        "ibkr_component_version_info",
        "Installed/runtime IBKR component version marker.",
        ("service", "environment", "component", "version", "status"),
    )
    GATEWAY_MAJOR_VERSION = Gauge(
        "ibkr_gateway_major_version",
        "Configured IB Gateway major version.",
        ("service", "environment", "version"),
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
    GATEWAY_REACHABLE = Gauge(
        "ibkr_gateway_reachable",
        "Whether the IB Gateway is reachable by the runtime service.",
        ("service", "environment"),
    )
    GATEWAY_SESSION_AUTHENTICATED = Gauge(
        "ibkr_gateway_session_authenticated",
        "Whether the IB Gateway session is authenticated.",
        ("service", "environment"),
    )
    GATEWAY_WEBSOCKET_READY = Gauge(
        "ibkr_gateway_websocket_ready",
        "Whether the IBKR market data websocket is ready.",
        ("service", "environment"),
    )
    MARKET_UNIVERSE_SYMBOLS = Gauge(
        "ibkr_market_universe_symbols_count",
        "IBKR market universe symbol counts by low-cardinality universe kind.",
        ("service", "environment", "kind"),
    )
    MARKET_DATA_SUBSCRIPTION_ACTIVE = Gauge(
        "ibkr_market_data_subscription_active_count",
        "Active IBKR market data subscriptions by low-cardinality subscription kind.",
        ("service", "environment", "kind"),
    )
    MARKET_DATA_SUBSCRIPTION_LIMIT = Gauge(
        "ibkr_market_data_subscription_limit",
        "Configured IBKR market data subscription limit by low-cardinality subscription kind.",
        ("service", "environment", "kind"),
    )
    MARKET_DATA_SUBSCRIPTION_UTILIZATION = Gauge(
        "ibkr_market_data_subscription_utilization_pct",
        "IBKR market data subscription utilization percentage by low-cardinality subscription kind.",
        ("service", "environment", "kind"),
    )
    MARKET_DATA_SUBSCRIPTION_PENDING = Gauge(
        "ibkr_market_data_subscription_pending_count",
        "Pending IBKR market data subscription count by low-cardinality subscription kind.",
        ("service", "environment", "kind"),
    )
    MARKET_DATA_WS_LAST_MESSAGE_AGE = Gauge(
        "ibkr_market_data_websocket_last_message_age_seconds",
        "Age of the latest IBKR market data websocket message in seconds.",
        ("service", "environment"),
    )
    MARKET_DATA_WS_LAST_TIC_AGE = Gauge(
        "ibkr_market_data_websocket_last_tic_age_seconds",
        "Age of the latest IBKR market data websocket TIC message in seconds.",
        ("service", "environment"),
    )
    MARKET_DATA_BAR_LAG = Gauge(
        "ibkr_market_data_bar_lag_seconds",
        "Lag of the latest completed market data bar pipeline in seconds.",
        ("service", "environment", "interval"),
    )
    MARKET_DATA_BAR_PENDING_SYMBOLS = Gauge(
        "ibkr_market_data_bar_pending_symbols_count",
        "Number of symbols still pending in the market data bar pipeline.",
        ("service", "environment", "interval"),
    )
    ACCOUNT_DATA_CIRCUIT_ACTIVE = Gauge(
        "ibkr_account_data_circuit_active",
        "Whether the account data circuit breaker is active.",
        ("service", "environment"),
    )
    ACCOUNT_SNAPSHOT_AVAILABLE = Gauge(
        "ibkr_account_snapshot_available",
        "Whether an effective account or buying-power snapshot is currently available.",
        ("service", "environment", "source"),
    )
    ACCOUNT_SNAPSHOT_REFRESH_OK = Gauge(
        "ibkr_account_snapshot_refresh_ok",
        "Whether the latest account or buying-power snapshot observation was fresh and error-free.",
        ("service", "environment", "source"),
    )
    ACCOUNT_SNAPSHOT_STALE = Gauge(
        "ibkr_account_snapshot_stale",
        "Whether the current account or buying-power snapshot is served from stale cache.",
        ("service", "environment", "source"),
    )
    ACCOUNT_SNAPSHOT_AGE = Gauge(
        "ibkr_account_snapshot_age_seconds",
        "Age in seconds of the current account or buying-power snapshot.",
        ("service", "environment", "source"),
    )
    ACCOUNT_NET_LIQUIDATION = Gauge(
        "ibkr_account_net_liquidation_usd",
        "Account net liquidation in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_AVAILABLE_FUNDS = Gauge(
        "ibkr_account_available_funds_usd",
        "Account available funds in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_EXCESS_LIQUIDITY = Gauge(
        "ibkr_account_excess_liquidity_usd",
        "Account excess liquidity in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_TOTAL_CASH = Gauge(
        "ibkr_account_total_cash_usd",
        "Account total cash value in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_GROSS_POSITION_VALUE = Gauge(
        "ibkr_account_gross_position_value_usd",
        "Account gross position value in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_INITIAL_MARGIN = Gauge(
        "ibkr_account_initial_margin_usd",
        "Account initial margin requirement in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_MAINTENANCE_MARGIN = Gauge(
        "ibkr_account_maintenance_margin_usd",
        "Account maintenance margin requirement in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_CONFIGURED = Gauge(
        "ibkr_account_buying_power_configured_usd",
        "Deprecated configured buying-power model value in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_REMAINING = Gauge(
        "ibkr_account_buying_power_remaining_usd",
        "Remaining account buying power in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_USED_EXPOSURE = Gauge(
        "ibkr_account_buying_power_used_exposure_usd",
        "Account gross exposure used as buying-power pressure context.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_UTILIZATION = Gauge(
        "ibkr_account_buying_power_utilization_pct",
        "Approximate buying-power utilization percentage.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_REMAINING_SLOTS = Gauge(
        "ibkr_account_buying_power_remaining_slots",
        "Deprecated estimated remaining entry slots.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_WARN_FLOOR = Gauge(
        "ibkr_account_buying_power_warn_floor_usd",
        "Buying-power warning floor in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_BLOCK_FLOOR = Gauge(
        "ibkr_account_buying_power_block_floor_usd",
        "Buying-power block floor in USD.",
        ("service", "environment", "source"),
    )
    ACCOUNT_BUYING_POWER_GUARD_STATE = Gauge(
        "ibkr_account_buying_power_guard_state",
        "Buying-power guard state marker; the active state has value 1.",
        ("service", "environment", "source", "state"),
    )
    TRADE_REALIZED_PNL_TODAY = Gauge(
        "ibkr_trade_realized_pnl_today_usd",
        "Current trading-day realized PnL aggregated from execution fills.",
        ("service", "environment", "basis"),
    )
    TRADE_RESULT_COUNT_TODAY = Gauge(
        "ibkr_trade_result_count_today",
        "Current trading-day execution fill counts by realized PnL result bucket.",
        ("service", "environment", "basis", "grain", "result"),
    )
    POSITION_CURRENT_COUNT = Gauge(
        "ibkr_position_current_count",
        "Current Gateway position counts by low-cardinality direction bucket.",
        ("service", "environment", "direction"),
    )
    ORDER_CURRENT_COUNT = Gauge(
        "ibkr_order_current_count",
        "Current live open order counts by low-cardinality kind.",
        ("service", "environment", "kind"),
    )
    RUNTIME_CONFIG_SWITCH_ENABLED = Gauge(
        "ibkr_runtime_config_switch_enabled",
        "Effective runtime/trading config switch state; enabled switches have value 1.",
        ("service", "environment", "runtime_environment", "key", "mode_scope", "importance"),
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
        (
            "service",
            "environment",
            "client_role",
            "ib_error_code",
            "ib_error_cn",
            "ib_error_action_cn",
            "request_kind",
            "severity",
        ),
    )
    BROKER_REQUESTS = Counter(
        "ibkr_broker_request_total",
        "IB API synchronous request counts.",
        ("service", "environment", "client_role", "request_kind", "result", "error_class"),
    )
    BROKER_REQUEST_SUPPRESSED = Counter(
        "ibkr_broker_request_suppressed_total",
        "IB API requests suppressed by local pacing/cooldown guards.",
        ("service", "environment", "client_role", "request_kind", "reason_code"),
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
    ORDER_SERIAL_QUEUE_WAIT = Histogram(
        "ibkr_gateway_order_serial_queue_wait_seconds",
        "Queue wait before entering the shared IB Gateway order mutation gate.",
        ("service", "environment", "operation", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    ORDER_SERIAL_TIMEOUTS = Counter(
        "ibkr_gateway_order_serial_timeouts_total",
        "Timeouts while waiting for the shared IB Gateway order mutation gate.",
        ("service", "environment", "operation"),
    )
    ORDER_SYMBOL_QUEUE_WAIT = Histogram(
        "ibkr_order_symbol_queue_wait_seconds",
        "Wait time before a command starts in the per-symbol order queue.",
        ("service", "environment", "operation", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    ORDER_SYMBOL_QUEUE_ACTIVE = Histogram(
        "ibkr_order_symbol_queue_active_seconds",
        "Active execution time for commands run by the per-symbol order queue.",
        ("service", "environment", "operation", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
    ORDER_SYMBOL_QUEUE_ACTIVE_SYMBOLS = Histogram(
        "ibkr_order_symbol_queue_active_symbols",
        "Number of active symbols observed when a per-symbol order command starts.",
        ("service", "environment", "operation", "result"),
        buckets=(1, 2, 3, 4, 5, 8, 12, 16, float("inf")),
    )
    SIGNAL_EVENTS = Counter(
        "ibkr_signal_events_total",
        "Signal processing events.",
        ("service", "environment", "stage", "signal_source", "result", "reason_code"),
    )
    SIGNAL_RECORDS = Counter(
        "ibkr_signal_records_total",
        "Business signal records created in ibkr_signals.",
        ("service", "environment", "signal_source", "direction", "initial_status"),
    )
    SIGNAL_CURRENT_COUNT = Gauge(
        "ibkr_signal_current_count",
        "Current active business signal counts by direction and state.",
        ("service", "environment", "direction", "state"),
    )
    SIGNAL_DURATION = Histogram(
        "ibkr_signal_stage_duration_seconds",
        "Signal processing stage duration.",
        ("service", "environment", "stage", "result"),
        buckets=_DEFAULT_BUCKETS,
    )
else:  # pragma: no cover
    SERVICE_INFO = COMPONENT_VERSION_INFO = GATEWAY_MAJOR_VERSION = None
    HTTP_SERVER_REQUESTS = HTTP_SERVER_DURATION = HTTP_SERVER_IN_FLIGHT = HTTP_SERVER_EXCEPTIONS = None
    HTTP_CLIENT_REQUESTS = HTTP_CLIENT_DURATION = HTTP_CLIENT_IN_FLIGHT = HTTP_CLIENT_EXCEPTIONS = None
    GATEWAY_SOCKET_PROBES = GATEWAY_SOCKET_DURATION = GATEWAY_SOCKET_LISTENING = None
    GATEWAY_SERVICE_ACTIONS = GATEWAY_SERVICE_RUNNING = GATEWAY_SERVICE_UPTIME = GATEWAY_STATUS_CODE = None
    GATEWAY_REACHABLE = GATEWAY_SESSION_AUTHENTICATED = GATEWAY_WEBSOCKET_READY = None
    MARKET_UNIVERSE_SYMBOLS = MARKET_DATA_SUBSCRIPTION_ACTIVE = MARKET_DATA_SUBSCRIPTION_LIMIT = None
    MARKET_DATA_SUBSCRIPTION_UTILIZATION = MARKET_DATA_SUBSCRIPTION_PENDING = None
    MARKET_DATA_WS_LAST_MESSAGE_AGE = MARKET_DATA_WS_LAST_TIC_AGE = None
    MARKET_DATA_BAR_LAG = MARKET_DATA_BAR_PENDING_SYMBOLS = ACCOUNT_DATA_CIRCUIT_ACTIVE = None
    ACCOUNT_SNAPSHOT_AVAILABLE = ACCOUNT_SNAPSHOT_REFRESH_OK = ACCOUNT_SNAPSHOT_STALE = ACCOUNT_SNAPSHOT_AGE = None
    ACCOUNT_NET_LIQUIDATION = ACCOUNT_AVAILABLE_FUNDS = ACCOUNT_EXCESS_LIQUIDITY = None
    ACCOUNT_TOTAL_CASH = ACCOUNT_GROSS_POSITION_VALUE = ACCOUNT_INITIAL_MARGIN = ACCOUNT_MAINTENANCE_MARGIN = None
    ACCOUNT_BUYING_POWER_CONFIGURED = None
    ACCOUNT_BUYING_POWER_REMAINING = ACCOUNT_BUYING_POWER_USED_EXPOSURE = ACCOUNT_BUYING_POWER_UTILIZATION = None
    ACCOUNT_BUYING_POWER_REMAINING_SLOTS = ACCOUNT_BUYING_POWER_WARN_FLOOR = ACCOUNT_BUYING_POWER_BLOCK_FLOOR = None
    ACCOUNT_BUYING_POWER_GUARD_STATE = None
    TRADE_REALIZED_PNL_TODAY = TRADE_RESULT_COUNT_TODAY = None
    POSITION_CURRENT_COUNT = ORDER_CURRENT_COUNT = SIGNAL_CURRENT_COUNT = None
    RUNTIME_CONFIG_SWITCH_ENABLED = None
    BROKER_CONNECTS = BROKER_CONNECT_DURATION = BROKER_READY = BROKER_CONNECTED = BROKER_DISCONNECTS = BROKER_ERRORS = None
    BROKER_REQUESTS = BROKER_REQUEST_SUPPRESSED = BROKER_REQUEST_DURATION = BROKER_PENDING_REQUESTS = None
    HISTORY_EVENTS = HISTORY_DURATION = HISTORY_ACTIVE = HISTORY_ROWS = None
    ORDER_EVENTS = ORDER_DURATION = ORDER_SERIAL_QUEUE_WAIT = ORDER_SERIAL_TIMEOUTS = None
    ORDER_SYMBOL_QUEUE_WAIT = ORDER_SYMBOL_QUEUE_ACTIVE = ORDER_SYMBOL_QUEUE_ACTIVE_SYMBOLS = None
    SIGNAL_EVENTS = SIGNAL_RECORDS = SIGNAL_DURATION = None


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
    refresh_component_version_metrics(service_name=service)
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
        refresh_component_version_metrics(service_name=service)
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


def _ibapi_version() -> str:
    try:
        import ibapi

        return str(getattr(ibapi, "__version__", "") or "unknown").strip() or "unknown"
    except Exception:
        return "unknown"


def _gateway_major_version() -> str:
    for key in ("IBKR_TWS_MAJOR_VERSION", "TWS_MAJOR_VERSION", "IBGW_TWS_MAJOR_VERSION"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def refresh_component_version_metrics(*, service_name: str | None = None, environment: str = "") -> None:
    if not _client_available():
        return
    service = resolve_source_service(service_name)
    env = _metric_environment(environment)
    components = {"ibapi": _ibapi_version()}
    if service == "ibkr-runtime":
        components["gateway"] = _gateway_major_version()
    if COMPONENT_VERSION_INFO is not None:
        for component, version in components.items():
            status = "present" if version != "unknown" else "unknown"
            COMPONENT_VERSION_INFO.labels(
                service,
                env,
                _sanitize_label(component),
                _sanitize_label(version),
                status,
            ).set(1.0)
    gateway_version = components.get("gateway", "")
    version_number = _optional_float(gateway_version)
    if GATEWAY_MAJOR_VERSION is not None and version_number is not None:
        GATEWAY_MAJOR_VERSION.labels(service, env, _sanitize_label(gateway_version)).set(version_number)


def _metric_environment(environment: str = "") -> str:
    return _sanitize_label(
        environment
        or os.environ.get("IBKR_BROKER_MODE")
        or os.environ.get("BROKER_MODE")
        or "unknown"
    )


def _gauge_set(metric: Any, labels: tuple[Any, ...], value: Any) -> None:
    if metric is None:
        return
    number = _optional_float(value)
    if number is None:
        return
    try:
        metric.labels(*labels).set(number)
    except Exception:
        pass


def _gauge_clear(metric: Any, labels: tuple[Any, ...]) -> None:
    if metric is None:
        return
    try:
        metric.labels(*labels).set(float("nan"))
    except Exception:
        pass


def _metric_bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "ok", "available"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "unavailable"}:
        return False
    return None


def _snapshot_summary_available(payload: dict[str, Any], summary: dict[str, Any], health: dict[str, Any]) -> bool:
    explicit = _metric_bool_or_none(payload.get("summary_available"))
    if explicit is None:
        explicit = _metric_bool_or_none(health.get("summary_available"))
    if explicit is not None:
        return explicit
    for key in (
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "total_cash_value",
        "gross_position_value",
        "initial_margin",
        "maintenance_margin",
        "remaining_buying_power",
        "buying_power",
    ):
        if _optional_float(summary.get(key)) is not None:
            return True
    return False


def _snapshot_age_seconds(payload: dict[str, Any], health: dict[str, Any]) -> float | None:
    for value in (payload.get("cache_age_s"), health.get("cache_age_s")):
        number = _optional_float(value)
        if number is not None:
            return max(0.0, number)
    raw = payload.get("fetched_at")
    if raw in (None, ""):
        return None
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        fetched_at = datetime.fromisoformat(text)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - fetched_at.timestamp())
    except Exception:
        return None


def _safe_len(value: Any) -> int:
    try:
        return len(value or [])
    except Exception:
        return 0


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _optional_float(value)
        if number is not None:
            return number
    return None


def _set_market_data_subscription_metrics(
    service: str,
    env: str,
    *,
    kind: str,
    active: Any = None,
    limit: Any = None,
    pending: Any = None,
) -> None:
    kind_label = _sanitize_label(kind or "unknown")
    active_number = _optional_float(active)
    limit_number = _optional_float(limit)
    pending_number = _optional_float(pending)
    if active_number is not None:
        _gauge_set(MARKET_DATA_SUBSCRIPTION_ACTIVE, (service, env, kind_label), max(0.0, active_number))
    if limit_number is not None:
        _gauge_set(MARKET_DATA_SUBSCRIPTION_LIMIT, (service, env, kind_label), max(0.0, limit_number))
    if pending_number is not None:
        _gauge_set(MARKET_DATA_SUBSCRIPTION_PENDING, (service, env, kind_label), max(0.0, pending_number))
    if active_number is not None and limit_number is not None and limit_number > 0:
        utilization = max(0.0, min(100.0, active_number / limit_number * 100.0))
        _gauge_set(MARKET_DATA_SUBSCRIPTION_UTILIZATION, (service, env, kind_label), utilization)


def record_gateway_socket_probe(*, environment: str = "", host: Any = "", port: Any = "", source: str = "status", result: str = "unknown", reason_code: str = "", duration_s: float | None = None, listening: bool | None = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _metric_environment(environment)
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
        GATEWAY_SERVICE_ACTIONS.labels(resolve_source_service(), _metric_environment(environment), _sanitize_label(action), _sanitize_label(result)).inc()


def set_gateway_status(
    *,
    environment: str = "",
    running: Any = None,
    uptime_s: Any = None,
    status_code: Any = None,
    reachable: Any = None,
) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _metric_environment(environment)
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
    if GATEWAY_REACHABLE is not None and reachable is not None:
        GATEWAY_REACHABLE.labels(service, env).set(1.0 if bool(reachable) else 0.0)


def set_runtime_status_metrics(status: dict[str, Any] | None = None, *, environment: str = "") -> None:
    if not _client_available() or not isinstance(status, dict):
        return
    service = resolve_source_service()
    env = _metric_environment(environment or status.get("environment") or status.get("broker_mode"))
    gateway = status.get("gateway") if isinstance(status.get("gateway"), dict) else {}
    session = status.get("session") if isinstance(status.get("session"), dict) else {}
    websocket = status.get("websocket") if isinstance(status.get("websocket"), dict) else {}
    market_universe = status.get("market_universe") if isinstance(status.get("market_universe"), dict) else {}
    account_data_circuit = status.get("account_data_circuit") if isinstance(status.get("account_data_circuit"), dict) else {}
    if not account_data_circuit and isinstance(gateway.get("broker"), dict):
        broker_circuit = gateway["broker"].get("account_data_circuit")
        account_data_circuit = broker_circuit if isinstance(broker_circuit, dict) else {}
    _gauge_set(GATEWAY_REACHABLE, (service, env), 1.0 if bool(gateway.get("reachable")) else 0.0)
    _gauge_set(GATEWAY_SESSION_AUTHENTICATED, (service, env), 1.0 if bool(session.get("authenticated")) else 0.0)
    _gauge_set(
        GATEWAY_WEBSOCKET_READY,
        (service, env),
        1.0 if bool(websocket.get("ready") or websocket.get("connected")) else 0.0,
    )
    _gauge_set(ACCOUNT_DATA_CIRCUIT_ACTIVE, (service, env), 1.0 if bool(account_data_circuit.get("active")) else 0.0)
    set_runtime_config_switch_metrics(status, environment=env)

    active_subscription_count = _first_number(
        market_universe.get("active_subscription_count"),
        _safe_len(market_universe.get("active_subscription_symbols")),
    )
    watchlist_pool_count = _first_number(
        market_universe.get("watchlist_pool_count"),
        market_universe.get("data_symbols_total"),
        _safe_len(market_universe.get("data_symbols")),
    )
    watchlist_trade_count = _first_number(
        market_universe.get("watchlist_trade_count"),
        market_universe.get("scan_symbols_total"),
        _safe_len(market_universe.get("scan_symbols")),
    )
    active_trade_count = _first_number(
        market_universe.get("active_trade_symbol_count"),
        market_universe.get("active_target_count"),
        _safe_len(market_universe.get("active_trade_symbols")),
    )
    active_monitor_count = _first_number(
        market_universe.get("active_monitor_symbol_count"),
        market_universe.get("market_ws_symbols_ready"),
    )
    if active_monitor_count is None and active_subscription_count is not None and active_trade_count is not None:
        active_monitor_count = max(0.0, active_subscription_count - active_trade_count)
    ws_subscribed_count = _first_number(
        websocket.get("subscribed_count"),
        _safe_len(websocket.get("subscribed_conids")),
        market_universe.get("market_ws_symbols_ready"),
    )
    pending_count = _first_number(
        websocket.get("pending_count"),
        _safe_len(websocket.get("pending_conids")),
        market_universe.get("pending_subscription_count"),
    )
    target_limit = _first_number(market_universe.get("target_subscription_limit"))
    total_limit = _first_number(
        market_universe.get("total_subscription_limit"),
        market_universe.get("subscription_limit"),
        target_limit,
    )
    if total_limit is not None and total_limit <= 0 and target_limit is not None:
        total_limit = target_limit
    trade_limit = _first_number(
        market_universe.get("trade_subscription_limit"),
        market_universe.get("trade_subscription_budget"),
        target_limit,
    )
    monitor_limit = _first_number(
        market_universe.get("market_monitor_subscription_limit"),
        market_universe.get("market_ws_symbols_total"),
        _safe_len(market_universe.get("market_ws_symbols")),
    )
    context_ws_count = _first_number(
        market_universe.get("market_ws_symbols_total"),
        _safe_len(market_universe.get("market_ws_symbols")),
        monitor_limit,
    )
    for universe_kind, universe_value in (
        ("pool", watchlist_pool_count),
        ("trade_watchlist", watchlist_trade_count),
        ("context_ws", context_ws_count),
        ("active_trade", active_trade_count),
        ("active_subscription", active_subscription_count),
    ):
        _gauge_set(
            MARKET_UNIVERSE_SYMBOLS,
            (service, env, _sanitize_label(universe_kind)),
            universe_value,
        )
    websocket_limit = _first_number(market_universe.get("websocket_subscription_limit"), total_limit)
    _set_market_data_subscription_metrics(
        service,
        env,
        kind="total",
        active=active_subscription_count,
        limit=total_limit,
        pending=pending_count,
    )
    _set_market_data_subscription_metrics(
        service,
        env,
        kind="trade",
        active=active_trade_count,
        limit=trade_limit,
    )
    _set_market_data_subscription_metrics(
        service,
        env,
        kind="monitor",
        active=active_monitor_count,
        limit=monitor_limit,
    )
    _set_market_data_subscription_metrics(
        service,
        env,
        kind="websocket",
        active=ws_subscribed_count,
        limit=websocket_limit,
        pending=pending_count,
    )
    _gauge_set(MARKET_DATA_WS_LAST_MESSAGE_AGE, (service, env), websocket.get("last_message_age_s"))
    _gauge_set(MARKET_DATA_WS_LAST_TIC_AGE, (service, env), websocket.get("last_tic_age_s"))

    bar_freshness = market_universe.get("bar_freshness") if isinstance(market_universe.get("bar_freshness"), dict) else {}
    canonical_5m = status.get("canonical_5m") if isinstance(status.get("canonical_5m"), dict) else {}
    interval = _sanitize_label(bar_freshness.get("interval") or canonical_5m.get("interval") or "5m")
    _gauge_set(MARKET_DATA_BAR_LAG, (service, env, interval), _first_number(bar_freshness.get("lag_s"), canonical_5m.get("lag_s")))
    _gauge_set(
        MARKET_DATA_BAR_PENDING_SYMBOLS,
        (service, env, interval),
        _first_number(bar_freshness.get("pending_symbols_total"), canonical_5m.get("pending_symbols_total")),
    )


def _switch_enabled_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return 1.0
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return 0.0
    return _optional_float(value)


def set_runtime_config_switch_metrics(status: dict[str, Any] | None = None, *, environment: str = "") -> None:
    if not _client_available() or RUNTIME_CONFIG_SWITCH_ENABLED is None or not isinstance(status, dict):
        return
    payload = status.get("runtime_config_switches")
    if isinstance(payload, dict):
        switches = payload.get("items") or payload.get("switches") or []
    elif isinstance(payload, list):
        switches = payload
    else:
        switches = []
    if not isinstance(switches, list):
        return
    service = resolve_source_service()
    runtime_env = _metric_environment(environment or status.get("environment") or status.get("broker_mode"))
    for item in switches:
        if not isinstance(item, dict):
            continue
        enabled_value = _switch_enabled_value(item.get("enabled"))
        if enabled_value is None:
            continue
        key = _sanitize_label(item.get("key"), "")
        if not key:
            continue
        config_env = _metric_environment(item.get("config_environment") or item.get("environment") or runtime_env)
        mode_scope = _sanitize_label(item.get("mode_scope") or item.get("scope") or "runtime")
        importance = _sanitize_label(item.get("importance") or "important")
        _gauge_set(
            RUNTIME_CONFIG_SWITCH_ENABLED,
            (service, config_env, runtime_env, key, mode_scope, importance),
            1.0 if enabled_value > 0 else 0.0,
        )


def _metric_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _current_position_counts(payload: dict[str, Any]) -> dict[str, int]:
    positions_value = payload.get("positions")
    if not isinstance(positions_value, list):
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        long_count = max(0, int(_first_number(counts.get("long_positions"), counts.get("positions_long")) or 0))
        short_count = max(0, int(_first_number(counts.get("short_positions"), counts.get("positions_short")) or 0))
        total_count = max(
            long_count + short_count,
            int(_first_number(counts.get("open_positions"), counts.get("positions"), long_count + short_count) or 0),
        )
        return {"total": total_count, "long": long_count, "short": short_count}
    positions = positions_value
    long_count = 0
    short_count = 0
    for item in positions:
        if not isinstance(item, dict):
            continue
        quantity = _first_number(item.get("quantity"), item.get("position"), item.get("position_qty"))
        if quantity is None:
            direction = str(item.get("direction") or "").strip().lower()
            if direction == "long":
                long_count += 1
            elif direction == "short":
                short_count += 1
            continue
        if quantity > 0:
            long_count += 1
        elif quantity < 0:
            short_count += 1
    return {"total": long_count + short_count, "long": long_count, "short": short_count}


def _current_order_group_key(order: dict[str, Any], index: int) -> str:
    for field_name in (
        "trade_group_id",
        "entry_order_unique_id",
        "linked_trade_group_id",
        "linked_entry_order_unique_id",
        "parent_order_unique_id",
        "parent_id",
        "parent_order_id",
        "parentId",
    ):
        value = str(order.get(field_name) or "").strip()
        if value:
            return value
    for field_name in ("order_id", "orderId", "id", "client_order_id", "unique_id", "coid", "order_ref", "orderRef"):
        value = str(order.get(field_name) or "").strip()
        if value:
            return value
    return f"row-{index}"


def _current_order_group_counts(groups: Any) -> dict[str, int] | None:
    if not isinstance(groups, list):
        return None
    open_groups = 0
    open_legs = 0
    cancelable = 0
    editable = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        open_groups += 1
        orders = group.get("orders") if isinstance(group.get("orders"), list) else []
        leg_count = int(_first_number(group.get("live_order_count"), group.get("order_count")) or 0)
        if leg_count <= 0 and orders:
            leg_count = len([order for order in orders if isinstance(order, dict)])
        if leg_count <= 0:
            leg_count = 1
        open_legs += leg_count

        cancelable_count = _first_number(group.get("cancelable_orders"))
        if cancelable_count is not None:
            cancelable += max(0, int(cancelable_count))
        elif orders:
            cancelable += len([order for order in orders if isinstance(order, dict) and _metric_truthy(order.get("can_cancel"))])

        editable_count = _first_number(group.get("editable_orders"))
        if editable_count is not None:
            editable += max(0, int(editable_count))
        elif orders:
            editable += len([order for order in orders if isinstance(order, dict) and _metric_truthy(order.get("can_modify"))])
    return {"open_legs": open_legs, "open_groups": open_groups, "cancelable": cancelable, "editable": editable}


def _current_order_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    group_counts = _current_order_group_counts(payload.get("live_order_groups"))
    live_orders = payload.get("live_open_orders") if isinstance(payload.get("live_open_orders"), list) else None
    if live_orders is None:
        if group_counts is not None:
            return group_counts
        open_legs = int(_first_number(counts.get("open_orders"), counts.get("orders"), (group_counts or {}).get("open_legs")) or 0)
        return {
            "open_legs": max(0, open_legs),
            "open_groups": max(0, int(_first_number(counts.get("open_order_groups"), counts.get("order_groups"), (group_counts or {}).get("open_groups"), open_legs) or 0)),
            "cancelable": max(0, int(_first_number(counts.get("cancelable_orders"), (group_counts or {}).get("cancelable")) or 0)),
            "editable": max(0, int(_first_number(counts.get("editable_orders"), (group_counts or {}).get("editable")) or 0)),
        }

    groups: dict[str, dict[str, bool]] = {}
    cancelable = 0
    editable = 0
    for index, order in enumerate(live_orders):
        if not isinstance(order, dict):
            continue
        if _metric_truthy(order.get("can_cancel")):
            cancelable += 1
        if _metric_truthy(order.get("can_modify")):
            editable += 1
        group = groups.setdefault(_current_order_group_key(order, index), {"cancelable": False, "editable": False})
        group["cancelable"] = bool(group["cancelable"] or _metric_truthy(order.get("can_cancel")))
        group["editable"] = bool(group["editable"] or _metric_truthy(order.get("can_modify")))
    return {
        "open_legs": len([item for item in live_orders if isinstance(item, dict)]),
        "open_groups": (group_counts or {}).get("open_groups") or len(groups),
        "cancelable": cancelable,
        "editable": editable,
    }


def _has_current_account_snapshot(payload: dict[str, Any]) -> bool:
    if any(isinstance(payload.get(field_name), list) for field_name in ("positions", "live_open_orders", "live_order_groups")):
        return True
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    return any(
        key in counts
        for key in (
            "open_positions",
            "positions",
            "open_orders",
            "orders",
            "open_order_groups",
            "order_groups",
            "cancelable_orders",
            "editable_orders",
        )
    )


def _set_account_current_count_metrics(service: str, env: str, payload: dict[str, Any]) -> None:
    position_counts = _current_position_counts(payload)
    for direction in ("total", "long", "short"):
        _gauge_set(POSITION_CURRENT_COUNT, (service, env, direction), position_counts.get(direction, 0))

    order_counts = _current_order_counts(payload)
    for kind in ("open_legs", "open_groups", "cancelable", "editable"):
        _gauge_set(ORDER_CURRENT_COUNT, (service, env, kind), order_counts.get(kind, 0))


def set_buying_power_guard_effective_metrics(
    guard: dict[str, Any] | None = None,
    *,
    environment: str = "",
    service_name: str | None = None,
) -> None:
    if not _client_available() or not isinstance(guard, dict):
        return
    service = resolve_source_service(service_name)
    env = _metric_environment(environment or guard.get("environment"))
    src = "effective_guard"

    configured = _optional_float(guard.get("configured_buying_power"))
    remaining = _first_number(
        guard.get("remaining"),
        guard.get("remaining_after"),
        guard.get("account_remaining_buying_power"),
    )
    used = _first_number(
        guard.get("risk_model_used_exposure"),
        guard.get("used_exposure"),
        guard.get("gross_position_value"),
    )
    if used is None and configured is not None and remaining is not None:
        used = max(0.0, configured - remaining)

    _gauge_set(ACCOUNT_BUYING_POWER_CONFIGURED, (service, env, src), configured)
    _gauge_set(ACCOUNT_BUYING_POWER_REMAINING, (service, env, src), remaining)
    _gauge_set(ACCOUNT_BUYING_POWER_USED_EXPOSURE, (service, env, src), used)
    _gauge_set(ACCOUNT_BUYING_POWER_REMAINING_SLOTS, (service, env, src), guard.get("risk_model_remaining_slots"))
    _gauge_set(ACCOUNT_BUYING_POWER_WARN_FLOOR, (service, env, src), guard.get("warn_floor"))
    _gauge_set(ACCOUNT_BUYING_POWER_BLOCK_FLOOR, (service, env, src), guard.get("block_floor"))

    utilization = _optional_float(guard.get("utilization_pct"))
    if utilization is None and configured is not None and configured > 0 and used is not None:
        utilization = max(0.0, min(100.0, used / configured * 100.0))
    _gauge_set(ACCOUNT_BUYING_POWER_UTILIZATION, (service, env, src), utilization)

    state = _sanitize_label(str(guard.get("state") or "unknown").strip().lower() or "unknown")
    if ACCOUNT_BUYING_POWER_GUARD_STATE is not None:
        for candidate in ("ok", "warning", "ledger_safe", "blocked", "unavailable", "disabled", "unknown"):
            ACCOUNT_BUYING_POWER_GUARD_STATE.labels(service, env, src, candidate).set(1.0 if state == candidate else 0.0)


def set_trade_result_today_metrics(
    summary: dict[str, Any] | None = None,
    *,
    environment: str = "",
    service_name: str | None = None,
    basis: str = "realized",
    grain: str = "trade_group",
) -> None:
    if not _client_available() or not isinstance(summary, dict):
        return
    service = resolve_source_service(service_name)
    env = _metric_environment(environment or summary.get("environment"))
    basis_label = _sanitize_label(str(basis or summary.get("basis") or "realized").strip().lower() or "realized")
    grain_label = _sanitize_label(str(grain or summary.get("grain") or "trade_group").strip().lower() or "trade_group")
    pnl = _first_number(summary.get("realized_pnl"), summary.get("realized_pnl_sum"), summary.get("net_pnl"), 0.0)
    _gauge_set(TRADE_REALIZED_PNL_TODAY, (service, env, basis_label), pnl)

    counts = summary.get("result_counts") if isinstance(summary.get("result_counts"), dict) else {}
    for result in ("win", "loss", "flat", "unknown"):
        value = _first_number(counts.get(result), summary.get(f"{result}_count"), 0.0)
        _gauge_set(TRADE_RESULT_COUNT_TODAY, (service, env, basis_label, grain_label, result), value)


def set_account_snapshot_metrics(payload: dict[str, Any] | None = None, *, source: str = "", environment: str = "") -> None:
    if not _client_available() or not isinstance(payload, dict):
        return
    service = resolve_source_service()
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    env = _metric_environment(environment or payload.get("environment") or guard.get("environment"))
    src = _sanitize_label(source or guard.get("source") or payload.get("source") or "account_snapshot")

    health = payload.get("account_snapshot_health") if isinstance(payload.get("account_snapshot_health"), dict) else {}
    guard_available = _metric_bool_or_none(guard.get("available"))
    guard_state = str(guard.get("state") or "").strip().lower()
    health_state = str(health.get("state") or health.get("health") or "ok").strip().lower()
    cache_state = str(payload.get("cache_state") or health.get("cache_state") or "").strip().lower()
    positions_stale = bool(_metric_bool_or_none(payload.get("positions_stale"))) or bool(
        _metric_bool_or_none(payload.get("positions_refresh_required"))
    )
    stale = (
        bool(_metric_bool_or_none(payload.get("stale")))
        or positions_stale
        or cache_state in {"stale", "stale_after_error", "stale_after_account_data_circuit"}
    )
    errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    refresh_error = str(payload.get("refresh_error") or errors.get("refresh") or "").strip()
    payload_ok = _metric_bool_or_none(payload.get("ok"))
    if payload_ok is None:
        payload_ok = True
    summary_available = _snapshot_summary_available(payload, summary, health)
    guard_unavailable = guard_available is False or guard_state == "unavailable"
    available = (
        summary_available
        and not guard_unavailable
        and health_state != "unavailable"
        and (payload_ok or stale)
    )
    refresh_ok = (
        bool(payload_ok)
        and summary_available
        and not guard_unavailable
        and health_state == "ok"
        and not stale
        and not refresh_error
    )
    age_seconds = _snapshot_age_seconds(payload, health)
    positions_age = _optional_float(payload.get("positions_age_s"))
    if positions_age is not None:
        age_seconds = max(age_seconds or 0.0, positions_age)
    if ACCOUNT_SNAPSHOT_AVAILABLE is not None:
        ACCOUNT_SNAPSHOT_AVAILABLE.labels(service, env, src).set(1.0 if available else 0.0)
    if ACCOUNT_SNAPSHOT_REFRESH_OK is not None:
        ACCOUNT_SNAPSHOT_REFRESH_OK.labels(service, env, src).set(1.0 if refresh_ok else 0.0)
    if ACCOUNT_SNAPSHOT_STALE is not None:
        ACCOUNT_SNAPSHOT_STALE.labels(service, env, src).set(1.0 if stale else 0.0)
    _gauge_set(ACCOUNT_SNAPSHOT_AGE, (service, env, src), age_seconds)

    net_liq = _optional_float(guard.get("net_liquidation"))
    if net_liq is None:
        net_liq = _optional_float(summary.get("net_liquidation"))
    available_funds = _optional_float(summary.get("available_funds"))
    excess_liquidity = _optional_float(summary.get("excess_liquidity"))
    total_cash = _optional_float(summary.get("total_cash_value"))
    gross_position_value = _optional_float(summary.get("gross_position_value"))
    initial_margin = _optional_float(summary.get("initial_margin"))
    maintenance_margin = _optional_float(summary.get("maintenance_margin"))
    remaining = _optional_float(guard.get("remaining"))
    if remaining is None:
        remaining = _optional_float(summary.get("remaining_buying_power"))
    if remaining is None:
        remaining = _optional_float(summary.get("buying_power"))
    configured = _optional_float(guard.get("configured_buying_power"))
    used = _optional_float(guard.get("risk_model_used_exposure"))
    if used is None and configured is not None and remaining is not None:
        used = max(0.0, configured - remaining)
    elif used is None:
        used = gross_position_value

    account_value_metrics = (
        (ACCOUNT_NET_LIQUIDATION, net_liq),
        (ACCOUNT_AVAILABLE_FUNDS, available_funds),
        (ACCOUNT_EXCESS_LIQUIDITY, excess_liquidity),
        (ACCOUNT_TOTAL_CASH, total_cash),
        (ACCOUNT_GROSS_POSITION_VALUE, gross_position_value),
        (ACCOUNT_INITIAL_MARGIN, initial_margin),
        (ACCOUNT_MAINTENANCE_MARGIN, maintenance_margin),
        (ACCOUNT_BUYING_POWER_CONFIGURED, configured),
        (ACCOUNT_BUYING_POWER_REMAINING, remaining),
        (ACCOUNT_BUYING_POWER_USED_EXPOSURE, used),
        (ACCOUNT_BUYING_POWER_REMAINING_SLOTS, guard.get("risk_model_remaining_slots")),
        (ACCOUNT_BUYING_POWER_WARN_FLOOR, guard.get("warn_floor")),
        (ACCOUNT_BUYING_POWER_BLOCK_FLOOR, guard.get("block_floor")),
    )
    for metric, value in account_value_metrics:
        if available:
            _gauge_set(metric, (service, env, src), value)
        else:
            _gauge_clear(metric, (service, env, src))

    utilization = _optional_float(guard.get("utilization_pct"))
    if utilization is None and configured is not None and configured > 0 and used is not None:
        utilization = max(0.0, min(100.0, used / configured * 100.0))
    elif utilization is None and remaining is not None:
        buying_power = _optional_float(summary.get("buying_power"))
        if buying_power is not None and buying_power > 0 and abs(buying_power - remaining) > 1e-9:
            utilization = max(0.0, min(100.0, (buying_power - remaining) / buying_power * 100.0))
        elif used is not None and remaining + used > 0:
            utilization = max(0.0, min(100.0, used / (remaining + used) * 100.0))
    if available:
        _gauge_set(ACCOUNT_BUYING_POWER_UTILIZATION, (service, env, src), utilization)
    else:
        _gauge_clear(ACCOUNT_BUYING_POWER_UTILIZATION, (service, env, src))

    state = _sanitize_label(str(guard.get("state") or "unknown").strip().lower() or "unknown")
    if ACCOUNT_BUYING_POWER_GUARD_STATE is not None:
        for candidate in ("ok", "warning", "blocked", "unavailable", "disabled", "unknown"):
            ACCOUNT_BUYING_POWER_GUARD_STATE.labels(service, env, src, candidate).set(1.0 if state == candidate else 0.0)
    if _has_current_account_snapshot(payload):
        _set_account_current_count_metrics(service, env, payload)


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
        info = describe_ib_error(ib_error_code)
        BROKER_ERRORS.labels(
            resolve_source_service(),
            _env_from_obj(obj),
            _client_role(obj),
            _sanitize_label(ib_error_code or 0),
            _sanitize_display_label(info.summary_cn),
            _sanitize_display_label(info.action_cn),
            _sanitize_label(request_kind),
            _sanitize_label(severity),
        ).inc()


def record_broker_request(obj: Any = None, *, request_kind: str, result: str, duration_s: float | None = None, error_class: str = "") -> None:
    if not _client_available():
        return
    labels = (resolve_source_service(), _env_from_obj(obj), _client_role(obj), _sanitize_label(request_kind), _sanitize_label(result), _sanitize_reason_code(error_class or result, _sanitize_label(result)))
    if BROKER_REQUESTS is not None:
        BROKER_REQUESTS.labels(*labels).inc()
    if BROKER_REQUEST_DURATION is not None and duration_s is not None:
        BROKER_REQUEST_DURATION.labels(labels[0], labels[1], labels[2], labels[3], labels[4]).observe(max(0.0, float(duration_s or 0.0)))


def record_broker_request_suppressed(obj: Any = None, *, request_kind: str, reason_code: str = "pacing") -> None:
    if not _client_available() or BROKER_REQUEST_SUPPRESSED is None:
        return
    BROKER_REQUEST_SUPPRESSED.labels(
        resolve_source_service(),
        _env_from_obj(obj),
        _client_role(obj),
        _sanitize_label(request_kind),
        _sanitize_reason_code(reason_code or "pacing", "pacing"),
    ).inc()


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



def record_gateway_order_serial_event(*, environment: str = "", operation: str, result: str = "ok", queue_wait_s: float | None = None) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    op = _sanitize_label(operation or "gateway_order_mutation")
    result_label = _sanitize_label(result or "ok")
    if ORDER_SERIAL_QUEUE_WAIT is not None and queue_wait_s is not None:
        ORDER_SERIAL_QUEUE_WAIT.labels(service, env, op, result_label).observe(max(0.0, float(queue_wait_s or 0.0)))
    if ORDER_SERIAL_TIMEOUTS is not None and result_label == "timeout":
        ORDER_SERIAL_TIMEOUTS.labels(service, env, op).inc()


def record_symbol_order_queue_event(
    *,
    environment: str = "",
    operation: str,
    result: str = "ok",
    queue_wait_s: float | None = None,
    active_elapsed_s: float | None = None,
    active_symbols: int | None = None,
) -> None:
    if not _client_available():
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    op = _sanitize_label(operation or "order_command")
    result_label = _sanitize_label(result or "ok")
    if ORDER_SYMBOL_QUEUE_WAIT is not None and queue_wait_s is not None:
        ORDER_SYMBOL_QUEUE_WAIT.labels(service, env, op, result_label).observe(max(0.0, float(queue_wait_s or 0.0)))
    if ORDER_SYMBOL_QUEUE_ACTIVE is not None and active_elapsed_s is not None:
        ORDER_SYMBOL_QUEUE_ACTIVE.labels(service, env, op, result_label).observe(max(0.0, float(active_elapsed_s or 0.0)))
    if ORDER_SYMBOL_QUEUE_ACTIVE_SYMBOLS is not None and active_symbols is not None:
        ORDER_SYMBOL_QUEUE_ACTIVE_SYMBOLS.labels(service, env, op, result_label).observe(max(0.0, float(active_symbols or 0)))


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


def _signal_record_source_label(value: Any) -> str:
    text = _sanitize_label(value, "unknown").lower()
    if text in {"tv", "tradingview", "webhook_tv", "tv_webhook", "tradingview_webhook", "signal"}:
        return "tradingview"
    if text in {
        "ibkr",
        "ibkr_compute",
        "ibkr_runtime",
        "ibkr_compute_realtime",
        "ibkr_compute_timeline",
        "ibkr_history_recompute",
        "timeline",
        "chart_timeline",
        "history_repair",
        "recompute",
        "backfill_recompute",
    }:
        return "ibkr_compute"
    if text in {"manual", "manual_order", "runtime_page", "account_page"}:
        return "manual"
    return "unknown"


def _signal_record_direction_label(value: Any) -> str:
    text = _sanitize_label(value, "unknown").lower()
    return text if text in {"long", "short"} else "unknown"


def set_current_signal_count_metrics(
    counts: dict[str, Any] | None = None,
    *,
    environment: str = "",
    state: str = "active",
    service_name: str | None = None,
) -> None:
    if not _client_available() or SIGNAL_CURRENT_COUNT is None:
        return
    service = resolve_source_service(service_name)
    env = _sanitize_label(environment or os.environ.get("IBKR_MARKET_DATA_MODE") or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    state_label = _sanitize_label(str(state or "active").strip().lower() or "active")
    source = counts if isinstance(counts, dict) else {}
    for direction in ("long", "short"):
        value = _first_number(source.get(direction), source.get(direction.upper()), 0)
        SIGNAL_CURRENT_COUNT.labels(service, env, direction, state_label).set(max(0.0, float(value or 0.0)))


def record_signal_record_created(
    *,
    environment: str = "",
    signal_source: str = "unknown",
    direction: str = "unknown",
    initial_status: str = "unknown",
) -> None:
    if not _client_available() or SIGNAL_RECORDS is None:
        return
    service = resolve_source_service()
    env = _sanitize_label(environment or os.environ.get("IBKR_BROKER_MODE") or "unknown")
    SIGNAL_RECORDS.labels(
        service,
        env,
        _signal_record_source_label(signal_source),
        _signal_record_direction_label(direction),
        _sanitize_reason_code(initial_status or "unknown", "unknown"),
    ).inc()


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
    "record_broker_request_suppressed",
    "record_gateway_service_action",
    "record_gateway_socket_probe",
    "record_history_event",
    "record_order_event",
    "record_signal_event",
    "record_signal_record_created",
    "record_symbol_order_queue_event",
    "refresh_component_version_metrics",
    "register_flask_metrics",
    "register_prometheus_metrics",
    "resolve_source_service",
    "resolve_target_service",
    "safe_metric_labels",
    "sanitize_labels",
    "sanitize_metric_labels",
    "set_account_snapshot_metrics",
    "set_broker_pending",
    "set_buying_power_guard_effective_metrics",
    "set_current_signal_count_metrics",
    "set_gateway_status",
    "set_history_active",
    "set_runtime_config_switch_metrics",
    "set_runtime_status_metrics",
    "set_trade_result_today_metrics",
    "setup_prometheus_metrics",
]
