from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

import requests


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
    _PB_SIGNAL_STATUS_ALIASES = {
        "cancelled": "rejected",
        "canceled": "rejected",
        "filled_position": "protected_active",
        "protection_reprice_failed": "protection_incomplete",
    }

    @staticmethod
    def _escape_filter_value(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _safe_extra(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def _pb_signal_status(cls, status: Any) -> str:
        normalized = str(status or "").strip().lower()
        return cls._PB_SIGNAL_STATUS_ALIASES.get(normalized, normalized)

    @classmethod
    def _annotate_pb_signal_status_alias(cls, extra: dict[str, Any], requested_status: Any) -> dict[str, Any]:
        payload = dict(extra or {})
        requested = str(requested_status or "").strip().lower()
        mapped = cls._pb_signal_status(requested)
        if requested and mapped != requested:
            payload.setdefault("signal_lifecycle_status", requested)
            payload["pb_signal_status"] = mapped
            payload["pb_signal_status_mapped_from"] = requested
        return payload

    @staticmethod
    def _order_role(order: dict[str, Any]) -> str:
        extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
        role = str(order.get("role") or extra.get("role") or order.get("order_type") or "").strip().lower()
        unique_id = str(order.get("unique_id") or order.get("cOID") or order.get("coid") or "").strip().lower()
        if not role and unique_id.startswith("tp_"):
            return "take_profit"
        if not role and unique_id.startswith("sl_"):
            return "stop_loss"
        if not role and unique_id.startswith("entry_"):
            return "entry"
        if unique_id.startswith("close_"):
            return "close"
        return role

    @staticmethod
    def _normalize_order_role(role: Any) -> str:
        normalized = str(role or "").strip().lower()
        if normalized in {"tp", "takeprofit", "take_profit", "profit_target", "target"}:
            return "take_profit"
        if normalized in {"sl", "stop", "stoploss", "stop_loss"}:
            return "stop_loss"
        if normalized in {"close", "manual_close", "market_close", "close_order", "reverse_close"}:
            return "close"
        return normalized

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

    def _resolve_signal_id_for_order(self, order: dict, runtime_environment: str) -> str:
        signal_id = str((order or {}).get("signal_id") or "").strip()
        if signal_id:
            return signal_id

        coid = str(
            (order or {}).get("cOID")
            or (order or {}).get("coid")
            or (order or {}).get("orderRef")
            or (order or {}).get("order_ref")
            or ""
        ).strip()
        order_id = str(
            (order or {}).get("orderId")
            or (order or {}).get("order_id")
            or (order or {}).get("broker_order_id")
            or ""
        ).strip()
        if not coid and not order_id:
            return ""

        filters = []
        env_filter = self._escape_filter_value(runtime_environment)
        if coid:
            safe_coid = self._escape_filter_value(coid)
            filters.append(f'unique_id = "{safe_coid}" && environment = "{env_filter}"')
            filters.append(f'entry_order_unique_id = "{safe_coid}" && environment = "{env_filter}"')
        if order_id:
            safe_order_id = self._escape_filter_value(order_id)
            filters.append(f'order_id = "{safe_order_id}" && environment = "{env_filter}"')
            filters.append(f'broker_order_id = "{safe_order_id}" && environment = "{env_filter}"')

        for filter_expr in filters:
            rows = self.pb.get_records("orders", filter=filter_expr, sort="-updated", per_page=1)
            if rows:
                signal_id = str((rows[0] or {}).get("signal_id") or "").strip()
                if signal_id:
                    return signal_id
        return ""

    @staticmethod
    def _first_nonempty_order_value(order: dict, *keys: str) -> Any:
        for key in keys:
            value = (order or {}).get(key)
            if value not in (None, ""):
                return value
        return ""

    def _now_iso_for_signal_patch(self) -> str:
        now_fn = getattr(self, "_now_iso", None)
        if callable(now_fn):
            try:
                return str(now_fn())
            except Exception:
                pass
        service_mod = _service_mod()
        return datetime.now(service_mod.ET).isoformat()

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _first_positive_order_float(self, order: dict, *keys: str) -> float:
        for key in keys:
            value = self._coerce_float((order or {}).get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _config_bool_for_environment(self, key: str, environment: str, default: bool) -> bool:
        config = getattr(self, "config", None)
        getter = getattr(config, "get_bool_for_environment", None)
        if callable(getter):
            try:
                return bool(getter(key, environment, default))
            except Exception:
                return default
        return default

    def _config_float_for_environment(self, key: str, environment: str, default: float) -> float:
        config = getattr(self, "config", None)
        getter = getattr(config, "get_float_for_environment", None)
        if callable(getter):
            try:
                return float(getter(key, environment, default))
            except Exception:
                return default
        getter = getattr(config, "get_for_environment", None)
        if callable(getter):
            try:
                return float(getter(key, environment, str(default)))
            except Exception:
                return default
        return default

    def _signal_order_rows(self, *, signal_id: str, environment: str, trade_group_id: str = "") -> list[dict[str, Any]]:
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
            add_rows(f'signal_id = "{self._escape_filter_value(signal_id)}" && {env_filter}')
        if trade_group_id:
            add_rows(f'trade_group_id = "{self._escape_filter_value(trade_group_id)}" && {env_filter}')
        return rows

    def _resolve_order_role_for_fill_event(self, order: dict, runtime_environment: str) -> str:
        coid = str(
            (order or {}).get("cOID")
            or (order or {}).get("coid")
            or (order or {}).get("orderRef")
            or (order or {}).get("order_ref")
            or ""
        ).strip()
        order_id = str(
            (order or {}).get("orderId")
            or (order or {}).get("order_id")
            or (order or {}).get("broker_order_id")
            or ""
        ).strip()
        if not getattr(self, "pb", None) or not (coid or order_id):
            return ""

        filters = []
        env_filter = self._escape_filter_value(runtime_environment)
        if coid:
            safe_coid = self._escape_filter_value(coid)
            filters.append(f'unique_id = "{safe_coid}" && environment = "{env_filter}"')
        if order_id:
            safe_order_id = self._escape_filter_value(order_id)
            filters.append(f'order_id = "{safe_order_id}" && environment = "{env_filter}"')
            filters.append(f'broker_order_id = "{safe_order_id}" && environment = "{env_filter}"')
        for filter_expr in filters:
            try:
                rows = self.pb.get_records("orders", filter=filter_expr, sort="-updated", per_page=1) or []
            except Exception:
                rows = []
            if rows:
                return self._normalize_order_role(self._order_role(rows[0]))
        return ""

    def _load_pb_order_for_event(self, order: dict, runtime_environment: str) -> dict[str, Any]:
        coid = str(
            (order or {}).get("cOID")
            or (order or {}).get("coid")
            or (order or {}).get("orderRef")
            or (order or {}).get("order_ref")
            or (order or {}).get("unique_id")
            or ""
        ).strip()
        order_id = str(
            (order or {}).get("orderId")
            or (order or {}).get("order_id")
            or (order or {}).get("broker_order_id")
            or ""
        ).strip()
        if not getattr(self, "pb", None) or not (coid or order_id):
            return {}
        filters = []
        env_filter = self._escape_filter_value(runtime_environment)
        if coid:
            safe_coid = self._escape_filter_value(coid)
            filters.append(f'unique_id = "{safe_coid}" && environment = "{env_filter}"')
        if order_id:
            safe_order_id = self._escape_filter_value(order_id)
            filters.append(f'order_id = "{safe_order_id}" && environment = "{env_filter}"')
            filters.append(f'broker_order_id = "{safe_order_id}" && environment = "{env_filter}"')
        for filter_expr in filters:
            try:
                rows = self.pb.get_records("orders", filter=filter_expr, sort="-updated", per_page=1) or []
            except Exception:
                rows = []
            if rows:
                return dict(rows[0])
        return {}

    def _order_price_value(self, row: dict[str, Any], *keys: str) -> float:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        for key in keys:
            value = self._coerce_float(row.get(key), 0.0)
            if value > 0:
                return value
            value = self._coerce_float(extra.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _signal_price_value(self, signal_record: dict[str, Any], *keys: str) -> float:
        extra = self._safe_extra((signal_record or {}).get("extra"))
        for key in keys:
            value = self._coerce_float((signal_record or {}).get(key), 0.0)
            if value > 0:
                return value
            value = self._coerce_float(extra.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _order_numeric_value(self, row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        for key in keys:
            if key in row and row.get(key) not in (None, ""):
                return self._coerce_float(row.get(key), default)
            if key in extra and extra.get(key) not in (None, ""):
                return self._coerce_float(extra.get(key), default)
        return default

    def _signal_text_value(self, signal_record: dict[str, Any], *keys: str) -> str:
        extra = self._safe_extra((signal_record or {}).get("extra"))
        for key in keys:
            value = (signal_record or {}).get(key)
            if value not in (None, ""):
                return str(value).strip()
            value = extra.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def _signal_first_value(self, signal_record: dict[str, Any], *keys: str) -> Any:
        extra = self._safe_extra((signal_record or {}).get("extra"))
        for key in keys:
            if key in (signal_record or {}) and (signal_record or {}).get(key) not in (None, ""):
                return (signal_record or {}).get(key)
            if key in extra and extra.get(key) not in (None, ""):
                return extra.get(key)
        return ""

    def _parse_reward_risk_ratio(self, value: Any) -> tuple[float, str]:
        text = str(value or "").strip().lower().replace("rr", "").replace("r", "")
        if not text:
            return 0.0, ""
        for sep in (":", "/"):
            if sep not in text:
                continue
            left, right = text.split(sep, 1)
            risk = self._coerce_float(left.strip(), 0.0)
            reward = self._coerce_float(right.strip(), 0.0)
            if risk > 0 and reward > 0:
                return reward / risk, "ratio_text"
        parsed = self._coerce_float(text, 0.0)
        return (parsed, "numeric") if parsed > 0 else (0.0, "")

    def _entry_fill_tv_reprice_result(
        self,
        *,
        order: dict,
        signal_record: dict[str, Any],
        environment: str,
        actual_fill_price: float,
        submitted_entry: float,
        entry_row: dict[str, Any],
        tp_row: dict[str, Any],
        sl_row: dict[str, Any],
        old_tp: float,
        old_sl: float,
    ) -> dict[str, Any]:
        enabled = self._config_bool_for_environment("tv_entry_fill_reprice_enabled", environment, True)
        signal_extra = self._safe_extra((signal_record or {}).get("extra"))
        direction = self._signal_text_value(signal_record, "direction").lower()
        if direction not in {"long", "short"}:
            side = str((order or {}).get("side") or "").strip().upper()
            direction = "long" if side == "BUY" else "short" if side == "SELL" else ""

        reference_entry = (
            self._signal_price_value(signal_record, "reference_entry", "tv_reference_entry", "signal_reference_price", "original_entry")
            or submitted_entry
        )
        reference_stop = self._signal_price_value(
            signal_record,
            "reference_stop_loss",
            "tv_reference_stop_loss",
            "reference_sl",
            "original_stop_loss",
            "initial_stop_loss",
        )
        reference_target = self._signal_price_value(
            signal_record,
            "reference_take_profit",
            "tv_reference_take_profit",
            "reference_tp",
            "original_take_profit",
            "initial_take_profit",
        )
        risk_per_share = self._signal_price_value(
            signal_record,
            "risk_per_share",
            "risk_r",
            "initial_risk_r",
        )
        risk_source = "risk_per_share" if risk_per_share > 0 else ""
        atr = self._signal_price_value(signal_record, "atr", "atr_raw", "atrRaw")
        sl_atr_mult = 0.0
        if risk_per_share <= 0 and reference_entry > 0 and reference_stop > 0:
            reference_risk = abs(reference_entry - reference_stop)
            if atr > 0:
                sl_atr_mult = reference_risk / atr
                risk_per_share = atr * sl_atr_mult
                risk_source = "reference_stop_loss_atr"
            else:
                risk_per_share = reference_risk
                risk_source = "reference_stop_loss"
        rr_value = self._signal_first_value(
            signal_record,
            "reward_risk",
            "rewardRisk",
            "rr",
            "risk_reward",
        )
        reward_risk, reward_risk_source = self._parse_reward_risk_ratio(rr_value)
        if reward_risk <= 0 and reference_entry > 0 and reference_target > 0 and risk_per_share > 0:
            reward_risk = abs(reference_target - reference_entry) / risk_per_share
            reward_risk_source = "reference_take_profit"

        delta = actual_fill_price - submitted_entry if actual_fill_price > 0 and submitted_entry > 0 else 0.0
        directional_slippage = (
            reference_entry - actual_fill_price if direction == "short" else actual_fill_price - reference_entry
        ) if reference_entry > 0 and actual_fill_price > 0 else 0.0
        result: dict[str, Any] = {
            "ok": True,
            "enabled": enabled,
            "attempted": False,
            "material": False,
            "method": "tv_fill_based",
            "reason": "not_material",
            "actual_fill_price": actual_fill_price,
            "submitted_entry": submitted_entry,
            "reference_entry": reference_entry,
            "reference_stop_loss": reference_stop,
            "reference_take_profit": reference_target,
            "delta": delta,
            "reference_delta": actual_fill_price - reference_entry if reference_entry > 0 and actual_fill_price > 0 else 0.0,
            "directional_slippage": directional_slippage,
            "old_stop_loss": old_sl,
            "old_take_profit": old_tp,
            "stop_loss": old_sl,
            "take_profit": old_tp,
            "risk_per_share": risk_per_share,
            "reward_risk": reward_risk,
            "atr": atr,
            "sl_atr_mult": sl_atr_mult,
            "stop_loss_source": risk_source,
            "reward_risk_source": reward_risk_source,
        }
        if actual_fill_price <= 0:
            return {**result, "ok": False, "reason": "missing_actual_fill_price"}
        if reference_entry <= 0 or direction not in {"long", "short"}:
            return {**result, "ok": False, "reason": "missing_reference_entry_or_direction"}
        if not enabled:
            return {**result, "reason": "disabled"}
        if risk_per_share <= 0 or reward_risk <= 0:
            return {**result, "ok": False, "reason": "missing_tv_fill_reprice_inputs"}

        new_sl = round(actual_fill_price + risk_per_share if direction == "short" else actual_fill_price - risk_per_share, 4)
        new_tp = round(actual_fill_price - (risk_per_share * reward_risk) if direction == "short" else actual_fill_price + (risk_per_share * reward_risk), 4)
        slippage_bps = directional_slippage / reference_entry * 10000.0 if reference_entry > 0 else 0.0
        slippage_r = directional_slippage / risk_per_share if risk_per_share > 0 else 0.0
        sl_order_id = self._order_broker_id(sl_row)
        tp_order_id = self._order_broker_id(tp_row)
        requires_modify = (
            old_sl <= 0
            or old_tp <= 0
            or abs(new_sl - old_sl) > 0.0000001
            or abs(new_tp - old_tp) > 0.0000001
        )
        result.update(
            {
                "material": bool(requires_modify),
                "attempted": bool(requires_modify),
                "reason": "tv_fill_based_repriced" if requires_modify else "already_aligned",
                "stop_loss": new_sl,
                "take_profit": new_tp,
                "final_stop_loss": new_sl,
                "final_take_profit": new_tp,
                "slippage_bps": round(slippage_bps, 4),
                "slippage_r": round(slippage_r, 6),
                "stop_loss_order_id": sl_order_id,
                "take_profit_order_id": tp_order_id,
            }
        )
        if new_sl <= 0 or new_tp <= 0:
            return {**result, "ok": False, "reason": "invalid_final_protection_price"}
        if not requires_modify:
            self._update_pb_order_row(
                entry_row,
                {"stop_loss": new_sl, "take_profit": new_tp, "sl_price": new_sl, "tp_price": new_tp},
            )
            self._update_pb_order_row(sl_row, {"limit_price": new_sl, "stop_loss": new_sl, "sl_price": new_sl})
            self._update_pb_order_row(tp_row, {"limit_price": new_tp, "take_profit": new_tp, "tp_price": new_tp})
            return result
        if not sl_order_id or not tp_order_id:
            return {**result, "ok": False, "reason": "missing_reprice_order_id"}
        order_modifier = getattr(self, "order_modifier", None)
        if not order_modifier:
            return {**result, "ok": False, "reason": "order_modifier_unavailable"}

        try:
            sl_modify = order_modifier.update_stop_loss(sl_order_id, new_sl)
        except Exception as exc:
            sl_modify = {"ok": False, "error": str(exc), "exception": type(exc).__name__}
        try:
            tp_modify = order_modifier.update_take_profit(tp_order_id, new_tp)
        except Exception as exc:
            tp_modify = {"ok": False, "error": str(exc), "exception": type(exc).__name__}
        result["modify_results"] = {
            "stop_loss": dict(sl_modify or {}),
            "take_profit": dict(tp_modify or {}),
        }
        if not (sl_modify or {}).get("ok") or not (tp_modify or {}).get("ok"):
            return {**result, "ok": False, "reason": "broker_modify_failed"}

        sl_normalization = ((sl_modify or {}).get("price_normalization") or {}).get("auxPrice") or {}
        tp_normalization = (
            ((tp_modify or {}).get("price_normalization") or {}).get("price")
            or ((tp_modify or {}).get("price_normalization") or {}).get("lmtPrice")
            or {}
        )
        normalized_sl = float(sl_normalization.get("normalized") or 0.0)
        normalized_tp = float(tp_normalization.get("normalized") or 0.0)
        if normalized_sl > 0 or normalized_tp > 0:
            result["price_normalization"] = {
                "stop_loss": dict(sl_normalization or {}),
                "take_profit": dict(tp_normalization or {}),
            }
        if normalized_sl > 0 and abs(normalized_sl - new_sl) > 0.0000001:
            result["raw_stop_loss"] = new_sl
            new_sl = normalized_sl
            result["stop_loss"] = result["final_stop_loss"] = new_sl
        if normalized_tp > 0 and abs(normalized_tp - new_tp) > 0.0000001:
            result["raw_take_profit"] = new_tp
            new_tp = normalized_tp
            result["take_profit"] = result["final_take_profit"] = new_tp

        self._update_pb_order_row(
            entry_row,
            {"stop_loss": new_sl, "take_profit": new_tp, "sl_price": new_sl, "tp_price": new_tp},
        )
        self._update_pb_order_row(sl_row, {"limit_price": new_sl, "stop_loss": new_sl, "sl_price": new_sl})
        self._update_pb_order_row(tp_row, {"limit_price": new_tp, "take_profit": new_tp, "tp_price": new_tp})
        return result

    def _sync_signal_lifecycle_ack(
        self,
        *,
        signal_id: str,
        status: str,
        note: str,
        environment: str,
        extra: dict[str, Any],
        executed_price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> None:
        ack = getattr(getattr(self, "pb", None), "ack_ibkr_signal", None)
        if not callable(ack) or not signal_id:
            return
        order_extra = {
            **(extra if isinstance(extra, dict) else {}),
            "signal_lifecycle_update": True,
            "force_signal_status_notification": True,
        }
        if executed_price > 0:
            order_extra["actual_fill_price"] = executed_price
            order_extra["entry_fill_price"] = executed_price
            order_extra["executed_price"] = executed_price
        if stop_loss > 0:
            order_extra["final_stop_loss"] = stop_loss
        if take_profit > 0:
            order_extra["final_take_profit"] = take_profit
        ack_status = str(status or "").strip()
        ack_note = str(note or "").strip()
        status_key = ack_status.lower()
        reason_key = str(order_extra.get("status_reason") or "").strip().lower()
        entry_cancelled_before_fill = (
            status_key == "entry_missed_limit_cap"
            or reason_key == "entry_missed_limit_cap"
            or bool(order_extra.get("entry_missed_limit_cap"))
        )
        if entry_cancelled_before_fill:
            ack_status = "cancelled"
            ack_note = "cancelled"
            order_extra.setdefault("status_reason", "entry_missed_limit_cap")
            order_extra["entry_missed_limit_cap"] = True
            order_extra.setdefault("entry_missed_reason", "entry_missed_limit_cap")
        requested_ack_status = ack_status
        ack_status = self._pb_signal_status(ack_status)
        if requested_ack_status != ack_status:
            order_extra.setdefault("signal_lifecycle_status", requested_ack_status)
            order_extra["pb_signal_status"] = ack_status
            order_extra["pb_signal_status_mapped_from"] = requested_ack_status
        try:
            ack(
                signal_id=signal_id,
                status=ack_status,
                note=ack_note,
                order={
                    "executed_price": executed_price,
                    "actual_fill_price": executed_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "extra": order_extra,
                },
                environment=environment,
            )
        except Exception as exc:
            _service_mod().logger.debug("Signal lifecycle ack/notification sync failed: signal_id=%s error=%s", signal_id, exc)

    def _trade_group_id_for_exit(self, signal_record: dict[str, Any], order: dict[str, Any]) -> str:
        extra = self._safe_extra((signal_record or {}).get("extra"))
        order_extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
        return str(
            order.get("trade_group_id")
            or order_extra.get("trade_group_id")
            or extra.get("bracket_group")
            or extra.get("trade_group_id")
            or extra.get("submit_failed_bracket_group")
            or ""
        ).strip()

    def _exit_realized_pnl(
        self,
        *,
        signal_record: dict[str, Any],
        exit_order: dict[str, Any],
        signal_id: str,
        environment: str,
        trade_group_id: str = "",
    ) -> dict[str, Any]:
        rows = self._signal_order_rows(signal_id=signal_id, environment=environment, trade_group_id=trade_group_id)
        entry_row = next(
            (row for row in rows if self._normalize_order_role(self._order_role(row)) == "entry"),
            {},
        )
        entry_price = (
            self._order_price_value(entry_row, "fill_price", "filled_price", "avgPrice", "avgFillPrice")
            or self._signal_price_value(signal_record, "executed_price", "entry_fill_price")
            or self._order_price_value(entry_row, "limit_price", "entry", "entry_price")
            or self._signal_price_value(signal_record, "entry", "limit_price")
        )
        exit_price = (
            self._order_price_value(exit_order, "avgPrice", "avgFillPrice", "fill_price", "filled_price", "lastFillPrice")
            or self._order_price_value(exit_order, "price", "limit_price", "exit_price", "tp_price", "sl_price")
        )
        quantity = (
            self._order_price_value(exit_order, "filledQuantity", "filled_qty", "filled", "totalSize", "quantity")
            or self._order_price_value(entry_row, "filled_qty", "quantity")
            or self._signal_price_value(signal_record, "shares", "quantity")
        )
        direction = (
            self._signal_text_value(signal_record, "direction")
            or str(entry_row.get("position_side") or entry_row.get("direction") or "").strip().lower()
            or str(exit_order.get("position_side") or exit_order.get("direction") or "").strip().lower()
        )
        if direction not in {"long", "short"}:
            exit_side = str(exit_order.get("side") or exit_order.get("action") or "").strip().upper()
            if exit_side == "SELL":
                direction = "long"
            elif exit_side == "BUY":
                direction = "short"
        result = {
            "ok": False,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": quantity,
            "direction": direction,
            "gross_pnl": 0.0,
            "commission": 0.0,
            "net_pnl": 0.0,
            "pnl_pct": 0.0,
            "source": "computed_from_entry_exit_fills",
        }
        if entry_price <= 0 or exit_price <= 0 or quantity <= 0 or direction not in {"long", "short"}:
            result["reason"] = "missing_entry_exit_price_or_quantity"
            return result

        per_share = entry_price - exit_price if direction == "short" else exit_price - entry_price
        gross = per_share * abs(quantity)
        entry_commission = abs(self._order_numeric_value(entry_row, "commission", default=0.0)) if entry_row else 0.0
        exit_commission = abs(self._order_numeric_value(exit_order, "commission", default=0.0)) if exit_order else 0.0
        same_order = bool(
            entry_row
            and exit_order
            and str(entry_row.get("id") or entry_row.get("unique_id") or "")
            and str(entry_row.get("id") or entry_row.get("unique_id") or "")
            == str(exit_order.get("id") or exit_order.get("unique_id") or "")
        )
        commission = exit_commission if same_order else entry_commission + exit_commission
        net = gross - commission
        position_cost = entry_price * abs(quantity)
        result.update(
            {
                "ok": True,
                "gross_pnl": round(gross, 4),
                "commission": round(commission, 4),
                "entry_commission": round(entry_commission, 4),
                "exit_commission": round(exit_commission, 4),
                "net_pnl": round(net, 4),
                "pnl_pct": round((net / position_cost) * 100.0, 4) if position_cost > 0 else 0.0,
                "reason": "",
            }
        )
        return result

    def _patch_exit_order_pnl(self, order: dict[str, Any], pnl_result: dict[str, Any]) -> None:
        if not bool((pnl_result or {}).get("ok")):
            return
        patch = {
            "pnl": pnl_result.get("gross_pnl", 0.0),
            "commission": pnl_result.get("exit_commission", 0.0),
            "extra": {
                **self._safe_extra((order or {}).get("extra")),
                "realized_gross_pnl": pnl_result.get("gross_pnl", 0.0),
                "realized_net_pnl": pnl_result.get("net_pnl", 0.0),
                "realized_pnl": pnl_result.get("net_pnl", 0.0),
                "total_commission": pnl_result.get("commission", 0.0),
                "entry_commission": pnl_result.get("entry_commission", 0.0),
                "exit_commission": pnl_result.get("exit_commission", 0.0),
                "realized_pnl_source": pnl_result.get("source", "computed_from_entry_exit_fills"),
                "entry_price_for_pnl": pnl_result.get("entry_price", 0.0),
                "exit_price_for_pnl": pnl_result.get("exit_price", 0.0),
                "quantity_for_pnl": pnl_result.get("quantity", 0.0),
                "pnl_pct": pnl_result.get("pnl_pct", 0.0),
            },
        }
        self._update_pb_order_row(order, patch)

    def _update_pb_order_row(self, row: dict[str, Any], patch: dict[str, Any]) -> None:
        if not getattr(self, "pb", None) or not isinstance(row, dict) or not patch:
            return
        try:
            record_id = str(row.get("id") or "").strip()
            if record_id and callable(getattr(self.pb, "update_record", None)):
                self.pb.update_record("orders", record_id, patch)
                return
            if callable(getattr(self.pb, "upsert_order", None)):
                self.pb.upsert_order({**row, **patch})
        except Exception:
            _service_mod().logger.debug("PB order update failed", exc_info=True)

    def _entry_fill_rebase_result(
        self,
        *,
        order: dict,
        signal_record: dict[str, Any],
        signal_id: str,
        environment: str,
        trade_group_id: str,
        actual_fill_price: float,
    ) -> dict[str, Any]:
        enabled = self._config_bool_for_environment("entry_fill_rebase_enabled", environment, True)
        min_bps = max(0.0, self._config_float_for_environment("entry_fill_rebase_min_bps", environment, 1.0))
        min_abs = max(0.0, self._config_float_for_environment("entry_fill_rebase_min_abs", environment, 0.01))
        rows = self._signal_order_rows(signal_id=signal_id, environment=environment, trade_group_id=trade_group_id)
        entry_rows = [row for row in rows if self._normalize_order_role(self._order_role(row)) == "entry"]
        tp_rows = [row for row in rows if self._normalize_order_role(self._order_role(row)) == "take_profit"]
        sl_rows = [row for row in rows if self._normalize_order_role(self._order_role(row)) == "stop_loss"]
        entry_row = entry_rows[0] if entry_rows else {}
        tp_row = tp_rows[0] if tp_rows else {}
        sl_row = sl_rows[0] if sl_rows else {}

        entry_order_limit = self._order_price_value(
            entry_row,
            "limit_price",
            "limitPrice",
            "lmtPrice",
            "lmt_price",
            "submitted_price",
            "submitted_limit_price",
            "entry_limit_price",
            "entry_limit",
        )
        event_order_limit = self._first_positive_order_float(
            order,
            "limit_price",
            "limitPrice",
            "lmtPrice",
            "lmt_price",
            "submitted_price",
            "submitted_limit_price",
            "entry_limit_price",
            "entry_limit",
        )
        entry_order_price = self._order_price_value(entry_row, "entry_price", "entry", "price")
        signal_entry_reference = self._signal_price_value(
            signal_record,
            "limit_price",
            "submitted_price",
            "submitted_limit_price",
            "entry_limit_price",
            "entry_price",
            "entry",
        )
        submitted_entry = entry_order_limit or event_order_limit or entry_order_price or signal_entry_reference
        old_tp = (
            self._order_price_value(tp_row, "limit_price", "take_profit", "tp_price", "price")
            or self._signal_price_value(signal_record, "take_profit", "tp_price", "initial_take_profit")
        )
        old_sl = (
            self._order_price_value(sl_row, "limit_price", "stop_loss", "sl_price", "auxPrice", "price")
            or self._signal_price_value(signal_record, "stop_loss", "sl_price", "initial_stop_loss")
        )
        signal_extra = self._safe_extra((signal_record or {}).get("extra"))
        if bool(signal_extra.get("tv_direct_entry") or signal_extra.get("final_protection_from_fill")):
            return self._entry_fill_tv_reprice_result(
                order=order,
                signal_record=signal_record,
                environment=environment,
                actual_fill_price=actual_fill_price,
                submitted_entry=submitted_entry,
                entry_row=entry_row,
                tp_row=tp_row,
                sl_row=sl_row,
                old_tp=old_tp,
                old_sl=old_sl,
            )
        delta = actual_fill_price - submitted_entry if actual_fill_price > 0 and submitted_entry > 0 else 0.0
        threshold = max(min_abs, abs(submitted_entry) * min_bps / 10000.0) if submitted_entry > 0 else min_abs
        result: dict[str, Any] = {
            "ok": True,
            "enabled": enabled,
            "attempted": False,
            "material": False,
            "reason": "not_material",
            "actual_fill_price": actual_fill_price,
            "submitted_entry": submitted_entry,
            "delta": delta,
            "min_bps": min_bps,
            "min_abs": min_abs,
            "threshold_abs": threshold,
            "old_stop_loss": old_sl,
            "old_take_profit": old_tp,
            "stop_loss": old_sl,
            "take_profit": old_tp,
        }
        if actual_fill_price <= 0:
            return {**result, "reason": "missing_actual_fill_price"}
        if submitted_entry <= 0:
            return {**result, "reason": "missing_submitted_entry_price"}
        if not enabled:
            return {**result, "reason": "disabled"}
        if abs(delta) < threshold:
            return result
        result["material"] = True

        new_sl = round(old_sl + delta, 4) if old_sl > 0 else 0.0
        new_tp = round(old_tp + delta, 4) if old_tp > 0 else 0.0
        sl_order_id = self._order_broker_id(sl_row)
        tp_order_id = self._order_broker_id(tp_row)
        result.update(
            {
                "attempted": True,
                "reason": "rebased",
                "stop_loss": new_sl,
                "take_profit": new_tp,
                "stop_loss_order_id": sl_order_id,
                "take_profit_order_id": tp_order_id,
            }
        )
        if not sl_order_id or not tp_order_id or new_sl <= 0 or new_tp <= 0:
            return {**result, "ok": False, "reason": "missing_rebase_order_or_price"}
        order_modifier = getattr(self, "order_modifier", None)
        if not order_modifier:
            return {**result, "ok": False, "reason": "order_modifier_unavailable"}

        try:
            sl_modify = order_modifier.update_stop_loss(sl_order_id, new_sl)
        except Exception as exc:
            sl_modify = {"ok": False, "error": str(exc), "exception": type(exc).__name__}
        try:
            tp_modify = order_modifier.update_take_profit(tp_order_id, new_tp)
        except Exception as exc:
            tp_modify = {"ok": False, "error": str(exc), "exception": type(exc).__name__}
        result["modify_results"] = {
            "stop_loss": dict(sl_modify or {}),
            "take_profit": dict(tp_modify or {}),
        }
        if not (sl_modify or {}).get("ok") or not (tp_modify or {}).get("ok"):
            return {**result, "ok": False, "reason": "broker_modify_failed"}

        sl_normalization = ((sl_modify or {}).get("price_normalization") or {}).get("auxPrice") or {}
        tp_normalization = (
            ((tp_modify or {}).get("price_normalization") or {}).get("price")
            or ((tp_modify or {}).get("price_normalization") or {}).get("lmtPrice")
            or {}
        )
        normalized_sl = float(sl_normalization.get("normalized") or 0.0)
        normalized_tp = float(tp_normalization.get("normalized") or 0.0)
        if normalized_sl > 0 or normalized_tp > 0:
            result["price_normalization"] = {
                "stop_loss": dict(sl_normalization or {}),
                "take_profit": dict(tp_normalization or {}),
            }
        if normalized_sl > 0 and abs(normalized_sl - new_sl) > 0.0000001:
            result["raw_stop_loss"] = new_sl
            new_sl = normalized_sl
            result["stop_loss"] = new_sl
        if normalized_tp > 0 and abs(normalized_tp - new_tp) > 0.0000001:
            result["raw_take_profit"] = new_tp
            new_tp = normalized_tp
            result["take_profit"] = new_tp

        self._update_pb_order_row(
            entry_row,
            {"stop_loss": new_sl, "take_profit": new_tp, "sl_price": new_sl, "tp_price": new_tp},
        )
        self._update_pb_order_row(sl_row, {"limit_price": new_sl, "stop_loss": new_sl, "sl_price": new_sl})
        self._update_pb_order_row(tp_row, {"limit_price": new_tp, "take_profit": new_tp, "tp_price": new_tp})
        return result

    def _reconcile_signal_exit_fill(
        self,
        *,
        signal_record: dict[str, Any],
        signal_id: str,
        environment: str,
        symbol: str,
        trade_group_id: str = "",
        trigger_order: dict | None = None,
        preferred_role: str = "",
    ) -> str:
        if not signal_record or str(signal_record.get("status") or "").strip().lower() == "closed":
            return ""
        rows = self._signal_order_rows(signal_id=signal_id, environment=environment, trade_group_id=trade_group_id)
        preferred = self._normalize_order_role(preferred_role)
        filled_children: list[dict[str, Any]] = []
        for row in rows:
            role = self._normalize_order_role(self._order_role(row))
            if role not in {"stop_loss", "take_profit", "close"}:
                continue
            if self._order_status(row).strip().lower() == "filled":
                filled_children.append(row)
        if preferred in {"stop_loss", "take_profit", "close"}:
            filled_children.sort(key=lambda row: 0 if self._normalize_order_role(self._order_role(row)) == preferred else 1)
        for child in filled_children:
            role = self._normalize_order_role(self._order_role(child))
            if self._update_signal_after_exit_fill(child, symbol, role, signal_record=signal_record):
                return role
        return ""

    def _update_signal_after_entry_fill(self, order: dict, symbol: str, direction: str) -> str:
        service_mod = _service_mod()
        if not getattr(self, "pb", None):
            return ""
        coid = str(
            order.get("cOID")
            or order.get("coid")
            or order.get("orderRef")
            or order.get("order_ref")
            or ""
        ).strip()
        order_id = str(order.get("orderId") or order.get("order_id") or "").strip()
        broker_environment = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
        data_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live"

        try:
            signal_id = self._resolve_signal_id_for_order(order, broker_environment)
            if not signal_id:
                return ""

            signal_record = self.pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                    f'environment = "{self._escape_filter_value(data_environment)}"'
                ),
            )
            if not signal_record or not signal_record.get("id"):
                return ""
            current_status = str(signal_record.get("status") or "").strip().lower()
            if current_status in {"closed", "expired", "rejected"}:
                return ""
            existing_extra = self._safe_extra(signal_record.get("extra"))
            trade_group_id = str(
                existing_extra.get("bracket_group")
                or existing_extra.get("trade_group_id")
                or existing_extra.get("submit_failed_bracket_group")
                or (coid[6:] if coid.startswith("entry_") else "")
                or ""
            ).strip()
            closed_role = self._reconcile_signal_exit_fill(
                signal_record=signal_record,
                signal_id=signal_id,
                environment=broker_environment,
                symbol=symbol,
                trade_group_id=trade_group_id,
                trigger_order=order,
            )
            if closed_role:
                return closed_role
            if current_status in {"protected_active", "filled_position"}:
                return ""
            protection_status = self._signal_protection_status(
                signal_id=signal_id,
                environment=broker_environment,
                trade_group_id=trade_group_id,
            )
            actual_fill_price = self._first_positive_order_float(
                order,
                "avgPrice",
                "avgFillPrice",
                "fill_price",
                "filled_price",
                "lastFillPrice",
                "price",
            )
            entry_rows = [
                row
                for row in self._signal_order_rows(
                    signal_id=signal_id,
                    environment=broker_environment,
                    trade_group_id=trade_group_id,
                )
                if self._normalize_order_role(self._order_role(row)) == "entry"
            ]
            if actual_fill_price > 0:
                for entry_row in entry_rows:
                    self._update_pb_order_row(
                        entry_row,
                        {
                            "fill_price": actual_fill_price,
                            "status": "Filled",
                        },
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
                    "entry_fill_price": actual_fill_price,
                    "executed_price": actual_fill_price,
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
                self._sync_signal_lifecycle_ack(
                    signal_id=signal_id,
                    status="protection_incomplete",
                    note="entry_fill_detected_with_incomplete_protection",
                    environment=broker_environment,
                    extra=extra,
                    executed_price=actual_fill_price,
                    stop_loss=self._signal_price_value(signal_record, "stop_loss", "sl_price"),
                    take_profit=self._signal_price_value(signal_record, "take_profit", "tp_price"),
                )
                return ""
            rebase_result = self._entry_fill_rebase_result(
                order=order,
                signal_record=signal_record,
                signal_id=signal_id,
                environment=broker_environment,
                trade_group_id=trade_group_id,
                actual_fill_price=actual_fill_price,
            )
            tv_fill_based = str(rebase_result.get("method") or "").strip().lower() == "tv_fill_based"
            if not bool(rebase_result.get("ok", True)):
                failure_status = "protection_reprice_failed" if tv_fill_based else "protection_incomplete"
                failure_note = "protection_reprice_failed" if tv_fill_based else "protection_rebase_failed"
                diagnostic = {
                    "status": failure_status,
                    "reason": failure_note,
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "direction": direction,
                    "protection_complete": False,
                    "protection_rebase_result": rebase_result,
                    "safe_action": "diagnostic_only_no_protected_active_claim",
                    "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
                    "cancel_recommended": True,
                }
                handler = getattr(getattr(self, "order_lifecycle", None), "handle_protection_incomplete", None)
                if callable(handler):
                    try:
                        lifecycle_diagnostic = handler(
                            signal_id=signal_id,
                            symbol=symbol,
                            direction=direction,
                            order=order,
                            result={
                                "order_ids": [
                                    item
                                    for item in (
                                        rebase_result.get("stop_loss_order_id"),
                                        rebase_result.get("take_profit_order_id"),
                                    )
                                    if item
                                ],
                                "bracket_group": trade_group_id,
                            },
                            reason=failure_note,
                        )
                        if isinstance(lifecycle_diagnostic, dict):
                            diagnostic = {**lifecycle_diagnostic, **diagnostic}
                    except Exception:
                        pass
                extra = {
                    **existing_extra,
                    "entry_fill_detected_by": "order_tracker",
                    "entry_fill_broker_order_id": order_id,
                    "entry_fill_price": actual_fill_price,
                    "entry_fill_rebase_delta": rebase_result.get("delta", 0.0),
                    "executed_price": actual_fill_price,
                    "entry_fill_status": str(order.get("status") or ""),
                    "entry_fill_direction": direction,
                    "entry_fill_symbol": symbol,
                    "status_reason": failure_note,
                    "protection_complete": False,
                    "protection_incomplete": True,
                    "protection_rebase_result": rebase_result,
                    "protection_reprice_result": rebase_result if tv_fill_based else {},
                    "protection_reprice_failed": tv_fill_based,
                    "protection_rebase_failed": diagnostic,
                    "protection_incomplete_diagnostic": diagnostic,
                    "missing_protection_roles": [],
                    "protection_order_statuses": dict(protection_status.get("role_statuses") or {}),
                    "protection_live_order_statuses": dict(protection_status.get("live_role_statuses") or {}),
                    "protection_live_check_performed": bool(protection_status.get("live_check_performed")),
                    "protection_live_coverage": dict(protection_status.get("live_coverage") or {}),
                    "unverified_protection_roles": [],
                    "protection_orders_checked": int(protection_status.get("orders_checked") or 0),
                    "safety_cancel_recommended": True,
                }
                extra = self._annotate_pb_signal_status_alias(extra, failure_status)
                pb_failure_status = self._pb_signal_status(failure_status)
                self.pb.update_record(
                    "ibkr_signals",
                    str(signal_record.get("id")),
                    {
                        "status": pb_failure_status,
                        "note": failure_note,
                        "executed_price": actual_fill_price,
                        "extra": extra,
                    },
                )
                self._sync_signal_lifecycle_ack(
                    signal_id=signal_id,
                    status=failure_status,
                    note=failure_note,
                    environment=broker_environment,
                    extra=extra,
                    executed_price=actual_fill_price,
                    stop_loss=self._coerce_float(rebase_result.get("stop_loss"), 0.0),
                    take_profit=self._coerce_float(rebase_result.get("take_profit"), 0.0),
                )
                return ""
            success_status = "filled_position" if tv_fill_based else "protected_active"
            success_note = (
                "entry_filled_final_protection_repriced"
                if tv_fill_based and bool(rebase_result.get("attempted"))
                else "entry_filled_final_protection_aligned"
                if tv_fill_based
                else "entry_filled_protection_expected"
            )
            extra = {
                **existing_extra,
                "entry_fill_detected_by": "order_tracker",
                "entry_fill_broker_order_id": order_id,
                "entry_fill_price": actual_fill_price,
                "entry_fill_rebase_delta": rebase_result.get("delta", 0.0),
                "executed_price": actual_fill_price,
                "entry_fill_status": str(order.get("status") or ""),
                "entry_fill_direction": direction,
                "entry_fill_symbol": symbol,
                "status_reason": success_note,
                "protection_complete": True,
                "protection_incomplete": False,
                "protection_rebase_result": rebase_result,
                "protection_reprice_result": rebase_result if tv_fill_based else {},
                "signal_lifecycle_status": success_status,
                "final_stop_loss": rebase_result.get("final_stop_loss") or rebase_result.get("stop_loss"),
                "final_take_profit": rebase_result.get("final_take_profit") or rebase_result.get("take_profit"),
                "entry_slippage_bps": rebase_result.get("slippage_bps", 0.0),
                "entry_slippage_r": rebase_result.get("slippage_r", 0.0),
                "slippage_bps": rebase_result.get("slippage_bps", 0.0),
                "slippage_r": rebase_result.get("slippage_r", 0.0),
                "missing_protection_roles": [],
                "protection_order_statuses": dict(protection_status.get("role_statuses") or {}),
                "protection_live_order_statuses": dict(protection_status.get("live_role_statuses") or {}),
                "protection_live_check_performed": bool(protection_status.get("live_check_performed")),
                "protection_live_coverage": dict(protection_status.get("live_coverage") or {}),
                "unverified_protection_roles": [],
                "protection_orders_checked": int(protection_status.get("orders_checked") or 0),
                "safety_cancel_recommended": False,
            }
            extra = self._annotate_pb_signal_status_alias(extra, success_status)
            pb_success_status = self._pb_signal_status(success_status)
            self.pb.update_record(
                "ibkr_signals",
                str(signal_record.get("id")),
                {
                    "status": pb_success_status,
                    "note": success_note,
                    "executed_price": actual_fill_price,
                    "stop_loss": rebase_result.get("stop_loss") or self._signal_price_value(signal_record, "stop_loss", "sl_price"),
                    "take_profit": rebase_result.get("take_profit") or self._signal_price_value(signal_record, "take_profit", "tp_price"),
                    "extra": extra,
                },
            )
            self._sync_signal_lifecycle_ack(
                signal_id=signal_id,
                status=success_status,
                note=success_note,
                environment=broker_environment,
                extra=extra,
                executed_price=actual_fill_price,
                stop_loss=self._coerce_float(rebase_result.get("stop_loss"), 0.0),
                take_profit=self._coerce_float(rebase_result.get("take_profit"), 0.0),
            )
        except Exception as exc:
            service_mod.logger.error("Failed to update signal after entry fill: %s", exc)
        return ""

    def _update_signal_after_entry_cancel(self, order: dict, symbol: str) -> bool:
        service_mod = _service_mod()
        if not getattr(self, "pb", None):
            return False
        broker_environment = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
        data_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live"

        try:
            pb_order = self._load_pb_order_for_event(order, broker_environment)
            role = self._normalize_order_role(self._order_role(pb_order or order))
            if not role:
                role = self._resolve_order_role_for_fill_event(order, broker_environment)
            if role and role != "entry":
                return False
            if str(order.get("parentId") or order.get("parent_id") or "").strip():
                return False

            filled_qty = self._coerce_float(
                self._first_nonempty_order_value(order, "filledQuantity", "filled_qty", "filled", "executedQuantity"),
                0.0,
            )
            fill_price = self._first_positive_order_float(
                order,
                "avgPrice",
                "avgFillPrice",
                "fill_price",
                "filled_price",
                "lastFillPrice",
                "price",
            )
            if filled_qty > 0 or fill_price > 0:
                return False

            signal_id = self._resolve_signal_id_for_order(order, broker_environment)
            if not signal_id:
                return False
            signal_record = self.pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                    f'environment = "{self._escape_filter_value(data_environment)}"'
                ),
            )
            if not signal_record or not signal_record.get("id"):
                return False
            current_status = str(signal_record.get("status") or "").strip().lower()
            if current_status in {
                "closed",
                "cancelled",
                "canceled",
                "expired",
                "rejected",
                "entry_missed_limit_cap",
                "filled_position",
                "protected_active",
                "protection_incomplete",
                "protection_reprice_failed",
            }:
                return False
            existing_extra = self._safe_extra(signal_record.get("extra"))
            if not bool(existing_extra.get("tv_direct_entry") or existing_extra.get("final_protection_from_fill")):
                return False

            order_id = str(order.get("broker_order_id") or order.get("order_id") or order.get("orderId") or "").strip()
            raw_status = str(order.get("status") or order.get("order_status") or "").strip()
            order_status = raw_status or "Cancelled"
            missed_at = self._now_iso_for_signal_patch()
            extra = {
                **existing_extra,
                "status_reason": "entry_missed_limit_cap",
                "entry_missed_limit_cap": True,
                "entry_missed_reason": "entry_missed_limit_cap",
                "entry_missed_by": "order_tracker",
                "entry_missed_at": missed_at,
                "entry_cancel_status": order_status,
                "entry_cancel_broker_order_id": order_id,
                "entry_fill_detected": False,
                "position_open": False,
                "protection_complete": False,
                "protection_incomplete": False,
                "safety_cancel_recommended": False,
            }
            extra = self._annotate_pb_signal_status_alias(extra, "cancelled")
            self.pb.update_record(
                "ibkr_signals",
                str(signal_record.get("id")),
                {
                    "status": self._pb_signal_status("cancelled"),
                    "note": "cancelled",
                    "extra": extra,
                },
            )
            if pb_order:
                order_extra = self._safe_extra(pb_order.get("extra"))
                self._update_pb_order_row(
                    pb_order,
                    {
                        "status": order_status,
                        "relation_status": "entry_missed",
                        "extra": {
                            **order_extra,
                            "entry_missed_limit_cap": True,
                            "entry_missed_at": missed_at,
                            "entry_cancel_status": order_status,
                        },
                    },
                )
            self._sync_signal_lifecycle_ack(
                signal_id=signal_id,
                status="cancelled",
                note="cancelled",
                environment=broker_environment,
                extra=extra,
            )
            self._demote_entry_target_after_signal_close(
                signal_record=signal_record,
                signal_id=signal_id,
                symbol=symbol,
                environment=data_environment,
                reason="entry_missed_limit_cap",
            )
            return True
        except Exception as exc:
            service_mod.logger.error("Failed to mark TV direct entry missed after cancel: %s", exc)
        return False

    def _demote_entry_target_after_signal_close(
        self,
        *,
        signal_record: dict[str, Any] | None,
        signal_id: str,
        symbol: str,
        environment: str,
        reason: str,
    ) -> dict[str, Any]:
        pb = getattr(self, "pb", None)
        if not pb:
            return {"ok": False, "reason": "pb_unavailable"}
        service_mod = _service_mod()
        try:
            from ibkr_compute.universe.target_lifecycle import demote_entry_activated_targets_after_close

            current_market_date = str(getattr(self, "_current_market_date", "") or "").strip()
            if not current_market_date:
                market_date_fn = getattr(self, "_market_date", None)
                if callable(market_date_fn):
                    current_market_date = str(market_date_fn() or "").strip()
            if not current_market_date:
                current_market_date = datetime.now(service_mod.ET).strftime("%Y-%m-%d")
            dates = [
                (signal_record or {}).get("date"),
                current_market_date,
            ]
            result = demote_entry_activated_targets_after_close(
                pb,
                symbol=symbol,
                environment=environment,
                signal_id=signal_id,
                dates=dates,
                reason=reason,
                now_iso=self._now_iso_for_signal_patch(),
                logger=service_mod.logger,
            )
            if int((result or {}).get("demoted") or 0) > 0:
                service_mod.logger.info(
                    "Demoted entry-activated target after signal close: symbol=%s signal_id=%s demoted=%s",
                    symbol,
                    signal_id,
                    result.get("demoted"),
                )
            return result
        except Exception as exc:
            service_mod.logger.warning(
                "Failed to demote entry-activated target after signal close: symbol=%s signal_id=%s error=%s",
                symbol,
                signal_id,
                exc,
            )
            return {"ok": False, "reason": "exception", "error": str(exc)}

    def _update_signal_after_exit_fill(
        self,
        order: dict,
        symbol: str,
        role: str,
        *,
        signal_record: dict[str, Any] | None = None,
    ) -> bool:
        service_mod = _service_mod()
        if not getattr(self, "pb", None):
            return True
        broker_environment = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
        data_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower() or "live"
        normalized_role = self._normalize_order_role(role)
        if normalized_role not in {"stop_loss", "take_profit", "close"}:
            normalized_role = "take_profit"
        status_reason = "closed_by_manual_close" if normalized_role == "close" else f"closed_by_{normalized_role}"
        order_id = str(order.get("broker_order_id") or order.get("order_id") or order.get("orderId") or "").strip()
        fill_price = self._first_nonempty_order_value(
            order,
            "avgPrice",
            "avgFillPrice",
            "fill_price",
            "filled_price",
            "lastFillPrice",
            "price",
        )
        filled_qty = self._first_nonempty_order_value(
            order,
            "filledQuantity",
            "filled_qty",
            "filled",
            "totalSize",
            "quantity",
        )

        try:
            signal_id = self._resolve_signal_id_for_order(order, broker_environment)
            if not signal_id:
                return True

            if not signal_record:
                signal_record = self.pb.get_first_record(
                    "ibkr_signals",
                    filter=(
                        f'signal_id = "{self._escape_filter_value(signal_id)}" && '
                        f'environment = "{self._escape_filter_value(data_environment)}"'
                    ),
                )
            if not signal_record or not signal_record.get("id"):
                return True
            current_status = str(signal_record.get("status") or "").strip().lower()
            existing_extra = self._safe_extra(signal_record.get("extra"))
            pb_exit_order = self._load_pb_order_for_event(order, broker_environment)
            exit_order_for_pnl = {**pb_exit_order, **order} if pb_exit_order else order
            if pb_exit_order:
                merged_extra = {
                    **self._safe_extra(pb_exit_order.get("extra")),
                    **self._safe_extra(order.get("extra")),
                }
                if merged_extra:
                    exit_order_for_pnl["extra"] = merged_extra
            if current_status == "closed":
                trade_group_id = self._trade_group_id_for_exit(signal_record, exit_order_for_pnl)
                pnl_result = self._exit_realized_pnl(
                    signal_record=signal_record,
                    exit_order=exit_order_for_pnl,
                    signal_id=signal_id,
                    environment=broker_environment,
                    trade_group_id=trade_group_id,
                )
                self._patch_exit_order_pnl(exit_order_for_pnl, pnl_result)
                if pnl_result.get("ok"):
                    self.pb.update_record(
                        "ibkr_signals",
                        str(signal_record.get("id")),
                        {
                            "extra": {
                                **existing_extra,
                                "realized_pnl": pnl_result.get("net_pnl", 0.0),
                                "realized_net_pnl": pnl_result.get("net_pnl", 0.0),
                                "realized_gross_pnl": pnl_result.get("gross_pnl", 0.0),
                                "total_commission": pnl_result.get("commission", 0.0),
                                "entry_commission": pnl_result.get("entry_commission", 0.0),
                                "exit_commission": pnl_result.get("exit_commission", 0.0),
                                "realized_pnl_pct": pnl_result.get("pnl_pct", 0.0),
                                "realized_pnl_source": pnl_result.get("source", "computed_from_entry_exit_fills"),
                                "realized_pnl_diagnostic": pnl_result,
                                "pnl": pnl_result.get("net_pnl", 0.0),
                            },
                        },
                    )
                self._demote_entry_target_after_signal_close(
                    signal_record=signal_record,
                    signal_id=signal_id,
                    symbol=symbol,
                    environment=data_environment,
                    reason=str(existing_extra.get("status_reason") or "signal_already_closed"),
                )
                return False

            trade_group_id = self._trade_group_id_for_exit(signal_record, exit_order_for_pnl)
            pnl_result = self._exit_realized_pnl(
                signal_record=signal_record,
                exit_order=exit_order_for_pnl,
                signal_id=signal_id,
                environment=broker_environment,
                trade_group_id=trade_group_id,
            )
            self._patch_exit_order_pnl(exit_order_for_pnl, pnl_result)
            extra = {
                **existing_extra,
                "status_reason": status_reason,
                "closed_by": "order_tracker",
                "closed_at": self._now_iso_for_signal_patch(),
                "exit_fill_detected_by": "order_tracker",
                "exit_fill_role": normalized_role,
                "exit_fill_broker_order_id": order_id,
                "exit_fill_status": str(order.get("status") or ""),
                "exit_fill_symbol": symbol,
                "exit_fill_side": str(order.get("side") or ""),
                "exit_fill_order_type": str(order.get("orderType") or order.get("order_type") or ""),
                "exit_fill_price": fill_price,
                "exit_fill_quantity": filled_qty,
                "exit_price": pnl_result.get("exit_price") or self._coerce_float(fill_price, 0.0),
                "exit_quantity": pnl_result.get("quantity") or self._coerce_float(filled_qty, 0.0),
                "realized_pnl": pnl_result.get("net_pnl", 0.0) if pnl_result.get("ok") else existing_extra.get("realized_pnl", 0.0),
                "realized_net_pnl": pnl_result.get("net_pnl", 0.0) if pnl_result.get("ok") else existing_extra.get("realized_net_pnl", 0.0),
                "realized_gross_pnl": pnl_result.get("gross_pnl", 0.0) if pnl_result.get("ok") else existing_extra.get("realized_gross_pnl", 0.0),
                "total_commission": pnl_result.get("commission", 0.0) if pnl_result.get("ok") else existing_extra.get("total_commission", 0.0),
                "entry_commission": pnl_result.get("entry_commission", 0.0) if pnl_result.get("ok") else existing_extra.get("entry_commission", 0.0),
                "exit_commission": pnl_result.get("exit_commission", 0.0) if pnl_result.get("ok") else existing_extra.get("exit_commission", 0.0),
                "realized_pnl_pct": pnl_result.get("pnl_pct", 0.0) if pnl_result.get("ok") else existing_extra.get("realized_pnl_pct", 0.0),
                "realized_pnl_source": pnl_result.get("source", "computed_from_entry_exit_fills"),
                "realized_pnl_diagnostic": pnl_result,
                "pnl": pnl_result.get("net_pnl", 0.0) if pnl_result.get("ok") else existing_extra.get("pnl", 0.0),
                "protection_active": False,
                "protection_incomplete": False,
                "safety_cancel_recommended": False,
            }
            self.pb.update_record(
                "ibkr_signals",
                str(signal_record.get("id")),
                {
                    "status": "closed",
                    "note": status_reason,
                    "extra": extra,
                },
            )
            self._demote_entry_target_after_signal_close(
                signal_record=signal_record,
                signal_id=signal_id,
                symbol=symbol,
                environment=data_environment,
                reason=status_reason,
            )
            return True
        except Exception as exc:
            service_mod.logger.error("Failed to close signal after exit fill: %s", exc)
        return True

    def _stop_loss_count_snapshot(self) -> tuple[int, int, bool]:
        lifecycle = getattr(self, "order_lifecycle", None)
        count = 0
        limit = 0
        if lifecycle:
            status_fn = getattr(lifecycle, "status", None)
            status: dict[str, Any] = {}
            if callable(status_fn):
                try:
                    raw_status = status_fn()
                    status = raw_status if isinstance(raw_status, dict) else {}
                except Exception:
                    status = {}
            for attr in ("_daily_sl_count", "daily_sl_count", "sl_count", "consecutive_stop_loss_count"):
                try:
                    value = getattr(lifecycle, attr)
                except Exception:
                    value = None
                if value not in (None, ""):
                    count = int(value)
                    break
            else:
                for key in ("daily_sl_count", "consecutive_stop_loss_count"):
                    if status.get(key) not in (None, ""):
                        count = int(status.get(key) or 0)
                        break
            limit_fn = getattr(lifecycle, "_consecutive_stop_loss_limit", None)
            if callable(limit_fn):
                try:
                    limit = int(limit_fn() or 0)
                except Exception:
                    limit = 0
            if limit <= 0:
                for attr in ("consecutive_stop_loss_limit", "sl_limit"):
                    try:
                        value = getattr(lifecycle, attr)
                    except Exception:
                        value = None
                    if callable(value):
                        try:
                            value = value()
                        except Exception:
                            value = None
                    if value not in (None, ""):
                        limit = int(value)
                        break
            if limit <= 0 and status.get("consecutive_stop_loss_limit") not in (None, ""):
                limit = int(status.get("consecutive_stop_loss_limit") or 0)
            try:
                breaker = bool(getattr(lifecycle, "is_sl_circuit_breaker"))
            except Exception:
                breaker = False
        else:
            breaker = False
        if limit > 0 and count >= limit:
            breaker = True
        return max(0, count), max(0, limit), breaker

    def _notify_stop_loss_fill(self, symbol: str, order: dict | None = None, *, cooldown_bars: int = 0) -> None:
        pb = getattr(self, "pb", None)
        notifier = getattr(pb, "notify_system_event", None)
        if not callable(notifier):
            return

        service_mod = _service_mod()
        order = order if isinstance(order, dict) else {}
        broker_environment = str(service_mod.ENVIRONMENT or "paper").strip().lower() or "paper"
        count, limit, breaker = self._stop_loss_count_snapshot()
        level = "error" if breaker else "warning"
        title = "连续止损熔断已触发" if breaker else "止损成交报警"
        signal_id = str(order.get("signal_id") or "").strip()
        if not signal_id and getattr(self, "pb", None):
            try:
                signal_id = self._resolve_signal_id_for_order(order, broker_environment)
            except Exception:
                signal_id = ""
        order_id = str(order.get("broker_order_id") or order.get("order_id") or order.get("orderId") or "").strip()
        fill_price = self._first_nonempty_order_value(
            order,
            "avgPrice",
            "avgFillPrice",
            "fill_price",
            "filled_price",
            "lastFillPrice",
            "price",
        )
        filled_qty = self._first_nonempty_order_value(
            order,
            "filledQuantity",
            "filled_qty",
            "filled",
            "totalSize",
            "quantity",
        )
        conclusion = (
            "连续止损次数已达到风控上限，系统进入 sl_circuit_breaker，新的开仓信号会被拒绝。"
            if breaker
            else "止损保护单已成交，标的已移出活跃持仓并进入止损冷却。"
        )
        action = (
            "暂停追单，检查行情环境、策略质量和最近成交；确认风险后再决定是否重置计数或调参。"
            if breaker
            else "检查该标的走势、信号质量和保护单成交价；冷却结束前不要重复开同标的仓位。"
        )
        detail = {
            "状态结论": conclusion,
            "标的": str(symbol or order.get("ticker") or order.get("symbol") or "").strip().upper() or "-",
            "信号ID": signal_id or "-",
            "Broker订单ID": order_id or "-",
            "成交价": fill_price if fill_price not in (None, "") else "-",
            "成交数量": filled_qty if filled_qty not in (None, "") else "-",
            "订单Side": str(order.get("side") or "-"),
            "订单类型": str(order.get("orderType") or order.get("order_type") or "-"),
            "连续止损次数": count,
            "连续止损上限": limit if limit > 0 else "-",
            "熔断状态": "yes" if breaker else "no",
            "冷却K线": cooldown_bars if cooldown_bars > 0 else "-",
            "处理建议": action,
        }
        try:
            notifier(
                title,
                detail,
                event_type="alert",
                level=level,
                source="ibkr_compute",
                environment=broker_environment,
            )
        except Exception as exc:
            service_mod.logger.warning("Stop-loss notification failed: %s", exc)

    def _apply_exit_fill_side_effects(self, symbol: str, role: str, order: dict | None = None) -> None:
        self.signal_processor.remove_position(symbol)
        if role == "stop_loss":
            self.order_lifecycle.increment_sl_count()
            cooldown_bars = self.signal_processor.cooldown_bars_after_sl()
            self.signal_processor.start_cooldown(
                symbol,
                cooldown_bars,
                "cooldown_after_stop_loss",
            )
            try:
                self._notify_stop_loss_fill(symbol, order, cooldown_bars=cooldown_bars)
            except Exception as exc:
                _service_mod().logger.warning("Stop-loss notification failed: %s", exc)
        else:
            self.order_lifecycle.reset_sl_count()

    def _release_buying_power_reservation_for_order(self, order: dict, *, reason: str) -> None:
        store = getattr(self, "buying_power_reservations", None)
        releaser = getattr(store, "release", None)
        if not callable(releaser):
            return
        order_id = str(
            order.get("orderId")
            or order.get("order_id")
            or order.get("broker_order_id")
            or order.get("id")
            or ""
        ).strip()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        try:
            if order_id:
                result = releaser(entry_order_id=order_id, reason=reason)
                if symbol and isinstance(result, dict) and int(result.get("released") or 0) <= 0:
                    releaser(symbol=symbol, reason=f"{reason}_symbol_fallback")
            elif symbol:
                releaser(symbol=symbol, reason=reason)
        except Exception as exc:
            _service_mod().logger.warning("Buying-power reservation release failed: %s", exc)

    def _on_order_fill(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        order_type = str(order.get("orderType") or order.get("order_type") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        side = str(order.get("side") or "").strip().upper()
        runtime_environment = str(service_mod.ENVIRONMENT or "live").strip().lower() or "live"
        role = "entry"
        if has_parent and order_type in {"STP", "STOP", "STOPLOSS"}:
            role = "stop_loss"
        elif has_parent:
            role = "take_profit"
        resolved_role = self._resolve_order_role_for_fill_event(order, runtime_environment)
        if resolved_role in {"stop_loss", "take_profit", "close"}:
            role = resolved_role
        elif not has_parent and str(order.get("cOID") or order.get("coid") or order.get("orderRef") or "").lower().startswith("close_"):
            role = "close"
        service_mod.logger.info("Order filled: %s role=%s", symbol or order.get("ticker"), role)
        if not symbol:
            return
        if role == "entry":
            self._release_buying_power_reservation_for_order(order, reason="entry_fill")
            direction = "long" if side == "BUY" else "short" if side == "SELL" else ""
            self.signal_processor.register_filled_position(
                symbol,
                {
                    "direction": direction,
                    "broker_order_id": str(order.get("orderId") or order.get("order_id") or ""),
                    "state": "filled_position",
                },
            )
            closed_role = self._update_signal_after_entry_fill(order, symbol, direction)
            if closed_role:
                self._apply_exit_fill_side_effects(symbol, closed_role, order)
            return
        if self._update_signal_after_exit_fill(order, symbol, role):
            self._apply_exit_fill_side_effects(symbol, role, order)

    def _on_order_cancel(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        service_mod.logger.info("Order cancelled: %s", symbol or order.get("ticker"))
        if symbol and not has_parent:
            self._release_buying_power_reservation_for_order(order, reason="entry_cancel")
            self._update_signal_after_entry_cancel(order, symbol)
        if symbol and not has_parent:
            self.signal_processor.remove_position(symbol)

    def _scheduler_retention_handled_current_hour(self, et_now: datetime) -> bool:
        service_mod = _service_mod()
        try:
            from ibkr_compute.api.service_topology import get_scheduler_internal_url

            response = requests.get(
                f"{get_scheduler_internal_url()}/status",
                params={
                    "broker_mode": service_mod.ENVIRONMENT,
                    "market_data_mode": service_mod.DATA_ENVIRONMENT,
                    "data_environment": service_mod.DATA_ENVIRONMENT,
                },
                timeout=2.0,
            )
            if not response.ok:
                return False
            payload = response.json()
        except Exception:
            return False
        if not isinstance(payload, dict) or str(payload.get("status") or "").strip().lower() != "running":
            return False
        jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
        state = jobs.get("ibkr_history_retention") if isinstance(jobs.get("ibkr_history_retention"), dict) else {}
        if not state:
            return False

        def same_hour(timestamp_ms) -> bool:
            try:
                timestamp = int(timestamp_ms or 0)
                if timestamp <= 0:
                    return False
                handled_at = datetime.fromtimestamp(timestamp / 1000.0, service_mod.ET)
            except Exception:
                return False
            return handled_at.strftime("%Y-%m-%d %H") == et_now.strftime("%Y-%m-%d %H")

        status = str(state.get("status") or "").strip().lower()
        if status == "running" and same_hour(state.get("last_run_started_at_ms")):
            return True
        return same_hour(state.get("last_success_at_ms"))

    def _schedule_retention(self):
        service_mod = _service_mod()

        def retention_loop():
            last_handled_hour = ""
            while self._running:
                et_now = datetime.now(service_mod.ET)
                hour_key = et_now.strftime("%Y-%m-%d %H")
                if et_now.minute == 12 and hour_key != last_handled_hour:
                    if self._scheduler_retention_handled_current_hour(et_now):
                        service_mod.logger.info(
                            "Skip runtime retention cleanup because scheduler handled current hour: environment=%s hour=%s",
                            service_mod.DATA_ENVIRONMENT,
                            hour_key,
                        )
                        last_handled_hour = hour_key
                        time.sleep(30)
                        continue
                    result = self.data_retention.cleanup(source="runtime_thread")
                    env_result = (result.get("environments") or [{}])[0]
                    service_mod.logger.info(
                        "Runtime retention cleanup finished: environment=%s deleted=%s errors=%s skipped=%s reason=%s",
                        service_mod.DATA_ENVIRONMENT,
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

        if self._account_snapshot_thread:
            self._account_snapshot_thread.join(timeout=5)
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
