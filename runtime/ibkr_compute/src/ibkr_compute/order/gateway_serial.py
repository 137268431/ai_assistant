from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


_GLOBAL_ORDER_MUTATION_LOCK = threading.RLock()
DEFAULT_GATEWAY_ORDER_SERIAL_TIMEOUT_SECONDS = 900.0


class GatewayOrderMutationTimeout(RuntimeError):
    def __init__(self, operation: str, timeout_s: float):
        self.operation = str(operation or "gateway_order_mutation")
        self.timeout_s = float(timeout_s or 0.0)
        super().__init__(f"gateway_order_queue_timeout:{self.operation}:{self.timeout_s:g}s")


class GatewayOrderMutationGate:
    """Shared process-level gate for IB Gateway order writes."""

    def __init__(
        self,
        *,
        config: Any = None,
        environment: str = "live",
        lock: threading.RLock | None = None,
    ):
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self._lock = lock or _GLOBAL_ORDER_MUTATION_LOCK
        self._owner_operation = ""
        self._owner_since = 0.0

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

    def _config_float(self, key: str, default: float) -> float:
        getter = getattr(self.config, "get_float_for_environment", None)
        if callable(getter):
            try:
                return float(getter(key, self.environment, default))
            except Exception:
                return float(default)
        getter = getattr(self.config, "get_float", None)
        if callable(getter):
            try:
                return float(getter(key, default))
            except Exception:
                return float(default)
        return float(default)

    def enabled(self) -> bool:
        return self._config_bool("ibkr_gateway_order_serial_enabled", True)

    def timeout_seconds(self) -> float:
        return max(
            0.1,
            self._config_float(
                "ibkr_gateway_order_serial_timeout_sec",
                DEFAULT_GATEWAY_ORDER_SERIAL_TIMEOUT_SECONDS,
            ),
        )

    @contextmanager
    def hold(self, operation: str, **metadata: Any) -> Iterator[dict[str, Any]]:
        operation_name = str(operation or "gateway_order_mutation").strip() or "gateway_order_mutation"
        if not self.enabled():
            yield {"serialized": False, "operation": operation_name, "metadata": dict(metadata or {})}
            return

        timeout_s = self.timeout_seconds()
        started = time.perf_counter()
        acquired = self._lock.acquire(timeout=timeout_s)
        wait_s = time.perf_counter() - started
        if not acquired:
            raise GatewayOrderMutationTimeout(operation_name, timeout_s)
        self._owner_operation = operation_name
        self._owner_since = time.time()
        try:
            yield {
                "serialized": True,
                "operation": operation_name,
                "metadata": dict(metadata or {}),
                "queue_wait_s": wait_s,
            }
        finally:
            self._owner_operation = ""
            self._owner_since = 0.0
            self._lock.release()

    def status(self) -> dict[str, Any]:
        owner_since = float(self._owner_since or 0.0)
        return {
            "enabled": self.enabled(),
            "timeout_s": self.timeout_seconds(),
            "owner_operation": self._owner_operation,
            "owner_age_s": max(0.0, time.time() - owner_since) if owner_since else 0.0,
        }


__all__ = [
    "DEFAULT_GATEWAY_ORDER_SERIAL_TIMEOUT_SECONDS",
    "GatewayOrderMutationGate",
    "GatewayOrderMutationTimeout",
]
