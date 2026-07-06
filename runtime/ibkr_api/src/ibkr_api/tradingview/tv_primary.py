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
from ibkr_api.universe.maintenance import ensure_target_watchlist_record

try:
    from ibkr_compute.core.time_utils import ET
except Exception:  # pragma: no cover - imported in tests without compute sometimes
    ET = None  # type: ignore[assignment]

TV_EVENT_TYPES = {"pre_alert", "entry", "risk_update", "exit", "cancel", "heartbeat"}
TV_EVENT_COLLECTION = "tv_webhook_events"
TRADINGVIEW_SOURCE = "tradingview"
TV_WEBHOOK_SPOOL_DIR_ENV = "IBKR_TV_WEBHOOK_SPOOL_DIR"
TV_RUNTIME_WAKEUP_EVENT_TYPES = {"entry", "risk_update", "exit", "cancel"}
TV_RUNTIME_WAKEUP_TARGETS = {"ibkr_signals", "ibkr_reverse_signals"}
TV_PREMARKET_VALIDATION_MODES = {"premarket_linkage", "tv_premarket_linkage"}
TV_PREMARKET_VALIDATION_REJECTED_REASON = "premarket_validation_not_authorized"
ETF_ROTATION_TRADE_SYMBOLS = {"QQQ", "XLK", "SMH", "XLF", "XLE", "XLV", "XLY", "XLI"}
ETF_ROTATION_STRATEGY_GROUPS = {"etf_rotation", "etf_rotation_long_only"}
TV_REAL_ACTIVE_ORDER_STATUSES = {"presubmitted", "pre_submitted", "submitted", "submitted_waiting_fill"}
TV_REAL_FILLED_ORDER_STATUSES = {
    "executed",
    "filled",
    "partially_filled",
    "partiallyfilled",
    "protected_active",
    "filled_repricing_protection",
    "filled_position",
    "protection_reprice_failed",
}
TV_REAL_ORDER_STATUSES = TV_REAL_ACTIVE_ORDER_STATUSES | TV_REAL_FILLED_ORDER_STATUSES
TV_TERMINAL_ORDER_STATUSES = {
    "cancelled",
    "canceled",
    "closed",
    "entry_missed_limit_cap",
    "error",
    "expired",
    "failed",
    "inactive",
    "not_submitted",
    "rejected",
}

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


def _current_et_date() -> str:
    return _now_et().strftime("%Y-%m-%d")


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


def _target_watchlist_time_strings(payload: dict[str, Any], target: dict[str, Any] | None = None) -> dict[str, str]:
    target = target or {}
    return {
        "us": _text(payload.get("us_time") or target.get("us_time")),
        "cn": _text(payload.get("cn_time") or target.get("cn_time")),
        "date": _market_date(payload),
    }


def _sync_target_watchlist(
    pb: Any,
    payload: dict[str, Any],
    *,
    target: dict[str, Any] | None,
    environment: str,
    escape_filter: Callable[[Any], str],
) -> dict[str, Any]:
    target = dict(target or {})
    symbol = _symbol(payload) or _text(target.get("symbol")).upper()
    status = _text(target.get("status")).lower()
    if not symbol or status not in {"candidate", "active"}:
        return {"action": "skipped", "reason": "target_not_candidate_or_active"}
    market_date = _market_date(payload)
    if market_date != _current_et_date():
        return {"action": "skipped", "reason": "not_current_market_date", "date": market_date}
    try:
        return ensure_target_watchlist_record(
            pb,
            symbol=symbol,
            environment=environment,
            exchange=_text(payload.get("exchange") or target.get("exchange") or "SMART"),
            industry=_text(payload.get("industry") or payload.get("asset_class") or payload.get("description")),
            escape_filter_string=escape_filter,
            time_strings=lambda: _target_watchlist_time_strings(payload, target),
        )
    except Exception as exc:
        return {"action": "error", "error": str(exc)}


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
    bar_close_ms = _payload_bar_close_ms(payload)
    if bar_close_ms > 0:
        try:
            event_dt = datetime.fromtimestamp(bar_close_ms / 1000.0, ET or timezone.utc)
            return event_dt.hour, event_dt.minute
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


def _strategy_trade_symbol_universe(payload: dict[str, Any]) -> tuple[set[str], str]:
    strategy_group = _lower(payload.get("strategy_group"))
    script_tag = _lower(payload.get("script_tag"))
    trade_model = _lower(payload.get("trade_model"))
    if (
        strategy_group in ETF_ROTATION_STRATEGY_GROUPS
        or "etf_rotation" in script_tag
        or "etf_rotation" in trade_model
    ):
        return set(ETF_ROTATION_TRADE_SYMBOLS), "strategy_group:etf_rotation"
    return set(), ""


def _entry_authorized_symbol_universe(
    pb: Any,
    payload: dict[str, Any],
    *,
    config_value: Callable[[str, str, str], str] | None,
    environment: str,
) -> tuple[set[str], str]:
    configured_symbols, configured_source = _authorized_symbol_universe(
        pb,
        config_value=config_value,
        environment=environment,
    )
    strategy_symbols, strategy_source = _strategy_trade_symbol_universe(payload)
    if not strategy_symbols:
        return configured_symbols, configured_source
    if not configured_symbols:
        return strategy_symbols, strategy_source
    return configured_symbols | strategy_symbols, f"{configured_source}+{strategy_source}" if configured_source else strategy_source


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


