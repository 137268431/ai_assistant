/**
 * system_notify_scheduler.js
 * 健康检查通知 / 状态提醒的共享执行逻辑
 */

const HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
const DAILY_REMINDER_STATE_KEY = "system_notify_daily"
const HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const BAR_STALE_WARN_MIN = 10
const INDICATOR_STALE_WARN_MIN = 10
const COMPUTE_STARTUP_GRACE_MS = 3 * 60 * 1000
const RUNTIME_KEYS = ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
const SCAN_SUMMARY_STATE_KEY = "system_notify_scan_summary"
const SCAN_SUMMARY_HOUR = 5
const SCAN_SUMMARY_MINUTE = 55
const PB_HOST = "https://pb.lzw-glory.top"
const MARKET_OPEN_REMINDER_HOUR = 9
const MARKET_OPEN_REMINDER_MINUTE = 20
const MARKET_CLOSE_REMINDER_HOUR = 16
const MARKET_CLOSE_REMINDER_MINUTE = 5

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
        return {
            bar_time_ms: barTimeMs,
            symbol: String(row.get("symbol") || ""),
            us_time: String(row.get("us_time") || ""),
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
        return {
            bar_time_ms: toNumber(row.get("bar_time_ms"), 0),
            symbol: String(row.get("symbol") || ""),
            us_time: String(row.get("us_time") || ""),
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
        targets: countCollectionRows("ibkr_targets", "date = {:d} && environment = {:env}", { d: times.date, env: environment }),
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
                : 999
        )
        : 0
    const websocketConnected = Boolean(runtime.websocket && runtime.websocket.connected === true)
    const websocketReady = Boolean(runtime.websocket && runtime.websocket.ready === true)

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
        },
        session: {
            authenticated: Boolean(runtime.session && runtime.session.authenticated === true),
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

function getUsClock() {
    const now = new Date()
    const usOffset = -4 * 60
    const usTime = new Date(now.getTime() + usOffset * 60000)
    return {
        date: usTime.toISOString().slice(0, 10),
        time: usTime.toISOString().slice(11, 16),
        hour: usTime.getUTCHours(),
        minute: usTime.getUTCMinutes(),
        weekday: usTime.getUTCDay(),
    }
}

function matchesUsDate(value, dateToken) {
    return String(value || "").slice(0, 10) === String(dateToken || "")
}

function classifyMarketSession(snapshot, times, clock) {
    const weekday = Number(clock && clock.weekday)
    const isWeekend = weekday === 0 || weekday === 6
    const hasTargets = toNumber(snapshot && snapshot.today && snapshot.today.targets, 0) > 0
        || toNumber(snapshot && snapshot.active_target_count, 0) > 0
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
        kind: "closed_uncertain",
        label: "未检测到交易计划",
        open_title: "IBKR 非交易日提醒",
        open_summary: "当前未检测到今日目标池或盘中活动，可能为休市日，或盘前计划尚未生成；先按闭市提醒处理。",
        close_title: "IBKR 非交易日汇总",
        close_summary: "当前未检测到今日交易活动，按闭市日生成系统汇总。",
        reason: "no_targets_or_intraday_activity",
    }
}

function buildDataFreshnessWindow(snapshot, times, clock) {
    const marketSession = classifyMarketSession(snapshot, times, clock)
    const minuteOfDay = toNumber(clock && clock.hour, 0) * 60 + toNumber(clock && clock.minute, 0)
    if (marketSession.kind !== "trading") {
        return {
            required: false,
            reason: marketSession.reason || "market_closed",
            label: marketSession.label || "closed",
        }
    }
    if (minuteOfDay < (9 * 60 + 40)) {
        return {
            required: false,
            reason: "pre_open",
            label: "盘前宽限期",
        }
    }
    if (minuteOfDay > (16 * 60 + 15)) {
        return {
            required: false,
            reason: "post_close",
            label: "收盘后宽限期",
        }
    }
    return {
        required: true,
        reason: "regular_session",
        label: "盘中新鲜度检查",
    }
}

