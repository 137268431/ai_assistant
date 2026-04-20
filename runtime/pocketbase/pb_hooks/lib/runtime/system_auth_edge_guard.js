const AUTH_EDGE_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_EDGE_MONITOR_STATE_KEY = "system_auth_edge_monitor"
const AUTH_PENDING_ALERT_TRIGGER_MS = 15 * 60 * 1000
const AUTH_PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000
const AUTH_MONITOR_STATE_KEY = "system_auth_monitor"
const RUNTIME_KEYS = ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]
const usEasternTime = require(`${__hooks}/lib/runtime/us_eastern_time.js`)

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

function parseUsTimeMs(value) {
    return usEasternTime.parseUsEasternTimeMs(value)
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

function loadRuntimeSnapshot(environment) {
    const payload = fetchComputeJson("/ibkr/status", 8, environment)
    return {
        ok: payload.ok !== false,
        starting: payload.starting,
        runtime_phase: payload.runtime_phase || "",
        auth_recovery: payload.auth_recovery || {},
        session: payload.session || {},
        gateway: payload.gateway || {},
        websocket: payload.websocket || {},
        order_tracker: payload.order_tracker || {},
        warmup: payload.warmup || {},
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
        has_request: hasRequest,
        active: active,
        pending_too_long: pendingTooLong,
        cycle_id: String(state.cycle_id || ""),
        recovery_phase: String(state.recovery_phase || ""),
        recovery_reason: String(state.recovery_reason || ""),
        interruption_kind: String(state.interruption_kind || ""),
        last_runtime_authenticated_at: String(state.last_runtime_authenticated_at || ""),
        last_gateway_status_code: toNumber(state.last_gateway_status_code, 0),
        last_recovery_source: String(state.last_recovery_source || ""),
        age_min: ageMin,
        requested_at: String(state.requested_at || ""),
        triggered_at: String(state.triggered_at || ""),
        mode: String(state.mode || ""),
        challenge_code: String(state.challenge_code || ""),
        response_status: String(state.response_status || ""),
        response_received_at: String(state.response_received_at || ""),
        response_submitted_at: String(state.response_submitted_at || ""),
        response_rejected_at: String(state.response_rejected_at || ""),
        challenge_feedback: String(state.challenge_feedback || ""),
        operator_action: String(state.operator_action || ""),
        reset_recommended: state.reset_recommended === true,
        reset_reason: String(state.reset_reason || ""),
        last_result: String(state.last_result || ""),
        last_error: String(state.last_error || ""),
        reason: String(state.reason || ""),
        message: String(state.message || ""),
        page_url: String(state.page_url || ""),
        runtime_started: runtimeStarted,
        runtime_authenticated: runtimeAuthenticated,
        gateway_reachable: gatewayReachable,
        gateway_status_code: gatewayStatusCode,
        gateway_pid: toNumber(runtimeStatus.gateway && runtimeStatus.gateway.pid, 0),
        gateway_uptime_s: toNumber(runtimeStatus.gateway && runtimeStatus.gateway.uptime_s, 0),
    }
}

function isAuthActiveStatus(status) {
    return ["requested", "triggered", "waiting_confirm", "waiting_response"].indexOf(String(status || "").trim().toLowerCase()) !== -1
}

function isWaitingResponseIssueKind(kind) {
    return String(kind || "").trim().toLowerCase().indexOf("waiting_response") === 0
}

function isGatewayDownAuthIssue(auth) {
    if (!auth || typeof auth !== "object") return false
    const markers = [
        auth.reason,
        auth.recovery_reason,
        auth.interruption_kind,
    ]
    return markers.some((value) => String(value || "").trim().toLowerCase() === "gateway_down")
}

function isServerBootResumeRecovery(auth) {
    if (!auth || typeof auth !== "object") return false
    const interruptionKind = String(auth.interruption_kind || "").trim().toLowerCase()
    const recoveryPhase = String(auth.recovery_phase || "").trim().toLowerCase()
    const recoveryReason = String(auth.recovery_reason || "").trim().toLowerCase()
    const lastRecoverySource = String(auth.last_recovery_source || "").trim().toLowerCase()
    return (
        interruptionKind === "server_boot_resume"
        || recoveryPhase === "resume_waiting_manual"
        || (recoveryReason === "auto_restore" && lastRecoverySource === "server_boot")
    )
}

function buildWaitingResponseAdvice(auth) {
    if (!auth) return "优先打开 Runtime 页面确认当前轮次。"
    if (auth.reset_recommended) {
        return "当前旧 2FA / Session 状态很可能已失配。请直接去 Runtime 页面点“全量清空并重新验证”，不要继续围绕旧 Challenge / Response 重试。"
    }
    if (auth.response_status === "gateway_rejected") {
        return "Gateway 已拒绝当前 Response Code。请在 Runtime 页面核对当前 Challenge，用 IBKR App 重新生成 Response Code 后重提。"
    }
    if (auth.response_status === "submitted") {
        return "Response Code 已提交。先不要重新触发或重复提交；继续观察 Runtime 是否恢复认证。"
    }
    if (auth.response_status === "received") {
        return "Runtime 已收到 Response Code。先不要重复输入，优先观察是否自动推进到 submitted。"
    }
    if (auth.response_status === "submit_failed") {
        return "浏览器提交动作失败。请在 Runtime 页面重新提交当前 Challenge 对应的 Response Code，不要重新触发。"
    }
    return "不要再点旧确认消息。若接受 Challenge/Response，请按当前 Challenge 提交 Response Code；若不想继续旧轮次，请去 Runtime 页面点“全量清空并重新验证”。"
}

function buildWaitingResponseIssue(auth) {
    const feedback = String(auth && auth.challenge_feedback || "").trim()
    if (auth && auth.reset_recommended) {
        return {
            kind: "waiting_response_desynced",
            title: "IBKR 2FA 会话已失配，建议干净重开",
            summary: auth.response_status === "gateway_rejected"
                ? "Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。当前旧 2FA / Session 状态很可能已失配，请直接去 Runtime 页面执行“全量清空并重新验证”。"
                : "Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请直接去 Runtime 页面执行“全量清空并重新验证”。",
        }
    }
    if (auth && auth.response_status === "gateway_rejected") {
        return {
            kind: "waiting_response_rejected",
            title: "IBKR Gateway 已拒绝当前 Response Code",
            summary: feedback
                ? `Gateway 已返回失败反馈（${feedback}）。请核对当前 Challenge 后重新生成并提交。`
                : "Gateway 已明确拒绝当前 Response Code。请核对当前 Challenge 后重新生成并提交。",
        }
    }
    if (auth && auth.response_status === "submitted") {
        return {
            kind: "waiting_response_submitted",
            title: "IBKR Response Code 已提交，等待认证恢复",
            summary: "Runtime 已经把当前 Response Code 提交给 Gateway。先不要重复提交或重开，继续观察会话是否恢复认证。",
        }
    }
    if (auth && auth.response_status === "received") {
        return {
            kind: "waiting_response_received",
            title: "IBKR Response Code 已收到，等待浏览器提交",
            summary: "Runtime 已收到 Response Code，但浏览器提交流程尚未完成。先不要重复提交，继续观察当前轮次。",
        }
    }
    if (auth && auth.response_status === "submit_failed") {
        return {
            kind: "waiting_response_submit_failed",
            title: "IBKR Response Code 浏览器提交失败",
            summary: "Runtime 已收到 Response Code，但浏览器提交动作失败。请打开 Runtime 页面重新提交当前 Challenge 的 Response Code。",
        }
    }
    return {
        kind: "waiting_response",
        title: "IBKR 2FA 已切到 Challenge/Response",
        summary: "本轮 2FA 已不再是手机确认。不要再点旧的确认消息；如不想提交 Response Code，请去 Runtime 页面执行“全量清空并重新验证”。",
    }
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
        return buildWaitingResponseIssue(auth)
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

    if (isServerBootResumeRecovery(auth) && !runtimeAuthenticated) {
        return {
            kind: "server_boot_resume_pending",
            title: "IBKR 会话静默恢复中，暂不需要重新 2FA",
            summary: auth.recovery_phase === "resume_waiting_manual"
                ? "检测到 compute 重启后的静默恢复尚未自动成功；当前不会自动补发新的 2FA，如需立即恢复请去 Runtime 页面人工接管或手动重开。"
                : "检测到 compute 重启后正在静默复用现有 Gateway Session；当前不会自动触发新的 2FA，请先等待恢复窗口结束。",
        }
    }

    if (gatewayStatusCode === 401 && !runtimeAuthenticated) {
        if (isGatewayDownAuthIssue(auth)) {
            return {
                kind: "gateway_restart_reauth_required",
                title: "IBKR Gateway 已重启，需重新完成 2FA",
                summary: "检测到 Gateway 重启后会话尚未恢复认证（401），当前需要重新完成这一轮 2FA。",
            }
        }
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

function isOperational2faIssue(issue) {
    const kind = String(issue && issue.kind || "")
    return (
        kind === "requested"
        || kind === "waiting_confirm"
        || kind === "server_boot_resume_pending"
        || isWaitingResponseIssueKind(kind)
    )
}

function buildAuthImmediateFingerprint(auth, issue) {
    return JSON.stringify({
        issue_kind: issue && issue.kind || "",
        cycle_id: auth && auth.cycle_id || "",
        status: auth && auth.status || "",
        requested_at: auth && auth.requested_at || "",
        triggered_at: auth && auth.triggered_at || "",
        gateway_status_code: auth && auth.gateway_status_code || 0,
        recovery_reason: auth && auth.recovery_reason || "",
        interruption_kind: auth && auth.interruption_kind || "",
        runtime_started: auth && auth.runtime_started ? "yes" : "no",
        runtime_authenticated: auth && auth.runtime_authenticated ? "yes" : "no",
        challenge_code: auth && auth.challenge_code || "",
        response_status: auth && auth.response_status || "",
        response_rejected_at: auth && auth.response_rejected_at || "",
        challenge_feedback: auth && auth.challenge_feedback || "",
        operator_action: auth && auth.operator_action || "",
        reset_recommended: auth && auth.reset_recommended ? "yes" : "no",
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
        (issueKind === "waiting_confirm" || isWaitingResponseIssueKind(issueKind))
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
                "Gateway运行时长(s)": auth.gateway_uptime_s > 0 ? String(auth.gateway_uptime_s) : "n/a",
                "处理建议": isWaitingResponseIssueKind(issue.kind)
                    ? buildWaitingResponseAdvice(auth)
                    : issue.kind === "gateway_restart_reauth_required"
                        ? "本次是 Gateway 重启后的新轮次。优先处理当前 2FA，不要把它当成旧 Session 自然失效后反复重触发。"
                    : "优先打开 Runtime 页面确认当前状态；如果仍是 waiting_confirm，只在 IBKR App 点一次确认。",
            }
            if (auth.reason) detail["触发原因"] = auth.reason
            if (auth.recovery_reason) detail["恢复原因"] = auth.recovery_reason
            if (auth.interruption_kind) detail["中断类型"] = auth.interruption_kind
            if (auth.message) detail["最近反馈"] = auth.message
            if (auth.mode) detail["验证模式"] = auth.mode
            if (auth.challenge_code) detail["Challenge"] = auth.challenge_code
            if (auth.response_status) detail["响应状态"] = auth.response_status
            if (auth.response_received_at) detail["响应码收到"] = auth.response_received_at
            if (auth.response_submitted_at) detail["响应码提交"] = auth.response_submitted_at
            if (auth.response_rejected_at) detail["响应码拒绝"] = auth.response_rejected_at
            if (auth.challenge_feedback) detail["Gateway反馈"] = auth.challenge_feedback
            if (auth.operator_action) detail["建议动作"] = auth.operator_action
            if (auth.reset_recommended) detail["建议重开"] = "yes"
            if (auth.reset_reason) detail["重开原因"] = auth.reset_reason
            if (auth.requested_at) detail["请求时间"] = auth.requested_at
            if (auth.triggered_at) detail["触发时间"] = auth.triggered_at
            if (auth.last_runtime_authenticated_at) detail["上次认证成功"] = auth.last_runtime_authenticated_at
            if (auth.gateway_pid > 0) detail["GatewayPID"] = String(auth.gateway_pid)
            if (auth.page_url) detail["页面"] = auth.page_url
            if (auth.last_error) detail["最近错误"] = auth.last_error
            if (auth.last_result) detail["最近结果"] = auth.last_result

            const operationalOnly = isOperational2faIssue(issue)
            let notified = false
            if (operationalOnly) {
                writeSystemEvent("status_change", "info", "ibkr_compute", issue.title, detail, environment, false)
            } else {
                notified = feishuSystem.notifyWarning("ibkr_compute", issue.title, detail, environment)
                writeSystemEvent("alert", "warning", "ibkr_compute", issue.title, detail, environment, notified)
            }
            console.log(`[IBKRAuthEdgeGuard] ${environment}: issue=${issue.kind}, notified=${notified}, operational_only=${operationalOnly ? "yes" : "no"}`)
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
                response_rejected_at: auth.response_rejected_at || "",
                challenge_feedback: auth.challenge_feedback || "",
                operator_action: auth.operator_action || "",
                reset_recommended: auth.reset_recommended ? "yes" : "no",
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
                title = buildWaitingResponseIssue(auth).title
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
                "Gateway运行时长(s)": auth.gateway_uptime_s > 0 ? String(auth.gateway_uptime_s) : "n/a",
            }
            if (auth.recovery_reason) detail["恢复原因"] = auth.recovery_reason
            if (auth.interruption_kind) detail["中断类型"] = auth.interruption_kind
            if (auth.mode) detail["验证模式"] = auth.mode
            if (auth.challenge_code) detail["Challenge"] = auth.challenge_code
            if (auth.response_status) detail["响应状态"] = auth.response_status
            if (auth.response_received_at) detail["响应码收到"] = auth.response_received_at
            if (auth.response_submitted_at) detail["响应码提交"] = auth.response_submitted_at
            if (auth.response_rejected_at) detail["响应码拒绝"] = auth.response_rejected_at
            if (auth.challenge_feedback) detail["Gateway反馈"] = auth.challenge_feedback
            if (auth.operator_action) detail["建议动作"] = auth.operator_action
            if (auth.reset_recommended) detail["建议重开"] = "yes"
            if (auth.reset_reason) detail["重开原因"] = auth.reset_reason
            if (auth.triggered_at) detail["触发时间"] = auth.triggered_at
            if (auth.last_runtime_authenticated_at) detail["上次认证成功"] = auth.last_runtime_authenticated_at
            if (auth.gateway_pid > 0) detail["GatewayPID"] = String(auth.gateway_pid)
            if (auth.last_result) detail["最近反馈"] = auth.last_result
            if (auth.last_error) detail["最近错误"] = auth.last_error
            detail["处理建议"] = auth.status === "waiting_response"
                ? buildWaitingResponseAdvice(auth)
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
            const reason = String(state.reason || "").trim().toLowerCase()
            const lastPushMs = Number(state.last_request_push_ms || 0) || 0
            const reminderEligible = status === "requested" && ["manual_start", "startup", "weekly_reauth", "manual_gateway_restart"].indexOf(reason) !== -1
            if (!reminderEligible) {
                continue
            }
            if (lastPushMs > 0 && (nowMs - lastPushMs) < 55 * 60 * 1000) {
                continue
            }

            request2faApproval({
                environment: environment,
                reason: String(state.reason || "scheduled_2fa_check"),
                source: "pb_scheduler",
                message: reason === "weekly_reauth"
                    ? (
                        state.business_deadline_overdue
                            ? "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
                            : "本周重登仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。"
                    )
                    : reason === "manual_gateway_restart"
                        ? "网关重启后的新轮次仍停在待手动触发阶段，请只去当前启动卡片点击开始验证。"
                        : "启动验证仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证。",
                detail: {
                    "当前状态": status || "requested",
                    "周验证截止": state.business_deadline_cn
                        ? `${state.business_deadline_cn} 北京时间 / ${String(state.business_deadline_at || "-")} 美东`
                        : "n/a",
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

function runIbkrWeeklyReauthFollowupReminder() {
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getStatePayload, normalizeStateWithRuntime, request2faApproval } = require(`${__hooks}/lib/feishu_2fa.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)

    console.log(`[IBKRWeekly2FAFollowup] tick: environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const cronState = getPbCronToggleState("ibkr_weekly_reauth_followup", environment)
            if (!cronState.effective_enabled) {
                console.log(`[IBKRWeekly2FAFollowup] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
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
            const reason = String(state.reason || "").trim().toLowerCase()
            if (status !== "requested" || reason !== "weekly_reauth") {
                continue
            }

            request2faApproval({
                environment: environment,
                reason: "weekly_reauth",
                source: "pb_scheduler",
                message: state.business_deadline_overdue
                    ? "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
                    : "美国周一已进入盘前准备窗口。本周重登仍待手动开始，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。",
                detail: {
                    "提醒类型": "weekly_reauth_followup",
                    "最晚完成": state.business_deadline_cn
                        ? `${state.business_deadline_cn} 北京时间 / ${String(state.business_deadline_at || "-")} 美东`
                        : "美股周一盘前前",
                    "点击后时限": "180 秒",
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
            console.log(`[IBKRWeekly2FAFollowup] ${environment}: reminder requested`)
        } catch (err) {
            console.log(`[IBKRWeekly2FAFollowup] ${environment}: ${err.message || err}`)
        }
    }
}

function runIbkrWeeklyReauthReminder() {
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment, getTradingEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getStatePayload, normalizeStateWithRuntime, request2faApproval } = require(`${__hooks}/lib/feishu_2fa.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)

    console.log(`[IBKRWeekly2FA] tick: environments=${environments.join(",") || "-"}`)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        try {
            const cronState = getPbCronToggleState("ibkr_weekly_reauth_reminder", environment)
            if (!cronState.effective_enabled) {
                console.log(`[IBKRWeekly2FA] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
                continue
            }
            if (!getComputeEnabledForEnvironment(environment, RUNTIME_KEYS) && !getTradingEnabledForEnvironment(environment, RUNTIME_KEYS)) {
                continue
            }

            const runtimeStatus = loadRuntimeSnapshot(environment)
            const auth = loadAuthAttentionSummary(environment, runtimeStatus)
            if (auth.runtime_authenticated && auth.gateway_reachable && auth.gateway_status_code !== 401) {
                continue
            }

            const statePayload = getStatePayload(environment)
            const state = normalizeStateWithRuntime(statePayload.data || {}, runtimeStatus || {})
            request2faApproval({
                environment: environment,
                reason: "weekly_reauth",
                source: "pb_scheduler",
                message: "美国周一已开始，本周重登提醒已发出。你有空时再去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。点击开始后需在 180 秒内完成当前 2FA。",
                detail: {
                    "提醒类型": "weekly_reauth",
                    "最晚完成": state.business_deadline_cn
                        ? `${state.business_deadline_cn} 北京时间 / ${String(state.business_deadline_at || "-")} 美东`
                        : "美股周一盘前前",
                    "点击后时限": "180 秒",
                    "当前状态": String(state.status || "requested"),
                    "Runtime已启动": auth.runtime_started ? "yes" : "no",
                    "Session认证": auth.runtime_authenticated ? "yes" : "no",
                    "Gateway状态码": auth.gateway_status_code ? String(auth.gateway_status_code) : "n/a",
                },
                forceReset: false,
                forceNew: false,
            })
            console.log(`[IBKRWeekly2FA] ${environment}: reminder requested`)
        } catch (err) {
            console.log(`[IBKRWeekly2FA] ${environment}: ${err.message || err}`)
        }
    }
}

module.exports = {
    runIbkrAuthEdgeGuard,
    runIbkrAuthPendingGuard,
    runIbkrWeeklyReauthReminder,
    runIbkrWeeklyReauthFollowupReminder,
    runIbkr2faHourlyCheck,
}
