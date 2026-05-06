"""Engine materialization support for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

from .daily_scanner_constants import DEFAULT_SCAN_MATERIALIZE_INTERVALS
from .daily_scanner_support import _safe_int


class DailyScannerMaterializeMixin:
    def _scan_materialize_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        has_materializer = hasattr(self.api_app, "materialize_engines_from_storage")
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return False
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_materialize_enabled",
                    environment,
                    False,
                )
            ) and has_materializer
        except Exception:
            return False

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
