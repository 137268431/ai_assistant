"""Stored indicator snapshot loading for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS, normalize_interval

from .daily_scanner_constants import DEFAULT_INDICATOR_SNAPSHOT_INTERVALS
from .daily_scanner_support import _safe_extra


class DailyScannerStoredSnapshotsMixin:
    def _stored_indicator_snapshots_enabled(self, environment: str) -> bool:
        cfg = getattr(self.api_app, "cfg", None)
        if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
            return True
        try:
            return bool(
                cfg.get_bool_for_environment(
                    "ibkr_daily_scan_indicator_snapshot_enabled",
                    environment,
                    True,
                )
            )
        except Exception:
            return True

    def _stored_indicator_snapshot_intervals(self, environment: str) -> list[str]:
        cfg = getattr(self.api_app, "cfg", None)
        raw = DEFAULT_INDICATOR_SNAPSHOT_INTERVALS
        if cfg is not None and hasattr(cfg, "get_for_environment"):
            try:
                raw = str(cfg.get_for_environment("ibkr_daily_scan_indicator_snapshot_intervals", environment, raw) or raw)
            except Exception:
                raw = DEFAULT_INDICATOR_SNAPSHOT_INTERVALS
        parsed = [normalize_interval(item) for item in raw.split(",") if str(item or "").strip()]
        parsed = [item for item in dict.fromkeys(parsed) if item in COMPUTE_INTERVALS]
        return parsed or [normalize_interval(DEFAULT_INDICATOR_SNAPSHOT_INTERVALS)]

    def _indicator_interval_aliases(self, interval: str) -> list[str]:
        normalized = normalize_interval(interval)
        aliases = [normalized]
        numeric_aliases = {
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "1D",
        }
        numeric = numeric_aliases.get(normalized)
        if numeric:
            aliases.append(numeric)
        return list(dict.fromkeys(aliases))

    def _load_stored_indicator_snapshots(self, environment: str, symbols: list[str]) -> dict[str, dict[str, dict]]:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols or not self._stored_indicator_snapshots_enabled(runtime_environment):
            return {}
        intervals = self._stored_indicator_snapshot_intervals(runtime_environment)
        snapshots: dict[str, dict[str, dict]] = {symbol: {} for symbol in normalized_symbols}
        safe_environment = runtime_environment.replace('"', '\\"')
        for symbol in normalized_symbols:
            safe_symbol = symbol.replace('"', '\\"')
            for interval in intervals:
                interval_filter = " || ".join(
                    f'interval = "{alias.replace(chr(34), chr(92) + chr(34))}"'
                    for alias in self._indicator_interval_aliases(interval)
                )
                try:
                    rows = self.pb_client.get_records(
                        "ibkr_indicators",
                        filter=(
                            f'environment = "{safe_environment}" && '
                            f'symbol = "{safe_symbol}" && '
                            f"({interval_filter})"
                        ),
                        sort="-bar_time_ms",
                        per_page=1,
                    )
                except Exception:
                    rows = []
                if not rows:
                    continue
                row = rows[0]
                extra = _safe_extra(row)
                if not extra:
                    continue
                extra.setdefault("environment", runtime_environment)
                extra.setdefault("symbol", symbol)
                extra.setdefault("chart_tf", interval)
                extra.setdefault("bar_time_ms", row.get("bar_time_ms", 0) or 0)
                extra.setdefault("us_time", row.get("us_time", "") or "")
                snapshots[symbol][interval] = extra
        return {symbol: rows for symbol, rows in snapshots.items() if rows}
