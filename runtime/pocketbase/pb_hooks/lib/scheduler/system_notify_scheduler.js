/**
 * system_notify_scheduler.js
 * 健康检查通知 / 状态提醒的共享执行逻辑
 */

const HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
const DAILY_REMINDER_STATE_KEY = "system_notify_daily"
const HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const BAR_STALE_WARN_MIN = 10
const INDICATOR_STALE_WARN_MIN = 10
const INDICATOR_MISSING_GRACE_MS = 90 * 1000
const COMPUTE_STARTUP_GRACE_MS = 3 * 60 * 1000
const RUNTIME_KEYS = ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
const SCAN_SUMMARY_STATE_KEY = "system_notify_scan_summary"
const SCAN_SUMMARY_HOUR = 5
const SCAN_SUMMARY_MINUTE = 55
const MARKET_OPEN_REMINDER_HOUR = 9
const MARKET_OPEN_REMINDER_MINUTE = 20
const MARKET_CLOSE_REMINDER_HOUR = 16
const MARKET_CLOSE_REMINDER_MINUTE = 5
const HOOKS_ROOT = typeof __hooks !== "undefined"
    ? __hooks
    : String(__dirname || "").replace(/[\\/]lib[\\/]scheduler$/, "")
const publicUrls = require(`${HOOKS_ROOT}/lib/feishu/public_urls.js`)
const usEasternTime = require(`${HOOKS_ROOT}/lib/runtime/us_eastern_time.js`)

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function normalizeSymbolList(values, limit = 12) {
    if (!Array.isArray(values)) return []
    const normalized = []
    const seen = {}
    for (let i = 0; i < values.length; i++) {
        const symbol = String(values[i] || "").trim().toUpperCase()
        if (!symbol || seen[symbol]) continue
        seen[symbol] = true
        normalized.push(symbol)
        if (normalized.length >= limit) break
    }
    return normalized
}

function formatPendingSymbolsPreview(symbols, limit = 4) {
    const items = normalizeSymbolList(symbols, Math.max(1, limit))
    if (!items.length) return ""
    const clipped = items.slice(0, Math.max(1, limit))
    const more = items.length - clipped.length
    return more > 0 ? `${clipped.join(",")} +${more}` : clipped.join(",")
}

function formatPendingDetailPreview(details, limit = 4) {
    if (!Array.isArray(details) || !details.length) return ""
    const clipped = details.slice(0, Math.max(1, limit)).map((item) => {
        const symbol = String(item && item.symbol || "").trim().toUpperCase()
        const missingTimes = Array.isArray(item && item.missing_us_times) ? item.missing_us_times : []
        const firstMissing = missingTimes.length ? String(missingTimes[0] || "").trim().slice(11, 16) : ""
        if (symbol && firstMissing) {
            return `${symbol}(${firstMissing})`
        }
        return symbol
    }).filter(Boolean)
    if (!clipped.length) return ""
    const more = details.length - clipped.length
    return more > 0 ? `${clipped.join(",")} +${more}` : clipped.join(",")
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

function parseUsTimeMs(value) {
    return usEasternTime.parseUsEasternTimeMs(value)
}

function parseTimeMs(value) {
    const text = String(value || "").trim()
    if (!text) return 0
    const ms = Date.parse(text)
    return Number.isFinite(ms) ? ms : 0
}

function asObject(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
        return value
    }
    if (typeof value === "string") {
        try {
            const parsed = JSON.parse(value)
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                return parsed
            }
        } catch (_) {}
    }
    return {}
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

function countCollectionRows(collectionName, filterStr, params) {
    try {
        const rows = $app.findRecordsByFilter(collectionName, filterStr, "", 0, 0, params || {}) || []
        return rows.length
    } catch (_) {
        return 0
    }
}

function loadLatestBar(environment) {
    try {
        const rows = $app.findRecordsByFilter(
            "ibkr_bars",
            "environment = {:env} && interval = '5m'",
            "-bar_time_ms",
            1,
            0,
            { env: environment }
        ) || []
        if (!rows.length) return { bar_time_ms: 0, symbol: "", us_time: "", age_min: 0 }
        const row = rows[0]
        const barTimeMs = toNumber(row.get("bar_time_ms"), 0)
        const created = String(row.get("created") || "")
        return {
            bar_time_ms: barTimeMs,
            symbol: String(row.get("symbol") || ""),
            us_time: String(row.get("us_time") || ""),
            created: created,
            created_ms: parseTimeMs(created),
            age_min: barTimeMs > 0 ? Math.max(0, Math.round((Date.now() - barTimeMs) / 60000)) : 0,
        }
    } catch (_) {
        return { bar_time_ms: 0, symbol: "", us_time: "", age_min: 0 }
    }
}

function loadLatestIndicator(environment) {
    try {
        const rows = $app.findRecordsByFilter(
            "ibkr_indicators",
            "environment = {:env} && interval = '5'",
            "-bar_time_ms",
            1,
            0,
            { env: environment }
        ) || []
        if (!rows.length) return { bar_time_ms: 0, symbol: "", us_time: "" }
        const row = rows[0]
        const created = String(row.get("created") || "")
        return {
            bar_time_ms: toNumber(row.get("bar_time_ms"), 0),
            symbol: String(row.get("symbol") || ""),
            us_time: String(row.get("us_time") || ""),
            created: created,
            created_ms: parseTimeMs(created),
        }
    } catch (_) {
        return { bar_time_ms: 0, symbol: "", us_time: "" }
    }
}

function loadTodayCounts(environment, times) {
    return {
        bars: countCollectionRows("ibkr_bars", "created >= {:t} && environment = {:env}", { t: times.todayStart, env: environment }),
        indicators: countCollectionRows("ibkr_indicators", "created >= {:t} && environment = {:env}", { t: times.todayStart, env: environment }),
        signals: countCollectionRows("ibkr_signals", "created >= {:t} && environment = {:env}", { t: times.todayStart, env: environment }),
        orders: countCollectionRows("orders", "created >= {:t} && environment = {:env}", { t: times.todayStart, env: environment }),
        targets: countCollectionRows(
            "ibkr_targets",
            'date = {:d} && environment = {:env} && (status = "candidate" || status = "active")',
            { d: times.date, env: environment }
        ),
    }
}

function loadDailyScanState(environment) {
    const record = getStateRecord("ibkr_daily_scan_state", environment, "global")
    if (!record) return {}
    const raw = typeof record.getString === "function"
        ? (record.getString("data") || "")
        : record.get("data")
    const payload = asObject(raw)
    return {
        ...payload,
        result: asObject(payload.result),
    }
}

function loadTodayEventCounts(environment, times) {
    const rows = (() => {
        try {
            return $app.findRecordsByFilter(
                "system_events",
                "created >= {:t} && environment = {:env}",
                "",
                0,
                0,
                { t: times.todayStart, env: environment }
            ) || []
        } catch (_) {
            return []
        }
    })()

    let errorCount = 0
    for (let i = 0; i < rows.length; i++) {
        if (String(rows[i].get("level") || "").trim().toLowerCase() === "error") {
            errorCount += 1
        }
    }

    return {
        events: rows.length,
        error_events: errorCount,
    }
}

function load2faSummary(environment, runtimeStatus) {
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
    const startedMs = Math.max(parseUsTimeMs(state.triggered_at), parseUsTimeMs(state.requested_at))
    const ageMin = startedMs > 0 ? Math.max(0, Math.round((Date.now() - startedMs) / 60000)) : 0
    const pending = hasRequest && ["requested", "triggered", "waiting_confirm", "waiting_response"].indexOf(status) !== -1
    const runtimeAuthenticated = Boolean(runtimeStatus.session && runtimeStatus.session.authenticated === true)
    const label = pending
        ? `${status || "requested"}${ageMin > 0 ? ` / ${ageMin}m` : ""}`
        : (runtimeAuthenticated ? "ok" : (status || "none"))
    return {
        label: label,
        mode: String(state.mode || ""),
        last_result: String(state.last_result || ""),
        last_error: String(state.last_error || ""),
    }
}

function listNotifyEnvironments(cronId) {
    const envUtils = require(`${__hooks}/lib/environment.js`)
    const runtimeModes = require(`${__hooks}/lib/runtime_modes.js`)
    const { isPbCronEnabled } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const environments = runtimeModes.getActiveRuntimeEnvironments(RUNTIME_KEYS)
    return environments.filter((environment) => {
        if (cronId && !isPbCronEnabled(cronId, environment)) {
            return false
        }
        const schedulerEnabled = runtimeModes.isEnabledConfigValue(envUtils.getConfigValue("pb_scheduler_enabled", "TRUE", environment))
        if (!schedulerEnabled) return false
        return runtimeModes.getComputeEnabledForEnvironment(environment, RUNTIME_KEYS)
            || runtimeModes.getTradingEnabledForEnvironment(environment, RUNTIME_KEYS)
    })
}

