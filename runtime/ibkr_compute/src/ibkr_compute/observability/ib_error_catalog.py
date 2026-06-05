from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IBErrorInfo:
    summary_cn: str
    action_cn: str


UNKNOWN_IB_ERROR = IBErrorInfo("未收录IB错误码", "查看IB日志原文")

_IB_ERROR_CATALOG: dict[int, IBErrorInfo] = {
    100: IBErrorInfo("消息发送速率超限", "降低API请求频率"),
    101: IBErrorInfo("行情ticker数量达到上限", "减少订阅或增加行情额度"),
    102: IBErrorInfo("ticker id重复", "检查reqId/tickerId生成"),
    103: IBErrorInfo("order id重复或过旧", "使用nextValidId后的新订单号"),
    107: IBErrorInfo("订单字段不完整", "检查订单必填字段"),
    109: IBErrorInfo("价格超出TWS风控范围", "检查价格和TWS风控设置"),
    110: IBErrorInfo("价格不符合最小tick", "按market rule调整价格精度"),
    162: IBErrorInfo("历史行情服务错误", "查看权限/数据/频控原因"),
    200: IBErrorInfo("合约定义缺失或歧义", "补充交易所/币种/乘数"),
    201: IBErrorInfo("订单被IB拒绝", "查看拒单原文"),
    202: IBErrorInfo("订单被取消", "确认取消来源"),
    326: IBErrorInfo("clientId已被占用", "更换唯一clientId"),
    354: IBErrorInfo("无实时行情权限", "订阅行情或启用延迟行情"),
    355: IBErrorInfo("订单数量不符合规则", "按合约最小数量/步长调整"),
    365: IBErrorInfo("scanner订阅不存在", "检查scanner reqId生命周期"),
    366: IBErrorInfo("历史数据查询不存在", "检查历史数据reqId状态"),
    388: IBErrorInfo("订单数量低于最小要求", "调整订单数量"),
    420: IBErrorInfo("实时查询触发频控", "降低请求频率"),
    501: IBErrorInfo("API已连接", "避免重复连接"),
    502: IBErrorInfo("无法连接TWS/Gateway", "检查socket/端口/防火墙"),
    503: IBErrorInfo("TWS/Gateway版本过旧", "升级TWS或IB Gateway"),
    504: IBErrorInfo("API当前未连接", "先重连再请求"),
    1100: IBErrorInfo("TWS与IB服务器断开", "检查网络/维护窗口"),
    1101: IBErrorInfo("连接恢复但行情丢失", "重新订阅行情"),
    1102: IBErrorInfo("连接恢复且数据保持", "观察即可"),
    1300: IBErrorInfo("API socket端口被重置", "用新端口重连"),
    2100: IBErrorInfo("账户数据订阅被覆盖", "检查账户更新订阅冲突"),
    2103: IBErrorInfo("行情数据农场断开", "关注行情连接"),
    2104: IBErrorInfo("行情数据农场正常", "良性通知"),
    2105: IBErrorInfo("历史数据农场断开", "关注历史数据连接"),
    2106: IBErrorInfo("历史数据农场正常", "良性通知"),
    2107: IBErrorInfo("历史数据农场空闲", "通常可忽略"),
    2108: IBErrorInfo("行情数据农场空闲", "通常可忽略"),
    2110: IBErrorInfo("TWS与IB服务器中断", "等待自动恢复并检查网络"),
    2158: IBErrorInfo("合约定义数据农场正常", "良性通知"),
    2168: IBErrorInfo("EtradeOnly属性不支持", "移除旧订单属性"),
    2169: IBErrorInfo("firmQuoteOnly属性不支持", "移除旧订单属性"),
    2176: IBErrorInfo("API客户端不支持小数股规则", "升级ibapi/Gateway"),
    10089: IBErrorInfo("行情不支持API使用", "检查TWS/API行情权限"),
    10090: IBErrorInfo("部分行情未订阅", "补订阅或减少generic ticks"),
    10186: IBErrorInfo("未订阅行情且未启用延迟", "订阅实时或启用延迟行情"),
    10197: IBErrorInfo("竞争会话导致无行情", "避免多会话争用行情"),
    10242: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10243: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10244: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10245: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10246: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10247: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10248: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10249: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10250: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10251: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10252: IBErrorInfo("小数股订单受限", "检查账户/路由/订单规则"),
    10285: IBErrorInfo("API客户端不支持小数股规则", "升级ibapi/Gateway"),
}


VERSION_RELATED_CODES = {2176, 10285, 503}


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return 0


def describe_ib_error(code: Any) -> IBErrorInfo:
    return _IB_ERROR_CATALOG.get(_to_int(code), UNKNOWN_IB_ERROR)


def is_version_related_ib_error(code: Any) -> bool:
    return _to_int(code) in VERSION_RELATED_CODES


__all__ = [
    "IBErrorInfo",
    "UNKNOWN_IB_ERROR",
    "VERSION_RELATED_CODES",
    "describe_ib_error",
    "is_version_related_ib_error",
]
