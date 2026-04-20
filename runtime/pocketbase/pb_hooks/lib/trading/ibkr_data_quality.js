function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback
    const normalized = String(value || "").trim().toLowerCase()
    if (["true", "1", "yes", "y"].indexOf(normalized) !== -1) return true
    if (["false", "0", "no", "n"].indexOf(normalized) !== -1) return false
    return fallback
}

function serializeIntegrityRecord(record) {
    return {
        id: record.id || "",
        environment: record.get("environment") || "",
        market_date: record.get("market_date") || "",
        symbol: record.get("symbol") || "",
        interval: record.get("interval") || "5m",
        scan_scope: record.get("scan_scope") || "",
        status: record.get("status") || "",
        needs_repair: Boolean(record.get("needs_repair")),
        safe_repair: Boolean(record.get("safe_repair")),
        bar_count: Number(record.get("bar_count") || 0) || 0,
        latest_bar_time_ms: Number(record.get("latest_bar_time_ms") || 0) || 0,
        latest_bar_us_time: record.get("latest_bar_us_time") || "",
        oldest_loaded_ms: Number(record.get("oldest_loaded_ms") || 0) || 0,
        gap_count: Number(record.get("gap_count") || 0) || 0,
        duplicate_count: Number(record.get("duplicate_count") || 0) || 0,
        bad_ohlc_count: Number(record.get("bad_ohlc_count") || 0) || 0,
        missing_intervals: record.get("missing_intervals") || [],
        stale_intervals: record.get("stale_intervals") || [],
        gap_examples: record.get("gap_examples") || [],
        duplicate_examples: record.get("duplicate_examples") || [],
        bad_ohlc_examples: record.get("bad_ohlc_examples") || [],
        repair_attempts: Number(record.get("repair_attempts") || 0) || 0,
        last_scan_at: record.get("last_scan_at") || "",
        last_repair_at: record.get("last_repair_at") || "",
        last_repair_result: record.get("last_repair_result") || {},
        extra: record.get("extra") || {},
        created: record.get("created") || "",
        updated: record.get("updated") || "",
    }
}

function serializeTruthRecord(record) {
    return {
        id: record.id || "",
        environment: record.get("environment") || "",
        market_date: record.get("market_date") || "",
        symbol: record.get("symbol") || "",
        interval: record.get("interval") || "5m",
        window_start_ms: Number(record.get("window_start_ms") || 0) || 0,
        window_end_ms: Number(record.get("window_end_ms") || 0) || 0,
        sampled_bar_count: Number(record.get("sampled_bar_count") || 0) || 0,
        matched_bar_count: Number(record.get("matched_bar_count") || 0) || 0,
        missing_stored_bar_count: Number(record.get("missing_stored_bar_count") || 0) || 0,
        missing_ibkr_bar_count: Number(record.get("missing_ibkr_bar_count") || 0) || 0,
        bar_mismatch_count: Number(record.get("bar_mismatch_count") || 0) || 0,
        indicator_mismatch_count: Number(record.get("indicator_mismatch_count") || 0) || 0,
        signal_mismatch_count: Number(record.get("signal_mismatch_count") || 0) || 0,
        status: record.get("status") || "",
        mismatch_examples: record.get("mismatch_examples") || [],
        source_meta: record.get("source_meta") || {},
        last_checked_at: record.get("last_checked_at") || "",
        created: record.get("created") || "",
        updated: record.get("updated") || "",
    }
}

function latestByKey(records, serialize, keyBuilder) {
    const items = []
    const seen = {}
    for (let i = 0; i < (records || []).length; i++) {
        const item = serialize(records[i])
        const key = String(keyBuilder(item) || "").trim()
        if (!key || seen[key]) continue
        seen[key] = true
        items.push(item)
    }
    return items
}

function loadEffectiveWatchlistSymbols(environment) {
    const runtimeEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    const rows = $app.findRecordsByFilter(
        "watchlist",
        "environment = {:env} || environment = 'global' || environment = ''",
        "-updated",
        500,
        0,
        { env: runtimeEnvironment },
    ) || []
    const priority = { "": 0, global: 1 }
    priority[runtimeEnvironment] = 2
    const bestBySymbol = {}
    for (let i = 0; i < rows.length; i++) {
        const row = rows[i]
        const symbol = String(row.get("symbol") || "").trim().toUpperCase()
        if (!symbol) continue
        const rowEnvironment = String(row.get("environment") || "").trim().toLowerCase()
        const rank = priority[rowEnvironment]
        if (rank == null) continue
        if (!bestBySymbol[symbol] || rank > bestBySymbol[symbol].rank) {
            bestBySymbol[symbol] = { rank: rank }
        }
    }
    return Object.keys(bestBySymbol).sort()
}

