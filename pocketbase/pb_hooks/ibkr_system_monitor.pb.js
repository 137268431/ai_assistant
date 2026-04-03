/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_system_monitor.pb.js
 * 系统监控: 事件接收 + 健康聚合 + 心跳 cron + 每日汇总
 */

console.log("[IBKRSystemMonitor] Hook 文件开始加载...")

const MONITORED_INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]

function getRuntimeKeys() {
    return ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
}

function parseHttpJson(resp) {
    if (!resp) return {}
    const raw = typeof resp.raw === "string" ? resp.raw : String(resp.raw || "")
    return raw ? JSON.parse(raw) : {}
}

function fetchComputeJson(path, timeoutSeconds, environment) {
    try {
        const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputePublicUrl(environment || "live", "https://qc.lzw-glory.top")
        const resp = $http.send({ url: `${computeBaseUrl}${path}`, method: "GET", timeout: timeoutSeconds || 5 })
        if (resp.statusCode === 200) {
            return parseHttpJson(resp)
        }
        return { ok: false, status: "error", code: resp.statusCode }
    } catch (err) {
        return { ok: false, status: "offline", error: err.message }
    }
}

function loadComputeSnapshot(environment) {
    const health = fetchComputeJson("/health", 5, environment)
    const status = fetchComputeJson("/status", 5, environment)
    const engineMap = status && status.engines && typeof status.engines === "object" ? status.engines : {}
    const totalEngines = Number(status.total_engines || 0) || Object.keys(engineMap).length

    return {
        ok: health.ok !== false || status.ok !== false,
        status: health.status || status.status || "unknown",
        code: health.code || status.code || 0,
        error: health.error || status.error || "",
        engines: engineMap,
        total_engines: totalEngines,
        ready_engines: Number(status.ready_engines || 0) || 0,
        compute_count: Number(health.compute_count || status.compute_count || 0) || 0,
        error_count: Number(health.error_count || status.error_count || 0) || 0,
        uptime_s: Number(health.uptime_s || 0) || 0,
        last_compute: health.last_compute || status.last_compute || null,
        last_scan: health.last_scan || status.last_scan || null,
        compute_enabled: status.compute_enabled,
        compute_enabled_by_environment: status.compute_enabled_by_environment || {},
        supported_environments: status.supported_environments || [],
        default_environments: status.default_environments || [],
    }
}

function loadFreshness(environment) {
    const freshness = {}
    for (let i = 0; i < MONITORED_INTERVALS.length; i++) {
        const tf = MONITORED_INTERVALS[i]
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "interval = {:i} && environment = {:env}", "-bar_time_ms", 1, 0, { i: tf, env: environment })
            if (bars && bars.length > 0) {
                const ms = Number(bars[0].get("bar_time_ms") || 0)
                freshness[tf] = {
                    last_bar_time_ms: ms,
                    age_min: Math.round((Date.now() - ms) / 60000),
                    symbol: bars[0].get("symbol") || "",
                }
            }
        } catch (_) {}
    }
    return freshness
}

function logRouteError(route, err) {
    const message = err && err.message ? err.message : String(err || "unknown error")
    console.log(`[IBKRSystemMonitor] ${route} error: ${message}`)
    if (err && err.stack) {
        console.log(err.stack)
    }
    return message
}

routerAdd("POST", "/api/custom/system/event", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)

    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const rawTitle = d.title || ""
    const rawDetail = d.detail || {}
    const eventType = d.event_type || "status_change"
    const level = d.level || "info"
    const source = d.source || "pb"
    const title = labelTitleWithEnvironment(rawTitle, environment)
    const detail = addEnvironmentToDetail(rawDetail, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    let notified = false
    if (level === "error" || level === "warning" || eventType === "status_change" || eventType === "daily_report") {
        notified = feishuSystem.notifySystemEvent(eventType, level, source, title, detail, environment)
    }

    writeSystemEvent(eventType, level, source, rawTitle, rawDetail, environment, notified)
    return c.json(200, { ok: true, notified: notified })
})

