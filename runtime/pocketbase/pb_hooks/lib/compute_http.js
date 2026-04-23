function parseHttpJson(resp) {
    if (!resp) return {}
    const raw = typeof resp.raw === "string"
        ? resp.raw
        : (typeof resp === "string" ? resp : String(resp.raw || ""))
    return raw ? JSON.parse(raw) : {}
}

function buildComputeCandidateUrls(environment, path, internalDefault, publicDefault) {
    const { getIbkrComputeInternalUrl, getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const normalizedPath = String(path || "").trim()
    const internalBase = String(
        getIbkrComputeInternalUrl(environment || "live", internalDefault || "http://127.0.0.1:5100") || ""
    ).trim().replace(/\/+$/, "")
    const publicBase = String(
        getIbkrComputePublicUrl(environment || "live", publicDefault || "http://127.0.0.1:5100") || ""
    ).trim().replace(/\/+$/, "")
    const candidates = []
    const seen = {}
    ;[internalBase, publicBase].forEach((base) => {
        if (!base) return
        const url = `${base}${normalizedPath}`
        if (seen[url]) return
        seen[url] = true
        candidates.push(url)
    })
    return candidates
}

function buildRuntimeCandidateUrls(environment, path, internalDefault, publicDefault) {
    const { getIbkrRuntimeInternalUrl, getIbkrRuntimePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const normalizedPath = String(path || "").trim()
    const internalBase = String(
        getIbkrRuntimeInternalUrl(environment || "live", internalDefault || "http://127.0.0.1:5101") || ""
    ).trim().replace(/\/+$/, "")
    const publicBase = String(
        getIbkrRuntimePublicUrl(environment || "live", publicDefault || "") || ""
    ).trim().replace(/\/+$/, "")
    const candidates = []
    const seen = {}
    ;[internalBase, publicBase].forEach((base) => {
        if (!base) return
        const url = `${base}${normalizedPath}`
        if (seen[url]) return
        seen[url] = true
        candidates.push(url)
    })
    return candidates
}

function fetchComputeJsonWithFallback(path, timeoutSeconds, environment, options) {
    const candidates = buildComputeCandidateUrls(
        environment,
        path,
        options && options.internalDefault,
        options && options.publicDefault
    )
    const attempts = []
    let lastCode = 0
    let lastError = ""

    for (let i = 0; i < candidates.length; i++) {
        const url = candidates[i]
        try {
            const resp = $http.send({
                url: url,
                method: "GET",
                timeout: timeoutSeconds || 5,
            })
            const code = Number(resp && resp.statusCode || 0) || 0
            if (code === 200) {
                try {
                    return {
                        payload: parseHttpJson(resp),
                        upstream: url,
                        attempts: attempts,
                        status_code: code,
                    }
                } catch (err) {
                    const detail = `invalid_json: ${err && err.message ? err.message : err}`
                    lastCode = code
                    lastError = detail
                    attempts.push({ url: url, code: code, error: detail })
                    continue
                }
            }

            const detail = code > 0 ? `HTTP ${code}` : "unknown_http_error"
            lastCode = code
            lastError = detail
            attempts.push({ url: url, code: code, error: detail })
        } catch (err) {
            const detail = err && err.message ? err.message : String(err)
            lastCode = 0
            lastError = detail
            attempts.push({ url: url, code: 0, error: detail })
        }
    }

    return {
        payload: {
            ok: false,
            status: lastCode >= 400 ? "error" : "offline",
            code: lastCode,
            error: lastError || "compute_request_failed",
        },
        upstream: "",
        attempts: attempts,
        status_code: lastCode,
    }
}

function fetchRuntimeJsonWithFallback(path, timeoutSeconds, environment, options) {
    const candidates = buildRuntimeCandidateUrls(
        environment,
        path,
        options && options.internalDefault,
        options && options.publicDefault
    )
    const attempts = []
    let lastCode = 0
    let lastError = ""

    for (let i = 0; i < candidates.length; i++) {
        const url = candidates[i]
        try {
            const resp = $http.send({
                url: url,
                method: "GET",
                timeout: timeoutSeconds || 5,
            })
            const code = Number(resp && resp.statusCode || 0) || 0
            if (code === 200) {
                try {
                    return {
                        payload: parseHttpJson(resp),
                        upstream: url,
                        attempts: attempts,
                        status_code: code,
                    }
                } catch (err) {
                    const detail = `invalid_json: ${err && err.message ? err.message : err}`
                    lastCode = code
                    lastError = detail
                    attempts.push({ url: url, code: code, error: detail })
                    continue
                }
            }

            const detail = code > 0 ? `HTTP ${code}` : "unknown_http_error"
            lastCode = code
            lastError = detail
            attempts.push({ url: url, code: code, error: detail })
        } catch (err) {
            const detail = err && err.message ? err.message : String(err)
            lastCode = 0
            lastError = detail
            attempts.push({ url: url, code: 0, error: detail })
        }
    }

    return {
        payload: {
            ok: false,
            status: lastCode >= 400 ? "error" : "offline",
            code: lastCode,
            error: lastError || "runtime_request_failed",
        },
        upstream: "",
        attempts: attempts,
        status_code: lastCode,
    }
}

module.exports = {
    parseHttpJson,
    buildComputeCandidateUrls,
    buildRuntimeCandidateUrls,
    fetchComputeJsonWithFallback,
    fetchRuntimeJsonWithFallback,
}
