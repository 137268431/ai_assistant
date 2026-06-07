from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ibkr_compute.observability.prometheus import record_symbol_order_queue_event


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


@dataclass(order=True)
class _QueuedCommand:
    sort_key: tuple[int, int] = field(init=False, repr=False)
    priority: int
    sequence: int
    symbol: str = field(compare=False)
    operation: str = field(compare=False)
    fn: Callable[[], Any] = field(compare=False)
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)
    enqueued_at: float = field(default_factory=time.perf_counter, compare=False)
    started_at: float = field(default=0.0, compare=False)
    finished_at: float = field(default=0.0, compare=False)
    active_symbols_at_start: int = field(default=0, compare=False)
    result: Any = field(default=None, compare=False)
    exception: BaseException | None = field(default=None, compare=False)
    done: threading.Event = field(default_factory=threading.Event, compare=False)

    def __post_init__(self) -> None:
        self.sort_key = (int(self.priority), int(self.sequence))


class SymbolOrderCommandScheduler:
    """Synchronous per-symbol queue with bounded cross-symbol concurrency."""

    def __init__(self, *, config: Any = None, environment: str = "live"):
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self._condition = threading.Condition(threading.RLock())
        self._queues: dict[str, list[_QueuedCommand]] = {}
        self._active_symbols: set[str] = set()
        self._sequence = itertools.count(1)
        self._local = threading.local()

    def _config_bool(self, key: str, default: bool) -> bool:
        def coerce(value: Any) -> bool:
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"true", "1", "yes", "y", "on"}:
                    return True
                if text in {"false", "0", "no", "n", "off"}:
                    return False
            return bool(value)

        getter = getattr(self.config, "get_bool_for_environment", None)
        if callable(getter):
            try:
                return coerce(getter(key, self.environment, default))
            except Exception:
                return bool(default)
        getter = getattr(self.config, "get_bool", None)
        if callable(getter):
            try:
                return coerce(getter(key, default))
            except Exception:
                return bool(default)
        return bool(default)

    def _config_int(self, key: str, default: int) -> int:
        getter = getattr(self.config, "get_int_for_environment", None)
        if callable(getter):
            try:
                return int(getter(key, self.environment, default))
            except Exception:
                return int(default)
        getter = getattr(self.config, "get_int", None)
        if callable(getter):
            try:
                return int(getter(key, default))
            except Exception:
                return int(default)
        return int(default)

    def enabled(self) -> bool:
        return self._config_bool("ibkr_order_symbol_queue_enabled", True)

    def max_active_symbols(self) -> int:
        return max(1, self._config_int("ibkr_order_symbol_queue_max_active_symbols", 4))

    def submit(
        self,
        *,
        symbol: Any,
        operation: str,
        fn: Callable[[], Any],
        priority: int = 50,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        normalized_symbol = _normalize_symbol(symbol)
        operation_name = str(operation or "order_command").strip() or "order_command"
        if not self.enabled() or not normalized_symbol or getattr(self._local, "inside_symbol_queue", False):
            return self._annotate_result(
                fn(),
                symbol=normalized_symbol,
                operation=operation_name,
                enabled=False,
                queue_wait_s=0.0,
                active_elapsed_s=0.0,
                active_symbols=0,
            )

        command = _QueuedCommand(
            priority=int(priority),
            sequence=next(self._sequence),
            symbol=normalized_symbol,
            operation=operation_name,
            fn=fn,
            metadata=dict(metadata or {}),
        )
        with self._condition:
            queue = self._queues.setdefault(normalized_symbol, [])
            queue.append(command)
            queue.sort()
            self._dispatch_locked()

        command.done.wait()
        if command.exception is not None:
            raise command.exception
        return command.result

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "enabled": self.enabled(),
                "max_active_symbols": self.max_active_symbols(),
                "active_symbols": sorted(self._active_symbols),
                "queued_symbols": {
                    symbol: len(queue)
                    for symbol, queue in self._queues.items()
                    if queue
                },
                "queued_total": sum(len(queue) for queue in self._queues.values()),
            }

    def _dispatch_locked(self) -> None:
        max_active = self.max_active_symbols()
        while len(self._active_symbols) < max_active:
            candidate_symbol = ""
            candidate: _QueuedCommand | None = None
            for symbol, queue in self._queues.items():
                if symbol in self._active_symbols or not queue:
                    continue
                head = queue[0]
                if candidate is None or head.sort_key < candidate.sort_key:
                    candidate_symbol = symbol
                    candidate = head
            if candidate is None:
                return
            self._queues[candidate_symbol].pop(0)
            if not self._queues[candidate_symbol]:
                self._queues.pop(candidate_symbol, None)
            self._active_symbols.add(candidate_symbol)
            candidate.started_at = time.perf_counter()
            candidate.active_symbols_at_start = len(self._active_symbols)
            thread = threading.Thread(
                target=self._run_command,
                args=(candidate,),
                name=f"symbol-order-{candidate_symbol[:16]}-{candidate.sequence}",
                daemon=True,
            )
            thread.start()

    def _run_command(self, command: _QueuedCommand) -> None:
        previous_inside = getattr(self._local, "inside_symbol_queue", False)
        self._local.inside_symbol_queue = True
        result_label = "error"
        try:
            raw = command.fn()
            command.finished_at = time.perf_counter()
            result_label = "ok" if not isinstance(raw, dict) or raw.get("ok") is not False else "error"
            command.result = self._annotate_result(
                raw,
                symbol=command.symbol,
                operation=command.operation,
                enabled=True,
                queue_wait_s=max(0.0, command.started_at - command.enqueued_at),
                active_elapsed_s=max(0.0, command.finished_at - command.started_at),
                active_symbols=command.active_symbols_at_start,
            )
        except BaseException as exc:  # pragma: no cover - surfaced to caller
            command.finished_at = time.perf_counter()
            command.exception = exc
        finally:
            record_symbol_order_queue_event(
                environment=self.environment,
                operation=command.operation,
                result=result_label,
                queue_wait_s=max(0.0, command.started_at - command.enqueued_at),
                active_elapsed_s=max(0.0, (command.finished_at or time.perf_counter()) - command.started_at),
                active_symbols=command.active_symbols_at_start,
            )
            self._local.inside_symbol_queue = previous_inside
            command.done.set()
            with self._condition:
                self._active_symbols.discard(command.symbol)
                self._dispatch_locked()
                self._condition.notify_all()

    @staticmethod
    def _annotate_result(
        result: Any,
        *,
        symbol: str,
        operation: str,
        enabled: bool,
        queue_wait_s: float,
        active_elapsed_s: float,
        active_symbols: int,
    ) -> Any:
        if not isinstance(result, dict):
            return result
        payload = dict(result)
        diagnostics = {
            "enabled": bool(enabled),
            "symbol": symbol,
            "operation": operation,
            "queue_wait_s": round(float(queue_wait_s or 0.0), 6),
            "active_elapsed_s": round(float(active_elapsed_s or 0.0), 6),
            "active_symbols": int(active_symbols or 0),
        }
        payload.setdefault("symbol_queue", diagnostics)
        payload.setdefault("symbol_queue_wait_s", diagnostics["queue_wait_s"])
        payload.setdefault("symbol_queue_active_symbols", diagnostics["active_symbols"])
        return payload


__all__ = ["SymbolOrderCommandScheduler"]
