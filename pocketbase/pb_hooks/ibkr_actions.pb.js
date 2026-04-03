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

function ibkrActionsUpsertRecord(collectionName, filterStr, filterParams, data) {
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
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    if (!bars.length) {
        return c.json(400, { ok: false, error: "Empty bars array" })
    }

    const enabled = getConfigValue("ibkr_bar_publish_enabled", "true", defaultEnvironment)
    if (enabled !== "true") {
        return c.json(200, { ok: true, skipped: true, reason: "ibkr_bar_publish_enabled=false" })
    }

    let created = 0
    let updated = 0
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
            let record = null
            try {
                record = $app.findFirstRecordByFilter(
                    "ibkr_bars",
                    "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
                    { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment }
                )
            } catch (_) {}

            const col = $app.findCollectionByNameOrId("ibkr_bars")
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
            console.error(`[IBKRActions] bars upsert error: ${symbol}/${interval}/${barTimeMs}: ${err.message}`)
        }
    }

    console.log(`[IBKRActions] bars: received=${bars.length}, created=${created}, updated=${updated}, errors=${errors}`)
    return c.json(200, { ok: true, received: bars.length, created, updated, errors })
})

// ══════════════════════════════════════
// 指标写入 -> ibkr_indicators
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/indicator", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

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
        extra: {
            ...(d.extra || {}),
            environment: environment,
            source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
        },
    }

    const filterStr = "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}"
    const filterParams = { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment }

    try {
        ibkrActionsUpsertRecord("ibkr_indicators", filterStr, filterParams, recordData)
        return c.json(200, { ok: true, symbol, interval, collection: "ibkr_indicators" })
    } catch (err) {
        console.error(`[IBKRActions] indicator upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

// ══════════════════════════════════════
// 信号写入 -> ibkr_signals
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/signal", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const signalId = String(d.signal_id || "").trim()
    const environment = String(d.environment || "live").trim().toLowerCase() || "live"

    if (!symbol || !signalId) {
        return c.json(400, { ok: false, error: "Missing symbol or signal_id" })
    }

    try {
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_signals",
                "signal_id = {:sid} && environment = {:env}",
                { sid: signalId, env: environment }
            )
        } catch (_) {}

        const col = $app.findCollectionByNameOrId("ibkr_signals")
        if (!record) {
            record = new Record(col, {})
        }
        record.set("symbol", symbol)
        record.set("environment", environment)
        record.set("direction", String(d.direction || "").trim())
        record.set("signal", String(d.signal || "").trim())
        record.set("limit_price", Number(d.limit_price) || 0)
        record.set("entry", Number(d.entry) || 0)
        record.set("stop_loss", Number(d.stop_loss) || 0)
        record.set("take_profit", Number(d.take_profit) || 0)
        record.set("rr", String(d.rr || ""))
        record.set("shares", Number(d.shares) || 0)
        record.set("signal_id", signalId)
        record.set("exchange", String(d.exchange || "").trim().toUpperCase())
        record.set("interval", String(d.interval || "").trim())
        record.set("reason", String(d.reason || ""))
        record.set("us_time", String(d.us_time || "").trim())
        record.set("cn_time", String(d.cn_time || "").trim())
        record.set("date", String(d.date || "").trim())
        record.set("bar_time_ms", d.bar_time_ms ? Math.trunc(Number(d.bar_time_ms)) : 0)
        record.set("bar_index", d.bar_index != null ? Number(d.bar_index) : null)
        record.set("script_tag", String(d.script_tag || "").trim())
        record.set("chart_tf", String(d.chart_tf || "").trim())
        record.set("extra", {
            ...(d.extra || {}),
            source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
            environment: environment,
        })
        record.set("status", String(d.status || "pending"))
        record.set("note", String(d.note || ""))
        $app.save(record)
        return c.json(200, { ok: true, signal_id: signalId, target: "ibkr_signals", id: record.id || "" })
    } catch (err) {
        console.error(`[IBKRActions] signal upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err), target: "ibkr_signals" })
    }
})

// ══════════════════════════════════════
// IBKR 盘前扫描结果 → ibkr_targets
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/scan", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

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
        ibkrActionsUpsertRecord(
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
        return c.json(resp.statusCode || 200, withMeta(parsePayload(resp.raw), upstream))
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
    try {
        const resp = $http.send({
            url: `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/status`,
            method: "GET",
            timeout: 10,
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        return c.json(200, payload)
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
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/start`
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
        payload.proxy_route = "/api/custom/ibkr/start"
        payload.proxy_upstream = upstream
        return c.json(resp.statusCode || 200, payload)
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
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/stop`
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
        payload.proxy_route = "/api/custom/ibkr/stop"
        payload.proxy_upstream = upstream
        return c.json(resp.statusCode || 200, payload)
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
        return c.json(resp.statusCode || 200, payload)
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
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getStatePayload } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const payload = getStatePayload(environment)
    return c.json(200, {
        ok: true,
        environment: payload.environment,
        date: payload.date,
        state: payload.data || {},
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
