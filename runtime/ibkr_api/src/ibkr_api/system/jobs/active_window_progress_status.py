from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlencode

from ibkr_api.modes import request_broker_mode, request_market_data_mode


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildActiveWindowProgressResponse = Callable[..., tuple[dict[str, Any], int]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
FeishuUpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[..., dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
StartupChatId = Callable[[str], str]


ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY = "ibkr_active_window_progress_card"
JOB_ID = "ibkr_active_window_progress_status"


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


def _call_payload_builder(builder: Callable[..., tuple[dict[str, Any], int]], payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        return builder(payload=payload)
    except TypeError:
        return builder(payload)


def _state_data(record: Any) -> dict[str, Any]:
    payload = _as_dict(record)
    if isinstance(payload.get("data"), dict):
        return dict(payload.get("data") or {})
    return payload


def _load_state(get_state_payload: GetStatePayload, broker_mode: str, market_date: str) -> dict[str, Any]:
    try:
        return _state_data(get_state_payload(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, broker_mode, date=market_date))
    except TypeError:
        try:
            return _state_data(get_state_payload(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, broker_mode, market_date))
        except TypeError:
            return _state_data(get_state_payload(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, broker_mode))
    except Exception:
        return {}


def _save_state(
    upsert_state: UpsertState,
    broker_mode: str,
    market_date: str,
    data: dict[str, Any],
) -> None:
    try:
        upsert_state(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, broker_mode, data, market_date)
    except Exception:
        pass


def _config_enabled(config_value: ConfigValue, broker_mode: str) -> bool:
    try:
        return _truthy(config_value("status_notify_enabled", "TRUE", broker_mode))
    except Exception:
        return True


def _report_url(console_base_url: ConsoleBaseUrl, data_environment: str, market_date: str) -> str:
    try:
        base = _to_text(console_base_url()).rstrip("/")
    except Exception:
        base = ""
    if not base:
        return ""
    query = urlencode(
        {
            "environment": data_environment,
            "tab": "screener",
            "view": "window-progress",
            "date": market_date,
            "market_date": market_date,
        }
    )
    return f"{base}/ibkr_screener.html?{query}"


def _data_badge(data_environment: str) -> str:
    return "Shared Data" if _to_text(data_environment).lower() == "live" else f"Data {_to_text(data_environment).upper()}"


def _format_percent(value: Any) -> str:
    number = _to_float(value, 0.0)
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return f"{number:.0f}%"


def _compact_list(values: Any, *, limit: int = 3) -> str:
    if not isinstance(values, list):
        return "-"
    items = [_to_text(item) for item in values if _to_text(item)]
    if not items:
        return "-"
    visible = items[: max(1, int(limit or 1))]
    suffix = f" +{len(items) - len(visible)}" if len(items) > len(visible) else ""
    return ", ".join(visible) + suffix


def _status_label(item: dict[str, Any]) -> str:
    status = _to_text(item.get("window_status") or item.get("status")).lower()
    labels = {
        "confirmed": "已确认信号",
        "signal_pressure": "接近触发",
        "near_expiry": "窗口临期",
        "upper_active": "上轨窗口",
        "lower_active": "下轨窗口",
        "both_active": "双侧窗口",
        "active": "窗口活跃",
        "blocked": "条件阻塞",
        "no_window": "等待窗口",
    }
    if status in labels:
        return labels[status]
    if bool(item.get("sd_upper_active")) and bool(item.get("sd_lower_active")):
        return "双侧窗口"
    if bool(item.get("sd_upper_active")):
        return "上轨窗口"
    if bool(item.get("sd_lower_active")):
        return "下轨窗口"
    return status or "等待窗口"


def _item_rank(item: dict[str, Any]) -> tuple[int, float, int, float, str]:
    status = _to_text(item.get("window_status") or item.get("status")).lower()
    trace_stage = _to_text(item.get("trace_stage")).lower()
    priority = 6
    if status == "confirmed":
        priority = 0
    elif bool(item.get("candidate_signal")) or trace_stage == "candidate":
        priority = 1
    elif status in {"signal_pressure", "near_expiry"}:
        priority = 2
    elif bool(item.get("sd_upper_active")) or bool(item.get("sd_lower_active")):
        priority = 3
    elif bool(item.get("sd_upper_valid")) or bool(item.get("sd_lower_valid")):
        priority = 4
    elif status == "blocked":
        priority = 5
    progress = _to_float(item.get("component_progress"), 0.0)
    bars_remaining = _to_int(item.get("bars_remaining"), 999)
    if bars_remaining <= 0:
        bars_remaining = 999
    score = _to_float(item.get("target_score") or item.get("score"), 0.0)
    return (priority, -progress, bars_remaining, -score, _to_text(item.get("symbol")))


def _progress_lines(items: list[dict[str, Any]], limit: int = 8) -> list[str]:
    if not items:
        return ["当前暂无 active 标的；等待 08:20 预筛或 09:25 后补充入池。"]
    sorted_items = sorted([_as_dict(item) for item in items], key=_item_rank)
    lines: list[str] = []
    for index, item in enumerate(sorted_items[: max(1, int(limit or 1))], start=1):
        symbol = _to_text(item.get("symbol")) or f"#{index}"
        status = _status_label(item)
        progress = _format_percent(item.get("component_progress"))
        bars_remaining = _to_int(item.get("bars_remaining"), 0)
        remaining_text = f"剩 {bars_remaining} bars" if bars_remaining > 0 else "窗口待刷新"
        latest = _to_text(item.get("latest_us_time")) or "-"
        freshness = item.get("freshness_min")
        freshness_text = f"{_to_int(freshness)}m" if isinstance(freshness, int) else "-"
        collected = _compact_list(item.get("collected_components"), limit=3)
        missing = _compact_list(item.get("missing_components"), limit=3)
        signal_label = _to_text(item.get("candidate_signal_label"))
        signal_suffix = f" | 信号: {signal_label}" if signal_label else ""
        lines.append(
            f"{index}. **{symbol}** | {status} | 进度 {progress} | {remaining_text} | bar {latest} / fresh {freshness_text}{signal_suffix}  \n"
            f"   已满足: {collected}；缺口: {missing}"
        )
    if len(items) > len(lines):
        lines.append(f"... 另有 {len(items) - len(lines)} 个 active 标的在跟踪。")
    return lines


def _combined_summary(progress: dict[str, Any], today_targets: dict[str, Any]) -> dict[str, Any]:
    progress_summary = _as_dict(progress.get("summary"))
    target_summary = _as_dict(today_targets.get("summary"))
    timeline = _as_dict(progress_summary.get("timeline_data"))
    return {
        "total": _to_int(progress_summary.get("total"), _to_int(target_summary.get("total"), 0)),
        "active_count": _to_int(progress_summary.get("active_count"), _to_int(target_summary.get("active_count"), 0)),
        "candidate_count": _to_int(target_summary.get("candidate_count"), _to_int(progress_summary.get("candidate_count"), 0)),
        "with_live_bar_count": _to_int(progress_summary.get("with_live_bar_count"), 0),
        "symbols_with_today_bars_count": _to_int(timeline.get("symbols_with_today_bars_count"), 0),
        "window_active_count": _to_int(progress_summary.get("window_active_count"), 0),
        "window_valid_count": _to_int(progress_summary.get("window_valid_count"), 0),
        "confirmed_count": _to_int(progress_summary.get("confirmed_count"), 0),
        "candidate_signal_count": _to_int(progress_summary.get("candidate_signal_count"), 0),
        "current_candidate_signal_count": _to_int(progress_summary.get("current_candidate_signal_count"), 0),
        "blocked_count": _to_int(progress_summary.get("blocked_count"), 0),
        "near_expiry_count": _to_int(progress_summary.get("near_expiry_count"), 0),
        "trace_error_count": _to_int(progress_summary.get("trace_error_count"), 0),
        "latest_bar_time_max_us": _to_text(timeline.get("latest_bar_time_max_us")),
        "latest_bar_time_min_us": _to_text(timeline.get("latest_bar_time_min_us")),
        "window_status_counts": _as_dict(progress_summary.get("window_status_counts")),
        "trace_stage_counts": _as_dict(progress_summary.get("trace_stage_counts")),
    }


def _card_template(summary: dict[str, Any], items: list[dict[str, Any]]) -> str:
    if _to_int(summary.get("trace_error_count"), 0) > 0:
        return "orange"
    if _to_int(summary.get("confirmed_count"), 0) > 0 or _to_int(summary.get("current_candidate_signal_count"), 0) > 0:
        return "green"
    if _to_int(summary.get("window_active_count"), 0) > 0 or items:
        return "blue"
    return "orange"


def _build_card(
    *,
    broker_mode: str,
    data_environment: str,
    market_date: str,
    times: dict[str, str],
    progress: dict[str, Any],
    today_targets: dict[str, Any],
    console_base_url: ConsoleBaseUrl,
    delivery_note: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    items = [_as_dict(item) for item in (progress.get("items") or []) if isinstance(item, dict)]
    summary = _combined_summary(progress, today_targets)
    latest_bar = _to_text(summary.get("latest_bar_time_max_us")) or "-"
    active_count = _to_int(summary.get("active_count"), 0)
    candidate_count = _to_int(summary.get("candidate_count"), 0)
    live_bars = _to_int(summary.get("with_live_bar_count"), _to_int(summary.get("symbols_with_today_bars_count"), 0))
    status_counts = _as_dict(summary.get("window_status_counts"))
    count_bits = ", ".join(f"{key}:{value}" for key, value in status_counts.items()) or "-"
    header_template = _card_template(summary, items)
    note = delivery_note or "同一张卡每 5 分钟更新；没有信号/订单时也展示等待进度。"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**交易日**: {market_date}\n"
                f"**数据**: {_data_badge(data_environment)} | **Broker**: {broker_mode.upper()}\n"
                f"**检查时间**: 美东 {_to_text(times.get('us')) or _to_text(progress.get('computed_at_us')) or 'n/a'} | 北京 {_to_text(times.get('cn')) or _to_text(progress.get('computed_at_cn')) or 'n/a'}\n"
                f"**标的池**: active {active_count} | candidate {candidate_count} | 新鲜 5m bars {live_bars}\n"
                f"**信号窗口**: active {_to_int(summary.get('window_active_count'))} | valid {_to_int(summary.get('window_valid_count'))} | confirmed {_to_int(summary.get('confirmed_count'))} | candidate {_to_int(summary.get('current_candidate_signal_count'))}\n"
                f"**最新 bar**: {latest_bar}\n"
                f"**状态分布**: {count_bits}\n"
                f"**说明**: {note}"
            ),
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": "**Top 进度**:\n" + "\n".join(_progress_lines(items, limit=8))},
    ]
    url = _report_url(console_base_url, data_environment, market_date)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "查看窗口进度"},
                        "multi_url": {"url": url, "pc_url": url, "ios_url": url, "android_url": url},
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"IBKR 标的/信号窗口动态 · Broker {broker_mode.upper()}"},
            "template": header_template,
        },
        "elements": elements,
    }, summary