function loadIntegrityItems(environment, opts) {
    const options = opts || {}
    const filterParts = ['environment = {:env}']
    const filterParams = { env: environment }
    if (options.market_date) {
        filterParts.push('market_date = {:date}')
        filterParams.date = options.market_date
    }
    if (options.scan_scope) {
        filterParts.push('scan_scope = {:scope}')
        filterParams.scope = options.scan_scope
    }
    if (options.symbol) {
        filterParts.push('symbol = {:symbol}')
        filterParams.symbol = options.symbol
    }
    const rows = $app.findRecordsByFilter(
        "ibkr_bar_integrity",
        filterParts.join(" && "),
        "-updated",
        0,
        0,
        filterParams,
    ) || []
    return latestByKey(
        rows,
        serializeIntegrityRecord,
        (item) => `${String(item.symbol || "").trim().toUpperCase()}::${String(item.interval || "5m").trim()}`
    )
}

function loadTruthItems(environment, opts) {
    const options = opts || {}
    const filterParts = ['environment = {:env}']
    const filterParams = { env: environment }
    if (options.market_date) {
        filterParts.push('market_date = {:date}')
        filterParams.date = options.market_date
    }
    if (options.symbol) {
        filterParts.push('symbol = {:symbol}')
        filterParams.symbol = options.symbol
    }
    let rows = []
    try {
        rows = $app.findRecordsByFilter(
            "ibkr_bar_truth_audit",
            filterParts.join(" && "),
            "-updated",
            0,
            0,
            filterParams,
        ) || []
    } catch (err) {
        console.log(`[IBKRActions] ibkr_bar_truth_audit unavailable, fallback to empty rows: ${err.message || err}`)
        rows = []
    }
    return latestByKey(
        rows,
        serializeTruthRecord,
        (item) => `${String(item.symbol || "").trim().toUpperCase()}::${String(item.interval || "5m").trim()}`
    )
}

function resolveProof(item) {
    const integrityStatus = String(item && item.status || "").trim().toLowerCase()
    const truthStatus = String(item && item.truth_status || "").trim().toLowerCase()
    const duplicateCount = Number(item && item.duplicate_count || 0) || 0
    const badOhlcCount = Number(item && item.bad_ohlc_count || 0) || 0
    if (duplicateCount > 0 || badOhlcCount > 0 || integrityStatus === "error" || integrityStatus === "repair_failed" || truthStatus === "error") {
        return {
            status: "red",
            title: "证明失败",
            copy: truthStatus === "error"
                ? "IBKR 真值比对发现缺失或字段不一致。"
                : "内部一致性存在人工复核或修复失败问题。",
        }
    }
    if ((integrityStatus === "ok" || integrityStatus === "repaired") && truthStatus === "ok") {
        return {
            status: "green",
            title: "证明通过",
            copy: "内部一致性正常，且已通过 IBKR 真值比对。",
        }
    }
    let copy = "当前仍缺少完整证明。"
    if (!integrityStatus) {
        copy = "尚未完成内部一致性扫描。"
    } else if (integrityStatus === "warn" || Boolean(item && item.needs_repair)) {
        copy = "内部一致性仍有待补齐问题。"
    } else if (!truthStatus) {
        copy = "尚未运行 IBKR 真值审计。"
    } else if (truthStatus === "unavailable") {
        copy = "真值审计本轮不可用，请检查网关或会话。"
    }
    return {
        status: "yellow",
        title: "证明未完成",
        copy: copy,
    }
}

function mergeItems(integrityItems, truthItems) {
    const merged = {}
    const buildKey = (item) => `${String(item && item.symbol || "").trim().toUpperCase()}::${String(item && item.interval || "5m").trim()}`
    for (let i = 0; i < (integrityItems || []).length; i++) {
        const item = integrityItems[i]
        const key = buildKey(item)
        merged[key] = {
            ...item,
            truth_audit: null,
            truth_status: "",
            truth_checked_at: "",
            proof: { status: "yellow", title: "证明未完成", copy: "尚未运行 IBKR 真值审计。" },
            row_updated_at: String(item.updated || item.last_scan_at || ""),
        }
    }
    for (let i = 0; i < (truthItems || []).length; i++) {
        const truth = truthItems[i]
        const key = buildKey(truth)
        const base = merged[key] || {
            id: "",
            environment: truth.environment,
            market_date: truth.market_date,
            symbol: truth.symbol,
            interval: truth.interval,
            scan_scope: "",
            status: "",
            needs_repair: false,
            safe_repair: false,
            bar_count: 0,
            latest_bar_time_ms: 0,
            latest_bar_us_time: "",
            oldest_loaded_ms: 0,
            gap_count: 0,
            duplicate_count: 0,
            bad_ohlc_count: 0,
            missing_intervals: [],
            stale_intervals: [],
            gap_examples: [],
            duplicate_examples: [],
            bad_ohlc_examples: [],
            repair_attempts: 0,
            last_scan_at: "",
            last_repair_at: "",
            last_repair_result: {},
            extra: {},
            created: "",
            updated: "",
            row_updated_at: "",
        }
        base.truth_audit = truth
        base.truth_status = String(truth.status || "").trim()
        base.truth_checked_at = String(truth.last_checked_at || "").trim()
        base.row_updated_at = String(
            truth.updated
            || truth.last_checked_at
            || base.row_updated_at
            || base.updated
            || base.last_scan_at
            || ""
        )
        base.proof = resolveProof(base)
        merged[key] = base
    }
    const items = Object.keys(merged).map((key) => {
        const item = merged[key]
        item.proof = resolveProof(item)
        return item
    })
    items.sort((left, right) => String(right.row_updated_at || "").localeCompare(String(left.row_updated_at || "")))
    return items
}

