from __future__ import annotations

import time
import traceback
from datetime import date, datetime, timedelta

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.timeframe_utils import interval_to_ms, ms_to_et


RECENT_INTRADAY_CLOSE_LOOKBACK_DAYS = 14
MAX_REGULAR_CLOSE_FALLBACK_PAGES = 20


def _escape_filter_value(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _et_midnight_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=ET).timestamp() * 1000)


def _coerce_positive_float(value) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _coerce_bar_ms(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _sorted_daily_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        [row for row in rows if row.get("date") and _coerce_bar_ms(row.get("bar_time_ms")) > 0],
        key=lambda row: _coerce_bar_ms(row.get("bar_time_ms")),
    )


def _regular_intraday_close_rows(api_app, environment: str, symbol: str, rows: list[dict], current_date: str) -> list[dict]:
    current_day = _parse_date(current_date)
    pb = getattr(api_app, "pb", None)
    if current_day is None or pb is None or not hasattr(pb, "get_all_records"):
        return rows

    latest_day = _parse_date(rows[-1]["date"]) if rows else None
    if latest_day is not None and (current_day - latest_day).days <= 3:
        return rows

    current_start_ms = _et_midnight_ms(current_day)
    if latest_day is None:
        start_ms = _et_midnight_ms(current_day - timedelta(days=RECENT_INTRADAY_CLOSE_LOOKBACK_DAYS))
    else:
        start_ms = _et_midnight_ms(latest_day + timedelta(days=1))
    if start_ms >= current_start_ms:
        return rows

    environment_filter = api_app.build_bar_environment_filter(environment, include_legacy_empty=True)
    filter_text = (
        f'symbol = "{_escape_filter_value(symbol)}" && '
        'interval = "5m" && '
        'session_type = "regular" && '
        f'{environment_filter} && '
        f"bar_time_ms >= {max(0, start_ms)} && "
        f"bar_time_ms < {current_start_ms}"
    )
    try:
        intraday_rows = pb.get_all_records(
            "ibkr_bars",
            filter=filter_text,
            sort="bar_time_ms",
            max_pages=MAX_REGULAR_CLOSE_FALLBACK_PAGES,
        ) or []
    except Exception:
        return rows

    by_date: dict[str, dict] = {str(row.get("date")): dict(row) for row in rows if row.get("date")}
    for row in intraday_rows:
        bar_ms = _coerce_bar_ms((row or {}).get("bar_time_ms"))
        close = _coerce_positive_float((row or {}).get("close"))
        if bar_ms <= 0 or close <= 0:
            continue
        if str((row or {}).get("session_type") or "").strip().lower() not in {"", "regular"}:
            continue
        row_date = ms_to_et(bar_ms).strftime("%Y-%m-%d")
        if row_date >= current_date:
            continue
        existing = by_date.get(row_date)
        if existing is None or bar_ms >= _coerce_bar_ms(existing.get("bar_time_ms")):
            by_date[row_date] = {
                "bar_time_ms": bar_ms,
                "date": row_date,
                "close": close,
                "source": "regular_5m_fallback",
            }

    return _sorted_daily_rows(list(by_date.values()))


def refresh_symbol_metadata(force: bool = False):
    api_app = _api_app()
    now = time.time()
    if api_app.symbol_metadata_cache and not force and (now - api_app.metadata_cache_updated_at) < 300:
        return api_app.symbol_metadata_cache

    metadata = {}
    try:
        rows = api_app.pb.get_all_records("watchlist", max_pages=20)
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            metadata[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
            }
    except Exception:
        traceback.print_exc()

    api_app.symbol_metadata_cache = metadata
    api_app.metadata_cache_updated_at = now
    return api_app.symbol_metadata_cache