function buildStatusSnapshot(environment, times) {
    const runtimeModes = require(`${__hooks}/lib/runtime_modes.js`)
    const health = fetchComputeJson("/health", 5, environment)
    const status = fetchComputeJson("/status", 8, environment)
    const runtime = fetchComputeJson("/ibkr/status", 8, environment)
    const account = fetchComputeJson("/ibkr/account", 10, environment)
    const latestBar = loadLatestBar(environment)
    const latestIndicator = loadLatestIndicator(environment)
    const today = loadTodayCounts(environment, times)
    const auth = load2faSummary(environment, runtime)
    const indicatorLagMin = latestBar.bar_time_ms > 0
        ? (
            latestIndicator.bar_time_ms > 0
                ? Math.max(0, Math.round((latestBar.bar_time_ms - latestIndicator.bar_time_ms) / 60000))
                : null
        )
        : 0
    const websocketConnected = Boolean(runtime.websocket && runtime.websocket.connected === true)
    const websocketReady = Boolean(runtime.websocket && runtime.websocket.ready === true)
    const warmup = runtime.warmup && typeof runtime.warmup === "object" ? runtime.warmup : {}
    const runtimeDailyScan = runtime.daily_scan && typeof runtime.daily_scan === "object" ? runtime.daily_scan : {}
    const stateDailyScan = loadDailyScanState(environment)
    const dailyScan = Object.keys(stateDailyScan).length ? stateDailyScan : runtimeDailyScan
    const warmupPendingSymbols = Array.isArray(warmup.pending_symbols) ? warmup.pending_symbols : []
    const barBucket = buildBarBucketSnapshot(runtime)

    return {
        environment: environment,
        compute: {
            status: String(health.status || status.status || "unknown"),
            error: String(health.error || status.error || ""),
            ready_engines: toNumber(status.ready_engines, 0),
            total_engines: toNumber(status.total_engines, 0),
            uptime_s: toNumber(health.uptime_s, 0),
        },
        runtime: {
            starting: Boolean(runtime.starting === true),
            warmup_phase: String(runtime.warmup && runtime.warmup.phase || ""),
            warmup_started_at: String(warmup.started_at || ""),
            warmup_finished_at: String(warmup.finished_at || ""),
            warmup_last_success_at: String(warmup.last_success_at || warmup.finished_at || ""),
            warmup_pending_symbols_total: toNumber(warmup.pending_symbols_total, warmupPendingSymbols.length),
            warmup_ready_symbols: toNumber(warmup.ready_symbols, 0),
            warmup_symbols_total: toNumber(warmup.symbols_total, 0),
            warmup_ready_trade_symbols: toNumber(warmup.ready_trade_symbols, 0),
            warmup_trade_symbols_total: toNumber(warmup.trade_symbols_total, 0),
        },
        daily_scan: {
            market_date: String(dailyScan.market_date || ""),
            status: String(dailyScan.status || ""),
            reason: String(dailyScan.reason || ""),
            started_at: String(dailyScan.started_at || ""),
            finished_at: String(dailyScan.finished_at || ""),
            last_error: String(dailyScan.last_error || ""),
            result: asObject(dailyScan.result),
        },
        market_session: {
            kind: String(runtime.market_session && runtime.market_session.kind || ""),
            label: String(runtime.market_session && runtime.market_session.label || ""),
            requires_live_5m: Boolean(runtime.market_session && runtime.market_session.requires_live_5m === true),
        },
        session: {
            authenticated: Boolean(runtime.session && runtime.session.authenticated === true),
        },
        auth_recovery: {
            recovery_phase: String(runtime.auth_recovery && runtime.auth_recovery.recovery_phase || ""),
            recovery_class: String(runtime.auth_recovery && runtime.auth_recovery.recovery_class || ""),
            recovery_reason: String(runtime.auth_recovery && runtime.auth_recovery.recovery_reason || ""),
            interruption_kind: String(runtime.auth_recovery && runtime.auth_recovery.interruption_kind || ""),
            probe_result: String(runtime.auth_recovery && runtime.auth_recovery.probe_result || ""),
            auto_restart_scheduled: Boolean(runtime.auth_recovery && runtime.auth_recovery.auto_restart_scheduled === true),
            last_recovery_source: String(runtime.auth_recovery && runtime.auth_recovery.last_recovery_source || ""),
        },
        websocket: {
            connected: websocketConnected,
            ready: websocketReady,
            message_count: toNumber(runtime.websocket && runtime.websocket.message_count, 0),
            label: websocketConnected
                ? `connected / ready=${websocketReady ? "true" : "false"}`
                : "offline",
        },
        total_ticks: toNumber(runtime.bar_aggregator && runtime.bar_aggregator.total_ticks, 0),
        latest_bar: {
            ...latestBar,
            label: latestBar.bar_time_ms > 0
                ? `${latestBar.symbol || "-"} / ${latestBar.age_min || 0}m / ${latestBar.us_time || "-"}`
                : "no_data_today",
        },
        latest_indicator: {
            ...latestIndicator,
            lag_min: indicatorLagMin,
            label: latestIndicator.bar_time_ms > 0
                ? `${latestIndicator.symbol || "-"} / lag ${indicatorLagMin}m / ${latestIndicator.us_time || "-"}`
                : "missing",
        },
        bar_bucket: barBucket,
        today: today,
        account: {
            ok: Boolean(account && account.ok !== false),
            positions: toNumber(account.counts && account.counts.open_positions, 0),
            open_orders: toNumber(account.counts && account.counts.open_orders, 0),
            net_liquidation: toNumber(account.summary && account.summary.net_liquidation, 0),
        },
        active_target_count: toNumber(runtime.market_universe && runtime.market_universe.active_target_count, 0),
        trading_enabled: runtimeModes.getTradingEnabledForEnvironment(environment, RUNTIME_KEYS),
        auth: auth,
    }
}

function getIndicatorMissingGraceState(snapshot, freshnessWindow) {
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    const latestBar = snapshot && snapshot.latest_bar && typeof snapshot.latest_bar === "object" ? snapshot.latest_bar : {}
    const latestIndicator = snapshot && snapshot.latest_indicator && typeof snapshot.latest_indicator === "object" ? snapshot.latest_indicator : {}
    const latestBarMs = toNumber(latestBar.bar_time_ms, 0)
    const latestBarCreatedMs = toNumber(latestBar.created_ms, 0)
    const latestIndicatorMs = toNumber(latestIndicator.bar_time_ms, 0)
    if (!enforceFreshness || latestBarMs <= 0 || latestIndicatorMs > 0 || latestBarCreatedMs <= 0) {
        return { active: false, elapsed_ms: 0, remaining_ms: 0 }
    }
    const elapsedMs = Math.max(0, Date.now() - latestBarCreatedMs)
    const remainingMs = Math.max(0, INDICATOR_MISSING_GRACE_MS - elapsedMs)
    return {
        active: remainingMs > 0,
        elapsed_ms: elapsedMs,
        remaining_ms: remainingMs,
    }
}

function hasMissingLatestIndicator(snapshot, freshnessWindow) {
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    if (!enforceFreshness) return false
    const latestIndicator = snapshot && snapshot.latest_indicator && typeof snapshot.latest_indicator === "object" ? snapshot.latest_indicator : {}
    if (toNumber(latestIndicator.bar_time_ms, 0) > 0) return false
    return !getIndicatorMissingGraceState(snapshot, freshnessWindow).active
}

function hasIndicatorFreshnessIssue(snapshot, freshnessWindow) {
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    if (!enforceFreshness) return false
    const latestIndicator = snapshot && snapshot.latest_indicator && typeof snapshot.latest_indicator === "object" ? snapshot.latest_indicator : {}
    return hasMissingLatestIndicator(snapshot, freshnessWindow)
        || toNumber(latestIndicator.lag_min, 0) > INDICATOR_STALE_WARN_MIN
}

function buildIndicatorLagIssue(snapshot) {
    const latestIndicator = snapshot && snapshot.latest_indicator && typeof snapshot.latest_indicator === "object" ? snapshot.latest_indicator : {}
    const lagMin = toNumber(latestIndicator.lag_min, 0)
    if (lagMin <= INDICATOR_STALE_WARN_MIN) return ""
    if (lagMin >= 180) {
        return `最新指标未跟上当前 5m bars（延迟 ${lagMin}m）`
    }
    return `指标延迟 ${lagMin}m`
}

function getIndicatorStatusLabel(snapshot, freshnessWindow) {
    const latestIndicator = snapshot && snapshot.latest_indicator && typeof snapshot.latest_indicator === "object" ? snapshot.latest_indicator : {}
    if (toNumber(latestIndicator.bar_time_ms, 0) > 0) {
        return latestIndicator.label || "ok"
    }
    const grace = getIndicatorMissingGraceState(snapshot, freshnessWindow)
    if (!grace.active) {
        return latestIndicator.label || "missing"
    }
    return `pending / grace ${Math.ceil(grace.remaining_ms / 1000)}s`
}

function getUsClock() {
    const usParts = usEasternTime.getUsEasternParts(Date.now())
    return {
        date: `${String(usParts.year || 0).padStart(4, "0")}-${String(usParts.month || 0).padStart(2, "0")}-${String(usParts.day || 0).padStart(2, "0")}`,
        time: `${String(usParts.hour || 0).padStart(2, "0")}:${String(usParts.minute || 0).padStart(2, "0")}`,
        hour: usParts.hour || 0,
        minute: usParts.minute || 0,
        weekday: usParts.weekday || 0,
    }
}

function matchesUsDate(value, dateToken) {
    return String(value || "").slice(0, 10) === String(dateToken || "")
}

function normalizeDailyScanState(snapshot, times) {
    const raw = snapshot && snapshot.daily_scan && typeof snapshot.daily_scan === "object"
        ? snapshot.daily_scan
        : snapshot
    const data = raw && typeof raw === "object" ? raw : {}
    const result = asObject(data.result)
    const marketDate = String(data.market_date || "").trim()
    const currentDate = String(times && times.date || "").trim()
    return {
        market_date: marketDate,
        status: String(data.status || "").trim().toLowerCase(),
        reason: String(data.reason || "").trim(),
        started_at: String(data.started_at || "").trim(),
        finished_at: String(data.finished_at || "").trim(),
        last_error: String(data.last_error || "").trim(),
        active_count: toNumber(result.active, 0),
        candidate_count: toNumber(result.candidates, 0),
        scanned: toNumber(result.scanned, 0),
        errors: toNumber(result.errors, 0),
        rejection_summary: asObject(result.rejection_summary),
        rejection_examples: Array.isArray(result.rejection_examples) ? result.rejection_examples : [],
        result: result,
        is_current_date: !!marketDate && !!currentDate && marketDate === currentDate,
    }
}

