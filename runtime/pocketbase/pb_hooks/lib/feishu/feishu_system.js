/**
 * feishu_system.js
 * 系统状态/异常/心跳 飞书通知
 * 正常状态群默认: oc_b7b52fc28816d90e27ce50ca7922a9ac
 * 2FA 专用群默认: oc_c48c10447685e80cfea0c003864aa51f
 * 异常告警群默认: oc_91aa4f84bc6fedb125b1a263d91d4104
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)
var usEasternTime = require(`${__hooks}/lib/runtime/us_eastern_time.js`)

var PB_HOST = "https://pb.lzw-glory.top"
var SYSTEM_CHAT_ID = "oc_b7b52fc28816d90e27ce50ca7922a9ac"
var TWO_FA_CHAT_ID = "oc_c48c10447685e80cfea0c003864aa51f"
var ALERT_CHAT_ID = "oc_91aa4f84bc6fedb125b1a263d91d4104"

var LEVEL_CONFIG = {
    info:    { emoji: "✅", color: "green",  template: "green" },
    warning: { emoji: "⚠️", color: "yellow", template: "yellow" },
    error:   { emoji: "🚨", color: "red",    template: "red" }
}

var SOURCE_LABELS = {
    qc: "IBKR Data",
    ibkr_compute: "IBKR Compute",
    pb: "PocketBase",
    manual: "手动"
}

function getLevelConfig(level) {
    return LEVEL_CONFIG[String(level || "info").trim().toLowerCase()] || LEVEL_CONFIG.info
}

function resolveCardLevelConfig(level, options) {
    var overrideLevel = String(options && options.template_level_override || "").trim().toLowerCase()
    return getLevelConfig(overrideLevel || level)
}

function normalizeTargetChat(value) {
    var normalized = String(value || "").trim().toLowerCase()
    if (normalized === "system" || normalized === "alert" || normalized === "2fa") {
        return normalized
    }
    return ""
}

function getTimeStrings() {
    var nowMs = Date.now()
    return {
        us: usEasternTime.formatUsEasternTime(nowMs),
        cn: usEasternTime.formatCnTime(nowMs)
    }
}

function buildSystemCard(level, source, title, detailFields, environment, options) {
    var levelCfg = getLevelConfig(level)
    var cardCfg = resolveCardLevelConfig(level, options)
    var srcLabel = SOURCE_LABELS[source] || source
    var times = getTimeStrings()
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)

    var elements = []

    elements.push({
        tag: "markdown",
        content: "**来源**: " + srcLabel + "  |  **级别**: " + levelCfg.emoji + " " + level.toUpperCase()
    })

    if (detailFields && detailFields.length > 0) {
        var mdLines = []
        for (var i = 0; i < detailFields.length; i++) {
            var f = detailFields[i]
            mdLines.push("**" + f.label + "**: " + f.value)
        }
        elements.push({
            tag: "markdown",
            content: mdLines.join("\n")
        })
    }

    elements.push({
        tag: "markdown",
        content: "🕐 美东 " + times.us + " | 北京 " + times.cn
    })

    elements.push({
        tag: "action",
        actions: [{
            tag: "button",
            text: { tag: "plain_text", content: "📊 查看系统状态" },
            type: "default",
            multi_url: { url: PB_HOST + "/ibkr_system.html?environment=" + encodeURIComponent(runtimeEnvironment) }
        }]
    })

    return {
        type: "template",
        data: {
            template_id: "ctp_AA0vSwsFswsr",
            template_variable: {
                title: cardCfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment),
                content: JSON.stringify({
                    config: { wide_screen_mode: true },
                    header: {
                        title: { tag: "plain_text", content: cardCfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment) },
                        template: cardCfg.template
                    },
                    elements: elements
                })
            }
        }
    }
}

function buildSimpleCard(level, source, title, detailFields, environment, options) {
    var levelCfg = getLevelConfig(level)
    var cardCfg = resolveCardLevelConfig(level, options)
    var srcLabel = SOURCE_LABELS[source] || source
    var times = getTimeStrings()
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)

    var elements = []

    elements.push({
        tag: "markdown",
        content: "**来源**: " + srcLabel + "  |  **级别**: " + levelCfg.emoji + " " + level.toUpperCase()
    })

    if (detailFields && detailFields.length > 0) {
        var mdLines = []
        for (var i = 0; i < detailFields.length; i++) {
            var f = detailFields[i]
            mdLines.push("**" + f.label + "**: " + f.value)
        }
        elements.push({
            tag: "markdown",
            content: mdLines.join("\n")
        })
    }

    elements.push({
        tag: "markdown",
        content: "🕐 美东 " + times.us + " | 北京 " + times.cn
    })

    elements.push({
        tag: "action",
        actions: [{
            tag: "button",
            text: { tag: "plain_text", content: "📊 查看系统状态" },
            type: "default",
            multi_url: { url: PB_HOST + "/ibkr_system.html?environment=" + encodeURIComponent(runtimeEnvironment) }
        }]
    })

    var card = {
        config: { wide_screen_mode: true },
        header: {
            title: { tag: "plain_text", content: cardCfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment) },
            template: cardCfg.template
        },
        elements: elements
    }

    return card
}

function isEnabledText(value) {
    var text = String(value == null ? "" : value).trim().toLowerCase()
    return text !== "false" && text !== "0" && text !== "off" && text !== "no"
}

function getConfigValue(key, defaultValue, environment) {
    return envUtils.getConfigValue(key, defaultValue, environment)
}

function isFeishuNotificationEnabled(environment) {
    return true
}

function getSystemChatId(environment) {
    return String(getConfigValue("system_status_chat_id", SYSTEM_CHAT_ID, environment) || SYSTEM_CHAT_ID).trim() || SYSTEM_CHAT_ID
}

function get2faChatId(environment) {
    return String(getConfigValue("system_2fa_chat_id", TWO_FA_CHAT_ID, environment) || TWO_FA_CHAT_ID).trim() || TWO_FA_CHAT_ID
}

function getAlertChatId(environment) {
    return String(getConfigValue("system_alert_chat_id", ALERT_CHAT_ID, environment) || ALERT_CHAT_ID).trim() || ALERT_CHAT_ID
}

function isLikely2faAlert(title, detail) {
    var normalizedTitle = String(title || "").trim()
    if (!normalizedTitle) return false
    if (normalizedTitle.indexOf("2FA") !== -1) return true

    var authTitles = [
        "IBKR Session 已失效",
        "IBKR Runtime 未认证",
        "IBKR Session 长时间未恢复认证",
    ]
    for (var i = 0; i < authTitles.length; i++) {
        if (normalizedTitle.indexOf(authTitles[i]) !== -1) {
            return true
        }
    }

    if (!detail || typeof detail !== "object") {
        return false
    }

    return (
        Object.prototype.hasOwnProperty.call(detail, "2FA状态")
        && (
            Object.prototype.hasOwnProperty.call(detail, "Session认证")
            || Object.prototype.hasOwnProperty.call(detail, "验证模式")
            || Object.prototype.hasOwnProperty.call(detail, "Challenge")
            || Object.prototype.hasOwnProperty.call(detail, "响应状态")
        )
    )
}

function shouldNotifyEvent(eventType, level, source, title, environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var normalizedEventType = String(eventType || "status_change").trim().toLowerCase()
    var normalizedLevel = String(level || "info").trim().toLowerCase()
    var normalizedTitle = String(title || "")
    var normalizedSource = String(source || "").trim().toLowerCase()

    if (!isFeishuNotificationEnabled(runtimeEnvironment)) {
        return false
    }
    if (normalizedEventType === "daily_report") {
        return isEnabledText(getConfigValue("daily_summary_notify_enabled", "TRUE", runtimeEnvironment))
    }
    if (normalizedEventType === "heartbeat") {
        if (normalizedLevel === "warning" || normalizedLevel === "error") {
            return isEnabledText(getConfigValue("inspection_notify_enabled", "TRUE", runtimeEnvironment))
        }
        return isEnabledText(getConfigValue("health_check_notify_enabled", "TRUE", runtimeEnvironment))
    }
    if (normalizedLevel === "warning" || normalizedLevel === "error" || normalizedEventType === "alert") {
        return isEnabledText(getConfigValue("inspection_notify_enabled", "TRUE", runtimeEnvironment))
    }
    if (
        normalizedSource === "manual"
        && (normalizedTitle.indexOf("停止") !== -1 || normalizedTitle.indexOf("急停") !== -1 || normalizedTitle.toLowerCase().indexOf("stop") !== -1)
    ) {
        return isEnabledText(getConfigValue("manual_stop_notify_enabled", "TRUE", runtimeEnvironment))
    }
    return isEnabledText(getConfigValue("status_notify_enabled", "TRUE", runtimeEnvironment))
}

function getTargetChatId(eventType, level, environment, source, title, detail, options) {
    var normalizedEventType = String(eventType || "status_change").trim().toLowerCase()
    var normalizedLevel = String(level || "info").trim().toLowerCase()
    var targetChat = normalizeTargetChat(options && options.target_chat)
    if (targetChat === "system") {
        return getSystemChatId(environment)
    }
    if (targetChat === "alert") {
        return getAlertChatId(environment)
    }
    if (targetChat === "2fa") {
        return get2faChatId(environment)
    }
    if (isLikely2faAlert(title, detail)) {
        return get2faChatId(environment)
    }
    if (normalizedLevel === "warning" || normalizedLevel === "error" || normalizedEventType === "alert") {
        return getAlertChatId(environment)
    }
    return getSystemChatId(environment)
}

function buildDetailFields(detail) {
    var detailFields = []
    if (detail && typeof detail === "object") {
        var keys = Object.keys(detail)
        for (var i = 0; i < keys.length; i++) {
            detailFields.push({ label: keys[i], value: String(detail[keys[i]]) })
        }
    } else if (detail && typeof detail === "string") {
        detailFields.push({ label: "详情", value: detail })
    }
    return detailFields
}

function notifySystemEventDetailed(eventType, level, source, title, detail, environment, options) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var detailFields = buildDetailFields(detail)
    var messageId = String((options && (options.message_id || options.messageId)) || "").trim()

    if (!shouldNotifyEvent(eventType, level, source, title, runtimeEnvironment)) {
        return {
            success: false,
            skipped: true,
            message_id: messageId,
            reason: "notify_disabled"
        }
    }

    if (runtimeEnvironment === "paper") {
        console.log("[FeishuSystem] 发送跳过: " + title + " environment=paper")
        return {
            success: false,
            skipped: true,
            suppressed: true,
            message_id: messageId,
            reason: "paper_environment_disabled"
        }
    }

    var card = buildSimpleCard(level, source, title, detailFields, runtimeEnvironment, options)
    var result = messageId
        ? feishuApp.updateMessageCard(messageId, card, runtimeEnvironment)
        : feishuApp.sendMessageDetailed(
            "interactive",
            card,
            getTargetChatId(eventType, level, runtimeEnvironment, source, title, detail, options),
            "chat_id",
            runtimeEnvironment
        )
    if (!result || typeof result !== "object") {
        result = { success: false, message_id: messageId, error: "empty_result" }
    }
    if (!result.message_id && messageId) {
        result.message_id = messageId
    }
    if (messageId) {
        result.updated = !!result.success
    }
    if (!result.success) {
        console.error("[FeishuSystem] 发送失败: " + title)
    }
    return result
}

function notifySystemEvent(eventType, level, source, title, detail, environment, options) {
    var result = notifySystemEventDetailed(eventType, level, source, title, detail, environment, options)
    return !!(result && result.success && !result.suppressed)
}

function notifyHeartbeat(source, status, extra) {
    var runtimeEnvironment = extra && extra.environment ? extra.environment : envUtils.LIVE_ENVIRONMENT
    var detail = { "状态": status }
    if (extra) {
        var keys = Object.keys(extra)
        for (var i = 0; i < keys.length; i++) {
            detail[keys[i]] = extra[keys[i]]
        }
    }
    var level = status === "ok" || status === "running" ? "info" : "error"
    return notifySystemEvent("heartbeat", level, source, source + " 心跳 - " + status, detail, runtimeEnvironment)
}

function notifyAlert(source, title, detail, environment) {
    return notifySystemEvent("alert", "error", source, title, detail, environment)
}

function notifyWarning(source, title, detail, environment) {
    return notifySystemEvent("alert", "warning", source, title, detail, environment)
}

function notifyStatusChange(source, title, detail, environment) {
    return notifySystemEvent("status_change", "info", source, title, detail, environment)
}

function notifyComputeStats(stats, environment) {
    return notifySystemEvent("compute_stats", "info", "ibkr_compute", "计算完成", stats, environment)
}

function notifyDailyReport(report, environment) {
    return notifySystemEvent("daily_report", "info", "pb", "每日汇总", report, environment)
}

module.exports = {
    notifySystemEvent: notifySystemEvent,
    notifySystemEventDetailed: notifySystemEventDetailed,
    notifyHeartbeat: notifyHeartbeat,
    notifyAlert: notifyAlert,
    notifyWarning: notifyWarning,
    notifyStatusChange: notifyStatusChange,
    notifyComputeStats: notifyComputeStats,
    notifyDailyReport: notifyDailyReport,
    buildSimpleCard: buildSimpleCard,
    SYSTEM_CHAT_ID: SYSTEM_CHAT_ID,
    TWO_FA_CHAT_ID: TWO_FA_CHAT_ID,
    ALERT_CHAT_ID: ALERT_CHAT_ID,
    getSystemChatId: getSystemChatId,
    get2faChatId: get2faChatId,
    getAlertChatId: getAlertChatId,
}
