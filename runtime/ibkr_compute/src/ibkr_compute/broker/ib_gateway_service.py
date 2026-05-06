from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from ibkr_compute.broker.ib_gateway_support import (
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
        return self._systemctl("start") and self.is_running

    def stop(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        return self._systemctl("stop")

    def restart(self) -> bool:
        if self.broker:
            self.broker.disconnect()
        return self._systemctl("restart") and self.is_running

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
        running = str(data.get("ActiveState") or "") == "active"
        status_code = int(broker_status.get("status_code", 0) or 0)
        if not running:
            status_code = 503
        elif running and not status_code:
            status_code = 401
        return {
            "managed_by": "systemd",
            "service": self.service_name,
            "pid": _safe_int(data.get("MainPID"), 0),
            "uptime_s": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
            "reachable": bool(running and status_code != 503),
            "running": running,
            "status_code": status_code,
            "active_state": str(data.get("ActiveState") or ""),
            "sub_state": str(data.get("SubState") or ""),
            "active_since": str(data.get("ActiveEnterTimestamp") or ""),
            "unit_file_state": str(data.get("UnitFileState") or ""),
            "broker": broker_status,
        }
