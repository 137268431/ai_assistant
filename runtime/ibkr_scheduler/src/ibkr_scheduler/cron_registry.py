from __future__ import annotations

from typing import Any


def _schedule(
    schedule_id: str,
    cron_expr: str,
    *,
    cron_timezone: str = "UTC",
    label: str = "",
    payload_mode: str = "",
) -> dict[str, Any]:
    return {
        "id": str(schedule_id or "default").strip() or "default",
        "cron_expr": str(cron_expr or "").strip(),
        "cron_timezone": str(cron_timezone or "UTC").strip() or "UTC",
        "label": str(label or "").strip(),
        "payload_mode": str(payload_mode or "").strip(),
    }


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
    mode_scope: str,
    cron_timezone: str = "UTC",
    family: str = "",
    schedules: list[dict[str, Any]] | None = None,
    deprecated_aliases: list[str] | None = None,
    deprecated_config_keys: list[str] | None = None,
) -> dict[str, Any]:
    normalized_mode_scope = str(mode_scope or "").strip().lower()
    if normalized_mode_scope not in {"broker", "market_data"}:
        raise ValueError(f"invalid mode_scope for {cron_id}: {mode_scope!r}")
    schedule_items = schedules or [
        _schedule("default", cron_expr, cron_timezone=cron_timezone, label=cycle_label)
    ]
    return {
        "id": cron_id,
        "canonical_id": cron_id,
        "family": str(family or "default").strip() or "default",
        "deprecated_aliases": [
            str(item or "").strip()
            for item in (deprecated_aliases or [])
            if str(item or "").strip()
        ],
        "deprecated_config_keys": [
            str(item or "").strip()
            for item in (deprecated_config_keys or [])
            if str(item or "").strip()
        ],
        "config_key": config_key,
        "display_name": display_name,
        "config_display_name": config_display_name,
        "group_name": "IBKR Scheduler",
        "sort_order": sort_order,
        "default_value": "TRUE",
        "cron_expr": cron_expr,
        "cron_timezone": str(cron_timezone or "UTC").strip() or "UTC",
        "schedules": schedule_items,
        "cycle_label": cycle_label,
        "beijing_cycle_label": beijing_cycle_label,
        "et_cycle_label": et_cycle_label,
        "window_label": window_label,
        "function_summary": function_summary,
        "mode_scope": normalized_mode_scope,
        "scope_label": "Broker Mode" if normalized_mode_scope == "broker" else "Market Data Mode",
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
        mode_scope="broker",
        family="trade_maintenance",
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
        mode_scope="broker",
        family="trade_maintenance",
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
        mode_scope="broker",
        family="trade_maintenance",
    ),
    _definition(
        "ibkr_compute_runtime",
        "pb_cron_ibkr_compute_runtime_enabled",
        "Compute / 系统状态摘要 Cron",
        "Compute + 状态摘要",
        132,
        "*/5 * * * *",
        "每 5 分钟检查一次；仅当已落库 5m bar 游标前进时触发 compute",
        "基于已落库的 bar ingest cursor 触发 compute，并串行执行系统状态摘要。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关；空转时只比较 PB state 游标，不调用 compute。",
        beijing_cycle_label="北京时间 每 5 分钟检查一次；是否执行由已持久化 bar cursor 决定",
        et_cycle_label="美东时间 每 5 分钟检查一次；覆盖盘前、盘中、盘后和 DST 切换",
        window_label="游标驱动",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_compute_dispatch",
        mode_scope="market_data",
        family="compute_dispatch",
    ),
    _definition(
        "system_heartbeat",
        "pb_cron_system_heartbeat_enabled",
        "系统心跳 Cron",
        "系统心跳",
        132.1,
        "*/5 4-20 * * 1-5",
        "工作日 UTC 04:00-20:55 每 5 分钟",
        "基于 ibkr-api 的 summaryz / monitorz 生成原生系统心跳与恢复通知。",
        "已迁到 ibkr-api + ibkr-scheduler；PocketBase 不再注册对应 cron。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        family="system_monitor",
    ),
    _definition(
        "system_monitor_alert_guard",
        "pb_cron_system_monitor_alert_guard_enabled",
        "系统监控告警 Cron",
        "系统监控告警",
        132.2,
        "*/5 4-20 * * 1-5",
        "工作日 UTC 04:00-20:55 每 5 分钟",
        "读取 monitorz flags 和故障域状态，生成系统监控告警。",
        "已迁到 ibkr-api + ibkr-scheduler；PocketBase 不再注册对应 cron。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        family="system_monitor",
    ),
    _definition(
        "system_status_reminder",
        "pb_cron_system_status_reminder_enabled",
        "系统状态摘要 Cron",
        "系统状态摘要",
        132.3,
        "0,30 4-20 * * 1-5",
        "工作日 UTC 04:00-20:30 每 30 分钟",
        "定时发送 split stack 的系统状态摘要；09:30 ET 由开盘交易摘要接管，避免重复卡片。",
        "已迁到 ibkr-api + ibkr-scheduler；PocketBase 不再注册对应 cron。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        family="system_monitor",
    ),
    _definition(
        "ibkr_scan_runtime",
        "pb_cron_ibkr_scan_runtime_enabled",
        "盘前 Scan Cron",
        "盘前 Scan",
        133,
        "20 9 * * 1-5",
        "工作日 America/New_York 09:20",
        "按 09:20 ET 触发盘前 daily scan，刷新当天 candidate / active 目标池。",
        "兼容读取 pb_scheduler_enabled 和原有 pb_cron_* 开关。",
        beijing_cycle_label="北京时间 工作日 21:20（美东夏令时）/ 22:20（美东冬令时）",
        et_cycle_label="美东时间 工作日 09:20",
        window_label="09:20 ET 日筛",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="target_universe",
    ),
    _definition(
        "ibkr_fundamentals_refresh",
        "pb_cron_ibkr_fundamentals_refresh_enabled",
        "Fundamentals 刷新 Cron",
        "Fundamentals 刷新",
        133.05,
        "5 8 * * 1-5",
        "工作日 ET 08:05",
        "盘前小批量刷新 trade watchlist 的股票基础数据，用于动态准入画像和分层筛选。",
        "由 ibkr-api 读取 FINNHUB_API_KEY 并写入 ibkr_fundamentals 缓存；未配置 key 时只记录失败原因，不泄露 token。",
        beijing_cycle_label="北京时间 工作日 20:05（美东夏令时）/ 21:05（美东冬令时）",
        et_cycle_label="美东时间 工作日 08:05",
        window_label="08:05 ET 基础数据刷新",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="target_universe",
    ),
    _definition(
        "ibkr_early_expansion_topup",
        "pb_cron_ibkr_early_expansion_topup_enabled",
        "早盘扩池补充 Cron",
        "早盘扩池补充",
        133.2,
        "30,40,50 9 * * 1-5",
        "工作日 ET 09:30/09:40/09:50",
        "09:20 主池后，在 09:30-10:30 ET 每 10 分钟执行增量扩池；只增加新可操作标的并按需提醒。",
        "由 ibkr-api 触发 compute topup scan 并负责新增标的提醒；使用 America/New_York 时区匹配。",
        beijing_cycle_label="北京时间 工作日 21:30-22:30（美东夏令时）/ 22:30-23:30（美东冬令时）每 10 分钟",
        et_cycle_label="美东时间 工作日 09:30-10:30 每 10 分钟",
        window_label="09:30-10:30 ET 早盘扩池",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="target_universe",
        schedules=[
            _schedule(
                "early_0930_0950",
                "30,40,50 9 * * 1-5",
                cron_timezone="America/New_York",
                label="工作日 ET 09:30/09:40/09:50",
            ),
            _schedule(
                "early_1000_1030",
                "0,10,20,30 10 * * 1-5",
                cron_timezone="America/New_York",
                label="工作日 ET 10:00/10:10/10:20/10:30",
            ),
        ],
        deprecated_aliases=["ibkr_early_expansion_topup_late"],
    ),
    _definition(
        "ibkr_intraday_window_admission",
        "pb_cron_ibkr_intraday_window_admission_enabled",
        "盘中窗口入池 Cron",
        "盘中窗口入池",
        133.25,
        "*/5 9-15 * * 1-5",
        "工作日 ET 09:00-15:55 每 5 分钟；API 内部只在 09:35-15:55 入池",
        "扫描 trade 观察池中已有新鲜 5m bars 的非 active 标的，若当前窗口有效且通过流动性/ATR/新鲜度门槛，则自动加入今日 active 标的池并触发 runtime reconcile。",
        "由 ibkr-api 执行窗口追踪和目标 upsert；只新增或提升有效窗口标的，不替换已有目标。",
        beijing_cycle_label="北京时间 工作日 21:35-03:55（美东夏令时）/ 22:35-04:55（美东冬令时）每 5 分钟",
        et_cycle_label="美东时间 工作日 09:35-15:55 每 5 分钟",
        window_label="09:35-15:55 ET 盘中窗口入池",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="target_universe",
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
        mode_scope="broker",
        family="auth_monitor",
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
        mode_scope="broker",
        family="auth_monitor",
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
        mode_scope="market_data",
        family="system_monitor",
    ),
    _definition(
        "ibkr_data_quality_repair_sweep",
        "pb_cron_ibkr_data_quality_repair_sweep_enabled",
        "全观察池 Sweep Cron",
        "全观察池 Sweep",
        136.2,
        "40 9 * * 1-5",
        "工作日 UTC 09:40 / 20:10",
        "对全观察池执行 5m 内部一致性 sweep，并尝试安全修复可回补问题。",
        "统一管理开盘前和收盘后两次 sweep；兼容原 open_sweep / close_sweep 开关。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
        mode_scope="market_data",
        family="data_quality",
        schedules=[
            _schedule("open_sweep", "40 9 * * 1-5", label="工作日 UTC 09:40"),
            _schedule("close_sweep", "10 20 * * 1-5", label="工作日 UTC 20:10"),
        ],
        deprecated_aliases=[
            "ibkr_data_quality_open_sweep",
            "ibkr_data_quality_close_sweep",
        ],
        deprecated_config_keys=[
            "pb_cron_ibkr_data_quality_open_sweep_enabled",
            "pb_cron_ibkr_data_quality_close_sweep_enabled",
        ],
    ),
    _definition(
        "ibkr_data_quality_truth_audit_cycle",
        "pb_cron_ibkr_data_quality_truth_audit_enabled",
        "IBKR 真值审计 Cron",
        "IBKR 真值审计",
        136.35,
        "20 8 * * 1-5",
        "工作日 ET 08:20 / 16:20",
        "对全观察池执行 stored bars vs IBKR authoritative history 真值审计。",
        "统一管理盘前上一交易日审计和盘后当天审计；使用 America/New_York 时区匹配，自动覆盖 DST。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="data_quality",
        schedules=[
            _schedule(
                "premarket_previous_business_day",
                "20 8 * * 1-5",
                cron_timezone="America/New_York",
                label="工作日 ET 08:20，检查上一交易日",
                payload_mode="previous_business_day",
            ),
            _schedule(
                "postmarket_current_day",
                "20 16 * * 1-5",
                cron_timezone="America/New_York",
                label="工作日 ET 16:20，检查当天",
                payload_mode="current_day",
            ),
        ],
        deprecated_aliases=[
            "ibkr_data_quality_premarket_truth_audit",
            "ibkr_data_quality_truth_audit",
        ],
        deprecated_config_keys=["pb_cron_ibkr_data_quality_premarket_truth_audit_enabled"],
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
        mode_scope="broker",
        family="auth_monitor",
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
        mode_scope="broker",
        family="auth_monitor",
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
        mode_scope="broker",
        family="auth_monitor",
    ),
    _definition(
        "system_market_open_reminder",
        "pb_cron_system_market_open_reminder_enabled",
        "09:30 开盘交易摘要兼容 Cron",
        "09:30 开盘交易摘要",
        139,
        "*/5 * * * *",
        "每 5 分钟轮询一次；内部按 ET 09:30-09:39 仅发送一次",
        "兼容入口：仅 NYSE 交易日 09:30 发送开盘交易摘要，实际与 system_scan_summary 共用同一状态避免重复。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生开盘交易摘要；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        family="system_monitor",
        deprecated_aliases=["system_scan_summary"],
        deprecated_config_keys=["pb_cron_system_scan_summary_enabled"],
    ),
    _definition(
        "system_daily_report",
        "pb_cron_system_daily_report_enabled",
        "系统日报 Cron",
        "系统日报",
        139.5,
        "*/5 * * * *",
        "每 5 分钟轮询一次；内部按 ET 16:05 仅发送一次",
        "仅 NYSE 交易日汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报。",
        "当前由 ibkr-scheduler 触发 ibkr-api 原生系统日报；PB 仅保留兼容壳。",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_api_http",
        mode_scope="market_data",
        family="system_monitor",
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
        mode_scope="market_data",
        family="storage_maintenance",
    ),
    _definition(
        "ibkr_storage_governor",
        "pb_cron_ibkr_storage_governor_enabled",
        "PocketBase 存储治理 Cron",
        "PocketBase 存储治理",
        140.5,
        "20 3 * * *",
        "每日 America/New_York 03:20",
        "按 balanced_50g 策略清理可重建指标、旧日志、TV 兼容数据和旧回测产物。",
        "兼容读取 pb_scheduler_enabled 和 storage_cleanup_enabled；仅删除安全过期数据，不自动 VACUUM。",
        beijing_cycle_label="北京时间 每日 15:20（美东夏令时）/ 16:20（美东冬令时）",
        et_cycle_label="美东时间 每日 03:20",
        window_label="低峰存储治理",
        hook_file="ibkr_system_monitor.pb.js",
        runner_kind="native_http",
        mode_scope="market_data",
        cron_timezone="America/New_York",
        family="storage_maintenance",
    ),
]


