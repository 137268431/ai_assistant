from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from ibkr_compute.core.time_utils import ET


IBKR_DAILY_RESET_START_MINUTE = 15
IBKR_DAILY_RESET_END_MINUTE = 105
DEFAULT_IBC_AUTO_RESTART_TIME = "02:35 AM"
DEFAULT_IBC_AUTO_RESTART_WINDOW_MINUTES = 10
EPOCH_MS_ERROR_CODE_FLOOR = 1_000_000_000_000


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


def _looks_like_epoch_ms(value: Any) -> bool:
    return _safe_int(value) >= EPOCH_MS_ERROR_CODE_FLOOR


def _looks_like_ib_error_code(value: Any) -> bool:
    number = abs(_safe_int(value))
    return 0 < number < 1_000_000


def _normalize_error_code_and_message(code_value: Any, message_value: Any = "") -> tuple[int, str]:
    code = _safe_int(code_value)
    message = str(message_value or "")
    if _looks_like_epoch_ms(code) and _looks_like_ib_error_code(message):
        return _safe_int(message), ""
    if _looks_like_epoch_ms(code):
        return 0, message
    return code, message


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _coerce_et(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(ET)
    if value.tzinfo is None:
        return value.replace(tzinfo=ET)
    return value.astimezone(ET)


def in_ibkr_daily_reset_window(value: datetime | None = None) -> bool:
    dt = _coerce_et(value)
    minute = dt.hour * 60 + dt.minute
    return IBKR_DAILY_RESET_START_MINUTE <= minute < IBKR_DAILY_RESET_END_MINUTE


def _parse_time_to_minute(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    pieces = text.upper().replace(".", "").split()
    time_part = pieces[0] if pieces else ""
    suffix = pieces[1] if len(pieces) > 1 else ""
    if time_part.endswith(("AM", "PM")):
        suffix = time_part[-2:]
        time_part = time_part[:-2]
    if ":" in time_part:
        hour_text, minute_text = time_part.split(":", 1)
    else:
        hour_text, minute_text = time_part, "0"
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return None
    if minute < 0 or minute > 59:
        return None
    if suffix in {"AM", "PM"}:
        if hour < 1 or hour > 12:
            return None
        if suffix == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif hour < 0 or hour > 23:
        return None
    return hour * 60 + minute


def _format_minute(minute: int) -> str:
    value = int(minute or 0) % (24 * 60)
    hour = value // 60
    minute_part = value % 60
    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12 or 12
    return f"{hour_12:02d}:{minute_part:02d} {suffix}"


def _circular_minute_distance(left: int, right: int) -> int:
    distance = abs((int(left) % 1440) - (int(right) % 1440))
    return min(distance, 1440 - distance)


def _read_ibc_config_auto_restart_time(path: str) -> str:
    candidate = str(path or "").strip()
    if not candidate:
        return ""
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "AutoRestartTime":
                    return value.strip()
    except Exception:
        return ""
    return ""


def _first_restart_time_value(*payloads: dict[str, Any]) -> str:
    keys = (
        "ibc_auto_restart_time",
        "auto_restart_time",
        "ibkr_auto_restart_time",
        "AutoRestartTime",
        "IBKR_AUTO_RESTART_TIME",
    )
    for payload in payloads:
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        ibc = _as_dict(payload.get("ibc"))
        for key in keys:
            value = str(ibc.get(key) or "").strip()
            if value:
                return value
    config_value = _read_ibc_config_auto_restart_time(
        os.environ.get("IBKR_IBC_INI", "").strip() or "/opt/ibc/config.ini"
    )
    if config_value:
        return config_value
    env_value = os.environ.get("IBKR_AUTO_RESTART_TIME", "").strip()
    return env_value or DEFAULT_IBC_AUTO_RESTART_TIME


def _ibc_auto_restart_window(
    now: datetime | None,
    *payloads: dict[str, Any],
) -> dict[str, Any]:
    configured_time = _first_restart_time_value(*payloads)
    configured_minute = _parse_time_to_minute(configured_time)
    window_minutes = max(
        1,
        min(120, _safe_int(os.environ.get("IBKR_AUTO_RESTART_WINDOW_MINUTES"), DEFAULT_IBC_AUTO_RESTART_WINDOW_MINUTES)),
    )
    if configured_minute is None:
        return {
            "configured_time": configured_time,
            "configured_minute": None,
            "window_minutes": window_minutes,
            "window_et": "",
            "in_window": False,
        }
    current_minute = _coerce_et(now).hour * 60 + _coerce_et(now).minute
    return {
        "configured_time": configured_time,
        "configured_minute": configured_minute,
        "window_minutes": window_minutes,
        "window_et": (
            f"{_format_minute(configured_minute - window_minutes)}-"
            f"{_format_minute(configured_minute + window_minutes)}"
        ),
        "in_window": _circular_minute_distance(current_minute, configured_minute) <= window_minutes,
    }


def _collect_recent_errors(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen = set()
    for payload in payloads:
        for raw in _as_list(payload.get("recent_errors")):
            item = _as_dict(raw)
            code, message = _normalize_error_code_and_message(
                item.get("code") or item.get("error_code"),
                item.get("message") or item.get("error") or "",
            )
            if not code:
                continue
            normalized = {
                "code": code,
                "message": message,
                "req_id": _safe_int(item.get("req_id"), 0),
                "at": str(item.get("at") or ""),
            }
            key = (normalized["code"], normalized["message"], normalized["at"])
            if key not in seen:
                seen.add(key)
                items.append(normalized)
        code, message = _normalize_error_code_and_message(
            payload.get("last_error_code"),
            payload.get("last_error") or "",
        )
        if code:
            last_message = message
            if any(int(item.get("code") or 0) == code and not last_message for item in items):
                continue
            normalized = {
                "code": code,
                "message": last_message,
                "req_id": 0,
                "at": str(payload.get("last_error_at") or ""),
            }
            key = (normalized["code"], normalized["message"], normalized["at"])
            if key not in seen:
                seen.add(key)
                items.append(normalized)
    return items[-20:]


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_int(*values: Any) -> int:
    for value in values:
        number = _safe_int(value)
        if number:
            return number
    return 0


def _bool_from_payload(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    return bool(payload.get(key))


def _format_recent_errors(errors: list[dict[str, Any]]) -> str:
    parts = []
    for item in errors[-5:]:
        code = _safe_int(item.get("code"))
        message = str(item.get("message") or "").strip().replace("\n", " ")
        if code and message:
            parts.append(f"{code}: {message}")
        elif code:
            parts.append(str(code))
    return " | ".join(parts)


def _socket_unreachable_like(
    *,
    status_code: int,
    last_error_code: int,
    last_error: str,
    error_code_set: set[int],
    gateway: dict[str, Any],
) -> bool:
    if status_code in {502, 504}:
        return True
    if last_error_code in {502, 504} or error_code_set.intersection({502, 504}):
        return True
    if gateway.get("api_socket_listening") is False:
        return True
    if status_code == 503 and _safe_int(last_error) in {502, 504}:
        return True
    normalized_error = str(last_error or "").lower()
    return any(
        marker in normalized_error
        for marker in (
            "couldn't connect to tws",
            "could not connect to tws",
            "connect to tws",
            "connection refused",
            "port_not_listening",
        )
    )


def _build_result(
    *,
    reason_code: str,
    reason_label: str,
    level: str,
    confidence: str,
    title: str,
    summary: str,
    recommendation: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "reason_label": reason_label,
        "level": level,
        "confidence": confidence,
        "title": title,
        "summary": summary,
        "recommendation": recommendation,
        "evidence": evidence,
    }


def classify_ibkr_disconnect(
    snapshot: dict[str, Any] | None = None,
    *,
    gateway_status: dict[str, Any] | None = None,
    interruption_kind: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    session = _as_dict(snapshot)
    gateway = _as_dict(gateway_status)
    broker = _as_dict(gateway.get("broker"))
    if not broker:
        broker = _as_dict(session.get("broker"))

    recent_errors = _collect_recent_errors(session, broker, gateway)
    error_codes = [int(item.get("code") or 0) for item in recent_errors if _safe_int(item.get("code"))]
    error_code_set = set(error_codes)
    status_code = _first_int(session.get("status_code"), broker.get("status_code"), gateway.get("status_code"))
    last_error_code = error_codes[-1] if error_codes else _first_int(
        session.get("last_error_code"),
        broker.get("last_error_code"),
        gateway.get("last_error_code"),
    )
    last_error = _first_text(
        session.get("last_error"),
        broker.get("last_error"),
        gateway.get("last_error"),
        session.get("error"),
    )
    reset_window = in_ibkr_daily_reset_window(now)
    auto_restart = _ibc_auto_restart_window(now, session, broker, gateway)
    gateway_running = _bool_from_payload(session, "gateway_running")
    if gateway_running is None:
        gateway_running = _bool_from_payload(session, "running")
    if gateway_running is None:
        gateway_running = _bool_from_payload(gateway, "running")
    if gateway_running is None:
        gateway_running = True

    evidence = {
        "interruption_kind": str(interruption_kind or ""),
        "status_code": status_code,
        "last_error_code": last_error_code,
        "last_error": last_error,
        "error_codes": error_codes,
        "recent_errors": recent_errors[-5:],
        "recent_errors_text": _format_recent_errors(recent_errors),
        "gateway_running": bool(gateway_running),
        "gateway_active_state": str(gateway.get("active_state") or ""),
        "gateway_sub_state": str(gateway.get("sub_state") or ""),
        "api_socket_listening": bool(gateway.get("api_socket_listening", True)),
        "api_socket_host": str(gateway.get("api_socket_host") or ""),
        "api_socket_port": _safe_int(gateway.get("api_socket_port")),
        "api_socket_reason": str(gateway.get("api_socket_reason") or ""),
        "in_daily_reset_window": bool(reset_window),
        "reset_window_et": "00:15-01:45",
        "in_ibc_auto_restart_window": bool(auto_restart.get("in_window")),
        "ibc_auto_restart_time": str(auto_restart.get("configured_time") or ""),
        "ibc_auto_restart_window_et": str(auto_restart.get("window_et") or ""),
        "ibc_auto_restart_window_minutes": _safe_int(auto_restart.get("window_minutes")),
    }

    if not gateway_running:
        return _build_result(
            reason_code="local_gateway_down",
            reason_label="本地 IB Gateway 服务不可用",
            level="error",
            confidence="high",
            title="IBKR Gateway 本地服务不可用，已触发恢复",
            summary="检测到本地 IB Gateway systemd 服务不在运行态，属于本机运行时/Gateway 故障。",
            recommendation="检查 ibkr-gateway 服务、IBC/Gateway 日志和主机资源；系统会尝试重启 Gateway 并进入恢复探测。",
            evidence=evidence,
        )

    if error_code_set.intersection({1100, 2110}) and reset_window:
        return _build_result(
            reason_code="ibkr_daily_reset",
            reason_label="IBKR 官方每日 reset 窗口短暂断线",
            level="info",
            confidence="high",
            title="IBKR 每日 reset 短暂断线，正在静默恢复",
            summary="检测到 1100/2110 连接断开码，且发生在 IBKR North America 每日 reset 窗口内，通常是官方维护造成的短暂断线。",
            recommendation="无需手动 2FA；等待系统静默恢复即可。若超过恢复窗口仍未认证，再去 Runtime 页面人工接管。",
            evidence=evidence,
        )

    if error_code_set.intersection({1100, 2110}):
        return _build_result(
            reason_code="ibkr_upstream_disconnect",
            reason_label="IBKR 上游连接中断",
            level="warning",
            confidence="medium",
            title="IBKR 上游连接中断，正在静默恢复",
            summary="检测到 1100/2110 连接断开码，但不在每日 reset 窗口内，可能是 IBKR 上游、网络或 Gateway 连接抖动。",
            recommendation="先观察静默恢复；若持续未恢复，检查主机网络、IBKR system status 和 Gateway 日志。",
            evidence=evidence,
        )

    socket_unreachable = _socket_unreachable_like(
        status_code=status_code,
        last_error_code=last_error_code,
        last_error=last_error,
        error_code_set=error_code_set,
        gateway=gateway,
    )

    if socket_unreachable and auto_restart.get("in_window"):
        return _build_result(
            reason_code="scheduled_gateway_restart",
            reason_label="IBC/Gateway 计划自动重启窗口",
            level="info",
            confidence="high",
            title="IBKR Gateway 计划自动重启窗口，正在静默恢复",
            summary="检测到 Gateway API 短暂不可达，且发生在 IBC AutoRestartTime 配置窗口内，通常是计划内自动重启造成。",
            recommendation="无需手动 2FA；等待静默探测恢复。若超过 2 分钟仍未认证，再检查 Gateway 日志和 2FA 状态。",
            evidence=evidence,
        )

    if socket_unreachable:
        return _build_result(
            reason_code="local_socket_unreachable",
            reason_label="本地 Gateway Socket 不可达",
            level="warning",
            confidence="high",
            title="IBKR Gateway Socket 不可达，正在恢复",
            summary="检测到 502/504 或 Gateway API 端口未监听，本地 API 客户端暂时无法连到 Gateway socket。",
            recommendation="检查 Gateway API 端口、服务监听、IBC 启动状态和防火墙；若长期未监听，请重启 Gateway 后重新开始登录/认证周期。",
            evidence=evidence,
        )

    if error_code_set == {326} or (last_error_code == 326 and not error_code_set.intersection({1100, 2110})):
        return _build_result(
            reason_code="client_id_conflict",
            reason_label="IB API clientId 冲突",
            level="warning",
            confidence="high",
            title="IBKR API clientId 冲突，正在恢复",
            summary="检测到 326，说明当前 clientId 已被另一个连接占用或旧连接尚未释放。",
            recommendation="等待旧连接释放或检查是否有重复 runtime/compute 进程；必要时重启对应服务。",
            evidence=evidence,
        )

    if status_code == 401:
        return _build_result(
            reason_code="auth_not_ready",
            reason_label="Gateway 已运行但 API 尚未 ready",
            level="warning",
            confidence="medium",
            title="IBKR API 尚未 ready，正在静默恢复",
            summary="Gateway 服务仍在运行，但 API ready 探测未通过；可能是登录/认证状态尚未完成或连接刚恢复。",
            recommendation="等待静默探测；若长时间停留在 401，再检查 Gateway 登录窗口或手动接管 2FA。",
            evidence=evidence,
        )

    if session.get("error"):
        return _build_result(
            reason_code="probe_failed",
            reason_label="Session 探测异常",
            level="warning",
            confidence="medium",
            title="IBKR Session 探测异常，正在静默恢复",
            summary="Session 探测过程抛出异常，当前无法确认是否为 Gateway、网络或认证问题。",
            recommendation="查看 runtime 日志中的探测异常；系统会继续静默探测并尝试恢复。",
            evidence=evidence,
        )

    return _build_result(
        reason_code="unknown",
        reason_label="未知断线原因",
        level="warning",
        confidence="low",
        title="IBKR 会话状态异常，正在静默恢复",
        summary="当前断线没有足够错误码可归因，系统已进入静默探测与本地重连窗口。",
        recommendation="先观察静默恢复；若持续未恢复，再检查 Gateway、网络和 2FA 状态。",
        evidence=evidence,
    )


def classification_detail_fields(classification: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(classification)
    evidence = _as_dict(payload.get("evidence"))
    error_codes = evidence.get("error_codes")
    if isinstance(error_codes, list):
        codes_text = ",".join(str(_safe_int(item)) for item in error_codes if _safe_int(item))
    else:
        codes_text = ""
    return {
        "断线归因": str(payload.get("reason_label") or ""),
        "归因代码": str(payload.get("reason_code") or ""),
        "归因置信度": str(payload.get("confidence") or ""),
        "IB错误码": codes_text or str(evidence.get("last_error_code") or ""),
        "IB状态码": str(evidence.get("status_code") or ""),
        "原始错误": str(evidence.get("last_error") or ""),
        "Reset窗口": "yes" if evidence.get("in_daily_reset_window") else "no",
        "IBC自动重启窗口": "yes" if evidence.get("in_ibc_auto_restart_window") else "no",
        "IBC自动重启时间": str(evidence.get("ibc_auto_restart_time") or ""),
        "最近错误": str(evidence.get("recent_errors_text") or ""),
        "API端口监听": "yes" if evidence.get("api_socket_listening") else "no",
        "API端口": str(evidence.get("api_socket_port") or ""),
    }
