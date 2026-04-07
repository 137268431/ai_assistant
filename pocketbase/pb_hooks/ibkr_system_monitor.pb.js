/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_system_monitor.pb.js
 * 系统监控: 事件接收 + 健康聚合 + 心跳 cron + 每日汇总
 */

console.log("[IBKRSystemMonitor] Hook 文件开始加载...")

const MONITORED_INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
const BAR_INTERVAL_MS = 5 * 60 * 1000
const BAR_LAG_ALERT_MS = 10 * 60 * 1000
const INDICATOR_LAG_ALERT_MS = 10 * 60 * 1000
const GAP_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const GAP_MONITOR_STATE_KEY = "system_gap_monitor"
const AUTH_PENDING_ALERT_TRIGGER_MS = 15 * 60 * 1000
const AUTH_PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_MONITOR_STATE_KEY = "system_auth_monitor"

function getRuntimeKeys() {
    return ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
}

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function uniqueSorted(values) {
    const seen = {}
    const output = []
    for (let i = 0; i < (values || []).length; i++) {
        const item = String(values[i] || "").trim().toUpperCase()
        if (!item || seen[item]) continue
        seen[item] = true
        output.push(item)
    }
    output.sort()
    return output
}

function getStateRecord(stateKey, environment, dateToken) {
    try {
        return $app.findFirstRecordByFilter(
            "ibkr_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: stateKey, d: dateToken, env: environment }
        )
    } catch (_) {
        return null
    }
}

function getStateData(stateKey, environment, dateToken) {
    const record = getStateRecord(stateKey, environment, dateToken)
    if (!record) return { record: null, data: {} }
    let data = record.get("data") || {}
    if (!data || typeof data !== "object") {
        data = {}
    }
    return { record: record, data: data }
}

function saveStateData(stateKey, environment, dateToken, patch) {
    const current = getStateData(stateKey, environment, dateToken)
    const collection = $app.findCollectionByNameOrId("ibkr_state")
    const record = current.record || new Record(collection, {})
    const next = {
        ...(current.data || {}),
        ...(patch || {}),
    }
    record.set("state_key", stateKey)
    record.set("date", dateToken)
    record.set("environment", environment)
    record.set("data", next)
    $app.save(record)
    return next
}

function parseHttpJson(resp) {
    if (!resp) return {}
    const raw = typeof resp.raw === "string" ? resp.raw : String(resp.raw || "")
    return raw ? JSON.parse(raw) : {}
}

function parseShiftedTimeMs(value, offsetMinutes) {
    const text = String(value || "").trim()
    if (!text) return 0
    const parsed = Date.parse(text.replace(" ", "T") + "Z")
    if (!Number.isFinite(parsed)) return 0
    return parsed - (Number(offsetMinutes || 0) * 60000)
}

function parseUsTimeMs(value) {
    return parseShiftedTimeMs(value, -4 * 60)
}

function fetchComputeJson(path, timeoutSeconds, environment) {
    try {
        const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputePublicUrl(environment || "live", "https://qc.lzw-glory.top")
        const resp = $http.send({ url: `${computeBaseUrl}${path}`, method: "GET", timeout: timeoutSeconds || 5 })
        if (resp.statusCode === 200) {
            return parseHttpJson(resp)
        }
        return { ok: false, status: "error", code: resp.statusCode }
    } catch (err) {
        return { ok: false, status: "offline", error: err.message }
    }
}

function loadComputeSnapshot(environment) {
    const health = fetchComputeJson("/health", 5, environment)
    const status = fetchComputeJson("/status", 5, environment)
    const engineMap = status && status.engines && typeof status.engines === "object" ? status.engines : {}
    const totalEngines = Number(status.total_engines || 0) || Object.keys(engineMap).length

    return {
        ok: health.ok !== false || status.ok !== false,
        status: health.status || status.status || "unknown",
        code: health.code || status.code || 0,
        error: health.error || status.error || "",
        engines: engineMap,
        total_engines: totalEngines,
        ready_engines: Number(status.ready_engines || 0) || 0,
        compute_count: Number(health.compute_count || status.compute_count || 0) || 0,
        error_count: Number(health.error_count || status.error_count || 0) || 0,
        uptime_s: Number(health.uptime_s || 0) || 0,
        last_compute: health.last_compute || status.last_compute || null,
        last_scan: health.last_scan || status.last_scan || null,
        compute_enabled: status.compute_enabled,
        compute_enabled_by_environment: status.compute_enabled_by_environment || {},
        supported_environments: status.supported_environments || [],
        default_environments: status.default_environments || [],
    }
}

function loadRuntimeSnapshot(environment) {
    try {
        const payload = fetchComputeJson("/ibkr/status", 8, environment)
        return {
            ok: payload.ok !== false,
            starting: payload.starting,
            session: payload.session || {},
            gateway: payload.gateway || {},
            websocket: payload.websocket || {},
            bar_aggregator: payload.bar_aggregator || {},
            order_tracker: payload.order_tracker || {},
            order_lifecycle: payload.order_lifecycle || {},
            signal_router: payload.signal_router || {},
            signal_processor: payload.signal_processor || {},
            market_universe: payload.market_universe || {},
        }
    } catch (err) {
        console.log(`[IBKRSystemMonitor] loadRuntimeSnapshot(${environment}) error: ${err.message || err}`)
        return {
            ok: false,
            starting: false,
            session: {},
            gateway: {},
            websocket: {},
            bar_aggregator: {},
            order_tracker: {},
            order_lifecycle: {},
            signal_router: {},
            signal_processor: {},
            market_universe: {},
        }
    }
}

function countCollectionRows(collectionName, filterStr, params) {
    try {
        const rows = $app.findRecordsByFilter(collectionName, filterStr, "", 0, 0, params || {}) || []
        return rows.length
    } catch (_) {
        return 0
    }
}

