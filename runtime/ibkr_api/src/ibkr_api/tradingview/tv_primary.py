from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.values import parse_boolean
from ibkr_api.reverse.repository import upsert_reverse_record

try:
    from ibkr_compute.core.time_utils import ET
except Exception:  # pragma: no cover - imported in tests without compute sometimes
    ET = None  # type: ignore[assignment]

TV_EVENT_TYPES = {"pre_alert", "entry", "risk_update", "exit", "heartbeat"}
TV_EVENT_COLLECTION = "tv_webhook_events"
TRADINGVIEW_SOURCE = "tradingview"
TV_WEBHOOK_SPOOL_DIR_ENV = "IBKR_TV_WEBHOOK_SPOOL_DIR"

logger = logging.getLogger(__name__)


class TvPrimaryError(ValueError):
    def __init__(self, reason: str, status_code: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status_code = int(status_code or 400)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower().replace("-", "_")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _timestamp_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        number = float(value)
        if number > 1_000_000_000_000:
            return int(number)
        if number > 1_000_000_000:
            return int(number * 1000)
        return int(number)
    except Exception:
        pass
    text = _text(value)
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _joined_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(_text(item) for item in value if _text(item))
    return _text(value)


def _mtf_payload(payload: dict[str, Any]) -> dict[str, Any]:
    extra = _as_object(payload.get("extra"))
    mtf = _as_object(payload.get("mtf"))
    extra_mtf = _as_object(extra.get("mtf"))
    return {**extra_mtf, **mtf}


def _entry_tf(payload: dict[str, Any]) -> str:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    return _text(
        payload.get("entry_tf")
        or payload.get("chart_tf")
        or extra.get("entry_tf")
        or extra.get("chart_tf")
        or mtf.get("entry_tf")
    )


def _confirm_tfs(payload: dict[str, Any]) -> str:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    return _joined_text(payload.get("confirm_tfs") or extra.get("confirm_tfs") or mtf.get("confirm_tfs"))


def _mtf_status(payload: dict[str, Any]) -> str:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    return _lower(payload.get("mtf_status") or extra.get("mtf_status") or mtf.get("status"))


def _mtf_score(payload: dict[str, Any]) -> float:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    return _float(payload.get("mtf_score") or extra.get("mtf_score") or mtf.get("score"), 0.0)


def _mtf_block_reason(payload: dict[str, Any]) -> str:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    return _text(payload.get("mtf_block_reason") or extra.get("mtf_block_reason") or mtf.get("block_reason"))


def _has_mtf_context(payload: dict[str, Any]) -> bool:
    extra = _as_object(payload.get("extra"))
    if _mtf_payload(payload):
        return True
    for key in ("mtf_status", "mtf_score", "mtf_block_reason", "confirm_tfs"):
        if payload.get(key) not in (None, "") or extra.get(key) not in (None, ""):
            return True
    return False


def _interval_text(payload: dict[str, Any]) -> str:
    extra = _as_object(payload.get("extra"))
    return _text(
        payload.get("interval")
        or payload.get("chart_tf")
        or payload.get("entry_tf")
        or payload.get("timeframe")
        or payload.get("timeframe_stack")
        or extra.get("interval")
        or extra.get("chart_tf")
        or extra.get("timeframe")
        or extra.get("timeframe_stack")
    )


def _interval_ms(value: Any) -> int:
    text = _text(value).lower()
    if "entry=" in text:
        for part in text.split(";"):
            key, _, raw_value = part.partition("=")
            if key.strip() == "entry":
                text = raw_value.strip()
                break
    if text.startswith("chart="):
        text = text.split("=", 1)[1].strip()
    if not text:
        return 0
    multipliers = {
        "s": 1000,
        "m": 60 * 1000,
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
        "w": 7 * 24 * 60 * 60 * 1000,
    }
    if text in {"d", "1d", "day"}:
        return multipliers["d"]
    if text in {"w", "1w", "week"}:
        return multipliers["w"]
    suffix = text[-1:]
    if suffix in multipliers:
        amount = _float(text[:-1] or 1, 0.0)
        return int(amount * multipliers[suffix]) if amount > 0 else 0
    amount = _float(text, 0.0)
    return int(amount * multipliers["m"]) if amount > 0 else 0


def _payload_bar_open_ms(payload: dict[str, Any]) -> int:
    extra = _as_object(payload.get("extra"))
    return (
        _timestamp_ms(payload.get("bar_open_ms"))
        or _timestamp_ms(payload.get("bar_time_ms"))
        or _timestamp_ms(payload.get("time_ms"))
        or _timestamp_ms(payload.get("time"))
        or _timestamp_ms(extra.get("bar_open_ms"))
        or _timestamp_ms(extra.get("bar_time_ms"))
    )


def _payload_bar_close_ms(payload: dict[str, Any]) -> int:
    extra = _as_object(payload.get("extra"))
    explicit = (
        _timestamp_ms(payload.get("bar_close_ms"))
        or _timestamp_ms(payload.get("time_close_ms"))
        or _timestamp_ms(payload.get("time_close"))
        or _timestamp_ms(extra.get("bar_close_ms"))
        or _timestamp_ms(extra.get("time_close_ms"))
    )
    if explicit:
        return explicit
    bar_open_ms = _payload_bar_open_ms(payload)
    interval_ms = _interval_ms(_interval_text(payload))
    return bar_open_ms + interval_ms if bar_open_ms > 0 and interval_ms > 0 else 0


def _payload_pine_eval_ms(payload: dict[str, Any]) -> int:
    extra = _as_object(payload.get("extra"))
    return (
        _timestamp_ms(payload.get("pine_eval_ms"))
        or _timestamp_ms(payload.get("script_eval_ms"))
        or _timestamp_ms(payload.get("timenow_ms"))
        or _timestamp_ms(payload.get("timenow"))
        or _timestamp_ms(payload.get("tv_fire_ms"))
        or _timestamp_ms(payload.get("tv_fire_time"))
        or _timestamp_ms(extra.get("pine_eval_ms"))
        or _timestamp_ms(extra.get("timenow"))
    )


def _duration_ms(start_ms: int, end_ms: int) -> int | None:
    if start_ms > 0 and end_ms > 0 and end_ms >= start_ms:
        return int(end_ms - start_ms)
    return None


def _latency_trace(
    payload: dict[str, Any],
    *,
    api_received_at_ms: int | None = None,
    pb_created_at_ms: int | None = None,
    route_finished_at_ms: int | None = None,
) -> dict[str, Any]:
    interval = _interval_text(payload)
    bar_open_ms = _payload_bar_open_ms(payload)
    bar_close_ms = _payload_bar_close_ms(payload)
    pine_eval_ms = _payload_pine_eval_ms(payload)
    api_received_ms = _int(api_received_at_ms, 0)
    pb_created_ms = _int(pb_created_at_ms, 0)
    route_finished_ms = _int(route_finished_at_ms, 0)
    trace: dict[str, Any] = {}
    if interval:
        trace["interval"] = interval
    for key, value in (
        ("bar_open_ms", bar_open_ms),
        ("bar_close_ms", bar_close_ms),
        ("pine_eval_ms", pine_eval_ms),
        ("api_received_at_ms", api_received_ms),
        ("pb_created_at_ms", pb_created_ms),
        ("route_finished_at_ms", route_finished_ms),
    ):
        if value > 0:
            trace[key] = value
    trace["bar_close_to_pine_eval_ms"] = _duration_ms(bar_close_ms, pine_eval_ms)
    trace["pine_eval_to_api_received_ms"] = _duration_ms(pine_eval_ms, api_received_ms)
    trace["api_received_to_pb_created_ms"] = _duration_ms(api_received_ms, pb_created_ms)
    trace["pb_created_to_route_finished_ms"] = _duration_ms(pb_created_ms, route_finished_ms)
    trace["bar_close_to_route_finished_ms"] = _duration_ms(bar_close_ms, route_finished_ms)
    return {key: value for key, value in trace.items() if value not in ("", None)}


def _extra_with_latency_trace(
    base_extra: dict[str, Any],
    payload: dict[str, Any],
    *,
    api_received_at_ms: int | None = None,
    pb_created_at_ms: int | None = None,
    route_finished_at_ms: int | None = None,
) -> dict[str, Any]:
    extra = dict(base_extra or {})
    trace = {
        **_as_object(extra.get("latency_trace")),
        **_latency_trace(
            payload,
            api_received_at_ms=api_received_at_ms,
            pb_created_at_ms=pb_created_at_ms,
            route_finished_at_ms=route_finished_at_ms,
        ),
    }
    if trace:
        extra["latency_trace"] = trace
    for key in ("bar_open_ms", "bar_close_ms", "pine_eval_ms", "interval"):
        if trace.get(key) not in (None, "", 0):
            extra.setdefault(key, trace[key])
    return extra


def _escape(escape_filter: Callable[[Any], str], value: Any) -> str:
    return escape_filter(value)


def _now_et() -> datetime:
    if ET is not None:
        return datetime.now(ET)
    return datetime.now()


def _market_date(payload: dict[str, Any]) -> str:
    explicit = _text(payload.get("date") or payload.get("market_date"))
    if explicit:
        return explicit[:10]
    us_time = _text(payload.get("us_time"))
    if len(us_time) >= 10 and us_time[4:5] == "-" and us_time[7:8] == "-":
        return us_time[:10]
    return _now_et().strftime("%Y-%m-%d")


def _event_time_hhmm(payload: dict[str, Any]) -> tuple[int, int]:
    for key in ("us_time", "time", "timestamp"):
        text = _text(payload.get(key))
        if len(text) >= 16 and text[13:14] == ":":
            try:
                return int(text[11:13]), int(text[14:16])
            except Exception:
                pass
        if len(text) >= 5 and text[2:3] == ":":
            try:
                return int(text[:2]), int(text[3:5])
            except Exception:
                pass
    bar_open_ms = _payload_bar_open_ms(payload)
    if bar_open_ms > 0:
        try:
            event_dt = datetime.fromtimestamp(bar_open_ms / 1000.0, ET or timezone.utc)
            return event_dt.hour, event_dt.minute
        except Exception:
            pass
    now = _now_et()
    return now.hour, now.minute


def _parse_hhmm(raw: Any, default: str) -> tuple[int, int]:
    text = _text(raw) or default
    try:
        hour, minute = text.split(":", 1)
        return int(hour), int(minute)
    except Exception:
        return tuple(int(part) for part in default.split(":"))  # type: ignore[return-value]


def _config(config_value: Callable[[str, str, str], str] | None, key: str, default: str, environment: str) -> str:
    if not callable(config_value):
        return default
    try:
        return _text(config_value(key, default, environment)) or default
    except Exception:
        return default


def _config_bool(config_value: Callable[[str, str, str], str] | None, key: str, default: bool, environment: str) -> bool:
    raw = _config(config_value, key, "TRUE" if default else "FALSE", environment)
    return parse_boolean(raw, default)


def _config_int(config_value: Callable[[str, str, str], str] | None, key: str, default: int, environment: str) -> int:
    return max(0, _int(_config(config_value, key, str(default), environment), default))


def _config_symbol_set(config_value: Callable[[str, str, str], str] | None, key: str, environment: str) -> set[str]:
    raw = _config(config_value, key, "", environment)
    parts = raw.replace(";", ",").replace("\n", ",").split(",")
    return {_text(part).upper() for part in parts if _text(part)}


def _trade_watchlist_symbol_set(pb: Any, *, environment: str) -> set[str]:
    getter = getattr(pb, "get_all_records", None)
    try:
        rows = getter("watchlist", filter="", sort="-updated", max_pages=5) if callable(getter) else pb.get_records(
            "watchlist",
            filter="",
            sort="-updated",
            per_page=500,
            page=1,
        )
    except Exception as exc:
        logger.debug("Failed to load TV-primary watchlist universe: %s", exc)
        return set()

    allowed_envs = {environment, "global", ""}
    symbols: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_env = _lower(row.get("environment"))
        if row_env not in allowed_envs:
            continue
        role = _lower(row.get("symbol_role") or row.get("role") or "trade")
        if role == "market_monitor":
            continue
        symbol = _text(row.get("symbol")).upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def _authorized_symbol_universe(
    pb: Any,
    *,
    config_value: Callable[[str, str, str], str] | None,
    environment: str,
) -> tuple[set[str], str]:
    configured = _config_symbol_set(config_value, "tv_primary_trade_universe_symbols", environment)
    if configured:
        return configured, "config"
    watchlist = _trade_watchlist_symbol_set(pb, environment=environment)
    if watchlist:
        return watchlist, "watchlist"
    return set(), ""


def _event_type(payload: dict[str, Any]) -> str:
    event_type = _lower(payload.get("event_type") or payload.get("type") or "")
    aliases = {
        "signal": "entry",
        "buy": "entry",
        "open": "entry",
        "close": "exit",
        "sell_exit": "exit",
        "adjust": "risk_update",
        "risk": "risk_update",
    }
    return aliases.get(event_type, event_type)


def _symbol(payload: dict[str, Any]) -> str:
    return _text(payload.get("symbol") or payload.get("ticker")).upper()


def _direction_bias(payload: dict[str, Any]) -> str:
    value = _lower(
        payload.get("direction_bias")
        or payload.get("candidate_direction")
        or payload.get("direction")
        or payload.get("position_side")
    )
    return value if value in {"long", "short", "neutral"} else ""


def _entry_price(payload: dict[str, Any]) -> float:
    return _float(payload.get("entry") or payload.get("entry_price") or payload.get("limit_price"), 0.0)


def _shares(payload: dict[str, Any]) -> float:
    return _float(payload.get("shares") or payload.get("quantity") or payload.get("qty"), 0.0)


def _source_is_tv(payload: dict[str, Any]) -> bool:
    source = _lower(payload.get("source") or payload.get("signal_source") or "tv")
    return source in {"tv", "tradingview", "webhook_tv", "tv_webhook", ""}


def _event_id(payload: dict[str, Any], *, event_type: str) -> str:
    explicit = _text(payload.get("event_id"))
    if explicit:
        return explicit
    signal_id = _text(payload.get("signal_id"))
    if signal_id and event_type == "entry":
        return signal_id
    symbol = _symbol(payload)
    side = _lower(payload.get("position_side") or payload.get("direction") or payload.get("direction_bias")) or "na"
    position_id = _text(payload.get("position_id"))
    bar_time_ms = _text(payload.get("bar_time_ms") or payload.get("time_ms") or payload.get("time")) or str(int(_now_et().timestamp() * 1000))
    script = _text(payload.get("script_tag") or payload.get("strategy_version") or "tv")
    if position_id:
        return f"{script}:{position_id}:{event_type}:{bar_time_ms}"
    return f"{script}:{symbol}:{event_type}:{side}:{bar_time_ms}"


def _base_extra(
    payload: dict[str, Any],
    event_id: str,
    event_type: str,
    *,
    api_received_at_ms: int | None = None,
) -> dict[str, Any]:
    extra = _as_object(payload.get("extra"))
    mtf = _mtf_payload(payload)
    entry_tf = _entry_tf(payload)
    confirm_tfs = _confirm_tfs(payload)
    activity_score = _float(payload.get("activity_score"), 0.0)
    quality_score = _float(payload.get("quality_score"), activity_score)
    base = {
        **extra,
        "source": TRADINGVIEW_SOURCE,
        "tv_event_id": event_id,
        "event_type": event_type,
        "position_id": _text(payload.get("position_id")),
        "script_tag": _text(payload.get("script_tag")),
        "strategy_version": _text(payload.get("strategy_version")),
        "timeframe_stack": _text(payload.get("timeframe_stack") or extra.get("timeframe_stack")),
        "tv_chart_url": _text(payload.get("tv_chart_url")),
        "activity_score": activity_score,
        "quality_score": quality_score,
        "premarket_context": _as_object(payload.get("premarket_context")),
        "tv_snapshot": _as_object(payload.get("tv_snapshot")),
        "reason": _text(payload.get("reason") or extra.get("reason")),
    }
    if entry_tf:
        base["entry_tf"] = entry_tf
    if confirm_tfs:
        base["confirm_tfs"] = confirm_tfs
    if _has_mtf_context(payload):
        base.update(
            {
                "mtf": mtf,
                "mtf_status": _mtf_status(payload),
                "mtf_score": _mtf_score(payload),
                "mtf_block_reason": _mtf_block_reason(payload),
            }
        )
    return _extra_with_latency_trace(base, payload, api_received_at_ms=api_received_at_ms)


def _event_record_payload(
    payload: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    broker_mode: str,
    api_received_at_ms: int | None = None,
) -> dict[str, Any]:
    direction = _lower(payload.get("direction") or payload.get("direction_bias") or payload.get("candidate_direction"))
    position_side = _lower(payload.get("position_side") or payload.get("direction"))
    return {
        "event_id": event_id,
        "event_type": event_type,
        "symbol": _symbol(payload),
        "direction": direction if direction in {"long", "short", "neutral"} else "",
        "position_side": position_side if position_side in {"long", "short"} else "",
        "signal_id": _text(payload.get("signal_id")),
        "position_id": _text(payload.get("position_id")),
        "environment": environment,
        "broker_mode": broker_mode,
        "date": _market_date(payload),
        "bar_time_ms": _payload_bar_open_ms(payload),
        "script_tag": _text(payload.get("script_tag")),
        "strategy_version": _text(payload.get("strategy_version")),
        "timeframe_stack": _text(payload.get("timeframe_stack")),
        "tv_chart_url": _text(payload.get("tv_chart_url")),
        "status": "received",
        "route_target": "",
        "route_record_id": "",
        "error_msg": "",
        "payload": dict(payload),
        "extra": _base_extra(payload, event_id, event_type, api_received_at_ms=api_received_at_ms),
    }


def _find_event(pb: Any, event_id: str, environment: str, escape_filter: Callable[[Any], str]) -> dict[str, Any] | None:
    row = pb.get_first_record(
        TV_EVENT_COLLECTION,
        filter=(
            f'event_id = "{_escape(escape_filter, event_id)}" && '
            f'environment = "{_escape(escape_filter, environment)}"'
        ),
    )
    return dict(row) if isinstance(row, dict) else None


def _patch_event(pb: Any, event: dict[str, Any] | None, patch: dict[str, Any]) -> None:
    if not event or not event.get("id"):
        return
    try:
        pb.update_record(TV_EVENT_COLLECTION, str(event.get("id")), patch)
    except Exception:
        pass


def _event_extra_with_final_latency(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    api_received_at_ms: int,
    route_finished_at_ms: int,
) -> dict[str, Any]:
    return _extra_with_latency_trace(
        _as_object((event or {}).get("extra")),
        payload,
        api_received_at_ms=api_received_at_ms,
        pb_created_at_ms=_timestamp_ms((event or {}).get("created")),
        route_finished_at_ms=route_finished_at_ms,
    )


def _load_target(pb: Any, *, symbol: str, date: str, environment: str, escape_filter: Callable[[Any], str]) -> dict[str, Any] | None:
    row = pb.get_first_record(
        "ibkr_targets",
        filter=(
            f'symbol = "{_escape(escape_filter, symbol)}" && '
            f'date = "{_escape(escape_filter, date)}" && '
            f'environment = "{_escape(escape_filter, environment)}"'
        ),
    )
    return dict(row) if isinstance(row, dict) else None


def _is_same_day_tv_pre_alert_target(target: dict[str, Any] | None) -> bool:
    if not target:
        return False
    extra = _as_object(target.get("extra"))
    source = _lower(extra.get("source") or extra.get("activation_source"))
    if source not in {TRADINGVIEW_SOURCE, "tv", "tv_webhook", "webhook_tv"}:
        return False
    if _lower(extra.get("event_type")) == "pre_alert":
        return True
    return bool(_text(extra.get("first_tv_event_id") or extra.get("last_tv_event_id")))


def _load_today_targets(pb: Any, *, date: str, environment: str, escape_filter: Callable[[Any], str]) -> list[dict[str, Any]]:
    filter_expr = f'date = "{_escape(escape_filter, date)}" && environment = "{_escape(escape_filter, environment)}"'
    try:
        rows = pb.get_all_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", max_pages=20)
    except Exception:
        rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", per_page=200, page=1)
    return [dict(row) for row in (rows or []) if isinstance(row, dict)]


def _activation_created_text(payload: dict[str, Any]) -> str:
    explicit = _text(payload.get("us_time") or payload.get("time") or payload.get("timestamp"))
    if explicit:
        return explicit
    bar_ms = _payload_bar_open_ms(payload)
    if bar_ms > 0:
        try:
            return datetime.fromtimestamp(bar_ms / 1000.0, ET or timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def _target_activation_extra(
    existing_extra: dict[str, Any],
    payload: dict[str, Any],
    *,
    event_id: str,
) -> dict[str, Any]:
    bar_ms = _payload_bar_open_ms(payload)
    first_bar_ms = _int(existing_extra.get("first_bar_time_ms"), 0) or bar_ms
    result = {
        "activation_source": _text(existing_extra.get("activation_source")) or TRADINGVIEW_SOURCE,
        "first_tv_event_id": _text(existing_extra.get("first_tv_event_id")) or event_id,
        "first_bar_time_ms": first_bar_ms,
        "first_created": _text(existing_extra.get("first_created")) or _activation_created_text(payload),
        "last_tv_event_id": event_id,
        "last_bar_time_ms": bar_ms,
        "last_created": _activation_created_text(payload),
    }
    if _has_mtf_context(payload):
        result.update(
            {
                "mtf_last_status": _mtf_status(payload),
                "mtf_last_score": _mtf_score(payload),
                "mtf_last_block_reason": _mtf_block_reason(payload),
            }
        )
    return {key: value for key, value in result.items() if value not in ("", None)}


def _target_sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, int, str]:
    extra = _as_object(row.get("extra"))
    score = _float(row.get("score"), _float(extra.get("activity_score"), 0.0))
    quality = _float(extra.get("quality_score"), 0.0)
    dollar_volume = _float(extra.get("opening_dollar_volume_10m"), 0.0)
    rvol = _float(extra.get("relative_opening_volume"), 0.0)
    bar_ms = _int(row.get("bar_time_ms") or extra.get("bar_time_ms"), 0)
    return (-score, -quality, -dollar_volume, -rvol, bar_ms, _text(row.get("symbol")))


def _rerank_targets(
    pb: Any,
    *,
    date: str,
    environment: str,
    config_value: Callable[[str, str, str], str] | None,
    escape_filter: Callable[[Any], str],
) -> None:
    rows = _load_today_targets(pb, date=date, environment=environment, escape_filter=escape_filter)
    tv_rows = []
    for row in rows:
        extra = _as_object(row.get("extra"))
        if _lower(extra.get("source")) == TRADINGVIEW_SOURCE and _text(row.get("status")).lower() in {"candidate", "active"}:
            tv_rows.append(row)
    tv_rows.sort(key=_target_sort_key)
    max_active = _config_int(config_value, "tv_max_active_targets", 10, environment)
    max_same_dir = _config_int(config_value, "tv_max_same_direction_targets", 7, environment)
    direction_counts: dict[str, int] = {}
    active_count = 0
    for index, row in enumerate(tv_rows, start=1):
        symbol = _text(row.get("symbol")).upper()
        direction = _lower(row.get("direction_bias") or _as_object(row.get("extra")).get("direction_bias")) or "neutral"
        eligible_by_total = active_count < max_active if max_active > 0 else False
        eligible_by_dir = direction == "neutral" or max_same_dir <= 0 or direction_counts.get(direction, 0) < max_same_dir
        next_status = "active" if eligible_by_total and eligible_by_dir else "candidate"
        reason = "tv_activity_rank_active" if next_status == "active" else "tv_activity_rank_overflow"
        if next_status == "active":
            active_count += 1
            if direction != "neutral":
                direction_counts[direction] = direction_counts.get(direction, 0) + 1
        extra = {**_as_object(row.get("extra")), "activity_rank": index, "rank_reason": reason}
        if _text(row.get("status")) != next_status or _as_object(row.get("extra")).get("activity_rank") != index:
            try:
                pb.update_record("ibkr_targets", str(row.get("id")), {"status": next_status, "extra": extra})
            except Exception:
                pass


def _upsert_target(
    pb: Any,
    payload: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    config_value: Callable[[str, str, str], str] | None,
    escape_filter: Callable[[Any], str],
) -> dict[str, Any]:
    symbol = _symbol(payload)
    if not symbol:
        raise TvPrimaryError("missing_symbol")
    date = _market_date(payload)
    existing = _load_target(pb, symbol=symbol, date=date, environment=environment, escape_filter=escape_filter)
    existing_extra = _as_object((existing or {}).get("extra"))
    extra = {
        **existing_extra,
        **_base_extra(payload, event_id, event_type),
        "qualified": parse_boolean(payload.get("qualified"), True),
        "direction_bias": _direction_bias(payload),
        "opening_volume_10m": _float(payload.get("opening_volume_10m"), 0.0),
        "opening_dollar_volume_10m": _float(payload.get("opening_dollar_volume_10m"), 0.0),
        "relative_opening_volume": _float(payload.get("relative_opening_volume"), 0.0),
        "opening_range_atr_pct": _float(payload.get("opening_range_atr_pct"), 0.0),
        "gap_pct": _float(payload.get("gap_pct"), 0.0),
        **_target_activation_extra(existing_extra, payload, event_id=event_id),
    }
    direction_bias = extra["direction_bias"] if extra["direction_bias"] in {"long", "short", "neutral"} else ""
    record = {
        "symbol": symbol,
        "exchange": _text(payload.get("exchange")),
        "date": date,
        "direction_bias": direction_bias,
        "score": _float(payload.get("activity_score"), 0.0),
        "scan_reason": _text(payload.get("reason") or payload.get("setup") or "tv_pre_alert"),
        "status": "candidate",
        "us_time": _text(payload.get("us_time")),
        "cn_time": _text(payload.get("cn_time")),
        "bar_time_ms": _int(payload.get("bar_time_ms"), 0),
        "environment": environment,
        "extra": extra,
    }
    if existing and existing.get("id"):
        saved = pb.update_record("ibkr_targets", str(existing.get("id")), record)
    else:
        saved = pb.create_record("ibkr_targets", record)
    _rerank_targets(pb, date=date, environment=environment, config_value=config_value, escape_filter=escape_filter)
    refreshed = _load_target(pb, symbol=symbol, date=date, environment=environment, escape_filter=escape_filter)
    return dict(refreshed or saved or record)


def _entry_window_status(
    payload: dict[str, Any], *, config_value: Callable[[str, str, str], str] | None, environment: str) -> tuple[bool, str]:
    if not _config_bool(config_value, "tv_entry_window_enforce_enabled", True, environment):
        return True, "disabled"
    current = _event_time_hhmm(payload)
    primary_start = _parse_hhmm(_config(config_value, "tv_entry_primary_start", "09:40", environment), "09:40")
    primary_end = _parse_hhmm(_config(config_value, "tv_entry_primary_end", "11:30", environment), "11:30")
    quality_end = _parse_hhmm(_config(config_value, "tv_entry_quality_end", "14:30", environment), "14:30")
    if current < primary_start:
        return False, "outside_tv_entry_window"
    if current <= primary_end:
        return True, "primary"
    if current <= quality_end:
        return True, "quality"
    return False, "no_new_entry_after"


def _route_entry(
    pb: Any,
    payload: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    broker_mode: str,
    config_value: Callable[[str, str, str], str] | None,
    escape_filter: Callable[[Any], str],
    build_signal_ingest_response: Callable[..., tuple[dict[str, Any], int]],
    normalize_environment: Callable[[Any, str], str],
    send_interactive: Callable[..., Any] | None,
    update_interactive: Callable[..., Any] | None,
    signal_chat_id_fn: Callable[[str], str] | None,
    console_base_url: str,
) -> tuple[dict[str, Any], int]:
    symbol = _symbol(payload)
    direction = _lower(payload.get("direction") or payload.get("position_side"))
    if not symbol:
        raise TvPrimaryError("missing_symbol")
    if direction not in {"long", "short"}:
        raise TvPrimaryError("invalid_direction")
    entry_price = _entry_price(payload)
    stop_loss = _float(payload.get("stop_loss") or payload.get("sl"), 0.0)
    take_profit = _float(payload.get("take_profit") or payload.get("tp"), 0.0)
    if entry_price <= 0:
        raise TvPrimaryError("invalid_entry")
    if stop_loss <= 0:
        raise TvPrimaryError("invalid_stop_loss")
    if take_profit <= 0:
        raise TvPrimaryError("invalid_take_profit")

    if _mtf_status(payload) == "block":
        return {
            "ok": False,
            "rejected": True,
            "reason": "mtf_blocked",
            "target": "",
            "id": "",
            "mtf": _mtf_payload(payload),
            "mtf_block_reason": _mtf_block_reason(payload),
        }, 200

    window_ok, window = _entry_window_status(payload, config_value=config_value, environment=broker_mode)
    if not window_ok:
        raise TvPrimaryError(window, 200)

    date = _market_date(payload)
    target = _load_target(pb, symbol=symbol, date=date, environment=environment, escape_filter=escape_filter)
    authorized_symbols, authorized_source = _authorized_symbol_universe(pb, config_value=config_value, environment=environment)
    requires_authorized_symbol = _config_bool(config_value, "tv_entry_requires_authorized_symbol", True, environment)
    authorized_symbol = symbol in authorized_symbols if authorized_symbols else None
    if requires_authorized_symbol and authorized_symbols and not authorized_symbol:
        raise TvPrimaryError("symbol_not_authorized_for_tv_entry", 200)
    has_same_day_tv_target = _is_same_day_tv_pre_alert_target(target)
    admission_reason = ""
    if has_same_day_tv_target:
        admission_reason = "same_day_tv_pre_alert_target"
    elif requires_authorized_symbol and authorized_symbol:
        admission_reason = "authorized_symbol"
    else:
        # Compatibility fallback for deployments that have not populated a TV universe yet.
        requires_target = _config_bool(config_value, "tv_entry_requires_active_target", True, environment)
        allow_self_activate = _config_bool(config_value, "tv_entry_allow_self_activate", True, environment)
        if requires_target and _text((target or {}).get("status")).lower() != "active":
            if allow_self_activate and bool(payload.get("qualified", False)):
                target = _upsert_target(
                    pb,
                    {**payload, "event_type": "pre_alert", "direction_bias": direction, "activity_score": payload.get("activity_score", 0)},
                    event_id=f"{event_id}:self_activate",
                    event_type="pre_alert",
                    environment=environment,
                    config_value=config_value,
                    escape_filter=escape_filter,
                )
            if _text((target or {}).get("status")).lower() != "active":
                raise TvPrimaryError("target_not_active_by_activity_rank", 200)
        admission_reason = "legacy_active_target" if requires_target else "legacy_target_check_disabled"

    target_extra = _as_object((target or {}).get("extra"))
    if window == "quality":
        rank_gate_enabled = _config_bool(config_value, "tv_quality_window_rank_enforce_enabled", False, environment)
        min_activity = _float(_config(config_value, "tv_quality_window_min_activity_score", "80", environment), 80.0)
        min_quality = _float(_config(config_value, "tv_quality_window_min_signal_quality_score", "85", environment), 85.0)
        if rank_gate_enabled and _int(target_extra.get("activity_rank"), 999999) > _config_int(config_value, "tv_quality_window_max_rank", 5, environment):
            raise TvPrimaryError("quality_window_rank_too_low", 200)
        if _float(payload.get("activity_score"), _float(target_extra.get("activity_score"), 0.0)) < min_activity:
            raise TvPrimaryError("activity_score_too_low_for_late_window", 200)
        if _float(payload.get("quality_score"), _float(target_extra.get("quality_score"), 0.0)) < min_quality:
            raise TvPrimaryError("quality_score_too_low_for_late_window", 200)

    signal_id = _text(payload.get("signal_id")) or event_id
    extra = {
        **_base_extra(payload, event_id, event_type),
        "execution_window": window,
        "admission_reason": admission_reason,
        "authorized_symbol": authorized_symbol,
        "authorized_symbol_source": authorized_source,
        "has_same_day_tv_target": has_same_day_tv_target,
        "activity_rank": target_extra.get("activity_rank"),
        "target_id": _text((target or {}).get("id")),
        "source": TRADINGVIEW_SOURCE,
        "signal_source": "tradingview_webhook",
    }
    signal_payload = {
        **payload,
        "source": "tv",
        "signal_source": "tv",
        "broker_mode": broker_mode,
        "market_data_mode": environment,
        "environment": environment,
        "symbol": symbol,
        "direction": direction,
        "signal": _text(payload.get("entry_setup") or payload.get("signal") or "tv_entry"),
        "signal_id": signal_id,
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "shares": _shares(payload),
        "date": _market_date(payload),
        "us_time": _text(payload.get("us_time")),
        "cn_time": _text(payload.get("cn_time")),
        "bar_time_ms": _int(payload.get("bar_time_ms"), 0),
        "script_tag": _text(payload.get("script_tag")),
        "chart_tf": _text(payload.get("chart_tf") or payload.get("entry_tf")),
        "reason": _text(payload.get("reason") or payload.get("setup")),
        "extra": extra,
    }
    return build_signal_ingest_response(
        pb,
        payload=signal_payload,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter,
        config_value=config_value,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        signal_chat_id_fn=signal_chat_id_fn,
        console_base_url=console_base_url,
    )


def _active_order_id(row: dict[str, Any]) -> str:
    return _text(row.get("broker_order_id") or row.get("order_id") or row.get("unique_id"))


def _resolve_child_orders(pb: Any, *, signal_id: str, environment: str, escape_filter: Callable[[Any], str]) -> dict[str, str]:
    if not signal_id:
        return {}
    try:
        rows = pb.get_records(
            "orders",
            filter=(
                f'signal_id = "{_escape(escape_filter, signal_id)}" && '
                f'environment = "{_escape(escape_filter, environment)}"'
            ),
            sort="-created",
            per_page=100,
            page=1,
        )
    except Exception:
        return {}
    result: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        role = _lower(row.get("role") or row.get("order_type"))
        status = _lower(row.get("status"))
        if status in {"filled", "closed", "cancelled", "canceled", "rejected", "expired"}:
            continue
        if role in {"stop_loss", "stoploss"} and not result.get("sl_order_id"):
            result["sl_order_id"] = _active_order_id(row)
        if role in {"take_profit", "takeprofit"} and not result.get("tp_order_id"):
            result["tp_order_id"] = _active_order_id(row)
    return result


def _route_reverse(
    pb: Any,
    payload: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    broker_mode: str,
    escape_filter: Callable[[Any], str],
) -> tuple[dict[str, Any], int]:
    symbol = _symbol(payload)
    if not symbol:
        raise TvPrimaryError("missing_symbol")
    side = _lower(payload.get("position_side") or payload.get("direction"))
    if side not in {"long", "short"}:
        raise TvPrimaryError("invalid_position_side")
    signal_id = _text(payload.get("signal_id"))
    extra = {
        **_base_extra(payload, event_id, event_type),
        "origin_signal_id": signal_id,
        "position_id": _text(payload.get("position_id")),
        "exit_reason": _text(payload.get("exit_reason")),
        "risk_update_reason": _text(payload.get("risk_update_reason") or payload.get("update_reason")),
        "risk_update_seq": _int(payload.get("risk_update_seq"), 0),
        "new_sl": _float(payload.get("new_stop_loss") or payload.get("new_sl"), 0.0),
        "new_tp": _float(payload.get("new_take_profit") or payload.get("new_tp"), 0.0),
        "previous_stop_loss": _float(payload.get("previous_stop_loss"), 0.0),
        "previous_take_profit": _float(payload.get("previous_take_profit"), 0.0),
    }
    action_type = "close" if event_type == "exit" else "adjust_bracket"
    if action_type == "adjust_bracket":
        extra.update(_resolve_child_orders(pb, signal_id=signal_id, environment=broker_mode, escape_filter=escape_filter))
    reverse_payload = {
        "symbol": symbol,
        "broker_mode": broker_mode,
        "environment": broker_mode,
        "data_environment": environment,
        "direction": side,
        "source": TRADINGVIEW_SOURCE,
        "priority": 9,
        "strength": "strong",
        "score": _float(payload.get("quality_score"), 100.0),
        "triggered_signals": [item for item in (signal_id, _text(payload.get("position_id")), event_id) if item],
        "action_type": action_type,
        "status": "pending",
        "reason": _text(payload.get("exit_reason") or payload.get("risk_update_reason") or event_type),
        "bar_time_ms": _int(payload.get("bar_time_ms"), 0),
        "us_time": _text(payload.get("us_time")),
        "cn_time": _text(payload.get("cn_time")),
        "extra": {**extra, "action_type": action_type, "reverse_kind": f"tv_{event_type}", "target_state": "tv_primary"},
        "dedupe": False if event_type == "risk_update" else True,
    }
    result = upsert_reverse_record(pb, reverse_payload, escape_filter=escape_filter)
    record = dict(result.get("record") or {}) if isinstance(result, dict) else {}
    return {
        "ok": True,
        "target": "ibkr_reverse_signals",
        "id": _text(record.get("id")),
        "action": "created" if result.get("created") else "updated",
        "status": _text(record.get("status") or "pending"),
        "event_type": event_type,
        "signal_id": signal_id,
    }, 200


def _spool_tv_event_payload(payload: dict[str, Any], *, event_id: str, event_type: str, environment: str, reason: str) -> dict[str, Any]:
    spool_root = os.environ.get(TV_WEBHOOK_SPOOL_DIR_ENV) or "/tmp/ibkr_tv_webhook_spool"
    os.makedirs(spool_root, exist_ok=True)
    safe_event_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in event_id)[:120] or "tv_event"
    filename = f"{int(time.time() * 1000)}_{safe_event_id}_{uuid.uuid4().hex}.json"
    final_path = os.path.join(spool_root, filename)
    temp_path = f"{final_path}.tmp"
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "environment": environment,
        "spooled_at_ms": _epoch_ms(),
        "reason": reason,
        "payload": dict(payload or {}),
    }
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, final_path)
    try:
        dir_fd = os.open(spool_root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        pass
    return {"path": final_path, "filename": filename}


def _dispatch_route_async(route_fn: Callable[[], None], route_executor: Callable[[Callable[[], None]], Any] | None = None) -> bool:
    try:
        if callable(route_executor):
            route_executor(route_fn)
        else:
            threading.Thread(target=route_fn, name="tv-primary-route", daemon=True).start()
        return True
    except Exception as exc:
        logger.warning("Failed to dispatch TV-primary async route: %s", exc)
        return False


def _route_persisted_tv_event(
    pb: Any,
    data: dict[str, Any],
    event: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    broker_mode: str,
    received_at_ms: int,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    build_signal_ingest_response: Callable[..., tuple[dict[str, Any], int]],
    config_value: Callable[[str, str, str], str] | None,
    send_interactive: Callable[..., Any] | None,
    update_interactive: Callable[..., Any] | None,
    signal_chat_id_fn: Callable[[str], str] | None,
    console_base_url: str,
) -> tuple[dict[str, Any], int]:
    try:
        if event_type == "heartbeat":
            route_payload, status_code = {"ok": True, "target": TV_EVENT_COLLECTION, "id": _text(event.get("id")), "event_type": event_type}, 200
        elif event_type == "pre_alert":
            target = _upsert_target(
                pb,
                data,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                config_value=config_value,
                escape_filter=escape_filter_string,
            )
            route_payload, status_code = {
                "ok": True,
                "target": "ibkr_targets",
                "id": _text(target.get("id")),
                "status": _text(target.get("status")),
                "event_type": event_type,
                "event_id": event_id,
            }, 200
        elif event_type == "entry":
            route_payload, status_code = _route_entry(
                pb,
                data,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                broker_mode=broker_mode,
                config_value=config_value,
                escape_filter=escape_filter_string,
                build_signal_ingest_response=build_signal_ingest_response,
                normalize_environment=normalize_environment,
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id_fn=signal_chat_id_fn,
                console_base_url=console_base_url,
            )
        else:
            route_payload, status_code = _route_reverse(
                pb,
                data,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                broker_mode=broker_mode,
                escape_filter=escape_filter_string,
            )
        event_status = "routed" if status_code < 400 and route_payload.get("ok") else "failed"
        if route_payload.get("error") or route_payload.get("reason") in {
            "outside_tv_entry_window",
            "no_new_entry_after",
            "target_not_active_by_activity_rank",
            "symbol_not_authorized_for_tv_entry",
            "mtf_blocked",
            "quality_window_rank_too_low",
            "activity_score_too_low_for_late_window",
            "quality_score_too_low_for_late_window",
        }:
            event_status = "rejected"
        route_finished_at_ms = _epoch_ms()
        _patch_event(
            pb,
            event,
            {
                "status": event_status,
                "route_target": _text(route_payload.get("target")),
                "route_record_id": _text(route_payload.get("id")),
                "error_msg": _text(route_payload.get("error") or route_payload.get("reason")),
                "extra": _event_extra_with_final_latency(
                    event,
                    data,
                    api_received_at_ms=received_at_ms,
                    route_finished_at_ms=route_finished_at_ms,
                ),
            },
        )
        return {**route_payload, "tv_event_id": event_id, "event_type": event_type}, status_code
    except TvPrimaryError as exc:
        route_finished_at_ms = _epoch_ms()
        _patch_event(
            pb,
            event,
            {
                "status": "rejected",
                "error_msg": exc.reason,
                "extra": _event_extra_with_final_latency(
                    event,
                    data,
                    api_received_at_ms=received_at_ms,
                    route_finished_at_ms=route_finished_at_ms,
                ),
            },
        )
        return {"ok": False, "rejected": True, "reason": exc.reason, "tv_event_id": event_id, "event_type": event_type}, exc.status_code
    except Exception as exc:
        route_finished_at_ms = _epoch_ms()
        _patch_event(
            pb,
            event,
            {
                "status": "failed",
                "error_msg": str(exc),
                "extra": _event_extra_with_final_latency(
                    event,
                    data,
                    api_received_at_ms=received_at_ms,
                    route_finished_at_ms=route_finished_at_ms,
                ),
            },
        )
        return {"ok": False, "error": str(exc), "tv_event_id": event_id, "event_type": event_type}, 500


def process_tv_primary_event(
    pb: Any,
    payload: dict[str, Any],
    *,
    api_received_at_ms: int | None = None,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    build_signal_ingest_response: Callable[..., tuple[dict[str, Any], int]],
    config_value: Callable[[str, str, str], str] | None = None,
    send_interactive: Callable[..., Any] | None = None,
    update_interactive: Callable[..., Any] | None = None,
    signal_chat_id_fn: Callable[[str], str] | None = None,
    console_base_url: str = "",
    async_route: bool = False,
    spool_on_persist_failure: bool = False,
    route_executor: Callable[[Callable[[], None]], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    received_at_ms = _int(api_received_at_ms, 0) or _epoch_ms()
    data = dict(payload or {})
    if not _source_is_tv(data):
        return {"ok": False, "error": "invalid_source", "source": data.get("source")}, 400
    event_type = _event_type(data)
    if event_type not in TV_EVENT_TYPES:
        return {"ok": False, "error": "unsupported_tv_event_type", "event_type": event_type}, 400
    broker_mode = request_broker_mode(data)
    environment = request_market_data_mode(data)
    event_id = _event_id(data, event_type=event_type)

    existing = _find_event(pb, event_id, environment, escape_filter_string)
    if existing:
        route_status = _text(existing.get("status")) or "received"
        route_reason = _text(existing.get("error_msg")) or "duplicate_tv_event"
        if async_route and route_status in {"received", "failed"}:
            existing_event = dict(existing)
            existing_payload = _as_object(existing_event.get("payload")) or data

            def route_existing() -> None:
                _route_persisted_tv_event(
                    pb,
                    existing_payload,
                    existing_event,
                    event_id=event_id,
                    event_type=event_type,
                    environment=environment,
                    broker_mode=broker_mode,
                    received_at_ms=received_at_ms,
                    normalize_environment=normalize_environment,
                    escape_filter_string=escape_filter_string,
                    build_signal_ingest_response=build_signal_ingest_response,
                    config_value=config_value,
                    send_interactive=send_interactive,
                    update_interactive=update_interactive,
                    signal_chat_id_fn=signal_chat_id_fn,
                    console_base_url=console_base_url,
                )

            _dispatch_route_async(route_existing, route_executor=route_executor)
        return {
            "ok": True,
            "skipped": True,
            "reason": "duplicate_tv_event",
            "status": "duplicate",
            "route_status": route_status,
            "route_reason": route_reason,
            "event_id": event_id,
            "event_type": event_type,
            "target": existing.get("route_target") or TV_EVENT_COLLECTION,
            "id": existing.get("route_record_id") or existing.get("id"),
        }, 200

    try:
        event = pb.create_record(
            TV_EVENT_COLLECTION,
            _event_record_payload(
                data,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                broker_mode=broker_mode,
                api_received_at_ms=received_at_ms,
            ),
        )
    except Exception as exc:
        if spool_on_persist_failure:
            try:
                spool = _spool_tv_event_payload(data, event_id=event_id, event_type=event_type, environment=environment, reason=str(exc))
                return {
                    "ok": True,
                    "accepted": True,
                    "queued": True,
                    "spooled": True,
                    "reason": "tv_event_spooled_after_persist_failure",
                    "spool_file": spool.get("filename"),
                    "tv_event_id": event_id,
                    "event_type": event_type,
                }, 202
            except Exception as spool_exc:
                return {
                    "ok": False,
                    "error": "tv_event_spool_failed",
                    "detail": str(spool_exc),
                    "persist_error": str(exc),
                    "retryable": True,
                    "tv_event_id": event_id,
                    "event_type": event_type,
                }, 503
        return {
            "ok": False,
            "error": "tv_event_persist_failed",
            "detail": str(exc),
            "retryable": True,
            "tv_event_id": event_id,
            "event_type": event_type,
        }, 503
    event = dict(event) if isinstance(event, dict) else {}
    if async_route:
        def route_current() -> None:
            _route_persisted_tv_event(
                pb,
                data,
                event,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                broker_mode=broker_mode,
                received_at_ms=received_at_ms,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                build_signal_ingest_response=build_signal_ingest_response,
                config_value=config_value,
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id_fn=signal_chat_id_fn,
                console_base_url=console_base_url,
            )

        dispatched = _dispatch_route_async(route_current, route_executor=route_executor)
        return {
            "ok": dispatched,
            "accepted": True,
            "queued": dispatched,
            "status": "received",
            "target": TV_EVENT_COLLECTION,
            "id": _text(event.get("id")),
            "tv_event_id": event_id,
            "event_type": event_type,
        }, 202 if dispatched else 503

    return _route_persisted_tv_event(
        pb,
        data,
        event,
        event_id=event_id,
        event_type=event_type,
        environment=environment,
        broker_mode=broker_mode,
        received_at_ms=received_at_ms,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
        build_signal_ingest_response=build_signal_ingest_response,
        config_value=config_value,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        signal_chat_id_fn=signal_chat_id_fn,
        console_base_url=console_base_url,
    )


__all__ = ["TV_EVENT_COLLECTION", "TV_EVENT_TYPES", "process_tv_primary_event"]
