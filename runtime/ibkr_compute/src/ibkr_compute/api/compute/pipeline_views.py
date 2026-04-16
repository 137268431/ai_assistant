from __future__ import annotations

import time
import traceback

from flask import jsonify

from ibkr_compute.api.compute.request import build_compute_disabled_payload, build_compute_execution_plan


def _api_app():
    from .. import app as api_app

    return api_app


def build_compute_response(payload=None):
    api_app = _api_app()

    with api_app.compute_lock:
        api_app.cfg.refresh()
        plan = build_compute_execution_plan(payload)
        if not plan["enabled_environments"]:
            return jsonify(build_compute_disabled_payload(plan["requested_environments"]))

        start = time.time()
        processed = 0
        signals_found = 0
        errors = 0
        indicator_batch = []
        signal_batch = []
        dirty_cursor_environments = set()

        def flush_pending_indicators():
            nonlocal errors, indicator_batch
            if not indicator_batch:
                return
            result = api_app.flush_indicator_batch(indicator_batch)
            errors += int(result.get("errors", 0) or 0)
            indicator_batch = []

        def flush_pending_signals():
            nonlocal errors, signals_found, signal_batch
            if not signal_batch:
                return
            result = api_app.flush_signal_batch(signal_batch)
            errors += int(result.get("errors", 0) or 0)
            signals_found += int(result.get("written", 0) or 0)
            signal_batch = []

        api_app.refresh_symbol_metadata()
        rollup_results = api_app.ensure_higher_timeframe_bars(
            plan["enabled_environments"],
            force=plan["force_rollup"],
            symbols=plan["requested_symbols"] if plan["targeted_rollup"] else None,
            incremental=plan["incremental_rollup"],
            intervals=plan["rollup_intervals"],
        )
        errors += sum(int(result.get("errors", 0) or 0) for result in rollup_results.values())
        api_app.refresh_daily_close_cache(plan["enabled_environments"])

        try:
            for environment in plan["enabled_environments"]:
                if plan["targeted_rebuild"]:
                    api_app.reset_compute_state_for_symbols(
                        environment,
                        plan["requested_symbols"],
                        intervals=plan["intervals"],
                    )
                if not plan["skip_persisted_cursor"]:
                    api_app.load_persisted_compute_cursors(environment)
                signal_params = api_app.get_signal_generator_params(environment)
                signal_enabled_symbols = {
                    str(symbol or "").strip().upper()
                    for symbol in (api_app.normalize_symbol_csv(signal_params.get("signal_enabled_symbols") or ""))
                }
                for interval in plan["intervals"]:
                    interval_bars = api_app.fetch_interval_bars(
                        environment,
                        interval,
                        symbols=plan["requested_symbols"] if plan["requested_symbols"] else None,
                        full_scan=plan["targeted_rebuild"],
                    )
                    if not interval_bars:
                        continue

                    by_symbol = {}
                    for bar in interval_bars:
                        symbol = str(bar.get("symbol", "")).upper()
                        if symbol:
                            by_symbol.setdefault(symbol, []).append(bar)

                    for symbol, bars in by_symbol.items():
                        bars.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                        engine = api_app.get_or_create_engine(environment, symbol, interval, signal_params=signal_params)
                        signal_generator = api_app.signal_gens.get((environment, symbol, interval))
                        key = (environment, symbol, interval)
                        last_ms = int(api_app.last_processed_ms.get(key, 0) or 0)
                        bootstrap_target_ms = last_ms
                        bootstrap_inclusive = True
                        if bootstrap_target_ms <= 0 and bars:
                            bootstrap_target_ms = int(bars[0].get("bar_time_ms", 0) or 0)
                            bootstrap_inclusive = False
                        if bootstrap_target_ms > 0:
                            api_app.bootstrap_engine_state(
                                environment,
                                symbol,
                                interval,
                                bootstrap_target_ms,
                                inclusive=bootstrap_inclusive,
                                hydrate_signal_state=plan["persist_signals"],
                            )
                            last_ms = int(api_app.last_processed_ms.get(key, 0) or 0)

                        for bar in bars:
                            bar_ms = int(bar.get("bar_time_ms", 0) or 0)
                            if bar_ms <= last_ms:
                                continue

                            snapshot = engine.update({
                                "open": float(bar.get("open", 0) or 0),
                                "high": float(bar.get("high", 0) or 0),
                                "low": float(bar.get("low", 0) or 0),
                                "close": float(bar.get("close", 0) or 0),
                                "volume": float(bar.get("volume", 0) or 0),
                                "bar_time_ms": bar_ms,
                                "us_time": bar.get("us_time", ""),
                                "cn_time": bar.get("cn_time", ""),
                                "session_type": bar.get("session_type", "regular"),
                            })
                            api_app.last_processed_ms[key] = bar_ms
                            last_ms = bar_ms
                            dirty_cursor_environments.add(environment)
                            processed += 1

                            if not snapshot or not engine.is_ready():
                                continue

                            indicator_batch.append(api_app.build_indicator_payload(environment, symbol, interval, bar, engine, snapshot))
                            if len(indicator_batch) >= api_app.INDICATOR_BATCH_SIZE:
                                flush_pending_indicators()

                            if (
                                not plan["persist_signals"]
                                or interval != "5m"
                                or not signal_generator
                                or symbol not in signal_enabled_symbols
                            ):
                                continue

                            signal = signal_generator.update(snapshot)
                            if signal and api_app.is_recent_signal_bar(bar_ms, interval):
                                signal_batch.append(api_app.build_signal_payload(environment, symbol, interval, bar, engine, signal))
                                if len(signal_batch) >= api_app.SIGNAL_BATCH_SIZE:
                                    flush_pending_signals()
        except Exception:
            errors += 1
            api_app.error_count += 1
            traceback.print_exc()
        finally:
            flush_pending_indicators()
            flush_pending_signals()
            for environment in sorted(dirty_cursor_environments):
                api_app.persist_compute_cursors(environment)

        api_app.last_compute_time = time.time()
        api_app.compute_count += 1
        return jsonify({
            "ok": True,
            "requested_environments": plan["requested_environments"],
            "environments": sorted(plan["enabled_environments"]),
            "symbols": plan["requested_symbols"],
            "processed": processed,
            "signals": signals_found,
            "persist_signals": plan["persist_signals"],
            "errors": errors,
            "rollup": rollup_results,
            "engines": len(api_app.engines),
            "elapsed_s": round(time.time() - start, 3),
        })
