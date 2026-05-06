from __future__ import annotations

import time

from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.timeframe_utils import normalize_interval

from .startup_preload_config_resolvers import *
from .startup_preload_state_store import *


def _collect_preload_interval_targets(api_app, environment: str, intervals: list[str] | tuple[str, ...] | set[str] | None = None) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    interval_filter = {str(interval or "").strip().lower() for interval in (intervals or []) if str(interval or "").strip()}
    cursor_map = api_app.collect_environment_cursor_map(environment) or {}
    for raw_key, raw_value in cursor_map.items():
        symbol, interval = api_app.parse_compute_cursor_key(raw_key)
        interval = str(interval or "").strip().lower()
        target_ms = int(raw_value or 0)
        if not symbol or not interval or target_ms <= 0:
            continue
        if interval_filter and interval not in interval_filter:
            continue
        grouped.setdefault(interval, {})[symbol] = target_ms

    ordered_groups = {}
    for interval in list(getattr(api_app, "INTERVALS", None) or []):
        targets = grouped.pop(interval, {})
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    for interval in sorted(grouped):
        targets = grouped[interval]
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    return ordered_groups


def _filter_preload_interval_targets(
    interval_targets: dict[str, dict[str, int]],
    symbols: list[str] | tuple[str, ...] | set[str],
) -> dict[str, dict[str, int]]:
    allowed = {
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    }
    if not allowed:
        return {}
    filtered: dict[str, dict[str, int]] = {}
    for interval, targets in (interval_targets or {}).items():
        kept = {
            str(symbol or "").strip().upper(): int(target_ms or 0)
            for symbol, target_ms in (targets or {}).items()
            if str(symbol or "").strip().upper() in allowed and int(target_ms or 0) > 0
        }
        if kept:
            filtered[interval] = {symbol: kept[symbol] for symbol in sorted(kept)}
    return filtered


def _resolve_preload_watchlist_symbols(api_app, environment: str) -> list[str]:
    try:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        rows = api_app.pb.get_all_records(
            "watchlist",
            filter=(
                f'environment = "{runtime_environment}" '
                '|| environment = "global" '
                '|| environment = ""'
            ),
            sort="-updated",
            max_pages=30,
        )
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, runtime_environment: 2}
        for row in rows:
            symbol = str((row or {}).get("symbol", "")).strip().upper()
            if not symbol:
                continue
            row_environment = str((row or {}).get("environment", "") or "").strip().lower()
            rank = priority.get(row_environment, -1)
            if rank < 0:
                continue
            if symbol in applied and applied[symbol] > rank:
                continue
            applied[symbol] = rank
            merged[symbol] = True
        if merged:
            return sorted(merged.keys())
    except Exception:
        LOGGER.exception("Compute startup preload watchlist resolve failed: env=%s", environment)

    metadata = getattr(api_app, "symbol_metadata_cache", {}) or {}
    return sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in metadata.keys()
            if str(symbol or "").strip()
        }
    )


def _resolve_startup_direct_core_symbols(api_app, environment: str) -> list[str]:
    """Resolve the full data universe that must stay warm all day.

    Trading windows decide whether orders may be placed, but compute readiness
    should not degrade outside regular hours just because there are no active
    trade targets yet.
    """

    runtime_environment = str(environment or "live").strip().lower() or "live"
    symbols: set[str] = set()
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        try:
            market_date = api_app.current_market_date() if hasattr(api_app, "current_market_date") else ""
            if market_date:
                rows = pb.get_all_records(
                    "ibkr_targets",
                    filter=(
                        f'date = "{_pb_filter_escape(market_date)}" && '
                        f'environment = "{_pb_filter_escape(runtime_environment)}" && '
                        '(status = "active" || status = "candidate")'
                    ),
                    max_pages=10,
                )
                symbols.update(
                    str((row or {}).get("symbol") or "").strip().upper()
                    for row in rows or []
                    if str((row or {}).get("symbol") or "").strip()
                )
        except Exception:
            LOGGER.debug("Startup direct target symbols unavailable", exc_info=True)

        try:
            rows = pb.get_all_records(
                "watchlist",
                filter=(
                    f'(environment = "{_pb_filter_escape(runtime_environment)}" '
                    '|| environment = "global" '
                    '|| environment = "") && '
                    'symbol_role = "market_monitor"'
                ),
                sort="-updated",
                max_pages=10,
            )
            symbols.update(
                str((row or {}).get("symbol") or "").strip().upper()
                for row in rows or []
                if str((row or {}).get("symbol") or "").strip()
            )
        except Exception:
            LOGGER.debug("Startup direct monitor symbols unavailable", exc_info=True)

    symbols.update(_resolve_preload_watchlist_symbols(api_app, runtime_environment))

    if not symbols:
        try:
            raw = str(api_app.cfg.get_for_environment("ibkr_market_ws_symbols", runtime_environment, "SPY,QQQ,VIX") or "")
        except Exception:
            raw = "SPY,QQQ,VIX"
        symbols.update(str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip())
    return sorted(symbols)


