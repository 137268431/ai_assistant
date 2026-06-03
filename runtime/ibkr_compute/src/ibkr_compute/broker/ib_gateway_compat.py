from __future__ import annotations

try:
    from ibapi.client import EClient
    from ibapi.commission_report import CommissionReport
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.order import Order
    from ibapi.tag_value import TagValue
    from ibapi.wrapper import EWrapper

    IBAPI_AVAILABLE = True
    IBAPI_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - import availability depends on runtime env
    IBAPI_AVAILABLE = False
    IBAPI_IMPORT_ERROR = str(exc)

    class EWrapper:  # type: ignore[override]
        pass

    class EClient:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

    class Contract:  # type: ignore[override]
        pass

    class Order:  # type: ignore[override]
        pass

    class CommissionReport:  # type: ignore[override]
        pass

    class ExecutionFilter:  # type: ignore[override]
        pass

    class TagValue:  # type: ignore[override]
        def __init__(self, tag: str = "", value: str = ""):
            self.tag = tag
            self.value = value


__all__ = [
    "CommissionReport",
    "Contract",
    "EClient",
    "EWrapper",
    "ExecutionFilter",
    "IBAPI_AVAILABLE",
    "IBAPI_IMPORT_ERROR",
    "Order",
    "TagValue",
]
