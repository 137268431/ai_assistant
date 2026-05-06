"""Watchlist loading for the daily IBKR scanner."""

from __future__ import annotations

from .daily_scanner_constants import WATCHLIST_SYMBOL_ROLE_TRADE


class DailyScannerWatchlistMixin:
    def _get_watchlist(self, environment: str) -> list:
        try:
            records = self.pb_client.get_records(
                "watchlist",
                filter=(
                    f'(environment = "{environment}" || environment = "global" || environment = "") '
                    f'&& (symbol_role = "{WATCHLIST_SYMBOL_ROLE_TRADE}" || symbol_role = "")'
                ),
                per_page=500,
            )
            merged = {}
            priority = {"": 0, "global": 1, environment: 2}
            applied = {}
            for item in records:
                symbol = str(item.get("symbol", "")).upper()
                if not symbol:
                    continue
                env = str(item.get("environment", "") or "").strip().lower()
                rank = priority.get(env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = item
            return list(merged.values())
        except Exception as exc:
            print(f"[Scanner] get watchlist error: {exc}")
            return []
