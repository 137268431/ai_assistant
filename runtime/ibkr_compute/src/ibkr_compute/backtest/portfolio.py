from __future__ import annotations

from .runtime_support import *


class BacktestPortfolioMixin:
    def _compact_portfolio_bar(self, bar: dict, *, is_last_bar: bool, next_day: str) -> tuple:
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        return (
            bar_ms,
            float(bar.get("open", 0) or 0),
            float(bar.get("high", 0) or 0),
            float(bar.get("low", 0) or 0),
            float(bar.get("close", 0) or 0),
            float(bar.get("volume", 0) or 0),
            str(bar.get("session_type", "") or classify_session(bar_time_ms=bar_ms)),
            str(bar.get("exchange", "") or "").upper(),
            bool(is_last_bar),
            str(next_day or "")[:10],
        )

    def _inflate_portfolio_bar(self, symbol: str, compact_bar: tuple) -> dict:
        bar_ms = int(compact_bar[0] or 0)
        return {
            "symbol": symbol,
            "exchange": str(compact_bar[7] or "").upper(),
            "interval": "5m",
            "open": float(compact_bar[1] or 0),
            "high": float(compact_bar[2] or 0),
            "low": float(compact_bar[3] or 0),
            "close": float(compact_bar[4] or 0),
            "volume": float(compact_bar[5] or 0),
            "session_type": str(compact_bar[6] or ""),
            "us_time": format_us_time(bar_ms),
            "cn_time": format_cn_time(bar_ms),
            "bar_time_ms": bar_ms,
            "_backtest_is_last_bar": bool(compact_bar[8]),
            "_backtest_next_day": str(compact_bar[9] or "")[:10],
        }

    def _parse_hhmm_tuple(self, raw_value: Any, default: str) -> tuple[int, int]:
        text = str(raw_value or default or "00:00").strip()
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        hour_text, minute_text = str(default or "00:00").split(":", 1)
        return int(hour_text), int(minute_text)

    def _portfolio_bar_time_tuple(self, bar_or_ms: dict | int) -> tuple[int, int]:
        if isinstance(bar_or_ms, dict):
            bar_ms = int(bar_or_ms.get("bar_time_ms", 0) or 0)
        else:
            bar_ms = int(bar_or_ms or 0)
        et_time = ms_to_et(bar_ms) if bar_ms > 0 else datetime.now(ET)
        return et_time.hour, et_time.minute

    def _portfolio_bar_in_trade_window(self, bar_or_ms: dict | int, request: dict) -> bool:
        current = self._portfolio_bar_time_tuple(bar_or_ms)
        start = self._parse_hhmm_tuple(request.get("trade_window_start_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_START)
        end = self._parse_hhmm_tuple(request.get("trade_window_end_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_END)
        return start <= current <= end

    def _portfolio_bar_in_order_window(self, bar_or_ms: dict | int, request: dict) -> bool:
        current = self._portfolio_bar_time_tuple(bar_or_ms)
        start = self._parse_hhmm_tuple(request.get("trade_window_start_time"), DEFAULT_PORTFOLIO_TRADE_WINDOW_START)
        end = self._parse_hhmm_tuple(request.get("order_window_end_time"), DEFAULT_PORTFOLIO_ORDER_WINDOW_END)
        return start <= current <= end

    def _portfolio_consecutive_stop_loss_limit(self, request: dict) -> int:
        try:
            value = int(request.get("consecutive_stop_loss_limit", DEFAULT_PORTFOLIO_CONSECUTIVE_STOP_LOSS_LIMIT))
        except Exception:
            value = DEFAULT_PORTFOLIO_CONSECUTIVE_STOP_LOSS_LIMIT
        return max(1, value)

    def _portfolio_sl_circuit_breaker_active(self, ledger: dict, request: dict) -> bool:
        return int(ledger.get("consecutive_stop_loss_count", 0) or 0) >= self._portfolio_consecutive_stop_loss_limit(request)

    def _read_backtest_resource_snapshot(self) -> dict:
        load1 = 0.0
        try:
            load1 = float(os.getloadavg()[0])
        except Exception:
            load1 = 0.0

        rss_mb = 0.0
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        rss_mb = float(line.split()[1]) / 1024.0
                        break
        except Exception:
            rss_mb = 0.0

        available_mb = 0.0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        available_mb = float(line.split()[1]) / 1024.0
                        break
        except Exception:
            available_mb = 0.0

        return {
            "load1": round(load1, 2),
            "rss_mb": round(rss_mb, 1),
            "available_mb": round(available_mb, 1),
        }

    def _resource_guard_reasons(self, request: dict, snapshot: dict) -> list[str]:
        reasons: list[str] = []
        max_load = self._coerce_float_value(
            request.get("resource_guard_max_load"),
            DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD,
        )
        min_available = self._coerce_float_value(
            request.get("resource_guard_min_available_mb"),
            float(DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB),
        )
        max_rss = self._coerce_float_value(
            request.get("resource_guard_max_rss_mb"),
            float(DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB),
        )
        load1 = float(snapshot.get("load1", 0) or 0)
        available_mb = float(snapshot.get("available_mb", 0) or 0)
        rss_mb = float(snapshot.get("rss_mb", 0) or 0)
        if max_load > 0 and load1 > max_load:
            reasons.append(f"load1={load1:.2f}>{max_load:.2f}")
        if min_available > 0 and available_mb > 0 and available_mb < min_available:
            reasons.append(f"mem_available={available_mb:.0f}MB<{min_available:.0f}MB")
        if max_rss > 0 and rss_mb > max_rss:
            reasons.append(f"rss={rss_mb:.0f}MB>{max_rss:.0f}MB")
        return reasons

    def _maybe_throttle_backtest_resources(
        self,
        request: dict,
        *,
        step_index: int,
        total_steps: int,
        progress_context: dict | None = None,
    ) -> bool:
        if not self._normalize_bool(
            request.get("resource_guard_enabled"),
            DEFAULT_BACKTEST_RESOURCE_GUARD_ENABLED,
        ):
            return False
        check_steps = max(1, int(request.get("resource_guard_check_steps") or DEFAULT_BACKTEST_RESOURCE_GUARD_CHECK_STEPS))
        if int(step_index or 0) % check_steps != 0:
            return False

        snapshot = self._read_backtest_resource_snapshot()
        reasons = self._resource_guard_reasons(request, snapshot)
        if not reasons:
            return False

        gc.collect()
        base_sleep_s = self._coerce_float_value(
            request.get("resource_guard_sleep_s"),
            DEFAULT_BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS,
        )
        sleep_s = max(0.1, min(30.0, base_sleep_s))
        max_load = self._coerce_float_value(request.get("resource_guard_max_load"), DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD)
        min_available = self._coerce_float_value(
            request.get("resource_guard_min_available_mb"),
            float(DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB),
        )
        max_rss = self._coerce_float_value(request.get("resource_guard_max_rss_mb"), float(DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB))
        if (
            (max_load > 0 and float(snapshot.get("load1", 0) or 0) > max_load * 1.5)
            or (min_available > 0 and 0 < float(snapshot.get("available_mb", 0) or 0) < min_available * 0.75)
            or (max_rss > 0 and float(snapshot.get("rss_mb", 0) or 0) > max_rss * 1.25)
        ):
            sleep_s = min(30.0, sleep_s * 3)

        progress_value = 25 + int((max(0, int(step_index or 0)) / max(1, int(total_steps or 1))) * 60)
        self._set_progress_context(
            "running",
            "streaming",
            f"throttling portfolio backtest: {', '.join(reasons)}",
            progress_value,
            progress_context,
        )
        deadline = time.monotonic() + sleep_s
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                return True
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        return True

    def _portfolio_signal_expired(self, signal: dict, bar_or_ms: dict | int, request: dict) -> bool:
        if isinstance(bar_or_ms, dict):
            bar_ms = int(bar_or_ms.get("bar_time_ms", 0) or 0)
        else:
            bar_ms = int(bar_or_ms or 0)
        signal_ms = int(signal.get("signal_bar_ms", signal.get("bar_time_ms", 0)) or 0)
        if signal_ms <= 0 or bar_ms <= 0:
            return False
        validity_ms = self._portfolio_signal_validity_minutes(signal, request) * 60 * 1000
        return bar_ms - signal_ms > validity_ms

    def _portfolio_signal_validity_minutes(self, signal: dict, request: dict) -> int:
        request_default = int(
            request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES)
            or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES
        )
        extra = self._parse_object((signal or {}).get("extra"))
        raw = extra.get("validity_minutes", (signal or {}).get("validity_minutes"))
        try:
            minutes = int(raw)
            if minutes > 0:
                return minutes
        except Exception:
            pass
        return request_default

    def _max_strategy_open_positions(self, request: dict) -> int:
        try:
            return max(
                0,
                int(
                    request.get(
                        "max_strategy_open_positions",
                        DEFAULT_MAX_STRATEGY_OPEN_POSITIONS,
                    )
                    or 0
                ),
            )
        except Exception:
            return DEFAULT_MAX_STRATEGY_OPEN_POSITIONS

    def _portfolio_strategy_slot_count(self, ledger: dict) -> int:
        return max(0, int(ledger.get("open_position_slots", 0) or 0)) + max(
            0,
            int(ledger.get("pending_entry_slots", 0) or 0),
        )

    def _portfolio_capacity_details(self, ledger: dict, request: dict) -> dict:
        open_slots = max(0, int(ledger.get("open_position_slots", 0) or 0))
        pending_slots = max(0, int(ledger.get("pending_entry_slots", 0) or 0))
        max_slots = self._max_strategy_open_positions(request)
        return {
            "max_strategy_open_positions": max_slots,
            "strategy_open_position_slots": open_slots,
            "strategy_pending_entry_slots": pending_slots,
            "strategy_used_slots": open_slots + pending_slots,
        }

    def _portfolio_has_strategy_capacity(self, ledger: dict, request: dict) -> bool:
        max_slots = self._max_strategy_open_positions(request)
        return max_slots <= 0 or self._portfolio_strategy_slot_count(ledger) < max_slots

    def _mark_capacity_wait_expired(
        self,
        ledger: dict,
        candidate: dict,
        signal_index: dict,
        bar: dict,
        request: dict,
    ):
        details = {
            **self._portfolio_capacity_details(ledger, request),
            "event_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "event_us_time": str(bar.get("us_time", "") or ""),
            "event_cn_time": str(bar.get("cn_time", "") or ""),
        }
        self._mark_backtest_signal_status(
            signal_index,
            candidate.get("signal_id"),
            "dropped",
            "capacity_wait_expired",
            details,
        )
        self._portfolio_record_rejection(ledger, "capacity_wait_expired")
        self._portfolio_record_candidate_sample(ledger, candidate, "dropped", "capacity_wait_expired", details)

    def _cooldown_bars_to_ms(self, bars: int) -> int:
        return max(0, int(bars or 0)) * interval_to_ms("5m")

    def _backtest_cooldown_active(self, state: dict, bar: dict) -> tuple[bool, str]:
        until_ms = int(state.get("cooldown_until_ms", 0) or 0)
        bar_ms = int(bar.get("bar_time_ms", 0) or 0)
        if until_ms <= 0 or bar_ms >= until_ms:
            if until_ms > 0 and bar_ms >= until_ms:
                state["cooldown_until_ms"] = 0
                state["cooldown_reason"] = ""
            return False, ""
        return True, str(state.get("cooldown_reason") or "cooldown_active")

    def _start_backtest_cooldown(self, state: dict, bar: dict, bars: int, reason: str):
        duration_ms = self._cooldown_bars_to_ms(bars)
        if duration_ms <= 0:
            return
        state["cooldown_until_ms"] = int(bar.get("bar_time_ms", 0) or 0) + duration_ms
        state["cooldown_reason"] = str(reason or "cooldown_active").strip() or "cooldown_active"

    def _backtest_intraday_harvest_settings(self, request: dict) -> dict:
        enabled = self._normalize_bool(request.get("intraday_harvest_enabled"), False)
        raw_settings = request.get("intraday_harvest_settings")
        settings = normalize_harvest_settings(raw_settings if isinstance(raw_settings, dict) else {})
        settings["enabled"] = enabled
        settings["live_auto_enabled"] = enabled
        return settings

    @staticmethod
    def _backtest_stop_tightens(direction: str, old_stop: float, new_stop: float) -> bool:
        if new_stop <= 0:
            return False
        if old_stop <= 0:
            return True
        if str(direction or "").strip().lower() == "short":
            return new_stop < old_stop - 0.005
        return new_stop > old_stop + 0.005

    def _maybe_apply_backtest_intraday_harvest(
        self,
        position: dict | None,
        snapshot: dict,
        request: dict,
    ) -> dict | None:
        if not position:
            return position
        settings = self._backtest_intraday_harvest_settings(request)
        if not settings.get("enabled"):
            return position
        state = dict(position.get("harvest_state") or {})
        decision = evaluate_intraday_harvest(position, snapshot, state=state, settings=settings)
        position["harvest_state"] = dict(decision.get("state") or state)
        position["last_intraday_harvest_decision"] = {
            "action": str(decision.get("action") or ""),
            "reason": str(decision.get("reason") or ""),
            "score": int(decision.get("score") or 0),
            "reasons": list(decision.get("reasons") or []),
            "progress_r": decision.get("progress_r"),
            "mfe_r": decision.get("mfe_r"),
            "bar_time_ms": int(snapshot.get("bar_time_ms", 0) or 0),
        }
        action = str(decision.get("action") or "")
        if action == ACTION_TIGHTEN_STOP:
            new_sl = self._coerce_float_value(decision.get("stop_price"), 0.0)
            old_sl = float(position.get("stop_price", 0) or 0)
            if self._backtest_stop_tightens(str(position.get("direction") or ""), old_sl, new_sl):
                position["stop_price"] = float(new_sl)
                position["harvest_stop_adjust_count"] = int(position.get("harvest_stop_adjust_count", 0) or 0) + 1
                self._append_backtest_risk_adjustment(
                    position,
                    {
                        "event_type": "intraday_harvest_tighten_stop",
                        "source": "intraday_harvest",
                        "bar_time_ms": int(snapshot.get("bar_time_ms", 0) or 0),
                        "us_time": str(snapshot.get("us_time", "") or ""),
                        "cn_time": str(snapshot.get("cn_time", "") or ""),
                        "old_sl": round(old_sl, 4),
                        "new_sl": round(new_sl, 4),
                        "reason": str(decision.get("reason") or "intraday_harvest_tighten_stop"),
                        "progress_r": round(float(decision.get("progress_r", 0) or 0), 4),
                        "mfe_r": round(float(decision.get("mfe_r", 0) or 0), 4),
                    },
                )
        elif action == ACTION_FULL_EXIT:
            position["backtest_harvest_force_exit"] = dict(decision)
        return position

    def _maybe_apply_backtest_atr_stop(
        self,
        position: dict | None,
        snapshot: dict,
        request: dict,
    ) -> dict | None:
        if not position:
            return position
        if not self._normalize_bool(request.get("atr_dynamic_stop_enabled"), True):
            return self._maybe_apply_backtest_intraday_harvest(position, snapshot, request)
        current_price = self._coerce_float_value(snapshot.get("close"), 0.0)
        current_atr = self._coerce_float_value(snapshot.get("atr"), 0.0)
        if is_signal_mode_adaptive_exit_profile(position.get("exit_policy_profile")):
            target_result = compute_exit_policy_target_update(
                position,
                current_price=current_price,
                bar_high=self._coerce_float_value(snapshot.get("high"), current_price),
                bar_low=self._coerce_float_value(snapshot.get("low"), current_price),
                min_change=self._coerce_float_value(request.get("atr_stop_min_change"), 0.01),
            )
            if target_result.get("target_state"):
                position["target_state"] = dict(target_result.get("target_state") or {})
            if target_result.get("should_update_stop"):
                old_sl = float(position.get("stop_price", 0) or 0)
                position["stop_price"] = float(target_result["new_sl"])
                position["target_stop_adjust_count"] = int(position.get("target_stop_adjust_count", 0) or 0) + 1
                position["last_target_policy_update"] = target_result
                self._append_backtest_risk_adjustment(
                    position,
                    {
                        "event_type": "target_policy_stop_adjust",
                        "source": "exit_policy",
                        "bar_time_ms": int(snapshot.get("bar_time_ms", 0) or 0),
                        "us_time": str(snapshot.get("us_time", "") or ""),
                        "cn_time": str(snapshot.get("cn_time", "") or ""),
                        "old_sl": round(old_sl, 4),
                        "new_sl": round(float(target_result.get("new_sl", 0) or 0), 4),
                        "current_price": round(current_price, 4),
                        "current_atr": round(current_atr, 4),
                        "reason": str(target_result.get("reason", "") or "target_policy_stop_adjust"),
                        "mfe_r": round(float(target_result.get("mfe_r", 0) or 0), 4),
                    },
                )
            result = compute_exit_policy_stop_update(
                position,
                current_price=current_price,
                current_atr=current_atr,
                bar_high=self._coerce_float_value(snapshot.get("high"), current_price),
                bar_low=self._coerce_float_value(snapshot.get("low"), current_price),
                min_change=self._coerce_float_value(request.get("atr_stop_min_change"), 0.01),
            )
            if result.get("trail_state"):
                position["trail_state"] = dict(result.get("trail_state") or {})
        else:
            result = compute_atr_tightened_stop(
                position,
                current_price=current_price,
                current_atr=current_atr,
                sl_atr_mult=self._coerce_float_value((request.get("params") or {}).get("strategy_params", {}).get("sl_atr_mult"), DEFAULT_PARAMS["sl_atr_mult"]),
                min_profit_r=self._coerce_float_value(request.get("atr_stop_min_profit_r"), 0.3),
                deviation_threshold=self._coerce_float_value(request.get("atr_stop_deviation_threshold"), 0.30),
                min_change=self._coerce_float_value(request.get("atr_stop_min_change"), 0.01),
            )
        if not result.get("should_update"):
            return self._maybe_apply_backtest_intraday_harvest(position, snapshot, request)
        old_sl = float(position.get("stop_price", 0) or 0)
        position["stop_price"] = float(result["new_sl"])
        position["last_stop_atr"] = float(result.get("current_atr", current_atr) or current_atr)
        position["atr_stop_adjust_count"] = int(position.get("atr_stop_adjust_count", 0) or 0) + 1
        position["last_atr_stop_adjust"] = result
        self._append_backtest_risk_adjustment(
            position,
            {
                "event_type": "exit_policy_stop_adjust" if is_signal_mode_adaptive_exit_profile(position.get("exit_policy_profile")) else "atr_stop_adjust",
                "source": "atr_dynamic_stop",
                "bar_time_ms": int(snapshot.get("bar_time_ms", 0) or 0),
                "us_time": str(snapshot.get("us_time", "") or ""),
                "cn_time": str(snapshot.get("cn_time", "") or ""),
                "old_sl": round(old_sl, 4),
                "new_sl": round(float(result.get("new_sl", 0) or 0), 4),
                "current_price": round(current_price, 4),
                "current_atr": round(current_atr, 4),
                "reason": str(result.get("reason", "") or "atr_tighten_stop"),
                "progress_r": round(float(result.get("progress_r", 0) or 0), 4),
                "atr_deviation": round(float(result.get("atr_deviation", 0) or 0), 4),
            },
        )
        return self._maybe_apply_backtest_intraday_harvest(position, snapshot, request)

    def _maybe_close_backtest_exit_policy_time_stop(
        self,
        position: dict | None,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> dict | None:
        if not position or not is_signal_mode_adaptive_exit_profile(position.get("exit_policy_profile")):
            return None
        result = compute_exit_policy_time_exit(position)
        if not result.get("should_exit"):
            return None
        trade = self._close_position(
            position,
            bar,
            commission_per_share,
            slippage_bps,
            str(result.get("reason") or "exit_policy_time_stop"),
            execution_profile,
        )
        extra = self._parse_object(trade.get("extra"))
        extra["exit_policy_time_stop"] = result
        trade["extra"] = extra
        return trade

    def _maybe_close_backtest_intraday_harvest(
        self,
        position: dict | None,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> dict | None:
        if not position:
            return None
        decision = position.pop("backtest_harvest_force_exit", None)
        if not isinstance(decision, dict) or not decision:
            return None
        trade = self._close_position(
            position,
            bar,
            commission_per_share,
            slippage_bps,
            "intraday_harvest_full_exit",
            execution_profile,
        )
        extra = self._parse_object(trade.get("extra"))
        extra["intraday_harvest_decision"] = decision
        trade["extra"] = extra
        return trade

    def _coerce_float_value(self, value: Any, default: float = 0.0) -> float:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip().replace(",", "")
        if not text:
            return default
        try:
            return float(text)
        except Exception:
            return default

    def _resolve_account_buying_power_snapshot(self, environment: str = "live") -> dict:
        if not callable(self.account_snapshot_provider):
            return {
                "ok": False,
                "reason": "account_snapshot_provider_unavailable",
                "snapshot": {},
                "buying_power": 0.0,
            }
        try:
            try:
                snapshot = self.account_snapshot_provider(environment) or {}
            except TypeError:
                snapshot = self.account_snapshot_provider() or {}
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"account_snapshot_error: {exc}",
                "snapshot": {},
                "buying_power": 0.0,
            }
        summary = snapshot.get("summary") if isinstance(snapshot, dict) else {}
        summary = summary if isinstance(summary, dict) else {}
        buying_power = self._coerce_float_value(summary.get("buying_power"), 0.0)
        return {
            "ok": buying_power > 0,
            "reason": "" if buying_power > 0 else "buying_power_unavailable",
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
            "buying_power": buying_power,
        }

    def _prepare_backtest_account_model(self, request: dict) -> dict:
        mode = str(request.get("account_model_mode") or DEFAULT_ACCOUNT_MODEL_MODE).strip().lower()
        if mode != "current_snapshot":
            request["account_model_status"] = {"mode": mode, "ok": True, "reason": "fixed_capital"}
            return request
        if request.get("_account_model_prepared"):
            return request

        resolved = self._resolve_account_buying_power_snapshot(request.get("source_environment") or "live")
        snapshot = resolved.get("snapshot") if isinstance(resolved.get("snapshot"), dict) else {}
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        net_liquidation = self._coerce_float_value(summary.get("net_liquidation"), 0.0)
        total_cash = self._coerce_float_value(summary.get("total_cash_value"), 0.0)
        available_funds = self._coerce_float_value(summary.get("available_funds"), 0.0)
        effective_capital = net_liquidation or total_cash or available_funds
        status = {
            "mode": mode,
            "ok": bool(resolved.get("ok") or effective_capital > 0),
            "reason": str(resolved.get("reason") or ""),
            "requested_initial_capital": round(float(request.get("initial_capital", 0) or 0), 4),
        }
        if effective_capital > 0:
            request["initial_capital"] = max(1000.0, effective_capital)
            status["effective_initial_capital"] = round(float(request["initial_capital"]), 4)
        elif not status["reason"]:
            status["reason"] = "account_equity_unavailable"

        compact_summary = {
            "account_code": str(summary.get("account_code") or ""),
            "buying_power": self._coerce_float_value(summary.get("buying_power"), 0.0),
            "available_funds": available_funds,
            "net_liquidation": net_liquidation,
            "total_cash_value": total_cash,
            "gross_position_value": self._coerce_float_value(summary.get("gross_position_value"), 0.0),
        }
        request["_account_model_snapshot"] = snapshot
        request["account_model_summary"] = compact_summary
        request["account_model_status"] = status
        request["_account_model_prepared"] = True
        return request

    def _resolve_portfolio_risk_limits(self, request: dict) -> dict:
        initial_capital = float(request.get("initial_capital", 0) or 0)
        account_model_mode = str(request.get("account_model_mode") or DEFAULT_ACCOUNT_MODEL_MODE).strip().lower()
        mode = str(request.get("borrow_limit_mode") or "none").strip().lower()
        account_snapshot = request.get("_account_model_snapshot") if isinstance(request.get("_account_model_snapshot"), dict) else {}
        account_summary = account_snapshot.get("summary") if isinstance(account_snapshot.get("summary"), dict) else {}
        if not account_summary and isinstance(request.get("account_model_summary"), dict):
            account_summary = dict(request.get("account_model_summary") or {})
        account_snapshot_ok = bool((request.get("account_model_status") or {}).get("ok"))
        account_snapshot_reason = str((request.get("account_model_status") or {}).get("reason") or "")
        if mode == "account_buying_power":
            if account_snapshot:
                buying_power = self._coerce_float_value(account_summary.get("buying_power"), 0.0)
            else:
                resolved = self._resolve_account_buying_power_snapshot(request.get("source_environment") or "live")
                account_snapshot = resolved.get("snapshot") or {}
                account_summary = account_snapshot.get("summary") if isinstance(account_snapshot.get("summary"), dict) else {}
                account_snapshot_ok = bool(resolved.get("ok"))
                account_snapshot_reason = str(resolved.get("reason") or "")
                buying_power = float(resolved.get("buying_power", 0) or 0)
            if buying_power <= 0:
                raise ValueError(f"account_buying_power_unavailable: {account_snapshot_reason or 'missing buying_power'}")
            total_exposure_limit = buying_power
            max_borrow_amount = max(0.0, buying_power - initial_capital)
        elif mode == "fixed":
            max_borrow_amount = max(0.0, float(request.get("max_borrow_amount", 0) or 0))
            total_exposure_limit = initial_capital + max_borrow_amount
        else:
            mode = "none"
            max_borrow_amount = 0.0
            total_exposure_limit = initial_capital
        return {
            "initial_capital": round(initial_capital, 4),
            "account_model_mode": account_model_mode,
            "borrow_limit_mode": mode,
            "max_borrow_amount": round(max_borrow_amount, 4),
            "total_exposure_limit": round(max(0.0, total_exposure_limit), 4),
            "account_snapshot_ok": account_snapshot_ok,
            "account_snapshot_reason": account_snapshot_reason,
            "account_summary": {
                "account_code": account_summary.get("account_code") or "",
                "account_type": account_summary.get("account_type") or "",
                "currency": account_summary.get("currency") or "",
                "buying_power": self._coerce_float_value(account_summary.get("buying_power"), 0.0),
                "available_funds": self._coerce_float_value(account_summary.get("available_funds"), 0.0),
                "net_liquidation": self._coerce_float_value(account_summary.get("net_liquidation"), 0.0),
                "gross_position_value": self._coerce_float_value(account_summary.get("gross_position_value"), 0.0),
            },
        }

    def _estimate_portfolio_entry_commission(self, signal: dict, execution_profile: dict | None = None) -> float:
        shares = max(0, int(signal.get("shares", 0) or 0))
        entry_price = float(signal.get("entry_price", signal.get("entry", 0)) or 0)
        direction = str(signal.get("direction", "") or "").strip().lower()
        if shares <= 0 or entry_price <= 0:
            return 0.0
        detail = calculate_execution_commission(
            shares=shares,
            price=entry_price,
            side=execution_side(direction, True),
            profile=execution_profile,
        )
        return max(0.0, float(detail.get("commission", 0) or 0))

    def _portfolio_signal_exposure(self, signal: dict, execution_profile: dict | None = None) -> float:
        explicit = self._coerce_float_value(signal.get("reserved_exposure"), -1.0)
        if explicit >= 0:
            return explicit
        shares = max(0, int(signal.get("shares", 0) or 0))
        entry_price = float(signal.get("entry_price", signal.get("entry", 0)) or 0)
        exposure = max(0.0, shares * entry_price)
        if execution_profile and str(execution_profile.get("fee_model") or DEFAULT_FEE_MODEL) != DEFAULT_FEE_MODEL:
            exposure += self._estimate_portfolio_entry_commission(signal, execution_profile)
        return max(0.0, exposure)

    def _portfolio_position_exposure(self, position: dict) -> float:
        explicit = self._coerce_float_value(position.get("entry_exposure"), -1.0)
        if explicit >= 0:
            return explicit
        shares = max(0, int(position.get("shares", 0) or 0))
        entry_price = float(position.get("entry_price", 0) or 0)
        return max(0.0, shares * entry_price)

    def _portfolio_projected_borrow(self, ledger: dict, additional_exposure: float = 0.0) -> float:
        equity_cash = float(ledger.get("initial_capital", 0) or 0) + float(ledger.get("realized_pnl", 0) or 0)
        projected_exposure = (
            float(ledger.get("open_exposure", 0) or 0)
            + float(ledger.get("reserved_exposure", 0) or 0)
            + float(additional_exposure or 0)
        )
        return max(0.0, projected_exposure - equity_cash)

    def _portfolio_can_reserve_exposure(self, ledger: dict, exposure: float) -> tuple[bool, str, dict]:
        requested = max(0.0, float(exposure or 0))
        projected_exposure = (
            float(ledger.get("open_exposure", 0) or 0)
            + float(ledger.get("reserved_exposure", 0) or 0)
            + requested
        )
        total_limit = float(ledger.get("total_exposure_limit", 0) or 0)
        projected_borrow = self._portfolio_projected_borrow(ledger, requested)
        max_borrow = float(ledger.get("max_borrow_amount", 0) or 0)
        details = {
            "requested_exposure": round(requested, 4),
            "projected_exposure": round(projected_exposure, 4),
            "total_exposure_limit": round(total_limit, 4),
            "projected_borrowed_amount": round(projected_borrow, 4),
            "max_borrow_amount": round(max_borrow, 4),
        }
        if total_limit > 0 and projected_exposure - total_limit > 1e-6:
            return False, "buying_power_exceeded", details
        if projected_borrow - max_borrow > 1e-6:
            return False, "borrow_limit_exceeded", details
        return True, "ok", details

    def _portfolio_record_rejection(self, ledger: dict, reason: str):
        key = str(reason or "unknown").strip() or "unknown"
        rejection_counts = ledger.setdefault("rejection_counts", {})
        rejection_counts[key] = int(rejection_counts.get(key, 0) or 0) + 1

    def _portfolio_record_candidate_sample(self, ledger: dict, candidate: dict, status: str, reason: str, details: dict | None = None):
        samples = ledger.setdefault("candidate_samples", [])
        if len(samples) >= 200:
            return
        signal_payload = candidate.get("signal_payload") or {}
        bar = candidate.get("bar") or {}
        samples.append(
            {
                "bar_time_ms": int(bar.get("bar_time_ms", signal_payload.get("bar_time_ms", 0)) or 0),
                "us_time": str(bar.get("us_time", signal_payload.get("us_time", "")) or ""),
                "symbol": str(signal_payload.get("symbol", candidate.get("symbol", "")) or ""),
                "direction": str(signal_payload.get("direction", "") or ""),
                "status": str(status or ""),
                "reason": str(reason or ""),
                "target_rank": int(candidate.get("target_rank", 999999) or 999999),
                "target_score": round(float(candidate.get("target_score", 0) or 0), 4),
                "target_direction_bias": str(candidate.get("target_direction_bias", "") or ""),
                "signal_quality": round(float(candidate.get("signal_quality", 0) or 0), 4),
                "requested_exposure": round(float(candidate.get("requested_exposure", 0) or 0), 4),
                "estimated_entry_commission": round(float(candidate.get("estimated_entry_commission", 0) or 0), 4),
                **(details or {}),
            }
        )

    def _build_portfolio_candidate(
        self,
        state: dict,
        bar: dict,
        index: int,
        signal: dict,
        signal_payload: dict,
        signal_row: dict,
        current_day: str,
        target_lookup: dict[tuple[str, str], dict],
        execution_profile: dict | None = None,
    ) -> dict:
        symbol = str(state.get("symbol", signal_payload.get("symbol", "")) or "").strip().upper()
        target_meta = target_lookup.get((current_day, symbol)) or target_lookup.get(("", symbol)) or {}
        extra = dict(signal_payload.get("extra") or {})
        rr = self._coerce_float_value(signal_payload.get("rr"), 0.0)
        signal_quality = max(
            rr,
            self._coerce_float_value(extra.get("signal_score"), 0.0),
            self._coerce_float_value(extra.get("score"), 0.0),
            self._coerce_float_value(extra.get("sl_atr_ratio"), 0.0),
        )
        estimated_entry_commission = self._estimate_portfolio_entry_commission(signal_payload, execution_profile)
        requested_exposure = self._portfolio_signal_exposure(signal_payload, execution_profile)
        return {
            "state": state,
            "symbol": symbol,
            "bar": bar,
            "index": index,
            "signal": signal,
            "signal_payload": signal_payload,
            "signal_row": signal_row,
            "signal_id": str(signal_row.get("signal_id", "") or ""),
            "current_day": current_day,
            "target_rank": int(target_meta.get("rank", 999999) or 999999),
            "target_score": float(target_meta.get("score", 0) or 0),
            "target_direction_bias": str(target_meta.get("direction_bias", "") or "").strip().lower(),
            "signal_quality": signal_quality,
            "requested_exposure": requested_exposure,
            "estimated_entry_commission": estimated_entry_commission,
        }

    def _target_strategy_meta_for_symbol(self, target_lookup: dict[tuple[str, str], dict], symbol: str) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return {}
        exact_rows = [
            dict(meta or {})
            for (date_text, row_symbol), meta in (target_lookup or {}).items()
            if str(row_symbol or "").strip().upper() == normalized_symbol and str(date_text or "").strip()
        ]
        if len(exact_rows) == 1:
            return exact_rows[0]
        return dict((target_lookup or {}).get(("", normalized_symbol)) or {})

    def _request_with_target_strategy_policy(
        self,
        request: dict,
        target_lookup: dict[tuple[str, str], dict],
        symbol: str,
    ) -> dict:
        if not self._normalize_bool(request.get("portfolio_use_target_strategy_policy"), False):
            return request
        target_meta = self._target_strategy_meta_for_symbol(target_lookup, symbol)
        strategy_policy = target_meta.get("strategy_policy") if isinstance(target_meta.get("strategy_policy"), dict) else {}
        exit_policy = target_meta.get("recommended_exit_policy") if isinstance(target_meta.get("recommended_exit_policy"), dict) else {}
        if not exit_policy and isinstance(strategy_policy.get("recommended_exit_policy"), dict):
            exit_policy = strategy_policy.get("recommended_exit_policy") or {}
        if not strategy_policy and not exit_policy:
            return request

        base_params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        effective_params = dict(base_params)
        applied: dict[str, Any] = {}

        profile = str(exit_policy.get("exit_policy_profile") or exit_policy.get("profile") or "").strip()
        if profile:
            effective_params["exit_policy_profile"] = profile
            applied["exit_policy_profile"] = profile

        sl_mult = self._coerce_float_value(exit_policy.get("sl_atr_mult"), 0.0)
        if sl_mult > 0:
            effective_params["sl_atr_mult"] = sl_mult
            applied["sl_atr_mult"] = sl_mult

        tp_rr = self._coerce_float_value(exit_policy.get("tp_rr") or exit_policy.get("rr_ratio"), 0.0)
        if tp_rr > 0:
            effective_params["rr_ratio"] = tp_rr
            applied["rr_ratio"] = tp_rr

        signal_profile = str(strategy_policy.get("recommended_signal_profile") or "").strip()
        if signal_profile:
            effective_params["signal_strategy_profile"] = signal_profile
            effective_params["ibkr_signal_strategy_profile"] = signal_profile
            applied["signal_strategy_profile"] = signal_profile

        if not applied:
            return request

        effective_params["target_strategy_policy"] = dict(strategy_policy or {})
        effective_params["target_symbol_profile"] = dict(target_meta.get("symbol_profile") or {})
        symbol_request = dict(request)
        params = dict(request.get("params") or {})
        params["strategy_params"] = effective_params
        symbol_request["params"] = params
        symbol_request["_target_strategy_policy_applied"] = {
            "symbol": str(symbol or "").strip().upper(),
            "applied": applied,
            "strategy_policy": dict(strategy_policy or {}),
            "symbol_profile": dict(target_meta.get("symbol_profile") or {}),
        }
        return symbol_request

    def _sort_portfolio_candidates(self, candidates: list[dict], request: dict) -> list[dict]:
        priority = str(request.get("simultaneous_signal_priority") or "daily_target_rank").strip().lower()

        def sort_key(candidate: dict):
            symbol = str(candidate.get("symbol", "") or "")
            bar_ms = int((candidate.get("bar") or {}).get("bar_time_ms", 0) or 0)
            target_rank = int(candidate.get("target_rank", 999999) or 999999)
            target_score = float(candidate.get("target_score", 0) or 0)
            signal_quality = float(candidate.get("signal_quality", 0) or 0)
            requested_exposure = float(candidate.get("requested_exposure", 0) or 0)
            if priority == "signal_quality":
                return (bar_ms, -signal_quality, target_rank, -target_score, requested_exposure, symbol)
            if priority == "liquidity":
                bar = candidate.get("bar") or {}
                liquidity = float(bar.get("volume", 0) or 0) * float(bar.get("close", 0) or 0)
                return (bar_ms, -liquidity, target_rank, -signal_quality, requested_exposure, symbol)
            return (bar_ms, target_rank, -target_score, -signal_quality, requested_exposure, symbol)

        return sorted(candidates, key=sort_key)

    def _release_portfolio_pending(self, ledger: dict, pending_signal: dict | None):
        if not pending_signal:
            return
        ledger["reserved_exposure"] = max(
            0.0,
            float(ledger.get("reserved_exposure", 0) or 0) - self._portfolio_signal_exposure(pending_signal),
        )
        ledger["pending_entry_slots"] = max(0, int(ledger.get("pending_entry_slots", 0) or 0) - 1)

    def _open_portfolio_position_from_pending(
        self,
        ledger: dict,
        symbol: str,
        bar: dict,
        pending_signal: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> dict | None:
        position = self._check_pending_entry_fill(
            symbol,
            bar,
            pending_signal,
            commission_per_share,
            slippage_bps,
            execution_profile,
        )
        if not position:
            return None
        self._release_portfolio_pending(ledger, pending_signal)
        exposure = self._portfolio_position_exposure(position)
        position["entry_exposure"] = exposure
        position["portfolio_execution_model"] = "portfolio_stream"
        ledger["open_exposure"] = float(ledger.get("open_exposure", 0) or 0) + exposure
        ledger["open_position_slots"] = int(ledger.get("open_position_slots", 0) or 0) + 1
        ledger["max_gross_exposure"] = max(float(ledger.get("max_gross_exposure", 0) or 0), float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0))
        ledger["max_borrowed_amount"] = max(float(ledger.get("max_borrowed_amount", 0) or 0), self._portfolio_projected_borrow(ledger, 0))
        return position

    def _close_portfolio_position(self, ledger: dict, position: dict | None, trade: dict | None) -> dict | None:
        if not position or not trade:
            return trade
        exposure = self._portfolio_position_exposure(position)
        ledger["open_exposure"] = max(0.0, float(ledger.get("open_exposure", 0) or 0) - exposure)
        ledger["open_position_slots"] = max(0, int(ledger.get("open_position_slots", 0) or 0) - 1)
        ledger["realized_pnl"] = float(ledger.get("realized_pnl", 0) or 0) + float(trade.get("pnl", 0) or 0)
        trade_extra = self._parse_object(trade.get("extra"))
        trade_extra.update(
            {
                "execution_model": "portfolio_stream",
                "entry_exposure": round(exposure, 4),
                "portfolio_open_exposure_after": round(float(ledger.get("open_exposure", 0) or 0), 4),
                "portfolio_reserved_exposure_after": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
                "portfolio_borrowed_after": round(self._portfolio_projected_borrow(ledger, 0), 4),
            }
        )
        trade["extra"] = trade_extra
        return trade

    def _accept_portfolio_candidate(self, ledger: dict, candidate: dict, signal_index: dict, request: dict) -> bool:
        max_target_rank = int(request.get("portfolio_max_target_rank", 0) or 0)
        target_rank = int(candidate.get("target_rank", 999999) or 999999)
        if max_target_rank > 0 and target_rank > max_target_rank:
            reason = "target_rank_over_limit"
            details = {"portfolio_max_target_rank": max_target_rank, "portfolio_target_rank": target_rank}
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
            return False

        min_target_score = float(request.get("portfolio_min_target_score", 0) or 0)
        target_score = float(candidate.get("target_score", 0) or 0)
        if min_target_score > 0 and target_score < min_target_score:
            reason = "target_score_below_min"
            details = {
                "portfolio_min_target_score": round(min_target_score, 4),
                "portfolio_target_score": round(target_score, 4),
            }
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
            return False

        if self._normalize_bool(request.get("portfolio_require_target_direction_alignment"), False):
            target_direction = str(candidate.get("target_direction_bias", "") or "").strip().lower()
            signal_payload = candidate.get("signal_payload") or {}
            signal_direction = str(signal_payload.get("direction", "") or "").strip().lower()
            if target_direction not in {"long", "short"}:
                reason = "target_direction_missing"
                details = {"portfolio_target_direction_bias": target_direction}
                self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
                self._portfolio_record_rejection(ledger, reason)
                self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
                return False
            if signal_direction != target_direction:
                reason = "target_direction_mismatch"
                details = {
                    "portfolio_target_direction_bias": target_direction,
                    "signal_direction": signal_direction,
                }
                self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
                self._portfolio_record_rejection(ledger, reason)
                self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
                return False

        if self._normalize_bool(request.get("portfolio_block_mr_overextended_state"), False):
            signal_payload = candidate.get("signal_payload") or {}
            extra = self._parse_object(signal_payload.get("extra"))
            signal_mode = str(extra.get("signal_mode") or "").strip().lower()
            signal_name = str(signal_payload.get("signal") or "").strip().lower()
            signal_direction = str(signal_payload.get("direction", "") or "").strip().lower()
            if signal_mode == "mr" or signal_name.startswith("mr_") or signal_name.startswith("mr"):
                crsi_state = str(extra.get("crsi_state") or "").strip().lower()
                sd_zone = str(extra.get("sd_zone") or "").strip().lower()
                states = {crsi_state, sd_zone}
                if signal_direction == "long" and "overbought" in states:
                    reason = "mr_long_overextended_state"
                    details = {"signal_mode": signal_mode or "mr", "crsi_state": crsi_state, "sd_zone": sd_zone}
                    self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
                    self._portfolio_record_rejection(ledger, reason)
                    self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
                    return False
                if signal_direction == "short" and "oversold" in states:
                    reason = "mr_short_overextended_state"
                    details = {"signal_mode": signal_mode or "mr", "crsi_state": crsi_state, "sd_zone": sd_zone}
                    self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
                    self._portfolio_record_rejection(ledger, reason)
                    self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
                    return False

        if self._normalize_bool(request.get("portfolio_block_early_trend_without_ema_touch"), False):
            signal_payload = candidate.get("signal_payload") or {}
            extra = self._parse_object(signal_payload.get("extra"))
            signal_mode = str(extra.get("signal_mode") or "").strip().lower()
            signal_name = str(signal_payload.get("signal") or "").strip().lower()
            if signal_mode == "trend" or signal_name.startswith("trend"):
                dtp_phase = str(extra.get("dtp_phase") or "").strip().lower()
                ema_touch_line = str(extra.get("ema_touch_line") or "").strip().lower()
                if dtp_phase == "early" and ema_touch_line in {"", "none"}:
                    reason = "trend_early_without_ema_touch"
                    details = {
                        "signal_mode": signal_mode or "trend",
                        "dtp_phase": dtp_phase,
                        "ema_touch_line": ema_touch_line,
                    }
                    self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
                    self._portfolio_record_rejection(ledger, reason)
                    self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
                    return False

        if str(request.get("manual_confirm_mode") or "auto") == "strict":
            reason = "manual_confirmation_required"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        if self._portfolio_sl_circuit_breaker_active(ledger, request):
            reason = "sl_circuit_breaker"
            details = {
                "consecutive_stop_loss_count": int(ledger.get("consecutive_stop_loss_count", 0) or 0),
                "consecutive_stop_loss_limit": self._portfolio_consecutive_stop_loss_limit(request),
            }
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
            return False

        position_limit = int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or 0)
        if position_limit > 0 and int(ledger.get("daily_position_count", 0) or 0) >= position_limit:
            reason = "position_limit_reached"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        if not self._portfolio_has_strategy_capacity(ledger, request):
            reason = "waiting_for_capacity"
            details = self._portfolio_capacity_details(ledger, request)
            current_status = str((signal_index.get(str(candidate.get("signal_id") or "")) or {}).get("status") or "")
            if current_status != reason:
                self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), reason, reason, details)
            state = candidate.get("state") or {}
            state["capacity_wait_candidate"] = {
                **candidate,
                "capacity_wait_since_bar_ms": int((candidate.get("bar") or {}).get("bar_time_ms", 0) or 0),
                "capacity_wait_since_us_time": str((candidate.get("bar") or {}).get("us_time", "") or ""),
            }
            self._portfolio_record_candidate_sample(ledger, candidate, reason, reason, details)
            return False

        confirm_delay_minutes = int(request.get("confirm_delay_minutes", 0) or 0)
        if str(request.get("manual_confirm_mode") or "auto") != "delayed":
            confirm_delay_minutes = 0
        signal_payload = candidate.get("signal_payload") or {}
        validity_minutes = self._portfolio_signal_validity_minutes(signal_payload, request)
        if confirm_delay_minutes > validity_minutes:
            reason = "signal_expired_before_confirm"
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason)
            return False

        exposure = float(candidate.get("requested_exposure", 0) or 0)
        ok, reason, details = self._portfolio_can_reserve_exposure(ledger, exposure)
        if not ok:
            self._mark_backtest_signal_status(signal_index, candidate.get("signal_id"), "skipped", reason, details)
            self._portfolio_record_rejection(ledger, reason)
            self._portfolio_record_candidate_sample(ledger, candidate, "skipped", reason, details)
            return False

        state = candidate["state"]
        pending_signal = self._build_backtest_pending_signal(
            candidate["symbol"],
            candidate["bar"],
            candidate["signal"],
            candidate["signal_payload"],
        )
        confirm_ready_ms = int(candidate["bar"].get("bar_time_ms", 0) or 0) + confirm_delay_minutes * 60 * 1000
        pending_signal["reserved_exposure"] = exposure
        pending_signal["confirm_ready_bar_ms"] = confirm_ready_ms
        pending_signal["validity_minutes"] = validity_minutes
        pending_signal["portfolio_target_rank"] = int(candidate.get("target_rank", 999999) or 999999)
        pending_signal["portfolio_target_score"] = float(candidate.get("target_score", 0) or 0)
        original_signal_ms = int((candidate.get("signal_payload") or {}).get("bar_time_ms", 0) or 0)
        if original_signal_ms > 0:
            pending_signal["signal_bar_ms"] = original_signal_ms
            pending_signal["signal_us_time"] = str((candidate.get("signal_payload") or {}).get("us_time", "") or "")
            pending_signal["signal_cn_time"] = str((candidate.get("signal_payload") or {}).get("cn_time", "") or "")
        state["pending_signal"] = pending_signal
        state["capacity_wait_candidate"] = None
        ledger["reserved_exposure"] = float(ledger.get("reserved_exposure", 0) or 0) + exposure
        ledger["pending_entry_slots"] = int(ledger.get("pending_entry_slots", 0) or 0) + 1
        ledger["daily_position_count"] = int(ledger.get("daily_position_count", 0) or 0) + 1
        ledger["max_gross_exposure"] = max(float(ledger.get("max_gross_exposure", 0) or 0), float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0))
        ledger["max_borrowed_amount"] = max(float(ledger.get("max_borrowed_amount", 0) or 0), self._portfolio_projected_borrow(ledger, 0))
        self._mark_backtest_signal_status(
            signal_index,
            candidate.get("signal_id"),
            "pending",
            "accepted_pending_entry",
            {
                **details,
                "portfolio_target_rank": int(candidate.get("target_rank", 999999) or 999999),
                "portfolio_target_score": round(float(candidate.get("target_score", 0) or 0), 4),
                "confirm_delay_minutes": confirm_delay_minutes,
                "confirm_ready_bar_ms": confirm_ready_ms,
            },
        )
        self._portfolio_record_candidate_sample(ledger, candidate, "pending", "accepted_pending_entry", details)
        return True

    def _bootstrap_backtest_state(self, engine: IndicatorEngine, signal_gen: SignalGenerator, warmup_bars: list[dict]) -> str:
        previous_day = ""
        for bar in warmup_bars:
            current_day = str(bar.get("us_time", "") or "")[:10]
            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
            previous_day = current_day
            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue
            signal_gen.update(snapshot)
        return previous_day

    def _prepare_portfolio_symbol_state(
        self,
        symbol: str,
        bars: list[dict],
        request: dict,
        allowed_trade_days: set[str] | None,
        admitted_after_ms_by_day: dict[str, int] | None = None,
    ) -> tuple[dict | None, dict | None]:
        if len(bars) < 40:
            return None, {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": 0,
                "status": "insufficient_data",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }
        precheck_gap_count = self._count_internal_5m_gaps(bars)
        if precheck_gap_count > 0:
            return None, {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": precheck_gap_count,
                "status": "invalid_gap",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }

        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        engine = runtime_indicator_engine()(symbol, "5m", params=params)
        signal_gen = runtime_signal_generator()(symbol, "5m", params=params)
        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0),
            request["session_mode"],
            limit=self._effective_indicator_warmup_bars(request, "warmup_bars"),
        )
        previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars) if warmup_bars else ""
        compare_with_tv = self._should_compare_with_tv(request)
        compare_tv_signals = self._should_compare_tv_signals(request)
        tv_reference = self._load_tv_reference(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
            include_signals=compare_tv_signals,
        ) if compare_with_tv else {}
        symbol_tv_parity = self._init_symbol_tv_parity(
            symbol,
            tv_reference,
            compare_tv_signals=compare_tv_signals,
        ) if compare_with_tv else None
        daily_close_lookup_cache = request.get("_daily_close_lookup_cache") if isinstance(request.get("_daily_close_lookup_cache"), dict) else {}
        return {
            "symbol": symbol,
            "bars": bars,
            "engine": engine,
            "signal_gen": signal_gen,
            "daily_close_lookup": list(
                daily_close_lookup_cache.get(symbol)
                if symbol in daily_close_lookup_cache
                else self._load_daily_close_lookup(
                    symbol,
                    request["source_environment"],
                    request["date_from"],
                    request["date_to"],
                )
            ),
            "symbol_tv_parity": symbol_tv_parity,
            "allowed_trade_days": allowed_trade_days,
            "admitted_after_ms_by_day": dict(admitted_after_ms_by_day or {}),
            "previous_ms": 0,
            "previous_day": previous_day,
            "previous_bar": None,
            "pending_signal": None,
            "capacity_wait_candidate": None,
            "open_position": None,
            "cooldown_until_ms": 0,
            "cooldown_reason": "",
            "gap_count": 0,
            "market_bars": 0,
            "reverse_index": set(),
        }, None

    def _build_daily_admission_lookup(self, target_rows: list[dict]) -> dict[str, dict[str, int]]:
        lookup: dict[str, dict[str, int]] = {}
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            trade_date = str(row.get("date", "") or "")[:10]
            if not symbol or not trade_date:
                continue
            extra = self._parse_object(row.get("extra"))
            admitted_ms = int(extra.get("sd_admitted_at_ms", 0) or 0)
            if admitted_ms > 0:
                lookup.setdefault(symbol, {})[trade_date] = admitted_ms
        return lookup

    def _run_portfolio_daily_selected_backtest(
        self,
        symbols: list[str],
        request: dict,
        selection_plan: dict[str, list[str]],
        target_rows: list[dict] | None = None,
        progress_context: dict | None = None,
    ) -> dict:
        started_at = time.time()
        target_rows = list(target_rows or [])
        target_rows_by_day: dict[str, list[dict]] = {}
        for row in target_rows:
            trade_date = str(row.get("date", "") or "")[:10]
            if trade_date:
                target_rows_by_day.setdefault(trade_date, []).append(row)
        if not selection_plan:
            for row in target_rows:
                trade_date = str(row.get("date", "") or "")[:10]
                symbol = str(row.get("symbol", "") or "").strip().upper()
                if trade_date and symbol:
                    selection_plan.setdefault(trade_date, []).append(symbol)

        admitted_lookup = self._build_daily_admission_lookup(target_rows)
        all_dates = sorted(str(date or "")[:10] for date in selection_plan.keys() if str(date or "")[:10])
        all_trades: list[dict] = []
        all_indicator_rows: list[dict] = []
        all_signal_rows: list[dict] = []
        all_reverse_rows: list[dict] = []
        all_data_quality: list[dict] = []
        all_skipped_symbols: list[str] = []
        tv_symbol_reports: list[dict] = []
        indicator_count = 0
        cumulative_realized_pnl = 0.0
        rejection_counts: dict[str, int] = {}
        candidate_samples: list[dict] = []
        daily_profiles: list[dict] = []
        portfolio_risk: dict = {}
        max_gross_exposure = 0.0
        max_borrowed_amount = 0.0
        final_open_exposure = 0.0
        final_reserved_exposure = 0.0
        total_loaded_bars = 0
        total_bar_times = 0

        total_days = max(1, len(all_dates))
        base_initial_capital = float(request.get("initial_capital", 0) or 0)
        selected_symbol_set = {
            str(item or "").strip().upper()
            for date in all_dates
            for item in list(selection_plan.get(date) or [])
            if str(item or "").strip()
        }
        selected_symbols = [symbol for symbol in symbols if symbol in selected_symbol_set]
        selected_symbols_seen = set(selected_symbols)
        selected_symbols.extend(sorted(symbol for symbol in selected_symbol_set if symbol not in selected_symbols_seen))
        close_lookup_started_at = time.time()
        daily_close_lookup_cache = self._build_daily_close_lookup_cache(
            selected_symbols,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
        )
        close_lookup_cache_profile = {
            "enabled": True,
            "symbols": len(daily_close_lookup_cache),
            "rows": sum(len(rows or []) for rows in daily_close_lookup_cache.values()),
            "duration_s": round(time.time() - close_lookup_started_at, 3),
        }
        for day_index, trade_date in enumerate(all_dates, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            day_symbols = [
                str(symbol or "").strip().upper()
                for symbol in list(selection_plan.get(trade_date) or [])
                if str(symbol or "").strip()
            ]
            day_symbols = [symbol for index, symbol in enumerate(day_symbols) if symbol and symbol not in day_symbols[:index]]
            progress_value = 16 + int(((day_index - 1) / total_days) * 69)
            next_progress_value = 16 + int((day_index / total_days) * 69)
            self._set_progress_context(
                "running",
                "daily_selected_stream",
                f"streaming selected day {trade_date} {day_index}/{total_days}",
                progress_value,
                progress_context,
            )
            if not day_symbols:
                daily_profiles.append(
                    {
                        "date": trade_date,
                        "selected_count": 0,
                        "symbols": [],
                        "trades": 0,
                        "signals": 0,
                        "bars_loaded": 0,
                        "duration_s": 0.0,
                    }
                )
                continue

            day_request = deepcopy(request)
            day_request["date_from"] = trade_date
            day_request["date_to"] = trade_date
            day_request["symbols"] = day_symbols
            day_request["symbols_text"] = ",".join(day_symbols)
            day_request["max_symbols"] = len(day_symbols)
            day_request["_daily_close_lookup_cache"] = daily_close_lookup_cache
            if cumulative_realized_pnl:
                day_request["initial_capital"] = max(1000.0, base_initial_capital + cumulative_realized_pnl)
            day_target_rows = list(target_rows_by_day.get(trade_date) or [])
            day_allowed = {symbol: {trade_date} for symbol in day_symbols}
            day_admitted = {
                symbol: {trade_date: int((admitted_lookup.get(symbol) or {}).get(trade_date, 0) or 0)}
                for symbol in day_symbols
                if int((admitted_lookup.get(symbol) or {}).get(trade_date, 0) or 0) > 0
            }
            day_started_at = time.time()
            day_progress_context = {
                "start": self._map_progress(progress_value, progress_context),
                "end": max(
                    self._map_progress(progress_value, progress_context),
                    self._map_progress(next_progress_value, progress_context),
                ),
                "prefix": str((progress_context or {}).get("prefix") or ""),
            }
            day_result = self._run_portfolio_stream_backtest(
                day_symbols,
                day_request,
                allowed_trade_days_by_symbol=day_allowed,
                target_rows=day_target_rows,
                admitted_after_ms_by_symbol_day=day_admitted,
                progress_context=day_progress_context,
            )
            day_metrics = dict(day_result.get("portfolio_metrics") or {})
            day_profile = dict(day_metrics.get("portfolio_profile") or {})
            day_rejections = dict(day_metrics.get("portfolio_rejection_counts") or {})
            for key, value in day_rejections.items():
                rejection_counts[key] = int(rejection_counts.get(key, 0) or 0) + int(value or 0)
            if not portfolio_risk:
                portfolio_risk = dict(day_metrics.get("portfolio_risk") or {})
            candidate_samples.extend(list(day_metrics.get("portfolio_candidate_samples") or [])[: max(0, 20 - len(candidate_samples))])
            day_trades = list(day_result.get("trades") or [])
            day_signals = list(day_result.get("signal_rows") or [])
            day_reverse_rows = list(day_result.get("reverse_rows") or [])
            all_trades.extend(day_trades)
            all_indicator_rows.extend(list(day_result.get("indicator_rows") or []))
            all_signal_rows.extend(day_signals)
            all_reverse_rows.extend(day_reverse_rows)
            tv_symbol_reports.extend(list(day_result.get("tv_symbol_reports") or []))
            indicator_count += int(day_result.get("indicator_count", 0) or 0)
            cumulative_realized_pnl += float(day_metrics.get("portfolio_realized_pnl", 0) or 0)
            max_gross_exposure = max(max_gross_exposure, float(day_metrics.get("portfolio_max_gross_exposure", 0) or 0))
            max_borrowed_amount = max(max_borrowed_amount, float(day_metrics.get("portfolio_max_borrowed_amount", 0) or 0))
            final_open_exposure = float(day_metrics.get("portfolio_final_open_exposure", 0) or 0)
            final_reserved_exposure = float(day_metrics.get("portfolio_final_reserved_exposure", 0) or 0)
            total_loaded_bars += int(day_profile.get("bars_loaded", 0) or 0)
            total_bar_times += int(day_profile.get("bar_times", 0) or 0)
            for row in list(day_result.get("data_quality") or []):
                quality_row = dict(row)
                quality_row.setdefault("date", trade_date)
                all_data_quality.append(quality_row)
            for symbol in list(day_result.get("skipped_symbols") or []):
                all_skipped_symbols.append(f"{trade_date}:{symbol}")
            daily_profiles.append(
                {
                    "date": trade_date,
                    "selected_count": len(day_symbols),
                    "symbols": day_symbols,
                    "trades": len(day_trades),
                    "signals": len(day_signals),
                    "reverse_rows": len(day_reverse_rows),
                    "bars_loaded": int(day_profile.get("bars_loaded", 0) or 0),
                    "bar_times": int(day_profile.get("bar_times", 0) or 0),
                    "duration_s": round(time.time() - day_started_at, 3),
                    "rejection_counts": day_rejections,
                }
            )

        all_trades.sort(key=lambda item: (int(item.get("exit_bar_ms", 0) or 0), item.get("symbol", "")))
        all_signal_rows.sort(key=lambda item: (int(item.get("bar_time_ms", 0) or 0), item.get("symbol", "")))
        all_reverse_rows.sort(key=lambda item: (int(item.get("bar_time_ms", 0) or 0), item.get("symbol", ""), item.get("action_type", "")))
        selected_symbol_days = sum(len(selection_plan.get(date) or []) for date in all_dates)
        daily_selected_profile = {
            "enabled": True,
            "mode": "daily_selected_live_sd",
            "trade_dates": len(all_dates),
            "unique_symbols": len(set(symbols or [])),
            "selected_symbol_days": selected_symbol_days,
            "avg_selected_per_day": round(selected_symbol_days / max(1, len(all_dates)), 4),
            "max_selected_per_day": max([len(selection_plan.get(date) or []) for date in all_dates] or [0]),
            "bars_loaded": total_loaded_bars,
            "bar_times": total_bar_times,
            "indicator_count": indicator_count,
            "duration_s": round(time.time() - started_at, 3),
            "daily_close_lookup_cache": close_lookup_cache_profile,
            "resource_snapshot": self._read_backtest_resource_snapshot(),
            "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
            "daily": daily_profiles,
        }
        gross_now = final_open_exposure + final_reserved_exposure
        portfolio_metrics = {
            "execution_model": "portfolio_stream",
            "daily_selected_only": True,
            "execution_cost_profile": compact_execution_cost_profile(build_execution_cost_profile(request)),
            "portfolio_profile": daily_selected_profile,
            "daily_selected_profile": daily_selected_profile,
            "portfolio_risk": portfolio_risk or self._resolve_portfolio_risk_limits(request),
            "portfolio_rejection_counts": rejection_counts,
            "portfolio_candidate_samples": candidate_samples,
            "portfolio_max_gross_exposure": round(max_gross_exposure, 4),
            "portfolio_max_borrowed_amount": round(max_borrowed_amount, 4),
            "portfolio_final_open_exposure": round(final_open_exposure, 4),
            "portfolio_final_reserved_exposure": round(final_reserved_exposure, 4),
            "portfolio_final_gross_exposure": round(gross_now, 4),
            "portfolio_realized_pnl": round(cumulative_realized_pnl, 4),
        }
        return {
            "trades": all_trades,
            "indicator_rows": all_indicator_rows,
            "indicator_count": indicator_count,
            "signal_rows": all_signal_rows,
            "reverse_rows": all_reverse_rows,
            "data_quality": all_data_quality,
            "skipped_symbols": all_skipped_symbols,
            "tv_symbol_reports": tv_symbol_reports,
            "portfolio_metrics": portfolio_metrics,
        }

    def _run_portfolio_stream_backtest(
        self,
        symbols: list[str],
        request: dict,
        allowed_trade_days_by_symbol: dict[str, set[str]] | None = None,
        target_rows: list[dict] | None = None,
        admitted_after_ms_by_symbol_day: dict[str, dict[str, int]] | None = None,
        progress_context: dict | None = None,
    ) -> dict:
        risk_limits = self._resolve_portfolio_risk_limits(request)
        ledger = {
            **risk_limits,
            "realized_pnl": 0.0,
            "open_exposure": 0.0,
            "reserved_exposure": 0.0,
            "open_position_slots": 0,
            "pending_entry_slots": 0,
            "max_gross_exposure": 0.0,
            "max_borrowed_amount": 0.0,
            "daily_position_count": 0,
            "consecutive_stop_loss_count": 0,
            "max_consecutive_stop_loss_count": 0,
            "current_day": "",
            "rejection_counts": {},
            "candidate_samples": [],
        }
        states: dict[str, dict] = {}
        bars_by_time: dict[int, list[tuple[str, int, dict]]] = {}
        data_quality = []
        skipped_symbols = []
        all_trades = []
        all_indicator_rows = []
        indicator_count = 0
        all_signal_rows = []
        all_reverse_rows = []
        tv_symbol_reports = []
        signal_index = {}
        target_lookup = self._build_daily_target_lookup(target_rows or [], symbols)
        load_started_at = time.time()
        total_loaded_bars = 0

        total_symbols = max(1, len(symbols))
        for completed_symbols, symbol in enumerate(symbols):
            if self._cancel_event.is_set():
                self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
                raise BacktestCancelled()
            progress_value = 16 + int((completed_symbols / total_symbols) * 8)
            self._set_progress_context("running", "loading", f"loading {symbol}", progress_value, progress_context)
            bars = self._load_symbol_bars(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                request["session_mode"],
                allow_backfill=False,
            )
            symbol_request = self._request_with_target_strategy_policy(request, target_lookup, symbol)
            state, quality = self._prepare_portfolio_symbol_state(
                symbol,
                bars,
                symbol_request,
                (allowed_trade_days_by_symbol or {}).get(symbol),
                (admitted_after_ms_by_symbol_day or {}).get(symbol),
            )
            if quality:
                data_quality.append(quality)
                skipped_symbols.append(symbol)
                bars.clear()
                continue
            if not state:
                skipped_symbols.append(symbol)
                bars.clear()
                continue
            states[symbol] = state
            symbol_bars = state.get("bars") or []
            bar_count = len(symbol_bars)
            state["bar_count"] = bar_count
            total_loaded_bars += bar_count
            state["first_bar_us"] = symbol_bars[0].get("us_time", "") if symbol_bars else ""
            state["last_bar_us"] = symbol_bars[-1].get("us_time", "") if symbol_bars else ""
            last_compact_bar = None
            for index, bar in enumerate(symbol_bars):
                bar_ms = int(bar.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                compact_bar = self._compact_portfolio_bar(
                    bar,
                    is_last_bar=index >= bar_count - 1,
                    next_day=(
                        str(symbol_bars[index + 1].get("us_time", "") or "")[:10]
                        if index < bar_count - 1
                        else ""
                    ),
                )
                last_compact_bar = compact_bar
                bars_by_time.setdefault(bar_ms, []).append((symbol, index, compact_bar))
            state["last_bar"] = self._inflate_portfolio_bar(symbol, last_compact_bar) if last_compact_bar else None
            if hasattr(symbol_bars, "clear"):
                symbol_bars.clear()
            state["bars"] = []

        if not states:
            result = {
                "trades": [],
                "indicator_rows": [],
                "indicator_count": 0,
                "signal_rows": [],
                "reverse_rows": [],
                "data_quality": data_quality,
                "skipped_symbols": skipped_symbols,
                "tv_symbol_reports": [],
                "portfolio_metrics": {
                    "execution_model": "portfolio_stream",
                    "portfolio_risk": {
                        **risk_limits,
                        "position_limit_max": int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or 0),
                        "max_strategy_open_positions": self._max_strategy_open_positions(request),
                        "consecutive_stop_loss_limit": self._portfolio_consecutive_stop_loss_limit(request),
                    },
                    "execution_cost_profile": compact_execution_cost_profile(build_execution_cost_profile(request)),
                    "portfolio_rejection_counts": {},
                    "portfolio_candidate_samples": [],
                },
            }
            self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
            return result

        bar_times = sorted(bars_by_time.keys())
        load_duration_s = round(time.time() - load_started_at, 3)
        stream_started_at = time.time()
        total_steps = max(1, len(bar_times))
        commission_per_share = float(request["commission_per_share"])
        slippage_bps = float(request["slippage_bps"])
        execution_profile = build_execution_cost_profile(
            request,
            commission_per_share=commission_per_share,
            slippage_bps=slippage_bps,
        )
        force_flat_eod = bool(request["force_flat_eod"])
        compare_tv_signals = self._should_compare_tv_signals(request)
        capture_indicator_rows = self._should_persist_backtest_indicators(request)

        def append_trade(position: dict, trade: dict | None):
            if not trade:
                return
            closed_trade = self._close_portfolio_position(ledger, position, trade)
            if closed_trade:
                all_trades.append(closed_trade)
                if str(closed_trade.get("exit_reason") or "") == "stop_loss":
                    ledger["consecutive_stop_loss_count"] = int(ledger.get("consecutive_stop_loss_count", 0) or 0) + 1
                    ledger["max_consecutive_stop_loss_count"] = max(
                        int(ledger.get("max_consecutive_stop_loss_count", 0) or 0),
                        int(ledger.get("consecutive_stop_loss_count", 0) or 0),
                    )
                    state = states.get(str(closed_trade.get("symbol") or "").upper())
                    if state is not None:
                        self._start_backtest_cooldown(
                            state,
                            {"bar_time_ms": int(closed_trade.get("exit_bar_ms", 0) or 0)},
                            int(request.get("cooldown_bars_after_sl", 6) or 0),
                            "cooldown_after_stop_loss",
                        )
                else:
                    ledger["consecutive_stop_loss_count"] = 0

        for step_index, bar_ms in enumerate(bar_times):
            if self._cancel_event.is_set():
                self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
                self._clear_backtest_row_buffers(
                    all_trades,
                    all_indicator_rows,
                    all_signal_rows,
                    all_reverse_rows,
                    tv_symbol_reports,
                    data_quality,
                    skipped_symbols,
                )
                raise BacktestCancelled()
            throttled = self._maybe_throttle_backtest_resources(
                request,
                step_index=step_index,
                total_steps=total_steps,
                progress_context=progress_context,
            )
            if self._cancel_event.is_set():
                self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
                self._clear_backtest_row_buffers(
                    all_trades,
                    all_indicator_rows,
                    all_signal_rows,
                    all_reverse_rows,
                    tv_symbol_reports,
                    data_quality,
                    skipped_symbols,
                )
                raise BacktestCancelled()
            if not throttled and (step_index % 50 == 0 or step_index == total_steps - 1):
                progress_value = 25 + int((step_index / total_steps) * 60)
                self._set_progress_context("running", "streaming", f"streaming portfolio {step_index + 1}/{total_steps}", progress_value, progress_context)
            entries = sorted(bars_by_time.pop(bar_ms, []) or [], key=lambda item: item[0])
            current_day = ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if ledger.get("current_day") and ledger["current_day"] != current_day:
                ledger["daily_position_count"] = 0
                ledger["consecutive_stop_loss_count"] = 0
            ledger["current_day"] = current_day

            candidates = []
            for symbol, index, compact_bar in entries:
                bar = self._inflate_portfolio_bar(symbol, compact_bar)
                state = states[symbol]
                state["market_bars"] = int(state.get("market_bars", 0) or 0) + 1
                current_symbol_day = str(bar.get("us_time", "") or "")[:10]

                previous_ms = int(state.get("previous_ms", 0) or 0)
                if previous_ms > 0 and int(bar["bar_time_ms"]) - previous_ms > interval_to_ms("5m") * 3:
                    state["gap_count"] = int(state.get("gap_count", 0) or 0) + 1
                state["previous_ms"] = int(bar["bar_time_ms"])

                if state.get("previous_day") and current_symbol_day != state.get("previous_day"):
                    state["signal_gen"].daily_reset()
                    state["cooldown_until_ms"] = 0
                    state["cooldown_reason"] = ""
                    if state.get("open_position") and force_flat_eod and state.get("previous_bar"):
                        trade = self._close_position(
                            state["open_position"],
                            state["previous_bar"],
                            commission_per_share,
                            slippage_bps,
                            "eod",
                            execution_profile,
                        )
                        append_trade(state["open_position"], trade)
                        state["open_position"] = None
                    if state.get("pending_signal"):
                        self._mark_backtest_signal_status(
                            signal_index,
                            state["pending_signal"].get("signal_id"),
                            "dropped",
                            "new_day_reset",
                            {
                                "event_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                                "event_us_time": str(bar.get("us_time", "") or ""),
                                "event_cn_time": str(bar.get("cn_time", "") or ""),
                            },
                        )
                        self._release_portfolio_pending(ledger, state["pending_signal"])
                    state["pending_signal"] = None
                    if state.get("capacity_wait_candidate"):
                        self._mark_capacity_wait_expired(
                            ledger,
                            state["capacity_wait_candidate"],
                            signal_index,
                            state.get("previous_bar") or bar,
                            request,
                        )
                    state["capacity_wait_candidate"] = None
                state["previous_day"] = current_symbol_day

                pending_signal = state.get("pending_signal")
                if pending_signal and state.get("open_position") is None:
                    if self._portfolio_signal_expired(pending_signal, bar, request):
                        self._mark_backtest_signal_status(
                            signal_index,
                            pending_signal.get("signal_id"),
                            "dropped",
                            "signal_expired",
                            {
                                "event_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                                "event_us_time": str(bar.get("us_time", "") or ""),
                                "event_cn_time": str(bar.get("cn_time", "") or ""),
                            },
                        )
                        self._portfolio_record_rejection(ledger, "signal_expired")
                        self._release_portfolio_pending(ledger, pending_signal)
                        state["pending_signal"] = None
                    elif int(bar.get("bar_time_ms", 0) or 0) >= int(pending_signal.get("confirm_ready_bar_ms", pending_signal.get("signal_bar_ms", 0)) or 0):
                        filled_position = self._open_portfolio_position_from_pending(
                            ledger,
                            symbol,
                            bar,
                            pending_signal,
                            commission_per_share,
                            slippage_bps,
                            execution_profile,
                        )
                        if filled_position:
                            state["open_position"] = filled_position
                            self._mark_backtest_signal_status(
                                signal_index,
                                pending_signal.get("signal_id"),
                                "executed",
                                "entry_limit_filled",
                                {
                                    "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                                    "entry_us_time": str(bar.get("us_time", "") or ""),
                                    "entry_cn_time": str(bar.get("cn_time", "") or ""),
                                    "entry_price": round(float(filled_position.get("entry_price", 0) or 0), 4),
                                    "entry_limit_price": round(float(filled_position.get("entry_limit_price", 0) or 0), 4),
                                    "portfolio_open_exposure": round(float(ledger.get("open_exposure", 0) or 0), 4),
                                    "portfolio_reserved_exposure": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
                                },
                            )
                            state["pending_signal"] = None

                if state.get("open_position"):
                    closed = self._check_exit(state["open_position"], bar, commission_per_share, slippage_bps, execution_profile)
                    if closed:
                        append_trade(state["open_position"], closed)
                        state["open_position"] = None

                snapshot = state["engine"].update(bar)
                if not snapshot or not state["engine"].is_ready():
                    state["previous_bar"] = bar
                    continue
                indicator_count += 1

                daily_fields = self._get_daily_change_fields_from_lookup(
                    state["daily_close_lookup"],
                    float(snapshot.get("close", 0) or 0),
                    int(bar.get("bar_time_ms", 0) or 0),
                )
                indicator_payload = None
                indicator_audit = None
                if state.get("symbol_tv_parity") is not None or capture_indicator_rows:
                    indicator_payload = self._build_tv_indicator_compare_payload(
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        request["source_environment"],
                        daily_fields,
                    )
                    if state.get("symbol_tv_parity") is not None:
                        indicator_audit = self._compare_generated_indicator(state["symbol_tv_parity"], indicator_payload)
                    if capture_indicator_rows:
                        all_indicator_rows.append(
                            self._build_backtest_indicator_row(
                                request,
                                bar,
                                indicator_payload,
                                indicator_audit,
                            )
                        )

                signal_snapshot = {**snapshot, **daily_fields}
                signal = state["signal_gen"].update(signal_snapshot)
                trading_day_enabled = state.get("allowed_trade_days") is None or current_symbol_day in state.get("allowed_trade_days")
                admitted_after_ms = int((state.get("admitted_after_ms_by_day") or {}).get(current_symbol_day, 0) or 0)
                sd_admitted_for_bar = admitted_after_ms <= 0 or int(bar.get("bar_time_ms", 0) or 0) >= admitted_after_ms
                preexisting_pending_signal = state.get("pending_signal") if state.get("pending_signal") and state.get("open_position") is None else None
                preexisting_open_position = state.get("open_position")
                preexisting_capacity_wait_candidate = (
                    state.get("capacity_wait_candidate")
                    if state.get("capacity_wait_candidate") and state.get("open_position") is None and state.get("pending_signal") is None
                    else None
                )
                signal_conflict_emitted = False

                if preexisting_capacity_wait_candidate:
                    if self._portfolio_signal_expired(
                        preexisting_capacity_wait_candidate.get("signal_payload") or {},
                        bar,
                        request,
                    ):
                        self._mark_capacity_wait_expired(
                            ledger,
                            preexisting_capacity_wait_candidate,
                            signal_index,
                            bar,
                            request,
                        )
                        state["capacity_wait_candidate"] = None
                        preexisting_capacity_wait_candidate = None
                    elif (
                        trading_day_enabled
                        and sd_admitted_for_bar
                        and self._portfolio_bar_in_trade_window(bar, request)
                        and self._portfolio_bar_in_order_window(bar, request)
                        and not self._portfolio_sl_circuit_breaker_active(ledger, request)
                        and not self._backtest_cooldown_active(state, bar)[0]
                    ):
                        retry_candidate = dict(preexisting_capacity_wait_candidate)
                        retry_candidate["bar"] = bar
                        retry_candidate["state"] = state
                        retry_candidate["current_day"] = current_symbol_day
                        retry_candidate["capacity_retry"] = True
                        candidates.append(retry_candidate)

                if signal and int(signal.get("shares", 0) or 0) > 0:
                    signal_payload = self._build_tv_signal_compare_payload(
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        signal,
                        request["source_environment"],
                        daily_fields,
                    )
                    if state.get("symbol_tv_parity") is not None and compare_tv_signals:
                        self._compare_generated_signal(state["symbol_tv_parity"], signal_payload)
                    signal_row = self._build_backtest_signal_row(request, bar, signal_payload)
                    signal_row["status"] = "generated"
                    all_signal_rows.append(signal_row)
                    signal_id = str(signal_row.get("signal_id", "") or "")
                    if signal_id:
                        signal_index[signal_id] = signal_row

                    if not trading_day_enabled:
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "symbol_not_selected_for_day")
                    elif not sd_admitted_for_bar:
                        self._mark_backtest_signal_status(
                            signal_index,
                            signal_id,
                            "skipped",
                            "before_sd_admission",
                            {"sd_admitted_at_ms": admitted_after_ms},
                        )
                        self._portfolio_record_rejection(ledger, "before_sd_admission")
                    elif not self._portfolio_bar_in_trade_window(bar, request):
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "outside_trade_window")
                        self._portfolio_record_rejection(ledger, "outside_trade_window")
                    elif not self._portfolio_bar_in_order_window(bar, request):
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "outside_order_window")
                        self._portfolio_record_rejection(ledger, "outside_order_window")
                    elif self._portfolio_sl_circuit_breaker_active(ledger, request):
                        details = {
                            "consecutive_stop_loss_count": int(ledger.get("consecutive_stop_loss_count", 0) or 0),
                            "consecutive_stop_loss_limit": self._portfolio_consecutive_stop_loss_limit(request),
                        }
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "sl_circuit_breaker", details)
                        self._portfolio_record_rejection(ledger, "sl_circuit_breaker")
                    elif self._backtest_cooldown_active(state, bar)[0]:
                        _, cooldown_reason = self._backtest_cooldown_active(state, bar)
                        self._mark_backtest_signal_status(signal_index, signal_id, "skipped", cooldown_reason)
                        self._portfolio_record_rejection(ledger, cooldown_reason)
                    elif preexisting_capacity_wait_candidate:
                        self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "capacity_wait_exists")
                    else:
                        active_target = preexisting_pending_signal if preexisting_pending_signal else preexisting_open_position
                        active_state = "pending_entry" if preexisting_pending_signal else ("filled_position" if preexisting_open_position else "")
                        active_direction = str((active_target or {}).get("direction", "") or "").strip().lower()
                        new_direction = str(signal_payload.get("direction", "") or "").strip().lower()
                        is_last_bar = bool(bar.get("_backtest_is_last_bar"))
                        if active_target or is_last_bar:
                            if active_target and active_direction and new_direction and new_direction != active_direction:
                                reverse_row = self._build_backtest_reverse_signal_row(
                                    request,
                                    symbol,
                                    bar,
                                    state["engine"].bar_count,
                                    snapshot,
                                    daily_fields,
                                    active_target,
                                    target_state=active_state,
                                    reverse_kind="signal_conflict",
                                    source="signal",
                                    origin_signal_payload=signal_payload,
                                )
                                reverse_key = self._build_backtest_reverse_key(reverse_row)
                                if reverse_row and reverse_key not in state["reverse_index"]:
                                    all_reverse_rows.append(reverse_row)
                                    state["reverse_index"].add(reverse_key)
                                    signal_conflict_emitted = True
                                    if preexisting_pending_signal and active_state == "pending_entry":
                                        updated_pending = self._apply_backtest_pending_reverse_action(
                                            state["pending_signal"],
                                            reverse_row,
                                            signal_index,
                                        )
                                        if updated_pending is None:
                                            self._release_portfolio_pending(ledger, state["pending_signal"])
                                        state["pending_signal"] = updated_pending
                                    elif preexisting_open_position and active_state == "filled_position":
                                        original_position = state["open_position"]
                                        state["open_position"], reverse_trade = self._apply_backtest_position_reverse_action(
                                            state["open_position"],
                                            reverse_row,
                                            bar,
                                            commission_per_share,
                                            slippage_bps,
                                            execution_profile,
                                        )
                                        if reverse_trade:
                                            append_trade(original_position, reverse_trade)
                                            self._start_backtest_cooldown(
                                                state,
                                                bar,
                                                int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                                "cooldown_after_reverse_close",
                                            )
                            if active_target:
                                drop_reason = "active_target_exists"
                                if active_direction and new_direction and new_direction != active_direction:
                                    drop_reason = "signal_conflict_active_target"
                                self._mark_backtest_signal_status(signal_index, signal_id, "dropped", drop_reason)
                            elif is_last_bar:
                                self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "last_bar_no_entry")
                        else:
                            if force_flat_eod and not is_last_bar:
                                next_day = str(bar.get("_backtest_next_day", "") or "")[:10]
                                if next_day != current_symbol_day:
                                    self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "force_flat_eod")
                                else:
                                    candidates.append(
                                        self._build_portfolio_candidate(
                                            state,
                                            bar,
                                            index,
                                            signal,
                                            signal_payload,
                                            signal_row,
                                            current_symbol_day,
                                            target_lookup,
                                            execution_profile,
                                        )
                                    )
                            else:
                                candidates.append(
                                    self._build_portfolio_candidate(
                                        state,
                                        bar,
                                        index,
                                        signal,
                                        signal_payload,
                                        signal_row,
                                        current_symbol_day,
                                        target_lookup,
                                        execution_profile,
                                    )
                                )

                if not signal_conflict_emitted and preexisting_pending_signal and state.get("pending_signal") and state.get("open_position") is None:
                    reverse_row = self._build_backtest_reverse_signal_row(
                        request,
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        daily_fields,
                        state["pending_signal"],
                        target_state="pending_entry",
                        reverse_kind="indicator_conflict",
                        source="indicator",
                    )
                    reverse_key = self._build_backtest_reverse_key(reverse_row)
                    if reverse_row and reverse_key not in state["reverse_index"]:
                        all_reverse_rows.append(reverse_row)
                        state["reverse_index"].add(reverse_key)
                        updated_pending = self._apply_backtest_pending_reverse_action(
                            state["pending_signal"],
                            reverse_row,
                            signal_index,
                        )
                        if updated_pending is None:
                            self._release_portfolio_pending(ledger, state["pending_signal"])
                        state["pending_signal"] = updated_pending

                if not signal_conflict_emitted and preexisting_open_position and state.get("open_position"):
                    reverse_row = self._build_backtest_reverse_signal_row(
                        request,
                        symbol,
                        bar,
                        state["engine"].bar_count,
                        snapshot,
                        daily_fields,
                        state["open_position"],
                        target_state="filled_position",
                        reverse_kind="indicator_conflict",
                        source="indicator",
                    )
                    reverse_key = self._build_backtest_reverse_key(reverse_row)
                    if reverse_row and reverse_key not in state["reverse_index"]:
                        all_reverse_rows.append(reverse_row)
                        state["reverse_index"].add(reverse_key)
                        original_position = state["open_position"]
                        state["open_position"], reverse_trade = self._apply_backtest_position_reverse_action(
                            state["open_position"],
                            reverse_row,
                            bar,
                            commission_per_share,
                            slippage_bps,
                            execution_profile,
                        )
                        if reverse_trade:
                            append_trade(original_position, reverse_trade)
                            self._start_backtest_cooldown(
                                state,
                                bar,
                                int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                "cooldown_after_reverse_close",
                            )

                if state.get("open_position"):
                    state["open_position"] = self._maybe_apply_backtest_atr_stop(
                        state["open_position"],
                        snapshot,
                        request,
                    )
                    harvest_trade = self._maybe_close_backtest_intraday_harvest(
                        state["open_position"],
                        bar,
                        commission_per_share,
                        slippage_bps,
                        execution_profile,
                    )
                    if harvest_trade:
                        append_trade(state["open_position"], harvest_trade)
                        state["open_position"] = None
                        continue
                    time_stop_trade = self._maybe_close_backtest_exit_policy_time_stop(
                        state["open_position"],
                        bar,
                        commission_per_share,
                        slippage_bps,
                        execution_profile,
                    )
                    if time_stop_trade:
                        append_trade(state["open_position"], time_stop_trade)
                        state["open_position"] = None

                state["previous_bar"] = bar

            for candidate in self._sort_portfolio_candidates(candidates, request):
                self._accept_portfolio_candidate(ledger, candidate, signal_index, request)

        for symbol in sorted(states.keys()):
            state = states[symbol]
            if state.get("open_position"):
                last_bar = state.get("last_bar") or state.get("previous_bar") or {}
                trade = self._close_position(
                    state["open_position"],
                    last_bar,
                    commission_per_share,
                    slippage_bps,
                    "last_bar",
                    execution_profile,
                )
                append_trade(state["open_position"], trade)
                state["open_position"] = None
            if state.get("pending_signal"):
                last_bar = state.get("last_bar") or state.get("previous_bar") or {}
                self._mark_backtest_signal_status(
                    signal_index,
                    state["pending_signal"].get("signal_id"),
                    "dropped",
                    "last_bar_no_entry",
                    {
                        "event_bar_ms": int(last_bar.get("bar_time_ms", 0) or 0),
                        "event_us_time": str(last_bar.get("us_time", "") or ""),
                        "event_cn_time": str(last_bar.get("cn_time", "") or ""),
                    },
                )
                self._release_portfolio_pending(ledger, state["pending_signal"])
                state["pending_signal"] = None
            if state.get("capacity_wait_candidate"):
                last_bar = state.get("last_bar") or state.get("previous_bar") or {}
                if self._portfolio_signal_expired(
                    (state["capacity_wait_candidate"] or {}).get("signal_payload") or {},
                    last_bar,
                    request,
                ):
                    self._mark_capacity_wait_expired(
                        ledger,
                        state["capacity_wait_candidate"],
                        signal_index,
                        last_bar,
                        request,
                    )
                else:
                    details = {
                        **self._portfolio_capacity_details(ledger, request),
                        "event_bar_ms": int(last_bar.get("bar_time_ms", 0) or 0),
                        "event_us_time": str(last_bar.get("us_time", "") or ""),
                        "event_cn_time": str(last_bar.get("cn_time", "") or ""),
                    }
                    self._mark_backtest_signal_status(
                        signal_index,
                        state["capacity_wait_candidate"].get("signal_id"),
                        "dropped",
                        "capacity_wait_open_at_backtest_end",
                        details,
                    )
                    self._portfolio_record_rejection(ledger, "capacity_wait_open_at_backtest_end")
                    self._portfolio_record_candidate_sample(
                        ledger,
                        state["capacity_wait_candidate"],
                        "dropped",
                        "capacity_wait_open_at_backtest_end",
                        details,
                    )
                state["capacity_wait_candidate"] = None

            quality = {
                "symbol": symbol,
                "bar_count": int(state.get("bar_count", 0) or 0),
                "gap_count": int(state.get("gap_count", 0) or 0),
                "status": "ok",
                "first_bar_us": str(state.get("first_bar_us", "") or ""),
                "last_bar_us": str(state.get("last_bar_us", "") or ""),
                "market_bars": int(state.get("market_bars", 0) or 0),
                "selected_trade_day_count": len(state.get("allowed_trade_days") or []),
            }
            data_quality.append(quality)
            if state.get("symbol_tv_parity") is not None:
                self._finalize_symbol_tv_parity(state["symbol_tv_parity"])
                tv_symbol_reports.append(state["symbol_tv_parity"])

        all_trades.sort(key=lambda item: (int(item.get("exit_bar_ms", 0) or 0), item.get("symbol", "")))
        all_reverse_rows.sort(key=lambda item: (int(item.get("bar_time_ms", 0) or 0), item.get("symbol", ""), item.get("action_type", "")))
        gross_now = float(ledger.get("open_exposure", 0) or 0) + float(ledger.get("reserved_exposure", 0) or 0)
        resource_snapshot = self._read_backtest_resource_snapshot()
        portfolio_metrics = {
            "execution_model": "portfolio_stream",
            "execution_cost_profile": compact_execution_cost_profile(execution_profile),
            "portfolio_profile": {
                "symbols_requested": len(symbols),
                "symbols_loaded": len(states),
                "bars_loaded": total_loaded_bars,
                "bar_times": len(bar_times),
                "load_duration_s": load_duration_s,
                "stream_duration_s": round(time.time() - stream_started_at, 3),
                "compact_bar_storage": True,
                "resource_snapshot": resource_snapshot,
            },
            "portfolio_risk": {
                **risk_limits,
                "execution_cost_profile": compact_execution_cost_profile(execution_profile),
                "position_limit_max": int(request.get("position_limit_max", DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX) or 0),
                "max_strategy_open_positions": self._max_strategy_open_positions(request),
                "strategy_open_position_slots": int(ledger.get("open_position_slots", 0) or 0),
                "strategy_pending_entry_slots": int(ledger.get("pending_entry_slots", 0) or 0),
                "consecutive_stop_loss_limit": self._portfolio_consecutive_stop_loss_limit(request),
                "consecutive_stop_loss_count": int(ledger.get("consecutive_stop_loss_count", 0) or 0),
                "max_consecutive_stop_loss_count": int(ledger.get("max_consecutive_stop_loss_count", 0) or 0),
                "portfolio_require_target_direction_alignment": self._normalize_bool(
                    request.get("portfolio_require_target_direction_alignment"),
                    False,
                ),
                "portfolio_use_target_strategy_policy": self._normalize_bool(
                    request.get("portfolio_use_target_strategy_policy"),
                    False,
                ),
                "portfolio_max_target_rank": int(request.get("portfolio_max_target_rank", 0) or 0),
                "portfolio_min_target_score": self._coerce_float_value(request.get("portfolio_min_target_score"), 0.0),
                "portfolio_block_mr_overextended_state": self._normalize_bool(
                    request.get("portfolio_block_mr_overextended_state"),
                    False,
                ),
                "portfolio_block_early_trend_without_ema_touch": self._normalize_bool(
                    request.get("portfolio_block_early_trend_without_ema_touch"),
                    False,
                ),
                "signal_validity_minutes": int(request.get("signal_validity_minutes", DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES) or DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES),
                "trade_window_start_time": str(request.get("trade_window_start_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_START),
                "trade_window_end_time": str(request.get("trade_window_end_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_END),
                "order_window_end_time": str(request.get("order_window_end_time") or DEFAULT_PORTFOLIO_ORDER_WINDOW_END),
                "simultaneous_signal_priority": str(request.get("simultaneous_signal_priority") or "daily_target_rank"),
                "manual_confirm_mode": str(request.get("manual_confirm_mode") or "auto"),
                "confirm_delay_minutes": int(request.get("confirm_delay_minutes", 0) or 0),
            },
            "portfolio_rejection_counts": dict(ledger.get("rejection_counts") or {}),
            "portfolio_candidate_samples": list(ledger.get("candidate_samples") or []),
            "portfolio_max_gross_exposure": round(float(ledger.get("max_gross_exposure", 0) or 0), 4),
            "portfolio_max_borrowed_amount": round(float(ledger.get("max_borrowed_amount", 0) or 0), 4),
            "portfolio_final_open_exposure": round(float(ledger.get("open_exposure", 0) or 0), 4),
            "portfolio_final_reserved_exposure": round(float(ledger.get("reserved_exposure", 0) or 0), 4),
            "portfolio_final_gross_exposure": round(gross_now, 4),
            "portfolio_realized_pnl": round(float(ledger.get("realized_pnl", 0) or 0), 4),
        }
        result = {
            "trades": all_trades,
            "indicator_rows": all_indicator_rows,
            "indicator_count": indicator_count,
            "signal_rows": all_signal_rows,
            "reverse_rows": all_reverse_rows,
            "data_quality": data_quality,
            "skipped_symbols": skipped_symbols,
            "tv_symbol_reports": tv_symbol_reports,
            "portfolio_metrics": portfolio_metrics,
        }
        self._clear_portfolio_working_sets(states, bars_by_time, signal_index, target_lookup)
        return result
