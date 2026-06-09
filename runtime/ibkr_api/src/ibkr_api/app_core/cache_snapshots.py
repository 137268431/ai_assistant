from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

SnapshotBuilder = Callable[[], tuple[dict[str, Any], int]]

COLLECTION = "ibkr_cache_snapshots"
_SKIP_KEY_FIELDS = {"_", "cache", "cache_bust", "broker_force", "force", "refresh"}
_REFRESH_LOCK = threading.RLock()
_REFRESH_IN_FLIGHT: set[str] = set()


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _to_text(value).lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    try:
        return float(text) != 0.0
    except (TypeError, ValueError):
        return False


def _disabled(value: Any) -> bool:
    return _to_text(value).lower() in {"0", "false", "no", "off"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def snapshots_enabled() -> bool:
    return not _disabled(os.environ.get("IBKR_PB_SNAPSHOT_CACHE_ENABLED", "true"))


def now_ms() -> int:
    return int(time.time() * 1000)


def escape_filter(value: Any) -> str:
    return _to_text(value).replace("\\", "\\\\").replace('"', '\\"')


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, set):
        return [_stable_value(item) for item in sorted(value, key=lambda item: str(item))]
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(_stable_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def build_snapshot_cache_key(
    scope: str,
    payload: dict[str, Any] | None,
    *,
    include_keys: list[str] | tuple[str, ...] | None = None,
    extra: tuple[Any, ...] | list[Any] | None = None,
) -> str:
    data = payload if isinstance(payload, dict) else {}
    if include_keys is None:
        filtered = {str(key): value for key, value in data.items() if str(key) not in _SKIP_KEY_FIELDS}
    else:
        wanted = {str(key) for key in include_keys}
        filtered = {str(key): value for key, value in data.items() if str(key) in wanted}
    body = {"scope": _to_text(scope), "payload": filtered, "extra": list(extra or [])}
    digest = hashlib.sha256(_stable_json(body).encode("utf-8")).hexdigest()[:24]
    safe_scope = "".join(ch if ch.isalnum() or ch in {".", "_", "-"} else "_" for ch in _to_text(scope))[:80]
    return f"{safe_scope}:{digest}"


def _copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return copy.deepcopy(payload)
    except Exception:
        return dict(payload)


def _normalize_record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return _copy_payload(payload)


def _cache_meta(record: dict[str, Any], *, state: str, now: int | None = None, error: Any = None) -> dict[str, Any]:
    current_ms = int(now if now is not None else now_ms())
    computed_at = int(float(record.get("computed_at_ms") or 0))
    fresh_until = int(float(record.get("fresh_until_ms") or 0))
    stale_until = int(float(record.get("stale_until_ms") or 0))
    age_s = round(max(0, current_ms - computed_at) / 1000.0, 3) if computed_at > 0 else 0.0
    meta: dict[str, Any] = {
        "name": "pb_snapshot",
        "state": state,
        "age_s": age_s,
        "stale": current_ms > fresh_until,
        "computed_at_ms": computed_at,
        "fresh_until_ms": fresh_until,
        "stale_until_ms": stale_until,
        "cache_key": _to_text(record.get("cache_key")),
        "scope": _to_text(record.get("scope")),
    }
    record_error = error if error is not None else record.get("error")
    if record_error:
        meta["error"] = _to_text(record_error)
    return meta


def with_snapshot_cache_meta(payload: dict[str, Any], record: dict[str, Any], *, state: str, error: Any = None) -> dict[str, Any]:
    result = _copy_payload(payload if isinstance(payload, dict) else {})
    result["_snapshot_cache"] = _cache_meta(record, state=state, error=error)
    return result


def get_cached_snapshot(pb: Any, cache_key: str) -> dict[str, Any] | None:
    if not snapshots_enabled() or not _to_text(cache_key):
        return None
    get_records = getattr(pb, "get_records", None)
    if not callable(get_records):
        return None
    try:
        rows = get_records(
            COLLECTION,
            filter=f'cache_key = "{escape_filter(cache_key)}"',
            per_page=1,
            page=1,
        ) or []
    except Exception:
        return None
    for row in rows:
        if isinstance(row, dict):
            return row
    return None


def _needs_invalidation(record: dict[str, Any], current_ms: int) -> bool:
    if _to_text(record.get("error")).lower() == "invalidated":
        return False
    if _to_text(record.get("status")).lower() in {"stale", "expired"}:
        return False
    try:
        fresh_until = int(float(record.get("fresh_until_ms") or 0))
    except Exception:
        fresh_until = 0
    return fresh_until > current_ms


def _delete_or_expire_record(pb: Any, record: dict[str, Any]) -> bool:
    record_id = _to_text(record.get("id"))
    if not record_id:
        return False
    current_ms = now_ms()
    if not _needs_invalidation(record, current_ms):
        return False
    grace_ms = max(0, int(_float_env("IBKR_PB_SNAPSHOT_INVALIDATION_STALE_SEC", 30.0) * 1000))
    update_record = getattr(pb, "update_record", None)
    if callable(update_record):
        try:
            data = {
                "cache_key": _to_text(record.get("cache_key")),
                "scope": _to_text(record.get("scope")),
                "environment": _to_text(record.get("environment")) or "global",
                "market_date": _to_text(record.get("market_date")) or "global",
                "payload": record.get("payload") if isinstance(record.get("payload"), dict) else {},
                "computed_at_ms": int(float(record.get("computed_at_ms") or 0)),
                "fresh_until_ms": 1,
                "stale_until_ms": current_ms + grace_ms if grace_ms > 0 else 1,
                "status": "stale",
                "error": "invalidated",
            }
            update_record(
                COLLECTION,
                record_id,
                data,
            )
            return True
        except Exception:
            pass
    delete_record = getattr(pb, "delete_record", None)
    if callable(delete_record):
        try:
            delete_record(COLLECTION, record_id)
            return True
        except Exception:
            pass
    return False


def clear_cached_snapshots(
    pb: Any,
    *,
    scopes: list[str] | tuple[str, ...] | set[str] | None = None,
    cache_keys: list[str] | tuple[str, ...] | set[str] | None = None,
) -> int:
    """Best-effort invalidation for UI snapshots after write operations."""
    if not snapshots_enabled() or pb is None:
        return 0
    get_records = getattr(pb, "get_records", None)
    if not callable(get_records):
        return 0
    invalidated = 0
    seen_ids: set[str] = set()
    filters: list[str] = []
    for cache_key in cache_keys or ():
        if _to_text(cache_key):
            filters.append(f'cache_key = "{escape_filter(cache_key)}"')
    for scope in scopes or ():
        if _to_text(scope):
            filters.append(f'scope = "{escape_filter(scope)}"')
    for filter_text in filters:
        page = 1
        while True:
            try:
                rows = get_records(COLLECTION, filter=filter_text, per_page=200, page=page) or []
            except Exception:
                break
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record_id = _to_text(row.get("id"))
                if record_id and record_id in seen_ids:
                    continue
                if record_id:
                    seen_ids.add(record_id)
                if _delete_or_expire_record(pb, row):
                    invalidated += 1
            if len(rows) < 200:
                break
            page += 1
    return invalidated


def upsert_cached_snapshot(
    pb: Any,
    cache_key: str,
    scope: str,
    environment: str,
    market_date: str,
    payload: dict[str, Any],
    ttl_seconds: float,
    stale_seconds: float,
    *,
    now: int | None = None,
    error: Any = None,
) -> dict[str, Any] | None:
    if not snapshots_enabled() or not _to_text(cache_key):
        return None
    current_ms = int(now if now is not None else now_ms())
    ttl_ms = max(0, int(float(ttl_seconds or 0) * 1000))
    stale_ms = max(0, int(float(stale_seconds or 0) * 1000))
    status = "fresh" if error in (None, "") else "error"
    data = {
        "cache_key": _to_text(cache_key),
        "scope": _to_text(scope),
        "environment": _to_text(environment) or "global",
        "market_date": _to_text(market_date) or "global",
        "payload": payload if isinstance(payload, dict) else {},
        "computed_at_ms": current_ms,
        "fresh_until_ms": current_ms + ttl_ms,
        "stale_until_ms": current_ms + ttl_ms + stale_ms,
        "status": status,
        "error": _to_text(error),
    }
    existing = get_cached_snapshot(pb, cache_key)
    try:
        if existing and existing.get("id") and callable(getattr(pb, "update_record", None)):
            return pb.update_record(COLLECTION, existing["id"], data)
        if callable(getattr(pb, "create_record", None)):
            return pb.create_record(COLLECTION, data)
    except Exception:
        return None
    return None


def _record_state(record: dict[str, Any], current_ms: int) -> str:
    fresh_until = int(float(record.get("fresh_until_ms") or 0))
    stale_until = int(float(record.get("stale_until_ms") or 0))
    if current_ms <= fresh_until:
        return "hit"
    if current_ms <= stale_until:
        return "stale"
    return "expired"


def _refresh_key_async(
    *,
    pb: Any,
    cache_key: str,
    scope: str,
    environment: str,
    market_date: str,
    builder: SnapshotBuilder,
    ttl_seconds: float,
    stale_seconds: float,
) -> None:
    try:
        payload, status_code = builder()
        if status_code < 400 and isinstance(payload, dict):
            upsert_cached_snapshot(pb, cache_key, scope, environment, market_date, payload, ttl_seconds, stale_seconds)
    except Exception:
        return
    finally:
        with _REFRESH_LOCK:
            _REFRESH_IN_FLIGHT.discard(cache_key)


def _start_background_refresh(**kwargs: Any) -> None:
    cache_key = _to_text(kwargs.get("cache_key"))
    if not cache_key:
        return
    with _REFRESH_LOCK:
        if cache_key in _REFRESH_IN_FLIGHT:
            return
        _REFRESH_IN_FLIGHT.add(cache_key)
    threading.Thread(target=_refresh_key_async, kwargs=kwargs, daemon=True).start()


def cached_snapshot_response(
    pb: Any,
    *,
    scope: str,
    cache_key: str,
    builder: SnapshotBuilder,
    ttl_seconds: float,
    stale_seconds: float,
    environment: str = "global",
    market_date: str = "global",
    force: bool = False,
    background_refresh: bool = True,
) -> tuple[dict[str, Any], int]:
    if force or not snapshots_enabled():
        payload, status_code = builder()
        if status_code < 400 and isinstance(payload, dict):
            upsert_cached_snapshot(pb, cache_key, scope, environment, market_date, payload, ttl_seconds, stale_seconds)
        return payload, status_code

    record = get_cached_snapshot(pb, cache_key)
    current_ms = now_ms()
    if record:
        state = _record_state(record, current_ms)
        payload = _normalize_record_payload(record)
        if state == "hit":
            return with_snapshot_cache_meta(payload, record, state="hit"), 200
        if state == "stale":
            if background_refresh:
                _start_background_refresh(
                    pb=pb,
                    cache_key=cache_key,
                    scope=scope,
                    environment=environment,
                    market_date=market_date,
                    builder=builder,
                    ttl_seconds=ttl_seconds,
                    stale_seconds=stale_seconds,
                )
            return with_snapshot_cache_meta(payload, record, state="stale"), 200

    try:
        payload, status_code = builder()
    except Exception as exc:
        if record:
            payload = _normalize_record_payload(record)
            return with_snapshot_cache_meta(payload, record, state="stale_error", error=exc), 200
        raise

    if status_code < 400 and isinstance(payload, dict):
        stored = upsert_cached_snapshot(pb, cache_key, scope, environment, market_date, payload, ttl_seconds, stale_seconds)
        if stored:
            payload = with_snapshot_cache_meta(payload, stored, state="miss")
    elif record:
        cached_payload = _normalize_record_payload(record)
        return with_snapshot_cache_meta(cached_payload, record, state="stale_error", error=f"upstream_status_{status_code}"), 200
    return payload, status_code


def request_force_refresh(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    return (
        _truthy(data.get("cache_bust"))
        or _truthy(data.get("refresh"))
        or _truthy(data.get("broker_force"))
        or _disabled(data.get("cache"))
    )


__all__ = [
    "COLLECTION",
    "build_snapshot_cache_key",
    "cached_snapshot_response",
    "clear_cached_snapshots",
    "get_cached_snapshot",
    "request_force_refresh",
    "upsert_cached_snapshot",
    "with_snapshot_cache_meta",
]
