from __future__ import annotations

from ibkr_compute.market.timeframe_utils import normalize_interval

from ibkr_compute.api.chart.compare.request import get_chart_compare_request_period
from ibkr_compute.api.chart.timeline.runtime import _api_app
from ibkr_compute.api.chart.timeline.source import (
    build_chart_source_window_from_rows,
)
from ibkr_compute.api.shared.service_status import get_service_status_snapshot


def load_chart_compare_ibkr_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    service = api_app.get_ibkr_service()
    if not service:
        raise RuntimeError("IBKR service not initialized")
    if not hasattr(service, "conid_resolver") or service.conid_resolver is None:
        raise RuntimeError("IBKR contract resolver unavailable")
    if not hasattr(service, "data_backfill") or service.data_backfill is None:
        raise RuntimeError("IBKR history fetch unavailable")

    api_app._maybe_restore_ibkr_service(service)
    service_status = get_service_status_snapshot(service)
    gateway_running = bool((service_status.get("gateway") or {}).get("running"))
    session_authenticated = bool((service_status.get("session") or {}).get("authenticated"))
    if not gateway_running:
        raise RuntimeError("IBKR gateway not running")
    if not session_authenticated:
        raise RuntimeError("IBKR session not authenticated")

    conid = int(service.conid_resolver.resolve(normalized_symbol) or 0)
    if conid <= 0:
        raise RuntimeError(f"Cannot resolve conid for {normalized_symbol}")

    symbol_meta = api_app.refresh_symbol_metadata().get(normalized_symbol, {})
    request_period = get_chart_compare_request_period(normalized_interval, start_ms=start_ms, end_ms=end_ms)
    fetched_rows = service.data_backfill.fetch_history(
        conid,
        normalized_symbol,
        interval=normalized_interval,
        exchange=str(symbol_meta.get("exchange") or ""),
        repair=True,
        request_period=request_period,
    )

    normalized_rows = []
    for row in fetched_rows:
        payload = api_app.normalize_bar_environment(row, runtime_environment)
        payload["source"] = "ibkr_chart_compare"
        extra = api_app.parse_json_object(payload.get("extra"))
        extra["compare_chain"] = "ibkr_api"
        extra["request_period"] = request_period
        payload["extra"] = extra
        normalized_rows.append(payload)

    source = build_chart_source_window_from_rows(
        normalized_rows,
        normalized_interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    source["meta"] = {
        "chain": "ibkr_api",
        "conid": conid,
        "gateway_running": gateway_running,
        "session_authenticated": session_authenticated,
        "request_period": request_period,
        "fetched_bar_count": len(normalized_rows),
    }
    return source


__all__ = ["load_chart_compare_ibkr_source_bars"]
