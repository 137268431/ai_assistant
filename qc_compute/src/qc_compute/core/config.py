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
        self._last_refresh = 0

    def refresh(self):
        if not self.pb_client:
            return
        try:
            records = self.pb_client.get_records("config", per_page=100)
            for r in records:
                key = r.get("key", "")
                value = r.get("value", "")
                if key and value:
                    self._cache[key] = value
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

    @property
    def write_mode(self) -> str:
        return self.get("qc_write_mode", "shadow")

    @property
    def compute_enabled(self) -> bool:
        return self.get_bool("qc_compute_enabled", True)

    @property
    def scan_schedule(self) -> str:
        return self.get("qc_scan_schedule", "7:00-10:00")
