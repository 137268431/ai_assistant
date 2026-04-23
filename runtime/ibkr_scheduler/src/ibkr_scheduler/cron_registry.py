from __future__ import annotations

from typing import Any


def _definition(
    cron_id: str,
    config_key: str,
    display_name: str,
    config_display_name: str,
    sort_order: float,
    cron_expr: str,
    cycle_label: str,
    function_summary: str,
    note: str,
    *,
    beijing_cycle_label: str = "",
    et_cycle_label: str = "",
    window_label: str = "",
    hook_file: str = "",
    runner_kind: str = "compatibility_pending",
) -> dict[str, Any]:
    return {
        "id": cron_id,
        "config_key": config_key,
        "display_name": display_name,
        "config_display_name": config_display_name,
        "group_name": "IBKR Scheduler",
        "sort_order": sort_order,
        "default_value": "TRUE",
        "cron_expr": cron_expr,
        "cycle_label": cycle_label,
        "beijing_cycle_label": beijing_cycle_label,
        "et_cycle_label": et_cycle_label,
        "window_label": window_label,
        "function_summary": function_summary,
        "scope_label": "按环境执行",
        "note": note,
        "hook_file": hook_file,
        "runner_kind": runner_kind,
    }


CRON_DEFINITIONS: list[dict[str, Any]] = [
    _definition(
        "signal_expiry_check",
        "pb_cron_signal_expiry_enabled",
        "信号过期清理 Cron",
        "信号过期清理",
        130,
        "*/5 * * * *",
        "每 5 分钟",
        "扫描 pending / awaiting_confirm 信号，超时后自动标记为 expired。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生信号过期处理；PB 仅保留兼容壳。",
        hook_file="ibkr_signal_scheduler.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "order_expiry_check",
        "pb_cron_order_expiry_enabled",
        "订单过期取消 Cron",
        "订单过期取消",
        131,
        "*/5 * * * *",
        "每 5 分钟",
        "扫描 Init / Submitted 订单，超时后自动标记为 Canceled。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生订单过期处理；PB 仅保留兼容壳。",
        hook_file="order_scheduler.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "order_detail_integrity_guard",
        "pb_cron_order_detail_integrity_guard_enabled",
        "订单明细自愈 Cron",
        "订单明细自愈",
        131.5,
        "*/10 * * * *",
        "每 10 分钟",
        "巡检 orders 与 ibkr_order_details 是否一致，缺失时自动回补当前状态明细。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生订单明细巡检；PB 仅保留兼容壳。",
        hook_file="order_scheduler.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_compute_runtime",
        "pb_cron_ibkr_compute_runtime_enabled",
        "Compute / 系统状态摘要 Cron",
        "Compute + 状态摘要",
        132,
        "*/5 4-20 * * 1-5",
        "工作日 UTC 04:00-20:55 每 5 分钟",
        "基于已落库的 bar ingest cursor 触发 compute，并串行执行系统状态摘要。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        beijing_cycle_label="北京时间 周一至周五 12:00-23:55，且延续到次日 04:55（周六凌晨结束）",
        et_cycle_label="美东时间 夏令时 EDT 周一至周五 00:00-16:55；冬令时 EST 前一日 23:00-当日 15:55（跨日）",
        window_label="盘前到盘后",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_compute_dispatch",
    ),
    _definition(
        "ibkr_scan_runtime",
        "pb_cron_ibkr_scan_runtime_enabled",
        "盘前 Scan Cron",
        "盘前 Scan",
        133,
        "*/5 7-9 * * 1-5",
        "工作日 UTC 07:00-09:55 每 5 分钟",
        "触发开盘前 scan 调度，刷新当天候选信号与市场扫描结果。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        beijing_cycle_label="北京时间 工作日 15:00-17:55 每 5 分钟",
        et_cycle_label="美东时间 夏令时 EDT 工作日 03:00-05:55；冬令时 EST 工作日 02:00-04:55",
        window_label="盘前窗口",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
    ),
    _definition(
        "ibkr_auth_edge_guard",
        "pb_cron_ibkr_auth_edge_guard_enabled",
        "2FA / Session 即时巡检 Cron",
        "2FA 即时巡检",
        134,
        "* 4-20 * * 1-5",
        "工作日 UTC 04:00-20:59 每 1 分钟",
        "巡检 Session / 2FA 的边沿变化，并在需要时立即告警。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生 2FA 边沿巡检；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_auth_pending_guard",
        "pb_cron_ibkr_auth_pending_guard_enabled",
        "2FA 长时间未恢复巡检 Cron",
        "2FA 长时间未恢复巡检",
        135,
        "*/10 4-20 * * 1-5",
        "工作日 UTC 04:00-20:50 每 10 分钟",
        "巡检 Session / 2FA 长时间未恢复状态，并在需要时发出系统告警。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生 2FA 长时间未恢复巡检；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "system_data_gap_guard",
        "pb_cron_system_data_gap_guard_enabled",
        "数据缺口巡检 Cron",
        "数据缺口巡检",
        136,
        "*/10 4-20 * * 1-5",
        "工作日 UTC 04:00-20:50 每 10 分钟",
        "巡检 bars / indicators / 序列缺口，并在检测到市场活动异常时发出告警。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生数据缺口巡检；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_data_quality_open_sweep",
        "pb_cron_ibkr_data_quality_open_sweep_enabled",
        "开盘前全观察池 Sweep Cron",
        "开盘前全观察池 Sweep",
        136.2,
        "40 9 * * 1-5",
        "工作日 UTC 09:40",
        "对全观察池执行一次 5m 内部一致性 sweep，并尝试安全修复可回补问题。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
    ),
    _definition(
        "ibkr_data_quality_close_sweep",
        "pb_cron_ibkr_data_quality_close_sweep_enabled",
        "收盘后全观察池 Sweep Cron",
        "收盘后全观察池 Sweep",
        136.3,
        "10 20 * * 1-5",
        "工作日 UTC 20:10",
        "收盘后再次对全观察池执行 5m 内部一致性 sweep，沉淀最终 coverage。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
    ),
    _definition(
        "ibkr_data_quality_truth_audit",
        "pb_cron_ibkr_data_quality_truth_audit_enabled",
        "收盘后 IBKR 真值审计 Cron",
        "收盘后 IBKR 真值审计",
        136.4,
        "20 20 * * 1-5",
        "工作日 UTC 20:20",
        "对全观察池执行一次 stored bars vs IBKR authoritative history 真值审计。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
    ),
    _definition(
        "ibkr_weekly_reauth_reminder",
        "pb_cron_ibkr_weekly_reauth_reminder_enabled",
        "周验证提醒 Cron",
        "周验证提醒",
        137,
        "0 5 * * 1",
        "每周一 UTC 05:00",
        "每周发送一张周验证提醒卡片；只提醒，不自动触发 Gateway 登录。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生周验证提醒；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_weekly_reauth_followup",
        "pb_cron_ibkr_weekly_reauth_followup_enabled",
        "周验证补提醒 Cron",
        "周验证补提醒",
        137.5,
        "30 7 * * 1",
        "每周一 UTC 07:30",
        "若周验证仍停在待手动触发阶段，则补发一张飞书验证卡片提醒。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生周验证补提醒；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_2fa_hourly_check",
        "pb_cron_ibkr_2fa_hourly_check_enabled",
        "2FA 每小时提醒 Cron",
        "2FA 每小时提醒",
        138,
        "5 4-20 * * 1-5",
        "工作日 UTC 每小时 05 分",
        "若 2FA 仍未恢复，则按小时补发飞书验证卡片提醒。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生 2FA 每小时提醒；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "system_market_open_reminder",
        "pb_cron_system_market_open_reminder_enabled",
        "开盘前状态提醒 Cron",
        "开盘前状态提醒",
        139,
        "*/5 * * * *",
        "每 5 分钟轮询一次；内部按 ET 09:20 仅发送一次",
        "每日 09:20 发送开盘前系统状态；若为非交易日则发送闭市 / 休市提醒。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生开盘前状态提醒；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "system_daily_report",
        "pb_cron_system_daily_report_enabled",
        "系统日报 Cron",
        "系统日报",
        139.5,
        "*/5 * * * *",
        "每 5 分钟轮询一次；内部按 ET 16:05 仅发送一次",
        "汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生系统日报；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
    ),
    _definition(
        "ibkr_history_retention",
        "pb_cron_ibkr_history_retention_enabled",
        "历史数据留存 Cron",
        "历史数据留存",
        140,
        "10 * * * *",
        "每小时第 10 分钟",
        "清理超过留存窗口的历史数据。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
    ),
]


