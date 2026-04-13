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

function normalizeSignalSourceMeta(rawValue) {
    const raw = String(rawValue || "").trim().toLowerCase()
    if (["tv", "tradingview", "webhook_tv", "tv_webhook", "signal"].includes(raw)) {
        return {
            route_source: "tradingview",
            signal_source: "tradingview_webhook",
            signal_source_label: "TradingView Webhook",
            signal_source_detail: "来自 TradingView webhook 信号",
        }
    }
    if (["ibkr_compute_timeline", "timeline", "chart_timeline"].includes(raw)) {
        return {
            route_source: "ibkr_compute",
            signal_source: "ibkr_compute_timeline",
            signal_source_label: "IBKR 图表回放",
            signal_source_detail: "来自缓存 bars 时间线重算",
        }
    }
    if (["history_repair", "recompute", "backfill_recompute"].includes(raw)) {
        return {
            route_source: "ibkr_compute",
            signal_source: "ibkr_history_recompute",
            signal_source_label: "IBKR 历史重算",
            signal_source_detail: "来自历史回补/重算链路",
        }
    }
    if (["manual", "manual_order", "runtime_page", "account_page"].includes(raw)) {
        return {
            route_source: "manual",
            signal_source: "manual_order",
            signal_source_label: "手动触发",
            signal_source_detail: "来自账户页/人工操作",
        }
    }
    if (["ibkr_runtime", "ibkr_compute_realtime", "ibkr_compute", "ibkr"].includes(raw)) {
        return {
            route_source: "ibkr_compute",
            signal_source: "ibkr_compute_realtime",
            signal_source_label: "IBKR 实时计算",
            signal_source_detail: "来自 IBKR 实盘 bars 收盘计算",
        }
    }
    return {
        route_source: raw || "unknown",
        signal_source: raw || "unknown",
        signal_source_label: raw ? raw.toUpperCase() : "未知来源",
        signal_source_detail: "",
    }
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

function getRecordExtra(record) {
    if (!record) return {}
    const value = record.get("extra")
    if (value && typeof value === "object" && !Array.isArray(value)) {
        return value
    }
    if (typeof value === "string" && value) {
        try {
            const parsed = JSON.parse(value)
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
        } catch (_) {}
    }
    return {}
}

function buildSignalBarDedupeKey(data, environment) {
    const symbol = String(data.symbol || "").trim().toUpperCase()
    const direction = String(data.direction || "").trim().toLowerCase()
    const barTimeMs = Math.trunc(Number(data.bar_time_ms) || 0)
    const interval = String(data.interval || "").trim()
    const chartTf = String(data.chart_tf || "").trim()
    const scriptTag = String(data.script_tag || "").trim()
    const runtimeEnvironment = String(environment || data.environment || "live").trim().toLowerCase() || "live"
    if (!symbol || !direction || !barTimeMs) {
        return ""
    }
    return [
        runtimeEnvironment,
        symbol,
        direction,
        barTimeMs,
        interval || "-",
        chartTf || "-",
        scriptTag || "-",
    ].join("|")
}

function findSignalDuplicateByBarKey(data, environment, excludeSignalId) {
    const symbol = String(data.symbol || "").trim().toUpperCase()
    const direction = String(data.direction || "").trim().toLowerCase()
    const barTimeMs = Math.trunc(Number(data.bar_time_ms) || 0)
    const runtimeEnvironment = String(environment || data.environment || "live").trim().toLowerCase() || "live"
    if (!symbol || !direction || !barTimeMs) {
        return null
    }

    let filter = "environment = {:env} && symbol = {:sym} && direction = {:dir} && bar_time_ms = {:ms}"
    const params = {
        env: runtimeEnvironment,
        sym: symbol,
        dir: direction,
        ms: barTimeMs,
    }

    const interval = String(data.interval || "").trim()
    const chartTf = String(data.chart_tf || "").trim()
    const scriptTag = String(data.script_tag || "").trim()
    const signalId = String(excludeSignalId || data.signal_id || "").trim()

    if (interval) {
        filter += " && interval = {:tf}"
        params.tf = interval
    }
    if (chartTf) {
        filter += " && chart_tf = {:chartTf}"
        params.chartTf = chartTf
    }
    if (scriptTag) {
        filter += " && script_tag = {:scriptTag}"
        params.scriptTag = scriptTag
    }
    if (signalId) {
        filter += " && signal_id != {:sid}"
        params.sid = signalId
    }

    try {
        const rows = $app.findRecordsByFilter("ibkr_signals", filter, "-updated,-created", 5, 0, params) || []
        return rows.length > 0 ? rows[0] : null
    } catch (_) {
        return null
    }
}

function annotateSignalDuplicate(record, incomingData, environment) {
    if (!record || !incomingData) return record

    const extra = getRecordExtra(record)
    const duplicateSignalIds = Array.isArray(extra.duplicate_signal_ids) ? extra.duplicate_signal_ids.slice() : []
    const incomingSignalId = String(incomingData.signal_id || "").trim()
    if (incomingSignalId && duplicateSignalIds.indexOf(incomingSignalId) === -1) {
        duplicateSignalIds.push(incomingSignalId)
    }

    const incomingExtra = incomingData.extra && typeof incomingData.extra === "object" && !Array.isArray(incomingData.extra)
        ? incomingData.extra
        : {}
    const sourceMeta = normalizeSignalSourceMeta(
        incomingData.signal_source
        || incomingExtra.signal_source
        || incomingData.source
        || incomingExtra.source
        || ""
    )

    record.set("extra", {
        ...extra,
        duplicate_signal_ids: duplicateSignalIds,
        duplicate_signal_count: duplicateSignalIds.length,
        last_duplicate_signal_id: incomingSignalId,
        last_duplicate_signal_at: new Date().toISOString(),
        last_duplicate_signal_source: String(
            incomingExtra.signal_source
            || incomingData.signal_source
            || sourceMeta.signal_source
            || ""
        ),
        last_duplicate_signal_source_label: String(
            incomingExtra.signal_source_label
            || incomingData.signal_source_label
            || sourceMeta.signal_source_label
            || ""
        ),
        duplicate_bar_dedupe_key: buildSignalBarDedupeKey(incomingData, environment),
    })
    $app.save(record)
    return record
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
    const incomingExtra = d.extra && typeof d.extra === "object" && !Array.isArray(d.extra) ? d.extra : {}

    if (!symbol || !signalId) {
        return { ok: false, error: "Missing symbol or signal_id" }
    }

    const sourceMeta = normalizeSignalSourceMeta(
        d.signal_source
        || incomingExtra.signal_source
        || d.source
        || incomingExtra.source
        || "ibkr_compute"
    )

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
                ...incomingExtra,
                source: sourceMeta.route_source,
                signal_source: String(incomingExtra.signal_source || d.signal_source || sourceMeta.signal_source),
                signal_source_label: String(incomingExtra.signal_source_label || d.signal_source_label || sourceMeta.signal_source_label),
                signal_source_detail: String(incomingExtra.signal_source_detail || d.signal_source_detail || sourceMeta.signal_source_detail),
                environment: environment,
            },
            status: String(d.status || "pending"),
            note: String(d.note || ""),
        },
    }
}

