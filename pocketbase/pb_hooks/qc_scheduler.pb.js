/// <reference path="./pb_data/types.d.ts" />

/**
 * qc_scheduler.pb.js
 * PB 定时调度 → 触发 qc_compute Python 服务
 */

console.log("[QCScheduler] Hook 文件开始加载...");

const QC_SCHEDULER_BACKTEST_KEYS = ["qc_compute_enabled", "pb_scheduler_enabled"]

function getShadowValidationEnvironments() {
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    const { getEnabledRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    return getEnabledRuntimeEnvironments("qc_compute_enabled", "true", QC_SCHEDULER_BACKTEST_KEYS).filter((environment) => {
        const writeMode = String(getConfigValue("qc_write_mode", "shadow", environment) || "shadow").trim().toLowerCase()
        return writeMode === "shadow"
    })
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
    const { getEnabledRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    triggerQcEndpoint("compute", getEnabledRuntimeEnvironments("qc_compute_enabled", "true", QC_SCHEDULER_BACKTEST_KEYS), 30)
})

// 盘前扫描 (7:00-10:00, 每5分钟, 周一到周五)
cronAdd("qc_scan", "*/5 7-9 * * 1-5", () => {
    const { getEnabledRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    triggerQcEndpoint("scan", getEnabledRuntimeEnvironments("qc_compute_enabled", "true", QC_SCHEDULER_BACKTEST_KEYS), 60)
})

// Shadow模式验证 (每30分钟, 9:30-16:00, 周一到周五)
// 比较 qc_signals vs signals, qc_indicators vs indicators
cronAdd("qc_shadow_validate", "*/30 9-15 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
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