function buildDailyScanStatusLabel(snapshot, times) {
    const dailyScan = normalizeDailyScanState(snapshot, times)
    const status = dailyScan.status || "idle"
    const parts = [status]
    if (dailyScan.market_date) parts.push(dailyScan.market_date)
    if (dailyScan.scanned > 0 || dailyScan.active_count > 0 || dailyScan.candidate_count > 0 || dailyScan.errors > 0) {
        parts.push(
            `scanned ${dailyScan.scanned}`,
            `active ${dailyScan.active_count}`,
            `candidate ${dailyScan.candidate_count}`,
            `errors ${dailyScan.errors}`
        )
    }
    if (dailyScan.finished_at) {
        parts.push(`finished ${dailyScan.finished_at}`)
    } else if (dailyScan.started_at) {
        parts.push(`started ${dailyScan.started_at}`)
    }
    return parts.join(" / ")
}

function buildDailyScanRejectionSummary(snapshot, times, limit = 4) {
    const dailyScan = normalizeDailyScanState(snapshot, times)
    const entries = Object.entries(dailyScan.rejection_summary || {})
        .filter(([, count]) => toNumber(count, 0) > 0)
        .sort((left, right) => {
            const countDiff = toNumber(right[1], 0) - toNumber(left[1], 0)
            if (countDiff !== 0) return countDiff
            return String(left[0] || "").localeCompare(String(right[0] || ""))
        })
        .slice(0, Math.max(1, limit))
    if (!entries.length) {
        if (dailyScan.scanned <= 0) return "watchlist 为空或日筛尚未装载可评估标的"
        return "所有标的都被技术投票或质量门过滤"
    }
    return entries.map(([bucket, count]) => `${bucket}:${toNumber(count, 0)}`).join(", ")
}

function buildDailyScanRejectionExamples(snapshot, times, limit = 3) {
    const dailyScan = normalizeDailyScanState(snapshot, times)
    const examples = Array.isArray(dailyScan.rejection_examples) ? dailyScan.rejection_examples : []
    if (!examples.length) return ""
    return examples.slice(0, Math.max(1, limit)).map((item) => {
        const bucket = String(item && item.bucket || "").trim()
        const symbol = String(item && item.symbol || "").trim().toUpperCase()
        const actual = String(item && item.actual || "").trim()
        const threshold = String(item && item.threshold || "").trim()
        const note = String(item && item.note || "").trim()
        const parts = [symbol || bucket || "example"]
        if (bucket) parts.push(bucket)
        if (actual && threshold) {
            parts.push(`${actual} <=> ${threshold}`)
        } else if (actual) {
            parts.push(actual)
        } else if (threshold) {
            parts.push(threshold)
        } else if (note) {
            parts.push(note)
        }
        return parts.join(" / ")
    }).join(" ; ")
}

function classifyMarketSession(snapshot, times, clock) {
    const weekday = Number(clock && clock.weekday)
    const isWeekend = weekday === 0 || weekday === 6
    const dailyScan = normalizeDailyScanState(snapshot, times)
    const dailyScanTargetCount = dailyScan.active_count + dailyScan.candidate_count
    const currentDailyScanTargetCount = dailyScan.is_current_date ? dailyScanTargetCount : 0
    const hasTargets = toNumber(snapshot && snapshot.today && snapshot.today.targets, 0) > 0
        || toNumber(snapshot && snapshot.active_target_count, 0) > 0
        || currentDailyScanTargetCount > 0
    const hasIntradayActivity = (
        toNumber(snapshot && snapshot.today && snapshot.today.bars, 0) > 0
        || toNumber(snapshot && snapshot.today && snapshot.today.indicators, 0) > 0
        || toNumber(snapshot && snapshot.today && snapshot.today.signals, 0) > 0
        || toNumber(snapshot && snapshot.today && snapshot.today.orders, 0) > 0
        || matchesUsDate(snapshot && snapshot.latest_bar && snapshot.latest_bar.us_time, times && times.date)
    )

    if (isWeekend) {
        return {
            kind: "closed",
            label: "周末休市",
            open_title: "IBKR 休市提醒",
            open_summary: "今日为周末休市，09:20 仍按日常规则发送系统状态提醒。",
            close_title: "IBKR 闭市汇总",
            close_summary: "今日为周末休市，按闭市日生成系统汇总。",
            reason: "weekend",
        }
    }

    if (dailyScan.is_current_date && dailyScan.status === "failed") {
        return {
            kind: "trading",
            label: "交易日",
            open_title: "IBKR 开盘前系统检查（筛选失败）",
            open_summary: "今日为交易日，但盘前日筛失败，目标池未正常生成。",
            close_title: "IBKR 收盘汇总",
            close_summary: "今日交易已收盘，已生成当日系统汇总。",
            reason: "daily_scan_failed",
        }
    }

    if (dailyScan.is_current_date && dailyScan.status === "completed") {
        if (currentDailyScanTargetCount > 0 || hasTargets || hasIntradayActivity) {
            return {
                kind: "trading",
                label: "交易日",
                open_title: "IBKR 开盘前系统检查",
                open_summary: "今日为交易日，09:20 开盘前系统状态检查已完成。",
                close_title: "IBKR 收盘汇总",
                close_summary: "今日交易已收盘，已生成当日系统汇总。",
                reason: "targets_ready",
            }
        }
        return {
            kind: "trading",
            label: "交易日",
            open_title: "IBKR 开盘前系统检查（未筛出标的）",
            open_summary: "今日为交易日，盘前日筛已执行完成，但未筛出 active / candidate 标的。",
            close_title: "IBKR 收盘汇总",
            close_summary: "今日交易已收盘，已生成当日系统汇总。",
            reason: "daily_scan_zero_targets",
        }
    }

    if (hasTargets || hasIntradayActivity) {
        return {
            kind: "trading",
            label: "交易日",
            open_title: "IBKR 开盘前系统检查",
            open_summary: "今日为交易日，09:20 开盘前系统状态检查已完成。",
            close_title: "IBKR 收盘汇总",
            close_summary: "今日交易已收盘，已生成当日系统汇总。",
            reason: hasTargets ? "targets_ready" : "intraday_activity_detected",
        }
    }

    return {
        kind: "trading_pending",
        label: "交易日待确认",
        open_title: "IBKR 开盘前系统检查（待日筛）",
        open_summary: "当前未检测到当日可用目标池；09:20 状态检查时盘前日筛尚未完成，先按待扫描处理。",
        close_title: "IBKR 非交易日汇总",
        close_summary: "当前未检测到今日交易活动，按闭市日生成系统汇总。",
        reason: "daily_scan_pending",
    }
}

function resolveDataFreshnessSessionKind(snapshot, clock) {
    const runtimeSessionKind = String(snapshot && snapshot.market_session && snapshot.market_session.kind || "").trim().toLowerCase()
    if (runtimeSessionKind) {
        return runtimeSessionKind
    }
    const weekday = Number(clock && clock.weekday)
    if (weekday === 0 || weekday === 6) {
        return "closed"
    }
    const minuteOfDay = toNumber(clock && clock.hour, 0) * 60 + toNumber(clock && clock.minute, 0)
    if (minuteOfDay < (9 * 60 + 40)) {
        return "closed"
    }
    if (minuteOfDay < (16 * 60)) {
        return "regular"
    }
    if (minuteOfDay < (16 * 60 + 10)) {
        return "close_transition"
    }
    if (minuteOfDay < (20 * 60)) {
        return "afterhours"
    }
    return "closed"
}

function isAuthRecoveryInProgress(snapshot) {
    const authRecovery = snapshot && snapshot.auth_recovery && typeof snapshot.auth_recovery === "object"
        ? snapshot.auth_recovery
        : {}
    const recoveryPhase = String(authRecovery.recovery_phase || "").trim().toLowerCase()
    const recoveryClass = String(authRecovery.recovery_class || "").trim().toLowerCase()
    const probeResult = String(authRecovery.probe_result || "").trim().toLowerCase()
    if (!snapshot || !snapshot.session || snapshot.session.authenticated) {
        return false
    }
    if (recoveryClass === "manual_auth_required" || recoveryPhase === "requested") {
        return false
    }
    return (
        recoveryPhase === "silent_probe"
        || recoveryPhase === "resume_waiting_manual"
        || authRecovery.auto_restart_scheduled === true
        || recoveryClass === "scheduled_restart"
        || recoveryClass === "stale_broker"
        || ["pending", "self_heal", "self_heal_pending", "stale_broker_restart_scheduled", "resume_probe_timeout"].indexOf(probeResult) !== -1
    )
}

function buildDataFreshnessWindow(snapshot, times, clock) {
    const marketSession = classifyMarketSession(snapshot, times, clock)
    if (marketSession.kind !== "trading") {
        return {
            required: false,
            reason: marketSession.reason || "market_closed",
            label: marketSession.label || "closed",
            session_kind: "closed",
            enforce_latest_bar_age: false,
        }
    }
    const sessionKind = resolveDataFreshnessSessionKind(snapshot, clock)
    if (sessionKind === "regular") {
        return {
            required: true,
            reason: "regular_session",
            label: "盘中新鲜度检查",
            session_kind: sessionKind,
            enforce_latest_bar_age: true,
        }
    }
    if (sessionKind === "close_transition") {
        return {
            required: true,
            reason: "close_transition",
            label: "收盘过渡期",
            session_kind: sessionKind,
            enforce_latest_bar_age: false,
        }
    }
    if (sessionKind === "afterhours") {
        return {
            required: true,
            reason: "afterhours",
            label: "盘后 5m 连续性检查",
            session_kind: sessionKind,
            enforce_latest_bar_age: true,
        }
    }
    return {
        required: false,
        reason: sessionKind === "closed" ? "market_closed" : "pre_open",
        label: sessionKind === "closed" ? "非交易时段" : "盘前宽限期",
        session_kind: sessionKind || "closed",
        enforce_latest_bar_age: false,
    }
}

