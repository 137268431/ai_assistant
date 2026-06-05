#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STARTED_AT = time.time()
COUNTERS = {
    "requests_total": 0,
    "grafana_notifications_total": 0,
    "feishu_send_success_total": 0,
    "feishu_send_error_total": 0,
    "bad_requests_total": 0,
}

DEFAULT_ALERT_CHAT_ID = ""
try:
    from ibkr_api.integrations.feishu import feishu_send_interactive  # type: ignore
    from ibkr_api.system.events import DEFAULT_FEISHU_ALERT_CHAT_ID  # type: ignore
except Exception:  # pragma: no cover - import depends on remote PYTHONPATH.
    feishu_send_interactive = None  # type: ignore[assignment]
    DEFAULT_FEISHU_ALERT_CHAT_ID = ""


def normalize_environment(value: Any, default: str = "live") -> str:
    text = str(value or default or "live").strip().lower()
    if text in {"paper", "demo", "sim"}:
        return "paper"
    if text in {"live", "prod", "production"}:
        return "live"
    return text or default


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > 2_000_000:
        raise ValueError("invalid_content_length")
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("json_object_required")
    return payload


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


_VALUE_LABEL_RE = re.compile(r"labels=\{([^}]*)\}")


def _labels_from_value_string(value: Any) -> dict[str, str]:
    text = str(value or "")
    found: dict[str, str] = {}
    for match in _VALUE_LABEL_RE.finditer(text):
        for item in match.group(1).split(","):
            key, sep, value_text = item.partition("=")
            if sep and key.strip() and value_text.strip():
                found[key.strip()] = value_text.strip().strip('"')
    return found


def _labels_for_alert(alert: dict[str, Any]) -> dict[str, str]:
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    merged = _labels_from_value_string(alert.get("valueString"))
    merged.update({str(k): str(v) for k, v in labels.items() if v is not None})
    return merged


def _annotations_for_alert(alert: dict[str, Any]) -> dict[str, str]:
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {str(k): str(v) for k, v in annotations.items() if v is not None}


def _payload_common_labels(payload: dict[str, Any]) -> dict[str, str]:
    common_labels = payload.get("commonLabels") if isinstance(payload.get("commonLabels"), dict) else {}
    return {str(k): str(v) for k, v in common_labels.items() if v is not None}


def _common_labels_for_payload(payload: dict[str, Any], alerts: list[dict[str, Any]]) -> dict[str, str]:
    labels = _labels_for_alert(alerts[0]) if alerts else {}
    labels.update(_payload_common_labels(payload))
    return labels


def _common_annotations_for_payload(payload: dict[str, Any], alerts: list[dict[str, Any]]) -> dict[str, str]:
    annotations = _annotations_for_alert(alerts[0]) if alerts else {}
    common_annotations = payload.get("commonAnnotations") if isinstance(payload.get("commonAnnotations"), dict) else {}
    annotations.update({str(k): str(v) for k, v in common_annotations.items() if v is not None})
    return annotations


_STATUS_META = {
    "firing": ("⚠️", "触发中"),
    "resolved": ("✅", "已恢复"),
    "pending": ("⏳", "待确认"),
    "no_data": ("❔", "无数据"),
    "error": ("🚨", "执行异常"),
}


def _status_meta(status: Any) -> tuple[str, str]:
    return _STATUS_META.get(str(status or "unknown").strip().lower(), ("ℹ️", "未知"))


def _severity_label(severity: Any) -> str:
    text = str(severity or "unknown").strip().lower() or "unknown"
    names = {
        "critical": "critical",
        "error": "error",
        "warning": "warning",
        "warn": "warning",
        "info": "info",
    }
    return names.get(text, text)


def _severity_display(severity: Any) -> str:
    text = _severity_label(severity)
    labels = {
        "critical": "🚨 critical / 严重",
        "error": "🚨 error / 错误",
        "warning": "⚠️ warning / 警告",
        "info": "ℹ️ info / 信息",
    }
    return labels.get(text, text)


def _header_template(status: str, severity: str) -> str:
    if str(status or "").lower() == "resolved":
        return "green"
    severity_text = _severity_label(severity)
    if severity_text in {"critical", "error", "fatal"}:
        return "red"
    if severity_text in {"warning", "warn"}:
        return "orange"
    return "blue"


_SUMMARY_LABEL_KEYS = ["alertname", "severity", "environment", "service", "target_service", "grafana_folder"]
_SOURCE_LABEL_KEYS = [
    "service",
    "environment",
    "target_service",
    "route",
    "job",
    "instance",
    "client_role",
    "request_kind",
    "operation",
    "order_family_type",
    "ib_error_code",
    "host",
    "port",
    "mountpoint",
    "grafana_folder",
    "alert_source",
]
_LABEL_ALIASES = {
    "alertname": "alert",
    "severity": "severity",
    "environment": "env",
    "service": "service",
    "target_service": "target",
    "grafana_folder": "folder",
    "alert_source": "source",
}


