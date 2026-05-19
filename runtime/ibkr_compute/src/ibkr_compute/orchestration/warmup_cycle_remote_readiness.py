from __future__ import annotations

import time

from .warmup_cycle_support import _service_mod

class WarmupCycleRemoteReadinessMixin:
    def _warmup_uses_remote_compute_service(self) -> bool:
        from ibkr_compute.api.service_topology import uses_remote_compute_service

        return uses_remote_compute_service()

    def _load_warmup_compute_cursors(self) -> int:
        service_mod = _service_mod()
        if self._warmup_uses_remote_compute_service():
            return 0
        from ibkr_compute.api import server as compute_server

        with compute_server.compute_lock:
            return int(compute_server.load_persisted_compute_cursors(service_mod.DATA_ENVIRONMENT) or 0)

    def _materialize_warmup_compute_symbols(
        self,
        symbols: list[str] | None,
        *,
        hydrate_signal_state: bool = True,
        persist_latest_indicator: bool = False,
    ) -> dict:
        service_mod = _service_mod()
        if self._warmup_uses_remote_compute_service():
            return {}
        from ibkr_compute.api import server as compute_server

        return compute_server.materialize_engines_from_storage(
            service_mod.DATA_ENVIRONMENT,
            symbols or [],
            service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
            hydrate_signal_state=hydrate_signal_state,
            persist_latest_indicator=persist_latest_indicator,
        )

    def _trigger_remote_warmup_prime(self, symbols: list[str] | None, *, source: str = "warmup") -> dict:
        service_mod = _service_mod()
        if not self._warmup_uses_remote_compute_service():
            return {"ok": True, "skipped": True, "reason": "local_compute_mode"}
        normalized_symbols = self._normalize_symbol_list(symbols or [])
        if not normalized_symbols:
            return {"ok": True, "skipped": True, "reason": "empty_symbols"}
        from ibkr_compute.api.compute_status_client import trigger_remote_prime

        # Keep the synchronous pending-symbol prime on the hard gate only; the
        # background interval prime/topup handles higher timeframes separately.
        intervals = [service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL]
        chunk_size = max(1, int(getattr(service_mod, "STARTUP_BACKGROUND_PRIME_CHUNK_SIZE", 16) or 16))
        results = []
        errors = 0
        for index in range(0, len(normalized_symbols), chunk_size):
            chunk = normalized_symbols[index:index + chunk_size]
            result = trigger_remote_prime({
                "environments": [service_mod.DATA_ENVIRONMENT],
                "market_data_mode": service_mod.DATA_ENVIRONMENT,
                "broker_mode": service_mod.ENVIRONMENT,
                "symbols": chunk,
                "intervals": intervals,
                "persist_latest_indicator": False,
                "source": source,
            })
            results.append(result)
            if result.get("ok") is False:
                errors += 1
        return {
            "ok": errors == 0,
            "errors": errors,
            "chunks": len(results),
            "symbols": normalized_symbols,
            "intervals": intervals,
            "results": results,
        }

    def _schedule_remote_warmup_retry_if_needed(self, readiness: dict) -> bool:
        if not self._warmup_uses_remote_compute_service():
            return False
        pending_sources = {
            str((item or {}).get("source") or "").strip()
            for item in (readiness.get("symbol_status") or [])
            if not bool((item or {}).get("ready"))
        }
        retryable_sources = {
            "remote_compute_status_missing",
            "remote_compute_status_unavailable",
        }
        if not pending_sources or not pending_sources.issubset(retryable_sources):
            return False

        service_mod = _service_mod()
        retry_delay_s = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_warmup_remote_compute_retry_sec",
                service_mod.DATA_ENVIRONMENT,
                5,
            ),
        )
        service_mod.logger.info(
            "Warmup waiting for remote compute readiness: sources=%s retry_in=%ss",
            ",".join(sorted(pending_sources)),
            retry_delay_s,
        )
        if self._running:
            time.sleep(retry_delay_s)
            if self._running:
                self._warmup_wakeup.set()
                return True
        return False

    def _collect_remote_warmup_readiness(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        from ibkr_compute.api.compute_status_client import (
            get_remote_compute_status,
            is_compute_status_payload,
        )

        required_interval = service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL
        remote_symbols = self._normalize_symbol_list(
            snapshot.get("subscription_symbols")
            or list((snapshot.get("trade_symbols") or [])) + list((snapshot.get("monitor_symbols") or []))
            or snapshot.get("symbols")
            or []
        )
        remote_symbol_set = set(remote_symbols)
        payload = get_remote_compute_status(
            force_refresh=True,
            symbols=remote_symbols,
            include_engines=False,
        )
        if not is_compute_status_payload(payload):
            readiness = self._build_warmup_readiness(
                snapshot,
                {
                    symbol: {
                        "ready": symbol not in remote_symbol_set,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "source": (
                            "scan_pool_non_blocking"
                            if symbol not in remote_symbol_set
                            else "remote_compute_status_unavailable"
                        ),
                    }
                    for symbol in snapshot["symbols"]
                },
                required_interval=required_interval,
            )
            readiness["phase"] = "degraded"
            readiness["pending_symbols"] = []
            readiness["trading_gate_open"] = False
            readiness["trading_gate_reason"] = "remote_compute_status_unavailable"
            return readiness
        engines = payload.get("engines") or {}
        readiness_interval = (
            ((payload.get("multi_timeframe_readiness") or {}).get("intervals") or {}).get(required_interval)
            if is_compute_status_payload(payload)
            else {}
        )
        readiness_all_ready = (
            isinstance(readiness_interval, dict)
            and str(readiness_interval.get("status") or "").strip().lower() == "ready"
            and int(readiness_interval.get("missing_ready_symbols_total", 0) or 0) == 0
        )
        storage_checked = bool((readiness_interval or {}).get("storage_checked"))
        missing_bar_symbol_set = {
            str(symbol or "").strip().upper()
            for symbol in ((readiness_interval or {}).get("missing_bar_symbols") or [])
            if str(symbol or "").strip()
        }
        missing_ready_symbol_set = {
            str(symbol or "").strip().upper()
            for symbol in ((readiness_interval or {}).get("missing_ready_symbols") or [])
            if str(symbol or "").strip()
        }
        missing_ready_total = int((readiness_interval or {}).get("missing_ready_symbols_total", 0) or 0)
        missing_ready_list_complete = len(missing_ready_symbol_set) >= missing_ready_total
        status_by_symbol = {}
        if isinstance(engines, dict):
            for symbol in snapshot["symbols"]:
                if symbol not in remote_symbol_set:
                    status_by_symbol[symbol] = {
                        "ready": True,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "source": "scan_pool_non_blocking",
                    }
                    continue
                engine_state = dict(
                    engines.get(f"{service_mod.DATA_ENVIRONMENT}/{symbol}/{required_interval}") or {}
                )
                if engine_state:
                    status_by_symbol[symbol] = {
                        "ready": bool(engine_state.get("is_ready")),
                        "bar_count": int(engine_state.get("bar_count", 0) or 0),
                        "last_bar_time_ms": int(engine_state.get("last_bar_time_ms", 0) or 0),
                        "source": "remote_compute_status",
                    }
                elif readiness_all_ready:
                    status_by_symbol[symbol] = {
                        "ready": True,
                        "bar_count": 0,
                        "last_bar_time_ms": int(
                            readiness_interval.get("latest_bar_time_ms", 0)
                            or readiness_interval.get("latest_indicator_time_ms", 0)
                            or 0
                        ),
                        "source": "remote_compute_bar_readiness",
                    }
                elif storage_checked and missing_ready_list_complete:
                    source = "remote_compute_bar_readiness"
                    if symbol in missing_bar_symbol_set:
                        source = "remote_compute_bar_missing"
                    elif symbol in missing_ready_symbol_set:
                        source = "remote_compute_status_missing"
                    status_by_symbol[symbol] = {
                        "ready": symbol not in missing_ready_symbol_set,
                        "bar_count": 0,
                        "last_bar_time_ms": int(
                            readiness_interval.get("latest_bar_time_ms", 0)
                            or readiness_interval.get("latest_indicator_time_ms", 0)
                            or 0
                        ),
                        "source": source,
                    }
                else:
                    source = "remote_compute_status_missing"
                    if storage_checked and symbol in missing_bar_symbol_set:
                        source = "remote_compute_bar_missing"
                    status_by_symbol[symbol] = {
                        "ready": False,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "source": source,
                    }
        return self._build_warmup_readiness(
            snapshot,
            status_by_symbol,
            required_interval=required_interval,
        )

    def _preserve_previous_gate_for_transient_remote_readiness(
        self,
        snapshot: dict,
        readiness: dict,
        previous_state: dict | None,
    ) -> dict:
        reason = str(readiness.get("trading_gate_reason") or "").strip().lower()
        if reason not in {"remote_compute_status_unavailable", "remote_compute_status_missing"}:
            return readiness
        previous = self._copy_warmup_state(previous_state)
        if not bool(previous.get("trading_gate_open")):
            return readiness

        symbols = self._normalize_symbol_list(snapshot.get("symbols") or [])
        trade_symbols = self._normalize_symbol_list(snapshot.get("trade_symbols") or [])
        if not symbols or not trade_symbols:
            return readiness
        previous_ready_set = set(self._normalize_symbol_list(previous.get("ready_symbols_list") or []))
        if any(symbol not in previous_ready_set for symbol in trade_symbols):
            return readiness

        integrity_pending_set = set(self._normalize_symbol_list(readiness.get("integrity_pending_symbols") or []))
        if integrity_pending_set.intersection(trade_symbols):
            return readiness

        current_ready_set = set(self._normalize_symbol_list(readiness.get("ready_symbols_list") or []))
        ready_set = (current_ready_set | previous_ready_set.intersection(symbols)) - integrity_pending_set
        if any(symbol not in ready_set for symbol in trade_symbols):
            return readiness

        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(trade_symbols)
        monitor_symbol_set = set(snapshot.get("monitor_symbols") or [])
        current_status = {
            str((item or {}).get("symbol") or "").strip().upper(): dict(item or {})
            for item in (readiness.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        previous_status = {
            str((item or {}).get("symbol") or "").strip().upper(): dict(item or {})
            for item in (previous.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        symbol_status = []
        for symbol in symbols:
            if symbol in trade_symbol_set:
                role = "trade"
            elif symbol in monitor_symbol_set:
                role = "monitor"
            elif symbol in scan_symbol_set:
                role = "scan"
            elif symbol in subscription_symbol_set:
                role = "subscription"
            else:
                role = "data"
            row = dict(current_status.get(symbol) or {})
            row["symbol"] = symbol
            row["role"] = row.get("role") or role
            if symbol in ready_set and not bool(row.get("ready")):
                previous_row = previous_status.get(symbol) or {}
                row["ready"] = True
                row["bar_count"] = int(previous_row.get("bar_count", row.get("bar_count", 0)) or 0)
                row["last_bar_time_ms"] = int(
                    previous_row.get("last_bar_time_ms", row.get("last_bar_time_ms", 0)) or 0
                )
                row["source"] = "previous_warmup_snapshot"
            elif symbol not in ready_set:
                row["ready"] = False
            symbol_status.append(row)

        pending_symbols = [symbol for symbol in symbols if symbol not in ready_set]
        ready_trade_symbols = len([symbol for symbol in trade_symbols if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot.get("monitor_symbols") or [] if symbol in ready_set])
        data_ready = bool(symbols) and not pending_symbols and not integrity_pending_set
        preserved = dict(readiness)
        preserved.update(
            {
                "phase": "ready" if data_ready else "degraded",
                "data_ready": data_ready,
                "trade_allowed": True,
                "ready_symbols": len(ready_set),
                "ready_scan_symbols": len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set]),
                "ready_subscription_symbols": len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set]),
                "ready_trade_symbols": ready_trade_symbols,
                "ready_monitor_symbols": ready_monitor_symbols,
                "ready_symbols_list": [symbol for symbol in symbols if symbol in ready_set],
                "pending_symbols": pending_symbols,
                "symbol_status": symbol_status,
                "trading_gate_open": True,
                "trading_gate_reason": "ready",
                "preserved_previous_success": True,
            }
        )
        return preserved

    def _collect_warmup_readiness(self, snapshot: dict) -> dict:
        service_mod = _service_mod()

        if self._warmup_uses_remote_compute_service():
            return self._collect_remote_warmup_readiness(snapshot)

        from ibkr_compute.api import server as compute_server

        status_by_symbol = {}
        for symbol in snapshot["symbols"]:
            engine = compute_server.engines.get(
                (
                    service_mod.DATA_ENVIRONMENT,
                    symbol,
                    service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
                )
            )
            status_by_symbol[symbol] = {
                "ready": bool(engine and engine.is_ready()),
                "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
                "last_bar_time_ms": (
                    int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0
                ),
                "source": "local_compute_state",
            }

        return self._build_warmup_readiness(
            snapshot,
            status_by_symbol,
            required_interval=service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
        )
