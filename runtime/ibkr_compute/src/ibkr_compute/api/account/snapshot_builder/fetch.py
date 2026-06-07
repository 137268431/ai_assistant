from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_snapshot_sources(service, account_id: str, *, include_pnl: bool = True) -> dict:
    summary_raw = {}
    pnl_raw = {}
    positions_raw = []
    orders_raw = []
    summary_error = ""
    pnl_error = ""
    positions_error = ""
    orders_error = ""
    account_snapshot_requested = False
    positions_loaded = False
    orders_loaded = False

    fetchers = {}
    if hasattr(service, "order_lifecycle") and service.order_lifecycle:
        snapshot_getter = getattr(service.order_lifecycle, "get_account_snapshot", None)
        if callable(snapshot_getter):
            account_snapshot_requested = True
            fetchers["account_snapshot"] = lambda: snapshot_getter(account_id)
        else:
            fetchers["summary"] = lambda: service.order_lifecycle.get_account_summary(account_id)
            fetchers["positions"] = lambda: service.order_lifecycle.get_positions(account_id)
        pnl_getter = getattr(service.order_lifecycle, "get_account_pnl", None)
        if include_pnl and callable(pnl_getter):
            fetchers["pnl"] = lambda: pnl_getter(account_id)
    if hasattr(service, "order_tracker") and service.order_tracker:
        cached_getter = getattr(service.order_tracker, "get_cached_live_orders", None)
        cached_orders = []
        if callable(cached_getter):
            try:
                cached_orders = list(cached_getter() or [])
            except Exception:
                cached_orders = []
        if cached_orders:
            fetchers["orders"] = lambda: cached_orders
        else:
            fetchers["orders"] = service.order_tracker.get_live_orders

    if fetchers:
        with ThreadPoolExecutor(max_workers=len(fetchers), thread_name_prefix="ibkr-account") as executor:
            future_map = {
                executor.submit(fetcher): name
                for name, fetcher in fetchers.items()
            }
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    value = future.result()
                except Exception as exc:
                    if name == "account_snapshot":
                        summary_error = str(exc)
                        positions_error = str(exc)
                    elif name == "summary":
                        summary_error = str(exc)
                    elif name == "positions":
                        positions_error = str(exc)
                    elif name == "pnl":
                        pnl_error = str(exc)
                    else:
                        orders_error = str(exc)
                    continue

                if name == "account_snapshot":
                    payload = value if isinstance(value, dict) else {}
                    summary_raw = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
                    if isinstance(payload.get("positions"), list):
                        positions_raw = payload.get("positions")
                        positions_loaded = True
                elif name == "summary":
                    summary_raw = value if isinstance(value, dict) else {}
                elif name == "positions":
                    positions_raw = value if isinstance(value, list) else []
                    positions_loaded = isinstance(value, list)
                elif name == "pnl":
                    payload = value if isinstance(value, dict) else {}
                    if payload.get("error") and not bool(payload.get("ok", True)):
                        pnl_error = str(payload.get("error") or "")
                        pnl_raw = {}
                    else:
                        pnl_raw = payload
                else:
                    orders_raw = value if isinstance(value, list) else []
                    orders_loaded = isinstance(value, list)

    # Fall back to the lightweight socket calls if the richer account download path
    # yields no usable payload. This preserves the pre-existing behavior while
    # allowing the account page to show full valuation fields when available.
    if account_snapshot_requested and hasattr(service, "order_lifecycle") and service.order_lifecycle:
        if not summary_raw:
            try:
                summary_raw = service.order_lifecycle.get_account_summary(account_id)
                summary_error = ""
            except Exception as exc:
                summary_error = summary_error or str(exc)
        if not positions_loaded:
            try:
                positions_raw = service.order_lifecycle.get_positions(account_id)
                positions_loaded = True
                positions_error = ""
            except Exception as exc:
                positions_error = positions_error or str(exc)

    return {
        "summary_raw": summary_raw,
        "pnl_raw": pnl_raw,
        "positions_raw": positions_raw,
        "orders_raw": orders_raw,
        "positions_loaded": positions_loaded,
        "orders_loaded": orders_loaded,
        "errors": {
            "summary": summary_error,
            "pnl": pnl_error,
            "positions": positions_error,
            "orders": orders_error,
        },
    }


__all__ = ["fetch_snapshot_sources"]
