from __future__ import annotations

import os

from .api_app import PB_BASE_URL, app


def main() -> None:
    port = int(os.environ.get("IBKR_API_PORT") or os.environ.get("PORT", "5102"))
    print(f"[IBKR API] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
