from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from ibkr_compute.api.account.buying_power_guard import (
    build_buying_power_guard,
    estimate_entry_exposure,
)
from ibkr_compute.core.time_utils import ET
from ibkr_compute.observability.prometheus import record_signal_event, set_buying_power_guard_effective_metrics
from ibkr_compute.order.buying_power_reservations import (
    apply_reservations_to_buying_power_summary,
    merge_reservation_snapshot_into_guard,
)
from ibkr_compute.order.scale_plan import normalize_independent_scale_plan_extra


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
    TV_ENTRY_WORKING_ORDER_STATUSES = {
        "active",
        "apipending",
        "apisent",
        "held",
        "open",
        "pending",
        "pendingsubmit",
        "presubmitted",
        "submitted",
        "working",
    }
    TV_ENTRY_FILLED_ORDER_STATUSES = {"executed", "filled"}
    TV_ENTRY_TERMINAL_ORDER_STATUSES = {
        "apicancelled",
        "canceled",
        "cancelled",
        "executed",
        "expired",
        "filled",
        "inactive",
        "rejected",
    }

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

    def _signal_submit_max_concurrency(self) -> int:
        return max(1, int(self._config_float("ibkr_signal_submit_max_concurrency", 12.0)))

    def _process_signals(self):
        service_mod = _service_mod()
        if not self.session_keeper.is_authenticated:
            service_mod.logger.info("Skip signal processing while session is unauthenticated")
            return

        pending_signals = self.signal_router.fetch_pending_signals()
        if not pending_signals:
            return

        signals_by_symbol: dict[str, list[dict]] = {}
        for sig in pending_signals:
            symbol = str((sig or {}).get("symbol") or "").strip().upper()
            signal_id = str((sig or {}).get("signal_id") or "").strip()
            key = symbol or signal_id or "_unknown"
            signals_by_symbol.setdefault(key, []).append(sig)

        signal_batches = list(signals_by_symbol.values())
        max_workers = min(len(signal_batches), self._signal_submit_max_concurrency())
        if max_workers <= 1 or not self._fast_order_accept_enabled() or not getattr(self, "_running", False):
            self._process_signal_batch_serial(pending_signals)
            return

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="signal-submit") as executor:
            futures = [executor.submit(self._process_signal_batch_serial, batch) for batch in signal_batches]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    service_mod.logger.error("Signal submit worker failed: %s", exc)
                    record_signal_event(
                        environment=service_mod.ENVIRONMENT,
                        stage="signal_submit_fanout",
                        signal_source="runtime_loop",
                        result="error",
                        reason_code=exc.__class__.__name__,
                    )

    def _process_signal_batch_serial(self, pending_signals: list[dict]):
        service_mod = _service_mod()
        if not self.session_keeper.is_authenticated:
            service_mod.logger.info("Skip signal processing while session is unauthenticated")
            return

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
                    reconcile_result = self._reconcile_tv_direct_entry_before_submit(sig)
                    if reconcile_result.get("handled"):
                        service_mod.logger.warning(
                            "TV direct signal reconciled before duplicate submit: signal_id=%s symbol=%s status=%s source=%s",
                            signal_id,
                            sig.get("symbol"),
                            reconcile_result.get("status") or "-",
                            reconcile_result.get("source") or "-",
                        )
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
                        trade_group_id = str(
                            extra.get("trade_group_id")
                            or extra.get("bracket_group")
                            or extra.get("leg_trade_group_id")
                            or signal_id
                        ).strip()
                        order_extra = {
                            **extra,
                            "trade_group_id": trade_group_id,
                            "bracket_group": trade_group_id,
                        }
                        order_extra = normalize_independent_scale_plan_extra(
                            order_extra,
                            signal_id=signal_id,
                            trade_group_id=trade_group_id,
                            symbol=symbol,
                            direction=str(sig.get("direction") or ""),
                        )
                        sig["extra"] = order_extra
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
                            buying_power_guard=buying_power_guard,
                            confirmation_mode="background",
                            outside_rth=bool(extra.get("outside_rth")),
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
                            buying_power_guard=buying_power_guard,
                            confirmation_mode="background",
                            outside_rth=bool(extra.get("outside_rth")),
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
                    submitted_buying_power_guard = (
                        dict(result.get("buying_power_guard"))
                        if isinstance(result.get("buying_power_guard"), dict)
                        else dict(buying_power_guard)
                    )
                    if submitted_buying_power_guard.get("enabled"):
                        result["buying_power_guard"] = dict(submitted_buying_power_guard)
                        self._notify_buying_power_guard(
                            sig,
                            submitted_buying_power_guard,
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
                        self._submit_signal_ack_after_order_submission(sig, result)
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
                        reason_code=str(result.get("error") or "")
                        if result.get("error") in {"buying_power_blocked", "buying_power_unavailable"}
                        else "protection_incomplete"
                        if result.get("protection_incomplete")
                        else "submit_failed",
                        duration_s=submission_duration,
                    )
                    result_guard = result.get("buying_power_guard") if isinstance(result.get("buying_power_guard"), dict) else {}
                    if result.get("error") == "buying_power_blocked" and result_guard:
                        self._handle_buying_power_guard_state_transition(sig, result_guard)
                        self._mark_signal_buying_power_blocked(sig, result_guard)
                        self._notify_buying_power_guard(sig, result_guard, level="error", event_type="alert", force=True)
                    elif result.get("error") == "buying_power_unavailable" and result_guard:
                        self._handle_buying_power_guard_state_transition(sig, result_guard)
                        self._mark_signal_buying_power_unavailable(sig, result_guard)
                        self._notify_buying_power_guard(sig, result_guard, level="error", event_type="alert", force=True)
                    else:
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

    def _fast_order_accept_enabled(self) -> bool:
        return self._config_bool("ibkr_order_place_fast_accept_enabled", True)

    def _signal_ack_queue_maxsize(self) -> int:
        return max(1, int(self._config_float("ibkr_signal_ack_queue_maxsize", 1000.0)))

    def _signal_ack_retry_attempts(self) -> int:
        return max(1, int(self._config_float("ibkr_signal_ack_retry_attempts", 5.0)))

    def _signal_ack_retry_base_delay_sec(self) -> float:
        return max(0.1, self._config_float("ibkr_signal_ack_retry_base_delay_sec", 0.5))

    def _ensure_signal_ack_worker(self) -> queue.Queue:
        ack_queue = getattr(self, "_signal_ack_queue", None)
        if not isinstance(ack_queue, queue.Queue):
            ack_queue = queue.Queue(maxsize=self._signal_ack_queue_maxsize())
            setattr(self, "_signal_ack_queue", ack_queue)
        worker = getattr(self, "_signal_ack_worker_thread", None)
        if not worker or not worker.is_alive():
            worker = threading.Thread(
                target=self._signal_ack_worker_loop,
                daemon=True,
                name="signal-ack-worker",
            )
            setattr(self, "_signal_ack_worker_thread", worker)
            worker.start()
        return ack_queue

    def _submit_signal_ack_after_order_submission(self, sig: dict, result: dict) -> None:
        if not self._fast_order_accept_enabled() or not getattr(self, "_running", False):
            self._ack_signal_after_order_submission(sig, result)
            return
        ack_queue = self._ensure_signal_ack_worker()
        try:
            ack_queue.put_nowait(
                {
                    "sig": copy.deepcopy(sig),
                    "result": copy.deepcopy(result),
                    "attempt": 1,
                    "queued_at": time.time(),
                }
            )
        except queue.Full:
            _service_mod().logger.error(
                "Signal ack queue full; falling back to synchronous ack: signal_id=%s",
                sig.get("signal_id"),
            )
            self._ack_signal_after_order_submission(sig, result)

    def _signal_ack_worker_loop(self) -> None:
        service_mod = _service_mod()
        ack_queue = getattr(self, "_signal_ack_queue", None)
        if not isinstance(ack_queue, queue.Queue):
            return
        while getattr(self, "_running", False) or not ack_queue.empty():
            try:
                item = ack_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_signal_ack_queue_item(item)
            finally:
                ack_queue.task_done()
        service_mod.logger.info("Signal ack worker stopped")

    def _run_signal_ack_queue_item(self, item: dict) -> None:
        service_mod = _service_mod()
        sig = item.get("sig") if isinstance(item, dict) else {}
        result = item.get("result") if isinstance(item, dict) else {}
        attempt = max(1, int((item or {}).get("attempt") or 1))
        signal_id = str((sig or {}).get("signal_id") or "").strip()
        try:
            self._ack_signal_after_order_submission(sig if isinstance(sig, dict) else {}, result if isinstance(result, dict) else {})
            return
        except Exception as exc:
            max_attempts = self._signal_ack_retry_attempts()
            if attempt >= max_attempts:
                service_mod.logger.error(
                    "Signal ack failed after retries: signal_id=%s attempts=%s error=%s",
                    signal_id,
                    attempt,
                    exc,
                )
                return
            delay = min(30.0, self._signal_ack_retry_base_delay_sec() * (2 ** (attempt - 1)))
            service_mod.logger.warning(
                "Signal ack retry scheduled: signal_id=%s attempt=%s/%s delay=%.1fs error=%s",
                signal_id,
                attempt + 1,
                max_attempts,
                delay,
                exc,
            )
            time.sleep(delay)
            ack_queue = getattr(self, "_signal_ack_queue", None)
            if isinstance(ack_queue, queue.Queue):
                try:
                    retry_item = dict(item or {})
                    retry_item["attempt"] = attempt + 1
                    ack_queue.put_nowait(retry_item)
                except queue.Full:
                    service_mod.logger.error(
                        "Signal ack queue full during retry: signal_id=%s attempt=%s",
                        signal_id,
                        attempt + 1,
                    )

    def signal_ack_queue_status(self) -> dict:
        ack_queue = getattr(self, "_signal_ack_queue", None)
        worker = getattr(self, "_signal_ack_worker_thread", None)
        return {
            "enabled": self._fast_order_accept_enabled(),
            "depth": ack_queue.qsize() if isinstance(ack_queue, queue.Queue) else 0,
            "worker_alive": bool(worker and worker.is_alive()),
        }

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

    @staticmethod
    def _structural_price_plan(sig: dict) -> str:
        extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        for source in (sig, extra, raw):
            plan = str((source or {}).get("entry_price_plan") or "").strip().lower()
            intent = str((source or {}).get("entry_limit_intent") or "").strip().lower()
            mode = str((source or {}).get("entry_anchor_mode") or "").strip().lower()
            if plan == "structural_anchor_limit" or intent == "structural_anchor" or mode == "setup_structural":
                return "structural_anchor_limit"
        return ""

    @classmethod
    def _is_structural_anchor_entry(cls, sig: dict) -> bool:
        return cls._structural_price_plan(sig) == "structural_anchor_limit"

    @classmethod
    def _planned_entry_price(cls, sig: dict) -> float:
        extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        for source in (sig, extra, raw):
            for key in ("planned_entry_price", "submitted_entry_limit_price", "entry"):
                value = cls._safe_float((source or {}).get(key), 0.0)
                if value > 0:
                    return value
        return 0.0

    @classmethod
    def _reference_entry_price(cls, sig: dict, default: float = 0.0) -> float:
        extra = sig.get("extra") if isinstance(sig.get("extra"), dict) else {}
        raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
        for source in (sig, extra, raw):
            for key in ("reference_entry", "signal_reference_price", "tv_reference_entry", "original_entry"):
                value = cls._safe_float((source or {}).get(key), 0.0)
                if value > 0:
                    return value
        return default

    @staticmethod
    def _price_worse_than_planned(direction: str, candidate_price: float, planned_price: float) -> bool:
        if candidate_price <= 0 or planned_price <= 0:
            return False
        normalized_direction = str(direction or "").strip().lower()
        return candidate_price > planned_price if normalized_direction == "long" else candidate_price < planned_price

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

    @staticmethod
    def _truthy_extra_value(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        return text in {"true", "1", "yes", "y", "on", "enabled"}

    def _signal_has_bounded_limit_cap(self, sig: dict, extra: dict | None = None) -> bool:
        data = sig if isinstance(sig, dict) else {}
        extra = dict(extra if isinstance(extra, dict) else self._signal_extra(data))
        if self._truthy_extra_value(extra.get("submitted_limit_cap_applied")):
            return True
        if self._truthy_extra_value(data.get("submitted_limit_cap_applied")):
            return True
        intent = str(extra.get("entry_limit_intent") or data.get("entry_limit_intent") or "").strip().lower()
        plan = str(extra.get("entry_price_plan") or data.get("entry_price_plan") or "").strip().lower()
        if intent in {"bounded_marketable", "bounded_limit", "limit_cap", "marketable_limit_cap"}:
            return True
        if plan in {"tv_direct_bounded_limit", "bounded_marketable_limit", "limit_cap"}:
            return True
        for source in (extra, data):
            if not isinstance(source, dict):
                continue
            for key in ("submitted_limit_cap_bps", "tv_entry_limit_cap_bps", "entry_limit_cap_bps", "limit_cap_bps"):
                if source.get(key) not in (None, ""):
                    return self._safe_float(source.get(key), 0.0) > 0
        return False

    def _prepare_tv_direct_entry_signal(self, sig: dict) -> tuple[bool, dict, str]:
        extra = self._signal_extra(sig)
        structural_anchor_entry = self._is_structural_anchor_entry(sig)
        signal_entry = self._safe_float(sig.get("entry"), 0.0)
        planned_entry = self._planned_entry_price(sig) if structural_anchor_entry else 0.0
        entry = planned_entry if structural_anchor_entry and planned_entry > 0 else signal_entry
        reference_entry = self._reference_entry_price(sig, entry) if structural_anchor_entry else entry
        reference_stop = self._safe_float(sig.get("stop_loss"), 0.0)
        reference_target = self._safe_float(sig.get("take_profit"), 0.0)
        direction = str(sig.get("direction") or "").strip().lower()
        if entry <= 0 or reference_entry <= 0 or reference_stop <= 0 or reference_target <= 0 or direction not in {"long", "short"}:
            return False, sig, "invalid_prices"

        default_max_age_s = max(1.0, self._config_float("tv_entry_freshness_sec", 120.0))
        limit_max_age_s = max(
            default_max_age_s,
            self._config_float("tv_entry_limit_freshness_sec", 240.0),
        )
        bounded_limit_freshness = not structural_anchor_entry
        max_age_s = limit_max_age_s if bounded_limit_freshness else default_max_age_s
        clock_skew_s = max(5.0, self._config_float("tv_entry_clock_skew_sec", 60.0))
        signal_dt, signal_time_source, raw_signal_time = self._tv_signal_timestamp(sig)
        now_utc = datetime.now(timezone.utc)
        if signal_dt is None:
            sig["extra"] = {
                **extra,
                "tv_direct_entry": True,
                "status_reason": "stale_signal",
                "signal_freshness_source": "missing",
                "signal_freshness_max_age_s": max_age_s,
                "signal_freshness_default_max_age_s": default_max_age_s,
                "signal_freshness_limit_max_age_s": limit_max_age_s,
                "signal_freshness_policy": "bounded_limit_cap" if bounded_limit_freshness else "default",
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
            "signal_freshness_default_max_age_s": default_max_age_s,
            "signal_freshness_limit_max_age_s": limit_max_age_s,
            "signal_freshness_policy": "bounded_limit_cap" if bounded_limit_freshness else "default",
            "signal_clock_skew_threshold_s": clock_skew_s,
        }
        if signal_age_s < -clock_skew_s:
            sig["extra"] = {
                **extra,
                **freshness_fields,
                "tv_direct_entry": True,
                "status_reason": "signal_clock_skew",
                "tv_direct_rejected_reason": "signal_clock_skew",
            }
            return False, sig, "signal_clock_skew"
        if signal_age_s > max_age_s:
            sig["extra"] = {
                **extra,
                **freshness_fields,
                "tv_direct_entry": True,
                "status_reason": "stale_signal",
                "tv_direct_rejected_reason": "stale_signal",
            }
            return False, sig, "stale_signal"

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
        if structural_anchor_entry:
            submitted_limit = self._round_price(entry)
            entry_limit_intent = "structural_anchor"
            entry_price_plan = "structural_anchor_limit"
            submitted_limit_cap_applied = False
            validation_policy = "freshness_then_structural_anchor_guard"
        else:
            raw_limit = (
                reference_entry * (1.0 - cap_bps / 10000.0)
                if direction == "short"
                else reference_entry * (1.0 + cap_bps / 10000.0)
            )
            submitted_limit = self._round_tv_entry_limit(raw_limit, direction)
            entry_limit_intent = "bounded_marketable"
            entry_price_plan = "tv_direct_bounded_limit"
            submitted_limit_cap_applied = True
            validation_policy = "freshness_only_then_hard_safety"
        adaptive_enabled = self._config_bool("tv_entry_adaptive_enabled", True)
        adaptive_priority = self._config_text("tv_entry_adaptive_priority", "Normal").strip() or "Normal"
        adjusted_sig = copy.deepcopy(sig)
        adjusted_sig["entry"] = submitted_limit
        trade_group_id = str(
            extra.get("trade_group_id")
            or extra.get("bracket_group")
            or extra.get("leg_trade_group_id")
            or sig.get("trade_group_id")
            or sig.get("bracket_group")
            or sig.get("signal_id")
            or ""
        ).strip()
        adjusted_extra = {
            **extra,
            **freshness_fields,
            "tv_direct_entry": True,
            "tv_primary_direct_execution": True,
            "signal_reference_price": reference_entry,
            "tv_reference_entry": reference_entry,
            "tv_reference_stop_loss": reference_stop,
            "tv_reference_take_profit": reference_target,
            "reference_entry": reference_entry,
            "planned_entry_price": submitted_limit,
            "reference_stop_loss": reference_stop,
            "reference_take_profit": reference_target,
            "reference_protection_only": True,
            "final_protection_from_fill": True,
            "submitted_entry_limit_price": submitted_limit,
            "submitted_limit_cap_price": submitted_limit,
            "submitted_limit_cap_bps": cap_bps,
            "submitted_limit_cap_applied": submitted_limit_cap_applied,
            "tv_entry_limit_cap_bps": cap_bps,
            "tv_entry_adaptive_enabled": adaptive_enabled,
            "tv_entry_adaptive_priority": adaptive_priority,
            "adaptive_priority": adaptive_priority if adaptive_enabled else "",
            "entry_order_type": "LMT",
            "entry_limit_intent": entry_limit_intent,
            "entry_price_plan": entry_price_plan,
            "entry_repriced": submitted_limit != self._round_price(signal_entry),
            "tv_direct_validation_policy": validation_policy,
            "tv_direct_signal_processor_validation_skipped": True,
            "original_entry": signal_entry,
            "original_stop_loss": reference_stop,
            "original_take_profit": reference_target,
            "status_reason": "tv_direct_ready",
        }
        adjusted_sig["extra"] = normalize_independent_scale_plan_extra(
            adjusted_extra,
            signal_id=str(sig.get("signal_id") or ""),
            trade_group_id=trade_group_id,
            symbol=str(sig.get("symbol") or ""),
            direction=direction,
        )
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

    @staticmethod
    def _tv_entry_safe_extra(value) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _tv_entry_escape_filter_value(value) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _tv_entry_normalized_status(value) -> str:
        return str(value or "").strip().lower().replace("_", "").replace(" ", "")

    @staticmethod
    def _tv_entry_dedupe(values) -> list[str]:
        seen = set()
        result = []
        for value in values or []:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @classmethod
    def _tv_entry_order_extra(cls, order: dict) -> dict:
        return cls._tv_entry_safe_extra((order or {}).get("extra"))

    @classmethod
    def _tv_entry_order_status(cls, order: dict) -> str:
        return str((order or {}).get("status") or (order or {}).get("order_status") or "").strip()

    @classmethod
    def _tv_entry_order_broker_id(cls, order: dict) -> str:
        extra = cls._tv_entry_order_extra(order or {})
        return str(
            (order or {}).get("broker_order_id")
            or (order or {}).get("order_id")
            or (order or {}).get("orderId")
            or (order or {}).get("id")
            or extra.get("broker_order_id")
            or ""
        ).strip()

    @classmethod
    def _tv_entry_order_unique_id(cls, order: dict) -> str:
        extra = cls._tv_entry_order_extra(order or {})
        return str(
            (order or {}).get("unique_id")
            or (order or {}).get("cOID")
            or (order or {}).get("coid")
            or (order or {}).get("orderRef")
            or (order or {}).get("order_ref")
            or extra.get("unique_id")
            or extra.get("cOID")
            or extra.get("coid")
            or extra.get("orderRef")
            or extra.get("order_ref")
            or ""
        ).strip()

    @classmethod
    def _tv_entry_order_group(cls, order: dict) -> str:
        extra = cls._tv_entry_order_extra(order or {})
        for key in ("trade_group_id", "bracket_group", "oca_group"):
            text = str((order or {}).get(key) or extra.get(key) or "").strip()
            if text:
                return text
        for ref in (
            cls._tv_entry_order_unique_id(order or {}),
            str((order or {}).get("orderRef") or (order or {}).get("order_ref") or "").strip(),
        ):
            lowered = str(ref or "").strip().lower()
            for prefix in ("entry_", "tp_", "sl_"):
                if lowered.startswith(prefix):
                    return str(ref or "").strip()[len(prefix) :]
        return ""

    @classmethod
    def _tv_entry_order_role(cls, order: dict) -> str:
        extra = cls._tv_entry_order_extra(order or {})
        raw_role = str(
            (order or {}).get("role")
            or extra.get("role")
            or (order or {}).get("order_type")
            or (order or {}).get("orderType")
            or extra.get("order_type")
            or ""
        ).strip().lower()
        normalized = raw_role.replace("_", "").replace(" ", "")
        if normalized in {"entry", "parent"}:
            return "entry"
        if normalized in {"tp", "takeprofit", "profittarget", "target"}:
            return "take_profit"
        if normalized in {"sl", "stop", "stoploss"}:
            return "stop_loss"

        unique_id = cls._tv_entry_order_unique_id(order or {}).lower()
        if unique_id.startswith("entry_"):
            return "entry"
        if unique_id.startswith("tp_"):
            return "take_profit"
        if unique_id.startswith("sl_"):
            return "stop_loss"

        parent_id = str((order or {}).get("parentId") or (order or {}).get("parent_id") or "").strip()
        order_type = str((order or {}).get("orderType") or (order or {}).get("order_type") or "").strip().upper()
        if parent_id:
            return "stop_loss" if order_type in {"STP", "STOP", "STOPLOSS"} else "take_profit"
        return ""

    @classmethod
    def _tv_entry_order_is_filled(cls, order: dict) -> bool:
        status = cls._tv_entry_normalized_status(cls._tv_entry_order_status(order or {}))
        if status in cls.TV_ENTRY_FILLED_ORDER_STATUSES:
            return True
        filled_qty = cls._safe_float(
            (order or {}).get("filledQuantity")
            or (order or {}).get("filled_qty")
            or (order or {}).get("filled")
            or (order or {}).get("executedQuantity"),
            0.0,
        )
        quantity = cls._safe_float(
            (order or {}).get("totalSize")
            if (order or {}).get("totalSize") not in (None, "")
            else (order or {}).get("quantity"),
            0.0,
        )
        remaining = cls._safe_float(
            (order or {}).get("remainingQuantity")
            or (order or {}).get("remaining_qty")
            or (order or {}).get("remaining"),
            0.0,
        )
        return bool(quantity > 0 and filled_qty >= quantity and remaining <= 0.0001)

    @classmethod
    def _tv_entry_order_is_working(cls, order: dict) -> bool:
        if cls._tv_entry_order_is_filled(order or {}):
            return False
        status = cls._tv_entry_normalized_status(cls._tv_entry_order_status(order or {}))
        return bool(status and status in cls.TV_ENTRY_WORKING_ORDER_STATUSES)

    @classmethod
    def _tv_entry_order_price(cls, order: dict, *keys: str) -> float:
        extra = cls._tv_entry_order_extra(order or {})
        for key in keys:
            value = cls._safe_float((order or {}).get(key), 0.0)
            if value > 0:
                return value
            value = cls._safe_float(extra.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _tv_entry_reconcile_identifiers(self, sig: dict, record: dict | None, existing_extra: dict | None) -> dict:
        service_mod = _service_mod()
        signal_id = str((sig or {}).get("signal_id") or (record or {}).get("signal_id") or "").strip()
        broker_mode = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
        raw = (sig or {}).get("raw") if isinstance((sig or {}).get("raw"), dict) else {}
        raw_extra = self._tv_entry_safe_extra(raw.get("extra"))
        signal_extra = self._signal_extra(sig or {})
        existing_extra = existing_extra if isinstance(existing_extra, dict) else {}
        record_extra = self._tv_entry_safe_extra((record or {}).get("extra"))
        sources = [sig or {}, raw, record or {}, signal_extra, raw_extra, existing_extra, record_extra]

        for extra in (signal_extra, existing_extra, record_extra):
            execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
            broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
            if isinstance(broker_execution, dict):
                sources.append(broker_execution)

        groups = []
        order_ids = []
        unique_ids = []

        def add_text(target: list, value) -> None:
            text = str(value or "").strip()
            if text:
                target.append(text)

        def add_values(target: list, value) -> None:
            if isinstance(value, str):
                for part in value.split(","):
                    add_text(target, part)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    add_text(target, item)
            else:
                add_text(target, value)

        if signal_id:
            groups.append(signal_id)
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in ("trade_group_id", "bracket_group", "submit_failed_bracket_group", "oca_group"):
                add_text(groups, source.get(key))
            for key in (
                "order_ids",
                "submitted_order_ids",
                "broker_order_ids",
                "submit_failed_order_ids",
                "missing_order_ids",
            ):
                add_values(order_ids, source.get(key))
            for key in (
                "entry_order_id",
                "tp_order_id",
                "sl_order_id",
                "take_profit_order_id",
                "stop_loss_order_id",
                "broker_order_id",
                "entry_broker_order_id",
                "tp_broker_order_id",
                "sl_broker_order_id",
                "duplicate_broker_order_id",
            ):
                add_text(order_ids, source.get(key))
            for key in (
                "entry_coid",
                "tp_coid",
                "sl_coid",
                "entry_unique_id",
                "tp_unique_id",
                "sl_unique_id",
                "entry_order_unique_id",
                "tp_order_unique_id",
                "sl_order_unique_id",
                "parent_order_unique_id",
            ):
                add_text(unique_ids, source.get(key))

        groups = self._tv_entry_dedupe(groups)
        for group in groups:
            unique_ids.extend([f"entry_{group}", f"tp_{group}", f"sl_{group}"])
        return {
            "signal_id": signal_id,
            "symbol": str((sig or {}).get("symbol") or (record or {}).get("symbol") or "").strip().upper(),
            "direction": str((sig or {}).get("direction") or (record or {}).get("direction") or "").strip().lower(),
            "broker_environment": broker_mode,
            "data_environment": str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live",
            "groups": self._tv_entry_dedupe(groups),
            "order_ids": self._tv_entry_dedupe(order_ids),
            "unique_ids": self._tv_entry_dedupe(unique_ids),
        }

    def _tv_entry_identifiers_with_order_rows(self, identifiers: dict, rows: list[dict]) -> dict:
        merged = dict(identifiers or {})
        groups = list(merged.get("groups") or [])
        order_ids = list(merged.get("order_ids") or [])
        unique_ids = list(merged.get("unique_ids") or [])
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            groups.append(self._tv_entry_order_group(row))
            order_ids.append(self._tv_entry_order_broker_id(row))
            unique_ids.append(self._tv_entry_order_unique_id(row))
            extra = self._tv_entry_order_extra(row)
            for key in ("trade_group_id", "bracket_group", "oca_group"):
                groups.append(row.get(key) or extra.get(key))
            for key in ("entry_order_unique_id", "parent_order_unique_id", "sibling_order_unique_id"):
                unique_ids.append(row.get(key) or extra.get(key))
        merged["groups"] = self._tv_entry_dedupe(groups)
        merged["order_ids"] = self._tv_entry_dedupe(order_ids)
        merged["unique_ids"] = self._tv_entry_dedupe(unique_ids)
        return merged

    def _fetch_tv_entry_pb_order_rows(self, identifiers: dict) -> list[dict]:
        pb = getattr(self, "pb", None)
        getter = getattr(pb, "get_records", None)
        if not callable(getter):
            return []

        environment = self._tv_entry_escape_filter_value((identifiers or {}).get("broker_environment"))
        filters = []
        signal_id = str((identifiers or {}).get("signal_id") or "").strip()
        if signal_id:
            filters.append(f'signal_id = "{self._tv_entry_escape_filter_value(signal_id)}"')
        for group in identifiers.get("groups") or []:
            safe_group = self._tv_entry_escape_filter_value(group)
            filters.append(f'trade_group_id = "{safe_group}"')
            filters.append(f'bracket_group = "{safe_group}"')
        for order_id in identifiers.get("order_ids") or []:
            safe_order_id = self._tv_entry_escape_filter_value(order_id)
            filters.append(f'order_id = "{safe_order_id}"')
            filters.append(f'broker_order_id = "{safe_order_id}"')
        for unique_id in identifiers.get("unique_ids") or []:
            safe_unique_id = self._tv_entry_escape_filter_value(unique_id)
            filters.append(f'unique_id = "{safe_unique_id}"')
            filters.append(f'entry_order_unique_id = "{safe_unique_id}"')

        rows = []
        seen = set()
        for filter_expr in self._tv_entry_dedupe(filters):
            query = f'{filter_expr} && environment = "{environment}"'
            try:
                candidates = getter("orders", filter=query, sort="-updated", per_page=100) or []
            except Exception as exc:
                _service_mod().logger.debug("TV direct reconcile PB order query failed: filter=%s error=%s", query, exc)
                continue
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("unique_id") or row.get("order_id") or len(seen))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dict(row))
        return rows

    def _tv_entry_order_matches_identifiers(self, order: dict, identifiers: dict) -> bool:
        order_ids = {str(item or "").strip() for item in (identifiers or {}).get("order_ids") or [] if str(item or "").strip()}
        unique_ids = {str(item or "").strip() for item in (identifiers or {}).get("unique_ids") or [] if str(item or "").strip()}
        groups = {str(item or "").strip() for item in (identifiers or {}).get("groups") or [] if str(item or "").strip()}
        signal_id = str((identifiers or {}).get("signal_id") or "").strip()
        extra = self._tv_entry_order_extra(order or {})

        if self._tv_entry_order_broker_id(order or {}) in order_ids:
            return True
        unique_id = self._tv_entry_order_unique_id(order or {})
        if unique_id in unique_ids:
            return True
        if signal_id and str((order or {}).get("signal_id") or extra.get("signal_id") or "").strip() == signal_id:
            return True
        group = self._tv_entry_order_group(order or {})
        return bool(group and group in groups)

    def _fetch_tv_entry_live_order_rows(self, identifiers: dict) -> dict:
        tracker = getattr(self, "order_tracker", None)
        broker = getattr(self, "broker", None)
        raw_orders = []
        coverage = {}
        diagnostics = {}
        performed = False
        error = ""

        complete_getter = getattr(tracker, "get_complete_live_open_orders", None)
        if callable(complete_getter):
            performed = True
            try:
                payload = complete_getter(
                    pb_seed_ids=list((identifiers or {}).get("order_ids") or []),
                    retries=1,
                    retry_delay=0.1,
                    force=True,
                )
            except TypeError:
                try:
                    payload = complete_getter(pb_seed_ids=list((identifiers or {}).get("order_ids") or []))
                except Exception as exc:
                    payload = {}
                    error = str(exc)
            except Exception as exc:
                payload = {}
                error = str(exc)
            if isinstance(payload, dict):
                raw_orders.extend([dict(item) for item in (payload.get("orders") or []) if isinstance(item, dict)])
                coverage = dict(payload.get("coverage") or {})
                diagnostics = dict(payload.get("diagnostics") or {})

        live_getter = getattr(tracker, "get_live_orders", None)
        if callable(live_getter):
            performed = True
            try:
                live_orders = live_getter(force=True, include_all=True)
            except TypeError:
                try:
                    live_orders = live_getter(force=True)
                except TypeError:
                    try:
                        live_orders = live_getter()
                    except Exception as exc:
                        live_orders = []
                        error = error or str(exc)
                except Exception as exc:
                    live_orders = []
                    error = error or str(exc)
            except Exception as exc:
                live_orders = []
                error = error or str(exc)
            raw_orders.extend([dict(item) for item in (live_orders or []) if isinstance(item, dict)])
        elif callable(getattr(broker, "list_open_orders", None)):
            performed = True
            try:
                live_orders = broker.list_open_orders(include_all=True, force=True)
            except TypeError:
                try:
                    live_orders = broker.list_open_orders(include_all=True)
                except TypeError:
                    try:
                        live_orders = broker.list_open_orders()
                    except Exception as exc:
                        live_orders = []
                        error = error or str(exc)
                except Exception as exc:
                    live_orders = []
                    error = error or str(exc)
            except Exception as exc:
                live_orders = []
                error = error or str(exc)
            raw_orders.extend([dict(item) for item in (live_orders or []) if isinstance(item, dict)])

        matched = []
        seen = set()
        for row in raw_orders:
            if not self._tv_entry_order_matches_identifiers(row, identifiers):
                continue
            key = self._tv_entry_order_broker_id(row) or self._tv_entry_order_unique_id(row) or str(len(seen))
            if key in seen:
                continue
            seen.add(key)
            matched.append(row)
        return {
            "orders": matched,
            "performed": performed,
            "coverage": coverage,
            "diagnostics": diagnostics,
            "error": error,
            "raw_count": len(raw_orders),
        }

    def _tv_entry_row_symbol_matches(self, sig: dict, row: dict) -> bool:
        symbol = str((sig or {}).get("symbol") or "").strip().upper()
        if not symbol:
            return True
        extra = self._tv_entry_order_extra(row or {})
        row_symbol = str((row or {}).get("symbol") or (row or {}).get("ticker") or extra.get("symbol") or "").strip().upper()
        return not row_symbol or row_symbol == symbol

    def _tv_entry_role_summary(self, rows: list[dict]) -> dict:
        role_rows = {"entry": [], "take_profit": [], "stop_loss": []}
        role_order_ids = {"entry": [], "take_profit": [], "stop_loss": []}
        role_unique_ids = {"entry": [], "take_profit": [], "stop_loss": []}
        role_statuses = {"entry": [], "take_profit": [], "stop_loss": []}
        for row in rows or []:
            role = self._tv_entry_order_role(row)
            if role not in role_rows:
                continue
            role_rows[role].append(row)
            role_order_ids[role].append(self._tv_entry_order_broker_id(row))
            role_unique_ids[role].append(self._tv_entry_order_unique_id(row))
            role_statuses[role].append(self._tv_entry_order_status(row))
        return {
            "role_rows": role_rows,
            "role_order_ids": {key: self._tv_entry_dedupe(value) for key, value in role_order_ids.items()},
            "role_unique_ids": {key: self._tv_entry_dedupe(value) for key, value in role_unique_ids.items()},
            "role_statuses": {key: self._tv_entry_dedupe(value) for key, value in role_statuses.items()},
        }

    @staticmethod
    def _tv_entry_can_use_pb_only_evidence(pb_rows: list[dict], live_payload: dict) -> bool:
        if not pb_rows:
            return False
        if not bool((live_payload or {}).get("performed")):
            return True
        if str((live_payload or {}).get("error") or "").strip():
            return True
        coverage = (live_payload or {}).get("coverage") if isinstance((live_payload or {}).get("coverage"), dict) else {}
        return bool(coverage.get("unresolved_order_ids") or coverage.get("unresolved_seed_count"))

    def _classify_tv_entry_reconcile_evidence(
        self,
        sig: dict,
        identifiers: dict,
        pb_rows: list[dict],
        live_payload: dict,
    ) -> dict:
        live_rows = [row for row in (live_payload or {}).get("orders") or [] if isinstance(row, dict)]
        usable_rows = [
            row
            for row in [*pb_rows, *live_rows]
            if isinstance(row, dict) and self._tv_entry_order_matches_identifiers(row, identifiers) and self._tv_entry_row_symbol_matches(sig, row)
        ]
        if not usable_rows:
            return {"handled": False, "source": "none"}

        live_order_ids = self._tv_entry_dedupe(self._tv_entry_order_broker_id(row) for row in live_rows)
        pb_order_ids = self._tv_entry_dedupe(self._tv_entry_order_broker_id(row) for row in pb_rows)
        has_live_evidence = bool(live_order_ids)
        if not has_live_evidence and not self._tv_entry_can_use_pb_only_evidence(pb_rows, live_payload):
            return {
                "handled": False,
                "source": "pb_stale_without_broker_evidence",
                "pb_order_ids": pb_order_ids,
                "live_order_ids": live_order_ids,
            }

        summary = self._tv_entry_role_summary(usable_rows)
        role_rows = summary["role_rows"]
        entry_filled = any(self._tv_entry_order_is_filled(row) for row in role_rows["entry"])
        entry_working = any(self._tv_entry_order_is_working(row) for row in role_rows["entry"])
        take_profit_working = any(self._tv_entry_order_is_working(row) for row in role_rows["take_profit"])
        stop_loss_working = any(self._tv_entry_order_is_working(row) for row in role_rows["stop_loss"])
        protection_complete = bool(take_profit_working and stop_loss_working)
        if not protection_complete:
            return {
                "handled": False,
                "source": "incomplete_bracket_evidence",
                "pb_order_ids": pb_order_ids,
                "live_order_ids": live_order_ids,
                **summary,
            }

        group = ""
        for row in usable_rows:
            group = self._tv_entry_order_group(row)
            if group:
                break
        if not group:
            groups = identifiers.get("groups") or []
            group = str(groups[0] or "") if groups else str((identifiers or {}).get("signal_id") or "")

        entry_fill_price = 0.0
        if entry_filled:
            for row in role_rows["entry"]:
                entry_fill_price = self._tv_entry_order_price(
                    row,
                    "avgPrice",
                    "avgFillPrice",
                    "fill_price",
                    "filled_price",
                    "lastFillPrice",
                    "price",
                    "limit_price",
                )
                if entry_fill_price > 0:
                    break
            return {
                "handled": True,
                "status": "filled_position",
                "pb_status": "protected_active",
                "note": "tv_direct_reconciled_filled_protected",
                "source": "broker_order" if has_live_evidence else "pb_order",
                "trade_group_id": group,
                "bracket_group": group,
                "protection_complete": True,
                "entry_fill_price": entry_fill_price,
                "pb_order_ids": pb_order_ids,
                "live_order_ids": live_order_ids,
                "broker_check_performed": bool((live_payload or {}).get("performed")),
                "broker_check_error": str((live_payload or {}).get("error") or ""),
                "broker_coverage": dict((live_payload or {}).get("coverage") or {}),
                **summary,
            }

        if entry_working:
            return {
                "handled": True,
                "status": "submitted_waiting_fill",
                "pb_status": "submitted_waiting_fill",
                "note": "submitted_waiting_fill",
                "source": "broker_order" if has_live_evidence else "pb_order",
                "trade_group_id": group,
                "bracket_group": group,
                "protection_complete": True,
                "entry_fill_price": 0.0,
                "pb_order_ids": pb_order_ids,
                "live_order_ids": live_order_ids,
                "broker_check_performed": bool((live_payload or {}).get("performed")),
                "broker_check_error": str((live_payload or {}).get("error") or ""),
                "broker_coverage": dict((live_payload or {}).get("coverage") or {}),
                **summary,
            }
        return {
            "handled": False,
            "source": "entry_not_open_or_filled",
            "pb_order_ids": pb_order_ids,
            "live_order_ids": live_order_ids,
            **summary,
        }

    def _tv_entry_reconcile_order_result(self, sig: dict, evidence: dict) -> dict:
        role_order_ids = evidence.get("role_order_ids") if isinstance(evidence.get("role_order_ids"), dict) else {}
        role_unique_ids = evidence.get("role_unique_ids") if isinstance(evidence.get("role_unique_ids"), dict) else {}
        group = str(evidence.get("trade_group_id") or evidence.get("bracket_group") or sig.get("signal_id") or "").strip()

        def first_role_value(payload: dict, role: str, fallback: str = "") -> str:
            values = payload.get(role) if isinstance(payload, dict) else []
            for value in values or []:
                text = str(value or "").strip()
                if text:
                    return text
            return fallback

        entry_coid = first_role_value(role_unique_ids, "entry", f"entry_{group}" if group else "")
        tp_coid = first_role_value(role_unique_ids, "take_profit", f"tp_{group}" if group else "")
        sl_coid = first_role_value(role_unique_ids, "stop_loss", f"sl_{group}" if group else "")
        order_ids = [
            first_role_value(role_order_ids, "entry"),
            first_role_value(role_order_ids, "take_profit"),
            first_role_value(role_order_ids, "stop_loss"),
        ]
        order_ids = [str(item or "").strip() for item in order_ids]
        return {
            "ok": True,
            "order_ids": order_ids,
            "bracket_group": group,
            "trade_group_id": group,
            "oca_group": "",
            "order_family_type": "bracket_oco",
            "quantity": int(sig.get("shares") or 0),
            "take_profit_quantity": int(sig.get("shares") or 0),
            "stop_loss_quantity": int(sig.get("shares") or 0),
            "entry_coid": entry_coid,
            "tp_coid": tp_coid,
            "sl_coid": sl_coid,
            "entry_price": sig.get("entry"),
            "take_profit_price": sig.get("take_profit"),
            "stop_loss_price": sig.get("stop_loss"),
            "protection_complete": bool(evidence.get("protection_complete")),
            "protection_incomplete": not bool(evidence.get("protection_complete")),
            "order_extra": {},
        }

    def _tv_entry_reconcile_extra(self, sig: dict, evidence: dict, result: dict) -> dict:
        status = str(evidence.get("status") or "").strip()
        note = str(evidence.get("note") or status or "tv_direct_reconciled").strip()
        order_ids = [str(item or "").strip() for item in (result or {}).get("order_ids") or [] if str(item or "").strip()]
        group = str((result or {}).get("trade_group_id") or (result or {}).get("bracket_group") or "").strip()
        role_statuses = evidence.get("role_statuses") if isinstance(evidence.get("role_statuses"), dict) else {}
        extra = {
            **self._signal_extra(sig),
            "status_reason": note,
            "execution_state": "tv_direct_reconciled",
            "tv_direct_reconciled": True,
            "tv_direct_reconcile_at": self._now_iso(),
            "tv_direct_reconcile_action": "skip_duplicate_submit",
            "tv_direct_reconcile_status": status,
            "tv_direct_reconcile_source": str(evidence.get("source") or ""),
            "tv_direct_reconcile_broker_checked": bool(evidence.get("broker_check_performed")),
            "tv_direct_reconcile_broker_error": str(evidence.get("broker_check_error") or ""),
            "tv_direct_reconcile_broker_coverage": dict(evidence.get("broker_coverage") or {}),
            "tv_direct_reconcile_pb_order_ids": list(evidence.get("pb_order_ids") or []),
            "tv_direct_reconcile_live_order_ids": list(evidence.get("live_order_ids") or []),
            "submitted_order_ids": order_ids,
            "trade_group_id": group,
            "bracket_group": group,
            "entry_order_id": order_ids[0] if len(order_ids) > 0 else "",
            "tp_order_id": order_ids[1] if len(order_ids) > 1 else "",
            "sl_order_id": order_ids[2] if len(order_ids) > 2 else "",
            "entry_coid": (result or {}).get("entry_coid") or "",
            "tp_coid": (result or {}).get("tp_coid") or "",
            "sl_coid": (result or {}).get("sl_coid") or "",
            "order_family_type": (result or {}).get("order_family_type") or "bracket_oco",
            "protection_complete": bool(evidence.get("protection_complete")),
            "protection_incomplete": not bool(evidence.get("protection_complete")),
            "missing_protection_roles": [],
            "protection_order_statuses": {
                "take_profit": list(role_statuses.get("take_profit") or []),
                "stop_loss": list(role_statuses.get("stop_loss") or []),
            },
            "protection_orders_checked": sum(len(value or []) for value in role_statuses.values()),
            "signal_lifecycle_status": status,
        }
        if status == "filled_position":
            entry_order_id = order_ids[0] if order_ids else ""
            fill_price = self._safe_float(evidence.get("entry_fill_price"), 0.0)
            extra.update(
                {
                    "entry_fill_detected_by": "tv_direct_pre_submit_reconcile",
                    "entry_fill_broker_order_id": entry_order_id,
                    "entry_fill_price": fill_price,
                    "executed_price": fill_price,
                    "entry_fill_status": "Filled",
                    "final_stop_loss": sig.get("stop_loss"),
                    "final_take_profit": sig.get("take_profit"),
                }
            )
        return extra

    def _mark_signal_tv_direct_reconciled(self, sig: dict, evidence: dict, result: dict) -> None:
        if not self.pb:
            return
        service_mod = _service_mod()
        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            if not record:
                return
            broker_status = str(evidence.get("status") or "submitted_waiting_fill").strip() or "submitted_waiting_fill"
            pb_status = str(evidence.get("pb_status") or broker_status).strip() or broker_status
            note = str(evidence.get("note") or broker_status).strip() or broker_status
            lifecycle_extra = self._tv_entry_reconcile_extra(sig, evidence, result)
            patch = self._signal_broker_patch(pb_status, note, existing_extra, lifecycle_extra)
            broker_mode = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
            data_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live"
            execution_by_mode = patch["extra"].get("execution_by_mode")
            if not isinstance(execution_by_mode, dict):
                execution_by_mode = {}
            broker_execution = execution_by_mode.get(broker_mode)
            if not isinstance(broker_execution, dict):
                broker_execution = {}
            execution_by_mode[broker_mode] = {
                **broker_execution,
                "status": broker_status,
                "note": note,
                "status_reason": note,
                "data_environment": data_environment,
                "source": "ibkr_compute_tv_direct_reconcile",
                "trade_group_id": result.get("trade_group_id") or "",
                "bracket_group": result.get("bracket_group") or "",
                "order_ids": [str(item or "").strip() for item in (result.get("order_ids") or []) if str(item or "").strip()],
                "entry_order_id": lifecycle_extra.get("entry_order_id") or "",
                "tp_order_id": lifecycle_extra.get("tp_order_id") or "",
                "sl_order_id": lifecycle_extra.get("sl_order_id") or "",
                "entry_coid": result.get("entry_coid") or "",
                "tp_coid": result.get("tp_coid") or "",
                "sl_coid": result.get("sl_coid") or "",
                "protection_complete": bool(evidence.get("protection_complete")),
                "updated_at": self._now_iso(),
            }
            patch["extra"]["execution_by_mode"] = execution_by_mode
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark TV direct signal reconciled: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def _apply_tv_direct_entry_reconcile(self, sig: dict, evidence: dict) -> None:
        service_mod = _service_mod()
        result = self._tv_entry_reconcile_order_result(sig, evidence)
        reconcile_extra = self._tv_entry_reconcile_extra(sig, evidence, result)
        result["order_extra"] = dict(reconcile_extra)
        sig_for_ack = copy.deepcopy(sig)
        sig_for_ack["extra"] = reconcile_extra
        if str(evidence.get("status") or "") == "submitted_waiting_fill":
            try:
                self._ack_signal_after_order_submission(sig_for_ack, result)
            except Exception as ack_err:
                service_mod.logger.warning(
                    "TV direct reconcile order ack failed; applying lifecycle patch only: signal_id=%s error=%s",
                    sig.get("signal_id"),
                    ack_err,
                )
            try:
                self._register_submitted_order_result(result, sig_for_ack, str(sig.get("symbol") or ""))
            except Exception as track_err:
                service_mod.logger.debug("TV direct reconcile order tracker register failed: %s", track_err)
            try:
                self.signal_processor.register_pending_entry(
                    sig.get("symbol"),
                    {
                        "direction": sig.get("direction"),
                        "bracket_group": result.get("bracket_group"),
                        "signal_id": sig.get("signal_id"),
                        "reconciled_existing_order": True,
                    },
                )
            except Exception:
                pass
        self._mark_signal_tv_direct_reconciled(sig_for_ack, evidence, result)

    def _reconcile_tv_direct_entry_before_submit(self, sig: dict) -> dict:
        service_mod = _service_mod()
        signal_id = str((sig or {}).get("signal_id") or "").strip()
        if not signal_id or not getattr(self, "pb", None):
            return {"handled": False, "source": "unavailable"}
        try:
            record, existing_extra = self._load_signal_record_and_extra(sig)
            identifiers = self._tv_entry_reconcile_identifiers(sig, record, existing_extra)
            pb_rows = self._fetch_tv_entry_pb_order_rows(identifiers)
            identifiers = self._tv_entry_identifiers_with_order_rows(identifiers, pb_rows)
            live_payload = self._fetch_tv_entry_live_order_rows(identifiers)
            live_rows = list(live_payload.get("orders") or [])
            if live_rows and callable(getattr(getattr(self, "order_tracker", None), "sync_live_orders_snapshot", None)):
                try:
                    self.order_tracker.sync_live_orders_snapshot(live_rows)
                except Exception as sync_err:
                    service_mod.logger.debug("TV direct reconcile live-order sync failed: %s", sync_err)
            evidence = self._classify_tv_entry_reconcile_evidence(sig, identifiers, pb_rows, live_payload)
            if not evidence.get("handled"):
                return evidence
            self._apply_tv_direct_entry_reconcile(sig, evidence)
            return evidence
        except Exception as exc:
            service_mod.logger.warning(
                "TV direct pre-submit reconcile failed; continuing normal submission flow: signal_id=%s error=%s",
                signal_id,
                exc,
            )
            return {"handled": False, "source": "error", "error": str(exc)}

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
                try:
                    subscriber(normalized_conid, symbol=normalized_symbol, kind="entry_pre_submit")
                except TypeError:
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
        structural_anchor_entry = self._is_structural_anchor_entry(sig)
        planned_entry = self._planned_entry_price(sig) if structural_anchor_entry else 0.0
        if structural_anchor_entry:
            diagnostics.update(
                {
                    "structural_anchor_guard": True,
                    "planned_entry_price": planned_entry or entry,
                    "entry_limit_intent": "structural_anchor",
                    "entry_price_plan": "structural_anchor_limit",
                }
            )

        if live_environment and not fresh_quote:
            if self._signal_has_bounded_limit_cap(sig, extra):
                status_reason = "entry_guard_quote_unavailable_allowed_by_limit_cap"
                sig["extra"] = {
                    **extra,
                    **diagnostics,
                    "quote_guard_status": "unavailable_allowed_by_limit_cap",
                    "quote_guard_missing_quote_allowed": True,
                    "quote_guard_missing_quote_allow_reason": "bounded_limit_cap",
                    "status_reason": status_reason,
                }
                return True, sig, ""
            status_reason = guard_reason or "entry_guard_no_fresh_quote"
            sig["extra"] = {**extra, **diagnostics, "status_reason": status_reason}
            return False, sig, status_reason

        if not fresh_quote:
            sig["extra"] = {**extra, **diagnostics}
            return True, sig, ""

        if structural_anchor_entry:
            planned_entry = planned_entry or entry
            executable_price = self._quote_price(quote, "ask" if direction == "long" else "bid")
            executable_source = "ask" if direction == "long" else "bid"
            if executable_price <= 0:
                executable_price = reference_price
                executable_source = reference_source
            diagnostics.update(
                {
                    "structural_anchor_reference_price": round(executable_price, 4) if executable_price > 0 else None,
                    "structural_anchor_reference_source": executable_source,
                    "structural_anchor_price_reached": not self._price_worse_than_planned(
                        direction,
                        executable_price,
                        planned_entry,
                    ),
                    "reprice_source": "structural_anchor_guard",
                }
            )
            if direction == "long" and executable_price > 0 and executable_price <= stop_loss:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_stop_already_crossed"}
                return False, sig, "entry_guard_stop_already_crossed"
            if direction == "short" and executable_price > 0 and executable_price >= stop_loss:
                sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_stop_already_crossed"}
                return False, sig, "entry_guard_stop_already_crossed"
            if self._price_worse_than_planned(direction, executable_price, planned_entry):
                status_reason = "entry_structural_price_not_reached"
                sig["extra"] = {**extra, **diagnostics, "status_reason": status_reason}
                return False, sig, status_reason
            adjusted_sig = copy.deepcopy(sig)
            adjusted_sig["entry"] = self._round_price(planned_entry)
            adjusted_sig["extra"] = {
                **extra,
                **diagnostics,
                "entry_repriced": adjusted_sig["entry"] != self._round_price(entry),
                "status_reason": "entry_structural_price_ready",
            }
            return True, adjusted_sig, ""

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
        original_entry = self._safe_float(sig.get("entry"), 0.0)
        original_stop = self._safe_float(sig.get("stop_loss"), 0.0)
        original_target = self._safe_float(sig.get("take_profit"), 0.0)
        extra = self._signal_extra(sig)
        if self._is_structural_anchor_entry(sig):
            planned_entry = self._planned_entry_price(sig) or original_entry
            direction = str(sig.get("direction") or "").strip().lower()
            if self._price_worse_than_planned(direction, entry_price, planned_entry):
                preserved_sig = copy.deepcopy(sig)
                preserved_sig["extra"] = {
                    **extra,
                    "order_flow": decision,
                    "order_flow_shadow": decision,
                    "order_flow_enforced": bool(decision.get("enforced")),
                    "order_flow_entry_confirmed": False,
                    "order_flow_reprice_blocked": True,
                    "order_flow_reprice_block_reason": "structural_anchor_worse_than_planned",
                    "order_flow_market_limit_price": entry_price,
                    "planned_entry_price": planned_entry,
                    "entry_order_type": "LMT",
                    "entry_limit_intent": "structural_anchor",
                    "entry_price_plan": "structural_anchor_limit",
                    "entry_repriced": False,
                }
                return preserved_sig
        adjusted_sig = copy.deepcopy(sig)
        adjusted_sig["entry"] = self._round_price(entry_price)
        delta = adjusted_sig["entry"] - self._round_price(original_entry)
        if original_stop > 0:
            adjusted_sig["stop_loss"] = self._round_price(original_stop + delta)
        if original_target > 0:
            adjusted_sig["take_profit"] = self._round_price(original_target + delta)
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
            "buying_power_baseline_available": guard.get("baseline_available"),
            "buying_power_baseline_source": guard.get("baseline_source"),
            "buying_power_baseline_fetched_at": guard.get("baseline_fetched_at"),
            "buying_power_snapshot_age_s": guard.get("snapshot_age_s"),
            "buying_power_snapshot_max_age_s": guard.get("snapshot_max_age_s"),
            "buying_power_snapshot_fresh": guard.get("snapshot_fresh"),
            "buying_power_snapshot_stale_allowed": guard.get("snapshot_stale_allowed"),
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
        if (guard or {}).get("local_reserved_exposure") not in (None, ""):
            detail["本地已预占购买力"] = guard_number("local_reserved_exposure")
        if (guard or {}).get("local_reserved_count") not in (None, ""):
            detail["本地预占订单数"] = guard.get("local_reserved_count")
        if (guard or {}).get("baseline_fetched_at"):
            detail["购买力基线时间"] = str(guard.get("baseline_fetched_at") or "")
        if (guard or {}).get("snapshot_fetched_at"):
            detail["购买力快照时间"] = str(guard.get("snapshot_fetched_at") or "")
        if (guard or {}).get("snapshot_force_refresh_result") not in (None, ""):
            detail["快照强制刷新"] = str(guard.get("snapshot_force_refresh_result") or "")
            if (guard or {}).get("snapshot_force_refresh_reason"):
                detail["强制刷新原因"] = str(guard.get("snapshot_force_refresh_reason") or "")
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

    def _buying_power_max_snapshot_age_sec(self) -> float:
        return max(1.0, self._config_float("ibkr_buying_power_max_snapshot_age_sec", 180.0))

    def _buying_power_stale_baseline_max_age_sec(self) -> float:
        return max(
            self._buying_power_max_snapshot_age_sec(),
            self._config_float("ibkr_buying_power_stale_baseline_max_age_sec", 1800.0),
        )

    def _buying_power_stale_safe_max_age_sec(self) -> float:
        return max(
            self._buying_power_max_snapshot_age_sec(),
            self._config_float("ibkr_buying_power_stale_safe_max_age_sec", 1800.0),
        )

    def _buying_power_stale_force_refresh_cooldown_sec(self) -> float:
        return max(0.0, self._config_float("ibkr_buying_power_stale_force_refresh_cooldown_sec", 15.0))

    def _buying_power_stale_force_refresh_allowed(self) -> tuple[bool, float]:
        cooldown_s = self._buying_power_stale_force_refresh_cooldown_sec()
        if cooldown_s <= 0:
            setattr(self, "_buying_power_stale_force_refresh_last_at", time.time())
            return True, 0.0
        now = time.time()
        last_at = self._safe_float(getattr(self, "_buying_power_stale_force_refresh_last_at", 0.0), 0.0)
        remaining_s = cooldown_s - max(0.0, now - last_at)
        if last_at > 0 and remaining_s > 0:
            return False, remaining_s
        setattr(self, "_buying_power_stale_force_refresh_last_at", now)
        return True, 0.0

    def _buying_power_should_force_refresh_stale_snapshot(
        self,
        *,
        stale_age_s: float | None,
        freshness_block_reason: str,
    ) -> bool:
        reason = str(freshness_block_reason or "").strip()
        if reason == "buying_power_snapshot_missing_timestamp":
            return True
        if not self._config_bool("ibkr_buying_power_stale_safe_enabled", True):
            return True
        if stale_age_s is None:
            return True
        return float(stale_age_s) > self._buying_power_stale_safe_max_age_sec()

    def _buying_power_stale_safe_required_remaining(self, guard: dict) -> float:
        min_usd = max(0.0, self._config_float("ibkr_buying_power_stale_safe_min_usd", 50000.0))
        block_multiple = max(0.0, self._config_float("ibkr_buying_power_stale_safe_block_multiple", 5.0))
        exposure_multiple = max(0.0, self._config_float("ibkr_buying_power_stale_safe_exposure_multiple", 3.0))
        block_floor = self._safe_float((guard or {}).get("block_floor"), 0.0)
        requested_exposure = self._safe_float((guard or {}).get("requested_exposure"), 0.0)
        return max(
            min_usd,
            block_floor * block_multiple,
            requested_exposure * exposure_multiple,
        )

    def _buying_power_stale_snapshot_can_allow(
        self,
        guard: dict,
        *,
        stale_age_s: float | None,
        freshness_block_reason: str,
    ) -> tuple[bool, dict]:
        details = {
            "stale_safe_enabled": self._config_bool("ibkr_buying_power_stale_safe_enabled", True),
            "stale_safe_age_s": round(float(stale_age_s), 1) if stale_age_s is not None else None,
            "stale_safe_max_age_s": round(float(self._buying_power_stale_safe_max_age_sec()), 1),
            "stale_safe_required_remaining": round(self._buying_power_stale_safe_required_remaining(guard), 2),
            "stale_safe_freshness_reason": str(freshness_block_reason or ""),
        }
        if not details["stale_safe_enabled"]:
            details["stale_safe_block_reason"] = "disabled"
            return False, details
        if freshness_block_reason == "buying_power_snapshot_missing_timestamp":
            details["stale_safe_block_reason"] = "missing_timestamp"
            return False, details
        if stale_age_s is None or float(stale_age_s) > self._buying_power_stale_safe_max_age_sec():
            details["stale_safe_block_reason"] = "too_old"
            return False, details
        state = str((guard or {}).get("state") or "").strip().lower()
        if state not in {"ok", "warning"}:
            details["stale_safe_block_reason"] = f"state_{state or 'unknown'}"
            return False, details
        remaining_after = (guard or {}).get("remaining_after")
        if remaining_after in (None, ""):
            remaining_after = (guard or {}).get("remaining")
        remaining_after_value = self._safe_float(remaining_after, -1.0)
        details["stale_safe_remaining_after"] = round(remaining_after_value, 2)
        if remaining_after_value < details["stale_safe_required_remaining"]:
            details["stale_safe_block_reason"] = "remaining_after_below_safe_floor"
            return False, details
        details["stale_safe_block_reason"] = ""
        return True, details

    @staticmethod
    def _parse_snapshot_timestamp(value) -> float:
        if value in (None, ""):
            return 0.0
        try:
            number = float(value)
            if number > 0:
                return number / 1000.0 if number > 10_000_000_000 else number
        except (TypeError, ValueError):
            pass
        try:
            text = str(value or "").strip()
            if not text:
                return 0.0
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except Exception:
            return 0.0

    @classmethod
    def _buying_power_payload_age_s(cls, payload: dict | None) -> float | None:
        data = payload if isinstance(payload, dict) else {}
        guard = data.get("buying_power_guard") if isinstance(data.get("buying_power_guard"), dict) else {}
        for value in (
            data.get("fetched_at"),
            data.get("snapshot_fetched_at"),
            data.get("baseline_fetched_at"),
            guard.get("snapshot_fetched_at"),
            guard.get("baseline_fetched_at"),
        ):
            epoch = cls._parse_snapshot_timestamp(value)
            if epoch > 0:
                return max(0.0, time.time() - epoch)
        return None

    def _account_buying_power_snapshot(self, *, force_refresh: bool = False) -> dict:
        provider = getattr(self, "account_snapshot_provider", None)
        if callable(provider):
            try:
                if force_refresh:
                    try:
                        snapshot = provider(force_refresh=True)
                    except TypeError:
                        snapshot = provider()
                else:
                    snapshot = provider()
                return snapshot if isinstance(snapshot, dict) else {}
            except Exception as exc:
                _service_mod().logger.warning("Buying-power snapshot provider failed: %s", exc)
                return {}
        try:
            from ibkr_compute.api.account.snapshot import _build_ibkr_account_buying_power_snapshot

            snapshot = _build_ibkr_account_buying_power_snapshot(self, force_refresh=bool(force_refresh))
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as exc:
            _service_mod().logger.warning("Buying-power snapshot failed: %s", exc)
            return {}

    def _evaluate_signal_buying_power_guard(self, sig: dict) -> dict:
        service_mod = _service_mod()
        reservation_store = getattr(self, "buying_power_reservations", None)
        reservation_snapshot = {}
        snapshotter = getattr(reservation_store, "snapshot", None)
        if callable(snapshotter):
            try:
                reservation_snapshot = snapshotter()
            except Exception as exc:
                service_mod.logger.warning("Buying-power reservation snapshot failed: %s", exc)
                reservation_snapshot = {}

        baseline = {}
        baseline_getter = getattr(reservation_store, "baseline_snapshot", None)
        if callable(baseline_getter):
            try:
                baseline_candidate = baseline_getter()
                baseline = baseline_candidate if isinstance(baseline_candidate, dict) else {}
            except Exception as exc:
                service_mod.logger.warning("Buying-power baseline load failed: %s", exc)
                baseline = {}

        max_snapshot_age_s = self._buying_power_max_snapshot_age_sec()
        baseline_max_age_s = self._buying_power_stale_baseline_max_age_sec()
        baseline_available = bool(baseline.get("available"))
        baseline_age_s = self._buying_power_payload_age_s(baseline) if baseline_available else None
        baseline_fresh = bool(
            baseline_available
            and baseline_age_s is not None
            and baseline_age_s <= baseline_max_age_s
        )
        snapshot = {}
        snapshot_guard = {}
        snapshot_age_s = None
        snapshot_fresh = False
        freshness_block_reason = ""
        force_refresh_details = {}
        if baseline_fresh:
            account_summary = dict(baseline.get("summary") or {})
            account_summary_source = "baseline"
            guard_source = "local_baseline"
        else:
            def evaluate_snapshot_payload(payload: dict) -> tuple[dict, float | None, dict, bool, bool]:
                payload = payload if isinstance(payload, dict) else {}
                candidate_guard = payload.get("buying_power_guard")
                candidate_age_s = self._buying_power_payload_age_s(payload)
                candidate_summary = dict(payload.get("summary") or {})
                candidate_guard_available = (
                    not isinstance(candidate_guard, dict)
                    or candidate_guard.get("available") is not False
                    or str(candidate_guard.get("state") or "").strip().lower() in {"ok", "warning", "blocked"}
                )
                candidate_fresh = bool(
                    candidate_summary
                    and candidate_guard_available
                    and candidate_age_s is not None
                    and candidate_age_s <= max_snapshot_age_s
                )
                return (
                    candidate_guard if isinstance(candidate_guard, dict) else {},
                    candidate_age_s,
                    candidate_summary,
                    bool(candidate_guard_available),
                    bool(candidate_fresh),
                )

            snapshot = self._account_buying_power_snapshot()
            snapshot_guard, snapshot_age_s, snapshot_summary, snapshot_guard_available, snapshot_fresh = evaluate_snapshot_payload(snapshot)
            if not snapshot_fresh:
                candidate_summary = snapshot_summary or dict(baseline.get("summary") or {})
                candidate_source = "snapshot" if snapshot_summary else "baseline" if baseline.get("summary") else ""
                if candidate_summary and (baseline_available or snapshot_guard_available):
                    candidate_reason = (
                        "buying_power_snapshot_missing_timestamp"
                        if snapshot_age_s is None and baseline_age_s is None
                        else "buying_power_snapshot_stale"
                    )
                    stale_age_s = snapshot_age_s if candidate_source == "snapshot" else baseline_age_s
                    if self._buying_power_should_force_refresh_stale_snapshot(
                        stale_age_s=stale_age_s,
                        freshness_block_reason=candidate_reason,
                    ):
                        allowed, retry_after_s = self._buying_power_stale_force_refresh_allowed()
                        if allowed:
                            forced_snapshot = self._account_buying_power_snapshot(force_refresh=True)
                            (
                                forced_guard,
                                forced_age_s,
                                forced_summary,
                                forced_guard_available,
                                forced_fresh,
                            ) = evaluate_snapshot_payload(forced_snapshot)
                            force_refresh_details = {
                                "snapshot_force_refresh_attempted": True,
                                "snapshot_force_refresh_result": "fresh" if forced_fresh else "stale" if forced_summary else "unavailable",
                                "snapshot_force_refresh_reason": str(
                                    forced_guard.get("reason")
                                    or forced_snapshot.get("refresh_error")
                                    or forced_snapshot.get("last_refresh_error")
                                    or ""
                                ),
                                "snapshot_force_refresh_age_s": round(float(forced_age_s), 1) if forced_age_s is not None else None,
                            }
                            if forced_fresh:
                                snapshot = forced_snapshot
                                snapshot_guard = forced_guard
                                snapshot_age_s = forced_age_s
                                snapshot_summary = forced_summary
                                snapshot_guard_available = forced_guard_available
                                snapshot_fresh = True
                            elif isinstance(forced_snapshot, dict) and forced_snapshot.get("hard_blocked") is not None:
                                force_refresh_details["snapshot_force_refresh_hard_blocked"] = bool(forced_snapshot.get("hard_blocked"))
                        else:
                            force_refresh_details = {
                                "snapshot_force_refresh_attempted": False,
                                "snapshot_force_refresh_result": "cooldown",
                                "snapshot_force_refresh_retry_after_s": round(float(retry_after_s), 1),
                            }
            if snapshot_fresh:
                baseline_updater = getattr(reservation_store, "update_baseline_from_snapshot", None)
                if callable(baseline_updater):
                    try:
                        baseline_result = baseline_updater(snapshot)
                        if baseline_result.get("ok") and isinstance(baseline_result.get("baseline"), dict):
                            baseline = dict(baseline_result.get("baseline") or {})
                            baseline_available = True
                            baseline_age_s = self._buying_power_payload_age_s(baseline)
                            baseline_fresh = bool(
                                baseline_age_s is not None and baseline_age_s <= baseline_max_age_s
                            )
                    except Exception as exc:
                        service_mod.logger.warning("Buying-power baseline update failed: %s", exc)
                account_summary = snapshot_summary
                account_summary_source = "snapshot"
                guard_source = ""
            else:
                account_summary = snapshot_summary or dict(baseline.get("summary") or {})
                account_summary_source = "snapshot" if snapshot_summary else "baseline" if baseline.get("summary") else ""
                guard_source = ""
                if account_summary and (baseline_available or snapshot_guard_available):
                    freshness_block_reason = (
                        "buying_power_snapshot_missing_timestamp"
                        if snapshot_age_s is None and baseline_age_s is None
                        else "buying_power_snapshot_stale"
                    )

        summary = apply_reservations_to_buying_power_summary(
            account_summary,
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
        account_remaining_raw = account_summary.get("remaining_buying_power")
        if account_remaining_raw in (None, ""):
            account_remaining_raw = account_summary.get("buying_power")
        if account_remaining_raw not in (None, ""):
            guard["account_remaining_buying_power"] = self._safe_float(account_remaining_raw, 0.0)
        merge_reservation_snapshot_into_guard(guard, reservation_snapshot)
        if guard_source:
            guard["source"] = guard_source
        else:
            self._merge_buying_power_snapshot_guard(guard, snapshot_guard if isinstance(snapshot_guard, dict) else None)
        if force_refresh_details:
            guard.update(force_refresh_details)
        if baseline_available:
            guard["baseline_available"] = True
            guard["baseline_source"] = str(baseline.get("source") or "")
            guard["baseline_fetched_at"] = str(baseline.get("fetched_at") or "")
            guard["baseline_stored_at"] = str(baseline.get("stored_at") or "")
            guard["baseline_cache_state"] = str(baseline.get("cache_state") or "")
            if baseline_age_s is not None:
                guard["baseline_age_s"] = round(float(baseline_age_s), 1)
            guard["baseline_max_age_s"] = round(float(baseline_max_age_s), 1)
            guard["baseline_fresh"] = bool(baseline_fresh)
            for key in self.BUYING_POWER_SNAPSHOT_META_KEYS:
                if key.startswith("risk_model") and baseline.get(key) not in (None, ""):
                    guard[key] = baseline.get(key)
        if isinstance((snapshot or {}).get("errors"), dict):
            guard["snapshot_errors"] = dict((snapshot or {}).get("errors") or {})
        guard["snapshot_fetched_at"] = (
            (snapshot or {}).get("fetched_at")
            or baseline.get("fetched_at")
            or ""
        )
        if snapshot_age_s is not None:
            guard["snapshot_age_s"] = round(float(snapshot_age_s), 1)
        guard["snapshot_max_age_s"] = round(float(max_snapshot_age_s), 1)
        guard["baseline_max_age_s"] = round(float(baseline_max_age_s), 1)
        guard["snapshot_fresh"] = bool(snapshot_fresh or baseline_fresh)
        if freshness_block_reason and guard.get("enabled"):
            stale_age_s = snapshot_age_s if account_summary_source == "snapshot" else baseline_age_s
            stale_allowed, stale_details = self._buying_power_stale_snapshot_can_allow(
                guard,
                stale_age_s=stale_age_s,
                freshness_block_reason=freshness_block_reason,
            )
            guard.update(stale_details)
            if stale_allowed:
                guard["available"] = True
                guard["state"] = "warning"
                guard["reason"] = "buying_power_snapshot_stale_allowed_safe"
                guard["snapshot_error"] = freshness_block_reason
                guard["snapshot_stale_allowed"] = True
                guard["snapshot_stale_allowed_reason"] = "safe_remaining_after"
                guard["original_freshness_block_reason"] = freshness_block_reason
                guard["source"] = guard.get("source") or (
                    "stale_account_snapshot" if account_summary_source == "snapshot" else "stale_local_baseline"
                )
            else:
                guard["available"] = False
                guard["state"] = "unavailable"
                guard["reason"] = freshness_block_reason
                guard["snapshot_error"] = freshness_block_reason
        if not freshness_block_reason and exposure <= 0 and guard.get("enabled"):
            guard["state"] = "blocked"
            guard["reason"] = "buying_power_price_unavailable"
        self._recompute_buying_power_guard_capacity(guard)
        try:
            set_buying_power_guard_effective_metrics(guard, environment=service_mod.ENVIRONMENT)
        except Exception:
            service_mod.logger.debug("Failed to publish effective buying-power guard metrics", exc_info=True)
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
        reason = str((guard or {}).get("reason") or "").strip()
        title = "自动开仓购买力预警"
        if state == "unavailable":
            title = "自动开仓暂停：购买力快照过期" if reason == "buying_power_snapshot_stale" else "自动开仓暂停：购买力风控不可用"
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
            ack_result = ack(
                signal_id=signal_id,
                status=status,
                note=note,
                environment=service_mod.ENVIRONMENT,
                extra=dict(extra or {}),
                lifecycle_update=True,
            )
            if isinstance(ack_result, dict) and any(
                key in ack_result for key in ("success", "ok", "updated", "idempotent", "signal_status")
            ):
                return True
            return False
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
        oca_group = raw_oca_group
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
        signal_extra = normalize_independent_scale_plan_extra(
            signal_extra,
            signal_id=str(sig.get("signal_id") or ""),
            trade_group_id=str(trade_group_id or ""),
            symbol=str(sig.get("symbol") or ""),
            direction=str(sig.get("direction") or ""),
        )
        if bool(signal_extra.get("tv_direct_entry")):
            order_extra = {**signal_extra, **order_extra}
        order_extra = normalize_independent_scale_plan_extra(
            order_extra,
            signal_id=str(sig.get("signal_id") or ""),
            trade_group_id=str(trade_group_id or ""),
            symbol=str(sig.get("symbol") or ""),
            direction=str(sig.get("direction") or ""),
        )
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
                "quote_guard_status",
                "quote_guard_missing_quote_allowed",
                "quote_guard_missing_quote_allow_reason",
                "price_drift_r",
                "price_drift_threshold_r",
                "price_drift_exceeds_threshold",
                "structural_anchor_guard",
                "planned_entry_price",
                "structural_anchor_reference_price",
                "structural_anchor_reference_source",
                "structural_anchor_price_reached",
                "reprice_source",
                "original_entry",
                "original_stop_loss",
                "original_take_profit",
            )
            if signal_extra.get(key) not in (None, "")
        }
        buying_power_guard = (
            dict(result.get("buying_power_guard"))
            if isinstance(result.get("buying_power_guard"), dict)
            else dict(signal_extra.get("buying_power_guard"))
            if isinstance(signal_extra.get("buying_power_guard"), dict)
            else {}
        )
        buying_power_fields = self._buying_power_extra_fields(buying_power_guard) if buying_power_guard else {}
        protection_complete = bool(result.get("protection_complete"))
        protection_confirmation_pending = bool(
            result.get("protection_confirmation_pending")
            or result.get("pending_confirmation")
            or (
                isinstance(result.get("submission"), dict)
                and result.get("submission", {}).get("pending_confirmation")
            )
        )
        protection_pending_async = bool(protection_confirmation_pending and not protection_complete)
        protection_incomplete = bool(result.get("protection_incomplete")) or self._is_protection_incomplete_result(result)
        if protection_confirmation_pending:
            protection_incomplete = False
        elif not protection_complete and not protection_incomplete and "protection_complete" in result:
            protection_incomplete = True
        protection_ready = bool(protection_complete or protection_pending_async)
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
            if protection_ready and tv_direct_ack
            else "submitted"
            if protection_ready
            else "protection_incomplete"
        )
        signal_note = (
            "submitted_waiting_fill"
            if protection_ready and tv_direct_ack
            else "order_submitted_by_ibkr_compute"
            if protection_ready
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
                    "status": "Submitted" if protection_ready else "Init",
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
                    "status": "Submitted" if protection_ready else "Init",
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
            "relation_status": "active" if protection_ready else "protection_incomplete",
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
                "protection_pending_async": protection_pending_async,
                "protection_confirmation_pending": protection_confirmation_pending,
                "pending_confirmation": bool(result.get("pending_confirmation") or protection_confirmation_pending),
                "confirmation_mode": str(result.get("confirmation_mode") or ""),
                "missing_order_ids": list(result.get("missing_order_ids") or []),
                **protection_fields,
                "submitted_order_ids": list(result.get("order_ids") or []),
                "protection_incomplete_diagnostic": diagnostic,
                "safety_cancel_recommended": bool(diagnostic.get("cancel_recommended")) if diagnostic else False,
                **order_extra,
                **harvest_fields,
                **exit_policy_fields,
                **quote_guard_fields,
                **buying_power_fields,
                "order_flow": signal_extra.get("order_flow", {}),
                "order_flow_shadow": signal_extra.get("order_flow_shadow", {}),
                "status_reason": signal_note if protection_ready else "bracket_protection_incomplete",
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
