"""
IB Gateway 进程管理
- 启动/停止/重启 Client Portal Gateway
- 进程监控与健康检查
"""

import os
import time
import signal
import subprocess
import logging
from typing import Optional

import requests

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_DIR = os.environ.get("IBKR_GATEWAY_DIR", "/opt/ibkr/clientportal.gw")
GATEWAY_CONF = os.environ.get("IBKR_GATEWAY_CONF", "root/conf.yaml")
GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001").rstrip("/")
GATEWAY_HEALTH_PATH = os.environ.get("IBKR_GATEWAY_HEALTH_PATH", "/v1/api/tickle")
GATEWAY_SYSTEMD_SERVICE = os.environ.get("IBKR_GATEWAY_SYSTEMD_SERVICE", "ibkr-gateway")
GATEWAY_PROCESS_PATTERN = os.environ.get(
    "IBKR_GATEWAY_PROCESS_PATTERN",
    "ibgroup.web.core.clientportal.gw.GatewayStart",
)


class GatewayManager:
    def __init__(self, gateway_dir: str = None, conf_path: str = None):
        self.gateway_dir = gateway_dir or GATEWAY_DIR
        self.conf_path = conf_path or GATEWAY_CONF
        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None
        self._session = requests.Session()
        self._session.verify = False
        load_cookies(self._session)

    @property
    def run_script(self) -> str:
        return os.path.join(self.gateway_dir, "bin", "run.sh")

    @property
    def health_url(self) -> str:
        return f"{GATEWAY_URL}{GATEWAY_HEALTH_PATH}"

    def _run_command(self, args, timeout: int = 10) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except Exception:
            return None

    def _systemctl(self, action: str) -> bool:
        if not GATEWAY_SYSTEMD_SERVICE:
            return False
        result = self._run_command(["systemctl", action, GATEWAY_SYSTEMD_SERVICE], timeout=20)
        if result and result.returncode == 0:
            return True
        if result and result.stderr:
            logger.debug("systemctl %s %s failed: %s", action, GATEWAY_SYSTEMD_SERVICE, result.stderr.strip())
        return False

    def _systemd_is_active(self) -> bool:
        if not GATEWAY_SYSTEMD_SERVICE:
            return False
        result = self._run_command(["systemctl", "is-active", GATEWAY_SYSTEMD_SERVICE], timeout=5)
        return bool(result and result.returncode == 0 and result.stdout.strip() == "active")

    def recent_logs(self, lines: int = 30, since_minutes: int = 5) -> list[str]:
        if not GATEWAY_SYSTEMD_SERVICE:
            return []
        tail_lines = max(1, int(lines or 0))
        minutes = max(1, int(since_minutes or 0))
        result = self._run_command(
            [
                "journalctl",
                "-u",
                GATEWAY_SYSTEMD_SERVICE,
                "--since",
                f"{minutes} min ago",
                "--no-pager",
                "-n",
                str(tail_lines),
            ],
            timeout=10,
        )
        if not result or result.returncode != 0:
            return []
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()][-tail_lines:]

    def _probe_gateway(self) -> tuple[bool, Optional[int]]:
        try:
            load_cookies(self._session)
            resp = self._session.post(self.health_url, timeout=3)
            save_cookies(self._session)
            return True, resp.status_code
        except Exception:
            return False, None

    def _find_external_pid(self) -> Optional[int]:
        result = self._run_command(["pgrep", "-f", GATEWAY_PROCESS_PATTERN], timeout=5)
        if not result or result.returncode not in (0, 1):
            return None

        current_pid = self._process.pid if self._process is not None else None
        for line in (result.stdout or "").splitlines():
            try:
                pid = int(line.strip())
            except (TypeError, ValueError):
                continue
            if current_pid and pid == current_pid:
                continue
            return pid
        return None

    def _pid_uptime_seconds(self, pid: Optional[int]) -> Optional[float]:
        if not pid:
            return None
        result = self._run_command(["ps", "-o", "etimes=", "-p", str(pid)], timeout=5)
        if not result or result.returncode != 0:
            return None
        try:
            return float((result.stdout or "").strip())
        except (TypeError, ValueError):
            return None

    def _kill_pid(self, pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            return True
        except Exception as exc:
            logger.error("Error stopping Gateway pid %s: %s", pid, exc)
            return False

    def _is_managed_process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def is_running(self) -> bool:
        if self._is_managed_process_alive():
            return True
        if self._systemd_is_active():
            return True
        if self._find_external_pid() is not None:
            return True
        reachable, _ = self._probe_gateway()
        return reachable

    def start(self) -> bool:
        if self.is_running:
            logger.info("Gateway already running (pid=%s)", self.pid or "-")
            return True

        if not os.path.isfile(self.run_script):
            logger.error("Gateway run script not found: %s", self.run_script)
            return False

        if self._systemctl("start"):
            self._start_time = time.time()
            logger.info("Gateway started via systemd (%s)", GATEWAY_SYSTEMD_SERVICE)
            time.sleep(5)
            return self.is_running

        try:
            self._process = subprocess.Popen(
                ["bash", self.run_script, self.conf_path],
                cwd=self.gateway_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self._start_time = time.time()
            logger.info("Gateway started (pid=%d), conf=%s", self._process.pid, self.conf_path)
            time.sleep(5)
            return self.is_running
        except Exception as e:
            logger.error("Failed to start Gateway: %s", e)
            return False

    def stop(self) -> bool:
        stopped = False

        try:
            if self._is_managed_process_alive():
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=15)
                logger.info("Gateway stopped (managed subprocess)")
                stopped = True
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            self._process.wait(timeout=5)
            logger.warning("Gateway force killed")
            stopped = True
        except Exception as e:
            logger.error("Error stopping managed Gateway process: %s", e)
        finally:
            self._process = None

        if self._systemd_is_active():
            if self._systemctl("stop"):
                logger.info("Gateway stopped via systemd (%s)", GATEWAY_SYSTEMD_SERVICE)
                stopped = True

        external_pid = self._find_external_pid()
        if external_pid is not None:
            if self._kill_pid(external_pid):
                logger.info("Gateway stopped (external pid=%s)", external_pid)
                stopped = True

        self._start_time = None
        if stopped:
            return True
        if not self.is_running:
            logger.info("Gateway not running")
            return True
        return False

    def restart(self) -> bool:
        logger.info("Restarting Gateway...")
        if self._systemd_is_active() and self._systemctl("restart"):
            self._start_time = time.time()
            time.sleep(5)
            return self.is_running
        self.stop()
        time.sleep(3)
        return self.start()

    @property
    def uptime_seconds(self) -> Optional[float]:
        pid = self.pid
        external_uptime = self._pid_uptime_seconds(pid)
        if external_uptime is not None:
            return external_uptime
        if self._start_time and self.is_running:
            return time.time() - self._start_time
        return None

    @property
    def pid(self) -> Optional[int]:
        if self._is_managed_process_alive():
            return self._process.pid
        return self._find_external_pid()

    def status(self) -> dict:
        reachable, status_code = self._probe_gateway()
        managed_by = "unknown"
        if self._is_managed_process_alive():
            managed_by = "subprocess"
        elif self._systemd_is_active():
            managed_by = "systemd"
        elif self._find_external_pid() is not None:
            managed_by = "external_pid"
        return {
            "running": self.is_running,
            "pid": self.pid,
            "uptime_s": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
            "gateway_dir": self.gateway_dir,
            "conf_path": self.conf_path,
            "gateway_url": GATEWAY_URL,
            "reachable": reachable,
            "status_code": status_code,
            "managed_by": managed_by,
        }
