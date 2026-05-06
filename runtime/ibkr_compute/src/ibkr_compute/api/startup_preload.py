from __future__ import annotations

from . import startup_preload_config_resolvers as _config_resolvers
from . import startup_preload_direct_backfill as _direct_backfill
from . import startup_preload_indicator_preload as _indicator_preload
from . import startup_preload_scheduler_entrypoints as _scheduler_entrypoints
from . import startup_preload_state_store as _state_store
from .startup_preload_config_resolvers import *
from .startup_preload_direct_backfill import *
from .startup_preload_indicator_preload import *
from .startup_preload_scheduler_entrypoints import *
from .startup_preload_state_store import *

_COMPAT_MODULES = (
    _state_store,
    _config_resolvers,
    _direct_backfill,
    _indicator_preload,
    _scheduler_entrypoints,
)
_FACADE_PUBLICS = {
    "get_compute_startup_preload_state",
    "is_compute_startup_preload_enabled",
    "resolve_compute_startup_preload_environments",
    "resolve_compute_startup_preload_intervals",
    "resolve_startup_direct_backfill_enabled",
    "resolve_startup_direct_backfill_intervals",
    "resolve_startup_direct_backfill_required_bars",
    "run_compute_startup_preload",
    "schedule_compute_startup_preload",
    "should_schedule_compute_startup_preload",
}


def _sync_compat_symbols() -> None:
    current = globals()
    for module in _COMPAT_MODULES:
        for name in list(vars(module)):
            if name.startswith("__"):
                continue
            if name in _FACADE_PUBLICS and current.get(name) is _FACADE_WRAPPERS.get(name):
                continue
            if name in current:
                setattr(module, name, current[name])


def is_compute_startup_preload_enabled() -> bool:
    _sync_compat_symbols()
    return _config_resolvers.is_compute_startup_preload_enabled()


def should_schedule_compute_startup_preload() -> bool:
    _sync_compat_symbols()
    return _config_resolvers.should_schedule_compute_startup_preload()


def resolve_compute_startup_preload_environments(api_app=None) -> list[str]:
    _sync_compat_symbols()
    return _config_resolvers.resolve_compute_startup_preload_environments(api_app)


def resolve_compute_startup_preload_intervals(api_app=None) -> list[str]:
    _sync_compat_symbols()
    return _config_resolvers.resolve_compute_startup_preload_intervals(api_app)


def resolve_startup_direct_backfill_enabled(api_app=None, environment: str = "live") -> bool:
    _sync_compat_symbols()
    return _config_resolvers.resolve_startup_direct_backfill_enabled(api_app, environment)


def resolve_startup_direct_backfill_intervals(api_app=None, environment: str = "live") -> list[str]:
    _sync_compat_symbols()
    return _config_resolvers.resolve_startup_direct_backfill_intervals(api_app, environment)


def resolve_startup_direct_backfill_required_bars(api_app=None, environment: str = "live") -> int:
    _sync_compat_symbols()
    return _config_resolvers.resolve_startup_direct_backfill_required_bars(api_app, environment)


def get_compute_startup_preload_state(api_app=None) -> dict:
    _sync_compat_symbols()
    return _scheduler_entrypoints.get_compute_startup_preload_state(api_app)


def run_compute_startup_preload(api_app=None) -> dict:
    _sync_compat_symbols()
    return _scheduler_entrypoints.run_compute_startup_preload(api_app)


def schedule_compute_startup_preload() -> bool:
    _sync_compat_symbols()
    result = _scheduler_entrypoints.schedule_compute_startup_preload()
    globals()["_STARTUP_PRELOAD_THREAD"] = _scheduler_entrypoints._STARTUP_PRELOAD_THREAD
    return result


_FACADE_WRAPPERS = {name: globals()[name] for name in _FACADE_PUBLICS}


__all__ = [
    "get_compute_startup_preload_state",
    "is_compute_startup_preload_enabled",
    "resolve_compute_startup_preload_environments",
    "resolve_compute_startup_preload_intervals",
    "resolve_startup_direct_backfill_enabled",
    "resolve_startup_direct_backfill_intervals",
    "resolve_startup_direct_backfill_required_bars",
    "run_compute_startup_preload",
    "schedule_compute_startup_preload",
    "should_schedule_compute_startup_preload",
]