function loadTodayOverview(environment, times) {
    const todayStart = times && times.todayStart ? times.todayStart : `${times.date} 00:00:00`
    const overview = {
        today: {
            bars: 0,
            indicators: 0,
            signals: 0,
            orders: 0,
            events: 0,
            error_events: 0,
        },
        targets: loadTargetSymbols(environment, times.date).length,
        account: {
            ok: false,
            account_id: "",
            positions: 0,
            open_orders: 0,
            net_liquidation: 0,
        },
    }

    overview.today.bars = countCollectionRows("ibkr_bars", "created >= {:t} && environment = {:env}", { t: todayStart, env: environment })
    overview.today.indicators = countCollectionRows("ibkr_indicators", "created >= {:t} && environment = {:env}", { t: todayStart, env: environment })
    overview.today.signals = countCollectionRows("ibkr_signals", "created >= {:t} && environment = {:env}", { t: todayStart, env: environment })
    overview.today.orders = countCollectionRows("orders", "created >= {:t} && environment = {:env}", { t: todayStart, env: environment })
    overview.today.events = countCollectionRows("system_events", "created >= {:t} && environment = {:env}", { t: todayStart, env: environment })
    overview.today.error_events = countCollectionRows("system_events", "created >= {:t} && environment = {:env} && level = 'error'", { t: todayStart, env: environment })

    const accountSnapshot = fetchComputeJson("/ibkr/account", 10, environment)
    if (accountSnapshot && accountSnapshot.ok !== false) {
        overview.account = {
            ok: true,
            account_id: String(accountSnapshot.account_id || ""),
            positions: Number(accountSnapshot.counts && accountSnapshot.counts.open_positions || 0) || 0,
            open_orders: Number(accountSnapshot.counts && accountSnapshot.counts.open_orders || 0) || 0,
            net_liquidation: Number(accountSnapshot.summary && accountSnapshot.summary.net_liquidation || 0) || 0,
        }
    }

    return overview
}

function loadAuthAttentionSummary(environment, runtimeStatus) {
    const { getStatePayload } = require(`${__hooks}/lib/feishu_2fa.js`)
    const statePayload = getStatePayload(environment)
    const state = statePayload.data || {}
    const status = String(state.status || "").trim().toLowerCase()
    const hasRequest = Boolean(
        toNumber(state.request_count, 0) > 0
        || state.requested_at
        || state.triggered_at
        || state.message_id
    )
    const runtimeStarted = Boolean(
        runtimeStatus.starting
        || (runtimeStatus.session && runtimeStatus.session.running)
        || (runtimeStatus.websocket && runtimeStatus.websocket.running)
        || (runtimeStatus.order_tracker && runtimeStatus.order_tracker.running)
    )
    const runtimeAuthenticated = Boolean(runtimeStatus.session && runtimeStatus.session.authenticated)
    const gatewayReachable = Boolean(runtimeStatus.gateway && (runtimeStatus.gateway.running || runtimeStatus.gateway.reachable))
    const gatewayStatusCode = Number(runtimeStatus.gateway && runtimeStatus.gateway.status_code || 0) || 0
    const startedMs = Math.max(parseUsTimeMs(state.triggered_at), parseUsTimeMs(state.requested_at))
    const ageMin = startedMs > 0 ? Math.max(0, Math.round((Date.now() - startedMs) / 60000)) : 0
    const active = hasRequest && ["requested", "triggered", "waiting_confirm", "waiting_response"].indexOf(status) !== -1
    const needsAttention = gatewayReachable && (!runtimeAuthenticated || gatewayStatusCode === 401 || !runtimeStarted)
    const pendingTooLong = active && needsAttention && startedMs > 0 && (Date.now() - startedMs) >= AUTH_PENDING_ALERT_TRIGGER_MS

    return {
        status: status || "requested",
        has_request: hasRequest,
        active: active,
        pending_too_long: pendingTooLong,
        age_min: ageMin,
        requested_at: String(state.requested_at || ""),
        triggered_at: String(state.triggered_at || ""),
        mode: String(state.mode || ""),
        challenge_code: String(state.challenge_code || ""),
        response_status: String(state.response_status || ""),
        last_result: String(state.last_result || ""),
        last_error: String(state.last_error || ""),
        runtime_started: runtimeStarted,
        runtime_authenticated: runtimeAuthenticated,
        gateway_status_code: gatewayStatusCode,
    }
}

function loadFreshness(environment) {
    const freshness = {}
    for (let i = 0; i < MONITORED_INTERVALS.length; i++) {
        const tf = MONITORED_INTERVALS[i]
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "interval = {:i} && environment = {:env}", "-bar_time_ms", 1, 0, { i: tf, env: environment })
            if (bars && bars.length > 0) {
                const ms = Number(bars[0].get("bar_time_ms") || 0)
                freshness[tf] = {
                    last_bar_time_ms: ms,
                    age_min: Math.round((Date.now() - ms) / 60000),
                    symbol: bars[0].get("symbol") || "",
                }
            }
        } catch (_) {}
    }
    return freshness
}

function loadWatchlistSymbols(environment) {
    try {
        const rows = $app.findRecordsByFilter(
            "watchlist",
            "symbol != '' && (environment = {:env} || environment = 'global' || environment = '')",
            "",
            500,
            0,
            { env: environment || "live" }
        ) || []
        const symbols = []
        for (let i = 0; i < rows.length; i++) {
            symbols.push(String(rows[i].get("symbol") || "").toUpperCase())
        }
        return uniqueSorted(symbols)
    } catch (_) {
        return []
    }
}

