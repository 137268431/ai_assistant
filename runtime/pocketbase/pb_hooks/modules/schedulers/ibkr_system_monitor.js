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
const AUTH_EDGE_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_EDGE_MONITOR_STATE_KEY = "system_auth_edge_monitor"
const AUTH_PENDING_ALERT_TRIGGER_MS = 15 * 60 * 1000
const AUTH_PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_MONITOR_STATE_KEY = "system_auth_monitor"
const MONITOR_ALERT_COOLDOWN_MS = 15 * 60 * 1000
const MONITOR_ALERT_STATE_KEY = "system_monitor_alert"
const MONITOR_ALERT_FLAG_CODES = {
    monitor_endpoint_unavailable: true,
    gateway_offline: true,
    session_unauthenticated: true,
    websocket_not_ready: true,
    subscription_utilization_high: true,
    subscription_utilization_critical: true,
    market_data_silent: true,
    market_data_silent_critical: true,
    data_freshness_delayed: true,
    data_freshness_offline: true,
    host_memory_high: true,
    host_memory_critical: true,
    host_disk_high: true,
    host_disk_critical: true,
    host_load_high: true,
    host_load_critical: true,
    host_cpu_high: true,
    host_cpu_critical: true,
    pb_disk_high: true,
    pb_disk_critical: true,
}

function getRuntimeKeys() {
    return ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
}

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function parseStateValue(raw) {
    if (!raw) return {}
    if (typeof raw === "object") return raw
    try {
        const parsed = JSON.parse(String(raw || ""))
        return parsed && typeof parsed === "object" ? parsed : {}
    } catch (_) {
        return {}
    }
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
    const raw = typeof record.getString === "function"
        ? (record.getString("data") || "")
        : record.get("data")
    const data = parseStateValue(raw)
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
    record.set("data", JSON.stringify(next))
    $app.save(record)
    return next
}

function parseHttpJson(resp) {
    if (!resp) return {}
    const raw = typeof resp === "string"
        ? resp
        : (typeof resp.raw === "string" ? resp.raw : String(resp.raw || ""))
    return raw ? JSON.parse(raw) : {}
}

function runHistoryRetentionCleanup(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRHistoryRetention]"
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)

    let executedCount = 0
    let totalDeleted = 0
    let totalErrors = 0

    for (const environment of getRuntimeEnvironments()) {
        const cronState = getPbCronToggleState(cronId, environment)
        if (!cronState.effective_enabled) {
            console.log(`${prefix} ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }

        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/retention/cleanup`
        try {
            const resp = $http.send({
                url: upstream,
                method: "POST",
                timeout: 180,
                body: JSON.stringify({ environment: environment, source: "pb_cron" }),
                headers: { "Content-Type": "application/json" },
            })
            const payload = parseHttpJson(resp)
            if (Number(resp.statusCode || 0) !== 200) {
                totalErrors += 1
                console.log(`${prefix} ${environment}: status=${resp.statusCode}, upstream=${upstream}, raw=${resp.raw}`)
                continue
            }

            const environments = Array.isArray(payload.environments) ? payload.environments : []
            const envPayload = environments.find((item) => String(item && item.environment || "") === environment) || {}
            const deleted = Number(envPayload.total_deleted || payload.total_deleted || 0) || 0
            const errors = Number(envPayload.total_errors || 0) || 0
            const retentionDays = Number(envPayload.retention_days || 0) || 0
            totalDeleted += deleted
            totalErrors += errors
            executedCount += 1

            console.log(
                `${prefix} ${environment}: upstream=${upstream}, ok=${payload.ok !== false}, retention_days=${retentionDays || "-"}, deleted=${deleted}, errors=${errors}`
            )
        } catch (err) {
            totalErrors += 1
            console.log(`${prefix} ${environment}: upstream=${upstream}, error=${err.message || err}`)
        }
    }

    if (executedCount > 0 || totalDeleted > 0 || totalErrors > 0) {
        console.log(`${prefix} summary: executed=${executedCount}, deleted=${totalDeleted}, errors=${totalErrors}`)
    }
}

const runHistoryRetentionCleanupCron = (logPrefix, cronId) => runHistoryRetentionCleanup(logPrefix, cronId)

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
    const { fetchComputeJsonWithFallback } = require(`${__hooks}/lib/compute_http.js`)
    const result = fetchComputeJsonWithFallback(path, timeoutSeconds, environment)
    const payload = result && result.payload && typeof result.payload === "object"
        ? result.payload
        : {}
    if (result && result.upstream && !Array.isArray(payload) && !payload.proxy_upstream) {
        payload.proxy_upstream = result.upstream
    }
    return payload
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
    const { getStatePayload, normalizeStateWithRuntime } = require(`${__hooks}/lib/feishu_2fa.js`)
    const statePayload = getStatePayload(environment)
    const state = normalizeStateWithRuntime(statePayload.data || {}, runtimeStatus || {})
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
        cycle_id: String(state.cycle_id || ""),
        recovery_phase: String(state.recovery_phase || ""),
        age_min: ageMin,
        requested_at: String(state.requested_at || ""),
        triggered_at: String(state.triggered_at || ""),
        mode: String(state.mode || ""),
        challenge_code: String(state.challenge_code || ""),
        response_status: String(state.response_status || ""),
        last_result: String(state.last_result || ""),
        last_error: String(state.last_error || ""),
        reason: String(state.reason || ""),
        message: String(state.message || ""),
        page_url: String(state.page_url || ""),
        runtime_started: runtimeStarted,
        runtime_authenticated: runtimeAuthenticated,
        gateway_reachable: gatewayReachable,
        gateway_status_code: gatewayStatusCode,
    }
}

