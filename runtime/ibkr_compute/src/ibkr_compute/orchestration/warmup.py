from __future__ import annotations

import math

from ibkr_compute.market.timeframe_utils import interval_to_ms, normalize_interval


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceWarmupMixin:
    def _initial_warmup_state(self) -> dict:
        service_mod = _service_mod()
        return {
            "phase": "idle",
            "reason": "",
            "required_interval": service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
            "last_success_at": None,
            "last_error": "",
            "data_ready": False,
            "trade_allowed": False,
            "trading_gate_open": False,
            "trading_gate_reason": "warmup_idle",
            "target_date": "",
            "symbols_total": 0,
            "scan_symbols_total": 0,
            "subscription_symbols_total": 0,
            "trade_symbols_total": 0,
            "monitor_symbols_total": 0,
            "ready_symbols": 0,
            "ready_scan_symbols": 0,
            "ready_subscription_symbols": 0,
            "ready_trade_symbols": 0,
            "ready_monitor_symbols": 0,
            "symbols": [],
            "scan_symbols": [],
            "subscription_symbols": [],
            "trade_symbols": [],
            "monitor_symbols": [],
            "ready_symbols_list": [],
            "pending_symbols": [],
            "symbol_status": [],
            "integrity_pending_symbols": [],
            "integrity_repair_reasons": {},
            "preflight_repair": {},
            "backfill_written": 0,
            "backfill_result": {},
            "compute_result": {},
            "timings": {},
            "last_duration_s": 0.0,
        }

    def _copy_warmup_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._warmup_state
        copied = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _normalize_symbol_list(self, values) -> list[str]:
        normalized = []
        seen = set()
        for raw in list(values or []):
            symbol = str(raw or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    def _configured_market_ws_symbols(self) -> list[str]:
        service_mod = _service_mod()
        if not self.config.get_bool_for_environment("ibkr_market_ws_enabled", service_mod.DATA_ENVIRONMENT, True):
            return []
        raw_value = self.config.get_for_environment(
            "ibkr_market_ws_symbols",
            service_mod.DATA_ENVIRONMENT,
            ",".join(service_mod.DEFAULT_MARKET_WS_SYMBOLS),
        )
        return self._normalize_symbol_list(str(raw_value or "").replace("\n", ",").split(","))

    def _market_ws_symbols(self) -> list[str]:
        return self._normalize_symbol_list(
            list(self._watchlist_monitor_symbols) + list(self._configured_market_ws_symbols())
        )

    def _live_warmup_days(self) -> int:
        service_mod = _service_mod()
        return max(1, self.config.get_int_for_environment("ibkr_live_warmup_days", service_mod.DATA_ENVIRONMENT, 14))

    def _multi_timeframe_warmup_intervals(self) -> list[str]:
        service_mod = _service_mod()
        default_intervals = [
            service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
            *list(getattr(service_mod, "STARTUP_BACKGROUND_PRIME_INTERVALS", ()) or ()),
        ]
        configured = self.config.get_for_environment(
            "ibkr_warmup_indicator_intervals",
            service_mod.DATA_ENVIRONMENT,
            ",".join(default_intervals),
        )
        intervals = []
        for raw in str(configured or "").replace("\n", ",").split(","):
            try:
                interval = normalize_interval(raw)
                interval_to_ms(interval)
            except Exception:
                continue
            if interval not in intervals:
                intervals.append(interval)
        return intervals or default_intervals

    def _multi_timeframe_warmup_days(self) -> int:
        service_mod = _service_mod()
        base_days = self._live_warmup_days()
        ready_bars = max(1, int(service_mod.indicator_ready_bar_count() or 0))
        session_minutes = max(
            60,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_regular_minutes_per_day",
                service_mod.DATA_ENVIRONMENT,
                390,
            ),
        )
        calendar_multiplier = max(
            1.0,
            self.config.get_float_for_environment(
                "ibkr_warmup_indicator_calendar_multiplier",
                service_mod.DATA_ENVIRONMENT,
                1.4,
            ),
        )
        buffer_days = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_buffer_days",
                service_mod.DATA_ENVIRONMENT,
                5,
            ),
        )
        max_days = max(
            base_days,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_max_days",
                service_mod.DATA_ENVIRONMENT,
                60,
            ),
        )
        required_minutes = max(
            (
                max(1, interval_to_ms(interval) // 60000) * ready_bars
                for interval in self._multi_timeframe_warmup_intervals()
            ),
            default=ready_bars * 5,
        )
        calculated_days = int(math.ceil((required_minutes / session_minutes) * calendar_multiplier)) + buffer_days
        return min(max_days, max(base_days, calculated_days))

    def _multi_timeframe_warmup_period(self) -> str:
        return f"{self._multi_timeframe_warmup_days()}d"

    def _warmup_required_5m_period(self) -> str:
        service_mod = _service_mod()
        period = self.config.get_for_environment(
            "ibkr_warmup_required_5m_period",
            service_mod.DATA_ENVIRONMENT,
            "4d",
        )
        return str(period or "4d").strip() or "4d"

    def _multi_timeframe_5m_period_overrides(self, symbols) -> dict[str, dict]:
        period = self._warmup_required_5m_period()
        return {
            str(symbol or "").strip().upper(): {"5m": period}
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }

    def _restart_overlap_days(self) -> int:
        service_mod = _service_mod()
        return max(1, self.config.get_int_for_environment("ibkr_restart_overlap_days", service_mod.DATA_ENVIRONMENT, 1))

    def _data_universe_symbols(self) -> list[str]:
        return self._normalize_symbol_list(list(self._watchlist_symbols) + list(self._market_ws_symbols()))

    def _warmup_scope_fields(self, snapshot: dict) -> dict:
        return {
            "target_date": snapshot.get("target_date", ""),
            "symbols_total": int(snapshot.get("symbols_total", 0) or 0),
            "scan_symbols_total": int(snapshot.get("scan_symbols_total", 0) or 0),
            "subscription_symbols_total": int(snapshot.get("subscription_symbols_total", 0) or 0),
            "trade_symbols_total": int(snapshot.get("trade_symbols_total", 0) or 0),
            "monitor_symbols_total": int(snapshot.get("monitor_symbols_total", 0) or 0),
            "symbols": list(snapshot.get("symbols") or []),
            "scan_symbols": list(snapshot.get("scan_symbols") or []),
            "subscription_symbols": list(snapshot.get("subscription_symbols") or []),
            "trade_symbols": list(snapshot.get("trade_symbols") or []),
            "monitor_symbols": list(snapshot.get("monitor_symbols") or []),
        }

    def _build_transient_warmup_retry_state(
        self,
        snapshot: dict,
        previous_state: dict | None = None,
        *,
        reason: str,
        requested_at: str | None = None,
        started_at: str | None = None,
    ) -> dict:
        previous = self._copy_warmup_state(previous_state)
        symbols = self._normalize_symbol_list(snapshot.get("symbols") or previous.get("symbols") or [])
        trade_symbols = self._normalize_symbol_list(
            snapshot.get("trade_symbols") or previous.get("trade_symbols") or []
        )
        monitor_symbols = self._normalize_symbol_list(
            snapshot.get("monitor_symbols") or previous.get("monitor_symbols") or []
        )
        scan_symbols = self._normalize_symbol_list(
            snapshot.get("scan_symbols") or previous.get("scan_symbols") or []
        )
        ready_set = set(self._normalize_symbol_list(previous.get("ready_symbols_list") or []))
        ready_symbols_list = [symbol for symbol in symbols if symbol in ready_set]
        pending_symbols = [symbol for symbol in symbols if symbol not in ready_set]
        integrity_pending_symbols = [
            symbol
            for symbol in self._normalize_symbol_list(previous.get("integrity_pending_symbols") or [])
            if symbol in symbols
        ]
        integrity_repair_reasons = {
            symbol: str(reason_text or "")
            for symbol, reason_text in dict(previous.get("integrity_repair_reasons") or {}).items()
            if symbol in integrity_pending_symbols
        }

        status_by_symbol = {}
        for row in list(previous.get("symbol_status") or []):
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                status_by_symbol[symbol] = dict(row)

        scan_set = set(scan_symbols)
        trade_set = set(trade_symbols)
        monitor_set = set(monitor_symbols)
        symbol_status = []
        for symbol in symbols:
            existing = dict(status_by_symbol.get(symbol) or {})
            if symbol in trade_set:
                role = "trade"
            elif symbol in monitor_set:
                role = "monitor"
            elif symbol in scan_set:
                role = "scan"
            else:
                role = "data"
            if existing or symbol in ready_set:
                existing["symbol"] = symbol
                existing["role"] = str(existing.get("role") or role)
                if symbol in ready_set:
                    existing["ready"] = True
                    existing["integrity_ready"] = bool(
                        existing.get("integrity_ready", symbol not in integrity_repair_reasons)
                    )
                    existing["integrity_reason"] = str(existing.get("integrity_reason") or "")
                symbol_status.append(existing)

        blocking_pending_symbols = self._non_monitor_pending_symbols(pending_symbols, monitor_symbols)
        blocking_integrity_pending_symbols = self._non_monitor_pending_symbols(
            integrity_pending_symbols,
            monitor_symbols,
        )
        if not blocking_pending_symbols and not blocking_integrity_pending_symbols:
            phase = "ready"
        elif ready_symbols_list:
            phase = "running"
        else:
            phase = "pending"

        ready_trade_symbols = len([symbol for symbol in trade_symbols if symbol in ready_set])
        trading_gate_open = bool(trade_symbols) and ready_trade_symbols >= len(trade_symbols)
        if not trade_symbols:
            trading_gate_reason = "no_trade_symbols"
        elif trading_gate_open:
            trading_gate_reason = "ready"
        else:
            trading_gate_reason = reason

        preserve_finished = phase == "ready"
        previous_finished_at = str(previous.get("finished_at") or "").strip()
        previous_last_success_at = str(previous.get("last_success_at") or "").strip()
        return {
            "phase": phase,
            "reason": reason,
            "requested_at": requested_at or self._now_iso(),
            "started_at": previous.get("started_at") if preserve_finished else started_at,
            "finished_at": (previous_finished_at or None) if preserve_finished else None,
            "last_success_at": previous_last_success_at or (previous_finished_at or None),
            "last_error": "",
            "trading_gate_open": trading_gate_open,
            "trading_gate_reason": trading_gate_reason,
            "ready_symbols_list": ready_symbols_list,
            "pending_symbols": pending_symbols,
            "symbol_status": symbol_status,
            "integrity_pending_symbols": integrity_pending_symbols,
            "integrity_repair_reasons": integrity_repair_reasons,
            **self._warmup_scope_fields(snapshot),
        }

    def _set_warmup_state(self, **updates) -> dict:
        with self._warmup_lock:
            next_state = self._copy_warmup_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = dict(value)
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            symbols = self._normalize_symbol_list(next_state.get("symbols") or [])
            scan_symbols = self._normalize_symbol_list(next_state.get("scan_symbols") or [])
            trade_symbols = self._normalize_symbol_list(next_state.get("trade_symbols") or [])
            monitor_symbols = self._normalize_symbol_list(next_state.get("monitor_symbols") or [])
            subscription_symbols = self._normalize_symbol_list(
                next_state.get("subscription_symbols") or (trade_symbols + monitor_symbols)
            )
            symbol_set = set(symbols)
            ready_symbols_list = [
                symbol
                for symbol in self._normalize_symbol_list(next_state.get("ready_symbols_list") or [])
                if symbol in symbol_set
            ]
            ready_set = set(ready_symbols_list)
            pending_symbols = [
                symbol
                for symbol in self._normalize_symbol_list(next_state.get("pending_symbols") or [])
                if symbol in symbol_set and symbol not in ready_set
            ]
            integrity_pending_symbols = [
                symbol
                for symbol in self._normalize_symbol_list(next_state.get("integrity_pending_symbols") or [])
                if symbol in symbol_set
            ]
            next_state["symbols"] = symbols
            next_state["scan_symbols"] = scan_symbols
            next_state["subscription_symbols"] = subscription_symbols
            next_state["trade_symbols"] = trade_symbols
            next_state["monitor_symbols"] = monitor_symbols
            next_state["symbols_total"] = len(symbols)
            next_state["scan_symbols_total"] = len(scan_symbols)
            next_state["subscription_symbols_total"] = len(subscription_symbols)
            next_state["trade_symbols_total"] = len(trade_symbols)
            next_state["monitor_symbols_total"] = len(monitor_symbols)
            next_state["ready_symbols_list"] = ready_symbols_list
            next_state["pending_symbols"] = pending_symbols
            next_state["integrity_pending_symbols"] = integrity_pending_symbols
            next_state["ready_symbols"] = len(ready_symbols_list)
            next_state["ready_scan_symbols"] = len([symbol for symbol in scan_symbols if symbol in ready_set])
            next_state["ready_subscription_symbols"] = len([symbol for symbol in subscription_symbols if symbol in ready_set])
            next_state["ready_trade_symbols"] = len([symbol for symbol in trade_symbols if symbol in ready_set])
            next_state["ready_monitor_symbols"] = len([symbol for symbol in monitor_symbols if symbol in ready_set])
            next_state["data_ready"] = bool(symbols) and not pending_symbols and not integrity_pending_symbols
            next_state["trade_allowed"] = bool(next_state.get("trading_gate_open"))
            self._warmup_state = next_state
            return self._copy_warmup_state(next_state)

    def _reset_warmup_state(self, reason: str = ""):
        with self._warmup_lock:
            self._warmup_signature = ()
            self._warmup_state = self._initial_warmup_state()
            if reason:
                self._warmup_state["reason"] = reason
                self._warmup_state["trading_gate_reason"] = "warmup_reset"

    def _non_monitor_pending_symbols(
        self,
        pending_symbols: list[str] | None,
        monitor_symbols: list[str] | None = None,
    ) -> list[str]:
        monitor_set = set(self._normalize_symbol_list(monitor_symbols or []))
        return [
            symbol
            for symbol in self._normalize_symbol_list(pending_symbols or [])
            if symbol not in monitor_set
        ]

    def _warmup_snapshot_from_subscriptions(self) -> dict:
        service_mod = _service_mod()
        with self._subscription_lock:
            target_date = self._active_target_date or self._current_market_date or self._market_date()
            trade_symbols = sorted(self._active_trade_symbols)
            active_conid_map = dict(self._active_subscription_map)
            symbol_meta_seed = {
                symbol: dict(self._symbol_meta.get(symbol) or {})
                for symbol in self._symbol_meta.keys()
            }
        symbols = self._data_universe_symbols()
        scan_symbols = self._normalize_symbol_list(self._watchlist_trade_symbols)
        monitor_symbols = self._market_ws_symbols()
        subscription_symbols = self._normalize_symbol_list(monitor_symbols + trade_symbols)
        backfill_symbol_set = set(trade_symbols) | set(monitor_symbols)
        conid_map = {
            symbol: int(active_conid_map.get(symbol) or 0)
            for symbol in symbols
            if symbol in backfill_symbol_set and int(active_conid_map.get(symbol) or 0) > 0
        }
        unresolved = [
            symbol
            for symbol in symbols
            if symbol in backfill_symbol_set and symbol not in conid_map
        ]
        if unresolved:
            try:
                resolved = self.conid_resolver.resolve_bulk(unresolved)
            except Exception:
                resolved = {}
            for symbol, conid in (resolved or {}).items():
                if int(conid or 0) > 0:
                    conid_map[str(symbol or "").strip().upper()] = int(conid)
        symbol_meta = {}
        scan_symbol_set = set(scan_symbols)
        monitor_symbol_set = set(monitor_symbols)
        for symbol in symbols:
            base_meta = dict(symbol_meta_seed.get(symbol) or {})
            role = base_meta.get("symbol_role")
            if not role:
                if symbol in monitor_symbol_set:
                    role = service_mod.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
                elif symbol in scan_symbol_set:
                    role = service_mod.WATCHLIST_SYMBOL_ROLE_TRADE
                else:
                    role = "data"
            symbol_meta[symbol] = {
                **base_meta,
                "exchange": str(base_meta.get("exchange") or "").upper(),
                "industry": str(base_meta.get("industry") or ""),
                "symbol_role": str(role or "data"),
            }
        return {
            "target_date": target_date,
            "symbols": symbols,
            "scan_symbols": scan_symbols,
            "subscription_symbols": subscription_symbols,
            "trade_symbols": trade_symbols,
            "monitor_symbols": monitor_symbols,
            "symbols_total": len(symbols),
            "scan_symbols_total": len(scan_symbols),
            "subscription_symbols_total": len(subscription_symbols),
            "trade_symbols_total": len(trade_symbols),
            "monitor_symbols_total": len(monitor_symbols),
            "conid_map": conid_map,
            "symbol_meta": symbol_meta,
            "signature": (
                target_date,
                tuple(symbols),
                tuple(scan_symbols),
                tuple(subscription_symbols),
                tuple(trade_symbols),
                tuple(monitor_symbols),
            ),
        }

    def _trade_readiness_scope_snapshot(self, state: dict) -> dict:
        snapshot = {
            "symbols": self._normalize_symbol_list(state.get("symbols") or []),
            "trade_symbols": self._normalize_symbol_list(state.get("trade_symbols") or []),
            "monitor_symbols": self._normalize_symbol_list(state.get("monitor_symbols") or []),
            "subscription_symbols": self._normalize_symbol_list(state.get("subscription_symbols") or []),
        }
        if snapshot["trade_symbols"] or snapshot["subscription_symbols"] or snapshot["symbols"]:
            return snapshot
        try:
            return self._warmup_snapshot_from_subscriptions()
        except Exception:
            return snapshot

    def _trade_readiness_from_current_compute(self, state: dict) -> dict | None:
        service_mod = _service_mod()
        snapshot = self._trade_readiness_scope_snapshot(state)
        trade_symbols = self._normalize_symbol_list(snapshot.get("trade_symbols") or [])
        if not trade_symbols:
            return {"open": False, "reason": "no_trade_symbols", "source": "current_readiness"}

        requested_symbols = self._normalize_symbol_list(
            snapshot.get("subscription_symbols")
            or list(trade_symbols) + list(snapshot.get("monitor_symbols") or [])
            or snapshot.get("symbols")
            or []
        )
        if not requested_symbols:
            requested_symbols = trade_symbols

        required_interval = getattr(service_mod, "DEFAULT_WARMUP_REQUIRED_INTERVAL", "5m")
        readiness = {}
        source = "local_multi_timeframe_readiness"
        try:
            uses_remote = False
            uses_remote_fn = getattr(self, "_warmup_uses_remote_compute_service", None)
            if callable(uses_remote_fn):
                uses_remote = bool(uses_remote_fn())
            if uses_remote:
                from ibkr_compute.api.compute_status_client import get_remote_compute_status

                chunk_size = 30
                missing_symbols: list[str] = []
                latest_bar_time_ms = 0
                latest_indicator_time_ms = 0
                storage_checked = False
                unknown = False
                for index in range(0, len(requested_symbols), chunk_size):
                    chunk = requested_symbols[index:index + chunk_size]
                    payload = get_remote_compute_status(
                        force_refresh=False,
                        symbols=chunk,
                        include_engines=False,
                    )
                    candidate = dict(payload.get("multi_timeframe_readiness") or {}) if isinstance(payload, dict) else {}
                    intervals = candidate.get("intervals") if isinstance(candidate.get("intervals"), dict) else {}
                    interval = intervals.get(required_interval) if isinstance(intervals.get(required_interval), dict) else {}
                    if not interval:
                        unknown = True
                        missing_symbols.extend(chunk)
                        continue
                    storage_checked = storage_checked or bool(interval.get("storage_checked"))
                    latest_bar_time_ms = max(latest_bar_time_ms, int(interval.get("latest_bar_time_ms", 0) or 0))
                    latest_indicator_time_ms = max(
                        latest_indicator_time_ms,
                        int(interval.get("latest_indicator_time_ms", 0) or 0),
                    )
                    missing_symbols.extend(self._normalize_symbol_list(interval.get("missing_ready_symbols") or []))
                missing_symbols = self._normalize_symbol_list(missing_symbols)
                readiness = {
                    "environment": service_mod.DATA_ENVIRONMENT,
                    "status": "unknown" if unknown else ("blocked" if missing_symbols else "ready"),
                    "hard_gate_interval": required_interval,
                    "symbols_total": len(requested_symbols),
                    "intervals": {
                        required_interval: {
                            "status": "blocked" if missing_symbols else "ready",
                            "storage_checked": storage_checked,
                            "symbols_total": len(requested_symbols),
                            "missing_ready_symbols": missing_symbols[:50],
                            "missing_ready_symbols_total": len(missing_symbols),
                            "latest_bar_time_ms": latest_bar_time_ms,
                            "latest_indicator_time_ms": latest_indicator_time_ms,
                        }
                    },
                }
                source = "runtime_multi_timeframe_readiness"
            else:
                from ibkr_compute.api.compute.prime import build_multi_timeframe_readiness

                readiness = build_multi_timeframe_readiness(
                    environment=service_mod.DATA_ENVIRONMENT,
                    symbols=requested_symbols,
                    intervals=[required_interval],
                    use_cache=True,
                    include_storage=True,
                )
        except Exception:
            readiness = {}

        intervals = readiness.get("intervals") if isinstance(readiness.get("intervals"), dict) else {}
        hard_interval = str(readiness.get("hard_gate_interval") or required_interval or "5m").strip() or "5m"
        interval = intervals.get(hard_interval) if isinstance(intervals.get(hard_interval), dict) else {}
        if not interval:
            if bool(state.get("trading_gate_open")):
                return {"open": True, "reason": str(state.get("trading_gate_reason") or "ready"), "source": "previous_warmup_snapshot"}
            return {"open": False, "reason": "remote_compute_status_unavailable", "source": source}

        status = str(interval.get("status") or "").strip().lower()
        missing_ready_symbols = self._normalize_symbol_list(interval.get("missing_ready_symbols") or [])
        try:
            missing_ready_total = int(interval.get("missing_ready_symbols_total", len(missing_ready_symbols)) or 0)
        except (TypeError, ValueError):
            missing_ready_total = len(missing_ready_symbols)
        missing_trade_symbols = sorted(set(missing_ready_symbols).intersection(trade_symbols))

        if status == "ready" and missing_ready_total == 0:
            return {"open": True, "reason": "ready", "source": source}
        if missing_ready_total > 0 and missing_ready_symbols and not missing_trade_symbols:
            return {"open": True, "reason": "non_trade_readiness_pending", "source": source}
        if missing_trade_symbols:
            return {
                "open": False,
                "reason": "missing_trade_symbols",
                "source": source,
                "symbols": missing_trade_symbols[:20],
            }
        if bool(state.get("trading_gate_open")):
            return {"open": True, "reason": str(state.get("trading_gate_reason") or "ready"), "source": "previous_warmup_snapshot"}
        return {"open": False, "reason": "remote_compute_status_missing", "source": source}

    def _trade_readiness_snapshot(self) -> dict:
        if not self._running:
            return {"open": False, "reason": "runtime_stopped"}
        if not self.session_keeper.is_authenticated:
            return {"open": False, "reason": "session_unauthenticated"}
        state = self._copy_warmup_state()
        slim_enabled = getattr(self, "_runtime_slim_mode_enabled", None)
        if callable(slim_enabled) and slim_enabled() and state.get("trading_gate_open"):
            return {
                "open": True,
                "reason": str(state.get("trading_gate_reason") or "runtime_slim_mode"),
                "phase": state.get("phase"),
                "source": "runtime_slim_mode",
            }
        current = self._trade_readiness_from_current_compute(state)
        if current and current.get("open"):
            return {
                "open": True,
                "reason": str(current.get("reason") or "ready"),
                "phase": state.get("phase"),
                "source": str(current.get("source") or "current_readiness"),
            }
        if current and str(current.get("reason") or "") in {
            "missing_trade_symbols",
            "no_trade_symbols",
            "remote_compute_status_unavailable",
            "remote_compute_status_missing",
        }:
            return {
                "open": False,
                "reason": str(current.get("reason") or "warmup_incomplete"),
                "phase": state.get("phase"),
                "source": str(current.get("source") or "current_readiness"),
                "symbols": list(current.get("symbols") or []),
            }
        if state.get("trading_gate_open"):
            return {
                "open": True,
                "reason": str(state.get("trading_gate_reason") or "ready"),
                "phase": state.get("phase"),
            }
        return {
            "open": False,
            "reason": str(state.get("trading_gate_reason") or "warmup_incomplete"),
            "phase": state.get("phase"),
        }

    def _close_warmup_gate(self, reason: str):
        snapshot = self._warmup_snapshot_from_subscriptions()
        phase = "blocked" if snapshot["symbols_total"] else "idle"
        self._set_warmup_state(
            phase=phase,
            reason=reason,
            trading_gate_open=False,
            trading_gate_reason=reason,
            **self._warmup_scope_fields(snapshot),
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            timings={},
            last_duration_s=0.0,
        )
        if self._running and reason in {"session_unauthenticated", "gateway_down"}:
            conclusion = "启动线程已就绪，但当前认证中断，运行态已降级等待恢复。"
            if reason == "gateway_down":
                conclusion = "启动线程已就绪，但当前 Gateway 中断，运行态已降级等待恢复。"
            self._release_startup_gate(
                reason=reason,
                title="IBKR Runtime 启动态已解除（等待恢复）",
                detail={
                    "状态结论": conclusion,
                    "Warmup阶段": phase,
                    "交易门": "closed",
                },
            )

    def _schedule_warmup(self, reason: str = "subscriptions_changed", force: bool = False) -> bool:
        service_mod = _service_mod()
        snapshot = self._warmup_snapshot_from_subscriptions()
        if snapshot["symbols_total"] == 0:
            self._set_warmup_state(
                phase="idle",
                reason=reason,
                requested_at=self._now_iso(),
                started_at=None,
                finished_at=self._now_iso(),
                last_error="",
                trading_gate_open=False,
                trading_gate_reason="no_active_symbols",
                **self._warmup_scope_fields(snapshot),
                ready_symbols=0,
                ready_symbols_list=[],
                pending_symbols=[],
                symbol_status=[],
                integrity_pending_symbols=[],
                integrity_repair_reasons={},
                preflight_repair={},
                backfill_written=0,
                backfill_result={},
                compute_result={},
                timings={},
                last_duration_s=0.0,
            )
            return False

        with self._warmup_lock:
            same_signature = self._warmup_signature == snapshot["signature"]
            current_phase = str(self._warmup_state.get("phase") or "")
            current_state = self._copy_warmup_state(self._warmup_state)
            pending_symbols = list(self._warmup_state.get("pending_symbols") or [])
            if (
                not force
                and same_signature
                and (
                    current_phase in {"pending", "running"}
                    or (current_phase == "ready" and not pending_symbols)
                )
            ):
                return False
            self._warmup_signature = snapshot["signature"]
            queued_refresh = current_phase in {"pending", "running"}

        if queued_refresh:
            ready_symbols = self._normalize_symbol_list(current_state.get("ready_symbols_list") or [])
            ready_set = set(ready_symbols)
            queued_pending_symbols = self._normalize_symbol_list(
                list(current_state.get("pending_symbols") or [])
                + [symbol for symbol in snapshot["symbols"] if symbol not in ready_set]
            )
            self._set_warmup_state(
                phase=current_phase,
                reason=reason,
                requested_at=self._now_iso(),
                last_error="",
                pending_symbols=queued_pending_symbols,
                **self._warmup_scope_fields(snapshot),
            )
            self._warmup_wakeup.set()
            service_mod.logger.info(
                "Warmup refresh queued (%s): phase=%s symbols=%d trade=%d monitor=%d",
                reason,
                current_phase,
                snapshot["symbols_total"],
                snapshot["trade_symbols_total"],
                snapshot["monitor_symbols_total"],
            )
            return True

        self._set_warmup_state(
            phase="pending",
            reason=reason,
            requested_at=self._now_iso(),
            started_at=None,
            finished_at=None,
            last_error="",
            trading_gate_open=False,
            trading_gate_reason="warmup_pending",
            **self._warmup_scope_fields(snapshot),
            ready_symbols_list=[],
            pending_symbols=snapshot["symbols"],
            symbol_status=[],
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            backfill_written=0,
            backfill_result={},
            compute_result={},
            timings={},
            last_duration_s=0.0,
        )
        self._warmup_wakeup.set()
        service_mod.logger.info(
            "Warmup scheduled (%s): symbols=%d trade=%d monitor=%d",
            reason,
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
        )
        return True

    def _sync_session_transition(self):
        authenticated = bool(self.session_keeper.is_authenticated)
        if authenticated and not self._last_session_authenticated:
            previous_kind = self._last_session_issue_kind
            self._last_session_authenticated = True
            self._mark_auth_recovered(source="session_transition", reason=previous_kind or "session_restored")
            _service_mod().logger.info("IBKR session restored; scheduling warmup refresh")
            self._notify_session_recovered(previous_kind)
            self._schedule_warmup(reason="session_restored", force=True)
            try:
                self._force_resubscribe_active_market_data(reason="session_restored")
            except Exception:
                _service_mod().logger.warning("Session restore market data resubscribe failed", exc_info=True)
        elif not authenticated and self._last_session_authenticated:
            self._last_session_authenticated = False
            self._close_warmup_gate("session_unauthenticated")
            self._start_auth_recovery(
                interruption_kind="runtime_unauthenticated",
                recovery_reason="session_unauthenticated",
                source="session_transition",
            )
            self._notify_session_issue(
                "runtime_unauthenticated",
                "IBKR Runtime 未认证",
                "检测到运行态已降为未认证，实时行情和交易链路可能不可用。",
                "请立即检查 Gateway 与飞书 2FA 状态，并在需要时重新触发验证。",
            )

    def _warmup_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Runtime warmup loop started")
        while self._running:
            triggered = self._warmup_wakeup.wait(timeout=1)
            if not self._running:
                break
            if not triggered:
                continue
            self._warmup_wakeup.clear()
            try:
                self._run_warmup_cycle()
            except Exception as exc:
                service_mod.logger.error("Warmup loop error: %s", exc)
                self._set_warmup_state(
                    phase="failed",
                    finished_at=self._now_iso(),
                    last_error=str(exc),
                    trading_gate_open=False,
                    trading_gate_reason="warmup_failed",
                )

    def _is_warmup_active(self) -> bool:
        phase = str(self._warmup_state.get("phase") or "").strip().lower()
        return phase in {"pending", "running"}
