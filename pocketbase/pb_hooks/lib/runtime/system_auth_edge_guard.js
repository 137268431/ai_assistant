const AUTH_EDGE_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_EDGE_MONITOR_STATE_KEY = "system_auth_edge_monitor"
const AUTH_PENDING_ALERT_TRIGGER_MS = 15 * 60 * 1000
const AUTH_PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_MONITOR_STATE_KEY = "system_auth_monitor"
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
        const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputeInternalUrl(environment || "live", "http://127.0.0.1:5100")
        const resp = $http.send({ url: `${computeBaseUrl}${path}`, method: "GET", timeout: timeoutSeconds || 5 })
        if (resp.statusCode === 200) {
            return parseHttpJson(resp)
        }
        return { ok: false, status: "error", code: resp.statusCode }
    } catch (err) {
        return { ok: false, status: "offline", error: err.message || String(err) }
    }
}

function loadRuntimeSnapshot(environment) {
    const payload = fetchComputeJson("/ibkr/status", 8, environment)
    return {
        ok: payload.ok !== false,
        starting: payload.starting,
        session: payload.session || {},
        gateway: payload.gateway || {},
        websocket: payload.websocket || {},
        order_tracker: payload.order_tracker || {},
    }
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
    const active = auth.active === true || isAuthActiveStatus(status)

    if (!gatewayReachable && !active) {
        return null
    }

    if (status === "waiting_response") {
        return {
            kind: "waiting_response",
            title: "IBKR 2FA 已切到 Challenge/Response",
            summary: "本轮 2FA 已不再是手机确认。不要再点旧的确认消息；如不想提交 Response Code，请去 Runtime 页面执行“全量清空并重新验证”。",
        }
    }

    if (status === "waiting_confirm") {
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

function runIbkrAuthEdgeGuard() {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const times = getTimeStrings()
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)
    const nowMs = Date.now()

    console.log(`[IBKRAuthEdgeGuard] tick: environments=${environments.join(",") || "-"} at=${times.us}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const cronState = getPbCronToggleState("ibkr_auth_edge_guard", environment)
            if (!cronState.effective_enabled) {
                console.log(`[IBKRAuthEdgeGuard] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
                continue
            }
            if (!getComputeEnabledForEnvironment(environment, RUNTIME_KEYS) && !getTradingEnabledForEnvironment(environment, RUNTIME_KEYS)) {
                continue
            }

            const runtime = loadRuntimeSnapshot(environment)
            const auth = loadAuthAttentionSummary(environment, runtime)
            const current = getStateData(AUTH_EDGE_MONITOR_STATE_KEY, environment, times.date).data || {}
            const nextState = {
                last_auth_scan_at: times.us,
                last_auth_status: auth.status || "",
                last_auth_age_min: auth.age_min || 0,
                last_runtime_authenticated: auth.runtime_authenticated ? "yes" : "no",
                last_gateway_status_code: auth.gateway_status_code || 0,
                last_auth_active: auth.active ? "yes" : "no",
                last_requested_at: auth.requested_at || "",
                last_triggered_at: auth.triggered_at || "",
                last_auth_cycle_id: auth.cycle_id || "",
            }
            const issue = buildAuthImmediateIssue(auth)

            if (!issue) {
                saveStateData(AUTH_EDGE_MONITOR_STATE_KEY, environment, times.date, {
                    ...nextState,
                    last_auth_issue_at: "",
                    last_auth_issue_kind: "",
                    last_auth_issue_title: "",
                    last_auth_issue_summary: "",
                    last_auth_edge_alert_ms: 0,
                    last_auth_edge_alert_hash: "",
                })
                continue
            }

            const fingerprint = buildAuthImmediateFingerprint(auth, issue)
            const shouldNotify = shouldNotifyAuthImmediateAlert(current, auth, issue, fingerprint, nowMs)
            if (!shouldNotify) {
                saveStateData(AUTH_EDGE_MONITOR_STATE_KEY, environment, times.date, {
                    ...nextState,
                    last_auth_issue_kind: issue.kind,
                    last_auth_issue_title: issue.title,
                    last_auth_issue_summary: issue.summary,
                })
                continue
            }

            const detail = {
                "异常结论": issue.summary,
                "检查时间": times.us,
                "2FA状态": auth.status || "requested",
                "恢复阶段": auth.recovery_phase || "idle",
                "轮次ID": auth.cycle_id || "-",
                "Session认证": auth.runtime_authenticated ? "yes" : "no",
                "Runtime已启动": auth.runtime_started ? "yes" : "no",
                "Gateway状态码": auth.gateway_status_code ? String(auth.gateway_status_code) : "n/a",
                "处理建议": issue.kind === "waiting_response"
                    ? "不要再点旧确认消息。若不接受 Challenge/Response，请去 Runtime 页面点“全量清空并重新验证”；若接受，则按当前 Challenge 提交 Response Code。"
                    : "优先打开 Runtime 页面确认当前状态；如果仍是 waiting_confirm，只在 IBKR App 点一次确认。",
            }
            if (auth.reason) detail["触发原因"] = auth.reason
            if (auth.message) detail["最近反馈"] = auth.message
            if (auth.mode) detail["验证模式"] = auth.mode
            if (auth.challenge_code) detail["Challenge"] = auth.challenge_code
            if (auth.response_status) detail["响应状态"] = auth.response_status
            if (auth.requested_at) detail["请求时间"] = auth.requested_at
            if (auth.triggered_at) detail["触发时间"] = auth.triggered_at
            if (auth.page_url) detail["页面"] = auth.page_url
            if (auth.last_error) detail["最近错误"] = auth.last_error
            if (auth.last_result) detail["最近结果"] = auth.last_result

            const notified = feishuSystem.notifyWarning("ibkr_compute", issue.title, detail, environment)
            writeSystemEvent("alert", "warning", "ibkr_compute", issue.title, detail, environment, notified)
            console.log(`[IBKRAuthEdgeGuard] ${environment}: issue=${issue.kind}, notified=${notified}`)
            saveStateData(AUTH_EDGE_MONITOR_STATE_KEY, environment, times.date, {
                ...nextState,
                last_auth_issue_at: times.us,
                last_auth_issue_kind: issue.kind,
                last_auth_issue_title: issue.title,
                last_auth_issue_summary: issue.summary,
                last_auth_edge_alert_ms: nowMs,
                last_auth_edge_alert_hash: fingerprint,
            })
        } catch (err) {
            console.log(`[IBKRAuthEdgeGuard] ${environment} error: ${err.message || err}`)
        }
    }
}

