var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)
var timeUtils = require(`${__hooks}/lib/time_utils.js`)
var systemEvents = require(`${__hooks}/lib/system_events.js`)

var STARTUP_STATE_KEY = "ibkr_runtime_startup"
var STARTUP_STATE_DATE = "global"
var STARTUP_CHAT_ID = "oc_cc5d0a950797b1c2c010953e14bceeff"
var DEFAULT_PB_PUBLIC_URL = "https://pb.lzw-glory.top"

var STEP_ORDER = [
    "service_boot",
    "card_ready",
    "manual_trigger",
    "manual_confirm",
    "runtime_resume",
    "health_check",
]

var STEP_LABELS = {
    service_boot: "服务已拉起",
    card_ready: "已准备 2FA 卡片",
    manual_trigger: "已在飞书手动触发 2FA",
    manual_confirm: "已完成当前 2FA 验证",
    runtime_resume: "鉴权成功并恢复 Runtime",
    health_check: "健康检查通过",
}

var LEGACY_STEP_KEY_MAP = {
    gateway: "service_boot",
    subscriptions: "runtime_resume",
    core_threads: "runtime_resume",
    warmup: "runtime_resume",
    trading_gate: "health_check",
}

var STEP_STATUS = {
    pending: { icon: "⬜", text: "待开始" },
    running: { icon: "⏳", text: "进行中" },
    waiting: { icon: "⏳", text: "等待中" },
    done: { icon: "✅", text: "已完成" },
    failed: { icon: "❌", text: "失败" },
    skipped: { icon: "➖", text: "非阻塞" },
}

function getStartupChatId(environment) {
    return String(envUtils.getConfigValue("system_startup_chat_id", STARTUP_CHAT_ID, environment) || STARTUP_CHAT_ID).trim() || STARTUP_CHAT_ID
}

function padNumber(value, width) {
    var text = String(Math.max(0, parseInt(value, 10) || 0))
    while (text.length < width) {
        text = "0" + text
    }
    return text
}

function formatStartupLabelTimestamp(timestamp) {
    var text = String(timestamp || "").trim()
    if (!text) return ""
    var compact = text.replace(/[^0-9]/g, "")
    if (compact.length >= 14) return compact.slice(0, 14)
    return ""
}

function buildStartupLabel(environment, sequence, startedAt) {
    var env = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT).toUpperCase()
    var compactTs = formatStartupLabelTimestamp(startedAt)
    if (!compactTs) {
        compactTs = formatStartupLabelTimestamp(timeUtils.getTimeStrings().us) || String(Date.now())
    }
    return env + "-" + compactTs.slice(0, 8) + "-" + compactTs.slice(8, 14) + "-" + padNumber(sequence, 3)
}

function normalizeStepStatus(value) {
    var text = String(value || "").trim().toLowerCase()
    if (!text) return "pending"
    if (STEP_STATUS[text]) return text
    if (text === "complete" || text === "completed" || text === "success" || text === "ok") return "done"
    if (text === "in_progress" || text === "active") return "running"
    if (text === "blocked" || text === "manual" || text === "manual_action") return "waiting"
    if (text === "error") return "failed"
    if (text === "background") return "skipped"
    return "pending"
}

function defaultSteps() {
    var steps = {}
    for (var i = 0; i < STEP_ORDER.length; i++) {
        var key = STEP_ORDER[i]
        steps[key] = {
            key: key,
            label: STEP_LABELS[key],
            status: "pending",
            detail: "",
        }
    }
    return steps
}

function applyStepPatch(steps, key, patch) {
    if (!steps[key] || !patch || typeof patch !== "object") return
    steps[key] = {
        key: key,
        label: String(patch.label || steps[key].label || STEP_LABELS[key] || key),
        status: normalizeStepStatus(patch.status || steps[key].status),
        detail: String(patch.detail != null ? patch.detail : steps[key].detail || ""),
    }
}