def _startup_direct_backfill_full_watchlist_enabled(api_app, environment: str) -> bool:
    return _get_config_bool(
        api_app,
        "ibkr_startup_direct_backfill_full_watchlist_enabled",
        environment,
        False,
    )


def _resolve_startup_direct_backfill_symbols(api_app, environment: str) -> list[str]:
    """Resolve symbols allowed to use direct IBKR history requests at startup.

    The full data universe is still warmed from storage, but direct IBKR
    requests are intentionally narrower by default so deploy/restart warmup
    cannot overload PocketBase or the gateway with the entire watchlist.
    """

    runtime_environment = str(environment or "live").strip().lower() or "live"
    symbols: set[str] = set()
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        try:
            market_date = api_app.current_market_date() if hasattr(api_app, "current_market_date") else ""
            if market_date:
                rows = pb.get_all_records(
                    "ibkr_targets",
                    filter=(
                        f'date = "{_pb_filter_escape(market_date)}" && '
                        f'environment = "{_pb_filter_escape(runtime_environment)}" && '
                        '(status = "active" || status = "candidate")'
                    ),
                    max_pages=10,
                )
                symbols.update(
                    str((row or {}).get("symbol") or "").strip().upper()
                    for row in rows or []
                    if str((row or {}).get("symbol") or "").strip()
                )
        except Exception:
            LOGGER.debug("Startup direct target symbols unavailable", exc_info=True)

        try:
            rows = pb.get_all_records(
                "watchlist",
                filter=(
                    f'(environment = "{_pb_filter_escape(runtime_environment)}" '
                    '|| environment = "global" '
                    '|| environment = "") && '
                    'symbol_role = "market_monitor"'
                ),
                sort="-updated",
                max_pages=10,
            )
            symbols.update(
                str((row or {}).get("symbol") or "").strip().upper()
                for row in rows or []
                if str((row or {}).get("symbol") or "").strip()
            )
        except Exception:
            LOGGER.debug("Startup direct monitor symbols unavailable", exc_info=True)

    if _startup_direct_backfill_full_watchlist_enabled(api_app, runtime_environment):
        symbols.update(_resolve_preload_watchlist_symbols(api_app, runtime_environment))

    if not symbols:
        try:
            raw = str(api_app.cfg.get_for_environment("ibkr_market_ws_symbols", runtime_environment, "SPY,QQQ,VIX") or "")
        except Exception:
            raw = "SPY,QQQ,VIX"
        symbols.update(str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip())
    return sorted(symbols)


def _pb_filter_escape(value: str) -> str:
    return str(value or "").replace('"', '\\"')


def _count_stored_bars_for_startup_direct_backfill(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    required_bars: int,
    conid_map: dict[str, int] | None = None,
) -> int:
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "get_all_records"):
        return 0

    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    max_pages = max(1, min(20, (max(1, int(required_bars or 0)) + 199) // 200))
    try:
        rows = pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'symbol = "{_pb_filter_escape(normalized_symbol)}" && '
                f'interval = "{_pb_filter_escape(normalized_interval)}" && '
                f'environment = "{_pb_filter_escape(normalized_environment)}"'
            ),
            sort="-bar_time_ms",
            max_pages=max_pages,
        )
        if conid_map is not None and normalized_symbol not in conid_map:
            for row in rows or []:
                conid = _extract_conid_from_bar_row(row)
                if conid > 0:
                    conid_map[normalized_symbol] = conid
                    break
        return len(rows or [])
    except Exception as exc:
        LOGGER.warning(
            "Startup direct backfill count failed: env=%s interval=%s symbol=%s error=%s",
            normalized_environment,
            normalized_interval,
            normalized_symbol,
            exc,
        )
        return 0


