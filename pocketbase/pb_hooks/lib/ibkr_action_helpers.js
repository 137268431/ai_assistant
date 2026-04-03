const VOLATILE_COMPARE_KEYS = {
    computed_at_ms: true,
    computed_at_us: true,
    computed_at_cn: true,
}

function normalizeForCompare(value) {
    if (Array.isArray(value)) {
        return value.map((item) => normalizeForCompare(item))
    }
    if (typeof value === "string") {
        const trimmed = value.trim()
        if (
            (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
            (trimmed.startsWith("[") && trimmed.endsWith("]"))
        ) {
            try {
                return normalizeForCompare(JSON.parse(trimmed))
            } catch (_) {}
        }
        if (/^-?\d+(\.\d+)?$/.test(trimmed)) {
            return Number(trimmed)
        }
        if (trimmed === "true") return true
        if (trimmed === "false") return false
        return value
    }
    if (value && typeof value === "object") {
        const normalized = {}
        Object.keys(value).sort().forEach((key) => {
            if (VOLATILE_COMPARE_KEYS[key]) {
                return
            }
            normalized[key] = normalizeForCompare(value[key])
        })
        return normalized
    }
    if (typeof value === "number") {
        return Number.isFinite(value) ? Number(value) : null
    }
    return value == null ? null : value
}

function valuesEqual(left, right) {
    return JSON.stringify(normalizeForCompare(left)) === JSON.stringify(normalizeForCompare(right))
}

function recordNeedsUpdate(record, data) {
    return Object.keys(data || {}).some((key) => !valuesEqual(record.get(key), data[key]))
}

function upsertRecord(collectionName, filterStr, filterParams, data) {
    let record = null
    try {
        record = $app.findFirstRecordByFilter(collectionName, filterStr, filterParams)
    } catch (_) {}

    const col = $app.findCollectionByNameOrId(collectionName)
    let action = "updated"
    if (!record) {
        record = new Record(col, {})
        action = "created"
    } else if (!recordNeedsUpdate(record, data)) {
        return { record: record, action: "skipped" }
    }

    Object.keys(data).forEach((key) => {
        record.set(key, data[key])
    })
    $app.save(record)
    return { record: record, action: action }
}

function buildIndicatorData(d, environment) {
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

function buildSignalData(d, environment) {
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

function upsertConfigValue(key, value, environment, extras) {
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

const exported = {
    normalizeForCompare,
    valuesEqual,
    recordNeedsUpdate,
    upsertRecord,
    buildIndicatorData,
    buildSignalData,
    upsertConfigValue,
}

if (typeof globalThis !== "undefined") {
    if (!globalThis.__ibkrActionHelpers) {
        globalThis.__ibkrActionHelpers = exported
    }
}

module.exports = exported