function loadTargetSymbols(environment, date) {
    try {
        const rows = $app.findRecordsByFilter(
            "ibkr_targets",
            "date = {:d} && environment = {:env} && (status = 'candidate' || status = 'active')",
            "-score,-updated",
            500,
            0,
            { d: date, env: environment || "live" }
        ) || []
        const symbols = []
        const seen = {}
        for (let i = 0; i < rows.length; i++) {
            const symbol = String(rows[i].get("symbol") || "").toUpperCase()
            if (!symbol || seen[symbol]) continue
            seen[symbol] = true
            symbols.push(symbol)
        }
        return uniqueSorted(symbols)
    } catch (_) {
        return []
    }
}

function loadRecentRowsBySymbol(collection, environment, interval, todayStart, limit) {
    const rows = $app.findRecordsByFilter(
        collection,
        "environment = {:env} && interval = {:interval} && us_time >= {:start}",
        "-bar_time_ms",
        limit || 2000,
        0,
        { env: environment, interval: interval, start: todayStart }
    ) || []

    const latestBySymbol = {}
    const seriesBySymbol = {}

    for (let i = 0; i < rows.length; i++) {
        const row = rows[i]
        const symbol = String(row.get("symbol") || "").toUpperCase()
        if (!symbol) continue

        if (!latestBySymbol[symbol]) {
            latestBySymbol[symbol] = {
                bar_time_ms: toNumber(row.get("bar_time_ms"), 0),
                us_time: String(row.get("us_time") || ""),
                session_type: String(row.get("session_type") || ""),
            }
        }

        if (!seriesBySymbol[symbol]) {
            seriesBySymbol[symbol] = []
        }
        if (seriesBySymbol[symbol].length < 12) {
            seriesBySymbol[symbol].push({
                bar_time_ms: toNumber(row.get("bar_time_ms"), 0),
                us_time: String(row.get("us_time") || ""),
                session_type: String(row.get("session_type") || ""),
            })
        }
    }

    return {
        rows: rows,
        latest_by_symbol: latestBySymbol,
        series_by_symbol: seriesBySymbol,
    }
}

function buildGapFingerprint(summary) {
    return JSON.stringify({
        latest_bar_time_ms: summary.latest_bar_time_ms || 0,
        bar_lag_symbols: (summary.bar_lag_symbols || []).slice(0, 12),
        indicator_lag_symbols: (summary.indicator_lag_symbols || []).slice(0, 12),
        sequence_gap_examples: (summary.sequence_gap_examples || []).slice(0, 6),
    })
}

function loadDataGapSummary(environment, times) {
    try {
        const watchlistSymbols = loadWatchlistSymbols(environment)
        const targetSymbols = loadTargetSymbols(environment, times.date)
        const bars = loadRecentRowsBySymbol("ibkr_bars", environment, "5m", times.todayStart, 1200)
        const indicators = loadRecentRowsBySymbol("ibkr_indicators", environment, "5m", times.todayStart, 1200)
        const latestBarBySymbol = bars.latest_by_symbol || {}
        const latestIndicatorBySymbol = indicators.latest_by_symbol || {}
        const monitoredSymbols = targetSymbols.length > 0 ? targetSymbols : Object.keys(latestBarBySymbol)
        const symbols = uniqueSorted(monitoredSymbols.concat(Object.keys(latestBarBySymbol)))
        let latestBarTimeMs = 0
        let latestBarSymbol = ""

        const recentSymbols = Object.keys(latestBarBySymbol)
        for (let i = 0; i < recentSymbols.length; i++) {
            const symbol = recentSymbols[i]
            const barMs = toNumber(latestBarBySymbol[symbol] && latestBarBySymbol[symbol].bar_time_ms, 0)
            if (barMs > latestBarTimeMs) {
                latestBarTimeMs = barMs
                latestBarSymbol = symbol
            }
        }

        const barLagSymbols = []
        const indicatorLagSymbols = []
        const sequenceGapExamples = []
        let maxBarLagMs = 0
        let maxIndicatorLagMs = 0

        for (let i = 0; i < symbols.length; i++) {
            const symbol = symbols[i]
            const latestBar = latestBarBySymbol[symbol]
            const barMs = toNumber(latestBar && latestBar.bar_time_ms, 0)
            if (latestBarTimeMs > 0 && barMs > 0) {
                const lagMs = latestBarTimeMs - barMs
                if (lagMs >= BAR_LAG_ALERT_MS) {
                    barLagSymbols.push(symbol)
                    if (lagMs > maxBarLagMs) maxBarLagMs = lagMs
                }
            }

            const latestIndicator = latestIndicatorBySymbol[symbol]
            const indicatorMs = toNumber(latestIndicator && latestIndicator.bar_time_ms, 0)
            if (barMs > 0 && (barMs - indicatorMs) >= INDICATOR_LAG_ALERT_MS) {
                indicatorLagSymbols.push(symbol)
                if ((barMs - indicatorMs) > maxIndicatorLagMs) maxIndicatorLagMs = (barMs - indicatorMs)
            }

            if (sequenceGapExamples.length >= 6) continue
            const series = (bars.series_by_symbol && bars.series_by_symbol[symbol]) ? bars.series_by_symbol[symbol].slice() : []
            if (series.length < 3) continue
            series.sort((a, b) => a.bar_time_ms - b.bar_time_ms)
            for (let j = 1; j < series.length; j++) {
                const prev = series[j - 1]
                const curr = series[j]
                if (!prev || !curr) continue
                if (String(prev.session_type || "") !== "regular" || String(curr.session_type || "") !== "regular") continue
                const deltaMs = toNumber(curr.bar_time_ms, 0) - toNumber(prev.bar_time_ms, 0)
                if (deltaMs > BAR_INTERVAL_MS && deltaMs <= (6 * BAR_INTERVAL_MS)) {
                    sequenceGapExamples.push({
                        symbol: symbol,
                        prev_us_time: prev.us_time || "",
                        next_us_time: curr.us_time || "",
                        missing_points: Math.max(Math.round(deltaMs / BAR_INTERVAL_MS) - 1, 1),
                    })
                    break
                }
            }
        }

        const summary = {
            watchlist_count: watchlistSymbols.length,
            target_count: targetSymbols.length,
            monitored_symbol_count: monitoredSymbols.length,
            today_bar_symbol_count: Object.keys(latestBarBySymbol).length,
            latest_bar_time_ms: latestBarTimeMs,
            latest_bar_symbol: latestBarSymbol,
            latest_bar_us_time: latestBarSymbol && latestBarBySymbol[latestBarSymbol]
                ? latestBarBySymbol[latestBarSymbol].us_time || ""
                : "",
            bar_lag_symbols: barLagSymbols,
            indicator_lag_symbols: indicatorLagSymbols,
            sequence_gap_examples: sequenceGapExamples,
            bar_lag_count: barLagSymbols.length,
            indicator_lag_count: indicatorLagSymbols.length,
            sequence_gap_count: sequenceGapExamples.length,
            max_bar_lag_min: Math.round(maxBarLagMs / 60000),
            max_indicator_lag_min: Math.round(maxIndicatorLagMs / 60000),
            market_activity_detected: latestBarTimeMs > 0,
        }
        summary.has_issue = summary.market_activity_detected && (
            summary.bar_lag_count > 0
            || summary.indicator_lag_count > 0
            || summary.sequence_gap_count > 0
        )
        summary.fingerprint = buildGapFingerprint(summary)
        return summary
    } catch (err) {
        console.log(`[IBKRSystemMonitor] loadDataGapSummary(${environment}) error: ${err.message || err}`)
        return {
            watchlist_count: 0,
            target_count: 0,
            monitored_symbol_count: 0,
            today_bar_symbol_count: 0,
            latest_bar_time_ms: 0,
            latest_bar_symbol: "",
            latest_bar_us_time: "",
            bar_lag_symbols: [],
            indicator_lag_symbols: [],
            sequence_gap_examples: [],
            bar_lag_count: 0,
            indicator_lag_count: 0,
            sequence_gap_count: 0,
            max_bar_lag_min: 0,
            max_indicator_lag_min: 0,
            market_activity_detected: false,
            has_issue: false,
            fingerprint: "",
            error: String(err && err.message ? err.message : err || ""),
        }
    }
}

