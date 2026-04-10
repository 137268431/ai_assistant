function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") {
        return Boolean(fallback)
    }
    if (typeof value === "boolean") {
        return value
    }
    const normalized = String(value).trim().toLowerCase()
    if (["1", "true", "yes", "y", "on"].includes(normalized)) {
        return true
    }
    if (["0", "false", "no", "n", "off"].includes(normalized)) {
        return false
    }
    return Boolean(fallback)
}

function getSignalExtra(record) {
    if (!record) return {}
    const value = record.get("extra")
    if (value && typeof value === "object" && !Array.isArray(value)) {
        return value
    }
    if (typeof value === "string" && value) {
        try {
            const parsed = JSON.parse(value)
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
        } catch (_) {}
    }
    return {}
}

function signalConfirmationRequired(environment) {
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    return parseBoolean(
        getConfigValue("signal_manual_confirm_enabled", "true", environment),
        true
    )
}

function prepareSignalLifecycle(prepared, existingRecord, environment) {
    const existingStatus = existingRecord ? String(existingRecord.get("status") || "").trim() : ""
    const existingNote = existingRecord ? String(existingRecord.get("note") || "").trim() : ""
    const existingExtra = getSignalExtra(existingRecord)
    const incomingStatus = String(prepared.data.status || "pending").trim() || "pending"
    const incomingNote = String(prepared.data.note || "").trim()
    const manualConfirmEnabled = signalConfirmationRequired(environment)
    const finalStatuses = {
        executed: true,
        rejected: true,
        expired: true,
        closed: true,
    }

    let resolvedStatus = incomingStatus
    let resolvedNote = incomingNote

    if (finalStatuses[existingStatus]) {
        resolvedStatus = existingStatus
        resolvedNote = existingNote || incomingNote
    } else if (manualConfirmEnabled) {
        if (existingStatus === "pending") {
            resolvedStatus = "pending"
            resolvedNote = existingNote || incomingNote
        } else if (incomingStatus === "pending" || incomingStatus === "awaiting_confirm") {
            resolvedStatus = "awaiting_confirm"
            resolvedNote = incomingNote || "manual_confirmation_required"
        }
    } else if (incomingStatus === "pending" || incomingStatus === "awaiting_confirm") {
        resolvedStatus = "pending"
        resolvedNote = incomingNote === "manual_confirmation_required" ? "" : incomingNote
    }

    prepared.data.status = resolvedStatus
    prepared.data.note = resolvedNote
    prepared.data.extra = {
        ...existingExtra,
        ...(prepared.data.extra || {}),
        signal_confirmation_required: manualConfirmEnabled,
        signal_confirmation_mode: manualConfirmEnabled ? "manual" : "auto",
    }
    if (resolvedNote) {
        prepared.data.extra.status_reason = resolvedNote
    }

    return {
        previous_status: existingStatus,
        next_status: resolvedStatus,
    }
}

function syncSignalNotification(record, previousStatus) {
    if (!record) return
    const currentStatus = String(record.get("status") || "").trim()
    if (currentStatus !== "awaiting_confirm" && currentStatus !== "pending" && currentStatus !== "rejected") {
        return
    }

    const { getSignalExtra, mergeSignalExtra, notifyNewSignal, notifySignalStatus } = require(`${__hooks}/lib/feishu_signal.js`)
    const signalExtra = getSignalExtra(record)
    let syncResult = null

    if (currentStatus === "rejected") {
        syncResult = notifySignalStatus("rejected", record, {
            messageId: signalExtra.feishu_signal_message_id || "",
        })
    } else if (!signalExtra.feishu_signal_message_id) {
        syncResult = notifyNewSignal(record)
    } else if (previousStatus !== currentStatus) {
        syncResult = notifySignalStatus(currentStatus, record, {
            messageId: signalExtra.feishu_signal_message_id || "",
            message: currentStatus === "awaiting_confirm" ? "等待人工确认" : "信号已确认，等待执行",
        })
    }

    if (syncResult && syncResult.success && syncResult.message_id && syncResult.message_id !== signalExtra.feishu_signal_message_id) {
        mergeSignalExtra(record, {
            feishu_signal_message_id: syncResult.message_id,
            feishu_signal_card_version: 1,
        }, true)
    }
}

module.exports = {
    prepareSignalLifecycle: prepareSignalLifecycle,
    syncSignalNotification: syncSignalNotification,
}