function applyLegacyAuthPatch(steps, patch, context) {
    if (!patch || typeof patch !== "object") return
    var status = normalizeStepStatus(patch.status)
    var detail = String(patch.detail || "")
    var triggerLogin = context && context.trigger_login === true

    applyStepPatch(steps, "service_boot", {
        status: "done",
        detail: steps.service_boot && steps.service_boot.detail ? steps.service_boot.detail : "Gateway 已启动并可访问。",
    })

    if (status === "waiting") {
        applyStepPatch(steps, "card_ready", {
            status: "done",
            detail: detail || "已把 2FA 卡片准备好，等待人工点击开始验证。",
        })
        applyStepPatch(steps, "manual_trigger", {
            status: "waiting",
            detail: "点击当前启动卡片下方“开始 2FA 验证”。",
        })
        return
    }

    if (status === "running") {
        if (triggerLogin) {
            applyStepPatch(steps, "card_ready", {
                status: "done",
                detail: "当前轮次已使用现有卡片或同一入口继续推进。",
            })
            applyStepPatch(steps, "manual_trigger", {
                status: "done",
                detail: "当前轮次已手动触发，不会自动补发新的 Push。",
            })
            applyStepPatch(steps, "manual_confirm", {
                status: "waiting",
                detail: detail || "等待手机确认或完成当前响应码验证。",
            })
            return
        }

        applyStepPatch(steps, "card_ready", {
            status: "running",
            detail: detail || "正在确认是否需要进入手动 2FA。",
        })
        return
    }

    if (status === "done") {
        applyStepPatch(steps, "card_ready", {
            status: "done",
            detail: "2FA 卡片阶段已完成。",
        })
        applyStepPatch(steps, "manual_trigger", {
            status: "done",
            detail: "人工触发步骤已完成。",
        })
        applyStepPatch(steps, "manual_confirm", {
            status: "done",
            detail: detail || "当前 2FA 验证已完成。",
        })
        return
    }

    if (status === "failed") {
        applyStepPatch(steps, triggerLogin ? "manual_confirm" : "manual_trigger", {
            status: "failed",
            detail: detail || "当前轮次未完成，需要人工重新发起。",
        })
    }
}

function translatePatchSteps(existingSteps, patchSteps, context) {
    var merged = normalizeSteps(existingSteps)
    if (!patchSteps || typeof patchSteps !== "object") {
        return merged
    }

    var keys = Object.keys(patchSteps)
    for (var i = 0; i < keys.length; i++) {
        var rawKey = String(keys[i] || "").trim()
        if (!rawKey) continue
        var patch = patchSteps[rawKey]
        if (!patch || typeof patch !== "object") continue

        if (merged[rawKey]) {
            applyStepPatch(merged, rawKey, patch)
            continue
        }

        if (rawKey === "auth") {
            applyLegacyAuthPatch(merged, patch, context)
            continue
        }

        var mappedKey = LEGACY_STEP_KEY_MAP[rawKey]
        if (!mappedKey || !merged[mappedKey]) continue

        var normalizedStatus = normalizeStepStatus(patch.status)
        if (mappedKey === "runtime_resume" && normalizedStatus === "done") {
            applyStepPatch(merged, "runtime_resume", {
                status: "running",
                detail: String(patch.detail || "认证已恢复，正在恢复订阅、线程与 Warmup。"),
            })
            continue
        }

        applyStepPatch(merged, mappedKey, patch)
    }

    return merged
}

function buildCycleId(environment) {
    var env = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    return env + "_" + Date.now() + "_" + Math.floor(Math.random() * 1000000)
}

function getStateRecord(environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    try {
        var records = $app.findRecordsByFilter(
            "ibkr_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            "-updated",
            1,
            0,
            { k: STARTUP_STATE_KEY, d: STARTUP_STATE_DATE, env: runtimeEnvironment }
        ) || []
        return records.length ? records[0] : null
    } catch (_) {
        return null
    }
}

