/// <reference path="./pb_data/types.d.ts" />

/**
 * qc_scheduler.pb.js
 * PB 定时调度 → 触发 qc_compute Python 服务
 */

console.log("[QCScheduler] Hook 文件开始加载...");
const envUtils = require(`${__hooks}/lib/environment.js`)
const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
const SCHEDULE_ENVIRONMENTS = [envUtils.LIVE_ENVIRONMENT, envUtils.PAPER_ENVIRONMENT, envUtils.BACKTEST_ENVIRONMENT]

function isEnabledConfigValue(value) {
    const text = String(value || "").trim().toLowerCase()
    return !(text === "false" || text === "0" || text === "off" || text === "no")
}

function isBacktestOptedIn() {
    return envUtils.hasConfigOverride("qc_compute_enabled", envUtils.BACKTEST_ENVIRONMENT) ||
        envUtils.hasConfigOverride("pb_scheduler_enabled", envUtils.BACKTEST_ENVIRONMENT)
}

function isComputeEnabledForEnvironment(environment) {
    const runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment, envUtils.LIVE_ENVIRONMENT)
    if (runtimeEnvironment === envUtils.BACKTEST_ENVIRONMENT && !isBacktestOptedIn()) {
        return false
    }
    const defaultValue = runtimeEnvironment === envUtils.BACKTEST_ENVIRONMENT ? "false" : "true"
    return isEnabledConfigValue(envUtils.getConfigValue("qc_compute_enabled", defaultValue, runtimeEnvironment))
}

function getActiveScheduleEnvironments() {
    return SCHEDULE_ENVIRONMENTS.filter((environment) => {
        if (environment === envUtils.BACKTEST_ENVIRONMENT) {
            return isBacktestOptedIn()
        }
        return true
    })
}

function getEnabledComputeEnvironments() {
    return getActiveScheduleEnvironments().filter((environment) => {
        return isComputeEnabledForEnvironment(environment)
    })
}

function getShadowValidationEnvironments() {
    return getActiveScheduleEnvironments().filter((environment) => {
        const computeEnabled = isComputeEnabledForEnvironment(environment)
        const writeMode = String(envUtils.getConfigValue("qc_write_mode", "shadow", environment) || "shadow").trim().toLowerCase()
        return computeEnabled && writeMode === "shadow"
    })
}

function getTimeStrings() {
    const now = new Date()
    const usOffset = -4 * 60
    const cnOffset = 8 * 60
    const usTime = new Date(now.getTime() + usOffset * 60000)
    const cnTime = new Date(now.getTime() + cnOffset * 60000)
    return {
        us: usTime.toISOString().slice(0, 19).replace("T", " "),
        cn: cnTime.toISOString().slice(0, 19).replace("T", " "),
        date: usTime.toISOString().slice(0, 10),
        todayStart: usTime.toISOString().slice(0, 10) + " 00:00:00",
    }
}

function writeSystemEvent(eventType, level, source, title, detail, environment) {
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        const times = getTimeStrings()
        record.set("event_type", eventType)
        record.set("level", level)
        record.set("source", source)
        record.set("environment", environment)
        record.set("title", envUtils.labelTitleWithEnvironment(title, environment))
        record.set("detail", envUtils.addEnvironmentToDetail(detail, environment))
        record.set("us_time", times.us)
        record.set("cn_time", times.cn)
        record.set("notified", false)
        $app.save(record)
    } catch (err) {
        console.error(`[QCScheduler] system_events 写入失败 (${environment}): ${err.message}`)
    }
}