function hasLatestBarAgeIssue(snapshot, freshnessWindow) {
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    const enforceLatestBarAge = !freshnessWindow || freshnessWindow.enforce_latest_bar_age !== false
    if (!enforceFreshness || !enforceLatestBarAge) {
        return false
    }
    const latestBar = snapshot && snapshot.latest_bar && typeof snapshot.latest_bar === "object" ? snapshot.latest_bar : {}
    if (toNumber(latestBar.bar_time_ms, 0) <= 0) {
        return false
    }
    return toNumber(latestBar.age_min, 0) > BAR_STALE_WARN_MIN
}

function buildBarBucketLabel(bucket) {
    const parts = []
    const status = String(bucket && bucket.status || "").trim().toLowerCase()
    if (status) {
        parts.push(status)
    }
    if (bucket && bucket.last_due_bucket_us) {
        parts.push(`due ${bucket.last_due_bucket_us}`)
    }
    if (bucket && bucket.last_completed_bucket_us) {
        parts.push(`done ${bucket.last_completed_bucket_us}`)
    }
    if (toNumber(bucket && bucket.pending_symbols_total, 0) > 0) {
        parts.push(`pending ${toNumber(bucket.pending_symbols_total, 0)}`)
        const pendingPreview = formatPendingDetailPreview(bucket && bucket.pending_symbol_details)
            || formatPendingSymbolsPreview(bucket && bucket.pending_symbols)
        if (pendingPreview) {
            parts.push(`symbols ${pendingPreview}`)
        }
    } else if (toNumber(bucket && bucket.lag_s, 0) > 0) {
        parts.push(`lag ${Math.round(toNumber(bucket.lag_s, 0))}s`)
    }
    return parts.length ? parts.join(" / ") : "unknown"
}

function normalizeBarBucketLagSeconds(lagValue, dueBucketMs, completedBucketMs) {
    const dueMs = toNumber(dueBucketMs, 0)
    const completedMs = toNumber(completedBucketMs, 0)
    if (dueMs > 0) {
        const referenceMs = completedMs > 0 && completedMs >= dueMs ? completedMs : Date.now()
        return Math.max(0, Math.round((referenceMs - dueMs) / 1000))
    }
    const rawLag = toNumber(lagValue, 0)
    if (rawLag > 1000000000000) {
        return Math.max(0, Math.round((Date.now() - rawLag) / 1000))
    }
    if (rawLag > 1000000000) {
        return Math.max(0, Math.round(Date.now() / 1000 - rawLag))
    }
    return rawLag
}

function buildBarBucketSnapshot(runtimePayload) {
    const runtime = runtimePayload && typeof runtimePayload === "object" ? runtimePayload : {}
    const canonical = runtime.canonical_5m && typeof runtime.canonical_5m === "object"
        ? runtime.canonical_5m
        : {}
    const marketUniverse = runtime.market_universe && typeof runtime.market_universe === "object"
        ? runtime.market_universe
        : {}
    const freshness = marketUniverse.bar_freshness && typeof marketUniverse.bar_freshness === "object"
        ? marketUniverse.bar_freshness
        : {}
    const pendingTotal = toNumber(canonical.pending_symbols_total, toNumber(freshness.pending_symbols_total, 0))
    const dueBucketMs = toNumber(canonical.last_due_bucket_ms, 0)
    const completedBucketMs = toNumber(canonical.last_completed_bucket_ms, 0)
    const lagS = normalizeBarBucketLagSeconds(
        toNumber(canonical.lag_s, toNumber(freshness.lag_s, 0)),
        dueBucketMs,
        completedBucketMs
    )
    const bucket = {
        enabled: canonical.enabled !== false,
        status: String(freshness.status || "").trim().toLowerCase() || (
            lagS <= 90 && pendingTotal <= 0
                ? "fresh"
                : ((lagS > 0 || pendingTotal > 0) ? "stale" : "unknown")
        ),
        lag_s: lagS,
        pending_symbols_total: pendingTotal,
        pending_symbols: normalizeSymbolList(canonical.pending_symbols),
        pending_symbol_details: Array.isArray(canonical.pending_symbol_details)
            ? canonical.pending_symbol_details.slice(0, 8).map((item) => ({
                symbol: String(item && item.symbol || "").trim().toUpperCase(),
                missing_count: toNumber(item && item.missing_count, 0),
                missing_us_times: Array.isArray(item && item.missing_us_times)
                    ? item.missing_us_times.slice(0, 4).map((value) => String(value || ""))
                    : [],
            })).filter((item) => item.symbol)
            : [],
        sequence_gap_count: toNumber(canonical.sequence_gap_count, 0),
        missing_required_bars_total: toNumber(canonical.missing_required_bars_total, 0),
        last_completed_bucket_ms: completedBucketMs,
        last_completed_bucket_us: String(canonical.last_completed_bucket_us || freshness.last_completed_bucket_us || ""),
        last_due_bucket_ms: dueBucketMs,
        last_due_bucket_us: String(canonical.last_due_bucket_us || ""),
    }
    bucket.label = buildBarBucketLabel(bucket)
    return bucket
}

function listBarBucketProblems(snapshot, freshnessWindow) {
    const problems = []
    if (!snapshot) return problems
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    const bucket = snapshot.bar_bucket && typeof snapshot.bar_bucket === "object" ? snapshot.bar_bucket : {}
    if (!enforceFreshness || bucket.enabled === false) {
        return problems
    }
    const status = String(bucket.status || "").trim().toLowerCase()
    const pendingTotal = toNumber(bucket.pending_symbols_total, 0)
    const lagS = toNumber(bucket.lag_s, 0)
    const dueBucketMs = toNumber(bucket.last_due_bucket_ms, 0)
    const completedBucketMs = toNumber(bucket.last_completed_bucket_ms, 0)
    const bucketIncomplete = dueBucketMs > 0 && (completedBucketMs <= 0 || completedBucketMs < dueBucketMs)
    if (status === "stale" || bucketIncomplete) {
        let label = "当前 5m bar 桶未完成"
        if (pendingTotal > 0) {
            label += `，pending ${pendingTotal}`
            const pendingPreview = formatPendingDetailPreview(bucket.pending_symbol_details)
                || formatPendingSymbolsPreview(bucket.pending_symbols)
            if (pendingPreview) {
                label += `：${pendingPreview}`
            }
        } else if (lagS > 0) {
            label += `，lag ${Math.round(lagS)}s`
        } else if (bucket.last_due_bucket_us || bucket.last_completed_bucket_us) {
            label += `，due ${bucket.last_due_bucket_us || "--"} / done ${bucket.last_completed_bucket_us || "--"}`
        }
        problems.push(label)
    }
    return problems
}

function hasBarBucketIssue(snapshot, freshnessWindow) {
    return listBarBucketProblems(snapshot, freshnessWindow).length > 0
}

function formatWarmupPhaseLabel(value) {
    const normalized = String(value || "").trim().toLowerCase()
    const labels = {
        idle: "空闲",
        pending: "待启动",
        running: "进行中",
        ready: "已就绪",
        blocked: "已阻塞",
        failed: "失败",
    }
    return labels[normalized] || (normalized || "未知")
}

function buildWarmupProgressLabel(snapshot) {
    const runtime = snapshot && snapshot.runtime ? snapshot.runtime : {}
    const tradeReady = toNumber(runtime.warmup_ready_trade_symbols, 0)
    const tradeTotal = toNumber(runtime.warmup_trade_symbols_total, 0)
    const pendingTotal = toNumber(runtime.warmup_pending_symbols_total, 0)
    const parts = [`阶段 ${formatWarmupPhaseLabel(runtime.warmup_phase)}`]

    if (tradeTotal > 0) {
        parts.push(`交易标的就绪 ${tradeReady}/${tradeTotal}`)
    }
    if (pendingTotal > 0 || String(runtime.warmup_phase || "").trim().toLowerCase() === "pending") {
        parts.push(`待补齐 ${pendingTotal}`)
    }
    if (runtime.warmup_last_success_at) {
        parts.push(`最近完成 ${runtime.warmup_last_success_at}`)
    } else if (runtime.warmup_started_at) {
        parts.push(`开始于 ${runtime.warmup_started_at}`)
    }

    return parts.join(" · ")
}

