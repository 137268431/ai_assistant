"""
数据留存管理
- 每日清理超过 14 天的 K 线数据
- 定时执行
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

RETENTION_DAYS = 14
ET = timezone(timedelta(hours=-4))


class DataRetention:
    def __init__(self, pb_client, collection: str = "ibkr_bars"):
        self.pb_client = pb_client
        self.collection = collection
        self._last_cleanup: str = ""
        self._total_deleted = 0

    def cleanup(self) -> int:
        cutoff = datetime.now(ET) - timedelta(days=RETENTION_DAYS)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        cutoff_str = cutoff.strftime("%Y-%m-%d 00:00:00")

        logger.info("Cleaning bars older than %s (ms=%d)", cutoff_str, cutoff_ms)

        deleted = 0
        try:
            old_records = self.pb_client.get_all_records(
                self.collection,
                filter=f"bar_time_ms < {cutoff_ms}",
                max_pages=50,
            )

            for record in old_records:
                try:
                    record_id = record["id"]
                    url = f"{self.pb_client.base_url}/api/collections/{self.collection}/records/{record_id}"
                    resp = self.pb_client.session.delete(url, timeout=10)
                    resp.raise_for_status()
                    deleted += 1
                except Exception as e:
                    logger.debug("Delete failed for record %s: %s", record.get("id"), e)

            self._total_deleted += deleted
            self._last_cleanup = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
            logger.info("Cleanup complete: %d records deleted", deleted)

        except Exception as e:
            logger.error("Cleanup error: %s", e)

        return deleted

    def status(self) -> dict:
        return {
            "retention_days": RETENTION_DAYS,
            "last_cleanup": self._last_cleanup,
            "total_deleted": self._total_deleted,
            "collection": self.collection,
        }