function logRouteError(route, err) {
    const message = err && err.message ? err.message : String(err || "unknown error")
    console.log(`[IBKRSystemMonitor] ${route} error: ${message}`)
    if (err && err.stack) {
        console.log(err.stack)
    }
    return message
}

routerAdd("POST", "/api/custom/system/event", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)

    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const rawTitle = d.title || ""
    const rawDetail = d.detail || {}
    const eventType = d.event_type || "status_change"
    const level = d.level || "info"
    const source = d.source || "pb"
    const title = labelTitleWithEnvironment(rawTitle, environment)
    const detail = addEnvironmentToDetail(rawDetail, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    let notified = false
    if (level === "error" || level === "warning" || eventType === "status_change" || eventType === "daily_report") {
        notified = feishuSystem.notifySystemEvent(eventType, level, source, title, detail, environment)
    }

    writeSystemEvent(eventType, level, source, rawTitle, rawDetail, environment, notified)
    return c.json(200, { ok: true, notified: notified })
})

routerAdd("GET", "/api/custom/system/healthz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, getConfigValue, getIbkrComputePublicUrl, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        let compute = {
            status: "offline",
            engines: 0,
            ready_engines: 0,
            total_engines: 0,
            compute_count: 0,
            error_count: 0,
            uptime_s: 0,
            last_compute: null,
            last_scan: null,
        }
        let runtime = {}
        try {
            const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
            const healthResp = $http.send({ url: `${computeBaseUrl}/health`, method: "GET", timeout: 5 })
            const healthData = JSON.parse(healthResp.raw || "{}")
            const statusResp = $http.send({ url: `${computeBaseUrl}/status`, method: "GET", timeout: 5 })
            const statusData = JSON.parse(statusResp.raw || "{}")
            try {
                const runtimeResp = $http.send({ url: `${computeBaseUrl}/ibkr/status`, method: "GET", timeout: 8 })
                runtime = JSON.parse(runtimeResp.raw || "{}")
            } catch (_) {}
            compute = {
                status: healthData.status || statusData.status || "unknown",
                engines: Number(statusData.total_engines || 0) || 0,
                ready_engines: Number(statusData.ready_engines || 0) || 0,
                total_engines: Number(statusData.total_engines || 0) || 0,
                compute_count: Number(healthData.compute_count || statusData.compute_count || 0) || 0,
                error_count: Number(healthData.error_count || statusData.error_count || 0) || 0,
                uptime_s: Number(healthData.uptime_s || 0) || 0,
                last_compute: healthData.last_compute || statusData.last_compute || null,
                last_scan: healthData.last_scan || statusData.last_scan || null,
            }
        } catch (err) {
            compute.status = "offline"
            compute.error = err.message || String(err)
        }

        let dataHealth = { status: "no_data" }
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: environment })
            if (bars && bars.length > 0) {
                const lastMs = Number(bars[0].get("bar_time_ms") || 0)
                const ageMin = Math.round((Date.now() - lastMs) / 60000)
                dataHealth = {
                    status: ageMin <= 5 ? "online" : (ageMin <= 15 ? "delayed" : "offline"),
                    last_bar_age_min: ageMin,
                    last_bar_time_ms: lastMs,
                    last_symbol: bars[0].get("symbol") || "",
                }
            }
        } catch (err) {
            dataHealth = { status: "unknown", error: err.message }
        }

        const computeEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_compute_enabled", "TRUE", environment)).trim().toLowerCase() !== "false"

        return c.json(200, {
            ok: compute.status === "running",
            environment: environment,
            pb: { status: "running" },
            ibkr_compute: {
                status: compute.status || "unknown",
                engines: compute.total_engines || 0,
                ready_engines: compute.ready_engines || 0,
                total_engines: compute.total_engines || 0,
                compute_count: compute.compute_count || 0,
                error_count: compute.error_count || 0,
                uptime_s: compute.uptime_s || 0,
                last_compute: compute.last_compute || null,
                last_scan: compute.last_scan || null,
            },
            ibkr_data: dataHealth,
            runtime: runtime,
            compute_enabled: computeEnabled,
        })
    } catch (err) {
        return c.json(500, {
            ok: false,
            error: logRouteError("/api/custom/system/healthz", err),
        })
    }
})

