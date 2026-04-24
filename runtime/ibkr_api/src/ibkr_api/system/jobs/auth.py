from __future__ import annotations

from .auth_edge_guard import build_auth_edge_guard_response
from .auth_issue import (
    build_auth_immediate_issue,
    build_waiting_response_advice,
    build_waiting_response_issue,
    is_operational_2fa_issue,
    is_waiting_response_issue_kind,
)
from .auth_pending_guard import build_auth_pending_guard_response
from .auth_reminders import (
    _needs_auth_attention,
    build_two_factor_hourly_check_response,
    build_weekly_reauth_followup_response,
    build_weekly_reauth_reminder_response,
)
from .auth_shared import (
    AUTH_EDGE_ALERT_COOLDOWN_MS,
    AUTH_EDGE_MONITOR_STATE_KEY,
    AUTH_MONITOR_STATE_KEY,
    AUTH_PENDING_ALERT_COOLDOWN_MS,
    AUTH_PENDING_ALERT_TRIGGER_MS,
    ET,
    EmitSystemEvent,
    FetchRuntimeStatus,
    GetStatePayload,
    NormalizeEnvironment,
    NormalizeTwoFactorStateWithRuntime,
    RequestTwoFactorApproval,
    TimeStrings,
    UpsertState,
    _as_dict,
    _parse_et_time_ms,
    _to_bool,
    _to_int,
    _to_text,
    load_auth_attention_summary,
)

# Keep legacy private helper names import-compatible while delegating to split modules.
_build_waiting_response_advice = build_waiting_response_advice
_build_waiting_response_issue = build_waiting_response_issue

__all__ = [name for name in globals() if not name.startswith("__")]