function safeJsonParse(value) {
    if (!value) return {}
    if (typeof value === "object") return value
    try {
        var parsed = JSON.parse(String(value || ""))
        return parsed && typeof parsed === "object" ? parsed : {}
    } catch (_) {
        return {}
    }
}

function normalizeFields(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {}
    var next = {}
    var keys = Object.keys(value)
    for (var i = 0; i < keys.length; i++) {
        var key = keys[i]
        next[key] = value[key]
    }
    return next
}

function normalizeSteps(rawSteps) {
    var merged = defaultSteps()
    var source = rawSteps && typeof rawSteps === "object" ? rawSteps : {}
    var keys = Object.keys(source)
    for (var i = 0; i < keys.length; i++) {
        var key = String(keys[i] || "").trim()
        if (!key) continue
        var patch = source[key]
        if (!patch || typeof patch !== "object") continue
        if (merged[key]) {
            applyStepPatch(merged, key, patch)
            continue
        }
        if (key === "auth") {
            applyLegacyAuthPatch(merged, patch, {})
            continue
        }
        var mappedKey = LEGACY_STEP_KEY_MAP[key]
        if (mappedKey && merged[mappedKey]) {
            applyStepPatch(merged, mappedKey, patch)
        }
    }
    return merged
}

function mergeSteps(existingSteps, patchSteps, context) {
    return translatePatchSteps(existingSteps, patchSteps, context)
}

function normalizeState(rawState, environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var state = rawState && typeof rawState === "object" ? rawState : {}
    return {
        cycle_id: String(state.cycle_id || ""),
        startup_seq: Math.max(0, parseInt(state.startup_seq, 10) || 0),
        startup_label: String(state.startup_label || ""),
        active: state.active === true,
        status: String(state.status || "idle").trim().toLowerCase() || "idle",
        title: String(state.title || "IBKR Runtime 启动中"),
        summary: String(state.summary || ""),
        current_step: String(state.current_step || ""),
        current_blocker: String(state.current_blocker || ""),
        operator_action: String(state.operator_action || ""),
        started_at: String(state.started_at || ""),
        finished_at: String(state.finished_at || ""),
        last_update_at: String(state.last_update_at || ""),
        reason: String(state.reason || ""),
        source: String(state.source || ""),
        trigger_login: state.trigger_login === true,
        runtime_phase: String(state.runtime_phase || ""),
        runtime_url: String(state.runtime_url || ""),
        startup_chat_id: String(state.startup_chat_id || ""),
        message_id: String(state.message_id || ""),
        last_delivery_mode: String(state.last_delivery_mode || ""),
        last_delivery_at: String(state.last_delivery_at || ""),
        last_delivery_error: String(state.last_delivery_error || ""),
        fields: normalizeFields(state.fields),
        steps: normalizeSteps(state.steps),
    }
}

function getStatePayload(environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var record = getStateRecord(runtimeEnvironment)
    var data = {}
    if (record) {
        try {
            var rawData = typeof record.getString === "function" ? (record.getString("data") || "") : record.get("data")
            data = safeJsonParse(rawData)
        } catch (_) {
            data = {}
        }
    }
    return {
        environment: runtimeEnvironment,
        date: STARTUP_STATE_DATE,
        record: record,
        data: normalizeState(data, runtimeEnvironment),
    }
}

function saveState(environment, nextState) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var payload = normalizeState(nextState, runtimeEnvironment)
    var statePayload = getStatePayload(runtimeEnvironment)
    var record = statePayload.record
    if (!record) {
        var collection = $app.findCollectionByNameOrId("ibkr_state")
        record = new Record(collection, {})
        record.set("state_key", STARTUP_STATE_KEY)
        record.set("date", STARTUP_STATE_DATE)
        record.set("environment", runtimeEnvironment)
    }
    record.set("data", payload)
    $app.save(record)
    return {
        environment: runtimeEnvironment,
        date: STARTUP_STATE_DATE,
        record: record,
        data: payload,
    }
}

function getStepMeta(status) {
    return STEP_STATUS[normalizeStepStatus(status)] || STEP_STATUS.pending
}

