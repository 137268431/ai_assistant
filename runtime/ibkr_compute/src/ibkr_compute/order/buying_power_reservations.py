from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ibkr_compute.core.time_utils import ET


STATE_KEY = "buying_power_reservations:v1"
DEFAULT_RESERVATION_TTL_SECONDS = 24 * 60 * 60
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[tuple[str, str], threading.RLock] = {}
LEDGER_SAFE_GUARD_META_KEYS = (
    "ledger_safe_used",
    "ledger_snapshot_fetched_at",
    "ledger_snapshot_age_s",
    "ledger_snapshot_source",
    "ledger_snapshot_et_date",
    "ledger_today_et_date",
    "ledger_today_entry_exposure_available",
    "ledger_today_entry_exposure_error",
    "ledger_today_entry_order_ids",
    "ledger_account_remaining_buying_power",
    "ledger_today_open_exposure",
    "ledger_pending_reserved_exposure",
    "ledger_requested_exposure",
    "ledger_remaining_after",
    "ledger_remaining_before_request",
    "ledger_remaining_pct_net_liq",
    "ledger_remaining_after_pct_net_liq",
    "ledger_next_refresh_at",
    "ledger_safe_freshness_reason",
    "ledger_safe_block_reason",
    "ledger_today_entry_count",
    "ledger_pending_reserved_count",
    "refresh_block_reason",
    "original_freshness_block_reason",
    "snapshot_stale_allowed_reason",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return float(number)


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return float(number)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _parse_epoch(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    number = _safe_float(value, 0.0)
    if number > 0:
        return number
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _active_date() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 0:
            try:
                return datetime.fromtimestamp(number / 1000.0 if number > 10_000_000_000 else number, timezone.utc)
            except Exception:
                return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        if "T" not in normalized and len(normalized) >= 19:
            parsed = datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")
            return parsed.replace(tzinfo=ET)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _state_data(record: dict[str, Any] | None) -> dict[str, Any]:
    data = (record or {}).get("data") if isinstance(record, dict) else {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


def _escape_filter_value(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _reservation_key(
    *,
    entry_order_id: Any = "",
    trade_group_id: Any = "",
    signal_id: Any = "",
    symbol: Any = "",
) -> str:
    for prefix, value in (
        ("order", entry_order_id),
        ("group", trade_group_id),
        ("signal", signal_id),
        ("symbol", symbol),
    ):
        text = str(value or "").strip()
        if text:
            return f"{prefix}:{text}"
    return f"reservation:{int(time.time() * 1000)}"


def _state_lock(environment: str) -> threading.RLock:
    key = (str(environment or "live").strip().lower() or "live", _active_date())
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STATE_LOCKS[key] = lock
        return lock


def _summary_remaining(summary: dict[str, Any]) -> float | None:
    if not isinstance(summary, dict):
        return None
    for key in ("remaining_buying_power", "buying_power"):
        value = _optional_float(summary.get(key))
        if value is not None:
            return value
    return None


def _summary_has_substantive_value(summary: dict[str, Any]) -> bool:
    if not isinstance(summary, dict):
        return False
    for key in (
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "equity_with_loan",
        "gross_position_value",
        "total_cash_value",
        "initial_margin",
        "maintenance_margin",
    ):
        value = summary.get(key)
        if value in (None, ""):
            continue
        number = _optional_float(value)
        if number is None or number != 0.0:
            return True
    return bool(str(summary.get("account_type") or "").strip())


def _row_extra(row: dict[str, Any] | None) -> dict[str, Any]:
    extra = (row or {}).get("extra") if isinstance(row, dict) else {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    return extra if isinstance(extra, dict) else {}


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    extra = _row_extra(row)
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
        if extra.get(key) not in (None, ""):
            return extra.get(key)
    return ""


def base_buying_power_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Return account buying power before this store's local reservation overlay."""
    base = dict(summary or {})
    existing_reserved = max(0.0, _safe_float(base.get("local_reserved_exposure"), 0.0))
    if existing_reserved > 0:
        for key in ("remaining_buying_power", "buying_power"):
            value = base.get(key)
            if value not in (None, ""):
                base[key] = _safe_float(value, 0.0) + existing_reserved
    for key in ("local_reserved_exposure", "local_reserved_count", "local_reserved_order_ids"):
        base.pop(key, None)
    return base


class BuyingPowerReservationStore:
    def __init__(self, pb_client: Any = None, *, environment: str = "live", ttl_seconds: float | None = None):
        self.pb_client = pb_client
        self.environment = str(environment or "live").strip().lower() or "live"
        self.ttl_seconds = max(60.0, float(ttl_seconds or DEFAULT_RESERVATION_TTL_SECONDS))

    def _load_state(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        getter = getattr(self.pb_client, "get_state", None)
        if not callable(getter):
            return {"version": 1, "reservations": []}, None
        try:
            record = getter(STATE_KEY, self.environment, _active_date())
        except Exception:
            return {"version": 1, "reservations": []}, None
        data = _state_data(record)
        reservations = data.get("reservations")
        if not isinstance(reservations, list):
            reservations = []
        baseline = data.get("baseline")
        data["version"] = int(data.get("version") or 1)
        data["reservations"] = [dict(item) for item in reservations if isinstance(item, dict)]
        data["baseline"] = dict(baseline) if isinstance(baseline, dict) else {}
        return data, record if isinstance(record, dict) else None

    def _save_state(self, data: dict[str, Any]) -> dict[str, Any] | None:
        upsert = getattr(self.pb_client, "upsert_state", None)
        if not callable(upsert):
            return None
        payload = {
            "version": 1,
            "updated_at": _iso(),
            "reservations": list(data.get("reservations") or []),
            "last_release": data.get("last_release") if isinstance(data.get("last_release"), dict) else {},
            "baseline": data.get("baseline") if isinstance(data.get("baseline"), dict) else {},
        }
        return upsert(STATE_KEY, self.environment, payload, date=_active_date())

    @staticmethod
    def _snapshot_baseline_payload(snapshot: dict[str, Any] | None) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        guard = snapshot.get("buying_power_guard") if isinstance(snapshot.get("buying_power_guard"), dict) else {}
        guard_state = str(guard.get("state") or "").strip().lower()
        if guard_state == "unavailable" or guard.get("available") is False:
            return {}
        summary = base_buying_power_summary(snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {})
        remaining = _summary_remaining(summary)
        if remaining is None:
            return {}
        if remaining == 0.0 and not _summary_has_substantive_value(summary):
            return {}
        fetched_at = str(snapshot.get("fetched_at") or guard.get("snapshot_fetched_at") or _iso()).strip()
        source = str(guard.get("source") or snapshot.get("source") or "account_summary").strip() or "account_summary"
        baseline = {
            "available": True,
            "environment": str(snapshot.get("environment") or "").strip().lower(),
            "summary": summary,
            "remaining_buying_power": remaining,
            "net_liquidation": _safe_float(summary.get("net_liquidation"), 0.0),
            "source": source,
            "snapshot_source": str(snapshot.get("snapshot_source") or snapshot.get("source") or source).strip(),
            "fetched_at": fetched_at,
            "stored_at": _iso(),
            "cache_state": str(snapshot.get("cache_state") or "").strip(),
            "stale": bool(snapshot.get("stale")),
        }
        for key in (
            "configured_buying_power",
            "risk_model",
            "risk_model_default_entry_exposure",
            "risk_model_position_exposure",
            "risk_model_open_order_exposure",
            "risk_model_used_exposure",
            "risk_model_strategy_position_count",
            "risk_model_strategy_entry_order_count",
            "risk_model_strategy_position_symbols",
            "risk_model_strategy_entry_order_symbols",
        ):
            if guard.get(key) not in (None, ""):
                baseline[key] = guard.get(key)
        return baseline

    def update_baseline_from_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        baseline = self._snapshot_baseline_payload(snapshot)
        if not baseline:
            return {"ok": False, "reason": "snapshot_unavailable"}
        if not baseline.get("environment"):
            baseline["environment"] = self.environment
        with _state_lock(self.environment):
            data, _record = self._load_state()
            data["baseline"] = baseline
            self._save_state(data)
        return {"ok": True, "baseline": dict(baseline)}

    def baseline_snapshot(self) -> dict[str, Any]:
        with _state_lock(self.environment):
            data, _record = self._load_state()
            baseline = data.get("baseline") if isinstance(data.get("baseline"), dict) else {}
            if not baseline or not baseline.get("available"):
                return {"available": False, "environment": self.environment, "date": _active_date()}
            summary = dict(baseline.get("summary") or {})
            if _summary_remaining(summary) is None:
                return {"available": False, "environment": self.environment, "date": _active_date()}
            return {
                **dict(baseline),
                "available": True,
                "environment": str(baseline.get("environment") or self.environment).strip().lower() or self.environment,
                "date": _active_date(),
                "summary": summary,
            }

    @staticmethod
    def _is_active(item: dict[str, Any], now_epoch: float | None = None) -> bool:
        if str(item.get("status") or "active").strip().lower() != "active":
            return False
        expires_epoch = _parse_epoch(item.get("expires_at") or item.get("expires_epoch"))
        if expires_epoch > 0 and expires_epoch <= float(now_epoch or time.time()):
            return False
        return True

    @staticmethod
    def _pb_order_row_terminal(row: dict[str, Any]) -> bool:
        status = str((row or {}).get("status") or "").strip().upper()
        relation_status = str((row or {}).get("relation_status") or "").strip().lower()
        return relation_status == "closed" or status in {
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }

    def _load_pb_order_rows_for_entry_ids(self, entry_order_ids: list[str]) -> list[dict[str, Any]]:
        getter = getattr(self.pb_client, "get_records", None)
        if not callable(getter):
            return []
        ids = [str(item or "").strip() for item in (entry_order_ids or []) if str(item or "").strip()]
        if not ids:
            return []
        environment_filter = _escape_filter_value(self.environment)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ids), 25):
            chunk = ids[start:start + 25]
            id_filter = " || ".join(
                f'broker_order_id = "{_escape_filter_value(order_id)}" || order_id = "{_escape_filter_value(order_id)}"'
                for order_id in chunk
            )
            try:
                batch = getter(
                    "orders",
                    filter=f'environment = "{environment_filter}" && ({id_filter})',
                    sort="-updated",
                    per_page=max(50, len(chunk) * 2),
                )
            except Exception:
                continue
            rows.extend(dict(row) for row in (batch or []) if isinstance(row, dict))
        return rows

    @staticmethod
    def _row_is_today(row: dict[str, Any]) -> bool:
        target_date = _active_date()
        for key in (
            "us_time",
            "order_time",
            "fill_time",
            "created",
            "updated",
            "broker_callback_received_at",
        ):
            raw = _row_value(row, key)
            if raw in (None, ""):
                continue
            parsed = _parse_datetime(raw)
            if parsed is None:
                if str(raw).startswith(target_date):
                    return True
                continue
            if parsed.astimezone(ET).strftime("%Y-%m-%d") == target_date:
                return True
        return False

    @staticmethod
    def _row_role(row: dict[str, Any]) -> str:
        role = str(_row_value(row, "role", "leg_role", "order_role") or "").strip().lower()
        if role:
            return role
        unique_id = str(_row_value(row, "unique_id", "order_ref", "orderRef", "coid") or "").strip().lower()
        if unique_id.startswith("entry_"):
            return "entry"
        return ""

    @staticmethod
    def _row_status(row: dict[str, Any]) -> str:
        return str(_row_value(row, "status", "order_status", "orderStatus", "current_status") or "").strip()

    @staticmethod
    def _row_is_countable_entry(row: dict[str, Any]) -> bool:
        if BuyingPowerReservationStore._row_role(row) != "entry":
            return False
        status_key = BuyingPowerReservationStore._row_status(row).replace("_", "").replace(" ", "").lower()
        filled_qty = _safe_float(_row_value(row, "filled_qty", "filledQuantity", "filled"), 0.0)
        if status_key in {"canceled", "cancelled", "apicanceled", "apicancelled", "rejected", "expired", "inactive"}:
            return filled_qty > 0
        if status_key in {"closed"}:
            return filled_qty > 0
        return True

    @staticmethod
    def _row_entry_order_id(row: dict[str, Any]) -> str:
        for key in ("broker_order_id", "order_id", "orderId", "id"):
            value = str(_row_value(row, key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _row_entry_exposure(row: dict[str, Any]) -> float:
        for key in (
            "entry_notional",
            "submitted_entry_notional",
            "requested_exposure",
            "exposure",
        ):
            value = _safe_float(_row_value(row, key), 0.0)
            if value > 0:
                return value
        quantity = abs(_safe_float(_row_value(row, "quantity", "totalSize", "totalQuantity", "shares"), 0.0))
        price = 0.0
        for key in (
            "fill_price",
            "avg_price",
            "avgFillPrice",
            "entry_price",
            "submitted_entry_limit_price",
            "limit_price",
            "price",
        ):
            price = abs(_safe_float(_row_value(row, key), 0.0))
            if price > 0:
                break
        return max(0.0, quantity * price)

    def today_entry_exposure_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.pb_client, "get_records", None)
        if not callable(getter):
            return {"available": False, "reason": "pb_client_unavailable", "date": _active_date()}
        environment_filter = _escape_filter_value(self.environment)
        try:
            rows = getter(
                "orders",
                filter=f'environment = "{environment_filter}"',
                sort="-updated",
                per_page=500,
            )
        except Exception as exc:
            return {"available": False, "reason": str(exc) or "orders_query_failed", "date": _active_date()}
        entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        exposure = 0.0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if not self._row_is_today(row) or not self._row_is_countable_entry(row):
                continue
            order_id = self._row_entry_order_id(row)
            dedupe_key = order_id or str(_row_value(row, "unique_id", "trade_group_id", "signal_id") or "")
            if dedupe_key and dedupe_key in seen_ids:
                continue
            if dedupe_key:
                seen_ids.add(dedupe_key)
            row_exposure = self._row_entry_exposure(row)
            if row_exposure <= 0:
                continue
            exposure += row_exposure
            entries.append(
                {
                    "order_id": order_id,
                    "symbol": str(_row_value(row, "symbol") or "").strip().upper(),
                    "direction": str(_row_value(row, "direction", "position_side", "side") or "").strip().lower(),
                    "status": self._row_status(row),
                    "exposure": round(row_exposure, 2),
                }
            )
        return {
            "available": True,
            "date": _active_date(),
            "count": len(entries),
            "exposure": exposure,
            "order_ids": [item["order_id"] for item in entries if item.get("order_id")],
            "entries": entries,
        }

    def _reconcile_active_with_pb_locked(self, active: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
        entry_ids = [
            str(item.get("entry_order_id") or "").strip()
            for item in active
            if str(item.get("entry_order_id") or "").strip()
        ]
        if not entry_ids:
            return active
        rows = self._load_pb_order_rows_for_entry_ids(entry_ids)
        if not rows:
            return active
        rows_by_order_id: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for key in ("broker_order_id", "order_id"):
                order_id = str(row.get(key) or "").strip()
                if order_id:
                    rows_by_order_id.setdefault(order_id, []).append(row)
        terminal_ids = {
            order_id
            for order_id, order_rows in rows_by_order_id.items()
            if any(self._pb_order_row_terminal(row) for row in order_rows)
        }
        if not terminal_ids:
            return active
        remaining = [
            item for item in active
            if str(item.get("entry_order_id") or "").strip() not in terminal_ids
        ]
        released = len(active) - len(remaining)
        if released:
            data["last_release"] = {
                "released_at": _iso(),
                "reason": "pb_terminal_reconcile",
                "count": released,
                "order_ids": sorted(terminal_ids),
                "symbols": sorted({
                    str(item.get("symbol") or "").upper()
                    for item in active
                    if str(item.get("entry_order_id") or "").strip() in terminal_ids and item.get("symbol")
                }),
            }
        return remaining

    def cleanup_expired(self) -> dict[str, Any]:
        with _state_lock(self.environment):
            data, _record = self._load_state()
            now_epoch = time.time()
            before = len(data.get("reservations") or [])
            data["reservations"] = [
                item for item in (data.get("reservations") or [])
                if self._is_active(item, now_epoch)
            ]
            data["reservations"] = self._reconcile_active_with_pb_locked(data["reservations"], data)
            removed = before - len(data["reservations"])
            if removed:
                self._save_state(data)
            return {"removed": removed, "active": len(data["reservations"])}

    def active_reservations(self) -> list[dict[str, Any]]:
        with _state_lock(self.environment):
            data, _record = self._load_state()
            active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
            active = self._reconcile_active_with_pb_locked(active, data)
            if len(active) != len(data.get("reservations") or []):
                data["reservations"] = active
                self._save_state(data)
            return [dict(item) for item in active]

    def snapshot(self) -> dict[str, Any]:
        active = self.active_reservations()
        exposure = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in active)
        today_entries = self.today_entry_exposure_snapshot()
        today_entry_order_ids = {
            str(item or "").strip()
            for item in (today_entries.get("order_ids") or [])
            if str(item or "").strip()
        }
        pending_reservations = [
            item for item in active
            if not str(item.get("entry_order_id") or "").strip()
            or str(item.get("entry_order_id") or "").strip() not in today_entry_order_ids
        ]
        pending_exposure = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in pending_reservations)
        baseline = self.baseline_snapshot()
        return {
            "state_key": STATE_KEY,
            "environment": self.environment,
            "date": _active_date(),
            "active": active,
            "count": len(active),
            "exposure": exposure,
            "order_ids": [
                str(item.get("entry_order_id") or "").strip()
                for item in active
                if str(item.get("entry_order_id") or "").strip()
            ],
            "baseline": baseline if baseline.get("available") else {},
            "baseline_available": bool(baseline.get("available")),
            "today_entry_exposure_available": bool(today_entries.get("available")),
            "today_entry_exposure": float(today_entries.get("exposure") or 0.0),
            "today_entry_count": int(today_entries.get("count") or 0),
            "today_entry_order_ids": list(today_entries.get("order_ids") or []),
            "today_entry_exposure_error": "" if today_entries.get("available") else str(today_entries.get("reason") or ""),
            "pending_reservation_exposure": pending_exposure,
            "pending_reservation_count": len(pending_reservations),
        }

    def reserve_entry(
        self,
        *,
        signal_id: Any = "",
        trade_group_id: Any = "",
        entry_order_id: Any = "",
        symbol: Any = "",
        direction: Any = "",
        quantity: Any = 0,
        entry_price: Any = 0.0,
        exposure: Any = 0.0,
        source: str = "order_submission",
    ) -> dict[str, Any]:
        with _state_lock(self.environment):
            return self._reserve_entry_locked(
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                entry_order_id=entry_order_id,
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=entry_price,
                exposure=exposure,
                source=source,
            )

    def _reserve_entry_locked(
        self,
        *,
        signal_id: Any = "",
        trade_group_id: Any = "",
        entry_order_id: Any = "",
        symbol: Any = "",
        direction: Any = "",
        quantity: Any = 0,
        entry_price: Any = 0.0,
        exposure: Any = 0.0,
        source: str = "order_submission",
    ) -> dict[str, Any]:
        if not self.pb_client:
            return {"ok": False, "error": "pb_client_unavailable"}
        quantity_int = abs(_safe_int(quantity, 0))
        price = _safe_float(entry_price, 0.0)
        exposure_value = max(0.0, _safe_float(exposure, 0.0) or (quantity_int * price))
        if exposure_value <= 0:
            return {"ok": False, "error": "reservation_exposure_unavailable"}
        key = _reservation_key(
            entry_order_id=entry_order_id,
            trade_group_id=trade_group_id,
            signal_id=signal_id,
            symbol=symbol,
        )
        created_at = _now_utc()
        expires_at = created_at + timedelta(seconds=self.ttl_seconds)
        data, _record = self._load_state()
        active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
        reservation = {
            "key": key,
            "status": "active",
            "source": str(source or "order_submission"),
            "signal_id": str(signal_id or "").strip(),
            "trade_group_id": str(trade_group_id or "").strip(),
            "entry_order_id": str(entry_order_id or "").strip(),
            "symbol": str(symbol or "").strip().upper(),
            "direction": str(direction or "").strip().lower(),
            "quantity": quantity_int,
            "entry_price": price,
            "exposure": exposure_value,
            "created_at": _iso(created_at),
            "expires_at": _iso(expires_at),
        }
        replaced = False
        next_active = []
        for item in active:
            if str(item.get("key") or "") == key:
                next_active.append(reservation)
                replaced = True
            else:
                next_active.append(item)
        if not replaced:
            next_active.append(reservation)
        data["reservations"] = next_active
        self._save_state(data)
        return {"ok": True, "reservation": reservation, "replaced": replaced}

    def reserve_entry_if_available(
        self,
        *,
        pre_submit_guard: dict[str, Any] | None = None,
        config: Any = None,
        signal_id: Any = "",
        trade_group_id: Any = "",
        symbol: Any = "",
        direction: Any = "",
        quantity: Any = 0,
        entry_price: Any = 0.0,
        exposure: Any = 0.0,
        source: str = "pre_gateway_submission",
    ) -> dict[str, Any]:
        if not self.pb_client:
            return {"ok": False, "error": "buying_power_unavailable", "reason": "pb_client_unavailable"}
        exposure_value = max(0.0, _safe_float(exposure, 0.0) or (abs(_safe_int(quantity, 0)) * _safe_float(entry_price, 0.0)))
        if exposure_value <= 0:
            return {"ok": False, "error": "buying_power_blocked", "reason": "buying_power_price_unavailable"}

        from ibkr_compute.api.account.buying_power_guard import build_buying_power_guard

        guard = dict(pre_submit_guard or {})
        with _state_lock(self.environment):
            data, _record = self._load_state()
            active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
            reconciled = self._reconcile_active_with_pb_locked(active, data)
            if len(reconciled) != len(active):
                active = reconciled
                data["reservations"] = active
                self._save_state(data)
            current_reserved = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in active)
            guard_remaining = _safe_float(guard.get("remaining"), 0.0)
            guard_local_reserved = _safe_float(guard.get("local_reserved_exposure"), 0.0)
            account_remaining = _safe_float(
                guard.get("account_remaining_buying_power"),
                guard_remaining + guard_local_reserved,
            )
            pending_reserved = current_reserved
            pending_reservation_count = len(active)
            today_entry_count = guard.get("ledger_today_entry_count")
            if _guard_uses_ledger_safe(guard):
                today_entries = self.today_entry_exposure_snapshot()
                today_entry_order_ids = {
                    str(item or "").strip()
                    for item in (today_entries.get("order_ids") or [])
                    if str(item or "").strip()
                }
                pending_reservations = [
                    item for item in active
                    if not str(item.get("entry_order_id") or "").strip()
                    or str(item.get("entry_order_id") or "").strip() not in today_entry_order_ids
                ]
                pending_reserved = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in pending_reservations)
                pending_reservation_count = len(pending_reservations)
                if today_entries.get("available"):
                    today_entry_count = int(today_entries.get("count") or 0)
            ledger_remaining = _ledger_adjusted_remaining_before_request(guard, pending_reserved)
            adjusted_remaining = ledger_remaining if ledger_remaining is not None else max(0.0, account_remaining - current_reserved)
            recheck_guard = build_buying_power_guard(
                {
                    "buying_power": adjusted_remaining,
                    "remaining_buying_power": adjusted_remaining,
                    "net_liquidation": _safe_float(guard.get("net_liquidation"), 0.0),
                },
                config=config,
                environment=self.environment,
                requested_exposure=exposure_value,
            )
            recheck_guard["account_remaining_buying_power"] = account_remaining
            recheck_guard["pre_submit_remaining"] = guard.get("remaining")
            recheck_guard["pre_submit_remaining_after"] = guard.get("remaining_after")
            for key in (
                "source",
                "snapshot_fetched_at",
                "baseline_available",
                "baseline_source",
                "baseline_fetched_at",
                "baseline_stored_at",
                "baseline_cache_state",
                "configured_buying_power",
                "risk_model",
                "risk_model_default_entry_exposure",
                "risk_model_position_exposure",
                "risk_model_open_order_exposure",
                "risk_model_used_exposure",
                "risk_model_strategy_position_count",
                "risk_model_strategy_entry_order_count",
                "risk_model_strategy_position_symbols",
                "risk_model_strategy_entry_order_symbols",
                *LEDGER_SAFE_GUARD_META_KEYS,
            ):
                if guard.get(key) not in (None, ""):
                    recheck_guard[key] = guard.get(key)
            if ledger_remaining is not None:
                recheck_guard["source"] = "today_ledger_safe"
                recheck_guard["ledger_safe_used"] = True
                recheck_guard["ledger_pending_reserved_exposure"] = round(pending_reserved, 2)
                recheck_guard["ledger_pending_reserved_count"] = pending_reservation_count
                if today_entry_count not in (None, ""):
                    recheck_guard["ledger_today_entry_count"] = int(today_entry_count or 0)
                recheck_guard["ledger_remaining_before_request"] = round(adjusted_remaining, 2)
                recheck_guard["ledger_remaining_after"] = round(max(0.0, adjusted_remaining - exposure_value), 2)
            recheck_guard["local_reserved_exposure"] = current_reserved
            recheck_guard["local_reserved_count"] = len(active)
            recheck_guard["local_reserved_order_ids"] = [
                str(item.get("entry_order_id") or "").strip()
                for item in active
                if str(item.get("entry_order_id") or "").strip()
            ]
            recheck_guard["local_reservation_state_key"] = STATE_KEY
            state = str(recheck_guard.get("state") or "").strip().lower()
            if state in {"blocked", "unavailable"}:
                error = "buying_power_unavailable" if state == "unavailable" else "buying_power_blocked"
                return {
                    "ok": False,
                    "error": error,
                    "reason": str(recheck_guard.get("reason") or error),
                    "buying_power_guard": recheck_guard,
                    "pre_submit_buying_power_guard": guard,
                    "local_reserved_exposure": current_reserved,
                    "local_reserved_count": len(active),
                }

            reservation_result = self._reserve_entry_locked(
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                entry_order_id="",
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=entry_price,
                exposure=exposure_value,
                source=source,
            )
            reservation = dict(reservation_result.get("reservation") or {})
            recheck_guard["local_reserved_exposure_after"] = current_reserved + exposure_value
            recheck_guard["local_reserved_count_after"] = len(active) + 1
            return {
                "ok": bool(reservation_result.get("ok")),
                "error": reservation_result.get("error"),
                "reservation": reservation,
                "buying_power_guard": recheck_guard,
                "pre_submit_buying_power_guard": guard,
                "local_reserved_exposure": current_reserved,
                "local_reserved_exposure_after": current_reserved + exposure_value,
            }

    def attach_entry_order_id(
        self,
        *,
        reservation_key: Any = "",
        entry_order_id: Any = "",
        trade_group_id: Any = "",
        signal_id: Any = "",
        symbol: Any = "",
    ) -> dict[str, Any]:
        entry_order_id_text = str(entry_order_id or "").strip()
        if not entry_order_id_text:
            return {"ok": False, "error": "missing_entry_order_id"}
        with _state_lock(self.environment):
            data, _record = self._load_state()
            active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
            key_text = str(reservation_key or "").strip()
            trade_group_text = str(trade_group_id or "").strip()
            signal_text = str(signal_id or "").strip()
            symbol_text = str(symbol or "").strip().upper()
            updated = None
            next_active = []
            for item in active:
                matches = bool(key_text and key_text == str(item.get("key") or "").strip())
                matches = matches or bool(trade_group_text and trade_group_text == str(item.get("trade_group_id") or "").strip())
                matches = matches or bool(signal_text and signal_text == str(item.get("signal_id") or "").strip())
                matches = matches or bool(symbol_text and symbol_text == str(item.get("symbol") or "").strip().upper())
                if matches and updated is None:
                    item = dict(item)
                    item["entry_order_id"] = entry_order_id_text
                    item["key"] = _reservation_key(entry_order_id=entry_order_id_text)
                    item["updated_at"] = _iso()
                    updated = item
                next_active.append(item)
            if updated is None:
                return {"ok": False, "error": "reservation_not_found"}
            data["reservations"] = next_active
            self._save_state(data)
            return {"ok": True, "reservation": updated}

    def release(
        self,
        *,
        reservation_key: Any = "",
        entry_order_id: Any = "",
        trade_group_id: Any = "",
        signal_id: Any = "",
        symbol: Any = "",
        reason: str = "released",
    ) -> dict[str, Any]:
        if not self.pb_client:
            return {"ok": False, "error": "pb_client_unavailable", "released": 0}
        with _state_lock(self.environment):
            data, _record = self._load_state()
            active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
            entry_order_id_text = str(entry_order_id or "").strip()
            trade_group_id_text = str(trade_group_id or "").strip()
            signal_id_text = str(signal_id or "").strip()
            symbol_text = str(symbol or "").strip().upper()
            reservation_key_text = str(reservation_key or "").strip()

            def matches(item: dict[str, Any]) -> bool:
                if reservation_key_text and reservation_key_text == str(item.get("key") or "").strip():
                    return True
                if entry_order_id_text and entry_order_id_text == str(item.get("entry_order_id") or "").strip():
                    return True
                if trade_group_id_text and trade_group_id_text == str(item.get("trade_group_id") or "").strip():
                    return True
                if signal_id_text and signal_id_text == str(item.get("signal_id") or "").strip():
                    return True
                if symbol_text and symbol_text == str(item.get("symbol") or "").strip().upper():
                    return True
                return False

            released = [item for item in active if matches(item)]
            remaining = [item for item in active if not matches(item)]
            data["reservations"] = remaining
            if released:
                data["last_release"] = {
                    "released_at": _iso(),
                    "reason": str(reason or "released"),
                    "count": len(released),
                    "order_ids": [item.get("entry_order_id") for item in released if item.get("entry_order_id")],
                    "symbols": sorted({str(item.get("symbol") or "").upper() for item in released if item.get("symbol")}),
                }
                self._save_state(data)
            return {"ok": True, "released": len(released), "remaining": len(remaining)}


def apply_reservations_to_buying_power_summary(
    summary: dict[str, Any] | None,
    reservation_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    adjusted = base_buying_power_summary(summary)
    snapshot = reservation_snapshot if isinstance(reservation_snapshot, dict) else {}
    reserved = max(0.0, _safe_float(snapshot.get("exposure"), 0.0))
    if reserved <= 0:
        adjusted.setdefault("local_reserved_exposure", 0.0)
        adjusted.setdefault("local_reserved_count", 0)
        adjusted.setdefault("local_reserved_order_ids", [])
        return adjusted
    for key in ("remaining_buying_power", "buying_power"):
        current = adjusted.get(key)
        if current not in (None, ""):
            adjusted[key] = max(0.0, _safe_float(current, 0.0) - reserved)
    adjusted["local_reserved_exposure"] = reserved
    adjusted["local_reserved_count"] = int(snapshot.get("count") or 0)
    adjusted["local_reserved_order_ids"] = list(snapshot.get("order_ids") or [])
    return adjusted


def merge_reservation_snapshot_into_guard(guard: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    guard["local_reserved_exposure"] = max(0.0, _safe_float(snapshot.get("exposure"), 0.0))
    guard["local_reserved_count"] = int(snapshot.get("count") or 0)
    guard["local_reserved_order_ids"] = list(snapshot.get("order_ids") or [])
    guard["local_reservation_state_key"] = STATE_KEY
    for source_key, target_key in (
        ("today_entry_exposure_available", "ledger_today_entry_exposure_available"),
        ("today_entry_exposure", "ledger_today_open_exposure"),
        ("today_entry_count", "ledger_today_entry_count"),
        ("today_entry_order_ids", "ledger_today_entry_order_ids"),
        ("today_entry_exposure_error", "ledger_today_entry_exposure_error"),
        ("pending_reservation_exposure", "ledger_pending_reserved_exposure"),
        ("pending_reservation_count", "ledger_pending_reserved_count"),
    ):
        if snapshot.get(source_key) not in (None, ""):
            value = snapshot.get(source_key)
            if source_key.endswith("_exposure"):
                value = max(0.0, _safe_float(value, 0.0))
            guard[target_key] = value
    return guard


def _guard_uses_ledger_safe(guard: dict[str, Any] | None) -> bool:
    payload = guard if isinstance(guard, dict) else {}
    return bool(payload.get("ledger_safe_used")) or str(payload.get("source") or "").strip().lower() == "today_ledger_safe"


def _ledger_adjusted_remaining_before_request(
    guard: dict[str, Any] | None,
    current_pending_reserved: Any,
) -> float | None:
    payload = guard if isinstance(guard, dict) else {}
    if not _guard_uses_ledger_safe(payload):
        return None
    base = _optional_float(payload.get("ledger_remaining_before_request"))
    if base is None:
        base = _optional_float(payload.get("remaining"))
    if base is None:
        return None
    previous_pending = max(0.0, _safe_float(payload.get("ledger_pending_reserved_exposure"), 0.0))
    current_pending = max(0.0, _safe_float(current_pending_reserved, 0.0))
    extra_pending = max(0.0, current_pending - previous_pending)
    return max(0.0, float(base) - extra_pending)


__all__ = [
    "BuyingPowerReservationStore",
    "LEDGER_SAFE_GUARD_META_KEYS",
    "STATE_KEY",
    "apply_reservations_to_buying_power_summary",
    "base_buying_power_summary",
    "merge_reservation_snapshot_into_guard",
]
