from __future__ import annotations

from ibkr_compute.api.compat.aliases.account import ACCOUNT_ALIASES
from ibkr_compute.api.compat.aliases.app_core import APP_CORE_ALIASES
from ibkr_compute.api.compat.aliases.chart import CHART_ALIASES
from ibkr_compute.api.compat.aliases.compute import COMPUTE_ALIASES
from ibkr_compute.api.compat.aliases.legacy import LEGACY_ALIASES
from ibkr_compute.api.compat.aliases.market import MARKET_ALIASES
from ibkr_compute.api.compat.aliases.monitor import MONITOR_ALIASES
from ibkr_compute.api.compat.aliases.ops import OPS_ALIASES
from ibkr_compute.api.compat.aliases.route import ROUTE_ALIASES
from ibkr_compute.api.compat.aliases.runtime import RUNTIME_ALIASES


COMPAT_ALIAS_GROUPS = (
    ACCOUNT_ALIASES,
    APP_CORE_ALIASES,
    CHART_ALIASES,
    COMPUTE_ALIASES,
    LEGACY_ALIASES,
    MARKET_ALIASES,
    MONITOR_ALIASES,
    OPS_ALIASES,
    ROUTE_ALIASES,
    RUNTIME_ALIASES,
)

COMPAT_MODULE_ALIASES = {}
for alias_group in COMPAT_ALIAS_GROUPS:
    COMPAT_MODULE_ALIASES.update(alias_group)


__all__ = [
    "ACCOUNT_ALIASES",
    "APP_CORE_ALIASES",
    "CHART_ALIASES",
    "COMPAT_ALIAS_GROUPS",
    "COMPAT_MODULE_ALIASES",
    "COMPUTE_ALIASES",
    "LEGACY_ALIASES",
    "MARKET_ALIASES",
    "MONITOR_ALIASES",
    "OPS_ALIASES",
    "ROUTE_ALIASES",
    "RUNTIME_ALIASES",
]
