/**
 * system_notify_scheduler.js
 * 健康检查通知 / 状态提醒的共享执行逻辑
 */

const HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
const HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const BAR_STALE_WARN_MIN = 10
const INDICATOR_STALE_WARN_MIN = 10
const COMPUTE_STARTUP_GRACE_MS = 3 * 60 * 1000
const RUNTIME_KEYS = ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]

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
    try {
        const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputeInternalUrl(environment || "live", "http://127.0.0.1:5100")
        const resp = $http.send({ url: `${computeBaseUrl}${path}`, method: "GET", timeout: timeoutSeconds || 5 })
        if (resp.statusCode === 200) {
            const raw = typeof resp.raw === "string" ? resp.raw : String(resp.raw || "")
            return raw ? JSON.parse(raw) : {}
        }
        return { ok: false, status: "error", code: resp.statusCode }
    } catch (err) {
        return { ok: false, status: "offline", error: err.message || String(err) }
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

function hasHeartbeatIssue(snapshot) {
    return (
        snapshot.compute.status !== "running"
        || snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
        || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
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

function buildStatusAssessment(snapshot, startupGraceActive) {
    const blockingIssues = []
    const watchItems = []

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

    if (snapshot.latest_bar.bar_time_ms <= 0) {
        if (startupGraceActive) {
            watchItems.push("最新 5m bars 尚未建立")
        } else {
            blockingIssues.push("缺少最新 5m bars")
        }
    } else if (snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN) {
        if (startupGraceActive) {
            watchItems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        } else {
            blockingIssues.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
        }
    }

    if (snapshot.latest_indicator.bar_time_ms <= 0) {
        if (startupGraceActive) {
            watchItems.push("指标流尚未建立")
        } else {
            blockingIssues.push("缺少最新指标")
        }
    } else if (snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
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

function listDataHealthProblems(snapshot) {
    const problems = []
    if (!snapshot) return problems

    if (snapshot.latest_bar.bar_time_ms <= 0) {
        problems.push("缺少最新 5m bars")
    } else if (snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN) {
        problems.push(`最新 5m bars 偏旧 ${snapshot.latest_bar.age_min}m`)
    }

    if (snapshot.latest_indicator.bar_time_ms <= 0) {
        problems.push("缺少最新指标")
    } else if (snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN) {
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

function buildDataHealthRecommendation(snapshot) {
    const actions = []
    if (!snapshot) return "检查 compute / IBKR / PocketBase 链路状态"

    if (!snapshot.session.authenticated) {
        actions.push("检查 IBKR 会话认证状态")
    }
    if (!snapshot.websocket.connected || !snapshot.websocket.ready) {
        actions.push("检查 WebSocket 连接与 IB Gateway 网关状态")
    }
    if (
        snapshot.latest_bar.bar_time_ms <= 0
        || snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
        || snapshot.latest_indicator.bar_time_ms <= 0
        || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
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

function shouldMergeHeartbeatIntoSummary(snapshot, currentMinute, environment) {
    if (!isStatusSummaryMinute(currentMinute)) {
        return false
    }
    if (hasHeartbeatIssue(snapshot)) {
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

    console.log(`${prefix} heartbeat tick: minute=${currentMinute}, environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const snapshot = buildStatusSnapshot(environment, times)
            const state = getStateData(HEARTBEAT_STATE_KEY, environment, times.date).data || {}
            const hadOutstandingIssue = String(state.last_issue_hash || "").trim() !== ""
            const startupGraceActive = isStartupGraceActive(snapshot)
            const startupGraceLabel = startupGraceActive ? buildStartupGraceLabel(snapshot) : ""
            const mergeHeartbeatIntoSummary = shouldMergeHeartbeatIntoSummary(snapshot, currentMinute, environment)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive)
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
                snapshot.latest_bar.age_min > BAR_STALE_WARN_MIN
                || snapshot.latest_indicator.lag_min > INDICATOR_STALE_WARN_MIN
            ) {
                const fingerprint = `data:${snapshot.latest_bar.bar_time_ms || 0}:${snapshot.latest_indicator.bar_time_ms || 0}:${snapshot.latest_bar.age_min || 0}:${snapshot.latest_indicator.lag_min || 0}`
                const problemSummary = listDataHealthProblems(snapshot)
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
                            "建议": buildDataHealthRecommendation(snapshot),
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
    const environments = listNotifyEnvironments(cronId)

    console.log(`${prefix} status reminder tick: environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const snapshot = buildStatusSnapshot(environment, times)
            const startupGraceActive = isStartupGraceActive(snapshot)
            const assessment = buildStatusAssessment(snapshot, startupGraceActive)
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

module.exports = {
    runSystemHeartbeatTick,
    runSystemStatusReminderTick,
}
