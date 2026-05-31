from __future__ import annotations

import queue
import sys
import threading as _threading
import time

from ibkr_compute.market.timeframe_utils import HIGHER_INTERVALS, normalize_interval


def _facade_module():
    return sys.modules.get("ibkr_compute.orchestration.runtime_pipeline")


def _service_mod():
    facade = _facade_module()
    service_getter = getattr(facade, "_service_mod", None) if facade is not None else None
    if callable(service_getter) and service_getter is not _service_mod:
        return service_getter()
    from . import trading_service as service_mod

    return service_mod


def _data_environment(service_mod) -> str:
    environment = str(
        getattr(service_mod, "DATA_ENVIRONMENT", None)
        or getattr(service_mod, "ENVIRONMENT", None)
        or "live"
    ).strip().lower()
    return environment or "live"


def _broker_environment(service_mod, data_environment: str | None = None) -> str:
    broker = str(getattr(service_mod, "ENVIRONMENT", None) or data_environment or "live").strip().lower()
    return broker or (data_environment or "live")


def _has_explicit_data_environment(service_mod) -> bool:
    return bool(str(getattr(service_mod, "DATA_ENVIRONMENT", "") or "").strip())


class _ThreadingProxy:
    def __getattr__(self, name):
        facade = _facade_module()
        facade_threading = getattr(facade, "threading", None) if facade is not None else None
        target = facade_threading if facade_threading is not None and facade_threading is not self else _threading
        return getattr(target, name)


threading = _ThreadingProxy()


