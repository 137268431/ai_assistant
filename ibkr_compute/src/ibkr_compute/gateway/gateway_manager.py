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

logger = logging.getLogger(__name__)

GATEWAY_DIR = os.environ.get("IBKR_GATEWAY_DIR", "/opt/ibkr/clientportal.gw")
GATEWAY_CONF = os.environ.get("IBKR_GATEWAY_CONF", "root/conf.yaml")


class GatewayManager:
    def __init__(self, gateway_dir: str = None, conf_path: str = None):
        self.gateway_dir = gateway_dir or GATEWAY_DIR
        self.conf_path = conf_path or GATEWAY_CONF
        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None

    @property
    def run_script(self) -> str:
        return os.path.join(self.gateway_dir, "bin", "run.sh")

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def start(self) -> bool:
        if self.is_running:
            logger.info("Gateway already running (pid=%d)", self._process.pid)
            return True

        if not os.path.isfile(self.run_script):
            logger.error("Gateway run script not found: %s", self.run_script)
            return False

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
        if not self.is_running:
            logger.info("Gateway not running")
            return True

        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            self._process.wait(timeout=15)
            logger.info("Gateway stopped")
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            self._process.wait(timeout=5)
            logger.warning("Gateway force killed")
        except Exception as e:
            logger.error("Error stopping Gateway: %s", e)
            return False
        finally:
            self._process = None
            self._start_time = None

        return True

    def restart(self) -> bool:
        logger.info("Restarting Gateway...")
        self.stop()
        time.sleep(3)
        return self.start()

    @property
    def uptime_seconds(self) -> Optional[float]:
        if self._start_time and self.is_running:
            return time.time() - self._start_time
        return None

    @property
    def pid(self) -> Optional[int]:
        if self._process and self.is_running:
            return self._process.pid
        return None

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "pid": self.pid,
            "uptime_s": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
            "gateway_dir": self.gateway_dir,
            "conf_path": self.conf_path,
        }
