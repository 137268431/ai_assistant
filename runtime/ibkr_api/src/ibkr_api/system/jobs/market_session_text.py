from __future__ import annotations

from typing import Any


SESSION_LABELS_ZH = {
    "closed": "闭市",
    "premarket": "盘前",
    "regular": "盘中",
    "close_transition": "盘后过渡",
    "afterhours": "盘后",
    "overnight": "夜盘",
}
IBKR_CALENDAR_REFRESH_OMITTED = "ibkr_calendar_refresh_omitted"


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def market_session_label(session: dict[str, Any]) -> str:
    kind = _to_text(session.get("kind")).lower()
    return _to_text(session.get("label_zh") or session.get("display_label")) or SESSION_LABELS_ZH.get(kind, kind or "待确认")


def market_calendar_source_label(source: Any, source_error: Any = "") -> str:
    source_text = _to_text(source)
    label = {
        "ibkr_schedule": "IBKR 合约交易时间",
        "local_nyse_fallback": "本地 NYSE 兜底日历",
    }.get(source_text, source_text or "unknown")
    error = _to_text(source_error)
    if error == IBKR_CALENDAR_REFRESH_OMITTED and source_text == "local_nyse_fallback":
        return f"{label}（IBKR 日历刷新已跳过）"
    return f"{label}（IBKR 拉取失败: {error}）" if error and source_text == "local_nyse_fallback" else label


def _window(session: dict[str, Any], open_key: str, close_key: str) -> str:
    open_text = _to_text(session.get(open_key))
    close_text = _to_text(session.get(close_key))
    if not open_text and not close_text:
        return ""
    return f"{open_text or '--'} - {close_text or '--'}"


def market_session_detail_fields(market_session: dict[str, Any] | None) -> dict[str, str]:
    session = _as_dict(market_session)
    if not session:
        return {}
    label = market_session_label(session)
    kind = _to_text(session.get("kind"))
    source = _to_text(session.get("source"))
    source_error = _to_text(session.get("source_error"))
    detail = {
        "市场时段": f"{label}{f' ({kind})' if kind else ''}",
    }
    regular_us = _window(session, "regular_open_us", "regular_close_us")
    extended_us = _window(session, "extended_open_us", "extended_close_us")
    regular_cn = _window(session, "regular_open_beijing", "regular_close_beijing")
    extended_cn = _window(session, "extended_open_beijing", "extended_close_beijing")
    if regular_us or extended_us:
        detail["交易时间(美东)"] = " | ".join(
            item for item in (f"常规 {regular_us}" if regular_us else "", f"扩展 {extended_us}" if extended_us else "") if item
        )
    if regular_cn or extended_cn:
        detail["交易时间(北京)"] = " | ".join(
            item for item in (f"常规 {regular_cn}" if regular_cn else "", f"扩展 {extended_cn}" if extended_cn else "") if item
        )
    if source or source_error:
        detail["日历来源"] = market_calendar_source_label(source, source_error)
    if kind == "closed":
        next_open_us = _to_text(session.get("next_open_us"))
        next_open_cn = _to_text(session.get("next_open_beijing"))
        if next_open_us or next_open_cn:
            detail["下次开盘"] = f"美东 {next_open_us or '待确认'} | 北京 {next_open_cn or '待确认'}"
    return detail


def market_session_from_calendar(calendar: dict[str, Any] | None) -> dict[str, Any]:
    calendar_data = _as_dict(calendar)
    session = _as_dict(calendar_data.get("market_session"))
    if not session:
        return {}
    return {
        **session,
        "source": _to_text(session.get("source") or calendar_data.get("source")),
        "source_error": _to_text(session.get("source_error") or calendar_data.get("source_error")),
        "next_open_us": _to_text(session.get("next_open_us") or calendar_data.get("next_open_us")),
        "next_open_beijing": _to_text(session.get("next_open_beijing") or calendar_data.get("next_open_beijing")),
    }


__all__ = [
    "market_calendar_source_label",
    "market_session_detail_fields",
    "market_session_from_calendar",
    "market_session_label",
]
