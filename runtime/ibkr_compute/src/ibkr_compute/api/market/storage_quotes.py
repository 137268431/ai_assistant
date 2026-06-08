from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable


logger = logging.getLogger(__name__)


def _slow_log_threshold_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("IBKR_COMPUTE_STORAGE_QUOTE_SLOW_LOG_SEC", "2.0") or 0.0))
    except Exception:
        return 2.0


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


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
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cfg_float(api_app: Any, key: str, environment: str, default: float) -> float:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_float_for_environment"):
        try:
            return float(cfg.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    return float(default)


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
    safe_clause = "AND bar_time_ms <= ?" if safe_upper_ms > 0 else ""
    params: tuple[Any, ...]
    if safe_upper_ms > 0:
        params = (str(symbol or "").strip().upper(), chart_tf, *env_params, int(safe_upper_ms))
    else:
        params = (str(symbol or "").strip().upper(), chart_tf, *env_params)
    rows = conn.execute(
        f"""
        SELECT symbol, interval, us_time, cn_time, bar_time_ms, extra, environment, updated
        FROM ibkr_indicators
        WHERE symbol = ?
          AND interval = ?
          AND {env_sql}
          {safe_clause}
        ORDER BY bar_time_ms DESC
        LIMIT 2
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_bar_rows(conn: Any, symbol: str, environment: str, interval: str, safe_upper_ms: int) -> list[dict[str, Any]]:
    from ibkr_compute.market.timeframe_utils import normalize_interval

    env_sql, env_params = _environment_sql(environment, include_legacy_empty=True)
    safe_clause = "AND bar_time_ms <= ?" if safe_upper_ms > 0 else ""
    params: tuple[Any, ...]
    if safe_upper_ms > 0:
        params = (str(symbol or "").strip().upper(), normalize_interval(interval), *env_params, int(safe_upper_ms))
    else:
        params = (str(symbol or "").strip().upper(), normalize_interval(interval), *env_params)
    rows = conn.execute(
        f"""
        SELECT symbol, interval, us_time, cn_time, bar_time_ms, close, extra, environment, updated
        FROM ibkr_bars
        WHERE symbol = ?
          AND interval = ?
          AND {env_sql}
          {safe_clause}
        ORDER BY bar_time_ms DESC
        LIMIT 2
        """,
        params,
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


def _first_extra_float(extra: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = _coerce_float(extra.get(key))
        if value is not None:
            return value
    return None


def _snapshot_from_rows(*, symbol: str, source: str, rows: list[dict[str, Any]], now_ms: int) -> dict[str, Any] | None:
    if not rows:
        return None
    current = dict(rows[0])
    value = _value_from_row(current, source)
    if value is None:
        return None

    extra = _parse_json(current.get("extra"))
    bar_time_ms = _coerce_int(current.get("bar_time_ms"))
    close_time_ms = _coerce_int(extra.get("bar_close_time_ms")) or bar_time_ms
    age_s = round(max(0, int(now_ms or 0) - close_time_ms) / 1000.0, 1) if close_time_ms > 0 else None
    prev_close = _first_extra_float(extra, ("prev_close", "previous_close", "prior_close"))
    day_change = _first_extra_float(extra, ("day_change", "change", "change_1d"))
    day_change_pct = _first_extra_float(extra, ("day_change_pct", "change_pct", "pct_change", "change_1d_pct"))
    conid = _coerce_int(extra.get("conid"))
    tick_count = _coerce_int(extra.get("tick_count"))

    return {
        "symbol": str(symbol or "").strip().upper(),
        "conid": conid or None,
        "last_price": round(float(value), 4),
        "prev_close": round(float(prev_close), 4) if prev_close is not None else None,
        "day_change": round(float(day_change), 4) if day_change is not None else None,
        "day_change_pct": round(float(day_change_pct), 4) if day_change_pct is not None else None,
        "bar_time_ms": bar_time_ms,
        "bar_close_time_ms": close_time_ms,
        "us_time": str(current.get("us_time") or ""),
        "cn_time": str(current.get("cn_time") or ""),
        "updated": str(current.get("updated") or ""),
        "data_age_s": age_s,
        "tick_count": tick_count,
        "source": "indicator" if source == "indicator" else "canonical_5m",
        "storage_source": str(extra.get("source") or ("indicator" if source == "indicator" else "ibkr_bars")),
        "quote_fallback": True,
    }


def _choose_best_snapshot(indicator_snapshot: dict[str, Any] | None, bar_snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if indicator_snapshot and bar_snapshot:
        indicator_ms = _coerce_int(indicator_snapshot.get("bar_time_ms"))
        bar_ms = _coerce_int(bar_snapshot.get("bar_time_ms"))
        if bar_ms > indicator_ms:
            merged = dict(bar_snapshot)
            for key in ("day_change", "day_change_pct", "prev_close"):
                if merged.get(key) is None and indicator_snapshot.get(key) is not None:
                    merged[key] = indicator_snapshot.get(key)
            if merged.get("day_change_pct") is None:
                merged["day_change_pct_source"] = "missing"
            else:
                merged["day_change_pct_source"] = "indicator" if indicator_snapshot.get("day_change_pct") is not None else merged.get("source")
            return merged
        indicator_snapshot = dict(indicator_snapshot)
        indicator_snapshot["day_change_pct_source"] = "indicator" if indicator_snapshot.get("day_change_pct") is not None else "missing"
        return indicator_snapshot
    snapshot = indicator_snapshot or bar_snapshot
    if snapshot:
        snapshot = dict(snapshot)
        snapshot["day_change_pct_source"] = snapshot.get("source") if snapshot.get("day_change_pct") is not None else "missing"
    return snapshot


def fetch_storage_quote_snapshots(
    *,
    api_app: Any,
    environment: str,
    symbols: Iterable[str],
    safe_upper_ms: int = 0,
    interval: str = "5m",
) -> dict[str, dict[str, Any]]:
    from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
    from ibkr_compute.market.timeframe_utils import interval_to_chart_tf

    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = []
    seen = set()
    for symbol in symbols or []:
        normalized = str(symbol or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_symbols.append(normalized)
    if not normalized_symbols:
        return {}

    chart_tf = interval_to_chart_tf(interval)
    safe_ms = max(0, int(safe_upper_ms or 0))
    now_ms = int(time.time() * 1000)
    snapshots: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    query_count = len(normalized_symbols) * 2
    try:
        with open_pb_sqlite(readonly=True, timeout=_direct_sqlite_timeout(api_app, runtime_environment)) as conn:
            for symbol in normalized_symbols:
                indicator_rows = _fetch_indicator_rows(conn, symbol, runtime_environment, chart_tf, safe_ms)
                bar_rows = _fetch_bar_rows(conn, symbol, runtime_environment, interval, safe_ms)
                snapshot = _choose_best_snapshot(
                    _snapshot_from_rows(symbol=symbol, source="indicator", rows=indicator_rows, now_ms=now_ms),
                    _snapshot_from_rows(symbol=symbol, source="bar", rows=bar_rows, now_ms=now_ms),
                )
                if snapshot:
                    snapshots[symbol] = snapshot
    finally:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        threshold_s = _slow_log_threshold_seconds()
        if threshold_s > 0 and elapsed_ms >= threshold_s * 1000.0:
            logger.warning(
                "Storage quote snapshot fetch slow: environment=%s symbols=%d query_count=%d elapsed_ms=%.1f",
                runtime_environment,
                len(normalized_symbols),
                query_count,
                elapsed_ms,
            )
    return snapshots


def merge_quote_with_storage(existing: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    item = dict(existing or {})
    had_live_quote = (
        item.get("quote_fallback") is False
        or item.get("snapshot_ok") is True
        or (
            item.get("quote_fallback") is not True
            and (
                _coerce_float(item.get("bid")) is not None
                or _coerce_float(item.get("ask")) is not None
                or (_coerce_float(item.get("last_price")) is not None and item.get("quote_age_s") is not None)
            )
        )
    )
    if not item.get("symbol"):
        item["symbol"] = str(snapshot.get("symbol") or "").strip().upper()
    if not item.get("conid") and snapshot.get("conid"):
        item["conid"] = snapshot.get("conid")
    for key in ("last_price", "prev_close", "day_change", "day_change_pct"):
        if item.get(key) is None and snapshot.get(key) is not None:
            item[key] = snapshot.get(key)
    item.setdefault("bid", None)
    item.setdefault("ask", None)
    item.setdefault("last_size", None)
    item.setdefault("volume", None)
    item.setdefault("quote_age_s", None)
    item.setdefault("last_update", snapshot.get("updated") or "")
    item["quote_fallback"] = not had_live_quote
    item["fallback_source"] = snapshot.get("source") or "storage"
    item["last_price_source"] = item.get("last_price_source") or (snapshot.get("source") if snapshot.get("last_price") is not None else "missing")
    item["day_change_pct_source"] = item.get("day_change_pct_source") or snapshot.get("day_change_pct_source") or snapshot.get("source") or "storage"
    item["bar_time_ms"] = snapshot.get("bar_time_ms")
    item["bar_close_time_ms"] = snapshot.get("bar_close_time_ms")
    item["us_time"] = snapshot.get("us_time") or item.get("us_time") or ""
    item["cn_time"] = snapshot.get("cn_time") or item.get("cn_time") or ""
    item["data_age_s"] = snapshot.get("data_age_s")
    return item


__all__ = ["fetch_storage_quote_snapshots", "merge_quote_with_storage"]
