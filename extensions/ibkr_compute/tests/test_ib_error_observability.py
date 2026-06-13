from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.observability.ib_error_catalog import describe_ib_error
from ibkr_compute.observability import prometheus


class _FakeCounter:
    def __init__(self) -> None:
        self.labels_args = None
        self.inc_called = False

    def labels(self, *args):
        self.labels_args = args
        return self

    def inc(self) -> None:
        self.inc_called = True


def test_ib_error_catalog_maps_fractional_sizing_codes() -> None:
    assert describe_ib_error(300).summary_cn == "ticker订阅不存在"
    assert describe_ib_error(2176).summary_cn == "API客户端不支持小数股规则"
    assert describe_ib_error(10285).summary_cn == "API客户端不支持小数股规则"
    assert describe_ib_error(2104).action_cn == "良性通知"
    assert describe_ib_error(999999).summary_cn == "未收录IB错误码"


def test_record_broker_error_adds_chinese_low_cardinality_labels(monkeypatch) -> None:
    fake = _FakeCounter()
    monkeypatch.setattr(prometheus, "BROKER_ERRORS", fake)
    monkeypatch.setenv("IBKR_SERVICE_PROFILE", "runtime")

    obj = SimpleNamespace(mode="paper", client_role="runtime")
    prometheus.record_broker_error(obj, ib_error_code=2176, request_kind="market_data", severity="warning")

    assert fake.inc_called is True
    assert fake.labels_args == (
        "ibkr-runtime",
        "paper",
        "runtime",
        "2176",
        "API客户端不支持小数股规则",
        "升级ibapi/Gateway",
        "market_data",
        "warning",
    )