NATIVE_HTTP_JOB_ENDPOINTS: dict[str, tuple[str, str]] = {
    "ibkr_scan_runtime": ("POST", "/scan"),
    "ibkr_history_retention": ("POST", "/retention/cleanup"),
    "ibkr_storage_governor": ("POST", "/storage/cleanup"),
    "ibkr_data_quality_repair_sweep": ("POST", "/ibkr/data-quality/repair"),
    "ibkr_data_quality_truth_audit_cycle": ("POST", "/ibkr/data-quality/truth-audit"),
}


NATIVE_API_HTTP_JOB_ENDPOINTS: dict[str, tuple[str, str]] = {
    "signal_expiry_check": ("POST", "/api/custom/system/jobs/signal_expiry"),
    "order_detail_integrity_guard": ("POST", "/api/custom/system/jobs/order_detail_integrity"),
    "order_expiry_check": ("POST", "/api/custom/system/jobs/order_expiry"),
    "ibkr_auth_edge_guard": ("POST", "/api/custom/system/jobs/auth_edge_guard"),
    "ibkr_auth_pending_guard": ("POST", "/api/custom/system/jobs/auth_pending_guard"),
    "system_data_gap_guard": ("POST", "/api/custom/system/jobs/data_gap_guard"),
    "system_heartbeat": ("POST", "/api/custom/system/jobs/heartbeat"),
    "system_monitor_alert_guard": ("POST", "/api/custom/system/jobs/monitor_alert_guard"),
    "system_status_reminder": ("POST", "/api/custom/system/jobs/status_reminder"),
    "ibkr_fundamentals_refresh": ("POST", "/api/custom/system/jobs/fundamentals_refresh"),
    "ibkr_early_expansion_topup": ("POST", "/api/custom/system/jobs/early_expansion_topup"),
    "ibkr_intraday_window_admission": ("POST", "/api/custom/system/jobs/intraday_window_admission"),
    "ibkr_2fa_hourly_check": ("POST", "/api/custom/system/jobs/2fa_hourly_check"),
    "ibkr_weekly_reauth_reminder": ("POST", "/api/custom/system/jobs/weekly_reauth_reminder"),
    "ibkr_weekly_reauth_followup": ("POST", "/api/custom/system/jobs/weekly_reauth_followup"),
    "system_market_open_reminder": ("POST", "/api/custom/system/jobs/market_open_reminder"),
    "system_daily_report": ("POST", "/api/custom/system/jobs/daily_report"),
}


