/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_actions.pb.js
 * IBKR 数据接收端点 — OHLCV / 指标 / 信号 / 扫描结果
 */

console.log("[IBKRActions] Hook 文件开始加载...");

// ── 工具函数 ──

function ibkrActionsParseHttpJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}

const IBKR_ACTIONS_VOLATILE_COMPARE_KEYS = {
    computed_at_ms: true,
    computed_at_us: true,
    computed_at_cn: true,
}

function ibkrActionsNormalizeForCompare(value) {
    if (Array.isArray(value)) {
        return value.map((item) => ibkrActionsNormalizeForCompare(item))
    }
    if (value && typeof value === "object") {
        const normalized = {}
        Object.keys(value).sort().forEach((key) => {
            if (IBKR_ACTIONS_VOLATILE_COMPARE_KEYS[key]) {
                return
            }
            normalized[key] = ibkrActionsNormalizeForCompare(value[key])
        })
        return normalized
    }
    if (typeof value === "number") {
        return Number.isFinite(value) ? Number(value) : null
    }
    return value == null ? null : value
}
globalThis.ibkrActionsNormalizeForCompare = ibkrActionsNormalizeForCompare

function ibkrActionsValuesEqual(left, right) {
    return JSON.stringify(globalThis.ibkrActionsNormalizeForCompare(left)) === JSON.stringify(globalThis.ibkrActionsNormalizeForCompare(right))
}
globalThis.ibkrActionsValuesEqual = ibkrActionsValuesEqual

function ibkrActionsRecordNeedsUpdate(record, data) {
    return Object.keys(data || {}).some((key) => !globalThis.ibkrActionsValuesEqual(record.get(key), data[key]))
}
globalThis.ibkrActionsRecordNeedsUpdate = ibkrActionsRecordNeedsUpdate

function ibkrActionsUpsertRecord(collectionName, filterStr, filterParams, data) {
    let record = null
    try {
        record = $app.findFirstRecordByFilter(collectionName, filterStr, filterParams)
    } catch (_) {}

    const col = $app.findCollectionByNameOrId(collectionName)
    let action = "updated"
    if (!record) {
        record = new Record(col, {})
        action = "created"
    } else if (!globalThis.ibkrActionsRecordNeedsUpdate(record, data)) {
        return { record: record, action: "skipped" }
    }
    Object.keys(data).forEach((key) => {
        record.set(key, data[key])
    })
    $app.save(record)
    return { record: record, action: action }
}
globalThis.ibkrActionsUpsertRecord = ibkrActionsUpsertRecord

function ibkrActionsBuildProxyMeta(payload, route, upstream) {
    const base = payload && typeof payload === "object" && !Array.isArray(payload)
        ? { ...payload }
        : { ok: false, raw: String(payload || "") }

    base.proxy_source = "pocketbase_ibkr_hook"
    base.proxy_hook = "ibkr_actions.pb.js"
    base.proxy_route = route
    base.proxy_upstream = upstream
    return base
}
globalThis.ibkrActionsBuildProxyMeta = ibkrActionsBuildProxyMeta

function ibkrActionsHttpStatusCode(resp, fallback) {
    const code = Number(resp && resp.statusCode)
    return Number.isFinite(code) && code > 0 ? Math.trunc(code) : (fallback || 200)
}

function ibkrActionsSendJson(c, statusCode, payload) {
    return c.html(Number(statusCode) || 200, JSON.stringify(payload || {}))
}
globalThis.ibkrActionsSendJson = ibkrActionsSendJson

function ibkrActionsFetchProxyPayload(route, upstream, method, body, timeoutSec) {
    const requestOptions = {
        url: upstream,
        method: method || "GET",
        timeout: timeoutSec || 20,
    }
    if (body !== undefined && body !== null) {
        requestOptions.body = JSON.stringify(body)
        requestOptions.headers = { "Content-Type": "application/json" }
    }

    const resp = $http.send(requestOptions)
    return {
        statusCode: (Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200),
        payload: ibkrActionsBuildProxyMeta(ibkrActionsParseHttpJson(resp.raw), route, upstream),
    }
}
globalThis.ibkrActionsFetchProxyPayload = ibkrActionsFetchProxyPayload

