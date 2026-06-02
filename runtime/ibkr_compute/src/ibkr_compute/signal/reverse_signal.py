"""
执行动作处理
- close: 平仓
- cancel: 取消待成交订单
- adjust_sl: 调整止损
- adjust_tp: 调整止盈
- adjust_bracket: 同时或单边调整止损/止盈
"""

import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_compute.core.time_utils import ET

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
REVERSE_CONFIRM_ATTEMPTS = max(1, int(os.environ.get("IBKR_REVERSE_CONFIRM_ATTEMPTS", "3") or "3"))
REVERSE_CONFIRM_POLL_SECONDS = max(0.0, float(os.environ.get("IBKR_REVERSE_CONFIRM_POLL_SECONDS", "0.25") or 0.25))


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

    def check_and_process(self):
        try:
            records = self.pb_client.get_records(
                REVERSE_SIGNAL_COLLECTION,
                filter=f'status = "pending" && environment = "{self.environment}"',
                sort="-priority,-bar_time_ms",
                per_page=50,
            )

            for r in records:
                rid = r.get("id", "")
                if rid in self._processed_ids:
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
            logger.error("Reverse signal check failed: %s", e)

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

    @staticmethod
    def _order_status(order: Dict[str, Any]) -> str:
        return str(
            order.get("status")
            or order.get("order_status")
            or order.get("orderStatus")
            or order.get("state")
            or ""
        ).strip()

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
            detail["origin_signal_status_patch"] = {
                "signal_id": origin_signal_id,
                "status": "closed",
                "note": patch["note"],
            }
        except Exception as exc:
            detail["origin_signal_status_patch_error"] = str(exc)

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
        symbol = str(self._signal_value(signal, "symbol") or "").upper()
        logger.info("Processing execution action: %s %s", action, symbol)

        detail = self._base_reverse_detail(signal, action)
        if self._has_protection_incomplete(signal):
            return self._mark_blocked(
                detail,
                "protection_incomplete",
                protection_complete=False,
                safe_action="no_reentry_until_protection_reviewed",
            )

        if action == "close":
            return self._handle_close(signal, detail)
        if action == "cancel":
            return self._handle_cancel(signal, detail)
        if action == "adjust_sl":
            return self._handle_adjust_sl(signal, detail)
        if action == "adjust_tp":
            return self._handle_adjust_tp(signal, detail)
        if action == "adjust_bracket":
            return self._handle_adjust_bracket(signal, detail)

        return self._mark_blocked(detail, "unsupported_reverse_action", action=action)

    def _has_protection_incomplete(self, signal: dict) -> bool:
        extra = self._signal_extra(signal)
        status_values = {
            str(signal.get("status") or "").strip().lower(),
            str(extra.get("status") or "").strip().lower(),
            str(extra.get("relation_status") or "").strip().lower(),
            str(extra.get("target_state") or "").strip().lower(),
            str(extra.get("target_signal_status") or "").strip().lower(),
        }
        if "protection_incomplete" in status_values:
            return True
        for source in (signal, extra):
            if bool(source.get("protection_incomplete")):
                return True
            if "protection_complete" in source and source.get("protection_complete") is False:
                return True
        return False

    def _handle_close(self, signal: dict, detail: Dict[str, Any]) -> Dict[str, Any]:
        if not self.order_lifecycle or not self.order_placer:
            logger.warning("Order placer/lifecycle not configured for close")
            return self._mark_blocked(detail, "close_dependencies_missing")

        symbol = str(self._signal_value(signal, "symbol") or "").upper()
        conid = self._resolve_conid(symbol, signal)
        if not conid:
            return self._mark_blocked(detail, "conid_unresolved", symbol=symbol)

        cancel_result = self._cancel_old_order_if_present(signal, detail)
        if cancel_result is not None and not cancel_result.get("ok"):
            return cancel_result

        positions = self._get_positions()
        qty = self._position_quantity(symbol, positions)
        detail["position_qty_before_close"] = qty
        if qty == 0:
            detail["close_old_position"] = "skipped_flat"
            detail["wait_flat"] = "confirmed"
            detail["flat_confirmation"] = {
                "confirmed": True,
                "position_qty": 0.0,
                "source": "broker_positions",
            }
            return self._start_cooldown_and_ready(signal, symbol, detail, "already_flat_ready_reentry")

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
        result = self.order_placer.place_market_close(
            conid,
            symbol,
            direction,
            close_qty,
            trade_group_id=self._related_trade_group_id(signal),
            entry_order_unique_id=str(self._signal_value(signal, "entry_order_unique_id") or "").strip(),
            signal_id=origin_signal_id,
            source="reverse_signal_close",
        )
        detail["close_old_position"] = "submitted" if result.get("ok") else "failed"
        detail["close_result"] = dict(result or {})
        if not result.get("ok"):
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
            return self._mark_blocked(
                detail,
                "flat_not_confirmed",
                position_qty=flat_detail.get("position_qty"),
                attempts=flat_detail.get("attempts"),
            )

        detail["close_old_position"] = "confirmed"
        return self._start_cooldown_and_ready(signal, symbol, detail, "close_confirmed_ready_reentry")

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
                result = self.order_modifier.cancel_order(oid)
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
            return self._mark_blocked(
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
    def _is_order_flow_signal(cls, signal: dict) -> bool:
        return any("order_flow" in token for token in cls._source_tokens(signal))

    def _missing_child_order_retry_pending_enabled(self, signal: dict) -> bool:
        return self._is_tv_risk_update_signal(signal) and self._config_bool_prefer(
            ("tv_risk_update_missing_child_order_retry_pending", "tv_risk_update_retry_missing_child_orders"),
            True,
        )

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

        results: Dict[str, Dict[str, Any]] = {}
        side_inputs: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], Any, str, float, bool, bool]] = {}
        valid_prices = 0
        retryable_missing_order_sides: List[str] = []
        failed_sides: List[str] = []

        for side, spec in side_specs.items():
            raw_price = self._signal_value(signal, spec["price_key"], None)
            order_id = str(self._signal_value(signal, spec["order_key"]) or "").strip()
            price = self._coerce_adjust_price(raw_price)
            price_valid = price > 0
            if price_valid:
                valid_prices += 1

            requested = bool(order_id) or raw_price not in (None, "")
            side_detail = {
                "requested": requested,
                "order_id": order_id,
                "new_price": price,
                "price_valid": price_valid,
                "price_key": spec["price_key"],
                "order_key": spec["order_key"],
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

            if requested and price_valid and not order_id and self._missing_child_order_retry_pending_enabled(signal):
                side_detail.update({"ok": False, "skipped": True, "reason": spec["missing_order_reason"], "retryable": True})
                retryable_missing_order_sides.append(side)

        if retryable_missing_order_sides:
            for side, (_, side_detail, raw_price, _, price, price_valid, requested) in side_inputs.items():
                if side in retryable_missing_order_sides:
                    results[side] = dict(side_detail)
                    continue
                if not requested:
                    side_detail.update({"ok": True, "skipped": True, "reason": "not_requested"})
                elif not price_valid:
                    side_detail.update({"ok": False, "skipped": True, "reason": side_specs[side]["invalid_price_reason"]})
                    if raw_price not in (None, ""):
                        side_detail["raw_price"] = raw_price
                else:
                    side_detail.update({"ok": True, "skipped": True, "reason": "deferred_until_child_orders_resolved"})
                results[side] = dict(side_detail)
            detail["adjust_bracket"] = "pending_retry"
            detail["adjust_results"] = results
            detail["adjust_bracket_result"] = {
                "attempted_sides": [],
                "succeeded_sides": [],
                "failed_sides": retryable_missing_order_sides,
            }
            return self._mark_retryable_blocked(
                detail,
                "risk_update_child_order_id_missing",
                missing_sides=retryable_missing_order_sides,
                missing_order_keys=[side_specs[side]["order_key"] for side in retryable_missing_order_sides],
            )

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
                    "reason": "confirmed" if ok else "broker_update_failed",
                    "result": dict(modify_result or {}),
                }
            )
            results[side] = side_detail
            if ok:
                succeeded_sides.append(side)
            else:
                failed_sides.append(side)

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

    def _get_positions(self) -> List[Dict[str, Any]]:
        try:
            return list(self.order_lifecycle.get_positions() or [])
        except Exception as exc:
            logger.warning("Failed to get positions for reverse confirmation: %s", exc)
            return []

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

    def _confirm_flat(self, symbol: str) -> Tuple[bool, Dict[str, Any]]:
        last_qty = 0.0
        for attempt in range(1, REVERSE_CONFIRM_ATTEMPTS + 1):
            positions = self._get_positions()
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