CRON_DEFINITION_BY_ID: dict[str, dict[str, Any]] = {
    str(definition.get("id") or ""): definition
    for definition in CRON_DEFINITIONS
    if str(definition.get("id") or "")
}

CRON_ALIAS_SCHEDULE_IDS: dict[str, str] = {
    "ibkr_early_expansion_topup_late": "early_1000_1030",
    "ibkr_data_quality_open_sweep": "open_sweep",
    "ibkr_data_quality_close_sweep": "close_sweep",
    "ibkr_data_quality_premarket_truth_audit": "premarket_previous_business_day",
    "ibkr_data_quality_truth_audit": "postmarket_current_day",
}

CRON_ALIAS_MAP: dict[str, str] = {}
for _definition_item in CRON_DEFINITIONS:
    _canonical_id = str(_definition_item.get("id") or "").strip()
    for _alias in _definition_item.get("deprecated_aliases") or []:
        _alias_id = str(_alias or "").strip()
        if _canonical_id and _alias_id:
            CRON_ALIAS_MAP[_alias_id] = _canonical_id


def resolve_cron_job(job_id: str, schedule_id: str = "") -> tuple[dict[str, Any] | None, str, str, str]:
    requested_job_id = str(job_id or "").strip()
    requested_schedule_id = str(schedule_id or "").strip()
    canonical_id = CRON_ALIAS_MAP.get(requested_job_id, requested_job_id)
    definition = CRON_DEFINITION_BY_ID.get(canonical_id)
    alias_schedule_id = CRON_ALIAS_SCHEDULE_IDS.get(requested_job_id, "")
    effective_schedule_id = requested_schedule_id or alias_schedule_id
    alias_job_id = requested_job_id if requested_job_id and requested_job_id != canonical_id else ""
    return definition, canonical_id, alias_job_id, effective_schedule_id


