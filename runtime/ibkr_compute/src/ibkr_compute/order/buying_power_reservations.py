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


def _state_data(record: dict[str, Any] | None) -> dict[str, Any]:
    data = (record or {}).get("data") if isinstance(record, dict) else {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


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
        data["version"] = int(data.get("version") or 1)
        data["reservations"] = [dict(item) for item in reservations if isinstance(item, dict)]
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
        }
        return upsert(STATE_KEY, self.environment, payload, date=_active_date())

    @staticmethod
    def _is_active(item: dict[str, Any], now_epoch: float | None = None) -> bool:
        if str(item.get("status") or "active").strip().lower() != "active":
            return False
        expires_epoch = _parse_epoch(item.get("expires_at") or item.get("expires_epoch"))
        if expires_epoch > 0 and expires_epoch <= float(now_epoch or time.time()):
            return False
        return True

    def cleanup_expired(self) -> dict[str, Any]:
        with _state_lock(self.environment):
            data, _record = self._load_state()
            now_epoch = time.time()
            before = len(data.get("reservations") or [])
            data["reservations"] = [
                item for item in (data.get("reservations") or [])
                if self._is_active(item, now_epoch)
            ]
            removed = before - len(data["reservations"])
            if removed:
                self._save_state(data)
            return {"removed": removed, "active": len(data["reservations"])}

    def active_reservations(self) -> list[dict[str, Any]]:
        with _state_lock(self.environment):
            data, _record = self._load_state()
            active = [item for item in (data.get("reservations") or []) if self._is_active(item)]
            if len(active) != len(data.get("reservations") or []):
                data["reservations"] = active
                self._save_state(data)
            return [dict(item) for item in active]

    def snapshot(self) -> dict[str, Any]:
        active = self.active_reservations()
        exposure = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in active)
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
            current_reserved = sum(max(0.0, _safe_float(item.get("exposure"), 0.0)) for item in active)
            guard_remaining = _safe_float(guard.get("remaining"), 0.0)
            guard_local_reserved = _safe_float(guard.get("local_reserved_exposure"), 0.0)
            account_remaining = _safe_float(
                guard.get("account_remaining_buying_power"),
                guard_remaining + guard_local_reserved,
            )
            adjusted_remaining = max(0.0, account_remaining - current_reserved)
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
    adjusted = dict(summary or {})
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
    return guard


__all__ = [
    "BuyingPowerReservationStore",
    "STATE_KEY",
    "apply_reservations_to_buying_power_summary",
    "merge_reservation_snapshot_into_guard",
]