routerAdd("GET", "/api/custom/system/summaryz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, getConfigValue, getIbkrComputePublicUrl, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
        const times = getTimeStrings()
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        const liteMode = ["1", "true", "yes", "on"].indexOf(String(c.request.url.query().get("lite") || "").trim().toLowerCase()) !== -1
        const todayStart = times.date + " 00:00:00"
        const computeEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_compute_enabled", "TRUE", environment)).trim().toLowerCase() !== "false"
        const tradingEnabled = environment !== BACKTEST_ENVIRONMENT
            && String(getConfigValue("ibkr_trading_enabled", getConfigValue("trading_enabled", "TRUE", environment), environment)).trim().toLowerCase() !== "false"

        let computeSummary = {
            ok: false,
            status: "offline",
            engines: {},
            total_engines: 0,
            ready_engines: 0,
            compute_count: 0,
            error_count: 0,
            uptime_s: 0,
            last_compute: null,
            last_scan: null,
        }
        try {
            const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
            const healthResp = $http.send({ url: `${computeBaseUrl}/health`, method: "GET", timeout: 5 })
            const healthData = JSON.parse(healthResp.raw || "{}")
            const statusResp = $http.send({ url: `${computeBaseUrl}/status`, method: "GET", timeout: 5 })
            const statusData = JSON.parse(statusResp.raw || "{}")
            computeSummary = {
                ok: healthData.ok !== false || statusData.ok !== false,
                status: healthData.status || statusData.status || "unknown",
                engines: statusData.engines || {},
                total_engines: Number(statusData.total_engines || 0) || 0,
                ready_engines: Number(statusData.ready_engines || 0) || 0,
                compute_count: Number(healthData.compute_count || statusData.compute_count || 0) || 0,
                error_count: Number(healthData.error_count || statusData.error_count || 0) || 0,
                uptime_s: Number(healthData.uptime_s || 0) || 0,
                last_compute: healthData.last_compute || statusData.last_compute || null,
                last_scan: healthData.last_scan || statusData.last_scan || null,
            }
        } catch (err) {
            computeSummary.error = err.message || String(err)
        }
        const dataFreshness = []

        const summary = {
            timestamp: times.us,
            environment: environment,
            compute_enabled: computeEnabled,
            ibkr_trading_enabled: tradingEnabled,
            daily_target_filter: false,
            max_positions: "3",
            config: {},
            today: { ibkr_signals: 0, ibkr_indicators: 0, orders: 0, ibkr_bars: 0, ibkr_targets: 0, events: 0 },
            ibkr_compute: computeSummary,
            recent_events: [],
            data_freshness: dataFreshness,
            lite_mode: liteMode,
        }

        try {
            const configs = $app.findRecordsByFilter("config", "environment = {:env} || environment = 'global' || environment = ''", "", 200, 0, { env: environment }) || []
            for (let i = 0; i < configs.length; i++) {
                const key = configs[i].get("key")
                if (key) {
                    summary.config[String(key)] = String(configs[i].get("value") || "")
                }
            }
            if (summary.config.ibkr_compute_enabled) summary.compute_enabled = String(summary.config.ibkr_compute_enabled).trim().toLowerCase() === "true"
            if (summary.config.ibkr_trading_enabled) {
                summary.ibkr_trading_enabled = String(summary.config.ibkr_trading_enabled).trim().toLowerCase() === "true"
            } else if (summary.config.trading_enabled) {
                summary.ibkr_trading_enabled = String(summary.config.trading_enabled).trim().toLowerCase() === "true"
            }
            if (summary.config.max_positions) summary.max_positions = summary.config.max_positions
            if (summary.config.ibkr_target_filter_on) summary.daily_target_filter = String(summary.config.ibkr_target_filter_on).trim().toLowerCase() === "true"
        } catch (_) {}

        if (!liteMode) {
            try {
                const records = $app.findRecordsByFilter("ibkr_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
                summary.today.ibkr_signals = records ? records.length : 0
            } catch (_) {}
            try {
                const records = $app.findRecordsByFilter("ibkr_indicators", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
                summary.today.ibkr_indicators = records ? records.length : 0
            } catch (_) {}
            try {
                const records = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
                summary.today.orders = records ? records.length : 0
            } catch (_) {}
            try {
                const records = $app.findRecordsByFilter("ibkr_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
                summary.today.ibkr_bars = records ? records.length : 0
            } catch (_) {}
            try {
                const records = $app.findRecordsByFilter("ibkr_targets", "date = {:d} && environment = {:env}", "", 0, 0, { d: times.date, env: environment })
                summary.today.ibkr_targets = records ? records.length : 0
            } catch (_) {}
            try {
                const records = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment })
                summary.today.events = records ? records.length : 0
            } catch (_) {}
        }

        try {
            const recent = $app.findRecordsByFilter("system_events", "environment = {:env}", "-created", 20, 0, { env: environment }) || []
            for (let i = 0; i < recent.length; i++) {
                summary.recent_events.push({
                    id: String(recent[i].getId() || ""),
                    event_type: String(recent[i].get("event_type") || ""),
                    level: String(recent[i].get("level") || ""),
                    source: String(recent[i].get("source") || ""),
                    environment: String(recent[i].get("environment") || environment),
                    title: String(recent[i].get("title") || ""),
                    notified: Boolean(recent[i].get("notified")),
                    us_time: String(recent[i].get("us_time") || ""),
                    created: String(recent[i].get("created") || ""),
                })
            }
        } catch (_) {}
        return c.json(200, JSON.parse(JSON.stringify(summary)))
    } catch (err) {
        return c.json(500, {
            ok: false,
            error: logRouteError("/api/custom/system/summaryz", err),
        })
    }
})