def _label_line(labels: dict[str, str], keys: list[str], *, default: str = "") -> str:
    parts = []
    seen: set[str] = set()
    for key in keys:
        value = str(labels.get(key) or "").strip()
        if not value or key in seen:
            continue
        seen.add(key)
        parts.append(f"{_LABEL_ALIASES.get(key, key)}={value}")
    return " | ".join(parts) if parts else default


def _alert_source_line(labels: dict[str, str]) -> str:
    return _label_line(labels, _SOURCE_LABEL_KEYS, default="labels unavailable")


def _alert_title(payload: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    common_labels = _common_labels_for_payload(payload, alerts)
    common_annotations = _common_annotations_for_payload(payload, alerts)
    status = str(payload.get("status") or (alerts[0].get("status") if alerts else "unknown")).lower()
    icon, status_label = _status_meta(status)
    alertname = _first_text(common_labels.get("alertname"), payload.get("title"), default="GrafanaAlert")
    summary = _first_text(common_annotations.get("summary"), common_annotations.get("description"), default=alertname)
    return f"{icon} {status_label} · {summary}"


def _max_alert_items() -> int:
    try:
        return max(1, int(os.environ.get("FEISHU_ALERT_MAX_ITEMS") or "8"))
    except ValueError:
        return 8


def _clean_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("0001-01-01"):
        return ""
    return text


def _dashboard_url(payload: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    for key in ("panelURL", "dashboardURL", "generatorURL"):
        for alert in alerts:
            value = str(alert.get(key) or "").strip()
            if value:
                return value
    return _first_text(payload.get("externalURL"), os.environ.get("GRAFANA_PUBLIC_URL"), default="https://quant-monitor.lzw-glory.top")


def _alert_block(idx: int, alert: dict[str, Any], fallback_status: str) -> str:
    labels = _labels_for_alert(alert)
    annotations = _annotations_for_alert(alert)
    status = str(alert.get("status") or fallback_status or "unknown").lower()
    icon, status_label = _status_meta(status)
    alertname = _first_text(labels.get("alertname"), default="GrafanaAlert")
    lines = [f"**{idx}. {icon} {status_label}** · `{alertname}`"]
    item_summary = _first_text(annotations.get("summary"), annotations.get("description"), default="")
    if item_summary:
        lines.append(f"**摘要**: {item_summary}")
    source = _alert_source_line(labels)
    if source and source != "labels unavailable":
        lines.append(f"**标签**: {source}")
    starts_at = _clean_time(alert.get("startsAt"))
    ends_at = _clean_time(alert.get("endsAt"))
    if starts_at:
        lines.append(f"**开始**: {starts_at}")
    if ends_at and status == "resolved":
        lines.append(f"**恢复**: {ends_at}")
    return "\n".join(lines)


def _build_card(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    alerts = [item for item in (payload.get("alerts") or []) if isinstance(item, dict)]
    status = str(payload.get("status") or "firing").lower()
    icon, status_label = _status_meta(status)
    title = _alert_title(payload, alerts)
    common_labels = _common_labels_for_payload(payload, alerts)
    common_annotations = _common_annotations_for_payload(payload, alerts)
    severity = _severity_label(common_labels.get("severity"))
    template = _header_template(status, severity)
    summary = _first_text(common_annotations.get("summary"), payload.get("message"), default="Grafana alert notification")
    description = _first_text(common_annotations.get("description"), default="")
    dashboard_url = _dashboard_url(payload, alerts)
    max_alerts = _max_alert_items()
    group_line = _label_line(_payload_common_labels(payload) or common_labels, _SUMMARY_LABEL_KEYS, default="")

    summary_lines = [
        f"**状态**: {icon} {status_label}",
        f"**级别**: {_severity_display(severity)}",
        f"**摘要**: {summary}",
        f"**实例**: {len(alerts)} 个",
    ]
    if description and description != summary:
        summary_lines.append(f"**说明**: {description}")
    if group_line:
        summary_lines.append(f"**分组**: {group_line}")

    detail_blocks = [_alert_block(idx, alert, status) for idx, alert in enumerate(alerts[:max_alerts], start=1)]
    if len(alerts) > max_alerts:
        detail_blocks.append(f"还有 {len(alerts) - max_alerts} 个实例未展开。")
    detail_content = "**实例明细**:\n" + ("\n\n".join(detail_blocks) if detail_blocks else "无实例明细")
    footer_content = f"**通知时间**: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    actions = []
    if dashboard_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开 Grafana 面板"},
            "type": "primary" if status == "firing" else "default",
            "multi_url": {"url": dashboard_url, "pc_url": dashboard_url, "ios_url": dashboard_url, "android_url": dashboard_url},
        })
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title[:120]}},
        "elements": [
            {"tag": "markdown", "content": "\n".join(summary_lines)},
            {"tag": "hr"},
            {"tag": "markdown", "content": detail_content},
            {"tag": "hr"},
            {"tag": "markdown", "content": footer_content},
        ],
    }
    if actions:
        card["elements"].append({"tag": "action", "actions": actions})
    env = _first_text(
        common_labels.get("environment"),
        os.environ.get("IBKR_BROKER_MODE"),
        default="live",
    )
    return card, normalize_environment(env, "live")


