#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HOST = os.environ.get("MONITORING_DEPLOY_HOST") or os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.246")
DEFAULT_REMOTE_PYTHON = os.environ.get("MONITORING_REMOTE_PYTHON", "python3")
DEFAULT_PROMETHEUS_URL = os.environ.get("MONITORING_PROMETHEUS_URL", "http://127.0.0.1:9090")
DEFAULT_NODE_EXPORTER_URL = os.environ.get("MONITORING_NODE_EXPORTER_URL", "http://127.0.0.1:9100")
DEFAULT_GRAFANA_URL = os.environ.get("MONITORING_GRAFANA_URL", "http://127.0.0.1:3000")
DEFAULT_PUBLIC_URL = os.environ.get("MONITORING_PUBLIC_BASE_URL", "https://quant-monitor.lzw-glory.top")
DEFAULT_MONITORING_REMOTE_ROOT = os.environ.get("MONITORING_REMOTE_ROOT", "/opt/monitoring").rstrip("/")
DEFAULT_CADDY_SITE_FILE = os.environ.get("MONITORING_CADDY_SITE_FILE", "/etc/caddy/conf.d/quant-monitor.lzw-glory.top.caddy")
DEFAULT_CADDY_AUTH_FILE = os.environ.get(
    "MONITORING_CADDY_AUTH_FILE",
    "/etc/caddy/secrets/quant-monitor.lzw-glory.top.auth.caddy",
)
DEFAULT_GRAFANA_ENV_FILE = os.environ.get("GRAFANA_ENV_FILE", "/etc/monitoring/grafana.env")

