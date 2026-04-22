const LIVE_ENVIRONMENT = "live"
const PAPER_ENVIRONMENT = "paper"
const BACKTEST_ENVIRONMENT = "backtest"
const GLOBAL_ENVIRONMENT = "global"

const RUNTIME_ENVIRONMENTS = [LIVE_ENVIRONMENT, PAPER_ENVIRONMENT, BACKTEST_ENVIRONMENT]
const CONFIG_ENVIRONMENTS = [LIVE_ENVIRONMENT, PAPER_ENVIRONMENT, BACKTEST_ENVIRONMENT, GLOBAL_ENVIRONMENT]

const ENVIRONMENT_LABELS = {
    live: "LIVE",
    paper: "PAPER",
    backtest: "BACKTEST",
    global: "GLOBAL",
}

const DEFAULT_IBKR_COMPUTE_PUBLIC_URL = "https://qc.lzw-glory.top"
const DEFAULT_IBKR_COMPUTE_INTERNAL_URL = "http://127.0.0.1:5100"
const DEFAULT_IBKR_API_PUBLIC_URL = ""
const DEFAULT_IBKR_API_INTERNAL_URL = "http://127.0.0.1:5102"
const DEFAULT_IBKR_SCHEDULER_INTERNAL_URL = "http://127.0.0.1:5103"
const DEFAULT_IBKR_RUNTIME_PUBLIC_URL = ""
const DEFAULT_IBKR_RUNTIME_INTERNAL_URL = "http://127.0.0.1:5101"

function normalizeRuntimeEnvironment(value, defaultValue) {
    const fallback = RUNTIME_ENVIRONMENTS.includes(defaultValue) ? defaultValue : LIVE_ENVIRONMENT
    const text = String(value || "").trim().toLowerCase()
    const aliases = {
        prod: LIVE_ENVIRONMENT,
        production: LIVE_ENVIRONMENT,
        sim: PAPER_ENVIRONMENT,
        simulated: PAPER_ENVIRONMENT,
        simulation: PAPER_ENVIRONMENT,
        test: BACKTEST_ENVIRONMENT,
    }
    const normalized = aliases[text] || text
    return RUNTIME_ENVIRONMENTS.includes(normalized) ? normalized : fallback
}

function normalizeConfigEnvironment(value, defaultValue) {
    const fallback = CONFIG_ENVIRONMENTS.includes(defaultValue) ? defaultValue : GLOBAL_ENVIRONMENT
    const text = String(value || "").trim().toLowerCase()
    if (!text) return fallback
    if (text === GLOBAL_ENVIRONMENT) return GLOBAL_ENVIRONMENT
    return normalizeRuntimeEnvironment(text, fallback === GLOBAL_ENVIRONMENT ? LIVE_ENVIRONMENT : fallback)
}

function getRuntimeEnvironmentFromData(data, defaultValue) {
    return normalizeRuntimeEnvironment(data && data.environment, defaultValue || LIVE_ENVIRONMENT)
}

function getRuntimeEnvironmentFromRequest(c, defaultValue) {
    let value = ""
    try {
        value = c.request.url.query().get("environment") || ""
    } catch (_) {}
    if (!value) {
        const reqInfo = c.requestInfo()
        const data = reqInfo.body || reqInfo.data || {}
        value = data.environment || ""
    }
    return normalizeRuntimeEnvironment(value, defaultValue || LIVE_ENVIRONMENT)
}

function getRecordEnvironment(recordOrData, defaultValue) {
    if (!recordOrData) {
        return normalizeRuntimeEnvironment(defaultValue || LIVE_ENVIRONMENT, LIVE_ENVIRONMENT)
    }
    const get = typeof recordOrData.get === "function"
        ? recordOrData.get.bind(recordOrData)
        : function(field) { return recordOrData[field] }
    const extra = get("extra")
    const extraEnvironment = extra && typeof extra === "object" ? extra.environment : ""
    return normalizeRuntimeEnvironment(get("environment") || extraEnvironment, defaultValue || LIVE_ENVIRONMENT)
}

function getEnvironmentTag(environment) {
    const normalized = normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT)
    return `[${ENVIRONMENT_LABELS[normalized] || normalized.toUpperCase()}]`
}

function labelTitleWithEnvironment(title, environment) {
    const text = String(title || "").trim()
    if (!text) return getEnvironmentTag(environment)
    const tag = getEnvironmentTag(environment)
    return text.startsWith(tag) ? text : `${tag} ${text}`
}

function addEnvironmentToDetail(detail, environment) {
    const env = normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT)
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        return { environment: env, ...detail }
    }
    if (detail == null || detail === "") {
        return { environment: env }
    }
    return { environment: env, detail: String(detail) }
}

function attachEnvironment(extra, environment) {
    return {
        ...(extra && typeof extra === "object" ? extra : {}),
        environment: normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT),
    }
}

function hasConfigOverride(key, environment) {
    const configEnvironment = normalizeConfigEnvironment(environment, GLOBAL_ENVIRONMENT)
    if (!key || configEnvironment === GLOBAL_ENVIRONMENT) {
        return false
    }

    try {
        const records = $app.findRecordsByFilter(
            "config",
            "key = {:k} && environment = {:env}",
            "-updated",
            1,
            0,
            { k: key, env: configEnvironment }
        ) || []
        return records.length > 0
    } catch (_) {
        return false
    }
}