function isAuthActiveStatus(status) {
    return ["requested", "triggered", "waiting_confirm", "waiting_response"].indexOf(String(status || "").trim().toLowerCase()) !== -1
}

function buildAuthImmediateIssue(auth) {
    if (!auth) return null
    const status = String(auth.status || "").trim().toLowerCase()
    const gatewayStatusCode = toNumber(auth.gateway_status_code, 0)
    const gatewayReachable = auth.gateway_reachable === true
    const runtimeAuthenticated = auth.runtime_authenticated === true
    const runtimeStarted = auth.runtime_started === true
    const hasRequest = auth.has_request === true || auth.active === true
    const active = auth.active === true

    if (!gatewayReachable && !active && !hasRequest) {
        return null
    }

    if (hasRequest && status === "waiting_response") {
        return {
            kind: "waiting_response",
            title: "IBKR 2FA 已切到 Challenge/Response",
            summary: "本轮 2FA 已不再是手机确认。不要再点旧的确认消息；如不想提交 Response Code，请去 Runtime 页面执行“全量清空并重新验证”。",
        }
    }

    if (hasRequest && status === "waiting_confirm") {
        return {
            kind: "waiting_confirm",
            title: "IBKR 2FA 已触发，等待确认",
            summary: "本轮 2FA 当前仍是手机确认。只需要在 IBKR App 点一次确认；如果手机没有反应，不要反复点旧消息，先去 Runtime 页面确认当前状态是否已变成 Challenge/Response。",
        }
    }

    if (active && (status === "requested" || status === "triggered")) {
        return {
            kind: "requested",
            title: "IBKR 2FA 已请求，待处理",
            summary: "检测到系统已请求 2FA，当前会话尚未恢复认证，请立即处理飞书 2FA 卡片。",
        }
    }

    if (gatewayStatusCode === 401 && !runtimeAuthenticated) {
        return {
            kind: "session_expired",
            title: "IBKR Session 已失效，需重新触发 2FA",
            summary: "检测到 Gateway Session 已失效（401），运行态未认证，需要立即重新触发 2FA。",
        }
    }

    if (runtimeStarted && !runtimeAuthenticated) {
        return {
            kind: "runtime_unauthenticated",
            title: "IBKR Runtime 未认证",
            summary: "检测到运行态未认证，实时链路可能不可用，请立即检查 Gateway 与 2FA 状态。",
        }
    }

    return null
}