function mapStepKeyToDisplayKey(key) {
    var rawKey = String(key || "").trim()
    if (!rawKey) return ""
    if (STEP_LABELS[rawKey]) return rawKey
    if (rawKey === "auth") return "manual_trigger"
    return LEGACY_STEP_KEY_MAP[rawKey] || rawKey
}

function resolveCurrentStepLabel(state) {
    var key = mapStepKeyToDisplayKey(state.current_step)
    if (!key) return ""
    var steps = state.steps || {}
    if (steps[key] && steps[key].label) return String(steps[key].label)
    return STEP_LABELS[key] || key
}

function getPbPublicBaseUrl(environment) {
    return envUtils.getConfigValue("pb_public_url", DEFAULT_PB_PUBLIC_URL, environment).replace(/\/+$/, "")
}

function buildOpenButton(label, url, type) {
    return {
        tag: "button",
        type: type || "default",
        width: "fill",
        text: { tag: "plain_text", content: label },
        multi_url: {
            url: url,
            pc_url: url,
            ios_url: url,
            android_url: url,
        },
    }
}

function build2faTriggerButton(environment) {
    return {
        tag: "button",
        type: "primary",
        width: "fill",
        text: { tag: "plain_text", content: "开始 2FA 验证" },
        action_type: "request",
        url: getPbPublicBaseUrl(environment) + "/webhook/feishu/callback",
        value: {
            action: "ibkr_2fa_start",
            environment: environment,
            force_restart: false,
        },
    }
}

function buildChecklistMarkdown(steps) {
    var lines = []
    for (var i = 0; i < STEP_ORDER.length; i++) {
        var key = STEP_ORDER[i]
        var step = steps[key] || {}
        var meta = getStepMeta(step.status)
        var line = meta.icon + " " + String(step.label || STEP_LABELS[key] || key)
        if (step.detail) {
            line += "  \n" + String(step.detail)
        }
        lines.push(line)
    }
    return lines.join("\n")
}

function buildContextMarkdown(state) {
    var lines = [
        "**启动编号**: " + (state.startup_label || "-"),
        "**内部轮次**: " + (state.cycle_id || "-"),
        "**环境**: " + envUtils.getEnvironmentTag(state.environment || ""),
        "**最近更新时间**: " + (state.last_update_at || "-"),
    ]
    if (state.reason) lines.push("**启动原因**: " + state.reason)
    if (state.source) lines.push("**启动来源**: " + state.source)
    if (state.runtime_phase) lines.push("**Runtime 阶段**: " + state.runtime_phase)
    if (state.started_at) lines.push("**开始时间**: " + state.started_at)
    if (state.finished_at) lines.push("**完成时间**: " + state.finished_at)

    var fieldKeys = Object.keys(state.fields || {})
    for (var i = 0; i < fieldKeys.length; i++) {
        var key = fieldKeys[i]
        if (!key) continue
        lines.push("**" + key + "**: " + String(state.fields[key]))
    }
    return lines.join("\n")
}

function resolveHeader(state) {
    var status = String(state.status || "active").trim().toLowerCase()
    var hasBlockingFailure = false
    var steps = state.steps || {}
    for (var i = 0; i < STEP_ORDER.length; i++) {
        var key = STEP_ORDER[i]
        if (normalizeStepStatus((steps[key] || {}).status) === "failed") {
            hasBlockingFailure = true
            break
        }
    }

    if (status === "completed") {
        return { icon: "✅", template: "green" }
    }
    if (status === "failed") {
        return { icon: "❌", template: "red" }
    }
    if (status === "aborted") {
        return { icon: "⚠️", template: "yellow" }
    }
    if (hasBlockingFailure) {
        return { icon: "⚠️", template: "yellow" }
    }
    return { icon: "⏳", template: "blue" }
}

