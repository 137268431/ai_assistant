from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode

try:
    from ibkr_compute.market.bar_coverage_daily import is_nyse_trading_day as _is_nyse_trading_day
except Exception:  # pragma: no cover - fail open if the compute calendar is unavailable.
    _is_nyse_trading_day = None

from ibkr_compute.market.calendar import (
    DEFAULT_CALENDAR_EXCHANGE,
    DEFAULT_CALENDAR_SEC_TYPE,
    DEFAULT_CALENDAR_SYMBOL,
    IBKR_SCHEDULE_SOURCE,
    LOCAL_NYSE_FALLBACK_SOURCE,
    build_local_nyse_calendar_snapshot,
)

RequestJsonRequest = Callable[..., dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]

_MARKET_CALENDAR_CACHE_LOCK = threading.RLock()
_MARKET_CALENDAR_CACHE: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
_MARKET_CALENDAR_IN_FLIGHT: dict[tuple[str, str, str, str, str, str], threading.Event] = {}


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return _to_text(value).lower() in {"1", "true", "yes", "on"}


def _disabled(value: Any) -> bool:
    return _to_text(value).lower() in {"0", "false", "no", "off"}


def _cache_seconds(env_name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, fallback)))
    except (TypeError, ValueError):
        return fallback


def _parse_market_date(value: Any):
    text = _to_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def is_nyse_non_trading_day(market_date: Any) -> bool:
    day = _parse_market_date(market_date)
    if day is None or _is_nyse_trading_day is None:
        return False
    try:
        return not bool(_is_nyse_trading_day(day))
    except Exception:
        return False


def _config_text(config_value: ConfigValue | None, key: str, default: str, environment: str) -> str:
    if callable(config_value):
        try:
            return _to_text(config_value(key, default, environment)) or default
        except Exception:
            return default
    return default


def _calendar_contract_args(
    *,
    payload: dict[str, Any] | None,
    config_value: ConfigValue | None,
    environment: str,
) -> dict[str, str]:
    data = _as_dict(payload)
    return {
        "symbol": _to_text(data.get("symbol"))
        or _config_text(config_value, "ibkr_market_calendar_symbol", DEFAULT_CALENDAR_SYMBOL, environment),
        "exchange": _to_text(data.get("exchange"))
        or _config_text(config_value, "ibkr_market_calendar_exchange", DEFAULT_CALENDAR_EXCHANGE, environment),
        "sec_type": _to_text(data.get("sec_type") or data.get("secType"))
        or _config_text(config_value, "ibkr_market_calendar_sec_type", DEFAULT_CALENDAR_SEC_TYPE, environment),
    }


def _status_error(result: dict[str, Any]) -> str:
    payload = _as_dict(result.get("payload"))
    return _to_text(payload.get("error") or result.get("error") or result.get("status_code"))


