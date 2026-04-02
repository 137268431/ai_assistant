/// <reference path="./pb_data/types.d.ts" />

/**
 * system_monitor.pb.js
 * 系统监控: 事件接收 + 健康聚合 + 心跳Cron + 每日汇总
 */

console.log("[SystemMonitor] Hook 文件开始加载...")

const MONITOR_BACKTEST_KEYS = ["qc_compute_enabled", "trading_enabled", "pb_scheduler_enabled"]

// ══════════════════════════════════════
// POST /api/custom/system/event
// 接收系统事件 → 写表 + 按level发飞书
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/system/event", (c) => {
    var feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)

    var reqInfo = c.requestInfo()
    var d = reqInfo.body || reqInfo.data || {}
    var environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    var rawTitle = d.title || ""
    var rawDetail = d.detail || {}

    var eventType = d.event_type || "status_change"
    var level = d.level || "info"
    var source = d.source || "pb"
    var title = labelTitleWithEnvironment(rawTitle, environment)
    var detail = addEnvironmentToDetail(rawDetail, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    // 发飞书通知: warning/error 即时发送, info 只记录不发
    var notified = false
    if (level === "error" || level === "warning") {
        notified = feishuSystem.notifySystemEvent(eventType, level, source, title, detail, environment)
    } else if (eventType === "status_change" || eventType === "daily_report") {
        notified = feishuSystem.notifySystemEvent(eventType, level, source, title, detail, environment)
    }

    writeSystemEvent(eventType, level, source, rawTitle, rawDetail, environment, notified)

    return c.json(200, { ok: true, notified: notified })
})

// ══════════════════════════════════════
// GET /api/custom/system/health
// 聚合健康检查
// ══════════════════════════════════════
routerAdd("GET", "/api/custom/system/health", (c) => {
    const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getComputeEnabledForEnvironment, getEffectiveWriteMode } = require(`${__hooks}/lib/runtime_modes.js`)
    var environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
    var result = {
        ok: true,
        environment: environment,
        pb: { status: "running" },
        qc_compute: { status: "unknown" },
        qc: { status: "unknown" },
        write_mode: getEffectiveWriteMode(environment, MONITOR_BACKTEST_KEYS),
        compute_enabled: getComputeEnabledForEnvironment(environment, MONITOR_BACKTEST_KEYS)
    }

    // 检查 qc_compute
    try {
        var resp = $http.send({
            url: "http://localhost:5100/health",
            method: "GET",
            timeout: 5
        })
        if (resp.statusCode === 200) {
            var data = typeof resp.json === "function" ? resp.json() : JSON.parse(resp.raw || "{}")
            result.qc_compute = {
                status: data.status || "running",
                engines: data.engines || 0,
                compute_count: data.compute_count || 0,
                error_count: data.error_count || 0,
                uptime_s: data.uptime_s || 0,
                last_compute: data.last_compute || null
            }
        } else {
            result.qc_compute = { status: "error", code: resp.statusCode }
        }
    } catch (err) {
        result.qc_compute = { status: "offline", error: err.message }
        result.ok = false
    }

    // 检查 QC (通过 qc_bars 最新时间判断)
    try {
        var bars = $app.findRecordsByFilter("qc_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: environment })
        if (bars && bars.length > 0) {
            var lastMs = Number(bars[0].get("bar_time_ms") || 0)
            var ageMs = Date.now() - lastMs
            var ageMin = Math.round(ageMs / 60000)
            result.qc = {
                status: ageMin <= 5 ? "online" : (ageMin <= 15 ? "delayed" : "offline"),
                last_bar_age_min: ageMin,
                last_bar_time_ms: lastMs,
                last_symbol: bars[0].get("symbol") || ""
            }
        } else {
            result.qc = { status: "no_data" }
        }
    } catch (err) {
        result.qc = { status: "unknown", error: err.message }
    }

    return c.json(200, result)
})

