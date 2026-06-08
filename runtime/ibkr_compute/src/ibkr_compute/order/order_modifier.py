"""
IB Gateway order modification and cancellation helpers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterable

from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.observability.prometheus import record_gateway_order_serial_event, record_order_event
from ibkr_compute.order.gateway_serial import GatewayOrderMutationGate, GatewayOrderMutationTimeout
from ibkr_compute.order.symbol_queue import SymbolOrderCommandScheduler

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")


class OrderModifier:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        broker: BrokerAdapter | None = None,
        config=None,
        environment: str = "live",
        gateway_gate: GatewayOrderMutationGate | None = None,
        symbol_scheduler: SymbolOrderCommandScheduler | None = None,
        reservation_store: Any = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.broker = broker or BrokerAdapter()
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.reservation_store = reservation_store
        self.gateway_gate = gateway_gate or GatewayOrderMutationGate(config=config, environment=self.environment)
        self.symbol_scheduler = symbol_scheduler or SymbolOrderCommandScheduler(
            config=config,
            environment=self.environment,
        )

    @staticmethod
    def _infer_modify_family(updates: Dict[str, Any] | None, fallback: str = "unknown") -> str:
        update_keys = {str(key or "").strip() for key in (updates or {}).keys()}
        if "auxPrice" in update_keys and not ({"price", "lmtPrice"} & update_keys):
            return "stop_loss"
        if {"price", "lmtPrice"} & update_keys and "auxPrice" not in update_keys:
            return "take_profit"
        if "quantity" in update_keys or "tif" in update_keys:
            return "entry"
        if update_keys:
            return "mixed"
        return fallback or "unknown"

    def modify_order(
        self,
        order_id: str,
        updates: Dict[str, Any],
        acct_id: str = None,
        *,
        operation: str = "modify_manual",
        order_family_type: str = "",
        symbol: str = "",
    ) -> Dict[str, Any]:
        family = str(order_family_type or "").strip() or self._infer_modify_family(updates)
        queue_symbol = str(symbol or self._infer_order_symbol(order_id) or "").strip().upper()
        return self.symbol_scheduler.submit(
            symbol=queue_symbol,
            operation="modify_order",
            priority=20,
            metadata={"order_id": str(order_id or "").strip(), "family": family},
            fn=lambda: self._modify_order_unqueued(
                order_id,
                updates,
                acct_id,
                operation=operation,
                order_family_type=family,
            ),
        )

    def _modify_order_unqueued(
        self,
        order_id: str,
        updates: Dict[str, Any],
        acct_id: str = None,
        *,
        operation: str = "modify_manual",
        order_family_type: str = "",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        family = str(order_family_type or "").strip() or self._infer_modify_family(updates)
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.modify_order(
                    str(order_id or "").strip(),
                    dict(updates or {}),
                    account_id=str(acct_id or self.account_id or "").strip(),
                    metric_environment=self.environment,
                )
            else:
                with self.gateway_gate.hold("modify_order", order_id=str(order_id or "").strip(), family=family) as gate_info:
                    result = self.broker.modify_order(
                        str(order_id or "").strip(),
                        dict(updates or {}),
                        account_id=str(acct_id or self.account_id or "").strip(),
                    )
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="modify_order",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
        except GatewayOrderMutationTimeout as exc:
            result = {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
                "order_id": str(order_id or "").strip(),
            }
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
        record_order_event(
            operation=str(operation or "modify_manual"),
            order_family_type=family,
            result="ok" if result.get("ok") else "error",
            reason_code=str(result.get("error") or result.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        if not result.get("ok"):
            logger.error("Order modify failed for %s: %s", order_id, result.get("error"))
        return result

    def update_stop_loss(self, order_id: str, new_sl_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating stop loss %s to %.4f", order_id, float(new_sl_price or 0.0))
        return self.modify_order(
            order_id,
            {"auxPrice": float(new_sl_price or 0.0)},
            acct_id,
            operation="adjust_stop_loss",
            order_family_type="stop_loss",
        )

    def update_take_profit(self, order_id: str, new_tp_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating take profit %s to %.4f", order_id, float(new_tp_price or 0.0))
        return self.modify_order(
            order_id,
            {"price": float(new_tp_price or 0.0)},
            acct_id,
            operation="adjust_take_profit",
            order_family_type="take_profit",
        )

    def cancel_order(
        self,
        order_id: str,
        acct_id: str = None,
        *,
        operation: str = "cancel_order",
        order_family_type: str = "unknown",
        symbol: str = "",
    ) -> Dict[str, Any]:
        queue_symbol = str(symbol or self._infer_order_symbol(order_id) or "").strip().upper()
        return self.symbol_scheduler.submit(
            symbol=queue_symbol,
            operation="cancel_order",
            priority=10,
            metadata={"order_id": str(order_id or "").strip(), "family": str(order_family_type or "unknown")},
            fn=lambda: self._cancel_order_unqueued(
                order_id,
                acct_id,
                operation=operation,
                order_family_type=order_family_type,
            ),
        )

    def cancel_order_ids(
        self,
        order_ids: Iterable[Any],
        acct_id: str = None,
        *,
        source: str = "cancel_order_ids",
        symbol: str = "",
    ) -> Dict[str, Any]:
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for item in order_ids or []:
            order_id = str(item or "").strip()
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            normalized_ids.append(order_id)
        if not normalized_ids:
            return {
                "ok": False,
                "error": "missing_order_ids",
                "requested": 0,
                "submitted": 0,
                "active_order_ids": [],
                "submitted_order_ids": [],
                "order_ids": [],
                "errors": [],
                "error_details": [],
                "source": source,
            }
        queue_symbol = str(symbol or self._infer_order_symbol(normalized_ids[0]) or "").strip().upper()
        return self.symbol_scheduler.submit(
            symbol=queue_symbol,
            operation="cancel_order_ids",
            priority=10,
            metadata={"order_ids": normalized_ids, "source": source},
            fn=lambda: self._cancel_order_ids_unqueued(
                normalized_ids,
                acct_id=acct_id,
                source=source,
            ),
        )

    def _cancel_order_ids_unqueued(
        self,
        order_ids: list[str],
        acct_id: str = None,
        *,
        source: str = "cancel_order_ids",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        operation = str(source or "cancel_order_ids")
        try:
            batch_cancel = getattr(self.broker, "cancel_order_ids", None)
            if callable(batch_cancel):
                result = batch_cancel(order_ids, metric_environment=self.environment, source=source)
            else:
                submitted: list[str] = []
                errors: list[dict] = []
                for order_id in order_ids:
                    try:
                        item = self.broker.cancel_order(order_id, metric_environment=self.environment)
                    except TypeError:
                        item = self.broker.cancel_order(order_id)
                    if isinstance(item, dict) and item.get("ok"):
                        submitted.append(order_id)
                    else:
                        errors.append({"order_id": order_id, "error": str((item or {}).get("error") if isinstance(item, dict) else "cancel_failed")})
                result = {
                    "ok": not errors or bool(submitted),
                    "pending_confirmation": bool(submitted),
                    "requested": len(order_ids),
                    "submitted": len(submitted),
                    "active_order_ids": list(order_ids),
                    "submitted_order_ids": submitted,
                    "order_ids": submitted,
                    "errors": [item["error"] for item in errors],
                    "error_details": errors,
                    "source": source,
                }
        except GatewayOrderMutationTimeout as exc:
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
            result = {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
                "requested": len(order_ids),
                "submitted": 0,
                "active_order_ids": list(order_ids),
                "submitted_order_ids": [],
                "order_ids": [],
                "errors": ["gateway_order_queue_timeout"],
                "error_details": [{"error": "gateway_order_queue_timeout"}],
                "source": source,
            }
        record_order_event(
            operation=operation,
            result="ok" if result.get("ok") else "error",
            reason_code=str(result.get("error") or result.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        if not result.get("ok"):
            logger.error("Cancel order ids failed: %s", result.get("error"))
        return dict(result or {}) if isinstance(result, dict) else {"ok": False, "error": "cancel_order_ids_failed"}

    def _cancel_order_unqueued(
        self,
        order_id: str,
        acct_id: str = None,
        *,
        operation: str = "cancel_order",
        order_family_type: str = "unknown",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.cancel_order(
                    str(order_id or "").strip(),
                    metric_environment=self.environment,
                )
            else:
                with self.gateway_gate.hold("cancel_order", order_id=str(order_id or "").strip()) as gate_info:
                    result = self.broker.cancel_order(str(order_id or "").strip())
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="cancel_order",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
        except GatewayOrderMutationTimeout as exc:
            result = {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
                "order_id": str(order_id or "").strip(),
            }
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
        record_order_event(
            operation=str(operation or "cancel_order"),
            order_family_type=str(order_family_type or "unknown"),
            result="ok" if result.get("ok") else "error",
            reason_code=str(result.get("error") or result.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        if not result.get("ok"):
            logger.error("Cancel order %s failed: %s", order_id, result.get("error"))
        terminal_sync = self._sync_terminal_cancel_to_pb(str(order_id or "").strip(), result)
        if terminal_sync:
            result["terminal_cancel_sync"] = terminal_sync
        return result

    def cancel_all_orders(self, acct_id: str = None) -> Dict[str, Any]:
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.cancel_all_orders(metric_environment=self.environment)
            else:
                with self.gateway_gate.hold("cancel_all_orders") as gate_info:
                    result = self.broker.cancel_all_orders()
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="cancel_all_orders",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
            pb_active_cancel = self._cancel_pb_active_orders_missing_from_result(result)
            if pb_active_cancel:
                result["pb_active_cancel"] = pb_active_cancel
                result["active_order_ids"] = self._merge_unique_order_ids(result.get("active_order_ids"), pb_active_cancel.get("active_order_ids"))
                result["submitted_order_ids"] = self._merge_unique_order_ids(result.get("submitted_order_ids"), pb_active_cancel.get("submitted_order_ids"))
                result["order_ids"] = self._merge_unique_order_ids(result.get("order_ids"), pb_active_cancel.get("order_ids"))
                result["requested"] = len(result.get("active_order_ids") or [])
                result["submitted"] = len(result.get("submitted_order_ids") or [])
                result["pending_confirmation"] = bool(result.get("pending_confirmation") or pb_active_cancel.get("pending_confirmation"))
                if pb_active_cancel.get("pending_confirmation"):
                    result["status"] = "CANCEL_ALL_REQUESTED"
                result["errors"] = [*(result.get("errors") or []), *(pb_active_cancel.get("errors") or [])]
                result["error_details"] = [*(result.get("error_details") or []), *(pb_active_cancel.get("error_details") or [])]
                result["ok"] = bool(result.get("ok") or pb_active_cancel.get("ok"))
            if result.get("ok") and not result.get("pending_confirmation"):
                sync_results = self._sync_terminal_cancel_all_to_pb(result)
                if sync_results:
                    result["terminal_cancel_sync"] = sync_results
            return result
        except GatewayOrderMutationTimeout as exc:
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
            return {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
            }

    def _infer_order_symbol(self, order_id: str) -> str:
        snapshotter = getattr(getattr(self.broker, "client", None), "get_order_snapshot", None)
        if not callable(snapshotter):
            return ""
        try:
            snapshot = snapshotter(str(order_id or "").strip())
        except Exception:
            return ""
        if not isinstance(snapshot, dict):
            return ""
        return str(snapshot.get("symbol") or snapshot.get("ticker") or "").strip().upper()

    @staticmethod
    def _escape_filter_value(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _ensure_object(value: Any) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _cancel_result_terminal_status(result: Dict[str, Any] | None) -> str:
        payload = result if isinstance(result, dict) else {}
        confirm = payload.get("confirm") if isinstance(payload.get("confirm"), dict) else {}
        status = str(payload.get("status") or confirm.get("status") or "").strip().upper()
        if status in {"NOT_OPEN", "CANCELLED", "CANCELED", "API_CANCELLED", "INACTIVE", "REJECTED", "EXPIRED"}:
            return status
        source = str(payload.get("source") or confirm.get("source") or "").strip().lower()
        details = payload.get("details") if isinstance(payload.get("details"), dict) else confirm.get("details")
        code = None
        if isinstance(details, dict):
            try:
                code = int(details.get("code"))
            except Exception:
                code = None
        if source == "cancel_terminal_notice" or code in {10147, 10148}:
            return "NOT_OPEN" if code == 10147 else "CANCELLED"
        return ""

    @classmethod
    def _is_terminal_cancel_result(cls, result: Dict[str, Any] | None) -> bool:
        payload = result if isinstance(result, dict) else {}
        if not bool(payload.get("ok")):
            return False
        if bool(payload.get("pending_confirmation")):
            return False
        return bool(cls._cancel_result_terminal_status(payload))

    @staticmethod
    def _pb_order_row_is_open_like(row: Dict[str, Any]) -> bool:
        status = str((row or {}).get("status") or "").strip().upper()
        relation_status = str((row or {}).get("relation_status") or "").strip().lower()
        if relation_status == "closed" or status in {
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }:
            return False
        return relation_status in {"active", "planned", ""} or status in {
            "INIT",
            "SUBMITTED",
            "PRESUBMITTED",
            "PENDING",
            "PENDINGSUBMIT",
            "APIPENDING",
            "API_PENDING",
        }

    def _load_pb_orders_for_broker_order_id(self, order_id: str) -> list[Dict[str, Any]]:
        getter = getattr(self.pb_client, "get_records", None)
        if not callable(getter):
            return []
        order_id_filter = self._escape_filter_value(order_id)
        environment_filter = self._escape_filter_value(self.environment)
        try:
            rows = getter(
                "orders",
                filter=(
                    f'(broker_order_id = "{order_id_filter}" || order_id = "{order_id_filter}") '
                    f'&& environment = "{environment_filter}"'
                ),
                sort="-updated",
                per_page=50,
            )
        except Exception as exc:
            logger.debug("PB terminal cancel lookup failed: order_id=%s error=%s", order_id, exc)
            return []
        return [dict(row) for row in (rows or []) if isinstance(row, dict)]

    def _load_active_pb_cancel_rows(self) -> list[Dict[str, Any]]:
        getter = getattr(self.pb_client, "get_records", None)
        if not callable(getter):
            return []
        environment_filter = self._escape_filter_value(self.environment)
        try:
            rows = getter(
                "orders",
                filter=(
                    f'environment="{environment_filter}" && broker_order_id!="" '
                    '&& (relation_status="active" || relation_status="planned" || relation_status="" || relation_status=null) '
                    '&& (status="Submitted" || status="Init" || status="PreSubmitted" || '
                    'status="PendingSubmit" || status="Pending" || status="ApiPending" || status="API_PENDING")'
                ),
                sort="-updated",
                per_page=500,
            )
        except Exception as exc:
            logger.debug("PB active cancel_all lookup failed: environment=%s error=%s", self.environment, exc)
            return []
        return [dict(row) for row in (rows or []) if isinstance(row, dict) and self._pb_order_row_is_open_like(dict(row))]

    @staticmethod
    def _merge_unique_order_ids(*groups: Any) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group or []:
                order_id = str(item or "").strip()
                if not order_id or order_id in seen:
                    continue
                seen.add(order_id)
                merged.append(order_id)
        return merged

    def _cancel_pb_active_orders_missing_from_result(self, result: Dict[str, Any]) -> dict:
        known_ids = set(
            self._merge_unique_order_ids(
                result.get("active_order_ids"),
                result.get("submitted_order_ids"),
                result.get("order_ids"),
            )
        )
        rows = self._load_active_pb_cancel_rows()
        extra_ids = self._merge_unique_order_ids(
            [
                str(row.get("broker_order_id") or row.get("order_id") or "").strip()
                for row in rows
                if str(row.get("broker_order_id") or row.get("order_id") or "").strip() not in known_ids
            ]
        )
        if not extra_ids:
            return {}
        batch_cancel = getattr(self.broker, "cancel_order_ids", None)
        if callable(batch_cancel):
            extra_result = batch_cancel(extra_ids, metric_environment=self.environment, source="pb_active_cancel_all")
        else:
            submitted: list[str] = []
            errors: list[dict] = []
            for order_id in extra_ids:
                try:
                    item = self.broker.cancel_order(order_id, metric_environment=self.environment)
                except TypeError:
                    item = self.broker.cancel_order(order_id)
                if isinstance(item, dict) and item.get("ok"):
                    submitted.append(order_id)
                else:
                    errors.append({"order_id": order_id, "error": str((item or {}).get("error") if isinstance(item, dict) else "cancel_failed")})
            extra_result = {
                "ok": not errors or bool(submitted),
                "pending_confirmation": bool(submitted),
                "requested": len(extra_ids),
                "submitted": len(submitted),
                "active_order_ids": extra_ids,
                "submitted_order_ids": submitted,
                "order_ids": submitted,
                "errors": [item["error"] for item in errors],
                "error_details": errors,
                "source": "pb_active_cancel_all",
            }
        return dict(extra_result or {}) if isinstance(extra_result, dict) else {}

    def _release_entry_reservation_for_cancel(self, order_id: str, rows: list[Dict[str, Any]]) -> dict:
        releaser = getattr(self.reservation_store, "release", None)
        if not callable(releaser):
            return {}
        should_release = not rows or any(str(row.get("role") or "").strip().lower() in {"", "entry"} for row in rows)
        if not should_release:
            return {}
        try:
            result = releaser(entry_order_id=order_id, reason="terminal_cancel_sync")
            return dict(result or {}) if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning("Failed to release buying-power reservation after terminal cancel: %s", exc)
            return {"ok": False, "error": str(exc)}

    def _sync_terminal_cancel_to_pb(self, order_id: str, result: Dict[str, Any] | None) -> dict:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id or not self._is_terminal_cancel_result(result):
            return {}
        if not self.pb_client:
            release_result = self._release_entry_reservation_for_cancel(normalized_order_id, [])
            return {"order_id": normalized_order_id, "updated": 0, "reservation_release": release_result} if release_result else {}

        updater = getattr(self.pb_client, "update_record", None)
        rows = self._load_pb_orders_for_broker_order_id(normalized_order_id)
        open_rows = [row for row in rows if self._pb_order_row_is_open_like(row)]
        terminal_status = self._cancel_result_terminal_status(result)
        updated_ids: list[str] = []
        errors: list[dict] = []
        now_ms = int(time.time() * 1000)
        if callable(updater):
            for row in open_rows:
                record_id = str(row.get("id") or "").strip()
                if not record_id:
                    continue
                extra = self._ensure_object(row.get("extra"))
                extra.update(
                    {
                        "terminal_cancel_sync": True,
                        "terminal_cancel_sync_source": "order_modifier",
                        "terminal_cancel_status": terminal_status,
                        "terminal_cancel_order_id": normalized_order_id,
                        "terminal_cancel_synced_at_ms": now_ms,
                    }
                )
                confirm = (result or {}).get("confirm") if isinstance((result or {}).get("confirm"), dict) else {}
                details = (result or {}).get("details") if isinstance((result or {}).get("details"), dict) else confirm.get("details")
                if isinstance(details, dict) and details:
                    extra["terminal_cancel_details"] = dict(details)
                try:
                    updater(
                        "orders",
                        record_id,
                        {
                            "status": "Canceled",
                            "relation_status": "closed",
                            "extra": extra,
                        },
                    )
                    updated_ids.append(record_id)
                except Exception as exc:
                    errors.append({"id": record_id, "error": str(exc)})
        release_result = self._release_entry_reservation_for_cancel(normalized_order_id, open_rows)
        return {
            "order_id": normalized_order_id,
            "terminal_status": terminal_status,
            "matched": len(rows),
            "open_matched": len(open_rows),
            "updated": len(updated_ids),
            "updated_ids": updated_ids,
            "errors": errors,
            "reservation_release": release_result,
        }

    def _sync_terminal_cancel_all_to_pb(self, result: Dict[str, Any]) -> list[dict]:
        order_ids = [
            str(item or "").strip()
            for item in (
                result.get("submitted_order_ids")
                or result.get("order_ids")
                or result.get("active_order_ids")
                or []
            )
            if str(item or "").strip()
        ]
        if not order_ids:
            return []
        syncs = []
        terminal_result = {"ok": True, "status": "CANCELLED", "confirm": {"status": "CANCELLED"}}
        for order_id in order_ids:
            sync = self._sync_terminal_cancel_to_pb(order_id, terminal_result)
            if sync:
                syncs.append(sync)
        return syncs