function buildAuthImmediateFingerprint(auth, issue) {
    return JSON.stringify({
        issue_kind: issue && issue.kind || "",
        cycle_id: auth && auth.cycle_id || "",
        status: auth && auth.status || "",
        requested_at: auth && auth.requested_at || "",
        triggered_at: auth && auth.triggered_at || "",
        gateway_status_code: auth && auth.gateway_status_code || 0,
        runtime_started: auth && auth.runtime_started ? "yes" : "no",
        runtime_authenticated: auth && auth.runtime_authenticated ? "yes" : "no",
        challenge_code: auth && auth.challenge_code || "",
        response_status: auth && auth.response_status || "",
    })
}

function shouldNotifyAuthImmediateAlert(previous, auth, issue, fingerprint, nowMs) {
    const prev = previous || {}
    const prevGatewayStatusCode = toNumber(prev.last_gateway_status_code, 0)
    const prevRuntimeAuthenticated = String(prev.last_runtime_authenticated || "") === "yes"
    const prevActive = String(prev.last_auth_active || "") === "yes"
    const prevStatus = String(prev.last_auth_status || "").trim().toLowerCase()
    const prevRequestedAt = String(prev.last_requested_at || "")
    const prevTriggeredAt = String(prev.last_triggered_at || "")
    const prevIssueKind = String(prev.last_auth_issue_kind || "")
    const prevCycleId = String(prev.last_auth_cycle_id || "")
    const lastAlertHash = String(prev.last_auth_edge_alert_hash || "")
    const lastAlertMs = toNumber(prev.last_auth_edge_alert_ms, 0)
    const currentGatewayStatusCode = toNumber(auth && auth.gateway_status_code, 0)
    const currentRuntimeAuthenticated = auth && auth.runtime_authenticated === true
    const currentActive = auth && auth.active === true
    const currentStatus = String(auth && auth.status || "").trim().toLowerCase()
    const currentRequestedAt = String(auth && auth.requested_at || "")
    const currentTriggeredAt = String(auth && auth.triggered_at || "")
    const issueKind = String(issue && issue.kind || "")
    const currentCycleId = String(auth && auth.cycle_id || "")

    if (
        (issueKind === "waiting_confirm" || issueKind === "waiting_response")
        && !!currentCycleId
        && issueKind === prevIssueKind
        && currentCycleId === prevCycleId
    ) {
        return false
    }

    const edgeDetected = (
        !String(prev.last_auth_scan_at || "")
        || (currentGatewayStatusCode === 401 && prevGatewayStatusCode !== 401)
        || (!currentRuntimeAuthenticated && prevRuntimeAuthenticated)
        || (currentActive && !prevActive)
        || (currentActive && !!currentRequestedAt && currentRequestedAt !== prevRequestedAt)
        || (currentActive && !!currentTriggeredAt && currentTriggeredAt !== prevTriggeredAt)
        || (!!issueKind && issueKind !== prevIssueKind)
        || (!!currentStatus && currentStatus !== prevStatus && currentActive)
    )

    return (
        edgeDetected
        || fingerprint !== lastAlertHash
        || lastAlertMs <= 0
        || (nowMs - lastAlertMs) >= AUTH_EDGE_ALERT_COOLDOWN_MS
    )
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
        const indicators = loadRecentRowsBySymbol("ibkr_indicators", environment, "5", times.todayStart, 1200)
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

function loadRecentSystemEvents(environment, limit) {
    const items = []
    try {
        const recent = $app.findRecordsByFilter("system_events", "environment = {:env}", "-created", Math.max(1, Number(limit) || 20), 0, { env: environment }) || []
        for (let i = 0; i < recent.length; i++) {
            items.push({
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
    return items
}

function buildMonitorConfigMap(records) {
    const selectedKeys = {
        ibkr_target_subscription_limit: true,
        ibkr_history_request_spacing: true,
        ibkr_target_refresh_sec: true,
        ibkr_watchlist_backfill_interval_min: true,
    }
    const config = {}
    for (let i = 0; i < (records || []).length; i++) {
        const key = String(records[i].get("key") || "")
        if (!selectedKeys[key]) continue
        config[key] = String(records[i].get("value") || "")
    }
    return config
}

function runSystemMonitorAlertGuard(logPrefix) {
    // Keep the scheduler hook thin; the real monitor alert behavior lives in lib/system_monitor_alert_guard.js.
    return require(`${__hooks}/lib/system_monitor_alert_guard.js`).runSystemMonitorAlertGuard(logPrefix)
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
    const messageId = String(d.message_id || "").trim()
    const title = labelTitleWithEnvironment(rawTitle, environment)
    const detail = addEnvironmentToDetail(rawDetail, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    let notifyResult = {
        success: false,
        message_id: messageId,
        updated: false,
        skipped: false,
        suppressed: false,
        error: "",
    }
    if (level === "error" || level === "warning" || eventType === "status_change" || eventType === "daily_report") {
        notifyResult = feishuSystem.notifySystemEventDetailed(
            eventType,
            level,
            source,
            title,
            detail,
            environment,
            { message_id: messageId },
        )
    }

    const notified = !!(notifyResult && notifyResult.success && !notifyResult.suppressed)
    writeSystemEvent(eventType, level, source, rawTitle, rawDetail, environment, notified)
    return c.json(200, {
        ok: true,
        notified: notified,
        message_id: String((notifyResult && notifyResult.message_id) || messageId || ""),
        updated: !!(notifyResult && notifyResult.updated),
        skipped: !!(notifyResult && notifyResult.skipped),
        suppressed: !!(notifyResult && notifyResult.suppressed),
        error: String((notifyResult && notifyResult.error) || ""),
    })
})

routerAdd("GET", "/api/custom/system/cronz", (c) => {
    try {
        const { getPbCronDefinitions } = require(`${__hooks}/lib/pb_cron_registry.js`)
        return c.json(200, {
            ok: true,
            items: getPbCronDefinitions(),
        })
    } catch (err) {
        return c.json(500, {
            ok: false,
            error: logRouteError("/api/custom/system/cronz", err),
        })
    }
})

routerAdd("GET", "/api/custom/system/healthz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const { fetchComputeJsonWithFallback } = require(`${__hooks}/lib/compute_http.js`)
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        const fetchResult = fetchComputeJsonWithFallback("/ibkr/monitor", 10, environment)
        const monitorPayload = fetchResult && fetchResult.payload && typeof fetchResult.payload === "object"
            ? fetchResult.payload
            : {}
        const payload = {
            ok: monitorPayload.ok !== false,
            environment: environment,
            status: String(monitorPayload.status || (monitorPayload.ok === false ? "offline" : "ok")).trim().toLowerCase() || "ok",
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_system_monitor.pb.js",
            proxy_route: "/api/custom/system/healthz",
            proxy_upstream: String(fetchResult && fetchResult.upstream || ""),
        }
        return c.json(200, payload)
    } catch (err) {
        return c.json(200, {
            ok: false,
            status: "offline",
            error: logRouteError("/api/custom/system/healthz", err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_system_monitor.pb.js",
            proxy_route: "/api/custom/system/healthz",
        })
    }
})

routerAdd("GET", "/api/custom/system/summaryz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, getConfigValue, getIbkrComputeInternalUrl, listEffectiveConfigRecords, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
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
            const computeBaseUrl = getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")
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
            config: {},
            today: { ibkr_signals: 0, ibkr_indicators: 0, orders: 0, ibkr_bars: 0, ibkr_targets: 0, events: 0 },
            ibkr_compute: computeSummary,
            recent_events: [],
            data_freshness: dataFreshness,
            lite_mode: liteMode,
        }

        try {
            const configs = listEffectiveConfigRecords(environment)
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
        } catch (_) {}

        // Keep summaryz lightweight for UI callers. Heavy per-day counts are fetched
        // directly by pages via paginated collection APIs when needed.

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

routerAdd("GET", "/api/custom/system/monitorz", (c) => {
    try {
        const { normalizeRuntimeEnvironment, listEffectiveConfigRecords, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const { fetchComputeJsonWithFallback } = require(`${__hooks}/lib/compute_http.js`)
        const pocketbaseDiskMonitor = require(`${__hooks}/lib/pocketbase_disk_monitor.js`)
        const environment = normalizeRuntimeEnvironment(c.request.url.query().get("environment") || "", LIVE_ENVIRONMENT)
        const loadMonitorConfig = () => {
            const selectedKeys = {
                ibkr_target_subscription_limit: true,
                ibkr_history_request_spacing: true,
                ibkr_target_refresh_sec: true,
                ibkr_watchlist_backfill_interval_min: true,
            }
            const config = {}
            try {
                const records = listEffectiveConfigRecords(environment) || []
                for (let i = 0; i < records.length; i++) {
                    const key = String(records[i].get("key") || "")
                    if (!selectedKeys[key]) continue
                    config[key] = String(records[i].get("value") || "")
                }
            } catch (_) {}
            return config
        }
        const loadMonitorRecentEvents = (limit) => {
            const items = []
            try {
                const recent = $app.findRecordsByFilter("system_events", "environment = {:env}", "-created", Math.max(1, Number(limit) || 20), 0, { env: environment }) || []
                for (let i = 0; i < recent.length; i++) {
                    items.push({
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
            return items
        }
        const fetchResult = fetchComputeJsonWithFallback("/ibkr/monitor", 10, environment)
        const monitorPayload = fetchResult && fetchResult.payload && typeof fetchResult.payload === "object"
            ? fetchResult.payload
            : {}

        const config = loadMonitorConfig()

        const response = monitorPayload && typeof monitorPayload === "object" && !Array.isArray(monitorPayload)
            ? { ...monitorPayload }
            : {}

        const actualRuntimeEnvironment = String(
            response.environment
            || ((response.runtime || {}).environment)
            || environment
        ).trim().toLowerCase() || environment

        response.requested_environment = environment
        response.actual_runtime_environment = actualRuntimeEnvironment
        response.runtime_environment_mismatch = actualRuntimeEnvironment !== environment
        response.config = config
        response.recent_events = loadMonitorRecentEvents(20)
        response.proxy_source = "pocketbase_ibkr_hook"
        response.proxy_hook = "ibkr_system_monitor.pb.js"
        response.proxy_route = "/api/custom/system/monitorz"
        response.proxy_upstream = String(fetchResult && fetchResult.upstream || "")
        response.proxy_upstream_attempts = Array.isArray(fetchResult && fetchResult.attempts)
            ? fetchResult.attempts
            : []
        response.ok = response.ok !== false
        response.status = String(response.status || (response.ok === false ? "offline" : "ok")).trim().toLowerCase() || "ok"
        response.flags = Array.isArray(response.flags) ? response.flags : []

        pocketbaseDiskMonitor.enrichMonitorPayloadWithPocketBaseDisk(response, false)

        return c.html(200, JSON.stringify(response))
    } catch (err) {
        return c.html(500, JSON.stringify({
            ok: false,
            status: "offline",
            error: logRouteError("/api/custom/system/monitorz", err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_system_monitor.pb.js",
            proxy_route: "/api/custom/system/monitorz",
        }))
    }
})

cronAdd("ibkr_compute_runtime", "*/5 4-20 * * 1-5", () => {
    const { runIbkrScheduledAction } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    try {
        runIbkrScheduledAction("compute", 60, "[IBKRComputeCron]", "ibkr_compute_runtime")
    } catch (err) {
        console.log(`[IBKRComputeCron] compute dispatch error: ${err.message || err}`)
    }
    try {
        const systemNotify = require(`${__hooks}/lib/system_notify_scheduler.js`)
        const monitorAlertGuard = require(`${__hooks}/lib/system_monitor_alert_guard.js`)
        systemNotify.runSystemHeartbeatTick("[IBKRComputeCron]", "ibkr_compute_runtime")
        monitorAlertGuard.runSystemMonitorAlertGuard("[IBKRMonitorAlert]")
        const minute = new Date().getMinutes()
        if (minute === 0 || minute === 30) {
            systemNotify.runSystemStatusReminderTick("[IBKRComputeCron]", "ibkr_compute_runtime")
        }
    } catch (err) {
        console.log(`[IBKRComputeCron] system notify error: ${err.message || err}`)
    }
})

cronAdd("ibkr_scan_runtime", "*/5 7-9 * * 1-5", () => {
    const { runIbkrScheduledAction } = require(`${__hooks}/lib/ibkr_scheduler.js`)
    runIbkrScheduledAction("scan", 60, "[IBKRComputeCron]", "ibkr_scan_runtime")
    try {
        require(`${__hooks}/lib/system_notify_scheduler.js`).runDailyScanSummaryTick("[IBKRScanSummary]", "ibkr_scan_runtime")
    } catch (err) {
        console.log(`[IBKRScanSummary] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_history_retention", "10 * * * *", () => {
    try {
        runHistoryRetentionCleanupCron("[IBKRHistoryRetention]", "ibkr_history_retention")
    } catch (err) {
        console.log(`[IBKRHistoryRetention] fatal error: ${err.message || err}`)
    }
})

// System heartbeat / status reminder piggyback on ibkr_compute_runtime via lib/system_notify_scheduler.js

cronAdd("system_market_open_reminder", "*/5 * * * *", () => {
    try {
        require(`${__hooks}/lib/system_notify_scheduler.js`).runDailyOpenReminderTick("[IBKROpenReminder]", "system_market_open_reminder")
    } catch (err) {
        console.log(`[IBKROpenReminder] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_auth_edge_guard", "* 4-20 * * 1-5", () => {
    try {
        require(`${__hooks}/lib/system_auth_edge_guard.js`).runIbkrAuthEdgeGuard()
    } catch (err) {
        console.log(`[IBKRAuthEdgeGuard] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_auth_pending_guard", "*/10 4-20 * * 1-5", () => {
    try {
        require(`${__hooks}/lib/system_auth_edge_guard.js`).runIbkrAuthPendingGuard()
    } catch (err) {
        console.log(`[IBKRAuthPendingGuard] fatal error: ${err.message || err}`)
    }
})

cronAdd("system_data_gap_guard", "*/10 4-20 * * 1-5", () => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const times = getTimeStrings()
    const runtimeKeys = getRuntimeKeys()
    const environments = getActiveRuntimeEnvironments(runtimeKeys)
    const nowMs = Date.now()

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        const cronState = getPbCronToggleState("system_data_gap_guard", environment)
        if (!cronState.effective_enabled) {
            console.log(`[SystemDataGapGuard] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }
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
    try {
        require(`${__hooks}/lib/system_auth_edge_guard.js`).runIbkr2faHourlyCheck()
    } catch (err) {
        console.log(`[IBKR2FAHourly] fatal error: ${err.message || err}`)
    }
})

cronAdd("ibkr_weekly_reauth_reminder", "20 1 * * 1", () => {
    try {
        require(`${__hooks}/lib/system_auth_edge_guard.js`).runIbkrWeeklyReauthReminder()
    } catch (err) {
        console.log(`[IBKRWeekly2FA] fatal error: ${err.message || err}`)
    }
})

cronAdd("system_daily_report", "*/5 * * * *", () => {
    try {
        require(`${__hooks}/lib/system_notify_scheduler.js`).runDailyCloseSummaryTick("[SystemDailyReport]", "system_daily_report")
    } catch (err) {
        console.log(`[SystemDailyReport] fatal error: ${err.message || err}`)
    }
})

console.log("[IBKRSystemMonitor] Hook 文件加载完成")
