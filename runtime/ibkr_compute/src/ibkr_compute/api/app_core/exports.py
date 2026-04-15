from __future__ import annotations

from ibkr_compute.api.app_core.exports_compute import COMPUTE_EXPORTS
from ibkr_compute.api.app_core.exports_runtime import RUNTIME_EXPORTS
from ibkr_compute.api.app_core.exports_support import SUPPORT_EXPORTS


APP_EXPORTS = {
    **SUPPORT_EXPORTS,
    **COMPUTE_EXPORTS,
    **RUNTIME_EXPORTS,
}


def apply_app_exports(target_globals: dict):
    target_globals.update(APP_EXPORTS)
