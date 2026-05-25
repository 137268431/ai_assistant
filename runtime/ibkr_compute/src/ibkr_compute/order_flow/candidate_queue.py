from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .models import freeze_mapping, normalize_position_side, normalize_symbol


DEFAULT_CANDIDATE_TTL_MS = 60_000
DEFAULT_CANDIDATE_STRATEGY = "order_flow_v1"


@dataclass(frozen=True, slots=True)
class OrderFlowCandidate:
    symbol: str
    side: str
    score: float
    created_at_ms: int
    candidate_id: str = ""
    strategy: str = DEFAULT_CANDIDATE_STRATEGY
    priority: int = 0
    ttl_ms: int | None = None
    expires_at_ms: int | None = None
    updated_at_ms: int | None = None
    reason: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        if not symbol:
            raise ValueError("symbol is required")

        side = normalize_position_side(self.side)
        if side == "unknown":
            raise ValueError("side must be long or short")

        created_at_ms = int(self.created_at_ms or 0)
        updated_at_ms = int(self.updated_at_ms if self.updated_at_ms is not None else created_at_ms)
        ttl_ms = int(self.ttl_ms) if self.ttl_ms is not None else None
        expires_at_ms = int(self.expires_at_ms) if self.expires_at_ms is not None else None
        strategy = str(self.strategy or DEFAULT_CANDIDATE_STRATEGY).strip() or DEFAULT_CANDIDATE_STRATEGY
        candidate_id = str(self.candidate_id or f"{strategy}:{symbol}:{side}").strip()

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "score", float(self.score or 0.0))
        object.__setattr__(self, "created_at_ms", created_at_ms)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "priority", int(self.priority or 0))
        object.__setattr__(self, "ttl_ms", ttl_ms)
        object.__setattr__(self, "expires_at_ms", expires_at_ms)
        object.__setattr__(self, "updated_at_ms", updated_at_ms)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "payload", freeze_mapping(self.payload))

    @property
    def merge_key(self) -> tuple[str, str, str]:
        return (self.symbol, self.side, self.strategy)

    def is_expired(self, now_ms: int) -> bool:
        return self.expires_at_ms is not None and int(now_ms) >= self.expires_at_ms


@dataclass(frozen=True, slots=True)
class CandidateQueueUpdate:
    action: str
    candidate: OrderFlowCandidate | None = None
    replaced: tuple[OrderFlowCandidate, ...] = ()
    expired: tuple[OrderFlowCandidate, ...] = ()
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.action in {"merged", "queued", "replaced_conflict"}


@dataclass(frozen=True, slots=True)
class CandidateQueueSignal:
    key: str
    symbol: str
    direction: str
    status: str
    signal_id: str = ""
    confluence: tuple[str, ...] = ()
    score: float = 0.0
    created_at_ms: int = 0
    updated_at_ms: int = 0
    expires_at_ms: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "direction", normalize_position_side(self.direction))
        object.__setattr__(self, "status", str(self.status or "active"))
        object.__setattr__(self, "signal_id", str(self.signal_id or ""))
        object.__setattr__(self, "confluence", tuple(str(item) for item in self.confluence if item))
        object.__setattr__(self, "score", float(self.score or 0.0))
        object.__setattr__(self, "created_at_ms", int(self.created_at_ms or 0))
        object.__setattr__(self, "updated_at_ms", int(self.updated_at_ms or 0))
        object.__setattr__(
            self,
            "expires_at_ms",
            int(self.expires_at_ms) if self.expires_at_ms is not None else None,
        )
        object.__setattr__(self, "extra", freeze_mapping(self.extra))


