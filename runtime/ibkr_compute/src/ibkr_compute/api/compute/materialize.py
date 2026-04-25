from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ibkr_compute.market.timeframe_utils import latest_safe_closed_bucket_ms, normalize_interval

from ibkr_compute.api.compute.runtime_state.engines import get_or_create_engine
from ibkr_compute.api.compute.runtime_state.runtime import _api_app


def _safe_storage_upper_bound_ms(api_app, environment: str, interval: str) -> int:
    normalized_interval = normalize_interval(interval)
    if normalized_interval != "5m":
        return 0

    close_delay_seconds = 8
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None:
        try:
            close_delay_seconds = max(
                0,
                int(
                    cfg.get_int_for_environment(
                        "ibkr_official_5m_close_delay_sec",
                        environment,
                        8,
                    )
                ),
            )
        except Exception:
            close_delay_seconds = 8

    return latest_safe_closed_bucket_ms(
        normalized_interval,
        delay_seconds=close_delay_seconds,
    )


def bootstrap_engine_state(
    environment: str,
    symbol: str,
    interval: str,
    before_bar_time_ms: int,
    inclusive: bool = True,
    force_rebuild: bool = False,
    hydrate_signal_state: bool = True,
):
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    key = (runtime_environment, symbol, normalized_interval)
    engine = get_or_create_engine(runtime_environment, symbol, normalized_interval)
    signal_generator = api_app.signal_gens.get(key)
    target_ms = int(before_bar_time_ms or 0)

    if force_rebuild:
        api_app.engine_bootstrap_checked.discard(key)

    if (
        not force_rebuild
        and key in api_app.engine_bootstrap_checked
        and (target_ms <= 0 or engine.last_bar_time_ms >= target_ms)
    ):
        return 0

    lookback = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 192) or 192)
    max_pages = max(1, (lookback + 199) // 200 + 1)
    comparison = "<=" if inclusive else "<"
    filter_parts = [
        f'symbol = "{symbol}"',
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
    ]
    safe_upper_ms = _safe_storage_upper_bound_ms(api_app, runtime_environment, normalized_interval)
    if safe_upper_ms > 0:
        filter_parts.append(f"bar_time_ms <= {safe_upper_ms}")
    if target_ms > 0:
        filter_parts.append(f"bar_time_ms {comparison} {target_ms}")

    rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="-bar_time_ms",
        max_pages=max_pages,
    )
    if lookback > 0:
        rows = rows[:lookback]
    rows = list(reversed(rows))

    engine.reset()
    if signal_generator and hydrate_signal_state:
        signal_generator.daily_reset()

    processed = 0
    for row in rows:
        normalized_row = api_app.normalize_bar_environment(row, runtime_environment)
        snapshot = engine.update({
            "open": float(normalized_row.get("open", 0) or 0),
            "high": float(normalized_row.get("high", 0) or 0),
            "low": float(normalized_row.get("low", 0) or 0),
            "close": float(normalized_row.get("close", 0) or 0),
            "volume": float(normalized_row.get("volume", 0) or 0),
            "bar_time_ms": int(normalized_row.get("bar_time_ms", 0) or 0),
            "us_time": normalized_row.get("us_time", ""),
            "cn_time": normalized_row.get("cn_time", ""),
            "session_type": normalized_row.get("session_type", "regular"),
        })
        if (
            hydrate_signal_state
            and normalized_interval == "5m"
            and signal_generator
            and snapshot
            and engine.is_ready()
        ):
            signal_generator.update(snapshot)
        processed += 1

    if engine.last_bar_time_ms > 0:
        # Storage bootstrap warms the engine only; processed cursors move after indicator flush.
        interval_key = (runtime_environment, normalized_interval)
        api_app.last_interval_fetch_ms[interval_key] = max(
            int(api_app.last_interval_fetch_ms.get(interval_key, 0) or 0),
            int(engine.last_bar_time_ms),
        )
    api_app.engine_bootstrap_checked.add(key)
    return processed


