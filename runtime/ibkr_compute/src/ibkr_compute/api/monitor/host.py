from __future__ import annotations

import os
import platform
import resource
import shutil
import socket
import sys
import threading
import time


def _api_app():
    from .. import app as api_app

    return api_app


def _copy_active_subscription_map(service) -> dict:
    lock = getattr(service, "_subscription_lock", None)
    if lock:
        with lock:
            return dict(getattr(service, "_active_subscription_map", {}) or {})
    return dict(getattr(service, "_active_subscription_map", {}) or {})


def _normalize_symbol_list(values) -> list[str]:
    seen = set()
    normalized = []
    for item in values or []:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    normalized.sort()
    return normalized


def _parse_proc_kv_text(raw_text: str) -> dict[str, str]:
    payload = {}
    for line in str(raw_text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[str(key).strip()] = str(value).strip()
    return payload


def _parse_meminfo_text(raw_text: str) -> dict[str, int]:
    payload = {}
    for key, value in _parse_proc_kv_text(raw_text).items():
        number_text = str(value).split()[0]
        try:
            payload[str(key)] = int(number_text) * 1024
        except (TypeError, ValueError):
            continue
    return payload


def _read_proc_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_proc_cpu_times() -> dict | None:
    raw_text = _read_proc_text("/proc/stat")
    if not raw_text:
        return None
    for line in raw_text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        if len(parts) < 5:
            return None
        try:
            values = [int(item) for item in parts[1:]]
        except (TypeError, ValueError):
            return None
        total = sum(values)
        iowait = values[4] if len(values) > 4 else 0
        idle = values[3] + iowait
        return {
            "total": total,
            "idle": idle,
            "iowait": iowait,
            "sampled_at": time.time(),
        }
    return None


def _build_cpu_usage_snapshot(previous: dict | None, current: dict | None, source: str = "/proc/stat") -> dict:
    if not previous or not current:
        return {
            "used_pct": None,
            "idle_pct": None,
            "iowait_pct": None,
            "sample_span_s": None,
            "source": source,
        }

    total_delta = int(current.get("total", 0) or 0) - int(previous.get("total", 0) or 0)
    idle_delta = int(current.get("idle", 0) or 0) - int(previous.get("idle", 0) or 0)
    iowait_delta = int(current.get("iowait", 0) or 0) - int(previous.get("iowait", 0) or 0)
    sample_span_s = max(0.0, float(current.get("sampled_at", 0) or 0) - float(previous.get("sampled_at", 0) or 0))
    if total_delta <= 0:
        return {
            "used_pct": None,
            "idle_pct": None,
            "iowait_pct": None,
            "sample_span_s": round(sample_span_s, 3) if sample_span_s > 0 else None,
            "source": source,
        }

    used_pct = max(0.0, min(100.0, ((total_delta - idle_delta) / total_delta) * 100.0))
    idle_pct = max(0.0, min(100.0, (idle_delta / total_delta) * 100.0))
    iowait_pct = max(0.0, min(100.0, (iowait_delta / total_delta) * 100.0))
    return {
        "used_pct": round(used_pct, 2),
        "idle_pct": round(idle_pct, 2),
        "iowait_pct": round(iowait_pct, 2),
        "sample_span_s": round(sample_span_s, 3) if sample_span_s > 0 else None,
        "source": source,
    }


def _collect_cpu_usage_snapshot(prime_interval_s: float = 0.05) -> dict:
    api_app = _api_app()
    with api_app.host_cpu_snapshot_lock:
        previous = api_app.host_cpu_snapshot_cache
        current = _read_proc_cpu_times()
        if not current:
            return {
                "used_pct": None,
                "idle_pct": None,
                "sample_span_s": None,
                "source": "unavailable",
            }

        if previous is None and prime_interval_s > 0:
            previous = current
            time.sleep(prime_interval_s)
            current = _read_proc_cpu_times() or current

        api_app.host_cpu_snapshot_cache = current

    return _build_cpu_usage_snapshot(previous, current)


def _collect_host_memory_snapshot() -> dict:
    meminfo = _parse_meminfo_text(_read_proc_text("/proc/meminfo"))
    total_bytes = int(meminfo.get("MemTotal", 0) or 0)
    available_bytes = int(meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) or 0)
    if total_bytes <= 0:
        return {
            "total_bytes": None,
            "available_bytes": None,
            "used_bytes": None,
            "used_pct": None,
            "source": "unavailable",
        }
    used_bytes = max(0, total_bytes - max(0, available_bytes))
    return {
        "total_bytes": total_bytes,
        "available_bytes": max(0, available_bytes),
        "used_bytes": used_bytes,
        "used_pct": round((used_bytes / total_bytes) * 100.0, 2),
        "source": "/proc/meminfo",
    }


def _collect_disk_snapshot(path: str | None = None) -> dict:
    target_path = str(path or os.environ.get("IBKR_MONITOR_DISK_PATH", "/") or "/")
    try:
        usage = shutil.disk_usage(target_path)
    except OSError:
        return {
            "path": target_path,
            "total_bytes": None,
            "free_bytes": None,
            "used_bytes": None,
            "used_pct": None,
        }
    used_bytes = max(0, int(usage.total or 0) - int(usage.free or 0))
    used_pct = round((used_bytes / usage.total) * 100.0, 2) if usage.total else None
    return {
        "path": target_path,
        "total_bytes": int(usage.total or 0),
        "free_bytes": int(usage.free or 0),
        "used_bytes": used_bytes,
        "used_pct": used_pct,
    }


def _collect_load_snapshot() -> dict:
    cpu_count = max(1, int(os.cpu_count() or 1))
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = None
    return {
        "cpu_count": cpu_count,
        "loadavg": {
            "1": round(load1, 2) if load1 is not None else None,
            "5": round(load5, 2) if load5 is not None else None,
            "15": round(load15, 2) if load15 is not None else None,
            "per_cpu_1": round(load1 / cpu_count, 3) if load1 is not None and cpu_count > 0 else None,
        },
    }


def _resource_rss_bytes() -> int | None:
    try:
        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    except Exception:
        return None
    if rss <= 0:
        return None
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def _collect_process_snapshot() -> dict:
    api_app = _api_app()
    proc_status = _parse_proc_kv_text(_read_proc_text("/proc/self/status"))
    thread_count = None
    try:
        thread_count = int(proc_status.get("Threads", "0") or 0)
    except (TypeError, ValueError):
        thread_count = None
    fd_count = None
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            fd_count = len([name for name in os.listdir(path) if name not in {".", ".."}])
            break
        except OSError:
            continue
    return {
        "pid": os.getpid(),
        "uptime_s": round(time.time() - api_app._start_time, 1),
        "rss_bytes": _resource_rss_bytes(),
        "threads": thread_count if thread_count is not None else threading.active_count(),
        "fd_count": fd_count,
    }


def _collect_host_snapshot() -> dict:
    load_snapshot = _collect_load_snapshot()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": load_snapshot["cpu_count"],
        "cpu": _collect_cpu_usage_snapshot(),
        "loadavg": load_snapshot["loadavg"],
        "memory": _collect_host_memory_snapshot(),
        "disk": _collect_disk_snapshot(),
        "process": _collect_process_snapshot(),
    }
