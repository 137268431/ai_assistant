from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


SCAN_SUMMARY_STATE_KEY = "system_notify_scan_summary"

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
SignalChatId = Callable[[str], str]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return _to_text(value).lower() not in {"", "0", "false", "no", "off"}


def _matches_time_window(current_us: str, target_et: str) -> bool:
    current = _to_text(current_us)
    target = _to_text(target_et)
    if len(current) < 16 or len(target) < 5:
        return False
    return current[11:16] == target[:5]


def _scan_summary_url(console_base_url: str, environment: str, market_date: str) -> str:
    base = _to_text(console_base_url).rstrip("/")
    if not base:
        return ""
    return (
        f"{base}/ibkr_screener.html"
        f"?environment={environment}"
        f"&tab=screener&view=current"
        f"&date={market_date}"
        f"&market_date={market_date}"
    )


def _build_card(payload: dict[str, Any], *, environment: str, console_base_url: str) -> dict[str, Any]:
    summary = _as_dict(payload.get("summary"))
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    market_date = _to_text(payload.get("market_date"))
    jump_url = _scan_summary_url(console_base_url, environment, market_date)
    lines = []
    if items:
        for index, row in enumerate(items[:5], start=1):
            item = _as_dict(row)
            symbol = _to_text(item.get("symbol")) or f"#{index}"
            status = _to_text(item.get("status")) or "watch"
            score = float(item.get("score") or 0)
            reason = _to_text(item.get("scan_reason")) or "--"
            lines.append(f"{index}. {symbol} | {status}/{score:.1f} | {reason}")
    else:
        lines.append("今日未筛出 candidate / active 标的。")
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**交易日**: {market_date or 'n/a'}\n"
                f"**结果**: {int(summary.get('total') or 0)} 条 · "
                f"active {int(summary.get('active_count') or 0)} · "
                f"candidate {int(summary.get('candidate_count') or 0)} · "
                f"operable {int(summary.get('operable_count') or 0)}"
            ),
        },
        {"tag": "markdown", "content": "\n".join(lines)},
        {
            "tag": "markdown",
            "content": f"🕐 美东 {_to_text(payload.get('computed_at_us')) or 'n/a'}",
        },
    ]
    if jump_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看当前标的榜"},
                        "type": "default",
                        "multi_url": {
                            "url": jump_url,
                            "pc_url": jump_url,
                            "ios_url": jump_url,
                            "android_url": jump_url,
                        },
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "IBKR 今日筛选结果" if _to_int(summary.get("total"), 0) > 0 else "IBKR 今日筛选结果（未筛出标的）",
            },
            "template": "green" if _to_int(summary.get("total"), 0) > 0 else "grey",
        },
        "elements": elements,
    }


def _event_detail(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _as_dict(payload.get("summary"))
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    examples = ", ".join(
        f"{_to_text(_as_dict(item).get('symbol'))}({_to_text(_as_dict(item).get('status'))}/{float(_as_dict(item).get('score') or 0):.1f})"
        for item in items[:5]
        if _to_text(_as_dict(item).get("symbol"))
    )
    return {
        "交易日": _to_text(payload.get("market_date")) or "n/a",
        "总标的": str(_to_int(summary.get("total"), 0)),
        "Active": str(_to_int(summary.get("active_count"), 0)),
        "Candidate": str(_to_int(summary.get("candidate_count"), 0)),
        "Operable": str(_to_int(summary.get("operable_count"), 0)),
        "标的样例": examples or "none",
    }


def build_system_scan_summary_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_today_targets_response: BuildTodayTargetsResponse,
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    signal_chat_id: SignalChatId,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    target_time_et = _to_text(request_payload.get("target_time_et")) or "09:20"
    if not _matches_time_window(times["us"], target_time_et):
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_scan_summary",
            "skipped": True,
            "reason": "outside_time_window",
            "target_time_et": target_time_et,
            "source": "ibkr-api",
        }, 200

    state = _as_dict(get_state_payload(SCAN_SUMMARY_STATE_KEY, environment).get("data"))
    if _to_text(state.get("sent_at")):
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_scan_summary",
            "skipped": True,
            "reason": "already_sent",
            "source": "ibkr-api",
        }, 200

    if not _truthy(config_value("status_notify_enabled", "TRUE", environment)):
        next_state = {
            **state,
            "skipped_at": times["us"],
            "skipped_reason": "status_notify_disabled",
        }
        upsert_state(SCAN_SUMMARY_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_scan_summary",
            "skipped": True,
            "reason": "status_notify_disabled",
            "state": next_state,
            "source": "ibkr-api",
        }, 200

    targets_payload, _ = build_today_targets_response(
        payload={
            "environment": environment,
            "market_date": times["date"],
            "date": times["date"],
            "per_page": 5,
            "page": 1,
            "paginate": False,
        }
    )
    card = _build_card(
        _as_dict(targets_payload),
        environment=environment,
        console_base_url=console_base_url(),
    )
    result = feishu_send_interactive(card, signal_chat_id(environment), environment)
    notified = bool(result.get("success")) and not bool(result.get("suppressed"))
    write_system_event_record(
        "scan_summary",
        "info",
        "ibkr-api",
        "IBKR 今日筛选结果",
        _event_detail(_as_dict(targets_payload)),
        environment,
        notified,
    )
    next_state = {
        **state,
        "sent_at": times["us"] if notified else "",
        "error_at": "" if notified else times["us"],
        "error": "" if notified else (_to_text(result.get("error")) or "send_failed"),
        "message_id": _to_text(result.get("message_id")),
        "market_date": _to_text(targets_payload.get("market_date")) or times["date"],
        "total": _to_int(_as_dict(targets_payload.get("summary")).get("total"), 0),
    }
    upsert_state(SCAN_SUMMARY_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_scan_summary",
        "notified": notified,
        "message_id": _to_text(result.get("message_id")),
        "result": result,
        "state": next_state,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_system_scan_summary_response"]
