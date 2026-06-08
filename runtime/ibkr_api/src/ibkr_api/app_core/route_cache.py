from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any, Callable


RouteBuilder = Callable[[], tuple[dict[str, Any], int]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _truthy(value: Any) -> bool:
    text = _to_text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    try:
        return float(text) != 0.0
    except (TypeError, ValueError):
        return False


def _disabled(value: Any) -> bool:
    return _to_text(value).lower() in {"0", "false", "no", "off"}


def cache_seconds(env_name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, fallback)))
    except (TypeError, ValueError):
        return fallback


def request_cache_bypass(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    return _truthy(data.get("cache_bust")) or _disabled(data.get("cache"))


def canonical_cache_key(namespace: str, payload: dict[str, Any] | None, *, extra: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    data = payload if isinstance(payload, dict) else {}
    filtered = {
        str(key): value
        for key, value in data.items()
        if str(key) not in {"cache_bust", "cache", "_"}
    }
    return (
        namespace,
        tuple(sorted((key, _normalize_cache_value(value)) for key, value in filtered.items())),
        tuple(extra),
    )


def _normalize_cache_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _normalize_cache_value(item)) for key, item in value.items()))
    if isinstance(value, set):
        return tuple(sorted(_normalize_cache_value(item) for item in value))
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_cache_value(item) for item in value)
    return value