cronAdd("ibkr_compute_runtime", "*/5 4-20 * * 1-5", () => {
    const { runIbkrScheduledAction } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    runIbkrScheduledAction("compute", 60, "[IBKRComputeCron]")
})

cronAdd("ibkr_scan_runtime", "*/5 7-9 * * 1-5", () => {
    const { runIbkrScheduledAction } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    runIbkrScheduledAction("scan", 60, "[IBKRComputeCron]")
})

cronAdd("system_heartbeat", "*/5 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const backtestKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(backtestKeys)
    const compute = loadComputeSnapshot("live")

    if (compute.status !== "running") {
        for (let i = 0; i < environments.length; i++) {
            const environment = environments[i]
            if (!getComputeEnabledForEnvironment(environment, backtestKeys)) continue
            feishuSystem.notifyAlert("ibkr_compute", "IBKR Compute 服务离线", {
                "检查时间": times.us,
                "建议": "检查 systemctl status ibkr-compute",
            }, environment)
            writeSystemEvent("alert", "error", "ibkr_compute", "IBKR Compute 服务离线", { check_time: times.us }, environment, true)
        }
        return
    }

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const bars = $app.findRecordsByFilter("ibkr_bars", "environment = {:env}", "-bar_time_ms", 1, 0, { env: environment }) || []
            if (bars.length > 0) {
                const ageMin = Math.round((Date.now() - Number(bars[0].get("bar_time_ms") || 0)) / 60000)
                if (ageMin > 10) {
                    feishuSystem.notifyWarning("ibkr_compute", "IBKR 数据延迟", {
                        "延迟": ageMin + " 分钟",
                        "最后标的": bars[0].get("symbol") || "",
                        "检查时间": times.us,
                    }, environment)
                }
            }
        } catch (_) {}

        if (new Date().getMinutes() < 5 && (getComputeEnabledForEnvironment(environment, backtestKeys) || getTradingEnabledForEnvironment(environment, backtestKeys))) {
            feishuSystem.notifyHeartbeat("pb", "ok", {
                environment: environment,
                ibkr_compute: "running",
                trading: getTradingEnabledForEnvironment(environment, backtestKeys) ? "true" : "false",
            })
        }
    }
})

cronAdd("system_status_reminder", "0,30 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const runtimeKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(runtimeKeys)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        if (!getComputeEnabledForEnvironment(environment, runtimeKeys) && !getTradingEnabledForEnvironment(environment, runtimeKeys)) {
            continue
        }

        const compute = loadComputeSnapshot(environment)
        const runtime = loadRuntimeSnapshot(environment)
        const freshness = loadFreshness(environment)
        const gaps = loadDataGapSummary(environment, times)
        const overview = loadTodayOverview(environment, times)
        const auth = loadAuthAttentionSummary(environment, runtime)
        const latest5m = freshness["5m"] || {}
        const level = (
            compute.status !== "running"
            || !(runtime.session && runtime.session.authenticated === true)
            || !(runtime.websocket && runtime.websocket.connected === true)
            || gaps.has_issue
        ) ? "warning" : "info"

        const detail = {
            "检查时间": times.us,
            "Compute": compute.status || "unknown",
            "认证": runtime.session && runtime.session.authenticated === true ? "ok" : "pending",
            "WebSocket": runtime.websocket && runtime.websocket.connected === true
                ? ("connected / ready=" + (runtime.websocket.ready === true ? "true" : "false"))
                : "offline",
            "2FA状态": auth.has_request
                ? `${auth.status}${auth.age_min > 0 ? ` / ${auth.age_min}m` : ""}`
                : (runtime.session && runtime.session.authenticated === true ? "ok" : "none"),
            "消息数": String(toNumber(runtime.websocket && runtime.websocket.message_count, 0)),
            "Tick数": String(toNumber(runtime.bar_aggregator && runtime.bar_aggregator.total_ticks, 0)),
            "引擎就绪": `${toNumber(compute.ready_engines, 0)}/${toNumber(compute.total_engines, 0)}`,
            "最新5m": latest5m.last_bar_time_ms
                ? `${latest5m.symbol || "-"} / ${latest5m.age_min || 0}m / ${latest5m.last_bar_time_ms}`
                : "no_data_today",
            "bars缺口": String(gaps.bar_lag_count || 0),
            "指标滞后": String(gaps.indicator_lag_count || 0),
            "序列缺口": String(gaps.sequence_gap_count || 0),
            "今日概况": `bars ${overview.today.bars} / ind ${overview.today.indicators} / sig ${overview.today.signals} / ord ${overview.today.orders}`,
            "实时账户": overview.account.ok
                ? `pos ${overview.account.positions} / open ${overview.account.open_orders} / netliq ${overview.account.net_liquidation.toFixed(2)}`
                : "unavailable",
            "目标池": `${overview.targets} targets / active ${toNumber(runtime.market_universe && runtime.market_universe.active_target_count, 0)}`,
            "交易开关": getTradingEnabledForEnvironment(environment, runtimeKeys) ? "true" : "false",
        }
        if (gaps.latest_bar_us_time) {
            detail["最新bar时间"] = gaps.latest_bar_us_time
        }
        if (auth.mode) {
            detail["验证模式"] = auth.mode
        }
        if (auth.last_result) {
            detail["2FA反馈"] = auth.last_result
        }
        if (auth.last_error) {
            detail["2FA异常"] = auth.last_error
        }
        if (gaps.bar_lag_symbols && gaps.bar_lag_symbols.length > 0) {
            detail["bars异常样本"] = gaps.bar_lag_symbols.slice(0, 8).join(", ")
        }
        if (gaps.indicator_lag_symbols && gaps.indicator_lag_symbols.length > 0) {
            detail["指标异常样本"] = gaps.indicator_lag_symbols.slice(0, 8).join(", ")
        }

        const title = level === "warning" ? "IBKR 系统状态提醒（需关注）" : "IBKR 系统状态提醒"
        const notified = feishuSystem.notifySystemEvent("heartbeat", level, "pb", title, detail, environment)
        writeSystemEvent("heartbeat", level, "pb", title, detail, environment, notified)
    }
})

