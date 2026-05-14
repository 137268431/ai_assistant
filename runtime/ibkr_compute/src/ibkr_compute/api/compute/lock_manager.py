from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable

from ibkr_compute.market.timeframe_utils import normalize_interval

ComputeSlot = tuple[str, str, str]


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

    def acquire(self, request: ComputeLockRequest, timeout: float | None = None) -> ComputeLockLease | None:
        if request.global_scope:
            return self.acquire_global(timeout)
        return self.acquire_slots(request.slots, timeout)

    def acquire_global(self, timeout: float | None = None) -> ComputeLockLease | None:
        deadline = self._deadline(timeout)
        with self._condition:
            while self._active_global or self._active_slot_leases:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            self._active_global = True
        return ComputeLockLease(self, (), global_scope=True)

    def acquire_slots(self, slots: Iterable[ComputeSlot], timeout: float | None = None) -> ComputeLockLease | None:
        normalized_slots = tuple(sorted({slot for slot in slots if slot[1] and slot[2]}))
        if not normalized_slots:
            return self.acquire_global(timeout)

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

        return ComputeLockLease(self, normalized_slots)

    def release(self, lease: ComputeLockLease) -> None:
        if lease.global_scope:
            with self._condition:
                self._active_global = False
                self._condition.notify_all()
            return

        for slot in reversed(lease.slots):
            lock = self._slot_locks.get(slot)
            if lock is not None:
                lock.release()
        with self._condition:
            self._active_slot_leases -= 1
            self._condition.notify_all()

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
