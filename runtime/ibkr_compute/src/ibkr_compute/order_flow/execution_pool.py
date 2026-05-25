from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .models import freeze_mapping, normalize_position_side, normalize_symbol


DEFAULT_RESERVATION_TTL_MS = 30_000
DEFAULT_FILL_WATCH_TTL_MS = 120_000
FILLED_OUTCOMES = {"fill", "filled", "complete", "completed"}
RELEASE_OUTCOMES = {"cancel", "canceled", "cancelled", "expired", "reject", "rejected", "timeout"}


@dataclass(frozen=True, slots=True)
class ExecutionSlot:
    reservation_id: str
    symbol: str
    side: str
    state: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int | None = None
    candidate_id: str = ""
    order_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        if not symbol:
            raise ValueError("symbol is required")
        side = normalize_position_side(self.side)
        state = str(self.state or "").strip().lower()
        if state not in {"reserved", "fill_watch", "open"}:
            raise ValueError("state must be reserved, fill_watch, or open")

        object.__setattr__(self, "reservation_id", str(self.reservation_id or "").strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "created_at_ms", int(self.created_at_ms or 0))
        object.__setattr__(self, "updated_at_ms", int(self.updated_at_ms or 0))
        object.__setattr__(
            self,
            "expires_at_ms",
            int(self.expires_at_ms) if self.expires_at_ms is not None else None,
        )
        object.__setattr__(self, "candidate_id", str(self.candidate_id or ""))
        object.__setattr__(self, "order_id", str(self.order_id or ""))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @property
    def uses_position_slot(self) -> bool:
        return self.state in {"reserved", "fill_watch", "open"}

    @property
    def is_pending_entry(self) -> bool:
        return self.state in {"reserved", "fill_watch"}

    def is_expired(self, now_ms: int) -> bool:
        return self.state != "open" and self.expires_at_ms is not None and int(now_ms) >= self.expires_at_ms


@dataclass(frozen=True, slots=True)
class ExecutionPoolDecision:
    accepted: bool
    reason: str
    slot: ExecutionSlot | None = None
    released: tuple[ExecutionSlot, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionCandidateWatch:
    symbol: str
    direction: str
    conid: int
    candidate_key: str
    state: str
    allocated_at_ms: int
    filled_at_ms: int | None = None
    release_at_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "direction", normalize_position_side(self.direction))
        object.__setattr__(self, "conid", int(self.conid or 0))
        object.__setattr__(self, "candidate_key", str(self.candidate_key or ""))
        object.__setattr__(self, "state", str(self.state or "entry_watch"))
        object.__setattr__(self, "allocated_at_ms", int(self.allocated_at_ms or 0))
        object.__setattr__(
            self,
            "filled_at_ms",
            int(self.filled_at_ms) if self.filled_at_ms is not None else None,
        )
        object.__setattr__(
            self,
            "release_at_ms",
            int(self.release_at_ms) if self.release_at_ms is not None else None,
        )


