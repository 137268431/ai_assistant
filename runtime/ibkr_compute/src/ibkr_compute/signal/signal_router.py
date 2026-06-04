"""
信号源路由
- TradingView webhook 和 ibkr_compute 自生成信号统一写入 ibkr_signals
- 通过 extra.source 区分来源并按配置过滤
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from ibkr_compute.core.time_utils import ET
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)



class SignalRouter:
    def __init__(self, pb_client, config, environment: str = "live", broker_mode: str | None = None):
        self.pb_client = pb_client
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker_mode = str(broker_mode or self.environment or "live").strip().lower() or "live"
        self._processed_ids = set()
        self._inflight_ids = set()
        self._last_poll: Optional[float] = None

    @property
    def signal_source(self) -> str:
        return self.config.get_for_environment("ibkr_signal_source", self.broker_mode, "both")

    @staticmethod
    def _execution_status_for_mode(extra: dict, broker_mode: str) -> str:
        execution_by_mode = extra.get("execution_by_mode")
        if not isinstance(execution_by_mode, dict):
            return ""
        broker_execution = execution_by_mode.get(broker_mode)
        if not isinstance(broker_execution, dict):
            return ""
        return str(broker_execution.get("status") or "").strip().lower()

    def _already_handled_for_broker(self, row: Dict, extra: dict) -> bool:
        status = self._execution_status_for_mode(extra, self.broker_mode)
        if status in {"awaiting_confirm", "confirm_pending"}:
            return True
        if status in {
            "submitted",
            "submitted_waiting_fill",
            "filled_repricing_protection",
            "filled_position",
            "rejected",
            "expired",
            "blocked",
            "duplicate_existing_broker_order",
            "validation_rejected",
            "submit_failed",
            "protection_incomplete",
            "protection_reprice_failed",
            "entry_missed_limit_cap",
            "ignored_no_broker_position",
            "stale_signal",
            "signal_clock_skew",
            "stale_signal/signal_clock_skew",
            "executed",
            "closed",
        }:
            return True
        top_level_status = str(row.get("status") or "").strip().lower()
        if self.broker_mode == self.environment and top_level_status and top_level_status != "pending":
            return True
        return False

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
            extra = row.get("extra", {})
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            if not isinstance(extra, dict):
                extra = {}
            if self._already_handled_for_broker(row, extra):
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
                "risk_r": extra.get("risk_r", 0),
                "exit_policy": extra.get("exit_policy", ""),
                "exit_policy_profile": extra.get("exit_policy_profile", ""),
                "exit_policy_type": extra.get("exit_policy_type", ""),
                "extra": extra,
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
            "broker_mode": self.broker_mode,
            "data_environment": self.environment,
            "processed_count": len(self._processed_ids),
            "inflight_count": len(self._inflight_ids),
            "last_poll": datetime.fromtimestamp(self._last_poll, timezone.utc).isoformat()
            if self._last_poll else None,
        }
