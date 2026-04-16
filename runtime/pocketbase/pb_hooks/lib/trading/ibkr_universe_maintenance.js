function parseJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}

function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") {
        return Boolean(fallback)
    }
    if (typeof value === "boolean") {
        return value
    }
    const normalized = String(value || "").trim().toLowerCase()
    if (["1", "true", "yes", "y", "on"].includes(normalized)) return true
    if (["0", "false", "no", "n", "off"].includes(normalized)) return false
    return Boolean(fallback)
}

function getCurrentRuntimeMarketDate(environment) {
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const fallbackDate = String(getTimeStrings().date || "").trim()
    try {
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/status`
        const response = $http.send({
            url: upstream,
            method: "GET",
            timeout: 8,
        })
        const payload = parseJson(response.raw)
        const runtimeDate = String(
            (payload && payload.market_universe && payload.market_universe.market_date)
            || (payload && payload.market_date)
            || ""
        ).trim()
        return runtimeDate || fallbackDate
    } catch (_) {
        return fallbackDate
    }
}

function callUniverseReconcile(environment, payload) {
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/universe/reconcile`
    const response = $http.send({
        url: upstream,
        method: "POST",
        timeout: 180,
        body: JSON.stringify({
            ...(payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {}),
            environment: environment,
        }),
        headers: { "Content-Type": "application/json" },
    })
    return {
        statusCode: Number(response && response.statusCode) > 0 ? Number(response && response.statusCode) : 200,
        payload: parseJson(response.raw),
        upstream: upstream,
    }
}

function findRecordByIdOrFilter(collectionName, recordId, filter, params) {
    if (recordId) {
        try {
            return $app.findFirstRecordByFilter(collectionName, "id = {:id}", { id: String(recordId || "").trim() })
        } catch (_) {}
    }
    if (filter) {
        try {
            return $app.findFirstRecordByFilter(collectionName, filter, params || {})
        } catch (_) {}
    }
    return null
}

function deleteCollectionRecord(record) {
    if (!record || !record.id) return false
    $app.delete(record)
    return true
}

function findWatchlistRecordForSymbol(symbol, environment) {
    try {
        return $app.findFirstRecordByFilter(
            "watchlist",
            "symbol = {:sym} && environment = {:env}",
            { sym: String(symbol || "").trim().toUpperCase(), env: String(environment || "live").trim().toLowerCase() || "live" }
        )
    } catch (_) {
        return null
    }
}

function listActiveTodayTargets(symbol, environment, marketDate) {
    const normalizedSymbol = String(symbol || "").trim().toUpperCase()
    const normalizedEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    const normalizedMarketDate = String(marketDate || "").trim()
    if (!normalizedSymbol || !normalizedMarketDate) return []
    try {
        return $app.findRecordsByFilter(
            "ibkr_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env} && (status = \"candidate\" || status = \"active\")",
            "-updated",
            1000,
            0,
            { sym: normalizedSymbol, d: normalizedMarketDate, env: normalizedEnvironment }
        ) || []
    } catch (_) {
        return []
    }
}

function hasEffectiveWatchlistMember(symbol, environment) {
    const normalizedSymbol = String(symbol || "").trim().toUpperCase()
    const normalizedEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    if (!normalizedSymbol) return false
    try {
        const rows = $app.findRecordsByFilter(
            "watchlist",
            "symbol = {:sym} && (environment = {:env} || environment = \"global\" || environment = \"\")",
            "-updated",
            20,
            0,
            { sym: normalizedSymbol, env: normalizedEnvironment }
        ) || []
        return rows.length > 0
    } catch (_) {
        return false
    }
}

function ensureTargetWatchlistRecord(opts) {
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const times = getTimeStrings()
    const normalizedSymbol = String(opts && opts.symbol || "").trim().toUpperCase()
    const environment = String(opts && opts.environment || "live").trim().toLowerCase() || "live"
    if (!normalizedSymbol) {
        return { action: "skipped", manual_member: false }
    }

    let existing = null
    try {
        existing = $app.findFirstRecordByFilter(
            "watchlist",
            "symbol = {:sym} && environment = {:env}",
            { sym: normalizedSymbol, env: environment }
        )
    } catch (_) {}

    const preservedManualMember = existing ? parseBoolean(existing.get("manual_member"), false) : false
    const payload = {
        symbol: normalizedSymbol,
        environment: environment,
        exchange: String(
            (opts && opts.exchange)
            || (existing ? existing.get("exchange") : "")
            || "SMART"
        ).trim().toUpperCase(),
        industry: String(
            (opts && opts.industry)
            || (existing ? existing.get("industry") : "")
            || ""
        ).trim(),
        note: String(existing ? (existing.get("note") || "") : "").trim(),
        symbol_role: String(existing ? (existing.get("symbol_role") || "trade") : "trade").trim() || "trade",
        manual_member: preservedManualMember,
        created_us: String(existing ? (existing.get("created_us") || times.us) : times.us).trim(),
        created_cn: String(existing ? (existing.get("created_cn") || times.cn) : times.cn).trim(),
        updated_us: times.us,
        updated_cn: times.cn,
        us_time: times.us,
        cn_time: times.cn,
        bar_time_ms: Date.now(),
    }
    const result = actionHelpers.upsertRecord(
        "watchlist",
        "symbol = {:sym} && environment = {:env}",
        { sym: normalizedSymbol, env: environment },
        payload
    )
    return {
        action: result.action,
        id: result.record && result.record.id ? result.record.id : "",
        manual_member: preservedManualMember,
    }
}

function removeAutoWatchlistRecordIfEligible(symbol, environment, marketDate) {
    const remainingTargets = listActiveTodayTargets(symbol, environment, marketDate)
    if (remainingTargets.length > 0) {
        return { removed: false, reason: "target_still_active" }
    }

    const record = findWatchlistRecordForSymbol(symbol, environment)
    if (!record) {
        return { removed: false, reason: "watchlist_missing" }
    }
    if (parseBoolean(record.get("manual_member"), true)) {
        return { removed: false, reason: "manual_watchlist_retained", id: record.id || "" }
    }

    deleteCollectionRecord(record)
    return { removed: true, reason: "auto_target_watchlist_removed", id: record.id || "" }
}

module.exports = {
    parseBoolean,
    getCurrentRuntimeMarketDate,
    callUniverseReconcile,
    findRecordByIdOrFilter,
    deleteCollectionRecord,
    findWatchlistRecordForSymbol,
    listActiveTodayTargets,
    hasEffectiveWatchlistMember,
    ensureTargetWatchlistRecord,
    removeAutoWatchlistRecordIfEligible,
}
