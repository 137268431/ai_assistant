from __future__ import annotations

import time
from datetime import datetime, timezone

from ibkr_compute.api.monitor.host import _api_app
from ibkr_compute.api.ops.common import _snapshot_engine_items
from ibkr_compute.api.startup_preload import get_compute_startup_preload_state


def _build_compute_summary() -> dict:
    api_app = _api_app()
    engine_items = _snapshot_engine_items(api_app, blocking=False)
    return {
        "status": "running",
        "total_engines": len(engine_items),
        "ready_engines": sum(1 for _, engine in engine_items if engine.is_ready()),
        "tracked_cursors": len(api_app.last_processed_ms),
        "compute_count": api_app.compute_count,
        "error_count": api_app.error_count,
        "last_compute": datetime.fromtimestamp(api_app.last_compute_time, timezone.utc).isoformat() if api_app.last_compute_time else None,
        "last_scan": datetime.fromtimestamp(api_app.last_scan_time, timezone.utc).isoformat() if api_app.last_scan_time else None,
        "uptime_s": round(time.time() - api_app._start_time, 1),
        "compute_startup_preload": get_compute_startup_preload_state(api_app),
    }


__all__ = ["_build_compute_summary"]
