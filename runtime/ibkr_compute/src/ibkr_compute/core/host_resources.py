from __future__ import annotations

import copy
import os
import platform
import resource
import shutil
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable


BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _parse_proc_kv_text(raw_text: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in str(raw_text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[str(key).strip()] = str(value).strip()
    return payload


def parse_meminfo_text(raw_text: str) -> dict[str, int]:
    payload: dict[str, int] = {}
    for key, value in _parse_proc_kv_text(raw_text).items():
        number_text = str(value).split()[0]
        try:
            payload[str(key)] = int(number_text) * 1024
        except (TypeError, ValueError):
            continue
    return payload


def read_proc_cpu_times(raw_text: str | None = None, *, sampled_at: float | None = None) -> dict | None:
    text = raw_text if raw_text is not None else _read_text("/proc/stat")
    if not text:
        return None
    for line in str(text or "").splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        if len(parts) < 5:
            return None
        try:
            values = [int(item) for item in parts[1:]]
        except (TypeError, ValueError):
            return None
        iowait = values[4] if len(values) > 4 else 0
        idle = values[3] + iowait
        return {
            "total": sum(values),
            "idle": idle,
            "iowait": iowait,
            "sampled_at": float(sampled_at if sampled_at is not None else time.time()),
        }
    return None


def build_cpu_usage_snapshot(previous: dict | None, current: dict | None, source: str = "/proc/stat") -> dict:
    sampled_at = float((current or {}).get("sampled_at", 0.0) or time.time())
    if not previous or not current:
        return {
            "used_pct": None,
            "idle_pct": None,
            "iowait_pct": None,
            "sample_span_s": None,
            "sampled_at": sampled_at,
            "source": source,
        }

    total_delta = int(current.get("total", 0) or 0) - int(previous.get("total", 0) or 0)
    idle_delta = int(current.get("idle", 0) or 0) - int(previous.get("idle", 0) or 0)
    iowait_delta = int(current.get("iowait", 0) or 0) - int(previous.get("iowait", 0) or 0)
    sample_span_s = max(
        0.0,
        float(current.get("sampled_at", 0) or 0) - float(previous.get("sampled_at", 0) or 0),
    )
    if total_delta <= 0:
        return {
            "used_pct": None,
            "idle_pct": None,
            "iowait_pct": None,
            "sample_span_s": round(sample_span_s, 3) if sample_span_s > 0 else None,
            "sampled_at": sampled_at,
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
        "sampled_at": sampled_at,
        "source": source,
    }


def collect_memory_snapshot(raw_text: str | None = None) -> dict:
    meminfo = parse_meminfo_text(raw_text if raw_text is not None else _read_text("/proc/meminfo"))
    total_bytes = int(meminfo.get("MemTotal", 0) or 0)
    available_bytes = int(meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) or 0)
    if total_bytes <= 0:
        return {
            "total_bytes": None,
            "available_bytes": None,
            "available_pct": None,
            "used_bytes": None,
            "used_pct": None,
            "source": "unavailable",
        }
    available_bytes = max(0, available_bytes)
    used_bytes = max(0, total_bytes - available_bytes)
    return {
        "total_bytes": total_bytes,
        "available_bytes": available_bytes,
        "available_pct": round((available_bytes / total_bytes) * 100.0, 2),
        "used_bytes": used_bytes,
        "used_pct": round((used_bytes / total_bytes) * 100.0, 2),
        "source": "/proc/meminfo",
    }


def collect_disk_snapshot(path: str | None = None) -> dict:
    target_path = str(path or os.environ.get("IBKR_MONITOR_DISK_PATH", "/") or "/")
    try:
        usage = shutil.disk_usage(target_path)
    except OSError:
        return {
            "path": target_path,
            "total_bytes": None,
            "free_bytes": None,
            "free_pct": None,
            "used_bytes": None,
            "used_pct": None,
        }
    total_bytes = int(usage.total or 0)
    free_bytes = int(usage.free or 0)
    used_bytes = max(0, total_bytes - free_bytes)
    return {
        "path": target_path,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "free_pct": round((free_bytes / total_bytes) * 100.0, 2) if total_bytes else None,
        "used_bytes": used_bytes,
        "used_pct": round((used_bytes / total_bytes) * 100.0, 2) if total_bytes else None,
    }


def collect_load_snapshot() -> dict:
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
            "per_cpu_5": round(load5 / cpu_count, 3) if load5 is not None and cpu_count > 0 else None,
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


def collect_process_snapshot(start_time: float | None = None) -> dict:
    proc_status = _parse_proc_kv_text(_read_text("/proc/self/status"))
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
    uptime_s = None
    if start_time is not None:
        uptime_s = round(max(0.0, time.time() - float(start_time or 0.0)), 1)
    return {
        "pid": os.getpid(),
        "uptime_s": uptime_s,
        "rss_bytes": _resource_rss_bytes(),
        "threads": thread_count if thread_count is not None else threading.active_count(),
        "fd_count": fd_count,
    }


def _safe_number(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values) -> float | None:
    for value in values:
        parsed = _safe_number(value)
        if parsed is not None:
            return parsed
    return None


def _window_average(samples: list[dict], key: str, window_seconds: float, now_ts: float) -> float | None:
    cutoff = now_ts - max(1.0, float(window_seconds or 0.0))
    weighted_total = 0.0
    weighted_span = 0.0
    fallback_values = []
    for sample in samples:
        sampled_at = float(sample.get("sampled_at", 0.0) or 0.0)
        if sampled_at < cutoff:
            continue
        value = _safe_number(sample.get(key))
        if value is None:
            continue
        span_s = max(0.0, _safe_number(sample.get("sample_span_s")) or 0.0)
        if span_s > 0:
            weighted_total += value * span_s
            weighted_span += span_s
        fallback_values.append(value)
    if weighted_span > 0:
        return round(weighted_total / weighted_span, 2)
    if fallback_values:
        return round(sum(fallback_values) / len(fallback_values), 2)
    return None


def _config_float(config, key: str, environment: str, default: float) -> float:
    if config is not None and hasattr(config, "get_float_for_environment"):
        try:
            return float(config.get_float_for_environment(key, environment, default))
        except (TypeError, ValueError):
            return float(default)
    return float(default)


def _config_int(config, key: str, environment: str, default: int) -> int:
    if config is not None and hasattr(config, "get_int_for_environment"):
        try:
            return int(config.get_int_for_environment(key, environment, default))
        except (TypeError, ValueError):
            return int(default)
    return int(default)


def _thresholds_from_config(config, environment: str) -> dict:
    return {
        "sample_stale_sec": _config_int(config, "ibkr_host_resource_monitor_stale_sec", environment, 30),
        "watchlist_cpu_5m_max_pct": _config_float(
            config,
            "ibkr_resource_governor_watchlist_cpu_5m_max_pct",
            environment,
            60.0,
        ),
        "watchlist_load5_max": _config_float(
            config,
            "ibkr_resource_governor_watchlist_load5_max",
            environment,
            2.8,
        ),
        "watchlist_mem_available_min_mb": _config_float(
            config,
            "ibkr_resource_governor_watchlist_mem_available_min_mb",
            environment,
            2048.0,
        ),
        "watchlist_mem_available_min_pct": _config_float(
            config,
            "ibkr_resource_governor_watchlist_mem_available_min_pct",
            environment,
            25.0,
        ),
        "watchlist_disk_free_min_gb": _config_float(
            config,
            "ibkr_resource_governor_watchlist_disk_free_min_gb",
            environment,
            15.0,
        ),
        "watchlist_disk_free_min_pct": _config_float(
            config,
            "ibkr_resource_governor_watchlist_disk_free_min_pct",
            environment,
            20.0,
        ),
        "watchlist_iowait_max_pct": _config_float(
            config,
            "ibkr_resource_governor_watchlist_iowait_max_pct",
            environment,
            8.0,
        ),
        "warning_cpu_5m_pct": _config_float(
            config,
            "ibkr_resource_governor_warning_cpu_5m_pct",
            environment,
            70.0,
        ),
        "warning_mem_available_mb": _config_float(
            config,
            "ibkr_resource_governor_warning_mem_available_mb",
            environment,
            1536.0,
        ),
        "warning_disk_free_pct": _config_float(
            config,
            "ibkr_resource_governor_warning_disk_free_pct",
            environment,
            15.0,
        ),
        "critical_cpu_5m_pct": _config_float(
            config,
            "ibkr_resource_governor_critical_cpu_5m_pct",
            environment,
            85.0,
        ),
        "critical_mem_available_mb": _config_float(
            config,
            "ibkr_resource_governor_critical_mem_available_mb",
            environment,
            1024.0,
        ),
        "critical_disk_free_pct": _config_float(
            config,
            "ibkr_resource_governor_critical_disk_free_pct",
            environment,
            10.0,
        ),
        "critical_iowait_pct": _config_float(
            config,
            "ibkr_resource_governor_critical_iowait_pct",
            environment,
            20.0,
        ),
    }


def _append_threshold_reason(
    reasons: list[dict],
    code: str,
    metric,
    threshold,
    message: str,
) -> None:
    reasons.append(
        {
            "code": code,
            "metric": metric,
            "threshold": threshold,
            "message": message,
        }
    )


def build_resource_governor_snapshot(
    host_snapshot: dict | None,
    config=None,
    environment: str = "live",
) -> dict:
    payload = host_snapshot if isinstance(host_snapshot, dict) else {}
    thresholds = _thresholds_from_config(config, environment)
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    loadavg = payload.get("loadavg") if isinstance(payload.get("loadavg"), dict) else {}
    memory = payload.get("memory") if isinstance(payload.get("memory"), dict) else {}
    disk = payload.get("disk") if isinstance(payload.get("disk"), dict) else {}

    cpu_5m_pct = _first_number(cpu.get("used_pct_5m"), cpu.get("used_pct_1m"), cpu.get("used_pct"))
    iowait_5m_pct = _first_number(cpu.get("iowait_pct_5m"), cpu.get("iowait_pct_1m"), cpu.get("iowait_pct"))
    load5 = _first_number(loadavg.get("5"))
    mem_available_bytes = _first_number(memory.get("available_bytes"))
    mem_total_bytes = _first_number(memory.get("total_bytes"))
    mem_available_mb = (mem_available_bytes / BYTES_PER_MB) if mem_available_bytes is not None else None
    mem_available_pct = _first_number(memory.get("available_pct"))
    if mem_available_pct is None and mem_total_bytes and mem_available_bytes is not None:
        mem_available_pct = (mem_available_bytes / mem_total_bytes) * 100.0
    disk_free_bytes = _first_number(disk.get("free_bytes"))
    disk_total_bytes = _first_number(disk.get("total_bytes"))
    disk_free_gb = (disk_free_bytes / BYTES_PER_GB) if disk_free_bytes is not None else None
    disk_free_pct = _first_number(disk.get("free_pct"))
    if disk_free_pct is None:
        used_pct = _first_number(disk.get("used_pct"))
        if used_pct is not None:
            disk_free_pct = max(0.0, 100.0 - used_pct)
        elif disk_total_bytes and disk_free_bytes is not None:
            disk_free_pct = (disk_free_bytes / disk_total_bytes) * 100.0

    sample_age_s = _first_number(payload.get("sample_age_s"))
    metrics = {
        "cpu_5m_pct": round(cpu_5m_pct, 2) if cpu_5m_pct is not None else None,
        "iowait_5m_pct": round(iowait_5m_pct, 2) if iowait_5m_pct is not None else None,
        "load5": round(load5, 2) if load5 is not None else None,
        "mem_available_mb": round(mem_available_mb, 1) if mem_available_mb is not None else None,
        "mem_available_pct": round(mem_available_pct, 2) if mem_available_pct is not None else None,
        "disk_free_gb": round(disk_free_gb, 2) if disk_free_gb is not None else None,
        "disk_free_pct": round(disk_free_pct, 2) if disk_free_pct is not None else None,
        "sample_age_s": round(sample_age_s, 1) if sample_age_s is not None else None,
    }

    critical_reasons: list[dict] = []
    warning_reasons: list[dict] = []
    watchlist_blockers: list[dict] = []

    if not payload or payload.get("ok") is False:
        _append_threshold_reason(
            critical_reasons,
            "host_resource_snapshot_unavailable",
            None,
            None,
            "host resource snapshot is unavailable",
        )
    elif sample_age_s is not None and sample_age_s > float(thresholds["sample_stale_sec"]):
        _append_threshold_reason(
            critical_reasons,
            "host_resource_snapshot_stale",
            round(sample_age_s, 1),
            thresholds["sample_stale_sec"],
            "host resource snapshot is stale",
        )

    if cpu_5m_pct is None:
        _append_threshold_reason(watchlist_blockers, "cpu_metric_unavailable", None, None, "CPU metric unavailable")
    elif cpu_5m_pct >= thresholds["critical_cpu_5m_pct"]:
        _append_threshold_reason(
            critical_reasons,
            "cpu_critical",
            round(cpu_5m_pct, 2),
            thresholds["critical_cpu_5m_pct"],
            "CPU 5m average is critical",
        )
    elif cpu_5m_pct >= thresholds["warning_cpu_5m_pct"]:
        _append_threshold_reason(
            warning_reasons,
            "cpu_warning",
            round(cpu_5m_pct, 2),
            thresholds["warning_cpu_5m_pct"],
            "CPU 5m average is high",
        )

    if iowait_5m_pct is None:
        _append_threshold_reason(
            watchlist_blockers,
            "iowait_metric_unavailable",
            None,
            None,
            "iowait metric unavailable",
        )
    elif iowait_5m_pct >= thresholds["critical_iowait_pct"]:
        _append_threshold_reason(
            critical_reasons,
            "iowait_critical",
            round(iowait_5m_pct, 2),
            thresholds["critical_iowait_pct"],
            "iowait is critical",
        )

    if mem_available_mb is None or mem_available_pct is None:
        _append_threshold_reason(
            watchlist_blockers,
            "memory_metric_unavailable",
            None,
            None,
            "memory metric unavailable",
        )
    elif mem_available_mb <= thresholds["critical_mem_available_mb"]:
        _append_threshold_reason(
            critical_reasons,
            "memory_critical",
            round(mem_available_mb, 1),
            thresholds["critical_mem_available_mb"],
            "available memory is critical",
        )
    elif mem_available_mb <= thresholds["warning_mem_available_mb"]:
        _append_threshold_reason(
            warning_reasons,
            "memory_warning",
            round(mem_available_mb, 1),
            thresholds["warning_mem_available_mb"],
            "available memory is low",
        )

    if disk_free_pct is None:
        _append_threshold_reason(
            watchlist_blockers,
            "disk_metric_unavailable",
            None,
            None,
            "disk metric unavailable",
        )
    elif disk_free_pct <= thresholds["critical_disk_free_pct"]:
        _append_threshold_reason(
            critical_reasons,
            "disk_critical",
            round(disk_free_pct, 2),
            thresholds["critical_disk_free_pct"],
            "disk free percent is critical",
        )
    elif disk_free_pct <= thresholds["warning_disk_free_pct"]:
        _append_threshold_reason(
            warning_reasons,
            "disk_warning",
            round(disk_free_pct, 2),
            thresholds["warning_disk_free_pct"],
            "disk free percent is low",
        )

    if cpu_5m_pct is not None and cpu_5m_pct >= thresholds["watchlist_cpu_5m_max_pct"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_cpu_over_limit",
            round(cpu_5m_pct, 2),
            thresholds["watchlist_cpu_5m_max_pct"],
            "CPU is above watchlist admission limit",
        )
    if load5 is None:
        _append_threshold_reason(watchlist_blockers, "load_metric_unavailable", None, None, "load metric unavailable")
    elif load5 >= thresholds["watchlist_load5_max"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_load5_over_limit",
            round(load5, 2),
            thresholds["watchlist_load5_max"],
            "load5 is above watchlist admission limit",
        )
    if mem_available_mb is not None and mem_available_mb <= thresholds["watchlist_mem_available_min_mb"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_memory_mb_below_limit",
            round(mem_available_mb, 1),
            thresholds["watchlist_mem_available_min_mb"],
            "available memory MB is below watchlist admission limit",
        )
    if mem_available_pct is not None and mem_available_pct <= thresholds["watchlist_mem_available_min_pct"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_memory_pct_below_limit",
            round(mem_available_pct, 2),
            thresholds["watchlist_mem_available_min_pct"],
            "available memory percent is below watchlist admission limit",
        )
    if disk_free_gb is None:
        _append_threshold_reason(watchlist_blockers, "disk_free_metric_unavailable", None, None, "disk free metric unavailable")
    elif disk_free_gb <= thresholds["watchlist_disk_free_min_gb"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_disk_gb_below_limit",
            round(disk_free_gb, 2),
            thresholds["watchlist_disk_free_min_gb"],
            "disk free GB is below watchlist admission limit",
        )
    if disk_free_pct is not None and disk_free_pct <= thresholds["watchlist_disk_free_min_pct"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_disk_pct_below_limit",
            round(disk_free_pct, 2),
            thresholds["watchlist_disk_free_min_pct"],
            "disk free percent is below watchlist admission limit",
        )
    if iowait_5m_pct is not None and iowait_5m_pct >= thresholds["watchlist_iowait_max_pct"]:
        _append_threshold_reason(
            watchlist_blockers,
            "watchlist_iowait_over_limit",
            round(iowait_5m_pct, 2),
            thresholds["watchlist_iowait_max_pct"],
            "iowait is above watchlist admission limit",
        )

    if critical_reasons:
        status = "critical"
        health = "unhealthy"
    elif warning_reasons:
        status = "warning"
        health = "degraded"
    else:
        status = "green"
        health = "ok"

    admit_watchlist = not critical_reasons and not watchlist_blockers and status == "green"
    return {
        "status": status,
        "health": health,
        "metrics": metrics,
        "thresholds": thresholds,
        "reasons": critical_reasons + warning_reasons,
        "admission": {
            "watchlist_idle_topup": {
                "admit": bool(admit_watchlist),
                "blockers": watchlist_blockers,
            },
            "non_priority": {
                "admit": not critical_reasons,
                "blockers": critical_reasons,
            },
        },
    }


class HostResourceMonitor:
    def __init__(self, *, disk_path: str | None = None, max_samples: int = 720, start_time: float | None = None):
        self.disk_path = disk_path
        self.start_time = float(start_time if start_time is not None else time.time())
        self._max_samples = max(10, int(max_samples or 720))
        self._cpu_previous: dict | None = None
        self._cpu_samples: deque[dict] = deque(maxlen=self._max_samples)
        self._snapshot: dict = self._unavailable_snapshot("not_sampled")
        self._lock = threading.RLock()

    def _unavailable_snapshot(self, reason: str) -> dict:
        now_ts = time.time()
        return {
            "ok": False,
            "reason": str(reason or "unavailable"),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "cpu_count": max(1, int(os.cpu_count() or 1)),
            "sampled_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            "sampled_at_ts": now_ts,
            "sample_age_s": 0.0,
            "cpu": {
                "used_pct": None,
                "used_pct_1m": None,
                "used_pct_5m": None,
                "idle_pct": None,
                "iowait_pct": None,
                "iowait_pct_1m": None,
                "iowait_pct_5m": None,
                "source": "unavailable",
            },
            "loadavg": {},
            "memory": collect_memory_snapshot(""),
            "disk": collect_disk_snapshot(self.disk_path),
            "process": collect_process_snapshot(self.start_time),
        }

    def sample_once(self) -> dict:
        now_ts = time.time()
        current_cpu = read_proc_cpu_times(sampled_at=now_ts)
        cpu_snapshot = build_cpu_usage_snapshot(self._cpu_previous, current_cpu)
        if current_cpu is not None:
            self._cpu_previous = current_cpu
        if cpu_snapshot.get("used_pct") is not None:
            self._cpu_samples.append(dict(cpu_snapshot))
        samples = list(self._cpu_samples)
        cpu_snapshot["used_pct_1m"] = _window_average(samples, "used_pct", 60.0, now_ts)
        cpu_snapshot["used_pct_5m"] = _window_average(samples, "used_pct", 300.0, now_ts)
        cpu_snapshot["iowait_pct_1m"] = _window_average(samples, "iowait_pct", 60.0, now_ts)
        cpu_snapshot["iowait_pct_5m"] = _window_average(samples, "iowait_pct", 300.0, now_ts)

        load_snapshot = collect_load_snapshot()
        ok = bool(current_cpu is not None)
        snapshot = {
            "ok": ok,
            "reason": "" if ok else "cpu_proc_stat_unavailable",
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "cpu_count": load_snapshot["cpu_count"],
            "sampled_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            "sampled_at_ts": now_ts,
            "sample_age_s": 0.0,
            "cpu": cpu_snapshot,
            "loadavg": load_snapshot["loadavg"],
            "memory": collect_memory_snapshot(),
            "disk": collect_disk_snapshot(self.disk_path),
            "process": collect_process_snapshot(self.start_time),
        }
        with self._lock:
            self._snapshot = snapshot
            return copy.deepcopy(snapshot)

    def snapshot(self) -> dict:
        with self._lock:
            payload = copy.deepcopy(self._snapshot)
        sampled_at_ts = _safe_number(payload.get("sampled_at_ts"))
        if sampled_at_ts is not None:
            payload["sample_age_s"] = round(max(0.0, time.time() - sampled_at_ts), 1)
        return payload

    def governor_snapshot(self, config=None, environment: str = "live") -> dict:
        return build_resource_governor_snapshot(self.snapshot(), config=config, environment=environment)

    def run(self, stop_event: threading.Event, interval_provider: Callable[[], float] | float = 5.0) -> None:
        while not stop_event.is_set():
            self.sample_once()
            if callable(interval_provider):
                try:
                    interval_s = float(interval_provider())
                except (TypeError, ValueError):
                    interval_s = 5.0
            else:
                interval_s = float(interval_provider or 5.0)
            stop_event.wait(max(1.0, interval_s))
