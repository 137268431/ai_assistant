from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from ibkr_compute.market.timeframe_utils import normalize_interval

ComputeSlot = tuple[str, str, str]


def compute_lock_stale_threshold_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("IBKR_COMPUTE_LOCK_STALE_THRESHOLD_SEC", "600") or "600"))
    except (TypeError, ValueError):
        return 600.0


def normalize_compute_slot(environment: str, symbol: str, interval: str) -> ComputeSlot:
    return (
        str(environment or "").strip().lower() or "live",
        str(symbol or "").strip().upper(),
        normalize_interval(interval),
    )


@dataclass
class ComputeLockLease:
    manager: "ComputeLockManager"
    slots: tuple[ComputeSlot, ...]
    global_scope: bool = False
    acquired: bool = True
    request: "ComputeLockRequest | None" = None
    lease_id: int = 0
    acquired_at: float = 0.0
    thread_id: int = 0
    thread_name: str = ""

    def __enter__(self) -> "ComputeLockLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        self.manager.release(self)


@dataclass(frozen=True)
class ComputeLockRequest:
    reason: str
    slots: tuple[ComputeSlot, ...] = ()
    global_scope: bool = False

    @classmethod
    def global_lock(cls, reason: str = "global") -> "ComputeLockRequest":
        return cls(reason=reason, global_scope=True)

    def describe(self) -> dict:
        payload = {
            "lock_scope": "global" if self.global_scope else "slots",
            "lock_reason": self.reason,
        }
        if self.slots:
            payload["lock_slots"] = [
                {
                    "environment": environment,
                    "symbol": symbol,
                    "interval": interval,
                }
                for environment, symbol, interval in self.slots[:20]
            ]
            payload["lock_slot_count"] = len(self.slots)
        return payload


