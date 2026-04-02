"""
信号源路由
- TradingView webhook → PB signals 表
- qc_compute 指标引擎自主生成
- 支持双源模式切换 (通过 PB config signal_source 字段)
"""

import time
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))
POLL_INTERVAL = 120  # 2 minutes, same as original


class SignalRouter:
    def __init__(self, pb_client, config, environment: str = "live"):
        self.pb_client = pb_client
        self.config = config
        self.environment = environment
        self._processed_ids = set()
        self._last_poll: Optional[float] = None

    @property
    def signal_source(self) -> str:
        return self.config.get_for_environment("signal_source", self.environment, "tradingview")

    def fetch_pending_signals(self) -> List[Dict]:
        source = self.signal_source
        signals = []

        if source in ("tradingview", "both"):
            tv_signals = self._fetch_tv_signals()
            signals.extend(tv_signals)

        if source in ("qc_compute", "both"):
            qc_signals = self._fetch_qc_signals()
            signals.extend(qc_signals)

        self._last_poll = time.time()
        return signals

    def _fetch_tv_signals(self) -> List[Dict]:
        try:
            et_now = datetime.now(ET)
            today = et_now.strftime("%Y-%m-%d")

            records = self.pb_client.get_records(
                "signals",
                filter=f'status = "confirmed" && date = "{today}"',
                sort="-created",
                per_page=50,
            )

            signals = []
            for r in records:
                signal_id = r.get("signal_id", r.get("id", ""))
                if signal_id in self._processed_ids:
                    continue

                signals.append({
                    "signal_id": signal_id,
                    "symbol": r.get("symbol", "").upper(),
                    "direction": r.get("direction", ""),
                    "entry": float(r.get("entry", 0)),
                    "stop_loss": float(r.get("stop_loss", 0)),
                    "take_profit": float(r.get("take_profit", 0)),
                    "shares": int(r.get("shares", 0)),
                    "rr": r.get("rr", ""),
                    "source": "tradingview",
                    "signal_time": r.get("us_time", r.get("created", "")),
                    "raw": r,
                })

            return signals

        except Exception as e:
            logger.error("Failed to fetch TV signals: %s", e)
            return []

    def _fetch_qc_signals(self) -> List[Dict]:
        try:
            et_now = datetime.now(ET)
            today = et_now.strftime("%Y-%m-%d")

            records = self.pb_client.get_records(
                "qc_signals",
                filter=f'status = "pending" && date = "{today}" && environment = "{self.environment}"',
                sort="-created",
                per_page=50,
            )

            signals = []
            for r in records:
                signal_id = r.get("signal_id", r.get("id", ""))
                if signal_id in self._processed_ids:
                    continue

                signals.append({
                    "signal_id": signal_id,
                    "symbol": r.get("symbol", "").upper(),
                    "direction": r.get("direction", ""),
                    "entry": float(r.get("entry", 0)),
                    "stop_loss": float(r.get("stop_loss", 0)),
                    "take_profit": float(r.get("take_profit", 0)),
                    "shares": int(r.get("shares", 0)),
                    "rr": r.get("rr", ""),
                    "source": "qc_compute",
                    "signal_time": r.get("us_time", r.get("created", "")),
                    "raw": r,
                })

            return signals

        except Exception as e:
            logger.error("Failed to fetch QC signals: %s", e)
            return []

    def mark_processed(self, signal_id: str):
        self._processed_ids.add(signal_id)

    def daily_reset(self):
        self._processed_ids.clear()
        logger.info("Signal router daily reset")

    def status(self) -> dict:
        return {
            "signal_source": self.signal_source,
            "environment": self.environment,
            "processed_count": len(self._processed_ids),
            "last_poll": datetime.fromtimestamp(self._last_poll).isoformat()
            if self._last_poll else None,
        }
