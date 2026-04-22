from __future__ import annotations

"""IBKR runtime HTTP entrypoint.

The runtime service keeps its bootstrap module in `runtime/ibkr_runtime`, while
the shared trading/runtime implementation still lives in `ibkr_compute` during
the compatibility migration window.
"""

from ibkr_compute.api.app import *  # noqa: F401,F403