class RouteSWRCache:
    def __init__(self, name: str, *, enabled_env: str = "IBKR_ROUTE_CACHE_ENABLED") -> None:
        self.name = name
        self.enabled_env = enabled_env
        self._lock = threading.RLock()
        self._entries: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._in_flight: dict[tuple[Any, ...], threading.Event] = {}

    def enabled(self) -> bool:
        return not _disabled(os.environ.get(self.enabled_env, "true"))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._in_flight.clear()

    def clear_matching(self, predicate: Callable[[tuple[Any, ...]], bool]) -> None:
        with self._lock:
            for key in list(self._entries):
                if predicate(key):
                    self._entries.pop(key, None)
            for key in list(self._in_flight):
                if predicate(key):
                    self._in_flight.pop(key, None)

    def peek(self, key: tuple[Any, ...], *, allow_stale: bool = True) -> tuple[dict[str, Any], int] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
        if not entry:
            return None
        if allow_stale:
            if now > float(entry.get("stale_until") or 0.0):
                return None
            state = "peek_stale" if now > float(entry.get("expires_at") or 0.0) else "peek_hit"
            return self._with_meta(
                entry["payload"],
                int(entry["status_code"]),
                entry=entry,
                state=state,
                stale=state == "peek_stale",
            )
        if now > float(entry.get("expires_at") or 0.0):
            return None
        return self._with_meta(entry["payload"], int(entry["status_code"]), entry=entry, state="peek_hit")

    def get(
        self,
        key: tuple[Any, ...],
        *,
        builder: RouteBuilder,
        ttl_seconds: float,
        stale_seconds: float,
        force: bool = False,
        prefer_stale_on_force: bool = False,
    ) -> tuple[dict[str, Any], int]:
        ttl = max(0.0, float(ttl_seconds or 0.0))
        stale = max(0.0, float(stale_seconds or 0.0))
        if force or not self.enabled() or ttl <= 0:
            stale_entry = None
            if force and self.enabled() and ttl > 0:
                with self._lock:
                    candidate = self._entries.get(key)
                    event = self._in_flight.get(key)
                if candidate and time.monotonic() <= float(candidate.get("stale_until") or 0.0):
                    stale_entry = candidate
                    if prefer_stale_on_force:
                        if event is None:
                            event = threading.Event()
                            with self._lock:
                                if key not in self._in_flight:
                                    self._in_flight[key] = event
                                    threading.Thread(
                                        target=self._refresh,
                                        args=(key, event),
                                        kwargs={
                                            "builder": builder,
                                            "ttl_seconds": ttl,
                                            "stale_seconds": stale,
                                        },
                                        daemon=True,
                                    ).start()
                                else:
                                    event = self._in_flight[key]
                        return self._with_meta(
                            stale_entry["payload"],
                            int(stale_entry["status_code"]),
                            entry=stale_entry,
                            state="bypass_stale_refresh",
                            stale=time.monotonic() > float(stale_entry.get("expires_at") or 0.0),
                        )
            try:
                payload, status_code = builder()
            except Exception as exc:
                if stale_entry is not None:
                    return self._with_meta(
                        stale_entry["payload"],
                        int(stale_entry["status_code"]),
                        entry=stale_entry,
                        state="bypass_stale_error",
                        stale=True,
                        error=exc,
                    )
                raise
            entry = self._entry(payload, status_code, ttl, stale)
            if force and self.enabled() and status_code < 400 and ttl > 0:
                with self._lock:
                    self._entries[key] = entry
            elif force and self.enabled() and status_code >= 400 and stale_entry is not None:
                error = (payload or {}).get("error") if isinstance(payload, dict) else None
                return self._with_meta(
                    stale_entry["payload"],
                    int(stale_entry["status_code"]),
                    entry=stale_entry,
                    state="bypass_stale_error",
                    stale=True,
                    error=error or f"upstream_status_{status_code}",
                )
            return self._with_meta(payload, status_code, entry=entry, state="bypass")

        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and now <= float(entry.get("expires_at") or 0.0):
                return self._with_meta(entry["payload"], int(entry["status_code"]), entry=entry, state="hit")
            if entry and now <= float(entry.get("stale_until") or 0.0):
                event = self._in_flight.get(key)
                if event is None:
                    event = threading.Event()
                    self._in_flight[key] = event
                    threading.Thread(
                        target=self._refresh,
                        args=(key, event),
                        kwargs={
                            "builder": builder,
                            "ttl_seconds": ttl,
                            "stale_seconds": stale,
                        },
                        daemon=True,
                    ).start()
                return self._with_meta(
                    entry["payload"],
                    int(entry["status_code"]),
                    entry=entry,
                    state="stale",
                    stale=True,
                )
            event = self._in_flight.get(key)

        if event is not None:
            event.wait(timeout=max(0.25, min(2.0, ttl)))
            with self._lock:
                entry = self._entries.get(key)
            if entry and time.monotonic() <= float(entry.get("stale_until") or 0.0):
                return self._with_meta(
                    entry["payload"],
                    int(entry["status_code"]),
                    entry=entry,
                    state="wait_hit",
                    stale=time.monotonic() > float(entry.get("expires_at") or 0.0),
                )

        created_event = threading.Event()
        with self._lock:
            event = self._in_flight.get(key)
            if event is None:
                self._in_flight[key] = created_event
                event = created_event

        if event is not created_event:
            event.wait(timeout=max(0.25, min(2.0, ttl)))
            with self._lock:
                entry = self._entries.get(key)
            if entry:
                return self._with_meta(entry["payload"], int(entry["status_code"]), entry=entry, state="wait_hit")

        try:
            payload, status_code = builder()
            entry = self._entry(payload, status_code, ttl, stale)
            if status_code < 400:
                with self._lock:
                    self._entries[key] = entry
            return self._with_meta(payload, status_code, entry=entry, state="miss")
        except Exception as exc:
            with self._lock:
                entry = self._entries.get(key)
            if entry and time.monotonic() <= float(entry.get("stale_until") or 0.0):
                return self._with_meta(
                    entry["payload"],
                    int(entry["status_code"]),
                    entry=entry,
                    state="stale_error",
                    stale=True,
                    error=exc,
                )
            raise
        finally:
            with self._lock:
                current = self._in_flight.get(key)
                if current is created_event:
                    self._in_flight.pop(key, None)
                    created_event.set()

    def _refresh(
        self,
        key: tuple[Any, ...],
        event: threading.Event,
        *,
        builder: RouteBuilder,
        ttl_seconds: float,
        stale_seconds: float,
    ) -> None:
        try:
            payload, status_code = builder()
            if status_code < 400:
                entry = self._entry(payload, status_code, ttl_seconds, stale_seconds)
                with self._lock:
                    self._entries[key] = entry
        except Exception:
            pass
        finally:
            with self._lock:
                current = self._in_flight.get(key)
                if current is event:
                    self._in_flight.pop(key, None)
                event.set()

    def _entry(
        self,
        payload: dict[str, Any],
        status_code: int,
        ttl_seconds: float,
        stale_seconds: float,
    ) -> dict[str, Any]:
        now = time.monotonic()
        entry_ttl = float(ttl_seconds or 0.0)
        entry_stale = float(stale_seconds or 0.0)
        snapshot_meta = payload.get("_snapshot_cache") if isinstance(payload, dict) else None
        if isinstance(snapshot_meta, dict) and bool(snapshot_meta.get("stale")):
            entry_ttl = min(entry_ttl, cache_seconds("IBKR_ROUTE_CACHE_STALE_SNAPSHOT_TTL_SEC", 1.0))
            entry_stale = min(entry_stale, cache_seconds("IBKR_ROUTE_CACHE_STALE_SNAPSHOT_STALE_SEC", 5.0))
        return {
            "payload": copy.deepcopy(payload if isinstance(payload, dict) else {}),
            "status_code": int(status_code or 200),
            "created_at": now,
            "expires_at": now + entry_ttl,
            "stale_until": now + entry_ttl + entry_stale,
            "ttl_seconds": entry_ttl,
        }

    def _with_meta(
        self,
        payload: dict[str, Any],
        status_code: int,
        *,
        entry: dict[str, Any],
        state: str,
        stale: bool = False,
        error: Any = None,
    ) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return payload, status_code
        now = time.monotonic()
        created_at = float(entry.get("created_at") or now)
        meta = {
            "name": self.name,
            "state": state,
            "age_s": round(max(0.0, now - created_at), 3),
            "ttl_s": round(float(entry.get("ttl_seconds") or 0.0), 3),
            "stale": bool(stale),
        }
        if error is not None:
            meta["error"] = str(error)
        return {**copy.deepcopy(payload), "_cache": meta}, status_code
