from __future__ import annotations

import time
from typing import Any, Callable


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
RequestJsonRequest = Callable[..., dict[str, Any]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
StartupChatId = Callable[[str], str]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]


TOPUP_NOTIFY_STATE_KEY = "ibkr_early_expansion_topup_notify"


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


def _elapsed_s(started_at: float) -> float:
    return round(max(0.0, time.monotonic() - started_at), 3)


def _submit_timeout_seconds(payload: dict[str, Any]) -> float:
    requested = payload.get("submit_timeout")
    if requested is None:
        requested = payload.get("timeout")
    timeout = _to_float(requested, 15.0)
    if timeout <= 0:
        timeout = 15.0
    return min(max(timeout, 1.0), 30.0)


def _is_read_timeout_error(error: Any) -> bool:
    text = _to_text(error).lower()
    return "read timed out" in text or "readtimeout" in text or "read timeout" in text


def _scan_status(result: dict[str, Any]) -> str:
    return _to_text(result.get("status") or result.get("state")).lower()


def _submission_status(result: dict[str, Any]) -> str:
    status = _scan_status(result)
    if status in {"running", "in_progress", "processing"}:
        return "running"
    return "submitted"


def _is_pending_scan_status(status: str) -> bool:
    return status in {"accepted", "submitted", "running", "pending", "in_progress", "processing"}


def _scan_status_params(environment: str, market_date: str) -> list[tuple[str, str]]:
    return [
        ("environment", environment),
        ("date", market_date),
        ("mode", "topup"),
    ]


def _scan_result_from_status(payload: dict[str, Any]) -> dict[str, Any]:
    result = _as_dict(payload.get("result"))
    return result if result else _as_dict(payload)


def _new_targets(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (result.get("new_targets") or []) if isinstance(item, dict)]


def _notify_key(status_payload: dict[str, Any], result: dict[str, Any], environment: str, market_date: str) -> str:
    run_id = _to_text(status_payload.get("run_id") or result.get("run_id"))
    if run_id:
        return run_id
    symbols = ",".join(_to_text(item.get("symbol")).upper() for item in _new_targets(result) if _to_text(item.get("symbol")))
    finished_at = _to_text(status_payload.get("finished_at") or result.get("finished_at"))
    return f"{environment}:{market_date}:{symbols}:{finished_at}"


def _load_notify_state(get_state_payload: GetStatePayload | None, environment: str) -> dict[str, Any]:
    if not callable(get_state_payload):
        return {}
    try:
        return _as_dict(_as_dict(get_state_payload(TOPUP_NOTIFY_STATE_KEY, environment)).get("data"))
    except Exception:
        return {}


def _notified_keys(state: dict[str, Any]) -> set[str]:
    values = state.get("notified_keys") or state.get("notified_run_ids") or []
    keys = {_to_text(item) for item in values if _to_text(item)}
    last_key = _to_text(state.get("last_notified_key") or state.get("last_notified_run_id"))
    if last_key:
        keys.add(last_key)
    return keys


def _record_notify_state(
    *,
    upsert_state: UpsertState | None,
    environment: str,
    market_date: str,
    state: dict[str, Any],
    notify_key: str,
    times: dict[str, str],
    result: dict[str, Any],
    message_id: str,
) -> None:
    if not callable(upsert_state) or not notify_key:
        return
    previous = [item for item in (state.get("notified_keys") or state.get("notified_run_ids") or []) if _to_text(item)]
    keys = [*previous, notify_key]
    deduped_keys = list(dict.fromkeys(keys))[-30:]
    next_state = {
        **state,
        "last_notified_key": notify_key,
        "last_notified_run_id": notify_key,
        "last_notified_at": _to_text(times.get("us")),
        "last_message_id": message_id,
        "last_new_active": _to_int(result.get("new_active")),
        "last_new_candidates": _to_int(result.get("new_candidates")),
        "last_symbols": [_to_text(item.get("symbol")) for item in _new_targets(result)[:20]],
        "notified_keys": deduped_keys,
        "notified_run_ids": deduped_keys,
    }
    try:
        upsert_state(TOPUP_NOTIFY_STATE_KEY, environment, next_state, market_date)
    except Exception:
        pass