def _write_delivery_event(
    *,
    write_system_event_record: WriteSystemEventRecord,
    broker_mode: str,
    data_environment: str,
    market_date: str,
    level: str,
    title: str,
    detail: dict[str, Any],
    notified: bool,
) -> None:
    try:
        write_system_event_record(
            JOB_ID,
            level,
            "ibkr_api",
            title,
            {
                "market_date": market_date,
                "broker_mode": broker_mode,
                "data_environment": data_environment,
                **detail,
            },
            broker_mode,
            notified,
        )
    except Exception:
        pass


def _delivery_success(result: dict[str, Any]) -> bool:
    return bool(result.get("success"))


def _delivery_visible(result: dict[str, Any]) -> bool:
    return _delivery_success(result) and not bool(result.get("suppressed")) and not bool(result.get("skipped"))


def build_active_window_progress_status_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_active_window_progress_response: BuildActiveWindowProgressResponse,
    build_today_targets_response: BuildTodayTargetsResponse,
    feishu_send_interactive: FeishuSendInteractive,
    feishu_update_interactive: FeishuUpdateInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    broker_mode = normalize_environment(request_broker_mode(request_payload), "paper")
    data_environment = request_market_data_mode(request_payload)
    times = time_strings()
    market_date = _to_text(request_payload.get("market_date") or request_payload.get("date") or times.get("date"))
    if not market_date:
        market_date = _to_text(times.get("date"))
    dry_run = _truthy(request_payload.get("dry_run")) if "dry_run" in request_payload else False
    limit = max(1, min(200, _to_int(request_payload.get("limit"), 200)))

    base_payload = {
        **request_payload,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "environment": data_environment,
        "market_date": market_date,
        "date": market_date,
    }
    progress_payload = {
        **base_payload,
        "status": "active",
        "interval": "5m",
        "limit": limit,
    }
    progress, progress_status = _call_payload_builder(build_active_window_progress_response, progress_payload)
    progress = _as_dict(progress)
    if progress_status != 200 or progress.get("ok") is False:
        error = _to_text(progress.get("error")) or f"active_window_progress_http_{progress_status}"
        return {
            "ok": False,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "job_id": JOB_ID,
            "status": "progress_failed",
            "error": error,
            "progress": progress,
            "source": "ibkr-api",
        }, 502

    today_payload = {
        **base_payload,
        "target_status": "active",
        "paginate": False,
        "per_page": limit,
        "sort_by": "attention_asc",
    }
    today_targets, today_status = _call_payload_builder(build_today_targets_response, today_payload)
    today_targets = _as_dict(today_targets)
    if today_status != 200 or today_targets.get("ok") is False:
        today_targets = {
            "ok": False,
            "error": _to_text(today_targets.get("error")) or f"today_targets_http_{today_status}",
            "summary": {},
            "items": [],
        }

    notify_enabled = _config_enabled(config_value, broker_mode)
    delivery_note = "Dry run：只生成卡片，不发送飞书。" if dry_run else (
        "通知开关已关闭：只计算进度，不发送飞书。" if not notify_enabled else ""
    )
    card, summary = _build_card(
        broker_mode=broker_mode,
        data_environment=data_environment,
        market_date=market_date,
        times=times,
        progress=progress,
        today_targets=today_targets,
        console_base_url=console_base_url,
        delivery_note=delivery_note,
    )

    if dry_run:
        return {
            "ok": True,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "job_id": JOB_ID,
            "status": "dry_run",
            "delivered": False,
            "updated": False,
            "sent": False,
            "message_id": "",
            "summary": summary,
            "delivery": {"action": "dry_run", "success": True},
            "card": card,
            "progress": {"summary": _as_dict(progress.get("summary")), "returned_count": _to_int(progress.get("returned_count"))},
            "source": "ibkr-api",
        }, 200

    if not notify_enabled:
        return {
            "ok": True,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "job_id": JOB_ID,
            "status": "notify_disabled",
            "delivered": False,
            "updated": False,
            "sent": False,
            "message_id": "",
            "summary": summary,
            "delivery": {"action": "skipped", "success": True, "reason": "status_notify_disabled"},
            "card": card,
            "progress": {"summary": _as_dict(progress.get("summary")), "returned_count": _to_int(progress.get("returned_count"))},
            "source": "ibkr-api",
        }, 200

    state = _load_state(get_state_payload, broker_mode, market_date)
    previous_message_id = _to_text(state.get("message_id") or state.get("last_message_id"))
    chat_id = _to_text(state.get("chat_id")) or _to_text(startup_chat_id(broker_mode))
    delivery: dict[str, Any]
    message_id = previous_message_id
    sent = False
    updated = False
    status = "updated" if previous_message_id else "sent"
    event_level = ""
    event_title = ""
    event_detail: dict[str, Any] = {}

    if previous_message_id:
        update_result = _as_dict(feishu_update_interactive(previous_message_id, card, broker_mode))
        if _delivery_success(update_result):
            message_id = _to_text(update_result.get("message_id")) or previous_message_id
            updated = _delivery_visible(update_result)
            delivery = {"action": "update", **update_result}
            status = "suppressed" if bool(update_result.get("suppressed")) else "updated"
        else:
            send_result = _as_dict(feishu_send_interactive(card, chat_id, broker_mode))
            delivery = {
                "action": "update_failed_fallback_send",
                "update": update_result,
                "fallback_send": send_result,
                "success": _delivery_success(send_result),
                "error": "" if _delivery_success(send_result) else (_to_text(send_result.get("error")) or "fallback_send_failed"),
            }
            if _delivery_success(send_result):
                message_id = _to_text(send_result.get("message_id")) or ""
                sent = _delivery_visible(send_result) and bool(message_id)
                if sent:
                    status = "fallback_sent"
                    event_level = "warning"
                    event_title = "IBKR 标的/信号窗口动态卡更新失败后已重发"
                    event_detail = {
                        "old_message_id": previous_message_id,
                        "message_id": message_id,
                        "update_error": _to_text(update_result.get("error")) or "update_failed",
                    }
                elif bool(send_result.get("suppressed")) or bool(send_result.get("skipped")):
                    status = "suppressed"
                else:
                    delivery["success"] = False
                    delivery["error"] = "missing_message_id"
                    status = "delivery_failed"
                    event_level = "error"
                    event_title = "IBKR 标的/信号窗口动态卡发送失败"
                    event_detail = {
                        "message_id": previous_message_id,
                        "update_error": _to_text(update_result.get("error")) or "update_failed",
                        "send_error": "missing_message_id",
                    }
            else:
                message_id = previous_message_id
                status = "delivery_failed"
                event_level = "error"
                event_title = "IBKR 标的/信号窗口动态卡发送失败"
                event_detail = {
                    "message_id": previous_message_id,
                    "update_error": _to_text(update_result.get("error")) or "update_failed",
                    "send_error": _to_text(send_result.get("error")) or "send_failed",
                }
    else:
        send_result = _as_dict(feishu_send_interactive(card, chat_id, broker_mode))
        message_id = _to_text(send_result.get("message_id")) or ""
        sent = _delivery_visible(send_result) and bool(message_id)
        delivery = {"action": "send", **send_result}
        if _delivery_success(send_result) and sent:
            status = "sent"
            event_level = "info"
            event_title = "IBKR 标的/信号窗口动态卡已创建"
            event_detail = {"message_id": message_id, "chat_id": chat_id}
        elif _delivery_success(send_result) and (bool(send_result.get("suppressed")) or bool(send_result.get("skipped"))):
            status = "suppressed"
        elif _delivery_success(send_result):
            delivery["success"] = False
            delivery["error"] = "missing_message_id"
            status = "delivery_failed"
            event_level = "error"
            event_title = "IBKR 标的/信号窗口动态卡发送失败"
            event_detail = {"send_error": "missing_message_id", "chat_id": chat_id}
        else:
            status = "delivery_failed"
            event_level = "error"
            event_title = "IBKR 标的/信号窗口动态卡发送失败"
            event_detail = {"send_error": _to_text(send_result.get("error")) or "send_failed", "chat_id": chat_id}

    ok = status != "delivery_failed"
    delivered = sent or updated
    state_patch = {
        **state,
        "version": 1,
        "market_date": market_date,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "chat_id": chat_id,
        "message_id": message_id,
        "last_message_id": message_id,
        "last_delivery_mode": _to_text(delivery.get("action")),
        "last_delivery_status": status,
        "last_delivery_at": _to_text(times.get("us")),
        "last_delivery_error": _to_text(delivery.get("error")),
        "last_summary": summary,
        "last_progress_returned_count": _to_int(progress.get("returned_count")),
    }
    if sent and not _to_text(state_patch.get("first_sent_at")):
        state_patch["first_sent_at"] = _to_text(times.get("us"))
    _save_state(upsert_state, broker_mode, market_date, state_patch)

    if event_level and event_title:
        _write_delivery_event(
            write_system_event_record=write_system_event_record,
            broker_mode=broker_mode,
            data_environment=data_environment,
            market_date=market_date,
            level=event_level,
            title=event_title,
            detail={**event_detail, "status": status, "summary": summary},
            notified=delivered,
        )

    return {
        "ok": ok,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "market_date": market_date,
        "job_id": JOB_ID,
        "status": status,
        "delivered": delivered,
        "updated": updated,
        "sent": sent,
        "notified": delivered,
        "message_id": message_id,
        "summary": summary,
        "delivery": delivery,
        "card": card,
        "progress": {"summary": _as_dict(progress.get("summary")), "returned_count": _to_int(progress.get("returned_count"))},
        "source": "ibkr-api",
    }, (200 if ok else 502)


__all__ = ["ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY", "build_active_window_progress_status_response"]
