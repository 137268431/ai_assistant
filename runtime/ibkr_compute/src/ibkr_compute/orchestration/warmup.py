from __future__ import annotations

import time


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
        if not self.config.get_bool_for_environment("ibkr_market_ws_enabled", service_mod.ENVIRONMENT, True):
            return []
        raw_value = self.config.get_for_environment(
            "ibkr_market_ws_symbols",
            service_mod.ENVIRONMENT,
            ",".join(service_mod.DEFAULT_MARKET_WS_SYMBOLS),
        )
        return self._normalize_symbol_list(str(raw_value or "").replace("\n", ",").split(","))

    def _market_ws_symbols(self) -> list[str]:
        return self._normalize_symbol_list(
            list(self._watchlist_monitor_symbols) + list(self._configured_market_ws_symbols())
        )

    def _live_warmup_days(self) -> int:
        service_mod = _service_mod()
        return max(1, self.config.get_int_for_environment("ibkr_live_warmup_days", service_mod.ENVIRONMENT, 14))

    def _restart_overlap_days(self) -> int:
        service_mod = _service_mod()
        return max(1, self.config.get_int_for_environment("ibkr_restart_overlap_days", service_mod.ENVIRONMENT, 1))

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
            ready_symbols_list = self._normalize_symbol_list(next_state.get("ready_symbols_list") or [])
            ready_set = set(ready_symbols_list)
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
            next_state["ready_symbols"] = len(ready_symbols_list)
            next_state["ready_scan_symbols"] = len([symbol for symbol in scan_symbols if symbol in ready_set])
            next_state["ready_subscription_symbols"] = len([symbol for symbol in subscription_symbols if symbol in ready_set])
            next_state["ready_trade_symbols"] = len([symbol for symbol in trade_symbols if symbol in ready_set])
            next_state["ready_monitor_symbols"] = len([symbol for symbol in monitor_symbols if symbol in ready_set])
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
        conid_map = {
            symbol: int(active_conid_map.get(symbol) or 0)
            for symbol in symbols
            if int(active_conid_map.get(symbol) or 0) > 0
        }
        unresolved = [symbol for symbol in symbols if symbol not in conid_map]
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

    def _trade_readiness_snapshot(self) -> dict:
        if not self._running:
            return {"open": False, "reason": "runtime_stopped"}
        if not self.session_keeper.is_authenticated:
            return {"open": False, "reason": "session_unauthenticated"}
        state = self._copy_warmup_state()
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

    def _collect_warmup_readiness(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        from ibkr_compute.api import server as compute_server

        ready_symbols = []
        pending_symbols = []
        symbol_status = []
        ready_set = set()
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot["trade_symbols"])
        monitor_symbol_set = set(snapshot["monitor_symbols"])

        for symbol in snapshot["symbols"]:
            engine = compute_server.engines.get((service_mod.ENVIRONMENT, symbol, service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL))
            is_ready = bool(engine and engine.is_ready())
            bar_count = int(getattr(engine, "bar_count", 0) or 0) if engine else 0
            last_bar_time_ms = int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0
            if symbol in trade_symbol_set:
                role = "trade"
            elif symbol in monitor_symbol_set:
                role = "monitor"
            elif symbol in scan_symbol_set:
                role = "scan"
            elif symbol in subscription_symbol_set:
                role = "subscription"
            else:
                role = "data"
            symbol_status.append(
                {
                    "symbol": symbol,
                    "role": role,
                    "ready": is_ready,
                    "bar_count": bar_count,
                    "last_bar_time_ms": last_bar_time_ms,
                }
            )
            if is_ready:
                ready_symbols.append(symbol)
                ready_set.add(symbol)
            else:
                pending_symbols.append(symbol)

        ready_scan_symbols = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        ready_subscription_symbols = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
        ready_trade_symbols = len([symbol for symbol in snapshot["trade_symbols"] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot["monitor_symbols"] if symbol in ready_set])
        trading_gate_open = bool(snapshot["trade_symbols"]) and ready_trade_symbols == snapshot["trade_symbols_total"]
        blocking_pending_symbols = self._non_monitor_pending_symbols(pending_symbols, snapshot["monitor_symbols"])
        return {
            "phase": "ready" if not blocking_pending_symbols else "degraded",
            "required_interval": service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
            "ready_symbols": len(ready_symbols),
            "ready_scan_symbols": ready_scan_symbols,
            "ready_subscription_symbols": ready_subscription_symbols,
            "ready_trade_symbols": ready_trade_symbols,
            "ready_monitor_symbols": ready_monitor_symbols,
            "ready_symbols_list": ready_symbols,
            "pending_symbols": pending_symbols,
            "symbol_status": symbol_status,
            "trading_gate_open": trading_gate_open,
            "trading_gate_reason": "ready" if trading_gate_open else ("no_trade_symbols" if not snapshot["trade_symbols"] else "warmup_incomplete"),
        }

    def _apply_integrity_readiness(self, readiness: dict, snapshot: dict, repair_plan: dict[str, dict] | None = None) -> dict:
        plan = repair_plan or {}
        if not plan:
            readiness["integrity_pending_symbols"] = []
            readiness["integrity_repair_reasons"] = {}
            for item in readiness.get("symbol_status") or []:
                if isinstance(item, dict):
                    item["integrity_ready"] = True
                    item["integrity_reason"] = ""
            return readiness

        blocking_symbols = sorted(plan.keys())
        repair_reasons = {
            symbol: str((plan.get(symbol) or {}).get("repair_reason") or "history_repair_pending")
            for symbol in blocking_symbols
        }
        ready_set = set(readiness.get("ready_symbols_list") or []) - set(blocking_symbols)
        pending_set = set(readiness.get("pending_symbols") or []) | set(blocking_symbols)
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot.get("trade_symbols") or [])
        monitor_symbol_set = set(snapshot.get("monitor_symbols") or [])

        status_map = {
            str((item or {}).get("symbol") or "").upper(): dict(item or {})
            for item in (readiness.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        merged_status = []
        for symbol in snapshot.get("symbols") or []:
            if symbol in trade_symbol_set:
                role = "trade"
            elif symbol in monitor_symbol_set:
                role = "monitor"
            elif symbol in scan_symbol_set:
                role = "scan"
            elif symbol in subscription_symbol_set:
                role = "subscription"
            else:
                role = "data"
            row = dict(status_map.get(symbol) or {})
            row["symbol"] = symbol
            row["role"] = row.get("role") or role
            row["integrity_ready"] = symbol not in repair_reasons
            row["integrity_reason"] = repair_reasons.get(symbol, "")
            if symbol in repair_reasons:
                row["ready"] = False
            merged_status.append(row)

        ready_trade_symbols = len([symbol for symbol in snapshot.get("trade_symbols") or [] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot.get("monitor_symbols") or [] if symbol in ready_set])
        trade_blocked = any(symbol in trade_symbol_set for symbol in blocking_symbols)
        trading_gate_open = (
            bool(snapshot.get("trade_symbols"))
            and ready_trade_symbols == int(snapshot.get("trade_symbols_total", 0) or 0)
            and not trade_blocked
        )
        blocking_pending_symbols = self._non_monitor_pending_symbols(sorted(pending_set), snapshot.get("monitor_symbols") or [])

        readiness["phase"] = "ready" if not blocking_pending_symbols else "degraded"
        readiness["ready_symbols"] = len(ready_set)
        readiness["ready_scan_symbols"] = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        readiness["ready_subscription_symbols"] = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
        readiness["ready_trade_symbols"] = ready_trade_symbols
        readiness["ready_monitor_symbols"] = ready_monitor_symbols
        readiness["ready_symbols_list"] = sorted(ready_set)
        readiness["pending_symbols"] = sorted(pending_set)
        readiness["symbol_status"] = merged_status
        readiness["integrity_pending_symbols"] = blocking_symbols
        readiness["integrity_repair_reasons"] = repair_reasons
        readiness["trading_gate_open"] = trading_gate_open
        if trading_gate_open:
            readiness["trading_gate_reason"] = "ready"
        elif trade_blocked:
            readiness["trading_gate_reason"] = "history_repair_pending"
        elif not snapshot.get("trade_symbols"):
            readiness["trading_gate_reason"] = "no_trade_symbols"
        else:
            readiness["trading_gate_reason"] = "warmup_incomplete"
        return readiness

    def _run_warmup_preflight_repairs(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        repair_plan = self._build_startup_history_repair_plan(snapshot.get("symbols") or [])
        period_overrides = self._build_startup_history_period_overrides(repair_plan)
        if not repair_plan:
            return {
                "initial_repair_symbols": [],
                "attempted_repair_symbols": [],
                "remaining_repair_symbols": [],
                "repair_reasons": {},
                "history_fetch_symbols": [],
                "history_period_overrides": {},
                "history_written_total": 0,
                "repair_result": {},
            }

        service_mod.logger.info(
            "Warmup preflight history repair started: symbols=%s short_window=%s",
            ",".join(sorted(repair_plan.keys())),
            ",".join(
                f"{symbol}:{(period_overrides.get(symbol) or {}).get('5m')}"
                for symbol in sorted(period_overrides.keys())
            ) or "none",
        )
        repair_result = self._run_bar_integrity_repairs(
            repair_plan,
            source="warmup_preflight",
            allow_defer=False,
            run_pipeline_repair=False,
            history_period_overrides=period_overrides,
        )
        remaining_plan = self._build_startup_history_repair_plan(snapshot.get("symbols") or [])
        history_written_total = 0
        for item in (repair_result.get("per_symbol") or {}).values():
            result = (item or {}).get("result") or {}
            history_written_total += int(result.get("history_written", 0) or 0)
        return {
            "initial_repair_symbols": sorted(repair_plan.keys()),
            "attempted_repair_symbols": sorted(repair_result.get("repair_symbols") or []),
            "remaining_repair_symbols": sorted(remaining_plan.keys()),
            "repair_reasons": {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in remaining_plan.items()
            },
            "history_fetch_symbols": sorted(repair_result.get("history_symbols") or []),
            "history_period_overrides": {
                symbol: dict((period_overrides.get(symbol) or {}))
                for symbol in sorted(period_overrides.keys())
            },
            "history_written_total": history_written_total,
            "repair_result": repair_result,
        }

    def _is_warmup_active(self) -> bool:
        phase = str(self._warmup_state.get("phase") or "").strip().lower()
        return phase in {"pending", "running"}
