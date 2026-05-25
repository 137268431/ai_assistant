from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any

from .aggregator import OrderFlowAggregator
from .candidate_queue import CandidateQueueManager, OrderFlowCandidate
from .execution_pool import ExecutionPoolManager
from .models import OrderFlowBar, OrderFlowTick, normalize_symbol

logger = logging.getLogger(__name__)


class OrderFlowManager:
    """Non-invasive order-flow shadow coordinator for realtime experiments.

    Config is scoped by broker environment (paper/live), while market data may
    still come from the shared live data environment used by paper trading.
    """

    def __init__(
        self,
        *,
        config=None,
        environment: str = "live",
        data_environment: str | None = None,
        ws_client=None,
    ):
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker_environment = self.environment
        self.data_environment = str(data_environment or self.environment or "live").strip().lower() or "live"
        self.ws_client = ws_client
        self.aggregator = OrderFlowAggregator()
        self.candidates = CandidateQueueManager(
            default_ttl_ms=self._config_int("candidate_pullback_ttl_sec", 300) * 1000,
            max_candidates=self._config_int("candidate_queue_max", 10),
            ttl_by_setup={
                "breakout": self._config_int("candidate_breakout_ttl_sec", 120),
                "pullback": self._config_int("candidate_pullback_ttl_sec", 300),
                "reversal": self._config_int("candidate_reversal_ttl_sec", 600),
                "mean_reversion": self._config_int("candidate_reversal_ttl_sec", 600),
            },
        )
        pool_size = self._config_int("ibkr_order_flow_execution_pool_size", self._config_int("ibkr_order_flow_active_limit", 3))
        self.execution_pool = ExecutionPoolManager(
            max_symbols=pool_size,
            max_position_slots=self._config_int("ibkr_order_flow_max_position_slots", 1),
            reservation_ttl_ms=self._config_int("candidate_pullback_ttl_sec", 300) * 1000,
            fill_watch_ttl_ms=self._config_int("entry_watch_after_fill_sec", 180) * 1000,
            entry_watch_after_fill_sec=self._config_int("entry_watch_after_fill_sec", 180),
        )
        self._lock = threading.RLock()
        self._decision_log: list[dict[str, Any]] = []
        self._recent_closed_bars: deque[OrderFlowBar] = deque(maxlen=600)
        self._conid_by_symbol: dict[str, int] = {}
        self._tick_count = 0
        self._subscribe_errors = 0
        self._last_tick_ms = 0
        self._last_error = ""

    def enabled(self) -> bool:
        return self._config_bool("ibkr_order_flow_enabled", True)

    def mode(self) -> str:
        return self._config_text("ibkr_order_flow_mode", "shadow").strip().lower() or "shadow"

    def on_market_tick(self, payload: dict[str, Any]) -> None:
        if not self.enabled():
            return
        tick = self._tick_from_payload(payload)
        if tick is None:
            return
        try:
            closed = self.aggregator.update(tick)
        except Exception as exc:
            self._last_error = str(exc)
            logger.debug("Order-flow aggregation failed: %s", exc, exc_info=True)
            return
        with self._lock:
            self._tick_count += 1
            self._last_tick_ms = max(self._last_tick_ms, tick.timestamp_ms)
            self._recent_closed_bars.extend(closed)

    def observe_signal(self, signal: dict[str, Any], *, conid: int) -> dict[str, Any]:
        if not self.enabled():
            return {"enabled": False, "mode": self.mode()}
        current_ms = int(time.time() * 1000)
        candidate = self._candidate_from_signal(signal, current_ms)
        update = self.candidates.upsert(candidate, now_ms=current_ms)
        if not update.accepted or update.candidate is None:
            decision = {
                "enabled": True,
                "mode": self.mode(),
                "action": "candidate_rejected",
                "reason": update.reason or update.action,
                "candidate": self._candidate_dict(candidate),
                "enforced": False,
            }
            self._record_decision(decision)
            return decision

        candidate = update.candidate
        normalized_symbol = candidate.symbol
        self._conid_by_symbol[normalized_symbol] = int(conid or 0)
        if update.action == "replaced_conflict":
            releaser = getattr(self.execution_pool, "release_candidate_watch", None)
            if callable(releaser):
                releaser(normalized_symbol)
        allocated, watch, allocation_reason = self.execution_pool.allocate_candidate(
            candidate,
            conid=int(conid or 0),
            at_ms=current_ms,
        )
        if allocated and int(conid or 0) > 0:
            self._subscribe_tick(int(conid))
        confirmation = self.confirmation(normalized_symbol, candidate.side)
        decision = {
            "enabled": True,
            "mode": self.mode(),
            "action": "observe",
            "candidate_action": update.action,
            "allocation_ok": bool(allocated),
            "allocation_reason": allocation_reason,
            "candidate": self._candidate_dict(candidate),
            "slot": self._watch_dict(watch),
            "confirmation": confirmation,
            "enforced": False,
        }
        self._record_decision(decision)
        return decision

    def confirmation(self, symbol: str, direction: str) -> dict[str, Any]:
        normalized_symbol = normalize_symbol(symbol)
        interval_sec = self._config_int("ibkr_order_flow_confirm_window_sec", 60)
        min_delta_ratio = self._config_float("ibkr_order_flow_min_delta_ratio", 0.12)
        bars = [
            bar for bar in self.aggregator.snapshot(normalized_symbol)
            if bar.interval_seconds == interval_sec
        ]
        if not bars:
            return {"ok": False, "reason": "order_flow_missing", "symbol": normalized_symbol}
        latest = bars[-1]
        known = float(latest.buy_volume + latest.sell_volume)
        ratio = (latest.delta / known) if known > 0 else 0.0
        side = str(direction or "").strip().lower()
        ok = ratio >= min_delta_ratio if side == "long" else ratio <= -min_delta_ratio
        return {
            "ok": bool(ok),
            "reason": "ok" if ok else "delta_ratio_not_confirmed",
            "symbol": normalized_symbol,
            "interval_sec": latest.interval_seconds,
            "delta": latest.delta,
            "cvd": latest.cvd_close,
            "delta_ratio": round(ratio, 6),
            "min_delta_ratio": min_delta_ratio,
            "tick_count": latest.tick_count,
        }

    def mark_filled(self, symbol: str, *, conid: int, direction: str = "", signal_id: str = "") -> None:
        if not self.enabled():
            return
        current_ms = int(time.time() * 1000)
        normalized_symbol = normalize_symbol(symbol)
        if int(conid or 0) > 0:
            self._conid_by_symbol[normalized_symbol] = int(conid)
        watch = self.execution_pool.mark_filled_watch(
            normalized_symbol,
            conid=int(conid or 0),
            at_ms=current_ms,
        )
        if int(conid or 0) > 0:
            self._subscribe_tick(int(conid))
        self._record_decision({"action": "filled_watch", "accepted": watch is not None, "slot": self._watch_dict(watch)})

    def release_symbol(self, symbol: str, *, reason: str = "released") -> None:
        normalized_symbol = normalize_symbol(symbol)
        current_ms = int(time.time() * 1000)
        releaser = getattr(self.execution_pool, "release_candidate_watch", None)
        released = releaser(normalized_symbol) if callable(releaser) else None
        conid = self._conid_by_symbol.pop(normalized_symbol, 0)
        if conid:
            self._unsubscribe_tick(conid)
        self._record_decision({"action": "release_symbol", "symbol": normalized_symbol, "accepted": released is not None, "reason": reason})

    def status(self) -> dict[str, Any]:
        with self._lock:
            recent_bars = [bar.to_dict() for bar in list(self._recent_closed_bars)[-25:]]
            decisions = list(self._decision_log[-25:])
            tick_count = int(self._tick_count)
            last_tick_ms = int(self._last_tick_ms)
        return {
            "enabled": self.enabled(),
            "mode": self.mode(),
            "environment": self.broker_environment,
            "broker_environment": self.broker_environment,
            "data_environment": self.data_environment,
            "tick_count": tick_count,
            "last_tick_ms": last_tick_ms,
            "current_cvd": {symbol: self.aggregator.current_cvd(symbol) for symbol in self.execution_pool.active_symbols()},
            "candidate_queue": {
                "active_count": len(self.candidates),
                "candidates": [self._candidate_dict(candidate) for candidate in self.candidates.ranked(limit=10)],
            },
            "execution_pool": {
                **self.execution_pool.status(),
                "active_conids": sorted({self._conid_by_symbol.get(symbol, 0) for symbol in self.execution_pool.active_symbols() if self._conid_by_symbol.get(symbol, 0)}),
            },
            "recent_closed_bars": recent_bars,
            "subscribe_errors": int(self._subscribe_errors),
            "last_error": self._last_error,
            "recent_decisions": decisions,
        }

    def _candidate_from_signal(self, signal: dict[str, Any], current_ms: int) -> OrderFlowCandidate:
        extra = signal.get("extra") if isinstance(signal.get("extra"), dict) else {}
        raw = signal.get("raw") if isinstance(signal.get("raw"), dict) else {}
        raw_extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        payload = {**(raw_extra or {}), **(extra or {})}
        setup = str(payload.get("setup_id") or payload.get("setup") or signal.get("setup") or "unknown")
        score = self._safe_float(payload.get("quality_score") or signal.get("quality_score") or signal.get("score"), 0.0)
        priority = int(self._safe_float(payload.get("priority") or self._priority_for_setup(setup), 50.0))
        return OrderFlowCandidate(
            symbol=str(signal.get("symbol") or ""),
            side=str(signal.get("direction") or ""),
            score=score,
            priority=priority,
            candidate_id=str(signal.get("signal_id") or signal.get("id") or ""),
            strategy=str(payload.get("strategy") or setup or "order_flow_v1"),
            created_at_ms=current_ms,
            ttl_ms=self._ttl_ms_for_setup(setup),
            reason="signal_observed",
            payload=payload,
        )

    def _tick_from_payload(self, payload: dict[str, Any]) -> OrderFlowTick | None:
        payload = payload if isinstance(payload, dict) else {}
        symbol = normalize_symbol(str(payload.get("symbol") or ""))
        if not symbol:
            return None
        price = self._first_float(payload, ("price", "last_price", "last", "31"))
        size = self._first_float(payload, ("size", "last_size", "lastSize", "7059"))
        if price <= 0 or size <= 0:
            return None
        timestamp_ms = int(self._first_float(payload, ("timestamp_ms", "time_ms", "_updated")))
        if timestamp_ms <= 0:
            raw_time = int(self._first_float(payload, ("time",)))
            timestamp_ms = raw_time * 1000 if 0 < raw_time < 10_000_000_000 else int(time.time() * 1000)
        return OrderFlowTick(
            symbol=symbol,
            timestamp_ms=timestamp_ms,
            price=price,
            size=size,
            side=str(payload.get("side") or ""),
            bid=self._first_optional_float(payload, ("bid", "bid_price", "84")),
            ask=self._first_optional_float(payload, ("ask", "ask_price", "86")),
            source=str(payload.get("source") or payload.get("tick_source") or ""),
            metadata={"conid": payload.get("conid") or payload.get("conidEx")},
        )

    def _subscribe_tick(self, conid: int) -> None:
        subscriber = getattr(self.ws_client, "subscribe_tick_by_tick", None)
        if callable(subscriber) and int(conid or 0) > 0:
            try:
                subscriber(int(conid), tick_type=self._config_text("ibkr_order_flow_tick_types", "Last").split(",")[0] or "Last")
            except Exception as exc:
                self._subscribe_errors += 1
                self._last_error = str(exc)
                logger.warning("Order-flow tick subscription failed conid=%s: %s", conid, exc)

    def _unsubscribe_tick(self, conid: int) -> None:
        unsubscriber = getattr(self.ws_client, "unsubscribe_tick_by_tick", None)
        if callable(unsubscriber) and int(conid or 0) > 0:
            try:
                unsubscriber(int(conid))
            except Exception as exc:
                self._last_error = str(exc)
                logger.debug("Order-flow tick unsubscribe failed conid=%s: %s", conid, exc)

    def _record_decision(self, decision: dict[str, Any]) -> None:
        with self._lock:
            self._decision_log.append({"ts_ms": int(time.time() * 1000), **dict(decision or {})})
            self._decision_log = self._decision_log[-200:]

    def _ttl_ms_for_setup(self, setup: str) -> int:
        normalized = str(setup or "").strip().lower()
        if "breakout" in normalized or "squeeze" in normalized:
            return self._config_int("candidate_breakout_ttl_sec", 120) * 1000
        if "pullback" in normalized or "vwap" in normalized or "trend" in normalized:
            return self._config_int("candidate_pullback_ttl_sec", 300) * 1000
        return self._config_int("candidate_reversal_ttl_sec", 600) * 1000

    @staticmethod
    def _priority_for_setup(setup: str) -> float:
        normalized = str(setup or "").strip().lower()
        if "squeeze" in normalized or "breakout" in normalized:
            return 100.0
        if "vwap" in normalized or "pullback" in normalized:
            return 80.0
        if "trend" in normalized:
            return 60.0
        if "mean" in normalized or "reversion" in normalized or "mr" in normalized:
            return 50.0
        return 40.0

    @staticmethod
    def _candidate_dict(candidate: OrderFlowCandidate | None) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {
            "symbol": candidate.symbol,
            "direction": candidate.side,
            "score": candidate.score,
            "priority": candidate.priority,
            "candidate_id": candidate.candidate_id,
            "strategy": candidate.strategy,
            "created_at_ms": candidate.created_at_ms,
            "updated_at_ms": candidate.updated_at_ms,
            "expires_at_ms": candidate.expires_at_ms,
            "reason": candidate.reason,
            "payload": dict(candidate.payload),
        }

    @staticmethod
    def _slot_dict(slot) -> dict[str, Any] | None:
        if slot is None:
            return None
        return {
            "reservation_id": slot.reservation_id,
            "symbol": slot.symbol,
            "direction": slot.side,
            "state": slot.state,
            "candidate_id": slot.candidate_id,
            "order_id": slot.order_id,
            "created_at_ms": slot.created_at_ms,
            "updated_at_ms": slot.updated_at_ms,
            "expires_at_ms": slot.expires_at_ms,
            "metadata": dict(slot.metadata),
        }

    @staticmethod
    def _watch_dict(watch) -> dict[str, Any] | None:
        if watch is None:
            return None
        return {
            "symbol": watch.symbol,
            "direction": watch.direction,
            "conid": watch.conid,
            "candidate_key": watch.candidate_key,
            "state": watch.state,
            "allocated_at_ms": watch.allocated_at_ms,
            "filled_at_ms": watch.filled_at_ms,
            "release_at_ms": watch.release_at_ms,
        }

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except Exception:
            return default
        return number if math.isfinite(number) else default

    @classmethod
    def _first_float(cls, payload: dict[str, Any], keys: tuple[str, ...]) -> float:
        for key in keys:
            number = cls._safe_float(payload.get(key), 0.0)
            if number > 0:
                return number
        return 0.0

    @classmethod
    def _first_optional_float(cls, payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        value = cls._first_float(payload, keys)
        return value if value > 0 else None

    def _config_text(self, key: str, default: str) -> str:
        getter = getattr(self.config, "get_for_environment", None)
        if callable(getter):
            try:
                return str(getter(key, self.broker_environment, default))
            except Exception:
                return str(default)
        return str(default)

    def _config_bool(self, key: str, default: bool) -> bool:
        getter = getattr(self.config, "get_bool_for_environment", None)
        if callable(getter):
            try:
                return bool(getter(key, self.broker_environment, default))
            except Exception:
                return bool(default)
        return bool(default)

    def _config_int(self, key: str, default: int) -> int:
        getter = getattr(self.config, "get_int_for_environment", None)
        if callable(getter):
            try:
                return int(getter(key, self.broker_environment, default))
            except Exception:
                return int(default)
        return int(default)

    def _config_float(self, key: str, default: float) -> float:
        getter = getattr(self.config, "get_float_for_environment", None)
        if callable(getter):
            try:
                return float(getter(key, self.broker_environment, default))
            except Exception:
                return float(default)
        return float(default)