function buildSummary(environment, marketDate, scanScope, mergedItems, expectedSymbols) {
    const items = Array.isArray(mergedItems) ? mergedItems : []
    const expected = Array.isArray(expectedSymbols) ? expectedSymbols.slice() : []
    const integritySymbols = {}
    const truthSymbols = {}
    const summary = {
        environment: environment,
        market_date: marketDate,
        total: items.length,
        needs_repair: 0,
        manual_review: 0,
        latest_scan_at: "",
        latest_repair_at: "",
        latest_truth_checked_at: "",
        status_counts: { ok: 0, warn: 0, error: 0, repaired: 0, repair_failed: 0, repairing: 0, missing: 0 },
        truth_status_counts: { ok: 0, error: 0, unavailable: 0, missing: 0 },
        proof_status_counts: { green: 0, yellow: 0, red: 0 },
        scan_scope_counts: { active_target: 0, watchlist: 0, manual: 0 },
        symbols: [],
        expected_symbols_total: expected.length,
        scanned_symbols_total: 0,
        truth_audited_symbols_total: 0,
        coverage_complete: false,
        truth_coverage_complete: false,
        unscanned_symbols: [],
        unaudited_symbols: [],
        database_correctness_status: "yellow",
        database_correctness_copy: "当前还不能证明整库 bar 正确。",
    }

    const symbolSet = {}
    for (let i = 0; i < items.length; i++) {
        const item = items[i]
        const symbol = String(item.symbol || "").trim().toUpperCase()
        if (!symbol) continue
        symbolSet[symbol] = true
        const status = String(item.status || "missing").trim().toLowerCase() || "missing"
        const truthStatus = String(item.truth_status || "").trim().toLowerCase() || "missing"
        const proofStatus = String(item.proof && item.proof.status || "yellow").trim().toLowerCase() || "yellow"
        const scope = String(item.scan_scope || "").trim()
        if (summary.status_counts[status] == null) summary.status_counts[status] = 0
        if (summary.truth_status_counts[truthStatus] == null) summary.truth_status_counts[truthStatus] = 0
        if (summary.proof_status_counts[proofStatus] == null) summary.proof_status_counts[proofStatus] = 0
        if (summary.scan_scope_counts[scope] == null) summary.scan_scope_counts[scope] = 0
        summary.status_counts[status]++
        summary.truth_status_counts[truthStatus]++
        summary.proof_status_counts[proofStatus]++
        if (scope) summary.scan_scope_counts[scope]++
        if (status !== "missing") integritySymbols[symbol] = true
        if (truthStatus !== "missing") truthSymbols[symbol] = true
        if (item.needs_repair) summary.needs_repair++
        if ((Number(item.duplicate_count || 0) || 0) > 0 || (Number(item.bad_ohlc_count || 0) || 0) > 0) {
            summary.manual_review++
        }
        if (item.last_scan_at && (!summary.latest_scan_at || item.last_scan_at > summary.latest_scan_at)) {
            summary.latest_scan_at = item.last_scan_at
        }
        if (item.last_repair_at && (!summary.latest_repair_at || item.last_repair_at > summary.latest_repair_at)) {
            summary.latest_repair_at = item.last_repair_at
        }
        if (item.truth_checked_at && (!summary.latest_truth_checked_at || item.truth_checked_at > summary.latest_truth_checked_at)) {
            summary.latest_truth_checked_at = item.truth_checked_at
        }
    }

    summary.symbols = Object.keys(symbolSet).sort()
    summary.scanned_symbols_total = Object.keys(integritySymbols).length
    summary.truth_audited_symbols_total = Object.keys(truthSymbols).length
    if (expected.length) {
        summary.unscanned_symbols = expected.filter((symbol) => !integritySymbols[symbol])
        summary.unaudited_symbols = expected.filter((symbol) => !truthSymbols[symbol])
        summary.coverage_complete = summary.unscanned_symbols.length === 0
        summary.truth_coverage_complete = summary.unaudited_symbols.length === 0
    } else {
        summary.coverage_complete = false
        summary.truth_coverage_complete = false
    }

    if (expected.length <= 0) {
        summary.database_correctness_status = "yellow"
        summary.database_correctness_copy = "当前范围没有可证明的观察池标的。"
    } else if (summary.proof_status_counts.red > 0) {
        summary.database_correctness_status = "red"
        summary.database_correctness_copy = "至少有一个标的内部一致性失败或 IBKR 真值比对失败。"
    } else if (summary.coverage_complete && summary.truth_coverage_complete && summary.proof_status_counts.green === expected.length) {
        summary.database_correctness_status = "green"
        summary.database_correctness_copy = "全观察池已完成内部扫描与 IBKR 真值比对，当前库内 bar 可证明为正确。"
    } else {
        summary.database_correctness_status = "yellow"
        summary.database_correctness_copy = "当前仍缺少完整覆盖或真值审计，整库还不能被证明为正确。"
    }

    return summary
}

