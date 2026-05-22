from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.system.jobs.early_expansion_topup import TOPUP_NOTIFY_STATE_KEY, notify_new_targets_from_scan
from ibkr_api.system.jobs.market_calendar import is_nyse_non_trading_day
from ibkr_api.system.jobs.open_report import (
    DEFAULT_OPEN_REPORT_TIME_ET,
    DEFAULT_OPEN_REPORT_WINDOW_MINUTES,
    OPEN_REPORT_STATE_KEY,
    build_system_open_report_response,
)
from ibkr_api.system.jobs.reminders import (
    DEFAULT_DAILY_REPORT_TIME_ET,
    DEFAULT_DAILY_REPORT_WINDOW_MINUTES,
    build_system_daily_report_response,
)


LEDGER_STATE_KEY = "ibkr_daily_event_ledger"
DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
OPEN_REPORT_CUTOFF_ET = "10:30"
DAILY_REPORT_CUTOFF_ET = "18:00"
DAILY_SCAN_DUE_ET = "09:20"
SEED_TARGET_NOTIFY_DUE_ET = "09:30"
EVENT_ORDER = {
    "daily_scan_seed": 10,
    "new_targets:seed": 20,
    "open_report": 30,
    "daily_report": 40,
}

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[..., dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
StartupChatId = Callable[[str], str]
LoadMarketSnapshots = Callable[[str, list[str], str, int], list[dict[str, Any]]]


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


def _parse_hhmm(value: Any) -> int | None:
    text = _to_text(value)
    if len(text) < 5 or ":" not in text[:5]:
        return None
    try:
        hour, minute = text[:5].split(":", 1)
        return int(hour) * 60 + int(minute)
    except Exception:
        return None


def _current_minute(times: dict[str, str]) -> int | None:
    current_us = _to_text(times.get("us"))
    return _parse_hhmm(current_us[11:16]) if len(current_us) >= 16 else None


def _at_or_after(times: dict[str, str], hhmm: str) -> bool:
    current = _current_minute(times)
    target = _parse_hhmm(hhmm)
    return current is not None and target is not None and current >= target


def _before_or_at(times: dict[str, str], hhmm: str) -> bool:
    current = _current_minute(times)
    target = _parse_hhmm(hhmm)
    return current is not None and target is not None and current <= target


def _after(times: dict[str, str], hhmm: str) -> bool:
    current = _current_minute(times)
    target = _parse_hhmm(hhmm)
    return current is not None and target is not None and current > target


def _window_minutes_until_now(times: dict[str, str], target_hhmm: str, default: int) -> int:
    current = _current_minute(times)
    target = _parse_hhmm(target_hhmm)
    if current is None or target is None:
        return int(default)
    return max(int(default), current - target + 1)


def _load_state(get_state_payload: GetStatePayload, key: str, environment: str, date: str) -> dict[str, Any]:
    try:
        payload = get_state_payload(key, environment, date=date)
    except TypeError:
        payload = get_state_payload(key, environment)
    return _as_dict(_as_dict(payload).get("data"))


def _load_daily_scan_state(get_state_payload: GetStatePayload, data_environment: str) -> dict[str, Any]:
    try:
        payload = get_state_payload(DAILY_SCAN_STATE_KEY, data_environment, date="global")
    except TypeError:
        payload = get_state_payload(DAILY_SCAN_STATE_KEY, data_environment)
    return _as_dict(_as_dict(payload).get("data"))


def _new_targets(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in (result.get("new_targets") or []) if isinstance(item, dict)]


def _notified_keys(state: dict[str, Any]) -> set[str]:
    values = state.get("notified_keys") or state.get("notified_run_ids") or []
    keys = {_to_text(item) for item in values if _to_text(item)}
    last_key = _to_text(state.get("last_notified_key") or state.get("last_notified_run_id"))
    if last_key:
        keys.add(last_key)
    return keys


def _event(
    *,
    event_id: str,
    title: str,
    status: str,
    due_at_et: str,
    cutoff_at_et: str = "",
    source: str = "",
    detail: dict[str, Any] | None = None,
    times: dict[str, str],
) -> dict[str, Any]:
    order_key = event_id.rsplit(":", 1)[0] if event_id.startswith("new_targets:seed:") else event_id
    payload = {
        "event_id": event_id,
        "order": EVENT_ORDER.get(order_key, 100),
        "title": title,
        "status": status,
        "due_at_et": due_at_et,
        "cutoff_at_et": cutoff_at_et,
        "source": source,
        "updated_at": _to_text(times.get("us")),
    }
    if detail:
        payload["detail"] = dict(detail)
    return payload


def _update_event(ledger: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    events = _as_dict(ledger.get("events"))
    current = _as_dict(events.get(_to_text(item.get("event_id"))))
    next_item = {**current, **item}
    events[_to_text(item.get("event_id"))] = next_item
    ledger["events"] = events
    return next_item


def _summarize_events(events: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in events.values():
        status = _to_text(_as_dict(item).get("status")) or "unknown"
        counts[status] = counts.get(status, 0) + 1
    complete_statuses = {"completed", "completed_late", "skipped"}
    completed = sum(count for status, count in counts.items() if status in complete_statuses)
    total = sum(counts.values())
    blocking = [event_id for event_id, item in events.items() if _to_text(_as_dict(item).get("status")) in {"failed", "missed"}]
    return {
        "total": total,
        "completed": completed,
        "pending": max(0, total - completed - len(blocking)),
        "blocking": len(blocking),
        "status_counts": counts,
        "blocking_event_ids": blocking,
        "status": "blocked" if blocking else ("completed" if total and completed == total else "pending"),
    }


def _ordered_events(events: dict[str, Any]) -> list[dict[str, Any]]:
    items = [dict(item) for item in events.values() if isinstance(item, dict)]
    return sorted(items, key=lambda item: (_to_int(item.get("order"), 100), _to_text(item.get("event_id"))))


def _compact_action_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result) if isinstance(result, dict) else {}
    new_targets = _new_targets(payload)
    if new_targets:
        payload["new_targets_count"] = len(new_targets)
        payload["symbols"] = [_to_text(item.get("symbol")) for item in new_targets[:20]]
        payload.pop("new_targets", None)
    if isinstance(payload.get("state"), dict):
        state = _as_dict(payload.get("state"))
        payload["state"] = {
            key: state.get(key)
            for key in (
                "open_sent_at",
                "open_message_id",
                "open_error",
                "close_sent_at",
                "close_message_id",
                "close_error",
            )
            if key in state
        }
    return payload


def _save_ledger(
    *,
    upsert_state: UpsertState,
    broker_mode: str,
    market_date: str,
    ledger: dict[str, Any],
    times: dict[str, str],
) -> dict[str, Any]:
    events = _as_dict(ledger.get("events"))
    summary = _summarize_events(events)
    next_ledger = {
        **ledger,
        "market_date": market_date,
        "broker_mode": broker_mode,
        "updated_at": _to_text(times.get("us")),
        "items": _ordered_events(events),
        "summary": summary,
    }
    upsert_state(LEDGER_STATE_KEY, broker_mode, next_ledger, market_date)
    return next_ledger


def build_daily_event_ledger_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    get_state_payload: GetStatePayload,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    times = time_strings()
    market_date = _to_text(request_payload.get("market_date") or request_payload.get("date") or times.get("date"))
    ledger = _load_state(get_state_payload, LEDGER_STATE_KEY, broker_mode, market_date)
    events = _as_dict(ledger.get("events"))
    if events:
        ledger = {**ledger, "items": _ordered_events(events), "summary": _summarize_events(events)}
    return {
        "ok": True,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "market_date": market_date,
        "ledger": ledger,
        "source": "ibkr-api",
    }, 200


def build_daily_event_reconcile_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_today_targets_response: BuildTodayTargetsResponse,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
    load_market_snapshots: LoadMarketSnapshots | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    times = time_strings()
    market_date = _to_text(request_payload.get("market_date") or request_payload.get("date") or times.get("date"))
    dry_run = _truthy(request_payload.get("dry_run")) if "dry_run" in request_payload else False
    force_event_id = _to_text(request_payload.get("force_event_id"))
    allow_after_cutoff = _truthy(request_payload.get("allow_after_cutoff") or request_payload.get("force_after_cutoff"))
    actions: list[dict[str, Any]] = []
    ledger = _load_state(get_state_payload, LEDGER_STATE_KEY, broker_mode, market_date)
    ledger = {
        "version": 1,
        "market_date": market_date,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        **ledger,
    }

    def wants(event_id: str) -> bool:
        if not force_event_id:
            return True
        return (
            force_event_id == event_id
            or event_id.startswith(f"{force_event_id}:")
            or force_event_id == event_id.split(":", 1)[0]
        )

    if is_nyse_non_trading_day(market_date):
        for event_id, title, due_at in (
            ("daily_scan_seed", "盘前日筛", DAILY_SCAN_DUE_ET),
            ("open_report", "09:30 开盘交易摘要", DEFAULT_OPEN_REPORT_TIME_ET),
            ("daily_report", "16:05 收盘汇总", DEFAULT_DAILY_REPORT_TIME_ET),
        ):
            _update_event(
                ledger,
                _event(
                    event_id=event_id,
                    title=title,
                    status="skipped",
                    due_at_et=due_at,
                    source="calendar",
                    detail={"reason": "non_trading_day"},
                    times=times,
                ),
            )
        saved = ledger if dry_run else _save_ledger(upsert_state=upsert_state, broker_mode=broker_mode, market_date=market_date, ledger=ledger, times=times)
        return {
            "ok": True,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "dry_run": dry_run,
            "allow_after_cutoff": allow_after_cutoff,
            "actions": actions,
            "ledger": saved,
            "source": "ibkr-api",
            "job_id": "system_daily_event_reconcile",
        }, 200

    daily_scan = _load_daily_scan_state(get_state_payload, data_environment)
    daily_result = _as_dict(daily_scan.get("result"))
    daily_completed = _to_text(daily_scan.get("market_date")) == market_date and _to_text(daily_scan.get("status")).lower() == "completed"
    _update_event(
        ledger,
        _event(
            event_id="daily_scan_seed",
            title="盘前日筛",
            status="completed" if daily_completed else ("pending" if not _at_or_after(times, OPEN_REPORT_CUTOFF_ET) else "missed"),
            due_at_et=DAILY_SCAN_DUE_ET,
            cutoff_at_et=OPEN_REPORT_CUTOFF_ET,
            source="ibkr_daily_scan_state",
            detail={
                "scan_status": _to_text(daily_scan.get("status")),
                "scan_market_date": _to_text(daily_scan.get("market_date")),
                "active": _to_int(daily_result.get("active")),
                "candidates": _to_int(daily_result.get("candidates")),
                "new_targets": len(_new_targets(daily_result)),
            },
            times=times,
        ),
    )

    seed_targets = _new_targets(daily_result) if daily_completed else []
    if seed_targets:
        seed_event_id = f"new_targets:seed:{_to_text(daily_scan.get('run_id') or daily_result.get('run_id')) or market_date}"
        existing_seed = _as_dict(_as_dict(ledger.get("events")).get(seed_event_id))
        seed_done = _to_text(existing_seed.get("status")) in {"completed", "completed_late", "skipped"}
        seed_status = _to_text(existing_seed.get("status")) or "ready"
        detail = {
            **_as_dict(existing_seed.get("detail")),
            "new_targets": len(seed_targets),
            "symbols": [_to_text(item.get("symbol")) for item in seed_targets[:20]],
        }
        if not _to_text(detail.get("message_id")):
            notify_state = _load_state(get_state_payload, TOPUP_NOTIFY_STATE_KEY, broker_mode, market_date)
            notify_key = _to_text(daily_scan.get("run_id") or daily_result.get("run_id"))
            if notify_key and notify_key in _notified_keys(notify_state):
                detail["message_id"] = _to_text(notify_state.get("last_message_id"))
                detail["notify_key"] = notify_key
        if seed_done:
            seed_status = _to_text(existing_seed.get("status"))
        elif not _at_or_after(times, SEED_TARGET_NOTIFY_DUE_ET):
            seed_status = "pending"
        elif not _before_or_at(times, OPEN_REPORT_CUTOFF_ET) and not (allow_after_cutoff and wants(seed_event_id)):
            seed_status = "missed"
        elif wants(seed_event_id) and not dry_run:
            result = {
                **daily_result,
                "run_id": _to_text(daily_scan.get("run_id") or daily_result.get("run_id")),
                "new_targets": seed_targets,
            }
            delivered = notify_new_targets_from_scan(
                status_payload={
                    "run_id": _to_text(daily_scan.get("run_id") or daily_result.get("run_id")),
                    "finished_at": _to_text(daily_scan.get("finished_at")),
                    "status": "completed",
                },
                result=result,
                broker_mode=broker_mode,
                data_environment=data_environment,
                market_date=market_date,
                times=times,
                feishu_send_interactive=feishu_send_interactive,
                write_system_event_record=write_system_event_record,
                config_value=config_value,
                console_base_url=console_base_url,
                startup_chat_id=startup_chat_id,
                get_state_payload=get_state_payload,
                upsert_state=upsert_state,
                source="seed",
            )
            actions.append({"event_id": seed_event_id, "action": "notify_new_targets", "result": _compact_action_result(delivered)})
            if delivered.get("error"):
                seed_status = "failed"
                detail["error"] = _to_text(delivered.get("error"))
            elif delivered.get("reason") == "already_notified" or delivered.get("finalized") or delivered.get("notified"):
                seed_status = "completed_late" if _after(times, SEED_TARGET_NOTIFY_DUE_ET) else "completed"
                detail["message_id"] = _to_text(delivered.get("message_id"))
                detail["notify_key"] = _to_text(delivered.get("notify_key"))
        elif dry_run and wants(seed_event_id):
            seed_status = "ready"
        _update_event(
            ledger,
            _event(
                event_id=seed_event_id,
                title="盘前新增标的通知",
                status=seed_status,
                due_at_et=SEED_TARGET_NOTIFY_DUE_ET,
                cutoff_at_et=OPEN_REPORT_CUTOFF_ET,
                source="daily_scan_seed",
                detail=detail,
                times=times,
            ),
        )

    reminder_state = _load_state(get_state_payload, OPEN_REPORT_STATE_KEY, broker_mode, market_date)
    open_sent = bool(_to_text(reminder_state.get("open_sent_at")))
    if open_sent:
        open_status = "completed"
    elif not _at_or_after(times, DEFAULT_OPEN_REPORT_TIME_ET):
        open_status = "pending"
    elif not _before_or_at(times, OPEN_REPORT_CUTOFF_ET):
        open_status = "missed"
    else:
        open_status = "ready"
    if wants("open_report") and not open_sent and _at_or_after(times, DEFAULT_OPEN_REPORT_TIME_ET) and _before_or_at(times, OPEN_REPORT_CUTOFF_ET):
        if not dry_run:
            report, _status = build_system_open_report_response(
                payload={
                    "broker_mode": broker_mode,
                    "market_data_mode": data_environment,
                    "target_time_et": DEFAULT_OPEN_REPORT_TIME_ET,
                    "window_minutes": _window_minutes_until_now(times, DEFAULT_OPEN_REPORT_TIME_ET, DEFAULT_OPEN_REPORT_WINDOW_MINUTES),
                },
                normalize_environment=normalize_environment,
                time_strings=time_strings,
                build_today_targets_response=build_today_targets_response,
                build_system_summary_payload=build_system_summary_payload,
                build_system_monitor_payload=build_system_monitor_payload,
                feishu_send_interactive=feishu_send_interactive,
                write_system_event_record=write_system_event_record,
                get_state_payload=get_state_payload,
                upsert_state=upsert_state,
                config_value=config_value,
                console_base_url=console_base_url,
                startup_chat_id=startup_chat_id,
                load_market_snapshots=load_market_snapshots,
            )
            actions.append({"event_id": "open_report", "action": "send_open_report", "result": _compact_action_result(report)})
            if isinstance(report.get("state"), dict):
                reminder_state = _as_dict(report.get("state"))
            open_status = "completed_late" if report.get("ok") and not report.get("skipped") and _after(times, DEFAULT_OPEN_REPORT_TIME_ET) else (
                "completed" if report.get("ok") and not report.get("skipped") else ("failed" if report.get("error") else "ready")
            )
        else:
            open_status = "ready"
    _update_event(
        ledger,
        _event(
            event_id="open_report",
            title="09:30 开盘交易摘要",
            status=open_status,
            due_at_et=DEFAULT_OPEN_REPORT_TIME_ET,
            cutoff_at_et=OPEN_REPORT_CUTOFF_ET,
            source="system_notify_daily",
            detail={
                "open_sent_at": _to_text(reminder_state.get("open_sent_at")),
                "open_message_id": _to_text(reminder_state.get("open_message_id")),
            },
            times=times,
        ),
    )

    reminder_state = _load_state(get_state_payload, OPEN_REPORT_STATE_KEY, broker_mode, market_date)
    close_sent = bool(_to_text(reminder_state.get("close_sent_at")))
    if close_sent:
        close_status = "completed"
    elif not _at_or_after(times, DEFAULT_DAILY_REPORT_TIME_ET):
        close_status = "pending"
    elif not _before_or_at(times, DAILY_REPORT_CUTOFF_ET):
        close_status = "missed"
    else:
        close_status = "ready"
    if wants("daily_report") and not close_sent and _at_or_after(times, DEFAULT_DAILY_REPORT_TIME_ET) and _before_or_at(times, DAILY_REPORT_CUTOFF_ET):
        if not dry_run:
            report, _status = build_system_daily_report_response(
                payload={
                    "broker_mode": broker_mode,
                    "market_data_mode": data_environment,
                    "target_time_et": DEFAULT_DAILY_REPORT_TIME_ET,
                    "window_minutes": _window_minutes_until_now(times, DEFAULT_DAILY_REPORT_TIME_ET, DEFAULT_DAILY_REPORT_WINDOW_MINUTES),
                },
                normalize_environment=normalize_environment,
                time_strings=time_strings,
                build_system_summary_payload=build_system_summary_payload,
                build_system_monitor_payload=build_system_monitor_payload,
                feishu_send_interactive=feishu_send_interactive,
                write_system_event_record=write_system_event_record,
                get_state_payload=get_state_payload,
                upsert_state=upsert_state,
                config_value=config_value,
                console_base_url=console_base_url,
                startup_chat_id=startup_chat_id,
                build_today_targets_response=build_today_targets_response,
            )
            actions.append({"event_id": "daily_report", "action": "send_daily_report", "result": _compact_action_result(report)})
            if isinstance(report.get("state"), dict):
                reminder_state = _as_dict(report.get("state"))
            close_status = "completed_late" if report.get("ok") and not report.get("skipped") and _after(times, DEFAULT_DAILY_REPORT_TIME_ET) else (
                "completed" if report.get("ok") and not report.get("skipped") else ("failed" if report.get("error") else "ready")
            )
        else:
            close_status = "ready"
    _update_event(
        ledger,
        _event(
            event_id="daily_report",
            title="16:05 收盘汇总",
            status=close_status,
            due_at_et=DEFAULT_DAILY_REPORT_TIME_ET,
            cutoff_at_et=DAILY_REPORT_CUTOFF_ET,
            source="system_notify_daily",
            detail={
                "close_sent_at": _to_text(reminder_state.get("close_sent_at")),
                "close_message_id": _to_text(reminder_state.get("close_message_id")),
            },
            times=times,
        ),
    )

    saved = ledger if dry_run else _save_ledger(
        upsert_state=upsert_state,
        broker_mode=broker_mode,
        market_date=market_date,
        ledger=ledger,
        times=times,
    )
    return {
        "ok": True,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "market_date": market_date,
        "dry_run": dry_run,
        "allow_after_cutoff": allow_after_cutoff,
        "actions": actions,
        "ledger": saved,
        "source": "ibkr-api",
        "job_id": "system_daily_event_reconcile",
    }, 200


__all__ = [
    "DAILY_SCAN_STATE_KEY",
    "LEDGER_STATE_KEY",
    "build_daily_event_ledger_response",
    "build_daily_event_reconcile_response",
]