def _startup_direct_backfill_freshness_plan(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    required_bars: int,
) -> dict:
    pb = getattr(api_app, "pb", None)
    if pb is None:
        return {"status": "missing", "needs_repair": True, "stored_count": 0, "reason": "pb_unavailable"}
    try:
        planner = BarFreshnessPlanner(pb, getattr(api_app, "cfg", None), environment=environment)
        payload = planner.plan_symbol(
            symbol,
            [interval],
            environment=environment,
            required_bars=required_bars,
        )
        interval_payload = (payload.get("intervals") or {}).get(normalize_interval(interval)) or {}
        return {
            **interval_payload,
            "conid": int(payload.get("conid", 0) or 0),
            "aggregate_status": str(payload.get("status") or ""),
        }
    except Exception as exc:
        LOGGER.warning(
            "Startup direct backfill freshness planning failed: env=%s interval=%s symbol=%s error=%s",
            environment,
            interval,
            symbol,
            exc,
        )
        stored_count = _count_stored_bars_for_startup_direct_backfill(
            api_app,
            environment,
            symbol,
            interval,
            required_bars,
        )
        return {
            "status": "ready" if stored_count >= required_bars else "missing",
            "needs_repair": stored_count < required_bars,
            "stored_count": stored_count,
            "reason": "fallback_count",
        }


def _extract_conid_from_bar_row(row: dict | None) -> int:
    payload = row or {}
    extra = payload.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}

    for value in (
        payload.get("conid"),
        payload.get("conidEx"),
        extra.get("conid"),
        extra.get("conidEx"),
    ):
        try:
            conid = int(float(value or 0))
        except (TypeError, ValueError):
            conid = 0
        if conid > 0:
            return conid
    return 0


def _merge_conids_from_bar_rows(
    result: dict[str, int],
    rows: list[dict] | None,
    wanted: set[str],
) -> None:
    for row in rows or []:
        symbol = str((row or {}).get("symbol") or "").strip().upper()
        if symbol not in wanted or symbol in result:
            continue
        conid = _extract_conid_from_bar_row(row)
        if conid > 0:
            result[symbol] = conid


def _resolve_startup_direct_backfill_conids(api_app, symbols: list[str], environment: str = "live") -> dict[str, int]:
    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols:
        return {}

    result: dict[str, int] = {}
    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_all_records"):
        wanted = set(normalized_symbols)
        safe_environment = _pb_filter_escape(str(environment or "live").strip().lower() or "live")
        scan_pages = _resolve_startup_direct_backfill_conid_scan_pages(api_app, environment)
        for interval in _resolve_startup_direct_backfill_conid_scan_intervals(api_app, environment):
            if len(result) >= len(wanted):
                return result
            try:
                safe_interval = _pb_filter_escape(normalize_interval(interval))
                rows = pb.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'environment = "{safe_environment}" && '
                        f'interval = "{safe_interval}"'
                    ),
                    sort="-bar_time_ms",
                    max_pages=scan_pages,
                )
                _merge_conids_from_bar_rows(result, rows, wanted)
            except Exception as exc:
                LOGGER.warning(
                    "Startup direct backfill bar conid read failed: env=%s interval=%s error=%s",
                    environment,
                    interval,
                    exc,
                )

        try:
            rows = pb.get_all_records("ibkr_conid_cache", max_pages=20)
            for row in rows or []:
                symbol = str((row or {}).get("symbol") or "").strip().upper()
                conid = int((row or {}).get("conid") or 0)
                if symbol in wanted and conid > 0:
                    result[symbol] = conid
            if len(result) >= len(wanted):
                return result
        except Exception as exc:
            text = str(exc or "")
            if "Missing collection context" in text or "status=404" in text:
                LOGGER.debug("Startup direct backfill PB conid cache unavailable: %s", exc)
            else:
                LOGGER.warning("Startup direct backfill PB conid cache read failed: %s", exc)

    missing_symbols = [symbol for symbol in normalized_symbols if symbol not in result]
    if not missing_symbols:
        return result
    if not (
        _startup_direct_backfill_resolve_missing_conids_enabled(api_app, environment)
        and _startup_direct_backfill_live_conid_resolution_enabled(api_app, environment)
    ):
        return result

    resolver = getattr(api_app, "conid_resolver", None)
    if resolver is not None and hasattr(resolver, "resolve_bulk"):
        try:
            result.update({
                str(symbol or "").strip().upper(): int(conid)
                for symbol, conid in (resolver.resolve_bulk(missing_symbols) or {}).items()
                if str(symbol or "").strip() and int(conid or 0) > 0
            })
            missing_symbols = [symbol for symbol in normalized_symbols if symbol not in result]
            if not missing_symbols:
                return result
        except Exception as exc:
            LOGGER.warning("Startup direct backfill existing conid resolver failed: %s", exc)

    try:
        from ibkr_compute.broker import BrokerAdapter
        from ibkr_compute.market.conid_resolver import ConidResolver

        resolver = ConidResolver(
            pb_client=getattr(api_app, "pb", None),
            broker=BrokerAdapter(
                client_id=_resolve_startup_direct_backfill_client_id(api_app, environment),
            ),
        )
        try:
            resolver.load_cache_from_pb()
        except Exception:
            LOGGER.debug("Startup direct backfill conid cache load failed", exc_info=True)
        result.update({
            str(symbol or "").strip().upper(): int(conid)
            for symbol, conid in (resolver.resolve_bulk(missing_symbols) or {}).items()
            if str(symbol or "").strip() and int(conid or 0) > 0
        })
        return result
    except Exception as exc:
        LOGGER.warning("Startup direct backfill conid resolve failed: %s", exc)
        return result


