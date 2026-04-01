/// <reference path="./pb_data/types.d.ts" />

/**
 * qc_scheduler.pb.js
 * PB 定时调度 → 触发 qc_compute Python 服务
 */

console.log("[QCScheduler] Hook 文件开始加载...");

function getConfigValue(key, defaultValue) {
    try {
        const record = $app.findFirstRecordByFilter("config", "key = {:k}", { k: key })
        return record ? String(record.get("value") || defaultValue) : defaultValue
    } catch (_) {
        return defaultValue
    }
}

// 每分钟触发指标计算 (美东 4:00-20:00, 周一到周五)
cronAdd("qc_compute", "* 4-20 * * 1-5", () => {
    const enabled = getConfigValue("qc_compute_enabled", "true")
    if (enabled !== "true") {
        return
    }

    try {
        const resp = $http.send({
            url: "http://localhost:5100/compute",
            method: "POST",
            body: JSON.stringify({ source: "cron" }),
            headers: { "Content-Type": "application/json" },
            timeout: 30,
        })
        if (resp.statusCode !== 200) {
            console.error(`[QCScheduler] compute returned ${resp.statusCode}: ${resp.raw}`)
        }
    } catch (err) {
        console.error(`[QCScheduler] compute error: ${err.message}`)
    }
})

// 盘前扫描 (7:00-10:00, 每5分钟, 周一到周五)
cronAdd("qc_scan", "*/5 7-9 * * 1-5", () => {
    const enabled = getConfigValue("qc_compute_enabled", "true")
    if (enabled !== "true") {
        return
    }

    try {
        const resp = $http.send({
            url: "http://localhost:5100/scan",
            method: "POST",
            body: JSON.stringify({ source: "cron" }),
            headers: { "Content-Type": "application/json" },
            timeout: 60,
        })
        if (resp.statusCode !== 200) {
            console.error(`[QCScheduler] scan returned ${resp.statusCode}: ${resp.raw}`)
        }
    } catch (err) {
        console.error(`[QCScheduler] scan error: ${err.message}`)
    }
})

// Shadow模式验证 (每30分钟, 9:30-16:00, 周一到周五)
// 比较 qc_signals vs signals, qc_indicators vs indicators
cronAdd("qc_shadow_validate", "*/30 9-15 * * 1-5", () => {
    const writeMode = getConfigValue("qc_write_mode", "shadow")
    if (writeMode !== "shadow") {
        return
    }

    try {
        const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)

        // 获取今日日期
        const now = new Date()
        const etOffset = -4 * 60
        const etTime = new Date(now.getTime() + etOffset * 60000)
        const todayDate = etTime.toISOString().slice(0, 10)
        const todayStart = todayDate + " 00:00:00"

        // 统计今日 signals vs qc_signals
        let tvSignals = 0
        let qcSignals = 0
        let tvIndicators = 0
        let qcIndicators = 0

        try {
            const tvSigs = $app.findRecordsByFilter("signals", "created >= {:t}", "", 0, 0, { t: todayStart })
            tvSignals = tvSigs ? tvSigs.length : 0
        } catch (_) {}

        try {
            const qcSigs = $app.findRecordsByFilter("qc_signals", "created >= {:t}", "", 0, 0, { t: todayStart })
            qcSignals = qcSigs ? qcSigs.length : 0
        } catch (_) {}

        try {
            const tvInds = $app.findRecordsByFilter("indicators", "created >= {:t}", "", 0, 0, { t: todayStart })
            tvIndicators = tvInds ? tvInds.length : 0
        } catch (_) {}

        try {
            const qcInds = $app.findRecordsByFilter("qc_indicators", "created >= {:t}", "", 0, 0, { t: todayStart })
            qcIndicators = qcInds ? qcInds.length : 0
        } catch (_) {}

        // 写system_events
        try {
            const collection = $app.findCollectionByNameOrId("system_events")
            const record = new Record(collection)
            record.set("event_type", "compute_stats")
            record.set("level", "info")
            record.set("source", "pb")
            record.set("title", "Shadow模式验证")
            record.set("detail", {
                date: todayDate,
                tv_signals: tvSignals,
                qc_signals: qcSignals,
                tv_indicators: tvIndicators,
                qc_indicators: qcIndicators,
                signal_match: tvSignals === qcSignals,
            })
            record.set("us_time", etTime.toISOString().slice(0, 19).replace("T", " "))
            record.set("notified", false)
            $app.save(record)
        } catch (_) {}

        // 差异较大时告警
        if (qcSignals > 0 && tvSignals > 0 && Math.abs(tvSignals - qcSignals) > 2) {
            feishuSystem.notifyWarning("pb", "Shadow验证: 信号数差异", {
                "TV信号": String(tvSignals),
                "QC信号": String(qcSignals),
                "差值": String(Math.abs(tvSignals - qcSignals)),
                "日期": todayDate,
            })
        }

        console.log(`[QCScheduler] shadow validate: TV=${tvSignals}sig/${tvIndicators}ind, QC=${qcSignals}sig/${qcIndicators}ind`)
    } catch (err) {
        console.error(`[QCScheduler] shadow validate error: ${err.message}`)
    }
})

console.log("[QCScheduler] Hook 文件加载完成");