def _status_detail(
    *,
    status: str,
    elapsed_s: float,
    scan_payload: dict[str, Any],
    upstream: dict[str, Any],
    result: dict[str, Any],
    error: str = "",
    timeout_waiting: bool = False,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "status": status,
        "elapsed_s": elapsed_s,
        "mode": _to_text(scan_payload.get("mode")),
        "async": bool(scan_payload.get("async")),
        "compute_status_code": _to_int(upstream.get("status_code"), 0),
    }
    for key in ("target_url",):
        value = _to_text(upstream.get(key))
        if value:
            detail[key] = value
    for key in ("run_id", "date", "market_date", "mode"):
        value = _to_text(result.get(key))
        if value:
            detail[key] = value
    upstream_status = _scan_status(result)
    if upstream_status:
        detail["upstream_status"] = upstream_status
    if bool(result.get("accepted")):
        detail["accepted"] = True
    if bool(result.get("existing")):
        detail["existing"] = True
    if timeout_waiting and error:
        detail["upstream_error"] = error
    elif error:
        detail["error"] = error
    return detail


def _target_line(item: dict[str, Any], index: int) -> str:
    symbol = _to_text(item.get("symbol")) or f"#{index}"
    status = _to_text(item.get("status")) or "candidate"
    direction = _to_text(item.get("direction_bias")) or "neutral"
    score = _to_float(item.get("score"), 0.0)
    reason = _to_text(item.get("scan_reason")) or "--"
    return f"{index}. {symbol} | {status}/{direction} | score {score:.1f} | {reason}"


def _report_url(console_base_url: str, environment: str, market_date: str) -> str:
    base = _to_text(console_base_url).rstrip("/")
    if not base:
        return ""
    return f"{base}/ibkr_screener.html?environment={environment}&tab=screener&view=current&date={market_date}&market_date={market_date}"


def _build_card(
    *,
    environment: str,
    market_date: str,
    times: dict[str, str],
    result: dict[str, Any],
    console_base_url: str,
) -> dict[str, Any]:
    new_targets = [_as_dict(item) for item in (result.get("new_targets") or []) if isinstance(item, dict)]
    lines = [_target_line(item, index) for index, item in enumerate(new_targets[:8], start=1)]
    if len(new_targets) > len(lines):
        lines.append(f"... 另有 {len(new_targets) - len(lines)} 个新增标的")
    if not lines:
        lines = ["本轮没有新增标的。"]

    active = _to_int(result.get("new_active"), 0)
    candidates = _to_int(result.get("new_candidates"), 0)
    scan_time = _to_text(times.get("us")) or "n/a"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**交易日**: {market_date}\n"
                f"**检查时间**: 美东 {scan_time} | 北京 {_to_text(times.get('cn')) or 'n/a'}\n"
                f"**本轮新增**: active {active} | candidate {candidates}\n"
                f"**扫描统计**: scanned {_to_int(result.get('scanned'))} | eligible {_to_int(result.get('eligible'))} | errors {_to_int(result.get('errors'))}"
            ),
        },
        {"tag": "markdown", "content": "**新增可操作标的**:\n" + "\n".join(lines)},
    ]
    url = _report_url(console_base_url, environment, market_date)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看今日标的榜"},
                        "type": "primary",
                        "multi_url": {"url": url, "pc_url": url, "ios_url": url, "android_url": url},
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"IBKR 早盘扩池新增 · Broker {environment.upper()}"},
            "template": "green",
        },
        "elements": elements,
    }


def _build_failure_card(*, environment: str, market_date: str, times: dict[str, str], error: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"IBKR 早盘扩池失败 · Broker {environment.upper()}"},
            "template": "red",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**交易日**: {market_date}\n"
                    f"**检查时间**: 美东 {_to_text(times.get('us')) or 'n/a'} | 北京 {_to_text(times.get('cn')) or 'n/a'}\n"
                    f"**错误**: {error or 'unknown_error'}"
                ),
            }
        ],
    }