def _startup_direct_backfill_symbol_meta(api_app, symbols: list[str]) -> dict[str, dict]:
    metadata = getattr(api_app, "symbol_metadata_cache", {}) or {}
    result = {}
    for symbol in symbols or []:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        result[normalized_symbol] = dict(metadata.get(normalized_symbol) or {})
    return result


def _materialize_startup_direct_interval(api_app, environment: str, symbols: list[str], interval: str) -> tuple[int, int]:
    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols or not hasattr(api_app, "materialize_engines_from_storage"):
        return 0, 0

    results = api_app.materialize_engines_from_storage(
        environment,
        normalized_symbols,
        normalize_interval(interval),
        hydrate_signal_state=True,
        persist_latest_indicator=False,
    )
    ready_count = sum(1 for payload in (results or {}).values() if bool((payload or {}).get("is_ready")))
    seeded_count = sum(1 for payload in (results or {}).values() if bool((payload or {}).get("indicator_seeded")))
    return ready_count, seeded_count


def _run_startup_direct_backfill_environment(
    api_app,
    summary: dict,
    environment: str,
    symbols: list[str],
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    direct_state = summary.setdefault(
        "direct_backfill",
        _new_direct_backfill_state(api_app, normalized_environment),
    )
    enabled = resolve_startup_direct_backfill_enabled(api_app, normalized_environment)
    intervals = resolve_startup_direct_backfill_intervals(api_app, normalized_environment)
    required_bars = resolve_startup_direct_backfill_required_bars(api_app, normalized_environment)
    direct_state.update(
        {
            "enabled": enabled,
            "intervals": intervals,
            "required_bars": required_bars,
        }
    )

    if not enabled:
        direct_state["status"] = "disabled"
        direct_state["running"] = False
        direct_state["reason"] = "disabled"
        return direct_state
    if not intervals:
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = "no_direct_backfill_intervals"
        return direct_state

    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols:
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = "no_direct_backfill_symbols"
        return direct_state

    if float(direct_state.get("started_at") or 0.0) <= 0.0:
        direct_state["started_at"] = time.time()
    direct_state["status"] = "running"
    direct_state["running"] = True
    direct_state["reason"] = ""

    env_state = {
        "status": "running",
        "intervals": {},
        "symbol_count": len(normalized_symbols),
        "symbol_completed": 0,
        "planned_total": 0,
        "written": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "missing_conid_symbols": [],
        "error": "",
    }
    direct_state.setdefault("results", {})[normalized_environment] = env_state
    direct_state["symbol_total"] += len(normalized_symbols) * len(intervals)
    _publish_startup_preload_state(api_app, summary)

    interval_targets: dict[str, list[str]] = {}
    planning_conid_map: dict[str, int] = {}
    for interval in intervals:
        normalized_interval = normalize_interval(interval)
        interval_state = {
            "status": "planning",
            "symbol_count": len(normalized_symbols),
            "symbol_completed": 0,
            "backfill_symbols_total": 0,
            "freshness_status_counts": {},
            "stale_symbols": [],
            "missing_symbols": [],
            "written": 0,
            "ready_count": 0,
            "indicator_seeded": 0,
            "missing_conid_symbols": [],
            "request_period": _resolve_startup_direct_backfill_period(
                api_app,
                normalized_environment,
                normalized_interval,
            ),
            "error": "",
        }
        env_state["intervals"][normalized_interval] = interval_state
        targets = []
        for symbol in normalized_symbols:
            freshness = _startup_direct_backfill_freshness_plan(
                api_app,
                normalized_environment,
                symbol,
                normalized_interval,
                required_bars,
            )
            conid = int(freshness.get("conid", 0) or 0)
            if conid > 0 and symbol not in planning_conid_map:
                planning_conid_map[symbol] = conid
            freshness_status = str(freshness.get("status") or "unknown")
            interval_state["freshness_status_counts"][freshness_status] = int(
                interval_state["freshness_status_counts"].get(freshness_status, 0) or 0
            ) + 1
            if freshness_status == "stale":
                interval_state["stale_symbols"].append(symbol)
            elif freshness_status in {"missing", "gap", "failed", "unknown"}:
                interval_state["missing_symbols"].append(symbol)
            if bool(freshness.get("needs_repair")):
                targets.append(symbol)
        interval_targets[normalized_interval] = targets
        interval_state["backfill_symbols_total"] = len(targets)
        env_state["planned_total"] += len(targets)
        direct_state["planned_total"] += len(targets)
        _publish_startup_preload_state(api_app, summary)

    all_backfill_symbols = sorted({symbol for targets in interval_targets.values() for symbol in targets})
    conid_map = {
        symbol: int(planning_conid_map.get(symbol) or 0)
        for symbol in all_backfill_symbols
        if int(planning_conid_map.get(symbol) or 0) > 0
    }
    unresolved_backfill_symbols = [
        symbol
        for symbol in all_backfill_symbols
        if int(conid_map.get(symbol) or 0) <= 0
    ]
    direct_state["conid_status"] = "resolving" if unresolved_backfill_symbols else ("completed" if all_backfill_symbols else "skipped")
    direct_state["resolved_conids"] = len(conid_map)
    for interval, targets in interval_targets.items():
        if targets:
            env_state["intervals"][interval]["status"] = "resolving_conids"
    _publish_startup_preload_state(api_app, summary)
    if unresolved_backfill_symbols:
        conid_map.update(
            _resolve_startup_direct_backfill_conids(
                api_app,
                unresolved_backfill_symbols,
                normalized_environment,
            )
        )
    missing_all_conids = [symbol for symbol in all_backfill_symbols if int(conid_map.get(symbol) or 0) <= 0]
    direct_state["conid_status"] = "completed"
    direct_state["resolved_conids"] = len(conid_map)
    direct_state["missing_conid_total"] = len(missing_all_conids)
    env_state["missing_conid_symbols"] = missing_all_conids
    _publish_startup_preload_state(api_app, summary)
    symbol_meta = _startup_direct_backfill_symbol_meta(api_app, normalized_symbols)
    writer = None
    backfill = None
    broker = None
    try:
        if all_backfill_symbols:
            from ibkr_compute.broker import BrokerAdapter
            from ibkr_compute.market.data_backfill import DataBackfill
            from ibkr_compute.market.data_writer import DataWriter

            writer = DataWriter(
                pb_client=getattr(api_app, "pb", None),
                config=getattr(api_app, "cfg", None),
                environment=normalized_environment,
            )
            broker = BrokerAdapter(
                client_id=_resolve_startup_direct_backfill_client_id(
                    api_app,
                    normalized_environment,
                ),
            )
            backfill = DataBackfill(
                data_writer=writer,
                config=getattr(api_app, "cfg", None),
                environment=normalized_environment,
                broker=broker,
            )

        ordered_intervals = [
            normalize_interval(interval)
            for interval in intervals
        ]
        ordered_intervals = sorted(
            ordered_intervals,
            key=lambda interval: 0 if interval_targets.get(interval) else 1,
        )

        for normalized_interval in ordered_intervals:
            interval_state = env_state["intervals"][normalized_interval]
            interval_state["status"] = "running"
            _publish_startup_preload_state(api_app, summary)
            target_symbols = list(interval_targets.get(normalized_interval) or [])
            missing_conid_symbols = [symbol for symbol in target_symbols if int(conid_map.get(symbol) or 0) <= 0]
            interval_state["missing_conid_symbols"] = missing_conid_symbols
            env_state["missing_conid_symbols"] = sorted(
                set(env_state.get("missing_conid_symbols") or []).union(missing_conid_symbols)
            )

            runnable_conids = {
                symbol: int(conid_map.get(symbol) or 0)
                for symbol in target_symbols
                if int(conid_map.get(symbol) or 0) > 0
            }
            written = 0
            if runnable_conids and backfill is not None:
                request_period = str(interval_state.get("request_period") or "").strip()
                period_overrides = {
                    symbol: {normalized_interval: request_period}
                    for symbol in runnable_conids
                }
                results = backfill.backfill_all(
                    runnable_conids,
                    symbol_meta=symbol_meta,
                    intervals=[normalized_interval],
                    repair_symbols=list(runnable_conids.keys()),
                    period_overrides=period_overrides,
                )
                if writer is not None:
                    writer.flush()
                written = sum(
                    int((payload or {}).get(normalized_interval, 0) or 0)
                    for payload in (results or {}).values()
                )
                interval_state["written"] = written
                env_state["written"] += written
                direct_state["written"] += written
                _publish_startup_preload_state(api_app, summary)

            if not target_symbols:
                interval_state["reason"] = "no_backfill_needed"
                interval_state["symbol_completed"] = len(normalized_symbols)
                interval_state["status"] = "completed"
                env_state["symbol_completed"] += len(normalized_symbols)
                direct_state["symbol_completed"] += len(normalized_symbols)
                _publish_startup_preload_state(api_app, summary)
                continue
            if written <= 0:
                interval_state["reason"] = "no_bars_written"
                interval_state["symbol_completed"] = len(normalized_symbols)
                interval_state["status"] = "completed"
                env_state["symbol_completed"] += len(normalized_symbols)
                direct_state["symbol_completed"] += len(normalized_symbols)
                _publish_startup_preload_state(api_app, summary)
                continue

            materialize_symbols = target_symbols
            interval_state["status"] = "materializing"
            already_ready_count = max(0, len(normalized_symbols) - len(materialize_symbols))
            interval_state["symbol_completed"] = already_ready_count
            env_state["symbol_completed"] += already_ready_count
            direct_state["symbol_completed"] += already_ready_count
            _publish_startup_preload_state(api_app, summary)
            ready_count = 0
            indicator_seeded = 0
            for symbol in materialize_symbols:
                symbol_ready, symbol_seeded = _materialize_startup_direct_interval(
                    api_app,
                    normalized_environment,
                    [symbol],
                    normalized_interval,
                )
                ready_count += symbol_ready
                indicator_seeded += symbol_seeded
                interval_state["ready_count"] = ready_count
                interval_state["indicator_seeded"] = indicator_seeded
                interval_state["symbol_completed"] += 1
                env_state["symbol_completed"] += 1
                env_state["ready_count"] += symbol_ready
                env_state["indicator_seeded"] += symbol_seeded
                direct_state["symbol_completed"] += 1
                direct_state["ready_count"] += symbol_ready
                _publish_startup_preload_state(api_app, summary)
            interval_state["status"] = "completed"
            _publish_startup_preload_state(api_app, summary)
    except Exception as exc:
        LOGGER.exception("Startup direct backfill failed: env=%s", normalized_environment)
        env_state["status"] = "failed"
        env_state["error"] = str(exc)
        direct_state["status"] = "failed"
        direct_state["error"] = str(exc)
        _publish_startup_preload_state(api_app, summary)
    finally:
        if backfill is not None:
            status = backfill.status()
            direct_state["request_count"] += int(status.get("request_count", 0) or 0)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                LOGGER.debug("Startup direct backfill writer close failed", exc_info=True)
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                LOGGER.debug("Startup direct backfill broker disconnect failed", exc_info=True)

    if env_state.get("status") != "failed":
        env_state["status"] = "completed"
    if direct_state.get("status") != "failed":
        direct_state["status"] = "completed"
        direct_state["error"] = ""
    direct_state["running"] = False
    direct_state["finished_at"] = time.time()
    _publish_startup_preload_state(api_app, summary)
    return direct_state


__all__ = [name for name in globals() if not name.startswith("__")]
