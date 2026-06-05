from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime, timezone

from ibkr_compute.api.account.buying_power_guard import (
    build_buying_power_guard,
    estimate_entry_exposure,
)
from ibkr_compute.core.time_utils import ET
from ibkr_compute.observability.prometheus import record_signal_event
from ibkr_compute.order.buying_power_reservations import (
    apply_reservations_to_buying_power_summary,
    merge_reservation_snapshot_into_guard,
)


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceSignalsMixin:
    VALIDATION_REJECTION_REASON_HUMAN = {
        "target_direction_mismatch": "方向不匹配：信号方向与当日 active target 方向不一致",
        "target_direction_missing": "缺少目标方向：当日 active target 未提供 long/short direction_bias",
        "target_direction_provider_error": "目标方向读取失败：无法确认当日 active target 方向",
        "trading_disabled": "交易开关关闭，自动开仓被禁用",
        "outside_trade_window": "当前不在交易窗口内",
        "outside_order_window": "当前不在下单窗口内",
        "sl_circuit_breaker": "止损熔断已触发",
        "position_limit_reached": "持仓数量已达到上限",
        "fixed_position_symbol_blocked": "固定持仓标的禁止自动开仓",
        "strategy_capacity_full": "策略容量已满",
        "direction_conflict": "已有同标的持仓或挂单，方向冲突",
        "invalid_prices": "信号价格结构无效",
    }
    CAPACITY_DEFER_REASONS = {"strategy_capacity_full"}
    READINESS_DEFER_REASONS = {
        "history_repair_pending",
        "session_unauthenticated",
        "runtime_stopped",
        "no_trade_symbols",
    }
    BUYING_POWER_SNAPSHOT_META_KEYS = (
        "source",
        "snapshot_error",
        "configured_buying_power",
        "risk_model",
        "risk_model_default_entry_exposure",
        "risk_model_remaining_slots",
        "risk_model_position_exposure",
        "risk_model_open_order_exposure",
        "risk_model_used_exposure",
        "risk_model_strategy_position_count",
        "risk_model_strategy_entry_order_count",
        "risk_model_strategy_position_symbols",
        "risk_model_strategy_entry_order_symbols",
        "local_reserved_exposure",
        "local_reserved_count",
        "local_reserved_order_ids",
        "local_reservation_state_key",
    )

    @staticmethod
    def _is_protection_incomplete_result(result: dict) -> bool:
        if not isinstance(result, dict):
            return False
        if bool(result.get("protection_complete")):
            return False
        missing_order_ids = [item for item in (result.get("missing_order_ids") or []) if str(item or "").strip()]
        order_ids = [item for item in (result.get("order_ids") or []) if str(item or "").strip()]
        error_text = str(result.get("error") or "").lower()
        return bool(
            missing_order_ids
            or (order_ids and "order_submission_unconfirmed" in error_text)
            or (order_ids and "missing=" in error_text)
        )

    def _build_protection_incomplete_diagnostic(self, sig: dict, result: dict, reason: str) -> dict:
        handler = getattr(getattr(self, "order_lifecycle", None), "handle_protection_incomplete", None)
        if callable(handler):
            diagnostic = handler(
                signal_id=str(sig.get("signal_id") or ""),
                symbol=str(sig.get("symbol") or ""),
                direction=str(sig.get("direction") or ""),
                result=result if isinstance(result, dict) else {},
                reason=reason,
            )
            if not isinstance(diagnostic, dict):
                diagnostic = {}
            return {
                **diagnostic,
                "missing_protection_roles": list(
                    diagnostic.get("missing_protection_roles")
                    or (result or {}).get("missing_protection_roles")
                    or []
                ),
                "protection_order_statuses": dict(
                    diagnostic.get("protection_order_statuses")
                    or (result or {}).get("protection_order_statuses")
                    or {}
                ),
                "protection_orders_checked": int(
                    diagnostic.get("protection_orders_checked")
                    or (result or {}).get("protection_orders_checked")
                    or 0
                ),
            }
        return {
            "status": "protection_incomplete",
            "reason": reason,
            "signal_id": str(sig.get("signal_id") or ""),
            "symbol": str(sig.get("symbol") or "").upper(),
            "direction": str(sig.get("direction") or "").lower(),
            "protection_complete": False,
            "missing_order_ids": list((result or {}).get("missing_order_ids") or []),
            "submitted_order_ids": list((result or {}).get("order_ids") or []),
            "missing_protection_roles": list((result or {}).get("missing_protection_roles") or []),
            "protection_order_statuses": dict((result or {}).get("protection_order_statuses") or {}),
            "protection_orders_checked": int((result or {}).get("protection_orders_checked") or 0),
            "safe_action": "diagnostic_only_no_broker_call",
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
            "cancel_recommended": True,
        }

    @classmethod
    def _validation_rejection_human_reason(cls, reason: str) -> str:
        text = str(reason or "").strip()
        if not text:
            return "信号校验未通过"
        return cls.VALIDATION_REJECTION_REASON_HUMAN.get(text, text)

    def _target_direction_diagnostic(self, sig: dict) -> dict:
        symbol = str((sig or {}).get("symbol") or "").strip().upper()
        processor = getattr(self, "signal_processor", None)
        provider = getattr(processor, "target_direction_provider", None)
        if not symbol:
            return {"target_direction_at_validation": "", "target_direction_source": "symbol_missing"}
        if not callable(provider):
            return {"target_direction_at_validation": "", "target_direction_source": "provider_unavailable"}
        try:
            payload = provider()
        except Exception as exc:
            return {
                "target_direction_at_validation": "",
                "target_direction_source": "provider_error",
                "target_direction_error": str(exc),
            }
        if not isinstance(payload, dict):
            return {"target_direction_at_validation": "", "target_direction_source": "provider_invalid"}
        target_direction = str(payload.get(symbol, "") or "").strip().lower()
        return {
            "target_direction_at_validation": target_direction,
            "target_direction_source": "active_target_direction_provider" if target_direction else "active_target_missing",
        }

    def _validation_rejection_extra(self, sig: dict, reason: str) -> dict:
        status_reason = str(reason or "validation_rejected").strip() or "validation_rejected"
        return {
            "status_reason": status_reason,
            "rejection_reason_code": status_reason,
            "rejection_reason_human": self._validation_rejection_human_reason(status_reason),
            "rejected_by": "signal_validation",
            "signal_direction_at_validation": str((sig or {}).get("direction") or "").strip().lower(),
            **self._target_direction_diagnostic(sig),
        }

    @staticmethod
    def _protection_fields(result: dict, diagnostic: dict) -> dict:
        result = result if isinstance(result, dict) else {}
        diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        return {
            "missing_protection_roles": list(
                result.get("missing_protection_roles")
                or diagnostic.get("missing_protection_roles")
                or []
            ),
            "protection_order_statuses": dict(
                result.get("protection_order_statuses")
                or diagnostic.get("protection_order_statuses")
                or {}
            ),
            "protection_orders_checked": int(
                result.get("protection_orders_checked")
                or diagnostic.get("protection_orders_checked")
                or 0
            ),
        }

    def _signal_loop(self):
        service_mod = _service_mod()
        signal_poll_interval = service_mod.DEFAULT_SIGNAL_POLL_INTERVAL
        last_logged_interval = None
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._sync_session_transition()
                signal_poll_interval = max(
                    1,
                    self.config.get_int_for_environment(
                        "signal_poll_interval_sec",
                        service_mod.ENVIRONMENT,
                        service_mod.DEFAULT_SIGNAL_POLL_INTERVAL,
                    ),
                )
                if signal_poll_interval != last_logged_interval:
                    service_mod.logger.info(
                        "Signal processing loop running (interval=%ds)",
                        signal_poll_interval,
                    )
                    last_logged_interval = signal_poll_interval
                if self.session_keeper.is_authenticated:
                    self._process_signals()
                    self.reverse_handler.check_and_process()
                    record_signal_event(
                        environment=service_mod.ENVIRONMENT,
                        stage="loop",
                        signal_source="runtime_loop",
                        result="ok",
                    )
                else:
                    service_mod.logger.info(
                        "Skip signal/reverse processing while session is unauthenticated"
                    )
                    record_signal_event(
                        environment=service_mod.ENVIRONMENT,
                        stage="loop",
                        signal_source="runtime_loop",
                        result="unauthenticated",
                    )
            except Exception as exc:
                service_mod.logger.error("Signal loop error: %s", exc)
                record_signal_event(
                    environment=service_mod.ENVIRONMENT,
                    stage="loop",
                    signal_source="runtime_loop",
                    result="error",
                    reason_code=exc.__class__.__name__,
                )
                signal_poll_interval = service_mod.DEFAULT_SIGNAL_POLL_INTERVAL
            self._signal_wakeup.wait(timeout=signal_poll_interval)
            self._signal_wakeup.clear()

    def _process_signals(self):
        service_mod = _service_mod()
        if not self.session_keeper.is_authenticated:
            service_mod.logger.info("Skip signal processing while session is unauthenticated")
            return

        pending_signals = self.signal_router.fetch_pending_signals()

        for sig in pending_signals:
            signal_id = str(sig.get("signal_id") or "").strip()
            if not signal_id:
                continue
            if not self.signal_router.claim_signal(signal_id):
                service_mod.logger.info("Skip duplicate in-flight signal: %s", signal_id)
                continue

            finalized = False
            signal_process_started = time.perf_counter()
            try:
                if self._is_fixed_position_signal(sig):
                    self._mark_signal_fixed_position_blocked(sig)
                    service_mod.logger.info(
                        "Signal blocked for fixed-position symbol: %s signal_id=%s",
                        sig.get("symbol"),
                        signal_id,
                    )
                    self.signal_router.mark_processed(signal_id)
                    finalized = True
                    continue

                direct_tv_entry = self._tv_direct_execution_enabled(sig)
                if direct_tv_entry:
                    direct_ok, direct_sig, direct_reason = self._prepare_tv_direct_entry_signal(sig)
                    if not direct_ok:
                        service_mod.logger.info(
                            "TV direct signal rejected before submission: signal_id=%s symbol=%s reason=%s",
                            signal_id,
                            sig.get("symbol"),
                            direct_reason,
                        )
                        self._mark_signal_tv_direct_rejected(sig, direct_reason)
                        self.signal_router.mark_processed(signal_id)
                        finalized = True
                        continue
                    sig = direct_sig
                    hard_safety_reason = self._tv_direct_hard_safety_reason()
                    if hard_safety_reason:
                        service_mod.logger.info(
                            "TV direct signal blocked by hard safety gate: signal_id=%s symbol=%s reason=%s",
                            signal_id,
                            sig.get("symbol"),
                            hard_safety_reason,
                        )
                        self._mark_signal_tv_direct_rejected(sig, hard_safety_reason)
                        self.signal_router.mark_processed(signal_id)
                        finalized = True
                        continue
                else:
                    if self._signal_runtime_expired(sig):
                        service_mod.logger.info(
                            "Signal expired before runtime readiness/submission: %s %s",
                            sig.get("symbol"),
                            sig.get("direction"),
                        )
                        self._mark_signal_validation_expired(sig)
                        self.signal_router.mark_processed(signal_id)
                        finalized = True
                        continue

                    valid, reason = self.signal_processor.validate_signal(sig)
                    if not valid:
                        if reason == "signal_expired":
                            service_mod.logger.info(
                                "Signal expired before runtime submission: %s %s - %s",
                                sig.get("symbol"),
                                sig.get("direction"),
                                reason,
                            )
                            self._mark_signal_validation_expired(sig)
                            self.signal_router.mark_processed(signal_id)
                            finalized = True
                            continue
                        if (
                            str(reason or "").startswith("warmup")
                            or reason in self.READINESS_DEFER_REASONS
                            or reason in self.CAPACITY_DEFER_REASONS
                        ):
                            service_mod.logger.info(
                                "Signal deferred: %s %s - %s",
                                sig.get("symbol"),
                                sig.get("direction"),
                                reason,
                            )
                            if reason in self.CAPACITY_DEFER_REASONS:
                                self._mark_signal_waiting_for_capacity(sig, self._strategy_capacity_snapshot())
                            continue
                        service_mod.logger.info(
                            "Signal rejected: %s %s - %s",
                            sig.get("symbol"),
                            sig.get("direction"),
                            reason,
                        )
                        self._mark_signal_validation_rejected(sig, reason)
                        self.signal_router.mark_processed(signal_id)
                        finalized = True
                        continue

                capacity = self._strategy_capacity_snapshot()
                if capacity.get("capacity_full"):
                    service_mod.logger.info(
                        "Signal waiting for strategy capacity: signal_id=%s symbol=%s used=%s max=%s",
                        signal_id,
                        sig.get("symbol"),
                        capacity.get("strategy_capacity_used"),
                        capacity.get("max_strategy_open_positions"),
                    )
                    self._mark_signal_waiting_for_capacity(sig, capacity)
                    continue

                if not direct_tv_entry:
                    guard_ok, guarded_sig, guard_reason = self._prepare_pre_submit_signal(sig)
                    if not guard_ok:
                        service_mod.logger.warning(
                            "Signal rejected by entry pre-submit guard: signal_id=%s symbol=%s reason=%s",
                            signal_id,
                            sig.get("symbol"),
                            guard_reason,
                        )
                        self._mark_signal_entry_guard_rejected(sig, guard_reason)
                        self.signal_router.mark_processed(signal_id)
                        finalized = True
                        continue

                    sig = guarded_sig
                buying_power_guard = self._evaluate_signal_buying_power_guard(sig)
                self._handle_buying_power_guard_state_transition(sig, buying_power_guard)
                if buying_power_guard.get("state") == "unavailable":
                    service_mod.logger.warning(
                        "Signal waiting for account/Gateway snapshot before buying-power guard: signal_id=%s symbol=%s reason=%s",
                        signal_id,
                        sig.get("symbol"),
                        buying_power_guard.get("reason"),
                    )
                    self._mark_signal_buying_power_unavailable(sig, buying_power_guard)
                    self._notify_buying_power_guard(sig, buying_power_guard, level="error", event_type="alert")
                    continue
                if buying_power_guard.get("state") == "blocked":
                    service_mod.logger.warning(
                        "Signal blocked by buying-power guard: signal_id=%s symbol=%s remaining_after=%s block_floor=%s",
                        signal_id,
                        sig.get("symbol"),
                        buying_power_guard.get("remaining_after"),
                        buying_power_guard.get("block_floor"),
                    )
                    self._mark_signal_buying_power_blocked(sig, buying_power_guard)
                    self._notify_buying_power_guard(sig, buying_power_guard, level="error", event_type="alert")
                    self.signal_router.mark_processed(signal_id)
                    finalized = True
                    continue
                if buying_power_guard.get("enabled"):
                    extra = self._signal_extra(sig)
                    sig["extra"] = {
                        **extra,
                        **self._buying_power_extra_fields(buying_power_guard),
                    }
                if buying_power_guard.get("state") == "warning":
                    self._notify_buying_power_guard(sig, buying_power_guard, level="warning", event_type="alert")

                symbol = sig["symbol"]
                duplicate_order = self.order_tracker.find_duplicate_open_entry(
                    symbol=symbol,
                    direction=sig["direction"],
                    quantity=sig["shares"],
                    entry_price=sig["entry"],
                    entry_order_type="LMT",
                )
                if duplicate_order:
                    broker_order_id = str(
                        duplicate_order.get("orderId") or duplicate_order.get("id") or ""
                    ).strip()
                    broker_status = str(duplicate_order.get("status") or "").strip()
                    broker_price = duplicate_order.get("price")
                    try:
                        self.order_tracker.sync_live_orders_snapshot([duplicate_order])
                    except Exception as sync_err:
                        service_mod.logger.error("Duplicate broker order sync failed: %s", sync_err)
                    self._mark_signal_duplicate_open_order(sig, duplicate_order)
                    service_mod.logger.warning(
                        "Skip duplicate order submission: signal_id=%s symbol=%s direction=%s broker_order_id=%s status=%s price=%s",
                        signal_id,
                        symbol,
                        sig.get("direction"),
                        broker_order_id or "-",
                        broker_status or "-",
                        broker_price,
                    )
                    self.signal_router.mark_processed(signal_id)
                    finalized = True
                    continue

                conid = self.conid_resolver.resolve(symbol)
                if not conid:
                    service_mod.logger.warning("Cannot resolve conid for %s, skipping", symbol)
                    continue

                order_flow_decision = {}
                order_flow_manager = getattr(self, "order_flow_manager", None)
                if order_flow_manager is not None and not direct_tv_entry:
                    try:
                        quote = self._entry_guard_quote(symbol)
                        entry_decision = getattr(order_flow_manager, "entry_decision", None)
                        if callable(entry_decision):
                            order_flow_decision = entry_decision(sig, conid=int(conid), quote=quote)
                        else:
                            order_flow_decision = order_flow_manager.observe_signal(sig, conid=int(conid))
                        extra = self._signal_extra(sig)
                        sig["extra"] = {
                            **extra,
                            "order_flow": order_flow_decision,
                            "order_flow_shadow": order_flow_decision,
                        }
                        action = str(order_flow_decision.get("action") or "").strip().lower()
                        if action == "wait":
                            self._mark_signal_order_flow_waiting(sig, order_flow_decision)
                            service_mod.logger.info(
                                "Signal waiting for order-flow confirmation: signal_id=%s symbol=%s reason=%s",
                                signal_id,
                                symbol,
                                order_flow_decision.get("reason"),
                            )
                            continue
                        if action == "reject":
                            self._mark_signal_order_flow_rejected(sig, order_flow_decision)
                            releaser = getattr(order_flow_manager, "release_symbol", None)
                            if callable(releaser):
                                releaser(symbol, reason=str(order_flow_decision.get("reason") or "order_flow_rejected"))
                            self.signal_router.mark_processed(signal_id)
                            finalized = True
                            continue
                        if action == "allow":
                            previous_entry = self._safe_float(sig.get("entry"), 0.0)
                            sig = self._apply_order_flow_entry_decision(sig, order_flow_decision)
                            if self._safe_float(sig.get("entry"), 0.0) != previous_entry:
                                buying_power_guard = self._evaluate_signal_buying_power_guard(sig)
                                self._handle_buying_power_guard_state_transition(sig, buying_power_guard)
                                if buying_power_guard.get("state") == "unavailable":
                                    service_mod.logger.warning(
                                        "Signal waiting for account/Gateway snapshot after order-flow repricing: signal_id=%s symbol=%s reason=%s",
                                        signal_id,
                                        sig.get("symbol"),
                                        buying_power_guard.get("reason"),
                                    )
                                    self._mark_signal_buying_power_unavailable(sig, buying_power_guard)
                                    self._notify_buying_power_guard(sig, buying_power_guard, level="error", event_type="alert")
                                    releaser = getattr(order_flow_manager, "release_symbol", None)
                                    if callable(releaser):
                                        releaser(symbol, reason=str(buying_power_guard.get("reason") or "account_snapshot_unavailable"))
                                    continue
                                if buying_power_guard.get("state") == "blocked":
                                    service_mod.logger.warning(
                                        "Signal blocked by buying-power guard after order-flow repricing: signal_id=%s symbol=%s remaining_after=%s block_floor=%s",
                                        signal_id,
                                        sig.get("symbol"),
                                        buying_power_guard.get("remaining_after"),
                                        buying_power_guard.get("block_floor"),
                                    )
                                    self._mark_signal_buying_power_blocked(sig, buying_power_guard)
                                    self._notify_buying_power_guard(sig, buying_power_guard, level="error", event_type="alert")
                                    releaser = getattr(order_flow_manager, "release_symbol", None)
                                    if callable(releaser):
                                        releaser(symbol, reason="buying_power_blocked_after_order_flow")
                                    self.signal_router.mark_processed(signal_id)
                                    finalized = True
                                    continue
                                if buying_power_guard.get("enabled"):
                                    extra = self._signal_extra(sig)
                                    sig["extra"] = {
                                        **extra,
                                        **self._buying_power_extra_fields(buying_power_guard),
                                    }
                    except Exception as order_flow_err:
                        enforce_mode = getattr(order_flow_manager, "enforce_mode", None)
                        should_fail_closed = callable(enforce_mode) and bool(enforce_mode())
                        if should_fail_closed:
                            order_flow_decision = {
                                "action": "wait",
                                "reason": "order_flow_error",
                                "error": str(order_flow_err),
                                "enforced": True,
                            }
                            extra = self._signal_extra(sig)
                            sig["extra"] = {
                                **extra,
                                "order_flow": order_flow_decision,
                                "order_flow_shadow": order_flow_decision,
                            }
                            self._mark_signal_order_flow_waiting(sig, order_flow_decision)
                            service_mod.logger.warning(
                                "Order-flow enforce failed closed: signal_id=%s symbol=%s error=%s",
                                signal_id,
                                symbol,
                                order_flow_err,
                            )
                            continue
                        service_mod.logger.warning(
                            "Order-flow shadow observe failed: signal_id=%s symbol=%s error=%s",
                            signal_id,
                            symbol,
                            order_flow_err,
                        )

                submission_started = time.perf_counter()
                if direct_tv_entry:
                    direct_submitter = getattr(self.order_placer, "place_bracket_order", None)
                    if not callable(direct_submitter):
                        result = {
                            "ok": False,
                            "error": "tv_direct_submitter_unavailable",
                            "protection_complete": False,
                        }
                    else:
                        extra = self._signal_extra(sig)
                        trade_group_id = str(extra.get("trade_group_id") or extra.get("bracket_group") or signal_id).strip()
                        order_extra = {
                            **extra,
                            "trade_group_id": trade_group_id,
                            "bracket_group": trade_group_id,
                        }
                        result = direct_submitter(
                            conid=conid,
                            symbol=symbol,
                            direction=sig["direction"],
                            quantity=sig["shares"],
                            entry_price=sig["entry"],
                            take_profit_price=sig["take_profit"],
                            stop_loss_price=sig["stop_loss"],
                            use_paper=service_mod.ENVIRONMENT == "paper",
                            signal_id=signal_id,
                            trade_group_id=trade_group_id,
                            bracket_group=trade_group_id,
                            order_extra=order_extra,
                            order_ref_suffix="tv_direct",
                            order_family_type="bracket_oco",
                            entry_algo_strategy="Adaptive" if bool(extra.get("tv_entry_adaptive_enabled")) else "",
                            entry_adaptive_priority=str(extra.get("tv_entry_adaptive_priority") or ""),
                        )
                else:
                    harvest_settings = self._harvest_entry_settings()
                    harvest_submitter = getattr(self.order_placer, "place_harvest_bracket_order", None)
                    if not callable(harvest_submitter):
                        result = {
                            "ok": False,
                            "error": "intraday_harvest_submitter_unavailable",
                            "protection_complete": False,
                        }
                    else:
                        extra = self._signal_extra(sig)
                        trade_group_id = str(extra.get("trade_group_id") or extra.get("bracket_group") or signal_id).strip()
                        sig["extra"] = {
                            **extra,
                            "trade_group_id": trade_group_id,
                            "bracket_group": trade_group_id,
                            "intraday_harvest_profile": "intraday_volatility_harvest_v1",
                            "partial_harvest_requested": True,
                            "intraday_harvest_split_requested": False,
                        }
                        result = harvest_submitter(
                            conid=conid,
                            symbol=symbol,
                            direction=sig["direction"],
                            quantity=sig["shares"],
                            entry_price=sig["entry"],
                            take_profit_price=sig["take_profit"],
                            stop_loss_price=sig["stop_loss"],
                            use_paper=service_mod.ENVIRONMENT == "paper",
                            signal_id=signal_id,
                            trade_group_id=trade_group_id,
                            settings=harvest_settings,
                        )

                submission_duration = time.perf_counter() - submission_started
                if result.get("ok"):
                    record_signal_event(
                        environment=service_mod.ENVIRONMENT,
                        stage="order_submission",
                        signal_source=str(sig.get("source") or "unknown"),
                        result="ok",
                        reason_code=str(result.get("order_family_type") or "submitted"),
                        duration_s=submission_duration,
                    )
                    if order_flow_manager is not None and not direct_tv_entry:
                        try:
                            order_flow_manager.mark_filled(
                                symbol,
                                conid=int(conid),
                                direction=sig.get("direction", ""),
                                signal_id=signal_id,
                            )
                        except Exception as order_flow_err:
                            service_mod.logger.debug(
                                "Order-flow filled watch failed: signal_id=%s symbol=%s error=%s",
                                signal_id,
                                symbol,
                                order_flow_err,
                            )
                    if buying_power_guard.get("enabled"):
                        result["buying_power_guard"] = dict(buying_power_guard)
                        self._notify_buying_power_guard(
                            sig,
                            buying_power_guard,
                            level="info",
                            event_type="account_order",
                            force=True,
                        )
                    self._patch_signal_pre_submit_prices(sig)
                    service_mod.logger.info(
                        "Order placed: %s %s bracket_group=%s",
                        symbol,
                        sig["direction"],
                        result.get("bracket_group"),
                    )
                    try:
                        self._register_submitted_order_result(result, sig, symbol)
                    except Exception as track_err:
                        service_mod.logger.error("Order tracker register failed: %s", track_err)
                    try:
                        self._ack_signal_after_order_submission(sig, result)
                    except Exception as ack_err:
                        service_mod.logger.error(
                            "Signal ack failed after order placement: %s signal_id=%s",
                            ack_err,
                            signal_id,
                        )
                    self.signal_processor.register_pending_entry(
                        symbol,
                        {
                            "direction": sig["direction"],
                            "bracket_group": result.get("bracket_group"),
                            "signal_id": signal_id,
                        },
                    )
                    self.order_lifecycle.increment_position_count()
                else:
                    service_mod.logger.error("Order failed: %s - %s", symbol, result.get("error"))
                    record_signal_event(
                        environment=service_mod.ENVIRONMENT,
                        stage="order_submission",
                        signal_source=str(sig.get("source") or "unknown"),
                        result="error",
                        reason_code="protection_incomplete" if result.get("protection_incomplete") else "submit_failed",
                        duration_s=submission_duration,
                    )
                    self._mark_signal_submit_failed(sig, result)

                self.signal_router.mark_processed(signal_id)
                finalized = True
            finally:
                if not finalized:
                    self.signal_router.release_signal(signal_id)
                record_signal_event(
                    environment=service_mod.ENVIRONMENT,
                    stage="signal_process",
                    signal_source=str(sig.get("source") or "unknown"),
                    result="finalized" if finalized else "deferred",
                    reason_code="processed" if finalized else "released",
                    duration_s=time.perf_counter() - signal_process_started,
                )

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:
            return default
        return number

    def _signal_runtime_expired(self, sig: dict) -> bool:
        checker = getattr(getattr(self, "signal_processor", None), "_is_signal_expired", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(sig, datetime.now(ET)))
        except Exception:
            return False

    def _tv_direct_hard_safety_reason(self) -> str:
        service_mod = _service_mod()
        if not self._config_bool("ibkr_trading_enabled", True):
            return "trading_disabled"
        if str(service_mod.ENVIRONMENT or "").strip().lower() == "live" and not self._config_bool(
            "ibkr_live_trading_enabled",
            True,
        ):
            return "trading_disabled"
        lifecycle = getattr(self, "order_lifecycle", None)
        if bool(getattr(lifecycle, "is_sl_circuit_breaker", False)):
            return "sl_circuit_breaker"
        if bool(getattr(lifecycle, "is_position_limit_reached", False)):
            return "position_limit_reached"
        return ""

    @staticmethod
    def _round_price(value) -> float:
        return round(TradingServiceSignalsMixin._safe_float(value, 0.0), 2)

    @staticmethod
    def _round_tv_entry_limit(value, direction: str) -> float:
        price = TradingServiceSignalsMixin._safe_float(value, 0.0)
        if price <= 0:
            return 0.0
        if str(direction or "").strip().lower() == "short":
            return math.floor(price * 100.0 + 1e-9) / 100.0
        return math.ceil(price * 100.0 - 1e-9) / 100.0

    @classmethod
    def _timestamp_from_epoch_value(cls, value):
        number = cls._safe_float(value, 0.0)
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number = number / 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _timestamp_from_text(value, *, assume_et: bool = True):
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except Exception:
            dt = None
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except Exception:
                    dt = None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET if assume_et else timezone.utc)
        return dt.astimezone(timezone.utc)

    def _tv_signal_timestamp(self, sig: dict) -> tuple[datetime | None, str, object]:
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        extra = self._signal_extra(sig)
        epoch_candidates = (
            ("extra.pine_eval_ms", extra.get("pine_eval_ms")),
            ("extra.pine_eval_time_ms", extra.get("pine_eval_time_ms")),
            ("extra.bar_close_ms", extra.get("bar_close_ms")),
            ("extra.bar_time_ms", extra.get("bar_time_ms")),
            ("raw.pine_eval_ms", raw.get("pine_eval_ms")),
            ("raw.pine_eval_time_ms", raw.get("pine_eval_time_ms")),
            ("raw.bar_close_ms", raw.get("bar_close_ms")),
            ("raw.bar_time_ms", raw.get("bar_time_ms")),
            ("signal.bar_time_ms", sig.get("bar_time_ms")),
        )
        for source, value in epoch_candidates:
            dt = self._timestamp_from_epoch_value(value)
            if dt is not None:
                return dt, source, value

        text_candidates = (
            ("extra.pine_eval_time", extra.get("pine_eval_time")),
            ("extra.signal_time", extra.get("signal_time")),
            ("raw.us_time", raw.get("us_time")),
            ("signal.signal_time", sig.get("signal_time")),
            ("signal.us_time", sig.get("us_time")),
            ("raw.created", raw.get("created")),
            ("signal.created", sig.get("created")),
            ("raw.updated", raw.get("updated")),
        )
        for source, value in text_candidates:
            dt = self._timestamp_from_text(value, assume_et=not source.endswith(".created") and not source.endswith(".updated"))
            if dt is not None:
                return dt, source, value
        return None, "", None

    def _tv_direct_execution_enabled(self, sig: dict) -> bool:
        if not self._config_bool("tv_primary_direct_execution_enabled", True):
            return False
        extra = self._signal_extra(sig)
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        source_candidates = (
            sig.get("source"),
            extra.get("source"),
            extra.get("signal_source"),
            raw.get("source"),
        )
        normalized_sources = {str(item or "").strip().lower() for item in source_candidates if str(item or "").strip()}
        if not (normalized_sources & {"tv", "tradingview", "webhook_tv", "tv_webhook", "signal"}):
            return False
        direction = str(sig.get("direction") or "").strip().lower()
        if direction not in {"long", "short"}:
            return False
        event_type = str(extra.get("event_type") or extra.get("tv_event_type") or raw.get("event_type") or "").strip().lower()
        if event_type and any(token in event_type for token in ("exit", "close", "risk_update", "cancel")):
            return False
        return True

    def _prepare_tv_direct_entry_signal(self, sig: dict) -> tuple[bool, dict, str]:
        reference_entry = self._safe_float(sig.get("entry"), 0.0)
        reference_stop = self._safe_float(sig.get("stop_loss"), 0.0)
        reference_target = self._safe_float(sig.get("take_profit"), 0.0)
        direction = str(sig.get("direction") or "").strip().lower()
        if reference_entry <= 0 or reference_stop <= 0 or reference_target <= 0 or direction not in {"long", "short"}:
            return False, sig, "invalid_prices"

        max_age_s = max(1.0, self._config_float("tv_entry_freshness_sec", 120.0))
        clock_skew_s = max(5.0, self._config_float("tv_entry_clock_skew_sec", 60.0))
        signal_dt, signal_time_source, raw_signal_time = self._tv_signal_timestamp(sig)
        now_utc = datetime.now(timezone.utc)
        if signal_dt is None:
            extra = self._signal_extra(sig)
            sig["extra"] = {
                **extra,
                "tv_direct_entry": True,
                "status_reason": "stale_signal",
                "signal_freshness_source": "missing",
                "signal_freshness_max_age_s": max_age_s,
                "tv_direct_rejected_reason": "stale_signal",
            }
            return False, sig, "stale_signal"

        signal_age_s = (now_utc - signal_dt).total_seconds()
        freshness_fields = {
            "signal_freshness_source": signal_time_source,
            "signal_freshness_raw_value": raw_signal_time,
            "signal_freshness_signal_at": signal_dt.isoformat(),
            "signal_freshness_checked_at": now_utc.isoformat(),
            "signal_freshness_age_s": round(signal_age_s, 3),
            "signal_freshness_max_age_s": max_age_s,
            "signal_clock_skew_threshold_s": clock_skew_s,
        }
        if signal_age_s < -clock_skew_s:
            extra = self._signal_extra(sig)
            sig["extra"] = {
                **extra,
                **freshness_fields,
                "tv_direct_entry": True,
                "status_reason": "signal_clock_skew",
                "tv_direct_rejected_reason": "signal_clock_skew",
            }
            return False, sig, "signal_clock_skew"
        if signal_age_s > max_age_s:
            extra = self._signal_extra(sig)
            sig["extra"] = {
                **extra,
                **freshness_fields,
                "tv_direct_entry": True,
                "status_reason": "stale_signal",
                "tv_direct_rejected_reason": "stale_signal",
            }
            return False, sig, "stale_signal"

        extra = self._signal_extra(sig)
        cap_bps = self._config_float("tv_entry_limit_cap_bps", 15.0)
        for source in (sig, extra):
            for key in ("submitted_limit_cap_bps", "tv_entry_limit_cap_bps", "entry_limit_cap_bps", "limit_cap_bps"):
                if isinstance(source, dict) and key in source and source.get(key) not in (None, ""):
                    cap_bps = self._safe_float(source.get(key), cap_bps)
                    break
            else:
                continue
            break
        cap_bps = max(0.0, cap_bps)
        raw_limit = (
            reference_entry * (1.0 - cap_bps / 10000.0)
            if direction == "short"
            else reference_entry * (1.0 + cap_bps / 10000.0)
        )
        submitted_limit = self._round_tv_entry_limit(raw_limit, direction)
        adaptive_enabled = self._config_bool("tv_entry_adaptive_enabled", True)
        adaptive_priority = self._config_text("tv_entry_adaptive_priority", "Normal").strip() or "Normal"
        adjusted_sig = copy.deepcopy(sig)
        adjusted_sig["entry"] = submitted_limit
        adjusted_sig["extra"] = {
            **extra,
            **freshness_fields,
            "tv_direct_entry": True,
            "tv_primary_direct_execution": True,
            "signal_reference_price": reference_entry,
            "tv_reference_entry": reference_entry,
            "tv_reference_stop_loss": reference_stop,
            "tv_reference_take_profit": reference_target,
            "reference_entry": reference_entry,
            "reference_stop_loss": reference_stop,
            "reference_take_profit": reference_target,
            "reference_protection_only": True,
            "final_protection_from_fill": True,
            "submitted_entry_limit_price": submitted_limit,
            "submitted_limit_cap_price": submitted_limit,
            "submitted_limit_cap_bps": cap_bps,
            "submitted_limit_cap_applied": True,
            "tv_entry_limit_cap_bps": cap_bps,
            "tv_entry_adaptive_enabled": adaptive_enabled,
            "tv_entry_adaptive_priority": adaptive_priority,
            "adaptive_priority": adaptive_priority if adaptive_enabled else "",
            "entry_order_type": "LMT",
            "entry_limit_intent": "bounded_marketable",
            "entry_price_plan": "tv_direct_bounded_limit",
            "entry_repriced": submitted_limit != self._round_price(reference_entry),
            "tv_direct_validation_policy": "freshness_only_then_hard_safety",
            "tv_direct_signal_processor_validation_skipped": True,
            "original_entry": reference_entry,
            "original_stop_loss": reference_stop,
            "original_take_profit": reference_target,
            "status_reason": "tv_direct_ready",
        }
        return True, adjusted_sig, ""

    def _mark_signal_tv_direct_rejected(self, sig: dict, reason: str):
        service_mod = _service_mod()
        if not self.pb:
            return
        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return
        status_reason = str(reason or "stale_signal").strip() or "stale_signal"
        status = "expired" if status_reason in {"stale_signal", "signal_expired"} else "rejected"
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            signal_extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else self._signal_extra(sig)
            lifecycle_extra = {
                **signal_extra,
                "tv_direct_entry": True,
                "tv_direct_rejected": True,
                "tv_direct_rejected_reason": status_reason,
                "tv_direct_rejected_at": self._now_iso(),
                "status_reason": status_reason,
                "execution_state": "tv_direct_rejected",
            }
            if self._ack_signal_lifecycle_update(sig, status, status_reason, lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                status,
                status_reason,
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark TV direct signal rejected: signal_id=%s reason=%s error=%s",
                signal_id,
                status_reason,
                exc,
            )

    def _config_bool(self, key: str, default: bool) -> bool:
        getter = getattr(getattr(self, "config", None), "get_bool_for_environment", None)
        if not callable(getter):
            return bool(default)
        try:
            value = getter(key, _service_mod().ENVIRONMENT, default)
        except Exception:
            return bool(default)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "y", "on"}
        return bool(value)

    def _config_float(self, key: str, default: float) -> float:
        getter = getattr(getattr(self, "config", None), "get_float_for_environment", None)
        if not callable(getter):
            return float(default)
        try:
            return float(getter(key, _service_mod().ENVIRONMENT, default))
        except Exception:
            return float(default)

    def _config_text(self, key: str, default: str) -> str:
        getter = getattr(getattr(self, "config", None), "get_for_environment", None)
        if not callable(getter):
            return str(default)
        try:
            return str(getter(key, _service_mod().ENVIRONMENT, default))
        except Exception:
            return str(default)

    def _harvest_entry_settings(self) -> dict:
        try:
            from ibkr_compute.core.intraday_harvest import harvest_settings_from_config

            settings = harvest_settings_from_config(
                getattr(self, "config", None),
                _service_mod().ENVIRONMENT,
            )
            settings["enabled"] = True
            settings["live_auto_enabled"] = True
            settings["split_brackets_enabled"] = False
            return settings
        except Exception:
            return {"enabled": True, "live_auto_enabled": True, "split_brackets_enabled": False}

    @staticmethod
    def _harvest_entry_split_enabled(settings: dict | None) -> bool:
        settings = settings if isinstance(settings, dict) else {}
        return bool(settings.get("enabled") and settings.get("split_brackets_enabled"))

    @staticmethod
    def _primary_order_result(result: dict) -> dict:
        result = result if isinstance(result, dict) else {}
        legs = [dict(item) for item in (result.get("legs") or []) if isinstance(item, dict)]
        if not legs:
            return dict(result)
        primary = next((item for item in legs if item.get("ok") and item.get("lot") == "core"), None)
        if primary is None:
            primary = next((item for item in legs if item.get("ok")), None)
        if primary is None:
            primary = legs[0]
        return {
            **result,
            **primary,
            "harvest_split": bool(result.get("harvest_split")),
            "harvest_legs": legs,
            "combined_order_ids": list(result.get("order_ids") or []),
            "order_ids": list(primary.get("order_ids") or []),
        }

    def _register_submitted_order_result(self, result: dict, sig: dict, symbol: str):
        legs = [dict(item) for item in ((result or {}).get("legs") or []) if isinstance(item, dict)]
        targets = legs if (result or {}).get("harvest_split") and legs else [dict(result or {})]
        for target in targets:
            order_ids = target.get("order_ids") or []
            if not order_ids:
                continue
            extra = self._signal_extra(sig)
            self.order_tracker.register_submitted_orders(
                order_ids,
                {
                    "symbol": symbol,
                    "direction": sig["direction"],
                    "quantity": target.get("quantity") or sig["shares"],
                    "entry_price": sig["entry"],
                    "tp_price": sig["take_profit"],
                    "sl_price": sig["stop_loss"],
                    "entry_order_type": "LMT",
                    "entry_limit_intent": str(extra.get("entry_limit_intent") or "passive"),
                    "entry_price_plan": str(extra.get("entry_price_plan") or "passive_limit"),
                    "entry_unique_id": target.get("entry_coid")
                    or target.get("bracket_group")
                    or "",
                    "tp_unique_id": target.get("tp_coid") or "",
                    "sl_unique_id": target.get("sl_coid") or "",
                },
            )

    @staticmethod
    def _signal_extra(sig: dict) -> dict:
        extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        if extra:
            return dict(extra)
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        raw_extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        if isinstance(raw.get("extra"), str):
            try:
                raw_extra = json.loads(raw.get("extra") or "{}")
            except Exception:
                raw_extra = {}
        return dict(raw_extra or {}) if isinstance(raw_extra, dict) else {}

    @staticmethod
    def _quote_price(quote: dict, key: str) -> float:
        return TradingServiceSignalsMixin._safe_float((quote or {}).get(key), 0.0)

    def _entry_guard_quote(self, symbol: str) -> dict:
        quote_book = getattr(self, "realtime_quote_book", None)
        getter = getattr(quote_book, "get_quote", None)
        if not callable(getter):
            return {}
        try:
            quote = getter(symbol)
        except Exception as exc:
            _service_mod().logger.warning("Entry pre-submit quote lookup failed for %s: %s", symbol, exc)
            return {}
        return dict(quote or {}) if isinstance(quote, dict) else {}

    def _entry_guard_fresh_quote(
        self,
        symbol: str,
        direction: str,
        max_age_s: float,
    ) -> tuple[dict, float, str, bool]:
        quote = self._entry_guard_quote(symbol)
        quote_age_s = self._safe_float(quote.get("quote_age_s"), -1.0) if quote else -1.0
        reference_price, reference_source = self._guard_reference_price(quote, direction)
        fresh_quote = bool(
            quote
            and quote_age_s >= 0
            and quote_age_s <= max_age_s
            and reference_price > 0
        )
        return quote, reference_price, reference_source, fresh_quote

    def _entry_temp_quote_subscriptions(self) -> dict:
        subscriptions = getattr(self, "_entry_temp_quote_subscription_map", None)
        if not isinstance(subscriptions, dict):
            subscriptions = {}
            setattr(self, "_entry_temp_quote_subscription_map", subscriptions)
        return subscriptions

    def _entry_active_subscription_map(self) -> dict:
        source = getattr(self, "_active_subscription_map", None)
        if not isinstance(source, dict):
            return {}
        result = {}
        for symbol, conid in source.items():
            normalized_symbol = str(symbol or "").strip().upper()
            try:
                normalized_conid = int(conid or 0)
            except (TypeError, ValueError):
                normalized_conid = 0
            if normalized_symbol and normalized_conid > 0:
                result[normalized_symbol] = normalized_conid
        return result

    def _entry_refresh_quote_symbol_map(self):
        active_map = self._entry_active_subscription_map()
        temp_map = {
            str(symbol or "").strip().upper(): int((meta or {}).get("conid") or 0)
            for symbol, meta in self._entry_temp_quote_subscriptions().items()
            if str(symbol or "").strip() and int((meta or {}).get("conid") or 0) > 0
        }
        combined = {**active_map, **temp_map}
        reverse_map = {int(conid): symbol for symbol, conid in combined.items() if int(conid or 0) > 0}
        for attr in ("bar_aggregator", "realtime_quote_book"):
            target = getattr(self, attr, None)
            setter = getattr(target, "set_symbol_map", None)
            if callable(setter):
                try:
                    setter(reverse_map)
                except Exception as exc:
                    _service_mod().logger.debug("Failed to refresh %s symbol map: %s", attr, exc)

    def _entry_release_temp_quote_subscription(self, symbol: str):
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return
        active_map = self._entry_active_subscription_map()
        if normalized_symbol in active_map:
            return
        subscriptions = self._entry_temp_quote_subscriptions()
        meta = subscriptions.pop(normalized_symbol, None)
        conid = int((meta or {}).get("conid") or 0)
        if conid > 0:
            unsubscriber = getattr(getattr(self, "ws_client", None), "unsubscribe", None)
            if callable(unsubscriber):
                try:
                    unsubscriber(conid)
                except Exception as exc:
                    _service_mod().logger.debug("Temporary quote unsubscribe failed for %s/%s: %s", normalized_symbol, conid, exc)
        self._entry_refresh_quote_symbol_map()

    def _entry_cleanup_temp_quote_subscriptions(self):
        now_ts = time.time()
        subscriptions = self._entry_temp_quote_subscriptions()
        expired = [
            symbol
            for symbol, meta in list(subscriptions.items())
            if now_ts >= float((meta or {}).get("expires_at") or 0.0)
        ]
        for symbol in expired:
            self._entry_release_temp_quote_subscription(symbol)

    def _entry_temp_quote_capacity_available(self, symbol: str) -> tuple[bool, str]:
        normalized_symbol = str(symbol or "").strip().upper()
        subscriptions = self._entry_temp_quote_subscriptions()
        if normalized_symbol in subscriptions or normalized_symbol in self._entry_active_subscription_map():
            return True, ""

        temp_limit = max(0, int(self._config_float("entry_pre_submit_temp_subscription_limit", 8)))
        if temp_limit > 0 and len(subscriptions) >= temp_limit:
            return False, "temp_subscription_limit"

        total_limit = max(0, int(self._config_float("ibkr_total_subscription_limit", 80)))
        if total_limit > 0:
            status_getter = getattr(getattr(self, "ws_client", None), "status", None)
            status = {}
            if callable(status_getter):
                try:
                    status = status_getter() or {}
                except Exception:
                    status = {}
            active_count = int(status.get("subscribed_count") or 0) + int(status.get("pending_count") or 0)
            if active_count >= total_limit:
                return False, "total_subscription_limit"
        return True, ""

    def _entry_resolve_conid(self, symbol: str) -> int:
        resolver = getattr(self, "conid_resolver", None)
        resolve = getattr(resolver, "resolve", None)
        if not callable(resolve):
            return 0
        try:
            return int(resolve(symbol) or 0)
        except Exception as exc:
            _service_mod().logger.warning("Entry quote conid resolve failed for %s: %s", symbol, exc)
            return 0

    def _entry_request_temp_quote_subscription(self, symbol: str, conid: int, ttl_s: float) -> tuple[bool, str]:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_conid = int(conid or 0)
        if not normalized_symbol or normalized_conid <= 0:
            return False, "conid_unavailable"
        ok, reason = self._entry_temp_quote_capacity_available(normalized_symbol)
        if not ok:
            return False, reason or "capacity_full"

        now_ts = time.time()
        self._entry_temp_quote_subscriptions()[normalized_symbol] = {
            "conid": normalized_conid,
            "created_at": now_ts,
            "expires_at": now_ts + max(1.0, float(ttl_s or 0.0)),
            "source": "entry_pre_submit",
        }
        self._entry_refresh_quote_symbol_map()
        subscriber = getattr(getattr(self, "ws_client", None), "subscribe", None)
        if callable(subscriber):
            try:
                subscriber(normalized_conid)
            except Exception as exc:
                self._entry_release_temp_quote_subscription(normalized_symbol)
                _service_mod().logger.warning("Temporary quote subscribe failed for %s/%s: %s", normalized_symbol, normalized_conid, exc)
                return False, "subscribe_failed"
        return True, ""

    def _entry_wait_for_fresh_quote(
        self,
        symbol: str,
        direction: str,
        max_age_s: float,
        timeout_s: float,
    ) -> tuple[dict, float, str, bool]:
        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        while True:
            quote, reference_price, reference_source, fresh = self._entry_guard_fresh_quote(symbol, direction, max_age_s)
            if fresh or time.monotonic() >= deadline:
                return quote, reference_price, reference_source, fresh
            time.sleep(0.05)

    def _entry_request_quote_snapshot(
        self,
        symbol: str,
        conid: int,
        timeout_s: float,
    ) -> tuple[dict, str]:
        requester = getattr(getattr(self, "ws_client", None), "request_market_data_snapshot", None)
        if not callable(requester):
            requester = getattr(getattr(self, "broker", None), "request_market_data_snapshot", None)
        if not callable(requester):
            return {}, "snapshot_unavailable"
        try:
            result = requester(conid=int(conid or 0), symbol=str(symbol or "").strip().upper(), timeout=float(timeout_s or 0.0))
        except Exception as exc:
            return {}, str(exc) or "snapshot_failed"
        if not isinstance(result, dict):
            return {}, "snapshot_empty"
        if result.get("ok") is False:
            return {}, str(result.get("error") or "snapshot_failed")
        quote = result.get("quote") if isinstance(result.get("quote"), dict) else {}
        if not quote:
            quote = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        if quote:
            return dict(quote), ""
        return {}, str(result.get("error") or "snapshot_empty")

    def _entry_acquire_fresh_quote(
        self,
        symbol: str,
        direction: str,
        max_age_s: float,
    ) -> tuple[dict, float, str, bool, dict, str]:
        started = time.monotonic()
        diagnostics = {
            "quote_acquire_source": "none",
            "quote_acquire_wait_ms": 0,
            "quote_acquire_error": "",
            "temporary_quote_subscription": False,
        }
        if not self._config_bool("entry_pre_submit_quote_acquire_enabled", True):
            diagnostics["quote_acquire_error"] = "quote_acquire_disabled"
            return {}, 0.0, "", False, diagnostics, "entry_guard_no_fresh_quote"

        self._entry_cleanup_temp_quote_subscriptions()
        conid = self._entry_resolve_conid(symbol)
        diagnostics["quote_acquire_conid"] = conid or None
        if conid <= 0:
            diagnostics["quote_acquire_error"] = "conid_unavailable"
            return {}, 0.0, "", False, diagnostics, "entry_guard_quote_conid_unavailable"

        ttl_s = max(1.0, self._config_float("entry_pre_submit_temp_subscription_ttl_sec", 30.0))
        subscribe_ok, subscribe_reason = self._entry_request_temp_quote_subscription(symbol, conid, ttl_s)
        diagnostics["temporary_quote_subscription"] = bool(subscribe_ok)
        if not subscribe_ok:
            diagnostics["quote_acquire_error"] = subscribe_reason or "capacity_full"
            reason = (
                "entry_guard_quote_capacity_full"
                if subscribe_reason in {"temp_subscription_limit", "total_subscription_limit", "capacity_full"}
                else "entry_guard_no_fresh_quote"
            )
            diagnostics["quote_acquire_wait_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return {}, 0.0, "", False, diagnostics, reason

        wait_s = max(0.0, self._config_float("entry_pre_submit_quote_wait_sec", 2.0))
        quote, reference_price, reference_source, fresh = self._entry_wait_for_fresh_quote(
            symbol,
            direction,
            max_age_s,
            wait_s,
        )
        if fresh:
            diagnostics["quote_acquire_source"] = "temp_ws"
            diagnostics["quote_acquire_wait_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return quote, reference_price, reference_source, True, diagnostics, ""

        diagnostics["quote_acquire_error"] = "subscribe_timeout"
        if not self._config_bool("entry_pre_submit_snapshot_enabled", True):
            diagnostics["quote_acquire_wait_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return quote, reference_price, reference_source, False, diagnostics, "entry_guard_quote_subscribe_timeout"

        snapshot_timeout_s = max(0.1, self._config_float("entry_pre_submit_snapshot_timeout_sec", 2.0))
        snapshot_quote, snapshot_error = self._entry_request_quote_snapshot(symbol, conid, snapshot_timeout_s)
        if snapshot_quote:
            # Snapshot implementations may either update the quote book through listeners or return a quote directly.
            quote, reference_price, reference_source, fresh = self._entry_guard_fresh_quote(symbol, direction, max_age_s)
            if not fresh:
                quote = snapshot_quote
                quote.setdefault("quote_age_s", 0.0)
                quote_age_s = self._safe_float(quote.get("quote_age_s"), 0.0)
                reference_price, reference_source = self._guard_reference_price(quote, direction)
                fresh = bool(quote_age_s >= 0 and quote_age_s <= max_age_s and reference_price > 0)
            if fresh:
                diagnostics["quote_acquire_source"] = "snapshot"
                diagnostics["quote_acquire_error"] = ""
                diagnostics["quote_acquire_wait_ms"] = round((time.monotonic() - started) * 1000.0, 1)
                return quote, reference_price, reference_source, True, diagnostics, ""

        diagnostics["quote_acquire_error"] = snapshot_error or "snapshot_timeout"
        diagnostics["quote_acquire_wait_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        return quote, reference_price, reference_source, False, diagnostics, "entry_guard_quote_snapshot_timeout"

    def _guard_reference_price(self, quote: dict, direction: str) -> tuple[float, str]:
        last_price = self._quote_price(quote, "last_price")
        if last_price > 0:
            return last_price, "last_price"
        normalized_direction = str(direction or "").strip().lower()
        primary_key = "ask" if normalized_direction == "long" else "bid"
        fallback_key = "bid" if normalized_direction == "long" else "ask"
        primary = self._quote_price(quote, primary_key)
        if primary > 0:
            return primary, primary_key
        fallback = self._quote_price(quote, fallback_key)
        if fallback > 0:
            return fallback, fallback_key
        return 0.0, ""

    def _reprice_base_price(self, quote: dict, direction: str) -> tuple[float, str]:
        normalized_direction = str(direction or "").strip().lower()
        if normalized_direction == "short":
            ask = self._quote_price(quote, "ask")
            if ask > 0:
                return ask, "ask"
            last_price = self._quote_price(quote, "last_price")
            if last_price > 0:
                return last_price, "last_price"
            bid = self._quote_price(quote, "bid")
            return (bid, "bid") if bid > 0 else (0.0, "")

        bid = self._quote_price(quote, "bid")
        if bid > 0:
            return bid, "bid"
        last_price = self._quote_price(quote, "last_price")
        if last_price > 0:
            return last_price, "last_price"
        ask = self._quote_price(quote, "ask")
        return (ask, "ask") if ask > 0 else (0.0, "")

    def _entry_limit_offset(self, sig: dict, reference_price: float, extra: dict) -> tuple[float, str]:
        offset = self._safe_float(extra.get("entry_limit_offset"), 0.0)
        if offset > 0:
            return offset, "signal_extra.entry_limit_offset"

        entry = self._safe_float(sig.get("entry"), 0.0)
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        limit_candidates = (
            sig.get("limit_price"),
            sig.get("entry_limit_price"),
            raw.get("limit_price"),
            raw.get("entry_limit_price"),
            extra.get("limit_price"),
            extra.get("entry_limit_price"),
        )
        for candidate in limit_candidates:
            limit_price = self._safe_float(candidate, 0.0)
            candidate_offset = abs(entry - limit_price)
            if entry > 0 and limit_price > 0 and candidate_offset > 0:
                return candidate_offset, "entry_limit_price_delta"

        mode = self._config_text("entry_limit_mode", "passive_limit_dynamic").strip().lower()
        if mode in {
            "dynamic",
            "passive_limit_dynamic",
            "passive-limit-dynamic",
            "passive_limit_dynamic_v1",
            "marketable_limit_dynamic",
            "marketable-limit-dynamic",
            "marketable_limit_dynamic_v1",
        }:
            atr = max(
                0.0,
                self._safe_float(extra.get("atr"), 0.0)
                or self._safe_float(raw.get("atr") if isinstance(raw, dict) else 0.0, 0.0),
            )
            atr_mult = max(0.0, self._config_float("entry_limit_atr_mult", 0.30))
            floor_bps = max(0.0, self._config_float("entry_limit_floor_bps", 15.0))
            cap_bps = max(floor_bps, self._config_float("entry_limit_cap_bps", 30.0))
            floor = reference_price * floor_bps / 10000.0 if reference_price > 0 else 0.0
            cap = reference_price * cap_bps / 10000.0 if reference_price > 0 else floor
            raw_offset = atr * atr_mult if atr > 0 else floor
            return max(0.01, min(max(raw_offset, floor), max(floor, cap))), "config.passive_limit_dynamic"

        bps = max(0.0, self._config_float("entry_limit_bps", self._config_float("marketable_limit_bps", 10.0)))
        if reference_price <= 0:
            return 0.0, "none"
        return max(0.01, reference_price * bps / 10000.0), "config.passive_limit_bps"

    def _prepare_pre_submit_signal(self, sig: dict) -> tuple[bool, dict, str]:
        environment = str(_service_mod().ENVIRONMENT or "").strip().lower()
        guard_enabled = self._config_bool("entry_pre_submit_guard_enabled", True)
        reprice_enabled = self._config_bool("entry_pre_submit_reprice_enabled", True)
        if not guard_enabled:
            return True, sig, ""

        symbol = str(sig.get("symbol") or "").strip().upper()
        direction = str(sig.get("direction") or "").strip().lower()
        entry = self._safe_float(sig.get("entry"), 0.0)
        stop_loss = self._safe_float(sig.get("stop_loss"), 0.0)
        take_profit = self._safe_float(sig.get("take_profit"), 0.0)
        risk_r = abs(entry - stop_loss)
        max_age_s = max(0.0, self._config_float("entry_pre_submit_quote_max_age_sec", 10.0))
        max_drift_r = max(0.0, self._config_float("entry_pre_submit_max_adverse_drift_r", 0.50))
        live_environment = environment in {"live", "paper"}

        quote, reference_price, reference_source, fresh_quote = self._entry_guard_fresh_quote(
            symbol,
            direction,
            max_age_s,
        )
        quote_acquire_diagnostics = {
            "quote_acquire_source": "cached_ws" if fresh_quote else "none",
            "quote_acquire_wait_ms": 0,
            "quote_acquire_error": "",
            "temporary_quote_subscription": False,
        }
        guard_reason = "entry_guard_no_fresh_quote"
        if live_environment and not fresh_quote:
            (
                quote,
                reference_price,
                reference_source,
                fresh_quote,
                quote_acquire_diagnostics,
                guard_reason,
            ) = self._entry_acquire_fresh_quote(symbol, direction, max_age_s)

        extra = self._signal_extra(sig)
        diagnostics = {
            "original_entry": entry,
            "original_stop_loss": stop_loss,
            "original_take_profit": take_profit,
            "pre_submit_guard": True,
            "quote_age_s": quote.get("quote_age_s") if quote else None,
            "pre_submit_reference_price": round(reference_price, 4) if reference_price > 0 else None,
            "pre_submit_reference_source": reference_source,
            **quote_acquire_diagnostics,
            "price_drift_r": 0.0,
            "reprice_source": "",
            "entry_limit_offset": self._safe_float(extra.get("entry_limit_offset"), 0.0),
            "entry_order_type": "LMT",
            "entry_limit_intent": "passive",
            "entry_price_plan": "passive_limit",
            "entry_repriced": False,
        }

        if live_environment and not fresh_quote:
            status_reason = guard_reason or "entry_guard_no_fresh_quote"
            sig["extra"] = {**extra, **diagnostics, "status_reason": status_reason}
            return False, sig, status_reason

        if not fresh_quote:
            sig["extra"] = {**extra, **diagnostics}
            return True, sig, ""

        if direction == "long":
            if reference_price <= stop_loss:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_stop_already_crossed"}
                return False, sig, "entry_guard_stop_already_crossed"
            adverse_drift_r = max(0.0, entry - reference_price) / risk_r if risk_r > 0 else 0.0
        elif direction == "short":
            if reference_price >= stop_loss:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_stop_already_crossed"}
                return False, sig, "entry_guard_stop_already_crossed"
            adverse_drift_r = max(0.0, reference_price - entry) / risk_r if risk_r > 0 else 0.0
        else:
            adverse_drift_r = 0.0

        diagnostics["price_drift_r"] = round(adverse_drift_r, 4)
        drift_requires_reprice = adverse_drift_r > max_drift_r
        diagnostics["price_drift_threshold_r"] = max_drift_r
        diagnostics["price_drift_exceeds_threshold"] = bool(drift_requires_reprice)
        if drift_requires_reprice and not reprice_enabled:
            sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_price_drift"}
            return False, sig, "entry_guard_price_drift"

        if not reprice_enabled:
            sig["extra"] = {**extra, **diagnostics}
            return True, sig, ""

        base_price, base_source = self._reprice_base_price(quote, direction)
        if base_price <= 0:
            if drift_requires_reprice:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_price_drift"}
                return False, sig, "entry_guard_price_drift"
            sig["extra"] = {**extra, **diagnostics}
            return True, sig, ""
        offset, offset_source = self._entry_limit_offset(sig, base_price, extra)
        new_entry = base_price + offset if direction == "short" else base_price - offset
        if new_entry <= 0:
            if drift_requires_reprice:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_price_drift"}
                return False, sig, "entry_guard_price_drift"
            sig["extra"] = {**extra, **diagnostics}
            return True, sig, ""

        delta = new_entry - entry
        adjusted_sig = copy.deepcopy(sig)
        adjusted_sig["entry"] = self._round_price(new_entry)
        adjusted_sig["stop_loss"] = self._round_price(stop_loss + delta)
        adjusted_sig["take_profit"] = self._round_price(take_profit + delta)
        diagnostics.update(
            {
                "reprice_source": f"{base_source}{'+' if direction == 'short' else '-'}{offset_source}"
                if offset > 0
                else base_source,
                "entry_limit_offset": round(offset, 4),
                "entry_repriced": (
                    adjusted_sig["entry"] != self._round_price(entry)
                    or adjusted_sig["stop_loss"] != self._round_price(stop_loss)
                    or adjusted_sig["take_profit"] != self._round_price(take_profit)
                ),
            }
        )
        adjusted_sig["extra"] = {**extra, **diagnostics}
        return True, adjusted_sig, ""

    def _apply_order_flow_entry_decision(self, sig: dict, decision: dict) -> dict:
        marketable = decision.get("marketable_limit") if isinstance(decision.get("marketable_limit"), dict) else {}
        entry_price = self._safe_float(marketable.get("price"), 0.0)
        if entry_price <= 0:
            return sig
        adjusted_sig = copy.deepcopy(sig)
        original_entry = self._safe_float(sig.get("entry"), 0.0)
        original_stop = self._safe_float(sig.get("stop_loss"), 0.0)
        original_target = self._safe_float(sig.get("take_profit"), 0.0)
        adjusted_sig["entry"] = self._round_price(entry_price)
        delta = adjusted_sig["entry"] - self._round_price(original_entry)
        if original_stop > 0:
            adjusted_sig["stop_loss"] = self._round_price(original_stop + delta)
        if original_target > 0:
            adjusted_sig["take_profit"] = self._round_price(original_target + delta)
        extra = self._signal_extra(sig)
        adjusted_sig["extra"] = {
            **extra,
            "order_flow": decision,
            "order_flow_shadow": decision,
            "order_flow_enforced": bool(decision.get("enforced")),
            "order_flow_entry_confirmed": True,
            "order_flow_original_entry": original_entry,
            "order_flow_original_stop_loss": original_stop,
            "order_flow_original_take_profit": original_target,
            "order_flow_entry_limit_price": adjusted_sig["entry"],
            "order_flow_stop_loss": adjusted_sig.get("stop_loss"),
            "order_flow_take_profit": adjusted_sig.get("take_profit"),
            "entry_order_type": "LMT",
            "entry_limit_intent": "marketable",
            "entry_price_plan": "order_flow_marketable_limit",
            "entry_repriced": (
                adjusted_sig["entry"] != self._round_price(original_entry)
                or adjusted_sig.get("stop_loss") != self._round_price(original_stop)
                or adjusted_sig.get("take_profit") != self._round_price(original_target)
            ),
        }
        return adjusted_sig

    def _is_fixed_position_signal(self, sig: dict) -> bool:
        lifecycle = getattr(self, "order_lifecycle", None)
        checker = getattr(lifecycle, "is_fixed_position_symbol", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(str(sig.get("symbol") or "")))
        except Exception:
            return False

    def _strategy_capacity_snapshot(self) -> dict:
        lifecycle = getattr(self, "order_lifecycle", None)
        snapshotter = getattr(lifecycle, "strategy_capacity_snapshot", None)
        if not callable(snapshotter):
            return {"capacity_full": False}
        try:
            snapshot = snapshotter(order_tracker=getattr(self, "order_tracker", None))
        except TypeError:
            snapshot = snapshotter()
        except Exception as exc:
            _service_mod().logger.warning("Strategy capacity check failed: %s", exc)
            return {"capacity_full": False, "capacity_check_error": str(exc)}
        return snapshot if isinstance(snapshot, dict) else {"capacity_full": False}

    @staticmethod
    def _buying_power_extra_fields(guard: dict) -> dict:
        guard = dict(guard or {}) if isinstance(guard, dict) else {}
        return {
            "buying_power_guard": guard,
            "buying_power_remaining": guard.get("remaining"),
            "buying_power_remaining_after": guard.get("remaining_after"),
            "buying_power_remaining_pct_net_liq": guard.get("remaining_pct_net_liq"),
            "buying_power_remaining_after_pct_net_liq": guard.get("remaining_after_pct_net_liq"),
            "buying_power_requested_exposure": guard.get("requested_exposure"),
            "buying_power_guard_state": guard.get("state"),
            "buying_power_guard_reason": guard.get("reason"),
            "buying_power_local_reserved_exposure": guard.get("local_reserved_exposure"),
            "buying_power_local_reserved_count": guard.get("local_reserved_count"),
        }

    def _merge_buying_power_snapshot_guard(self, guard: dict, snapshot_guard: dict | None) -> dict:
        if not isinstance(snapshot_guard, dict):
            return guard
        for key in self.BUYING_POWER_SNAPSHOT_META_KEYS:
            if snapshot_guard.get(key) not in (None, ""):
                guard[key] = snapshot_guard.get(key)
        if guard.get("state") == "unavailable" and snapshot_guard.get("reason"):
            guard["reason"] = snapshot_guard.get("reason")
        self._recompute_buying_power_guard_capacity(guard)
        return guard

    def _recompute_buying_power_guard_capacity(self, guard: dict) -> None:
        default_entry_exposure = self._safe_float(guard.get("risk_model_default_entry_exposure"), 0.0)
        if default_entry_exposure <= 0:
            return
        remaining_after = guard.get("remaining_after")
        if remaining_after in (None, ""):
            remaining_after = guard.get("remaining")
        remaining_value = self._safe_float(remaining_after, 0.0)
        block_floor = self._safe_float(guard.get("block_floor"), 0.0)
        guard["risk_model_remaining_slots"] = int(
            math.floor(max(0.0, remaining_value - block_floor) / default_entry_exposure)
        )

    def _buying_power_guard_state_key(self) -> str:
        return "buying_power_guard:auto_entry"

    def _load_buying_power_guard_state(self) -> dict:
        pb = getattr(self, "pb", None)
        getter = getattr(pb, "get_state", None)
        if not callable(getter):
            return {}
        try:
            record = getter(self._buying_power_guard_state_key(), _service_mod().ENVIRONMENT, "global")
        except Exception as exc:
            _service_mod().logger.debug("Buying-power guard state load failed: %s", exc)
            return {}
        data = (record or {}).get("data") if isinstance(record, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return data if isinstance(data, dict) else {}

    def _save_buying_power_guard_state(self, sig: dict, guard: dict) -> None:
        pb = getattr(self, "pb", None)
        upsert = getattr(pb, "upsert_state", None)
        if not callable(upsert):
            return
        state = str((guard or {}).get("state") or "ok").strip().lower() or "ok"
        payload = {
            "state": state,
            "reason": str((guard or {}).get("reason") or ""),
            "source": str((guard or {}).get("source") or ""),
            "signal_id": str((sig or {}).get("signal_id") or ""),
            "symbol": str((sig or {}).get("symbol") or "").upper(),
            "remaining": (guard or {}).get("remaining"),
            "remaining_after": (guard or {}).get("remaining_after"),
            "warn_floor": (guard or {}).get("warn_floor"),
            "block_floor": (guard or {}).get("block_floor"),
            "updated_at": self._now_iso(),
        }
        try:
            upsert(self._buying_power_guard_state_key(), _service_mod().ENVIRONMENT, payload, date="global")
        except Exception as exc:
            _service_mod().logger.debug("Buying-power guard state save failed: %s", exc)

    def _handle_buying_power_guard_state_transition(self, sig: dict, guard: dict) -> None:
        state = str((guard or {}).get("state") or "ok").strip().lower() or "ok"
        previous = self._load_buying_power_guard_state()
        previous_state = str((previous or {}).get("state") or "ok").strip().lower() or "ok"
        if state == "ok" and previous_state in {"warning", "blocked", "unavailable"}:
            self._notify_buying_power_recovered(sig, guard, previous)
        self._save_buying_power_guard_state(sig, guard)

    def _buying_power_guard_detail(self, sig: dict, guard: dict, *, previous: dict | None = None) -> dict:
        def guard_number(key: str):
            raw = (guard or {}).get(key)
            if raw in (None, ""):
                return "不可用"
            try:
                return round(float(raw), 2)
            except (TypeError, ValueError):
                return "不可用"

        detail = {
            "信号ID": str((sig or {}).get("signal_id") or ""),
            "标的": str((sig or {}).get("symbol") or "").upper(),
            "方向": str((sig or {}).get("direction") or ""),
            "数量": int(self._safe_float((sig or {}).get("shares"), 0.0)),
            "风控来源": str((guard or {}).get("source") or "account_summary"),
            "当前剩余购买力": guard_number("remaining"),
            "本次预估占用": guard_number("requested_exposure"),
            "下单后剩余购买力": guard_number("remaining_after"),
            "预警阈值": guard_number("warn_floor"),
            "禁止阈值": guard_number("block_floor"),
            "状态": str((guard or {}).get("state") or "ok").strip().lower() or "ok",
            "原因": str((guard or {}).get("reason") or ""),
        }
        if (guard or {}).get("configured_buying_power") not in (None, ""):
            detail["配置购买力"] = guard_number("configured_buying_power")
        if (guard or {}).get("risk_model_used_exposure") not in (None, ""):
            detail["策略已占用"] = guard_number("risk_model_used_exposure")
        if (guard or {}).get("risk_model_remaining_slots") not in (None, ""):
            detail["估算剩余可开仓数"] = guard.get("risk_model_remaining_slots")
        if isinstance(previous, dict) and previous:
            detail["上一状态"] = str(previous.get("state") or "")
            detail["上一原因"] = str(previous.get("reason") or "")
        return detail

    def _notify_buying_power_recovered(self, sig: dict, guard: dict, previous: dict) -> None:
        if not self._buying_power_notify_enabled():
            return
        pb = getattr(self, "pb", None)
        notifier = getattr(pb, "notify_system_event", None)
        if not callable(notifier):
            return
        try:
            notifier(
                "自动开仓购买力风控已恢复",
                self._buying_power_guard_detail(sig, guard, previous=previous),
                event_type="alert",
                level="info",
                source="ibkr_compute",
                environment=_service_mod().ENVIRONMENT,
            )
        except Exception as exc:
            _service_mod().logger.warning("Buying-power recovery notification failed: %s", exc)

    def _account_buying_power_snapshot(self) -> dict:
        provider = getattr(self, "account_snapshot_provider", None)
        if callable(provider):
            try:
                snapshot = provider()
                return snapshot if isinstance(snapshot, dict) else {}
            except Exception as exc:
                _service_mod().logger.warning("Buying-power snapshot provider failed: %s", exc)
                return {}
        try:
            from ibkr_compute.api.account.snapshot import _build_ibkr_account_buying_power_snapshot

            snapshot = _build_ibkr_account_buying_power_snapshot(self)
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as exc:
            _service_mod().logger.warning("Buying-power snapshot failed: %s", exc)
            return {}

    def _evaluate_signal_buying_power_guard(self, sig: dict) -> dict:
        service_mod = _service_mod()
        snapshot = self._account_buying_power_snapshot()
        reservation_store = getattr(self, "buying_power_reservations", None)
        reservation_snapshot = {}
        snapshotter = getattr(reservation_store, "snapshot", None)
        if callable(snapshotter):
            try:
                reservation_snapshot = snapshotter()
            except Exception as exc:
                service_mod.logger.warning("Buying-power reservation snapshot failed: %s", exc)
                reservation_snapshot = {}
        summary = apply_reservations_to_buying_power_summary(
            (snapshot or {}).get("summary") or {},
            reservation_snapshot,
        )
        exposure = estimate_entry_exposure(
            sig.get("shares"),
            sig.get("entry"),
            sig.get("take_profit"),
            sig.get("stop_loss"),
            sig.get("direction"),
            "LMT",
        )
        guard = build_buying_power_guard(
            summary,
            config=getattr(self, "config", None),
            environment=service_mod.ENVIRONMENT,
            requested_exposure=exposure,
        )
        merge_reservation_snapshot_into_guard(guard, reservation_snapshot)
        snapshot_guard = (snapshot or {}).get("buying_power_guard")
        self._merge_buying_power_snapshot_guard(guard, snapshot_guard if isinstance(snapshot_guard, dict) else None)
        if isinstance((snapshot or {}).get("errors"), dict):
            guard["snapshot_errors"] = dict((snapshot or {}).get("errors") or {})
        guard["snapshot_fetched_at"] = (snapshot or {}).get("fetched_at") or ""
        if exposure <= 0 and guard.get("enabled"):
            guard["state"] = "blocked"
            guard["reason"] = "buying_power_price_unavailable"
        self._recompute_buying_power_guard_capacity(guard)
        return guard

    def _buying_power_notify_enabled(self) -> bool:
        return self._config_bool("ibkr_buying_power_notify_enabled", True)

    def _buying_power_guard_notify_allowed(self, sig: dict, guard: dict, level: str, *, force: bool = False) -> bool:
        if force:
            return True
        cooldown = max(0.0, self._config_float("ibkr_buying_power_notify_cooldown_sec", 1800.0))
        if cooldown <= 0:
            return True
        state = str((guard or {}).get("state") or "ok").strip().lower()
        symbol = str((sig or {}).get("symbol") or "").strip().upper()
        key = f"{_service_mod().ENVIRONMENT}:{level}:{state}:{symbol}"
        now = time.time()
        last_map = getattr(self, "_buying_power_guard_last_notify", None)
        if not isinstance(last_map, dict):
            last_map = {}
            setattr(self, "_buying_power_guard_last_notify", last_map)
        last = float(last_map.get(key) or 0.0)
        if last and now - last < cooldown:
            return False
        last_map[key] = now
        return True

    def _notify_buying_power_guard(
        self,
        sig: dict,
        guard: dict,
        *,
        level: str,
        event_type: str = "alert",
        force: bool = False,
    ) -> None:
        if not self._buying_power_notify_enabled():
            return
        if not self._buying_power_guard_notify_allowed(sig, guard, level, force=force):
            return
        pb = getattr(self, "pb", None)
        notifier = getattr(pb, "notify_system_event", None)
        if not callable(notifier):
            return
        state = str((guard or {}).get("state") or "ok").strip().lower()
        title = "自动开仓购买力预警"
        if state == "unavailable":
            title = "自动开仓暂停：购买力风控不可用"
        elif state == "blocked":
            title = "自动开仓已被动态购买力上限拦截"
        elif level == "info":
            title = "自动开仓已提交"
        try:
            notifier(
                title,
                self._buying_power_guard_detail(sig, guard),
                event_type=event_type,
                level=level,
                source="ibkr_compute",
                environment=_service_mod().ENVIRONMENT,
            )
        except Exception as exc:
            _service_mod().logger.warning("Buying-power notification failed: %s", exc)

    def _load_signal_record_and_extra(self, sig: dict) -> tuple[dict, dict]:
        service_mod = _service_mod()
        if not self.pb:
            return {}, {}

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return {}, {}

        safe_signal_id = signal_id.replace('"', '\\"')
        safe_environment = str(service_mod.DATA_ENVIRONMENT or "live").replace('"', '\\"')
        record = self.pb.get_first_record(
            "ibkr_signals",
            filter=(
                f'signal_id = "{safe_signal_id}" && '
                f'environment = "{safe_environment}"'
            ),
        )
        if not record or not record.get("id"):
            return {}, {}

        existing_extra = record.get("extra") or {}
        if isinstance(existing_extra, str):
            try:
                existing_extra = json.loads(existing_extra)
            except Exception:
                existing_extra = {}
        if not isinstance(existing_extra, dict):
            existing_extra = {}
        return record, existing_extra

    def _signal_broker_patch(self, status: str, note: str, existing_extra: dict, extra: dict | None = None) -> dict:
        service_mod = _service_mod()
        broker_mode = str(service_mod.ENVIRONMENT or "live").strip().lower() or "live"
        data_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live"
        status_text = str(status or "").strip() or "pending"
        note_text = str(note or status_text).strip() or status_text
        merged_extra = {
            **(existing_extra if isinstance(existing_extra, dict) else {}),
            **(extra if isinstance(extra, dict) else {}),
            "last_runtime_broker_mode": broker_mode,
            "last_runtime_data_environment": data_environment,
        }
        execution_by_mode = merged_extra.get("execution_by_mode")
        if not isinstance(execution_by_mode, dict):
            execution_by_mode = {}
        broker_execution = execution_by_mode.get(broker_mode)
        if not isinstance(broker_execution, dict):
            broker_execution = {}
        execution_by_mode[broker_mode] = {
            **broker_execution,
            "status": status_text,
            "note": note_text,
            "status_reason": str((extra or {}).get("status_reason") or note_text).strip() or status_text,
            "data_environment": data_environment,
            "updated_at": self._now_iso(),
            "source": "ibkr_compute",
        }
        merged_extra["execution_by_mode"] = execution_by_mode
        patch = {"extra": merged_extra}
        existing_note = str((existing_extra or {}).get("note") or "").strip().lower()
        if existing_note.startswith(f"{broker_mode}:") or "history_repair_pending" in existing_note:
            patch["note"] = ""
        if broker_mode == data_environment == "live":
            patch.update({"status": status_text, "note": note_text})
        return patch

    def _ack_signal_lifecycle_update(self, sig: dict, status: str, note: str, extra: dict | None = None) -> bool:
        service_mod = _service_mod()
        ack = getattr(self.pb, "ack_ibkr_signal", None)
        signal_id = str(sig.get("signal_id") or "").strip()
        if not callable(ack) or not signal_id:
            return False
        try:
            ack(
                signal_id=signal_id,
                status=status,
                note=note,
                environment=service_mod.ENVIRONMENT,
                extra=dict(extra or {}),
                lifecycle_update=True,
            )
            return True
        except TypeError:
            # Older test doubles or clients may not yet support lifecycle-only ack.
            return False
        except Exception as exc:
            service_mod.logger.warning(
                "Canonical signal lifecycle ack failed; using fallback patch: signal_id=%s status=%s note=%s error=%s",
                signal_id,
                status,
                note,
                exc,
            )
            return False

    def _mark_signal_order_flow_waiting(self, sig: dict, decision: dict):
        service_mod = _service_mod()
        if not self.pb:
            return
        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            patch = self._signal_broker_patch(
                "pending",
                "order_flow_waiting",
                existing_extra,
                {
                    **self._signal_extra(sig),
                    "status_reason": "order_flow_waiting",
                    "execution_state": "waiting_for_order_flow",
                    "order_flow_waiting": True,
                    "order_flow_waiting_at": self._now_iso(),
                    "order_flow": dict(decision or {}),
                    "order_flow_shadow": dict(decision or {}),
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal order-flow-waiting: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_order_flow_rejected(self, sig: dict, decision: dict):
        service_mod = _service_mod()
        if not self.pb:
            return
        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return
        reason = str((decision or {}).get("reason") or "order_flow_rejected").strip() or "order_flow_rejected"
        status = "expired" if reason == "order_flow_timeout" else "rejected"
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            lifecycle_extra = {
                **self._signal_extra(sig),
                "status_reason": reason,
                "execution_state": "order_flow_rejected",
                "order_flow_rejected": True,
                "order_flow_rejected_at": self._now_iso(),
                "order_flow": dict(decision or {}),
                "order_flow_shadow": dict(decision or {}),
            }
            if self._ack_signal_lifecycle_update(sig, status, reason, lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                status,
                reason,
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal order-flow-rejected: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_waiting_for_capacity(self, sig: dict, capacity: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            patch = self._signal_broker_patch(
                "pending",
                "strategy_capacity_full",
                existing_extra,
                {
                    "status_reason": "strategy_capacity_full",
                    "execution_state": "waiting_for_capacity",
                    "waiting_for_capacity": True,
                    "waiting_for_capacity_at": self._now_iso(),
                    "strategy_capacity": dict(capacity or {}),
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal waiting-for-capacity: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_fixed_position_blocked(self, sig: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            fixed_symbols = []
            lifecycle = getattr(self, "order_lifecycle", None)
            getter = getattr(lifecycle, "_fixed_position_symbols", None)
            if callable(getter):
                fixed_symbols = sorted(getter())
            lifecycle_extra = {
                "status_reason": "fixed_position_symbol_blocked",
                "execution_state": "blocked",
                "fixed_position_symbol_blocked": True,
                "fixed_position_symbol": str(sig.get("symbol") or "").strip().upper(),
                "fixed_position_symbols": fixed_symbols,
                "blocked_at": self._now_iso(),
            }
            if self._ack_signal_lifecycle_update(sig, "rejected", "fixed_position_symbol_blocked", lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                "rejected",
                "fixed_position_symbol_blocked",
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal fixed-position-blocked: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_buying_power_blocked(self, sig: dict, guard: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            signal_extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
            lifecycle_extra = {
                **signal_extra,
                **self._buying_power_extra_fields(guard),
                "status_reason": "buying_power_blocked",
                "execution_state": "blocked",
                "buying_power_blocked": True,
                "buying_power_blocked_at": self._now_iso(),
            }
            if self._ack_signal_lifecycle_update(sig, "rejected", "buying_power_blocked", lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                "rejected",
                "buying_power_blocked",
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal buying-power-blocked: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_buying_power_unavailable(self, sig: dict, guard: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        reason = str((guard or {}).get("reason") or "account_snapshot_unavailable").strip()
        if not reason:
            reason = "account_snapshot_unavailable"
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            signal_extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
            patch = self._signal_broker_patch(
                "pending",
                reason,
                existing_extra,
                {
                    **signal_extra,
                    **self._buying_power_extra_fields(guard),
                    "status_reason": reason,
                    "execution_state": "waiting_for_account_snapshot",
                    "waiting_for_account_snapshot": True,
                    "waiting_for_account_snapshot_at": self._now_iso(),
                    "buying_power_blocked": False,
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal buying-power-unavailable: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _patch_signal_pre_submit_prices(self, sig: dict):
        extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        if not bool(extra.get("entry_repriced")) or not self.pb:
            return
        service_mod = _service_mod()
        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            patch = self._signal_broker_patch(
                "pending",
                "pre_submit_prices_patched",
                existing_extra,
                {
                    **extra,
                    "pre_submit_prices_patched": True,
                    "pre_submit_prices_patched_at": self._now_iso(),
                },
            )
            if service_mod.ENVIRONMENT == service_mod.DATA_ENVIRONMENT == "live":
                patch.update(
                    {
                        "entry": self._round_price(sig.get("entry")),
                        "stop_loss": self._round_price(sig.get("stop_loss")),
                        "take_profit": self._round_price(sig.get("take_profit")),
                    }
                )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to patch signal pre-submit prices: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_validation_rejected(self, sig: dict, reason: str):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            status_reason = str(reason or "validation_rejected").strip() or "validation_rejected"
            lifecycle_extra = {
                **self._validation_rejection_extra(sig, status_reason),
                "validation_rejected": True,
                "validation_rejected_at": self._now_iso(),
            }
            if self._ack_signal_lifecycle_update(sig, "rejected", status_reason, lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                "rejected",
                status_reason,
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal validation-rejected: signal_id=%s reason=%s error=%s",
                signal_id,
                reason,
                exc,
            )

    def _mark_signal_validation_expired(self, sig: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        ack = getattr(self.pb, "ack_ibkr_signal", None)
        if callable(ack):
            try:
                ack(
                    signal_id=signal_id,
                    status="expired",
                    note="signal_expired",
                    environment=service_mod.ENVIRONMENT,
                    extra={
                        "status_reason": "signal_expired",
                        "expired_by": "ibkr_compute_validation",
                        "expired_at": self._now_iso(),
                        "validation_expired": True,
                        "validation_expired_at": self._now_iso(),
                    },
                    lifecycle_update=True,
                )
                return
            except Exception as exc:
                service_mod.logger.warning(
                    "Canonical signal-expired ack failed; using fallback patch: signal_id=%s error=%s",
                    signal_id,
                    exc,
                )

        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            expired_at = self._now_iso()
            patch = self._signal_broker_patch(
                "expired",
                "signal_expired",
                existing_extra,
                {
                    "status_reason": "signal_expired",
                    "expired_by": "ibkr_compute_validation_fallback",
                    "expired_at": expired_at,
                    "validation_expired": True,
                    "validation_expired_at": expired_at,
                },
            )
            broker_mode = str(service_mod.ENVIRONMENT or "live").strip().lower() or "live"
            execution_by_mode = patch["extra"].get("execution_by_mode")
            broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
            if not isinstance(broker_execution, dict):
                broker_execution = {}
            execution_by_mode[broker_mode] = {
                **broker_execution,
                "status": "expired",
                "note": "signal_expired",
                "expired_by": "ibkr_compute_validation_fallback",
                "expired_at": expired_at,
                "status_reason": "signal_expired",
            }
            patch["extra"]["execution_by_mode"] = execution_by_mode
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal validation-expired: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_entry_guard_rejected(self, sig: dict, reason: str):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        status_reason = str(reason or "entry_guard_rejected").strip() or "entry_guard_rejected"
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return

            guard_extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
            lifecycle_extra = {
                **guard_extra,
                "status_reason": status_reason,
                "entry_guard_rejected": True,
                "entry_guard_rejected_at": self._now_iso(),
            }
            if self._ack_signal_lifecycle_update(sig, "rejected", status_reason, lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                "rejected",
                status_reason,
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal entry-guard-rejected: signal_id=%s reason=%s error=%s",
                signal_id,
                reason,
                exc,
            )

    def _mark_signal_duplicate_open_order(self, sig: dict, broker_order: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            broker_order_id = str(broker_order.get("orderId") or broker_order.get("id") or "").strip()
            broker_coid = str(
                broker_order.get("cOID")
                or broker_order.get("coid")
                or broker_order.get("order_ref")
                or broker_order.get("orderRef")
                or ""
            ).strip()
            lifecycle_extra = {
                "status_reason": "duplicate_existing_broker_order",
                "duplicate_broker_order_detected": True,
                "duplicate_broker_order_id": broker_order_id,
                "duplicate_broker_order_status": str(broker_order.get("status") or "").strip(),
                "duplicate_broker_order_price": broker_order.get("price"),
                "duplicate_broker_order_quantity": (
                    broker_order.get("totalSize")
                    if broker_order.get("totalSize") is not None
                    else broker_order.get("quantity")
                ),
                "duplicate_broker_order_side": str(broker_order.get("side") or "").strip(),
                "duplicate_broker_order_type": str(broker_order.get("orderType") or "").strip(),
                "duplicate_broker_order_coid": broker_coid,
                "duplicate_detected_at": self._now_iso(),
                "duplicate_action": "skip_submit_existing_broker_order",
            }
            if self._ack_signal_lifecycle_update(sig, "rejected", "duplicate_existing_broker_order", lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                "rejected",
                "duplicate_existing_broker_order",
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal duplicate-open-order: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _mark_signal_submit_failed(self, sig: dict, result: dict):
        service_mod = _service_mod()
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            error_text = str((result or {}).get("error") or "submit_failed").strip() or "submit_failed"
            entry_error = (result or {}).get("entry_error") if isinstance(result, dict) else {}
            if not isinstance(entry_error, dict):
                entry_error = {}

            protection_incomplete = self._is_protection_incomplete_result(result or {})
            status = "protection_incomplete" if protection_incomplete else "rejected"
            note = "protection_incomplete" if protection_incomplete else "submit_failed"
            status_reason = "bracket_protection_incomplete" if protection_incomplete else "submit_failed"
            cancel_sync = self._cancel_unconfirmed_submission_orders(result or {})
            diagnostic = (
                self._build_protection_incomplete_diagnostic(
                    sig,
                    result or {},
                    "bracket_submission_protection_incomplete",
                )
                if protection_incomplete
                else {}
            )
            protection_fields = self._protection_fields(result or {}, diagnostic)
            lifecycle_extra = {
                "status_reason": status_reason,
                "submit_failed": not protection_incomplete,
                "submit_failed_error": error_text,
                "submit_failed_at": self._now_iso(),
                "submit_failed_order_ids": list((result or {}).get("order_ids") or []),
                "submit_failed_bracket_group": str((result or {}).get("bracket_group") or ""),
                "submit_failed_entry_error_code": entry_error.get("code"),
                "submit_failed_entry_error": str(entry_error.get("error") or ""),
                "submit_failed_entry_details": entry_error,
                "protection_incomplete": protection_incomplete,
                "protection_complete": False if protection_incomplete else bool((result or {}).get("protection_complete")),
                "missing_order_ids": list((result or {}).get("missing_order_ids") or []),
                **protection_fields,
                "protection_incomplete_diagnostic": diagnostic,
                "safety_cancel_recommended": bool(diagnostic.get("cancel_recommended")) if diagnostic else False,
                "submit_failed_cancel_sync": cancel_sync,
            }
            if self._ack_signal_lifecycle_update(sig, status, note, lifecycle_extra):
                return
            patch = self._signal_broker_patch(
                status,
                note,
                existing_extra,
                lifecycle_extra,
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal submit-failed: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    @staticmethod
    def _result_order_ids(result: dict) -> list[str]:
        values = []
        for key in ("order_ids", "submitted_order_ids", "broker_order_ids", "missing_order_ids"):
            raw = (result or {}).get(key)
            if isinstance(raw, str):
                values.extend(part.strip() for part in raw.split(","))
            elif isinstance(raw, (list, tuple, set)):
                values.extend(str(item or "").strip() for item in raw)
        seen = set()
        order_ids = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            order_ids.append(text)
        return order_ids

    def _cancel_unconfirmed_submission_orders(self, result: dict) -> dict:
        error_text = str((result or {}).get("error") or "").lower()
        order_ids = self._result_order_ids(result or {})
        should_cancel = bool(order_ids) and (
            "order_submission_unconfirmed" in error_text
            or "missing=" in error_text
            or bool((result or {}).get("protection_incomplete"))
            or bool((result or {}).get("missing_order_ids"))
        )
        detail = {
            "real_order_required": True,
            "precondition": "traceable_broker_order_ids",
            "order_ids": order_ids,
            "attempted": False,
            "gateway_request_blocked": not should_cancel,
            "reason": "not_unconfirmed_submission" if not should_cancel else "",
            "results": [],
        }
        if not should_cancel:
            return detail

        cancel = getattr(getattr(self, "order_modifier", None), "cancel_order", None)
        if not callable(cancel):
            detail["reason"] = "order_modifier_unavailable"
            detail["gateway_request_blocked"] = True
            return detail

        detail["attempted"] = True
        detail["gateway_request_blocked"] = False
        entry_order_id = order_ids[0] if order_ids else ""
        for order_id in order_ids:
            try:
                result_row = cancel(order_id)
            except Exception as exc:
                result_row = {"ok": False, "error": str(exc), "exception": type(exc).__name__}
            if order_id == entry_order_id and bool((result_row or {}).get("ok")):
                reservation_releaser = getattr(getattr(self, "buying_power_reservations", None), "release", None)
                if callable(reservation_releaser):
                    try:
                        reservation_releaser(entry_order_id=order_id, reason="submit_failed_cancel_sync")
                    except Exception as exc:
                        detail.setdefault("reservation_release_errors", []).append(str(exc))
            detail["results"].append({"order_id": order_id, **dict(result_row or {})})
        failed = [row for row in detail["results"] if not row.get("ok")]
        detail["ok"] = not failed
        detail["failed_order_ids"] = [row.get("order_id") for row in failed]
        detail["reason"] = "cancel_sync_failed" if failed else "cancel_sync_requested"
        return detail

    @staticmethod
    def _live_trailing_stop_metadata(signal_extra: dict) -> dict:
        signal_extra = signal_extra if isinstance(signal_extra, dict) else {}
        settings = signal_extra.get("exit_policy_settings") if isinstance(signal_extra.get("exit_policy_settings"), dict) else {}
        target_mode = str(settings.get("target_mode") or "").strip().lower()
        has_trail_intent = bool(
            signal_extra.get("trail_state")
            or "trail" in target_mode
            or str(settings.get("trail_type") or "").strip()
        )
        if not has_trail_intent:
            return {}
        return {
            "live_trailing_stop": {
                "supported": True,
                "mode": "completed_5m_bar_strategy_stop_updates",
                "parity_source": "shared_risk_management_exit_policy",
            }
        }

    def _ack_signal_after_order_submission(self, sig: dict, result: dict):
        service_mod = _service_mod()
        submitted_result = dict(result or {})
        result = self._primary_order_result(submitted_result)
        raw = sig.get("raw") or {}
        order_ids = result.get("order_ids") or []
        entry_order_id = str(order_ids[0]) if len(order_ids) > 0 and order_ids[0] else ""
        tp_order_id = str(order_ids[1]) if len(order_ids) > 1 and order_ids[1] else ""
        sl_order_id = str(order_ids[2]) if len(order_ids) > 2 and order_ids[2] else ""
        entry_unique_id = result.get("entry_coid") or result.get("bracket_group") or ""
        tp_unique_id = result.get("tp_coid") or ""
        sl_unique_id = result.get("sl_coid") or ""
        trade_group_id = result.get("trade_group_id") or result.get("bracket_group") or entry_unique_id
        order_family_type = str(result.get("order_family_type") or ("bracket_oco" if trade_group_id else "")).strip()
        raw_oca_group = str(result.get("oca_group") or "").strip()
        oca_group = raw_oca_group or (trade_group_id if order_family_type == "bracket_oco" else "")
        ack_quantity = int(result.get("quantity") or sig["shares"] or 0)
        tp_quantity = int(result.get("take_profit_quantity") or ack_quantity)
        sl_quantity = int(result.get("stop_loss_quantity") or ack_quantity)
        order_extra = dict(result.get("order_extra") or {})
        harvest_legs = [dict(item) for item in (submitted_result.get("legs") or []) if isinstance(item, dict)]
        harvest_fields = {}
        if submitted_result.get("harvest_split") or order_extra.get("harvest_managed"):
            harvest_fields = {
                "intraday_harvest_managed": True,
                "intraday_harvest_profile": submitted_result.get("harvest_profile")
                or order_extra.get("harvest_profile")
                or "intraday_volatility_harvest_v1",
                "intraday_harvest_split": bool(submitted_result.get("harvest_split")),
                "intraday_harvest_lot": order_extra.get("harvest_lot") or result.get("lot") or "",
                "intraday_harvest_legs": [
                    {
                        "lot": item.get("lot") or "",
                        "quantity": item.get("quantity") or 0,
                        "order_ids": list(item.get("order_ids") or []),
                        "bracket_group": item.get("bracket_group") or "",
                        "entry_coid": item.get("entry_coid") or "",
                        "tp_coid": item.get("tp_coid") or "",
                        "sl_coid": item.get("sl_coid") or "",
                        "ok": bool(item.get("ok")),
                    }
                    for item in harvest_legs
                ],
            }
        bar_time_ms = int(raw.get("bar_time_ms") or 0)
        us_time = raw.get("us_time") or sig.get("signal_time") or ""
        cn_time = raw.get("cn_time") or ""
        signal_extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        if not signal_extra:
            raw_extra = raw.get("extra") if isinstance(raw, dict) else {}
            if isinstance(raw_extra, str):
                try:
                    raw_extra = json.loads(raw_extra)
                except Exception:
                    raw_extra = {}
            signal_extra = raw_extra if isinstance(raw_extra, dict) else {}
        if bool(signal_extra.get("tv_direct_entry")):
            order_extra = {**signal_extra, **order_extra}
        exit_policy_fields = {
            key: signal_extra.get(key)
            for key in (
                "exit_policy_profile",
                "exit_policy",
                "exit_policy_type",
                "risk_r",
                "initial_stop_loss",
                "initial_take_profit",
                "exit_policy_settings",
                "trail_state",
            )
            if signal_extra.get(key) not in (None, "")
        }
        exit_policy_fields.update(self._live_trailing_stop_metadata(signal_extra))
        quote_guard_fields = {
            key: signal_extra.get(key)
            for key in (
                "pre_submit_guard",
                "quote_age_s",
                "pre_submit_reference_price",
                "pre_submit_reference_source",
                "quote_acquire_source",
                "quote_acquire_wait_ms",
                "quote_acquire_error",
                "quote_acquire_conid",
                "temporary_quote_subscription",
                "price_drift_r",
                "price_drift_threshold_r",
                "price_drift_exceeds_threshold",
                "original_entry",
                "original_stop_loss",
                "original_take_profit",
            )
            if signal_extra.get(key) not in (None, "")
        }
        buying_power_guard = (
            dict(signal_extra.get("buying_power_guard"))
            if isinstance(signal_extra.get("buying_power_guard"), dict)
            else {}
        )
        buying_power_fields = self._buying_power_extra_fields(buying_power_guard) if buying_power_guard else {}
        protection_complete = bool(result.get("protection_complete"))
        protection_incomplete = not protection_complete
        diagnostic = (
            self._build_protection_incomplete_diagnostic(
                sig,
                result,
                "bracket_ack_protection_incomplete",
            )
            if protection_incomplete
            else {}
        )
        protection_fields = self._protection_fields(result, diagnostic)
        tv_direct_ack = bool(signal_extra.get("tv_direct_entry"))
        signal_status = (
            "submitted_waiting_fill"
            if protection_complete and tv_direct_ack
            else "submitted"
            if protection_complete
            else "protection_incomplete"
        )
        signal_note = (
            "submitted_waiting_fill"
            if protection_complete and tv_direct_ack
            else "order_submitted_by_ibkr_compute"
            if protection_complete
            else "protection_incomplete"
        )

        child_orders = []
        if tp_unique_id:
            child_orders.append(
                {
                    "unique_id": tp_unique_id,
                    "order_id": tp_order_id,
                    "broker_order_id": tp_order_id,
                    "order_type": "TakeProfit",
                    "role": "take_profit",
                    "relation_status": "planned",
                    "trade_group_id": trade_group_id,
                    "bracket_group": trade_group_id,
                    "entry_order_unique_id": entry_unique_id,
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": sl_unique_id,
                    "quantity": tp_quantity,
                    "limit_price": sig["take_profit"],
                    "status": "Submitted" if protection_complete else "Init",
                    "extra": {
                        "bracket_group": trade_group_id,
                        "oca_group": oca_group,
                        "order_family_type": order_family_type,
                        **order_extra,
                        **exit_policy_fields,
                    },
                }
            )
        if sl_unique_id:
            child_orders.append(
                {
                    "unique_id": sl_unique_id,
                    "order_id": sl_order_id,
                    "broker_order_id": sl_order_id,
                    "order_type": "StopLoss",
                    "role": "stop_loss",
                    "relation_status": "planned",
                    "trade_group_id": trade_group_id,
                    "bracket_group": trade_group_id,
                    "entry_order_unique_id": entry_unique_id,
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": tp_unique_id,
                    "quantity": sl_quantity,
                    "limit_price": sig["stop_loss"],
                    "status": "Submitted" if protection_complete else "Init",
                    "extra": {
                        "bracket_group": trade_group_id,
                        "oca_group": oca_group,
                        "order_family_type": order_family_type,
                        **order_extra,
                        **exit_policy_fields,
                    },
                }
            )

        ack_payload = {
            "unique_id": entry_unique_id,
            "order_id": entry_order_id,
            "broker_order_id": entry_order_id,
            "order_type": "Entry",
            "role": "entry",
            "relation_status": "active" if protection_complete else "protection_incomplete",
            "direction": sig["direction"],
            "position_side": sig["direction"],
            "quantity": ack_quantity,
            "limit_price": sig["entry"],
            "status": "Submitted",
            "stop_loss": sig["stop_loss"],
            "take_profit": sig["take_profit"],
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": entry_unique_id,
            "us_time": us_time,
            "cn_time": cn_time,
            "bar_time_ms": bar_time_ms,
            "extra": {
                "source": "ibkr_compute",
                "reason": "order_submitted_by_ibkr_compute",
                "ack_source": "ibkr_service",
                "entry_order_type": "LMT",
                "entry_limit_intent": str(signal_extra.get("entry_limit_intent") or "passive"),
                "entry_price_plan": str(signal_extra.get("entry_price_plan") or "passive_limit"),
                "submitted_entry_limit_price": sig["entry"],
                "bracket_group": trade_group_id,
                "oca_group": oca_group,
                "order_family_type": order_family_type,
                "signal_lifecycle_status": signal_status,
                "protection_complete": protection_complete,
                "protection_incomplete": protection_incomplete,
                "missing_order_ids": list(result.get("missing_order_ids") or []),
                **protection_fields,
                "submitted_order_ids": list(result.get("order_ids") or []),
                "status_reason": signal_note if protection_complete else "bracket_protection_incomplete",
                "protection_incomplete_diagnostic": diagnostic,
                "safety_cancel_recommended": bool(diagnostic.get("cancel_recommended")) if diagnostic else False,
                **order_extra,
                **harvest_fields,
                **exit_policy_fields,
                **quote_guard_fields,
                **buying_power_fields,
                "order_flow": signal_extra.get("order_flow", {}),
                "order_flow_shadow": signal_extra.get("order_flow_shadow", {}),
            },
        }

        ack_result = self.pb.ack_ibkr_signal(
            signal_id=sig["signal_id"],
            status=signal_status,
            note=signal_note,
            order=ack_payload,
            child_orders=child_orders,
            environment=service_mod.ENVIRONMENT,
        )
        service_mod.logger.info(
            "Signal acked after order submission: signal_id=%s status=%s fallback=%s",
            sig.get("signal_id"),
            ack_result.get("status", "unknown"),
            ack_result.get("fallback", False),
        )
