from __future__ import annotations

import json
from typing import Any, Iterable

from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval

DEFAULT_MARKET_SENTIMENT_SYMBOLS = ("VIX", "SPY", "QQQ")
DEFAULT_STALE_MINUTES = 20
DEFAULT_THRESHOLDS = {
    "vix_calm_max": 20.0,
    "vix_risk_off": 25.0,
    "vix_panic": 30.0,
    "vix_reversal_floor": 20.0,
    "vix_falling_delta": -0.05,
}


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _cfg_bool(api_app: Any, key: str, environment: str, default: bool) -> bool:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_bool_for_environment"):
        try:
            return bool(cfg.get_bool_for_environment(key, environment, default))
        except Exception:
            return bool(default)
    return bool(default)


def _cfg_float(api_app: Any, key: str, environment: str, default: float) -> float:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_float_for_environment"):
        try:
            return float(cfg.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    return float(default)


def _cfg_text(api_app: Any, key: str, environment: str, default: str) -> str:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_for_environment"):
        try:
            return str(cfg.get_for_environment(key, environment, default) or default)
        except Exception:
            return str(default)
    if cfg is not None and hasattr(cfg, "get"):
        try:
            return str(cfg.get(key, default) or default)
        except Exception:
            return str(default)
    return str(default)


def _cfg_symbols(api_app: Any, environment: str) -> tuple[str, ...]:
    raw = _cfg_text(
        api_app,
        "ibkr_market_sentiment_symbols",
        environment,
        ",".join(DEFAULT_MARKET_SENTIMENT_SYMBOLS),
    )
    symbols = tuple(
        str(item or "").strip().upper()
        for item in raw.split(",")
        if str(item or "").strip()
    )
    return symbols or DEFAULT_MARKET_SENTIMENT_SYMBOLS


def _direct_sqlite_timeout(api_app: Any, environment: str) -> float:
    fallback = _cfg_float(api_app, "ibkr_bar_direct_sqlite_timeout_sec", environment, 30.0)
    return max(
        0.5,
        _cfg_float(api_app, "ibkr_bar_direct_sqlite_read_timeout_sec", environment, fallback),
    )


def _environment_sql(environment: str, *, include_legacy_empty: bool = True) -> tuple[str, list[Any]]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    values: list[Any] = [runtime_environment]
    if include_legacy_empty and runtime_environment == "live":
        values.append("")
    if len(values) == 1:
        return "environment = ?", values
    placeholders = ", ".join("?" for _ in values)
    return f"environment IN ({placeholders})", values


def _fetch_indicator_rows(conn: Any, symbol: str, environment: str, chart_tf: str, safe_upper_ms: int) -> list[dict[str, Any]]:
    env_sql, env_params = _environment_sql(environment, include_legacy_empty=True)
    rows = conn.execute(
        f"""
        SELECT symbol, interval, us_time, cn_time, bar_time_ms, extra, environment, updated
        FROM ibkr_indicators
        WHERE symbol = ?
          AND interval = ?
          AND {env_sql}
          AND bar_time_ms <= ?
        ORDER BY bar_time_ms DESC
        LIMIT 2
        """,
        (str(symbol or "").strip().upper(), chart_tf, *env_params, int(safe_upper_ms or 0)),
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_bar_rows(conn: Any, symbol: str, environment: str, interval: str, safe_upper_ms: int) -> list[dict[str, Any]]:
    env_sql, env_params = _environment_sql(environment, include_legacy_empty=True)
    rows = conn.execute(
        f"""
        SELECT symbol, interval, us_time, cn_time, bar_time_ms, close, extra, environment, updated
        FROM ibkr_bars
        WHERE symbol = ?
          AND interval = ?
          AND {env_sql}
          AND bar_time_ms <= ?
        ORDER BY bar_time_ms DESC
        LIMIT 2
        """,
        (str(symbol or "").strip().upper(), normalize_interval(interval), *env_params, int(safe_upper_ms or 0)),
    ).fetchall()
    return [dict(row) for row in rows]


def _value_from_row(row: dict[str, Any], source: str) -> float | None:
    extra = _parse_json(row.get("extra"))
    if source == "bar":
        return _coerce_float(row.get("close"))
    return _coerce_float(
        extra.get("close")
        if extra.get("close") is not None
        else extra.get("last_price")
    )


def _change_pct_from_row(row: dict[str, Any]) -> float | None:
    extra = _parse_json(row.get("extra"))
    for key in ("day_change_pct", "change_pct", "pct_change", "change_1d_pct"):
        value = _coerce_float(extra.get(key))
        if value is not None:
            return value
    return None


def _snapshot_from_rows(
    *,
    symbol: str,
    source: str,
    rows: list[dict[str, Any]],
    signal_bar_time_ms: int,
    stale_ms: int,
) -> dict[str, Any] | None:
    if not rows:
        return None
    current = dict(rows[0])
    previous = dict(rows[1]) if len(rows) > 1 else {}
    value = _value_from_row(current, source)
    prev_value = _value_from_row(previous, source) if previous else None
    bar_ms = _coerce_int(current.get("bar_time_ms"))
    age_ms = max(0, int(signal_bar_time_ms or 0) - bar_ms) if bar_ms > 0 else 0
    value_delta = None
    value_delta_pct = None
    if value is not None and prev_value is not None:
        value_delta = round(value - prev_value, 4)
        if prev_value:
            value_delta_pct = round((value_delta / prev_value) * 100.0, 4)

    return {
        "symbol": str(symbol or "").strip().upper(),
        "value": round(value, 4) if value is not None else None,
        "change_pct": _change_pct_from_row(current),
        "source": source,
        "stale": bool(stale_ms > 0 and age_ms > stale_ms),
        "age_min": round(age_ms / 60000.0, 1) if bar_ms > 0 else None,
        "us_time": current.get("us_time", "") or "",
        "cn_time": current.get("cn_time", "") or "",
        "bar_time_ms": bar_ms,
        "prev_value": round(prev_value, 4) if prev_value is not None else None,
        "value_delta": value_delta,
        "value_delta_pct": value_delta_pct,
    }


def _missing_snapshot(symbol: str) -> dict[str, Any]:
    return {
        "symbol": str(symbol or "").strip().upper(),
        "value": None,
        "change_pct": None,
        "source": "missing",
        "stale": True,
        "age_min": None,
        "us_time": "",
        "cn_time": "",
        "bar_time_ms": 0,
        "prev_value": None,
        "value_delta": None,
        "value_delta_pct": None,
    }


def _choose_best_snapshot(indicator_snapshot: dict[str, Any] | None, bar_snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if indicator_snapshot and bar_snapshot:
        indicator_ms = _coerce_int(indicator_snapshot.get("bar_time_ms"))
        bar_ms = _coerce_int(bar_snapshot.get("bar_time_ms"))
        if bar_ms > indicator_ms:
            if bar_snapshot.get("change_pct") is None and indicator_snapshot.get("change_pct") is not None:
                bar_snapshot = {**bar_snapshot, "change_pct": indicator_snapshot.get("change_pct")}
            return bar_snapshot
        return indicator_snapshot
    return indicator_snapshot or bar_snapshot


def fetch_market_sentiment_snapshots(
    *,
    api_app: Any,
    environment: str,
    signal_bar_time_ms: int,
    interval: str = "5m",
    symbols: Iterable[str] = DEFAULT_MARKET_SENTIMENT_SYMBOLS,
) -> list[dict[str, Any]]:
    chart_tf = interval_to_chart_tf(interval)
    stale_min = _cfg_float(api_app, "ibkr_market_sentiment_stale_min", environment, DEFAULT_STALE_MINUTES)
    stale_ms = int(max(1.0, stale_min) * 60000)
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized_symbols:
        normalized_symbols = list(DEFAULT_MARKET_SENTIMENT_SYMBOLS)

    snapshots: list[dict[str, Any]] = []
    with open_pb_sqlite(readonly=True, timeout=_direct_sqlite_timeout(api_app, environment)) as conn:
        for symbol in normalized_symbols:
            indicator_rows = _fetch_indicator_rows(conn, symbol, environment, chart_tf, signal_bar_time_ms)
            bar_rows = _fetch_bar_rows(conn, symbol, environment, interval, signal_bar_time_ms)
            indicator_snapshot = _snapshot_from_rows(
                symbol=symbol,
                source="indicator",
                rows=indicator_rows,
                signal_bar_time_ms=signal_bar_time_ms,
                stale_ms=stale_ms,
            )
            bar_snapshot = _snapshot_from_rows(
                symbol=symbol,
                source="bar",
                rows=bar_rows,
                signal_bar_time_ms=signal_bar_time_ms,
                stale_ms=stale_ms,
            )
            snapshots.append(_choose_best_snapshot(indicator_snapshot, bar_snapshot) or _missing_snapshot(symbol))
    return snapshots


def _snapshot_map(snapshots: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("symbol") or "").strip().upper(): dict(item)
        for item in (snapshots or [])
        if str(item.get("symbol") or "").strip()
    }


def _pct(snapshot: dict[str, Any] | None) -> float | None:
    return _coerce_float((snapshot or {}).get("change_pct"))


def _value(snapshot: dict[str, Any] | None) -> float | None:
    return _coerce_float((snapshot or {}).get("value"))


def _is_usable(snapshot: dict[str, Any] | None) -> bool:
    return bool(snapshot) and not bool(snapshot.get("stale")) and _value(snapshot) is not None


def _direction_kind(signal_direction: str) -> str:
    direction = str(signal_direction or "").strip().lower()
    if direction in {"long", "buy", "bull", "bullish"}:
        return "long"
    if direction in {"short", "sell", "bear", "bearish"}:
        return "short"
    return ""


def _relation_for_sentiment(sentiment: str, signal_direction: str) -> str:
    direction = _direction_kind(signal_direction)
    if not direction:
        return "neutral"
    if sentiment == "risk_on":
        return "with_trend" if direction == "long" else "against_trend"
    if sentiment in {"risk_off", "panic"}:
        return "with_trend" if direction == "short" else "against_trend"
    return "neutral"


def _market_bias(spy: dict[str, Any] | None, qqq: dict[str, Any] | None) -> str:
    spy_pct = _pct(spy)
    qqq_pct = _pct(qqq)
    positives = sum(1 for value in (spy_pct, qqq_pct) if value is not None and value > 0)
    negatives = sum(1 for value in (spy_pct, qqq_pct) if value is not None and value < 0)
    known = sum(1 for value in (spy_pct, qqq_pct) if value is not None)
    if known == 0:
        return "unknown"
    if negatives == known:
        return "weak"
    if positives > 0 and negatives == 0:
        return "strong"
    if positives > 0:
        return "mixed_positive"
    return "flat"


def classify_market_sentiment(
    snapshots: Iterable[dict[str, Any]],
    signal_direction: str,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    config = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    by_symbol = _snapshot_map(snapshots)
    vix = by_symbol.get("VIX")
    spy = by_symbol.get("SPY")
    qqq = by_symbol.get("QQQ")
    vix_value = _value(vix)
    vix_prev = _coerce_float((vix or {}).get("prev_value"))
    vix_delta = _coerce_float((vix or {}).get("value_delta"))
    if vix_delta is None and vix_value is not None and vix_prev is not None:
        vix_delta = vix_value - vix_prev
    market_bias = _market_bias(spy, qqq)
    direction = _direction_kind(signal_direction)

    vix_usable = _is_usable(vix)
    vix_falling = vix_delta is not None and vix_delta <= float(config["vix_falling_delta"])
    equity_nonweak = market_bias in {"strong", "mixed_positive", "flat"}
    equity_weak = market_bias == "weak"

    sentiment = "unknown"
    if not vix_usable:
        sentiment = "unknown"
    elif vix_value is not None and vix_value >= float(config["vix_panic"]):
        sentiment = "panic_reversal_watch" if vix_falling and equity_nonweak else "panic"
    elif vix_value is not None and vix_value >= float(config["vix_risk_off"]):
        sentiment = "panic_reversal_watch" if vix_falling and equity_nonweak else "risk_off"
    elif (
        vix_value is not None
        and vix_value >= float(config["vix_reversal_floor"])
        and vix_prev is not None
        and vix_prev >= float(config["vix_risk_off"])
        and vix_falling
        and equity_nonweak
    ):
        sentiment = "panic_reversal_watch"
    elif vix_value is not None and vix_value >= float(config["vix_calm_max"]):
        sentiment = "risk_off" if equity_weak else "cautious"
    elif vix_value is not None and vix_value < float(config["vix_calm_max"]):
        if market_bias in {"strong", "mixed_positive"}:
            sentiment = "risk_on"
        elif equity_weak:
            sentiment = "cautious"
        else:
            sentiment = "neutral"

    relation = _relation_for_sentiment(sentiment, direction)
    return {
        "market_sentiment": sentiment,
        "market_relation": relation,
        "market_sentiment_text": _build_sentiment_text(sentiment, by_symbol, market_bias),
        "market_relation_text": _build_relation_text(sentiment, relation, direction),
    }


def _format_value(value: Any) -> str:
    numeric = _coerce_float(value)
    return "--" if numeric is None else f"{numeric:.2f}"


def _format_pct(value: Any) -> str:
    numeric = _coerce_float(value)
    if numeric is None:
        return "--"
    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:.2f}%"


def _build_sentiment_text(sentiment: str, by_symbol: dict[str, dict[str, Any]], market_bias: str) -> str:
    vix = by_symbol.get("VIX") or {}
    spy = by_symbol.get("SPY") or {}
    qqq = by_symbol.get("QQQ") or {}
    if not _is_usable(vix):
        return "VIX 数据缺失或延迟，市场情绪未知，仅做标注"

    label_map = {
        "risk_on": "市场偏 risk-on",
        "risk_off": "市场偏 risk-off",
        "panic": "极度恐慌，风险高",
        "panic_reversal_watch": "恐慌退潮，观察修复",
        "cautious": "市场谨慎",
        "neutral": "市场中性",
        "unknown": "市场情绪未知",
    }
    bias_text = {
        "strong": "SPY/QQQ 偏强",
        "mixed_positive": "SPY/QQQ 分歧但有转强",
        "weak": "SPY/QQQ 偏弱",
        "flat": "SPY/QQQ 平稳",
        "unknown": "SPY/QQQ 数据不足",
    }.get(market_bias, "SPY/QQQ 中性")
    return (
        f"VIX {_format_value(vix.get('value'))}({_format_pct(vix.get('change_pct'))})，"
        f"SPY {_format_pct(spy.get('change_pct'))}，QQQ {_format_pct(qqq.get('change_pct'))}，"
        f"{bias_text}，{label_map.get(sentiment, '市场情绪未知')}"
    )


def _build_relation_text(sentiment: str, relation: str, direction: str) -> str:
    direction_text = "多头" if direction == "long" else "空头" if direction == "short" else "信号"
    if relation == "with_trend":
        return f"{direction_text}信号顺市场情绪"
    if relation == "against_trend":
        return f"{direction_text}信号逆市场情绪"
    if sentiment == "panic_reversal_watch":
        return "恐慌退潮观察，不直接判定顺逆势"
    if sentiment == "unknown":
        return "市场情绪数据不足，不参与顺逆势判断"
    return "市场情绪中性或谨慎，不强行判定顺逆势"


def build_unknown_market_sentiment_extra(
    *,
    symbols: Iterable[str] = DEFAULT_MARKET_SENTIMENT_SYMBOLS,
    reason: str = "market_sentiment_unavailable",
) -> dict[str, Any]:
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    return {
        "market_indexes": [_missing_snapshot(symbol) for symbol in (normalized_symbols or DEFAULT_MARKET_SENTIMENT_SYMBOLS)],
        "market_sentiment": "unknown",
        "market_sentiment_text": "VIX 数据缺失或延迟，市场情绪未知，仅做标注",
        "market_relation": "neutral",
        "market_relation_text": "市场情绪数据不足，不参与顺逆势判断",
        "market_sentiment_source": reason,
    }


def build_market_sentiment_extra(
    *,
    api_app: Any,
    environment: str,
    signal_direction: str,
    signal_bar_time_ms: int,
    interval: str = "5m",
) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if not _cfg_bool(api_app, "ibkr_market_sentiment_enabled", runtime_environment, True):
        return {}
    mode = _cfg_text(api_app, "ibkr_market_sentiment_mode", runtime_environment, "annotate").strip().lower()
    if mode in {"off", "disabled", "false", "none"}:
        return {}

    symbols = _cfg_symbols(api_app, runtime_environment)
    thresholds = {
        "vix_calm_max": _cfg_float(api_app, "ibkr_market_sentiment_vix_calm_max", runtime_environment, DEFAULT_THRESHOLDS["vix_calm_max"]),
        "vix_risk_off": _cfg_float(api_app, "ibkr_market_sentiment_vix_risk_off", runtime_environment, DEFAULT_THRESHOLDS["vix_risk_off"]),
        "vix_panic": _cfg_float(api_app, "ibkr_market_sentiment_vix_panic", runtime_environment, DEFAULT_THRESHOLDS["vix_panic"]),
        "vix_reversal_floor": _cfg_float(api_app, "ibkr_market_sentiment_vix_reversal_floor", runtime_environment, DEFAULT_THRESHOLDS["vix_reversal_floor"]),
        "vix_falling_delta": _cfg_float(api_app, "ibkr_market_sentiment_vix_falling_delta", runtime_environment, DEFAULT_THRESHOLDS["vix_falling_delta"]),
    }

    try:
        snapshots = fetch_market_sentiment_snapshots(
            api_app=api_app,
            environment=runtime_environment,
            signal_bar_time_ms=int(signal_bar_time_ms or 0),
            interval=interval,
            symbols=symbols,
        )
        classification = classify_market_sentiment(snapshots, signal_direction, thresholds=thresholds)
        return {
            "market_indexes": snapshots,
            **classification,
            "market_sentiment_source": "ibkr_market_monitors",
            "market_sentiment_mode": "annotate",
        }
    except Exception as exc:
        logger = getattr(api_app, "logger", None)
        if logger is not None and hasattr(logger, "warning"):
            try:
                logger.warning("Market sentiment annotation unavailable: %s", exc)
            except Exception:
                pass
        return build_unknown_market_sentiment_extra(symbols=symbols, reason="market_sentiment_error")


__all__ = [
    "DEFAULT_MARKET_SENTIMENT_SYMBOLS",
    "build_market_sentiment_extra",
    "build_unknown_market_sentiment_extra",
    "classify_market_sentiment",
    "fetch_market_sentiment_snapshots",
]
