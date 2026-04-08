const PB_CRON_DEFINITIONS = [
    {
        id: "signal_expiry_check",
        config_key: "pb_cron_signal_expiry_enabled",
        display_name: "信号过期清理 Cron",
        config_display_name: "信号过期清理",
        group_name: "PB Cron 调度",
        sort_order: 130,
        default_value: "TRUE",
        cron_expr: "*/5 * * * *",
        cycle_label: "每 5 分钟",
        beijing_cycle_label: "每 5 分钟",
        et_cycle_label: "每 5 分钟",
        window_label: "全天",
        function_summary: "扫描 pending / awaiting_confirm 信号，超时后自动标记为 expired。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关和本开关共同控制。",
        hook_file: "ibkr_signal_scheduler.pb.js",
    },
    {
        id: "order_expiry_check",
        config_key: "pb_cron_order_expiry_enabled",
        display_name: "订单过期取消 Cron",
        config_display_name: "订单过期取消",
        group_name: "PB Cron 调度",
        sort_order: 131,
        default_value: "TRUE",
        cron_expr: "*/5 * * * *",
        cycle_label: "每 5 分钟",
        beijing_cycle_label: "每 5 分钟",
        et_cycle_label: "每 5 分钟",
        window_label: "全天",
        function_summary: "扫描 Init / Submitted 订单，超时后自动标记为 Canceled。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关和本开关共同控制。",
        hook_file: "order_scheduler.pb.js",
    },
    {
        id: "ibkr_compute_runtime",
        config_key: "pb_cron_ibkr_compute_runtime_enabled",
        display_name: "Compute / 系统状态摘要 Cron",
        config_display_name: "Compute + 状态摘要",
        group_name: "PB Cron 调度",
        sort_order: 132,
        default_value: "TRUE",
        cron_expr: "*/5 4-20 * * 1-5",
        cycle_label: "工作日 UTC 04:00-20:55 每 5 分钟",
        beijing_cycle_label: "北京时间 周一至周五 12:00-23:55，且延续到次日 04:55（周六凌晨结束）",
        et_cycle_label: "美东时间 夏令时 EDT 周一至周五 00:00-16:55；冬令时 EST 前一日 23:00-当日 15:55（跨日）",
        window_label: "整点发送合并状态摘要，:30 再发送一次状态摘要；同轮心跳/健康检查并入摘要，异常巡检告警仍即时单发",
        function_summary: "触发 compute 调度，并串行执行系统健康检查与状态摘要通知。",
        scope_label: "按环境执行",
        note: "状态摘要在 :00 / :30 合并同轮心跳和健康检查结果；若状态摘要关闭，整点 ok 心跳才会回退到 health_check_notify_enabled；warning / error 仍受 inspection_notify_enabled 控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "ibkr_scan_runtime",
        config_key: "pb_cron_ibkr_scan_runtime_enabled",
        display_name: "盘前 Scan Cron",
        config_display_name: "盘前 Scan",
        group_name: "PB Cron 调度",
        sort_order: 133,
        default_value: "TRUE",
        cron_expr: "*/5 7-9 * * 1-5",
        cycle_label: "工作日 UTC 07:00-09:55 每 5 分钟",
        beijing_cycle_label: "北京时间 工作日 15:00-17:55 每 5 分钟",
        et_cycle_label: "美东时间 夏令时 EDT 工作日 03:00-05:55；冬令时 EST 工作日 02:00-04:55",
        window_label: "盘前窗口",
        function_summary: "触发开盘前 scan 调度，刷新当天候选信号与市场扫描结果。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关、Compute 开关和本开关共同控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "ibkr_auth_pending_guard",
        config_key: "pb_cron_ibkr_auth_pending_guard_enabled",
        display_name: "2FA 长时间未恢复巡检 Cron",
        config_display_name: "2FA 长时间未恢复巡检",
        group_name: "PB Cron 调度",
        sort_order: 135,
        default_value: "TRUE",
        cron_expr: "*/10 4-20 * * 1-5",
        cycle_label: "工作日 UTC 04:00-20:50 每 10 分钟",
        beijing_cycle_label: "北京时间 周一至周五 12:00-23:50，且延续到次日 04:50（周六凌晨结束）",
        et_cycle_label: "美东时间 夏令时 EDT 周一至周五 00:00-16:50；冬令时 EST 前一日 23:00-当日 15:50（跨日）",
        window_label: "盘前到盘后",
        function_summary: "巡检 Session / 2FA 长时间未恢复状态，并在需要时发出系统告警。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关和本开关共同控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "ibkr_auth_edge_guard",
        config_key: "pb_cron_ibkr_auth_edge_guard_enabled",
        display_name: "2FA / Session 即时巡检 Cron",
        config_display_name: "2FA 即时巡检",
        group_name: "PB Cron 调度",
        sort_order: 134,
        default_value: "TRUE",
        cron_expr: "* 4-20 * * 1-5",
        cycle_label: "工作日 UTC 04:00-20:59 每 1 分钟",
        beijing_cycle_label: "北京时间 周一至周五 12:00-23:59，且延续到次日 04:59（周六凌晨结束）",
        et_cycle_label: "美东时间 夏令时 EDT 周一至周五 00:00-16:59；冬令时 EST 前一日 23:00-当日 15:59（跨日）",
        window_label: "盘前到盘后",
        function_summary: "巡检 Session / 2FA 的边沿变化，并在会话失效、401 或进入待验证状态时立即告警。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关和本开关共同控制；长时间未恢复仍由 2FA 长时间未恢复巡检继续补报。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "system_data_gap_guard",
        config_key: "pb_cron_system_data_gap_guard_enabled",
        display_name: "数据缺口巡检 Cron",
        config_display_name: "数据缺口巡检",
        group_name: "PB Cron 调度",
        sort_order: 136,
        default_value: "TRUE",
        cron_expr: "*/10 4-20 * * 1-5",
        cycle_label: "工作日 UTC 04:00-20:50 每 10 分钟",
        beijing_cycle_label: "北京时间 周一至周五 12:00-23:50，且延续到次日 04:50（周六凌晨结束）",
        et_cycle_label: "美东时间 夏令时 EDT 周一至周五 00:00-16:50；冬令时 EST 前一日 23:00-当日 15:50（跨日）",
        window_label: "盘前到盘后",
        function_summary: "巡检 bars / indicators / 序列缺口，并在检测到市场活动异常时发出告警。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关、Compute 开关和本开关共同控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "ibkr_2fa_hourly_check",
        config_key: "pb_cron_ibkr_2fa_hourly_check_enabled",
        display_name: "2FA 每小时提醒 Cron",
        config_display_name: "2FA 每小时提醒",
        group_name: "PB Cron 调度",
        sort_order: 137,
        default_value: "TRUE",
        cron_expr: "5 4-20 * * 1-5",
        cycle_label: "工作日 UTC 每小时 05 分",
        beijing_cycle_label: "北京时间 周一至周五 12:05-23:05，且延续到次日 04:05（周六凌晨结束）",
        et_cycle_label: "美东时间 夏令时 EDT 周一至周五 00:05-16:05 每小时；冬令时 EST 前一日 23:05-当日 15:05（跨日）",
        window_label: "盘前到盘后",
        function_summary: "若 2FA 仍未恢复，则按小时补发飞书验证卡片提醒。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关和本开关共同控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "system_market_open_reminder",
        config_key: "pb_cron_system_market_open_reminder_enabled",
        display_name: "开盘前状态提醒 Cron",
        config_display_name: "开盘前状态提醒",
        group_name: "PB Cron 调度",
        sort_order: 138,
        default_value: "TRUE",
        cron_expr: "*/5 * * * *",
        cycle_label: "每 5 分钟轮询一次；内部按 ET 09:20 仅发送一次",
        beijing_cycle_label: "北京时间 全天轮询；内部按美东 09:20 发送开盘前状态，周末/非交易日改发闭市提醒",
        et_cycle_label: "美东时间 全天轮询；09:20 发送开盘前系统状态，非交易日按闭市/休市提醒处理",
        window_label: "每日 ET 09:20",
        function_summary: "每日 09:20 发送开盘前系统状态；若为非交易日则发送闭市/休市提醒。",
        scope_label: "按环境执行",
        note: "受 PB 调度总开关、本开关和 status_notify_enabled 共同控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
    {
        id: "system_daily_report",
        config_key: "pb_cron_system_daily_report_enabled",
        display_name: "系统日报 Cron",
        config_display_name: "系统日报",
        group_name: "PB Cron 调度",
        sort_order: 139,
        default_value: "TRUE",
        cron_expr: "*/5 * * * *",
        cycle_label: "每 5 分钟轮询一次；内部按 ET 16:05 仅发送一次",
        beijing_cycle_label: "北京时间 全天轮询；内部按美东收盘后生成汇总，周末/非交易日也会发送闭市汇总",
        et_cycle_label: "美东时间 全天轮询；16:05 发送收盘汇总，周末/非交易日按闭市日汇总处理",
        window_label: "每日 ET 16:05",
        function_summary: "汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报。",
        scope_label: "按环境执行",
        note: "日报是否真正发送，还受 daily_summary_notify_enabled 控制。",
        hook_file: "ibkr_system_monitor.pb.js",
    },
]

function cloneDefinition(definition) {
    return {
        id: definition.id,
        config_key: definition.config_key,
        display_name: definition.display_name,
        config_display_name: definition.config_display_name,
        group_name: definition.group_name,
        sort_order: definition.sort_order,
        default_value: definition.default_value,
        cron_expr: definition.cron_expr,
        cycle_label: definition.cycle_label,
        beijing_cycle_label: definition.beijing_cycle_label,
        et_cycle_label: definition.et_cycle_label,
        window_label: definition.window_label,
        function_summary: definition.function_summary,
        scope_label: definition.scope_label,
        note: definition.note,
        hook_file: definition.hook_file,
        config_description: `${definition.function_summary} Cron: ${definition.cron_expr}；UTC 周期: ${definition.cycle_label}${definition.beijing_cycle_label ? `；北京时间: ${definition.beijing_cycle_label}` : ""}${definition.et_cycle_label ? `；美东时间: ${definition.et_cycle_label}` : ""}${definition.window_label ? `；时间窗口: ${definition.window_label}` : ""} ${definition.note}`.trim(),
    }
}

function getPbCronDefinitions() {
    return PB_CRON_DEFINITIONS.map(cloneDefinition)
}

function getPbCronDefinition(cronId) {
    if (!cronId) return null
    for (let i = 0; i < PB_CRON_DEFINITIONS.length; i++) {
        if (PB_CRON_DEFINITIONS[i].id === cronId) {
            return PB_CRON_DEFINITIONS[i]
        }
    }
    return null
}

function getPbCronToggleState(cronId, environment) {
    const definition = getPbCronDefinition(cronId)
    const { getConfigValue, normalizeRuntimeEnvironment, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment || "", LIVE_ENVIRONMENT)
    const schedulerRaw = String(getConfigValue("pb_scheduler_enabled", "TRUE", runtimeEnvironment) || "TRUE")
    const cronRaw = String(getConfigValue(definition && definition.config_key, definition && definition.default_value || "TRUE", runtimeEnvironment) || (definition && definition.default_value || "TRUE"))
    const schedulerEnabled = isEnabledConfigValue(schedulerRaw)
    const cronEnabled = isEnabledConfigValue(cronRaw)

    return {
        cron_id: definition ? definition.id : String(cronId || ""),
        config_key: definition ? definition.config_key : "",
        environment: runtimeEnvironment,
        scheduler_raw: schedulerRaw,
        cron_raw: cronRaw,
        scheduler_enabled: schedulerEnabled,
        cron_enabled: cronEnabled,
        effective_enabled: schedulerEnabled && cronEnabled,
    }
}

function isPbCronEnabled(cronId, environment) {
    return getPbCronToggleState(cronId, environment).effective_enabled
}

module.exports = {
    getPbCronDefinitions,
    getPbCronDefinition,
    getPbCronToggleState,
    isPbCronEnabled,
}
