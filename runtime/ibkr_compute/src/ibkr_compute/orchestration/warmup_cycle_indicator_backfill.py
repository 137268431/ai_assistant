from __future__ import annotations

import math
import time

from .market_universe_targets import _bar_pipeline_skip_reason
from .warmup_cycle_support import _service_mod

class WarmupCycleIndicatorBackfillMixin:
    def _warmup_indicator_backfill_intervals(self) -> list[str]:
        service_mod = _service_mod()
        if not self.config.get_bool_for_environment(
            "ibkr_warmup_indicator_backfill_enabled",
            service_mod.DATA_ENVIRONMENT,
            True,
        ):
            return []
        configured = self.config.get_for_environment(
            "ibkr_warmup_indicator_backfill_intervals",
            service_mod.DATA_ENVIRONMENT,
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
                service_mod.DATA_ENVIRONMENT,
                390,
            ),
        )
        calendar_multiplier = max(
            1.0,
            self.config.get_float_for_environment(
                "ibkr_warmup_indicator_calendar_multiplier",
                service_mod.DATA_ENVIRONMENT,
                1.4,
            ),
        )
        buffer_days = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_buffer_days",
                service_mod.DATA_ENVIRONMENT,
                5,
            ),
        )
        max_days = max(
            base_days,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_max_days",
                service_mod.DATA_ENVIRONMENT,
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
                service_mod.DATA_ENVIRONMENT,
                3,
            ),
        )
        retry_delay_s = max(
            0,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_prime_retry_delay_sec",
                service_mod.DATA_ENVIRONMENT,
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
                        "environments": [service_mod.DATA_ENVIRONMENT],
                        "market_data_mode": service_mod.DATA_ENVIRONMENT,
                        "broker_mode": service_mod.ENVIRONMENT,
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
                    service_mod.DATA_ENVIRONMENT,
                    self._normalize_symbol_list(symbols),
                    interval,
                    persist_latest_indicator=False,
                )
        return prime_result

    def _run_warmup_indicator_backfill(self, snapshot: dict) -> dict:
        service_mod = _service_mod()
        period = "bar_only"
        bar_skip_reason = _bar_pipeline_skip_reason(self, service_mod)
        if bar_skip_reason:
            service_mod.logger.info("Warmup indicator backfill skipped: reason=%s", bar_skip_reason)
            return {
                "period": period,
                "plan": {},
                "written_total": 0,
                "backfill": {},
                "prime": {},
                "skipped": True,
                "skip_reason": bar_skip_reason,
            }
        max_passes = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_warmup_indicator_backfill_passes",
                service_mod.DATA_ENVIRONMENT,
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