def get_schedule(definition: dict[str, Any], schedule_id: str = "") -> dict[str, Any]:
    schedules = definition.get("schedules") if isinstance(definition.get("schedules"), list) else []
    requested = str(schedule_id or "").strip()
    if requested:
        for schedule in schedules:
            if str((schedule or {}).get("id") or "").strip() == requested:
                return dict(schedule)
    if schedules:
        return dict(schedules[0])
    return _schedule(
        "default",
        str(definition.get("cron_expr") or ""),
        cron_timezone=str(definition.get("cron_timezone") or "UTC"),
        label=str(definition.get("cycle_label") or ""),
    )


def mode_scope(definition: dict[str, Any]) -> str:
    scope = str((definition or {}).get("mode_scope") or "").strip().lower()
    if scope not in {"broker", "market_data"}:
        raise ValueError(f"invalid mode_scope for {str((definition or {}).get('id') or '')}: {scope!r}")
    return scope


def mode_for_scope(definition: dict[str, Any], broker_mode: str, market_data_mode: str) -> str:
    scope = mode_scope(definition)
    selected = broker_mode if scope == "broker" else market_data_mode
    return str(selected or "live").strip().lower() or "live"


def is_truthy_config_value(value: Any, default: str = "TRUE") -> bool:
    text = str(value if value not in (None, "") else default).strip().lower()
    return text in {"true", "1", "yes", "on"}


