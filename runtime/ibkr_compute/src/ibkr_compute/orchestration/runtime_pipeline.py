from __future__ import annotations

import threading

from .runtime_pipeline_direct_topup import RuntimePipelineDirectTopupMixin
from .runtime_pipeline_official_5m import RuntimePipelineOfficial5mMixin
from .runtime_pipeline_support import RuntimePipelineSupportMixin


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimePipelineMixin(
    RuntimePipelineOfficial5mMixin,
    RuntimePipelineDirectTopupMixin,
    RuntimePipelineSupportMixin,
):
    pass
