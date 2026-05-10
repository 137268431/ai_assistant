from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from ibkr_api.orders.values import ensure_object, parse_boolean, to_int, to_text
from ibkr_api.universe.dynamic_admission import classify_fundamental_profile, normalize_market_cap_usd, safe_float


EscapeFilterString = Callable[[Any], str]
HttpGet = Callable[..., Any]

FUNDAMENTALS_COLLECTION = "ibkr_fundamentals"
DEFAULT_PROVIDER = "finnhub"
SUPPORTED_PROVIDERS = {DEFAULT_PROVIDER}
DEFAULT_CACHE_TTL_HOURS = 168
MAX_REFRESH_SYMBOLS = 50
FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
SENSITIVE_KEY_PARTS = ("api_key", "apikey", "token", "secret")


def normalize_symbols(value: Any) -> list[str]:
    raw_items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        symbol = to_text(raw_item).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def normalize_provider(value: Any) -> str:
    provider = to_text(value or DEFAULT_PROVIDER).lower() or DEFAULT_PROVIDER
    return provider if provider in SUPPORTED_PROVIDERS else ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> datetime | None:
    text = to_text(value)
    if not text:
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text[:19], pattern.replace("Z", "")) if pattern.endswith("Z") else datetime.strptime(text[:19], pattern)
            return parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_sensitive(raw_value)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _fundamentals_filter(symbol: str, provider: str, escape_filter_string: EscapeFilterString) -> str:
    return (
        f'symbol = "{escape_filter_string(symbol)}" && '
        f'provider = "{escape_filter_string(provider)}"'
    )


def _load_cached_record(
    pb: Any,
    symbol: str,
    provider: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any]:
    try:
        record = pb.get_first_record(
            FUNDAMENTALS_COLLECTION,
            filter=_fundamentals_filter(symbol, provider, escape_filter_string),
        )
        return dict(record or {})
    except Exception:
        return {}


def load_fundamentals_cache_by_symbol(
    pb: Any,
    symbols: list[str],
    *,
    provider: str = DEFAULT_PROVIDER,
    escape_filter_string: EscapeFilterString,
) -> dict[str, dict[str, Any]]:
    normalized_symbols = normalize_symbols(symbols)
    normalized_provider = normalize_provider(provider) or DEFAULT_PROVIDER
    if not normalized_symbols:
        return {}
    symbol_filter = "(" + " || ".join(f'symbol = "{escape_filter_string(symbol)}"' for symbol in normalized_symbols) + ")"
    filter_expr = f'{symbol_filter} && provider = "{escape_filter_string(normalized_provider)}"'
    try:
        rows = pb.get_records(FUNDAMENTALS_COLLECTION, filter=filter_expr, sort="-updated", per_page=200, page=1)
    except Exception:
        rows = []
    result: dict[str, dict[str, Any]] = {}
    for raw_row in rows or []:
        row = dict(raw_row or {})
        symbol = to_text(row.get("symbol")).upper()
        if symbol and symbol not in result:
            result[symbol] = row
    return result


def _record_is_fresh(record: dict[str, Any], *, now: datetime) -> bool:
    if to_text(record.get("status")).lower() != "fresh":
        return False
    expires_at = _parse_utc(record.get("expires_at"))
    return bool(expires_at and expires_at > now)


