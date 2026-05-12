"""Engine materialization support for the daily IBKR scanner."""

from __future__ import annotations

import time

from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, interval_to_ms, normalize_interval

from .daily_scanner_constants import DEFAULT_SCAN_MATERIALIZE_INTERVALS, DEFAULT_SCAN_ROLLUP_INTERVALS
from .daily_scanner_support import _safe_int


class DailyScannerMaterializeMixin:
    def _scan_rollup_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        has_rollup = hasattr(self.api_app, "ensure_higher_timeframe_bars")
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return has_rollup
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_rollup_enabled",
                    environment,
                    True,
                )
            ) and has_rollup
        except Exception:
            return has_rollup

    def _scan_rollup_incremental(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return True
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_rollup_incremental",
                    environment,
                    True,
                )
            )
        except Exception:
            return True

    def _scan_rollup_intervals(self, environment: str) -> list[str]:
        cfg = getattr(self.api_app, "cfg", None)
        raw = DEFAULT_SCAN_ROLLUP_INTERVALS
        if cfg is not None and hasattr(cfg, "get_for_environment"):
            try:
                raw = str(cfg.get_for_environment("ibkr_daily_scan_rollup_intervals", environment, raw) or raw)
            except Exception:
                raw = DEFAULT_SCAN_ROLLUP_INTERVALS
        parsed = [normalize_interval(item) for item in raw.split(",") if str(item or "").strip()]
        parsed = [item for item in dict.fromkeys(parsed) if item in COMPUTE_INTERVALS and item != "5m"]
        return parsed or [normalize_interval(item) for item in DEFAULT_SCAN_ROLLUP_INTERVALS.split(",")]

    def _rollup_scan_timeframes(self, environment: str, symbols: list[str]) -> dict:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols or not self._scan_rollup_enabled(environment):
            return {"enabled": False, "symbols_total": len(normalized_symbols), "intervals": []}

        rollup = getattr(self.api_app, "ensure_higher_timeframe_bars", None)
        recent_rollup = getattr(self.api_app, "rebuild_higher_timeframe_bars", None)
        intervals = self._scan_rollup_intervals(environment)
        incremental = self._scan_rollup_incremental(environment)
        try:
            if incremental and callable(recent_rollup):
                max_interval_ms = max(interval_to_ms(interval) for interval in intervals)
                lookback_ms = max_interval_ms * 2
                if "1d" in intervals:
                    # Daily provisional/closed context must survive weekends and holidays.
                    lookback_ms = max(lookback_ms, 7 * 24 * 60 * 60 * 1000)
                since_ms = max(0, int(time.time() * 1000) - lookback_ms)
                result = recent_rollup(
                    environment,
                    symbols=normalized_symbols,
                    intervals=intervals,
                    since_ms=since_ms,
                )
                return {
                    "enabled": True,
                    "symbols_total": len(normalized_symbols),
                    "intervals": intervals,
                    "incremental": True,
                    "recent_window": True,
                    **(result if isinstance(result, dict) else {}),
                }
            result = rollup(
                [environment],
                force=True,
                symbols=normalized_symbols,
                incremental=incremental,
                intervals=intervals,
            )
            env_result = result.get(environment, {}) if isinstance(result, dict) else {}
            return {
                "enabled": True,
                "symbols_total": len(normalized_symbols),
                "intervals": intervals,
                "incremental": bool(incremental),
                **(env_result if isinstance(env_result, dict) else {}),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "symbols_total": len(normalized_symbols),
                "intervals": intervals,
                "incremental": bool(incremental),
                "errors": 1,
                "error": str(exc),
            }

    def _scan_materialize_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        has_materializer = hasattr(self.api_app, "materialize_engines_from_storage")
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return has_materializer
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_materialize_enabled",
                    environment,
                    True,
                )
            ) and has_materializer
        except Exception:
            return has_materializer

    def _scan_materialize_intervals(self, environment: str) -> list[str]:
        cfg = getattr(self.api_app, "cfg", None)
        raw = DEFAULT_SCAN_MATERIALIZE_INTERVALS
        if cfg is not None and hasattr(cfg, "get_for_environment"):
            try:
                raw = str(cfg.get_for_environment("ibkr_daily_scan_materialize_intervals", environment, raw) or raw)
            except Exception:
                raw = DEFAULT_SCAN_MATERIALIZE_INTERVALS
        parsed = [normalize_interval(item) for item in raw.split(",") if str(item or "").strip()]
        parsed = [item for item in dict.fromkeys(parsed) if item in COMPUTE_INTERVALS]
        return parsed or [normalize_interval(DEFAULT_SCAN_MATERIALIZE_INTERVALS)]

    def _materialize_scan_engines(self, environment: str, symbols: list[str]) -> dict:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols or not self._scan_materialize_enabled(environment):
            return {"enabled": False, "symbols_total": len(normalized_symbols), "intervals": []}

        materialize = getattr(self.api_app, "materialize_engines_from_storage", None)
        intervals = self._scan_materialize_intervals(environment)
        interval_results = {}
        ready_symbols = set()
        processed_total = 0
        errors = []
        for interval in intervals:
            try:
                raw_result = materialize(
                    environment,
                    normalized_symbols,
                    interval=interval,
                    hydrate_signal_state=False,
                    persist_latest_indicator=False,
                ) or {}
            except Exception as exc:
                errors.append({"interval": interval, "error": str(exc)})
                continue

            ready = sorted(
                str(symbol or "").strip().upper()
                for symbol, item in raw_result.items()
                if isinstance(item, dict) and bool(item.get("is_ready"))
            )
            ready_symbols.update(ready)
            processed = sum(
                _safe_int((item or {}).get("processed"), 0)
                for item in raw_result.values()
                if isinstance(item, dict)
            )
            processed_total += processed
            reason_counts: dict[str, int] = {}
            missing_examples = []
            for symbol, item in raw_result.items():
                if not isinstance(item, dict) or bool(item.get("is_ready")):
                    continue
                reason = str(item.get("reason") or "not_ready")
                reason_counts[reason] = int(reason_counts.get(reason, 0) or 0) + 1
                if len(missing_examples) < 10:
                    missing_examples.append(str(symbol or "").strip().upper())
            interval_results[interval] = {
                "ready_count": len(ready),
                "not_ready_count": max(0, len(normalized_symbols) - len(ready)),
                "processed": processed,
                "reason_counts": reason_counts,
                "not_ready_examples": missing_examples,
            }

        return {
            "enabled": True,
            "intervals": intervals,
            "symbols_total": len(normalized_symbols),
            "ready_symbol_count": len(ready_symbols),
            "processed": processed_total,
            "errors": errors,
            "results": interval_results,
        }