function buildDailyOpenReminderDetail(snapshot, assessment, marketSession, freshnessWindow, times, eventCounts) {
    const detail = {
        "日程判断": marketSession.open_summary,
        "今日模式": marketSession.label,
        "状态结论": assessment.summary,
        "检查时间": times.us,
        "Compute": snapshot.compute.status || "unknown",
        "认证": snapshot.session.authenticated ? "ok" : "pending",
        "WebSocket": snapshot.websocket.label,
        "2FA状态": snapshot.auth.label,
        "引擎就绪": `${snapshot.compute.ready_engines || 0}/${snapshot.compute.total_engines || 0}`,
        "盘前预热": buildWarmupProgressLabel(snapshot),
        "最新5m": snapshot.latest_bar.label,
        "当前Bar桶": snapshot.bar_bucket.label,
        "指标状态": getIndicatorStatusLabel(snapshot, freshnessWindow),
        "今日概况": `bars ${snapshot.today.bars} / ind ${snapshot.today.indicators} / sig ${snapshot.today.signals} / ord ${snapshot.today.orders}`,
        "目标池": `${snapshot.today.targets} targets / active ${snapshot.active_target_count || 0}`,
        "日筛状态": buildDailyScanStatusLabel(snapshot, times),
        "系统事件": `${eventCounts.events} / error ${eventCounts.error_events}`,
        "交易开关": snapshot.trading_enabled ? "true" : "false",
    }
    if (snapshot.account.ok) {
        detail["实时账户"] = `pos ${snapshot.account.positions} / open ${snapshot.account.open_orders} / netliq ${snapshot.account.net_liquidation.toFixed(2)}`
    } else {
        detail["实时账户"] = "unavailable"
    }
    if (snapshot.auth.mode) detail["验证模式"] = snapshot.auth.mode
    if (snapshot.auth.last_result) detail["2FA反馈"] = snapshot.auth.last_result
    if (snapshot.auth.last_error) detail["2FA异常"] = snapshot.auth.last_error
    if (marketSession.reason === "daily_scan_failed") {
        const dailyScan = normalizeDailyScanState(snapshot, times)
        detail["日筛错误"] = dailyScan.last_error || "daily_scan_failed"
    } else if (marketSession.reason === "daily_scan_zero_targets") {
        detail["未筛出原因"] = buildDailyScanRejectionSummary(snapshot, times)
        const examples = buildDailyScanRejectionExamples(snapshot, times)
        if (examples) detail["原因样例"] = examples
    } else if (marketSession.reason === "daily_scan_pending") {
        detail["待扫说明"] = "09:20 检查时 runtime 仍在等待本轮盘前日筛完成，目标池会在日筛结束后刷新。"
    }
    return detail
}

function buildDailyCloseSummaryDetail(snapshot, assessment, marketSession, freshnessWindow, times, eventCounts) {
    const detail = {
        "日期": times.date,
        "收盘结论": marketSession.close_summary,
        "今日模式": marketSession.label,
        "系统状态": assessment.summary,
        "信号数": String(snapshot.today.signals || 0),
        "订单数": String(snapshot.today.orders || 0),
        "IBKR Bars": String(snapshot.today.bars || 0),
        "指标数": String(snapshot.today.indicators || 0),
        "Targets": String(snapshot.today.targets || 0),
        "系统事件": String(eventCounts.events || 0),
        "错误事件": String(eventCounts.error_events || 0),
        "2FA状态": snapshot.auth.label,
        "最新5m": snapshot.latest_bar.label,
        "当前Bar桶": snapshot.bar_bucket.label,
        "指标状态": getIndicatorStatusLabel(snapshot, freshnessWindow),
        "交易开关": snapshot.trading_enabled ? "true" : "false",
        "汇总时间": times.us,
    }
    if (snapshot.account.ok) {
        detail["实时账户"] = `pos ${snapshot.account.positions} / open ${snapshot.account.open_orders} / netliq ${snapshot.account.net_liquidation.toFixed(2)}`
    } else {
        detail["实时账户"] = "unavailable"
    }
    return detail
}

function isStatusSummaryNotifyEnabled(environment) {
    const envUtils = require(`${__hooks}/lib/environment.js`)
    const runtimeModes = require(`${__hooks}/lib/runtime_modes.js`)
    return runtimeModes.isEnabledConfigValue(
        envUtils.getConfigValue("status_notify_enabled", "TRUE", environment)
    )
}

function isStatusSummaryMinute(minute) {
    return minute === 0 || minute === 30
}

function hasHeartbeatIssue(snapshot, freshnessWindow) {
    return (
        snapshot.compute.status !== "running"
        || (
            freshnessWindow && freshnessWindow.required
            && (
                hasLatestBarAgeIssue(snapshot, freshnessWindow)
                || hasIndicatorFreshnessIssue(snapshot, freshnessWindow)
                || hasBarBucketIssue(snapshot, freshnessWindow)
            )
        )
    )
}

function isStartupGraceActive(snapshot) {
    if (!snapshot || !snapshot.compute) return false
    const runtime = snapshot.runtime && typeof snapshot.runtime === "object" ? snapshot.runtime : {}
    const warmupPhase = String(runtime.warmup_phase || "").trim().toLowerCase()
    const hasWarmupSuccess = Boolean(String(runtime.warmup_last_success_at || "").trim())
    if (snapshot.compute.status === "running" && runtime.starting) {
        return true
    }
    const uptimeMs = toNumber(snapshot.compute.uptime_s, 0) * 1000
    if (uptimeMs > 0 && uptimeMs < COMPUTE_STARTUP_GRACE_MS) {
        return true
    }
    return (warmupPhase === "pending" || warmupPhase === "running") && !hasWarmupSuccess
}

function buildStartupGraceLabel(snapshot) {
    const uptimeS = toNumber(snapshot && snapshot.compute && snapshot.compute.uptime_s, 0)
    const runtime = snapshot && snapshot.runtime && typeof snapshot.runtime === "object" ? snapshot.runtime : {}
    const warmupPhase = String(runtime.warmup_phase || "").trim()
    const tradeReady = toNumber(runtime.warmup_ready_trade_symbols, 0)
    const tradeTotal = toNumber(runtime.warmup_trade_symbols_total, 0)
    const pendingTotal = toNumber(runtime.warmup_pending_symbols_total, 0)
    const parts = []
    if (runtime.starting) {
        parts.push("运行态仍在启动")
    }
    if (uptimeS > 0 && uptimeS < Math.round(COMPUTE_STARTUP_GRACE_MS / 1000)) {
        parts.push(`Compute 运行 ${Math.round(uptimeS)}s`)
    }
    if (warmupPhase && !String(runtime.warmup_last_success_at || "").trim()) {
        if (tradeTotal > 0) {
            parts.push(`启动预热 ${tradeReady}/${tradeTotal}`)
        } else {
            parts.push(`启动预热 ${formatWarmupPhaseLabel(warmupPhase)}`)
        }
        if (pendingTotal > 0) {
            parts.push(`剩余 ${pendingTotal} 个标的`)
        }
    }
    if (!parts.length) {
        parts.push("启动保护生效")
    }
    return `${parts.join("；")}；保护期 ${Math.round(COMPUTE_STARTUP_GRACE_MS / 1000)}s`
}

function buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow) {
    const blockingIssues = []
    const watchItems = []
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    const authRecoveryInProgress = isAuthRecoveryInProgress(snapshot)

    if (!snapshot || !snapshot.compute) {
        return {
            level: "warning",
            kind: "broken",
            summary: "当前系统状态无法正确判断，状态快照缺失。",
        }
    }

    if (snapshot.compute.status !== "running") {
        blockingIssues.push(`Compute=${snapshot.compute.status || "unknown"}`)
    }

    if (!snapshot.session.authenticated) {
        if (startupGraceActive) {
            watchItems.push("IBKR 会话尚未完成认证")
        } else if (authRecoveryInProgress) {
            watchItems.push("IBKR 会话正在静默恢复")
        } else {
            blockingIssues.push("IBKR 会话未认证")
        }
    }

    if (!snapshot.websocket.connected) {
        if (startupGraceActive) {
            watchItems.push("WebSocket 尚未连接")
        } else {
            blockingIssues.push("WebSocket 未连接")
        }
    } else if (!snapshot.websocket.ready) {
        watchItems.push("WebSocket 已连接但未 ready")
    }

    if (enforceFreshness && snapshot.latest_bar.bar_time_ms <= 0) {
        if (startupGraceActive) {
            watchItems.push("最新 5m bars 尚未建立")
        } else {
            blockingIssues.push("缺少最新 5m bars")
        }
    } else if (hasLatestBarAgeIssue(snapshot, freshnessWindow)) {
        if (startupGraceActive) {
            watchItems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        } else {
            blockingIssues.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        }
    }

    if (hasMissingLatestIndicator(snapshot, freshnessWindow)) {
        if (startupGraceActive) {
            watchItems.push("指标流尚未建立")
        } else {
            blockingIssues.push("缺少最新指标")
        }
    } else if (enforceFreshness && snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
        const indicatorLagIssue = buildIndicatorLagIssue(snapshot)
        if (startupGraceActive) {
            watchItems.push(indicatorLagIssue)
        } else {
            blockingIssues.push(indicatorLagIssue)
        }
    }

    const barBucketProblems = listBarBucketProblems(snapshot, freshnessWindow)
    for (let i = 0; i < barBucketProblems.length; i++) {
        if (startupGraceActive) {
            watchItems.push(barBucketProblems[i])
        } else {
            blockingIssues.push(barBucketProblems[i])
        }
    }

    if (!snapshot.account.ok) {
        watchItems.push("账户快照不可用")
    }

    if (startupGraceActive) {
        const graceLabel = buildStartupGraceLabel(snapshot)
        watchItems.unshift(`系统处于启动宽限期（${graceLabel}）`)
    }

    if (blockingIssues.length > 0) {
        return {
            level: "warning",
            kind: "broken",
            summary: `当前系统状态不正确，需要处理：${blockingIssues.join("；")}。`,
        }
    }

    if (watchItems.length > 0) {
        return {
            level: "warning",
            kind: "watch",
            summary: `当前系统状态未完全就绪，暂不判定为正确：${watchItems.join("；")}。`,
        }
    }

    if (!snapshot.trading_enabled) {
        return {
            level: "info",
            kind: "healthy_paused",
            summary: "当前系统状态正确，行情、当前 5m bar 桶与执行链路正常；交易开关关闭，属于只观察模式。",
        }
    }

    return {
        level: "info",
        kind: "healthy",
        summary: "当前系统状态正确，Compute、认证、WebSocket、当前 5m bar 桶、bars 与指标链路正常。",
    }
}