NATIVE_HTTP_JOB_ENDPOINTS: dict[str, tuple[str, str]] = {
    "ibkr_scan_runtime": ("POST", "/scan"),
    "ibkr_history_retention": ("POST", "/retention/cleanup"),
    "ibkr_data_quality_open_sweep": ("POST", "/ibkr/data-quality/repair"),
    "ibkr_data_quality_close_sweep": ("POST", "/ibkr/data-quality/repair"),
    "ibkr_data_quality_truth_audit": ("POST", "/ibkr/data-quality/truth-audit"),
}


NATIVE_API_HTTP_JOB_ENDPOINTS: dict[str, tuple[str, str]] = {
    "signal_expiry_check": ("POST", "/api/custom/system/jobs/signal_expiry"),
    "order_detail_integrity_guard": ("POST", "/api/custom/system/jobs/order_detail_integrity"),
    "order_expiry_check": ("POST", "/api/custom/system/jobs/order_expiry"),
    "ibkr_auth_edge_guard": ("POST", "/api/custom/system/jobs/auth_edge_guard"),
    "ibkr_auth_pending_guard": ("POST", "/api/custom/system/jobs/auth_pending_guard"),
    "system_data_gap_guard": ("POST", "/api/custom/system/jobs/data_gap_guard"),
    "ibkr_2fa_hourly_check": ("POST", "/api/custom/system/jobs/2fa_hourly_check"),
    "ibkr_weekly_reauth_reminder": ("POST", "/api/custom/system/jobs/weekly_reauth_reminder"),
    "ibkr_weekly_reauth_followup": ("POST", "/api/custom/system/jobs/weekly_reauth_followup"),
    "system_market_open_reminder": ("POST", "/api/custom/system/jobs/market_open_reminder"),
    "system_daily_report": ("POST", "/api/custom/system/jobs/daily_report"),
}


