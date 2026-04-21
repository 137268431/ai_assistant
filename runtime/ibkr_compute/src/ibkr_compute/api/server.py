"""Compatibility HTTP API entrypoint.

Primary implementation now lives in ibkr_compute.api.app.
"""

from .app import *  # noqa: F401,F403
from .app import _run_internal_compute, _run_internal_scan  # noqa: F401


if __name__ == "__main__":
    import os

    from .startup_preload import schedule_compute_startup_preload

    port = int(os.environ.get("PORT", "5100"))
    schedule_compute_startup_preload()
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
