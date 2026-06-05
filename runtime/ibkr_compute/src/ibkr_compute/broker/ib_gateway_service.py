from __future__ import annotations

import logging
import socket
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from ibkr_compute.observability.prometheus import (
    record_gateway_service_action,
    record_gateway_socket_probe,
    set_gateway_status,
)
from ibkr_compute.broker.ib_gateway_support import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SERVICE_NAME,
    _run_command,
    _safe_int,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ibkr_compute.broker.ib_gateway import BrokerAdapter


def _systemctl_show(service: str) -> Dict[str, str]:
    proc = _run_command(
        [
            "systemctl",
            "show",
            service,
            "--property=Id,ActiveState,SubState,MainPID,ActiveEnterTimestamp,UnitFileState",
            "--no-pager",
        ],
        timeout=20,
    )
    data: Dict[str, str] = {}
    if proc is None:
        return data
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _pid_uptime_seconds(pid: int) -> Optional[float]:
    if int(pid or 0) <= 0:
        return None
    proc = _run_command(["ps", "-o", "etimes=", "-p", str(int(pid))], timeout=5)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return float((proc.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def _api_socket_status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict:
    host = str(host or DEFAULT_HOST)
    port = int(port or DEFAULT_PORT)
    started = time.perf_counter()
    proc = _run_command(["ss", "-ltn"], timeout=5)
    if proc is not None and proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0].upper() != "LISTEN":
                continue
            local_address = parts[3].strip()
            if local_address.rsplit(":", 1)[-1] == str(port):
                duration_s = time.perf_counter() - started
                record_gateway_socket_probe(
                    host=host,
                    port=port,
                    source="ss",
                    result="ok",
                    reason_code="listening",
                    duration_s=duration_s,
                    listening=True,
                )
                return {
                    "listening": True,
                    "host": host,
                    "port": port,
                    "source": "ss",
                    "reason": "",
                }
        duration_s = time.perf_counter() - started
        record_gateway_socket_probe(
            host=host,
            port=port,
            source="ss",
            result="error",
            reason_code="port_not_listening",
            duration_s=duration_s,
            listening=False,
        )
        return {
            "listening": False,
            "host": host,
            "port": port,
            "source": "ss",
            "reason": "port_not_listening",
        }

    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.5):
            pass
        duration_s = time.perf_counter() - started
        record_gateway_socket_probe(
            host=host,
            port=port,
            source="socket",
            result="ok",
            reason_code="listening",
            duration_s=duration_s,
            listening=True,
        )
        return {
            "listening": True,
            "host": host,
            "port": port,
            "source": "socket",
            "reason": "",
        }
    except OSError as exc:
        duration_s = time.perf_counter() - started
        record_gateway_socket_probe(
            host=host,
            port=port,
            source="socket",
            result="error",
            reason_code="connect_failed",
            duration_s=duration_s,
            listening=False,
        )
        return {
            "listening": False,
            "host": host,
            "port": port,
            "source": "socket",
            "reason": str(exc) or "connect_failed",
        }


class GatewayServiceManager:
    def __init__(self, service_name: str = DEFAULT_SERVICE_NAME, broker: Optional["BrokerAdapter"] = None):
        self.service_name = str(service_name or DEFAULT_SERVICE_NAME)
        self.broker = broker

    def _systemctl(self, action: str) -> bool:
        proc = _run_command(["systemctl", action, self.service_name], timeout=30)
        return bool(proc and proc.returncode == 0)

    @property
    def pid(self) -> int:
        return _safe_int(_systemctl_show(self.service_name).get("MainPID"), 0)

    @property
    def is_running(self) -> bool:
        return str(_systemctl_show(self.service_name).get("ActiveState") or "") == "active"

    @property
    def uptime_seconds(self) -> Optional[float]:
        return _pid_uptime_seconds(self.pid)

    def start(self) -> bool:
        ok = self._systemctl("start") and self.is_running
        record_gateway_service_action(action="start", result="ok" if ok else "error")
        return ok

    def stop(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        ok = self._systemctl("stop")
        record_gateway_service_action(action="stop", result="ok" if ok else "error")
        return ok

    def restart(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        ok = self._systemctl("restart") and self.is_running
        record_gateway_service_action(action="restart", result="ok" if ok else "error")
        return ok

    def recent_logs(self, lines: int = 50, since_minutes: int = 10) -> List[str]:
        proc = _run_command(
            [
                "journalctl",
                "-u",
                self.service_name,
                "-n",
                str(max(1, int(lines))),
                "--since",
                f"{max(1, int(since_minutes))} minutes ago",
                "--no-pager",
            ],
            timeout=20,
        )
        if proc is None:
            return []
        return [line for line in (proc.stdout or "").splitlines() if line.strip()]

    def status(self) -> dict:
        data = _systemctl_show(self.service_name)
        broker_status = self.broker.status() if self.broker else {}
        socket_host = str(broker_status.get("host") or DEFAULT_HOST)
        socket_port = _safe_int(broker_status.get("port"), DEFAULT_PORT)
        api_socket = _api_socket_status(socket_host, socket_port)
        running = str(data.get("ActiveState") or "") == "active"
        status_code = int(broker_status.get("status_code", 0) or 0)
        if not running:
            status_code = 503
        elif not bool(api_socket.get("listening")):
            status_code = 502
        elif running and not status_code:
            status_code = 401
        reachable = bool(running and bool(api_socket.get("listening")) and status_code not in {502, 503})
        set_gateway_status(
            running=running,
            uptime_s=self.uptime_seconds,
            status_code=status_code,
            reachable=reachable,
        )
        return {
            "managed_by": "systemd",
            "service": self.service_name,
            "pid": _safe_int(data.get("MainPID"), 0),
            "uptime_s": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
            "reachable": reachable,
            "running": running,
            "status_code": status_code,
            "api_socket_listening": bool(api_socket.get("listening")),
            "api_socket_host": str(api_socket.get("host") or socket_host),
            "api_socket_port": int(api_socket.get("port") or socket_port or 0),
            "api_socket_source": str(api_socket.get("source") or ""),
            "api_socket_reason": str(api_socket.get("reason") or ""),
            "active_state": str(data.get("ActiveState") or ""),
            "sub_state": str(data.get("SubState") or ""),
            "active_since": str(data.get("ActiveEnterTimestamp") or ""),
            "unit_file_state": str(data.get("UnitFileState") or ""),
            "broker": broker_status,
        }