function triggerQcEndpoint(endpoint, environments, timeoutSeconds) {
    if (!environments.length) {
        console.log(`[QCScheduler] ${endpoint}: 无启用环境，跳过`)
        return
    }

    try {
        const resp = $http.send({
            url: `http://localhost:5100/${endpoint}`,
            method: "POST",
            body: JSON.stringify({ source: "cron", environments: environments }),
            headers: { "Content-Type": "application/json" },
            timeout: timeoutSeconds,
        })
        if (resp.statusCode !== 200) {
            console.error(`[QCScheduler] ${endpoint} returned ${resp.statusCode}: ${resp.raw}`)
            return
        }

        let payload = {}
        try {
            payload = typeof resp.json === "function" ? resp.json() : JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        console.log(`[QCScheduler] ${endpoint}: environments=${(payload.environments || environments).join(",")}, ok=${payload.ok !== false}`)
    } catch (err) {
        console.error(`[QCScheduler] ${endpoint} error: ${err.message}`)
    }
}

// 每分钟触发指标计算 (美东 4:00-20:00, 周一到周五)
cronAdd("qc_compute", "* 4-20 * * 1-5", () => {
    triggerQcEndpoint("compute", getEnabledComputeEnvironments(), 30)
})

// 盘前扫描 (7:00-10:00, 每5分钟, 周一到周五)
cronAdd("qc_scan", "*/5 7-9 * * 1-5", () => {
    triggerQcEndpoint("scan", getEnabledComputeEnvironments(), 60)
})

// Shadow模式验证 (每30分钟, 9:30-16:00, 周一到周五)
// 比较 qc_signals vs signals, qc_indicators vs indicators
cronAdd("qc_shadow_validate", "*/30 9-15 * * 1-5", () => {
    const environments = getShadowValidationEnvironments()
    if (!environments.length) {
        return
    }

    try {
        const times = getTimeStrings()

        for (const environment of environments) {
            let tvSignals = 0
            let qcSignals = 0
            let tvIndicators = 0
            let qcIndicators = 0

            try {
                const tvSigs = $app.findRecordsByFilter("signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: times.todayStart, env: environment })
                tvSignals = tvSigs ? tvSigs.length : 0
            } catch (_) {}

            try {
                const qcSigs = $app.findRecordsByFilter("qc_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: times.todayStart, env: environment })
                qcSignals = qcSigs ? qcSigs.length : 0
            } catch (_) {}

            try {
                const tvInds = $app.findRecordsByFilter("indicators", "created >= {:t} && environment = {:env}", "", 0, 0, { t: times.todayStart, env: environment })
                tvIndicators = tvInds ? tvInds.length : 0
            } catch (_) {}

            try {
                const qcInds = $app.findRecordsByFilter("qc_indicators", "created >= {:t} && environment = {:env}", "", 0, 0, { t: times.todayStart, env: environment })
                qcIndicators = qcInds ? qcInds.length : 0
            } catch (_) {}

            const signalDiff = Math.abs(tvSignals - qcSignals)
            const indicatorDiff = Math.abs(tvIndicators - qcIndicators)
            const detail = {
                date: times.date,
                tv_signals: tvSignals,
                qc_signals: qcSignals,
                tv_indicators: tvIndicators,
                qc_indicators: qcIndicators,
                signal_diff: signalDiff,
                indicator_diff: indicatorDiff,
                signal_match: signalDiff === 0,
                indicator_match: indicatorDiff === 0,
            }

            writeSystemEvent("compute_stats", "info", "pb", "Shadow模式验证", detail, environment)

            if (signalDiff > 2 || indicatorDiff > 2) {
                feishuSystem.notifyWarning("pb", "Shadow验证: 结果差异", {
                    "日期": times.date,
                    "TV信号": String(tvSignals),
                    "QC信号": String(qcSignals),
                    "信号差值": String(signalDiff),
                    "TV指标": String(tvIndicators),
                    "QC指标": String(qcIndicators),
                    "指标差值": String(indicatorDiff),
                }, environment)
            }

            console.log(`[QCScheduler] shadow validate ${environment}: TV=${tvSignals}sig/${tvIndicators}ind, QC=${qcSignals}sig/${qcIndicators}ind`)
        }
    } catch (err) {
        console.error(`[QCScheduler] shadow validate error: ${err.message}`)
    }
})

console.log("[QCScheduler] Hook 文件加载完成");