class CandidateQueueManager:
    """Ranks order-flow candidates while enforcing TTL and one direction per symbol."""

    def __init__(
        self,
        default_ttl_ms: int = DEFAULT_CANDIDATE_TTL_MS,
        *,
        max_candidates: int | None = None,
        ttl_by_setup: Mapping[str, int | float] | None = None,
    ) -> None:
        self.default_ttl_ms = max(0, int(default_ttl_ms))
        self.max_candidates = int(max_candidates) if max_candidates is not None else None
        self.ttl_by_setup = {str(key): value for key, value in dict(ttl_by_setup or {}).items()}
        self._candidates: dict[tuple[str, str, str], OrderFlowCandidate] = {}
        self._signals: dict[str, CandidateQueueSignal] = {}
        self._blocked_count = 0

    def upsert(
        self,
        candidate: OrderFlowCandidate | Mapping[str, Any],
        *,
        now_ms: int | None = None,
    ) -> CandidateQueueUpdate:
        normalized = self._normalize_candidate(candidate, now_ms=now_ms)
        effective_now_ms = normalized.updated_at_ms or normalized.created_at_ms
        expired = tuple(self._purge_expired(effective_now_ms))

        if normalized.is_expired(effective_now_ms):
            return CandidateQueueUpdate(
                action="rejected_expired",
                candidate=normalized,
                expired=expired,
                reason="candidate_expired",
            )

        existing = self._candidates.get(normalized.merge_key)
        if existing is not None:
            merged = self._merge(existing, normalized, effective_now_ms)
            self._candidates[merged.merge_key] = merged
            return CandidateQueueUpdate(action="merged", candidate=merged, expired=expired)

        conflicts = self._conflicts_for(normalized)
        if conflicts:
            strongest_conflict = max(conflicts, key=self._strength_key)
            if self._strength_key(normalized) <= self._strength_key(strongest_conflict):
                return CandidateQueueUpdate(
                    action="rejected_conflict",
                    candidate=normalized,
                    replaced=tuple(conflicts),
                    expired=expired,
                    reason="stronger_conflict_exists",
                )
            for conflict in conflicts:
                self._drop_candidate(conflict)
            self._candidates[normalized.merge_key] = normalized
            return CandidateQueueUpdate(
                action="replaced_conflict",
                candidate=normalized,
                replaced=tuple(conflicts),
                expired=expired,
            )

        self._candidates[normalized.merge_key] = normalized
        self._trim_to_max_candidates()
        return CandidateQueueUpdate(action="queued", candidate=normalized, expired=expired)

    def add_signal(self, signal: Mapping[str, Any], *, at_ms: int = 0) -> CandidateQueueSignal:
        data = dict(signal or {})
        extra = dict(data.get("extra") or {})
        symbol = normalize_symbol(data.get("symbol", ""))
        direction = normalize_position_side(data.get("direction", data.get("side", "")))
        if not symbol or direction == "unknown":
            self._blocked_count += 1
            return CandidateQueueSignal(
                key=f"{symbol}:{direction}",
                symbol=symbol,
                direction=direction,
                status="blocked",
                signal_id=str(data.get("signal_id", "")),
                updated_at_ms=int(at_ms),
                extra=extra,
            )

        setup = str(extra.get("setup", data.get("setup", "")) or "").strip()
        score = float(extra.get("quality_score", data.get("quality_score", data.get("score", 0))) or 0.0)
        key = self._signal_key(symbol, direction)
        expires_at_ms = self._expires_at_for_setup(setup, int(at_ms))
        candidate = OrderFlowCandidate(
            symbol=symbol,
            side=direction,
            score=score,
            created_at_ms=int(at_ms),
            candidate_id=str(data.get("signal_id", "")) or key,
            priority=int(score),
            expires_at_ms=expires_at_ms,
            updated_at_ms=int(at_ms),
            reason=setup,
            payload={"setup": setup, "signal": data},
        )

        self.expire(at_ms=int(at_ms))
        conflict = self._legacy_conflict_for(symbol, direction)
        if conflict is not None:
            self._blocked_count += 1
            return CandidateQueueSignal(
                key=key,
                symbol=symbol,
                direction=direction,
                status="blocked",
                signal_id=str(data.get("signal_id", "")),
                confluence=(setup,) if setup else (),
                score=score,
                created_at_ms=int(at_ms),
                updated_at_ms=int(at_ms),
                expires_at_ms=expires_at_ms,
                extra=extra,
            )

        existing_signal = self._signals.get(key)
        if existing_signal is not None:
            confluence = tuple(dict.fromkeys((*existing_signal.confluence, setup) if setup else existing_signal.confluence))
            merged_signal = replace(
                existing_signal,
                signal_id=str(data.get("signal_id", existing_signal.signal_id) or existing_signal.signal_id),
                confluence=confluence,
                score=max(existing_signal.score, score),
                updated_at_ms=int(at_ms),
                expires_at_ms=max(existing_signal.expires_at_ms or 0, expires_at_ms or 0) or None,
                extra={**dict(existing_signal.extra), **extra},
            )
            existing_candidate = self._candidates.get(candidate.merge_key)
            self._candidates[candidate.merge_key] = (
                self._merge(existing_candidate, candidate, int(at_ms)) if existing_candidate else candidate
            )
            self._signals[key] = merged_signal
            return merged_signal

        signal_item = CandidateQueueSignal(
            key=key,
            symbol=symbol,
            direction=direction,
            status="active",
            signal_id=str(data.get("signal_id", "")),
            confluence=(setup,) if setup else (),
            score=score,
            created_at_ms=int(at_ms),
            updated_at_ms=int(at_ms),
            expires_at_ms=expires_at_ms,
            extra=extra,
        )
        self._candidates[candidate.merge_key] = candidate
        self._signals[key] = signal_item
        self._trim_to_max_candidates()
        return signal_item

    def ranked(self, *, now_ms: int | None = None, limit: int | None = None) -> list[OrderFlowCandidate]:
        if now_ms is not None:
            self._purge_expired(now_ms)
        candidates = sorted(self._candidates.values(), key=self._sort_key)
        if limit is None:
            return candidates
        return candidates[: max(0, int(limit))]

    def pop_next(self, *, now_ms: int | None = None) -> OrderFlowCandidate | None:
        ranked = self.ranked(now_ms=now_ms, limit=1)
        if not ranked:
            return None
        candidate = ranked[0]
        self._drop_candidate(candidate)
        return candidate

    def remove(
        self,
        *,
        symbol: str,
        side: str,
        strategy: str = DEFAULT_CANDIDATE_STRATEGY,
    ) -> OrderFlowCandidate | None:
        key = (normalize_symbol(symbol), normalize_position_side(side), str(strategy or DEFAULT_CANDIDATE_STRATEGY))
        candidate = self._candidates.get(key)
        if candidate is None:
            return None
        self._drop_candidate(candidate)
        return candidate

    def purge_expired(self, now_ms: int) -> list[OrderFlowCandidate]:
        return self._purge_expired(now_ms)

    def expire(self, at_ms: int) -> list[CandidateQueueSignal]:
        expired_candidates = [
            candidate for candidate in self._candidates.values() if candidate.is_expired(int(at_ms))
        ]
        expired_signals: list[CandidateQueueSignal] = []
        for candidate in expired_candidates:
            expired_signal = self._signals.get(self._signal_key(candidate.symbol, candidate.side))
            if expired_signal is not None:
                expired_signals.append(expired_signal)
            self._drop_candidate(candidate)
        return expired_signals

    def status(self) -> dict[str, Any]:
        return {
            "active_count": len(self._candidates),
            "blocked_count": self._blocked_count,
            "max_candidates": self.max_candidates,
            "candidates": [candidate.candidate_id for candidate in self.ranked()],
        }

    def clear(self) -> None:
        self._candidates.clear()
        self._signals.clear()
        self._blocked_count = 0

    def __len__(self) -> int:
        return len(self._candidates)

    def _normalize_candidate(
        self,
        candidate: OrderFlowCandidate | Mapping[str, Any],
        *,
        now_ms: int | None,
    ) -> OrderFlowCandidate:
        if isinstance(candidate, Mapping):
            candidate = OrderFlowCandidate(**dict(candidate))
        if not isinstance(candidate, OrderFlowCandidate):
            raise TypeError("candidate must be an OrderFlowCandidate or mapping")

        effective_now_ms = int(now_ms if now_ms is not None else (candidate.updated_at_ms or candidate.created_at_ms))
        created_at_ms = int(candidate.created_at_ms or effective_now_ms)
        updated_at_ms = effective_now_ms
        ttl_ms = candidate.ttl_ms if candidate.ttl_ms is not None else self.default_ttl_ms
        expires_at_ms = candidate.expires_at_ms
        if expires_at_ms is None and ttl_ms > 0:
            expires_at_ms = created_at_ms + ttl_ms

        return replace(
            candidate,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            ttl_ms=ttl_ms,
            expires_at_ms=expires_at_ms,
        )

    @staticmethod
    def _merge(
        existing: OrderFlowCandidate,
        incoming: OrderFlowCandidate,
        now_ms: int,
    ) -> OrderFlowCandidate:
        payload = dict(existing.payload)
        payload.update(dict(incoming.payload))
        return replace(
            existing,
            score=max(existing.score, incoming.score),
            priority=max(existing.priority, incoming.priority),
            ttl_ms=max(existing.ttl_ms or 0, incoming.ttl_ms or 0) or None,
            expires_at_ms=max(
                existing.expires_at_ms or 0,
                incoming.expires_at_ms or 0,
            )
            or None,
            updated_at_ms=max(now_ms, incoming.updated_at_ms or 0, existing.updated_at_ms or 0),
            reason=incoming.reason or existing.reason,
            payload=payload,
        )

    def _conflicts_for(self, candidate: OrderFlowCandidate) -> list[OrderFlowCandidate]:
        return [
            existing
            for existing in self._candidates.values()
            if existing.symbol == candidate.symbol and existing.side != candidate.side
        ]

    @staticmethod
    def _strength_key(candidate: OrderFlowCandidate) -> tuple[float, int, int, int]:
        return (
            candidate.score,
            candidate.priority,
            candidate.updated_at_ms or 0,
            -(candidate.created_at_ms or 0),
        )

    @staticmethod
    def _sort_key(candidate: OrderFlowCandidate) -> tuple[float, int, int, int, str, str, str]:
        return (
            -candidate.score,
            -candidate.priority,
            -(candidate.updated_at_ms or 0),
            candidate.created_at_ms or 0,
            candidate.symbol,
            candidate.side,
            candidate.candidate_id,
        )

    def _purge_expired(self, now_ms: int) -> list[OrderFlowCandidate]:
        expired = [
            candidate for candidate in self._candidates.values() if candidate.is_expired(int(now_ms))
        ]
        for candidate in expired:
            self._drop_candidate(candidate)
        return expired

    def _drop_candidate(self, candidate: OrderFlowCandidate) -> None:
        self._candidates.pop(candidate.merge_key, None)
        self._signals.pop(self._signal_key(candidate.symbol, candidate.side), None)

    def _legacy_conflict_for(self, symbol: str, direction: str) -> CandidateQueueSignal | None:
        for signal in self._signals.values():
            if signal.symbol == symbol and signal.direction != direction and signal.status == "active":
                return signal
        return None

    def _trim_to_max_candidates(self) -> None:
        if self.max_candidates is None or self.max_candidates <= 0:
            return
        while len(self._candidates) > self.max_candidates:
            worst = sorted(self._candidates.values(), key=self._sort_key)[-1]
            self._drop_candidate(worst)

    def _expires_at_for_setup(self, setup: str, at_ms: int) -> int | None:
        ttl_ms = self.default_ttl_ms
        for setup_key, ttl_value_raw in self.ttl_by_setup.items():
            if setup == setup_key or (setup_key and setup_key in setup):
                ttl_value = float(ttl_value_raw)
                ttl_ms = int(ttl_value if ttl_value >= 1000 else ttl_value * 1000)
                break
        return int(at_ms) + ttl_ms if ttl_ms > 0 else None

    @staticmethod
    def _signal_key(symbol: str, direction: str) -> str:
        return f"{normalize_symbol(symbol)}:{normalize_position_side(direction)}"