cronAdd("ibkr_auth_pending_guard", "*/10 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const runtimeKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(runtimeKeys)
    const nowMs = Date.now()

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        if (!getComputeEnabledForEnvironment(environment, runtimeKeys) && !getTradingEnabledForEnvironment(environment, runtimeKeys)) {
            continue
        }

        const runtime = loadRuntimeSnapshot(environment)
        const auth = loadAuthAttentionSummary(environment, runtime)
        const nextState = {
            last_auth_scan_at: times.us,
            last_auth_status: auth.status || "",
            last_auth_age_min: auth.age_min || 0,
        }

        if (!auth.pending_too_long) {
            saveStateData(AUTH_MONITOR_STATE_KEY, environment, times.date, {
                ...nextState,
                last_auth_issue_at: "",
            })
            continue
        }

        const fingerprint = JSON.stringify({
            status: auth.status || "",
            mode: auth.mode || "",
            age_bucket: Math.floor((auth.age_min || 0) / 5),
            challenge: auth.challenge_code ? "yes" : "no",
            response_status: auth.response_status || "",
            gateway_status_code: auth.gateway_status_code || 0,
            runtime_started: auth.runtime_started ? "yes" : "no",
            runtime_authenticated: auth.runtime_authenticated ? "yes" : "no",
        })
        const state = getStateData(AUTH_MONITOR_STATE_KEY, environment, times.date).data || {}
        const lastAlertHash = String(state.last_auth_alert_hash || "")
        const lastAlertMs = toNumber(state.last_auth_alert_ms, 0)
        const shouldNotify = (
            fingerprint !== lastAlertHash
            || lastAlertMs <= 0
            || (nowMs - lastAlertMs) >= AUTH_PENDING_ALERT_COOLDOWN_MS
        )

        if (!shouldNotify) {
            saveStateData(AUTH_MONITOR_STATE_KEY, environment, times.date, nextState)
            continue
        }

        let title = "IBKR Session 长时间未恢复认证"
        if (auth.status === "waiting_confirm") {
            title = "IBKR 2FA 长时间未确认"
        } else if (auth.status === "waiting_response") {
            title = "IBKR 2FA Response 长时间未提交"
        } else if (auth.status === "requested" || auth.status === "triggered") {
            title = "IBKR 2FA 长时间未完成"
        }

        const detail = {
            "检查时间": times.us,
            "2FA状态": auth.status || "requested",
            "持续时间": `${auth.age_min || 0} 分钟`,
            "Runtime已启动": auth.runtime_started ? "yes" : "no",
            "Session认证": auth.runtime_authenticated ? "yes" : "no",
            "Gateway状态码": auth.gateway_status_code ? String(auth.gateway_status_code) : "n/a",
        }
        if (auth.mode) detail["验证模式"] = auth.mode
        if (auth.challenge_code) detail["Challenge"] = auth.challenge_code
        if (auth.response_status) detail["响应状态"] = auth.response_status
        if (auth.triggered_at) detail["触发时间"] = auth.triggered_at
        if (auth.last_result) detail["最近反馈"] = auth.last_result
        if (auth.last_error) detail["最近错误"] = auth.last_error

        const notified = feishuSystem.notifyWarning("ibkr_compute", title, detail, environment)
        writeSystemEvent("alert", "warning", "ibkr_compute", title, detail, environment, notified)
        saveStateData(AUTH_MONITOR_STATE_KEY, environment, times.date, {
            ...nextState,
            last_auth_issue_at: times.us,
            last_auth_alert_ms: nowMs,
            last_auth_alert_hash: fingerprint,
        })
    }
})