def refresh_daily_close_cache(environments, force: bool = False):
    api_app = _api_app()
    market_date = api_app.current_market_date()
    if (
        api_app.daily_close_cache
        and not force
        and api_app.daily_close_cache_date == market_date
        and set(environments).issubset(set(api_app.daily_close_cache.keys()))
    ):
        return api_app.daily_close_cache

    cache = {}
    now_ms = int(time.time() * 1000)
    lookback_ms = interval_to_ms("1d") * 400

    for environment in environments:
        rows = api_app.pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'interval = "1d" && {api_app.build_bar_environment_filter(environment, include_legacy_empty=True)} '
                f"&& bar_time_ms >= {max(0, now_ms - lookback_ms)}"
            ),
            sort="bar_time_ms",
            max_pages=400,
        )
        env_cache = {}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            close = float(row.get("close", 0) or 0)
            if not symbol or bar_ms <= 0 or close <= 0:
                continue
            env_cache.setdefault(symbol, []).append({
                "bar_time_ms": bar_ms,
                "date": ms_to_et(bar_ms).strftime("%Y-%m-%d"),
                "close": close,
            })
        cache[environment] = env_cache

    api_app.daily_close_cache = cache
    api_app.daily_close_cache_date = market_date
    return api_app.daily_close_cache


def reset_daily_runtime_state(environments=None, reason: str = "new_day") -> dict:
    api_app = _api_app()
    runtime_environments = []
    for environment in (environments or api_app.DEFAULT_COMPUTE_ENVIRONMENTS):
        normalized = str(environment or "").strip().lower()
        if normalized in api_app.SUPPORTED_COMPUTE_ENVIRONMENTS and normalized not in runtime_environments:
            runtime_environments.append(normalized)

    reset_count = 0
    with api_app.compute_lock:
        for (environment, _, _), signal_generator in api_app.signal_gens.items():
            if environment not in runtime_environments:
                continue
            signal_generator.daily_reset()
            reset_count += 1

        signal_bootstrap_checked = getattr(api_app, "signal_bootstrap_checked", None)
        if signal_bootstrap_checked is not None:
            for key in list(signal_bootstrap_checked):
                environment = str(key[0] if isinstance(key, tuple) and key else "").strip().lower()
                if environment in runtime_environments:
                    signal_bootstrap_checked.discard(key)

        api_app.daily_close_cache = {}
        api_app.daily_close_cache_date = ""

    return {
        "ok": True,
        "reason": reason,
        "date": api_app.current_market_date(),
        "environments": runtime_environments,
        "signal_generators_reset": reset_count,
    }


def get_daily_change_fields(environment: str, symbol: str, current_close: float, bar_time_ms: int):
    api_app = _api_app()
    env_cache = api_app.daily_close_cache.get(environment, {})
    normalized_symbol = symbol.upper()
    rows = _sorted_daily_rows(env_cache.get(normalized_symbol, []))
    current_date = ms_to_et(bar_time_ms).strftime("%Y-%m-%d")
    rows = _regular_intraday_close_rows(api_app, environment, normalized_symbol, rows, current_date)
    api_app.daily_close_cache.setdefault(environment, {})[normalized_symbol] = rows
    history = [row for row in rows if row["date"] < current_date]

    prev_close = history[-1]["close"] if len(history) >= 1 else 0.0
    prev_prev_close = history[-2]["close"] if len(history) >= 2 else 0.0
    close_5 = history[-5]["close"] if len(history) >= 5 else 0.0

    day_change_pct = ((current_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    prev_close_change_pct = (
        (prev_close - prev_prev_close) / prev_prev_close * 100.0
        if prev_prev_close > 0
        else 0.0
    )
    change_7d = ((current_close - close_5) / close_5 * 100.0) if close_5 > 0 else 0.0

    return {
        "day_change_pct": round(day_change_pct, 2),
        "prev_close_change_pct": round(prev_close_change_pct, 2),
        "change_7d": round(change_7d, 2),
    }


__all__ = [
    "get_daily_change_fields",
    "refresh_daily_close_cache",
    "refresh_symbol_metadata",
    "reset_daily_runtime_state",
]
