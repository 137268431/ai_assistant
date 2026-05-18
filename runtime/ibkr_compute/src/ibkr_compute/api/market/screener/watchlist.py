from __future__ import annotations

from ibkr_compute.api.market.screener.runtime import get_api_app
from ibkr_compute.core.broker_mode import resolve_data_environment


def load_effective_watchlist(environment: str) -> dict:
    api_app = get_api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    data_environment = resolve_data_environment(runtime_environment)
    rows = api_app.pb.get_all_records(
        "watchlist",
        filter=(
            f'environment = "{data_environment}" '
            '|| environment = "global" '
            '|| environment = ""'
        ),
        sort="-updated",
        max_pages=30,
    )
    merged = {}
    applied = {}
    priority = {"": 0, "global": 1, data_environment: 2}
    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        row_environment = str(row.get("environment", "") or "").strip().lower()
        rank = priority.get(row_environment, -1)
        if rank < 0:
            continue
        if symbol in applied and applied[symbol] > rank:
            continue
        applied[symbol] = rank
        normalized_row = dict(row)
        normalized_row["symbol_role"] = api_app.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))
        merged[symbol] = normalized_row
    return merged


__all__ = ["load_effective_watchlist"]
