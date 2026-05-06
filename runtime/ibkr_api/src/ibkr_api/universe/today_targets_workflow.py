from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import first_defined, parse_boolean, to_float, to_int, to_text
from ibkr_api.universe.today_targets_shared import (
    DAILY_SCAN_SUMMARY_TIME_ET,
    DEFAULT_TECHNICAL_STATE,
    INTRADAY_REFRESH_RULE,
    MARKET_OPEN_CHECK_TIME_ET,
    format_et_datetime,
    normalize_signal_status,
    push_unique_text,
)
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
)

READY_REQUIRED_ALIGNED_FLAGS = 2
READY_LONG_FLAGS = ("EMA多头", "多头背离", "分形↑", "EMA支撑", "趋势多头", "VWAP多头")
READY_SHORT_FLAGS = ("EMA空头", "空头背离", "分形↓", "EMA压力", "趋势空头", "VWAP空头")


def _format_count(value: Any) -> str:
    try:
        return f"{int(float(value or 0)):,}"
    except Exception:
        return "0"


def _format_number(value: Any, digits: int = 0) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "0"
    if digits <= 0:
        return str(int(round(parsed)))
    return f"{parsed:.{digits}f}".rstrip("0").rstrip(".")


def build_technical_flags(indicator_extra: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if not isinstance(indicator_extra, dict):
        return flags
    if indicator_extra.get("ema_bullish"):
        push_unique_text(flags, "EMA多头")
    if indicator_extra.get("ema_bearish"):
        push_unique_text(flags, "EMA空头")
    if indicator_extra.get("crsi_bull_div") or indicator_extra.get("obv_bull_div"):
        push_unique_text(flags, "多头背离")
    if indicator_extra.get("crsi_bear_div") or indicator_extra.get("obv_bear_div"):
        push_unique_text(flags, "空头背离")
    if indicator_extra.get("fractal_bull"):
        push_unique_text(flags, "分形↑")
    if indicator_extra.get("fractal_bear"):
        push_unique_text(flags, "分形↓")
    if indicator_extra.get("ema_bull_touch"):
        push_unique_text(flags, "EMA支撑")
    if indicator_extra.get("ema_bear_touch"):
        push_unique_text(flags, "EMA压力")
    if to_int(indicator_extra.get("trend_dir"), 0) == 1:
        push_unique_text(flags, "趋势多头")
    if to_int(indicator_extra.get("trend_dir"), 0) == -1:
        push_unique_text(flags, "趋势空头")
    if indicator_extra.get("vwap_bullish") is True:
        push_unique_text(flags, "VWAP多头")
    if indicator_extra.get("vwap_bullish") is False:
        push_unique_text(flags, "VWAP空头")
    return flags


def build_aligned_technical_flags(indicator_extra: dict[str, Any], direction_bias: str) -> list[str]:
    direction = to_text(direction_bias).lower()
    flags: list[str] = []
    if not isinstance(indicator_extra, dict):
        return flags
    if direction == "long":
        if indicator_extra.get("ema_bullish"):
            push_unique_text(flags, "EMA多头")
        if indicator_extra.get("crsi_bull_div") or indicator_extra.get("obv_bull_div"):
            push_unique_text(flags, "多头背离")
        if indicator_extra.get("fractal_bull"):
            push_unique_text(flags, "分形↑")
        if indicator_extra.get("ema_bull_touch"):
            push_unique_text(flags, "EMA支撑")
        if to_int(indicator_extra.get("trend_dir"), 0) == 1:
            push_unique_text(flags, "趋势多头")
        if indicator_extra.get("vwap_bullish") is True:
            push_unique_text(flags, "VWAP多头")
        return flags
    if direction == "short":
        if indicator_extra.get("ema_bearish"):
            push_unique_text(flags, "EMA空头")
        if indicator_extra.get("crsi_bear_div") or indicator_extra.get("obv_bear_div"):
            push_unique_text(flags, "空头背离")
        if indicator_extra.get("fractal_bear"):
            push_unique_text(flags, "分形↓")
        if indicator_extra.get("ema_bear_touch"):
            push_unique_text(flags, "EMA压力")
        if to_int(indicator_extra.get("trend_dir"), 0) == -1:
            push_unique_text(flags, "趋势空头")
        if indicator_extra.get("vwap_bullish") is False:
            push_unique_text(flags, "VWAP空头")
    return flags


def resolve_technical_state(row: dict[str, Any], aligned_flags: list[str]) -> str:
    freshness_min = row.get("freshness_min") if isinstance(row.get("freshness_min"), int) else None
    if not row.get("has_live_bar") or freshness_min is None or freshness_min > TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN:
        return "stale"
    if row.get("is_operable") and len(aligned_flags or []) >= READY_REQUIRED_ALIGNED_FLAGS:
        return "ready"
    return DEFAULT_TECHNICAL_STATE


def build_ready_definition() -> dict[str, Any]:
    return {
        "title": "READY 判定",
        "summary": (
            "READY = 可操作条件通过 + 方向一致技术条件达到阈值；"
            "它表示标的进入等待信号触发阶段，不代表已经出信号、下单或成交。"
        ),
        "thresholds": {
            "has_live_bar": True,
            "price_gt": 0,
            "avg_10d_volume_gte": TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
            "tradability_score_gte": TRADABILITY_OPERABLE_MIN_SCORE,
            "freshness_lte_min": TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
        },
        "required_aligned_flags": READY_REQUIRED_ALIGNED_FLAGS,
        "long_flags": list(READY_LONG_FLAGS),
        "short_flags": list(READY_SHORT_FLAGS),
        "not_signal_or_execution": True,
    }


def build_ready_explanation(row: dict[str, Any]) -> dict[str, Any]:
    passed: list[str] = []
    missing: list[str] = []
    aligned_flags = row.get("technical_aligned_flags") if isinstance(row.get("technical_aligned_flags"), list) else []
    freshness_min = row.get("freshness_min") if isinstance(row.get("freshness_min"), int) else None
    price = to_float(row.get("price")) or 0.0
    avg_10d_volume = to_float(row.get("avg_10d_volume")) or 0.0
    tradability_score = to_float(row.get("tradability_score")) or 0.0

    if row.get("has_live_bar"):
        push_unique_text(passed, "当日 5m bar")
        if freshness_min is not None and freshness_min <= TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN:
            push_unique_text(passed, f"freshness {freshness_min}m <= {TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN}m")
        else:
            label = "freshness 未就绪" if freshness_min is None else f"freshness {freshness_min}m > {TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN}m"
            push_unique_text(missing, label)
    else:
        push_unique_text(missing, "缺少当日 5m bar")

    if price > 0:
        push_unique_text(passed, "价格已就绪")
    else:
        push_unique_text(missing, "价格未就绪")

    if avg_10d_volume >= TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME:
        push_unique_text(passed, f"10D均量 {_format_count(avg_10d_volume)} >= {_format_count(TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME)}")
    else:
        push_unique_text(missing, f"10D均量 {_format_count(avg_10d_volume)} < {_format_count(TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME)}")

    if tradability_score >= TRADABILITY_OPERABLE_MIN_SCORE:
        push_unique_text(passed, f"tradability {_format_number(tradability_score)} >= {TRADABILITY_OPERABLE_MIN_SCORE}")
    else:
        push_unique_text(missing, f"tradability {_format_number(tradability_score)} < {TRADABILITY_OPERABLE_MIN_SCORE}")

    aligned_count = len(aligned_flags)
    aligned_label = f"方向一致技术条件 {aligned_count}/{READY_REQUIRED_ALIGNED_FLAGS}"
    if aligned_count >= READY_REQUIRED_ALIGNED_FLAGS:
        push_unique_text(passed, aligned_label)
    else:
        push_unique_text(missing, aligned_label)

    ready = to_text(row.get("technical_state")).lower() == "ready"
    summary = (
        "已满足 READY 判定；等待信号触发，不代表已下单或成交。"
        if ready
        else "尚未达到 READY；先处理缺失条件，再等待 5m close 刷新。"
    )
    return {
        "ready": ready,
        "passed": passed,
        "missing": missing,
        "aligned_flags": [to_text(item) for item in aligned_flags if to_text(item)],
        "summary": summary,
    }


def resolve_attention_state(row: dict[str, Any]) -> tuple[str, int]:
    signal_status = normalize_signal_status(row.get("latest_signal_status"))
    if signal_status == "awaiting_confirm":
        return "awaiting_confirm", 10
    if signal_status == "pending":
        return "pending", 11
    if not row.get("has_signal_today") and row.get("technical_state") == "ready":
        return "ready_no_signal", 20
    if signal_status == "executed":
        return "executed", 30
    if signal_status == "closed":
        return "closed", 31
    if signal_status == "expired":
        return "expired", 32
    if signal_status == "rejected":
        return "rejected", 33
    if row.get("technical_state") == "stale":
        return "stale", 40
    return DEFAULT_TECHNICAL_STATE, 25


def build_primary_view_url(runtime_environment: str, market_date: str) -> str:
    return (
        "/ibkr_screener.html"
        f"?tab=screener&view=current&date={market_date}&market_date={market_date}&environment={runtime_environment}"
    )


def build_workflow_guide(runtime_environment: str, market_date: str) -> dict[str, Any]:
    return {
        "scan_summary_time_et": DAILY_SCAN_SUMMARY_TIME_ET,
        "open_check_time_et": MARKET_OPEN_CHECK_TIME_ET,
        "intraday_refresh_rule": INTRADAY_REFRESH_RULE,
        "focus_order_rule": "先看 awaiting_confirm / pending，再看 ready 未出信号，最后看 executed / stale。",
        "primary_view_url": build_primary_view_url(runtime_environment, market_date),
        "ready_definition": build_ready_definition(),
    }


def build_base_workflow_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    freshness_min = row.get("freshness_min") if isinstance(row.get("freshness_min"), int) else None
    aligned_flags = row.get("technical_aligned_flags") if isinstance(row.get("technical_aligned_flags"), list) else []
    if not row.get("has_live_bar"):
        push_unique_text(blockers, "缺少当日 5m bars")
    elif freshness_min is not None and freshness_min > TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN:
        push_unique_text(blockers, f"bars 延迟 {round(freshness_min)}m")
    if to_float(row.get("price")) in (None, 0.0):
        push_unique_text(blockers, "价格未就绪")
    if (to_float(row.get("avg_10d_volume")) or 0.0) < TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME:
        push_unique_text(blockers, "10日均量不足 50 万")
    if not row.get("is_operable") and (to_float(row.get("tradability_score")) or 0.0) < TRADABILITY_OPERABLE_MIN_SCORE:
        push_unique_text(blockers, "可操作分不足")
    if len(aligned_flags) < 2:
        push_unique_text(blockers, "方向一致技术条件未集齐")
    return blockers


def build_workflow_meta(row: dict[str, Any]) -> dict[str, Any]:
    signal_status = normalize_signal_status(row.get("latest_signal_status"))
    blockers: list[str] = []
    stage = "watch"
    label = "观察中"
    summary = "该标的仍在今日目标池内，但技术或信号条件还没进入优先执行阶段。"
    next_action = "先看技术 flags、量能与 freshness，满足后继续等 5m close 刷新。"
    if signal_status == "awaiting_confirm":
        stage = "awaiting_confirm"
        label = "信号待确认"
        summary = (
            f"{row.get('latest_signal_time')} 已产生 {to_text(first_defined(row.get('latest_signal_direction'), row.get('direction_bias'))).upper()} 信号，当前等待确认完成。"
            if row.get("latest_signal_time")
            else "今日已产生信号，当前等待确认完成。"
        )
        push_unique_text(blockers, "等待信号确认完成")
        push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "优先查看 Signals 页，确认 signal 状态、有效期和后续订单动作。"
    elif signal_status == "pending":
        stage = "pending"
        label = "信号待执行"
        summary = (
            f"{row.get('latest_signal_time')} 信号已进入 pending，等待执行链路推进。"
            if row.get("latest_signal_time")
            else "今日信号已进入 pending，等待执行链路推进。"
        )
        push_unique_text(blockers, "等待下单或成交反馈")
        push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "优先查看 Signals / Orders，确认挂单、成交和风控状态。"
    elif not row.get("has_signal_today") and row.get("technical_state") == "ready":
        stage = "ready_no_signal"
        label = "技术已就绪"
        summary = "技术条件和可操作性已基本满足，但今日还没有触发信号。"
        push_unique_text(blockers, "等待下一次 5m close 触发信号")
        next_action = "盘中按 5m close 继续观察，重点联动当前榜单和 Signals 页。"
    elif row.get("technical_state") == "stale":
        stage = "stale"
        label = "数据待刷新"
        summary = "该标的缺少足够新鲜的盘中 bars，当前技术判断不可靠。"
        for blocker in build_base_workflow_blockers(row):
            push_unique_text(blockers, blocker)
        next_action = "先检查 bars / indicators 是否刷新，再决定是否继续跟踪。"
    elif signal_status == "executed":
        stage = "executed"
        label = "已执行"
        summary = "今日信号已执行，后续重点转向持仓、退出和保护单管理。"
        next_action = "去 Signals / Orders 跟踪持仓、止盈止损和退出状态。"
    elif signal_status == "closed":
        stage = "closed"
        label = "已闭环"
        summary = "今日信号已结束闭环，当前不再是优先执行对象。"
        next_action = "保留复盘结论；若再次入榜，再重新进入关注。"
    elif signal_status == "expired":
        stage = "expired"
        label = "信号过期"
        summary = "今日信号已过期，当前执行窗口已经结束。"
        next_action = "等待新的 5m close 或次日重新筛选。"
    elif signal_status == "rejected":
        stage = "rejected"
        label = "信号已拒绝"
        summary = "今日信号已被拒绝或取消，不再继续推进执行。"
        push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "查看信号备注与风控原因，确认是否继续观察。"
    else:
        for blocker in build_base_workflow_blockers(row):
            push_unique_text(blockers, blocker)
    if not blockers and stage in {"watch", "ready_no_signal"}:
        push_unique_text(blockers, "等待下一次 5m close 刷新")
    return {
        "stage": stage,
        "label": label,
        "summary": summary,
        "blockers": blockers,
        "next_action": next_action,
    }


def ensure_row_details(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("ready_explanation"):
        row["ready_explanation"] = build_ready_explanation(row)
    if row.get("workflow_summary"):
        return row
    workflow = build_workflow_meta(row)
    row["workflow_stage"] = workflow["stage"]
    row["workflow_label"] = workflow["label"]
    row["workflow_summary"] = workflow["summary"]
    row["workflow_blockers"] = workflow["blockers"]
    row["workflow_next_action"] = workflow["next_action"]
    return row


def normalize_filters(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "search": to_text(first_defined(options.get("search"), options.get("q"))).upper(),
        "technical_state": to_text(first_defined(options.get("technical_state"), options.get("technicalState"))).lower(),
        "signal_state": to_text(first_defined(options.get("signal_state"), options.get("signalState"))).lower(),
        "target_status": to_text(first_defined(options.get("target_status"), options.get("targetStatus"))).lower(),
        "direction_bias": to_text(first_defined(options.get("direction_bias"), options.get("directionBias"))).lower(),
        "ready_only": parse_boolean(first_defined(options.get("ready_only"), options.get("readyOnly")), False),
        "signaled_only": parse_boolean(first_defined(options.get("signaled_only"), options.get("signaledOnly")), False),
        "sort_by": to_text(first_defined(options.get("sort_by"), options.get("sortBy"))).lower() or "attention_asc",
    }


def matches_signal_state(row: dict[str, Any], signal_state: str) -> bool:
    value = to_text(signal_state).lower()
    if not value:
        return True
    latest_status = normalize_signal_status(row.get("latest_signal_status"))
    if value == "needs_action":
        return latest_status in {"awaiting_confirm", "pending"}
    if value == "signaled":
        return bool(row.get("has_signal_today"))
    if value == "no_signal":
        return not bool(row.get("has_signal_today"))
    return latest_status == value


def matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    normalized = filters or normalize_filters({})
    search = to_text(normalized.get("search")).upper()
    if search:
        ensure_row_details(row)
        haystack = " ".join(
            [
                to_text(row.get("symbol")),
                to_text(row.get("exchange")),
                to_text(row.get("industry")),
                to_text(row.get("scan_reason")),
                to_text(row.get("note")),
                to_text(row.get("latest_signal_id")),
                to_text(row.get("latest_signal_status")),
                to_text(row.get("workflow_label")),
                to_text(row.get("workflow_summary")),
                to_text(row.get("workflow_next_action")),
            ]
            + [to_text(item) for item in (row.get("technical_flags") or [])]
            + [to_text(item) for item in (row.get("operable_reasons") or [])]
            + [to_text(item) for item in (row.get("workflow_blockers") or [])]
        ).upper()
        if search not in haystack:
            return False
    if normalized.get("technical_state") and to_text(row.get("technical_state")).lower() != normalized.get("technical_state"):
        return False
    if normalized.get("target_status") and to_text(row.get("target_status")).lower() != normalized.get("target_status"):
        return False
    if normalized.get("direction_bias") and to_text(row.get("direction_bias")).lower() != normalized.get("direction_bias"):
        return False
    if not matches_signal_state(row, to_text(normalized.get("signal_state"))):
        return False
    if normalized.get("ready_only") and to_text(row.get("technical_state")).lower() != "ready":
        return False
    if normalized.get("signaled_only") and not row.get("has_signal_today"):
        return False
    return True


def sort_rows(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    normalized_sort = to_text(sort_by).lower() or "attention_asc"
    if normalized_sort == "symbol_asc":
        return sorted(rows, key=lambda row: to_text(row.get("symbol")))
    if normalized_sort == "signal_desc":
        return sorted(
            rows,
            key=lambda row: (
                -to_int(row.get("latest_signal_time_ms"), 0),
                to_int(row.get("attention_rank"), 99),
            ),
        )
    if normalized_sort == "tradability_desc":
        return sorted(
            rows,
            key=lambda row: (
                -(to_float(row.get("tradability_score")) or 0.0),
                -(to_float(row.get("target_score")) or 0.0),
                to_text(row.get("symbol")),
            ),
        )
    if normalized_sort == "target_desc":
        return sorted(
            rows,
            key=lambda row: (
                -(to_float(row.get("target_score")) or 0.0),
                -(to_float(row.get("tradability_score")) or 0.0),
                to_text(row.get("symbol")),
            ),
        )
    return sorted(
        rows,
        key=lambda row: (
            to_int(row.get("attention_rank"), 99),
            -to_int(row.get("latest_signal_time_ms"), 0),
            -(to_float(row.get("tradability_score")) or 0.0),
            -(to_float(row.get("target_score")) or 0.0),
            to_text(row.get("symbol")),
        ),
    )


def build_filtered_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    ready_count = 0
    signaled_count = 0
    needs_action_count = 0
    for row in rows:
        if to_text(row.get("technical_state")).lower() == "ready":
            ready_count += 1
        if row.get("has_signal_today"):
            signaled_count += 1
        if normalize_signal_status(row.get("latest_signal_status")) in {"awaiting_confirm", "pending"}:
            needs_action_count += 1
    return {
        "total": len(rows),
        "ready_count": ready_count,
        "signaled_count": signaled_count,
        "needs_action_count": needs_action_count,
    }


__all__ = [
    "build_aligned_technical_flags",
    "build_filtered_summary",
    "build_ready_definition",
    "build_ready_explanation",
    "build_technical_flags",
    "build_workflow_guide",
    "ensure_row_details",
    "matches_filters",
    "normalize_filters",
    "resolve_attention_state",
    "resolve_technical_state",
    "sort_rows",
]