function buildStartupCard(state, environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || state.environment || "", envUtils.LIVE_ENVIRONMENT)
    var normalized = normalizeState(state, runtimeEnvironment)
    normalized.environment = runtimeEnvironment

    var header = resolveHeader(normalized)
    var title = header.icon + " " + envUtils.labelTitleWithEnvironment(normalized.title || "IBKR Runtime 启动中", runtimeEnvironment)
    var publicBaseUrl = getPbPublicBaseUrl(runtimeEnvironment)
    var runtimeUrl = normalized.runtime_url || (publicBaseUrl + "/ibkr_runtime.html?environment=" + encodeURIComponent(runtimeEnvironment))
    var systemUrl = publicBaseUrl + "/ibkr_system.html?environment=" + encodeURIComponent(runtimeEnvironment)
    var currentStepLabel = resolveCurrentStepLabel(normalized) || "-"
    var manualTriggerStep = normalized.steps && normalized.steps.manual_trigger ? normalized.steps.manual_trigger : {}
    var manualTriggerStatus = normalizeStepStatus(manualTriggerStep.status)
    var show2faTrigger = normalized.active === true
        && (
            mapStepKeyToDisplayKey(normalized.current_step) === "manual_trigger"
            || manualTriggerStatus === "waiting"
            || manualTriggerStatus === "failed"
        )

    var summaryLines = [
        "**启动编号**: " + (normalized.startup_label || "-"),
        "**当前阶段**: " + currentStepLabel,
        "**当前卡点**: " + (normalized.current_blocker || normalized.summary || "-"),
        "**下一步**: " + (normalized.operator_action || "等待系统继续推进"),
    ]

    var elements = [
        {
            tag: "markdown",
            content: summaryLines.join("\n")
        },
    ]

    if (show2faTrigger) {
        elements.push(
            {
                tag: "markdown",
                content: "**当前不会自动发送新的 Push，需人工点击下方“开始 2FA 验证”触发。**"
            },
            {
                tag: "column_set",
                columns: [
                    {
                        tag: "column",
                        width: "weighted",
                        weight: 1,
                        elements: [build2faTriggerButton(runtimeEnvironment)],
                    },
                ],
            }
        )
    }

    elements.push(
        { tag: "hr" },
        {
            tag: "markdown",
            content: buildChecklistMarkdown(normalized.steps || {})
        },
        { tag: "hr" },
        {
            tag: "markdown",
            content: buildContextMarkdown(normalized)
        },
        { tag: "hr" },
        {
            tag: "column_set",
            columns: [
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [buildOpenButton("查看 Runtime", runtimeUrl, "primary")],
                },
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [buildOpenButton("查看 System", systemUrl, "default")],
                },
            ],
        },
    )

    return {
        schema: "2.0",
        config: { update_multi: true, wide_screen_mode: true },
        header: {
            title: {
                tag: "plain_text",
                content: title,
            },
            template: header.template,
        },
        body: {
            direction: "vertical",
            elements: elements,
        },
    }
}

function deliverCard(savedState) {
    var state = normalizeState(savedState.data || {}, savedState.environment)
    var card = buildStartupCard(state, savedState.environment)
    var messageId = String(state.message_id || "").trim()
    var deliveryMode = messageId ? "update" : "send"
    var result = null

    if (messageId) {
        result = feishuApp.updateMessageCard(messageId, card, savedState.environment)
        if (!result || result.success !== true) {
            deliveryMode = "replace"
            result = feishuApp.sendMessageDetailed("interactive", card, getStartupChatId(savedState.environment), "chat_id", savedState.environment)
        }
    } else {
        result = feishuApp.sendMessageDetailed("interactive", card, getStartupChatId(savedState.environment), "chat_id", savedState.environment)
    }

    if (result && result.success) {
        state.message_id = String(result.message_id || messageId || "")
        state.last_delivery_mode = deliveryMode
        state.last_delivery_at = timeUtils.getTimeStrings().us
        state.last_delivery_error = ""
        savedState = saveState(savedState.environment, state)
        console.log("[FeishuStartup] card delivered:", deliveryMode, savedState.environment, state.message_id || "-")
    } else {
        state.last_delivery_mode = deliveryMode
        state.last_delivery_error = String((result && result.error) || "send_failed")
        savedState = saveState(savedState.environment, state)
        console.log("[FeishuStartup] card delivery failed:", deliveryMode, savedState.environment, state.last_delivery_error)
    }

    return {
        ok: !!(result && result.success),
        environment: savedState.environment,
        date: savedState.date,
        message_id: String((result && result.message_id) || state.message_id || ""),
        card: card,
        data: state,
        result: result || { success: false, error: "empty_result" },
    }
}

