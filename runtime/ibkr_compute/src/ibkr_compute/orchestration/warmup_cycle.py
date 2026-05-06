from __future__ import annotations

from .warmup_cycle_support import WarmupCycleSupportMixin, _service_mod
from .warmup_cycle_readiness_model import WarmupCycleReadinessModelMixin
from .warmup_cycle_remote_readiness import WarmupCycleRemoteReadinessMixin
from .warmup_cycle_indicator_backfill import WarmupCycleIndicatorBackfillMixin
from .warmup_cycle_preflight_repair import WarmupCyclePreflightRepairMixin
from .warmup_cycle_cycle_runner import WarmupCycleRunnerMixin


class TradingServiceWarmupCycleMixin(
    WarmupCycleRunnerMixin,
    WarmupCyclePreflightRepairMixin,
    WarmupCycleIndicatorBackfillMixin,
    WarmupCycleRemoteReadinessMixin,
    WarmupCycleReadinessModelMixin,
    WarmupCycleSupportMixin,
):
    pass