function listDataHealthProblems(snapshot, freshnessWindow) {
    const problems = []
    if (!snapshot) return problems
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)

    if (enforceFreshness && snapshot.latest_bar.bar_time_ms <= 0) {
        problems.push("缺少最新 5m bars")
    } else if (hasLatestBarAgeIssue(snapshot, freshnessWindow)) {
        problems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
    }

    if (hasMissingLatestIndicator(snapshot, freshnessWindow)) {
        problems.push("缺少最新指标")
    } else if (enforceFreshness && snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
        problems.push(buildIndicatorLagIssue(snapshot))
    }

    const barBucketProblems = listBarBucketProblems(snapshot, freshnessWindow)
    for (let i = 0; i < barBucketProblems.length; i++) {
        problems.push(barBucketProblems[i])
    }

    if (!snapshot.session.authenticated && isAuthRecoveryInProgress(snapshot)) {
        problems.push("IBKR 会话正在静默恢复")
    } else if (!snapshot.session.authenticated) {
        problems.push("IBKR 会话未认证")
    }

    if (!snapshot.websocket.connected) {
        problems.push("WebSocket 未连接")
    } else if (!snapshot.websocket.ready) {
        problems.push("WebSocket 已连接但未 ready")
    }

    return problems
}

function buildDataHealthRecommendation(snapshot, freshnessWindow) {
    const actions = []
    if (!snapshot) return "检查 compute / IBKR / PocketBase 链路状态"
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)
    const bucketIssue = hasBarBucketIssue(snapshot, freshnessWindow)

    if (!snapshot.session.authenticated && isAuthRecoveryInProgress(snapshot)) {
        actions.push("观察静默恢复窗口；若超时再人工触发 2FA")
    } else if (!snapshot.session.authenticated) {
        actions.push("检查 IBKR 会话认证状态")
    }
    if (!snapshot.websocket.connected || !snapshot.websocket.ready) {
        actions.push("检查 WebSocket 连接与 IB Gateway 网关状态")
    }
    if (bucketIssue) {
        actions.push("检查 canonical 5m bar 桶完整性与 pending symbols")
    }
    if (
        bucketIssue || (
            enforceFreshness && (
                snapshot.latest_bar.bar_time_ms <= 0
                || hasLatestBarAgeIssue(snapshot, freshnessWindow)
                || hasIndicatorFreshnessIssue(snapshot, freshnessWindow)
            )
        )
    ) {
        actions.push("检查实时 ticks / bars / indicators 写入链路")
    }
    if (!actions.length) {
        actions.push("检查 compute 健康状态与行情链路")
    }
    return actions.join("；")
}

function buildRecoveryTitle(state) {
    const lastIssueKind = String(state && state.last_issue_kind || "").trim().toLowerCase()
    if (lastIssueKind === "compute") {
        return "IBKR Compute 服务已恢复"
    }
    if (lastIssueKind === "data") {
        return "IBKR 数据健康已恢复"
    }
    return "IBKR 系统状态已恢复"
}

function buildRecoveryDetail(snapshot, times, state, assessment, freshnessWindow) {
    const summary = assessment && assessment.kind === "healthy_paused"
        ? "先前异常已恢复，当前系统状态正确；交易开关关闭，属于只观察模式。"
        : "先前异常已恢复，当前系统状态正确，核心链路已恢复正常。"
    return {
        "恢复结论": summary,
        "恢复时间": times.us,
        "上次异常": String(state && (state.last_issue_summary || state.last_issue_title) || "n/a"),
        "Compute": snapshot.compute.status || "unknown",
        "认证": snapshot.session.authenticated ? "ok" : "pending",
        "WebSocket": snapshot.websocket.label,
        "最新5m": snapshot.latest_bar.label,
        "当前Bar桶": snapshot.bar_bucket.label,
        "指标状态": getIndicatorStatusLabel(snapshot, freshnessWindow),
        "交易开关": snapshot.trading_enabled ? "true" : "false",
    }
}

function shouldMergeHeartbeatIntoSummary(snapshot, currentMinute, environment, freshnessWindow) {
    if (!isStatusSummaryMinute(currentMinute)) {
        return false
    }
    if (hasHeartbeatIssue(snapshot, freshnessWindow)) {
        return true
    }
    return isStatusSummaryNotifyEnabled(environment)
}