def is_truthy_config_value(value: Any, default: str = "TRUE") -> bool:
    text = str(value if value not in (None, "") else default).strip().lower()
    return text in {"true", "1", "yes", "on"}


def build_effective_cron_definition(definition: dict[str, Any], config, environment: str, job_state: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    scheduler_raw = str(config.get_for_environment("pb_scheduler_enabled", runtime_environment, "TRUE") or "TRUE")
    cron_raw = str(config.get_for_environment(definition["config_key"], runtime_environment, definition["default_value"]) or definition["default_value"])
    scheduler_enabled = is_truthy_config_value(scheduler_raw)
    cron_enabled = is_truthy_config_value(cron_raw, definition["default_value"])
    state = job_state if isinstance(job_state, dict) else {}
    return {
        **definition,
        "environment": runtime_environment,
        "scheduler_raw": scheduler_raw,
        "cron_raw": cron_raw,
        "scheduler_enabled": scheduler_enabled,
        "cron_enabled": cron_enabled,
        "effective_enabled": scheduler_enabled and cron_enabled,
        "job_state": state,
        "config_description": (
            f"{definition['function_summary']} Cron: {definition['cron_expr']}；UTC 周期: {definition['cycle_label']} "
            f"{definition['note']}"
        ).strip(),
    }


def build_cron_payload(config, environment: str, job_states: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    state_map = job_states if isinstance(job_states, dict) else {}
    items = [
        build_effective_cron_definition(definition, config, environment, state_map.get(definition["id"]))
        for definition in CRON_DEFINITIONS
    ]
    return sorted(items, key=lambda item: (float(item.get("sort_order", 0) or 0), str(item.get("id") or "")))
