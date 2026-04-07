/**
 * feishu_system.js
 * 系统状态/异常/心跳 飞书通知
 * 正常状态群默认: oc_b7b52fc28816d90e27ce50ca7922a9ac
 * 异常告警群默认: oc_91aa4f84bc6fedb125b1a263d91d4104
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)

var PB_HOST = "https://pb.lzw-glory.top"
var SYSTEM_CHAT_ID = "oc_b7b52fc28816d90e27ce50ca7922a9ac"
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

function getTimeStrings() {
    var now = new Date()
    var usOffset = -4 * 60
    var cnOffset = 8 * 60
    var usTime = new Date(now.getTime() + usOffset * 60000)
    var cnTime = new Date(now.getTime() + cnOffset * 60000)
    return {
        us: usTime.toISOString().slice(0, 19).replace("T", " "),
        cn: cnTime.toISOString().slice(0, 19).replace("T", " ")
    }
}

function buildSystemCard(level, source, title, detailFields, environment) {
    var cfg = LEVEL_CONFIG[level] || LEVEL_CONFIG.info
    var srcLabel = SOURCE_LABELS[source] || source
    var times = getTimeStrings()
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)

    var elements = []

    elements.push({
        tag: "markdown",
        content: "**来源**: " + srcLabel + "  |  **级别**: " + cfg.emoji + " " + level.toUpperCase()
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
                title: cfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment),
                content: JSON.stringify({
                    config: { wide_screen_mode: true },
                    header: {
                        title: { tag: "plain_text", content: cfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment) },
                        template: cfg.template
                    },
                    elements: elements
                })
            }
        }
    }
}

function buildSimpleCard(level, source, title, detailFields, environment) {
    var cfg = LEVEL_CONFIG[level] || LEVEL_CONFIG.info
    var srcLabel = SOURCE_LABELS[source] || source
    var times = getTimeStrings()
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)

    var elements = []

    elements.push({
        tag: "markdown",
        content: "**来源**: " + srcLabel + "  |  **级别**: " + cfg.emoji + " " + level.toUpperCase()
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
            title: { tag: "plain_text", content: cfg.emoji + " " + envUtils.labelTitleWithEnvironment(title, runtimeEnvironment) },
            template: cfg.template
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

function getAlertChatId(environment) {
    return String(getConfigValue("system_alert_chat_id", ALERT_CHAT_ID, environment) || ALERT_CHAT_ID).trim() || ALERT_CHAT_ID
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

function getTargetChatId(eventType, level, environment) {
    var normalizedEventType = String(eventType || "status_change").trim().toLowerCase()
    var normalizedLevel = String(level || "info").trim().toLowerCase()
    if (normalizedLevel === "warning" || normalizedLevel === "error" || normalizedEventType === "alert") {
        return getAlertChatId(environment)
    }
    return getSystemChatId(environment)
}

function notifySystemEvent(eventType, level, source, title, detail, environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var detailFields = []
    if (detail && typeof detail === "object") {
        var keys = Object.keys(detail)
        for (var i = 0; i < keys.length; i++) {
            detailFields.push({ label: keys[i], value: String(detail[keys[i]]) })
        }
    } else if (detail && typeof detail === "string") {
        detailFields.push({ label: "详情", value: detail })
    }

    if (!shouldNotifyEvent(eventType, level, source, title, runtimeEnvironment)) {
        return false
    }

    if (runtimeEnvironment === "paper") {
        console.log("[FeishuSystem] 发送跳过: " + title + " environment=paper")
        return false
    }

    var card = buildSimpleCard(level, source, title, detailFields, runtimeEnvironment)
    var success = feishuApp.sendMessage("interactive", card, getTargetChatId(eventType, level, runtimeEnvironment), "chat_id", runtimeEnvironment)
    if (!success) {
        console.error("[FeishuSystem] 发送失败: " + title)
    }
    return success
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
    notifyHeartbeat: notifyHeartbeat,
    notifyAlert: notifyAlert,
    notifyWarning: notifyWarning,
    notifyStatusChange: notifyStatusChange,
    notifyComputeStats: notifyComputeStats,
    notifyDailyReport: notifyDailyReport,
    buildSimpleCard: buildSimpleCard,
    SYSTEM_CHAT_ID: SYSTEM_CHAT_ID,
    ALERT_CHAT_ID: ALERT_CHAT_ID,
    getSystemChatId: getSystemChatId,
    getAlertChatId: getAlertChatId,
}