function rankConfigEnvironment(recordEnvironment, runtimeEnvironment) {
    const normalized = String(recordEnvironment || "").trim().toLowerCase()
    if (normalized === runtimeEnvironment) return 2
    if (normalized === GLOBAL_ENVIRONMENT || normalized === "") return 1
    return -1
}

function getEffectiveConfigRecord(key, environment) {
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT)
    let records = []
    try {
        records = $app.findRecordsByFilter(
            "config",
            "key = {:k} && (environment = {:env} || environment = 'global' || environment = '')",
            "-updated",
            20,
            0,
            { k: key, env: runtimeEnvironment }
        ) || []
    } catch (_) {
        try {
            records = $app.findRecordsByFilter("config", "key = {:k}", "-updated", 20, 0, { k: key }) || []
        } catch (_) {
            records = []
        }
    }

    let best = null
    let bestRank = -1
    for (let i = 0; i < records.length; i++) {
        const record = records[i]
        const rank = rankConfigEnvironment(record.get("environment"), runtimeEnvironment)
        if (rank > bestRank) {
            best = record
            bestRank = rank
        }
    }
    return best
}

function getConfigValue(key, defaultValue, environment) {
    const best = getEffectiveConfigRecord(key, environment)
    return best ? String(best.get("value") || defaultValue) : defaultValue
}

function listEffectiveConfigRecords(environment) {
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT)
    let records = []
    try {
        records = $app.findRecordsByFilter(
            "config",
            "environment = {:env} || environment = 'global' || environment = ''",
            "-updated",
            500,
            0,
            { env: runtimeEnvironment }
        ) || []
    } catch (_) {
        records = []
    }

    const bestByKey = {}
    const keyOrder = []
    for (let i = 0; i < records.length; i++) {
        const record = records[i]
        const key = String(record.get("key") || "").trim()
        if (!key) continue

        const rank = rankConfigEnvironment(record.get("environment"), runtimeEnvironment)
        if (rank < 0) continue

        if (!bestByKey[key]) {
            bestByKey[key] = { record, rank }
            keyOrder.push(key)
            continue
        }

        if (rank > bestByKey[key].rank) {
            bestByKey[key] = { record, rank }
        }
    }

    return keyOrder.map((key) => bestByKey[key].record)
}

function getIbkrComputePublicUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_COMPUTE_PUBLIC_URL || "").trim() || DEFAULT_IBKR_COMPUTE_PUBLIC_URL
    const configured = getConfigValue("ibkr_compute_public_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "") || fallback
}

function getIbkrComputeInternalUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_COMPUTE_INTERNAL_URL || "").trim() || DEFAULT_IBKR_COMPUTE_INTERNAL_URL
    const configured = getConfigValue("ibkr_compute_internal_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "") || fallback
}

function getIbkrRuntimePublicUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_RUNTIME_PUBLIC_URL || "").trim()
    const configured = getConfigValue("ibkr_runtime_public_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "")
}

function getIbkrApiPublicUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_API_PUBLIC_URL || "").trim()
    const configured = getConfigValue("ibkr_api_public_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "")
}

function getIbkrApiInternalUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_API_INTERNAL_URL || "").trim() || DEFAULT_IBKR_API_INTERNAL_URL
    const configured = getConfigValue("ibkr_api_internal_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "") || fallback
}

function getIbkrSchedulerInternalUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_SCHEDULER_INTERNAL_URL || "").trim() || DEFAULT_IBKR_SCHEDULER_INTERNAL_URL
    const configured = getConfigValue("ibkr_scheduler_internal_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "") || fallback
}

function getIbkrRuntimeInternalUrl(environment, defaultValue) {
    const fallback = String(defaultValue || DEFAULT_IBKR_RUNTIME_INTERNAL_URL || "").trim() || DEFAULT_IBKR_RUNTIME_INTERNAL_URL
    const configured = getConfigValue("ibkr_runtime_internal_url", fallback, environment)
    return String(configured || fallback).trim().replace(/\/+$/, "") || fallback
}

const exported = {
    LIVE_ENVIRONMENT,
    PAPER_ENVIRONMENT,
    BACKTEST_ENVIRONMENT,
    GLOBAL_ENVIRONMENT,
    normalizeRuntimeEnvironment,
    normalizeConfigEnvironment,
    getRuntimeEnvironmentFromData,
    getRuntimeEnvironmentFromRequest,
    getRecordEnvironment,
    getEnvironmentTag,
    labelTitleWithEnvironment,
    addEnvironmentToDetail,
    attachEnvironment,
    hasConfigOverride,
    getEffectiveConfigRecord,
    getConfigValue,
    listEffectiveConfigRecords,
    getIbkrApiPublicUrl,
    getIbkrApiInternalUrl,
    getIbkrSchedulerInternalUrl,
    getIbkrComputePublicUrl,
    getIbkrComputeInternalUrl,
    getIbkrRuntimePublicUrl,
    getIbkrRuntimeInternalUrl,
}

if (typeof globalThis !== "undefined") {
    if (!globalThis.__gloryEnvUtils) {
        globalThis.__gloryEnvUtils = exported
    }
    if (!globalThis.envUtils) {
        globalThis.envUtils = globalThis.__gloryEnvUtils
    }
}

module.exports = exported