def _deliver_new_targets_notification(
    *,
    environment: str,
    market_date: str,
    times: dict[str, str],
    result: dict[str, Any],
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
) -> dict[str, Any]:
    new_targets = _new_targets(result)
    notified = False
    message_id = ""
    send_result: dict[str, Any] = {}
    notify_enabled = _truthy(config_value("status_notify_enabled", "TRUE", environment))
    if notify_enabled:
        card = _build_card(
            environment=environment,
            market_date=market_date,
            times=times,
            result={**result, "new_targets": new_targets},
            console_base_url=console_base_url(),
        )
        send_result = _as_dict(feishu_send_interactive(card, startup_chat_id(environment), environment))
        notified = bool(send_result.get("success")) and not bool(send_result.get("suppressed"))
        message_id = _to_text(send_result.get("message_id"))

    write_system_event_record(
        "early_expansion_topup",
        "info",
        "ibkr_api",
        "IBKR 早盘扩池新增",
        {
            "market_date": market_date,
            "new_active": _to_int(result.get("new_active")),
            "new_candidates": _to_int(result.get("new_candidates")),
            "symbols": [_to_text(item.get("symbol")) for item in new_targets[:20]],
        },
        environment,
        notified,
    )
    finalized = (
        not notify_enabled
        or notified
        or bool(send_result.get("skipped"))
        or bool(send_result.get("suppressed"))
    )
    return {
        "notified": notified,
        "message_id": message_id,
        "finalized": finalized,
        "skipped": bool(send_result.get("skipped")) if notify_enabled else True,
        "suppressed": bool(send_result.get("suppressed")),
        "error": "" if finalized else (_to_text(send_result.get("error")) or "send_failed"),
        "new_targets": new_targets,
        "new_active": _to_int(result.get("new_active")),
        "new_candidates": _to_int(result.get("new_candidates")),
    }


def _notify_completed_async_scan(
    *,
    status_payload: dict[str, Any],
    result: dict[str, Any],
    environment: str,
    market_date: str,
    times: dict[str, str],
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
    get_state_payload: GetStatePayload | None,
    upsert_state: UpsertState | None,
) -> dict[str, Any]:
    new_targets = _new_targets(result)
    if not new_targets:
        return {"checked": True, "skipped": True, "reason": "no_new_targets", "notified": False}

    state = _load_notify_state(get_state_payload, environment)
    notify_key = _notify_key(status_payload, result, environment, market_date)
    if notify_key and notify_key in _notified_keys(state):
        return {
            "checked": True,
            "skipped": True,
            "reason": "already_notified",
            "notify_key": notify_key,
            "notified": False,
            "new_targets": new_targets,
        }

    delivered = _deliver_new_targets_notification(
        environment=environment,
        market_date=market_date,
        times=times,
        result=result,
        feishu_send_interactive=feishu_send_interactive,
        write_system_event_record=write_system_event_record,
        config_value=config_value,
        console_base_url=console_base_url,
        startup_chat_id=startup_chat_id,
    )
    if delivered.get("finalized"):
        _record_notify_state(
            upsert_state=upsert_state,
            environment=environment,
            market_date=market_date,
            state=state,
            notify_key=notify_key,
            times=times,
            result=result,
            message_id=_to_text(delivered.get("message_id")),
        )
    return {
        "checked": True,
        "notify_key": notify_key,
        **delivered,
    }