def _enrich_next_open(snapshot: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if _to_text(snapshot.get("next_open_us")):
        return snapshot
    enriched = dict(snapshot)
    for key in ("next_open_us", "next_open_beijing"):
        if _to_text(fallback.get(key)):
            enriched[key] = fallback.get(key)
    return enriched


def _market_calendar_cache_enabled() -> bool:
    return not _disabled(os.environ.get("IBKR_MARKET_CALENDAR_CACHE_ENABLED", "true"))


def _market_calendar_cache_bypass(data: dict[str, Any]) -> bool:
    return _truthy(data.get("cache_bust")) or _disabled(data.get("cache"))


def _market_calendar_cache_key(
    *,
    market_date: str,
    broker_mode: str,
    data_environment: str,
    contract_args: dict[str, str],
) -> tuple[str, str, str, str, str, str]:
    return (
        market_date,
        _to_text(broker_mode).lower(),
        _to_text(data_environment).lower(),
        _to_text(contract_args.get("symbol")).upper(),
        _to_text(contract_args.get("exchange")).upper(),
        _to_text(contract_args.get("sec_type")).upper(),
    )


def _market_calendar_ttl_seconds(payload: dict[str, Any], *, market_date: str, today_date: str) -> float:
    source_error = _to_text(payload.get("source_error") or payload.get("error"))
    if source_error or payload.get("ok") is False:
        return _cache_seconds("IBKR_MARKET_CALENDAR_ERROR_TTL_SEC", 60.0)
    if market_date and today_date and market_date < today_date:
        return _cache_seconds("IBKR_MARKET_CALENDAR_HISTORY_TTL_SEC", 86400.0)

    market_session = _as_dict(payload.get("market_session"))
    kind = _to_text(market_session.get("kind") or payload.get("session_kind")).lower()
    if payload.get("is_closed") or payload.get("is_trading_day") is False or kind == "closed":
        return _cache_seconds("IBKR_MARKET_CALENDAR_CLOSED_TTL_SEC", 1800.0)
    if kind == "afterhours":
        return _cache_seconds("IBKR_MARKET_CALENDAR_AFTERHOURS_TTL_SEC", 120.0)
    if kind in {"premarket", "regular", "close_transition"}:
        return _cache_seconds("IBKR_MARKET_CALENDAR_ACTIVE_TTL_SEC", 30.0)
    return _cache_seconds("IBKR_MARKET_CALENDAR_DEFAULT_TTL_SEC", 60.0)


def _market_calendar_stale_seconds(ttl_seconds: float) -> float:
    fallback = 300.0 if ttl_seconds < 300.0 else min(ttl_seconds, 3600.0)
    return _cache_seconds("IBKR_MARKET_CALENDAR_STALE_SEC", fallback)


def _market_calendar_cache_entry(payload: dict[str, Any], *, market_date: str, today_date: str) -> dict[str, Any]:
    now = time.monotonic()
    ttl_seconds = _market_calendar_ttl_seconds(payload, market_date=market_date, today_date=today_date)
    stale_seconds = _market_calendar_stale_seconds(ttl_seconds)
    return {
        "payload": payload,
        "created_at": now,
        "expires_at": now + ttl_seconds,
        "stale_until": now + ttl_seconds + stale_seconds,
        "ttl_seconds": ttl_seconds,
    }


def _with_market_calendar_cache_meta(
    payload: dict[str, Any],
    *,
    entry: dict[str, Any],
    state: str,
    stale: bool = False,
    error: Any = None,
) -> dict[str, Any]:
    now = time.monotonic()
    created_at = float(entry.get("created_at") or now)
    meta = {
        "state": state,
        "age_s": round(max(0.0, now - created_at), 3),
        "ttl_s": round(float(entry.get("ttl_seconds") or 0.0), 3),
        "stale": bool(stale),
    }
    if error is not None:
        meta["error"] = str(error)
    return {**payload, "_cache": meta}


def _store_market_calendar_cache_entry(
    key: tuple[str, str, str, str, str, str],
    payload: dict[str, Any],
    *,
    market_date: str,
    today_date: str,
) -> dict[str, Any]:
    entry = _market_calendar_cache_entry(payload, market_date=market_date, today_date=today_date)
    with _MARKET_CALENDAR_CACHE_LOCK:
        _MARKET_CALENDAR_CACHE[key] = entry
    return entry


def _refresh_market_calendar_cache(
    key: tuple[str, str, str, str, str, str],
    *,
    builder: Callable[[], dict[str, Any]],
    market_date: str,
    today_date: str,
    event: threading.Event,
) -> None:
    try:
        payload = builder()
        _store_market_calendar_cache_entry(key, payload, market_date=market_date, today_date=today_date)
    except Exception:
        pass
    finally:
        with _MARKET_CALENDAR_CACHE_LOCK:
            current = _MARKET_CALENDAR_IN_FLIGHT.get(key)
            if current is event:
                _MARKET_CALENDAR_IN_FLIGHT.pop(key, None)
            event.set()


def _cached_market_calendar_payload(
    key: tuple[str, str, str, str, str, str],
    *,
    builder: Callable[[], dict[str, Any]],
    market_date: str,
    today_date: str,
    force: bool = False,
) -> dict[str, Any]:
    if not _market_calendar_cache_enabled():
        payload = builder()
        entry = _market_calendar_cache_entry(payload, market_date=market_date, today_date=today_date)
        return _with_market_calendar_cache_meta(payload, entry=entry, state="bypass")
    if force:
        payload = builder()
        entry = _store_market_calendar_cache_entry(key, payload, market_date=market_date, today_date=today_date)
        return _with_market_calendar_cache_meta(payload, entry=entry, state="bypass")

    now = time.monotonic()
    with _MARKET_CALENDAR_CACHE_LOCK:
        entry = _MARKET_CALENDAR_CACHE.get(key)
        if entry and now <= float(entry.get("expires_at") or 0.0):
            return _with_market_calendar_cache_meta(_as_dict(entry.get("payload")), entry=entry, state="hit")
        if entry and now <= float(entry.get("stale_until") or 0.0):
            event = _MARKET_CALENDAR_IN_FLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _MARKET_CALENDAR_IN_FLIGHT[key] = event
                thread = threading.Thread(
                    target=_refresh_market_calendar_cache,
                    kwargs={
                        "key": key,
                        "builder": builder,
                        "market_date": market_date,
                        "today_date": today_date,
                        "event": event,
                    },
                    daemon=True,
                )
                thread.start()
            return _with_market_calendar_cache_meta(
                _as_dict(entry.get("payload")),
                entry=entry,
                state="stale",
                stale=True,
            )
        event = _MARKET_CALENDAR_IN_FLIGHT.get(key)

    if event is not None:
        event.wait(timeout=2.0)
        with _MARKET_CALENDAR_CACHE_LOCK:
            entry = _MARKET_CALENDAR_CACHE.get(key)
            if entry and time.monotonic() <= float(entry.get("stale_until") or 0.0):
                return _with_market_calendar_cache_meta(
                    _as_dict(entry.get("payload")),
                    entry=entry,
                    state="wait_hit",
                    stale=time.monotonic() > float(entry.get("expires_at") or 0.0),
                )

    created_event = threading.Event()
    with _MARKET_CALENDAR_CACHE_LOCK:
        event = _MARKET_CALENDAR_IN_FLIGHT.get(key)
        if event is None:
            _MARKET_CALENDAR_IN_FLIGHT[key] = created_event
            event = created_event

    if event is not created_event:
        event.wait(timeout=2.0)
        with _MARKET_CALENDAR_CACHE_LOCK:
            entry = _MARKET_CALENDAR_CACHE.get(key)
            if entry:
                return _with_market_calendar_cache_meta(_as_dict(entry.get("payload")), entry=entry, state="wait_hit")

    try:
        payload = builder()
        entry = _store_market_calendar_cache_entry(key, payload, market_date=market_date, today_date=today_date)
        return _with_market_calendar_cache_meta(payload, entry=entry, state="miss")
    except Exception as exc:
        with _MARKET_CALENDAR_CACHE_LOCK:
            entry = _MARKET_CALENDAR_CACHE.get(key)
        if entry and time.monotonic() <= float(entry.get("stale_until") or 0.0):
            return _with_market_calendar_cache_meta(
                _as_dict(entry.get("payload")),
                entry=entry,
                state="stale_error",
                stale=True,
                error=exc,
            )
        raise
    finally:
        with _MARKET_CALENDAR_CACHE_LOCK:
            current = _MARKET_CALENDAR_IN_FLIGHT.get(key)
            if current is created_event:
                _MARKET_CALENDAR_IN_FLIGHT.pop(key, None)
                created_event.set()


def _clear_market_calendar_cache() -> None:
    with _MARKET_CALENDAR_CACHE_LOCK:
        _MARKET_CALENDAR_CACHE.clear()
        _MARKET_CALENDAR_IN_FLIGHT.clear()


def build_market_calendar_snapshot(
    *,
    market_date: Any,
    broker_mode: str = "",
    data_environment: str = "",
    payload: dict[str, Any] | None = None,
    request_json_request: RequestJsonRequest | None = None,
    compute_base_url: str = "",
    config_value: ConfigValue | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    environment = _to_text(data_environment or broker_mode or "live").lower() or "live"
    contract_args = _calendar_contract_args(payload=payload, config_value=config_value, environment=environment)
    source_error = ""
    if callable(request_json_request) and _to_text(compute_base_url):
        try:
            result = request_json_request(
                "GET",
                compute_base_url,
                "/ibkr/market/calendar",
                params=[
                    ("date", _to_text(market_date)),
                    ("market_date", _to_text(market_date)),
                    ("symbol", contract_args["symbol"]),
                    ("exchange", contract_args["exchange"]),
                    ("sec_type", contract_args["sec_type"]),
                    ("broker_mode", _to_text(broker_mode)),
                    ("market_data_mode", environment),
                    ("data_environment", environment),
                ],
                timeout=8.0,
            )
            payload_data = _as_dict(result.get("payload"))
            if (
                int(result.get("status_code") or 200) < 400
                and payload_data.get("ok")
                and _to_text(payload_data.get("source")) == IBKR_SCHEDULE_SOURCE
            ):
                fallback = build_local_nyse_calendar_snapshot(
                    market_date,
                    symbol=contract_args["symbol"],
                    exchange=contract_args["exchange"],
                    sec_type=contract_args["sec_type"],
                    now=now,
                )
                return _enrich_next_open(payload_data, fallback)
            source_error = _status_error(result) or "ibkr_schedule_unavailable"
        except Exception as exc:
            source_error = str(exc)
    else:
        source_error = "compute_calendar_unavailable"

    return build_local_nyse_calendar_snapshot(
        market_date,
        symbol=contract_args["symbol"],
        exchange=contract_args["exchange"],
        sec_type=contract_args["sec_type"],
        source_error=source_error,
        now=now,
    )


def build_market_calendar_response(
    *,
    payload: dict[str, Any] | None,
    time_strings: Callable[[], dict[str, str]],
    request_json_request: RequestJsonRequest | None = None,
    compute_base_url: str = "",
    config_value: ConfigValue | None = None,
) -> tuple[dict[str, Any], int]:
    data = _as_dict(payload)
    times = time_strings()
    market_date = _to_text(data.get("date") or data.get("market_date") or times.get("date"))
    if _parse_market_date(market_date) is None:
        return {"ok": False, "error": "invalid_market_date", "market_date": market_date, "source": "ibkr-api"}, 400
    broker_mode = request_broker_mode(data)
    data_environment = request_market_data_mode(data)
    contract_args = _calendar_contract_args(payload=data, config_value=config_value, environment=data_environment)

    def build_payload() -> dict[str, Any]:
        snapshot = build_market_calendar_snapshot(
            market_date=market_date,
            broker_mode=broker_mode,
            data_environment=data_environment,
            payload=data,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            config_value=config_value,
        )
        return {
            **snapshot,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "source": _to_text(snapshot.get("source")) or LOCAL_NYSE_FALLBACK_SOURCE,
            "api_source": "ibkr-api",
        }

    cache_key = _market_calendar_cache_key(
        market_date=market_date,
        broker_mode=broker_mode,
        data_environment=data_environment,
        contract_args=contract_args,
    )
    payload_data = _cached_market_calendar_payload(
        cache_key,
        builder=build_payload,
        market_date=market_date,
        today_date=_to_text(times.get("date")),
        force=_market_calendar_cache_bypass(data),
    )
    return {
        **payload_data,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
    }, 200


__all__ = [
    "build_market_calendar_response",
    "build_market_calendar_snapshot",
    "_clear_market_calendar_cache",
    "is_nyse_non_trading_day",
]