def materialize_engines_from_storage(
    environment: str,
    symbols,
    interval: str = "5m",
    hydrate_signal_state: bool = True,
    persist_latest_indicator: bool = False,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = normalize_interval(interval)
    normalized_symbols = api_app.normalize_symbols(symbols)
    if not normalized_symbols:
        return {}
    started_at = time.perf_counter()

    symbol_filters = " || ".join(f'symbol = "{symbol}"' for symbol in normalized_symbols)
    filter_parts = [
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
        f'({symbol_filters})',
    ]
    safe_upper_ms = _safe_storage_upper_bound_ms(api_app, runtime_environment, normalized_interval)
    if safe_upper_ms > 0:
        filter_parts.append(f"bar_time_ms <= {safe_upper_ms}")
    rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="-bar_time_ms",
        max_pages=max(4, len(normalized_symbols)),
    )

    latest_by_symbol = {}
    latest_row_by_symbol = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        bar_ms = int(row.get("bar_time_ms", 0) or 0)
        if symbol and bar_ms > 0 and symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = bar_ms
            latest_row_by_symbol[symbol] = api_app.normalize_bar_environment(row, runtime_environment)
        if len(latest_by_symbol) >= len(normalized_symbols):
            break

    def _materialize_symbol(symbol: str) -> dict:
        target_ms = int(latest_by_symbol.get(symbol, 0) or 0)
        if target_ms <= 0:
            return {
                "processed": 0,
                "bar_count": 0,
                "last_bar_time_ms": 0,
                "is_ready": False,
                "reason": "no_stored_bars",
            }

        existing_engine = api_app.engines.get((runtime_environment, symbol, normalized_interval))
        force_rebuild = bool(existing_engine and not existing_engine.is_ready())
        processed = bootstrap_engine_state(
            runtime_environment,
            symbol,
            normalized_interval,
            target_ms,
            inclusive=True,
            force_rebuild=force_rebuild,
            hydrate_signal_state=hydrate_signal_state,
        )
        engine = api_app.engines.get((runtime_environment, symbol, normalized_interval))
        return {
            "processed": processed,
            "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
            "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
            "is_ready": bool(engine and engine.is_ready()),
            "reason": (
                "rebootstrapped"
                if force_rebuild and processed > 0
                else "bootstrapped"
                if processed > 0
                else "already_materialized"
            ),
        }

    results = {}
    worker_count = min(api_app.MATERIALIZE_MAX_WORKERS, len(normalized_symbols))
    if worker_count <= 1:
        for symbol in normalized_symbols:
            results[symbol] = _materialize_symbol(symbol)
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="materialize-engine") as executor:
            future_map = {
                executor.submit(_materialize_symbol, symbol): symbol
                for symbol in normalized_symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    traceback.print_exc()
                    results[symbol] = {
                        "processed": 0,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "is_ready": False,
                        "reason": "materialize_failed",
                    }

    ready_count = sum(1 for item in results.values() if bool((item or {}).get("is_ready")))
    indicator_seed_written = 0
    indicator_seed_errors = 0
    if persist_latest_indicator:
        indicator_batch = []
        ready_symbols = []
        for symbol in normalized_symbols:
            result = dict(results.get(symbol) or {})
            if not bool(result.get("is_ready")):
                continue
            engine = api_app.engines.get((runtime_environment, symbol, normalized_interval))
            latest_row = latest_row_by_symbol.get(symbol)
            snapshot = engine.get_snapshot() if engine and hasattr(engine, "get_snapshot") else {}
            if not engine or not latest_row or not snapshot:
                continue
            indicator_batch.append(
                api_app.build_indicator_payload(
                    runtime_environment,
                    symbol,
                    normalized_interval,
                    latest_row,
                    engine,
                    snapshot,
                )
            )
            ready_symbols.append(symbol)
        flush_result = api_app.flush_indicator_batch(indicator_batch)
        indicator_seed_written = int(flush_result.get("written", 0) or 0)
        indicator_seed_errors = int(flush_result.get("errors", 0) or 0)
        indicator_seed_ok = indicator_seed_errors == 0 and indicator_seed_written >= len(ready_symbols)
        last_processed_ms = getattr(api_app, "last_processed_ms", None)
        if not isinstance(last_processed_ms, dict):
            last_processed_ms = {}
            setattr(api_app, "last_processed_ms", last_processed_ms)
        last_interval_fetch_ms = getattr(api_app, "last_interval_fetch_ms", None)
        if not isinstance(last_interval_fetch_ms, dict):
            last_interval_fetch_ms = {}
            setattr(api_app, "last_interval_fetch_ms", last_interval_fetch_ms)
        for symbol in ready_symbols:
            results.setdefault(symbol, {})["indicator_seeded"] = indicator_seed_ok
            if indicator_seed_ok:
                key = (runtime_environment, symbol, normalized_interval)
                latest_bar_ms = int(results.get(symbol, {}).get("last_bar_time_ms", 0) or 0)
                last_processed_ms[key] = max(
                    int(last_processed_ms.get(key, 0) or 0),
                    latest_bar_ms,
                )
                last_interval_fetch_ms[(runtime_environment, normalized_interval)] = max(
                    int(last_interval_fetch_ms.get((runtime_environment, normalized_interval), 0) or 0),
                    latest_bar_ms,
                )
        for symbol in normalized_symbols:
            results.setdefault(symbol, {}).setdefault("indicator_seeded", False)
        if indicator_seed_ok and hasattr(api_app, "persist_compute_cursors"):
            api_app.persist_compute_cursors(runtime_environment)
    api_app.logger.info(
        "Materialized engines from storage: env=%s interval=%s symbols=%d ready=%d indicator_seed_written=%d indicator_seed_errors=%d workers=%d elapsed_s=%.3f",
        runtime_environment,
        normalized_interval,
        len(normalized_symbols),
        ready_count,
        indicator_seed_written,
        indicator_seed_errors,
        worker_count,
        time.perf_counter() - started_at,
    )
    return results


def reset_compute_state_for_symbols(environment: str, symbols, intervals=None) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = set(api_app.normalize_symbols(symbols))
    interval_filter = {normalize_interval(interval) for interval in (intervals or api_app.INTERVALS)}
    removed = {"engines": 0, "signal_gens": 0, "cursors": 0, "bootstraps": 0}

    if not normalized_symbols:
        return removed

    for key in list(api_app.engines.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            api_app.engines[key].reset()
            removed["engines"] += 1

    for key in list(api_app.signal_gens.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            api_app.signal_gens[key].daily_reset()
            removed["signal_gens"] += 1

    for key in list(api_app.last_processed_ms.keys()):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            api_app.last_processed_ms.pop(key, None)
            removed["cursors"] += 1

    for key in list(api_app.engine_bootstrap_checked):
        env, symbol, interval = key
        if env == runtime_environment and symbol in normalized_symbols and interval in interval_filter:
            api_app.engine_bootstrap_checked.discard(key)
            removed["bootstraps"] += 1

    return removed
