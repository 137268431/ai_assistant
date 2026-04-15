from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_snapshot_sources(service, account_id: str) -> dict:
    summary_raw = {}
    positions_raw = []
    orders_raw = []
    summary_error = ""
    positions_error = ""
    orders_error = ""

    fetchers = {}
    if hasattr(service, "order_lifecycle") and service.order_lifecycle:
        fetchers["summary"] = lambda: service.order_lifecycle.get_account_summary(account_id)
        fetchers["positions"] = lambda: service.order_lifecycle.get_positions(account_id)
    if hasattr(service, "order_tracker") and service.order_tracker:
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
                    if name == "summary":
                        summary_error = str(exc)
                    elif name == "positions":
                        positions_error = str(exc)
                    else:
                        orders_error = str(exc)
                    continue

                if name == "summary":
                    summary_raw = value if isinstance(value, dict) else {}
                elif name == "positions":
                    positions_raw = value if isinstance(value, list) else []
                else:
                    orders_raw = value if isinstance(value, list) else []

    return {
        "summary_raw": summary_raw,
        "positions_raw": positions_raw,
        "orders_raw": orders_raw,
        "errors": {
            "summary": summary_error,
            "positions": positions_error,
            "orders": orders_error,
        },
    }


__all__ = ["fetch_snapshot_sources"]
