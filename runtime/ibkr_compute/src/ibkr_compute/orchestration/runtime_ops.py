from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimeOpsMixin:
    _WORKING_PROTECTION_ORDER_STATUSES = {
        "apisent",
        "apipending",
        "pending",
        "pendingsubmit",
        "presubmitted",
        "submitted",
        "working",
    }

    @staticmethod
    def _escape_filter_value(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _safe_extra(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _order_role(order: dict[str, Any]) -> str:
        extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
        role = str(order.get("role") or extra.get("role") or order.get("order_type") or "").strip().lower()
        unique_id = str(order.get("unique_id") or order.get("cOID") or order.get("coid") or "").strip().lower()
        if not role and unique_id.startswith("tp_"):
            return "take_profit"
        if not role and unique_id.startswith("sl_"):
            return "stop_loss"
        return role

    @staticmethod
    def _order_status(order: dict[str, Any]) -> str:
        return str(order.get("status") or order.get("order_status") or "").strip()

    @staticmethod
    def _order_broker_id(order: dict[str, Any]) -> str:
        extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
        return str(
            order.get("broker_order_id")
            or order.get("order_id")
            or order.get("orderId")
            or extra.get("broker_order_id")
            or ""
        ).strip()

    @staticmethod
    def _order_unique_id(order: dict[str, Any]) -> str:
        return str(
            order.get("unique_id")
            or order.get("cOID")
            or order.get("coid")
            or order.get("orderRef")
            or order.get("order_ref")
            or ""
        ).strip()

    def _live_protection_status(
        self,
        *,
        role_order_ids: dict[str, list[str]],
        role_unique_ids: dict[str, list[str]],
    ) -> dict[str, Any]:
        tracker = getattr(self, "order_tracker", None)
        if not tracker:
            return {
                "performed": False,
                "role_statuses": {"take_profit": [], "stop_loss": []},
                "missing_roles": [],
                "unverified_roles": [],
                "coverage": {},
                "error": "",
            }

        order_id_to_role: dict[str, str] = {}
        unique_id_to_role: dict[str, str] = {}
        all_order_ids: list[str] = []
        for role, order_ids in role_order_ids.items():
            for order_id in order_ids:
                normalized = str(order_id or "").strip()
                if not normalized:
                    continue
                order_id_to_role[normalized] = role
                all_order_ids.append(normalized)
        for role, unique_ids in role_unique_ids.items():
            for unique_id in unique_ids:
                normalized = str(unique_id or "").strip()
                if normalized:
                    unique_id_to_role[normalized] = role

        live_orders: list[dict[str, Any]] = []
        coverage: dict[str, Any] = {}
        error = ""
        if all_order_ids and callable(getattr(tracker, "get_complete_live_open_orders", None)):
            try:
                live_payload = tracker.get_complete_live_open_orders(
                    pb_seed_ids=all_order_ids,
                    retries=1,
                    retry_delay=0.1,
                    force=True,
                )
                if isinstance(live_payload, dict):
                    live_orders = [
                        dict(item)
                        for item in (live_payload.get("orders") or [])
                        if isinstance(item, dict)
                    ]
                    coverage = dict(live_payload.get("coverage") or {})
            except Exception as exc:
                error = str(exc)
        elif all_order_ids and callable(getattr(tracker, "get_orders_by_ids", None)):
            try:
                live_orders = [
                    dict(item)
                    for item in (tracker.get_orders_by_ids(all_order_ids) or [])
                    if isinstance(item, dict)
                ]
            except Exception as exc:
                error = str(exc)

        live_role_statuses: dict[str, list[str]] = {"take_profit": [], "stop_loss": []}
        for live_order in live_orders:
            live_order_id = self._order_broker_id(live_order) or str(live_order.get("id") or "").strip()
            live_unique_id = self._order_unique_id(live_order)
            role = order_id_to_role.get(live_order_id) or unique_id_to_role.get(live_unique_id) or self._order_role(live_order)
            if role in {"tp", "takeprofit"}:
                role = "take_profit"
            elif role in {"sl", "stoploss"}:
                role = "stop_loss"
            if role not in live_role_statuses:
                continue
            live_role_statuses[role].append(self._order_status(live_order))

        missing_live_roles = [
            role
            for role, statuses in live_role_statuses.items()
            if not any(status.strip().lower() in self._WORKING_PROTECTION_ORDER_STATUSES for status in statuses)
        ]
        unverified_roles = [
            role
            for role in live_role_statuses
            if not role_order_ids.get(role)
        ]
        return {
            "performed": True,
            "role_statuses": live_role_statuses,
            "missing_roles": missing_live_roles,
            "unverified_roles": unverified_roles,
            "coverage": coverage,
            "error": error,
        }

    def _signal_protection_status(self, *, signal_id: str, environment: str, trade_group_id: str = "") -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        env_filter = f'environment = "{self._escape_filter_value(environment)}"'

        def add_rows(filter_expr: str) -> None:
            for row in (
                self.pb.get_records(
                    "orders",
                    filter=filter_expr,
                    sort="-updated",
                    per_page=100,
                )
                or []
            ):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("unique_id") or row.get("order_id") or len(seen))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)

        if signal_id:
            add_rows(
                f'signal_id = "{self._escape_filter_value(signal_id)}" && {env_filter}'
            )
        if trade_group_id:
            add_rows(
                f'trade_group_id = "{self._escape_filter_value(trade_group_id)}" && {env_filter}'
            )

        role_statuses: dict[str, list[str]] = {"take_profit": [], "stop_loss": []}
        role_order_ids: dict[str, list[str]] = {"take_profit": [], "stop_loss": []}
        role_unique_ids: dict[str, list[str]] = {"take_profit": [], "stop_loss": []}
        for row in rows:
            if not isinstance(row, dict):
                continue
            role = self._order_role(row)
            if role in {"tp", "takeprofit"}:
                role = "take_profit"
            elif role in {"sl", "stoploss"}:
                role = "stop_loss"
            if role not in role_statuses:
                continue
            role_statuses[role].append(self._order_status(row))
            broker_order_id = self._order_broker_id(row)
            unique_id = self._order_unique_id(row)
            if broker_order_id:
                role_order_ids[role].append(broker_order_id)
            if unique_id:
                role_unique_ids[role].append(unique_id)

        pb_missing_roles = [
            role
            for role, statuses in role_statuses.items()
            if not any(status.strip().lower() in self._WORKING_PROTECTION_ORDER_STATUSES for status in statuses)
        ]
        live_status = self._live_protection_status(
            role_order_ids=role_order_ids,
            role_unique_ids=role_unique_ids,
        )
        live_missing_roles = list(live_status.get("missing_roles") or [])
        live_unverified_roles = list(live_status.get("unverified_roles") or [])
        missing_roles = list(dict.fromkeys([
            *pb_missing_roles,
            *(live_missing_roles if live_status.get("performed") else []),
            *(live_unverified_roles if live_status.get("performed") else []),
        ]))
        return {
            "complete": not missing_roles,
            "missing_roles": missing_roles,
            "role_statuses": role_statuses,
            "live_role_statuses": dict(live_status.get("role_statuses") or {}),
            "live_check_performed": bool(live_status.get("performed")),
            "live_check_error": str(live_status.get("error") or ""),
            "live_coverage": dict(live_status.get("coverage") or {}),
            "unverified_roles": live_unverified_roles,
            "orders_checked": len(rows),
        }

    @staticmethod
    def _protection_diagnostic_from_status(
        diagnostic: dict[str, Any],
        protection_status: dict[str, Any],
    ) -> dict[str, Any]:
        role_statuses = dict(protection_status.get("role_statuses") or {})
        missing_roles = list(protection_status.get("missing_roles") or [])
        return {
            **(diagnostic if isinstance(diagnostic, dict) else {}),
            "missing_protection_roles": missing_roles,
            "protection_order_statuses": role_statuses,
            "protection_live_order_statuses": dict(protection_status.get("live_role_statuses") or {}),
            "protection_live_check_performed": bool(protection_status.get("live_check_performed")),
            "protection_live_check_error": str(protection_status.get("live_check_error") or ""),
            "protection_live_coverage": dict(protection_status.get("live_coverage") or {}),
            "unverified_protection_roles": list(protection_status.get("unverified_roles") or []),
            "protection_orders_checked": int(protection_status.get("orders_checked") or 0),
            "cancel_recommended": True,
        }

    def _update_signal_after_entry_fill(self, order: dict, symbol: str, direction: str):
        service_mod = _service_mod()
        if not getattr(self, "pb", None):
            return
        signal_id = str(order.get("signal_id") or "").strip()
        coid = str(
            order.get("cOID")
            or order.get("coid")
            or order.get("orderRef")
            or order.get("order_ref")
            or ""
        ).strip()
        order_id = str(order.get("orderId") or order.get("order_id") or "").strip()
        runtime_environment = str(service_mod.ENVIRONMENT or "live").strip().lower() or "live"

        try:
            if not signal_id and (coid or order_id):
                filters = []
                if coid:
                    safe_coid = self._escape_filter_value(coid)
                    filters.append(
                        f'unique_id = "{safe_coid}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    filters.append(
                        f'entry_order_unique_id = "{safe_coid}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                if order_id:
                    safe_order_id = self._escape_filter_value(order_id)
                    filters.append(
                        f'order_id = "{safe_order_id}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    filters.append(
                        f'broker_order_id = "{safe_order_id}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                for filter_expr in filters:
                    rows = self.pb.get_records("orders", filter=filter_expr, sort="-updated", per_page=1)
                    if rows:
                        signal_id = str((rows[0] or {}).get("signal_id") or "").strip()
                        if signal_id:
                            break
            if not signal_id:
                return

            signal_record = self.pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                    f'environment = "{self._escape_filter_value(runtime_environment)}"'
                ),
            )
            if not signal_record or not signal_record.get("id"):
                return
            current_status = str(signal_record.get("status") or "").strip().lower()
            if current_status in {"protected_active", "closed", "expired", "rejected"}:
                return
            existing_extra = self._safe_extra(signal_record.get("extra"))
            trade_group_id = str(
                existing_extra.get("bracket_group")
                or existing_extra.get("trade_group_id")
                or existing_extra.get("submit_failed_bracket_group")
                or (coid[6:] if coid.startswith("entry_") else "")
                or ""
            ).strip()
            protection_status = self._signal_protection_status(
                signal_id=signal_id,
                environment=runtime_environment,
                trade_group_id=trade_group_id,
            )
            if not bool(protection_status.get("complete")):
                diagnostic = {}
                handler = getattr(getattr(self, "order_lifecycle", None), "handle_protection_incomplete", None)
                if callable(handler):
                    diagnostic = handler(
                        signal_id=signal_id,
                        symbol=symbol,
                        direction=direction,
                        order=order,
                        result={
                            "missing_order_ids": existing_extra.get("missing_order_ids") or [],
                            "order_ids": existing_extra.get("submitted_order_ids")
                            or existing_extra.get("submit_failed_order_ids")
                            or [],
                            "bracket_group": existing_extra.get("bracket_group")
                            or existing_extra.get("submit_failed_bracket_group")
                            or "",
                        },
                        reason="entry_fill_detected_with_incomplete_protection",
                    )
                missing_roles = list(protection_status.get("missing_roles") or [])
                role_statuses = dict(protection_status.get("role_statuses") or {})
                live_role_statuses = dict(protection_status.get("live_role_statuses") or {})
                diagnostic = self._protection_diagnostic_from_status(diagnostic, protection_status)
                extra = {
                    **existing_extra,
                    "entry_fill_detected_by": "order_tracker",
                    "entry_fill_broker_order_id": order_id,
                    "entry_fill_status": str(order.get("status") or ""),
                    "entry_fill_direction": direction,
                    "entry_fill_symbol": symbol,
                    "status_reason": "entry_fill_detected_with_incomplete_protection",
                    "protection_incomplete": True,
                    "protection_complete": False,
                    "protection_incomplete_diagnostic": diagnostic,
                    "missing_protection_roles": missing_roles,
                    "protection_order_statuses": role_statuses,
                    "protection_live_order_statuses": live_role_statuses,
                    "protection_live_check_performed": bool(protection_status.get("live_check_performed")),
                    "protection_live_check_error": str(protection_status.get("live_check_error") or ""),
                    "protection_live_coverage": dict(protection_status.get("live_coverage") or {}),
                    "unverified_protection_roles": list(protection_status.get("unverified_roles") or []),
                    "protection_orders_checked": int(protection_status.get("orders_checked") or 0),
                    "safety_cancel_recommended": True,
                }
                self.pb.update_record(
                    "ibkr_signals",
                    str(signal_record.get("id")),
                    {
                        "status": "protection_incomplete",
                        "note": "entry_fill_detected_with_incomplete_protection",
                        "extra": extra,
                    },
                )
                return
            extra = {
                **existing_extra,
                "entry_fill_detected_by": "order_tracker",
                "entry_fill_broker_order_id": order_id,
                "entry_fill_status": str(order.get("status") or ""),
                "entry_fill_direction": direction,
                "entry_fill_symbol": symbol,
                "status_reason": "entry_filled_protection_expected",
                "protection_complete": True,
                "protection_incomplete": False,
                "missing_protection_roles": [],
                "protection_order_statuses": dict(protection_status.get("role_statuses") or {}),
                "protection_live_order_statuses": dict(protection_status.get("live_role_statuses") or {}),
                "protection_live_check_performed": bool(protection_status.get("live_check_performed")),
                "protection_live_coverage": dict(protection_status.get("live_coverage") or {}),
                "unverified_protection_roles": [],
                "protection_orders_checked": int(protection_status.get("orders_checked") or 0),
                "safety_cancel_recommended": False,
            }
            self.pb.update_record(
                "ibkr_signals",
                str(signal_record.get("id")),
                {
                    "status": "protected_active",
                    "note": "entry_filled_protection_expected",
                    "extra": extra,
                },
            )
        except Exception as exc:
            service_mod.logger.error("Failed to update signal after entry fill: %s", exc)

    def _on_order_fill(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        order_type = str(order.get("orderType") or order.get("order_type") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        side = str(order.get("side") or "").strip().upper()
        role = "entry"
        if has_parent and order_type in {"STP", "STOP", "STOPLOSS"}:
            role = "stop_loss"
        elif has_parent:
            role = "take_profit"
        service_mod.logger.info("Order filled: %s role=%s", symbol or order.get("ticker"), role)
        if not symbol:
            return
        if role == "entry":
            direction = "long" if side == "BUY" else "short" if side == "SELL" else ""
            self.signal_processor.register_filled_position(
                symbol,
                {
                    "direction": direction,
                    "broker_order_id": str(order.get("orderId") or order.get("order_id") or ""),
                    "state": "filled_position",
                },
            )
            self._update_signal_after_entry_fill(order, symbol, direction)
            return
        self.signal_processor.remove_position(symbol)
        if role == "stop_loss":
            self.order_lifecycle.increment_sl_count()
            self.signal_processor.start_cooldown(
                symbol,
                self.signal_processor.cooldown_bars_after_sl(),
                "cooldown_after_stop_loss",
            )

    def _on_order_cancel(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        service_mod.logger.info("Order cancelled: %s", symbol or order.get("ticker"))
        if symbol and not has_parent:
            self.signal_processor.remove_position(symbol)

    def _schedule_retention(self):
        service_mod = _service_mod()

        def retention_loop():
            last_handled_hour = ""
            while self._running:
                et_now = datetime.now(service_mod.ET)
                hour_key = et_now.strftime("%Y-%m-%d %H")
                if et_now.minute == 12 and hour_key != last_handled_hour:
                    result = self.data_retention.cleanup(source="runtime_thread")
                    env_result = (result.get("environments") or [{}])[0]
                    service_mod.logger.info(
                        "Runtime retention cleanup finished: environment=%s deleted=%s errors=%s skipped=%s reason=%s",
                        service_mod.ENVIRONMENT,
                        int(result.get("total_deleted", 0) or 0),
                        int(result.get("total_errors", 0) or 0),
                        bool(env_result.get("skipped")),
                        str(env_result.get("reason") or ""),
                    )
                    last_handled_hour = hour_key
                time.sleep(30)

        thread = threading.Thread(
            target=retention_loop,
            daemon=True,
            name="data-retention",
        )
        thread.start()

    def stop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Stopping IBKR Trading Service...")
        with self._state_lock:
            self._starting = False
            self._startup_progress_enabled = False
            self._startup_cycle_id = ""
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
            self._startup_status_message_id = ""
        self._running = False
        self._last_session_authenticated = False
        self._auth_probe_stop.set()
        self._resource_monitor_stop.set()

        self.auth_handler.cancel()
        self.bar_aggregator.force_close_all()
        self.timeframe_builder.reset()
        self.realtime_quote_book.reset()
        self.ws_client.stop()
        self.session_keeper.stop()
        self.order_tracker.stop()
        self.order_lifecycle.stop()
        self.data_writer.close()
        self._signal_wakeup.set()
        self._warmup_wakeup.set()
        self._compute_queue.put(None)

        if self._signal_thread:
            self._signal_thread.join(timeout=10)
        if self._subscription_thread:
            self._subscription_thread.join(timeout=10)
        if self._active_repair_thread:
            self._active_repair_thread.join(timeout=10)
        if self._watchlist_backfill_thread:
            self._watchlist_backfill_thread.join(timeout=10)
        if self._compute_thread:
            self._compute_thread.join(timeout=10)
        if self._official_close_thread:
            self._official_close_thread.join(timeout=10)
        if self._bar_close_thread:
            self._bar_close_thread.join(timeout=10)
        if self._warmup_thread:
            self._warmup_thread.join(timeout=10)
        if self._interval_prime_thread:
            self._interval_prime_thread.join(timeout=10)
        if self._resource_monitor_thread:
            self._resource_monitor_thread.join(timeout=5)
        if self._auth_probe_thread and self._auth_probe_thread is not threading.current_thread():
            self._auth_probe_thread.join(timeout=5)

        self._set_warmup_state(
            phase="stopped",
            reason="service_stopped",
            finished_at=self._now_iso(),
            trading_gate_open=False,
            trading_gate_reason="runtime_stopped",
        )
        self._set_auth_recovery_state(
            recovery_phase="runtime_stopped",
            last_recovery_source="runtime_stop",
            lock_owner="",
            lock_expires_at="",
        )

        service_mod.logger.info("IBKR Trading Service stopped")
