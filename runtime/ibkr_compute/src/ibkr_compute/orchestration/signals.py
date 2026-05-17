from __future__ import annotations

import copy
import json
import time

from ibkr_compute.api.account.buying_power_guard import (
    build_buying_power_guard,
    estimate_entry_exposure,
)


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceSignalsMixin:
    CAPACITY_DEFER_REASONS = {"strategy_capacity_full"}

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
                else:
                    service_mod.logger.info(
                        "Skip signal/reverse processing while session is unauthenticated"
                    )
            except Exception as exc:
                service_mod.logger.error("Signal loop error: %s", exc)
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

                valid, reason = self.signal_processor.validate_signal(sig)
                if not valid:
                    if (
                        str(reason or "").startswith("warmup")
                        or reason in {"session_unauthenticated", "runtime_stopped", "no_trade_symbols"}
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
                    sig["extra"] = {
                        **extra,
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
                        settings=harvest_settings,
                    )

                if result.get("ok"):
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
                    self._mark_signal_submit_failed(sig, result)

                self.signal_router.mark_processed(signal_id)
                finalized = True
            finally:
                if not finalized:
                    self.signal_router.release_signal(signal_id)

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:
            return default
        return number

    @staticmethod
    def _round_price(value) -> float:
        return round(TradingServiceSignalsMixin._safe_float(value, 0.0), 2)

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
                    "entry_limit_intent": "passive",
                    "entry_price_plan": "passive_limit",
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

        quote = self._entry_guard_quote(symbol)
        quote_age_s = self._safe_float(quote.get("quote_age_s"), -1.0) if quote else -1.0
        reference_price, reference_source = self._guard_reference_price(quote, direction)
        fresh_quote = bool(quote and quote_age_s >= 0 and quote_age_s <= max_age_s and reference_price > 0)

        extra = self._signal_extra(sig)
        diagnostics = {
            "original_entry": entry,
            "original_stop_loss": stop_loss,
            "original_take_profit": take_profit,
            "pre_submit_guard": True,
            "quote_age_s": quote.get("quote_age_s") if quote else None,
            "pre_submit_reference_price": round(reference_price, 4) if reference_price > 0 else None,
            "pre_submit_reference_source": reference_source,
            "price_drift_r": 0.0,
            "reprice_source": "",
            "entry_limit_offset": self._safe_float(extra.get("entry_limit_offset"), 0.0),
            "entry_order_type": "LMT",
            "entry_limit_intent": "passive",
            "entry_price_plan": "passive_limit",
            "entry_repriced": False,
        }

        if live_environment and not fresh_quote:
            sig["extra"] = {**extra, **diagnostics, "status_reason": "entry_guard_no_fresh_quote"}
            return False, sig, "entry_guard_no_fresh_quote"

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
        }

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
            from ibkr_compute.api.account.snapshot import _build_ibkr_account_snapshot

            snapshot = _build_ibkr_account_snapshot(self)
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as exc:
            _service_mod().logger.warning("Buying-power snapshot failed: %s", exc)
            return {}

    def _evaluate_signal_buying_power_guard(self, sig: dict) -> dict:
        service_mod = _service_mod()
        snapshot = self._account_buying_power_snapshot()
        exposure = estimate_entry_exposure(
            sig.get("shares"),
            sig.get("entry"),
            sig.get("take_profit"),
            sig.get("stop_loss"),
            sig.get("direction"),
            "LMT",
        )
        guard = build_buying_power_guard(
            (snapshot or {}).get("summary") or {},
            config=getattr(self, "config", None),
            environment=service_mod.ENVIRONMENT,
            requested_exposure=exposure,
        )
        guard["snapshot_fetched_at"] = (snapshot or {}).get("fetched_at") or ""
        if exposure <= 0 and guard.get("enabled"):
            guard["state"] = "blocked"
            guard["reason"] = "buying_power_price_unavailable"
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
        if state == "blocked":
            title = "自动开仓已被购买力阈值拦截"
        elif level == "info":
            title = "自动开仓已提交"
        try:
            notifier(
                title,
                {
                    "信号ID": str((sig or {}).get("signal_id") or ""),
                    "标的": str((sig or {}).get("symbol") or "").upper(),
                    "方向": str((sig or {}).get("direction") or ""),
                    "数量": int(self._safe_float((sig or {}).get("shares"), 0.0)),
                    "当前剩余购买力": round(float((guard or {}).get("remaining") or 0.0), 2),
                    "本次预估占用": round(float((guard or {}).get("requested_exposure") or 0.0), 2),
                    "下单后剩余购买力": round(float((guard or {}).get("remaining_after") or 0.0), 2),
                    "预警阈值": round(float((guard or {}).get("warn_floor") or 0.0), 2),
                    "禁止阈值": round(float((guard or {}).get("block_floor") or 0.0), 2),
                    "状态": state,
                    "原因": str((guard or {}).get("reason") or ""),
                },
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
            "data_environment": data_environment,
            "updated_at": self._now_iso(),
            "source": "ibkr_compute",
        }
        merged_extra["execution_by_mode"] = execution_by_mode
        patch = {
            "note": note_text if broker_mode == data_environment == "live" else f"{broker_mode}:{note_text}",
            "extra": merged_extra,
        }
        if broker_mode == data_environment == "live":
            patch["status"] = status_text
        return patch

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
            patch = self._signal_broker_patch(
                "rejected",
                "fixed_position_symbol_blocked",
                existing_extra,
                {
                    "status_reason": "fixed_position_symbol_blocked",
                    "execution_state": "blocked",
                    "fixed_position_symbol_blocked": True,
                    "fixed_position_symbol": str(sig.get("symbol") or "").strip().upper(),
                    "fixed_position_symbols": fixed_symbols,
                    "blocked_at": self._now_iso(),
                },
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
            patch = self._signal_broker_patch(
                "rejected",
                "buying_power_blocked",
                existing_extra,
                {
                    **signal_extra,
                    **self._buying_power_extra_fields(guard),
                    "status_reason": "buying_power_blocked",
                    "execution_state": "blocked",
                    "buying_power_blocked": True,
                    "buying_power_blocked_at": self._now_iso(),
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal buying-power-blocked: signal_id=%s error=%s",
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
            patch = self._signal_broker_patch(
                "rejected",
                status_reason,
                existing_extra,
                {
                    "status_reason": status_reason,
                    "validation_rejected": True,
                    "validation_rejected_at": self._now_iso(),
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal validation-rejected: signal_id=%s reason=%s error=%s",
                signal_id,
                reason,
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
            patch = self._signal_broker_patch(
                "rejected",
                status_reason,
                existing_extra,
                {
                    **guard_extra,
                    "status_reason": status_reason,
                    "entry_guard_rejected": True,
                    "entry_guard_rejected_at": self._now_iso(),
                },
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
            patch = self._signal_broker_patch(
                "rejected",
                "duplicate_existing_broker_order",
                existing_extra,
                {
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
                },
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
            patch = self._signal_broker_patch(
                status,
                note,
                existing_extra,
                {
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
                },
            )
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            service_mod.logger.error(
                "Failed to mark signal submit-failed: signal_id=%s error=%s",
                signal_id,
                exc,
            )

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
        trade_group_id = result.get("bracket_group") or entry_unique_id
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
        signal_status = "submitted" if protection_complete else "protection_incomplete"
        signal_note = "order_submitted_by_ibkr_compute" if protection_complete else "protection_incomplete"

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
                "entry_limit_intent": "passive",
                "entry_price_plan": "passive_limit",
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
                "status_reason": "order_submitted_by_ibkr_compute"
                if protection_complete
                else "bracket_protection_incomplete",
                "protection_incomplete_diagnostic": diagnostic,
                "safety_cancel_recommended": bool(diagnostic.get("cancel_recommended")) if diagnostic else False,
                **order_extra,
                **harvest_fields,
                **exit_policy_fields,
                **buying_power_fields,
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