def _payload_first(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    extra = _as_object(payload.get("extra"))
    for source in (payload, extra):
        for key in keys:
            if not isinstance(source, dict) or key not in source:
                continue
            value = source.get(key)
            if value not in (None, "", []):
                return value
    return default


def _payload_has_any(payload: dict[str, Any], *keys: str) -> bool:
    extra = _as_object(payload.get("extra"))
    for source in (payload, extra):
        for key in keys:
            if isinstance(source, dict) and key in source and source.get(key) not in (None, "", []):
                return True
    return False


def _payload_has_key(payload: dict[str, Any], *keys: str) -> bool:
    extra = _as_object(payload.get("extra"))
    for source in (payload, extra):
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                return True
    return False


def _payload_bool(payload: dict[str, Any], *keys: str, default: bool = False) -> bool:
    return parse_boolean(_payload_first(payload, *keys, default=default), default)


def _validation_run_id(payload: dict[str, Any]) -> str:
    return _text(_payload_first(payload, "validation_run_id", "run_id", "tv_validation_run_id", default=""))


def _validation_mode(payload: dict[str, Any]) -> str:
    return _lower(_payload_first(payload, "validation_mode", "tv_validation_mode", default=""))


def _premarket_validation_status(
    payload: dict[str, Any],
    *,
    config_value: Callable[[str, str, str], str] | None,
    broker_mode: str,
) -> dict[str, Any]:
    mode = _validation_mode(payload)
    run_id = _validation_run_id(payload)
    requested = mode in TV_PREMARKET_VALIDATION_MODES or bool(run_id)
    detail = {
        "requested": requested,
        "authorized": False,
        "mode": mode,
        "run_id": run_id,
        "reason": "not_requested",
    }
    if not requested:
        return detail
    if _lower(broker_mode) != "paper":
        detail["reason"] = "broker_mode_not_paper"
        return detail
    if mode not in TV_PREMARKET_VALIDATION_MODES:
        detail["reason"] = "invalid_validation_mode"
        return detail
    if not _payload_bool(payload, "paper_only", "validation_paper_only", default=False):
        detail["reason"] = "paper_only_required"
        return detail
    if not _config_bool(config_value, "tv_premarket_validation_enabled", False, "paper"):
        detail["reason"] = "config_disabled"
        return detail
    expected_run_id = _config(config_value, "tv_premarket_validation_run_id", "", "paper")
    if not expected_run_id or run_id != expected_run_id:
        detail["reason"] = "run_id_mismatch"
        detail["expected_run_id_configured"] = bool(expected_run_id)
        return detail
    configured_token = _config(config_value, "tv_premarket_validation_token", "", "paper")
    supplied_token = _text(_payload_first(payload, "validation_token", "tv_validation_token", default=""))
    if not configured_token or supplied_token != configured_token:
        detail["reason"] = "token_mismatch"
        detail["token_configured"] = bool(configured_token)
        return detail
    expires_at_ms = _int(_config(config_value, "tv_premarket_validation_expires_at_ms", "0", "paper"), 0)
    if expires_at_ms > 0 and _epoch_ms() > expires_at_ms:
        detail["reason"] = "expired"
        detail["expires_at_ms"] = expires_at_ms
        return detail
    detail["authorized"] = True
    detail["reason"] = "authorized"
    detail["expires_at_ms"] = expires_at_ms
    return detail


def _payload_text_first(payload: dict[str, Any], *keys: str, default: Any = "") -> str:
    return _text(_payload_first(payload, *keys, default=default))


def _trade_group_id(payload: dict[str, Any], default: str = "") -> str:
    return _payload_text_first(
        payload,
        "trade_group_id",
        "tradeGroupId",
        "trade_group",
        "bracket_group",
        "bracketGroup",
        default=default,
    )


def _origin_signal_id(payload: dict[str, Any], default: str = "") -> str:
    return _payload_text_first(
        payload,
        "origin_signal_id",
        "originSignalId",
        "entry_signal_id",
        "entrySignalId",
        "signal_id_orig",
        "signalIdOrig",
        "signal_id",
        "signalId",
        default=default,
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            value = parsed
        else:
            return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [_text(item) for item in value if _text(item)]
    if value in (None, ""):
        return []
    return [_text(value)]


def _normalize_requested_side(value: Any) -> str:
    text = _lower(value)
    aliases = {
        "sl": "stop_loss",
        "stop": "stop_loss",
        "stoploss": "stop_loss",
        "stop_loss": "stop_loss",
        "new_sl": "stop_loss",
        "new_stop_loss": "stop_loss",
        "tp": "take_profit",
        "takeprofit": "take_profit",
        "take_profit": "take_profit",
        "new_tp": "take_profit",
        "new_take_profit": "take_profit",
    }
    return aliases.get(text, "")


def _requested_sides(payload: dict[str, Any]) -> list[str]:
    raw = _payload_first(payload, "requested_sides", "requested_side", "adjust_sides", "adjust_side", default=None)
    sides: list[str] = []
    for item in _string_list(raw):
        side = _normalize_requested_side(item)
        if side and side not in sides:
            sides.append(side)
    return sides


def _risk_update_type(
    payload: dict[str, Any],
    *,
    reason: str,
    requested_sides: list[str],
    requested_sides_explicit: bool,
) -> str:
    explicit = _lower(_payload_first(payload, "risk_update_type", "update_type", default=""))
    if explicit:
        return explicit
    reason_key = _lower(reason)
    if "initial" in reason_key and "protection" in reason_key:
        return "initial_protection_bracket"
    if "runner" in reason_key and "activation" in reason_key:
        return "runner_activation"
    if "runner" in reason_key and ("trail" in reason_key or "stop" in reason_key):
        return "runner_trail_stop"
    if "breakeven" in reason_key or "break_even" in reason_key:
        return "breakeven_stop"
    has_sl = _payload_has_any(payload, "new_stop_loss", "new_sl")
    has_tp = _payload_has_any(payload, "new_take_profit", "new_tp")
    if requested_sides_explicit:
        side_set = set(requested_sides or [])
        has_sl = "stop_loss" in side_set
        has_tp = "take_profit" in side_set
    if has_sl and has_tp:
        return "bracket_update"
    if has_sl:
        return "stop_loss_update"
    if has_tp:
        return "take_profit_update"
    return reason_key or "risk_update"


def _runner_fields(payload: dict[str, Any], *, take_profit: float, event_type: str) -> dict[str, Any]:
    runner_enabled = parse_boolean(
        _payload_first(payload, "runner_enabled", "tp_checkpoint_runner", "enable_runner", "runner_mode", default=False),
        False,
    )
    runner_active_default = False if event_type == "entry" else False
    runner_active = parse_boolean(_payload_first(payload, "runner_active", "active_runner_mode", default=runner_active_default), runner_active_default)
    safety_take_profit = _float(
        _payload_first(payload, "safety_take_profit", "safety_tp", "runner_safety_tp", default=None),
        0.0,
    )
    runner_activation_price = _float(
        _payload_first(payload, "runner_activation_price", "runner_activation", "target_checkpoint", "tp_checkpoint", default=None),
        0.0,
    )
    runner_activation_r = _float(
        _payload_first(payload, "runner_activation_r", "runnerActivationR", "runner_activation_R", default=None),
        0.0,
    )
    target_checkpoint = _float(_payload_first(payload, "target_checkpoint", "tp_checkpoint", default=None), 0.0)
    target_role = _lower(_payload_first(payload, "target_role", default=""))
    if target_role not in {"hard_tp", "safety_tp"}:
        target_role = "safety_tp" if runner_enabled else "hard_tp"

    fields: dict[str, Any] = {
        "runner_enabled": runner_enabled,
        "runner_active": runner_active,
        "target_role": target_role,
        "target_is_hard": target_role == "hard_tp",
        "tp_checkpoint_runner": runner_enabled,
    }
    if safety_take_profit > 0:
        fields["safety_take_profit"] = safety_take_profit
    elif runner_enabled and take_profit > 0:
        fields["safety_take_profit"] = take_profit
    if runner_activation_price > 0:
        fields["runner_activation_price"] = runner_activation_price
    if runner_activation_r > 0:
        fields["runner_activation_r"] = runner_activation_r
    if target_checkpoint > 0:
        fields["target_checkpoint"] = target_checkpoint
    elif runner_enabled and runner_activation_price > 0:
        fields["target_checkpoint"] = runner_activation_price
    if runner_enabled:
        fields["target_checkpoint_is_exit"] = False
    elif "target_checkpoint" in fields:
        fields["target_checkpoint_is_exit"] = parse_boolean(
            _payload_first(payload, "target_checkpoint_is_exit", default=True),
            True,
        )
    return fields


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
    link_trade_group_id = _trade_group_id(payload)
    link_origin_signal_id = _origin_signal_id(payload)
    interval = _interval_text(payload)
    base = {
        **extra,
        "source": TRADINGVIEW_SOURCE,
        "tv_event_id": event_id,
        "event_type": event_type,
        "position_id": _text(payload.get("position_id")),
        "script_tag": _text(payload.get("script_tag")),
        "strategy_name": _text(payload.get("strategy_name") or extra.get("strategy_name")),
        "strategy_version": _text(payload.get("strategy_version")),
        "timeframe_stack": _text(payload.get("timeframe_stack") or extra.get("timeframe_stack")),
        "tv_chart_url": _text(payload.get("tv_chart_url")),
        "activity_score": activity_score,
        "quality_score": quality_score,
        "premarket_context": _as_object(payload.get("premarket_context")),
        "tv_snapshot": _as_object(payload.get("tv_snapshot")),
        "reason": _text(payload.get("reason") or extra.get("reason")),
    }
    for key in (
        "validation_mode",
        "validation_run_id",
        "paper_only",
        "outside_rth",
        "premarket_validation",
    ):
        value = _payload_first(payload, key)
        if value not in (None, "", []):
            base[key] = value
    if interval:
        base["interval"] = interval
    for key in (
        "pre_alert_stage",
        "activation_window",
        "activation_window_upper",
        "activation_window_lower",
        "entry_decides_direction",
        "qualified",
        "activity_grade",
        "missing_components",
        "lower_window_valid",
        "upper_window_valid",
    ):
        value = _payload_first(payload, key)
        if value not in (None, "", []):
            base[key] = value
    for key in (
        "atr",
        "atr_pct",
        "volatility_filter_enabled",
        "min_atr_pct_for_entry",
        "volatility_atr_pct",
        "volatility_entry_blocked",
        "volatility_entry_allowed",
        "profit_space_filter_enabled",
        "profit_space_entry_allowed",
        "profit_space_entry_blocked",
        "profit_space_filter_reason",
        "profit_space_basis",
        "entry_notional",
        "requested_exposure",
        "strategy_group",
        "max_daily_loss_dollars",
        "gross_risk",
        "planned_risk_dollars",
        "actual_risk_dollars",
        "expected_gross_profit",
        "expected_1r_dollars",
        "expected_2r_dollars",
        "estimated_round_trip_cost",
        "expected_net_profit",
        "expected_net_roi_pct",
        "cost_pct_of_reward",
        "structure_risk_per_share",
        "structure_reward_per_share",
        "structure_reward_risk",
        "gross_reward_risk",
        "net_reward_risk",
        "target_checkpoint_net_reward_risk",
        "risk_per_share",
        "reward_risk",
        "configured_reward_risk",
        "target_distance_atr",
        "profit_space_target_price",
        "profit_space_checkpoint_price",
        "checkpoint_gross_profit",
        "checkpoint_net_profit",
        "checkpoint_net_roi_pct",
        "checkpoint_cost_pct_of_reward",
        "checkpoint_target_distance_atr",
        "profit_space_final_target_price",
        "final_target_gross_profit",
        "final_target_net_profit",
        "final_target_net_roi_pct",
        "final_target_distance_atr",
        "min_net_profit_for_entry",
        "min_net_roi_pct_for_entry",
        "max_cost_pct_of_reward",
        "min_target_atr_multiple_for_entry",
        "min_structure_reward_risk_for_entry",
        "base_min_structure_reward_risk_for_entry",
        "weak_trend_min_structure_reward_risk_for_entry",
        "weak_trend_or_low_vol_context",
        "tp_sl_model",
        "rr_basis",
        "atr_role",
        "atr_buffer_mult",
        "atr_buffer_amount",
        "structure_stop_available",
        "structure_target_available",
        "target_is_structure",
        "risk_cap_stop_applied",
        "structure_stop_source",
        "structure_target_source",
        "structure_stop_loss",
        "structure_take_profit",
        "estimated_round_trip_fee",
        "estimated_slippage_per_share_per_side",
        "reference_entry",
        "planned_entry_price",
        "submitted_entry_limit_price",
        "submitted_limit_cap_price",
        "submitted_limit_cap_bps",
        "submitted_limit_cap_applied",
        "entry_anchor_mode",
        "entry_anchor_source",
        "entry_anchor_reason",
        "entry_anchor_distance_atr",
        "entry_anchor_distance_bps",
        "entry_order_ttl_bars",
        "entry_order_expires_bar_index",
        "entry_window_model",
        "entry_window_stage",
        "entry_cutoff_time",
        "entry_limit_intent",
        "entry_price_plan",
        "risk_model",
        "trade_model",
        "signal_reference_price",
        "entry_intent",
        "trial_risk_mode",
        "structure_clear",
        "structure_anchor_price",
        "expected_r",
        "trend_struct_dir",
        "trend_struct_pattern",
        "trend_anchor_price",
        "trend_support_sources",
        "trend_regime",
        "dominant_trend_side",
        "day_range_formed",
        "day_range_atr",
        "level_role",
        "exhaustion_side",
        "exhaustion_confirmed",
        "exhaustion_reason",
        "momentum_decay_ratio",
        "momentum_decay_streak",
        "momentum_decay_confirm_bars",
        "attack_volume_cooling",
        "attack_volume_cooldown_bars",
        "structure_break_accepted",
        "structure_break_accept_bars",
        "prior_level_distance_atr",
        "middle_retrace_ratio",
        "right_retest_ratio",
        "any_plan_gap_bars",
        "any_plan_distance_atr",
        "profit_protect_reason",
        "probe_pattern",
        "probe_filter_reason",
        "probe_anchor_price",
        "probe_anchor_age_bars",
        "probe_pullback_atr",
        "probe_retest_gap_bars",
        "visual_left_leg_bps",
        "visual_middle_retrace_ratio",
        "visual_right_retest_ratio",
        "volume_ratio",
        "volume_reason",
        "close_position",
        "volume_attack_up",
        "volume_attack_down",
        "today_struct_high",
        "today_struct_low",
        "prediction_level",
        "prediction_zone_low",
        "prediction_zone_high",
        "invalidation_price",
        "invalidation_source",
        "small_stop_gate_passed",
        "small_stop_gate_reason",
        "stop_distance_atr",
        "stop_distance_bps",
        "max_trial_risk_dollars",
        "main_reason",
        "secondary_reason",
        "plan_replace_reason",
        "cancel_reason",
        "cancel_scope",
        "cancel_policy",
        "tv_position_filled",
        "scale_plan_enabled",
        "scale_plan_version",
        "plan_type",
        "scale_plan_type",
        "plan_id",
        "scale_plan_id",
        "leg_index",
        "scale_leg_index",
        "leg_count",
        "leg_role",
        "leg_trigger",
        "leg_trade_group_id",
        "max_leg_notional",
        "max_leg_risk",
        "max_plan_risk",
        "independent_legs",
        "cross_leg_protection_sync",
        "aggregate_position_management",
        "leg_order_mode",
        "leg2_requires_leg1_protected",
        "scale_plan_risk_model",
        "plan_risk_budget_used",
        "plan_risk_budget_ok",
        "chart_symbol",
        "chart_symbol_rank",
        "chart_symbol_score",
        "chart_symbol_in_top3",
        "rank_1_symbol",
        "rank_1_score",
        "rank_2_symbol",
        "rank_2_score",
        "rank_3_symbol",
        "rank_3_score",
        "spy_vwap_ok",
        "spy_chop_block",
        "spy_vwap_cross_count_6",
        "spy_change_pct",
        "chart_change_pct",
        "orh",
        "orl",
        "vwap",
        "ema20_5m",
        "rvol",
        "candidate_active",
        "candidate_age_bars",
        "candidate_rank_at_birth",
        "candidate_score_at_birth",
        "candidate_wait_reason",
        "candidate_support",
        "candidate_stop",
        "runner_mode",
        "diagnostic_type",
        "debug_reason",
        "candidate_block_reason",
        "entry_block_reason",
        "gate_is_5m",
        "gate_chart_in_pool",
        "gate_chart_top3",
        "gate_score_ok",
        "gate_spy_ok",
        "gate_entry_window_open",
        "gate_candidate_window_open",
        "gate_manual_event_block",
        "gate_daily_loss_stop",
        "gate_trade_count_stop",
        "gate_loss_count_stop",
        "gate_slot_open",
        "gate_orh_broken_today",
        "gate_pullback_held",
        "gate_candidate_stop_ok",
        "gate_candidate_rank_ok",
        "gate_candidate_fresh",
        "gate_reconfirm_now",
        "gate_entry_stop_ok",
        "gate_entry_qty_ok",
        "gate_pullback_candidate_now",
        "gate_entry_signal",
        "debug_raw_candidate_stop_pct",
        "debug_entry_stop_pct",
        "debug_entry_risk_dollars",
        "debug_close_location",
    ):
        value = _payload_first(payload, key)
        if value not in (None, "", []):
            base[key] = value
    if link_trade_group_id:
        base["trade_group_id"] = link_trade_group_id
        base["bracket_group"] = link_trade_group_id
    if link_origin_signal_id and event_type in {"risk_update", "exit", "cancel"}:
        base["origin_signal_id"] = link_origin_signal_id
    for key, aliases in {
        "entry_order_unique_id": ("entry_order_unique_id", "entryOrderUniqueId", "entry_coid", "entryCoid"),
        "tp_order_unique_id": ("tp_order_unique_id", "take_profit_order_unique_id", "tp_coid", "tpCoid"),
        "sl_order_unique_id": ("sl_order_unique_id", "stop_loss_order_unique_id", "sl_coid", "slCoid"),
        "entry_coid": ("entry_coid", "entryCoid", "entry_order_unique_id", "entryOrderUniqueId"),
        "tp_coid": ("tp_coid", "tpCoid", "tp_order_unique_id", "take_profit_order_unique_id"),
        "sl_coid": ("sl_coid", "slCoid", "sl_order_unique_id", "stop_loss_order_unique_id"),
    }.items():
        value = _payload_text_first(payload, *aliases)
        if value:
            base[key] = value
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


def _target_extra_has_entry_activation(extra: dict[str, Any] | None) -> bool:
    payload = _as_object(extra)
    strategy_policy = payload.get("strategy_policy") if isinstance(payload.get("strategy_policy"), dict) else {}
    setup_type = _lower(strategy_policy.get("setup_type"))
    return bool(
        _lower(payload.get("event_type")) == "entry"
        or parse_boolean(payload.get("entry_backfilled_target"), False)
        or _text(payload.get("entry_signal_id"))
        or _lower(payload.get("target_admission_reason")) == "entry_signal_backfill"
        or setup_type == "tradingview_entry_backfill"
    )


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
    for index, row in enumerate(tv_rows, start=1):
        row_extra = _as_object(row.get("extra"))
        entry_activated = _target_extra_has_entry_activation(row_extra)
        next_status = "active" if _text(row.get("status")).lower() == "active" and entry_activated else "candidate"
        reason = "tv_entry_active" if next_status == "active" else "tv_candidate_rank"
        extra = {**row_extra, "activity_rank": index, "rank_reason": reason}
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
    existing_status = _text((existing or {}).get("status")).lower()
    keep_active = existing_status == "active" and _target_extra_has_entry_activation(existing_extra)
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
        "status": "active" if keep_active else "candidate",
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
    target = dict(refreshed or saved or record)
    target["watchlist_sync"] = _sync_target_watchlist(
        pb,
        payload,
        target=target,
        environment=environment,
        escape_filter=escape_filter,
    )
    return target


def _ensure_entry_backfill_target(
    pb: Any,
    payload: dict[str, Any],
    *,
    event_id: str,
    signal_id: str,
    direction: str,
    environment: str,
    escape_filter: Callable[[Any], str],
    existing_target: dict[str, Any] | None,
) -> dict[str, Any]:
    symbol = _symbol(payload)
    date = _market_date(payload)
    existing = dict(existing_target or {})
    existing_status = _text(existing.get("status")).lower()
    existing_id = _text(existing.get("id"))
    existing_extra = _as_object(existing.get("extra"))
    existing_entry_active = existing_status == "active" and _target_extra_has_entry_activation(existing_extra)
    if existing_id and existing_entry_active:
        watchlist_sync = _sync_target_watchlist(
            pb,
            payload,
            target=existing,
            environment=environment,
            escape_filter=escape_filter,
        )
        return {
            "ok": True,
            "action": "unchanged",
            "reason": "active_target_exists",
            "target_id": existing_id,
            "status": existing_status,
            "previous_status": existing_status,
            "watchlist_sync": watchlist_sync,
            "target": existing,
        }
    if existing_id and existing_status not in {"", "candidate", "active"}:
        return {
            "ok": True,
            "action": "skipped",
            "reason": "target_status_not_backfilled",
            "target_id": existing_id,
            "status": existing_status,
            "previous_status": existing_status,
            "target": existing,
        }

    previous_source = _text(existing_extra.get("source"))
    if not existing_id:
        backfill_reason = "missing_pre_alert_or_candidate"
    elif existing_status == "active":
        backfill_reason = "active_entry_upgrade"
    else:
        backfill_reason = "candidate_entry_upgrade"
    activity_score = _float(payload.get("activity_score"), _float(existing.get("score"), 0.0))
    direction_bias = direction if direction in {"long", "short"} else _direction_bias(payload)
    allowed_sides = [direction_bias] if direction_bias in {"long", "short"} else []
    extra = {
        **existing_extra,
        **_base_extra(payload, event_id, "entry"),
        **_target_activation_extra(existing_extra, payload, event_id=event_id),
        "source": TRADINGVIEW_SOURCE,
        "previous_source": previous_source if previous_source and previous_source != TRADINGVIEW_SOURCE else "",
        "entry_backfilled_target": True,
        "entry_backfill_reason": backfill_reason,
        "entry_backfill_previous_status": existing_status,
        "entry_signal_id": signal_id,
        "target_admission_reason": "entry_signal_backfill",
        "active_gate_passed": True,
        "context_active": True,
        "context_gate_passed": True,
        "context_allowed_sides": allowed_sides,
        "direction_bias": direction_bias,
        "strategy_policy": {
            **(existing_extra.get("strategy_policy") if isinstance(existing_extra.get("strategy_policy"), dict) else {}),
            "setup_type": "tradingview_entry_backfill",
            "allowed_sides": allowed_sides,
            "avoid_new_entries": False,
        },
    }
    extra = {key: value for key, value in extra.items() if value not in ("", None)}
    record = {
        "symbol": symbol,
        "exchange": _text(payload.get("exchange") or existing.get("exchange")),
        "date": date,
        "direction_bias": direction_bias,
        "score": activity_score,
        "scan_reason": _text(payload.get("reason") or payload.get("setup") or existing.get("scan_reason") or "tv_entry_backfill"),
        "status": "active",
        "us_time": _text(payload.get("us_time") or existing.get("us_time")),
        "cn_time": _text(payload.get("cn_time") or existing.get("cn_time")),
        "bar_time_ms": _payload_bar_open_ms(payload) or _int(existing.get("bar_time_ms"), 0),
        "environment": environment,
        "extra": extra,
    }
    if existing_id:
        saved = pb.update_record("ibkr_targets", existing_id, record)
        action = "updated"
    else:
        saved = pb.create_record("ibkr_targets", record)
        action = "created"
    refreshed = _load_target(pb, symbol=symbol, date=date, environment=environment, escape_filter=escape_filter)
    target = dict(refreshed or saved or record)
    watchlist_sync = _sync_target_watchlist(
        pb,
        payload,
        target=target,
        environment=environment,
        escape_filter=escape_filter,
    )
    return {
        "ok": True,
        "action": action,
        "reason": backfill_reason,
        "target_id": _text(target.get("id")),
        "status": _text(target.get("status")),
        "previous_status": existing_status,
        "watchlist_sync": watchlist_sync,
        "target": target,
    }


def _entry_window_status(
    payload: dict[str, Any],
    *,
    config_value: Callable[[str, str, str], str] | None,
    environment: str,
    validation_status: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if not _config_bool(config_value, "tv_entry_window_enforce_enabled", True, environment):
        return True, "disabled"
    current = _event_time_hhmm(payload)
    primary_start = _parse_hhmm(_config(config_value, "tv_entry_primary_start", "09:45", environment), "09:45")
    primary_end = _parse_hhmm(_config(config_value, "tv_entry_primary_end", "11:30", environment), "11:30")
    closing_start = _parse_hhmm(_config(config_value, "tv_entry_closing_start", "14:00", environment), "14:00")
    quality_end = _parse_hhmm(_config(config_value, "tv_entry_quality_end", "15:15", environment), "15:15")
    if current < primary_start:
        validation = validation_status or {}
        if validation.get("authorized"):
            return True, "premarket_validation"
        if validation.get("requested"):
            return False, TV_PREMARKET_VALIDATION_REJECTED_REASON
        return False, "outside_tv_entry_window"
    if current <= primary_end:
        return True, "primary"
    if current < closing_start:
        return True, "quality"
    if current <= quality_end:
        return True, "closing_quality"
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
    strategy_capacity_getter: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    symbol = _symbol(payload)
    direction = _lower(payload.get("direction") or payload.get("position_side"))
    if not symbol:
        raise TvPrimaryError("missing_symbol")
    if direction not in {"long", "short"}:
        raise TvPrimaryError("invalid_direction")
    entry_price = _entry_price(payload)
    stop_loss = _float(payload.get("stop_loss") or payload.get("sl"), 0.0)
    raw_take_profit = _float(payload.get("take_profit") or payload.get("tp"), 0.0)
    raw_runner_enabled = parse_boolean(
        _payload_first(payload, "runner_enabled", "tp_checkpoint_runner", "enable_runner", "runner_mode", default=False),
        False,
    )
    safety_take_profit = _float(
        _payload_first(payload, "safety_take_profit", "safety_tp", "runner_safety_tp", default=None),
        0.0,
    )
    take_profit = safety_take_profit if raw_runner_enabled and safety_take_profit > 0 else raw_take_profit
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

    validation_status = _premarket_validation_status(payload, config_value=config_value, broker_mode=broker_mode)
    window_ok, window = _entry_window_status(
        payload,
        config_value=config_value,
        environment=broker_mode,
        validation_status=validation_status,
    )
    if not window_ok:
        raise TvPrimaryError(window, 200)
    validation_authorized = bool(validation_status.get("authorized"))

    date = _market_date(payload)
    target = _load_target(pb, symbol=symbol, date=date, environment=environment, escape_filter=escape_filter)
    authorized_symbols, authorized_source = _entry_authorized_symbol_universe(
        pb,
        payload,
        config_value=config_value,
        environment=environment,
    )
    requires_authorized_symbol = _config_bool(config_value, "tv_entry_requires_authorized_symbol", True, environment)
    authorized_symbol = symbol in authorized_symbols if authorized_symbols else None
    if not validation_authorized and requires_authorized_symbol and authorized_symbols and not authorized_symbol:
        raise TvPrimaryError("symbol_not_authorized_for_tv_entry", 200)
    has_same_day_tv_target = _is_same_day_tv_pre_alert_target(target)
    admission_reason = ""
    if validation_authorized:
        admission_reason = "premarket_validation"
    elif has_same_day_tv_target:
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
                admission_reason = "self_activated_entry"
            else:
                raise TvPrimaryError("target_not_active_by_activity_rank", 200)
        if not admission_reason:
            admission_reason = "legacy_active_target" if requires_target else "legacy_target_check_disabled"

    target_extra = _as_object((target or {}).get("extra"))
    if window in {"quality", "closing_quality"}:
        rank_gate_enabled = _config_bool(config_value, "tv_quality_window_rank_enforce_enabled", False, environment)
        min_activity_key = (
            "tv_closing_quality_window_min_activity_score"
            if window == "closing_quality"
            else "tv_quality_window_min_activity_score"
        )
        min_quality_key = (
            "tv_closing_quality_window_min_signal_quality_score"
            if window == "closing_quality"
            else "tv_quality_window_min_signal_quality_score"
        )
        min_activity_default = "90" if window == "closing_quality" else "80"
        min_quality_default = "90" if window == "closing_quality" else "85"
        min_activity = _float(_config(config_value, min_activity_key, min_activity_default, environment), _float(min_activity_default, 90.0))
        min_quality = _float(_config(config_value, min_quality_key, min_quality_default, environment), _float(min_quality_default, 90.0))
        if rank_gate_enabled and _int(target_extra.get("activity_rank"), 999999) > _config_int(config_value, "tv_quality_window_max_rank", 5, environment):
            raise TvPrimaryError("quality_window_rank_too_low", 200)
        if _float(payload.get("activity_score"), _float(target_extra.get("activity_score"), 0.0)) < min_activity:
            raise TvPrimaryError("activity_score_too_low_for_late_window", 200)
        if _float(payload.get("quality_score"), _float(target_extra.get("quality_score"), 0.0)) < min_quality:
            raise TvPrimaryError("quality_score_too_low_for_late_window", 200)

    signal_id = _text(payload.get("signal_id")) or event_id
    trade_group_id = _trade_group_id(payload, default=signal_id)
    target_backfill = (
        {"action": "skipped", "reason": "premarket_validation"}
        if validation_authorized
        else _ensure_entry_backfill_target(
            pb,
            payload,
            event_id=event_id,
            signal_id=signal_id,
            direction=direction,
            environment=environment,
            escape_filter=escape_filter,
            existing_target=target,
        )
    )
    if isinstance(target_backfill.get("target"), dict):
        target = dict(target_backfill["target"])
        target_extra = _as_object(target.get("extra"))
    target_backfill_meta = {key: value for key, value in target_backfill.items() if key != "target"}
    outside_rth = validation_authorized and _payload_bool(payload, "outside_rth", "outsideRth", default=True)
    extra = {
        **_base_extra(payload, event_id, event_type),
        "execution_window": window,
        "entry_window_model": _text(payload.get("entry_window_model")) or "continuous_layered_v1",
        "entry_window_stage": _text(payload.get("entry_window_stage")) or window,
        "entry_cutoff_time": _text(payload.get("entry_cutoff_time")) or _config(config_value, "tv_entry_quality_end", "15:15", broker_mode),
        "admission_reason": admission_reason,
        "authorized_symbol": authorized_symbol,
        "authorized_symbol_source": authorized_source,
        "has_same_day_tv_target": has_same_day_tv_target,
        "activity_rank": target_extra.get("activity_rank"),
        "target_id": _text((target or {}).get("id")),
        "target_backfilled": target_backfill_meta.get("action") in {"created", "updated"},
        "target_backfill": target_backfill_meta,
        "source": TRADINGVIEW_SOURCE,
        "signal_source": "tradingview_webhook",
        "trade_group_id": trade_group_id,
        "bracket_group": trade_group_id,
        "entry_order_linkage_policy": "tv_signal_id_trade_group",
        "take_profit": take_profit,
        "premarket_validation": validation_authorized,
        "premarket_validation_status": validation_status,
        "outside_rth": bool(outside_rth),
        **_runner_fields(payload, take_profit=take_profit, event_type=event_type),
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
        "trade_group_id": trade_group_id,
        "bracket_group": trade_group_id,
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "outside_rth": bool(outside_rth),
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
    response_payload, response_status = build_signal_ingest_response(
        pb,
        payload=signal_payload,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter,
        config_value=config_value,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        signal_chat_id_fn=signal_chat_id_fn,
        console_base_url=console_base_url,
        strategy_capacity_getter=strategy_capacity_getter,
    )
    if isinstance(response_payload, dict):
        response_payload = {**response_payload, "target_backfill": target_backfill_meta}
    return response_payload, response_status


def _active_order_id(row: dict[str, Any]) -> str:
    return _text(row.get("broker_order_id") or row.get("order_id") or row.get("unique_id"))


def _status_key(value: Any) -> str:
    return _lower(value)


def _order_status_key(row: dict[str, Any]) -> str:
    return _status_key(row.get("status") or row.get("order_status") or row.get("orderStatus") or row.get("state"))


def _order_role(row: dict[str, Any]) -> str:
    extra = _as_object(row.get("extra"))
    return _lower(row.get("role") or row.get("order_role") or row.get("order_type") or row.get("orderType") or extra.get("role"))


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _fetch_tv_related_orders(
    pb: Any,
    *,
    signal_id: str,
    trade_group_id: str,
    environment: str,
    escape_filter: Callable[[Any], str],
) -> list[dict[str, Any]]:
    filters: list[str] = []
    env_filter = _escape(escape_filter, environment)
    if signal_id:
        filters.append(
            f'signal_id = "{_escape(escape_filter, signal_id)}" && '
            f'environment = "{env_filter}"'
        )
    if trade_group_id:
        group_filter = _escape(escape_filter, trade_group_id)
        filters.extend(
            [
                f'trade_group_id = "{group_filter}" && environment = "{env_filter}"',
                f'bracket_group = "{group_filter}" && environment = "{env_filter}"',
                f'entry_order_unique_id = "{group_filter}" && environment = "{env_filter}"',
            ]
        )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for filter_expr in filters:
        try:
            records = pb.get_records("orders", filter=filter_expr, sort="-created", per_page=100, page=1)
        except Exception:
            records = []
        for row in records or []:
            if not isinstance(row, dict):
                continue
            key = _text(row.get("id") or row.get("broker_order_id") or row.get("order_id") or json.dumps(row, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(row))
    return rows


def _resolve_child_orders(
    pb: Any,
    *,
    signal_id: str,
    trade_group_id: str = "",
    environment: str,
    escape_filter: Callable[[Any], str],
) -> dict[str, str]:
    rows = _fetch_tv_related_orders(
        pb,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        environment=environment,
        escape_filter=escape_filter,
    )
    result: dict[str, str] = {}
    for row in rows:
        role = _order_role(row)
        status = _order_status_key(row)
        if status not in TV_REAL_ACTIVE_ORDER_STATUSES:
            continue
        if role in {"stop_loss", "stoploss"} and not result.get("sl_order_id"):
            result["sl_order_id"] = _active_order_id(row)
        if role in {"take_profit", "takeprofit"} and not result.get("tp_order_id"):
            result["tp_order_id"] = _active_order_id(row)
    return result


def _origin_signal_record(
    pb: Any,
    *,
    signal_id: str,
    environment: str,
    broker_mode: str,
    escape_filter: Callable[[Any], str],
) -> dict[str, Any]:
    if not signal_id:
        return {}
    environments = _unique_text([environment, broker_mode])
    for candidate in environments:
        try:
            row = pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{_escape(escape_filter, signal_id)}" && '
                    f'environment = "{_escape(escape_filter, candidate)}"'
                ),
                sort="-created",
            )
        except Exception:
            row = None
        if isinstance(row, dict) and row.get("id"):
            return dict(row)
    return {}


def _origin_execution_payload(origin: dict[str, Any], *, environment: str, broker_mode: str) -> tuple[dict[str, Any], str]:
    extra = _as_object((origin or {}).get("extra"))
    by_mode = _as_object(extra.get("execution_by_mode"))
    for mode in _unique_text([broker_mode, environment, extra.get("last_ack_broker_mode"), extra.get("last_runtime_broker_mode")]):
        payload = _as_object(by_mode.get(mode))
        if payload:
            return payload, mode
    for mode, payload in by_mode.items():
        if isinstance(payload, dict):
            return dict(payload), _text(mode)
    return {}, ""


def _origin_status_key(origin: dict[str, Any], execution_payload: dict[str, Any]) -> str:
    extra = _as_object((origin or {}).get("extra"))
    return _status_key(
        (execution_payload or {}).get("status")
        or (execution_payload or {}).get("order_status")
        or (execution_payload or {}).get("orderStatus")
        or (execution_payload or {}).get("primary_order_status")
        or extra.get("last_execution_status")
        or extra.get("status")
        or (origin or {}).get("status")
    )


def _reverse_execution_preflight(
    pb: Any,
    *,
    event_type: str,
    signal_id: str,
    trade_group_id: str,
    environment: str,
    broker_mode: str,
    extra: dict[str, Any],
    escape_filter: Callable[[Any], str],
) -> dict[str, Any]:
    origin = _origin_signal_record(
        pb,
        signal_id=signal_id,
        environment=environment,
        broker_mode=broker_mode,
        escape_filter=escape_filter,
    )
    execution_payload, execution_mode = _origin_execution_payload(origin, environment=environment, broker_mode=broker_mode)
    origin_status = _origin_status_key(origin, execution_payload)
    related_orders = _fetch_tv_related_orders(
        pb,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        environment=broker_mode,
        escape_filter=escape_filter,
    )
    real_active_orders = [row for row in related_orders if _order_status_key(row) in TV_REAL_ACTIVE_ORDER_STATUSES]
    real_filled_orders = [row for row in related_orders if _order_status_key(row) in TV_REAL_FILLED_ORDER_STATUSES]
    active_child_order_ids = {
        key: value
        for key, value in {
            "sl_order_id": extra.get("sl_order_id"),
            "tp_order_id": extra.get("tp_order_id"),
        }.items()
        if _text(value)
    }
    origin_real = origin_status in TV_REAL_ORDER_STATUSES
    origin_filled = origin_status in TV_REAL_FILLED_ORDER_STATUSES
    origin_terminal = origin_status in TV_TERMINAL_ORDER_STATUSES
    action = "close" if event_type == "exit" else "adjust_bracket"
    ok = False
    reason = "ok"
    ack_status = "pending"
    order_linkage_status = ""

    if event_type == "exit":
        ok = bool(origin_filled or real_filled_orders)
        if not ok:
            if origin_terminal:
                reason = "tv_exit_origin_order_not_active"
                ack_status = "cancelled"
                order_linkage_status = "origin_order_not_active"
            else:
                reason = "real_filled_order_required_for_close"
                ack_status = "expired"
    else:
        ok = bool(active_child_order_ids or real_active_orders or origin_real)
        if not ok:
            if origin_terminal:
                reason = "risk_update_origin_order_not_active"
                ack_status = "cancelled"
                order_linkage_status = "origin_order_not_active"
            else:
                reason = "real_child_order_required_for_adjust"
                ack_status = "expired"

    return {
        "ok": ok,
        "action": action,
        "reason": reason,
        "ack_status": ack_status,
        "gateway_request_blocked": not ok,
        "real_order_required": True,
        "real_order_confirmed": bool(origin_real or real_active_orders or real_filled_orders),
        "filled_order_or_position_confirmed": bool(origin_filled or real_filled_orders),
        "broker_mode": broker_mode,
        "data_environment": environment,
        "origin_signal_id": signal_id,
        "origin_signal_found": bool(origin),
        "origin_execution_mode": execution_mode,
        "origin_execution_status": origin_status,
        "origin_execution_terminal": origin_terminal,
        "trade_group_id": trade_group_id,
        "order_ids": _unique_text([_active_order_id(row) for row in related_orders]),
        "resolved_child_order_ids": active_child_order_ids,
        "pb_order_count": len(related_orders),
        "pb_order_statuses": sorted({_order_status_key(row).upper() for row in related_orders if _order_status_key(row)}),
        "order_linkage_status": order_linkage_status,
    }


def _preflight_invalidated_extra(preflight: dict[str, Any]) -> dict[str, Any]:
    reason = _text(preflight.get("reason")) or "real_order_preflight_failed"
    return {
        "execution_preflight": dict(preflight),
        "result_status": "invalidated",
        "invalidated_by": "real_order_preflight",
        "invalidated_reason": reason,
        "gateway_request_blocked": True,
        "execution_readiness": "not_executable",
        "execution_blocked_reason": reason,
        "execution_blocked_message": (
            "Execution action invalidated at TV ingest because no confirmed real broker order/position "
            "was available for the requested follow-up action."
        ),
        "order_linkage_status": _text(preflight.get("order_linkage_status")),
    }


def _cancel_execution_plan(
    pb: Any,
    payload: dict[str, Any],
    *,
    signal_id: str,
    trade_group_id: str,
    environment: str,
    broker_mode: str,
    extra: dict[str, Any],
    escape_filter: Callable[[Any], str],
) -> dict[str, Any]:
    origin = _origin_signal_record(
        pb,
        signal_id=signal_id,
        environment=environment,
        broker_mode=broker_mode,
        escape_filter=escape_filter,
    )
    execution_payload, execution_mode = _origin_execution_payload(origin, environment=environment, broker_mode=broker_mode)
    origin_status = _origin_status_key(origin, execution_payload)
    related_orders = _fetch_tv_related_orders(
        pb,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        environment=broker_mode,
        escape_filter=escape_filter,
    )
    real_active_orders = [row for row in related_orders if _order_status_key(row) in TV_REAL_ACTIVE_ORDER_STATUSES]
    real_filled_orders = [row for row in related_orders if _order_status_key(row) in TV_REAL_FILLED_ORDER_STATUSES]
    real_terminal_orders = [row for row in related_orders if _order_status_key(row) in TV_TERMINAL_ORDER_STATUSES]
    origin_active = origin_status in TV_REAL_ACTIVE_ORDER_STATUSES
    origin_filled = origin_status in TV_REAL_FILLED_ORDER_STATUSES
    origin_terminal = origin_status in TV_TERMINAL_ORDER_STATUSES
    order_ids = _unique_text([_active_order_id(row) for row in related_orders])
    active_order_ids = _unique_text([_active_order_id(row) for row in real_active_orders])
    filled_order_ids = _unique_text([_active_order_id(row) for row in real_filled_orders])
    has_filled_state = bool(origin_filled or real_filled_orders)
    has_active_state = bool(origin_active or real_active_orders)
    status = "pending"
    action_type = ""
    target_state = ""
    reason = "ok"
    if has_filled_state:
        action_type = "close"
        target_state = "filled_position"
        reason = "cancel_after_filled_close_position"
    elif has_active_state:
        action_type = "cancel"
        target_state = "pending_entry"
        reason = "cancel_unfilled_entry"
    elif origin_terminal or real_terminal_orders:
        action_type = "cancel"
        target_state = "terminal"
        status = "cancelled"
        reason = "cancel_ignored_terminal"
    else:
        action_type = "cancel"
        target_state = "state_unknown"
        status = "cancelled"
        reason = "cancel_pending_state_unknown"

    return {
        "ok": status == "pending",
        "action": action_type,
        "status": status,
        "target_state": target_state,
        "reason": reason,
        "gateway_request_blocked": status != "pending",
        "real_order_required": True,
        "real_order_confirmed": bool(has_active_state or has_filled_state),
        "filled_order_or_position_confirmed": has_filled_state,
        "broker_mode": broker_mode,
        "data_environment": environment,
        "origin_signal_id": signal_id,
        "origin_signal_found": bool(origin),
        "origin_execution_mode": execution_mode,
        "origin_execution_status": origin_status,
        "origin_execution_terminal": origin_terminal,
        "trade_group_id": trade_group_id,
        "order_ids": order_ids,
        "active_order_ids": active_order_ids,
        "filled_order_ids": filled_order_ids,
        "pb_order_count": len(related_orders),
        "pb_order_statuses": sorted({_order_status_key(row).upper() for row in related_orders if _order_status_key(row)}),
        "resolved_child_order_ids": _resolve_child_orders(
            pb,
            signal_id=signal_id,
            trade_group_id=trade_group_id,
            environment=broker_mode,
            escape_filter=escape_filter,
        ),
        "cancel_reason": _text(_payload_first(payload, "cancel_reason", "reason", default="")),
        "cancel_scope": _text(_payload_first(payload, "cancel_scope", default="trade_intent")),
        "cancel_policy": _text(_payload_first(payload, "cancel_policy", default="cancel_unfilled_or_close_filled")),
        "entry_order_unique_id": _text(extra.get("entry_order_unique_id") or payload.get("entry_order_unique_id")),
    }


def _route_cancel(
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
    signal_id = _origin_signal_id(payload)
    trade_group_id = _trade_group_id(payload, default=signal_id)
    cancel_reason = _text(_payload_first(payload, "cancel_reason", "reason", default="tv_cancel"))
    extra = {
        **_base_extra(payload, event_id, event_type),
        "origin_signal_id": signal_id,
        "position_id": _text(payload.get("position_id")),
        "cancel_reason": cancel_reason,
        "cancel_scope": _text(_payload_first(payload, "cancel_scope", default="trade_intent")) or "trade_intent",
        "cancel_policy": _text(_payload_first(payload, "cancel_policy", default="cancel_unfilled_or_close_filled"))
        or "cancel_unfilled_or_close_filled",
    }
    if trade_group_id:
        extra["trade_group_id"] = trade_group_id
        extra["bracket_group"] = trade_group_id
    plan = _cancel_execution_plan(
        pb,
        payload,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        environment=environment,
        broker_mode=broker_mode,
        extra=extra,
        escape_filter=escape_filter,
    )
    action_type = _text(plan.get("action") or "cancel")
    reverse_status = _text(plan.get("status") or "cancelled")
    extra.update(
        {
            **(plan.get("resolved_child_order_ids") if isinstance(plan.get("resolved_child_order_ids"), dict) else {}),
            "execution_preflight": dict(plan),
            "action_type": action_type,
            "reverse_kind": "tv_cancel",
            "target_state": _text(plan.get("target_state")),
            "target_order_status": _text(plan.get("origin_execution_status")),
            "result_status": "pending" if reverse_status == "pending" else _text(plan.get("reason")),
            "gateway_request_blocked": bool(plan.get("gateway_request_blocked")),
            "execution_readiness": "executable" if reverse_status == "pending" else "not_executable",
            "execution_blocked_reason": "" if reverse_status == "pending" else _text(plan.get("reason")),
            "order_ids": list(plan.get("order_ids") or []),
            "active_order_ids": list(plan.get("active_order_ids") or []),
            "filled_order_ids": list(plan.get("filled_order_ids") or []),
        }
    )
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
        "triggered_signals": [item for item in (signal_id, trade_group_id, _text(payload.get("position_id")), event_id) if item],
        "action_type": action_type,
        "status": reverse_status,
        "reason": cancel_reason or _text(plan.get("reason")),
        "bar_time_ms": _int(payload.get("bar_time_ms"), 0),
        "us_time": _text(payload.get("us_time")),
        "cn_time": _text(payload.get("cn_time")),
        "extra": extra,
        "dedupe": True,
    }
    result = upsert_reverse_record(pb, reverse_payload, escape_filter=escape_filter)
    record = dict(result.get("record") or {}) if isinstance(result, dict) else {}
    return {
        "ok": True,
        "target": "ibkr_reverse_signals",
        "id": _text(record.get("id")),
        "action": "created" if result.get("created") else "updated",
        "status": _text(record.get("status") or reverse_status),
        "event_type": event_type,
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "cancel_action_type": action_type,
        "cancel_plan": plan,
    }, 200


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
    signal_id = _origin_signal_id(payload)
    trade_group_id = _trade_group_id(payload)
    exit_reason = _text(_payload_first(payload, "exit_reason", default=""))
    risk_update_reason = _text(_payload_first(payload, "risk_update_reason", "update_reason", default=""))
    extra = {
        **_base_extra(payload, event_id, event_type),
        "origin_signal_id": signal_id,
        "position_id": _text(payload.get("position_id")),
        **_runner_fields(payload, take_profit=_float(_payload_first(payload, "take_profit", "tp", default=0), 0.0), event_type=event_type),
    }
    if trade_group_id:
        extra["trade_group_id"] = trade_group_id
        extra["bracket_group"] = trade_group_id
    if event_type == "exit":
        extra.update(
            {
                "exit_reason": exit_reason,
                "exit_fill_role": _text(_payload_first(payload, "exit_fill_role", "fill_role", default="")),
            }
        )
    else:
        requested_sides = _requested_sides(payload)
        requested_sides_explicit = _payload_has_key(payload, "requested_sides", "requested_side", "adjust_sides", "adjust_side")
        extra.update(
            {
                "risk_update_reason": risk_update_reason,
                "risk_update_seq": _int(_payload_first(payload, "risk_update_seq", default=0), 0),
                "risk_update_type": _risk_update_type(
                    payload,
                    reason=risk_update_reason,
                    requested_sides=requested_sides,
                    requested_sides_explicit=requested_sides_explicit,
                ),
            }
        )
        if requested_sides_explicit:
            extra["requested_sides"] = requested_sides
        if _payload_has_any(payload, "new_stop_loss", "new_sl") and (
            not requested_sides_explicit or "stop_loss" in requested_sides
        ):
            extra["new_sl"] = _float(_payload_first(payload, "new_stop_loss", "new_sl"), 0.0)
        if _payload_has_any(payload, "new_take_profit", "new_tp") and (
            not requested_sides_explicit or "take_profit" in requested_sides
        ):
            extra["new_tp"] = _float(_payload_first(payload, "new_take_profit", "new_tp"), 0.0)
        if _payload_has_any(payload, "previous_stop_loss", "previous_sl", "current_stop_loss", "current_sl"):
            extra["previous_stop_loss"] = _float(
                _payload_first(payload, "previous_stop_loss", "previous_sl", "current_stop_loss", "current_sl"),
                0.0,
            )
        if _payload_has_any(payload, "previous_take_profit", "previous_tp", "current_take_profit", "current_tp"):
            extra["previous_take_profit"] = _float(
                _payload_first(payload, "previous_take_profit", "previous_tp", "current_take_profit", "current_tp"),
                0.0,
            )
    action_type = "close" if event_type == "exit" else "adjust_bracket"
    if action_type == "adjust_bracket":
        extra.update(
            _resolve_child_orders(
                pb,
                signal_id=signal_id,
                trade_group_id=trade_group_id,
                environment=broker_mode,
                escape_filter=escape_filter,
            )
        )
    preflight = _reverse_execution_preflight(
        pb,
        event_type=event_type,
        signal_id=signal_id,
        trade_group_id=trade_group_id,
        environment=environment,
        broker_mode=broker_mode,
        extra=extra,
        escape_filter=escape_filter,
    )
    reverse_status = "pending" if preflight.get("ok") else _text(preflight.get("ack_status") or "expired")
    reverse_reason = exit_reason or risk_update_reason or event_type
    if not preflight.get("ok"):
        extra.update(_preflight_invalidated_extra(preflight))
        reverse_reason = f"execution invalidated: {_text(preflight.get('reason')) or 'real_order_preflight_failed'}"
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
        "triggered_signals": [item for item in (signal_id, trade_group_id, _text(payload.get("position_id")), event_id) if item],
        "action_type": action_type,
        "status": reverse_status,
        "reason": reverse_reason,
        "bar_time_ms": _int(payload.get("bar_time_ms"), 0),
        "us_time": _text(payload.get("us_time")),
        "cn_time": _text(payload.get("cn_time")),
        "extra": {**extra, "action_type": action_type, "reverse_kind": f"tv_{event_type}", "target_state": "tv_primary"},
        "dedupe": event_type == "risk_update",
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


def _should_trigger_runtime_wakeup(event_type: str, route_payload: dict[str, Any], status_code: int) -> bool:
    return (
        event_type in TV_RUNTIME_WAKEUP_EVENT_TYPES
        and int(status_code or 0) < 400
        and isinstance(route_payload, dict)
        and bool(route_payload.get("ok"))
        and _text(route_payload.get("target")) in TV_RUNTIME_WAKEUP_TARGETS
    )


def _runtime_wakeup_request_payload(
    data: dict[str, Any],
    event: dict[str, Any],
    route_payload: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    environment: str,
    broker_mode: str,
    event_status: str,
    route_finished_at_ms: int,
) -> dict[str, Any]:
    return {
        "source": "tv_webhook",
        "reason": "tv_primary_routed",
        "tv_event_id": event_id,
        "tv_event_record_id": _text((event or {}).get("id")),
        "event_type": event_type,
        "symbol": _symbol(data),
        "signal_id": _text(route_payload.get("signal_id") or data.get("signal_id")),
        "route_target": _text(route_payload.get("target")),
        "route_record_id": _text(route_payload.get("id")),
        "route_status": event_status,
        "route_payload_status": _text(route_payload.get("status")),
        "environment": environment,
        "data_environment": environment,
        "broker_mode": broker_mode,
        "route_finished_at_ms": int(route_finished_at_ms or 0),
    }


def _call_runtime_wakeup(
    runtime_wakeup: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not callable(runtime_wakeup):
        return {}
    try:
        result = runtime_wakeup(dict(payload))
    except Exception as exc:
        logger.warning("TV-primary runtime signal wakeup failed: %s", exc)
        return {
            "ok": False,
            "woke": False,
            "reason": "runtime_wakeup_failed",
            "error": str(exc),
            "request": dict(payload),
        }
    if isinstance(result, dict):
        return {**result, "request": dict(payload)}
    return {"ok": bool(result), "woke": bool(result), "request": dict(payload)}


def _async_runtime_wakeup_diagnostic(
    event_type: str,
    runtime_wakeup: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    if event_type not in TV_RUNTIME_WAKEUP_EVENT_TYPES:
        return {}
    return {
        "enabled": callable(runtime_wakeup),
        "deferred": callable(runtime_wakeup),
        "trigger": "after_route_success",
    }


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
    strategy_capacity_getter: Callable[[str], dict[str, Any]] | None = None,
    runtime_wakeup: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        if event_type == "heartbeat":
            route_payload, status_code = {"ok": True, "target": TV_EVENT_COLLECTION, "id": _text(event.get("id")), "event_type": event_type}, 200
        elif event_type == "pre_alert":
            validation_status = _premarket_validation_status(data, config_value=config_value, broker_mode=broker_mode)
            if validation_status.get("authorized"):
                route_payload, status_code = {
                    "ok": True,
                    "target": TV_EVENT_COLLECTION,
                    "id": _text(event.get("id")),
                    "status": "validation_pre_alert_received",
                    "watchlist_sync": {"action": "skipped", "reason": "premarket_validation"},
                    "event_type": event_type,
                    "event_id": event_id,
                    "premarket_validation": True,
                    "premarket_validation_status": validation_status,
                }, 200
            else:
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
                    "watchlist_sync": target.get("watchlist_sync") if isinstance(target.get("watchlist_sync"), dict) else {},
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
                strategy_capacity_getter=strategy_capacity_getter,
            )
        elif event_type == "cancel":
            route_payload, status_code = _route_cancel(
                pb,
                data,
                event_id=event_id,
                event_type=event_type,
                environment=environment,
                broker_mode=broker_mode,
                escape_filter=escape_filter_string,
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
            TV_PREMARKET_VALIDATION_REJECTED_REASON,
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
        final_extra = _event_extra_with_final_latency(
            event,
            data,
            api_received_at_ms=received_at_ms,
            route_finished_at_ms=route_finished_at_ms,
        )
        response_payload = {**route_payload, "tv_event_id": event_id, "event_type": event_type}
        if _should_trigger_runtime_wakeup(event_type, route_payload, status_code):
            wakeup_result = _call_runtime_wakeup(
                runtime_wakeup,
                _runtime_wakeup_request_payload(
                    data,
                    event,
                    route_payload,
                    event_id=event_id,
                    event_type=event_type,
                    environment=environment,
                    broker_mode=broker_mode,
                    event_status=event_status,
                    route_finished_at_ms=route_finished_at_ms,
                ),
            )
            if wakeup_result:
                final_extra["runtime_wakeup"] = wakeup_result
                response_payload["runtime_wakeup"] = wakeup_result
        _patch_event(
            pb,
            event,
            {
                "status": event_status,
                "route_target": _text(route_payload.get("target")),
                "route_record_id": _text(route_payload.get("id")),
                "error_msg": _text(route_payload.get("error") or route_payload.get("reason")),
                "extra": final_extra,
            },
        )
        return response_payload, status_code
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
    strategy_capacity_getter: Callable[[str], dict[str, Any]] | None = None,
    runtime_wakeup: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
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
                    strategy_capacity_getter=strategy_capacity_getter,
                    runtime_wakeup=runtime_wakeup,
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
                strategy_capacity_getter=strategy_capacity_getter,
                runtime_wakeup=runtime_wakeup,
            )

        dispatched = _dispatch_route_async(route_current, route_executor=route_executor)
        response_payload = {
            "ok": dispatched,
            "accepted": True,
            "queued": dispatched,
            "status": "received",
            "target": TV_EVENT_COLLECTION,
            "id": _text(event.get("id")),
            "tv_event_id": event_id,
            "event_type": event_type,
        }
        wakeup_diagnostic = _async_runtime_wakeup_diagnostic(event_type, runtime_wakeup)
        if wakeup_diagnostic:
            response_payload["runtime_wakeup"] = wakeup_diagnostic
        return response_payload, 202 if dispatched else 503

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
        strategy_capacity_getter=strategy_capacity_getter,
        runtime_wakeup=runtime_wakeup,
    )


__all__ = ["TV_EVENT_COLLECTION", "TV_EVENT_TYPES", "process_tv_primary_event"]