def _fetch_completed_or_pending_scan(
    *,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
    environment: str,
    market_date: str,
) -> dict[str, Any]:
    try:
        status_result = request_json_request(
            "GET",
            compute_base_url,
            "/scan/status",
            params=_scan_status_params(environment, market_date),
            timeout=3.0,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload": {}}
    payload = _as_dict(status_result.get("payload"))
    if not bool(status_result.get("ok")):
        return {"ok": False, "error": _to_text(status_result.get("error")), "payload": payload}
    return {"ok": True, "payload": payload}


def build_early_expansion_topup_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
    get_state_payload: GetStatePayload | None = None,
    upsert_state: UpsertState | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    market_date = _to_text(request_payload.get("market_date") or request_payload.get("date") or times.get("date"))
    scan_payload = {
        "environment": environment,
        "mode": "topup",
        "force": True,
        "async": True,
        "trigger_source": _to_text(request_payload.get("trigger_source")) or "early_expansion_topup",
    }
    completed_notification: dict[str, Any] = {}
    should_check_existing_scan = callable(get_state_payload) and callable(upsert_state)
    if should_check_existing_scan:
        existing_scan = _fetch_completed_or_pending_scan(
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            environment=environment,
            market_date=market_date,
        )
        existing_payload = _as_dict(existing_scan.get("payload"))
        existing_status = _scan_status(existing_payload)
        if bool(existing_scan.get("ok")) and existing_status == "completed":
            existing_result = _scan_result_from_status(existing_payload)
            completed_notification = _notify_completed_async_scan(
                status_payload=existing_payload,
                result=existing_result,
                environment=environment,
                market_date=market_date,
                times=times,
                feishu_send_interactive=feishu_send_interactive,
                write_system_event_record=write_system_event_record,
                config_value=config_value,
                console_base_url=console_base_url,
                startup_chat_id=startup_chat_id,
                get_state_payload=get_state_payload,
                upsert_state=upsert_state,
            )
            if completed_notification.get("error"):
                return {
                    "ok": False,
                    "environment": environment,
                    "market_date": market_date,
                    "job_id": "ibkr_early_expansion_topup",
                    "status": "notification_failed",
                    "notified": False,
                    "message_id": _to_text(completed_notification.get("message_id")),
                    "error": _to_text(completed_notification.get("error")),
                    "completed_notification": completed_notification,
                    "scan": existing_payload,
                    "source": "ibkr-api",
                }, 502
        elif bool(existing_scan.get("ok")) and _is_pending_scan_status(existing_status):
            return {
                "ok": True,
                "environment": environment,
                "market_date": market_date,
                "job_id": "ibkr_early_expansion_topup",
                "status": _submission_status(existing_payload),
                "accepted": True,
                "async": True,
                "run_id": _to_text(existing_payload.get("run_id")),
                "pending": True,
                "notified": False,
                "detail": _status_detail(
                    status=_submission_status(existing_payload),
                    elapsed_s=0.0,
                    scan_payload=scan_payload,
                    upstream={"ok": True, "status_code": 200, "target_url": f"{compute_base_url.rstrip('/')}/scan/status"},
                    result=existing_payload,
                ),
                "scan": existing_payload,
                "source": "ibkr-api",
            }, 200

    started_at = time.monotonic()
    upstream = request_json_request(
        "POST",
        compute_base_url,
        "/scan",
        json_body=scan_payload,
        timeout=_submit_timeout_seconds(request_payload),
    )
    elapsed_s = _elapsed_s(started_at)
    result = _as_dict(upstream.get("payload"))
    error = _to_text(upstream.get("error") or result.get("error") or result.get("reason"))
    if not bool(upstream.get("ok")) and _is_read_timeout_error(error):
        status = "timeout_waiting"
        detail = _status_detail(
            status=status,
            elapsed_s=elapsed_s,
            scan_payload=scan_payload,
            upstream=upstream,
            result=result,
            error=error,
            timeout_waiting=True,
        )
        return {
            "ok": True,
            "environment": environment,
            "market_date": market_date,
            "job_id": "ibkr_early_expansion_topup",
            "status": status,
            "async": True,
            "pending": True,
            "notified": False,
            "elapsed_s": elapsed_s,
            "detail": detail,
            "scan": result,
            "source": "ibkr-api",
        }, 200

    ok = bool(upstream.get("ok")) and bool(result.get("ok", True)) and not bool(result.get("skipped"))

    if ok and (
        bool(result.get("accepted"))
        or _scan_status(result) in {"accepted", "submitted", "running", "pending", "in_progress", "processing"}
    ):
        status = _submission_status(result)
        detail = _status_detail(
            status=status,
            elapsed_s=elapsed_s,
            scan_payload=scan_payload,
            upstream=upstream,
            result=result,
        )
        return {
            "ok": True,
            "environment": environment,
            "market_date": market_date,
            "job_id": "ibkr_early_expansion_topup",
            "status": status,
            "accepted": True,
            "async": True,
            "run_id": _to_text(result.get("run_id")),
            "notified": bool(completed_notification.get("notified")),
            "message_id": _to_text(completed_notification.get("message_id")),
            "elapsed_s": elapsed_s,
            "detail": detail,
            "completed_notification": completed_notification,
            "scan": result,
            "source": "ibkr-api",
        }, 200

    if not ok:
        status = "failed"
        detail = _status_detail(
            status=status,
            elapsed_s=elapsed_s,
            scan_payload=scan_payload,
            upstream=upstream,
            result=result,
            error=error or "scan_failed",
        )
        notified = False
        message_id = ""
        if _truthy(config_value("status_notify_enabled", "TRUE", environment)):
            card = _build_failure_card(environment=environment, market_date=market_date, times=times, error=error)
            send_result = _as_dict(feishu_send_interactive(card, startup_chat_id(environment), environment))
            notified = bool(send_result.get("success")) and not bool(send_result.get("suppressed"))
            message_id = _to_text(send_result.get("message_id"))
        write_system_event_record(
            "early_expansion_topup",
            "error",
            "ibkr_api",
            "IBKR 早盘扩池失败",
            {"market_date": market_date, "error": error or "scan_failed", "elapsed_s": elapsed_s, "upstream": upstream},
            environment,
            notified,
        )
        return {
            "ok": False,
            "environment": environment,
            "market_date": market_date,
            "job_id": "ibkr_early_expansion_topup",
            "status": status,
            "notified": notified,
            "message_id": message_id,
            "error": error or "scan_failed",
            "elapsed_s": elapsed_s,
            "detail": detail,
            "scan": result,
            "source": "ibkr-api",
        }, 502

    new_targets = [item for item in (result.get("new_targets") or []) if isinstance(item, dict)]
    if not new_targets:
        return {
            "ok": True,
            "environment": environment,
            "market_date": market_date,
            "job_id": "ibkr_early_expansion_topup",
            "status": "success",
            "skipped": True,
            "reason": "no_new_targets",
            "notified": False,
            "elapsed_s": elapsed_s,
            "detail": _status_detail(
                status="success",
                elapsed_s=elapsed_s,
                scan_payload=scan_payload,
                upstream=upstream,
                result=result,
            ),
            "scan": result,
            "source": "ibkr-api",
        }, 200

    notify_key = _notify_key({"run_id": result.get("run_id")}, result, environment, market_date)
    state = _load_notify_state(get_state_payload, environment)
    if notify_key and notify_key in _notified_keys(state):
        delivered = {
            "notified": False,
            "message_id": "",
            "finalized": True,
            "skipped": True,
            "reason": "already_notified",
            "new_targets": new_targets,
            "new_active": _to_int(result.get("new_active")),
            "new_candidates": _to_int(result.get("new_candidates")),
        }
    else:
        delivered = _deliver_new_targets_notification(
            environment=environment,
            market_date=market_date,
            times=times,
            result=result,
            feishu_send_interactive=feishu_send_interactive,
            write_system_event_record=write_system_event_record,
            config_value=config_value,
            console_base_url=console_base_url,
            startup_chat_id=startup_chat_id,
        )
        if delivered.get("finalized"):
            _record_notify_state(
                upsert_state=upsert_state,
                environment=environment,
                market_date=market_date,
                state=state,
                notify_key=notify_key,
                times=times,
                result=result,
                message_id=_to_text(delivered.get("message_id")),
            )
    if delivered.get("error"):
        return {
            "ok": False,
            "environment": environment,
            "market_date": market_date,
            "job_id": "ibkr_early_expansion_topup",
            "status": "notification_failed",
            "notified": False,
            "message_id": _to_text(delivered.get("message_id")),
            "error": _to_text(delivered.get("error")),
            "new_targets": new_targets,
            "new_active": _to_int(result.get("new_active")),
            "new_candidates": _to_int(result.get("new_candidates")),
            "elapsed_s": elapsed_s,
            "detail": _status_detail(
                status="notification_failed",
                elapsed_s=elapsed_s,
                scan_payload=scan_payload,
                upstream=upstream,
                result=result,
                error=_to_text(delivered.get("error")),
            ),
            "scan": result,
            "source": "ibkr-api",
        }, 502

    notified = bool(delivered.get("notified"))
    message_id = _to_text(delivered.get("message_id"))
    completed_notification = completed_notification or {
        "checked": False,
        "notify_key": notify_key,
        **delivered,
    }
    return {
        "ok": True,
        "environment": environment,
        "market_date": market_date,
        "job_id": "ibkr_early_expansion_topup",
        "status": "success",
        "notified": notified,
        "message_id": message_id,
        "new_targets": new_targets,
        "new_active": _to_int(result.get("new_active")),
        "new_candidates": _to_int(result.get("new_candidates")),
        "elapsed_s": elapsed_s,
        "detail": _status_detail(
            status="success",
            elapsed_s=elapsed_s,
            scan_payload=scan_payload,
            upstream=upstream,
            result=result,
        ),
        "completed_notification": completed_notification,
        "scan": result,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_early_expansion_topup_response"]
