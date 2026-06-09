"""
执行动作处理
- close: 平仓
- cancel: 取消待成交订单
- adjust_sl: 调整止损
- adjust_tp: 调整止盈
- adjust_bracket: 同时或单边调整止损/止盈
"""

import copy
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_compute.core.time_utils import ET
from ibkr_compute.observability.prometheus import record_signal_event

logger = logging.getLogger(__name__)


REVERSE_ACTIONS = {"close", "cancel", "adjust_sl", "adjust_tp", "adjust_bracket"}
REVERSE_SIGNAL_COLLECTION = "ibkr_reverse_signals"
REVERSE_PERSISTED_STATUSES = {"pending", "confirmed", "cancelled", "expired"}
REVERSE_BLOCKED_PERSISTED_STATUS = "cancelled"
TRADINGVIEW_REVERSE_SOURCES = {"tradingview", "tv", "tv_webhook", "webhook_tv"}

ACTIVE_ORDER_STATUSES = {
    "APIPENDING",
    "PENDINGSUBMIT",
    "PENDINGCANCEL",
    "PRESUBMITTED",
    "SUBMITTED",
}
INACTIVE_ORDER_STATUSES = {
    "",
    "API_CANCELLED",
    "CANCELLED",
    "CANCELED",
    "CLOSED",
    "EXECUTED",
    "EXPIRED",
    "FILLED",
    "INACTIVE",
    "REJECTED",
}
REAL_ACTIVE_ORDER_STATUSES = {
    "PRESUBMITTED",
    "PRE_SUBMITTED",
    "SUBMITTED",
    "SUBMITTED_WAITING_FILL",
}
REAL_FILLED_ORDER_STATUSES = {
    "EXECUTED",
    "FILLED",
    "FILLED_POSITION",
    "FILLED_REPRICING_PROTECTION",
    "PARTIALLYFILLED",
    "PARTIALLY_FILLED",
    "PROTECTED_ACTIVE",
    "PROTECTION_REPRICE_FAILED",
}
REAL_ORDER_STATUSES = REAL_ACTIVE_ORDER_STATUSES | REAL_FILLED_ORDER_STATUSES
TRACEABLE_CANCEL_ORDER_STATUSES = REAL_ACTIVE_ORDER_STATUSES | {
    "APIPENDING",
    "API_PENDING",
    "INIT",
    "PENDINGCANCEL",
    "PENDING_CANCEL",
    "PENDINGSUBMIT",
    "PENDING_SUBMIT",
}
REVERSE_CONFIRM_ATTEMPTS = max(1, int(os.environ.get("IBKR_REVERSE_CONFIRM_ATTEMPTS", "3") or "3"))
REVERSE_CONFIRM_POLL_SECONDS = max(0.0, float(os.environ.get("IBKR_REVERSE_CONFIRM_POLL_SECONDS", "0.25") or 0.25))
ADJUST_PRICE_FIELDS_BY_SIDE = {
    "stop_loss": ("auxPrice", "aux_price", "stopPrice", "stop_price", "price"),
    "take_profit": ("price", "lmtPrice", "limitPrice", "limit_price", "takeProfit", "take_profit"),
}
ADJUST_PRICE_MATCH_TOLERANCE = 0.005