function buildBarIntegrityData(d, environment) {
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const marketDate = String(d.market_date || "").trim()
    const interval = String(d.interval || "5m").trim() || "5m"
    const scanScope = String(d.scan_scope || "manual").trim() || "manual"

    if (!symbol || !marketDate) {
        return { ok: false, error: "Missing symbol or market_date" }
    }

    return {
        ok: true,
        symbol: symbol,
        market_date: marketDate,
        interval: interval,
        filter: "environment = {:env} && market_date = {:date} && symbol = {:sym} && interval = {:tf}",
        params: { env: environment, date: marketDate, sym: symbol, tf: interval },
        data: {
            environment: environment,
            market_date: marketDate,
            symbol: symbol,
            interval: interval,
            scan_scope: scanScope,
            status: String(d.status || "ok").trim() || "ok",
            needs_repair: Boolean(d.needs_repair),
            safe_repair: Boolean(d.safe_repair),
            bar_count: Number(d.bar_count) || 0,
            latest_bar_time_ms: Math.trunc(Number(d.latest_bar_time_ms) || 0),
            latest_bar_us_time: String(d.latest_bar_us_time || "").trim(),
            oldest_loaded_ms: Math.trunc(Number(d.oldest_loaded_ms) || 0),
            gap_count: Number(d.gap_count) || 0,
            duplicate_count: Number(d.duplicate_count) || 0,
            bad_ohlc_count: Number(d.bad_ohlc_count) || 0,
            missing_intervals: Array.isArray(d.missing_intervals) ? d.missing_intervals : [],
            stale_intervals: Array.isArray(d.stale_intervals) ? d.stale_intervals : [],
            gap_examples: Array.isArray(d.gap_examples) ? d.gap_examples : [],
            duplicate_examples: Array.isArray(d.duplicate_examples) ? d.duplicate_examples : [],
            bad_ohlc_examples: Array.isArray(d.bad_ohlc_examples) ? d.bad_ohlc_examples : [],
            repair_attempts: Number(d.repair_attempts) || 0,
            last_scan_at: String(d.last_scan_at || "").trim(),
            last_repair_at: String(d.last_repair_at || "").trim(),
            last_repair_result: d.last_repair_result || {},
            extra: d.extra || {},
        },
        increment_repair_attempts: Boolean(d.increment_repair_attempts),
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
    buildSignalBarDedupeKey,
    findSignalDuplicateByBarKey,
    annotateSignalDuplicate,
    buildIndicatorData,
    buildSignalData,
    buildBarIntegrityData,
    upsertConfigValue,
}

if (typeof globalThis !== "undefined") {
    if (!globalThis.__ibkrActionHelpers) {
        globalThis.__ibkrActionHelpers = exported
    }
}

module.exports = exported
