/// <reference path="./pb_data/types.d.ts" />

/**
 * qc_actions.pb.js
 * QC 数据接收端点 — OHLCV / 指标 / 信号 / 扫描结果
 */

console.log("[QCActions] Hook 文件开始加载...");

const QC_PROXY_SOURCE = "pocketbase_qc_hook"
const QC_PROXY_HOOK = "qc_actions.pb.js"
const envUtils = require(`${__hooks}/lib/environment.js`)

// ── 工具函数 ──

function getConfigValue(key, defaultValue, environment) {
    return envUtils.getConfigValue(key, defaultValue, environment)
}

function upsertRecord(collectionName, filterStr, filterParams, data) {
    let record = null
    try {
        record = $app.findFirstRecordByFilter(collectionName, filterStr, filterParams)
    } catch (_) {}

    const col = $app.findCollectionByNameOrId(collectionName)
    if (!record) {
        record = new Record(col, {})
    }
    Object.keys(data).forEach((key) => {
        record.set(key, data[key])
    })
    $app.save(record)
    return record
}

function withProxyMeta(payload, route, upstream) {
    const base = payload && typeof payload === "object" && !Array.isArray(payload)
        ? { ...payload }
        : { ok: false, raw: String(payload || "") }

    base.proxy_source = QC_PROXY_SOURCE
    base.proxy_hook = QC_PROXY_HOOK
    base.proxy_route = route
    base.proxy_upstream = upstream
    return base
}

// ══════════════════════════════════════
// 批量OHLCV接收 → qc_bars
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/qc/bars", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const bars = d.bars || []
    const defaultEnvironment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)

    if (!bars.length) {
        return c.json(400, { ok: false, error: "Empty bars array" })
    }

    const enabled = getConfigValue("qc_bar_publish_enabled", "true", defaultEnvironment)
    if (enabled !== "true") {
        return c.json(200, { ok: true, skipped: true, reason: "qc_bar_publish_enabled=false" })
    }

    let created = 0
    let updated = 0
    let errors = 0

    for (let i = 0; i < bars.length; i++) {
        const bar = bars[i]
        const symbol = String(bar.symbol || "").trim().toUpperCase()
        const interval = String(bar.interval || "").trim()
        const barTimeMs = Number(bar.bar_time_ms)
        const environment = envUtils.getRuntimeEnvironmentFromData(bar, defaultEnvironment)

        if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
            errors++
            continue
        }

        try {
            let record = null
            try {
                record = $app.findFirstRecordByFilter(
                    "qc_bars",
                    "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
                    { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment }
                )
            } catch (_) {}

            const col = $app.findCollectionByNameOrId("qc_bars")
            if (!record) {
                record = new Record(col, {})
                created++
            } else {
                updated++
            }

            record.set("symbol", symbol)
            record.set("environment", environment)
            record.set("exchange", String(bar.exchange || "").trim().toUpperCase())
            record.set("interval", interval)
            record.set("open", Number(bar.open) || 0)
            record.set("high", Number(bar.high) || 0)
            record.set("low", Number(bar.low) || 0)
            record.set("close", Number(bar.close) || 0)
            record.set("volume", Number(bar.volume) || 0)
            record.set("session_type", String(bar.session_type || "").trim())
            record.set("us_time", String(bar.us_time || "").trim())
            record.set("cn_time", String(bar.cn_time || "").trim())
            record.set("bar_time_ms", Math.trunc(barTimeMs))
            if (bar.extra) {
                record.set("extra", bar.extra)
            }

            $app.save(record)
        } catch (err) {
            errors++
            console.error(`[QCActions] bars upsert error: ${symbol}/${interval}/${barTimeMs}: ${err.message}`)
        }
    }

    console.log(`[QCActions] bars: received=${bars.length}, created=${created}, updated=${updated}, errors=${errors}`)
    return c.json(200, { ok: true, received: bars.length, created, updated, errors })
})

