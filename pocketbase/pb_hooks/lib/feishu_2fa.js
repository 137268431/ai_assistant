var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)
var timeUtils = require(`${__hooks}/lib/time_utils.js`)
var systemEvents = require(`${__hooks}/lib/system_events.js`)

var IBKR_2FA_STATE_KEY = "ibkr_2fa"
var PB_HOST = "https://pb.lzw-glory.top"
var CARD_UPDATE_COOLDOWN_MS = 15000
var REQUEST_RENOTIFY_COOLDOWN_MS = 900000
var DELIVERY_LOCK_TTL_MS = 20000
var ACTIVE_STATUSES = ["requested", "triggered", "waiting_confirm", "waiting_response"]
var TERMINAL_STATUSES = ["success", "timeout", "failed"]
var rootScope = typeof globalThis !== "undefined" ? globalThis : this
if (!rootScope.__IBKR_2FA_DELIVERY_LOCKS) {
    rootScope.__IBKR_2FA_DELIVERY_LOCKS = {}
}
var DELIVERY_LOCKS = rootScope.__IBKR_2FA_DELIVERY_LOCKS

var STATUS_CONFIG = {
    requested: {
        emoji: "🔐",
        title: "IBKR 2FA 待触发",
        template: "yellow",
        summary: "点击按钮后才会开始登录与 2FA 推送。",
        button: "开始 2FA 验证"
    },
    triggered: {
        emoji: "🚀",
        title: "IBKR 2FA 已触发",
        template: "blue",
        summary: "已开始启动登录流程，请稍候。",
        button: "重新触发"
    },
    waiting_confirm: {
        emoji: "📲",
        title: "IBKR 2FA 待确认",
        template: "yellow",
        summary: "请在 IBKR Mobile 上确认推送；如果后续切到 Challenge/Response，再按卡片提示改为输入 Response Code。",
        button: "重新触发"
    },
    waiting_response: {
        emoji: "🔢",
        title: "IBKR 2FA 待输入响应码",
        template: "orange",
        summary: "当前不是手机点确认，而是要在 App 输入 Challenge 生成 Response Code，再去 Runtime 页面提交。",
        button: "重新触发"
    },
    success: {
        emoji: "✅",
        title: "IBKR 2FA 验证成功",
        template: "green",
        summary: "Gateway 已恢复认证。",
        button: "再次验证"
    },
    timeout: {
        emoji: "⏰",
        title: "IBKR 2FA 等待超时",
        template: "red",
        summary: "未在等待窗口内完成确认，可点击按钮重试。",
        button: "重新触发"
    },
    failed: {
        emoji: "🚨",
        title: "IBKR 2FA 触发失败",
        template: "red",
        summary: "登录流程未成功完成，可点击按钮重试。",
        button: "重新触发"
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

function stringifyValue(value) {
    if (value == null) return "-"
    if (Array.isArray(value)) return value.join(", ") || "-"
    if (typeof value === "object") return JSON.stringify(value)
    var text = String(value)
    return text || "-"
}

function sanitizeResponseCode(value) {
    return String(value || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase()
}

function getModeLabel(mode) {
    var text = String(mode || "").trim().toLowerCase()
    if (text === "push_notification") return "Push Notification"
    if (text === "challenge_response") return "Challenge/Response"
    if (text === "success") return "Success"
    if (text === "login_form") return "Login Form"
    return text ? text : "-"
}

function toNumber(value, fallback) {
    var num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function isActiveStatus(status) {
    return ACTIVE_STATUSES.indexOf(String(status || "")) !== -1
}

function isTerminalStatus(status) {
    return TERMINAL_STATUSES.indexOf(String(status || "")) !== -1
}

function buildDeliveryFingerprint(stateData) {
    return JSON.stringify({
        status: stateData.status || "",
        mode: stateData.mode || "",
        challenge_code: stateData.challenge_code || "",
        response_status: stateData.response_status || "",
        reason: stateData.reason || "",
        message: stateData.message || "",
        last_result: stateData.last_result || "",
        last_error: stateData.last_error || "",
        requested_at: stateData.requested_at || "",
        triggered_at: stateData.triggered_at || "",
        result_at: stateData.result_at || "",
        detail: stateData.detail || {},
    })
}

function acquireDeliveryLock(lockKey) {
    var now = Date.now()
    var lockedUntil = toNumber(DELIVERY_LOCKS[lockKey], 0)
    if (lockedUntil > now) {
        return false
    }
    DELIVERY_LOCKS[lockKey] = now + DELIVERY_LOCK_TTL_MS
    return true
}

function releaseDeliveryLock(lockKey) {
    delete DELIVERY_LOCKS[lockKey]
}

function buildDetailMarkdown(detail) {
    if (!detail || typeof detail !== "object") return ""
    var keys = Object.keys(detail)
    if (keys.length === 0) return ""
    return keys.map(function(key) {
        return "**" + key + "**: " + stringifyValue(detail[key])
    }).join("\n")
}

function getStateRecord(environment, dateToken) {
    try {
        return $app.findFirstRecordByFilter(
            "ibkr_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: IBKR_2FA_STATE_KEY, d: dateToken, env: environment }
        )
    } catch (_) {
        return null
    }
}

function getStatePayload(environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var times = timeUtils.getTimeStrings()
    var record = getStateRecord(runtimeEnvironment, times.date)
    var rawData = ""
    if (record) {
        rawData = typeof record.getString === "function" ? (record.getString("data") || "") : record.get("data")
    }
    var data = record ? safeJsonParse(rawData) : {}
    if (!data.status) data.status = "requested"
    return {
        ok: true,
        environment: runtimeEnvironment,
        date: times.date,
        state_key: IBKR_2FA_STATE_KEY,
        record: record,
        data: data,
    }
}

function saveState(environment, patch, options) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var times = timeUtils.getTimeStrings()
    var record = getStateRecord(runtimeEnvironment, times.date)
    var collection = $app.findCollectionByNameOrId("ibkr_state")
    if (!record) {
        record = new Record(collection, {})
    }

    var existingRaw = ""
    if (record && record.id) {
        existingRaw = typeof record.getString === "function" ? (record.getString("data") || "") : record.get("data")
    }
    var current = record && record.id ? safeJsonParse(existingRaw) : {}
    var next = {
        ...current,
        ...(patch || {}),
        updated_at: times.us,
    }

    record.set("state_key", IBKR_2FA_STATE_KEY)
    record.set("date", times.date)
    record.set("environment", runtimeEnvironment)
    record.set("data", next)
    $app.save(record)

    return {
        environment: runtimeEnvironment,
        date: times.date,
        record: record,
        data: next,
    }
}

function ensureRequestedState(environment, options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var times = timeUtils.getTimeStrings()
    var status = currentData.status || ""
    var isActive = isActiveStatus(status)
    var keepActiveFlow = ["triggered", "waiting_confirm", "waiting_response"].indexOf(status) !== -1
    var preserveCurrentDisplay = isActive && !!currentData.message_id
    var nextStatus = (isActive && !opts.forceReset) || keepActiveFlow ? status : "requested"

    var patch = {
        status: nextStatus,
        reason: preserveCurrentDisplay
            ? (currentData.reason || opts.reason || "manual_reauth")
            : (opts.reason || currentData.reason || "manual_reauth"),
        detail: preserveCurrentDisplay
            ? (currentData.detail || opts.detail || {})
            : (opts.detail || currentData.detail || {}),
        source: preserveCurrentDisplay
            ? (currentData.source || opts.source || "ibkr_compute")
            : (opts.source || currentData.source || "ibkr_compute"),
        requested_at: currentData.requested_at || times.us,
        last_request_at: times.us,
        request_count: toNumber(currentData.request_count, 0) + 1,
    }

    if (!keepActiveFlow && (!isActive || opts.forceReset)) {
        patch.triggered_at = ""
        patch.result_at = ""
        patch.last_error = ""
        patch.last_result = ""
        patch.mode = ""
        patch.challenge_code = ""
        patch.challenge_detected_at = ""
        patch.response_code = ""
        patch.response_status = ""
        patch.response_received_at = ""
        patch.response_submitted_at = ""
    }

    if (preserveCurrentDisplay && currentData.message) {
        patch.message = currentData.message
    } else if (nextStatus === "requested") {
        if (opts.message) patch.message = opts.message
    } else if (!currentData.message && opts.message) {
        patch.message = opts.message
    }

    return saveState(runtimeEnvironment, patch)
}

function getStatusConfig(status) {
    return STATUS_CONFIG[status] || STATUS_CONFIG.requested
}

function buildActionButton(stateData, environment) {
    var cfg = getStatusConfig(stateData.status)
    return {
        tag: "button",
        type: "primary",
        width: "fill",
        text: { tag: "plain_text", content: cfg.button },
        action_type: "request",
        url: PB_HOST + "/webhook/feishu/callback",
        value: {
            action: "ibkr_2fa_start",
            environment: environment,
        },
    }
}

function buildOpenButton(label, url) {
    return {
        tag: "button",
        type: "default",
        width: "fill",
        text: { tag: "plain_text", content: label },
        multi_url: {
            url: url,
            pc_url: url,
            ios_url: url,
            android_url: url,
        }
    }
}

function build2faCard(stateData, environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || stateData.environment || "", envUtils.LIVE_ENVIRONMENT)
    var cfg = getStatusConfig(stateData.status)
    var summary = stateData.message || cfg.summary
    var detailMarkdown = buildDetailMarkdown(stateData.detail)
    var runtimeUrl = PB_HOST + "/ibkr_runtime.html?environment=" + encodeURIComponent(runtimeEnvironment)
    var systemUrl = PB_HOST + "/ibkr_system.html?environment=" + encodeURIComponent(runtimeEnvironment)
    var metaLines = [
        "**环境**: " + envUtils.getEnvironmentTag(runtimeEnvironment),
        "**状态**: " + cfg.emoji + " " + (stateData.status || "requested"),
        "**请求时间**: " + (stateData.requested_at || "-"),
    ]

    if (stateData.triggered_at) metaLines.push("**触发时间**: " + stateData.triggered_at)
    if (stateData.result_at) metaLines.push("**结果时间**: " + stateData.result_at)
    if (stateData.reason) metaLines.push("**触发原因**: " + stateData.reason)
    if (stateData.mode) metaLines.push("**验证模式**: " + getModeLabel(stateData.mode))
    if (stateData.challenge_code) metaLines.push("**Challenge**: " + stateData.challenge_code)
    if (stateData.response_received_at) metaLines.push("**响应码收到**: " + stateData.response_received_at)
    if (stateData.response_submitted_at) metaLines.push("**响应码提交**: " + stateData.response_submitted_at)
    if (stateData.last_result) metaLines.push("**反馈**: " + stateData.last_result)
    if (stateData.last_error) metaLines.push("**异常**: " + stateData.last_error)

    var elements = [
        {
            tag: "markdown",
            content: "**说明**: " + summary
        },
        {
            tag: "markdown",
            content: metaLines.join("\n")
        }
    ]

    if (detailMarkdown) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: detailMarkdown
        })
    }

    if (stateData.status === "waiting_confirm") {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: "**当前阶段**: 只需要点手机通知确认；如果卡片稍后变成 Challenge/Response，再改去 Runtime 页面提交 Response Code。"
        })
    }

    if (stateData.status === "waiting_response" && stateData.challenge_code) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: "**操作提示**: 这一步不是点手机推送。请在 IBKR App 的 Two-Factor Authentication 输入当前 Challenge，拿到 Response Code 后打开 Runtime 页面提交。"
        })
    }

    elements.push({ tag: "hr" })
    elements.push({
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            (stateData.status || "requested") !== "success"
                ? {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [buildActionButton(stateData, runtimeEnvironment)]
                }
                : null,
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [buildOpenButton("查看 Runtime", runtimeUrl)]
            },
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [buildOpenButton("查看 System", systemUrl)]
            }
        ].filter(Boolean)
    })

    return {
        schema: "2.0",
        config: { update_multi: true, wide_screen_mode: true },
        header: {
            title: {
                tag: "plain_text",
                content: cfg.emoji + " " + envUtils.labelTitleWithEnvironment(cfg.title, runtimeEnvironment)
            },
            template: cfg.template
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function persistDeliveryState(record, data, patch) {
    if (!record) return data || {}
    var next = {
        ...(data || {}),
        ...(patch || {}),
    }
    record.set("data", next)
    $app.save(record)
    return next
}

function deliverCard(savedState, options) {
    var opts = options || {}
    var lockKey = savedState.environment + ":" + savedState.date
    if (!acquireDeliveryLock(lockKey)) {
        return {
            ok: true,
            skipped: true,
            skipped_reason: "delivery_locked",
            environment: savedState.environment,
            date: savedState.date,
            card: build2faCard(savedState.data || {}, savedState.environment),
            message_id: (savedState.data && savedState.data.message_id) || "",
            data: savedState.data || {},
            result: { success: true, skipped: true, reason: "delivery_locked" },
        }
    }

    try {
        var latest = getStatePayload(savedState.environment)
        var effectiveState = latest && latest.record ? latest : savedState
        var stateData = effectiveState.data || {}
        var card = build2faCard(stateData, effectiveState.environment)
        var messageId = stateData.message_id || ""
        var now = Date.now()
        var fingerprint = buildDeliveryFingerprint(stateData)
        var lastDeliveredMs = toNumber(stateData.last_delivered_ms, 0)
        var lastDeliveredHash = String(stateData.last_delivered_hash || "")
        var lastDeliveredStatus = String(stateData.last_delivered_status || "")
        var allowReplace = opts.allowReplace === true || opts.forceNew === true
        var isSamePayload = lastDeliveredHash === fingerprint && lastDeliveredStatus === String(stateData.status || "")
        var throttled = (
            messageId &&
            !opts.forceNew &&
            !opts.bypassThrottle &&
            isActiveStatus(stateData.status) &&
            isSamePayload &&
            lastDeliveredMs > 0 &&
            (now - lastDeliveredMs) < CARD_UPDATE_COOLDOWN_MS
        )

        if (throttled) {
            return {
                ok: true,
                skipped: true,
                skipped_reason: "cooldown",
                environment: effectiveState.environment,
                date: effectiveState.date,
                card: card,
                message_id: messageId,
                data: stateData,
                result: { success: true, skipped: true, reason: "cooldown" },
            }
        }

        var result = null
        if (messageId && !opts.forceNew) {
            result = feishuApp.updateMessageCard(messageId, card)
            if (!result.success && allowReplace) {
                result = feishuApp.sendMessageDetailed("interactive", card, feishuSystem.getSystemChatId(effectiveState.environment), "chat_id")
            }
        } else {
            result = feishuApp.sendMessageDetailed("interactive", card, feishuSystem.getSystemChatId(effectiveState.environment), "chat_id")
        }

        if (result && result.success) {
            var persisted = persistDeliveryState(effectiveState.record, stateData, {
                message_id: result.message_id || messageId || "",
                last_delivered_ms: now,
                last_delivered_at: timeUtils.getTimeStrings().us,
                last_delivered_hash: fingerprint,
                last_delivered_status: stateData.status || "",
            })
            effectiveState.data = persisted
            return {
                ok: true,
                environment: effectiveState.environment,
                date: effectiveState.date,
                card: card,
                message_id: persisted.message_id || result.message_id || "",
                data: persisted,
                result: result,
            }
        }

        return {
            ok: false,
            environment: effectiveState.environment,
            date: effectiveState.date,
            card: card,
            message_id: messageId,
            data: stateData,
            result: result || { success: false, error: "send_failed" },
        }
    } finally {
        releaseDeliveryLock(lockKey)
    }
}

function request2faApproval(options) {
    var opts = options || {}
    var current = getStatePayload(opts.environment)
    var currentData = current.data || {}
    var saved = ensureRequestedState(opts.environment, {
        reason: opts.reason,
        detail: opts.detail,
        source: opts.source,
        message: opts.message,
        forceReset: !!opts.forceReset,
    })
    var currentStatus = currentData.status || ""
    var currentMessageId = currentData.message_id || ""
    var lastRequestPushMs = toNumber(currentData.last_request_push_ms, 0)
    var activeCardExists = isActiveStatus(currentStatus) && !!currentMessageId
    var shouldRenotify = (
        activeCardExists &&
        (Date.now() - lastRequestPushMs) >= REQUEST_RENOTIFY_COOLDOWN_MS
    )

    var delivered = activeCardExists && !opts.forceNew && !shouldRenotify
        ? {
            ok: true,
            skipped: true,
            skipped_reason: "active_card_reused",
            environment: saved.environment,
            date: saved.date,
            card: build2faCard(saved.data || {}, saved.environment),
            message_id: saved.data.message_id || currentMessageId,
            data: saved.data,
            result: { success: true, skipped: true, reason: "active_card_reused" },
        }
        : deliverCard(saved, { forceNew: !!opts.forceNew })

    if (delivered.ok && !delivered.skipped) {
        var refreshed = persistDeliveryState(saved.record, delivered.data || saved.data, {
            last_request_push_ms: Date.now(),
            last_request_push_at: timeUtils.getTimeStrings().us,
        })
        saved.data = refreshed
        delivered.data = refreshed
    }

    systemEvents.writeSystemEvent(
        "status_change",
        "warning",
        "ibkr_compute",
        "IBKR 2FA 等待触发",
        {
            reason: saved.data.reason || "manual_reauth",
            status: saved.data.status || "requested",
            ...(saved.data.detail || {}),
        },
        saved.environment,
        delivered.ok
    )

    return {
        ok: delivered.ok,
        environment: saved.environment,
        date: saved.date,
        status: saved.data.status || "requested",
        message_id: delivered.message_id,
        state: saved.data,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
    }
}

function parseHttpPayload(raw) {
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { raw: raw }
    }
}

function trigger2faFlow(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var callbackDriven = opts.source === "feishu_callback"

    if (["triggered", "waiting_confirm", "waiting_response"].indexOf(currentData.status || "") !== -1) {
        return {
            ok: true,
            environment: runtimeEnvironment,
            already_active: true,
            state: currentData,
            card: build2faCard(currentData, runtimeEnvironment),
        }
    }

    var triggerSaved = saveState(runtimeEnvironment, {
        status: "triggered",
        reason: opts.reason || currentData.reason || "manual_reauth",
        detail: opts.detail || currentData.detail || {},
        source: opts.source || currentData.source || "feishu_2fa",
        triggered_at: timeUtils.getTimeStrings().us,
        result_at: "",
        last_error: "",
        last_result: opts.message || "已触发登录流程，等待网关提交 2FA。",
        mode: "",
        challenge_code: "",
        challenge_detected_at: "",
        response_code: "",
        response_status: "",
        response_received_at: "",
        response_submitted_at: "",
    })

    var computeBaseUrl = envUtils.getIbkrComputePublicUrl(runtimeEnvironment, "https://qc.lzw-glory.top")
    try {
        $http.send({
            url: `${computeBaseUrl}/ibkr/stop`,
            method: "POST",
            timeout: 30,
        })
    } catch (_) {}

    try {
        var resp = $http.send({
            url: `${computeBaseUrl}/ibkr/start`,
            method: "POST",
            body: JSON.stringify({
                environment: runtimeEnvironment,
                trigger_login: true,
                source: opts.source || "feishu_2fa",
                reason: opts.reason || currentData.reason || "manual_reauth",
            }),
            headers: { "Content-Type": "application/json" },
            timeout: 30,
        })
        var payload = parseHttpPayload(resp.raw)
        if ((resp.statusCode || 500) >= 400 || payload.ok === false) {
            throw new Error(payload.error || payload.raw || ("http_" + (resp.statusCode || 500)))
        }

        var delivered = callbackDriven
            ? {
                ok: true,
                skipped: true,
                skipped_reason: "callback_card_response",
                environment: runtimeEnvironment,
                date: triggerSaved.date,
                card: build2faCard(triggerSaved.data, runtimeEnvironment),
                message_id: (triggerSaved.data && triggerSaved.data.message_id) || "",
                data: triggerSaved.data,
                result: { success: true, skipped: true, reason: "callback_card_response" },
            }
            : deliverCard(triggerSaved, { bypassThrottle: true })
        return {
            ok: true,
            environment: runtimeEnvironment,
            status_code: resp.statusCode || 200,
            upstream: computeBaseUrl + "/ibkr/start",
            state: delivered.data || triggerSaved.data,
            card: delivered.card,
            payload: payload,
        }
    } catch (err) {
        var failed = saveState(runtimeEnvironment, {
            status: "failed",
            result_at: timeUtils.getTimeStrings().us,
            last_error: err.message || String(err),
            last_result: "触发失败，请稍后重试。",
        })
        if (!callbackDriven) {
            deliverCard(failed, { bypassThrottle: true })
        }
        systemEvents.writeSystemEvent(
            "alert",
            "error",
            "manual",
            "IBKR 2FA 触发失败",
            {
                reason: failed.data.reason || "manual_reauth",
                error: err.message || String(err),
            },
            runtimeEnvironment,
            true
        )
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: err.message || String(err),
            state: failed.data,
            card: build2faCard(failed.data, runtimeEnvironment),
        }
    }
}