class ComputeLockManager:
    """Coordinates global and per-symbol compute work.

    Targeted compute/prime requests only need to serialize with work touching the
    same environment/symbol/interval. Untargeted maintenance still uses a global
    barrier so it cannot overlap with targeted leases from this manager.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active_global = False
        self._active_slot_leases = 0
        self._slot_locks: dict[ComputeSlot, threading.RLock] = {}
        self._next_lease_id = 0
        self._active_leases: dict[int, ComputeLockLease] = {}
        self._active_global_lease_id = 0

    def acquire(self, request: ComputeLockRequest, timeout: float | None = None) -> ComputeLockLease | None:
        if request.global_scope:
            return self.acquire_global(timeout, request=request)
        return self.acquire_slots(request.slots, timeout, request=request)

    def acquire_global(
        self,
        timeout: float | None = None,
        *,
        request: ComputeLockRequest | None = None,
    ) -> ComputeLockLease | None:
        deadline = self._deadline(timeout)
        with self._condition:
            while self._active_global or self._active_slot_leases:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            self._active_global = True
            lease = self._new_lease(
                slots=(),
                global_scope=True,
                request=request or ComputeLockRequest.global_lock("global"),
            )
            self._active_global_lease_id = lease.lease_id
            return lease

    def acquire_slots(
        self,
        slots: Iterable[ComputeSlot],
        timeout: float | None = None,
        *,
        request: ComputeLockRequest | None = None,
    ) -> ComputeLockLease | None:
        normalized_slots = tuple(sorted({slot for slot in slots if slot[1] and slot[2]}))
        if not normalized_slots:
            return self.acquire_global(timeout, request=request or ComputeLockRequest.global_lock("global"))

        deadline = self._deadline(timeout)
        with self._condition:
            while self._active_global:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            self._active_slot_leases += 1
            slot_locks = [self._slot_locks.setdefault(slot, threading.RLock()) for slot in normalized_slots]

        acquired_locks: list[threading.RLock] = []
        try:
            for lock in slot_locks:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                acquired = lock.acquire(timeout=remaining) if remaining is not None else lock.acquire()
                if not acquired:
                    raise TimeoutError
                acquired_locks.append(lock)
        except TimeoutError:
            for lock in reversed(acquired_locks):
                lock.release()
            with self._condition:
                self._active_slot_leases -= 1
                self._condition.notify_all()
            return None

        with self._condition:
            return self._new_lease(
                slots=normalized_slots,
                global_scope=False,
                request=request or ComputeLockRequest(reason="slots", slots=normalized_slots),
            )

    def release(self, lease: ComputeLockLease) -> None:
        if lease.global_scope:
            with self._condition:
                self._active_leases.pop(int(lease.lease_id or 0), None)
                if self._active_global_lease_id == int(lease.lease_id or 0):
                    self._active_global_lease_id = 0
                    self._active_global = False
                self._condition.notify_all()
            return

        for slot in reversed(lease.slots):
            lock = self._slot_locks.get(slot)
            if lock is not None:
                lock.release()
        with self._condition:
            removed = self._active_leases.pop(int(lease.lease_id or 0), None)
            if removed is not None:
                self._active_slot_leases = max(0, self._active_slot_leases - 1)
            self._condition.notify_all()

    def snapshot(self) -> dict:
        now = time.time()
        stale_threshold_s = compute_lock_stale_threshold_seconds()
        with self._condition:
            leases = [
                self._lease_payload(lease, now=now, stale_threshold_s=stale_threshold_s)
                for lease in self._active_leases.values()
            ]
            stale_leases = [lease for lease in leases if bool(lease.get("stale"))]
            active_global_count = sum(1 for lease in self._active_leases.values() if lease.global_scope)
            active_slot_count = sum(1 for lease in self._active_leases.values() if not lease.global_scope)
            return {
                "active_global": bool(self._active_global),
                "active_global_lease_id": int(self._active_global_lease_id or 0),
                "active_slot_leases": int(self._active_slot_leases or 0),
                "active_lease_count": len(leases),
                "stale_threshold_s": round(stale_threshold_s, 3),
                "stale_lease_count": len(stale_leases),
                "stale_leases": stale_leases[:8],
                "leases": leases,
                "state_inconsistent": bool(
                    bool(self._active_global) != bool(active_global_count)
                    or int(self._active_slot_leases or 0) != active_slot_count
                ),
            }

    def describe_blockers(self, request: ComputeLockRequest | None, *, limit: int = 8) -> dict:
        requested = request or ComputeLockRequest.global_lock("global")
        requested_slots = set(requested.slots or ())
        now = time.time()
        stale_threshold_s = compute_lock_stale_threshold_seconds()
        blockers = []
        conflict_slots: set[ComputeSlot] = set()
        with self._condition:
            for lease in self._active_leases.values():
                if requested.global_scope or lease.global_scope:
                    matches = True
                    if not requested.global_scope and lease.global_scope:
                        conflict_slots.update(requested_slots)
                    elif requested.global_scope:
                        conflict_slots.update(lease.slots)
                else:
                    overlap = requested_slots.intersection(lease.slots)
                    matches = bool(overlap)
                    conflict_slots.update(overlap)
                if matches:
                    blockers.append(self._lease_payload(lease, now=now, stale_threshold_s=stale_threshold_s))
            blockers.sort(key=lambda item: float(item.get("age_s") or 0), reverse=True)
            stale_blockers = [item for item in blockers if bool(item.get("stale"))]
            snapshot = {
                "active_global": bool(self._active_global),
                "active_slot_leases": int(self._active_slot_leases or 0),
                "active_lease_count": len(self._active_leases),
                "stale_threshold_s": round(stale_threshold_s, 3),
                "stale_blocked_by_count": len(stale_blockers),
                "stale_blocked_by": stale_blockers[: max(0, int(limit or 0))],
                "blocked_by": blockers[: max(0, int(limit or 0))],
                "blocked_by_count": len(blockers),
                "conflict_slots": [
                    {"environment": env, "symbol": symbol, "interval": interval}
                    for env, symbol, interval in sorted(conflict_slots)[:20]
                ],
                "conflict_slot_count": len(conflict_slots),
            }
            return snapshot

    def _new_lease(
        self,
        *,
        slots: tuple[ComputeSlot, ...],
        global_scope: bool,
        request: ComputeLockRequest,
    ) -> ComputeLockLease:
        self._next_lease_id += 1
        current_thread = threading.current_thread()
        lease = ComputeLockLease(
            manager=self,
            slots=slots,
            global_scope=global_scope,
            request=request,
            lease_id=self._next_lease_id,
            acquired_at=time.time(),
            thread_id=int(threading.get_ident() or 0),
            thread_name=str(current_thread.name or ""),
        )
        self._active_leases[lease.lease_id] = lease
        return lease

    @staticmethod
    def _lease_payload(lease: ComputeLockLease, *, now: float, stale_threshold_s: float | None = None) -> dict:
        request = lease.request or ComputeLockRequest.global_lock("global" if lease.global_scope else "slots")
        age_s = round(max(0.0, float(now or time.time()) - float(lease.acquired_at or 0.0)), 3)
        threshold_s = compute_lock_stale_threshold_seconds() if stale_threshold_s is None else max(0.0, float(stale_threshold_s))
        payload = {
            "lease_id": int(lease.lease_id or 0),
            "scope": "global" if lease.global_scope else "slots",
            "reason": str(request.reason or ""),
            "acquired_at_ms": int(float(lease.acquired_at or 0.0) * 1000),
            "age_s": age_s,
            "stale": bool(threshold_s > 0 and age_s >= threshold_s),
            "stale_threshold_s": round(threshold_s, 3),
            "thread_id": int(lease.thread_id or 0),
            "thread_name": str(lease.thread_name or ""),
            "slot_count": len(lease.slots or ()),
        }
        if lease.slots:
            payload["slots"] = [
                {
                    "environment": environment,
                    "symbol": symbol,
                    "interval": interval,
                }
                for environment, symbol, interval in lease.slots[:20]
            ]
        return payload

    @staticmethod
    def _deadline(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        return time.monotonic() + max(0.0, float(timeout))

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())


def build_compute_slots(environments: Iterable[str], symbols: Iterable[str], intervals: Iterable[str]) -> list[ComputeSlot]:
    slots: list[ComputeSlot] = []
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    normalized_intervals = [normalize_interval(interval) for interval in intervals if str(interval or "").strip()]
    for environment in environments:
        for symbol in normalized_symbols:
            for interval in normalized_intervals:
                slots.append(normalize_compute_slot(environment, symbol, interval))
    return slots


def build_compute_lock_request(
    *,
    environments: Iterable[str],
    symbols: Iterable[str],
    intervals: Iterable[str],
    reason: str,
    global_scope: bool = False,
) -> ComputeLockRequest:
    if global_scope:
        return ComputeLockRequest.global_lock(reason)
    slots = tuple(build_compute_slots(environments, symbols, intervals))
    if not slots:
        return ComputeLockRequest.global_lock(reason)
    return ComputeLockRequest(reason=reason, slots=slots)


def build_compute_plan_lock_request(plan: dict) -> ComputeLockRequest:
    requested_symbols = list(plan.get("requested_symbols") or [])
    enabled_environments = list(plan.get("enabled_environments") or [])
    intervals = list(plan.get("intervals") or [])
    rollup_intervals = list(plan.get("rollup_intervals") or [])

    # Untargeted rollups or full-environment computes can update many cursors
    # and engines, so keep the conservative global barrier for those paths.
    if not requested_symbols:
        return ComputeLockRequest.global_lock("compute_plan_unbounded")
    if rollup_intervals and not bool(plan.get("targeted_rollup")):
        return ComputeLockRequest.global_lock("compute_plan_unbounded_rollup")

    lock_intervals = [*intervals, *rollup_intervals]
    return build_compute_lock_request(
        environments=enabled_environments,
        symbols=requested_symbols,
        intervals=lock_intervals,
        reason=str(plan.get("source") or "compute_plan"),
    )


__all__ = [
    "ComputeLockLease",
    "ComputeLockManager",
    "ComputeLockRequest",
    "ComputeSlot",
    "build_compute_lock_request",
    "build_compute_plan_lock_request",
    "build_compute_slots",
    "normalize_compute_slot",
]
