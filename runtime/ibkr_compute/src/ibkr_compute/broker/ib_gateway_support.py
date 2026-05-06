from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from ibkr_compute.core.time_utils import ET


DEFAULT_HOST = os.environ.get("IBGW_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("IBGW_PORT", "4001"))
DEFAULT_CLIENT_ID = int(os.environ.get("IBGW_CLIENT_ID", "31"))
DEFAULT_CONNECT_TIMEOUT_SECONDS = max(3, int(os.environ.get("IBGW_CONNECT_TIMEOUT_SEC", "10")))
DEFAULT_LOGIN_TIMEOUT_SECONDS = max(30, int(os.environ.get("IBKR_LOGIN_TIMEOUT", "180")))
DEFAULT_LOGIN_POLL_INTERVAL_SECONDS = max(1, int(os.environ.get("IBKR_LOGIN_POLL_INTERVAL_SEC", "5")))
DEFAULT_SERVICE_NAME = os.environ.get("IBKR_GATEWAY_SYSTEMD_SERVICE", "ibkr-gateway").strip() or "ibkr-gateway"
DEFAULT_ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live").strip().lower() or "live"

TICK_LAST_PRICE = 4
TICK_BID_PRICE = 1
TICK_ASK_PRICE = 2
TICK_BID_SIZE = 0
TICK_ASK_SIZE = 3
TICK_LAST_SIZE = 5
TICK_VOLUME = 8
TICK_LAST_TIMESTAMP = 45
BENIGN_ERROR_CODES = {2104, 2106, 2107, 2108, 2158}
PREFERRED_CONTRACT_EXCHANGES = (
    "NYSE",
    "NASDAQ",
    "ARCA",
    "AMEX",
    "CBOE",
    "BATS",
    "IEX",
    "ISLAND",
    "SMART",
)
PREFERRED_CONTRACT_SEC_TYPE_SCORE = {
    "STK": 80,
    "ETF": 75,
    "IND": 70,
}
FRESH_PROBE_CLIENT_ID_START = max(DEFAULT_CLIENT_ID + 100, 9000)
FRESH_PROBE_RETRIES = max(1, int(os.environ.get("IBGW_FRESH_PROBE_RETRIES", "3")))

_fresh_probe_client_id_lock = threading.Lock()
_fresh_probe_client_id_seq = FRESH_PROBE_CLIENT_ID_START


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


def _iso_now() -> str:
    return datetime.now(ET).isoformat()


def _next_fresh_probe_client_id(exclude: int = 0) -> int:
    global _fresh_probe_client_id_seq
    with _fresh_probe_client_id_lock:
        _fresh_probe_client_id_seq = max(
            int(_fresh_probe_client_id_seq or FRESH_PROBE_CLIENT_ID_START) + 1,
            FRESH_PROBE_CLIENT_ID_START,
            int(exclude or 0) + 1,
        )
        return int(_fresh_probe_client_id_seq)


def _run_command(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


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


def _ib_timestamp_to_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit() and len(text) == 8:
        try:
            dt = datetime.strptime(text, "%Y%m%d")
            return int(dt.replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            pass
    if text.isdigit():
        raw = int(text)
        return raw if raw > 1_000_000_000_000 else raw * 1000
    for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d-%H:%M:%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(text.split(" ", 1)[0] if fmt == "%Y%m%d" else text, fmt)
            return int(dt.replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    return 0


@dataclass
class _PendingRequest:
    kind: str
    event: threading.Event = field(default_factory=threading.Event)
    items: list = field(default_factory=list)
    error: str = ""


@dataclass
class _AccountUpdatesCapture:
    account: str
    event: threading.Event = field(default_factory=threading.Event)
    summary: Dict[str, dict] = field(default_factory=dict)
    positions: Dict[str, dict] = field(default_factory=dict)


__all__ = [name for name in globals() if name.startswith("DEFAULT_") or name.startswith("TICK_")] + [
    "BENIGN_ERROR_CODES",
    "FRESH_PROBE_CLIENT_ID_START",
    "FRESH_PROBE_RETRIES",
    "PREFERRED_CONTRACT_EXCHANGES",
    "PREFERRED_CONTRACT_SEC_TYPE_SCORE",
    "_AccountUpdatesCapture",
    "_PendingRequest",
    "_ib_timestamp_to_ms",
    "_iso_now",
    "_next_fresh_probe_client_id",
    "_pid_uptime_seconds",
    "_run_command",
    "_safe_float",
    "_safe_int",
    "_systemctl_show",
]