REMOTE_SCRIPT = r'''
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request

config = json.loads(base64.b64decode(os.environ["MONITORING_HEALTH_CONFIG_B64"]).decode("utf-8"))

def fetch_url(url, timeout=5, max_body=4096):
    request = urllib.request.Request(url, headers={"User-Agent": "codex-monitoring-health-check"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_body).decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
            payload = {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "url": url,
                "content_type": content_type,
            }
            if "json" in content_type:
                try:
                    payload["json"] = json.loads(body)
                except Exception:
                    payload["body_snippet"] = body[:300]
            else:
                payload["body_snippet"] = body[:300]
            return payload
    except urllib.error.HTTPError as exc:
        body = exc.read(max_body).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": exc.code,
            "url": url,
            "body_snippet": body[:300],
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}

def systemctl_show(service):
    cmd = [
        "systemctl",
        "show",
        service,
        "--property=Id,ActiveState,SubState,MainPID,UnitFileState",
        "--no-pager",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except Exception as exc:
        return {"service": service, "ok": False, "error": str(exc)}
    payload = {"service": service, "ok": proc.returncode == 0}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            payload[key] = value
    if proc.stderr.strip():
        payload["stderr"] = proc.stderr.strip()
    payload["active"] = payload.get("ActiveState") == "active"
    return payload

def path_status(path, redact=False):
    exists = os.path.exists(path)
    payload = {"path": path, "exists": exists}
    if exists:
        stat_result = os.stat(path)
        payload["mode"] = oct(stat_result.st_mode & 0o777)
        payload["size"] = None if redact else stat_result.st_size
    return payload

def join_url(base, suffix):
    return base.rstrip("/") + suffix

prometheus_url = config["prometheus_url"].rstrip("/")
node_exporter_url = config["node_exporter_url"].rstrip("/")
grafana_url = config["grafana_url"].rstrip("/")
remote_root = config["remote_root"].rstrip("/")

services = {name: systemctl_show(name) for name in ["prometheus.service", "node-exporter.service", "grafana-server.service"]}
endpoints = {
    "prometheus_ready": fetch_url(join_url(prometheus_url, "/-/ready"), timeout=config["timeout"]),
    "prometheus_targets": fetch_url(join_url(prometheus_url, "/api/v1/targets?state=active"), timeout=config["timeout"], max_body=1048576),
    "node_exporter_metrics": fetch_url(join_url(node_exporter_url, "/metrics"), timeout=config["timeout"]),
    "grafana_health": fetch_url(join_url(grafana_url, "/api/health"), timeout=config["timeout"]),
}

required_files = [
    remote_root + "/prometheus/prometheus.yml",
    remote_root + "/grafana/grafana.ini",
    remote_root + "/grafana/provisioning/datasources/prometheus.yml",
    remote_root + "/grafana/provisioning/dashboards/dashboards.yml",
    remote_root + "/grafana/dashboards/quant-monitoring-overview.json",
    "/etc/systemd/system/prometheus.service",
    "/etc/systemd/system/node-exporter.service",
    "/etc/systemd/system/grafana-server.service",
    config["grafana_env_file"],
]
if config.get("check_caddy", True):
    required_files.extend([config["caddy_site_file"], config["caddy_auth_file"]])
files = {path: path_status(path, redact=path in {config["caddy_auth_file"], config["grafana_env_file"]}) for path in required_files}

targets = []
target_payload = endpoints["prometheus_targets"].get("json")
if isinstance(target_payload, dict):
    for item in target_payload.get("data", {}).get("activeTargets", []) or []:
        labels = item.get("labels") if isinstance(item, dict) else {}
        targets.append(
            {
                "scrape_url": item.get("scrapeUrl"),
                "health": item.get("health"),
                "job": (labels or {}).get("job"),
                "instance": (labels or {}).get("instance"),
                "last_error": item.get("lastError") or "",
            }
        )

print(json.dumps({"services": services, "endpoints": endpoints, "files": files, "targets": targets}, sort_keys=True))
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the monitoring-only Prometheus/Grafana/node_exporter stack.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument("--remote-root", default=DEFAULT_MONITORING_REMOTE_ROOT)
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--node-exporter-url", default=DEFAULT_NODE_EXPORTER_URL)
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL)
    parser.add_argument("--caddy-site-file", default=DEFAULT_CADDY_SITE_FILE)
    parser.add_argument("--caddy-auth-file", default=DEFAULT_CADDY_AUTH_FILE)
    parser.add_argument("--grafana-env-file", default=DEFAULT_GRAFANA_ENV_FILE)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--strict-targets", action="store_true", help="Fail if any Prometheus target is down.")
    parser.add_argument("--skip-public", action="store_true", help="Skip public URL Basic Auth check.")
    parser.add_argument("--skip-caddy", action="store_true", help="Skip Caddy file and public URL checks.")
    parser.add_argument("--local", action="store_true", help="Run checks on this machine instead of over SSH.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    return args


def decode_json_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def remote_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "remote_root": args.remote_root,
        "prometheus_url": args.prometheus_url,
        "node_exporter_url": args.node_exporter_url,
        "grafana_url": args.grafana_url,
        "caddy_site_file": args.caddy_site_file,
        "caddy_auth_file": args.caddy_auth_file,
        "grafana_env_file": args.grafana_env_file,
        "check_caddy": not args.skip_caddy,
        "timeout": args.timeout,
    }


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    env_blob = base64.b64encode(json.dumps(remote_config(args)).encode("utf-8")).decode("ascii")
    if args.local:
        env = os.environ.copy()
        env["MONITORING_HEALTH_CONFIG_B64"] = env_blob
        proc = subprocess.run(
            [sys.executable, "-c", REMOTE_SCRIPT],
            text=True,
            capture_output=True,
            timeout=max(20, args.timeout * 4),
            env=env,
        )
    else:
        remote_command = (
            f"env MONITORING_HEALTH_CONFIG_B64={shlex.quote(env_blob)} "
            f"{shlex.quote(args.remote_python)} -c {shlex.quote(REMOTE_SCRIPT)}"
        )
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            args.host,
            remote_command,
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=max(30, args.timeout * 5))
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or f"remote check failed with code {proc.returncode}")
    payload = decode_json_payload(proc.stdout)
    payload["runner_returncode"] = proc.returncode
    if proc.stderr.strip():
        payload["runner_stderr"] = proc.stderr.strip()
    return payload


def fetch_public_url(url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-monitoring-health-check"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2048).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "url": url,
                "body_snippet": body[:200],
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(2048).decode("utf-8", errors="replace")
        return {"ok": False, "status_code": exc.code, "url": url, "body_snippet": body[:200]}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def evaluate(payload: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    services = payload.get("services") or {}
    for service_name in ["prometheus.service", "node-exporter.service", "grafana-server.service"]:
        service_payload = services.get(service_name) or {}
        if not service_payload.get("active"):
            failures.append(f"service_inactive:{service_name}:{service_payload.get('ActiveState') or 'unknown'}")

    endpoints = payload.get("endpoints") or {}
    for endpoint_name in ["prometheus_ready", "node_exporter_metrics", "grafana_health"]:
        endpoint_payload = endpoints.get(endpoint_name) or {}
        if not endpoint_payload.get("ok"):
            status = endpoint_payload.get("status_code") or endpoint_payload.get("error") or "unknown"
            failures.append(f"endpoint_unhealthy:{endpoint_name}:{status}")

    files = payload.get("files") or {}
    for path, file_payload in files.items():
        if not (file_payload or {}).get("exists"):
            failures.append(f"missing_file:{path}")

    if args.strict_targets:
        for target in payload.get("targets") or []:
            if target.get("health") != "up":
                failures.append(f"target_down:{target.get('job')}:{target.get('instance')}:{target.get('last_error') or 'down'}")

    public_payload = payload.get("public") or {}
    if not args.skip_public and not args.skip_caddy:
        status_code = public_payload.get("status_code")
        if status_code != 401:
            failures.append(f"public_basic_auth_not_enforced:{status_code or public_payload.get('error') or 'unknown'}")

    return failures


def target_summary(targets: list[dict[str, Any]]) -> tuple[int, int]:
    total = len(targets)
    up = sum(1 for target in targets if target.get("health") == "up")
    return up, total


def print_human(payload: dict[str, Any], failures: list[str], args: argparse.Namespace) -> None:
    status = "ok" if not failures else "fail"
    print(f"monitoring_health={status}")
    print("services:")
    for service_name, service_payload in (payload.get("services") or {}).items():
        active_state = service_payload.get("ActiveState") or "unknown"
        sub_state = service_payload.get("SubState") or "unknown"
        print(f"  {service_name}: {active_state}/{sub_state}")
    print("endpoints:")
    for endpoint_name, endpoint_payload in (payload.get("endpoints") or {}).items():
        marker = "ok" if endpoint_payload.get("ok") else "fail"
        detail = endpoint_payload.get("status_code") or endpoint_payload.get("error") or "unknown"
        print(f"  {endpoint_name}: {marker} ({detail})")
    up, total = target_summary(list(payload.get("targets") or []))
    strict_note = "strict" if args.strict_targets else "non-strict"
    print(f"prometheus_targets: {up}/{total} up ({strict_note})")
    if not args.skip_public and not args.skip_caddy:
        public_payload = payload.get("public") or {}
        public_status = public_payload.get("status_code") or public_payload.get("error") or "unknown"
        auth_state = "basic_auth_enforced" if public_payload.get("status_code") == 401 else "unexpected"
        print(f"public_url: {public_status} ({auth_state})")
    if failures:
        print("failures:")
        for failure in failures:
            print(f"  - {failure}")


def main() -> int:
    args = parse_args()
    try:
        payload = run_remote(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "failures": [f"runner_error:{exc}"]}, sort_keys=True))
        else:
            print(f"monitoring_health=fail\nfailures:\n  - runner_error:{exc}")
        return 1

    if args.skip_public or args.skip_caddy:
        payload["public"] = {"skipped": True}
    else:
        payload["public"] = fetch_public_url(args.public_url, args.timeout)

    failures = evaluate(payload, args)
    payload["ok"] = not failures
    payload["failures"] = failures

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_human(payload, failures, args)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
