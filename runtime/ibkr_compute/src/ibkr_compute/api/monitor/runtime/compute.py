from __future__ import annotations

import time
from datetime import datetime, timezone

from ibkr_compute.api.monitor.host import _api_app


def _build_compute_summary() -> dict:
    api_app = _api_app()
    return {
        "status": "running",
        "total_engines": len(api_app.engines),
        "ready_engines": sum(1 for engine in api_app.engines.values() if engine.is_ready()),
        "tracked_cursors": len(api_app.last_processed_ms),
        "compute_count": api_app.compute_count,
        "error_count": api_app.error_count,
        "last_compute": datetime.fromtimestamp(api_app.last_compute_time, timezone.utc).isoformat() if api_app.last_compute_time else None,
        "last_scan": datetime.fromtimestamp(api_app.last_scan_time, timezone.utc).isoformat() if api_app.last_scan_time else None,
        "uptime_s": round(time.time() - api_app._start_time, 1),
    }


__all__ = ["_build_compute_summary"]