def _config_has_record(config: Any, key: str, environment: str) -> bool:
    checker = getattr(config, "has_value_for_environment", None)
    if callable(checker):
        try:
            return bool(checker(key, environment))
        except Exception:
            return False
    return False


def _definition_config_value(definition: dict[str, Any], config: Any, environment: str) -> str:
    config_key = str(definition.get("config_key") or "").strip()
    default_value = str(definition.get("default_value") or "TRUE")
    if config_key and _config_has_record(config, config_key, environment):
        return str(config.get_for_environment(config_key, environment, default_value) or default_value)

    alias_values: list[str] = []
    for alias_key in definition.get("deprecated_config_keys") or []:
        alias_text = str(alias_key or "").strip()
        if not alias_text or not _config_has_record(config, alias_text, environment):
            continue
        alias_values.append(str(config.get_for_environment(alias_text, environment, default_value) or default_value))
    if alias_values:
        return "TRUE" if any(is_truthy_config_value(value, default_value) for value in alias_values) else "FALSE"

    return str(config.get_for_environment(config_key, environment, default_value) or default_value)


def _schedule_description(definition: dict[str, Any]) -> str:
    schedules = definition.get("schedules") if isinstance(definition.get("schedules"), list) else []
    if not schedules:
        return f"{definition.get('cron_expr') or ''}；时区: {definition.get('cron_timezone') or 'UTC'}"
    parts = []
    for schedule in schedules:
        label = str((schedule or {}).get("label") or (schedule or {}).get("id") or "").strip()
        parts.append(
            f"{(schedule or {}).get('id')}: {(schedule or {}).get('cron_expr')} "
            f"[{(schedule or {}).get('cron_timezone') or 'UTC'}]"
            f"{' ' + label if label else ''}"
        )
    return "；".join(parts)