cronAdd("system_data_gap_guard", "*/10 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const runtimeKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(runtimeKeys)
    const nowMs = Date.now()

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        if (!getComputeEnabledForEnvironment(environment, runtimeKeys)) {
            continue
        }

        const gaps = loadDataGapSummary(environment, times)
        const nextState = {
            last_gap_scan_at: times.us,
            last_gap_fingerprint: gaps.fingerprint || "",
        }

        if (!gaps.market_activity_detected || !gaps.has_issue) {
            saveStateData(GAP_MONITOR_STATE_KEY, environment, times.date, {
                ...nextState,
                last_gap_issue_at: "",
            })
            continue
        }

        const state = getStateData(GAP_MONITOR_STATE_KEY, environment, times.date).data || {}
        const lastAlertHash = String(state.last_gap_alert_hash || "")
        const lastAlertMs = toNumber(state.last_gap_alert_ms, 0)
        const shouldNotify = (
            gaps.fingerprint !== lastAlertHash
            || lastAlertMs <= 0
            || (nowMs - lastAlertMs) >= GAP_ALERT_COOLDOWN_MS
        )

        if (!shouldNotify) {
            saveStateData(GAP_MONITOR_STATE_KEY, environment, times.date, nextState)
            continue
        }

        const detail = {
            "检查时间": times.us,
            "最新bar时间": gaps.latest_bar_us_time || "unknown",
            "bars缺口数": String(gaps.bar_lag_count || 0),
            "指标滞后数": String(gaps.indicator_lag_count || 0),
            "序列缺口数": String(gaps.sequence_gap_count || 0),
        }
        if (gaps.bar_lag_symbols && gaps.bar_lag_symbols.length > 0) {
            detail["bars异常样本"] = gaps.bar_lag_symbols.slice(0, 10).join(", ")
        }
        if (gaps.indicator_lag_symbols && gaps.indicator_lag_symbols.length > 0) {
            detail["指标异常样本"] = gaps.indicator_lag_symbols.slice(0, 10).join(", ")
        }
        if (gaps.sequence_gap_examples && gaps.sequence_gap_examples.length > 0) {
            const first = gaps.sequence_gap_examples[0]
            detail["序列缺口样本"] = `${first.symbol}: ${first.prev_us_time} -> ${first.next_us_time} (${first.missing_points})`
        }

        const title = "IBKR 数据缺口告警"
        const notified = feishuSystem.notifyWarning("ibkr_compute", title, detail, environment)
        writeSystemEvent("alert", "warning", "ibkr_compute", title, detail, environment, notified)
        saveStateData(GAP_MONITOR_STATE_KEY, environment, times.date, {
            ...nextState,
            last_gap_issue_at: times.us,
            last_gap_alert_ms: nowMs,
            last_gap_alert_hash: gaps.fingerprint || "",
        })
    }
})

cronAdd("ibkr_2fa_hourly_check", "5 4-20 * * 1-5", () => {
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getStatePayload, request2faApproval } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environments = getActiveRuntimeEnvironments(getRuntimeKeys())
    const nowMs = Date.now()

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        if (!getComputeEnabledForEnvironment(environment, getRuntimeKeys()) && !getTradingEnabledForEnvironment(environment, getRuntimeKeys())) {
            continue
        }

        try {
            const runtimeStatus = fetchComputeJson("/ibkr/status", 8, environment)
            const runtimeStarted = Boolean(
                runtimeStatus.starting
                || (runtimeStatus.session && runtimeStatus.session.running)
                || (runtimeStatus.websocket && runtimeStatus.websocket.running)
                || (runtimeStatus.order_tracker && runtimeStatus.order_tracker.running)
            )
            const runtimeAuthenticated = Boolean(runtimeStatus.session && runtimeStatus.session.authenticated)
            const gatewayReachable = Boolean(runtimeStatus.gateway && (runtimeStatus.gateway.running || runtimeStatus.gateway.reachable))
            const gatewayStatusCode = Number(runtimeStatus.gateway && runtimeStatus.gateway.status_code || 0) || 0
            const needsAuthAttention = gatewayReachable && (!runtimeAuthenticated || gatewayStatusCode === 401 || !runtimeStarted)
            if (!needsAuthAttention) {
                continue
            }

            const statePayload = getStatePayload(environment)
            const state = statePayload.data || {}
            const status = String(state.status || "").trim().toLowerCase()
            const lastPushMs = Number(state.last_request_push_ms || 0) || 0

            if (lastPushMs > 0 && (nowMs - lastPushMs) < 55 * 60 * 1000) {
                continue
            }

            request2faApproval({
                environment: environment,
                reason: String(state.reason || "scheduled_2fa_check"),
                source: "pb_scheduler",
                message: "检测到 IBKR 2FA 仍未恢复，已按小时发送提醒，请在方便时点击卡片继续验证。",
                detail: {
                    "当前状态": status || "requested",
                    "Runtime已启动": runtimeStarted ? "yes" : "no",
                    "Session认证": runtimeAuthenticated ? "yes" : "no",
                    "Gateway状态码": gatewayStatusCode ? String(gatewayStatusCode) : "n/a",
                    "最近结果": String(state.last_result || ""),
                    "最近错误": String(state.last_error || ""),
                },
                forceReset: false,
                forceNew: false,
            })
        } catch (err) {
            console.log(`[IBKR2FAHourly] ${environment}: ${err.message || err}`)
        }
    }
})

cronAdd("system_daily_report", "5 20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const times = getTimeStrings()
    const todayStart = times.date + " 00:00:00"
    const environments = getActiveRuntimeEnvironments(getRuntimeKeys())

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        const report = {
            "日期": times.date,
            "信号数": "0",
            "订单数": "0",
            "IBKR Bars": "0",
            "Targets": "0",
            "系统事件": "0",
            "错误事件": "0",
        }

        try {
            const rows = $app.findRecordsByFilter("ibkr_signals", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["信号数"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("orders", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["订单数"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("ibkr_bars", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["IBKR Bars"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("ibkr_targets", "date = {:d} && environment = {:env}", "", 0, 0, { d: times.date, env: environment }) || []
            report["Targets"] = String(rows.length)
        } catch (_) {}
        try {
            const rows = $app.findRecordsByFilter("system_events", "created >= {:t} && environment = {:env}", "", 0, 0, { t: todayStart, env: environment }) || []
            report["系统事件"] = String(rows.length)
            let errorCount = 0
            for (let j = 0; j < rows.length; j++) {
                if (rows[j].get("level") === "error") errorCount += 1
            }
            report["错误事件"] = String(errorCount)
        } catch (_) {}

        feishuSystem.notifyDailyReport(report, environment)
    }
})

console.log("[IBKRSystemMonitor] Hook 文件加载完成")
