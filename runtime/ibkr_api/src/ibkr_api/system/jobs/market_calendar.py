from __future__ import annotations

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


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


def build_market_calendar_snapshot(
    *,
    market_date: Any,
    broker_mode: str = "",
    data_environment: str = "",
    payload: dict[str, Any] | None = None,
    request_json_request: RequestJsonRequest | None = None,
    compute_base_url: str = "",
    config_value: ConfigValue | None = None,
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
    }, 200


__all__ = [
    "build_market_calendar_response",
    "build_market_calendar_snapshot",
    "is_nyse_non_trading_day",
]