function report2faResult(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var status = String(opts.status || "requested").trim().toLowerCase() || "requested"
    if (!STATUS_CONFIG[status]) status = "failed"

    var patch = {
        status: status,
        detail: opts.detail || {},
        source: opts.source || "ibkr_compute",
        message: opts.message || "",
        last_result: opts.last_result || opts.message || "",
        ...(opts.state_patch && typeof opts.state_patch === "object" ? opts.state_patch : {}),
    }

    if (status === "triggered" || status === "waiting_confirm" || status === "waiting_response") {
        patch.triggered_at = (getStatePayload(runtimeEnvironment).data || {}).triggered_at || timeUtils.getTimeStrings().us
    }
    if (["success", "timeout", "failed"].indexOf(status) !== -1) {
        patch.result_at = timeUtils.getTimeStrings().us
    }
    if (opts.error) {
        patch.last_error = String(opts.error)
    } else if (status === "success") {
        patch.last_error = ""
    }

    var saved = saveState(runtimeEnvironment, patch)
    var delivered = deliverCard(saved, {
        bypassThrottle: isTerminalStatus(status),
    })

    if (["success", "timeout", "failed"].indexOf(status) !== -1) {
        systemEvents.writeSystemEvent(
            status === "success" ? "status_change" : "alert",
            status === "success" ? "info" : (status === "timeout" ? "warning" : "error"),
            "ibkr_compute",
            status === "success" ? "IBKR 2FA 完成" : (status === "timeout" ? "IBKR 2FA 超时" : "IBKR 2FA 失败"),
            {
                status: status,
                ...(saved.data.detail || {}),
                result: saved.data.last_result || "",
                error: saved.data.last_error || "",
            },
            runtimeEnvironment,
            delivered.ok
        )
    }

    return {
        ok: delivered.ok,
        environment: runtimeEnvironment,
        status: status,
        message_id: delivered.message_id,
        state: delivered.data || saved.data,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
    }
}