function ibkrActionsProxyRequest(c, route, upstream, method, body, timeoutSec) {
    try {
        const result = globalThis.ibkrActionsFetchProxyPayload(route, upstream, method, body, timeoutSec)
        return ibkrActionsSendJson(c, result.statusCode, result.payload)
    } catch (err) {
        return c.json(
            502,
            globalThis.ibkrActionsBuildProxyMeta(
                { ok: false, status: "offline", error: err.message || String(err) },
                route,
                upstream
            )
        )
    }
}

function ibkrActionsBuildIndicatorData(d, environment) {
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const interval = String(d.interval || "").trim()
    const barTimeMs = Number(d.bar_time_ms)

    if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
        return { ok: false, error: "Missing symbol/interval/bar_time_ms" }
    }

    return {
        ok: true,
        symbol: symbol,
        interval: interval,
        bar_time_ms: Math.trunc(barTimeMs),
        filter: "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
        params: { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment },
        data: {
            symbol: symbol,
            environment: environment,
            exchange: String(d.exchange || "").trim().toUpperCase(),
            interval: interval,
            script_tag: String(d.script_tag || "").trim(),
            us_time: String(d.us_time || "").trim(),
            cn_time: String(d.cn_time || "").trim(),
            bar_time_ms: Math.trunc(barTimeMs),
            bar_index: d.bar_index != null ? Number(d.bar_index) : null,
            extra: {
                ...(d.extra || {}),
                environment: environment,
                source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
            },
        },
    }
}
globalThis.ibkrActionsBuildIndicatorData = ibkrActionsBuildIndicatorData

function ibkrActionsBuildSignalData(d, environment) {
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const signalId = String(d.signal_id || "").trim()

    if (!symbol || !signalId) {
        return { ok: false, error: "Missing symbol or signal_id" }
    }

    return {
        ok: true,
        symbol: symbol,
        signal_id: signalId,
        filter: "signal_id = {:sid} && environment = {:env}",
        params: { sid: signalId, env: environment },
        data: {
            symbol: symbol,
            environment: environment,
            direction: String(d.direction || "").trim(),
            signal: String(d.signal || "").trim(),
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
            extra: {
                ...(d.extra || {}),
                source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
                environment: environment,
            },
            status: String(d.status || "pending"),
            note: String(d.note || ""),
        },
    }
}
globalThis.ibkrActionsBuildSignalData = ibkrActionsBuildSignalData

function ibkrActionsUpsertConfigValue(key, value, environment, extras) {
    const runtimeEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    let record = null
    try {
        record = $app.findFirstRecordByFilter(
            "config",
            "key = {:key} && environment = {:env}",
            { key: String(key || ""), env: runtimeEnvironment }
        )
    } catch (_) {}

    const collection = $app.findCollectionByNameOrId("config")
    if (!record) {
        record = new Record(collection, {})
        record.set("key", String(key || ""))
        record.set("environment", runtimeEnvironment)
    }

    const meta = extras || {}
    if (meta.display_name) record.set("display_name", String(meta.display_name))
    if (meta.description) record.set("description", String(meta.description))
    if (meta.group_name) record.set("group_name", String(meta.group_name))
    if (meta.default_value != null) record.set("default_value", String(meta.default_value))
    if (meta.sort_order != null) record.set("sort_order", Number(meta.sort_order) || 0)
    record.set("value", String(value == null ? "" : value))
    $app.save(record)
    return record
}
globalThis.ibkrActionsUpsertConfigValue = ibkrActionsUpsertConfigValue

