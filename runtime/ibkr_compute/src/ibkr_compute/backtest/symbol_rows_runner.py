from __future__ import annotations

from .runtime_support import *


class BacktestSymbolRowsRunnerMixin:
    def _run_symbol_backtest(
        self,
        symbol: str,
        bars: list[dict],
        request: dict,
        allowed_trade_days: set[str] | None = None,
    ) -> tuple[list[dict], dict, dict | None, list[dict], list[dict], list[dict], int]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        slippage_bps = float(request["slippage_bps"])
        commission_per_share = float(request["commission_per_share"])
        execution_profile = build_execution_cost_profile(
            request,
            commission_per_share=commission_per_share,
            slippage_bps=slippage_bps,
        )
        force_flat_eod = bool(request["force_flat_eod"])
        engine = runtime_indicator_engine()(symbol, "5m", params=params)
        signal_gen = runtime_signal_generator()(symbol, "5m", params=params)
        compare_with_tv = self._should_compare_with_tv(request)
        compare_tv_signals = self._should_compare_tv_signals(request)
        capture_indicator_rows = self._should_persist_backtest_indicators(request)
        tv_reference = self._load_tv_reference(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
            include_signals=compare_tv_signals,
        ) if compare_with_tv else {}
        daily_close_lookup = self._load_daily_close_lookup(
            symbol,
            request["source_environment"],
            request["date_from"],
            request["date_to"],
        )
        symbol_tv_parity = self._init_symbol_tv_parity(
            symbol,
            tv_reference,
            compare_tv_signals=compare_tv_signals,
        ) if compare_with_tv else None
        trades = []
        indicator_rows = []
        indicator_count = 0
        signal_rows = []
        reverse_rows = []
        signal_index = {}
        reverse_index = set()
        gap_count = 0
        market_bars = 0
        previous_ms = 0
        previous_day = ""
        pending_signal = None
        open_position = None
        cooldown_state = {"cooldown_until_ms": 0, "cooldown_reason": ""}

        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0) if bars else 0,
            request["session_mode"],
            limit=self._effective_indicator_warmup_bars(request, "warmup_bars"),
        )
        if warmup_bars:
            previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars)

        precheck_gap_count = self._count_internal_5m_gaps(bars)
        if precheck_gap_count > 0:
            quality = {
                "symbol": symbol,
                "bar_count": len(bars),
                "gap_count": precheck_gap_count,
                "status": "invalid_gap",
                "first_bar_us": bars[0].get("us_time", "") if bars else "",
                "last_bar_us": bars[-1].get("us_time", "") if bars else "",
                "market_bars": len(bars),
                "selected_trade_day_count": len(allowed_trade_days or []),
            }
            return [], quality, symbol_tv_parity, [], [], [], 0

        for index, bar in enumerate(bars):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            market_bars += 1
            current_day = str(bar.get("us_time", "") or "")[:10]

            if previous_ms > 0 and int(bar["bar_time_ms"]) - previous_ms > interval_to_ms("5m") * 3:
                gap_count += 1
            previous_ms = int(bar["bar_time_ms"])

            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
                cooldown_state["cooldown_until_ms"] = 0
                cooldown_state["cooldown_reason"] = ""
                if open_position and force_flat_eod:
                    exit_trade = self._close_position(open_position, bars[index - 1], commission_per_share, slippage_bps, "eod", execution_profile)
                    trades.append(exit_trade)
                    open_position = None
                if pending_signal:
                    self._mark_backtest_signal_status(
                        signal_index,
                        pending_signal.get("signal_id"),
                        "dropped",
                        "new_day_reset",
                        {
                            "event_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                            "event_us_time": str(bar.get("us_time", "") or ""),
                            "event_cn_time": str(bar.get("cn_time", "") or ""),
                        },
                    )
                pending_signal = None
            previous_day = current_day

            if pending_signal and open_position is None:
                filled_position = self._check_pending_entry_fill(
                    symbol,
                    bar,
                    pending_signal,
                    commission_per_share,
                    slippage_bps,
                    execution_profile,
                )
                if filled_position:
                    open_position = filled_position
                    self._mark_backtest_signal_status(
                        signal_index,
                        pending_signal.get("signal_id"),
                        "executed",
                        "entry_limit_filled",
                        {
                            "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
                            "entry_us_time": str(bar.get("us_time", "") or ""),
                            "entry_cn_time": str(bar.get("cn_time", "") or ""),
                            "entry_price": round(float(open_position.get("entry_price", 0) or 0), 4),
                            "entry_limit_price": round(float(open_position.get("entry_limit_price", 0) or 0), 4),
                        },
                    )
                    pending_signal = None

            if open_position:
                closed = self._check_exit(open_position, bar, commission_per_share, slippage_bps, execution_profile)
                if closed:
                    trades.append(closed)
                    if str(closed.get("exit_reason") or "") == "stop_loss":
                        self._start_backtest_cooldown(
                            cooldown_state,
                            bar,
                            int(request.get("cooldown_bars_after_sl", 6) or 0),
                            "cooldown_after_stop_loss",
                        )
                    open_position = None

            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue
            indicator_count += 1

            daily_fields = self._get_daily_change_fields_from_lookup(
                daily_close_lookup,
                float(snapshot.get("close", 0) or 0),
                int(bar.get("bar_time_ms", 0) or 0),
            )
            indicator_payload = None
            indicator_audit = None
            if symbol_tv_parity is not None or capture_indicator_rows:
                indicator_payload = self._build_tv_indicator_compare_payload(
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    request["source_environment"],
                    daily_fields,
                )
                if symbol_tv_parity is not None:
                    indicator_audit = self._compare_generated_indicator(symbol_tv_parity, indicator_payload)
                if capture_indicator_rows:
                    indicator_rows.append(
                        self._build_backtest_indicator_row(
                            request,
                            bar,
                            indicator_payload,
                            indicator_audit,
                        )
                    )
            signal_snapshot = {**snapshot, **daily_fields}
            signal = signal_gen.update(signal_snapshot)
            trading_day_enabled = allowed_trade_days is None or current_day in allowed_trade_days
            preexisting_pending_signal = pending_signal if pending_signal and open_position is None else None
            preexisting_open_position = open_position
            signal_conflict_emitted = False

            if signal and int(signal.get("shares", 0) or 0) > 0:
                signal_payload = self._build_tv_signal_compare_payload(
                    symbol,
                    bar,
                    engine.bar_count,
                    signal,
                    request["source_environment"],
                    daily_fields,
                )
                if symbol_tv_parity is not None and compare_tv_signals:
                    self._compare_generated_signal(symbol_tv_parity, signal_payload)
                signal_row = self._build_backtest_signal_row(request, bar, signal_payload)
                signal_row["status"] = "generated"
                signal_rows.append(signal_row)
                signal_id = str(signal_row.get("signal_id", "") or "")
                if signal_id:
                    signal_index[signal_id] = signal_row
                if not trading_day_enabled:
                    self._mark_backtest_signal_status(signal_index, signal_id, "skipped", "symbol_not_selected_for_day")
                elif self._backtest_cooldown_active(cooldown_state, bar)[0]:
                    _, cooldown_reason = self._backtest_cooldown_active(cooldown_state, bar)
                    self._mark_backtest_signal_status(signal_index, signal_id, "skipped", cooldown_reason)
                else:
                    active_target = preexisting_pending_signal if preexisting_pending_signal else preexisting_open_position
                    active_state = "pending_entry" if preexisting_pending_signal else ("filled_position" if preexisting_open_position else "")
                    active_direction = str((active_target or {}).get("direction", "") or "").strip().lower()
                    new_direction = str(signal_payload.get("direction", "") or "").strip().lower()
                    if active_target or index >= len(bars) - 1:
                        if active_target and active_direction and new_direction and new_direction != active_direction:
                            reverse_row = self._build_backtest_reverse_signal_row(
                                request,
                                symbol,
                                bar,
                                engine.bar_count,
                                snapshot,
                                daily_fields,
                                active_target,
                                target_state=active_state,
                                reverse_kind="signal_conflict",
                                source="signal",
                                origin_signal_payload=signal_payload,
                            )
                            reverse_key = self._build_backtest_reverse_key(reverse_row)
                            if reverse_row and reverse_key not in reverse_index:
                                reverse_rows.append(reverse_row)
                                reverse_index.add(reverse_key)
                                signal_conflict_emitted = True
                                if preexisting_pending_signal and active_state == "pending_entry":
                                    pending_signal = self._apply_backtest_pending_reverse_action(
                                        pending_signal,
                                        reverse_row,
                                        signal_index,
                                    )
                                elif preexisting_open_position and active_state == "filled_position":
                                    open_position, reverse_trade = self._apply_backtest_position_reverse_action(
                                        open_position,
                                        reverse_row,
                                        bar,
                                        commission_per_share,
                                        slippage_bps,
                                        execution_profile,
                                    )
                                    if reverse_trade:
                                        trades.append(reverse_trade)
                                        self._start_backtest_cooldown(
                                            cooldown_state,
                                            bar,
                                            int(request.get("cooldown_bars_after_reverse", 3) or 0),
                                            "cooldown_after_reverse_close",
                                        )
                        if active_target:
                            drop_reason = "active_target_exists"
                            if active_direction and new_direction and new_direction != active_direction:
                                drop_reason = "signal_conflict_active_target"
                            self._mark_backtest_signal_status(signal_index, signal_id, "dropped", drop_reason)
                        elif index >= len(bars) - 1:
                            self._mark_backtest_signal_status(signal_index, signal_id, "dropped", "last_bar_no_entry")
                    else:
                        pending_signal = self._build_backtest_pending_signal(
                            symbol,
                            bar,
                            signal,
                            signal_payload,
                        )
                        self._mark_backtest_signal_status(
                            signal_index,
                            pending_signal.get("signal_id"),
                            "pending",
                            "accepted_pending_entry",
                            {
                                "confirm_ready_bar_ms": int(pending_signal.get("signal_bar_ms", 0) or 0),
                                "validity_minutes": self._portfolio_signal_validity_minutes(pending_signal, request),
                            },
                        )

                        if force_flat_eod and index < len(bars) - 1:
                            next_day = str(bars[index + 1].get("us_time", "") or "")[:10]
                            if next_day != current_day:
                                self._mark_backtest_signal_status(signal_index, pending_signal.get("signal_id"), "dropped", "force_flat_eod")
                                pending_signal = None

            if not signal_conflict_emitted and preexisting_pending_signal and pending_signal and open_position is None:
                reverse_row = self._build_backtest_reverse_signal_row(
                    request,
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    daily_fields,
                    pending_signal,
                    target_state="pending_entry",
                    reverse_kind="indicator_conflict",
                    source="indicator",
                )
                reverse_key = self._build_backtest_reverse_key(reverse_row)
                if reverse_row and reverse_key not in reverse_index:
                    reverse_rows.append(reverse_row)
                    reverse_index.add(reverse_key)
                    pending_signal = self._apply_backtest_pending_reverse_action(
                        pending_signal,
                        reverse_row,
                        signal_index,
                    )

            if not signal_conflict_emitted and preexisting_open_position and open_position:
                reverse_row = self._build_backtest_reverse_signal_row(
                    request,
                    symbol,
                    bar,
                    engine.bar_count,
                    snapshot,
                    daily_fields,
                    open_position,
                    target_state="filled_position",
                    reverse_kind="indicator_conflict",
                    source="indicator",
                )
                reverse_key = self._build_backtest_reverse_key(reverse_row)
                if reverse_row and reverse_key not in reverse_index:
                    reverse_rows.append(reverse_row)
                    reverse_index.add(reverse_key)
                    open_position, reverse_trade = self._apply_backtest_position_reverse_action(
                        open_position,
                        reverse_row,
                        bar,
                        commission_per_share,
                        slippage_bps,
                        execution_profile,
                    )
                    if reverse_trade:
                        trades.append(reverse_trade)
                        self._start_backtest_cooldown(
                            cooldown_state,
                            bar,
                            int(request.get("cooldown_bars_after_reverse", 3) or 0),
                            "cooldown_after_reverse_close",
                        )

            if open_position:
                open_position = self._maybe_apply_backtest_atr_stop(open_position, snapshot, request)
                harvest_trade = self._maybe_close_backtest_intraday_harvest(
                    open_position,
                    bar,
                    commission_per_share,
                    slippage_bps,
                    execution_profile,
                )
                if harvest_trade:
                    trades.append(harvest_trade)
                    open_position = None
                    continue
                time_stop_trade = self._maybe_close_backtest_exit_policy_time_stop(
                    open_position,
                    bar,
                    commission_per_share,
                    slippage_bps,
                    execution_profile,
                )
                if time_stop_trade:
                    trades.append(time_stop_trade)
                    open_position = None

        if open_position:
            trades.append(self._close_position(open_position, bars[-1], commission_per_share, slippage_bps, "last_bar", execution_profile))
        if pending_signal:
            last_bar = bars[-1] if bars else {}
            self._mark_backtest_signal_status(
                signal_index,
                pending_signal.get("signal_id"),
                "dropped",
                "last_bar_no_entry",
                {
                    "event_bar_ms": int(last_bar.get("bar_time_ms", 0) or 0),
                    "event_us_time": str(last_bar.get("us_time", "") or ""),
                    "event_cn_time": str(last_bar.get("cn_time", "") or ""),
                },
            )

        quality = {
            "symbol": symbol,
            "bar_count": len(bars),
            "gap_count": gap_count,
            "status": "ok",
            "first_bar_us": bars[0].get("us_time", "") if bars else "",
            "last_bar_us": bars[-1].get("us_time", "") if bars else "",
            "market_bars": market_bars,
            "selected_trade_day_count": len(allowed_trade_days or []),
        }
        if symbol_tv_parity is not None:
            self._finalize_symbol_tv_parity(symbol_tv_parity)
        return trades, quality, symbol_tv_parity, indicator_rows, signal_rows, reverse_rows, indicator_count
