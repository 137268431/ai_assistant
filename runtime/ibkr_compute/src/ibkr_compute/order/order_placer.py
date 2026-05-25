"""
IB Gateway bracket and close order placement.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from ibkr_compute.core.time_utils import ET
from typing import Any, Dict, List

from ibkr_compute.broker import BrokerAdapter

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
        take_profit_quantity: int | None = None,
        stop_loss_quantity: int | None = None,
        order_family_type: str = "",
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
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
            order_family_type=resolved_family_type,
        )
        if result.get("ok"):
            self._order_count += 1
            self._log_order_to_pb(
                symbol=str(symbol or "").upper(),
                conid=int(conid or 0),
                direction=str(direction or "").lower(),
                entry_coid=result.get("entry_coid") or "",
                tp_coid=result.get("tp_coid") or "",
                sl_coid=result.get("sl_coid") or "",
                entry_price=float(entry_price or 0.0),
                tp_price=float(take_profit_price or 0.0),
                sl_price=float(stop_loss_price or 0.0),
                quantity=entry_quantity,
                take_profit_quantity=int(result.get("take_profit_quantity") or tp_quantity),
                stop_loss_quantity=int(result.get("stop_loss_quantity") or sl_quantity),
                signal_id=signal_id,
                account=acct_id,
                order_ids=result.get("order_ids") or [],
                bracket_group=result.get("bracket_group") or "",
                oca_group=result.get("oca_group") or "",
                order_family_type=result.get("order_family_type") or resolved_family_type,
                order_extra=dict(order_extra or {}),
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
        returned_oca_group = str(
            result.get("oca_group")
            or (result.get("bracket_group") if returned_family_type == "bracket_oco" else "")
            or ""
        )
        return {
            "ok": bool(result.get("ok")),
            "entry_coid": str(result.get("entry_coid") or ""),
            "tp_coid": str(result.get("tp_coid") or ""),
            "sl_coid": str(result.get("sl_coid") or ""),
            "bracket_group": str(result.get("bracket_group") or result.get("entry_coid") or ""),
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
            "order_extra": dict(order_extra or {}),
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
        settings: Dict[str, Any] | None = None,
        entry_order_type: str = "LMT",
    ) -> Dict[str, Any]:
        from ibkr_compute.core.intraday_harvest import (
            INTRADAY_VOLATILITY_HARVEST_PROFILE,
            normalize_harvest_settings,
        )

        settings = normalize_harvest_settings(settings or {})
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
        source: str = "",
        order_type: str = "MKT",
        limit_price: float = 0.0,
        wait_for_fill: bool = False,
        fill_timeout: float = 5.0,
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        symbol = str(symbol or "").upper()
        direction = str(direction or "").lower()
        close_order_ref = f"close_{symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
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
        if result.get("ok") or result.get("submitted"):
            self._log_close_order_to_pb(
                symbol=symbol,
                conid=int(conid or 0),
                direction=direction,
                quantity=int(quantity or 0),
                close_coid=str(result.get("entry_coid") or close_order_ref),
                broker_order_id=(result.get("order_ids") or [""])[0],
                trade_group_id=trade_group_id,
                entry_order_unique_id=entry_order_unique_id,
                signal_id=signal_id,
                source=source,
                account=acct_id,
                order_type=str(result.get("order_type") or order_type or "MKT").upper(),
                limit_price=float(result.get("limit_price") or limit_price or 0.0),
                status="Filled" if bool(result.get("filled")) else "Submitted",
                result=result,
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
                "extra": {
                    "source": str(kwargs.get("source") or "order_placer_market_close"),
                    "account": kwargs.get("account") or "",
                    "close_order": True,
                    "close_order_unique_id": close_coid or broker_order_id,
                    "linked_trade_group_id": trade_group_id,
                    "linked_entry_order_unique_id": entry_order_unique_id,
                    "submitted_via": "market_close",
                    "harvest_managed": True,
                    "harvest_lot": "close",
                    "market_close_result": dict(kwargs.get("result") or {}),
                },
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
            trade_group_id = bracket_group or entry_unique_id
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
                        "bracket_group": bracket_group,
                        "oca_group": oca_group,
                        "order_family_type": order_family_type,
                        **order_extra,
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
        except Exception as exc:
            logger.debug("PB order log failed: %s", exc)

    def status(self) -> dict:
        return {
            "account_id": self.account_id,
            "total_orders": self._order_count,
            "question_suppression_enabled": self._suppression_enabled,
            "question_suppression_attempted": self._suppression_attempted,
            "question_suppression_message_ids": list(self._suppression_message_ids),
        }
