from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 180
WEEKLY_REAUTH_REASON = "weekly_reauth"
WEEKLY_PREAMARKET_HOUR = 4
WEEKLY_PREAMARKET_MINUTE = 0


def parse_et_time_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        else:
            parsed = parsed.astimezone(ET)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=ET)
            return int(parsed.timestamp() * 1000)
        except Exception:
            continue
    return 0


def _format_time(ms: int, tz: ZoneInfo) -> str:
    if int(ms or 0) <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000.0, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def get_weekly_reauth_business_deadline(base_ms: int, *, now_ms: int) -> dict[str, Any]:
    effective_base_ms = int(base_ms or 0) or int(now_ms or 0)
    base_dt = datetime.fromtimestamp(effective_base_ms / 1000.0, tz=ET)
    weekday = base_dt.weekday()
    monday_dt = base_dt + timedelta(days=(1 if weekday == 6 else -weekday))
    deadline_dt = monday_dt.replace(
        hour=WEEKLY_PREAMARKET_HOUR,
        minute=WEEKLY_PREAMARKET_MINUTE,
        second=0,
        microsecond=0,
    )
    deadline_ms = int(deadline_dt.timestamp() * 1000)
    safe_now_ms = int(now_ms or effective_base_ms)
    return {
        "business_deadline_ms": deadline_ms,
        "business_deadline_at": _format_time(deadline_ms, ET),
        "business_deadline_cn": _format_time(deadline_ms, CN),
        "business_deadline_label": "美股周一盘前前完成验证",
        "business_deadline_overdue": safe_now_ms > deadline_ms,
    }


def get_confirm_deadline(triggered_at: Any, *, now_ms: int, confirm_timeout_seconds: int = DEFAULT_CONFIRM_TIMEOUT_SECONDS) -> dict[str, Any]:
    timeout_seconds = max(1, int(confirm_timeout_seconds or DEFAULT_CONFIRM_TIMEOUT_SECONDS))
    triggered_ms = int(triggered_at) if isinstance(triggered_at, (int, float)) else parse_et_time_ms(triggered_at)
    if triggered_ms <= 0:
        return {
            "confirm_window_seconds": timeout_seconds,
            "confirm_deadline_ms": 0,
            "confirm_deadline_at": "",
            "confirm_deadline_cn": "",
            "confirm_deadline_overdue": False,
        }
    deadline_ms = triggered_ms + (timeout_seconds * 1000)
    return {
        "confirm_window_seconds": timeout_seconds,
        "confirm_deadline_ms": deadline_ms,
        "confirm_deadline_at": _format_time(deadline_ms, ET),
        "confirm_deadline_cn": _format_time(deadline_ms, CN),
        "confirm_deadline_overdue": int(now_ms or 0) > deadline_ms,
    }


def derive_two_factor_deadlines(
    state_data: dict[str, Any] | None,
    *,
    now_ms: int | None = None,
    confirm_timeout_seconds: int = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    state = dict(state_data or {})
    current_ms = int(now_ms or 0) or int(datetime.now(tz=ET).timestamp() * 1000)
    requested_ms = parse_et_time_ms(state.get("requested_at"))
    triggered_ms = parse_et_time_ms(state.get("triggered_at"))
    status = str(state.get("status") or "").strip().lower()
    reason = str(state.get("reason") or state.get("recovery_reason") or "").strip().lower()
    payload = {
        "business_deadline_ms": 0,
        "business_deadline_at": "",
        "business_deadline_cn": "",
        "business_deadline_label": "",
        "business_deadline_overdue": False,
        "confirm_window_seconds": max(1, int(confirm_timeout_seconds or DEFAULT_CONFIRM_TIMEOUT_SECONDS)),
        "confirm_deadline_ms": 0,
        "confirm_deadline_at": "",
        "confirm_deadline_cn": "",
        "confirm_deadline_overdue": False,
    }
    if reason == WEEKLY_REAUTH_REASON:
        payload.update(get_weekly_reauth_business_deadline(requested_ms or triggered_ms or current_ms, now_ms=current_ms))
    if status in {"triggered", "waiting_confirm", "waiting_response", "timeout", "failed"}:
        payload.update(
            get_confirm_deadline(
                triggered_ms,
                now_ms=current_ms,
                confirm_timeout_seconds=payload["confirm_window_seconds"],
            )
        )
    return payload


__all__ = [
    "DEFAULT_CONFIRM_TIMEOUT_SECONDS",
    "WEEKLY_REAUTH_REASON",
    "derive_two_factor_deadlines",
    "get_confirm_deadline",
    "get_weekly_reauth_business_deadline",
    "parse_et_time_ms",
]