function buildDailyOpenReminderDetail(snapshot, assessment, marketSession, times, eventCounts) {
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
        "最新5m": snapshot.latest_bar.label,
        "指标状态": snapshot.latest_indicator.label,
        "今日概况": `bars ${snapshot.today.bars} / ind ${snapshot.today.indicators} / sig ${snapshot.today.signals} / ord ${snapshot.today.orders}`,
        "目标池": `${snapshot.today.targets} targets / active ${snapshot.active_target_count || 0}`,
        "系统事件": `${eventCounts.events} / error ${eventCounts.error_events}`,
        "交易开关": snapshot.trading_enabled ? "true" : "false",
    }
    if (snapshot.account.ok) {
        detail["实时账户"] = `pos ${snapshot.account.positions} / open ${snapshot.account.open_orders} / netliq ${snapshot.account.net_liquidation.toFixed(2)}`
    } else {
        detail["实时账户"] = "unavailable"
    }
    if (snapshot.compute.uptime_s > 0) detail["Compute Uptime"] = `${Math.round(snapshot.compute.uptime_s)}s`
    if (snapshot.runtime.warmup_phase) detail["Warmup"] = snapshot.runtime.warmup_phase
    if (snapshot.auth.mode) detail["验证模式"] = snapshot.auth.mode
    if (snapshot.auth.last_result) detail["2FA反馈"] = snapshot.auth.last_result
    if (snapshot.auth.last_error) detail["2FA异常"] = snapshot.auth.last_error
    return detail
}

function buildDailyCloseSummaryDetail(snapshot, assessment, marketSession, times, eventCounts) {
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
        "指标状态": snapshot.latest_indicator.label,
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
                snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
                || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
            )
        )
    )
}

function isStartupGraceActive(snapshot) {
    if (!snapshot || !snapshot.compute) return false
    const warmupPhase = String(snapshot.runtime && snapshot.runtime.warmup_phase || "").trim().toLowerCase()
    if (warmupPhase === "pending" || warmupPhase === "running") {
        return true
    }
    if (snapshot.compute.status === "running" && snapshot.runtime && snapshot.runtime.starting) {
        return true
    }
    const uptimeMs = toNumber(snapshot.compute.uptime_s, 0) * 1000
    return uptimeMs > 0 && uptimeMs < COMPUTE_STARTUP_GRACE_MS
}

function buildStartupGraceLabel(snapshot) {
    const uptimeS = toNumber(snapshot && snapshot.compute && snapshot.compute.uptime_s, 0)
    const warmupPhase = String(snapshot && snapshot.runtime && snapshot.runtime.warmup_phase || "").trim()
    const parts = []
    if (snapshot && snapshot.runtime && snapshot.runtime.starting) {
        parts.push("runtime starting")
    }
    if (uptimeS > 0) {
        parts.push(`uptime ${Math.round(uptimeS)}s`)
    }
    if (warmupPhase) {
        parts.push(`warmup ${warmupPhase}`)
    }
    if (!parts.length) {
        parts.push("startup grace active")
    }
    return `${parts.join(" / ")} / grace ${Math.round(COMPUTE_STARTUP_GRACE_MS / 1000)}s`
}

function buildStatusAssessment(snapshot, startupGraceActive, freshnessWindow) {
    const blockingIssues = []
    const watchItems = []
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)

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
    } else if (enforceFreshness && snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN) {
        if (startupGraceActive) {
            watchItems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        } else {
            blockingIssues.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        }
    }

    if (enforceFreshness && snapshot.latest_indicator.bar_time_ms <= 0) {
        if (startupGraceActive) {
            watchItems.push("指标流尚未建立")
        } else {
            blockingIssues.push("缺少最新指标")
        }
    } else if (enforceFreshness && snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
        if (startupGraceActive) {
            watchItems.push(`指标延迟 ${snapshot.latest_indicator.lag_min}m`)
        } else {
            blockingIssues.push(`指标延迟 ${snapshot.latest_indicator.lag_min}m`)
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
            summary: "当前系统状态正确，行情与执行链路正常；交易开关关闭，属于只观察模式。",
        }
    }

    return {
        level: "info",
        kind: "healthy",
        summary: "当前系统状态正确，Compute、认证、WebSocket、bars 与指标链路正常。",
    }
}