class ReverseSignalHandler:
    def __init__(self, pb_client, order_placer=None, order_modifier=None,
                 order_lifecycle=None, signal_processor=None,
                 conid_resolver=None, environment: str = "", config=None):
        self.pb_client = pb_client
        self.order_placer = order_placer
        self.order_modifier = order_modifier
        self.order_lifecycle = order_lifecycle
        self.signal_processor = signal_processor
        self.conid_resolver = conid_resolver
        self.environment = normalize_broker_mode(environment, configured_broker_mode())
        self.config = config or getattr(order_lifecycle, "config", None) or getattr(signal_processor, "config", None)
        self._processed_ids = set()
        self._position_snapshot_alerted = set()

    def check_and_process(self):
        started = time.perf_counter()
        try:
            records = self._load_processable_reverse_records()
            record_signal_event(
                environment=self.environment,
                stage="reverse_poll",
                signal_source="reverse",
                result="ok",
                reason_code="pending_found" if records else "empty",
                duration_s=time.perf_counter() - started,
            )

            for r in records:
                rid = r.get("id", "")
                if rid in self._processed_ids:
                    continue

                if self._is_cancelled_flat_not_confirmed_tv_close_candidate(r):
                    result = self._self_heal_cancelled_flat_not_confirmed_close(r)
                    if result:
                        self._processed_ids.add(rid)
                        try:
                            self._ack_reverse_signal(r, "close", result)
                        except Exception as e:
                            logger.debug("Failed to self-heal cancelled TV close action: %s", e)
                    continue

                if not self._is_tradingview_reverse_signal(r):
                    result = self._expire_non_tv_reverse_signal(r)
                    self._processed_ids.add(rid)
                    try:
                        self._ack_reverse_signal(r, "expire_non_tv_action", result)
                    except Exception as e:
                        logger.debug("Failed to expire non-TV execution action: %s", e)
                    continue

                action = str(r.get("action_type", "") or "").lower()
                if action not in REVERSE_ACTIONS:
                    continue

                result = self._process_reverse(r, action)
                if not self._is_retryable_result(result):
                    self._processed_ids.add(rid)

                try:
                    self._ack_reverse_signal(r, action, result)
                except Exception as e:
                    logger.debug("Failed to update execution action status: %s", e)

        except Exception as e:
            record_signal_event(
                environment=self.environment,
                stage="reverse_poll",
                signal_source="reverse",
                result="error",
                reason_code=e.__class__.__name__,
                duration_s=time.perf_counter() - started,
            )
            logger.error("Reverse signal check failed: %s", e)

    def _load_processable_reverse_records(self) -> List[Dict[str, Any]]:
        pending = self.pb_client.get_records(
            REVERSE_SIGNAL_COLLECTION,
            filter=f'status = "pending" && environment = "{self.environment}"',
            sort="-priority,-bar_time_ms",
            per_page=50,
        )
        records = [dict(row) for row in (pending or [])]
        try:
            cancelled = self.pb_client.get_records(
                REVERSE_SIGNAL_COLLECTION,
                filter=f'status = "cancelled" && environment = "{self.environment}"',
                sort="-processed_time,-bar_time_ms",
                per_page=100,
            )
        except Exception as exc:
            logger.debug("Failed to load cancelled reverse self-heal candidates: %s", exc)
            cancelled = []

        seen = {str(row.get("id") or "") for row in records}
        for row in cancelled or []:
            rid = str(row.get("id") or "")
            if rid in seen or not self._is_cancelled_flat_not_confirmed_tv_close_candidate(row):
                continue
            records.append(dict(row))
            seen.add(rid)
        return records

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(ET).isoformat()

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return dict(parsed)
            except Exception:
                return {}
        return {}

    @classmethod
    def _signal_extra(cls, signal: dict) -> Dict[str, Any]:
        return cls._as_dict((signal or {}).get("extra"))

    @classmethod
    def _signal_value(cls, signal: dict, key: str, default: Any = "") -> Any:
        if not isinstance(signal, dict):
            return default
        value = signal.get(key)
        if value not in (None, "", []):
            return value
        extra = cls._signal_extra(signal)
        value = extra.get(key)
        return default if value in (None, "") else value

    @classmethod
    def _is_tradingview_reverse_signal(cls, signal: dict) -> bool:
        source = str(cls._signal_value(signal, "source") or "").strip().lower()
        return source in TRADINGVIEW_REVERSE_SOURCES

    @staticmethod
    def _expire_non_tv_reverse_signal(signal: dict) -> Dict[str, Any]:
        source = str((signal or {}).get("source") or "").strip().lower()
        extra = ReverseSignalHandler._as_dict((signal or {}).get("extra"))
        if not source:
            source = str(extra.get("source") or "").strip().lower()
        return {
            "ok": False,
            "ack_status": "expired",
            "reason": "legacy_non_tv_action_disabled",
            "detail": {
                "result_status": "expired_non_tv_action",
                "flow_error_code": "tv_action_non_tv_source",
                "source": source,
                "tv_primary_only": True,
            },
        }

    @classmethod
    def _signal_list(cls, signal: dict, *keys: str) -> List[str]:
        values: List[str] = []
        extra = cls._signal_extra(signal)
        for key in keys:
            for source in (signal, extra):
                raw = source.get(key) if isinstance(source, dict) else None
                if raw is None:
                    continue
                if isinstance(raw, str):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list):
                        values.extend(str(item or "").strip() for item in parsed)
                    else:
                        values.extend(part.strip() for part in stripped.split(","))
                elif isinstance(raw, (list, tuple, set)):
                    values.extend(str(item or "").strip() for item in raw)
                else:
                    values.append(str(raw or "").strip())
        return cls._unique_nonempty(values)

    @staticmethod
    def _unique_nonempty(values: List[Any]) -> List[str]:
        seen = set()
        unique: List[str] = []
        for value in values or []:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            unique.append(text)
        return unique

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _is_retryable_result(result: Dict[str, Any]) -> bool:
        return str((result or {}).get("ack_status") or "").strip().lower() == "pending"

    @classmethod
    def _close_submission_unconfirmed_fields(cls, result: Dict[str, Any]) -> Tuple[bool, List[str], str]:
        if not isinstance(result, dict):
            return False, [], ""
        error = str(result.get("error") or "").strip().lower()
        order_ids = cls._signal_list(
            result,
            "order_ids",
            "submitted_order_ids",
            "broker_order_ids",
            "submitted_broker_order_ids",
        )
        order_ref = str(
            result.get("entry_coid")
            or result.get("bracket_group")
            or result.get("order_ref")
            or result.get("orderRef")
            or result.get("trade_group_id")
            or ""
        ).strip()
        return "order_submission_unconfirmed" in error, order_ids, order_ref

    @staticmethod
    def _order_status(order: Dict[str, Any]) -> str:
        return str(
            order.get("status")
            or order.get("order_status")
            or order.get("orderStatus")
            or order.get("state")
            or ""
        ).strip()

    @staticmethod
    def _status_key(value: Any) -> str:
        return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")

    @classmethod
    def _order_status_key(cls, order: Dict[str, Any]) -> str:
        return cls._status_key(cls._order_status(order))

    @classmethod
    def _is_active_order_status(cls, status: Any) -> bool:
        text = str(status or "").strip().upper()
        if text in INACTIVE_ORDER_STATUSES:
            return False
        if text in ACTIVE_ORDER_STATUSES:
            return True
        return bool(text)

    @classmethod
    def _is_active_order(cls, order: Dict[str, Any]) -> bool:
        return cls._is_active_order_status(cls._order_status(order))

    @staticmethod
    def _order_id(order: Dict[str, Any]) -> str:
        return str(
            order.get("broker_order_id")
            or order.get("order_id")
            or order.get("orderId")
            or order.get("id")
            or ""
        ).strip()

    @staticmethod
    def _order_trade_group(order: Dict[str, Any]) -> str:
        extra = ReverseSignalHandler._as_dict(order.get("extra"))
        return str(
            order.get("trade_group_id")
            or order.get("bracket_group")
            or extra.get("trade_group_id")
            or extra.get("bracket_group")
            or ""
        ).strip()

    @staticmethod
    def _order_ref_values(order: Dict[str, Any]) -> List[str]:
        extra = ReverseSignalHandler._as_dict(order.get("extra"))
        keys = (
            "broker_order_id",
            "order_id",
            "orderId",
            "id",
            "unique_id",
            "entry_order_unique_id",
            "parent_order_unique_id",
            "cOID",
            "coid",
            "orderRef",
            "order_ref",
        )
        refs = []
        for key in keys:
            refs.append(str(order.get(key) or "").strip())
            refs.append(str(extra.get(key) or "").strip())
        return ReverseSignalHandler._unique_nonempty(refs)

    @staticmethod
    def _base_reverse_detail(signal: dict, action: str) -> Dict[str, Any]:
        extra = ReverseSignalHandler._signal_extra(signal)
        current_direction = str(
            ReverseSignalHandler._signal_value(signal, "current_direction")
            or ReverseSignalHandler._signal_value(signal, "direction")
            or ""
        ).strip().lower()
        new_direction = str(ReverseSignalHandler._signal_value(signal, "new_direction") or "").strip().lower()
        return {
            "executed_action": action,
            "result_status": "started",
            "reverse_state": "started",
            "reverse_state_path": [],
            "cancel_old_order": "skipped",
            "close_old_position": "skipped",
            "wait_flat": "skipped",
            "cooldown": "skipped",
            "ready_reentry": False,
            "reentry_submitted": False,
            "blocked": False,
            "reentry_blocked": {},
            "reverse_runtime_detail": {
                "version": 1,
                "source": "ibkr_compute.reverse_signal",
            },
            "symbol": str(ReverseSignalHandler._signal_value(signal, "symbol") or "").strip().upper(),
            "current_direction": current_direction,
            "new_direction": new_direction,
            "trade_group_id": str(ReverseSignalHandler._signal_value(signal, "trade_group_id") or "").strip(),
            "entry_order_unique_id": str(ReverseSignalHandler._signal_value(signal, "entry_order_unique_id") or "").strip(),
            "order_unique_id": str(ReverseSignalHandler._signal_value(signal, "order_unique_id") or "").strip(),
            "broker_order_id": str(ReverseSignalHandler._signal_value(signal, "broker_order_id") or "").strip(),
            "quantity": ReverseSignalHandler._coerce_float(
                ReverseSignalHandler._signal_value(signal, "quantity", extra.get("shares", 0)),
                0.0,
            ),
        }

    @staticmethod
    def _append_state(detail: Dict[str, Any], state: str) -> None:
        path = detail.setdefault("reverse_state_path", [])
        if state and (not path or path[-1] != state):
            path.append(state)
        if state:
            detail["reverse_state"] = state

    @classmethod
    def _mark_blocked(cls, detail: Dict[str, Any], reason: str, **context: Any) -> Dict[str, Any]:
        cls._append_state(detail, "blocked")
        detail["blocked"] = True
        detail["ready_reentry"] = False
        detail["reentry_submitted"] = False
        detail["result_status"] = "reentry_blocked"
        block_detail = {"reason": str(reason or "blocked")}
        block_detail.update({key: value for key, value in context.items() if value not in (None, "")})
        detail["reentry_blocked"] = block_detail
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        runtime_detail["blocked_reason"] = block_detail["reason"]
        if context:
            runtime_detail["blocked_context"] = dict(context)
        return {
            "ok": False,
            "ack_status": "blocked",
            "reason": f"reverse blocked: {block_detail['reason']}",
            "detail": detail,
        }

    @classmethod
    def _mark_retryable_blocked(cls, detail: Dict[str, Any], reason: str, **context: Any) -> Dict[str, Any]:
        cls._append_state(detail, "pending_retry")
        detail["blocked"] = True
        detail["ready_reentry"] = False
        detail["reentry_submitted"] = False
        detail["result_status"] = "pending_retry"
        block_detail = {"reason": str(reason or "pending_retry"), "retryable": True}
        block_detail.update({key: value for key, value in context.items() if value not in (None, "")})
        detail["reentry_blocked"] = block_detail
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        runtime_detail["blocked_reason"] = block_detail["reason"]
        runtime_detail["retryable"] = True
        if context:
            runtime_detail["blocked_context"] = dict(context)
        return {
            "ok": False,
            "ack_status": "pending",
            "reason": f"reverse pending retry: {block_detail['reason']}",
            "detail": detail,
        }

    @classmethod
    def _mark_invalidated(
        cls,
        detail: Dict[str, Any],
        reason: str,
        *,
        ack_status: str = "expired",
        **context: Any,
    ) -> Dict[str, Any]:
        cls._append_state(detail, "invalidated")
        invalid_reason = str(reason or "real_order_preflight_failed")
        detail["blocked"] = False
        detail["ready_reentry"] = False
        detail["reentry_submitted"] = False
        detail["result_status"] = "invalidated"
        detail["invalidated_by"] = "real_order_preflight"
        detail["invalidated_reason"] = invalid_reason
        detail["gateway_request_blocked"] = True
        detail["execution_readiness"] = "not_executable"
        detail["execution_blocked_reason"] = invalid_reason
        detail["execution_blocked_message"] = (
            "Execution action invalidated because no confirmed real broker order/position "
            "was available for the requested follow-up action."
        )
        if context.get("order_linkage_status"):
            detail["order_linkage_status"] = context.get("order_linkage_status")
        if context:
            detail["invalidation_context"] = {
                key: value for key, value in context.items() if value not in (None, "")
            }
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        runtime_detail.update(
            {
                "invalidated_by": "real_order_preflight",
                "invalidated_reason": invalid_reason,
                "gateway_request_blocked": True,
                "execution_readiness": "not_executable",
            }
        )
        if context:
            runtime_detail["invalidation_context"] = dict(detail.get("invalidation_context") or {})
        return {
            "ok": False,
            "ack_status": ack_status if ack_status in REVERSE_PERSISTED_STATUSES else "expired",
            "reason": f"execution invalidated: {invalid_reason}",
            "detail": detail,
        }

    @classmethod
    def _annotate_execution_blocked(
        cls,
        detail: Dict[str, Any],
        *,
        readiness: str,
        reason: str,
        message: str,
        order_linkage_status: str,
        gateway_request_blocked: bool = True,
        **context: Any,
    ) -> None:
        detail["execution_readiness"] = readiness
        detail["execution_blocked_reason"] = reason
        detail["execution_blocked_message"] = message
        detail["order_linkage_status"] = order_linkage_status
        detail["gateway_request_blocked"] = bool(gateway_request_blocked)
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        runtime_detail.update(
            {
                "execution_readiness": readiness,
                "execution_blocked_reason": reason,
                "execution_blocked_message": message,
                "order_linkage_status": order_linkage_status,
                "gateway_request_blocked": bool(gateway_request_blocked),
            }
        )
        if context:
            runtime_detail["execution_blocked_context"] = {
                key: value for key, value in context.items() if value not in (None, "")
            }

    def _mark_ready_reentry(self, signal: dict, detail: Dict[str, Any], reason: str = "ready_reentry") -> Dict[str, Any]:
        if self._is_tradingview_reverse_signal(signal):
            return self._mark_tv_exit_confirmed(signal, detail, reason)

        self._append_state(detail, "ready_reentry")
        detail["ready_reentry"] = True
        detail["reentry_submitted"] = False
        detail["blocked"] = False
        detail["result_status"] = "ready_reentry"
        detail["reentry_blocked"] = {
            "reason": "reentry_payload_missing",
            "next_step": "manual_review_or_api_ingest_reentry",
            "message": "old order/position is safe; no reentry payload was available",
        }
        detail.setdefault("reverse_runtime_detail", {})["ready_reason"] = reason
        submit_result = self._submit_reentry_signal(signal, detail)
        if submit_result.get("submitted"):
            self._append_state(detail, "reentry_submitted")
            detail["reentry_submitted"] = True
            detail["result_status"] = "reentry_submitted"
            detail["reentry_blocked"] = {}
            detail["reentry_signal_id"] = submit_result.get("signal_id") or ""
            detail["reentry_record_id"] = submit_result.get("record_id") or ""
            detail.setdefault("reverse_runtime_detail", {})["reentry_submit_action"] = submit_result.get("action") or ""
            return {
                "ok": True,
                "ack_status": "confirmed",
                "reason": "reentry_submitted",
                "detail": detail,
            }
        if submit_result.get("blocked"):
            return self._mark_blocked(
                detail,
                submit_result.get("reason") or "reentry_signal_submit_failed",
                error=submit_result.get("error") or "",
            )
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": reason,
            "detail": detail,
        }

    def _mark_tv_exit_confirmed(self, signal: dict, detail: Dict[str, Any], reason: str) -> Dict[str, Any]:
        self._append_state(detail, "tv_exit_confirmed")
        detail["ready_reentry"] = False
        detail["reentry_submitted"] = False
        detail["blocked"] = False
        detail["result_status"] = "tv_exit_confirmed"
        detail["auto_reentry_disabled"] = True
        detail["reentry_blocked"] = {
            "reason": "tv_primary_exit_requires_next_tv_entry",
            "next_step": "wait_for_tradingview_entry",
            "message": "old order/position is safe; automatic reentry is disabled in TV-primary mode",
        }
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        runtime_detail["ready_reason"] = reason
        runtime_detail["auto_reentry_disabled"] = True
        runtime_detail["next_step"] = "wait_for_tradingview_entry"
        self._mark_origin_signal_tv_exit_resolved(signal, detail)
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": "tv_exit_confirmed_no_reentry",
            "detail": detail,
        }

    def _submit_reentry_signal(self, signal: dict, detail: Dict[str, Any]) -> Dict[str, Any]:
        if self._is_tradingview_reverse_signal(signal):
            return {"submitted": False, "reason": "tv_primary_auto_reentry_disabled"}
        payload = self._reentry_signal_payload(signal)
        if not payload:
            return {"submitted": False, "reason": "reentry_payload_missing"}

        self._mark_origin_signal_resolved(signal, detail)
        signal_id = str(payload.get("signal_id") or "").strip()
        environment = normalize_broker_mode(payload.get("broker_mode") or payload.get("environment"), self.environment)
        if not signal_id:
            return {"submitted": False, "blocked": True, "reason": "reentry_signal_id_missing"}

        prepared = dict(payload)
        extra = self._as_dict(prepared.get("extra"))
        prepared["environment"] = environment
        prepared["status"] = "pending"
        prepared["note"] = str(prepared.get("note") or "reverse_reentry_after_old_risk_resolved")
        prepared["extra"] = {
            **extra,
            "environment": environment,
            "reverse_reentry": True,
            "reverse_policy": "full_auto_reverse",
            "reverse_stage": "reentry_submitted",
            "reverse_id": str(signal.get("id") or ""),
            "reverse_signal_id": str(signal.get("id") or ""),
            "status_reason": "reverse_reentry_after_old_risk_resolved",
            "signal_confirmation_required": False,
            "signal_confirmation_mode": "auto_reverse",
            "auto_reentry_submitted_at": self._now_iso(),
            "old_trade_group_id": detail.get("trade_group_id") or "",
        }

        getter = getattr(self.pb_client, "get_first_record", None)
        updater = getattr(self.pb_client, "update_record", None)
        creator = getattr(self.pb_client, "create_record", None)
        try:
            existing = None
            if callable(getter):
                existing = getter(
                    "ibkr_signals",
                    filter=(
                        f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                        f'environment = "{self._escape_filter_value(environment)}"'
                    ),
                )
            if existing and existing.get("id") and callable(updater):
                existing_extra = self._as_dict(existing.get("extra"))
                prepared["extra"] = {**existing_extra, **prepared["extra"]}
                updated = updater("ibkr_signals", str(existing.get("id")), prepared)
                return {
                    "submitted": True,
                    "action": "updated",
                    "signal_id": signal_id,
                    "record_id": str((updated or existing).get("id") or ""),
                }
            if callable(creator):
                created = creator("ibkr_signals", prepared)
                return {
                    "submitted": True,
                    "action": "created",
                    "signal_id": signal_id,
                    "record_id": str((created or {}).get("id") or ""),
                }
        except Exception as exc:
            return {
                "submitted": False,
                "blocked": True,
                "reason": "reentry_signal_submit_failed",
                "error": str(exc),
            }
        return {"submitted": False, "reason": "pb_signal_write_unavailable"}

    def _reentry_signal_payload(self, signal: dict) -> Dict[str, Any]:
        payload = self._signal_value(signal, "reentry_signal_payload", {})
        return self._as_dict(payload)

    def _mark_origin_signal_tv_exit_resolved(self, signal: dict, detail: Dict[str, Any]) -> None:
        origin_signal_id = str(
            self._signal_value(signal, "origin_signal_id")
            or self._signal_value(signal, "signal_id")
            or self._signal_value(signal, "source_signal_id")
            or ""
        ).strip()
        if not origin_signal_id:
            return
        getter = getattr(self.pb_client, "get_first_record", None)
        updater = getattr(self.pb_client, "update_record", None)
        if not callable(getter) or not callable(updater):
            return
        environment = normalize_broker_mode(self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"), self.environment)
        try:
            record = getter(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{self._escape_filter_value(origin_signal_id)}" && '
                    f'environment = "{self._escape_filter_value(environment)}"'
                ),
            )
            if not record or not record.get("id"):
                return
            extra = self._as_dict(record.get("extra"))
            action = str(detail.get("executed_action") or signal.get("action_type") or "").strip()
            status_reason = f"closed_by_tv_execution_action_{action}" if action else "closed_by_tv_execution_action"
            patch = {
                "status": "closed",
                "note": status_reason,
                "extra": {
                    **extra,
                    "status_reason": status_reason,
                    "execution_policy": "tv_exit_required",
                    "execution_stage": "old_risk_resolved",
                    "execution_action_id": str(signal.get("id") or ""),
                    "execution_action_resolved_at": self._now_iso(),
                    "next_entry_source": "tradingview",
                },
            }
            updater("ibkr_signals", str(record.get("id")), patch)
            self._demote_origin_target_after_close(
                record,
                origin_signal_id=origin_signal_id,
                environment=environment,
                reason=status_reason,
                detail=detail,
            )
            detail["origin_signal_status_patch"] = {
                "signal_id": origin_signal_id,
                "status": "closed",
                "note": patch["note"],
            }
        except Exception as exc:
            detail["origin_signal_status_patch_error"] = str(exc)

    def _demote_origin_target_after_close(
        self,
        record: dict,
        *,
        origin_signal_id: str,
        environment: str,
        reason: str,
        detail: Dict[str, Any],
    ) -> None:
        try:
            from ibkr_compute.universe.target_lifecycle import demote_entry_activated_targets_after_close

            symbol = str(record.get("symbol") or "").strip().upper()
            result = demote_entry_activated_targets_after_close(
                self.pb_client,
                symbol=symbol,
                environment=str(record.get("environment") or environment or self.environment),
                signal_id=origin_signal_id,
                dates=[record.get("date"), datetime.now(ET).strftime("%Y-%m-%d")],
                reason=reason,
                now_iso=self._now_iso(),
                logger=logger,
            )
            detail["origin_target_deactivation"] = result
        except Exception as exc:
            detail["origin_target_deactivation_error"] = str(exc)

    def _mark_origin_signal_resolved(self, signal: dict, detail: Dict[str, Any]) -> None:
        origin_signal_id = str(
            self._signal_value(signal, "origin_signal_id")
            or self._signal_value(signal, "signal_id")
            or self._signal_value(signal, "source_signal_id")
            or ""
        ).strip()
        if not origin_signal_id:
            return
        getter = getattr(self.pb_client, "get_first_record", None)
        updater = getattr(self.pb_client, "update_record", None)
        if not callable(getter) or not callable(updater):
            return
        environment = normalize_broker_mode(self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"), self.environment)
        try:
            record = getter(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{self._escape_filter_value(origin_signal_id)}" && '
                    f'environment = "{self._escape_filter_value(environment)}"'
                ),
            )
            if not record or not record.get("id"):
                return
            extra = self._as_dict(record.get("extra"))
            action = str(detail.get("executed_action") or "").strip() or str(signal.get("action_type") or "")
            patch = {
                "status": "closed",
                "note": f"closed_by_reverse_{action}" if action else "closed_by_reverse_signal",
                "extra": {
                    **extra,
                    "status_reason": f"closed_by_reverse_{action}" if action else "closed_by_reverse_signal",
                    "reverse_policy": "full_auto_reverse",
                    "reverse_stage": "old_risk_resolved",
                    "reverse_id": str(signal.get("id") or ""),
                    "reverse_resolved_at": self._now_iso(),
                    "reentry_signal_id": str(self._reentry_signal_payload(signal).get("signal_id") or ""),
                },
            }
            updater("ibkr_signals", str(record.get("id")), patch)
            self._demote_origin_target_after_close(
                record,
                origin_signal_id=origin_signal_id,
                environment=environment,
                reason=str(patch.get("note") or "closed_by_reverse_signal"),
                detail=detail,
            )
            detail["origin_signal_status_patch"] = {
                "signal_id": origin_signal_id,
                "status": "closed",
                "note": patch["note"],
            }
        except Exception as exc:
            detail["origin_signal_status_patch_error"] = str(exc)

    def _ack_reverse_signal(self, signal: dict, action: str, result: Dict[str, Any]) -> None:
        rid = str(signal.get("id", "") or "").strip()
        if not rid:
            return
        detail = dict(result.get("detail") or {})
        detail.setdefault("executed_action", action)
        detail.setdefault("result_status", "ok" if result.get("ok") else "failed")
        raw_status = str(result.get("ack_status") or ("confirmed" if result.get("ok") else "blocked")).strip()
        status = self._persistable_ack_status(raw_status, detail)
        reason = str(
            result.get("reason")
            or (
                f"ibkr_compute 自动执行 {action} 成功"
                if result.get("ok") else
                f"ibkr_compute 自动执行 {action} 被阻止"
            )
        )

        self._patch_reverse_signal_detail(signal, rid, status, reason, detail)

        ack = getattr(self.pb_client, "ack_ibkr_reverse_signal", None)
        if callable(ack):
            ack(
                rid,
                status=status,
                reason=reason,
                detail=detail,
            )

    @staticmethod
    def _persistable_ack_status(status: str, detail: Dict[str, Any]) -> str:
        normalized = str(status or "").strip().lower()
        if not normalized:
            normalized = "confirmed"
        if normalized in REVERSE_PERSISTED_STATUSES:
            return normalized

        persisted = REVERSE_BLOCKED_PERSISTED_STATUS if normalized == "blocked" else "cancelled"
        detail["ack_status_original"] = normalized
        detail["ack_status_normalized"] = persisted
        runtime_detail = detail.setdefault("reverse_runtime_detail", {})
        if isinstance(runtime_detail, dict):
            runtime_detail["ack_status_original"] = normalized
            runtime_detail["ack_status_normalized"] = persisted
        return persisted

    def _patch_reverse_signal_detail(
        self,
        signal: dict,
        rid: str,
        status: str,
        reason: str,
        detail: Dict[str, Any],
    ) -> None:
        updater = getattr(self.pb_client, "update_record", None)
        if not callable(updater):
            return
        try:
            extra = self._signal_extra(signal)
            extra.update(detail)
            patch = {
                "status": status,
                "reason": reason,
                "processed_time": self._now_iso(),
                "extra": extra,
            }
            updated = updater(REVERSE_SIGNAL_COLLECTION, rid, patch)
            if isinstance(updated, dict):
                signal.update(updated)
            else:
                signal.update(patch)
        except Exception as exc:
            logger.debug("Failed to patch execution action detail: %s", exc)

    def _process_reverse(self, signal: dict, action: str) -> Dict[str, Any]:
        started = time.perf_counter()
        symbol = str(self._signal_value(signal, "symbol") or "").upper()
        logger.info("Processing execution action: %s %s", action, symbol)

        detail = self._base_reverse_detail(signal, action)

        def _finish(payload: Dict[str, Any]) -> Dict[str, Any]:
            ok = bool((payload or {}).get("ok"))
            reason = str((payload or {}).get("reason") or ("ok" if ok else "blocked"))
            ack_status = str((payload or {}).get("ack_status") or "")
            result = "ok" if ok else ("blocked" if ack_status in {"cancelled", "expired"} else "error")
            record_signal_event(
                environment=self.environment,
                stage="reverse_action",
                signal_source=str(action or "unknown"),
                result=result,
                reason_code=reason,
                duration_s=time.perf_counter() - started,
            )
            return payload

        if self._is_tradingview_reverse_signal(signal):
            self_heal_result = self._attempt_tv_async_self_heal(signal, action, detail)
            if self_heal_result is not None:
                return _finish(self_heal_result)

        if self._has_protection_incomplete(signal):
            return _finish(self._mark_blocked(
                detail,
                "protection_incomplete",
                protection_complete=False,
                safe_action="no_reentry_until_protection_reviewed",
            ))

        if action in {"close", "cancel", "adjust_sl", "adjust_tp"}:
            preflight = self._execution_preflight(signal, action)
            detail["execution_preflight"] = preflight
            if not preflight.get("ok"):
                return _finish(self._mark_invalidated(
                    detail,
                    str(preflight.get("reason") or "real_order_preflight_failed"),
                    ack_status=str(preflight.get("ack_status") or "expired"),
                    action=action,
                    real_order_required=True,
                    real_order_confirmed=preflight.get("real_order_confirmed"),
                    filled_order_or_position_confirmed=preflight.get("filled_order_or_position_confirmed"),
                    order_ids=preflight.get("order_ids"),
                    trade_group_id=preflight.get("trade_group_id"),
                    origin_signal_id=preflight.get("origin_signal_id"),
                    origin_execution_status=preflight.get("origin_execution_status"),
                    order_linkage_status=preflight.get("order_linkage_status"),
                    pb_order_statuses=preflight.get("pb_order_statuses"),
                    gateway_request_blocked=True,
                ))

        if action == "close":
            return _finish(self._handle_close(signal, detail))
        if action == "cancel":
            return _finish(self._handle_cancel(signal, detail))
        if action == "adjust_sl":
            return _finish(self._handle_adjust_sl(signal, detail))
        if action == "adjust_tp":
            return _finish(self._handle_adjust_tp(signal, detail))
        if action == "adjust_bracket":
            return _finish(self._handle_adjust_bracket(signal, detail))

        return _finish(self._mark_blocked(detail, "unsupported_reverse_action", action=action))

    def _has_protection_incomplete(self, signal: dict) -> bool:
        extra = self._signal_extra(signal)
        status_values = {
            str(signal.get("status") or "").strip().lower(),
            str(extra.get("status") or "").strip().lower(),
            str(extra.get("relation_status") or "").strip().lower(),
            str(extra.get("target_state") or "").strip().lower(),
            str(extra.get("target_signal_status") or "").strip().lower(),
        }
        if status_values & {"protection_incomplete", "protection_reprice_failed"}:
            return True
        for source in (signal, extra):
            if bool(source.get("protection_incomplete")):
                return True
            if "protection_complete" in source and source.get("protection_complete") is False:
                return True
        return False

    def _execution_order_ids_from_signal(self, signal: dict) -> List[str]:
        values = list(self._related_order_ids_from_signal(signal))
        values.extend(
            self._signal_value(signal, key)
            for key in (
                "sl_order_id",
                "stop_loss_order_id",
                "stop_order_id",
                "tp_order_id",
                "take_profit_order_id",
                "profit_target_order_id",
            )
        )
        return self._unique_nonempty(values)

    def _requested_child_order_ids(self, signal: dict, action: str) -> List[str]:
        keys: Tuple[str, ...]
        if action == "adjust_sl":
            keys = self._side_order_id_keys("stop_loss")
        elif action == "adjust_tp":
            keys = self._side_order_id_keys("take_profit")
        elif action == "adjust_bracket":
            explicit_sides, has_explicit_sides, _side_source = self._requested_adjust_sides(signal)
            if has_explicit_sides:
                requested_sides = set(explicit_sides)
            else:
                requested_sides = set()
                if self._signal_has_value(signal, "new_sl"):
                    requested_sides.add("stop_loss")
                if self._signal_has_value(signal, "new_tp"):
                    requested_sides.add("take_profit")
                if not requested_sides:
                    requested_sides.update({"stop_loss", "take_profit"})
            selected_keys: List[str] = []
            if "stop_loss" in requested_sides:
                selected_keys.extend(self._side_order_id_keys("stop_loss"))
            if "take_profit" in requested_sides:
                selected_keys.extend(self._side_order_id_keys("take_profit"))
            keys = tuple(selected_keys)
        else:
            keys = ()
        return self._unique_nonempty([self._signal_value(signal, key) for key in keys])

    def _origin_execution_status_key(self, origin: Dict[str, Any], execution_payload: Dict[str, Any]) -> str:
        origin_extra = self._as_dict((origin or {}).get("extra"))
        return self._status_key(
            (execution_payload or {}).get("status")
            or (execution_payload or {}).get("order_status")
            or (execution_payload or {}).get("orderStatus")
            or origin_extra.get("last_execution_status")
            or origin_extra.get("status")
            or (origin or {}).get("status")
        )

    def _signal_execution_status_keys(self, signal: dict) -> set[str]:
        keys = {
            self._status_key(self._signal_value(signal, key))
            for key in (
                "target_order_status",
                "order_status",
                "origin_execution_status",
                "execution_status",
                "last_execution_status",
                "target_state",
                "relation_status",
            )
        }
        return {key for key in keys if key}

    @staticmethod
    def _orders_summary(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "id": str(order.get("id") or ""),
                "order_id": ReverseSignalHandler._order_id(order),
                "status": ReverseSignalHandler._order_status(order),
                "role": str(order.get("role") or ReverseSignalHandler._as_dict(order.get("extra")).get("role") or ""),
            }
            for order in orders or []
        ]

    def _execution_preflight(self, signal: dict, action: str) -> Dict[str, Any]:
        started = time.perf_counter()
        runtime_environment = normalize_broker_mode(
            self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"),
            self.environment,
        )
        trade_group_id = self._related_trade_group_id(signal)
        requested_child_order_ids = self._requested_child_order_ids(signal, action)
        order_ids = self._unique_nonempty(self._execution_order_ids_from_signal(signal) + requested_child_order_ids)
        related_orders = self._fetch_related_orders(
            trade_group_id=trade_group_id,
            order_ids=order_ids,
            environment=runtime_environment,
        ) if (trade_group_id or order_ids) else []
        origin = self._origin_signal_record(signal)
        execution_payload, execution_mode = self._origin_execution_payload(origin, runtime_environment)
        origin_status_key = self._origin_execution_status_key(origin, execution_payload)
        signal_status_keys = self._signal_execution_status_keys(signal)
        order_status_keys = {self._order_status_key(order) for order in related_orders}
        traceable_order_ids = self._unique_nonempty(order_ids + [self._order_id(order) for order in related_orders])
        real_active_orders = [
            order for order in related_orders if self._order_status_key(order) in REAL_ACTIVE_ORDER_STATUSES
        ]
        real_filled_orders = [
            order for order in related_orders if self._order_status_key(order) in REAL_FILLED_ORDER_STATUSES
        ]
        traceable_cancel_orders = [
            order for order in related_orders if self._order_status_key(order) in TRACEABLE_CANCEL_ORDER_STATUSES
        ]
        origin_real = origin_status_key in REAL_ORDER_STATUSES
        origin_terminal = self._origin_execution_terminal(origin, execution_payload)
        has_local_linkage = bool(trade_group_id or order_ids or related_orders or origin)
        signal_claims_filled = bool(
            (signal_status_keys & REAL_FILLED_ORDER_STATUSES) or "FILLED_POSITION" in signal_status_keys
        ) and has_local_linkage
        has_filled_state = bool(
            real_filled_orders
            or origin_status_key in REAL_FILLED_ORDER_STATUSES
            or signal_claims_filled
        )
        has_real_order = bool(
            real_active_orders
            or real_filled_orders
            or origin_real
            or (signal_status_keys & REAL_ORDER_STATUSES)
        )
        has_traceable_cancel_target = bool(
            traceable_order_ids
            or traceable_cancel_orders
            or (origin_real and (trade_group_id or order_ids))
        )
        explicit_sides, has_explicit_sides, _side_source = self._requested_adjust_sides(signal)
        adjust_noop = action == "adjust_bracket" and has_explicit_sides and not explicit_sides
        origin_child_order_ids = self._unique_nonempty(
            [
                self._child_order_id_from_payload(execution_payload, "stop_loss"),
                self._child_order_id_from_payload(execution_payload, "take_profit"),
            ]
        )
        requested_child_id_set = set(requested_child_order_ids)
        verified_child_order_ids = set()
        if requested_child_id_set:
            for order in real_active_orders:
                refs = set(self._order_ref_values(order))
                if requested_child_id_set & refs:
                    verified_child_order_ids.update(requested_child_id_set & refs)
            if origin_real:
                verified_child_order_ids.update(requested_child_id_set & set(origin_child_order_ids))
        child_order_ids_verified = bool(
            requested_child_order_ids
            and requested_child_id_set.issubset(verified_child_order_ids)
        )
        if requested_child_order_ids:
            has_adjust_target = child_order_ids_verified
        else:
            has_adjust_target = bool(
                adjust_noop and origin_real
                or real_active_orders
                or origin_real
            )

        preflight = {
            "ok": False,
            "action": action,
            "real_order_required": True,
            "real_order_confirmed": has_real_order,
            "filled_order_or_position_confirmed": has_filled_state,
            "broker_mode": runtime_environment,
            "trade_group_id": trade_group_id,
            "order_ids": traceable_order_ids,
            "requested_child_order_ids": requested_child_order_ids,
            "verified_child_order_ids": sorted(verified_child_order_ids),
            "origin_child_order_ids": origin_child_order_ids,
            "adjust_noop": adjust_noop,
            "pb_order_count": len(related_orders),
            "pb_order_statuses": sorted(order_status_keys),
            "pb_orders": self._orders_summary(related_orders),
            "origin_signal_id": self._origin_signal_id(signal),
            "origin_signal_found": bool(origin),
            "origin_execution_mode": execution_mode,
            "origin_execution_status": origin_status_key,
            "origin_execution_terminal": origin_terminal,
            "gateway_request_blocked": False,
            "reason": "ok",
        }

        if action == "close":
            preflight["ok"] = has_filled_state
            if not preflight["ok"]:
                if origin_terminal:
                    preflight["reason"] = "tv_exit_origin_order_not_active" if self._is_tv_exit_signal(signal) else "origin_order_not_active"
                    preflight["ack_status"] = "cancelled"
                    preflight["order_linkage_status"] = "origin_order_not_active"
                else:
                    preflight["reason"] = "real_filled_order_required_for_close"
                    preflight["ack_status"] = "expired"
        elif action == "cancel":
            preflight["ok"] = has_traceable_cancel_target
            if not preflight["ok"]:
                preflight["reason"] = "traceable_order_required_for_cancel"
                preflight["ack_status"] = "expired"
        elif action in {"adjust_sl", "adjust_tp", "adjust_bracket"}:
            preflight["ok"] = has_adjust_target
            if not preflight["ok"]:
                if origin_terminal:
                    preflight["reason"] = (
                        "risk_update_origin_order_not_active"
                        if self._is_tv_risk_update_signal(signal)
                        else "origin_order_not_active"
                    )
                    preflight["ack_status"] = "cancelled"
                    preflight["order_linkage_status"] = "origin_order_not_active"
                else:
                    preflight["reason"] = "real_child_order_required_for_adjust"
                    preflight["ack_status"] = "expired"
        else:
            preflight["reason"] = "unsupported_reverse_action"
            preflight["ack_status"] = "expired"

        if not preflight["ok"]:
            preflight["gateway_request_blocked"] = True
        record_signal_event(
            environment=self.environment,
            stage="reverse_preflight",
            signal_source=str(action or "unknown"),
            result="ok" if preflight.get("ok") else "blocked",
            reason_code=str(preflight.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        return preflight

    def _handle_close(self, signal: dict, detail: Dict[str, Any]) -> Dict[str, Any]:
        tv_exit_preflight = self._tv_exit_non_executable_preflight(signal)
        if tv_exit_preflight:
            detail["tv_exit_preflight"] = tv_exit_preflight
            reason = str(tv_exit_preflight.get("reason") or "tv_exit_not_executable")
            self._annotate_execution_blocked(
                detail,
                readiness="not_executable",
                reason=reason,
                message=str(tv_exit_preflight.get("message") or "TV exit cannot execute without an active origin order/position."),
                order_linkage_status=str(tv_exit_preflight.get("order_linkage_status") or "origin_order_not_active"),
                gateway_request_blocked=True,
                origin_execution_status=tv_exit_preflight.get("origin_execution_status"),
                active_related_order_count=tv_exit_preflight.get("active_related_order_count"),
            )
            return self._mark_blocked(
                detail,
                reason,
                order_linkage_status=detail.get("order_linkage_status"),
                gateway_request_blocked=True,
                origin_execution_status=tv_exit_preflight.get("origin_execution_status"),
            )

        if not self.order_lifecycle or not self.order_placer:
            logger.warning("Order placer/lifecycle not configured for close")
            return self._mark_blocked(detail, "close_dependencies_missing")

        symbol = str(self._signal_value(signal, "symbol") or "").upper()
        conid = self._resolve_conid(symbol, signal)
        if not conid:
            return self._mark_blocked(detail, "conid_unresolved", symbol=symbol)

        positions_result = self._get_positions_result()
        detail["position_snapshot"] = self._compact_position_snapshot_result(positions_result)
        if not positions_result.get("ok"):
            self._notify_position_snapshot_unavailable(signal, symbol, positions_result)
            return self._mark_retryable_blocked(
                detail,
                "position_snapshot_unavailable",
                error=str(positions_result.get("error") or ""),
                retry_after_s=positions_result.get("retry_after_s"),
                account_data_backoff_reason=positions_result.get("account_data_backoff_reason"),
            )

        positions = list(positions_result.get("positions") or [])
        qty = self._position_quantity(symbol, positions)
        detail["position_qty_before_close"] = qty
        if qty == 0:
            cancel_result = self._cancel_old_order_if_present(signal, detail)
            if cancel_result is not None and not cancel_result.get("ok"):
                return cancel_result
            detail["close_old_position"] = "skipped_flat"
            detail["wait_flat"] = "confirmed"
            detail["flat_confirmation"] = {
                "confirmed": True,
                "position_qty": 0.0,
                "source": "broker_positions",
            }
            return self._start_cooldown_and_ready(signal, symbol, detail, "already_flat_ready_reentry")

        cancel_result = self._cancel_old_order_if_present(signal, detail)
        if cancel_result is not None and not cancel_result.get("ok"):
            return cancel_result

        self._append_state(detail, "close_old_position")
        direction = "long" if qty > 0 else "short"
        close_qty = abs(int(round(qty)))
        if close_qty <= 0:
            return self._mark_blocked(detail, "close_quantity_invalid", position_qty=qty)

        origin_signal_id = str(
            self._signal_value(signal, "origin_signal_id")
            or self._signal_value(signal, "signal_id")
            or self._signal_value(signal, "source_signal_id")
            or ""
        ).strip()
        close_reason = str(
            self._signal_value(signal, "exit_reason")
            or self._signal_value(signal, "reason")
            or "reverse_signal_close"
        ).strip()
        result = self.order_placer.place_market_close(
            conid,
            symbol,
            direction,
            close_qty,
            trade_group_id=self._related_trade_group_id(signal),
            entry_order_unique_id=str(self._signal_value(signal, "entry_order_unique_id") or "").strip(),
            signal_id=origin_signal_id,
            source="reverse_signal_close",
            close_reason=close_reason,
            position_snapshot=self._position_snapshot_for_symbol(symbol, positions),
        )
        detail["close_old_position"] = "submitted" if result.get("ok") else "failed"
        detail["close_result"] = dict(result or {})
        if not result.get("ok"):
            submission_unconfirmed, pending_order_ids, pending_order_ref = self._close_submission_unconfirmed_fields(result)
            if submission_unconfirmed:
                detail["close_old_position"] = "submission_unconfirmed"
                detail["close_submission_unconfirmed"] = True
                detail["pending_close_order_ids"] = pending_order_ids
                detail["pending_close_order_ref"] = pending_order_ref
                runtime_detail = detail.setdefault("reverse_runtime_detail", {})
                runtime_detail["close_submission_unconfirmed"] = True
                runtime_detail["pending_close_order_ids"] = list(pending_order_ids)
                runtime_detail["pending_close_order_ref"] = pending_order_ref
                pending = self._self_heal_close_by_flat(
                    signal,
                    detail,
                    "close_order_submission_unconfirmed",
                )
                if pending is not None:
                    return pending
            return self._mark_blocked(
                detail,
                "close_order_failed",
                error=str((result or {}).get("error") or ""),
            )

        self._append_state(detail, "wait_flat")
        flat_confirmed, flat_detail = self._confirm_flat(symbol)
        detail["wait_flat"] = "confirmed" if flat_confirmed else "unconfirmed"
        detail["flat_confirmation"] = flat_detail
        if not flat_confirmed:
            return self._mark_retryable_blocked(
                detail,
                "flat_not_confirmed",
                position_qty=flat_detail.get("position_qty"),
                attempts=flat_detail.get("attempts"),
            )

        detail["close_old_position"] = "confirmed"
        return self._start_cooldown_and_ready(signal, symbol, detail, "close_confirmed_ready_reentry")

    def _tv_exit_non_executable_preflight(self, signal: dict) -> Dict[str, Any]:
        if not self._is_tv_exit_signal(signal):
            return {}
        broker_mode = self._risk_update_broker_mode(signal)
        origin_signal_id = self._origin_signal_id(signal)
        origin = self._origin_signal_record(signal) if origin_signal_id else {}
        execution_payload, execution_mode = self._origin_execution_payload(origin, broker_mode)
        if not self._origin_execution_terminal(origin, execution_payload):
            return {}

        trade_group_id = self._related_trade_group_id(signal)
        order_ids = self._related_order_ids_from_signal(signal)
        related_orders = self._fetch_related_orders(
            trade_group_id=trade_group_id,
            order_ids=order_ids,
            environment=broker_mode,
        )
        active_related = [order for order in related_orders or [] if self._is_active_order(order)]
        if active_related:
            return {}

        origin_execution_status = self._normalized_execution_status(
            (execution_payload or {}).get("status")
            or (execution_payload or {}).get("order_status")
            or (origin or {}).get("status")
        )
        return {
            "reason": "tv_exit_origin_order_not_active",
            "message": (
                "Gateway position/open-order lookup skipped because the TV exit origin order is terminal "
                f"({origin_execution_status or 'unknown'}) and no active local related orders exist."
            ),
            "order_linkage_status": "origin_order_not_active",
            "gateway_request_blocked": True,
            "broker_mode": broker_mode,
            "origin_signal_id": origin_signal_id,
            "origin_signal_found": bool(origin),
            "origin_execution_mode": execution_mode,
            "origin_execution_status": origin_execution_status,
            "trade_group_id": trade_group_id,
            "related_order_count": len(related_orders or []),
            "active_related_order_count": len(active_related),
        }

    def _cancel_old_order_if_present(self, signal: dict, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        trade_group_id = self._related_trade_group_id(signal)
        order_ids = self._related_order_ids_from_signal(signal)
        if not trade_group_id and not order_ids:
            detail["cancel_old_order"] = "skipped_no_related_order"
            return None
        return self._handle_cancel(
            signal,
            detail,
            ready_on_success=False,
        )

    def _start_cooldown_and_ready(self, signal: dict, symbol: str, detail: Dict[str, Any], reason: str) -> Dict[str, Any]:
        self._append_state(detail, "cooldown")
        cooldown_bars = 0
        if self.signal_processor:
            try:
                self.signal_processor.remove_position(symbol)
            except Exception as exc:
                logger.debug("Failed to remove reversed position from signal processor: %s", exc)
            try:
                cooldown_bars = int(self.signal_processor.cooldown_bars_after_reverse())
                self.signal_processor.start_cooldown(
                    symbol,
                    cooldown_bars,
                    "cooldown_after_reverse_close",
                )
                detail["cooldown"] = "started"
            except Exception as exc:
                detail["cooldown"] = "failed"
                return self._mark_blocked(detail, "cooldown_start_failed", error=str(exc))
        else:
            detail["cooldown"] = "skipped_no_signal_processor"
        detail["cooldown_bars"] = cooldown_bars
        return self._mark_ready_reentry(signal=signal, detail=detail, reason=reason)

    def _handle_cancel(
        self,
        signal: dict,
        detail: Optional[Dict[str, Any]] = None,
        *,
        allow_no_related_rows: bool = False,
        ready_on_success: bool = True,
    ) -> Dict[str, Any]:
        detail = detail or self._base_reverse_detail(signal, "cancel")
        if not self.order_modifier:
            return self._mark_blocked(detail, "cancel_dependency_missing")

        self._append_state(detail, "cancel_old_order")
        runtime_environment = normalize_broker_mode(self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"), self.environment)
        trade_group_id = self._related_trade_group_id(signal)
        seed_order_ids = self._related_order_ids_from_signal(signal)
        orders = self._fetch_related_orders(
            trade_group_id=trade_group_id,
            order_ids=seed_order_ids,
            environment=runtime_environment,
        )
        related_order_ids = self._unique_nonempty(
            seed_order_ids + [self._order_id(order) for order in orders]
        )
        active_pb_orders = [order for order in orders if self._is_active_order(order)]
        if active_pb_orders:
            cancel_ids = self._unique_nonempty([self._order_id(order) for order in active_pb_orders])
        elif orders:
            cancel_ids = []
        else:
            cancel_ids = related_order_ids
        role_by_order_id = {
            self._order_id(order): self._child_order_role(order)
            for order in active_pb_orders
            if self._order_id(order)
        }

        detail["cancel_old_order"] = "no_active_orders" if related_order_ids and not cancel_ids else "started"
        detail["cancel_target"] = {
            "trade_group_id": trade_group_id,
            "order_ids": related_order_ids,
            "pb_orders_checked": len(orders),
            "active_pb_order_ids": [self._order_id(order) for order in active_pb_orders],
        }

        if not related_order_ids and not trade_group_id:
            return self._mark_blocked(detail, "cancel_target_missing")
        if not related_order_ids and not allow_no_related_rows:
            return self._mark_blocked(detail, "no_related_orders_found", trade_group_id=trade_group_id)

        cancel_results = []
        cancel_errors = []
        if cancel_ids:
            for oid in cancel_ids:
                role = role_by_order_id.get(str(oid), "") or "unknown"
                operation = f"cancel_{role}" if role in {"take_profit", "stop_loss"} else "cancel_order"
                result = self.order_modifier.cancel_order(oid, operation=operation, order_family_type=role)
                item = {"order_id": str(oid), **dict(result or {})}
                cancel_results.append(item)
                if not result.get("ok"):
                    cancel_errors.append(item)
        detail["cancel_results"] = cancel_results
        if cancel_errors:
            detail["cancel_old_order"] = "failed"
            return self._mark_blocked(
                detail,
                "cancel_order_failed",
                failed_order_ids=[item.get("order_id") for item in cancel_errors],
                errors=[item.get("error") for item in cancel_errors if item.get("error")],
            )

        confirmed, confirmation = self._confirm_related_orders_inactive(
            trade_group_id=trade_group_id,
            order_ids=related_order_ids or cancel_ids,
            environment=runtime_environment,
            pb_orders=orders,
        )
        detail["cancel_confirmation"] = confirmation
        if not confirmed:
            detail["cancel_old_order"] = "unconfirmed"
            return self._mark_retryable_blocked(
                detail,
                "cancel_not_confirmed",
                active_order_ids=confirmation.get("active_order_ids"),
                confirmation_source=confirmation.get("source"),
            )

        detail["cancel_old_order"] = "confirmed"
        if not ready_on_success:
            return {
                "ok": True,
                "ack_status": "confirmed",
                "reason": "cancel_confirmed",
                "detail": detail,
            }
        return self._mark_ready_reentry(signal, detail, "cancel_confirmed_ready_reentry")

    def _handle_adjust_sl(self, signal: dict, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        detail = detail or self._base_reverse_detail(signal, "adjust_sl")
        if not self.order_modifier:
            return self._mark_blocked(detail, "adjust_sl_dependency_missing")
        order_id = str(self._signal_value(signal, "sl_order_id") or "").strip()
        new_price = float(self._signal_value(signal, "new_sl", 0) or 0)
        if not order_id or new_price <= 0:
            return self._mark_blocked(detail, "adjust_sl_target_invalid")
        result = self.order_modifier.update_stop_loss(order_id, new_price)
        detail["adjust_result"] = dict(result or {})
        detail["result_status"] = "ok" if result.get("ok") else "failed"
        if not result.get("ok"):
            return self._mark_blocked(detail, "adjust_sl_failed", error=str(result.get("error") or ""))
        confirmed, confirmation = self._confirm_adjust_child_order_target_price(
            signal,
            order_id=order_id,
            side="stop_loss",
            expected_price=new_price,
            modify_result=result,
        )
        detail["adjust_confirmation"] = confirmation
        if not confirmed:
            return self._mark_retryable_blocked(
                detail,
                "adjust_sl_not_confirmed",
                order_id=order_id,
                new_price=new_price,
                confirmation_source=confirmation.get("source"),
            )
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": "adjust_sl_confirmed",
            "detail": detail,
        }

    def _handle_adjust_tp(self, signal: dict, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        detail = detail or self._base_reverse_detail(signal, "adjust_tp")
        if not self.order_modifier:
            return self._mark_blocked(detail, "adjust_tp_dependency_missing")
        order_id = str(self._signal_value(signal, "tp_order_id") or "").strip()
        new_price = float(self._signal_value(signal, "new_tp", 0) or 0)
        if not order_id or new_price <= 0:
            return self._mark_blocked(detail, "adjust_tp_target_invalid")
        result = self.order_modifier.update_take_profit(order_id, new_price)
        detail["adjust_result"] = dict(result or {})
        detail["result_status"] = "ok" if result.get("ok") else "failed"
        if not result.get("ok"):
            return self._mark_blocked(detail, "adjust_tp_failed", error=str(result.get("error") or ""))
        confirmed, confirmation = self._confirm_adjust_child_order_target_price(
            signal,
            order_id=order_id,
            side="take_profit",
            expected_price=new_price,
            modify_result=result,
        )
        detail["adjust_confirmation"] = confirmation
        if not confirmed:
            return self._mark_retryable_blocked(
                detail,
                "adjust_tp_not_confirmed",
                order_id=order_id,
                new_price=new_price,
                confirmation_source=confirmation.get("source"),
            )
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": "adjust_tp_confirmed",
            "detail": detail,
        }

    def _config_bool(self, key: str, default: bool = False) -> bool:
        config = self.config
        getter = getattr(config, "get_bool_for_environment", None)
        if callable(getter):
            try:
                return bool(getter(key, self.environment, default))
            except Exception:
                pass
        getter = getattr(config, "get_bool", None)
        if callable(getter):
            try:
                return bool(getter(key, default))
            except Exception:
                pass
        try:
            from ibkr_compute.core.config import Config

            raw_default = str(Config.DEFAULTS.get(key, str(default).lower()) or "").strip().lower()
            return raw_default in {"1", "true", "yes", "on"}
        except Exception:
            return bool(default)

    def _config_has_value(self, key: str) -> bool:
        checker = getattr(self.config, "has_value_for_environment", None)
        if callable(checker):
            try:
                return bool(checker(key, self.environment))
            except Exception:
                return False
        return False

    def _config_bool_prefer(self, keys: Tuple[str, ...], default: bool = False) -> bool:
        for key in keys:
            if self._config_has_value(key):
                return self._config_bool(key, default)
        return self._config_bool(keys[0], default) if keys else bool(default)

    @classmethod
    def _source_tokens(cls, signal: dict) -> set[str]:
        extra = cls._signal_extra(signal)
        values = [
            signal.get("source"),
            signal.get("event_type"),
            signal.get("reason"),
            signal.get("action_type"),
            extra.get("source"),
            extra.get("event_type"),
            extra.get("tv_event_type"),
            extra.get("reverse_kind"),
            extra.get("reason"),
            extra.get("risk_update_reason"),
            extra.get("action_type"),
        ]
        return {str(value or "").strip().lower() for value in values if str(value or "").strip()}

    @classmethod
    def _is_tv_risk_update_signal(cls, signal: dict) -> bool:
        tokens = cls._source_tokens(signal)
        if "risk_update" in tokens or "tv_risk_update" in tokens:
            return True
        return any("risk_update" in token for token in tokens) and bool(tokens & {"tv", "tradingview"})

    @classmethod
    def _is_tv_exit_signal(cls, signal: dict) -> bool:
        tokens = cls._source_tokens(signal)
        if "tv_exit" in tokens:
            return True
        return "exit" in tokens and bool(tokens & {"tv", "tradingview"})

    @classmethod
    def _is_order_flow_signal(cls, signal: dict) -> bool:
        return any("order_flow" in token for token in cls._source_tokens(signal))

    def _is_cancelled_flat_not_confirmed_tv_close_candidate(self, signal: dict) -> bool:
        if str((signal or {}).get("status") or "").strip().lower() != "cancelled":
            return False
        if str((signal or {}).get("action_type") or "").strip().lower() != "close":
            return False
        if not self._is_tradingview_reverse_signal(signal):
            return False
        extra = self._signal_extra(signal)
        blocked = self._as_dict(extra.get("reentry_blocked"))
        text = " ".join(
            str(value or "").strip().lower()
            for value in (
                signal.get("reason"),
                extra.get("reason"),
                extra.get("result_status"),
                extra.get("reverse_state"),
                extra.get("wait_flat"),
                extra.get("execution_blocked_reason"),
                blocked.get("reason"),
            )
        )
        return "flat_not_confirmed" in text

    def _attempt_tv_async_self_heal(
        self,
        signal: dict,
        action: str,
        detail: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if action == "close" and self._has_tv_close_submission_evidence(signal):
            return self._self_heal_close_by_flat(signal, detail, "flat_not_confirmed")
        if action == "cancel" and self._has_tv_cancel_submission_evidence(signal):
            return self._self_heal_cancel_by_inactive(signal, detail)
        if action in {"adjust_sl", "adjust_tp"} and self._has_tv_adjust_submission_evidence(signal, action):
            return self._self_heal_adjust_by_price(signal, action, detail)
        if action == "adjust_bracket" and self._has_tv_adjust_submission_evidence(signal, action):
            return self._self_heal_adjust_bracket_by_price(signal, detail)
        return None

    def _has_tv_close_submission_evidence(self, signal: dict) -> bool:
        extra = self._signal_extra(signal)
        close_result = self._as_dict(extra.get("close_result"))
        submission_unconfirmed, pending_order_ids, pending_order_ref = self._close_submission_unconfirmed_fields(close_result)
        blocked = self._as_dict(extra.get("reentry_blocked"))
        text = " ".join(
            str(value or "").strip().lower()
            for value in (
                signal.get("reason"),
                extra.get("result_status"),
                extra.get("reverse_state"),
                extra.get("close_old_position"),
                extra.get("wait_flat"),
                blocked.get("reason"),
            )
        )
        if bool(extra.get("close_submission_unconfirmed")) or submission_unconfirmed:
            return True
        if pending_order_ids or pending_order_ref:
            return True
        if self._signal_list(extra, "pending_close_order_ids", "submitted_close_order_ids"):
            return True
        if str(extra.get("close_old_position") or "").strip().lower() in {"submitted", "submission_unconfirmed"}:
            return True
        return "flat_not_confirmed" in text or "close_order_submission_unconfirmed" in text

    def _has_tv_cancel_submission_evidence(self, signal: dict) -> bool:
        extra = self._signal_extra(signal)
        confirmation = self._as_dict(extra.get("cancel_confirmation"))
        blocked = self._as_dict(extra.get("reentry_blocked"))
        cancel_results = extra.get("cancel_results")
        text = " ".join(
            str(value or "").strip().lower()
            for value in (
                signal.get("reason"),
                extra.get("result_status"),
                extra.get("reverse_state"),
                extra.get("cancel_old_order"),
                blocked.get("reason"),
            )
        )
        if isinstance(cancel_results, list) and cancel_results:
            return True
        if confirmation and not confirmation.get("confirmed"):
            return True
        return "cancel_not_confirmed" in text

    def _has_tv_adjust_submission_evidence(self, signal: dict, action: str) -> bool:
        extra = self._signal_extra(signal)
        blocked = self._as_dict(extra.get("reentry_blocked"))
        text = " ".join(
            str(value or "").strip().lower()
            for value in (
                signal.get("reason"),
                extra.get("result_status"),
                extra.get("reverse_state"),
                extra.get("adjust_bracket"),
                blocked.get("reason"),
            )
        )
        if action in {"adjust_sl", "adjust_tp"}:
            adjust_result = self._as_dict(extra.get("adjust_result"))
            confirmation = self._as_dict(extra.get("adjust_confirmation"))
            return bool(adjust_result.get("ok") or confirmation or "adjust_price_not_confirmed" in text)

        adjust_results = extra.get("adjust_results")
        if not isinstance(adjust_results, dict):
            return "adjust_bracket_not_confirmed" in text or "adjust_price_not_confirmed" in text
        for side_detail in adjust_results.values():
            if not isinstance(side_detail, dict):
                continue
            result = self._as_dict(side_detail.get("result"))
            if side_detail.get("skipped"):
                continue
            if result.get("ok") or str(side_detail.get("reason") or "") == "adjust_price_not_confirmed":
                return True
        return False

    def _self_heal_cancelled_flat_not_confirmed_close(self, signal: dict) -> Optional[Dict[str, Any]]:
        detail = self._base_reverse_detail(signal, "close")
        detail["historical_self_heal"] = {
            "enabled": True,
            "previous_status": str(signal.get("status") or ""),
            "previous_reason": str(signal.get("reason") or ""),
            "source": "cancelled_flat_not_confirmed_tv_close",
        }
        return self._self_heal_close_by_flat(
            signal,
            detail,
            "historical_flat_not_confirmed",
            pending_on_unconfirmed=False,
        )

    def _self_heal_close_by_flat(
        self,
        signal: dict,
        detail: Dict[str, Any],
        reason: str,
        *,
        pending_on_unconfirmed: bool = True,
    ) -> Optional[Dict[str, Any]]:
        extra = self._signal_extra(signal)
        for key in (
            "close_result",
            "close_submission_unconfirmed",
            "close_old_position",
            "wait_flat",
            "pending_close_order_ids",
            "pending_close_order_ref",
            "position_qty_before_close",
        ):
            if key in extra and (key not in detail or detail.get(key) in ("", "skipped")):
                detail[key] = copy.deepcopy(extra.get(key))

        detail["async_self_heal"] = {
            "enabled": True,
            "action": "close",
            "confirmation": "broker_position_flat",
            "reason": reason,
            "duplicate_submit_blocked": True,
        }
        symbol = str(self._signal_value(signal, "symbol") or detail.get("symbol") or "").upper()
        if not self.order_lifecycle:
            if not pending_on_unconfirmed:
                return None
            detail["wait_flat"] = "unconfirmed"
            return self._mark_retryable_blocked(
                detail,
                str(reason or "flat_not_confirmed"),
                error="order_lifecycle_missing",
            )
        flat_confirmed, flat_detail = self._confirm_flat(symbol)
        detail["wait_flat"] = "confirmed" if flat_confirmed else "unconfirmed"
        detail["flat_confirmation"] = flat_detail
        if not flat_confirmed:
            detail["close_old_position"] = detail.get("close_old_position") or extra.get("close_old_position") or "submitted"
            if not pending_on_unconfirmed:
                return None
            return self._mark_retryable_blocked(
                detail,
                str(reason or "flat_not_confirmed"),
                position_qty=flat_detail.get("position_qty"),
                attempts=flat_detail.get("attempts"),
            )

        detail["close_old_position"] = "confirmed"
        return self._start_cooldown_and_ready(signal, symbol, detail, "close_self_healed_flat_ready_reentry")

    def _cancel_self_heal_targets(self, signal: dict) -> Tuple[str, List[str]]:
        extra = self._signal_extra(signal)
        target = self._as_dict(extra.get("cancel_target"))
        confirmation = self._as_dict(extra.get("cancel_confirmation"))
        trade_group_id = str(
            self._related_trade_group_id(signal)
            or target.get("trade_group_id")
            or confirmation.get("trade_group_id")
            or ""
        ).strip()
        order_ids: List[str] = []
        order_ids.extend(self._related_order_ids_from_signal(signal))
        for payload in (target, confirmation):
            order_ids.extend(self._signal_list(payload, "order_ids", "active_order_ids", "active_pb_order_ids"))
        for item in extra.get("cancel_results") or []:
            if isinstance(item, dict):
                order_ids.append(str(item.get("order_id") or item.get("broker_order_id") or "").strip())
        return trade_group_id, self._unique_nonempty(order_ids)

    def _self_heal_cancel_by_inactive(self, signal: dict, detail: Dict[str, Any]) -> Dict[str, Any]:
        extra = self._signal_extra(signal)
        for key in ("cancel_results", "cancel_target", "cancel_confirmation"):
            if key in extra:
                detail[key] = extra.get(key)
        detail["async_self_heal"] = {
            "enabled": True,
            "action": "cancel",
            "confirmation": "target_orders_inactive",
            "duplicate_submit_blocked": True,
        }
        runtime_environment = normalize_broker_mode(
            self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"),
            self.environment,
        )
        trade_group_id, order_ids = self._cancel_self_heal_targets(signal)
        if not trade_group_id and not order_ids:
            detail["cancel_old_order"] = "unconfirmed"
            return self._mark_retryable_blocked(detail, "cancel_target_missing")

        pb_orders = self._fetch_related_orders(
            trade_group_id=trade_group_id,
            order_ids=order_ids,
            environment=runtime_environment,
        )
        confirmed, confirmation = self._confirm_related_orders_inactive(
            trade_group_id=trade_group_id,
            order_ids=order_ids,
            environment=runtime_environment,
            pb_orders=pb_orders,
        )
        detail["cancel_confirmation"] = confirmation
        if not confirmed:
            detail["cancel_old_order"] = "unconfirmed"
            return self._mark_retryable_blocked(
                detail,
                "cancel_not_confirmed",
                active_order_ids=confirmation.get("active_order_ids"),
                confirmation_source=confirmation.get("source"),
            )

        detail["cancel_old_order"] = "confirmed"
        return self._mark_ready_reentry(signal, detail, "cancel_self_healed_inactive_ready_reentry")

    def _self_heal_adjust_by_price(self, signal: dict, action: str, detail: Dict[str, Any]) -> Dict[str, Any]:
        side = "stop_loss" if action == "adjust_sl" else "take_profit"
        order_key = "sl_order_id" if side == "stop_loss" else "tp_order_id"
        price_key = "new_sl" if side == "stop_loss" else "new_tp"
        extra = self._signal_extra(signal)
        adjust_result = self._as_dict(extra.get("adjust_result"))
        order_id = str(self._signal_value(signal, order_key) or adjust_result.get("order_id") or "").strip()
        price = self._coerce_adjust_price(self._signal_value(signal, price_key, adjust_result.get("price", 0)))
        detail["adjust_result"] = adjust_result
        detail["async_self_heal"] = {
            "enabled": True,
            "action": action,
            "confirmation": "active_child_order_target_price",
            "duplicate_submit_blocked": True,
        }
        if not order_id or price <= 0:
            return self._mark_retryable_blocked(
                detail,
                "adjust_child_order_id_or_price_missing",
                order_id=order_id,
                new_price=price,
            )

        confirmed, confirmation = self._confirm_adjust_child_order_target_price(
            signal,
            order_id=order_id,
            side=side,
            expected_price=price,
            modify_result=adjust_result,
        )
        detail["adjust_confirmation"] = confirmation
        if not confirmed:
            detail["result_status"] = "pending_retry"
            return self._mark_retryable_blocked(
                detail,
                f"{action}_not_confirmed",
                order_id=order_id,
                new_price=price,
                confirmation_source=confirmation.get("source"),
            )
        detail["result_status"] = "ok"
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": f"{action}_confirmed",
            "detail": detail,
        }

    def _self_heal_adjust_bracket_by_price(self, signal: dict, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        extra = self._signal_extra(signal)
        previous_results = extra.get("adjust_results")
        if not isinstance(previous_results, dict):
            return None

        results: Dict[str, Dict[str, Any]] = {}
        attempted_sides: List[str] = []
        succeeded_sides: List[str] = []
        unconfirmed_sides: List[str] = []
        missing_sides: List[str] = []
        for side in ("stop_loss", "take_profit"):
            side_detail = dict(previous_results.get(side) or {})
            results[side] = side_detail
            if side_detail.get("skipped"):
                continue
            modify_result = self._as_dict(side_detail.get("result"))
            if not modify_result.get("ok") and str(side_detail.get("reason") or "") != "adjust_price_not_confirmed":
                continue
            attempted_sides.append(side)
            order_id = str(side_detail.get("order_id") or modify_result.get("order_id") or "").strip()
            price = self._coerce_adjust_price(side_detail.get("new_price") or modify_result.get("price") or 0)
            if not order_id or price <= 0:
                missing_sides.append(side)
                side_detail.update({"ok": False, "reason": "adjust_child_order_id_or_price_missing"})
                continue
            confirmed, confirmation = self._confirm_adjust_child_order_target_price(
                signal,
                order_id=order_id,
                side=side,
                expected_price=price,
                modify_result=modify_result,
            )
            side_detail["confirmation"] = confirmation
            if confirmed:
                side_detail.update({"ok": True, "reason": "confirmed"})
                succeeded_sides.append(side)
            else:
                side_detail.update({"ok": False, "reason": "adjust_price_not_confirmed"})
                unconfirmed_sides.append(side)

        if not attempted_sides:
            return None

        detail["async_self_heal"] = {
            "enabled": True,
            "action": "adjust_bracket",
            "confirmation": "active_child_order_target_price",
            "duplicate_submit_blocked": True,
        }
        detail["adjust_results"] = results
        detail["adjust_bracket_result"] = {
            "attempted_sides": attempted_sides,
            "succeeded_sides": succeeded_sides,
            "failed_sides": [],
            "unconfirmed_sides": unconfirmed_sides,
            "missing_sides": missing_sides,
        }

        if unconfirmed_sides or missing_sides:
            detail["adjust_bracket"] = "pending_confirmation"
            return self._mark_retryable_blocked(
                detail,
                "adjust_bracket_not_confirmed",
                unconfirmed_sides=unconfirmed_sides,
                missing_sides=missing_sides,
            )

        detail["adjust_bracket"] = "confirmed"
        detail["result_status"] = "ok"
        self._persist_risk_update_state(signal, detail, results)
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": "adjust_bracket_confirmed",
            "detail": detail,
        }

    def _missing_child_order_retry_pending_enabled(self, signal: dict) -> bool:
        if not self._is_tv_risk_update_signal(signal):
            return False
        return self._config_bool_prefer(
            ("tv_risk_update_missing_child_order_retry_pending", "tv_risk_update_retry_missing_child_orders"),
            True,
        )

    @staticmethod
    def _timestamp_from_value(value: Any) -> Tuple[Optional[float], str]:
        if value in (None, ""):
            return None, ""
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric <= 0:
                return None, ""
            return (numeric / 1000.0 if numeric > 10_000_000_000 else numeric), "numeric"
        text = str(value or "").strip()
        if not text:
            return None, ""
        try:
            numeric = float(text)
            if numeric > 0:
                return (numeric / 1000.0 if numeric > 10_000_000_000 else numeric), "numeric"
        except (TypeError, ValueError):
            pass
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ET)
            return parsed.timestamp(), "iso"
        except Exception:
            return None, ""

    def _risk_update_age_status(self, signal: dict) -> Dict[str, Any]:
        try:
            max_age_seconds = max(
                0.0,
                float(os.environ.get("IBKR_TV_RISK_UPDATE_MISSING_CHILD_DEFER_SECONDS", "180") or 180),
            )
        except (TypeError, ValueError):
            max_age_seconds = 180.0
        extra = self._signal_extra(signal)
        timestamp_keys = (
            "created",
            "updated",
            "received_at",
            "received_time",
            "ingested_at",
            "created_at",
            "updated_at",
            "event_time",
            "event_time_ms",
            "received_at_ms",
            "created_at_ms",
        )
        for source_name, payload in (("reverse_signal", signal), ("reverse_signal.extra", extra)):
            for key in timestamp_keys:
                timestamp, kind = self._timestamp_from_value((payload or {}).get(key))
                if timestamp is None:
                    continue
                age_seconds = max(0.0, time.time() - timestamp)
                return {
                    "source": f"{source_name}.{key}",
                    "timestamp_kind": kind,
                    "age_seconds": round(age_seconds, 3),
                    "max_defer_seconds": max_age_seconds,
                    "young": True if max_age_seconds <= 0 else age_seconds <= max_age_seconds,
                }
        return {
            "source": "unavailable",
            "age_seconds": None,
            "max_defer_seconds": max_age_seconds,
            "young": True,
            "assumed_young": True,
        }

    def _should_defer_missing_child_order_resolution(
        self,
        signal: dict,
        resolution: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        age_status = self._risk_update_age_status(signal)
        enabled = self._missing_child_order_retry_pending_enabled(signal)
        local_linkage_exists = bool((resolution or {}).get("linkage_exists"))
        linkage_hint_exists = bool((resolution or {}).get("linkage_hint_exists"))
        status = {
            "enabled": enabled,
            "local_linkage_exists": local_linkage_exists,
            "linkage_hint_exists": linkage_hint_exists,
            "gateway_lookup_allowed": bool((resolution or {}).get("gateway_lookup_allowed")),
            "gateway_request_blocked": bool(((resolution or {}).get("detail") or {}).get("gateway_request_blocked")),
            "origin_execution_terminal": bool(((resolution or {}).get("detail") or {}).get("origin_execution_terminal")),
            "age": age_status,
            "reason": "",
        }
        if not enabled:
            status["reason"] = "config_disabled"
            return False, status
        if status["origin_execution_terminal"]:
            status["reason"] = "origin_execution_terminal"
            return False, status
        if not local_linkage_exists and not linkage_hint_exists:
            status["reason"] = "local_linkage_missing"
            return False, status
        if not age_status.get("young"):
            status["reason"] = "risk_update_too_old"
            return False, status
        status["reason"] = "young_linked_risk_update" if local_linkage_exists else "young_waiting_local_order_linkage"
        return True, status

    def _incoming_risk_update_seq(self, signal: dict) -> int:
        return self._coerce_int(self._signal_value(signal, "risk_update_seq", 0), 0)

    def _origin_signal_record(self, signal: dict) -> Dict[str, Any]:
        signal_id = str(
            self._signal_value(signal, "origin_signal_id")
            or self._signal_value(signal, "signal_id")
            or self._signal_value(signal, "source_signal_id")
            or ""
        ).strip()
        getter = getattr(self.pb_client, "get_first_record", None)
        if not signal_id or not callable(getter):
            return {}
        raw_candidates = (
            self._signal_value(signal, "data_environment"),
            self._signal_value(signal, "market_data_mode"),
            self._signal_value(signal, "signal_environment"),
            self._signal_value(signal, "environment"),
            self._signal_value(signal, "broker_mode"),
            self.environment,
        )
        environments: List[str] = []
        for raw in raw_candidates:
            if raw in (None, ""):
                continue
            for candidate in (resolve_data_environment(raw), normalize_broker_mode(raw, self.environment)):
                if candidate and candidate not in environments:
                    environments.append(candidate)
        for environment in environments:
            try:
                record = getter(
                    "ibkr_signals",
                    filter=(
                        f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                        f'environment = "{self._escape_filter_value(environment)}"'
                    ),
                )
                if isinstance(record, dict) and record:
                    return dict(record)
            except Exception as exc:
                logger.debug("Failed to load origin signal for risk update guard from %s: %s", environment, exc)
        return {}

    def _stored_risk_update_seq(self, signal: dict) -> Tuple[Optional[int], str, Dict[str, Any]]:
        prior_keys = (
            "last_risk_update_seq",
            "last_applied_risk_update_seq",
            "tv_last_risk_update_seq",
            "applied_risk_update_seq",
            "previous_risk_update_seq",
            "risk_update_seq_prior",
        )
        signal_extra = self._signal_extra(signal)
        for source_name, payload in (("reverse_signal.extra", signal_extra), ("reverse_signal", signal)):
            for key in prior_keys:
                if key not in payload:
                    continue
                value = self._coerce_int(payload.get(key), 0)
                if value > 0:
                    return value, f"{source_name}.{key}", {}

        origin = self._origin_signal_record(signal)
        origin_extra = self._as_dict(origin.get("extra"))
        origin_prior_keys = prior_keys + ("risk_update_seq", "tv_risk_update_seq")
        for source_name, payload in (("origin_signal.extra", origin_extra), ("origin_signal", origin)):
            for key in origin_prior_keys:
                if key not in payload:
                    continue
                value = self._coerce_int(payload.get(key), 0)
                if value > 0:
                    return value, f"{source_name}.{key}", origin
        return None, "", origin

    def _risk_update_sequence_status(self, signal: dict) -> Dict[str, Any]:
        if not self._is_tv_risk_update_signal(signal):
            return {"enabled": False, "reason": "not_tv_risk_update"}
        if not self._config_bool_prefer(
            ("tv_risk_update_seq_guard_enabled", "tv_risk_update_require_monotonic_seq"),
            True,
        ):
            return {"enabled": False, "reason": "config_disabled"}
        incoming_seq = self._incoming_risk_update_seq(signal)
        if incoming_seq <= 0:
            return {"enabled": True, "incoming_seq": incoming_seq, "guarded": False, "reason": "incoming_seq_missing"}
        prior_seq, prior_source, origin = self._stored_risk_update_seq(signal)
        if prior_seq is None:
            return {
                "enabled": True,
                "incoming_seq": incoming_seq,
                "guarded": False,
                "reason": "prior_seq_unavailable",
                "origin_signal_found": bool(origin),
            }
        stale = incoming_seq <= int(prior_seq)
        return {
            "enabled": True,
            "incoming_seq": incoming_seq,
            "prior_seq": int(prior_seq),
            "prior_source": prior_source,
            "guarded": stale,
            "reason": "stale_or_duplicate_seq" if stale else "monotonic_seq_ok",
        }

    def _risk_update_stop_guard_enabled(self, signal: dict) -> Tuple[bool, str]:
        if self._is_tv_risk_update_signal(signal) and self._config_bool("tv_risk_update_never_widen_stop", True):
            return True, "tv_risk_update_never_widen_stop"
        if self._is_order_flow_signal(signal) and self._config_bool("never_widen_stop_by_order_flow", True):
            return True, "never_widen_stop_by_order_flow"
        return False, ""

    @classmethod
    def _signal_has_value(cls, signal: dict, key: str) -> bool:
        if not isinstance(signal, dict):
            return False
        if key in signal and signal.get(key) not in (None, "", []):
            return True
        extra = cls._signal_extra(signal)
        return key in extra and extra.get(key) not in (None, "", [])

    @classmethod
    def _signal_has_key(cls, signal: dict, key: str) -> bool:
        if not isinstance(signal, dict):
            return False
        if key in signal:
            return True
        extra = cls._signal_extra(signal)
        return key in extra

    @staticmethod
    def _normalize_adjust_side(value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "sl": "stop_loss",
            "stop": "stop_loss",
            "stoploss": "stop_loss",
            "stop_loss": "stop_loss",
            "new_sl": "stop_loss",
            "new_stop_loss": "stop_loss",
            "tp": "take_profit",
            "takeprofit": "take_profit",
            "take_profit": "take_profit",
            "new_tp": "take_profit",
            "new_take_profit": "take_profit",
        }
        return aliases.get(text, "")

    def _requested_adjust_sides(self, signal: dict) -> Tuple[set[str], bool, str]:
        for key in ("requested_sides", "requested_side", "adjust_sides", "adjust_side"):
            if not self._signal_has_key(signal, key):
                continue
            sides = {
                normalized
                for normalized in (self._normalize_adjust_side(item) for item in self._signal_list(signal, key))
                if normalized
            }
            explicit_flag = str(self._signal_value(signal, "requested_sides_explicit", "")).strip().lower()
            if key == "requested_sides" and not sides and explicit_flag in {"false", "0", "no"}:
                continue
            return sides, True, key
        return set(), False, ""

    def _origin_signal_id(self, signal: dict) -> str:
        return str(
            self._signal_value(signal, "origin_signal_id")
            or self._signal_value(signal, "signal_id")
            or self._signal_value(signal, "source_signal_id")
            or ""
        ).strip()

    @staticmethod
    def _normalize_order_role(value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "sl": "stop_loss",
            "stp": "stop_loss",
            "stop": "stop_loss",
            "stoploss": "stop_loss",
            "stop_loss": "stop_loss",
            "stop_order": "stop_loss",
            "stoploss_order": "stop_loss",
            "stop_loss_order": "stop_loss",
            "repair_sl": "stop_loss",
            "tp": "take_profit",
            "takeprofit": "take_profit",
            "take_profit": "take_profit",
            "take_profit_order": "take_profit",
            "takeprofit_order": "take_profit",
            "profit_target": "take_profit",
            "target": "take_profit",
            "repair_tp": "take_profit",
        }
        return aliases.get(text, "")

    @classmethod
    def _child_order_role(cls, order: Dict[str, Any]) -> str:
        extra = cls._as_dict((order or {}).get("extra"))
        for key in ("role", "order_role", "leg_role"):
            role = cls._normalize_order_role((order or {}).get(key) or extra.get(key))
            if role:
                return role
        for key in ("order_type", "orderType", "type"):
            role = cls._normalize_order_role((order or {}).get(key) or extra.get(key))
            if role:
                return role
        for ref in cls._order_ref_values(order or {}):
            lowered = ref.lower()
            if lowered.startswith(("sl_", "stop_loss_", "stoploss_")):
                return "stop_loss"
            if lowered.startswith(("tp_", "take_profit_", "takeprofit_")):
                return "take_profit"
        return ""

    @staticmethod
    def _broker_order_id_value(payload: Dict[str, Any], *, allow_record_id: bool = False) -> str:
        if not isinstance(payload, dict):
            return ""
        extra = ReverseSignalHandler._as_dict(payload.get("extra"))
        keys = ("broker_order_id", "order_id", "orderId", "ib_order_id", "ibOrderId")
        for key in keys:
            value = payload.get(key)
            if value in (None, ""):
                value = extra.get(key)
            text = str(value or "").strip()
            if text:
                return text
        if allow_record_id:
            return str(payload.get("id") or extra.get("id") or "").strip()
        return ""

    @classmethod
    def _child_order_id_from_order(cls, order: Dict[str, Any], *, allow_record_id: bool = False) -> str:
        return cls._broker_order_id_value(order or {}, allow_record_id=allow_record_id)

    @staticmethod
    def _side_order_id_keys(side: str) -> Tuple[str, ...]:
        if side == "stop_loss":
            return (
                "sl_order_id",
                "stop_loss_order_id",
                "stop_order_id",
                "stopLossOrderId",
                "slOrderId",
            )
        if side == "take_profit":
            return (
                "tp_order_id",
                "take_profit_order_id",
                "takeProfitOrderId",
                "profit_target_order_id",
                "tpOrderId",
            )
        return ()

    @classmethod
    def _any_order_id_from_payload(cls, payload: Any, depth: int = 0) -> str:
        if depth > 8 or payload in (None, ""):
            return ""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return ""
        if isinstance(payload, dict):
            oid = cls._broker_order_id_value(payload, allow_record_id=False)
            if oid:
                return oid
            for key in ("order", "payload", "result", "detail", "details"):
                oid = cls._any_order_id_from_payload(payload.get(key), depth + 1)
                if oid:
                    return oid
        return ""

    @classmethod
    def _child_order_id_from_payload(cls, payload: Any, side: str, depth: int = 0) -> str:
        if depth > 5 or payload in (None, ""):
            return ""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return ""
        if isinstance(payload, (list, tuple)):
            for item in payload:
                oid = cls._child_order_id_from_payload(item, side, depth + 1)
                if oid:
                    return oid
            return ""
        if not isinstance(payload, dict):
            return ""

        extra = cls._as_dict(payload.get("extra"))
        for key in cls._side_order_id_keys(side):
            value = payload.get(key)
            if value in (None, ""):
                value = extra.get(key)
            text = str(value or "").strip()
            if text:
                return text

        for key in ("order_ids", "submitted_order_ids", "broker_order_ids", "submitted_broker_order_ids"):
            values = payload.get(key)
            if values in (None, ""):
                values = extra.get(key)
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except Exception:
                    values = [part.strip() for part in values.split(",")]
            if isinstance(values, (list, tuple)):
                index = 2 if side == "stop_loss" else 1 if side == "take_profit" else -1
                if 0 <= index < len(values):
                    text = str(values[index] or "").strip()
                    if text:
                        return text

        role = cls._normalize_order_role(
            payload.get("role")
            or payload.get("order_role")
            or payload.get("leg_role")
            or payload.get("order_type")
            or payload.get("orderType")
            or extra.get("role")
            or extra.get("order_type")
        )
        if role == side:
            oid = cls._any_order_id_from_payload(payload, depth + 1)
            if oid:
                return oid

        side_containers = {
            "stop_loss": ("stop_loss_order", "stop_order", "sl_order", "stop_loss", "sl"),
            "take_profit": ("take_profit_order", "tp_order", "profit_target_order", "take_profit", "tp"),
        }
        for key in side_containers.get(side, ()):
            oid = cls._child_order_id_from_payload(payload.get(key), side, depth + 1)
            if oid:
                return oid
            oid = cls._child_order_id_from_payload(extra.get(key), side, depth + 1)
            if oid:
                return oid

        for key in ("child_orders", "orders", "order_results", "submitted_orders", "legs"):
            oid = cls._child_order_id_from_payload(payload.get(key), side, depth + 1)
            if oid:
                return oid
            oid = cls._child_order_id_from_payload(extra.get(key), side, depth + 1)
            if oid:
                return oid

        for key in ("order", "payload", "result", "detail", "details"):
            oid = cls._child_order_id_from_payload(payload.get(key), side, depth + 1)
            if oid:
                return oid
        return ""

    @classmethod
    def _group_from_stable_ref(cls, value: Any) -> str:
        text = str(value or "").strip()
        lowered = text.lower()
        for prefix in ("entry_", "tp_", "sl_", "take_profit_", "stop_loss_", "stoploss_"):
            if lowered.startswith(prefix):
                return text[len(prefix):].strip()
        for suffix in ("_entry", "_take_profit", "_stop_loss", "_stoploss"):
            if lowered.endswith(suffix):
                return text[: -len(suffix)].strip()
        return ""

    @classmethod
    def _payload_values(cls, payload: Any, keys: Tuple[str, ...]) -> List[str]:
        if payload in (None, ""):
            return []
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return []
        if not isinstance(payload, dict):
            return []
        extra = cls._as_dict(payload.get("extra"))
        values: List[str] = []
        for key in keys:
            for source in (payload, extra):
                raw = source.get(key) if isinstance(source, dict) else None
                if raw in (None, ""):
                    continue
                if isinstance(raw, (list, tuple, set)):
                    values.extend(str(item or "").strip() for item in raw)
                elif isinstance(raw, str):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list):
                        values.extend(str(item or "").strip() for item in parsed)
                    else:
                        values.extend(part.strip() for part in stripped.split(",") if part.strip())
                else:
                    values.append(str(raw or "").strip())
        return cls._unique_nonempty(values)

    def _risk_update_broker_mode(self, signal: dict) -> str:
        return normalize_broker_mode(
            self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"),
            self.environment,
        )

    def _origin_execution_payload(self, origin: Dict[str, Any], broker_mode: str) -> Tuple[Dict[str, Any], str]:
        extra = self._as_dict((origin or {}).get("extra"))
        execution_by_mode = self._as_dict(extra.get("execution_by_mode"))
        if not execution_by_mode:
            return {}, ""
        mode_candidates = self._unique_nonempty(
            [
                broker_mode,
                self.environment,
                normalize_broker_mode(broker_mode, self.environment),
                str((origin or {}).get("broker_mode") or ""),
                str(extra.get("broker_mode") or ""),
                str(extra.get("last_ack_broker_mode") or ""),
            ]
        )
        for mode in mode_candidates:
            payload = self._as_dict(execution_by_mode.get(mode))
            if payload:
                return payload, mode
        for mode, payload in execution_by_mode.items():
            if isinstance(payload, dict):
                return dict(payload), str(mode or "")
        return {}, ""

    @staticmethod
    def _normalized_execution_status(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @classmethod
    def _origin_execution_allows_gateway_lookup(cls, origin: Dict[str, Any], execution_payload: Dict[str, Any]) -> bool:
        if not origin:
            return False
        origin_extra = cls._as_dict((origin or {}).get("extra"))
        active_statuses = {
            "accepted",
            "active",
            "api_pending",
            "confirmed",
            "filled",
            "filled_position",
            "filled_repricing_protection",
            "partially_filled",
            "pending_submit",
            "pre_submitted",
            "presubmitted",
            "protected_active",
            "protection_reprice_failed",
            "submitted",
            "submitted_waiting_fill",
        }
        status = cls._normalized_execution_status(
            (execution_payload or {}).get("status")
            or (execution_payload or {}).get("order_status")
            or origin_extra.get("last_execution_status")
            or (origin or {}).get("status")
        )
        return status in active_statuses

    @classmethod
    def _origin_execution_terminal(cls, origin: Dict[str, Any], execution_payload: Dict[str, Any]) -> bool:
        if not origin:
            return False
        origin_extra = cls._as_dict((origin or {}).get("extra"))
        terminal_statuses = {
            "cancelled",
            "canceled",
            "error",
            "expired",
            "failed",
            "inactive",
            "entry_missed_limit_cap",
            "not_submitted",
            "rejected",
        }
        status = cls._normalized_execution_status(
            (execution_payload or {}).get("status")
            or (execution_payload or {}).get("order_status")
            or origin_extra.get("last_execution_status")
            or (origin or {}).get("status")
        )
        return status in terminal_statuses

    def _bracket_trade_group_candidates(
        self,
        signal: dict,
        origin: Dict[str, Any],
        execution_payload: Dict[str, Any],
    ) -> List[str]:
        keys = (
            "trade_group_id",
            "bracket_group",
            "entry_order_unique_id",
            "entry_coid",
            "tp_coid",
            "sl_coid",
            "cOID",
            "coid",
            "orderRef",
            "order_ref",
            "parent_order_unique_id",
            "oca_group",
        )
        values: List[str] = [self._related_trade_group_id(signal)]
        for payload in (signal, self._signal_extra(signal), origin, self._as_dict((origin or {}).get("extra")), execution_payload):
            values.extend(self._payload_values(payload, keys))
        expanded = list(values)
        for value in values:
            group = self._group_from_stable_ref(value)
            if group:
                expanded.append(group)
        return self._unique_nonempty(expanded)

    def _stable_bracket_refs(
        self,
        signal: dict,
        origin: Dict[str, Any],
        execution_payload: Dict[str, Any],
        related_orders: List[Dict[str, Any]],
        trade_group_ids: List[str],
    ) -> List[str]:
        keys = (
            "broker_order_id",
            "order_id",
            "orderId",
            "unique_id",
            "entry_order_unique_id",
            "parent_order_unique_id",
            "sibling_order_unique_id",
            "entry_coid",
            "tp_coid",
            "sl_coid",
            "cOID",
            "coid",
            "orderRef",
            "order_ref",
            "trade_group_id",
            "bracket_group",
            "oca_group",
            "submitted_order_ids",
            "order_ids",
        )
        refs: List[str] = []
        refs.extend(trade_group_ids or [])
        origin_signal_id = self._origin_signal_id(signal)
        if origin_signal_id:
            refs.append(origin_signal_id)
        for group in trade_group_ids or []:
            refs.extend([f"entry_{group}", f"tp_{group}", f"sl_{group}"])
        for payload in (signal, self._signal_extra(signal), origin, self._as_dict((origin or {}).get("extra")), execution_payload):
            refs.extend(self._payload_values(payload, keys))
        for side in ("stop_loss", "take_profit"):
            oid = self._child_order_id_from_payload(execution_payload, side)
            if oid:
                refs.append(oid)
        for order in related_orders or []:
            refs.extend(self._order_ref_values(order))
        return self._unique_nonempty(refs)

    def _fetch_bracket_child_orders(
        self,
        *,
        trade_group_ids: List[str],
        origin_signal_id: str,
        environment: str,
    ) -> List[Dict[str, Any]]:
        getter = getattr(self.pb_client, "get_records", None)
        if not callable(getter):
            return []
        env_filter = self._escape_filter_value(environment)
        filters: List[str] = []
        roles = ("stop_loss", "take_profit")
        for role in roles:
            role_filter = self._escape_filter_value(role)
            for group in trade_group_ids or []:
                group_filter = self._escape_filter_value(group)
                filters.extend(
                    [
                        f'environment = "{env_filter}" && role = "{role_filter}" && trade_group_id = "{group_filter}"',
                        f'environment = "{env_filter}" && role = "{role_filter}" && entry_order_unique_id = "{group_filter}"',
                        f'environment = "{env_filter}" && role = "{role_filter}" && bracket_group = "{group_filter}"',
                    ]
                )
            if origin_signal_id:
                signal_filter = self._escape_filter_value(origin_signal_id)
                filters.append(f'environment = "{env_filter}" && role = "{role_filter}" && signal_id = "{signal_filter}"')

        rows: List[Dict[str, Any]] = []
        seen = set()
        for filter_expr in filters:
            try:
                records = getter("orders", filter=filter_expr, per_page=100)
            except Exception:
                records = []
            for record in records or []:
                key = str(record.get("id") or self._child_order_id_from_order(record) or json.dumps(record, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dict(record))
        return rows

    def _select_child_order_id_from_orders(self, orders: List[Dict[str, Any]], side: str) -> Tuple[str, Dict[str, Any]]:
        for order in orders or []:
            if self._child_order_role(order) != side:
                continue
            if self._order_status_key(order) not in REAL_ACTIVE_ORDER_STATUSES:
                continue
            order_id = self._child_order_id_from_order(order)
            if order_id:
                return order_id, dict(order)
        return "", {}

    def _live_order_matches_bracket_linkage(
        self,
        order: Dict[str, Any],
        *,
        trade_group_ids: List[str],
        stable_refs: List[str],
    ) -> bool:
        refs = set(self._order_ref_values(order))
        ref_set = set(stable_refs or [])
        group_set = set(trade_group_ids or [])
        if refs & ref_set:
            return True
        order_group = self._order_trade_group(order)
        if order_group and order_group in group_set:
            return True
        for ref in refs:
            parsed_group = self._group_from_stable_ref(ref)
            if parsed_group and parsed_group in group_set:
                return True
        return False

    def _select_child_order_id_from_live_orders(
        self,
        orders: List[Dict[str, Any]],
        side: str,
        *,
        trade_group_ids: List[str],
        stable_refs: List[str],
    ) -> Tuple[str, Dict[str, Any]]:
        for order in orders or []:
            if self._order_status_key(order) not in REAL_ACTIVE_ORDER_STATUSES:
                continue
            if self._child_order_role(order) != side:
                continue
            if not self._live_order_matches_bracket_linkage(
                order,
                trade_group_ids=trade_group_ids,
                stable_refs=stable_refs,
            ):
                continue
            order_id = self._child_order_id_from_order(order, allow_record_id=True)
            if order_id:
                return order_id, dict(order)
        return "", {}

    def _resolve_adjust_bracket_child_order_ids(self, signal: dict, missing_sides: List[str]) -> Dict[str, Any]:
        broker_mode = self._risk_update_broker_mode(signal)
        origin_signal_id = self._origin_signal_id(signal)
        origin = self._origin_signal_record(signal) if origin_signal_id else {}
        execution_payload, execution_mode = self._origin_execution_payload(origin, broker_mode)
        origin_execution_status = self._normalized_execution_status(
            (execution_payload or {}).get("status")
            or (execution_payload or {}).get("order_status")
            or (origin or {}).get("status")
        )
        origin_execution_terminal = self._origin_execution_terminal(origin, execution_payload)
        origin_execution_lookup_allowed = self._origin_execution_allows_gateway_lookup(origin, execution_payload)
        trade_group_ids = self._bracket_trade_group_candidates(signal, origin, execution_payload)
        explicit_child_ids = self._unique_nonempty(
            [
                self._signal_value(signal, "sl_order_id"),
                self._signal_value(signal, "tp_order_id"),
            ]
        )
        linkage_order_ids = self._unique_nonempty(self._related_order_ids_from_signal(signal) + explicit_child_ids)
        detail: Dict[str, Any] = {
            "broker_mode": broker_mode,
            "origin_signal_id": origin_signal_id,
            "origin_signal_found": bool(origin),
            "origin_execution_mode": execution_mode,
            "origin_execution_status": origin_execution_status,
            "origin_execution_terminal": origin_execution_terminal,
            "origin_execution_lookup_allowed": origin_execution_lookup_allowed,
            "trade_group_ids": trade_group_ids,
            "linkage_order_ids": linkage_order_ids,
            "sources": {},
        }
        resolved: Dict[str, str] = {}

        for side in missing_sides:
            order_id = self._child_order_id_from_payload(execution_payload, side)
            if order_id:
                resolved[side] = order_id
                detail["sources"][side] = "origin_signal.execution_by_mode"

        unresolved = [side for side in missing_sides if side not in resolved]
        related_orders: List[Dict[str, Any]] = []
        if unresolved:
            related_orders = self._fetch_bracket_child_orders(
                trade_group_ids=trade_group_ids,
                origin_signal_id=origin_signal_id,
                environment=broker_mode,
            )
            detail["orders_table_checked"] = len(related_orders)
            for side in list(unresolved):
                order_id, order = self._select_child_order_id_from_orders(related_orders, side)
                if order_id:
                    resolved[side] = order_id
                    detail["sources"][side] = "orders_table"
                    detail.setdefault("orders_table_matches", {})[side] = {
                        "order_id": order_id,
                        "record_id": str(order.get("id") or ""),
                        "role": str(order.get("role") or ""),
                    }
            unresolved = [side for side in missing_sides if side not in resolved]

        linkage_hint_exists = bool(origin_signal_id or trade_group_ids or linkage_order_ids or origin)
        local_linkage_exists = bool(
            linkage_order_ids
            or related_orders
            or resolved
            or ((origin_signal_id and origin) and origin_execution_lookup_allowed)
        )
        detail["linkage_hint_exists"] = linkage_hint_exists
        detail["linkage_exists"] = local_linkage_exists
        detail["local_linkage_exists"] = local_linkage_exists
        detail["gateway_lookup_allowed"] = False
        detail["gateway_request_blocked"] = False

        if unresolved:
            stable_refs = self._stable_bracket_refs(signal, origin, execution_payload, related_orders, trade_group_ids)
            detail["stable_refs"] = stable_refs
            if local_linkage_exists:
                detail["gateway_lookup_allowed"] = True
                live_checked, live_orders, live_error = self._load_live_open_orders()
                detail["live_open_orders_checked"] = bool(live_checked)
                if live_error:
                    detail["live_open_orders_error"] = live_error
                if live_checked:
                    detail["live_open_orders_count"] = len(live_orders)
                    for side in list(unresolved):
                        order_id, order = self._select_child_order_id_from_live_orders(
                            live_orders,
                            side,
                            trade_group_ids=trade_group_ids,
                            stable_refs=stable_refs,
                        )
                        if order_id:
                            resolved[side] = order_id
                            detail["sources"][side] = "broker_open_orders"
                            detail.setdefault("live_open_order_matches", {})[side] = {
                                "order_id": order_id,
                                "order_ref": str(order.get("orderRef") or order.get("cOID") or ""),
                                "status": self._order_status(order),
                            }
            else:
                detail["gateway_request_blocked"] = True
                detail["live_open_orders_checked"] = False
                detail["execution_readiness"] = "not_executable"
                if origin_execution_terminal:
                    detail["execution_blocked_reason"] = "risk_update_origin_order_not_active"
                    detail["execution_blocked_message"] = (
                        "Gateway open-order lookup skipped because the origin signal order status is terminal "
                        f"({origin_execution_status or 'unknown'})."
                    )
                elif linkage_hint_exists:
                    detail["execution_blocked_reason"] = "risk_update_local_order_linkage_missing"
                    detail["execution_blocked_message"] = (
                        "Gateway open-order lookup skipped because the risk_update only has TV payload hints; "
                        "no local signal/order child linkage is available yet."
                    )
                else:
                    detail["execution_blocked_reason"] = "risk_update_linkage_missing"
                    detail["execution_blocked_message"] = (
                        "Gateway open-order lookup skipped because the risk_update has no origin, "
                        "trade group, order id, or child order linkage."
                    )

        unresolved = [side for side in missing_sides if side not in resolved]
        linkage_exists = bool(local_linkage_exists)
        if not linkage_exists and origin_execution_terminal:
            detail["order_linkage_status"] = "origin_order_not_active"
        elif not linkage_exists and linkage_hint_exists:
            detail["order_linkage_status"] = "local_order_linkage_missing"
        elif not linkage_exists:
            detail["order_linkage_status"] = "missing"
        elif unresolved and resolved:
            detail["order_linkage_status"] = "partial_child_order_id_unresolved"
        elif unresolved:
            detail["order_linkage_status"] = "child_order_id_unresolved"
        else:
            detail["order_linkage_status"] = "resolved"
        detail["resolved_order_ids"] = dict(resolved)
        detail["unresolved_sides"] = unresolved
        return {
            "resolved": resolved,
            "unresolved_sides": unresolved,
            "linkage_exists": linkage_exists,
            "linkage_hint_exists": linkage_hint_exists,
            "gateway_lookup_allowed": bool(detail.get("gateway_lookup_allowed")),
            "detail": detail,
        }

    def _build_adjust_resolution_block_results(
        self,
        *,
        side_inputs: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], Any, str, float, bool, bool]],
        blocked_sides: List[str],
        reason: str,
    ) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        blocked = set(blocked_sides or [])
        for side, (spec, side_detail, raw_price, order_id, price, price_valid, requested) in side_inputs.items():
            if not requested:
                side_detail.update({"ok": True, "skipped": True, "reason": "not_requested"})
            elif not price_valid:
                side_detail.update({"ok": False, "skipped": True, "reason": spec["invalid_price_reason"]})
                if raw_price not in (None, ""):
                    side_detail["raw_price"] = raw_price
            elif side in blocked:
                side_detail.update({"ok": False, "skipped": True, "reason": reason})
            else:
                side_detail.update(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "deferred_due_to_child_order_resolution_block",
                        "order_id": order_id,
                        "new_price": price,
                    }
                )
            results[side] = dict(side_detail)
        return results

    def _current_stop_price(self, signal: dict) -> Tuple[float, str]:
        keys = (
            "previous_stop_loss",
            "current_stop_loss",
            "existing_stop_loss",
            "old_stop_loss",
            "current_sl",
            "old_sl",
            "stop_loss",
            "stop_price",
        )
        for key in keys:
            value = self._coerce_adjust_price(self._signal_value(signal, key, 0))
            if value > 0:
                return value, f"reverse_signal.{key}"

        origin = self._origin_signal_record(signal)
        origin_extra = self._as_dict(origin.get("extra"))
        origin_keys = (
            "last_risk_update_stop_loss",
            "current_stop_loss",
            "stop_loss",
            "stop_price",
            "initial_stop_loss",
            "original_stop_loss",
        )
        for source_name, payload in (("origin_signal.extra", origin_extra), ("origin_signal", origin)):
            for key in origin_keys:
                if key not in payload:
                    continue
                value = self._coerce_adjust_price(payload.get(key))
                if value > 0:
                    return value, f"{source_name}.{key}"
        return 0.0, ""

    def _stop_widen_check(self, signal: dict, new_stop: float) -> Dict[str, Any]:
        enabled, guard_key = self._risk_update_stop_guard_enabled(signal)
        status = {"enabled": enabled, "guard_key": guard_key}
        if not enabled:
            return status
        current_stop, source = self._current_stop_price(signal)
        direction = str(
            self._signal_value(signal, "position_side")
            or self._signal_value(signal, "direction")
            or self._signal_value(signal, "current_direction")
            or ""
        ).strip().lower()
        status.update({"current_stop": current_stop, "current_stop_source": source, "direction": direction})
        if current_stop <= 0 or new_stop <= 0 or direction not in {"long", "short"}:
            status["blocked"] = False
            status["reason"] = "comparison_unavailable"
            return status
        widens = new_stop < current_stop - 1e-9 if direction == "long" else new_stop > current_stop + 1e-9
        status["blocked"] = bool(widens)
        status["reason"] = "stop_would_widen" if widens else "stop_not_widened"
        return status

    def _persist_risk_update_state(self, signal: dict, detail: Dict[str, Any], results: Dict[str, Dict[str, Any]]) -> None:
        if not self._is_tv_risk_update_signal(signal):
            return
        origin = self._origin_signal_record(signal)
        updater = getattr(self.pb_client, "update_record", None)
        if not origin or not origin.get("id") or not callable(updater):
            detail.setdefault("risk_update_persistence", {})["origin_signal_found"] = bool(origin)
            return

        extra = self._as_dict(origin.get("extra"))
        incoming_seq = self._incoming_risk_update_seq(signal)
        stop_detail = results.get("stop_loss") or {}
        tp_detail = results.get("take_profit") or {}
        patch_extra = {
            **extra,
            "last_risk_update_at": self._now_iso(),
            "last_risk_update_reason": str(self._signal_value(signal, "risk_update_reason") or ""),
        }
        for key in (
            "runner_enabled",
            "runner_active",
            "target_role",
            "target_is_hard",
            "tp_checkpoint_runner",
            "target_checkpoint",
            "target_checkpoint_is_exit",
            "take_profit",
            "safety_take_profit",
            "runner_activation_price",
            "runner_activation_r",
        ):
            if self._signal_has_value(signal, key):
                patch_extra[key] = self._signal_value(signal, key)
        if incoming_seq > 0:
            patch_extra["last_risk_update_seq"] = incoming_seq
            patch_extra["tv_last_risk_update_seq"] = incoming_seq
        if stop_detail.get("ok") and float(stop_detail.get("new_price") or 0) > 0:
            patch_extra["last_risk_update_stop_loss"] = float(stop_detail.get("new_price") or 0)
        if tp_detail.get("ok") and float(tp_detail.get("new_price") or 0) > 0:
            patch_extra["last_risk_update_take_profit"] = float(tp_detail.get("new_price") or 0)
        try:
            updater("ibkr_signals", str(origin.get("id")), {"extra": patch_extra})
            detail["risk_update_persistence"] = {
                "origin_signal_id": str(origin.get("signal_id") or ""),
                "last_risk_update_seq": incoming_seq if incoming_seq > 0 else None,
                "updated": True,
            }
        except Exception as exc:
            detail["risk_update_persistence"] = {"updated": False, "error": str(exc)}

    def _handle_adjust_bracket(self, signal: dict, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        detail = detail or self._base_reverse_detail(signal, "adjust_bracket")
        if not self.order_modifier:
            return self._mark_blocked(detail, "adjust_bracket_dependency_missing")

        self._append_state(detail, "adjust_bracket")
        side_specs = {
            "stop_loss": {
                "order_key": "sl_order_id",
                "price_key": "new_sl",
                "update_method": self.order_modifier.update_stop_loss,
                "missing_order_reason": "sl_order_id_missing",
                "invalid_price_reason": "new_sl_missing_or_invalid",
            },
            "take_profit": {
                "order_key": "tp_order_id",
                "price_key": "new_tp",
                "update_method": self.order_modifier.update_take_profit,
                "missing_order_reason": "tp_order_id_missing",
                "invalid_price_reason": "new_tp_missing_or_invalid",
            },
        }

        sequence_status = self._risk_update_sequence_status(signal)
        detail["risk_update_sequence"] = sequence_status
        if sequence_status.get("guarded"):
            detail["adjust_bracket"] = "skipped_stale_seq"
            detail["result_status"] = "stale_risk_update"
            return {
                "ok": True,
                "ack_status": "confirmed",
                "reason": "risk_update_seq_stale",
                "detail": detail,
            }

        preflight = self._execution_preflight(signal, "adjust_bracket")
        detail["execution_preflight"] = preflight
        if not preflight.get("ok"):
            return self._mark_invalidated(
                detail,
                str(preflight.get("reason") or "real_child_order_required_for_adjust"),
                ack_status=str(preflight.get("ack_status") or "expired"),
                action="adjust_bracket",
                real_order_required=True,
                real_order_confirmed=preflight.get("real_order_confirmed"),
                order_ids=preflight.get("order_ids"),
                requested_child_order_ids=preflight.get("requested_child_order_ids"),
                trade_group_id=preflight.get("trade_group_id"),
                origin_signal_id=preflight.get("origin_signal_id"),
                origin_execution_status=preflight.get("origin_execution_status"),
                order_linkage_status=preflight.get("order_linkage_status"),
                pb_order_statuses=preflight.get("pb_order_statuses"),
                gateway_request_blocked=True,
            )

        explicit_requested_sides, has_explicit_requested_sides, requested_sides_source = self._requested_adjust_sides(signal)
        if has_explicit_requested_sides:
            detail["requested_sides"] = sorted(explicit_requested_sides)
        detail["requested_sides_source"] = requested_sides_source
        detail["requested_sides_explicit"] = has_explicit_requested_sides

        results: Dict[str, Dict[str, Any]] = {}
        side_inputs: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], Any, str, float, bool, bool]] = {}
        inferred_requested_sides: List[str] = []
        valid_prices = 0

        for side, spec in side_specs.items():
            raw_price = self._signal_value(signal, spec["price_key"], None)
            order_id = str(self._signal_value(signal, spec["order_key"]) or "").strip()
            price = self._coerce_adjust_price(raw_price)
            price_valid = price > 0
            has_price_field = self._signal_has_value(signal, spec["price_key"])
            requested = side in explicit_requested_sides if has_explicit_requested_sides else has_price_field
            if requested and not has_explicit_requested_sides:
                inferred_requested_sides.append(side)
            if requested and price_valid:
                valid_prices += 1
            side_detail = {
                "requested": requested,
                "order_id": order_id,
                "new_price": price,
                "price_valid": price_valid,
                "price_key": spec["price_key"],
                "order_key": spec["order_key"],
                "price_field_present": has_price_field,
            }
            side_inputs[side] = (spec, side_detail, raw_price, order_id, price, price_valid, requested)

            if side == "stop_loss" and requested and price_valid:
                stop_guard = self._stop_widen_check(signal, price)
                side_detail["stop_widen_guard"] = stop_guard
                if stop_guard.get("blocked"):
                    side_detail.update({"ok": False, "skipped": True, "reason": "stop_widen_blocked"})
                    results = {
                        name: dict(item[1])
                        for name, item in side_inputs.items()
                    }
                    for name, item in side_inputs.items():
                        if name in results:
                            continue
                        results[name] = dict(item[1])
                    detail["adjust_bracket"] = "blocked"
                    detail["adjust_results"] = results
                    detail["adjust_bracket_result"] = {
                        "attempted_sides": [],
                        "succeeded_sides": [],
                        "failed_sides": ["stop_loss"],
                    }
                    blocked_reason = (
                        "order_flow_stop_widen_blocked"
                        if stop_guard.get("guard_key") == "never_widen_stop_by_order_flow"
                        else "risk_update_stop_widen_blocked"
                    )
                    return self._mark_blocked(
                        detail,
                        blocked_reason,
                        guard_key=stop_guard.get("guard_key"),
                        direction=stop_guard.get("direction"),
                        current_stop=stop_guard.get("current_stop"),
                        new_stop=price,
                    )

        if not has_explicit_requested_sides:
            detail["inferred_requested_sides"] = inferred_requested_sides

        missing_order_sides = [
            side
            for side, (_, _, _, order_id, _, price_valid, requested) in side_inputs.items()
            if requested and price_valid and not order_id
        ]
        if missing_order_sides:
            resolution = self._resolve_adjust_bracket_child_order_ids(signal, missing_order_sides)
            detail["child_order_resolution"] = resolution.get("detail", {})
            for side, order_id in (resolution.get("resolved") or {}).items():
                if side not in side_inputs:
                    continue
                spec, side_detail, raw_price, _old_order_id, price, price_valid, requested = side_inputs[side]
                side_detail["order_id"] = order_id
                side_detail["order_id_source"] = (resolution.get("detail", {}).get("sources") or {}).get(side, "")
                side_inputs[side] = (spec, side_detail, raw_price, order_id, price, price_valid, requested)

            unresolved_sides = list(resolution.get("unresolved_sides") or [])
            if unresolved_sides and self._is_tv_risk_update_signal(signal):
                resolution_detail = resolution.get("detail", {})
                local_linkage_exists = bool(resolution.get("linkage_exists"))
                linkage_hint_exists = bool(resolution.get("linkage_hint_exists"))
                origin_execution_terminal = bool(resolution_detail.get("origin_execution_terminal"))
                if origin_execution_terminal:
                    block_reason = "risk_update_origin_order_not_active"
                    side_reason = "origin_order_not_active"
                elif local_linkage_exists:
                    block_reason = "risk_update_child_order_id_unresolved"
                    side_reason = "child_order_id_unresolved"
                elif linkage_hint_exists:
                    block_reason = "risk_update_local_order_linkage_missing"
                    side_reason = "local_order_linkage_missing"
                else:
                    block_reason = "risk_update_linkage_missing"
                    side_reason = "linkage_missing"
                order_linkage_status = str(
                    resolution_detail.get("order_linkage_status")
                    or (
                        "origin_order_not_active"
                        if origin_execution_terminal
                        else (
                            "child_order_id_unresolved"
                            if local_linkage_exists
                            else ("local_order_linkage_missing" if linkage_hint_exists else "missing")
                        )
                    )
                )
                should_defer, defer_status = self._should_defer_missing_child_order_resolution(signal, resolution)
                execution_readiness = "deferred" if should_defer else "not_executable"
                if should_defer and not local_linkage_exists:
                    blocked_message = (
                        "Risk update is deferred until local bracket order linkage is available; "
                        "Gateway lookup is blocked for TV-hint-only updates."
                    )
                elif should_defer:
                    blocked_message = "Risk update is deferred until bracket child order linkage is available."
                else:
                    blocked_message = "Risk update cannot execute without local bracket child order linkage."
                results = self._build_adjust_resolution_block_results(
                    side_inputs=side_inputs,
                    blocked_sides=unresolved_sides,
                    reason=side_reason,
                )
                detail["adjust_bracket"] = "blocked"
                detail["adjust_results"] = results
                detail["adjust_bracket_result"] = {
                    "attempted_sides": [],
                    "succeeded_sides": [],
                    "failed_sides": unresolved_sides,
                }
                detail["gateway_request_blocked"] = bool(resolution_detail.get("gateway_request_blocked"))
                detail["execution_readiness"] = execution_readiness
                detail["execution_blocked_reason"] = block_reason
                detail["execution_blocked_message"] = blocked_message
                detail["order_linkage_status"] = order_linkage_status
                detail["missing_child_order_defer"] = defer_status
                resolution_detail["execution_readiness"] = execution_readiness
                resolution_detail["execution_blocked_reason"] = block_reason
                resolution_detail["execution_blocked_message"] = blocked_message
                resolution_detail["order_linkage_status"] = order_linkage_status
                resolution_detail["missing_child_order_defer"] = defer_status
                if should_defer:
                    detail["adjust_bracket"] = "deferred"
                    detail["deferred"] = True
                    return self._mark_retryable_blocked(
                        detail,
                        block_reason,
                        missing_sides=unresolved_sides,
                        missing_order_keys=[side_specs[side]["order_key"] for side in unresolved_sides],
                        order_linkage_status=order_linkage_status,
                        gateway_request_blocked=bool(resolution_detail.get("gateway_request_blocked")),
                    )
                return self._mark_blocked(
                    detail,
                    block_reason,
                    missing_sides=unresolved_sides,
                    missing_order_keys=[side_specs[side]["order_key"] for side in unresolved_sides],
                    order_linkage_status=order_linkage_status,
                    gateway_request_blocked=bool(resolution_detail.get("gateway_request_blocked")),
                )

        if has_explicit_requested_sides and not explicit_requested_sides:
            for side, (_, side_detail, *_rest) in side_inputs.items():
                side_detail.update({"ok": True, "skipped": True, "reason": "not_requested"})
                results[side] = dict(side_detail)
            detail["adjust_bracket"] = "confirmed_noop"
            detail["adjust_results"] = results
            detail["adjust_bracket_result"] = {
                "attempted_sides": [],
                "succeeded_sides": [],
                "failed_sides": [],
            }
            detail["result_status"] = "ok"
            self._persist_risk_update_state(signal, detail, results)
            return {
                "ok": True,
                "ack_status": "confirmed",
                "reason": "adjust_bracket_noop",
                "detail": detail,
            }

        attempted_sides: List[str] = []
        succeeded_sides: List[str] = []
        failed_sides = []

        for side, (spec, side_detail, raw_price, order_id, price, price_valid, requested) in side_inputs.items():

            if not requested:
                side_detail.update({"ok": True, "skipped": True, "reason": "not_requested"})
                results[side] = side_detail
                continue
            if not price_valid:
                side_detail.update({"ok": False, "skipped": True, "reason": spec["invalid_price_reason"]})
                if raw_price not in (None, ""):
                    side_detail["raw_price"] = raw_price
                results[side] = side_detail
                failed_sides.append(side)
                continue
            if not order_id:
                side_detail.update({"ok": False, "skipped": True, "reason": spec["missing_order_reason"]})
                results[side] = side_detail
                failed_sides.append(side)
                continue

            attempted_sides.append(side)
            try:
                modify_result = spec["update_method"](order_id, price)
            except Exception as exc:
                modify_result = {"ok": False, "error": str(exc), "exception": type(exc).__name__}

            ok = bool((modify_result or {}).get("ok"))
            side_detail.update(
                {
                    "ok": ok,
                    "skipped": False,
                    "reason": "broker_update_submitted" if ok else "broker_update_failed",
                    "result": dict(modify_result or {}),
                }
            )
            if ok:
                confirmed, confirmation = self._confirm_adjust_child_order_target_price(
                    signal,
                    order_id=order_id,
                    side=side,
                    expected_price=price,
                    modify_result=modify_result,
                )
                side_detail["confirmation"] = confirmation
                side_detail["ok"] = bool(confirmed)
                side_detail["reason"] = "confirmed" if confirmed else "adjust_price_not_confirmed"
                if confirmed:
                    succeeded_sides.append(side)
                else:
                    failed_sides.append(side)
            else:
                failed_sides.append(side)
            results[side] = side_detail

        detail["adjust_bracket"] = "started"
        detail["adjust_results"] = results
        detail["adjust_bracket_result"] = {
            "attempted_sides": attempted_sides,
            "succeeded_sides": succeeded_sides,
            "failed_sides": failed_sides,
        }

        if valid_prices == 0:
            detail["adjust_bracket"] = "blocked"
            return self._mark_blocked(
                detail,
                "adjust_bracket_prices_missing_or_invalid",
                failed_sides=failed_sides,
            )
        if not attempted_sides:
            detail["adjust_bracket"] = "blocked"
            return self._mark_blocked(
                detail,
                "adjust_bracket_targets_missing_or_invalid",
                failed_sides=failed_sides,
            )
        if failed_sides:
            unconfirmed_sides = [
                side
                for side in failed_sides
                if str((results.get(side) or {}).get("reason") or "") == "adjust_price_not_confirmed"
            ]
            if unconfirmed_sides and len(unconfirmed_sides) == len(failed_sides):
                detail["adjust_bracket"] = "pending_confirmation"
                detail["adjust_bracket_result"]["unconfirmed_sides"] = unconfirmed_sides
                return self._mark_retryable_blocked(
                    detail,
                    "adjust_bracket_not_confirmed",
                    unconfirmed_sides=unconfirmed_sides,
                    succeeded_sides=succeeded_sides,
                )
            partial = bool(succeeded_sides)
            detail["adjust_bracket"] = "partial_failed" if partial else "failed"
            return self._mark_blocked(
                detail,
                "adjust_bracket_partial_failed" if partial else "adjust_bracket_failed",
                failed_sides=failed_sides,
                succeeded_sides=succeeded_sides,
            )

        detail["adjust_bracket"] = "confirmed"
        detail["result_status"] = "ok"
        self._persist_risk_update_state(signal, detail, results)
        return {
            "ok": True,
            "ack_status": "confirmed",
            "reason": "adjust_bracket_confirmed",
            "detail": detail,
        }

    @staticmethod
    def _coerce_adjust_price(value: Any) -> float:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return 0.0
        return price if math.isfinite(price) and price > 0 else 0.0

    def _related_trade_group_id(self, signal: dict) -> str:
        return str(
            self._signal_value(signal, "trade_group_id")
            or self._signal_value(signal, "bracket_group")
            or self._signal_value(signal, "entry_order_unique_id")
            or ""
        ).strip()

    def _related_order_ids_from_signal(self, signal: dict) -> List[str]:
        values = [
            self._signal_value(signal, "broker_order_id"),
            self._signal_value(signal, "order_id"),
            self._signal_value(signal, "target_order_id"),
        ]
        values.extend(self._signal_list(signal, "order_ids", "submitted_order_ids", "missing_order_ids"))
        return self._unique_nonempty(values)

    def _fetch_related_orders(
        self,
        *,
        trade_group_id: str,
        order_ids: List[str],
        environment: str,
    ) -> List[Dict[str, Any]]:
        if not self.pb_client:
            return []
        filters = []
        env_filter = self._escape_filter_value(environment)
        if trade_group_id:
            group_filter = self._escape_filter_value(str(trade_group_id))
            filters.extend(
                [
                    f'environment = "{env_filter}" && trade_group_id = "{group_filter}"',
                    f'environment = "{env_filter}" && entry_order_unique_id = "{group_filter}"',
                    f'environment = "{env_filter}" && bracket_group = "{group_filter}"',
                ]
            )
        for oid in order_ids or []:
            oid_filter = self._escape_filter_value(str(oid))
            filters.extend(
                [
                    f'environment = "{env_filter}" && broker_order_id = "{oid_filter}"',
                    f'environment = "{env_filter}" && order_id = "{oid_filter}"',
                ]
            )

        rows: List[Dict[str, Any]] = []
        seen = set()
        for filter_expr in filters:
            try:
                records = self.pb_client.get_records(
                    "orders",
                    filter=filter_expr,
                    per_page=100,
                )
            except Exception:
                records = []
            for record in records or []:
                key = str(record.get("id") or self._order_id(record) or json.dumps(record, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dict(record))
        return rows

    def _confirm_related_orders_inactive(
        self,
        *,
        trade_group_id: str,
        order_ids: List[str],
        environment: str,
        pb_orders: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        order_ids = self._unique_nonempty(order_ids or [])
        for attempt in range(1, REVERSE_CONFIRM_ATTEMPTS + 1):
            live_checked, live_orders, live_error = self._load_live_open_orders()
            if live_checked:
                active_live = self._matching_active_orders(live_orders, trade_group_id, order_ids)
                confirmation = {
                    "confirmed": not active_live,
                    "source": "broker_open_orders",
                    "attempts": attempt,
                    "order_ids": order_ids,
                    "trade_group_id": trade_group_id,
                    "active_order_ids": [self._order_id(order) for order in active_live],
                    "active_orders": active_live,
                }
                if not active_live:
                    return True, confirmation
                if attempt < REVERSE_CONFIRM_ATTEMPTS:
                    time.sleep(REVERSE_CONFIRM_POLL_SECONDS)
                    continue
                return False, confirmation

            current_pb_orders = pb_orders
            if current_pb_orders is None or attempt > 1:
                current_pb_orders = self._fetch_related_orders(
                    trade_group_id=trade_group_id,
                    order_ids=order_ids,
                    environment=environment,
                )
            active_pb = [order for order in current_pb_orders or [] if self._is_active_order(order)]
            confirmation = {
                "confirmed": bool(current_pb_orders) and not active_pb,
                "source": "pb_orders" if current_pb_orders else "unavailable",
                "attempts": attempt,
                "order_ids": order_ids,
                "trade_group_id": trade_group_id,
                "active_order_ids": [self._order_id(order) for order in active_pb],
                "active_orders": active_pb,
            }
            if live_error:
                confirmation["broker_error"] = live_error
            if confirmation["confirmed"]:
                return True, confirmation
            if attempt < REVERSE_CONFIRM_ATTEMPTS:
                time.sleep(REVERSE_CONFIRM_POLL_SECONDS)
        return False, confirmation

    @classmethod
    def _order_payload_candidates(cls, payload: Any, depth: int = 0) -> List[Dict[str, Any]]:
        if depth > 5 or payload in (None, ""):
            return []
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return []
        if isinstance(payload, (list, tuple)):
            rows: List[Dict[str, Any]] = []
            for item in payload:
                rows.extend(cls._order_payload_candidates(item, depth + 1))
            return rows
        if not isinstance(payload, dict):
            return []

        rows = [dict(payload)]
        extra = cls._as_dict(payload.get("extra"))
        if extra:
            rows.append(extra)
        for key in ("order", "payload", "result", "detail", "details", "confirm"):
            rows.extend(cls._order_payload_candidates(payload.get(key), depth + 1))
            if extra:
                rows.extend(cls._order_payload_candidates(extra.get(key), depth + 1))
        return rows

    @classmethod
    def _is_confirmable_active_child_order(cls, order: Dict[str, Any]) -> bool:
        return cls._order_status_key(order or {}) in REAL_ACTIVE_ORDER_STATUSES

    @classmethod
    def _order_matches_ref(cls, order: Dict[str, Any], order_id: str) -> bool:
        target = str(order_id or "").strip()
        return bool(target and target in set(cls._order_ref_values(order or {})))

    @classmethod
    def _order_price_match_detail(
        cls,
        order: Dict[str, Any],
        side: str,
        expected_price: float,
    ) -> Tuple[bool, Dict[str, Any]]:
        fields = ADJUST_PRICE_FIELDS_BY_SIDE.get(side, ())
        extra = cls._as_dict((order or {}).get("extra"))
        expected = float(expected_price or 0.0)
        for field in fields:
            raw = (order or {}).get(field)
            if raw in (None, ""):
                raw = extra.get(field)
            actual = cls._coerce_float(raw, 0.0)
            if actual > 0 and math.isfinite(actual) and abs(actual - expected) <= ADJUST_PRICE_MATCH_TOLERANCE:
                return True, {
                    "matched_field": field,
                    "actual_price": actual,
                    "expected_price": expected,
                    "tolerance": ADJUST_PRICE_MATCH_TOLERANCE,
                }
        return False, {
            "matched_field": "",
            "actual_price": 0.0,
            "expected_price": expected,
            "tolerance": ADJUST_PRICE_MATCH_TOLERANCE,
            "checked_fields": list(fields),
        }

    def _confirm_adjust_price_from_orders(
        self,
        orders: List[Dict[str, Any]],
        *,
        order_id: str,
        side: str,
        expected_price: float,
        source: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        inspected: List[Dict[str, Any]] = []
        for order in orders or []:
            if not self._order_matches_ref(order, order_id):
                continue
            active = self._is_confirmable_active_child_order(order)
            matched, price_detail = self._order_price_match_detail(order, side, expected_price)
            item = {
                "order_id": self._order_id(order),
                "status": self._order_status(order),
                "active": active,
                **price_detail,
            }
            inspected.append(item)
            if active and matched:
                return True, {
                    "confirmed": True,
                    "source": source,
                    "order_id": str(order_id),
                    "side": side,
                    **item,
                }
        return False, {
            "confirmed": False,
            "source": source,
            "order_id": str(order_id),
            "side": side,
            "expected_price": float(expected_price or 0.0),
            "inspected_orders": inspected,
        }

    def _confirm_adjust_child_order_target_price(
        self,
        signal: dict,
        *,
        order_id: str,
        side: str,
        expected_price: float,
        modify_result: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        order_id = str(order_id or "").strip()
        expected_price = self._coerce_adjust_price(expected_price)
        confirmation: Dict[str, Any] = {
            "confirmed": False,
            "order_id": order_id,
            "side": side,
            "expected_price": expected_price,
            "source": "unavailable",
        }
        if not order_id or expected_price <= 0:
            confirmation["reason"] = "order_id_or_expected_price_missing"
            return False, confirmation

        modify_candidates = self._order_payload_candidates(modify_result or {})
        confirmed, source_detail = self._confirm_adjust_price_from_orders(
            modify_candidates,
            order_id=order_id,
            side=side,
            expected_price=expected_price,
            source="modify_result",
        )
        if confirmed:
            return True, {**confirmation, **source_detail}
        if source_detail.get("inspected_orders"):
            confirmation["modify_result"] = source_detail

        runtime_environment = normalize_broker_mode(
            self._signal_value(signal, "broker_mode") or self._signal_value(signal, "environment"),
            self.environment,
        )
        trade_group_id = self._related_trade_group_id(signal)
        last_details: List[Dict[str, Any]] = []
        for attempt in range(1, REVERSE_CONFIRM_ATTEMPTS + 1):
            pb_orders = self._fetch_related_orders(
                trade_group_id="",
                order_ids=[order_id],
                environment=runtime_environment,
            )
            confirmed, pb_detail = self._confirm_adjust_price_from_orders(
                pb_orders,
                order_id=order_id,
                side=side,
                expected_price=expected_price,
                source="pb_orders",
            )
            pb_detail["attempts"] = attempt
            if confirmed:
                return True, {**confirmation, **pb_detail}
            if pb_detail.get("inspected_orders"):
                last_details.append(pb_detail)

            live_checked, live_orders, live_error = self._load_live_open_orders()
            live_detail: Dict[str, Any] = {
                "confirmed": False,
                "source": "broker_open_orders",
                "attempts": attempt,
                "live_open_orders_checked": bool(live_checked),
                "trade_group_id": trade_group_id,
            }
            if live_error:
                live_detail["broker_error"] = live_error
            if live_checked:
                confirmed, live_match_detail = self._confirm_adjust_price_from_orders(
                    live_orders,
                    order_id=order_id,
                    side=side,
                    expected_price=expected_price,
                    source="broker_open_orders",
                )
                live_detail.update(live_match_detail)
                if confirmed:
                    return True, {**confirmation, **live_detail}
            last_details.append(live_detail)
            if attempt < REVERSE_CONFIRM_ATTEMPTS:
                time.sleep(REVERSE_CONFIRM_POLL_SECONDS)

        confirmation["attempts"] = REVERSE_CONFIRM_ATTEMPTS
        confirmation["details"] = last_details[-3:]
        confirmation["reason"] = "active_child_order_target_price_not_confirmed"
        return False, confirmation

    def _load_live_open_orders(self) -> Tuple[bool, List[Dict[str, Any]], str]:
        broker = getattr(self.order_modifier, "broker", None) if self.order_modifier else None
        loader = getattr(broker, "list_open_orders", None)
        if not callable(loader):
            return False, [], ""
        try:
            try:
                orders = loader(include_all=True)
            except TypeError:
                orders = loader()
            return True, [dict(order) for order in (orders or [])], ""
        except Exception as exc:
            return False, [], str(exc)

    def _matching_active_orders(
        self,
        orders: List[Dict[str, Any]],
        trade_group_id: str,
        order_ids: List[str],
    ) -> List[Dict[str, Any]]:
        target_ids = {str(item or "").strip() for item in (order_ids or []) if str(item or "").strip()}
        target_group = str(trade_group_id or "").strip()
        matches = []
        for order in orders or []:
            refs = set(self._order_ref_values(order))
            group = self._order_trade_group(order)
            if target_ids and not (target_ids & refs):
                if not target_group or group != target_group:
                    continue
            elif target_group and group and group != target_group and not (target_ids & refs):
                continue
            elif not target_ids and target_group and group != target_group:
                continue
            if self._is_active_order(order):
                matches.append(dict(order))
        return matches

    def _get_positions_result(self) -> Dict[str, Any]:
        lifecycle = getattr(self, "order_lifecycle", None)
        if not lifecycle:
            return {
                "ok": False,
                "positions": [],
                "source": "broker_positions",
                "reason": "position_snapshot_unavailable",
                "error": "order_lifecycle_missing",
            }
        result_getter = getattr(lifecycle, "get_positions_result", None)
        try:
            if callable(result_getter):
                result = dict(result_getter() or {})
                result.setdefault("positions", [])
                result.setdefault("source", "broker_positions")
                result.setdefault("reason", "ok" if result.get("ok") else "position_snapshot_unavailable")
                result.setdefault("error", "" if result.get("ok") else "position_snapshot_unavailable")
                return result
            positions = list(lifecycle.get_positions() or [])
            return {
                "ok": True,
                "positions": positions,
                "source": "broker_positions",
                "reason": "ok",
                "error": "",
            }
        except Exception as exc:
            logger.warning("Failed to get positions for reverse confirmation: %s", exc)
            return {
                "ok": False,
                "positions": [],
                "source": "broker_positions",
                "reason": "position_snapshot_unavailable",
                "error": str(exc) or "positions_unavailable",
            }

    @staticmethod
    def _compact_position_snapshot_result(result: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(result or {})
        positions = payload.pop("positions", []) or []
        payload["position_count"] = len(positions) if isinstance(positions, list) else 0
        return payload

    def _notify_position_snapshot_unavailable(self, signal: dict, symbol: str, result: Dict[str, Any]) -> None:
        rid = str((signal or {}).get("id") or "").strip()
        alert_key = f"{self.environment}:{rid or symbol}:position_snapshot_unavailable"
        if alert_key in self._position_snapshot_alerted:
            return
        notifier = getattr(self.pb_client, "notify_system_event", None)
        if not callable(notifier):
            return
        self._position_snapshot_alerted.add(alert_key)
        error = str((result or {}).get("error") or "position_snapshot_unavailable")
        try:
            notifier(
                "TV 平仓未执行：持仓快照不可用",
                {
                    "状态结论": "收到 TV close，但券商持仓快照不可用；系统未取消保护单、未提交平仓单，保留 pending 等待重试。",
                    "标的": str(symbol or "-").upper(),
                    "ActionID": rid or "-",
                    "Broker模式": self.environment,
                    "原因": error,
                    "RetryAfter秒": (result or {}).get("retry_after_s", 0),
                    "AccountDataBackoff": (result or {}).get("account_data_backoff_reason") or error,
                    "处理建议": "检查 IBKR account data / positions 通道；恢复后该 close action 会重试，必要时人工核对持仓。",
                },
                event_type="alert",
                level="error",
                source="ibkr_compute",
                environment=self.environment,
                message_id=f"ibkr_reverse_close_position_snapshot_unavailable:{self.environment}:{rid or symbol}",
            )
        except Exception as exc:
            logger.debug("Reverse close position snapshot notification failed: %s", exc)

    def _get_positions(self) -> List[Dict[str, Any]]:
        result = self._get_positions_result()
        if not result.get("ok"):
            return []
        return list(result.get("positions") or [])

    def _position_quantity(self, symbol: str, positions: List[Dict[str, Any]]) -> float:
        target = str(symbol or "").upper()
        for pos in positions or []:
            pos_symbol = str(
                pos.get("ticker")
                or pos.get("symbol")
                or pos.get("contractDesc")
                or ""
            ).upper()
            if pos_symbol != target:
                continue
            return self._coerce_float(pos.get("position", pos.get("quantity", 0)), 0.0)
        return 0.0

    def _position_snapshot_for_symbol(self, symbol: str, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        target = str(symbol or "").upper()
        for pos in positions or []:
            if not isinstance(pos, dict):
                continue
            pos_symbol = str(pos.get("ticker") or pos.get("symbol") or pos.get("contractDesc") or "").upper()
            if pos_symbol == target:
                return dict(pos)
        return {}

    def _confirm_flat(self, symbol: str) -> Tuple[bool, Dict[str, Any]]:
        last_qty = 0.0
        for attempt in range(1, REVERSE_CONFIRM_ATTEMPTS + 1):
            positions_result = self._get_positions_result()
            if not positions_result.get("ok"):
                return False, {
                    "confirmed": False,
                    "source": "broker_positions",
                    "attempts": attempt,
                    "position_qty": None,
                    "reason": "position_snapshot_unavailable",
                    "error": str(positions_result.get("error") or "position_snapshot_unavailable"),
                    "retry_after_s": positions_result.get("retry_after_s"),
                    "account_data_backoff_reason": positions_result.get("account_data_backoff_reason"),
                }
            positions = list(positions_result.get("positions") or [])
            last_qty = self._position_quantity(symbol, positions)
            if last_qty == 0:
                return True, {
                    "confirmed": True,
                    "source": "broker_positions",
                    "attempts": attempt,
                    "position_qty": 0.0,
                }
            if attempt < REVERSE_CONFIRM_ATTEMPTS:
                time.sleep(REVERSE_CONFIRM_POLL_SECONDS)
        return False, {
            "confirmed": False,
            "source": "broker_positions",
            "attempts": REVERSE_CONFIRM_ATTEMPTS,
            "position_qty": last_qty,
        }

    def _resolve_conid(self, symbol: str, signal: Optional[dict] = None) -> Optional[int]:
        conid = self._signal_value(signal or {}, "conid", 0)
        try:
            if conid:
                return int(conid)
        except (TypeError, ValueError):
            pass
        if self.conid_resolver:
            return self.conid_resolver.resolve(symbol)
        return None

    def daily_reset(self):
        self._processed_ids.clear()