function shouldCreateCycle(action, state, createIfMissing) {
    if (action === "begin") return true
    if (state.active) return false
    return createIfMissing === true
}

function updateArchivedCycleMessage(state, environment, replacementLabel) {
    var previous = normalizeState(state, environment)
    var messageId = String(previous.message_id || "").trim()
    if (!messageId) return

    var times = timeUtils.getTimeStrings()
    previous.active = false
    previous.status = "aborted"
    previous.summary = "当前启动轮次已被新的启动编号取代，请改看最新卡片。"
    previous.current_blocker = replacementLabel
        ? ("当前轮次已终止，请改看新的启动编号 " + replacementLabel)
        : "当前轮次已终止，请改看最新启动卡片。"
    previous.operator_action = replacementLabel
        ? ("改看新的启动卡片: " + replacementLabel)
        : "改看最新启动卡片。"
    previous.finished_at = previous.finished_at || times.us
    previous.last_update_at = times.us

    if (previous.current_step) {
        var currentStepKey = mapStepKeyToDisplayKey(previous.current_step)
        if (currentStepKey && previous.steps && previous.steps[currentStepKey]) {
            applyStepPatch(previous.steps, currentStepKey, {
                status: "failed",
                detail: replacementLabel
                    ? ("当前轮次已终止，请改看新的启动编号 " + replacementLabel)
                    : "当前轮次已终止，请改看最新启动卡片。",
            })
        }
    }

    try {
        feishuApp.updateMessageCard(messageId, buildStartupCard(previous, environment), environment)
    } catch (err) {
        console.log("[FeishuStartup] archive previous cycle failed:", environment, messageId, err && err.message ? err.message : String(err))
    }
}