function listDataHealthProblems(snapshot, freshnessWindow) {
    const problems = []
    if (!snapshot) return problems
    const enforceFreshness = Boolean(freshnessWindow && freshnessWindow.required)

    if (enforceFreshness && snapshot.latest_bar.bar_time_ms <= 0) {
        problems.push("缺少最新 5m bars")
    } else if (enforceFreshness && snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN) {
        problems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
    }

    if (enforceFreshness && snapshot.latest_indicator.bar_time_ms <= 0) {
        problems.push("缺少最新指标")
    } else if (enforceFreshness && snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
        problems.push(`指标延迟 ${snapshot.latest_indicator.lag_min}m`)
    }

    if (!snapshot.session.authenticated) {
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

    if (!snapshot.session.authenticated) {
        actions.push("检查 IBKR 会话认证状态")
    }
    if (!snapshot.websocket.connected || !snapshot.websocket.ready) {
        actions.push("检查 WebSocket 连接与 IB Gateway 网关状态")
    }
    if (
        enforceFreshness && (
        snapshot.latest_bar.bar_time_ms <= 0
        || snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
        || snapshot.latest_indicator.bar_time_ms <= 0
        || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
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

function buildRecoveryDetail(snapshot, times, state, assessment) {
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
        "指标状态": snapshot.latest_indicator.label,
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
                || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
                )
            ) {
                const fingerprint = `data:${snapshot.latest_bar.bar_time_ms || 0}:${snapshot.latest_indicator.bar_time_ms || 0}:${snapshot.latest_bar.age_min || 0}:${snapshot.latest_indicator.lag_min || 0}`
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
                            "指标状态": snapshot.latest_indicator.label,
                            "WebSocket": snapshot.websocket.label,
                            "建议": buildDataHealthRecommendation(snapshot, freshnessWindow),
                        }
                        const notified = feishuSystem.notifySystemEvent("heartbeat", "warning", "pb", "IBKR 数据健康异常", detail, environment)
                        writeSystemEvent("heartbeat", "warning", "pb", "IBKR 数据健康异常", detail, environment, notified)
                        console.log(`${prefix} heartbeat ${environment}: data warning, notified=${notified}`)
                    }
                }
            } else {
                if (hadOutstandingIssue && isHealthyState) {
                    const title = buildRecoveryTitle(state)
                    const detail = buildRecoveryDetail(snapshot, times, state, assessment)
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
                "最新5m": snapshot.latest_bar.label,
                "指标状态": snapshot.latest_indicator.label,
                "今日概况": `bars ${snapshot.today.bars} / ind ${snapshot.today.indicators} / sig ${snapshot.today.signals} / ord ${snapshot.today.orders}`,
                "实时账户": snapshot.account.ok
                    ? `pos ${snapshot.account.positions} / open ${snapshot.account.open_orders} / netliq ${snapshot.account.net_liquidation.toFixed(2)}`
                    : "unavailable",
                "目标池": `${snapshot.today.targets} targets / active ${snapshot.active_target_count || 0}`,
                "交易开关": snapshot.trading_enabled ? "true" : "false",
            }
            if (snapshot.compute.uptime_s > 0) detail["Compute Uptime"] = `${Math.round(snapshot.compute.uptime_s)}s`
            if (snapshot.runtime.warmup_phase) detail["Warmup"] = snapshot.runtime.warmup_phase
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
    return `${PB_HOST}/ibkr_screener.html?environment=${encodeURIComponent(runtimeEnvironment)}&tab=targets&date=${encodeURIComponent(dateToken)}&market_date=${encodeURIComponent(dateToken)}`
}

function buildScanSummaryLines(payload) {
    const items = Array.isArray(payload && payload.items) ? payload.items.slice(0, 5) : []
    if (!items.length) {
        return "今日未筛出 candidate / active 标的。"
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
                    text: { tag: "plain_text", content: "查看今日 Targets" },
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
    return {
        "交易日": String(payload && payload.market_date || ""),
        "总标的": String(toNumber(summary.total, 0)),
        "Active": String(toNumber(summary.active_count, 0)),
        "Candidate": String(toNumber(summary.candidate_count, 0)),
        "Operable": String(toNumber(summary.operable_count, 0)),
        "标的样例": items.length
            ? items.map((row) => `${row.symbol}(${row.status}/${toNumber(row.score, 0).toFixed(1)})`).join(", ")
            : "none",
    }
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
                times,
                loadTodayEventCounts(environment, times)
            )
            const title = marketSession.kind === "trading" && assessment.level === "warning"
                ? `${marketSession.open_title}（需关注）`
                : marketSession.open_title
            const level = marketSession.kind === "trading" ? assessment.level : "info"
            const notified = feishuSystem.notifySystemEvent("status_change", level, "pb", title, detail, environment)
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
    runSystemHeartbeatTick,
    runSystemStatusReminderTick,
    runDailyScanSummaryTick,
    runDailyOpenReminderTick,
    runDailyCloseSummaryTick,
}
