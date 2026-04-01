"""
配置管理 — 从 PB config 表读取配置
"""

from qc_compute.integrations.pb_client import PBClient


class Config:
    DEFAULTS = {
        "qc_write_mode": "shadow",
        "qc_compute_enabled": "true",
        "qc_bar_publish_enabled": "true",
        "daily_target_filter_on": "false",
        "qc_scan_schedule": "7:00-10:00",
        "qc_publish_batch_size": "10",
    }

    def __init__(self, pb_client: PBClient = None):
        self.pb_client = pb_client
        self._cache = dict(self.DEFAULTS)
        self._records_by_key = {}
        self._last_refresh = 0

    def refresh(self):
        if not self.pb_client:
            return
        try:
            records = self.pb_client.get_records("config", per_page=500, sort="-updated")
            self._cache = dict(self.DEFAULTS)
            self._records_by_key = {}
            applied_global_keys = set()
            for r in records:
                key = r.get("key", "")
                value = r.get("value", "")
                environment = str(r.get("environment", "") or "").strip().lower()
                if key:
                    self._records_by_key.setdefault(key, []).append(r)
                if key and value and environment in ("", "global") and key not in applied_global_keys:
                    self._cache[key] = value
                    applied_global_keys.add(key)
        except Exception as e:
            print(f"[Config] refresh error: {e}")

    def get(self, key: str, default: str = None) -> str:
        return self._cache.get(key, default or self.DEFAULTS.get(key, ""))

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, str(default).lower())
        return val.lower() in ("true", "1", "yes")

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def has_environment_override(self, key: str, environment: str) -> bool:
        runtime_environment = str(environment or "").strip().lower()
        if not key or not runtime_environment:
            return False
        return any(
            str(record.get("environment", "") or "").strip().lower() == runtime_environment
            for record in self._records_by_key.get(key, [])
        )

    def get_for_environment(self, key: str, environment: str, default: str = None) -> str:
        runtime_environment = str(environment or "").strip().lower()
        fallback = default if default is not None else self.DEFAULTS.get(key, "")
        records = self._records_by_key.get(key, [])
        best_value = None
        best_rank = -1

        for record in records:
            value = record.get("value", "")
            if value in (None, ""):
                continue
            record_environment = str(record.get("environment", "") or "").strip().lower()
            rank = 2 if record_environment == runtime_environment else (1 if record_environment in ("", "global") else -1)
            if rank > best_rank:
                best_rank = rank
                best_value = value

        return best_value if best_value is not None else fallback

    def get_bool_for_environment(self, key: str, environment: str, default: bool = False) -> bool:
        val = self.get_for_environment(key, environment, str(default).lower())
        return str(val).lower() in ("true", "1", "yes")

    @property
    def write_mode(self) -> str:
        return self.get("qc_write_mode", "shadow")

    @property
    def compute_enabled(self) -> bool:
        return self.get_bool("qc_compute_enabled", True)

    @property
    def scan_schedule(self) -> str:
        return self.get("qc_scan_schedule", "7:00-10:00")