def _send_via_ibkr_api(card: dict[str, Any], environment: str, title: str) -> dict[str, Any]:
    api_url = os.environ.get("IBKR_API_NOTIFY_URL", "http://127.0.0.1:5102/api/custom/system/event")
    detail = {"来源": "Grafana", "Grafana": os.environ.get("GRAFANA_PUBLIC_URL", "https://quant-monitor.lzw-glory.top")}
    title_lower = title.lower()
    payload = {
        "event_type": "alert",
        "level": "error" if any(token in title_lower for token in ("critical", "firing", "触发中", "🚨", "⚠️")) else "warning",
        "source": "grafana",
        "title": title,
        "detail": detail,
        "environment": environment,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"success": 200 <= response.status < 300, "status": response.status, "body": body[:500], "via": "ibkr-api"}
    except Exception as exc:
        return {"success": False, "error": str(exc), "via": "ibkr-api"}


def send_feishu(card: dict[str, Any], environment: str, title: str) -> dict[str, Any]:
    if os.environ.get("FEISHU_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "on"}:
        return {"success": True, "skipped": True, "dry_run": True}
    chat_id = _first_text(os.environ.get("FEISHU_ALERT_CHAT_ID"), DEFAULT_FEISHU_ALERT_CHAT_ID)
    if feishu_send_interactive is not None and chat_id:
        try:
            return dict(feishu_send_interactive(card, chat_id, environment, normalize_environment=normalize_environment) or {})
        except Exception as exc:
            fallback = _send_via_ibkr_api(card, environment, title)
            fallback["direct_error"] = str(exc)
            return fallback
    return _send_via_ibkr_api(card, environment, title)


def metrics_text() -> str:
    lines = [
        "# HELP feishu_alert_relay_up Whether the Feishu alert relay is running.",
        "# TYPE feishu_alert_relay_up gauge",
        "feishu_alert_relay_up 1",
        "# HELP feishu_alert_relay_uptime_seconds Relay uptime seconds.",
        "# TYPE feishu_alert_relay_uptime_seconds gauge",
        f"feishu_alert_relay_uptime_seconds {time.time() - STARTED_AT:.3f}",
    ]
    for name, value in sorted(COUNTERS.items()):
        lines.extend([f"# HELP feishu_alert_relay_{name} Relay counter {name}.", f"# TYPE feishu_alert_relay_{name} counter", f"feishu_alert_relay_{name} {int(value)}"])
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-alert-relay/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        COUNTERS["requests_total"] += 1
        if self.path == "/health":
            _json_response(self, 200, {"ok": True, "service": "feishu-alert-relay"})
        elif self.path == "/metrics":
            _text_response(self, 200, metrics_text(), "text/plain; version=0.0.4; charset=utf-8")
        else:
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        COUNTERS["requests_total"] += 1
        if self.path != "/grafana":
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        try:
            payload = _read_json(self)
            card, environment = _build_card(payload)
            title = str((card.get("header") or {}).get("title", {}).get("content") or "Grafana Alert")
            result = send_feishu(card, environment, title)
        except ValueError as exc:
            COUNTERS["bad_requests_total"] += 1
            _json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            COUNTERS["feishu_send_error_total"] += 1
            _json_response(self, 500, {"ok": False, "error": str(exc)})
            return
        COUNTERS["grafana_notifications_total"] += 1
        if result.get("success"):
            COUNTERS["feishu_send_success_total"] += 1
            _json_response(self, 200, {"ok": True, "environment": environment, "result": {k: v for k, v in result.items() if k not in {"data"}}})
        else:
            COUNTERS["feishu_send_error_total"] += 1
            _json_response(self, 502, {"ok": False, "environment": environment, "result": {k: v for k, v in result.items() if k not in {"response_body"}}})


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay Grafana alert webhooks to the existing Feishu alert chat.")
    parser.add_argument("--listen", default=os.environ.get("FEISHU_ALERT_RELAY_LISTEN", "127.0.0.1:9812"))
    args = parser.parse_args()
    host, _, port_text = args.listen.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port_text or "9812")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"feishu-alert-relay listening on {host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
