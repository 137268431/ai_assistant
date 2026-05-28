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
        quote_provider=None,
    ):
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker_environment = self.environment
        self.data_environment = str(data_environment or self.environment or "live").strip().lower() or "live"
        self.ws_client = ws_client
        self.quote_provider = quote_provider
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
        self._entry_wait_started_ms: dict[str, int] = {}
        self._open_position_symbols: set[str] = set()
        self._tick_count = 0
        self._ignored_non_tbt_tick_count = 0
        self._subscribe_errors = 0
        self._last_tick_ms = 0
        self._last_tbt_tick_ms_by_symbol: dict[str, int] = {}
        self._tbt_tick_count_by_symbol: dict[str, int] = {}
        self._last_ignored_tick_ms = 0
        self._last_ignored_tick_source = ""
        self._last_error = ""

    def enabled(self) -> bool:
        return self._config_bool("ibkr_order_flow_enabled", True)

    def mode(self) -> str:
        return self._config_text("ibkr_order_flow_mode", "enforce").strip().lower() or "enforce"

    def enforce_mode(self) -> bool:
        return self.mode() in {"confirm", "enforce"}

    def on_market_tick(self, payload: dict[str, Any]) -> None:
        if not self.enabled():
            return
        if not self._is_tick_by_tick_payload(payload):
            self._record_ignored_non_tbt_tick(payload)
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
            self._last_tbt_tick_ms_by_symbol[tick.symbol] = max(
                int(self._last_tbt_tick_ms_by_symbol.get(tick.symbol, 0) or 0),
                tick.timestamp_ms,
            )
            self._tbt_tick_count_by_symbol[tick.symbol] = int(self._tbt_tick_count_by_symbol.get(tick.symbol, 0) or 0) + 1
            self._recent_closed_bars.extend(closed)

    def observe_signal(self, signal: dict[str, Any], *, conid: int) -> dict[str, Any]:
        if not self.enabled():
            return {"enabled": False, "mode": self.mode()}
        current_ms = int(time.time() * 1000)
        self.execution_pool.release_expired_watches(current_ms)
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

    def entry_decision(self, signal: dict[str, Any], *, conid: int, quote: dict[str, Any] | None = None) -> dict[str, Any]:
        observed = self.observe_signal(signal, conid=conid)
        if not self.enabled():
            return {**observed, "action": "allow", "reason": "order_flow_disabled", "enforced": False}
        if not self._config_bool("ibkr_order_flow_auto_entry_enabled", True):
            return {**observed, "action": "allow", "reason": "auto_entry_disabled", "enforced": False}
        mode = self.mode()
        if mode not in {"confirm", "enforce"}:
            return {**observed, "action": "allow", "reason": "shadow_mode", "enforced": False}

        signal_id = str(signal.get("signal_id") or signal.get("id") or "").strip()
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        direction = str(signal.get("direction") or "").strip().lower()
        current_ms = int(time.time() * 1000)
        wait_key = signal_id or f"{symbol}:{direction}"
        started_ms = self._entry_wait_started_ms.setdefault(wait_key, current_ms)
        timeout_ms = max(1, self._config_int("ibkr_order_flow_entry_timeout_sec", 60)) * 1000
        elapsed_ms = max(0, current_ms - started_ms)
        timeout = elapsed_ms >= timeout_ms

        if not observed.get("allocation_ok"):
            reason = str(observed.get("allocation_reason") or "execution_pool_unavailable")
            decision = {
                **observed,
                "action": "reject" if timeout else "wait",
                "reason": "order_flow_timeout" if timeout else reason,
                "wait_elapsed_ms": elapsed_ms,
                "wait_timeout_ms": timeout_ms,
                "enforced": True,
            }
            if timeout:
                self._release_entry_wait(symbol, wait_key, reason=decision["reason"])
            self._record_decision(decision)
            return decision

        quote = self._quote_for_symbol(symbol, quote)
        spread = self._spread_snapshot(quote)
        if not spread.get("ok"):
            decision = {
                **observed,
                "action": "reject" if timeout else "wait",
                "reason": "order_flow_timeout" if timeout else str(spread.get("reason") or "quote_unavailable"),
                "spread": spread,
                "wait_elapsed_ms": elapsed_ms,
                "wait_timeout_ms": timeout_ms,
                "enforced": True,
            }
            if timeout:
                self._release_entry_wait(symbol, wait_key, reason=decision["reason"])
            self._record_decision(decision)
            return decision

        confirmation = dict(observed.get("confirmation") or self.confirmation(symbol, direction))
        if confirmation.get("ok"):
            marketable = self._marketable_limit_price(direction, quote)
            if marketable.get("ok"):
                self._entry_wait_started_ms.pop(wait_key, None)
                decision = {
                    **observed,
                    "action": "allow",
                    "reason": "order_flow_confirmed",
                    "confirmation": confirmation,
                    "spread": spread,
                    "marketable_limit": marketable,
                    "enforced": True,
                }
                self._record_decision(decision)
                return decision

        decision = {
            **observed,
            "action": "reject" if timeout else "wait",
            "reason": "order_flow_timeout" if timeout else str(confirmation.get("reason") or "delta_ratio_not_confirmed"),
            "confirmation": confirmation,
            "spread": spread,
            "wait_elapsed_ms": elapsed_ms,
            "wait_timeout_ms": timeout_ms,
            "enforced": True,
        }
        if timeout:
            self._release_entry_wait(symbol, wait_key, reason=decision["reason"])
        self._record_decision(decision)
        return decision

    def confirmation(self, symbol: str, direction: str) -> dict[str, Any]:
        normalized_symbol = normalize_symbol(symbol)
        interval_sec = self._config_int("ibkr_order_flow_confirm_window_sec", 60)
        min_delta_ratio = self._config_float("ibkr_order_flow_min_delta_ratio", 0.12)
        tbt_status = self._tbt_status(normalized_symbol)
        if not tbt_status.get("has_recent_tbt"):
            return {
                "ok": False,
                "reason": str(tbt_status.get("reason") or "tbt_missing"),
                "symbol": normalized_symbol,
                "interval_sec": interval_sec,
                "min_delta_ratio": min_delta_ratio,
                "tbt": tbt_status,
            }
        bars = [
            bar for bar in self.aggregator.snapshot(normalized_symbol)
            if bar.interval_seconds == interval_sec
        ]
        if not bars:
            return {
                "ok": False,
                "reason": "order_flow_missing",
                "symbol": normalized_symbol,
                "interval_sec": interval_sec,
                "min_delta_ratio": min_delta_ratio,
                "tbt": tbt_status,
            }
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
            "tbt": tbt_status,
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

    def sync_positions(self, positions: list[dict[str, Any]] | None) -> dict[str, Any]:
        if not self.enabled():
            return {"enabled": False}
        current_ms = int(time.time() * 1000)
        self.execution_pool.release_expired_watches(current_ms)
        active_symbols: set[str] = set()
        synced = 0
        blocked = 0
        for item in positions or []:
            symbol = normalize_symbol(str(item.get("ticker") or item.get("symbol") or item.get("contractDesc") or ""))
            quantity = self._safe_float(item.get("position", item.get("quantity", 0)), 0.0)
            conid = int(self._safe_float(item.get("conid"), 0.0))
            if not symbol or not quantity:
                continue
            direction = "long" if quantity > 0 else "short"
            accepted, watch, reason = self.execution_pool.upsert_position_watch(
                symbol,
                direction=direction,
                conid=conid,
                at_ms=current_ms,
                candidate_key=f"{symbol}:position",
            )
            if not accepted:
                blocked += 1
                self._record_decision({"action": "position_watch_blocked", "symbol": symbol, "reason": reason})
                continue
            active_symbols.add(symbol)
            synced += 1
            if conid > 0:
                self._conid_by_symbol[symbol] = conid
                self._subscribe_tick(conid)
            self._record_decision({"action": "position_watch", "symbol": symbol, "slot": self._watch_dict(watch)})

        for symbol in sorted(self._open_position_symbols - active_symbols):
            self.release_symbol(symbol, reason="position_flat")
        self._open_position_symbols = active_symbols
        return {"enabled": True, "synced": synced, "blocked": blocked, "active_symbols": sorted(active_symbols)}

    def position_decision(
        self,
        position: dict[str, Any],
        *,
        quote: dict[str, Any] | None = None,
        order_group: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled():
            return {"action": "hold", "reason": "order_flow_disabled", "enforced": False}
        if self.mode() != "enforce":
            return {"action": "hold", "reason": "not_enforce_mode", "enforced": False}
        symbol = normalize_symbol(str(position.get("symbol") or position.get("ticker") or ""))
        direction = str(position.get("direction") or "").strip().lower()
        if not symbol or direction not in {"long", "short"}:
            return {"action": "hold", "reason": "invalid_position", "enforced": True}
        quote = self._quote_for_symbol(symbol, quote)
        spread = self._spread_snapshot(quote)
        if not spread.get("ok"):
            return {"action": "hold", "reason": spread.get("reason") or "quote_unavailable", "spread": spread, "enforced": True}

        opposite = "short" if direction == "long" else "long"
        confirmation = self.confirmation(symbol, opposite)
        if not confirmation.get("ok"):
            return {"action": "hold", "reason": str(confirmation.get("reason") or "no_adverse_delta"), "confirmation": confirmation, "spread": spread, "enforced": True}

        current_price = self._current_price_for_direction(direction, quote)
        entry_price = self._safe_float(position.get("entry_price", position.get("entry")), 0.0)
        stop_price = self._safe_float(position.get("stop_price", position.get("stop_loss")), 0.0)
        risk_r = self._safe_float(position.get("risk_r"), abs(entry_price - stop_price))
        if current_price <= 0 or entry_price <= 0 or risk_r <= 0:
            return {"action": "hold", "reason": "missing_position_prices", "confirmation": confirmation, "spread": spread, "enforced": True}
        pnl_r = ((current_price - entry_price) if direction == "long" else (entry_price - current_price)) / risk_r
        exit_ratio = max(
            self._config_float("ibkr_order_flow_exit_delta_ratio", 0.18),
            self._config_float("ibkr_order_flow_min_delta_ratio", 0.12),
        )
        stop_ratio = max(
            self._config_float("ibkr_order_flow_stop_delta_ratio", 0.12),
            self._config_float("ibkr_order_flow_min_delta_ratio", 0.12),
        )
        delta_ratio = abs(self._safe_float(confirmation.get("delta_ratio"), 0.0))

        if self._config_bool("ibkr_order_flow_auto_exit_enabled", True) and delta_ratio >= exit_ratio and pnl_r <= 0.15:
            marketable = self._marketable_limit_price("short" if direction == "long" else "long", quote)
            decision = {
                "action": "full_exit",
                "reason": "order_flow_adverse_delta_exit",
                "symbol": symbol,
                "direction": direction,
                "pnl_r": round(pnl_r, 4),
                "confirmation": confirmation,
                "spread": spread,
                "marketable_limit": marketable,
                "limit_price": marketable.get("price") if marketable.get("ok") else 0.0,
                "enforced": True,
            }
            self._record_decision(decision)
            return decision

        if self._config_bool("ibkr_order_flow_stop_tighten_enabled", True) and delta_ratio >= stop_ratio:
            stop_price_new = self._tightened_stop_price(direction, current_price, entry_price, stop_price, risk_r)
            if stop_price_new > 0:
                decision = {
                    "action": "tighten_stop",
                    "reason": "order_flow_adverse_delta_tighten_stop",
                    "symbol": symbol,
                    "direction": direction,
                    "pnl_r": round(pnl_r, 4),
                    "stop_price": stop_price_new,
                    "old_stop": stop_price,
                    "confirmation": confirmation,
                    "spread": spread,
                    "enforced": True,
                }
                self._record_decision(decision)
                return decision

        return {
            "action": "hold",
            "reason": "adverse_delta_below_action_threshold",
            "pnl_r": round(pnl_r, 4),
            "confirmation": confirmation,
            "spread": spread,
            "enforced": True,
        }

    def release_symbol(self, symbol: str, *, reason: str = "released") -> None:
        normalized_symbol = normalize_symbol(symbol)
        current_ms = int(time.time() * 1000)
        releaser = getattr(self.execution_pool, "release_candidate_watch", None)
        released = releaser(normalized_symbol) if callable(releaser) else None
        self._entry_wait_started_ms = {
            key: value
            for key, value in self._entry_wait_started_ms.items()
            if not str(key).startswith(f"{normalized_symbol}:")
        }
        conid = self._conid_by_symbol.pop(normalized_symbol, 0)
        if conid:
            self._unsubscribe_tick(conid)
        self._record_decision({"action": "release_symbol", "symbol": normalized_symbol, "accepted": released is not None, "reason": reason})

    def _release_entry_wait(self, symbol: str, wait_key: str, *, reason: str) -> None:
        self._entry_wait_started_ms.pop(wait_key, None)
        normalized_symbol = normalize_symbol(symbol)
        released = self.execution_pool.release_candidate_watch(normalized_symbol)
        conid = self._conid_by_symbol.pop(normalized_symbol, 0)
        if conid:
            self._unsubscribe_tick(conid)
        self._record_decision({
            "action": "release_entry_wait",
            "symbol": normalized_symbol,
            "wait_key": wait_key,
            "accepted": released is not None,
            "reason": reason,
        })

    def status(self) -> dict[str, Any]:
        with self._lock:
            recent_bars = [bar.to_dict() for bar in list(self._recent_closed_bars)[-25:]]
            decisions = list(self._decision_log[-25:])
            tick_count = int(self._tick_count)
            ignored_non_tbt_tick_count = int(self._ignored_non_tbt_tick_count)
            last_tick_ms = int(self._last_tick_ms)
            last_tbt_tick_ms_by_symbol = dict(self._last_tbt_tick_ms_by_symbol)
            tbt_tick_count_by_symbol = dict(self._tbt_tick_count_by_symbol)
            last_ignored_tick_ms = int(self._last_ignored_tick_ms)
            last_ignored_tick_source = str(self._last_ignored_tick_source or "")
        ws_status = self._ws_status()
        active_conids = sorted({
            self._conid_by_symbol.get(symbol, 0)
            for symbol in self.execution_pool.active_symbols()
            if self._conid_by_symbol.get(symbol, 0)
        })
        return {
            "enabled": self.enabled(),
            "mode": self.mode(),
            "environment": self.broker_environment,
            "broker_environment": self.broker_environment,
            "data_environment": self.data_environment,
            "tick_count": tick_count,
            "tbt_tick_count": tick_count,
            "ignored_non_tbt_tick_count": ignored_non_tbt_tick_count,
            "last_tick_ms": last_tick_ms,
            "last_tbt_tick_ms": last_tick_ms,
            "last_ignored_tick_ms": last_ignored_tick_ms,
            "last_ignored_tick_source": last_ignored_tick_source,
            "tick_by_tick": {
                "policy": "tbt_only",
                "expected_subscription_count": len(active_conids),
                "expected_conids": active_conids,
                "subscribed_count": int(ws_status.get("tick_by_tick_subscribed_count", 0) or 0),
                "pending_count": int(ws_status.get("tick_by_tick_pending_count", 0) or 0),
                "last_tick_ms_by_symbol": last_tbt_tick_ms_by_symbol,
                "tick_count_by_symbol": tbt_tick_count_by_symbol,
                "ignored_l1_tick_count": ignored_non_tbt_tick_count,
                "freshness_sec": self._tbt_freshness_sec(),
            },
            "current_cvd": {symbol: self.aggregator.current_cvd(symbol) for symbol in self.execution_pool.active_symbols()},
            "candidate_queue": {
                "active_count": len(self.candidates),
                "candidates": [self._candidate_dict(candidate) for candidate in self.candidates.ranked(limit=10)],
            },
            "execution_pool": {
                **self.execution_pool.status(),
                "active_conids": active_conids,
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
        if not self._is_tick_by_tick_payload(payload):
            return None
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
        quote = self._quote_for_symbol(symbol, None)
        bid = self._first_optional_float(payload, ("bid", "bid_price", "84"))
        ask = self._first_optional_float(payload, ("ask", "ask_price", "86"))
        if bid is None:
            bid = self._safe_float((quote or {}).get("bid"), 0.0) or None
        if ask is None:
            ask = self._safe_float((quote or {}).get("ask"), 0.0) or None
        return OrderFlowTick(
            symbol=symbol,
            timestamp_ms=timestamp_ms,
            price=price,
            size=size,
            side=str(payload.get("side") or ""),
            bid=bid,
            ask=ask,
            source=str(payload.get("source") or payload.get("tick_source") or ""),
            metadata={"conid": payload.get("conid") or payload.get("conidEx")},
        )

    @staticmethod
    def _is_tick_by_tick_payload(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        source = str(payload.get("source") or payload.get("tick_source") or "").strip().lower()
        normalized_source = source.replace("-", "_").replace(" ", "_")
        if normalized_source in {"tick_by_tick", "tickbytick", "tbt", "ibkr_tbt", "tick_by_tick_all_last"}:
            return True
        return False

    def _record_ignored_non_tbt_tick(self, payload: dict[str, Any] | None) -> None:
        payload = payload if isinstance(payload, dict) else {}
        source = str(payload.get("source") or payload.get("tick_source") or "l1_market_data").strip() or "l1_market_data"
        timestamp_ms = int(self._first_float(payload, ("timestamp_ms", "time_ms", "_updated")))
        if timestamp_ms <= 0:
            raw_time = int(self._first_float(payload, ("time",)))
            timestamp_ms = raw_time * 1000 if 0 < raw_time < 10_000_000_000 else int(time.time() * 1000)
        with self._lock:
            self._ignored_non_tbt_tick_count += 1
            self._last_ignored_tick_ms = max(int(self._last_ignored_tick_ms or 0), timestamp_ms)
            self._last_ignored_tick_source = source

    def _tbt_status(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = normalize_symbol(symbol)
        freshness_sec = self._tbt_freshness_sec()
        now_ms = int(time.time() * 1000)
        with self._lock:
            last_ms = int(self._last_tbt_tick_ms_by_symbol.get(normalized_symbol, 0) or 0)
            tick_count = int(self._tbt_tick_count_by_symbol.get(normalized_symbol, 0) or 0)
        if tick_count <= 0 or last_ms <= 0:
            return {
                "has_recent_tbt": False,
                "reason": "tbt_missing",
                "symbol": normalized_symbol,
                "last_tick_ms": last_ms,
                "tick_count": tick_count,
                "freshness_sec": freshness_sec,
            }
        age_ms = max(0, now_ms - last_ms)
        if freshness_sec > 0 and age_ms > freshness_sec * 1000:
            return {
                "has_recent_tbt": False,
                "reason": "tbt_stale",
                "symbol": normalized_symbol,
                "last_tick_ms": last_ms,
                "age_ms": age_ms,
                "tick_count": tick_count,
                "freshness_sec": freshness_sec,
            }
        return {
            "has_recent_tbt": True,
            "reason": "ok",
            "symbol": normalized_symbol,
            "last_tick_ms": last_ms,
            "age_ms": age_ms,
            "tick_count": tick_count,
            "freshness_sec": freshness_sec,
        }

    def _tbt_freshness_sec(self) -> int:
        default = max(10, self._config_int("ibkr_order_flow_confirm_window_sec", 60) * 2)
        return max(0, self._config_int("ibkr_order_flow_tbt_freshness_sec", default))

    def _ws_status(self) -> dict[str, Any]:
        provider = getattr(self.ws_client, "status", None)
        if not callable(provider):
            return {}
        try:
            status = provider()
        except Exception as exc:
            self._last_error = str(exc)
            return {}
        return dict(status or {}) if isinstance(status, dict) else {}

    def _quote_for_symbol(self, symbol: str, quote: dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(quote, dict) and quote:
            return dict(quote)
        provider = self.quote_provider
        if not callable(provider):
            return {}
        try:
            value = provider(symbol)
        except Exception as exc:
            self._last_error = str(exc)
            return {}
        return dict(value or {}) if isinstance(value, dict) else {}

    def _spread_snapshot(self, quote: dict[str, Any] | None) -> dict[str, Any]:
        quote = quote if isinstance(quote, dict) else {}
        bid = self._safe_float(quote.get("bid"), 0.0)
        ask = self._safe_float(quote.get("ask"), 0.0)
        last = self._safe_float(quote.get("last_price", quote.get("last")), 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False, "reason": "quote_bid_ask_unavailable", "bid": bid, "ask": ask, "last": last}
        mid = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 else 0.0
        max_spread = self._config_float("ibkr_order_flow_max_spread_bps", 12.0)
        return {
            "ok": spread_bps <= max_spread,
            "reason": "ok" if spread_bps <= max_spread else "spread_too_wide",
            "bid": bid,
            "ask": ask,
            "last": last,
            "spread_bps": round(spread_bps, 4),
            "max_spread_bps": max_spread,
        }

    def _marketable_limit_price(self, direction: str, quote: dict[str, Any] | None) -> dict[str, Any]:
        quote = quote if isinstance(quote, dict) else {}
        normalized = str(direction or "").strip().lower()
        bid = self._safe_float(quote.get("bid"), 0.0)
        ask = self._safe_float(quote.get("ask"), 0.0)
        last = self._safe_float(quote.get("last_price", quote.get("last")), 0.0)
        reference = ask if normalized == "long" else bid
        source = "ask" if normalized == "long" else "bid"
        if reference <= 0:
            reference = last
            source = "last_price"
        if reference <= 0:
            return {"ok": False, "reason": "reference_price_unavailable"}
        bps = max(0.0, self._config_float("ibkr_order_flow_marketable_limit_bps", 8.0))
        offset = max(0.02, reference * bps / 10000.0)
        price = reference + offset if normalized == "long" else reference - offset
        if price <= 0:
            return {"ok": False, "reason": "invalid_marketable_limit_price"}
        return {
            "ok": True,
            "price": round(price, 2),
            "reference": round(reference, 4),
            "reference_source": source,
            "offset": round(offset, 4),
            "bps": bps,
            "order_type": "marketable_limit",
        }

    def _current_price_for_direction(self, direction: str, quote: dict[str, Any]) -> float:
        if str(direction or "").strip().lower() == "short":
            return self._safe_float(quote.get("ask"), 0.0) or self._safe_float(quote.get("last_price"), 0.0)
        return self._safe_float(quote.get("bid"), 0.0) or self._safe_float(quote.get("last_price"), 0.0)

    @staticmethod
    def _tightened_stop_price(direction: str, current_price: float, entry_price: float, old_stop: float, risk_r: float) -> float:
        cushion = max(0.01, float(risk_r or 0.0) * 0.10)
        if str(direction or "").strip().lower() == "short":
            candidate = max(entry_price, current_price + cushion) if current_price < entry_price else current_price + cushion
            if old_stop > 0 and candidate >= old_stop - 0.005:
                return 0.0
            return round(max(candidate, current_price + 0.01), 2)
        candidate = min(entry_price, current_price - cushion) if current_price > entry_price else current_price - cushion
        if old_stop > 0 and candidate <= old_stop + 0.005:
            return 0.0
        return round(min(candidate, current_price - 0.01), 2)

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