class RuntimePipelineSupportMixin:
    def _load_persisted_compute_cursor_map(self, environment: str) -> dict[str, int]:
            runtime_environment = str(environment or "live").strip().lower() or "live"
            try:
                record = self.pb.get_state("compute_cursors", runtime_environment, date="global")
            except Exception:
                return {}

            payload = (record or {}).get("data") if isinstance(record, dict) else {}
            cursors = payload.get("cursors") if isinstance(payload, dict) else {}
            if not isinstance(cursors, dict):
                return {}

            cursor_map = {}
            for raw_key, raw_value in cursors.items():
                key = str(raw_key or "").strip()
                bar_ms = int(raw_value or 0)
                if key and bar_ms > 0:
                    cursor_map[key] = bar_ms
            return cursor_map

    def _copy_interval_prime_state(self) -> dict:
            with self._interval_prime_lock:
                return dict(self._interval_prime_state)

    def _schedule_interval_prime(self, symbols: list[str], source: str = "startup") -> bool:
            service_mod = _service_mod()
            data_environment = _data_environment(service_mod)
            broker_environment = _broker_environment(service_mod, data_environment)
            normalized_symbols = [
                str(symbol or "").strip().upper()
                for symbol in (symbols or [])
                if str(symbol or "").strip()
            ]
            if not normalized_symbols:
                return False

            with self._interval_prime_lock:
                if self._interval_prime_state.get("running"):
                    return False
                self._interval_prime_state = {
                    **self._interval_prime_state,
                    "running": True,
                    "completed_intervals": [],
                    "symbol_count": len(normalized_symbols),
                    "last_started_at": self._now_iso(),
                    "last_finished_at": "",
                    "last_duration_s": 0.0,
                    "last_error": "",
                    "last_source": source,
                }

            def worker():
                started = time.time()
                completed_intervals = []
                last_error = ""
                try:
                    from ibkr_compute.api.service_topology import uses_remote_compute_service

                    if uses_remote_compute_service():
                        from ibkr_compute.api.compute_status_client import trigger_remote_prime

                        for interval in service_mod.STARTUP_BACKGROUND_PRIME_INTERVALS:
                            if not self._running:
                                break
                            for index in range(
                                0,
                                len(normalized_symbols),
                                service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE,
                            ):
                                if not self._running:
                                    break
                                chunk = normalized_symbols[index:index + service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE]
                                payload = {
                                    "environments": [data_environment],
                                    "symbols": chunk,
                                    "intervals": [interval],
                                    "persist_latest_indicator": False,
                                }
                                if _has_explicit_data_environment(service_mod):
                                    payload.update(
                                        {
                                            "market_data_mode": data_environment,
                                            "broker_mode": broker_environment,
                                        }
                                    )
                                result = trigger_remote_prime(payload)
                                if result.get("ok") is False:
                                    raise RuntimeError(
                                        str(result.get("error") or f"remote_prime_failed:{interval}")
                                    )
                            completed_intervals.append(interval)
                            service_mod.logger.info(
                                "Remote background interval prime finished (%s): interval=%s symbols=%d",
                                source,
                                interval,
                                len(normalized_symbols),
                            )
                        return

                    from ibkr_compute.api import server as compute_server

                    try:
                        with compute_server.compute_lock:
                            compute_server.load_persisted_compute_cursors(data_environment)
                    except Exception as exc:
                        service_mod.logger.warning("Interval prime cursor preload failed: %s", exc)

                    for interval in service_mod.STARTUP_BACKGROUND_PRIME_INTERVALS:
                        if not self._running:
                            break
                        for index in range(
                            0,
                            len(normalized_symbols),
                            service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE,
                        ):
                            if not self._running:
                                break
                            chunk = normalized_symbols[index:index + service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE]
                            with compute_server.compute_lock:
                                compute_server.materialize_engines_from_storage(
                                    data_environment,
                                    chunk,
                                    interval,
                                    persist_latest_indicator=False,
                                )
                        completed_intervals.append(interval)
                        service_mod.logger.info(
                            "Background interval prime finished (%s): interval=%s symbols=%d",
                            source,
                            interval,
                            len(normalized_symbols),
                        )
                except Exception as exc:
                    last_error = str(exc)
                    service_mod.logger.warning("Background interval prime failed (%s): %s", source, exc)
                finally:
                    finished_at = self._now_iso()
                    with self._interval_prime_lock:
                        self._interval_prime_state = {
                            **self._interval_prime_state,
                            "running": False,
                            "completed_intervals": completed_intervals,
                            "last_finished_at": finished_at,
                            "last_duration_s": round(max(0.0, time.time() - started), 3),
                            "last_error": last_error,
                            "last_source": source,
                        }

            self._interval_prime_thread = threading.Thread(
                target=worker,
                daemon=True,
                name="interval-prime",
            )
            self._interval_prime_thread.start()
            return True

    def _trigger_realtime_compute(
            self,
            source: str = "bar_close",
            symbols: list[str] | None = None,
            persist_signals: bool | None = None,
            persist_signal_symbols: list[str] | None = None,
            intervals: list[str] | None = None,
            rollup_intervals: list[str] | None = None,
        ) -> dict:
            service_mod = _service_mod()
            data_environment = _data_environment(service_mod)
            broker_environment = _broker_environment(service_mod, data_environment)
            try:
                payload = {
                    "source": source,
                    "environments": [data_environment],
                }
                if _has_explicit_data_environment(service_mod):
                    payload.update(
                        {
                            "market_data_mode": data_environment,
                            "broker_mode": broker_environment,
                        }
                    )
                if persist_signals is not None:
                    payload["persist_signals"] = bool(persist_signals)
                if persist_signal_symbols is not None:
                    payload["persist_signal_symbols"] = sorted(
                        {
                            str(symbol or "").strip().upper()
                            for symbol in persist_signal_symbols
                            if str(symbol or "").strip()
                        }
                    )
                if intervals is not None:
                    payload["intervals"] = [
                        normalize_interval(interval)
                        for interval in intervals
                        if str(interval or "").strip()
                    ]
                if rollup_intervals is not None:
                    payload["rollup_intervals"] = [
                        normalize_interval(interval)
                        for interval in rollup_intervals
                        if str(interval or "").strip()
                    ]
                normalized_symbols = sorted(
                    {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
                )
                if normalized_symbols:
                    payload["symbols"] = normalized_symbols
                from ibkr_compute.api.service_topology import uses_remote_compute_service

                if uses_remote_compute_service():
                    from ibkr_compute.api.compute_status_client import trigger_remote_compute

                    return trigger_remote_compute(payload)

                from ibkr_compute.api import server as compute_server

                with compute_server.app.test_request_context(
                    "/compute",
                    method="POST",
                    json=payload,
                ):
                    response = compute_server.compute()
                if hasattr(response, "get_json"):
                    return response.get_json() or {}
            except Exception as exc:
                service_mod.logger.error("Realtime compute trigger failed: %s", exc)
                return {"ok": False, "error": str(exc)}
            return {"ok": False, "error": "empty_response"}

    def _normalize_compute_event(self, item) -> dict | None:
            if item is None:
                return None
            if isinstance(item, dict):
                symbols = sorted(
                    {
                        str(symbol or "").strip().upper()
                        for symbol in (item.get("symbols") or [])
                        if str(symbol or "").strip()
                    }
                )
                return {
                    "source": str(item.get("source") or "bar_close").strip().lower() or "bar_close",
                    "bar_count": max(0, int(item.get("bar_count", 0) or 0)),
                    "symbols": symbols,
                }
            return {
                "source": "bar_close",
                "bar_count": max(0, int(item or 0)),
                "symbols": [],
            }

    def _queue_compute_event(self, source: str, bar_count: int = 0, symbols: list[str] | None = None):
            if not self._running:
                return
            self._compute_queue.put(
                {
                    "source": str(source or "bar_close").strip().lower() or "bar_close",
                    "bar_count": max(0, int(bar_count or 0)),
                    "symbols": sorted(
                        {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
                    ),
                }
            )

    def _compute_loop(self):
            service_mod = _service_mod()
            service_mod.logger.info("Realtime close-driven compute loop started")
            while self._running or not self._compute_queue.empty():
                try:
                    first_item = self._compute_queue.get(timeout=1)
                except queue.Empty:
                    continue

                if first_item is None:
                    continue

                close_events = 1
                merged_event = self._normalize_compute_event(first_item) or {
                    "source": "bar_close",
                    "bar_count": 0,
                    "symbols": [],
                }
                drain_until = time.time() + 0.25
                while time.time() < drain_until:
                    try:
                        next_item = self._compute_queue.get_nowait()
                    except queue.Empty:
                        break
                    if next_item is None:
                        continue
                    close_events += 1
                    next_event = self._normalize_compute_event(next_item)
                    if not next_event:
                        continue
                    merged_event["bar_count"] += int(next_event.get("bar_count", 0) or 0)
                    merged_event["symbols"] = sorted(
                        set(merged_event.get("symbols") or []).union(next_event.get("symbols") or [])
                    )
                    if str(next_event.get("source") or "") == "canonical_close":
                        merged_event["source"] = "canonical_close"

                try:
                    startup_compute = bool(self._starting)
                    compute_source = str(merged_event.get("source") or "bar_close")
                    compute_symbols = list(merged_event.get("symbols") or [])
                    critical_intervals = ["5m"] if compute_source == "canonical_close" else None
                    critical_rollup_intervals = [] if compute_source == "canonical_close" else None
                    self._last_realtime_compute_started_at = time.time()
                    self.data_writer.flush()
                    result = self._trigger_realtime_compute(
                        source=compute_source,
                        symbols=compute_symbols,
                        persist_signals=False if startup_compute else None,
                        intervals=critical_intervals,
                        rollup_intervals=critical_rollup_intervals,
                    )
                    self._realtime_compute_runs += 1
                    self._last_realtime_compute_at = time.time()
                    self._last_realtime_compute_result = result or {}
                    service_mod.logger.info(
                        "Realtime compute finished: source=%s events=%d bars=%d symbols=%d processed=%s signals=%s errors=%s elapsed_s=%s",
                        merged_event.get("source"),
                        close_events,
                        merged_event.get("bar_count", 0),
                        len(merged_event.get("symbols") or []),
                        result.get("processed", 0),
                        result.get("signals", 0),
                        result.get("errors", 0),
                        result.get("elapsed_s", 0),
                    )
                    if int(result.get("signals", 0) or 0) > 0:
                        self._signal_wakeup.set()
                    try:
                        self._request_watchlist_idle_topup_now(ttl_s=5.0)
                    except Exception:
                        pass
                    if compute_source == "canonical_close" and compute_symbols:
                        rollup_intervals = list(HIGHER_INTERVALS)
                        if rollup_intervals:
                            rollup_result = self._trigger_realtime_compute(
                                source="canonical_close",
                                symbols=compute_symbols,
                                persist_signals=False,
                                intervals=[],
                                rollup_intervals=rollup_intervals,
                            )
                            service_mod.logger.info(
                                "Canonical close background rollup finished: symbols=%d errors=%s elapsed_s=%s",
                                len(compute_symbols),
                                rollup_result.get("errors", 0),
                                rollup_result.get("elapsed_s", 0),
                            )
                except Exception as exc:
                    service_mod.logger.error("Realtime compute loop error: %s", exc)
                finally:
                    self._last_realtime_compute_started_at = 0.0

    def _bar_close_loop(self):
            service_mod = _service_mod()
            service_mod.logger.info("Bar close guard loop started")
            while self._running:
                try:
                    self.bar_aggregator.force_close_due()
                except Exception as exc:
                    service_mod.logger.error("Bar close guard loop error: %s", exc)
                time.sleep(1)

    def _on_bar_close(self, bar_data: dict):
            _ = bar_data
            self._last_bar_close_at = time.time()
