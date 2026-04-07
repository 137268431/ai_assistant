/// <reference path="./pb_data/types.d.ts" />

console.log("[IBKRBacktest] Hook file loading...")

routerAdd("POST", "/api/custom/ibkr/backtest/run", (c) => {
    const route = "/api/custom/ibkr/backtest/run"
    const parseJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const buildMeta = (payload, upstream) => {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_backtest.pb.js"
        base.proxy_route = route
        base.proxy_upstream = upstream
        return base
    }

    try {
        const reqInfo = c.requestInfo()
        const data = reqInfo.body || reqInfo.data || {}
        const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/backtest/run`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify(data),
            headers: { "Content-Type": "application/json" },
            timeout: 120,
        })
        return c.json(resp.statusCode || 200, buildMeta(parseJson(resp.raw || "{}"), upstream))
    } catch (err) {
        console.error(`[IBKRBacktest] run error: ${err.message}`)
        return c.json(502, buildMeta({ ok: false, error: err.message || String(err) }, ""))
    }
})

routerAdd("GET", "/api/custom/ibkr/backtest/status", (c) => {
    const route = "/api/custom/ibkr/backtest/status"
    const parseJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const buildMeta = (payload, upstream) => {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_backtest.pb.js"
        base.proxy_route = route
        base.proxy_upstream = upstream
        return base
    }

    try {
        const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/backtest/status`
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        return c.json(resp.statusCode || 200, buildMeta(parseJson(resp.raw || "{}"), upstream))
    } catch (err) {
        console.error(`[IBKRBacktest] status error: ${err.message}`)
        return c.json(502, buildMeta({ ok: false, error: err.message || String(err) }, ""))
    }
})

routerAdd("POST", "/api/custom/ibkr/backtest/cancel", (c) => {
    const route = "/api/custom/ibkr/backtest/cancel"
    const parseJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const buildMeta = (payload, upstream) => {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_backtest.pb.js"
        base.proxy_route = route
        base.proxy_upstream = upstream
        return base
    }

    try {
        const reqInfo = c.requestInfo()
        const data = reqInfo.body || reqInfo.data || {}
        const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/backtest/cancel`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify(data),
            headers: { "Content-Type": "application/json" },
            timeout: 30,
        })
        return c.json(resp.statusCode || 200, buildMeta(parseJson(resp.raw || "{}"), upstream))
    } catch (err) {
        console.error(`[IBKRBacktest] cancel error: ${err.message}`)
        return c.json(502, buildMeta({ ok: false, error: err.message || String(err) }, ""))
    }
})

routerAdd("GET", "/api/custom/ibkr/backtest/replay", (c) => {
    const route = "/api/custom/ibkr/backtest/replay"
    const parseJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const buildMeta = (payload, upstream) => {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_backtest.pb.js"
        base.proxy_route = route
        base.proxy_upstream = upstream
        return base
    }
    const buildQuery = (query) => {
        const pairs = []
        Object.keys(query || {}).forEach((key) => {
            const value = query[key]
            if (value == null || value === "") return
            pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
        })
        return pairs.length ? `?${pairs.join("&")}` : ""
    }

    try {
        const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const query = c.request.url.query()
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/backtest/replay${buildQuery({
            run_id: query.get("run_id") || "",
            symbol: query.get("symbol") || "",
            center_bar_ms: query.get("center_bar_ms") || "",
            window: query.get("window") || "",
        })}`
        const resp = $http.send({ url: upstream, method: "GET", timeout: 30 })
        return c.json(resp.statusCode || 200, buildMeta(parseJson(resp.raw || "{}"), upstream))
    } catch (err) {
        console.error(`[IBKRBacktest] replay error: ${err.message}`)
        return c.json(502, buildMeta({ ok: false, error: err.message || String(err) }, ""))
    }
})

routerAdd("POST", "/api/custom/ibkr/backtest/cleanup", (c) => {
    const route = "/api/custom/ibkr/backtest/cleanup"
    const parseJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const buildMeta = (payload, upstream) => {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_backtest.pb.js"
        base.proxy_route = route
        base.proxy_upstream = upstream
        return base
    }

    try {
        const reqInfo = c.requestInfo()
        const data = reqInfo.body || reqInfo.data || {}
        const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/backtest/cleanup`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify(data),
            headers: { "Content-Type": "application/json" },
            timeout: 30,
        })
        return c.json(resp.statusCode || 200, buildMeta(parseJson(resp.raw || "{}"), upstream))
    } catch (err) {
        console.error(`[IBKRBacktest] cleanup error: ${err.message}`)
        return c.json(502, buildMeta({ ok: false, error: err.message || String(err) }, ""))
    }
})

console.log("[IBKRBacktest] Hook file loaded")
