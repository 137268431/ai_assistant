/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_system_monitor.pb.js
 * system routes / cron 全部迁到 ibkr-api + ibkr-scheduler；
 * PB 这里仅保留兼容代理，以及尚未迁完的 piggyback notify 壳。
 */

console.log("[IBKRSystemMonitor] Hook 文件开始加载...")

routerAdd("POST", "/api/custom/system/event", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const body = reqInfo.body || reqInfo.data || {}
    return proxyIbkrApiJson(c, "/api/custom/system/event", {
        method: "POST",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        body: body,
        timeout: 10,
    })
})

routerAdd("GET", "/api/custom/system/cronz", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return proxyIbkrApiJson(c, "/api/custom/system/cronz", {
        method: "GET",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        timeout: 10,
    })
})

routerAdd("GET", "/api/custom/system/healthz", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return proxyIbkrApiJson(c, "/api/custom/system/healthz", {
        method: "GET",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        timeout: 10,
    })
})

routerAdd("GET", "/api/custom/system/summaryz", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const query = c.request.url.query()
    return proxyIbkrApiJson(c, "/api/custom/system/summaryz", {
        method: "GET",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        query: {
            lite: query.get("lite") || "",
        },
        timeout: 10,
    })
})

routerAdd("GET", "/api/custom/system/monitorz", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return proxyIbkrApiJson(c, "/api/custom/system/monitorz", {
        method: "GET",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        timeout: 10,
    })
})

cronAdd("ibkr_compute_runtime", "*/5 4-20 * * 1-5", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    try {
        for (const environment of getIbkrSchedulerEnvironments("ibkr_compute_runtime")) {
            proxySchedulerCronJob("ibkr_compute_runtime", {
                environment: environment,
                timeout: 90,
                logPrefix: "[IBKRComputeCron]",
            })
        }
    } catch (err) {
        console.log(`[IBKRComputeCron] compute dispatch error: ${err.message || err}`)
    }
    try {
        const systemNotify = require(`${__hooks}/lib/system_notify_scheduler.js`)
        const monitorAlertGuard = require(`${__hooks}/lib/system_monitor_alert_guard.js`)
        systemNotify.runSystemHeartbeatTick("[IBKRComputeCron]", "ibkr_compute_runtime")
        monitorAlertGuard.runSystemMonitorAlertGuard("[IBKRMonitorAlert]")
        const minute = new Date().getMinutes()
        if (minute === 0 || minute === 30) {
            systemNotify.runSystemStatusReminderTick("[IBKRComputeCron]", "ibkr_compute_runtime")
        }
    } catch (err) {
        console.log(`[IBKRComputeCron] system notify error: ${err.message || err}`)
    }
})

cronAdd("ibkr_scan_runtime", "*/5 7-9 * * 1-5", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getIbkrSchedulerEnvironments("ibkr_scan_runtime")) {
        proxySchedulerCronJob("ibkr_scan_runtime", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKRScanCron]",
        })
    }
    try {
        require(`${__hooks}/lib/system_notify_scheduler.js`).runDailyScanSummaryTick("[IBKRScanSummary]", "ibkr_scan_runtime")
    } catch (err) {
        console.log(`[IBKRScanSummary] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_history_retention", "10 * * * *", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    try {
        for (const environment of getIbkrSchedulerEnvironments("ibkr_history_retention")) {
            proxySchedulerCronJob("ibkr_history_retention", {
                environment: environment,
                timeout: 120,
                logPrefix: "[IBKRHistoryRetention]",
            })
        }
    } catch (err) {
        console.log(`[IBKRHistoryRetention] fatal error: ${err.message || err}`)
    }
})

cronAdd("system_market_open_reminder", "*/5 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("system_market_open_reminder", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKROpenReminder]",
        })
    }
})

cronAdd("ibkr_auth_edge_guard", "* 4-20 * * 1-5", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("ibkr_auth_edge_guard", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKRAuthEdgeGuard]",
        })
    }
})

cronAdd("ibkr_auth_pending_guard", "*/10 4-20 * * 1-5", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("ibkr_auth_pending_guard", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKRAuthPendingGuard]",
        })
    }
})

cronAdd("system_data_gap_guard", "*/10 4-20 * * 1-5", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("system_data_gap_guard", {
            environment: environment,
            timeout: 90,
            logPrefix: "[SystemDataGapGuard]",
        })
    }
})

cronAdd("ibkr_data_quality_open_sweep", "40 9 * * 1-5", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    try {
        for (const environment of getIbkrSchedulerEnvironments("ibkr_data_quality_open_sweep")) {
            proxySchedulerCronJob("ibkr_data_quality_open_sweep", {
                environment: environment,
                timeout: 180,
                logPrefix: "[IBKRDataQualityOpenSweep]",
            })
        }
    } catch (err) {
        console.log(`[IBKRDataQualityOpenSweep] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_data_quality_close_sweep", "10 20 * * 1-5", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    try {
        for (const environment of getIbkrSchedulerEnvironments("ibkr_data_quality_close_sweep")) {
            proxySchedulerCronJob("ibkr_data_quality_close_sweep", {
                environment: environment,
                timeout: 180,
                logPrefix: "[IBKRDataQualityCloseSweep]",
            })
        }
    } catch (err) {
        console.log(`[IBKRDataQualityCloseSweep] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_data_quality_truth_audit", "20 20 * * 1-5", () => {
    const { getIbkrSchedulerEnvironments } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    try {
        for (const environment of getIbkrSchedulerEnvironments("ibkr_data_quality_truth_audit")) {
            proxySchedulerCronJob("ibkr_data_quality_truth_audit", {
                environment: environment,
                timeout: 180,
                logPrefix: "[IBKRDataQualityTruthAudit]",
            })
        }
    } catch (err) {
        console.log(`[IBKRDataQualityTruthAudit] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_2fa_hourly_check", "5 4-20 * * 1-5", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("ibkr_2fa_hourly_check", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKR2FAHourly]",
        })
    }
})

cronAdd("ibkr_weekly_reauth_reminder", "0 5 * * 1", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("ibkr_weekly_reauth_reminder", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKRWeekly2FA]",
        })
    }
})

cronAdd("ibkr_weekly_reauth_followup", "30 7 * * 1", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("ibkr_weekly_reauth_followup", {
            environment: environment,
            timeout: 60,
            logPrefix: "[IBKRWeekly2FAFollowup]",
        })
    }
})

cronAdd("system_daily_report", "*/5 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)
    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("system_daily_report", {
            environment: environment,
            timeout: 60,
            logPrefix: "[SystemDailyReport]",
        })
    }
})

console.log("[IBKRSystemMonitor] Hook 文件加载完成")