function runSystemHeartbeatTick(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRSystemNotify]"
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const environments = listNotifyEnvironments(cronId)
    const now = new Date()
    const nowMs = now.getTime()
    const currentHourToken = times.us.slice(0, 13)
    const currentMinute = now.getMinutes()
    const clock = getUsClock()

    console.log(`${prefix} heartbeat tick: minute=${currentMinute}, environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const snapshot = buildStatusSnapshot(environment, times)
            const state = getStateData(HEARTBEAT_STATE_KEY, environment, times.date).data || {}
            const hadOutstandingIssue = String(state.last_issue_hash || "").trim() !== ""
            const startupGraceActive = isStartupGraceActive(snapshot)
            const startupGraceLabel = startupGraceActive ? buildStartupGraceLabel(snapshot) : ""
            const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
            const mergeHeartbeatIntoSummary = shouldMergeHeartbeatIntoSummary(snapshot, currentMinute, environment, freshnessWindow)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow)
            const isHealthyState = assessment.kind === "healthy" || assessment.kind === "healthy_paused"
            const patch = {
                last_checked_at: times.us,
                last_compute_status: snapshot.compute.status || "",
                last_bar_time_ms: snapshot.latest_bar.bar_time_ms || 0,
            }

            if (snapshot.compute.status !== "running") {
                const fingerprint = `compute:${snapshot.compute.status}:${snapshot.compute.error || ""}`
                const issueSummary = `当前系统状态不正确：Compute=${snapshot.compute.status || "unknown"}${snapshot.compute.error ? `；错误=${snapshot.compute.error}` : ""}。`
                patch.last_issue_kind = "compute"
                patch.last_issue_title = "IBKR Compute 服务离线"
                patch.last_issue_summary = issueSummary
                if (startupGraceActive) {
                    if (hadOutstandingIssue) {
                        patch.last_issue_kind = String(state.last_issue_kind || "")
                        patch.last_issue_title = String(state.last_issue_title || "")
                        patch.last_issue_summary = String(state.last_issue_summary || "")
                    } else {
                        patch.last_issue_hash = ""
                        patch.last_issue_ms = 0
                        patch.last_issue_at = ""
                        patch.last_issue_kind = ""
                        patch.last_issue_title = ""
                        patch.last_issue_summary = ""
                    }
                    patch.last_startup_grace_at = times.us
                    patch.last_startup_grace_reason = startupGraceLabel
                    console.log(`${prefix} heartbeat ${environment}: compute offline suppressed during startup grace (${startupGraceLabel})`)
                    saveStateData(HEARTBEAT_STATE_KEY, environment, times.date, patch)
                    continue
                }
                const shouldNotify = (
                    fingerprint !== String(state.last_issue_hash || "")
                    || nowMs - toNumber(state.last_issue_ms, 0) >= HEARTBEAT_ALERT_COOLDOWN_MS
                )
                if (shouldNotify) {
                    patch.last_issue_hash = fingerprint
                    patch.last_issue_ms = nowMs
                    patch.last_issue_at = times.us
                    if (mergeHeartbeatIntoSummary) {
                        console.log(`${prefix} heartbeat ${environment}: compute offline merged into status summary`)
                    } else {
                        const detail = {
                            "异常结论": issueSummary,
                            "检查时间": times.us,
                            "Compute": snapshot.compute.status || "unknown",
                            "错误": snapshot.compute.error || "n/a",
                            "建议": "检查 systemctl status ibkr-compute",
                        }
                        const notified = feishuSystem.notifySystemEvent("heartbeat", "error", "pb", "IBKR Compute 服务离线", detail, environment)
                        writeSystemEvent("heartbeat", "error", "pb", "IBKR Compute 服务离线", detail, environment, notified)
                        console.log(`${prefix} heartbeat ${environment}: compute offline, notified=${notified}`)
                    }
                }
                saveStateData(HEARTBEAT_STATE_KEY, environment, times.date, patch)
                continue
            }

            if (
                freshnessWindow.required && (
                snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
                || hasIndicatorFreshnessIssue(snapshot, freshnessWindow)
                || hasBarBucketIssue(snapshot, freshnessWindow)
                )
            ) {
                const fingerprint = `data:${snapshot.latest_bar.bar_time_ms || 0}:${snapshot.latest_indicator.bar_time_ms || 0}:${snapshot.latest_bar.age_min || 0}:${snapshot.latest_indicator.lag_min || 0}:${snapshot.bar_bucket.status || ""}:${snapshot.bar_bucket.last_due_bucket_ms || 0}:${snapshot.bar_bucket.last_completed_bucket_ms || 0}:${snapshot.bar_bucket.pending_symbols_total || 0}:${formatPendingSymbolsPreview(snapshot.bar_bucket.pending_symbols, 8)}`
                const problemSummary = listDataHealthProblems(snapshot, freshnessWindow)
                const issueSummary = problemSummary.length
                    ? `当前数据链路不正确：${problemSummary.join("；")}。`
                    : "当前数据链路不正确，请检查实时数据与指标链路。"
                patch.last_issue_kind = "data"
                patch.last_issue_title = "IBKR 数据健康异常"
                patch.last_issue_summary = issueSummary
                if (startupGraceActive) {
                    if (hadOutstandingIssue) {
                        patch.last_issue_kind = String(state.last_issue_kind || "")
                        patch.last_issue_title = String(state.last_issue_title || "")
                        patch.last_issue_summary = String(state.last_issue_summary || "")
                    } else {
                        patch.last_issue_hash = ""
                        patch.last_issue_ms = 0
                        patch.last_issue_at = ""
                        patch.last_issue_kind = ""
                        patch.last_issue_title = ""
                        patch.last_issue_summary = ""
                    }
                    patch.last_startup_grace_at = times.us
                    patch.last_startup_grace_reason = startupGraceLabel
                    console.log(`${prefix} heartbeat ${environment}: data warning suppressed during startup grace (${startupGraceLabel})`)
                    saveStateData(HEARTBEAT_STATE_KEY, environment, times.date, patch)
                    continue
                }
                const shouldNotify = (
                    fingerprint !== String(state.last_issue_hash || "")
                    || nowMs - toNumber(state.last_issue_ms, 0) >= HEARTBEAT_ALERT_COOLDOWN_MS
                )
                if (shouldNotify) {
                    patch.last_issue_hash = fingerprint
                    patch.last_issue_ms = nowMs
                    patch.last_issue_at = times.us
                    if (mergeHeartbeatIntoSummary) {
                        console.log(`${prefix} heartbeat ${environment}: data warning merged into status summary`)
                    } else {
                        const detail = {
                            "异常结论": issueSummary,
                            "检查时间": times.us,
                            "认证": snapshot.session.authenticated ? "ok" : "pending",
                            "最新5m": snapshot.latest_bar.label,
                            "当前Bar桶": snapshot.bar_bucket.label,
                            "指标状态": getIndicatorStatusLabel(snapshot, freshnessWindow),
                            "WebSocket": snapshot.websocket.label,
                            "建议": buildDataHealthRecommendation(snapshot, freshnessWindow),
                        }
                        const pendingPreview = formatPendingDetailPreview(snapshot.bar_bucket.pending_symbol_details, 8)
                            || formatPendingSymbolsPreview(snapshot.bar_bucket.pending_symbols, 8)
                        if (pendingPreview) {
                            detail["待补齐标的"] = pendingPreview
                        }
                        const notified = feishuSystem.notifySystemEvent("heartbeat", "warning", "pb", "IBKR 数据健康异常", detail, environment)
                        writeSystemEvent("heartbeat", "warning", "pb", "IBKR 数据健康异常", detail, environment, notified)
                        console.log(`${prefix} heartbeat ${environment}: data warning, notified=${notified}`)
                    }
                }
            } else {
                if (hadOutstandingIssue && isHealthyState) {
                    const title = buildRecoveryTitle(state)
                    const detail = buildRecoveryDetail(snapshot, times, state, assessment, freshnessWindow)
                    const notified = feishuSystem.notifySystemEvent("alert", "info", "pb", title, detail, environment)
                    writeSystemEvent("alert", "info", "pb", title, detail, environment, notified)
                    console.log(`${prefix} heartbeat ${environment}: recovery notified=${notified}`)
                }
                if (isHealthyState) {
                    patch.last_issue_hash = ""
                    patch.last_issue_ms = 0
                    patch.last_issue_at = ""
                    patch.last_issue_kind = ""
                    patch.last_issue_title = ""
                    patch.last_issue_summary = ""
                    patch.last_startup_grace_at = ""
                    patch.last_startup_grace_reason = ""
                }
            }

            const suppressOkHeartbeatForSummary = isHealthyState && currentMinute < 5 && isStatusSummaryNotifyEnabled(environment)
            if (suppressOkHeartbeatForSummary) {
                console.log(`${prefix} heartbeat ${environment}: ok heartbeat merged into status summary`)
            }
            const shouldSendOkHeartbeat = (
                isHealthyState
                && String(state.last_issue_hash || "").trim() === ""
                && currentMinute < 5
                && !suppressOkHeartbeatForSummary
                && String(state.last_ok_hour || "") !== currentHourToken
            )
            if (shouldSendOkHeartbeat) {
                const detail = {
                    "状态": "ok",
                    "ibkr_compute": "running",
                    "trading": snapshot.trading_enabled ? "true" : "false",
                    "WebSocket": snapshot.websocket.label,
                    "最新5m": snapshot.latest_bar.label,
                    "当前Bar桶": snapshot.bar_bucket.label,
                }
                const notified = feishuSystem.notifySystemEvent("heartbeat", "info", "pb", "IBKR 系统心跳（兜底）", detail, environment)
                writeSystemEvent("heartbeat", "info", "pb", "IBKR 系统心跳（兜底）", detail, environment, notified)
                patch.last_ok_hour = currentHourToken
                console.log(`${prefix} heartbeat ${environment}: ok, notified=${notified}`)
            }

            saveStateData(HEARTBEAT_STATE_KEY, environment, times.date, patch)
        } catch (err) {
            console.log(`${prefix} heartbeat ${environment} error: ${err.message || err}`)
        }
    }
}

function runSystemStatusReminderTick(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRSystemNotify]"
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const clock = getUsClock()
    const environments = listNotifyEnvironments(cronId)

    console.log(`${prefix} status reminder tick: environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const snapshot = buildStatusSnapshot(environment, times)
            const startupGraceActive = isStartupGraceActive(snapshot)
            const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow)
            const level = assessment.level

            const detail = {
                "状态结论": assessment.summary,
                "检查时间": times.us,
                "Compute": snapshot.compute.status || "unknown",
                "认证": snapshot.session.authenticated ? "ok" : "pending",
                "WebSocket": snapshot.websocket.label,
                "2FA状态": snapshot.auth.label,
                "消息数": String(snapshot.websocket.message_count || 0),
                "Tick数": String(snapshot.total_ticks || 0),
                "引擎就绪": `${snapshot.compute.ready_engines || 0}/${snapshot.compute.total_engines || 0}`,
                "盘前预热": buildWarmupProgressLabel(snapshot),
                "最新5m": snapshot.latest_bar.label,
                "当前Bar桶": snapshot.bar_bucket.label,
                "指标状态": getIndicatorStatusLabel(snapshot, freshnessWindow),
                "今日概况": `bars ${snapshot.today.bars} / ind ${snapshot.today.indicators} / sig ${snapshot.today.signals} / ord ${snapshot.today.orders}`,
                "实时账户": snapshot.account.ok
                    ? `pos ${snapshot.account.positions} / open ${snapshot.account.open_orders} / netliq ${snapshot.account.net_liquidation.toFixed(2)}`
                    : "unavailable",
                "目标池": `${snapshot.today.targets} targets / active ${snapshot.active_target_count || 0}`,
                "交易开关": snapshot.trading_enabled ? "true" : "false",
            }
            if (snapshot.auth.mode) detail["验证模式"] = snapshot.auth.mode
            if (snapshot.auth.last_result) detail["2FA反馈"] = snapshot.auth.last_result
            if (snapshot.auth.last_error) detail["2FA异常"] = snapshot.auth.last_error

            if (startupGraceActive) {
                detail["启动窗口"] = buildStartupGraceLabel(snapshot)
            }

            detail["摘要模式"] = assessment.kind === "healthy"
                ? "状态摘要 / 已合并心跳"
                : (assessment.kind === "healthy_paused"
                    ? "状态摘要 / 观察模式"
                    : (assessment.kind === "watch" ? "状态摘要 / 待观察" : "状态摘要 / 需关注"))

            const title = assessment.kind === "watch"
                ? "IBKR 系统状态摘要（待观察）"
                : (level === "warning" ? "IBKR 系统状态摘要（需关注）" : "IBKR 系统状态摘要")
            const notified = feishuSystem.notifySystemEvent("status_change", level, "pb", title, detail, environment)
            writeSystemEvent("status_change", level, "pb", title, detail, environment, notified)
            console.log(`${prefix} status reminder ${environment}: level=${level}, notified=${notified}`)
        } catch (err) {
            console.log(`${prefix} status reminder ${environment} error: ${err.message || err}`)
        }
    }
}

function buildScanSummaryUrl(environment, marketDate) {
    const runtimeEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    const dateToken = String(marketDate || "").trim()
    const consoleBaseUrl = publicUrls.getConsolePublicUrl(runtimeEnvironment)
    return `${consoleBaseUrl}/ibkr_screener.html?environment=${encodeURIComponent(runtimeEnvironment)}&tab=screener&view=current&date=${encodeURIComponent(dateToken)}&market_date=${encodeURIComponent(dateToken)}`
}

function buildScanSummaryLines(payload) {
    const items = Array.isArray(payload && payload.items) ? payload.items.slice(0, 5) : []
    if (!items.length) {
        const dailyScan = normalizeDailyScanState(payload && payload.daily_scan, { date: payload && payload.market_date })
        const lines = ["今日未筛出 candidate / active 标的。"]
        if (dailyScan.status === "failed") {
            lines.push(`日筛失败: ${dailyScan.last_error || "daily_scan_failed"}`)
        } else if (dailyScan.status === "completed") {
            lines.push(`原因汇总: ${buildDailyScanRejectionSummary(payload && payload.daily_scan, { date: payload && payload.market_date })}`)
            const examples = buildDailyScanRejectionExamples(payload && payload.daily_scan, { date: payload && payload.market_date })
            if (examples) {
                lines.push(`样例: ${examples}`)
            }
        }
        return lines.join("\n")
    }
    return items.map((row, index) => {
        const support = Array.isArray(row && row.operable_reasons) && row.operable_reasons.length
            ? row.operable_reasons.slice(0, 2).join(" / ")
            : "等待补充依据"
        return `${index + 1}. ${row.symbol} | ${row.status}/${toNumber(row.score, 0).toFixed(1)} | ${row.scan_reason || "--"} | ${support}`
    }).join("\n")
}