function buildTruthSummary(environment, marketDate, truthItems, expectedSymbols) {
    const items = Array.isArray(truthItems) ? truthItems : []
    const expected = Array.isArray(expectedSymbols) ? expectedSymbols.slice() : []
    const seenSymbols = {}
    const summary = {
        environment: environment,
        market_date: marketDate,
        total: items.length,
        latest_checked_at: "",
        status_counts: { ok: 0, error: 0, unavailable: 0 },
        expected_symbols_total: expected.length,
        audited_symbols_total: 0,
        coverage_complete: false,
        unscanned_symbols: [],
        symbols: [],
    }
    for (let i = 0; i < items.length; i++) {
        const item = items[i]
        const symbol = String(item.symbol || "").trim().toUpperCase()
        if (symbol) seenSymbols[symbol] = true
        const status = String(item.status || "unavailable").trim().toLowerCase() || "unavailable"
        if (summary.status_counts[status] == null) summary.status_counts[status] = 0
        summary.status_counts[status]++
        if (item.last_checked_at && (!summary.latest_checked_at || item.last_checked_at > summary.latest_checked_at)) {
            summary.latest_checked_at = item.last_checked_at
        }
    }
    summary.symbols = Object.keys(seenSymbols).sort()
    summary.audited_symbols_total = summary.symbols.length
    if (expected.length) {
        summary.unscanned_symbols = expected.filter((symbol) => !seenSymbols[symbol])
        summary.coverage_complete = summary.unscanned_symbols.length === 0
    }
    return summary
}

function persistRows(environment, route, items, timeoutSeconds) {
    if (!Array.isArray(items) || !items.length) {
        return {}
    }
    const resp = $http.send({
        url: route,
        method: "POST",
        timeout: timeoutSeconds,
        body: JSON.stringify({ environment, items }),
        headers: { "Content-Type": "application/json" },
    })
    try {
        return resp.raw ? JSON.parse(resp.raw) : {}
    } catch (_) {
        return {
            ok: false,
            statusCode: Number(resp.statusCode) || 200,
            raw: String(resp.raw || ""),
        }
    }
}

function runProxyAction(environment, route, upstream, timeoutSeconds, persistRoute, body) {
    const resp = $http.send({
        url: upstream,
        method: "POST",
        timeout: timeoutSeconds,
        body: JSON.stringify(body || {}),
        headers: { "Content-Type": "application/json" },
    })
    let payload = {}
    try {
        payload = resp.raw ? JSON.parse(resp.raw) : {}
    } catch (_) {
        payload = { ok: false, raw: String(resp.raw || "") }
    }
    if (Array.isArray(payload.rows) && payload.rows.length && persistRoute) {
        try {
            payload.persistence = persistRows(environment, persistRoute, payload.rows, 20)
        } catch (persistErr) {
            payload.persistence = { ok: false, error: persistErr.message || String(persistErr) }
        }
    }
    payload.proxy_source = "pocketbase_ibkr_hook"
    payload.proxy_route = route
    payload.proxy_upstream = upstream
    return { payload, statusCode: Number(resp.statusCode) || 200 }
}

const exported = {
    parseBoolean,
    loadEffectiveWatchlistSymbols,
    loadIntegrityItems,
    loadTruthItems,
    resolveProof,
    mergeItems,
    buildSummary,
    buildTruthSummary,
    persistRows,
    runProxyAction,
}

if (typeof globalThis !== "undefined") {
    if (!globalThis.__ibkrDataQualityHelpers) {
        globalThis.__ibkrDataQualityHelpers = exported
    }
}

module.exports = exported
