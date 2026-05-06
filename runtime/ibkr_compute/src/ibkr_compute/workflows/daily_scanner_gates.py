"""Data-completeness gates for the daily IBKR scanner."""

from __future__ import annotations

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
            return False
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_data_completeness_blocking_enabled",
                    environment,
                    False,
                )
            )
        except Exception:
            return False

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
        for symbol in normalized_symbols:
            freshness = planner.plan_symbol(symbol, intervals, environment=environment, required_bars=0)
            items[symbol] = freshness
            if not bool(freshness.get("needs_repair")):
                continue
            incomplete_symbols.append(symbol)
            if coordinator is not None and hasattr(coordinator, "enqueue_from_freshness"):
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
        return {
            "enabled": True,
            "blocking_enabled": blocking_enabled,
            "intervals": intervals,
            "items": items,
            "incomplete_symbols": incomplete_symbols,
            "repairing_symbols": incomplete_symbols,
            "repair_job_count": repair_job_count,
            "repair_jobs": repair_jobs,
            "status": "ready" if not incomplete_symbols else "repairing",
        }