routerAdd("GET", "/api/custom/system/healthz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, getConfigValue, getIbkrComputePublicUrl, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        let compute = {
            status: "offline",
            engines: 0,
            ready_engines: 0,
            total_engines: 0,
            compute_count: 0,
            error_count: 0,
            uptime_s: 0,
            last_compute: null,
            last_scan: null,
        }
        try {
            const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
            const healthResp = $http.send({ url: `${computeBaseUrl}/health`, method: "GET", timeout: 5 })
            const healthData = JSON.parse(healthResp.raw || "{}")
            const statusResp = $http.send({ url: `${computeBaseUrl}/status`, method: "GET", timeout: 5 })
            const statusData = JSON.parse(statusResp.raw || "{}")
            compute = {
                status: healthData.status || statusData.status || "unknown",
                engines: Number(statusData.total_engines || 0) || 0,
                ready_engines: Number(statusData.ready_engines || 0) || 0,
                total_engines: Number(statusData.total_engines || 0) || 0,
                compute_count: Number(healthData.compute_count || statusData.compute_count || 0) || 0,
                error_count: Number(healthData.error_count || statusData.error_count || 0) || 0,
                uptime_s: Number(healthData.uptime_s || 0) || 0,
                last_compute: healthData.last_compute || statusData.last_compute || null,
                last_scan: healthData.last_scan || statusData.last_scan || null,
            }
        } catch (err) {
            compute.status = "offline"
            compute.error = err.message || String(err)
        }

        let dataHealth = { status: "no_data" }
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: environment })
            if (bars && bars.length > 0) {
                const lastMs = Number(bars[0].get("bar_time_ms") || 0)
                const ageMin = Math.round((Date.now() - lastMs) / 60000)
                dataHealth = {
                    status: ageMin <= 5 ? "online" : (ageMin <= 15 ? "delayed" : "offline"),
                    last_bar_age_min: ageMin,
                    last_bar_time_ms: lastMs,
                    last_symbol: bars[0].get("symbol") || "",
                }
            }
        } catch (err) {
            dataHealth = { status: "unknown", error: err.message }
        }

        const computeEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_compute_enabled", "TRUE", environment)).trim().toLowerCase() !== "false"
        const writeMode = environment === BACKTEST_ENVIRONMENT
            ? "disabled"
            : String(getConfigValue("ibkr_write_mode", "shadow", environment) || "shadow")

        return c.json(200, {
            ok: compute.status === "running",
            environment: environment,
            pb: { status: "running" },
            ibkr_compute: {
                status: compute.status || "unknown",
                engines: compute.total_engines || 0,
                ready_engines: compute.ready_engines || 0,
                total_engines: compute.total_engines || 0,
                compute_count: compute.compute_count || 0,
                error_count: compute.error_count || 0,
                uptime_s: compute.uptime_s || 0,
                last_compute: compute.last_compute || null,
                last_scan: compute.last_scan || null,
            },
            ibkr_data: dataHealth,
            write_mode: writeMode,
            compute_enabled: computeEnabled,
        })
    } catch (err) {
        return c.json(500, {
            ok: false,
            error: logRouteError("/api/custom/system/healthz", err),
        })
    }
})

