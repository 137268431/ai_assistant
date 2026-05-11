"""Data-completeness gates for the daily IBKR scanner."""

from __future__ import annotations

import time

from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

from .daily_scanner_constants import (
    DEFAULT_DATA_COMPLETENESS_INTERVALS,
    MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT,
)


class DailyScannerDataCompletenessMixin:
    def _data_completeness_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return hasattr(self.api_app, "bar_freshness_planner")
        try:
            return bool(cfg.get_bool_for_environment("ibkr_daily_scan_data_completeness_enabled", environment, True))
        except Exception:
            return hasattr(self.api_app, "bar_freshness_planner")

    def _data_completeness_blocking_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return True
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_data_completeness_blocking_enabled",
                    environment,
                    True,
                )
            )
        except Exception:
            return True

    def _data_completeness_intervals(self, environment: str) -> list[str]:
        cfg = getattr(self.api_app, "cfg", None)
        raw = DEFAULT_DATA_COMPLETENESS_INTERVALS
        if cfg is not None and hasattr(cfg, "get_for_environment"):
            try:
                raw = str(cfg.get_for_environment("ibkr_daily_scan_data_completeness_intervals", environment, raw) or raw)
            except Exception:
                raw = DEFAULT_DATA_COMPLETENESS_INTERVALS
        parsed = [normalize_interval(item) for item in raw.split(",") if str(item or "").strip()]
        parsed = [item for item in dict.fromkeys(parsed) if item in COMPUTE_INTERVALS]
        return parsed or [normalize_interval(DEFAULT_DATA_COMPLETENESS_INTERVALS)]

    def _data_completeness_runtime_topup_wait_sec(self, environment: str) -> int:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_int_for_environment"):
            return 0
        try:
            return max(0, min(60, int(cfg.get_int_for_environment(
                "ibkr_daily_scan_runtime_topup_wait_sec",
                environment,
                20,
            ) or 0)))
        except Exception:
            return 0

    def _data_completeness_runtime_topup_poll_sec(self, environment: str) -> float:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_float_for_environment"):
            return 2.0
        try:
            return max(0.5, min(5.0, float(cfg.get_float_for_environment(
                "ibkr_daily_scan_runtime_topup_poll_sec",
                environment,
                2.0,
            ) or 2.0)))
        except Exception:
            return 2.0

    def _runtime_watchlist_topup_fresh(self) -> bool:
        try:
            from ibkr_compute.api.runtime_status_client import get_remote_runtime_status

            payload = get_remote_runtime_status(force_refresh=True)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        topup = payload.get("watchlist_idle_topup")
        if not isinstance(topup, dict):
            return False
        completion = topup.get("completion")
        if not isinstance(completion, dict):
            return False
        total = int(completion.get("total") or 0)
        fresh = int(completion.get("fresh") or 0)
        stale = int(completion.get("stale") or 0)
        missing = int(completion.get("missing") or 0)
        unobserved = int(completion.get("unobserved") or 0)
        expected_ms = int(completion.get("expected_latest_5m_ms") or 0)
        oldest_ms = int(completion.get("oldest_latest_ms") or 0)
        return total > 0 and fresh >= total and stale <= 0 and missing <= 0 and unobserved <= 0 and (
            expected_ms <= 0 or oldest_ms >= expected_ms
        )

    def _wait_for_runtime_watchlist_topup_if_due(self, environment: str) -> bool:
        wait_sec = self._data_completeness_runtime_topup_wait_sec(environment)
        if wait_sec <= 0:
            return False
        deadline = time.monotonic() + wait_sec
        poll_sec = self._data_completeness_runtime_topup_poll_sec(environment)
        while time.monotonic() < deadline:
            if self._runtime_watchlist_topup_fresh():
                return True
            time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
        return self._runtime_watchlist_topup_fresh()

    def _plan_data_completeness_symbols(
        self,
        *,
        planner,
        symbols: list[str],
        intervals: list[str],
        environment: str,
        coordinator,
        enqueue_repairs: bool,
    ) -> tuple[dict, list[str], list, int]:
        items = {}
        incomplete_symbols = []
        repair_jobs = []
        repair_job_count = 0
        for symbol in symbols:
            freshness = planner.plan_symbol(symbol, intervals, environment=environment, required_bars=0)
            items[symbol] = freshness
            if not bool(freshness.get("needs_repair")):
                continue
            incomplete_symbols.append(symbol)
            if enqueue_repairs and coordinator is not None and hasattr(coordinator, "enqueue_from_freshness"):
                try:
                    queued_jobs = list(
                        coordinator.enqueue_from_freshness(
                            freshness,
                            priority="daily_scan",
                            trigger="daily_scan_data_completeness",
                        )
                        or []
                    )
                    repair_job_count += len(queued_jobs)
                    remaining_slots = MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT - len(repair_jobs)
                    if remaining_slots > 0:
                        repair_jobs.extend(queued_jobs[:remaining_slots])
                except Exception as exc:
                    repair_job_count += 1
                    if len(repair_jobs) < MAX_DATA_COMPLETENESS_REPAIR_JOBS_IN_RESULT:
                        repair_jobs.append({"queued": False, "symbol": symbol, "error": str(exc)})
        return items, incomplete_symbols, repair_jobs, repair_job_count

    def _build_data_completeness_gate(self, environment: str, symbols: list[str]) -> dict:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols or not self._data_completeness_enabled(environment):
            return {"enabled": False, "items": {}, "incomplete_symbols": [], "repair_jobs": []}

        planner = getattr(self.api_app, "bar_freshness_planner", None)
        if planner is None:
            planner = self._build_bar_freshness_planner(environment)
        intervals = self._data_completeness_intervals(environment)
        blocking_enabled = self._data_completeness_blocking_enabled(environment)
        items = {}
        incomplete_symbols = []
        repair_jobs = []
        repair_job_count = 0
        coordinator = getattr(self.api_app, "bar_repair_coordinator", None)
        repair_strategy = (
            "bar_repair_queue"
            if coordinator is not None and hasattr(coordinator, "enqueue_from_freshness")
            else "runtime_watchlist_idle_topup"
        )
        items, incomplete_symbols, repair_jobs, repair_job_count = self._plan_data_completeness_symbols(
            planner=planner,
            symbols=normalized_symbols,
            intervals=intervals,
            environment=environment,
            coordinator=coordinator,
            enqueue_repairs=True,
        )
        runtime_topup_waited = False
        if incomplete_symbols and repair_strategy == "runtime_watchlist_idle_topup":
            runtime_topup_waited = self._wait_for_runtime_watchlist_topup_if_due(environment)
            if runtime_topup_waited:
                items, incomplete_symbols, repair_jobs, repair_job_count = self._plan_data_completeness_symbols(
                    planner=planner,
                    symbols=normalized_symbols,
                    intervals=intervals,
                    environment=environment,
                    coordinator=coordinator,
                    enqueue_repairs=False,
                )
        return {
            "enabled": True,
            "blocking_enabled": blocking_enabled,
            "intervals": intervals,
            "items": items,
            "incomplete_symbols": incomplete_symbols,
            "repairing_symbols": incomplete_symbols,
            "repair_strategy": repair_strategy,
            "runtime_topup_waited": runtime_topup_waited,
            "repair_job_count": repair_job_count,
            "repair_jobs": repair_jobs,
            "status": "ready" if not incomplete_symbols else "repairing",
        }