function buildScanSummaryCard(payload, environment) {
    const summary = payload && payload.summary ? payload.summary : {}
    const total = toNumber(summary.total, 0)
    const marketDate = String(payload && payload.market_date || "").trim()
    const title = total > 0 ? "IBKR 今日筛选结果" : "IBKR 今日筛选结果（未筛出标的）"
    const jumpUrl = buildScanSummaryUrl(environment, marketDate)
    return {
        config: { wide_screen_mode: true },
        header: {
            title: { tag: "plain_text", content: title },
            template: total > 0 ? "green" : "grey",
        },
        elements: [
            {
                tag: "markdown",
                content: `**交易日**: ${marketDate}\n**结果**: ${total} 条 · active ${toNumber(summary.active_count, 0)} · candidate ${toNumber(summary.candidate_count, 0)} · operable ${toNumber(summary.operable_count, 0)}`
            },
            {
                tag: "markdown",
                content: buildScanSummaryLines(payload)
            },
            {
                tag: "markdown",
                content: `🕐 美东 ${String(payload && payload.computed_at_us || "").trim() || "n/a"}`
            },
            {
                tag: "action",
                actions: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "查看当前标的榜" },
                    type: "default",
                    multi_url: {
                        url: jumpUrl,
                        pc_url: jumpUrl,
                        ios_url: jumpUrl,
                        android_url: jumpUrl,
                    }
                }]
            }
        ]
    }
}

function buildScanSummaryEventDetail(payload) {
    const summary = payload && payload.summary ? payload.summary : {}
    const items = Array.isArray(payload && payload.items) ? payload.items.slice(0, 5) : []
    const detail = {
        "交易日": String(payload && payload.market_date || ""),
        "总标的": String(toNumber(summary.total, 0)),
        "Active": String(toNumber(summary.active_count, 0)),
        "Candidate": String(toNumber(summary.candidate_count, 0)),
        "Operable": String(toNumber(summary.operable_count, 0)),
        "标的样例": items.length
            ? items.map((row) => `${row.symbol}(${row.status}/${toNumber(row.score, 0).toFixed(1)})`).join(", ")
            : "none",
    }
    const dailyScan = normalizeDailyScanState(payload && payload.daily_scan, { date: payload && payload.market_date })
    detail["日筛状态"] = buildDailyScanStatusLabel(payload && payload.daily_scan, { date: payload && payload.market_date })
    if (!items.length && dailyScan.status === "failed") {
        detail["未筛出原因"] = dailyScan.last_error || "daily_scan_failed"
    } else if (!items.length && dailyScan.status === "completed") {
        detail["未筛出原因"] = buildDailyScanRejectionSummary(payload && payload.daily_scan, { date: payload && payload.market_date })
    }
    return detail
}

function runDailyScanSummaryTick(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRScanSummary]"
    const feishuApp = require(`${__hooks}/lib/feishu_app.js`)
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const envUtils = require(`${__hooks}/lib/environment.js`)
    const runtimeModes = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const { buildTodayTargetPayload } = require(`${__hooks}/lib/ibkr_today_targets.js`)
    const times = getTimeStrings()
    const clock = getUsClock()

    if (clock.hour !== SCAN_SUMMARY_HOUR || clock.minute !== SCAN_SUMMARY_MINUTE) {
        return
    }

    const environments = listNotifyEnvironments(cronId).filter((environment) => environment === envUtils.LIVE_ENVIRONMENT)
    console.log(`${prefix} tick: time=${clock.time}, environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const state = getStateData(SCAN_SUMMARY_STATE_KEY, environment, times.date).data || {}
            if (String(state.sent_at || "").trim()) {
                continue
            }

            const notifyEnabled = runtimeModes.isEnabledConfigValue(
                envUtils.getConfigValue("status_notify_enabled", "TRUE", environment)
            )
            if (!notifyEnabled) {
                saveStateData(SCAN_SUMMARY_STATE_KEY, environment, times.date, {
                    skipped_at: times.us,
                    skipped_reason: "status_notify_disabled",
                })
                continue
            }

            const payload = buildTodayTargetPayload({
                environment: environment,
                marketDate: times.date,
            })
            const title = toNumber(payload && payload.summary && payload.summary.total, 0) > 0
                ? "IBKR 今日筛选结果"
                : "IBKR 今日筛选结果（未筛出标的）"
            const card = buildScanSummaryCard(payload, environment)
            const result = feishuApp.sendMessageDetailed(
                "interactive",
                card,
                feishuSystem.getSystemChatId(environment),
                "chat_id",
                environment
            )
            const notified = !!(result && result.success && !result.suppressed)
            writeSystemEvent("scan_summary", "info", "pb", title, buildScanSummaryEventDetail(payload), environment, notified)

            if (notified) {
                saveStateData(SCAN_SUMMARY_STATE_KEY, environment, times.date, {
                    sent_at: times.us,
                    message_id: String(result.message_id || ""),
                    market_date: String(payload.market_date || times.date),
                    total: toNumber(payload.summary && payload.summary.total, 0),
                    active_count: toNumber(payload.summary && payload.summary.active_count, 0),
                    candidate_count: toNumber(payload.summary && payload.summary.candidate_count, 0),
                    operable_count: toNumber(payload.summary && payload.summary.operable_count, 0),
                })
            } else {
                saveStateData(SCAN_SUMMARY_STATE_KEY, environment, times.date, {
                    error_at: times.us,
                    error: String(result && result.error || "send_failed"),
                })
            }

            console.log(`${prefix} ${environment}: notified=${notified}, total=${toNumber(payload.summary && payload.summary.total, 0)}`)
        } catch (err) {
            console.log(`${prefix} ${environment} error: ${err.message || err}`)
        }
    }
}

function runDailyOpenReminderTick(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRSystemNotify]"
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const clock = getUsClock()

    if (clock.hour !== MARKET_OPEN_REMINDER_HOUR || clock.minute !== MARKET_OPEN_REMINDER_MINUTE) {
        return
    }

    const environments = listNotifyEnvironments(cronId)
    console.log(`${prefix} open reminder tick: time=${clock.time}, environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const state = getStateData(DAILY_REMINDER_STATE_KEY, environment, times.date).data || {}
            if (String(state.open_sent_at || "").trim()) {
                continue
            }

            const snapshot = buildStatusSnapshot(environment, times)
            const startupGraceActive = isStartupGraceActive(snapshot)
            const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow)
            const marketSession = classifyMarketSession(snapshot, times, clock)
            const detail = buildDailyOpenReminderDetail(
                snapshot,
                assessment,
                marketSession,
                freshnessWindow,
                times,
                loadTodayEventCounts(environment, times)
            )
            if (startupGraceActive) {
                detail["启动保护"] = buildStartupGraceLabel(snapshot)
            }
            const title = marketSession.open_title
            const level = marketSession.kind === "closed"
                ? "info"
                : (marketSession.reason === "daily_scan_failed" ? "warning" : assessment.level)
            const notified = feishuSystem.notifySystemEvent("status_change", level, "pb", title, detail, environment, {
                target_chat: "system",
                template_level_override: "info",
            })
            writeSystemEvent("status_change", level, "pb", title, detail, environment, notified)
            saveStateData(DAILY_REMINDER_STATE_KEY, environment, times.date, {
                open_sent_at: times.us,
                open_title: title,
                open_market_kind: marketSession.kind,
                open_market_reason: marketSession.reason,
            })
            console.log(`${prefix} open reminder ${environment}: level=${level}, notified=${notified}`)
        } catch (err) {
            console.log(`${prefix} open reminder ${environment} error: ${err.message || err}`)
        }
    }
}

function runDailyCloseSummaryTick(logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRSystemNotify]"
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const times = getTimeStrings()
    const clock = getUsClock()

    if (clock.hour !== MARKET_CLOSE_REMINDER_HOUR || clock.minute !== MARKET_CLOSE_REMINDER_MINUTE) {
        return
    }

    const environments = listNotifyEnvironments(cronId)
    console.log(`${prefix} close summary tick: time=${clock.time}, environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const state = getStateData(DAILY_REMINDER_STATE_KEY, environment, times.date).data || {}
            if (String(state.close_sent_at || "").trim()) {
                continue
            }

            const snapshot = buildStatusSnapshot(environment, times)
            const startupGraceActive = isStartupGraceActive(snapshot)
            const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow)
            const marketSession = classifyMarketSession(snapshot, times, clock)
            const detail = buildDailyCloseSummaryDetail(
                snapshot,
                assessment,
                marketSession,
                freshnessWindow,
                times,
                loadTodayEventCounts(environment, times)
            )
            const title = marketSession.close_title
            const notified = feishuSystem.notifySystemEvent("daily_report", "info", "pb", title, detail, environment)
            writeSystemEvent("daily_report", "info", "pb", title, detail, environment, notified)
            saveStateData(DAILY_REMINDER_STATE_KEY, environment, times.date, {
                close_sent_at: times.us,
                close_title: title,
                close_market_kind: marketSession.kind,
                close_market_reason: marketSession.reason,
            })
            console.log(`${prefix} close summary ${environment}: notified=${notified}`)
        } catch (err) {
            console.log(`${prefix} close summary ${environment} error: ${err.message || err}`)
        }
    }
}

module.exports = {
    buildDataFreshnessWindow,
    buildStatusAssessment,
    buildDailyOpenReminderDetail,
    buildDailyScanRejectionExamples,
    buildDailyScanRejectionSummary,
    buildDailyScanStatusLabel,
    classifyMarketSession,
    hasHeartbeatIssue,
    listDataHealthProblems,
    normalizeDailyScanState,
    runSystemHeartbeatTick,
    runSystemStatusReminderTick,
    runDailyScanSummaryTick,
    runDailyOpenReminderTick,
    runDailyCloseSummaryTick,
}