routerAdd("GET", "/api/custom/ibkr/ping_write", (c) => {
    try {
        const signalId = "PING_" + Date.now()
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_signals",
                "signal_id = {:sid} && environment = {:env}",
                { sid: signalId, env: "live" }
            )
        } catch (_) {}

        const col = $app.findCollectionByNameOrId("ibkr_signals")
        if (!record) {
            record = new Record(col, {})
        }

        record.set("symbol", "AAPL")
        record.set("environment", "live")
        record.set("direction", "long")
        record.set("signal", "ping_write")
        record.set("limit_price", 0)
        record.set("entry", 100)
        record.set("stop_loss", 99)
        record.set("take_profit", 101)
        record.set("rr", "1.00")
        record.set("shares", 1)
        record.set("signal_id", signalId)
        record.set("exchange", "NASDAQ")
        record.set("interval", "5")
        record.set("reason", "ping_write")
        record.set("us_time", "2026-04-02 11:24:00")
        record.set("cn_time", "2026-04-02 23:24:00")
        record.set("date", "2026-04-02")
        record.set("bar_time_ms", Date.now())
        record.set("bar_index", 1)
        record.set("script_tag", "ping")
        record.set("chart_tf", "5")
        record.set("extra", { source: "ping_write", environment: "live" })
        record.set("status", "pending")
        record.set("note", "")
        $app.save(record)

        return c.json(200, { ok: true, signal_id: signalId, id: record.id || "" })
    } catch (err) {
        console.error(`[IBKRActions] ping_write error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

// ══════════════════════════════════════
// 批量OHLCV接收 → ibkr_bars
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/bars", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const bars = d.bars || []
    const { getRuntimeEnvironmentFromData, getConfigValue, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    if (!bars.length) {
        return c.json(400, { ok: false, error: "Empty bars array" })
    }

    const enabled = getConfigValue("ibkr_bar_publish_enabled", "true", defaultEnvironment)
    if (!isEnabledConfigValue(enabled)) {
        return c.json(200, {
            ok: true,
            skipped: true,
            reason: "ibkr_bar_publish_enabled=false",
            config_value: String(enabled || ""),
        })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0

    for (let i = 0; i < bars.length; i++) {
        const bar = bars[i]
        const symbol = String(bar.symbol || "").trim().toUpperCase()
        const interval = String(bar.interval || "").trim()
        const barTimeMs = Number(bar.bar_time_ms)
        const environment = getRuntimeEnvironmentFromData(bar, defaultEnvironment)

        if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
            errors++
            continue
        }

        try {
            const result = actionHelpers.upsertRecord(
                "ibkr_bars",
                "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
                { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment },
                {
                    symbol: symbol,
                    environment: environment,
                    exchange: String(bar.exchange || "").trim().toUpperCase(),
                    interval: interval,
                    open: Number(bar.open) || 0,
                    high: Number(bar.high) || 0,
                    low: Number(bar.low) || 0,
                    close: Number(bar.close) || 0,
                    volume: Number(bar.volume) || 0,
                    session_type: String(bar.session_type || "").trim(),
                    us_time: String(bar.us_time || "").trim(),
                    cn_time: String(bar.cn_time || "").trim(),
                    bar_time_ms: Math.trunc(barTimeMs),
                    extra: bar.extra || {},
                },
            )
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] bars upsert error: ${symbol}/${interval}/${barTimeMs}: ${err.message}`)
        }
    }

    console.log(`[IBKRActions] bars: received=${bars.length}, created=${created}, updated=${updated}, skipped=${skipped}, errors=${errors}`)
    return c.json(200, { ok: true, received: bars.length, created, updated, skipped, errors })
})

// ══════════════════════════════════════
// 指标写入 -> ibkr_indicators
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/indicator", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const prepared = actionHelpers.buildIndicatorData(d, environment)
    if (!prepared.ok) {
        return c.json(400, { ok: false, error: prepared.error || "invalid_indicator_payload" })
    }

    try {
        const result = actionHelpers.upsertRecord("ibkr_indicators", prepared.filter, prepared.params, prepared.data)
        return c.json(200, {
            ok: true,
            symbol: prepared.symbol,
            interval: prepared.interval,
            collection: "ibkr_indicators",
            action: result.action,
        })
    } catch (err) {
        console.error(`[IBKRActions] indicator upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

routerAdd("POST", "/api/custom/ibkr/indicators", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const items = Array.isArray(d.items) ? d.items : []
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    if (!items.length) {
        return c.json(400, { ok: false, error: "Empty indicators array" })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0
    for (let i = 0; i < items.length; i++) {
        const environment = getRuntimeEnvironmentFromData(items[i], defaultEnvironment)
        const prepared = actionHelpers.buildIndicatorData(items[i], environment)
        if (!prepared.ok) {
            errors++
            continue
        }
        try {
            const result = actionHelpers.upsertRecord("ibkr_indicators", prepared.filter, prepared.params, prepared.data)
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] indicators upsert error: ${prepared.symbol}/${prepared.interval}/${prepared.bar_time_ms}: ${err.message}`)
        }
    }

    return c.json(200, {
        ok: errors === 0,
        received: items.length,
        success: created + updated,
        created: created,
        updated: updated,
        skipped: skipped,
        errors: errors,
        collection: "ibkr_indicators",
    })
})

