from __future__ import annotations

from datetime import datetime

from .warmup_cycle_support import _service_mod

class WarmupCyclePreflightRepairMixin:
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