def build_effective_cron_definition(definition: dict[str, Any], config, environment: str, job_state: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    scheduler_raw = str(config.get_for_environment("pb_scheduler_enabled", runtime_environment, "TRUE") or "TRUE")
    cron_raw = _definition_config_value(definition, config, runtime_environment)
    scheduler_enabled = is_truthy_config_value(scheduler_raw)
    cron_enabled = is_truthy_config_value(cron_raw, definition["default_value"])
    state = job_state if isinstance(job_state, dict) else {}
    schedules = definition.get("schedules") if isinstance(definition.get("schedules"), list) else []
    return {
        **definition,
        "environment": runtime_environment,
        "scheduler_raw": scheduler_raw,
        "cron_raw": cron_raw,
        "scheduler_enabled": scheduler_enabled,
        "cron_enabled": cron_enabled,
        "effective_enabled": scheduler_enabled and cron_enabled,
        "job_state": state,
        "schedule_count": len(schedules),
        "schedule_ids": [
            str((schedule or {}).get("id") or "").strip()
            for schedule in schedules
            if str((schedule or {}).get("id") or "").strip()
        ],
        "config_description": (
            f"{definition['function_summary']} Cron: {_schedule_description(definition)}；周期: {definition['cycle_label']} "
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


def build_cron_payload_for_modes(
    config,
    *,
    broker_mode: str,
    market_data_mode: str,
    job_states: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    state_map = job_states if isinstance(job_states, dict) else {}
    normalized_broker_mode = str(broker_mode or "live").strip().lower() or "live"
    normalized_market_data_mode = str(market_data_mode or "live").strip().lower() or "live"
    items = []
    for definition in CRON_DEFINITIONS:
        runtime_mode = mode_for_scope(definition, normalized_broker_mode, normalized_market_data_mode)
        items.append(
            {
                **build_effective_cron_definition(definition, config, runtime_mode, state_map.get(definition["id"])),
                "broker_mode": normalized_broker_mode,
                "market_data_mode": normalized_market_data_mode,
            }
        )
    return sorted(items, key=lambda item: (float(item.get("sort_order", 0) or 0), str(item.get("id") or "")))


def build_cron_families(items: list[dict[str, Any]]) -> dict[str, Any]:
    families: dict[str, Any] = {}
    for item in items if isinstance(items, list) else []:
        family = str((item or {}).get("family") or "default").strip() or "default"
        bucket = families.setdefault(
            family,
            {
                "family": family,
                "job_ids": [],
                "enabled_job_count": 0,
                "schedule_count": 0,
            },
        )
        bucket["job_ids"].append(str((item or {}).get("id") or ""))
        if bool((item or {}).get("effective_enabled")):
            bucket["enabled_job_count"] += 1
        bucket["schedule_count"] += int((item or {}).get("schedule_count") or 0)
    return families
