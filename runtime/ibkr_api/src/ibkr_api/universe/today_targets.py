from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, first_defined, parse_boolean, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
    build_tradability_assessment,
)
from ibkr_compute.market.timeframe_utils import ET, classify_session, format_cn_time, format_us_time, interval_to_chart_tf, ms_to_et


LIVE_ENVIRONMENT = "live"
WATCHLIST_ROLE_TRADE = "trade"
DEFAULT_TECHNICAL_STATE = "watch"
DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
DAILY_SCAN_STATE_DATE = "global"
DAILY_SCAN_SUMMARY_TIME_ET = "09:20"
MARKET_OPEN_CHECK_TIME_ET = "09:20"
INTRADAY_REFRESH_RULE = "5m close-driven"
TODAY_TARGET_STATUSES = {"active", "candidate"}

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]


def _normalize_symbols(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        symbol = to_text(raw_item).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def _escape_filter(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _format_et_date(ms: int) -> str:
    return ms_to_et(ms).strftime("%Y-%m-%d") if int(ms or 0) > 0 else ""


def _format_et_datetime(ms: int) -> str:
    return format_us_time(int(ms or 0)) if int(ms or 0) > 0 else ""


def _parse_et_datetime_ms(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _current_market_date(time_strings: TimeStrings) -> str:
    return to_text((time_strings() or {}).get("date"))


def _get_priority_environment_rank(environment: Any, runtime_environment: str) -> int:
    normalized = to_text(environment).lower()
    if normalized == runtime_environment:
        return 2
    if normalized == "global":
        return 1
    if not normalized:
        return 0
    return -1


def _build_symbol_filter(symbols: list[str]) -> str:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return ""
    return "(" + " || ".join(f'symbol = "{_escape_filter(symbol)}"' for symbol in normalized) + ")"


def _build_bar_environment_filter(runtime_environment: str) -> str:
    clauses = [f'environment = "{_escape_filter(runtime_environment)}"']
    if runtime_environment == LIVE_ENVIRONMENT:
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})"


def _normalize_watchlist_role(value: Any) -> str:
    return "market_monitor" if to_text(value).lower() == "market_monitor" else WATCHLIST_ROLE_TRADE


def _load_watch_meta(pb: Any, environment: str, symbols: list[str]) -> dict[str, dict[str, str]]:
    normalized_symbols = set(_normalize_symbols(symbols))
    if not normalized_symbols:
        return {}
    rows = pb.get_records(
        "watchlist",
        filter=f'environment = "{_escape_filter(environment)}" || environment = "global" || environment = ""',
        sort="-updated",
        per_page=500,
        page=1,
    )
    meta: dict[str, dict[str, str]] = {}
    ranks: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        if symbol not in normalized_symbols:
            continue
        if _normalize_watchlist_role(row.get("symbol_role")) != WATCHLIST_ROLE_TRADE:
            continue
        rank = _get_priority_environment_rank(row.get("environment"), environment)
        if rank < 0:
            continue
        if symbol in ranks and ranks[symbol] > rank:
            continue
        ranks[symbol] = rank
        meta[symbol] = {
            "exchange": to_text(row.get("exchange")).upper(),
            "industry": to_text(row.get("industry")),
            "note": to_text(row.get("note")),
        }
    return meta


def _load_daily_scan_state(pb: Any, environment: str) -> dict[str, Any]:
    try:
        record = pb.get_state(DAILY_SCAN_STATE_KEY, environment, date=DAILY_SCAN_STATE_DATE)
    except Exception:
        record = None
    if not isinstance(record, dict):
        return {}
    payload = ensure_object(record.get("data"))
    payload["result"] = parse_json_object(payload.get("result")) if not isinstance(payload.get("result"), dict) else dict(payload.get("result"))
    return payload


def _load_records_for_symbols(
    pb: Any,
    collection: str,
    *,
    base_filter_parts: list[str],
    symbols: list[str],
    sort: str,
    max_pages: int,
    chunk_size: int = 24,
) -> list[dict[str, Any]]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return []
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(normalized_symbols), max(1, int(chunk_size or 1))):
        chunk = normalized_symbols[offset:offset + max(1, int(chunk_size or 1))]
        filter_parts = list(base_filter_parts)
        symbol_filter = _build_symbol_filter(chunk)
        if symbol_filter:
            filter_parts.append(symbol_filter)
        rows.extend(
            dict(row)
            for row in (pb.get_all_records(collection, filter=" && ".join(filter_parts), sort=sort, max_pages=max_pages) or [])
            if isinstance(row, dict)
        )
    return rows


