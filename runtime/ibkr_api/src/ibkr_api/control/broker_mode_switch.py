from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode

from ibkr_api.account.snapshot import build_account_snapshot_response
from ibkr_api.orders.values import ensure_object, to_float, to_int, to_text


NormalizeEnvironment = Callable[[Any, str], str]
RequestJsonRequest = Callable[..., dict[str, Any]]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]
GetStatePayload = Callable[..., dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
SystemctlAction = Callable[[str, str], dict[str, Any]]
ScheduleApiRestart = Callable[[str], dict[str, Any]]


DEFAULT_IBC_CONFIG_PATH = "/opt/ibc/config.ini"
DEFAULT_MODE_SWITCH_ENV_FILES = (
    "/opt/ibkr_runtime/.env",
    "/opt/ibkr_compute/.env",
    "/opt/ibkr_api/.env",
    "/opt/ibkr_scheduler/.env",
    "/opt/ibkr_backtest/.env",
)
MODE_SWITCH_RESTART_PLAN = (
    {"service": "ibkr-runtime", "action": "stop", "label": "stop runtime before broker switch"},
    {"service": "ibkr-gateway", "action": "restart", "label": "restart IB Gateway in target mode"},
    {"service": "ibkr-runtime", "action": "restart", "label": "restart runtime service"},
    {"service": "ibkr-compute", "action": "restart", "label": "restart compute service"},
    {"service": "ibkr-scheduler", "action": "restart", "label": "restart scheduler service"},
    {"service": "ibkr-api", "action": "restart", "label": "restart API service after response"},
)

_SWITCH_LOCK = threading.Lock()


def _split_env_file_override(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").replace("\n", ",").split(",") if item.strip()]


def resolve_mode_switch_env_files(env_files: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if env_files is not None:
        return [str(item).strip() for item in env_files if str(item).strip()]
    override = _split_env_file_override(os.environ.get("IBKR_MODE_SWITCH_ENV_FILES", ""))
    return override or list(DEFAULT_MODE_SWITCH_ENV_FILES)


def resolve_mode_switch_ibc_config_path(env_values: dict[str, str] | None = None, ibc_config_path: str | None = None) -> str:
    if ibc_config_path is not None and str(ibc_config_path).strip():
        return str(ibc_config_path).strip()
    override = str(os.environ.get("IBKR_MODE_SWITCH_IBC_CONFIG") or "").strip()
    if override:
        return override
    values = env_values if isinstance(env_values, dict) else {}
    return str(values.get("IBKR_IBC_INI") or os.environ.get("IBKR_IBC_INI") or DEFAULT_IBC_CONFIG_PATH).strip()


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def _load_env_files(env_files: list[str] | tuple[str, ...] | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    files: list[dict[str, Any]] = []
    merged: dict[str, str] = {}
    for file_path in resolve_mode_switch_env_files(env_files):
        path = Path(file_path)
        exists = path.exists()
        values: dict[str, str] = {}
        if exists and path.is_file():
            try:
                text = path.read_text()
                values = _parse_env_text(text)
                for key, value in values.items():
                    if key not in merged:
                        merged[key] = value
            except Exception as exc:
                files.append({"path": str(path), "exists": True, "readable": False, "error": str(exc)})
                continue
        files.append(
            {
                "path": str(path),
                "exists": exists,
                "readable": bool(exists and path.is_file()),
                "has_broker_mode": "IBKR_BROKER_MODE" in values,
                "has_gateway_mode": "IBKR_GATEWAY_MODE" in values,
                "has_live_account": bool(values.get("IBKR_ACCOUNT_ID")),
                "has_paper_account": bool(values.get("IBKR_PAPER_ACCOUNT_ID")),
            }
        )
    for key in (
        "IBKR_ACCOUNT_ID",
        "IBKR_PAPER_ACCOUNT_ID",
        "IBKR_BROKER_MODE",
        "IBKR_GATEWAY_MODE",
        "IBKR_LIVE_USERNAME",
        "IBKR_LIVE_PASSWORD",
        "IBKR_PAPER_USERNAME",
        "IBKR_PAPER_PASSWORD",
        "IBKR_USERNAME",
        "IBKR_PASSWORD",
        "IBKR_IBC_INI",
        "IBGW_PORT",
    ):
        if key not in merged and os.environ.get(key):
            merged[key] = str(os.environ.get(key) or "").strip()
    return files, merged


def _mask_account_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    if text.startswith("DU") and len(text) > 6:
        return f"DU****{text[-4:]}"
    if text.startswith("U") and len(text) > 5:
        return f"U****{text[-4:]}"
    return f"{text[:2]}****{text[-4:]}"


def _mask_login_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def _target_account_key(target_broker_mode: str) -> str:
    return "IBKR_PAPER_ACCOUNT_ID" if target_broker_mode == "paper" else "IBKR_ACCOUNT_ID"


def _current_account_key(current_broker_mode: str) -> str:
    return _target_account_key(current_broker_mode)


def _normalize_requested_target(payload: dict[str, Any], current_broker_mode: str) -> str:
    requested = payload.get("target_broker_mode") or payload.get("target") or payload.get("broker_mode_target")
    if requested is None or str(requested).strip() == "":
        requested = "live" if current_broker_mode == "paper" else "paper"
    return normalize_broker_mode(requested, "paper")


def _first_env_value(env_values: dict[str, str], keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        value = str(env_values.get(key) or "").strip()
        if value:
            return value, key
    return "", ""


def _broker_mode_or_empty(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"live", "paper"} else ""


def resolve_gateway_credentials(target_broker_mode: str, env_values: dict[str, str]) -> dict[str, Any]:
    normalized_target = normalize_broker_mode(target_broker_mode, "paper")
    if normalized_target == "live":
        username, username_key = _first_env_value(env_values, ("IBKR_LIVE_USERNAME",))
        username_required_key = "IBKR_LIVE_USERNAME"
        username_legacy_available = bool(str(env_values.get("IBKR_USERNAME") or "").strip())
        password, password_key = _first_env_value(env_values, ("IBKR_LIVE_PASSWORD", "IBKR_PASSWORD"))
    else:
        username, username_key = _first_env_value(env_values, ("IBKR_PAPER_USERNAME", "IBKR_USERNAME"))
        username_required_key = "IBKR_PAPER_USERNAME or IBKR_USERNAME"
        username_legacy_available = False
        password, password_key = _first_env_value(env_values, ("IBKR_PAPER_PASSWORD", "IBKR_PASSWORD"))
    return {
        "broker_mode": normalized_target,
        "username": username,
        "username_key": username_key,
        "username_required_key": username_required_key,
        "username_present": bool(username),
        "username_legacy_available": username_legacy_available,
        "username_masked": _mask_login_id(username),
        "password": password,
        "password_key": password_key,
        "password_present": bool(password),
    }


def _load_key_value_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    return _parse_env_text(path.read_text())


def inspect_gateway_config(
    target_broker_mode: str,
    *,
    env_values: dict[str, str],
    credentials: dict[str, Any],
    ibc_config_path: str | None = None,
) -> dict[str, Any]:
    normalized_target = normalize_broker_mode(target_broker_mode, "paper")
    path = Path(resolve_mode_switch_ibc_config_path(env_values, ibc_config_path))
    exists = path.exists()
    readable = bool(exists and path.is_file())
    values: dict[str, str] = {}
    error = ""
    if readable:
        try:
            values = _load_key_value_file(path)
        except Exception as exc:
            readable = False
            error = str(exc)
    writable = bool(readable and os.access(path, os.W_OK) and os.access(path.parent, os.W_OK))
    api_port = str(env_values.get("IBGW_PORT") or values.get("OverrideTwsApiPort") or "4001").strip() or "4001"
    current_login = str(values.get("IbLoginId") or "").strip()
    current_mode = _broker_mode_or_empty(values.get("TradingMode"))
    target_login = str(credentials.get("username") or "").strip()
    return {
        "path": str(path),
        "exists": bool(exists),
        "readable": bool(readable),
        "writable": bool(writable),
        "error": error,
        "current_login_id_masked": _mask_login_id(current_login),
        "current_trading_mode": current_mode,
        "target_login_key": str(credentials.get("username_key") or credentials.get("username_required_key") or ""),
        "target_login_present": bool(target_login),
        "target_login_masked": _mask_login_id(target_login),
        "target_password_key": str(credentials.get("password_key") or "IBKR_PASSWORD"),
        "target_password_present": bool(credentials.get("password_present")),
        "target_trading_mode": normalized_target,
        "api_port": api_port,
        "login_matches_target": bool(current_login and target_login and current_login == target_login),
        "trading_mode_matches_target": bool(current_mode and current_mode == normalized_target),
    }


def _runtime_context(
    payload: dict[str, Any],
    *,
    fetch_runtime_status: FetchRuntimeStatus,
    as_dict: AsDict,
) -> dict[str, Any]:
    requested_environment = normalize_broker_mode(
        payload.get("broker_mode") or payload.get("environment") or configured_broker_mode(),
        configured_broker_mode(),
    )
    runtime_result: dict[str, Any] = {}
    runtime_payload: dict[str, Any] = {}
    try:
        runtime_result = fetch_runtime_status(requested_environment)
        runtime_payload = as_dict(runtime_result.get("payload"))
    except Exception as exc:
        runtime_result = {"ok": False, "error": str(exc)}
        runtime_payload = {}
    actual_runtime_environment = normalize_broker_mode(
        runtime_payload.get("broker_mode") or runtime_payload.get("environment") or requested_environment,
        requested_environment,
    )
    return {
        "requested_broker_mode": requested_environment,
        "current_broker_mode": actual_runtime_environment,
        "runtime_payload": runtime_payload,
        "runtime_result": runtime_result,
        "runtime_status_error": str(runtime_result.get("error") or ""),
    }


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _two_factor_active_state(
    current_broker_mode: str,
    *,
    get_state_payload: GetStatePayload | None,
    as_dict: AsDict,
    ibkr_2fa_state_key: str,
    ibkr_2fa_state_date: str,
) -> dict[str, Any]:
    if not callable(get_state_payload):
        return {"active": False, "status": "", "reason": "state_unavailable"}
    try:
        payload = get_state_payload(ibkr_2fa_state_key, current_broker_mode, date=ibkr_2fa_state_date)
        state = as_dict(payload.get("data"))
    except Exception as exc:
        return {"active": True, "status": "unknown", "reason": f"2fa_state_read_failed:{exc}"}

    status = str(state.get("status") or "").strip().lower()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    has_cycle = any(str(state.get(key) or "").strip() for key in ("cycle_id", "challenge_code", "startup_label", "last_request_at"))
    active_status = status in {"requested", "pending", "waiting", "waiting_response", "responded", "submitted", "running"}
    active_recovery = recovery_phase.startswith("waiting") or recovery_phase in {"requested", "challenge", "response_pending", "manual_confirm"}
    active = bool(
        _is_truthy(state.get("manual_takeover_active"))
        or _is_truthy(state.get("auto_restart_scheduled"))
        or (has_cycle and (active_status or active_recovery))
    )
    return {
        "active": active,
        "status": status,
        "recovery_phase": recovery_phase,
        "cycle_id": str(state.get("cycle_id") or ""),
        "challenge_code": str(state.get("challenge_code") or ""),
    }


def summarize_account_switch_risk(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = ensure_object(snapshot)
    counts = ensure_object(payload.get("counts"))
    positions = [ensure_object(item) for item in (payload.get("positions") or []) if isinstance(item, dict)]
    open_positions = [item for item in positions if abs(to_float(item.get("quantity")) or 0.0) > 0]
    live_open_orders = [ensure_object(item) for item in (payload.get("live_open_orders") or []) if isinstance(item, dict)]

    open_positions_count = to_int(counts.get("open_positions"), 0)
    if open_positions_count <= 0:
        open_positions_count = len(open_positions)
    open_orders_count = to_int(counts.get("open_orders"), 0)
    if open_orders_count <= 0:
        open_orders_count = len(live_open_orders)

    pb_active_order_groups = to_int(counts.get("pb_active_order_groups"), 0)
    stale_pb_order_groups = to_int(counts.get("stale_pb_order_groups"), 0)
    pb_only_active_order_groups = to_int(counts.get("pb_only_active_order_groups"), 0)
    pb_shadow_groups = to_int(counts.get("pb_shadow_groups"), 0)
    pb_risk_groups = max(stale_pb_order_groups, pb_only_active_order_groups, pb_shadow_groups)
    total_pb_blocking_groups = pb_active_order_groups + pb_risk_groups

    return {
        "open_positions": open_positions_count,
        "open_orders": open_orders_count,
        "pb_active_order_groups": pb_active_order_groups,
        "stale_pb_order_groups": stale_pb_order_groups,
        "pb_only_active_order_groups": pb_only_active_order_groups,
        "pb_shadow_groups": pb_shadow_groups,
        "pb_blocking_groups": total_pb_blocking_groups,
        "account_id": str(payload.get("account_id") or ensure_object(payload.get("summary")).get("account_code") or ""),
        "session_authenticated": bool(payload.get("session_authenticated")),
    }


def _load_account_snapshot(
    pb: Any,
    current_broker_mode: str,
    *,
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
) -> tuple[dict[str, Any], int]:
    return build_account_snapshot_response(
        pb,
        payload={"broker_mode": current_broker_mode, "environment": current_broker_mode, "include_pnl": "0"},
        normalize_environment=normalize_environment,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
    )


def _build_blockers(
    *,
    target_broker_mode: str,
    switch_required: bool,
    account_snapshot: dict[str, Any],
    account_status_code: int,
    account_risk: dict[str, Any],
    target_account_id: str,
    two_factor_state: dict[str, Any],
    gateway_credentials: dict[str, Any] | None = None,
    gateway_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if not switch_required:
        return blockers
    credentials = ensure_object(gateway_credentials)
    config = ensure_object(gateway_config)
    if not target_account_id:
        blockers.append(
            {
                "code": "target_account_missing",
                "message": f"目标 {target_broker_mode.upper()} 账户 ID 未配置。",
                "severity": "blocker",
            }
        )
    if not bool(credentials.get("username_present")):
        detail = "已检测到 legacy IBKR_USERNAME，但 live 切换需要显式 IBKR_LIVE_USERNAME。" if bool(credentials.get("username_legacy_available")) else ""
        blockers.append(
            {
                "code": "target_login_missing",
                "message": f"目标 {target_broker_mode.upper()} Gateway 登录名未配置。",
                "severity": "blocker",
                "required_key": str(credentials.get("username_required_key") or ""),
                "detail": detail,
            }
        )
    if not bool(credentials.get("password_present")):
        blockers.append(
            {
                "code": "target_password_missing",
                "message": f"目标 {target_broker_mode.upper()} Gateway 登录密码未配置。",
                "severity": "blocker",
                "required_key": f"IBKR_{target_broker_mode.upper()}_PASSWORD or IBKR_PASSWORD",
            }
        )
    if not bool(config.get("exists")):
        blockers.append(
            {
                "code": "ibc_config_missing",
                "message": "IBC config.ini 不存在，不能安全切换 Gateway 登录配置。",
                "severity": "blocker",
                "path": str(config.get("path") or ""),
            }
        )
    elif not bool(config.get("readable")):
        blockers.append(
            {
                "code": "ibc_config_unreadable",
                "message": "IBC config.ini 不可读取，不能安全切换 Gateway 登录配置。",
                "severity": "blocker",
                "path": str(config.get("path") or ""),
                "detail": str(config.get("error") or ""),
            }
        )
    elif not bool(config.get("writable")):
        blockers.append(
            {
                "code": "ibc_config_unwritable",
                "message": "IBC config.ini 不可写入，不能安全切换 Gateway 登录配置。",
                "severity": "blocker",
                "path": str(config.get("path") or ""),
            }
        )
    if account_status_code >= 400 or account_snapshot.get("ok") is False:
        blockers.append(
            {
                "code": "account_snapshot_unavailable",
                "message": f"无法读取当前 {account_snapshot.get('environment') or ''} 账户快照，不能确认持仓/挂单为空。",
                "severity": "blocker",
                "detail": str(account_snapshot.get("error") or account_snapshot.get("message") or ""),
            }
        )
    if to_int(account_risk.get("open_positions"), 0) > 0:
        blockers.append(
            {
                "code": "open_positions",
                "message": f"当前账户还有 {to_int(account_risk.get('open_positions'), 0)} 个持仓，先处理后再切换。",
                "severity": "blocker",
                "count": to_int(account_risk.get("open_positions"), 0),
            }
        )
    if to_int(account_risk.get("open_orders"), 0) > 0:
        blockers.append(
            {
                "code": "open_orders",
                "message": f"当前账户还有 {to_int(account_risk.get('open_orders'), 0)} 个 IBKR 挂单，先撤单后再切换。",
                "severity": "blocker",
                "count": to_int(account_risk.get("open_orders"), 0),
            }
        )
    if to_int(account_risk.get("pb_blocking_groups"), 0) > 0:
        blockers.append(
            {
                "code": "pb_active_or_stale_groups",
                "message": f"PB 仍有 {to_int(account_risk.get('pb_blocking_groups'), 0)} 个 active/stale 订单组，先修复或关闭后再切换。",
                "severity": "blocker",
                "count": to_int(account_risk.get("pb_blocking_groups"), 0),
            }
        )
    if bool(two_factor_state.get("active")):
        blockers.append(
            {
                "code": "two_factor_active",
                "message": "当前已有 2FA / Gateway 验证流程未完成，先完成或重开后再切换账户模式。",
                "severity": "blocker",
                "status": str(two_factor_state.get("status") or ""),
                "recovery_phase": str(two_factor_state.get("recovery_phase") or ""),
            }
        )
    return blockers


def build_broker_mode_switch_preview_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    as_dict: AsDict,
    get_state_payload: GetStatePayload | None = None,
    ibkr_2fa_state_key: str = "ibkr_2fa",
    ibkr_2fa_state_date: str = "global",
    env_files: list[str] | tuple[str, ...] | None = None,
    ibc_config_path: str | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    runtime_context = _runtime_context(request_payload, fetch_runtime_status=fetch_runtime_status, as_dict=as_dict)
    current_broker_mode = runtime_context["current_broker_mode"]
    target_broker_mode = _normalize_requested_target(request_payload, current_broker_mode)
    switch_required = target_broker_mode != current_broker_mode
    env_file_items, env_values = _load_env_files(env_files)
    target_key = _target_account_key(target_broker_mode)
    current_key = _current_account_key(current_broker_mode)
    target_account_id = str(env_values.get(target_key) or "").strip()
    current_account_id = str(env_values.get(current_key) or "").strip()
    gateway_credentials = resolve_gateway_credentials(target_broker_mode, env_values)
    gateway_config = inspect_gateway_config(
        target_broker_mode,
        env_values=env_values,
        credentials=gateway_credentials,
        ibc_config_path=ibc_config_path,
    )

    account_snapshot, account_status_code = _load_account_snapshot(
        pb,
        current_broker_mode,
        normalize_environment=normalize_environment,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
    )
    account_risk = summarize_account_switch_risk(account_snapshot if isinstance(account_snapshot, dict) else {})
    two_factor_state = _two_factor_active_state(
        current_broker_mode,
        get_state_payload=get_state_payload,
        as_dict=as_dict,
        ibkr_2fa_state_key=ibkr_2fa_state_key,
        ibkr_2fa_state_date=ibkr_2fa_state_date,
    )
    blockers = _build_blockers(
        target_broker_mode=target_broker_mode,
        switch_required=switch_required,
        account_snapshot=account_snapshot if isinstance(account_snapshot, dict) else {},
        account_status_code=account_status_code,
        account_risk=account_risk,
        target_account_id=target_account_id,
        two_factor_state=two_factor_state,
        gateway_credentials=gateway_credentials,
        gateway_config=gateway_config,
    )
    missing_env_files = [item for item in env_file_items if not (item.get("exists") and item.get("readable"))]
    if switch_required and missing_env_files:
        blockers.append(
            {
                "code": "env_files_missing",
                "message": f"有 {len(missing_env_files)} 个运行 .env 文件不存在或不可读，不能保证所有服务同步切换。",
                "severity": "blocker",
                "paths": [str(item.get("path") or "") for item in missing_env_files],
            }
        )

    allowed = switch_required and not blockers
    return {
        "ok": True,
        "allowed": allowed,
        "switch_required": switch_required,
        "current_broker_mode": current_broker_mode,
        "requested_broker_mode": runtime_context["requested_broker_mode"],
        "target_broker_mode": target_broker_mode,
        "confirm_text": f"SWITCH {target_broker_mode.upper()}",
        "blockers": blockers,
        "account": {
            "current_account_key": current_key,
            "target_account_key": target_key,
            "current_account_id_masked": _mask_account_id(current_account_id or account_risk.get("account_id")),
            "target_account_id_masked": _mask_account_id(target_account_id),
            "target_account_present": bool(target_account_id),
        },
        "counts": account_risk,
        "two_factor": two_factor_state,
        "gateway_config": gateway_config,
        "env_files": env_file_items,
        "restart_plan": list(MODE_SWITCH_RESTART_PLAN),
        "runtime_status_error": runtime_context.get("runtime_status_error") or "",
        "source": "ibkr-api",
    }, 200


def _replace_or_append_env_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        if "=" not in raw_line or raw_line.strip().startswith("#"):
            result.append(raw_line)
            continue
        key, _value = raw_line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            result.append(f"{normalized_key}={updates[normalized_key]}")
            seen.add(normalized_key)
        else:
            result.append(raw_line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")
    return result


def _preserve_legacy_login_updates(values: dict[str, str]) -> dict[str, str]:
    current_mode = _broker_mode_or_empty(values.get("IBKR_BROKER_MODE") or values.get("IBKR_GATEWAY_MODE"))
    if current_mode not in {"live", "paper"}:
        return {}
    prefix = "IBKR_LIVE" if current_mode == "live" else "IBKR_PAPER"
    updates: dict[str, str] = {}
    legacy_username = str(values.get("IBKR_USERNAME") or "").strip()
    legacy_password = str(values.get("IBKR_PASSWORD") or "").strip()
    if legacy_username and not str(values.get(f"{prefix}_USERNAME") or "").strip():
        updates[f"{prefix}_USERNAME"] = legacy_username
    if legacy_password and not str(values.get(f"{prefix}_PASSWORD") or "").strip():
        updates[f"{prefix}_PASSWORD"] = legacy_password
    return updates


def update_mode_env_files(
    target_broker_mode: str,
    *,
    env_files: list[str] | tuple[str, ...] | None = None,
    gateway_credentials: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    normalized_target = normalize_broker_mode(target_broker_mode, "paper")
    stamp = str(timestamp or int(time.time()))
    updates = {
        "IBKR_BROKER_MODE": normalized_target,
        "IBKR_GATEWAY_MODE": normalized_target,
    }
    credentials = ensure_object(gateway_credentials)
    if credentials.get("username"):
        updates["IBKR_USERNAME"] = str(credentials.get("username") or "")
    if credentials.get("password"):
        updates["IBKR_PASSWORD"] = str(credentials.get("password") or "")
    results: list[dict[str, Any]] = []
    for file_path in resolve_mode_switch_env_files(env_files):
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            results.append({"path": str(path), "updated": False, "skipped": True, "reason": "missing"})
            continue
        stat_result = path.stat()
        original_text = path.read_text()
        original_lines = original_text.splitlines()
        original_values = _parse_env_text(original_text)
        file_updates = {
            **_preserve_legacy_login_updates(original_values),
            **updates,
        }
        backup_path = Path(f"{path}.bak.mode-switch.{stamp}")
        shutil.copy2(path, backup_path)
        next_text = "\n".join(_replace_or_append_env_lines(original_lines, file_updates)) + "\n"
        temp_path = path.with_name(f".{path.name}.tmp.mode-switch.{os.getpid()}.{stamp}")
        temp_path.write_text(next_text)
        os.chmod(temp_path, stat_result.st_mode & 0o777)
        os.replace(temp_path, path)
        results.append(
            {
                "path": str(path),
                "updated": True,
                "backup_path": str(backup_path),
                "broker_mode": normalized_target,
                "gateway_mode": normalized_target,
                "username_key": "IBKR_USERNAME" if credentials.get("username") else "",
                "password_key": "IBKR_PASSWORD" if credentials.get("password") else "",
            }
        )
    return results


def update_ibc_config_file(
    target_broker_mode: str,
    *,
    env_values: dict[str, str],
    gateway_credentials: dict[str, Any],
    ibc_config_path: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    normalized_target = normalize_broker_mode(target_broker_mode, "paper")
    credentials = ensure_object(gateway_credentials)
    username = str(credentials.get("username") or "").strip()
    password = str(credentials.get("password") or "").strip()
    if not username:
        raise ValueError("target_gateway_username_missing")
    if not password:
        raise ValueError("target_gateway_password_missing")
    path = Path(resolve_mode_switch_ibc_config_path(env_values, ibc_config_path))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    stat_result = path.stat()
    original_text = path.read_text()
    original_values = _parse_env_text(original_text)
    api_port = str(env_values.get("IBGW_PORT") or original_values.get("OverrideTwsApiPort") or "4001").strip() or "4001"
    updates = {
        "IbLoginId": username,
        "IbPassword": password,
        "TradingMode": normalized_target,
        "OverrideTwsApiPort": api_port,
    }
    stamp = str(timestamp or int(time.time()))
    backup_path = Path(f"{path}.bak.mode-switch.{stamp}")
    shutil.copy2(path, backup_path)
    next_text = "\n".join(_replace_or_append_env_lines(original_text.splitlines(), updates)) + "\n"
    temp_path = path.with_name(f".{path.name}.tmp.mode-switch.{os.getpid()}.{stamp}")
    temp_path.write_text(next_text)
    os.chmod(temp_path, stat_result.st_mode & 0o777)
    os.replace(temp_path, path)
    return {
        "path": str(path),
        "updated": True,
        "backup_path": str(backup_path),
        "trading_mode": normalized_target,
        "login_id_masked": _mask_login_id(username),
        "api_port": api_port,
    }


def _run_systemctl(unit: str, action: str, *, timeout: float = 45.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["systemctl", action, unit],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": int(result.returncode or 0),
            "stdout": str(result.stdout or "").strip(),
            "stderr": str(result.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": str(exc.stdout or "").strip(),
            "stderr": str(exc.stderr or "").strip() or f"systemctl {action} {unit} timed out",
        }
    except Exception as exc:
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": str(exc)}


def default_systemctl_action(service: str, action: str) -> dict[str, Any]:
    timeout = 60.0 if action == "restart" else 30.0
    return _run_systemctl(service, action, timeout=timeout)


def default_schedule_api_restart(reason: str = "broker_mode_switch") -> dict[str, Any]:
    command = "sleep 2; systemctl restart ibkr-api"
    try:
        subprocess.Popen(
            ["/bin/sh", "-lc", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "scheduled": True, "reason": reason, "command": command}
    except Exception as exc:
        return {"ok": False, "scheduled": False, "reason": reason, "error": str(exc), "command": command}


def _emit_switch_event(
    emit_system_event: EmitSystemEvent | None,
    *,
    level: str,
    title: str,
    detail: dict[str, Any],
    environment: str,
) -> None:
    if not callable(emit_system_event):
        return
    try:
        emit_system_event(
            event_type="broker_mode_switch",
            level=level,
            source="manual",
            title=title,
            detail=detail,
            environment=environment,
        )
    except Exception:
        pass


def build_broker_mode_switch_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    as_dict: AsDict,
    get_state_payload: GetStatePayload | None = None,
    ibkr_2fa_state_key: str = "ibkr_2fa",
    ibkr_2fa_state_date: str = "global",
    env_files: list[str] | tuple[str, ...] | None = None,
    ibc_config_path: str | None = None,
    emit_system_event: EmitSystemEvent | None = None,
    systemctl_action: SystemctlAction | None = None,
    schedule_api_restart: ScheduleApiRestart | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    preview, _status_code = build_broker_mode_switch_preview_response(
        pb,
        payload=request_payload,
        normalize_environment=normalize_environment,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
        fetch_runtime_status=fetch_runtime_status,
        as_dict=as_dict,
        get_state_payload=get_state_payload,
        ibkr_2fa_state_key=ibkr_2fa_state_key,
        ibkr_2fa_state_date=ibkr_2fa_state_date,
        env_files=env_files,
        ibc_config_path=ibc_config_path,
    )
    current_broker_mode = str(preview.get("current_broker_mode") or "paper")
    target_broker_mode = str(preview.get("target_broker_mode") or "paper")
    if not bool(preview.get("switch_required")):
        return {**preview, "ok": True, "action": "noop", "message": "已在目标 broker mode。"}, 200
    if preview.get("blockers"):
        _emit_switch_event(
            emit_system_event,
            level="warning",
            title=f"Broker mode switch blocked: {current_broker_mode}->{target_broker_mode}",
            detail={"blockers": preview.get("blockers") or [], "source": request_payload.get("source") or "ibkr-api"},
            environment=current_broker_mode,
        )
        return {**preview, "ok": False, "error": "broker_mode_switch_blocked"}, 409

    required_confirm = str(preview.get("confirm_text") or f"SWITCH {target_broker_mode.upper()}")
    provided_confirm = str(request_payload.get("confirm_text") or "").strip()
    if provided_confirm != required_confirm:
        return {
            **preview,
            "ok": False,
            "error": "invalid_confirm_text",
            "message": f"请输入 {required_confirm} 确认切换。",
        }, 400

    if not _SWITCH_LOCK.acquire(blocking=False):
        return {
            **preview,
            "ok": False,
            "error": "broker_mode_switch_in_progress",
            "message": "已有 broker mode 切换正在执行。",
        }, 409

    try:
        checked_preview, _ = build_broker_mode_switch_preview_response(
            pb,
            payload=request_payload,
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            as_dict=as_dict,
            get_state_payload=get_state_payload,
            ibkr_2fa_state_key=ibkr_2fa_state_key,
            ibkr_2fa_state_date=ibkr_2fa_state_date,
            env_files=env_files,
            ibc_config_path=ibc_config_path,
        )
        if checked_preview.get("blockers"):
            return {**checked_preview, "ok": False, "error": "broker_mode_switch_blocked"}, 409

        timestamp = time.strftime("%Y%m%d%H%M%S")
        _env_file_items, env_values = _load_env_files(env_files)
        gateway_credentials = resolve_gateway_credentials(target_broker_mode, env_values)
        env_updates = update_mode_env_files(
            target_broker_mode,
            env_files=env_files,
            gateway_credentials=gateway_credentials,
            timestamp=timestamp,
        )
        updated_files = [item for item in env_updates if item.get("updated")]
        if not updated_files:
            return {
                **checked_preview,
                "ok": False,
                "error": "env_update_failed",
                "message": "没有任何 .env 文件被更新。",
                "env_updates": env_updates,
            }, 500
        try:
            ibc_config_update = update_ibc_config_file(
                target_broker_mode,
                env_values=env_values,
                gateway_credentials=gateway_credentials,
                ibc_config_path=ibc_config_path,
                timestamp=timestamp,
            )
        except Exception as exc:
            return {
                **checked_preview,
                "ok": False,
                "error": "ibc_config_update_failed",
                "message": "已更新 .env，但 IBC config.ini 更新失败；未重启服务，请检查配置备份后重试。",
                "env_updates": env_updates,
                "ibc_config_update": {"updated": False, "error": str(exc)},
            }, 500

        _emit_switch_event(
            emit_system_event,
            level="info",
            title=f"Broker mode switch accepted: {current_broker_mode}->{target_broker_mode}",
            detail={
                "current_broker_mode": current_broker_mode,
                "target_broker_mode": target_broker_mode,
                "env_updates": env_updates,
                "ibc_config_update": ibc_config_update,
                "source": request_payload.get("source") or "ibkr-api",
            },
            environment=target_broker_mode,
        )

        action_runner = systemctl_action or default_systemctl_action
        restart_results: list[dict[str, Any]] = []
        for step in MODE_SWITCH_RESTART_PLAN:
            service = str(step["service"])
            action = str(step["action"])
            if service == "ibkr-api":
                scheduler = schedule_api_restart or default_schedule_api_restart
                scheduled = scheduler(f"broker_mode_switch_{target_broker_mode}")
                restart_results.append({**step, "result": scheduled})
                continue
            result = action_runner(service, action)
            restart_results.append({**step, "result": result})
            if action == "restart" and result.get("ok") is False and service in {"ibkr-gateway", "ibkr-runtime"}:
                break

        hard_failures = [
            item for item in restart_results
            if item.get("service") in {"ibkr-gateway", "ibkr-runtime"} and ensure_object(item.get("result")).get("ok") is False
        ]
        ok = not hard_failures
        _emit_switch_event(
            emit_system_event,
            level="info" if ok else "warning",
            title=f"Broker mode switch {'requested' if ok else 'partially failed'}: {target_broker_mode}",
            detail={"restart_results": restart_results, "env_updates": env_updates, "ibc_config_update": ibc_config_update},
            environment=target_broker_mode,
        )
        return {
            **checked_preview,
            "ok": ok,
            "accepted": ok,
            "status": "restart_requested" if ok else "restart_failed",
            "message": (
                f"已写入 {target_broker_mode.upper()} 模式并开始重启服务；完成后需要重新确认 2FA。"
                if ok
                else "Broker mode 已写入，但核心服务重启失败；请检查 systemd 状态。"
            ),
            "env_updates": env_updates,
            "ibc_config_update": ibc_config_update,
            "restart_results": restart_results,
            "next_step": "等待 Gateway/Runtime 以目标模式恢复，然后完成 2FA。" if ok else "检查 gateway/runtime restart failure。",
            "source": "ibkr-api",
        }, 202 if ok else 502
    finally:
        _SWITCH_LOCK.release()


__all__ = [
    "build_broker_mode_switch_preview_response",
    "build_broker_mode_switch_response",
    "summarize_account_switch_risk",
    "update_ibc_config_file",
    "update_mode_env_files",
]