function submit2faResponse(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var responseCode = sanitizeResponseCode(opts.response_code)
    var expectedChallenge = String(currentData.challenge_code || "").trim()
    var submittedChallenge = String(opts.challenge_code || "").trim()

    if (!responseCode) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "response_code_required",
            state: currentData,
        }
    }

    if (!expectedChallenge) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_not_ready",
            state: currentData,
        }
    }

    if (["timeout", "failed"].indexOf(String(currentData.status || "").trim().toLowerCase()) !== -1) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_expired_retrigger_required",
            state: currentData,
        }
    }

    if (submittedChallenge && expectedChallenge && submittedChallenge !== expectedChallenge) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_mismatch",
            state: currentData,
        }
    }

    var saved = saveState(runtimeEnvironment, {
        status: currentData.status === "success" ? "success" : "waiting_response",
        response_code: responseCode,
        response_status: "pending",
        response_received_at: timeUtils.getTimeStrings().us,
        source: opts.source || currentData.source || "runtime_page",
        last_result: "已收到 Response Code，等待浏览器提交流程。",
    })
    var delivered = deliverCard(saved, { bypassThrottle: true })

    return {
        ok: delivered.ok,
        environment: runtimeEnvironment,
        status: (delivered.data || saved.data).status || "waiting_response",
        message_id: delivered.message_id || "",
        state: delivered.data || saved.data,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
    }
}

