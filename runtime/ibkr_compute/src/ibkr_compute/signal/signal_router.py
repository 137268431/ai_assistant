"""
信号源路由
- TradingView webhook 和 ibkr_compute 自生成信号统一写入 ibkr_signals
- 通过 extra.source 区分来源并按配置过滤
"""

import json
import logging
import time
from datetime import datetime, timedelta
from ibkr_compute.core.time_utils import ET
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)



class SignalRouter:
    def __init__(self, pb_client, config, environment: str = "live"):
        self.pb_client = pb_client
        self.config = config
        self.environment = environment
        self._processed_ids = set()
        self._inflight_ids = set()
        self._last_poll: Optional[float] = None

    @property
    def signal_source(self) -> str:
        return self.config.get_for_environment("ibkr_signal_source", self.environment, "both")

    def fetch_pending_signals(self) -> List[Dict]:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        source_mode = self.signal_source
        rows = self.pb_client.get_records(
            "ibkr_signals",
            filter=f'status = "pending" && date = "{today}" && environment = "{self.environment}"',
            sort="-created",
            per_page=100,
        )

        signals: List[Dict] = []
        seen_signal_ids = set()
        for row in rows:
            signal_id = row.get("signal_id", row.get("id", ""))
            if (
                not signal_id
                or signal_id in seen_signal_ids
                or signal_id in self._processed_ids
                or signal_id in self._inflight_ids
            ):
                continue
            seen_signal_ids.add(signal_id)

            source = self._resolve_source(row)
            if source_mode == "tradingview" and source != "tradingview":
                continue
            if source_mode == "ibkr_compute" and source != "ibkr_compute":
                continue

            signals.append({
                "signal_id": signal_id,
                "symbol": str(row.get("symbol", "")).upper(),
                "direction": row.get("direction", ""),
                "entry": float(row.get("entry", 0) or 0),
                "stop_loss": float(row.get("stop_loss", 0) or 0),
                "take_profit": float(row.get("take_profit", 0) or 0),
                "shares": int(row.get("shares", 0) or 0),
                "rr": row.get("rr", ""),
                "source": source,
                "signal_time": row.get("us_time", row.get("created", "")),
                "raw": row,
            })

        self._last_poll = time.time()
        return signals

    def _resolve_source(self, row: Dict) -> str:
        extra = row.get("extra", {})
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            extra = {}

        source = str(extra.get("source", "") or "").strip().lower()
        if source in ("tv", "signal", "tradingview", "webhook_tv"):
            return "tradingview"
        if source in ("ibkr", "ibkr_compute", "ibkr_runtime"):
            return "ibkr_compute"
        return "unknown"

    def mark_processed(self, signal_id: str):
        text = str(signal_id or "").strip()
        if not text:
            return
        self._inflight_ids.discard(text)
        self._processed_ids.add(text)

    def claim_signal(self, signal_id: str) -> bool:
        text = str(signal_id or "").strip()
        if not text or text in self._processed_ids or text in self._inflight_ids:
            return False
        self._inflight_ids.add(text)
        return True

    def release_signal(self, signal_id: str):
        text = str(signal_id or "").strip()
        if text:
            self._inflight_ids.discard(text)

    def forget_processed(self, signal_ids):
        for signal_id in (signal_ids or []):
            text = str(signal_id or "").strip()
            if text:
                self._processed_ids.discard(text)
                self._inflight_ids.discard(text)

    def daily_reset(self):
        self._processed_ids.clear()
        self._inflight_ids.clear()
        logger.info("Signal router daily reset")

    def status(self) -> dict:
        return {
            "signal_source": self.signal_source,
            "environment": self.environment,
            "processed_count": len(self._processed_ids),
            "inflight_count": len(self._inflight_ids),
            "last_poll": datetime.fromtimestamp(self._last_poll, timezone.utc).isoformat()
            if self._last_poll else None,
        }