function syncStartupProgress(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var action = String(opts.action || "update").trim().toLowerCase() || "update"
    var statePayload = getStatePayload(runtimeEnvironment)
    var current = statePayload.data || normalizeState({}, runtimeEnvironment)
    var times = timeUtils.getTimeStrings()
    var createIfMissing = opts.create_if_missing === true
    var eventSource = String(opts.event_source || opts.source || "ibkr_compute").trim() || "ibkr_compute"
    var next = normalizeState(current, runtimeEnvironment)
    var nextStartupSeq = current.startup_seq || 0
    var nextStartupLabel = current.startup_label || ""
    var startupChatId = getStartupChatId(runtimeEnvironment)

    if (shouldCreateCycle(action, current, createIfMissing)) {
        nextStartupSeq = Math.max(0, parseInt(current.startup_seq, 10) || 0) + 1
        nextStartupLabel = buildStartupLabel(runtimeEnvironment, nextStartupSeq, times.us)
        if (current.active && current.message_id) {
            updateArchivedCycleMessage(current, runtimeEnvironment, nextStartupLabel)
        }
        next.cycle_id = buildCycleId(runtimeEnvironment)
        next.startup_seq = nextStartupSeq
        next.startup_label = nextStartupLabel
        next.active = true
        next.status = "active"
        next.started_at = times.us
        next.finished_at = ""
        next.startup_chat_id = startupChatId
        next.message_id = ""
        next.last_delivery_mode = ""
        next.last_delivery_at = ""
        next.last_delivery_error = ""
        next.fields = {}
        next.steps = defaultSteps()
    } else if (!current.cycle_id) {
        next.cycle_id = buildCycleId(runtimeEnvironment)
        next.startup_seq = Math.max(1, parseInt(current.startup_seq, 10) || 1)
        next.startup_label = current.startup_label || buildStartupLabel(runtimeEnvironment, next.startup_seq, current.started_at || times.us)
    }
    next.startup_chat_id = startupChatId

    if (opts.title != null) next.title = String(opts.title || next.title || "IBKR Runtime 启动中")
    if (opts.summary != null) next.summary = String(opts.summary || "")
    if (opts.current_step != null) next.current_step = String(opts.current_step || "")
    if (opts.current_blocker != null) next.current_blocker = String(opts.current_blocker || "")
    if (opts.operator_action != null) next.operator_action = String(opts.operator_action || "")
    if (opts.reason != null) next.reason = String(opts.reason || "")
    if (opts.source != null) next.source = String(opts.source || "")
    if (opts.runtime_phase != null) next.runtime_phase = String(opts.runtime_phase || "")
    if (opts.runtime_url != null) next.runtime_url = String(opts.runtime_url || "")
    if (opts.trigger_login != null) next.trigger_login = opts.trigger_login === true

    if (opts.fields && typeof opts.fields === "object") {
        next.fields = {
            ...(next.fields || {}),
            ...normalizeFields(opts.fields),
        }
    }
    if (opts.steps && typeof opts.steps === "object") {
        next.steps = mergeSteps(next.steps, opts.steps, {
            trigger_login: Object.prototype.hasOwnProperty.call(opts, "trigger_login")
                ? (opts.trigger_login === true)
                : (next.trigger_login === true),
        })
    }

    if (action === "begin" || action === "update" || action === "pause") {
        next.active = true
        next.status = String(opts.status || "active").trim().toLowerCase() || "active"
        if (!next.started_at) next.started_at = times.us
        next.finished_at = ""
    } else if (action === "complete") {
        next.active = false
        next.status = "completed"
        next.finished_at = times.us
    } else if (action === "fail") {
        next.active = false
        next.status = "failed"
        next.finished_at = times.us
    } else if (action === "abort" || action === "clear") {
        next.active = false
        next.status = "aborted"
        next.finished_at = times.us
    }

    if (action === "complete") {
        next.steps = mergeSteps(next.steps, {
            runtime_resume: {
                status: "done",
                detail: "认证恢复后 Runtime 已回到可运行状态。",
            },
            health_check: {
                status: "done",
                detail: "启动后健康检查已通过。",
            },
        }, { trigger_login: next.trigger_login === true })
    }

    next.last_update_at = times.us
    var saved = saveState(runtimeEnvironment, next)
    var delivered = deliverCard(saved)

    if (opts.record_event === true) {
        systemEvents.writeSystemEvent(
            String(opts.event_type || "status_change").trim() || "status_change",
            String(opts.level || "info").trim() || "info",
            eventSource,
            String(opts.event_title || next.title || "IBKR Runtime 启动中"),
            normalizeFields(opts.event_detail || {
                cycle_id: next.cycle_id,
                current_step: resolveCurrentStepLabel(next),
                current_blocker: next.current_blocker || next.summary || "",
                operator_action: next.operator_action || "",
            }),
            runtimeEnvironment,
            delivered.ok
        )
    }

    return {
        ok: delivered.ok,
        environment: runtimeEnvironment,
        date: STARTUP_STATE_DATE,
        cycle_id: next.cycle_id,
        startup_label: next.startup_label,
        message_id: delivered.message_id || next.message_id || "",
        state: normalizeState(saved.data || next, runtimeEnvironment),
        result: delivered.result || {},
    }
}

module.exports = {
    STARTUP_STATE_KEY: STARTUP_STATE_KEY,
    STARTUP_STATE_DATE: STARTUP_STATE_DATE,
    STEP_ORDER: STEP_ORDER,
    STEP_LABELS: STEP_LABELS,
    getStatePayload: getStatePayload,
    buildStartupCard: buildStartupCard,
    syncStartupProgress: syncStartupProgress,
}
