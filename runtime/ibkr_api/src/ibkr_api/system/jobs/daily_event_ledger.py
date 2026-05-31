from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.system.jobs.early_expansion_topup import TOPUP_NOTIFY_STATE_KEY, notify_new_targets_from_scan
from ibkr_api.system.jobs.legacy_target_universe import legacy_target_universe_suppressed
from ibkr_api.system.jobs.market_calendar import build_market_calendar_snapshot
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
TARGET_POOL_QUALITY_DUE_ET = "09:31"
TARGET_POOL_QUALITY_CUTOFF_ET = "18:00"
SEED_TARGET_NOTIFY_DUE_ET = "09:30"
EVENT_ORDER = {
    "daily_scan_seed": 10,
    "target_pool_quality": 15,
    "market_closed_notice": 18,
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
RequestJsonRequest = Callable[..., dict[str, Any]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


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
    return [
        dict(item)
        for item in (result.get("new_targets") or [])
        if isinstance(item, dict) and _to_text(item.get("symbol"))
    ]


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


def _scan_quality_thresholds(config_value: ConfigValue, data_environment: str) -> dict[str, Any]:
    return {
        "incomplete_ratio": max(
            0.0,
            min(
                1.0,
                _to_float(
                    config_value("ibkr_daily_scan_quality_defer_incomplete_ratio", "0.2", data_environment),
                    0.2,
                ),
            ),
        ),
        "min_incomplete": max(
            0,
            _to_int(
                config_value("ibkr_daily_scan_quality_defer_min_incomplete", "10", data_environment),
                10,
            ),
        ),
        "min_active": max(
            0,
            _to_int(
                config_value("ibkr_daily_scan_quality_min_active", "8", data_environment),
                8,
            ),
        ),
    }


def _scan_quality_issue(
    *,
    daily_scan: dict[str, Any],
    daily_result: dict[str, Any],
    market_date: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    status = _to_text(daily_scan.get("status")).lower()
    if _to_text(daily_scan.get("market_date")) != market_date or status not in {"completed", "failed"}:
        return {"issue": False, "reason": "scan_not_terminal_for_date"}
    completeness = _as_dict(daily_result.get("data_completeness"))
    rejection_summary = _as_dict(daily_result.get("rejection_summary"))
    scanned = _to_int(daily_result.get("scanned"), 0)
    active = _to_int(daily_result.get("active"), 0)
    excluded = _to_int(
        completeness.get("excluded_incomplete_count")
        or daily_result.get("excluded_incomplete_count")
        or rejection_summary.get("data_incomplete_repairing"),
        0,
    )
    ratio = (excluded / scanned) if scanned > 0 else 0.0
    blocking_enabled = bool(completeness.get("blocking_enabled")) or excluded > 0
    repairing = _to_text(completeness.get("status")).lower() == "repairing" or excluded > 0
    high_incomplete = (
        blocking_enabled
        and repairing
        and excluded >= _to_int(thresholds.get("min_incomplete"), 10)
        and ratio >= _to_float(thresholds.get("incomplete_ratio"), 0.2)
    )
    low_active_with_incomplete = (
        blocking_enabled
        and repairing
        and excluded >= _to_int(thresholds.get("min_incomplete"), 10)
        and active < _to_int(thresholds.get("min_active"), 8)
    )
    reason = ""
    if high_incomplete:
        reason = "high_incomplete_data"
    elif low_active_with_incomplete:
        reason = "low_active_with_incomplete_data"
    return {
        "issue": bool(high_incomplete or low_active_with_incomplete),
        "reason": reason,
        "scan_status": status,
        "scanned": scanned,
        "active": active,
        "excluded_incomplete_count": excluded,
        "excluded_incomplete_ratio": round(ratio, 6),
        "blocking_enabled": blocking_enabled,
        "repairing": repairing,
        "thresholds": dict(thresholds),
    }


def _runtime_watchlist_fresh(payload: dict[str, Any], *, scanned: int = 0) -> dict[str, Any]:
    status_payload = _as_dict(payload)
    topup = _as_dict(status_payload.get("watchlist_idle_topup"))
    completion = _as_dict(topup.get("completion"))
    total = _to_int(completion.get("total"), 0)
    fresh = _to_int(completion.get("fresh"), 0)
    stale = _to_int(completion.get("stale"), 0)
    missing = _to_int(completion.get("missing"), 0)
    unobserved = _to_int(completion.get("unobserved"), 0)
    expected_ms = _to_int(completion.get("expected_latest_5m_ms"), 0)
    oldest_ms = _to_int(completion.get("oldest_latest_ms"), 0)
    ready = total > 0 and fresh >= total and stale <= 0 and missing <= 0 and unobserved <= 0 and (
        expected_ms <= 0 or oldest_ms >= expected_ms
    )
    if not ready:
        market_universe = _as_dict(status_payload.get("market_universe"))
        freshness = _as_dict(market_universe.get("bar_freshness"))
        if _to_text(freshness.get("status")).lower() == "fresh" and _to_int(freshness.get("pending_symbols_total"), 0) <= 0:
            ready = True
    return {
        "ready": ready,
        "total": total,
        "fresh": fresh,
        "stale": stale,
        "missing": missing,
        "unobserved": unobserved,
        "expected_latest_5m_ms": expected_ms,
        "oldest_latest_ms": oldest_ms,
        "scanned": max(0, int(scanned or 0)),
    }


def _fetch_compute_status(
    *,
    request_json_request: RequestJsonRequest | None,
    compute_base_url: str,
    data_environment: str,
) -> dict[str, Any]:
    if not callable(request_json_request) or not _to_text(compute_base_url):
        return {"ok": False, "error": "compute_request_unavailable", "payload": {}}
    try:
        return request_json_request(
            "GET",
            compute_base_url,
            "/ibkr/status",
            params=[("environment", data_environment)],
            timeout=8.0,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload": {}}


def _fetch_seed_scan_status(
    *,
    request_json_request: RequestJsonRequest | None,
    compute_base_url: str,
    data_environment: str,
    market_date: str,
    run_id: str = "",
) -> dict[str, Any]:
    if not callable(request_json_request) or not _to_text(compute_base_url):
        return {"ok": False, "error": "compute_request_unavailable", "payload": {}}
    params = [
        ("environment", data_environment),
        ("market_data_mode", data_environment),
        ("date", market_date),
        ("mode", "seed"),
    ]
    if _to_text(run_id):
        params.append(("run_id", _to_text(run_id)))
    try:
        return request_json_request("GET", compute_base_url, "/scan/status", params=params, timeout=8.0)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload": {}}


def _submit_seed_rescan(
    *,
    request_json_request: RequestJsonRequest | None,
    compute_base_url: str,
    broker_mode: str,
    data_environment: str,
    market_date: str,
) -> dict[str, Any]:
    if not callable(request_json_request) or not _to_text(compute_base_url):
        return {"ok": False, "error": "compute_request_unavailable", "payload": {}}
    run_id = f"target-pool-quality-{data_environment}-{market_date}"
    try:
        return request_json_request(
            "POST",
            compute_base_url,
            "/scan",
            json_body={
                "environment": data_environment,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
                "broker_mode": broker_mode,
                "mode": "seed",
                "force": True,
                "async": True,
                "run_id": run_id,
                "trigger_source": "daily_event_target_pool_quality_repair",
            },
            timeout=15.0,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload": {}}


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
    request_json_request: RequestJsonRequest | None = None,
    compute_base_url: str = "",
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

    calendar = build_market_calendar_snapshot(
        market_date=market_date,
        broker_mode=broker_mode,
        data_environment=data_environment,
        payload=request_payload,
        request_json_request=request_json_request,
        compute_base_url=compute_base_url,
        config_value=config_value,
    )
    if bool(calendar.get("is_closed")):
        for event_id, title, due_at in (
            ("daily_scan_seed", "08:20-09:20 预筛最终轮", DAILY_SCAN_DUE_ET),
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
                    detail={"reason": "market_closed", "calendar": calendar},
                    times=times,
                ),
            )
        reminder_state = _load_state(get_state_payload, OPEN_REPORT_STATE_KEY, broker_mode, market_date)
        closed_sent = bool(_to_text(reminder_state.get("market_closed_notice_sent_at") or reminder_state.get("open_sent_at")))
        if closed_sent and _to_text(reminder_state.get("open_reason")) not in {"market_closed", "non_trading_day"}:
            closed_sent = False
        if closed_sent:
            closed_status = "completed"
        elif not _at_or_after(times, DEFAULT_OPEN_REPORT_TIME_ET):
            closed_status = "pending"
        elif not _before_or_at(times, OPEN_REPORT_CUTOFF_ET):
            closed_status = "missed"
        else:
            closed_status = "ready"
        _update_event(
            ledger,
            _event(
                event_id="market_closed_notice",
                title="闭市提醒",
                status=closed_status,
                due_at_et=DEFAULT_OPEN_REPORT_TIME_ET,
                cutoff_at_et=OPEN_REPORT_CUTOFF_ET,
                source="system_notify_daily",
                detail={
                    "reason": "market_closed",
                    "open_sent_at": _to_text(reminder_state.get("open_sent_at")),
                    "message_id": _to_text(reminder_state.get("open_message_id")),
                    "calendar": calendar,
                },
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

    legacy_suppressed, legacy_reason, legacy_detail = legacy_target_universe_suppressed(
        config_value,
        data_environment,
        broker_mode=broker_mode,
    )
    daily_scan = _load_daily_scan_state(get_state_payload, data_environment)
    if legacy_suppressed:
        daily_scan = {
            "status": "skipped",
            "market_date": market_date,
            "result": {},
            "skip_reason": legacy_reason,
            "skip_detail": legacy_detail,
        }
    daily_result = _as_dict(daily_scan.get("result"))
    daily_completed = _to_text(daily_scan.get("market_date")) == market_date and _to_text(daily_scan.get("status")).lower() == "completed"
    _update_event(
        ledger,
        _event(
            event_id="daily_scan_seed",
            title="08:20-09:20 预筛最终轮",
            status="skipped" if legacy_suppressed else (
                "completed" if daily_completed else ("pending" if not _at_or_after(times, OPEN_REPORT_CUTOFF_ET) else "missed")
            ),
            due_at_et=DAILY_SCAN_DUE_ET,
            cutoff_at_et=OPEN_REPORT_CUTOFF_ET,
            source="tv_primary_slim" if legacy_suppressed else "ibkr_daily_scan_state",
            detail={
                "scan_status": _to_text(daily_scan.get("status")),
                "scan_market_date": _to_text(daily_scan.get("market_date")),
                "active": _to_int(daily_result.get("active")),
                "candidates": _to_int(daily_result.get("candidates")),
                "new_targets": len(_new_targets(daily_result)),
                **({"reason": legacy_reason, **legacy_detail} if legacy_suppressed else {}),
            },
            times=times,
        ),
    )

    quality_thresholds = _scan_quality_thresholds(config_value, data_environment)
    quality_issue = _scan_quality_issue(
        daily_scan=daily_scan,
        daily_result=daily_result,
        market_date=market_date,
        thresholds=quality_thresholds,
    )
    quality_existing = _as_dict(_as_dict(ledger.get("events")).get("target_pool_quality"))
    quality_detail = {
        **_as_dict(quality_existing.get("detail")),
        **quality_issue,
    }
    quality_status = _to_text(quality_existing.get("status")) or "pending"
    repair_run_id = _to_text(quality_detail.get("repair_run_id"))
    if legacy_suppressed:
        quality_status = "skipped"
        quality_detail.update({"reason": legacy_reason, **legacy_detail})
        quality_detail.pop("repair_error", None)
    elif not daily_completed and _to_text(daily_scan.get("market_date")) != market_date:
        quality_status = "pending" if not _after(times, TARGET_POOL_QUALITY_CUTOFF_ET) else "missed"
    elif not bool(quality_issue.get("issue")):
        quality_status = "completed" if _to_text(daily_scan.get("market_date")) == market_date else "pending"
        quality_detail.pop("repair_error", None)
    elif not _at_or_after(times, TARGET_POOL_QUALITY_DUE_ET):
        quality_status = "pending"
    elif not _before_or_at(times, TARGET_POOL_QUALITY_CUTOFF_ET) and not (
        allow_after_cutoff and wants("target_pool_quality")
    ):
        quality_status = "missed"
    else:
        poll_payload: dict[str, Any] = {}
        if repair_run_id:
            poll = _fetch_seed_scan_status(
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
                data_environment=data_environment,
                market_date=market_date,
                run_id=repair_run_id,
            )
            poll_payload = _as_dict(poll.get("payload"))
            poll_status = _to_text(poll_payload.get("status")).lower()
            quality_detail["repair_poll"] = {
                "ok": bool(poll.get("ok")),
                "status": poll_status,
                "error": _to_text(poll.get("error") or poll_payload.get("last_error") or poll_payload.get("error")),
            }
            if poll_status in {"accepted", "pending", "running", "submitted", "in_progress", "processing"}:
                quality_status = "repairing"
            elif poll_status == "completed":
                poll_result = _as_dict(poll_payload.get("result"))
                polled_issue = _scan_quality_issue(
                    daily_scan={"status": "completed", "market_date": market_date},
                    daily_result=poll_result,
                    market_date=market_date,
                    thresholds=quality_thresholds,
                )
                quality_detail["repair_result"] = polled_issue
                quality_status = "completed" if not bool(polled_issue.get("issue")) else "ready"
            elif poll_status == "failed":
                quality_status = "ready"
                quality_detail["repair_error"] = _to_text(poll_payload.get("last_error") or poll_payload.get("error") or poll.get("error"))

        if quality_status not in {"completed", "repairing"}:
            status_result = _fetch_compute_status(
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
                data_environment=data_environment,
            )
            status_payload = _as_dict(status_result.get("payload"))
            runtime_ready = _runtime_watchlist_fresh(status_payload, scanned=_to_int(quality_issue.get("scanned"), 0))
            quality_detail["runtime_readiness"] = {
                **runtime_ready,
                "ok": bool(status_result.get("ok")),
                "error": _to_text(status_result.get("error")),
            }
            if not bool(runtime_ready.get("ready")):
                quality_status = "waiting_data"
            elif wants("target_pool_quality") and not dry_run:
                submit = _submit_seed_rescan(
                    request_json_request=request_json_request,
                    compute_base_url=compute_base_url,
                    broker_mode=broker_mode,
                    data_environment=data_environment,
                    market_date=market_date,
                )
                submit_payload = _as_dict(submit.get("payload"))
                submitted_run_id = _to_text(submit_payload.get("run_id")) or f"target-pool-quality-{data_environment}-{market_date}"
                quality_detail["repair_run_id"] = submitted_run_id
                quality_detail["repair_submitted_at"] = _to_text(times.get("us"))
                quality_detail["repair_submit"] = {
                    "ok": bool(submit.get("ok")) and bool(submit_payload.get("ok", True)),
                    "status": _to_text(submit_payload.get("status")),
                    "accepted": bool(submit_payload.get("accepted") or submit_payload.get("async")),
                    "error": _to_text(submit.get("error") or submit_payload.get("error")),
                }
                actions.append({
                    "event_id": "target_pool_quality",
                    "action": "submit_seed_rescan",
                    "result": _compact_action_result({"run_id": submitted_run_id, **quality_detail["repair_submit"]}),
                })
                quality_status = "repairing" if bool(quality_detail["repair_submit"].get("ok")) else "failed"
            elif dry_run and wants("target_pool_quality"):
                quality_status = "ready"
            else:
                quality_status = "ready"

    _update_event(
        ledger,
        _event(
            event_id="target_pool_quality",
            title="目标池质量复核",
            status=quality_status,
            due_at_et=TARGET_POOL_QUALITY_DUE_ET,
            cutoff_at_et=TARGET_POOL_QUALITY_CUTOFF_ET,
            source="tv_primary_slim" if legacy_suppressed else "ibkr_daily_scan_state",
            detail=quality_detail,
            times=times,
        ),
    )

    seed_targets = _new_targets(daily_result) if daily_completed else []
    if seed_targets:
        seed_notify_key = _to_text(daily_scan.get("run_id") or daily_result.get("run_id")) or market_date
        seed_event_id = f"new_targets:seed:{seed_notify_key}"
        existing_seed = _as_dict(_as_dict(ledger.get("events")).get(seed_event_id))
        seed_done = _to_text(existing_seed.get("status")) in {"completed", "completed_late", "skipped"}
        seed_status = _to_text(existing_seed.get("status")) or "ready"
        detail = {
            **_as_dict(existing_seed.get("detail")),
            "new_targets": len(seed_targets),
            "symbols": [_to_text(item.get("symbol")) for item in seed_targets[:20]],
        }
        repair_seed_notice = bool(seed_notify_key and seed_notify_key == _to_text(quality_detail.get("repair_run_id")))
        if repair_seed_notice:
            detail["late_reason"] = "target_pool_quality_repair"
        if not _to_text(detail.get("message_id")):
            notify_state = _load_state(get_state_payload, TOPUP_NOTIFY_STATE_KEY, broker_mode, market_date)
            notify_key = seed_notify_key
            if notify_key and notify_key in _notified_keys(notify_state):
                detail["message_id"] = _to_text(notify_state.get("last_message_id"))
                detail["notify_key"] = notify_key
        if seed_done:
            seed_status = _to_text(existing_seed.get("status"))
        elif not _at_or_after(times, SEED_TARGET_NOTIFY_DUE_ET):
            seed_status = "pending"
        elif not _before_or_at(times, OPEN_REPORT_CUTOFF_ET) and not (
            (allow_after_cutoff or repair_seed_notice) and wants(seed_event_id)
        ):
            seed_status = "missed"
        elif wants(seed_event_id) and not dry_run:
            result = {
                **daily_result,
                "run_id": seed_notify_key,
                "new_targets": seed_targets,
            }
            delivered = notify_new_targets_from_scan(
                status_payload={
                    "run_id": seed_notify_key,
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
                title="预筛新增标的通知",
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
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
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