def _extract_finnhub_profile(raw_payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    raw = ensure_object(raw_payload)
    market_cap_millions = safe_float(raw.get("marketCapitalization"))
    market_cap_usd = normalize_market_cap_usd(None, market_cap_millions=market_cap_millions)
    share_outstanding_millions = safe_float(raw.get("shareOutstanding"))
    profile = classify_fundamental_profile(market_cap_usd)
    return {
        "symbol": to_text(raw.get("ticker") or symbol).upper(),
        "company_name": to_text(raw.get("name")),
        "exchange": to_text(raw.get("exchange")).upper(),
        "industry": to_text(raw.get("finnhubIndustry")),
        "currency": to_text(raw.get("currency")).upper(),
        "ipo": to_text(raw.get("ipo")),
        "web_url": to_text(raw.get("weburl")),
        "logo_url": to_text(raw.get("logo")),
        "market_cap_millions": round(market_cap_millions, 6) if market_cap_millions > 0 else None,
        "market_cap_usd": round(market_cap_usd, 2) if market_cap_usd > 0 else None,
        "share_outstanding_millions": round(share_outstanding_millions, 6) if share_outstanding_millions > 0 else None,
        "profile": profile,
        "raw_profile": _redact_sensitive(raw),
    }


def _fetch_finnhub_profile(
    symbol: str,
    *,
    api_key: str,
    http_get: HttpGet,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    if not api_key:
        return {"ok": False, "error": "finnhub_api_key_missing"}, 503
    try:
        response = http_get(
            FINNHUB_PROFILE_URL,
            params={"symbol": symbol, "token": api_key},
            timeout=max(1.0, float(timeout or 0)),
        )
    except Exception:
        # Do not echo request exceptions: requests may include the tokenized URL.
        return {"ok": False, "error": "finnhub_request_failed"}, 502

    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json() if getattr(response, "content", True) else {}
    except Exception:
        payload = {}
    if status_code == 429:
        return {"ok": False, "error": "finnhub_rate_limited", "status_code": status_code}, 429
    if status_code >= 400 or not isinstance(payload, dict):
        return {"ok": False, "error": "finnhub_request_failed", "status_code": status_code}, 502
    if not payload or not to_text(payload.get("ticker") or symbol):
        return {"ok": False, "error": "finnhub_profile_not_found", "status_code": status_code}, 404
    return {"ok": True, "payload": payload, "status_code": status_code}, 200


def _build_cache_record(
    *,
    symbol: str,
    provider: str,
    status: str,
    fetched_at: str,
    expires_at: str,
    normalized_profile: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    profile = normalized_profile or {}
    return {
        "symbol": symbol,
        "provider": provider,
        "status": status,
        "company_name": to_text(profile.get("company_name")),
        "exchange": to_text(profile.get("exchange")),
        "industry": to_text(profile.get("industry")),
        "currency": to_text(profile.get("currency")),
        "ipo": to_text(profile.get("ipo")),
        "web_url": to_text(profile.get("web_url")),
        "logo_url": to_text(profile.get("logo_url")),
        "market_cap_usd": profile.get("market_cap_usd"),
        "market_cap_millions": profile.get("market_cap_millions"),
        "share_outstanding_millions": profile.get("share_outstanding_millions"),
        "profile": to_text(profile.get("profile")),
        "raw_profile": profile.get("raw_profile") or {},
        "error": to_text(error)[:500],
        "fetched_at": fetched_at,
        "expires_at": expires_at,
    }


def _save_cache_record(
    pb: Any,
    data: dict[str, Any],
    *,
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, Any], str]:
    symbol = to_text(data.get("symbol")).upper()
    provider = normalize_provider(data.get("provider")) or DEFAULT_PROVIDER
    filter_expr = _fundamentals_filter(symbol, provider, escape_filter_string)
    existing = None
    try:
        existing = pb.get_first_record(FUNDAMENTALS_COLLECTION, filter=filter_expr)
    except Exception:
        existing = None
    if existing:
        record = pb.update_record(FUNDAMENTALS_COLLECTION, to_text(existing.get("id")), data)
        return dict(record or data), "updated"
    record = pb.create_record(FUNDAMENTALS_COLLECTION, data)
    return dict(record or data), "created"


def serialize_fundamentals_record(record: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    row = dict(record or {})
    item = {
        "id": to_text(row.get("id")),
        "symbol": to_text(row.get("symbol")).upper(),
        "provider": to_text(row.get("provider") or DEFAULT_PROVIDER).lower(),
        "status": to_text(row.get("status")),
        "profile": to_text(row.get("profile")),
        "company_name": to_text(row.get("company_name")),
        "exchange": to_text(row.get("exchange")),
        "industry": to_text(row.get("industry")),
        "currency": to_text(row.get("currency")),
        "ipo": to_text(row.get("ipo")),
        "web_url": to_text(row.get("web_url")),
        "logo_url": to_text(row.get("logo_url")),
        "market_cap_usd": row.get("market_cap_usd"),
        "market_cap_millions": row.get("market_cap_millions"),
        "share_outstanding_millions": row.get("share_outstanding_millions"),
        "fetched_at": to_text(row.get("fetched_at")),
        "expires_at": to_text(row.get("expires_at")),
        "error": to_text(row.get("error")),
        "updated": to_text(row.get("updated")),
    }
    if include_raw:
        item["raw_profile"] = _redact_sensitive(row.get("raw_profile") or {})
    return item


def build_fundamentals_refresh_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    escape_filter_string: EscapeFilterString,
    http_get: HttpGet | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    provider = normalize_provider(payload.get("provider"))
    if not provider:
        return {"ok": False, "error": "unsupported_fundamentals_provider", "source": "ibkr-api"}, 400
    symbols = normalize_symbols(payload.get("symbols") or payload.get("symbol"))[:MAX_REFRESH_SYMBOLS]
    if not symbols:
        return {"ok": False, "error": "missing_symbols", "source": "ibkr-api"}, 400

    env = environ if environ is not None else os.environ
    api_key = to_text(env.get("FINNHUB_API_KEY"))
    if provider == DEFAULT_PROVIDER and not api_key:
        return {"ok": False, "error": "finnhub_api_key_missing", "provider": provider, "source": "ibkr-api"}, 503

    force = parse_boolean(payload.get("force"), False)
    include_raw = parse_boolean(payload.get("include_raw"), False)
    ttl_hours = max(1, min(24 * 30, to_int(payload.get("ttl_hours"), DEFAULT_CACHE_TTL_HOURS)))
    timeout = max(1.0, float(safe_float(payload.get("timeout_sec"), 10.0) or 10.0))
    now = _utc_now()
    fetched_at = _format_utc(now)
    expires_at = _format_utc(now + timedelta(hours=ttl_hours))
    getter = http_get or requests.get

    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    refreshed = 0
    cache_hits = 0
    for symbol in symbols:
        cached = _load_cached_record(pb, symbol, provider, escape_filter_string=escape_filter_string)
        if cached and not force and _record_is_fresh(cached, now=now):
            cache_hits += 1
            item = serialize_fundamentals_record(cached, include_raw=include_raw)
            item["action"] = "cache_hit"
            items.append(item)
            continue

        fetch_result, fetch_status = _fetch_finnhub_profile(symbol, api_key=api_key, http_get=getter, timeout=timeout)
        if not fetch_result.get("ok"):
            error = to_text(fetch_result.get("error") or "fundamentals_refresh_failed")
            errors.append({"symbol": symbol, "provider": provider, "error": error, "status_code": fetch_status})
            failed_data = _build_cache_record(
                symbol=symbol,
                provider=provider,
                status="failed",
                fetched_at=fetched_at,
                expires_at=fetched_at,
                error=error,
            )
            try:
                record, action = _save_cache_record(pb, failed_data, escape_filter_string=escape_filter_string)
                item = serialize_fundamentals_record(record, include_raw=include_raw)
                item["action"] = action
                items.append(item)
            except Exception:
                items.append({"symbol": symbol, "provider": provider, "status": "failed", "error": error, "action": "not_cached"})
            continue

        normalized = _extract_finnhub_profile(ensure_object(fetch_result.get("payload")), symbol)
        record_data = _build_cache_record(
            symbol=symbol,
            provider=provider,
            status="fresh",
            fetched_at=fetched_at,
            expires_at=expires_at,
            normalized_profile=normalized,
        )
        try:
            record, action = _save_cache_record(pb, record_data, escape_filter_string=escape_filter_string)
        except Exception as exc:
            return {
                "ok": False,
                "error": "fundamentals_cache_write_failed",
                "detail": str(exc),
                "provider": provider,
                "source": "ibkr-api",
            }, 500
        refreshed += 1
        item = serialize_fundamentals_record(record, include_raw=include_raw)
        item["action"] = action
        items.append(item)

    ok = refreshed > 0 or cache_hits == len(symbols)
    return {
        "ok": ok,
        "provider": provider,
        "ttl_hours": ttl_hours,
        "requested": len(symbols),
        "refreshed": refreshed,
        "cache_hits": cache_hits,
        "failed": len(errors),
        "items": items,
        "errors": errors,
        "source": "ibkr-api",
    }, 200 if ok else 502


def build_fundamentals_list_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, Any], int]:
    provider = normalize_provider(payload.get("provider") or DEFAULT_PROVIDER)
    if not provider:
        return {"ok": False, "error": "unsupported_fundamentals_provider", "source": "ibkr-api"}, 400
    symbols = normalize_symbols(payload.get("symbols") or payload.get("symbol"))
    include_raw = parse_boolean(payload.get("include_raw"), False)
    page = max(1, to_int(payload.get("page"), 1))
    per_page = max(1, min(200, to_int(payload.get("per_page") or payload.get("page_size"), 100)))
    status = to_text(payload.get("status")).lower()

    clauses = [f'provider = "{escape_filter_string(provider)}"']
    if symbols:
        clauses.append("(" + " || ".join(f'symbol = "{escape_filter_string(symbol)}"' for symbol in symbols) + ")")
    if status:
        clauses.append(f'status = "{escape_filter_string(status)}"')
    filter_expr = " && ".join(clauses)
    try:
        rows = pb.get_records(
            FUNDAMENTALS_COLLECTION,
            filter=filter_expr,
            sort=to_text(payload.get("sort") or "symbol"),
            per_page=per_page,
            page=page,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "fundamentals_cache_read_failed",
            "detail": str(exc),
            "source": "ibkr-api",
        }, 500

    items = [serialize_fundamentals_record(dict(row or {}), include_raw=include_raw) for row in rows or []]
    return {
        "ok": True,
        "provider": provider,
        "page": page,
        "per_page": per_page,
        "items": items,
        "count": len(items),
        "source": "ibkr-api",
    }, 200


__all__ = [
    "FUNDAMENTALS_COLLECTION",
    "build_fundamentals_list_response",
    "build_fundamentals_refresh_response",
    "load_fundamentals_cache_by_symbol",
    "normalize_symbols",
    "serialize_fundamentals_record",
]