// ══════════════════════════════════════
// QC指标写入 → qc_indicators + (老表)
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/qc/indicator", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
    const mode = getConfigValue("qc_write_mode", "shadow", environment)

    const symbol = String(d.symbol || "").trim().toUpperCase()
    const interval = String(d.interval || "").trim()
    const barTimeMs = Number(d.bar_time_ms)

    if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
        return c.json(400, { ok: false, error: "Missing symbol/interval/bar_time_ms" })
    }

    const recordData = {
        symbol: symbol,
        environment: environment,
        exchange: String(d.exchange || "").trim().toUpperCase(),
        interval: interval,
        script_tag: String(d.script_tag || "").trim(),
        us_time: String(d.us_time || "").trim(),
        cn_time: String(d.cn_time || "").trim(),
        bar_time_ms: Math.trunc(barTimeMs),
        bar_index: d.bar_index != null ? Number(d.bar_index) : null,
        extra: d.extra || {},
    }

    const filterStr = "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}"
    const filterParams = { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment }

    try {
        // shadow / primary → 写 qc_indicators
        if (mode === "shadow" || mode === "primary") {
            upsertRecord("qc_indicators", filterStr, filterParams, recordData)
        }

        // primary / settled → 写 indicators 老表
        if (mode === "primary" || mode === "settled") {
            upsertRecord("indicators", filterStr, filterParams, recordData)
        }

        return c.json(200, { ok: true, mode, symbol, interval })
    } catch (err) {
        console.error(`[QCActions] indicator upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

// ══════════════════════════════════════
// QC信号写入 → qc_signals + (老表)
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/qc/signal", (c) => {
    const { notifyNewSignal, mergeSignalExtra } = require(`${__hooks}/lib/feishu_signal.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
    const mode = getConfigValue("qc_write_mode", "shadow", environment)

    const symbol = String(d.symbol || "").trim().toUpperCase()
    const signalId = String(d.signal_id || "").trim()

    if (!symbol || !signalId) {
        return c.json(400, { ok: false, error: "Missing symbol or signal_id" })
    }

    const extra = envUtils.attachEnvironment(d.extra && typeof d.extra === "object" ? d.extra : {}, environment)
    extra.source = "qc"
    if (d.session_type) extra.session_type = d.session_type

    const signalData = {
        symbol: symbol,
        environment: environment,
        direction: d.direction || "",
        signal: d.signal || "",
        limit_price: Number(d.limit_price) || 0,
        entry: Number(d.entry) || 0,
        stop_loss: Number(d.stop_loss) || 0,
        take_profit: Number(d.take_profit) || 0,
        rr: String(d.rr || ""),
        shares: Number(d.shares) || 0,
        signal_id: signalId,
        exchange: String(d.exchange || "").trim().toUpperCase(),
        interval: String(d.interval || "").trim(),
        reason: String(d.reason || ""),
        us_time: String(d.us_time || "").trim(),
        cn_time: String(d.cn_time || "").trim(),
        date: String(d.date || "").trim(),
        bar_time_ms: d.bar_time_ms ? Math.trunc(Number(d.bar_time_ms)) : 0,
        bar_index: d.bar_index != null ? Number(d.bar_index) : null,
        script_tag: String(d.script_tag || "").trim(),
        chart_tf: String(d.chart_tf || "").trim(),
        extra: extra,
        status: d.status || "pending",
        note: String(d.note || ""),
    }

    const filterStr = "signal_id = {:sid} && environment = {:env}"
    const filterParams = { sid: signalId, env: environment }

    try {
        // shadow / primary → 写 qc_signals
        if (mode === "shadow" || mode === "primary") {
            const qcExtra = { ...extra, forwarded: mode === "primary" }
            upsertRecord("qc_signals", filterStr, filterParams, { ...signalData, extra: qcExtra })
        }

        // primary / settled → 写 signals 老表
        if (mode === "primary" || mode === "settled") {
            const filterOn = getConfigValue("daily_target_filter_on", "false", environment)
            let shouldWrite = true

            if (filterOn === "true") {
                // 检查 daily_targets
                const today = signalData.date || new Date().toISOString().slice(0, 10)
                try {
                    $app.findFirstRecordByFilter(
                        "daily_targets",
                        "symbol = {:sym} && date = {:d} && environment = {:env} && status != 'removed'",
                        { sym: symbol, d: today, env: environment }
                    )
                } catch (_) {
                    shouldWrite = false
                    console.log(`[QCActions] signal filtered: ${symbol} not in daily_targets for ${today}`)
                }
            }

            if (shouldWrite) {
                const oldRecord = upsertRecord("signals", filterStr, filterParams, signalData)
                // 飞书通知 (仅新信号)
                try {
                    if (oldRecord && signalData.status === "pending") {
                        notifyNewSignal(oldRecord)
                    }
                } catch (notifyErr) {
                    console.error(`[QCActions] feishu notify error: ${notifyErr.message}`)
                }
            }
        }

        return c.json(200, { ok: true, mode, symbol, signal_id: signalId })
    } catch (err) {
        console.error(`[QCActions] signal upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

// ══════════════════════════════════════
// QC扫描结果 → daily_targets
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/qc/scan", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)

    const symbol = String(d.symbol || "").trim().toUpperCase()
    const date = String(d.date || "").trim()

    if (!symbol || !date) {
        return c.json(400, { ok: false, error: "Missing symbol or date" })
    }

    const targetData = {
        symbol: symbol,
        environment: environment,
        exchange: String(d.exchange || "").trim().toUpperCase(),
        date: date,
        direction_bias: d.direction_bias || "neutral",
        score: Number(d.score) || 0,
        scan_reason: String(d.scan_reason || ""),
        status: d.status || "candidate",
        us_time: String(d.us_time || "").trim(),
        cn_time: String(d.cn_time || "").trim(),
        bar_time_ms: d.bar_time_ms ? Math.trunc(Number(d.bar_time_ms)) : 0,
        extra: d.extra || {},
    }

    try {
        upsertRecord(
            "daily_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env}",
            { sym: symbol, d: date, env: environment },
            targetData
        )
        return c.json(200, { ok: true, symbol, date })
    } catch (err) {
        console.error(`[QCActions] scan upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

// ══════════════════════════════════════
// QC Compute 代理端点 (PB页面调用 → 转发到Python服务)
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/qc/proxy", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const action = String(d.action || "").trim()

    const validActions = ["compute", "scan", "recompute"]
    if (!validActions.includes(action)) {
        return c.json(400, { ok: false, error: "Invalid action, must be: " + validActions.join("/") })
    }

    try {
        const resp = $http.send({
            url: `http://localhost:5100/${action}`,
            method: "POST",
            body: JSON.stringify({ source: "pb_proxy", ...d }),
            headers: { "Content-Type": "application/json" },
            timeout: 60,
        })
        return c.json(
            resp.statusCode || 200,
            withProxyMeta(JSON.parse(resp.raw || "{}"), "/api/custom/qc/proxy", `http://localhost:5100/${action}`)
        )
    } catch (err) {
        console.error(`[QCActions] proxy ${action} error: ${err.message}`)
        return c.json(
            502,
            withProxyMeta(
                { ok: false, status: "offline", error: `qc_compute unreachable: ${err.message}` },
                "/api/custom/qc/proxy",
                `http://localhost:5100/${action}`
            )
        )
    }
})

// ══════════════════════════════════════
// QC Compute 状态查询 (PB页面用)
// ══════════════════════════════════════
routerAdd("GET", "/api/custom/qc/status", (c) => {
    try {
        const resp = $http.send({
            url: "http://localhost:5100/status",
            method: "GET",
            timeout: 10,
        })
        return c.json(
            resp.statusCode || 200,
            withProxyMeta(JSON.parse(resp.raw || "{}"), "/api/custom/qc/status", "http://localhost:5100/status")
        )
    } catch (err) {
        return c.json(
            200,
            withProxyMeta(
                { ok: false, status: "offline", error: err.message },
                "/api/custom/qc/status",
                "http://localhost:5100/status"
            )
        )
    }
})

routerAdd("GET", "/api/custom/qc/health", (c) => {
    try {
        const resp = $http.send({
            url: "http://localhost:5100/health",
            method: "GET",
            timeout: 5,
        })
        return c.json(
            resp.statusCode || 200,
            withProxyMeta(JSON.parse(resp.raw || "{}"), "/api/custom/qc/health", "http://localhost:5100/health")
        )
    } catch (err) {
        return c.json(
            200,
            withProxyMeta(
                { ok: false, status: "offline", error: err.message },
                "/api/custom/qc/health",
                "http://localhost:5100/health"
            )
        )
    }
})

// ══════════════════════════════════════
// QC 状态持久化 — 替代 ObjectStore
// ══════════════════════════════════════

// GET /api/custom/qc/state/signals?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/qc/state/signals", (c) => {
    const date = c.queryParam("date")
    const environment = envUtils.getRuntimeEnvironmentFromRequest(c, envUtils.LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "qc_state", "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "signals", d: date, env: environment }
        )
        if (record) {
            return c.json(200, { ok: true, date: date, environment: environment, data: record.get("data") || {} })
        }
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    } catch (_) {
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    }
})