routerAdd("GET", "/api/custom/system/summaryz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, getConfigValue, getIbkrComputePublicUrl, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
        const times = getTimeStrings()
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        const todayStart = times.date + " 00:00:00"
        const computeEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_compute_enabled", "TRUE", environment)).trim().toLowerCase() !== "false"
        const tradingEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_trading_enabled", getConfigValue("trading_enabled", "TRUE", environment), environment)).trim().toLowerCase() !== "false"
        const writeMode = environment === BACKTEST_ENVIRONMENT
            ? "disabled"
            : String(getConfigValue("ibkr_write_mode", "shadow", environment) || "shadow")

        let computeSummary = {
            ok: false,
            status: "offline",
            engines: {},
            total_engines: 0,
            ready_engines: 0,
            compute_count: 0,
            error_count: 0,
            uptime_s: 0,
            last_compute: null,
            last_scan: null,
        }
        try {
            const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
            const healthResp = $http.send({ url: `${computeBaseUrl}/health`, method: "GET", timeout: 5 })
            const healthData = JSON.parse(healthResp.raw || "{}")
            const statusResp = $http.send({ url: `${computeBaseUrl}/status`, method: "GET", timeout: 5 })
            const statusData = JSON.parse(statusResp.raw || "{}")
            computeSummary = {
                ok: healthData.ok !== false || statusData.ok !== false,
                status: healthData.status || statusData.status || "unknown",
                engines: statusData.engines || {},
                total_engines: Number(statusData.total_engines || 0) || 0,
                ready_engines: Number(statusData.ready_engines || 0) || 0,
                compute_count: Number(healthData.compute_count || statusData.compute_count || 0) || 0,
                error_count: Number(healthData.error_count || statusData.error_count || 0) || 0,
                uptime_s: Number(healthData.uptime_s || 0) || 0,
                last_compute: healthData.last_compute || statusData.last_compute || null,
                last_scan: healthData.last_scan || statusData.last_scan || null,
            }
        } catch (err) {
            computeSummary.error = err.message || String(err)
        }
        const dataFreshness = []

        const summary = {
            timestamp: times.us,
            environment: environment,
            write_mode: writeMode,
            compute_enabled: computeEnabled,
            ibkr_trading_enabled: tradingEnabled,
            daily_target_filter: false,
            max_positions: "3",
            config: {},
            today: { ibkr_signals: 0, ibkr_indicators: 0, orders: 0, ibkr_bars: 0, ibkr_targets: 0, events: 0 },
            ibkr_compute: computeSummary,
            recent_events: [],
            data_freshness: dataFreshness,
        }

        try {
            const configs = $app.findRecordsByFilter("config", "environment = {:env} || environment = 'global' || environment = ''", "", 200, 0, { env: environment }) || []
            for (let i = 0; i < configs.length; i++) {
                const key = configs[i].get("key")
                if (key) {
                    summary.config[String(key)] = String(configs[i].get("value") || "")
                }
            }
            if (summary.config.ibkr_write_mode) summary.write_mode = summary.config.ibkr_write_mode
            if (summary.config.ibkr_compute_enabled) summary.compute_enabled = String(summary.config.ibkr_compute_enabled).trim().toLowerCase() === "true"
            if (summary.config.ibkr_trading_enabled) {
                summary.ibkr_trading_enabled = String(summary.config.ibkr_trading_enabled).trim().toLowerCase() === "true"
            } else if (summary.config.trading_enabled) {
                summary.ibkr_trading_enabled = String(summary.config.trading_enabled).trim().toLowerCase() === "true"
            }
            if (summary.config.max_positions) summary.max_positions = summary.config.max_positions
            if (summary.config.ibkr_target_filter_on) summary.daily_target_filter = String(summary.config.ibkr_target_filter_on).trim().toLowerCase() === "true"
        } catch (_) {}

        try {
            const records = $app.findRecordsByFilter("ibkr_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            summary.today.ibkr_signals = records ? records.length : 0
        } catch (_) {}
        try {
            const records = $app.findRecordsByFilter("ibkr_indicators", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            summary.today.ibkr_indicators = records ? records.length : 0
        } catch (_) {}
        try {
            const records = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            summary.today.orders = records ? records.length : 0
        } catch (_) {}
        try {
            const records = $app.findRecordsByFilter("ibkr_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            summary.today.ibkr_bars = records ? records.length : 0
        } catch (_) {}
        try {
            const records = $app.findRecordsByFilter("ibkr_targets", "date = {:d} && environment = {:env}", "", 0, 0, { d: times.date, env: environment })
            summary.today.ibkr_targets = records ? records.length : 0
        } catch (_) {}
        try {
            const records = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            summary.today.events = records ? records.length : 0
        } catch (_) {}

        try {
            const recent = $app.findRecordsByFilter("system_events", "environment = {:env}", "-created", 20, 0, { env: environment }) || []
            for (let i = 0; i < recent.length; i++) {
                summary.recent_events.push({
                    id: String(recent[i].getId() || ""),
                    event_type: String(recent[i].get("event_type") || ""),
                    level: String(recent[i].get("level") || ""),
                    source: String(recent[i].get("source") || ""),
                    environment: String(recent[i].get("environment") || environment),
                    title: String(recent[i].get("title") || ""),
                    notified: Boolean(recent[i].get("notified")),
                    us_time: String(recent[i].get("us_time") || ""),
                    created: String(recent[i].get("created") || ""),
                })
            }
        } catch (_) {}
        return c.json(200, JSON.parse(JSON.stringify(summary)))
    } catch (err) {
        return c.json(500, {
            ok: false,
            error: logRouteError("/api/custom/system/summaryz", err),
        })
    }
})

cronAdd("system_heartbeat", "*/5 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment, getEffectiveWriteMode } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const backtestKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(backtestKeys)
    const compute = loadComputeSnapshot("live")

    if (compute.status !== "running") {
        for (let i = 0; i < environments.length; i++) {
            const environment = environments[i]
            if (!getComputeEnabledForEnvironment(environment, backtestKeys)) continue
            feishuSystem.notifyAlert("ibkr_compute", "IBKR Compute 服务离线", {
                "检查时间": times.us,
                "建议": "检查 systemctl status ibkr-compute",
            }, environment)
            writeSystemEvent("alert", "error", "ibkr_compute", "IBKR Compute 服务离线", { check_time: times.us }, environment, true)
        }
        return
    }

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: environment }) || []
            if (bars.length > 0) {
                const ageMin = Math.round((Date.now() - Number(bars[0].get("bar_time_ms") || 0)) / 60000)
                if (ageMin > 10) {
                    feishuSystem.notifyWarning("ibkr_compute", "IBKR 数据延迟", {
                        "延迟": ageMin + " 分钟",
                        "最后标的": bars[0].get("symbol") || "",
                        "检查时间": times.us,
                    }, environment)
                }
            }
        } catch (_) {}

        if (new Date().getMinutes() < 5 && (getComputeEnabledForEnvironment(environment, backtestKeys) || getTradingEnabledForEnvironment(environment, backtestKeys))) {
            feishuSystem.notifyHeartbeat("pb", "ok", {
                environment: environment,
                ibkr_compute: "running",
                write_mode: getEffectiveWriteMode(environment, backtestKeys),
                trading: getTradingEnabledForEnvironment(environment, backtestKeys) ? "true" : "false",
            })
        }
    }
})

cronAdd("system_daily_report", "5 20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const times = getTimeStrings()
    const todayStart = times.date + " 00:00:00"
    const environments = getActiveRuntimeEnvironments(getRuntimeKeys())

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        const report = {
            "日期": times.date,
            "信号数": "0",
            "订单数": "0",
            "IBKR Bars": "0",
            "Targets": "0",
            "系统事件": "0",
            "错误事件": "0",
        }

        try {
            const rows = $app.findRecordsByFilter("ibkr_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["信号数"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["订单数"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("ibkr_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["IBKR Bars"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("ibkr_targets", "date = {:d} && environment = {:env}", "", 0, 0, { d: times.date, env: environment }) || []
            report["Targets"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["系统事件"] = String(rows.length)
            let errorCount = 0
            for (let j = 0; j < rows.length; j++) {
                if (rows[j].get("level") === "error") errorCount += 1
            }
            report["错误事件"] = String(errorCount)
        } catch (_) {}

        feishuSystem.notifyDailyReport(report, environment)
    }
})

console.log("[IBKRSystemMonitor] Hook 文件加载完成")