function runIbkrAuthPendingGuard() {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const times = getTimeStrings()
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)
    const nowMs = Date.now()

    console.log(`[IBKRAuthPendingGuard] tick: environments=${environments.join(",") || "-"} at=${times.us}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const cronState = getPbCronToggleState("ibkr_auth_pending_guard", environment)
            if (!cronState.effective_enabled) {
                console.log(`[IBKRAuthPendingGuard] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
                continue
            }
            if (!getComputeEnabledForEnvironment(environment, RUNTIME_KEYS) && !getTradingEnabledForEnvironment(environment, RUNTIME_KEYS)) {
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
                title = "IBKR 2FA 已卡在 Challenge/Response"
            } else if (auth.status === "requested" || auth.status === "triggered") {
                title = "IBKR 2FA 长时间未完成"
            }

            const detail = {
                "检查时间": times.us,
                "2FA状态": auth.status || "requested",
                "持续时间": `${auth.age_min || 0} 分钟`,
                "恢复阶段": auth.recovery_phase || "idle",
                "轮次ID": auth.cycle_id || "-",
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
            detail["处理建议"] = auth.status === "waiting_response"
                ? "这轮已经不是手机确认。若不接受 Challenge/Response，请直接去 Runtime 页面点“全量清空并重新验证”。"
                : "优先去 Runtime 页面确认当前轮次；如果仍是手机确认，只在 IBKR App 点一次确认。"

            const notified = feishuSystem.notifyWarning("ibkr_compute", title, detail, environment)
            writeSystemEvent("alert", "warning", "ibkr_compute", title, detail, environment, notified)
            console.log(`[IBKRAuthPendingGuard] ${environment}: title=${title}, notified=${notified}`)
            saveStateData(AUTH_MONITOR_STATE_KEY, environment, times.date, {
                ...nextState,
                last_auth_issue_at: times.us,
                last_auth_alert_ms: nowMs,
                last_auth_alert_hash: fingerprint,
            })
        } catch (err) {
            console.log(`[IBKRAuthPendingGuard] ${environment} error: ${err.message || err}`)
        }
    }
}

function runIbkr2faHourlyCheck() {
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getStatePayload, normalizeStateWithRuntime, request2faApproval } = require(`${__hooks}/lib/feishu_2fa.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)
    const nowMs = Date.now()

    console.log(`[IBKR2FAHourly] tick: environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const cronState = getPbCronToggleState("ibkr_2fa_hourly_check", environment)
            if (!cronState.effective_enabled) {
                console.log(`[IBKR2FAHourly] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
                continue
            }
            if (!getComputeEnabledForEnvironment(environment, RUNTIME_KEYS) && !getTradingEnabledForEnvironment(environment, RUNTIME_KEYS)) {
                continue
            }

            const runtimeStatus = loadRuntimeSnapshot(environment)
            const auth = loadAuthAttentionSummary(environment, runtimeStatus)
            const needsAuthAttention = auth.gateway_reachable && (!auth.runtime_authenticated || auth.gateway_status_code === 401 || !auth.runtime_started)
            if (!needsAuthAttention) {
                continue
            }

            const statePayload = getStatePayload(environment)
            const state = normalizeStateWithRuntime(statePayload.data || {}, runtimeStatus || {})
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
                    "Runtime已启动": auth.runtime_started ? "yes" : "no",
                    "Session认证": auth.runtime_authenticated ? "yes" : "no",
                    "Gateway状态码": auth.gateway_status_code ? String(auth.gateway_status_code) : "n/a",
                    "最近结果": String(state.last_result || ""),
                    "最近错误": String(state.last_error || ""),
                },
                forceReset: false,
                forceNew: false,
            })
            console.log(`[IBKR2FAHourly] ${environment}: reminder requested`)
        } catch (err) {
            console.log(`[IBKR2FAHourly] ${environment}: ${err.message || err}`)
        }
    }
}

module.exports = {
    runIbkrAuthEdgeGuard,
    runIbkrAuthPendingGuard,
    runIbkr2faHourlyCheck,
}
