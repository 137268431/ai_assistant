"""
IB Gateway bracket and close order placement.
"""

from __future__ import annotations

import logging
import os
import json
import time
from datetime import datetime
from ibkr_compute.core.broker_mode import normalize_broker_mode, resolve_market_data_mode
from ibkr_compute.core.time_utils import ET
from typing import Any, Dict, List

from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.observability.prometheus import record_order_event

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
PAPER_ACCOUNT_ID = os.environ.get("IBKR_PAPER_ACCOUNT_ID", "")


class OrderPlacer:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()
        self._order_count = 0
        self._suppression_attempted = False
        self._suppression_enabled = False
        self._suppression_message_ids: List[str] = []

    def get_active_account_id(self, use_paper: bool = False) -> str:
        if use_paper:
            return PAPER_ACCOUNT_ID or self.account_id
        return self.account_id

    @staticmethod
    def _coerce_extra(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _escape_filter_value(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _broker_mode(self) -> str:
        return normalize_broker_mode(self.environment, "paper")

    def _data_environment(self) -> str:
        try:
            return str(resolve_market_data_mode(None) or "live").strip().lower() or "live"
        except Exception:
            return "live"

    def _record_bracket_order_metrics(
        self,
        *,
        result: Dict[str, Any],
        order_family_type: str,
        duration_s: float,
    ) -> None:
        payload = dict(result or {})
        family = str(order_family_type or payload.get("order_family_type") or "bracket_oco").strip() or "bracket_oco"
        missing_roles = {
            str(role or "").strip()
            for role in (payload.get("missing_protection_roles") or [])
            if str(role or "").strip()
        }
        result_ok = bool(payload.get("ok"))
        protection_incomplete = bool(payload.get("protection_incomplete")) or (
            "protection_complete" in payload and not bool(payload.get("protection_complete"))
        )
        reason = str(payload.get("error") or payload.get("reason") or "ok")
        bracket_result = "ok" if result_ok and not protection_incomplete else "incomplete" if result_ok else "error"
        record_order_event(
            environment=self.environment,
            operation="place_bracket",
            order_family_type=family,
            result=bracket_result,
            reason_code="protection_incomplete" if protection_incomplete and result_ok else reason,
            duration_s=duration_s,
        )
        role_results = [
            ("place_entry", "entry"),
            ("place_take_profit", "take_profit"),
            ("place_stop_loss", "stop_loss"),
        ]
        for operation, role in role_results:
            if role in missing_roles:
                role_result = "missing"
                role_reason = "protection_missing"
            elif role != "entry" and protection_incomplete:
                role_result = "unconfirmed"
                role_reason = "protection_incomplete"
            else:
                role_result = "ok" if result_ok else "error"
                role_reason = reason
            record_order_event(
                environment=self.environment,
                operation=operation,
                order_family_type=role,
                result=role_result,
                reason_code=role_reason,
                duration_s=duration_s,
            )

    def _signal_lookup_environments(self) -> List[str]:
        candidates: List[str] = []
        for value in (self._data_environment(), self.environment, self._broker_mode()):
            text = str(value or "").strip().lower()
            if text and text not in candidates:
                candidates.append(text)
        return candidates

    def _load_origin_signal_record_and_extra(self, signal_id: str) -> tuple[dict, Dict[str, Any]]:
        signal_id = str(signal_id or "").strip()
        getter = getattr(self.pb_client, "get_first_record", None) if self.pb_client else None
        if not signal_id or not callable(getter):
            return {}, {}
        safe_signal_id = self._escape_filter_value(signal_id)
        for environment in self._signal_lookup_environments():
            try:
                record = getter(
                    "ibkr_signals",
                    filter=(
                        f'signal_id = "{safe_signal_id}" && '
                        f'environment = "{self._escape_filter_value(environment)}"'
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "Failed to look up origin signal for bracket linkage: signal_id=%s environment=%s error=%s",
                    signal_id,
                    environment,
                    exc,
                )
                continue
            if isinstance(record, dict) and record:
                return dict(record), self._coerce_extra(record.get("extra"))
        return {}, {}

    def _resolve_origin_trade_group_id(
        self,
        *,
        trade_group_id: str = "",
        signal_id: str = "",
        order_extra: Dict[str, Any] | None = None,
    ) -> str:
        candidates = [
            trade_group_id,
            (order_extra or {}).get("trade_group_id"),
            (order_extra or {}).get("bracket_group"),
        ]
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text
        record, extra = self._load_origin_signal_record_and_extra(signal_id)
        broker_execution = {}
        execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
        if isinstance(execution_by_mode, dict):
            broker_execution = execution_by_mode.get(self._broker_mode()) or {}
        if not isinstance(broker_execution, dict):
            broker_execution = {}
        for value in (
            record.get("trade_group_id"),
            record.get("bracket_group"),
            extra.get("trade_group_id"),
            extra.get("bracket_group"),
            broker_execution.get("trade_group_id"),
            broker_execution.get("bracket_group"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _resolve_origin_order_linkage(
        self,
        *,
        signal_id: str = "",
        origin_signal_id: str = "",
        trade_group_id: str = "",
        bracket_group: str = "",
        entry_order_unique_id: str = "",
    ) -> Dict[str, str]:
        resolved_signal_id = str(origin_signal_id or signal_id or "").strip()
        record, extra = self._load_origin_signal_record_and_extra(resolved_signal_id)
        execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
        broker_execution = {}
        if isinstance(execution_by_mode, dict):
            broker_execution = execution_by_mode.get(self._broker_mode()) or {}
            if not isinstance(broker_execution, dict):
                broker_execution = {}
            if not broker_execution:
                for payload in execution_by_mode.values():
                    if isinstance(payload, dict):
                        broker_execution = dict(payload)
                        break

        group_candidates = (
            trade_group_id,
            bracket_group,
            record.get("trade_group_id") if isinstance(record, dict) else "",
            record.get("bracket_group") if isinstance(record, dict) else "",
            extra.get("trade_group_id") if isinstance(extra, dict) else "",
            extra.get("bracket_group") if isinstance(extra, dict) else "",
            broker_execution.get("trade_group_id") if isinstance(broker_execution, dict) else "",
            broker_execution.get("bracket_group") if isinstance(broker_execution, dict) else "",
        )
        resolved_trade_group = next((str(value or "").strip() for value in group_candidates if str(value or "").strip()), "")

        entry_candidates = (
            entry_order_unique_id,
            broker_execution.get("entry_order_unique_id") if isinstance(broker_execution, dict) else "",
            broker_execution.get("entry_coid") if isinstance(broker_execution, dict) else "",
            extra.get("entry_order_unique_id") if isinstance(extra, dict) else "",
            extra.get("entry_coid") if isinstance(extra, dict) else "",
            record.get("entry_order_unique_id") if isinstance(record, dict) else "",
            resolved_trade_group,
        )
        resolved_entry_unique_id = next((str(value or "").strip() for value in entry_candidates if str(value or "").strip()), "")
        return {
            "signal_id": resolved_signal_id,
            "trade_group_id": resolved_trade_group,
            "bracket_group": str(bracket_group or resolved_trade_group or "").strip(),
            "entry_order_unique_id": resolved_entry_unique_id,
        }

    def place_bracket_order(
        self,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        use_paper: bool = False,
        signal_id: str = "",
        entry_order_type: str = "LMT",
        order_extra: Dict[str, Any] | None = None,
        order_ref_suffix: str = "",
        trade_group_id: str = "",
        bracket_group: str = "",
        take_profit_quantity: int | None = None,
        stop_loss_quantity: int | None = None,
        order_family_type: str = "",
        entry_algo_strategy: str = "",
        entry_adaptive_priority: str = "",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        acct_id = self.get_active_account_id(use_paper)
        order_extra_payload = dict(order_extra or {})
        resolved_trade_group_id = self._resolve_origin_trade_group_id(
            trade_group_id=str(trade_group_id or bracket_group or "").strip(),
            signal_id=signal_id,
            order_extra=order_extra_payload,
        )
        entry_quantity = int(quantity or 0)
        tp_quantity = int(take_profit_quantity if take_profit_quantity is not None else entry_quantity)
        sl_quantity = int(stop_loss_quantity if stop_loss_quantity is not None else entry_quantity)
        resolved_family_type = str(order_family_type or "").strip() or (
            "bracket_oco" if tp_quantity == entry_quantity and sl_quantity == entry_quantity else "partial_harvest_bracket"
        )
        logger.info(
            "Placing bracket order: %s %s qty=%s tp_qty=%s sl_qty=%s entry=%s tp=%s sl=%s account=%s",
            symbol,
            direction,
            entry_quantity,
            tp_quantity,
            sl_quantity,
            entry_price,
            take_profit_price,
            stop_loss_price,
            acct_id or "-",
        )
        result = self.broker.place_bracket_order(
            conid=int(conid or 0),
            symbol=str(symbol or "").upper(),
            direction=str(direction or "").lower(),
            quantity=entry_quantity,
            entry_price=float(entry_price or 0.0),
            take_profit_price=float(take_profit_price or 0.0),
            stop_loss_price=float(stop_loss_price or 0.0),
            take_profit_quantity=tp_quantity,
            stop_loss_quantity=sl_quantity,
            entry_order_type=str(entry_order_type or "LMT").upper(),
            account_id=acct_id,
            order_ref_suffix=str(order_ref_suffix or ""),
            trade_group_id=resolved_trade_group_id,
            order_family_type=resolved_family_type,
            entry_algo_strategy=str(entry_algo_strategy or ""),
            entry_adaptive_priority=str(entry_adaptive_priority or ""),
        )
        self._record_bracket_order_metrics(
            result=result,
            order_family_type=result.get("order_family_type") or resolved_family_type,
            duration_s=time.perf_counter() - started,
        )
        if result.get("ok"):
            self._order_count += 1
            broker_order_extra = dict(order_extra_payload)
            if isinstance(result.get("price_normalization"), dict) and result.get("price_normalization"):
                broker_order_extra["broker_price_normalization"] = dict(result.get("price_normalization") or {})
            returned_trade_group_id = str(
                result.get("trade_group_id")
                or result.get("bracket_group")
                or resolved_trade_group_id
                or ""
            ).strip()
            self._log_order_to_pb(
                symbol=str(symbol or "").upper(),
                conid=int(conid or 0),
                direction=str(direction or "").lower(),
                entry_coid=result.get("entry_coid") or "",
                tp_coid=result.get("tp_coid") or "",
                sl_coid=result.get("sl_coid") or "",
                entry_price=float(result.get("entry_price") or entry_price or 0.0),
                tp_price=float(result.get("take_profit_price") or take_profit_price or 0.0),
                sl_price=float(result.get("stop_loss_price") or stop_loss_price or 0.0),
                quantity=entry_quantity,
                take_profit_quantity=int(result.get("take_profit_quantity") or tp_quantity),
                stop_loss_quantity=int(result.get("stop_loss_quantity") or sl_quantity),
                signal_id=signal_id,
                account=acct_id,
                order_ids=result.get("order_ids") or [],
                trade_group_id=returned_trade_group_id,
                bracket_group=result.get("bracket_group") or "",
                oca_group=result.get("oca_group") or "",
                order_family_type=result.get("order_family_type") or resolved_family_type,
                order_extra=broker_order_extra,
                protection_complete=result.get("protection_complete"),
            )
        missing_order_ids = [
            str(item or "").strip()
            for item in (result.get("missing_order_ids") or [])
            if str(item or "").strip()
        ]
        protection_complete = bool(result.get("protection_complete"))
        protection_incomplete = bool(missing_order_ids) or (
            "protection_complete" in result and not protection_complete and bool(result.get("order_ids"))
        )
        returned_family_type = str(
            result.get("order_family_type")
            or resolved_family_type
            or ("bracket_oco" if result.get("bracket_group") else "")
        )
        returned_bracket_group = str(
            result.get("bracket_group")
            or result.get("trade_group_id")
            or resolved_trade_group_id
            or result.get("entry_coid")
            or ""
        )
        returned_oca_group = str(
            result.get("oca_group")
            or (returned_bracket_group if returned_family_type == "bracket_oco" else "")
            or ""
        )
        return {
            "ok": bool(result.get("ok")),
            "entry_coid": str(result.get("entry_coid") or ""),
            "tp_coid": str(result.get("tp_coid") or ""),
            "sl_coid": str(result.get("sl_coid") or ""),
            "bracket_group": returned_bracket_group,
            "trade_group_id": str(
                result.get("trade_group_id")
                or returned_bracket_group
            ),
            "oca_group": returned_oca_group,
            "order_family_type": returned_family_type,
            "order_ids": [str(item or "").strip() for item in (result.get("order_ids") or []) if str(item or "").strip()],
            "error": result.get("error"),
            "entry_error": result.get("entry_error"),
            "submission": result.get("submission"),
            "protection_complete": protection_complete,
            "protection_incomplete": protection_incomplete,
            "missing_order_ids": missing_order_ids,
            "missing_protection_roles": list(result.get("missing_protection_roles") or []),
            "protection_order_statuses": dict(result.get("protection_order_statuses") or {}),
            "protection_orders_checked": int(result.get("protection_orders_checked") or 0),
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry"
            if protection_incomplete
            else "",
            "safe_action": "diagnostic_only_no_broker_call" if protection_incomplete else "",
            "quantity": entry_quantity,
            "take_profit_quantity": int(result.get("take_profit_quantity") or tp_quantity),
            "stop_loss_quantity": int(result.get("stop_loss_quantity") or sl_quantity),
            "order_extra": dict(order_extra_payload),
            "price_normalization": dict(result.get("price_normalization") or {}),
            "entry_algo_strategy": str(result.get("entry_algo_strategy") or entry_algo_strategy or ""),
            "entry_adaptive_priority": str(result.get("entry_adaptive_priority") or entry_adaptive_priority or ""),
            "raw_response": result.get("raw"),
        }

    def place_harvest_bracket_order(
        self,
        *,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        use_paper: bool = False,
        signal_id: str = "",
        trade_group_id: str = "",
        bracket_group: str = "",
        settings: Dict[str, Any] | None = None,
        entry_order_type: str = "LMT",
    ) -> Dict[str, Any]:
        from ibkr_compute.core.intraday_harvest import (
            INTRADAY_VOLATILITY_HARVEST_PROFILE,
            normalize_harvest_settings,
        )

        settings = normalize_harvest_settings(settings or {})
        trade_group_id = str(trade_group_id or settings.get("trade_group_id") or "").strip()
        bracket_group = str(bracket_group or settings.get("bracket_group") or "").strip()
        total_qty = max(0, int(quantity or 0))
        tactical_fraction = float(settings.get("tactical_fraction") or 0.30)
        partial_tp_qty = int(round(total_qty * tactical_fraction)) if total_qty >= 2 else total_qty
        partial_tp_qty = max(1, min(total_qty, partial_tp_qty)) if total_qty > 0 else 0
        partial_harvest = total_qty >= 2 and partial_tp_qty < total_qty
        order_extra = {
            "harvest_managed": True,
            "harvest_profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
            "harvest_lot": "primary",
            "harvest_lot_fraction": 1.0,
            "harvest_original_quantity": total_qty,
            "partial_harvest_managed": partial_harvest,
            "partial_tp_fraction": round(partial_tp_qty / max(1, total_qty), 6) if partial_harvest else 1.0,
            "partial_tp_quantity": partial_tp_qty,
            "initial_stop_quantity": total_qty,
            "reentry_allowed": partial_harvest,
            "harvest_state": {
                "profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
                "lot": "primary",
                "cycles": 0,
                "partial_exited": False,
            },
        }
        result = self.place_bracket_order(
            conid=conid,
            symbol=symbol,
            direction=direction,
            quantity=total_qty,
            take_profit_quantity=partial_tp_qty,
            stop_loss_quantity=total_qty,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            use_paper=use_paper,
            signal_id=signal_id,
            entry_order_type=entry_order_type,
            order_extra=order_extra,
            order_ref_suffix="harvest",
            trade_group_id=trade_group_id,
            bracket_group=bracket_group,
            order_family_type="partial_harvest_bracket" if partial_harvest else "bracket_oco",
        )
        result["harvest_split"] = False
        result["partial_harvest"] = partial_harvest
        result["harvest_profile"] = INTRADAY_VOLATILITY_HARVEST_PROFILE
        result["partial_tp_quantity"] = partial_tp_qty
        result["remaining_after_partial_tp"] = max(0, total_qty - partial_tp_qty)
        return result

    def place_market_close(
        self,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        use_paper: bool = False,
        trade_group_id: str = "",
        entry_order_unique_id: str = "",
        signal_id: str = "",
        origin_signal_id: str = "",
        source: str = "",
        order_type: str = "MKT",
        limit_price: float = 0.0,
        position_snapshot: Dict[str, Any] | None = None,
        wait_for_fill: bool = False,
        fill_timeout: float = 5.0,
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        symbol = str(symbol or "").upper()
        direction = str(direction or "").lower()
        close_order_ref = f"close_{symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        linkage = self._resolve_origin_order_linkage(
            signal_id=signal_id,
            origin_signal_id=origin_signal_id,
            trade_group_id=trade_group_id,
            entry_order_unique_id=entry_order_unique_id,
        )
        resolved_trade_group_id = linkage.get("trade_group_id") or trade_group_id
        resolved_entry_order_unique_id = linkage.get("entry_order_unique_id") or entry_order_unique_id
        resolved_signal_id = linkage.get("signal_id") or signal_id
        logger.info(
            "Placing market close: %s %s qty=%s order_type=%s limit=%s account=%s",
            symbol,
            direction,
            quantity,
            order_type,
            limit_price,
            acct_id or "-",
        )
        result = self.broker.place_market_close(
            conid=int(conid or 0),
            symbol=symbol,
            direction=direction,
            quantity=int(quantity or 0),
            account_id=acct_id,
            order_ref=close_order_ref,
            order_type=order_type,
            limit_price=limit_price,
            wait_for_fill=wait_for_fill,
            fill_timeout=fill_timeout,
        )
        order_ids: List[str] = []
        for key in ("order_ids", "submitted_order_ids", "broker_order_ids", "submitted_broker_order_ids"):
            value = result.get(key)
            values = value if isinstance(value, (list, tuple, set)) else ([value] if value not in (None, "") else [])
            order_ids.extend(str(item or "").strip() for item in values if str(item or "").strip())
        close_coid = str(
            result.get("entry_coid")
            or result.get("bracket_group")
            or result.get("order_ref")
            or result.get("orderRef")
            or result.get("trade_group_id")
            or ""
        ).strip()
        submission_error = str(result.get("error") or "")
        submission_unconfirmed = bool(order_ids and close_coid and "order_submission_unconfirmed" in submission_error.lower())
        if result.get("ok") or result.get("submitted") or submission_unconfirmed:
            self._log_close_order_to_pb(
                symbol=symbol,
                conid=int(conid or 0),
                direction=direction,
                quantity=int(quantity or 0),
                close_coid=close_coid or close_order_ref,
                broker_order_id=order_ids[0] if order_ids else "",
                trade_group_id=resolved_trade_group_id,
                entry_order_unique_id=resolved_entry_order_unique_id,
                signal_id=resolved_signal_id,
                source=source,
                account=acct_id,
                order_type=str(result.get("order_type") or order_type or "MKT").upper(),
                limit_price=float(result.get("limit_price") or limit_price or 0.0),
                position_snapshot=position_snapshot,
                status="Filled" if bool(result.get("filled")) else "Submitted",
                result=result,
                submission_unconfirmed=submission_unconfirmed,
                submission_error=submission_error,
            )
        return result

    def _log_close_order_to_pb(self, **kwargs):
        if not self.pb_client or not hasattr(self.pb_client, "upsert_order"):
            return
        try:
            et_now = datetime.now(ET)
            us_time = et_now.strftime("%Y-%m-%d %H:%M:%S")
            close_coid = str(kwargs.get("close_coid") or "").strip()
            broker_order_id = str(kwargs.get("broker_order_id") or "").strip()
            symbol = str(kwargs.get("symbol") or "").strip().upper()
            direction = str(kwargs.get("direction") or "").strip().lower()
            trade_group_id = str(kwargs.get("trade_group_id") or "").strip()
            entry_order_unique_id = str(kwargs.get("entry_order_unique_id") or "").strip()
            if not trade_group_id:
                trade_group_id = entry_order_unique_id or close_coid
            if not entry_order_unique_id:
                entry_order_unique_id = trade_group_id or close_coid
            submission_unconfirmed = bool(kwargs.get("submission_unconfirmed"))
            position_snapshot = dict(kwargs.get("position_snapshot") or {})
            position_avg_cost = 0.0
            for key in ("avg_cost", "avgCost", "avg_price", "avgPrice", "average_cost", "averageCost"):
                try:
                    parsed = float(position_snapshot.get(key) or 0)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed:
                    position_avg_cost = abs(parsed)
                    break
            extra = {
                "source": str(kwargs.get("source") or "order_placer_market_close"),
                "account": kwargs.get("account") or "",
                "close_order": True,
                "close_order_unique_id": close_coid or broker_order_id,
                "linked_trade_group_id": trade_group_id,
                "linked_entry_order_unique_id": entry_order_unique_id,
                "submitted_via": "market_close",
                "harvest_managed": True,
                "harvest_lot": "close",
                "submission_unconfirmed": submission_unconfirmed,
                "order_submission_unconfirmed": submission_unconfirmed,
                "submission_error": str(kwargs.get("submission_error") or ""),
                "market_close_result": dict(kwargs.get("result") or {}),
            }
            if position_snapshot:
                extra["position_snapshot"] = position_snapshot
            if position_avg_cost > 0:
                extra["position_avg_cost"] = position_avg_cost
                extra["entry_price_for_pnl"] = position_avg_cost
            payload = {
                "symbol": symbol,
                "environment": self.environment,
                "conid": kwargs.get("conid", 0),
                "direction": direction,
                "position_side": direction,
                "quantity": kwargs.get("quantity", 0),
                "limit_price": kwargs.get("limit_price", 0) or 0,
                "status": str(kwargs.get("status") or "Submitted"),
                "order_type": str(kwargs.get("order_type") or "MKT").upper(),
                "unique_id": close_coid or broker_order_id,
                "order_id": broker_order_id,
                "broker_order_id": broker_order_id,
                "trade_group_id": trade_group_id,
                "entry_order_unique_id": entry_order_unique_id,
                "parent_order_unique_id": entry_order_unique_id if entry_order_unique_id != (close_coid or broker_order_id) else "",
                "sibling_order_unique_id": "",
                "role": "close",
                "relation_status": (
                    "closed"
                    if str(kwargs.get("status") or "").strip().upper() in {"FILLED", "EXECUTED", "CLOSED"}
                    else "active"
                ),
                "signal_id": str(kwargs.get("signal_id") or "").strip(),
                "bar_time_ms": int(et_now.timestamp() * 1000),
                "us_time": us_time,
                "cn_time": "",
                "extra": extra,
            }
            self.pb_client.upsert_order(payload)
        except Exception as exc:
            logger.debug("Failed to log close order to PB: %s", exc)

    def _log_order_to_pb(self, **kwargs):
        if not self.pb_client:
            return
        try:
            et_now = datetime.now(ET)
            us_time = et_now.strftime("%Y-%m-%d %H:%M:%S")
            direction = kwargs.get("direction")
            quantity = kwargs.get("quantity")
            entry_unique_id = kwargs.get("entry_coid")
            tp_unique_id = kwargs.get("tp_coid")
            sl_unique_id = kwargs.get("sl_coid")
            bracket_group = str(kwargs.get("bracket_group") or "").strip()
            if not bracket_group and str(entry_unique_id or "").startswith("entry_"):
                bracket_group = str(entry_unique_id or "")[len("entry_") :]
            trade_group_id = str(kwargs.get("trade_group_id") or "").strip() or bracket_group or entry_unique_id
            if not bracket_group and trade_group_id:
                bracket_group = str(trade_group_id or "").strip()
            order_family_type = str(kwargs.get("order_family_type") or "bracket_oco").strip()
            raw_oca_group = str(kwargs.get("oca_group") or "").strip()
            oca_group = raw_oca_group or (bracket_group if order_family_type == "bracket_oco" else "")
            order_extra = dict(kwargs.get("order_extra") or {})
            symbol = kwargs.get("symbol")
            signal_id = kwargs.get("signal_id", "")
            order_ids = [str(item or "").strip() for item in (kwargs.get("order_ids") or [])]
            entry_order_id = order_ids[0] if len(order_ids) > 0 else ""
            tp_order_id = order_ids[1] if len(order_ids) > 1 else ""
            sl_order_id = order_ids[2] if len(order_ids) > 2 else ""
            entry_quantity = int(quantity or 0)
            tp_quantity = int(kwargs.get("take_profit_quantity") or entry_quantity)
            sl_quantity = int(kwargs.get("stop_loss_quantity") or entry_quantity)

            if hasattr(self.pb_client, "upsert_order"):
                base_payload = {
                    "symbol": symbol,
                    "environment": self.environment,
                    "direction": direction,
                    "position_side": direction,
                    "trade_group_id": trade_group_id,
                    "bracket_group": bracket_group,
                    "oca_group": oca_group,
                    "order_family_type": order_family_type,
                    "entry_order_unique_id": entry_unique_id,
                    "signal_id": signal_id,
                    "us_time": us_time,
                    "bar_time_ms": int(et_now.timestamp() * 1000),
                    "extra": {
                        **order_extra,
                        "trade_group_id": trade_group_id,
                        "bracket_group": bracket_group,
                        "oca_group": oca_group,
                        "order_family_type": order_family_type,
                    },
                }
                self.pb_client.upsert_order({
                    **base_payload,
                    "conid": kwargs.get("conid", 0),
                    "unique_id": entry_unique_id,
                    "order_id": entry_order_id,
                    "broker_order_id": entry_order_id,
                    "order_type": "Entry",
                    "role": "entry",
                    "relation_status": "active",
                    "quantity": entry_quantity,
                    "limit_price": kwargs.get("entry_price"),
                    "status": "Submitted",
                    "filled_qty": 0,
                    "fill_price": 0,
                    "tp_price": kwargs.get("tp_price"),
                    "sl_price": kwargs.get("sl_price"),
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "conid": kwargs.get("conid", 0),
                    "unique_id": tp_unique_id,
                    "order_id": tp_order_id,
                    "broker_order_id": tp_order_id,
                    "order_type": "TakeProfit",
                    "role": "take_profit",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": sl_unique_id,
                    "quantity": tp_quantity,
                    "limit_price": kwargs.get("tp_price"),
                    "status": "Init",
                    "filled_qty": 0,
                    "fill_price": 0,
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "conid": kwargs.get("conid", 0),
                    "unique_id": sl_unique_id,
                    "order_id": sl_order_id,
                    "broker_order_id": sl_order_id,
                    "order_type": "StopLoss",
                    "role": "stop_loss",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": tp_unique_id,
                    "quantity": sl_quantity,
                    "limit_price": kwargs.get("sl_price"),
                    "status": "Init",
                    "filled_qty": 0,
                    "fill_price": 0,
                })
                self._update_origin_signal_execution_metadata(
                    signal_id=signal_id,
                    trade_group_id=trade_group_id,
                    bracket_group=bracket_group,
                    entry_order_id=entry_order_id,
                    tp_order_id=tp_order_id,
                    sl_order_id=sl_order_id,
                    entry_unique_id=entry_unique_id,
                    tp_unique_id=tp_unique_id,
                    sl_unique_id=sl_unique_id,
                    protection_complete=kwargs.get("protection_complete"),
                    oca_group=oca_group,
                    order_family_type=order_family_type,
                )
        except Exception as exc:
            logger.debug("PB order log failed: %s", exc)

    def _update_origin_signal_execution_metadata(
        self,
        *,
        signal_id: str,
        trade_group_id: str,
        bracket_group: str,
        entry_order_id: str,
        tp_order_id: str,
        sl_order_id: str,
        entry_unique_id: str,
        tp_unique_id: str,
        sl_unique_id: str,
        protection_complete: Any = None,
        oca_group: str = "",
        order_family_type: str = "",
    ) -> None:
        signal_id = str(signal_id or "").strip()
        updater = getattr(self.pb_client, "update_record", None) if self.pb_client else None
        if not signal_id or not callable(updater):
            return
        try:
            record, existing_extra = self._load_origin_signal_record_and_extra(signal_id)
            record_id = str((record or {}).get("id") or "").strip()
            if not record_id:
                return
            broker_mode = self._broker_mode()
            data_environment = str((record or {}).get("environment") or self._data_environment()).strip().lower() or "live"
            execution_by_mode = existing_extra.get("execution_by_mode")
            if not isinstance(execution_by_mode, dict):
                execution_by_mode = {}
            broker_execution = execution_by_mode.get(broker_mode)
            if not isinstance(broker_execution, dict):
                broker_execution = {}

            now_iso = datetime.now(ET).isoformat()
            linkage = {
                "trade_group_id": str(trade_group_id or "").strip(),
                "bracket_group": str(bracket_group or trade_group_id or "").strip(),
                "data_environment": data_environment,
                "source": "order_placer_bracket_log",
                "updated_at": now_iso,
            }
            optional_fields = {
                "entry_order_id": entry_order_id,
                "tp_order_id": tp_order_id,
                "sl_order_id": sl_order_id,
                "entry_order_unique_id": entry_unique_id,
                "tp_order_unique_id": tp_unique_id,
                "sl_order_unique_id": sl_unique_id,
                "entry_coid": entry_unique_id,
                "tp_coid": tp_unique_id,
                "sl_coid": sl_unique_id,
                "oca_group": oca_group,
                "order_family_type": order_family_type,
            }
            linkage.update(
                {
                    key: str(value or "").strip()
                    for key, value in optional_fields.items()
                    if str(value or "").strip()
                }
            )
            if protection_complete is not None:
                linkage["protection_complete"] = bool(protection_complete)

            execution_by_mode[broker_mode] = {**broker_execution, **linkage}
            extra_patch = {
                **existing_extra,
                "execution_by_mode": execution_by_mode,
                "last_runtime_broker_mode": broker_mode,
                "last_runtime_data_environment": data_environment,
                "last_order_linkage_update_at": now_iso,
            }
            for key in (
                "trade_group_id",
                "bracket_group",
                "entry_order_unique_id",
                "tp_order_unique_id",
                "sl_order_unique_id",
                "entry_coid",
                "tp_coid",
                "sl_coid",
            ):
                value = linkage.get(key)
                if value not in (None, ""):
                    extra_patch[key] = value
            updater("ibkr_signals", record_id, {"extra": extra_patch})
        except Exception as exc:
            logger.debug(
                "Failed to update origin signal bracket linkage: signal_id=%s error=%s",
                signal_id,
                exc,
            )

    def status(self) -> dict:
        return {
            "account_id": self.account_id,
            "total_orders": self._order_count,
            "question_suppression_enabled": self._suppression_enabled,
            "question_suppression_attempted": self._suppression_attempted,
            "question_suppression_message_ids": list(self._suppression_message_ids),
        }