class ExecutionPoolManager:
    """Tracks deterministic symbol admission and new-entry slot reservations."""

    def __init__(
        self,
        *,
        max_symbols: int = 3,
        max_position_slots: int = 1,
        reservation_ttl_ms: int = DEFAULT_RESERVATION_TTL_MS,
        fill_watch_ttl_ms: int = DEFAULT_FILL_WATCH_TTL_MS,
        entry_watch_after_fill_sec: int | float = 0,
    ) -> None:
        self.max_symbols = max(0, int(max_symbols))
        self.max_position_slots = max(0, int(max_position_slots))
        self.reservation_ttl_ms = max(0, int(reservation_ttl_ms))
        self.fill_watch_ttl_ms = max(0, int(fill_watch_ttl_ms))
        self.entry_watch_after_fill_ms = max(0, int(float(entry_watch_after_fill_sec or 0) * 1000))
        self._slots: dict[str, ExecutionSlot] = {}
        self._candidate_watches: dict[str, ExecutionCandidateWatch] = {}
        self._sequence = 0

    def reserve_new_entry(
        self,
        symbol: str,
        *,
        side: str = "unknown",
        candidate_id: str = "",
        now_ms: int = 0,
        ttl_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionPoolDecision:
        released = tuple(self.purge_expired(now_ms))
        symbol = normalize_symbol(symbol)
        if not symbol:
            return ExecutionPoolDecision(False, "symbol_required", released=released)

        if self.max_position_slots <= 0:
            return ExecutionPoolDecision(False, "position_slots_disabled", released=released)

        existing_for_symbol = self._slot_for_symbol(symbol)
        if existing_for_symbol is not None:
            return ExecutionPoolDecision(False, "symbol_already_active", slot=existing_for_symbol, released=released)

        active_symbols = set(self.active_symbols())
        if symbol not in active_symbols and self.max_symbols > 0 and len(active_symbols) >= self.max_symbols:
            return ExecutionPoolDecision(False, "symbol_pool_full", released=released)

        if self.used_position_slots >= self.max_position_slots:
            return ExecutionPoolDecision(False, "position_slots_full", released=released)

        self._sequence += 1
        ttl = self.reservation_ttl_ms if ttl_ms is None else max(0, int(ttl_ms))
        expires_at_ms = int(now_ms) + ttl if ttl > 0 else None
        slot = ExecutionSlot(
            reservation_id=f"entry-{self._sequence}",
            symbol=symbol,
            side=side,
            state="reserved",
            created_at_ms=int(now_ms),
            updated_at_ms=int(now_ms),
            expires_at_ms=expires_at_ms,
            candidate_id=str(candidate_id or ""),
            metadata=metadata or {},
        )
        self._slots[slot.reservation_id] = slot
        return ExecutionPoolDecision(True, "reserved", slot=slot, released=released)

    def allocate_candidate(
        self,
        candidate: Any,
        *,
        conid: int = 0,
        at_ms: int = 0,
    ) -> tuple[bool, ExecutionCandidateWatch | None, str]:
        symbol = normalize_symbol(getattr(candidate, "symbol", ""))
        direction = normalize_position_side(getattr(candidate, "direction", getattr(candidate, "side", "")))
        candidate_key = str(getattr(candidate, "key", getattr(candidate, "candidate_id", "")) or f"{symbol}:{direction}")
        if not symbol:
            return False, None, "symbol_required"

        existing = self._candidate_watches.get(symbol)
        if existing is not None:
            return True, existing, "already_allocated"

        entry_slot_limit = max(0, self.max_symbols - self.max_position_slots)
        active_entry_watches = [
            watch for watch in self._candidate_watches.values() if watch.state == "entry_watch"
        ]
        if entry_slot_limit <= 0 or len(active_entry_watches) >= entry_slot_limit:
            return False, None, "entry_slots_full"

        watch = ExecutionCandidateWatch(
            symbol=symbol,
            direction=direction,
            conid=int(conid or 0),
            candidate_key=candidate_key,
            state="entry_watch",
            allocated_at_ms=int(at_ms),
            release_at_ms=int(at_ms) + self.reservation_ttl_ms if self.reservation_ttl_ms > 0 else None,
        )
        self._candidate_watches[symbol] = watch
        return True, watch, "allocated"

    def start_fill_watch(
        self,
        reservation_id: str,
        *,
        order_id: str = "",
        now_ms: int = 0,
        ttl_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionPoolDecision:
        released = tuple(self.purge_expired(now_ms))
        slot = self._slots.get(str(reservation_id or "")) or self._find_slot(reservation_id)
        if slot is None:
            return ExecutionPoolDecision(False, "reservation_not_found", released=released)
        if slot.state == "open":
            return ExecutionPoolDecision(False, "position_already_open", slot=slot, released=released)

        ttl = self.fill_watch_ttl_ms if ttl_ms is None else max(0, int(ttl_ms))
        expires_at_ms = int(now_ms) + ttl if ttl > 0 else None
        payload = dict(slot.metadata)
        payload.update(dict(metadata or {}))
        updated = replace(
            slot,
            state="fill_watch",
            order_id=str(order_id or slot.order_id or ""),
            updated_at_ms=int(now_ms),
            expires_at_ms=expires_at_ms,
            metadata=payload,
        )
        self._slots[updated.reservation_id] = updated
        return ExecutionPoolDecision(True, "fill_watch_started", slot=updated, released=released)

    def release_fill_watch(
        self,
        identifier: str,
        *,
        outcome: str,
        now_ms: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionPoolDecision:
        released = tuple(self.purge_expired(now_ms))
        slot = self._find_slot(identifier)
        if slot is None:
            return ExecutionPoolDecision(False, "fill_watch_not_found", released=released)
        if slot.state not in {"reserved", "fill_watch"}:
            return ExecutionPoolDecision(False, "not_a_pending_entry", slot=slot, released=released)

        normalized_outcome = str(outcome or "").strip().lower()
        payload = dict(slot.metadata)
        payload.update(dict(metadata or {}))
        payload["fill_watch_outcome"] = normalized_outcome

        if normalized_outcome in FILLED_OUTCOMES:
            opened = replace(
                slot,
                state="open",
                updated_at_ms=int(now_ms),
                expires_at_ms=None,
                metadata=payload,
            )
            self._slots[opened.reservation_id] = opened
            return ExecutionPoolDecision(True, "filled_open_position", slot=opened, released=(slot, *released))

        if normalized_outcome in RELEASE_OUTCOMES:
            self._slots.pop(slot.reservation_id, None)
            return ExecutionPoolDecision(True, "released_without_position", released=(slot, *released))

        return ExecutionPoolDecision(False, "unknown_fill_watch_outcome", slot=slot, released=released)

    def release_position(
        self,
        identifier: str,
        *,
        now_ms: int = 0,
    ) -> ExecutionPoolDecision:
        released = tuple(self.purge_expired(now_ms))
        slot = self._find_slot(identifier)
        if slot is None:
            return ExecutionPoolDecision(False, "position_not_found", released=released)
        if slot.state != "open":
            return ExecutionPoolDecision(False, "position_not_open", slot=slot, released=released)
        self._slots.pop(slot.reservation_id, None)
        return ExecutionPoolDecision(True, "position_released", released=(slot, *released))

    def mark_filled_watch(
        self,
        symbol: str,
        *,
        conid: int = 0,
        at_ms: int = 0,
    ) -> ExecutionCandidateWatch | None:
        normalized_symbol = normalize_symbol(symbol)
        watch = self._candidate_watches.get(normalized_symbol)
        if watch is None:
            return None
        if conid and int(conid) != watch.conid:
            return None
        release_at_ms = int(at_ms) + self.entry_watch_after_fill_ms if self.entry_watch_after_fill_ms > 0 else None
        updated = replace(
            watch,
            state="filled_watch",
            filled_at_ms=int(at_ms),
            release_at_ms=release_at_ms,
        )
        self._candidate_watches[normalized_symbol] = updated
        return updated

    def upsert_position_watch(
        self,
        symbol: str,
        *,
        direction: str = "unknown",
        conid: int = 0,
        at_ms: int = 0,
        candidate_key: str = "",
    ) -> tuple[bool, ExecutionCandidateWatch | None, str]:
        normalized_symbol = normalize_symbol(symbol)
        if not normalized_symbol:
            return False, None, "symbol_required"
        existing = self._candidate_watches.get(normalized_symbol)
        position_watches = [
            watch for watch in self._candidate_watches.values() if watch.state == "open_position"
        ]
        if existing is None and self.max_position_slots > 0 and len(position_watches) >= self.max_position_slots:
            return False, None, "position_slots_full"
        updated = ExecutionCandidateWatch(
            symbol=normalized_symbol,
            direction=direction or (existing.direction if existing is not None else "unknown"),
            conid=int(conid or (existing.conid if existing is not None else 0)),
            candidate_key=str(candidate_key or (existing.candidate_key if existing is not None else f"{normalized_symbol}:position")),
            state="open_position",
            allocated_at_ms=existing.allocated_at_ms if existing is not None else int(at_ms),
            filled_at_ms=existing.filled_at_ms if existing is not None else int(at_ms),
            release_at_ms=None,
        )
        self._candidate_watches[normalized_symbol] = updated
        return True, updated, "position_watch_upserted"

    def release_expired_watches(self, at_ms: int) -> list[ExecutionCandidateWatch]:
        expired = [
            watch
            for watch in self._candidate_watches.values()
            if watch.state in {"entry_watch", "filled_watch"}
            and watch.release_at_ms is not None
            and int(at_ms) >= watch.release_at_ms
        ]
        for watch in expired:
            self._candidate_watches.pop(watch.symbol, None)
        return expired

    def release_candidate_watch(self, symbol: str) -> ExecutionCandidateWatch | None:
        return self._candidate_watches.pop(normalize_symbol(symbol), None)

    def purge_expired(self, now_ms: int) -> list[ExecutionSlot]:
        expired = [slot for slot in self._slots.values() if slot.is_expired(now_ms)]
        for slot in expired:
            self._slots.pop(slot.reservation_id, None)
        return expired

    def active_symbols(self) -> list[str]:
        return sorted(
            {
                *[slot.symbol for slot in self._slots.values()],
                *[watch.symbol for watch in self._candidate_watches.values()],
            }
        )

    def slots(self) -> list[ExecutionSlot]:
        return sorted(self._slots.values(), key=lambda slot: slot.reservation_id)

    @property
    def used_position_slots(self) -> int:
        return sum(1 for slot in self._slots.values() if slot.uses_position_slot)

    @property
    def pending_entry_slots(self) -> int:
        return sum(1 for slot in self._slots.values() if slot.is_pending_entry)

    @property
    def open_position_slots(self) -> int:
        return sum(1 for slot in self._slots.values() if slot.state == "open")

    def status(self) -> dict[str, Any]:
        return {
            "max_symbols": self.max_symbols,
            "active_symbols": self.active_symbols(),
            "active_symbol_count": len(self.active_symbols()),
            "max_position_slots": self.max_position_slots,
            "entry_slot_capacity": max(0, self.max_symbols - self.max_position_slots),
            "entry_slot_count": sum(1 for watch in self._candidate_watches.values() if watch.state == "entry_watch"),
            "used_position_slots": self.used_position_slots,
            "pending_entry_slots": self.pending_entry_slots,
            "open_position_slots": self.open_position_slots,
            "position_slot_count": self.open_position_slots
            + sum(1 for watch in self._candidate_watches.values() if watch.state in {"filled_watch", "open_position"}),
            "fill_watch_slots": sum(1 for slot in self._slots.values() if slot.state == "fill_watch"),
        }

    def _slot_for_symbol(self, symbol: str) -> ExecutionSlot | None:
        for slot in self._slots.values():
            if slot.symbol == symbol:
                return slot
        return None

    def _find_slot(self, identifier: str) -> ExecutionSlot | None:
        normalized = str(identifier or "")
        if normalized in self._slots:
            return self._slots[normalized]
        symbol = normalize_symbol(normalized)
        for slot in self._slots.values():
            if slot.order_id and slot.order_id == normalized:
                return slot
            if symbol and slot.symbol == symbol:
                return slot
        return None
