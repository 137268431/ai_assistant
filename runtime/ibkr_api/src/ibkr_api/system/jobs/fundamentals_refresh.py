from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ibkr_api.modes import request_market_data_mode
from ibkr_api.orders.values import parse_boolean, to_int, to_text
from ibkr_api.universe.fundamentals import (
    DEFAULT_PROVIDER,
    build_fundamentals_refresh_response,
    load_fundamentals_cache_by_symbol,
    normalize_symbols,
)


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
ConfigValue = Callable[[str, str, str], str]
WriteSystemEventRecord = Callable[..., dict[str, Any]]

DEFAULT_BATCH_SIZE = 12
DEFAULT_REFRESH_DAYS = 7
MAX_BATCH_SIZE = 50


def _config_bool(config_value: ConfigValue, key: str, default: bool, environment: str) -> bool:
    try:
        raw = config_value(key, "TRUE" if default else "FALSE", environment)
    except Exception:
        raw = default
    return parse_boolean(raw, default)


def _config_int(
    config_value: ConfigValue,
    key: str,
    default: int,
    environment: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        raw = config_value(key, str(default), environment)
    except Exception:
        raw = default
    parsed = max(int(minimum), to_int(raw, default))
    if maximum is not None:
        parsed = min(int(maximum), parsed)
    return parsed


def _unique_symbols(values: Any) -> list[str]:
    return normalize_symbols(values)


def _watchlist_filter(environment: str) -> str:
    runtime_environment = to_text(environment).lower() or "live"
    return (
        f'(environment = "{runtime_environment}" || environment = "global" || environment = "") && '
        'symbol != "" && '
        '(symbol_role = "trade" || symbol_role = "")'
    )


def _load_trade_watchlist_symbols(pb: Any, environment: str) -> list[str]:
    filter_expr = _watchlist_filter(environment)
    try:
        if hasattr(pb, "get_all_records"):
            rows = pb.get_all_records("watchlist", filter=filter_expr, sort="symbol", max_pages=20)
        else:
            rows = pb.get_records("watchlist", filter=filter_expr, sort="symbol", per_page=500, page=1)
    except Exception:
        rows = []
    return _unique_symbols([row.get("symbol") for row in rows or [] if isinstance(row, dict)])


def _parse_utc(value: Any) -> datetime | None:
    text = to_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _needs_refresh(record: dict[str, Any], *, now: datetime, refresh_days: int, force: bool) -> bool:
    if force or not record:
        return True
    if to_text(record.get("status")).lower() != "fresh":
        return True
    fetched_at = _parse_utc(record.get("fetched_at") or record.get("updated"))
    if fetched_at is None:
        return True
    return fetched_at <= now - timedelta(days=max(1, int(refresh_days or DEFAULT_REFRESH_DAYS)))


def _select_refresh_symbols(
    pb: Any,
    symbols: list[str],
    *,
    provider: str,
    refresh_days: int,
    force: bool,
    batch_size: int,
    escape_filter_string: EscapeFilterString,
) -> tuple[list[str], dict[str, int]]:
    cache = load_fundamentals_cache_by_symbol(
        pb,
        symbols,
        provider=provider,
        escape_filter_string=escape_filter_string,
    )
    now = datetime.now(timezone.utc)
    missing: list[str] = []
    stale: list[str] = []
    fresh = 0
    for symbol in symbols:
        record = cache.get(symbol) or {}
        if not record:
            missing.append(symbol)
        elif _needs_refresh(record, now=now, refresh_days=refresh_days, force=force):
            stale.append(symbol)
        else:
            fresh += 1
    selected = [*missing, *stale][: max(1, min(MAX_BATCH_SIZE, int(batch_size or DEFAULT_BATCH_SIZE)))]
    return selected, {"missing": len(missing), "stale": len(stale), "fresh": fresh}


def build_fundamentals_refresh_job_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    config_value: ConfigValue,
    write_system_event_record: WriteSystemEventRecord | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    environment = request_market_data_mode(request_payload)
    provider = to_text(request_payload.get("provider") or DEFAULT_PROVIDER).lower() or DEFAULT_PROVIDER
    if not _config_bool(config_value, "ibkr_fundamentals_enabled", True, environment):
        return {
            "ok": True,
            "skipped": True,
            "reason": "fundamentals_disabled",
            "environment": environment,
            "provider": provider,
            "source": "ibkr-api",
            "job_id": "ibkr_fundamentals_refresh",
        }, 200

    requested_symbols = _unique_symbols(request_payload.get("symbols") or request_payload.get("symbol"))
    symbols = requested_symbols or _load_trade_watchlist_symbols(pb, environment)
    if not symbols:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_trade_watchlist_symbols",
            "environment": environment,
            "provider": provider,
            "source": "ibkr-api",
            "job_id": "ibkr_fundamentals_refresh",
        }, 200

    force = parse_boolean(request_payload.get("force"), False)
    batch_size = _config_int(
        config_value,
        "ibkr_fundamentals_batch_size",
        DEFAULT_BATCH_SIZE,
        environment,
        minimum=1,
        maximum=MAX_BATCH_SIZE,
    )
    batch_size = max(1, min(MAX_BATCH_SIZE, to_int(request_payload.get("batch_size"), batch_size)))
    refresh_days = _config_int(
        config_value,
        "ibkr_fundamentals_refresh_days",
        DEFAULT_REFRESH_DAYS,
        environment,
        minimum=1,
        maximum=365,
    )
    selected, counts = _select_refresh_symbols(
        pb,
        symbols,
        provider=provider,
        refresh_days=refresh_days,
        force=force,
        batch_size=batch_size,
        escape_filter_string=escape_filter_string,
    )
    if not selected:
        return {
            "ok": True,
            "skipped": True,
            "reason": "fundamentals_cache_fresh",
            "environment": environment,
            "provider": provider,
            "watched_symbols": len(symbols),
            "selected_symbols": [],
            "cache_counts": counts,
            "refresh_days": refresh_days,
            "source": "ibkr-api",
            "job_id": "ibkr_fundamentals_refresh",
        }, 200

    result, status_code = build_fundamentals_refresh_response(
        pb,
        payload={
            "symbols": selected,
            "provider": provider,
            "force": force,
            "ttl_hours": refresh_days * 24,
            "include_raw": False,
            "source": "ibkr_fundamentals_refresh_job",
        },
        escape_filter_string=escape_filter_string,
    )
    response = {
        **result,
        "environment": environment,
        "watched_symbols": len(symbols),
        "selected_symbols": selected,
        "cache_counts": counts,
        "refresh_days": refresh_days,
        "job_id": "ibkr_fundamentals_refresh",
    }
    if status_code >= 400 and callable(write_system_event_record):
        try:
            write_system_event_record(
                "fundamentals_refresh",
                "warning",
                "ibkr-api",
                "Fundamentals refresh failed",
                f"provider={provider}, error={to_text(response.get('error')) or 'unknown'}",
                environment,
                False,
            )
        except Exception:
            pass
    return response, status_code


__all__ = ["build_fundamentals_refresh_job_response"]
