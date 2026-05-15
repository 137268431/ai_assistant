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
            account_id=acct_id,
            order_ref_suffix=str(order_ref_suffix or ""),
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
            "quantity": int(quantity or 0),
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
            split_core_tactical_quantity,
        )

        settings = normalize_harvest_settings(settings or {})
        split = split_core_tactical_quantity(int(quantity or 0), settings)
        if not split.get("split"):
            result = self.place_bracket_order(
                conid=conid,
                symbol=symbol,
                direction=direction,
                quantity=int(quantity or 0),
                entry_price=entry_price,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                use_paper=use_paper,
                signal_id=signal_id,
                entry_order_type=entry_order_type,
                order_extra={
                    "harvest_managed": True,
                    "harvest_profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
                    "harvest_lot": "core",
                    "harvest_lot_fraction": 1.0,
                    "harvest_original_quantity": int(quantity or 0),
                    "harvest_state": {
                        "profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
                        "lot": "core",
                        "cycles": 0,
                        "partial_exited": False,
                    },
                },
                order_ref_suffix="core",
            )
            result["harvest_split"] = False
            result["harvest_profile"] = INTRADAY_VOLATILITY_HARVEST_PROFILE
            result["legs"] = [{"lot": "core", "quantity": int(quantity or 0), **result}]
            return result

        legs = []
        combined_order_ids: list[str] = []
        ok = True
        errors = []
        for lot_name, lot_qty in (("core", int(split["core"])), ("tactical", int(split["tactical"]))):
            if lot_qty <= 0:
                continue
            leg = self.place_bracket_order(
                conid=conid,
                symbol=symbol,
                direction=direction,
                quantity=lot_qty,
                entry_price=entry_price,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                use_paper=use_paper,
                signal_id=signal_id,
                entry_order_type=entry_order_type,
                order_extra={
                    "harvest_managed": True,
                    "harvest_profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
                    "harvest_lot": lot_name,
                    "harvest_lot_fraction": round(lot_qty / max(1, int(quantity or 0)), 6),
                    "harvest_original_quantity": int(quantity or 0),
                    "harvest_state": {
                        "profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
                        "lot": lot_name,
                        "cycles": 0,
                        "partial_exited": False,
                    },
                },
                order_ref_suffix=lot_name,
            )
            legs.append({"lot": lot_name, "quantity": lot_qty, **leg})
            combined_order_ids.extend(leg.get("order_ids") or [])
            if not leg.get("ok"):
                ok = False
                errors.append({"lot": lot_name, "error": leg.get("error") or "submit_failed"})

        primary = legs[0] if legs else {}
        all_legs_ok = bool(legs) and ok
        failed_order_ids = [
            order_id
            for leg in legs
            if not leg.get("ok")
            for order_id in (leg.get("order_ids") or [])
        ]
        return {
            "ok": all_legs_ok,
            "harvest_split": True,
            "harvest_profile": INTRADAY_VOLATILITY_HARVEST_PROFILE,
            "legs": legs,
            "order_ids": combined_order_ids,
            "bracket_group": str(primary.get("bracket_group") or ""),
            "oca_group": str(primary.get("oca_group") or ""),
            "entry_coid": str(primary.get("entry_coid") or ""),
            "tp_coid": str(primary.get("tp_coid") or ""),
            "sl_coid": str(primary.get("sl_coid") or ""),
            "protection_complete": all_legs_ok and all(bool(item.get("protection_complete")) for item in legs),
            "protection_incomplete": (bool(legs) and not all_legs_ok) or any(bool(item.get("protection_incomplete")) for item in legs),
            "missing_order_ids": failed_order_ids,
            "missing_protection_roles": [
                role
                for leg in legs
                for role in (leg.get("missing_protection_roles") or [])
            ],
            "error": "; ".join(item["error"] for item in errors) if errors else None,
            "partial_submit_errors": errors,
        }

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
    ) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        symbol = str(symbol or "").upper()
        direction = str(direction or "").lower()
        close_order_ref = f"close_{symbol}_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        logger.info(
            "Placing market close: %s %s qty=%s account=%s",
            symbol,
            direction,
            quantity,
            acct_id or "-",
        )
        result = self.broker.place_market_close(
            conid=int(conid or 0),
            symbol=symbol,
            direction=direction,
            quantity=int(quantity or 0),
            account_id=acct_id,
            order_ref=close_order_ref,
        )
        if result.get("ok"):
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
                "conid": kwargs.get("conid", 0),
                "direction": direction,
                "position_side": direction,
                "quantity": kwargs.get("quantity", 0),
                "limit_price": 0,
                "status": "Submitted",
                "order_type": "MKT",
                "unique_id": close_coid or broker_order_id,
                "order_id": broker_order_id,
                "broker_order_id": broker_order_id,
                "trade_group_id": trade_group_id,
                "entry_order_unique_id": entry_order_unique_id,
                "parent_order_unique_id": entry_order_unique_id if entry_order_unique_id != (close_coid or broker_order_id) else "",
                "sibling_order_unique_id": "",
                "role": "close",
                "relation_status": "active",
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
            oca_group = str(kwargs.get("oca_group") or bracket_group or "").strip()
            order_family_type = str(kwargs.get("order_family_type") or "bracket_oco").strip()
            order_extra = dict(kwargs.get("order_extra") or {})
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
