from __future__ import annotations

import math
import time
from datetime import datetime


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceWarmupCycleMixin:
    def _is_local_pb_unavailable_error(self, error) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        if "8090" not in text:
            return False
        if "127.0.0.1" not in text and "localhost" not in text:
            return False
        return any(
            marker in text
            for marker in (
                "connection refused",
                "failed to establish a new connection",
                "max retries exceeded",
            )
        )

    def _release_startup_after_trade_gate(
        self,
        snapshot: dict,
        readiness: dict,
        preflight_result: dict,
        backfill_written: int,
        started_at: str,
        warmup_timings: dict,
    ) -> bool:
        finished_at = self._now_iso()
        startup_detail = {
            "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
            "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
            "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
            "预检修复标的": self._format_symbol_list(
                preflight_result.get("attempted_repair_symbols") or []
            ),
            "回补写入Bars": backfill_written,
            "预热开始": started_at,
            "预热完成": finished_at,
            "预热耗时": f"{float(warmup_timings.get('total_elapsed_s', 0.0) or 0.0):.3f}s",
            "交易门": "open",
            "后续动作": "交易链路已开放，剩余 monitor / integrity repair 在后台继续。",
            "待完成标的": self._format_symbol_list(
                readiness.get("pending_symbols") or []
            ),
            "完整性阻塞": self._format_symbol_list(
                readiness.get("integrity_pending_symbols") or []
            ),
        }
        if not self._complete_startup_success(
            "IBKR Runtime 启动完成（后台继续预热）",
            startup_detail,
        ):
            return False
        return True

    def _build_warmup_readiness(
        self,
        snapshot: dict,
        status_by_symbol: dict[str, dict] | None,
        *,
        required_interval: str,
    ) -> dict:
        ready_symbols = []
        pending_symbols = []
        symbol_status = []
        ready_set = set()
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot["trade_symbols"])
        monitor_symbol_set = set(snapshot["monitor_symbols"])

        for symbol in snapshot["symbols"]:
            status = dict((status_by_symbol or {}).get(symbol) or {})
            is_ready = bool(status.get("ready"))
            bar_count = int(status.get("bar_count", 0) or 0)
            last_bar_time_ms = int(status.get("last_bar_time_ms", 0) or 0)
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
            row = {
                "symbol": symbol,
                "role": role,
                "ready": is_ready,
                "bar_count": bar_count,
                "last_bar_time_ms": last_bar_time_ms,
            }
            source = str(status.get("source") or "").strip()
            if source:
                row["source"] = source
            symbol_status.append(row)
            if is_ready:
                ready_symbols.append(symbol)
                ready_set.add(symbol)
            else:
                pending_symbols.append(symbol)

        ready_scan_symbols = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        ready_subscription_symbols = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
        ready_trade_symbols = len([symbol for symbol in snapshot["trade_symbols"] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot["monitor_symbols"] if symbol in ready_set])
        trading_gate_open = bool(snapshot["trade_symbols"]) and ready_trade_symbols == snapshot["trade_symbols_total"]
        blocking_pending_symbols = self._non_monitor_pending_symbols(
            pending_symbols,
            snapshot["monitor_symbols"],
        )
        return {
            "phase": "ready" if not blocking_pending_symbols else "degraded",
            "required_interval": required_interval,
            "ready_symbols": len(ready_symbols),
            "ready_scan_symbols": ready_scan_symbols,
            "ready_subscription_symbols": ready_subscription_symbols,
            "ready_trade_symbols": ready_trade_symbols,
            "ready_monitor_symbols": ready_monitor_symbols,
            "ready_symbols_list": ready_symbols,
            "pending_symbols": pending_symbols,
            "symbol_status": symbol_status,
            "trading_gate_open": trading_gate_open,
            "trading_gate_reason": "ready" if trading_gate_open else ("no_trade_symbols" if not snapshot["trade_symbols"] else "warmup_incomplete"),
        }

    def _warmup_uses_remote_compute_service(self) -> bool:
        from ibkr_compute.api.service_topology import uses_remote_compute_service

        return uses_remote_compute_service()

    def _load_warmup_compute_cursors(self) -> int:
        service_mod = _service_mod()
        if self._warmup_uses_remote_compute_service():
            return 0
        from ibkr_compute.api import server as compute_server

        with compute_server.compute_lock:
            return int(compute_server.load_persisted_compute_cursors(service_mod.ENVIRONMENT) or 0)

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
            service_mod.ENVIRONMENT,
            symbols or [],
            service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL,
            hydrate_signal_state=hydrate_signal_state,
            persist_latest_indicator=persist_latest_indicator,
        )

    def _collect_remote_warmup_readiness(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        from ibkr_compute.api.compute_status_client import (
            get_remote_compute_status,
            is_compute_status_payload,
        )

        required_interval = service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL
        payload = get_remote_compute_status(force_refresh=True)
        if not is_compute_status_payload(payload):
            readiness = self._build_warmup_readiness(
                snapshot,
                {
                    symbol: {
                        "ready": False,
                        "bar_count": 0,
                        "last_bar_time_ms": 0,
                        "source": "remote_compute_status_unavailable",
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
        status_by_symbol = {}
        if isinstance(engines, dict):
            for symbol in snapshot["symbols"]:
                engine_state = dict(
                    engines.get(f"{service_mod.ENVIRONMENT}/{symbol}/{required_interval}") or {}
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

    def _collect_warmup_readiness(self, snapshot: dict) -> dict:
        service_mod = _service_mod()

        if self._warmup_uses_remote_compute_service():
            return self._collect_remote_warmup_readiness(snapshot)

        from ibkr_compute.api import server as compute_server

        status_by_symbol = {}
        for symbol in snapshot["symbols"]:
            engine = compute_server.engines.get(
                (
                    service_mod.ENVIRONMENT,
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

    def _warmup_indicator_backfill_intervals(self) -> list[str]:
        service_mod = _service_mod()
        if not self.config.get_bool_for_environment(
            "ibkr_warmup_indicator_backfill_enabled",
            service_mod.ENVIRONMENT,
            True,
        ):
            return []
        configured = self.config.get_for_environment(
            "ibkr_warmup_indicator_backfill_intervals",
            service_mod.ENVIRONMENT,
            ",".join(
                interval
                for interval in self._multi_timeframe_warmup_intervals()
                if interval != service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL
            ),
        )
        intervals = []
        for raw in str(configured or "").replace("\n", ",").split(","):
            interval = str(raw or "").strip()
            if not interval:
                continue
            try:
                from ibkr_compute.market.timeframe_utils import interval_to_ms, normalize_interval

                normalized = normalize_interval(interval)
                interval_to_ms(normalized)
            except Exception:
                continue
            if normalized != service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL and normalized not in intervals:
                intervals.append(normalized)
        return intervals

    def _collect_warmup_indicator_backfill_plan(self, snapshot: dict) -> dict[str, list[str]]:
        """Return only bar-missing repair work.

        Indicators are derived from bars and may be re-created by replaying storage
        into in-memory engines, so indicator mirror gaps must not trigger IB
        history requests during startup.
        """
        intervals = self._warmup_indicator_backfill_intervals()
        if not intervals:
            return {}
        snapshot_symbols = self._normalize_symbol_list(snapshot.get("symbols") or [])
        symbol_set = set(snapshot_symbols)
        conid_symbols = {
            str(symbol or "").strip().upper()
            for symbol in (snapshot.get("conid_map") or {}).keys()
            if str(symbol or "").strip()
        }

        def eligible(values) -> list[str]:
            return [
                symbol
                for symbol in self._normalize_symbol_list(values)
                if symbol in symbol_set and symbol in conid_symbols
            ]

        if not self._warmup_uses_remote_compute_service():
            return {}

        from ibkr_compute.api.compute_status_client import (
            get_remote_compute_status,
            is_compute_status_payload,
        )

        payload = get_remote_compute_status(force_refresh=True)
        if not is_compute_status_payload(payload):
            return {}

        readiness_intervals = (payload.get("multi_timeframe_readiness") or {}).get("intervals") or {}
        plan = {}
        for interval in intervals:
            row = readiness_intervals.get(interval) or {}
            if not bool(row.get("storage_checked")):
                continue
            symbols = eligible(row.get("missing_bar_symbols") or [])
            if symbols:
                plan[interval] = symbols
        return plan

    def _warmup_indicator_backfill_period_for_interval(self, interval: str) -> str:
        service_mod = _service_mod()
        try:
            from ibkr_compute.market.timeframe_utils import interval_to_ms, normalize_interval

            interval_minutes = max(
                1,
                interval_to_ms(normalize_interval(interval)) // 60000,
            )
        except Exception:
            interval_minutes = 5

        base_days = self._live_warmup_days()
        ready_bars = max(1, int(service_mod.indicator_ready_bar_count() or 0))
        session_minutes = max(
            60,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_regular_minutes_per_day",
                service_mod.ENVIRONMENT,
                390,
            ),
        )
        calendar_multiplier = max(
            1.0,
            self.config.get_float_for_environment(
                "ibkr_warmup_indicator_calendar_multiplier",
                service_mod.ENVIRONMENT,
                1.4,
            ),
        )
        buffer_days = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_buffer_days",
                service_mod.ENVIRONMENT,
                5,
            ),
        )
        max_days = max(
            base_days,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_max_days",
                service_mod.ENVIRONMENT,
                60,
            ),
        )
        calculated_days = int(
            math.ceil(((interval_minutes * ready_bars) / session_minutes) * calendar_multiplier)
        ) + buffer_days
        return f"{min(max_days, max(base_days, calculated_days))}d"

    def _prime_warmup_indicator_backfill(self, plan: dict[str, list[str]]) -> dict:
        service_mod = _service_mod()
        prime_result = {}
        if not plan:
            return prime_result
        max_retries = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_prime_retries",
                service_mod.ENVIRONMENT,
                3,
            ),
        )
        retry_delay_s = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_prime_retry_delay_sec",
                service_mod.ENVIRONMENT,
                5,
            ),
        )

        def retryable(result: dict) -> bool:
            if bool((result or {}).get("ok")):
                return False
            text = str((result or {}).get("error") or "").lower()
            return bool((result or {}).get("retryable")) or "compute_busy" in text or "timed out" in text

        if self._warmup_uses_remote_compute_service():
            from ibkr_compute.api.compute_status_client import trigger_remote_prime

            for interval, symbols in sorted(plan.items()):
                interval_results = []
                normalized_symbols = self._normalize_symbol_list(symbols)
                for index in range(
                    0,
                    len(normalized_symbols),
                    service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE,
                ):
                    chunk = normalized_symbols[index:index + service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE]
                    if not chunk:
                        continue
                    payload = {
                        "environments": [service_mod.ENVIRONMENT],
                        "symbols": chunk,
                        "intervals": [interval],
                        "persist_latest_indicator": False,
                    }
                    result = {}
                    for attempt in range(max_retries + 1):
                        result = trigger_remote_prime(payload)
                        if not retryable(result) or attempt >= max_retries:
                            break
                        if retry_delay_s > 0:
                            time.sleep(retry_delay_s)
                    interval_results.append(result)
                prime_result[interval] = interval_results
            return prime_result

        from ibkr_compute.api import server as compute_server

        for interval, symbols in sorted(plan.items()):
            with compute_server.compute_lock:
                prime_result[interval] = compute_server.materialize_engines_from_storage(
                    service_mod.ENVIRONMENT,
                    self._normalize_symbol_list(symbols),
                    interval,
                    persist_latest_indicator=False,
                )
        return prime_result

    def _run_warmup_indicator_backfill(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        period = "bar_only"
        max_passes = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_backfill_passes",
                service_mod.ENVIRONMENT,
                2,
            ),
        )
        attempted = {}
        combined_plan = {}
        backfill = {}
        prime = {}
        written_total = 0

        for pass_index in range(max_passes):
            plan = self._collect_warmup_indicator_backfill_plan(snapshot)
            if not plan:
                break
            service_mod.logger.info(
                "Warmup indicator backfill started: pass=%d/%d period=%s plan=%s",
                pass_index + 1,
                max_passes,
                period,
                ",".join(
                    f"{interval}:{len(symbols)}"
                    for interval, symbols in sorted(plan.items())
                ),
            )
            prime_plan = {}
            backfill_plan = {}
            for interval, symbols in sorted(plan.items()):
                normalized_symbols = self._normalize_symbol_list(symbols)
                if not normalized_symbols:
                    continue
                combined_plan.setdefault(interval, [])
                for symbol in normalized_symbols:
                    if symbol not in combined_plan[interval]:
                        combined_plan[interval].append(symbol)
                prime_plan[interval] = normalized_symbols
                attempted_set = attempted.setdefault(interval, set())
                new_symbols = [symbol for symbol in normalized_symbols if symbol not in attempted_set]
                if new_symbols:
                    backfill_plan[interval] = new_symbols
                    attempted_set.update(new_symbols)

            for interval, symbols in sorted(backfill_plan.items()):
                conid_map = {
                    symbol: (snapshot.get("conid_map") or {}).get(symbol)
                    for symbol in self._normalize_symbol_list(symbols)
                    if (snapshot.get("conid_map") or {}).get(symbol)
                }
                if not conid_map:
                    continue
                interval_result = self.data_backfill.backfill_all(
                    conid_map,
                    symbol_meta=snapshot.get("symbol_meta") or {},
                    intervals=[interval],
                    repair_symbols=list(conid_map.keys()),
                    period_overrides={
                        symbol: {
                            interval: self._warmup_indicator_backfill_period_for_interval(interval)
                        }
                        for symbol in conid_map.keys()
                    },
                )
                backfill.setdefault(interval, {}).update(interval_result)
                written_total += sum(
                    int(count or 0)
                    for per_symbol in interval_result.values()
                    for count in per_symbol.values()
                )

            if backfill_plan:
                self.data_writer.flush()
            pass_prime = self._prime_warmup_indicator_backfill(prime_plan)
            for interval, result in pass_prime.items():
                prime.setdefault(interval, []).extend(result if isinstance(result, list) else [result])

        if not combined_plan:
            return {
                "period": period,
                "plan": {},
                "written_total": 0,
                "backfill": {},
                "prime": {},
                "skipped": True,
                "skip_reason": "indicator_is_derived_from_bars",
            }

        service_mod.logger.info(
            "Warmup indicator backfill finished: period=%s written=%d",
            period,
            written_total,
        )
        return {
            "period": period,
            "plan": {interval: list(symbols) for interval, symbols in sorted(combined_plan.items())},
            "written_total": written_total,
            "backfill": backfill,
            "prime": prime,
            "reason": "bar_missing_only",
        }

    def _apply_integrity_readiness(self, readiness: dict, snapshot: dict, repair_plan: dict[str, dict] | None = None) -> dict:
        plan = repair_plan or {}
        if not plan:
            readiness["integrity_pending_symbols"] = []
            readiness["integrity_repair_reasons"] = {}
            for item in readiness.get("symbol_status") or []:
                if isinstance(item, dict):
                    item["integrity_ready"] = True
                    item["integrity_reason"] = ""
            return readiness

        blocking_symbols = sorted(plan.keys())
        repair_reasons = {
            symbol: str((plan.get(symbol) or {}).get("repair_reason") or "history_repair_pending")
            for symbol in blocking_symbols
        }
        ready_set = set(readiness.get("ready_symbols_list") or []) - set(blocking_symbols)
        pending_set = set(readiness.get("pending_symbols") or []) | set(blocking_symbols)
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot.get("trade_symbols") or [])
        monitor_symbol_set = set(snapshot.get("monitor_symbols") or [])

        status_map = {
            str((item or {}).get("symbol") or "").upper(): dict(item or {})
            for item in (readiness.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        merged_status = []
        for symbol in snapshot.get("symbols") or []:
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
            row = dict(status_map.get(symbol) or {})
            row["symbol"] = symbol
            row["role"] = row.get("role") or role
            row["integrity_ready"] = symbol not in repair_reasons
            row["integrity_reason"] = repair_reasons.get(symbol, "")
            if symbol in repair_reasons:
                row["ready"] = False
            merged_status.append(row)

        ready_trade_symbols = len([symbol for symbol in snapshot.get("trade_symbols") or [] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot.get("monitor_symbols") or [] if symbol in ready_set])
        trade_blocked = any(symbol in trade_symbol_set for symbol in blocking_symbols)
        trading_gate_open = (
            bool(snapshot.get("trade_symbols"))
            and ready_trade_symbols == int(snapshot.get("trade_symbols_total", 0) or 0)
            and not trade_blocked
        )
        blocking_pending_symbols = self._non_monitor_pending_symbols(sorted(pending_set), snapshot.get("monitor_symbols") or [])

        readiness["phase"] = "ready" if not blocking_pending_symbols else "degraded"
        readiness["ready_symbols"] = len(ready_set)
        readiness["ready_scan_symbols"] = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        readiness["ready_subscription_symbols"] = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
        readiness["ready_trade_symbols"] = ready_trade_symbols
        readiness["ready_monitor_symbols"] = ready_monitor_symbols
        readiness["ready_symbols_list"] = sorted(ready_set)
        readiness["pending_symbols"] = sorted(pending_set)
        readiness["symbol_status"] = merged_status
        readiness["integrity_pending_symbols"] = blocking_symbols
        readiness["integrity_repair_reasons"] = repair_reasons
        readiness["trading_gate_open"] = trading_gate_open
        if trading_gate_open:
            readiness["trading_gate_reason"] = "ready"
        elif trade_blocked:
            readiness["trading_gate_reason"] = "history_repair_pending"
        elif not snapshot.get("trade_symbols"):
            readiness["trading_gate_reason"] = "no_trade_symbols"
        else:
            readiness["trading_gate_reason"] = "warmup_incomplete"
        return readiness

    def _run_warmup_preflight_repairs(
        self,
        snapshot: dict,
        integrity_reference_et: datetime | None = None,
    ) -> dict:
        service_mod = _service_mod()
        checked_symbols = self._normalize_symbol_list(snapshot.get("trade_symbols") or [])
        if not checked_symbols:
            return {
                "checked_symbols": [],
                "initial_repair_symbols": [],
                "attempted_repair_symbols": [],
                "remaining_repair_symbols": [],
                "repair_reasons": {},
                "history_fetch_symbols": [],
                "history_period_overrides": {},
                "history_written_total": 0,
                "repair_result": {},
                "skipped": True,
                "skip_reason": "no_trade_symbols",
            }
        repair_plan = self._build_startup_history_repair_plan(
            checked_symbols,
            et_now=integrity_reference_et,
        )
        period_overrides = self._build_startup_history_period_overrides(repair_plan)
        if not repair_plan:
            return {
                "checked_symbols": checked_symbols,
                "initial_repair_symbols": [],
                "attempted_repair_symbols": [],
                "remaining_repair_symbols": [],
                "repair_reasons": {},
                "history_fetch_symbols": [],
                "history_period_overrides": {},
                "history_written_total": 0,
                "repair_result": {},
            }

        service_mod.logger.info(
            "Warmup preflight history repair started: symbols=%s short_window=%s",
            ",".join(sorted(repair_plan.keys())),
            ",".join(
                f"{symbol}:{(period_overrides.get(symbol) or {}).get('5m')}"
                for symbol in sorted(period_overrides.keys())
            ) or "none",
        )
        repair_result = self._run_bar_integrity_repairs(
            repair_plan,
            source="warmup_preflight",
            allow_defer=False,
            run_pipeline_repair=False,
            history_period_overrides=period_overrides,
        )
        remaining_plan = self._build_startup_history_repair_plan(
            checked_symbols,
            et_now=integrity_reference_et,
        )
        history_written_total = 0
        for item in (repair_result.get("per_symbol") or {}).values():
            result = (item or {}).get("result") or {}
            history_written_total += int(result.get("history_written", 0) or 0)
        return {
            "checked_symbols": checked_symbols,
            "initial_repair_symbols": sorted(repair_plan.keys()),
            "attempted_repair_symbols": sorted(repair_result.get("repair_symbols") or []),
            "remaining_repair_symbols": sorted(remaining_plan.keys()),
            "repair_reasons": {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in remaining_plan.items()
            },
            "history_fetch_symbols": sorted(repair_result.get("history_symbols") or []),
            "history_period_overrides": {
                symbol: dict((period_overrides.get(symbol) or {}))
                for symbol in sorted(period_overrides.keys())
            },
            "history_written_total": history_written_total,
            "repair_result": repair_result,
        }

    def _run_warmup_cycle(self):
        service_mod = _service_mod()
        snapshot = self._warmup_snapshot_from_subscriptions()
        previous_warmup_state = self._copy_warmup_state()
        if snapshot["symbols_total"] == 0:
            self._set_warmup_state(
                phase="idle",
                reason="no_active_symbols",
                trading_gate_open=False,
                trading_gate_reason="no_active_symbols",
            )
            return
        if not self.session_keeper.is_authenticated:
            self._close_warmup_gate("session_unauthenticated")
            return

        started_at = self._now_iso()
        warmup_started_perf = time.perf_counter()
        warmup_timings = {}
        integrity_reference_et = datetime.now(service_mod.ET)
        self._set_warmup_state(
            phase="running",
            reason=str(self._warmup_state.get("reason") or "warmup"),
            started_at=started_at,
            finished_at=None,
            last_error="",
            trading_gate_open=False,
            trading_gate_reason="warmup_running",
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            pending_symbols=snapshot["symbols"],
            symbol_status=[],
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            backfill_written=0,
            backfill_result={},
            compute_result={},
            timings={},
            last_duration_s=0.0,
        )

        service_mod.logger.info(
            "Warmup started: symbols=%d trade=%d monitor=%d target_date=%s",
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
            snapshot["target_date"] or "n/a",
        )
        self._sync_startup_progress(
            action="update",
            title="IBKR Runtime 启动中",
            summary="核心线程已启动，Warmup / 预检修复 / 历史回补已开始。",
            current_step="warmup",
            current_blocker="等待 warmup 基线统计与首轮预检修复结果",
            operator_action="等待系统推进预检修复、历史回补与交易门判断",
            steps={
                "warmup": {
                    "status": "running",
                    "detail": (
                        f"symbols={snapshot['symbols_total']} "
                        f"trade={snapshot['trade_symbols_total']} "
                        f"monitor={snapshot['monitor_symbols_total']}"
                    ),
                },
            },
            fields=self._build_startup_progress_fields(
                str(self._warmup_state.get("reason") or "warmup"),
                self._startup_source or "api_start",
                bool(self._startup_trigger_login),
            ),
            reason=self._startup_reason or "warmup",
            source=self._startup_source or "api_start",
            trigger_login=bool(self._startup_trigger_login),
        )

        backfill_result = {}
        backfill_written = 0
        compute_result = {}
        last_error = ""
        startup_gate_open_once = False

        try:
            step_started = time.perf_counter()
            self._load_warmup_compute_cursors()
            warmup_timings["cursor_load_s"] = round(time.perf_counter() - step_started, 3)

            step_started = time.perf_counter()
            preflight_result = self._run_warmup_preflight_repairs(
                snapshot,
                integrity_reference_et=integrity_reference_et,
            )
            warmup_timings["preflight_repair_s"] = round(time.perf_counter() - step_started, 3)
            compute_result["preflight_repair"] = preflight_result
            preflight_blockers = {
                symbol: {
                    "repair_reason": (
                        preflight_result.get("repair_reasons") or {}
                    ).get(symbol, "history_repair_pending")
                }
                for symbol in (preflight_result.get("remaining_repair_symbols") or [])
            }
            if preflight_result.get("history_written_total"):
                backfill_written += int(preflight_result.get("history_written_total", 0) or 0)
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(
                    preflight_result.get("attempted_repair_symbols") or []
                )

            step_started = time.perf_counter()
            storage_bootstrap = self._materialize_warmup_compute_symbols(
                snapshot["symbols"],
                hydrate_signal_state=True,
            )
            warmup_timings["storage_bootstrap_s"] = round(time.perf_counter() - step_started, 3)
            if storage_bootstrap:
                compute_result["storage_bootstrap"] = storage_bootstrap

            step_started = time.perf_counter()
            indicator_backfill = self._run_warmup_indicator_backfill(snapshot)
            warmup_timings["indicator_backfill_s"] = round(time.perf_counter() - step_started, 3)
            if indicator_backfill.get("plan"):
                compute_result["indicator_backfill"] = indicator_backfill
                backfill_written += int(indicator_backfill.get("written_total", 0) or 0)
            elif indicator_backfill.get("skipped"):
                compute_result["indicator_backfill"] = {
                    "skipped": True,
                    "skip_reason": indicator_backfill.get("skip_reason") or "no_bar_backfill_needed",
                }

            readiness = self._collect_warmup_readiness(snapshot)
            readiness = self._apply_integrity_readiness(readiness, snapshot, preflight_blockers)
            if readiness["pending_symbols"]:
                step_started = time.perf_counter()
                pending_storage_bootstrap = self._materialize_warmup_compute_symbols(
                    readiness["pending_symbols"],
                    hydrate_signal_state=True,
                )
                warmup_timings["pending_storage_bootstrap_s"] = round(
                    time.perf_counter() - step_started,
                    3,
                )
                if pending_storage_bootstrap:
                    compute_result["pending_storage_bootstrap"] = pending_storage_bootstrap
                    readiness = self._collect_warmup_readiness(snapshot)
                    readiness = self._apply_integrity_readiness(
                        readiness,
                        snapshot,
                        preflight_blockers,
                    )
            compute_result["warmup_timings"] = dict(warmup_timings)
            self._set_warmup_state(
                phase="running",
                required_interval=readiness["required_interval"],
                started_at=started_at,
                target_date=snapshot["target_date"],
                symbols_total=snapshot["symbols_total"],
                trade_symbols_total=snapshot["trade_symbols_total"],
                monitor_symbols_total=snapshot["monitor_symbols_total"],
                ready_symbols=readiness["ready_symbols"],
                ready_trade_symbols=readiness["ready_trade_symbols"],
                ready_monitor_symbols=readiness["ready_monitor_symbols"],
                symbols=snapshot["symbols"],
                trade_symbols=snapshot["trade_symbols"],
                monitor_symbols=snapshot["monitor_symbols"],
                ready_symbols_list=readiness["ready_symbols_list"],
                pending_symbols=readiness["pending_symbols"],
                symbol_status=readiness["symbol_status"],
                integrity_pending_symbols=readiness["integrity_pending_symbols"],
                integrity_repair_reasons=readiness["integrity_repair_reasons"],
                preflight_repair=preflight_result,
                trading_gate_open=readiness["trading_gate_open"],
                trading_gate_reason=readiness["trading_gate_reason"],
                compute_result=compute_result,
                timings=warmup_timings,
                last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
            )
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 已进入预热判断阶段，正在核对 readiness 与完整性阻塞。",
                current_step="warmup",
                current_blocker=(
                    "待完成标的: " + self._format_symbol_list(readiness["pending_symbols"])
                    if readiness["pending_symbols"]
                    else "等待交易门判断"
                ),
                operator_action="等待预检修复、历史回补与交易门开放",
                steps={
                    "warmup": {
                        "status": "running",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"integrity={self._format_symbol_list(readiness['integrity_pending_symbols'])}"
                        ),
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "预检修复标的": self._format_symbol_list(
                            preflight_result.get("attempted_repair_symbols") or []
                        ),
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )

            if readiness["trading_gate_open"] and not readiness["pending_symbols"]:
                finished_at = self._now_iso()
                phase = readiness["phase"]
                warmup_timings["total_elapsed_s"] = round(
                    time.perf_counter() - warmup_started_perf,
                    3,
                )
                compute_result["warmup_timings"] = dict(warmup_timings)
                self._set_warmup_state(
                    phase=phase,
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    finished_at=finished_at,
                    last_success_at=finished_at,
                    last_error=last_error,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    backfill_written=backfill_written,
                    backfill_result=backfill_result,
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=warmup_timings["total_elapsed_s"],
                )
                service_mod.logger.info(
                    "Warmup finished early after bootstrap: phase=%s gate=open ready=%d/%d trade_ready=%d/%d",
                    phase,
                    readiness["ready_symbols"],
                    snapshot["symbols_total"],
                    readiness["ready_trade_symbols"],
                    snapshot["trade_symbols_total"],
                )
                self._complete_startup_success(
                    "IBKR Runtime 启动完成",
                    {
                        "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                        "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                        "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                        "预检修复标的": self._format_symbol_list(
                            preflight_result.get("attempted_repair_symbols") or []
                        ),
                        "回补写入Bars": backfill_written,
                        "预热开始": started_at,
                        "预热完成": finished_at,
                        "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                        "交易门": "open",
                    },
                )
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
                self._signal_wakeup.set()
                return
            if readiness["trading_gate_open"] and readiness["pending_symbols"]:
                startup_gate_open_once = True
                warmup_timings["total_elapsed_s"] = round(
                    time.perf_counter() - warmup_started_perf,
                    3,
                )
                self._set_warmup_state(
                    phase="running",
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
                )
                service_mod.logger.info(
                    "Warmup trade gate open after bootstrap; continuing repair for pending symbols: %s",
                    ",".join(readiness["pending_symbols"]),
                )
                self._release_startup_after_trade_gate(
                    snapshot,
                    readiness,
                    preflight_result,
                    backfill_written,
                    started_at,
                    warmup_timings,
                )
                self._signal_wakeup.set()

            pending_status_by_symbol = {
                str((item or {}).get("symbol") or "").strip().upper(): dict(item or {})
                for item in (readiness.get("symbol_status") or [])
                if str((item or {}).get("symbol") or "").strip()
            }
            pending_map = {
                symbol: snapshot["conid_map"][symbol]
                for symbol in readiness["pending_symbols"]
                if symbol in snapshot["conid_map"]
                and str(
                    (pending_status_by_symbol.get(symbol) or {}).get("source") or ""
                ) not in {
                    "remote_compute_status_missing",
                    "remote_compute_status_unavailable",
                }
            }
            if pending_map:
                service_mod.logger.info(
                    "Warmup backfilling pending symbols: %d of %d",
                    len(pending_map),
                    snapshot["symbols_total"],
                )
                step_started = time.perf_counter()
                backfill_result = self.data_backfill.backfill_all(
                    pending_map,
                    symbol_meta=snapshot["symbol_meta"],
                    intervals=[service_mod.DEFAULT_WARMUP_REQUIRED_INTERVAL],
                    repair_symbols=list(pending_map.keys()),
                    period_overrides=self._multi_timeframe_5m_period_overrides(pending_map.keys()),
                )
                warmup_timings["pending_backfill_s"] = round(
                    time.perf_counter() - step_started,
                    3,
                )
                backfill_written += sum(
                    int(count or 0)
                    for per_symbol in backfill_result.values()
                    for count in per_symbol.values()
                )
                self.data_writer.flush()
                if readiness["trading_gate_open"]:
                    self._last_history_repair_at = time.time()
                    self._last_history_repair_symbols = sorted(pending_map.keys())
                step_started = time.perf_counter()
                after_backfill_result = self._materialize_warmup_compute_symbols(
                    list(pending_map.keys()),
                    hydrate_signal_state=True,
                )
                warmup_timings["after_backfill_bootstrap_s"] = round(
                    time.perf_counter() - step_started,
                    3,
                )
                compute_result["after_backfill"] = after_backfill_result
        except Exception as exc:
            last_error = str(exc)
            service_mod.logger.error("Warmup cycle failed: %s", exc)
            if self._is_local_pb_unavailable_error(exc):
                retry_delay_s = 30
                service_mod.logger.warning(
                    "Warmup pending retry because PocketBase is temporarily unavailable: retry_in=%ss error=%s",
                    retry_delay_s,
                    exc,
                )
                retry_state = self._build_transient_warmup_retry_state(
                    snapshot,
                    previous_warmup_state,
                    reason="pb_unavailable_retry",
                    requested_at=self._now_iso(),
                    started_at=started_at,
                )
                if str(retry_state.get("phase") or "").strip().lower() == "ready":
                    retry_state.update(
                        preflight_repair=dict(previous_warmup_state.get("preflight_repair") or {}),
                        backfill_written=int(previous_warmup_state.get("backfill_written", 0) or 0),
                        backfill_result=dict(previous_warmup_state.get("backfill_result") or {}),
                        compute_result=dict(previous_warmup_state.get("compute_result") or {}),
                        timings=dict(previous_warmup_state.get("timings") or {}),
                        last_duration_s=float(previous_warmup_state.get("last_duration_s", 0.0) or 0.0),
                    )
                else:
                    retry_state.update(
                        preflight_repair={},
                        backfill_written=backfill_written,
                        backfill_result=backfill_result,
                        compute_result=compute_result,
                        timings=warmup_timings,
                        last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
                    )
                self._set_warmup_state(**retry_state)
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="PocketBase 本地服务暂时不可达，Warmup 已进入自动重试等待。",
                    current_step="warmup",
                    current_blocker="等待 PocketBase 恢复可用后自动重试 Warmup",
                    operator_action="无需人工干预，系统将在短暂延迟后自动重试；如持续失败再检查 PB hooks 发布流程",
                    steps={
                        "warmup": {
                            "status": "running",
                            "detail": f"pb_retry_in={retry_delay_s}s error={last_error}",
                        },
                    },
                    fields=self._build_startup_progress_fields(
                        self._startup_reason or "warmup",
                        self._startup_source or "api_start",
                        bool(self._startup_trigger_login),
                    ),
                    reason=self._startup_reason or "warmup",
                    source=self._startup_source or "api_start",
                    trigger_login=bool(self._startup_trigger_login),
                )
                if self._running:
                    time.sleep(retry_delay_s)
                    if self._running:
                        self._warmup_wakeup.set()
                return

        readiness = self._collect_warmup_readiness(snapshot)
        final_preflight = dict(compute_result.get("preflight_repair") or {})
        if final_preflight.get("initial_repair_symbols") or backfill_result:
            final_recheck_symbols = self._normalize_symbol_list(
                list(final_preflight.get("checked_symbols") or [])
                + list((backfill_result or {}).keys())
            )
            final_remaining_plan = self._build_startup_history_repair_plan(
                final_recheck_symbols,
                et_now=integrity_reference_et,
            )
            final_preflight["checked_symbols"] = final_recheck_symbols
            final_preflight["remaining_repair_symbols"] = sorted(final_remaining_plan.keys())
            final_preflight["repair_reasons"] = {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in final_remaining_plan.items()
            }
        final_blockers = {
            symbol: {
                "repair_reason": (
                    final_preflight.get("repair_reasons") or {}
                ).get(symbol, "history_repair_pending")
            }
            for symbol in (final_preflight.get("remaining_repair_symbols") or [])
        }
        readiness = self._apply_integrity_readiness(readiness, snapshot, final_blockers)
        if startup_gate_open_once and not readiness["trading_gate_open"]:
            previous_ready_state = self._copy_warmup_state()
            if int(previous_ready_state.get("ready_symbols", 0) or 0) > int(readiness.get("ready_symbols", 0) or 0):
                for key in (
                    "required_interval",
                    "ready_symbols",
                    "ready_scan_symbols",
                    "ready_subscription_symbols",
                    "ready_trade_symbols",
                    "ready_monitor_symbols",
                    "ready_symbols_list",
                    "pending_symbols",
                    "symbol_status",
                    "integrity_pending_symbols",
                    "integrity_repair_reasons",
                ):
                    if key in previous_ready_state:
                        readiness[key] = previous_ready_state.get(key)
                blocking_pending_symbols = self._non_monitor_pending_symbols(
                    readiness.get("pending_symbols") or [],
                    snapshot.get("monitor_symbols") or [],
                )
                readiness["phase"] = "ready" if not blocking_pending_symbols else "degraded"
            readiness["trading_gate_open"] = True
            if str(readiness.get("trading_gate_reason") or "").strip() != "ready":
                readiness["trading_gate_reason"] = "background_repair"
        finished_at = self._now_iso()
        phase = "failed" if last_error and readiness["ready_symbols"] == 0 else readiness["phase"]
        warmup_timings["total_elapsed_s"] = round(time.perf_counter() - warmup_started_perf, 3)
        compute_result["warmup_timings"] = dict(warmup_timings)
        self._set_warmup_state(
            phase=phase,
            required_interval=readiness["required_interval"],
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=(
                finished_at
                if readiness["phase"] == "ready"
                else self._warmup_state.get("last_success_at")
            ),
            last_error=last_error,
            trading_gate_open=readiness["trading_gate_open"],
            trading_gate_reason=readiness["trading_gate_reason"],
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            ready_symbols=readiness["ready_symbols"],
            ready_trade_symbols=readiness["ready_trade_symbols"],
            ready_monitor_symbols=readiness["ready_monitor_symbols"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            ready_symbols_list=readiness["ready_symbols_list"],
            pending_symbols=readiness["pending_symbols"],
            symbol_status=readiness["symbol_status"],
            integrity_pending_symbols=readiness["integrity_pending_symbols"],
            integrity_repair_reasons=readiness["integrity_repair_reasons"],
            preflight_repair=final_preflight,
            backfill_written=backfill_written,
            backfill_result=backfill_result,
            compute_result=compute_result,
            timings=warmup_timings,
            last_duration_s=warmup_timings["total_elapsed_s"],
        )

        service_mod.logger.info(
            "Warmup finished: phase=%s gate=%s ready=%d/%d trade_ready=%d/%d backfill_written=%d compute_processed=%s total_elapsed_s=%.3f",
            phase,
            "open" if readiness["trading_gate_open"] else "closed",
            readiness["ready_symbols"],
            snapshot["symbols_total"],
            readiness["ready_trade_symbols"],
            snapshot["trade_symbols_total"],
            backfill_written,
            compute_result.get("processed", 0) if isinstance(compute_result, dict) else 0,
            warmup_timings["total_elapsed_s"],
        )
        startup_ready = bool(readiness["trading_gate_open"])
        if snapshot["trade_symbols_total"] <= 0:
            startup_ready = True

        if startup_ready:
            startup_title = "IBKR Runtime 启动完成"
            startup_detail = {
                "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                "预检修复标的": self._format_symbol_list(
                    final_preflight.get("attempted_repair_symbols") or []
                ),
                "回补写入Bars": backfill_written,
                "预热开始": started_at,
                "预热完成": finished_at,
                "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                "交易门": "open" if readiness["trading_gate_open"] else "closed",
            }
            if snapshot["trade_symbols_total"] <= 0:
                startup_title = "IBKR Runtime 启动完成（监控模式）"
                startup_detail["后续动作"] = (
                    "当前无 trade symbols，runtime 将继续执行 canonical 5m close、指标和 scan 链路。"
                )
            if phase != "ready":
                startup_title = "IBKR Runtime 启动完成（后台继续预热）"
                if snapshot["trade_symbols_total"] <= 0:
                    startup_detail["后续动作"] = (
                        "当前无 trade symbols，runtime 将在后台继续 monitor/integrity repair，并保持 canonical 5m close 链路运行。"
                    )
                else:
                    startup_detail["后续动作"] = "交易链路已开放，剩余 monitor/integrity repair 在后台继续。"
                startup_detail["待完成标的"] = self._format_symbol_list(
                    readiness.get("pending_symbols") or []
                )
                startup_detail["完整性阻塞"] = self._format_symbol_list(
                    readiness.get("integrity_pending_symbols") or []
                )
            if self._complete_startup_success(startup_title, startup_detail):
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
            self._signal_wakeup.set()
        else:
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 尚未满足交易门开放条件，启动流程停在预热阶段。",
                current_step="warmup",
                current_blocker="交易门尚未开放，仍有待完成标的或完整性阻塞",
                operator_action="等待后续行情 / 回补推进，必要时人工检查阻塞标的",
                steps={
                    "warmup": {
                        "status": "failed" if last_error else "waiting",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"pending={self._format_symbol_list(readiness['pending_symbols'])}"
                        ),
                    },
                    "trading_gate": {
                        "status": "pending",
                        "detail": "等待交易门开放。",
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "完整性阻塞": self._format_symbol_list(
                            readiness["integrity_pending_symbols"]
                        ),
                        "最近异常": last_error or "",
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )
            if self._warmup_uses_remote_compute_service():
                pending_sources = {
                    str((item or {}).get("source") or "").strip()
                    for item in (readiness.get("symbol_status") or [])
                    if not bool((item or {}).get("ready"))
                }
                retryable_sources = {
                    "remote_compute_status_missing",
                    "remote_compute_status_unavailable",
                }
                if pending_sources and pending_sources.issubset(retryable_sources):
                    retry_delay_s = max(
                        1,
                        self.config.get_int_for_environment(
                            "ibkr_warmup_remote_compute_retry_sec",
                            service_mod.ENVIRONMENT,
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