// ══════════════════════════════════════
// GET /api/custom/system/summary
// 系统总览 (供前端 system.html 调用)
// ══════════════════════════════════════
routerAdd("GET", "/api/custom/system/summary", (c) => {
    const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT, getConfigValue } = require(`${__hooks}/lib/environment.js`)
    const { getComputeEnabledForEnvironment, getTradingEnabledForEnvironment, getEffectiveWriteMode } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    var times = getTimeStrings()
    var todayDate = times.date
    var environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)

    var summary = {
        timestamp: times.us,
        environment: environment,
        write_mode: getEffectiveWriteMode(environment, MONITOR_BACKTEST_KEYS),
        compute_enabled: getComputeEnabledForEnvironment(environment, MONITOR_BACKTEST_KEYS),
        trading_enabled: getTradingEnabledForEnvironment(environment, MONITOR_BACKTEST_KEYS),
        daily_target_filter: getConfigValue("daily_target_filter_on", "false", environment) === "true",
        max_positions: getConfigValue("max_positions", "3", environment),
        config: {},
        today: { signals: 0, orders: 0, qc_signals: 0, qc_bars: 0, daily_targets: 0, events: 0 },
        qc_compute: {},
        qc_status: {},
        recent_events: [],
        data_freshness: {}
    }

    // 关键配置
    try {
        var configs = $app.findRecordsByFilter("config", "environment = {:env} || environment = 'global' || environment = ''", "", 200, 0, { env: environment })
        for (var i = 0; i < configs.length; i++) {
            var k = configs[i].get("key")
            var v = configs[i].get("value")
            if (k) summary.config[k] = v
        }
    } catch (_) {}

    // 今日统计
    try {
        var todayStart = todayDate + " 00:00:00"
        var signals = $app.findRecordsByFilter("signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
        summary.today.signals = signals ? signals.length : 0
    } catch (_) {}

    try {
        var todayStart = todayDate + " 00:00:00"
        var orders = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
        summary.today.orders = orders ? orders.length : 0
    } catch (_) {}

    try {
        var todayStart = todayDate + " 00:00:00"
        var qcSignals = $app.findRecordsByFilter("qc_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
        summary.today.qc_signals = qcSignals ? qcSignals.length : 0
    } catch (_) {}

    try {
        var todayStart = todayDate + " 00:00:00"
        var qcBars = $app.findRecordsByFilter("qc_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
        summary.today.qc_bars = qcBars ? qcBars.length : 0
    } catch (_) {}

    try {
        var targets = $app.findRecordsByFilter("daily_targets", "date = {:d} && environment = {:env}", "", 0, 0, { d: todayDate, env: environment })
        summary.today.daily_targets = targets ? targets.length : 0
    } catch (_) {}

    try {
        var todayStart = todayDate + " 00:00:00"
        var events = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
        summary.today.events = events ? events.length : 0
    } catch (_) {}

    // qc_compute 状态
    try {
        var resp = $http.send({ url: "http://localhost:5100/status", method: "GET", timeout: 5 })
        if (resp.statusCode === 200) {
            summary.qc_compute = typeof resp.json === "function" ? resp.json() : JSON.parse(resp.raw || "{}")
        }
    } catch (_) {
        summary.qc_compute = { ok: false, status: "offline" }
    }

    // QC 连接状态 (各TF最新bar时间)
    var intervals = ["5m", "15m", "30m", "1H", "4H", "1D", "1W"]
    for (var j = 0; j < intervals.length; j++) {
        var tf = intervals[j]
        try {
            var bars = $app.findRecordsByFilter("qc_bars", "interval = {:i} && environment = {:env}", "-bar_time_ms", 1, 0, { i: tf, env: environment })
            if (bars && bars.length > 0) {
                var ms = Number(bars[0].get("bar_time_ms") || 0)
                summary.data_freshness[tf] = {
                    last_bar_time_ms: ms,
                    age_min: Math.round((Date.now() - ms) / 60000),
                    symbol: bars[0].get("symbol") || ""
                }
            }
        } catch (_) {}
    }

    // 最近系统事件
    try {
        var recent = $app.findRecordsByFilter("system_events", "environment = {:env}", "-created", 20, 0, { env: environment })
        for (var k = 0; k < recent.length; k++) {
            summary.recent_events.push({
                id: recent[k].getId(),
                event_type: recent[k].get("event_type"),
                level: recent[k].get("level"),
                source: recent[k].get("source"),
                environment: recent[k].get("environment") || environment,
                title: recent[k].get("title"),
                notified: recent[k].get("notified"),
                us_time: recent[k].get("us_time"),
                created: recent[k].get("created")
            })
        }
    } catch (_) {}

    return c.json(200, summary)
})

// ══════════════════════════════════════
// Cron: 系统心跳 (每5分钟, 4:00-20:00)
// ══════════════════════════════════════
cronAdd("system_heartbeat", "*/5 4-20 * * 1-5", () => {
    var feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment, getEffectiveWriteMode } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    var times = getTimeStrings()
    var activeEnvironments = getActiveRuntimeEnvironments(MONITOR_BACKTEST_KEYS)

    // 检查 qc_compute
    var computeOk = false
    try {
        var resp = $http.send({ url: "http://localhost:5100/health", method: "GET", timeout: 5 })
        if (resp.statusCode === 200) {
            var data = typeof resp.json === "function" ? resp.json() : JSON.parse(resp.raw || "{}")
            computeOk = data.status === "running"

            if (data.error_count > 0) {
                for (var i = 0; i < activeEnvironments.length; i++) {
                    var warningEnvironment = activeEnvironments[i]
                    if (!getComputeEnabledForEnvironment(warningEnvironment, MONITOR_BACKTEST_KEYS)) continue
                    feishuSystem.notifyWarning("qc_compute", "计算引擎存在错误", {
                        "错误次数": String(data.error_count),
                        "引擎数": String(data.engines),
                        "运行时间": String(data.uptime_s) + "s"
                    }, warningEnvironment)
                }
            }
        }
    } catch (_) {
        computeOk = false
    }

    if (!computeOk) {
        for (var j = 0; j < activeEnvironments.length; j++) {
            var alertEnvironment = activeEnvironments[j]
            if (!getComputeEnabledForEnvironment(alertEnvironment, MONITOR_BACKTEST_KEYS)) continue

            feishuSystem.notifyAlert("qc_compute", "QC Compute 服务离线", {
                "检查时间": times.us,
                "建议": "检查 systemctl status qc_compute"
            }, alertEnvironment)

            writeSystemEvent("alert", "error", "qc_compute", "QC Compute 服务离线", {
                check_time: times.us
            }, alertEnvironment, true)
        }
    }

    // 检查各环境 qc_bars 数据新鲜度
    for (var k = 0; k < activeEnvironments.length; k++) {
        var dataEnvironment = activeEnvironments[k]
        try {
            var bars = $app.findRecordsByFilter("qc_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: dataEnvironment })
            if (bars && bars.length > 0) {
                var lastMs = Number(bars[0].get("bar_time_ms") || 0)
                var ageMin = Math.round((Date.now() - lastMs) / 60000)

                if (ageMin > 10) {
                    feishuSystem.notifyWarning("qc", "QC 数据延迟", {
                        "延迟": ageMin + " 分钟",
                        "最后标的": bars[0].get("symbol") || "",
                        "检查时间": times.us
                    }, dataEnvironment)
                }
            }
        } catch (_) {}
    }

    // 整点发一次心跳汇总 (每小时第0分钟的那次5分钟检查)
    var minute = new Date().getMinutes()
    if (minute < 5 && computeOk) {
        for (var m = 0; m < activeEnvironments.length; m++) {
            var heartbeatEnvironment = activeEnvironments[m]
            if (!getComputeEnabledForEnvironment(heartbeatEnvironment, MONITOR_BACKTEST_KEYS) &&
                !getTradingEnabledForEnvironment(heartbeatEnvironment, MONITOR_BACKTEST_KEYS)) {
                continue
            }

            feishuSystem.notifyHeartbeat("pb", "ok", {
                environment: heartbeatEnvironment,
                "qc_compute": "running",
                "write_mode": getEffectiveWriteMode(heartbeatEnvironment, MONITOR_BACKTEST_KEYS),
                "trading": getTradingEnabledForEnvironment(heartbeatEnvironment, MONITOR_BACKTEST_KEYS) ? "true" : "false"
            })
        }
    }
})

// ══════════════════════════════════════
// Cron: 每日汇总 (20:05 ET)
// ══════════════════════════════════════
cronAdd("system_daily_report", "5 20 * * 1-5", () => {
    var feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    var times = getTimeStrings()
    var todayDate = times.date
    var activeEnvironments = getActiveRuntimeEnvironments(MONITOR_BACKTEST_KEYS)

    for (var i = 0; i < activeEnvironments.length; i++) {
        var environment = activeEnvironments[i]
        var report = {
            "日期": todayDate,
            "信号数": "0",
            "订单数": "0",
            "QC信号": "0",
            "QC Bars": "0",
            "系统事件": "0",
            "错误事件": "0"
        }

        try {
            var todayStart = todayDate + " 00:00:00"
            var signals = $app.findRecordsByFilter("signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            report["信号数"] = String(signals ? signals.length : 0)
        } catch (_) {}

        try {
            var todayStart = todayDate + " 00:00:00"
            var orders = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            report["订单数"] = String(orders ? orders.length : 0)
        } catch (_) {}

        try {
            var todayStart = todayDate + " 00:00:00"
            var qcSignals = $app.findRecordsByFilter("qc_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            report["QC信号"] = String(qcSignals ? qcSignals.length : 0)
        } catch (_) {}

        try {
            var todayStart = todayDate + " 00:00:00"
            var qcBars = $app.findRecordsByFilter("qc_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            report["QC Bars"] = String(qcBars ? qcBars.length : 0)
        } catch (_) {}

        try {
            var todayStart = todayDate + " 00:00:00"
            var events = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
            report["系统事件"] = String(events ? events.length : 0)

            var errorCount = 0
            for (var j = 0; j < events.length; j++) {
                if (events[j].get("level") === "error") errorCount++
            }
            report["错误事件"] = String(errorCount)
        } catch (_) {}

        feishuSystem.notifyDailyReport(report, environment)
    }
})

console.log("[SystemMonitor] Hook 文件加载完成")