// POST /api/custom/qc/state/signals
routerAdd("POST", "/api/custom/qc/state/signals", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    const stateData = {
        processed_ids: d.processed_ids || [],
        confirmed_ids: d.confirmed_ids || [],
        active_signals: d.active_signals || []
    }

    try {
        upsertRecord(
            "qc_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "signals", d: date, env: environment },
            { state_key: "signals", date: date, environment: environment, data: stateData }
        )
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// GET /api/custom/qc/state/orders?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/qc/state/orders", (c) => {
    const date = c.queryParam("date")
    const environment = envUtils.getRuntimeEnvironmentFromRequest(c, envUtils.LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "qc_state", "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "orders", d: date, env: environment }
        )
        if (record) {
            return c.json(200, { ok: true, date: date, environment: environment, data: record.get("data") || {} })
        }
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    } catch (_) {
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    }
})

// POST /api/custom/qc/state/orders
routerAdd("POST", "/api/custom/qc/state/orders", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    const stateData = {
        closed_today: d.closed_today || [],
        stop_loss_count_today: d.stop_loss_count_today || 0,
        pending: d.pending || {},
        positions: d.positions || {},
        order_id_map: d.order_id_map || {},
        completed_signal_ids: d.completed_signal_ids || [],
        completed_signal_outcomes: d.completed_signal_outcomes || {}
    }

    try {
        upsertRecord(
            "qc_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "orders", d: date, env: environment },
            { state_key: "orders", date: date, environment: environment, data: stateData }
        )
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// POST /api/custom/qc/health — QC健康数据上报
routerAdd("POST", "/api/custom/qc/health-report", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)

    // 写 system_events
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        record.set("event_type", "heartbeat")
        record.set("level", "info")
        record.set("source", "qc")
        record.set("environment", environment)
        record.set("title", envUtils.labelTitleWithEnvironment("QC 健康上报", environment))
        record.set("detail", envUtils.addEnvironmentToDetail(d, environment))
        record.set("us_time", d.et_time || "")
        record.set("cn_time", d.bj_time || "")
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    return c.json(200, { ok: true })
})

// POST /api/custom/qc/notify — QC通知转发
routerAdd("POST", "/api/custom/qc/notify", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)

    const notifyType = d.type || "status"
    const title = envUtils.labelTitleWithEnvironment(d.title || "", environment)
    const detail = envUtils.addEnvironmentToDetail(d.data || d.detail || {}, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    var level = "info"
    if (notifyType === "alert" || notifyType === "error") level = "error"
    if (notifyType === "warning") level = "warning"

    // 写 system_events
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        record.set("event_type", "status_change")
        record.set("level", level)
        record.set("source", "qc")
        record.set("environment", environment)
        record.set("title", title)
        record.set("detail", detail)
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    // 发飞书
    var notified = feishuSystem.notifySystemEvent("status_change", level, "qc", title, detail, environment)

    return c.json(200, { ok: true, notified: notified })
})

console.log("[QCActions] Hook 文件加载完成");
