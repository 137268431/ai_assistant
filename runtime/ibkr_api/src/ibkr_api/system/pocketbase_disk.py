from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POCKETBASE_DISK_WARN_USED_PCT = 85.0
POCKETBASE_DISK_CRITICAL_USED_PCT = 92.0
POCKETBASE_DISK_CACHE_TTL_SECONDS = 60.0
_POCKETBASE_DISK_CACHE: dict[str, Any] = {"expires_at": 0.0, "snapshot": {}}



def pocketbase_data_path() -> Path:
    candidates = (
        os.environ.get("PB_DATA_DIR"),
        os.environ.get("POCKETBASE_DATA_DIR"),
        str(Path(os.environ["PB_DB_PATH"]).expanduser().resolve().parent) if os.environ.get("PB_DB_PATH") else "",
        str(Path(os.environ["PB_SQLITE_PATH"]).expanduser().resolve().parent) if os.environ.get("PB_SQLITE_PATH") else "",
        "/opt/pocketbase/pb_data",
    )
    for raw in candidates:
        text = str(raw or "").strip()
        if text:
            return Path(text).expanduser()
    return Path("/opt/pocketbase/pb_data")



def directory_size_snapshot(path: Path) -> tuple[int, int, int]:
    total_size = 0
    file_count = 0
    dir_count = 0
    if not path.exists():
        return total_size, file_count, dir_count
    if path.is_file():
        try:
            return int(path.stat().st_size), 1, 0
        except OSError:
            return total_size, file_count, dir_count

    for root, dirs, files in os.walk(path):
        dir_count += len(dirs)
        for filename in files:
            file_count += 1
            file_path = Path(root) / filename
            try:
                total_size += int(file_path.stat().st_size)
            except OSError:
                continue
    return total_size, file_count, dir_count



def collect_pocketbase_disk_snapshot(force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _POCKETBASE_DISK_CACHE.get("snapshot")
    expires_at = float(_POCKETBASE_DISK_CACHE.get("expires_at") or 0)
    if not force_refresh and isinstance(cached, dict) and cached and expires_at > now:
        return dict(cached)

    data_path = pocketbase_data_path()
    root_path = data_path.parent
    top_entries: list[dict[str, Any]] = []
    top_entry_sizes: dict[str, int] = {}
    data_exists = data_path.exists()
    scan_error = ""
    total_size = 0
    file_count = 0
    dir_count = 1 if data_exists and data_path.is_dir() else 0

    try:
        if data_exists and data_path.is_dir():
            for child in sorted(data_path.iterdir(), key=lambda item: item.name):
                child_size, child_files, child_dirs = directory_size_snapshot(child)
                total_size += child_size
                file_count += child_files
                dir_count += child_dirs
                top_entry_sizes[child.name] = child_size
                top_entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_dir": child.is_dir(),
                        "size_bytes": child_size,
                    }
                )
        elif data_exists and data_path.is_file():
            size_bytes = int(data_path.stat().st_size)
            total_size = size_bytes
            file_count = 1
            top_entry_sizes[data_path.name] = size_bytes
            top_entries.append(
                {
                    "name": data_path.name,
                    "path": str(data_path),
                    "is_dir": False,
                    "size_bytes": size_bytes,
                }
            )
    except OSError as exc:
        scan_error = str(exc)

    top_entries.sort(key=lambda item: (-int(item.get("size_bytes") or 0), str(item.get("name") or "")))

    filesystem = {
        "path": str(data_path),
        "mount_path": str(data_path),
        "device": "",
        "total_bytes": 0,
        "used_bytes": 0,
        "available_bytes": 0,
        "used_pct": None,
        "source": "python_shutil.disk_usage",
        "available": False,
        "error": "",
    }
    try:
        usage = shutil.disk_usage(data_path if data_exists else root_path)
        total_bytes = int(usage.total)
        used_bytes = int(usage.used)
        filesystem.update(
            {
                "total_bytes": total_bytes,
                "used_bytes": used_bytes,
                "available_bytes": int(usage.free),
                "used_pct": round((used_bytes / total_bytes) * 100, 2) if total_bytes > 0 else None,
                "available": True,
            }
        )
    except OSError as exc:
        filesystem["error"] = str(exc)

    db_backup_entries = [item for item in top_entries if str(item.get("name") or "").startswith("data.db.backup.")]
    db_backup_size_bytes = sum(int(item.get("size_bytes") or 0) for item in db_backup_entries)
    snapshot = {
        "source": "ibkr_api_disk_monitor",
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cache_ttl_s": int(POCKETBASE_DISK_CACHE_TTL_SECONDS),
        "status": "ok",
        "root_path": str(root_path),
        "data_path": str(data_path),
        "data_exists": data_exists,
        "data_size_bytes": int(total_size),
        "file_count": int(file_count),
        "dir_count": int(dir_count),
        "scan_error": scan_error,
        "db_path": str(data_path / "data.db"),
        "db_size_bytes": int(top_entry_sizes.get("data.db") or 0),
        "storage_path": str(data_path / "storage"),
        "storage_size_bytes": int(top_entry_sizes.get("storage") or 0),
        "backups_path": str(data_path / "backups"),
        "backups_size_bytes": int(top_entry_sizes.get("backups") or 0),
        "db_backup_glob": str(data_path / "data.db.backup.*"),
        "db_backup_size_bytes": int(db_backup_size_bytes),
        "db_backup_file_count": len(db_backup_entries),
        "aux_path": str(data_path / "aux"),
        "aux_size_bytes": int(top_entry_sizes.get("aux") or 0),
        "filesystem": filesystem,
        "top_entries": top_entries[:6],
    }

    used_pct = filesystem.get("used_pct")
    if not snapshot["data_exists"] and not bool(filesystem.get("available")):
        snapshot["status"] = "unavailable"
    elif isinstance(used_pct, (int, float)) and float(used_pct) >= POCKETBASE_DISK_CRITICAL_USED_PCT:
        snapshot["status"] = "error"
    elif isinstance(used_pct, (int, float)) and float(used_pct) >= POCKETBASE_DISK_WARN_USED_PCT:
        snapshot["status"] = "warning"
    elif snapshot["scan_error"]:
        snapshot["status"] = "partial"

    _POCKETBASE_DISK_CACHE["expires_at"] = now + POCKETBASE_DISK_CACHE_TTL_SECONDS
    _POCKETBASE_DISK_CACHE["snapshot"] = dict(snapshot)
    return snapshot