// ══════════════════════════════════════
// 信号写入 -> ibkr_signals
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/signal", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const environment = String(d.environment || "live").trim().toLowerCase() || "live"
    const prepared = actionHelpers.buildSignalData(d, environment)
    if (!prepared.ok) {
        return c.json(400, { ok: false, error: prepared.error || "invalid_signal_payload" })
    }

    try {
        const result = actionHelpers.upsertRecord("ibkr_signals", prepared.filter, prepared.params, prepared.data)
        return c.json(200, {
            ok: true,
            signal_id: prepared.signal_id,
            target: "ibkr_signals",
            id: (result.record && result.record.id) || "",
            action: result.action,
        })
    } catch (err) {
        console.error(`[IBKRActions] signal upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err), target: "ibkr_signals" })
    }
})

routerAdd("POST", "/api/custom/ibkr/signals", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const items = Array.isArray(d.items) ? d.items : []
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const defaultEnvironment = String(d.environment || "live").trim().toLowerCase() || "live"

    if (!items.length) {
        return c.json(400, { ok: false, error: "Empty signals array" })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0
    for (let i = 0; i < items.length; i++) {
        const environment = String((items[i] && items[i].environment) || defaultEnvironment).trim().toLowerCase() || defaultEnvironment
        const prepared = actionHelpers.buildSignalData(items[i] || {}, environment)
        if (!prepared.ok) {
            errors++
            continue
        }
        try {
            const result = actionHelpers.upsertRecord("ibkr_signals", prepared.filter, prepared.params, prepared.data)
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] signals upsert error: ${prepared.signal_id}: ${err.message}`)
        }
    }

    return c.json(200, {
        ok: errors === 0,
        received: items.length,
        success: created + updated,
        created: created,
        updated: updated,
        skipped: skipped,
        errors: errors,
        target: "ibkr_signals",
    })
})