def _indicator_snapshot(record: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(record or {})
    extra = parse_json_object(row.get("extra"))
    snapshot = dict(extra)
    for field in (
        "atr_pct",
        "ema_bullish",
        "ema_bearish",
        "crsi_bull_div",
        "crsi_bear_div",
        "obv_bull_div",
        "obv_bear_div",
        "fractal_bull",
        "fractal_bear",
        "ema_bull_touch",
        "ema_bear_touch",
        "trend_dir",
        "vwap_bullish",
    ):
        value = row.get(field)
        if value not in (None, ""):
            snapshot[field] = value
    return snapshot


def _build_daily_change_fields(history: list[dict[str, Any]], current_close: float) -> dict[str, float]:
    prev_close = to_float(history[-1].get("close")) if len(history) >= 1 else 0.0
    prev_prev_close = to_float(history[-2].get("close")) if len(history) >= 2 else 0.0
    close_5 = to_float(history[-5].get("close")) if len(history) >= 5 else 0.0
    day_change_pct = ((current_close - prev_close) / prev_close) * 100 if prev_close and prev_close > 0 else 0.0
    prev_close_change_pct = ((prev_close - prev_prev_close) / prev_prev_close) * 100 if prev_prev_close and prev_prev_close > 0 else 0.0
    change_7d = ((current_close - close_5) / close_5) * 100 if close_5 and close_5 > 0 else 0.0
    return {
        "day_change_pct": round(day_change_pct, 2),
        "prev_close_change_pct": round(prev_close_change_pct, 2),
        "change_7d": round(change_7d, 2),
    }


def _push_unique_text(items: list[str], value: Any) -> None:
    text = to_text(value)
    if text and text not in items:
        items.append(text)


def _pick_reason_list(current_reasons: list[str] | None, fallback_reasons: list[str] | None) -> list[str]:
    merged: list[str] = []
    for source in (current_reasons or [], fallback_reasons or []):
        for item in source:
            _push_unique_text(merged, item)
    return merged


def _normalize_signal_status(value: Any) -> str:
    return to_text(value).lower()


def _normalize_signal_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = parse_json_object(record.get("extra"))
    bar_time_ms = to_int(first_defined(record.get("bar_time_ms"), extra.get("bar_time_ms")), 0)
    created = to_text(record.get("created"))
    updated = to_text(record.get("updated")) or created
    created_ms = _parse_et_datetime_ms(created)
    updated_ms = _parse_et_datetime_ms(updated) or created_ms
    return {
        "symbol": to_text(record.get("symbol")).upper(),
        "signal_id": to_text(first_defined(record.get("signal_id"), extra.get("signal_id"))),
        "direction": to_text(first_defined(record.get("direction"), extra.get("direction"))).lower(),
        "signal": to_text(first_defined(record.get("signal"), extra.get("signal"))),
        "status": _normalize_signal_status(first_defined(record.get("status"), extra.get("status"))),
        "bar_time_ms": bar_time_ms,
        "created": created,
        "updated": updated,
        "created_ms": created_ms,
        "updated_ms": updated_ms,
        "sort_ms": bar_time_ms or updated_ms or created_ms,
        "us_time": to_text(record.get("us_time")) or (_format_et_datetime(bar_time_ms) if bar_time_ms > 0 else (updated or created)),
        "note": to_text(record.get("note")) or to_text(first_defined(extra.get("note"), extra.get("status_reason"))),
    }


def _pick_latest_signal(current_signal: dict[str, Any] | None, next_signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not next_signal:
        return current_signal
    if not current_signal:
        return next_signal
    if to_int(next_signal.get("sort_ms"), 0) != to_int(current_signal.get("sort_ms"), 0):
        return next_signal if to_int(next_signal.get("sort_ms"), 0) > to_int(current_signal.get("sort_ms"), 0) else current_signal
    if to_int(next_signal.get("updated_ms"), 0) != to_int(current_signal.get("updated_ms"), 0):
        return next_signal if to_int(next_signal.get("updated_ms"), 0) > to_int(current_signal.get("updated_ms"), 0) else current_signal
    return next_signal


def _build_technical_flags(indicator_extra: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if not isinstance(indicator_extra, dict):
        return flags
    if indicator_extra.get("ema_bullish"):
        _push_unique_text(flags, "EMA多头")
    if indicator_extra.get("ema_bearish"):
        _push_unique_text(flags, "EMA空头")
    if indicator_extra.get("crsi_bull_div") or indicator_extra.get("obv_bull_div"):
        _push_unique_text(flags, "多头背离")
    if indicator_extra.get("crsi_bear_div") or indicator_extra.get("obv_bear_div"):
        _push_unique_text(flags, "空头背离")
    if indicator_extra.get("fractal_bull"):
        _push_unique_text(flags, "分形↑")
    if indicator_extra.get("fractal_bear"):
        _push_unique_text(flags, "分形↓")
    if indicator_extra.get("ema_bull_touch"):
        _push_unique_text(flags, "EMA支撑")
    if indicator_extra.get("ema_bear_touch"):
        _push_unique_text(flags, "EMA压力")
    if to_int(indicator_extra.get("trend_dir"), 0) == 1:
        _push_unique_text(flags, "趋势多头")
    if to_int(indicator_extra.get("trend_dir"), 0) == -1:
        _push_unique_text(flags, "趋势空头")
    if indicator_extra.get("vwap_bullish") is True:
        _push_unique_text(flags, "VWAP多头")
    if indicator_extra.get("vwap_bullish") is False:
        _push_unique_text(flags, "VWAP空头")
    return flags


def _build_aligned_technical_flags(indicator_extra: dict[str, Any], direction_bias: str) -> list[str]:
    direction = to_text(direction_bias).lower()
    flags: list[str] = []
    if not isinstance(indicator_extra, dict):
        return flags
    if direction == "long":
        if indicator_extra.get("ema_bullish"):
            _push_unique_text(flags, "EMA多头")
        if indicator_extra.get("crsi_bull_div") or indicator_extra.get("obv_bull_div"):
            _push_unique_text(flags, "多头背离")
        if indicator_extra.get("fractal_bull"):
            _push_unique_text(flags, "分形↑")
        if indicator_extra.get("ema_bull_touch"):
            _push_unique_text(flags, "EMA支撑")
        if to_int(indicator_extra.get("trend_dir"), 0) == 1:
            _push_unique_text(flags, "趋势多头")
        if indicator_extra.get("vwap_bullish") is True:
            _push_unique_text(flags, "VWAP多头")
        return flags
    if direction == "short":
        if indicator_extra.get("ema_bearish"):
            _push_unique_text(flags, "EMA空头")
        if indicator_extra.get("crsi_bear_div") or indicator_extra.get("obv_bear_div"):
            _push_unique_text(flags, "空头背离")
        if indicator_extra.get("fractal_bear"):
            _push_unique_text(flags, "分形↓")
        if indicator_extra.get("ema_bear_touch"):
            _push_unique_text(flags, "EMA压力")
        if to_int(indicator_extra.get("trend_dir"), 0) == -1:
            _push_unique_text(flags, "趋势空头")
        if indicator_extra.get("vwap_bullish") is False:
            _push_unique_text(flags, "VWAP空头")
    return flags


def _resolve_technical_state(row: dict[str, Any], aligned_flags: list[str]) -> str:
    freshness_min = row.get("freshness_min") if isinstance(row.get("freshness_min"), int) else None
    if not row.get("has_live_bar") or freshness_min is None or freshness_min > TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN:
        return "stale"
    if row.get("is_operable") and len(aligned_flags or []) >= 2:
        return "ready"
    return DEFAULT_TECHNICAL_STATE


def _resolve_attention_state(row: dict[str, Any]) -> tuple[str, int]:
    signal_status = _normalize_signal_status(row.get("latest_signal_status"))
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


def _build_primary_view_url(runtime_environment: str, market_date: str) -> str:
    return (
        "/ibkr_screener.html"
        f"?tab=screener&view=current&date={market_date}&market_date={market_date}&environment={runtime_environment}"
    )


def _build_workflow_guide(runtime_environment: str, market_date: str) -> dict[str, str]:
    return {
        "scan_summary_time_et": DAILY_SCAN_SUMMARY_TIME_ET,
        "open_check_time_et": MARKET_OPEN_CHECK_TIME_ET,
        "intraday_refresh_rule": INTRADAY_REFRESH_RULE,
        "focus_order_rule": "先看 awaiting_confirm / pending，再看 ready 未出信号，最后看 executed / stale。",
        "primary_view_url": _build_primary_view_url(runtime_environment, market_date),
    }


def _build_base_workflow_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    freshness_min = row.get("freshness_min") if isinstance(row.get("freshness_min"), int) else None
    aligned_flags = row.get("technical_aligned_flags") if isinstance(row.get("technical_aligned_flags"), list) else []
    if not row.get("has_live_bar"):
        _push_unique_text(blockers, "缺少当日 5m bars")
    elif freshness_min is not None and freshness_min > TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN:
        _push_unique_text(blockers, f"bars 延迟 {round(freshness_min)}m")
    if to_float(row.get("price")) in (None, 0.0):
        _push_unique_text(blockers, "价格未就绪")
    if (to_float(row.get("avg_10d_volume")) or 0.0) < TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME:
        _push_unique_text(blockers, "10日均量不足 50 万")
    if not row.get("is_operable") and (to_float(row.get("tradability_score")) or 0.0) < TRADABILITY_OPERABLE_MIN_SCORE:
        _push_unique_text(blockers, "可操作分不足")
    if len(aligned_flags) < 2:
        _push_unique_text(blockers, "方向一致技术条件未集齐")
    return blockers


def _build_workflow_meta(row: dict[str, Any]) -> dict[str, Any]:
    signal_status = _normalize_signal_status(row.get("latest_signal_status"))
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
        _push_unique_text(blockers, "等待信号确认完成")
        _push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "优先查看 Signals 页，确认 signal 状态、有效期和后续订单动作。"
    elif signal_status == "pending":
        stage = "pending"
        label = "信号待执行"
        summary = (
            f"{row.get('latest_signal_time')} 信号已进入 pending，等待执行链路推进。"
            if row.get("latest_signal_time")
            else "今日信号已进入 pending，等待执行链路推进。"
        )
        _push_unique_text(blockers, "等待下单或成交反馈")
        _push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "优先查看 Signals / Orders，确认挂单、成交和风控状态。"
    elif not row.get("has_signal_today") and row.get("technical_state") == "ready":
        stage = "ready_no_signal"
        label = "技术已就绪"
        summary = "技术条件和可操作性已基本满足，但今日还没有触发信号。"
        _push_unique_text(blockers, "等待下一次 5m close 触发信号")
        next_action = "盘中按 5m close 继续观察，重点联动当前榜单和 Signals 页。"
    elif row.get("technical_state") == "stale":
        stage = "stale"
        label = "数据待刷新"
        summary = "该标的缺少足够新鲜的盘中 bars，当前技术判断不可靠。"
        for blocker in _build_base_workflow_blockers(row):
            _push_unique_text(blockers, blocker)
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
        _push_unique_text(blockers, row.get("latest_signal_note"))
        next_action = "查看信号备注与风控原因，确认是否继续观察。"
    else:
        for blocker in _build_base_workflow_blockers(row):
            _push_unique_text(blockers, blocker)
    if not blockers and stage in {"watch", "ready_no_signal"}:
        _push_unique_text(blockers, "等待下一次 5m close 刷新")
    return {
        "stage": stage,
        "label": label,
        "summary": summary,
        "blockers": blockers,
        "next_action": next_action,
    }


def _ensure_row_details(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("workflow_summary"):
        return row
    workflow = _build_workflow_meta(row)
    row["workflow_stage"] = workflow["stage"]
    row["workflow_label"] = workflow["label"]
    row["workflow_summary"] = workflow["summary"]
    row["workflow_blockers"] = workflow["blockers"]
    row["workflow_next_action"] = workflow["next_action"]
    return row


def _normalize_filters(options: dict[str, Any]) -> dict[str, Any]:
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


def _matches_signal_state(row: dict[str, Any], signal_state: str) -> bool:
    value = to_text(signal_state).lower()
    if not value:
        return True
    latest_status = _normalize_signal_status(row.get("latest_signal_status"))
    if value == "needs_action":
        return latest_status in {"awaiting_confirm", "pending"}
    if value == "signaled":
        return bool(row.get("has_signal_today"))
    if value == "no_signal":
        return not bool(row.get("has_signal_today"))
    return latest_status == value


def _matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    normalized = filters or _normalize_filters({})
    search = to_text(normalized.get("search")).upper()
    if search:
        _ensure_row_details(row)
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
    if not _matches_signal_state(row, to_text(normalized.get("signal_state"))):
        return False
    if normalized.get("ready_only") and to_text(row.get("technical_state")).lower() != "ready":
        return False
    if normalized.get("signaled_only") and not row.get("has_signal_today"):
        return False
    return True


def _sort_rows(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
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


def _build_filtered_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    ready_count = 0
    signaled_count = 0
    needs_action_count = 0
    for row in rows:
        if to_text(row.get("technical_state")).lower() == "ready":
            ready_count += 1
        if row.get("has_signal_today"):
            signaled_count += 1
        if _normalize_signal_status(row.get("latest_signal_status")) in {"awaiting_confirm", "pending"}:
            needs_action_count += 1
    return {
        "total": len(rows),
        "ready_count": ready_count,
        "signaled_count": signaled_count,
        "needs_action_count": needs_action_count,
    }


def build_today_targets_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    runtime_environment = normalize_environment(payload.get("environment"), LIVE_ENVIRONMENT)
    current_market_date = _current_market_date(time_strings)
    requested_market_date = to_text(first_defined(payload.get("marketDate"), payload.get("market_date"), payload.get("date"))) or current_market_date
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(requested_market_date)
        market_date = requested_market_date
    except Exception:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(current_market_date)
        market_date = current_market_date
    filters = _normalize_filters(payload)
    workflow_guide = _build_workflow_guide(runtime_environment, market_date)
    paginate = bool(payload.get("paginate"))
    requested_per_page = max(1, min(200, to_int(first_defined(payload.get("per_page"), payload.get("perPage")), 10)))
    requested_page = max(1, to_int(payload.get("page"), 1)) if paginate else 1
    computed_at_ms = int(time.time() * 1000)
    daily_scan = _load_daily_scan_state(pb, runtime_environment)

    target_rows = pb.get_records(
        "ibkr_targets",
        filter=(
            f'environment = "{_escape_filter(runtime_environment)}" && '
            f'date = "{_escape_filter(market_date)}" && '
            '(status = "candidate" || status = "active")'
        ),
        sort="-updated",
        per_page=500,
        page=1,
    )
    target_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_symbols: list[str] = []
    for row in target_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in target_by_symbol:
            continue
        target_by_symbol[symbol] = dict(row)
        ordered_symbols.append(symbol)

    if not ordered_symbols:
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            "current_market_date": current_market_date,
            "computed_at_ms": computed_at_ms,
            "computed_at_us": _format_et_datetime(computed_at_ms),
            "computed_at_cn": format_cn_time(computed_at_ms),
            "workflow": workflow_guide,
            "daily_scan": daily_scan,
            "summary": {
                "total": 0,
                "active_count": 0,
                "candidate_count": 0,
                "operable_count": 0,
                "technical_ready_count": 0,
                "signaled_count": 0,
                "awaiting_confirm_count": 0,
                "pending_count": 0,
                "executed_count": 0,
                "stale_count": 0,
            },
            "filters": filters,
            "filtered_summary": {"total": 0, "ready_count": 0, "signaled_count": 0, "needs_action_count": 0},
            "filtered_total": 0,
            "pagination_enabled": paginate,
            "page": 1,
            "per_page": requested_per_page if paginate else 0,
            "total_pages": 1,
            "has_prev_page": False,
            "has_next_page": False,
            "returned_count": 0,
            "items": [],
            "source": "ibkr-api",
        }, 200

    watch_meta = _load_watch_meta(pb, runtime_environment, ordered_symbols)
    lookback_daily_ms = market_start_ms - 20 * 24 * 60 * 60 * 1000
    indicator_lookback_ms = market_start_ms

    daily_records = _load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "1d"',
            _build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {lookback_daily_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=12,
    )
    intraday_records = _load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            _build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=30,
    )
    indicator_records = _load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf("5m")}"',
            f'environment = "{_escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {indicator_lookback_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )
    signal_records = _load_records_for_symbols(
        pb,
        "ibkr_signals",
        base_filter_parts=[
            f'environment = "{_escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )

    daily_history_by_symbol: dict[str, list[dict[str, Any]]] = {}
    fallback_daily_by_symbol: dict[str, dict[str, Any]] = {}
    for row in daily_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        close = to_float(row.get("close")) or 0.0
        if not symbol or bar_time_ms <= 0 or close <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": close,
            "volume": to_float(row.get("volume")) or 0.0,
            "us_time": to_text(row.get("us_time")),
            "date": _format_et_date(bar_time_ms),
        }
        daily_history_by_symbol.setdefault(symbol, []).append(payload_row)
        if payload_row["date"] < market_date:
            fallback_daily_by_symbol[symbol] = payload_row

    latest_intraday_by_symbol: dict[str, dict[str, Any]] = {}
    volume_stats_by_symbol: dict[str, dict[str, float]] = {}
    for row in intraday_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": to_float(row.get("close")) or 0.0,
            "exchange": to_text(row.get("exchange")).upper(),
            "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
            "us_time": to_text(row.get("us_time")),
            "volume": to_float(row.get("volume")) or 0.0,
        }
        current_latest = latest_intraday_by_symbol.get(symbol)
        if current_latest is None or payload_row["bar_time_ms"] >= current_latest["bar_time_ms"]:
            latest_intraday_by_symbol[symbol] = payload_row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "today": 0.0})
        stats["today"] += payload_row["volume"]
        if payload_row["session_type"] == "premarket":
            stats["premarket"] += payload_row["volume"]

    latest_indicator_by_symbol: dict[str, dict[str, Any]] = {}
    for row in indicator_records:
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in latest_indicator_by_symbol:
            continue
        latest_indicator_by_symbol[symbol] = row

    signal_agg_by_symbol: dict[str, dict[str, Any]] = {}
    for row in signal_records:
        normalized_signal = _normalize_signal_record(row)
        symbol = normalized_signal.get("symbol")
        if not symbol:
            continue
        bucket = signal_agg_by_symbol.setdefault(symbol, {"count": 0, "latest": None})
        bucket["count"] += 1
        bucket["latest"] = _pick_latest_signal(bucket.get("latest"), normalized_signal)

    items: list[dict[str, Any]] = []
    active_count = 0
    candidate_count = 0
    operable_count = 0
    technical_ready_count = 0
    signaled_count = 0
    awaiting_confirm_count = 0
    pending_count = 0
    executed_count = 0
    stale_count = 0

    for symbol in ordered_symbols:
        target = target_by_symbol.get(symbol)
        if not target:
            continue
        target_extra = parse_json_object(target.get("extra"))
        screener_snapshot = parse_json_object(target_extra.get("screener_snapshot"))
        meta = watch_meta.get(symbol, {})
        intraday = latest_intraday_by_symbol.get(symbol)
        fallback_daily = fallback_daily_by_symbol.get(symbol)
        indicator_record = latest_indicator_by_symbol.get(symbol)
        indicator_extra = _indicator_snapshot(indicator_record)
        signal_agg = signal_agg_by_symbol.get(symbol, {"count": 0, "latest": None})
        latest_signal = signal_agg.get("latest")
        history = [row for row in daily_history_by_symbol.get(symbol, []) if row.get("date") < market_date]
        last_10 = history[-10:]
        avg_10d_volume = round(sum(to_float(row.get("volume")) or 0.0 for row in last_10) / len(last_10), 2) if last_10 else 0.0
        price = (to_float((intraday or {}).get("close")) or 0.0)
        price_source = "5m"
        if price <= 0:
            price = to_float((fallback_daily or {}).get("close")) or 0.0
            price_source = "1d_close" if price > 0 else ""
        compare_history = _build_daily_change_fields(history, price) if price > 0 else {
            "day_change_pct": 0.0,
            "prev_close_change_pct": 0.0,
            "change_7d": 0.0,
        }
        target_status = to_text(target.get("status")).lower()
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        score = round(to_float(target.get("score")) or 0.0, 2)
        scan_reason = to_text(target.get("scan_reason"))
        intraday_bar_time_ms = to_int((intraday or {}).get("bar_time_ms"), 0)
        latest_bar_time_ms = intraday_bar_time_ms or to_int((fallback_daily or {}).get("bar_time_ms"), 0)
        freshness_min = max(0, int((computed_at_ms - intraday_bar_time_ms) // 60000)) if intraday_bar_time_ms > 0 else None
        volume_stats = volume_stats_by_symbol.get(symbol, {"premarket": 0.0, "today": 0.0})
        row = {
            "symbol": symbol,
            "record_id": to_text(target.get("id")),
            "status": target_status,
            "target_status": target_status,
            "direction_bias": direction_bias,
            "score": score,
            "target_score": score,
            "scan_reason": scan_reason,
            "exchange": to_text(first_defined(meta.get("exchange"), (intraday or {}).get("exchange"), target.get("exchange"))).upper(),
            "industry": to_text(meta.get("industry")),
            "note": to_text(meta.get("note")),
            "price": round(price, 4) if price > 0 else 0.0,
            "price_source": price_source,
            "atr_pct": round(to_float(first_defined(indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "avg_10d_volume": avg_10d_volume,
            "premarket_volume": round(to_float(first_defined(volume_stats.get("premarket"), screener_snapshot.get("premarket_volume"))) or 0.0, 2),
            "today_volume": round(to_float(first_defined(volume_stats.get("today"), screener_snapshot.get("today_volume"))) or 0.0, 2),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_intraday_bar_time_ms": intraday_bar_time_ms,
            "latest_us_time": to_text((intraday or {}).get("us_time")) or to_text(target.get("us_time")) or to_text((fallback_daily or {}).get("us_time")),
            "freshness_min": freshness_min,
            "has_live_bar": intraday_bar_time_ms > 0,
            "day_change_pct": compare_history["day_change_pct"],
            "prev_close_change_pct": compare_history["prev_close_change_pct"],
            "change_7d": compare_history["change_7d"],
            "extra": target_extra,
            "updated": to_text(target.get("updated")),
        }
        tradability_score, assessment_notes = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = _pick_reason_list(assessment_notes, screener_snapshot.get("operable_reasons") if isinstance(screener_snapshot.get("operable_reasons"), list) else [])
        row["is_operable"] = bool(
            row["has_live_bar"]
            and (to_float(row.get("price")) or 0.0) > 0
            and (to_float(row.get("avg_10d_volume")) or 0.0) >= TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME
            and (to_float(row.get("tradability_score")) or 0.0) >= TRADABILITY_OPERABLE_MIN_SCORE
            and isinstance(row.get("freshness_min"), int)
            and row["freshness_min"] <= TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN
        )
        row["technical_flags"] = _build_technical_flags(indicator_extra)
        row["technical_aligned_flags"] = _build_aligned_technical_flags(indicator_extra, direction_bias)
        row["technical_state"] = _resolve_technical_state(row, row["technical_aligned_flags"])
        row["has_signal_today"] = bool(signal_agg.get("count"))
        row["signal_count_today"] = int(signal_agg.get("count") or 0)
        row["latest_signal_id"] = to_text((latest_signal or {}).get("signal_id"))
        row["latest_signal_status"] = to_text((latest_signal or {}).get("status"))
        row["latest_signal_direction"] = to_text((latest_signal or {}).get("direction"))
        row["latest_signal_time"] = to_text((latest_signal or {}).get("us_time"))
        row["latest_signal_time_ms"] = to_int((latest_signal or {}).get("sort_ms"), 0)
        row["latest_signal_note"] = to_text((latest_signal or {}).get("note"))
        attention_state, attention_rank = _resolve_attention_state(row)
        row["attention_state"] = attention_state
        row["attention_rank"] = attention_rank

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if row["is_operable"]:
            operable_count += 1
        if row["technical_state"] == "ready":
            technical_ready_count += 1
        if row["technical_state"] == "stale":
            stale_count += 1
        if row["has_signal_today"]:
            signaled_count += 1
        if row["latest_signal_status"] == "awaiting_confirm":
            awaiting_confirm_count += 1
        if row["latest_signal_status"] == "pending":
            pending_count += 1
        if row["latest_signal_status"] == "executed":
            executed_count += 1
        items.append(row)

    filtered_items = [row for row in _sort_rows(items, to_text(filters.get("sort_by"))) if _matches_filters(row, filters)]
    filtered_summary = _build_filtered_summary(filtered_items)
    total_pages = max(1, (len(filtered_items) + requested_per_page - 1) // requested_per_page) if paginate else 1
    page = min(requested_page, total_pages) if paginate else 1
    offset = (page - 1) * requested_per_page if paginate else 0
    paged_items = filtered_items[offset:offset + requested_per_page] if paginate else filtered_items
    for row in paged_items:
        _ensure_row_details(row)

    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        "current_market_date": current_market_date,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": _format_et_datetime(computed_at_ms),
        "computed_at_cn": format_cn_time(computed_at_ms),
        "workflow": workflow_guide,
        "daily_scan": daily_scan,
        "summary": {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "operable_count": operable_count,
            "technical_ready_count": technical_ready_count,
            "signaled_count": signaled_count,
            "awaiting_confirm_count": awaiting_confirm_count,
            "pending_count": pending_count,
            "executed_count": executed_count,
            "stale_count": stale_count,
        },
        "filters": filters,
        "filtered_summary": filtered_summary,
        "filtered_total": len(filtered_items),
        "pagination_enabled": paginate,
        "page": page,
        "per_page": requested_per_page if paginate else len(filtered_items),
        "total_pages": total_pages,
        "has_prev_page": page > 1 if paginate else False,
        "has_next_page": page < total_pages if paginate else False,
        "returned_count": len(paged_items),
        "items": paged_items,
        "source": "ibkr-api",
    }, 200


__all__ = ["TODAY_TARGET_STATUSES", "build_today_targets_response"]
