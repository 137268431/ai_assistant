from __future__ import annotations

from .market_universe_support import *
from .market_universe_support import (
    _classify_daily_scan_failure,
    _compact_daily_scan_diagnostics,
    _compact_daily_scan_result_for_state,
    _daily_scan_all_snapshotless,
    _daily_scan_running_age_seconds,
    _extract_daily_scan_failure_evidence,
    _parse_iso_datetime,
    _safe_extra,
    _safe_float,
    _safe_int,
    _service_mod,
    _target_row_is_manual,
)

from . import market_universe_support as _market_universe_support


def _service_mod():
    # Keep tests and legacy callers that patch market_universe._service_mod effective.
    import sys

    facade = sys.modules.get(f"{__package__}.market_universe")
    patched = getattr(facade, "_service_mod", None) if facade is not None else None
    if patched is not None:
        return patched()
    return _market_universe_support._service_mod()


class TradingServiceMarketUniverseReconcileMixin:
    def _normalize_runtime_symbols(self, symbols) -> list[str]:
        normalized = []
        seen = set()
        for item in symbols or []:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
        return normalized

    def _select_latest_valid_prime_signals(self, captured_signals, allowed_symbols=None) -> dict:
        allowed = set(self._normalize_runtime_symbols(allowed_symbols))
        grouped: dict[str, list[dict]] = {}
        evaluated: list[dict] = []
        selected: list[dict] = []

        for item in captured_signals or []:
            symbol = str((item or {}).get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if allowed and symbol not in allowed:
                continue
            grouped.setdefault(symbol, []).append(dict(item or {}))

        for symbol, rows in grouped.items():
            rows.sort(key=lambda row: int(row.get("bar_time_ms", 0) or 0), reverse=True)
            for row in rows:
                signal_payload = {
                    "signal_id": str(row.get("signal_id") or ""),
                    "symbol": symbol,
                    "direction": str(row.get("direction") or "").strip(),
                    "entry": float(row.get("entry", 0) or 0),
                    "stop_loss": float(row.get("stop_loss", 0) or 0),
                    "take_profit": float(row.get("take_profit", 0) or 0),
                    "shares": int(row.get("shares", 0) or 0),
                    "signal_time": row.get("us_time") or "",
                }
                valid, reason = self.signal_processor.validate_signal(signal_payload)
                evaluated.append(
                    {
                        "symbol": symbol,
                        "signal_id": str(row.get("signal_id") or ""),
                        "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                        "valid": bool(valid),
                        "reason": str(reason or ""),
                    }
                )
                if not valid:
                    continue

                extra = dict(row.get("extra") or {})
                extra.update(
                    {
                        "universe_prime": True,
                        "universe_prime_source": "manual_pool_reconcile",
                        "universe_prime_triggered_at": self._now_iso(),
                    }
                )
                row["extra"] = extra
                selected.append(row)
                break

        return {
            "symbols": sorted(grouped.keys()),
            "selected_signals": selected,
            "evaluated": evaluated,
        }

    def _persist_prime_signals(self, selected_signals) -> dict:
        service_mod = _service_mod()
        persisted = []
        errors = []
        signal_ids = []

        for item in selected_signals or []:
            payload = dict(item or {})
            signal_id = str(payload.get("signal_id") or "").strip()
            if not signal_id:
                continue
            payload["environment"] = service_mod.DATA_ENVIRONMENT
            payload["broker_mode"] = service_mod.ENVIRONMENT
            payload["data_environment"] = service_mod.DATA_ENVIRONMENT
            try:
                result = self.pb.upsert_signal(payload)
                persisted.append(
                    {
                        "signal_id": signal_id,
                        "symbol": str(payload.get("symbol") or "").strip().upper(),
                        "action": str(result.get("action") or ""),
                        "status": str(result.get("status") or ""),
                    }
                )
                signal_ids.append(signal_id)
            except Exception as exc:
                errors.append({"signal_id": signal_id, "error": str(exc)})

        if signal_ids:
            self.signal_router.forget_processed(signal_ids)
            self._signal_wakeup.set()

        return {
            "persisted": persisted,
            "errors": errors,
            "signal_ids": signal_ids,
        }

    def _prime_universe_symbols(self, symbols, *, emit_signals: bool = False, source: str = "universe_prime") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        backfill_result: dict = {"ok": True, "symbols": [], "resolved": []}
        conid_map = {}
        try:
            conid_map = self.conid_resolver.resolve_bulk(normalized_symbols)
        except Exception as exc:
            backfill_result = {"ok": False, "symbols": normalized_symbols, "error": str(exc)}

        if conid_map:
            try:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())
                backfill_result = {
                    "ok": True,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "missing": sorted(set(normalized_symbols) - set(conid_map.keys())),
                }
            except Exception as exc:
                backfill_result = {
                    "ok": False,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "error": str(exc),
                }

        backtest_preload_result = {
            "ok": True,
            "available": False,
            "skipped": True,
            "reason": "coordinator_unavailable",
            "symbols": normalized_symbols,
        }
        try:
            coordinator = getattr(self, "backtest_preload_coordinator", None)
            if coordinator is None:
                from ibkr_compute.api import server as compute_server

                coordinator = getattr(compute_server, "backtest_preload_coordinator", None)
            if coordinator is not None and hasattr(coordinator, "enqueue"):
                backtest_preload_result = coordinator.enqueue(
                    normalized_symbols,
                    environment=service_mod.DATA_ENVIRONMENT,
                    trigger=str(source or "universe_prime"),
                    reason="new_universe_symbol_default_backtest_preload",
                    source_payload={
                        "source": str(source or "universe_prime"),
                        "emit_signals": bool(emit_signals),
                        "resolved_symbols": sorted(conid_map.keys()),
                    },
                )
            elif coordinator is None:
                backtest_preload_result["available"] = False
        except Exception as exc:
            backtest_preload_result = {
                "ok": False,
                "available": True,
                "skipped": True,
                "error": str(exc),
                "symbols": normalized_symbols,
            }

        compute_result = {}
        selected_signal_result = {"symbols": [], "selected_signals": [], "evaluated": []}
        persisted_signal_result = {"persisted": [], "errors": [], "signal_ids": []}
        interval_prime_started = False
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_result = compute_server._run_internal_compute(
                    {
                        "source": "targeted_recompute",
                        "environments": [service_mod.DATA_ENVIRONMENT],
                        "broker_mode": service_mod.ENVIRONMENT,
                        "data_environment": service_mod.DATA_ENVIRONMENT,
                        "symbols": normalized_symbols,
                        "force_rollup": True,
                        "persist_signals": False,
                        "capture_signals": bool(emit_signals),
                    }
                ) or {}
        except Exception as exc:
            compute_result = {"ok": False, "error": str(exc)}

        if emit_signals:
            selected_signal_result = self._select_latest_valid_prime_signals(
                (compute_result or {}).get("captured_signals") or [],
                allowed_symbols=normalized_symbols,
            )
            persisted_signal_result = self._persist_prime_signals(
                selected_signal_result.get("selected_signals") or []
            )

        try:
            interval_prime_started = self._schedule_interval_prime(
                normalized_symbols,
                source=str(source or "universe_prime"),
            )
        except Exception:
            interval_prime_started = False

        return {
            "ok": bool((compute_result or {}).get("ok", True)) and bool(backfill_result.get("ok", True)),
            "symbols": normalized_symbols,
            "emit_signals": bool(emit_signals),
            "backfill": backfill_result,
            "backtest_preload": backtest_preload_result,
            "compute": compute_result,
            "signal_selection": selected_signal_result,
            "signal_persist": persisted_signal_result,
            "interval_prime_started": interval_prime_started,
        }

    def _cleanup_universe_symbols(self, symbols, *, source: str = "universe_cleanup") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        removed_conids = []
        with self._subscription_lock:
            removed_conids = sorted(
                {
                    int(conid)
                    for symbol, conid in self._active_subscription_map.items()
                    if symbol in normalized_symbols and int(conid or 0) > 0
                }
            )

        if removed_conids:
            self.bar_aggregator.remove_conids(removed_conids)
            self.realtime_quote_book.remove_conids(removed_conids)
            for conid in removed_conids:
                try:
                    self.ws_client.unsubscribe(conid)
                except Exception:
                    service_mod.logger.warning("Universe cleanup unsubscribe failed: conid=%s", conid, exc_info=True)

        for symbol in normalized_symbols:
            self._quote_prev_close_cache.pop(symbol, None)
        self._last_backfill_symbols = [symbol for symbol in self._last_backfill_symbols if symbol not in normalized_symbols]
        self._last_active_repair_symbols = [symbol for symbol in self._last_active_repair_symbols if symbol not in normalized_symbols]
        self._last_history_repair_symbols = [symbol for symbol in self._last_history_repair_symbols if symbol not in normalized_symbols]
        self._last_pipeline_repair_symbols = [symbol for symbol in self._last_pipeline_repair_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_symbols = [symbol for symbol in self._last_watchlist_integrity_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_repair_symbols = [
            symbol for symbol in self._last_watchlist_integrity_repair_symbols if symbol not in normalized_symbols
        ]

        compute_reset = {}
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_reset = compute_server.reset_compute_state_for_symbols(
                    service_mod.DATA_ENVIRONMENT,
                    normalized_symbols,
                )
                compute_server.persist_compute_cursors(service_mod.DATA_ENVIRONMENT)
        except Exception as exc:
            compute_reset = {"ok": False, "error": str(exc)}

        sqlite_result = {}
        try:
            from ibkr_compute.market.pocketbase_sqlite import delete_symbol_runtime_data, open_pb_sqlite

            with open_pb_sqlite(readonly=False) as conn:
                sqlite_result = delete_symbol_runtime_data(conn, service_mod.DATA_ENVIRONMENT, normalized_symbols)
                conn.commit()
        except Exception as exc:
            sqlite_result = {"ok": False, "error": str(exc), "symbols": normalized_symbols}

        deleted_signal_ids = list((sqlite_result or {}).get("deleted_signal_ids") or [])
        if deleted_signal_ids:
            self.signal_router.forget_processed(deleted_signal_ids)

        return {
            "ok": not bool((sqlite_result or {}).get("error")) and not bool((compute_reset or {}).get("error")),
            "symbols": normalized_symbols,
            "source": str(source or "universe_cleanup"),
            "removed_conids": removed_conids,
            "compute_reset": compute_reset,
            "sqlite": sqlite_result,
        }

    def reconcile_market_universe(
        self,
        *,
        prime_symbols=None,
        cleanup_symbols=None,
        emit_signals: bool = False,
        source: str = "runtime_api",
        reason: str = "manual_reconcile",
    ) -> dict:
        service_mod = _service_mod()
        prime_list = self._normalize_runtime_symbols(prime_symbols)
        cleanup_list = self._normalize_runtime_symbols(cleanup_symbols)

        self.config.refresh()
        self._refresh_runtime_settings()
        self._reset_for_new_market_day(force=False)
        self._refresh_watchlist_pool(force=True)

        target_refresh = {
            "ok": True,
            "skipped": not self.session_keeper.is_authenticated,
            "reason": "session_unauthenticated" if not self.session_keeper.is_authenticated else "",
        }
        if self.session_keeper.is_authenticated:
            try:
                self._refresh_target_subscriptions(force=True, reason=reason or "manual_reconcile")
                target_refresh = {
                    "ok": True,
                    "skipped": False,
                    "active_target_date": self._active_target_date,
                    "active_target_symbols": list(self._active_trade_symbols),
                    "active_subscription_symbols": list(self._active_subscription_symbols),
                }
            except Exception as exc:
                target_refresh = {"ok": False, "skipped": False, "error": str(exc)}

        prime_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if prime_list:
            prime_result = self._prime_universe_symbols(
                prime_list,
                emit_signals=emit_signals,
                source=source or "runtime_api",
            )

        cleanup_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if cleanup_list:
            cleanup_result = self._cleanup_universe_symbols(
                cleanup_list,
                source=source or "runtime_api",
            )

        return {
            "ok": bool(target_refresh.get("ok", True)) and bool(prime_result.get("ok", True)) and bool(cleanup_result.get("ok", True)),
            "environment": service_mod.DATA_ENVIRONMENT,
            "broker_mode": service_mod.ENVIRONMENT,
            "data_environment": service_mod.DATA_ENVIRONMENT,
            "market_date": str(self._current_market_date or self._market_date()),
            "source": str(source or "runtime_api"),
            "reason": str(reason or "manual_reconcile"),
            "emit_signals": bool(emit_signals),
            "target_refresh": target_refresh,
            "prime": prime_result,
            "cleanup": cleanup_result,
        }