// ══════════════════════════════════════
// IBKR 盘前扫描结果 → ibkr_targets
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/scan", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upsertRecord = function(collectionName, filterStr, filterParams, data) {
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
            "ibkr_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env}",
            { sym: symbol, d: date, env: environment },
            targetData
        )
        return c.json(200, { ok: true, symbol, date })
    } catch (err) {
        console.error(`[IBKRActions] scan upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

// ══════════════════════════════════════
// IBKR Compute 代理端点 (PB页面调用 → 转发到Python服务)
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/proxy", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const action = String(d.action || "").trim()
    // PB hooks run in a shared JS runtime; keep critical proxy helpers route-local.
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
    const parsePayload = function(rawValue) {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const withMeta = function(payload, upstream) {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_actions.pb.js"
        base.proxy_route = "/api/custom/ibkr/proxy"
        base.proxy_upstream = upstream
        return base
    }

    const validActions = ["compute", "scan", "recompute"]
    if (!validActions.includes(action)) {
        return c.json(400, { ok: false, error: "Invalid action, must be: " + validActions.join("/") })
    }

    try {
        const upstream = `${computeBaseUrl}/${action}`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify({ source: "pb_proxy", ...d }),
            headers: { "Content-Type": "application/json" },
            timeout: 60,
        })
        return c.html((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), JSON.stringify(withMeta(parsePayload(resp.raw), upstream)))
    } catch (err) {
        console.error(`[IBKRActions] proxy ${action} error: ${err.message}`)
        const upstream = `${computeBaseUrl}/${action}`
        return c.json(
            502,
            withMeta(
                { ok: false, status: "offline", error: `ibkr_compute unreachable: ${err.message}` },
                upstream
            )
        )
    }
})

// ══════════════════════════════════════
// IBKR Compute 状态查询 (PB页面用)
// ══════════════════════════════════════
routerAdd("GET", "/api/custom/ibkr/statusz", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const computeBase = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
    try {
        const computeResp = $http.send({
            url: `${computeBase}/status`,
            method: "GET",
            timeout: 10,
        })
        const runtimeResp = $http.send({
            url: `${computeBase}/ibkr/status`,
            method: "GET",
            timeout: 10,
        })
        let computePayload = {}
        let runtimePayload = {}
        try {
            computePayload = JSON.parse(computeResp.raw || "{}")
        } catch (_) {
            computePayload = {}
        }
        try {
            runtimePayload = JSON.parse(runtimeResp.raw || "{}")
        } catch (_) {
            runtimePayload = {}
        }
        return c.json(200, {
            ...computePayload,
            ...(runtimePayload && runtimePayload.ok !== false ? runtimePayload : {}),
            compute: computePayload,
            runtime: runtimePayload,
            proxy_upstream_compute: `${computeBase}/status`,
            proxy_upstream_runtime: `${computeBase}/ibkr/status`,
        })
    } catch (err) {
        return c.json(200, { ok: false, status: "offline", error: err.message })
    }
})

routerAdd("GET", "/api/custom/ibkr/healthz", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    try {
        const resp = $http.send({
            url: `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/health`,
            method: "GET",
            timeout: 5,
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        return c.json(200, payload)
    } catch (err) {
        return c.json(200, { ok: false, status: "offline", error: err.message || String(err) })
    }
})

routerAdd("GET", "/api/custom/ibkr/account_snapshot", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/account`
    try {
        const resp = $http.send({
            url: upstream,
            method: "GET",
            timeout: 20,
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/account_snapshot"
        payload.proxy_upstream = upstream
        return c.json(200, payload)
    } catch (err) {
        return c.json(200, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/account_snapshot",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/runtime/config", (c) => {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const items = []
    try {
        const records = $app.findRecordsByFilter(
            "config",
            "environment = {:env} || environment = 'global' || environment = ''",
            "-updated",
            500,
            0,
            { env: environment }
        ) || []

        for (let i = 0; i < records.length; i++) {
            items.push({
                key: records[i].get("key") || "",
                value: records[i].get("value") || "",
                environment: records[i].get("environment") || "",
                updated: records[i].get("updated") || "",
            })
        }
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
    return c.json(200, { ok: true, environment: environment, items: items })
})

routerAdd("POST", "/api/custom/ibkr/start", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/start`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/start"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/start",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/stop", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/stop`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/stop"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/stop",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/account", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/account`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/account"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/account",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/positions", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/positions`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/positions"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/positions",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/orders/live", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/live`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/live"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/live",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/cancel`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/cancel"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/cancel",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel_all", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/cancel_all`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/cancel_all"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/cancel_all",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/modify", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/modify`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/modify"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/modify",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/place", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/place`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/place"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/place",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/positions/close", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/positions/close`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/positions/close"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/positions/close",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/emergency-stop", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const action = String(d.action || "all").trim().toLowerCase() || "all"
    const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")

    const configActions = {
        compute: [
            ["ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"],
        ],
        trading: [
            ["ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"],
        ],
        scheduler: [
            ["pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"],
        ],
        publish: [
            ["ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"],
        ],
        all: [
            ["ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"],
            ["ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"],
            ["pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"],
            ["ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"],
        ],
        runtime: [],
    }

    const selected = configActions[action]
    if (!selected) {
        return c.json(400, { ok: false, error: "Unsupported emergency action", action: action })
    }

    const updated = []
    for (let i = 0; i < selected.length; i++) {
        const item = selected[i]
        const record = actionHelpers.upsertConfigValue(item[0], item[1], environment, {
            display_name: item[2],
            description: item[3],
            group_name: "PB / IBKR 服务",
        })
        updated.push({
            key: item[0],
            value: item[1],
            id: record && record.id ? record.id : "",
        })
    }

    let stopPayload = { ok: true, skipped: true }
    if (action === "runtime" || action === "all" || action === "compute") {
        try {
            const resp = $http.send({ url: `${computeBaseUrl}/ibkr/stop`, method: "POST", timeout: 20 })
            try {
                stopPayload = JSON.parse(resp.raw || "{}")
            } catch (_) {
                stopPayload = {}
            }
            stopPayload.status_code = (Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200)
        } catch (err) {
            stopPayload = { ok: false, error: err.message || String(err) }
        }
    }

    writeSystemEvent(
        "status_change",
        "warning",
        "manual",
        "触发紧急停止",
        {
            action: action,
            updated_keys: updated.map((item) => item.key).join(","),
            runtime_stop: stopPayload.ok !== false ? "requested" : "failed",
        },
        environment,
        false,
    )

    return c.json(200, {
        ok: stopPayload.ok !== false,
        environment: environment,
        action: action,
        updated: updated,
        runtime_stop: stopPayload,
    })
})

routerAdd("POST", "/api/custom/ibkr/recover", (c) => {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const action = String(d.action || "all").trim().toLowerCase() || "all"

    const configActions = {
        compute: [
            ["ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"],
        ],
        trading: [
            ["ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"],
        ],
        scheduler: [
            ["pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"],
        ],
        publish: [
            ["ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"],
        ],
        all: [
            ["ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"],
            ["ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"],
            ["pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"],
            ["ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"],
        ],
    }

    const selected = configActions[action]
    if (!selected) {
        return c.json(400, { ok: false, error: "Unsupported recover action", action: action })
    }

    const updated = []
    for (let i = 0; i < selected.length; i++) {
        const item = selected[i]
        const record = actionHelpers.upsertConfigValue(item[0], item[1], environment, {
            display_name: item[2],
            description: item[3],
            group_name: "PB / IBKR 服务",
        })
        updated.push({
            key: item[0],
            value: item[1],
            id: record && record.id ? record.id : "",
        })
    }

    writeSystemEvent(
        "status_change",
        "info",
        "manual",
        "恢复运行开关",
        {
            action: action,
            updated_keys: updated.map((item) => item.key).join(","),
        },
        environment,
        false,
    )

    return c.json(200, {
        ok: true,
        environment: environment,
        action: action,
        updated: updated,
    })
})

routerAdd("POST", "/api/custom/ibkr/reauth", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
    try {
        $http.send({ url: `${computeBaseUrl}/ibkr/stop`, method: "POST", timeout: 30 })
    } catch (_) {}
    const upstream = `${computeBaseUrl}/ibkr/start`
    try {
        const resp = $http.send({ url: upstream, method: "POST", timeout: 30 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/reauth"
        payload.proxy_upstream = upstream
        return c.html((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), JSON.stringify(payload))
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/reauth",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/2fa/status", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getStatePayload } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const payload = getStatePayload(environment)
    const state = { ...(payload.data || {}) }

    try {
        const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
        const runtimeResp = $http.send({
            url: `${computeBaseUrl}/ibkr/status`,
            method: "GET",
            timeout: 8,
        })
        let runtime = {}
        try {
            runtime = JSON.parse(runtimeResp.raw || "{}")
        } catch (_) {
            runtime = {}
        }
        const runtimeStarted = Boolean(
            runtime.starting
            || (runtime.session && runtime.session.running)
            || (runtime.websocket && runtime.websocket.running)
            || (runtime.order_tracker && runtime.order_tracker.running)
        )
        const runtimeAuthenticated = Boolean(runtime.session && runtime.session.authenticated)
        const gatewayReachable = Boolean(runtime.gateway && (runtime.gateway.running || runtime.gateway.reachable))
        const gatewayStatusCode = Number(runtime.gateway && runtime.gateway.status_code || 0) || 0

        state.runtime_started = runtimeStarted
        state.runtime_authenticated = runtimeAuthenticated
        state.gateway_status_code = gatewayStatusCode
        state.gateway_reachable = gatewayReachable

        if (!runtimeAuthenticated || gatewayStatusCode === 401) {
            state.gateway_authenticated = false
            state.backend_authenticated = false
            if (!runtimeStarted) {
                state.browser_authenticated = false
            }
            if (String(state.status || "").trim().toLowerCase() === "success") {
                state.status = "requested"
                state.message = "旧 Gateway 认证已失效，请重新触发 2FA。"
                state.last_result = "旧 Gateway 认证已失效，等待重新触发 2FA。"
            }
        }
    } catch (err) {
        state.runtime_status_error = err.message || String(err)
    }

    return c.json(200, {
        ok: true,
        environment: payload.environment,
        date: payload.date,
        state: state,
    })
})

routerAdd("POST", "/api/custom/ibkr/2fa/request", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { request2faApproval } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const forceReset = d.force_reset === true || ["1", "true", "yes", "on"].includes(String(d.force_reset || "").trim().toLowerCase())
    const forceNew = d.force_new === true || ["1", "true", "yes", "on"].includes(String(d.force_new || "").trim().toLowerCase())

    try {
        const result = request2faApproval({
            environment: environment,
            reason: String(d.reason || "").trim() || "manual_reauth",
            source: String(d.source || "").trim() || "ibkr_compute",
            message: String(d.message || "").trim(),
            detail: d.detail && typeof d.detail === "object" ? d.detail : {},
            forceReset: forceReset,
            forceNew: forceNew,
        })
        return c.json(result.ok ? 200 : 500, {
            ok: !!result.ok,
            environment: result.environment || environment,
            date: result.date || "",
            status: result.status || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.error || "",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/result", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { report2faResult } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const result = report2faResult({
            environment: environment,
            status: String(d.status || "").trim() || "requested",
            source: String(d.source || "").trim() || "ibkr_compute",
            message: String(d.message || "").trim(),
            last_result: String(d.last_result || "").trim(),
            error: d.error != null ? String(d.error) : "",
            detail: d.detail && typeof d.detail === "object" ? d.detail : {},
            state_patch: d.state_patch && typeof d.state_patch === "object" ? d.state_patch : {},
        })
        return c.json(result.ok ? 200 : 500, {
            ok: !!result.ok,
            environment: result.environment || environment,
            status: result.status || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.error || "",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/respond", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { submit2faResponse } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const result = submit2faResponse({
            environment: environment,
            response_code: String(d.response_code || "").trim(),
            challenge_code: String(d.challenge_code || "").trim(),
            source: String(d.source || "").trim() || "runtime_page",
        })
        return c.json(result.ok ? 200 : 400, {
            ok: !!result.ok,
            environment: result.environment || environment,
            status: result.status || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.error || "",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

// ══════════════════════════════════════
// IBKR 状态持久化 — 替代 ObjectStore
// ══════════════════════════════════════

// GET /api/custom/ibkr/state/signals?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/ibkr/state/signals", (c) => {
    const date = c.request.url.query().get("date") || ""
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "ibkr_state", "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "ibkr_signals", d: date, env: environment }
        )
        if (record) {
            return c.json(200, { ok: true, date: date, environment: environment, data: record.get("data") || {} })
        }
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    } catch (_) {
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    }
})

// POST /api/custom/ibkr/state/signals
routerAdd("POST", "/api/custom/ibkr/state/signals", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    const stateData = {
        processed_ids: d.processed_ids || [],
        confirmed_ids: d.confirmed_ids || [],
        active_signals: d.active_signals || []
    }

    try {
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_state",
                "state_key = {:k} && date = {:d} && environment = {:env}",
                { k: "ibkr_signals", d: date, env: environment }
            )
        } catch (_) {}
        const col = $app.findCollectionByNameOrId("ibkr_state")
        if (!record) {
            record = new Record(col, {})
        }
        record.set("state_key", "ibkr_signals")
        record.set("date", date)
        record.set("environment", environment)
        record.set("data", stateData)
        $app.save(record)
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// GET /api/custom/ibkr/state/orders?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/ibkr/state/orders", (c) => {
    const date = c.request.url.query().get("date") || ""
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "ibkr_state", "state_key = {:k} && date = {:d} && environment = {:env}",
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

// POST /api/custom/ibkr/state/orders
routerAdd("POST", "/api/custom/ibkr/state/orders", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
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
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_state",
                "state_key = {:k} && date = {:d} && environment = {:env}",
                { k: "orders", d: date, env: environment }
            )
        } catch (_) {}
        const col = $app.findCollectionByNameOrId("ibkr_state")
        if (!record) {
            record = new Record(col, {})
        }
        record.set("state_key", "orders")
        record.set("date", date)
        record.set("environment", environment)
        record.set("data", stateData)
        $app.save(record)
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// POST /api/custom/ibkr/health-report — IBKR Compute 健康数据上报
routerAdd("POST", "/api/custom/ibkr/health-report", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    // 写 system_events
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        record.set("event_type", "heartbeat")
        record.set("level", "info")
        record.set("source", "ibkr_compute")
        record.set("environment", environment)
        record.set("title", labelTitleWithEnvironment("IBKR 健康上报", environment))
        record.set("detail", addEnvironmentToDetail(d, environment))
        record.set("us_time", d.et_time || "")
        record.set("cn_time", d.bj_time || "")
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    return c.json(200, { ok: true })
})

// POST /api/custom/ibkr/notify — IBKR Compute 通知转发
routerAdd("POST", "/api/custom/ibkr/notify", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    const notifyType = d.type || "status"
    const title = labelTitleWithEnvironment(d.title || "", environment)
    const detail = addEnvironmentToDetail(d.data || d.detail || {}, environment)

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
        record.set("source", "ibkr_compute")
        record.set("environment", environment)
        record.set("title", title)
        record.set("detail", detail)
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    // 发飞书
    var notified = feishuSystem.notifySystemEvent("status_change", level, "ibkr_compute", title, detail, environment)

    return c.json(200, { ok: true, notified: notified })
})

console.log("[IBKRActions] Hook 文件加载完成");
