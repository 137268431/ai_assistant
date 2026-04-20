const usEasternTime = typeof __hooks !== "undefined"
    ? require(`${__hooks}/lib/runtime/us_eastern_time.js`)
    : require("./us_eastern_time.js")

const US_OFFSET_MINUTES = usEasternTime.US_EASTERN_DST_OFFSET_MINUTES
const CN_OFFSET_MINUTES = usEasternTime.CN_OFFSET_MINUTES
const WEEKLY_REAUTH_REASON = "weekly_reauth"
const WEEKLY_PREAMARKET_HOUR = 4
const WEEKLY_PREAMARKET_MINUTE = 0
const DEFAULT_CONFIRM_TIMEOUT_SECONDS = 180

function toFiniteNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : fallback
}

function parseShiftedTimeMs(value, offsetMinutes) {
    const text = String(value || "").trim()
    if (!text) return 0
    const parsed = Date.parse(text.replace(" ", "T") + "Z")
    if (!Number.isFinite(parsed)) return 0
    return parsed - (Number(offsetMinutes || 0) * 60000)
}

function formatShiftedTime(ms, offsetMinutes) {
    const safeMs = toFiniteNumber(ms, 0)
    if (safeMs <= 0) return ""
    return new Date(safeMs + Number(offsetMinutes || 0) * 60000)
        .toISOString()
        .slice(0, 19)
        .replace("T", " ")
}

function getShiftedParts(ms, offsetMinutes) {
    const safeMs = toFiniteNumber(ms, 0)
    if (safeMs <= 0) {
        return {
            year: 0,
            month: 0,
            day: 0,
            weekday: 0,
            hour: 0,
            minute: 0,
            second: 0,
        }
    }
    const shifted = new Date(safeMs + Number(offsetMinutes || 0) * 60000)
    return {
        year: shifted.getUTCFullYear(),
        month: shifted.getUTCMonth() + 1,
        day: shifted.getUTCDate(),
        weekday: shifted.getUTCDay(),
        hour: shifted.getUTCHours(),
        minute: shifted.getUTCMinutes(),
        second: shifted.getUTCSeconds(),
    }
}

function buildShiftedTimestampMs(parts, offsetMinutes) {
    const year = toFiniteNumber(parts && parts.year, 0)
    const month = toFiniteNumber(parts && parts.month, 0)
    const day = toFiniteNumber(parts && parts.day, 0)
    const hour = toFiniteNumber(parts && parts.hour, 0)
    const minute = toFiniteNumber(parts && parts.minute, 0)
    const second = toFiniteNumber(parts && parts.second, 0)
    if (year <= 0 || month <= 0 || day <= 0) return 0
    return Date.UTC(year, month - 1, day, hour, minute, second) - (Number(offsetMinutes || 0) * 60000)
}

function getWeeklyReauthBusinessDeadline(baseMs, options) {
    const safeBaseMs = toFiniteNumber(baseMs, Date.now())
    const safeNowMs = toFiniteNumber(options && options.nowMs, safeBaseMs)
    const usParts = usEasternTime.getUsEasternParts(safeBaseMs)
    const mondayDate = new Date(Date.UTC(
        usParts.year,
        usParts.month - 1,
        usParts.day + (usParts.weekday === 0 ? 1 : (1 - usParts.weekday)),
        0,
        0,
        0
    ))
    const deadlineMs = usEasternTime.buildUsEasternTimestampMs({
        year: mondayDate.getUTCFullYear(),
        month: mondayDate.getUTCMonth() + 1,
        day: mondayDate.getUTCDate(),
        hour: WEEKLY_PREAMARKET_HOUR,
        minute: WEEKLY_PREAMARKET_MINUTE,
        second: 0,
    })

    return {
        business_deadline_ms: deadlineMs,
        business_deadline_at: usEasternTime.formatUsEasternTime(deadlineMs),
        business_deadline_cn: usEasternTime.formatCnTime(deadlineMs),
        business_deadline_label: "美股周一盘前前完成验证",
        business_deadline_overdue: safeNowMs > deadlineMs,
    }
}

function getConfirmDeadline(triggeredAt, options) {
    const opts = options || {}
    const timeoutSeconds = Math.max(1, Math.round(toFiniteNumber(opts.confirmTimeoutSeconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS)))
    const triggeredMs = typeof triggeredAt === "number"
        ? toFiniteNumber(triggeredAt, 0)
        : usEasternTime.parseUsEasternTimeMs(triggeredAt)
    if (triggeredMs <= 0) {
        return {
            confirm_window_seconds: timeoutSeconds,
            confirm_deadline_ms: 0,
            confirm_deadline_at: "",
            confirm_deadline_cn: "",
            confirm_deadline_overdue: false,
        }
    }
    const nowMs = toFiniteNumber(opts.nowMs, Date.now())
    const deadlineMs = triggeredMs + timeoutSeconds * 1000
    return {
        confirm_window_seconds: timeoutSeconds,
        confirm_deadline_ms: deadlineMs,
        confirm_deadline_at: usEasternTime.formatUsEasternTime(deadlineMs),
        confirm_deadline_cn: usEasternTime.formatCnTime(deadlineMs),
        confirm_deadline_overdue: nowMs > deadlineMs,
    }
}

function deriveTwoFactorDeadlines(stateData, options) {
    const state = stateData && typeof stateData === "object" ? stateData : {}
    const opts = options || {}
    const nowMs = toFiniteNumber(opts.nowMs, Date.now())
    const requestedMs = usEasternTime.parseUsEasternTimeMs(state.requested_at)
    const triggeredMs = usEasternTime.parseUsEasternTimeMs(state.triggered_at)
    const status = String(state.status || "").trim().toLowerCase()
    const reason = String(state.reason || state.recovery_reason || "").trim().toLowerCase()
    const deadlines = {
        business_deadline_ms: 0,
        business_deadline_at: "",
        business_deadline_cn: "",
        business_deadline_label: "",
        business_deadline_overdue: false,
        confirm_window_seconds: Math.max(1, Math.round(toFiniteNumber(opts.confirmTimeoutSeconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS))),
        confirm_deadline_ms: 0,
        confirm_deadline_at: "",
        confirm_deadline_cn: "",
        confirm_deadline_overdue: false,
    }

    if (reason === WEEKLY_REAUTH_REASON) {
        Object.assign(deadlines, getWeeklyReauthBusinessDeadline(requestedMs || triggeredMs || nowMs, { nowMs: nowMs }))
    }

    if (["triggered", "waiting_confirm", "waiting_response", "timeout", "failed"].includes(status)) {
        Object.assign(deadlines, getConfirmDeadline(triggeredMs, {
            nowMs: nowMs,
            confirmTimeoutSeconds: deadlines.confirm_window_seconds,
        }))
    }

    return deadlines
}

module.exports = {
    US_OFFSET_MINUTES,
    CN_OFFSET_MINUTES,
    WEEKLY_REAUTH_REASON,
    WEEKLY_PREAMARKET_HOUR,
    WEEKLY_PREAMARKET_MINUTE,
    DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    parseShiftedTimeMs,
    formatShiftedTime,
    getWeeklyReauthBusinessDeadline,
    getConfirmDeadline,
    deriveTwoFactorDeadlines,
}