def format_bytes(num_bytes: Any) -> str:
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "--"
    if value < 0:
        return "--"
    if value == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    unit_idx = 0
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1
    precision = 0 if value >= 100 else (1 if value >= 10 else 2)
    return f"{value:.{precision}f} {units[unit_idx]}"



def build_pocketbase_disk_flags(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    filesystem = snapshot.get("filesystem") if isinstance(snapshot.get("filesystem"), dict) else {}
    used_pct = filesystem.get("used_pct")
    if not isinstance(used_pct, (int, float)):
        return []
    mount_path = str(filesystem.get("mount_path") or filesystem.get("path") or snapshot.get("data_path") or "--")
    detail = (
        f"{mount_path} 已使用 {float(used_pct):.1f}%，剩余 {format_bytes(filesystem.get('available_bytes'))}，"
        f"pb_data {format_bytes(snapshot.get('data_size_bytes'))}，"
        f"db backups {int(snapshot.get('db_backup_file_count') or 0)} 个 / {format_bytes(snapshot.get('db_backup_size_bytes'))}"
    )
    if float(used_pct) >= POCKETBASE_DISK_CRITICAL_USED_PCT:
        return [{"severity": "error", "code": "pb_disk_critical", "title": "PocketBase disk critical", "detail": detail}]
    if float(used_pct) >= POCKETBASE_DISK_WARN_USED_PCT:
        return [{"severity": "warning", "code": "pb_disk_high", "title": "PocketBase disk high", "detail": detail}]
    return []



def merge_monitor_flags(base_flags: Any, extra_flags: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (base_flags, extra_flags):
        for item in (group if isinstance(group, list) else []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("code") or item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged



def merge_monitor_status(current_status: Any, flags: list[dict[str, Any]]) -> str:
    normalized = str(current_status or "ok").strip().lower() or "ok"
    if normalized in {"offline", "error"}:
        return normalized
    severities = {str((item or {}).get("severity") or "").strip().lower() for item in flags}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning" if normalized == "ok" else normalized
    return normalized



def enrich_monitor_payload_with_pocketbase_disk(payload: dict[str, Any], *, force_refresh: bool = False) -> dict[str, Any]:
    response = dict(payload or {})
    pocketbase_payload = response.get("pocketbase") if isinstance(response.get("pocketbase"), dict) else {}
    disk_snapshot = collect_pocketbase_disk_snapshot(force_refresh=force_refresh)
    flags = merge_monitor_flags(response.get("flags"), build_pocketbase_disk_flags(disk_snapshot))
    pocketbase_payload["disk"] = disk_snapshot
    response["pocketbase"] = pocketbase_payload
    response["flags"] = flags
    response["status"] = merge_monitor_status(response.get("status"), flags)
    if response["status"] in {"offline", "error"}:
        response["ok"] = False
    elif response.get("ok") is None:
        response["ok"] = True
    return response
