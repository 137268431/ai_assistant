function parseJson(raw) {
    if (!raw) return {}
    const text = typeof raw === "string" ? raw : String(raw || "")
    if (!text) return {}
    try {
        const payload = JSON.parse(text)
        return payload && typeof payload === "object" ? payload : {}
    } catch (_) {
        return {}
    }
}

function buildQueryString(params) {
    const entries = []
    const source = params && typeof params === "object" ? params : {}
    for (const key in source) {
        if (!Object.prototype.hasOwnProperty.call(source, key)) continue
        const value = source[key]
        if (value == null || value === "") continue
        entries.push(`${encodeURIComponent(String(key))}=${encodeURIComponent(String(value))}`)
    }
    return entries.length ? `?${entries.join("&")}` : ""
}

function parseHeaders(rawHeaders) {
    if (!rawHeaders || typeof rawHeaders !== "object") return {}
    const normalized = {}
    for (const key in rawHeaders) {
        if (!Object.prototype.hasOwnProperty.call(rawHeaders, key)) continue
        const value = rawHeaders[key]
        if (Array.isArray(value)) {
            const items = value.map((item) => String(item || "")).filter(Boolean)
            if (items.length) {
                normalized[String(key)] = items
            }
            continue
        }
        if (value != null && value !== "") {
            normalized[String(key)] = [String(value)]
        }
    }
    return normalized
}

function applyResponseHeaders(c, headers) {
    if (!c || !c.response || !c.response.header || typeof c.response.header !== "object") return
    const source = headers && typeof headers === "object" ? headers : {}
    const excluded = {
        "connection": true,
        "content-length": true,
        "transfer-encoding": true,
    }
    for (const key in source) {
        if (!Object.prototype.hasOwnProperty.call(source, key)) continue
        if (excluded[String(key).toLowerCase()]) continue
        const values = Array.isArray(source[key])
            ? source[key].map((item) => String(item || "")).filter(Boolean)
            : []
        if (!values.length) continue
        c.response.header[String(key)] = values
    }
}

function forwardIbkrApiRequest(route, options) {
    const opts = options && typeof options === "object" ? options : {}
    const method = String(opts.method || "GET").trim().toUpperCase() || "GET"
    const environment = String(opts.environment || "live").trim().toLowerCase() || "live"
    const { getIbkrApiInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const query = {
        ...(opts.query && typeof opts.query === "object" ? opts.query : {}),
    }
    if (!("environment" in query) && environment) {
        query.environment = environment
    }
    const upstreamBase = getIbkrApiInternalUrl(environment, "http://127.0.0.1:5102")
    const upstream = `${upstreamBase}${route}${buildQueryString(query)}`
    const requestOptions = {
        url: upstream,
        method: method,
        timeout: Number(opts.timeout) > 0 ? Number(opts.timeout) : 10,
    }
    if (opts.body != null) {
        requestOptions.body = JSON.stringify(opts.body)
        requestOptions.headers = { "Content-Type": "application/json" }
    }
    const resp = $http.send(requestOptions)
    return {
        ok: Number(resp && resp.statusCode) < 400,
        statusCode: Number(resp && resp.statusCode) || 200,
        payload: parseJson(resp && resp.raw),
        headers: parseHeaders(resp && resp.headers),
        upstream: upstream,
    }
}

function forwardIbkrSchedulerRequest(route, options) {
    const opts = options && typeof options === "object" ? options : {}
    const method = String(opts.method || "GET").trim().toUpperCase() || "GET"
    const environment = String(opts.environment || "live").trim().toLowerCase() || "live"
    const { getIbkrSchedulerInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const query = {
        ...(opts.query && typeof opts.query === "object" ? opts.query : {}),
    }
    if (!("environment" in query) && environment) {
        query.environment = environment
    }
    const upstreamBase = getIbkrSchedulerInternalUrl(environment, "http://127.0.0.1:5103")
    const upstream = `${upstreamBase}${route}${buildQueryString(query)}`
    const requestOptions = {
        url: upstream,
        method: method,
        timeout: Number(opts.timeout) > 0 ? Number(opts.timeout) : 10,
    }
    if (opts.body != null) {
        requestOptions.body = JSON.stringify(opts.body)
        requestOptions.headers = { "Content-Type": "application/json" }
    }
    const resp = $http.send(requestOptions)
    return {
        ok: Number(resp && resp.statusCode) < 400,
        statusCode: Number(resp && resp.statusCode) || 200,
        payload: parseJson(resp && resp.raw),
        headers: parseHeaders(resp && resp.headers),
        upstream: upstream,
    }
}

function proxyIbkrApiJson(c, route, options) {
    try {
        const result = forwardIbkrApiRequest(route, options)
        const payload = result.payload && typeof result.payload === "object"
            ? result.payload
            : {}
        if (!payload.proxy_upstream) {
            payload.proxy_upstream = result.upstream
        }
        if (!payload.proxy_source) {
            payload.proxy_source = "pocketbase_ibkr_hook"
        }
        applyResponseHeaders(c, result.headers)
        return c.json(result.statusCode, payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err && err.message ? err.message : String(err || ""),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_target: "ibkr-api",
        })
    }
}

module.exports = {
    forwardIbkrApiRequest,
    forwardIbkrSchedulerRequest,
    proxyIbkrApiJson,
}