function handle2faCardCallback(c, options) {
    var opts = options || {}
    var action = opts.action || ""
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var updateToken = opts.updateToken || null
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || { status: "requested" }

    if (action !== "ibkr_2fa_start") {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "未知操作: " + action },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    if (currentData.status === "success") {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "当前已通过验证" },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    if (["triggered", "waiting_confirm", "waiting_response"].indexOf(currentData.status || "") !== -1) {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "info", content: "验证流程已经在进行中" },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    var result = trigger2faFlow({
        environment: runtimeEnvironment,
        source: "feishu_callback",
        reason: currentData.reason || "manual_reauth",
        detail: currentData.detail || {},
    })

    return feishuApp.sendFeishuCallbackResponse(c, {
        toast: {
            type: result.ok ? "success" : "error",
            content: result.ok
                ? "2FA 已触发，请在 IBKR Mobile 确认"
                : ("2FA 触发失败: " + (result.error || "unknown_error"))
        },
        card: { type: "raw", data: result.card || build2faCard(result.state || currentData, runtimeEnvironment) }
    }, updateToken)
}

module.exports = {
    IBKR_2FA_STATE_KEY: IBKR_2FA_STATE_KEY,
    getStatePayload: getStatePayload,
    build2faCard: build2faCard,
    request2faApproval: request2faApproval,
    trigger2faFlow: trigger2faFlow,
    report2faResult: report2faResult,
    submit2faResponse: submit2faResponse,
    handle2faCardCallback: handle2faCardCallback,
}
