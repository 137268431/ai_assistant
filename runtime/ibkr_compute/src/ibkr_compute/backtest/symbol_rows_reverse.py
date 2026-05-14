from __future__ import annotations

from .runtime_support import *


class BacktestSymbolRowsReverseMixin:
    def _map_reverse_strength(self, score: float) -> str:
        safe_score = float(score or 0)
        if safe_score >= 6:
            return "strong"
        if safe_score >= 3:
            return "medium"
        return "weak"

    def _resolve_reverse_indicator_action(
        self,
        score: float,
        target_state: str,
        progress_ratio: float = 0.0,
    ) -> str:
        if target_state == "pending_entry":
            return "cancel"
        if score >= 6:
            return "close"
        if score >= 3:
            if float(progress_ratio or 0) >= 0.7:
                return "adjust_tp"
            return "adjust_sl"
        if score > 0:
            return "cancel"
        return ""

    def _analyze_backtest_reverse_snapshot(self, snapshot: dict, direction: str) -> dict:
        if not snapshot or direction not in {"long", "short"}:
            return {"score": 0.0, "triggered_signals": []}

        score = 0.0
        triggered_signals = []
        crsi = float(snapshot.get("crsi", 0) or 0)
        vwap_dist = float(snapshot.get("vwap_dist", 0) or 0)
        is_bear_signal = direction == "long"
        crsi_reverse_div = bool(snapshot.get("crsi_bear_div" if is_bear_signal else "crsi_bull_div"))
        obv_reverse_div = bool(snapshot.get("obv_bear_div" if is_bear_signal else "obv_bull_div"))
        fractal_reverse = bool(snapshot.get("fractal_bear" if is_bear_signal else "fractal_bull"))
        sd_channel_reverse = bool(snapshot.get("sd_upper" if is_bear_signal else "sd_lower"))
        ema_touch_reverse = bool(snapshot.get("ema_bear_touch" if is_bear_signal else "ema_bull_touch"))

        if (direction == "long" and crsi > 70) or (direction == "short" and crsi < 30):
            score += 2
            triggered_signals.append("cRSI超买" if direction == "long" else "cRSI超卖")
        if crsi_reverse_div or obv_reverse_div:
            score += 3
            triggered_signals.append("背离")
        if fractal_reverse or sd_channel_reverse:
            score += 2
            triggered_signals.append("分形/SD通道")
        if abs(vwap_dist) > 2 or ema_touch_reverse:
            score += 1
            triggered_signals.append("VWAP偏离/EMA触碰")

        deduped = []
        seen = set()
        for item in triggered_signals:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return {
            "score": score,
            "triggered_signals": deduped,
            "crsi": crsi,
            "obv_rsi": float(snapshot.get("obv_rsi", 0) or 0),
            "vwap_dist": vwap_dist,
            "close": float(snapshot.get("close", 0) or 0),
        }

    def _build_backtest_reverse_key(self, reverse_row: dict | None) -> str:
        if not reverse_row:
            return ""
        signal_id = str(reverse_row.get("signal_id", "") or "").strip()
        trade_group_id = str(reverse_row.get("trade_group_id", "") or "").strip()
        target_state = str(reverse_row.get("target_state", "") or "").strip()
        action_type = str(reverse_row.get("action_type", "") or "").strip()
        direction = str(reverse_row.get("direction", "") or "").strip()
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip()
        origin_signal_id = str(reverse_row.get("origin_signal_id", "") or "").strip()
        new_direction = str(self._parse_object(reverse_row.get("extra")).get("new_direction", "") or "").strip()
        return "|".join(
            [
                signal_id or trade_group_id or str(reverse_row.get("symbol", "") or "").strip().upper(),
                direction,
                target_state,
                action_type,
                reverse_kind,
                origin_signal_id,
                new_direction,
            ]
        )

    def _build_backtest_pending_signal(
        self,
        symbol: str,
        bar: dict,
        signal: dict,
        signal_payload: dict,
    ) -> dict:
        signal_id = signal_payload.get("signal_id", build_signal_id(symbol, int(bar["bar_time_ms"]), str(signal.get("signal", ""))))
        extra = self._parse_object(signal_payload.get("extra"))
        entry_order_type = str(
            extra.get("entry_order_type") or signal_payload.get("entry_order_type") or signal.get("entry_order_type") or "limit"
        ).strip().lower()
        if entry_order_type not in {"limit", "marketable_limit"}:
            entry_order_type = "limit"
        return {
            **signal,
            "symbol": symbol,
            "signal_id": signal_id,
            "extra": extra,
            "signal_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "signal_us_time": str(bar.get("us_time", "") or ""),
            "signal_cn_time": str(bar.get("cn_time", "") or ""),
            "signal_close": float(bar.get("close", 0) or 0),
            "reason": str(signal.get("reason", "") or ""),
            "chart_tf": interval_to_chart_tf("5m"),
            "entry_price": float(signal.get("entry", 0) or 0),
            "target_price": float(signal.get("take_profit", 0) or 0),
            "stop_price": float(signal.get("stop_loss", 0) or 0),
            "risk_r": float(signal.get("risk_r", extra.get("risk_r", 0)) or 0),
            "initial_stop_loss": float(signal.get("initial_stop_loss", extra.get("initial_stop_loss", signal.get("stop_loss", 0))) or 0),
            "initial_take_profit": float(signal.get("initial_take_profit", extra.get("initial_take_profit", signal.get("take_profit", 0))) or 0),
            "exit_policy_profile": str(extra.get("exit_policy_profile") or signal.get("exit_policy_profile", "") or ""),
            "exit_policy": str(extra.get("exit_policy") or signal.get("exit_policy", "") or ""),
            "exit_policy_type": str(extra.get("exit_policy_type") or signal.get("exit_policy_type", "") or ""),
            "exit_policy_settings": dict(extra.get("exit_policy_settings") or {}),
            "trail_state": dict(extra.get("trail_state") or {}),
            "entry_order_type": entry_order_type,
            "setup": str(extra.get("setup") or signal_payload.get("setup") or signal.get("setup") or signal.get("signal", "") or "").strip(),
            "pending_since_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "pending_since_us_time": str(bar.get("us_time", "") or ""),
        }

    def _check_pending_entry_fill(
        self,
        symbol: str,
        bar: dict,
        pending_signal: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> Optional[dict]:
        direction = str(pending_signal.get("direction", "") or "").strip().lower()
        entry_price = float(
            pending_signal.get("entry_price", pending_signal.get("entry", 0)) or 0
        )
        if direction not in {"long", "short"} or entry_price <= 0:
            return None

        bar_open = float(bar.get("open", 0) or 0)
        bar_high = float(bar.get("high", 0) or 0)
        bar_low = float(bar.get("low", 0) or 0)
        raw_fill_price = 0.0
        entry_order_type = str(pending_signal.get("entry_order_type") or "limit").strip().lower()
        if direction == "long":
            if bar_open > 0 and bar_open <= entry_price:
                raw_fill_price = bar_open
            elif bar_low <= entry_price <= max(bar_high, bar_open):
                raw_fill_price = entry_price
        else:
            if bar_open > 0 and bar_open >= entry_price:
                raw_fill_price = bar_open
            elif min(bar_low, bar_open) <= entry_price <= bar_high:
                raw_fill_price = entry_price

        if raw_fill_price <= 0:
            return None
        position = self._open_position(
            symbol,
            bar,
            pending_signal,
            commission_per_share,
            slippage_bps,
            raw_fill_price=raw_fill_price,
            execution_profile=execution_profile,
        )
        if position:
            position["entry_order_type"] = entry_order_type
            setup_meta = build_setup_metadata(
                pending_signal.get("setup") or pending_signal.get("signal", ""),
                fallback_signal=pending_signal.get("signal", ""),
                direction=pending_signal.get("direction", ""),
                signal_mode=pending_signal.get("signal_mode") or self._parse_object(pending_signal.get("extra")).get("signal_mode", ""),
                strategy_profile=self._parse_object(pending_signal.get("extra")).get("strategy_profile", ""),
                setup_priority=self._parse_object(pending_signal.get("extra")).get("setup_priority"),
                exit_policy_type=pending_signal.get("exit_policy_type") or self._parse_object(pending_signal.get("extra")).get("exit_policy_type", ""),
            )
            position.update(setup_meta)
        return position

    def _calculate_position_progress(self, target: dict, current_price: float) -> dict:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        target_price = float(target.get("target_price", target.get("take_profit", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or target_price <= 0:
            return {"favorable_move": 0.0, "target_move": 0.0, "progress_ratio": 0.0}

        if direction == "long":
            favorable_move = current_price - entry_price
            target_move = target_price - entry_price
        else:
            favorable_move = entry_price - current_price
            target_move = entry_price - target_price
        progress_ratio = (favorable_move / target_move) if abs(target_move) > 1e-9 else 0.0
        return {
            "favorable_move": round(float(favorable_move or 0), 4),
            "target_move": round(float(target_move or 0), 4),
            "progress_ratio": round(float(progress_ratio or 0), 4),
        }

    def _compute_adjusted_stop_price(self, target: dict, current_price: float) -> float:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        stop_price = float(target.get("stop_price", target.get("stop_loss", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or stop_price <= 0:
            return 0.0
        price_buffer = max(0.01, current_price * 0.001)
        if direction == "long":
            favorable_move = max(0.0, current_price - entry_price)
            tightened = entry_price + (favorable_move * 0.35)
            candidate = max(stop_price, tightened)
            candidate = min(candidate, current_price - price_buffer)
        else:
            favorable_move = max(0.0, entry_price - current_price)
            tightened = entry_price - (favorable_move * 0.35)
            candidate = min(stop_price, tightened)
            candidate = max(candidate, current_price + price_buffer)
        return round(float(candidate or 0), 4)

    def _compute_adjusted_take_profit_price(self, target: dict, current_price: float) -> float:
        direction = str(target.get("direction", "") or "").strip().lower()
        entry_price = float(target.get("entry_price", target.get("entry", 0)) or 0)
        take_profit = float(target.get("target_price", target.get("take_profit", 0)) or 0)
        if direction not in {"long", "short"} or entry_price <= 0 or take_profit <= 0:
            return 0.0
        price_buffer = max(0.01, current_price * 0.001)
        if direction == "long":
            if take_profit <= current_price:
                return round(take_profit, 4)
            gap = take_profit - current_price
            candidate = current_price + max(price_buffer, gap * 0.35)
            candidate = min(take_profit, max(entry_price + price_buffer, candidate))
        else:
            if take_profit >= current_price:
                return round(take_profit, 4)
            gap = current_price - take_profit
            candidate = current_price - max(price_buffer, gap * 0.35)
            candidate = max(take_profit, min(entry_price - price_buffer, candidate))
        return round(float(candidate or 0), 4)

    def _build_backtest_reverse_price_patch(self, target: dict, action_type: str, current_price: float) -> dict:
        if action_type == "adjust_sl":
            old_sl = float(target.get("stop_price", target.get("stop_loss", 0)) or 0)
            new_sl = self._compute_adjusted_stop_price(target, current_price)
            if old_sl > 0 and new_sl > 0 and abs(new_sl - old_sl) >= 0.0001:
                return {
                    "old_sl": round(old_sl, 4),
                    "new_sl": round(new_sl, 4),
                    "stop_loss": round(new_sl, 4),
                }
        if action_type == "adjust_tp":
            old_tp = float(target.get("target_price", target.get("take_profit", 0)) or 0)
            new_tp = self._compute_adjusted_take_profit_price(target, current_price)
            if old_tp > 0 and new_tp > 0 and abs(new_tp - old_tp) >= 0.0001:
                return {
                    "old_tp": round(old_tp, 4),
                    "new_tp": round(new_tp, 4),
                    "take_profit": round(new_tp, 4),
                }
        return {}

    def _resolve_reverse_signal_conflict_action(self, target_state: str, progress_ratio: float) -> str:
        if target_state == "pending_entry":
            return "cancel"
        return "close"

    def _build_backtest_signal_conflict_analysis(
        self,
        target: dict,
        target_state: str,
        origin_signal_payload: dict,
        current_price: float,
    ) -> dict:
        progress = self._calculate_position_progress(target, current_price)
        action_type = self._resolve_reverse_signal_conflict_action(
            target_state,
            progress.get("progress_ratio", 0),
        )
        score_map = {
            "cancel": 2.0,
            "adjust_sl": 4.0,
            "adjust_tp": 5.0,
            "close": 7.0,
        }
        triggered_signals = ["信号反转"]
        price_patch = self._build_backtest_reverse_price_patch(target, action_type, current_price)
        return {
            "score": score_map.get(action_type, 2.0),
            "action_type": action_type,
            "triggered_signals": triggered_signals,
            "progress": progress,
            "price_patch": price_patch,
            "current_price": round(float(current_price or 0), 4),
            "new_direction": str(origin_signal_payload.get("direction", "") or "").strip().lower(),
        }

    def _apply_backtest_pending_reverse_action(
        self,
        pending_signal: dict | None,
        reverse_row: dict | None,
        signal_index: dict,
    ) -> dict | None:
        if not pending_signal or not reverse_row:
            return pending_signal
        action_type = str(reverse_row.get("action_type", "") or "").strip().lower()
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip().lower()
        if action_type == "cancel":
            self._mark_backtest_signal_status(
                signal_index,
                pending_signal.get("signal_id"),
                "dropped",
                f"reverse_{reverse_kind}_{action_type}",
                {
                    "reverse_action_type": action_type,
                    "reverse_kind": reverse_kind,
                    "event_bar_ms": int(reverse_row.get("bar_time_ms", 0) or 0),
                    "event_us_time": str(reverse_row.get("us_time", "") or ""),
                    "event_cn_time": str(reverse_row.get("cn_time", "") or ""),
                },
            )
            return None
        return pending_signal

    def _apply_backtest_position_reverse_action(
        self,
        position: dict | None,
        reverse_row: dict | None,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> tuple[dict | None, dict | None]:
        if not position or not reverse_row:
            return position, None
        action_type = str(reverse_row.get("action_type", "") or "").strip().lower()
        extra = self._parse_object(reverse_row.get("extra"))
        reverse_kind = str(reverse_row.get("reverse_kind", "") or "").strip().lower()
        if action_type == "close":
            trade = self._close_position(position, bar, commission_per_share, slippage_bps, f"reverse_{reverse_kind}_close", execution_profile)
            trade_extra = self._parse_object(trade.get("extra"))
            trade_extra.update(
                {
                    "reverse_action_type": action_type,
                    "reverse_kind": reverse_kind,
                    "origin_signal_id": str(reverse_row.get("origin_signal_id", "") or ""),
                }
            )
            trade["extra"] = trade_extra
            return None, trade
        if action_type == "adjust_sl":
            new_sl = float(extra.get("new_sl", 0) or 0)
            if new_sl > 0:
                old_sl = float(position.get("stop_price", 0) or 0)
                position["stop_price"] = new_sl
                self._append_backtest_risk_adjustment(
                    position,
                    {
                        "event_type": "reverse_adjust_sl",
                        "source": "reverse_signal",
                        "reverse_kind": reverse_kind,
                        "action_type": action_type,
                        "bar_time_ms": int(reverse_row.get("bar_time_ms", 0) or 0),
                        "us_time": str(reverse_row.get("us_time", "") or ""),
                        "cn_time": str(reverse_row.get("cn_time", "") or ""),
                        "old_sl": round(old_sl, 4),
                        "new_sl": round(new_sl, 4),
                        "reason": str(reverse_row.get("reason", "") or ""),
                        "signal_id": str(position.get("signal_id", "") or ""),
                        "origin_signal_id": str(reverse_row.get("origin_signal_id", "") or ""),
                    },
                )
        elif action_type == "adjust_tp":
            new_tp = float(extra.get("new_tp", 0) or 0)
            if new_tp > 0:
                old_tp = float(position.get("target_price", 0) or 0)
                position["target_price"] = new_tp
                self._append_backtest_risk_adjustment(
                    position,
                    {
                        "event_type": "reverse_adjust_tp",
                        "source": "reverse_signal",
                        "reverse_kind": reverse_kind,
                        "action_type": action_type,
                        "bar_time_ms": int(reverse_row.get("bar_time_ms", 0) or 0),
                        "us_time": str(reverse_row.get("us_time", "") or ""),
                        "cn_time": str(reverse_row.get("cn_time", "") or ""),
                        "old_tp": round(old_tp, 4),
                        "new_tp": round(new_tp, 4),
                        "reason": str(reverse_row.get("reason", "") or ""),
                        "signal_id": str(position.get("signal_id", "") or ""),
                        "origin_signal_id": str(reverse_row.get("origin_signal_id", "") or ""),
                    },
                )
        return position, None

    def _build_backtest_reverse_signal_row(
        self,
        request: dict,
        symbol: str,
        bar: dict,
        bar_index: int,
        snapshot: dict,
        daily_fields: dict,
        target: dict,
        target_state: str = "filled_position",
        reverse_kind: str = "indicator_conflict",
        source: str = "indicator",
        origin_signal_payload: dict | None = None,
    ) -> dict | None:
        direction = str(target.get("direction", "") or "").strip().lower()
        if direction not in {"long", "short"}:
            return None
        bar_time_ms = int(bar.get("bar_time_ms", 0) or 0)
        if reverse_kind == "signal_conflict":
            if not origin_signal_payload:
                return None
            new_direction = str(origin_signal_payload.get("direction", "") or "").strip().lower()
            if not new_direction or new_direction == direction:
                return None
            analysis = self._build_backtest_signal_conflict_analysis(
                target,
                target_state,
                origin_signal_payload,
                float(snapshot.get("close", bar.get("close", 0)) or 0),
            )
        else:
            analysis = self._analyze_backtest_reverse_snapshot(snapshot, direction)
            if float(analysis.get("score", 0) or 0) <= 0 or not list(analysis.get("triggered_signals") or []):
                return None
            if target_state == "pending_entry":
                pending_since_ms = int(target.get("pending_since_bar_ms", target.get("signal_bar_ms", 0)) or 0)
                pending_age_bars = 0
                pending_same_day = False
                if pending_since_ms > 0:
                    pending_age_bars = max(
                        0,
                        int((bar_time_ms - pending_since_ms) / max(1, interval_to_ms("5m"))),
                    )
                    pending_same_day = (
                        ms_to_et(pending_since_ms).strftime("%Y-%m-%d")
                        == ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
                    )
                reverse_score = float(analysis.get("score", 0) or 0)
                if pending_age_bars < 2:
                    return None
                pending_late_session = False
                if pending_same_day:
                    pending_bar_et = ms_to_et(bar_time_ms)
                    pending_late_session = pending_bar_et.hour > 16 or (
                        pending_bar_et.hour == 16 and pending_bar_et.minute >= 45
                    )
                # Same-day pending entries should usually wait for a true opposite signal.
                # Only let indicator conflicts cancel them before the close when the reverse is exceptionally strong.
                if pending_same_day:
                    if reverse_score < 6 and not pending_late_session:
                        return None
                elif reverse_score < 3:
                    return None
                analysis["pending_age_bars"] = pending_age_bars
                analysis["pending_same_day"] = pending_same_day
                analysis["pending_late_session"] = pending_late_session
            progress = {}
            if target_state == "filled_position":
                progress = self._calculate_position_progress(
                    target,
                    float(analysis.get("close", snapshot.get("close", 0)) or 0),
                )
                analysis["progress"] = progress
            analysis["action_type"] = self._resolve_reverse_indicator_action(
                float(analysis.get("score", 0) or 0),
                target_state,
                float(progress.get("progress_ratio", 0) or 0),
            )
            if not analysis.get("action_type"):
                return None
            analysis["price_patch"] = self._build_backtest_reverse_price_patch(
                target,
                str(analysis.get("action_type", "") or ""),
                float(analysis.get("close", snapshot.get("close", 0)) or 0),
            )
            analysis["new_direction"] = ""

        score = float(analysis.get("score", 0) or 0)
        triggered_signals = list(analysis.get("triggered_signals") or [])
        action_type = str(analysis.get("action_type", "") or "").strip().lower()
        if score <= 0 or not triggered_signals or not action_type:
            return None

        signal_id = str(target.get("signal_id", "") or "").strip()
        trade_group_id = signal_id or f"{symbol}_{int(target.get('entry_bar_ms', target.get('signal_bar_ms', 0)) or 0)}"
        strength = self._map_reverse_strength(score)
        close_value = float(analysis.get("current_price", analysis.get("close", snapshot.get("close", 0))) or 0)
        entry_price = round(float(target.get("entry_price", target.get("entry", 0)) or 0), 4)
        take_profit = round(float(target.get("target_price", target.get("take_profit", 0)) or 0), 4)
        stop_loss = round(float(target.get("stop_price", target.get("stop_loss", 0)) or 0), 4)
        order_status = "Submitted" if target_state == "pending_entry" else "Filled"
        relation_status = "backtest_entry_pending" if target_state == "pending_entry" else "backtest_position_open"
        reverse_source = source or ("signal" if reverse_kind == "signal_conflict" else "indicator")
        price_patch = dict(analysis.get("price_patch") or {})
        new_direction = str(analysis.get("new_direction", "") or "").strip().lower()
        origin_signal_id = signal_id
        if origin_signal_payload is not None:
            origin_signal_id = str(origin_signal_payload.get("signal_id", "") or "").strip() or signal_id
        target_extra = self._parse_object(target.get("extra"))
        setup_meta = build_setup_metadata(
            target_extra.get("setup") or target.get("setup") or target.get("signal", ""),
            fallback_signal=target.get("signal", ""),
            direction=direction,
            signal_mode=target_extra.get("signal_mode") or target.get("signal_mode", ""),
            strategy_profile=target_extra.get("strategy_profile") or target.get("strategy_profile", ""),
            setup_priority=target_extra.get("setup_priority") or target.get("setup_priority"),
            exit_policy_type=target_extra.get("exit_policy_type") or target.get("exit_policy_type", ""),
        )
        extra = {
            **setup_meta,
            "environment": BACKTEST_ENVIRONMENT,
            "source_environment": request.get("source_environment") or "",
            "reverse_kind": reverse_kind,
            "target_state": target_state,
            "order_status": order_status,
            "relation_status": relation_status,
            "position_side": direction,
            "current_direction": direction,
            "signal_id": signal_id,
            "origin_signal_id": origin_signal_id,
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": trade_group_id,
            "order_unique_id": trade_group_id,
            "broker_order_id": "",
            "order_id": "",
            "entry_price": entry_price,
            "quantity": int(target.get("shares", 0) or 0),
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "crsi": round(float(analysis.get("crsi", 0) or 0), 4),
            "obv_rsi": round(float(analysis.get("obv_rsi", 0) or 0), 4),
            "vwap_dist": round(float(analysis.get("vwap_dist", 0) or 0), 4),
            "close": round(close_value, 4),
            "triggered_signals": triggered_signals,
            "chart_tf": interval_to_chart_tf("5m"),
            "bar_index": int(bar_index or 0),
            "bar_time_ms": bar_time_ms,
            "signal_bar_ms": int(target.get("signal_bar_ms", 0) or 0),
            "signal_us_time": str(target.get("signal_us_time", "") or ""),
            "signal_close": round(float(target.get("signal_close", 0) or 0), 4),
            "simulated_only": True,
            "backtest_compare_with_tv": bool(request.get("compare_with_tv", True)),
            "backtest_compare_tv_signals": bool(request.get("compare_tv_signals", False)),
            "script_tag": str(request.get("strategy_tag") or ""),
            **(daily_fields or {}),
            **build_runtime_timestamps(),
        }
        if new_direction:
            extra["new_direction"] = new_direction
        if price_patch:
            extra.update(price_patch)
        if analysis.get("pending_age_bars") is not None:
            extra["pending_age_bars"] = int(analysis.get("pending_age_bars", 0) or 0)
        if analysis.get("pending_same_day") is not None:
            extra["pending_same_day"] = bool(analysis.get("pending_same_day"))
        if analysis.get("pending_late_session") is not None:
            extra["pending_late_session"] = bool(analysis.get("pending_late_session"))
        progress = analysis.get("progress") or {}
        if progress:
            extra["progress_ratio"] = round(float(progress.get("progress_ratio", 0) or 0), 4)
            extra["favorable_move"] = round(float(progress.get("favorable_move", 0) or 0), 4)
            extra["target_move"] = round(float(progress.get("target_move", 0) or 0), 4)
        if reverse_kind == "signal_conflict":
            reason = f"signal_conflict({target_state}) -> new_signal={str(origin_signal_payload.get('signal', '') or '')}"
        else:
            reason = f"indicator_conflict({target_state}) -> {', '.join(triggered_signals)}"
        return {
            "symbol": symbol,
            "direction": direction,
            "reverse_kind": reverse_kind,
            "source": reverse_source,
            "target_state": target_state,
            "target_order_status": order_status,
            "strength": strength,
            "score": round(score, 4),
            "triggered_signals": triggered_signals,
            "action_type": action_type,
            "status": "generated",
            "reason": reason,
            "setup": setup_meta.get("setup", ""),
            "setup_label": setup_meta.get("setup_label", ""),
            "setup_family": setup_meta.get("setup_family", ""),
            "signal_mode": setup_meta.get("signal_mode", ""),
            "priority": max(1, min(10, int(round(score)) or 1)),
            "signal_id": signal_id,
            "origin_signal_id": origin_signal_id,
            "trade_group_id": trade_group_id,
            "date": str(bar.get("us_time", "") or "")[:10],
            "bar_time_ms": bar_time_ms,
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "environment": BACKTEST_ENVIRONMENT,
            "extra": extra,
        }
