"""Compatibility HTTP API entrypoint.

Primary implementation now lives in ibkr_compute.api.app.
"""

from .app import *  # noqa: F401,F403


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "5100"))
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
