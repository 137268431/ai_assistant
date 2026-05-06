from __future__ import annotations

from .startup_preload_state_store import LOGGER


def _preload_symbol_indicator_state(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    target_ms: int,
    *,
    use_materialize: bool = True,
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = str(interval or "").strip().lower()

    if use_materialize and hasattr(api_app, "materialize_engines_from_storage"):
        try:
            results = api_app.materialize_engines_from_storage(
                normalized_environment,
                [normalized_symbol],
                normalized_interval,
                hydrate_signal_state=True,
                persist_latest_indicator=False,
            )
            result = dict((results or {}).get(normalized_symbol) or {})
            if result:
                result.setdefault("indicator_seeded", False)
                return result
        except Exception:
            LOGGER.exception(
                "Compute startup preload materialize failed: env=%s interval=%s symbol=%s",
                normalized_environment,
                normalized_interval,
                normalized_symbol,
            )

    processed = api_app.bootstrap_engine_state(
        normalized_environment,
        normalized_symbol,
        normalized_interval,
        int(target_ms or 0),
        inclusive=True,
    )
    engine = api_app.engines.get((normalized_environment, normalized_symbol, normalized_interval))
    return {
        "processed": processed,
        "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
        "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
        "is_ready": bool(engine and engine.is_ready()),
        "indicator_seeded": False,
        "reason": "bootstrapped" if processed > 0 else "already_materialized",
    }


def _preload_interval_indicator_state(
    api_app,
    environment: str,
    symbols: list[str],
    interval: str,
    target_ms_by_symbol: dict[str, int] | None = None,
) -> dict[str, dict]:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_interval = str(interval or "").strip().lower()
    normalized_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    ]
    if not normalized_symbols:
        return {}

    targets = {
        str(symbol or "").strip().upper(): int(target_ms or 0)
        for symbol, target_ms in (target_ms_by_symbol or {}).items()
        if str(symbol or "").strip()
    }

    results: dict[str, dict] = {}
    if hasattr(api_app, "materialize_engines_from_storage"):
        try:
            materialized = api_app.materialize_engines_from_storage(
                normalized_environment,
                normalized_symbols,
                normalized_interval,
                hydrate_signal_state=True,
                persist_latest_indicator=False,
            )
            for symbol in normalized_symbols:
                payload = dict((materialized or {}).get(symbol) or {})
                if payload:
                    payload.setdefault("indicator_seeded", False)
                    results[symbol] = payload
            missing = [symbol for symbol in normalized_symbols if symbol not in results]
            if not missing:
                return results
            LOGGER.warning(
                "Compute startup preload materialize returned partial result: env=%s interval=%s missing=%s",
                normalized_environment,
                normalized_interval,
                ",".join(missing),
            )
        except Exception:
            LOGGER.exception(
                "Compute startup preload batch materialize failed: env=%s interval=%s symbols=%d",
                normalized_environment,
                normalized_interval,
                len(normalized_symbols),
            )

    for symbol in normalized_symbols:
        if symbol in results:
            continue
        results[symbol] = _preload_symbol_indicator_state(
            api_app,
            normalized_environment,
            symbol,
            normalized_interval,
            int(targets.get(symbol, 0) or 0),
            use_materialize=False,
        )
    return results


__all__ = [name for name in globals() if not name.startswith("__")]
