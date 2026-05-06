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
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        logger.info(
            "Placing bracket order: %s %s qty=%s entry=%s tp=%s sl=%s account=%s",
            symbol,
            direction,
            quantity,
            entry_price,
            take_profit_price,
            stop_loss_price,
            acct_id or "-",
        )
        result = self.broker.place_bracket_order(
            conid=int(conid or 0),
            symbol=str(symbol or "").upper(),
            direction=str(direction or "").lower(),
            quantity=int(quantity or 0),
            entry_price=float(entry_price or 0.0),
            take_profit_price=float(take_profit_price or 0.0),
            stop_loss_price=float(stop_loss_price or 0.0),
            entry_order_type=str(entry_order_type or "LMT").upper(),
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
                quantity=int(quantity or 0),
                signal_id=signal_id,
                account=acct_id,
                order_ids=result.get("order_ids") or [],
                bracket_group=result.get("bracket_group") or "",
                oca_group=result.get("oca_group") or result.get("bracket_group") or "",
                order_family_type=result.get("order_family_type") or "bracket_oco",
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
        return {
            "ok": bool(result.get("ok")),
            "entry_coid": str(result.get("entry_coid") or ""),
            "tp_coid": str(result.get("tp_coid") or ""),
            "sl_coid": str(result.get("sl_coid") or ""),
            "bracket_group": str(result.get("bracket_group") or result.get("entry_coid") or ""),
            "oca_group": str(result.get("oca_group") or result.get("bracket_group") or ""),
            "order_family_type": str(
                result.get("order_family_type") or ("bracket_oco" if result.get("bracket_group") else "")
            ),
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
            "raw_response": result.get("raw"),
        }

    def place_market_close(
        self,
        conid: int,
        symbol: str,
        direction: str,
        quantity: int,
        use_paper: bool = False,
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        logger.info(
            "Placing market close: %s %s qty=%s account=%s",
            symbol,
            direction,
            quantity,
            acct_id or "-",
        )
        return self.broker.place_market_close(
            conid=int(conid or 0),
            symbol=str(symbol or "").upper(),
            direction=str(direction or "").lower(),
            quantity=int(quantity or 0),
        )

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
            oca_group = str(kwargs.get("oca_group") or bracket_group or "").strip()
            order_family_type = str(kwargs.get("order_family_type") or "bracket_oco").strip()
            symbol = kwargs.get("symbol")
            signal_id = kwargs.get("signal_id", "")
            order_ids = [str(item or "").strip() for item in (kwargs.get("order_ids") or [])]
            entry_order_id = order_ids[0] if len(order_ids) > 0 else ""
            tp_order_id = order_ids[1] if len(order_ids) > 1 else ""
            sl_order_id = order_ids[2] if len(order_ids) > 2 else ""

            if hasattr(self.pb_client, "upsert_order"):
                base_payload = {
                    "symbol": symbol,
                    "direction": direction,
                    "position_side": direction,
                    "trade_group_id": trade_group_id,
                    "bracket_group": bracket_group,
                    "oca_group": oca_group,
                    "order_family_type": order_family_type,
                    "entry_order_unique_id": entry_unique_id,
                    "quantity": quantity,
                    "signal_id": signal_id,
                    "us_time": us_time,
                    "bar_time_ms": int(et_now.timestamp() * 1000),
                    "extra": {
                        "bracket_group": bracket_group,
                        "oca_group": oca_group,
                        "order_family_type": order_family_type,
                    },
                }
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": entry_unique_id,
                    "order_id": entry_order_id,
                    "broker_order_id": entry_order_id,
                    "order_type": "Entry",
                    "role": "entry",
                    "relation_status": "active",
                    "limit_price": kwargs.get("entry_price"),
                    "status": "Submitted",
                    "filled_qty": 0,
                    "fill_price": 0,
                    "tp_price": kwargs.get("tp_price"),
                    "sl_price": kwargs.get("sl_price"),
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": tp_unique_id,
                    "order_id": tp_order_id,
                    "broker_order_id": tp_order_id,
                    "order_type": "TakeProfit",
                    "role": "take_profit",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": sl_unique_id,
                    "limit_price": kwargs.get("tp_price"),
                    "status": "Init",
                    "filled_qty": 0,
                    "fill_price": 0,
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": sl_unique_id,
                    "order_id": sl_order_id,
                    "broker_order_id": sl_order_id,
                    "order_type": "StopLoss",
                    "role": "stop_loss",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": tp_unique_id,
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
